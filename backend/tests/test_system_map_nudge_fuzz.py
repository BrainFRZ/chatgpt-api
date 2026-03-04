"""
Fuzz tests for system_map missing-nudge injection and init_net_combat_state sr param.

Covers:
- Deterministic: nudge appears when system_map is None + correct tier
- Deterministic: nudge absent when system_map is present
- Deterministic: nudge absent for wrong tier (quick_hack / quick_hack)
- Deterministic: init_net_combat_state stores sr correctly
- Random fuzzing: malformed hack_state dicts never crash build_hack_injection
- Random fuzzing: malformed net_combat dicts never crash build_net_combat_injection
- Random fuzzing: malformed args to init_net_combat_state never crash
"""

import logging
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred import (
    apply_hack_state as cpred_apply_hack_state,
    apply_net_combat_state as cpred_apply_net_combat_state,
    build_hack_injection as cpred_build_hack_injection,
    build_net_combat_injection as cpred_build_net_combat_injection,
    init_hack_state as cpred_init_hack_state,
    init_net_combat_state as cpred_init_net_combat_state,
    init_net_combat_from_hack as cpred_init_net_combat_from_hack,
)
from game_systems.dnd5e_cyber import (
    build_hack_injection as cyber_build_hack_injection,
    init_hack_state as cyber_init_hack_state,
)


# ============================================================
# Helpers
# ============================================================

NUDGE_MARKER = "SYSTEM MAP MISSING"

ATOMS = [None, 0, 1, -1, 3.14, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

HACK_STATE_KEYS = [
    "active", "tier", "target_system", "hacker_name", "sr",
    "interface_rank", "net_actions_per_turn", "start_message_id",
    "system_map", "revealed_nodes", "alert_level", "cycles_remaining",
    "cycles_max", "active_programs", "current_node", "nodes_visited",
    "ice_status", "trace_progress", "tar_stacks", "brain_damage",
    "narrative_summary", "available_actions", "net_actions_remaining",
    "meatspace_due", "context",
]

NET_COMBAT_KEYS = [
    "active", "netrunner", "target", "initiated_from", "interface_rank",
    "net_actions_per_turn", "start_message_id", "sr", "tier",
    "alert_level", "cycles_remaining", "cycles_max", "active_programs",
    "current_node", "nodes_visited", "revealed_nodes", "ice_status",
    "trace_progress", "tar_stacks", "brain_damage", "system_map",
    "available_actions", "combat_complete", "net_complete",
    "narrative_summary", "_prev_brain_damage", "_combat_breakout", "context",
]


TIER_VALUES = ["full_run", "quick_hack", "full_sequence", "", "unknown", None]


def _rand_hack_state(depth=0):
    if depth > 2 or random.random() < 0.3:
        return random.choice(ATOMS)
    d = {}
    for _ in range(random.randint(0, 10)):
        d[random.choice(HACK_STATE_KEYS)] = _rand_hack_state(depth + 1)
    return d


def _rand_net_combat(depth=0):
    if depth > 2 or random.random() < 0.3:
        return random.choice(ATOMS)
    d = {}
    for _ in range(random.randint(0, 12)):
        d[random.choice(NET_COMBAT_KEYS)] = _rand_net_combat(depth + 1)
    return d


def _sample_system_map():
    return {
        "sr": 3,
        "nodes": {
            "Gateway": {"type": "gateway", "ice": "patrol", "dv": 9,
                        "connections": ["DataNode1"], "contents": "Entry"},
            "DataNode1": {"type": "data_node", "ice": "tar", "dv": 11,
                          "connections": ["Gateway", "Target"], "contents": "Data"},
            "Target": {"type": "target", "ice": None, "dv": 15,
                       "connections": ["DataNode1"], "contents": "Objective"},
        },
    }


def _minimal_combat():
    return {"round": 1, "initiative_order": [], "current_turn": ""}


def _minimal_pipeline_state():
    return {
        "character_states": {},
        "game_state": {"edgerunners": {}},
        "combat": _minimal_combat(),
    }


# ============================================================
# CPRED hack injection nudge tests
# ============================================================

class TestCpredHackInjectionNudge(unittest.TestCase):

    def test_nudge_present_full_run_no_map(self):
        hs = cpred_init_hack_state(tier="full_run")
        result = cpred_build_hack_injection(hs)
        self.assertIn(NUDGE_MARKER, result)

    def test_nudge_absent_full_run_with_map(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["system_map"] = _sample_system_map()
        result = cpred_build_hack_injection(hs)
        self.assertNotIn(NUDGE_MARKER, result)
        self.assertIn("SYSTEM MAP", result)

    def test_nudge_present_quick_hack_no_map(self):
        """CPRED quick hacks have 3-node linear architecture — nudge fires when map missing."""
        hs = cpred_init_hack_state(tier="quick_hack")
        result = cpred_build_hack_injection(hs)
        self.assertIn(NUDGE_MARKER, result)

    def test_nudge_absent_quick_hack_with_map(self):
        hs = cpred_init_hack_state(tier="quick_hack")
        hs["system_map"] = _sample_system_map()
        result = cpred_build_hack_injection(hs)
        self.assertNotIn(NUDGE_MARKER, result)

    def test_nudge_present_when_tier_missing(self):
        """If tier is absent, still nudge — all CPRED hacks need system_map."""
        hs = cpred_init_hack_state(tier="full_run")
        del hs["tier"]
        result = cpred_build_hack_injection(hs)
        self.assertIn(NUDGE_MARKER, result)


class TestRevealedNodes(unittest.TestCase):
    """Tests for revealed_nodes fog-of-war tracking."""

    def test_init_hack_state_has_revealed_nodes(self):
        hs = cpred_init_hack_state(tier="full_run")
        self.assertIn("revealed_nodes", hs)
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])

    def test_init_net_combat_state_has_revealed_nodes(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        self.assertIn("revealed_nodes", nc)
        self.assertEqual(nc["revealed_nodes"], ["Gateway"])

    def test_init_net_combat_from_hack_carries_revealed_nodes(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["revealed_nodes"] = ["Gateway", "DataNode1", "Target"]
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["revealed_nodes"], ["Gateway", "DataNode1", "Target"])

    def test_init_net_combat_from_hack_fallback_on_bad_type(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["revealed_nodes"] = "not a list"
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertIsInstance(nc["revealed_nodes"], list)

    def test_auto_merge_nodes_visited_into_revealed(self):
        """Visiting a node should auto-add it to revealed_nodes."""
        from game_systems.cpred import apply_hack_state
        hs = cpred_init_hack_state(tier="full_run")
        # Simulate model reporting a new visited node but forgetting to update revealed_nodes
        tool_input = {
            "hack_state": {
                "current_node": "DataNode1",
                "nodes_visited": ["Gateway", "DataNode1"],
                "alert_level": 0,
            }
        }
        apply_hack_state(hs, tool_input)
        self.assertIn("DataNode1", hs["revealed_nodes"])
        self.assertIn("Gateway", hs["revealed_nodes"])

    def test_revealed_nodes_in_injection(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["system_map"] = _sample_system_map()
        hs["revealed_nodes"] = ["Gateway", "DataNode1"]
        result = cpred_build_hack_injection(hs)
        self.assertIn("Revealed nodes:", result)
        self.assertIn("Gateway", result)
        self.assertIn("DataNode1", result)

    def test_revealed_nodes_in_net_combat_injection(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["system_map"] = _sample_system_map()
        nc["revealed_nodes"] = ["Gateway", "Target"]
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("Revealed nodes:", result)
        self.assertIn("Gateway", result)
        self.assertIn("Target", result)


class TestCpredHackInjectionFuzz(unittest.TestCase):

    def test_random_system_map_and_tier_combos_never_crash(self):
        """Fuzz system_map and tier on a valid base state — tests nudge code path."""
        map_values = [
            None, {}, _sample_system_map(),
            0, 1, -1, "", "map", True, False, [], [1, 2], [{"a": 1}], 3.14,
            {"sr": "bad", "nodes": None}, {"sr": 3, "nodes": []},
        ]
        logging.disable(logging.CRITICAL)
        try:
            for seed in (10, 42, 77, 200):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cpred_init_hack_state(
                            tier=random.choice(TIER_VALUES + ["full_run", "full_run"]),
                        )
                        hs["system_map"] = random.choice(map_values)
                        result = cpred_build_hack_injection(hs)
                        self.assertIsInstance(result, str)
                        # Verify nudge logic: CPRED always nudges when map is missing
                        if hs["system_map"] and hs["system_map"] != 0:
                            self.assertNotIn(NUDGE_MARKER, result)
                        elif not hs.get("system_map"):
                            self.assertIn(NUDGE_MARKER, result)
        finally:
            logging.disable(logging.NOTSET)

    def test_system_map_wrong_types_never_crash(self):
        """system_map set to non-dict types should not crash."""
        wrong_maps = [0, 1, -1, "", "map", True, False, [], [1, 2], [{"a": 1}], 3.14]
        for wrong in wrong_maps:
            with self.subTest(system_map=wrong):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = wrong
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_fully_random_hack_state_never_crashes(self):
        """Fully random hack_state dicts should never crash build_hack_injection."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (10, 42, 77, 200):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = _rand_hack_state()
                        if not isinstance(hs, dict):
                            hs = {"tier": hs}
                        result = cpred_build_hack_injection(hs)
                        self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# CPRED init_net_combat_state sr param tests
# ============================================================

class TestCpredInitNetCombatStateSr(unittest.TestCase):

    def test_default_sr_is_3(self):
        nc = cpred_init_net_combat_state()
        self.assertEqual(nc["sr"], 3)

    def test_custom_sr_stored(self):
        for sr in [1, 2, 3, 4, 5]:
            with self.subTest(sr=sr):
                nc = cpred_init_net_combat_state(sr=sr)
                self.assertEqual(nc["sr"], sr)

    def test_sr_passed_through_kwargs(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Vault", sr=5)
        self.assertEqual(nc["sr"], 5)
        self.assertEqual(nc["netrunner"], "V")
        self.assertEqual(nc["target"], "Vault")

    def test_init_from_hack_preserves_sr(self):
        hack = cpred_init_hack_state(tier="full_run", sr=4)
        nc = cpred_init_net_combat_from_hack(hack)
        self.assertEqual(nc["sr"], 4)

    def test_fuzz_init_net_combat_state_random_args(self):
        logging.disable(logging.CRITICAL)
        try:
            for seed in (31, 59):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(1000):
                        kwargs = {}
                        if random.random() < 0.5:
                            kwargs["netrunner_name"] = random.choice(ATOMS)
                        if random.random() < 0.5:
                            kwargs["target"] = random.choice(ATOMS)
                        if random.random() < 0.5:
                            kwargs["interface_rank"] = random.choice(ATOMS)
                        if random.random() < 0.5:
                            kwargs["cycles_max"] = random.choice(ATOMS)
                        if random.random() < 0.5:
                            kwargs["sr"] = random.choice(ATOMS)
                        try:
                            nc = cpred_init_net_combat_state(**kwargs)
                            self.assertIsInstance(nc, dict)
                            self.assertIn("sr", nc)
                        except (TypeError, ValueError):
                            pass  # expected for non-numeric interface_rank
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# CPRED net_combat injection nudge tests
# ============================================================

class TestCpredNetCombatInjectionNudge(unittest.TestCase):

    def test_nudge_present_when_no_map(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Vault", sr=3)
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn(NUDGE_MARKER, result)

    def test_nudge_absent_when_map_present(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Vault", sr=3)
        nc["system_map"] = _sample_system_map()
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertNotIn(NUDGE_MARKER, result)
        self.assertIn("SYSTEM MAP", result)

    def test_nudge_absent_when_net_complete(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Vault")
        nc["net_complete"] = True
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertNotIn(NUDGE_MARKER, result)

    def test_nudge_present_after_hack_transition_without_map(self):
        hack = cpred_init_hack_state(tier="full_run", sr=4)
        # Simulate model failing to provide system_map
        nc = cpred_init_net_combat_from_hack(hack)
        self.assertIsNone(nc.get("system_map"))
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn(NUDGE_MARKER, result)

    def test_nudge_absent_after_hack_transition_with_map(self):
        hack = cpred_init_hack_state(tier="full_run", sr=4)
        hack["system_map"] = _sample_system_map()
        nc = cpred_init_net_combat_from_hack(hack)
        self.assertIsNotNone(nc.get("system_map"))
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertNotIn(NUDGE_MARKER, result)

    def test_nudge_present_for_quick_hack_transition(self):
        """CPRED quick hacks have 3-node architecture — nudge fires when map missing in net_combat."""
        hack = cpred_init_hack_state(tier="quick_hack", sr=2)
        nc = cpred_init_net_combat_from_hack(hack)
        self.assertIsNone(nc.get("system_map"))
        self.assertEqual(nc.get("tier"), "quick_hack")
        combat = _minimal_combat()
        ps = _minimal_pipeline_state()
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn(NUDGE_MARKER, result)


class TestCpredNetCombatInjectionFuzz(unittest.TestCase):

    def test_random_system_map_combos_never_crash(self):
        """Fuzz system_map on a valid base net_combat state — tests nudge code path."""
        map_values = [
            None, {}, _sample_system_map(),
            0, 1, -1, "", "map", True, False, [], [1, 2], [{"a": 1}], 3.14,
            {"sr": "bad", "nodes": None}, {"sr": 3, "nodes": []},
        ]
        logging.disable(logging.CRITICAL)
        try:
            for seed in (13, 67, 150):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        nc = cpred_init_net_combat_state(
                            netrunner_name="V", target="Vault",
                            sr=random.choice([1, 2, 3, 4, 5]),
                        )
                        nc["tier"] = random.choice(["full_run", "quick_hack", None, ""])
                        nc["system_map"] = random.choice(map_values)
                        nc["net_complete"] = random.choice([True, False])
                        combat = _minimal_combat() if random.random() < 0.7 else None
                        ps = _minimal_pipeline_state()
                        result = cpred_build_net_combat_injection(combat, nc, ps)
                        self.assertIsInstance(result, str)
                        # Verify nudge logic — all CPRED tiers nudge when map is missing
                        if nc.get("net_complete"):
                            self.assertNotIn(NUDGE_MARKER, result)
                        elif nc["system_map"] and nc["system_map"] != 0:
                            self.assertNotIn(NUDGE_MARKER, result)
                        else:
                            self.assertIn(NUDGE_MARKER, result)
        finally:
            logging.disable(logging.NOTSET)

    def test_fully_random_net_combat_never_crashes(self):
        """Fully random net_combat dicts should never crash build_net_combat_injection."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (13, 67, 150):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        nc = _rand_net_combat()
                        if not isinstance(nc, dict):
                            nc = {"active": True}
                        combat = _minimal_combat() if random.random() < 0.7 else None
                        ps = _minimal_pipeline_state()
                        result = cpred_build_net_combat_injection(combat, nc, ps)
                        self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# D&D 5E Cyber hack injection nudge tests
# ============================================================

class TestCyberHackInjectionNudge(unittest.TestCase):

    def test_nudge_present_full_sequence_no_map(self):
        hs = cyber_init_hack_state(tier="full_sequence")
        result = cyber_build_hack_injection(hs)
        self.assertIn(NUDGE_MARKER, result)

    def test_nudge_absent_full_sequence_with_map(self):
        hs = cyber_init_hack_state(tier="full_sequence")
        hs["system_map"] = _sample_system_map()
        result = cyber_build_hack_injection(hs)
        self.assertNotIn(NUDGE_MARKER, result)
        self.assertIn("SYSTEM MAP", result)

    def test_nudge_absent_quick_hack(self):
        hs = cyber_init_hack_state(tier="quick_hack")
        result = cyber_build_hack_injection(hs)
        self.assertNotIn(NUDGE_MARKER, result)

    def test_nudge_absent_when_tier_missing(self):
        hs = cyber_init_hack_state(tier="full_sequence")
        del hs["tier"]
        result = cyber_build_hack_injection(hs)
        self.assertNotIn(NUDGE_MARKER, result)


class TestCyberHackInjectionFuzz(unittest.TestCase):

    def test_random_system_map_and_tier_combos_never_crash(self):
        """Fuzz system_map and tier on a valid base state — tests nudge code path."""
        map_values = [
            None, {}, _sample_system_map(),
            0, 1, -1, "", "map", True, False, [], [1, 2], [{"a": 1}], 3.14,
            {"sr": "bad", "nodes": None}, {"sr": 3, "nodes": []},
        ]
        cyber_tiers = ["full_sequence", "quick_hack", "", "unknown", None, "full_sequence"]
        logging.disable(logging.CRITICAL)
        try:
            for seed in (21, 55, 88):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cyber_init_hack_state(
                            tier=random.choice(cyber_tiers) or "full_sequence",
                        )
                        hs["system_map"] = random.choice(map_values)
                        result = cyber_build_hack_injection(hs)
                        self.assertIsInstance(result, str)
                        if hs["system_map"] and hs["system_map"] != 0:
                            self.assertNotIn(NUDGE_MARKER, result)
                        elif hs.get("tier") == "full_sequence" and not hs.get("system_map"):
                            self.assertIn(NUDGE_MARKER, result)
        finally:
            logging.disable(logging.NOTSET)

    def test_system_map_wrong_types_never_crash(self):
        wrong_maps = [0, 1, -1, "", "map", True, False, [], [1, 2], [{"a": 1}], 3.14]
        for wrong in wrong_maps:
            with self.subTest(system_map=wrong):
                hs = cyber_init_hack_state(tier="full_sequence")
                hs["system_map"] = wrong
                result = cyber_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_fully_random_hack_state_never_crashes(self):
        """Fully random hack_state dicts should never crash build_hack_injection."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (21, 55, 88):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = _rand_hack_state()
                        if not isinstance(hs, dict):
                            hs = {"tier": hs}
                        result = cyber_build_hack_injection(hs)
                        self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# Fuzz: apply_hack_state auto-merge revealed_nodes
# ============================================================

class TestApplyHackStateRevealedNodesFuzz(unittest.TestCase):
    """Fuzz the auto-merge logic in apply_hack_state that merges nodes_visited into revealed_nodes."""

    REVEALED_VALUES = [
        None, [], ["Gateway"], ["Gateway", "DataNode1"], "",
        "not_a_list", 42, True, False, {}, {"Gateway": True},
        [1, 2, 3], [None, None], [[], {}], [True, False],
        ["Gateway", None, 42, [], "DataNode1"],
    ]

    VISITED_VALUES = [
        None, [], ["Gateway"], ["Gateway", "DataNode1", "Target"], "",
        "not_a_list", 42, True, False, {}, {"Gateway": True},
        [1, 2, 3], [None, None], [[], {}],
        ["Gateway", None, 42, [], "DataNode1"],
    ]

    def test_deterministic_auto_merge_adds_visited_to_revealed(self):
        """Basic: visited nodes end up in revealed_nodes."""
        hs = cpred_init_hack_state(tier="full_run")
        tool_input = {"hack_state": {
            "current_node": "DataNode1",
            "nodes_visited": ["Gateway", "DataNode1", "Target"],
            "revealed_nodes": ["Gateway"],
            "alert_level": 0,
        }}
        cpred_apply_hack_state(hs, tool_input)
        for node in ["Gateway", "DataNode1", "Target"]:
            self.assertIn(node, hs["revealed_nodes"])

    def test_deterministic_auto_merge_preserves_extra_revealed(self):
        """Revealed nodes from Pathfinder that aren't visited should be preserved."""
        hs = cpred_init_hack_state(tier="full_run")
        tool_input = {"hack_state": {
            "current_node": "Gateway",
            "nodes_visited": ["Gateway"],
            "revealed_nodes": ["Gateway", "DataNode1"],  # DataNode1 from Pathfinder
            "alert_level": 0,
        }}
        cpred_apply_hack_state(hs, tool_input)
        self.assertIn("DataNode1", hs["revealed_nodes"])
        self.assertIn("Gateway", hs["revealed_nodes"])

    def test_deterministic_no_duplicates_in_revealed(self):
        """Auto-merge should not create duplicate entries."""
        hs = cpred_init_hack_state(tier="full_run")
        tool_input = {"hack_state": {
            "current_node": "Gateway",
            "nodes_visited": ["Gateway", "Gateway", "Gateway"],
            "revealed_nodes": ["Gateway"],
            "alert_level": 0,
        }}
        cpred_apply_hack_state(hs, tool_input)
        self.assertEqual(hs["revealed_nodes"].count("Gateway"), 1)

    def test_fuzz_revealed_nodes_in_tool_input_never_crash(self):
        """Random revealed_nodes values in tool_input should never crash apply_hack_state."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (100, 200, 300):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cpred_init_hack_state(tier="full_run")
                        tool_input = {"hack_state": {}}
                        if random.random() < 0.7:
                            tool_input["hack_state"]["revealed_nodes"] = random.choice(self.REVEALED_VALUES)
                        if random.random() < 0.7:
                            tool_input["hack_state"]["nodes_visited"] = random.choice(self.VISITED_VALUES)
                        if random.random() < 0.5:
                            tool_input["hack_state"]["current_node"] = random.choice(ATOMS)
                        if random.random() < 0.5:
                            tool_input["hack_state"]["alert_level"] = random.choice(ATOMS)
                        cpred_apply_hack_state(hs, tool_input)
                        # revealed_nodes should always be a list after apply
                        self.assertIsInstance(hs.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)

    def test_fuzz_pre_corrupted_state_never_crash(self):
        """Even if hack_state was pre-corrupted before apply, auto-merge should survive."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (101, 202):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cpred_init_hack_state(tier=random.choice(TIER_VALUES) or "full_run")
                        # Pre-corrupt revealed_nodes and nodes_visited in the state
                        hs["revealed_nodes"] = random.choice(self.REVEALED_VALUES)
                        hs["nodes_visited"] = random.choice(self.VISITED_VALUES)
                        tool_input = {"hack_state": {
                            "alert_level": random.choice([0, 1, 2, 3, None]),
                        }}
                        cpred_apply_hack_state(hs, tool_input)
                        # Should not crash — revealed_nodes type depends on what was set
                        self.assertIn("revealed_nodes", hs)
        finally:
            logging.disable(logging.NOTSET)

    def test_fuzz_fully_random_tool_input_never_crash(self):
        """Fully random tool_input dicts should never crash apply_hack_state."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (111, 222, 333):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cpred_init_hack_state(tier="full_run")
                        # Build random tool_input
                        tool_input = {}
                        if random.random() < 0.8:
                            tool_input["hack_state"] = _rand_hack_state()
                        if random.random() < 0.3:
                            tool_input["available_actions"] = random.choice(ATOMS)
                        if random.random() < 0.3:
                            tool_input["system_map"] = random.choice(ATOMS)
                        cpred_apply_hack_state(hs, tool_input)
                        self.assertIsInstance(hs, dict)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# Fuzz: apply_net_combat_state auto-merge revealed_nodes
# ============================================================

class TestApplyNetCombatStateRevealedNodesFuzz(unittest.TestCase):
    """Fuzz the auto-merge logic in apply_net_combat_state."""

    REVEALED_VALUES = TestApplyHackStateRevealedNodesFuzz.REVEALED_VALUES
    VISITED_VALUES = TestApplyHackStateRevealedNodesFuzz.VISITED_VALUES

    def _make_pipeline_state(self, nc):
        return {
            "net_combat": nc,
            "character_states": {},
            "game_state": {"edgerunners": {}},
            "combat": _minimal_combat(),
        }

    def test_deterministic_auto_merge_works(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        ps = self._make_pipeline_state(nc)
        tool_input = {
            "hack_state": {
                "current_node": "DataNode1",
                "nodes_visited": ["Gateway", "DataNode1"],
                "revealed_nodes": ["Gateway"],
                "alert_level": 0,
            }
        }
        cpred_apply_net_combat_state(ps, tool_input)
        self.assertIn("DataNode1", nc["revealed_nodes"])
        self.assertIn("Gateway", nc["revealed_nodes"])

    def test_fuzz_revealed_nodes_in_tool_input_never_crash(self):
        """Random revealed_nodes values should never crash apply_net_combat_state."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (400, 500, 600):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                        ps = self._make_pipeline_state(nc)
                        tool_input = {"hack_state": {}}
                        if random.random() < 0.7:
                            tool_input["hack_state"]["revealed_nodes"] = random.choice(self.REVEALED_VALUES)
                        if random.random() < 0.7:
                            tool_input["hack_state"]["nodes_visited"] = random.choice(self.VISITED_VALUES)
                        if random.random() < 0.5:
                            tool_input["hack_state"]["current_node"] = random.choice(ATOMS)
                        cpred_apply_net_combat_state(ps, tool_input)
                        self.assertIsInstance(nc.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)

    def test_fuzz_fully_random_tool_input_never_crash(self):
        """Fully random tool_input dicts should never crash apply_net_combat_state."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (444, 555):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                        ps = self._make_pipeline_state(nc)
                        tool_input = {}
                        if random.random() < 0.8:
                            tool_input["hack_state"] = _rand_net_combat()
                        if random.random() < 0.3:
                            tool_input["available_actions"] = random.choice(ATOMS)
                        if random.random() < 0.3:
                            tool_input["combat_complete"] = random.choice(ATOMS)
                        cpred_apply_net_combat_state(ps, tool_input)
                        self.assertIsInstance(nc, dict)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# Fuzz: init_net_combat_from_hack revealed_nodes type guard
# ============================================================

class TestInitNetCombatFromHackRevealedNodesFuzz(unittest.TestCase):
    """Fuzz the type guard for revealed_nodes in init_net_combat_from_hack."""

    REVEALED_VALUES = [
        None, [], ["Gateway"], ["Gateway", "DataNode1"], "",
        "not_a_list", 42, True, False, {}, {"Gateway": True},
        [1, 2, 3], [None, None], [[], {}], [True, False],
        ["Gateway", None, 42, [], "DataNode1"],
        3.14, object, set(), frozenset(), b"bytes",
    ]

    def test_always_returns_list_for_revealed_nodes(self):
        """No matter what revealed_nodes is in hack_state, result should always be a list."""
        for val in self.REVEALED_VALUES:
            with self.subTest(revealed_nodes=repr(val)):
                hs = cpred_init_hack_state(tier="full_run")
                hs["revealed_nodes"] = val
                nc = cpred_init_net_combat_from_hack(hs)
                self.assertIsInstance(nc["revealed_nodes"], list)

    def test_fuzz_random_hack_state_never_crash(self):
        """Fully random hack_state dicts should never crash init_net_combat_from_hack."""
        logging.disable(logging.CRITICAL)
        try:
            for seed in (700, 800, 900):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(2000):
                        hs = cpred_init_hack_state(tier=random.choice(TIER_VALUES) or "full_run")
                        # Randomize the fields that init_net_combat_from_hack reads
                        for key in ["revealed_nodes", "nodes_visited", "ice_status",
                                    "active_programs", "tar_stacks", "interface_rank",
                                    "system_map", "brain_damage", "sr"]:
                            if random.random() < 0.5:
                                hs[key] = random.choice(ATOMS)
                        try:
                            nc = cpred_init_net_combat_from_hack(hs)
                            self.assertIsInstance(nc, dict)
                            self.assertIn("revealed_nodes", nc)
                            self.assertIsInstance(nc["revealed_nodes"], list)
                        except (TypeError, ValueError):
                            pass  # expected for non-numeric interface_rank
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# Fuzz: injection builders with malformed revealed_nodes
# ============================================================

class TestInjectionBuildersRevealedNodesFuzz(unittest.TestCase):
    """Fuzz revealed_nodes specifically in injection builders."""

    REVEALED_VALUES = [
        None, [], ["Gateway"], ["Gateway", "DataNode1", "Target"], "",
        "not_a_list", 42, True, False, {}, {"Gateway": True},
        [1, 2, 3], [None, None], [[], {}], [True, False],
        ["Gateway", None, 42, [], "DataNode1"],
        [None], [{}], [[]], [""], [0], [False],
        # Edge case: very long list
        [f"Node{i}" for i in range(100)],
        # Edge case: nodes with special characters
        ["Node\nNewline", "Node\tTab", "Node with spaces", "Node<html>", ""],
    ]

    def test_hack_injection_with_malformed_revealed_nodes(self):
        """build_hack_injection should never crash with any revealed_nodes value."""
        for val in self.REVEALED_VALUES:
            with self.subTest(revealed_nodes=repr(val)[:60]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = _sample_system_map()
                hs["revealed_nodes"] = val
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_hack_injection_without_map_with_malformed_revealed(self):
        """Nudge path + malformed revealed_nodes should not crash."""
        for val in self.REVEALED_VALUES:
            with self.subTest(revealed_nodes=repr(val)[:60]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["revealed_nodes"] = val
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
                self.assertIn(NUDGE_MARKER, result)

    def test_net_combat_injection_with_malformed_revealed_nodes(self):
        """build_net_combat_injection should never crash with any revealed_nodes value."""
        for val in self.REVEALED_VALUES:
            with self.subTest(revealed_nodes=repr(val)[:60]):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                nc["system_map"] = _sample_system_map()
                nc["revealed_nodes"] = val
                combat = _minimal_combat()
                ps = _minimal_pipeline_state()
                result = cpred_build_net_combat_injection(combat, nc, ps)
                self.assertIsInstance(result, str)

    def test_fuzz_revealed_nodes_combined_with_random_system_map(self):
        """Fuzz both system_map and revealed_nodes simultaneously."""
        map_values = [
            None, {}, _sample_system_map(),
            0, "", True, [], {"sr": "bad", "nodes": None},
        ]
        logging.disable(logging.CRITICAL)
        try:
            for seed in (1000, 2000):
                random.seed(seed)
                with self.subTest(seed=seed):
                    for _ in range(3000):
                        hs = cpred_init_hack_state(tier=random.choice(TIER_VALUES) or "full_run")
                        hs["system_map"] = random.choice(map_values)
                        hs["revealed_nodes"] = random.choice(self.REVEALED_VALUES)
                        result = cpred_build_hack_injection(hs)
                        self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
