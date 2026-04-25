"""Tests for the Cyberpunk RED program/hardware effect registry framework.

Step 1: framework only — no effect entries shipped yet. These tests verify
the pipeline drivers, snapshot/filter logic, ordering, error handling, and
edge cases independent of any specific program effect.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems import cpred_program_effects as cpe


class _RegistryMixin:
    """Helper: install a temporary registry and restore on tearDown."""

    def setUp(self):
        self._orig_registry = dict(cpe.PROGRAM_EFFECTS)
        cpe.PROGRAM_EFFECTS.clear()

    def tearDown(self):
        cpe.PROGRAM_EFFECTS.clear()
        cpe.PROGRAM_EFFECTS.update(self._orig_registry)

    def _hs(self, programs=None, hardware=None):
        return {
            "active_programs": programs or [],
            "installed_hardware": hardware or [],
        }


class TestEmptyRegistry(_RegistryMixin, unittest.TestCase):
    """All drivers return no-op results when no effects are registered."""

    def test_empty_interface_check(self):
        bonus, labels = cpe.run_interface_check_hooks("Backdoor", 14, self._hs())
        self.assertEqual(bonus, 0)
        self.assertEqual(labels, [])

    def test_empty_brain_damage(self):
        amt, ops, trace = cpe.run_brain_damage_hooks(8, self._hs(), {})
        self.assertEqual(amt, 8)
        self.assertEqual(ops, [])
        self.assertEqual(trace, [])

    def test_empty_program_attack_hit(self):
        ops, trace = cpe.run_program_attack_hit_hooks({"hit": True}, self._hs())
        self.assertEqual(ops, [])
        self.assertEqual(trace, [])

    def test_empty_alert_increase(self):
        delta, ops, trace = cpe.run_alert_increase_hooks(1, "failed_backdoor", self._hs())
        self.assertEqual(delta, 1)
        self.assertEqual(ops, [])
        self.assertEqual(trace, [])

    def test_empty_turn_start_end(self):
        for fn in (cpe.run_turn_start_hooks, cpe.run_turn_end_hooks):
            ops, trace = fn(self._hs(), {})
            self.assertEqual(ops, [])
            self.assertEqual(trace, [])

    def test_empty_status_change(self):
        new_status, ops, trace = cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed", self._hs(), {})
        self.assertEqual(new_status, "destroyed")
        self.assertEqual(ops, [])
        self.assertEqual(trace, [])


class TestSnapshotActivePrograms(_RegistryMixin, unittest.TestCase):

    def test_only_active_programs(self):
        progs = [
            {"name": "Sword", "status": "active"},
            {"name": "Shield", "status": "deactivated"},
            {"name": "Worm", "status": "derezzed"},
            {"name": "Armor", "status": "active"},
        ]
        snap = cpe._snapshot_active_programs(progs)
        names = [p["name"] for p in snap]
        self.assertEqual(names, ["Sword", "Armor"])

    def test_skips_duplicates(self):
        progs = [
            {"name": "Sword", "status": "active"},
            {"name": "Sword", "status": "active"},
        ]
        with patch("game_systems.cpred_program_effects.logger.warning") as warn:
            snap = cpe._snapshot_active_programs(progs)
        self.assertEqual(len(snap), 1)
        warn.assert_called_once()

    def test_handles_none_and_garbage(self):
        self.assertEqual(cpe._snapshot_active_programs(None), [])
        self.assertEqual(cpe._snapshot_active_programs("not a list"), [])
        self.assertEqual(cpe._snapshot_active_programs([None, "str", 42]), [])

    def test_skips_blank_names(self):
        progs = [{"name": "", "status": "active"}, {"status": "active"}]
        self.assertEqual(cpe._snapshot_active_programs(progs), [])


class TestSnapshotInstalledHardware(_RegistryMixin, unittest.TestCase):

    def test_string_list_shape(self):
        snap = cpe._snapshot_installed_hardware(
            ["Backup Drive", "Range Extension"])
        self.assertEqual(snap, ["Backup Drive", "Range Extension"])

    def test_tolerates_dict_shape(self):
        snap = cpe._snapshot_installed_hardware(
            [{"name": "Backup Drive"}, {"name": "Range Extension"}])
        self.assertEqual(snap, ["Backup Drive", "Range Extension"])

    def test_strips_blanks_and_garbage(self):
        snap = cpe._snapshot_installed_hardware(
            ["Backup Drive", "", None, 42, {"name": ""}])
        self.assertEqual(snap, ["Backup Drive"])


class TestPriorityOrdering(_RegistryMixin, unittest.TestCase):
    """Hooks run in `order` ascending; ties broken by registration order."""

    def test_lower_order_runs_first(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Beta"] = {
            "category": "defender", "is_hardware": False, "order": 20,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (calls.append("Beta") or amt, [])}}
        cpe.PROGRAM_EFFECTS["Alpha"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (calls.append("Alpha") or amt, [])}}
        progs = [{"name": "Alpha", "status": "active"},
                 {"name": "Beta", "status": "active"}]
        cpe.run_brain_damage_hooks(5, self._hs(programs=progs), {})
        self.assertEqual(calls, ["Alpha", "Beta"])  # order 10 → 20

    def test_tied_order_uses_registration_order(self):
        calls = []
        cpe.PROGRAM_EFFECTS["First"] = {
            "category": "defender", "is_hardware": False, "order": 50,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (calls.append("First") or amt, [])}}
        cpe.PROGRAM_EFFECTS["Second"] = {
            "category": "defender", "is_hardware": False, "order": 50,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (calls.append("Second") or amt, [])}}
        progs = [{"name": "Second", "status": "active"},  # active_programs order
                 {"name": "First", "status": "active"}]   # doesn't matter
        cpe.run_brain_damage_hooks(5, self._hs(programs=progs), {})
        # Registration order wins: First registered first
        self.assertEqual(calls, ["First", "Second"])


class TestProgramVsHardwareFilter(_RegistryMixin, unittest.TestCase):

    def test_program_entry_matches_active_program(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_interface_check":
                      lambda ab, total, p, hs: (calls.append("Worm") or 0, None)}}
        progs = [{"name": "Worm", "status": "active"}]
        cpe.run_interface_check_hooks("Backdoor", 10, self._hs(programs=progs))
        self.assertEqual(calls, ["Worm"])

    def test_program_entry_skipped_when_inactive(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_interface_check":
                      lambda ab, total, p, hs: (calls.append("Worm") or 0, None)}}
        for status in ("deactivated", "derezzed", "destroyed"):
            calls.clear()
            progs = [{"name": "Worm", "status": status}]
            cpe.run_interface_check_hooks("Backdoor", 10, self._hs(programs=progs))
            self.assertEqual(calls, [])

    def test_hardware_entry_matches_installed_hardware(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 50,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      (calls.append(pn) or new, [])}}
        cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed",
            self._hs(hardware=["Backup Drive"]), {})
        self.assertEqual(calls, ["Sword"])

    def test_hardware_entry_skipped_when_not_installed(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 50,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      (calls.append(pn) or new, [])}}
        cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed", self._hs(hardware=[]), {})
        self.assertEqual(calls, [])

    def test_hardware_substring_match(self):
        """Hardware names match by substring (e.g. 'Backup Drive' inside
        'Backup Drive Mk2')."""
        calls = []
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 50,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      (calls.append(pn) or new, [])}}
        cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed",
            self._hs(hardware=["Backup Drive Mk2"]), {})
        self.assertEqual(calls, ["Sword"])

    def test_hardware_filter_does_not_match_program_name(self):
        """A hardware entry won't fire if its name is in active_programs (sanity)."""
        calls = []
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 50,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      (calls.append(pn) or new, [])}}
        progs = [{"name": "Backup Drive", "status": "active"}]
        cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed",
            self._hs(programs=progs, hardware=[]), {})
        self.assertEqual(calls, [])


class TestBrainDamageEarlyExit(_RegistryMixin, unittest.TestCase):
    """Damage hooks short-circuit when amount drops to 0."""

    def test_zero_amount_stops_chain(self):
        calls = []

        def shield(amt, p, hs, gs):
            calls.append(("Shield", amt))
            return (0, [])  # absorb everything

        def armor(amt, p, hs, gs):
            calls.append(("Armor", amt))
            return (max(0, amt - 4), [])

        cpe.PROGRAM_EFFECTS["Shield"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound": shield}}
        cpe.PROGRAM_EFFECTS["Armor"] = {
            "category": "defender", "is_hardware": False, "order": 20,
            "hooks": {"on_brain_damage_inbound": armor}}
        progs = [{"name": "Shield", "status": "active"},
                 {"name": "Armor", "status": "active"}]
        final, ops, trace = cpe.run_brain_damage_hooks(8, self._hs(programs=progs), {})
        self.assertEqual(final, 0)
        self.assertEqual(calls, [("Shield", 8)])  # Armor never called
        self.assertEqual(len(trace), 1)

    def test_chain_passes_decremented_amount(self):
        calls = []
        cpe.PROGRAM_EFFECTS["Shield"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs:
                      (calls.append(("Shield", amt)) or max(0, amt - 3), [])}}
        cpe.PROGRAM_EFFECTS["Armor"] = {
            "category": "defender", "is_hardware": False, "order": 20,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs:
                      (calls.append(("Armor", amt)) or max(0, amt - 4), [])}}
        progs = [{"name": "Shield", "status": "active"},
                 {"name": "Armor", "status": "active"}]
        final, ops, trace = cpe.run_brain_damage_hooks(10, self._hs(programs=progs), {})
        # Shield sees 10 → 7; Armor sees 7 → 3
        self.assertEqual(calls, [("Shield", 10), ("Armor", 7)])
        self.assertEqual(final, 3)
        self.assertEqual(len(trace), 2)
        self.assertEqual(trace[0]["before"], 10)
        self.assertEqual(trace[0]["after"], 7)
        self.assertEqual(trace[1]["before"], 7)
        self.assertEqual(trace[1]["after"], 3)

    def test_zero_input_returns_zero(self):
        cpe.PROGRAM_EFFECTS["Shield"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (max(0, amt - 99), [])}}
        progs = [{"name": "Shield", "status": "active"}]
        final, _, _ = cpe.run_brain_damage_hooks(0, self._hs(programs=progs), {})
        self.assertEqual(final, 0)


class TestMalformedHookReturns(_RegistryMixin, unittest.TestCase):
    """Hooks returning bad shapes get a warning + are treated as no-op."""

    def test_brain_damage_bad_shape_logs_warning(self):
        cpe.PROGRAM_EFFECTS["Bad"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound": lambda *a: "not a tuple"}}
        progs = [{"name": "Bad", "status": "active"}]
        with patch("game_systems.cpred_program_effects.logger.warning") as warn:
            final, ops, _ = cpe.run_brain_damage_hooks(5, self._hs(programs=progs), {})
        self.assertEqual(final, 5)  # unchanged
        self.assertEqual(ops, [])
        warn.assert_called_once()

    def test_hook_returning_none_is_silent_noop(self):
        """None returns are valid 'no-op' (don't log)."""
        cpe.PROGRAM_EFFECTS["Quiet"] = {
            "category": "attacker", "is_hardware": False, "order": 10,
            "hooks": {"on_program_attack_hit": lambda *a: None}}
        progs = [{"name": "Quiet", "status": "active"}]
        with patch("game_systems.cpred_program_effects.logger.warning") as warn:
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True}, self._hs(programs=progs))
        self.assertEqual(ops, [])
        warn.assert_not_called()

    def test_hook_raising_exception_is_caught(self):
        cpe.PROGRAM_EFFECTS["Crash"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound":
                      lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))}}
        progs = [{"name": "Crash", "status": "active"}]
        # Should not raise — exception caught by _safe_call
        with patch("game_systems.cpred_program_effects.logger.exception") as logexc:
            final, ops, _ = cpe.run_brain_damage_hooks(5, self._hs(programs=progs), {})
        self.assertEqual(final, 5)
        self.assertEqual(ops, [])
        logexc.assert_called_once()

    def test_attack_hit_non_list_return_warns(self):
        cpe.PROGRAM_EFFECTS["WrongShape"] = {
            "category": "attacker", "is_hardware": False, "order": 10,
            "hooks": {"on_program_attack_hit":
                      lambda *a: {"not": "a list"}}}
        progs = [{"name": "WrongShape", "status": "active"}]
        with patch("game_systems.cpred_program_effects.logger.warning") as warn:
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True}, self._hs(programs=progs))
        self.assertEqual(ops, [])
        warn.assert_called_once()


class TestStateOpDefensiveCopy(_RegistryMixin, unittest.TestCase):
    """Hook-returned state ops are deep-copied; pipeline never mutates the
    hook's own data structures."""

    def test_brain_damage_ops_isolated(self):
        original_ops = [{"op": "rez_damage", "target": "Self", "damage": 1}]
        cpe.PROGRAM_EFFECTS["Shield"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound":
                      lambda amt, p, hs, gs: (0, original_ops)}}
        progs = [{"name": "Shield", "status": "active"}]
        _, ops, _ = cpe.run_brain_damage_hooks(5, self._hs(programs=progs), {})
        # Mutate the returned ops; original_ops should be untouched
        ops[0]["damage"] = 99
        self.assertEqual(original_ops[0]["damage"], 1)


class TestStatusChangeChain(_RegistryMixin, unittest.TestCase):
    """on_program_status_change hooks may rewrite new_status in sequence."""

    def test_rewrite_chain(self):
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 10,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      ("deactivated" if new == "destroyed" else new, [])}}
        new_status, ops, trace = cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed",
            self._hs(hardware=["Backup Drive"]), {})
        self.assertEqual(new_status, "deactivated")
        self.assertEqual(trace[0]["before"], "destroyed")
        self.assertEqual(trace[0]["after"], "deactivated")

    def test_status_change_fires_even_when_subject_inactive(self):
        """Subject (program being changed) doesn't need to be active — only
        the OBSERVERS (other active programs / installed hardware) matter."""
        calls = []
        cpe.PROGRAM_EFFECTS["Backup Drive"] = {
            "category": "hardware", "is_hardware": True, "order": 10,
            "hooks": {"on_program_status_change":
                      lambda pn, old, new, hs, gs:
                      (calls.append((pn, old, new)) or new, [])}}
        # Subject 'Sword' is destroyed (not active); Backup Drive is hardware.
        # Hook fires.
        cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed",
            self._hs(programs=[{"name": "Sword", "status": "destroyed"}],
                     hardware=["Backup Drive"]), {})
        self.assertEqual(calls, [("Sword", "active", "destroyed")])


class TestInterfaceCheckBonusAccumulation(_RegistryMixin, unittest.TestCase):

    def test_multiple_boosters_stack(self):
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_interface_check":
                      lambda ab, total, p, hs:
                      (2, "Worm") if ab == "Backdoor" else (0, None)}}
        cpe.PROGRAM_EFFECTS["Surge"] = {
            "category": "booster", "is_hardware": False, "order": 60,
            "hooks": {"on_interface_check": lambda ab, total, p, hs: (4, "Surge")}}
        progs = [{"name": "Worm", "status": "active"},
                 {"name": "Surge", "status": "active"}]
        bonus, labels = cpe.run_interface_check_hooks(
            "Backdoor", 14, self._hs(programs=progs))
        self.assertEqual(bonus, 6)
        self.assertEqual(labels, [("Worm", 2), ("Surge", 4)])

    def test_zero_bonus_skipped_from_labels(self):
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_interface_check":
                      lambda ab, total, p, hs:
                      (0, None) if ab != "Backdoor" else (2, "Worm")}}
        progs = [{"name": "Worm", "status": "active"}]
        bonus, labels = cpe.run_interface_check_hooks(
            "Cloak", 14, self._hs(programs=progs))
        self.assertEqual(bonus, 0)
        self.assertEqual(labels, [])

    def test_no_ability_returns_zero(self):
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_interface_check": lambda *a: (2, "Worm")}}
        progs = [{"name": "Worm", "status": "active"}]
        bonus, _ = cpe.run_interface_check_hooks("", 14, self._hs(programs=progs))
        self.assertEqual(bonus, 0)


class TestProgramDamageDiceSelect(_RegistryMixin, unittest.TestCase):
    """on_program_damage_dice_select fires only for the firing program."""

    def test_firing_program_hook_fires(self):
        cpe.PROGRAM_EFFECTS["Sword"] = {
            "category": "attacker", "is_hardware": False, "order": 50,
            "hooks": {"on_program_damage_dice_select":
                      lambda ice, p, hs:
                      (3, "Sword vs Black") if ice.get("category") == "black"
                      else (2, "Sword vs non-Black")}}
        progs = [{"name": "Sword", "status": "active"}]
        firing_prog = progs[0]
        dice, label = cpe.run_program_damage_dice_select_hooks(
            {"category": "black"}, firing_prog, self._hs(programs=progs))
        self.assertEqual(dice, 3)
        self.assertEqual(label, "Sword vs Black")

    def test_non_firing_programs_skipped(self):
        """If 'Worm' is also loaded, its hook (if any) doesn't fire on a
        Sword attack."""
        calls = []
        cpe.PROGRAM_EFFECTS["Sword"] = {
            "category": "attacker", "is_hardware": False, "order": 50,
            "hooks": {"on_program_damage_dice_select":
                      lambda ice, p, hs: (calls.append("Sword") or 3, "Sword")}}
        cpe.PROGRAM_EFFECTS["Worm"] = {
            "category": "booster", "is_hardware": False, "order": 50,
            "hooks": {"on_program_damage_dice_select":
                      lambda ice, p, hs: (calls.append("Worm") or 99, "Worm")}}
        progs = [{"name": "Sword", "status": "active"},
                 {"name": "Worm", "status": "active"}]
        firing_prog = progs[0]  # Sword is firing
        cpe.run_program_damage_dice_select_hooks(
            {}, firing_prog, self._hs(programs=progs))
        self.assertEqual(calls, ["Sword"])

    def test_no_hook_returns_none(self):
        dice, label = cpe.run_program_damage_dice_select_hooks(
            {}, {"name": "Sword", "status": "active"}, self._hs())
        self.assertIsNone(dice)
        self.assertIsNone(label)


class TestUnusedHookSkipped(_RegistryMixin, unittest.TestCase):
    """An entry without a particular hook is skipped for that pipeline."""

    def test_entry_with_only_brain_damage_hook_skipped_for_interface(self):
        cpe.PROGRAM_EFFECTS["Shield"] = {
            "category": "defender", "is_hardware": False, "order": 10,
            "hooks": {"on_brain_damage_inbound": lambda *a: (0, [])}}
        progs = [{"name": "Shield", "status": "active"}]
        bonus, labels = cpe.run_interface_check_hooks(
            "Backdoor", 14, self._hs(programs=progs))
        self.assertEqual(bonus, 0)
        self.assertEqual(labels, [])


if __name__ == "__main__":
    unittest.main()
