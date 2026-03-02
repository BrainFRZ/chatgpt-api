"""
Comprehensive tests for the right-side character panel feature.

Covers:
- Pipeline state migration (combat field, character_states format migration)
- apply_character_states() with structured and legacy formats
- _format_character_data() for model injection
- build_character_states_injection() with new data format
- Combat persistence in pipeline and single-agent paths
- Game system schema validation (all 5 systems)
- ChatResponse model fields
- Character sheet endpoint
- State update SSE event format
"""

import sys
import os
import json
import copy
import importlib.util
import pytest

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pipeline import (
    _fresh_pipeline_state,
    migrate_pipeline_state,
    apply_character_states,
    _format_character_data,
    build_character_states_injection,
    apply_single_agent_state_updates,
    build_single_agent_injections,
    CHARACTER_STATE_TTL,
)


# ============================================================
# _fresh_pipeline_state
# ============================================================

class TestFreshPipelineState:
    def test_contains_combat_key(self):
        state = _fresh_pipeline_state()
        assert "combat" in state
        assert state["combat"] is None

    def test_contains_all_required_keys(self):
        state = _fresh_pipeline_state()
        expected_keys = {
            "pacing", "callback_ledger", "npc_memories", "scene_state",
            "character_states", "game_state", "hud_state", "combat", "ship_combat", "turn_counter"
        }
        assert set(state.keys()) == expected_keys

    def test_character_states_starts_empty(self):
        state = _fresh_pipeline_state()
        assert state["character_states"] == {}

    def test_turn_counter_starts_at_zero(self):
        state = _fresh_pipeline_state()
        assert state["turn_counter"] == 0


# ============================================================
# migrate_pipeline_state
# ============================================================

class TestMigratePipelineState:
    def test_none_returns_fresh(self):
        state = migrate_pipeline_state(None)
        assert state["combat"] is None
        assert state["character_states"] == {}
        assert state["turn_counter"] == 0

    def test_old_flat_format_gets_combat(self):
        """Old format: state IS the pacing dict (no 'pacing' key). Gets combat and game_state."""
        old_state = {"episode": "pilot", "beat": "intro"}
        result = migrate_pipeline_state(old_state)
        assert result["combat"] is None
        assert result["game_state"] == {}
        assert result["pacing"] == old_state

    def test_new_format_gets_combat_defaulted(self):
        """New format: has 'pacing' key. combat should be defaulted."""
        state = {"pacing": {"episode": "1"}, "character_states": {}, "turn_counter": 5}
        result = migrate_pipeline_state(state)
        assert result["combat"] is None

    def test_existing_combat_preserved(self):
        state = {
            "pacing": {},
            "combat": {"round": 3, "initiative_order": ["Alice", "Bob"], "current_turn": "Alice"},
            "character_states": {},
            "turn_counter": 10,
        }
        result = migrate_pipeline_state(state)
        assert result["combat"]["round"] == 3
        assert result["combat"]["current_turn"] == "Alice"

    def test_migrate_old_state_key_to_data_key(self):
        """Old format: {"state": "HP: 12/14", "last_updated": 5} → {"data": {"summary": "HP: 12/14"}, ...}"""
        state = {
            "pacing": {},
            "character_states": {
                "Aldric": {"state": "HP: 12/14, AC: 16", "last_updated": 5}
            },
            "turn_counter": 5,
        }
        result = migrate_pipeline_state(state)
        char = result["character_states"]["Aldric"]
        assert "data" in char
        assert "state" not in char
        assert char["data"]["summary"] == "HP: 12/14, AC: 16"
        assert char["last_updated"] == 5

    def test_migrate_bare_string_character_state(self):
        """Bare string (no dict wrapper) → wrapped."""
        state = {
            "pacing": {},
            "character_states": {
                "Aldric": "HP 10/14"
            },
            "turn_counter": 3,
        }
        result = migrate_pipeline_state(state)
        char = result["character_states"]["Aldric"]
        assert char["data"]["summary"] == "HP 10/14"
        assert char["last_updated"] == 3

    def test_migrate_already_new_format(self):
        """Already in new format: {"data": {...}, "last_updated": N} — untouched."""
        state = {
            "pacing": {},
            "character_states": {
                "Aldric": {
                    "data": {"type": "pc", "vitals": [{"label": "HP", "current": 12, "max": 14}]},
                    "last_updated": 7,
                }
            },
            "turn_counter": 7,
        }
        result = migrate_pipeline_state(state)
        char = result["character_states"]["Aldric"]
        assert char["data"]["type"] == "pc"
        assert char["data"]["vitals"][0]["current"] == 12

    def test_migrate_dict_without_data_or_state_key(self):
        """Dict without 'data' or 'state' key → treat entire dict as data."""
        state = {
            "pacing": {},
            "character_states": {
                "Aldric": {"type": "pc", "vitals": [{"label": "HP", "current": 10, "max": 14}]}
            },
            "turn_counter": 4,
        }
        result = migrate_pipeline_state(state)
        char = result["character_states"]["Aldric"]
        assert char["data"]["type"] == "pc"
        assert char["last_updated"] == 4

    def test_migrate_old_state_key_with_dict_value(self):
        """Old {"state": {dict_value}, "last_updated": N} → {"data": dict_value, ...}"""
        state = {
            "pacing": {},
            "character_states": {
                "Aldric": {
                    "state": {"type": "pc", "summary": "armed"},
                    "last_updated": 6
                }
            },
            "turn_counter": 6,
        }
        result = migrate_pipeline_state(state)
        char = result["character_states"]["Aldric"]
        assert char["data"]["type"] == "pc"
        assert char["data"]["summary"] == "armed"
        assert "state" not in char


# ============================================================
# apply_character_states
# ============================================================

class TestApplyCharacterStates:
    def test_structured_dict_format(self):
        """New structured format: dict with vitals/resources/etc."""
        existing = {}
        mechanics_output = {
            "Aldric": {
                "type": "pc",
                "vitals": [{"label": "HP", "current": 12, "max": 14}],
                "conditions": ["Poisoned"],
                "summary": "Longsword equipped"
            }
        }
        result = apply_character_states(existing, mechanics_output, current_turn=1)
        assert "Aldric" in result
        assert result["Aldric"]["data"]["type"] == "pc"
        assert result["Aldric"]["data"]["vitals"][0]["current"] == 12
        assert result["Aldric"]["data"]["conditions"] == ["Poisoned"]
        assert result["Aldric"]["last_updated"] == 1

    def test_old_string_format_wrapped(self):
        """Old string format: wrapped into {"data": {"summary": str}}."""
        existing = {}
        mechanics_output = {"Aldric": "HP: 12/14, AC: 16, Poisoned"}
        result = apply_character_states(existing, mechanics_output, current_turn=2)
        assert result["Aldric"]["data"]["summary"] == "HP: 12/14, AC: 16, Poisoned"
        assert result["Aldric"]["last_updated"] == 2

    def test_merge_preserves_existing(self):
        """New entries merge with existing; existing entries not in output preserved."""
        existing = {
            "Aldric": {"data": {"type": "pc", "summary": "old"}, "last_updated": 1}
        }
        mechanics_output = {
            "Kira": {"type": "npc", "summary": "new NPC"}
        }
        result = apply_character_states(existing, mechanics_output, current_turn=2)
        assert "Aldric" in result  # preserved
        assert "Kira" in result  # added

    def test_update_overwrites_same_name(self):
        """Updating same character replaces data."""
        existing = {
            "Aldric": {"data": {"summary": "old"}, "last_updated": 1}
        }
        mechanics_output = {
            "Aldric": {"type": "pc", "vitals": [{"label": "HP", "current": 10, "max": 14}]}
        }
        result = apply_character_states(existing, mechanics_output, current_turn=2)
        assert result["Aldric"]["data"]["vitals"][0]["current"] == 10
        assert result["Aldric"]["last_updated"] == 2

    def test_prune_stale_entries(self):
        """Entries older than CHARACTER_STATE_TTL turns are pruned."""
        stale_turn = 1
        current_turn = stale_turn + CHARACTER_STATE_TTL + 1
        existing = {
            "Aldric": {"data": {"summary": "fresh"}, "last_updated": current_turn},
            "OldNPC": {"data": {"summary": "stale"}, "last_updated": stale_turn},
        }
        result = apply_character_states(existing, {}, current_turn=current_turn)
        assert "Aldric" in result
        assert "OldNPC" not in result

    def test_empty_mechanics_output(self):
        """Empty mechanics output doesn't crash, existing preserved."""
        existing = {
            "Aldric": {"data": {"summary": "fine"}, "last_updated": 5}
        }
        result = apply_character_states(existing, {}, current_turn=5)
        assert "Aldric" in result


# ============================================================
# _format_character_data
# ============================================================

class TestFormatCharacterData:
    def test_string_passthrough(self):
        assert _format_character_data("HP: 12/14") == "HP: 12/14"

    def test_vitals_with_current_max(self):
        data = {"vitals": [{"label": "HP", "current": 12, "max": 14}]}
        result = _format_character_data(data)
        assert "HP: 12/14" in result

    def test_vitals_with_value(self):
        data = {"vitals": [{"label": "AC", "value": 16}]}
        result = _format_character_data(data)
        assert "AC: 16" in result

    def test_resources(self):
        data = {"resources": [{"label": "Spell Slots", "current": 2, "max": 3}]}
        result = _format_character_data(data)
        assert "Spell Slots: 2/3" in result

    def test_conditions(self):
        data = {"conditions": ["Poisoned", "Exhausted"]}
        result = _format_character_data(data)
        assert "Conditions: Poisoned, Exhausted" in result

    def test_summary(self):
        data = {"summary": "Longsword equipped"}
        result = _format_character_data(data)
        assert "Longsword equipped" in result

    def test_full_structured(self):
        data = {
            "type": "pc",
            "vitals": [
                {"label": "HP", "current": 12, "max": 14},
                {"label": "AC", "value": 16}
            ],
            "resources": [{"label": "Slots (1st)", "current": 2, "max": 3}],
            "conditions": ["Poisoned"],
            "summary": "Longsword equipped"
        }
        result = _format_character_data(data)
        assert "HP: 12/14" in result
        assert "AC: 16" in result
        assert "Slots (1st): 2/3" in result
        assert "Poisoned" in result
        assert "Longsword equipped" in result

    def test_empty_dict_falls_back(self):
        result = _format_character_data({})
        # Should fall back to json.dumps for empty dict
        assert result == "{}"

    def test_non_dict_non_string(self):
        result = _format_character_data(42)
        assert result == "42"


# ============================================================
# build_character_states_injection
# ============================================================

class TestBuildCharacterStatesInjection:
    def test_empty_returns_empty(self):
        result = build_character_states_injection({})
        assert "[CHARACTER STATES]" in result
        assert "(empty" in result
        assert "[/CHARACTER STATES]" in result

    def test_new_data_format(self):
        cs = {
            "Aldric": {
                "data": {"type": "pc", "vitals": [{"label": "HP", "current": 12, "max": 14}]},
                "last_updated": 5,
            }
        }
        result = build_character_states_injection(cs)
        assert "[CHARACTER STATES]" in result
        assert "[/CHARACTER STATES]" in result
        assert "Aldric:" in result
        assert "HP: 12/14" in result

    def test_old_state_format(self):
        cs = {
            "Aldric": {"state": "HP: 10/14, poisoned", "last_updated": 3}
        }
        result = build_character_states_injection(cs)
        assert "Aldric:" in result
        assert "HP: 10/14, poisoned" in result

    def test_bare_string_format(self):
        cs = {"Aldric": "HP: 8/14"}
        result = build_character_states_injection(cs)
        assert "Aldric: HP: 8/14" in result

    def test_multiple_characters(self):
        cs = {
            "Aldric": {"data": {"summary": "healthy"}, "last_updated": 5},
            "Kira": {"data": {"summary": "wounded"}, "last_updated": 5},
        }
        result = build_character_states_injection(cs)
        assert "Aldric: healthy" in result
        assert "Kira: wounded" in result


# ============================================================
# apply_single_agent_state_updates — combat persistence
# ============================================================

class TestSingleAgentCombatPersistence:
    def _make_state(self):
        return _fresh_pipeline_state()

    def test_combat_persisted_from_parsed(self):
        state = self._make_state()
        parsed = {
            "combat": {"round": 1, "initiative_order": ["Alice", "Bob"], "current_turn": "Alice"},
            "character_states": {},
        }
        apply_single_agent_state_updates(state, parsed, current_turn=1)
        assert state["combat"]["round"] == 1
        assert state["combat"]["current_turn"] == "Alice"

    def test_combat_null_clears_combat(self):
        state = self._make_state()
        state["combat"] = {"round": 2, "initiative_order": [], "current_turn": "Alice"}
        parsed = {"combat": None, "character_states": {}}
        apply_single_agent_state_updates(state, parsed, current_turn=2)
        assert state["combat"] is None

    def test_no_combat_key_doesnt_clear(self):
        state = self._make_state()
        state["combat"] = {"round": 1}
        parsed = {"character_states": {}}  # No combat key at all
        apply_single_agent_state_updates(state, parsed, current_turn=2)
        assert state["combat"] == {"round": 1}  # Preserved

    def test_character_states_structured_through_single_agent(self):
        state = self._make_state()
        parsed = {
            "character_states": {
                "Aldric": {
                    "type": "pc",
                    "vitals": [{"label": "HP", "current": 12, "max": 14}],
                    "conditions": [],
                    "summary": "Healthy"
                }
            }
        }
        apply_single_agent_state_updates(state, parsed, current_turn=1)
        assert state["character_states"]["Aldric"]["data"]["type"] == "pc"
        assert state["character_states"]["Aldric"]["last_updated"] == 1

    def test_hud_state_persisted(self):
        state = self._make_state()
        parsed = {
            "hud_state": {"date": "Day 3", "time": "1400", "location": "Tavern"},
            "character_states": {},
        }
        apply_single_agent_state_updates(state, parsed, current_turn=1)
        assert state["hud_state"]["location"] == "Tavern"


# ============================================================
# build_single_agent_injections — combat included
# ============================================================

class TestBuildSingleAgentInjections:
    def test_includes_character_states(self):
        state = _fresh_pipeline_state()
        state["character_states"] = {
            "Aldric": {"data": {"summary": "armed"}, "last_updated": 1}
        }
        result = build_single_agent_injections(state)
        assert "[CHARACTER STATES]" in result
        assert "Aldric" in result

    def test_includes_scene_state(self):
        state = _fresh_pipeline_state()
        state["scene_state"] = {"location": "Tavern", "npcs_present": ["Bard"]}
        result = build_single_agent_injections(state)
        assert "[SCENE STATE]" in result
        assert "Tavern" in result

    def test_empty_state_returns_empty_or_minimal(self):
        state = _fresh_pipeline_state()
        result = build_single_agent_injections(state)
        # Fresh state injects bootstrap markers for scene and character states
        assert "[CHARACTER STATES]" in result
        assert "[SCENE STATE]" in result
        assert "(empty" in result


# ============================================================
# Game System Schema Validation (all 5 systems)
# ============================================================

class TestGameSystemSchemas:
    """Validate that all 5 game systems have the required schema updates."""

    @pytest.fixture(autouse=True)
    def setup_game_systems(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from game_systems import get_game_system
        self.get_game_system = get_game_system

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_combat_in_state_report_tool(self, system_id):
        gs = self.get_game_system(system_id)
        props = gs["state_report_tool"]["input_schema"]["properties"]
        assert "combat" in props, f"{system_id}: missing combat in STATE_REPORT_TOOL"
        combat = props["combat"]
        assert combat["type"] == ["object", "null"], f"{system_id}: combat type should be [object, null]"
        assert "initiative" in combat["description"].lower(), f"{system_id}: combat description should mention initiative"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_character_states_additional_properties(self, system_id):
        gs = self.get_game_system(system_id)
        props = gs["state_report_tool"]["input_schema"]["properties"]
        cs = props["character_states"]
        assert cs["type"] == "object", f"{system_id}: character_states should be type object"
        assert "additionalProperties" in cs, f"{system_id}: character_states should define additionalProperties"
        assert isinstance(cs["additionalProperties"], (bool, dict)), f"{system_id}: additionalProperties should be bool or object schema"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_character_states_description_mentions_structured(self, system_id):
        gs = self.get_game_system(system_id)
        props = gs["state_report_tool"]["input_schema"]["properties"]
        desc = props["character_states"]["description"].lower()
        assert "vitals" in desc or "structured" in desc, \
            f"{system_id}: character_states description should mention structured format"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_required_fields_present(self, system_id):
        gs = self.get_game_system(system_id)
        required = gs["state_report_tool"]["input_schema"]["required"]
        assert "is_ooc" in required
        assert "pacing" in required
        assert "scene_state" in required
        assert "character_states" in required

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_single_agent_contract_mentions_combat(self, system_id):
        gs = self.get_game_system(system_id)
        contract = gs["single_agent_contract"].lower()
        assert "combat" in contract, f"{system_id}: single_agent_contract should mention combat"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_single_agent_contract_mentions_structured_format(self, system_id):
        gs = self.get_game_system(system_id)
        contract = gs["single_agent_contract"].lower()
        assert "vitals" in contract or "structured" in contract, \
            f"{system_id}: single_agent_contract should describe structured character_states format"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_events_contract_mentions_structured_format(self, system_id):
        gs = self.get_game_system(system_id)
        contract = gs["events_contract"].lower()
        assert "vitals" in contract, \
            f"{system_id}: events_contract should describe structured character_states format"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_mechanics_contract_mentions_structured_format(self, system_id):
        gs = self.get_game_system(system_id)
        contract = gs["mechanics_contract"].lower()
        assert "vitals" in contract, \
            f"{system_id}: mechanics_contract should describe structured character_states format"

    @pytest.mark.parametrize("system_id", ["dnd5e", "coc7e", "sr6e", "cpred", "dnd5e_cyber"])
    def test_game_system_has_all_keys(self, system_id):
        gs = self.get_game_system(system_id)
        required_keys = {
            "id", "display_name", "events_contract", "mechanics_contract",
            "narration_contract", "single_agent_contract", "state_report_tool",
        }
        for key in required_keys:
            assert key in gs, f"{system_id}: missing key {key}"


# ============================================================
# ChatResponse model
# ============================================================

class TestChatResponseModel:
    def test_pipeline_state_field_exists(self):
        """ChatResponse should accept pipeline_state and game_system fields."""
        # Import the model
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        # We can't easily import main.py due to FastAPI dependencies,
        # so let's just verify the fields exist by reading the file
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        assert 'pipeline_state: dict | None = None' in content
        assert 'game_system: str | None = None' in content

    def test_get_chat_populates_fields(self):
        """get_chat should include pipeline_state and game_system in response."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        assert 'pipeline_state=data.get("pipeline_state")' in content
        assert 'game_system=chat_game_system' in content


# ============================================================
# SSE state_update event
# ============================================================

class TestStateUpdateSSE:
    def test_pipeline_path_emits_state_update(self):
        """Pipeline path should emit state_update SSE event."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        # Check that state_update is yielded after pipeline state save
        assert 'event: state_update' in content

    def test_single_agent_path_emits_state_update(self):
        """Single-agent path should emit state_update SSE event."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        # Count occurrences — should be at least 2 (pipeline + single-agent)
        count = content.count('event: state_update')
        assert count >= 2, f"Expected at least 2 state_update SSE events, found {count}"

    def test_websocket_state_update_broadcast(self):
        """State update should be broadcast via WebSocket."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        assert 'SyncEventType.STATE_UPDATE' in content


# ============================================================
# SyncEventType.STATE_UPDATE
# ============================================================

class TestSyncManagerStateUpdate:
    def test_state_update_enum_exists(self):
        from sync_manager import SyncEventType
        assert hasattr(SyncEventType, 'STATE_UPDATE')
        assert SyncEventType.STATE_UPDATE.value == 'state_update'


# ============================================================
# Character sheet endpoint
# ============================================================

class TestCharacterSheetEndpoint:
    def test_endpoint_code_exists(self):
        """GET /api/character-sheet/{username}/{project} should exist."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        assert '/api/character-sheet/' in content
        assert 'def get_character_sheet' in content

    def test_endpoint_searches_character_files(self):
        """Endpoint should search for files matching *character*."""
        main_path = os.path.join(os.path.dirname(__file__), '..', 'main.py')
        with open(main_path, 'r') as f:
            content = f.read()
        assert 'character' in content.lower()
        assert 'fnmatch' in content


# ============================================================
# Frontend integration checks (file-level)
# ============================================================

class TestFrontendIntegration:
    @pytest.fixture(autouse=True)
    def setup_frontend_path(self):
        src_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'frontend', 'src')
        parts = []
        for rel in ['App.tsx', 'components/CharacterPanel.tsx', 'hooks/useMessaging.ts', 'hooks/useSync.ts']:
            path = os.path.join(src_dir, rel)
            if os.path.exists(path):
                with open(path, 'r') as f:
                    parts.append(f.read())
        self.content = '\n'.join(parts)

    def test_state_variables_exist(self):
        assert 'rightPanelOpen' in self.content
        assert 'pipelineState' in self.content
        assert 'chatGameSystem' in self.content
        assert 'selectedCharacter' in self.content
        assert 'showCharacterSheet' in self.content
        assert 'showAllCharactersModal' in self.content
        assert 'showNpcMemories' in self.content
        assert 'characterSheetFiles' in self.content
        assert 'mobileBottomSheetOpen' in self.content

    def test_keyboard_shortcut_ctrl_bracket(self):
        assert "e.key === ']'" in self.content
        assert 'setRightPanelOpen' in self.content

    def test_sse_state_update_handler(self):
        assert "event.type === 'state_update'" in self.content
        assert 'setPipelineState(event.data)' in self.content

    def test_websocket_state_update_handler(self):
        assert "case 'state_update':" in self.content
        assert 'event.data?.pipeline_state' in self.content

    def test_pipeline_state_loaded_on_chat_open(self):
        assert 'setPipelineState(data.pipeline_state' in self.content
        assert 'setChatGameSystem(data.game_system' in self.content

    def test_pipeline_state_cleared_on_chat_switch(self):
        assert 'setPipelineState(null)' in self.content
        assert 'setChatGameSystem(null)' in self.content

    def test_character_sheet_fetched_on_project_enter(self):
        assert '/api/character-sheet/' in self.content
        assert 'setCharacterSheetFiles' in self.content

    def test_right_panel_collapsed_strip(self):
        """Collapsed strip should have « button and character count badge."""
        # Check for the expand/collapse button characters (JS unicode escapes)
        assert '\\u00AB' in self.content
        assert '\\u00BB' in self.content

    def test_right_panel_expanded(self):
        """Expanded panel should have 280px width."""
        assert "width: '280px'" in self.content

    def test_combat_mode_rendering(self):
        assert 'Combat \\u2014 Round' in self.content
        assert 'COMBAT' in self.content
        assert 'initiative_order' in self.content
        assert 'current_turn' in self.content
        assert 'ACTING' in self.content

    def test_character_sheet_modal(self):
        assert 'Character Sheet Modal' in self.content
        assert 'Live State' in self.content
        assert 'Full Character Sheet' in self.content

    def test_all_characters_modal(self):
        assert 'All Characters Modal' in self.content
        assert 'In Scene' in self.content
        assert 'Other Characters' in self.content

    def test_npc_memories_modal(self):
        assert 'NPC Memories Modal' in self.content
        assert '\\u2605' in self.content  # filled star
        assert '\\u2606' in self.content  # empty star

    def test_game_specific_state_sections(self):
        """Should render state for all 5 game systems."""
        assert "gs === 'dnd5e'" in self.content or "gs === 'dnd5e' ||" in self.content
        assert "gs === 'coc7e'" in self.content
        assert "gs === 'sr6e'" in self.content
        assert "gs === 'cpred'" in self.content
        assert "gs === 'dnd5e_cyber'" in self.content

    def test_vital_bar_colors(self):
        """HP bar should gradient green→yellow→red."""
        assert '#4ade80' in self.content  # green
        assert '#fbbf24' in self.content  # yellow
        assert '#ef4444' in self.content  # red

    def test_condition_pill_colors(self):
        """Condition pills should have keyword-based coloring."""
        assert 'poison' in self.content.lower()
        assert 'exhaust' in self.content.lower()
        assert 'bless' in self.content.lower()

    def test_type_badge_colors(self):
        assert '#3b82f6' in self.content  # PC blue
        assert '#f59e0b' in self.content  # NPC amber
        assert '#94a3b8' in self.content  # Ship steel

    def test_pulse_animation_defined(self):
        assert '@keyframes pulse' in self.content

    def test_chat_game_system_syncs_on_dropdown_change(self):
        assert 'setChatGameSystem(newGameSystem)' in self.content
        assert 'setChatGameSystem(previousGameSystem)' in self.content

    def test_mobile_bottom_sheet_has_cards(self):
        """Mobile bottom sheet should render character cards when expanded."""
        # Check for the drag handle element
        assert "width: '32px', height: '3px'" in self.content
        # Check for card rendering in mobile
        assert 'mobileBottomSheetOpen' in self.content
        # Net combat mobile path should use initiative order/current turn, not scene fallback.
        assert 'isNetCombatM ?' in self.content
        assert 'state.combat.initiative_order' in self.content
        assert 'state.combat.current_turn' in self.content

    def test_funds_display(self):
        """Character sheet should display funds from hud_state."""
        assert 'hudFunds' in self.content
        assert 'hud_state' in self.content

    def test_memory_tier_colors(self):
        """NPC memories should have tier-based border colors."""
        assert '#fbbf24' in self.content  # gold/high
        assert '#3b82f6' in self.content  # blue/moderate
        assert '#4a4a6e' in self.content  # gray/flavor


# ============================================================
# Edge cases and regression tests
# ============================================================

class TestEdgeCases:
    def test_apply_character_states_with_mixed_formats(self):
        """Handle a mix of structured and string entries in one call."""
        existing = {}
        mechanics_output = {
            "Aldric": {"type": "pc", "vitals": [{"label": "HP", "current": 12, "max": 14}]},
            "OldNPC": "HP: 5/10, scared"
        }
        result = apply_character_states(existing, mechanics_output, current_turn=1)
        assert result["Aldric"]["data"]["type"] == "pc"
        assert result["OldNPC"]["data"]["summary"] == "HP: 5/10, scared"

    def test_format_empty_vitals(self):
        """Structured data with empty arrays still serializes (type key is meaningful)."""
        data = {"type": "npc", "vitals": [], "resources": [], "conditions": [], "summary": ""}
        result = _format_character_data(data)
        # Function JSON-serializes dicts that don't produce any formatted parts
        assert isinstance(result, str) and len(result) > 0

    def test_format_only_conditions(self):
        data = {"conditions": ["Stunned"]}
        result = _format_character_data(data)
        assert "Conditions: Stunned" in result

    def test_migration_idempotent(self):
        """Running migrate twice should not corrupt data."""
        state = {
            "pacing": {"episode": "1"},
            "character_states": {
                "Aldric": {"state": "HP 10/14", "last_updated": 3}
            },
            "turn_counter": 5,
        }
        result1 = migrate_pipeline_state(state)
        result2 = migrate_pipeline_state(copy.deepcopy(result1))
        assert result1["character_states"]["Aldric"]["data"] == result2["character_states"]["Aldric"]["data"]

    def test_apply_character_states_none_values(self):
        """None value in mechanics output should be handled."""
        existing = {}
        mechanics_output = {"Aldric": None}
        # None is not a dict or string — should be treated as string
        result = apply_character_states(existing, mechanics_output, current_turn=1)
        assert result["Aldric"]["data"]["summary"] == "None"

    def test_format_nested_data_without_known_keys(self):
        """Structured data without standard keys should fall back to JSON."""
        data = {"custom_field": "value", "other": 42}
        result = _format_character_data(data)
        assert "custom_field" in result  # JSON serialized

    def test_combat_field_in_fresh_state(self):
        state = _fresh_pipeline_state()
        assert "combat" in state
        assert state["combat"] is None

    def test_build_injection_with_data_and_state_mixed(self):
        """Injection builder handles mix of old and new formats in same state."""
        cs = {
            "Aldric": {"data": {"vitals": [{"label": "HP", "current": 12, "max": 14}]}, "last_updated": 5},
            "OldChar": {"state": "HP: 5/10", "last_updated": 4},
        }
        result = build_character_states_injection(cs)
        assert "Aldric:" in result
        assert "HP: 12/14" in result
        assert "OldChar:" in result
        assert "HP: 5/10" in result

    def test_prune_non_dict_entries(self):
        """Prune logic should not crash on non-dict entries (treats them as stale)."""
        existing = {
            "Valid": {"data": {"summary": "OK"}, "last_updated": 10},
            "Broken": "bare string somehow",
        }
        result = apply_character_states(existing, {}, current_turn=10)
        # Non-dict entry should be pruned
        assert "Broken" not in result
        assert "Valid" in result

    def test_old_format_migration_has_game_state(self):
        """Old flat format migration must include game_state and combat."""
        old_state = {"episode": "pilot", "beat": "intro"}
        result = migrate_pipeline_state(old_state)
        assert "game_state" in result
        assert result["game_state"] == {}
        assert "combat" in result
        assert result["combat"] is None

    def test_compute_state_delta_new_data_format(self):
        """_compute_state_delta should read from 'data' key, not 'state' key."""
        from pipeline import _compute_state_delta
        prev = {
            "character_states": {},
            "pacing": {}, "callback_ledger": {"open": [], "recently_resolved": []},
            "npc_memories": {}, "scene_state": {}, "hud_state": {},
        }
        curr = {
            "character_states": {
                "Aldric": {"data": {"vitals": [{"label": "HP", "current": 12, "max": 14}]}, "last_updated": 1}
            },
            "pacing": {}, "callback_ledger": {"open": [], "recently_resolved": []},
            "npc_memories": {}, "scene_state": {}, "hud_state": {},
        }
        result = _compute_state_delta(prev, curr)
        assert "character_state +Aldric" in result
        # Should contain actual data, not empty string
        assert "vitals" in result or "HP" in result
