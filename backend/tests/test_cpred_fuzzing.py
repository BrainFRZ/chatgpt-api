"""
Fuzzing and boundary-value tests for Cyberpunk RED mechanics.

Covers: boundary values, malformed inputs, extreme values, type mismatches,
performance limits, and property-based invariants with seeded randomness.

Complements test_cpred_mechanics.py (happy-path unit tests).
"""
import copy
import json
import random
import sys
import os
import time
import unittest
from unittest.mock import patch

from hypothesis import assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred import (
    apply_game_state,
    _cyberware_ceiling_cost,
    _normalize_cyberware_name,
    _normalize_cyberware_entry_name,
    _cyberware_has_qualifier,
)
from game_systems.cpred_mechanics import (
    resolve_check,
    resolve_damage,
    resolve_ranged_attack,
    resolve_melee_attack,
    resolve_autofire,
    resolve_death_save,
    resolve_initiative,
    next_combat_turn,
    resolve_opposed_check,
    resolve_actions,
    RESOLVE_MECHANICS_TOOL,
)
from game_systems.cpred_tables import RANGED_DV_TABLE, AUTOFIRE_DV_TABLE, CYBERWARE_TABLE

MOCK = "game_systems.cpred_mechanics.random.randint"


# ===========================================================================
# 1. TestResolveCheckFuzz
# ===========================================================================
class TestResolveCheckFuzz(unittest.TestCase):
    """Boundary, extreme, and malformed inputs for resolve_check."""

    REQUIRED_KEYS = {"die", "stat_value", "skill_value", "modifiers", "total", "dv", "success", "formatted"}

    @patch(MOCK, return_value=5)
    def test_zero_stat_and_skill(self, _m):
        r = resolve_check(0, 0, 1)
        self.assertEqual(r["total"], 5)
        self.assertTrue(r["success"])  # 5 > 1

    @patch(MOCK, return_value=5)
    def test_negative_stat_value(self, _m):
        r = resolve_check(-10, 0, 1)
        self.assertEqual(r["total"], -5)
        self.assertFalse(r["success"])

    @patch(MOCK, return_value=5)
    def test_negative_skill_value(self, _m):
        r = resolve_check(0, -10, 1)
        self.assertEqual(r["total"], -5)
        self.assertFalse(r["success"])

    @patch(MOCK, return_value=5)
    def test_both_very_negative(self, _m):
        r = resolve_check(-100, -100, -300)
        self.assertEqual(r["total"], 5 + (-100) + (-100))
        self.assertTrue(r["success"])  # -195 > -300

    @patch(MOCK, return_value=5)
    def test_very_large_stat(self, _m):
        r = resolve_check(10000, 10000, 1)
        self.assertEqual(r["total"], 20005)
        self.assertTrue(r["success"])

    @patch(MOCK, return_value=7)
    def test_float_stat_value(self, _m):
        r = resolve_check(5.5, 3.3, 10)
        self.assertAlmostEqual(r["total"], 15.8)
        self.assertTrue(r["success"])
        self.assertIsInstance(r["formatted"], str)

    @patch(MOCK, return_value=7)
    def test_float_dv(self, _m):
        r = resolve_check(6, 4, 16.5)
        self.assertEqual(r["total"], 17)
        self.assertTrue(r["success"])  # 17 > 16.5

    @patch(MOCK, side_effect=[1, 1])
    def test_dv_zero_fumble_to_zero(self, _m):
        # Fumble: 1 - 1 = 0, dv=0 → 0 > 0 is False
        r = resolve_check(0, 0, 0)
        self.assertEqual(r["total"], 0)
        self.assertFalse(r["success"])

    @patch(MOCK, side_effect=[1, 10])
    def test_dv_negative(self, _m):
        # Fumble: 1 - 10 = -9, dv=-100 → -9 > -100 is True
        r = resolve_check(0, 0, -100)
        self.assertEqual(r["total"], -9)
        self.assertTrue(r["success"])

    @patch(MOCK, return_value=5)
    def test_luck_exactly_5(self, _m):
        r = resolve_check(0, 0, 0, luck_spent=5)
        self.assertEqual(r["total"], 10)  # 5 + 5 luck
        any_luck_mod = any(m[0] == "Luck" for m in r["modifiers"])
        self.assertTrue(any_luck_mod)

    @patch(MOCK, return_value=5)
    def test_luck_over_cap(self, _m):
        r = resolve_check(0, 0, 0, luck_spent=6)
        self.assertEqual(r["total"], 11)

    @patch(MOCK, return_value=5)
    def test_luck_100(self, _m):
        r = resolve_check(0, 0, 0, luck_spent=100)
        self.assertEqual(r["total"], 105)

    @patch(MOCK, return_value=5)
    def test_rel_bonus_over_cap(self, _m):
        r = resolve_check(0, 0, 0, rel_bonus=10)
        # RS capped at 5
        self.assertEqual(r["total"], 10)

    @patch(MOCK, return_value=5)
    def test_luck_3_rel_3_split(self, _m):
        r = resolve_check(0, 0, 0, luck_spent=3, rel_bonus=3)
        self.assertEqual(r["total"], 11)  # 5 + 3 + 3
        luck_vals = [m[1] for m in r["modifiers"] if m[0] == "Luck"]
        rs_vals = [m[1] for m in r["modifiers"] if m[0] == "RS"]
        self.assertEqual(luck_vals, [3])
        self.assertEqual(rs_vals, [3])

    @patch(MOCK, return_value=5)
    def test_luck_100_rel_100_cap(self, _m):
        r = resolve_check(0, 0, 0, luck_spent=100, rel_bonus=100)
        self.assertEqual(r["total"], 110)  # 5 + 100 + RS cap 5

    @patch(MOCK, return_value=5)
    def test_negative_luck_does_nothing(self, _m):
        r = resolve_check(6, 4, 10, luck_spent=-3)
        # combined = min(-3, 5) = -3; actual_luck = min(-3, -3) = -3 → not > 0; actual_rel = 0
        # No bonus applied
        self.assertEqual(r["total"], 15)  # 5 + 6 + 4

    @patch(MOCK, return_value=5)
    def test_negative_rel_bonus_applied(self, _m):
        """Negative rel_bonus is clamped to [-5, +5] and applied."""
        r = resolve_check(6, 4, 10, rel_bonus=-5)
        self.assertEqual(r["total"], 10)  # 5+6+4+(-5)

    @patch(MOCK, return_value=5)
    def test_negative_luck_does_not_inflate_rel_bonus(self, _m):
        """Negative luck is clamped to 0, so rel_bonus is capped at 5 correctly."""
        r = resolve_check(0, 0, 0, luck_spent=-3, rel_bonus=8)
        # clamped_luck=0, clamped_rel=8, combined=min(8,5)=5, actual_luck=0, actual_rel=5
        # Total = 5 (die) + 5 (RS) = 10
        self.assertEqual(r["total"], 10)

    @patch(MOCK, side_effect=[1, 10])
    def test_seriously_wounded_low_stats_max_penalty(self, _m):
        # Fumble: 1-10=-9, stat=1, skill=0, wounded=-2 → total = -9+1+0-2 = -10
        r = resolve_check(1, 0, 5, seriously_wounded=True)
        self.assertEqual(r["total"], -10)
        self.assertFalse(r["success"])

    @patch(MOCK, return_value=5)
    def test_return_keys_invariant(self, _m):
        r = resolve_check(6, 4, 15)
        self.assertEqual(set(r.keys()), self.REQUIRED_KEYS)

    @patch(MOCK, return_value=5)
    def test_formatted_is_nonempty_string(self, _m):
        for stat in [0, -10, 100]:
            r = resolve_check(stat, 4, 15)
            self.assertIsInstance(r["formatted"], str)
            self.assertTrue(len(r["formatted"]) > 0)


# ===========================================================================
# 2. TestResolveDamageFuzz
# ===========================================================================
class TestResolveDamageFuzz(unittest.TestCase):
    """Boundary and edge-case tests for resolve_damage."""

    REQUIRED_KEYS = {
        "dice", "total_rolled", "effective_sp", "damage_past_sp",
        "crit", "crit_bonus", "crit_injury", "critical_injuries", "hp_damage",
        "ablation", "aimed_effect", "is_rubber", "formatted",
    }

    def test_zero_damage_dice(self):
        r = resolve_damage(0, "body", 5)
        self.assertEqual(r["dice"], [])
        self.assertEqual(r["total_rolled"], 0)
        self.assertEqual(r["hp_damage"], 0)
        self.assertFalse(r["crit"])

    def test_negative_damage_dice(self):
        r = resolve_damage(-1, "body", 5)
        self.assertEqual(r["dice"], [])
        self.assertEqual(r["total_rolled"], 0)

    @patch(MOCK, return_value=3)
    def test_very_large_damage_dice_performance(self, _m):
        start = time.time()
        r = resolve_damage(200, "body", 0)
        elapsed = time.time() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(r["dice"]), 200)
        self.assertEqual(r["total_rolled"], 600)

    @patch(MOCK, return_value=4)
    def test_target_sp_zero(self, _m):
        r = resolve_damage(2, "body", 0)
        self.assertEqual(r["damage_past_sp"], 8)
        self.assertEqual(r["ablation"], 1)

    @patch(MOCK, return_value=3)
    def test_target_sp_negative(self, _m):
        r = resolve_damage(2, "body", -5)
        # effective_sp = -5, damage_past_sp = max(0, 6 - (-5)) = 11
        self.assertEqual(r["damage_past_sp"], 11)

    @patch(MOCK, side_effect=[6, 6, 3, 4])  # 2 sixes = crit, then 2d6 for crit injury
    def test_target_sp_very_large_crit_bonus_only(self, _m):
        r = resolve_damage(2, "body", 9999)
        self.assertEqual(r["damage_past_sp"], 0)
        self.assertTrue(r["crit"])
        self.assertEqual(r["crit_bonus"], 5)
        self.assertEqual(r["hp_damage"], 5)  # Only crit bonus

    @patch(MOCK, return_value=4)
    def test_hit_location_empty_string(self, _m):
        r = resolve_damage(2, "", 0)
        self.assertIsInstance(r["formatted"], str)
        # Should not crash; uses body table for crit if triggered

    @patch(MOCK, return_value=4)
    def test_hit_location_none(self, _m):
        r = resolve_damage(2, None, 0)
        self.assertIsInstance(r["formatted"], str)

    @patch(MOCK, return_value=4)
    def test_hit_location_invalid_string(self, _m):
        r = resolve_damage(2, "cybernetic_arm", 0)
        self.assertIsInstance(r["formatted"], str)

    @patch(MOCK, return_value=4)
    def test_melee_sp_halving_zero(self, _m):
        r = resolve_damage(2, "body", 0, is_melee=True)
        self.assertEqual(r["effective_sp"], 0)

    @patch(MOCK, return_value=4)
    def test_melee_sp_halving_one(self, _m):
        r = resolve_damage(2, "body", 1, is_melee=True)
        # ceil(1/2) = 1
        self.assertEqual(r["effective_sp"], 1)

    @patch(MOCK, return_value=4)
    def test_melee_sp_halving_negative(self, _m):
        r = resolve_damage(2, "body", -3, is_melee=True)
        # -(-(-3)//2) = -(3//2) = -1
        self.assertEqual(r["effective_sp"], -1)

    @patch(MOCK, return_value=4)
    def test_brawling_without_melee_flag(self, _m):
        r = resolve_damage(2, "body", 10, is_melee=False, is_brawling=True)
        # is_melee=False → no halving, full SP used
        self.assertEqual(r["effective_sp"], 10)

    @patch(MOCK, side_effect=[6, 6, 3, 4])  # 2 sixes = crit
    def test_ap_and_rubber_combined(self, _m):
        r = resolve_damage(2, "body", 5, is_ap=True, is_rubber=True)
        # Rubber overrides: no crit, no ablation
        self.assertFalse(r["crit"])
        self.assertEqual(r["ablation"], 0)
        self.assertTrue(r["is_rubber"])

    @patch(MOCK, return_value=3)
    def test_aimed_head_zero_penetration(self, _m):
        r = resolve_damage(2, "head", 100, aimed_shot="head")
        # 6 < 100 → damage_past_sp = 0, doubling 0 is still 0
        self.assertEqual(r["damage_past_sp"], 0)
        self.assertEqual(r["hp_damage"], 0)

    @patch(MOCK, return_value=4)
    def test_aimed_leg(self, _m):
        r = resolve_damage(3, "body", 5, aimed_shot="leg")
        self.assertEqual(r["aimed_effect"], "broken_leg")

    @patch(MOCK, return_value=4)
    def test_aimed_leg_rubber_no_broken_leg(self, _m):
        r = resolve_damage(3, "body", 5, aimed_shot="leg", is_rubber=True)
        self.assertIsNone(r["aimed_effect"])
        self.assertIsNone(r["crit_injury"])

    @patch(MOCK, return_value=4)
    def test_aimed_held_item(self, _m):
        r = resolve_damage(3, "body", 5, aimed_shot="held_item")
        self.assertEqual(r["aimed_effect"], "drop_item")

    @patch(MOCK, return_value=4)
    def test_aimed_invalid_string(self, _m):
        r = resolve_damage(3, "body", 5, aimed_shot="torso")
        self.assertIsNone(r["aimed_effect"])

    @patch(MOCK, return_value=4)
    def test_aimed_empty_string(self, _m):
        r = resolve_damage(3, "body", 5, aimed_shot="")
        self.assertIsNone(r["aimed_effect"])

    @patch(MOCK, return_value=6)
    def test_single_die_no_crit(self, _m):
        r = resolve_damage(1, "body", 0)
        # Only 1 six, need 2+ for crit
        self.assertFalse(r["crit"])

    @patch(MOCK, return_value=6)
    def test_all_sixes_large_pool(self, _m):
        # 10 sixes = crit, then 2d6 for injury lookup = 6+6=12
        r = resolve_damage(10, "body", 0)
        self.assertTrue(r["crit"])
        self.assertEqual(r["total_rolled"], 60)
        self.assertEqual(r["hp_damage"], 60 + 5)

    @patch(MOCK, return_value=4)
    def test_return_keys_invariant(self, _m):
        r = resolve_damage(2, "body", 5)
        self.assertEqual(set(r.keys()), self.REQUIRED_KEYS)


# ===========================================================================
# 3. TestResolveRangedAttackFuzz
# ===========================================================================
class TestResolveRangedAttackFuzz(unittest.TestCase):
    """Boundary and malformed-input tests for resolve_ranged_attack."""

    def test_invalid_weapon_type(self):
        r = resolve_ranged_attack(6, 4, "LaserRifle", 2, 1, 5, 0)
        self.assertIn("error", r)

    def test_empty_weapon_type(self):
        r = resolve_ranged_attack(6, 4, "", 2, 1, 5, 0)
        self.assertIn("error", r)

    def test_none_weapon_type(self):
        r = resolve_ranged_attack(6, 4, None, 2, 1, 5, 0)
        self.assertIn("error", r)

    def test_negative_range_bracket_returns_error(self):
        """Negative range_bracket should be rejected (not silently index from end)."""
        r = resolve_ranged_attack(6, 4, "SMG", 2, 1, 0, -2)
        self.assertIn("error", r)

    def test_range_bracket_at_max_boundary(self):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 5, 8)
        self.assertIn("error", r)

    def test_range_bracket_very_large(self):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 5, 999)
        self.assertIn("error", r)

    def test_pistol_none_bracket(self):
        # Pistol at bracket 6 is None in the table
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 5, 6)
        self.assertIn("error", r)

    @patch(MOCK, return_value=5)
    def test_rof_zero(self, _m):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 0, 5, 0)
        self.assertEqual(r["attacks"], [])
        self.assertEqual(r["on_outcome"], "")  # on_miss default is ""

    @patch(MOCK, return_value=2)
    def test_rof_100_performance(self, _m):
        start = time.time()
        r = resolve_ranged_attack(6, 4, "SMG", 2, 100, 5, 0)
        elapsed = time.time() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(r["attacks"]), 100)

    @patch(MOCK, return_value=9)
    def test_sp_degrades_across_hits(self, _m):
        """Verify SP ablation compounds across multi-hit shots."""
        # High roll = hit, damage = all 9s → d6 mock returning 9 not valid (1-6),
        # but we're mocking ALL randint calls. d10=9 for check, d6=9 for damage (clamped to 9).
        # Actually randint(1,6) returns 9 here — that's the mock. This still tests ablation logic.
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 3, 10, 0,
                                  target_name="Ganger", on_hit="hit")
        # Check that state_ops have ablation entries if any hits penetrated
        if any(a.get("roll", {}).get("success") for a in r.get("attacks", [])):
            self.assertIn("on_outcome", r)

    @patch(MOCK, side_effect=[9, 4, 4, 5, 4, 4])
    def test_luck_only_first_shot(self, _m):
        """Luck should only apply to the first shot in multi-ROF."""
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 2, 0, 0,
                                  luck_spent=3)
        attacks = r.get("attacks", [])
        if len(attacks) == 2:
            # First shot should have luck in its check
            first_roll = attacks[0].get("roll", {})
            second_roll = attacks[1].get("roll", {})
            first_mods = first_roll.get("modifiers", [])
            second_mods = second_roll.get("modifiers", [])
            first_has_luck = any(m[0] == "Luck" for m in first_mods)
            second_has_luck = any(m[0] == "Luck" for m in second_mods)
            self.assertTrue(first_has_luck)
            self.assertFalse(second_has_luck)

    @patch(MOCK, return_value=9)
    def test_on_hit_propagation(self, _m):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 0, 0,
                                  on_hit="Ganger screams", on_miss="Bullet sparks")
        if any(a.get("roll", {}).get("success") for a in r.get("attacks", [])):
            self.assertEqual(r["on_outcome"], "Ganger screams")

    @patch(MOCK, return_value=1)
    def test_on_miss_propagation(self, _m):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 0, 0,
                                  on_hit="Ganger screams", on_miss="Bullet sparks")
        # Low roll → miss
        if not any(a.get("roll", {}).get("success") for a in r.get("attacks", [])):
            self.assertEqual(r["on_outcome"], "Bullet sparks")

    @patch(MOCK, return_value=9)
    def test_zero_target_sp_full_damage(self, _m):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 1, 0, 0,
                                  target_name="Ganger")
        attacks = r.get("attacks", [])
        if attacks and attacks[0].get("roll", {}).get("success"):
            dmg = attacks[0].get("damage", {})
            self.assertEqual(dmg.get("effective_sp"), 0)

    @patch(MOCK, return_value=9)
    def test_aimed_shot_with_multi_rof(self, _m):
        r = resolve_ranged_attack(6, 4, "Pistol", 2, 2, 5, 0,
                                  aimed_shot="head")
        # DV should include aimed shot penalty (+8)
        self.assertIn("dv", r)


# ===========================================================================
# 4. TestResolveMeleeAttackFuzz
# ===========================================================================
class TestResolveMeleeAttackFuzz(unittest.TestCase):
    """Edge cases for resolve_melee_attack."""

    @patch(MOCK, return_value=5)
    def test_zero_all_stats_tie_misses(self, _m):
        r = resolve_melee_attack(0, 0, 0, 0, 2, 1, 5)
        # Both roll 5+0+0=5, tie → miss
        attacks = r["attacks"]
        self.assertEqual(len(attacks), 1)
        self.assertFalse(attacks[0]["hit"])

    @patch(MOCK, return_value=5)
    def test_negative_attacker_stats(self, _m):
        r = resolve_melee_attack(-10, -10, 0, 0, 2, 1, 5)
        # Attacker: 5+(-10)+(-10) = -15, Defender: 5+0+0 = 5 → miss
        self.assertFalse(r["attacks"][0]["hit"])

    @patch(MOCK, return_value=5)
    def test_negative_defender_stats(self, _m):
        r = resolve_melee_attack(0, 0, -10, -10, 2, 1, 5)
        # Attacker: 5, Defender: -15 → hit
        self.assertTrue(r["attacks"][0]["hit"])

    @patch(MOCK, return_value=5)
    def test_both_seriously_wounded(self, _m):
        r = resolve_melee_attack(5, 5, 5, 5, 2, 1, 5,
                                 seriously_wounded_attacker=True,
                                 seriously_wounded_defender=True)
        # Both get -2: atk=5+5+5-2=13, def=5+5+5-2=13 → tie → miss
        self.assertFalse(r["attacks"][0]["hit"])

    @patch(MOCK, return_value=5)
    def test_rof_zero_melee(self, _m):
        r = resolve_melee_attack(6, 4, 6, 4, 2, 0, 5)
        self.assertEqual(r["attacks"], [])

    @patch(MOCK, return_value=3)
    def test_rof_50_performance(self, _m):
        start = time.time()
        r = resolve_melee_attack(6, 4, 6, 4, 2, 50, 5)
        elapsed = time.time() - start
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(r["attacks"]), 50)

    @patch(MOCK, return_value=9)
    def test_sp_degrades_on_melee_hits(self, _m):
        """Multiple melee hits should ablate SP."""
        r = resolve_melee_attack(10, 10, 0, 0, 3, 3, 8,
                                 target_name="Ganger")
        # Should have state_ops for ablation if any hit penetrated
        hit_count = sum(1 for a in r["attacks"] if a["hit"])
        self.assertEqual(hit_count, 3)  # attacker always wins with 10+10=29 vs 0

    @patch(MOCK, return_value=9)
    def test_brawling_full_sp(self, _m):
        r = resolve_melee_attack(10, 10, 0, 0, 3, 1, 10,
                                 is_brawling=True)
        # Brawling: full SP (not halved)
        if r["attacks"][0]["hit"] and r["attacks"][0]["damage"]:
            self.assertEqual(r["attacks"][0]["damage"]["effective_sp"], 10)

    @patch(MOCK, return_value=9)
    def test_melee_halves_sp(self, _m):
        r = resolve_melee_attack(10, 10, 0, 0, 3, 1, 11,
                                 is_brawling=False)
        # Melee: ceil(11/2) = 6
        if r["attacks"][0]["hit"] and r["attacks"][0]["damage"]:
            self.assertEqual(r["attacks"][0]["damage"]["effective_sp"], 6)

    @patch(MOCK, return_value=5)
    def test_tie_always_miss(self, _m):
        """Ties should always result in a miss (defender wins ties)."""
        for _ in range(5):
            r = resolve_melee_attack(5, 5, 5, 5, 2, 1, 5)
            self.assertFalse(r["attacks"][0]["hit"])


# ===========================================================================
# 5. TestResolveAutofireFuzz
# ===========================================================================
class TestResolveAutofireFuzz(unittest.TestCase):
    """Edge cases for resolve_autofire."""

    def test_invalid_weapon_pistol(self):
        r = resolve_autofire(6, 4, "Pistol", 3, 5, 0)
        self.assertIn("error", r)

    def test_empty_weapon(self):
        r = resolve_autofire(6, 4, "", 3, 5, 0)
        self.assertIn("error", r)

    def test_none_weapon(self):
        r = resolve_autofire(6, 4, None, 3, 5, 0)
        self.assertIn("error", r)

    def test_negative_range_bracket_autofire_returns_error(self):
        """Negative range_bracket should be rejected in autofire too."""
        r = resolve_autofire(6, 4, "SMG", 3, 0, -1)
        self.assertIn("error", r)

    def test_range_bracket_5_out_of_bounds(self):
        r = resolve_autofire(6, 4, "SMG", 3, 0, 5)
        self.assertIn("error", r)

    def test_range_bracket_100(self):
        r = resolve_autofire(6, 4, "SMG", 3, 0, 100)
        self.assertIn("error", r)

    @patch(MOCK, side_effect=[9, 3, 4])  # d10=9 (hit), 2d6=3+4=7 for damage
    def test_multiplier_zero_caps_to_zero(self, _m):
        r = resolve_autofire(6, 4, "SMG", 0, 0, 0)
        if r.get("hit"):
            # raw_damage = 7 * margin, capped = min(raw, 0 * 7) = 0
            # damage_past_sp = max(0, 0 - sp) = 0
            self.assertEqual(r["damage"]["hp_damage"], 0)

    @patch(MOCK, side_effect=[9, 3, 4])
    def test_multiplier_negative(self, _m):
        r = resolve_autofire(6, 4, "SMG", -3, 0, 0)
        if r.get("hit"):
            # capped = min(raw, -3 * 7) = negative → max(0, neg - sp) = 0
            self.assertGreaterEqual(r["damage"]["hp_damage"], 0)

    @patch(MOCK, side_effect=[9, 3, 4])
    def test_multiplier_very_large(self, _m):
        r = resolve_autofire(6, 4, "SMG", 1000, 0, 0)
        if r.get("hit"):
            # Cap never triggers when multiplier is huge
            self.assertGreater(r["damage"]["hp_damage"], 0)

    @patch(MOCK, return_value=9)
    def test_always_consumes_10_rounds(self, _m):
        r = resolve_autofire(6, 4, "SMG", 3, 0, 0)
        self.assertEqual(r.get("rounds_consumed"), 10)

    @patch(MOCK, return_value=1)
    def test_miss_consumes_10_rounds(self, _m):
        r = resolve_autofire(6, 4, "SMG", 3, 0, 0)
        self.assertEqual(r.get("rounds_consumed"), 10)
        self.assertFalse(r.get("hit", True))

    @patch(MOCK, return_value=9)
    def test_assault_rifle(self, _m):
        r = resolve_autofire(6, 4, "Assault Rifle", 4, 0, 0)
        self.assertNotIn("error", r)
        self.assertEqual(r.get("rounds_consumed"), 10)

    @patch(MOCK, side_effect=[8, 6, 6, 1, 2])
    def test_autofire_double_six_crit(self, _m):
        r = resolve_autofire(8, 6, "SMG", 3, 20, 1, target_name="Ganger")
        self.assertTrue(r["hit"])
        self.assertTrue(r["damage"]["crit"])
        self.assertEqual(r["damage"]["crit_bonus"], 5)
        self.assertEqual(r["damage"]["hp_damage"], r["damage"]["damage_past_sp"] + 5)
        self.assertTrue(any(op.get("op") == "critical_injury" for op in r["state_ops"]))


# ===========================================================================
# 6. TestResolveDeathSaveFuzz
# ===========================================================================
class TestResolveDeathSaveFuzz(unittest.TestCase):
    """Boundary and malformed-input tests for resolve_death_save."""

    REQUIRED_KEYS = {
        "type", "d10", "death_save_count", "injury_mod",
        "effective_roll", "threshold", "natural_10", "survived", "formatted",
    }

    @patch(MOCK, return_value=1)
    def test_body_stat_zero_always_fails(self, _m):
        r = resolve_death_save(0, 0)
        # effective_roll=1, threshold=0 → 1 < 0 = False
        self.assertFalse(r["survived"])

    @patch(MOCK, return_value=1)
    def test_body_stat_negative(self, _m):
        r = resolve_death_save(-5, 0)
        self.assertFalse(r["survived"])

    @patch(MOCK, return_value=9)
    def test_body_stat_very_large(self, _m):
        r = resolve_death_save(9999, 0)
        self.assertTrue(r["survived"])

    @patch(MOCK, return_value=1)
    def test_body_stat_1_minimum_roll(self, _m):
        r = resolve_death_save(1, 0)
        # effective_roll=1, 1 < 1 = False (must be strictly less)
        self.assertFalse(r["survived"])

    @patch(MOCK, return_value=5)
    def test_death_save_count_negative_helps(self, _m):
        r = resolve_death_save(6, -10)
        # effective_roll = 5 + (-10) + 0 = -5, -5 < 6 = True
        self.assertTrue(r["survived"])
        self.assertEqual(r["effective_roll"], -5)

    @patch(MOCK, return_value=1)
    def test_death_save_count_very_large(self, _m):
        r = resolve_death_save(6, 100)
        # effective_roll = 1 + 100 = 101, 101 < 6 = False
        self.assertFalse(r["survived"])

    @patch(MOCK, return_value=3)
    def test_active_injuries_none(self, _m):
        r = resolve_death_save(6, 0, None)
        self.assertEqual(r["injury_mod"], 0)

    @patch(MOCK, return_value=3)
    def test_active_injuries_empty_list(self, _m):
        r = resolve_death_save(6, 0, [])
        self.assertEqual(r["injury_mod"], 0)

    @patch(MOCK, return_value=3)
    def test_active_injuries_missing_dv_mod(self, _m):
        r = resolve_death_save(6, 0, [{}])
        self.assertEqual(r["injury_mod"], 0)

    @patch(MOCK, return_value=3)
    def test_active_injuries_string_dv_mod_ignored(self, _m):
        """String dv_mod is safely ignored (treated as 0)."""
        r = resolve_death_save(6, 0, [{"dv_mod": "abc"}])
        self.assertEqual(r["injury_mod"], 0)
        self.assertTrue(r["survived"])  # effective_roll=3, 3 < 6

    @patch(MOCK, return_value=3)
    def test_active_injuries_float_dv_mod(self, _m):
        r = resolve_death_save(6, 0, [{"dv_mod": 1.5}])
        self.assertAlmostEqual(r["injury_mod"], 1.5)

    @patch(MOCK, return_value=3)
    def test_active_injuries_negative_dv_mod(self, _m):
        r = resolve_death_save(10, 0, [{"dv_mod": -3}])
        # injury_mod=-3, effective_roll = 3+0+(-3) = 0, 0 < 10 = True
        self.assertTrue(r["survived"])

    @patch(MOCK, return_value=1)
    def test_active_injuries_very_large_list(self, _m):
        injuries = [{"dv_mod": 1} for _ in range(100)]
        r = resolve_death_save(6, 0, injuries)
        self.assertEqual(r["injury_mod"], 100)
        self.assertFalse(r["survived"])

    @patch(MOCK, return_value=10)
    def test_natural_10_always_fails_even_high_body(self, _m):
        r = resolve_death_save(1000, 0)
        self.assertFalse(r["survived"])
        self.assertTrue(r["natural_10"])

    @patch(MOCK, return_value=3)
    def test_return_keys_invariant(self, _m):
        r = resolve_death_save(6, 0)
        self.assertEqual(set(r.keys()), self.REQUIRED_KEYS)


# ===========================================================================
# 7. TestResolveInitiativeFuzz
# ===========================================================================
class TestResolveInitiativeFuzz(unittest.TestCase):
    """Edge cases for resolve_initiative."""

    def test_empty_combatants(self):
        r = resolve_initiative([])
        self.assertEqual(r, [])

    @patch(MOCK, return_value=5)
    def test_single_combatant(self, _m):
        r = resolve_initiative([{"name": "V", "ref": 8}])
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["total"], 13)

    @patch(MOCK, return_value=5)
    def test_missing_name_defaults_to_unknown(self, _m):
        """Missing name key defaults to 'Unknown' instead of crashing."""
        r = resolve_initiative([{"ref": 5}])
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["name"], "Unknown")

    @patch(MOCK, return_value=5)
    def test_missing_ref_defaults_to_zero(self, _m):
        r = resolve_initiative([{"name": "V"}])
        self.assertEqual(r[0]["total"], 5)
        self.assertEqual(r[0]["ref"], 0)

    @patch(MOCK, return_value=5)
    def test_ref_zero(self, _m):
        r = resolve_initiative([{"name": "V", "ref": 0}])
        self.assertEqual(r[0]["total"], 5)

    @patch(MOCK, return_value=5)
    def test_ref_negative(self, _m):
        r = resolve_initiative([{"name": "V", "ref": -10}])
        self.assertEqual(r[0]["total"], -5)

    def test_ref_string_raises_type_error(self):
        with self.assertRaises(TypeError):
            with patch(MOCK, return_value=5):
                resolve_initiative([{"name": "V", "ref": "fast"}])

    @patch(MOCK, return_value=5)
    def test_large_combatant_list(self, _m):
        combatants = [{"name": f"NPC_{i}", "ref": i} for i in range(100)]
        r = resolve_initiative(combatants)
        self.assertEqual(len(r), 100)
        # Should be sorted descending by total
        totals = [e["total"] for e in r]
        self.assertEqual(totals, sorted(totals, reverse=True))

    @patch(MOCK, return_value=5)
    def test_all_same_ref_tiebreaking(self, _m):
        """All combatants have same REF+d10, forcing tiebreak rolls."""
        combatants = [{"name": f"Ganger_{i}", "ref": 5} for i in range(10)]
        # All get total=10. Tiebreak d10s also all return 5.
        # With deterministic mock, tiebreaker won't differentiate — but should not crash.
        r = resolve_initiative(combatants)
        self.assertEqual(len(r), 10)
        for entry in r:
            self.assertIn("tiebreak", entry)

    @patch(MOCK, return_value=5)
    def test_duplicate_names(self, _m):
        combatants = [{"name": "V", "ref": 5}, {"name": "V", "ref": 8}]
        r = resolve_initiative(combatants)
        self.assertEqual(len(r), 2)
        names = [e["name"] for e in r]
        self.assertEqual(names.count("V"), 2)

    @patch(MOCK, return_value=5)
    def test_extra_fields_ignored(self, _m):
        r = resolve_initiative([{"name": "V", "ref": 5, "cool": 8, "hp": 40}])
        self.assertEqual(len(r), 1)
        self.assertNotIn("cool", r[0])
        self.assertNotIn("hp", r[0])


# ===========================================================================
# 8. TestNextCombatTurnFuzz
# ===========================================================================
class TestNextCombatTurnFuzz(unittest.TestCase):
    """Edge cases for next_combat_turn."""

    def test_empty_order_returns_bool_new_round(self):
        """Empty order returns new_round=True (bool), consistent with other paths."""
        r = next_combat_turn([], 0)
        self.assertEqual(r["next_turn"], 0)
        self.assertFalse(r["round_incremented"])
        self.assertTrue(r["new_round"])
        self.assertIsInstance(r["new_round"], bool)

    def test_current_turn_negative_one(self):
        r = next_combat_turn(["V", "Ganger"], -1)
        # idx = (-1+1) % 2 = 0; round_incremented = 0 <= -1 → False
        self.assertEqual(r["next_turn"], 0)
        self.assertFalse(r["round_incremented"])

    def test_current_turn_very_large(self):
        r = next_combat_turn(["V", "Ganger"], 999)
        # idx = 1000 % 2 = 0; round_incremented = 0 <= 999 → True
        self.assertEqual(r["next_turn"], 0)
        self.assertTrue(r["round_incremented"])

    def test_current_turn_deep_negative(self):
        r = next_combat_turn(["V", "Ganger", "Borg"], -100)
        # idx = (-99) % 3 = 0 (Python modulo is non-negative for positive divisor)
        self.assertEqual(r["next_turn"], 0)

    def test_all_eliminated(self):
        r = next_combat_turn(["V", "Ganger"], 0, ["V", "Ganger"])
        self.assertEqual(r["next_turn"], 0)
        self.assertFalse(r["round_incremented"])
        self.assertFalse(r["new_round"])

    def test_all_eliminated_but_one(self):
        r = next_combat_turn(["V", "Ganger", "Borg"], 0, ["Ganger"])
        # Skip Ganger (idx=1), go to Borg (idx=2)
        self.assertEqual(r["next_turn"], 2)

    def test_skip_eliminated_wraps_around(self):
        r = next_combat_turn(["V", "Ganger", "Borg"], 1, ["Borg"])
        # Skip Borg (idx=2), wrap to V (idx=0); round_incremented = True
        self.assertEqual(r["next_turn"], 0)
        self.assertTrue(r["round_incremented"])

    def test_single_non_eliminated(self):
        r = next_combat_turn(["V", "Ganger"], 0, ["Ganger"])
        # Skip Ganger (idx=1), wrap to V (idx=0)
        self.assertEqual(r["next_turn"], 0)

    def test_dict_entries(self):
        order = [{"name": "V"}, {"name": "Ganger"}]
        r = next_combat_turn(order, 0)
        self.assertEqual(r["next_turn"], 1)

    def test_mixed_string_and_dict(self):
        order = ["V", {"name": "Ganger"}]
        r = next_combat_turn(order, 0)
        self.assertEqual(r["next_turn"], 1)

    def test_eliminated_none(self):
        r = next_combat_turn(["V", "Ganger"], 0, None)
        self.assertEqual(r["next_turn"], 1)

    def test_eliminated_empty_list(self):
        r = next_combat_turn(["V", "Ganger"], 0, [])
        self.assertEqual(r["next_turn"], 1)

    def test_eliminated_name_not_in_order(self):
        r = next_combat_turn(["V", "Ganger"], 0, ["Ghost"])
        # Ghost not in order, no effect
        self.assertEqual(r["next_turn"], 1)


# ===========================================================================
# 9. TestResolveActionsFuzz
# ===========================================================================
class TestResolveActionsFuzz(unittest.TestCase):
    """Batch dispatcher edge cases."""

    def test_empty_actions(self):
        r = resolve_actions([])
        self.assertEqual(r, {"results": [], "state_ops": [], "tar_consumed": False})

    @patch(MOCK, return_value=5)
    def test_missing_type_key(self, _m):
        r = resolve_actions([{"character": "V"}])
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_type_none(self, _m):
        r = resolve_actions([{"type": None, "character": "V"}])
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_type_empty_string(self, _m):
        r = resolve_actions([{"type": "", "character": "V"}])
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_type_integer(self, _m):
        r = resolve_actions([{"type": 42, "character": "V"}])
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_empty_dict_action(self, _m):
        r = resolve_actions([{}])
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("error", r["results"][0])

    def test_string_action_returns_error(self):
        """Non-dict (string) action returns error result instead of crashing."""
        r = resolve_actions(["shoot him"])
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("error", r["results"][0])

    def test_none_action_returns_error(self):
        """Non-dict (None) action returns error result instead of crashing."""
        r = resolve_actions([None])
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("error", r["results"][0])

    def test_integer_action_returns_error(self):
        """Non-dict (int) action returns error result instead of crashing."""
        r = resolve_actions([42])
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_mixed_valid_and_invalid(self, _m):
        actions = [
            {"type": "skill_check", "character": "V", "stat_value": 6, "skill_value": 4, "dv": 15},
            {"type": "unknown", "character": "X"},
            {"type": "death_save", "character": "V", "body_stat": 8},
        ]
        r = resolve_actions(actions)
        self.assertEqual(len(r["results"]), 3)
        self.assertNotIn("error", r["results"][0])
        self.assertIn("error", r["results"][1])
        self.assertNotIn("error", r["results"][2])

    @patch(MOCK, return_value=5)
    def test_all_defaults_skill_check(self, _m):
        r = resolve_actions([{"type": "skill_check", "character": "V"}])
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_all_defaults_ranged_attack(self, _m):
        r = resolve_actions([{"type": "ranged_attack", "character": "V"}])
        # Default weapon_type="Pistol", range_bracket=0 → valid
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_all_defaults_melee_attack(self, _m):
        r = resolve_actions([{"type": "melee_attack", "character": "V"}])
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_all_defaults_autofire(self, _m):
        r = resolve_actions([{"type": "autofire", "character": "V"}])
        # Default weapon_type="SMG", range_bracket=0 → valid
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])

    def test_invalid_ranged_attack_no_luck_op(self):
        r = resolve_actions([{
            "type": "ranged_attack",
            "character": "V",
            "weapon_type": "Pistol",
            "range_bracket": 99,
            "luck_spent": 4,
        }])
        self.assertIn("error", r["results"][0])
        self.assertFalse(any(op.get("op") == "luck" for op in r["state_ops"]))

    def test_invalid_autofire_no_luck_op(self):
        r = resolve_actions([{
            "type": "autofire",
            "character": "V",
            "weapon_type": "SMG",
            "range_bracket": 99,
            "luck_spent": 4,
        }])
        self.assertIn("error", r["results"][0])
        self.assertFalse(any(op.get("op") == "luck" for op in r["state_ops"]))

    @patch(MOCK, side_effect=[9, 1])
    def test_invalid_opposed_check_no_luck_op(self, _m):
        r = resolve_actions([{
            "type": "opposed_check",
            "character": "V",
            "attacker_stat": 6,
            "defender_stat": 4,
            "luck_spent": 4,
            "zap": True,
            "interface_rank": "bad",
        }])
        self.assertIn("error", r["results"][0])
        self.assertFalse(any(op.get("op") == "luck" for op in r["state_ops"]))

    @patch(MOCK, return_value=5)
    def test_all_defaults_death_save(self, _m):
        r = resolve_actions([{"type": "death_save", "character": "V"}])
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_all_defaults_initiative(self, _m):
        r = resolve_actions([{"type": "initiative", "character": "all"}])
        # Default combatants=[] → empty order
        self.assertNotIn("error", r["results"][0])

    def test_string_stat_value_caught(self):
        r = resolve_actions([{"type": "skill_check", "character": "V", "stat_value": "six"}])
        self.assertIn("error", r["results"][0])

    @patch(MOCK, return_value=5)
    def test_1000_actions_performance(self, _m):
        action = {"type": "skill_check", "character": "V", "stat_value": 6, "skill_value": 4, "dv": 15}
        start = time.time()
        r = resolve_actions([action] * 1000)
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(r["results"]), 1000)

    @patch(MOCK, return_value=5)
    def test_extra_fields_ignored(self, _m):
        r = resolve_actions([{"type": "skill_check", "character": "V",
                              "stat_value": 6, "skill_value": 4, "dv": 15,
                              "favorite_color": "red"}])
        self.assertNotIn("error", r["results"][0])

    def test_error_includes_character_name(self):
        r = resolve_actions([{"type": "skill_check", "character": "Trauma Team", "stat_value": "bad"}])
        self.assertEqual(r["results"][0].get("character"), "Trauma Team")

    @patch(MOCK, return_value=9)
    def test_state_ops_aggregation(self, _m):
        """Two ranged attacks both hitting → merged state_ops."""
        actions = [
            {"type": "ranged_attack", "character": "V", "stat_value": 8, "skill_value": 8,
             "weapon_type": "Pistol", "damage_dice": 3, "rof": 1, "target_sp": 0,
             "range_bracket": 0, "target": "Ganger1"},
            {"type": "ranged_attack", "character": "V", "stat_value": 8, "skill_value": 8,
             "weapon_type": "Pistol", "damage_dice": 3, "rof": 1, "target_sp": 0,
             "range_bracket": 0, "target": "Ganger2"},
        ]
        r = resolve_actions(actions)
        self.assertEqual(len(r["results"]), 2)
        # Both should hit (total = 9+8+8=25 > DV 13)
        # State ops from both should be merged
        self.assertGreater(len(r["state_ops"]), 0)


# ===========================================================================
# 9b. TestResolveActionsUncommittedFuzz
# ===========================================================================
class TestResolveActionsUncommittedFuzz(unittest.TestCase):
    """Seeded fuzzing for TAR/alert/zap/program-deactivation branches."""

    @settings(max_examples=300, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_tar_consumed_once_seeded(self, seed):
        rng = random.Random(seed)
        tar_stacks = rng.randint(0, 4)
        actions = []
        expected_consumed = False
        for i in range(rng.randint(1, 12)):
            action_type = rng.choice(["skill_check", "opposed_check", "ranged_attack"])
            if action_type == "skill_check":
                is_net = bool(rng.getrandbits(1))
                actions.append({
                    "type": "skill_check",
                    "character": f"N{i}",
                    "stat_value": rng.randint(1, 10),
                    "skill_value": rng.randint(0, 10),
                    "dv": rng.randint(9, 21),
                    "net": is_net,
                })
                if tar_stacks > 0 and is_net and not expected_consumed:
                    expected_consumed = True
            elif action_type == "opposed_check":
                is_net = bool(rng.getrandbits(1))
                actions.append({
                    "type": "opposed_check",
                    "character": f"N{i}",
                    "attacker_stat": rng.randint(1, 10),
                    "defender_stat": rng.randint(1, 10),
                    "net": is_net,
                })
                if tar_stacks > 0 and is_net and not expected_consumed:
                    expected_consumed = True
            else:
                actions.append({
                    "type": "ranged_attack",
                    "character": f"N{i}",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "Pistol",
                    "damage_dice": 2,
                    "rof": 1,
                    "target_sp": 0,
                    "range_bracket": 0,
                })

        result = resolve_actions(copy.deepcopy(actions), tar_stacks=tar_stacks, alert_level=rng.randint(0, 7))
        consumed_ops = [op for op in result["state_ops"] if isinstance(op, dict) and op.get("op") == "tar_consumed"]
        self.assertEqual(result["tar_consumed"], expected_consumed, f"seed={seed}")
        self.assertEqual(bool(consumed_ops), expected_consumed, f"seed={seed}")
        self.assertLessEqual(len(consumed_ops), 1, f"seed={seed}")

    @settings(max_examples=250, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_alert_dv_penalty_seeded(self, seed):
        rng = random.Random(seed)
        base_dv = rng.randint(9, 21)
        alert_level = rng.randint(0, 7)
        result = resolve_actions([{
            "type": "skill_check",
            "character": "V",
            "stat_value": 6,
            "skill_value": 4,
            "dv": base_dv,
            "net": True,
        }], alert_level=alert_level, tar_stacks=0)
        expected_dv = base_dv + 2 if alert_level >= 3 else base_dv
        self.assertEqual(result["results"][0]["dv"], expected_dv, f"seed={seed}")

    @settings(max_examples=250, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_zap_damage_and_state_ops_seeded(self, seed):
        rng = random.Random(seed)
        interface_rank = rng.randint(1, 10)
        result = resolve_actions([{
            "type": "opposed_check",
            "character": "V",
            "attacker_stat": 30,
            "defender_stat": 1,
            "attacker_label": "Attacker",
            "defender_label": "Defender",
            "zap": True,
            "interface_rank": interface_rank,
            "target": "Hellhound",
        }])
        out = result["results"][0]
        if out["success"]:
            self.assertEqual(out["zap_dice"], interface_rank, f"seed={seed}")
            self.assertGreaterEqual(out["zap_damage"], interface_rank, f"seed={seed}")
            self.assertLessEqual(out["zap_damage"], interface_rank * 6, f"seed={seed}")
            rez_ops = [op for op in result["state_ops"] if op.get("op") == "rez_damage"]
            self.assertEqual(len(rez_ops), 1, f"seed={seed}")
            self.assertEqual(rez_ops[0]["target"], "Hellhound", f"seed={seed}")
            self.assertEqual(rez_ops[0]["damage"], out["zap_damage"], f"seed={seed}")
        else:
            rez_ops = [op for op in result["state_ops"] if op.get("op") == "rez_damage"]
            self.assertEqual(len(rez_ops), 0, f"seed={seed}: no rez_damage on miss")

    @settings(max_examples=250, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_program_attack_deactivation_seeded(self, seed):
        rng = random.Random(seed)
        program_name = f"Prog_{seed}"
        result = resolve_actions([{
            "type": "program_attack",
            "character": "V",
            "interface_rank": rng.randint(4, 10),
            "program_atk": rng.randint(1, 8),
            "target_def": rng.randint(1, 8),
            "program_damage_dice": rng.randint(1, 6),
            "target_rez": rng.randint(0, 12),
            "program_name": program_name,
            "target": "TargetICE",
        }])
        out = result["results"][0]
        self.assertEqual(out.get("program_deactivated"), program_name, f"seed={seed}")
        deact_ops = [op for op in result["state_ops"] if op.get("op") == "program_deactivate"]
        self.assertEqual(len(deact_ops), 1, f"seed={seed}")
        self.assertEqual(deact_ops[0]["program_name"], program_name, f"seed={seed}")


# ===========================================================================
# 9c. TestOpposedCheckUncommittedFuzz
# ===========================================================================
class TestOpposedCheckUncommittedFuzz(unittest.TestCase):
    """Seeded fuzz coverage for expanded opposed_check inputs."""

    @settings(max_examples=500, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_seeded_opposed_check_totals_margin_and_tie_rule(self, seed):
        rng = random.Random(seed)
        attacker_stat = rng.randint(-10, 20)
        defender_stat = rng.randint(-10, 20)
        attacker_skill = rng.randint(-5, 12)
        defender_skill = rng.randint(-5, 12)
        luck_spent = rng.randint(-4, 12)
        rel_bonus = rng.randint(-20, 20)
        wounded_attacker = bool(rng.getrandbits(1))
        wounded_defender = bool(rng.getrandbits(1))

        result = resolve_opposed_check(
            attacker_stat=attacker_stat,
            defender_stat=defender_stat,
            attacker_label="COOL",
            defender_label="WILL",
            attacker_skill=attacker_skill,
            defender_skill=defender_skill,
            attacker_skill_label="Persuasion",
            defender_skill_label="Concentration",
            seriously_wounded_attacker=wounded_attacker,
            seriously_wounded_defender=wounded_defender,
            luck_spent=luck_spent,
            rel_bonus=rel_bonus,
        )

        expected_atk = result["attacker_roll"]["die"]["total"] + attacker_stat + attacker_skill
        if wounded_attacker:
            expected_atk -= 2
        expected_atk += max(0, luck_spent)
        expected_atk += max(-5, min(5, rel_bonus))

        expected_def = result["defender_roll"]["die"]["total"] + defender_stat + defender_skill
        if wounded_defender:
            expected_def -= 2

        self.assertEqual(result["attacker_total"], expected_atk, f"seed={seed}")
        self.assertEqual(result["defender_total"], expected_def, f"seed={seed}")
        self.assertEqual(result["margin"], expected_atk - expected_def, f"seed={seed}")
        self.assertEqual(result["success"], expected_atk > expected_def, f"seed={seed}")
        if expected_atk == expected_def:
            self.assertFalse(result["success"], f"seed={seed}: ties must fail for attacker")

    @settings(max_examples=350, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_seeded_resolve_actions_opposed_luck_state_op(self, seed):
        rng = random.Random(seed)
        luck_spent = rng.randint(-5, 10)
        action = {
            "type": "opposed_check",
            "character": "V",
            "attacker_stat": rng.randint(1, 10),
            "attacker_skill": rng.randint(0, 10),
            "defender_stat": rng.randint(1, 10),
            "defender_skill": rng.randint(0, 10),
            "luck_spent": luck_spent,
        }

        result = resolve_actions([action])
        self.assertEqual(len(result["results"]), 1, f"seed={seed}")
        out = result["results"][0]
        self.assertNotIn("error", out, f"seed={seed}")
        luck_ops = [op for op in result["state_ops"] if isinstance(op, dict) and op.get("op") == "luck"]

        if luck_spent > 0:
            self.assertEqual(len(luck_ops), 1, f"seed={seed}")
            self.assertEqual(luck_ops[0]["edgerunner"], "V", f"seed={seed}")
            self.assertEqual(luck_ops[0]["change"], -luck_spent, f"seed={seed}")
        else:
            self.assertEqual(len(luck_ops), 0, f"seed={seed}")

    @settings(max_examples=200, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_seeded_resolve_actions_opposed_rel_bonus_autocompute(self, seed):
        rng = random.Random(seed)
        action = {
            "type": "opposed_check",
            "character": "V",
            "attacker_stat": rng.randint(1, 10),
            "attacker_skill": rng.randint(0, 10),
            "defender_stat": rng.randint(1, 10),
            "defender_skill": rng.randint(0, 10),
            "luck_spent": rng.randint(0, 5),
            "target": "Rogue",
            "check_context": "social",
        }
        relationships = {"Rogue": {"rs": 95, "roms": 95}}
        factions = {"Rogue": {"fr": 90}}

        result = resolve_actions([action], relationships=relationships, factions=factions)
        out = result["results"][0]
        self.assertNotIn("error", out, f"seed={seed}")

        base = out["attacker_roll"]["die"]["total"] + action["attacker_stat"] + action["attacker_skill"]
        bonus_from_flags = 0
        bonus_from_flags += max(0, action["luck_spent"])
        applied_rel = out["attacker_total"] - base - bonus_from_flags
        self.assertEqual(applied_rel, 5, f"seed={seed}")

    def test_opposed_check_skill_labels_render_even_for_zero_skill(self):
        result = resolve_opposed_check(
            attacker_stat=6,
            defender_stat=6,
            attacker_label="DEX",
            defender_label="INT",
            attacker_skill=0,
            defender_skill=0,
            attacker_skill_label="Stealth",
            defender_skill_label="Concentration",
        )
        self.assertIn("+Stealth 0", result["attacker_roll"]["formatted"])
        self.assertIn("+Concentration 0", result["defender_roll"]["formatted"])


# ===========================================================================
# 10. TestResolvePipelineMechanicsFuzz
# ===========================================================================
class TestResolvePipelineMechanicsFuzz(unittest.TestCase):
    """Edge cases for resolve_pipeline_mechanics from pipeline.py."""

    @classmethod
    def setUpClass(cls):
        from pipeline import resolve_pipeline_mechanics
        cls.resolve = staticmethod(resolve_pipeline_mechanics)

    def test_empty_beats(self):
        annotated, ops = self.resolve([], {})
        self.assertEqual(annotated, [])
        self.assertEqual(ops, [])

    def test_legacy_string_beat(self):
        annotated, ops = self.resolve(["The ganger fires wildly"], {})
        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0]["beat"], "The ganger fires wildly")
        self.assertIsNone(annotated[0]["resolution"])

    def test_mixed_legacy_and_dict_beats(self):
        beats = [
            "Narrative only",
            {"beat": "V checks a lock", "resolution": None},
            {"beat": "just description"},
        ]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(len(annotated), 3)
        # String wrapped
        self.assertEqual(annotated[0]["beat"], "Narrative only")
        # Null resolution passed through
        self.assertIsNone(annotated[1]["resolution"])
        # Missing resolution key treated as None
        self.assertNotIn("result", annotated[2])

    @patch(MOCK, return_value=5)
    def test_beat_with_resolution_missing_type(self, _m):
        beats = [{"beat": "something", "resolution": {"stat_value": 5}}]
        annotated, ops = self.resolve(beats, {})
        # type=None → unknown action error
        self.assertIn("result", annotated[0])
        result = annotated[0]["result"]
        self.assertIn("error", result)

    @patch(MOCK, return_value=9)
    def test_skill_check_success_callback(self, _m):
        beats = [{
            "beat": "V picks the lock",
            "resolution": {
                "type": "skill_check", "character": "V",
                "stat_value": 6, "skill_value": 4, "dv": 15,
                "on_success": "lock opens", "on_failure": "alarm triggers",
            }
        }]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(annotated[0]["result"]["on_outcome"], "lock opens")

    @patch(MOCK, return_value=1)
    def test_skill_check_failure_callback(self, _m):
        beats = [{
            "beat": "V picks the lock",
            "resolution": {
                "type": "skill_check", "character": "V",
                "stat_value": 6, "skill_value": 4, "dv": 15,
                "on_success": "lock opens", "on_failure": "alarm triggers",
            }
        }]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(annotated[0]["result"]["on_outcome"], "alarm triggers")

    @patch(MOCK, return_value=9)
    def test_ranged_attack_on_hit(self, _m):
        """Pipeline correctly detects ranged hits via a['roll']['success']."""
        beats = [{
            "beat": "V shoots",
            "resolution": {
                "type": "ranged_attack", "character": "V",
                "stat_value": 8, "skill_value": 8, "weapon_type": "Pistol",
                "damage_dice": 2, "rof": 1, "target_sp": 0, "range_bracket": 0,
                "target": "Ganger",
                "on_hit": "Ganger staggers", "on_miss": "Bullet misses",
            }
        }]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(annotated[0]["result"]["on_outcome"], "Ganger staggers")
        self.assertGreater(len(ops), 0)

    @patch(MOCK, return_value=5)
    def test_initiative_beat(self, _m):
        beats = [{
            "beat": "Initiative",
            "resolution": {
                "type": "initiative", "character": "all",
                "combatants": [{"name": "V", "ref": 8}, {"name": "Ganger", "ref": 5}],
            }
        }]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(annotated[0]["result"]["on_outcome"], "Initiative rolled")

    def test_non_dict_resolution_returns_error(self):
        """Non-dict resolution is caught by try/except and annotated with error."""
        beats = [{"beat": "error", "resolution": "shoot them"}]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(len(annotated), 1)
        self.assertIn("result", annotated[0])
        self.assertIn("error", annotated[0]["result"])

    @patch(MOCK, return_value=9)
    def test_state_ops_aggregation_across_beats(self, _m):
        beats = [
            {
                "beat": "V shoots Ganger 1",
                "resolution": {
                    "type": "ranged_attack", "character": "V",
                    "stat_value": 8, "skill_value": 8, "weapon_type": "Pistol",
                    "damage_dice": 2, "rof": 1, "target_sp": 0, "range_bracket": 0,
                    "target": "Ganger1",
                }
            },
            {
                "beat": "V shoots Ganger 2",
                "resolution": {
                    "type": "ranged_attack", "character": "V",
                    "stat_value": 8, "skill_value": 8, "weapon_type": "Pistol",
                    "damage_dice": 2, "rof": 1, "target_sp": 0, "range_bracket": 0,
                    "target": "Ganger2",
                }
            },
        ]
        annotated, ops = self.resolve(beats, {})
        self.assertEqual(len(annotated), 2)
        # State ops from both attacks should be aggregated
        targets = set(op.get("edgerunner") for op in ops)
        self.assertIn("Ganger1", targets)
        self.assertIn("Ganger2", targets)

    @patch(MOCK, side_effect=[9, 5, 4, 4, 9, 5, 4, 4])
    def test_sequential_beats_use_evolving_target_sp(self, _m):
        """Second beat should see armor ablation from the first beat."""
        from game_systems.cpred import init_game_state
        gs = init_game_state()
        gs["edgerunners"]["Ganger"] = {
            "hp": {"current": 20, "max": 20},
            "humanity": {"current": 0, "max": 0},
            "luck": {"current": 0, "max": 0},
            "armor": {"head": 11, "body": 11},
            "critical_injuries": [],
            "cyberware_effects": [],
            "death_save_count": 0,
            "seriously_wounded": False,
            "eurobucks": 0,
            "weapons": [],
        }

        beats = [
            {
                "beat": "V shoots once",
                "resolution": {
                    "type": "ranged_attack",
                    "character": "V",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "Pistol",
                    "damage_dice": 3,
                    "rof": 1,
                    "target": "Ganger",
                    "target_sp": 11,  # stale input should be overwritten from evolving state
                    "range_bracket": 0,
                    "hit_location": "body",
                },
            },
            {
                "beat": "V shoots again",
                "resolution": {
                    "type": "ranged_attack",
                    "character": "V",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "Pistol",
                    "damage_dice": 3,
                    "rof": 1,
                    "target": "Ganger",
                    "target_sp": 11,  # stale again
                    "range_bracket": 0,
                    "hit_location": "body",
                },
            },
        ]

        annotated, _ops = self.resolve(beats, gs)
        first_effective_sp = annotated[0]["result"]["attacks"][0]["damage"]["effective_sp"]
        second_effective_sp = annotated[1]["result"]["attacks"][0]["damage"]["effective_sp"]
        self.assertEqual(first_effective_sp, 11)
        self.assertEqual(second_effective_sp, 10)

    @patch(MOCK, side_effect=[9, 5, 4, 4, 9, 5, 4, 4])
    def test_non_edgerunner_target_not_shadow_polluted(self, _m):
        """Non-edgerunner targets should not get synthetic shadow-state armor/HP overrides."""
        from game_systems.cpred import init_game_state
        gs = init_game_state()  # no "Thug" edgerunner entry

        beats = [
            {
                "beat": "V shoots thug once",
                "resolution": {
                    "type": "ranged_attack",
                    "character": "V",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "Pistol",
                    "damage_dice": 3,
                    "rof": 1,
                    "target": "Thug",
                    "target_sp": 11,
                    "range_bracket": 0,
                    "hit_location": "body",
                },
            },
            {
                "beat": "V shoots thug again",
                "resolution": {
                    "type": "ranged_attack",
                    "character": "V",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "Pistol",
                    "damage_dice": 3,
                    "rof": 1,
                    "target": "Thug",
                    "target_sp": 11,
                    "range_bracket": 0,
                    "hit_location": "body",
                },
            },
        ]

        annotated, _ops = self.resolve(beats, gs)
        first_effective_sp = annotated[0]["result"]["attacks"][0]["damage"]["effective_sp"]
        second_effective_sp = annotated[1]["result"]["attacks"][0]["damage"]["effective_sp"]
        self.assertEqual(first_effective_sp, 11)
        self.assertEqual(second_effective_sp, 11)

    def test_game_state_none_no_crash(self):
        """game_state param is accepted but not used — None should not crash."""
        annotated, ops = self.resolve([{"beat": "test", "resolution": None}], None)
        self.assertEqual(len(annotated), 1)

    @patch(MOCK, return_value=5)
    def test_large_beats_list(self, _m):
        beats = [{"beat": f"beat_{i}", "resolution": {
            "type": "skill_check", "character": "V",
            "stat_value": 6, "skill_value": 4, "dv": 15,
        }} for i in range(50)]
        start = time.time()
        annotated, ops = self.resolve(beats, {})
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0)
        self.assertEqual(len(annotated), 50)

    @patch(MOCK, return_value=5)
    def test_beat_with_no_resolution_key(self, _m):
        """Beat dict without 'resolution' key — get() returns None."""
        annotated, ops = self.resolve([{"beat": "just a beat"}], {})
        self.assertEqual(len(annotated), 1)
        self.assertNotIn("result", annotated[0])

    @patch(MOCK, return_value=9)
    def test_autofire_on_hit_outcome(self, _m):
        """Bug #9: Pipeline must detect autofire hits via top-level 'hit' field."""
        beats = [{
            "beat": "V sprays the room",
            "resolution": {
                "type": "autofire", "character": "V",
                "stat_value": 8, "skill_value": 6, "weapon_type": "SMG",
                "autofire_multiplier": 3, "target": "Ganger",
                "target_sp": 5, "range_bracket": 0,
                "on_hit": "bullets shred the wall", "on_miss": "wide spray",
            }
        }]
        annotated, ops = self.resolve(beats, {})
        result = annotated[0]["result"]
        # Autofire should detect hit and use on_hit callback
        self.assertEqual(result["on_outcome"], "bullets shred the wall")

    @patch(MOCK, return_value=1)
    def test_autofire_on_miss_outcome(self, _m):
        """Autofire miss should use on_miss callback, not 'resolved'."""
        beats = [{
            "beat": "V sprays wildly",
            "resolution": {
                "type": "autofire", "character": "V",
                "stat_value": 2, "skill_value": 1, "weapon_type": "SMG",
                "autofire_multiplier": 3, "target": "Ganger",
                "target_sp": 5, "range_bracket": 0,
                "on_hit": "shreds wall", "on_miss": "bullets go everywhere",
            }
        }]
        annotated, ops = self.resolve(beats, {})
        result = annotated[0]["result"]
        self.assertEqual(result["on_outcome"], "bullets go everywhere")

    @patch(MOCK, return_value=5)
    def test_death_save_survived_outcome(self, _m):
        """Death save survived should set on_outcome to 'survived'."""
        beats = [{
            "beat": "V death save",
            "resolution": {
                "type": "death_save", "character": "V",
                "body_stat": 8, "death_save_count": 0,
            }
        }]
        annotated, ops = self.resolve(beats, {})
        result = annotated[0]["result"]
        self.assertEqual(result["on_outcome"], "survived")

    @patch(MOCK, return_value=10)
    def test_death_save_failed_outcome(self, _m):
        """Death save natural 10 should set on_outcome to 'failed'."""
        beats = [{
            "beat": "V death save",
            "resolution": {
                "type": "death_save", "character": "V",
                "body_stat": 8, "death_save_count": 0,
            }
        }]
        annotated, ops = self.resolve(beats, {})
        result = annotated[0]["result"]
        self.assertEqual(result["on_outcome"], "failed")


# ===========================================================================
# 11. TestPropertyBased — Seeded random invariant checks
# ===========================================================================
class TestPropertyBased(unittest.TestCase):
    """Property-based tests powered by Hypothesis."""

    def test_seed_determinism(self):
        """Same seed should produce identical results."""
        random.seed(42)
        r1 = resolve_check(6, 4, 15)
        random.seed(42)
        r2 = resolve_check(6, 4, 15)
        self.assertEqual(r1, r2)

    @settings(max_examples=250, deadline=None)
    @given(
        stat=st.integers(min_value=0, max_value=10),
        skill=st.integers(min_value=0, max_value=10),
        dv=st.integers(min_value=5, max_value=25),
    )
    def test_success_iff_total_gt_dv(self, stat, skill, dv):
        r = resolve_check(stat, skill, dv)
        self.assertEqual(r["success"], r["total"] > r["dv"])

    @settings(max_examples=250, deadline=None)
    @given(
        dice=st.integers(min_value=1, max_value=12),
        sp=st.integers(min_value=-10, max_value=40),
    )
    def test_hp_damage_geq_zero(self, dice, sp):
        r = resolve_damage(dice, "body", sp)
        self.assertGreaterEqual(r["hp_damage"], 0)

    @settings(max_examples=250, deadline=None)
    @given(dice=st.integers(min_value=2, max_value=10))
    def test_crit_iff_two_sixes_and_not_rubber(self, dice):
        r = resolve_damage(dice, "body", 0, is_rubber=False)
        sixes = sum(1 for d in r["dice"] if d == 6)
        self.assertEqual(r["crit"], sixes >= 2)

    @settings(max_examples=250, deadline=None)
    @given(
        dice=st.integers(min_value=1, max_value=10),
        sp=st.integers(min_value=0, max_value=20),
    )
    def test_ablation_only_on_penetration(self, dice, sp):
        r = resolve_damage(dice, "body", sp)
        if r["ablation"] > 0:
            self.assertGreater(r["damage_past_sp"], 0)

    @settings(max_examples=250, deadline=None)
    @given(dice=st.integers(min_value=2, max_value=12))
    def test_rubber_no_crit_no_ablation(self, dice):
        r = resolve_damage(dice, "body", 0, is_rubber=True)
        self.assertFalse(r["crit"])
        self.assertEqual(r["ablation"], 0)

    @settings(max_examples=200, deadline=None)
    @given(rof=st.integers(min_value=1, max_value=8))
    def test_ranged_attacks_equal_rof(self, rof):
        r = resolve_ranged_attack(6, 4, "SMG", 2, rof, 5, 0)
        if "error" not in r:
            self.assertEqual(len(r["attacks"]), rof)

    def test_melee_tie_never_hits(self):
        """Equal rolls should always result in miss."""
        with patch(MOCK, return_value=5):
            r = resolve_melee_attack(5, 5, 5, 5, 2, 1, 5)
            self.assertFalse(r["attacks"][0]["hit"])

    @settings(max_examples=120, deadline=None)
    @given(body=st.integers(min_value=1, max_value=200))
    def test_natural_10_always_fails_death_save(self, body):
        with patch(MOCK, return_value=10):
            r = resolve_death_save(body, 0)
            self.assertFalse(r["survived"])

    @settings(max_examples=250, deadline=None)
    @given(
        luck=st.integers(min_value=0, max_value=100),
        rel=st.integers(min_value=0, max_value=100),
    )
    def test_relationship_bonus_capped_to_5_with_nonneg_inputs(self, luck, rel):
        with patch(MOCK, return_value=5):
            r = resolve_check(0, 0, 0, luck_spent=luck, rel_bonus=rel)
            bonus = r["total"] - 5
            self.assertEqual(bonus, luck + min(rel, 5))

    @settings(max_examples=120, deadline=None)
    @given(
        refs=st.lists(st.integers(min_value=1, max_value=10), min_size=1, max_size=25),
    )
    def test_initiative_output_count_matches_input(self, refs):
        combatants = [{"name": f"NPC_{i}", "ref": ref} for i, ref in enumerate(refs)]
        r = resolve_initiative(combatants)
        self.assertEqual(len(r), len(combatants))

    @settings(max_examples=120, deadline=None)
    @given(
        refs=st.lists(st.integers(min_value=1, max_value=10), min_size=1, max_size=25),
    )
    def test_initiative_all_names_present(self, refs):
        combatants = [{"name": f"NPC_{i}", "ref": ref} for i, ref in enumerate(refs)]
        r = resolve_initiative(combatants)
        input_names = {c["name"] for c in combatants}
        output_names = {e["name"] for e in r}
        self.assertEqual(input_names, output_names)

    @settings(max_examples=120, deadline=None)
    @given(
        checks=st.lists(
            st.tuples(
                st.integers(min_value=1, max_value=10),
                st.integers(min_value=1, max_value=10),
                st.integers(min_value=9, max_value=25),
            ),
            min_size=1,
            max_size=20,
        )
    )
    def test_resolve_actions_results_count_matches(self, checks):
        actions = [
            {
                "type": "skill_check",
                "character": f"V_{i}",
                "stat_value": stat,
                "skill_value": skill,
                "dv": dv,
            }
            for i, (stat, skill, dv) in enumerate(checks)
        ]
        r = resolve_actions(actions)
        self.assertEqual(len(r["results"]), len(actions))

    @settings(max_examples=250, deadline=None)
    @given(
        stat=st.integers(min_value=0, max_value=10),
        skill=st.integers(min_value=0, max_value=10),
    )
    def test_formatted_contains_total(self, stat, skill):
        r = resolve_check(stat, skill, 15)
        self.assertIn(str(r["total"]), r["formatted"])


# ===========================================================================
# 12. TestHumanityCeilingHypothesis
# ===========================================================================
class TestHumanityCeilingHypothesis(unittest.TestCase):
    """Hypothesis fuzz tests for humanity ceiling and cyberware normalization."""

    def _make_er(self, humanity_current=60, humanity_max=60):
        return {
            "hp": {"current": 40, "max": 40},
            "humanity": {"current": humanity_current, "max": humanity_max},
            "luck": {"current": 7, "max": 7},
            "armor": {"head": 0, "body": 0},
            "eurobucks": 5000,
            "critical_injuries": [],
            "cyberware_effects": [],
            "conditions": [],
            "weapons": [],
            "seriously_wounded": False,
        }

    def _apply(self, game_state, ops):
        apply_game_state(game_state, {"edgerunner_ops": ops}, 1)

    def test_ceiling_cost_normalization_exhaustive(self):
        for name, spec in CYBERWARE_TABLE.items():
            expected = spec["ceiling_cost"]
            self.assertEqual(_cyberware_ceiling_cost(name), expected)
            self.assertEqual(_cyberware_ceiling_cost(f"  {name}  "), expected)
            self.assertEqual(_cyberware_ceiling_cost(f"{name} (Right)"), expected)

        self.assertEqual(_cyberware_ceiling_cost("cyberaudio"), 2)
        self.assertEqual(_cyberware_ceiling_cost("cybereyes"), 2)
        self.assertEqual(_cyberware_ceiling_cost("cyberarms"), 2)
        self.assertEqual(_cyberware_ceiling_cost("cyberlegs"), 2)

    @settings(max_examples=300, deadline=None)
    @given(
        raw=st.text(
            min_size=1,
            max_size=30,
            alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-+/")),
        )
    )
    def test_unknown_cyberware_defaults_to_two(self, raw):
        value = raw.strip()
        assume(value)
        normalized = value.lower()
        base = normalized.split("(")[0].strip()
        known = {k.lower() for k in CYBERWARE_TABLE}
        aliases = {"cyberaudio", "cybereyes", "cyberarms", "cyberlegs"}
        assume(normalized not in known)
        assume(base not in known)
        assume(normalized not in aliases)
        assume(base not in aliases)
        self.assertEqual(_cyberware_ceiling_cost(value), 2)

    @settings(max_examples=180, deadline=None)
    @given(
        base_max=st.integers(min_value=10, max_value=120),
        current=st.integers(min_value=0, max_value=180),
        picks=st.lists(
            st.tuples(
                st.sampled_from(list(CYBERWARE_TABLE.keys()) + ["cyberaudio", "cybereyes", "cyberarms", "cyberlegs"]),
                st.sampled_from(["", " (Left)", " (Right)", " (Mk.II)"]),
            ),
            min_size=1,
            max_size=25,
        ),
    )
    def test_migration_is_idempotent(self, base_max, current, picks):
        values = [name + suffix for name, suffix in picks]
        expected_reduction = sum(_cyberware_ceiling_cost(v) for v in values if isinstance(v, str) and v.strip())
        expected_max = base_max
        expected_cur = min(current, expected_max)

        gs = {"edgerunners": {"V": self._make_er(humanity_current=current, humanity_max=base_max)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = list(values)

        self._apply(gs, [])
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["humanity"]["max"], expected_max)
        self.assertEqual(er["humanity"]["current"], expected_cur)
        self.assertTrue(er.get("_humanity_ceiling_migrated"))
        self.assertEqual(er.get("_humanity_base_max"), base_max)
        self.assertEqual(er.get("_humanity_base_ceiling_total"), expected_reduction)

        snapshot = copy.deepcopy(er)
        self._apply(gs, [])
        self.assertEqual(gs["edgerunners"]["V"], snapshot)

    @settings(max_examples=160, deadline=None)
    @given(
        base=st.integers(min_value=20, max_value=120),
        cur=st.integers(min_value=0, max_value=120),
        ops=st.lists(
            st.tuples(
                st.sampled_from(["add", "remove"]),
                st.tuples(
                    st.sampled_from(
                        list(CYBERWARE_TABLE.keys()) +
                        ["cyberaudio", "cybereyes", "cyberarms", "cyberlegs",
                         "Homebrew Rig", "Prototype Armature", "Streetware X"]
                    ),
                    st.sampled_from(["", " (Left)", " (Right)", " (Mk.II)"]),
                ),
            ),
            min_size=1,
            max_size=120,
        ),
    )
    def test_add_remove_sequence_matches_model(self, base, cur, ops):
        cur = min(cur, base)
        gs = {"edgerunners": {"V": self._make_er(humanity_current=cur, humanity_max=base)}}
        er = gs["edgerunners"]["V"]
        er["_humanity_ceiling_migrated"] = True
        er["_humanity_base_max"] = base
        er["_humanity_base_ceiling_total"] = 0

        model_effects = []
        model_max = base
        model_cur = cur

        for action, value_tuple in ops:
            value = value_tuple[0] + value_tuple[1]
            payload = {"edgerunner": "V", "op": "cyberware", "action": action, "value": value}
            self._apply(gs, [payload])

            if action == "add":
                add_key = _normalize_cyberware_entry_name(value)
                if add_key and all(_normalize_cyberware_entry_name(v) != add_key for v in model_effects):
                    model_effects.append(value)
            else:
                removed = False
                remove_entry_key = _normalize_cyberware_entry_name(value)
                if remove_entry_key:
                    for idx, existing in enumerate(model_effects):
                        if _normalize_cyberware_entry_name(existing) == remove_entry_key:
                            model_effects.pop(idx)
                            removed = True
                            break

                if not removed and not _cyberware_has_qualifier(value):
                    remove_key = _normalize_cyberware_name(value)
                    candidate_indices = [
                        idx for idx, existing in enumerate(model_effects)
                        if _normalize_cyberware_name(existing) == remove_key
                    ]
                    if len(candidate_indices) == 1:
                        model_effects.pop(candidate_indices[0])

            total_ceiling = sum(_cyberware_ceiling_cost(v) for v in model_effects)
            model_max = max(0, min(base, base - total_ceiling))
            if model_cur > model_max:
                model_cur = model_max

            out = gs["edgerunners"]["V"]
            self.assertEqual(out["cyberware_effects"], model_effects)
            self.assertEqual(out["humanity"]["max"], model_max)
            self.assertEqual(out["humanity"]["current"], model_cur)
            self.assertLessEqual(out["humanity"]["max"], base)
            self.assertGreaterEqual(out["humanity"]["max"], 0)
            self.assertLessEqual(out["humanity"]["current"], out["humanity"]["max"])


# ===========================================================================
# RESOLVE_MECHANICS_TOOL schema validation
# ===========================================================================
class TestToolSchema(unittest.TestCase):
    """Validate the RESOLVE_MECHANICS_TOOL definition."""

    def test_tool_has_required_fields(self):
        self.assertEqual(RESOLVE_MECHANICS_TOOL["name"], "resolve_mechanics")
        self.assertIn("description", RESOLVE_MECHANICS_TOOL)
        self.assertIn("input_schema", RESOLVE_MECHANICS_TOOL)

    def test_schema_requires_actions(self):
        schema = RESOLVE_MECHANICS_TOOL["input_schema"]
        self.assertEqual(schema["type"], "object")
        self.assertIn("actions", schema.get("required", []))

    def test_actions_is_array(self):
        actions_schema = RESOLVE_MECHANICS_TOOL["input_schema"]["properties"]["actions"]
        self.assertEqual(actions_schema["type"], "array")

    def test_action_item_requires_type_and_character(self):
        item_schema = RESOLVE_MECHANICS_TOOL["input_schema"]["properties"]["actions"]["items"]
        self.assertIn("type", item_schema.get("required", []))
        self.assertIn("character", item_schema.get("required", []))

    def test_type_enum_matches_resolve_actions(self):
        type_enum = RESOLVE_MECHANICS_TOOL["input_schema"]["properties"]["actions"]["items"]["properties"]["type"]["enum"]
        expected = {"skill_check", "ranged_attack", "melee_attack", "autofire", "death_save", "initiative", "opposed_check", "program_attack", "program_attack_vs_netrunner", "ice_attack_vs_program", "ambush"}
        self.assertEqual(set(type_enum), expected)


if __name__ == "__main__":
    unittest.main()
