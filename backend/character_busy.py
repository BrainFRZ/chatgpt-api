"""Busy-state detection for Characters.

When the character's schedule says they're in a sleep block or a work shift,
they shouldn't reply normally — that's a real-friend break in the chat. The
user's message gets delivered with a status indicator ("she's asleep, she'll
see it when she wakes up") instead of a synthetic awake response.

Exception: if the user explicitly marks the message urgent (SOS / 911 /
emergency / /sos slash command), we break through. The voice agent gets a
[BUSY INTERRUPT] injection telling it to acknowledge being roused from
[activity] in voice — the character doesn't pretend they weren't busy.

This module is the pure detection layer:
  - current_busy_event(schedule, now_dt) → the busy event we're inside, or None
  - is_sos_message(text) → bool

Routing decisions live in main.py.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from typing import Any, Optional


# Per-channel probability the character is too busy to reply at any given
# moment during a work shift. Rolled FRESH per incoming message — most
# messages mid-shift get through (slow-shift moments), some don't (rush
# moments). Realistic baseline: a cafe owner with employees can text between
# customers cheaply but phone/video pulls her off the floor, and inperson
# is essentially incompatible with being mid-shift.
#
# Tunable.
WORK_BUSY_PROBABILITY_BY_CHANNEL = {
    "text": 0.30,      # 5-second hits between customers; rushes lock her out
    "phone": 0.70,     # needs uninterrupted minutes off the floor
    "video": 0.80,     # phone cost + a quiet spot + not being on camera mid-service
    "inperson": 0.95,  # physically can't be elsewhere — only break-time exceptions
}
# Fallback when channel is missing or unrecognized — matches text (the
# Characters default and the cheapest cognitive load).
WORK_BUSY_PROBABILITY_DEFAULT = 0.30


def _is_event_busy_now(
    event: dict,
    now_dt: datetime,
    rng: Optional[random.Random] = None,
    *,
    channel: Optional[str] = None,
) -> bool:
    """Decide whether THIS active event makes the character unable to reply
    RIGHT NOW. Layered logic:

    1. Explicit per-event `busy: True` / `busy: False` always wins. Lets the
       planner (or the user manually) hard-flag specific events as phone-
       down (movies, mom-on-the-phone, doctor's appointment, important
       date) or always-reachable (slow-known shift, low-key social).

    2. Sleep is unconditionally busy (she's unconscious — no roll).

    3. Work shifts roll a per-channel probability (see
       WORK_BUSY_PROBABILITY_BY_CHANNEL). Texts mostly get through during a
       cafe shift; phone/video usually don't; inperson essentially never.

    4. Everything else (social, self_care, family, admin, anticipated)
       is NOT busy by default — only the explicit override path makes
       them busy. This matches the design: most events should let the
       model use its judgment via the planner contract.
    """
    explicit = event.get("busy")
    if explicit is True:
        return True
    if explicit is False:
        return False
    kind = event.get("kind")
    if kind == "sleep":
        return True
    if kind == "work":
        prob = WORK_BUSY_PROBABILITY_BY_CHANNEL.get(channel or "", WORK_BUSY_PROBABILITY_DEFAULT)
        r = (rng if rng is not None else random).random()
        return r < prob
    return False


def _parse_iso(s: Any) -> Optional[datetime]:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt
    except (ValueError, TypeError):
        return None


def current_busy_event(
    schedule: Optional[dict],
    now_dt: datetime,
    *,
    rng: Optional[random.Random] = None,
    channel: Optional[str] = None,
) -> Optional[dict]:
    """Return the schedule event whose window contains `now_dt` AND whose
    busy-status logic resolves to busy. Otherwise None.

    Overlap precedence — important for the middle-of-the-night case: if a
    non-sleep event overlaps the sleep window (a 2am unplanned moment, a
    middle-of-night phone call, an emergency event), prefer the non-sleep
    event for the busy decision. She's awake for THAT — sleep is interrupted.
    Without this rule, an in-window sleep event would trump a real wake-up
    moment and the user would get a 'she's asleep' notification while she
    was clearly up doing something.

    Busy logic: see _is_event_busy_now. Sleep is always busy in window;
    work rolls a per-channel probability (text mostly gets through; phone/
    video usually don't; inperson essentially never); other kinds need
    explicit busy: True; explicit busy: False overrides.

    rng: pass an injected random.Random for deterministic tests.
    channel: current Characters channel (text/phone/inperson/video) — drives
    the work-shift roll. Defaults to the text-equivalent rate when missing.
    """
    if not isinstance(schedule, dict):
        return None
    events = schedule.get("events") or []
    if not isinstance(events, list):
        return None

    # First pass: collect all events whose window contains now_dt
    in_window: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if not isinstance(kind, str):
            continue
        status = ev.get("status") or "planned"
        if status in ("cancelled", "replaced"):
            continue
        start = _parse_iso(ev.get("when_local"))
        if start is None:
            continue
        try:
            duration_min = int(ev.get("duration_min") or 0)
        except (TypeError, ValueError):
            continue
        if duration_min <= 0:
            continue
        end = start + timedelta(minutes=duration_min)
        if start <= now_dt < end:
            in_window.append(ev)

    if not in_window:
        return None

    # Middle-of-the-night exception: if any non-sleep event overlaps, the
    # character is awake for THAT, not asleep. Sleep is implicitly interrupted.
    non_sleep = [e for e in in_window if e.get("kind") != "sleep"]
    candidates = non_sleep if non_sleep else in_window

    for ev in candidates:
        if _is_event_busy_now(ev, now_dt, rng=rng, channel=channel):
            return ev
    return None


# ── SOS detection ────────────────────────────────────────────────────────

# Word-boundary keyword matching. Each pattern is a regex that matches the
# whole token in case-insensitive context. We deliberately use word
# boundaries so "I had a 911 dispatcher tell me a story" doesn't trigger;
# "911!" or "this is a 911" does.
_SOS_KEYWORD_PATTERNS = (
    re.compile(r"\bSOS\b", re.IGNORECASE),
    re.compile(r"\b911\b"),
    re.compile(r"\bemergency\b", re.IGNORECASE),
)

# The /sos slash command, expected at the start of the message
_SOS_SLASH_COMMAND = re.compile(r"^\s*/sos\b", re.IGNORECASE)


def is_sos_message(text: Any) -> bool:
    """Detect if a user message is flagged as urgent enough to break through
    a busy state. Keywords (SOS, 911, emergency) match case-insensitively
    on word boundaries; the /sos slash command works at the start of the
    message.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _SOS_SLASH_COMMAND.search(text):
        return True
    for pat in _SOS_KEYWORD_PATTERNS:
        if pat.search(text):
            return True
    return False


def describe_busy_event(event: dict) -> str:
    """Short user-facing string for the status indicator. Examples:
    'asleep', 'at the cafe', 'sleeping'. Used by the frontend banner.
    """
    if not isinstance(event, dict):
        return "busy"
    kind = event.get("kind") or ""
    title = (event.get("title") or "").strip()
    if kind == "sleep":
        return "asleep"
    if kind == "work":
        return f"at work ({title})" if title else "at work"
    if title:
        return title
    return kind or "busy"


def strip_sos_slash_command(text: str) -> tuple[str, bool]:
    """If `text` begins with the /sos slash command, strip it and return
    (cleaned, True). Otherwise return (text, False).

    Used so the saved user message + chat display don't show the literal
    '/sos help me' but the SOS context is still tracked via metadata.
    """
    if not isinstance(text, str):
        return ("", False)
    m = _SOS_SLASH_COMMAND.search(text)
    if not m:
        return (text, False)
    # Remove "/sos" plus any following whitespace
    rest = text[m.end():].lstrip()
    return (rest, True)


def collapse_busy_placeholders_in_history(messages: list) -> list:
    """Compact busy_placeholder assistant turns out of a chat-history list
    before sending it to the model. Consecutive user-busy-user-busy-user
    runs (5 messages overnight while she was asleep) collapse to a single
    user message with a gap marker, so the model sees the gap once instead
    of N times.

    Operates on full message dicts (with id, parent_id, role, content,
    timestamp, busy_placeholder, busy_event, ...) and returns full dicts —
    the merged user message is based on the LATEST of the merged ones so
    its timestamp + attached_files reflect the most recent activity. The
    merged message gets a `merged_from` field for debug.

    Pure function; doesn't mutate input messages.
    """
    out: list[dict] = []
    accumulated: list[dict] = []
    pending_gap_descs: list[str] = []

    def _flush_user() -> None:
        if not accumulated:
            return
        if len(accumulated) == 1 and not pending_gap_descs:
            out.append(accumulated[0])
        else:
            base = dict(accumulated[-1])  # newest, preserves timestamp + attached_files
            content_parts: list[str] = []
            if pending_gap_descs:
                seen: list[str] = []
                for d in pending_gap_descs:
                    if d not in seen:
                        seen.append(d)
                marker = "; ".join(seen)
                content_parts.append(
                    f"[gap — Zara was {marker}; she didn't reply to the messages between]"
                )
            for m in accumulated:
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    content_parts.append(c)
            base["content"] = "\n\n".join(content_parts)
            base["merged_from"] = [m.get("id") for m in accumulated if m.get("id")]
            out.append(base)
        accumulated.clear()
        pending_gap_descs.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            accumulated.append(msg)
        elif role == "assistant":
            if msg.get("busy_placeholder"):
                ev = msg.get("busy_event") or {}
                pending_gap_descs.append(ev.get("description") or "unreachable")
                continue
            _flush_user()
            out.append(msg)
        else:
            _flush_user()
            out.append(msg)
    _flush_user()
    return out


def busy_status_for_notification(event: dict, now_dt: datetime) -> dict:
    """Frontend-shaped notification dict for a busy state."""
    end_dt = None
    start = _parse_iso(event.get("when_local"))
    try:
        duration_min = int(event.get("duration_min") or 0)
    except (TypeError, ValueError):
        duration_min = 0
    if start and duration_min > 0:
        end_dt = start + timedelta(minutes=duration_min)
    return {
        "type": "character_busy",
        "kind": event.get("kind"),
        "title": event.get("title"),
        "description": describe_busy_event(event),
        "ends_at": end_dt.isoformat() if end_dt else None,
    }
