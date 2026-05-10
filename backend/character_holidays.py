"""US holiday detection — pure date arithmetic, no API.

Used by game_systems/characters.py's build_holiday_injection to surface
'[HOLIDAY] Today is Mother's Day.' style context to the voice model on
holiday turns. Empty injection on non-holiday days (so the system prompt
stays lean).

Why pure code instead of an API:
- Microsecond-fast, no network, no rate limits, no failure modes
- All US floaters reduce to 'Nth weekday of month' or 'last weekday of
  month' rules. Easter (and the holidays anchored to it) reduce to the
  Computus algorithm — well-established since the 8th century.
- No cache needed; compute on every turn that asks.

Holidays surfaced — focused on culturally significant ones Shae might
care about, not government-only observances:
  - New Year's Day, New Year's Eve
  - Valentine's Day
  - Martin Luther King Jr. Day
  - St. Patrick's Day
  - Easter, Good Friday
  - Mother's Day
  - Memorial Day
  - Father's Day
  - Independence Day
  - Halloween
  - Veterans Day
  - Thanksgiving, Black Friday
  - Christmas Eve, Christmas

Adjust the list per character — Diwali, Lunar New Year, Hanukkah, etc.
could be added if a character would care about them.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm (aka 'Meeus/Jones/Butcher') for
    Western Easter Sunday. Deterministic for any 4-digit year.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Nth occurrence of `weekday` in `month`/`year`. weekday: 0=Monday,
    6=Sunday (Python's date.weekday() convention). n: 1-5.
    """
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    first_occurrence = first + timedelta(days=offset)
    return first_occurrence + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Last occurrence of `weekday` in `month`/`year`. weekday: 0=Monday."""
    if month == 12:
        first_of_next = date(year + 1, 1, 1)
    else:
        first_of_next = date(year, month + 1, 1)
    last_day = first_of_next - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


# Fixed-date holidays — (month, day) → name
_FIXED_HOLIDAYS = {
    (1, 1):   "New Year's Day",
    (2, 14):  "Valentine's Day",
    (3, 17):  "St. Patrick's Day",
    (7, 4):   "Independence Day",
    (10, 31): "Halloween",
    (11, 11): "Veterans Day",
    (12, 24): "Christmas Eve",
    (12, 25): "Christmas",
    (12, 31): "New Year's Eve",
}


def resolve_custom_holiday(entry: dict, year: int) -> Optional[date]:
    """Resolve a custom_holidays entry to its date for the given year.

    Two supported forms — entries can use either:
      - Fixed: {"month": int, "day": int, "name": str}
      - Floating rule: {"rule": "named_rule", "name": str}

    Named rules currently supported:
      - "day_after_labor_day": Tue after 1st Mon of Sept
      - "day_after_thanksgiving": Fri after 4th Thu of Nov (= Black Friday)
      - "good_friday": Easter - 2 days
      - "easter": Easter Sunday for the year
      - "memorial_day": Last Mon of May
      - "mothers_day": 2nd Sun of May
      - "fathers_day": 3rd Sun of Jun
      - "labor_day": 1st Mon of Sep
      - "thanksgiving": 4th Thu of Nov

    Returns None for malformed entries (silently skipped by callers).
    """
    if not isinstance(entry, dict):
        return None

    rule = entry.get("rule")
    if isinstance(rule, str) and rule.strip():
        return _resolve_named_rule(rule.strip().lower(), year)

    # Fixed-date form
    try:
        m = int(entry.get("month"))
        d = int(entry.get("day"))
        return date(year, m, d)
    except (TypeError, ValueError):
        return None


def _resolve_named_rule(name: str, year: int) -> Optional[date]:
    if name == "day_after_labor_day":
        return _nth_weekday(year, 9, 0, 1) + timedelta(days=1)
    if name == "day_after_thanksgiving" or name == "black_friday":
        return _nth_weekday(year, 11, 3, 4) + timedelta(days=1)
    if name == "easter":
        return _easter_sunday(year)
    if name == "good_friday":
        return _easter_sunday(year) - timedelta(days=2)
    if name == "memorial_day":
        return _last_weekday(year, 5, 0)
    if name == "mothers_day":
        return _nth_weekday(year, 5, 6, 2)
    if name == "fathers_day":
        return _nth_weekday(year, 6, 6, 3)
    if name == "labor_day":
        return _nth_weekday(year, 9, 0, 1)
    if name == "thanksgiving":
        return _nth_weekday(year, 11, 3, 4)
    return None


def holiday_for_date(d: date) -> Optional[str]:
    """Return the US holiday name for date `d`, or None if it's not a
    recognized holiday. First match wins; we order to prefer the more
    distinctive name when overlaps could happen (none currently do).
    """
    if not isinstance(d, date):
        return None
    y = d.year

    # Fixed dates
    fixed_name = _FIXED_HOLIDAYS.get((d.month, d.day))
    if fixed_name:
        return fixed_name

    # Floating Nth-weekday-of-month
    # (year, month, weekday[0=Mon..6=Sun], n)
    if d == _nth_weekday(y, 1, 0, 3):
        return "Martin Luther King Jr. Day"
    if d == _nth_weekday(y, 5, 6, 2):
        return "Mother's Day"
    if d == _last_weekday(y, 5, 0):
        return "Memorial Day"
    if d == _nth_weekday(y, 6, 6, 3):
        return "Father's Day"
    if d == _nth_weekday(y, 9, 0, 1):
        return "Labor Day"

    # Thanksgiving + Black Friday
    thanksgiving = _nth_weekday(y, 11, 3, 4)  # 4th Thursday Nov
    if d == thanksgiving:
        return "Thanksgiving"
    if d == thanksgiving + timedelta(days=1):
        return "Black Friday"

    # Easter family (Computus-derived)
    easter = _easter_sunday(y)
    if d == easter:
        return "Easter"
    if d == easter - timedelta(days=2):
        return "Good Friday"

    return None
