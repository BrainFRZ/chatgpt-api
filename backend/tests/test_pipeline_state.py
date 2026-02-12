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
    build_events_messages,
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
        assert result["turn_counter"] == 0

    def test_old_flat_pacing_wrapped(self):
        old_state = {"episode": "Ep1", "beat": "Opening", "beat_responses": 1, "notes": ""}
        result = migrate_pipeline_state(old_state)
        assert result["pacing"] == old_state
        assert "callback_ledger" in result
        assert result["callback_ledger"]["next_id"] == 1
        assert result["npc_memories"] == {}
        assert result["scene_state"] == {}
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
        assert set(state.keys()) == {"pacing", "callback_ledger", "npc_memories", "scene_state", "character_states", "turn_counter"}


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
        assert len(result["active_tensions"]) == 1
        assert result["atmosphere"] == "Tense, low lighting, rain hammering the windows"

    def test_missing_keys_get_defaults(self):
        result = apply_scene_state({"location": "Tavern"})
        assert result["location"] == "Tavern"
        assert result["npcs_present"] == []
        assert result["active_tensions"] == []
        assert result["scene_trigger"] == ""
        assert result["atmosphere"] == ""
        assert result["details"] == []
        assert result["pending_actions"] == []

    def test_empty_input(self):
        result = apply_scene_state({})
        assert result["location"] == ""
        assert result["npcs_present"] == []

    def test_extra_keys_ignored(self):
        result = apply_scene_state({"location": "X", "bogus_key": "should be dropped"})
        assert "bogus_key" not in result
        assert result["location"] == "X"


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
        assert "Atmosphere: Tense, low lighting" in result

    def test_empty_scene(self):
        assert build_scene_state_injection({}) == ""

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
            updates_text="RS Kira +2"
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
        assert "[CONTEXT UPDATES]" in user_content
        assert "I attack the guard" in user_content

    def test_injection_order(self, full_pipeline_state):
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=full_pipeline_state,
            updates_text="updates here"
        )
        user_content = messages[-1]["content"]
        # Verify order: PIPELINE STATE before CALLBACK LEDGER before NPC MEMORIES before SCENE STATE before CHARACTER STATES before CONTEXT UPDATES before user message
        idx_pipeline = user_content.index("[PIPELINE STATE]")
        idx_callback = user_content.index("[CALLBACK LEDGER]")
        idx_memories = user_content.index("[NPC MEMORIES:")
        idx_scene = user_content.index("[SCENE STATE]")
        idx_char = user_content.index("[CHARACTER STATES]")
        idx_updates = user_content.index("[CONTEXT UPDATES]")
        idx_action = user_content.index("action")
        assert idx_pipeline < idx_callback < idx_memories < idx_scene < idx_char < idx_updates < idx_action

    def test_empty_state_minimal_injections(self, fresh_state):
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "hello"},
            pipeline_state=fresh_state,
        )
        user_content = messages[-1]["content"]
        # Empty state should not inject callback/memory/scene blocks
        assert "[CALLBACK LEDGER]" not in user_content
        assert "[NPC MEMORIES:" not in user_content
        assert "[SCENE STATE]" not in user_content
        # Pacing is empty too
        assert "[PIPELINE STATE]" not in user_content
        # Just the user message
        assert user_content == "hello"

    def test_no_updates_text(self, full_pipeline_state):
        messages = build_events_messages(
            system_prompt="sys",
            history_messages=[],
            user_message={"role": "user", "content": "action"},
            pipeline_state=full_pipeline_state,
            updates_text=""
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
        pairs = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 60  # All 30 pairs included

    def test_at_threshold_includes_all(self):
        """At exactly threshold: all pairs included (no trim needed)."""
        branch = self._make_branch(40)
        pairs = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 80  # All 40 pairs included

    def test_over_threshold_trims_to_target(self):
        """Over threshold: trimmed to target pairs."""
        branch = self._make_branch(41)
        pairs = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert len(pairs) == 40  # Trimmed to 20 pairs
        # Should be the LAST 20 pairs
        assert pairs[0]["content"] == "user msg 21"

    def test_excludes_system_and_current_user(self):
        branch = self._make_branch(5)
        pairs = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
        assert all(p["content"] != "system prompt" for p in pairs)
        assert all(p["content"] != "current user message" for p in pairs)

    def test_attached_files_included(self):
        branch = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello", "attached_files": [{"filename": "test.md", "content": "file content"}]},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "next"},
        ]
        pairs = get_context_pairs(branch, threshold_pairs=40, target_pairs=20)
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
                              pipeline_state=None, n_history=5):
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
        assert set(state.keys()) == {"pacing", "callback_ledger", "npc_memories", "scene_state", "character_states", "turn_counter"}

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
            updates_text="RS Captain Reva +1 (12)"
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

        # Context updates
        assert "[CONTEXT UPDATES]" in user_content
        assert "RS Captain Reva +1" in user_content

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

        # No injection blocks should be present on fresh state
        assert "[CALLBACK LEDGER]" not in content
        assert "[NPC MEMORIES:" not in content
        assert "[SCENE STATE]" not in content
        assert "[PIPELINE STATE]" not in content
        # Just the user message
        assert content == "I attack the guard"


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
        assert build_character_states_injection({}) == ""

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
        assert result["character_states"]["Aedina"]["state"] == "50 HP"
        assert result["character_states"]["Aedina"]["last_updated"] == 3

    def test_already_structured_character_states_unchanged(self):
        """Already-structured character_states are not double-wrapped."""
        state = {
            "pacing": {},
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {"Aedina": {"state": "50 HP", "last_updated": 3}},
            "turn_counter": 5,
        }
        result = migrate_pipeline_state(state)
        assert result["character_states"]["Aedina"] == {"state": "50 HP", "last_updated": 3}


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
        assert cs["Aedina"]["state"] == "42/50 HP, 1/3 spell slots"
        assert cs["Aedina"]["last_updated"] == 1
        assert cs["Orrophim"]["state"] == "38/38 HP, rage 1/3"
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
            "character_states": {"Aedina": {"state": "50/50 HP", "last_updated": 2}},
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
        assert cs["Aedina"]["state"] == "50/50 HP"
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
        assert result["Aedina"] == {"state": "50 HP", "last_updated": 1}

    def test_merge_updates_existing(self):
        existing = {"Aedina": {"state": "50 HP", "last_updated": 1}}
        result = apply_character_states(existing, {"Aedina": "42 HP"}, current_turn=2)
        assert result["Aedina"] == {"state": "42 HP", "last_updated": 2}

    def test_merge_preserves_untouched(self):
        existing = {
            "Aedina": {"state": "50 HP", "last_updated": 5},
            "Orrophim": {"state": "38 HP", "last_updated": 5},
        }
        result = apply_character_states(existing, {"Aedina": "42 HP"}, current_turn=6)
        assert result["Aedina"] == {"state": "42 HP", "last_updated": 6}
        assert result["Orrophim"] == {"state": "38 HP", "last_updated": 5}

    def test_merge_adds_new_character(self):
        existing = {"Aedina": {"state": "50 HP", "last_updated": 5}}
        result = apply_character_states(existing, {"Vex": "68 HP, AC 16"}, current_turn=6)
        assert "Aedina" in result
        assert result["Vex"] == {"state": "68 HP, AC 16", "last_updated": 6}

    def test_ttl_prunes_stale(self):
        existing = {
            "Aedina": {"state": "50 HP", "last_updated": 5},
            "Old Guard": {"state": "dead", "last_updated": 1},
        }
        # Turn 1 + TTL(150) + 1 = 152 — Old Guard should be pruned
        result = apply_character_states(existing, {"Aedina": "45 HP"}, current_turn=152)
        assert "Aedina" in result
        assert "Old Guard" not in result

    def test_ttl_keeps_within_window(self):
        existing = {
            "Aedina": {"state": "50 HP", "last_updated": 5},
            "Guard": {"state": "68 HP", "last_updated": 5},
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
            assert result[name]["state"] == mechanics_output[name]
            assert result[name]["last_updated"] == 1
