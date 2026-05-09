"""Weekly planner — Claude Sonnet 4.6.

Fires Sunday 8pm ET via APScheduler (one job per Characters project) and
on-demand from interview-finalize for newly-created characters.

Per pass:
1. Load profile + user_life + state + last week's schedule + life_stream tail.
2. Roll major-life-event using roll_major_event (d100 + grace period). Returns
   None most weeks (~96.5%); a hit returns {magnitude, category, hint}.
3. Call Sonnet 4.6 once with all context + optional major-event hint, asks
   for next week's schedule. Backend rolls the major event; model only
   narrates how it threads into the week.
4. Carry-forward: events from the existing schedule whose when_local is
   AFTER next week's window survive (chat-added far-future plans). Past
   events drop (the resolver should have handled them).
5. Write new schedule.json with week_of=<next Monday>. Write a major_event
   entry to life_stream if the roll hit.
6. Update scheduler_state.last_planner_run_week_of and stamp first_seen_date
   if missing (anchors the grace period).

Cost: ~$0.01-0.02 per non-skipped pass. Once a week — comfortable.
"""
from __future__ import annotations

import logging
import os
import random
import uuid
from datetime import datetime, date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

PLANNER_MODEL = "claude-sonnet-4-6"
PLANNER_MAX_TOKENS = 4096
PLANNER_TIMEOUT_S = 90

SONNET_INPUT_RATE = 3.00
SONNET_CACHE_READ_RATE = 0.30
SONNET_CACHE_WRITE_RATE = 6.00
SONNET_OUTPUT_RATE = 15.00


def compute_planner_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * SONNET_INPUT_RATE
        + cache_read * SONNET_CACHE_READ_RATE
        + cache_write * SONNET_CACHE_WRITE_RATE
        + output * SONNET_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You generate next week's planned schedule for a specific character. The schedule is the spine of their continuous existence — daily resolver passes will narrate which planned events happened as planned, were modified, or got canceled.

You receive:
- Character profile (voice, life threads, weekly rhythms)
- User life context (so plans are consistent with what the user knows)
- Current state (mood band, callbacks, recent life_stream entries)
- Optional major-event hint (the backend rolled a magnitude/category/hint — weave it into ONE day naturally; do NOT change its category or invent a different event)

You produce:
- 8-15 daytime planned events covering the week — work shifts, social plans, family obligations, self-care, errands, anticipated quiet evenings
- 7 nightly sleep events (one per night) — kind="sleep", calibrated to the character's daily rhythm. These are scaffold so the backend knows when the character is asleep; they get auto-stamped without narration.
- Times in ISO 8601 with -04:00 offset (or -05:00 depending on the week — match the character's local timezone)
- Specific to THIS character. Lean on their canonical rhythms (e.g. cafe owner opens 6am, Sunday call with mom, Friday Shae night). Don't pad with generic "lunch" entries.

Sleep events:
- One per night, kind="sleep", title can just be "Sleep" or "Sleep — [night descriptor]"
- when_local = bedtime; duration_min = sleep duration (typically 360-540 = 6-9hr)
- Calibrate to the profile. Cafe owner up at 6am → bedtime ~10pm, ~7-8hr sleep. Sunday cafe closed → bedtime might shift later, sleep-in possible.
- Friday/Saturday nights might be later (social plans push bedtime back). Mid-week typical.
- Don't anticipation-tag sleep (always neutral or omit).

Hard rules:
- Do NOT change the rolled major event's category or magnitude. The hint tells you the kind; you generate the specific event from it. If category is "loss" and hint is "a death in their orbit", you don't decide it's a job change instead.
- Don't invent more than one major event per week. If the backend gave you no hint, the week is ordinary.
- Density: ~10 daytime events + 7 sleep events = ~17 typical. A pure-work week is realistic for some characters; a pure-social week is unrealistic. Mix.
- Times are realistic: cafe shifts are 8-10hr, social plans are 1-3hr, calls are 30-60min, sleep is 6-9hr.
- Anticipation: looking_forward / dreading / neutral. Use sparingly — most events are neutral.

Always call `report_week` exactly once."""


def build_planner_tool() -> dict:
    return {
        "name": "report_week",
        "description": "Emit the planned schedule for the upcoming week.",
        "input_schema": {
            "type": "object",
            "required": ["events"],
            "properties": {
                "events": {
                    "type": "array",
                    "description": "8-15 planned events for the week.",
                    "items": {
                        "type": "object",
                        "required": ["kind", "title", "when_local", "duration_min"],
                        "properties": {
                            "kind": {"type": "string", "enum": ["work", "social", "family", "self_care", "admin", "anticipated", "sleep"]},
                            "title": {"type": "string"},
                            "with": {"type": "array", "items": {"type": "string"}},
                            "when_local": {"type": "string", "description": "ISO 8601 with timezone offset"},
                            "duration_min": {"type": "integer", "minimum": 5},
                            "location": {"type": "string"},
                            "anticipation": {"type": "string", "enum": ["looking_forward", "dreading", "neutral"]},
                            "is_major_event": {"type": "boolean", "description": "Set true ONLY for the event derived from the rolled major-event hint."},
                            "resolver_hints": {"type": "string"},
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
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _next_monday(now_dt: datetime) -> date:
    """Monday of the week starting AFTER now_dt's week. The planner runs Sunday
    evening; "next Monday" is tomorrow if today is Sunday, or up to 7 days out."""
    today = now_dt.date()
    # weekday(): Mon=0, Sun=6. Days until next Monday = (7 - weekday()) % 7, with
    # today=Mon → 7 (we want NEXT Monday, not today).
    days_ahead = (7 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


def this_week_monday(now_dt: datetime) -> date:
    """Monday of the week CONTAINING now_dt. Used by finalize_interview to seed
    a brand-new character's initial week — covers Mon→Sun of the current week.
    Past parts of that week (e.g., Mon-Tue if today is Wed) get auto-stamped
    as_planned by the next resolver pass with empty resolution; the character
    has plausible "she had a Tuesday" continuity from day one.
    """
    today = now_dt.date()
    return today - timedelta(days=today.weekday())


def _carry_forward_events(
    existing_schedule: dict,
    week_start_dt: datetime,
    week_end_dt: datetime,
) -> list[dict]:
    """Salvage planned events OUTSIDE the new [week_start_dt, week_end_dt)
    window so the planner can be called repeatedly (e.g. finalize seeding
    two weeks back-to-back) without each call wiping the prior call's plans.

    Salvages:
    - Events BEFORE the window's start that are still status=planned
      (e.g. the prior week's plans being merged with a fresh future week)
    - Events past the window's end (chat-added far-future, prior planner
      runs that planned weeks further out)

    Drops:
    - Events INSIDE the new window — those are what the planner is about
      to generate fresh; old planner-run events for the same week get
      replaced.
    - Resolved/cancelled/modified events (status != planned) — their
      narratives live in life_stream, not the schedule.
    """
    salvaged: list[dict] = []
    if not isinstance(existing_schedule, dict):
        return salvaged
    for ev in existing_schedule.get("events") or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("status") != "planned":
            continue  # resolved/cancelled events drop
        when_str = ev.get("when_local")
        if not isinstance(when_str, str):
            continue
        try:
            ev_dt = datetime.fromisoformat(when_str)
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.astimezone()
        except (ValueError, TypeError):
            continue
        # Salvage if outside the new window in either direction
        if ev_dt < week_start_dt or ev_dt >= week_end_dt:
            salvaged.append(ev)
    return salvaged


def _format_life_stream_tail(entries: list[dict]) -> str:
    if not entries:
        return "(empty — first planner pass)"
    lines = []
    for e in entries[-20:]:  # cap at 20 most-recent for context
        if not isinstance(e, dict):
            continue
        ts = e.get("at_local", "?")
        kind = e.get("kind", "?")
        summary = e.get("summary", "")[:140]
        lines.append(f"  {ts} [{kind}] {summary}")
    return "\n".join(lines) if lines else "(empty)"


def run_weekly_planner(
    client,
    project_dir: str,
    *,
    now_dt: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
    week_of: Optional[date] = None,
) -> dict:
    """Run the weekly planner pass. Returns a meta dict for telemetry/logging.

    `week_of`: Monday of the week to plan. Defaults to next Monday (the
    Sunday-evening cron's natural target). Pass current-week's Monday from
    finalize_interview to seed the initial week immediately.

    Idempotent: if `last_planner_run_week_of` already matches `week_of`,
    bails without API call.
    """
    from character_schedule import (
        load_schedule, save_schedule, append_life_stream,
    )
    from character_project_state import load_project_state, save_project_state
    from game_systems.characters import roll_major_event, now_et

    if not project_dir or not os.path.isdir(project_dir):
        return {"error": "bad project_dir", "skipped": True}

    if now_dt is None:
        now_dt = now_et()
    if rng is None:
        rng = random.Random()

    state = load_project_state(project_dir) or {}
    sched_state = state.setdefault("scheduler_state", {})

    # First-ever run: stamp first_seen_date (anchors the major-event grace
    # period) but DO NOT bail — generate the week's schedule. roll_major_event
    # will return None during the grace window so no major event lands on
    # the new character; the rest of the week (work, social, etc.) still
    # gets planned. Without this, fresh characters had no schedule for 1+
    # weeks while the next cron firing produced one.
    if not sched_state.get("first_seen_date"):
        sched_state["first_seen_date"] = now_dt.date().isoformat()
        save_project_state(project_dir, state)
        logger.info(f"planner: stamped first_seen_date={sched_state['first_seen_date']} for {project_dir}")

    if week_of is None:
        # Default: plan the week TWO weeks from now (next_monday + 7), not the
        # immediately-next week. The Sunday cron pre-populates the week-after-
        # next while the immediately-next week is already filled by the prior
        # cron firing or the finalize-interview seed. This guarantees there's
        # always at least one full future week visible in the schedule, even
        # at the end of the current week — avoids the "I only have today
        # planned" gap on Sunday morning before the cron fires.
        week_of = _next_monday(now_dt) + timedelta(days=7)
    week_of_iso = week_of.isoformat()

    # Idempotence: bail if we already planned this week.
    if sched_state.get("last_planner_run_week_of") == week_of_iso:
        return {"skipped": True, "reason": "week already planned", "week_of": week_of_iso}

    # Roll the major event (returns None most weeks)
    today_iso = now_dt.date().isoformat()
    major_seed = roll_major_event(today_iso, sched_state.get("first_seen_date"), rng=rng)
    if major_seed:
        logger.info(f"planner: major event rolled — {major_seed['magnitude']}/{major_seed['category']}")

    # Existing schedule for carry-forward
    existing_schedule = load_schedule(project_dir)
    week_start_dt = datetime.combine(week_of, datetime.min.time()).astimezone()
    week_end_dt = week_start_dt + timedelta(days=7)
    salvaged = _carry_forward_events(existing_schedule or {}, week_start_dt, week_end_dt)

    # Sonnet call — generate next week's events
    profile_doc = _read_file(os.path.join(project_dir, "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir, "user_life.di"))

    payload, usage = _call_sonnet(
        client,
        profile_doc=profile_doc,
        user_life_doc=user_life_doc,
        state=state,
        project_dir=project_dir,
        week_of_iso=week_of_iso,
        major_seed=major_seed,
    )

    raw_events = payload.get("events") or []
    if not raw_events:
        logger.warning(f"planner: model returned no events for {project_dir}")
        return {
            "skipped": True, "reason": "no events from model",
            "week_of": week_of_iso, "usage": usage,
        }

    # Determine next event id. Two sources to consider:
    #   1. The previous schedule's next_id — even when zero events get
    #      salvaged, life_stream entries from the previous week reference
    #      ids like sch-1, sch-2... If we restart at 1 we collide.
    #   2. Max id from salvaged events (chat-added events with high ids).
    # Use the higher of the two so new events always get fresh ids.
    max_existing_id = 0
    for ev in salvaged:
        eid = ev.get("id", "")
        if isinstance(eid, str) and eid.startswith("sch-"):
            try:
                max_existing_id = max(max_existing_id, int(eid.split("-", 1)[1]))
            except (ValueError, IndexError):
                pass
    prev_next_id = 1
    if isinstance(existing_schedule, dict):
        try:
            prev_next_id = int(existing_schedule.get("next_id") or 1)
        except (ValueError, TypeError):
            prev_next_id = 1

    new_events: list[dict] = []
    next_id = max(max_existing_id + 1, prev_next_id)
    major_event_idx: Optional[int] = None
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        event = {
            "id": f"sch-{next_id}",
            "kind": raw.get("kind", "social"),
            "title": (raw.get("title") or "(untitled)")[:160],
            "with": raw.get("with") or [],
            "when_local": raw.get("when_local"),
            "duration_min": int(raw.get("duration_min") or 60),
            "location": (raw.get("location") or "")[:120],
            "anticipation": raw.get("anticipation") or "neutral",
            "magnitude": "major" if raw.get("is_major_event") and major_seed else "normal",
            "resolver_hints": (raw.get("resolver_hints") or "")[:300],
            "status": "planned",
            "resolution": None,
            "revision_history": [{
                "at": now_dt.isoformat(timespec="seconds"),
                "by": "planner",
                "reason": f"weekly plan for {week_of_iso}",
                "diff": {"status": {"from": None, "to": "planned"}},
            }],
        }
        new_events.append(event)
        if event["magnitude"] == "major" and major_event_idx is None:
            major_event_idx = len(new_events) - 1
        next_id += 1

    # If a major event was rolled but the model didn't tag any event, force-tag
    # the first one (defensive; the prompt instructs it but defensive anyway).
    if major_seed and major_event_idx is None and new_events:
        new_events[0]["magnitude"] = "major"
        major_event_idx = 0
        logger.warning("planner: model didn't tag a major event despite hint — promoted first event")

    # Build new schedule (new + salvaged future events)
    new_schedule = {
        "week_of": week_of_iso,
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "next_id": next_id + len(salvaged),
        "events": new_events + salvaged,
    }
    save_schedule(project_dir, new_schedule)

    # If a major event was rolled, write a life_stream entry too so recall can
    # surface "something is going on this week" before the actual day arrives.
    if major_seed and major_event_idx is not None:
        major_ev = new_events[major_event_idx]
        ls_iso = now_dt.isoformat(timespec="seconds")
        append_life_stream(project_dir, {
            "id": f"ls-{ls_iso[:10]}-major-{uuid.uuid4().hex[:6]}",
            "at_local": ls_iso,
            "kind": "major_event",
            "ref": major_ev["id"],
            "summary": f"Coming up this week: {major_ev['title']}",
            "tone": "even",
            "available_to_recall": True,
            "magnitude": major_seed["magnitude"],
            "category": major_seed["category"],
        })
        logger.info(f"planner: wrote major_event life_stream entry referencing {major_ev['id']}")

    # Stamp scheduler_state
    sched_state["last_planner_run_week_of"] = week_of_iso
    save_project_state(project_dir, state)

    cost = compute_planner_cost(usage) if usage else 0.0
    return {
        "skipped": False,
        "week_of": week_of_iso,
        "events_planned": len(new_events),
        "events_carried_forward": len(salvaged),
        "major_event": bool(major_seed),
        "major_seed": major_seed,
        "usage": usage,
        "cost": cost,
        "model": PLANNER_MODEL,
    }


def _call_sonnet(
    client,
    *,
    profile_doc: str,
    user_life_doc: str,
    state: dict,
    project_dir: str,
    week_of_iso: str,
    major_seed: Optional[dict],
) -> tuple[dict, dict]:
    """Single Sonnet 4.6 call. Returns (parsed_payload, usage). On error: ({}, {})."""
    from character_schedule import read_life_stream_tail

    stable_parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        stable_parts += ["[USER LIFE — context]", user_life_doc, ""]
    stable_text = "\n".join(stable_parts)

    parts = []
    parts.append(f"[GENERATING WEEK] {week_of_iso} (Monday) through Sunday")
    parts.append("")

    wb = state.get("wellbeing") or {}
    parts.append(f"[CURRENT MOOD] {wb.get('state', 'Even')} (wb_mod for tomorrow's roll: {wb.get('wb_mod', 0)})")
    arc = state.get("arc_state") or ""
    if arc:
        parts.append(f"[ARC] {arc}")

    cbs = (state.get("callbacks") or {}).get("open") or []
    if cbs:
        parts.append("[OPEN CALLBACKS — threads pulling at the character]")
        for cb in cbs[:8]:
            if isinstance(cb, dict):
                parts.append(f"  #{cb.get('id')} {cb.get('original_text', '')[:140]}")

    # Recent life_stream tail (last 2 weeks for context — what's been happening)
    parts.append("")
    parts.append("[RECENT LIFE — last 2 weeks of context, do not repeat]")
    from datetime import datetime as _dt, timedelta as _td
    since_iso = (_dt.now().astimezone() - _td(days=14)).isoformat(timespec="seconds")
    life_tail = read_life_stream_tail(project_dir, since_iso=since_iso)
    parts.append(_format_life_stream_tail(life_tail))
    parts.append("")

    if major_seed:
        parts.append(
            f"[MAJOR EVENT THIS WEEK — backend roll]\n"
            f"  magnitude: {major_seed['magnitude']}\n"
            f"  category: {major_seed['category']}\n"
            f"  hint: {major_seed['hint']}\n"
            f"  → Generate the specific event from this hint, calibrated to the character's life. "
            f"Tag the event with `is_major_event: true`. Place it on a believable day. "
            f"DO NOT change the category or invent a different kind of event."
        )
        parts.append("")
    else:
        parts.append("[NO MAJOR EVENT THIS WEEK] — generate ordinary weekly rhythms.")
        parts.append("")

    parts.append(f"Generate the planned events for the week starting {week_of_iso}. Call report_week exactly once.")
    volatile_text = "\n".join(parts)

    tool = build_planner_tool()
    try:
        response = client.messages.create(
            model=PLANNER_MODEL,
            max_tokens=PLANNER_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stable_text,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                    {
                        "type": "text",
                        "text": volatile_text,
                    },
                ],
            }],
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_week"},
            timeout=PLANNER_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"planner: API call failed: {type(e).__name__}: {e}")
        return {}, {}

    payload: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_week":
            inp = block.input
            if isinstance(inp, dict):
                payload = inp
            break

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens
        + (getattr(ru, "cache_read_input_tokens", 0) or 0)
        + (getattr(ru, "cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": getattr(ru, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(ru, "cache_creation_input_tokens", 0) or 0,
        "output_tokens": ru.output_tokens,
    }
    return payload, usage
