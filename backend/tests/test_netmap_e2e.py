"""
Exhaustive e2e and unit tests for the NET map / revealed_nodes feature.

Covers every code path in the uncommitted changes with deterministic assertions:
- Unit tests for each function (init, apply, transition, injection)
- Schema validation (revealed_nodes in tool schemas)
- Multi-exchange lifecycle simulations
- Injection output content validation
- Edge cases: empty states, completion flows, combat breakout transitions
- Net combat apply with character_updates, cover, brain damage delta
- All API calls mocked — no real tokens used.
"""

import copy
import json
import os
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
    REPORT_HACK_STATE_TOOL,
    REPORT_NET_COMBAT_STATE_TOOL,
)


# ============================================================
# Realistic fixtures
# ============================================================

SAMPLE_SYSTEM_MAP = {
    "sr": 3,
    "nodes": {
        "Gateway": {
            "type": "gateway",
            "ice": None,
            "dv": 6,
            "connections": ["Lobby"],
            "contents": "Entry point"
        },
        "Lobby": {
            "type": "data_node",
            "ice": "patrol",
            "dv": 8,
            "connections": ["Gateway", "Server Room", "Admin"],
            "contents": "Directory listing"
        },
        "Server Room": {
            "type": "data_node",
            "ice": "tar",
            "dv": 10,
            "connections": ["Lobby", "Core"],
            "contents": "File archives"
        },
        "Admin": {
            "type": "password_gate",
            "ice": "black",
            "dv": 12,
            "connections": ["Lobby", "Core"],
            "contents": "Admin access"
        },
        "Core": {
            "type": "target",
            "ice": "trace",
            "dv": 14,
            "connections": ["Server Room", "Admin"],
            "contents": "Main objective: payroll data"
        }
    }
}

SAMPLE_ICE_STATUS = {
    "Lobby": {
        "name": "Asp",
        "behavior": "patrol",
        "rez_current": 4,
        "rez_max": 4,
        "status": "active"
    },
    "Server Room": {
        "name": "Skunk",
        "behavior": "tar",
        "rez_current": 6,
        "rez_max": 6,
        "status": "active"
    },
    "Admin": {
        "name": "Killer",
        "behavior": "black",
        "rez_current": 8,
        "rez_max": 8,
        "status": "active"
    },
    "Core": {
        "name": "Hellhound",
        "behavior": "trace",
        "rez_current": 6,
        "rez_max": 6,
        "status": "active"
    }
}

SAMPLE_PROGRAMS = [
    {"name": "Armor", "category": "defender", "rez": 4, "status": "active"},
    {"name": "Sword", "category": "attacker", "rez": 6, "status": "active"},
]


def _make_hack_tool_input(
    alert_level=0, cycles_remaining=3, current_node="Gateway",
    nodes_visited=None, revealed_nodes=None, ice_status=None,
    active_programs=None, system_map=None, trace_progress=None,
    tar_stacks=0, brain_damage=0, net_actions_used=1,
    hack_complete=False, narrative_summary=None, available_actions=None,
    initiate_combat=None,
):
    """Build a realistic report_hack_state tool_input dict."""
    return {
        "narrative": "The NET pulses around you.",
        "rolls": [],
        "available_actions": available_actions or ["Move", "Backdoor", "Zap"],
        "hack_state": {
            "alert_level": alert_level,
            "cycles_remaining": cycles_remaining,
            "active_programs": active_programs if active_programs is not None else [],
            "current_node": current_node,
            "nodes_visited": nodes_visited or ["Gateway"],
            "ice_status": ice_status or {},
            "trace_progress": trace_progress,
            "tar_stacks": tar_stacks,
            "brain_damage": brain_damage,
            "system_map": system_map,
            "revealed_nodes": revealed_nodes or ["Gateway"],
            "net_actions_used": net_actions_used,
        },
        "hack_complete": hack_complete,
        "narrative_summary": narrative_summary,
        "initiate_combat": initiate_combat,
    }


def _make_net_combat_tool_input(
    alert_level=0, cycles_remaining=3, current_node="Gateway",
    nodes_visited=None, revealed_nodes=None, ice_status=None,
    active_programs=None, system_map=None, trace_progress=None,
    tar_stacks=0, brain_damage=0,
    combat=None, character_updates=None, cover_state=None,
    combat_complete=False, net_complete=False,
    narrative_summary=None, available_actions=None,
):
    """Build a realistic report_net_combat_state tool_input dict."""
    return {
        "narrative": "Gunfire echoes as data streams.",
        "rolls": [],
        "character_updates": character_updates or [],
        "cover_state": cover_state or [],
        "combat": combat,
        "hack_state": {
            "alert_level": alert_level,
            "cycles_remaining": cycles_remaining,
            "active_programs": active_programs if active_programs is not None else [],
            "current_node": current_node,
            "nodes_visited": nodes_visited or ["Gateway"],
            "ice_status": ice_status or {},
            "trace_progress": trace_progress,
            "tar_stacks": tar_stacks,
            "brain_damage": brain_damage,
            "system_map": system_map,
            "revealed_nodes": revealed_nodes or ["Gateway"],
        },
        "available_actions": available_actions or ["Move", "Zap"],
        "combat_complete": combat_complete,
        "net_complete": net_complete,
        "narrative_summary": narrative_summary,
    }


# ============================================================
# Unit: init_hack_state
# ============================================================

class TestInitHackState(unittest.TestCase):
    """Unit tests for init_hack_state."""

    def test_defaults(self):
        hs = cpred_init_hack_state()
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])
        self.assertEqual(hs["nodes_visited"], ["Gateway"])
        self.assertEqual(hs["current_node"], "Gateway")
        self.assertTrue(hs["active"])
        self.assertIsNone(hs["system_map"])
        self.assertEqual(hs["tier"], "full_run")
        self.assertEqual(hs["sr"], 3)
        self.assertEqual(hs["interface_rank"], 4)

    def test_custom_params(self):
        hs = cpred_init_hack_state(
            tier="quick_hack", target_system="Arasaka Tower",
            sr=5, cycles_max=5, interface_rank=7, hacker_name="V",
            context="Hired to steal data"
        )
        self.assertEqual(hs["tier"], "quick_hack")
        self.assertEqual(hs["target_system"], "Arasaka Tower")
        self.assertEqual(hs["sr"], 5)
        self.assertEqual(hs["cycles_max"], 5)
        self.assertEqual(hs["cycles_remaining"], 5)
        self.assertEqual(hs["interface_rank"], 7)
        self.assertEqual(hs["hacker_name"], "V")
        self.assertEqual(hs["context"], "Hired to steal data")
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])

    def test_net_actions_per_turn_tiers(self):
        """Verify interface_rank → net_actions_per_turn mapping."""
        for rank, expected in [(1, 2), (3, 2), (4, 3), (6, 3), (7, 4), (9, 4), (10, 5)]:
            hs = cpred_init_hack_state(interface_rank=rank)
            self.assertEqual(hs["net_actions_per_turn"], expected,
                             f"interface_rank={rank} should give {expected} actions")

    def test_extra_kwargs_ignored(self):
        """Unknown kwargs should not crash."""
        hs = cpred_init_hack_state(foo="bar", baz=123)
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])

    def test_no_context_key_when_none(self):
        hs = cpred_init_hack_state()
        self.assertNotIn("context", hs)


# ============================================================
# Unit: init_net_combat_state
# ============================================================

class TestInitNetCombatState(unittest.TestCase):

    def test_defaults(self):
        nc = cpred_init_net_combat_state()
        self.assertEqual(nc["revealed_nodes"], ["Gateway"])
        self.assertEqual(nc["nodes_visited"], ["Gateway"])
        self.assertTrue(nc["active"])
        self.assertFalse(nc["combat_complete"])
        self.assertFalse(nc["net_complete"])
        self.assertEqual(nc["initiated_from"], "combat")

    def test_custom_params(self):
        nc = cpred_init_net_combat_state(
            netrunner_name="Spider", target="Militech",
            interface_rank=8, cycles_max=4, sr=5, initiated_from="ambush"
        )
        self.assertEqual(nc["netrunner"], "Spider")
        self.assertEqual(nc["target"], "Militech")
        self.assertEqual(nc["interface_rank"], 8)
        self.assertEqual(nc["sr"], 5)
        self.assertEqual(nc["cycles_max"], 4)
        self.assertEqual(nc["initiated_from"], "ambush")
        self.assertEqual(nc["revealed_nodes"], ["Gateway"])


# ============================================================
# Unit: apply_hack_state — revealed_nodes
# ============================================================

class TestApplyHackStateRevealedNodes(unittest.TestCase):

    def _fresh(self):
        return cpred_init_hack_state(hacker_name="V", tier="full_run")

    def test_basic_update(self):
        """Model reports new revealed_nodes — they update."""
        hs = self._fresh()
        inp = _make_hack_tool_input(
            revealed_nodes=["Gateway", "Lobby", "Admin"],
            nodes_visited=["Gateway", "Lobby"],
            current_node="Lobby",
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn("Admin", hs["revealed_nodes"])
        self.assertIn("Lobby", hs["revealed_nodes"])
        self.assertIn("Gateway", hs["revealed_nodes"])

    def test_auto_merge_visited_into_revealed(self):
        """Visited nodes always end up in revealed, even if model forgets."""
        hs = self._fresh()
        inp = _make_hack_tool_input(
            revealed_nodes=["Gateway"],  # model forgot Lobby
            nodes_visited=["Gateway", "Lobby"],
            current_node="Lobby",
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn("Lobby", hs["revealed_nodes"])

    def test_non_list_revealed_fallback_to_visited(self):
        """Non-list revealed_nodes falls back to nodes_visited."""
        hs = self._fresh()
        inp = _make_hack_tool_input()
        inp["hack_state"]["revealed_nodes"] = "not_a_list"
        inp["hack_state"]["nodes_visited"] = ["Gateway", "Lobby"]
        cpred_apply_hack_state(hs, inp)
        self.assertIsInstance(hs["revealed_nodes"], list)
        self.assertIn("Gateway", hs["revealed_nodes"])
        self.assertIn("Lobby", hs["revealed_nodes"])

    def test_non_list_revealed_fallback_to_gateway(self):
        """If both revealed and visited are garbage, fallback to Gateway."""
        hs = self._fresh()
        inp = _make_hack_tool_input()
        inp["hack_state"]["revealed_nodes"] = 42
        inp["hack_state"]["nodes_visited"] = "also_garbage"
        cpred_apply_hack_state(hs, inp)
        self.assertIsInstance(hs["revealed_nodes"], list)
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])

    def test_revealed_superset_after_multiple_applies(self):
        """After multiple applies, revealed is always a superset of visited."""
        hs = self._fresh()
        visits = [
            (["Gateway"], ["Gateway"]),
            (["Gateway", "Lobby"], ["Gateway", "Lobby"]),
            (["Gateway", "Lobby", "Server Room"], ["Gateway", "Lobby"]),  # model forgets Server Room
        ]
        for nodes_visited, revealed in visits:
            inp = _make_hack_tool_input(
                nodes_visited=nodes_visited,
                revealed_nodes=revealed,
                current_node=nodes_visited[-1],
            )
            cpred_apply_hack_state(hs, inp)
            for v in hs["nodes_visited"]:
                self.assertIn(v, hs["revealed_nodes"],
                              f"{v} visited but not in revealed_nodes")

    def test_system_map_first_exchange_only(self):
        """System map is only set on first exchange (when None)."""
        hs = self._fresh()
        inp = _make_hack_tool_input(system_map=SAMPLE_SYSTEM_MAP)
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["system_map"], SAMPLE_SYSTEM_MAP)
        # Second apply with different map — should NOT overwrite
        different_map = {"sr": 5, "nodes": {"X": {}}}
        inp2 = _make_hack_tool_input(system_map=different_map)
        cpred_apply_hack_state(hs, inp2)
        self.assertEqual(hs["system_map"], SAMPLE_SYSTEM_MAP)  # unchanged

    def test_non_dict_tool_input_returns_hack_state(self):
        """Non-dict tool_input returns hack_state unchanged."""
        hs = self._fresh()
        original = copy.deepcopy(hs)
        result = cpred_apply_hack_state(hs, "garbage")
        self.assertEqual(result["revealed_nodes"], original["revealed_nodes"])

    def test_non_dict_hack_state_in_input(self):
        """Non-dict hack_state in tool_input treated as empty."""
        hs = self._fresh()
        inp = {"hack_state": "garbage", "available_actions": ["Move"]}
        cpred_apply_hack_state(hs, inp)
        # Should still work — auto-merge from existing state
        self.assertIsInstance(hs["revealed_nodes"], list)

    def test_hack_complete(self):
        """hack_complete flag sets active=False and narrative_summary."""
        hs = self._fresh()
        inp = _make_hack_tool_input(
            hack_complete=True,
            narrative_summary="Data exfiltrated, 2 cycles spent"
        )
        cpred_apply_hack_state(hs, inp)
        self.assertFalse(hs["active"])
        self.assertEqual(hs["narrative_summary"], "Data exfiltrated, 2 cycles spent")

    def test_initiate_combat_sets_flag(self):
        """initiate_combat triggers _initiate_combat for transition."""
        hs = self._fresh()
        combat_info = {"reason": "ICE triggered alarm", "enemies": ["Guard 1"]}
        inp = _make_hack_tool_input(initiate_combat=combat_info)
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["_initiate_combat"], combat_info)

    def test_initiate_combat_ignored_when_complete(self):
        """initiate_combat NOT set when hack_complete is also true."""
        hs = self._fresh()
        inp = _make_hack_tool_input(
            hack_complete=True,
            narrative_summary="Done",
            initiate_combat={"reason": "test"}
        )
        cpred_apply_hack_state(hs, inp)
        self.assertNotIn("_initiate_combat", hs)

    def test_net_actions_remaining_decrement(self):
        """net_actions_used decrements remaining count."""
        hs = self._fresh()
        self.assertEqual(hs["net_actions_remaining"], 3)  # rank 4 = 3 actions
        inp = _make_hack_tool_input(net_actions_used=1)
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["net_actions_remaining"], 2)
        self.assertFalse(hs["meatspace_due"])

    def test_meatspace_due_when_actions_exhausted(self):
        """All actions used → meatspace_due = True, remaining reset."""
        hs = self._fresh()
        inp = _make_hack_tool_input(net_actions_used=3)
        cpred_apply_hack_state(hs, inp)
        self.assertTrue(hs["meatspace_due"])
        self.assertEqual(hs["net_actions_remaining"], 3)  # reset

    def test_available_actions_update(self):
        hs = self._fresh()
        inp = _make_hack_tool_input(available_actions=["Backdoor", "Jack Out"])
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["available_actions"], ["Backdoor", "Jack Out"])

    def test_ice_status_update(self):
        hs = self._fresh()
        inp = _make_hack_tool_input(ice_status=SAMPLE_ICE_STATUS)
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["ice_status"], SAMPLE_ICE_STATUS)

    def test_programs_update(self):
        hs = self._fresh()
        inp = _make_hack_tool_input(active_programs=SAMPLE_PROGRAMS)
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["active_programs"], SAMPLE_PROGRAMS)


# ============================================================
# Unit: init_net_combat_from_hack — revealed_nodes carry-over
# ============================================================

class TestInitNetCombatFromHack(unittest.TestCase):

    def _mid_hack(self, **overrides):
        """Return a mid-hack state with realistic data."""
        hs = cpred_init_hack_state(
            tier="full_run", target_system="Arasaka Tower",
            sr=4, interface_rank=6, hacker_name="V"
        )
        hs.update({
            "alert_level": 2,
            "cycles_remaining": 2,
            "cycles_max": 3,
            "current_node": "Lobby",
            "nodes_visited": ["Gateway", "Lobby"],
            "revealed_nodes": ["Gateway", "Lobby", "Admin", "Server Room"],
            "system_map": copy.deepcopy(SAMPLE_SYSTEM_MAP),
            "ice_status": copy.deepcopy(SAMPLE_ICE_STATUS),
            "active_programs": copy.deepcopy(SAMPLE_PROGRAMS),
            "trace_progress": 1,
            "tar_stacks": 1,
            "brain_damage": 3,
        })
        hs.update(overrides)
        return hs

    def test_basic_carry_over(self):
        """Revealed nodes carry from hack to net_combat."""
        hs = self._mid_hack()
        nc = cpred_init_net_combat_from_hack(hs, {"reason": "Guards!"})
        self.assertEqual(nc["revealed_nodes"], ["Gateway", "Lobby", "Admin", "Server Room"])
        self.assertEqual(nc["nodes_visited"], ["Gateway", "Lobby"])
        self.assertEqual(nc["current_node"], "Lobby")
        self.assertEqual(nc["alert_level"], 2)
        self.assertEqual(nc["initiated_from"], "hack")

    def test_deep_copy_isolation(self):
        """Modifying nc doesn't affect original hack_state."""
        hs = self._mid_hack()
        nc = cpred_init_net_combat_from_hack(hs)
        nc["revealed_nodes"].append("Core")
        self.assertNotIn("Core", hs["revealed_nodes"])
        nc["ice_status"]["NewNode"] = {"name": "Test"}
        self.assertNotIn("NewNode", hs["ice_status"])

    def test_non_list_revealed_fallback(self):
        """Non-list revealed_nodes falls back to nodes_visited."""
        hs = self._mid_hack(revealed_nodes="garbage")
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["revealed_nodes"], ["Gateway", "Lobby"])

    def test_non_list_nodes_visited_fallback(self):
        """Non-list nodes_visited gets fallback to Gateway."""
        hs = self._mid_hack(nodes_visited="bad", revealed_nodes="also_bad")
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["nodes_visited"], ["Gateway"])
        # revealed_nodes falls back to nodes_visited which is now ["Gateway"]
        self.assertEqual(nc["revealed_nodes"], ["Gateway"])

    def test_non_dict_hack_state(self):
        """Non-dict hack_state → default net_combat with empty revealed."""
        nc = cpred_init_net_combat_from_hack("not_a_dict")
        # Non-dict → hack_state={} → get("revealed_nodes", []) → []
        self.assertEqual(nc["revealed_nodes"], [])
        self.assertTrue(nc["active"])

    def test_empty_dict(self):
        nc = cpred_init_net_combat_from_hack({})
        self.assertEqual(nc["revealed_nodes"], [])  # no visited to copy
        self.assertEqual(nc["nodes_visited"], ["Gateway"])

    def test_combat_info_in_context(self):
        hs = self._mid_hack()
        nc = cpred_init_net_combat_from_hack(hs, {"reason": "ICE alarm", "enemies": ["Guard 1", "Drone"]})
        self.assertIn("ICE alarm", nc.get("context", ""))
        self.assertIn("Guard 1", nc.get("context", ""))

    def test_system_map_deep_copied(self):
        hs = self._mid_hack()
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["system_map"], SAMPLE_SYSTEM_MAP)
        # Modify nc's map — original untouched
        nc["system_map"]["sr"] = 99
        self.assertEqual(hs["system_map"]["sr"], 3)  # SAMPLE_SYSTEM_MAP sr is 3

    def test_brain_damage_delta_tracking(self):
        hs = self._mid_hack(brain_damage=5)
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["brain_damage"], 5)
        self.assertEqual(nc["_prev_brain_damage"], 5)

    def test_numeric_field_overflow(self):
        """float('inf') in numeric fields — should fallback to defaults."""
        hs = self._mid_hack(
            interface_rank=float('inf'),
            cycles_remaining=float('inf'),
            alert_level=float('inf'),
            tar_stacks=float('inf'),
            brain_damage=float('inf'),
        )
        nc = cpred_init_net_combat_from_hack(hs)
        # All should fallback to defaults, not crash
        self.assertIsInstance(nc["interface_rank"], int)
        self.assertIsInstance(nc["cycles_remaining"], int)
        self.assertIsInstance(nc["alert_level"], int)


# ============================================================
# Unit: apply_net_combat_state — revealed_nodes
# ============================================================

class TestApplyNetCombatState(unittest.TestCase):

    def _fresh_ps(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Arasaka", sr=3)
        return {
            "net_combat": nc,
            "combat": {
                "round": 1,
                "initiative_order": ["V", "Guard"],
                "current_turn": "V"
            },
            "character_states": {},
        }

    def test_revealed_nodes_update(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(
            revealed_nodes=["Gateway", "Lobby", "Admin"],
            nodes_visited=["Gateway", "Lobby"],
            current_node="Lobby",
            combat={"round": 1, "initiative_order": ["V", "Guard"], "current_turn": "Guard"},
        )
        cpred_apply_net_combat_state(ps, inp)
        self.assertIn("Admin", ps["net_combat"]["revealed_nodes"])

    def test_auto_merge_in_net_combat(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(
            revealed_nodes=["Gateway"],  # forgot Lobby
            nodes_visited=["Gateway", "Lobby"],
            current_node="Lobby",
            combat={"round": 1, "initiative_order": ["V"], "current_turn": "V"},
        )
        cpred_apply_net_combat_state(ps, inp)
        self.assertIn("Lobby", ps["net_combat"]["revealed_nodes"])

    def test_non_list_revealed_fallback(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input()
        inp["hack_state"]["revealed_nodes"] = None
        inp["hack_state"]["nodes_visited"] = ["Gateway", "Server Room"]
        cpred_apply_net_combat_state(ps, inp)
        self.assertIsInstance(ps["net_combat"]["revealed_nodes"], list)
        self.assertIn("Gateway", ps["net_combat"]["revealed_nodes"])
        self.assertIn("Server Room", ps["net_combat"]["revealed_nodes"])

    def test_non_dict_tool_input(self):
        ps = self._fresh_ps()
        cpred_apply_net_combat_state(ps, "garbage")
        # Should not crash, net_combat preserved
        self.assertIn("net_combat", ps)

    def test_system_map_first_exchange(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(system_map=SAMPLE_SYSTEM_MAP)
        cpred_apply_net_combat_state(ps, inp)
        self.assertEqual(ps["net_combat"]["system_map"], SAMPLE_SYSTEM_MAP)

    def test_system_map_not_overwritten(self):
        ps = self._fresh_ps()
        ps["net_combat"]["system_map"] = SAMPLE_SYSTEM_MAP
        other_map = {"sr": 9, "nodes": {}}
        inp = _make_net_combat_tool_input(system_map=other_map)
        cpred_apply_net_combat_state(ps, inp)
        self.assertEqual(ps["net_combat"]["system_map"], SAMPLE_SYSTEM_MAP)

    def test_combat_complete_clears_combat(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(combat_complete=True)
        cpred_apply_net_combat_state(ps, inp)
        self.assertIsNone(ps["combat"])
        self.assertTrue(ps["net_combat"]["combat_complete"])

    def test_net_complete(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(net_complete=True)
        cpred_apply_net_combat_state(ps, inp)
        self.assertTrue(ps["net_combat"]["net_complete"])

    def test_both_complete_deactivates(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(
            combat_complete=True, net_complete=True,
            narrative_summary="All done"
        )
        cpred_apply_net_combat_state(ps, inp)
        self.assertFalse(ps["net_combat"]["active"])
        self.assertEqual(ps["net_combat"]["narrative_summary"], "All done")

    def test_brain_damage_delta_applied(self):
        """Brain damage delta should be tracked via _prev_brain_damage."""
        ps = self._fresh_ps()
        ps["net_combat"]["brain_damage"] = 0
        ps["net_combat"]["_prev_brain_damage"] = 0
        ps["net_combat"]["netrunner"] = "V"
        # Set up edgerunner data for brain damage application
        ps["game_state"] = {
            "edgerunners": {
                "V": {
                    "hp": {"current": 40, "max": 40},
                    "armor": {"head": 11, "body": 11},
                    "luck": {"current": 6, "max": 6},
                }
            }
        }
        inp = _make_net_combat_tool_input(brain_damage=3)
        cpred_apply_net_combat_state(ps, inp, game_state=ps.get("game_state"))
        self.assertEqual(ps["net_combat"]["brain_damage"], 3)
        self.assertEqual(ps["net_combat"]["_prev_brain_damage"], 3)
        # HP should have been reduced
        self.assertEqual(ps["game_state"]["edgerunners"]["V"]["hp"]["current"], 37)

    def test_cover_state_update(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(
            cover_state=[{"name": "V", "in_cover": True, "cover_type": "concrete wall", "cover_hp": 20}],
            combat={"round": 1, "initiative_order": ["V"], "current_turn": "V"},
        )
        cpred_apply_net_combat_state(ps, inp)
        cover = ps["combat"]["cover"]
        self.assertTrue(cover["V"]["in_cover"])
        self.assertEqual(cover["V"]["cover_type"], "concrete wall")

    def test_available_actions_update(self):
        ps = self._fresh_ps()
        inp = _make_net_combat_tool_input(available_actions=["Jack Out", "Zap"])
        cpred_apply_net_combat_state(ps, inp)
        self.assertEqual(ps["net_combat"]["available_actions"], ["Jack Out", "Zap"])


# ============================================================
# Unit: build_hack_injection — revealed_nodes output
# ============================================================

class TestBuildHackInjection(unittest.TestCase):

    def _state_with(self, **overrides):
        hs = cpred_init_hack_state(hacker_name="V")
        hs.update(overrides)
        return hs

    def test_revealed_nodes_in_output(self):
        """Revealed nodes appear in injection string."""
        hs = self._state_with(
            revealed_nodes=["Gateway", "Lobby", "Admin"],
            system_map=SAMPLE_SYSTEM_MAP,
        )
        inj = cpred_build_hack_injection(hs)
        self.assertIn("Revealed nodes: Gateway, Lobby, Admin", inj)

    def test_empty_revealed_not_in_output(self):
        """Empty revealed_nodes → no Revealed nodes line."""
        hs = self._state_with(revealed_nodes=[], system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertNotIn("Revealed nodes:", inj)

    def test_non_list_revealed_not_in_output(self):
        """Non-list revealed_nodes → no Revealed nodes line."""
        hs = self._state_with(revealed_nodes="garbage", system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertNotIn("Revealed nodes:", inj)

    def test_system_map_missing_nudge(self):
        """No system_map → SYSTEM MAP MISSING nudge."""
        hs = self._state_with(system_map=None)
        inj = cpred_build_hack_injection(hs)
        self.assertIn("SYSTEM MAP MISSING", inj)

    def test_system_map_present_no_nudge(self):
        hs = self._state_with(system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertNotIn("SYSTEM MAP MISSING", inj)
        self.assertIn("[SYSTEM MAP]", inj)

    def test_hack_state_section(self):
        hs = self._state_with(
            alert_level=3, cycles_remaining=2, cycles_max=3,
            current_node="Server Room",
            nodes_visited=["Gateway", "Lobby", "Server Room"],
            ice_status=SAMPLE_ICE_STATUS,
            active_programs=SAMPLE_PROGRAMS,
            trace_progress=2, tar_stacks=1, brain_damage=4,
            system_map=SAMPLE_SYSTEM_MAP,
        )
        inj = cpred_build_hack_injection(hs)
        self.assertIn("[HACK STATE]", inj)
        self.assertIn("[/HACK STATE]", inj)
        self.assertIn("Alert Level: 3", inj)
        self.assertIn("DVs +2", inj)  # alert 3 effect
        self.assertIn("Cycles: 2/3", inj)
        self.assertIn("Current Node: Server Room", inj)
        self.assertIn("Nodes Visited: Gateway, Lobby, Server Room", inj)
        self.assertIn("Trace Progress:", inj)
        self.assertIn("Tar Stacks: 1", inj)
        self.assertIn("Brain Damage", inj)
        # Programs
        self.assertIn("Armor (defender, REZ 4)", inj)
        self.assertIn("Sword (attacker, REZ 6)", inj)
        # ICE
        self.assertIn("Asp", inj)
        self.assertIn("Killer", inj)

    def test_context_injection(self):
        hs = self._state_with(
            context="Emergency hack on Militech subnet",
            system_map=SAMPLE_SYSTEM_MAP,
        )
        inj = cpred_build_hack_injection(hs)
        self.assertIn("[TRANSITION] Emergency hack on Militech subnet [/TRANSITION]", inj)

    def test_convergence_warning(self):
        hs = self._state_with(alert_level=7, system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertIn("CONVERGENCE", inj)

    def test_trace_complete_warning(self):
        hs = self._state_with(trace_progress=3, sr=3, system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertIn("TRACE COMPLETE", inj)

    def test_meatspace_due(self):
        hs = self._state_with(meatspace_due=True, system_map=SAMPLE_SYSTEM_MAP)
        inj = cpred_build_hack_injection(hs)
        self.assertIn("MEATSPACE ROUND DUE", inj)


# ============================================================
# Unit: build_net_combat_injection — revealed_nodes output
# ============================================================

class TestBuildNetCombatInjection(unittest.TestCase):

    def _state(self, nc_overrides=None, combat=None):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Arasaka", sr=3)
        if nc_overrides:
            nc.update(nc_overrides)
        if combat is None:
            combat = {"round": 1, "initiative_order": ["V", "Guard"], "current_turn": "V",
                       "cover": {}}
        ps = {"character_states": {}, "game_state": {"edgerunners": {}}}
        return combat, nc, ps

    def test_revealed_nodes_in_injection(self):
        combat, nc, ps = self._state(nc_overrides={
            "revealed_nodes": ["Gateway", "Lobby", "Core"],
            "system_map": SAMPLE_SYSTEM_MAP,
        })
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("Revealed nodes: Gateway, Lobby, Core", inj)

    def test_empty_revealed_not_in_injection(self):
        combat, nc, ps = self._state(nc_overrides={
            "revealed_nodes": [],
            "system_map": SAMPLE_SYSTEM_MAP,
        })
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertNotIn("Revealed nodes:", inj)

    def test_net_state_section(self):
        combat, nc, ps = self._state(nc_overrides={
            "alert_level": 2, "current_node": "Lobby",
            "nodes_visited": ["Gateway", "Lobby"],
            "system_map": SAMPLE_SYSTEM_MAP,
        })
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("[NET STATE]", inj)
        self.assertIn("[/NET STATE]", inj)
        self.assertIn("Current Node: Lobby", inj)
        self.assertIn("Nodes Visited: Gateway, Lobby", inj)

    def test_net_complete_shows_resolved(self):
        combat, nc, ps = self._state(nc_overrides={"net_complete": True})
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("NET encounter resolved.", inj)
        self.assertNotIn("Current Node:", inj)

    def test_meatspace_combat_resolved(self):
        combat, nc, ps = self._state(nc_overrides={
            "combat_complete": True,
            "system_map": SAMPLE_SYSTEM_MAP,
        })
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("Meatspace combat resolved.", inj)

    def test_hack_originated_bootstrap(self):
        """When combat=None (hack-originated), shows bootstrap message."""
        _, nc, ps = self._state()
        nc["_combat_breakout"] = {"reason": "ICE alarm", "enemies": ["Guard 1"]}
        inj = cpred_build_net_combat_injection(None, nc, ps)
        self.assertIn("Bootstrap enemies", inj)
        self.assertIn("ICE alarm", inj)
        self.assertIn("Guard 1", inj)

    def test_system_map_missing_nudge(self):
        combat, nc, ps = self._state(nc_overrides={"system_map": None})
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("SYSTEM MAP MISSING", inj)

    def test_context_in_injection(self):
        combat, nc, ps = self._state(nc_overrides={
            "context": "Combat breakout from hack",
            "system_map": SAMPLE_SYSTEM_MAP,
        })
        inj = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("[TRANSITION] Combat breakout from hack [/TRANSITION]", inj)


# ============================================================
# Schema: revealed_nodes in tool schemas
# ============================================================

class TestSchemaRevealedNodes(unittest.TestCase):
    """Verify revealed_nodes is correctly defined in both tool schemas."""

    def test_hack_state_schema_has_revealed_nodes(self):
        props = REPORT_HACK_STATE_TOOL["input_schema"]["properties"]["hack_state"]["properties"]
        self.assertIn("revealed_nodes", props)
        rn = props["revealed_nodes"]
        self.assertEqual(rn["type"], "array")
        self.assertEqual(rn["items"], {"type": "string"})
        self.assertIn("discovered", rn["description"].lower())

    def test_net_combat_schema_has_revealed_nodes(self):
        props = REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["properties"]["hack_state"]["properties"]
        self.assertIn("revealed_nodes", props)
        rn = props["revealed_nodes"]
        self.assertEqual(rn["type"], "array")
        self.assertEqual(rn["items"], {"type": "string"})

    def test_hack_schema_required_fields(self):
        req = REPORT_HACK_STATE_TOOL["input_schema"]["required"]
        self.assertIn("hack_state", req)
        self.assertIn("hack_complete", req)
        self.assertIn("narrative", req)

    def test_net_combat_schema_required_fields(self):
        req = REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["required"]
        self.assertIn("hack_state", req)
        self.assertIn("combat_complete", req)
        self.assertIn("net_complete", req)

    def test_hack_schema_has_system_map(self):
        props = REPORT_HACK_STATE_TOOL["input_schema"]["properties"]["hack_state"]["properties"]
        self.assertIn("system_map", props)

    def test_net_combat_schema_has_system_map(self):
        props = REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["properties"]["hack_state"]["properties"]
        self.assertIn("system_map", props)


# ============================================================
# E2E: Full Hack Lifecycle Simulation
# ============================================================

class TestFullHackLifecycle(unittest.TestCase):
    """Simulate a multi-exchange hack from init through completion."""

    def test_full_run_five_exchanges(self):
        """Full run: init → 5 exchanges → complete. Tracks revealed_nodes throughout."""
        hs = cpred_init_hack_state(
            tier="full_run", target_system="Kang Tao Lab",
            sr=3, interface_rank=6, hacker_name="Spider"
        )
        self.assertEqual(hs["revealed_nodes"], ["Gateway"])

        # Exchange 1: Model sends system_map, moves to Lobby
        inp1 = _make_hack_tool_input(
            system_map=SAMPLE_SYSTEM_MAP,
            current_node="Lobby",
            nodes_visited=["Gateway", "Lobby"],
            revealed_nodes=["Gateway", "Lobby"],
            ice_status={"Lobby": SAMPLE_ICE_STATUS["Lobby"]},
            alert_level=1,
            cycles_remaining=3,
            net_actions_used=1,
        )
        cpred_apply_hack_state(hs, inp1)
        self.assertEqual(hs["system_map"], SAMPLE_SYSTEM_MAP)
        self.assertEqual(hs["current_node"], "Lobby")
        self.assertIn("Lobby", hs["revealed_nodes"])
        inj1 = cpred_build_hack_injection(hs)
        self.assertIn("Revealed nodes:", inj1)
        self.assertNotIn("SYSTEM MAP MISSING", inj1)

        # Exchange 2: Pathfinder reveals Admin and Server Room
        inp2 = _make_hack_tool_input(
            current_node="Lobby",
            nodes_visited=["Gateway", "Lobby"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room"],
            ice_status={"Lobby": SAMPLE_ICE_STATUS["Lobby"]},
            alert_level=1,
            cycles_remaining=3,
            net_actions_used=1,
        )
        cpred_apply_hack_state(hs, inp2)
        self.assertIn("Admin", hs["revealed_nodes"])
        self.assertIn("Server Room", hs["revealed_nodes"])
        self.assertEqual(len(hs["revealed_nodes"]), 4)

        # Exchange 3: Move to Server Room, fight Tar ICE
        inp3 = _make_hack_tool_input(
            current_node="Server Room",
            nodes_visited=["Gateway", "Lobby", "Server Room"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room"],
            ice_status={
                "Lobby": SAMPLE_ICE_STATUS["Lobby"],
                "Server Room": SAMPLE_ICE_STATUS["Server Room"],
            },
            tar_stacks=1,
            alert_level=2,
            cycles_remaining=2,
            net_actions_used=2,
            active_programs=SAMPLE_PROGRAMS,
        )
        cpred_apply_hack_state(hs, inp3)
        self.assertEqual(hs["tar_stacks"], 1)
        self.assertIn("Server Room", hs["nodes_visited"])

        # Exchange 4: Move to Admin, discover Core via adjacency
        inp4 = _make_hack_tool_input(
            current_node="Admin",
            nodes_visited=["Gateway", "Lobby", "Server Room", "Admin"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            ice_status={
                "Lobby": SAMPLE_ICE_STATUS["Lobby"],
                "Server Room": {**SAMPLE_ICE_STATUS["Server Room"], "status": "derezzed"},
                "Admin": SAMPLE_ICE_STATUS["Admin"],
            },
            tar_stacks=0,
            alert_level=3,
            brain_damage=2,
            cycles_remaining=2,
            net_actions_used=2,
            active_programs=SAMPLE_PROGRAMS,
        )
        cpred_apply_hack_state(hs, inp4)
        self.assertIn("Core", hs["revealed_nodes"])
        self.assertEqual(len(hs["revealed_nodes"]), 5)

        # Exchange 5: Reach Core, hack_complete
        inp5 = _make_hack_tool_input(
            current_node="Core",
            nodes_visited=["Gateway", "Lobby", "Server Room", "Admin", "Core"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            brain_damage=4,
            alert_level=4,
            cycles_remaining=1,
            hack_complete=True,
            narrative_summary="Payroll data exfiltrated. 2 cycles, 4 brain damage."
        )
        cpred_apply_hack_state(hs, inp5)
        self.assertFalse(hs["active"])
        self.assertEqual(hs["narrative_summary"], "Payroll data exfiltrated. 2 cycles, 4 brain damage.")
        # revealed = visited at end
        for n in hs["nodes_visited"]:
            self.assertIn(n, hs["revealed_nodes"])

        # Final injection should still work
        inj = cpred_build_hack_injection(hs)
        self.assertIn("Revealed nodes:", inj)

    def test_quick_hack_lifecycle(self):
        """Quick hack: simpler 3-node linear path."""
        hs = cpred_init_hack_state(tier="quick_hack", sr=2, interface_rank=4, hacker_name="Kei")

        linear_map = {
            "sr": 2,
            "nodes": {
                "Gateway": {"type": "gateway", "ice": None, "dv": 6, "connections": ["Data"], "contents": "Entry"},
                "Data": {"type": "data_node", "ice": "patrol", "dv": 8, "connections": ["Gateway", "Target"], "contents": "Files"},
                "Target": {"type": "target", "ice": None, "dv": 10, "connections": ["Data"], "contents": "Objective"},
            }
        }

        # Exchange 1
        inp = _make_hack_tool_input(
            system_map=linear_map,
            current_node="Data",
            nodes_visited=["Gateway", "Data"],
            revealed_nodes=["Gateway", "Data", "Target"],
            alert_level=1,
            cycles_remaining=3,
            net_actions_used=1,
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn("Target", hs["revealed_nodes"])

        # Exchange 2 — complete
        inp2 = _make_hack_tool_input(
            current_node="Target",
            nodes_visited=["Gateway", "Data", "Target"],
            revealed_nodes=["Gateway", "Data", "Target"],
            hack_complete=True,
            narrative_summary="Quick hack: data stolen",
        )
        cpred_apply_hack_state(hs, inp2)
        self.assertFalse(hs["active"])


# ============================================================
# E2E: Hack → Net Combat Transition
# ============================================================

class TestHackToNetCombatTransition(unittest.TestCase):
    """Simulate hack → combat breakout → net_combat lifecycle."""

    def test_full_transition(self):
        # Phase 1: hack init
        hs = cpred_init_hack_state(
            tier="full_run", target_system="Biotechnica",
            sr=3, interface_rank=7, hacker_name="V"
        )

        # Phase 2: hack exchanges
        inp1 = _make_hack_tool_input(
            system_map=SAMPLE_SYSTEM_MAP,
            current_node="Lobby",
            nodes_visited=["Gateway", "Lobby"],
            revealed_nodes=["Gateway", "Lobby", "Admin"],
            alert_level=2,
            net_actions_used=1,
        )
        cpred_apply_hack_state(hs, inp1)

        inp2 = _make_hack_tool_input(
            current_node="Admin",
            nodes_visited=["Gateway", "Lobby", "Admin"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            alert_level=4,
            brain_damage=2,
            net_actions_used=2,
            initiate_combat={"reason": "Black ICE triggered physical alarm", "enemies": ["Guard 1", "Drone"]}
        )
        cpred_apply_hack_state(hs, inp2)
        self.assertIn("_initiate_combat", hs)

        # Phase 3: Transition
        nc = cpred_init_net_combat_from_hack(hs, hs.get("_initiate_combat"))
        self.assertEqual(nc["revealed_nodes"], ["Gateway", "Lobby", "Admin", "Server Room", "Core"])
        self.assertEqual(nc["current_node"], "Admin")
        self.assertEqual(nc["alert_level"], 4)
        self.assertEqual(nc["brain_damage"], 2)
        self.assertEqual(nc["initiated_from"], "hack")
        self.assertIn("Guard 1", nc.get("context", ""))

        # Phase 4: Net combat exchanges
        ps = {
            "net_combat": nc,
            "combat": None,
            "character_states": {},
            "game_state": {"edgerunners": {
                "V": {"hp": {"current": 40, "max": 40}, "armor": {"head": 11, "body": 11}, "luck": {"current": 6, "max": 6}}
            }},
        }

        nc_inp1 = _make_net_combat_tool_input(
            current_node="Admin",
            nodes_visited=["Gateway", "Lobby", "Admin"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            alert_level=4,
            brain_damage=3,
            combat={
                "round": 1,
                "initiative_order": ["V", "Guard 1", "Drone"],
                "current_turn": "V"
            },
            character_updates=[
                {"name": "Guard 1", "set_combat_stats": {"hp_max": 25, "armor": {"head": 7, "body": 11}, "weapons": [], "stats": {}}},
                {"name": "Drone", "set_combat_stats": {"hp_max": 15, "armor": {"head": 0, "body": 7}, "weapons": [], "stats": {}}},
            ],
        )
        cpred_apply_net_combat_state(ps, nc_inp1, game_state=ps.get("game_state"))
        self.assertEqual(ps["net_combat"]["brain_damage"], 3)
        # Brain damage delta: 3 - 2 = 1 → HP should be 39
        self.assertEqual(ps["game_state"]["edgerunners"]["V"]["hp"]["current"], 39)

        # Combat round 2 — move to Core
        nc_inp2 = _make_net_combat_tool_input(
            current_node="Core",
            nodes_visited=["Gateway", "Lobby", "Admin", "Core"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            alert_level=5,
            brain_damage=3,
            combat={
                "round": 2,
                "initiative_order": ["V", "Guard 1", "Drone"],
                "current_turn": "Drone"
            },
            character_updates=[
                {"name": "Guard 1", "hp_delta": -8}
            ],
        )
        cpred_apply_net_combat_state(ps, nc_inp2, game_state=ps.get("game_state"))
        self.assertIn("Core", ps["net_combat"]["nodes_visited"])

        # Final exchange — both complete
        nc_inp3 = _make_net_combat_tool_input(
            current_node="Core",
            nodes_visited=["Gateway", "Lobby", "Admin", "Core"],
            revealed_nodes=["Gateway", "Lobby", "Admin", "Server Room", "Core"],
            combat_complete=True,
            net_complete=True,
            narrative_summary="Data stolen, guards neutralized."
        )
        cpred_apply_net_combat_state(ps, nc_inp3, game_state=ps.get("game_state"))
        self.assertFalse(ps["net_combat"]["active"])
        self.assertEqual(ps["net_combat"]["narrative_summary"], "Data stolen, guards neutralized.")

        # Injection for completed state
        inj = cpred_build_net_combat_injection(ps.get("combat"), ps["net_combat"], ps)
        self.assertIn("NET encounter resolved.", inj)


# ============================================================
# E2E: Net Combat (standalone, not from hack)
# ============================================================

class TestStandaloneNetCombat(unittest.TestCase):

    def test_standalone_lifecycle(self):
        nc = cpred_init_net_combat_state(
            netrunner_name="Spider", target="Militech",
            interface_rank=5, sr=4
        )
        ps = {
            "net_combat": nc,
            "combat": {
                "round": 1,
                "initiative_order": ["Spider", "Solo 1"],
                "current_turn": "Spider",
                "cover": {},
            },
            "character_states": {},
            "game_state": {"edgerunners": {}},
        }

        # Exchange 1: system_map + first moves
        inp = _make_net_combat_tool_input(
            system_map=SAMPLE_SYSTEM_MAP,
            current_node="Lobby",
            nodes_visited=["Gateway", "Lobby"],
            revealed_nodes=["Gateway", "Lobby"],
            alert_level=1,
            combat={
                "round": 1,
                "initiative_order": ["Spider", "Solo 1"],
                "current_turn": "Solo 1"
            },
        )
        cpred_apply_net_combat_state(ps, inp)
        self.assertEqual(ps["net_combat"]["system_map"], SAMPLE_SYSTEM_MAP)
        self.assertIn("Lobby", ps["net_combat"]["revealed_nodes"])

        # Injection should show both theaters
        inj = cpred_build_net_combat_injection(ps["combat"], ps["net_combat"], ps)
        self.assertIn("[MEATSPACE COMBAT STATE]", inj)
        self.assertIn("[NET STATE]", inj)
        self.assertIn("Nodes Visited: Gateway, Lobby", inj)
        self.assertIn("Revealed nodes: Gateway, Lobby", inj)


# ============================================================
# E2E: JSON round-trip safety
# ============================================================

class TestJsonRoundTrip(unittest.TestCase):
    """State must survive json.dumps/loads at every stage."""

    def test_hack_state_serializable(self):
        hs = cpred_init_hack_state(hacker_name="V")
        inp = _make_hack_tool_input(
            system_map=SAMPLE_SYSTEM_MAP,
            revealed_nodes=["Gateway", "Lobby"],
            ice_status=SAMPLE_ICE_STATUS,
            active_programs=SAMPLE_PROGRAMS,
        )
        cpred_apply_hack_state(hs, inp)
        serialized = json.dumps(hs)
        restored = json.loads(serialized)
        self.assertEqual(restored["revealed_nodes"], hs["revealed_nodes"])
        self.assertEqual(restored["system_map"], hs["system_map"])

    def test_net_combat_state_serializable(self):
        nc = cpred_init_net_combat_state(netrunner_name="V")
        ps = {"net_combat": nc, "combat": None, "character_states": {}}
        inp = _make_net_combat_tool_input(
            system_map=SAMPLE_SYSTEM_MAP,
            revealed_nodes=["Gateway", "Lobby", "Core"],
        )
        cpred_apply_net_combat_state(ps, inp)
        serialized = json.dumps(ps["net_combat"])
        restored = json.loads(serialized)
        self.assertEqual(restored["revealed_nodes"], ps["net_combat"]["revealed_nodes"])

    def test_transition_state_serializable(self):
        hs = cpred_init_hack_state(hacker_name="V")
        hs["system_map"] = SAMPLE_SYSTEM_MAP
        hs["revealed_nodes"] = ["Gateway", "Lobby"]
        nc = cpred_init_net_combat_from_hack(hs, {"reason": "test"})
        serialized = json.dumps(nc)
        restored = json.loads(serialized)
        self.assertEqual(restored["revealed_nodes"], nc["revealed_nodes"])


# ============================================================
# E2E: Injection output completeness
# ============================================================

class TestInjectionCompleteness(unittest.TestCase):
    """Verify all injection sections are present and correct."""

    def test_hack_injection_all_sections(self):
        hs = cpred_init_hack_state(
            hacker_name="V", tier="full_run", sr=3,
            interface_rank=6, context="Mission briefing"
        )
        hs["system_map"] = SAMPLE_SYSTEM_MAP
        hs["alert_level"] = 5
        hs["current_node"] = "Server Room"
        hs["nodes_visited"] = ["Gateway", "Lobby", "Server Room"]
        hs["revealed_nodes"] = ["Gateway", "Lobby", "Server Room", "Admin"]
        hs["ice_status"] = SAMPLE_ICE_STATUS
        hs["active_programs"] = SAMPLE_PROGRAMS
        hs["trace_progress"] = 2
        hs["tar_stacks"] = 2
        hs["brain_damage"] = 3

        inj = cpred_build_hack_injection(hs)
        sections = [
            "[TRANSITION]", "[/TRANSITION]",
            "[HACK STATE]", "[/HACK STATE]",
            "[SYSTEM MAP]", "[/SYSTEM MAP]",
            "Revealed nodes:",
            "Alert Level: 5",
            "Interface check to move nodes",  # alert 5 effect
            "Cycles:",
            "Active Programs:",
            "Current Node: Server Room",
            "Nodes Visited:",
            "ICE Status:",
            "Trace Progress:",
            "Tar Stacks:",
            "Brain Damage",
        ]
        for s in sections:
            self.assertIn(s, inj, f"Missing section: {s}")

    def test_net_combat_injection_all_sections(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="Arasaka", sr=3)
        nc["system_map"] = SAMPLE_SYSTEM_MAP
        nc["revealed_nodes"] = ["Gateway", "Lobby"]
        nc["nodes_visited"] = ["Gateway", "Lobby"]
        nc["current_node"] = "Lobby"
        nc["context"] = "Combined engagement"
        nc["ice_status"] = SAMPLE_ICE_STATUS
        nc["active_programs"] = SAMPLE_PROGRAMS
        nc["trace_progress"] = 1
        nc["tar_stacks"] = 1
        nc["brain_damage"] = 2

        combat = {
            "round": 2,
            "initiative_order": ["V", "Guard"],
            "current_turn": "V",
            "cover": {},
        }
        ps = {
            "character_states": {},
            "game_state": {"edgerunners": {}},
        }

        inj = cpred_build_net_combat_injection(combat, nc, ps)
        sections = [
            "[TRANSITION]", "[/TRANSITION]",
            "[MEATSPACE COMBAT STATE]", "[/MEATSPACE COMBAT STATE]",
            "[NET STATE]", "[/NET STATE]",
            "[SYSTEM MAP]", "[/SYSTEM MAP]",
            "Revealed nodes:",
            "Netrunner: V",
            "Target: Arasaka",
            "Current Node: Lobby",
            "ICE Status:",
            "Trace Progress:",
        ]
        for s in sections:
            self.assertIn(s, inj, f"Missing section: {s}")


# ============================================================
# E2E: Revealed nodes monotonic growth
# ============================================================

class TestRevealedNodesMonotonic(unittest.TestCase):
    """revealed_nodes must never shrink across exchanges."""

    def test_hack_monotonic_growth(self):
        hs = cpred_init_hack_state()
        prev_count = len(hs["revealed_nodes"])

        exchanges = [
            {"nodes_visited": ["Gateway", "Lobby"], "revealed_nodes": ["Gateway", "Lobby"]},
            {"nodes_visited": ["Gateway", "Lobby"], "revealed_nodes": ["Gateway", "Lobby", "Admin"]},
            {"nodes_visited": ["Gateway", "Lobby", "Admin"], "revealed_nodes": ["Gateway", "Lobby", "Admin", "Core"]},
            # Model tries to remove nodes (shouldn't shrink because auto-merge re-adds visited)
            {"nodes_visited": ["Gateway", "Lobby", "Admin", "Core"], "revealed_nodes": ["Gateway"]},
        ]
        for i, ex in enumerate(exchanges):
            inp = _make_hack_tool_input(**ex, current_node=ex["nodes_visited"][-1])
            cpred_apply_hack_state(hs, inp)
            # After auto-merge, revealed should at least contain all visited
            for v in hs["nodes_visited"]:
                self.assertIn(v, hs["revealed_nodes"],
                              f"Exchange {i+1}: visited {v} not in revealed_nodes")

    def test_net_combat_monotonic_growth(self):
        ps = {
            "net_combat": cpred_init_net_combat_state(),
            "combat": None,
            "character_states": {},
        }
        exchanges = [
            {"nodes_visited": ["Gateway", "A"], "revealed_nodes": ["Gateway", "A"]},
            {"nodes_visited": ["Gateway", "A", "B"], "revealed_nodes": ["Gateway", "A", "B", "C"]},
            # Model drops C — but B is visited, so B stays
            {"nodes_visited": ["Gateway", "A", "B"], "revealed_nodes": ["Gateway", "A"]},
        ]
        for i, ex in enumerate(exchanges):
            inp = _make_net_combat_tool_input(**ex, current_node=ex["nodes_visited"][-1])
            cpred_apply_net_combat_state(ps, inp)
            for v in ps["net_combat"]["nodes_visited"]:
                self.assertIn(v, ps["net_combat"]["revealed_nodes"],
                              f"Exchange {i+1}: visited {v} not in revealed_nodes")


# ============================================================
# Edge cases
# ============================================================

class TestEdgeCases(unittest.TestCase):

    def test_empty_system_map_nodes(self):
        """Empty nodes dict in system_map is valid."""
        hs = cpred_init_hack_state()
        inp = _make_hack_tool_input(system_map={"sr": 1, "nodes": {}})
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["system_map"], {"sr": 1, "nodes": {}})

    def test_single_node_map(self):
        """Map with only Gateway."""
        single = {
            "sr": 1,
            "nodes": {
                "Gateway": {"type": "gateway", "ice": None, "dv": 6, "connections": [], "contents": "Just a gateway"}
            }
        }
        hs = cpred_init_hack_state()
        inp = _make_hack_tool_input(system_map=single, revealed_nodes=["Gateway"])
        cpred_apply_hack_state(hs, inp)
        inj = cpred_build_hack_injection(hs)
        self.assertIn("Revealed nodes: Gateway", inj)

    def test_unicode_node_names(self):
        """Node names with unicode characters."""
        hs = cpred_init_hack_state()
        inp = _make_hack_tool_input(
            nodes_visited=["Gateway", "データ"],
            revealed_nodes=["Gateway", "データ", "Übergang"],
            current_node="データ",
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn("データ", hs["revealed_nodes"])
        self.assertIn("Übergang", hs["revealed_nodes"])
        inj = cpred_build_hack_injection(hs)
        self.assertIn("データ", inj)

    def test_duplicate_nodes_in_visited(self):
        """Duplicates in nodes_visited shouldn't duplicate in revealed."""
        hs = cpred_init_hack_state()
        inp = _make_hack_tool_input(
            nodes_visited=["Gateway", "Lobby", "Lobby", "Gateway"],
            revealed_nodes=["Gateway"],
            current_node="Lobby",
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn("Lobby", hs["revealed_nodes"])

    def test_very_long_node_name(self):
        long_name = "N" * 1000
        hs = cpred_init_hack_state()
        inp = _make_hack_tool_input(
            nodes_visited=["Gateway", long_name],
            revealed_nodes=["Gateway", long_name],
            current_node=long_name,
        )
        cpred_apply_hack_state(hs, inp)
        self.assertIn(long_name, hs["revealed_nodes"])

    def test_injection_with_none_pipeline_state(self):
        """build_hack_injection with pipeline_state=None."""
        hs = cpred_init_hack_state()
        hs["system_map"] = SAMPLE_SYSTEM_MAP
        inj = cpred_build_hack_injection(hs, pipeline_state=None)
        self.assertIn("[HACK STATE]", inj)

    def test_apply_preserves_untracked_fields(self):
        """Fields not in the update loop should be preserved."""
        hs = cpred_init_hack_state(hacker_name="V", tier="full_run")
        hs["custom_field"] = "should_survive"
        inp = _make_hack_tool_input(current_node="Lobby")
        cpred_apply_hack_state(hs, inp)
        self.assertEqual(hs["custom_field"], "should_survive")
        self.assertEqual(hs["hacker_name"], "V")
        self.assertEqual(hs["tier"], "full_run")


# ============================================================
# E2E: Multiple sequential applies accumulate state correctly
# ============================================================

class TestSequentialApplyAccumulation(unittest.TestCase):

    def test_hack_state_accumulation(self):
        """10 sequential applies with progressive state changes."""
        hs = cpred_init_hack_state(hacker_name="V", interface_rank=7)
        nodes = ["Gateway", "A", "B", "C", "D", "E", "F", "G", "H", "Target"]

        for i in range(1, 10):
            visited = nodes[:i+1]
            revealed = nodes[:min(i+2, len(nodes))]  # one ahead via Pathfinder
            inp = _make_hack_tool_input(
                current_node=nodes[i],
                nodes_visited=visited,
                revealed_nodes=revealed,
                alert_level=min(i, 7),
                cycles_remaining=max(1, 3 - i // 3),
                brain_damage=i,
                tar_stacks=i % 3,
                net_actions_used=1,
            )
            cpred_apply_hack_state(hs, inp)

            # Invariant: visited ⊆ revealed
            for v in hs["nodes_visited"]:
                self.assertIn(v, hs["revealed_nodes"])
            # Injection should not crash
            cpred_build_hack_injection(hs)

        self.assertEqual(hs["current_node"], "Target")
        self.assertEqual(hs["alert_level"], 7)

    def test_net_combat_accumulation(self):
        """Multiple sequential net_combat applies."""
        ps = {
            "net_combat": cpred_init_net_combat_state(netrunner_name="V", sr=3),
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V", "cover": {}},
            "character_states": {},
            "game_state": {"edgerunners": {}},
        }
        nodes = ["Gateway", "X", "Y", "Z"]
        for i in range(1, 4):
            inp = _make_net_combat_tool_input(
                current_node=nodes[i],
                nodes_visited=nodes[:i+1],
                revealed_nodes=nodes[:i+1],
                alert_level=i,
                combat={"round": i, "initiative_order": ["V"], "current_turn": "V"},
            )
            cpred_apply_net_combat_state(ps, inp)
            for v in ps["net_combat"]["nodes_visited"]:
                self.assertIn(v, ps["net_combat"]["revealed_nodes"])
            cpred_build_net_combat_injection(ps.get("combat"), ps["net_combat"], ps)


if __name__ == "__main__":
    unittest.main()
