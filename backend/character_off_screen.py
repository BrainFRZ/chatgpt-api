"""Off-screen log builder.

Fires at session resume after a real-time gap > OFF_SCREEN_GAP_THRESHOLD_HOURS.
Reads `life_stream.jsonl` (the canonical "what actually happened" ledger
written by the daily resolver and the planner) and groups entries by date
into the days/events shape the [YOUR LIFE SINCE WE LAST TALKED] injection
already consumes.

Deterministic — no API call. The resolver already produced character-voice
summaries when it wrote life_stream entries; off-screen just slices them by
the gap-window's day_list and formats. Quiet days (skip-roll fired, no
unplanned moments rolled) produce an empty log, which is the *correct*
answer per the user-designed semantics: skip-roll = "uneventful, nothing
proactive to share." The character can still reference scheduled work
shifts (status=as_planned) and prior memories when asked.

Output is stored as a transient `off_screen_log` on the resume turn's
characters_state. Persists per-branch. Replaced on next gap.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from game_systems.characters import (
    OFF_SCREEN_EVENTS_PER_DAY,
    OFF_SCREEN_MAX_DAYS,
    OFF_SCREEN_GAP_THRESHOLD_HOURS,
    parse_iso_dt,
    now_et,
)

logger = logging.getLogger(__name__)


def _build_day_list(last_user_message_at_iso: Optional[str], now_dt: Optional[datetime] = None) -> list:
    """Build the list of day-windows for off-screen generation.

    Returns a list of {date, weekday, portion} entries. The "portion" field
    tells consumers what slice of that day is being represented:
    - "earlier today" — same-day gap (last message and now are the same date)
    - "after we talked" — last_date's remainder after the prior chat ended
    - "full" — a full intermediate day with no contact
    - "today before now" — current day up to the current moment

    Returns empty if the gap is below threshold.
    """
    if not last_user_message_at_iso:
        return []
    last = parse_iso_dt(last_user_message_at_iso)
    if not last:
        return []
    now = now_dt or now_et()

    gap_hours = (now - last).total_seconds() / 3600.0
    if gap_hours < OFF_SCREEN_GAP_THRESHOLD_HOURS:
        return []

    last_date = last.date()
    today = now.date()
    days: list = []

    if last_date == today:
        approx_hours = max(1, int(round(gap_hours)))
        days.append({
            "date": today.isoformat(),
            "weekday": today.strftime("%A"),
            "portion": f"earlier today (~{approx_hours} hours since you last talked)",
        })
        return days

    days.append({
        "date": last_date.isoformat(),
        "weekday": last_date.strftime("%A"),
        "portion": "after we talked",
    })
    cursor = last_date + timedelta(days=1)
    while cursor < today and len(days) < OFF_SCREEN_MAX_DAYS:
        days.append({
            "date": cursor.isoformat(),
            "weekday": cursor.strftime("%A"),
            "portion": "full",
        })
        cursor += timedelta(days=1)
    if len(days) < OFF_SCREEN_MAX_DAYS:
        days.append({
            "date": today.isoformat(),
            "weekday": today.strftime("%A"),
            "portion": "today before now",
        })
    return days


def should_generate_off_screen(wall_clock: dict, now_dt: Optional[datetime] = None) -> bool:
    """Returns True if the gap warrants off-screen generation (>= threshold)."""
    return len(_build_day_list((wall_clock or {}).get("last_user_message_at"), now_dt)) > 0


def generate_off_screen_log(
    client,  # kept in signature for callsite compatibility; unused
    project_dir: Optional[str],
    characters_state: dict,
    now_dt: Optional[datetime] = None,
) -> tuple[Optional[dict], dict]:
    """Build the off_screen_log from life_stream entries since last contact.

    Returns (log_dict, usage_dict). usage is always {} — this path makes no API
    calls. Returns (None, {}) when:
    - no gap (below threshold)
    - life_stream has no entries for any day in the gap window
    - project_dir is missing
    """
    wc = (characters_state or {}).get("wall_clock") or {}
    day_list = _build_day_list(wc.get("last_user_message_at"), now_dt)
    if not day_list or not project_dir:
        return None, {}

    log = _build_log_from_life_stream(project_dir, day_list, wc, now_dt=now_dt)
    if log is None:
        # Quiet gap — life_stream had nothing in the day-window. Returning
        # None means [YOUR LIFE SINCE WE LAST TALKED] won't surface this turn.
        # Per user-designed semantics: skip-roll is the resolver saying "nothing
        # to share"; we honor that here instead of inventing.
        logger.info(f"off_screen: life_stream empty for gap window ({len(day_list)} day(s)) — quiet period")
        return None, {}

    logger.info(f"off_screen: built log from life_stream ({len(log['days'])} day(s), no API call)")
    return log, {}


def _build_log_from_life_stream(
    project_dir: str,
    day_list: list,
    wc: dict,
    now_dt: Optional[datetime] = None,
) -> Optional[dict]:
    """Read life_stream entries since last contact, group by date matching the
    day_list, return the off-screen log shape. Returns None if no entries fall
    inside any day in day_list.
    """
    from character_schedule import read_life_stream_tail
    last_user_iso = wc.get("last_user_message_at")
    if not last_user_iso:
        return None

    entries = read_life_stream_tail(project_dir, since_iso=last_user_iso)
    if not entries:
        return None

    # Build a date → summaries map; align with the day_list dates
    by_date: dict[str, list[str]] = {}
    portion_by_date = {d["date"]: d.get("portion", "") for d in day_list}
    for e in entries:
        if not isinstance(e, dict):
            continue
        at_iso = e.get("at_local")
        if not isinstance(at_iso, str) or len(at_iso) < 10:
            continue
        date = at_iso[:10]
        if date not in portion_by_date:
            continue
        summary = (e.get("summary") or "").strip()
        if not summary:
            continue
        by_date.setdefault(date, []).append(summary)

    if not by_date:
        return None

    cleaned = []
    for d in day_list:
        date = d["date"]
        events = by_date.get(date, [])
        if not events:
            continue
        # Cap and dedupe (defensive against resolver double-firing on misfire-recovery)
        seen = set()
        kept = []
        for ev in events[:OFF_SCREEN_EVENTS_PER_DAY * 2]:
            if ev in seen:
                continue
            seen.add(ev)
            kept.append(ev[:240])
            if len(kept) >= OFF_SCREEN_EVENTS_PER_DAY:
                break
        if kept:
            cleaned.append({
                "date": date,
                "weekday": d.get("weekday") or "",
                "portion": d.get("portion", ""),
                "events": kept,
            })

    if not cleaned:
        return None
    return {
        "generated_at_iso": (now_dt or now_et()).isoformat(),
        "days": cleaned,
    }


# Compatibility shim for code paths that previously imported this. Off-screen
# no longer makes API calls so there's no usage to compute, but keeping the
# function avoids breaking callers in main.py / characters_runtime.py that
# imported it for the legacy path.
def compute_off_screen_cost(usage: dict) -> float:
    """Legacy: off-screen now makes no API call, so cost is always 0."""
    return 0.0
