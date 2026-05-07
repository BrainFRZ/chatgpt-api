"""Off-screen life generator — Claude Opus 4.5.

Fires at session resume after a real-time gap > OFF_SCREEN_GAP_THRESHOLD_HOURS.
Produces 1-3 events per real day in the gap, day-by-day, drawing on (but not
exclusive to) the character's memories and current life threads.

Output is stored as a transient `off_screen_log` on the resume turn's
characters_state. Persists per-branch. Is replaced on the next gap.

Why Opus 4.5 (not Haiku): voice texture matters here. These events surface
later in the conversation as things the character casually shares — the
specifics of "Tuesday I read a book" → "Nora began reading LotR on Tuesday"
need to land in the character's voice. Haiku is too generic. The cost is one
call per session-resume — small budget, high leverage.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from game_systems.characters import (
    OFF_SCREEN_EVENTS_PER_DAY,
    OFF_SCREEN_MAX_DAYS,
    OFF_SCREEN_GAP_THRESHOLD_HOURS,
    TZ,
    parse_iso_dt,
    now_et,
)

logger = logging.getLogger(__name__)

OFF_SCREEN_MODEL = "claude-opus-4-5"
OFF_SCREEN_MAX_TOKENS = 2048
OFF_SCREEN_TIMEOUT_S = 30

OPUS_45_INPUT_RATE = 15.00
OPUS_45_CACHE_READ_RATE = 1.50
OPUS_45_CACHE_WRITE_RATE = 18.75
OPUS_45_OUTPUT_RATE = 75.00


def compute_off_screen_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * OPUS_45_INPUT_RATE
        + cache_read * OPUS_45_CACHE_READ_RATE
        + cache_write * OPUS_45_CACHE_WRITE_RATE
        + output * OPUS_45_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You generate "what the character did during the gap" — the off-screen life of a specific person between conversations with the user.

You receive:
- The character profile (voice, life, current threads)
- Optional user_life context (so you don't generate things that contradict what the user is doing)
- Memory + callback context (for inspiration, not constraint)
- The list of real-life days in the gap (with weekdays)

You produce: 1-3 events per day-window, in this character's voice, that feel lived-in and ordinary or quietly notable. Some windows will be mundane (groceries, work). Some will have a small specific thing (saw an old friend; finished a book; had a weird dream). Most windows are NOT plot — they're texture. The character is a person with a life; this is that life happening.

The day-windows you receive may be FULL DAYS (no contact for a 24h+ stretch) or PARTIAL WINDOWS:
- "earlier today (~N hours since you last talked)" — same-day return, just a few hours of activity
- "after we talked" — the rest of the day after a previous chat ended (evening, bedtime, late thoughts)
- "today before now" — daytime activity before the user opened the chat tonight (work, lunch, errands)

Calibrate event count to window size: partial windows get 1-2 events; full days get 1-3. A 3-hour window shouldn't have 3 separate events — that's too dense for that span.

PRINCIPLES:
- SPECIFIC over generic. "Read a book" is weak. "Started LotR finally — only made it to the Council of Elrond" is what we want.
- ORDINARY most of the time. Most windows have ordinary content. A grand event every day flattens reality.
- CONSISTENT with the character's life. If they live in Portland, don't have them at a beach in California. If they hate cats, don't add a cat.
- DON'T resolve open callbacks here. Those resolve in conversation, not behind the scenes.
- DON'T spoil. Don't pre-commit to information the user hasn't asked for in a way that pre-empts conversation.
- CALIBRATE TO WINDOW. "earlier today" gets bedtime-adjacent or workday content depending on time of day; "after we talked" gets evening-into-night content; "today before now" gets daytime content.
- CALL OUT WEEKDAY rhythms. If the character has yoga Wednesdays, Wednesday should reflect that.
- VARY texture. If one window had a specific food, the next shouldn't also be food-themed. Mix sensory details, social, work, internal.

You will be asked to call `report_off_screen_life` once with the structured day list."""


def build_off_screen_tool(day_list: list) -> dict:
    """The day_list is the calendar of dates we're filling in (we tell the model which days to populate)."""
    return {
        "name": "report_off_screen_life",
        "description": "Emit the character's off-screen life for the gap days, in order.",
        "input_schema": {
            "type": "object",
            "required": ["days"],
            "properties": {
                "days": {
                    "type": "array",
                    "description": f"One entry per day in the gap, in chronological order ({len(day_list)} day(s) expected: {[d['date'] for d in day_list]}).",
                    "items": {
                        "type": "object",
                        "required": ["date", "weekday", "events"],
                        "properties": {
                            "date": {"type": "string", "description": "ISO date (YYYY-MM-DD). MUST match one of the requested days."},
                            "weekday": {"type": "string", "description": "Day of week (Monday, Tuesday, ...)."},
                            "events": {
                                "type": "array",
                                "description": f"1-{OFF_SCREEN_EVENTS_PER_DAY} short specific things that happened. In the character's voice, but not first-person — e.g. 'Started LotR — only made it to the Council of Elrond.'",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }


def _read_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ""


def _build_day_list(last_user_message_at_iso: Optional[str], now_dt: Optional[datetime] = None) -> list:
    """Build the list of day-windows for off-screen generation.

    Returns a list of {date, weekday, portion} entries. The "portion" field
    tells the agent what slice of that day to cover:
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
        # Intra-day gap: a single partial entry for the hours since last contact
        approx_hours = max(1, int(round(gap_hours)))
        days.append({
            "date": today.isoformat(),
            "weekday": today.strftime("%A"),
            "portion": f"earlier today (~{approx_hours} hours since you last talked)",
        })
        return days

    # Cross-day gap: rest-of-last-date + full intermediate days + today-up-to-now
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
    client,
    project_dir: Optional[str],
    characters_state: dict,
    now_dt: Optional[datetime] = None,
) -> tuple[Optional[dict], dict]:
    """Generate the off_screen_log for the current gap. Returns (log_dict, usage_dict).

    Returns (None, {}) when no full days to fill or on error.
    """
    wc = (characters_state or {}).get("wall_clock") or {}
    day_list = _build_day_list(wc.get("last_user_message_at"), now_dt)
    if not day_list:
        return None, {}

    profile_doc = _read_file(os.path.join(project_dir or "", "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir or "", "user_life.di"))

    # Lightweight context for inspiration — memories + arc + current threads
    parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        parts += ["[USER LIFE — for consistency, do not contradict]", user_life_doc, ""]

    arc = (characters_state or {}).get("arc_state") or ""
    if arc:
        parts += [f"[CURRENT ARC] {arc}", ""]

    mems = (characters_state or {}).get("character_memories") or []
    if mems:
        parts.append("[RECENT MEMORIES — for inspiration, not direct reuse]")
        for m in mems[:8]:
            if isinstance(m, dict):
                parts.append(f"  - ({m.get('impact', '?')}★) {m.get('text', '')[:160]}")
        parts.append("")

    # Life-event seed: a weekly auto-roll (or manual /seed-event) may have planted
    # a directive about something happening to the character this week. Weave it
    # into ONE of the days naturally — don't make every day about it. The seed is
    # a hint about kind+magnitude, not a script; generate the specific event in
    # character.
    pending_seed = ((characters_state or {}).get("life_events") or {}).get("pending_seed")
    seed_consumed = False
    if isinstance(pending_seed, dict) and pending_seed.get("hint"):
        magnitude = pending_seed.get("magnitude") or "moderate"
        category = pending_seed.get("category") or "?"
        hint = pending_seed.get("hint")
        source = pending_seed.get("source") or "auto"
        parts += [
            f"[LIFE EVENT SEED — magnitude={magnitude}, category={category}, source={source}]",
            f"Hint: {hint}",
            (
                "Weave this into ONE of the gap days as ONE of that day's events — not all days, not all events on that day. "
                "Generate the specific event from this hint using the character profile (so it's plausibly something that happens to THIS person — calibrate to their life, relationships, occupation). "
                "Other days remain ordinary. The character will reference the event naturally in conversation when the user opens up the chat."
                + (" This is a USER-AUTHORED seed — the user wants this specific event to happen, so honor it directly." if source == "manual" else "")
            ),
            "",
        ]
        seed_consumed = True  # we'll mark it consumed in state if generation succeeds

    # Render the gap as portion-aware day-windows. Partial windows get fewer
    # events than full days — the model should calibrate event count to how
    # much time the window actually covers.
    portion_lines = []
    for d in day_list:
        portion = d.get("portion", "full")
        if portion == "full":
            portion_lines.append(f"  - {d['weekday']}, {d['date']} — full day (1-3 events)")
        elif portion.startswith("earlier today"):
            portion_lines.append(f"  - {d['weekday']}, {d['date']} — {portion} (1-2 events; just what they did in those hours)")
        elif portion == "after we talked":
            portion_lines.append(f"  - {d['weekday']}, {d['date']} — after the previous chat ended (1-2 events; rest of that day)")
        elif portion == "today before now":
            portion_lines.append(f"  - {d['weekday']}, {d['date']} — today up to now (1-2 events; what they did today before you opened the chat)")
        else:
            portion_lines.append(f"  - {d['weekday']}, {d['date']} — {portion}")

    parts += [
        f"[GAP] {len(day_list)} day-window(s) to fill in:",
        *portion_lines,
        "",
        (
            "Generate events for each window, ordered chronologically. Calibrate the count to "
            "the window size — partial windows (\"earlier today\", \"after we talked\", \"today before now\") "
            "get 1-2 events; full days get 1-3. Most events should be ordinary — groceries, work, a "
            "small annoyance, a song stuck in their head. Specific details matter more than 'big' "
            "content. Call report_off_screen_life with one entry per window. The `date` field MUST be "
            "the ISO date string from the list above (multiple windows on the same date — e.g. "
            "\"earlier today\" and \"today before now\" on the same calendar day — never happen since "
            "those are mutually exclusive cases)."
        ),
    ]

    user_msg = "\n".join(parts)
    tool = build_off_screen_tool(day_list)

    try:
        response = client.messages.create(
            model=OFF_SCREEN_MODEL,
            max_tokens=OFF_SCREEN_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_off_screen_life"},
            timeout=OFF_SCREEN_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"off_screen_agent: API call failed: {type(e).__name__}: {e}")
        return None, {}

    days_payload = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_off_screen_life":
            inp = block.input
            if isinstance(inp, dict):
                raw_days = inp.get("days")
                if isinstance(raw_days, list):
                    days_payload = raw_days
            break

    # Validate / clamp. Map portions from day_list (date-keyed) onto cleaned entries
    # so the correspondence model's [YOUR LIFE SINCE WE LAST TALKED] injection can
    # show "Tuesday — after we talked" vs "Tuesday — full day" vs "today before now."
    portion_by_date = {d["date"]: d.get("portion", "") for d in day_list}
    cleaned = []
    for entry in days_payload:
        if not isinstance(entry, dict):
            continue
        date = entry.get("date")
        if date not in portion_by_date:
            continue
        events = entry.get("events") or []
        if not isinstance(events, list):
            continue
        events = [str(e)[:240] for e in events if isinstance(e, str) and e.strip()][:OFF_SCREEN_EVENTS_PER_DAY]
        if not events:
            continue
        cleaned.append({
            "date": date,
            "weekday": entry.get("weekday") or "",
            "portion": portion_by_date.get(date, ""),
            "events": events,
        })

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens
        + (getattr(ru, 'cache_read_input_tokens', 0) or 0)
        + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }

    if not cleaned:
        return None, usage

    log = {
        "generated_at_iso": (now_dt or now_et()).isoformat(),
        "days": cleaned,
    }
    # If a seed was injected into the prompt and generation succeeded, mark it
    # consumed so it doesn't get woven in again on the next session-resume.
    if seed_consumed:
        from game_systems.characters import consume_pending_event_seed
        try:
            consumed = consume_pending_event_seed(
                (characters_state or {}).get("life_events") or {},
                (now_dt or now_et()).date().isoformat(),
            )
            if consumed:
                log["consumed_seed"] = consumed
                logger.info(f"off_screen_agent: consumed seed {consumed.get('magnitude')}/{consumed.get('category')}")
        except Exception as e:
            logger.warning(f"off_screen_agent: seed consume failed: {e}")
    logger.info(f"off_screen_agent: generated {len(cleaned)} day(s) of off-screen life")
    return log, usage
