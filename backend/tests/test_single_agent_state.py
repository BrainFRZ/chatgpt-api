"""
Tests for single-agent stateful persistence (Claude project chats):
- State updates block parsing (per-section and full block)
- StateInterceptor (streaming interception)
- build_single_agent_injections
- apply_single_agent_state_updates (round-trip)
"""

import json
import copy
import sys
import os

import pytest

# Ensure backend is on sys.path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import (
    parse_state_updates_block,
    _parse_pacing_section,
    _parse_callbacks_section,
    _parse_memories_section,
    _parse_scene_section,
    _parse_characters_section,
    apply_single_agent_state_updates,
    apply_scene_state,
    apply_npc_memory_ops,
    build_single_agent_injections,
    StateInterceptor,
    STATE_BLOCK_START,
    STATE_BLOCK_END,
    migrate_pipeline_state,
    _fresh_pipeline_state,
    build_callback_injection,
    build_npc_memories_injection,
    build_scene_state_injection,
    build_character_states_injection,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fresh_state():
    return _fresh_pipeline_state()


@pytest.fixture
def populated_state():
    return {
        "pacing": {"episode": "Session 10", "beat": "Tunnel Entry", "responses": 2, "notes": "building tension"},
        "callback_ledger": {
            "next_id": 4,
            "open": [
                {"id": 1, "created_turn": 5, "original_text": "Kira promised to show the tunnels", "source_npc": "Kira"},
                {"id": 3, "created_turn": 12, "original_text": "Signal from abandoned sector", "source_npc": None},
            ],
            "recently_resolved": [
                {"id": 2, "created_turn": 8, "resolved_turn": 14, "original_text": "Vex said he'd get the parts", "source_npc": "Vex", "resolution_text": "Parts delivered"},
            ]
        },
        "npc_memories": {
            "Kira": [
                {"text": "Led the party into the tunnels", "quote": "Follow me.", "date": "Day 12", "impact": 4, "tier": "high", "turn_created": 10},
                {"text": "Shared a meal together", "quote": None, "date": "Day 11", "impact": 2, "tier": "flavor", "turn_created": 8},
            ]
        },
        "scene_state": {
            "location": "Underground tunnels",
            "npcs_present": ["Kira"],
            "active_tensions": ["Tunnel flooding"],
            "scene_trigger": "Entered the tunnels",
            "atmosphere": "Dark and damp",
            "details": ["Water rising"],
            "pending_actions": ["Find the exit"]
        },
        "character_states": {
            "Aedina": {"state": "42/50 HP, AC 15", "last_updated": 14},
            "Orrophim": {"state": "35/38 HP, AC 17", "last_updated": 14},
        },
        "turn_counter": 15
    }


# ============================================================
# Per-Section Parser Tests
# ============================================================

class TestParsePacingSection:
    def test_basic(self):
        lines = [
            "episode: Session 12",
            "beat: Tunnel Exploration",
            "responses: 3",
            "notes: tension building",
        ]
        result = _parse_pacing_section(lines)
        assert result["episode"] == "Session 12"
        assert result["beat"] == "Tunnel Exploration"
        assert result["responses"] == 3
        assert result["notes"] == "tension building"

    def test_empty(self):
        assert _parse_pacing_section([]) == {}

    def test_blank_lines(self):
        lines = ["", "episode: Test", ""]
        result = _parse_pacing_section(lines)
        assert result["episode"] == "Test"


class TestParseCallbacksSection:
    def test_add(self):
        lines = ['+ "Kira promised to reveal the artifact" | source: Kira']
        ops = _parse_callbacks_section(lines)
        assert len(ops) == 1
        assert ops[0]["action"] == "add"
        assert ops[0]["original_text"] == "Kira promised to reveal the artifact"
        assert ops[0]["source_npc"] == "Kira"

    def test_add_null_source(self):
        lines = ['+ "Strange signal detected" | source: null']
        ops = _parse_callbacks_section(lines)
        assert ops[0]["source_npc"] is None

    def test_add_no_source(self):
        lines = ['+ "Something happened"']
        ops = _parse_callbacks_section(lines)
        assert ops[0]["source_npc"] is None

    def test_resolve(self):
        lines = ['RESOLVE #5: "They found the tunnels as promised"']
        ops = _parse_callbacks_section(lines)
        assert len(ops) == 1
        assert ops[0]["action"] == "resolve"
        assert ops[0]["id"] == 5
        assert ops[0]["resolution_text"] == "They found the tunnels as promised"

    def test_mixed(self):
        lines = [
            '+ "New hook" | source: Vex',
            'RESOLVE #1: "Resolved the issue"',
        ]
        ops = _parse_callbacks_section(lines)
        assert len(ops) == 2
        assert ops[0]["action"] == "add"
        assert ops[1]["action"] == "resolve"

    def test_empty(self):
        assert _parse_callbacks_section([]) == []


class TestParseMemoriesSection:
    def test_add(self):
        lines = ['+ Kira [4] "Explored the tunnels together" | "This changes everything." | Day 12']
        ops = _parse_memories_section(lines)
        assert len(ops) == 1
        op = ops[0]
        assert op["action"] == "add"
        assert op["npc"] == "Kira"
        assert op["impact"] == 4
        assert op["text"] == "Explored the tunnels together"
        assert op["quote"] == "This changes everything."
        assert op["date"] == "Day 12"

    def test_add_no_quote(self):
        lines = ['+ Vex [2] "Had a casual conversation" | | Day 10']
        ops = _parse_memories_section(lines)
        # With empty quote, quotes list will have ["Had a casual conversation", ""]
        assert ops[0]["npc"] == "Vex"
        assert ops[0]["impact"] == 2

    def test_add_two_pipes_no_quote(self):
        """Date should be extracted even with only 2 pipe segments (text | date, no quote)."""
        lines = ['+ Kira [3] "Shared a meal together" | Day 10']
        ops = _parse_memories_section(lines)
        assert ops[0]["date"] == "Day 10"
        assert ops[0]["text"] == "Shared a meal together"
        # quote should be None since the second segment is a date not a quote
        assert ops[0]["quote"] is None

    def test_drop(self):
        lines = ['- Vex [2]']
        ops = _parse_memories_section(lines)
        assert len(ops) == 1
        assert ops[0]["action"] == "drop"
        assert ops[0]["npc"] == "Vex"
        assert ops[0]["index"] == 2

    def test_empty(self):
        assert _parse_memories_section([]) == []


class TestParseSceneSection:
    def test_full(self):
        lines = [
            "location: Underground tunnels, main chamber",
            "npcs_present: Kira, Ancient Guardian",
            "tensions: Guardian awakening, tunnel flooding",
            "trigger: Activated the artifact pedestal",
            "atmosphere: Blue bioluminescent glow, rumbling",
            "details: Artifact emitting pulse, exit partially blocked",
            "pending: Guardian's first action",
        ]
        result = _parse_scene_section(lines)
        assert result["location"] == "Underground tunnels, main chamber"
        assert result["npcs_present"] == ["Kira", "Ancient Guardian"]
        assert result["active_tensions"] == ["Guardian awakening", "tunnel flooding"]
        assert result["scene_trigger"] == "Activated the artifact pedestal"
        assert result["atmosphere"] == "Blue bioluminescent glow, rumbling"
        assert result["details"] == ["Artifact emitting pulse", "exit partially blocked"]
        assert result["pending_actions"] == ["Guardian's first action"]

    def test_empty(self):
        assert _parse_scene_section([]) == {}

    def test_none_list(self):
        lines = ["npcs_present: (none)"]
        result = _parse_scene_section(lines)
        assert result["npcs_present"] == []


class TestParseCharactersSection:
    def test_basic(self):
        lines = [
            "Aedina: 42/50 HP, AC 15, 2/3 L1 slots",
            "Orrophim: 35/38 HP, AC 17, rage 1/3",
        ]
        result = _parse_characters_section(lines)
        assert result["Aedina"] == "42/50 HP, AC 15, 2/3 L1 slots"
        assert result["Orrophim"] == "35/38 HP, AC 17, rage 1/3"

    def test_empty(self):
        assert _parse_characters_section([]) == {}


# ============================================================
# Full Block Parser Tests
# ============================================================

class TestParseStateUpdatesBlock:
    def test_full_block(self):
        text = """PACING:
episode: Session 12
beat: Tunnel Exploration
responses: 3
notes: tension building

CALLBACKS:
+ "Kira promised to reveal the artifact's origin" | source: Kira
RESOLVE #5: "They found the tunnels as promised"

MEMORIES:
+ Kira [4] "Explored the tunnels together" | "This changes everything." | Day 12
- Vex [2]

SCENE:
location: Underground tunnels, main chamber
npcs_present: Kira, Ancient Guardian
tensions: Guardian awakening, tunnel flooding
trigger: Activated the artifact pedestal
atmosphere: Blue bioluminescent glow, rumbling
details: Artifact emitting pulse, exit partially blocked
pending: Guardian's first action

CHARACTERS:
Aedina: 42/50 HP, AC 15, 2/3 L1 slots, Shield of Faith active
Orrophim: 35/38 HP, AC 17, rage 1/3"""

        result = parse_state_updates_block(text, current_turn=16)
        assert result["pacing"]["episode"] == "Session 12"
        assert result["pacing"]["responses"] == 3
        assert len(result["callback_ops"]) == 2
        assert result["callback_ops"][0]["action"] == "add"
        assert result["callback_ops"][1]["action"] == "resolve"
        assert len(result["npc_memory_ops"]) == 2
        assert result["scene_state"]["location"] == "Underground tunnels, main chamber"
        assert result["scene_state"]["npcs_present"] == ["Kira", "Ancient Guardian"]
        assert result["character_states"]["Aedina"] == "42/50 HP, AC 15, 2/3 L1 slots, Shield of Faith active"
        assert result["character_states"]["Orrophim"] == "35/38 HP, AC 17, rage 1/3"

    def test_partial_sections(self):
        text = """PACING:
episode: Session 12
beat: Exploration
responses: 1
notes: calm

SCENE:
location: Town square
npcs_present: Guard
tensions: none yet
trigger: Arrived in town
atmosphere: Sunny

CHARACTERS:
Aedina: 50/50 HP"""

        result = parse_state_updates_block(text, current_turn=5)
        assert result["pacing"] is not None
        assert result["callback_ops"] is None
        assert result["npc_memory_ops"] is None
        assert result["scene_state"] is not None
        assert result["character_states"] is not None

    def test_empty_text(self):
        result = parse_state_updates_block("", current_turn=1)
        assert result["pacing"] is None
        assert result["callback_ops"] is None
        assert result["npc_memory_ops"] is None
        assert result["scene_state"] is None
        assert result["character_states"] is None

    def test_missing_all_sections(self):
        result = parse_state_updates_block("just random text", current_turn=1)
        assert all(v is None for v in result.values())

    def test_alternate_section_headers(self):
        """Model might output 'SCENE STATE:' instead of 'SCENE:', etc."""
        text = """PACING:
episode: Test
beat: X
responses: 1
notes: n

SCENE STATE:
location: Town
npcs_present: Guard

CHARACTER STATES:
Hero: 50/50 HP"""

        result = parse_state_updates_block(text, current_turn=1)
        assert result["pacing"] is not None
        assert result["scene_state"]["location"] == "Town"
        assert result["character_states"]["Hero"] == "50/50 HP"


# ============================================================
# StateInterceptor Tests
# ============================================================

class TestStateInterceptor:
    def test_no_state_block(self):
        interceptor = StateInterceptor()
        safe1 = interceptor.feed("Hello world")
        safe2 = interceptor.feed(" more text")
        remaining, state = interceptor.finalize()
        # All text should be yielded (minus the delimiter-length buffer)
        full = safe1 + safe2 + remaining
        assert full == "Hello world more text"
        assert state is None

    def test_with_state_block(self):
        interceptor = StateInterceptor()
        narrative = "The story continues."
        state_content = "PACING:\nepisode: Test\nbeat: X\nresponses: 1\nnotes: n"
        full_text = f"{narrative}\n[STATE UPDATES]\n{state_content}\n[/STATE UPDATES]"

        # Feed the whole thing in one go
        safe = interceptor.feed(full_text)
        remaining, state = interceptor.finalize()
        full_narrative = safe + remaining
        assert full_narrative == narrative
        assert state is not None
        assert "episode: Test" in state

    def test_incremental_feed(self):
        interceptor = StateInterceptor()
        narrative = "The party explores."
        state_text = "PACING:\nepisode: S1\nbeat: B1\nresponses: 1\nnotes: n"
        full = f"{narrative}\n[STATE UPDATES]\n{state_text}\n[/STATE UPDATES]"

        # Feed character by character
        safe_parts = []
        for ch in full:
            result = interceptor.feed(ch)
            if result:
                safe_parts.append(result)

        remaining, state = interceptor.finalize()
        full_narrative = "".join(safe_parts) + remaining
        assert full_narrative == narrative
        assert state is not None
        assert "episode: S1" in state

    def test_partial_delimiter_buffering(self):
        interceptor = StateInterceptor()
        # Feed text that contains a partial match for the delimiter
        safe1 = interceptor.feed("text\n[STATE")
        safe2 = interceptor.feed(" UPDATES]\ncontent")
        # The partial delimiter should be held back then matched
        remaining, state = interceptor.finalize()
        # State block was detected
        assert state is not None or (safe1 + safe2 + remaining) == "text"

    def test_finalize_without_closing_tag(self):
        interceptor = StateInterceptor()
        text = "narrative\n[STATE UPDATES]\nPACING:\nepisode: Test"
        safe = interceptor.feed(text)
        remaining, state = interceptor.finalize()
        # Should still extract what's there
        assert state is not None
        assert "PACING:" in state


# ============================================================
# build_single_agent_injections Tests
# ============================================================

class TestBuildSingleAgentInjections:
    def test_empty_state(self, fresh_state):
        result = build_single_agent_injections(fresh_state)
        assert result == ""

    def test_populated_state(self, populated_state):
        result = build_single_agent_injections(populated_state)
        assert "[PIPELINE STATE]" in result
        assert "[CALLBACK LEDGER]" in result
        assert "[NPC MEMORIES: Kira]" in result
        assert "[SCENE STATE]" in result
        assert "[CHARACTER STATES]" in result

    def test_no_context_updates(self, fresh_state):
        """CONTEXT UPDATES injection was removed — verify it never appears."""
        result = build_single_agent_injections(fresh_state)
        assert "[CONTEXT UPDATES]" not in result

    def test_pacing_only(self):
        state = _fresh_pipeline_state()
        state["pacing"] = {"episode": "S1", "beat": "B1"}
        result = build_single_agent_injections(state)
        assert "[PIPELINE STATE]" in result
        assert "[CALLBACK LEDGER]" not in result


# ============================================================
# apply_single_agent_state_updates Tests
# ============================================================

class TestApplySingleAgentStateUpdates:
    def test_full_apply(self, fresh_state):
        parsed = {
            "pacing": {"episode": "Session 1", "beat": "Opening", "responses": 1, "notes": "start"},
            "callback_ops": [{"action": "add", "original_text": "Test callback", "source_npc": "NPC1"}],
            "npc_memory_ops": [{"action": "add", "npc": "NPC1", "text": "Met the party", "quote": "Hello!", "date": "Day 1", "impact": 3}],
            "scene_state": {
                "location": "Town square",
                "npcs_present": ["NPC1"],
                "active_tensions": ["Tax collectors"],
                "scene_trigger": "Arrived in town",
                "atmosphere": "Busy market day",
                "details": ["Market stalls"],
                "pending_actions": ["Meet the mayor"]
            },
            "character_states": {"Hero": "50/50 HP, AC 16"},
        }
        result = apply_single_agent_state_updates(fresh_state, parsed, current_turn=1)
        assert result["pacing"]["episode"] == "Session 1"
        assert len(result["callback_ledger"]["open"]) == 1
        assert "NPC1" in result["npc_memories"]
        assert result["scene_state"]["location"] == "Town square"
        assert result["character_states"]["Hero"]["state"] == "50/50 HP, AC 16"

    def test_partial_apply(self, populated_state):
        """Only pacing and scene — no callbacks, memories, or characters."""
        parsed = {
            "pacing": {"episode": "Session 11", "beat": "New Beat", "responses": 1, "notes": "updated"},
            "callback_ops": None,
            "npc_memory_ops": None,
            "scene_state": {
                "location": "New location",
                "npcs_present": ["Kira"],
                "active_tensions": [],
                "scene_trigger": "Moved",
                "atmosphere": "Quiet",
                "details": [],
                "pending_actions": []
            },
            "character_states": None,
        }
        result = apply_single_agent_state_updates(populated_state, parsed, current_turn=16)
        assert result["pacing"]["episode"] == "Session 11"
        assert result["scene_state"]["location"] == "New location"
        # Callbacks and memories should be unchanged
        assert len(result["callback_ledger"]["open"]) == 2

    def test_round_trip(self, populated_state):
        """Inject state → parse output → apply → inject again → verify consistency."""
        # Build injections from populated state
        injections = build_single_agent_injections(populated_state)
        assert "[PIPELINE STATE]" in injections

        # Simulate model output
        state_output = """PACING:
episode: Session 10
beat: Tunnel Entry
responses: 3
notes: building tension

SCENE:
location: Underground tunnels
npcs_present: Kira
tensions: Tunnel flooding
trigger: Entered the tunnels
atmosphere: Dark and damp
details: Water rising
pending: Find the exit

CHARACTERS:
Aedina: 42/50 HP, AC 15
Orrophim: 35/38 HP, AC 17"""

        state_copy = copy.deepcopy(populated_state)
        parsed = parse_state_updates_block(state_output, current_turn=16)
        updated = apply_single_agent_state_updates(state_copy, parsed, current_turn=16)

        # Verify state is reasonable
        assert updated["pacing"]["responses"] == 3
        assert updated["scene_state"]["location"] == "Underground tunnels"

        # Build injections again — should still work
        injections2 = build_single_agent_injections(updated)
        assert "[PIPELINE STATE]" in injections2
        assert "[SCENE STATE]" in injections2
        assert "[CHARACTER STATES]" in injections2


# ============================================================
# Scene State Merge Tests
# ============================================================

class TestApplySceneStateMerge:
    def test_partial_preserves_existing(self):
        """Partial scene update preserves existing keys not in the new dict."""
        existing = {
            "location": "Town square",
            "npcs_present": ["Guard"],
            "active_tensions": ["Tax revolt"],
            "scene_trigger": "Arrived in town",
            "atmosphere": "Sunny",
            "details": ["Market stalls"],
            "pending_actions": ["Meet the mayor"]
        }
        new = {
            "location": "Town square",
            "npcs_present": ["Guard", "Merchant"],
            "active_tensions": ["Tax revolt"],
            "atmosphere": "Cloudy",
        }
        result = apply_scene_state(new, existing_scene=existing)
        assert result["location"] == "Town square"
        assert result["npcs_present"] == ["Guard", "Merchant"]
        assert result["atmosphere"] == "Cloudy"
        # These were absent in new — preserved from existing
        assert result["scene_trigger"] == "Arrived in town"
        assert result["details"] == ["Market stalls"]
        assert result["pending_actions"] == ["Meet the mayor"]

    def test_full_replaces_all(self):
        """Full scene update replaces all values."""
        existing = {
            "location": "Old place",
            "npcs_present": ["NPC1"],
            "active_tensions": ["Old tension"],
            "scene_trigger": "Old trigger",
            "atmosphere": "Old atmo",
            "details": ["Old detail"],
            "pending_actions": ["Old action"]
        }
        new = {
            "location": "New place",
            "npcs_present": ["NPC2"],
            "active_tensions": ["New tension"],
            "scene_trigger": "New trigger",
            "atmosphere": "New atmo",
            "details": ["New detail"],
            "pending_actions": ["New action"]
        }
        result = apply_scene_state(new, existing_scene=existing)
        assert result["location"] == "New place"
        assert result["npcs_present"] == ["NPC2"]
        assert result["scene_trigger"] == "New trigger"
        assert result["details"] == ["New detail"]

    def test_backward_compat_no_existing(self):
        """Without existing_scene, behaves like before (defaults for missing keys)."""
        new = {
            "location": "Town",
            "npcs_present": ["Guard"],
            "active_tensions": [],
            "atmosphere": "Calm",
        }
        result = apply_scene_state(new)
        assert result["location"] == "Town"
        assert result["scene_trigger"] == ""
        assert result["details"] == []
        assert result["pending_actions"] == []


# ============================================================
# Pacing Merge Tests
# ============================================================

class TestPacingMerge:
    def test_partial_preserves_existing_keys(self):
        """Pacing merge preserves keys not in the new dict."""
        state = _fresh_pipeline_state()
        state["pacing"] = {"episode": "Session 1", "beat": "Opening", "responses": 3, "notes": "tension"}
        parsed = {
            "pacing": {"episode": "Session 1", "beat": "Battle", "responses": 1},
            # notes omitted
        }
        result = apply_single_agent_state_updates(state, parsed, current_turn=2)
        assert result["pacing"]["beat"] == "Battle"
        assert result["pacing"]["responses"] == 1
        assert result["pacing"]["notes"] == "tension"  # preserved

    def test_full_pacing_replaces(self):
        """Full pacing dict replaces all values."""
        state = _fresh_pipeline_state()
        state["pacing"] = {"episode": "S1", "beat": "B1", "responses": 5, "notes": "old"}
        parsed = {
            "pacing": {"episode": "S2", "beat": "B2", "responses": 1, "notes": "new"},
        }
        result = apply_single_agent_state_updates(state, parsed, current_turn=2)
        assert result["pacing"]["episode"] == "S2"
        assert result["pacing"]["notes"] == "new"


# ============================================================
# Tool Input Processing Tests
# ============================================================

class TestToolInputProcessing:
    def test_tool_input_applied(self):
        """Tool input dict (from forced tool_use) applies correctly via apply_single_agent_state_updates."""
        state = _fresh_pipeline_state()
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Session 5", "beat": "Combat", "responses": 1},
            "scene_state": {
                "location": "Battlefield",
                "npcs_present": ["Goblin Chief"],
                "active_tensions": ["Ambush"],
                "atmosphere": "Dust and chaos",
            },
            "character_states": {"Hero": "45/50 HP, AC 16"},
            "callback_ops": [{"action": "add", "original_text": "Goblin Chief threatened revenge", "source_npc": "Goblin Chief"}],
            "npc_memory_ops": [{"action": "add", "npc": "Goblin Chief", "text": "Attacked the party", "impact": 4, "date": "Day 5", "focus": "Hero"}],
        }
        result = apply_single_agent_state_updates(state, tool_input, current_turn=1)
        assert result["pacing"]["episode"] == "Session 5"
        assert result["scene_state"]["location"] == "Battlefield"
        assert result["scene_state"]["atmosphere"] == "Dust and chaos"
        assert result["character_states"]["Hero"]["state"] == "45/50 HP, AC 16"
        assert len(result["callback_ledger"]["open"]) == 1
        assert "Goblin Chief" in result["npc_memories"]

    def test_tool_input_empty_ops(self):
        """Tool input with empty callback_ops and npc_memory_ops doesn't crash."""
        state = _fresh_pipeline_state()
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Session 1", "beat": "Intro", "responses": 1},
            "scene_state": {
                "location": "Tavern",
                "npcs_present": [],
                "active_tensions": [],
                "atmosphere": "Cozy",
            },
            "character_states": {},
            "callback_ops": [],
            "npc_memory_ops": [],
        }
        result = apply_single_agent_state_updates(state, tool_input, current_turn=1)
        assert result["pacing"]["beat"] == "Intro"
        assert result["scene_state"]["location"] == "Tavern"
        assert len(result["callback_ledger"]["open"]) == 0

    def test_tool_input_no_optional_arrays(self):
        """Tool input without callback_ops and npc_memory_ops (omitted entirely)."""
        state = _fresh_pipeline_state()
        tool_input = {
            "is_ooc": False,
            "pacing": {"episode": "Session 1", "beat": "Intro", "responses": 1},
            "scene_state": {
                "location": "Tavern",
                "npcs_present": [],
                "active_tensions": [],
                "atmosphere": "Cozy",
            },
            "character_states": {"Hero": "50/50 HP"},
        }
        result = apply_single_agent_state_updates(state, tool_input, current_turn=1)
        assert result["pacing"]["beat"] == "Intro"
        assert result["character_states"]["Hero"]["state"] == "50/50 HP"


# ============================================================
# Memory Focus Field Tests
# ============================================================

class TestMemoryFocusField:
    def test_focus_stored_on_add(self):
        """The focus field is stored when adding an NPC memory."""
        memories = {}
        ops = [{"action": "add", "npc": "Kira", "text": "Warned about the cave", "impact": 3, "date": "Day 5", "focus": "Cave of Echoes"}]
        result = apply_npc_memory_ops(memories, ops, current_turn=5)
        assert len(result["Kira"]) == 1
        assert result["Kira"][0]["focus"] == "Cave of Echoes"

    def test_focus_none_when_absent(self):
        """Focus is None when not provided in the op."""
        memories = {}
        ops = [{"action": "add", "npc": "Vex", "text": "Sold a potion", "impact": 1, "date": "Day 3"}]
        result = apply_npc_memory_ops(memories, ops, current_turn=3)
        assert result["Vex"][0]["focus"] is None
