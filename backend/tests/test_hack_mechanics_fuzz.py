"""
Exhaustive fuzz tests for the hack/NET combat deterministic mechanics.

Covers all uncommitted code paths:
- resolve_actions: TAR penalty (next-check-only), alert DV, Zap damage, program_deactivate,
  program_attack_vs_netrunner, tar_consumed state_op
- apply_hack_state: rez_damage, program_deactivate, TAR clear, trace auto-tick,
  alert auto-spawn, forced disconnect
- apply_net_combat_state: same + forced disconnect on HP, completion flag ordering,
  trace gated on hack_state report
- Garbage inputs for all new fields (fuzz resilience)
- Multi-round state accumulation with new mechanics
- Cross-function integration (resolve → apply → inject)
"""

import copy
import json
import logging
import math
import os
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred_mechanics import resolve_actions, RESOLVE_MECHANICS_TOOL
from game_systems.cpred import (
    apply_hack_state as cpred_apply_hack_state,
    apply_net_combat_state as cpred_apply_net_combat_state,
    build_hack_injection as cpred_build_hack_injection,
    build_net_combat_injection as cpred_build_net_combat_injection,
    init_hack_state as cpred_init_hack_state,
    init_net_combat_state as cpred_init_net_combat_state,
    init_net_combat_from_hack as cpred_init_net_combat_from_hack,
)

MOCK = "game_systems.cpred_mechanics.random.randint"

ATOMS = [None, 0, 1, -1, 3.14, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]
BOUNDARY_NUMS = [
    0, 1, -1, 2**31, -(2**31), 2**63, -(2**63),
    float("inf"), float("-inf"), float("nan"),
    0.0, -0.0, 1e308, -1e308, 1e-308, 99999999999,
]


def _minimal_pipeline_state(nc=None):
    ps = {
        "character_states": {},
        "game_state": {"edgerunners": {}},
        "combat": {"round": 1, "initiative_order": [], "current_turn": ""},
    }
    if nc is not None:
        ps["net_combat"] = nc
    return ps


def _game_state_with_hp(name, current_hp, max_hp, conditions=None):
    er = {
        "hp": {"current": current_hp, "max": max_hp, "seriously_wounded": False},
    }
    if conditions:
        er["conditions"] = conditions
    return {
        "edgerunners": {
            name: er,
        }
    }


# ===========================================================================
# 1. resolve_actions: TAR penalty
# ===========================================================================
class TestResolveTarPenalty(unittest.TestCase):
    """TAR penalty auto-enforcement in resolve_actions."""

    @patch(MOCK, return_value=5)
    def test_tar_applied_to_net_skill_check(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, tar_stacks=2)
        # stat_value should be reduced by 4 (2*2)
        self.assertEqual(r["results"][0]["stat_value"], 4)  # 8-4
        self.assertTrue(r["tar_consumed"])
        self.assertTrue(any(op.get("op") == "tar_consumed" for op in r["state_ops"]))

    @patch(MOCK, return_value=5)
    def test_tar_not_applied_to_non_net(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13}]
        r = resolve_actions(actions, tar_stacks=2)
        self.assertEqual(r["results"][0]["stat_value"], 8)
        self.assertFalse(r["tar_consumed"])

    @patch(MOCK, return_value=5)
    def test_tar_only_first_net_check(self, _m):
        """TAR applies to first NET check only, not all."""
        actions = [
            {"type": "skill_check", "character": "V", "stat_value": 8,
             "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"},
            {"type": "skill_check", "character": "V", "stat_value": 8,
             "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"},
        ]
        r = resolve_actions(actions, tar_stacks=1)
        # First gets penalty
        self.assertEqual(r["results"][0]["stat_value"], 6)  # 8-2
        # Second does not
        self.assertEqual(r["results"][1]["stat_value"], 8)
        # Only one tar_consumed op
        tar_ops = [op for op in r["state_ops"] if op.get("op") == "tar_consumed"]
        self.assertEqual(len(tar_ops), 1)

    @patch(MOCK, return_value=5)
    def test_tar_applied_to_opposed_check(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 6, "defender_stat": 4, "net": True,
                     "ability": "Slide"}]
        r = resolve_actions(actions, tar_stacks=1)
        # attacker_stat reduced by 2
        self.assertTrue(r["tar_consumed"])

    @patch(MOCK, return_value=5)
    def test_tar_zero_stacks_no_effect(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, tar_stacks=0)
        self.assertEqual(r["results"][0]["stat_value"], 8)
        self.assertFalse(r["tar_consumed"])

    @patch(MOCK, return_value=5)
    def test_tar_not_applied_to_program_attack(self, _m):
        """program_attack is not skill_check/opposed_check, so TAR doesn't apply."""
        actions = [{"type": "program_attack", "character": "V",
                     "interface_rank": 6, "program_atk": 8, "target_def": 4,
                     "program_damage_dice": 3, "target_rez": 10,
                     "program_name": "Sword", "target": "Hellhound", "net": True}]
        r = resolve_actions(actions, tar_stacks=2)
        self.assertFalse(r["tar_consumed"])


# ===========================================================================
# 2. resolve_actions: Alert DV penalty
# ===========================================================================
class TestResolveAlertDV(unittest.TestCase):
    """Alert-level DV penalty auto-enforcement."""

    @patch(MOCK, return_value=5)
    def test_alert_dv_at_3(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, alert_level=3)
        self.assertEqual(r["results"][0]["dv"], 15)  # 13+2

    @patch(MOCK, return_value=5)
    def test_alert_dv_at_7(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, alert_level=7)
        self.assertEqual(r["results"][0]["dv"], 15)

    @patch(MOCK, return_value=5)
    def test_no_alert_dv_below_3(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, alert_level=2)
        self.assertEqual(r["results"][0]["dv"], 13)

    @patch(MOCK, return_value=5)
    def test_alert_dv_not_on_opposed(self, _m):
        """Alert DV only affects skill_check, not opposed_check."""
        # Zap (not Slide) so we don't trip the new Slide preemptive check.
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 6, "defender_stat": 4, "net": True,
                     "ability": "Zap"}]
        r = resolve_actions(actions, alert_level=5)
        # opposed_check doesn't have DV — no crash
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_alert_dv_not_on_non_net(self, _m):
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13}]
        r = resolve_actions(actions, alert_level=5)
        self.assertEqual(r["results"][0]["dv"], 13)

    @patch(MOCK, return_value=5)
    def test_tar_and_alert_combined(self, _m):
        """TAR + alert DV both apply to the same action."""
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, tar_stacks=1, alert_level=4)
        self.assertEqual(r["results"][0]["stat_value"], 6)  # 8 - 2
        self.assertEqual(r["results"][0]["dv"], 15)  # 13 + 2


# ===========================================================================
# 3. resolve_actions: Zap damage
# ===========================================================================
class TestResolveZapDamage(unittest.TestCase):
    """Zap damage resolution on opposed_check hit."""

    @patch(MOCK, return_value=8)
    def test_zap_on_hit(self, _m):
        """Successful opposed_check with zap=True rolls flat 1d6 damage."""
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 4,
                     "zap": True, "interface_rank": 4, "target": "Hellhound",
                     "net": True, "ability": "Zap"}]
        r = resolve_actions(actions)
        result = r["results"][0]
        if result["success"]:
            self.assertIn("zap_damage", result)
            self.assertIn("zap_dice", result)
            self.assertEqual(result["zap_dice"], 1)  # flat 1d6 per Hacking Rulebook §7
            self.assertGreater(result["zap_damage"], 0)
            self.assertIn("Zap", result["formatted"])
            # Should have rez_damage state_op
            rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
            self.assertEqual(len(rez_ops), 1)
            self.assertEqual(rez_ops[0]["target"], "Hellhound")

    @patch(MOCK, return_value=1)
    def test_zap_on_miss(self, _m):
        """Failed opposed_check with zap=True doesn't roll damage."""
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 2, "defender_stat": 10,
                     "zap": True, "interface_rank": 4, "target": "Hellhound"}]
        r = resolve_actions(actions)
        result = r["results"][0]
        if not result["success"]:
            self.assertNotIn("zap_damage", result)
            rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
            self.assertEqual(len(rez_ops), 0)

    @patch(MOCK, return_value=5)
    def test_zap_without_target_no_state_op(self, _m):
        """Zap hit without target field doesn't emit rez_damage op."""
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 3}]
        r = resolve_actions(actions)
        result = r["results"][0]
        if result["success"]:
            self.assertIn("zap_damage", result)
            rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
            self.assertEqual(len(rez_ops), 0)

    @patch(MOCK, return_value=5)
    def test_zap_false_no_damage(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": False, "interface_rank": 3}]
        r = resolve_actions(actions)
        self.assertNotIn("zap_damage", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_zap_interface_rank_1(self, _m):
        """Minimum interface_rank of 1 die."""
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 0}]
        r = resolve_actions(actions)
        if r["results"][0]["success"]:
            self.assertEqual(r["results"][0]["zap_dice"], 1)

    @patch(MOCK, return_value=6)
    def test_zap_emits_target_key_when_provided(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 3,
                     "target": "Hellhound", "target_ice_key": "NodeB_Hellhound"}]
        r = resolve_actions(actions, ice_status={
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active"},
        })
        rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
        self.assertEqual(len(rez_ops), 1)
        self.assertEqual(rez_ops[0].get("target_key"), "NodeB_Hellhound")

    @patch(MOCK, return_value=6)
    def test_zap_emits_rez_op_with_key_only(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 3,
                     "target_ice_key": "NodeB_Hellhound"}]
        r = resolve_actions(actions, ice_status={
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active"},
        })
        rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
        self.assertEqual(len(rez_ops), 1)
        self.assertEqual(rez_ops[0].get("target_key"), "NodeB_Hellhound")

    @patch(MOCK, return_value=6)
    def test_zap_target_name_case_insensitive_key_inference(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 3,
                     "target": "hellhound"}]
        r = resolve_actions(actions, ice_status={
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active"},
        })
        rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
        self.assertEqual(len(rez_ops), 1)
        self.assertEqual(rez_ops[0].get("target_key"), "NodeB_Hellhound")

    @patch(MOCK, return_value=6)
    def test_zap_target_node_hint_selects_unique_matching_key(self, _m):
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 3,
                     "target": "Hellhound", "target_node": "NodeB"}]
        r = resolve_actions(actions, ice_status={
            "NodeA_Hellhound": {"name": "Hellhound", "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active"},
        })
        rez_ops = [op for op in r["state_ops"] if op.get("op") == "rez_damage"]
        self.assertEqual(len(rez_ops), 1)
        self.assertEqual(rez_ops[0].get("target_key"), "NodeB_Hellhound")


# ===========================================================================
# 4. resolve_actions: program_deactivate
# ===========================================================================
class TestResolveProgramDeactivate(unittest.TestCase):
    """Program auto-deactivation after program_attack.

    Step 5: the legacy `program_deactivate` op was replaced by the registry's
    `program_status_change` op. Behavior is the same (auto-Deactivate fires
    after every program_attack regardless of hit/miss per RAW), but the
    op shape changed.
    """

    @patch(MOCK, return_value=5)
    def test_deactivate_on_attack(self, _m):
        actions = [{"type": "program_attack", "character": "V",
                     "interface_rank": 6, "program_atk": 8, "target_def": 4,
                     "program_damage_dice": 3, "target_rez": 10,
                     "program_name": "Sword", "target": "Hellhound"}]
        r = resolve_actions(actions)
        result = r["results"][0]
        self.assertEqual(result["program_deactivated"], "Sword")
        self.assertIn("deactivation_note", result)
        psc_ops = [op for op in r["state_ops"]
                   if op.get("op") == "program_status_change"
                   and op.get("program_name") == "Sword"
                   and op.get("new_status") == "deactivated"]
        self.assertEqual(len(psc_ops), 1)

    @patch(MOCK, return_value=5)
    def test_no_deactivate_without_program_name(self, _m):
        actions = [{"type": "program_attack", "character": "V",
                     "interface_rank": 6, "program_atk": 8, "target_def": 4,
                     "program_damage_dice": 3, "target_rez": 10,
                     "program_name": "", "target": "Hellhound"}]
        r = resolve_actions(actions)
        psc_ops = [op for op in r["state_ops"]
                   if op.get("op") == "program_status_change"]
        self.assertEqual(len(psc_ops), 0)

    @patch(MOCK, return_value=1)
    def test_deactivate_even_on_miss(self, _m):
        """Programs deactivate after use regardless of hit/miss (RAW)."""
        actions = [{"type": "program_attack", "character": "V",
                     "interface_rank": 6, "program_atk": 2, "target_def": 10,
                     "program_damage_dice": 3, "target_rez": 10,
                     "program_name": "Sword", "target": "Hellhound"}]
        r = resolve_actions(actions)
        self.assertEqual(r["results"][0]["program_deactivated"], "Sword")
        psc_ops = [op for op in r["state_ops"]
                   if op.get("op") == "program_status_change"
                   and op.get("program_name") == "Sword"]
        self.assertEqual(len(psc_ops), 1)


# ===========================================================================
# 5. resolve_actions: program_attack_vs_netrunner
# ===========================================================================
class TestResolveProgramAttackVsNetrunner(unittest.TestCase):
    """Black ICE attacking the Netrunner — brain_damage state_op."""

    @patch(MOCK, return_value=8)
    def test_hit_produces_brain_damage_op(self, _m):
        actions = [{"type": "program_attack_vs_netrunner", "character": "Hellhound",
                     "interface_rank": 6, "program_atk": 8, "target_def": 4,
                     "program_damage_dice": 3, "target_rez": 0,
                     "program_name": "Hellhound", "target": "V"}]
        r = resolve_actions(actions)
        result = r["results"][0]
        self.assertEqual(result["type"], "program_attack_vs_netrunner")
        if result.get("hit"):
            bd_ops = [op for op in r["state_ops"] if op.get("op") == "brain_damage"]
            self.assertEqual(len(bd_ops), 1)
            self.assertEqual(bd_ops[0]["edgerunner"], "V")
            self.assertGreater(bd_ops[0]["change"], 0)

    @patch(MOCK, return_value=1)
    def test_miss_no_brain_damage_op(self, _m):
        actions = [{"type": "program_attack_vs_netrunner", "character": "Hellhound",
                     "interface_rank": 6, "program_atk": 2, "target_def": 10,
                     "program_damage_dice": 3, "target_rez": 0,
                     "program_name": "Hellhound", "target": "V"}]
        r = resolve_actions(actions)
        if not r["results"][0].get("hit"):
            bd_ops = [op for op in r["state_ops"] if op.get("op") == "brain_damage"]
            self.assertEqual(len(bd_ops), 0)

    @patch(MOCK, return_value=5)
    def test_in_enum(self, _m):
        """program_attack_vs_netrunner is in the tool enum."""
        enum = RESOLVE_MECHANICS_TOOL["input_schema"]["properties"]["actions"]["items"]["properties"]["type"]["enum"]
        self.assertIn("program_attack_vs_netrunner", enum)


# ===========================================================================
# 6. apply_hack_state: program_deactivate op
# ===========================================================================
class TestApplyProgramDeactivate(unittest.TestCase):
    """apply_hack_state processes program_deactivate ops."""

    def test_deactivate_named_program(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["active_programs"] = [
            {"name": "Sword", "status": "active"},
            {"name": "Shield", "status": "active"},
        ]
        ops = [{"op": "program_deactivate", "program_name": "Sword"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["active_programs"][0]["status"], "deactivated")
        self.assertEqual(hs["active_programs"][1]["status"], "active")

    def test_deactivate_missing_program(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["active_programs"] = [{"name": "Sword", "status": "active"}]
        ops = [{"op": "program_deactivate", "program_name": "Nonexistent"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["active_programs"][0]["status"], "active")

    def test_deactivate_garbage_programs_list(self):
        """active_programs is garbage — no crash."""
        hs = cpred_init_hack_state(tier="full_run")
        for garbage in [None, "string", 42, True, {}, [None], [42], ["str"]]:
            hs["active_programs"] = garbage
            ops = [{"op": "program_deactivate", "program_name": "Sword"}]
            cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)


# ===========================================================================
# 7. apply_hack_state: rez_damage op
# ===========================================================================
class TestApplyRezDamage(unittest.TestCase):
    """apply_hack_state processes rez_damage ops."""

    def test_rez_damage_reduces_ice(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "GW_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["GW_Hellhound"]["rez_current"], 6)
        self.assertEqual(hs["ice_status"]["GW_Hellhound"]["status"], "active")

    def test_rez_damage_derezes_ice(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "GW_Hellhound": {"name": "Hellhound", "rez_current": 3, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 5}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["GW_Hellhound"]["rez_current"], 0)
        self.assertEqual(hs["ice_status"]["GW_Hellhound"]["status"], "derezzed")

    def test_rez_damage_garbage_ice_status(self):
        """ice_status is garbage — no crash."""
        hs = cpred_init_hack_state(tier="full_run")
        for garbage in [None, "string", 42, True, [], [1]]:
            hs["ice_status"] = garbage
            ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 5}]
            cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)

    def test_rez_damage_garbage_rez_current(self):
        """rez_current is garbage — no crash."""
        hs = cpred_init_hack_state(tier="full_run")
        for garbage in ["five", None, [], {}, True]:
            hs["ice_status"] = {
                "GW": {"name": "Hellhound", "rez_current": garbage, "status": "active"},
            }
            ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 5}]
            cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)

    def test_rez_damage_duplicate_name_uses_target_key(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "target_key": "NodeB_Hellhound", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["NodeA_Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["NodeB_Hellhound"]["rez_current"], 6)

    def test_rez_damage_duplicate_name_falls_back_to_current_node(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["current_node"] = "NodeB_Hellhound"
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["NodeA_Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["NodeB_Hellhound"]["rez_current"], 6)

    def test_rez_damage_with_bad_target_key_does_not_fallback(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "target_key": "MissingNode", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["NodeA_Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["NodeB_Hellhound"]["rez_current"], 10)

    def test_rez_damage_name_match_is_case_insensitive(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "hellhound", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["NodeA_Hellhound"]["rez_current"], 6)

    def test_rez_damage_ambiguous_duplicate_name_without_key_noop(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["NodeA_Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["NodeB_Hellhound"]["rez_current"], 10)

    def test_rez_damage_does_not_treat_target_name_as_ice_status_key(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "Hellhound": {"name": "Asp", "rez_current": 10, "rez_max": 10, "status": "active"},
            "Node2": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 3}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["Node2"]["rez_current"], 7)

    def test_rez_damage_target_node_hint_selects_unique_candidate(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "Lobby_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
            "Vault_Hellhound": {"name": "Hellhound", "rez_current": 10, "rez_max": 10, "status": "active"},
        }
        ops = [{"op": "rez_damage", "target": "Hellhound", "target_node": "Vault", "damage": 4}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["ice_status"]["Lobby_Hellhound"]["rez_current"], 10)
        self.assertEqual(hs["ice_status"]["Vault_Hellhound"]["rez_current"], 6)


# ===========================================================================
# 8. apply_hack_state: TAR clear only on consumption
# ===========================================================================
class TestApplyTarClear(unittest.TestCase):
    """TAR stacks only clear when tar_consumed op is present."""

    def test_tar_persists_without_resolver(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 2
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertEqual(hs["tar_stacks"], 2)

    def test_tar_persists_with_empty_ops(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 2
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=[])
        self.assertEqual(hs["tar_stacks"], 2)

    def test_tar_clears_with_tar_consumed_op(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 2
        ops = [{"op": "tar_consumed"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["tar_stacks"], 0)

    def test_tar_persists_with_other_ops(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 2
        ops = [{"op": "brain_damage", "change": 3, "edgerunner": "V"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(hs["tar_stacks"], 2)

    def test_tar_garbage_values_no_crash(self):
        hs = cpred_init_hack_state(tier="full_run")
        for garbage in ATOMS + BOUNDARY_NUMS:
            hs["tar_stacks"] = garbage
            cpred_apply_hack_state(hs, {"hack_state": {}})


# ===========================================================================
# 9. apply_hack_state: Trace auto-increment
# ===========================================================================
class TestApplyTraceAutoIncrement(unittest.TestCase):
    """Trace ticks once per completed turn (meatspace_due)."""

    def test_trace_ticks_on_meatspace_due(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["trace_progress"] = 0
        hs["sr"] = 3
        hs["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        # Simulate turn completion: net_actions_used = all remaining
        hs["net_actions_remaining"] = 1
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertTrue(hs["meatspace_due"])
        self.assertEqual(hs["trace_progress"], 1)

    def test_trace_no_tick_without_meatspace_due(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["trace_progress"] = 0
        hs["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        hs["net_actions_remaining"] = 3
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertFalse(hs.get("meatspace_due", False))
        self.assertEqual(hs["trace_progress"], 0)

    def test_trace_no_tick_if_no_active_trace_ice(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["trace_progress"] = 0
        hs["ice_status"] = {
            "GW_Patrol": {"name": "Patrol", "behavior": "patrol", "status": "active",
                           "rez_current": 6, "rez_max": 6},
        }
        hs["net_actions_remaining"] = 1
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertEqual(hs["trace_progress"], 0)

    def test_trace_no_tick_if_progress_is_none(self):
        """trace_progress=None means no Trace spawned yet."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["trace_progress"] = None
        hs["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        hs["net_actions_remaining"] = 1
        cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertIsNone(hs["trace_progress"])

    def test_trace_completion_forces_convergence(self):
        """Trace completing rolls dispatch (does NOT force Convergence — those
        are decoupled tracks per the corrected RAW interpretation)."""
        from unittest.mock import patch
        hs = cpred_init_hack_state(tier="full_run")
        hs["sr"] = 3
        hs["trace_progress"] = 2  # max is 6-3=3, so one more tick → 3 >= 3
        hs["alert_level"] = 5
        hs["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        hs["net_actions_remaining"] = 1
        # Force dispatch pass (d10=10) and a fixed ETA (d6=4) → ETA=4+2=6.
        with patch("game_systems.cpred_hack.random.randint",
                   side_effect=[10, 4]):
            cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        self.assertEqual(hs["trace_progress"], 3)
        # Alert is unchanged — Trace and Convergence are decoupled.
        self.assertEqual(hs["alert_level"], 5)
        # Dispatch attempted, passed, ETA decremented by one same-call.
        self.assertTrue(hs["meatspace_security_dispatch_attempted"])
        self.assertTrue(hs["meatspace_security_dispatched"])
        self.assertEqual(hs["meatspace_security_eta"], 5)  # 6 - 1 in-call

    def test_trace_garbage_values_no_crash(self):
        """Garbage in trace-related fields doesn't crash."""
        logging.disable(logging.CRITICAL)
        try:
            for garbage in ATOMS + BOUNDARY_NUMS:
                hs = cpred_init_hack_state(tier="full_run")
                hs["trace_progress"] = garbage
                hs["sr"] = garbage
                hs["ice_status"] = garbage
                hs["net_actions_remaining"] = 1
                cpred_apply_hack_state(hs, {"hack_state": {"net_actions_used": 1}})
        finally:
            logging.disable(logging.NOTSET)


# ===========================================================================
# 10. apply_hack_state: Alert threshold auto-spawn
# ===========================================================================
class TestApplyAlertAutoSpawn(unittest.TestCase):
    """ICE auto-spawns at alert thresholds."""

    def test_trace_spawns_at_alert_5(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["_prev_alert_level"] = 0
        hs["sr"] = 3
        hs["ice_status"] = {}
        cpred_apply_hack_state(hs, {"hack_state": {"alert_level": 5}})
        self.assertIn("Gateway_Trace", hs["ice_status"])
        trace = hs["ice_status"]["Gateway_Trace"]
        self.assertEqual(trace["behavior"], "trace")
        self.assertEqual(trace["status"], "active")
        self.assertEqual(trace["rez_current"], 6)  # sr*2
        self.assertEqual(hs["trace_progress"], 0)

    def test_no_trace_spawn_if_trace_exists(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["_prev_alert_level"] = 0
        hs["ice_status"] = {
            "Existing_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                                "rez_current": 4, "rez_max": 4},
        }
        cpred_apply_hack_state(hs, {"hack_state": {"alert_level": 5}})
        self.assertNotIn("Gateway_Trace", hs["ice_status"])

    def test_convergence_ice_spawns_at_alert_7(self):
        """SR 3 spawns Giant at convergence (alert 7+)."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["_prev_alert_level"] = 0
        hs["sr"] = 3
        hs["ice_status"] = {}
        hs["current_node"] = "Server Room"
        cpred_apply_hack_state(hs, {"hack_state": {"alert_level": 7}})
        self.assertIn("Server Room_Convergence", hs["ice_status"])
        spawned = hs["ice_status"]["Server Room_Convergence"]
        self.assertEqual(spawned["name"], "Giant")  # SR 3 → Giant per CONVERGENCE_ICE_BY_SR
        self.assertEqual(spawned["behavior"], "black")
        self.assertEqual(spawned["rez_current"], 25)  # ICE_STAT_BLOCKS["giant"]["rez"]

    def test_no_respawn_on_same_alert(self):
        """Auto-spawn only triggers on threshold crossing, not re-reporting same level."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["_prev_alert_level"] = 5
        hs["ice_status"] = {}
        cpred_apply_hack_state(hs, {"hack_state": {"alert_level": 5}})
        self.assertNotIn("Gateway_Trace", hs["ice_status"])

    def test_both_spawn_on_jump_0_to_7(self):
        """Alert jumping from 0 to 7 spawns both Trace and Kraken."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["_prev_alert_level"] = 0
        hs["sr"] = 3
        hs["ice_status"] = {}
        hs["current_node"] = "Gateway"
        cpred_apply_hack_state(hs, {"hack_state": {"alert_level": 7}})
        self.assertIn("Gateway_Trace", hs["ice_status"])
        self.assertIn("Gateway_Convergence", hs["ice_status"])

    def test_garbage_alert_values_no_crash(self):
        logging.disable(logging.CRITICAL)
        try:
            for garbage in ATOMS + BOUNDARY_NUMS:
                hs = cpred_init_hack_state(tier="full_run")
                hs["_prev_alert_level"] = garbage
                hs["alert_level"] = garbage
                hs["ice_status"] = garbage
                cpred_apply_hack_state(hs, {"hack_state": {}})
        finally:
            logging.disable(logging.NOTSET)


# ===========================================================================
# 11. apply_hack_state: Forced disconnect
# ===========================================================================
class TestApplyForcedDisconnectHack(unittest.TestCase):
    """Forced disconnect when brain damage drives Netrunner HP below 0."""

    def test_forced_disconnect_on_lethal_brain_damage(self):
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["brain_damage"] = 25
        hs["active"] = True
        gs = _game_state_with_hp("V", 0, 25, conditions=["dead"])
        cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=gs)
        self.assertFalse(hs["active"])
        self.assertTrue(hs.get("_forced_disconnect"))
        self.assertIn("dead", hs.get("narrative_summary", ""))

    def test_no_disconnect_when_hp_sufficient(self):
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["brain_damage"] = 10
        hs["active"] = True
        gs = _game_state_with_hp("V", 25, 25)
        cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=gs)
        self.assertTrue(hs["active"])

    def test_no_disconnect_without_game_state(self):
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["brain_damage"] = 100
        hs["active"] = True
        cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=None)
        self.assertTrue(hs["active"])

    def test_unconscious_netrunner_stays_jacked_in(self):
        """RAW p.187: Unconscious does NOT auto-disconnect. The Netrunner is stuck
        jacked in as a sitting duck; only a failed Death Save (flatline) or an ally
        physically unplugging them ends the hack. Regression guard against the old
        behavior that auto-disconnected unconscious netrunners."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["brain_damage"] = 5
        hs["active"] = True
        gs = _game_state_with_hp("V", 0, 25, conditions=["unconscious"])
        cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=gs)
        self.assertTrue(hs["active"], "Unconscious netrunner must NOT auto-disconnect")
        self.assertFalse(hs.get("_forced_disconnect"))

    def test_flatlined_netrunner_is_force_disconnected(self):
        """RAW p.187: flatline (failed Death Save = dead) is the ONLY auto-disconnect
        trigger. Cascades all rezzed Black ICE."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 0, 25, conditions=["dead"])
        cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=gs)
        self.assertFalse(hs["active"])
        self.assertTrue(hs.get("_forced_disconnect"))

    def test_disconnect_garbage_game_state_no_crash(self):
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["brain_damage"] = 100
        hs["active"] = True
        for garbage in ATOMS:
            cpred_apply_hack_state(hs, {"hack_state": {}}, game_state=garbage)

    def test_initiate_unsafe_jack_out_cascades_and_ends_hack(self):
        """Model-signaled Unsafe Jack Out: backend fires ICE cascade + ends hack.
        No flatline or unconscious needed — this is the explicit rescue path."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        # Active Black ICE with a damaging effect so we can observe the cascade
        hs["ice_status"] = {
            "Gateway": {"name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
                        "rez_current": 20, "rez_max": 20, "status": "active"},
        }
        gs = _game_state_with_hp("V", 20, 30)  # conscious, 20 HP — no auto-disconnect trigger
        tool_input = {
            "hack_state": {},
            "initiate_unsafe_jack_out": {
                "cause": "ally_dragged_out_of_range",
                "actor": "Kessler",
                "reason": "Kessler dragged V out of the access point to save her from the Hellhound.",
            },
        }
        cpred_apply_hack_state(hs, tool_input, game_state=gs)
        self.assertFalse(hs["active"], "Unsafe Jack Out must end the hack")
        self.assertTrue(hs.get("_forced_disconnect"))
        self.assertTrue(hs.get("_cascade_applied"))
        summary = hs.get("narrative_summary", "")
        self.assertIn("Unsafe Jack Out", summary)
        self.assertIn("Kessler", summary)

    def test_initiate_unsafe_jack_out_without_actor(self):
        """Actor field optional — summary still includes the reason."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 10, 30)
        tool_input = {
            "hack_state": {},
            "initiate_unsafe_jack_out": {
                "cause": "connection_severed",
                "reason": "Building power cut severed the connection.",
            },
        }
        cpred_apply_hack_state(hs, tool_input, game_state=gs)
        self.assertFalse(hs["active"])
        self.assertIn("Building power cut", hs.get("narrative_summary", ""))

    def test_initiate_unsafe_jack_out_idempotent(self):
        """Re-applying does not double-cascade. _cascade_applied guards it."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        hs["ice_status"] = {
            "Gateway": {"name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
                        "rez_current": 20, "rez_max": 20, "status": "active"},
        }
        gs = _game_state_with_hp("V", 30, 30)
        tool_input = {
            "hack_state": {},
            "initiate_unsafe_jack_out": {
                "cause": "self_unplugged",
                "actor": "self",
                "reason": "V yanked the plug before the Trace completed.",
            },
        }
        cpred_apply_hack_state(hs, tool_input, game_state=gs)
        hp_after_first = gs["edgerunners"]["V"]["hp"]["current"]
        # Re-apply with same input — the cascade should NOT fire again
        cpred_apply_hack_state(hs, tool_input, game_state=gs)
        hp_after_second = gs["edgerunners"]["V"]["hp"]["current"]
        self.assertEqual(hp_after_first, hp_after_second,
                         "Second call must not re-apply cascade damage")

    def test_initiate_unsafe_jack_out_ignored_when_not_dict(self):
        """Malformed initiate_unsafe_jack_out (wrong type) is a no-op."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 30, 30)
        for garbage in ["string", 42, [], True, None]:
            hs_copy = copy.deepcopy(hs)
            cpred_apply_hack_state(hs_copy, {"hack_state": {}, "initiate_unsafe_jack_out": garbage}, game_state=gs)
            self.assertTrue(hs_copy["active"],
                            f"Malformed input {garbage!r} must not end the hack")

    def test_ds_penalty_not_applied_when_attack_brings_to_mw(self):
        """RAW p.189: the attack that first makes you Mortally Wounded does not
        itself trigger the +1 Death Save penalty. Regression guard against the
        old overflow-per-point rule."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 10, 30)
        gs["edgerunners"]["V"]["death_save_count"] = 0
        # Single attack, 25 brain damage — enough to overflow by 15.
        # Old buggy behavior: +15 DS penalty. RAW: +0 (this attack makes them MW).
        ops = [{"op": "brain_damage", "change": 25, "target": "V"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(gs["edgerunners"]["V"]["hp"]["current"], 0)
        self.assertEqual(gs["edgerunners"]["V"]["death_save_count"], 0,
                         "Attack that makes you MW must NOT increment DS penalty")

    def test_ds_penalty_plus_one_when_hit_while_already_mw(self):
        """RAW p.189: when hit by an attack while ALREADY at 0 HP, +1 DS
        penalty (regardless of damage amount). Previously bug: +damage points."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 0, 30)  # already MW
        gs["edgerunners"]["V"]["death_save_count"] = 2
        # A massive 20-point attack: should only bump DS by +1, NOT +20.
        ops = [{"op": "brain_damage", "change": 20, "target": "V"}]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(gs["edgerunners"]["V"]["death_save_count"], 3,
                         "A 20-damage attack while MW must add +1 DS penalty, not +20")

    def test_ds_penalty_batched_attacks_while_already_mw(self):
        """Cascade of 3 Black ICE attacks while already Mortally Wounded → +3 DS
        penalty (one per attack in the batch)."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 0, 30)
        gs["edgerunners"]["V"]["death_save_count"] = 1
        ops = [
            {"op": "brain_damage", "change": 5, "target": "V"},
            {"op": "brain_damage", "change": 8, "target": "V"},
            {"op": "brain_damage", "change": 3, "target": "V"},
        ]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(gs["edgerunners"]["V"]["death_save_count"], 4,
                         "3 attacks while MW → +3 DS (started at 1, now 4)")

    def test_ds_penalty_batched_attacks_first_brings_to_mw(self):
        """Cascade of 3 attacks starting ABOVE 0 HP: one brings you to MW,
        the other two are post-MW hits (+2 DS). Approximation assumes the
        first attack in the batch is the MW-maker."""
        hs = cpred_init_hack_state(tier="full_run", hacker_name="V")
        hs["active"] = True
        gs = _game_state_with_hp("V", 10, 30)
        gs["edgerunners"]["V"]["death_save_count"] = 0
        ops = [
            {"op": "brain_damage", "change": 15, "target": "V"},
            {"op": "brain_damage", "change": 5, "target": "V"},
            {"op": "brain_damage", "change": 5, "target": "V"},
        ]
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=ops, game_state=gs)
        self.assertEqual(gs["edgerunners"]["V"]["death_save_count"], 2,
                         "3 attacks starting above 0 HP, ending at MW: +2 DS")


# ===========================================================================
# 12. apply_net_combat_state: Forced disconnect after completion flags
# ===========================================================================
class TestApplyForcedDisconnectNetCombat(unittest.TestCase):
    """Forced disconnect can't be overwritten by completion flags."""

    def test_forced_disconnect_not_overwritten(self):
        """Even if model says net_complete=false, forced disconnect wins."""
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        ps = _minimal_pipeline_state(nc)
        ps["game_state"] = _game_state_with_hp("V", 0, 25, conditions=["dead"])
        tool_input = {
            "hack_state": {},
            "net_complete": False,
            "combat_complete": False,
        }
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertTrue(ps["net_combat"]["net_complete"])
        self.assertTrue(ps["net_combat"].get("_forced_disconnect"))

    def test_no_forced_disconnect_when_hp_positive(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        ps = _minimal_pipeline_state(nc)
        ps["game_state"] = _game_state_with_hp("V", 10, 25)
        tool_input = {"hack_state": {}, "net_complete": False, "combat_complete": False}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertFalse(ps["net_combat"]["net_complete"])

    def test_unconscious_netrunner_does_not_auto_disconnect(self):
        """RAW p.187: Unconscious (KO, sleep ammo, etc.) does NOT auto-disconnect.
        Netrunner is stuck jacked in — ICE/Demons keep acting on them — until an
        ally physically unplugs or drags the body out of range (= Unsafe Jack Out,
        model-driven via hack_complete + cascade narration). Regression guard
        against the old auto-disconnect-on-unconscious behavior."""
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        ps = _minimal_pipeline_state(nc)
        ps["game_state"] = _game_state_with_hp("V", 1, 25)
        ps["game_state"]["edgerunners"]["V"]["conditions"] = ["Unconscious"]
        tool_input = {"hack_state": {}, "net_complete": False, "combat_complete": False}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertFalse(ps["net_combat"]["net_complete"],
                         "Unconscious must NOT auto-complete the NET track")
        self.assertFalse(ps["net_combat"].get("_forced_disconnect"))

    def test_forced_disconnect_skipped_if_already_complete(self):
        """If model already set net_complete=true, disconnect check is skipped (not nc.get("net_complete"))."""
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        ps = _minimal_pipeline_state(nc)
        ps["game_state"] = _game_state_with_hp("V", 0, 25)
        tool_input = {"hack_state": {}, "net_complete": True, "combat_complete": True}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        # net_complete is True (from model), _forced_disconnect not set since model handled it
        self.assertTrue(ps["net_combat"]["net_complete"])
        self.assertFalse(ps["net_combat"].get("_forced_disconnect", False))


# ===========================================================================
# 13. apply_net_combat_state: Trace gated on hack_state report
# ===========================================================================
class TestNetCombatTraceGating(unittest.TestCase):
    """Trace only ticks when model reported hack_state in tool_input."""

    def test_trace_ticks_with_hack_state(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["trace_progress"] = 0
        nc["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        ps = _minimal_pipeline_state(nc)
        tool_input = {"hack_state": {"alert_level": 2, "net_actions_used": 2}}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertEqual(ps["net_combat"]["trace_progress"], 1)

    def test_trace_no_tick_without_hack_state(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["trace_progress"] = 0
        nc["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        ps = _minimal_pipeline_state(nc)
        # Meatspace-only report — no hack_state key
        tool_input = {"combat_complete": False, "net_complete": False}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertEqual(ps["net_combat"]["trace_progress"], 0)

    def test_trace_no_tick_hack_state_is_not_dict(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["trace_progress"] = 0
        nc["ice_status"] = {
            "GW_Trace": {"name": "Trace", "behavior": "trace", "status": "active",
                          "rez_current": 6, "rez_max": 6},
        }
        ps = _minimal_pipeline_state(nc)
        tool_input = {"hack_state": "garbage"}
        cpred_apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertEqual(ps["net_combat"]["trace_progress"], 0)


# ===========================================================================
# 14. apply_net_combat_state: Duplicated mechanics (rez_damage, program_deactivate, TAR, alert)
# ===========================================================================
class TestNetCombatApplyMechanics(unittest.TestCase):
    """Net_combat versions of the same mechanics."""

    def test_rez_damage_in_net_combat(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["ice_status"] = {
            "GW_Hellhound": {"name": "Hellhound", "rez_current": 8, "rez_max": 8, "status": "active"},
        }
        ps = _minimal_pipeline_state(nc)
        ops = [{"op": "rez_damage", "target": "Hellhound", "damage": 10}]
        cpred_apply_net_combat_state(ps, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(ps["net_combat"]["ice_status"]["GW_Hellhound"]["rez_current"], 0)
        self.assertEqual(ps["net_combat"]["ice_status"]["GW_Hellhound"]["status"], "derezzed")

    def test_rez_damage_in_net_combat_duplicate_name_uses_target_key(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 8, "rez_max": 8, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 8, "rez_max": 8, "status": "active"},
        }
        ps = _minimal_pipeline_state(nc)
        ops = [{"op": "rez_damage", "target": "Hellhound", "target_key": "NodeB_Hellhound", "damage": 3}]
        cpred_apply_net_combat_state(ps, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(ps["net_combat"]["ice_status"]["NodeA_Hellhound"]["rez_current"], 8)
        self.assertEqual(ps["net_combat"]["ice_status"]["NodeB_Hellhound"]["rez_current"], 5)

    def test_rez_damage_in_net_combat_bad_key_does_not_fallback(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "rez_current": 8, "rez_max": 8, "status": "active"},
            "NodeB_Hellhound": {"name": "Hellhound", "rez_current": 8, "rez_max": 8, "status": "active"},
        }
        ps = _minimal_pipeline_state(nc)
        ops = [{"op": "rez_damage", "target": "Hellhound", "target_key": "MissingNode", "damage": 3}]
        cpred_apply_net_combat_state(ps, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(ps["net_combat"]["ice_status"]["NodeA_Hellhound"]["rez_current"], 8)
        self.assertEqual(ps["net_combat"]["ice_status"]["NodeB_Hellhound"]["rez_current"], 8)

    def test_tar_clear_in_net_combat(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["tar_stacks"] = 2
        ps = _minimal_pipeline_state(nc)
        ops = [{"op": "tar_consumed"}]
        cpred_apply_net_combat_state(ps, {"hack_state": {}}, resolver_state_ops=ops)
        self.assertEqual(ps["net_combat"]["tar_stacks"], 0)

    def test_tar_persists_without_consumption_in_net_combat(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["tar_stacks"] = 2
        ps = _minimal_pipeline_state(nc)
        cpred_apply_net_combat_state(ps, {"hack_state": {}}, resolver_state_ops=[])
        self.assertEqual(ps["net_combat"]["tar_stacks"], 2)

    def test_alert_spawn_in_net_combat(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["_prev_alert_level"] = 0
        nc["ice_status"] = {}
        nc["current_node"] = "DataNode1"
        ps = _minimal_pipeline_state(nc)
        cpred_apply_net_combat_state(ps, {"hack_state": {"alert_level": 7}})
        ice = ps["net_combat"]["ice_status"]
        self.assertIn("Gateway_Trace", ice)
        self.assertIn("DataNode1_Convergence", ice)


# ===========================================================================
# 15. Injection: lockdown DV, TAR text, alert effects
# ===========================================================================
class TestInjectionNewText(unittest.TestCase):
    """Injection strings contain new enforced mechanics text."""

    def test_lockdown_dv_in_hack_injection(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["alert_level"] = 6
        result = cpred_build_hack_injection(hs)
        self.assertIn("LOCKDOWN", result)
        self.assertIn("DV 15", result)  # SR 3 base DV from SR_BASE_DV table

    def test_tar_text_in_hack_injection(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 2
        result = cpred_build_hack_injection(hs)
        self.assertIn("auto-applied to next NET check", result)
        self.assertIn("-4", result)  # 2 * 2

    def test_no_net_actions_warning(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["net_actions_remaining"] = 0
        result = cpred_build_hack_injection(hs)
        self.assertIn("NO NET ACTIONS LEFT", result)

    def test_net_combat_alert_effects_in_injection(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["alert_level"] = 5
        ps = _minimal_pipeline_state(nc)
        combat = ps.get("combat")
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("DVs +2", result)
        self.assertIn("LOCKDOWN", result)
        self.assertIn("DV 15", result)  # SR 3 base DV from SR_BASE_DV table

    def test_net_combat_tar_text_in_injection(self):
        nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
        nc["tar_stacks"] = 1
        ps = _minimal_pipeline_state(nc)
        combat = ps.get("combat")
        result = cpred_build_net_combat_injection(combat, nc, ps)
        self.assertIn("auto-applied to next NET check", result)


# ===========================================================================
# 16. Integration: resolve → apply → inject round-trip
# ===========================================================================
class TestResolveApplyInjectIntegration(unittest.TestCase):
    """Full round-trip: resolve actions, apply state_ops, build injection."""

    @patch(MOCK, return_value=8)
    def test_zap_resolve_apply_inject(self, _m):
        """Zap hit → rez_damage op → apply derezes ICE → injection shows derezzed."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["ice_status"] = {
            "GW_Hellhound": {"name": "Hellhound", "behavior": "patrol",
                              "rez_current": 3, "rez_max": 8, "status": "active"},
        }
        actions = [{"type": "opposed_check", "character": "V",
                     "attacker_stat": 10, "defender_stat": 2,
                     "zap": True, "interface_rank": 4, "target": "Hellhound",
                     "net": True}]
        r = resolve_actions(actions)
        if r["results"][0].get("success"):
            cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=r["state_ops"])
            injection = cpred_build_hack_injection(hs)
            self.assertIsInstance(injection, str)
            # ICE should be derezzed
            self.assertEqual(hs["ice_status"]["GW_Hellhound"]["status"], "derezzed")

    @patch(MOCK, return_value=8)
    def test_program_attack_deactivate_round_trip(self, _m):
        hs = cpred_init_hack_state(tier="full_run")
        hs["active_programs"] = [
            {"name": "Sword", "category": "attacker", "rez": 5, "status": "active"},
        ]
        actions = [{"type": "program_attack", "character": "V",
                     "interface_rank": 6, "program_atk": 8, "target_def": 4,
                     "program_damage_dice": 3, "target_rez": 10,
                     "program_name": "Sword", "target": "Hellhound"}]
        r = resolve_actions(actions)
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=r["state_ops"])
        self.assertEqual(hs["active_programs"][0]["status"], "deactivated")

    @patch(MOCK, return_value=5)
    def test_tar_resolve_apply_round_trip(self, _m):
        """TAR consumed by resolver → cleared in apply."""
        hs = cpred_init_hack_state(tier="full_run")
        hs["tar_stacks"] = 1
        actions = [{"type": "skill_check", "character": "V", "stat_value": 8,
                     "skill_value": 0, "dv": 13, "net": True, "ability": "Backdoor"}]
        r = resolve_actions(actions, tar_stacks=1)
        cpred_apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=r["state_ops"])
        self.assertEqual(hs["tar_stacks"], 0)


# ===========================================================================
# 17. Massive fuzz: randomized multi-round with all new mechanics
# ===========================================================================
class TestMultiRoundWithNewMechanics(unittest.TestCase):
    """Multi-round random apply with resolver_state_ops — no crashes."""

    def test_hack_fuzz_500_iterations(self):
        logging.disable(logging.CRITICAL)
        try:
            rng = random.Random(7001)
            op_types = ["brain_damage", "program_deactivate", "rez_damage", "tar_consumed", "garbage"]
            for _ in range(500):
                hs = cpred_init_hack_state(tier=rng.choice(["full_run", "quick_hack"]),
                                           hacker_name="V")
                gs = _game_state_with_hp("V", rng.randint(1, 40), 40)
                for _ in range(rng.randint(3, 8)):
                    # Random tool_input
                    ti = {"hack_state": {}}
                    hs_report = ti["hack_state"]
                    if rng.random() < 0.5:
                        hs_report["alert_level"] = rng.choice([0, 1, 2, 3, 4, 5, 6, 7, "x", None, []])
                    if rng.random() < 0.3:
                        hs_report["net_actions_used"] = rng.choice([0, 1, 2, 3, "x", None])
                    if rng.random() < 0.3:
                        hs_report["tar_stacks"] = rng.choice([0, 1, 2, 3, "x", None, {}])
                    if rng.random() < 0.3:
                        hs_report["trace_progress"] = rng.choice([None, 0, 1, 2, 3, "x", []])
                    if rng.random() < 0.3:
                        hs_report["ice_status"] = rng.choice([
                            {}, None, "bad",
                            {"GW": {"name": "Trace", "behavior": "trace", "status": "active",
                                     "rez_current": 6, "rez_max": 6}},
                            {"GW": {"name": "Hellhound", "behavior": "patrol", "status": "active",
                                     "rez_current": rng.choice([5, "x", None]), "rez_max": 8}},
                        ])
                    # Random resolver_state_ops
                    ops = []
                    for _ in range(rng.randint(0, 3)):
                        ot = rng.choice(op_types)
                        if ot == "brain_damage":
                            ops.append({"op": "brain_damage", "change": rng.randint(1, 10), "edgerunner": "V"})
                        elif ot == "program_deactivate":
                            ops.append({"op": "program_deactivate", "program_name": "Sword"})
                        elif ot == "rez_damage":
                            ops.append({"op": "rez_damage", "target": "Hellhound",
                                         "damage": rng.choice([1, 5, 100, "x", None])})
                        elif ot == "tar_consumed":
                            ops.append({"op": "tar_consumed"})
                        else:
                            ops.append(rng.choice(ATOMS))

                    cpred_apply_hack_state(hs, ti, resolver_state_ops=ops, game_state=gs)
                # Must survive injection
                result = cpred_build_hack_injection(hs)
                self.assertIsInstance(result, str)
                # Must be JSON-serializable
                json.dumps(hs)
        finally:
            logging.disable(logging.NOTSET)

    def test_net_combat_fuzz_500_iterations(self):
        logging.disable(logging.CRITICAL)
        try:
            rng = random.Random(7002)
            op_types = ["brain_damage", "program_deactivate", "rez_damage", "tar_consumed", "garbage"]
            for _ in range(500):
                nc = cpred_init_net_combat_state(netrunner_name="V", target="T", sr=3)
                ps = _minimal_pipeline_state(nc)
                ps["game_state"] = _game_state_with_hp("V", rng.randint(1, 40), 40)
                for _ in range(rng.randint(3, 8)):
                    ti = {}
                    if rng.random() < 0.7:
                        ti["hack_state"] = {}
                        hs_report = ti["hack_state"]
                        if rng.random() < 0.5:
                            hs_report["alert_level"] = rng.choice([0, 3, 5, 7, "x", None])
                        if rng.random() < 0.3:
                            hs_report["tar_stacks"] = rng.choice([0, 1, "x", None])
                        if rng.random() < 0.3:
                            hs_report["ice_status"] = rng.choice([
                                {}, None, "bad",
                                {"GW": {"name": "Trace", "behavior": "trace", "status": "active",
                                         "rez_current": 6, "rez_max": 6}},
                            ])
                    ti["combat_complete"] = rng.choice([True, False, None])
                    ti["net_complete"] = rng.choice([True, False, None])

                    ops = []
                    for _ in range(rng.randint(0, 3)):
                        ot = rng.choice(op_types)
                        if ot == "brain_damage":
                            ops.append({"op": "brain_damage", "change": rng.randint(1, 10), "edgerunner": "V"})
                        elif ot == "rez_damage":
                            ops.append({"op": "rez_damage", "target": "Hellhound",
                                         "damage": rng.choice([1, 5, "x"])})
                        elif ot == "tar_consumed":
                            ops.append({"op": "tar_consumed"})
                        else:
                            ops.append(rng.choice(ATOMS))

                    cpred_apply_net_combat_state(ps, ti,
                                                 game_state=ps["game_state"],
                                                 resolver_state_ops=ops)
                # Must survive injection
                nc_state = ps.get("net_combat", {})
                combat = ps.get("combat")
                result = cpred_build_net_combat_injection(combat, nc_state, ps)
                self.assertIsInstance(result, str)
                json.dumps(nc_state)
        finally:
            logging.disable(logging.NOTSET)


# ===========================================================================
# 18. init_net_combat_from_hack: _prev_alert_level carried over
# ===========================================================================
class TestInitNetCombatPrevAlert(unittest.TestCase):
    def test_prev_alert_level_from_hack(self):
        hs = cpred_init_hack_state(tier="full_run")
        hs["alert_level"] = 4
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["_prev_alert_level"], 4)

    def test_prev_alert_level_default(self):
        hs = cpred_init_hack_state(tier="full_run")
        nc = cpred_init_net_combat_from_hack(hs)
        self.assertEqual(nc["_prev_alert_level"], 0)


if __name__ == "__main__":
    unittest.main()
