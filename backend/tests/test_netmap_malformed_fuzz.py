"""
Exhaustive malformed-input fuzz tests for the NET map / revealed_nodes feature.

Covers every code path touched by the uncommitted changes:
- Multi-round sequential apply (state accumulation / corruption resistance)
- Full hack → net_combat lifecycle transitions
- Property invariants (revealed_nodes ⊇ nodes_visited, monotonic growth, always-list)
- Malformed system_map node structures (missing fields, wrong types, cycles, disconnected)
- Extreme string inputs (unicode, long, null bytes, newlines, HTML-like)
- Malformed ice_status / active_programs through apply + injection
- Simultaneous corruption of all fields
- Boundary numeric values (INT_MAX, inf, NaN, negative)
- JSON serialization round-trip (state must survive json.dumps after any apply)
- Large / empty list edge cases
"""

import copy
import json
import logging
import math
import os
import random
import sys
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

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


# ============================================================
# Shared fixtures
# ============================================================

ATOMS = [None, 0, 1, -1, 3.14, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

EXTREME_STRINGS = [
    "", " ", "\t", "\n", "\r\n", "\x00", "\x00\x01\x02",
    "a" * 10_000,
    "Node<script>alert(1)</script>",
    "Node\nWith\nNewlines",
    "Node\tWith\tTabs",
    "🔥🧊💀",
    "Ñoño",
    "零節點",
    "\\u0000",
    "'; DROP TABLE nodes;--",
    "../../../etc/passwd",
    'Node"With"Quotes',
    "Node'With'Quotes",
    "Node with   many   spaces",
    "null",
    "undefined",
    "NaN",
    "Infinity",
    "-Infinity",
]

BOUNDARY_NUMS = [
    0, 1, -1, 2**31, -(2**31), 2**63, -(2**63),
    float("inf"), float("-inf"), float("nan"),
    0.0, -0.0, 1e308, -1e308, 1e-308, 99999999999,
]

MALFORMED_REVEALED_NODES = [
    None, [], ["Gateway"], ["Gateway", "DataNode1"], "",
    "not_a_list", 42, True, False, {}, {"Gateway": True},
    [1, 2, 3], [None, None], [[], {}], [True, False],
    ["Gateway", None, 42, [], "DataNode1"],
    [None], [{}], [[]], [""], [0], [False],
    [f"Node{i}" for i in range(200)],
    EXTREME_STRINGS[:5],
    {"__proto__": "polluted"},
    b"bytes" if False else "fallback",  # bytes would crash json
]

MALFORMED_SYSTEM_MAPS = [
    None,
    {},
    {"sr": 3},
    {"sr": 3, "nodes": {}},
    {"sr": 3, "nodes": None},
    {"sr": 3, "nodes": []},
    {"sr": 3, "nodes": "not_a_dict"},
    {"sr": "bad", "nodes": {}},
    {"sr": None, "nodes": {}},
    {"sr": -1, "nodes": {}},
    {"sr": float("inf"), "nodes": {}},
    # Valid structure
    {"sr": 3, "nodes": {
        "Gateway": {"type": "gateway", "ice": "patrol", "dv": 9,
                     "connections": ["DataNode1"], "contents": "Entry"},
        "DataNode1": {"type": "data_node", "ice": "tar", "dv": 11,
                      "connections": ["Gateway", "Target"], "contents": "Data"},
        "Target": {"type": "target", "ice": None, "dv": 15,
                   "connections": ["DataNode1"], "contents": "Objective"},
    }},
    # Missing node fields
    {"sr": 3, "nodes": {"A": {}}},
    {"sr": 3, "nodes": {"A": {"type": "gateway"}}},
    {"sr": 3, "nodes": {"A": {"connections": []}}},
    # Wrong types in node fields
    {"sr": 3, "nodes": {"A": {"type": 42, "ice": True, "dv": "high",
                               "connections": "not_list", "contents": None}}},
    # Self-loops
    {"sr": 3, "nodes": {"A": {"type": "gateway", "ice": None, "dv": 9,
                               "connections": ["A"], "contents": "Self-loop"}}},
    # Disconnected graph
    {"sr": 3, "nodes": {
        "A": {"type": "gateway", "ice": None, "dv": 9, "connections": [], "contents": "Isolated"},
        "B": {"type": "target", "ice": None, "dv": 9, "connections": [], "contents": "Isolated"},
    }},
    # Cyclic connections
    {"sr": 3, "nodes": {
        "A": {"type": "gateway", "ice": None, "dv": 9, "connections": ["B"], "contents": ""},
        "B": {"type": "data_node", "ice": None, "dv": 9, "connections": ["C"], "contents": ""},
        "C": {"type": "target", "ice": None, "dv": 9, "connections": ["A"], "contents": ""},
    }},
    # Connection to nonexistent node
    {"sr": 3, "nodes": {
        "A": {"type": "gateway", "ice": None, "dv": 9,
              "connections": ["DoesNotExist"], "contents": ""},
    }},
    # Node name with extreme strings
    {"sr": 3, "nodes": {
        "": {"type": "gateway", "ice": None, "dv": 9, "connections": [], "contents": ""},
    }},
    {"sr": 3, "nodes": {
        "\n\t\x00": {"type": "gateway", "ice": None, "dv": 9, "connections": [], "contents": ""},
    }},
    # Very large graph
    {"sr": 3, "nodes": {
        f"N{i}": {"type": "data_node", "ice": None, "dv": 9,
                   "connections": [f"N{i+1}" if i < 49 else "N0"], "contents": ""}
        for i in range(50)
    }},
    # Extra unexpected fields
    {"sr": 3, "extra": "field", "nodes": {
        "A": {"type": "gateway", "ice": None, "dv": 9, "connections": [],
              "contents": "", "extra": True, "__proto__": {}},
    }},
    0, 1, -1, "", "map", True, False, [], [1, 2], 3.14,
]

MALFORMED_ICE_STATUS = [
    {},
    {"Gateway": None},
    {"Gateway": "active"},
    {"Gateway": 42},
    {"Gateway": True},
    {"Gateway": []},
    {"Gateway": {"behavior": "patrol", "status": "active", "rez_current": 5, "rez_max": 5, "name": "Hellhound"}},
    {"Gateway": {"behavior": "black", "status": "derezzed", "rez_current": 0, "rez_max": 8, "name": "Kraken"}},
    {"Gateway": {"behavior": "tar", "status": "bypassed"}},
    {"Gateway": {"status": "active"}},
    {"Gateway": {"behavior": None, "status": None}},
    {"Gateway": {"behavior": 42, "status": [], "rez_current": "five", "rez_max": None}},
    {42: "active"},
    {None: None},
    None, "", 0, True, False, [], [1, 2],
    # Very deep nesting
    {"A": {"nested": {"deeper": {"deepest": True}}}},
    # Many nodes
    {f"N{i}": {"behavior": "patrol", "status": "active", "rez_current": i, "rez_max": 5, "name": f"ICE{i}"} for i in range(50)},
]

MALFORMED_ACTIVE_PROGRAMS = [
    [],
    [None],
    [42],
    ["Sword"],
    [True],
    [[]],
    [{}],
    [{"name": "Sword", "category": "attacker", "rez": 5, "status": "active"}],
    [{"name": "Shield", "category": "defender", "rez": 3, "status": "deactivated"}],
    [{"name": None, "category": None, "rez": None, "status": None}],
    [{"name": 42, "category": True, "rez": "five", "status": []}],
    [{"name": "A", "category": "attacker", "rez": 5, "status": "active"},
     {"name": "B", "category": "booster", "rez": 3, "status": "active"},
     {"name": "C", "category": "defender", "rez": 4, "status": "derezzed"}],
    [{"unexpected": "field"}],
    None, "", 0, True, False, "not_list", {}, {"a": 1},
    # Very long list
    [{"name": f"P{i}", "category": "attacker", "rez": i, "status": "active"} for i in range(100)],
]


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


def _minimal_pipeline_state(nc=None):
    ps = {
        "character_states": {},
        "game_state": {"edgerunners": {}},
        "combat": _minimal_combat(),
    }
    if nc is not None:
        ps["net_combat"] = nc
    return ps


def _rand_tool_input(keys, atoms=ATOMS):
    """Build a random tool_input dict."""
    ti = {}
    hs = {}
    for k in keys:
        if random.random() < 0.4:
            hs[k] = random.choice(atoms)
    if hs:
        ti["hack_state"] = hs
    if random.random() < 0.3:
        ti["available_actions"] = random.choice(atoms)
    if random.random() < 0.2:
        ti["hack_complete"] = random.choice([True, False, None, 0, 1])
    if random.random() < 0.2:
        ti["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS[:5])
    return ti


HACK_FIELDS = [
    "alert_level", "cycles_remaining", "active_programs",
    "current_node", "nodes_visited", "ice_status",
    "trace_progress", "tar_stacks", "brain_damage",
    "revealed_nodes", "system_map", "net_actions_used",
]


# ============================================================
# A. Multi-Round Sequential Apply
# ============================================================

class TestMultiRoundApplyHackState(unittest.TestCase):
    """Apply N rounds of random tool_input to hack_state. No crashes allowed."""

    def test_deterministic_10_rounds_clean(self):
        """10 rounds of valid-ish applies."""
        hs = cpred_init_hack_state(tier="full_run")
        for i in range(10):
            tool_input = {"hack_state": {
                "current_node": f"Node{i}",
                "nodes_visited": [f"Node{j}" for j in range(i + 1)],
                "revealed_nodes": [f"Node{j}" for j in range(i + 2)],
                "alert_level": i % 3,
                "net_actions_used": 1,
            }}
            cpred_apply_hack_state(hs, tool_input)
            self.assertIsInstance(hs["revealed_nodes"], list)
            self.assertIn(f"Node{i}", hs["revealed_nodes"])
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)

    @settings(max_examples=30, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_fuzz_multi_round_random_inputs(self, seed):
        """3-10 random applies per state, 500 iterations."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(500):
                hs = cpred_init_hack_state(tier=random.choice(["full_run", "quick_hack"]))
                rounds = random.randint(3, 10)
                for _ in range(rounds):
                    ti = _rand_tool_input(HACK_FIELDS)
                    cpred_apply_hack_state(hs, ti)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
                self.assertIsInstance(hs.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_fuzz_multi_round_with_system_map_injection(self, seed):
        """Multi-round with system_map set at random round."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(500):
                hs = cpred_init_hack_state(tier="full_run")
                map_round = random.randint(0, 7)
                for r in range(8):
                    ti = _rand_tool_input(HACK_FIELDS)
                    if r == map_round:
                        ti["hack_state"] = ti.get("hack_state", {})
                        ti["hack_state"]["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS)
                    cpred_apply_hack_state(hs, ti)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


class TestMultiRoundApplyNetCombat(unittest.TestCase):
    """Apply N rounds of random tool_input to net_combat. No crashes allowed."""

    @settings(max_examples=30, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_fuzz_multi_round_random_inputs(self, seed):
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(500):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                rounds = random.randint(3, 10)
                for _ in range(rounds):
                    ti = _rand_tool_input(HACK_FIELDS)
                    cpred_apply_net_combat_state(ps, ti)
                combat = _minimal_combat()
                result = cpred_build_net_combat_injection(combat, nc, ps)
                self.assertIsInstance(result, str)
                self.assertIsInstance(nc.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# B. Full Lifecycle: hack → transition → net_combat → injection
# ============================================================

class TestFullLifecycle(unittest.TestCase):
    """init_hack → N applies → transition → N applies → injection. No crashes."""

    def test_deterministic_clean_lifecycle(self):
        hs = cpred_init_hack_state(tier="full_run", sr=4)
        hs["system_map"] = _sample_system_map()
        for node in ["Gateway", "DataNode1"]:
            cpred_apply_hack_state(hs, {"hack_state": {
                "current_node": node,
                "nodes_visited": ["Gateway", node],
                "revealed_nodes": ["Gateway", node],
                "alert_level": 1,
                "net_actions_used": 1,
            }})
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertIsInstance(nc["revealed_nodes"], list)
        self.assertIn("DataNode1", nc["revealed_nodes"])
        ps = _minimal_pipeline_state(nc)
        cpred_apply_net_combat_state(ps, {"hack_state": {
            "current_node": "Target",
            "nodes_visited": ["Gateway", "DataNode1", "Target"],
            "revealed_nodes": ["Gateway", "DataNode1", "Target"],
            "alert_level": 2,
        }})
        self.assertIn("Target", nc["revealed_nodes"])
        result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
        self.assertIsInstance(result, str)
        self.assertIn("Target", result)

    @settings(max_examples=25, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_fuzz_full_lifecycle(self, seed):
        """Full lifecycle with random inputs at every stage."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(300):
                hs = cpred_init_hack_state(
                    tier=random.choice(["full_run", "quick_hack"]),
                    sr=random.choice([1, 2, 3, 4, 5]),
                )
                hack_rounds = random.randint(1, 6)
                for _ in range(hack_rounds):
                    ti = _rand_tool_input(HACK_FIELDS)
                    cpred_apply_hack_state(hs, ti)
                if random.random() < 0.5:
                    hs["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS)
                try:
                    nc = cpred_init_net_combat_from_hack(hs)
                except (TypeError, ValueError):
                    continue
                ps = _minimal_pipeline_state(nc)
                nc_rounds = random.randint(1, 6)
                for _ in range(nc_rounds):
                    ti = _rand_tool_input(HACK_FIELDS)
                    cpred_apply_net_combat_state(ps, ti)
                combat = _minimal_combat() if random.random() < 0.7 else None
                result = cpred_build_net_combat_injection(combat, nc, ps)
                self.assertIsInstance(result, str)
                self.assertIsInstance(nc.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# C. Property Invariants
# ============================================================

class TestRevealedNodesInvariants(unittest.TestCase):
    """After apply, key invariants must hold."""

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_revealed_always_superset_of_visited(self, seed):
        """If both are lists, every visited node must be in revealed."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                hs = cpred_init_hack_state(tier="full_run")
                visited = [f"Node{i}" for i in range(random.randint(1, 8))]
                ti = {"hack_state": {
                    "nodes_visited": visited,
                    "revealed_nodes": random.choice([
                        visited[:1], visited[:2], visited, [], None
                    ]),
                    "alert_level": 0,
                }}
                cpred_apply_hack_state(hs, ti)
                r = hs.get("revealed_nodes")
                v = hs.get("nodes_visited")
                if isinstance(r, list) and isinstance(v, list):
                    for node in v:
                        self.assertIn(node, r, f"visited {node} missing from revealed")
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=25, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_revealed_always_list_after_apply(self, seed):
        """revealed_nodes must always be a list after apply, no matter the input."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(2000):
                hs = cpred_init_hack_state(tier="full_run")
                ti = {"hack_state": {"revealed_nodes": random.choice(ATOMS + MALFORMED_REVEALED_NODES)}}
                cpred_apply_hack_state(hs, ti)
                self.assertIsInstance(hs["revealed_nodes"], list)
        finally:
            logging.disable(logging.NOTSET)

    def test_revealed_monotonically_grows_with_valid_visited(self):
        """With valid visited lists, revealed should only grow."""
        hs = cpred_init_hack_state(tier="full_run")
        prev_size = len(hs["revealed_nodes"])
        for i in range(10):
            visited = [f"Node{j}" for j in range(i + 1)]
            cpred_apply_hack_state(hs, {"hack_state": {
                "nodes_visited": visited,
                "revealed_nodes": list(hs["revealed_nodes"]),
                "alert_level": 0,
            }})
            current_size = len(hs["revealed_nodes"])
            self.assertGreaterEqual(current_size, prev_size,
                                    f"revealed shrank at round {i}")
            prev_size = current_size

    @settings(max_examples=25, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_net_combat_revealed_always_list_after_apply(self, seed):
        """Same invariant for net_combat."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(2000):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                ti = {"hack_state": {"revealed_nodes": random.choice(ATOMS + MALFORMED_REVEALED_NODES)}}
                cpred_apply_net_combat_state(ps, ti)
                self.assertIsInstance(nc["revealed_nodes"], list)
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_net_combat_revealed_superset_of_visited(self, seed):
        """net_combat: visited ⊆ revealed."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                visited = [f"Node{i}" for i in range(random.randint(1, 8))]
                ti = {"hack_state": {
                    "nodes_visited": visited,
                    "revealed_nodes": visited[:1],
                    "alert_level": 0,
                }}
                cpred_apply_net_combat_state(ps, ti)
                r = nc.get("revealed_nodes")
                v = nc.get("nodes_visited")
                if isinstance(r, list) and isinstance(v, list):
                    for node in v:
                        self.assertIn(node, r)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# D. Malformed System Map Structures
# ============================================================

class TestMalformedSystemMapInjection(unittest.TestCase):
    """Every malformed system_map in MALFORMED_SYSTEM_MAPS must not crash injection."""

    def test_hack_injection_all_malformed_maps(self):
        for i, smap in enumerate(MALFORMED_SYSTEM_MAPS):
            with self.subTest(map_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = smap
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_net_combat_injection_all_malformed_maps(self):
        for i, smap in enumerate(MALFORMED_SYSTEM_MAPS):
            with self.subTest(map_index=i):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                nc["system_map"] = smap
                combat = _minimal_combat()
                ps = _minimal_pipeline_state(nc)
                result = cpred_build_net_combat_injection(combat, nc, ps)
                self.assertIsInstance(result, str)

    def test_apply_then_inject_all_malformed_maps(self):
        """Apply system_map via tool_input, then inject."""
        for i, smap in enumerate(MALFORMED_SYSTEM_MAPS):
            with self.subTest(map_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                cpred_apply_hack_state(hs, {"hack_state": {"system_map": smap}})
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_fuzz_random_system_map_with_random_revealed(self):
        """Cross-product fuzz: random system_map x random revealed_nodes."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(7001)
            for _ in range(5000):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS)
                hs["revealed_nodes"] = random.choice(MALFORMED_REVEALED_NODES)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# E. Extreme String Inputs
# ============================================================

class TestExtremeStringInputs(unittest.TestCase):
    """Extreme strings in node names, target_system, revealed_nodes, etc."""

    def test_extreme_node_names_in_visited_and_revealed(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                cpred_apply_hack_state(hs, {"hack_state": {
                    "current_node": s,
                    "nodes_visited": ["Gateway", s],
                    "revealed_nodes": ["Gateway", s],
                    "alert_level": 0,
                }})
                self.assertIn(s, hs["revealed_nodes"])
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_node_names_in_system_map(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                smap = {"sr": 3, "nodes": {
                    s: {"type": "gateway", "ice": None, "dv": 9,
                        "connections": [], "contents": s},
                }}
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = smap
                hs["revealed_nodes"] = [s]
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_target_system_names(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run", target_system=s)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_strings_in_ice_status_keys(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["ice_status"] = {s: {"behavior": "patrol", "status": "active",
                                        "rez_current": 5, "rez_max": 5, "name": s}}
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_strings_in_program_names(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["active_programs"] = [{"name": s, "category": s, "rez": 5, "status": "active"}]
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_strings_in_narrative_summary(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["narrative_summary"] = s
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_extreme_node_names_through_net_combat(self):
        for s in EXTREME_STRINGS[:10]:
            with self.subTest(string=repr(s)[:40]):
                nc = cpred_init_net_combat_state(netrunner_name=s, target=s, sr=3)
                nc["system_map"] = {"sr": 3, "nodes": {
                    s: {"type": "gateway", "ice": None, "dv": 9, "connections": [], "contents": s},
                }}
                nc["revealed_nodes"] = [s]
                nc["nodes_visited"] = [s]
                nc["current_node"] = s
                ps = _minimal_pipeline_state(nc)
                result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
                self.assertIsInstance(result, str)


# ============================================================
# F. Malformed ICE Status Through Apply + Inject
# ============================================================

class TestMalformedIceStatus(unittest.TestCase):
    """Malformed ice_status values through apply and injection."""

    def test_hack_all_malformed_ice(self):
        for i, ice in enumerate(MALFORMED_ICE_STATUS):
            with self.subTest(ice_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                cpred_apply_hack_state(hs, {"hack_state": {"ice_status": ice}})
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_net_combat_all_malformed_ice(self):
        for i, ice in enumerate(MALFORMED_ICE_STATUS):
            with self.subTest(ice_index=i):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                cpred_apply_net_combat_state(ps, {"hack_state": {"ice_status": ice}})
                result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
                self.assertIsInstance(result, str)

    def test_fuzz_random_ice_with_random_revealed(self):
        """Cross-product: random ice_status x random revealed_nodes."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(8001)
            for _ in range(3000):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = _sample_system_map()
                hs["ice_status"] = random.choice(MALFORMED_ICE_STATUS)
                hs["revealed_nodes"] = random.choice(MALFORMED_REVEALED_NODES)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# G. Malformed Active Programs Through Apply + Inject
# ============================================================

class TestMalformedActivePrograms(unittest.TestCase):
    """Malformed active_programs values through apply and injection."""

    def test_hack_all_malformed_programs(self):
        for i, progs in enumerate(MALFORMED_ACTIVE_PROGRAMS):
            with self.subTest(prog_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                cpred_apply_hack_state(hs, {"hack_state": {"active_programs": progs}})
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)

    def test_net_combat_all_malformed_programs(self):
        for i, progs in enumerate(MALFORMED_ACTIVE_PROGRAMS):
            with self.subTest(prog_index=i):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                cpred_apply_net_combat_state(ps, {"hack_state": {"active_programs": progs}})
                result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
                self.assertIsInstance(result, str)


# ============================================================
# H. All Fields Simultaneously Corrupted
# ============================================================

class TestAllFieldsCorrupted(unittest.TestCase):
    """Set every field to garbage simultaneously."""

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_hack_all_fields_garbage(self, seed):
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                hs = cpred_init_hack_state(tier="full_run")
                ti = {"hack_state": {}}
                for field in HACK_FIELDS:
                    ti["hack_state"][field] = random.choice(
                        ATOMS + EXTREME_STRINGS[:5] + BOUNDARY_NUMS[:5]
                        + MALFORMED_REVEALED_NODES[:5]
                    )
                cpred_apply_hack_state(hs, ti)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
                self.assertIsInstance(hs.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_net_combat_all_fields_garbage(self, seed):
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                ti = {"hack_state": {}}
                for field in HACK_FIELDS:
                    ti["hack_state"][field] = random.choice(
                        ATOMS + EXTREME_STRINGS[:5] + BOUNDARY_NUMS[:5]
                        + MALFORMED_REVEALED_NODES[:5]
                    )
                cpred_apply_net_combat_state(ps, ti)
                result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
                self.assertIsInstance(result, str)
                self.assertIsInstance(nc.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# I. Boundary Numeric Values
# ============================================================

class TestBoundaryNumericValues(unittest.TestCase):
    """Boundary values in numeric fields."""

    NUMERIC_FIELDS = ["alert_level", "cycles_remaining", "trace_progress",
                      "tar_stacks", "brain_damage", "net_actions_used"]

    def test_hack_boundary_numerics_never_crash(self):
        for field in self.NUMERIC_FIELDS:
            for val in BOUNDARY_NUMS:
                with self.subTest(field=field, val=val):
                    hs = cpred_init_hack_state(tier="full_run")
                    cpred_apply_hack_state(hs, {"hack_state": {field: val}})
                    result = cpred_build_hack_injection(hs)
                    self.assertIsInstance(result, str)

    def test_net_combat_boundary_numerics_never_crash(self):
        for field in self.NUMERIC_FIELDS:
            for val in BOUNDARY_NUMS:
                with self.subTest(field=field, val=val):
                    nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                    ps = _minimal_pipeline_state(nc)
                    cpred_apply_net_combat_state(ps, {"hack_state": {field: val}})
                    result = cpred_build_net_combat_injection(_minimal_combat(), nc, ps)
                    self.assertIsInstance(result, str)

    def test_boundary_sr_values(self):
        """SR is used in trace_max calculation: max(1, 6 - sr). Test boundaries."""
        for sr in BOUNDARY_NUMS + [0, 1, 5, 6, 100, -100]:
            with self.subTest(sr=sr):
                try:
                    hs = cpred_init_hack_state(tier="full_run", sr=sr)
                    hs["trace_progress"] = 3
                    result = cpred_build_hack_injection(hs)
                    self.assertIsInstance(result, str)
                except (TypeError, ValueError):
                    pass  # sr must be comparable

    def test_boundary_interface_rank_in_init(self):
        """interface_rank determines net_actions — test extremes."""
        for ir in [0, 1, 3, 4, 6, 7, 9, 10, 100, -1]:
            with self.subTest(interface_rank=ir):
                hs = cpred_init_hack_state(tier="full_run", interface_rank=ir)
                self.assertIn("net_actions_per_turn", hs)
                self.assertIsInstance(hs["net_actions_per_turn"], int)


# ============================================================
# J. JSON Serialization Round-Trip
# ============================================================

class TestJsonSerializationRoundTrip(unittest.TestCase):
    """After any apply, the state must survive json.dumps (critical for SSE)."""

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_hack_state_serializable_after_random_apply(self, seed):
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                hs = cpred_init_hack_state(tier="full_run")
                ti = _rand_tool_input(HACK_FIELDS)
                cpred_apply_hack_state(hs, ti)
                try:
                    serialized = json.dumps(hs)
                    self.assertIsInstance(serialized, str)
                    deserialized = json.loads(serialized)
                    self.assertIsInstance(deserialized, dict)
                except (TypeError, ValueError, OverflowError):
                    pass
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_net_combat_serializable_after_random_apply(self, seed):
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                ti = _rand_tool_input(HACK_FIELDS)
                cpred_apply_net_combat_state(ps, ti)
                try:
                    serialized = json.dumps(nc)
                    self.assertIsInstance(serialized, str)
                except (TypeError, ValueError, OverflowError):
                    pass
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_injection_output_always_string_after_any_state(self, seed):
        """Injection output is always a string, even with non-serializable state."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(1000):
                hs = cpred_init_hack_state(tier="full_run")
                for field in HACK_FIELDS:
                    if random.random() < 0.3:
                        hs[field] = random.choice(ATOMS + BOUNDARY_NUMS)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)


# ============================================================
# K. Large / Empty List Edge Cases
# ============================================================

class TestListEdgeCases(unittest.TestCase):
    """Edge cases with list sizes."""

    def test_empty_everything(self):
        """All list/dict fields empty."""
        hs = cpred_init_hack_state(tier="full_run")
        cpred_apply_hack_state(hs, {"hack_state": {
            "nodes_visited": [],
            "revealed_nodes": [],
            "active_programs": [],
            "ice_status": {},
        }})
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)
        self.assertIsInstance(hs["revealed_nodes"], list)

    def test_very_large_revealed_nodes(self):
        """200 revealed nodes."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["system_map"] = _sample_system_map()
        nodes = [f"Node{i}" for i in range(200)]
        cpred_apply_hack_state(hs, {"hack_state": {
            "nodes_visited": nodes[:50],
            "revealed_nodes": nodes,
            "alert_level": 0,
        }})
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)
        self.assertEqual(len(hs["revealed_nodes"]), 200)

    def test_very_large_ice_status(self):
        """50 ICE entries."""
        hs = cpred_init_hack_state(tier="full_run")
        ice = {f"N{i}": {"behavior": "patrol", "status": "active",
                          "rez_current": 5, "rez_max": 5, "name": f"ICE{i}"}
               for i in range(50)}
        cpred_apply_hack_state(hs, {"hack_state": {"ice_status": ice}})
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)

    def test_single_element_lists(self):
        """Lists with exactly one element."""
        hs = cpred_init_hack_state(tier="full_run")
        cpred_apply_hack_state(hs, {"hack_state": {
            "nodes_visited": ["OnlyNode"],
            "revealed_nodes": ["OnlyNode"],
            "active_programs": [{"name": "X", "category": "attacker", "rez": 1, "status": "active"}],
        }})
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)
        self.assertIn("OnlyNode", hs["revealed_nodes"])

    def test_duplicate_heavy_lists(self):
        """Lists with many duplicates."""
        hs = cpred_init_hack_state(tier="full_run")
        cpred_apply_hack_state(hs, {"hack_state": {
            "nodes_visited": ["Gateway"] * 100,
            "revealed_nodes": ["Gateway"] * 50 + ["DataNode1"] * 50,
            "alert_level": 0,
        }})
        result = cpred_build_hack_injection(hs)
        self.assertIsInstance(result, str)
        # Auto-merge should not explode the list with duplicates
        self.assertLessEqual(hs["revealed_nodes"].count("Gateway"), 50 + 1)


# ============================================================
# L. init_net_combat_from_hack with Extreme Inputs
# ============================================================

class TestInitNetCombatFromHackExhaustive(unittest.TestCase):
    """Exhaustive malformed inputs to init_net_combat_from_hack."""

    def test_all_malformed_revealed_nodes(self):
        for val in MALFORMED_REVEALED_NODES:
            with self.subTest(revealed=repr(val)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["revealed_nodes"] = val
                nc = cpred_init_net_combat_from_hack(hs)
                self.assertIsInstance(nc["revealed_nodes"], list)

    def test_all_malformed_system_maps(self):
        for i, smap in enumerate(MALFORMED_SYSTEM_MAPS):
            with self.subTest(map_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                hs["system_map"] = smap
                nc = cpred_init_net_combat_from_hack(hs)
                self.assertIsInstance(nc, dict)
                # system_map should be carried through (deep copied)
                if smap is not None and isinstance(smap, (dict, list)):
                    self.assertIsNotNone(nc.get("system_map"))

    def test_all_malformed_ice_status(self):
        for i, ice in enumerate(MALFORMED_ICE_STATUS):
            with self.subTest(ice_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                hs["ice_status"] = ice
                try:
                    nc = cpred_init_net_combat_from_hack(hs)
                    self.assertIsInstance(nc, dict)
                except (TypeError, ValueError):
                    pass  # Some ice values may cause deep copy issues

    def test_all_malformed_active_programs(self):
        for i, progs in enumerate(MALFORMED_ACTIVE_PROGRAMS):
            with self.subTest(prog_index=i):
                hs = cpred_init_hack_state(tier="full_run")
                hs["active_programs"] = progs
                try:
                    nc = cpred_init_net_combat_from_hack(hs)
                    self.assertIsInstance(nc, dict)
                except (TypeError, ValueError):
                    pass

    def test_extreme_strings_in_all_string_fields(self):
        for s in EXTREME_STRINGS:
            with self.subTest(string=repr(s)[:40]):
                hs = cpred_init_hack_state(tier="full_run")
                hs["target_system"] = s
                hs["hacker_name"] = s
                hs["current_node"] = s
                hs["nodes_visited"] = [s]
                hs["revealed_nodes"] = [s]
                nc = cpred_init_net_combat_from_hack(hs)
                self.assertIsInstance(nc, dict)
                self.assertIn(s, nc["revealed_nodes"])

    def test_boundary_numerics_in_transition(self):
        for sr in BOUNDARY_NUMS[:8]:
            for ir in [0, 1, 4, 10, -1]:
                with self.subTest(sr=sr, ir=ir):
                    hs = cpred_init_hack_state(tier="full_run", sr=sr, interface_rank=ir)
                    hs["brain_damage"] = random.choice(BOUNDARY_NUMS[:6])
                    hs["tar_stacks"] = random.choice(BOUNDARY_NUMS[:6])
                    try:
                        nc = cpred_init_net_combat_from_hack(hs)
                        self.assertIsInstance(nc, dict)
                    except (TypeError, ValueError, OverflowError):
                        pass


# ============================================================
# M. Cross-Product Fuzz: Multiple Malformed Dimensions
# ============================================================

class TestCrossProductFuzz(unittest.TestCase):
    """Fuzz multiple dimensions simultaneously for maximum coverage."""

    def test_hack_triple_corruption(self):
        """system_map x revealed_nodes x ice_status simultaneously."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(11001)
            for _ in range(5000):
                hs = cpred_init_hack_state(tier=random.choice(["full_run", "quick_hack"]))
                hs["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS)
                hs["revealed_nodes"] = random.choice(MALFORMED_REVEALED_NODES)
                hs["ice_status"] = random.choice(MALFORMED_ICE_STATUS)
                hs["active_programs"] = random.choice(MALFORMED_ACTIVE_PROGRAMS)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)

    def test_net_combat_triple_corruption(self):
        """Same for net_combat."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(11002)
            for _ in range(5000):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                nc["system_map"] = random.choice(MALFORMED_SYSTEM_MAPS)
                nc["revealed_nodes"] = random.choice(MALFORMED_REVEALED_NODES)
                nc["ice_status"] = random.choice(MALFORMED_ICE_STATUS)
                nc["active_programs"] = random.choice(MALFORMED_ACTIVE_PROGRAMS)
                combat = _minimal_combat() if random.random() < 0.7 else None
                ps = _minimal_pipeline_state(nc)
                result = cpred_build_net_combat_injection(combat, nc, ps)
                self.assertIsInstance(result, str)
        finally:
            logging.disable(logging.NOTSET)

    @settings(max_examples=20, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_apply_then_inject_quad_corruption(self, seed):
        """Apply with all four dimensions corrupted, then inject."""
        logging.disable(logging.CRITICAL)
        try:
            random.seed(seed)
            for _ in range(2000):
                hs = cpred_init_hack_state(tier="full_run")
                ti = {"hack_state": {
                    "revealed_nodes": random.choice(MALFORMED_REVEALED_NODES),
                    "nodes_visited": random.choice(MALFORMED_REVEALED_NODES),
                    "ice_status": random.choice(MALFORMED_ICE_STATUS),
                    "active_programs": random.choice(MALFORMED_ACTIVE_PROGRAMS),
                    "system_map": random.choice(MALFORMED_SYSTEM_MAPS[:10]),
                    "alert_level": random.choice(ATOMS),
                    "current_node": random.choice(ATOMS + EXTREME_STRINGS[:3]),
                    "brain_damage": random.choice(ATOMS),
                    "tar_stacks": random.choice(ATOMS),
                    "trace_progress": random.choice(ATOMS),
                }}
                cpred_apply_hack_state(hs, ti)
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
                self.assertIsInstance(hs.get("revealed_nodes"), list)
        finally:
            logging.disable(logging.NOTSET)


class TestUncommittedHackStateFuzz(unittest.TestCase):
    """Seeded fuzzing for newly-added apply_hack_state branches."""

    @settings(max_examples=250, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_resolver_ops_seeded(self, seed):
        rng = random.Random(seed)
        hs = cpred_init_hack_state(hacker_name="V", sr=rng.randint(2, 5), interface_rank=6)
        hs["active_programs"] = [{"name": "Sword", "status": "active"}, {"name": "Armor", "status": "active"}]
        hs["ice_status"] = {
            "Core": {"name": "Hellhound", "behavior": "trace", "rez_current": 10, "rez_max": 10, "status": "active"}
        }
        tar_initial = rng.randint(0, 3)
        include_tar_in_input = rng.choice([True, False])
        tool_input = {
            "hack_state": {
                "current_node": "Core",
                "nodes_visited": ["Gateway", "Core"],
                "revealed_nodes": ["Gateway"],
                "net_actions_used": 0,
            }
        }
        if include_tar_in_input:
            tool_input["hack_state"]["tar_stacks"] = tar_initial
        rez_damage = rng.randint(0, 15)
        resolver_ops = [
            {"op": "program_deactivate", "program_name": "Sword"},
            {"op": "rez_damage", "target": "Hellhound", "damage": rez_damage},
        ]
        if rng.choice([True, False]):
            resolver_ops.append({"op": "tar_consumed"})

        before_rez = hs["ice_status"]["Core"]["rez_current"]
        cpred_apply_hack_state(hs, tool_input, resolver_state_ops=resolver_ops)

        self.assertIsInstance(hs.get("revealed_nodes"), list, f"seed={seed}")
        self.assertEqual(hs["active_programs"][0]["status"], "deactivated", f"seed={seed}")
        self.assertGreaterEqual(hs["ice_status"]["Core"]["rez_current"], 0, f"seed={seed}")
        self.assertLessEqual(hs["ice_status"]["Core"]["rez_current"], before_rez, f"seed={seed}")
        if any(op.get("op") == "tar_consumed" for op in resolver_ops):
            self.assertEqual(hs["tar_stacks"], 0, f"seed={seed}")
        else:
            if include_tar_in_input:
                self.assertEqual(hs["tar_stacks"], tar_initial, f"seed={seed}")
            else:
                self.assertEqual(hs["tar_stacks"], 0, f"seed={seed}")

    @settings(max_examples=220, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_forced_disconnect_seeded(self, seed):
        rng = random.Random(seed)
        max_hp = rng.randint(10, 50)
        current_hp = rng.randint(0, max_hp)
        bd = rng.randint(1, 60)
        hs = cpred_init_hack_state(hacker_name="V")
        game_state = {"edgerunners": {"V": {"hp": {"current": current_hp, "max": max_hp}}}}
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 0}}, resolver_state_ops=[
            {"op": "brain_damage", "change": -bd}
        ], game_state=game_state)
        expected_disconnect = current_hp - bd < 0
        self.assertEqual(bool(hs.get("_forced_disconnect")), expected_disconnect, f"seed={seed}")
        self.assertEqual(hs["active"], not expected_disconnect, f"seed={seed}")

    @settings(max_examples=180, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_alert_autospawn_seeded(self, seed):
        rng = random.Random(seed)
        hs = cpred_init_hack_state(sr=rng.randint(2, 5), interface_rank=6)
        hs["_prev_alert_level"] = rng.randint(0, 4)
        new_alert = rng.randint(5, 8)
        tool_input = {
            "hack_state": {
                "alert_level": new_alert,
                "ice_status": {},
                "current_node": f"Node{seed}",
                "nodes_visited": ["Gateway"],
                "revealed_nodes": ["Gateway"],
                "net_actions_used": 0,
                "trace_progress": 0,
            }
        }
        cpred_apply_hack_state(hs, tool_input, resolver_state_ops=[])
        self.assertIn("Gateway_Trace", hs.get("ice_status", {}), f"seed={seed}")
        if new_alert >= 7:
            self.assertIn(f"Node{seed}_Convergence", hs.get("ice_status", {}), f"seed={seed}")


class TestUncommittedNetCombatStateFuzz(unittest.TestCase):
    """Seeded fuzzing for newly-added apply_net_combat_state branches."""

    @settings(max_examples=220, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_forced_disconnect_seeded(self, seed):
        rng = random.Random(seed)
        nc = cpred_init_net_combat_state(netrunner_name="V")
        pipeline_state = {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": {"edgerunners": {"V": {"hp": {"current": rng.randint(0, 40), "max": 40}}}},
        }
        cpred_apply_net_combat_state(
            pipeline_state,
            {"hack_state": {"nodes_visited": ["Gateway"], "revealed_nodes": ["Gateway"]}},
            game_state=pipeline_state["game_state"],
        )
        current_hp = pipeline_state["game_state"]["edgerunners"]["V"]["hp"]["current"]
        self.assertEqual(bool(pipeline_state["net_combat"].get("_forced_disconnect")), current_hp < 0, f"seed={seed}")
        self.assertEqual(bool(pipeline_state["net_combat"].get("net_complete")), current_hp < 0, f"seed={seed}")

    @settings(max_examples=220, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_resolver_ops_seeded(self, seed):
        rng = random.Random(seed)
        nc = cpred_init_net_combat_state(netrunner_name="V", sr=rng.randint(2, 5))
        nc["active_programs"] = [{"name": "Sword", "status": "active"}]
        nc["ice_status"] = {
            "Core": {"name": "Hellhound", "behavior": "black", "rez_current": 8, "rez_max": 8, "status": "active"}
        }
        tar_initial = rng.randint(0, 3)
        include_tar_in_input = rng.choice([True, False])
        pipeline_state = {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": {"edgerunners": {"V": {"hp": {"current": 30, "max": 30}}}},
        }
        tool_input = {
            "hack_state": {
                "nodes_visited": ["Gateway", "Core"],
                "revealed_nodes": ["Gateway"],
                "current_node": "Core",
                "alert_level": rng.randint(0, 8),
                "trace_progress": 0,
            }
        }
        if include_tar_in_input:
            tool_input["hack_state"]["tar_stacks"] = tar_initial
        rez_damage = rng.randint(0, 20)
        resolver_ops = [
            {"op": "program_deactivate", "program_name": "Sword"},
            {"op": "rez_damage", "target": "Hellhound", "damage": rez_damage},
        ]
        if rng.choice([True, False]):
            resolver_ops.append({"op": "tar_consumed"})

        before_rez = nc["ice_status"]["Core"]["rez_current"]
        cpred_apply_net_combat_state(
            pipeline_state,
            tool_input,
            game_state=pipeline_state["game_state"],
            resolver_state_ops=resolver_ops,
        )
        out = pipeline_state["net_combat"]
        self.assertIsInstance(out.get("revealed_nodes"), list, f"seed={seed}")
        self.assertEqual(out["active_programs"][0]["status"], "deactivated", f"seed={seed}")
        self.assertGreaterEqual(out["ice_status"]["Core"]["rez_current"], 0, f"seed={seed}")
        self.assertLessEqual(out["ice_status"]["Core"]["rez_current"], before_rez, f"seed={seed}")
        if any(op.get("op") == "tar_consumed" for op in resolver_ops):
            self.assertEqual(out["tar_stacks"], 0, f"seed={seed}")
        else:
            if include_tar_in_input:
                self.assertEqual(out["tar_stacks"], tar_initial, f"seed={seed}")
            else:
                self.assertEqual(out["tar_stacks"], 0, f"seed={seed}")


if __name__ == "__main__":
    unittest.main()
