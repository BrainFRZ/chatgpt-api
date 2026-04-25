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


class TestStep2HardwareEntries(unittest.TestCase):
    """Step 2: the 3 existing hardware effects are now registry-driven.

    These tests use the production registry (no _RegistryMixin override)
    so they verify the live entries.
    """

    def test_insulated_wiring_blocks_body_fire(self):
        hs = {"installed_hardware": ["Insulated Wiring"], "active_programs": []}
        blocked, ops, label, _ = cpe.run_ice_effect_inbound_hooks(
            "body_fire", {"name": "Asp"}, hs, None)
        self.assertTrue(blocked)
        self.assertEqual(label, "Insulated Wiring")
        self.assertEqual(ops, [])

    def test_insulated_wiring_does_not_block_other_effects(self):
        hs = {"installed_hardware": ["Insulated Wiring"], "active_programs": []}
        for effect in ("forced_jack_out", "movement_lock", "stat_debuff",
                       "slide_penalty", "net_action_penalty"):
            blocked, _, _, _ = cpe.run_ice_effect_inbound_hooks(
                effect, {"name": "X"}, hs, None)
            self.assertFalse(blocked, f"Insulated Wiring wrongly blocked {effect!r}")

    def test_krash_barrier_blocks_forced_jack_out(self):
        hs = {"installed_hardware": ["KRASH Barrier"], "active_programs": []}
        blocked, ops, label, _ = cpe.run_ice_effect_inbound_hooks(
            "forced_jack_out", {"name": "Giant"}, hs, None)
        self.assertTrue(blocked)
        self.assertEqual(label, "KRASH Barrier")

    def test_krash_barrier_does_not_block_body_fire(self):
        hs = {"installed_hardware": ["KRASH Barrier"], "active_programs": []}
        blocked, _, _, _ = cpe.run_ice_effect_inbound_hooks(
            "body_fire", {"name": "Asp"}, hs, None)
        self.assertFalse(blocked)

    def test_no_hardware_no_block(self):
        hs = {"installed_hardware": [], "active_programs": []}
        for effect in ("body_fire", "forced_jack_out"):
            blocked, _, _, _ = cpe.run_ice_effect_inbound_hooks(
                effect, {"name": "X"}, hs, None)
            self.assertFalse(blocked, f"Bare deck wrongly blocked {effect!r}")

    def test_backup_drive_intercepts_destroyed_to_deactivated(self):
        hs = {"installed_hardware": ["Backup Drive"], "active_programs": []}
        new_status, ops, trace = cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed", hs, {})
        self.assertEqual(new_status, "deactivated")  # saved
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["before"], "destroyed")
        self.assertEqual(trace[0]["after"], "deactivated")

    def test_backup_drive_does_not_intercept_other_transitions(self):
        hs = {"installed_hardware": ["Backup Drive"], "active_programs": []}
        # active → derezzed: not destroyed, no intercept
        new_status, _, _ = cpe.run_program_status_change_hooks(
            "Shield", "active", "derezzed", hs, {})
        self.assertEqual(new_status, "derezzed")
        # active → deactivated: not destroyed
        new_status, _, _ = cpe.run_program_status_change_hooks(
            "Sword", "active", "deactivated", hs, {})
        self.assertEqual(new_status, "deactivated")
        # destroyed → destroyed (already destroyed, no double-save)
        new_status, _, _ = cpe.run_program_status_change_hooks(
            "Sword", "destroyed", "destroyed", hs, {})
        self.assertEqual(new_status, "destroyed")

    def test_no_backup_drive_destroyed_stays_destroyed(self):
        hs = {"installed_hardware": [], "active_programs": []}
        new_status, _, _ = cpe.run_program_status_change_hooks(
            "Sword", "active", "destroyed", hs, {})
        self.assertEqual(new_status, "destroyed")

    def test_backup_drive_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("Backup Drive")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["category"], "hardware")
        self.assertTrue(entry["is_hardware"])
        self.assertIn("on_program_status_change", entry["hooks"])

    def test_insulated_wiring_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("Insulated Wiring")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["is_hardware"])
        self.assertIn("on_ice_effect_inbound", entry["hooks"])

    def test_krash_barrier_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("KRASH Barrier")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["is_hardware"])
        self.assertIn("on_ice_effect_inbound", entry["hooks"])


class TestApplyProgramStatusChangeHelper(unittest.TestCase):
    """apply_program_status_change is the single-source-of-truth status
    transition helper. Routes through hooks, then maintains
    active_programs / destroyed_programs / REZ invariants."""

    def test_destroyed_with_backup_drive_saves_program(self):
        from game_systems.cpred_hack import apply_program_status_change
        state = {
            "active_programs": [{"name": "Sword", "status": "active",
                                 "rez": 0, "category": "attacker"}],
            "installed_hardware": ["Backup Drive"],
            "destroyed_programs": [],
        }
        final, _, _ = apply_program_status_change(state, "Sword", "active", "destroyed")
        self.assertEqual(final, "deactivated")
        # Saved: status flipped to deactivated
        self.assertEqual(state["active_programs"][0]["status"], "deactivated")
        # Not added to destroyed_programs (Backup Drive saved it before write)
        self.assertEqual(state["destroyed_programs"], [])

    def test_destroyed_without_backup_drive_persists(self):
        from game_systems.cpred_hack import apply_program_status_change
        state = {
            "active_programs": [{"name": "Sword", "status": "active",
                                 "rez": 0, "category": "attacker"}],
            "installed_hardware": [],
            "destroyed_programs": [],
        }
        final, _, _ = apply_program_status_change(state, "Sword", "active", "destroyed")
        self.assertEqual(final, "destroyed")
        self.assertEqual(state["active_programs"][0]["status"], "destroyed")
        self.assertIn("Sword", state["destroyed_programs"])

    def test_recovery_from_destroyed_clears_destroyed_programs(self):
        from game_systems.cpred_hack import apply_program_status_change
        state = {
            "active_programs": [{"name": "Sword", "status": "destroyed",
                                 "rez": 0, "category": "attacker"}],
            "installed_hardware": [],
            "destroyed_programs": ["Sword"],
        }
        # Reinstall: destroyed → deactivated
        final, _, _ = apply_program_status_change(
            state, "Sword", "destroyed", "deactivated")
        self.assertEqual(final, "deactivated")
        self.assertEqual(state["destroyed_programs"], [])  # cleared

    def test_recovery_from_derezzed_restores_rez(self):
        from game_systems.cpred_hack import apply_program_status_change
        from game_systems.cpred_tables import PROGRAM_STATS
        shield_rez = PROGRAM_STATS["Shield"]["rez"]
        state = {
            "active_programs": [{"name": "Shield", "status": "derezzed",
                                 "rez": 0, "category": "defender"}],
            "installed_hardware": [],
        }
        apply_program_status_change(state, "Shield", "derezzed", "active")
        self.assertEqual(state["active_programs"][0]["rez"], shield_rez)


class TestStep2EndToEndDestructionPath(unittest.TestCase):
    """Verify: anti-program ICE destruction with Backup Drive saves the
    program at the moment of destruction (not at writeback)."""

    def test_program_rez_damage_to_zero_with_backup_drive_saves(self):
        from game_systems.cpred_hack import _apply_single_ice_op
        state = {
            "active_programs": [{"name": "Sword", "status": "active",
                                 "rez": 1, "category": "attacker"}],
            "installed_hardware": ["Backup Drive"],
            "destroyed_programs": [],
        }
        # Anti-program ICE deals 1 REZ damage → REZ to 0 → would be destroyed
        op = {"op": "program_rez_damage", "program_name": "Sword",
              "damage": 1, "destroyed": True}
        _apply_single_ice_op(state, op, "program_rez_damage")
        # Backup Drive intercepts destruction at status-change time
        self.assertEqual(state["active_programs"][0]["status"], "deactivated")
        self.assertEqual(state["destroyed_programs"], [])

    def test_program_destroy_op_with_backup_drive_saves(self):
        from game_systems.cpred_hack import _apply_single_ice_op
        state = {
            "active_programs": [{"name": "Sword", "status": "active",
                                 "rez": 0, "category": "attacker"}],
            "installed_hardware": ["Backup Drive"],
            "destroyed_programs": [],
        }
        op = {"op": "program_destroy", "program_name": "Sword"}
        _apply_single_ice_op(state, op, "program_destroy")
        self.assertEqual(state["active_programs"][0]["status"], "deactivated")
        self.assertEqual(state["destroyed_programs"], [])

    def test_program_destroy_op_without_backup_drive_persists(self):
        from game_systems.cpred_hack import _apply_single_ice_op
        state = {
            "active_programs": [{"name": "Sword", "status": "active",
                                 "rez": 0, "category": "attacker"}],
            "installed_hardware": [],
            "destroyed_programs": [],
        }
        op = {"op": "program_destroy", "program_name": "Sword"}
        _apply_single_ice_op(state, op, "program_destroy")
        self.assertEqual(state["active_programs"][0]["status"], "destroyed")
        self.assertIn("Sword", state["destroyed_programs"])

    def test_program_derez_op_routes_through_helper(self):
        from game_systems.cpred_hack import _apply_single_ice_op
        state = {
            "active_programs": [{"name": "Shield", "status": "active",
                                 "rez": 7, "category": "defender"}],
            "installed_hardware": [],
        }
        op = {"op": "program_derez", "program_name": "Shield"}
        _apply_single_ice_op(state, op, "program_derez")
        self.assertEqual(state["active_programs"][0]["status"], "derezzed")


class TestStep3DefenderEntries(unittest.TestCase):
    """Step 3: Shield, Armor, Fortify Defender programs.

    Uses the production registry. The order=10/20/30 layering is the key
    correctness property — Shield consumes itself first, Armor's flat -4
    runs second, Fortify's conditional -4 runs last.
    """

    def _hs(self, programs, hardware=None, active_boosts=None):
        return {
            "active_programs": programs,
            "installed_hardware": hardware or [],
            "active_boosts": active_boosts or {},
        }

    def _active(self, name):
        return {"name": name, "status": "active", "rez": 7, "category": "defender"}

    # ----- Shield in isolation -----

    def test_shield_zeros_damage_and_emits_derez(self):
        hs = self._hs([self._active("Shield")])
        final, ops, trace = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 0)
        derez_ops = [o for o in ops if o.get("op") == "program_derez"]
        self.assertEqual(len(derez_ops), 1)
        self.assertEqual(derez_ops[0]["program_name"], "Shield")
        self.assertEqual(trace[0]["before"], 8)
        self.assertEqual(trace[0]["after"], 0)

    def test_shield_does_not_emit_derez_on_zero_input(self):
        """Shield is consumed only when it actually blocks damage."""
        hs = self._hs([self._active("Shield")])
        final, ops, _ = cpe.run_brain_damage_hooks(0, hs, {})
        self.assertEqual(final, 0)
        self.assertEqual(ops, [])  # no derez

    def test_shield_skipped_when_derezzed(self):
        progs = [{"name": "Shield", "status": "derezzed", "rez": 0, "category": "defender"}]
        final, ops, _ = cpe.run_brain_damage_hooks(8, self._hs(progs), {})
        self.assertEqual(final, 8)  # passes through unmitigated
        self.assertEqual(ops, [])

    # ----- Armor in isolation -----

    def test_armor_subtracts_four(self):
        hs = self._hs([self._active("Armor")])
        final, _, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 4)

    def test_armor_floor_at_zero(self):
        hs = self._hs([self._active("Armor")])
        final, _, _ = cpe.run_brain_damage_hooks(2, hs, {})
        self.assertEqual(final, 0)

    def test_armor_persistent_no_derez(self):
        """Armor doesn't consume itself — RAW: 'Reduces all incoming brain damage by 4.'"""
        hs = self._hs([self._active("Armor")])
        final1, ops1, _ = cpe.run_brain_damage_hooks(8, hs, {})
        final2, ops2, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final1, 4)
        self.assertEqual(final2, 4)
        self.assertEqual(ops1, [])
        self.assertEqual(ops2, [])

    # ----- Fortify in isolation -----

    def test_fortify_pending_subtracts_four(self):
        hs = self._hs([self._active("Fortify")],
                      active_boosts={"fortify_pending": True})
        final, _, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 4)

    def test_fortify_inactive_does_nothing(self):
        hs = self._hs([self._active("Fortify")],
                      active_boosts={})  # not pending
        final, _, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 8)

    def test_fortify_on_turn_end_emits_clear_when_pending(self):
        hs = self._hs([self._active("Fortify")],
                      active_boosts={"fortify_pending": True})
        ops, trace = cpe.run_turn_end_hooks(hs, {})
        clear_ops = [o for o in ops if o.get("op") == "active_boost_clear"]
        self.assertEqual(len(clear_ops), 1)
        self.assertEqual(clear_ops[0]["boost"], "fortify_pending")

    def test_fortify_on_turn_end_no_op_when_not_pending(self):
        hs = self._hs([self._active("Fortify")], active_boosts={})
        ops, _ = cpe.run_turn_end_hooks(hs, {})
        self.assertEqual(ops, [])

    # ----- Verification scenario from the plan -----

    def test_full_defender_stack_blocks_eight_damage(self):
        """Plan's verification: 8-damage hit on a Netrunner with
        Shield+Armor+Fortify all rezzed → damage=0, Shield derezzed,
        Armor and Fortify untouched."""
        hs = self._hs([
            self._active("Shield"),
            self._active("Armor"),
            self._active("Fortify"),
        ], active_boosts={"fortify_pending": True})
        final, ops, trace = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 0)
        # Only Shield should have fired (early exit at 0 stops Armor/Fortify)
        progs_in_trace = [t["prog"] for t in trace]
        self.assertEqual(progs_in_trace, ["Shield"])
        # Exactly one derez op for Shield
        derez_ops = [o for o in ops if o.get("op") == "program_derez"]
        self.assertEqual(len(derez_ops), 1)
        self.assertEqual(derez_ops[0]["program_name"], "Shield")

    def test_armor_plus_fortify_stack(self):
        """Without Shield: Armor (-4) + Fortify (-4) reduces 10 → 2."""
        hs = self._hs([
            self._active("Armor"),
            self._active("Fortify"),
        ], active_boosts={"fortify_pending": True})
        final, _, trace = cpe.run_brain_damage_hooks(10, hs, {})
        self.assertEqual(final, 2)
        self.assertEqual([t["prog"] for t in trace], ["Armor", "Fortify"])

    def test_shield_derezzed_armor_still_runs(self):
        """If Shield is already derezzed, Armor's -4 should still apply."""
        progs = [
            {"name": "Shield", "status": "derezzed", "rez": 0, "category": "defender"},
            self._active("Armor"),
        ]
        hs = self._hs(progs)
        final, ops, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final, 4)  # Armor reduced 8 → 4
        # Shield was inactive, no derez op emitted from it
        derez_ops = [o for o in ops if o.get("op") == "program_derez"]
        self.assertEqual(derez_ops, [])

    def test_multi_hit_sequence_shield_then_armor(self):
        """First hit: Shield blocks all 8, derezzes itself.
        Second hit: Armor reduces by 4 (Shield gone)."""
        progs = [self._active("Shield"), self._active("Armor")]
        hs = self._hs(progs)
        # First hit
        final1, ops1, _ = cpe.run_brain_damage_hooks(8, hs, {})
        self.assertEqual(final1, 0)
        # Simulate Shield derez — flip status (writeback would do this)
        for p in progs:
            if p["name"] == "Shield":
                p["status"] = "derezzed"
        # Second hit
        final2, ops2, _ = cpe.run_brain_damage_hooks(6, hs, {})
        self.assertEqual(final2, 2)  # Armor: 6 - 4

    # ----- Registry metadata sanity -----

    def test_shield_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("Shield")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["category"], "defender")
        self.assertFalse(entry["is_hardware"])
        self.assertEqual(entry["order"], 10)

    def test_armor_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("Armor")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["order"], 20)

    def test_fortify_registered_with_correct_metadata(self):
        entry = cpe.PROGRAM_EFFECTS.get("Fortify")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["order"], 30)
        self.assertIn("on_brain_damage_inbound", entry["hooks"])
        self.assertIn("on_turn_end", entry["hooks"])


class TestStep3ProgramAttackVsNetrunnerIntegration(unittest.TestCase):
    """Step 3 integration: program_attack_vs_netrunner now routes brain
    damage through Defender hooks before emitting the brain_damage state op."""

    def test_attack_with_shield_zeros_damage_and_appends_derez(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Shield", "status": "active", "rez": 7, "category": "defender"}]
        ice_status = {"Lobby_Hellhound": {
            "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
            "rez_current": 15, "status": "active",
        }}
        # All d10/d6 rolls = 5 → atk=5+ATK > def=5+DEF → hit, damage=1d6=5
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "program_attack_vs_netrunner",
                  "character": "Hellhound", "ice_type": "Hellhound",
                  "interface_rank": 4, "target_def": 4, "target": "V"}],
                active_programs=progs,
                ice_status=ice_status,
            )
        # No brain_damage op emitted (Shield zeroed it)
        bd_ops = [op for op in result["state_ops"]
                  if isinstance(op, dict) and op.get("op") == "brain_damage"]
        self.assertEqual(bd_ops, [])
        # Shield derez op emitted
        derez_ops = [op for op in result["state_ops"]
                     if isinstance(op, dict) and op.get("op") == "program_derez"
                     and op.get("program_name") == "Shield"]
        self.assertEqual(len(derez_ops), 1)
        # Result decorated with damage_resolution trace
        r = result["results"][0]
        self.assertIn("damage_resolution", r)
        self.assertEqual(r["damage_after_defenders"], 0)

    def test_attack_with_armor_reduces_damage_by_four(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Armor", "status": "active", "rez": 7, "category": "defender"}]
        ice_status = {"Lobby_Hellhound": {
            "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
            "rez_current": 15, "status": "active",
        }}
        # All rolls = 5 → atk d10=5+6 vs def d10=5+4 → hit; damage 2d6=5+5=10
        # Armor reduces 10 → 6
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "program_attack_vs_netrunner",
                  "character": "Hellhound", "ice_type": "Hellhound",
                  "interface_rank": 4, "target_def": 4, "target": "V"}],
                active_programs=progs,
                ice_status=ice_status,
            )
        bd_ops = [op for op in result["state_ops"]
                  if isinstance(op, dict) and op.get("op") == "brain_damage"]
        self.assertEqual(len(bd_ops), 1)
        self.assertEqual(bd_ops[0]["change"], 6)  # 10 - 4

    def test_attack_without_defenders_unchanged(self):
        from game_systems.cpred_mechanics import resolve_actions
        ice_status = {"Lobby_Hellhound": {
            "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
            "rez_current": 15, "status": "active",
        }}
        # All rolls = 5 → damage 2d6 = 10
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "program_attack_vs_netrunner",
                  "character": "Hellhound", "ice_type": "Hellhound",
                  "interface_rank": 4, "target_def": 4, "target": "V"}],
                active_programs=[],
                ice_status=ice_status,
            )
        bd_ops = [op for op in result["state_ops"]
                  if isinstance(op, dict) and op.get("op") == "brain_damage"]
        self.assertEqual(len(bd_ops), 1)
        self.assertEqual(bd_ops[0]["change"], 10)  # unmitigated 2d6=5+5


class TestStep3FortifyClearWriteback(unittest.TestCase):
    """active_boost_clear op emitted by Fortify's on_turn_end is consumed by
    _apply_resolver_net_ops."""

    def test_clear_op_pops_boost_key(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops
        state = {"active_boosts": {"fortify_pending": True}}
        _apply_resolver_net_ops(
            state,
            [{"op": "active_boost_clear", "boost": "fortify_pending"}],
        )
        self.assertEqual(state["active_boosts"], {})

    def test_clear_op_unknown_key_is_safe_noop(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops
        state = {"active_boosts": {}}
        _apply_resolver_net_ops(
            state,
            [{"op": "active_boost_clear", "boost": "nonexistent"}],
        )
        self.assertEqual(state["active_boosts"], {})


class TestStep4BoosterEntries(unittest.TestCase):
    """Step 4: Worm/Eraser/See Ya/Speedy Gonzalvez Booster programs.

    Each fires +2 on a specific Interface Ability via on_interface_check.
    Uses the production registry.
    """

    def _hs(self, programs):
        return {
            "active_programs": programs,
            "installed_hardware": [],
            "active_boosts": {},
        }

    def _active(self, name):
        return {"name": name, "status": "active", "rez": 7, "category": "booster"}

    # ----- Per-booster correctness -----

    def test_worm_fires_on_backdoor(self):
        hs = self._hs([self._active("Worm")])
        bonus, labels = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("Worm", 2)])

    def test_eraser_fires_on_cloak(self):
        hs = self._hs([self._active("Eraser")])
        bonus, labels = cpe.run_interface_check_hooks("Cloak", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("Eraser", 2)])

    def test_see_ya_fires_on_pathfinder(self):
        hs = self._hs([self._active("See Ya")])
        bonus, labels = cpe.run_interface_check_hooks("Pathfinder", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("See Ya", 2)])

    def test_speedy_fires_on_initiative(self):
        hs = self._hs([self._active("Speedy Gonzalvez")])
        bonus, labels = cpe.run_interface_check_hooks("Initiative", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("Speedy", 2)])

    # ----- Wrong-ability silence -----

    def test_worm_silent_on_other_abilities(self):
        hs = self._hs([self._active("Worm")])
        for ability in ("Cloak", "Control", "Eye-Dee", "Pathfinder",
                        "Slide", "Virus", "Zap", "Initiative"):
            bonus, _ = cpe.run_interface_check_hooks(ability, 14, hs)
            self.assertEqual(bonus, 0, f"Worm wrongly fired on {ability!r}")

    def test_eraser_silent_on_backdoor(self):
        hs = self._hs([self._active("Eraser")])
        bonus, _ = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(bonus, 0)

    # ----- Status filter -----

    def test_booster_silent_when_derezzed(self):
        progs = [{"name": "Worm", "status": "derezzed", "rez": 0, "category": "booster"}]
        bonus, _ = cpe.run_interface_check_hooks("Backdoor", 14, self._hs(progs))
        self.assertEqual(bonus, 0)

    def test_booster_silent_when_deactivated(self):
        progs = [{"name": "Worm", "status": "deactivated", "rez": 7, "category": "booster"}]
        bonus, _ = cpe.run_interface_check_hooks("Backdoor", 14, self._hs(progs))
        self.assertEqual(bonus, 0)

    def test_booster_silent_when_destroyed(self):
        progs = [{"name": "Worm", "status": "destroyed", "rez": 0, "category": "booster"}]
        bonus, _ = cpe.run_interface_check_hooks("Backdoor", 14, self._hs(progs))
        self.assertEqual(bonus, 0)

    # ----- Stacking -----

    def test_two_boosters_with_different_abilities_isolated(self):
        """Worm + Eraser loaded; only the matching one fires per ability."""
        hs = self._hs([self._active("Worm"), self._active("Eraser")])
        # Backdoor: Worm fires
        bonus, labels = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("Worm", 2)])
        # Cloak: Eraser fires
        bonus, labels = cpe.run_interface_check_hooks("Cloak", 14, hs)
        self.assertEqual(bonus, 2)
        self.assertEqual(labels, [("Eraser", 2)])

    # ----- Registry metadata sanity -----

    def test_all_four_boosters_registered(self):
        for name in ("Worm", "Eraser", "See Ya", "Speedy Gonzalvez"):
            entry = cpe.PROGRAM_EFFECTS.get(name)
            self.assertIsNotNone(entry, f"{name} not registered")
            self.assertEqual(entry["category"], "booster")
            self.assertFalse(entry["is_hardware"])
            self.assertEqual(entry["order"], 50)
            self.assertIn("on_interface_check", entry["hooks"])


class TestStep4SkillCheckIntegration(unittest.TestCase):
    """Step 4 integration: NET-context skill_check pulls booster bonuses
    from active_programs and folds them into the formatted roll."""

    def test_worm_fires_via_resolve_actions(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "active", "rez": 7, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_actions(
                [{"type": "skill_check", "character": "RedVelvet",
                  "stat_value": 4, "skill_value": 0, "dv": 10,
                  "net": True, "ability": "Backdoor"}],
                active_programs=progs,
            )
        r = result["results"][0]
        # d10=4 + Interface 4 + Worm 2 = 10 vs DV 10 → ✗ (ties favor defender)
        self.assertEqual(r["total"], 10)
        self.assertEqual(r["dv"], 10)
        self.assertFalse(r["success"])
        self.assertEqual(r["booster_bonuses"], [("Worm", 2)])
        self.assertIn("+Worm 2", r["formatted"])

    def test_no_booster_when_program_derezzed(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "derezzed", "rez": 0, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "RedVelvet",
                  "stat_value": 4, "skill_value": 0, "dv": 10,
                  "net": True, "ability": "Backdoor"}],
                active_programs=progs,
            )
        r = result["results"][0]
        # d10=5 + Interface 4 = 9 (no Worm) vs DV 10 → ✗
        self.assertEqual(r["total"], 9)
        self.assertNotIn("booster_bonuses", r)
        self.assertNotIn("Worm", r["formatted"])

    def test_wrong_ability_no_bonus(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "active", "rez": 7, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "RedVelvet",
                  "stat_value": 4, "skill_value": 0, "dv": 10,
                  "net": True, "ability": "Cloak"}],  # not Backdoor
                active_programs=progs,
            )
        r = result["results"][0]
        # d10=5 + Interface 4 = 9 (Worm doesn't fire on Cloak)
        self.assertEqual(r["total"], 9)
        self.assertNotIn("booster_bonuses", r)

    def test_non_net_skill_check_unaffected(self):
        """Meatspace skill checks ignore boosters even with same name."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "active", "rez": 7, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "skill_check", "character": "V",
                  "stat_value": 6, "skill_value": 4, "dv": 13}],  # no net flag
                active_programs=progs,
            )
        r = result["results"][0]
        # 5 + 6 + 4 = 15 (no booster fires)
        self.assertEqual(r["total"], 15)
        self.assertNotIn("booster_bonuses", r)

    def test_speedy_on_initiative_via_skill_check(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Speedy Gonzalvez", "status": "active", "rez": 7,
                  "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=6):
            result = resolve_actions(
                [{"type": "skill_check", "character": "RedVelvet",
                  "stat_value": 7, "skill_value": 0, "dv": 13,
                  "net": True, "ability": "Initiative"}],
                active_programs=progs,
            )
        r = result["results"][0]
        # 6 + 7 + 2 (Speedy) = 15
        self.assertEqual(r["total"], 15)
        self.assertEqual(r["booster_bonuses"], [("Speedy", 2)])


class TestStep4OpposedCheckIntegration(unittest.TestCase):
    """NET opposed_check (Zap/Slide) also gets booster bonus on attacker side."""

    def test_zap_with_no_matching_booster(self):
        """Zap is its own enum value; no current Booster targets Zap."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "active", "rez": 7, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "opposed_check", "character": "RedVelvet",
                  "attacker_stat": 6, "defender_stat": 4,
                  "net": True, "ability": "Zap", "zap": True}],
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertNotIn("booster_bonuses", r)

    def test_slide_does_not_fire_worm(self):
        """Worm targets Backdoor only — Slide opposed check gets no Worm bonus."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Worm", "status": "active", "rez": 7, "category": "booster"}]
        with patch("game_systems.cpred_mechanics.random.randint", return_value=5):
            result = resolve_actions(
                [{"type": "opposed_check", "character": "RedVelvet",
                  "attacker_stat": 6, "defender_stat": 4,
                  "net": True, "ability": "Slide"}],
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertNotIn("booster_bonuses", r)


class TestStep5AttackerEntries(unittest.TestCase):
    """Step 5: Sword/Banhammer/Hellbolt/Vrizzbolt Attacker programs.

    Attacker hooks fire only for the firing program (per RAW: Hellbolt's
    fire kicker only triggers when Hellbolt itself attacks, not on every
    attacker's hit).
    """

    def _hs(self, programs, hardware=None):
        return {
            "active_programs": programs,
            "installed_hardware": hardware or [],
            "active_boosts": {},
        }

    def _attacker(self, name):
        return {"name": name, "status": "active", "rez": 0, "category": "attacker"}

    # ----- Auto-Deactivate (resolver-driven, NOT in hooks) -----
    # The auto-Deactivate fires hit-or-miss per RAW so it lives in the
    # resolver, not in on_program_attack_hit. Hook here only emits
    # per-attacker on-hit kickers.

    def test_sword_has_no_attack_hit_hook(self):
        """Sword's on-hit behavior is just 'plain damage' — no kicker hook."""
        progs = [self._attacker("Sword")]
        ops, trace = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "Dragon"},
            self._hs(progs),
            firing_program_name="Sword",
        )
        self.assertEqual(ops, [])
        self.assertEqual(trace, [])

    def test_banhammer_has_no_attack_hit_hook(self):
        progs = [self._attacker("Banhammer")]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True}, self._hs(progs), firing_program_name="Banhammer")
        self.assertEqual(ops, [])

    def test_hooks_filter_to_firing_program_only(self):
        """If Sword AND Hellbolt are both rezzed, only the FIRING program's
        kicker fires (Hellbolt's deck-fire doesn't trigger when Sword fires)."""
        progs = [self._attacker("Sword"), self._attacker("Hellbolt")]
        ops, trace = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "Dragon"},
            self._hs(progs),
            firing_program_name="Sword",
        )
        # Sword has no kicker; Hellbolt's body_fire shouldn't fire either
        body_fire_ops = [o for o in ops if o.get("op") == "body_fire"]
        self.assertEqual(body_fire_ops, [])

    # ----- Hellbolt kicker -----

    def test_hellbolt_emits_body_fire(self):
        progs = [self._attacker("Hellbolt")]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "Vincent"},
            self._hs(progs),
            firing_program_name="Hellbolt",
        )
        body_fire_ops = [o for o in ops if o.get("op") == "body_fire"]
        self.assertEqual(len(body_fire_ops), 1)
        self.assertEqual(body_fire_ops[0]["target"], "Vincent")
        self.assertEqual(body_fire_ops[0]["damage_per_turn"], 2)

    # ----- Vrizzbolt kicker -----

    def test_vrizzbolt_emits_net_action_penalty(self):
        progs = [self._attacker("Vrizzbolt")]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "BlackHat"},
            self._hs(progs),
            firing_program_name="Vrizzbolt",
        )
        nap_ops = [o for o in ops if o.get("op") == "net_action_penalty"]
        self.assertEqual(len(nap_ops), 1)
        self.assertEqual(nap_ops[0]["penalty"], 1)
        self.assertEqual(nap_ops[0]["target"], "BlackHat")

    # ----- Sword/Banhammer damage scaling -----

    def test_sword_3d6_vs_black_ice(self):
        progs = [self._attacker("Sword")]
        firing_prog = progs[0]
        # Hellhound is anti_personnel = Black ICE
        ice_block = {"name": "Hellhound", "class": "anti_personnel"}
        dice, label = cpe.run_program_damage_dice_select_hooks(
            ice_block, firing_prog, self._hs(progs))
        self.assertEqual(dice, 3)
        self.assertEqual(label, "Sword vs Black")

    def test_sword_2d6_vs_non_black_ice(self):
        progs = [self._attacker("Sword")]
        firing_prog = progs[0]
        # File ICE / Patrol / etc. — not anti_personnel/anti_program
        ice_block = {"name": "Patrol", "class": "patrol"}
        dice, label = cpe.run_program_damage_dice_select_hooks(
            ice_block, firing_prog, self._hs(progs))
        self.assertEqual(dice, 2)
        self.assertEqual(label, "Sword vs non-Black")

    def test_sword_3d6_vs_anti_program_black_ice(self):
        progs = [self._attacker("Sword")]
        firing_prog = progs[0]
        ice_block = {"name": "Dragon", "class": "anti_program"}
        dice, label = cpe.run_program_damage_dice_select_hooks(
            ice_block, firing_prog, self._hs(progs))
        self.assertEqual(dice, 3)

    def test_banhammer_2d6_vs_black(self):
        progs = [self._attacker("Banhammer")]
        firing_prog = progs[0]
        ice_block = {"name": "Hellhound", "class": "anti_personnel"}
        dice, label = cpe.run_program_damage_dice_select_hooks(
            ice_block, firing_prog, self._hs(progs))
        self.assertEqual(dice, 2)
        self.assertEqual(label, "Banhammer vs Black")

    def test_banhammer_3d6_vs_non_black(self):
        progs = [self._attacker("Banhammer")]
        firing_prog = progs[0]
        ice_block = {"name": "Patrol", "class": "patrol"}
        dice, label = cpe.run_program_damage_dice_select_hooks(
            ice_block, firing_prog, self._hs(progs))
        self.assertEqual(dice, 3)
        self.assertEqual(label, "Banhammer vs non-Black")

    # ----- Defenders / Boosters do not auto-Deactivate -----

    def test_defender_does_not_auto_deactivate(self):
        """Shield/Armor/Fortify aren't Attackers — never get auto-Deactivate
        from on_program_attack_hit."""
        progs = [{"name": "Shield", "status": "active", "rez": 7,
                  "category": "defender"}]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True}, self._hs(progs), firing_program_name="Shield")
        # No hook on Shield, so no ops at all
        self.assertEqual(ops, [])

    def test_booster_does_not_auto_deactivate(self):
        progs = [{"name": "Worm", "status": "active", "rez": 7,
                  "category": "booster"}]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True}, self._hs(progs), firing_program_name="Worm")
        self.assertEqual(ops, [])

    # ----- Status filter on damage dice select -----

    def test_sword_dice_hook_skipped_when_derezzed(self):
        progs = [{"name": "Sword", "status": "derezzed", "rez": 0,
                  "category": "attacker"}]
        firing_prog = progs[0]
        dice, label = cpe.run_program_damage_dice_select_hooks(
            {"class": "anti_personnel"}, firing_prog, self._hs(progs))
        # Even though firing_prog matches by name, snapshot filters out
        # derezzed programs — hook entry filtered out.
        self.assertIsNone(dice)
        self.assertIsNone(label)

    # ----- Registry metadata sanity -----

    def test_all_four_attackers_registered(self):
        for name in ("Sword", "Banhammer", "Hellbolt", "Vrizzbolt"):
            entry = cpe.PROGRAM_EFFECTS.get(name)
            self.assertIsNotNone(entry, f"{name} not registered")
            self.assertEqual(entry["category"], "attacker")
            self.assertEqual(entry["order"], 100)

    def test_sword_banhammer_have_damage_dice_hook(self):
        for name in ("Sword", "Banhammer"):
            entry = cpe.PROGRAM_EFFECTS[name]
            self.assertIn("on_program_damage_dice_select", entry["hooks"])
            # Sword/Banhammer have no on_program_attack_hit — auto-Deactivate
            # is resolver-driven, no kicker beyond dice scaling.
            self.assertNotIn("on_program_attack_hit", entry["hooks"])

    def test_hellbolt_vrizzbolt_attack_hit_only(self):
        """Hellbolt and Vrizzbolt have on_program_attack_hit (kickers) but
        no damage dice scaling."""
        for name in ("Hellbolt", "Vrizzbolt"):
            entry = cpe.PROGRAM_EFFECTS[name]
            self.assertIn("on_program_attack_hit", entry["hooks"])
            self.assertNotIn("on_program_damage_dice_select", entry["hooks"])


class TestStep5ProgramAttackIntegration(unittest.TestCase):
    """End-to-end: program_attack via resolve_actions invokes both
    on_program_damage_dice_select and on_program_attack_hit."""

    def _ice_status(self, ice_type, rez_current=15):
        return {"Server Farm_X": {
            "name": ice_type.title(),
            "behavior": "black",
            "ice_type": ice_type,
            "rez_current": rez_current,
            "rez_max": 30,
            "status": "active",
        }}

    def test_sword_attack_on_black_ice_uses_3d6(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Sword", "status": "active", "rez": 0,
                  "category": "attacker"}]
        # atk d10=8, def d10=3 (no fumble), three damage d6=5,5,5
        # atk_total = 8+8+1 = 17 vs def_total = 3+6 = 9 → hit
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3, 5, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "RedVelvet",
                  "interface_rank": 8, "program": "Sword",
                  "target": "Dragon", "program_damage_dice": 99}],
                # Note: model passed 99 dice — backend should override to 3
                ice_status=self._ice_status("dragon"),
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertTrue(r.get("hit"))
        # Damage should be 3d6 (Sword vs Black) = 5+5+5 = 15
        self.assertEqual(r["damage_total"], 15)
        self.assertEqual(r.get("damage_dice_modifier"), "Sword vs Black")
        # Auto-Deactivate op emitted
        psc_ops = [op for op in result["state_ops"]
                   if isinstance(op, dict) and op.get("op") == "program_status_change"
                   and op.get("program_name") == "Sword"]
        self.assertEqual(len(psc_ops), 1)
        self.assertEqual(psc_ops[0]["new_status"], "deactivated")

    def test_hellbolt_attack_against_ice_skips_kicker(self):
        """Step 6d: PvP-only kickers (body_fire on target's deck) don't
        fire when target is ICE. The auto-Deactivate still fires."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Hellbolt", "status": "active", "rez": 0,
                  "category": "attacker"}]
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "RedVelvet",
                  "interface_rank": 8, "program": "Hellbolt",
                  "target": "Dragon", "program_damage_dice": 2}],
                ice_status=self._ice_status("dragon"),
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertTrue(r.get("hit"))
        # NO body_fire op — kicker skipped
        body_fire_ops = [op for op in result["state_ops"]
                         if isinstance(op, dict) and op.get("op") == "body_fire"]
        self.assertEqual(body_fire_ops, [])
        # Annotation surfaced for narration
        self.assertIn("pvp_kicker_skipped", r)
        self.assertIn("Hellbolt", r["pvp_kicker_skipped"])
        # Auto-Deactivate still fires
        psc_ops = [op for op in result["state_ops"]
                   if isinstance(op, dict)
                   and op.get("op") == "program_status_change"
                   and op.get("program_name") == "Hellbolt"]
        self.assertEqual(len(psc_ops), 1)

    def test_vrizzbolt_attack_against_ice_skips_kicker(self):
        """Step 6d: Vrizzbolt's net_action_penalty kicker doesn't self-fire
        when used against ICE."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Vrizzbolt", "status": "active", "rez": 0,
                  "category": "attacker"}]
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "RedVelvet",
                  "interface_rank": 8, "program": "Vrizzbolt",
                  "target": "Dragon", "program_damage_dice": 1}],
                ice_status=self._ice_status("dragon"),
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertTrue(r.get("hit"))
        nap_ops = [op for op in result["state_ops"]
                   if isinstance(op, dict)
                   and op.get("op") == "net_action_penalty"]
        self.assertEqual(nap_ops, [])
        self.assertIn("pvp_kicker_skipped", r)

    def test_attack_miss_still_fires_auto_deactivate_but_not_kickers(self):
        """RAW: programs Deactivate after USE regardless of hit/miss.
        Kickers (Hellbolt body_fire etc.) only fire on hit."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Hellbolt", "status": "active", "rez": 0,
                  "category": "attacker"}]
        # Force a miss: atk d10=1 (fumble: subtract another roll), def d10=10
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[1, 5, 10, 5, 5, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "RedVelvet",
                  "interface_rank": 0, "program": "Hellbolt",
                  "target": "Dragon", "program_damage_dice": 2}],
                ice_status=self._ice_status("dragon"),
                active_programs=progs,
            )
        r = result["results"][0]
        if not r.get("hit"):
            # Miss → auto-Deactivate still fires (resolver, hit-or-miss)
            psc_ops = [op for op in result["state_ops"]
                       if isinstance(op, dict) and op.get("op") == "program_status_change"]
            self.assertEqual(len(psc_ops), 1)
            # But Hellbolt's body_fire kicker does NOT fire on miss
            body_fire_ops = [op for op in result["state_ops"]
                             if isinstance(op, dict) and op.get("op") == "body_fire"]
            self.assertEqual(body_fire_ops, [])

    def test_no_legacy_program_deactivate_op_emitted(self):
        """Step 5 replaces the old `program_deactivate` op with the
        registry's `program_status_change` op. Verify the legacy op is gone."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Sword", "status": "active", "rez": 0,
                  "category": "attacker"}]
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3, 5, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "RedVelvet",
                  "interface_rank": 8, "program": "Sword",
                  "target": "Dragon", "program_damage_dice": 3}],
                ice_status=self._ice_status("dragon"),
                active_programs=progs,
            )
        legacy_ops = [op for op in result["state_ops"]
                      if isinstance(op, dict) and op.get("op") == "program_deactivate"]
        self.assertEqual(legacy_ops, [])


class TestStep6aPassiveBespokeEntries(unittest.TestCase):
    """Step 6a: Flak (defender), Hardened Circuitry (hardware), DNA Lock
    (hardware, not yet wired into runtime).
    """

    def _hs(self, programs=None, hardware=None):
        return {
            "active_programs": programs or [],
            "installed_hardware": hardware or [],
            "active_boosts": {},
        }

    # ----- Flak -----

    def test_flak_zeros_non_black_ice_atk(self):
        progs = [{"name": "Flak", "status": "active", "rez": 7, "category": "defender"}]
        hs = self._hs(programs=progs)
        # A non-Black ICE (e.g. Tar / Patrol class) attacks
        ice_block = {"name": "TarPit", "class": "tar"}
        final_atk, labels, trace = cpe.run_ice_attack_inbound_hooks(
            ice_block, 6, hs, {})
        self.assertEqual(final_atk, 0)
        self.assertEqual(len(labels), 1)
        self.assertIn("Flak", labels[0][0])

    def test_flak_does_not_affect_black_ice(self):
        progs = [{"name": "Flak", "status": "active", "rez": 7, "category": "defender"}]
        hs = self._hs(programs=progs)
        for cls in ("anti_personnel", "anti_program"):
            ice_block = {"name": "X", "class": cls}
            final_atk, labels, _ = cpe.run_ice_attack_inbound_hooks(
                ice_block, 6, hs, {})
            self.assertEqual(final_atk, 6, f"Flak wrongly modified Black ICE class={cls}")
            self.assertEqual(labels, [])

    def test_flak_silent_when_derezzed(self):
        progs = [{"name": "Flak", "status": "derezzed", "rez": 0, "category": "defender"}]
        hs = self._hs(programs=progs)
        final_atk, _, _ = cpe.run_ice_attack_inbound_hooks(
            {"name": "TarPit", "class": "tar"}, 6, hs, {})
        self.assertEqual(final_atk, 6)

    def test_flak_unknown_ice_class_treated_as_non_black(self):
        """An ICE block with no/unknown class defaults to non-Black —
        Flak fires (defensive default favors the Netrunner)."""
        progs = [{"name": "Flak", "status": "active", "rez": 7, "category": "defender"}]
        hs = self._hs(programs=progs)
        final_atk, _, _ = cpe.run_ice_attack_inbound_hooks(
            {"name": "Mystery"}, 6, hs, {})  # no class field
        self.assertEqual(final_atk, 0)

    # ----- Hardened Circuitry -----

    def test_hardened_circuitry_blocks_emp(self):
        hs = self._hs(hardware=["Hardened Circuitry"])
        blocked, _, label, _ = cpe.run_ice_effect_inbound_hooks(
            "emp", {"name": "EMPGremlin"}, hs, None)
        self.assertTrue(blocked)
        self.assertEqual(label, "Hardened Circuitry")

    def test_hardened_circuitry_does_not_block_other_effects(self):
        hs = self._hs(hardware=["Hardened Circuitry"])
        for effect in ("body_fire", "forced_jack_out", "movement_lock",
                       "stat_debuff", "slide_penalty"):
            blocked, _, _, _ = cpe.run_ice_effect_inbound_hooks(
                effect, {"name": "X"}, hs, None)
            self.assertFalse(blocked, f"Hardened Circuitry wrongly blocked {effect!r}")

    # ----- DNA Lock (signature only — not yet wired) -----

    def test_dna_lock_hook_allows_owner(self):
        from game_systems.cpred_program_effects import _dna_lock_on_jack_in
        # Owner jacks in: allowed, no DV
        allowed, dv, _ = _dna_lock_on_jack_in("V", "V", {}, {})
        self.assertTrue(allowed)
        self.assertIsNone(dv)

    def test_dna_lock_hook_blocks_non_owner_with_dv17(self):
        from game_systems.cpred_program_effects import _dna_lock_on_jack_in
        allowed, dv, label = _dna_lock_on_jack_in("V", "BlackHat", {}, {})
        self.assertFalse(allowed)
        self.assertEqual(dv, 17)
        self.assertIn("DNA Lock", label)

    def test_dna_lock_case_insensitive_owner_match(self):
        from game_systems.cpred_program_effects import _dna_lock_on_jack_in
        allowed, dv, _ = _dna_lock_on_jack_in("V", "v", {}, {})
        self.assertTrue(allowed)

    # ----- Registry metadata sanity -----

    def test_flak_registered(self):
        entry = cpe.PROGRAM_EFFECTS.get("Flak")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["category"], "defender")
        self.assertFalse(entry["is_hardware"])
        self.assertEqual(entry["order"], 25)
        self.assertIn("on_ice_attack_inbound", entry["hooks"])

    def test_hardened_circuitry_registered(self):
        entry = cpe.PROGRAM_EFFECTS.get("Hardened Circuitry")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["category"], "hardware")
        self.assertTrue(entry["is_hardware"])
        self.assertIn("on_ice_effect_inbound", entry["hooks"])

    def test_dna_lock_registered(self):
        entry = cpe.PROGRAM_EFFECTS.get("DNA Lock")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["is_hardware"])
        self.assertIn("on_jack_in", entry["hooks"])


class TestStep6aFlakIntegration(unittest.TestCase):
    """End-to-end: program_attack_vs_netrunner with Flak rezzed nullifies
    non-Black-ICE attacker ATK before the opposed roll."""

    def test_flak_rezzed_zeros_non_black_ice_atk_in_attack(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Flak", "status": "active", "rez": 7, "category": "defender"}]
        # Use a synthetic non-Black ICE via explicit ATK + def in action
        # (no non-Black ICE in ICE_STAT_BLOCKS to look up). Pass ice_type
        # not in the table — _lookup_ice_type returns None, _patk falls
        # back to action.program_atk. But Flak's hook needs an ice_block
        # to inspect; with None it treats as non-Black (defensive default).
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack_vs_netrunner",
                  "character": "TarICE",
                  "program_atk": 6, "program_damage_dice": 1,
                  "target_def": 4, "target": "V"}],
                active_programs=progs,
                ice_status={},
            )
        r = result["results"][0]
        # Flak should have applied — attacker ATK went 6 → 0
        self.assertIn("atk_modifiers", r)
        self.assertEqual(r["atk_modifiers"][0]["before"], 6)
        self.assertEqual(r["atk_modifiers"][0]["after"], 0)

    def test_flak_does_not_affect_black_ice_attack(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Flak", "status": "active", "rez": 7, "category": "defender"}]
        # Hellhound is anti_personnel = Black ICE
        with patch("game_systems.cpred_mechanics.random.randint",
                   return_value=5):
            result = resolve_actions(
                [{"type": "program_attack_vs_netrunner",
                  "character": "Hellhound", "ice_type": "Hellhound",
                  "interface_rank": 4, "target_def": 4, "target": "V"}],
                active_programs=progs,
                ice_status={"Lobby_Hellhound": {
                    "name": "Hellhound", "behavior": "black",
                    "ice_type": "hellhound", "rez_current": 15,
                    "status": "active",
                }},
            )
        r = result["results"][0]
        # Flak shouldn't have fired — no atk_modifiers
        self.assertNotIn("atk_modifiers", r)


class TestStep6bBoostedActionEntries(unittest.TestCase):
    """Step 6b: Surge / Mask / Spoof Signal Boosted-Action programs +
    DeckKRASH Attacker. Hook fires once on the next matching event then
    clears the pending flag (single-shot)."""

    def _hs(self, programs, hardware=None, active_boosts=None):
        return {
            "active_programs": programs,
            "installed_hardware": hardware or [],
            "active_boosts": active_boosts or {},
        }

    def _active(self, name, category="boosted_action"):
        return {"name": name, "status": "active", "rez": 7, "category": category}

    # ----- Surge -----

    def test_surge_fires_when_pending(self):
        hs = self._hs([self._active("Surge")],
                      active_boosts={"surge_pending": True})
        bonus, labels = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(bonus, 4)
        self.assertEqual(labels, [("Surge", 4)])
        # Flag cleared after firing
        self.assertNotIn("surge_pending", hs["active_boosts"])

    def test_surge_silent_when_not_pending(self):
        hs = self._hs([self._active("Surge")], active_boosts={})
        bonus, labels = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(bonus, 0)
        self.assertEqual(labels, [])

    def test_surge_one_shot_only(self):
        hs = self._hs([self._active("Surge")],
                      active_boosts={"surge_pending": True})
        # First check fires +4
        b1, _ = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(b1, 4)
        # Second check (without re-boosting): no bonus
        b2, _ = cpe.run_interface_check_hooks("Backdoor", 14, hs)
        self.assertEqual(b2, 0)

    def test_surge_fires_on_any_ability(self):
        """Surge isn't ability-specific — it boosts ANY Interface check."""
        hs = self._hs([self._active("Surge")],
                      active_boosts={"surge_pending": True})
        bonus, _ = cpe.run_interface_check_hooks("Cloak", 14, hs)
        self.assertEqual(bonus, 4)

    # ----- Mask -----

    def test_mask_zeros_alert_increase_when_pending(self):
        hs = self._hs([self._active("Mask")],
                      active_boosts={"mask_pending": True})
        final_delta, ops, _ = cpe.run_alert_increase_hooks(
            2, "failed_backdoor", hs)
        self.assertEqual(final_delta, 0)
        self.assertNotIn("mask_pending", hs["active_boosts"])
        # Annotation op for narration
        annot = [o for o in ops if o.get("op") == "alert_suppressed"]
        self.assertEqual(len(annot), 1)
        self.assertEqual(annot[0]["original_delta"], 2)

    def test_mask_silent_when_not_pending(self):
        hs = self._hs([self._active("Mask")], active_boosts={})
        final_delta, ops, _ = cpe.run_alert_increase_hooks(
            2, "failed_backdoor", hs)
        self.assertEqual(final_delta, 2)
        self.assertEqual(ops, [])

    # ----- Spoof Signal -----

    def test_spoof_signal_decrements_countdown(self):
        hs = self._hs([self._active("Spoof Signal")],
                      active_boosts={"spoof_signal_rounds_remaining": 2})
        ops, _ = cpe.run_turn_start_hooks(hs, {})
        self.assertEqual(hs["active_boosts"]["spoof_signal_rounds_remaining"], 1)
        tick_ops = [o for o in ops if o.get("op") == "spoof_signal_tick"]
        self.assertEqual(len(tick_ops), 1)

    def test_spoof_signal_clears_at_zero(self):
        hs = self._hs([self._active("Spoof Signal")],
                      active_boosts={"spoof_signal_rounds_remaining": 1})
        cpe.run_turn_start_hooks(hs, {})
        self.assertNotIn("spoof_signal_rounds_remaining", hs["active_boosts"])

    def test_spoof_signal_silent_when_inactive(self):
        hs = self._hs([self._active("Spoof Signal")], active_boosts={})
        ops, _ = cpe.run_turn_start_hooks(hs, {})
        self.assertEqual(ops, [])

    # ----- DeckKRASH -----

    def test_deckkrash_emits_initiate_unsafe_jack_out_on_hit(self):
        progs = [self._active("DeckKRASH", category="attacker")]
        hs = self._hs(progs)
        ops, trace = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "BlackHat"},
            hs, firing_program_name="DeckKRASH")
        unsafe_ops = [o for o in ops if o.get("op") == "initiate_unsafe_jack_out"]
        self.assertEqual(len(unsafe_ops), 1)
        self.assertEqual(unsafe_ops[0]["target"], "BlackHat")
        self.assertEqual(unsafe_ops[0]["actor"], "DeckKRASH")
        self.assertIn("deckkrash", unsafe_ops[0]["cause"])

    # ----- Registry metadata sanity -----

    def test_step6b_entries_registered(self):
        for name in ("Surge", "Mask", "Spoof Signal", "DeckKRASH"):
            entry = cpe.PROGRAM_EFFECTS.get(name)
            self.assertIsNotNone(entry, f"{name} not registered")

    def test_surge_metadata(self):
        e = cpe.PROGRAM_EFFECTS["Surge"]
        self.assertEqual(e["category"], "boosted_action")
        self.assertEqual(e["order"], 60)
        self.assertIn("on_interface_check", e["hooks"])

    def test_mask_metadata(self):
        e = cpe.PROGRAM_EFFECTS["Mask"]
        self.assertIn("on_alert_increase", e["hooks"])

    def test_spoof_signal_metadata(self):
        e = cpe.PROGRAM_EFFECTS["Spoof Signal"]
        self.assertIn("on_turn_start", e["hooks"])

    def test_deckkrash_metadata(self):
        e = cpe.PROGRAM_EFFECTS["DeckKRASH"]
        self.assertEqual(e["category"], "attacker")
        self.assertIn("on_program_attack_hit", e["hooks"])


class TestStep6bBoostedActionResolver(unittest.TestCase):
    """Boosted Action resolver: 1 NA + 1 Cycle atomic, sets active_boosts flag."""

    def _progs(self, name):
        return [{"name": name, "status": "active", "rez": 7,
                 "category": "boosted_action"}]

    def test_surge_boosted_happy_path(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "RedVelvet",
              "program": "Surge"}],
            active_programs=self._progs("Surge"),
            net_actions_remaining=3,
            cycles_remaining=2,
        )
        r = result["results"][0]
        self.assertTrue(r["success"])
        self.assertEqual(r["program"], "Surge")
        self.assertEqual(r["cost_net_actions"], 1)
        self.assertEqual(r["cost_cycles"], 1)
        # State ops emitted
        boost_ops = [op for op in result["state_ops"]
                     if op.get("op") == "active_boost_set"
                     and op.get("boost") == "surge_pending"]
        self.assertEqual(len(boost_ops), 1)
        cycle_ops = [op for op in result["state_ops"]
                     if op.get("op") == "cycle_consumed"]
        self.assertEqual(len(cycle_ops), 1)

    def test_boosted_action_fails_atomic_on_zero_cycles(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=self._progs("Surge"),
            net_actions_remaining=3,
            cycles_remaining=0,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "insufficient_cycles")
        # No state ops emitted (atomic fail)
        boost_ops = [op for op in result["state_ops"]
                     if op.get("op") in ("active_boost_set", "cycle_consumed")]
        self.assertEqual(boost_ops, [])

    def test_boosted_action_fails_atomic_on_zero_na(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=self._progs("Surge"),
            net_actions_remaining=0,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "insufficient_net_actions")

    def test_boosted_action_unknown_program(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Hellbolt"}],
            active_programs=self._progs("Hellbolt"),
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "unknown_boosted_action")

    def test_boosted_action_program_not_loaded(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=[],  # Surge not loaded
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "program_not_loaded")

    def test_spoof_signal_boosted_sets_duration(self):
        from game_systems.cpred_mechanics import resolve_actions
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V",
              "program": "Spoof Signal"}],
            active_programs=self._progs("Spoof Signal"),
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertTrue(r["success"])
        self.assertEqual(r["duration_field"], "spoof_signal_rounds_remaining")
        self.assertEqual(r["duration_value"], 2)
        boost_ops = [op for op in result["state_ops"]
                     if op.get("op") == "active_boost_set"]
        self.assertEqual(boost_ops[0]["duration_value"], 2)

    def test_running_cycle_debit_blocks_overspend_in_batch(self):
        """Two boosted actions in same batch with only 1 Cycle: first
        succeeds, second fails."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [
            {"name": "Surge", "status": "active", "rez": 7, "category": "boosted_action"},
            {"name": "Mask", "status": "active", "rez": 7, "category": "boosted_action"},
        ]
        result = resolve_actions(
            [
                {"type": "boosted_action", "character": "V", "program": "Surge"},
                {"type": "boosted_action", "character": "V", "program": "Mask"},
            ],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=1,
        )
        r0, r1 = result["results"]
        self.assertTrue(r0["success"])
        self.assertEqual(r1["error"], "insufficient_cycles")


class TestStep6bSurgeIntegration(unittest.TestCase):
    """End-to-end: model boosts Surge then makes a Backdoor check."""

    def test_boosted_surge_flows_into_skill_check(self):
        """Plan's verification scenario: Boosted Surge on a check rolls
        d10 + Interface + 4. Cycles -1, NET Actions -1."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        # Two-action batch: activate Surge, then perform a Backdoor check
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_actions(
                [
                    {"type": "boosted_action", "character": "RedVelvet",
                     "program": "Surge"},
                    {"type": "skill_check", "character": "RedVelvet",
                     "stat_value": 4, "skill_value": 0, "dv": 13,
                     "net": True, "ability": "Backdoor"},
                ],
                active_programs=progs,
                net_actions_remaining=3,
                cycles_remaining=2,
                # active_boosts shared dict so the boosted_action's state op
                # is observed by the skill_check via the proxy reference.
                active_boosts={},
            )
        # boosted_action emitted active_boost_set op
        boost_set = [op for op in result["state_ops"]
                     if op.get("op") == "active_boost_set"
                     and op.get("boost") == "surge_pending"]
        self.assertEqual(len(boost_set), 1)
        # Note: in this test the surge_pending flag isn't actually applied
        # to the proxy mid-batch (state ops only fire at writeback). So the
        # skill_check WON'T see surge_pending=True yet. Confirming current
        # behavior; integration with apply_hack_state writeback handles
        # cross-call propagation in production.
        r1 = result["results"][1]
        self.assertEqual(r1["type"], "skill_check")


class TestStep6bAlertHookWriteback(unittest.TestCase):
    """alert_level changes route through on_alert_increase hooks at
    apply_hack_state time so Mask can suppress."""

    def test_mask_suppresses_alert_increase_at_writeback(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["alert_level"] = 0
        hs["active_programs"] = [{"name": "Mask", "status": "active",
                                  "rez": 7, "category": "boosted_action"}]
        hs["active_boosts"] = {"mask_pending": True}
        # Model reports alert_level = 2 (would be a +2 increase)
        apply_hack_state(hs, {"hack_state": {"alert_level": 2}})
        # Mask should have zeroed the increase; alert stays at 0
        self.assertEqual(hs["alert_level"], 0)
        self.assertNotIn("mask_pending", hs["active_boosts"])

    def test_alert_increase_unmodified_without_mask(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["alert_level"] = 0
        apply_hack_state(hs, {"hack_state": {"alert_level": 3}})
        self.assertEqual(hs["alert_level"], 3)

    def test_alert_decrease_passes_through_unhooked(self):
        """on_alert_increase only fires on increases; decreases are
        applied directly."""
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["alert_level"] = 5
        apply_hack_state(hs, {"hack_state": {"alert_level": 2}})
        self.assertEqual(hs["alert_level"], 2)


class TestStep6cAttackerEntries(unittest.TestCase):
    """Step 6c: Nervescrub, Poison Flatline, Superglue, Overclock + the
    Superglue jack-out gate."""

    def _hs(self, programs, hardware=None, active_boosts=None):
        return {
            "active_programs": programs,
            "installed_hardware": hardware or [],
            "active_boosts": active_boosts or {},
        }

    def _attacker(self, name):
        return {"name": name, "status": "active", "rez": 0, "category": "attacker"}

    # ----- Nervescrub -----

    def test_nervescrub_emits_stat_debuff(self):
        progs = [self._attacker("Nervescrub")]
        with patch("game_systems.cpred_program_effects.random.randint",
                   return_value=4):
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True, "target_name": "BlackHat"},
                self._hs(progs), firing_program_name="Nervescrub")
        debuff_ops = [o for o in ops if o.get("op") == "stat_debuff"]
        self.assertEqual(len(debuff_ops), 1)
        op = debuff_ops[0]
        self.assertEqual(set(op["stats"]), {"INT", "REF", "DEX"})
        self.assertEqual(op["amount"], 4)
        self.assertEqual(op["duration"], "1 hour")
        self.assertEqual(op["source"], "Nervescrub")
        self.assertEqual(op["target"], "BlackHat")

    def test_nervescrub_metadata(self):
        e = cpe.PROGRAM_EFFECTS["Nervescrub"]
        self.assertEqual(e["category"], "attacker")
        self.assertIn("on_program_attack_hit", e["hooks"])

    # ----- Poison Flatline -----

    def test_poison_flatline_destroys_random_program(self):
        progs = [
            self._attacker("Poison Flatline"),
            {"name": "Worm", "status": "active", "rez": 7, "category": "booster"},
            {"name": "Armor", "status": "active", "rez": 7, "category": "defender"},
        ]
        firing = progs[0]
        # Force random.choice to pick first candidate deterministically.
        with patch("game_systems.cpred_program_effects.random.choice",
                   side_effect=lambda seq: seq[0]):
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True, "target_name": "BlackHat"},
                self._hs(progs), firing_program_name="Poison Flatline")
        destroy_ops = [o for o in ops if o.get("op") == "program_destroy"]
        self.assertEqual(len(destroy_ops), 1)
        self.assertEqual(destroy_ops[0]["program_name"], "Worm")
        self.assertEqual(destroy_ops[0]["source"], "Poison Flatline")

    def test_poison_flatline_skips_self(self):
        """Poison Flatline shouldn't destroy itself when it's the firing program."""
        progs = [self._attacker("Poison Flatline")]
        ops, _ = cpe.run_program_attack_hit_hooks(
            {"hit": True, "target_name": "X"},
            self._hs(progs), firing_program_name="Poison Flatline")
        destroy_ops = [o for o in ops if o.get("op") == "program_destroy"]
        self.assertEqual(destroy_ops, [])
        no_target = [o for o in ops if o.get("op") == "poison_flatline_no_target"]
        self.assertEqual(len(no_target), 1)

    def test_poison_flatline_metadata(self):
        e = cpe.PROGRAM_EFFECTS["Poison Flatline"]
        self.assertEqual(e["category"], "attacker")

    # ----- Superglue -----

    def test_superglue_emits_movement_lock_and_jack_out_lock(self):
        progs = [self._attacker("Superglue")]
        with patch("game_systems.cpred_program_effects.random.randint",
                   return_value=4):
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True, "target_name": "BlackHat"},
                self._hs(progs), firing_program_name="Superglue")
        ml = [o for o in ops if o.get("op") == "movement_lock"]
        jl = [o for o in ops if o.get("op") == "jack_out_lock"]
        self.assertEqual(len(ml), 1)
        self.assertEqual(len(jl), 1)
        self.assertEqual(jl[0]["rounds_remaining"], 4)
        self.assertEqual(jl[0]["source"], "Superglue")

    def test_superglue_duration_varies_with_d6_roll(self):
        progs = [self._attacker("Superglue")]
        with patch("game_systems.cpred_program_effects.random.randint",
                   return_value=1):
            ops, _ = cpe.run_program_attack_hit_hooks(
                {"hit": True, "target_name": "X"},
                self._hs(progs), firing_program_name="Superglue")
        jl = [o for o in ops if o.get("op") == "jack_out_lock"]
        self.assertEqual(jl[0]["rounds_remaining"], 1)

    # ----- Overclock -----

    def test_overclock_registered(self):
        e = cpe.PROGRAM_EFFECTS.get("Overclock")
        self.assertIsNotNone(e)
        self.assertEqual(e["category"], "boosted_action")
        self.assertEqual(e["order"], 60)

    def test_overclock_on_turn_end_is_noop(self):
        """Overclock pending flag persists until consumed at turn-boundary
        NA reset (apply_hack_state). on_turn_end is a no-op."""
        progs = [{"name": "Overclock", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        ops, _ = cpe.run_turn_end_hooks(
            self._hs(progs, active_boosts={"overclock_pending": True}), {})
        # Overclock contributes no ops (Fortify might emit its clear though,
        # so filter to overclock-related)
        oc_ops = [o for o in ops if o.get("source") == "Overclock"
                  or "overclock" in str(o).lower()]
        self.assertEqual(oc_ops, [])


class TestStep6cJackOutGate(unittest.TestCase):
    """Step 6c: _check_jack_out_allowed predicate + integration with
    _apply_initiate_unsafe_jack_out and the voluntary hack_complete path."""

    def test_gate_allows_with_no_lock(self):
        from game_systems.cpred_hack import _check_jack_out_allowed
        state = {"active_boosts": {}}
        allowed, reason = _check_jack_out_allowed(state, cause="self_unplugged")
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_gate_blocks_self_unplugged_when_glued(self):
        from game_systems.cpred_hack import _check_jack_out_allowed
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 3}}
        allowed, reason = _check_jack_out_allowed(state, cause="self_unplugged")
        self.assertFalse(allowed)
        self.assertIn("Superglue", reason)
        self.assertIn("3", reason)

    def test_gate_allows_ally_unplugged_even_when_glued(self):
        """Physical severance bypasses the glue."""
        from game_systems.cpred_hack import _check_jack_out_allowed
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 5}}
        for cause in ("ally_unplugged", "ally_dragged_out_of_range",
                       "connection_severed", "flatline"):
            allowed, _ = _check_jack_out_allowed(state, cause=cause)
            self.assertTrue(allowed, f"cause={cause!r} wrongly blocked")

    def test_gate_blocks_voluntary_safe_when_glued(self):
        from game_systems.cpred_hack import _check_jack_out_allowed
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 1}}
        allowed, reason = _check_jack_out_allowed(state, cause="voluntary_safe")
        self.assertFalse(allowed)
        self.assertIn("Superglue", reason)

    def test_gate_zero_lock_allows(self):
        from game_systems.cpred_hack import _check_jack_out_allowed
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 0}}
        allowed, _ = _check_jack_out_allowed(state, cause="self_unplugged")
        self.assertTrue(allowed)

    # ----- Integration: _apply_initiate_unsafe_jack_out gate -----

    def test_initiate_unsafe_jack_out_blocks_self_unplugged_when_glued(self):
        from game_systems.cpred_hack import _apply_initiate_unsafe_jack_out
        state = {
            "active": True,
            "active_boosts": {"jack_out_lock_rounds_remaining": 2},
            "ice_status": {},
        }
        tool_input = {"initiate_unsafe_jack_out": {
            "cause": "self_unplugged", "actor": "self",
            "reason": "Yanking the plug",
        }}
        _apply_initiate_unsafe_jack_out(state, tool_input, {}, "hacker_name")
        # Hack still active — jack-out blocked
        self.assertTrue(state.get("active"))
        self.assertNotIn("_cascade_applied", state)
        # Rejection surfaced for narration
        rej = state.get("_jack_out_rejected")
        self.assertIsNotNone(rej)
        self.assertEqual(rej["cause"], "self_unplugged")
        self.assertIn("Superglue", rej["gate_reason"])

    def test_initiate_unsafe_jack_out_allows_ally_unplug_when_glued(self):
        from game_systems.cpred_hack import _apply_initiate_unsafe_jack_out
        state = {
            "active": True,
            "active_boosts": {"jack_out_lock_rounds_remaining": 5},
            "ice_status": {},
        }
        tool_input = {"initiate_unsafe_jack_out": {
            "cause": "ally_unplugged", "actor": "Vincent",
            "reason": "Vincent yanks the cable",
        }}
        _apply_initiate_unsafe_jack_out(state, tool_input, {}, "hacker_name")
        # Cascade fired (ally bypasses glue)
        self.assertTrue(state.get("_cascade_applied"))
        self.assertNotIn("_jack_out_rejected", state)

    # ----- Integration: voluntary safe Jack Out (hack_complete=True) -----

    def test_hack_complete_blocked_when_glued(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["active_boosts"] = {"jack_out_lock_rounds_remaining": 2}
        apply_hack_state(hs, {
            "hack_complete": True,
            "narrative_summary": "Jacking out clean",
        })
        # Hack still active — voluntary jack-out blocked
        self.assertTrue(hs.get("active"))
        self.assertIsNotNone(hs.get("_jack_out_rejected"))

    def test_hack_complete_allowed_when_unglued(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        apply_hack_state(hs, {
            "hack_complete": True,
            "narrative_summary": "Clean exit",
        })
        self.assertFalse(hs.get("active"))
        self.assertEqual(hs.get("narrative_summary"), "Clean exit")

    def test_hack_complete_allowed_after_glue_expires(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["active_boosts"] = {"jack_out_lock_rounds_remaining": 0}
        apply_hack_state(hs, {
            "hack_complete": True,
            "narrative_summary": "Out the back door",
        })
        self.assertFalse(hs.get("active"))


class TestStep6cWriteback(unittest.TestCase):
    """Resolver state ops emitted by Step 6c hooks/handlers are consumed
    correctly by _apply_resolver_net_ops and apply_hack_state."""

    def test_jack_out_lock_op_sets_active_boost_countdown(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops
        state = {"active_boosts": {}}
        _apply_resolver_net_ops(state,
            [{"op": "jack_out_lock", "rounds_remaining": 4,
              "source": "Superglue"}])
        self.assertEqual(state["active_boosts"]["jack_out_lock_rounds_remaining"], 4)

    def test_jack_out_lock_takes_max_when_stacked(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 2}}
        _apply_resolver_net_ops(state,
            [{"op": "jack_out_lock", "rounds_remaining": 5,
              "source": "Superglue"}])
        self.assertEqual(state["active_boosts"]["jack_out_lock_rounds_remaining"], 5)

    def test_jack_out_lock_existing_higher_keeps_existing(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops
        state = {"active_boosts": {"jack_out_lock_rounds_remaining": 6}}
        _apply_resolver_net_ops(state,
            [{"op": "jack_out_lock", "rounds_remaining": 2,
              "source": "Superglue"}])
        self.assertEqual(state["active_boosts"]["jack_out_lock_rounds_remaining"], 6)

    def test_turn_boundary_decrements_jack_out_lock(self):
        """End of turn ticks the Superglue countdown."""
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_boosts"] = {"jack_out_lock_rounds_remaining": 3}
        # Burn the whole turn to trigger the boundary reset
        apply_hack_state(hs, {"hack_state": {
            "net_actions_used": hs["net_actions_per_turn"],
        }})
        self.assertEqual(hs["active_boosts"]["jack_out_lock_rounds_remaining"], 2)

    def test_turn_boundary_clears_jack_out_lock_at_zero(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_boosts"] = {"jack_out_lock_rounds_remaining": 1}
        apply_hack_state(hs, {"hack_state": {
            "net_actions_used": hs["net_actions_per_turn"],
        }})
        self.assertNotIn("jack_out_lock_rounds_remaining", hs["active_boosts"])

    # ----- Overclock turn-boundary integration -----

    def test_overclock_grants_plus_one_na_at_turn_reset(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        base_na = hs["net_actions_per_turn"]
        hs["active_boosts"] = {"overclock_pending": True}
        # Consume all NA → triggers turn boundary reset
        apply_hack_state(hs, {"hack_state": {"net_actions_used": base_na}})
        self.assertEqual(hs["net_actions_remaining"], base_na + 1)
        # Single-shot — flag cleared
        self.assertNotIn("overclock_pending", hs["active_boosts"])

    def test_overclock_does_not_persist_across_turns(self):
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        base_na = hs["net_actions_per_turn"]
        hs["active_boosts"] = {"overclock_pending": True}
        # First turn boundary: bonus applies
        apply_hack_state(hs, {"hack_state": {"net_actions_used": base_na}})
        self.assertEqual(hs["net_actions_remaining"], base_na + 1)
        # Second turn boundary: no more bonus
        apply_hack_state(hs, {"hack_state": {"net_actions_used": base_na + 1}})
        self.assertEqual(hs["net_actions_remaining"], base_na)

    def test_overclock_flag_via_boosted_action_resolver(self):
        """End-to-end: boosted_action sets overclock_pending."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Overclock", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        result = resolve_actions(
            [{"type": "boosted_action", "character": "RedVelvet",
              "program": "Overclock"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=2,
        )
        r = result["results"][0]
        self.assertTrue(r["success"])
        self.assertEqual(r["active_boost"], "overclock_pending")


class TestStep6dPvpKickerGate(unittest.TestCase):
    """Step 6d P0 fix: PvP-designed Attacker programs (Hellbolt, Vrizzbolt,
    Nervescrub, Poison Flatline, Superglue, DeckKRASH) emit kicker ops
    scoped to a Netrunner target. _apply_resolver_net_ops applies ops to
    the firing Netrunner's state regardless of op.target — so firing
    these at ICE self-applies the kicker. The fix gates kicker hooks on
    target-is-not-ICE."""

    def _ice_status(self):
        return {"Lobby_Dragon": {
            "name": "Dragon", "behavior": "black", "ice_type": "dragon",
            "rez_current": 30, "rez_max": 30, "status": "active",
        }}

    def _attacker_progs(self, name, extra=None):
        out = [{"name": name, "status": "active", "rez": 0,
                "category": "attacker"}]
        if extra:
            out.extend(extra)
        return out

    # ----- Each PvP program: kicker skipped on ICE target -----

    def test_nervescrub_against_ice_no_self_debuff(self):
        from game_systems.cpred_mechanics import resolve_actions
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_programs"] = self._attacker_progs("Nervescrub")
        hs["ice_status"] = self._ice_status()
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3]), \
             patch("game_systems.cpred_program_effects.random.randint",
                   return_value=4):
            result = resolve_actions(
                [{"type": "program_attack", "character": "V",
                  "interface_rank": 8, "program": "Nervescrub",
                  "target": "Dragon", "program_damage_dice": 0}],
                ice_status=hs["ice_status"],
                active_programs=hs["active_programs"],
            )
        _apply_resolver_net_ops(hs, result["state_ops"])
        self.assertEqual(hs.get("active_debuffs", []), [])
        self.assertIn("pvp_kicker_skipped", result["results"][0])

    def test_poison_flatline_against_ice_no_self_destroy(self):
        from game_systems.cpred_mechanics import resolve_actions
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_programs"] = self._attacker_progs("Poison Flatline", extra=[
            {"name": "Worm", "status": "active", "rez": 7, "category": "booster"},
        ])
        hs["ice_status"] = self._ice_status()
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "V",
                  "interface_rank": 8, "program": "Poison Flatline",
                  "target": "Dragon", "program_damage_dice": 0}],
                ice_status=hs["ice_status"],
                active_programs=hs["active_programs"],
            )
        _apply_resolver_net_ops(hs, result["state_ops"])
        # Worm survives — kicker didn't fire
        worm = next(p for p in hs["active_programs"] if p["name"] == "Worm")
        self.assertEqual(worm["status"], "active")
        self.assertEqual(hs.get("destroyed_programs", []), [])

    def test_superglue_against_ice_no_self_lock(self):
        from game_systems.cpred_mechanics import resolve_actions
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state, _check_jack_out_allowed
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_programs"] = self._attacker_progs("Superglue")
        hs["ice_status"] = self._ice_status()
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3]), \
             patch("game_systems.cpred_program_effects.random.randint",
                   return_value=4):
            result = resolve_actions(
                [{"type": "program_attack", "character": "V",
                  "interface_rank": 8, "program": "Superglue",
                  "target": "Dragon", "program_damage_dice": 0}],
                ice_status=hs["ice_status"],
                active_programs=hs["active_programs"],
            )
        _apply_resolver_net_ops(hs, result["state_ops"])
        self.assertIsNone(hs.get("movement_locked_by"))
        self.assertEqual(hs.get("active_boosts", {}).get(
            "jack_out_lock_rounds_remaining", 0), 0)
        allowed, _ = _check_jack_out_allowed(hs, cause="self_unplugged")
        self.assertTrue(allowed)

    def test_deckkrash_against_ice_no_self_jack_out(self):
        from game_systems.cpred_mechanics import resolve_actions
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_programs"] = self._attacker_progs("DeckKRASH")
        hs["ice_status"] = self._ice_status()
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "V",
                  "interface_rank": 8, "program": "DeckKRASH",
                  "target": "Dragon", "program_damage_dice": 0}],
                ice_status=hs["ice_status"],
                active_programs=hs["active_programs"],
            )
        _apply_resolver_net_ops(hs, result["state_ops"])
        self.assertTrue(hs.get("active"))  # not forced disconnected
        self.assertFalse(hs.get("_cascade_applied"))

    def test_sword_against_ice_normal_behavior(self):
        """Regression check: Sword/Banhammer (non-PvP attackers) still
        fire normally against ICE. Damage scaling + auto-Deactivate
        unchanged."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Sword", "status": "active", "rez": 0,
                  "category": "attacker"}]
        with patch("game_systems.cpred_mechanics.random.randint",
                   side_effect=[8, 3, 5, 5, 5]):
            result = resolve_actions(
                [{"type": "program_attack", "character": "V",
                  "interface_rank": 8, "program": "Sword",
                  "target": "Dragon", "program_damage_dice": 99}],
                ice_status=self._ice_status(),
                active_programs=progs,
            )
        r = result["results"][0]
        self.assertTrue(r["hit"])
        self.assertEqual(r["damage_total"], 15)  # 3d6 vs Black
        # No PvP-skip annotation (Sword has no kicker to skip)
        self.assertNotIn("pvp_kicker_skipped", r)
        # Auto-Deactivate still fires
        psc_ops = [op for op in result["state_ops"]
                   if isinstance(op, dict)
                   and op.get("op") == "program_status_change"
                   and op.get("program_name") == "Sword"]
        self.assertEqual(len(psc_ops), 1)


class TestStep6dInitiateUnsafeJackOutOpHandler(unittest.TestCase):
    """Step 6d P1 fix: resolver-emitted initiate_unsafe_jack_out op now
    routes through _apply_initiate_unsafe_jack_out (same gate + cascade)."""

    def test_initiate_unsafe_jack_out_op_triggers_cascade(self):
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["active"] = True
        hs["ice_status"] = {"Lobby_Hellhound": {
            "name": "Hellhound", "behavior": "black", "ice_type": "hellhound",
            "rez_current": 15, "status": "active",
        }}
        ops = [{
            "op": "initiate_unsafe_jack_out",
            "cause": "deckkrash_attack",
            "actor": "DeckKRASH",
            "target": "V",
            "reason": "DeckKRASH cascade",
        }]
        _apply_resolver_net_ops(hs, ops)
        self.assertTrue(hs.get("_cascade_applied"))
        self.assertTrue(hs.get("_forced_disconnect"))

    def test_initiate_unsafe_jack_out_op_blocked_by_glue(self):
        """Self-cause unsafe jack-out is gated by Superglue — same as the
        model-supplied tool_input path."""
        from game_systems.cpred_hack import _apply_resolver_net_ops, init_hack_state
        hs = init_hack_state(tier="full_run")
        hs["active"] = True
        hs["active_boosts"] = {"jack_out_lock_rounds_remaining": 3}
        ops = [{
            "op": "initiate_unsafe_jack_out",
            "cause": "self_unplugged",
            "actor": "self",
            "reason": "yanking",
        }]
        _apply_resolver_net_ops(hs, ops)
        self.assertTrue(hs.get("active"))  # not disconnected
        self.assertIsNotNone(hs.get("_jack_out_rejected"))


class TestStep6dBoostedActionSameBatchComposition(unittest.TestCase):
    """Step 6d P1 fix: Boosted Surge → check in the same batch now sees
    the surge_pending flag because the boosted_action handler mutates
    _active_boosts in-place (mirrors Step 0e composition fix)."""

    def test_boosted_surge_then_backdoor_in_same_batch(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        active_boosts = {}
        with patch("game_systems.cpred_mechanics.random.randint", return_value=4):
            result = resolve_actions(
                [
                    {"type": "boosted_action", "character": "V", "program": "Surge"},
                    {"type": "skill_check", "character": "V", "stat_value": 4,
                     "skill_value": 0, "dv": 13,
                     "net": True, "ability": "Backdoor"},
                ],
                active_programs=progs,
                net_actions_remaining=3,
                cycles_remaining=2,
                active_boosts=active_boosts,
            )
        r1 = result["results"][1]
        # d10(4) + Interface(4) + Surge(4) = 12
        self.assertEqual(r1["total"], 12)
        self.assertEqual(r1.get("booster_bonuses"), [("Surge", 4)])
        # surge_pending was cleared by the hook firing
        self.assertNotIn("surge_pending", active_boosts)

    def test_boosted_mask_then_alert_increase_in_same_batch_via_writeback(self):
        """Mask suppression composition is exercised at writeback time
        (alert_level changes route through _apply_net_model_fields). This
        test verifies the in-batch mutation propagates to the writeback
        path correctly."""
        from game_systems.cpred_mechanics import resolve_actions
        from game_systems.cpred_hack import apply_hack_state, init_hack_state
        progs = [{"name": "Mask", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        hs = init_hack_state(tier="full_run", interface_rank=4)
        hs["active_programs"] = progs
        hs["alert_level"] = 0
        # Activate Mask via boosted_action — emits active_boost_set op
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Mask"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=3,
            active_boosts=hs["active_boosts"],
        )
        # Apply the resolver state ops to flush active_boost_set
        apply_hack_state(hs, {"hack_state": {}}, resolver_state_ops=result["state_ops"])
        self.assertTrue(hs["active_boosts"].get("mask_pending"))
        # Now model reports alert level going to 2 — Mask should suppress
        apply_hack_state(hs, {"hack_state": {"alert_level": 2}})
        self.assertEqual(hs["alert_level"], 0)
        self.assertNotIn("mask_pending", hs["active_boosts"])


class TestStep6dBoostedActionStatusValidation(unittest.TestCase):
    """Step 6d P1 fix: Boosted Action rejects destroyed/derezzed programs
    (RAW: programs in those states are unusable until recovered)."""

    def test_boosted_action_rejects_destroyed_program(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "destroyed", "rez": 0,
                  "category": "boosted_action"}]
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertFalse(r["success"])
        self.assertEqual(r["error"], "program_unusable")
        self.assertIn("destroyed", r["reason"].lower())
        self.assertIn("reinstall_program", r["reason"])
        # No NA/Cycle consumed (atomic fail)
        self.assertEqual(
            [op for op in result["state_ops"]
             if op.get("op") in ("active_boost_set", "cycle_consumed")], [])

    def test_boosted_action_rejects_derezzed_program(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "derezzed", "rez": 0,
                  "category": "boosted_action"}]
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        r = result["results"][0]
        self.assertEqual(r["error"], "program_unusable")
        self.assertIn("derezzed", r["reason"].lower())
        self.assertIn("reactivate_program", r["reason"])

    def test_boosted_action_accepts_active_program(self):
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "active", "rez": 7,
                  "category": "boosted_action"}]
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        self.assertTrue(result["results"][0]["success"])

    def test_boosted_action_accepts_deactivated_program(self):
        """RAW: a deactivated program is just stored — Boosted Action
        activation implicitly powers it on."""
        from game_systems.cpred_mechanics import resolve_actions
        progs = [{"name": "Surge", "status": "deactivated", "rez": 7,
                  "category": "boosted_action"}]
        result = resolve_actions(
            [{"type": "boosted_action", "character": "V", "program": "Surge"}],
            active_programs=progs,
            net_actions_remaining=3,
            cycles_remaining=3,
        )
        self.assertTrue(result["results"][0]["success"])


if __name__ == "__main__":
    unittest.main()
