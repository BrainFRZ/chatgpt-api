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
    _roll_check_die,
)
from game_systems.cpred_tables import (
    CRIT_INJURY_BODY,
    CRIT_INJURY_HEAD,
    RANGED_DV_TABLE,
    AUTOFIRE_DV_TABLE,
    calculate_hp,
)


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


if __name__ == "__main__":
    unittest.main()
