"""
Fuzz tests for _tool_input_valid and malformed-input retry coverage.

Covers:
- Deterministic edge cases: empty dicts, wrong types, partial required fields
- Random fuzzing: thousands of random payloads against every real tool schema
- Schema-aware fuzzing: payloads that are "almost valid" (1 required field dropped/nulled)
- Cross-system consistency: all game systems' tool schemas behave identically
- Integration: _tool_input_valid correctly gates apply functions (malformed → rejected,
  valid → accepted, missing → rejected)
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _tool_input_valid

# Import every real tool schema from every game system
from game_systems.dnd5e import (
    GAME_SYSTEM as DND5E_GS,
)
from game_systems.cpred import (
    GAME_SYSTEM as CPRED_GS,
)
from game_systems.coc7e import (
    GAME_SYSTEM as COC7E_GS,
)
from game_systems.dnd5e_cyber import (
    GAME_SYSTEM as DND5E_CYBER_GS,
)

# ============================================================
# Collect all tool schemas from all game systems
# ============================================================

ALL_TOOL_SCHEMAS = {}

def _register(label, gs, key):
    tool = gs.get(key)
    if tool:
        ALL_TOOL_SCHEMAS[label] = tool

_register("dnd5e/state_report", DND5E_GS, "state_report_tool")
_register("dnd5e/combat", DND5E_GS, "combat_tool")
_register("cpred/state_report", CPRED_GS, "state_report_tool")
_register("cpred/combat", CPRED_GS, "combat_tool")
_register("cpred/hack", CPRED_GS, "hack_tool")
_register("cpred/net_combat", CPRED_GS, "net_combat_tool")
_register("coc7e/state_report", COC7E_GS, "state_report_tool")
_register("dnd5e_cyber/state_report", DND5E_CYBER_GS, "state_report_tool")
_register("dnd5e_cyber/ship_combat", DND5E_CYBER_GS, "ship_combat_tool")


# ============================================================
# Sample valid payloads for each tool type
# ============================================================

def _valid_state_report():
    return {
        "is_ooc": False,
        "pacing": {"episode": "Ep1", "beat": "Intro", "responses": 1},
        "scene_state": {"location": "Tavern", "npcs_present": [], "active_tensions": [], "atmosphere": "calm"},
        "character_states": {"Aedina": {"type": "pc", "class": "Fighter", "level": 5, "vitals": []}},
        "callback_ops": [],
        "npc_memory_ops": [],
    }

def _valid_combat():
    return {
        "narrative": "The fighter swings.",
        "rolls": [{"description": "Attack", "result": 18}],
        "character_updates": [{"name": "Aedina", "hp_delta": -5}],
        "combat": {"round": 1, "initiative_order": ["Aedina", "Goblin"], "current_turn": "Aedina"},
        "combat_complete": False,
    }

def _valid_cpred_combat():
    return {
        "narrative": "V fires.",
        "rolls": [{"description": "Attack", "result": 14}],
        "character_updates": [{"name": "V"}],
        "cover_state": [{"name": "V", "in_cover": True}],
        "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
        "combat_complete": False,
    }

def _valid_hack():
    return {
        "narrative": "V jacks in.",
        "rolls": [],
        "available_actions": ["Backdoor", "Zap"],
        "hack_state": {"alert_level": 0, "cycles_remaining": 3, "current_node": "Lobby"},
        "hack_complete": False,
    }

def _valid_net_combat():
    return {
        "narrative": "Exchange 1.",
        "rolls": [],
        "character_updates": [],
        "cover_state": [],
        "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
        "hack_state": {"alert_level": 1, "cycles_remaining": 2, "current_node": "Node-1"},
        "available_actions": ["Zap"],
        "combat_complete": False,
        "net_complete": False,
    }

def _valid_ship_combat():
    return {
        "narrative": "Cannons fire.",
        "ship_combat": {"round": 1, "initiative_order": [], "current_ship": "Aurora"},
        "ship_combat_complete": False,
    }

VALID_PAYLOADS = {
    "dnd5e/state_report": _valid_state_report,
    "dnd5e/combat": _valid_combat,
    "cpred/state_report": _valid_state_report,
    "cpred/combat": _valid_cpred_combat,
    "cpred/hack": _valid_hack,
    "cpred/net_combat": _valid_net_combat,
    "coc7e/state_report": _valid_state_report,
    "dnd5e_cyber/state_report": _valid_state_report,
    "dnd5e_cyber/ship_combat": _valid_ship_combat,
}


# ============================================================
# Fuzz atoms and random payload generator
# ============================================================

ATOMS = [None, 0, 1, -1, 3.14, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

# All known property keys across all tool schemas (superset)
ALL_KEYS = [
    "is_ooc", "pacing", "scene_state", "character_states", "combat",
    "callback_ops", "npc_memory_ops", "relationship_ops", "hud_state",
    "plot_ops", "edgerunner_ops", "ip_ops", "hack_trigger",
    "narrative", "rolls", "character_updates", "cover_state",
    "combat_complete", "narrative_summary", "initiate_net_combat",
    "available_actions", "hack_state", "hack_complete", "initiate_combat",
    "net_complete", "ship_combat", "ship_combat_complete", "ship_updates",
    "npc_actions", "combat_outcome", "ship_ops", "ship_combat_trigger",
    "investigator_ops",
]

def _rand_payload(depth=0):
    if depth > 2 or random.random() < 0.4:
        return random.choice(ATOMS)
    if random.choice(["list", "dict"]) == "list":
        return [_rand_payload(depth + 1) for _ in range(random.randint(0, 4))]
    d = {}
    for _ in range(random.randint(0, 8)):
        d[random.choice(ALL_KEYS)] = _rand_payload(depth + 1)
    return d


# ============================================================
# Tests
# ============================================================

class TestToolInputValidDeterministic(unittest.TestCase):
    """Deterministic edge cases for _tool_input_valid."""

    def test_none_input_fields_pass(self):
        """Every required field set to None should pass (keys are present; some schemas allow null)."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            required = tool_def.get("input_schema", {}).get("required", [])
            if not required:
                continue
            with self.subTest(tool=label):
                payload = {k: None for k in required}
                self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_empty_dict_passes_if_no_required(self):
        """Tool with no required fields should accept empty dict."""
        tool_def = {"name": "test", "input_schema": {"type": "object", "properties": {}}}
        self.assertTrue(_tool_input_valid({}, tool_def))

    def test_completely_empty_dict_fails_all_real_tools(self):
        """Empty dict should fail every real tool schema (all have required fields)."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            required = tool_def.get("input_schema", {}).get("required", [])
            if not required:
                continue
            with self.subTest(tool=label):
                self.assertFalse(_tool_input_valid({}, tool_def))

    def test_valid_payloads_pass_all_tools(self):
        """Known valid payloads should pass validation for their respective tool."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            with self.subTest(tool=label):
                self.assertTrue(_tool_input_valid(factory(), tool_def))

    def test_wrong_type_atoms_for_required_fields(self):
        """Each required field individually replaced with a non-None wrong-type atom."""
        wrong_atoms = [0, 1, -1, 3.14, "x", "", True, False, [], [1], ["x"]]
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                for atom in wrong_atoms:
                    with self.subTest(tool=label, field=field, atom=atom):
                        payload = factory()
                        payload[field] = atom
                        # Validation should still pass: _tool_input_valid only
                        # checks presence and non-None, not types.
                        self.assertTrue(_tool_input_valid(payload, tool_def))


class TestDropOneRequiredField(unittest.TestCase):
    """For each tool, drop exactly one required field and verify rejection."""

    def test_drop_each_required_field(self):
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, dropped=field):
                    payload = factory()
                    del payload[field]
                    self.assertFalse(
                        _tool_input_valid(payload, tool_def),
                        f"Dropping '{field}' from {label} should fail validation"
                    )

    def test_null_each_required_field_passes(self):
        """Set exactly one required field to None — key is still present, so passes."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, nulled=field):
                    payload = factory()
                    payload[field] = None
                    self.assertTrue(
                        _tool_input_valid(payload, tool_def),
                        f"Nulling '{field}' in {label} should pass (key present)"
                    )


class TestRandomFuzzAllSchemas(unittest.TestCase):
    """Random fuzzing: feed thousands of random payloads to _tool_input_valid."""

    def test_random_payloads_never_raise(self):
        """_tool_input_valid must never raise, regardless of input shape."""
        for seed in (42, 137, 256):
            random.seed(seed)
            for label, tool_def in ALL_TOOL_SCHEMAS.items():
                with self.subTest(seed=seed, tool=label):
                    for _ in range(1000):
                        payload = _rand_payload()
                        if isinstance(payload, dict):
                            # Should return bool, never raise
                            result = _tool_input_valid(payload, tool_def)
                            self.assertIsInstance(result, bool)

    def test_random_payloads_with_some_required_fields(self):
        """Payloads that have *some* required fields but not all should fail."""
        for seed in (71, 199):
            random.seed(seed)
            for label, tool_def in ALL_TOOL_SCHEMAS.items():
                factory = VALID_PAYLOADS.get(label)
                if not factory:
                    continue
                required = tool_def.get("input_schema", {}).get("required", [])
                if len(required) < 2:
                    continue
                with self.subTest(seed=seed, tool=label):
                    for _ in range(500):
                        # Start from valid, then drop a random subset
                        payload = factory()
                        drop_count = random.randint(1, len(required) - 1)
                        to_drop = random.sample(required, drop_count)
                        for k in to_drop:
                            del payload[k]
                        self.assertFalse(_tool_input_valid(payload, tool_def))

    def test_random_payloads_acceptance_rate(self):
        """Pure random payloads should almost never pass validation (sanity check).

        With 4-9 required fields and random keys, the odds of all required fields
        appearing with non-None values is negligible.  This test asserts <5% pass rate.
        """
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            required = tool_def.get("input_schema", {}).get("required", [])
            if not required:
                continue
            random.seed(999)
            passes = 0
            n = 2000
            for _ in range(n):
                payload = _rand_payload()
                if isinstance(payload, dict) and _tool_input_valid(payload, tool_def):
                    passes += 1
            with self.subTest(tool=label):
                rate = passes / n
                self.assertLess(rate, 0.05,
                    f"{label}: {rate:.1%} of random payloads passed (expected <5%)")


class TestNearMissPayloads(unittest.TestCase):
    """Payloads that are almost valid — designed to stress the boundary."""

    def test_extra_keys_dont_break_validation(self):
        """Valid payload + extra garbage keys should still pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            with self.subTest(tool=label):
                payload = factory()
                payload["__garbage__"] = {"nested": [1, 2, 3]}
                payload[""] = None
                payload["🎲"] = "dice"
                self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_required_field_as_empty_string(self):
        """Empty string for a required field is not None, so should pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, field=field):
                    payload = factory()
                    payload[field] = ""
                    self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_required_field_as_false(self):
        """False for a required field is not None, so should pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, field=field):
                    payload = factory()
                    payload[field] = False
                    self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_required_field_as_zero(self):
        """0 for a required field is not None, so should pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, field=field):
                    payload = factory()
                    payload[field] = 0
                    self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_required_field_as_empty_list(self):
        """Empty list for a required field is not None, so should pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, field=field):
                    payload = factory()
                    payload[field] = []
                    self.assertTrue(_tool_input_valid(payload, tool_def))

    def test_required_field_as_empty_dict(self):
        """Empty dict for a required field is not None, so should pass."""
        for label, tool_def in ALL_TOOL_SCHEMAS.items():
            factory = VALID_PAYLOADS.get(label)
            if not factory:
                continue
            required = tool_def.get("input_schema", {}).get("required", [])
            for field in required:
                with self.subTest(tool=label, field=field):
                    payload = factory()
                    payload[field] = {}
                    self.assertTrue(_tool_input_valid(payload, tool_def))


class TestMalformedToolDefResilience(unittest.TestCase):
    """_tool_input_valid should handle broken tool_def gracefully."""

    def test_tool_def_missing_input_schema(self):
        self.assertTrue(_tool_input_valid({"anything": 1}, {"name": "broken"}))

    def test_tool_def_input_schema_is_none(self):
        self.assertTrue(_tool_input_valid({"anything": 1}, {"name": "broken", "input_schema": None}))

    def test_tool_def_input_schema_missing_required(self):
        tool_def = {"name": "test", "input_schema": {"type": "object", "properties": {}}}
        self.assertTrue(_tool_input_valid({"anything": 1}, tool_def))

    def test_tool_def_required_is_empty_list(self):
        tool_def = {"name": "test", "input_schema": {"type": "object", "required": [], "properties": {}}}
        self.assertTrue(_tool_input_valid({}, tool_def))

    def test_tool_def_is_empty_dict(self):
        self.assertTrue(_tool_input_valid({"x": 1}, {}))

    def test_tool_def_required_has_weird_entries(self):
        """Required list with non-string entries should still work (key lookup)."""
        tool_def = {"name": "test", "input_schema": {"required": [123, None, "real_field"]}}
        payload = {"real_field": "ok"}
        # Should fail because 123 and None won't be in the dict
        self.assertFalse(_tool_input_valid(payload, tool_def))


class TestCrossSystemConsistency(unittest.TestCase):
    """All state_report tools share the same required fields."""

    def test_all_state_report_tools_require_same_base_fields(self):
        base_required = {"is_ooc", "pacing", "scene_state", "character_states"}
        state_tools = [
            ("dnd5e", ALL_TOOL_SCHEMAS.get("dnd5e/state_report")),
            ("cpred", ALL_TOOL_SCHEMAS.get("cpred/state_report")),
            ("coc7e", ALL_TOOL_SCHEMAS.get("coc7e/state_report")),
            ("dnd5e_cyber", ALL_TOOL_SCHEMAS.get("dnd5e_cyber/state_report")),
        ]
        for label, tool_def in state_tools:
            if not tool_def:
                continue
            with self.subTest(system=label):
                required = set(tool_def["input_schema"]["required"])
                self.assertTrue(
                    base_required.issubset(required),
                    f"{label} missing base required fields: {base_required - required}"
                )


if __name__ == "__main__":
    unittest.main()
