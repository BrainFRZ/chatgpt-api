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

import re
from datetime import datetime, timedelta
from typing import Any, Optional


# Event kinds that take the character's full attention. When the current
# wall-clock falls inside one of these events, the character is "busy" and
# won't reply unless an SOS breaks through.
#
# - sleep: obvious; the character is unconscious
# - work:  the cafe shift; she's on the floor, hands full, can't text
#
# Other kinds (social, self_care, family, admin, anticipated) are NOT
# auto-busy by default. Some of them might warrant it (Sunday mom call,
# yoga class) but those are case-by-case and can be added as a per-event
# `busy: True` flag on the schedule event later if needed.
BUSY_EVENT_KINDS = frozenset({"sleep", "work"})


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
    kinds: frozenset[str] = BUSY_EVENT_KINDS,
) -> Optional[dict]:
    """Return the schedule event whose [when_local, when_local + duration_min)
    window currently contains `now_dt`, IF that event's kind is busy-flagged
    (or has explicit `busy: True`). Otherwise None.

    A schedule shape from character_schedule.py: {events: [{kind, when_local,
    duration_min, status, ...}], ...}. We skip cancelled/replaced events —
    the character is bound by what they're actually doing right now, not
    by what was originally planned.
    """
    if not isinstance(schedule, dict):
        return None
    events = schedule.get("events") or []
    if not isinstance(events, list):
        return None

    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if not isinstance(kind, str):
            continue
        # Per-event override beats kind-based default. If `busy` is explicitly
        # set, honor it. Otherwise fall back to the BUSY_EVENT_KINDS check.
        explicit_busy = ev.get("busy")
        if explicit_busy is True:
            is_busy_kind = True
        elif explicit_busy is False:
            is_busy_kind = False
        else:
            is_busy_kind = kind in kinds
        if not is_busy_kind:
            continue
        # Skip non-active events. Sleep events are typically auto-stamped
        # at planning time; checking for status in the active set keeps us
        # from treating a cancelled shift as still happening.
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
