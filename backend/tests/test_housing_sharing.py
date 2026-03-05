"""
Comprehensive tests for CPRED housing sharing system.

Tests cover:
- Housing bedroom/capacity tables
- New edgerunner fields (housing_shared_with, housing_bedrooms, crammed)
- housing_shared_with op handler via apply_game_state
- _get_housing_groups() logic (solo, multi, circular, transitive, etc.)
- Cost splitting formula (_housing_cost_for_person)
- _process_expenses() with sharing (split payments, shortfall coverage, eviction)
- Owner-covers-shortfall mechanics
- Crammed flag and notifications
- Street types with sharing
- build_game_injection() output with sharing/crammed
- Edge cases (owner clears housing, first-turn sharers, sharer leaves)
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
    _parse_game_date,
    _process_expenses,
    _get_housing_groups,
    _housing_cost_for_person,
    apply_game_state,
    build_game_injection,
    init_game_state,
)


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


def _make_game_state_multi(**named_ers):
    """Helper for multiple edgerunners.

    Usage: _make_game_state_multi(V={...}, Jackie={...})
    """
    gs = init_game_state()
    for name, overrides in named_ers.items():
        er = _default_edgerunner()
        er.update(overrides)
        gs["edgerunners"][name] = er
    return gs


# ============================================================
# 1. TestHousingBedrooms
# ============================================================
class TestHousingBedrooms(unittest.TestCase):
    """Verify bedroom counts and derived capacity (1 + bedrooms)."""

    def test_all_types_have_bedrooms(self):
        """Every key in HOUSING_COSTS has an entry in HOUSING_BEDROOMS."""
        for key in HOUSING_COSTS:
            self.assertIn(key, HOUSING_BEDROOMS,
                          f"HOUSING_BEDROOMS missing key: {key!r}")

    def test_capacity_cube_hotel(self):
        """Cube Hotel: 0 bedrooms -> capacity 1."""
        self.assertEqual(HOUSING_BEDROOMS["Cube Hotel"], 0)
        capacity = 1 + HOUSING_BEDROOMS["Cube Hotel"]
        self.assertEqual(capacity, 1)

    def test_capacity_two_bedroom(self):
        """Two-Bedroom Apartment: 2 bedrooms -> capacity 3."""
        self.assertEqual(HOUSING_BEDROOMS["Two-Bedroom Apartment"], 2)
        capacity = 1 + HOUSING_BEDROOMS["Two-Bedroom Apartment"]
        self.assertEqual(capacity, 3)

    def test_capacity_luxury_penthouse(self):
        """Luxury Penthouse: 3 bedrooms -> capacity 4."""
        self.assertEqual(HOUSING_BEDROOMS["Luxury Penthouse"], 3)
        capacity = 1 + HOUSING_BEDROOMS["Luxury Penthouse"]
        self.assertEqual(capacity, 4)

    def test_capacity_mcmansion(self):
        """Corporate Beaverville McMansion: 4 bedrooms -> capacity 5."""
        self.assertEqual(HOUSING_BEDROOMS["Corporate Beaverville McMansion"], 4)
        capacity = 1 + HOUSING_BEDROOMS["Corporate Beaverville McMansion"]
        self.assertEqual(capacity, 5)


# ============================================================
# 2. TestNewEdgerunnerFields
# ============================================================
class TestNewEdgerunnerFields(unittest.TestCase):
    """Verify new housing-sharing fields in _default_edgerunner()."""

    def setUp(self):
        self.er = _default_edgerunner()

    def test_housing_shared_with_default(self):
        self.assertIsNone(self.er["housing_shared_with"])

    def test_housing_bedrooms_default(self):
        self.assertIsNone(self.er["housing_bedrooms"])

    def test_crammed_default(self):
        self.assertFalse(self.er["crammed"])


# ============================================================
# 3. TestHousingSharedWithOp
# ============================================================
class TestHousingSharedWithOp(unittest.TestCase):
    """Test the housing_shared_with op via apply_game_state()."""

    def test_set_via_op(self):
        """Setting housing_shared_with to a name stores it on the edgerunner."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"eurobucks": 3000},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing_shared_with", "value": "V"},
        ]}, turn=1)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_shared_with"], "V")

    def test_clear_via_op(self):
        """Setting housing_shared_with to null clears it."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment"},
            Jackie={"housing_shared_with": "V"},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing_shared_with", "value": None},
        ]}, turn=2)
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_shared_with"])

    def test_sharing_clears_own_housing(self):
        """When housing_shared_with is set, the sharer's own housing and housing_pending are cleared."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment"},
            Jackie={"housing": "Cargo Container", "housing_pending": "Studio Apartment"},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing_shared_with", "value": "V"},
        ]}, turn=1)
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing"])
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_pending"])
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_shared_with"], "V")

    def test_clearing_does_not_restore_housing(self):
        """When housing_shared_with is cleared, housing stays None (model must re-set)."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment"},
            Jackie={"housing_shared_with": "V"},
        )
        # Clear the sharing
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing_shared_with", "value": None},
        ]}, turn=2)
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_shared_with"])
        # Housing is NOT automatically restored
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing"])


# ============================================================
# 4. TestGetHousingGroups
# ============================================================
class TestGetHousingGroups(unittest.TestCase):
    """Test _get_housing_groups() grouping logic."""

    def test_solo_owner(self):
        """V has housing, no sharers -> groups = {'V': ['V']}, sharers = set()."""
        ers = {"V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"}}
        groups, sharers = _get_housing_groups(ers)
        self.assertEqual(groups, {"V": ["V"]})
        self.assertEqual(sharers, set())

    def test_two_person_group(self):
        """V has housing, Jackie shares with V."""
        ers = {
            "V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"},
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
        }
        groups, sharers = _get_housing_groups(ers)
        self.assertIn("V", groups)
        self.assertIn("V", groups["V"])
        self.assertIn("Jackie", groups["V"])
        self.assertEqual(len(groups["V"]), 2)
        self.assertEqual(sharers, {"Jackie"})

    def test_three_person_group(self):
        """V has housing, Jackie + Judy share with V."""
        ers = {
            "V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"},
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
            "Judy": {**_default_edgerunner(), "housing_shared_with": "V"},
        }
        groups, sharers = _get_housing_groups(ers)
        self.assertIn("V", groups)
        self.assertEqual(len(groups["V"]), 3)
        self.assertIn("V", groups["V"])
        self.assertIn("Jackie", groups["V"])
        self.assertIn("Judy", groups["V"])
        self.assertEqual(sharers, {"Jackie", "Judy"})

    def test_sharing_with_nonexistent(self):
        """Jackie shares with 'Ghost' who doesn't exist -> not in any group, not in sharers."""
        ers = {
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "Ghost"},
        }
        groups, sharers = _get_housing_groups(ers)
        self.assertEqual(sharers, set())
        # Jackie has no housing, not sharing validly -> not in groups
        self.assertNotIn("Jackie", groups)

    def test_sharing_with_no_housing(self):
        """Jackie shares with V, but V has no housing -> not valid."""
        ers = {
            "V": {**_default_edgerunner()},  # no housing
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
        }
        groups, sharers = _get_housing_groups(ers)
        self.assertEqual(sharers, set())
        self.assertNotIn("V", groups)

    def test_circular_ref_prevented(self):
        """V shares with Jackie, Jackie shares with V -> both have housing_shared_with set,
        meaning their own housing is None (sharing clears it). Neither valid."""
        ers = {
            "V": {**_default_edgerunner(), "housing_shared_with": "Jackie"},
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
        }
        groups, sharers = _get_housing_groups(ers)
        # Neither has housing (sharing clears it), neither is a valid owner
        self.assertEqual(sharers, set())
        self.assertEqual(groups, {})

    def test_multiple_groups(self):
        """V has apartment, Rogue has container. Jackie shares V, Judy shares Rogue."""
        ers = {
            "V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"},
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
            "Rogue": {**_default_edgerunner(), "housing": "Cargo Container"},
            "Judy": {**_default_edgerunner(), "housing_shared_with": "Rogue"},
        }
        groups, sharers = _get_housing_groups(ers)
        self.assertEqual(len(groups), 2)
        self.assertIn("V", groups)
        self.assertIn("Rogue", groups)
        self.assertEqual(set(groups["V"]), {"V", "Jackie"})
        self.assertEqual(set(groups["Rogue"]), {"Rogue", "Judy"})
        self.assertEqual(sharers, {"Jackie", "Judy"})

    def test_transitive_sharing_prevented(self):
        """V has housing, Jackie shares V (valid), Judy shares Jackie (invalid: Jackie
        has no housing since sharing clears it, plus Jackie is itself a sharer)."""
        ers = {
            "V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"},
            "Jackie": {**_default_edgerunner(), "housing_shared_with": "V"},
            "Judy": {**_default_edgerunner(), "housing_shared_with": "Jackie"},
        }
        groups, sharers = _get_housing_groups(ers)
        # Jackie is a valid sharer of V
        self.assertIn("Jackie", sharers)
        # Judy is NOT a valid sharer (Jackie has no housing, Jackie is a sharer)
        self.assertNotIn("Judy", sharers)
        # Judy not in V's group
        self.assertNotIn("Judy", groups.get("V", []))


# ============================================================
# 5. TestCostSplitting
# ============================================================
class TestCostSplitting(unittest.TestCase):
    """Test _housing_cost_for_person() cost splitting formula."""

    def _setup_group(self, housing, *sharer_names):
        """Build edgerunners dict and compute groups for a single owner with sharers."""
        ers = {"V": {**_default_edgerunner(), "housing": housing}}
        for s in sharer_names:
            ers[s] = {**_default_edgerunner(), "housing_shared_with": "V"}
        groups, sharers = _get_housing_groups(ers)
        return ers, groups, sharers

    def test_two_way_split(self):
        """Two-Bedroom (2500eb), 2 people -> owner=1250, sharer=1250."""
        ers, groups, sharers = self._setup_group("Two-Bedroom Apartment", "Jackie")
        owner_cost, base, _, n = _housing_cost_for_person("V", ers["V"], ers, groups, sharers)
        sharer_cost, _, _, _ = _housing_cost_for_person("Jackie", ers["Jackie"], ers, groups, sharers)
        self.assertEqual(base, 2500)
        self.assertEqual(n, 2)
        self.assertEqual(owner_cost, 1250)
        self.assertEqual(sharer_cost, 1250)
        self.assertEqual(owner_cost + sharer_cost, 2500)

    def test_three_way_split(self):
        """Two-Bedroom (2500eb), 3 people -> sharer=833, owner=834. Total=2500."""
        ers, groups, sharers = self._setup_group("Two-Bedroom Apartment", "Jackie", "Judy")
        owner_cost, base, _, n = _housing_cost_for_person("V", ers["V"], ers, groups, sharers)
        sharer1_cost, _, _, _ = _housing_cost_for_person("Jackie", ers["Jackie"], ers, groups, sharers)
        sharer2_cost, _, _, _ = _housing_cost_for_person("Judy", ers["Judy"], ers, groups, sharers)
        self.assertEqual(base, 2500)
        self.assertEqual(n, 3)
        self.assertEqual(sharer1_cost, 833)  # 2500 // 3
        self.assertEqual(sharer2_cost, 833)
        self.assertEqual(owner_cost, 834)    # 2500 - 2*833
        self.assertEqual(owner_cost + sharer1_cost + sharer2_cost, 2500)

    def test_solo_full_cost(self):
        """Owner alone -> full 2500."""
        ers = {"V": {**_default_edgerunner(), "housing": "Two-Bedroom Apartment"}}
        groups, sharers = _get_housing_groups(ers)
        cost, base, _, n = _housing_cost_for_person("V", ers["V"], ers, groups, sharers)
        self.assertEqual(cost, 2500)
        self.assertEqual(base, 2500)
        self.assertEqual(n, 1)

    def test_free_housing_split(self):
        """Corporate Conapt (0eb), 3 people -> all pay 0."""
        ers, groups, sharers = self._setup_group("Corporate Conapt", "Jackie", "Judy")
        owner_cost, _, _, _ = _housing_cost_for_person("V", ers["V"], ers, groups, sharers)
        s1_cost, _, _, _ = _housing_cost_for_person("Jackie", ers["Jackie"], ers, groups, sharers)
        s2_cost, _, _, _ = _housing_cost_for_person("Judy", ers["Judy"], ers, groups, sharers)
        self.assertEqual(owner_cost, 0)
        self.assertEqual(s1_cost, 0)
        self.assertEqual(s2_cost, 0)

    def test_odd_split_remainder(self):
        """Cargo Container (1000eb), 3 people -> sharer=333, owner=334. Total=1000."""
        ers, groups, sharers = self._setup_group("Cargo Container", "Jackie", "Judy")
        owner_cost, base, _, n = _housing_cost_for_person("V", ers["V"], ers, groups, sharers)
        s1_cost, _, _, _ = _housing_cost_for_person("Jackie", ers["Jackie"], ers, groups, sharers)
        s2_cost, _, _, _ = _housing_cost_for_person("Judy", ers["Judy"], ers, groups, sharers)
        self.assertEqual(base, 1000)
        self.assertEqual(n, 3)
        self.assertEqual(s1_cost, 333)
        self.assertEqual(s2_cost, 333)
        self.assertEqual(owner_cost, 334)
        self.assertEqual(owner_cost + s1_cost + s2_cost, 1000)


# ============================================================
# 6. TestProcessExpensesSharing
# ============================================================
class TestProcessExpensesSharing(unittest.TestCase):
    """Test _process_expenses() with housing sharing scenarios."""

    def test_both_pay_shared(self):
        """V owns Two-Bedroom (2500), Jackie shares. Both have enough money.
        Advance from Jan 31 to Feb 1: V pays 1250, Jackie pays 1250."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 3750)
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 3750)
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_sharer_broke_owner_covers(self):
        """V owns Two-Bedroom, Jackie shares. Jackie has 0. V covers Jackie's share.
        V: 5000 - 1250 (own) - 1250 (Jackie's deficit) = 2500."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 2500)
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 0)
        # Both should be marked as paid
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_sharer_partial_owner_covers_deficit(self):
        """V owns Two-Bedroom, Jackie shares. Jackie has 500, needs 1250. Deficit=750.
        Jackie -> 0, V pays own 1250 + covers 750 = 2000 total deducted from V."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 500,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # V: 5000 - 1250 (own share) - 750 (Jackie's deficit) = 3000
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 3000)
        # Jackie: 500 - 500 = 0 (paid what they could)
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 0)
        # Both paid
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_owner_cant_cover_sharer_evicted(self):
        """V owns Two-Bedroom, Jackie shares. V has 1500, Jackie has 0.
        V pays own 1250 (has 250 left). Jackie deficit 1250. V only has 250 < 1250. Jackie evicted."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 1500,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # V paid their own share: 1500 - 1250 = 250
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 250)
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        # Jackie: not paid (evicted)
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 0)
        self.assertNotEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")
        # Unpaid notification should exist for Jackie
        notifs = gs.get("_pending_notifications", [])
        unpaid = [n for n in notifs if n.get("type") == "expense_unpaid"
                  and n.get("edgerunner") == "Jackie"]
        self.assertTrue(len(unpaid) > 0, "Jackie should have an unpaid notification")

    def test_neither_can_afford(self):
        """V has 0, Jackie has 0. Both unpaid."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 0,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertNotEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        self.assertNotEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_catchup_with_sharing(self):
        """V and Jackie sharing, mid-month. Jackie gets money, catches up at shared rate.
        Advance from Feb 10 to Feb 15: Jackie already paid Jan, hasn't paid Feb.
        V is already paid for Feb. Jackie has enough to pay shared rate."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000,
               "housing_paid_month": "2045-02"},
            Jackie={"housing_shared_with": "V", "eurobucks": 2000,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-02-10"
        _process_expenses(gs, "2045-02-15")
        # Jackie should catch up: pay 1250 (shared rate for 2-person split)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 750)  # 2000 - 1250

    def test_free_housing_sharing(self):
        """Corporate Conapt shared by 3. No money deducted.
        All members (owner + sharers) are auto-marked paid via cost<=0 branch."""
        gs = _make_game_state_multi(
            V={"housing": "Corporate Conapt", "eurobucks": 1000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 500,
                    "housing_paid_month": "2045-01"},
            Judy={"housing_shared_with": "V", "eurobucks": 300,
                  "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # No money deducted from anyone
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 1000)
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 500)
        self.assertEqual(gs["edgerunners"]["Judy"]["eurobucks"], 300)
        # All marked paid for Feb (cost <= 0 branch in Pass 1 for owner, Pass 2 for sharers)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Judy"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")


# ============================================================
# 7. TestOwnerCoversShortfall
# ============================================================
class TestOwnerCoversShortfall(unittest.TestCase):
    """Test owner shortfall coverage mechanics in detail."""

    def test_owner_covers_full_deficit(self):
        """Owner covers entire deficit when sharer has 0."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # V pays: 1250 (own) + 1250 (Jackie deficit) = 2500
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 7500)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_owner_cant_cover(self):
        """Owner doesn't have enough to cover the deficit after paying own share."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 1300,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # V pays own 1250, has 50 left. Can't cover Jackie's 1250 deficit.
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 50)
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        # Jackie not paid
        self.assertNotEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_shortfall_notification(self):
        """Verify notification text includes shortfall coverage info."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        notifs = gs.get("_pending_notifications", [])
        # Find V's paid notification that mentions covering shortfall
        v_paid = [n for n in notifs if n.get("type") == "expense_paid"
                  and n.get("edgerunner") == "V"]
        self.assertTrue(len(v_paid) > 0)
        # Should mention "covered" and "shortfall"
        summary = v_paid[0].get("summary", "")
        self.assertIn("covered", summary.lower())
        self.assertIn("shortfall", summary.lower())

    def test_mid_month_catchup_shortfall(self):
        """Shortfall coverage works during mid-month catch-up too."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000,
               "housing_paid_month": "2045-02"},
            Jackie={"housing_shared_with": "V", "eurobucks": 0,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-02-05"
        _process_expenses(gs, "2045-02-10")
        # Jackie should catch up via owner covering shortfall
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")
        # V covered Jackie's 1250 deficit
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 8750)


# ============================================================
# 8. TestCrammedFlag
# ============================================================
class TestCrammedFlag(unittest.TestCase):
    """Test crammed flag logic after _process_expenses()."""

    def _run_month_advance(self, gs):
        """Advance from Jan 31 to Feb 1 to trigger expense processing + crammed check."""
        gs["current_date"] = "2045-01-31"
        # Make sure everyone is paid for Jan
        for er in gs["edgerunners"].values():
            if er.get("housing_paid_month") is None:
                er["housing_paid_month"] = "2045-01"
        _process_expenses(gs, "2045-02-01")

    def test_within_capacity(self):
        """Two-Bedroom (2BR, capacity 3), 2 occupants -> not crammed."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        self.assertFalse(gs["edgerunners"]["V"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Jackie"]["crammed"])

    def test_at_capacity(self):
        """Two-Bedroom (capacity 3), 3 occupants -> not crammed."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        self.assertFalse(gs["edgerunners"]["V"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Jackie"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Judy"]["crammed"])

    def test_over_capacity(self):
        """Two-Bedroom (capacity 3), 4 occupants -> crammed."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
            River={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        self.assertTrue(gs["edgerunners"]["V"]["crammed"])
        self.assertTrue(gs["edgerunners"]["Jackie"]["crammed"])
        self.assertTrue(gs["edgerunners"]["Judy"]["crammed"])
        self.assertTrue(gs["edgerunners"]["River"]["crammed"])

    def test_cube_hotel_two_people(self):
        """Cube Hotel (0BR, capacity 1), 2 people -> crammed."""
        gs = _make_game_state_multi(
            V={"housing": "Cube Hotel", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        self.assertTrue(gs["edgerunners"]["V"]["crammed"])
        self.assertTrue(gs["edgerunners"]["Jackie"]["crammed"])

    def test_custom_bedrooms(self):
        """housing_bedrooms=3 overrides default (Cube Hotel 0BR) -> capacity 4."""
        gs = _make_game_state_multi(
            V={"housing": "Cube Hotel", "eurobucks": 5000, "housing_bedrooms": 3},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
            River={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        # capacity = 1 + 3 = 4; 4 occupants = at capacity, not crammed
        self.assertFalse(gs["edgerunners"]["V"]["crammed"])

    def test_crammed_notification(self):
        """Verify housing_crammed notification is generated."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
            River={"housing_shared_with": "V", "eurobucks": 5000},
        )
        self._run_month_advance(gs)
        notifs = gs.get("_pending_notifications", [])
        crammed_notifs = [n for n in notifs if n.get("type") == "housing_crammed"]
        self.assertTrue(len(crammed_notifs) > 0, "Should have housing_crammed notification")
        cn = crammed_notifs[0]
        self.assertEqual(cn["owner"], "V")
        self.assertEqual(cn["capacity"], 3)  # 1 + 2 bedrooms

    def test_crammed_cleared_when_sharer_evicted_v2(self):
        """4 in Two-Bedroom (capacity 3). One sharer can't pay and owner can't cover.
        Paid count drops to 3 -> not crammed."""
        # 4-way split of 2500: each share = 625, owner = 625
        # V pays 625, has 626-625=1 left. Can't cover River's 625 deficit.
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 626},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
            River={"housing_shared_with": "V", "eurobucks": 0},
        )
        self._run_month_advance(gs)
        # V: 626 - 625 = 1. Can't cover River (1 < 625). River evicted.
        # Only 3 paid (V, Jackie, Judy). capacity = 3 -> not crammed.
        self.assertFalse(gs["edgerunners"]["V"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Jackie"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Judy"]["crammed"])

    def test_street_types_not_crammed(self):
        """Living on The Street never crammed, even with 'sharers'."""
        gs = _make_game_state_multi(
            V={"housing": "Living on The Street", "eurobucks": 0, "endurance_base": 8},
            Jackie={"housing_shared_with": "V", "eurobucks": 0, "endurance_base": 8},
        )
        gs["current_date"] = "2045-01-31"
        for er in gs["edgerunners"].values():
            er["housing_paid_month"] = "2045-01"
        with patch("game_systems.cpred.random.randint", return_value=10):
            _process_expenses(gs, "2045-02-01")
        self.assertFalse(gs["edgerunners"]["V"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Jackie"]["crammed"])


# ============================================================
# 9. TestStreetTypes
# ============================================================
class TestStreetTypes(unittest.TestCase):
    """Test street-type housing behavior."""

    def test_street_zero_cost(self):
        """Living on The Street costs 0, no eurobucks deducted.
        Street types are marked as paid (cost<=0 branch) but still face
        endurance checks via the is_street_type branch in consequences."""
        gs = _make_game_state_multi(
            V={"housing": "Living on The Street", "eurobucks": 100,
               "housing_paid_month": "2045-01", "endurance_base": 8},
        )
        gs["current_date"] = "2045-01-31"
        with patch("game_systems.cpred.random.randint", return_value=10):
            _process_expenses(gs, "2045-02-01")
        # No eurobucks deducted
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 100)
        # Marked as paid (cost=0), but still faces street consequences
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")

    @patch("game_systems.cpred.random.randint")
    def test_street_endurance_check(self, mock_randint):
        """Despite being 'paid', Living on The Street has endurance checks."""
        # Set d10=5, endurance_base=8: total=13, vs DV15 -> FAIL
        # Then hp_loss roll: d10 for the d6 (we return 3 for HP loss)
        mock_randint.side_effect = [5, 3]
        gs = _make_game_state_multi(
            V={"housing": "Living on The Street", "eurobucks": 0,
               "housing_paid_month": "2045-01",
               "endurance_base": 8,
               "hp": {"current": 30, "max": 40, "seriously_wounded": False}},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # d10[5]+8=13 vs DV15 -> FAIL -> lose d6[3] HP
        self.assertEqual(gs["edgerunners"]["V"]["hp"]["current"], 27)

    @patch("game_systems.cpred.random.randint")
    def test_vehicle_endurance_check(self, mock_randint):
        """Living on The Street in a Vehicle also has endurance checks."""
        # d10=6, endurance_base=7: total=13 vs DV15 -> FAIL, d6=4 HP loss
        mock_randint.side_effect = [6, 4]
        gs = _make_game_state_multi(
            V={"housing": "Living on The Street in a Vehicle", "eurobucks": 0,
               "housing_paid_month": "2045-01",
               "endurance_base": 7,
               "hp": {"current": 35, "max": 40, "seriously_wounded": False}},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        self.assertEqual(gs["edgerunners"]["V"]["hp"]["current"], 31)

    @patch("game_systems.cpred.random.randint", return_value=10)
    def test_days_on_street_increments(self, mock_randint):
        """days_on_street goes up for street types each day, even across month boundaries.
        Street types are marked as paid (cost=0) but days_on_street is NOT reset
        because they're still sleeping rough."""
        gs = _make_game_state_multi(
            V={"housing": "Living on The Street", "eurobucks": 0,
               "housing_paid_month": "2045-01",
               "endurance_base": 8, "days_on_street": 0},
        )
        gs["current_date"] = "2045-01-28"
        _process_expenses(gs, "2045-02-02")
        # Loop processes: Jan 29, 30, 31, Feb 1, Feb 2 = 5 days
        # days_on_street accumulates across month boundary (not reset for street types)
        self.assertEqual(gs["edgerunners"]["V"]["days_on_street"], 5)


# ============================================================
# 10. TestInjectionSharing
# ============================================================
class TestInjectionSharing(unittest.TestCase):
    """Test build_game_injection() output for sharing/crammed scenarios."""

    def test_owner_shows_sharers(self):
        """V has Two-Bedroom, Jackie shares -> injection shows sharing info with bedroom count."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        injection = build_game_injection(gs)
        # Should show "Housing: Two-Bedroom Apartment (2BR, shared with Jackie)"
        self.assertIn("Two-Bedroom Apartment (2BR, shared with Jackie)", injection)

    def test_sharer_shows_owner(self):
        """Jackie shares V -> injection shows 'sharing V's Two-Bedroom Apartment'."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        injection = build_game_injection(gs)
        self.assertIn("sharing V's Two-Bedroom Apartment", injection)

    def test_crammed_in_state(self):
        """Crammed flag -> 'CRAMMED (-2 all actions)' in edgerunner state."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000, "crammed": True},
        )
        injection = build_game_injection(gs)
        self.assertIn("CRAMMED (-2 all actions)", injection)

    def test_crammed_in_expense_status(self):
        """'crammed housing (-2 all actions)' in [EXPENSE STATUS]."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000, "crammed": True},
        )
        injection = build_game_injection(gs)
        self.assertIn("crammed housing (-2 all actions)", injection)
        self.assertIn("[EXPENSE STATUS]", injection)

    def test_upcoming_expenses_show_shares(self):
        """Near month end, upcoming expenses show per-person share for shared housing."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000,
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-27"
        injection = build_game_injection(gs)
        self.assertIn("[UPCOMING EXPENSES", injection)
        # Jackie's upcoming should show "Housing share (V's Two-Bedroom Apartment): 1250eb"
        self.assertIn("Housing share", injection)
        # V's upcoming should show their share (1250eb)
        self.assertIn("1250eb", injection)


# ============================================================
# 11. TestEdgeCases
# ============================================================
class TestEdgeCases(unittest.TestCase):
    """Edge cases for housing sharing."""

    def test_owner_clears_housing(self):
        """V clears housing via op -> sharers' group dissolves."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        # V clears housing
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "V", "op": "housing", "value": None},
        ]}, turn=2)
        # Now groups should be empty (V has no housing)
        groups, sharers = _get_housing_groups(gs["edgerunners"])
        self.assertEqual(groups, {})
        self.assertEqual(sharers, set())
        # Jackie still has housing_shared_with set, but it's invalid
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_shared_with"], "V")

    def test_first_turn_marks_sharers_paid(self):
        """First turn initialization marks sharers as paid too (not just owners)."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        # First turn: old_date is None, new_date is valid
        _process_expenses(gs, "2045-02-15")
        # Both should be marked paid for 2045-02
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")

    def test_sharer_leaves_then_next_month(self):
        """Jackie stops sharing mid-month. Next month, V pays full cost."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000,
                    "housing_paid_month": "2045-01"},
        )
        # Jackie stops sharing
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing_shared_with", "value": None},
        ]}, turn=2)
        # Advance to next month
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # V now pays full cost (2500) since no sharers
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 7500)
        self.assertEqual(gs["edgerunners"]["V"]["housing_paid_month"], "2045-02")
        # Jackie has no housing, nothing to pay
        self.assertNotEqual(gs["edgerunners"]["Jackie"]["housing_paid_month"], "2045-02")


    def test_evicted_member_not_crammed(self):
        """Evicted sharer should NOT get crammed flag — they're on the street, not in the apartment.
        5 in Two-Bedroom (capacity 3): 4 pay (over capacity), 1 evicted.
        The 4 paid members are crammed, the evicted one is not."""
        # 5-way split of 2500: sharer_share = 500, owner_share = 500
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 600},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000},
            Judy={"housing_shared_with": "V", "eurobucks": 5000},
            River={"housing_shared_with": "V", "eurobucks": 5000},
            Kerry={"housing_shared_with": "V", "eurobucks": 0},
        )
        for er in gs["edgerunners"].values():
            er["housing_paid_month"] = "2045-01"
        gs["current_date"] = "2045-01-31"
        with patch("game_systems.cpred.random.randint", return_value=10):
            _process_expenses(gs, "2045-02-01")
        # V pays 500, has 100 left. Kerry has 0, deficit 500. V can't cover (100 < 500).
        # 4 paid > capacity 3 -> crammed for the 4, NOT for Kerry
        self.assertTrue(gs["edgerunners"]["V"]["crammed"])
        self.assertTrue(gs["edgerunners"]["Jackie"]["crammed"])
        self.assertTrue(gs["edgerunners"]["Judy"]["crammed"])
        self.assertTrue(gs["edgerunners"]["River"]["crammed"])
        self.assertFalse(gs["edgerunners"]["Kerry"]["crammed"])

    def test_upcoming_expenses_uses_pending(self):
        """When housing_pending is set near month end, upcoming expenses
        should show the pending housing cost, not the current one."""
        gs = _make_game_state_multi(
            V={"housing": "Luxury Penthouse", "eurobucks": 16000,
               "housing_pending": "Cargo Container",
               "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-27"
        injection = build_game_injection(gs)
        self.assertIn("[UPCOMING EXPENSES", injection)
        # Should show Cargo Container at 1000eb (pending), not Luxury Penthouse at 15000eb
        self.assertIn("Cargo Container", injection)
        self.assertIn("1000eb", injection)
        self.assertNotIn("15000eb", injection)


    def test_set_op_rejects_self_ref_housing_shared_with(self):
        """set op with housing_shared_with pointing to self is rejected (set to None)."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "V", "op": "set", "fields": {"housing_shared_with": "V"}},
        ]}, turn=2)
        self.assertIsNone(gs["edgerunners"]["V"]["housing_shared_with"])
        # Housing should NOT be cleared since self-ref was rejected
        self.assertEqual(gs["edgerunners"]["V"]["housing"], "Two-Bedroom Apartment")

    def test_set_op_housing_shared_with_clears_housing(self):
        """set op with housing_shared_with clears housing and housing_pending."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing": "Cargo Container", "housing_pending": "Studio Apartment",
                    "eurobucks": 3000},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "set", "fields": {"housing_shared_with": "V"}},
        ]}, turn=2)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing_shared_with"], "V")
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing"])
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_pending"])

    def test_housing_op_on_sharer_clears_sharing(self):
        """Setting housing directly on a sharer should clear housing_shared_with."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "housing", "value": "Cargo Container"},
        ]}, turn=2)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing"], "Cargo Container")
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_shared_with"])

    def test_injection_invalid_sharing_shows_invalid(self):
        """When sharer's owner has no housing, injection shows INVALID message."""
        gs = _make_game_state_multi(
            V={"eurobucks": 5000},  # No housing set
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        injection = build_game_injection(gs)
        self.assertIn("INVALID", injection)
        self.assertIn("owner has no housing", injection)


    def test_housing_pending_on_sharer_clears_sharing_at_boundary(self):
        """When a sharer has housing_pending set, applying it on the 1st clears housing_shared_with."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 10000,
               "housing_paid_month": "2045-01"},
            Jackie={"housing_shared_with": "V", "eurobucks": 5000,
                    "housing_pending": "Cargo Container",
                    "housing_paid_month": "2045-01"},
        )
        gs["current_date"] = "2045-01-31"
        _process_expenses(gs, "2045-02-01")
        # Jackie's pending applied on the 1st: housing = Cargo Container, sharing cleared
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing"], "Cargo Container")
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_shared_with"])
        # Jackie pays own Cargo Container (1000eb), not V's share
        self.assertEqual(gs["edgerunners"]["Jackie"]["eurobucks"], 4000)
        # V pays full Two-Bedroom (2500eb) solo
        self.assertEqual(gs["edgerunners"]["V"]["eurobucks"], 7500)

    def test_set_op_housing_clears_sharing(self):
        """set op with housing field on a sharer should clear housing_shared_with."""
        gs = _make_game_state_multi(
            V={"housing": "Two-Bedroom Apartment", "eurobucks": 5000},
            Jackie={"housing_shared_with": "V", "eurobucks": 3000},
        )
        apply_game_state(gs, {"edgerunner_ops": [
            {"edgerunner": "Jackie", "op": "set", "fields": {"housing": "Cargo Container"}},
        ]}, turn=2)
        self.assertEqual(gs["edgerunners"]["Jackie"]["housing"], "Cargo Container")
        self.assertIsNone(gs["edgerunners"]["Jackie"]["housing_shared_with"])


if __name__ == "__main__":
    unittest.main()
