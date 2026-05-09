"""Daily resolver — Claude Sonnet 4.6.

Fires on cron (8am/1pm/6pm ET via APScheduler) and on-demand from session-resume
when the user opens a chat after a long enough gap that the resolver hasn't
covered yet.

For each pass:
1. Find planned events whose window has elapsed since the last resolver run.
2. Skip-roll: with probability SKIP_RATE, auto-stamp all elapsed as as_planned
   with empty resolution (no API call). Override: skip is forbidden if any
   elapsed event has a chat-driven revision since last_resolver_run_at — the
   character must acknowledge changes that came through dialogue.
3. Otherwise: roll outcome bucket per event (as_planned / modified / cancelled)
   via roll_event_outcome (real RNG, mood-weighted bands). Roll unplanned-moment
   count via Poisson(λ=1.5) capped 0-3.
4. Single Sonnet 4.6 call. Backend hands the model the rolled buckets; model
   only narrates them (it cannot pick a different outcome). Model also generates
   the unplanned moments.
5. Apply: stamp resolutions on schedule.json (status flips from planned →
   outcome). Append life_stream entries for resolutions and unplanned moments.
   Update wellbeing.wb_mod from summed mood_deltas (clamped to ±WB_MOD_CAP).
   Stamp scheduler_state.last_resolver_run_at.

Cost: ~$0.005-0.015 per non-skipped pass. With 3 daily resolver runs and
typical skip rate ~35%, ~$0.02-0.03/day per character.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import uuid
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

RESOLVER_MODEL = "claude-sonnet-4-6"
RESOLVER_MAX_TOKENS = 3072
RESOLVER_TIMEOUT_S = 60

# Sonnet 4.6 pricing per MTok ($/1M tokens).
SONNET_INPUT_RATE = 3.00
SONNET_CACHE_READ_RATE = 0.30
SONNET_CACHE_WRITE_RATE = 6.00
SONNET_OUTPUT_RATE = 15.00

# Resolver dice constants
SKIP_RATE = 0.35                # P(skip-roll) when no chat-driven mutations in window
UNPLANNED_LAMBDA = 1.5          # Poisson mean for unplanned moments per pass
UNPLANNED_MAX = 3               # Cap on unplanned moments per pass
WB_MOD_CAP = 2                  # ±2 cumulative cap, mirrors apply_wb_mod_ops

LIFE_STREAM_TAIL_HOURS = 48     # Recent life_stream window passed to Sonnet for context

VALID_TONES = ("excellent", "buoyant", "even", "frayed", "rough")
VALID_UNPLANNED_KINDS = ("thought", "call", "incident", "small_thing", "observation")


def compute_resolver_cost(usage: dict) -> float:
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


SYSTEM_PROMPT = """You are narrating what just happened in a character's life since the last resolver pass. You are NOT picking outcomes — those are already rolled by the backend dice. You ONLY write what the rolled outcomes look like in this character's specific voice.

For each event in [ELAPSED EVENTS]:
- The OUTCOME is given (as_planned | modified | cancelled). Honor it exactly. Do NOT change it.
- Write `what_happened` in 1-2 sentences in third person, specific to THIS character (not generic).
  - as_planned: what unfolded, anything notable about THIS instance, or just that it happened normally
  - modified: HOW it differed (later, shorter, different company, swapped venue) and why
  - cancelled: WHY it didn't happen (their fault, someone else's, an external thing)
- Tag `how_it_went` with one of: excellent | buoyant | even | frayed | rough — the character's emotional weather AFTER this event.
- `mood_delta` is an integer -2..+2 — how this event nudges their mood toward tomorrow's roll. Most events are 0. Use ±1 for noticeable wins/losses, ±2 only for genuinely big.

Generate N unplanned moments where the backend told you to (might be 0). Each is a small lived moment in the window since the last pass — a thought, a phone call, a small incident, a customer interaction, an observation. Specific to THIS character, drawing on:
- Their profile (work / friendships / family / current threads)
- Recent life_stream context (don't repeat what's already there; build on it)
- Their current mood band

Hard rules:
- Restraint. Most resolutions have minimal `what_happened` — life is mostly ordinary. Reserve texture for events that earn it.
- Specificity. "She had a coffee" is too generic. "Marcus came in at 8:30 and lingered through her second pour" is the bar.
- Voice. Write IN THE CHARACTER'S register — what would a friend's friend say happened to her? Not narrator omniscience.
- Don't invent plot. Don't introduce dramatic events the planner didn't seed.

Always call `report_resolver_pass` exactly once."""


def build_resolver_tool() -> dict:
    return {
        "name": "report_resolver_pass",
        "description": "Emit narrative for the rolled resolutions and unplanned moments.",
        "input_schema": {
            "type": "object",
            "required": ["resolutions", "unplanned"],
            "properties": {
                "resolutions": {
                    "type": "array",
                    "description": "One entry per event in [ELAPSED EVENTS]. Outcomes are pre-rolled — DO NOT change them.",
                    "items": {
                        "type": "object",
                        "required": ["event_id", "outcome", "what_happened"],
                        "properties": {
                            "event_id": {"type": "string"},
                            "outcome": {"type": "string", "enum": ["as_planned", "modified", "cancelled"]},
                            "what_happened": {"type": "string", "description": "1-2 sentences in third person describing what unfolded (or didn't)."},
                            "how_it_went": {"type": "string", "enum": list(VALID_TONES)},
                            "mood_delta": {"type": "integer", "minimum": -2, "maximum": 2},
                        },
                    },
                },
                "unplanned": {
                    "type": "array",
                    "description": "Generate exactly the number of unplanned moments the backend asked for in [UNPLANNED COUNT]. May be 0.",
                    "items": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {
                            "summary": {"type": "string", "description": "1 sentence — what happened, said, or was thought."},
                            "tone": {"type": "string", "enum": list(VALID_TONES)},
                            "kind": {"type": "string", "enum": list(VALID_UNPLANNED_KINDS)},
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


def _poisson(lam: float, rng: random.Random) -> int:
    """Knuth's Poisson sampler — fine for small λ."""
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def _ls_id(at_iso: str) -> str:
    date_part = at_iso[:10] if isinstance(at_iso, str) and len(at_iso) >= 10 else "0000-00-00"
    return f"ls-{date_part}-{uuid.uuid4().hex[:6]}"


def _thin_ls_entry(ev: dict, at_iso: str) -> dict:
    """Build a minimal life_stream entry for an elapsed event when no model
    narration is available (skip-roll or model-failed-to-narrate fallback).
    Summary derives from the event title; the entry confirms the event
    happened and is recall-able even without a narrative.
    """
    return {
        "id": _ls_id(at_iso),
        "at_local": at_iso,
        "kind": "resolved_planned",
        "ref": ev.get("id"),
        "summary": ev.get("title") or "(scheduled event)",
        "tone": "even",
        "available_to_recall": True,
    }


def _event_end_iso(ev: dict, fallback_dt: datetime) -> str:
    """Compute an event's end timestamp from when_local + duration_min.
    Falls back to fallback_dt's iso string if the event's timing is
    unparseable (defensive — should always parse for resolver input)."""
    try:
        ev_start = datetime.fromisoformat(ev["when_local"])
        if ev_start.tzinfo is None:
            ev_start = ev_start.astimezone()
        ev_end = ev_start + timedelta(minutes=int(ev.get("duration_min") or 60))
        return ev_end.isoformat(timespec="seconds")
    except (ValueError, TypeError, KeyError):
        return fallback_dt.isoformat(timespec="seconds")


def _spread_unplanned_times(window_start: datetime, window_end: datetime, n: int) -> list[datetime]:
    """Distribute n unplanned-moment timestamps evenly across [window_start, window_end]."""
    if n <= 0 or window_end <= window_start:
        return []
    span = (window_end - window_start).total_seconds()
    if n == 1:
        return [window_start + timedelta(seconds=span / 2)]
    step = span / (n + 1)
    return [window_start + timedelta(seconds=step * (i + 1)) for i in range(n)]


def _format_event_for_prompt(ev: dict, rolled_outcome: str) -> str:
    parts = []
    parts.append(f"  event_id: {ev.get('id')}")
    parts.append(f"  rolled_outcome: {rolled_outcome}")
    parts.append(f"  kind: {ev.get('kind', '?')}")
    # Magnitude is "normal" for ordinary planned events; "major" for the
    # planner's weekly major-event roll (loss / job change / health real /
    # relationship shift). Surface to Sonnet so the narration calibrates —
    # a "loss" magnitude=major is grief-shaped, not a generic mood bump.
    magnitude = ev.get("magnitude") or "normal"
    if magnitude != "normal":
        parts.append(f"  magnitude: {magnitude}  ← weight the narrative accordingly")
    parts.append(f"  title: {ev.get('title', '?')}")
    parts.append(f"  when_local: {ev.get('when_local', '?')}")
    parts.append(f"  duration_min: {ev.get('duration_min', '?')}")
    if ev.get("with"):
        parts.append(f"  with: {', '.join(ev['with'])}")
    if ev.get("location"):
        parts.append(f"  location: {ev['location']}")
    if ev.get("anticipation") and ev["anticipation"] != "neutral":
        parts.append(f"  anticipation: {ev['anticipation']}")
    if ev.get("revision_history"):
        # Surface chat-driven changes so the model knows the user's been involved
        for rev in ev["revision_history"]:
            if not isinstance(rev, dict):
                continue
            parts.append(f"  revision (by {rev.get('by', '?')}, {rev.get('reason', '?')}): {json.dumps(rev.get('diff') or {})[:200]}")
    return "\n".join(parts)


def _format_life_stream_tail(entries: list[dict]) -> str:
    if not entries:
        return "(empty — first pass)"
    lines = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ts = e.get("at_local", "?")
        kind = e.get("kind", "?")
        summary = e.get("summary", "")
        lines.append(f"  {ts} [{kind}] {summary}")
    return "\n".join(lines) if lines else "(empty)"


def _had_chat_revision_in_window(events: list[dict], since_iso: Optional[str]) -> bool:
    """Hard-override skip-roll: did any elapsed event get mutated by chat since
    the last resolver run? If yes, the resolver must run so the change is
    acknowledged in life_stream.

    No prior run → check ALL chat revisions on the events (since the "window"
    is unbounded backwards). Practically this means the first pass after a
    chat-driven mutation will always run; skip-roll only kicks in on
    subsequent passes against unchanged state.
    """
    for ev in events:
        if not isinstance(ev, dict):
            continue
        for rev in ev.get("revision_history") or []:
            if not isinstance(rev, dict):
                continue
            if rev.get("by") != "chat":
                continue
            if not since_iso:
                # No prior run — any chat revision counts
                return True
            rev_at = rev.get("at")
            if isinstance(rev_at, str) and rev_at > since_iso:
                return True
    return False


def run_daily_resolver(
    client,
    project_dir: str,
    *,
    now_dt: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> dict:
    """Run a single resolver pass. Returns a meta dict for telemetry/logging.

    On skip-roll: returns {skipped: True, elapsed_count, ...}. No API call.
    On run: returns {skipped: False, resolutions: N, unplanned: N, usage, cost, ...}.
    On no-elapsed-events: returns {skipped: False, elapsed_count: 0}, no-op.
    """
    from character_schedule import (
        load_schedule,
        stamp_resolution,
        roll_event_outcome,
        append_life_stream_many,
        read_life_stream_tail,
    )
    from character_project_state import load_project_state, save_project_state

    if not project_dir or not os.path.isdir(project_dir):
        return {"error": "bad project_dir", "skipped": True}

    if now_dt is None:
        # Project tz: characters live in ET per the wall_clock helpers
        from game_systems.characters import now_et
        now_dt = now_et()
    if rng is None:
        rng = random.Random()

    schedule = load_schedule(project_dir)
    if not isinstance(schedule, dict):
        return {"skipped": True, "reason": "no schedule.json"}

    state = load_project_state(project_dir) or {}
    bands = state.get("flakiness_bands") or {}
    if not bands:
        # Character has no flakiness_bands set yet (pre-interview / pre-bootstrap).
        # Don't spend Sonnet tokens narrating events that would all roll as_planned
        # with no nuance — auto-stamp elapsed events and bail. The first real
        # resolver pass after bands get set will start producing texture.
        elapsed_count = 0
        for ev in schedule.get("events") or []:
            if not isinstance(ev, dict) or ev.get("status") != "planned":
                continue
            when_str = ev.get("when_local")
            if not isinstance(when_str, str):
                continue
            try:
                ev_start = datetime.fromisoformat(when_str)
                if ev_start.tzinfo is None:
                    ev_start = ev_start.astimezone()
            except (ValueError, TypeError):
                continue
            ev_end = ev_start + timedelta(minutes=int(ev.get("duration_min") or 60))
            if ev_end <= now_dt:
                from character_schedule import stamp_resolution
                stamp_resolution(
                    project_dir, ev["id"],
                    outcome="as_planned", what_happened="", how_it_went="", mood_delta=0,
                    at_iso=now_dt.isoformat(timespec="seconds"),
                )
                elapsed_count += 1
        _stamp_last_run(project_dir, state, now_dt)
        return {"skipped": True, "reason": "no flakiness_bands", "elapsed_count": elapsed_count}

    mood_state = (state.get("wellbeing") or {}).get("state") or "Even"
    scheduler_state = state.get("scheduler_state") or {}
    last_run_iso = scheduler_state.get("last_resolver_run_at")

    # Find elapsed planned events
    elapsed: list[dict] = []
    for ev in schedule.get("events") or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("status") != "planned":
            continue
        when_str = ev.get("when_local")
        duration_min = int(ev.get("duration_min") or 60)
        if not isinstance(when_str, str):
            continue
        try:
            ev_start = datetime.fromisoformat(when_str)
            if ev_start.tzinfo is None:
                ev_start = ev_start.astimezone()
        except (ValueError, TypeError):
            continue
        ev_end = ev_start + timedelta(minutes=duration_min)
        if ev_end <= now_dt:
            elapsed.append(ev)

    # Skip-roll first — gates BOTH event resolution AND unplanned-moment
    # generation. The window can be "uneventful" even with no scheduled
    # events to resolve (the character was just at work or asleep).
    # Hard-override: chat-driven mutations in the window force a real run.
    chat_forced = _had_chat_revision_in_window(elapsed, last_run_iso)
    skipped = (rng.random() < SKIP_RATE) and not chat_forced
    if skipped:
        skip_ls_entries: list[dict] = []
        for ev in elapsed:
            res_at_iso = _event_end_iso(ev, now_dt)
            stamp_resolution(
                project_dir, ev["id"],
                outcome="as_planned", what_happened="", how_it_went="", mood_delta=0,
                at_iso=res_at_iso,
            )
            # Thin life_stream entry — the event happened even without
            # narrative, and life_stream is the canonical "what happened"
            # ledger. Without this, recall and off-screen miss skip-roll
            # events entirely; the character has no grounding to share
            # them in conversation beyond the schedule injection.
            skip_ls_entries.append(_thin_ls_entry(ev, res_at_iso))
        if skip_ls_entries:
            append_life_stream_many(project_dir, skip_ls_entries)
        _stamp_last_run(project_dir, state, now_dt)
        return {
            "skipped": True,
            "elapsed_count": len(elapsed),
            "chat_forced": False,
            "ls_thin_entries": len(skip_ls_entries),
        }

    # Roll outcomes per elapsed event (could be empty list when no events
    # ended in this window — the character is still alive between events
    # and may still have unplanned moments to log).
    rolled: list[dict] = []
    for ev in elapsed:
        outcome = roll_event_outcome(ev, bands, mood_state=mood_state, rng=rng)
        rolled.append({"event": ev, "outcome": outcome})

    # Roll unplanned-moment count. Independent of whether any scheduled
    # events elapsed — the resolver's job is to log lived experience in
    # the window, not just resolve what was on the calendar.
    unplanned_n = min(_poisson(UNPLANNED_LAMBDA, rng), UNPLANNED_MAX)

    # Nothing to narrate? Stamp last_run and bail without an API call.
    # Saves tokens on the common quiet-window case (Poisson rolled 0
    # AND no events elapsed) while still treating the window as "ran
    # successfully, just produced nothing notable."
    if not rolled and unplanned_n == 0:
        _stamp_last_run(project_dir, state, now_dt)
        return {"skipped": False, "elapsed_count": 0, "unplanned": 0, "reason": "empty roll"}

    # Window for unplanned-moment timestamps
    window_start = (
        datetime.fromisoformat(last_run_iso).astimezone()
        if isinstance(last_run_iso, str) and last_run_iso
        else (now_dt - timedelta(hours=24))  # bootstrap window: 24hr back if no prior run
    )

    # Build context, call Sonnet
    profile_doc = _read_file(os.path.join(project_dir, "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir, "user_life.di"))
    life_tail = read_life_stream_tail(
        project_dir,
        since_iso=(now_dt - timedelta(hours=LIFE_STREAM_TAIL_HOURS)).isoformat(timespec="seconds"),
    )

    payload, usage = _call_sonnet(
        client,
        profile_doc=profile_doc,
        user_life_doc=user_life_doc,
        state=state,
        life_tail=life_tail,
        rolled=rolled,
        unplanned_n=unplanned_n,
        window_start=window_start,
        window_end=now_dt,
    )

    # Apply resolutions — TRUST THE ROLL, NOT THE MODEL: override outcome with rolled bucket
    rolled_by_id = {r["event"]["id"]: r["outcome"] for r in rolled}
    n_resolutions = 0
    ls_entries: list[dict] = []
    total_mood_delta = 0
    for res in payload.get("resolutions") or []:
        if not isinstance(res, dict):
            continue
        eid = res.get("event_id")
        if eid not in rolled_by_id:
            logger.warning(f"resolver: model emitted resolution for unknown event {eid!r} — skipping")
            continue
        # Force backend's rolled outcome — model's outcome field is advisory only
        backend_outcome = rolled_by_id[eid]
        what_happened = (res.get("what_happened") or "").strip()
        how_it_went = res.get("how_it_went") if res.get("how_it_went") in VALID_TONES else "even"
        mood_delta = int(res.get("mood_delta") or 0)
        mood_delta = max(-2, min(2, mood_delta))
        total_mood_delta += mood_delta

        ev_dict = next(r["event"] for r in rolled if r["event"]["id"] == eid)
        # Resolution timestamp: end of the event's window
        try:
            ev_start_dt = datetime.fromisoformat(ev_dict["when_local"])
            if ev_start_dt.tzinfo is None:
                ev_start_dt = ev_start_dt.astimezone()
            res_at_dt = ev_start_dt + timedelta(minutes=int(ev_dict.get("duration_min") or 60))
        except (ValueError, TypeError):
            res_at_dt = now_dt
        res_at_iso = res_at_dt.isoformat(timespec="seconds")

        stamp_resolution(
            project_dir, eid,
            outcome=backend_outcome,
            what_happened=what_happened,
            how_it_went=how_it_went,
            mood_delta=mood_delta,
            at_iso=res_at_iso,
        )
        ls_entries.append({
            "id": _ls_id(res_at_iso),
            "at_local": res_at_iso,
            "kind": "resolved_planned",
            "ref": eid,
            "summary": what_happened or f"({backend_outcome}, no detail)",
            "tone": how_it_went,
            "available_to_recall": True,
        })
        n_resolutions += 1

    # Any rolled events the model failed to narrate get auto-stamped AND
    # get a thin life_stream entry — the event happened even without
    # narrative; missing it from life_stream would silently lose the lived
    # experience for that event.
    narrated_ids = {res.get("event_id") for res in (payload.get("resolutions") or []) if isinstance(res, dict)}
    for r in rolled:
        eid = r["event"]["id"]
        if eid in narrated_ids:
            continue
        logger.warning(f"resolver: model didn't narrate {eid} — auto-stamping with rolled outcome {r['outcome']}")
        ev = r["event"]
        res_at_iso = _event_end_iso(ev, now_dt)
        stamp_resolution(
            project_dir, eid, outcome=r["outcome"],
            what_happened="", how_it_went="", mood_delta=0,
            at_iso=res_at_iso,
        )
        ls_entries.append(_thin_ls_entry(ev, res_at_iso))

    # Apply unplanned moments — backend-assigns timestamps via even spread
    unplanned_list = payload.get("unplanned") or []
    times = _spread_unplanned_times(window_start, now_dt, min(len(unplanned_list), unplanned_n))
    for i, up in enumerate(unplanned_list[:unplanned_n]):
        if not isinstance(up, dict):
            continue
        summary = (up.get("summary") or "").strip()
        if not summary:
            continue
        tone = up.get("tone") if up.get("tone") in VALID_TONES else "even"
        at_iso = times[i].isoformat(timespec="seconds") if i < len(times) else now_dt.isoformat(timespec="seconds")
        ls_entries.append({
            "id": _ls_id(at_iso),
            "at_local": at_iso,
            "kind": "unplanned",
            "ref": None,
            "summary": summary,
            "tone": tone,
            "available_to_recall": True,
        })

    # Append everything in one pass
    if ls_entries:
        append_life_stream_many(project_dir, ls_entries)

    # Wellbeing: bump wb_mod by summed mood_delta, clamped at ±WB_MOD_CAP
    wb = state.setdefault("wellbeing", {"state": "Even", "wb_mod": 0})
    cur_mod = int(wb.get("wb_mod", 0) or 0)
    new_mod = max(-WB_MOD_CAP, min(WB_MOD_CAP, cur_mod + total_mood_delta))
    wb["wb_mod"] = new_mod

    # Stamp last_resolver_run_at (also persists wellbeing change above)
    _stamp_last_run(project_dir, state, now_dt)

    cost = compute_resolver_cost(usage) if usage else 0.0
    return {
        "skipped": False,
        "elapsed_count": len(elapsed),
        "resolutions": n_resolutions,
        "unplanned": len(ls_entries) - n_resolutions,
        "mood_delta_total": total_mood_delta,
        "wb_mod_after": new_mod,
        "usage": usage,
        "cost": cost,
        "model": RESOLVER_MODEL,
    }


def _stamp_last_run(project_dir: str, state: dict, now_dt: datetime) -> None:
    """Persist scheduler_state.last_resolver_run_at and any wellbeing changes."""
    from character_project_state import save_project_state
    sched = state.setdefault("scheduler_state", {})
    sched["last_resolver_run_at"] = now_dt.isoformat(timespec="seconds")
    save_project_state(project_dir, state)


def _call_sonnet(
    client,
    *,
    profile_doc: str,
    user_life_doc: str,
    state: dict,
    life_tail: list[dict],
    rolled: list[dict],
    unplanned_n: int,
    window_start: datetime,
    window_end: datetime,
) -> tuple[dict, dict]:
    """Single Sonnet 4.6 call; returns (parsed_payload, usage).
    On error returns ({}, {})."""
    stable_parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        stable_parts += ["[USER LIFE — context]", user_life_doc, ""]
    stable_text = "\n".join(stable_parts)

    # Volatile context
    wb = state.get("wellbeing") or {}
    parts = []
    parts.append(f"[WINDOW] from {window_start.isoformat(timespec='seconds')} to {window_end.isoformat(timespec='seconds')}")
    parts.append(f"[CURRENT MOOD] {wb.get('state', 'Even')} (wb_mod for tomorrow's roll: {wb.get('wb_mod', 0)})")
    arc = state.get("arc_state") or ""
    if arc:
        parts.append(f"[ARC] {arc}")

    cbs = (state.get("callbacks") or {}).get("open") or []
    if cbs:
        parts.append("[OPEN CALLBACKS]:")
        for cb in cbs[:8]:
            if isinstance(cb, dict):
                parts.append(f"  #{cb.get('id')} {cb.get('original_text', '')[:140]}")

    parts.append("")
    parts.append("[RECENT LIFE STREAM TAIL — last 48hr context, do not repeat these]:")
    parts.append(_format_life_stream_tail(life_tail))
    parts.append("")

    parts.append(f"[ELAPSED EVENTS] ({len(rolled)} events. Honor the rolled_outcome for each — DO NOT change it):")
    for r in rolled:
        parts.append(_format_event_for_prompt(r["event"], r["outcome"]))
        parts.append("")

    parts.append(f"[UNPLANNED COUNT] {unplanned_n}  (Generate exactly this many unplanned moments in the window. May be 0.)")
    parts.append("")
    parts.append("Call report_resolver_pass exactly once.")
    volatile_text = "\n".join(parts)

    tool = build_resolver_tool()
    try:
        response = client.messages.create(
            model=RESOLVER_MODEL,
            max_tokens=RESOLVER_MAX_TOKENS,
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
            tool_choice={"type": "tool", "name": "report_resolver_pass"},
            timeout=RESOLVER_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"resolver: API call failed: {type(e).__name__}: {e}")
        return {}, {}

    payload: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_resolver_pass":
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
