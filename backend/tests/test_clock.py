"""Tests for the in-game clock helpers: advance_clock, parse_time_passed, _advance_hud_clock."""

import pytest
from pipeline import advance_clock, parse_time_passed, _advance_hud_clock, DEFAULT_TURN_SECONDS


# ── parse_time_passed ──────────────────────────────────────

class TestParseTimePassed:
    def test_seconds(self):
        assert parse_time_passed("30 seconds") == 30

    def test_minutes(self):
        assert parse_time_passed("5 minutes") == 300

    def test_hours(self):
        assert parse_time_passed("2 hours") == 7200

    def test_days(self):
        assert parse_time_passed("3 days") == 259200

    def test_compound(self):
        assert parse_time_passed("1 hour 30 minutes") == 5400

    def test_compound_with_seconds(self):
        assert parse_time_passed("2 hours 15 minutes 10 seconds") == 8110

    def test_singular(self):
        assert parse_time_passed("1 minute") == 60
        assert parse_time_passed("1 hour") == 3600
        assert parse_time_passed("1 day") == 86400
        assert parse_time_passed("1 second") == 1

    def test_garbage(self):
        assert parse_time_passed("some random text") == 0

    def test_empty(self):
        assert parse_time_passed("") == 0
        assert parse_time_passed(None) == 0

    def test_numeric_only(self):
        assert parse_time_passed("42") == 0

    def test_case_insensitive(self):
        assert parse_time_passed("5 Minutes") == 300
        assert parse_time_passed("2 HOURS") == 7200


# ── advance_clock ──────────────────────────────────────────

class TestAdvanceClock:
    def test_basic(self):
        t, d = advance_clock("1400", "2045-08-22", 300)
        assert t == "1405"
        assert d == "2045-08-22"

    def test_hour_rollover(self):
        t, d = advance_clock("1355", "2045-08-22", 600)
        assert t == "1405"

    def test_midnight_rollover(self):
        t, d = advance_clock("2350", "2045-08-22", 1200)  # 20 minutes
        assert t == "0010"
        assert d == "2045-08-23"

    def test_multi_day(self):
        t, d = advance_clock("1200", "2045-08-30", 86400 + 3600)  # 1 day + 1 hour
        assert t == "1300"
        assert d == "2045-08-31"

    def test_multi_day_month_rollover(self):
        t, d = advance_clock("2300", "2045-08-31", 7200)  # 2 hours
        assert t == "0100"
        assert d == "2045-09-01"

    def test_unparseable_time(self):
        t, d = advance_clock("noon", "2045-08-22", 300)
        assert t == "noon"
        assert d == "2045-08-22"

    def test_empty_time(self):
        t, d = advance_clock("", "2045-08-22", 300)
        assert t == ""
        assert d == "2045-08-22"

    def test_none_time(self):
        t, d = advance_clock(None, "2045-08-22", 300)
        assert t is None
        assert d == "2045-08-22"

    def test_three_digit_time(self):
        t, d = advance_clock("900", "2045-08-22", 3600)
        assert t == "1000"

    def test_zero_seconds(self):
        t, d = advance_clock("1400", "2045-08-22", 0)
        assert t == "1400"
        assert d == "2045-08-22"

    def test_empty_date_no_rollover(self):
        t, d = advance_clock("1400", "", 300)
        assert t == "1405"
        assert d == ""

    def test_empty_date_with_rollover(self):
        """Date stays empty if no date string provided even with midnight cross."""
        t, d = advance_clock("2350", "", 1200)
        assert t == "0010"
        assert d == ""


# ── _advance_hud_clock ─────────────────────────────────────

class TestAdvanceHudClock:
    def _make_ps(self, time="1400", date="2045-08-22", buf=0):
        return {
            "hud_state": {"time": time, "date": date, "location": "Watson"},
            "_clock_seconds_buffer": buf,
        }

    def test_sub_minute_accumulation(self):
        ps = self._make_ps()
        _advance_hud_clock(ps, 30)
        assert ps["_clock_seconds_buffer"] == 30
        assert ps["hud_state"]["time"] == "1400"  # No minute change yet

    def test_full_minute(self):
        ps = self._make_ps()
        _advance_hud_clock(ps, 60)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1401"

    def test_buffer_carries_over(self):
        ps = self._make_ps(buf=45)
        _advance_hud_clock(ps, 30)  # 45 + 30 = 75 → 1 min + 15s
        assert ps["_clock_seconds_buffer"] == 15
        assert ps["hud_state"]["time"] == "1401"

    def test_cpred_combat_accumulation(self):
        """20 rounds of 3-second combat = 1 minute."""
        ps = self._make_ps()
        for _ in range(20):
            _advance_hud_clock(ps, 3)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1401"

    def test_cpred_combat_partial(self):
        """10 rounds of 3-second combat = 30s in buffer."""
        ps = self._make_ps()
        for _ in range(10):
            _advance_hud_clock(ps, 3)
        assert ps["_clock_seconds_buffer"] == 30
        assert ps["hud_state"]["time"] == "1400"

    def test_no_clock_seed(self):
        """If no time set yet, skip advancement."""
        ps = {"hud_state": {"location": "Watson"}, "_clock_seconds_buffer": 0}
        _advance_hud_clock(ps, 30)
        assert ps["_clock_seconds_buffer"] == 0
        assert "time" not in ps["hud_state"]

    def test_zero_seconds_noop(self):
        ps = self._make_ps()
        _advance_hud_clock(ps, 0)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1400"

    def test_location_preserved(self):
        ps = self._make_ps()
        _advance_hud_clock(ps, 60)
        assert ps["hud_state"]["location"] == "Watson"

    def test_midnight_rollover(self):
        ps = self._make_ps(time="2359")
        _advance_hud_clock(ps, 120)  # 2 minutes
        assert ps["hud_state"]["time"] == "0001"
        assert ps["hud_state"]["date"] == "2045-08-23"

    def test_default_turn_seconds(self):
        """Default turn of 30s accumulates correctly."""
        ps = self._make_ps()
        _advance_hud_clock(ps, DEFAULT_TURN_SECONDS)
        assert ps["_clock_seconds_buffer"] == 30
        _advance_hud_clock(ps, DEFAULT_TURN_SECONDS)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1401"  # Two 30s turns = 1 minute

    def test_dnd5e_combat_accumulation(self):
        """10 rounds of 6-second D&D combat = 1 minute."""
        ps = self._make_ps()
        for _ in range(10):
            _advance_hud_clock(ps, 6)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1401"

    def test_dnd5e_combat_partial(self):
        """5 rounds of 6-second D&D combat = 30s in buffer."""
        ps = self._make_ps()
        for _ in range(5):
            _advance_hud_clock(ps, 6)
        assert ps["_clock_seconds_buffer"] == 30
        assert ps["hud_state"]["time"] == "1400"

    def test_ship_combat_accumulation(self):
        """2 rounds of 30-second ship combat = 1 minute."""
        ps = self._make_ps()
        for _ in range(2):
            _advance_hud_clock(ps, 30)
        assert ps["_clock_seconds_buffer"] == 0
        assert ps["hud_state"]["time"] == "1401"


# ── Game system combat_round_seconds fields ───────────────

class TestGameSystemRoundDurations:
    def test_dnd5e_has_combat_round_seconds(self):
        from game_systems.dnd5e import GAME_SYSTEM
        assert GAME_SYSTEM["combat_round_seconds"] == 6

    def test_dnd5e_cyber_has_combat_round_seconds(self):
        from game_systems.dnd5e_cyber import GAME_SYSTEM
        assert GAME_SYSTEM["combat_round_seconds"] == 6

    def test_dnd5e_cyber_has_ship_combat_round_seconds(self):
        from game_systems.dnd5e_cyber import GAME_SYSTEM
        assert GAME_SYSTEM["ship_combat_round_seconds"] == 30

    def test_cpred_has_combat_round_seconds(self):
        from game_systems.cpred import GAME_SYSTEM
        assert GAME_SYSTEM["combat_round_seconds"] == 3

    def test_coc7e_no_combat_round_seconds(self):
        from game_systems.coc7e import GAME_SYSTEM
        assert "combat_round_seconds" not in GAME_SYSTEM

    def test_sr6e_no_combat_round_seconds(self):
        from game_systems.sr6e import GAME_SYSTEM
        assert "combat_round_seconds" not in GAME_SYSTEM

    def test_dnd5e_no_ship_combat_round_seconds(self):
        from game_systems.dnd5e import GAME_SYSTEM
        assert "ship_combat_round_seconds" not in GAME_SYSTEM

    def test_cpred_no_ship_combat_round_seconds(self):
        from game_systems.cpred import GAME_SYSTEM
        assert "ship_combat_round_seconds" not in GAME_SYSTEM
