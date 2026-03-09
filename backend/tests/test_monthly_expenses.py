"""
Comprehensive tests for CPRED automated monthly expense processing.

Tests cover:
- Cost lookup tables
- New edgerunner fields
- _process_expenses() logic: deductions, catch-up, consequences
- Pending tier changes
- Multi-day and multi-month jumps
- Edge cases: body=0, unknown tiers, starvation death
- Notification format
- Injection output (EXPENSE STATUS, UPCOMING EXPENSES)
"""
import copy
import unittest
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred import (
    HOUSING_COSTS,
    HOUSING_BEDROOMS,
    HOUSING_STREET_TYPES,
    LIFESTYLE_COSTS,
    _default_edgerunner,
    _get_housing_groups,
    _housing_cost_for_person,
    _parse_game_date,
    _process_expenses,
    apply_game_state,
    build_game_injection,
    init_game_state,
)


class TestCostTables(unittest.TestCase):
    """Verify HOUSING_COSTS and LIFESTYLE_COSTS have expected keys/values."""

    def test_housing_costs_keys(self):
        expected = {
            "Living on The Street", "Living on The Street in a Vehicle",
            "Cube Hotel", "Cargo Container", "Studio Apartment",
            "Two-Bedroom Apartment", "Corporate Conapt", "Upscale Conapt",
            "Luxury Penthouse", "Corporate Beaverville House",
            "Corporate Beaverville McMansion",
        }
        self.assertEqual(set(HOUSING_COSTS.keys()), expected)

    def test_lifestyle_costs_keys(self):
        expected = {"Kibble", "Generic Prepak", "Good Prepak", "Fresh Food"}
        self.assertEqual(set(LIFESTYLE_COSTS.keys()), expected)

    def test_housing_costs_values_non_negative(self):
        for k, v in HOUSING_COSTS.items():
            self.assertGreaterEqual(v, 0, f"HOUSING_COSTS[{k!r}] should be non-negative")

    def test_lifestyle_costs_values_positive(self):
        for k, v in LIFESTYLE_COSTS.items():
            self.assertGreater(v, 0, f"LIFESTYLE_COSTS[{k!r}] should be positive")

    def test_housing_costs_sorted_ascending(self):
        values = list(HOUSING_COSTS.values())
        # Not necessarily sorted alphabetically, but cheapest should be 0, most expensive 15000
        self.assertEqual(min(values), 0)
        self.assertEqual(max(values), 15000)

    def test_housing_bedrooms_keys_match_costs(self):
        self.assertEqual(set(HOUSING_BEDROOMS.keys()), set(HOUSING_COSTS.keys()))

    def test_lifestyle_costs_sorted_ascending(self):
        self.assertEqual(min(LIFESTYLE_COSTS.values()), 100)
        self.assertEqual(max(LIFESTYLE_COSTS.values()), 1500)


class TestDefaultEdgerunner(unittest.TestCase):
    """Verify new fields exist with correct defaults."""

    def setUp(self):
        self.er = _default_edgerunner()

    def test_body_default(self):
        self.assertEqual(self.er["body"], 0)

    def test_endurance_base_default(self):
        self.assertEqual(self.er["endurance_base"], 0)

    def test_housing_paid_month_default(self):
        self.assertIsNone(self.er["housing_paid_month"])

    def test_lifestyle_paid_month_default(self):
        self.assertIsNone(self.er["lifestyle_paid_month"])

    def test_days_on_street_default(self):
        self.assertEqual(self.er["days_on_street"], 0)

    def test_days_without_food_default(self):
        self.assertEqual(self.er["days_without_food"], 0)

    def test_housing_pending_default(self):
        self.assertIsNone(self.er["housing_pending"])

    def test_lifestyle_pending_default(self):
        self.assertIsNone(self.er["lifestyle_pending"])

    def test_housing_shared_with_default(self):
        self.assertIsNone(self.er["housing_shared_with"])

    def test_housing_bedrooms_default(self):
        self.assertIsNone(self.er["housing_bedrooms"])

    def test_crammed_default(self):
        self.assertFalse(self.er["crammed"])

    def test_original_fields_still_present(self):
        """Ensure we didn't break existing fields."""
        for field in ["hp", "humanity", "luck", "armor", "eurobucks",
                      "death_save_count", "critical_injuries", "cyberware_effects",
                      "weapons", "lifestyle", "housing"]:
            self.assertIn(field, self.er, f"Missing original field: {field}")


class TestDateParsing(unittest.TestCase):
    """Test _parse_game_date() with various inputs."""

    def test_valid_date(self):
        d = _parse_game_date("2045-08-15")
        self.assertIsNotNone(d)
        self.assertEqual(d.year, 2045)
        self.assertEqual(d.month, 8)
        self.assertEqual(d.day, 15)

    def test_valid_date_leading_zeros(self):
        d = _parse_game_date("2045-01-03")
        self.assertEqual(d.month, 1)
        self.assertEqual(d.day, 3)

    def test_invalid_format(self):
        self.assertIsNone(_parse_game_date("August 15, 2045"))

    def test_none_input(self):
        self.assertIsNone(_parse_game_date(None))

    def test_empty_string(self):
        self.assertIsNone(_parse_game_date(""))

    def test_integer_input(self):
        self.assertIsNone(_parse_game_date(12345))

    def test_partial_date(self):
        self.assertIsNone(_parse_game_date("2045-08"))

    def test_invalid_month(self):
        self.assertIsNone(_parse_game_date("2045-13-01"))

    def test_invalid_day(self):
        self.assertIsNone(_parse_game_date("2045-02-30"))


def _make_game_state_with_er(er_name="V", **er_overrides):
    """Helper to build a game_state with a single edgerunner."""
    er = _default_edgerunner()
    er.update(er_overrides)
    return {
        "edgerunners": {er_name: er},
        "ip_tracker": {"session_scores": {"group": 0}, "awards": [], "balances": {}},
        "relationships": {},
        "factions": {},
    }


class TestProcessExpensesDeduction(unittest.TestCase):
    """Month boundary triggers deduction, updates paid_month, resets counters."""

    def test_housing_deducted_on_first(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 2500)  # 5000 - 2500
        self.assertEqual(er["housing_paid_month"], "2045-02")

    def test_lifestyle_deducted_on_first(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=3000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 1500)  # 3000 - 1500
        self.assertEqual(er["lifestyle_paid_month"], "2045-02")

    def test_both_deducted(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", lifestyle="Generic Prepak", eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 2200)  # 5000 - 2500 - 300
        self.assertEqual(er["housing_paid_month"], "2045-02")
        self.assertEqual(er["lifestyle_paid_month"], "2045-02")

    def test_days_reset_on_payment(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5000,
            days_on_street=5, days_without_food=3)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 0)

    def test_paid_notification_generated(self):
        gs = _make_game_state_with_er(
            housing="Cargo Container", eurobucks=2000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        notifs = gs.get("_pending_notifications", [])
        paid = [n for n in notifs if n["type"] == "expense_paid"]
        self.assertEqual(len(paid), 1)
        self.assertIn("Housing", paid[0]["summary"])

    def test_current_date_updated(self):
        gs = _make_game_state_with_er(eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertEqual(gs["current_date"], "2045-02-01")


class TestProcessExpensesCannotAfford(unittest.TestCase):
    """Insufficient eurobucks → no deduction, generates notification."""

    def test_housing_not_deducted_when_broke(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=500)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 500)  # Unchanged
        self.assertNotEqual(er["housing_paid_month"], "2045-02")

    def test_lifestyle_not_deducted_when_broke(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=500)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 500)

    def test_unpaid_notification_generated(self):
        gs = _make_game_state_with_er(
            housing="Luxury Penthouse", eurobucks=100)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        notifs = gs.get("_pending_notifications", [])
        unpaid = [n for n in notifs if n["type"] == "expense_unpaid"]
        self.assertEqual(len(unpaid), 1)
        self.assertIn("can't afford", unpaid[0]["summary"])

    def test_partial_afford_housing_only(self):
        """Can afford housing but not lifestyle."""
        gs = _make_game_state_with_er(
            housing="Cargo Container", lifestyle="Fresh Food",
            eurobucks=1500)  # 1000 for housing, but 1000 for food too
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        # Housing should be paid (1000), leaving 500, not enough for Fresh Food (1000)
        self.assertEqual(er["eurobucks"], 500)
        self.assertEqual(er["housing_paid_month"], "2045-02")
        self.assertNotEqual(er.get("lifestyle_paid_month"), "2045-02")


class TestProcessExpensesCatchUp(unittest.TestCase):
    """Earns money mid-month → auto-deducts on next turn."""

    def test_catchup_deducts_when_affordable(self):
        gs = _make_game_state_with_er(
            housing="Cargo Container", eurobucks=2000)
        gs["current_date"] = "2045-02-05"
        # housing_paid_month is None, so it's unpaid for Feb
        _process_expenses(gs, "2045-02-06")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 1000)  # 2000 - 1000
        self.assertEqual(er["housing_paid_month"], "2045-02")

    def test_catchup_resets_consequences(self):
        gs = _make_game_state_with_er(
            housing="Cargo Container", eurobucks=2000,
            days_on_street=3)
        gs["current_date"] = "2045-02-05"
        _process_expenses(gs, "2045-02-06")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 0)

    def test_catchup_generates_paid_notification(self):
        gs = _make_game_state_with_er(
            lifestyle="Kibble", eurobucks=500)
        gs["current_date"] = "2045-02-10"
        _process_expenses(gs, "2045-02-11")
        notifs = gs.get("_pending_notifications", [])
        paid = [n for n in notifs if n["type"] == "expense_paid"]
        self.assertTrue(len(paid) >= 1)
        self.assertIn("catch-up", paid[0]["summary"])


class TestPendingTierChanges(unittest.TestCase):
    """housing_pending/lifestyle_pending applied on 1st, then deducted at new rate."""

    def test_housing_pending_applied_on_first(self):
        gs = _make_game_state_with_er(
            housing="Luxury Penthouse", housing_pending="Cargo Container",
            eurobucks=3000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["housing"], "Cargo Container")
        self.assertIsNone(er["housing_pending"])
        self.assertEqual(er["eurobucks"], 2000)  # 3000 - 1000 (Cargo Container)
        self.assertEqual(er["housing_paid_month"], "2045-02")

    def test_lifestyle_pending_applied_on_first(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", lifestyle_pending="Kibble",
            eurobucks=500)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["lifestyle"], "Kibble")
        self.assertIsNone(er["lifestyle_pending"])
        self.assertEqual(er["eurobucks"], 400)  # 500 - 100 (Kibble)

    def test_pending_not_applied_mid_month(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", housing_pending="Cargo Container",
            eurobucks=5000)
        gs["current_date"] = "2045-02-10"
        _process_expenses(gs, "2045-02-11")
        er = gs["edgerunners"]["V"]
        # Pending should NOT be applied mid-month
        self.assertEqual(er["housing"], "Two-Bedroom Apartment")
        self.assertEqual(er["housing_pending"], "Cargo Container")


class TestPendingOps(unittest.TestCase):
    """housing_pending and lifestyle_pending ops store correctly in edgerunner."""

    def test_housing_pending_op(self):
        gs = init_game_state()
        gs["edgerunners"]["V"] = _default_edgerunner()
        apply_game_state(gs, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "housing_pending", "value": "Cargo Container"}
            ]
        }, 1)
        self.assertEqual(gs["edgerunners"]["V"]["housing_pending"], "Cargo Container")

    def test_lifestyle_pending_op(self):
        gs = init_game_state()
        gs["edgerunners"]["V"] = _default_edgerunner()
        apply_game_state(gs, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "lifestyle_pending", "value": "Fresh Food"}
            ]
        }, 1)
        self.assertEqual(gs["edgerunners"]["V"]["lifestyle_pending"], "Fresh Food")


class TestHousingConsequences(unittest.TestCase):
    """days_on_street increments, endurance check rolled, HP loss on failure."""

    @patch("game_systems.cpred.random.randint")
    def test_eviction_after_day_1(self, mock_randint):
        # d10=3, endurance_base=5 → total=8 <= 15 → FAIL; d6=4 → -4 HP
        mock_randint.side_effect = [3, 4]
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=5,
            hp={"current": 30, "max": 40, "seriously_wounded": False},
            body=6)
        gs["current_date"] = "2045-02-01"
        # Housing can't be paid (0 eurobucks), day 2 starts consequences
        _process_expenses(gs, "2045-02-02")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 1)
        self.assertEqual(er["hp"]["current"], 26)  # 30 - 4

    @patch("game_systems.cpred.random.randint")
    def test_endurance_check_passes(self, mock_randint):
        # d10=9, endurance_base=8 → total=17 > 15 → PASS
        mock_randint.return_value = 9
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=8,
            hp={"current": 30, "max": 40, "seriously_wounded": False},
            body=6)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-02")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 1)
        self.assertEqual(er["hp"]["current"], 30)  # Unchanged

    def test_endurance_base_zero_skips_roll(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=0,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-02")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 1)
        self.assertEqual(er["hp"]["current"], 30)  # No damage when no stat
        notifs = gs.get("_pending_notifications", [])
        consequences = [n for n in notifs if n["type"] == "expense_consequence"]
        if consequences:
            self.assertIn("endurance_base not set", consequences[0]["summary"])

    def test_no_consequences_before_day_2(self):
        """Day 1 (the 1st) should not trigger street sleeping — eviction is on the 2nd."""
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=8, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 0)


class TestLifestyleConsequences(unittest.TestCase):
    """days_without_food increments, grace period (days 1-7), death save after day 7."""

    def test_grace_period_no_roll(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        # Advance 7 days (through grace period)
        _process_expenses(gs, "2045-02-08")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_without_food"], 7)
        self.assertEqual(er["hp"]["current"], 30)  # No damage during grace

    @patch("game_systems.cpred.random.randint")
    def test_death_save_after_grace(self, mock_randint):
        # d10=3 < BODY 6 → survived
        mock_randint.return_value = 3
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        # Advance 8 days — day 8 triggers first death save
        _process_expenses(gs, "2045-02-09")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_without_food"], 8)
        self.assertEqual(er["hp"]["current"], 30)  # Survived

    def test_body_zero_skips_death_save(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=0,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-09")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_without_food"], 8)
        self.assertEqual(er["hp"]["current"], 30)  # No roll, no damage


class TestStarvationDeath(unittest.TestCase):
    """Death save failure sets HP to 0."""

    @patch("game_systems.cpred.random.randint")
    def test_death_save_fail_sets_hp_zero(self, mock_randint):
        # d10=7 >= BODY 6 → dead
        mock_randint.return_value = 7
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-09")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["hp"]["current"], 0)

    @patch("game_systems.cpred.random.randint")
    def test_natural_10_always_kills(self, mock_randint):
        # d10=10, BODY 12 → natural 10 always fails
        mock_randint.return_value = 10
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=12,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-09")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["hp"]["current"], 0)

    @patch("game_systems.cpred.random.randint")
    def test_death_generates_notification(self, mock_randint):
        mock_randint.return_value = 8
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-09")
        notifs = gs.get("_pending_notifications", [])
        consequences = [n for n in notifs if n["type"] == "expense_consequence"]
        self.assertTrue(len(consequences) >= 1)
        self.assertEqual(consequences[0]["result"], "dead")
        self.assertIn("DEAD", consequences[0]["summary"])

    @patch("game_systems.cpred.random.randint")
    def test_death_stops_further_processing(self, mock_randint):
        # First death save on day 8 kills, should not process day 9-15
        mock_randint.return_value = 8  # >= BODY 6
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, body=6,
            hp={"current": 30, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-15")
        er = gs["edgerunners"]["V"]
        # Only 8 days counted (1-7 grace + day 8 death), not 14
        self.assertEqual(er["days_without_food"], 8)


class TestMultiDayJump(unittest.TestCase):
    """Time skip processes each day, consequences accumulate correctly."""

    @patch("game_systems.cpred.random.randint")
    def test_multiple_endurance_checks(self, mock_randint):
        # 3 days: all fail (d10=2, d6=3 each time)
        mock_randint.side_effect = [2, 3, 2, 3, 2, 3]
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=5,
            hp={"current": 30, "max": 40, "seriously_wounded": False},
            body=6)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-04")  # 3 days (2nd, 3rd, 4th)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 3)
        self.assertEqual(er["hp"]["current"], 21)  # 30 - 3*3 = 21

    @patch("game_systems.cpred.random.randint")
    def test_mixed_pass_fail(self, mock_randint):
        # Day 1: d10=8, endurance_base=8 → 16 > 15 → pass
        # Day 2: d10=2, endurance_base=8 → 10 <= 15 → fail, d6=5
        mock_randint.side_effect = [8, 2, 5]
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=8,
            hp={"current": 30, "max": 40, "seriously_wounded": False},
            body=6)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-03")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 2)
        self.assertEqual(er["hp"]["current"], 25)  # 30 - 5


class TestMultiMonthJump(unittest.TestCase):
    """Crossing 2+ month boundaries deducts for each month."""

    def test_two_months_both_deducted(self):
        gs = _make_game_state_with_er(
            housing="Cargo Container", eurobucks=5000,
            housing_paid_month="2045-01")  # Jan already paid
        gs["current_date"] = "2045-01-28"
        _process_expenses(gs, "2045-03-02")
        er = gs["edgerunners"]["V"]
        # Feb 1st: -1000, Mar 1st: -1000 = 3000
        self.assertEqual(er["eurobucks"], 3000)
        self.assertEqual(er["housing_paid_month"], "2045-03")

    def test_three_months_lifestyle(self):
        gs = _make_game_state_with_er(
            lifestyle="Kibble", eurobucks=500,
            lifestyle_paid_month="2045-01")  # Jan already paid
        gs["current_date"] = "2045-01-15"
        _process_expenses(gs, "2045-04-01")
        er = gs["edgerunners"]["V"]
        # Feb: -100, Mar: -100, Apr: -100 = 200 remaining
        self.assertEqual(er["eurobucks"], 200)

    def test_runs_out_mid_multi_month(self):
        """Can afford 2 months but not 3."""
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5500, endurance_base=5, body=6,
            housing_paid_month="2045-01",
            hp={"current": 40, "max": 40, "seriously_wounded": False})
        gs["current_date"] = "2045-01-15"
        # Skip to Apr 1: Jan already paid, Feb -2500 (3000 left), Mar -2500 (500 left), Apr: can't afford (2500)
        with patch("game_systems.cpred.random.randint", return_value=10):  # All pass
            _process_expenses(gs, "2045-04-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 500)  # Couldn't pay April
        self.assertEqual(er["housing_paid_month"], "2045-03")


class TestNoExpensesWhenNoTier(unittest.TestCase):
    """Edgerunner with housing=None / lifestyle=None → no charges."""

    def test_no_housing_no_charge(self):
        gs = _make_game_state_with_er(eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)

    def test_no_lifestyle_no_charge(self):
        gs = _make_game_state_with_er(eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)

    def test_unknown_tier_no_charge(self):
        """Unknown tier name should not crash, just skip."""
        gs = _make_game_state_with_er(
            housing="Penthouse Suite", eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)  # Unknown tier → cost 0

    def test_no_consequences_without_tier(self):
        """No housing/lifestyle → no days_on_street/days_without_food."""
        gs = _make_game_state_with_er(eurobucks=0)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-05")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 0)
        self.assertEqual(er["days_without_food"], 0)


class TestNotificationFormat(unittest.TestCase):
    """Verify notification dicts have expected fields/types."""

    def test_paid_notification_fields(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5000)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        notifs = gs.get("_pending_notifications", [])
        paid = [n for n in notifs if n["type"] == "expense_paid"]
        self.assertEqual(len(paid), 1)
        self.assertIn("edgerunner", paid[0])
        self.assertIn("summary", paid[0])
        self.assertIn("new_balance", paid[0])
        self.assertEqual(paid[0]["edgerunner"], "V")
        self.assertEqual(paid[0]["new_balance"], 2500)

    def test_unpaid_notification_fields(self):
        gs = _make_game_state_with_er(
            housing="Luxury Penthouse", eurobucks=100)
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        notifs = gs.get("_pending_notifications", [])
        unpaid = [n for n in notifs if n["type"] == "expense_unpaid"]
        self.assertEqual(len(unpaid), 1)
        self.assertIn("edgerunner", unpaid[0])
        self.assertIn("summary", unpaid[0])

    @patch("game_systems.cpred.random.randint")
    def test_consequence_notification_fields(self, mock_randint):
        mock_randint.side_effect = [3, 4]  # d10=3, d6=4
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, endurance_base=5,
            hp={"current": 30, "max": 40, "seriously_wounded": False},
            body=6)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-02")
        notifs = gs.get("_pending_notifications", [])
        consequences = [n for n in notifs if n["type"] == "expense_consequence"]
        self.assertTrue(len(consequences) >= 1)
        self.assertIn("edgerunner", consequences[0])
        self.assertIn("summary", consequences[0])
        self.assertIn("result", consequences[0])
        self.assertIn("days_on_street", consequences[0])


class TestInjectionOutput(unittest.TestCase):
    """build_game_injection includes [EXPENSE STATUS] when consequences active."""

    def test_expense_status_shown_when_on_street(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=0, days_on_street=3)
        injection = build_game_injection(gs)
        self.assertIn("[EXPENSE STATUS]", injection)
        self.assertIn("sleeping on street (3 days)", injection)
        self.assertIn("[/EXPENSE STATUS]", injection)

    def test_expense_status_shown_when_starving(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, days_without_food=10)
        injection = build_game_injection(gs)
        self.assertIn("[EXPENSE STATUS]", injection)
        self.assertIn("no food (10 days, DAILY DEATH SAVES)", injection)

    def test_expense_status_grace_period(self):
        gs = _make_game_state_with_er(
            lifestyle="Fresh Food", eurobucks=0, days_without_food=5)
        injection = build_game_injection(gs)
        self.assertIn("[EXPENSE STATUS]", injection)
        self.assertIn("in grace period", injection)

    def test_no_expense_status_when_all_clear(self):
        gs = _make_game_state_with_er(eurobucks=5000)
        injection = build_game_injection(gs)
        self.assertNotIn("[EXPENSE STATUS]", injection)

    def test_upcoming_expenses_near_month_end(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", lifestyle="Fresh Food", eurobucks=1000)
        gs["current_date"] = "2045-01-28"
        injection = build_game_injection(gs)
        self.assertIn("[UPCOMING EXPENSES", injection)
        self.assertIn("CANNOT AFFORD", injection)

    def test_upcoming_expenses_not_shown_early_month(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", lifestyle="Fresh Food", eurobucks=1000)
        gs["current_date"] = "2045-01-15"
        injection = build_game_injection(gs)
        self.assertNotIn("[UPCOMING EXPENSES", injection)

    def test_upcoming_expenses_affordable(self):
        gs = _make_game_state_with_er(
            housing="Cargo Container", lifestyle="Kibble", eurobucks=5000)
        gs["current_date"] = "2045-01-28"
        injection = build_game_injection(gs)
        # When affordable, we might or might not show the warning
        # The plan says "Only injected when at least one edgerunner's total exceeds their balance"
        # 1000+100 = 1100 < 5000, so should NOT show
        self.assertNotIn("CANNOT AFFORD", injection)


class TestEndToEnd(unittest.TestCase):
    """Full cycle: bootstrap edgerunner → advance date → deduct → consequences → catch-up."""

    def test_full_lifecycle(self):
        gs = init_game_state()

        # Bootstrap: model sets initial state
        apply_game_state(gs, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "set", "fields": {
                    "hp": {"current": 40, "max": 40, "seriously_wounded": False},
                    "eurobucks": 3000, "body": 6, "endurance_base": 12,
                }},
                {"edgerunner": "V", "op": "housing", "value": "Two-Bedroom Apartment"},
                {"edgerunner": "V", "op": "lifestyle", "value": "Generic Prepak"},
            ],
            "hud_state": {"date": "2045-01-15"}
        }, 1)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 3000)
        self.assertEqual(er["housing"], "Two-Bedroom Apartment")
        self.assertEqual(er["body"], 6)
        self.assertEqual(gs["current_date"], "2045-01-15")

        # First turn auto-marks January as paid (first-turn initialization)
        self.assertEqual(er["housing_paid_month"], "2045-01")
        self.assertEqual(er["lifestyle_paid_month"], "2045-01")

        # Turn 2: advance to Feb 1 — expenses deducted
        apply_game_state(gs, {
            "hud_state": {"date": "2045-02-01"}
        }, 2)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 200)  # 3000 - 2500 - 300
        self.assertEqual(er["housing_paid_month"], "2045-02")
        self.assertEqual(er["lifestyle_paid_month"], "2045-02")

        # Turn 3: advance to Mar 1 — can't afford housing (2500 > 300)
        gs["_pending_notifications"] = []  # Clear old notifs
        apply_game_state(gs, {
            "hud_state": {"date": "2045-03-01"}
        }, 3)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 200)  # can't afford housing (2500) or lifestyle (300)
        self.assertEqual(er["housing_paid_month"], "2045-02")  # Didn't pay March
        self.assertEqual(er["lifestyle_paid_month"], "2045-02")  # Can't afford (300 > 200)

        # Turn 4: advance to Mar 5 — 4 days on street with consequences
        gs["_pending_notifications"] = []
        with patch("game_systems.cpred.random.randint", return_value=10):  # All endurance checks pass
            apply_game_state(gs, {
                "hud_state": {"date": "2045-03-05"}
            }, 4)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["days_on_street"], 4)  # Days 2, 3, 4, 5

        # Turn 5: gig payout + advance — catch-up pays housing
        gs["_pending_notifications"] = []
        apply_game_state(gs, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "eurobucks", "change": 5000, "reason": "Gig payout"}
            ],
            "hud_state": {"date": "2045-03-06"}
        }, 5)
        er = gs["edgerunners"]["V"]
        # 200 + 5000 = 5200, then housing catch-up -2500, lifestyle catch-up -300 = 2400
        self.assertEqual(er["eurobucks"], 2400)
        self.assertEqual(er["housing_paid_month"], "2045-03")
        self.assertEqual(er["days_on_street"], 0)  # Reset after catch-up


class TestImmediateDowngrade(unittest.TestCase):
    """Immediate tier change mid-month triggers catch-up at new rate."""

    def test_downgrade_triggers_catchup(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=1500, endurance_base=8, body=6,
            hp={"current": 40, "max": 40, "seriously_wounded": False},
            days_on_street=3)
        gs["current_date"] = "2045-02-05"
        # Player downgrades housing this turn, system should catch up at new rate
        apply_game_state(gs, {
            "edgerunner_ops": [
                {"edgerunner": "V", "op": "housing", "value": "Cargo Container"}
            ],
            "hud_state": {"date": "2045-02-06"}
        }, 1)
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["housing"], "Cargo Container")
        self.assertEqual(er["eurobucks"], 500)  # 1500 - 1000
        self.assertEqual(er["housing_paid_month"], "2045-02")
        self.assertEqual(er["days_on_street"], 0)  # Reset


class TestNoDateAdvance(unittest.TestCase):
    """No processing when date doesn't change or goes backward."""

    def test_same_date_no_processing(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5000)
        gs["current_date"] = "2045-02-01"
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)

    def test_backward_date_no_processing(self):
        gs = _make_game_state_with_er(
            housing="Two-Bedroom Apartment", eurobucks=5000)
        gs["current_date"] = "2045-02-15"
        _process_expenses(gs, "2045-02-10")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)

    def test_first_turn_no_processing(self):
        gs = _make_game_state_with_er(eurobucks=5000)
        # No current_date yet
        _process_expenses(gs, "2045-02-01")
        er = gs["edgerunners"]["V"]
        self.assertEqual(er["eurobucks"], 5000)
        self.assertEqual(gs["current_date"], "2045-02-01")


class TestMultipleEdgerunners(unittest.TestCase):
    """Expenses processed independently for each edgerunner."""

    def test_two_edgerunners_independent(self):
        gs = init_game_state()
        er_v = _default_edgerunner()
        er_v.update({"housing": "Two-Bedroom Apartment", "eurobucks": 5000})
        er_j = _default_edgerunner()
        er_j.update({"housing": "Cargo Container", "eurobucks": 500})
        gs["edgerunners"] = {"V": er_v, "Jackie": er_j}
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 2500)  # 5000 - 2500
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 500)  # Can't afford 1000


if __name__ == "__main__":
    unittest.main()
