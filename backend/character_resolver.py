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


SYSTEM_PROMPT = """You are pre-narrating what's about to happen in a character's life over the upcoming window. The system reveals these events at their actual time — your narrations get committed to the canonical "what happened" ledger when real time crosses each event's end.

You are NOT picking outcomes — those are already rolled by the backend dice. You ONLY write what the rolled outcomes look like in this character's specific voice. Past tense narration is correct: at any point AFTER the event has elapsed, this is how the character would describe it.

Chat between the user and the character can MUTATE events between now and when they happen (cancel, reschedule, swap). If that happens, your narration here gets discarded and the next cron re-rolls. So treat your job as: "given the current state of the schedule and the rolled outcomes, here's the most likely shape of how this window unfolds."

For each event in [UPCOMING EVENTS]:
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

INTERNAL TIME CONSISTENCY:
- A moment's narration must match its `at_local` timestamp. If the moment is at 1:20am, do NOT write "between 8:30 and the couch" or "during her morning rush." Reference the actual wall-clock time of the moment.
- Sequence narration in `what_happened` must respect event start/end times. An event running 8pm-10pm cannot reference "the morning crowd."

SLEEP-WINDOW AWARENESS:
- The character has a sleep schedule (visible in [SLEEP WINDOWS] below if present).
- When a moment's `at_local` falls inside a sleep window, the moment MUST be sleep-appropriate. Acceptable: a dream fragment, a brief wake (water, bathroom, glance at phone), insomnia spiral, sleepwalking, nightmare aftershock, the ambient feeling of being half-awake. NOT acceptable: customer interactions, conversations with anyone awake, daytime activities, work tasks, social plans.
- A wake-up at 3am can be a real beat; a 3am customer interaction at the cafe cannot be (the cafe is closed and she's at home asleep).
- If [SLEEP WINDOWS] is empty, you may distribute moments across the window freely.

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
    end = _event_end_dt(ev)
    if end is None:
        return fallback_dt.isoformat(timespec="seconds")
    return end.isoformat(timespec="seconds")


def _event_start_iso(ev: dict, fallback_dt: datetime) -> str:
    """Return the event's start timestamp (when_local) normalized to ISO,
    or `fallback_dt.isoformat()` if when_local is missing/malformed.

    Used as `at_local` on resolved_planned life_stream entries so the
    ledger reads chronologically by when events *happened* in the
    character's day — "Open the cafe — Monday morning shift" lands at
    06:00 (shift start), not 14:00 (resolution stamp). resolution.at
    on the schedule event keeps the end time; that field is the
    bookkeeping "when did the resolver commit this", not the lived
    timestamp.
    """
    raw = ev.get("when_local")
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt.isoformat(timespec="seconds")
        except (ValueError, TypeError):
            pass
    return fallback_dt.isoformat(timespec="seconds")


def _event_end_dt(ev: dict) -> Optional[datetime]:
    """Parse an event's end datetime from when_local + duration_min.
    Returns None if unparseable. Used for "is this event in the past/future"
    checks in the forward-rolling lifecycle."""
    try:
        ev_start = datetime.fromisoformat(ev["when_local"])
        if ev_start.tzinfo is None:
            ev_start = ev_start.astimezone()
        return ev_start + timedelta(minutes=int(ev.get("duration_min") or 60))
    except (ValueError, TypeError, KeyError):
        return None


def _resolver_cron_hours() -> tuple:
    """Lazy-import the cron schedule from scheduler.py to avoid drifting.

    scheduler.py is the single source of truth for when the resolver
    fires; this resolver module imports the same tuple at call time
    rather than duplicating it. Falls back to a hardcoded default if
    scheduler.py isn't importable (test contexts without apscheduler
    might bypass it, but DAILY_RESOLVER_HOURS itself doesn't depend on
    apscheduler).
    """
    try:
        from scheduler import DAILY_RESOLVER_HOURS
        return DAILY_RESOLVER_HOURS
    except ImportError:
        return (8, 13, 18)


def _next_resolver_cron_time(now_dt: datetime) -> datetime:
    """Returns the next 8am / 1pm / 6pm ET datetime strictly after now_dt.
    Used by the pre-roll phase to scope which events are "ending in the
    window I'm responsible for."

    The 6pm cron's next-cron is 8am the following day — that overnight
    window is wider than the daytime windows. λ=1.5 stays the same per
    pass; λ-per-hour is lower overnight, which is fine since the
    character is mostly asleep.
    """
    from game_systems.characters import TZ
    today_et = now_dt.astimezone(TZ).date()
    candidates = []
    from datetime import time as _time
    for h in _resolver_cron_hours():
        candidates.append(datetime.combine(today_et, _time(h, 0), tzinfo=TZ))
    # Tomorrow's first cron — covers the case where now is past today's last cron
    tomorrow = today_et + timedelta(days=1)
    candidates.append(datetime.combine(tomorrow, _time(_resolver_cron_hours()[0], 0), tzinfo=TZ))
    for c in candidates:
        if c > now_dt:
            return c
    # Should be unreachable given we appended tomorrow's first
    return now_dt + timedelta(hours=12)


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
    preroll: bool = True,
) -> dict:
    """Forward-rolling resolver pass. Three stages:

    1. STAMP — events whose pre-rolled `pending_resolution` is past their
       event_end get officially stamped: status flips, life_stream entry
       written, mood_delta applied. Pending unplanned moments whose
       at_local has passed get appended to life_stream too. This is
       formal commit: "the cron pre-decided this; now real time has
       crossed event_end, lock it in."

    2. FALLBACK — events with status=planned, no pending, event_end <= now.
       Legacy data or missed-cron events. Stamp thin (as_planned + empty
       narrative + 0 mood) so they don't stay planned forever. The
       character has zero detail for these — degraded mode.

    3. PRE-ROLL — events ending in [now, next_resolver_cron] without
       pending get rolled forward. Backend rolls the outcome bucket via
       flakiness_bands; Sonnet narrates each (one Sonnet call per pass
       covers all upcoming events + Poisson-rolled unplanned moments
       for the same window). Stored as `pending_resolution` on each
       event and `pending_unplanned[]` on the schedule. NOT yet in
       life_stream — the next cron commits them.

    Skip-roll gates only the PRE-ROLL stage (stages 1 + 2 always run —
    stamping past pre-rolls and handling legacy events is bookkeeping).
    Hard-override: chat-driven mutations on upcoming events force the
    pre-roll to run regardless of skip-roll.

    Chat between crons can mutate events (cancel/modify/add). Mutations
    invalidate any pending_resolution → next cron re-rolls based on
    the chat-updated state. Chat is authoritative between crons; cron
    is authoritative at stamp time.

    preroll: when False, skip Stage 3 (pre-rolling upcoming events).
    Use False for on-message catch-up triggered between cron firings:
    Stages 1+2 are deterministic and write the life_stream entries that
    the turn's off-screen log needs, while skipping the Sonnet call that
    would add latency to the user's reply. The next scheduled cron does
    Stage 3 normally.
    """
    from character_schedule import (
        load_schedule,
        save_schedule,
        roll_event_outcome,
        append_life_stream_many,
        read_life_stream_tail,
    )
    from character_project_state import load_project_state, save_project_state

    if not project_dir or not os.path.isdir(project_dir):
        return {"error": "bad project_dir", "skipped": True}

    if now_dt is None:
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
        # Pre-bootstrap / pre-interview character. Stamp any elapsed events
        # thin and bail — no Sonnet narration, no pre-roll.
        elapsed_stamped = _bail_no_bands_stamp(schedule, now_dt)
        save_schedule(project_dir, schedule)
        if elapsed_stamped:
            append_life_stream_many(
                project_dir,
                [_thin_ls_entry(ev, _event_start_iso(ev, now_dt)) for ev in elapsed_stamped],
            )
        _stamp_last_run(project_dir, state, now_dt)
        return {"skipped": True, "reason": "no flakiness_bands", "elapsed_count": len(elapsed_stamped)}

    mood_state = (state.get("wellbeing") or {}).get("state") or "Even"
    scheduler_state = state.get("scheduler_state") or {}
    last_run_iso = scheduler_state.get("last_resolver_run_at")

    events: list[dict] = schedule.get("events") or []

    # ── STAGE 1: STAMP pending pre-rolls whose time has passed ──────────
    stage1_ls_entries: list[dict] = []
    stamped_event_count = 0
    total_mood_delta = 0
    for ev in events:
        pending = ev.get("pending_resolution")
        if not isinstance(pending, dict):
            continue
        ev_end = _event_end_dt(ev)
        if ev_end is None or ev_end > now_dt:
            continue
        outcome = pending.get("outcome", "as_planned")
        what_happened = (pending.get("what_happened") or "").strip()
        how_it_went = pending.get("how_it_went") if pending.get("how_it_went") in VALID_TONES else "even"
        mood_delta = max(-2, min(2, int(pending.get("mood_delta") or 0)))
        end_iso = ev_end.isoformat(timespec="seconds")
        start_iso = _event_start_iso(ev, ev_end)
        ev["status"] = outcome
        ev["resolution"] = {
            "at": end_iso,
            "outcome": outcome,
            "what_happened": what_happened,
            "how_it_went": how_it_went,
            "mood_delta": mood_delta,
        }
        ev.pop("pending_resolution", None)
        stamped_event_count += 1
        total_mood_delta += mood_delta
        stage1_ls_entries.append({
            "id": _ls_id(start_iso),
            "at_local": start_iso,
            "kind": "resolved_planned",
            "ref": ev.get("id"),
            "summary": what_happened or ev.get("title") or "(scheduled event)",
            "tone": how_it_went,
            "available_to_recall": True,
        })

    # Stamp pending unplanned whose at_local has passed
    pending_unplanned = schedule.get("pending_unplanned") or []
    remaining_pending: list[dict] = []
    stamped_unplanned_count = 0
    for up in pending_unplanned:
        if not isinstance(up, dict):
            continue
        try:
            up_at = datetime.fromisoformat(up.get("at_local", ""))
            if up_at.tzinfo is None:
                up_at = up_at.astimezone()
        except (ValueError, TypeError):
            continue
        if up_at <= now_dt:
            stage1_ls_entries.append({
                "id": up.get("id") or _ls_id(up["at_local"]),
                "at_local": up["at_local"],
                "kind": "unplanned",
                "ref": None,
                "summary": up.get("summary", ""),
                "tone": up.get("tone", "even"),
                "available_to_recall": True,
            })
            stamped_unplanned_count += 1
        else:
            remaining_pending.append(up)
    schedule["pending_unplanned"] = remaining_pending

    # ── STAGE 2: FALLBACK for elapsed events with no pre-roll ───────────
    fallback_ls_entries: list[dict] = []
    for ev in events:
        if ev.get("status") != "planned":
            continue
        if ev.get("pending_resolution"):
            continue
        ev_end = _event_end_dt(ev)
        if ev_end is None or ev_end > now_dt:
            continue
        # Thin stamp — system missed pre-rolling, degrade gracefully
        end_iso = ev_end.isoformat(timespec="seconds")
        start_iso = _event_start_iso(ev, ev_end)
        ev["status"] = "as_planned"
        ev["resolution"] = {
            "at": end_iso,
            "outcome": "as_planned",
            "what_happened": "",
            "how_it_went": "even",
            "mood_delta": 0,
        }
        fallback_ls_entries.append(_thin_ls_entry(ev, start_iso))

    # Persist all of STAGE 1 + 2's writes
    if stage1_ls_entries or fallback_ls_entries:
        save_schedule(project_dir, schedule)
        append_life_stream_many(project_dir, stage1_ls_entries + fallback_ls_entries)
    elif schedule.get("pending_unplanned") != pending_unplanned:
        save_schedule(project_dir, schedule)

    # Apply STAGE 1's mood_delta to wb_mod
    if total_mood_delta:
        wb = state.setdefault("wellbeing", {"state": "Even", "wb_mod": 0})
        wb["wb_mod"] = max(-WB_MOD_CAP, min(WB_MOD_CAP, int(wb.get("wb_mod", 0) or 0) + total_mood_delta))
        save_project_state(project_dir, state)

    # Catch-up path stops here: stamping is enough to surface elapsed
    # events in this turn's off-screen log. The next scheduled cron does
    # Stage 3 (pre-roll + Sonnet call) on its own cadence.
    if not preroll:
        _stamp_last_run(project_dir, state, now_dt)
        return {
            "skipped": False,
            "stage": "stamp_only",
            "stamped_events": stamped_event_count,
            "stamped_unplanned": stamped_unplanned_count,
            "fallback_stamped": len(fallback_ls_entries),
            "stage1_mood_delta": total_mood_delta,
        }

    # ── STAGE 3: PRE-ROLL outcomes for events ending in next window ─────
    # Forward-looking: cron at T_N pre-rolls events ending [T_N, T_{N+1}].
    # Their outcomes wait as `pending_resolution` until the next cron stamps.
    # Chat between crons can mutate; mutation invalidates pending → re-roll.
    #
    # Sleep events (kind="sleep") are scaffold — they auto-stamp as_planned
    # without Sonnet narration. They DO get exposed to Sonnet as [SLEEP
    # WINDOWS] context so unplanned moments respect when the character is
    # asleep.
    next_cron_dt = _next_resolver_cron_time(now_dt)
    upcoming: list[dict] = []
    sleep_windows_in_window: list[dict] = []
    for ev in events:
        if ev.get("status") != "planned":
            continue
        if ev.get("pending_resolution"):
            continue
        ev_end = _event_end_dt(ev)
        if ev_end is None:
            continue
        if now_dt < ev_end <= next_cron_dt:
            if ev.get("kind") == "sleep":
                # Auto-thin-pending so next cron stamps without Sonnet roll
                ev["pending_resolution"] = {
                    "outcome": "as_planned",
                    "what_happened": "",
                    "how_it_went": "even",
                    "mood_delta": 0,
                }
                sleep_windows_in_window.append(ev)
            else:
                upcoming.append(ev)
    # Also collect ALL sleep events overlapping the window (even already-pending
    # ones) for context — Sonnet needs to know the FULL sleep schedule, not
    # just newly-rolled ones.
    sleep_for_context: list[dict] = []
    for ev in events:
        if ev.get("kind") != "sleep":
            continue
        ev_end = _event_end_dt(ev)
        if ev_end is None:
            continue
        try:
            ev_start = datetime.fromisoformat(ev["when_local"])
            if ev_start.tzinfo is None:
                ev_start = ev_start.astimezone()
        except (ValueError, TypeError, KeyError):
            continue
        # Overlaps with [now, next_cron]
        if ev_end > now_dt and ev_start < next_cron_dt:
            sleep_for_context.append(ev)

    # Skip-roll for the upcoming window
    chat_forced = _had_chat_revision_in_window(upcoming, last_run_iso)
    skipped_preroll = (rng.random() < SKIP_RATE) and not chat_forced
    if skipped_preroll:
        # Thin pending for each upcoming event — the event will happen
        # uneventfully (as_planned, no narrative). Stamp on next cron.
        for ev in upcoming:
            ev["pending_resolution"] = {
                "outcome": "as_planned",
                "what_happened": "",
                "how_it_went": "even",
                "mood_delta": 0,
            }
        save_schedule(project_dir, schedule)
        _stamp_last_run(project_dir, state, now_dt)
        return {
            "skipped": True,
            "stage": "preroll",
            "stamped_events": stamped_event_count,
            "stamped_unplanned": stamped_unplanned_count,
            "fallback_stamped": len(fallback_ls_entries),
            "preroll_thin": len(upcoming),
        }

    # Roll outcome for each upcoming event
    rolled: list[dict] = []
    for ev in upcoming:
        outcome = roll_event_outcome(ev, bands, mood_state=mood_state, rng=rng)
        rolled.append({"event": ev, "outcome": outcome})

    # Roll Poisson for unplanned count in the upcoming window
    unplanned_n = min(_poisson(UNPLANNED_LAMBDA, rng), UNPLANNED_MAX)

    # Nothing to narrate this pass? Skip Sonnet entirely.
    if not rolled and unplanned_n == 0:
        _stamp_last_run(project_dir, state, now_dt)
        return {
            "skipped": False,
            "stage": "preroll",
            "stamped_events": stamped_event_count,
            "stamped_unplanned": stamped_unplanned_count,
            "fallback_stamped": len(fallback_ls_entries),
            "preroll_events": 0,
            "preroll_unplanned": 0,
            "reason": "empty preroll",
        }

    # Sonnet narrates the upcoming window — outcomes given, model writes
    # what_happened + tone + mood_delta, plus unplanned moments
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
        window_start=now_dt,
        window_end=next_cron_dt,
        sleep_windows=sleep_for_context,
    )

    # If the Sonnet call failed entirely (caught exception → empty usage),
    # don't lock in thin pending_resolution for the upcoming events and
    # don't stamp last_resolver_run_at forward. Leaving events as
    # status=planned with no pending lets the next cron retry the pre-roll
    # cleanly; leaving last_run alone preserves the chat-revision
    # force-no-skip window. Stage 1 + Stage 2 results already persisted
    # above (line ~522) — we keep those. Distinguished from "model
    # returned empty resolutions" (a real model response with no
    # narration) by the usage dict: a successful call always reports
    # usage, even when payload is sparse.
    if not usage:
        logger.warning(
            "resolver: Sonnet call failed — Stage 3 pre-roll skipped; "
            "next cron will retry."
        )
        return {
            "skipped": False,
            "stage": "preroll",
            "stamped_events": stamped_event_count,
            "stamped_unplanned": stamped_unplanned_count,
            "fallback_stamped": len(fallback_ls_entries),
            "preroll_events": 0,
            "preroll_unplanned": 0,
            "stage1_mood_delta": total_mood_delta,
            "error": "sonnet_call_failed",
        }

    # Apply pre-rolled resolutions to events as `pending_resolution`
    rolled_by_id = {r["event"]["id"]: r["outcome"] for r in rolled}
    narrated_ids: set[str] = set()
    for res in payload.get("resolutions") or []:
        if not isinstance(res, dict):
            continue
        eid = res.get("event_id")
        if eid not in rolled_by_id:
            logger.warning(f"resolver: model emitted resolution for unknown event {eid!r}")
            continue
        narrated_ids.add(eid)
        ev = next(r["event"] for r in rolled if r["event"]["id"] == eid)
        ev["pending_resolution"] = {
            "outcome": rolled_by_id[eid],  # backend-rolled, override model
            "what_happened": (res.get("what_happened") or "").strip(),
            "how_it_went": res.get("how_it_went") if res.get("how_it_went") in VALID_TONES else "even",
            "mood_delta": max(-2, min(2, int(res.get("mood_delta") or 0))),
        }
    # Events the model didn't narrate get thin pending
    for r in rolled:
        if r["event"]["id"] in narrated_ids:
            continue
        logger.warning(f"resolver: model didn't narrate {r['event']['id']} — using thin pending")
        r["event"]["pending_resolution"] = {
            "outcome": r["outcome"],
            "what_happened": "",
            "how_it_went": "even",
            "mood_delta": 0,
        }

    # Apply pre-rolled unplanned moments to schedule.pending_unplanned
    unplanned_list = payload.get("unplanned") or []
    times = _spread_unplanned_times(now_dt, next_cron_dt, min(unplanned_n, len(unplanned_list)))
    for i, up in enumerate(unplanned_list[:unplanned_n]):
        if not isinstance(up, dict):
            continue
        summary = (up.get("summary") or "").strip()
        if not summary:
            continue
        tone = up.get("tone") if up.get("tone") in VALID_TONES else "even"
        at_iso = times[i].isoformat(timespec="seconds") if i < len(times) else now_dt.isoformat(timespec="seconds")
        schedule["pending_unplanned"] = (schedule.get("pending_unplanned") or []) + [{
            "id": _ls_id(at_iso),
            "at_local": at_iso,
            "summary": summary,
            "tone": tone,
        }]

    save_schedule(project_dir, schedule)
    _stamp_last_run(project_dir, state, now_dt)

    cost = compute_resolver_cost(usage) if usage else 0.0
    return {
        "skipped": False,
        "stage": "preroll",
        "stamped_events": stamped_event_count,
        "stamped_unplanned": stamped_unplanned_count,
        "fallback_stamped": len(fallback_ls_entries),
        "preroll_events": len(rolled),
        "preroll_unplanned": min(unplanned_n, len(unplanned_list)),
        "stage1_mood_delta": total_mood_delta,
        "usage": usage,
        "cost": cost,
        "model": RESOLVER_MODEL,
    }


def _bail_no_bands_stamp(schedule: dict, now_dt: datetime) -> list[dict]:
    """For pre-bootstrap characters: thin-stamp any events whose end has
    passed and return the list (caller persists)."""
    stamped: list[dict] = []
    for ev in schedule.get("events") or []:
        if not isinstance(ev, dict) or ev.get("status") != "planned":
            continue
        ev_end = _event_end_dt(ev)
        if ev_end is None or ev_end > now_dt:
            continue
        end_iso = ev_end.isoformat(timespec="seconds")
        ev["status"] = "as_planned"
        ev["resolution"] = {
            "at": end_iso, "outcome": "as_planned",
            "what_happened": "", "how_it_went": "even", "mood_delta": 0,
        }
        stamped.append(ev)
    return stamped


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
    sleep_windows: Optional[list[dict]] = None,
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
    parts.append(f"[UPCOMING WINDOW] {window_start.isoformat(timespec='seconds')} → {window_end.isoformat(timespec='seconds')}")
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

    # Sleep windows: scaffold context. Sonnet must place unplanned moments
    # falling inside these intervals as sleep-appropriate (dream / wake /
    # insomnia / etc.) rather than daytime activities.
    parts.append("[SLEEP WINDOWS — when the character is asleep; unplanned moments inside these intervals must be sleep-appropriate]:")
    if sleep_windows:
        for sw in sleep_windows:
            sw_start = sw.get("when_local", "?")
            sw_end_dt = _event_end_dt(sw)
            sw_end = sw_end_dt.isoformat(timespec="seconds") if sw_end_dt else "?"
            parts.append(f"  {sw_start} -> {sw_end}")
    else:
        parts.append("  (no sleep events scheduled in this window)")
    parts.append("")

    parts.append(f"[UPCOMING EVENTS] ({len(rolled)} events ending in this window. Honor the rolled_outcome for each — DO NOT change it):")
    for r in rolled:
        parts.append(_format_event_for_prompt(r["event"], r["outcome"]))
        parts.append("")

    parts.append(f"[UNPLANNED COUNT] {unplanned_n}  (Generate exactly this many unplanned moments in the window. May be 0. Each moment must respect the [SLEEP WINDOWS] above.)")
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
