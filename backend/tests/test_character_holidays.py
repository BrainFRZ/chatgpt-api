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

class TestHistoricalEaster:
    def test_easter_2000(self):
        assert _easter_sunday(2000) == date(2000, 4, 23)

    def test_easter_2010(self):
        assert _easter_sunday(2010) == date(2010, 4, 4)

    def test_easter_2019(self):
        assert _easter_sunday(2019) == date(2019, 4, 21)
