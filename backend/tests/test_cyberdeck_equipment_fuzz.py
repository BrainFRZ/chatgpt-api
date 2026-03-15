"""Fuzz tests for cyberdeck/equipment changes:

1. _default_edgerunner now includes cyberdeck field (defaults None)
2. build_game_injection renders cyberdeck + programs in [EDGERUNNER STATE]
3. set op bootstraps cyberdeck into edgerunner state
4. _sync_cpred_character_states_from_game_state does NOT propagate summary
5. STATE_REPORT_TOOL schema no longer contains summary
"""

import copy
import os
import sys
import random

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred_core import (
    _default_edgerunner,
    apply_game_state,
    build_game_injection,
)
from game_systems.cpred import STATE_REPORT_TOOL
from game_systems.cpred_hack import (
    _resolve_netrunner_name,
    build_netrunner_profile,
)
from pipeline import _sync_cpred_character_states_from_game_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_edgerunner(**overrides):
    """Build a fully-populated edgerunner with sensible defaults + overrides."""
    er = _default_edgerunner()
    er["hp"] = {"current": 40, "max": 40, "seriously_wounded": False}
    er["humanity"] = {"current": 50, "max": 60}
    er["luck"] = {"current": 5, "max": 7}
    er["armor"] = {"head": 11, "body": 11}
    er["eurobucks"] = 2000
    for k, v in overrides.items():
        er[k] = v
    return er


def _game_state_with(name="V", **er_overrides):
    return {"edgerunners": {name: _make_edgerunner(**er_overrides)}}


# Hypothesis strategies
_tier_st = st.sampled_from(["Standard", "Upgraded", "Advanced", "Superior", "Experimental", ""])
_small_int = st.integers(min_value=0, max_value=20)
_junk = st.one_of(
    st.none(),
    st.booleans(),
    st.text(max_size=30),
    st.integers(min_value=-999, max_value=999),
    st.floats(allow_nan=True, allow_infinity=True),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
)

_cyberdeck_st = st.fixed_dictionaries({
    "tier": _tier_st,
    "slots": _small_int,
    "cycles": _small_int,
})

_program_st = st.fixed_dictionaries({
    "name": st.text(min_size=1, max_size=20),
    "category": st.sampled_from(["Anti-Personnel", "Anti-Program", "Booster", "Defender", ""]),
    "rez_max": _small_int,
    "status": st.sampled_from(["stored", "rezzed", "deactivated", ""]),
})


# ===========================================================================
# 1. _default_edgerunner includes cyberdeck
# ===========================================================================

class TestDefaultEdgerunnerCyberdeck:
    def test_cyberdeck_field_exists(self):
        er = _default_edgerunner()
        assert "cyberdeck" in er

    def test_cyberdeck_defaults_to_none(self):
        er = _default_edgerunner()
        assert er["cyberdeck"] is None

    def test_deck_slots_defaults_empty(self):
        er = _default_edgerunner()
        assert er["deck_slots"] == []

    def test_all_default_fields_present(self):
        """Ensure adding cyberdeck didn't break the field ordering."""
        er = _default_edgerunner()
        expected_subset = {"hp", "humanity", "luck", "armor", "eurobucks",
                           "critical_injuries", "cyberware_effects", "weapons",
                           "deck_slots", "cyberdeck", "conditions"}
        assert expected_subset.issubset(set(er.keys()))


# ===========================================================================
# 2. build_game_injection — cyberdeck + programs rendering
# ===========================================================================

class TestBuildGameInjectionCyberdeck:
    """Fuzz the cyberdeck/programs rendering in build_game_injection."""

    def test_no_cyberdeck_no_deck_line(self):
        gs = _game_state_with(cyberdeck=None, deck_slots=[])
        result = build_game_injection(gs)
        assert "Cyberdeck" not in result
        assert "Programs:" not in result

    def test_cyberdeck_none_with_deck_slots_still_no_deck_line(self):
        """Deck slots without a cyberdeck should not render deck/program lines."""
        gs = _game_state_with(
            cyberdeck=None,
            deck_slots=[{"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"}],
        )
        result = build_game_injection(gs)
        assert "Cyberdeck" not in result
        # Programs line should also not render since the code checks isinstance(cyberdeck, dict)
        assert "Programs:" not in result

    def test_basic_cyberdeck_renders(self):
        gs = _game_state_with(
            cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3},
            deck_slots=[],
        )
        result = build_game_injection(gs)
        assert "Cyberdeck: Standard" in result
        assert "0/7 slots" in result
        assert "3 cycles" in result
        assert "Programs:" not in result  # empty deck_slots

    def test_cyberdeck_with_programs_renders_both(self):
        deck_slots = [
            {"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"},
            {"name": "Armor", "type": "program", "category": "Defender", "rez_max": 4, "status": "stored"},
        ]
        gs = _game_state_with(
            cyberdeck={"tier": "Upgraded", "slots": 9, "cycles": 5},
            deck_slots=deck_slots,
        )
        result = build_game_injection(gs)
        assert "Cyberdeck: Upgraded" in result
        assert "2/2 slots" in result  # 2 items in deck_slots array
        assert "5 cycles" in result
        assert "Programs:" in result
        assert "Sword (Anti-Program, rezzed)" in result
        assert "Armor (Defender, stored)" in result

    def test_cyberdeck_with_hardware_renders(self):
        deck_slots = [
            {"name": "Armor", "type": "program", "category": "Defender", "rez_max": 7, "status": "stored"},
            {"name": "Backup Drive", "type": "hardware", "slots_used": 2},
            {"_continuation_of": "Backup Drive"},
            None,
        ]
        gs = _game_state_with(
            cyberdeck={"tier": "Standard", "slots": 4, "cycles": 3},
            deck_slots=deck_slots,
        )
        result = build_game_injection(gs)
        assert "3/4 slots" in result
        assert "Programs:" in result
        assert "Armor (Defender, stored)" in result
        assert "Hardware:" in result
        assert "Backup Drive (2 slots)" in result

    @settings(max_examples=200, deadline=None)
    @given(deck=_cyberdeck_st, progs=st.lists(_program_st, max_size=10))
    def test_valid_cyberdeck_always_renders_without_error(self, deck, progs):
        deck_slots = [{**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p for p in progs]
        gs = _game_state_with(cyberdeck=deck, deck_slots=deck_slots)
        result = build_game_injection(gs)
        assert "Cyberdeck:" in result
        assert "slots" in result
        if progs:
            assert "Programs:" in result

    @settings(max_examples=200, deadline=None)
    @given(cyberdeck_value=_junk)
    def test_non_dict_cyberdeck_skipped_gracefully(self, cyberdeck_value):
        """Non-dict cyberdeck values should not render and should not crash."""
        assume(not isinstance(cyberdeck_value, dict))
        gs = _game_state_with(cyberdeck=cyberdeck_value)
        result = build_game_injection(gs)
        # Should not crash; "Cyberdeck:" line should not appear
        assert "Cyberdeck:" not in result

    def test_empty_dict_cyberdeck_renders_with_defaults(self):
        """An empty dict {} is still isinstance(dict) — should render with fallback values."""
        gs = _game_state_with(cyberdeck={}, deck_slots=[])
        result = build_game_injection(gs)
        assert "Cyberdeck: ?" in result
        assert "0/0 slots" in result
        assert "0 cycles" in result

    @settings(max_examples=200, deadline=None)
    @given(
        tier=_junk,
        slots=_junk,
        cycles=_junk,
    )
    def test_cyberdeck_with_garbage_fields_no_crash(self, tier, slots, cycles):
        """Cyberdeck dict with garbage field values should not crash."""
        gs = _game_state_with(
            cyberdeck={"tier": tier, "slots": slots, "cycles": cycles},
            deck_slots=[],
        )
        result = build_game_injection(gs)
        assert "[EDGERUNNER STATE]" in result
        assert "[/EDGERUNNER STATE]" in result

    @settings(max_examples=200, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_deck_slots_with_missing_fields_no_crash(self, seed):
        """Deck slot entries with missing or garbage fields should not crash rendering."""
        rng = random.Random(seed)
        fields = ["name", "category", "rez_max", "status"]
        prog = {"type": "program"}
        for f in fields:
            if rng.random() > 0.3:
                prog[f] = rng.choice(["Zap", "Armor", "", 42, None, True])
        gs = _game_state_with(
            cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3},
            deck_slots=[prog],
        )
        result = build_game_injection(gs)
        assert "Cyberdeck:" in result
        assert "Programs:" in result

    def test_multiple_edgerunners_only_deck_one(self):
        """Only the edgerunner with a cyberdeck should get a Cyberdeck line."""
        gs = {
            "edgerunners": {
                "V": _make_edgerunner(
                    cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3},
                    deck_slots=[{"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"}],
                ),
                "Jackie": _make_edgerunner(cyberdeck=None, deck_slots=[]),
            }
        }
        result = build_game_injection(gs)
        # Exactly one Cyberdeck line
        assert result.count("Cyberdeck:") == 1
        assert "Programs:" in result
        assert result.count("Programs:") == 1

    def test_cyberdeck_false_not_rendered(self):
        """cyberdeck=False is not a dict, should be skipped."""
        gs = _game_state_with(cyberdeck=False)
        result = build_game_injection(gs)
        assert "Cyberdeck:" not in result

    def test_cyberdeck_string_not_rendered(self):
        gs = _game_state_with(cyberdeck="Standard")
        result = build_game_injection(gs)
        assert "Cyberdeck:" not in result


# ===========================================================================
# 3. set op bootstraps cyberdeck into edgerunner state
# ===========================================================================

class TestSetOpCyberdeckBootstrap:
    def _apply_set(self, er_name, fields, game_state=None):
        if game_state is None:
            game_state = _game_state_with(er_name)
        agent_json = {
            "edgerunner_ops": [
                {"edgerunner": er_name, "op": "set", "fields": fields}
            ]
        }
        apply_game_state(game_state, agent_json, turn=1)
        return game_state["edgerunners"][er_name]

    def test_set_cyberdeck_basic(self):
        er = self._apply_set("V", {"cyberdeck": {"tier": "Standard", "slots": 7, "cycles": 3}})
        assert er["cyberdeck"] == {"tier": "Standard", "slots": 7, "cycles": 3}

    def test_set_cyberdeck_none_clears(self):
        gs = _game_state_with("V", cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3})
        er = self._apply_set("V", {"cyberdeck": None}, game_state=gs)
        assert er["cyberdeck"] is None

    @settings(max_examples=200, deadline=None)
    @given(deck=_cyberdeck_st)
    def test_set_cyberdeck_roundtrip(self, deck):
        """Any valid cyberdeck set via set op should be retrievable and renderable."""
        er = self._apply_set("V", {"cyberdeck": copy.deepcopy(deck)})
        assert er["cyberdeck"] == deck
        # And it should render without error
        gs = {"edgerunners": {"V": er}}
        result = build_game_injection(gs)
        assert "Cyberdeck:" in result

    @settings(max_examples=200, deadline=None)
    @given(deck=_junk)
    def test_set_cyberdeck_garbage_values_accepted_without_crash(self, deck):
        """The set op accepts any value; rendering should handle it gracefully."""
        er = self._apply_set("V", {"cyberdeck": deck})
        assert er["cyberdeck"] == deck
        gs = {"edgerunners": {"V": er}}
        # Should not crash
        build_game_injection(gs)

    def test_set_preserves_other_fields(self):
        """Setting cyberdeck shouldn't clobber unrelated fields."""
        gs = _game_state_with("V", eurobucks=5000)
        er = self._apply_set("V", {"cyberdeck": {"tier": "Advanced", "slots": 9, "cycles": 5}}, game_state=gs)
        assert er["cyberdeck"] == {"tier": "Advanced", "slots": 9, "cycles": 5}
        assert er["eurobucks"] == 5000
        assert er["hp"]["current"] == 40

    def test_set_unknown_field_ignored(self):
        """Fields not in _default_edgerunner() should not be set (set op checks `key in er`)."""
        er = self._apply_set("V", {"cyberdeck": {"tier": "Standard", "slots": 7, "cycles": 3}, "nonexistent_field": 42})
        assert er["cyberdeck"] == {"tier": "Standard", "slots": 7, "cycles": 3}
        assert "nonexistent_field" not in er

    @settings(max_examples=100, deadline=None)
    @given(
        deck=_cyberdeck_st,
        progs=st.lists(_program_st, min_size=1, max_size=8),
    )
    def test_set_cyberdeck_then_programs_set_compat(self, deck, progs):
        """Bootstrap cyberdeck via set, then programs via programs_set (backward compat) — both should render."""
        gs = _game_state_with("V")
        agent1 = {"edgerunner_ops": [{"edgerunner": "V", "op": "set", "fields": {"cyberdeck": copy.deepcopy(deck)}}]}
        apply_game_state(gs, agent1, turn=1)
        agent2 = {"edgerunner_ops": [{"edgerunner": "V", "op": "programs_set", "programs": copy.deepcopy(progs)}]}
        apply_game_state(gs, agent2, turn=2)

        er = gs["edgerunners"]["V"]
        assert er["cyberdeck"] == deck
        # programs_set backward compat converts to deck_slots
        assert len(er["deck_slots"]) == len(progs)

        result = build_game_injection(gs)
        assert "Cyberdeck:" in result
        assert "Programs:" in result

    @settings(max_examples=100, deadline=None)
    @given(
        deck=_cyberdeck_st,
        progs=st.lists(_program_st, min_size=1, max_size=8),
    )
    def test_set_with_legacy_programs_field_converts(self, deck, progs):
        """Model sending programs in set op fields should auto-convert to deck_slots."""
        gs = _game_state_with("V")
        fields = {"cyberdeck": copy.deepcopy(deck), "programs": copy.deepcopy(progs)}
        agent = {"edgerunner_ops": [{"edgerunner": "V", "op": "set", "fields": fields}]}
        apply_game_state(gs, agent, turn=1)

        er = gs["edgerunners"]["V"]
        assert er["cyberdeck"] == deck
        assert len(er["deck_slots"]) == len(progs)
        # All entries should have type: "program"
        for slot in er["deck_slots"]:
            if isinstance(slot, dict):
                assert slot.get("type") == "program"


# ===========================================================================
# 4. _sync_cpred_character_states_from_game_state vs summary
# ===========================================================================

class TestSyncDoesNotPropagateSummary:
    def _get_data(self, result, name):
        """Extract the data dict from sync result (may be wrapped or flat)."""
        entry = result.get(name, {})
        if isinstance(entry, dict) and "data" in entry:
            return entry["data"]
        return entry

    def test_sync_preserves_existing_summary(self):
        """If the model still sends summary, sync should not strip it (it just doesn't touch it)."""
        char_states = {
            "V": {
                "data": {
                    "type": "pc",
                    "class": "Solo",
                    "level": None,
                    "vitals": [{"label": "HP", "current": 30, "max": 40}],
                    "resources": [],
                    "conditions": [],
                    "summary": "Medium Pistol (8/8), Light Armorjack (SP 11/11)",
                },
                "last_updated": 0,
            }
        }
        gs = {"edgerunners": {"V": _make_edgerunner()}}
        result = _sync_cpred_character_states_from_game_state(
            copy.deepcopy(char_states), gs, current_turn=1
        )
        data = self._get_data(result, "V")
        # Sync doesn't delete summary — it just doesn't add it
        assert data.get("summary") == "Medium Pistol (8/8), Light Armorjack (SP 11/11)"

    def test_sync_does_not_add_summary(self):
        """Sync should never inject a summary field."""
        char_states = {
            "V": {
                "data": {
                    "type": "pc",
                    "class": "Solo",
                    "level": None,
                    "vitals": [{"label": "HP", "current": 30, "max": 40}],
                    "resources": [],
                    "conditions": [],
                },
                "last_updated": 0,
            }
        }
        gs = {
            "edgerunners": {
                "V": _make_edgerunner(
                    weapons=[{"name": "Pistol", "damage": "2d6", "current_ammo": 8, "max_ammo": 8, "type": "ranged"}],
                    cyberware_effects=["Cybereye"],
                    cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3},
                    deck_slots=[{"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"}],
                ),
            }
        }
        result = _sync_cpred_character_states_from_game_state(
            copy.deepcopy(char_states), gs, current_turn=1
        )
        data = self._get_data(result, "V")
        # summary should NOT appear — equipment lives in edgerunner state
        assert "summary" not in data

    @settings(max_examples=200, deadline=None)
    @given(
        weapons=st.lists(st.fixed_dictionaries({
            "name": st.text(min_size=1, max_size=10),
            "damage": st.text(min_size=1, max_size=5),
            "type": st.sampled_from(["ranged", "melee"]),
        }), max_size=5),
        cyberware=st.lists(st.text(min_size=1, max_size=15), max_size=5),
        deck=st.one_of(st.none(), _cyberdeck_st),
        progs=st.lists(_program_st, max_size=5),
    )
    def test_sync_never_injects_summary_fuzzed(self, weapons, cyberware, deck, progs):
        """No combination of equipment in edgerunner state should cause sync to add summary."""
        char_states = {
            "V": {
                "data": {
                    "type": "pc",
                    "class": "Netrunner",
                    "level": None,
                    "vitals": [],
                    "resources": [],
                    "conditions": [],
                },
                "last_updated": 0,
            }
        }
        deck_slots = [{**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p for p in progs]
        gs = {
            "edgerunners": {
                "V": _make_edgerunner(
                    weapons=weapons,
                    cyberware_effects=cyberware,
                    cyberdeck=deck,
                    deck_slots=deck_slots,
                ),
            }
        }
        result = _sync_cpred_character_states_from_game_state(
            copy.deepcopy(char_states), gs, current_turn=1
        )
        data = self._get_data(result, "V")
        assert "summary" not in data


# ===========================================================================
# 5. STATE_REPORT_TOOL schema no longer contains summary
# ===========================================================================

class TestStateReportToolSchema:
    def _get_char_state_props(self):
        """Navigate into the character_states additionalProperties."""
        params = STATE_REPORT_TOOL["input_schema"]
        cs = params["properties"]["character_states"]
        return cs["additionalProperties"]["properties"]

    def test_summary_removed_from_schema(self):
        props = self._get_char_state_props()
        assert "summary" not in props

    def test_conditions_still_present(self):
        props = self._get_char_state_props()
        assert "conditions" in props

    def test_vitals_still_present(self):
        props = self._get_char_state_props()
        assert "vitals" in props

    def test_resources_still_present(self):
        props = self._get_char_state_props()
        assert "resources" in props

    def test_type_still_present(self):
        props = self._get_char_state_props()
        assert "type" in props

    def test_class_still_present(self):
        props = self._get_char_state_props()
        assert "class" in props


# ===========================================================================
# 6. build_game_injection — weapons/armor/cyberware already render
#    (regression: ensure they still work alongside cyberdeck changes)
# ===========================================================================

class TestBuildGameInjectionEquipmentRegression:
    def test_weapons_render(self):
        gs = _game_state_with(
            weapons=[
                {"name": "Heavy Pistol", "damage": "3d6", "current_ammo": 8, "max_ammo": 8, "skill": "Handgun", "type": "ranged"},
                {"name": "Knife", "damage": "1d6", "skill": "Melee Weapon", "type": "melee"},
            ],
        )
        result = build_game_injection(gs)
        assert "Heavy Pistol" in result
        assert "8/8 ammo" in result
        assert "Knife" in result

    def test_cyberware_renders(self):
        gs = _game_state_with(cyberware_effects=["Cybereye (Low-Light)", "Neural Link"])
        result = build_game_injection(gs)
        assert "Cyberware:" in result
        assert "Cybereye (Low-Light)" in result
        assert "Neural Link" in result

    def test_conditions_render(self):
        gs = _game_state_with(conditions=["partially_nude", "unconscious"])
        result = build_game_injection(gs)
        assert "Conditions:" in result
        assert "partially_nude" in result

    def test_full_netrunner_renders_all_sections(self):
        """A fully kitted Netrunner should render weapons, armor, cyberware, cyberdeck, and deck slots."""
        gs = _game_state_with(
            weapons=[{"name": "Pistol", "damage": "2d6", "current_ammo": 12, "max_ammo": 12, "skill": "Handgun", "type": "ranged"}],
            cyberware_effects=["Neural Link", "Shift Tacts"],
            cyberdeck={"tier": "Advanced", "slots": 9, "cycles": 7},
            deck_slots=[
                {"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"},
                {"name": "Armor", "type": "program", "category": "Defender", "rez_max": 4, "status": "rezzed"},
                {"name": "Worm", "type": "program", "category": "Anti-Personnel", "rez_max": 5, "status": "stored"},
            ],
            conditions=["seriously_wounded"],
        )
        result = build_game_injection(gs)
        assert "Weapons:" in result
        assert "Pistol" in result
        assert "Cyberware:" in result
        assert "Neural Link" in result
        assert "Cyberdeck: Advanced" in result
        assert "3/3 slots" in result
        assert "7 cycles" in result
        assert "Programs:" in result
        assert "Sword (Anti-Program, rezzed)" in result
        assert "Armor (Defender, rezzed)" in result
        assert "Worm (Anti-Personnel, stored)" in result
        assert "Conditions:" in result
        assert "seriously_wounded" in result

    @settings(max_examples=200, deadline=None)
    @given(
        weapons=st.lists(st.fixed_dictionaries({
            "name": st.text(min_size=1, max_size=10),
            "damage": st.text(min_size=1, max_size=5),
            "type": st.sampled_from(["ranged", "melee"]),
            "current_ammo": _small_int,
            "max_ammo": _small_int,
            "skill": st.text(min_size=1, max_size=15),
        }), max_size=6),
        cyberware=st.lists(st.text(min_size=1, max_size=20), max_size=6),
        deck=st.one_of(st.none(), _cyberdeck_st),
        progs=st.lists(_program_st, max_size=8),
        conditions=st.lists(st.text(min_size=1, max_size=20), max_size=4),
    )
    def test_full_equipment_fuzz_no_crash(self, weapons, cyberware, deck, progs, conditions):
        """Any combination of equipment data should render without crashing."""
        deck_slots = [{**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p for p in progs]
        gs = _game_state_with(
            weapons=weapons,
            cyberware_effects=cyberware,
            cyberdeck=deck,
            deck_slots=deck_slots,
            conditions=conditions,
        )
        result = build_game_injection(gs)
        assert "[EDGERUNNER STATE]" in result
        assert "[/EDGERUNNER STATE]" in result
        # If cyberdeck is a dict, it should appear
        if isinstance(deck, dict):
            assert "Cyberdeck:" in result


# ===========================================================================
# 7. Edge cases: empty edgerunner state, no edgerunners
# ===========================================================================

class TestEdgeCases:
    def test_empty_edgerunners(self):
        result = build_game_injection({"edgerunners": {}})
        assert "empty" in result.lower()
        assert "Cyberdeck" not in result

    def test_no_edgerunners_key(self):
        result = build_game_injection({})
        assert "empty" in result.lower()

    def test_edgerunner_with_only_cyberdeck_no_other_equipment(self):
        er = _default_edgerunner()
        er["hp"] = {"current": 20, "max": 20, "seriously_wounded": False}
        er["humanity"] = {"current": 40, "max": 50}
        er["luck"] = {"current": 3, "max": 5}
        er["armor"] = {"head": 0, "body": 0}
        er["cyberdeck"] = {"tier": "Standard", "slots": 5, "cycles": 2}
        gs = {"edgerunners": {"Spider": er}}
        result = build_game_injection(gs)
        assert "Cyberdeck: Standard" in result
        assert "Weapons:" not in result
        assert "Cyberware:" not in result
        assert "Programs:" not in result  # empty programs list


# ===========================================================================
# 8. _resolve_netrunner_name uses edgerunner cyberdeck, not summary
# ===========================================================================

class TestResolveNetrunnerName:
    def _cs(self, name, cls="Solo", resources=None):
        """Build a minimal character_states entry."""
        return {
            name: {
                "data": {
                    "type": "pc",
                    "class": cls,
                    "vitals": [{"label": "HP", "current": 30, "max": 40}],
                    "resources": resources or [],
                    "conditions": [],
                },
                "last_updated": 1,
            }
        }

    def test_class_netrunner_wins(self):
        cs = {**self._cs("V", cls="Netrunner"), **self._cs("Jackie", cls="Solo")}
        assert _resolve_netrunner_name(cs) == "V"

    def test_cyberdeck_in_edgerunner_state_adds_score(self):
        """Edgerunner state cyberdeck field should contribute to netrunner detection."""
        cs = {**self._cs("V", cls="Solo"), **self._cs("Jackie", cls="Solo")}
        gs = {"edgerunners": {
            "V": _make_edgerunner(cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3}),
            "Jackie": _make_edgerunner(cyberdeck=None),
        }}
        result = _resolve_netrunner_name(cs, game_state=gs)
        assert result == "V"

    def test_cyberdeck_does_not_override_class(self):
        """Class=Netrunner (+4) should outweigh cyberdeck (+1)."""
        cs = {**self._cs("V", cls="Solo"), **self._cs("Jackie", cls="Netrunner")}
        gs = {"edgerunners": {
            "V": _make_edgerunner(cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3}),
            "Jackie": _make_edgerunner(cyberdeck=None),
        }}
        result = _resolve_netrunner_name(cs, game_state=gs)
        assert result == "Jackie"

    def test_no_game_state_still_works(self):
        """Should not crash when game_state is None."""
        cs = self._cs("V", cls="Netrunner")
        assert _resolve_netrunner_name(cs, game_state=None) == "V"

    def test_summary_field_ignored(self):
        """Even if legacy summary contains 'cyberdeck', it should NOT affect scoring."""
        cs = {
            "V": {
                "data": {
                    "type": "pc", "class": "Solo",
                    "vitals": [], "resources": [], "conditions": [],
                    "summary": "cyberdeck equipped, heavy pistol",
                },
                "last_updated": 1,
            },
            "Jackie": {
                "data": {
                    "type": "pc", "class": "Solo",
                    "vitals": [], "resources": [], "conditions": [],
                },
                "last_updated": 1,
            },
        }
        # Without game_state cyberdeck, both have score 0 → first PC wins
        result = _resolve_netrunner_name(cs, game_state={})
        # V is first alphabetically in iteration? Actually dict order...
        # Both score 0, so first_pc wins (whichever is iterated first)
        assert result in ("V", "Jackie")
        # The key point: summary should NOT give V a +1 bonus
        # Verify by giving Jackie a cyberdeck in game_state
        gs = {"edgerunners": {
            "V": _make_edgerunner(cyberdeck=None),
            "Jackie": _make_edgerunner(cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3}),
        }}
        result = _resolve_netrunner_name(cs, game_state=gs)
        assert result == "Jackie"

    @settings(max_examples=100, deadline=None)
    @given(
        has_deck_v=st.booleans(),
        has_deck_j=st.booleans(),
        class_v=st.sampled_from(["Solo", "Netrunner", "Tech", "Fixer"]),
        class_j=st.sampled_from(["Solo", "Netrunner", "Tech", "Fixer"]),
    )
    def test_resolve_never_crashes_fuzzed(self, has_deck_v, has_deck_j, class_v, class_j):
        cs = {**self._cs("V", cls=class_v), **self._cs("Jackie", cls=class_j)}
        gs = {"edgerunners": {
            "V": _make_edgerunner(
                cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3} if has_deck_v else None,
            ),
            "Jackie": _make_edgerunner(
                cyberdeck={"tier": "Upgraded", "slots": 9, "cycles": 5} if has_deck_j else None,
            ),
        }}
        result = _resolve_netrunner_name(cs, game_state=gs)
        assert result in ("V", "Jackie")


# ===========================================================================
# 9. build_netrunner_profile renders cyberdeck+programs from edgerunner state
# ===========================================================================

class TestBuildNetrunnerProfileCyberdeck:
    def _cs_and_gs(self, cyberdeck=None, deck_slots=None):
        cs = {
            "V": {
                "data": {
                    "type": "pc",
                    "class": "Netrunner",
                    "vitals": [{"label": "HP", "current": 30, "max": 40}],
                    "resources": [],
                    "conditions": [],
                },
                "last_updated": 1,
            }
        }
        gs = {"edgerunners": {"V": _make_edgerunner(
            cyberdeck=cyberdeck,
            deck_slots=deck_slots or [],
        )}}
        return cs, gs

    def test_cyberdeck_renders_in_profile(self):
        cs, gs = self._cs_and_gs(
            cyberdeck={"tier": "Advanced", "slots": 9, "cycles": 5},
            deck_slots=[
                {"name": "Sword", "type": "program", "category": "Anti-Program", "rez_max": 3, "status": "rezzed"},
                None, None, None, None, None, None, None, None,
            ],
        )
        profile = build_netrunner_profile(cs, game_state=gs, hack_state={"hacker_name": "V"})
        assert "Cyberdeck: Advanced" in profile
        assert "1/9 slots" in profile
        assert "5 cycles" in profile
        assert "Programs:" in profile
        assert "Sword (Anti-Program, rezzed)" in profile

    def test_no_cyberdeck_no_deck_line(self):
        cs, gs = self._cs_and_gs(cyberdeck=None)
        profile = build_netrunner_profile(cs, game_state=gs, hack_state={"hacker_name": "V"})
        assert "Cyberdeck:" not in profile
        assert "Programs:" not in profile

    def test_summary_not_injected(self):
        """Legacy summary field should NOT appear as 'Equipment:' line."""
        cs = {
            "V": {
                "data": {
                    "type": "pc", "class": "Netrunner",
                    "vitals": [], "resources": [], "conditions": [],
                    "summary": "Heavy pistol, cyberdeck, armored jacket",
                },
                "last_updated": 1,
            }
        }
        gs = {"edgerunners": {"V": _make_edgerunner()}}
        profile = build_netrunner_profile(cs, game_state=gs, hack_state={"hacker_name": "V"})
        assert "Equipment:" not in profile

    def test_hardware_renders_in_profile(self):
        cs, gs = self._cs_and_gs(
            cyberdeck={"tier": "Standard", "slots": 7, "cycles": 3},
            deck_slots=[
                {"name": "Armor", "type": "program", "category": "Defender", "rez_max": 7, "status": "stored"},
                {"name": "Backup Drive", "type": "hardware", "slots_used": 2},
                {"_continuation_of": "Backup Drive"},
                None, None, None, None,
            ],
        )
        profile = build_netrunner_profile(cs, game_state=gs, hack_state={"hacker_name": "V"})
        assert "3/7 slots" in profile
        assert "Programs:" in profile
        assert "Armor (Defender, stored)" in profile
        assert "Hardware:" in profile
        assert "Backup Drive (2 slots)" in profile

    @settings(max_examples=100, deadline=None)
    @given(
        deck=st.one_of(st.none(), _cyberdeck_st),
        progs=st.lists(_program_st, max_size=8),
    )
    def test_profile_with_fuzzed_deck_no_crash(self, deck, progs):
        # Fuzz generates old-style program dicts; wrap them as deck_slots
        deck_slots = [{**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p for p in progs]
        cs, gs = self._cs_and_gs(cyberdeck=deck, deck_slots=deck_slots)
        profile = build_netrunner_profile(cs, game_state=gs, hack_state={"hacker_name": "V"})
        assert "[NETRUNNER PROFILE]" in profile
        assert "[/NETRUNNER PROFILE]" in profile
        if isinstance(deck, dict):
            assert "Cyberdeck:" in profile
