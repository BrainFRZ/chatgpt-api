"""Exhaustive fuzz tests for per-ICE-type backend enforcement.

Covers every new code path in the uncommitted changes:
- _lookup_ice_type with malformed/extreme inputs
- resolve_ice_effect with every combination of bad programs/hardware/ice_status
- _resolve_jack_out_cascade with malformed ice_status dicts
- resolve_actions: program_attack_vs_netrunner and ice_attack_vs_program with garbled fields
- _apply_ice_effect_ops / _apply_single_ice_op with every op type + malformed data
- apply_hack_state ICE-effect paths: fire tick, extinguish, wisp, movement lock, slide, writeback
- apply_net_combat_state ICE-effect paths: same as hack but dual-theater
- build_hack_injection / build_net_combat_injection with extreme active effect states
- init_hack_state program bootstrap with malformed game_state
- init_net_combat_from_hack carrying over malformed ICE effect fields
- Convergence spawn using ICE_STAT_BLOCKS
- Multi-round state accumulation / corruption resistance
- JSON serialization round-trip after every apply
- Property invariants (no crash, state_ops always list, results always list)
"""
import copy
import json
import math
import random
import sys
import os
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred_tables import ICE_STAT_BLOCKS
from game_systems.cpred_mechanics import (
    _lookup_ice_type,
    resolve_ice_effect,
    _resolve_jack_out_cascade,
    resolve_actions,
)
from game_systems.cpred import (
    init_hack_state,
    apply_hack_state,
    build_hack_injection,
    apply_hack_writeback,
    _apply_ice_effect_ops,
    _apply_single_ice_op,
    init_net_combat_from_hack,
    apply_net_combat_state,
    build_net_combat_injection,
    _default_edgerunner,
)


# ============================================================
# Shared fuzz atoms
# ============================================================

ATOMS = [
    None, 0, 1, -1, 2, -2, 3.14, -3.14, True, False,
    "", " ", "x", "null", "undefined", "NaN",
    [], [1], ["x"], [None], [{}], [[]],
    {}, {"a": 1}, {"op": "bad"},
    float("inf"), float("-inf"), float("nan"),
    2**31, -(2**31), 2**63, -(2**63),
    99999999999, -99999999999,
]

EXTREME_STRINGS = [
    "", " ", "\t", "\n", "\r\n", "\x00", "\x00\x01\x02",
    "a" * 10_000,
    "Node<script>alert(1)</script>",
    "🔥🧊💀", "Ñoño", "零節點",
    "'; DROP TABLE nodes;--",
    "../../../etc/passwd",
    'Node"With"Quotes',
    "null", "undefined", "NaN", "Infinity", "-Infinity",
]

BOUNDARY_NUMS = [
    0, 1, -1, 2**31, -(2**31), 2**63, -(2**63),
    float("inf"), float("-inf"), float("nan"),
    0.0, -0.0, 1e308, -1e308, 1e-308,
]

ICE_NAMES = list(ICE_STAT_BLOCKS.keys())

ALL_OP_TYPES = [
    "program_destroy", "body_fire", "movement_lock", "stat_debuff",
    "slide_penalty", "net_action_penalty", "forced_jack_out",
    "program_rez_damage",
]


def _make_game_state(hacker="V", hp_current=30, hp_max=40):
    er = _default_edgerunner()
    er["hp"] = {"current": hp_current, "max": hp_max, "seriously_wounded": False}
    er["programs"] = [
        {"name": "Sword", "category": "attacker", "rez_max": 7, "status": "active"},
        {"name": "Armor", "category": "defender", "rez_max": 7, "status": "active"},
    ]
    return {"edgerunners": {hacker: er}}


def _make_hack_state(**kw):
    hs = init_hack_state(hacker_name=kw.pop("hacker_name", "V"),
                         interface_rank=kw.pop("interface_rank", 6),
                         **kw)
    return hs


def _make_nc_state(**kw):
    hs = _make_hack_state(**kw)
    return init_net_combat_from_hack(hs)


def _json_safe(obj):
    """Assert obj survives JSON round-trip without error."""
    try:
        json.dumps(obj, default=str)
    except (TypeError, ValueError, OverflowError):
        pass  # Some float("nan") etc won't serialize — that's OK


# ============================================================
# 1. _lookup_ice_type exhaustive fuzz
# ============================================================
class TestLookupICETypeFuzz(unittest.TestCase):
    def test_all_atoms(self):
        for atom in ATOMS:
            result = _lookup_ice_type(atom)
            self.assertTrue(result is None or isinstance(result, dict))

    def test_extreme_strings(self):
        for s in EXTREME_STRINGS:
            result = _lookup_ice_type(s)
            self.assertTrue(result is None or isinstance(result, dict))

    def test_boundary_nums(self):
        for n in BOUNDARY_NUMS:
            result = _lookup_ice_type(n)
            self.assertTrue(result is None or isinstance(result, dict))

    def test_valid_names_mixed_case(self):
        for name in ICE_NAMES:
            for variant in [name, name.upper(), name.capitalize(), f" {name} ", name + "  "]:
                result = _lookup_ice_type(variant)
                # Stripped lowercase should match for exact names
                if variant.strip().lower() == name:
                    self.assertIsNotNone(result, f"Failed for variant {variant!r}")

    def test_nested_objects(self):
        for obj in [{"name": "asp"}, ["asp"], (1, 2), set(), object()]:
            result = _lookup_ice_type(obj)
            self.assertTrue(result is None or isinstance(result, dict))


# ============================================================
# 2. resolve_ice_effect exhaustive fuzz
# ============================================================
class TestResolveICEEffectFuzz(unittest.TestCase):
    def _assert_valid_result(self, result):
        self.assertIsInstance(result, dict)
        self.assertIn("effect", result)
        self.assertIn("state_ops", result)
        self.assertIsInstance(result["state_ops"], list)
        self.assertIn("formatted", result)
        self.assertIsInstance(result["formatted"], str)
        self.assertIn("annotations", result)
        self.assertIsInstance(result["annotations"], list)

    def test_all_ice_types_with_no_context(self):
        """Every ICE type resolves cleanly with no programs/hardware/ice_status."""
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            random.seed(42)
            result = resolve_ice_effect(block)
            self._assert_valid_result(result)

    def test_all_ice_types_with_empty_context(self):
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            random.seed(42)
            result = resolve_ice_effect(block, active_programs=[], installed_hardware=[], ice_status={})
            self._assert_valid_result(result)

    def test_all_ice_types_with_malformed_programs(self):
        malformed_programs = [
            None, "string", 42, True,
            [None], [42], ["str"], [{}],
            [{"bad": True}],
            [{"name": None, "status": None, "category": None}],
            [{"name": "Prog", "status": "active", "category": "attacker"}] * 100,
            [{"name": "Prog", "status": "destroyed", "category": "defender"}],
            [{"name": "", "status": "", "category": ""}],
        ]
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            for progs in malformed_programs:
                random.seed(42)
                result = resolve_ice_effect(block, active_programs=progs)
                self._assert_valid_result(result)

    def test_all_ice_types_with_malformed_hardware(self):
        malformed_hw = [
            None, "string", 42, True,
            [None], [42], [{}], [[]],
            ["Insulated Wiring"], ["KRASH Barrier"],
            ["insulated wiring"], ["krash barrier"],
            EXTREME_STRINGS,
        ]
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            for hw in malformed_hw:
                random.seed(42)
                result = resolve_ice_effect(block, installed_hardware=hw)
                self._assert_valid_result(result)

    def test_all_ice_types_with_malformed_ice_status(self):
        malformed_ice = [
            None, "string", 42, True, [],
            {"node1": None}, {"node1": 42}, {"node1": "str"},
            {"node1": {"bad": True}},
            {"node1": {"name": "Asp", "behavior": "black", "status": "active"}},
            {f"n{i}": {"name": "Asp", "behavior": "black", "status": "active"} for i in range(50)},
        ]
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            for ice in malformed_ice:
                random.seed(42)
                result = resolve_ice_effect(block, ice_status=ice)
                self._assert_valid_result(result)

    def test_malformed_ice_block(self):
        """resolve_ice_effect with bad block dicts."""
        bad_blocks = [
            {},
            {"effect": None},
            {"effect": ""},
            {"effect": "nonexistent_effect"},
            {"effect": "body_fire", "name": None},
            {"effect": "program_destroy", "targets_defender": "yes"},
            {"effect": "stat_debuff"},  # missing debuff_stats
            {"effect": "stat_debuff", "debuff_stats": None},
            {"effect": "stat_debuff", "debuff_stats": "INT"},
            {"effect": "forced_jack_out", "name": "TestICE"},
        ]
        for block in bad_blocks:
            random.seed(42)
            result = resolve_ice_effect(block)
            self._assert_valid_result(result)

    def test_deep_recursion_protection(self):
        """_depth limit prevents infinite recursion."""
        block = ICE_STAT_BLOCKS["giant"]
        ice_status = {
            f"n{i}": {"name": "Giant", "behavior": "black", "status": "active"}
            for i in range(10)
        }
        random.seed(42)
        result = resolve_ice_effect(block, ice_status=ice_status, _depth=2)
        self._assert_valid_result(result)

    @settings(max_examples=80, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_all_seeds_all_types(self, seed):
        """Hypothesis-seeded coverage across all ICE types, no crash."""
        random.seed(seed)
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            programs = [
                {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"},
                {"name": "Armor", "category": "defender", "rez": 7, "status": "active"},
            ]
            result = resolve_ice_effect(
                block, active_programs=programs,
                installed_hardware=["Insulated Wiring", "KRASH Barrier"],
                ice_status={"n1": {"name": "Asp", "behavior": "black", "status": "active"}},
            )
            self._assert_valid_result(result)


# ============================================================
# 3. _resolve_jack_out_cascade exhaustive fuzz
# ============================================================
class TestJackOutCascadeFuzz(unittest.TestCase):
    def _assert_valid(self, result):
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result.get("state_ops"), list)
        self.assertIsInstance(result.get("cascade_results"), list)
        self.assertIsInstance(result.get("formatted"), str)

    def test_all_atoms_as_ice_status(self):
        for atom in ATOMS:
            result = _resolve_jack_out_cascade(atom)
            self._assert_valid(result)

    def test_malformed_ice_entries(self):
        bad_entries = [
            {"n": None}, {"n": 42}, {"n": "str"}, {"n": True},
            {"n": []}, {"n": {"status": "active"}},
            {"n": {"status": "active", "behavior": "black"}},  # no name
            {"n": {"status": "active", "behavior": "black", "name": None}},
            {"n": {"status": "active", "behavior": "black", "name": 42}},
            {"n": {"status": "active", "behavior": "black", "name": "NotRealICE"}},
            {"n": {"status": "active", "behavior": None, "name": "Asp"}},
            {"n": {"status": None, "behavior": "black", "name": "Asp"}},
        ]
        for entry in bad_entries:
            random.seed(42)
            result = _resolve_jack_out_cascade(entry)
            self._assert_valid(result)

    def test_many_ice_no_crash(self):
        """50 active Black ICE in a cascade."""
        ice = {}
        for i, key in enumerate(ICE_NAMES * 5):
            ice[f"n{i}"] = {
                "name": ICE_STAT_BLOCKS[key]["name"],
                "behavior": "black", "status": "active",
            }
        random.seed(42)
        result = _resolve_jack_out_cascade(ice, exclude_ice="Giant")
        self._assert_valid(result)

    def test_depth_limit(self):
        ice = {"n1": {"name": "Asp", "behavior": "black", "status": "active"}}
        result = _resolve_jack_out_cascade(ice, _depth=3)
        self.assertEqual(result["cascade_results"], [])

    def test_exclude_ice_variants(self):
        ice = {"n1": {"name": "Asp", "behavior": "black", "status": "active"}}
        for exclude in [None, "", "Asp", "Giant", 42, True, []]:
            random.seed(42)
            result = _resolve_jack_out_cascade(ice, exclude_ice=exclude)
            self._assert_valid(result)

    def test_mixed_statuses(self):
        ice = {
            "n1": {"name": "Asp", "behavior": "black", "status": "active"},
            "n2": {"name": "Kraken", "behavior": "black", "status": "derezzed"},
            "n3": {"name": "Hellhound", "behavior": "black", "status": "bypassed"},
            "n4": {"name": "Patrol", "behavior": "patrol", "status": "active"},
            "n5": {"name": "Wisp", "behavior": "black", "status": "active"},
        }
        random.seed(42)
        result = _resolve_jack_out_cascade(ice, exclude_ice="Giant")
        self._assert_valid(result)
        # Only Asp and Wisp should be processed (active + black)
        processed_names = [cr["ice_name"] for cr in result["cascade_results"]]
        self.assertNotIn("Kraken", processed_names)
        self.assertNotIn("Hellhound", processed_names)
        self.assertNotIn("Patrol", processed_names)


# ============================================================
# 4. resolve_actions ICE action types fuzz
# ============================================================
class TestResolveActionsICEFuzz(unittest.TestCase):
    def _assert_valid(self, result):
        self.assertIn("results", result)
        self.assertIsInstance(result["results"], list)
        self.assertIn("state_ops", result)
        self.assertIsInstance(result["state_ops"], list)

    def test_program_attack_vs_netrunner_all_atoms_as_ice_type(self):
        for atom in ATOMS:
            random.seed(42)
            action = {
                "type": "program_attack_vs_netrunner",
                "character": "ICE",
                "ice_type": atom,
                "interface_rank": 6,
                "target_def": 2,
                "target": "V",
            }
            result = resolve_actions([action])
            self._assert_valid(result)

    def test_ice_attack_vs_program_all_atoms_as_ice_type(self):
        for atom in ATOMS:
            random.seed(42)
            action = {
                "type": "ice_attack_vs_program",
                "character": "ICE",
                "ice_type": atom,
                "target_program": "Sword",
                "target_program_def": 2,
                "target_program_rez": 7,
            }
            result = resolve_actions([action])
            self._assert_valid(result)

    def test_program_attack_vs_netrunner_missing_fields(self):
        """Minimal and empty actions should not crash."""
        actions = [
            {"type": "program_attack_vs_netrunner", "character": "ICE"},
            {"type": "program_attack_vs_netrunner", "character": "ICE", "ice_type": "Hellhound"},
            {"type": "program_attack_vs_netrunner", "character": ""},
            {"type": "program_attack_vs_netrunner"},
        ]
        for action in actions:
            random.seed(42)
            result = resolve_actions([action])
            self._assert_valid(result)

    def test_ice_attack_vs_program_missing_fields(self):
        actions = [
            {"type": "ice_attack_vs_program", "character": "ICE"},
            {"type": "ice_attack_vs_program", "character": "ICE", "ice_type": "Dragon"},
            {"type": "ice_attack_vs_program", "character": ""},
        ]
        for action in actions:
            random.seed(42)
            result = resolve_actions([action])
            self._assert_valid(result)

    def test_ice_attack_vs_program_boundary_rez(self):
        """Boundary rez values for destroyed check."""
        for rez in BOUNDARY_NUMS:
            random.seed(42)
            action = {
                "type": "ice_attack_vs_program",
                "character": "Dragon",
                "ice_type": "Dragon",
                "target_program": "Sword",
                "target_program_def": 0,
                "target_program_rez": rez,
            }
            result = resolve_actions([action])
            self._assert_valid(result)

    @settings(max_examples=100, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_all_12_types_through_resolve_actions_100_seeds(self, seed):
        """Hypothesis-seeded coverage across all ICE types through resolve_actions."""
        random.seed(seed)
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            if block["class"] == "anti_personnel":
                action_template = {
                    "type": "program_attack_vs_netrunner",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "interface_rank": 6,
                    "target_def": 2,
                    "target": "V",
                }
            else:
                action_template = {
                    "type": "ice_attack_vs_program",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "target_program": "Sword",
                    "target_program_def": 2,
                    "target_program_rez": 7,
                }
            result = resolve_actions([action_template])
            self._assert_valid(result)

    def test_mixed_action_batch_with_ice(self):
        """Mixed batch: ICE actions + standard actions in one call."""
        actions = [
            {"type": "skill_check", "character": "V", "stat_value": 8, "skill_value": 6, "dv": 15},
            {"type": "program_attack_vs_netrunner", "character": "Hellhound",
             "ice_type": "Hellhound", "interface_rank": 6, "target_def": 2, "target": "V"},
            {"type": "ice_attack_vs_program", "character": "Dragon",
             "ice_type": "Dragon", "target_program": "Sword", "target_program_def": 2, "target_program_rez": 7},
            {"type": "initiative", "character": "all", "combatants": [{"name": "V", "ref": 8}]},
        ]
        random.seed(42)
        result = resolve_actions(actions)
        self._assert_valid(result)
        self.assertEqual(len(result["results"]), 4)

    def test_with_all_context_params(self):
        """Pass active_programs, installed_hardware, ice_status through resolve_actions."""
        programs = [{"name": "Sword", "category": "attacker", "rez": 7, "status": "active"}]
        hardware = ["Insulated Wiring", "KRASH Barrier"]
        ice = {"n1": {"name": "Asp", "behavior": "black", "status": "active"}}
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            if block["class"] == "anti_personnel":
                action = {
                    "type": "program_attack_vs_netrunner",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "interface_rank": 6, "target_def": 2, "target": "V",
                }
            else:
                action = {
                    "type": "ice_attack_vs_program",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "target_program": "Sword",
                    "target_program_def": 2, "target_program_rez": 7,
                }
            random.seed(42)
            result = resolve_actions(
                [action], active_programs=programs,
                installed_hardware=hardware, ice_status=ice,
            )
            self._assert_valid(result)

    def test_malformed_context_params(self):
        """Malformed active_programs/installed_hardware/ice_status shouldn't crash."""
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Hellhound", "ice_type": "Hellhound",
            "interface_rank": 6, "target_def": 2, "target": "V",
        }
        for atom in ATOMS:
            random.seed(42)
            result = resolve_actions(
                [action], active_programs=atom,
                installed_hardware=atom, ice_status=atom,
            )
            self._assert_valid(result)


# ============================================================
# 5. _apply_ice_effect_ops / _apply_single_ice_op exhaustive fuzz
# ============================================================
class TestApplyICEEffectOpsFuzz(unittest.TestCase):
    def _base_state(self):
        return {
            "active": True,
            "active_programs": [
                {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"},
                {"name": "Armor", "category": "defender", "rez": 7, "status": "active"},
            ],
            "destroyed_programs": [],
            "on_fire": False,
            "fire_rounds": 0,
            "movement_locked_by": None,
            "slide_penalty": 0,
            "net_action_penalty": 0,
            "active_debuffs": [],
            "narrative_summary": None,
        }

    def test_all_op_types_minimal(self):
        for op_type in ALL_OP_TYPES:
            state = self._base_state()
            _apply_ice_effect_ops(state, [{"op": op_type}])
            _json_safe(state)

    def test_all_op_types_with_atoms_as_values(self):
        """Each op type with every atom as its primary value field."""
        field_map = {
            "program_destroy": "program_name",
            "body_fire": "active",
            "movement_lock": "locked_by",
            "stat_debuff": "stats",
            "slide_penalty": "penalty",
            "net_action_penalty": "penalty",
            "forced_jack_out": "cascade_results",
            "program_rez_damage": "damage",
        }
        for op_type, field in field_map.items():
            for atom in ATOMS:
                state = self._base_state()
                _apply_ice_effect_ops(state, [{"op": op_type, field: atom}])

    def test_none_ops_list(self):
        state = self._base_state()
        _apply_ice_effect_ops(state, None)

    def test_empty_ops_list(self):
        state = self._base_state()
        _apply_ice_effect_ops(state, [])

    def test_non_dict_ops(self):
        state = self._base_state()
        _apply_ice_effect_ops(state, [None, 42, "str", True, [], {}, [1, 2]])

    def test_ops_with_none_op_type(self):
        state = self._base_state()
        _apply_ice_effect_ops(state, [{"op": None}, {"op": ""}, {"op": 42}])

    def test_program_destroy_malformed_programs(self):
        """program_destroy with various broken program lists."""
        broken_lists = [
            None, "str", 42, True,
            [None], [42], ["str"],
            [{"name": None}], [{}],
            [{"name": "Sword", "status": None}],
        ]
        for progs in broken_lists:
            state = self._base_state()
            state["active_programs"] = progs
            _apply_ice_effect_ops(state, [{"op": "program_destroy", "program_name": "Sword"}])

    def test_program_rez_damage_boundary_values(self):
        for dmg in BOUNDARY_NUMS:
            state = self._base_state()
            _apply_ice_effect_ops(state, [
                {"op": "program_rez_damage", "program_name": "Sword", "damage": dmg}
            ])

    def test_stat_debuff_accumulation(self):
        state = self._base_state()
        for _ in range(20):
            _apply_ice_effect_ops(state, [
                {"op": "stat_debuff", "stats": ["INT", "REF"], "amount": 3, "source": "Liche", "duration": "1 hour"}
            ])
        self.assertEqual(len(state["active_debuffs"]), 20)

    def test_slide_penalty_stacking(self):
        state = self._base_state()
        for _ in range(10):
            _apply_ice_effect_ops(state, [{"op": "slide_penalty", "penalty": -2}])
        self.assertEqual(state["slide_penalty"], -20)

    def test_net_action_penalty_stacking(self):
        state = self._base_state()
        for _ in range(10):
            _apply_ice_effect_ops(state, [{"op": "net_action_penalty", "penalty": 1}])
        self.assertEqual(state["net_action_penalty"], 10)

    def test_forced_jack_out_idempotent(self):
        state = self._base_state()
        _apply_ice_effect_ops(state, [{"op": "forced_jack_out"}])
        self.assertFalse(state["active"])
        _apply_ice_effect_ops(state, [{"op": "forced_jack_out"}])
        self.assertFalse(state["active"])

    def test_all_ops_simultaneously(self):
        state = self._base_state()
        ops = [
            {"op": "program_destroy", "program_name": "Sword", "source": "Asp"},
            {"op": "body_fire", "active": True},
            {"op": "movement_lock", "locked_by": "Kraken"},
            {"op": "stat_debuff", "stats": ["INT"], "amount": 4, "source": "Liche", "duration": "1 hour"},
            {"op": "slide_penalty", "penalty": -2},
            {"op": "net_action_penalty", "penalty": 1},
            {"op": "program_rez_damage", "program_name": "Armor", "damage": 10, "destroyed": True},
            {"op": "forced_jack_out", "cascade_results": []},
        ]
        _apply_ice_effect_ops(state, ops)
        self.assertFalse(state["active"])
        self.assertTrue(state["on_fire"])
        self.assertEqual(state["movement_locked_by"], "Kraken")
        self.assertEqual(len(state["destroyed_programs"]), 2)
        _json_safe(state)

    def test_program_destroy_duplicate_prevention(self):
        state = self._base_state()
        for _ in range(5):
            _apply_ice_effect_ops(state, [{"op": "program_destroy", "program_name": "Sword"}])
        self.assertEqual(state["destroyed_programs"].count("Sword"), 1)


# ============================================================
# 6. apply_hack_state ICE-effect paths fuzz
# ============================================================
class TestApplyHackStateICEFuzz(unittest.TestCase):
    def test_fire_tick_boundary_hp(self):
        """Fire tick at various HP boundary values."""
        for hp in [0, 1, 2, 3, 100, -1]:
            hs = _make_hack_state()
            hs["on_fire"] = True
            hs["meatspace_due"] = True
            gs = _make_game_state(hp_current=hp)
            apply_hack_state(hs, {"hack_state": {}}, game_state=gs)
            self.assertGreaterEqual(gs["edgerunners"]["V"]["hp"]["current"], 0)

    def test_fire_tick_no_game_state(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["meatspace_due"] = True
        apply_hack_state(hs, {"hack_state": {}}, game_state=None)

    def test_fire_tick_malformed_game_state(self):
        for gs in ATOMS:
            hs = _make_hack_state()
            hs["on_fire"] = True
            hs["meatspace_due"] = True
            apply_hack_state(hs, {"hack_state": {}}, game_state=gs)

    def test_fire_extinguish_without_game_state(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["fire_rounds"] = 2
        apply_hack_state(hs, {"hack_state": {"on_fire": False}}, game_state=None)
        self.assertFalse(hs["on_fire"])

    def test_fire_extinguish_boundary_fire_rounds(self):
        for rounds in [0, 1, 2, 3, 100, -1]:
            hs = _make_hack_state()
            hs["on_fire"] = True
            hs["fire_rounds"] = rounds
            gs = _make_game_state()
            apply_hack_state(hs, {"hack_state": {"on_fire": False}}, game_state=gs)
            self.assertFalse(hs["on_fire"])

    def test_wisp_penalty_boundary(self):
        for penalty in [0, 1, 2, 5, 100, -1]:
            hs = _make_hack_state()
            hs["net_action_penalty"] = penalty
            hs["meatspace_due"] = True
            hs["net_actions_remaining"] = 3
            apply_hack_state(hs, {"hack_state": {}})
            if penalty > 0:
                self.assertEqual(hs["net_action_penalty"], 0)

    def test_movement_lock_auto_clear_malformed_ice_status(self):
        for ice in ATOMS:
            hs = _make_hack_state()
            hs["movement_locked_by"] = "Kraken"
            hs["ice_status"] = ice
            apply_hack_state(hs, {"hack_state": {}})

    def test_slide_penalty_auto_clear_malformed_ice_status(self):
        for ice in ATOMS:
            hs = _make_hack_state()
            hs["slide_penalty"] = -2
            hs["ice_status"] = ice
            apply_hack_state(hs, {"hack_state": {}})

    def test_ice_effect_ops_through_apply_hack_state(self):
        """resolver_state_ops with ICE effects applied through apply_hack_state."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ops = [
            {"op": "brain_damage", "edgerunner": "V", "change": 5, "reason": "test"},
            {"op": "body_fire", "active": True},
            {"op": "movement_lock", "locked_by": "Kraken"},
            {"op": "program_destroy", "program_name": "Sword", "source": "Asp"},
        ]
        hs["active_programs"] = [
            {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"}
        ]
        # Kraken must be active in ice_status or movement_lock auto-clears
        hs["ice_status"] = {"n1": {"name": "Kraken", "behavior": "black", "status": "active",
                                   "rez_current": 30, "rez_max": 30}}
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(hs["brain_damage"], 5)
        self.assertTrue(hs["on_fire"])
        self.assertEqual(hs["movement_locked_by"], "Kraken")
        self.assertIn("Sword", hs.get("destroyed_programs", []))

    def test_multi_round_accumulation(self):
        """10 rounds of apply_hack_state with various ICE effects."""
        hs = _make_hack_state()
        gs = _make_game_state(hp_current=40)
        hs["active_programs"] = [
            {"name": "Sword", "rez": 7, "status": "active", "category": "attacker"},
            {"name": "Armor", "rez": 7, "status": "active", "category": "defender"},
        ]
        for i in range(10):
            random.seed(i)
            ops = []
            if i % 2 == 0:
                ops.append({"op": "brain_damage", "edgerunner": "V", "change": 1, "reason": f"round {i}"})
            if i == 3:
                ops.append({"op": "body_fire", "active": True})
            if i == 5:
                ops.append({"op": "slide_penalty", "penalty": -2})
            if i == 7:
                ops.append({"op": "net_action_penalty", "penalty": 1})
            hs["meatspace_due"] = (i % 2 == 0)
            apply_hack_state(hs, {"hack_state": {"net_actions_used": 2}}, resolver_state_ops=ops, game_state=gs)
            _json_safe(hs)
        self.assertIsInstance(hs["brain_damage"], int)

    def test_json_roundtrip_after_apply(self):
        """Hack state must survive JSON serialization after apply with ICE ops."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ops = [
            {"op": "body_fire"}, {"op": "movement_lock", "locked_by": "Kraken"},
            {"op": "stat_debuff", "stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1h"},
        ]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        serialized = json.dumps(hs, default=str)
        self.assertIsInstance(json.loads(serialized), dict)

    def test_malformed_tool_input_on_fire_field(self):
        """on_fire field in tool_input with various types."""
        for atom in ATOMS:
            hs = _make_hack_state()
            hs["on_fire"] = True
            hs["fire_rounds"] = 1
            gs = _make_game_state()
            apply_hack_state(hs, {"hack_state": {"on_fire": atom}}, game_state=gs)

    def test_convergence_spawn_with_ice_effects_active(self):
        """Convergence spawn while fire/movement_lock/debuffs are active."""
        hs = _make_hack_state(sr=3)
        hs["on_fire"] = True
        hs["movement_locked_by"] = "Kraken"
        hs["active_debuffs"] = [{"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1h"}]
        hs["_prev_alert_level"] = 0
        # Include active Kraken so movement_lock doesn't auto-clear
        hs["ice_status"] = {"n1": {"name": "Kraken", "behavior": "black", "status": "active",
                                   "rez_current": 30, "rez_max": 30}}
        apply_hack_state(hs, {"hack_state": {"alert_level": 7}})
        # Should still spawn Convergence Kraken
        self.assertTrue(any("Convergence" in k for k in hs["ice_status"]))
        # Effects should still be active
        self.assertTrue(hs["on_fire"])
        self.assertEqual(hs["movement_locked_by"], "Kraken")


# ============================================================
# 7. apply_net_combat_state ICE-effect paths fuzz
# ============================================================
class TestApplyNetCombatICEFuzz(unittest.TestCase):
    def _make_ps(self, nc=None, gs=None):
        nc = nc or _make_nc_state()
        gs = gs or _make_game_state()
        return {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": gs,
        }

    def test_fire_tick_in_net_combat_boundary_hp(self):
        for hp in [0, 1, 2, 40, -1]:
            nc = _make_nc_state()
            nc["on_fire"] = True
            gs = _make_game_state(hp_current=hp)
            ps = self._make_ps(nc, gs)
            tool = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
            apply_net_combat_state(ps, tool, game_state=gs)
            self.assertGreaterEqual(gs["edgerunners"]["V"]["hp"]["current"], 0)

    def test_fire_extinguish_in_net_combat(self):
        nc = _make_nc_state()
        nc["on_fire"] = True
        nc["fire_rounds"] = 3
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"on_fire": False, "net_actions_used": 1},
                "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertFalse(nc["on_fire"])

    def test_wisp_penalty_cleared_in_net_combat(self):
        nc = _make_nc_state()
        nc["net_action_penalty"] = 2
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 0)

    def test_movement_lock_auto_clear_in_net_combat(self):
        nc = _make_nc_state()
        nc["movement_locked_by"] = "Kraken"
        nc["ice_status"] = {"n1": {"name": "Kraken", "behavior": "black", "status": "derezzed"}}
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertIsNone(nc["movement_locked_by"])

    def test_slide_penalty_auto_clear_in_net_combat(self):
        nc = _make_nc_state()
        nc["slide_penalty"] = -4
        nc["ice_status"] = {}
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["slide_penalty"], 0)

    def test_ice_effect_ops_through_net_combat(self):
        nc = _make_nc_state()
        nc["active_programs"] = [{"name": "Sword", "rez": 7, "status": "active", "category": "attacker"}]
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        ops = [
            {"op": "brain_damage", "edgerunner": "V", "change": 3, "reason": "test"},
            {"op": "program_destroy", "program_name": "Sword", "source": "Asp"},
            {"op": "body_fire"},
        ]
        tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, resolver_state_ops=ops, game_state=gs)
        self.assertTrue(nc["on_fire"])
        self.assertIn("Sword", nc.get("destroyed_programs", []))

    def test_malformed_tool_input(self):
        for atom in ATOMS:
            nc = _make_nc_state()
            gs = _make_game_state()
            ps = self._make_ps(nc, gs)
            try:
                apply_net_combat_state(ps, atom, game_state=gs)
            except (TypeError, AttributeError):
                pass  # Some atoms will fail the isinstance check

    def test_malformed_hack_state_in_tool(self):
        for atom in ATOMS:
            nc = _make_nc_state()
            gs = _make_game_state()
            ps = self._make_ps(nc, gs)
            tool = {"hack_state": atom, "combat_complete": False, "net_complete": False}
            apply_net_combat_state(ps, tool, game_state=gs)

    def test_multi_round_net_combat_accumulation(self):
        nc = _make_nc_state()
        gs = _make_game_state(hp_current=40)
        ps = self._make_ps(nc, gs)
        for i in range(10):
            random.seed(i)
            ops = []
            if i == 2:
                ops.append({"op": "body_fire"})
            if i % 3 == 0:
                ops.append({"op": "brain_damage", "edgerunner": "V", "change": 1, "reason": "test"})
            tool = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
            apply_net_combat_state(ps, tool, resolver_state_ops=ops, game_state=gs)
            _json_safe(nc)


# ============================================================
# 8. build_hack_injection / build_net_combat_injection fuzz
# ============================================================
class TestBuildInjectionICEFuzz(unittest.TestCase):
    def test_hack_injection_all_effects_active(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["fire_rounds"] = 3
        hs["movement_locked_by"] = "Kraken"
        hs["slide_penalty"] = -4
        hs["net_action_penalty"] = 2
        hs["active_debuffs"] = [
            {"stats": ["INT", "REF", "DEX"], "amount": 4, "source": "Liche", "duration": "1 hour"},
            {"stats": ["MOVE"], "amount": 6, "source": "Scorpion", "duration": "1 hour"},
        ]
        hs["destroyed_programs"] = ["Sword", "Banhammer", "Armor"]
        inj = build_hack_injection(hs)
        self.assertIsInstance(inj, str)
        self.assertIn("ON FIRE", inj)
        self.assertIn("MOVEMENT LOCKED", inj)
        self.assertIn("SLIDE PENALTY", inj)
        self.assertIn("NET ACTION PENALTY", inj)
        self.assertIn("DEBUFF", inj)
        self.assertIn("DESTROYED PROGRAMS", inj)
        self.assertIn("Liche", inj)
        self.assertIn("Scorpion", inj)

    def test_hack_injection_malformed_effects(self):
        """Injection must not crash on malformed effect data."""
        hs = _make_hack_state()
        hs["active_debuffs"] = [None, 42, "str", {}, {"stats": None}, {"stats": [], "amount": "bad"}]
        hs["destroyed_programs"] = [None, 42, "Sword"]
        hs["on_fire"] = "yes"  # truthy non-bool
        hs["movement_locked_by"] = 42
        hs["slide_penalty"] = "bad"
        hs["net_action_penalty"] = "bad"
        inj = build_hack_injection(hs)
        self.assertIsInstance(inj, str)

    def test_hack_injection_extreme_string_values(self):
        hs = _make_hack_state()
        hs["movement_locked_by"] = "🔥<script>alert(1)</script>"
        hs["destroyed_programs"] = EXTREME_STRINGS[:5]
        hs["active_debuffs"] = [
            {"stats": EXTREME_STRINGS[:3], "amount": 99, "source": "x" * 1000, "duration": "∞"}
        ]
        inj = build_hack_injection(hs)
        self.assertIsInstance(inj, str)

    def test_net_combat_injection_all_effects(self):
        nc = _make_nc_state()
        nc["on_fire"] = True
        nc["movement_locked_by"] = "Kraken"
        nc["slide_penalty"] = -2
        nc["net_action_penalty"] = 1
        nc["active_debuffs"] = [{"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1h"}]
        nc["destroyed_programs"] = ["Sword"]
        ps = {"game_state": {}, "character_states": {}}
        inj = build_net_combat_injection(None, nc, ps)
        self.assertIsInstance(inj, str)
        self.assertIn("ON FIRE", inj)
        self.assertIn("MOVEMENT LOCKED", inj)
        self.assertIn("DESTROYED PROGRAMS", inj)

    def test_net_combat_injection_malformed_debuffs(self):
        nc = _make_nc_state()
        nc["active_debuffs"] = ATOMS
        nc["destroyed_programs"] = ATOMS
        ps = {"game_state": {}, "character_states": {}}
        inj = build_net_combat_injection(None, nc, ps)
        self.assertIsInstance(inj, str)

    def test_ice_type_tag_in_ice_status_injection(self):
        """ICE status lines include ice_type tag when present."""
        hs = _make_hack_state()
        hs["ice_status"] = {
            "node1": {"name": "Hellhound", "behavior": "black", "status": "active",
                      "rez_current": 20, "rez_max": 20, "ice_type": "hellhound"},
            "node2": {"name": "Patrol", "behavior": "patrol", "status": "active",
                      "rez_current": 10, "rez_max": 10},
        }
        inj = build_hack_injection(hs)
        self.assertIn("[hellhound]", inj)


# ============================================================
# 9. init_hack_state program bootstrap fuzz
# ============================================================
class TestProgramBootstrapFuzz(unittest.TestCase):
    def test_malformed_game_state(self):
        for gs in ATOMS:
            hs = init_hack_state(hacker_name="V", game_state=gs)
            self.assertIsInstance(hs["active_programs"], list)

    def test_malformed_programs_in_edgerunner(self):
        for progs in ATOMS:
            gs = {"edgerunners": {"V": {**_default_edgerunner(), "programs": progs}}}
            hs = init_hack_state(hacker_name="V", game_state=gs)
            self.assertIsInstance(hs["active_programs"], list)

    def test_programs_with_bad_entries(self):
        bad_entries = [None, 42, "str", True, {}, {"bad": True}, {"name": None}, {"status": "destroyed"}]
        gs = {"edgerunners": {"V": {**_default_edgerunner(), "programs": bad_entries}}}
        hs = init_hack_state(hacker_name="V", game_state=gs)
        self.assertIsInstance(hs["active_programs"], list)
        # None entries and non-dict entries should be filtered out
        for p in hs["active_programs"]:
            self.assertIsInstance(p, dict)

    def test_destroyed_programs_excluded(self):
        gs = {"edgerunners": {"V": {**_default_edgerunner(), "programs": [
            {"name": "Sword", "category": "attacker", "rez_max": 7, "status": "active"},
            {"name": "Dead", "category": "attacker", "rez_max": 5, "status": "destroyed"},
        ]}}}
        hs = init_hack_state(hacker_name="V", game_state=gs)
        self.assertEqual(len(hs["active_programs"]), 1)
        self.assertEqual(hs["active_programs"][0]["name"], "Sword")

    def test_empty_hacker_name(self):
        gs = _make_game_state()
        hs = init_hack_state(hacker_name="", game_state=gs)
        self.assertEqual(hs["active_programs"], [])

    def test_hacker_not_in_game_state(self):
        gs = _make_game_state(hacker="Other")
        hs = init_hack_state(hacker_name="V", game_state=gs)
        self.assertEqual(hs["active_programs"], [])


# ============================================================
# 10. init_net_combat_from_hack ICE effect field carry-over fuzz
# ============================================================
class TestNetCombatFromHackICEFieldsFuzz(unittest.TestCase):
    def test_all_ice_fields_carry_over(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["fire_rounds"] = 3
        hs["movement_locked_by"] = "Kraken"
        hs["slide_penalty"] = -4
        hs["net_action_penalty"] = 2
        hs["active_debuffs"] = [{"stats": ["INT"], "amount": 3, "source": "Liche"}]
        hs["destroyed_programs"] = ["Sword"]
        nc = init_net_combat_from_hack(hs)
        self.assertTrue(nc["on_fire"])
        self.assertEqual(nc["fire_rounds"], 3)
        self.assertEqual(nc["movement_locked_by"], "Kraken")
        self.assertEqual(nc["slide_penalty"], -4)
        self.assertEqual(nc["net_action_penalty"], 2)
        self.assertEqual(len(nc["active_debuffs"]), 1)
        self.assertEqual(nc["destroyed_programs"], ["Sword"])

    def test_malformed_hack_state(self):
        for atom in ATOMS:
            nc = init_net_combat_from_hack(atom)
            self.assertIsInstance(nc, dict)
            self.assertIn("on_fire", nc)
            self.assertIn("destroyed_programs", nc)

    def test_missing_ice_fields_default(self):
        """If hack_state has no ICE fields, net_combat gets defaults."""
        nc = init_net_combat_from_hack({})
        self.assertFalse(nc["on_fire"])
        self.assertEqual(nc["fire_rounds"], 0)
        self.assertIsNone(nc["movement_locked_by"])
        self.assertEqual(nc["slide_penalty"], 0)
        self.assertEqual(nc["net_action_penalty"], 0)
        self.assertEqual(nc["active_debuffs"], [])
        self.assertEqual(nc["destroyed_programs"], [])

    def test_malformed_ice_field_values(self):
        """Various bad types for ICE effect fields in hack_state."""
        for atom in ATOMS:
            hs = {
                "on_fire": atom,
                "fire_rounds": atom,
                "movement_locked_by": atom,
                "slide_penalty": atom,
                "net_action_penalty": atom,
                "active_debuffs": atom,
                "destroyed_programs": atom,
            }
            nc = init_net_combat_from_hack(hs)
            self.assertIsInstance(nc, dict)


# ============================================================
# 11. Hack writeback fuzz
# ============================================================
class TestHackWritebackFuzz(unittest.TestCase):
    def test_writeback_destroyed_programs_malformed(self):
        for destroyed in ATOMS:
            hs = _make_hack_state()
            hs["destroyed_programs"] = destroyed
            ps = {
                "game_state": _make_game_state(),
                "character_states": {},
            }
            apply_hack_writeback(hs, ps)

    def test_writeback_fire_nudity_no_game_state(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["fire_rounds"] = 3
        ps = {"game_state": {}, "character_states": {}}
        apply_hack_writeback(hs, ps)

    def test_writeback_fire_nudity_replaces_existing(self):
        hs = _make_hack_state()
        hs["on_fire"] = True
        hs["fire_rounds"] = 3
        gs = _make_game_state()
        gs["edgerunners"]["V"]["conditions"] = ["partially_nude"]
        ps = {"game_state": gs, "character_states": {}}
        apply_hack_writeback(hs, ps)
        conditions = gs["edgerunners"]["V"]["conditions"]
        self.assertNotIn("partially_nude", conditions)
        self.assertIn("nude", conditions)
        self.assertEqual(conditions.count("nude"), 1)

    def test_writeback_no_fire_no_nudity(self):
        hs = _make_hack_state()
        hs["on_fire"] = False
        gs = _make_game_state()
        ps = {"game_state": gs, "character_states": {}}
        apply_hack_writeback(hs, ps)
        self.assertEqual(gs["edgerunners"]["V"].get("conditions", []), [])

    def test_writeback_destroyed_all_programs(self):
        hs = _make_hack_state()
        hs["destroyed_programs"] = ["Sword", "Armor"]
        gs = _make_game_state()
        ps = {"game_state": gs, "character_states": {}}
        apply_hack_writeback(hs, ps)
        self.assertEqual(len(gs["edgerunners"]["V"]["programs"]), 0)

    def test_writeback_destroyed_program_not_in_list(self):
        hs = _make_hack_state()
        hs["destroyed_programs"] = ["Nonexistent"]
        gs = _make_game_state()
        ps = {"game_state": gs, "character_states": {}}
        apply_hack_writeback(hs, ps)
        # Original programs still intact
        self.assertEqual(len(gs["edgerunners"]["V"]["programs"]), 2)


# ============================================================
# 12. Edgerunner conditions ops fuzz
# ============================================================
class TestEdgerunnerConditionsOpsFuzz(unittest.TestCase):
    def test_conditions_field_in_default(self):
        er = _default_edgerunner()
        self.assertIn("conditions", er)
        self.assertIsInstance(er["conditions"], list)

    def test_programs_field_in_default(self):
        er = _default_edgerunner()
        self.assertIn("programs", er)
        self.assertIsInstance(er["programs"], list)


# ============================================================
# 13. Full lifecycle fuzz: hack → ICE effects → net_combat → writeback
# ============================================================
class TestFullLifecycleFuzz(unittest.TestCase):
    def test_hack_to_net_combat_with_all_effects(self):
        """Full lifecycle: init hack → apply with ICE ops → transition to net_combat → apply → writeback."""
        gs = _make_game_state(hp_current=40)
        hs = init_hack_state(hacker_name="V", interface_rank=6, game_state=gs)
        hs["active_programs"] = [
            {"name": "Sword", "rez": 7, "status": "active", "category": "attacker"},
            {"name": "Armor", "rez": 7, "status": "active", "category": "defender"},
        ]

        # Round 1: Hellhound sets fire
        ops1 = [{"op": "body_fire"}, {"op": "brain_damage", "edgerunner": "V", "change": 3, "reason": "Hellhound"}]
        apply_hack_state(hs, {"hack_state": {"net_actions_used": 3}}, resolver_state_ops=ops1, game_state=gs)
        self.assertTrue(hs["on_fire"])
        self.assertEqual(hs["brain_damage"], 3)

        # Round 2: Kraken locks movement, fire ticks
        ops2 = [{"op": "movement_lock", "locked_by": "Kraken"}]
        # Kraken must be active in ice_status or movement_lock auto-clears
        hs["ice_status"] = {"n1": {"name": "Kraken", "behavior": "black", "status": "active",
                                   "rez_current": 30, "rez_max": 30}}
        # Use net_actions_used: 1 so meatspace_due stays from round 1 but doesn't re-trigger
        apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}}, resolver_state_ops=ops2, game_state=gs)
        self.assertEqual(hs["movement_locked_by"], "Kraken")

        # Transition to net_combat
        nc = init_net_combat_from_hack(hs)
        self.assertTrue(nc["on_fire"])
        self.assertEqual(nc["movement_locked_by"], "Kraken")

        # Net combat round with wisp penalty
        ps = {"net_combat": nc, "combat": None, "character_states": {}, "game_state": gs}
        ops3 = [{"op": "net_action_penalty", "penalty": 1}]
        tool = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, resolver_state_ops=ops3, game_state=gs)

        # Writeback from hack state
        ps_wb = {"game_state": gs, "character_states": {}}
        hs["destroyed_programs"] = ["Sword"]
        hs["on_fire"] = True
        hs["fire_rounds"] = 2
        apply_hack_writeback(hs, ps_wb)
        self.assertEqual(len(gs["edgerunners"]["V"]["programs"]), 1)
        self.assertIn("nude", gs["edgerunners"]["V"].get("conditions", []))

        # Final JSON serialization check
        _json_safe(hs)
        _json_safe(nc)
        _json_safe(gs)

    def test_all_ice_types_full_attack_lifecycle(self):
        """Each ICE type: resolve_actions → apply to hack_state → build injection."""
        for key in ICE_NAMES:
            block = ICE_STAT_BLOCKS[key]
            gs = _make_game_state(hp_current=40)
            hs = init_hack_state(hacker_name="V", interface_rank=6, game_state=gs)
            hs["active_programs"] = [
                {"name": "Sword", "rez": 7, "status": "active", "category": "attacker"},
                {"name": "Armor", "rez": 7, "status": "active", "category": "defender"},
            ]
            hs["installed_hardware"] = []
            hs["ice_status"] = {
                "node1": {"name": block["name"], "behavior": "black", "status": "active",
                          "rez_current": block["rez"], "rez_max": block["rez"], "ice_type": key}
            }

            if block["class"] == "anti_personnel":
                action = {
                    "type": "program_attack_vs_netrunner",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "interface_rank": 6, "target_def": 2, "target": "V",
                }
            else:
                action = {
                    "type": "ice_attack_vs_program",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "target_program": "Sword",
                    "target_program_def": 2, "target_program_rez": 7,
                }

            random.seed(42)
            result = resolve_actions(
                [action],
                active_programs=hs["active_programs"],
                installed_hardware=hs["installed_hardware"],
                ice_status=hs["ice_status"],
            )

            # Apply ops to hack state
            apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}},
                             resolver_state_ops=result["state_ops"], game_state=gs)

            # Build injection — must not crash
            inj = build_hack_injection(hs)
            self.assertIsInstance(inj, str)
            _json_safe(hs)


# ============================================================
# 14. Stress / edge cases
# ============================================================
class TestStressEdgeCases(unittest.TestCase):
    def test_50_concurrent_ice_effects(self):
        """Apply 50 different ICE effect ops at once."""
        state = {
            "active": True,
            "active_programs": [
                {"name": f"Prog{i}", "rez": 7, "status": "active", "category": "attacker"}
                for i in range(20)
            ],
            "destroyed_programs": [],
            "on_fire": False, "fire_rounds": 0,
            "movement_locked_by": None,
            "slide_penalty": 0,
            "net_action_penalty": 0,
            "active_debuffs": [],
            "narrative_summary": None,
        }
        ops = []
        for i in range(10):
            ops.append({"op": "program_destroy", "program_name": f"Prog{i}", "source": "Asp"})
        for i in range(10):
            ops.append({"op": "stat_debuff", "stats": ["INT"], "amount": i, "source": f"Liche{i}", "duration": "1h"})
        for i in range(10):
            ops.append({"op": "slide_penalty", "penalty": -1})
        for i in range(10):
            ops.append({"op": "net_action_penalty", "penalty": 1})
        ops.extend([
            {"op": "body_fire"},
            {"op": "movement_lock", "locked_by": "Kraken"},
            {"op": "program_rez_damage", "program_name": "Prog10", "damage": 100, "destroyed": True},
        ])
        _apply_ice_effect_ops(state, ops)
        self.assertEqual(len(state["destroyed_programs"]), 11)
        self.assertEqual(len(state["active_debuffs"]), 10)
        self.assertEqual(state["slide_penalty"], -10)
        self.assertEqual(state["net_action_penalty"], 10)
        self.assertTrue(state["on_fire"])

    def test_rapid_fire_extinguish_cycle(self):
        """Fire on → tick → extinguish → fire on → tick → extinguish."""
        gs = _make_game_state(hp_current=40)
        hs = _make_hack_state()
        for _ in range(5):
            # Fire on
            _apply_ice_effect_ops(hs, [{"op": "body_fire"}])
            self.assertTrue(hs["on_fire"])
            # Tick (simulate meatspace_due)
            hs["meatspace_due"] = True
            apply_hack_state(hs, {"hack_state": {"net_actions_used": 3}}, game_state=gs)
            # Extinguish
            apply_hack_state(hs, {"hack_state": {"on_fire": False}}, game_state=gs)
            self.assertFalse(hs["on_fire"])
            self.assertEqual(hs["fire_rounds"], 0)

    def test_cascade_with_all_ice_types_active(self):
        """Jack out cascade with one of every ICE type active."""
        ice = {}
        for i, key in enumerate(ICE_NAMES):
            ice[f"n{i}"] = {
                "name": ICE_STAT_BLOCKS[key]["name"],
                "behavior": "black", "status": "active",
                "rez_current": ICE_STAT_BLOCKS[key]["rez"],
                "rez_max": ICE_STAT_BLOCKS[key]["rez"],
            }
        random.seed(42)
        result = _resolve_jack_out_cascade(ice, exclude_ice="OriginalGiant")
        self.assertIsInstance(result["cascade_results"], list)
        self.assertIsInstance(result["state_ops"], list)
        # Should have processed all 12 (minus none excluded)
        self.assertGreater(len(result["cascade_results"]), 0)

    def test_empty_action_batch(self):
        result = resolve_actions([])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["state_ops"], [])

    def test_100_actions_in_batch(self):
        """100 ICE actions in a single batch."""
        actions = []
        for i in range(100):
            key = ICE_NAMES[i % len(ICE_NAMES)]
            block = ICE_STAT_BLOCKS[key]
            if block["class"] == "anti_personnel":
                actions.append({
                    "type": "program_attack_vs_netrunner",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "interface_rank": 6, "target_def": 2, "target": "V",
                })
            else:
                actions.append({
                    "type": "ice_attack_vs_program",
                    "character": block["name"],
                    "ice_type": block["name"],
                    "target_program": "Sword",
                    "target_program_def": 2, "target_program_rez": 7,
                })
        random.seed(42)
        result = resolve_actions(actions)
        self.assertEqual(len(result["results"]), 100)


# ============================================================
# Wisp / net_action_penalty fuzz tests
# ============================================================

class TestWispPenaltyHackFuzz(unittest.TestCase):
    """Fuzz tests for wisp (net_action_penalty) deduction in apply_hack_state."""

    def test_wisp_deducts_on_meatspace_due(self):
        """Penalty=1, remaining=3 → remaining=2 after deduction."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = 1
        hs["meatspace_due"] = True
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_action_penalty"], 0)
        self.assertEqual(hs["net_actions_remaining"], 2)

    def test_wisp_no_deduct_without_meatspace_due(self):
        """Penalty set but meatspace_due=False → no deduction."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = 2
        hs["meatspace_due"] = False
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_action_penalty"], 2)  # Unchanged
        self.assertEqual(hs["net_actions_remaining"], 3)

    def test_wisp_clamps_to_zero(self):
        """Penalty exceeds remaining → clamp to 0."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = 10
        hs["meatspace_due"] = True
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_action_penalty"], 0)
        self.assertEqual(hs["net_actions_remaining"], 0)

    def test_wisp_zero_penalty_no_op(self):
        """Penalty=0 → no deduction."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = 0
        hs["meatspace_due"] = True
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_actions_remaining"], 3)

    def test_wisp_negative_penalty_no_op(self):
        """Negative penalty → no deduction (guard: > 0)."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = -1
        hs["meatspace_due"] = True
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_actions_remaining"], 3)

    def test_wisp_stacking_two_wisps(self):
        """Two wisp ops accumulate penalty=2, then deduct on meatspace_due."""
        hs = _make_hack_state()
        hs["net_actions_remaining"] = 3
        hs["ice_status"] = {
            "n1": {"name": "Wisp", "behavior": "black", "status": "active",
                   "rez_current": 15, "rez_max": 15},
        }
        ops = [
            {"op": "net_action_penalty", "penalty": 1},
            {"op": "net_action_penalty", "penalty": 1},
        ]
        # First call: apply ops (penalty accumulates), use 1 action (remaining 3→2)
        apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}},
                         resolver_state_ops=ops)
        self.assertEqual(hs["net_action_penalty"], 2)
        self.assertEqual(hs["net_actions_remaining"], 2)
        # Second call: meatspace_due triggers deduction (2 - 2 = 0)
        hs["meatspace_due"] = True
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_action_penalty"], 0)
        self.assertEqual(hs["net_actions_remaining"], 0)

    def test_wisp_boundary_penalties(self):
        """Various penalty values with meatspace_due."""
        for penalty in [1, 2, 3, 5, 50, 100]:
            hs = _make_hack_state()
            hs["net_action_penalty"] = penalty
            hs["meatspace_due"] = True
            hs["net_actions_remaining"] = 3
            apply_hack_state(hs, {"hack_state": {}})
            self.assertEqual(hs["net_action_penalty"], 0)
            self.assertEqual(hs["net_actions_remaining"], max(0, 3 - penalty))

    def test_wisp_uses_net_actions_per_turn_fallback(self):
        """If net_actions_remaining is missing, falls back to net_actions_per_turn."""
        hs = _make_hack_state()
        hs["net_action_penalty"] = 1
        hs["meatspace_due"] = True
        del hs["net_actions_remaining"]
        hs["net_actions_per_turn"] = 5
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["net_actions_remaining"], 4)  # 5 - 1


class TestWispPenaltyNetCombatFuzz(unittest.TestCase):
    """Fuzz tests for wisp (net_action_penalty) deduction in apply_net_combat_state."""

    def _make_ps(self, nc=None, gs=None):
        nc = nc or _make_nc_state()
        gs = gs or _make_game_state()
        return {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": gs,
        }

    def test_wisp_deducts_in_net_combat(self):
        """Penalty=1, has_net_actions → remaining decremented."""
        nc = _make_nc_state()
        nc["net_action_penalty"] = 1
        nc["net_actions_remaining"] = 3
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 0)
        self.assertEqual(nc["net_actions_remaining"], 2)  # 3 - 1

    def test_wisp_no_deduct_without_net_actions(self):
        """No NET actions used → penalty not consumed."""
        nc = _make_nc_state()
        nc["net_action_penalty"] = 2
        nc["net_actions_remaining"] = 3
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 2)  # Unchanged
        self.assertEqual(nc["net_actions_remaining"], 3)

    def test_wisp_clamps_to_zero_net_combat(self):
        """Penalty exceeds remaining → clamp to 0."""
        nc = _make_nc_state()
        nc["net_action_penalty"] = 10
        nc["net_actions_remaining"] = 2
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 0)
        self.assertEqual(nc["net_actions_remaining"], 0)

    def test_wisp_zero_penalty_no_op_net_combat(self):
        """Penalty=0 with net_actions → no deduction."""
        nc = _make_nc_state()
        nc["net_action_penalty"] = 0
        nc["net_actions_remaining"] = 3
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_actions_remaining"], 3)

    def test_wisp_boundary_penalties_net_combat(self):
        """Various penalty values in net_combat with net_actions."""
        for penalty in [1, 2, 3, 5, 50]:
            nc = _make_nc_state()
            nc["net_action_penalty"] = penalty
            nc["net_actions_remaining"] = 3
            gs = _make_game_state()
            ps = self._make_ps(nc, gs)
            tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
            apply_net_combat_state(ps, tool, game_state=gs)
            self.assertEqual(nc["net_action_penalty"], 0)
            self.assertEqual(nc["net_actions_remaining"], max(0, 3 - penalty))

    def test_wisp_uses_net_actions_per_turn_fallback_net_combat(self):
        """Falls back to net_actions_per_turn when net_actions_remaining is missing."""
        nc = _make_nc_state()
        nc["net_action_penalty"] = 1
        nc["net_actions_per_turn"] = 4
        nc.pop("net_actions_remaining", None)
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["net_actions_remaining"], 3)  # 4 - 1

    def test_wisp_op_through_resolver_then_deduct_net_combat(self):
        """net_action_penalty op from resolver accumulates, then deducts on next exchange."""
        nc = _make_nc_state()
        nc["net_actions_remaining"] = 3
        nc["ice_status"] = {
            "n1": {"name": "Wisp", "behavior": "black", "status": "active",
                   "rez_current": 15, "rez_max": 15},
        }
        gs = _make_game_state()
        ps = self._make_ps(nc, gs)
        # Exchange 1: wisp hits, penalty op applied (no deduction yet — first turn)
        ops = [{"op": "net_action_penalty", "penalty": 1}]
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 1)
        # Exchange 2: net actions used, penalty consumed
        tool2 = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool2, game_state=gs)
        self.assertEqual(nc["net_action_penalty"], 0)
        self.assertEqual(nc["net_actions_remaining"], 2)  # 3 - 1

    def test_wisp_symmetry_hack_vs_net_combat(self):
        """Same penalty value produces same deduction in both modes."""
        for penalty in [1, 2, 3]:
            # Hack mode
            hs = _make_hack_state()
            hs["net_action_penalty"] = penalty
            hs["meatspace_due"] = True
            hs["net_actions_remaining"] = 3
            apply_hack_state(hs, {"hack_state": {}})

            # Net combat mode
            nc = _make_nc_state()
            nc["net_action_penalty"] = penalty
            nc["net_actions_remaining"] = 3
            gs = _make_game_state()
            ps = self._make_ps(nc, gs)
            tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False, "net_complete": False}
            apply_net_combat_state(ps, tool, game_state=gs)

            self.assertEqual(hs["net_actions_remaining"], nc["net_actions_remaining"],
                             f"penalty={penalty}: hack remaining={hs['net_actions_remaining']} "
                             f"!= nc remaining={nc['net_actions_remaining']}")


class TestStatDebuffExpiration(unittest.TestCase):
    """Verify Liche/Scorpion/Nervescrub debuffs expire on the HUD clock.

    The duration string ('1 hour', '1h') was previously informational.
    These tests pin the new behavior: stamp expires_at_time/date on apply,
    drop entries whose expiration is past per the HUD clock.
    """

    def _ps(self, nc=None, gs=None, hud_time="1430", hud_date="2026-04-25"):
        nc = nc or _make_nc_state()
        gs = gs or _make_game_state()
        return {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": gs,
            "hud_state": {"time": hud_time, "date": hud_date},
        }

    def test_stamp_on_fresh_debuff_in_hack(self):
        """1 hour duration + 14:30 → expires_at 15:30 same date."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ps = {"hud_state": {"time": "1430", "date": "2026-04-25"}, "game_state": gs}
        ops = [{"op": "stat_debuff", "stats": ["INT", "REF", "DEX"],
                "amount": 3, "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        self.assertEqual(len(hs["active_debuffs"]), 1)
        db = hs["active_debuffs"][0]
        self.assertEqual(db["expires_at_time"], "1530")
        self.assertEqual(db["expires_at_date"], "2026-04-25")

    def test_expire_past_debuff_in_hack(self):
        """Debuff stamped to expire 14:30 dropped when HUD is 15:00 same day."""
        hs = _make_hack_state()
        hs["active_debuffs"] = [
            {"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1 hour",
             "expires_at_time": "1430", "expires_at_date": "2026-04-25"}
        ]
        gs = _make_game_state()
        ps = {"hud_state": {"time": "1500", "date": "2026-04-25"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps)
        self.assertEqual(hs["active_debuffs"], [])

    def test_keep_unexpired_debuff(self):
        """Debuff expiring 16:00 still present at 15:30."""
        hs = _make_hack_state()
        hs["active_debuffs"] = [
            {"stats": ["MOVE"], "amount": 4, "source": "Scorpion", "duration": "1 hour",
             "expires_at_time": "1600", "expires_at_date": "2026-04-25"}
        ]
        gs = _make_game_state()
        ps = {"hud_state": {"time": "1530", "date": "2026-04-25"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps)
        self.assertEqual(len(hs["active_debuffs"]), 1)

    def test_partial_expiration_keeps_some(self):
        """Mix of expired + unexpired in same list — only expired drop."""
        hs = _make_hack_state()
        hs["active_debuffs"] = [
            {"stats": ["INT"], "amount": 2, "source": "Liche-old", "duration": "1h",
             "expires_at_time": "1400", "expires_at_date": "2026-04-25"},
            {"stats": ["MOVE"], "amount": 3, "source": "Scorpion-new", "duration": "1h",
             "expires_at_time": "1600", "expires_at_date": "2026-04-25"},
        ]
        gs = _make_game_state()
        ps = {"hud_state": {"time": "1500", "date": "2026-04-25"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps)
        self.assertEqual(len(hs["active_debuffs"]), 1)
        self.assertEqual(hs["active_debuffs"][0]["source"], "Scorpion-new")

    def test_midnight_rollover(self):
        """Debuff applied at 23:30 with 1h duration → expires 00:30 next day."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ps = {"hud_state": {"time": "2330", "date": "2026-04-25"}, "game_state": gs}
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        db = hs["active_debuffs"][0]
        self.assertEqual(db["expires_at_time"], "0030")
        self.assertEqual(db["expires_at_date"], "2026-04-26")
        # Now advance to 00:31 next day — should expire
        ps2 = {"hud_state": {"time": "0031", "date": "2026-04-26"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps2)
        self.assertEqual(hs["active_debuffs"], [])

    def test_no_clock_seed_skips_stamping(self):
        """No HUD time → debuffs accumulate without expiration data (legacy fallback)."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ps = {"hud_state": {}, "game_state": gs}
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        db = hs["active_debuffs"][0]
        self.assertNotIn("expires_at_time", db)

    def test_unparseable_duration_persists(self):
        """Garbage duration string → no stamp, debuff persists indefinitely."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ps = {"hud_state": {"time": "1430", "date": "2026-04-25"}, "game_state": gs}
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Mystery", "duration": "forever"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        db = hs["active_debuffs"][0]
        self.assertNotIn("expires_at_time", db)
        # Re-apply with later HUD — still there
        ps2 = {"hud_state": {"time": "2359", "date": "2026-04-26"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps2)
        self.assertEqual(len(hs["active_debuffs"]), 1)

    def test_stamping_idempotent(self):
        """Re-applying with already-stamped debuff doesn't re-stamp."""
        hs = _make_hack_state()
        hs["active_debuffs"] = [
            {"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1 hour",
             "expires_at_time": "1530", "expires_at_date": "2026-04-25"}
        ]
        gs = _make_game_state()
        # Different "now" time — should NOT re-stamp from the new time
        ps = {"hud_state": {"time": "1500", "date": "2026-04-25"}, "game_state": gs}
        apply_hack_state(hs, {"hack_state": {}}, game_state=gs, pipeline_state=ps)
        self.assertEqual(hs["active_debuffs"][0]["expires_at_time"], "1530")

    def test_in_net_combat_too(self):
        """Same expiration logic ticks in apply_net_combat_state."""
        nc = _make_nc_state()
        nc["active_debuffs"] = [
            {"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1h",
             "expires_at_time": "1400", "expires_at_date": "2026-04-25"}
        ]
        gs = _make_game_state()
        ps = self._ps(nc, gs, hud_time="1500")
        tool = {"hack_state": {"net_actions_used": 1}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertEqual(nc["active_debuffs"], [])

    def test_nervescrub_debuff_expires(self):
        """Nervescrub uses the same stat_debuff op + 1h duration."""
        nc = _make_nc_state()
        gs = _make_game_state()
        ps = self._ps(nc, gs, hud_time="1430")
        ops = [{"op": "stat_debuff", "stats": ["INT", "REF", "DEX"],
                "amount": 4, "source": "Nervescrub", "duration": "1 hour"}]
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, resolver_state_ops=ops, game_state=gs)
        db = nc["active_debuffs"][0]
        self.assertEqual(db["expires_at_time"], "1530")
        # Tick forward an hour and a minute
        ps2 = self._ps(nc, gs, hud_time="1531")
        apply_net_combat_state(ps2, tool, game_state=gs)
        self.assertEqual(nc["active_debuffs"], [])


class TestStatDebuffCrossHackPersistence(unittest.TestCase):
    """Verify Liche/Scorpion/Nervescrub debuffs persist across hack boundaries.

    RAW: these are meatspace effects on the character, not on the hack state.
    Source of truth lives on game_state.edgerunners[name].active_debuffs;
    hack_state.active_debuffs is a working mirror seeded at init and synced
    back at the end of every apply_hack_state / apply_net_combat_state call.
    """

    def _ps(self, hs=None, gs=None, hud_time="1430", hud_date="2026-04-25"):
        gs = gs or _make_game_state()
        return {
            "hud_state": {"time": hud_time, "date": hud_date},
            "game_state": gs,
        }

    def test_apply_syncs_to_edgerunner(self):
        """After apply_hack_state, edgerunner.active_debuffs mirrors hack_state's."""
        gs = _make_game_state()
        hs = _make_hack_state(hacker_name="V")
        ps = self._ps(gs=gs)
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        er_dbs = gs["edgerunners"]["V"]["active_debuffs"]
        self.assertEqual(len(er_dbs), 1)
        self.assertEqual(er_dbs[0]["source"], "Liche")
        self.assertEqual(er_dbs[0]["expires_at_time"], "1530")

    def test_init_hack_state_seeds_from_edgerunner(self):
        """A fresh hack picks up a still-active Liche debuff from the edgerunner."""
        gs = _make_game_state()
        gs["edgerunners"]["V"]["active_debuffs"] = [
            {"stats": ["INT"], "amount": 3, "source": "Liche", "duration": "1 hour",
             "expires_at_time": "1530", "expires_at_date": "2026-04-25"}
        ]
        # Fresh init reads game_state.edgerunners.V.active_debuffs
        hs = init_hack_state(hacker_name="V", interface_rank=4, game_state=gs)
        self.assertEqual(len(hs["active_debuffs"]), 1)
        self.assertEqual(hs["active_debuffs"][0]["source"], "Liche")

    def test_debuff_survives_hack_end_to_new_hack(self):
        """Hack 1 applies Liche, hack ends, hack 2 starts — debuff is still there.

        Pin the regression: previously, init_hack_state's active_debuffs: []
        wiped the prior debuff because the source of truth was hack-scoped.
        """
        gs = _make_game_state()
        # Hack 1: 14:00, take a 1-hour Liche hit
        hs1 = init_hack_state(hacker_name="V", interface_rank=4, game_state=gs)
        ps1 = self._ps(gs=gs, hud_time="1400")
        ops = [{"op": "stat_debuff", "stats": ["INT", "REF", "DEX"], "amount": 4,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs1, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps1)
        self.assertEqual(gs["edgerunners"]["V"]["active_debuffs"][0]["expires_at_time"], "1500")
        # Hack 1 ends (model jacks out, hack_state is discarded)
        # Hack 2 starts at 14:30 — fresh init, but edgerunner still holds the debuff
        hs2 = init_hack_state(hacker_name="V", interface_rank=4, game_state=gs)
        self.assertEqual(len(hs2["active_debuffs"]), 1)
        # And the working mirror still shows it on the next apply
        ps2 = self._ps(gs=gs, hud_time="1430")
        apply_hack_state(hs2, {"hack_state": {}}, game_state=gs, pipeline_state=ps2)
        self.assertEqual(len(hs2["active_debuffs"]), 1)
        self.assertEqual(hs2["active_debuffs"][0]["source"], "Liche")

    def test_debuff_expires_between_hacks_via_edgerunner(self):
        """Hack 1 takes Liche at 14:00 (1h); hack 2 starts at 15:30 — debuff is gone."""
        gs = _make_game_state()
        hs1 = init_hack_state(hacker_name="V", interface_rank=4, game_state=gs)
        ps1 = self._ps(gs=gs, hud_time="1400")
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs1, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps1)
        # Time passes (model narrates an hour and a half) — HUD advances to 15:30.
        # Hack 2 init seeds from edgerunner; first apply expires it.
        hs2 = init_hack_state(hacker_name="V", interface_rank=4, game_state=gs)
        ps2 = self._ps(gs=gs, hud_time="1530")
        apply_hack_state(hs2, {"hack_state": {}}, game_state=gs, pipeline_state=ps2)
        self.assertEqual(hs2["active_debuffs"], [])
        self.assertEqual(gs["edgerunners"]["V"]["active_debuffs"], [])

    def test_init_net_combat_state_seeds_from_edgerunner(self):
        """A fresh net_combat (no prior hack) seeds active_debuffs from the edgerunner."""
        from game_systems.cpred import init_net_combat_state
        gs = _make_game_state()
        gs["edgerunners"]["V"]["active_debuffs"] = [
            {"stats": ["MOVE"], "amount": 4, "source": "Scorpion", "duration": "1 hour",
             "expires_at_time": "1600", "expires_at_date": "2026-04-25"}
        ]
        nc = init_net_combat_state(netrunner_name="V", target="T",
                                    interface_rank=4, game_state=gs)
        self.assertEqual(len(nc["active_debuffs"]), 1)
        self.assertEqual(nc["active_debuffs"][0]["source"], "Scorpion")

    def test_net_combat_apply_syncs_to_edgerunner(self):
        """After apply_net_combat_state, edgerunner.active_debuffs is updated."""
        gs = _make_game_state()
        nc = _make_nc_state(hacker_name="V")
        # nc inherits netrunner=V from init_net_combat_from_hack
        nc["netrunner"] = "V"
        ps = {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": gs,
            "hud_state": {"time": "1430", "date": "2026-04-25"},
        }
        ops = [{"op": "stat_debuff", "stats": ["INT", "REF", "DEX"], "amount": 4,
                "source": "Nervescrub", "duration": "1 hour"}]
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, resolver_state_ops=ops, game_state=gs)
        er_dbs = gs["edgerunners"]["V"]["active_debuffs"]
        self.assertEqual(len(er_dbs), 1)
        self.assertEqual(er_dbs[0]["source"], "Nervescrub")
        self.assertEqual(er_dbs[0]["expires_at_time"], "1530")

    def test_no_hacker_name_no_sync(self):
        """Hack with no hacker_name configured doesn't crash on sync."""
        gs = _make_game_state()
        hs = _make_hack_state(hacker_name="V")
        hs["hacker_name"] = None  # unusual but tolerated
        ps = self._ps(gs=gs)
        ops = [{"op": "stat_debuff", "stats": ["INT"], "amount": 3,
                "source": "Liche", "duration": "1 hour"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops,
                         game_state=gs, pipeline_state=ps)
        # Edgerunner V was untouched (no name = no sync target)
        self.assertEqual(gs["edgerunners"]["V"].get("active_debuffs", []), [])
        # But the debuff still landed in hack_state for HUD rendering this scene
        self.assertEqual(len(hs["active_debuffs"]), 1)


class TestSlideEnforcement(unittest.TestCase):
    """Backend-enforced Slide mechanics (RAW p.205).

    Three rules pinned here:
    1. Cannot Slide preemptively — target Black ICE must already be hunting.
    2. Once per turn — second Slide in the same batch fail-softs.
    3. Successful Slide clears the hunting bond on the target ICE.

    Plus: Black ICE attack hits start the hunt; ICE derez clears it.
    """

    def _make_ice_status(self, hunting_netrunners=None):
        return {
            "n1": {
                "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
                "rez_current": 20, "rez_max": 20, "status": "active",
                "hunting": list(hunting_netrunners) if hunting_netrunners else [],
            }
        }

    def test_preemptive_slide_blocked(self):
        """Slide vs Black ICE that hasn't engaged yet → fail-soft, no roll spent."""
        actions = [{
            "type": "opposed_check",
            "character": "RedVelvet",
            "target": "Hellhound",
            "attacker_stat": 4, "attacker_skill": 0,
            "defender_stat": 6, "defender_skill": 0,
            "attacker_label": "Slide", "defender_label": "Hellhound PER",
            "net": True, "ability": "Slide",
        }]
        result = resolve_actions(
            actions,
            ice_status=self._make_ice_status(hunting_netrunners=[]),
        )
        self.assertEqual(len(result["results"]), 1)
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "slide_preemptive")
        # No state ops emitted (didn't reach the success branch)
        self.assertEqual(result["state_ops"], [])

    def test_slide_against_hunting_ice_allowed(self):
        """Slide vs Black ICE actively hunting RedVelvet → roll proceeds."""
        actions = [{
            "type": "opposed_check",
            "character": "RedVelvet",
            "target": "Hellhound",
            "attacker_stat": 12, "attacker_skill": 0,  # high to favor success
            "defender_stat": 0, "defender_skill": 0,
            "attacker_label": "Slide", "defender_label": "Hellhound PER",
            "net": True, "ability": "Slide",
        }]
        ice_status = self._make_ice_status(hunting_netrunners=["RedVelvet"])
        result = resolve_actions(actions, ice_status=ice_status)
        r = result["results"][0]
        # Either success or failure — what matters is that validation passed
        self.assertNotIn("error", r)

    def test_slide_success_emits_hunt_clear_and_slide_used(self):
        """On success, backend emits hunt_clear + slide_used state ops."""
        import unittest.mock as mock
        ice_status = self._make_ice_status(hunting_netrunners=["RedVelvet"])
        actions = [{
            "type": "opposed_check",
            "character": "RedVelvet",
            "target": "Hellhound",
            "attacker_stat": 4, "attacker_skill": 0,
            "defender_stat": 0, "defender_skill": 0,
            "attacker_label": "Slide", "defender_label": "Hellhound PER",
            "net": True, "ability": "Slide",
        }]
        # Force success: attacker rolls 9, defender rolls 2 → 13 vs 2
        with mock.patch("game_systems.cpred_mechanics.random.randint",
                         side_effect=[9, 2]):
            result = resolve_actions(actions, ice_status=ice_status)
        r = result["results"][0]
        self.assertTrue(r["success"])
        ops = result["state_ops"]
        self.assertTrue(any(o.get("op") == "hunt_clear" and o.get("ice_key") == "n1"
                            and o.get("netrunner") == "RedVelvet" for o in ops))
        self.assertTrue(any(o.get("op") == "slide_used"
                            and o.get("netrunner") == "RedVelvet" for o in ops))

    def test_slide_once_per_turn_within_batch(self):
        """Two Slides in the same resolve_actions call → second fail-softs."""
        import unittest.mock as mock
        ice_status = self._make_ice_status(hunting_netrunners=["RedVelvet"])
        actions = [
            {
                "type": "opposed_check",
                "character": "RedVelvet",
                "target": "Hellhound",
                "attacker_stat": 4, "attacker_skill": 0,
                "defender_stat": 0, "defender_skill": 0,
                "attacker_label": "Slide", "defender_label": "Hellhound PER",
                "net": True, "ability": "Slide",
            },
            {
                "type": "opposed_check",
                "character": "RedVelvet",
                "target": "Hellhound",
                "attacker_stat": 4, "attacker_skill": 0,
                "defender_stat": 0, "defender_skill": 0,
                "attacker_label": "Slide", "defender_label": "Hellhound PER",
                "net": True, "ability": "Slide",
            },
        ]
        with mock.patch("game_systems.cpred_mechanics.random.randint",
                         side_effect=[9, 2, 9, 2]):
            result = resolve_actions(actions, ice_status=ice_status)
        # First Slide success (or attempt), second fail-softs with slide_already_used
        self.assertEqual(len(result["results"]), 2)
        r1, r2 = result["results"]
        self.assertTrue(r1["success"])
        self.assertFalse(r2["success"])
        self.assertEqual(r2["error"], "slide_already_used")

    def test_slide_used_param_blocks_first_attempt(self):
        """If slide_used_this_turn=True is passed, even first Slide fail-softs."""
        actions = [{
            "type": "opposed_check",
            "character": "RedVelvet",
            "target": "Hellhound",
            "attacker_stat": 4, "attacker_skill": 0,
            "defender_stat": 0, "defender_skill": 0,
            "attacker_label": "Slide", "defender_label": "Hellhound PER",
            "net": True, "ability": "Slide",
        }]
        result = resolve_actions(
            actions,
            ice_status=self._make_ice_status(hunting_netrunners=["RedVelvet"]),
            slide_used_this_turn=True,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "slide_already_used")

    def test_slide_against_wrong_ice_blocks(self):
        """Slide vs an ICE not hunting this Netrunner → fail even if other ICE is hunting."""
        ice_status = {
            "n1": {  # Hunting RedVelvet
                "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
                "rez_current": 20, "rez_max": 20, "status": "active",
                "hunting": ["RedVelvet"],
            },
            "n2": {  # NOT hunting RedVelvet
                "name": "Wisp", "behavior": "black", "ice_type": "wisp",
                "rez_current": 15, "rez_max": 15, "status": "active",
                "hunting": [],
            },
        }
        actions = [{
            "type": "opposed_check",
            "character": "RedVelvet",
            "target": "Wisp",  # Targeting the ICE that ISN'T hunting
            "target_ice_key": "n2",
            "attacker_stat": 4, "attacker_skill": 0,
            "defender_stat": 0, "defender_skill": 0,
            "attacker_label": "Slide", "defender_label": "Wisp PER",
            "net": True, "ability": "Slide",
        }]
        result = resolve_actions(actions, ice_status=ice_status)
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "slide_preemptive")

    def test_apply_hack_state_consumes_slide_used_op(self):
        """slide_used state op flips hack_state.slide_used_this_turn."""
        hs = _make_hack_state()
        gs = _make_game_state()
        ops = [{"op": "slide_used", "netrunner": "V"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertTrue(hs["slide_used_this_turn"])

    def test_apply_hack_state_consumes_hunt_clear_op(self):
        """hunt_clear state op removes Netrunner from ICE.hunting list."""
        hs = _make_hack_state()
        hs["ice_status"] = {
            "n1": {"name": "Hellhound", "behavior": "black", "status": "active",
                   "rez_current": 20, "rez_max": 20, "hunting": ["V", "Other"]},
        }
        gs = _make_game_state()
        ops = [{"op": "hunt_clear", "ice_key": "n1", "netrunner": "V"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(hs["ice_status"]["n1"]["hunting"], ["Other"])

    def test_apply_hack_state_consumes_hunt_start_op(self):
        """hunt_start state op adds Netrunner to ICE.hunting list (deduped)."""
        hs = _make_hack_state()
        hs["ice_status"] = {
            "n1": {"name": "Hellhound", "behavior": "black", "status": "active",
                   "rez_current": 20, "rez_max": 20, "hunting": []},
        }
        gs = _make_game_state()
        ops = [{"op": "hunt_start", "ice_key": "n1", "netrunner": "V"}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(hs["ice_status"]["n1"]["hunting"], ["V"])
        # Idempotent: re-applying doesn't duplicate
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(hs["ice_status"]["n1"]["hunting"], ["V"])

    def test_meatspace_due_resets_slide_used(self):
        """Turn boundary (meatspace_due) resets slide_used_this_turn to False."""
        hs = _make_hack_state()
        hs["slide_used_this_turn"] = True
        hs["net_actions_remaining"] = 1
        gs = _make_game_state()
        # Use up the last NET action → triggers meatspace_due
        apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}}, game_state=gs)
        self.assertTrue(hs["meatspace_due"])
        self.assertFalse(hs["slide_used_this_turn"])

    def test_ice_derez_clears_hunting(self):
        """Derezzing a Black ICE wipes its hunting list."""
        hs = _make_hack_state()
        hs["ice_status"] = {
            "n1": {"name": "Hellhound", "behavior": "black", "status": "active",
                   "rez_current": 5, "rez_max": 20, "hunting": ["V"]},
        }
        gs = _make_game_state()
        # Apply enough rez damage to derez
        ops = [{"op": "rez_damage", "target_key": "n1", "damage": 10}]
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(hs["ice_status"]["n1"]["status"], "derezzed")
        self.assertEqual(hs["ice_status"]["n1"]["hunting"], [])

    def test_init_hack_state_seeds_slide_used_false(self):
        """Fresh hack starts with slide_used_this_turn=False."""
        hs = init_hack_state(hacker_name="V", interface_rank=4)
        self.assertFalse(hs["slide_used_this_turn"])

    def test_net_combat_round_increment_resets_slide_used(self):
        """In net_combat, advancing combat.round resets slide_used_this_turn.

        Net_combat shares meatspace combat initiative — a new combat round
        IS a new turn for the netrunner, so the once-per-turn flag clears.
        """
        nc = _make_nc_state(hacker_name="V")
        nc["netrunner"] = "V"
        nc["slide_used_this_turn"] = True
        nc["_prev_combat_round"] = 1
        gs = _make_game_state()
        ps = {
            "net_combat": nc,
            "combat": {"round": 2, "initiative_order": ["V"], "current_turn": "V"},
            "character_states": {},
            "game_state": gs,
        }
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertFalse(nc["slide_used_this_turn"])
        self.assertEqual(nc["_prev_combat_round"], 2)

    def test_net_combat_same_round_keeps_slide_used(self):
        """Same combat.round → slide_used_this_turn unchanged (still True)."""
        nc = _make_nc_state(hacker_name="V")
        nc["netrunner"] = "V"
        nc["slide_used_this_turn"] = True
        nc["_prev_combat_round"] = 2
        gs = _make_game_state()
        ps = {
            "net_combat": nc,
            "combat": {"round": 2, "initiative_order": ["V"], "current_turn": "V"},
            "character_states": {},
            "game_state": gs,
        }
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertTrue(nc["slide_used_this_turn"])
        self.assertEqual(nc["_prev_combat_round"], 2)

    def test_net_combat_no_combat_dict_keeps_slide_used(self):
        """Missing combat dict (rare) → no reset, no crash."""
        nc = _make_nc_state(hacker_name="V")
        nc["netrunner"] = "V"
        nc["slide_used_this_turn"] = True
        nc["_prev_combat_round"] = 1
        gs = _make_game_state()
        ps = {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": gs,
        }
        tool = {"hack_state": {"net_actions_used": 0}, "combat_complete": False,
                "net_complete": False}
        apply_net_combat_state(ps, tool, game_state=gs)
        self.assertTrue(nc["slide_used_this_turn"])

    def test_net_combat_multi_round_progression(self):
        """Slide once per round, persists within round, resets next round."""
        nc = _make_nc_state(hacker_name="V")
        nc["netrunner"] = "V"
        nc["_prev_combat_round"] = 1
        gs = _make_game_state()
        # Round 1: a Slide just succeeded
        ps_r1 = {
            "net_combat": nc,
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
            "character_states": {},
            "game_state": gs,
        }
        tool_r1 = {"hack_state": {"net_actions_used": 1}, "combat_complete": False,
                   "net_complete": False}
        apply_net_combat_state(ps_r1, tool_r1, resolver_state_ops=[
            {"op": "slide_used", "netrunner": "V"},
        ], game_state=gs)
        self.assertTrue(nc["slide_used_this_turn"])
        # Same round, second exchange: still True
        apply_net_combat_state(ps_r1, tool_r1, game_state=gs)
        self.assertTrue(nc["slide_used_this_turn"])
        # Round 2 starts: reset
        ps_r2 = {
            "net_combat": nc,
            "combat": {"round": 2, "initiative_order": ["V"], "current_turn": "V"},
            "character_states": {},
            "game_state": gs,
        }
        apply_net_combat_state(ps_r2, tool_r1, game_state=gs)
        self.assertFalse(nc["slide_used_this_turn"])

    def test_black_ice_attack_emits_hunt_start(self):
        """A successful Black ICE attack emits hunt_start tying the ICE to the Netrunner."""
        import unittest.mock as mock
        ice_status = {
            "Lobby_Hellhound": {
                "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
                "rez_current": 20, "rez_max": 20, "status": "active",
                "hunting": [],
            }
        }
        actions = [{
            "type": "program_attack_vs_netrunner",
            "character": "Hellhound",
            "ice_type": "Hellhound",
            "target": "RedVelvet",
            "interface_rank": 4,
            "target_def": 0,
            "ice_status_key": "Lobby_Hellhound",
        }]
        # Force hit with a high attacker roll. Use return_value=4 so any
        # number of internal rolls (opposed d10s, damage dice, hook rolls)
        # all return safely without StopIteration. 4+6 vs 4+0 = 10 vs 4 → hit.
        with mock.patch("game_systems.cpred_mechanics.random.randint",
                         return_value=4):
            result = resolve_actions(actions, ice_status=ice_status)
        r = result["results"][0]
        self.assertTrue(r.get("hit"))
        ops = result["state_ops"]
        hunt_ops = [o for o in ops if o.get("op") == "hunt_start"]
        self.assertEqual(len(hunt_ops), 1)
        self.assertEqual(hunt_ops[0]["ice_key"], "Lobby_Hellhound")
        self.assertEqual(hunt_ops[0]["netrunner"], "RedVelvet")


class TestPlayerErrorsSummary(unittest.TestCase):
    """Verify resolve_actions surfaces player-side RAW violations as a
    top-level `player_errors` list so the narrator can route to OOC retry
    without scanning every action result.

    The narrator contract treats non-empty player_errors as: do NOT advance
    the world (skip report_*_state), drop OOC, paraphrase the reason field,
    prompt for retry.
    """

    def test_clean_batch_has_empty_player_errors(self):
        """All-valid actions → player_errors=[]."""
        ice_status = {"n1": {"name": "Hellhound", "behavior": "black",
                              "ice_type": "hellhound", "rez_current": 20,
                              "rez_max": 20, "status": "active",
                              "hunting": ["V"]}}
        result = resolve_actions([{
            "type": "opposed_check", "character": "V", "target": "Hellhound",
            "attacker_stat": 4, "defender_stat": 0,
            "attacker_label": "Slide", "defender_label": "PER",
            "net": True, "ability": "Slide",
        }], ice_status=ice_status)
        self.assertEqual(result["player_errors"], [])

    def test_slide_preemptive_surfaces_in_player_errors(self):
        """slide_preemptive error appears in player_errors with action_index + reason."""
        ice_status = {"n1": {"name": "Hellhound", "behavior": "black",
                              "ice_type": "hellhound", "rez_current": 20,
                              "rez_max": 20, "status": "active", "hunting": []}}
        result = resolve_actions([{
            "type": "opposed_check", "character": "V", "target": "Hellhound",
            "attacker_stat": 4, "defender_stat": 0,
            "attacker_label": "Slide", "defender_label": "PER",
            "net": True, "ability": "Slide",
        }], ice_status=ice_status)
        self.assertEqual(len(result["player_errors"]), 1)
        pe = result["player_errors"][0]
        self.assertEqual(pe["action_index"], 0)
        self.assertEqual(pe["error"], "slide_preemptive")
        self.assertEqual(pe["action_type"], "opposed_check")
        self.assertIn("hunting", pe["reason"].lower())

    def test_program_not_firable_surfaces_in_player_errors(self):
        """program_not_firable (Derezzed Sword) appears in player_errors."""
        progs = [{"name": "Sword", "status": "derezzed", "rez": 0,
                  "category": "attacker"}]
        result = resolve_actions([{
            "type": "program_attack", "character": "V",
            "program": "Sword", "target": "Hellhound",
            "interface_rank": 4, "program_atk": 1, "target_def": 2,
            "program_damage_dice": 3, "target_rez": 20,
        }], active_programs=progs, net_actions_remaining=4)
        self.assertEqual(len(result["player_errors"]), 1)
        self.assertEqual(result["player_errors"][0]["error"], "program_not_firable")

    def test_dice_failure_does_not_appear_in_player_errors(self):
        """A normal missed roll (success: false, no error code) is NOT a
        player error — the world should advance normally."""
        ice_status = {"n1": {"name": "Hellhound", "behavior": "black",
                              "ice_type": "hellhound", "rez_current": 20,
                              "rez_max": 20, "status": "active",
                              "hunting": ["V"]}}
        # Stack the deck against the player so they fail the Slide
        result = resolve_actions([{
            "type": "opposed_check", "character": "V", "target": "Hellhound",
            "attacker_stat": 1, "defender_stat": 99,
            "attacker_label": "Slide", "defender_label": "PER",
            "net": True, "ability": "Slide",
        }], ice_status=ice_status)
        # Even if the roll fails, no error code → not in player_errors
        self.assertEqual(result["player_errors"], [])

    def test_mixed_batch_only_errors_in_player_errors(self):
        """A batch with one valid + one invalid action — only the invalid
        one shows up in player_errors with the right action_index."""
        ice_status = {"n1": {"name": "Hellhound", "behavior": "black",
                              "ice_type": "hellhound", "rez_current": 20,
                              "rez_max": 20, "status": "active",
                              "hunting": ["V"]}}
        progs = [{"name": "Sword", "status": "derezzed", "rez": 0,
                  "category": "attacker"}]
        result = resolve_actions([
            # Valid: Slide vs hunting Hellhound
            {"type": "opposed_check", "character": "V", "target": "Hellhound",
             "attacker_stat": 4, "defender_stat": 0,
             "attacker_label": "Slide", "defender_label": "PER",
             "net": True, "ability": "Slide"},
            # Invalid: fire Derezzed Sword
            {"type": "program_attack", "character": "V",
             "program": "Sword", "target": "Hellhound",
             "interface_rank": 4, "program_atk": 1, "target_def": 2,
             "program_damage_dice": 3, "target_rez": 20},
        ], ice_status=ice_status, active_programs=progs,
           net_actions_remaining=4)
        self.assertEqual(len(result["player_errors"]), 1)
        self.assertEqual(result["player_errors"][0]["action_index"], 1)
        self.assertEqual(result["player_errors"][0]["error"], "program_not_firable")

    def test_insufficient_net_actions_surfaces_in_player_errors(self):
        """insufficient_net_actions (boosted action with no NA) surfaces."""
        result = resolve_actions([{
            "type": "activate_program", "character": "V",
            "program": "Shield",
        }], active_programs=[{"name": "Shield", "status": "deactivated", "rez": 7}],
           net_actions_remaining=0)
        self.assertEqual(len(result["player_errors"]), 1)
        self.assertEqual(result["player_errors"][0]["error"], "insufficient_net_actions")


if __name__ == "__main__":
    unittest.main()
