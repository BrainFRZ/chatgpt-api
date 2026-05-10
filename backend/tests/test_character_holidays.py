"""Tests for character_holidays — pure date-arithmetic US holiday detection."""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from character_holidays import (  # noqa: E402
    _easter_sunday,
    _last_weekday,
    _nth_weekday,
    holiday_for_date,
    resolve_custom_holiday,
)


# ── Computus algorithm ────────────────────────────────────────────────────

class TestEasterComputus:
    """Cross-check Computus output against published Easter dates.

    Reference values from any Easter-date table — these are the actual
    Western Christian Easter Sundays for the given years.
    """

    def test_easter_2025(self):
        assert _easter_sunday(2025) == date(2025, 4, 20)

    def test_easter_2026(self):
        assert _easter_sunday(2026) == date(2026, 4, 5)

    def test_easter_2027(self):
        assert _easter_sunday(2027) == date(2027, 3, 28)

    def test_easter_2024(self):
        assert _easter_sunday(2024) == date(2024, 3, 31)

    def test_easter_2030(self):
        assert _easter_sunday(2030) == date(2030, 4, 21)

    def test_easter_falls_on_sunday(self):
        # Easter is by definition a Sunday — sanity check the algorithm
        for y in range(2020, 2050):
            d = _easter_sunday(y)
            assert d.weekday() == 6, f"Easter {y} on {d} (weekday={d.weekday()}) is not Sunday"


# ── Nth-weekday and last-weekday helpers ──────────────────────────────────

class TestNthWeekday:
    def test_first_monday_of_jan_2026(self):
        # Jan 1 2026 is Thursday; first Monday is Jan 5
        assert _nth_weekday(2026, 1, 0, 1) == date(2026, 1, 5)

    def test_third_monday_of_jan_2026(self):
        # MLK Day 2026
        assert _nth_weekday(2026, 1, 0, 3) == date(2026, 1, 19)

    def test_second_sunday_of_may_2026(self):
        # Mother's Day 2026
        assert _nth_weekday(2026, 5, 6, 2) == date(2026, 5, 10)

    def test_third_sunday_of_june_2026(self):
        # Father's Day 2026
        assert _nth_weekday(2026, 6, 6, 3) == date(2026, 6, 21)

    def test_fourth_thursday_of_nov_2026(self):
        # Thanksgiving 2026
        assert _nth_weekday(2026, 11, 3, 4) == date(2026, 11, 26)


class TestLastWeekday:
    def test_last_monday_of_may_2026(self):
        # Memorial Day 2026
        assert _last_weekday(2026, 5, 0) == date(2026, 5, 25)

    def test_last_monday_of_dec_2025(self):
        # End-of-year edge case: month=12 needs special handling for next-month rollover
        assert _last_weekday(2025, 12, 0) == date(2025, 12, 29)


# ── holiday_for_date end-to-end ───────────────────────────────────────────

class TestHolidayForDate:
    def test_mothers_day_2026(self):
        assert holiday_for_date(date(2026, 5, 10)) == "Mother's Day"

    def test_thanksgiving_2026(self):
        assert holiday_for_date(date(2026, 11, 26)) == "Thanksgiving"

    def test_black_friday_2026(self):
        assert holiday_for_date(date(2026, 11, 27)) == "Black Friday"

    def test_easter_2026(self):
        assert holiday_for_date(date(2026, 4, 5)) == "Easter"

    def test_good_friday_2026(self):
        assert holiday_for_date(date(2026, 4, 3)) == "Good Friday"

    def test_christmas(self):
        assert holiday_for_date(date(2025, 12, 25)) == "Christmas"
        assert holiday_for_date(date(2026, 12, 25)) == "Christmas"

    def test_christmas_eve(self):
        assert holiday_for_date(date(2025, 12, 24)) == "Christmas Eve"

    def test_new_years(self):
        assert holiday_for_date(date(2026, 1, 1)) == "New Year's Day"
        assert holiday_for_date(date(2025, 12, 31)) == "New Year's Eve"

    def test_valentines(self):
        assert holiday_for_date(date(2026, 2, 14)) == "Valentine's Day"

    def test_st_patricks(self):
        assert holiday_for_date(date(2026, 3, 17)) == "St. Patrick's Day"

    def test_independence_day(self):
        assert holiday_for_date(date(2026, 7, 4)) == "Independence Day"

    def test_halloween(self):
        assert holiday_for_date(date(2026, 10, 31)) == "Halloween"

    def test_veterans_day(self):
        assert holiday_for_date(date(2026, 11, 11)) == "Veterans Day"

    def test_mlk_day_2026(self):
        # 3rd Monday of January 2026
        assert holiday_for_date(date(2026, 1, 19)) == "Martin Luther King Jr. Day"

    def test_memorial_day_2026(self):
        # Last Monday of May 2026
        assert holiday_for_date(date(2026, 5, 25)) == "Memorial Day"

    def test_fathers_day_2026(self):
        # 3rd Sunday of June 2026
        assert holiday_for_date(date(2026, 6, 21)) == "Father's Day"

    def test_labor_day_2026(self):
        # 1st Monday of September 2026
        assert holiday_for_date(date(2026, 9, 7)) == "Labor Day"

    def test_random_non_holiday(self):
        assert holiday_for_date(date(2026, 5, 13)) is None
        assert holiday_for_date(date(2026, 8, 17)) is None
        assert holiday_for_date(date(2026, 6, 5)) is None

    def test_non_date_input_returns_none(self):
        assert holiday_for_date("2026-05-10") is None  # type: ignore
        assert holiday_for_date(None) is None  # type: ignore
        assert holiday_for_date(12345) is None  # type: ignore

    def test_easter_doesnt_collide_with_other_dates(self):
        # Make sure across many years, holidays don't double-up
        seen_pairs: list[tuple[date, str]] = []
        for y in range(2020, 2035):
            for month in range(1, 13):
                for day in (1, 5, 10, 15, 20, 25, 28):
                    try:
                        d = date(y, month, day)
                    except ValueError:
                        continue
                    name = holiday_for_date(d)
                    if name:
                        # Just ensures function doesn't crash; collisions
                        # would manifest as bugs in the order of checks
                        seen_pairs.append((d, name))
        # At minimum we should have detected Christmas, Mother's Day,
        # Thanksgiving for many years
        names_seen = {n for _, n in seen_pairs}
        assert "Christmas" in names_seen
        assert "Mother's Day" in names_seen
        assert "Thanksgiving" in names_seen


# ── Spot check a few well-known historical Easters ────────────────────────

class TestResolveCustomHoliday:
    """The resolver supports both fixed dates and named floating rules."""

    def test_fixed_date_form(self):
        entry = {"month": 9, "day": 6, "name": "x"}
        assert resolve_custom_holiday(entry, 2026) == date(2026, 9, 6)
        assert resolve_custom_holiday(entry, 2027) == date(2027, 9, 6)

    def test_day_after_labor_day_2016(self):
        # Labor Day 2016 = Mon Sept 5 → Tue Sept 6
        entry = {"rule": "day_after_labor_day", "name": "Brewing Day"}
        assert resolve_custom_holiday(entry, 2016) == date(2016, 9, 6)

    def test_day_after_labor_day_2026(self):
        # Labor Day 2026 = Mon Sept 7 → Tue Sept 8
        entry = {"rule": "day_after_labor_day", "name": "Brewing Day"}
        assert resolve_custom_holiday(entry, 2026) == date(2026, 9, 8)

    def test_day_after_labor_day_2027(self):
        # Labor Day 2027 = Mon Sept 6 → Tue Sept 7
        entry = {"rule": "day_after_labor_day", "name": "Brewing Day"}
        assert resolve_custom_holiday(entry, 2027) == date(2027, 9, 7)

    def test_brewing_day_always_falls_on_tuesday(self):
        # The whole point of the rule — Brewing Day is always a Tuesday
        entry = {"rule": "day_after_labor_day", "name": "Brewing Day"}
        for y in range(2016, 2050):
            d = resolve_custom_holiday(entry, y)
            assert d.weekday() == 1, f"Brewing Day {y} on {d} is weekday {d.weekday()}, not Tuesday(1)"

    def test_other_named_rules(self):
        # Quick sanity that the other rules resolve too
        assert resolve_custom_holiday({"rule": "easter", "name": "x"}, 2026) == date(2026, 4, 5)
        assert resolve_custom_holiday({"rule": "memorial_day", "name": "x"}, 2026) == date(2026, 5, 25)
        assert resolve_custom_holiday({"rule": "thanksgiving", "name": "x"}, 2026) == date(2026, 11, 26)
        assert resolve_custom_holiday({"rule": "labor_day", "name": "x"}, 2026) == date(2026, 9, 7)

    def test_unknown_rule_returns_none(self):
        assert resolve_custom_holiday({"rule": "not_a_real_rule", "name": "x"}, 2026) is None

    def test_malformed_returns_none(self):
        assert resolve_custom_holiday(None, 2026) is None  # type: ignore
        assert resolve_custom_holiday({}, 2026) is None
        assert resolve_custom_holiday({"name": "x"}, 2026) is None  # no rule, no fixed date
        assert resolve_custom_holiday({"month": "nope", "day": 6}, 2026) is None


class TestBuildHolidayInjection:
    """Integration: the injection function reads custom holidays from the
    state dict in addition to running the universal US holiday detector.
    """

    def _state_with(self, custom):
        return {"custom_holidays": custom}

    def test_universal_holiday_emits_block(self, monkeypatch):
        # Force "today" to a known holiday
        from datetime import datetime, timezone
        import game_systems.characters as ch
        # Mother's Day 2026 = May 10
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
        out = ch.build_holiday_injection({})
        assert "[HOLIDAY]" in out
        assert "Mother's Day" in out

    def test_custom_holiday_floating_rule_match(self, monkeypatch):
        # 2026: Labor Day = Mon Sept 7, Brewing Day = Tue Sept 8
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 9, 8, 8, 0, tzinfo=timezone.utc),
        )
        state = self._state_with([{"rule": "day_after_labor_day", "name": "The Back Booth's Brewing Day"}])
        out = ch.build_holiday_injection(state)
        assert "[HOLIDAY]" in out
        assert "Brewing Day" in out

    def test_custom_holiday_floating_no_match_on_old_fixed_date(self, monkeypatch):
        # In 2026 Sept 6 is a Sunday — Brewing Day is Sept 8, NOT Sept 6
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc),
        )
        state = self._state_with([{"rule": "day_after_labor_day", "name": "The Back Booth's Brewing Day"}])
        out = ch.build_holiday_injection(state)
        # Sept 6 2026 isn't any US holiday and isn't Brewing Day under the rule
        assert out == ""

    def test_user_level_calendar_emits(self, monkeypatch):
        # Shae's birthday Feb 19 — user-level, should fire in any character chat
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 2, 19, 8, 0, tzinfo=timezone.utc),
        )
        state = {
            "user_calendar": [{"month": 2, "day": 19, "name": "Shae's birthday"}],
        }
        out = ch.build_holiday_injection(state)
        assert "Shae's birthday" in out

    def test_user_level_AND_project_level_stack(self, monkeypatch):
        # If user-level AND project-level land on the same day, both surface
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 2, 19, 8, 0, tzinfo=timezone.utc),
        )
        state = {
            "custom_holidays": [{"month": 2, "day": 19, "name": "Some Project Anniversary"}],
            "user_calendar": [{"month": 2, "day": 19, "name": "Shae's birthday"}],
        }
        out = ch.build_holiday_injection(state)
        assert "Shae's birthday" in out
        assert "Some Project Anniversary" in out
        assert " AND " in out

    def test_fixed_custom_holiday_still_works(self, monkeypatch):
        # Fixed-date entries continue to work (backward-compat)
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        )
        state = self._state_with([{"month": 8, "day": 15, "name": "Some Anniversary"}])
        out = ch.build_holiday_injection(state)
        assert "Some Anniversary" in out

    def test_universal_AND_custom_stack(self, monkeypatch):
        from datetime import datetime, timezone
        import game_systems.characters as ch
        # Pretend Mother's Day 2026 + a custom holiday on the same day
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
        state = self._state_with([{"month": 5, "day": 10, "name": "Some Personal Anniversary"}])
        out = ch.build_holiday_injection(state)
        assert "Mother's Day" in out
        assert "Some Personal Anniversary" in out
        assert " AND " in out

    def test_no_holiday_no_custom_emits_nothing(self, monkeypatch):
        from datetime import datetime, timezone
        import game_systems.characters as ch
        # Random non-holiday Tuesday
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc),
        )
        out = ch.build_holiday_injection({})
        assert out == ""

    def test_malformed_custom_entries_ignored(self, monkeypatch):
        from datetime import datetime, timezone
        import game_systems.characters as ch
        monkeypatch.setattr(
            ch, "now_et",
            lambda: datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc),
        )
        state = self._state_with([
            "not a dict",
            {"month": "not int", "day": 6, "name": "x"},
            {"month": 9, "day": "not int", "name": "y"},
            {"month": 9, "day": 6},  # missing name
            {"month": 9, "day": 6, "name": "The Back Booth's Brewing Day"},
        ])
        out = ch.build_holiday_injection(state)
        assert "Brewing Day" in out
        # No malformed names leaked through
        assert "not int" not in out


class TestHistoricalEaster:
    def test_easter_2000(self):
        assert _easter_sunday(2000) == date(2000, 4, 23)

    def test_easter_2010(self):
        assert _easter_sunday(2010) == date(2010, 4, 4)

    def test_easter_2019(self):
        assert _easter_sunday(2019) == date(2019, 4, 21)
