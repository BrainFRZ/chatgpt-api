"""
Ship combat mode tests (mocked / no real API calls).

Covers:
- ship combat trigger/handoff initialization
- strong vs thin trigger hidden bootstrap behavior
- ship combat SSE notifications for npc_actions
- saved chat metadata for hidden bootstrap and first-turn flags
- debug transcript visibility for hidden bootstrap messages
- collapse/migration/notification extraction helpers
- ship combat state apply/preservation behavior
"""

import copy
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main
from pipeline import (
    collapse_ship_combat_messages,
    extract_ship_combat_notifications,
    generate_debug_transcript,
    migrate_pipeline_state,
)
from game_systems.dnd5e_cyber import (
    REPORT_SHIP_COMBAT_STATE_TOOL,
    apply_ship_combat_state,
    build_ship_combat_injection,
)
from providers.openai_provider import OpenAIProvider


def _parse_sse_events(text: str):
    events = []
    event_type = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event_type = line[7:]
        elif line.startswith("data: ") and event_type:
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                data = line[6:]
            events.append((event_type, data))
            event_type = None
    return events


def _ship_entry(name: str, hull=100, shields=50):
    return {
        "data": {
            "type": "ship",
            "vitals": [
                {"label": "Hull", "current": hull, "max": hull},
                {"label": "Shields", "current": shields, "max": shields},
            ],
            "resources": [{"label": "Railgun Ammo", "current": 10, "max": 10}],
            "conditions": [],
            "summary": f"{name} test hull",
        },
        "last_updated": 1,
    }


def _base_pipeline_state(ship_combat_overrides=None):
    ship_combat = {
        "round": 1,
        "initiative_order": [],
        "current_ship": None,
        "current_role": None,
        "environment": "Open Space",
        "bootstrap_done": False,
        "ship_combat_handoff_source": None,
        "bootstrap_messages": [],
        "enemy_ships": [{"name": "Red Knife", "faction": "pirate"}],
    }
    if ship_combat_overrides:
        ship_combat.update(ship_combat_overrides)
    return {
        "pacing": {},
        "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
        "npc_memories": {},
        "scene_state": {"location": "Outer Belt", "pcs_present": ["Crew"], "npcs_present": []},
        "character_states": {
            "Warden": _ship_entry("Warden", hull=120, shields=60),
            "Red Knife": _ship_entry("Red Knife", hull=90, shields=40),
        },
        "game_state": {},
        "hud_state": {},
        "combat": None,
        "ship_combat": ship_combat,
        "turn_counter": 1,
    }


@pytest.fixture
def temp_data_dir(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    monkeypatch.setattr(main, "DATA_DIR", Path(temp_dir))
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def ship_chat(temp_data_dir):
    username = "shiptester"
    project = "orbit"
    chat_name = "combat"

    user_dir = Path(temp_data_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "api_keys.json").write_text(json.dumps({"openai": "sk-fake", "anthropic": "ak-fake"}))

    project_dir = user_dir / "projects" / project
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "metadata.json").write_text(json.dumps({
        "model": "gpt-5.2",
        "game_system": "dnd5e_cyber",
        "last_accessed": "2026-01-01T00:00:00",
    }))
    (project_dir / "instructions.di").write_text("GM instructions")
    uploads = project_dir / "uploads"
    uploads.mkdir(exist_ok=True)
    (uploads / "Ship Systems.md").write_text("# Ship Systems\n")
    (uploads / "Character Sheets.md").write_text("## Crew\n")
    (project_dir / "file_tokens.json").write_text(json.dumps({}))

    chat_data = {
        "messages": [
            {
                "id": "sys-001",
                "role": "system",
                "content": "System prompt",
                "total_tokens": 10,
            },
            {
                "id": "asst-001",
                "parent_id": "sys-001",
                "role": "assistant",
                "content": "Sensors flare with contacts.",
                "timestamp": "2026-01-01T00:00:01",
                "model": "claude-opus-4.5",
                "total_tokens": 12,
            },
        ],
        "current_leaf_id": "asst-001",
        "stats": {
            "total_input_tokens": 0,
            "total_cached_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "total_cost": 0,
            "total_prompts": 0,
            "last_accessed": "2026-01-01T00:00:00+00:00",
            "first_prompt_date": None,
        },
        "pipeline_state": _base_pipeline_state(),
    }
    (project_dir / f"chat_{chat_name}.json").write_text(json.dumps(chat_data))
    return {"username": username, "project": project, "chat_name": chat_name, "project_dir": project_dir}


def _mock_ship_combat_json(*, complete=False, include_npc_actions=True):
    payload = {
        "narrative": "The pirate cutter rolls through the freighter's wake and opens fire.",
        "ship_updates": [
            {"ship_name": "Red Knife", "shield_delta": -12, "conditions_add": ["weapons: damaged"]},
        ],
        "character_updates": [],
        "ship_combat": None if complete else {
            "round": 1,
            "initiative_order": [
                {"ship_name": "Warden", "initiative": 16, "faction": "ally"},
                {"ship_name": "Red Knife", "initiative": 12, "faction": "enemy"},
            ],
            "current_ship": "Red Knife",
            "current_role": "gunner",
            "environment": "Open Space",
        },
        "ship_combat_complete": bool(complete),
        "narrative_summary": "The pirates break off and surrender after their shields collapse." if complete else None,
        "npc_actions": [],
    }
    if include_npc_actions:
        payload["npc_actions"] = [
            {
                "ship_name": "Red Knife",
                "role": "gunner",
                "character_name": None,
                "action": "Fire Railgun",
                "effect": "18 vs AC 14 — hit, 12 damage to shields",
            }
        ]
    return payload


def _patch_gpt_provider(monkeypatch, responses, build_calls_out):
    """Patch OpenAIProvider methods. `responses` is a list of dicts returned in order."""
    response_iter = iter(responses)

    def fake_build_pipeline_request(self, **kwargs):
        build_calls_out.append(kwargs)
        return {"messages": kwargs.get("messages", []), "stage_name": kwargs.get("stage_name")}

    def fake_non_streaming(self, client, params, timeout=None):
        try:
            return next(response_iter)
        except StopIteration:
            raise AssertionError("Unexpected extra send_request_non_streaming call")

    async def _noop_broadcast(*args, **kwargs):
        return 0

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(OpenAIProvider, "get_client", lambda self, api_key: Mock())
    monkeypatch.setattr(OpenAIProvider, "count_tokens", lambda self, text: max(1, len(str(text)) // 8))
    monkeypatch.setattr(OpenAIProvider, "build_pipeline_request", fake_build_pipeline_request)
    monkeypatch.setattr(OpenAIProvider, "send_request_non_streaming", fake_non_streaming)
    monkeypatch.setattr(OpenAIProvider, "calculate_cost", lambda self, parsed: 0.001)
    monkeypatch.setattr(OpenAIProvider, "calculate_cost_with_tier", lambda self, parsed, tier: 0.001)
    monkeypatch.setattr(OpenAIProvider, "format_token_string", lambda self, parsed: "I:10 O:10")
    monkeypatch.setattr(main.sync_manager, "broadcast_to_chat", _noop_broadcast)
    monkeypatch.setattr(main, "StreamingResponse", _TestStreamingResponse)
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_to_thread)


class _FakeHTTPRequest:
    async def is_disconnected(self):
        return False


class _TestStreamingResponse:
    def __init__(self, body_iterator, media_type=None, headers=None, status_code=200, **kwargs):
        self.body_iterator = body_iterator
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = status_code


async def _invoke_stream(payload: dict) -> str:
    response = await main.send_message_stream(main.SendMessageRequest(**payload), _FakeHTTPRequest())
    chunks = []
    ait = response.body_iterator
    try:
        while True:
            chunk = await asyncio.wait_for(ait.__anext__(), timeout=15)
            if isinstance(chunk, bytes):
                chunk = chunk.decode()
            chunks.append(chunk)
            seen_types = {event_type for event_type, _ in _parse_sse_events("".join(chunks))}
            if "done" in seen_types or "error" in seen_types:
                break
    except StopAsyncIteration:
        pass
    finally:
        if hasattr(ait, "aclose"):
            await ait.aclose()
    return "".join(chunks)


class TestShipCombatEndpointGPT:
    def test_strong_trigger_skips_hidden_bootstrap_and_saves_init_flags(self, ship_chat, monkeypatch):
        chat_path = ship_chat["project_dir"] / f"chat_{ship_chat['chat_name']}.json"
        data = json.loads(chat_path.read_text())
        data["pipeline_state"]["ship_combat"].update({
            "handoff_summary": "The crew went pirate hunting and caught two raiders harassing a freighter.",
            "encounter_type": "intercept",
            "objective": "protect freighter",
            "opening_narration": "Alarms strobe across the bridge as the freighter drifts between two pirate hulls.",
        })
        chat_path.write_text(json.dumps(data))

        build_calls = []
        _patch_gpt_provider(monkeypatch, [
            {
                "content": json.dumps(_mock_ship_combat_json()),
                "reasoning": None,
                "input_tokens": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 80,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            }
        ], build_calls)

        sse_text = asyncio.run(_invoke_stream({
            "username": ship_chat["username"],
            "project": ship_chat["project"],
            "chat_name": ship_chat["chat_name"],
            "message": "Take the shot.",
            "model": "gpt-5.2",
        }))
        events = _parse_sse_events(sse_text)
        done = [d for t, d in events if t == "done"][-1]
        assert done["ship_combat_mode"] is True
        assert done["ship_combat_started"] is True
        assert done["ship_combat_system_init"] is True
        assert done.get("ship_combat_opening_narration")
        assert "ship_combat_bootstrap" not in [c.get("stage_name") for c in build_calls]

        notif = [d for t, d in events if t == "state_notifications"]
        assert notif, "Expected ship NPC action state_notifications SSE"
        assert notif[-1][0]["type"] == "ship_npc_action"

        saved = json.loads(chat_path.read_text())
        user_msg = saved["messages"][-3]
        hidden_init_msg = saved["messages"][-2]
        asst_msg = saved["messages"][-1]
        assert user_msg["role"] == "user"
        assert not user_msg.get("ship_combat_system_init")
        assert hidden_init_msg.get("ship_combat_hidden_init") is True
        assert hidden_init_msg.get("ship_combat_system_init") is True
        assert asst_msg.get("parent_id") == hidden_init_msg.get("id")
        assert asst_msg.get("ship_combat_started") is True
        assert asst_msg.get("ship_combat_mode") is True
        assert asst_msg.get("ship_combat_opening_embedded") in (True, False)
        sc = saved["pipeline_state"]["ship_combat"]
        assert sc["ship_combat_handoff_source"] == "trigger"
        assert sc["bootstrap_done"] is True
        assert sc.get("bootstrap_messages"), "Expected hidden bootstrap messages persisted even on strong trigger path"

    def test_thin_trigger_runs_hidden_bootstrap_and_persists_hidden_handoff(self, ship_chat, monkeypatch):
        chat_path = ship_chat["project_dir"] / f"chat_{ship_chat['chat_name']}.json"
        data = json.loads(chat_path.read_text())
        # thin trigger: no handoff_summary / no tactical detail
        data["pipeline_state"]["ship_combat"] = _base_pipeline_state({"environment": "Nebula"})["ship_combat"]
        chat_path.write_text(json.dumps(data))

        bootstrap_json = {
            "handoff_summary": "The crew set out to hunt pirates and found two raiders pinning a freighter in the nebula haze.",
            "opening_narration": "The bridge glass blooms with amber warning glyphs as two pirate signatures bracket a battered freighter.",
            "encounter_type": "ambush",
            "objective": "protect freighter",
            "positioning": "Pirates hold crossfire lanes around the freighter",
            "immediate_complications": ["nebula interference"],
        }
        build_calls = []
        _patch_gpt_provider(monkeypatch, [
            {
                "content": json.dumps(bootstrap_json),
                "reasoning": None,
                "input_tokens": 40,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 30,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            },
            {
                "content": json.dumps(_mock_ship_combat_json()),
                "reasoning": None,
                "input_tokens": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 80,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            },
        ], build_calls)

        sse_text = asyncio.run(_invoke_stream({
            "username": ship_chat["username"],
            "project": ship_chat["project"],
            "chat_name": ship_chat["chat_name"],
            "message": "Open a channel and warn them off.",
            "model": "gpt-5.2",
        }))
        assert "event: done" in sse_text

        stage_names = [c.get("stage_name") for c in build_calls]
        assert "ship_combat_bootstrap" in stage_names
        assert stage_names.count("ship_combat") >= 2  # initial + rebuilt

        saved = json.loads(chat_path.read_text())
        sc = saved["pipeline_state"]["ship_combat"]
        assert sc["ship_combat_handoff_source"] == "bootstrap"
        assert sc["bootstrap_done"] is True
        assert sc["handoff_summary"] == bootstrap_json["handoff_summary"]
        assert sc["opening_narration"] == bootstrap_json["opening_narration"]
        assert len(sc.get("bootstrap_messages", [])) == 2
        asst_msg = saved["messages"][-1]
        hidden_init_msg = saved["messages"][-2]
        assert hidden_init_msg.get("ship_combat_hidden_init") is True
        assert len(asst_msg.get("ship_combat_bootstrap_messages", [])) == 2

    def test_ship_combat_complete_done_flag_for_surrender_style_end(self, ship_chat, monkeypatch):
        chat_path = ship_chat["project_dir"] / f"chat_{ship_chat['chat_name']}.json"
        build_calls = []
        _patch_gpt_provider(monkeypatch, [
            {
                "content": json.dumps({
                    "handoff_summary": "The crew closes on pirate raiders and forces them to break formation around a freighter.",
                    "opening_narration": "Pirate silhouettes wheel away from the freighter as your warning bursts cut across the dark.",
                }),
                "reasoning": None,
                "input_tokens": 20,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            },
            {
                "content": json.dumps(_mock_ship_combat_json(complete=True, include_npc_actions=False)),
                "reasoning": None,
                "input_tokens": 80,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 60,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            }
        ], build_calls)

        sse_text = asyncio.run(_invoke_stream({
            "username": ship_chat["username"],
            "project": ship_chat["project"],
            "chat_name": ship_chat["chat_name"],
            "message": "Accept their surrender.",
            "model": "gpt-5.2",
        }))
        events = _parse_sse_events(sse_text)
        done = [d for t, d in events if t == "done"][-1]
        assert done.get("ship_combat_complete") is True
        saved = json.loads(chat_path.read_text())
        asst_msg = saved["messages"][-1]
        assert saved["messages"][-2].get("ship_combat_hidden_init") is True
        assert len(asst_msg.get("ship_combat_bootstrap_messages", [])) == 2

    def test_ship_combat_does_not_emit_custom_boarding_resolved_state_notification(self, ship_chat, monkeypatch):
        chat_path = ship_chat["project_dir"] / f"chat_{ship_chat['chat_name']}.json"
        data = json.loads(chat_path.read_text())
        data["pipeline_state"]["ship_combat"].update({
            "handoff_summary": "Boarders punch through the pirate cutter's lock while the bridge crew keeps the freighter screened.",
            "encounter_type": "boarding",
            "objective": "capture pirate cutter",
            "opening_narration": "Mag clamps bite and the cutter's outer hatch buckles inward under shaped charges.",
        })
        chat_path.write_text(json.dumps(data))

        payload = _mock_ship_combat_json()
        payload["ship_combat"]["current_role"] = "boarding"
        payload["ship_combat"]["boarding_state"] = {
            "attacker_ship": "Warden",
            "defender_ship": "Red Knife",
            "boarding_round": 1,
            "boarding_phase": "secured",
            "contested_sections": ["bridge"],
        }

        build_calls = []
        _patch_gpt_provider(monkeypatch, [
            {
                "content": json.dumps(payload),
                "reasoning": None,
                "input_tokens": 100,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "output_tokens": 80,
                "reasoning_tokens": 0,
                "service_tier": "flex",
            }
        ], build_calls)

        sse_text = asyncio.run(_invoke_stream({
            "username": ship_chat["username"],
            "project": ship_chat["project"],
            "chat_name": ship_chat["chat_name"],
            "message": "Secure the bridge and take the cutter intact.",
            "model": "gpt-5.2",
        }))
        events = _parse_sse_events(sse_text)
        notif_batches = [d for t, d in events if t == "state_notifications"]
        assert notif_batches, "Expected state_notifications SSE"
        flattened = [n for batch in notif_batches for n in batch]
        assert any(n.get("type") == "ship_npc_action" for n in flattened)
        assert not any(n.get("type") == "boarding_resolved" for n in flattened)


class TestShipCombatHelpersAndMigration:
    def test_extract_ship_combat_notifications(self):
        payload = {
            "npc_actions": [
                {
                    "ship_name": "Crimson Fang",
                    "role": "gunner",
                    "character_name": None,
                    "action": "Fire Railgun",
                    "effect": "Hit for 12 shields",
                }
            ]
        }
        result = extract_ship_combat_notifications(payload)
        assert result == [{
            "type": "ship_npc_action",
            "ship_name": "Crimson Fang",
            "role": "gunner",
            "character_name": None,
            "action": "Fire Railgun",
            "effect": "Hit for 12 shields",
        }]

    def test_extract_ship_combat_notifications_ignores_boarding_resolution(self):
        payload = {
            "npc_actions": [],
            "ship_combat": {
                "boarding_state": {
                    "attacker_ship": "Warden",
                    "defender_ship": "Red Knife",
                    "boarding_phase": "secured",
                }
            },
        }
        assert extract_ship_combat_notifications(payload) == []

    def test_extract_ship_combat_notifications_ignores_active_boarding_phase(self):
        payload = {
            "ship_combat": {
                "boarding_state": {
                    "attacker_ship": "Warden",
                    "defender_ship": "Red Knife",
                    "boarding_phase": "fighting",
                }
            },
        }
        assert extract_ship_combat_notifications(payload) == []

    def test_collapse_ship_combat_messages_to_summary(self):
        branch = [
            {"id": "sys", "role": "system", "content": "sys"},
            {"id": "u1", "role": "user", "content": "start", "ship_combat_mode": True},
            {"id": "a1", "role": "assistant", "content": "exchange 1", "ship_combat_mode": True,
             "ship_combat_tool_input": {"narrative_summary": None}},
            {"id": "u2", "role": "user", "content": "continue", "ship_combat_mode": True},
            {"id": "a2", "role": "assistant", "content": "exchange 2", "ship_combat_mode": True,
             "ship_combat_tool_input": {"narrative_summary": "Pirates surrendered after shields collapsed."}},
            {"id": "u3", "role": "user", "content": "post-combat"},
        ]
        collapsed = collapse_ship_combat_messages(branch)
        assert collapsed[1]["content"] == "[A ship combat encounter took place.]"
        assert "[SHIP COMBAT RESULT]" in collapsed[2]["content"]
        assert "Pirates surrendered" in collapsed[2]["content"]
        assert collapsed[-1]["content"] == "post-combat"

    def test_migrate_pipeline_state_adds_ship_combat_defaults(self):
        state = migrate_pipeline_state({"pacing": {}, "character_states": {}, "turn_counter": 1})
        assert "ship_combat" in state
        assert state["ship_combat"] is None

        existing = migrate_pipeline_state({
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "game_state": {},
            "hud_state": {},
            "combat": None,
            "ship_combat": {"environment": "Debris Field"},
            "turn_counter": 2
        })
        assert existing["ship_combat"]["environment"] == "Debris Field"
        assert existing["ship_combat"]["bootstrap_done"] is False
        assert existing["ship_combat"]["bootstrap_messages"] == []

    def test_apply_ship_combat_state_preserves_handoff_metadata_and_persists_crew_manifest(self):
        pipeline_state = {
            "character_states": {"Red Knife": _ship_entry("Red Knife", hull=90, shields=40)},
            "ship_combat": {
                "round": 1,
                "initiative_order": [],
                "current_ship": None,
                "current_role": None,
                "environment": "Open Space",
                "handoff_summary": "Lead-in summary",
                "opening_narration": "Opening line",
                "bootstrap_messages": [{"role": "user", "content": "hidden"}],
                "bootstrap_done": True,
                "ship_combat_handoff_source": "bootstrap",
                "start_message_id": "m1",
                "boarding_state": {
                    "attacker_ship": "Warden",
                    "defender_ship": "Red Knife",
                    "boarding_round": 1,
                    "boarding_phase": "fighting",
                    "contested_sections": ["airlock-2"],
                },
            },
        }
        tool_input = {
            "ship_updates": [{
                "ship_name": "Red Knife",
                "shield_delta": -5,
                "crew_roles_present": ["pilot", "gunner"],
                "crew_manifest": [{"name": "Kesh", "roles": ["pilot", "gunner"]}],
            }],
            "character_updates": [],
            "ship_combat": {
                "round": 2,
                "initiative_order": [],
                "current_ship": "Red Knife",
                "current_role": "pilot",
                "environment": "Open Space",
            },
            "ship_combat_complete": False,
        }
        result = apply_ship_combat_state(copy.deepcopy(pipeline_state), tool_input)
        sc = result["ship_combat"]
        assert sc["round"] == 2
        assert sc["handoff_summary"] == "Lead-in summary"
        assert sc["opening_narration"] == "Opening line"
        assert sc["bootstrap_messages"] == [{"role": "user", "content": "hidden"}]
        assert "boarding_state" not in sc
        ship = result["character_states"]["Red Knife"]["data"]
        assert ship["crew_roles_present"] == ["pilot", "gunner"]
        assert ship["crew_manifest"][0]["name"] == "Kesh"

    def test_build_ship_combat_injection_renders_boarding_state(self):
        pipeline_state = _base_pipeline_state()
        ship_combat = copy.deepcopy(pipeline_state["ship_combat"])
        ship_combat["boarding_state"] = {
            "attacker_ship": "Warden",
            "defender_ship": "Red Knife",
            "boarding_round": 2,
            "boarding_phase": "fighting",
            "contested_sections": ["airlock-2", "corridor-b"],
            "attacker_party": [
                {"name": "Mara", "status": "active"},
                {"name": "Jex", "status": "down"},
            ],
            "defender_party": [
                {"name": "Kesh", "status": "active"},
                {"name": "Varr", "status": "surrendered"},
            ],
        }
        rendered = build_ship_combat_injection(ship_combat, pipeline_state)
        assert "--- BOARDING ACTIVE ---" in rendered
        assert "Attacker: Warden" in rendered
        assert "Defender: Red Knife" in rendered
        assert "Contested Sections: airlock-2, corridor-b" in rendered
        assert "Attackers: Mara | Jex (down)" in rendered
        assert "Defenders: Kesh | Varr (surrendered)" in rendered
        assert "--- END BOARDING ---" in rendered

    def test_build_ship_combat_injection_skips_unnamed_boarding_entries(self):
        pipeline_state = _base_pipeline_state()
        ship_combat = copy.deepcopy(pipeline_state["ship_combat"])
        ship_combat["boarding_state"] = {
            "attacker_ship": "Warden",
            "defender_ship": "Red Knife",
            "boarding_round": 1,
            "boarding_phase": "fighting",
            "attacker_party": [
                {"status": "active"},
                {"name": "Mara", "status": "active"},
            ],
            "defender_party": [
                {"status": "down"},
            ],
        }
        rendered = build_ship_combat_injection(ship_combat, pipeline_state)
        assert "Attackers: Mara" in rendered
        assert "Defenders:" not in rendered

    def test_ship_combat_schema_supports_boarding_fields(self):
        props = REPORT_SHIP_COMBAT_STATE_TOOL["input_schema"]["properties"]
        ship_combat_required = props["ship_combat"]["required"]
        ship_combat_props = props["ship_combat"]["properties"]
        assert "boarding_state" in ship_combat_required
        assert "boarding" in ship_combat_props["current_role"]["enum"]
        assert "boarding_state" in ship_combat_props
        attacker_item_required = ship_combat_props["boarding_state"]["properties"]["attacker_party"]["items"]["required"]
        defender_item_required = ship_combat_props["boarding_state"]["properties"]["defender_party"]["items"]["required"]
        assert "name" in attacker_item_required and "status" in attacker_item_required
        assert "name" in defender_item_required and "status" in defender_item_required
        npc_role_enum = props["npc_actions"]["items"]["properties"]["role"]["enum"]
        assert "boarding" in npc_role_enum
        status_enum = props["combat_outcome"]["properties"]["ship_final_states"]["items"]["properties"]["status"]["enum"]
        assert "captured" in status_enum
        boarding_outcome_props = props["combat_outcome"]["properties"]["boarding_outcome"]["properties"]
        assert "captured" in boarding_outcome_props["result"]["enum"]

    def test_apply_ship_combat_state_initializes_missing_ship_entries(self):
        pipeline_state = {"character_states": {}, "ship_combat": {"round": 1}}
        tool_input = {
            "ship_updates": [{
                "ship_name": "New Raider",
                "shield_delta": -8,
                "conditions_add": ["Weapons: Damaged"],
                "crew_roles_present": ["pilot"],
                "crew_manifest": [{"name": "Ace", "roles": ["pilot"]}],
            }],
            "character_updates": [],
            "ship_combat": {"round": 1, "initiative_order": [], "current_ship": "New Raider", "environment": "Open Space"},
            "ship_combat_complete": False,
        }
        result = apply_ship_combat_state(copy.deepcopy(pipeline_state), tool_input)
        assert "New Raider" in result["character_states"]
        ship = result["character_states"]["New Raider"]["data"]
        assert ship["type"] == "ship"
        assert ship["crew_roles_present"] == ["pilot"]
        assert ship["crew_manifest"][0]["name"] == "Ace"
        assert "Weapons: Damaged" in ship["conditions"]
        vitals = {v["label"]: v for v in ship["vitals"]}
        assert vitals["Shields"]["max"] > 0

    def test_debug_transcript_prints_hidden_ship_bootstrap_messages(self, tmp_path):
        chat_path = tmp_path / "chat_debugtest.json"
        chat_data = {
            "messages": [
                {"id": "sys", "role": "system", "content": "sys"},
                {"id": "u1", "parent_id": "sys", "role": "user", "content": "Go"},
                {
                    "id": "a1",
                    "parent_id": "u1",
                    "role": "assistant",
                    "content": "Combat begins.",
                    "timestamp": "2026-01-01T00:00:00",
                    "cost": "$0.001",
                    "ship_combat_bootstrap_messages": [
                        {"role": "user", "content": "hidden bootstrap prompt", "ship_combat_bootstrap_hidden": True},
                        {"role": "assistant", "content": "hidden bootstrap output", "ship_combat_bootstrap_hidden": True},
                    ],
                },
            ],
            "current_leaf_id": "a1",
        }
        chat_path.write_text(json.dumps(chat_data))
        generate_debug_transcript(chat_data, str(chat_path), "debugtest")
        debug_txt = chat_path.with_name("chat_debugtest_debug.txt").read_text()
        assert "HIDDEN SHIP COMBAT BOOTSTRAP MESSAGES" in debug_txt
        assert "hidden bootstrap prompt" in debug_txt
        assert "hidden bootstrap output" in debug_txt

    def test_contract_mentions_surrender_and_abandon_ship_end_conditions(self):
        content = Path(Path(__file__).parent.parent / "game_systems" / "dnd5e_cyber.py").read_text()
        assert "All enemies surrender" in content
        assert "player crew abandons ship" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
