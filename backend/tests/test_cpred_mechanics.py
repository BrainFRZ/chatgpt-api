"""
Unit tests for Cyberpunk RED deterministic mechanics resolution.

Uses random.seed() for deterministic dice in tests.
"""
import random
import sys
import os
import unittest
from unittest.mock import patch

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred_mechanics import (
    resolve_check,
    resolve_damage,
    resolve_ranged_attack,
    resolve_melee_attack,
    resolve_autofire,
    resolve_death_save,
    resolve_initiative,
    resolve_opposed_check,
    resolve_program_attack,
    resolve_ice_effect,
    next_combat_turn,
    resolve_actions,
    resolve_driving_check,
    resolve_ramming,
    resolve_vehicle_weak_point,
    resolve_spike_strip,
    _roll_check_die,
)
from game_systems.cpred_tables import (
    CRIT_INJURY_BODY,
    CRIT_INJURY_HEAD,
    RANGED_DV_TABLE,
    AUTOFIRE_DV_TABLE,
    calculate_hp,
)
from game_systems.cpred import _apply_vehicle_updates, _format_vehicle_lines, apply_cpred_combat_state, GAME_SYSTEM


class TestResolveCheck(unittest.TestCase):
    """Tests for resolve_check — d10 + STAT + Skill vs DV."""

    def test_normal_success(self):
        """Normal roll that beats DV."""
        # d10 returns 7
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_check(stat_value=6, skill_value=4, dv=15)
        self.assertEqual(result["die"]["base"], 7)
        self.assertIsNone(result["die"]["extra"])
        self.assertEqual(result["total"], 7 + 6 + 4)  # 17
        self.assertTrue(result["success"])  # 17 > 15
        self.assertIn("✓", result["formatted"])

    def test_normal_failure(self):
        """Normal roll that fails to beat DV."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=3):
            result = resolve_check(stat_value=4, skill_value=2, dv=15)
        self.assertEqual(result["total"], 3 + 4 + 2)  # 9
        self.assertFalse(result["success"])  # 9 < 15
        self.assertIn("✗", result["formatted"])

    def test_equal_dv_fails(self):
        """Exactly meeting DV should fail (must BEAT)."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_check(stat_value=6, skill_value=4, dv=15)
        self.assertEqual(result["total"], 15)
        self.assertFalse(result["success"])

    def test_exploding_10(self):
        """Natural 10 should roll extra d10 and add."""
        call_count = [0]
        def mock_randint(a, b):
            call_count[0] += 1
            return 10 if call_count[0] == 1 else 7
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=mock_randint):
            result = resolve_check(stat_value=6, skill_value=4, dv=20)
        self.assertEqual(result["die"]["base"], 10)
        self.assertEqual(result["die"]["extra"], 7)
        self.assertEqual(result["die"]["total"], 17)
        self.assertEqual(result["total"], 17 + 6 + 4)  # 27
        self.assertTrue(result["success"])

    def test_fumble_1(self):
        """Natural 1 should roll extra d10 and subtract."""
        call_count = [0]
        def mock_randint(a, b):
            call_count[0] += 1
            return 1 if call_count[0] == 1 else 4
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=mock_randint):
            result = resolve_check(stat_value=6, skill_value=4, dv=9)
        self.assertEqual(result["die"]["base"], 1)
        self.assertEqual(result["die"]["extra"], 4)
        self.assertEqual(result["die"]["total"], -3)
        self.assertEqual(result["total"], -3 + 6 + 4)  # 7
        self.assertFalse(result["success"])

    def test_no_chaining_on_exploding(self):
        """Exploding 10 should NOT chain (only one extra roll)."""
        rolls = [10, 10]  # First 10 explodes, second 10 is just the extra
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_check(stat_value=6, skill_value=4, dv=15)
        self.assertEqual(result["die"]["total"], 20)  # 10 + 10, not 10 + 10 + more

    def test_seriously_wounded(self):
        """Seriously wounded applies -2."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_check(stat_value=6, skill_value=4, dv=13, seriously_wounded=True)
        self.assertEqual(result["total"], 5 + 6 + 4 - 2)  # 13
        self.assertFalse(result["success"])  # 13 not > 13

    def test_luck_spent(self):
        """Luck points add 1:1."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_check(stat_value=6, skill_value=4, dv=17, luck_spent=2)
        self.assertEqual(result["total"], 5 + 6 + 4 + 2)  # 17
        self.assertFalse(result["success"])  # 17 not > 17

    def test_luck_uncapped_rel_capped(self):
        """Luck is uncapped; relationship bonus is capped at +5."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_check(stat_value=6, skill_value=4, dv=13, luck_spent=8, rel_bonus=7)
        self.assertEqual(result["total"], 5 + 6 + 4 + 8 + 5)  # 28


class TestResolveDamage(unittest.TestCase):
    """Tests for resolve_damage — Nd6, crit check, SP, ablation."""

    def test_basic_penetration(self):
        """Damage exceeding SP should penetrate."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            # 3d6 = [4,4,4] = 12 vs SP 7
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=7)
        self.assertEqual(result["total_rolled"], 12)
        self.assertEqual(result["damage_past_sp"], 5)
        self.assertEqual(result["hp_damage"], 5)
        self.assertEqual(result["ablation"], 1)
        self.assertFalse(result["crit"])

    def test_sp_blocks_all(self):
        """Damage not exceeding SP should be fully blocked."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=2):
            # 3d6 = [2,2,2] = 6 vs SP 11
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=11)
        self.assertEqual(result["total_rolled"], 6)
        self.assertEqual(result["damage_past_sp"], 0)
        self.assertEqual(result["hp_damage"], 0)
        self.assertEqual(result["ablation"], 0)

    def test_crit_detection(self):
        """2+ dice showing 6 should trigger crit."""
        rolls = [6, 6, 3, 1, 2]  # 3 damage dice + 2 for injury lookup
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=5)
        self.assertTrue(result["crit"])
        self.assertEqual(result["crit_bonus"], 5)
        self.assertIsNotNone(result["crit_injury"])
        self.assertIn("name", result["crit_injury"])

    def test_no_crit_one_six(self):
        """Only 1 die showing 6 should NOT trigger crit."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[6, 5, 4]):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=5)
        self.assertFalse(result["crit"])
        self.assertEqual(result["crit_bonus"], 0)

    def test_crit_bonus_ignores_sp(self):
        """Crit +5 bonus goes direct to HP even if base damage is blocked."""
        # SP blocks base damage but crit bonus still applies
        rolls = [6, 6, 1, 3, 4]  # 3 damage + 2 for crit lookup
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=20)
        self.assertTrue(result["crit"])
        self.assertEqual(result["damage_past_sp"], 0)  # 13 < 20
        self.assertEqual(result["hp_damage"], 5)  # crit bonus only

    def test_melee_sp_halving(self):
        """Melee should halve SP (round up)."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=11, is_melee=True)
        self.assertEqual(result["effective_sp"], 6)  # ceil(11/2) = 6
        self.assertEqual(result["damage_past_sp"], 6)  # 12 - 6

    def test_melee_sp_halving_even(self):
        """Melee SP halving with even SP."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=10, is_melee=True)
        self.assertEqual(result["effective_sp"], 5)  # ceil(10/2) = 5

    def test_brawling_full_sp(self):
        """Brawling should use full SP (not halved)."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=11,
                                   is_melee=True, is_brawling=True)
        self.assertEqual(result["effective_sp"], 11)

    def test_ap_ammo_ablation(self):
        """AP ammo should ablate 2 instead of 1."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=7, is_ap=True)
        self.assertEqual(result["ablation"], 2)

    def test_rubber_no_crit_no_ablation(self):
        """Rubber ammo: no crits, no ablation."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[6, 6, 6]):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=5, is_rubber=True)
        self.assertFalse(result["crit"])
        self.assertEqual(result["ablation"], 0)

    def test_rubber_damage_capped_non_lethal(self):
        """Rubber ammo should not reduce a known target below 1 HP."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[6, 6, 6]):
            result = resolve_damage(
                damage_dice=3,
                hit_location="body",
                target_sp=0,
                is_rubber=True,
                target_hp_current=3,
            )
        self.assertEqual(result["hp_damage"], 2)

    def test_aimed_head_double_damage(self):
        """Aimed head shot should double damage past SP."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="head", target_sp=5, aimed_shot="head")
        self.assertEqual(result["damage_past_sp"], 14)  # (12-5) * 2

    def test_aimed_leg_applies_broken_leg_injury(self):
        """Aimed leg shot that penetrates should always apply Broken Leg injury."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=5, aimed_shot="leg")
        self.assertEqual(result["aimed_effect"], "broken_leg")
        self.assertIsNotNone(result["crit_injury"])
        self.assertEqual(result["crit_injury"]["name"], "Broken Leg")
        injury_names = [ci["name"] for ci in result.get("critical_injuries", [])]
        self.assertIn("Broken Leg", injury_names)

    def test_rubber_aimed_leg_does_not_apply_broken_leg(self):
        """Rubber ammo should not apply critical injuries from aimed leg shots."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_damage(
                damage_dice=3,
                hit_location="body",
                target_sp=5,
                aimed_shot="leg",
                is_rubber=True,
            )
        self.assertIsNone(result["aimed_effect"])
        self.assertIsNone(result["crit_injury"])
        self.assertEqual(result.get("critical_injuries", []), [])

    def test_crit_injury_lookup_body(self):
        """Crit injury lookup should use body table for body hits."""
        # 3 damage dice (6,6,3) + 2d6 for injury (3,4 = 7 = Foreign Object)
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[6, 6, 3, 3, 4]):
            result = resolve_damage(damage_dice=3, hit_location="body", target_sp=0)
        self.assertTrue(result["crit"])
        self.assertEqual(result["crit_injury"]["total"], 7)
        self.assertEqual(result["crit_injury"]["name"], "Foreign Object")
        self.assertEqual(result["crit_injury"]["location"], "body")

    def test_crit_injury_lookup_head(self):
        """Crit injury lookup should use head table for head hits."""
        # 3 damage dice (6,6,3) + 2d6 for injury (5,6 = 11 = Crushed Windpipe)
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[6, 6, 3, 5, 6]):
            result = resolve_damage(damage_dice=3, hit_location="head", target_sp=0)
        self.assertTrue(result["crit"])
        self.assertEqual(result["crit_injury"]["total"], 11)
        self.assertEqual(result["crit_injury"]["name"], "Crushed Windpipe")
        self.assertEqual(result["crit_injury"]["location"], "head")


class TestResolveDeathSave(unittest.TestCase):
    """Tests for resolve_death_save."""

    def test_survive(self):
        """Roll under BODY = survive."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=3):
            result = resolve_death_save(body_stat=6, death_save_count=0)
        self.assertTrue(result["survived"])
        self.assertEqual(result["effective_roll"], 3)

    def test_fail(self):
        """Roll >= BODY = fail."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_death_save(body_stat=6, death_save_count=0)
        self.assertFalse(result["survived"])

    def test_equal_body_fails(self):
        """Roll exactly equal to BODY = fail (must be under)."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=6):
            result = resolve_death_save(body_stat=6, death_save_count=0)
        self.assertFalse(result["survived"])

    def test_natural_10_auto_fail(self):
        """Natural 10 always fails regardless of BODY."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=10):
            result = resolve_death_save(body_stat=100, death_save_count=0)
        self.assertFalse(result["survived"])
        self.assertTrue(result["natural_10"])

    def test_cumulative_count(self):
        """Death save count adds to the roll."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=2):
            result = resolve_death_save(body_stat=6, death_save_count=4)
        self.assertEqual(result["effective_roll"], 6)  # 2 + 4
        self.assertFalse(result["survived"])  # 6 >= 6

    def test_injury_mods(self):
        """Active injuries' dv_mod adds to the roll."""
        injuries = [{"dv_mod": 1}, {"dv_mod": 1}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=2):
            result = resolve_death_save(body_stat=6, death_save_count=1, active_injuries=injuries)
        self.assertEqual(result["effective_roll"], 5)  # 2 + 1 + 2
        self.assertTrue(result["survived"])  # 5 < 6


class TestResolveAutofire(unittest.TestCase):
    """Tests for resolve_autofire."""

    def test_autofire_hit(self):
        """Autofire hit should calculate damage from margin."""
        # Check roll d10=8, then 2d6 for damage
        rolls = [8, 3, 4]
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_autofire(
                stat_value=8, skill_value=6, weapon_type="SMG",
                autofire_multiplier=3, target_sp=7, range_bracket=1,
            )
        self.assertTrue(result["hit"])
        # DV for SMG at bracket 1 = 17
        # Total = 8 + 8 + 6 = 22, margin = 22-17 = 5
        # 2d6 = 3+4 = 7, raw = 7*5 = 35, capped = 3*7 = 21
        self.assertIsNotNone(result["damage"])

    def test_autofire_miss(self):
        """Autofire miss should return no damage."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=2):
            result = resolve_autofire(
                stat_value=4, skill_value=2, weapon_type="SMG",
                autofire_multiplier=3, target_sp=7, range_bracket=0,
            )
        self.assertFalse(result["hit"])
        self.assertIsNone(result["damage"])

    def test_autofire_consumes_10_rounds(self):
        """Autofire always consumes 10 rounds."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_autofire(
                stat_value=8, skill_value=6, weapon_type="SMG",
                autofire_multiplier=3, target_sp=7, range_bracket=0,
            )
        self.assertEqual(result["rounds_consumed"], 10)

    def test_autofire_crit_applies_bonus_and_injury_op(self):
        """Double sixes on autofire damage should apply crit bonus and injury state op."""
        # d10 attack roll, then damage dice 6,6, then crit injury lookup 2d6
        rolls = [8, 6, 6, 1, 2]
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_autofire(
                stat_value=8,
                skill_value=6,
                weapon_type="SMG",
                autofire_multiplier=3,
                target_sp=20,
                range_bracket=1,
                target_name="Ganger",
            )

        self.assertTrue(result["hit"])
        self.assertIsNotNone(result["damage"])
        self.assertTrue(result["damage"]["crit"])
        self.assertEqual(result["damage"]["crit_bonus"], 5)
        self.assertEqual(result["damage"]["hp_damage"], result["damage"]["damage_past_sp"] + 5)
        crit_ops = [op for op in result["state_ops"] if op.get("op") == "critical_injury"]
        self.assertEqual(len(crit_ops), 1)
        hp_ops = [op for op in result["state_ops"] if op.get("op") == "hp"]
        self.assertEqual(len(hp_ops), 1)
        self.assertEqual(hp_ops[0]["change"], -result["damage"]["hp_damage"])


class TestResolveInitiative(unittest.TestCase):
    """Tests for resolve_initiative."""

    def test_basic_sorting(self):
        """Should sort by total descending."""
        combatants = [
            {"name": "V", "ref": 8},
            {"name": "Ganger", "ref": 5},
            {"name": "Borg", "ref": 3},
        ]
        # d10 rolls: V=3, Ganger=4, Borg=10 → V=11, Ganger=9, Borg=13
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[3, 4, 10]):
            result = resolve_initiative(combatants)
        self.assertEqual(result[0]["name"], "Borg")    # 10+3=13
        self.assertEqual(result[1]["name"], "V")       # 3+8=11
        self.assertEqual(result[2]["name"], "Ganger")  # 4+5=9
        self.assertEqual(len(result), 3)

    def test_tiebreaking(self):
        """Ties should be broken with additional d10 rolls."""
        combatants = [
            {"name": "A", "ref": 5},
            {"name": "B", "ref": 5},
        ]
        # Initial d10s: both roll 5 (tied at 10 each)
        # Tiebreak d10s: A=7, B=3
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[5, 5, 7, 3]):
            result = resolve_initiative(combatants)
        self.assertEqual(result[0]["name"], "A")
        self.assertEqual(result[1]["name"], "B")


class TestNextCombatTurn(unittest.TestCase):
    """Tests for next_combat_turn."""

    def test_basic_advance(self):
        """Should advance to next combatant."""
        order = ["V", "Ganger", "Borg"]
        result = next_combat_turn(order, current_turn=0)
        self.assertEqual(result["next_turn"], 1)
        self.assertFalse(result["round_incremented"])

    def test_wrap_around(self):
        """Should wrap to beginning and increment round."""
        order = ["V", "Ganger", "Borg"]
        result = next_combat_turn(order, current_turn=2)
        self.assertEqual(result["next_turn"], 0)
        self.assertTrue(result["new_round"])

    def test_skip_eliminated(self):
        """Should skip eliminated combatants."""
        order = ["V", "Ganger", "Borg"]
        result = next_combat_turn(order, current_turn=0, eliminated=["Ganger"])
        self.assertEqual(result["next_turn"], 2)


class TestResolveActions(unittest.TestCase):
    """Tests for resolve_actions batch resolver."""

    def test_skill_check_action(self):
        """Batch resolver handles skill_check type."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions([{
                "type": "skill_check",
                "character": "V",
                "stat_value": 6,
                "skill_value": 4,
                "dv": 15,
            }])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["type"], "skill_check")
        self.assertTrue(result["results"][0]["success"])

    def test_unknown_action_type(self):
        """Unknown action types should return error."""
        result = resolve_actions([{"type": "unknown", "character": "V"}])
        self.assertIn("error", result["results"][0])

    def test_multiple_actions(self):
        """Should resolve multiple actions and collect state_ops."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions([
                {"type": "skill_check", "character": "V", "stat_value": 6, "skill_value": 4, "dv": 13},
                {"type": "death_save", "character": "Ganger", "body_stat": 6, "death_save_count": 0},
            ])
        self.assertEqual(len(result["results"]), 2)

    def test_luck_spend_emits_luck_ops(self):
        """Actions that spend Luck should emit matching luck state ops."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions([
                {
                    "type": "skill_check",
                    "character": "V",
                    "stat_value": 6,
                    "skill_value": 4,
                    "dv": 15,
                    "luck_spent": 2,
                },
                {
                    "type": "ranged_attack",
                    "character": "V",
                    "stat_value": 6,
                    "skill_value": 6,
                    "weapon_type": "Pistol",
                    "damage_dice": 2,
                    "rof": 1,
                    "target_sp": 0,
                    "range_bracket": 0,
                    "target": "Ganger",
                    "luck_spent": 1,
                },
                {
                    "type": "autofire",
                    "character": "V",
                    "stat_value": 8,
                    "skill_value": 8,
                    "weapon_type": "SMG",
                    "autofire_multiplier": 3,
                    "target_sp": 0,
                    "range_bracket": 0,
                    "target": "Ganger",
                    "luck_spent": 3,
                },
            ])

        luck_ops = [op for op in result["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(len(luck_ops), 3)
        self.assertEqual(luck_ops[0]["change"], -2)
        self.assertEqual(luck_ops[1]["change"], -1)
        self.assertEqual(luck_ops[2]["change"], -3)
        self.assertTrue(all(op["edgerunner"] == "V" for op in luck_ops))

    def test_death_save_emits_state_op(self):
        """Death saves should increment persistent death_save_count via state op."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=3):
            result = resolve_actions([
                {"type": "death_save", "character": "Ganger", "body_stat": 6, "death_save_count": 2},
            ])
        death_ops = [op for op in result["state_ops"] if op.get("op") == "death_save"]
        self.assertEqual(len(death_ops), 1)
        self.assertEqual(death_ops[0]["edgerunner"], "Ganger")


class TestResolveIceEffect(unittest.TestCase):
    def test_program_destroy_targets_only_rezzed_programs(self):
        ice_block = {"name": "Raven", "effect": "program_destroy"}
        active_programs = [
            {"name": "ADeact", "category": "attacker", "status": "deactivated"},
            {"name": "ZActive", "category": "attacker", "status": "active"},
        ]
        with patch("game_systems.cpred_mechanics.random.choice", side_effect=lambda c: c[0]):
            result = resolve_ice_effect(ice_block, active_programs=active_programs)
        destroys = [op for op in result["state_ops"] if op.get("op") == "program_destroy"]
        self.assertEqual(len(destroys), 1)
        self.assertEqual(destroys[0]["program_name"], "ZActive")

    def test_invalid_ranged_attack_does_not_spend_luck(self):
        """Invalid ranged requests should not consume Luck."""
        result = resolve_actions([
            {
                "type": "ranged_attack",
                "character": "V",
                "weapon_type": "Pistol",
                "range_bracket": 99,  # invalid
                "luck_spent": 3,
            }
        ])
        self.assertIn("error", result["results"][0])
        self.assertFalse(any(op.get("op") == "luck" for op in result["state_ops"]))

    def test_invalid_autofire_does_not_spend_luck(self):
        """Invalid autofire requests should not consume Luck."""
        result = resolve_actions([
            {
                "type": "autofire",
                "character": "V",
                "weapon_type": "SMG",
                "range_bracket": 99,  # invalid
                "luck_spent": 2,
            }
        ])
        self.assertIn("error", result["results"][0])
        self.assertFalse(any(op.get("op") == "luck" for op in result["state_ops"]))


class TestResolverStateOpShapes(unittest.TestCase):
    """Regression tests for resolver state-op schema alignment."""

    def test_ranged_crit_injury_op_uses_top_level_fields(self):
        """Critical injury op should expose name/effect/dv_mod at top level."""
        # d10 attack roll, 3d6 damage, 2d6 crit table
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 6, 6, 3, 1, 2]):
            result = resolve_ranged_attack(
                stat_value=8,
                skill_value=8,
                weapon_type="Pistol",
                damage_dice=3,
                rof=1,
                target_sp=0,
                range_bracket=0,
                target_name="Ganger",
            )

        crit_ops = [op for op in result["state_ops"] if op.get("op") == "critical_injury"]
        self.assertEqual(len(crit_ops), 1)
        crit_op = crit_ops[0]
        self.assertIn("name", crit_op)
        self.assertIn("effect", crit_op)
        self.assertIn("dv_mod", crit_op)
        self.assertNotIn("injury", crit_op)

    def test_ranged_aimed_leg_emits_critical_injury_op(self):
        """Aimed leg shot should emit a Broken Leg critical_injury op when armor is penetrated."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=8):
            result = resolve_ranged_attack(
                stat_value=8,
                skill_value=8,
                weapon_type="Pistol",
                damage_dice=2,
                rof=1,
                target_sp=0,
                range_bracket=0,
                aimed_shot="leg",
                target_name="Ganger",
            )

        crit_ops = [op for op in result["state_ops"] if op.get("op") == "critical_injury"]
        self.assertTrue(any(op.get("name") == "Broken Leg" for op in crit_ops))


class TestCritInjuryTables(unittest.TestCase):
    """Verify critical injury table completeness."""

    def test_body_table_complete(self):
        """Body table should have entries for all 2d6 results (2-12)."""
        for i in range(2, 13):
            self.assertIn(i, CRIT_INJURY_BODY)
            self.assertIn("name", CRIT_INJURY_BODY[i])
            self.assertIn("dv_mod", CRIT_INJURY_BODY[i])

    def test_head_table_complete(self):
        """Head table should have entries for all 2d6 results (2-12)."""
        for i in range(2, 13):
            self.assertIn(i, CRIT_INJURY_HEAD)
            self.assertIn("name", CRIT_INJURY_HEAD[i])
            self.assertIn("dv_mod", CRIT_INJURY_HEAD[i])

    def test_body_dv_mods(self):
        """Verify specific body injury Death Save penalties."""
        self.assertEqual(CRIT_INJURY_BODY[2]["dv_mod"], 1)   # Dismembered Arm
        self.assertEqual(CRIT_INJURY_BODY[5]["dv_mod"], 0)   # Broken Ribs
        self.assertEqual(CRIT_INJURY_BODY[12]["dv_mod"], 1)  # Dismembered Leg

    def test_head_dv_mods(self):
        """Verify specific head injury Death Save penalties."""
        self.assertEqual(CRIT_INJURY_HEAD[2]["dv_mod"], 1)   # Lost Eye
        self.assertEqual(CRIT_INJURY_HEAD[5]["dv_mod"], 0)   # Concussion
        self.assertEqual(CRIT_INJURY_HEAD[8]["dv_mod"], 1)   # Whiplash


class TestHPFormula(unittest.TestCase):
    """Tests for HP calculation."""

    def test_known_values(self):
        """Verify against known CRB values."""
        self.assertEqual(calculate_hp(2, 2), 20)
        self.assertEqual(calculate_hp(8, 8), 50)
        self.assertEqual(calculate_hp(10, 10), 60)

    def test_odd_average(self):
        """Odd averages should round up."""
        self.assertEqual(calculate_hp(3, 2), 25)  # avg 2.5, ceil = 3, HP = 10 + 15 = 25
        self.assertEqual(calculate_hp(5, 4), 35)  # avg 4.5, ceil = 5, HP = 10 + 25 = 35


class TestRangedDVTable(unittest.TestCase):
    """Verify ranged DV table structure."""

    def test_all_weapon_types_present(self):
        expected = ["Pistol", "SMG", "Shotgun", "Assault Rifle", "Sniper Rifle",
                    "Bows & Crossbow", "Grenade Launcher", "Rocket Launcher"]
        for wtype in expected:
            self.assertIn(wtype, RANGED_DV_TABLE)

    def test_bracket_count(self):
        """Each weapon type should have 8 range brackets."""
        for wtype, dvs in RANGED_DV_TABLE.items():
            self.assertEqual(len(dvs), 8, f"{wtype} should have 8 brackets")

    def test_autofire_errata_values(self):
        """Verify autofire DV table uses errata-corrected values."""
        self.assertEqual(AUTOFIRE_DV_TABLE["SMG"], [20, 17, 20, 25, 30])
        self.assertEqual(AUTOFIRE_DV_TABLE["Assault Rifle"], [22, 20, 17, 20, 25])


class TestComputeRelBonus(unittest.TestCase):
    """Tests for compute_rel_bonus — auto-computed relationship bonuses."""

    def setUp(self):
        from game_systems.cpred import compute_rel_bonus
        self.compute = compute_rel_bonus

    def test_no_target_returns_zero(self):
        rels = {"Judy": {"rs": 70}}
        self.assertEqual(self.compute(rels, None, "", "social"), 0)

    def test_rs_positive_social(self):
        """RS 70 (T5: Close) → +2 social."""
        rels = {"Judy": {"rs": 70}}
        self.assertEqual(self.compute(rels, None, "Judy", "social"), 2)

    def test_rs_positive_persuasion(self):
        """RS 70 (T5: Close) → +3 persuasion (exact context match)."""
        rels = {"Judy": {"rs": 70}}
        self.assertEqual(self.compute(rels, None, "Judy", "persuasion"), 3)

    def test_rs_negative(self):
        """RS -30 (−T2: Disliked) → -1 social."""
        rels = {"Maelstrom Ganger": {"rs": -30}}
        self.assertEqual(self.compute(rels, None, "Maelstrom Ganger", "social"), -1)

    def test_rs_very_negative(self):
        """RS -70 (−T5: Nemesis) → -3 all checks."""
        rels = {"Smasher": {"rs": -70}}
        self.assertEqual(self.compute(rels, None, "Smasher", "combat"), -3)

    def test_roms_bonus(self):
        """RomS 50 (T3: Partner) → +2 social."""
        rels = {"Judy": {"rs": 70, "roms": 50}}
        # RS 70 social=+2, RomS 50 social=+2 → total=+4
        self.assertEqual(self.compute(rels, None, "Judy", "social"), 4)

    def test_combined_clamped_to_5(self):
        """RS + RomS bonus clamped to +5."""
        rels = {"Judy": {"rs": 95, "roms": 95}}
        # RS 95 social=+3, RomS 95 all=+3 → total=6, clamped to 5
        self.assertEqual(self.compute(rels, None, "Judy", "social"), 5)

    def test_combined_clamped_to_neg5(self):
        """Negative bonus clamped to -5."""
        rels = {"Smasher": {"rs": -100}}
        # RS -100 → all=-4
        factions = {"Arasaka": {"fr": -90}}
        # Won't match "Smasher" in factions, so just -4
        self.assertEqual(self.compute(rels, factions, "Smasher", "social"), -4)

    def test_faction_bonus(self):
        """FR 50 (T3: Valued) → +2 social."""
        facs = {"Valentinos": {"fr": 50}}
        self.assertEqual(self.compute(None, facs, "Valentinos", "social"), 2)

    def test_faction_negative(self):
        """FR -30 (−T1: Suspicious) → -1 social."""
        facs = {"Maelstrom": {"fr": -30}}
        self.assertEqual(self.compute(None, facs, "Maelstrom", "social"), -1)

    def test_case_insensitive(self):
        """Lookup should be case-insensitive."""
        rels = {"Judy": {"rs": 70}}
        self.assertEqual(self.compute(rels, None, "judy", "social"), 2)
        self.assertEqual(self.compute(rels, None, "JUDY", "persuasion"), 3)

    def test_no_context_uses_all_fallback(self):
        """No check_context → falls back to 'all' key."""
        rels = {"Smasher": {"rs": -70}}  # all: -3
        self.assertEqual(self.compute(rels, None, "Smasher"), -3)

    def test_no_context_no_all_returns_zero(self):
        """No check_context and no 'all' key → 0."""
        rels = {"Judy": {"rs": 25}}  # persuasion: 1 only
        self.assertEqual(self.compute(rels, None, "Judy"), 0)

    def test_social_bonus_does_not_apply_to_combat(self):
        """RS 55 (T4: +2 social) should NOT give +2 on combat checks."""
        rels = {"Judy": {"rs": 55}}
        self.assertEqual(self.compute(rels, None, "Judy", "combat"), 0)

    def test_all_penalty_does_apply_to_combat(self):
        """RS -70 (−T5: -3 all) SHOULD give -3 on combat checks."""
        rels = {"Smasher": {"rs": -70}}
        self.assertEqual(self.compute(rels, None, "Smasher", "combat"), -3)

    def test_social_fallback_works_for_interrogation(self):
        """Social-adjacent contexts like interrogation should fall back to social bonus."""
        rels = {"Judy": {"rs": 55}}  # social: +2
        self.assertEqual(self.compute(rels, None, "Judy", "interrogation"), 2)


class TestResolveActionsAutoRelBonus(unittest.TestCase):
    """Tests that resolve_actions auto-computes rel_bonus from relationships."""

    def test_skill_check_auto_bonus(self):
        """Skill check with target + relationships should auto-apply bonus."""
        rels = {"Judy": {"rs": 70}}  # +2 social, +3 persuasion
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "V", "stat_value": 6,
                  "skill_value": 4, "dv": 13, "target": "Judy", "check_context": "persuasion"}],
                relationships=rels,
            )
        # 5 + 6 + 4 + 3(rel) = 18 > 13
        self.assertEqual(result["results"][0]["total"], 18)
        self.assertTrue(result["results"][0]["success"])

    def test_skill_check_no_target_no_bonus(self):
        """Skill check without target should not apply relationship bonus."""
        rels = {"Judy": {"rs": 70}}
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "V", "stat_value": 6,
                  "skill_value": 4, "dv": 13}],
                relationships=rels,
            )
        self.assertEqual(result["results"][0]["total"], 15)

    def test_backward_compat_no_relationships(self):
        """Without relationships dict, manual rel_bonus still works."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "V", "stat_value": 6,
                  "skill_value": 4, "dv": 13, "rel_bonus": 2}],
            )
        self.assertEqual(result["results"][0]["total"], 17)

    def test_negative_rel_bonus_applied(self):
        """Negative relationship bonus should reduce the total."""
        rels = {"Smasher": {"rs": -70}}  # all: -3
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions(
                [{"type": "skill_check", "character": "V", "stat_value": 6,
                  "skill_value": 4, "dv": 13, "target": "Smasher", "check_context": "social"}],
                relationships=rels,
            )
        # 7 + 6 + 4 + (-3) = 14 > 13
        self.assertEqual(result["results"][0]["total"], 14)
        self.assertTrue(result["results"][0]["success"])


class TestResolveOpposedCheck(unittest.TestCase):
    """Tests for resolve_opposed_check — both sides roll d10 + stat."""

    def test_attacker_wins(self):
        """Attacker with higher total wins."""
        # Attacker rolls 8, defender rolls 3
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3]):
            result = resolve_opposed_check(attacker_stat=6, defender_stat=4)
        self.assertEqual(result["attacker_total"], 14)  # 8+6
        self.assertEqual(result["defender_total"], 7)    # 3+4
        self.assertTrue(result["success"])
        self.assertEqual(result["margin"], 7)
        self.assertIn("✓", result["formatted"])

    def test_defender_wins(self):
        """Defender with higher total wins."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[3, 8]):
            result = resolve_opposed_check(attacker_stat=4, defender_stat=6)
        self.assertFalse(result["success"])
        self.assertIn("✗", result["formatted"])

    def test_tie_goes_to_defender(self):
        """Equal totals should favor defender."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[5, 5]):
            result = resolve_opposed_check(attacker_stat=6, defender_stat=6)
        self.assertEqual(result["attacker_total"], 11)
        self.assertEqual(result["defender_total"], 11)
        self.assertFalse(result["success"])

    def test_exploding_10_attacker(self):
        """Attacker rolling 10 should explode."""
        # Attacker: 10 (explode) + 5 = 15 + stat 4 = 19
        # Defender: 6 + stat 4 = 10
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[10, 5, 6]):
            result = resolve_opposed_check(attacker_stat=4, defender_stat=4)
        self.assertEqual(result["attacker_total"], 19)
        self.assertTrue(result["success"])

    def test_fumble_1_defender(self):
        """Defender rolling 1 should fumble."""
        # Attacker: 5 + stat 4 = 9
        # Defender: 1 (fumble) - 7 = -6 + stat 8 = 2
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[5, 1, 7]):
            result = resolve_opposed_check(attacker_stat=4, defender_stat=8)
        self.assertEqual(result["defender_total"], 2)
        self.assertTrue(result["success"])

    def test_custom_labels(self):
        """Custom labels appear in formatted output."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[5, 5]):
            result = resolve_opposed_check(
                attacker_stat=6, defender_stat=4,
                attacker_label="Interface", defender_label="ICE DEF")
        self.assertIn("Interface", result["formatted"])
        self.assertIn("ICE DEF", result["formatted"])


class TestResolveProgramAttack(unittest.TestCase):
    """Tests for resolve_program_attack — opposed check + damage."""

    def test_hit_and_damage(self):
        """Hit should roll damage and reduce REZ."""
        # Opposed: atk d10=8, def d10=3 → hit
        # Damage: 2d6 = [4, 5] = 9
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3, 4, 5]):
            result = resolve_program_attack(
                interface_rank=6, program_atk=4, target_def=5,
                program_damage_dice=2, target_rez=15)
        self.assertTrue(result["hit"])
        self.assertEqual(result["damage_total"], 9)
        self.assertEqual(result["rez_remaining"], 6)
        self.assertFalse(result["derezzed"])

    def test_miss_no_damage(self):
        """Miss should not roll damage."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[2, 9]):
            result = resolve_program_attack(
                interface_rank=4, program_atk=2, target_def=8,
                program_damage_dice=2, target_rez=15)
        self.assertFalse(result["hit"])
        self.assertEqual(result["damage_total"], 0)
        self.assertEqual(result["rez_remaining"], 15)

    def test_derez(self):
        """Damage exceeding REZ should derez the target."""
        # Opposed: hit. Damage: 3d6 = [6,6,6] = 18 vs 10 REZ
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 2, 6, 6, 6]):
            result = resolve_program_attack(
                interface_rank=6, program_atk=4, target_def=3,
                program_damage_dice=3, target_rez=10)
        self.assertTrue(result["hit"])
        self.assertTrue(result["derezzed"])
        self.assertEqual(result["rez_remaining"], 0)
        self.assertIn("DEREZZED", result["formatted"])

    def test_custom_names_in_formatted(self):
        """Program and target names should appear in formatted output."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3, 4]):
            result = resolve_program_attack(
                interface_rank=6, program_atk=2, target_def=4,
                program_damage_dice=1, target_rez=10,
                program_name="Sword", target_name="Hellhound")
        self.assertIn("Sword", result["formatted"])
        self.assertIn("Hellhound", result["formatted"])


class TestResolveActionsNET(unittest.TestCase):
    """Tests for resolve_actions dispatch of NET action types."""

    def test_opposed_check_via_batch(self):
        """resolve_actions should dispatch opposed_check type."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[7, 4]):
            result = resolve_actions([{
                "type": "opposed_check",
                "character": "V",
                "attacker_stat": 6,
                "defender_stat": 4,
            }])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["type"], "opposed_check")
        self.assertTrue(result["results"][0]["success"])

    def test_program_attack_via_batch(self):
        """resolve_actions should dispatch program_attack type."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3, 4, 5]):
            result = resolve_actions([{
                "type": "program_attack",
                "character": "V",
                "interface_rank": 6,
                "program_atk": 4,
                "target_def": 5,
                "program_damage_dice": 2,
                "target_rez": 15,
                "target": "Hellhound",
            }])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["type"], "program_attack")
        self.assertTrue(result["results"][0]["hit"])


class TestAmmoStateOps(unittest.TestCase):
    """Tests for ammo state_ops from ranged_attack and autofire."""

    def test_ranged_attack_emits_ammo_op(self):
        """Ranged attack with weapon_name should emit ammo state_op."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_ranged_attack(
                stat_value=8, skill_value=6, weapon_type="Pistol",
                damage_dice=2, rof=2, target_sp=0, range_bracket=0,
                target_name="Ganger", character_name="V",
                weapon_name="Malorian Arms 3516",
            )
        ammo_ops = [op for op in result["state_ops"] if op.get("op") == "ammo"]
        self.assertEqual(len(ammo_ops), 1)
        self.assertEqual(ammo_ops[0]["edgerunner"], "V")
        self.assertEqual(ammo_ops[0]["weapon_name"], "Malorian Arms 3516")
        self.assertEqual(ammo_ops[0]["rounds_consumed"], 2)

    def test_ranged_attack_no_weapon_name_no_ammo_op(self):
        """Ranged attack without weapon_name should NOT emit ammo state_op."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_ranged_attack(
                stat_value=8, skill_value=6, weapon_type="Pistol",
                damage_dice=2, rof=1, target_sp=0, range_bracket=0,
                target_name="Ganger",
            )
        ammo_ops = [op for op in result["state_ops"] if op.get("op") == "ammo"]
        self.assertEqual(len(ammo_ops), 0)

    def test_autofire_emits_ammo_op(self):
        """Autofire with weapon_name should emit ammo state_op for 10 rounds."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_autofire(
                stat_value=8, skill_value=6, weapon_type="SMG",
                autofire_multiplier=3, target_sp=7, range_bracket=0,
                target_name="Ganger", character_name="V",
                weapon_name="Militech Crusher",
            )
        ammo_ops = [op for op in result["state_ops"] if op.get("op") == "ammo"]
        self.assertEqual(len(ammo_ops), 1)
        self.assertEqual(ammo_ops[0]["rounds_consumed"], 10)
        self.assertEqual(ammo_ops[0]["weapon_name"], "Militech Crusher")

    def test_resolve_actions_passes_weapon_name(self):
        """resolve_actions should pass weapon_name to ranged_attack."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions([{
                "type": "ranged_attack",
                "character": "V",
                "stat_value": 8,
                "skill_value": 6,
                "weapon_type": "Pistol",
                "damage_dice": 2,
                "rof": 1,
                "target": "Ganger",
                "target_sp": 0,
                "range_bracket": 0,
                "weapon_name": "Malorian Arms 3516",
            }])
        ammo_ops = [op for op in result["state_ops"] if op.get("op") == "ammo"]
        self.assertEqual(len(ammo_ops), 1)
        self.assertEqual(ammo_ops[0]["weapon_name"], "Malorian Arms 3516")


class TestSequentialResolution(unittest.TestCase):
    """Tests for sequential resolution with HP tracking and casualty skipping."""

    def test_eliminated_actor_skipped(self):
        """Actor at 0 HP should be skipped."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions(
                [
                    {"type": "skill_check", "character": "DeadGuy",
                     "stat_value": 6, "skill_value": 4, "dv": 13},
                ],
                sequential=True,
                combatant_hp={"DeadGuy": 0},
            )
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0].get("skipped"))
        self.assertEqual(result["results"][0]["reason"], "eliminated")

    def test_sequential_hp_tracking(self):
        """Target killed by first action should cause second action by that target to be skipped."""
        # First action: ranged attack that kills Ganger (20 HP, 0 SP)
        # Second action: Ganger attacks (should be skipped because eliminated)
        rolls = [8, 6, 6, 6, 6, 6, 1, 2]  # attack d10, 5d6 damage dice, 2d6 crit lookup
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_actions(
                [
                    {
                        "type": "ranged_attack", "character": "V",
                        "stat_value": 8, "skill_value": 8,
                        "weapon_type": "Assault Rifle", "damage_dice": 5,
                        "rof": 1, "target": "Ganger", "target_sp": 0,
                        "range_bracket": 0,
                    },
                    {
                        "type": "skill_check", "character": "Ganger",
                        "stat_value": 6, "skill_value": 4, "dv": 13,
                    },
                ],
                sequential=True,
                combatant_hp={"V": 40, "Ganger": 20},
            )
        # First action should succeed
        self.assertFalse(result["results"][0].get("skipped", False))
        # Second action (Ganger) should be skipped
        self.assertTrue(result["results"][1].get("skipped"))
        self.assertEqual(result["results"][1]["reason"], "eliminated")

    def test_non_sequential_no_skip(self):
        """With sequential=False, no skipping should occur."""
        with patch("game_systems.cpred_mechanics.random.randint", return_value=7):
            result = resolve_actions(
                [
                    {"type": "skill_check", "character": "DeadGuy",
                     "stat_value": 6, "skill_value": 4, "dv": 13},
                ],
                sequential=False,
                combatant_hp={"DeadGuy": 0},
            )
        self.assertFalse(result["results"][0].get("skipped", False))


class TestAmbushResolution(unittest.TestCase):
    """Tests for ambush action type."""

    def test_ambush_surprised_targets(self):
        """Ambush should resolve Stealth vs Perception for each target."""
        # Two opposed checks: ambusher d10=8, target1 d10=3 (surprise), target2 d10=9 (not surprised)
        rolls = [8, 3, 5, 9]
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_actions([{
                "type": "ambush",
                "character": "V",
                "stealth_stat": 6,
                "stealth_skill": 4,
                "targets": [
                    {"name": "Ganger1", "perception_stat": 4, "perception_skill": 2},
                    {"name": "Ganger2", "perception_stat": 6, "perception_skill": 4},
                ],
            }])
        self.assertEqual(len(result["results"]), 1)
        ambush = result["results"][0]
        self.assertEqual(ambush["type"], "ambush")
        self.assertEqual(len(ambush["results"]), 2)
        self.assertTrue(ambush["results"][0]["surprised"])   # Ganger1 surprised
        self.assertFalse(ambush["results"][1]["surprised"])   # Ganger2 not surprised

    def test_initiative_with_surprised(self):
        """Initiative with surprised list should mark those combatants."""
        rolls = [5, 5, 5, 7, 3]  # 3 d10s for init + 2 tiebreak
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=rolls):
            result = resolve_actions([{
                "type": "initiative",
                "character": "all",
                "combatants": [
                    {"name": "V", "ref": 8},
                    {"name": "Ganger1", "ref": 5},
                    {"name": "Ganger2", "ref": 5},
                ],
                "surprised": ["Ganger1"],
            }])
        init_result = result["results"][0]
        self.assertEqual(init_result["type"], "initiative")
        surprised_names = [e["name"] for e in init_result["order"] if e.get("surprised")]
        self.assertIn("Ganger1", surprised_names)
        self.assertNotIn("V", surprised_names)


class TestBrainDamageStateOps(unittest.TestCase):
    """Tests for brain_damage state_ops from program_attack_vs_netrunner."""

    def test_brain_damage_op_emitted(self):
        """program_attack_vs_netrunner that hits should emit brain_damage state_op."""
        # Opposed: atk d10=8, def d10=3 → hit. Damage: 2d6 = 4+5 = 9
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3, 4, 5]):
            result = resolve_actions([{
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "interface_rank": 0,
                "program_atk": 6,
                "target_def": 4,
                "program_damage_dice": 2,
                "target_rez": 99,
                "program_name": "Hellhound",
                "target": "V",
            }])
        bd_ops = [op for op in result["state_ops"] if op.get("op") == "brain_damage"]
        self.assertEqual(len(bd_ops), 1)
        self.assertEqual(bd_ops[0]["edgerunner"], "V")
        self.assertEqual(bd_ops[0]["change"], 9)

    def test_brain_damage_miss_no_op(self):
        """program_attack_vs_netrunner that misses should NOT emit brain_damage state_op."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[2, 9]):
            result = resolve_actions([{
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "interface_rank": 0,
                "program_atk": 4,
                "target_def": 8,
                "program_damage_dice": 2,
                "target_rez": 99,
                "target": "V",
            }])
        bd_ops = [op for op in result["state_ops"] if op.get("op") == "brain_damage"]
        self.assertEqual(len(bd_ops), 0)

    def test_black_ice_attack_does_not_use_netrunner_interface(self):
        """interface_rank in action should not buff ICE attack rolls."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[9, 2]):
            result = resolve_actions([{
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "interface_rank": 10,  # defender stat; must not be applied to attacker
                "program_atk": 1,
                "target_def": 10,
                "program_damage_dice": 2,
                "target": "V",
            }])
        self.assertFalse(result["results"][0]["hit"])

    def test_black_ice_hit_is_not_reported_as_derez(self):
        """Netrunner hits should report brain damage, not REZ/derez semantics."""
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=[8, 3, 4, 5]):
            result = resolve_actions([{
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "interface_rank": 0,
                "program_atk": 6,
                "target_def": 4,
                "program_damage_dice": 2,
                "target_rez": 0,
                "program_name": "Hellhound",
                "target": "V",
            }])
        hit_result = result["results"][0]
        self.assertTrue(hit_result["hit"])
        self.assertFalse(hit_result["derezzed"])
        self.assertIn("brain damage", hit_result["formatted"])
        self.assertNotIn("DEREZZED", hit_result["formatted"])


class TestNewTableCompleteness(unittest.TestCase):
    """Validate structural completeness of new reference tables."""

    def test_program_stats_fields(self):
        from game_systems.cpred_tables import PROGRAM_STATS
        valid_categories = {"booster", "defender", "attacker"}
        for name, p in PROGRAM_STATS.items():
            self.assertIn(p["category"], valid_categories, f"{name} bad category")
            for field in ("atk", "def", "rez", "effect", "cost"):
                self.assertIn(field, p, f"{name} missing {field}")

    def test_cyberdeck_stats(self):
        from game_systems.cpred_tables import CYBERDECK_STATS
        for quality in ("Poor", "Standard", "Excellent"):
            self.assertIn(quality, CYBERDECK_STATS)
            for field in ("slots", "cycles", "cost"):
                self.assertIn(field, CYBERDECK_STATS[quality])

    def test_cyberware_table_ceiling_costs(self):
        from game_systems.cpred_tables import CYBERWARE_TABLE
        for name, cw in CYBERWARE_TABLE.items():
            self.assertIn("ceiling_cost", cw, f"{name} missing ceiling_cost")
            if cw["category"] == "borgware":
                self.assertEqual(cw["ceiling_cost"], 4, f"{name} borgware should be 4")
        # Medical exempt
        self.assertEqual(CYBERWARE_TABLE["Contraceptive Implant"]["ceiling_cost"], 0)

    def test_skill_stat_map_valid_stats(self):
        from game_systems.cpred_tables import SKILL_STAT_MAP, X2_SKILLS
        valid_stats = {"INT", "WILL", "COOL", "EMP", "TECH", "REF", "DEX", "BODY", "MOVE", "LUCK"}
        for skill, stat in SKILL_STAT_MAP.items():
            self.assertIn(stat, valid_stats, f"{skill} has invalid stat {stat}")
        # X2 skills should be a subset of the skill map
        for skill in X2_SKILLS:
            self.assertIn(skill, SKILL_STAT_MAP, f"X2 skill {skill} not in SKILL_STAT_MAP")

    def test_netrunner_actions_per_rank(self):
        from game_systems.cpred_tables import NETRUNNER_ACTIONS_PER_RANK
        for rank in range(1, 11):
            self.assertIn(rank, NETRUNNER_ACTIONS_PER_RANK, f"rank {rank} missing")

    def test_ip_cost_tables(self):
        from game_systems.cpred_tables import IP_COST_TABLES
        for tier in ("typical", "difficult", "role"):
            self.assertIn(tier, IP_COST_TABLES)
            for level in range(1, 11):
                self.assertIn(level, IP_COST_TABLES[tier], f"{tier} missing level {level}")


class TestHumanityCeiling(unittest.TestCase):
    """Test humanity ceiling enforcement on cyberware add/remove."""

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
        from game_systems.cpred import apply_game_state
        agent_json = {"edgerunner_ops": ops}
        apply_game_state(game_state, agent_json, 1)

    def test_add_standard_cyberware_reduces_max(self):
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Neural Link"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)

    def test_add_borgware_reduces_max_by_4(self):
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Linear Frame Sigma"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 56)

    def test_add_medical_no_ceiling_change(self):
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Contraceptive Implant"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_add_alias_variant_deduplicated(self):
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cyberaudio Suite"}])
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "cyberaudio"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], ["Cyberaudio Suite"])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)

    def test_add_parenthetical_variants_remain_distinct(self):
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye (Left)"}])
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye (Right)"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], ["Cybereye (Left)", "Cybereye (Right)"])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 56)

    def test_current_clamped_when_max_drops(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=59, humanity_max=60)}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Neural Link"}])
        # max is 58, current was 59, should clamp to 58
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 58)

    def test_remove_restores_max(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Neural Link"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)
        # Current should NOT auto-raise (therapy still required)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 50)

    def test_remove_parenthetical_variant_restores_max(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Cybereye (Right)"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_remove_alias_variant_restores_max(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Cyberaudio Suite"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "cyberaudio"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_remove_qualified_variant_removes_correct_entry(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=56)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Cybereye (Left)", "Cybereye (Right)"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye (Right)"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], ["Cybereye (Left)"])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)

    def test_remove_unqualified_variant_with_multiple_matches_is_noop(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=56)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Cybereye (Left)", "Cybereye (Right)"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], ["Cybereye (Left)", "Cybereye (Right)"])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 56)

    def test_remove_unqualified_variant_with_single_match_removes(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Cybereye (Left)"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye"}])
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_remove_capped_at_base_max(self):
        """Removing cyberware cannot push max above the original base."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=59)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Neural Link"}])
        # 59 + 2 = 61, but base_max is 60, so capped to 60
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_therapy_capped_at_ceiling(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=50, humanity_max=58)}}
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        self._apply(gs, [{"edgerunner": "V", "op": "therapy", "change": 20}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 58)

    def test_max_floor_at_zero(self):
        """Humanity max cannot go below 0 even with many cyberware pieces."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=4, humanity_max=4)}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Linear Frame Sigma"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 0)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 0)

    def test_name_matching_with_parenthetical(self):
        """Cyberware names like 'Cybereye (Right)' should match 'Cybereye'."""
        gs = {"edgerunners": {"V": self._make_er()}}
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye (Right)"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)

    def test_name_matching_alias(self):
        """'Cyberaudio' should match 'Cyberaudio Suite'."""
        from game_systems.cpred import _cyberware_ceiling_cost
        self.assertEqual(_cyberware_ceiling_cost("Cyberaudio"), 2)

    def test_migration_applies_ceiling_once(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=60, humanity_max=60)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link", "Linear Frame Sigma"]
        self._apply(gs, [])  # no ops, just trigger migration
        # Migration should preserve existing max and only establish a baseline.
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 60)
        self.assertTrue(gs["edgerunners"]["V"]["_humanity_ceiling_migrated"])
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_ceiling_total"], 6)
        # Second call should NOT reduce again
        self._apply(gs, [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_migration_then_add_no_double_count(self):
        """Pre-existing cyberware migrated, then new cyberware added in same call."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=60, humanity_max=60)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link"]
        # Migration preserves existing max, then add applies delta for the new install.
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)

    def test_bootstrap_via_set_applies_ceiling(self):
        """Cyberware added via 'set' op should get ceiling applied in the same call."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=60, humanity_max=60)}}
        self._apply(gs, [{"edgerunner": "V", "op": "set", "fields": {
            "humanity": {"current": 60, "max": 60},
            "cyberware_effects": ["Neural Link", "Interface Plugs"],
        }}])
        # Set with explicit humanity max should rebase and preserve that max.
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_ceiling_total"], 4)

    def test_set_replaces_cyberware_and_recomputes_ceiling_for_migrated_character(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=56, humanity_max=56)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link", "Cybereye"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [{"edgerunner": "V", "op": "set", "fields": {
            "cyberware_effects": ["Linear Frame Sigma"],
        }}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 56)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 56)

    def test_migration_ignores_non_string_cyberware_entries(self):
        gs = {"edgerunners": {"V": self._make_er(humanity_current=60, humanity_max=60)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = [None, "Neural Link", 123, ""]
        self._apply(gs, [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["current"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["cyberware_effects"], ["Neural Link"])

    def test_set_humanity_max_rebases_baseline_without_cyberware_set(self):
        """Changing humanity max via set must update baseline used by later cyberware ops."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=56, humanity_max=56)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link", "Cybereye"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        gs["edgerunners"]["V"]["_humanity_base_ceiling_total"] = 4
        self._apply(gs, [{"edgerunner": "V", "op": "set", "fields": {
            "humanity": {"current": 80, "max": 80},
        }}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 80)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_max"], 80)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_ceiling_total"], 4)
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Interface Plugs"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 78)

    def test_set_echo_humanity_max_does_not_rebase_baseline(self):
        """Echoing unchanged humanity.max in set should not lower baseline metadata."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=40, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link"]
        gs["edgerunners"]["V"]["_humanity_ceiling_migrated"] = True
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        gs["edgerunners"]["V"]["_humanity_base_ceiling_total"] = 0
        self._apply(gs, [{"edgerunner": "V", "op": "set", "fields": {
            "humanity": {"current": 40, "max": 58},
        }}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_ceiling_total"], 0)
        self._apply(gs, [{"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Neural Link"}])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 60)

    def test_migration_preserves_legacy_base_max_semantics(self):
        """Legacy entries with only _humanity_base_max should not inflate on migration."""
        gs = {"edgerunners": {"V": self._make_er(humanity_current=40, humanity_max=58)}}
        gs["edgerunners"]["V"]["cyberware_effects"] = ["Neural Link"]
        gs["edgerunners"]["V"]["_humanity_base_max"] = 60
        self._apply(gs, [])
        self.assertEqual(gs["edgerunners"]["V"]["humanity"]["max"], 58)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_max"], 60)
        self.assertEqual(gs["edgerunners"]["V"]["_humanity_base_ceiling_total"], 0)


MOCK = "game_systems.cpred_mechanics.random.randint"


class TestResolveDrivingCheck(unittest.TestCase):
    """Tests for driving check resolution."""

    @patch(MOCK, return_value=7)
    def test_maintain_control_success(self, _m):
        r = resolve_driving_check(stat_value=6, skill_value=4, maneuver="maintain_control")
        self.assertTrue(r["success"])  # 7+6+4=17 > DV10
        self.assertFalse(r["control_lost"])
        self.assertEqual(r["maneuver"], "maintain_control")

    @patch(MOCK, return_value=2)
    def test_bootleg_turn_failure(self, _m):
        r = resolve_driving_check(stat_value=5, skill_value=4, maneuver="bootleg_turn")
        self.assertFalse(r["success"])  # 2+5+4=11 < DV17
        self.assertTrue(r["control_lost"])

    @patch(MOCK, return_value=5)
    def test_unknown_maneuver_returns_error(self, _m):
        r = resolve_driving_check(stat_value=4, skill_value=4, maneuver="barrel_roll")
        self.assertIn("error", r)
        self.assertFalse(r["success"])
        self.assertIsNone(r["dv"])

    @patch(MOCK, return_value=5)
    def test_luck_modifies_result(self, _m):
        r = resolve_driving_check(stat_value=4, skill_value=4, maneuver="swerve", luck_spent=2)
        # 5+4+4+2=15 > DV13
        self.assertTrue(r["success"])

    @patch(MOCK, return_value=5)
    def test_maneuver_is_case_insensitive_and_trimmed(self, _m):
        r = resolve_driving_check(stat_value=4, skill_value=4, maneuver="  SwErVe  ", luck_spent=2)
        self.assertTrue(r["success"])
        self.assertEqual(r["maneuver"], "swerve")

    @patch(MOCK, return_value=5)
    def test_string_numeric_inputs_are_coerced(self, _m):
        r = resolve_driving_check(stat_value="4", skill_value="4", maneuver="swerve", luck_spent="2")
        self.assertTrue(r["success"])

    @patch(MOCK, return_value=3)
    def test_seriously_wounded_penalty(self, _m):
        r = resolve_driving_check(stat_value=6, skill_value=4, maneuver="swerve", seriously_wounded=True)
        # 3+6+4-2=11 < DV13
        self.assertFalse(r["success"])

    @patch(MOCK, return_value=7)
    def test_on_outcome_success(self, _m):
        r = resolve_driving_check(stat_value=6, skill_value=4, maneuver="maintain_control",
                                  on_hit="kept control", on_miss="spun out")
        self.assertTrue(r["success"])
        self.assertEqual(r["on_outcome"], "kept control")

    @patch(MOCK, return_value=2)
    def test_on_outcome_failure(self, _m):
        r = resolve_driving_check(stat_value=5, skill_value=4, maneuver="bootleg_turn",
                                  on_hit="pulled it off", on_miss="crashed")
        self.assertFalse(r["success"])
        self.assertEqual(r["on_outcome"], "crashed")


class TestResolveRamming(unittest.TestCase):
    """Tests for ramming resolution."""

    @patch(MOCK, return_value=3)
    def test_ram_pedestrian_no_dodge(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Ganger",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=25, target_sp=7,
            pedestrian_dodge=False,
        )
        self.assertFalse(r["dodged"])
        # 6d6 all 3s = 18
        self.assertEqual(r["ram_damage_total"], 18)
        # Target: 18 - 7 SP = 11 damage
        self.assertEqual(r["target_damage"], 11)
        # Vehicle: 18 - 0 SP = 18 self-damage
        self.assertEqual(r["vehicle_damage"], 18)
        # state_ops: hp for target, vehicle_sdp for vehicle, critical_injuries for target + occupants
        hp_ops = [op for op in r["state_ops"] if op.get("op") == "hp"]
        self.assertEqual(len(hp_ops), 1)
        self.assertEqual(hp_ops[0]["change"], -11)
        # Target gets Whiplash with canonical crit injury format
        ci_ops = [op for op in r["state_ops"] if op.get("op") == "critical_injury"]
        target_whips = [op for op in ci_ops if op["edgerunner"] == "Ganger"]
        self.assertEqual(len(target_whips), 1)
        # Verify canonical fields from _critical_injury_state_op
        w = target_whips[0]
        self.assertEqual(w["action"], "add")
        self.assertEqual(w["name"], "Whiplash")
        self.assertIn("reason", w)

    @patch(MOCK, return_value=4)
    def test_ram_pedestrian_dodge_success(self, _m):
        """Pedestrian successfully dodges — should be early return."""
        # DEX+Evasion=14, DV=13, roll=4 -> 4+14=18 > 13 = success
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Ganger",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=25, target_sp=7,
            pedestrian_dodge=True, pedestrian_dex=8, pedestrian_evasion=6,
        )
        self.assertTrue(r["dodged"])
        self.assertEqual(len(r["state_ops"]), 0)

    @patch(MOCK, return_value=3)
    def test_ram_vehicle_vs_vehicle(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Enemy Car",
            vehicle_sdp_current=50, vehicle_sp=13,
            target_hp_current=0, target_sp=13,
            target_is_vehicle=True, target_sdp_current=50,
            occupants=[{"name": "V"}], target_occupants=[{"name": "Enemy"}],
        )
        # 6d6 all 3s = 18
        self.assertEqual(r["ram_damage_total"], 18)
        # Target: 18 - 13 = 5 SDP damage
        self.assertEqual(r["target_damage"], 5)
        # Vehicle: 18 - 13 = 5 SDP self-damage
        self.assertEqual(r["vehicle_damage"], 5)
        # All occupants get Whiplash (V and Enemy)
        ci_ops = [op for op in r["state_ops"] if op.get("op") == "critical_injury"]
        self.assertEqual(len(ci_ops), 2)
        ci_names = {op["edgerunner"] for op in ci_ops}
        self.assertEqual(ci_names, {"V", "Enemy"})

    @patch(MOCK, return_value=3)
    def test_combat_plow_no_self_damage(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Enemy Car",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=0, target_sp=0,
            target_is_vehicle=True, target_sdp_current=50,
            occupants=[{"name": "V"}], target_occupants=[{"name": "Enemy"}],
            combat_plow=True,
        )
        # Attacker takes NO SDP damage
        self.assertEqual(r["vehicle_damage"], 0)
        # No vehicle_sdp op for attacker
        attacker_sdp_ops = [op for op in r["state_ops"]
                           if op.get("op") == "vehicle_sdp" and op.get("vehicle") == "V's Car"]
        self.assertEqual(len(attacker_sdp_ops), 0)
        # Attacker occupants do NOT get Whiplash
        ci_ops = [op for op in r["state_ops"] if op.get("op") == "critical_injury"]
        attacker_ci = [op for op in ci_ops if op["edgerunner"] == "V"]
        self.assertEqual(len(attacker_ci), 0)
        # Target still takes damage and Whiplash
        target_ci = [op for op in ci_ops if op["edgerunner"] == "Enemy"]
        self.assertEqual(len(target_ci), 1)

    @patch(MOCK, return_value=4)
    def test_combat_plow_nos_boosted(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Enemy",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=0, target_sp=0,
            target_is_vehicle=True, target_sdp_current=50,
            combat_plow=True, nos_boosted=True,
        )
        # 8d6 all 4s = 32
        self.assertEqual(r["ram_damage_dice"], 8)
        self.assertEqual(r["ram_damage_total"], 32)

    @patch(MOCK, return_value=3)
    def test_vehicle_stopped_when_target_survives(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Tank",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=0, target_sp=0,
            target_is_vehicle=True, target_sdp_current=100,
        )
        # 18 < 100 SDP, target survives
        self.assertTrue(r["vehicle_stopped"])

    @patch(MOCK, return_value=6)
    def test_vehicle_not_stopped_when_target_destroyed(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Bike",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=0, target_sp=0,
            target_is_vehicle=True, target_sdp_current=35,
        )
        # 6d6 all 6s = 36 > 35 SDP
        self.assertFalse(r["vehicle_stopped"])

    @patch(MOCK, return_value=6)
    def test_vehicle_stopped_computed_from_string_target_sdp(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Car",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=0, target_sp=0,
            target_is_vehicle=True, target_sdp_current="50",
        )
        self.assertTrue(r["vehicle_stopped"])

    @patch(MOCK, return_value=8)
    def test_string_false_seriously_wounded_pedestrian_is_coerced(self, _m):
        r_false = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=20, target_sp=0,
            pedestrian_dodge=True, pedestrian_dex=4, pedestrian_evasion=2,
            seriously_wounded_pedestrian=False,
        )
        r_str_false = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=20, target_sp=0,
            pedestrian_dodge=True, pedestrian_dex=4, pedestrian_evasion=2,
            seriously_wounded_pedestrian="false",
        )
        self.assertEqual(r_false["dodged"], r_str_false["dodged"])

    @patch(MOCK, return_value=3)
    def test_sp_ablation_both_sides(self, _m):
        r = resolve_ramming(
            vehicle_name="V's Car", target_name="Enemy Car",
            vehicle_sdp_current=50, vehicle_sp=5,
            target_hp_current=0, target_sp=10,
            target_is_vehicle=True, target_sdp_current=50,
        )
        # Both sides penetrate (18 > 10, 18 > 5) -> ablation for both
        sp_ops = [op for op in r["state_ops"] if op.get("op") == "vehicle_sp"]
        self.assertEqual(len(sp_ops), 2)  # one for target, one for attacker

    @patch(MOCK, return_value=3)
    def test_on_outcome_hit(self, _m):
        r = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=25, target_sp=0,
            on_hit="target sent flying", on_miss="target dodged",
        )
        self.assertFalse(r["dodged"])
        self.assertEqual(r["on_outcome"], "target sent flying")

    @patch(MOCK, return_value=3)
    def test_negative_vehicle_sp_is_clamped(self, _m):
        r = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=-5,
            target_hp_current=25, target_sp=0,
        )
        self.assertEqual(r["vehicle_damage"], 18)

    @patch(MOCK, return_value=3)
    def test_pedestrian_ramming_emits_armor_ablation_op(self, _m):
        r = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=25, target_sp=7,
        )
        self.assertEqual(r["target_ablation"], 1)
        armor_ops = [op for op in r["state_ops"] if op.get("op") == "armor" and op.get("edgerunner") == "Ped"]
        self.assertEqual(len(armor_ops), 1)
        self.assertEqual(armor_ops[0]["change"], -1)

    @patch(MOCK, return_value=9)
    def test_on_outcome_dodged(self, _m):
        r = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=25, target_sp=0,
            pedestrian_dodge=True, pedestrian_dex=8, pedestrian_evasion=6,
            on_hit="target sent flying", on_miss="target dodged",
        )
        self.assertTrue(r["dodged"])
        self.assertEqual(r["on_outcome"], "target dodged")

    @patch(MOCK, return_value=3)
    def test_destroyed_attacker_vehicle_skips(self, _m):
        r = resolve_ramming(
            vehicle_name="Wreck", target_name="Ped",
            vehicle_sdp_current=0, vehicle_sp=0,
            target_hp_current=20, target_sp=0,
        )
        self.assertTrue(r.get("skipped"))
        self.assertEqual(r.get("reason"), "vehicle_destroyed")
        self.assertEqual(r["state_ops"], [])


class TestResolveVehicleWeakPoint(unittest.TestCase):
    """Tests for vehicle weak point shot resolution."""

    @patch(MOCK, return_value=9)
    def test_moving_target_hit(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Assault Rifle",
            damage_dice=5, vehicle_sp=13, vehicle_name="Enemy AV",
            range_bracket=2, target_moving=True,
        )
        # Ruleset §18: moving weak point = flat DV13 + aimed −8 = DV21.
        # Roll 9+8+6=23 > 21 → HIT.
        self.assertTrue(r["hit"])

    @patch(MOCK, return_value=5)
    def test_moving_target_miss_flat_dv21(self, _m):
        """Moving weak point DV is flat 21 regardless of weapon/range."""
        r = resolve_vehicle_weak_point(
            stat_value=6, skill_value=4, weapon_type="Pistol",
            damage_dice=2, vehicle_sp=0, vehicle_name="Bike",
            range_bracket=0, target_moving=True,
        )
        # Roll 5+6+4=15 vs DV21 → MISS.  Old bug would have used
        # Pistol range-0 DV13 + 8 = DV21 too (coincidence), so also test
        # a range bracket where old DV would have differed.
        self.assertFalse(r["hit"])

    @patch(MOCK, return_value=5)
    def test_moving_target_uses_flat_dv_not_range_table(self, _m):
        """Confirm DV doesn't scale with range — always flat DV21."""
        # Sniper at bracket 4 (51-100m): range DV=15, old code would give 15+8=23.
        # Fixed code: flat 13+8=21. Roll 5+8+8=21, must BEAT → 21>21 fails.
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=8, weapon_type="Sniper Rifle",
            damage_dice=5, vehicle_sp=0, vehicle_name="Van",
            range_bracket=4, target_moving=True,
        )
        # With old range-based DV: DV23, 21 < 23 → miss.
        # With fixed flat DV21: 21 > 21 → still miss (tied).
        self.assertFalse(r["hit"])
        # Now test that at bracket 5 (101-200m, sniper DV=16), where old code
        # would give DV24 but correct is still DV21:
        r2 = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Sniper Rifle",
            damage_dice=5, vehicle_sp=0, vehicle_name="Van",
            range_bracket=5, target_moving=True,
        )
        # Roll 5+8+6=19 vs DV21 → miss under both systems, but DV in result
        # should be 21 not 24.
        self.assertFalse(r2["hit"])
        self.assertEqual(r2["attack_roll"]["dv"], 21)

    @patch(MOCK, return_value=4)
    def test_stationary_auto_hit(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Assault Rifle",
            damage_dice=5, vehicle_sp=13, vehicle_name="Parked Car",
            target_moving=False,
        )
        self.assertTrue(r["hit"])
        self.assertIsNone(r["attack_roll"])
        # 5d6 all 4s = 20, SP 13, past = 7, doubled = 14
        self.assertEqual(r["raw_damage"], 20)
        self.assertEqual(r["damage_past_sp"], 7)
        self.assertEqual(r["doubled_damage"], 14)

    @patch(MOCK, return_value=4)
    def test_ap_ammo_ablation(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Assault Rifle",
            damage_dice=5, vehicle_sp=13, vehicle_name="Car",
            target_moving=False, is_ap=True,
        )
        self.assertEqual(r["ablation"], 2)

    @patch(MOCK, return_value=4)
    def test_string_false_is_ap_treated_as_non_ap(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Assault Rifle",
            damage_dice=5, vehicle_sp=13, vehicle_name="Car",
            target_moving=False, is_ap="false",
        )
        self.assertEqual(r["ablation"], 1)

    @patch(MOCK, return_value=4)
    def test_weapon_type_is_case_insensitive_and_trimmed(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="  assault rifle  ",
            damage_dice=5, vehicle_sp=13, vehicle_name="Car",
            target_moving=False,
        )
        self.assertTrue(r["hit"])
        self.assertNotIn("error", r)

    @patch(MOCK, return_value=6)
    def test_ramming_string_numeric_dodge_stats_do_not_error(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "Ped",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 0,
            "target_hp_current": 20,
            "target_sp": 0,
            "pedestrian_dodge": True,
            "pedestrian_dex": "8",
            "pedestrian_evasion": "6",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 0})
        self.assertEqual(r["results"][0]["type"], "ramming")
        self.assertNotIn("error", r["results"][0])

    @patch(MOCK, return_value=4)
    def test_damage_blocked_by_armor(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Pistol",
            damage_dice=2, vehicle_sp=13, vehicle_name="Tank",
            target_moving=False,
        )
        # 2d6 all 4s = 8 < 13 SP -> 0 past, doubled = 0
        self.assertEqual(r["damage_past_sp"], 0)
        self.assertEqual(r["doubled_damage"], 0)
        self.assertEqual(r["ablation"], 0)

    @patch(MOCK, return_value=4)
    def test_on_outcome_hit(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Pistol",
            damage_dice=3, vehicle_sp=5, vehicle_name="Car",
            target_moving=False, on_hit="car explodes", on_miss="missed",
        )
        self.assertTrue(r["hit"])
        self.assertEqual(r["on_outcome"], "car explodes")

    @patch(MOCK, return_value=4)
    def test_state_ops_on_hit(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8, skill_value=6, weapon_type="Assault Rifle",
            damage_dice=5, vehicle_sp=10, vehicle_name="Van",
            target_moving=False, character_name="V",
        )
        # 5d6 all 4s = 20, SP 10, past = 10, doubled = 20
        sdp_ops = [op for op in r["state_ops"] if op.get("op") == "vehicle_sdp"]
        self.assertEqual(len(sdp_ops), 1)
        self.assertEqual(sdp_ops[0]["change"], -20)
        sp_ops = [op for op in r["state_ops"] if op.get("op") == "vehicle_sp"]
        self.assertEqual(len(sp_ops), 1)
        self.assertEqual(sp_ops[0]["change"], -1)

    @patch(MOCK, return_value=10)
    def test_invalid_weapon_range_returns_error(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=10, skill_value=10, weapon_type="Invalid Weapon",
            damage_dice=6, vehicle_sp=0, vehicle_name="Car",
            range_bracket=0, target_moving=True,
        )
        self.assertFalse(r["hit"])
        self.assertIn("error", r)
        self.assertIn("cannot fire at range bracket", r["error"])
        self.assertEqual(r["state_ops"], [])

    @patch(MOCK, return_value=6)
    def test_stationary_target_invalid_range_still_errors(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8,
            skill_value=8,
            weapon_type="Pistol",
            damage_dice=4,
            vehicle_sp=10,
            vehicle_name="Parked Car",
            range_bracket=7,  # Pistol has no DV at this bracket
            target_moving=False,
        )
        self.assertFalse(r["hit"])
        self.assertIn("error", r)
        self.assertEqual(r["state_ops"], [])

    @patch(MOCK, return_value=6)
    def test_invalid_range_type_returns_error_not_exception(self, _m):
        r = resolve_vehicle_weak_point(
            stat_value=8,
            skill_value=8,
            weapon_type="Pistol",
            damage_dice=4,
            vehicle_sp=10,
            vehicle_name="Parked Car",
            range_bracket="far",
            target_moving=False,
        )
        self.assertFalse(r["hit"])
        self.assertIn("error", r)
        self.assertIn("Invalid range bracket", r["error"])


class TestResolveSpikeStrip(unittest.TestCase):
    """Tests for spike strip resolution."""

    @patch(MOCK, return_value=8)
    def test_driver_succeeds_check(self, _m):
        r = resolve_spike_strip(
            target_driver_name="Enemy Driver",
            stat_value=6, skill_value=6,
            target_vehicle_name="Enemy Car", target_vehicle_sp=13,
        )
        # 8+6+6=20 > DV17 = success
        self.assertTrue(r["check_result"]["success"])
        self.assertFalse(r["hit"])
        self.assertEqual(len(r["state_ops"]), 0)

    @patch(MOCK, return_value=3)
    def test_driver_fails_check(self, _m):
        r = resolve_spike_strip(
            target_driver_name="Enemy Driver",
            stat_value=5, skill_value=4,
            target_vehicle_name="Enemy Car", target_vehicle_sp=10,
        )
        # 3+5+4=12 < DV17 = fail
        self.assertFalse(r["check_result"]["success"])
        self.assertTrue(r["hit"])
        # 4d6 all 3s = 12, SP 10, past = 2, doubled = 4
        self.assertEqual(r["raw_damage"], 12)
        self.assertEqual(r["damage_past_sp"], 2)
        self.assertEqual(r["doubled_damage"], 4)
        self.assertEqual(r["ablation"], 1)
        sdp_ops = [op for op in r["state_ops"] if op.get("op") == "vehicle_sdp"]
        self.assertEqual(len(sdp_ops), 1)
        self.assertEqual(sdp_ops[0]["change"], -4)

    @patch(MOCK, return_value=8)
    def test_on_outcome_avoided(self, _m):
        r = resolve_spike_strip(
            target_driver_name="Driver", stat_value=6, skill_value=6,
            target_vehicle_name="Car", target_vehicle_sp=10,
            on_hit="tires shredded", on_miss="swerved clear",
        )
        self.assertFalse(r["hit"])
        self.assertEqual(r["on_outcome"], "swerved clear")

    @patch(MOCK, return_value=2)
    def test_on_outcome_hit(self, _m):
        r = resolve_spike_strip(
            target_driver_name="Driver", stat_value=4, skill_value=3,
            target_vehicle_name="Car", target_vehicle_sp=5,
            on_hit="tires shredded", on_miss="swerved clear",
        )
        self.assertTrue(r["hit"])
        self.assertEqual(r["on_outcome"], "tires shredded")

    @patch(MOCK, return_value=8)
    def test_non_land_vehicle_rejected(self, _m):
        r = resolve_spike_strip(
            target_driver_name="Pilot", stat_value=8, skill_value=8,
            target_vehicle_name="AV-4", target_vehicle_sp=13,
            target_vehicle_type="air",
        )
        self.assertFalse(r["hit"])
        self.assertIn("error", r)
        self.assertIn("land vehicles only", r["formatted"])
        self.assertEqual(r["state_ops"], [])


class TestResolveActionsVehicle(unittest.TestCase):
    """Tests that vehicle action types dispatch through resolve_actions()."""

    @patch(MOCK, return_value=7)
    def test_driving_check_dispatches(self, _m):
        r = resolve_actions([{
            "type": "driving_check", "character": "V",
            "vehicle_name": "Car",
            "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
        }])
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["type"], "driving_check")
        self.assertTrue(r["results"][0]["success"])

    @patch(MOCK, return_value=3)
    def test_ramming_dispatches(self, _m):
        r = resolve_actions([{
            "type": "ramming", "character": "V",
            "vehicle_name": "Car", "target": "Bike",
            "vehicle_sdp_current": 50, "vehicle_sp": 0,
            "target_hp_current": 0, "target_sp": 0,
            "target_is_vehicle": True, "target_sdp_current": 35,
        }])
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["type"], "ramming")
        # state_ops should have vehicle_sdp ops
        vsdp_ops = [op for op in r["state_ops"] if op.get("op") == "vehicle_sdp"]
        self.assertGreater(len(vsdp_ops), 0)

    @patch(MOCK, return_value=4)
    def test_vehicle_weak_point_dispatches(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point", "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "Car",
            "target_moving": False,
        }])
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["type"], "vehicle_weak_point")

    @patch(MOCK, return_value=3)
    def test_spike_strip_dispatches(self, _m):
        r = resolve_actions([{
            "type": "spike_strip", "character": "V",
            "target_driver": "Enemy", "target_stat_value": 5,
            "target_skill_value": 4, "target_vehicle_name": "Enemy Car",
            "target_vehicle_sp": 10,
        }])
        self.assertEqual(len(r["results"]), 1)
        self.assertEqual(r["results"][0]["type"], "spike_strip")

    @patch(MOCK, return_value=7)
    def test_invalid_vehicle_weak_point_does_not_spend_luck(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Invalid Weapon", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "Car",
            "range_bracket": 0, "target_moving": True,
            "luck_spent": 2,
        }])
        self.assertIn("error", r["results"][0])
        luck_ops = [op for op in r["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(luck_ops, [])

    @patch(MOCK, return_value=6)
    def test_stationary_vehicle_weak_point_does_not_spend_luck(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "Car",
            "target_moving": False,
            "luck_spent": 2,
        }])
        self.assertTrue(r["results"][0]["hit"])
        self.assertIsNone(r["results"][0]["attack_roll"])
        luck_ops = [op for op in r["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(luck_ops, [])

    @patch(MOCK, return_value=7)
    def test_invalid_driving_check_does_not_spend_luck(self, _m):
        r = resolve_actions([{
            "type": "driving_check", "character": "V",
            "vehicle_name": "Car",
            "stat_value": 6, "skill_value": 4, "maneuver": "unknown_stunt",
            "luck_spent": 2,
        }])
        self.assertIn("error", r["results"][0])
        luck_ops = [op for op in r["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(luck_ops, [])

    @patch(MOCK, return_value=7)
    def test_non_numeric_luck_spent_does_not_duplicate_driving_result(self, _m):
        r = resolve_actions([{
            "type": "driving_check", "character": "V",
            "vehicle_name": "Car",
            "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            "luck_spent": "abc",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 10})
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])
        luck_ops = [op for op in r["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(luck_ops, [])

    @patch(MOCK, return_value=7)
    def test_non_numeric_luck_spent_does_not_duplicate_weak_point_result(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "Car",
            "range_bracket": 0, "target_moving": True,
            "luck_spent": "abc",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 10})
        self.assertEqual(len(r["results"]), 1)
        self.assertNotIn("error", r["results"][0])
        luck_ops = [op for op in r["state_ops"] if op.get("op") == "luck"]
        self.assertEqual(luck_ops, [])

    @patch(MOCK, return_value=3)
    def test_ramming_dispatch_without_target_vehicle_sdp_does_not_error(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "Enemy Car",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 10,
            "target_is_vehicle": True,
            # target_sdp_current intentionally omitted
            "target_sp": 10,
        }], sequential=False)
        self.assertEqual(r["results"][0]["type"], "ramming")
        self.assertNotIn("error", r["results"][0])
        self.assertIsNone(r["results"][0]["vehicle_stopped"])

    @patch(MOCK, return_value=6)
    def test_vehicle_weak_point_requires_vehicle_name(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "",
            "target_moving": False,
        }])
        self.assertIn("error", r["results"][0])
        self.assertEqual(r["state_ops"], [])

    @patch(MOCK, return_value=6)
    def test_spike_strip_requires_target_vehicle_name(self, _m):
        r = resolve_actions([{
            "type": "spike_strip",
            "character": "V",
            "target_driver": "Enemy",
            "target_stat_value": 5, "target_skill_value": 4,
            "target_vehicle_name": "",
            "target_vehicle_sp": 10,
        }])
        self.assertIn("error", r["results"][0])
        self.assertEqual(r["state_ops"], [])

    @patch(MOCK, return_value=6)
    def test_ramming_requires_target_name(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "",
            "vehicle_sdp_current": 50, "vehicle_sp": 0,
            "target_is_vehicle": True, "target_sp": 0,
        }])
        self.assertIn("error", r["results"][0])
        self.assertEqual(r["state_ops"], [])

    @patch(MOCK, return_value=3)
    def test_ramming_string_false_target_is_vehicle_treated_as_false(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "Ped",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 0,
            "target_hp_current": 20,
            "target_sp": 0,
            "target_is_vehicle": "false",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 0})
        hp_ops = [op for op in r["state_ops"] if op.get("op") == "hp" and op.get("edgerunner") == "Ped"]
        self.assertGreater(len(hp_ops), 0)
        self.assertFalse(any(op.get("op") == "vehicle_sdp" and op.get("vehicle") == "Ped" for op in r["state_ops"]))

    @patch(MOCK, return_value=3)
    def test_ramming_string_false_target_not_skipped_as_destroyed_vehicle(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "Ped",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 0,
            "target_hp_current": 20,
            "target_sp": 0,
            "target_is_vehicle": "false",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 0, "Ped": 0, "Ped:sp": 0})
        self.assertFalse(r["results"][0].get("skipped", False))
        self.assertNotEqual(r["results"][0].get("reason"), "target_vehicle_destroyed")

    @patch(MOCK, return_value=3)
    def test_ramming_unknown_string_target_is_vehicle_defaults_true(self, _m):
        r = resolve_actions([{
            "type": "ramming",
            "character": "V",
            "vehicle_name": "Car",
            "target": "Wreck",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 0,
            "target_sdp_current": 20,
            "target_sp": 0,
            "target_is_vehicle": "vehicle",
        }], sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 0, "Wreck": 20, "Wreck:sp": 0})
        self.assertTrue(any(op.get("op") == "vehicle_sdp" and op.get("vehicle") == "Wreck" for op in r["state_ops"]))
        self.assertFalse(any(op.get("op") == "hp" and op.get("edgerunner") == "Wreck" for op in r["state_ops"]))

    @patch(MOCK, return_value=6)
    def test_vehicle_names_are_stripped_for_sequential_tracking(self, _m):
        r = resolve_actions([{
            "type": "driving_check",
            "character": "V",
            "vehicle_name": " Wreck ",
            "stat_value": 8,
            "skill_value": 8,
            "maneuver": "maintain_control",
        }], sequential=True, combatant_vehicle_sdp={"Wreck": 0, "Wreck:sp": 0})
        self.assertTrue(r["results"][0].get("skipped"))
        self.assertEqual(r["results"][0].get("reason"), "vehicle_destroyed")

    @patch(MOCK, return_value=6)
    def test_weak_point_string_false_target_moving_treated_as_stationary(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 3,
            "vehicle_sp": 5, "vehicle_name": "Car",
            "target_moving": "false",
        }])
        self.assertTrue(r["results"][0]["hit"])
        self.assertIsNone(r["results"][0]["attack_roll"])


class TestApplyVehicleUpdates(unittest.TestCase):
    """Tests for _apply_vehicle_updates bootstrap and upgrade application."""

    def test_bootstrap_basic(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "V's Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 30, "occupants": ["V"], "driver": "V",
                "upgrades": [],
            },
        }])
        v = combat["vehicles"]["V's Car"]
        self.assertEqual(v["sdp_max"], 50)
        self.assertEqual(v["sdp_current"], 50)
        self.assertEqual(v["sp"], 0)
        self.assertEqual(v["status"], "active")

    def test_bootstrap_armored_chassis_applies_sp(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Tank",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": [], "driver": "V",
                "upgrades": ["armored_chassis"],
            },
        }])
        v = combat["vehicles"]["Tank"]
        self.assertEqual(v["sp"], 13)  # Armored Chassis sets SP to 13

    def test_bootstrap_heavy_chassis_adds_sdp(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Truck",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": [], "driver": "V",
                "upgrades": ["heavy_chassis"],
            },
        }])
        v = combat["vehicles"]["Truck"]
        self.assertEqual(v["sdp_max"], 70)  # 50 + 20
        self.assertEqual(v["sdp_current"], 70)

    def test_bootstrap_both_upgrades(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "APC",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 100, "sp": 0,
                "combat_move": 20, "occupants": ["V", "Jackie"],
                "driver": "V",
                "upgrades": ["armored_chassis", "heavy_chassis"],
            },
        }])
        v = combat["vehicles"]["APC"]
        self.assertEqual(v["sp"], 13)
        self.assertEqual(v["sdp_max"], 120)  # 100 + 20
        self.assertEqual(v["sdp_current"], 120)

    def test_re_bootstrap_ignored(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 30, "sp": 10,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 100, "sp": 0,
                "combat_move": 20, "occupants": [], "driver": "V",
                "upgrades": [],
            },
        }])
        # Should NOT re-bootstrap — SDP should remain 30, not reset to 100
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 30)

    def test_sdp_delta_applies(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 13,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sdp_delta": -15}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 35)

    def test_sdp_delta_applies_case_insensitive_vehicle_name(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 13,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "car", "sdp_delta": -15}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 35)

    def test_auto_destroy_on_zero_sdp(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 5, "sp": 0,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sdp_delta": -10}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 0)
        self.assertEqual(combat["vehicles"]["Car"]["status"], "destroyed")

    def test_explicit_destroyed_status_sets_sdp_to_zero(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 10, "sp": 5,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "status": "destroyed"}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 0)
        self.assertEqual(combat["vehicles"]["Car"]["status"], "destroyed")


class TestApplyVehicleUpdatesEdgeCases(unittest.TestCase):
    """Edge cases for _apply_vehicle_updates: clamping, judgment validation, matching SP."""

    def test_sdp_clamps_to_zero(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 10, "sp": 0, "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sdp_delta": -999}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 0)

    def test_sdp_clamps_to_max(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 40, "sp": 0, "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sdp_delta": 999}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 50)

    def test_sp_clamps_to_zero(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 3, "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sp_delta": -10}])
        self.assertEqual(combat["vehicles"]["Car"]["sp"], 0)

    def test_occupants_type_validated(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "occupants": ["V"], "driver": "V", "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "occupants": "not_a_list"}])
        self.assertEqual(combat["vehicles"]["Car"]["occupants"], ["V"])  # unchanged

    def test_driver_accepts_none(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "occupants": [], "driver": "V", "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "driver": None}])
        self.assertIsNone(combat["vehicles"]["Car"]["driver"])

    def test_driver_rejects_non_string_non_object(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "occupants": [], "driver": "V", "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "driver": ["bad"]}])
        self.assertIsNone(combat["vehicles"]["Car"]["driver"])

    def test_occupants_drop_non_string_scalars(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "occupants": ["V"], "driver": "V", "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "occupants": [123, True, {"name": "Ok"}, "Also"]}])
        self.assertEqual(combat["vehicles"]["Car"]["occupants"], ["Ok", "Also"])

    def test_status_rejects_invalid(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "status": "on_fire"}])
        self.assertEqual(combat["vehicles"]["Car"]["status"], "active")  # unchanged

    def test_status_accepts_disabled(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 50, "sp": 0,
                                        "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "status": "disabled"}])
        self.assertEqual(combat["vehicles"]["Car"]["status"], "disabled")

    def test_armored_chassis_applies_when_sp_matches(self):
        """Armored chassis (sp=13) should apply even if model sends sp=13."""
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Tank",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 13,
                "combat_move": 20, "occupants": [], "driver": "V",
                "upgrades": ["armored_chassis"],
            },
        }])
        self.assertEqual(combat["vehicles"]["Tank"]["sp"], 13)

    def test_unknown_vehicle_sdp_delta_ignored(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{"name": "Ghost", "sdp_delta": -10}])
        self.assertNotIn("Ghost", combat["vehicles"])

    def test_vehicle_update_name_is_trimmed(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 40, "sp": 0, "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": " Car ", "sdp_delta": -10}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 30)

    def test_status_destroyed_preserved_when_sdp_positive(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 40, "sp": 0, "status": "active"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "status": "destroyed"}])
        self.assertEqual(combat["vehicles"]["Car"]["status"], "destroyed")

    def test_repair_reactivates_destroyed_vehicle(self):
        combat = {"vehicles": {"Car": {"sdp_max": 50, "sdp_current": 40, "sp": 0, "status": "destroyed"}}}
        _apply_vehicle_updates(combat, [{"name": "Car", "sdp_delta": 5}])
        self.assertEqual(combat["vehicles"]["Car"]["status"], "active")

    def test_bootstrap_driver_dict_normalized_to_name(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": ["V"],
                "driver": {"name": "V"},
                "upgrades": [],
            },
        }])
        self.assertEqual(combat["vehicles"]["Car"]["driver"], "V")

    def test_bootstrap_non_list_upgrades_ignored(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": ["V"], "driver": "V",
                "upgrades": "armored_chassis",
            },
        }])
        self.assertEqual(combat["vehicles"]["Car"]["upgrades"], [])

    def test_bootstrap_upgrade_key_is_trimmed_and_normalized(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": ["V"], "driver": "V",
                "upgrades": [" armored_chassis "],
            },
        }])
        self.assertIn("armored_chassis", combat["vehicles"]["Car"]["upgrades"])
        self.assertEqual(combat["vehicles"]["Car"]["sp"], 13)

    def test_bootstrap_upgrade_human_name_normalized_to_key(self):
        combat = {}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0,
                "combat_move": 20, "occupants": ["V"], "driver": "V",
                "upgrades": ["Armored Chassis"],
            },
        }])
        self.assertIn("armored_chassis", combat["vehicles"]["Car"]["upgrades"])
        self.assertEqual(combat["vehicles"]["Car"]["sp"], 13)


class TestCombatCompleteVehicleOrdering(unittest.TestCase):
    """Vehicle updates must apply BEFORE combat state is cleared."""

    def test_vehicle_updates_applied_on_combat_complete(self):
        """Resolver damage on final combat turn should still be applied."""
        pipeline_state = {
            "combat": {
                "round": 3,
                "vehicles": {
                    "Car": {"sdp_max": 50, "sdp_current": 50, "sp": 13, "status": "active",
                             "occupants": ["V"], "driver": "V", "upgrades": []},
                },
            },
            "character_states": {},
        }
        tool_input = {
            "narrative": "Final blow",
            "rolls": "",
            "character_updates": [],
            "cover_state": [],
            "combat": None,
            "combat_complete": True,
            "vehicle_updates": [{"name": "Car", "sdp_delta": -20}],
        }
        # Before the fix, vehicle_updates were silently dropped when combat_complete=True
        apply_cpred_combat_state(pipeline_state, tool_input, game_state={"edgerunners": {}})
        # Combat should be cleared
        self.assertIsNone(pipeline_state["combat"])
        # But the vehicle damage should have been applied before clearing
        # (We can't check the vehicle state after clearing, but we can verify
        # the function didn't crash and the combat was cleared properly)

    def test_vehicle_updates_applied_before_clearing(self):
        """Verify vehicle SDP actually changes when combat_complete=True."""
        # We need to verify the update actually ran, so we'll check
        # via a side channel: if sdp hits 0, status should be destroyed
        pipeline_state = {
            "combat": {
                "round": 5,
                "vehicles": {
                    "Bike": {"sdp_max": 35, "sdp_current": 5, "sp": 0, "status": "active",
                              "occupants": ["Enemy"], "driver": "Enemy", "upgrades": []},
                },
            },
            "character_states": {},
        }
        # Save a reference to the vehicles dict before apply
        vehicles_ref = pipeline_state["combat"]["vehicles"]
        tool_input = {
            "narrative": "Bike destroyed",
            "rolls": "",
            "character_updates": [],
            "cover_state": [],
            "combat": None,
            "combat_complete": True,
            "vehicle_updates": [{"name": "Bike", "sdp_delta": -10}],
        }
        apply_cpred_combat_state(pipeline_state, tool_input, game_state={"edgerunners": {}})
        # Combat cleared
        self.assertIsNone(pipeline_state["combat"])
        # But the vehicle was updated before clearing (verify via our saved reference)
        self.assertEqual(vehicles_ref["Bike"]["sdp_current"], 0)
        self.assertEqual(vehicles_ref["Bike"]["status"], "destroyed")


class TestFormatVehicleLines(unittest.TestCase):
    """Tests for _format_vehicle_lines display helper."""

    def test_empty_vehicles(self):
        self.assertEqual(_format_vehicle_lines({}), [])
        self.assertEqual(_format_vehicle_lines(None), [])

    def test_basic_vehicle(self):
        vehicles = {"Car": {
            "sdp_current": 40, "sdp_max": 50, "sp": 13,
            "combat_move": 30, "driver": "V", "occupants": ["V", "Jackie"],
            "upgrades": ["armored_chassis"], "status": "active",
        }}
        lines = _format_vehicle_lines(vehicles)
        self.assertEqual(len(lines), 2)  # header + 1 vehicle
        self.assertIn("Vehicles:", lines[0])
        self.assertIn("SDP 40/50", lines[1])
        self.assertIn("SP 13", lines[1])
        self.assertIn("Driver: V", lines[1])
        self.assertIn("Passengers: Jackie", lines[1])
        self.assertIn("armored_chassis", lines[1])

    def test_destroyed_vehicle(self):
        vehicles = {"Wreck": {"status": "destroyed"}}
        lines = _format_vehicle_lines(vehicles)
        self.assertIn("DESTROYED", lines[1])

    def test_driver_none(self):
        vehicles = {"Car": {
            "sdp_current": 50, "sdp_max": 50, "sp": 0,
            "combat_move": 20, "driver": None, "occupants": ["V"],
            "upgrades": [], "status": "active",
        }}
        lines = _format_vehicle_lines(vehicles)
        self.assertIn("Driver: ?", lines[1])
        self.assertIn("Passengers: V", lines[1])

    def test_no_passengers(self):
        vehicles = {"Bike": {
            "sdp_current": 35, "sdp_max": 35, "sp": 0,
            "combat_move": 40, "driver": "V", "occupants": ["V"],
            "upgrades": [], "status": "active",
        }}
        lines = _format_vehicle_lines(vehicles)
        self.assertNotIn("Passengers", lines[1])


class TestResolveActionsVehicleOnOutcome(unittest.TestCase):
    """Tests that on_outcome passes through resolve_actions dispatcher."""

    @patch(MOCK, return_value=9)
    def test_driving_check_on_outcome_success(self, _m):
        r = resolve_actions([{
            "type": "driving_check", "character": "V",
            "vehicle_name": "Car",
            "stat_value": 6, "skill_value": 4, "maneuver": "maintain_control",
            "on_hit": "Maintained control", "on_miss": "Lost it",
        }])
        self.assertEqual(r["results"][0]["on_outcome"], "Maintained control")

    @patch(MOCK, return_value=1)
    def test_driving_check_on_outcome_failure(self, _m):
        r = resolve_actions([{
            "type": "driving_check", "character": "V",
            "vehicle_name": "Car",
            "stat_value": 2, "skill_value": 1, "maneuver": "bootleg_turn",
            "on_hit": "Nailed it", "on_miss": "Spun out",
        }])
        self.assertEqual(r["results"][0]["on_outcome"], "Spun out")

    @patch(MOCK, return_value=3)
    def test_ramming_on_outcome_through_dispatcher(self, _m):
        r = resolve_actions([{
            "type": "ramming", "character": "V",
            "vehicle_name": "Car", "target": "Pedestrian",
            "vehicle_sdp_current": 50, "vehicle_sp": 0,
            "target_hp_current": 30, "target_sp": 0,
            "on_hit": "Crushed", "on_miss": "Dodged",
        }])
        self.assertEqual(r["results"][0]["on_outcome"], "Crushed")

    @patch(MOCK, return_value=4)
    def test_weak_point_on_outcome_through_dispatcher(self, _m):
        r = resolve_actions([{
            "type": "vehicle_weak_point", "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Pistol", "damage_dice": 2,
            "vehicle_sp": 0, "vehicle_name": "Car",
            "target_moving": False,
            "on_hit": "Shredded", "on_miss": "Whiffed",
        }])
        self.assertEqual(r["results"][0]["on_outcome"], "Shredded")

    @patch(MOCK, return_value=3)
    def test_spike_strip_on_outcome_through_dispatcher(self, _m):
        r = resolve_actions([{
            "type": "spike_strip", "character": "V",
            "target_driver": "Enemy", "target_stat_value": 3,
            "target_skill_value": 2, "target_vehicle_name": "Car",
            "target_vehicle_sp": 0,
            "on_hit": "Tires shredded", "on_miss": "Avoided",
        }])
        self.assertEqual(r["results"][0]["on_outcome"], "Tires shredded")


class TestReturnKeyConsistency(unittest.TestCase):
    """Verify all return paths include the same keys for each resolver."""

    @patch(MOCK, return_value=3)
    def test_ramming_dodged_has_all_keys(self, _m):
        from game_systems.cpred_mechanics import resolve_ramming
        r = resolve_ramming(
            vehicle_name="Car", target_name="Ped",
            vehicle_sdp_current=50, vehicle_sp=0,
            target_hp_current=30, target_sp=0,
            pedestrian_dodge=True, pedestrian_dex=10, pedestrian_evasion=10,
        )
        # Dodged path should still have all numeric keys
        for key in ("ram_damage_dice", "ram_damage_total", "vehicle_damage",
                     "target_damage", "vehicle_stopped", "target_ablation", "vehicle_ablation"):
            self.assertIn(key, r, f"Missing key {key} in dodged return")

    @patch(MOCK, return_value=1)
    def test_weak_point_miss_has_all_keys(self, _m):
        from game_systems.cpred_mechanics import resolve_vehicle_weak_point
        r = resolve_vehicle_weak_point(
            stat_value=2, skill_value=1, weapon_type="Pistol",
            damage_dice=2, vehicle_sp=10, vehicle_name="Car",
            target_moving=True,
        )
        for key in ("raw_damage", "effective_sp", "damage_past_sp", "doubled_damage", "ablation"):
            self.assertIn(key, r, f"Missing key {key} in miss return")

    @patch(MOCK, return_value=10)
    def test_spike_strip_avoided_has_all_keys(self, _m):
        from game_systems.cpred_mechanics import resolve_spike_strip
        r = resolve_spike_strip(
            target_driver_name="Enemy", stat_value=8, skill_value=8,
            target_vehicle_name="Car", target_vehicle_sp=10,
        )
        for key in ("raw_damage", "effective_sp", "damage_past_sp", "doubled_damage", "ablation"):
            self.assertIn(key, r, f"Missing key {key} in avoided return")


class TestSequentialVehicleSDP(unittest.TestCase):
    """Tests for sequential vehicle SDP/SP tracking in resolve_actions."""

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_two_weak_point_shots_use_updated_sp(self, _m):
        """Second weak-point shot should use SP reduced by first shot's ablation.

        With randint=6: 5d6 = 30 damage. SP 10 → 20 past SP → doubled = 40 SDP damage.
        Ablation = 1, so SP drops to 9. Second shot should see effective_sp = 9.
        """
        actions = [
            {
                "type": "vehicle_weak_point",
                "character": "V",
                "stat_value": 8, "skill_value": 6,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10, "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
            {
                "type": "vehicle_weak_point",
                "character": "Jackie",
                "stat_value": 7, "skill_value": 5,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10,  # model provides stale value — should be overridden
                "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
        ]
        vehicle_sdp = {"Target Car": 100, "Target Car:sp": 10}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        results = result["results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["hit"])
        self.assertTrue(results[1]["hit"])
        # First shot uses SP 10
        self.assertEqual(results[0]["effective_sp"], 10)
        # Second shot uses tracked SP (10 - 1 ablation = 9)
        self.assertLess(results[1]["effective_sp"], 10)
        self.assertEqual(results[1]["effective_sp"], 9)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_without_combatant_vehicle_sdp_both_use_stale_sp(self, _m):
        """Without combatant_vehicle_sdp, both actions use the model's stale SP value."""
        actions = [
            {
                "type": "vehicle_weak_point",
                "character": "V",
                "stat_value": 8, "skill_value": 6,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10, "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
            {
                "type": "vehicle_weak_point",
                "character": "Jackie",
                "stat_value": 7, "skill_value": 5,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10, "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
        ]
        result = resolve_actions(actions, sequential=True)
        results = result["results"]
        # Without tracking, both use the action's stale SP 10
        self.assertEqual(results[0]["effective_sp"], 10)
        self.assertEqual(results[1]["effective_sp"], 10)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_non_sequential_ignores_vehicle_tracking_map_for_sp(self, _m):
        actions = [{
            "type": "vehicle_weak_point",
            "character": "V",
            "stat_value": 8, "skill_value": 6,
            "weapon_type": "Assault Rifle", "damage_dice": 5,
            "vehicle_sp": 99, "vehicle_name": "Car",
            "range_bracket": 1, "target_moving": False,
        }]
        result = resolve_actions(actions, sequential=False, combatant_vehicle_sdp={"car:sp": 0})
        self.assertEqual(result["results"][0]["effective_sp"], 99)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=3)
    def test_sequential_ramming_tracks_pedestrian_armor_ablation(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": "Ped",
                "target_hp_current": 100,
                "target_sp": 10,
                "target_is_vehicle": False,
            },
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": "Ped",
                "target_hp_current": 100,
                "target_sp": 10,
                "target_is_vehicle": False,
            },
        ]
        out = resolve_actions(actions, sequential=True, combatant_hp={"Ped": 100})
        self.assertEqual(out["results"][0]["target_damage"], 8)
        self.assertEqual(out["results"][1]["target_damage"], 9)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=3)
    def test_sequential_ramming_pedestrian_armor_tracking_is_case_insensitive(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": "Ped",
                "target_hp_current": 100,
                "target_sp": 10,
                "target_is_vehicle": False,
            },
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": " ped ",
                "target_hp_current": 100,
                "target_sp": 10,
                "target_is_vehicle": False,
            },
        ]
        out = resolve_actions(actions, sequential=True, combatant_hp={"Ped": 100})
        self.assertEqual(out["results"][0]["target_damage"], 8)
        self.assertEqual(out["results"][1]["target_damage"], 9)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=3)
    def test_sequential_ramming_pedestrian_hp_tracking_is_case_insensitive(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": "Ped",
                "target_hp_current": 10,
                "target_sp": 0,
                "target_is_vehicle": False,
            },
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target": " ped ",
                "target_hp_current": 10,
                "target_sp": 0,
                "target_is_vehicle": False,
            },
        ]
        out = resolve_actions(actions, sequential=True, combatant_hp={"Ped": 10})
        self.assertFalse(out["results"][0]["vehicle_stopped"])
        self.assertFalse(out["results"][1]["vehicle_stopped"])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_empty_vehicle_tracking_dict_seeds_from_actions(self, _m):
        """An empty combatant_vehicle_sdp should still seed and track per-action vehicles."""
        actions = [
            {
                "type": "vehicle_weak_point",
                "character": "V",
                "stat_value": 8, "skill_value": 6,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10, "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
            {
                "type": "vehicle_weak_point",
                "character": "Jackie",
                "stat_value": 7, "skill_value": 5,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 10,  # stale, should be overridden to 9
                "vehicle_name": "Target Car",
                "range_bracket": 1, "target_moving": False,
            },
        ]
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp={})
        results = result["results"]
        self.assertEqual(results[0]["effective_sp"], 10)
        self.assertEqual(results[1]["effective_sp"], 9)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_spike_strip_uses_tracked_sp(self, _m):
        """Spike strip should use sequential-tracked SP, not stale action value."""
        actions = [
            {
                "type": "vehicle_weak_point",
                "character": "V",
                "stat_value": 8, "skill_value": 6,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 5, "vehicle_name": "Enemy Car",
                "range_bracket": 1, "target_moving": False,
            },
            {
                "type": "spike_strip",
                "character": "Jackie",
                "target_driver": "Ganger",
                "target_stat_value": 4, "target_skill_value": 2,
                "target_vehicle_name": "Enemy Car",
                "target_vehicle_sp": 5,  # stale — should be 4 after first action's ablation
            },
        ]
        vehicle_sdp = {"Enemy Car": 50, "Enemy Car:sp": 5}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        results = result["results"]
        self.assertEqual(len(results), 2)
        # First shot ablates SP by 1 (5→4)
        # Spike strip should use tracked SP 4, not stale 5
        if results[1].get("hit"):
            self.assertEqual(results[1]["effective_sp"], 4)

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_unknown_target_vehicle_sdp_keeps_vehicle_stopped_unknown(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V",
                "vehicle_name": "Car",
                "target": "Unknown Car",
                "vehicle_sdp_current": 50,
                "vehicle_sp": 0,
                "target_is_vehicle": True,
                "target_sp": 0,
                # target_sdp_current intentionally omitted
            },
        ]
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp={"Car": 50, "Car:sp": 0})
        self.assertIsNone(result["results"][0]["vehicle_stopped"])


class TestVehicleElimination(unittest.TestCase):
    """Tests that destroyed vehicles skip actions in sequential resolution."""

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_ramming(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V", "vehicle_name": "Wreck",
                "target": "Enemy", "vehicle_sdp_current": 0,
                "vehicle_sp": 0, "target_hp_current": 30, "target_sp": 5,
            },
        ]
        vehicle_sdp = {"Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertIn("notification", result["results"][0])
        self.assertIn("already destroyed", result["results"][0]["notification"])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_ramming_with_empty_tracking_dict(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V", "vehicle_name": "Wreck",
                "target": "Enemy", "vehicle_sdp_current": 0,
                "vehicle_sp": 0, "target_hp_current": 30, "target_sp": 5,
            },
        ]
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp={})
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_ramming_without_tracking_map(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V", "vehicle_name": "Wreck",
                "target": "Enemy", "vehicle_sdp_current": 0,
                "vehicle_sp": 0, "target_hp_current": 30, "target_sp": 5,
            },
        ]
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=None)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_ramming_skips_when_target_vehicle_already_destroyed(self, _m):
        actions = [
            {
                "type": "ramming",
                "character": "V", "vehicle_name": "Runner",
                "target": "Wreck", "target_is_vehicle": True,
                "vehicle_sdp_current": 30, "vehicle_sp": 8,
                "target_sdp_current": 0, "target_sp": 0,
            },
        ]
        vehicle_sdp = {"Runner": 30, "Runner:sp": 8, "Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "target_vehicle_destroyed")
        self.assertIn("notification", result["results"][0])
        self.assertIn("Wreck", result["results"][0]["notification"])
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_driving_check(self, _m):
        actions = [
            {
                "type": "driving_check",
                "character": "V", "vehicle_name": "Wreck",
                "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            },
        ]
        vehicle_sdp = {"Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_driving_check_with_untrimmed_tracking_key(self, _m):
        actions = [
            {
                "type": "driving_check",
                "character": "V", "vehicle_name": "Car",
                "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            },
        ]
        vehicle_sdp = {"Car ": 0, "Car :sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_driving_check_with_case_mismatched_tracking_key(self, _m):
        actions = [
            {
                "type": "driving_check",
                "character": "V", "vehicle_name": "wreck",
                "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            },
        ]
        vehicle_sdp = {"WRECK": 0, "WRECK:SP": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_vehicle_skips_driving_check_without_vehicle_name(self, _m):
        actions = [
            {
                "type": "driving_check",
                "character": "V",
                "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            },
        ]
        vehicle_sdp = {"Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_active_vehicle_not_skipped(self, _m):
        actions = [
            {
                "type": "driving_check",
                "character": "V", "vehicle_name": "Car",
                "stat_value": 6, "skill_value": 4, "maneuver": "swerve",
            },
        ]
        vehicle_sdp = {"Car": 50, "Car:sp": 10}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertFalse(result["results"][0].get("skipped", False))


    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_target_vehicle_skips_weak_point(self, _m):
        """vehicle_weak_point against a destroyed vehicle should be skipped."""
        actions = [
            {
                "type": "vehicle_weak_point",
                "character": "V", "stat_value": 8, "skill_value": 6,
                "weapon_type": "Assault Rifle", "damage_dice": 5,
                "vehicle_sp": 0, "vehicle_name": "Wreck",
                "range_bracket": 1, "target_moving": False,
            },
        ]
        vehicle_sdp = {"Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])

    @patch("game_systems.cpred_mechanics.random.randint", return_value=6)
    def test_destroyed_target_vehicle_skips_spike_strip(self, _m):
        """spike_strip against a destroyed vehicle should be skipped."""
        actions = [
            {
                "type": "spike_strip",
                "character": "V", "target_driver": "Enemy",
                "target_stat_value": 6, "target_skill_value": 4,
                "target_vehicle_name": "Wreck", "target_vehicle_sp": 0,
            },
        ]
        vehicle_sdp = {"Wreck": 0, "Wreck:sp": 0}
        result = resolve_actions(actions, sequential=True, combatant_vehicle_sdp=vehicle_sdp)
        self.assertTrue(result["results"][0]["skipped"])
        self.assertEqual(result["results"][0]["reason"], "vehicle_destroyed")
        self.assertEqual(result["state_ops"], [])


class TestDrivingCheckStateOps(unittest.TestCase):
    """Tests that resolve_driving_check returns explicit state_ops key."""

    @patch("game_systems.cpred_mechanics.random.randint", return_value=5)
    def test_driving_check_has_state_ops(self, _m):
        r = resolve_driving_check(stat_value=6, skill_value=4, maneuver="swerve")
        self.assertIn("state_ops", r)
        self.assertEqual(r["state_ops"], [])


class TestFormatVehicleLinesExtended(unittest.TestCase):
    """Tests for disabled status and type display in _format_vehicle_lines."""

    def test_disabled_vehicle_shows_disabled_with_details(self):
        vehicles = {"Truck": {
            "status": "disabled", "sdp_current": 20, "sdp_max": 50,
            "sp": 5, "combat_move": 0, "type": "land",
            "driver": "V", "occupants": ["V", "Jackie"], "upgrades": [],
        }}
        lines = _format_vehicle_lines(vehicles)
        joined = " ".join(lines)
        self.assertIn("DISABLED", joined)
        # Disabled vehicles SHOULD show SDP and occupants (people are still inside)
        self.assertIn("SDP 20/50", joined)
        self.assertIn("Jackie", joined)

    def test_active_vehicle_shows_type(self):
        vehicles = {
            "AV-4": {
                "status": "active",
                "type": "air",
                "sdp_current": 100,
                "sdp_max": 100,
                "sp": 13,
                "combat_move": 30,
                "driver": "V",
                "occupants": ["V"],
                "upgrades": [],
            }
        }
        lines = _format_vehicle_lines(vehicles)
        joined = " ".join(lines)
        self.assertIn("air", joined)

    def test_cover_hp_displayed(self):
        vehicles = {
            "Sedan": {
                "status": "active",
                "type": "land",
                "sdp_current": 50,
                "sdp_max": 50,
                "sp": 13,
                "combat_move": 25,
                "driver": "V",
                "occupants": ["V"],
                "upgrades": ["armored_chassis", "bulletproof_glass_thick"],
                "cover_hp": 30,
            }
        }
        lines = _format_vehicle_lines(vehicles)
        joined = " ".join(lines)
        self.assertIn("Glass: 30HP", joined)


class TestApplyVehicleUpdatesBootstrapUpgrades(unittest.TestCase):
    """Tests for bulletproof glass and seating upgrade auto-application at bootstrap."""

    def test_bulletproof_glass_thin_sets_cover_hp(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Sedan",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V",
                "upgrades": ["bulletproof_glass_thin"],
            },
        }])
        self.assertEqual(combat["vehicles"]["Sedan"]["cover_hp"], 15)

    def test_bulletproof_glass_thick_sets_cover_hp(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V",
                "upgrades": ["bulletproof_glass_thick"],
            },
        }])
        self.assertEqual(combat["vehicles"]["Car"]["cover_hp"], 30)

    def test_seating_upgrade_adds_to_base_seats(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "AV-4",
            "set_vehicle_stats": {
                "type": "air", "sdp_max": 100, "sp": 0, "combat_move": 30,
                "occupants": ["V"], "driver": "V",
                "upgrades": ["seating_upgrade"],
            },
        }])
        # AV-4 Aerodyne base seats = 6, + seating_upgrade bonus 2 = 8
        self.assertEqual(combat["vehicles"]["AV-4"]["seats"], 8)

    def test_no_cover_hp_without_bulletproof_glass(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Bike",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 35, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V",
                "upgrades": [],
            },
        }])
        self.assertNotIn("cover_hp", combat["vehicles"]["Bike"])


class TestMergeVehicleUpdates(unittest.TestCase):
    """Tests for _merge_vehicle_updates helper in main.py."""

    def test_merge_new_vehicle(self):
        from main import _merge_vehicle_updates
        existing = [{"name": "Car A", "status": "active"}]
        resolver = [{"name": "Car B", "sdp_delta": -10}]
        result = _merge_vehicle_updates(existing, resolver)
        names = [r["name"] for r in result]
        self.assertIn("Car A", names)
        self.assertIn("Car B", names)

    def test_merge_additive_deltas(self):
        from main import _merge_vehicle_updates
        existing = [{"name": "Car", "sdp_delta": -5, "occupants": ["V"]}]
        resolver = [{"name": "Car", "sdp_delta": -10, "sp_delta": -1}]
        result = _merge_vehicle_updates(existing, resolver)
        car = [r for r in result if r["name"] == "Car"][0]
        self.assertEqual(car["sdp_delta"], -15)
        self.assertEqual(car["sp_delta"], -1)
        # Preserves model judgment fields
        self.assertEqual(car["occupants"], ["V"])

    def test_merge_empty_lists(self):
        from main import _merge_vehicle_updates
        self.assertEqual(_merge_vehicle_updates([], []), [])
        self.assertEqual(_merge_vehicle_updates(None, None), [])

    def test_merge_case_insensitive_vehicle_names(self):
        from main import _merge_vehicle_updates
        existing = [{"name": "Car", "sdp_delta": -5}]
        resolver = [{"name": "car", "sdp_delta": -10, "sp_delta": -1}]
        result = _merge_vehicle_updates(existing, resolver)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Car")
        self.assertEqual(result[0]["sdp_delta"], -15)
        self.assertEqual(result[0]["sp_delta"], -1)

    def test_merge_existing_casefold_duplicates_do_not_drop_updates(self):
        from main import _merge_vehicle_updates
        existing = [
            {"name": "Car", "sdp_delta": -2, "occupants": ["V"]},
            {"name": "car", "sdp_delta": -3, "status": "disabled"},
        ]
        resolver = [{"name": "CAR", "sdp_delta": -5, "sp_delta": -1}]
        result = _merge_vehicle_updates(existing, resolver)
        self.assertEqual(len(result), 1)
        merged = result[0]
        self.assertEqual(merged["name"], "Car")
        self.assertEqual(merged["sdp_delta"], -10)
        self.assertEqual(merged["sp_delta"], -1)
        self.assertEqual(merged["occupants"], ["V"])
        self.assertEqual(merged["status"], "disabled")


class TestBuildVehicleReferenceTable(unittest.TestCase):
    """Smoke tests for _build_vehicle_reference_table()."""

    def test_table_generates_without_error(self):
        from game_systems.cpred_combat import _build_vehicle_reference_table
        table = _build_vehicle_reference_table()
        self.assertIn("Vehicle", table)  # header column
        self.assertIn("Type", table)     # land/air/sea column
        self.assertIn("SDP", table)

    def test_all_vehicle_stats_entries_present(self):
        from game_systems.cpred_combat import _build_vehicle_reference_table
        from game_systems.cpred_tables import VEHICLE_STATS
        table = _build_vehicle_reference_table()
        for v in VEHICLE_STATS.values():
            self.assertIn(v["name"], table, f"Missing vehicle {v['name']} in reference table")

    def test_table_has_correct_column_count(self):
        from game_systems.cpred_combat import _build_vehicle_reference_table
        table = _build_vehicle_reference_table()
        # Each data row should have 7 pipe-separated columns (6 pipes + outer pipes)
        data_lines = [l for l in table.split("\n") if l.startswith("|") and "---" not in l]
        for line in data_lines:
            pipes = line.count("|")
            self.assertEqual(pipes, 7, f"Wrong column count in: {line}")


class TestSeatsBaseValue(unittest.TestCase):
    """Tests that base seats are correctly looked up from VEHICLE_STATS at bootstrap."""

    def test_compact_groundcar_gets_4_seats(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Player Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V", "upgrades": [],
            },
        }])
        # Compact Groundcar: sdp=50, type=land → seats=4
        self.assertEqual(combat["vehicles"]["Player Car"]["seats"], 4)

    def test_seating_upgrade_adds_to_base(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Player Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V", "upgrades": ["seating_upgrade"],
            },
        }])
        # Base 4 + upgrade 2 = 6
        self.assertEqual(combat["vehicles"]["Player Car"]["seats"], 6)

    def test_bike_gets_2_seats(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "V's Bike",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 35, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V", "upgrades": [],
            },
        }])
        self.assertEqual(combat["vehicles"]["V's Bike"]["seats"], 2)

    def test_unknown_vehicle_fallback_2_seats(self):
        """Custom vehicle with non-matching sdp+type falls back to 2 seats."""
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Custom Tank",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 999, "sp": 0, "combat_move": 10,
                "occupants": ["V"], "driver": "V", "upgrades": [],
            },
        }])
        self.assertEqual(combat["vehicles"]["Custom Tank"]["seats"], 2)

    def test_name_substring_match_high_perf(self):
        """Name substring match prefers High Perf. Groundcar over Compact Groundcar."""
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "High Perf. Groundcar",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 50,
                "occupants": ["V"], "driver": "V", "upgrades": [],
            },
        }])
        self.assertEqual(combat["vehicles"]["High Perf. Groundcar"]["seats"], 4)

    def test_explicit_seats_in_set_vehicle_stats(self):
        """Model can provide seats directly, bypassing lookup."""
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Custom Ride",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 25,
                "occupants": ["V"], "driver": "V", "upgrades": [],
                "seats": 8,
            },
        }])
        self.assertEqual(combat["vehicles"]["Custom Ride"]["seats"], 8)


class TestVehicleStatePreservedAcrossCombatUpdate(unittest.TestCase):
    """Tests that vehicle state survives combat dict replacement in apply_single_agent_state_updates."""

    def test_vehicles_preserved_when_combat_replaced(self):
        from pipeline import apply_single_agent_state_updates
        ps = {
            "combat": {
                "round": 1,
                "initiative_order": ["V"],
                "current_turn": "V",
                "vehicles": {
                    "AV-4": {"sdp_current": 80, "sdp_max": 100, "sp": 13, "status": "active"},
                },
            },
            "pacing": "combat",
            "callback_ledger": [],
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "turn_counter": 1,
        }
        parsed = {
            "combat": {"round": 2, "initiative_order": ["V", "Enemy"], "current_turn": "Enemy"},
        }
        apply_single_agent_state_updates(ps, parsed, 2)
        # Combat dict replaced with new initiative data
        self.assertEqual(ps["combat"]["round"], 2)
        # But vehicles should be preserved
        self.assertIn("vehicles", ps["combat"])
        self.assertEqual(ps["combat"]["vehicles"]["AV-4"]["sdp_current"], 80)

    def test_vehicles_not_injected_when_no_old_vehicles(self):
        from pipeline import apply_single_agent_state_updates
        ps = {
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
            "pacing": "combat",
            "callback_ledger": [],
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "turn_counter": 1,
        }
        parsed = {
            "combat": {"round": 2, "initiative_order": ["V"], "current_turn": "V"},
        }
        apply_single_agent_state_updates(ps, parsed, 2)
        self.assertNotIn("vehicles", ps["combat"])


class TestInjectResolverOpsStateful(unittest.TestCase):
    """Tests for _inject_resolver_ops_stateful helper in main.py."""

    def test_strips_dice_dependent_ops_from_model_edgerunner_ops(self):
        from main import _inject_resolver_ops_stateful
        tool_input = {
            "edgerunner_ops": [
                {"op": "hp", "edgerunner": "V", "change": -10},  # model guessed
                {"op": "pacing", "value": "combat"},  # non-dice, should survive
            ]
        }
        state_ops = [{"op": "hp", "edgerunner": "V", "change": -7}]  # resolver authoritative
        _inject_resolver_ops_stateful(tool_input, state_ops, {}, {})
        ops = tool_input["edgerunner_ops"]
        # Model's guessed hp op stripped, pacing survives, resolver hp added
        op_types = [o["op"] for o in ops]
        self.assertEqual(op_types.count("pacing"), 1)
        self.assertEqual(op_types.count("hp"), 1)
        hp_op = [o for o in ops if o["op"] == "hp"][0]
        self.assertEqual(hp_op["change"], -7)  # resolver's value, not model's

    def test_no_ops_is_noop(self):
        from main import _inject_resolver_ops_stateful
        tool_input = {"edgerunner_ops": [{"op": "pacing", "value": "combat"}]}
        _inject_resolver_ops_stateful(tool_input, [], {}, {})
        # Empty state_ops → early return, original ops untouched
        self.assertEqual(len(tool_input["edgerunner_ops"]), 1)

    def test_vehicle_ops_applied_to_combat(self):
        from main import _inject_resolver_ops_stateful
        from game_systems.cpred import _apply_vehicle_updates
        combat = {"vehicles": {
            "Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"},
        }}
        pipeline_state = {"combat": combat}
        gs = {"apply_vehicle_updates": _apply_vehicle_updates}
        state_ops = [{"op": "vehicle_sdp", "vehicle": "Car", "change": -15}]
        _inject_resolver_ops_stateful({}, state_ops, pipeline_state, gs)
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 35)

    def test_vehicle_ops_deferred_when_combat_missing(self):
        from main import _inject_resolver_ops_stateful, _apply_deferred_stateful_vehicle_updates
        from game_systems.cpred import _apply_vehicle_updates
        tool_input = {}
        state_ops = [{"op": "vehicle_sdp", "vehicle": "Car", "change": -15}]
        pipeline_state = {}
        gs = {"apply_vehicle_updates": _apply_vehicle_updates}
        _inject_resolver_ops_stateful(tool_input, state_ops, pipeline_state, gs)
        self.assertIn("_resolver_vehicle_updates", tool_input)
        pipeline_state["combat"] = {"vehicles": {"Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"}}}
        _apply_deferred_stateful_vehicle_updates(tool_input, pipeline_state, gs)
        self.assertEqual(pipeline_state["combat"]["vehicles"]["Car"]["sdp_current"], 35)
        self.assertNotIn("_resolver_vehicle_updates", tool_input)

    def test_vehicle_ops_fallback_applied_when_hook_missing(self):
        from main import _inject_resolver_ops_stateful
        combat = {"vehicles": {
            "Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"},
        }}
        pipeline_state = {"combat": combat}
        state_ops = [{"op": "vehicle_sdp", "vehicle": "Car", "change": -15}]
        _inject_resolver_ops_stateful({}, state_ops, pipeline_state, gs={})
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 35)


class TestStripAndMergeResolverOps(unittest.TestCase):
    """Regression tests for resolver-authoritative strip behavior in main.py."""

    def test_strips_even_when_resolver_state_ops_empty(self):
        from main import _strip_and_merge_resolver_ops
        tool_input = {
            "character_updates": [{"name": "V", "hp_delta": -99, "luck_delta": -4}],
            "vehicle_updates": [{"name": "Car", "sdp_delta": -20, "sp_delta": -2}],
        }
        _strip_and_merge_resolver_ops(tool_input, [])
        self.assertNotIn("hp_delta", tool_input["character_updates"][0])
        self.assertNotIn("luck_delta", tool_input["character_updates"][0])
        self.assertNotIn("sdp_delta", tool_input["vehicle_updates"][0])
        self.assertNotIn("sp_delta", tool_input["vehicle_updates"][0])

    def test_strips_model_ammo_consumed_before_merge(self):
        from main import _strip_and_merge_resolver_ops
        tool_input = {
            "character_updates": [{
                "name": "V",
                "ammo_consumed": [{"weapon_name": "Heavy Pistol", "rounds_consumed": 999}],
            }],
        }
        state_ops = [{
            "op": "ammo",
            "edgerunner": "V",
            "weapon_name": "Heavy Pistol",
            "rounds_consumed": 3,
        }]
        _strip_and_merge_resolver_ops(tool_input, state_ops)
        ammo_consumed = tool_input["character_updates"][0].get("ammo_consumed", [])
        self.assertEqual(len(ammo_consumed), 1)
        self.assertEqual(ammo_consumed[0]["rounds_consumed"], 3)


class TestLegacyApplyCombatStateVehicleUpdates(unittest.TestCase):
    """Regression tests for legacy _apply_combat_state vehicle handling."""

    def test_legacy_apply_combat_state_applies_vehicle_updates(self):
        from main import _apply_combat_state
        pipeline_state = {
            "combat": {
                "round": 1,
                "initiative_order": ["V"],
                "current_turn": "V",
                "vehicles": {"Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"}},
            },
            "character_states": {},
        }
        _apply_combat_state({}, pipeline_state, {"vehicle_updates": [{"name": "Car", "sdp_delta": -5}]})
        self.assertEqual(pipeline_state["combat"]["vehicles"]["Car"]["sdp_current"], 45)

    def test_legacy_apply_combat_state_applies_vehicle_updates_before_clear(self):
        from main import _apply_combat_state
        calls = []

        def _capture_apply_vehicle_updates(combat_dict, vehicle_updates):
            calls.append([dict(upd) for upd in vehicle_updates])
            combat_dict["vehicles"]["Car"]["sdp_current"] += int(vehicle_updates[0].get("sdp_delta", 0))

        pipeline_state = {
            "combat": {
                "round": 1,
                "initiative_order": ["V"],
                "current_turn": "V",
                "vehicles": {"Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"}},
            },
            "character_states": {},
        }
        _apply_combat_state(
            {"apply_vehicle_updates": _capture_apply_vehicle_updates},
            pipeline_state,
            {
                "combat_complete": True,
                "vehicle_updates": [{"name": "Car", "sdp_delta": -5}],
            },
        )
        self.assertEqual(calls, [[{"name": "Car", "sdp_delta": -5}]])
        self.assertIsNone(pipeline_state["combat"])


class TestVehiclePreservationInApplyCombatState(unittest.TestCase):
    """Tests that apply_cpred_combat_state and apply_net_combat_state preserve vehicles."""

    def test_apply_cpred_combat_state_preserves_vehicles(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {
            "combat": {
                "round": 1, "initiative_order": ["V"],
                "vehicles": {"AV-4": {"sdp_current": 80, "sp": 13, "status": "active"}},
            },
        }
        tool_input = {
            "combat": {"round": 2, "initiative_order": ["V", "Enemy"]},
        }
        apply_cpred_combat_state(ps, tool_input)
        self.assertEqual(ps["combat"]["round"], 2)
        self.assertIn("vehicles", ps["combat"])
        self.assertEqual(ps["combat"]["vehicles"]["AV-4"]["sdp_current"], 80)

    def test_apply_net_combat_state_preserves_vehicles(self):
        from game_systems.cpred import apply_net_combat_state
        ps = {
            "combat": {
                "round": 1, "initiative_order": ["V"],
                "vehicles": {"Bike": {"sdp_current": 30, "sp": 0, "status": "active"}},
            },
            "net_combat": {"active": True},
        }
        tool_input = {
            "combat": {"round": 2, "initiative_order": ["V"]},
        }
        apply_net_combat_state(ps, tool_input)
        self.assertEqual(ps["combat"]["round"], 2)
        self.assertIn("vehicles", ps["combat"])
        self.assertEqual(ps["combat"]["vehicles"]["Bike"]["sdp_current"], 30)

    def test_apply_cpred_combat_state_bootstraps_vehicle_with_new_combat(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {}
        tool_input = {
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
            "vehicle_updates": [{
                "name": "Car",
                "set_vehicle_stats": {
                    "type": "land", "sdp_max": 50, "sp": 0,
                    "combat_move": 20, "occupants": ["V"], "driver": "V",
                    "upgrades": [],
                },
            }],
        }
        apply_cpred_combat_state(ps, tool_input, game_state={"edgerunners": {}})
        self.assertIn("vehicles", ps["combat"])
        self.assertIn("Car", ps["combat"]["vehicles"])
        self.assertEqual(ps["combat"]["vehicles"]["Car"]["sdp_current"], 50)

    def test_apply_cpred_combat_state_vehicle_delta_survives_combat_payload_vehicles(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {
            "combat": {
                "round": 1, "initiative_order": ["V"], "current_turn": "V",
                "vehicles": {"Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"}},
            },
        }
        tool_input = {
            "vehicle_updates": [{"name": "Car", "sdp_delta": -15}],
            "combat": {"round": 2, "initiative_order": ["V"], "current_turn": "V", "vehicles": {}},
        }
        apply_cpred_combat_state(ps, tool_input, game_state={"edgerunners": {}})
        self.assertIn("Car", ps["combat"]["vehicles"])
        self.assertEqual(ps["combat"]["vehicles"]["Car"]["sdp_current"], 35)

    def test_apply_cpred_combat_state_preserves_cover_on_new_combat(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {}
        tool_input = {
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
            "cover_state": [{"name": "V", "in_cover": True, "cover_type": "Concrete", "cover_hp": 20}],
        }
        apply_cpred_combat_state(ps, tool_input, game_state={"edgerunners": {}})
        self.assertTrue(ps["combat"]["cover"]["V"]["in_cover"])
        self.assertEqual(ps["combat"]["cover"]["V"]["cover_hp"], 20)

    def test_apply_cpred_combat_state_replaces_cover_snapshot(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {
            "combat": {
                "round": 1,
                "initiative_order": ["V", "Enemy"],
                "current_turn": "V",
                "cover": {"OldEnemy": {"in_cover": True, "cover_type": "Wall", "cover_hp": 15}},
            },
        }
        tool_input = {
            "cover_state": [{"name": "V", "in_cover": True, "cover_type": "Concrete", "cover_hp": 20}],
        }
        apply_cpred_combat_state(ps, tool_input, game_state={"edgerunners": {}})
        self.assertIn("V", ps["combat"]["cover"])
        self.assertNotIn("OldEnemy", ps["combat"]["cover"])

    def test_apply_cpred_combat_state_malformed_cover_does_not_clear_existing(self):
        from game_systems.cpred import apply_cpred_combat_state
        ps = {
            "combat": {
                "round": 1,
                "initiative_order": ["V"],
                "current_turn": "V",
                "cover": {"V": {"in_cover": True, "cover_type": "Wall", "cover_hp": 15}},
            },
        }
        tool_input = {"cover_state": [{"bad": "entry"}]}
        apply_cpred_combat_state(ps, tool_input, game_state={"edgerunners": {}})
        self.assertIn("V", ps["combat"]["cover"])


class TestMainCombatCompletionSemantics(unittest.TestCase):
    """Regression tests for explicit combat completion signaling in main.py."""

    def test_is_combat_marked_complete_requires_explicit_none_or_flag(self):
        from main import _is_combat_marked_complete

        self.assertFalse(_is_combat_marked_complete({}))
        self.assertFalse(_is_combat_marked_complete({"combat": {"round": 1}}))
        self.assertTrue(_is_combat_marked_complete({"combat": None}))
        self.assertTrue(_is_combat_marked_complete({"combat_complete": True}))


class TestApplyVehicleUpdatesFallback(unittest.TestCase):
    """Regression tests for fallback vehicle status normalization."""

    def test_positive_sdp_does_not_force_active_status(self):
        from main import _apply_vehicle_updates_fallback

        combat = {"vehicles": {"Car": {"sdp_current": 5, "sdp_max": 50, "sp": 0, "status": "destroyed"}}}
        _apply_vehicle_updates_fallback(combat, [{"name": "Car", "sdp_delta": 1}])
        self.assertEqual(combat["vehicles"]["Car"]["status"], "destroyed")

    def test_unknown_vehicle_without_deltas_is_ignored(self):
        from main import _apply_vehicle_updates_fallback

        combat = {"vehicles": {}}
        _apply_vehicle_updates_fallback(combat, [{"name": "Ghost"}])
        self.assertNotIn("Ghost", combat["vehicles"])

    def test_unknown_vehicle_with_set_vehicle_stats_bootstraps(self):
        from main import _apply_vehicle_updates_fallback

        combat = {"vehicles": {}}
        _apply_vehicle_updates_fallback(combat, [{
            "name": "Car",
            "set_vehicle_stats": {"type": "land", "sdp_max": 50, "sp": 10, "combat_move": 20},
            "driver": "V",
            "status": "active",
        }])
        self.assertIn("Car", combat["vehicles"])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 50)
        self.assertEqual(combat["vehicles"]["Car"]["sp"], 10)
        self.assertEqual(combat["vehicles"]["Car"]["status"], "active")

    def test_existing_vehicle_name_match_is_case_insensitive(self):
        from main import _apply_vehicle_updates_fallback

        combat = {"vehicles": {"Car": {"sdp_current": 50, "sdp_max": 50, "sp": 10, "status": "active"}}}
        _apply_vehicle_updates_fallback(combat, [{"name": " car ", "sdp_delta": -5, "sp_delta": -2}])
        self.assertEqual(combat["vehicles"]["Car"]["sdp_current"], 45)
        self.assertEqual(combat["vehicles"]["Car"]["sp"], 8)
        self.assertNotIn(" car ", combat["vehicles"])

    def test_bootstrap_normalizes_occupants_and_driver(self):
        from main import _apply_vehicle_updates_fallback

        combat = {"vehicles": {}}
        _apply_vehicle_updates_fallback(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land",
                "sdp_max": 50,
                "sp": 10,
                "combat_move": 20,
                "occupants": [{"name": " V "}, " Jackie ", {"name": ""}, 1],
                "driver": {"name": " V "},
                "upgrades": [],
            },
        }])
        self.assertEqual(combat["vehicles"]["Car"]["occupants"], ["V", "Jackie"])
        self.assertEqual(combat["vehicles"]["Car"]["driver"], "V")


class TestResolveMechanicsTrackingHelpers(unittest.TestCase):
    """Regression tests for resolver tracking extraction and TAR persistence."""

    def test_extract_resolve_mechanics_tracking_state(self):
        from main import _extract_resolve_mechanics_tracking_state
        pipeline_state = {
            "game_state": {"edgerunners": {"V": {"hp": {"current": 32}}}},
            "character_states": {
                "Enemy": {"data": {"vitals": [{"label": "HP", "current": 17, "max": 30}]}},
            },
            "combat": {"vehicles": {"Car": {"sdp_current": 40, "sp": 10, "status": "active"}}},
        }
        hp_map, vehicle_map = _extract_resolve_mechanics_tracking_state(pipeline_state)
        self.assertEqual(hp_map["V"], 32)
        self.assertEqual(hp_map["Enemy"], 17)
        self.assertEqual(vehicle_map["Car"], 40)
        self.assertEqual(vehicle_map["Car:sp"], 10)

    def test_extract_resolve_mechanics_tracking_state_canonicalizes_vehicle_case(self):
        from main import _extract_resolve_mechanics_tracking_state
        pipeline_state = {
            "combat": {
                "vehicles": {
                    "Car": {"sdp_current": 40, "sp": 10, "status": "active"},
                    " car ": {"sdp_current": 30, "sp": 8, "status": "active"},
                },
            },
        }
        _, vehicle_map = _extract_resolve_mechanics_tracking_state(pipeline_state)
        self.assertEqual(vehicle_map.get("Car"), 30)
        self.assertEqual(vehicle_map.get("Car:sp"), 8)

    def test_convert_state_ops_to_vehicle_updates_ignores_non_delta_vehicle_ops(self):
        from main import _convert_state_ops_to_vehicle_updates
        out = _convert_state_ops_to_vehicle_updates([
            {"op": "vehicle_status", "vehicle": "Ghost", "value": "disabled"},
            {"op": "vehicle_sdp", "vehicle": "Car", "change": -5},
        ])
        self.assertEqual(out, [{"name": "Car", "sdp_delta": -5}])

    def test_convert_state_ops_to_vehicle_updates_merges_case_insensitively(self):
        from main import _convert_state_ops_to_vehicle_updates
        out = _convert_state_ops_to_vehicle_updates([
            {"op": "vehicle_sdp", "vehicle": "Hellhound", "change": -5},
            {"op": "vehicle_sdp", "vehicle": "hellhound", "change": -3},
            {"op": "vehicle_sp", "vehicle": "HELLHOUND", "change": -1},
        ])
        self.assertEqual(out, [{"name": "Hellhound", "sdp_delta": -8, "sp_delta": -1}])

    def test_apply_tar_consumed_state_ops_zeroes_active_tar(self):
        from main import _apply_tar_consumed_state_ops
        pipeline_state = {"net_combat": {"active": True, "tar_stacks": 3}}
        _apply_tar_consumed_state_ops(pipeline_state, [{"op": "tar_consumed"}])
        self.assertEqual(pipeline_state["net_combat"]["tar_stacks"], 0)

    def test_deferred_vehicle_updates_not_lost_when_combat_still_missing(self):
        from main import _inject_resolver_ops_stateful, _apply_deferred_stateful_vehicle_updates
        tool_input = {}
        _inject_resolver_ops_stateful(
            tool_input,
            [{"op": "vehicle_sdp", "vehicle": "Car", "change": -5}],
            pipeline_state={},
            gs={},
        )
        self.assertIn("_resolver_vehicle_updates", tool_input)
        _apply_deferred_stateful_vehicle_updates(tool_input, pipeline_state={}, gs={})
        self.assertIn("_resolver_vehicle_updates", tool_input)

    def test_advance_tracking_maps_from_state_ops(self):
        from main import _advance_tracking_maps_from_state_ops
        hp_map = {"V": 20}
        vehicle_map = {"Car": 30, "Car:sp": 5}
        _advance_tracking_maps_from_state_ops(
            hp_map,
            vehicle_map,
            [
                {"op": "hp", "edgerunner": "V", "change": -7},
                {"op": "vehicle_sdp", "vehicle": "Car", "change": -12},
                {"op": "vehicle_sp", "vehicle": "Car", "change": -2},
            ],
        )
        self.assertEqual(hp_map["V"], 13)
        self.assertEqual(vehicle_map["Car"], 18)
        self.assertEqual(vehicle_map["Car:sp"], 3)

    def test_advance_tracking_maps_case_mismatch_does_not_split_keys(self):
        from main import _advance_tracking_maps_from_state_ops
        vehicle_map = {"Car": 30, "Car:sp": 5}
        _advance_tracking_maps_from_state_ops(
            {},
            vehicle_map,
            [
                {"op": "vehicle_sdp", "vehicle": "car", "change": -2},
                {"op": "vehicle_sp", "vehicle": "car", "change": -1},
            ],
        )
        self.assertEqual(vehicle_map["Car"], 28)
        self.assertEqual(vehicle_map["Car:sp"], 4)
        self.assertNotIn("car", vehicle_map)
        self.assertNotIn("car:sp", vehicle_map)

    def test_seed_vehicle_tracking_map_from_actions(self):
        from main import _seed_vehicle_tracking_map_from_actions
        vehicle_map = {}
        _seed_vehicle_tracking_map_from_actions(vehicle_map, [{
            "type": "vehicle_weak_point",
            "vehicle_name": "Enemy Car",
            "vehicle_sdp_current": 50,
            "vehicle_sp": 10,
        }])
        self.assertEqual(vehicle_map["Enemy Car"], 50)
        self.assertEqual(vehicle_map["Enemy Car:sp"], 10)

    def test_seed_vehicle_tracking_map_case_mismatch_reuses_existing_key(self):
        from main import _seed_vehicle_tracking_map_from_actions
        vehicle_map = {"Car": 0, "Car:sp": 0}
        _seed_vehicle_tracking_map_from_actions(vehicle_map, [{
            "type": "driving_check",
            "vehicle_name": "car",
        }])
        self.assertIn("Car", vehicle_map)
        self.assertIn("Car:sp", vehicle_map)
        self.assertNotIn("car", vehicle_map)
        self.assertNotIn("car:sp", vehicle_map)

    def test_seed_hp_tracking_map_from_actions(self):
        from main import _seed_hp_tracking_map_from_actions
        hp_map = {}
        _seed_hp_tracking_map_from_actions(hp_map, [{
            "type": "ramming",
            "target": "Ped",
            "target_hp_current": 17,
        }])
        self.assertEqual(hp_map["Ped"], 17)

    def test_seed_hp_tracking_map_case_mismatch_reuses_existing_key(self):
        from main import _seed_hp_tracking_map_from_actions
        hp_map = {"Ped": 9}
        _seed_hp_tracking_map_from_actions(hp_map, [{
            "type": "ramming",
            "target": " ped ",
            "target_hp_current": 17,
        }])
        self.assertEqual(hp_map, {"Ped": 9})

    def test_advance_tracking_maps_hp_case_mismatch_updates_existing_key(self):
        from main import _advance_tracking_maps_from_state_ops
        hp_map = {"Ped": 20}
        _advance_tracking_maps_from_state_ops(
            hp_map,
            {},
            [{"op": "hp", "edgerunner": " ped ", "change": -7}],
        )
        self.assertEqual(hp_map, {"Ped": 13})

    def test_is_net_combat_marked_complete_prefers_pipeline_state(self):
        from main import _is_net_combat_marked_complete
        self.assertTrue(
            _is_net_combat_marked_complete(
                {"combat_complete": True, "net_complete": False},
                {"net_combat": {"active": False, "combat_complete": True, "net_complete": True}},
            )
        )


class TestVehicleSchemaExposure(unittest.TestCase):
    """Tool schemas should expose vehicle_updates for combat reporting."""

    def test_cpred_combat_and_net_combat_schemas_include_vehicle_updates(self):
        from game_systems.cpred import (
            REPORT_CPRED_COMBAT_STATE_TOOL,
            REPORT_NET_COMBAT_STATE_TOOL,
            COMBAT_PLANNING_SCHEMA,
            NET_COMBAT_PLANNING_SCHEMA,
        )
        combat_props = REPORT_CPRED_COMBAT_STATE_TOOL["input_schema"]["properties"]
        net_props = REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["properties"]
        planning_props = COMBAT_PLANNING_SCHEMA["properties"]
        net_planning_props = NET_COMBAT_PLANNING_SCHEMA["properties"]
        self.assertIn("vehicle_updates", combat_props)
        self.assertIn("vehicle_updates", net_props)
        self.assertIn("vehicle_updates", planning_props)
        self.assertIn("vehicle_updates", net_planning_props)

    def test_vehicle_driver_schema_accepts_driver_object(self):
        from game_systems.cpred import REPORT_CPRED_COMBAT_STATE_TOOL, REPORT_NET_COMBAT_STATE_TOOL
        combat_driver_schema = (
            REPORT_CPRED_COMBAT_STATE_TOOL["input_schema"]["properties"]["vehicle_updates"]["items"]["properties"]["driver"]
        )
        net_driver_schema = (
            REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["properties"]["vehicle_updates"]["items"]["properties"]["driver"]
        )
        combat_set_driver_schema = (
            REPORT_CPRED_COMBAT_STATE_TOOL["input_schema"]["properties"]["vehicle_updates"]["items"]["properties"]["set_vehicle_stats"]["properties"]["driver"]
        )
        net_set_driver_schema = (
            REPORT_NET_COMBAT_STATE_TOOL["input_schema"]["properties"]["vehicle_updates"]["items"]["properties"]["set_vehicle_stats"]["properties"]["driver"]
        )
        for schema in (combat_driver_schema, net_driver_schema, combat_set_driver_schema, net_set_driver_schema):
            self.assertIn("oneOf", schema)
            self.assertTrue(any(s.get("type") == "object" for s in schema["oneOf"] if isinstance(s, dict)))


class TestVehicleOccupantsNormalization(unittest.TestCase):
    """Regression tests for occupant normalization in vehicle bootstrap/update."""

    def test_bootstrap_object_occupants_do_not_break_formatting(self):
        combat = {"vehicles": {}}
        _apply_vehicle_updates(combat, [{
            "name": "Car",
            "set_vehicle_stats": {
                "type": "land", "sdp_max": 50, "sp": 0, "combat_move": 20,
                "occupants": [{"name": "Bob"}, "Alice"], "driver": "V", "upgrades": [],
            },
        }])
        lines = _format_vehicle_lines(combat["vehicles"])
        joined = " ".join(lines)
        self.assertIn("Passengers: Bob, Alice", joined)


class TestRunModePipelineVehicleTracking(unittest.TestCase):
    """Regression test for run_mode_pipeline vehicle-state access."""

    def test_run_mode_pipeline_accepts_pipeline_state_for_vehicle_tracking(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from pipeline import run_mode_pipeline, PipelineStageResult

        class _Provider:
            def build_pipeline_request(self, **kwargs):
                return kwargs

            def send_request_stream(self, _client, _params):
                yield SimpleNamespace(
                    event_type="done",
                    usage={"input_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
                           "output_tokens": 0, "reasoning_tokens": 0},
                )

            def calculate_cost_with_tier(self, _parsed, _tier):
                return 0.0

        planning_result = PipelineStageResult(
            stage="planning",
            content='{"actions":[{"type":"initiative","combatants":[{"name":"V","ref":6}]}]}',
            parsed_json={"actions": [{"type": "initiative", "combatants": [{"name": "V", "ref": 6}]}]},
            usage={"input_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
                   "output_tokens": 0, "reasoning_tokens": 0},
            service_tier="standard",
        )

        with patch("pipeline.run_pipeline_stage", return_value=planning_result):
            events = list(run_mode_pipeline(
                provider=_Provider(),
                client=None,
                username="u",
                project="p",
                chat_name="c",
                mode="combat",
                planning_system="x",
                narration_system="y",
                mode_messages=[],
                user_content="go",
                planning_schema={},
                game_state={"edgerunners": {}},
                character_states={},
                pipeline_state={"combat": {"vehicles": {"Car": {"sdp_current": 10, "sp": 1, "status": "active"}}}},
            ))
        self.assertTrue(any(e[0] == "pipeline_done" for e in events))


class TestCPREDGameSystemHooks(unittest.TestCase):
    """Smoke tests for required CPRED game-system hooks."""

    def test_game_system_exposes_apply_vehicle_updates(self):
        self.assertIn("apply_vehicle_updates", GAME_SYSTEM)
        self.assertTrue(callable(GAME_SYSTEM["apply_vehicle_updates"]))


if __name__ == "__main__":
    unittest.main()
