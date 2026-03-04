"""
Tests for pipeline stateful Events features:
- Pipeline state migration
- Callback ops application
- NPC memory ops application
- Scene state application
- Injection builders
- build_events_messages integration
- run_pipeline end-to-end with mocked API calls
- Debug transcript generation
"""

import json
import copy
import random
import sys
import os
import logging
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass
from typing import Optional, Iterator

import pytest

# Ensure backend is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import (
    migrate_pipeline_state,
    _fresh_pipeline_state,
    apply_callback_ops,
    apply_npc_memory_ops,
    apply_scene_state,
    _memory_tier,
    build_callback_injection,
    build_npc_memories_injection,
    build_scene_state_injection,
    build_character_states_injection,
    apply_character_states,
    scope_hud_funds,
    derive_funds_from_ship_credits,
    build_hud_state_injection,
    apply_single_agent_state_updates,
    build_events_messages,
    build_mechanics_messages,
    build_narration_messages,
    build_agent_system_prompt,
    get_context_pairs,
    run_pipeline,
    generate_debug_transcript,
    PipelineResult,
    PipelineStageResult,
    EVENTS_THRESHOLD_PAIRS,
    EVENTS_TARGET_PAIRS,
    NARRATION_THRESHOLD_PAIRS,
    NARRATION_TARGET_PAIRS,
    CALLBACK_RESOLVED_RETENTION,
    NPC_MEMORY_TIER_LIMITS,
    NPC_MEMORY_MAX_PER_NPC,
    CHARACTER_STATE_TTL,
)
from game_systems.coc7e import init_game_state as coc_init, apply_game_state as coc_apply
from game_systems.cpred import (
    init_game_state as cpred_init,
    apply_game_state as cpred_apply,
    build_game_injection as cpred_build_injection,
    apply_net_combat_state as cpred_apply_net_combat_state,
    init_net_combat_from_hack as cpred_init_net_combat_from_hack,
)
from game_systems.dnd5e import init_game_state as dnd_init, apply_game_state as dnd_apply
from game_systems.dnd5e_cyber import init_game_state as cyber_init, apply_game_state as cyber_apply
from game_systems.sr6e import init_game_state as sr_init, apply_game_state as sr_apply
from game_systems import get_game_system
from pipeline import collapse_net_combat_messages


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fresh_state():
    return _fresh_pipeline_state()


@pytest.fixture
def empty_ledger():
    return {"next_id": 1, "open": [], "recently_resolved": []}


@pytest.fixture
def populated_ledger():
    return {
        "next_id": 4,
        "open": [
            {"id": 1, "created_turn": 5, "original_text": "Kira promised to show the tunnels", "source_npc": "Kira"},
            {"id": 2, "created_turn": 8, "original_text": "Vex said he'd get the parts", "source_npc": "Vex"},
            {"id": 3, "created_turn": 12, "original_text": "Signal from abandoned sector", "source_npc": None},
        ],
        "recently_resolved": []
    }


@pytest.fixture
def populated_memories():
    return {
        "Kira": [
            {"text": "Saved Kira from warehouse explosion", "quote": "I don't forget debts.", "date": "Day 3", "impact": 5, "tier": "high", "turn_created": 3},
            {"text": "Laughed at Orrophim's pun", "quote": None, "date": "Day 5", "impact": 2, "tier": "flavor", "turn_created": 5},
        ],
        "Vex": [
            {"text": "Delivered parts at the docks", "quote": "Here's your stuff, no questions.", "date": "Day 7", "impact": 3, "tier": "moderate", "turn_created": 7},
        ]
    }


@pytest.fixture
def populated_scene():
    return {
        "location": "The Rusted Anchor, back booth",
        "npcs_present": ["Kira", "Dockmaster Yael"],
        "pcs_present": ["Aedina", "Orrophim"],
        "active_tensions": ["Yael suspects Kira is lying about the cargo manifest"],
        "scene_trigger": "Kira called an emergency meeting after the warehouse raid",
        "atmosphere": "Tense, low lighting, rain hammering the windows",
        "details": ["Aedina's disguise is still active", "speeder parked in alley"],
        "pending_actions": ["Orrophim waiting for Vex's signal from outside"]
    }


@pytest.fixture
def full_pipeline_state(populated_ledger, populated_memories, populated_scene):
    return {
        "pacing": {"episode": "Episode 3", "beat": "The Meeting", "beat_responses": 2, "notes": "tension rising"},
        "callback_ledger": populated_ledger,
        "npc_memories": populated_memories,
        "scene_state": populated_scene,
        "character_states": {
            "Aedina": {"state": "45/50 HP, 2/3 spell slots", "last_updated": 9},
            "Orrophim": {"state": "38/38 HP, rage 2/3", "last_updated": 9},
        },
        "turn_counter": 10
    }


# ============================================================
# Step 1: Migration Tests
# ============================================================

class TestMigratePipelineState:
    def test_none_returns_fresh(self):
        result = migrate_pipeline_state(None)
        assert result["pacing"] == {}
        assert result["callback_ledger"]["next_id"] == 1
        assert result["callback_ledger"]["open"] == []
        assert result["callback_ledger"]["recently_resolved"] == []
        assert result["npc_memories"] == {}
        assert result["scene_state"] == {}
        assert result["hud_state"] == {}
        assert result["turn_counter"] == 0

    def test_old_flat_pacing_wrapped(self):
        old_state = {"episode": "Ep1", "beat": "Opening", "beat_responses": 1, "notes": ""}
        result = migrate_pipeline_state(old_state)
        assert result["pacing"] == old_state
        assert "callback_ledger" in result
        assert result["callback_ledger"]["next_id"] == 1
        assert result["npc_memories"] == {}
        assert result["scene_state"] == {}
        assert result["hud_state"] == {}
        assert result["turn_counter"] == 0

    def test_real_legacy_format(self):
        """Migrate a real legacy pipeline_state as stored in existing chats."""
        legacy = {
            "episode": "Session 6: The Walls Have Eyes",
            "beat": "Shipboard pre-Lab Setup: Sara talks privately with Lydia",
            "beat_responses": 7,
            "notes": "Player message appears OOC after context updates"
        }
        result = migrate_pipeline_state(legacy)

        # Pacing should be the original dict
        assert result["pacing"]["episode"] == "Session 6: The Walls Have Eyes"
        assert result["pacing"]["beat_responses"] == 7

        # All new subsystems should be initialized empty
        assert result["callback_ledger"] == {"next_id": 1, "open": [], "recently_resolved": []}
        assert result["npc_memories"] == {}
        assert result["scene_state"] == {}
        assert result["hud_state"] == {}
        assert result["turn_counter"] == 0

        # Migrated state should be usable by all op-application functions
        result["turn_counter"] += 1
        result["callback_ledger"] = apply_callback_ops(
            result["callback_ledger"],
            [{"action": "add", "original_text": "Sara promised to explain", "source_npc": "Sara"}],
            result["turn_counter"]
        )
        assert result["callback_ledger"]["open"][0]["id"] == 1

        result["npc_memories"] = apply_npc_memory_ops(
            result["npc_memories"],
            [{"action": "add", "npc": "Lydia", "text": "Confided about interface incident", "date": "Day 6", "impact": 4}],
            result["turn_counter"]
        )
        assert "Lydia" in result["npc_memories"]

        result["scene_state"] = apply_scene_state({
            "location": "Captain's Quarters",
            "npcs_present": ["Lydia"],
            "active_tensions": ["Sara worried about safety protocol"],
            "scene_trigger": "Private meeting requested",
            "atmosphere": "Quiet, serious",
            "details": [],
            "pending_actions": []
        })
        assert result["scene_state"]["location"] == "Captain's Quarters"

        # Injections should work with this state
        cb = build_callback_injection(result["callback_ledger"])
        assert "Sara promised to explain" in cb

        mem = build_npc_memories_injection(result["npc_memories"], result["scene_state"])
        assert "[NPC MEMORIES: Lydia]" in mem

        scene = build_scene_state_injection(result["scene_state"])
        assert "Captain's Quarters" in scene

        # build_events_messages should not crash
        messages = build_events_messages(
            system_prompt="You are Events.",
            history_messages=[],
            user_message={"role": "user", "content": "test"},
            pipeline_state=result,
        )
        user_content = messages[-1]["content"]
        assert "[CALLBACK LEDGER]" in user_content
        assert "[NPC MEMORIES: Lydia]" in user_content
        assert "[SCENE STATE]" in user_content

    def test_legacy_migration_then_second_migration_is_idempotent(self):
        """Migrating an already-migrated state should be a no-op."""
        legacy = {"episode": "Ep1", "beat": "Beat1", "beat_responses": 1, "notes": ""}
        first = migrate_pipeline_state(legacy)
        first["turn_counter"] = 5
        first["callback_ledger"]["next_id"] = 3
        first["npc_memories"]["Kira"] = [{"text": "test", "tier": "high"}]

        second = migrate_pipeline_state(copy.deepcopy(first))
        assert second["turn_counter"] == 5
        assert second["callback_ledger"]["next_id"] == 3
        assert "Kira" in second["npc_memories"]

    def test_new_format_passthrough(self):
        new_state = {
            "pacing": {"episode": "Ep2"},
            "callback_ledger": {"next_id": 5, "open": [{"id": 1}], "recently_resolved": []},
            "npc_memories": {"Kira": [{"text": "test"}]},
            "scene_state": {"location": "Tavern"},
            "turn_counter": 7
        }
        result = migrate_pipeline_state(copy.deepcopy(new_state))
        assert result["pacing"] == {"episode": "Ep2"}
        assert result["callback_ledger"]["next_id"] == 5
        assert result["turn_counter"] == 7

    def test_new_format_missing_keys_filled(self):
        """New format with some keys missing gets defaults via setdefault."""
        partial = {"pacing": {"episode": "Ep3"}}
        result = migrate_pipeline_state(partial)
        assert result["callback_ledger"]["next_id"] == 1
        assert result["npc_memories"] == {}
        assert result["scene_state"] == {}
        assert result["hud_state"] == {}
        assert result["turn_counter"] == 0

    def test_new_format_ledger_missing_subkeys(self):
        """Ledger exists but is missing sub-keys."""
        state = {
            "pacing": {},
            "callback_ledger": {},
            "npc_memories": {},
            "scene_state": {},
            "turn_counter": 3
        }
        result = migrate_pipeline_state(state)
        assert result["callback_ledger"]["next_id"] == 1
        assert result["callback_ledger"]["open"] == []
        assert result["callback_ledger"]["recently_resolved"] == []

    def test_fresh_state_structure(self):
        state = _fresh_pipeline_state()
        assert set(state.keys()) == {"pacing", "callback_ledger", "npc_memories", "scene_state", "character_states", "game_state", "hud_state", "combat", "ship_combat", "turn_counter"}


# ============================================================
# Step 2: Op-Application Tests
# ============================================================

class TestApplyCallbackOps:
    def test_add_op(self, empty_ledger):
        ops = [{"action": "add", "original_text": "Promised treasure map", "source_npc": "Kira"}]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["open"]) == 1
        assert result["open"][0]["id"] == 1
        assert result["open"][0]["created_turn"] == 1
        assert result["open"][0]["original_text"] == "Promised treasure map"
        assert result["open"][0]["source_npc"] == "Kira"
        assert result["next_id"] == 2

    def test_add_multiple(self, empty_ledger):
        ops = [
            {"action": "add", "original_text": "First callback", "source_npc": None},
            {"action": "add", "original_text": "Second callback", "source_npc": "Vex"},
        ]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["open"]) == 2
        assert result["open"][0]["id"] == 1
        assert result["open"][1]["id"] == 2
        assert result["next_id"] == 3

    def test_add_truncates_long_text(self, empty_ledger):
        ops = [{"action": "add", "original_text": "x" * 1000, "source_npc": None}]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["open"][0]["original_text"]) == 800

    def test_resolve_op(self, populated_ledger):
        ops = [{"action": "resolve", "id": 2, "resolution_text": "Delivered the parts at the docks"}]
        result = apply_callback_ops(populated_ledger, ops, current_turn=15)
        # Should move from open to recently_resolved
        open_ids = [cb["id"] for cb in result["open"]]
        assert 2 not in open_ids
        assert len(result["recently_resolved"]) == 1
        resolved = result["recently_resolved"][0]
        assert resolved["id"] == 2
        assert resolved["resolved_turn"] == 15
        assert resolved["resolution_text"] == "Delivered the parts at the docks"

    def test_resolve_missing_id_warns(self, empty_ledger, caplog):
        ops = [{"action": "resolve", "id": 999, "resolution_text": "nope"}]
        with caplog.at_level(logging.WARNING):
            result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert "999" in caplog.text
        assert len(result["recently_resolved"]) == 0

    def test_resolve_missing_id_key_warns(self, empty_ledger, caplog):
        ops = [{"action": "resolve", "resolution_text": "nope"}]
        with caplog.at_level(logging.WARNING):
            result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["recently_resolved"]) == 0

    def test_update_op(self, populated_ledger):
        ops = [{"action": "update", "id": 1, "fields": {"original_text": "Updated: tunnels are now flooded"}}]
        result = apply_callback_ops(populated_ledger, ops, current_turn=15)
        updated = [cb for cb in result["open"] if cb["id"] == 1][0]
        assert updated["original_text"] == "Updated: tunnels are now flooded"
        # Immutable fields should not change
        assert updated["created_turn"] == 5

    def test_update_cannot_change_immutable_fields(self, populated_ledger):
        ops = [{"action": "update", "id": 1, "fields": {"id": 999, "created_turn": 999}}]
        result = apply_callback_ops(populated_ledger, ops, current_turn=15)
        updated = [cb for cb in result["open"] if cb["id"] == 1][0]
        assert updated["id"] == 1
        assert updated["created_turn"] == 5

    def test_update_missing_id_warns(self, empty_ledger, caplog):
        ops = [{"action": "update", "id": 42, "fields": {"original_text": "nope"}}]
        with caplog.at_level(logging.WARNING):
            apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert "42" in caplog.text

    def test_prune_old_resolved(self):
        ledger = {
            "next_id": 5,
            "open": [],
            "recently_resolved": [
                {"id": 1, "created_turn": 1, "original_text": "old", "source_npc": None, "resolved_turn": 5, "resolution_text": "done"},
                {"id": 2, "created_turn": 2, "original_text": "recent", "source_npc": None, "resolved_turn": 25, "resolution_text": "done"},
            ]
        }
        # current_turn=30: entry resolved at turn 5 is 25 turns ago (> 20), should be pruned
        result = apply_callback_ops(ledger, [], current_turn=30)
        assert len(result["recently_resolved"]) == 1
        assert result["recently_resolved"][0]["id"] == 2

    def test_prune_happens_even_with_no_ops(self):
        ledger = {
            "next_id": 2,
            "open": [],
            "recently_resolved": [
                {"id": 1, "resolved_turn": 1, "original_text": "old", "source_npc": None, "created_turn": 1, "resolution_text": "done"},
            ]
        }
        result = apply_callback_ops(ledger, None, current_turn=100)
        assert len(result["recently_resolved"]) == 0

    def test_add_then_resolve_same_turn(self, empty_ledger):
        ops = [
            {"action": "add", "original_text": "Quick promise", "source_npc": "Kira"},
            {"action": "resolve", "id": 1, "resolution_text": "Immediately fulfilled"},
        ]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["open"]) == 0
        assert len(result["recently_resolved"]) == 1
        assert result["recently_resolved"][0]["resolution_text"] == "Immediately fulfilled"

    def test_none_ops_no_crash(self, empty_ledger):
        result = apply_callback_ops(empty_ledger, None, current_turn=1)
        assert result["open"] == []

    def test_empty_ops_list(self, populated_ledger):
        open_count = len(populated_ledger["open"])
        result = apply_callback_ops(populated_ledger, [], current_turn=15)
        assert len(result["open"]) == open_count

    def test_add_non_list_resolutions_ignored(self, empty_ledger):
        ops = [{"action": "add", "original_text": "Hook", "resolutions": 5}]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert len(result["open"]) == 1
        assert result["open"][0]["resolutions"] is None

    def test_malformed_open_entries_ignored(self):
        ledger = {"next_id": 2, "open": [1, {"id": 1, "created_turn": 1, "original_text": "x"}], "recently_resolved": []}
        result = apply_callback_ops(ledger, [], current_turn=2)
        assert len(result["open"]) == 1
        assert result["open"][0]["id"] == 1


class TestMemoryTier:
    def test_high(self):
        assert _memory_tier(5) == "high"
        assert _memory_tier(4) == "high"

    def test_moderate(self):
        assert _memory_tier(3) == "moderate"

    def test_flavor(self):
        assert _memory_tier(2) == "flavor"
        assert _memory_tier(1) == "flavor"


class TestApplyNpcMemoryOps:
    def test_add_op(self):
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "Saved her life", "quote": "I owe you.", "date": "Day 3", "impact": 5}]
        result = apply_npc_memory_ops(memories, ops, current_turn=3)
        assert "Kira" in result
        assert len(result["Kira"]) == 1
        mem = result["Kira"][0]
        assert mem["text"] == "Saved her life"
        assert mem["quote"] == "I owe you."
        assert mem["tier"] == "high"
        assert mem["turn_created"] == 3

    def test_add_truncates_text_and_quote(self):
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "x" * 800, "quote": "y" * 200, "date": "Day 1", "impact": 3}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert len(result["Kira"][0]["text"]) == 640
        assert len(result["Kira"][0]["quote"]) == 120

    def test_add_null_quote_becomes_none(self):
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "test", "date": "Day 1", "impact": 1}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert result["Kira"][0]["quote"] is None

    def test_add_empty_quote_becomes_none(self):
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "test", "quote": "", "date": "Day 1", "impact": 1}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert result["Kira"][0]["quote"] is None

    def test_drop_op(self, populated_memories):
        ops = [{"action": "drop", "npc": "Kira", "index": 1}]  # Drop the flavor memory
        result = apply_npc_memory_ops(populated_memories, ops, current_turn=10)
        assert len(result["Kira"]) == 1
        assert result["Kira"][0]["text"] == "Saved Kira from warehouse explosion"

    def test_drop_removes_empty_npc(self):
        memories = {"Kira": [{"text": "only one", "quote": None, "date": "Day 1", "impact": 1, "tier": "flavor", "turn_created": 1}]}
        ops = [{"action": "drop", "npc": "Kira", "index": 0}]
        result = apply_npc_memory_ops(memories, ops, current_turn=5)
        assert "Kira" not in result

    def test_drop_invalid_npc_warns(self, caplog):
        memories = {}
        ops = [{"action": "drop", "npc": "Nobody", "index": 0}]
        with caplog.at_level(logging.WARNING):
            apply_npc_memory_ops(memories, ops, current_turn=1)
        assert "Nobody" in caplog.text

    def test_drop_invalid_index_warns(self, populated_memories, caplog):
        ops = [{"action": "drop", "npc": "Kira", "index": 99}]
        with caplog.at_level(logging.WARNING):
            apply_npc_memory_ops(populated_memories, ops, current_turn=1)
        assert "99" in caplog.text

    def test_drops_processed_in_reverse_order(self):
        """Multiple drops on same NPC should process in reverse index order."""
        memories = {
            "Kira": [
                {"text": "mem0", "quote": None, "date": "D1", "impact": 1, "tier": "flavor", "turn_created": 1},
                {"text": "mem1", "quote": None, "date": "D2", "impact": 2, "tier": "flavor", "turn_created": 2},
                {"text": "mem2", "quote": None, "date": "D3", "impact": 3, "tier": "moderate", "turn_created": 3},
            ]
        }
        # Drop indices 0 and 2 — should work correctly regardless of order in ops
        ops = [
            {"action": "drop", "npc": "Kira", "index": 0},
            {"action": "drop", "npc": "Kira", "index": 2},
        ]
        result = apply_npc_memory_ops(memories, ops, current_turn=5)
        assert len(result["Kira"]) == 1
        assert result["Kira"][0]["text"] == "mem1"

    def test_per_npc_cap(self):
        """Per-NPC cap (30) trims oldest when exceeded, but tier limits may trim further."""
        # Use mixed tiers so the per-NPC cap is the binding constraint, not tier limits
        memories = {"Kira": []}
        # 8 high + 10 moderate + 12 flavor = 30 (exactly at tier limits)
        for i in range(8):
            memories["Kira"].append({"text": f"high{i}", "quote": None, "date": "D1", "impact": 5, "tier": "high", "turn_created": i})
        for i in range(10):
            memories["Kira"].append({"text": f"mod{i}", "quote": None, "date": "D1", "impact": 3, "tier": "moderate", "turn_created": 8+i})
        for i in range(12):
            memories["Kira"].append({"text": f"flav{i}", "quote": None, "date": "D1", "impact": 1, "tier": "flavor", "turn_created": 18+i})
        assert len(memories["Kira"]) == NPC_MEMORY_MAX_PER_NPC

        # Adding one more triggers per-NPC cap (31→30 keeps highest impact)
        # Also triggers tier limit for moderate (11→10 drops oldest moderate)
        ops = [{"action": "add", "npc": "Kira", "text": "new one", "date": "D99", "impact": 3}]
        result = apply_npc_memory_ops(memories, ops, current_turn=100)
        assert len(result["Kira"]) <= NPC_MEMORY_MAX_PER_NPC
        # The new memory should be present (sorted among moderates by impact desc, turn desc)
        new_texts = [m["text"] for m in result["Kira"]]
        assert "new one" in new_texts
        # Result should be sorted by impact desc, turn_created desc
        for i in range(len(result["Kira"]) - 1):
            a, b = result["Kira"][i], result["Kira"][i + 1]
            assert (a["impact"], a["turn_created"]) >= (b["impact"], b["turn_created"])

    def test_tier_limit_enforcement(self):
        """Adding beyond tier limit drops oldest in that tier."""
        tier_limit = NPC_MEMORY_TIER_LIMITS["high"]  # 8
        memories = {"Kira": [
            {"text": f"high{i}", "quote": None, "date": "D1", "impact": 5, "tier": "high", "turn_created": i}
            for i in range(tier_limit)
        ]}
        ops = [{"action": "add", "npc": "Kira", "text": "one more high", "date": "D99", "impact": 5}]
        result = apply_npc_memory_ops(memories, ops, current_turn=100)
        high_mems = [m for m in result["Kira"] if m["tier"] == "high"]
        assert len(high_mems) <= tier_limit

    def test_no_ops_returns_unchanged(self, populated_memories):
        original = copy.deepcopy(populated_memories)
        result = apply_npc_memory_ops(populated_memories, [], current_turn=10)
        assert result == original

    def test_none_ops_returns_unchanged(self, populated_memories):
        original = copy.deepcopy(populated_memories)
        result = apply_npc_memory_ops(populated_memories, None, current_turn=10)
        assert result == original


class TestApplySceneState:
    def test_full_scene(self, populated_scene):
        result = apply_scene_state(populated_scene)
        assert result["location"] == "The Rusted Anchor, back booth"
        assert result["npcs_present"] == ["Kira", "Dockmaster Yael"]
        assert result["pcs_present"] == ["Aedina", "Orrophim"]
        assert len(result["active_tensions"]) == 1
        assert result["atmosphere"] == "Tense, low lighting, rain hammering the windows"

    def test_missing_keys_get_defaults(self):
        result = apply_scene_state({"location": "Tavern"})
        assert result["location"] == "Tavern"
        assert result["npcs_present"] == []
        assert result["pcs_present"] == []
        assert result["active_tensions"] == []
        assert result["scene_trigger"] == ""
        assert result["atmosphere"] == ""
        assert result["details"] == []
        assert result["pending_actions"] == []

    def test_empty_input(self):
        result = apply_scene_state({})
        assert result["location"] == ""
        assert result["npcs_present"] == []
        assert result["pcs_present"] == []

    def test_extra_keys_ignored(self):
        result = apply_scene_state({"location": "X", "bogus_key": "should be dropped"})
        assert "bogus_key" not in result
        assert result["location"] == "X"


class TestSceneScopedFunds:
    """Test that hud_state.funds dict is filtered to scene-present characters + non-character keys."""

    @staticmethod
    def _apply_funds_filter(events_data, pipeline_state):
        """Replicate the filtering logic from run_pipeline."""
        hud_funds = events_data.get("hud_state", {}).get("funds")
        scene = pipeline_state.get("scene_state", {})
        pcs_present = scene.get("pcs_present")
        if isinstance(hud_funds, dict) and pcs_present:
            present = set(pcs_present) | set(scene.get("npcs_present", []))
            all_characters = set(pipeline_state.get("character_states", {}).keys())
            events_data["hud_state"]["funds"] = {
                k: v for k, v in hud_funds.items()
                if k in present or k not in all_characters
            }

    def test_funds_dict_filtered_to_pcs_present(self):
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp", "Orrophim": "18 gp", "Vex": "5 gp"}}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina", "Orrophim"]},
            "character_states": {"Aedina": {}, "Orrophim": {}, "Vex": {}}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp", "Orrophim": "18 gp"}

    def test_npc_funds_kept_when_present(self):
        """NPC hireling funds should survive if the NPC is in npcs_present."""
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp", "Squire Tam": "8 gp", "Vex": "5 gp"}}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina"], "npcs_present": ["Squire Tam"]},
            "character_states": {"Aedina": {}, "Squire Tam": {}, "Vex": {}}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp", "Squire Tam": "8 gp"}

    def test_absent_npc_funds_filtered(self):
        """NPC not in npcs_present should have their funds filtered out."""
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp", "Squire Tam": "8 gp"}}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina"], "npcs_present": []},
            "character_states": {"Aedina": {}, "Squire Tam": {}}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp"}

    def test_non_character_funds_pass_through(self):
        """Ship/party funds not in character_states should survive filtering."""
        events_data = {
            "hud_state": {"funds": {
                "The Rustbucket": "1,200 credits",
                "Aedina": "32 credits",
                "Orrophim": "18 credits",
                "Vex": "5 credits"
            }}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina", "Orrophim"]},
            "character_states": {"Aedina": {}, "Orrophim": {}, "Vex": {}}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {
            "The Rustbucket": "1,200 credits",
            "Aedina": "32 credits",
            "Orrophim": "18 credits"
        }

    def test_funds_string_unchanged(self):
        events_data = {
            "hud_state": {"funds": "50 gp party fund"}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina"]}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == "50 gp party fund"

    def test_funds_dict_no_pcs_present_unchanged(self):
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp", "Orrophim": "18 gp"}}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": []},
            "character_states": {"Aedina": {}, "Orrophim": {}}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        # Empty pcs_present is falsy, so funds should remain unchanged
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp", "Orrophim": "18 gp"}

    def test_funds_dict_no_scene_state(self):
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp"}}
        }
        pipeline_state = {}
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp"}

    def test_no_hud_state(self):
        events_data = {"route": "mechanics"}
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina"]}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        # No hud_state at all — should not crash
        assert "hud_state" not in events_data

    def test_no_character_states_all_funds_kept(self):
        """Without character_states, all keys are non-character so all pass through."""
        events_data = {
            "hud_state": {"funds": {"Aedina": "32 gp", "Vex": "5 gp"}}
        }
        pipeline_state = {
            "scene_state": {"pcs_present": ["Aedina"]}
        }
        self._apply_funds_filter(events_data, pipeline_state)
        assert events_data["hud_state"]["funds"] == {"Aedina": "32 gp", "Vex": "5 gp"}


# ============================================================
# Step 3: Injection Builder Tests
# ============================================================

class TestBuildCallbackInjection:
    def test_empty_ledger(self, empty_ledger):
        assert build_callback_injection(empty_ledger) == ""

    def test_open_only(self):
        ledger = {
            "open": [
                {"id": 1, "created_turn": 5, "original_text": "Kira promised tunnels", "source_npc": "Kira"},
                {"id": 2, "created_turn": 8, "original_text": "Signal from sector", "source_npc": None},
            ],
            "recently_resolved": []
        }
        result = build_callback_injection(ledger)
        assert "[CALLBACK LEDGER]" in result
        assert "[/CALLBACK LEDGER]" in result
        assert "OPEN:" in result
        assert '#1 (turn 5, Kira): "Kira promised tunnels"' in result
        assert '#2 (turn 8, null): "Signal from sector"' in result
        assert "RECENTLY RESOLVED" not in result

    def test_resolved_only(self):
        ledger = {
            "open": [],
            "recently_resolved": [
                {"id": 1, "created_turn": 5, "original_text": "Promise", "source_npc": "Kira", "resolved_turn": 10, "resolution_text": "Fulfilled"},
            ]
        }
        result = build_callback_injection(ledger)
        assert "OPEN: (none)" in result
        assert "RECENTLY RESOLVED:" in result
        assert "#1 (turn 5" in result
        assert "10" in result
        assert "Fulfilled" in result

    def test_both_open_and_resolved(self, populated_ledger):
        # Add a resolved entry
        populated_ledger["recently_resolved"] = [
            {"id": 10, "created_turn": 1, "original_text": "Old promise", "source_npc": "Yael", "resolved_turn": 8, "resolution_text": "Done"}
        ]
        result = build_callback_injection(populated_ledger)
        assert "OPEN:" in result
        assert "RECENTLY RESOLVED:" in result


class TestBuildNpcMemoriesInjection:
    def test_empty_scene_no_npcs(self, populated_memories):
        result = build_npc_memories_injection(populated_memories, {"npcs_present": []})
        assert result == ""

    def test_no_memories(self):
        result = build_npc_memories_injection({}, {"npcs_present": ["Kira"]})
        assert result == ""

    def test_scene_scoped(self, populated_memories):
        """Only NPCs in the scene should be included."""
        scene = {"npcs_present": ["Kira"]}  # Vex is NOT present
        result = build_npc_memories_injection(populated_memories, scene)
        assert "[NPC MEMORIES: Kira]" in result
        assert "Vex" not in result

    def test_sorted_by_impact_desc(self, populated_memories):
        scene = {"npcs_present": ["Kira"]}
        result = build_npc_memories_injection(populated_memories, scene)
        lines = result.split("\n")
        # Memory lines now start with "[0]", "[1]", etc.
        memory_lines = [l for l in lines if l.startswith("[") and "] [" in l]
        assert "\u2605\u2605\u2605\u2605\u2605" in memory_lines[0]  # 5 stars
        assert "\u2605\u2605" in memory_lines[1]  # 2 stars (but not 5)

    def test_quote_included(self, populated_memories):
        scene = {"npcs_present": ["Kira"]}
        result = build_npc_memories_injection(populated_memories, scene)
        assert '| "I don\'t forget debts."' in result

    def test_no_quote_no_pipe(self, populated_memories):
        scene = {"npcs_present": ["Kira"]}
        result = build_npc_memories_injection(populated_memories, scene)
        # The flavor memory has no quote
        lines = [l for l in result.split("\n") if "Orrophim" in l]
        for line in lines:
            # Should not have a pipe for null quote
            if "pun" in line:
                assert '| "' not in line

    def test_multiple_npcs(self, populated_memories):
        scene = {"npcs_present": ["Kira", "Vex"]}
        result = build_npc_memories_injection(populated_memories, scene)
        assert "[NPC MEMORIES: Kira]" in result
        assert "[NPC MEMORIES: Vex]" in result

    def test_npc_not_in_memories_skipped(self, populated_memories):
        scene = {"npcs_present": ["Kira", "Nobody"]}
        result = build_npc_memories_injection(populated_memories, scene)
        assert "Nobody" not in result

    def test_empty_scene_state(self, populated_memories):
        result = build_npc_memories_injection(populated_memories, {})
        assert result == ""


class TestBuildSceneStateInjection:
    def test_full_scene(self, populated_scene):
        result = build_scene_state_injection(populated_scene)
        assert "[SCENE STATE]" in result
        assert "[/SCENE STATE]" in result
        assert "Location: The Rusted Anchor, back booth" in result
        assert "NPCs Present: Kira, Dockmaster Yael" in result
        assert "PCs Present: Aedina, Orrophim" in result
        assert "Atmosphere: Tense, low lighting" in result

    def test_empty_scene(self):
        result = build_scene_state_injection({})
        assert "[SCENE STATE]" in result
        assert "(empty" in result
        assert "[/SCENE STATE]" in result

    def test_empty_lists_show_none(self):
        scene = {"location": "Tavern", "npcs_present": [], "active_tensions": []}
        result = build_scene_state_injection(scene)
        assert "NPCs Present: (none)" in result
        assert "Active Tensions: (none)" in result

    def test_list_values_joined(self, populated_scene):
        result = build_scene_state_injection(populated_scene)
        assert "Aedina's disguise is still active, speeder parked in alley" in result


# ============================================================
# Step 4: build_events_messages Integration Tests
# ============================================================

class TestBuildEventsMessages:
    def test_all_injections_present(self, full_pipeline_state):
        messages = build_events_messages(
            system_prompt="You are Events.",
            history_messages=[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
            user_message={"role": "user", "content": "I attack the guard"},
            pipeline_state=full_pipeline_state,
        )
        # System + history + final user message
        assert messages[0]["role"] == "system"
        assert len(messages) == 4  # system + 2 history + user

        user_content = messages[-1]["content"]
        assert "[PIPELINE STATE]" in user_content
        assert "[CALLBACK LEDGER]" in user_content
        assert "[NPC MEMORIES:" in user_content
        assert "[SCENE STATE]" in user_content
        assert "[CHARACTER STATES]" in user_content
        assert "[CONTEXT UPDATES]" not in user_content
        assert "I attack the guard" in user_content

    def test_injection_order(self, full_pipeline_state):
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=full_pipeline_state,
        )
        user_content = messages[-1]["content"]
        # Verify order: PIPELINE STATE before CALLBACK LEDGER before NPC MEMORIES before SCENE STATE before CHARACTER STATES before user message
        idx_pipeline = user_content.index("[PIPELINE STATE]")
        idx_callback = user_content.index("[CALLBACK LEDGER]")
        idx_memories = user_content.index("[NPC MEMORIES:")
        idx_scene = user_content.index("[SCENE STATE]")
        idx_char = user_content.index("[CHARACTER STATES]")
        idx_action = user_content.index("action")
        assert idx_pipeline < idx_callback < idx_memories < idx_scene < idx_char < idx_action

    def test_empty_state_minimal_injections(self, fresh_state):
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "hello"},
            pipeline_state=fresh_state,
        )
        user_content = messages[-1]["content"]
        # Empty state should not inject callback/memory blocks
        assert "[CALLBACK LEDGER]" not in user_content
        assert "[NPC MEMORIES:" not in user_content
        # Pacing is empty too
        assert "[PIPELINE STATE]" not in user_content
        # Bootstrap markers should be present for scene and character states
        assert "[SCENE STATE]" in user_content
        assert "(empty" in user_content
        assert "[CHARACTER STATES]" in user_content
        assert "hello" in user_content

    def test_no_context_updates_ever(self, full_pipeline_state):
        """CONTEXT UPDATES injection was removed — verify it never appears."""
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=full_pipeline_state,
        )
        user_content = messages[-1]["content"]
        assert "[CONTEXT UPDATES]" not in user_content

    def test_npc_memories_scoped_to_scene(self, full_pipeline_state):
        """NPC memories should only include NPCs from scene_state.npcs_present."""
        # Scene has Kira and Dockmaster Yael; Vex is NOT present
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=full_pipeline_state,
        )
        user_content = messages[-1]["content"]
        assert "[NPC MEMORIES: Kira]" in user_content
        # Vex has memories but is NOT in the scene
        assert "[NPC MEMORIES: Vex]" not in user_content


class TestBuildMechanicsMessages:
    """Coverage for mechanics message construction, including game injection."""

    def test_build_mechanics_messages_includes_game_injection_before_dice_pool(self):
        events_json = {"route": "narration", "beats": [{"beat": "x"}]}
        game_injection = "[RELATIONSHIP STATE]\nRogue: RS 55\n[/RELATIONSHIP STATE]"
        dice_pool = "[DICE POOL]\n1,2,3\n[/DICE POOL]"

        messages = build_mechanics_messages(
            system_prompt="sys",
            events_json=events_json,
            dice_pool=dice_pool,
            game_injection=game_injection,
        )

        assert isinstance(messages, list)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        content = messages[1]["content"]
        assert json.dumps(events_json, indent=2) in content
        assert game_injection in content
        assert dice_pool in content
        assert content.index(game_injection) < content.index(dice_pool)

    def test_build_mechanics_messages_seeded_fuzz_keeps_structure_and_order(self):
        rng = random.Random(9201)
        alphabet = "abcdef0123456789 []:_-/\n"
        events_json = {
            "route": "narration",
            "beats": [{"beat": "probe", "rolls": []}],
            "character_states": {"V": {"state": "ok"}},
        }
        base_json = json.dumps(events_json, indent=2)

        for _ in range(300):
            game_injection = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 140))).strip()
            dice_pool = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 100))).strip()
            messages = build_mechanics_messages(
                system_prompt="sys",
                events_json=events_json,
                dice_pool=dice_pool,
                game_injection=game_injection,
            )

            assert isinstance(messages, list)
            assert len(messages) == 2
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            content = messages[1]["content"]
            expected = base_json
            if game_injection:
                expected += "\n\n" + game_injection
            if dice_pool:
                expected += "\n\n" + dice_pool
            assert content == expected

            if game_injection:
                assert game_injection in content
            if dice_pool:
                assert dice_pool in content


# ============================================================
# Step 4 continued: get_context_pairs Tests
# ============================================================

class TestGetContextPairs:
    def _make_branch(self, n_pairs):
        """Create a branch_path with system + n_pairs*2 history + current user."""
        msgs = [{"role": "system", "content": "system prompt"}]
        for i in range(n_pairs):
            msgs.append({"role": "user", "content": f"user msg {i}"})
            msgs.append({"role": "assistant", "content": f"assistant msg {i}"})
        msgs.append({"role": "user", "content": "current user message"})
        return msgs

    def test_under_threshold_includes_all(self):
        """Under threshold: all pairs included (prefix preserved for caching)."""
        branch = self._make_branch(30)
        pairs, _, _ = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 60  # All 30 pairs included

    def test_at_threshold_includes_all(self):
        """At exactly threshold: all pairs included (no trim needed)."""
        branch = self._make_branch(40)
        pairs, _, _ = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 80  # All 40 pairs included

    def test_over_threshold_trims_to_target(self):
        """Over threshold: trimmed to target pairs."""
        branch = self._make_branch(41)
        pairs, _, _ = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 40  # Trimmed to 20 pairs
        # Should be the LAST 20 pairs
        assert pairs[0]["content"] == "user msg 21"

    def test_excludes_system_and_current_user(self):
        branch = self._make_branch(5)
        pairs, _, _ = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert all(p["content"] != "system prompt" for p in pairs)
        assert all(p["content"] != "current user message" for p in pairs)

    def test_attached_files_included(self):
        branch = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello", "attached_files": [{"filename": "test.md", "content": "file content"}]},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "next"},
        ]
        pairs, _, _ = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert "====FILE: test.md====" in pairs[0]["content"]
        assert "file content" in pairs[0]["content"]


# ============================================================
# Step 5: State extraction in run_pipeline (E2E with mocks)
# ============================================================

def _make_mock_provider():
    """Create a mock OpenAIProvider for testing."""
    provider = MagicMock()
    provider.build_pipeline_request = MagicMock(return_value={})
    provider.calculate_cost_with_tier = MagicMock(return_value=0.01)
    return provider


def _make_events_response(route="mechanics", pacing=None, callback_ops=None, npc_memory_ops=None, scene_state=None, extra_fields=None):
    """Build a mock Events JSON response."""
    data = {
        "route": route,
        "pacing": pacing or {"episode": "Ep1", "beat": "Beat1", "beat_responses": 1, "notes": ""},
        "time_passed": "5 minutes",
        "beats": ["something happened"],
        "player_action": "attack",
        "callbacks": [],
        "emotional_context": "tense",
        "character_states": {"Aedina": "50 HP"},
        "score_changes": [],
        "arc_label": None,
        "current_player": "Aedina",
        "next_player": "Orrophim",
        "next_player_prompt": "You see the aftermath",
        "hud_state": {"date": "Day 5", "time": "1430", "location": "Tavern", "funds": "100 gp", "trackables": None},
        "combat": None,
    }
    if callback_ops is not None:
        data["callback_ops"] = callback_ops
    if npc_memory_ops is not None:
        data["npc_memory_ops"] = npc_memory_ops
    if scene_state is not None:
        data["scene_state"] = scene_state
    if extra_fields:
        data.update(extra_fields)
    return data


def _make_mechanics_response(route="narration", character_states=None):
    data = {
        "route": route,
        "beats": [{"beat": "attack", "outcome": "hit", "rolls": [], "state_changes": []}],
        "dramatic_notes": "dramatic",
        "hud": "[Date: Day 5 | Time: 1435 | Loc: Tavern | Funds: 100 gp]",
        "score_changes": [],
        "arc_label": None,
        "callbacks": [],
        "current_player": "Aedina",
        "next_player": "Orrophim",
        "next_player_prompt": "You see the aftermath",
        "combat": None,
    }
    if character_states is not None:
        data["character_states"] = character_states
    return data


def _make_branch_path(n_history_pairs=5):
    """Create a realistic branch_path for testing."""
    msgs = [{"id": "sys", "parent_id": None, "role": "system", "content": "System prompt"}]
    parent = "sys"
    for i in range(n_history_pairs):
        uid = f"user_{i}"
        aid = f"asst_{i}"
        msgs.append({"id": uid, "parent_id": parent, "role": "user", "content": f"User message {i}"})
        msgs.append({"id": aid, "parent_id": uid, "role": "assistant", "content": f"Assistant response {i}"})
        parent = aid
    # Current user message
    msgs.append({"id": "user_current", "parent_id": parent, "role": "user", "content": "I attack the guard"})
    return msgs


from providers import StreamEvent


class TestRunPipelineE2E:
    """End-to-end tests for run_pipeline with mocked API calls."""

    def _run_pipeline_collect(self, events_data, mechanics_data=None, narration_text="The guard falls.",
                              pipeline_state=None, n_history=5, game_system="dnd5e"):
        """Helper to run pipeline and collect all yielded events."""
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history)

        events_json = json.dumps(events_data)
        mechanics_json = json.dumps(mechanics_data) if mechanics_data else None

        # Mock non-streaming for Events and Mechanics
        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                # Events
                return {"content": events_json, "reasoning": None, "input_tokens": 1000, "cache_read_tokens": 0,
                        "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                # Mechanics
                return {"content": mechanics_json, "reasoning": None, "input_tokens": 500, "cache_read_tokens": 0,
                        "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming

        # Mock streaming for Narration
        def mock_stream(cl, params):
            yield StreamEvent(event_type="content_delta", content=narration_text)
            yield StreamEvent(event_type="done", usage={
                "content": narration_text, "input_tokens": 800, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 200, "reasoning_tokens": 0
            })

        provider.send_request_stream = mock_stream

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="testuser",
            project="testproject",
            chat_name="testchat",
            branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=pipeline_state,
            game_system=game_system,
        ))
        return events

    def test_full_pipeline_yields_all_stages(self):
        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data)

        event_types = [e[0] for e in events]
        assert "pipeline_stage" in event_types
        assert "content" in event_types
        assert "pipeline_done" in event_types

        # Check stage progression
        stages = [(e[1]["stage"], e[1]["status"]) for e in events if e[0] == "pipeline_stage"]
        assert ("events", "thinking") in stages
        assert ("events", "complete") in stages
        assert ("mechanics", "thinking") in stages
        assert ("mechanics", "complete") in stages
        assert ("narration", "thinking") in stages
        assert ("narration", "complete") in stages

    def test_pipeline_done_has_injected_state(self):
        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        assert isinstance(result, PipelineResult)
        assert result.injected_state is not None
        # Should be valid JSON
        parsed = json.loads(result.injected_state)
        assert "pacing" in parsed
        assert "callback_ledger" in parsed
        assert "npc_memories" in parsed
        assert "scene_state" in parsed
        assert "turn_counter" in parsed

    def test_migration_from_none(self):
        events_data = _make_events_response(
            scene_state={"location": "Tavern", "npcs_present": ["Kira"], "active_tensions": [],
                        "scene_trigger": "arrived", "atmosphere": "calm", "details": [], "pending_actions": []}
        )
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=None)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        state = result.pipeline_state
        assert state["turn_counter"] == 1  # First turn
        assert state["pacing"]["episode"] == "Ep1"
        assert state["scene_state"]["location"] == "Tavern"

    def test_migration_from_old_flat_pacing(self):
        old_state = {"episode": "OldEp", "beat": "OldBeat", "beat_responses": 5, "notes": "old"}
        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=old_state)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        state = result.pipeline_state
        # Turn counter should be 1 (migrated from 0 + increment)
        assert state["turn_counter"] == 1
        # Pacing should be updated from events output (not old pacing)
        assert "callback_ledger" in state
        assert "npc_memories" in state

    def test_callback_ops_applied(self):
        events_data = _make_events_response(
            callback_ops=[
                {"action": "add", "original_text": "New promise from Kira", "source_npc": "Kira"}
            ]
        )
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=None)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        state = done_event[1].pipeline_state
        assert len(state["callback_ledger"]["open"]) == 1
        assert state["callback_ledger"]["open"][0]["original_text"] == "New promise from Kira"
        assert state["callback_ledger"]["next_id"] == 2

    def test_npc_memory_ops_applied(self):
        events_data = _make_events_response(
            npc_memory_ops=[
                {"action": "add", "npc": "Kira", "text": "Saved her life", "quote": "Thank you.", "date": "Day 3", "impact": 5}
            ]
        )
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=None)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        state = done_event[1].pipeline_state
        assert "Kira" in state["npc_memories"]
        assert state["npc_memories"]["Kira"][0]["text"] == "Saved her life"
        assert state["npc_memories"]["Kira"][0]["tier"] == "high"

    def test_scene_state_applied(self):
        scene = {
            "location": "Dock District",
            "npcs_present": ["Vex", "Yael"],
            "active_tensions": ["Yael knows about the cargo"],
            "scene_trigger": "Arrived at docks",
            "atmosphere": "Foggy, damp",
            "details": ["Ship is fueled"],
            "pending_actions": []
        }
        events_data = _make_events_response(scene_state=scene)
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=None)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        state = done_event[1].pipeline_state
        assert state["scene_state"]["location"] == "Dock District"
        assert state["scene_state"]["npcs_present"] == ["Vex", "Yael"]

    def test_events_output_shortcircuit(self):
        """Events routing to output should still apply state ops and return pipeline_state."""
        events_data = {
            "route": "output",
            "pacing": {"episode": "Ep1", "beat": "OOC", "beat_responses": 0, "notes": ""},
            "time_passed": "0 minutes",
            "content": "Sure, let's take a break!",
            "callback_ops": [],
            "npc_memory_ops": [],
            "scene_state": {"location": "Tavern", "npcs_present": ["Kira"], "active_tensions": [],
                          "scene_trigger": "", "atmosphere": "calm", "details": [], "pending_actions": []}
        }
        events = self._run_pipeline_collect(events_data, pipeline_state=None)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        assert result.final_content == "Sure, let's take a break!"
        assert result.stages_run == ["events"]
        assert result.pipeline_state is not None
        assert result.pipeline_state["scene_state"]["location"] == "Tavern"
        assert result.pipeline_state["turn_counter"] == 0  # OOC doesn't advance turn counter
        assert result.injected_state is not None

    def test_mechanics_output_shortcircuit(self):
        events_data = _make_events_response()
        mechanics_data = {
            "route": "output",
            "content": "Your AC is 16."
        }
        events = self._run_pipeline_collect(events_data, mechanics_data)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        assert result.final_content == "Your AC is 16."
        assert result.stages_run == ["events", "mechanics"]
        assert result.injected_state is not None

    def test_turn_counter_increments(self):
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=state)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        assert done_event[1].pipeline_state["turn_counter"] == 6

    def test_pipeline_state_not_mutated_in_place(self):
        """run_pipeline must deep-copy state so the caller's dict isn't mutated.
        This prevents corrupted state if the pipeline fails mid-way and the
        caller saves the original data dict."""
        state = _fresh_pipeline_state()
        state["turn_counter"] = 5
        state["callback_ledger"]["open"] = [
            {"id": 1, "created_turn": 1, "original_text": "Test callback", "source_npc": "Kira"}
        ]
        state["callback_ledger"]["next_id"] = 2
        original_turn = state["turn_counter"]
        original_open_len = len(state["callback_ledger"]["open"])

        events_data = _make_events_response(
            callback_ops=[{"action": "resolve", "id": 1, "resolution_text": "Done"}]
        )
        mechanics_data = _make_mechanics_response()
        self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=state)

        # The ORIGINAL state dict should be completely unchanged
        assert state["turn_counter"] == original_turn
        assert len(state["callback_ledger"]["open"]) == original_open_len

    def test_events_uses_threshold_target(self):
        """Under threshold: all pairs included. Over threshold: trimmed to target."""
        provider = _make_mock_provider()
        client = MagicMock()

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()

        def mock_non_streaming(cl, params):
            return {"content": json.dumps(events_data), "reasoning": None,
                    "input_tokens": 1000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="text"),
            StreamEvent(event_type="done", usage={"content": "text", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        # Test 1: 30 pairs (under threshold=40) — all included
        branch_path = _make_branch_path(n_history_pairs=30)
        captured_messages = []
        original_build = build_events_messages

        def capturing_build(*args, **kwargs):
            result = original_build(*args, **kwargs)
            captured_messages.extend(result)
            return result

        with patch("pipeline.build_events_messages", side_effect=capturing_build):
            list(run_pipeline(
                provider=provider, client=client, username="test", project="proj",
                chat_name="chat", branch_path=branch_path,
                agent_instructions={"events": "", "mechanics": "", "narration": ""},
                agent_files={"events": "", "mechanics": "", "narration": ""},
                pipeline_state=None,
            ))

        history_msgs = [m for m in captured_messages if m["role"] in ("user", "assistant") and m != captured_messages[-1]]
        # All 30 pairs (60 messages) should be included
        assert len(history_msgs) == 60

        # Test 2: 50 pairs (over threshold=40) — trimmed to target=20
        branch_path = _make_branch_path(n_history_pairs=50)
        captured_messages = []

        with patch("pipeline.build_events_messages", side_effect=capturing_build):
            list(run_pipeline(
                provider=provider, client=client, username="test", project="proj",
                chat_name="chat", branch_path=branch_path,
                agent_instructions={"events": "", "mechanics": "", "narration": ""},
                agent_files={"events": "", "mechanics": "", "narration": ""},
                pipeline_state=None,
            ))

        history_msgs = [m for m in captured_messages if m["role"] in ("user", "assistant") and m != captured_messages[-1]]
        # Trimmed to 20 pairs (40 messages)
        assert len(history_msgs) == 40

    def test_no_scene_state_in_events_preserves_existing(self):
        """If Events doesn't return scene_state, existing state persists."""
        state = _fresh_pipeline_state()
        state["scene_state"] = {"location": "Old Place", "npcs_present": ["Someone"],
                                "active_tensions": [], "scene_trigger": "", "atmosphere": "",
                                "details": [], "pending_actions": []}
        events_data = _make_events_response()  # No scene_state key
        mechanics_data = _make_mechanics_response()
        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=state)

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        # scene_state should be unchanged since Events didn't provide one
        assert done_event[1].pipeline_state["scene_state"]["location"] == "Old Place"

    def test_legacy_chat_first_message_after_upgrade(self):
        """
        Simulate a legacy chat (flat pacing dict) hitting run_pipeline for the
        first time after the upgrade. Events should bootstrap all new structures
        and the saved state should be in the new nested format.
        """
        # This is what data["pipeline_state"] looks like in a real legacy chat
        legacy_state = {
            "episode": "Session 6: The Walls Have Eyes",
            "beat": "Shipboard pre-Lab Setup",
            "beat_responses": 7,
            "notes": "tension rising"
        }

        # Events bootstraps everything on first turn with new code
        events_data = _make_events_response(
            pacing={"episode": "Session 6: The Walls Have Eyes", "beat": "Shipboard pre-Lab Setup",
                    "beat_responses": 8, "notes": "continuing"},
            callback_ops=[
                {"action": "add", "original_text": "Sara promised to explain the interface incident", "source_npc": "Sara"},
                {"action": "add", "original_text": "Captain mentioned an inspection is coming", "source_npc": "Captain"},
            ],
            npc_memory_ops=[
                {"action": "add", "npc": "Lydia", "text": "Confided about interface incident", "quote": "I didn't want anyone to know.", "date": "Day 6", "impact": 4},
                {"action": "add", "npc": "Sara", "text": "Volunteered for therapy session", "date": "Day 5", "impact": 3},
            ],
            scene_state={
                "location": "Captain's Quarters",
                "npcs_present": ["Lydia", "Sara"],
                "active_tensions": ["Sara worried about safety protocol", "Lydia hiding something"],
                "scene_trigger": "Private meeting after incident",
                "atmosphere": "Quiet, tense",
                "details": ["Door is locked", "Intercom disabled"],
                "pending_actions": ["Sara about to explain what happened"]
            }
        )
        mechanics_data = _make_mechanics_response()

        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=legacy_state)
        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        state = result.pipeline_state

        # Verify full nested structure exists
        assert set(state.keys()) == {"pacing", "callback_ledger", "npc_memories", "scene_state", "character_states", "game_state", "hud_state", "combat", "ship_combat", "turn_counter"}

        # Turn counter should be 1 (migrated from 0 + increment)
        assert state["turn_counter"] == 1

        # Pacing should be updated from Events output
        assert state["pacing"]["beat_responses"] == 8

        # Callbacks bootstrapped
        assert len(state["callback_ledger"]["open"]) == 2
        assert state["callback_ledger"]["next_id"] == 3
        assert state["callback_ledger"]["open"][0]["original_text"] == "Sara promised to explain the interface incident"
        assert state["callback_ledger"]["open"][1]["source_npc"] == "Captain"

        # NPC memories bootstrapped
        assert "Lydia" in state["npc_memories"]
        assert "Sara" in state["npc_memories"]
        assert state["npc_memories"]["Lydia"][0]["tier"] == "high"  # impact 4
        assert state["npc_memories"]["Lydia"][0]["quote"] == "I didn't want anyone to know."
        assert state["npc_memories"]["Sara"][0]["tier"] == "moderate"  # impact 3

        # Scene state bootstrapped
        assert state["scene_state"]["location"] == "Captain's Quarters"
        assert state["scene_state"]["npcs_present"] == ["Lydia", "Sara"]
        assert len(state["scene_state"]["active_tensions"]) == 2
        assert state["scene_state"]["details"] == ["Door is locked", "Intercom disabled"]

        # injected_state snapshot should show the pre-Events state (migrated but before ops)
        injected = json.loads(result.injected_state)
        # The original pacing should be wrapped in the snapshot
        assert injected["pacing"]["episode"] == "Session 6: The Walls Have Eyes"
        assert injected["pacing"]["beat_responses"] == 7  # Before Events updated it
        assert injected["callback_ledger"]["open"] == []  # Empty before bootstrap
        assert injected["npc_memories"] == {}  # Empty before bootstrap
        assert injected["scene_state"] == {}  # Empty before bootstrap
        assert injected["turn_counter"] == 0  # Snapshot taken before route-conditional increment

    def test_legacy_chat_second_message_has_injections(self):
        """
        After a legacy chat's first message bootstrapped state, the second
        message should receive all injections in the Events user message.
        """
        # State as it would be saved after the first post-upgrade message
        state_after_first = {
            "pacing": {"episode": "Session 6", "beat": "Setup", "beat_responses": 8, "notes": ""},
            "callback_ledger": {
                "next_id": 3,
                "open": [
                    {"id": 1, "created_turn": 1, "original_text": "Sara promised to explain", "source_npc": "Sara"},
                    {"id": 2, "created_turn": 1, "original_text": "Captain mentioned inspection", "source_npc": "Captain"},
                ],
                "recently_resolved": []
            },
            "npc_memories": {
                "Lydia": [{"text": "Confided about incident", "quote": "I didn't want anyone to know.", "date": "Day 6", "impact": 4, "tier": "high", "turn_created": 1}],
                "Sara": [{"text": "Volunteered for therapy", "quote": None, "date": "Day 5", "impact": 3, "tier": "moderate", "turn_created": 1}],
            },
            "scene_state": {
                "location": "Captain's Quarters",
                "npcs_present": ["Lydia", "Sara"],
                "active_tensions": ["Sara worried about safety"],
                "scene_trigger": "Private meeting",
                "atmosphere": "Quiet, tense",
                "details": ["Door is locked"],
                "pending_actions": ["Sara about to explain"]
            },
            "turn_counter": 1
        }

        # On the second message, Events should resolve a callback and add a memory
        events_data = _make_events_response(
            pacing={"episode": "Session 6", "beat": "The Explanation", "beat_responses": 1, "notes": "moving forward"},
            callback_ops=[
                {"action": "resolve", "id": 1, "resolution_text": "Sara explained the interface malfunction"}
            ],
            npc_memory_ops=[
                {"action": "add", "npc": "Sara", "text": "Explained interface malfunction in detail", "date": "Day 6", "impact": 4}
            ],
            scene_state={
                "location": "Captain's Quarters",
                "npcs_present": ["Lydia", "Sara"],
                "active_tensions": ["Lydia processing the explanation"],
                "scene_trigger": "Sara explaining",
                "atmosphere": "Emotional, vulnerable",
                "details": ["Door is locked"],
                "pending_actions": []
            }
        )
        mechanics_data = _make_mechanics_response()

        events = self._run_pipeline_collect(events_data, mechanics_data, pipeline_state=state_after_first)
        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        state = result.pipeline_state

        # Turn 2
        assert state["turn_counter"] == 2

        # Callback 1 should be resolved, callback 2 still open
        assert len(state["callback_ledger"]["open"]) == 1
        assert state["callback_ledger"]["open"][0]["id"] == 2
        assert len(state["callback_ledger"]["recently_resolved"]) == 1
        assert state["callback_ledger"]["recently_resolved"][0]["resolution_text"] == "Sara explained the interface malfunction"

        # Sara should have 2 memories now, sorted by impact desc
        assert len(state["npc_memories"]["Sara"]) == 2
        assert state["npc_memories"]["Sara"][0]["tier"] == "high"  # impact 4 (sorted first)
        assert state["npc_memories"]["Sara"][1]["tier"] == "moderate"  # impact 3

        # Scene updated
        assert state["scene_state"]["atmosphere"] == "Emotional, vulnerable"

        # The injected state snapshot should show state BEFORE this turn's ops
        injected = json.loads(result.injected_state)
        assert injected["turn_counter"] == 1  # Snapshot taken before route-conditional increment
        assert len(injected["callback_ledger"]["open"]) == 2  # Before resolve
        assert len(injected["npc_memories"]["Sara"]) == 1  # Before add


# ============================================================
# Step 7: Debug Transcript Tests
# ============================================================

class TestGenerateDebugTranscript:
    def _make_chat_data(self, with_pipeline_state=True, with_ops=True):
        events_json = {
            "route": "mechanics",
            "pacing": {"episode": "Ep1", "beat": "Beat1", "beat_responses": 1, "notes": ""},
            "beats": ["attacked"],
            "player_action": "attack",
            "callbacks": [],
            "emotional_context": "tense",
            "character_states": {},
            "score_changes": [],
            "time_passed": "1 minute",
            "arc_label": None,
            "current_player": "Aedina",
            "next_player": "Orrophim",
            "next_player_prompt": "scene",
            "hud_state": {"date": "Day 1", "time": "1000", "location": "Tavern", "funds": "100gp", "trackables": None},
            "combat": None,
        }
        if with_ops:
            events_json["callback_ops"] = [{"action": "add", "original_text": "Promise", "source_npc": "Kira"}]
            events_json["npc_memory_ops"] = [{"action": "add", "npc": "Kira", "text": "Saved", "impact": 5, "date": "Day1"}]
            events_json["scene_state"] = {"location": "Tavern", "npcs_present": ["Kira"]}

        messages = [
            {"id": "sys", "parent_id": None, "role": "system", "content": "system"},
            {"id": "u1", "parent_id": "sys", "role": "user", "content": "I attack", "timestamp": "2025-01-01T10:00:00"},
            {
                "id": "a1", "parent_id": "u1", "role": "assistant",
                "content": "The guard falls.",
                "timestamp": "2025-01-01T10:00:05",
                "events_stage": json.dumps(events_json),
                "mechanics_stage": json.dumps({"route": "narration", "beats": []}),
                "reasoning": "[Events] Events reasoning here\n[Mechanics] Mechanics reasoning\n[Narration] Narration reasoning",
            },
        ]
        if with_pipeline_state:
            messages[2]["pipeline_state_injected"] = json.dumps({"pacing": {}, "turn_counter": 1}, indent=2)

        return {
            "messages": messages,
            "current_leaf_id": "a1",
        }

    def test_debug_transcript_includes_injected_state(self, tmp_path):
        chat_data = self._make_chat_data(with_pipeline_state=True)
        chat_path = str(tmp_path / "test_chat.json")

        generate_debug_transcript(chat_data, chat_path, "test_chat")

        debug_path = str(tmp_path / "test_chat_debug.txt")
        with open(debug_path) as f:
            content = f.read()

        # First turn shows as "initial" since there's no previous state
        assert "--- PIPELINE STATE (initial) ---" in content
        assert '"turn_counter": 1' in content

    def test_debug_transcript_includes_state_ops(self, tmp_path):
        chat_data = self._make_chat_data(with_ops=True)
        chat_path = str(tmp_path / "test_chat.json")

        generate_debug_transcript(chat_data, chat_path, "test_chat")

        debug_path = str(tmp_path / "test_chat_debug.txt")
        with open(debug_path) as f:
            content = f.read()

        assert "--- STATE OPS EXTRACTED ---" in content
        assert "callback_ops" in content
        assert "npc_memory_ops" in content
        assert "scene_state" in content

    def test_debug_transcript_no_injected_state(self, tmp_path):
        """Old messages without pipeline_state_injected should still work."""
        chat_data = self._make_chat_data(with_pipeline_state=False)
        chat_path = str(tmp_path / "test_chat.json")

        generate_debug_transcript(chat_data, chat_path, "test_chat")

        debug_path = str(tmp_path / "test_chat_debug.txt")
        with open(debug_path) as f:
            content = f.read()

        assert "--- PIPELINE STATE (initial) ---" not in content
        assert "--- PIPELINE STATE DELTA ---" not in content
        # Should still have events stage
        assert "--- EVENTS STAGE ---" in content

    def test_debug_transcript_no_ops_in_events(self, tmp_path):
        """Events without ops should not show STATE OPS section."""
        chat_data = self._make_chat_data(with_ops=False)
        chat_path = str(tmp_path / "test_chat.json")

        generate_debug_transcript(chat_data, chat_path, "test_chat")

        debug_path = str(tmp_path / "test_chat_debug.txt")
        with open(debug_path) as f:
            content = f.read()

        assert "--- STATE OPS EXTRACTED ---" not in content

    def test_debug_transcript_has_all_sections(self, tmp_path):
        chat_data = self._make_chat_data()
        chat_path = str(tmp_path / "test_chat.json")

        generate_debug_transcript(chat_data, chat_path, "test_chat")

        debug_path = str(tmp_path / "test_chat_debug.txt")
        with open(debug_path) as f:
            content = f.read()

        assert "PIPELINE DEBUG TRANSCRIPT" in content
        assert "[USER]" in content
        assert "[ASSISTANT]" in content
        assert "--- EVENTS STAGE ---" in content
        assert "--- EVENTS REASONING ---" in content
        assert "--- MECHANICS STAGE ---" in content
        assert "--- MECHANICS REASONING ---" in content
        assert "--- NARRATION REASONING ---" in content
        assert "--- FINAL OUTPUT ---" in content
        assert "The guard falls." in content


# ============================================================
# Constants Tests
# ============================================================

class TestConstants:
    def test_events_threshold_pairs(self):
        assert EVENTS_THRESHOLD_PAIRS == 40

    def test_events_target_pairs(self):
        assert EVENTS_TARGET_PAIRS == 20

    def test_narration_threshold_pairs(self):
        assert NARRATION_THRESHOLD_PAIRS == 40

    def test_narration_target_pairs(self):
        assert NARRATION_TARGET_PAIRS == 20

    def test_callback_resolved_retention(self):
        assert CALLBACK_RESOLVED_RETENTION == 20

    def test_npc_memory_tier_limits(self):
        assert NPC_MEMORY_TIER_LIMITS == {"high": 8, "moderate": 10, "flavor": 12}

    def test_npc_memory_max_per_npc(self):
        assert NPC_MEMORY_MAX_PER_NPC == 30


# ============================================================
# Edge Case Tests
# ============================================================

class TestEdgeCases:
    def test_callback_resolve_then_add_same_turn(self, populated_ledger):
        """Resolve existing and add new in same turn."""
        ops = [
            {"action": "resolve", "id": 1, "resolution_text": "Showed tunnels"},
            {"action": "add", "original_text": "New promise", "source_npc": "Kira"},
        ]
        result = apply_callback_ops(populated_ledger, ops, current_turn=20)
        open_ids = [cb["id"] for cb in result["open"]]
        assert 1 not in open_ids
        assert len(result["recently_resolved"]) == 1
        # New callback should have next available ID
        new_cb = [cb for cb in result["open"] if cb["original_text"] == "New promise"]
        assert len(new_cb) == 1
        assert new_cb[0]["id"] == 4  # next_id was 4

    def test_empty_npc_memory_add_no_npc(self):
        """Add with no NPC name should be skipped."""
        memories = {}
        ops = [{"action": "add", "text": "something", "date": "Day 1", "impact": 3}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert result == {}

    def test_scene_state_with_none_values(self):
        """Scene state with None values should get defaults."""
        scene = {"location": "X", "npcs_present": None, "atmosphere": None}
        result = apply_scene_state(scene)
        assert result["location"] == "X"
        # None should be replaced by default
        assert result["npcs_present"] is None  # apply_scene_state does .get() which returns None
        # This is actually fine - None will just mean no memories injected

    def test_build_scene_state_none_values_handled(self):
        """Scene state injection should handle None values gracefully."""
        scene = {"location": "Tavern", "npcs_present": None}
        result = build_scene_state_injection(scene)
        # Should not crash
        assert "[SCENE STATE]" in result

    def test_multiple_npc_memory_adds_same_npc(self):
        memories = {}
        ops = [
            {"action": "add", "npc": "Kira", "text": "First", "date": "D1", "impact": 3},
            {"action": "add", "npc": "Kira", "text": "Second", "date": "D1", "impact": 4},
        ]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert len(result["Kira"]) == 2
        # Sorted by impact desc: Second (4) before First (3)
        assert result["Kira"][0]["text"] == "Second"
        assert result["Kira"][1]["text"] == "First"

    def test_callback_ops_unknown_action_ignored(self, empty_ledger):
        """Unknown action should not crash."""
        ops = [{"action": "unknown_action", "foo": "bar"}]
        result = apply_callback_ops(empty_ledger, ops, current_turn=1)
        assert result["open"] == []

    def test_npc_memory_ops_unknown_action_ignored(self):
        """Unknown action should not crash."""
        memories = {}
        ops = [{"action": "unknown", "npc": "Kira"}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert result == {}

    def test_drop_targets_injection_order_not_chronological(self):
        """Regression: drops must target injection display order (impact desc),
        not chronological append order. The model sees memories sorted by impact
        in [NPC MEMORIES] blocks, so its drop indices refer to that order."""
        # Add memories in chronological order with varying impact
        memories = {}
        memories = apply_npc_memory_ops(memories, [
            {"action": "add", "npc": "Kira", "text": "Low-importance gossip", "date": "Day 1", "impact": 1},
            {"action": "add", "npc": "Kira", "text": "Moderate event at market", "date": "Day 2", "impact": 3},
            {"action": "add", "npc": "Kira", "text": "Life-saving moment in fire", "date": "Day 3", "impact": 5},
        ], current_turn=3)

        # After sort: [Life-saving(5), Moderate(3), Low-importance(1)]
        # The model sees index 0 = "Life-saving moment in fire"
        assert memories["Kira"][0]["text"] == "Life-saving moment in fire"

        # Model says "drop index 2" meaning the lowest-impact memory it sees
        memories = apply_npc_memory_ops(memories, [
            {"action": "drop", "npc": "Kira", "index": 2},
        ], current_turn=4)

        assert len(memories["Kira"]) == 2
        # The low-importance gossip (which was index 2 in injection order) should be gone
        remaining_texts = [m["text"] for m in memories["Kira"]]
        assert "Low-importance gossip" not in remaining_texts
        assert "Life-saving moment in fire" in remaining_texts
        assert "Moderate event at market" in remaining_texts

    def test_tier_limit_removes_lowest_impact_among_turn_ties(self):
        """Regression: when tier limit eviction has ties in turn_created,
        remove the lowest-impact memory first, not the highest."""
        # 8 high-tier memories, all created on turn 5 but with different impacts
        memories = {"Kira": [
            {"text": f"high{i}", "quote": None, "date": "D1",
             "impact": 4 + (i % 2), "tier": "high", "turn_created": 5}
            for i in range(8)
        ]}
        # high0: impact=4, high1: impact=5, high2: impact=4, high3: impact=5, etc.
        # Adding one more pushes past the tier limit of 8
        ops = [{"action": "add", "npc": "Kira", "text": "newest_high", "date": "D2", "impact": 5}]
        result = apply_npc_memory_ops(memories, ops, current_turn=10)
        high_mems = [m for m in result["Kira"] if m["tier"] == "high"]
        assert len(high_mems) <= NPC_MEMORY_TIER_LIMITS["high"]
        # The removed one should be impact=4 (lowest among ties), NOT impact=5
        removed_texts = {"high0", "high1", "high2", "high3", "high4", "high5", "high6", "high7", "newest_high"} - {m["text"] for m in result["Kira"]}
        for removed_text in removed_texts:
            # Every removed entry should be one with impact=4
            idx = int(removed_text.replace("high", "")) if removed_text.startswith("high") else -1
            if idx >= 0:
                assert idx % 2 == 0, f"Removed {removed_text} which has impact=5, should have removed impact=4"

    def test_memory_tier_none_impact(self):
        """_memory_tier should handle None impact gracefully."""
        assert _memory_tier(None) == "flavor"

    def test_memory_tier_string_impact(self):
        """_memory_tier should handle string impact gracefully."""
        assert _memory_tier("high") == "flavor"

    def test_memory_add_none_impact_defaults(self):
        """Adding a memory with impact=None should default to 1."""
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "test", "date": "D1", "impact": None}]
        result = apply_npc_memory_ops(memories, ops, current_turn=1)
        assert result["Kira"][0]["impact"] == 1
        assert result["Kira"][0]["tier"] == "flavor"

    def test_memory_impact_zero_gets_one_star_in_injection(self):
        """Impact=0 memories should still display at least one star."""
        memories = {"Kira": [
            {"text": "Zero impact thing", "quote": None, "date": "D1", "impact": 0, "tier": "flavor", "turn_created": 1}
        ]}
        scene = {"npcs_present": ["Kira"]}
        injection = build_npc_memories_injection(memories, scene)
        # Should have at least 1 star, not zero
        assert "\u2605" in injection

    def test_drop_with_missing_index_warns(self, populated_memories, caplog):
        """Drop op with no index field should warn, not crash."""
        ops = [{"action": "drop", "npc": "Kira"}]  # Missing 'index'
        with caplog.at_level(logging.WARNING):
            apply_npc_memory_ops(populated_memories, ops, current_turn=1)
        assert "None" in caplog.text or "out of bounds" in caplog.text

    def test_drop_and_add_same_npc_same_turn(self):
        """Drop and add ops for the same NPC in one turn should work correctly."""
        memories = {}
        memories = apply_npc_memory_ops(memories, [
            {"action": "add", "npc": "Kira", "text": "Old memory", "date": "D1", "impact": 2},
            {"action": "add", "npc": "Kira", "text": "Another old", "date": "D2", "impact": 3},
        ], current_turn=1)

        # Drop the lower-impact one (index 1 after sort) and add a new one
        result = apply_npc_memory_ops(memories, [
            {"action": "drop", "npc": "Kira", "index": 1},  # Drops "Old memory" (impact=2, sorted last)
            {"action": "add", "npc": "Kira", "text": "New important", "date": "D3", "impact": 5},
        ], current_turn=2)

        assert len(result["Kira"]) == 2
        texts = [m["text"] for m in result["Kira"]]
        assert "Old memory" not in texts
        assert "Another old" in texts
        assert "New important" in texts

    def test_migrate_old_format_does_not_mutate_input(self):
        """Migration of old flat format should not mutate the original dict."""
        original = {"episode": "Ep1", "beat": "B1", "beat_responses": 3, "notes": "x"}
        original_copy = copy.deepcopy(original)
        migrate_pipeline_state(original)
        assert original == original_copy

    def test_migrate_new_format_mutates_input(self):
        """Migration of new format with missing keys DOES mutate the input via setdefault.
        This is why run_pipeline deep-copies before calling migrate."""
        # New format but missing npc_memories and scene_state
        state = {"pacing": {"episode": "Ep1"}, "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []}}
        assert "npc_memories" not in state
        migrate_pipeline_state(state)
        # setdefault adds the missing keys to the original
        assert "npc_memories" in state
        assert "scene_state" in state
        assert "turn_counter" in state


# ============================================================
# Round-Trip: Op-Application → Injection Builder
# ============================================================

class TestOpToInjectionRoundTrip:
    """Verify that op-application output is correctly consumed by injection builders."""

    def test_callback_add_resolve_renders_correctly(self):
        """Callbacks created by add ops should render in injection, resolved ones in resolved section."""
        ledger = {"next_id": 1, "open": [], "recently_resolved": []}

        # Turn 1: add two callbacks
        ledger = apply_callback_ops(ledger, [
            {"action": "add", "original_text": "Kira will meet at the docks at midnight", "source_npc": "Kira"},
            {"action": "add", "original_text": "Strange signal detected from sector 7", "source_npc": None},
        ], current_turn=1)

        injection = build_callback_injection(ledger)
        assert "[CALLBACK LEDGER]" in injection
        assert "OPEN:" in injection
        assert '#1 (turn 1, Kira): "Kira will meet at the docks at midnight"' in injection
        assert '#2 (turn 1, null): "Strange signal detected from sector 7"' in injection
        assert "RECENTLY RESOLVED" not in injection  # No resolved yet

        # Turn 5: resolve callback 1
        ledger = apply_callback_ops(ledger, [
            {"action": "resolve", "id": 1, "resolution_text": "Met Kira at the docks, she had intel"},
        ], current_turn=5)

        injection = build_callback_injection(ledger)
        assert "OPEN:" in injection
        # Callback 1 should NOT be in open section
        open_section = injection.split("RECENTLY RESOLVED")[0]
        assert "#1" not in open_section
        # Callback 2 should still be open
        assert '#2 (turn 1, null)' in open_section
        # Callback 1 should be in resolved section
        assert "RECENTLY RESOLVED:" in injection
        assert "#1" in injection.split("RECENTLY RESOLVED")[1]
        assert "Met Kira at the docks" in injection

    def test_callback_update_renders_updated_text(self):
        """Updated callback text should appear in the injection."""
        ledger = {"next_id": 1, "open": [], "recently_resolved": []}
        ledger = apply_callback_ops(ledger, [
            {"action": "add", "original_text": "Vex promised parts", "source_npc": "Vex"},
        ], current_turn=1)
        ledger = apply_callback_ops(ledger, [
            {"action": "update", "id": 1, "fields": {"original_text": "Vex promised parts — now says price doubled"}},
        ], current_turn=3)

        injection = build_callback_injection(ledger)
        assert "price doubled" in injection
        assert "Vex promised parts —" in injection  # Original prefix still there

    def test_npc_memory_add_renders_with_stars_and_quote(self):
        """Memories created by add ops should render with correct star count and quotes."""
        memories = {}
        memories = apply_npc_memory_ops(memories, [
            {"action": "add", "npc": "Kira", "text": "Saved Kira from explosion", "quote": "I owe you my life.", "date": "Day 3", "impact": 5},
            {"action": "add", "npc": "Kira", "text": "Shared a joke about the captain", "date": "Day 4", "impact": 1},
        ], current_turn=5)

        scene = {"npcs_present": ["Kira"]}
        injection = build_npc_memories_injection(memories, scene)

        assert "[NPC MEMORIES: Kira]" in injection
        assert "[/NPC MEMORIES]" in injection

        lines = [l for l in injection.split("\n") if l.startswith("[") and "] [" in l]
        # Should be sorted by impact desc — 5-star first
        assert "\u2605\u2605\u2605\u2605\u2605" in lines[0]  # 5 stars
        assert '| "I owe you my life."' in lines[0]
        assert "\u2605" in lines[1]  # 1 star
        assert '| "' not in lines[1]  # No quote for impact-1 memory

    def test_npc_memory_drop_then_inject(self):
        """After dropping a memory, the injection should reflect the remaining ones."""
        memories = {}
        memories = apply_npc_memory_ops(memories, [
            {"action": "add", "npc": "Vex", "text": "First meeting at bar", "date": "Day 1", "impact": 2},
            {"action": "add", "npc": "Vex", "text": "Betrayed the party", "quote": "Nothing personal.", "date": "Day 5", "impact": 5},
            {"action": "add", "npc": "Vex", "text": "Apologized later", "date": "Day 6", "impact": 3},
        ], current_turn=6)

        # After sort: [Betrayed(5), Apologized(3), First(2)]
        # Drop index 0 = "Betrayed the party" (highest impact)
        memories = apply_npc_memory_ops(memories, [
            {"action": "drop", "npc": "Vex", "index": 0},
        ], current_turn=7)

        scene = {"npcs_present": ["Vex"]}
        injection = build_npc_memories_injection(memories, scene)
        assert "Betrayed the party" not in injection  # Dropped (was index 0 = highest impact)
        assert "Apologized later" in injection
        assert "First meeting at bar" in injection

    def test_scene_state_applied_then_injected(self):
        """Scene state from apply_scene_state should render correctly in injection."""
        scene = apply_scene_state({
            "location": "The Rusted Anchor",
            "npcs_present": ["Kira", "Yael"],
            "active_tensions": ["Yael suspects Kira"],
            "scene_trigger": "Emergency meeting",
            "atmosphere": "Tense, rain outside",
            "details": ["Disguise active", "Speeder in alley"],
            "pending_actions": ["Waiting for signal"]
        })

        injection = build_scene_state_injection(scene)
        assert "Location: The Rusted Anchor" in injection
        assert "NPCs Present: Kira, Yael" in injection
        assert "Active Tensions: Yael suspects Kira" in injection
        assert "Scene Trigger: Emergency meeting" in injection
        assert "Atmosphere: Tense, rain outside" in injection
        assert "Details: Disguise active, Speeder in alley" in injection
        assert "Pending Actions: Waiting for signal" in injection

    def test_scene_state_controls_memory_scoping(self):
        """NPCs in apply_scene_state output should control which memories get injected."""
        memories = {}
        memories = apply_npc_memory_ops(memories, [
            {"action": "add", "npc": "Kira", "text": "Important moment", "date": "Day 1", "impact": 4},
            {"action": "add", "npc": "Vex", "text": "Also important", "date": "Day 1", "impact": 4},
            {"action": "add", "npc": "Yael", "text": "Background NPC moment", "date": "Day 1", "impact": 3},
        ], current_turn=1)

        # Scene only has Kira and Yael — Vex is absent
        scene = apply_scene_state({
            "location": "Tavern",
            "npcs_present": ["Kira", "Yael"],
        })

        injection = build_npc_memories_injection(memories, scene)
        assert "[NPC MEMORIES: Kira]" in injection
        assert "[NPC MEMORIES: Yael]" in injection
        assert "Vex" not in injection  # Not in scene

    def test_full_state_through_build_events_messages(self):
        """
        Simulate a full turn: migrate → apply ops → build_events_messages.
        Verify the Events user message contains all expected injection content.
        """
        state = migrate_pipeline_state(None)
        state["turn_counter"] += 1

        state["pacing"] = {"episode": "Ep1", "beat": "The Arrival", "beat_responses": 3, "notes": ""}
        state["callback_ledger"] = apply_callback_ops(
            state["callback_ledger"],
            [{"action": "add", "original_text": "Merchant promised rare item by morning", "source_npc": "Merchant Giles"}],
            state["turn_counter"]
        )
        state["npc_memories"] = apply_npc_memory_ops(
            state["npc_memories"],
            [{"action": "add", "npc": "Captain Reva", "text": "Gave the party a stern warning about the frontier",
              "quote": "Don't come crying to me.", "date": "Day 2", "impact": 3}],
            state["turn_counter"]
        )
        state["scene_state"] = apply_scene_state({
            "location": "Frontier Outpost, main hall",
            "npcs_present": ["Captain Reva", "Merchant Giles"],
            "active_tensions": ["Reva doesn't trust the party"],
            "scene_trigger": "Arrived seeking supplies",
            "atmosphere": "Cold, suspicious guards",
            "details": ["Weapons confiscated at gate"],
            "pending_actions": []
        })

        messages = build_events_messages(
            system_prompt="You are Events.",
            history_messages=[{"role": "user", "content": "We enter the outpost"}, {"role": "assistant", "content": "..."}],
            user_message={"role": "user", "content": "I ask Reva about the frontier dangers"},
            pipeline_state=state,
        )

        user_content = messages[-1]["content"]

        # Pacing state
        assert "[PIPELINE STATE]" in user_content
        assert "The Arrival" in user_content

        # Callback ledger — real content from apply_callback_ops
        assert "[CALLBACK LEDGER]" in user_content
        assert "Merchant promised rare item by morning" in user_content
        assert "Merchant Giles" in user_content

        # NPC memories — only Captain Reva should appear (she's in scene)
        assert "[NPC MEMORIES: Captain Reva]" in user_content
        assert "stern warning" in user_content
        assert '"Don\'t come crying to me."' in user_content
        assert "\u2605\u2605\u2605" in user_content  # impact 3 = 3 stars
        # Merchant Giles has no memories, so no memory block for them
        assert "[NPC MEMORIES: Merchant Giles]" not in user_content

        # Scene state
        assert "[SCENE STATE]" in user_content
        assert "Frontier Outpost" in user_content
        assert "Captain Reva, Merchant Giles" in user_content
        assert "Weapons confiscated at gate" in user_content

        # Context updates removed — should NOT appear
        assert "[CONTEXT UPDATES]" not in user_content

        # User message at the end
        assert user_content.endswith("I ask Reva about the frontier dangers")


# ============================================================
# E2E: Verify Events Agent Receives Correct Injections
# ============================================================

class TestEventsReceivesInjections:
    """Verify that run_pipeline passes correctly injected messages to the Events API call."""

    def test_events_api_call_contains_injections(self):
        """
        Run pipeline with populated state, capture the messages passed to
        build_pipeline_request for Events, verify injection content.
        """
        state = {
            "pacing": {"episode": "Ep3", "beat": "Confrontation", "beat_responses": 2, "notes": ""},
            "callback_ledger": {
                "next_id": 3,
                "open": [
                    {"id": 1, "created_turn": 1, "original_text": "Kira promised the codes", "source_npc": "Kira"},
                    {"id": 2, "created_turn": 3, "original_text": "Bounty hunter tracking party", "source_npc": None},
                ],
                "recently_resolved": [
                    {"id": 0, "created_turn": 0, "original_text": "Old resolved", "source_npc": "X", "resolved_turn": 4, "resolution_text": "Done"},
                ]
            },
            "npc_memories": {
                "Kira": [
                    {"text": "Saved her in warehouse", "quote": "I owe you.", "date": "Day 3", "impact": 5, "tier": "high", "turn_created": 3},
                ],
                "Vex": [
                    {"text": "Delivered parts", "quote": None, "date": "Day 7", "impact": 3, "tier": "moderate", "turn_created": 7},
                ]
            },
            "scene_state": {
                "location": "Docking Bay 7",
                "npcs_present": ["Kira"],  # Vex NOT present
                "active_tensions": ["Guards approaching"],
                "scene_trigger": "Alarm tripped",
                "atmosphere": "Red emergency lights, klaxons",
                "details": ["Cargo loaded"],
                "pending_actions": ["Kira starting engines"]
            },
            "turn_counter": 5
        }

        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history_pairs=5)

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()

        captured_build_calls = []
        original_build_pipeline_request = provider.build_pipeline_request

        def capturing_build(**kwargs):
            captured_build_calls.append(kwargs)
            return {}

        provider.build_pipeline_request = capturing_build

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": json.dumps(events_data), "reasoning": None, "input_tokens": 1000,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                return {"content": json.dumps(mechanics_data), "reasoning": None, "input_tokens": 500,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="narrative"),
            StreamEvent(event_type="done", usage={"content": "narrative", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        list(run_pipeline(
            provider=provider, client=client, username="test", project="proj",
            chat_name="chat", branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=state,
        ))

        # First build_pipeline_request call should be for Events
        assert len(captured_build_calls) >= 1
        events_call = captured_build_calls[0]
        assert events_call["stage_name"] == "events"

        events_messages = events_call["messages"]
        # Find the user message (last one)
        user_msg = events_messages[-1]
        assert user_msg["role"] == "user"
        content = user_msg["content"]

        # Verify pacing injection
        assert "[PIPELINE STATE]" in content
        assert "Confrontation" in content

        # Verify callback ledger injection
        assert "[CALLBACK LEDGER]" in content
        assert "Kira promised the codes" in content
        assert "Bounty hunter tracking party" in content
        assert "RECENTLY RESOLVED" in content
        assert "Old resolved" in content

        # Verify NPC memories — only Kira (she's in scene), NOT Vex
        assert "[NPC MEMORIES: Kira]" in content
        assert "Saved her in warehouse" in content
        assert '"I owe you."' in content
        assert "\u2605\u2605\u2605\u2605\u2605" in content  # 5 stars
        assert "[NPC MEMORIES: Vex]" not in content  # Vex not in scene

        # Verify scene state
        assert "[SCENE STATE]" in content
        assert "Docking Bay 7" in content
        assert "NPCs Present: Kira" in content
        assert "Red emergency lights" in content
        assert "Kira starting engines" in content

        # Verify history is 20 pairs max (we only have 5 pairs here)
        history_msgs = [m for m in events_messages if m["role"] in ("user", "assistant") and m != user_msg]
        assert len(history_msgs) == 10  # 5 pairs

        # Verify turn counter was incremented in the snapshot
        assert '"turn_counter": 6' in content or "turn_counter" not in content  # pacing doesn't have it, but state does in injection

    def test_events_receives_empty_injections_on_fresh_state(self):
        """With no prior state, Events should get only the user message, no injection blocks."""
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history_pairs=3)

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()

        captured_build_calls = []

        def capturing_build(**kwargs):
            captured_build_calls.append(kwargs)
            return {}

        provider.build_pipeline_request = capturing_build

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": json.dumps(events_data), "reasoning": None, "input_tokens": 1000,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                return {"content": json.dumps(mechanics_data), "reasoning": None, "input_tokens": 500,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="text"),
            StreamEvent(event_type="done", usage={"content": "text", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        list(run_pipeline(
            provider=provider, client=client, username="test", project="proj",
            chat_name="chat", branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None,  # Fresh — no prior state
        ))

        events_call = captured_build_calls[0]
        assert events_call["stage_name"] == "events"
        user_msg = events_call["messages"][-1]
        content = user_msg["content"]

        # No injection blocks for callback/memory/pacing on fresh state
        assert "[CALLBACK LEDGER]" not in content
        assert "[NPC MEMORIES:" not in content
        assert "[PIPELINE STATE]" not in content
        # Bootstrap markers should be present for scene and character states
        assert "[SCENE STATE]" in content
        assert "[CHARACTER STATES]" in content
        assert "(empty" in content
        assert "I attack the guard" in content


    def test_pipeline_state_preserved_across_turns(self):
        """Simulate two turns to verify state accumulates."""
        state = None

        # Turn 1: migrate from None, add callback + memory + scene
        state = migrate_pipeline_state(state)
        state["turn_counter"] += 1

        state["callback_ledger"] = apply_callback_ops(
            state["callback_ledger"],
            [{"action": "add", "original_text": "Promise", "source_npc": "Kira"}],
            state["turn_counter"]
        )
        state["npc_memories"] = apply_npc_memory_ops(
            state["npc_memories"],
            [{"action": "add", "npc": "Kira", "text": "Met at tavern", "date": "Day 1", "impact": 3}],
            state["turn_counter"]
        )
        state["scene_state"] = apply_scene_state({
            "location": "Tavern", "npcs_present": ["Kira"]
        })

        assert state["turn_counter"] == 1
        assert len(state["callback_ledger"]["open"]) == 1
        assert len(state["npc_memories"]["Kira"]) == 1
        assert state["scene_state"]["location"] == "Tavern"

        # Turn 2: migrate (already new format), add another callback, resolve first
        state = migrate_pipeline_state(state)
        state["turn_counter"] += 1

        state["callback_ledger"] = apply_callback_ops(
            state["callback_ledger"],
            [
                {"action": "add", "original_text": "Second promise", "source_npc": "Vex"},
                {"action": "resolve", "id": 1, "resolution_text": "Kira showed tunnels"},
            ],
            state["turn_counter"]
        )
        state["npc_memories"] = apply_npc_memory_ops(
            state["npc_memories"],
            [{"action": "add", "npc": "Kira", "text": "Explored tunnels together", "date": "Day 2", "impact": 4}],
            state["turn_counter"]
        )
        state["scene_state"] = apply_scene_state({
            "location": "Underground tunnels", "npcs_present": ["Kira", "Vex"]
        })

        assert state["turn_counter"] == 2
        assert len(state["callback_ledger"]["open"]) == 1  # Only second promise
        assert state["callback_ledger"]["open"][0]["original_text"] == "Second promise"
        assert len(state["callback_ledger"]["recently_resolved"]) == 1
        assert len(state["npc_memories"]["Kira"]) == 2
        assert state["scene_state"]["location"] == "Underground tunnels"
        assert state["scene_state"]["npcs_present"] == ["Kira", "Vex"]

        # Verify injections work with accumulated state
        cb_inj = build_callback_injection(state["callback_ledger"])
        assert "Second promise" in cb_inj
        assert "RECENTLY RESOLVED" in cb_inj
        assert "Kira showed tunnels" in cb_inj

        mem_inj = build_npc_memories_injection(state["npc_memories"], state["scene_state"])
        assert "[NPC MEMORIES: Kira]" in mem_inj
        assert "Explored tunnels together" in mem_inj

        scene_inj = build_scene_state_injection(state["scene_state"])
        assert "Underground tunnels" in scene_inj
        assert "Kira, Vex" in scene_inj


# ============================================================
# Character States Tests
# ============================================================

class TestBuildCharacterStatesInjection:
    def test_empty_returns_empty(self):
        result = build_character_states_injection({})
        assert "[CHARACTER STATES]" in result
        assert "(empty" in result
        assert "[/CHARACTER STATES]" in result

    def test_populated_structured_format(self):
        cs = {
            "Aedina": {"state": "45/50 HP, 2/3 spell slots", "last_updated": 5},
            "Orrophim": {"state": "38/38 HP, rage 2/3", "last_updated": 5},
        }
        result = build_character_states_injection(cs)
        assert result.startswith("[CHARACTER STATES]")
        assert result.endswith("[/CHARACTER STATES]")
        assert "Aedina: 45/50 HP, 2/3 spell slots" in result
        assert "Orrophim: 38/38 HP, rage 2/3" in result
        # Should NOT expose last_updated to the model
        assert "last_updated" not in result

    def test_single_character(self):
        cs = {"Kira": {"state": "full HP", "last_updated": 1}}
        result = build_character_states_injection(cs)
        lines = result.split("\n")
        assert len(lines) == 3  # header, one entry, footer
        assert lines[1] == "Kira: full HP"

    def test_flat_string_fallback(self):
        """Legacy flat string entries still render correctly."""
        cs = {"Aedina": "50 HP"}
        result = build_character_states_injection(cs)
        assert "Aedina: 50 HP" in result


class TestCharacterStatesMigration:
    def test_fresh_has_character_states(self):
        state = _fresh_pipeline_state()
        assert "character_states" in state
        assert state["character_states"] == {}

    def test_none_migration_has_character_states(self):
        state = migrate_pipeline_state(None)
        assert "character_states" in state
        assert state["character_states"] == {}

    def test_old_flat_migration_has_character_states(self):
        old = {"episode": "Ep1", "beat": "Opening", "beat_responses": 1, "notes": ""}
        state = migrate_pipeline_state(old)
        assert "character_states" in state
        assert state["character_states"] == {}

    def test_new_format_missing_character_states(self):
        """Existing new-format state without character_states gets it added."""
        state = {
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "turn_counter": 5,
        }
        result = migrate_pipeline_state(state)
        assert "character_states" in result
        assert result["character_states"] == {}
        assert result["turn_counter"] == 5  # preserved

    def test_existing_character_states_preserved(self):
        """Existing character_states in new-format state are kept (migrated to structured)."""
        state = {
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {"Aedina": "50 HP"},
            "turn_counter": 3,
        }
        result = migrate_pipeline_state(state)
        # Flat string migrated to structured format
        assert result["character_states"]["Aedina"]["data"] == {"summary": "50 HP"}
        assert result["character_states"]["Aedina"]["last_updated"] == 3

    def test_already_structured_character_states_unchanged(self):
        """Already-structured character_states are not double-wrapped."""
        state = {
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {"Aedina": {"data": {"summary": "50 HP"}, "last_updated": 3}},
            "turn_counter": 5,
        }
        result = migrate_pipeline_state(state)
        assert result["character_states"]["Aedina"] == {"data": {"summary": "50 HP"}, "last_updated": 3}


class TestCharacterStatesE2E:
    """E2E test verifying Mechanics' character_states is saved to pipeline_state."""

    def test_mechanics_character_states_saved(self):
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history_pairs=3)

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response(
            character_states={"Aedina": "42/50 HP, 1/3 spell slots", "Orrophim": "38/38 HP, rage 1/3"}
        )

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": json.dumps(events_data), "reasoning": None, "input_tokens": 1000,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                return {"content": json.dumps(mechanics_data), "reasoning": None, "input_tokens": 500,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="narrative"),
            StreamEvent(event_type="done", usage={"content": "narrative", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        events = list(run_pipeline(
            provider=provider, client=client, username="test", project="proj",
            chat_name="chat", branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None,
        ))

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        cs = result.pipeline_state["character_states"]
        assert cs["Aedina"]["data"] == {"summary": "42/50 HP, 1/3 spell slots"}
        assert cs["Aedina"]["last_updated"] == 1
        assert cs["Orrophim"]["data"] == {"summary": "38/38 HP, rage 1/3"}
        assert cs["Orrophim"]["last_updated"] == 1

    def test_mechanics_without_character_states_preserves_existing(self):
        """When Mechanics doesn't output character_states, existing state persists."""
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history_pairs=3)

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()  # No character_states

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": json.dumps(events_data), "reasoning": None, "input_tokens": 1000,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                return {"content": json.dumps(mechanics_data), "reasoning": None, "input_tokens": 500,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="text"),
            StreamEvent(event_type="done", usage={"content": "text", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        existing_state = {
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {"Aedina": {"data": {"summary": "50/50 HP"}, "last_updated": 2}},
            "turn_counter": 2,
        }

        events = list(run_pipeline(
            provider=provider, client=client, username="test", project="proj",
            chat_name="chat", branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=existing_state,
        ))

        done_event = [e for e in events if e[0] == "pipeline_done"][0]
        result = done_event[1]
        # Existing character_states should persist when Mechanics doesn't output them
        cs = result.pipeline_state["character_states"]
        assert cs["Aedina"]["data"] == {"summary": "50/50 HP"}
        assert cs["Aedina"]["last_updated"] == 2  # Not updated this turn

    def test_character_states_injected_into_events(self):
        """Verify character_states are injected into Events messages."""
        state = {
            "pacing": {"episode": "Ep1", "beat": "B1", "beat_responses": 1, "notes": ""},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {"Aedina": {"state": "45/50 HP, 2/3 slots", "last_updated": 3}},
            "turn_counter": 3,
        }

        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(n_history_pairs=2)

        events_data = _make_events_response()
        mechanics_data = _make_mechanics_response()

        captured_build_calls = []
        def capturing_build(**kwargs):
            captured_build_calls.append(kwargs)
            return {}

        provider.build_pipeline_request = capturing_build

        call_count = [0]
        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": json.dumps(events_data), "reasoning": None, "input_tokens": 1000,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100}
            else:
                return {"content": json.dumps(mechanics_data), "reasoning": None, "input_tokens": 500,
                        "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="text"),
            StreamEvent(event_type="done", usage={"content": "text", "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 50, "reasoning_tokens": 0})
        ])

        list(run_pipeline(
            provider=provider, client=client, username="test", project="proj",
            chat_name="chat", branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=state,
        ))

        events_call = captured_build_calls[0]
        assert events_call["stage_name"] == "events"
        user_msg = events_call["messages"][-1]
        content = user_msg["content"]

        assert "[CHARACTER STATES]" in content
        assert "Aedina: 45/50 HP, 2/3 slots" in content
        assert "[/CHARACTER STATES]" in content


class TestApplyCharacterStates:
    """Tests for apply_character_states merge + TTL pruning."""

    def test_merge_into_empty(self):
        existing = {}
        result = apply_character_states(existing, {"Aedina": "50 HP"}, current_turn=1)
        assert result["Aedina"] == {"data": {"summary": "50 HP"}, "last_updated": 1}

    def test_merge_updates_existing(self):
        existing = {"Aedina": {"data": {"summary": "50 HP"}, "last_updated": 1}}
        result = apply_character_states(existing, {"Aedina": "42 HP"}, current_turn=2)
        assert result["Aedina"] == {"data": {"summary": "42 HP"}, "last_updated": 2}

    def test_merge_preserves_untouched(self):
        existing = {
            "Aedina": {"data": {"summary": "50 HP"}, "last_updated": 5},
            "Orrophim": {"data": {"summary": "38 HP"}, "last_updated": 5},
        }
        result = apply_character_states(existing, {"Aedina": "42 HP"}, current_turn=6)
        assert result["Aedina"] == {"data": {"summary": "42 HP"}, "last_updated": 6}
        assert result["Orrophim"] == {"data": {"summary": "38 HP"}, "last_updated": 5}

    def test_merge_adds_new_character(self):
        existing = {"Aedina": {"data": {"summary": "50 HP"}, "last_updated": 5}}
        result = apply_character_states(existing, {"Vex": "68 HP, AC 16"}, current_turn=6)
        assert "Aedina" in result
        assert result["Vex"] == {"data": {"summary": "68 HP, AC 16"}, "last_updated": 6}

    def test_ttl_prunes_stale(self):
        existing = {
            "Aedina": {"data": {"summary": "50 HP"}, "last_updated": 5},
            "Old Guard": {"data": {"summary": "dead"}, "last_updated": 1},
        }
        # Turn 1 + TTL(150) + 1 = 152 — Old Guard should be pruned
        result = apply_character_states(existing, {"Aedina": "45 HP"}, current_turn=152)
        assert "Aedina" in result
        assert "Old Guard" not in result

    def test_ttl_keeps_within_window(self):
        existing = {
            "Aedina": {"data": {"summary": "50 HP"}, "last_updated": 5},
            "Guard": {"data": {"summary": "68 HP"}, "last_updated": 5},
        }
        # Turn 155 — Guard updated at 5, TTL is 150, so 155 - 5 = 150 which is NOT > 150
        result = apply_character_states(existing, {}, current_turn=155)
        assert "Guard" in result
        # Turn 156 — now 156 - 5 = 151 > 150, should be pruned
        result = apply_character_states(result, {}, current_turn=156)
        assert "Guard" not in result

    def test_ttl_constant_value(self):
        assert CHARACTER_STATE_TTL == 150

    def test_merge_multiple_characters(self):
        existing = {}
        mechanics_output = {
            "Aedina": "50/50 HP, 3/3 spell slots",
            "Orrophim": "38/38 HP, rage 3/3",
            "Kira": "68/68 HP, AC 16",
        }
        result = apply_character_states(existing, mechanics_output, current_turn=1)
        assert len(result) == 3
        for name in mechanics_output:
            assert result[name]["data"] == {"summary": mechanics_output[name]}
            assert result[name]["last_updated"] == 1


# ============================================================
# HUD State Tests
# ============================================================

class TestScopeHudFunds:
    """Test the extracted scope_hud_funds helper."""

    def test_dict_funds_filtered_to_present(self):
        hud = {"date": "Day 5", "funds": {"Aedina": "32 gp", "Orrophim": "18 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": ["Aedina", "Orrophim"]}
        chars = {"Aedina": {}, "Orrophim": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"Aedina": "32 gp", "Orrophim": "18 gp"}
        assert result["date"] == "Day 5"  # Other keys preserved

    def test_npc_funds_kept_when_present(self):
        hud = {"funds": {"Aedina": "32 gp", "Squire Tam": "8 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": ["Aedina"], "npcs_present": ["Squire Tam"]}
        chars = {"Aedina": {}, "Squire Tam": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"Aedina": "32 gp", "Squire Tam": "8 gp"}

    def test_non_character_funds_pass_through(self):
        hud = {"funds": {"The Rustbucket": "1,200 cr", "Aedina": "32 cr", "Vex": "5 cr"}}
        scene = {"pcs_present": ["Aedina"]}
        chars = {"Aedina": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"The Rustbucket": "1,200 cr", "Aedina": "32 cr"}

    def test_string_funds_unchanged(self):
        hud = {"funds": "50 gp party fund"}
        scene = {"pcs_present": ["Aedina"]}
        chars = {"Aedina": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == "50 gp party fund"

    def test_no_pcs_present_unchanged(self):
        hud = {"funds": {"Aedina": "32 gp", "Orrophim": "18 gp"}}
        scene = {"pcs_present": []}
        chars = {"Aedina": {}, "Orrophim": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"Aedina": "32 gp", "Orrophim": "18 gp"}

    def test_npc_only_scene_filters_to_present_npcs(self):
        hud = {"funds": {"Aedina": "32 gp", "Squire Tam": "8 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": [], "npcs_present": ["Squire Tam"]}
        chars = {"Aedina": {}, "Squire Tam": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"Squire Tam": "8 gp"}

    def test_empty_hud_state(self):
        result = scope_hud_funds({}, {}, {})
        assert result == {}

    def test_no_funds_key(self):
        hud = {"date": "Day 5", "time": "1400"}
        result = scope_hud_funds(hud, {"pcs_present": ["Aedina"]}, {"Aedina": {}})
        assert result == hud

    def test_original_hud_not_mutated(self):
        hud = {"funds": {"Aedina": "32 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": ["Aedina"]}
        chars = {"Aedina": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        # Original should still have Vex
        assert "Vex" in hud["funds"]
        assert "Vex" not in result["funds"]

    def test_unhashable_scene_entries_ignored(self):
        hud = {"funds": {"Aedina": "32 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": [{"bad": 1}, "Aedina"], "npcs_present": [None]}
        chars = {"Aedina": {}, "Vex": {}}
        result = scope_hud_funds(hud, scene, chars)
        assert result["funds"] == {"Aedina": "32 gp"}


class TestDeriveFundsFromShipCredits:
    def test_non_dict_ship_ignored(self):
        hud = {"date": "Day 5"}
        result = derive_funds_from_ship_credits(hud, {"ship": True})
        assert result == hud


class TestBuildHudStateInjection:
    """Test build_hud_state_injection rendering."""

    def test_empty_state_returns_empty(self):
        assert build_hud_state_injection({}, {}, {}) == ""

    def test_populated_renders_all_fields(self):
        hud = {
            "date": "Day 5",
            "time": "1430",
            "location": "The Rusted Anchor",
            "funds": "97,572 cr"
        }
        result = build_hud_state_injection(hud, {}, {})
        assert "[HUD STATE]" in result
        assert "[/HUD STATE]" in result
        assert "Date: Day 5" in result
        assert "Time: 1430" in result
        assert "Location: The Rusted Anchor" in result
        assert "Funds: 97,572 cr" in result

    def test_dict_funds_rendered(self):
        hud = {
            "date": "Day 5",
            "funds": {"Aedina": "32 gp", "Orrophim": "18 gp"}
        }
        result = build_hud_state_injection(hud, {}, {})
        assert "Funds: Aedina: 32 gp, Orrophim: 18 gp" in result

    def test_dict_funds_scene_scoped(self):
        hud = {"funds": {"Aedina": "32 gp", "Vex": "5 gp"}}
        scene = {"pcs_present": ["Aedina"]}
        chars = {"Aedina": {}, "Vex": {}}
        result = build_hud_state_injection(hud, scene, chars)
        assert "Aedina: 32 gp" in result
        assert "Vex" not in result

    def test_trackables_rendered(self):
        hud = {
            "date": "Day 5",
            "trackables": {"Ship Fuel": "72%", "Railgun Ammo": "14/20"}
        }
        result = build_hud_state_injection(hud, {}, {})
        assert "Ship Fuel: 72%" in result
        assert "Railgun Ammo: 14/20" in result

    def test_null_trackables_omitted(self):
        hud = {"date": "Day 5", "trackables": None}
        result = build_hud_state_injection(hud, {}, {})
        assert "trackables" not in result.lower()

    def test_partial_fields(self):
        hud = {"location": "Tavern"}
        result = build_hud_state_injection(hud, {}, {})
        assert "[HUD STATE]" in result
        assert "Location: Tavern" in result
        assert "Date" not in result
        assert "Time" not in result


class TestApplySingleAgentHudState:
    """Test that apply_single_agent_state_updates persists hud_state."""

    def test_hud_state_persisted(self):
        ps = _fresh_pipeline_state()
        parsed = {
            "hud_state": {"date": "Day 5", "time": "1430", "location": "Tavern", "funds": "50 gp"}
        }
        apply_single_agent_state_updates(ps, parsed, current_turn=1)
        assert ps["hud_state"] == parsed["hud_state"]

    def test_hud_state_not_overwritten_when_absent(self):
        ps = _fresh_pipeline_state()
        ps["hud_state"] = {"date": "Day 3", "time": "0900"}
        parsed = {}
        apply_single_agent_state_updates(ps, parsed, current_turn=2)
        assert ps["hud_state"] == {"date": "Day 3", "time": "0900"}

    def test_hud_state_replaced_not_merged(self):
        ps = _fresh_pipeline_state()
        ps["hud_state"] = {"date": "Day 3", "time": "0900", "location": "Tavern"}
        parsed = {
            "hud_state": {"date": "Day 4", "time": "1000", "location": "Market"}
        }
        apply_single_agent_state_updates(ps, parsed, current_turn=2)
        assert ps["hud_state"] == {"date": "Day 4", "time": "1000", "location": "Market"}

    def test_hud_state_empty_dict_still_persisted(self):
        """Empty dict hud_state from tool call should still overwrite (key present, value falsy)."""
        ps = _fresh_pipeline_state()
        ps["hud_state"] = {"date": "Day 3", "time": "0900"}
        parsed = {"hud_state": {}}
        apply_single_agent_state_updates(ps, parsed, current_turn=2)
        assert ps["hud_state"] == {}


class TestMalformedMechanicsHandoff:
    """Deterministic malformed-input coverage for mechanics->state handoff."""

    def _run_pipeline_collect(self, events_data, mechanics_data, game_system="dnd5e", pipeline_state=None):
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(3)

        events_json = json.dumps(events_data)
        mechanics_json = json.dumps(mechanics_data)

        call_count = [0]

        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"content": events_json, "reasoning": None, "input_tokens": 100, "cache_read_tokens": 0,
                        "cache_creation_tokens": 0, "output_tokens": 100, "reasoning_tokens": 0}
            return {"content": mechanics_json, "reasoning": None, "input_tokens": 100, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 100, "reasoning_tokens": 0}

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="narration"),
            StreamEvent(event_type="done", usage={
                "content": "narration", "input_tokens": 50, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 20, "reasoning_tokens": 0
            }),
        ])

        return list(run_pipeline(
            provider=provider,
            client=client,
            username="testuser",
            project="testproject",
            chat_name="testchat",
            branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=pipeline_state,
            game_system=game_system,
        ))

    def test_mechanics_relationship_ops_filtered_to_scene(self):
        events_data = _make_events_response(
            scene_state={
                "location": "Tavern",
                "npcs_present": ["Kira"],
                "active_tensions": [],
                "scene_trigger": "Conversation starts",
                "atmosphere": "Quiet",
                "details": [],
                "pending_actions": [],
            }
        )
        mechanics_data = _make_mechanics_response()
        mechanics_data["relationship_ops"] = [
            {"op": "rs", "target": "Vex", "change": 10},
            {"op": "rs", "target": "Kira", "change": 4},
        ]

        events = self._run_pipeline_collect(events_data, mechanics_data, game_system="dnd5e")
        done = [e for e in events if e[0] == "pipeline_done"][0][1]
        rels = done.pipeline_state["game_state"]["relationships"]

        assert "Kira" in rels
        assert rels["Kira"]["rs"] == 4
        assert "Vex" not in rels

    def test_mechanics_malformed_relationship_ops_do_not_break_pipeline(self):
        events_data = _make_events_response(
            scene_state={
                "location": "Dock",
                "npcs_present": ["Kira"],
                "active_tensions": [],
                "scene_trigger": "Arrival",
                "atmosphere": "Rain",
                "details": [],
                "pending_actions": [],
            }
        )
        mechanics_data = _make_mechanics_response()
        mechanics_data["relationship_ops"] = [
            {"op": "rs", "target": "Kira", "change": "bad-int"},
            {"op": "npc_rs", "target": "Kira", "other": None, "change": 4},
            {"op": "rs", "target": "Kira", "change": 3},
        ]

        events = self._run_pipeline_collect(events_data, mechanics_data, game_system="dnd5e")
        done = [e for e in events if e[0] == "pipeline_done"][0][1]
        rels = done.pipeline_state["game_state"]["relationships"]

        assert rels["Kira"]["rs"] == 3


def _malformed_dict_ops(rng: random.Random, target_key: str, count: int = 120):
    """Generate deterministic dict-shaped malformed ops (no non-dicts)."""
    options = [None, "", "bad", 0, 1, -3, 3.14, [], {}, {"x": 1}]
    id_options = [None, "", "bad", "Kira", "Vex", 0, 1, -3, 3.14, True, False]
    ops = []
    for _ in range(count):
        op = {
            # Keep identity key hashable to avoid unhashable-key TypeErrors.
            target_key: rng.choice(id_options),
            "op": rng.choice(options),
            "change": rng.choice(options),
            "action": rng.choice(options),
            "name": rng.choice(options),
            "other": rng.choice(options),
            "location": rng.choice(options),
            "value": rng.choice(options),
            "fields": rng.choice(options),
        }
        for _ in range(rng.randint(0, 3)):
            keys = list(op.keys())
            if keys:
                op.pop(rng.choice(keys), None)
        ops.append(op)
    return ops


def _malformed_cpred_ip_ops(rng: random.Random, count: int = 120):
    """Generate deterministic dict-shaped malformed ip_ops payload items."""
    options = [None, "", "bad", 0, 1, -3, 3.14, [], {}, {"x": 1}]
    score_values = [None, "", "bad", -10, 0, 1, 7, 10, 20, 30, 40, 50, 60, 70, 80, 120]
    categories = ["group", "warrior", "socializer", "explorer", "roleplayer", None, "", 7, True, {"bad": "dict"}]
    players = [None, "", "V", "Judy", "Panam", 0, -1, 3.14, True, {"bad": "dict"}]
    individuals_pool = [
        [],
        [{"player": "V", "style_ip": 20, "style_category": "warrior", "reason": "clean"}],
        [{"player": "Judy", "style_ip": 40, "style_category": 7, "reason": "odd-category"}],
        [{"player": {"bad": "dict"}, "style_ip": "bad", "style_category": "socializer"}],
        options,
    ]

    ops = []
    for _ in range(count):
        op = {
            "op": rng.choice(["score", "award", "spend", None, "", "bad", 1]),
            "player": rng.choice(players),
            "category": rng.choice(categories),
            "value": rng.choice(score_values),
            "reason": rng.choice(options),
            "group_ip": rng.choice(score_values),
            "group_reason": rng.choice(options),
            "individual": rng.choice(individuals_pool),
            "amount": rng.choice(score_values),
        }
        for _ in range(rng.randint(0, 3)):
            keys = list(op.keys())
            if keys:
                op.pop(rng.choice(keys), None)
        ops.append(op)
    return ops


def _net_combat_pipeline_state():
    return {
        "character_states": {
            "V": {
                "data": {
                    "type": "pc",
                    "vitals": [{"label": "HP", "current": 35, "max": 40}],
                    "resources": [{"label": "Cycles", "current": 3, "max": 3}],
                    "conditions": [],
                }
            },
            "Drone-1": {
                "data": {
                    "type": "enemy",
                    "vitals": [{"label": "HP", "current": 20, "max": 20}],
                    "resources": [],
                    "conditions": [],
                    "combat_data": {"armor": {"head": 4, "body": 4}, "weapons": [{"name": "SMG", "ammo": 30}]},
                }
            },
        },
        "combat": {"round": 1, "initiative_order": ["V", "Drone-1"], "current_turn": "V"},
        "net_combat": {"active": True, "netrunner": "V", "brain_damage": 0, "_prev_brain_damage": 0},
    }


def _net_game_state():
    return {
        "edgerunners": {
            "V": {
                "hp": {"current": 35, "max": 40, "seriously_wounded": False},
                "humanity": {"current": 50, "max": 60},
                "luck": {"current": 5, "max": 7},
                "armor": {"head": 11, "body": 11},
                "eurobucks": 1000,
                "critical_injuries": [],
                "cyberware_effects": [],
                "weapons": [{"name": "Heavy Pistol", "current_ammo": 8, "max_ammo": 8}],
            }
        }
    }


class TestGameStateMalformedInputs:
    """Deterministic malformed-input coverage for game-system apply_game_state handlers."""

    @pytest.mark.parametrize(
        "init_fn,apply_fn,ops_key,target_key,root_key",
        [
            pytest.param(dnd_init, dnd_apply, "relationship_ops", "target", "relationships", id="dnd5e"),
            pytest.param(cyber_init, cyber_apply, "relationship_ops", "target", "relationships", id="dnd5e_cyber"),
            pytest.param(coc_init, coc_apply, "investigator_ops", "investigator", "investigators", id="coc7e"),
            pytest.param(cpred_init, cpred_apply, "edgerunner_ops", "edgerunner", "edgerunners", id="cpred"),
            pytest.param(cpred_init, cpred_apply, "relationship_ops", "target", "relationships", id="cpred_rel"),
            pytest.param(sr_init, sr_apply, "runner_ops", "runner", "runners", id="sr6e"),
        ],
    )
    def test_apply_game_state_handles_malformed_op_corpus(self, init_fn, apply_fn, ops_key, target_key, root_key):
        rng = random.Random(1337)
        state = init_fn()

        payload = {ops_key: _malformed_dict_ops(rng, target_key)}
        result = apply_fn(state, payload, turn=1)

        assert isinstance(result, dict)
        assert root_key in result

    @pytest.mark.parametrize(
        "init_fn,apply_fn,ops_key,valid_op,assert_fn",
        [
            pytest.param(
                dnd_init,
                dnd_apply,
                "relationship_ops",
                {"op": "rs", "target": "Kira", "change": 5},
                lambda s: s["relationships"]["Kira"]["rs"] == 5,
                id="dnd5e",
            ),
            pytest.param(
                coc_init,
                coc_apply,
                "investigator_ops",
                {"investigator": "Harvey", "op": "luck", "change": 10},
                lambda s: s["investigators"]["Harvey"]["luck"] == 10,
                id="coc7e",
            ),
            pytest.param(
                cpred_init,
                cpred_apply,
                "edgerunner_ops",
                {
                    "edgerunner": "V",
                    "op": "set",
                    "fields": {"luck": {"current": 3, "max": 7}, "hp": {"current": 25, "max": 40}},
                },
                lambda s: s["edgerunners"]["V"]["luck"]["current"] == 3,
                id="cpred",
            ),
            pytest.param(
                cpred_init,
                cpred_apply,
                "relationship_ops",
                {"op": "rs", "target": "Rogue", "change": 5},
                lambda s: s["relationships"]["Rogue"]["rs"] == 5,
                id="cpred_rel",
            ),
            pytest.param(
                sr_init,
                sr_apply,
                "runner_ops",
                {"runner": "Raven", "op": "nuyen", "change": 1000},
                lambda s: s["runners"]["Raven"]["nuyen"] == 1000,
                id="sr6e",
            ),
            pytest.param(
                cyber_init,
                cyber_apply,
                "ship_ops",
                {"op": "set", "fields": {"credits": {"party": 10}}},
                lambda s: s["ship"]["credits"]["party"] == 10,
                id="dnd5e_cyber_ship",
            ),
        ],
    )
    def test_apply_game_state_malformed_corpus_does_not_block_valid_op(
        self, init_fn, apply_fn, ops_key, valid_op, assert_fn
    ):
        rng = random.Random(2026)
        state = init_fn()

        malformed = _malformed_dict_ops(rng, target_key="target", count=80)
        payload = {ops_key: copy.deepcopy(malformed) + [valid_op]}
        result = apply_fn(state, payload, turn=2)

        assert assert_fn(result)

    @pytest.mark.parametrize(
        "init_fn,apply_fn,ops_key,bad_identity_key,valid_op,assert_fn",
        [
            pytest.param(
                coc_init,
                coc_apply,
                "investigator_ops",
                "investigator",
                {"investigator": "Harvey", "op": "luck", "change": 5},
                lambda s: s["investigators"]["Harvey"]["luck"] == 5,
                id="coc7e_unhashable_identity",
            ),
            pytest.param(
                cpred_init,
                cpred_apply,
                "edgerunner_ops",
                "edgerunner",
                {"edgerunner": "V", "op": "eurobucks", "change": 100},
                lambda s: s["edgerunners"]["V"]["eurobucks"] == 100,
                id="cpred_unhashable_identity",
            ),
            pytest.param(
                sr_init,
                sr_apply,
                "runner_ops",
                "runner",
                {"runner": "Raven", "op": "nuyen", "change": 200},
                lambda s: s["runners"]["Raven"]["nuyen"] == 200,
                id="sr6e_unhashable_identity",
            ),
        ],
    )
    def test_unhashable_identity_op_is_ignored_and_valid_op_applies(
        self, init_fn, apply_fn, ops_key, bad_identity_key, valid_op, assert_fn
    ):
        state = init_fn()
        payload = {
            ops_key: [
                {bad_identity_key: {"not": "hashable as dict key"}, "op": "nuyen", "change": 999},
                valid_op,
            ]
        }
        result = apply_fn(state, payload, turn=3)
        assert assert_fn(result)


def _mutate_events_payload(rng: random.Random, base: dict, game_system: str) -> dict:
    """Deterministically mutate non-critical fields while preserving top-level shape."""
    payload = copy.deepcopy(base)

    # Keep route stable so the pipeline follows Events -> Mechanics.
    payload["route"] = "mechanics"

    type_pool = [
        None,
        "",
        0,
        -1,
        3.14,
        True,
        False,
        [],
        {},
        {"nested": "x"},
        ["x", 1, None],
    ]
    fields = [
        "pacing", "time_passed", "beats", "player_action", "callbacks", "emotional_context",
        "character_states", "score_changes", "arc_label", "current_player", "next_player",
        "next_player_prompt", "hud_state", "combat", "scene_state",
    ]
    for key in fields:
        if rng.random() < 0.35:
            payload[key] = rng.choice(type_pool)

    # Keep callback/memory ops as list[dict] so apply_* can process robustly.
    if rng.random() < 0.7:
        payload["callback_ops"] = [
            {
                "action": rng.choice(["add", "resolve", "update", "bad", None, 1]),
                "id": rng.choice([None, 1, "1", -3]),
                "original_text": rng.choice(["Hook", "", None, 7]),
                "source_npc": rng.choice(["Kira", "Vex", None, 2]),
                "fields": rng.choice([{}, {"original_text": "x"}, None, "bad"]),
            }
            for _ in range(rng.randint(0, 3))
        ]
    if rng.random() < 0.7:
        payload["npc_memory_ops"] = [
            {
                "action": rng.choice(["add", "drop", "bad", None]),
                "npc": rng.choice(["Kira", "Vex", None, 3]),
                "index": rng.choice([None, 0, 1, -1, "bad"]),
                "text": rng.choice(["Memory", "", None, 5]),
                "impact": rng.choice([None, 1, 3, 5, "bad"]),
                "date": rng.choice(["Day 1", None, 100]),
            }
            for _ in range(rng.randint(0, 3))
        ]

    # Game-system ops: keep as list[dict], deliberately including odd value types.
    if game_system in ("dnd5e", "dnd5e_cyber", "cpred") and rng.random() < 0.8:
        payload["relationship_ops"] = [
            {
                "op": rng.choice(["rs", "roms", "fr", "npc_rs", "npc_roms", "set", "bad", None]),
                "target": rng.choice(["Kira", "Vex", "", None, {"bad": "dict"}, 7]),
                "other": rng.choice(["Ari", None, {"bad": "dict"}]),
                "change": rng.choice([None, -5, 0, 5, "bad"]),
                "type": rng.choice(["npc", "faction", None]),
                "fields": rng.choice([{}, {"rs": 10}, None, "bad"]),
            }
            for _ in range(rng.randint(0, 4))
        ]
    if game_system == "dnd5e_cyber" and rng.random() < 0.8:
        payload["ship_ops"] = [
            {
                "op": rng.choice(["set", "hull", "shields", "shield_regen", "ammo", "credits", None, "bad"]),
                "change": rng.choice([None, -3, 0, 5, "bad"]),
                "fields": rng.choice([{}, {"credits": {"party": 10}}, None, "bad"]),
                "weapon": rng.choice([None, "railgun", 7]),
                "account": rng.choice([None, "party", 3]),
            }
            for _ in range(rng.randint(0, 4))
        ]

    return payload


def _mutate_mechanics_payload(rng: random.Random, base: dict, game_system: str) -> dict:
    """Deterministically mutate mechanics payload while preserving required shape."""
    payload = copy.deepcopy(base)
    payload["route"] = "narration"
    payload["character_states"] = payload.get("character_states") or {}

    type_pool = [None, "", 0, -1, 3.14, True, False, [], {}, {"nested": "x"}, ["x", 1, None]]
    for key in ["beats", "dramatic_notes", "hud", "score_changes", "arc_label", "callbacks", "combat"]:
        if rng.random() < 0.35:
            payload[key] = rng.choice(type_pool)

    if game_system in ("dnd5e", "dnd5e_cyber", "cpred") and rng.random() < 0.8:
        payload["relationship_ops"] = [
            {
                "op": rng.choice(["rs", "roms", "fr", "npc_rs", "npc_roms", "set", "bad", None]),
                "target": rng.choice(["Kira", "Vex", "", None, {"bad": "dict"}, 7]),
                "other": rng.choice(["Ari", None, {"bad": "dict"}]),
                "change": rng.choice([None, -5, 0, 5, "bad"]),
                "type": rng.choice(["npc", "faction", None]),
                "fields": rng.choice([{}, {"rs": 10}, None, "bad"]),
            }
            for _ in range(rng.randint(0, 4))
        ]
    if game_system == "dnd5e_cyber" and rng.random() < 0.8:
        payload["ship_ops"] = [
            {
                "op": rng.choice(["set", "hull", "shields", "shield_regen", "ammo", "credits", None, "bad"]),
                "change": rng.choice([None, -3, 0, 5, "bad"]),
                "fields": rng.choice([{}, {"credits": {"party": 10}}, None, "bad"]),
                "weapon": rng.choice([None, "railgun", 7]),
                "account": rng.choice([None, "party", 3]),
            }
            for _ in range(rng.randint(0, 4))
        ]

    return payload


class TestPipelineBoundaryStructureFuzz:
    """Seeded structure fuzzing at Events/Mechanics JSON handoff boundaries."""

    def _run_once(self, events_data: dict, mechanics_data: dict, game_system: str):
        provider = _make_mock_provider()
        client = MagicMock()
        branch_path = _make_branch_path(3)

        events_json = json.dumps(events_data)
        mechanics_json = json.dumps(mechanics_data)
        call_count = [0]

        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "content": events_json, "reasoning": None, "input_tokens": 100,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 100, "reasoning_tokens": 0,
                }
            return {
                "content": mechanics_json, "reasoning": None, "input_tokens": 100,
                "cache_read_tokens": 0, "cache_creation_tokens": 0, "output_tokens": 100, "reasoning_tokens": 0,
            }

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content="narration"),
            StreamEvent(event_type="done", usage={
                "content": "narration", "input_tokens": 50, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 20, "reasoning_tokens": 0,
            }),
        ])

        events = list(run_pipeline(
            provider=provider,
            client=client,
            username="testuser",
            project="testproject",
            chat_name="testchat",
            branch_path=branch_path,
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=None,
            game_system=game_system,
        ))
        assert any(e[0] == "pipeline_done" for e in events)

    @pytest.mark.parametrize("game_system", ["dnd5e", "dnd5e_cyber"])
    def test_seeded_structure_fuzz_handoff_does_not_throw(self, game_system):
        rng = random.Random(6060)
        base_events = _make_events_response(
            scene_state={
                "location": "Tavern",
                "npcs_present": ["Kira", "Vex"],
                "active_tensions": [],
                "scene_trigger": "Talk",
                "atmosphere": "Calm",
                "details": [],
                "pending_actions": [],
            },
            callback_ops=[],
            npc_memory_ops=[],
        )
        base_mechanics = _make_mechanics_response(character_states={})

        for _ in range(80):
            e_payload = _mutate_events_payload(rng, base_events, game_system)
            m_payload = _mutate_mechanics_payload(rng, base_mechanics, game_system)
            self._run_once(e_payload, m_payload, game_system=game_system)


class TestStatefulSequenceFuzz:
    """Seeded multi-turn sequence fuzzing with invariants."""

    def test_cpred_sequence_invariants(self):
        rng = random.Random(7001)
        state = cpred_init()
        cpred_apply(state, {
            "edgerunner_ops": [{
                "edgerunner": "V",
                "op": "set",
                "fields": {
                    "hp": {"current": 40, "max": 40},
                    "humanity": {"current": 50, "max": 60},
                    "luck": {"current": 7, "max": 7},
                    "armor": {"head": 11, "body": 11},
                    "eurobucks": 5000,
                },
            }]
        }, turn=0)

        for turn in range(1, 121):
            ops = []
            for _ in range(rng.randint(1, 4)):
                op_type = rng.choice(["hp", "luck", "armor", "eurobucks", "humanity", "therapy", "critical_injury"])
                if op_type == "armor":
                    ops.append({
                        "edgerunner": "V", "op": "armor", "location": rng.choice(["head", "body"]),
                        "change": rng.randint(-2, 2),
                    })
                elif op_type == "critical_injury":
                    ops.append({
                        "edgerunner": "V", "op": "critical_injury", "action": "add",
                        "name": f"Injury-{turn}-{rng.randint(1, 9)}", "effect": "test", "dv_mod": rng.randint(0, 3),
                    })
                else:
                    ops.append({"edgerunner": "V", "op": op_type, "change": rng.randint(-5, 5)})
            cpred_apply(state, {"edgerunner_ops": ops}, turn=turn)

            er = state["edgerunners"]["V"]
            assert 0 <= er["hp"]["current"] <= er["hp"]["max"]
            assert 0 <= er["luck"]["current"] <= er["luck"]["max"]
            assert er["armor"]["head"] >= 0 and er["armor"]["body"] >= 0
            assert er["eurobucks"] >= 0
            assert er["humanity"]["current"] >= 0

    def test_sr6e_sequence_invariants(self):
        rng = random.Random(7002)
        state = sr_init()
        sr_apply(state, {
            "runner_ops": [{
                "runner": "Raven",
                "op": "set",
                "fields": {
                    "edge": {"current": 5, "max": 5},
                    "physical_cm": {"filled": 0, "max": 11},
                    "stun_cm": {"filled": 0, "max": 10},
                    "overflow": 0,
                    "essence": 6.0,
                    "nuyen": 10000,
                },
            }]
        }, turn=0)

        for turn in range(1, 121):
            ops = []
            for _ in range(rng.randint(1, 4)):
                op_type = rng.choice([
                    "edge", "edge_reset", "physical", "stun", "heal_physical", "heal_stun", "essence", "nuyen"
                ])
                if op_type == "edge_reset":
                    ops.append({"runner": "Raven", "op": "edge_reset"})
                elif op_type == "essence":
                    ops.append({"runner": "Raven", "op": "essence", "change": round(rng.uniform(-0.5, 0.2), 2)})
                elif op_type in ("heal_physical", "heal_stun"):
                    ops.append({"runner": "Raven", "op": op_type, "change": -rng.randint(0, 3)})
                else:
                    ops.append({"runner": "Raven", "op": op_type, "change": rng.randint(-3, 4)})
            sr_apply(state, {"runner_ops": ops}, turn=turn)

            r = state["runners"]["Raven"]
            assert 0 <= r["edge"]["current"] <= r["edge"]["max"]
            assert 0 <= r["physical_cm"]["filled"] <= r["physical_cm"]["max"]
            assert 0 <= r["stun_cm"]["filled"] <= r["stun_cm"]["max"]
            assert r["overflow"] >= 0
            assert r["essence"] >= 0
            assert r["nuyen"] >= 0

    def test_dnd5e_relationship_sequence_invariants(self):
        rng = random.Random(7003)
        state = dnd_init()
        for turn in range(1, 161):
            ops = []
            for _ in range(rng.randint(1, 5)):
                op = rng.choice(["rs", "roms", "fr", "npc_rs", "npc_roms"])
                entry = {
                    "op": op,
                    "target": rng.choice(["Kira", "Vex", "Ari"]),
                    "change": rng.randint(-15, 15),
                }
                if op in ("npc_rs", "npc_roms"):
                    entry["other"] = rng.choice(["Kira", "Vex", "Ari"])
                ops.append(entry)
            dnd_apply(state, {"relationship_ops": ops}, turn=turn)

            for rel in state["relationships"].values():
                assert -100 <= rel.get("rs", 0) <= 100
                assert 0 <= rel.get("roms", 0) <= 100
                for nrel in rel.get("npc_relationships", {}).values():
                    assert -100 <= nrel.get("rs", 0) <= 100
                    assert 0 <= nrel.get("roms", 0) <= 100
            for faction in state["factions"].values():
                assert -100 <= faction.get("fr", 0) <= 100


class TestCpredIpTrackerFuzz:
    """Seeded malformed-input coverage for CPRED IP tracker ops and rendering."""

    def test_cpred_ip_ops_malformed_corpus_keeps_tracker_shape(self):
        rng = random.Random(9001)
        state = cpred_init()

        for turn in range(1, 61):
            cpred_apply(state, {"ip_ops": _malformed_cpred_ip_ops(rng, count=25)}, turn=turn)
            tracker = state.get("ip_tracker")
            assert isinstance(tracker, dict)
            assert isinstance(tracker.get("session_scores"), dict)
            assert isinstance(tracker.get("awards"), list)
            assert isinstance(tracker.get("balances"), dict)
            assert all(isinstance(v, int) and v >= 0 for v in tracker["balances"].values())

    def test_cpred_ip_ops_malformed_noise_does_not_block_valid_awards(self):
        rng = random.Random(9002)
        state = cpred_init()

        for turn in range(1, 26):
            malformed = _malformed_cpred_ip_ops(rng, count=20)
            valid = [
                {"op": "score", "category": "group", "value": 30, "reason": f"group-{turn}"},
                {"op": "score", "player": "V", "category": "warrior", "value": 40, "reason": f"warrior-{turn}"},
                {"op": "award", "group_ip": 10, "group_reason": f"award-{turn}", "individual": [{"player": "V", "style_ip": 10, "style_category": "warrior", "reason": "clean"}]},
                {"op": "spend", "player": "V", "amount": 5, "reason": "downtime spend"},
            ]
            cpred_apply(state, {"ip_ops": malformed + valid}, turn=turn)

        tracker = state["ip_tracker"]
        assert isinstance(tracker["awards"], list)
        assert len(tracker["awards"]) >= 1
        assert tracker["balances"].get("V", 0) > 0
        assert tracker["session_scores"] == {"group": 0}

    def test_cpred_ip_tracker_rendering_survives_seeded_malformed_sequence(self):
        rng = random.Random(9003)
        state = cpred_init()

        cpred_apply(state, {"edgerunner_ops": [{"edgerunner": "V", "op": "set", "fields": {"hp": {"current": 35, "max": 40}}}]}, turn=0)

        for turn in range(1, 41):
            cpred_apply(state, {"ip_ops": _malformed_cpred_ip_ops(rng, count=18)}, turn=turn)

        # Ensure IP tracker has content so rendering path is exercised.
        cpred_apply(state, {"ip_ops": [{"op": "score", "category": "group", "value": 30, "reason": "crew teamwork"}]}, turn=99)

        rendered = cpred_build_injection(state)
        assert isinstance(rendered, str)
        assert "[EDGERUNNER STATE]" in rendered
        assert "[IP TRACKER]" in rendered
        assert "[/IP TRACKER]" in rendered

    def test_cpred_invalid_individual_score_not_reclassified_as_group(self):
        state = cpred_init()
        cpred_apply(
            state,
            {"ip_ops": [{"op": "score", "category": "warrior", "value": 40, "reason": "missing player"}]},
            turn=1,
        )
        assert state["ip_tracker"]["session_scores"] == {"group": 0}

    def test_cpred_ip_tracker_renders_even_when_edgerunners_empty(self):
        state = cpred_init()
        cpred_apply(
            state,
            {"ip_ops": [{"op": "score", "category": "group", "value": 30, "reason": "crew teamwork"}]},
            turn=1,
        )
        rendered = cpred_build_injection(state)
        assert "[EDGERUNNER STATE]" in rendered
        assert "[IP TRACKER]" in rendered
        assert "Group: 30" in rendered

    def test_cpred_award_with_malformed_style_ip_still_resets_and_updates(self):
        state = cpred_init()
        cpred_apply(
            state,
            {
                "ip_ops": [
                    {"op": "score", "category": "group", "value": 30, "reason": "group"},
                    {"op": "award", "group_ip": 10, "group_reason": "award", "individual": [
                        {"player": "V", "style_ip": "bad", "style_category": "warrior", "reason": "bad style"},
                        {"player": "Judy", "style_ip": 20, "style_category": "socializer", "reason": "good style"},
                    ]},
                ]
            },
            turn=2,
        )
        tracker = state["ip_tracker"]
        assert tracker["session_scores"] == {"group": 0}
        assert tracker["balances"]["V"] == 30
        assert tracker["balances"]["Judy"] == 50
        assert tracker["awards"][-1]["individual"][0]["style_ip"] == 0


class TestCpredNetCombatMalformedFuzz:
    """Seeded malformed-input coverage for CPRED NET combat helpers from latest commit."""

    def test_init_net_combat_from_hack_handles_non_string_enemies(self):
        hack_state = {"hacker_name": "Raven", "target_system": "Node-77", "context": "Infiltration underway"}
        net = cpred_init_net_combat_from_hack(
            hack_state,
            combat_info={"reason": "Alarm tripped", "enemies": [1, {"x": 2}, "Guard"]},
        )
        assert isinstance(net, dict)
        assert net.get("target") == "Node-77"
        assert net.get("context")

    def test_apply_net_combat_state_handles_none_net_combat(self):
        pipeline_state = _net_combat_pipeline_state()
        pipeline_state["net_combat"] = None
        cpred_apply_net_combat_state(
            pipeline_state,
            {"hack_state": {"brain_damage": 1}, "available_actions": ["Backdoor"]},
            game_state=_net_game_state(),
        )
        assert isinstance(pipeline_state.get("net_combat"), dict)
        assert pipeline_state["net_combat"].get("brain_damage") == 1

    def test_collapse_net_combat_messages_handles_non_dict_tool_input(self):
        branch = [
            {"id": "sys", "role": "system", "content": "System"},
            {"id": "u1", "role": "user", "content": "Start", "net_combat_mode": True},
            {
                "id": "a1",
                "role": "assistant",
                "content": "Exchange 1",
                "net_combat_mode": True,
                "net_combat_tool_input": [1, 2, 3],
            },
            {"id": "u2", "role": "user", "content": "After"},
        ]
        collapsed = collapse_net_combat_messages(branch)
        assert collapsed[-1]["content"] == "After"

    def test_seeded_malformed_apply_net_combat_state_does_not_throw(self):
        rng = random.Random(9101)
        atoms = [None, 0, 1, -1, 3.2, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

        def rand(depth=0):
            if depth > 2 or rng.random() < 0.5:
                return rng.choice(atoms)
            if rng.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(rng.randint(0, 3))]
            d = {}
            keys = [
                "character_updates", "cover_state", "combat", "combat_complete",
                "hack_state", "available_actions", "net_complete", "narrative_summary",
            ]
            for _ in range(rng.randint(0, 7)):
                d[rng.choice(keys)] = rand(depth + 1)
            return d

        for _ in range(1200):
            cpred_apply_net_combat_state(
                _net_combat_pipeline_state(),
                rand(),
                game_state=_net_game_state(),
            )

    def test_seeded_malformed_collapse_net_combat_does_not_throw(self):
        rng = random.Random(9102)
        atoms = [None, 0, 1, "", "txt", True, False, [], [1], {}, {"x": 1}]

        def rand(depth=0):
            if depth > 2 or rng.random() < 0.5:
                return rng.choice(atoms)
            if rng.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(rng.randint(0, 3))]
            d = {}
            keys = ["narrative_summary", "net_complete", "combat_complete", "hack_state", "available_actions"]
            for _ in range(rng.randint(0, 5)):
                d[rng.choice(keys)] = rand(depth + 1)
            return d

        for _ in range(1000):
            history = []
            for i in range(rng.randint(1, 8)):
                in_nc = rng.random() < 0.5
                msg = {"id": f"m{i}", "role": "assistant" if i % 2 else "user", "content": f"msg{i}"}
                if in_nc:
                    msg["net_combat_mode"] = True
                    msg["net_combat_tool_input"] = rand()
                history.append(msg)

            branch = [{"id": "sys", "role": "system", "content": "sys"}] + history + [
                {"id": "last", "role": "user", "content": "last"}
            ]
            collapsed = collapse_net_combat_messages(branch)
            assert collapsed[0]["content"] == "sys"
            assert collapsed[-1]["content"] == "last"

    def test_seeded_mixed_type_history_collapse_net_combat_does_not_throw(self):
        rng = random.Random(9103)
        atoms = [None, 0, 1, "", "txt", True, False, [], [1], {"x": 1}]

        for _ in range(600):
            history = []
            for i in range(rng.randint(1, 10)):
                # Mix dict and non-dict entries; collapse should never assume dict.
                if rng.random() < 0.35:
                    history.append(rng.choice(atoms))
                    continue

                msg = {"id": f"m{i}", "role": "assistant" if i % 2 else "user", "content": f"msg{i}"}
                if rng.random() < 0.5:
                    msg["net_combat_mode"] = True
                    msg["net_combat_tool_input"] = rng.choice([
                        {"narrative_summary": "NET clash resolved"},
                        {"narrative_summary": ""},
                        {"other": "x"},
                        [1, 2, 3],
                        "bad",
                        None,
                    ])
                history.append(msg)

            branch = [{"id": "sys", "role": "system", "content": "sys"}] + history + [
                {"id": "last", "role": "user", "content": "last"}
            ]
            collapsed = collapse_net_combat_messages(branch)
            assert isinstance(collapsed, list)
            assert collapsed[0]["content"] == "sys"
            assert collapsed[-1]["content"] == "last"


class TestNestedMalformedStructureFuzz:
    """Seeded malformed nested-structure fuzzing for migration and single-agent path."""

    def test_migrate_pipeline_state_handles_malformed_nested_types(self):
        rng = random.Random(8080)
        weird_vals = [None, True, 7, "bad", [], [1, 2], {"x": 1}]

        for _ in range(80):
            state = {
                "pacing": rng.choice(weird_vals),
                "callback_ledger": rng.choice(weird_vals),
                "npc_memories": rng.choice(weird_vals),
                "scene_state": rng.choice(weird_vals),
                "character_states": rng.choice(weird_vals),
                "game_state": rng.choice(weird_vals),
                "hud_state": rng.choice(weird_vals),
                "combat": rng.choice(weird_vals),
                "ship_combat": rng.choice(weird_vals),
                "turn_counter": rng.choice(weird_vals),
            }
            migrated = migrate_pipeline_state(state)
            assert isinstance(migrated, dict)
            assert isinstance(migrated.get("callback_ledger"), dict)
            assert isinstance(migrated["callback_ledger"].get("open"), list)
            assert isinstance(migrated["callback_ledger"].get("recently_resolved"), list)
            assert isinstance(migrated.get("character_states"), dict)
            assert isinstance(migrated.get("scene_state"), dict)
            assert isinstance(migrated.get("npc_memories"), dict)
            assert isinstance(migrated.get("hud_state"), dict)
            assert isinstance(migrated.get("pacing"), dict)
            assert isinstance(migrated.get("game_state"), dict)
            assert isinstance(migrated.get("turn_counter"), int)

    def test_apply_single_agent_state_updates_handles_malformed_nested_types(self):
        rng = random.Random(8081)
        weird_vals = [None, True, 7, "bad", [], [1, 2], {"x": 1}]

        for _ in range(80):
            ps = migrate_pipeline_state({
                "pacing": rng.choice(weird_vals),
                "callback_ledger": rng.choice(weird_vals),
                "npc_memories": rng.choice(weird_vals),
                "scene_state": rng.choice(weird_vals),
                "character_states": rng.choice(weird_vals),
                "game_state": rng.choice(weird_vals),
                "hud_state": rng.choice(weird_vals),
                "turn_counter": rng.randint(0, 20),
            })
            parsed = {
                "pacing": rng.choice(weird_vals),
                "callback_ops": rng.choice(weird_vals),
                "npc_memory_ops": rng.choice(weird_vals),
                "scene_state": rng.choice(weird_vals),
                "character_states": rng.choice(weird_vals),
                "relationship_ops": rng.choice(weird_vals),
                "hud_state": rng.choice(weird_vals),
                "combat": rng.choice(weird_vals),
            }
            updated = apply_single_agent_state_updates(ps, parsed, current_turn=ps["turn_counter"] + 1)
            assert isinstance(updated, dict)
            assert isinstance(updated.get("callback_ledger"), dict)
            assert isinstance(updated.get("npc_memories"), dict)
            assert isinstance(updated.get("scene_state"), dict)
            assert isinstance(updated.get("character_states"), dict)
            assert isinstance(updated.get("hud_state"), dict)


class TestPipelineSingleAgentDifferential:
    """Differential checks: equivalent payloads should converge across apply paths."""

    @staticmethod
    def _build_provider_for_payloads(events_data: dict, mechanics_data: dict, narration_text: str = "Narration"):
        provider = _make_mock_provider()
        events_json = json.dumps(events_data)
        mechanics_json = json.dumps(mechanics_data)
        call_count = [0]

        def mock_non_streaming(cl, params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "content": events_json, "reasoning": None, "input_tokens": 1000, "cache_read_tokens": 0,
                    "cache_creation_tokens": 0, "output_tokens": 500, "reasoning_tokens": 100
                }
            return {
                "content": mechanics_json, "reasoning": None, "input_tokens": 500, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 300, "reasoning_tokens": 50
            }

        provider.send_request_non_streaming = mock_non_streaming
        provider.send_request_stream = lambda cl, params: iter([
            StreamEvent(event_type="content_delta", content=narration_text),
            StreamEvent(event_type="done", usage={
                "content": narration_text, "input_tokens": 200, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "output_tokens": 100, "reasoning_tokens": 0
            }),
        ])
        return provider

    def _run_pipeline_once(self, pipeline_state: dict, events_data: dict, mechanics_data: dict, game_system: str):
        provider = self._build_provider_for_payloads(events_data, mechanics_data)
        events = list(run_pipeline(
            provider=provider,
            client=MagicMock(),
            username="testuser",
            project="testproject",
            chat_name="testchat",
            branch_path=_make_branch_path(4),
            agent_instructions={"events": "", "mechanics": "", "narration": ""},
            agent_files={"events": "", "mechanics": "", "narration": ""},
            pipeline_state=copy.deepcopy(pipeline_state),
            game_system=game_system,
        ))
        done = [e for e in events if e[0] == "pipeline_done"][0][1]
        return done.pipeline_state

    @staticmethod
    def _state_snapshot(ps: dict):
        """Compare only stateful fields shared across both apply pathways."""
        return {
            "pacing": ps.get("pacing", {}),
            "callback_ledger": ps.get("callback_ledger", {}),
            "npc_memories": ps.get("npc_memories", {}),
            "scene_state": ps.get("scene_state", {}),
            "character_states": ps.get("character_states", {}),
            "game_state": ps.get("game_state", {}),
            "hud_state": ps.get("hud_state", {}),
            "combat": ps.get("combat"),
            "turn_counter": ps.get("turn_counter"),
        }

    @pytest.mark.parametrize("game_system", ["dnd5e", "dnd5e_cyber"])
    def test_equivalent_payloads_converge_pipeline_vs_single_agent(self, game_system):
        base = _fresh_pipeline_state()

        events_data = _make_events_response(
            pacing={"episode": "Ep4", "beat": "Crossroads", "beat_responses": 2, "notes": "rising tension"},
            callback_ops=[
                {"action": "add", "original_text": "Kira asks for help", "source_npc": "Kira"}
            ],
            npc_memory_ops=[
                {"action": "add", "npc": "Kira", "text": "Agreed to help with smuggler issue", "date": "Day 6", "impact": 3}
            ],
            scene_state={
                "location": "Tavern",
                "npcs_present": ["Kira"],
                "active_tensions": ["Debt collectors approaching"],
                "scene_trigger": "Kira sits down",
                "atmosphere": "Low, tense voices",
                "details": ["Rain at windows"],
                "pending_actions": ["Decide whether to help"]
            },
            extra_fields={
                "relationship_ops": [{"op": "rs", "target": "Kira", "change": 2}],
                "hud_state": {"date": "Day 6", "time": "2015", "location": "Tavern", "funds": {"Aedina": "95 gp"}},
                # Pipeline ignores Events character_states on non-output turns.
                "character_states": {}
            }
        )

        mechanics_data = _make_mechanics_response(
            character_states={
                "Aedina": {"type": "pc", "class": "Rogue", "level": 4, "vitals": [], "resources": [], "conditions": [], "summary": "alert"}
            }
        )
        mechanics_data["relationship_ops"] = [{"op": "rs", "target": "Kira", "change": 3}]

        # Pipeline path
        pipeline_result = self._run_pipeline_once(base, events_data, mechanics_data, game_system=game_system)

        # Single-agent equivalent path:
        # 1) Apply events-like parsed payload (same turn).
        single_state = migrate_pipeline_state(copy.deepcopy(base))
        single_state["turn_counter"] += 1
        current_turn = single_state["turn_counter"]
        apply_single_agent_state_updates(
            single_state,
            copy.deepcopy(events_data),
            current_turn,
            game_system=__import__("game_systems", fromlist=["get_game_system"]).get_game_system(game_system),
        )

        # 2) Apply mechanics-like payload same turn.
        mechanics_parsed = {
            "character_states": copy.deepcopy(mechanics_data["character_states"]),
            "relationship_ops": copy.deepcopy(mechanics_data.get("relationship_ops", [])),
            "combat": mechanics_data.get("combat"),
        }
        apply_single_agent_state_updates(
            single_state,
            mechanics_parsed,
            current_turn,
            game_system=__import__("game_systems", fromlist=["get_game_system"]).get_game_system(game_system),
        )

        assert self._state_snapshot(single_state) == self._state_snapshot(pipeline_result)

    def test_single_agent_and_pipeline_match_cpred_state_projection(self):
        base = _fresh_pipeline_state()

        events_data = _make_events_response(
            scene_state={
                "location": "Warehouse",
                "npcs_present": ["Fixer Lux"],
                "active_tensions": [],
                "scene_trigger": "Briefing",
                "atmosphere": "Neon hum",
                "details": [],
                "pending_actions": []
            },
            extra_fields={
                "edgerunner_ops": [
                    {"edgerunner": "V", "op": "set", "fields": {
                        "hp": {"current": 35, "max": 40},
                        "humanity": {"current": 48, "max": 60},
                        "luck": {"current": 6, "max": 7},
                        "armor": {"head": 11, "body": 11},
                        "eurobucks": 2500
                    }}
                ],
                # Pipeline ignores Events character_states on non-output turns.
                "character_states": {}
            }
        )
        mechanics_data = _make_mechanics_response(character_states={})
        mechanics_data["edgerunner_ops"] = [
            {"edgerunner": "V", "op": "hp", "change": -5, "reason": "incoming fire"},
            {"edgerunner": "V", "op": "armor", "location": "body", "change": -1, "reason": "ablation"},
        ]

        pipeline_result = self._run_pipeline_once(base, events_data, mechanics_data, game_system="cpred")

        single_state = migrate_pipeline_state(copy.deepcopy(base))
        single_state["turn_counter"] += 1
        current_turn = single_state["turn_counter"]
        gs_cpred = __import__("game_systems", fromlist=["get_game_system"]).get_game_system("cpred")
        apply_single_agent_state_updates(single_state, copy.deepcopy(events_data), current_turn, game_system=gs_cpred)
        apply_single_agent_state_updates(
            single_state,
            {"character_states": {}, "edgerunner_ops": copy.deepcopy(mechanics_data["edgerunner_ops"])},
            current_turn,
            game_system=gs_cpred,
        )

        assert self._state_snapshot(single_state) == self._state_snapshot(pipeline_result)


class TestContractDriftGuards:
    """Guard critical op-schema tokens from drifting away from apply_game_state behavior."""

    @staticmethod
    def _report_state_properties(system_id: str) -> dict:
        return get_game_system(system_id)["state_report_tool"]["input_schema"]["properties"]

    def test_dnd5e_relationship_ops_schema_covers_inter_npc_ops(self):
        props = self._report_state_properties("dnd5e")
        rel_items = props["relationship_ops"]["items"]["properties"]
        op_enum = set(rel_items["op"]["enum"])
        assert {"rs", "roms", "fr", "set", "npc_rs", "npc_roms", "npc_set"} <= op_enum
        assert "other" in rel_items
        assert "fields" in rel_items

    def test_dnd5e_cyber_ship_ops_schema_covers_apply_ops(self):
        props = self._report_state_properties("dnd5e_cyber")
        ship_items = props["ship_ops"]["items"]["properties"]
        op_enum = set(ship_items["op"]["enum"])
        assert {"hull", "shields", "shield_regen", "ammo", "credits", "set"} <= op_enum
        assert "weapon" in ship_items
        assert "account" in ship_items
        assert "fields" in ship_items

    def test_cpred_critical_injury_schema_includes_required_fields(self):
        props = self._report_state_properties("cpred")
        er_items = props["edgerunner_ops"]["items"]["properties"]
        op_enum = set(er_items["op"]["enum"])
        assert "critical_injury" in op_enum
        assert set(er_items["action"]["enum"]) == {"add", "remove", "quick_fix"}
        assert "name" in er_items
        assert "effect" in er_items
        assert "dv_mod" in er_items

    def test_coc7e_mania_phobia_schema_exposes_action_and_value(self):
        props = self._report_state_properties("coc7e")
        inv_items = props["investigator_ops"]["items"]["properties"]
        op_enum = set(inv_items["op"]["enum"])
        assert {"phobia", "mania"} <= op_enum
        assert set(inv_items["action"]["enum"]) == {"add", "remove"}
        assert "value" in inv_items

    def test_sr6e_sustained_effect_schema_exposes_action_and_value(self):
        props = self._report_state_properties("sr6e")
        runner_items = props["runner_ops"]["items"]["properties"]
        op_enum = set(runner_items["op"]["enum"])
        assert {"sustained", "effect"} <= op_enum
        assert set(runner_items["action"]["enum"]) == {"add", "remove"}
        assert "value" in runner_items

    def test_cpred_relationship_ops_schema_covers_inter_npc_ops(self):
        props = self._report_state_properties("cpred")
        rel_items = props["relationship_ops"]["items"]["properties"]
        op_enum = set(rel_items["op"]["enum"])
        assert {"rs", "roms", "fr", "set", "npc_rs", "npc_roms", "npc_set"} <= op_enum
        assert "other" in rel_items
        assert "fields" in rel_items


class TestCpredRelationshipOps:
    """Test CPRED relationship_ops apply, clamping, and injection rendering."""

    def test_cpred_rs_basic(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "rs", "target": "Rogue", "change": 10}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["rs"] == 10
        assert state["relationships"]["Rogue"]["roms"] == 0

    def test_cpred_roms_basic(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "roms", "target": "Judy", "change": 30}
        ]}, turn=1)
        assert state["relationships"]["Judy"]["roms"] == 30

    def test_cpred_fr_basic(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "fr", "target": "Tyger Claws", "change": -15}
        ]}, turn=1)
        assert state["factions"]["Tyger Claws"]["fr"] == -15

    def test_cpred_rs_clamp_positive(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "rs", "target": "Rogue", "change": 200}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["rs"] == 100

    def test_cpred_rs_clamp_negative(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "rs", "target": "Rogue", "change": -200}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["rs"] == -100

    def test_cpred_roms_clamp_zero(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "roms", "target": "Judy", "change": -50}
        ]}, turn=1)
        assert state["relationships"]["Judy"]["roms"] == 0

    def test_cpred_roms_clamp_hundred(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "roms", "target": "Judy", "change": 200}
        ]}, turn=1)
        assert state["relationships"]["Judy"]["roms"] == 100

    def test_cpred_fr_clamp(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "fr", "target": "TC", "change": 200}
        ]}, turn=1)
        assert state["factions"]["TC"]["fr"] == 100
        cpred_apply(state, {"relationship_ops": [
            {"op": "fr", "target": "TC", "change": -300}
        ]}, turn=2)
        assert state["factions"]["TC"]["fr"] == -100

    def test_cpred_set_npc(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "set", "target": "Rogue", "type": "npc", "fields": {"rs": 50, "roms": 0, "notes": "Crew fixer"}}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["rs"] == 50
        assert state["relationships"]["Rogue"]["notes"] == "Crew fixer"

    def test_cpred_set_faction(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "set", "target": "Tyger Claws", "type": "faction", "fields": {"fr": -30}}
        ]}, turn=1)
        assert state["factions"]["Tyger Claws"]["fr"] == -30

    def test_cpred_npc_rs(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "npc_rs", "target": "Rogue", "other": "Judy", "change": 15}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["npc_relationships"]["Judy"]["rs"] == 15

    def test_cpred_npc_roms(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "npc_roms", "target": "Rogue", "other": "Judy", "change": 20}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["npc_relationships"]["Judy"]["roms"] == 20

    def test_cpred_npc_set(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "npc_set", "target": "Rogue", "other": "Judy", "fields": {"rs": 60, "roms": 10}}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["npc_relationships"]["Judy"]["rs"] == 60
        assert state["relationships"]["Rogue"]["npc_relationships"]["Judy"]["roms"] == 10

    def test_cpred_npc_rs_clamp(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "npc_rs", "target": "A", "other": "B", "change": 200}
        ]}, turn=1)
        assert state["relationships"]["A"]["npc_relationships"]["B"]["rs"] == 100
        cpred_apply(state, {"relationship_ops": [
            {"op": "npc_roms", "target": "A", "other": "B", "change": -50}
        ]}, turn=2)
        assert state["relationships"]["A"]["npc_relationships"]["B"]["roms"] == 0

    def test_cpred_relationship_injection_empty(self):
        state = cpred_init()
        injection = cpred_build_injection(state)
        assert "[RELATIONSHIP STATE]" in injection
        assert "bootstrap" in injection

    def test_cpred_relationship_injection_populated(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "set", "target": "Rogue", "type": "npc", "fields": {"rs": 55, "roms": 0}},
            {"op": "set", "target": "Tyger Claws", "type": "faction", "fields": {"fr": 30}},
        ]}, turn=1)
        injection = cpred_build_injection(state)
        assert "Rogue" in injection
        assert "T4: Good" in injection
        assert "Tyger Claws" in injection
        assert "T2: Accepted" in injection

    def test_cpred_relationship_injection_with_inter_npc(self):
        state = cpred_init()
        cpred_apply(state, {"relationship_ops": [
            {"op": "set", "target": "Rogue", "type": "npc", "fields": {"rs": 40, "roms": 0}},
            {"op": "npc_rs", "target": "Rogue", "other": "Judy", "change": 50},
        ]}, turn=1)
        injection = cpred_build_injection(state)
        assert "\u2192 Judy" in injection

    def test_cpred_backward_compat_no_relationships(self):
        """Existing campaigns with no relationships key still work."""
        state = {"edgerunners": {}, "ip_tracker": {"session_scores": {"group": 0}, "awards": [], "balances": {}}}
        # apply_game_state should not crash
        cpred_apply(state, {"relationship_ops": [
            {"op": "rs", "target": "Rogue", "change": 5}
        ]}, turn=1)
        assert state["relationships"]["Rogue"]["rs"] == 5

    def test_cpred_mixed_edgerunner_and_relationship_ops(self):
        """Both edgerunner_ops and relationship_ops in same turn."""
        state = cpred_init()
        cpred_apply(state, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "set", "fields": {"hp": {"current": 35, "max": 40}}}
            ],
            "relationship_ops": [
                {"op": "rs", "target": "Rogue", "change": 10}
            ]
        }, turn=1)
        assert state["edgerunners"]["V"]["hp"]["current"] == 35
        assert state["relationships"]["Rogue"]["rs"] == 10

    def test_cpred_relationship_ops_non_dict_entries_are_ignored(self):
        state = cpred_init()
        cpred_apply(
            state,
            {
                "relationship_ops": [
                    "bad-op",
                    42,
                    None,
                    {"op": "rs", "target": "Rogue", "change": 5},
                ]
            },
            turn=1,
        )
        assert state["relationships"]["Rogue"]["rs"] == 5

    def test_cpred_relationship_sequence_invariants(self):
        """160-turn fuzz: all scores stay in valid ranges."""
        rng = random.Random(7004)
        state = cpred_init()
        for turn in range(1, 161):
            ops = []
            for _ in range(rng.randint(1, 5)):
                op = rng.choice(["rs", "roms", "fr", "npc_rs", "npc_roms"])
                entry = {
                    "op": op,
                    "target": rng.choice(["Rogue", "Judy", "Jackie"]),
                    "change": rng.randint(-15, 15),
                }
                if op in ("npc_rs", "npc_roms"):
                    entry["other"] = rng.choice(["Rogue", "Judy", "Jackie"])
                ops.append(entry)
            cpred_apply(state, {"relationship_ops": ops}, turn=turn)

            for rel in state["relationships"].values():
                assert -100 <= rel.get("rs", 0) <= 100
                assert 0 <= rel.get("roms", 0) <= 100
                for nrel in rel.get("npc_relationships", {}).values():
                    assert -100 <= nrel.get("rs", 0) <= 100
                    assert 0 <= nrel.get("roms", 0) <= 100
            for faction in state["factions"].values():
                assert -100 <= faction.get("fr", 0) <= 100
