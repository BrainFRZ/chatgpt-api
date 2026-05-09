"""Schedule + life-stream storage for the Characters continuous-existence layer.

Two files per project:
- `schedule.json` — current week's planned events. Lock-serialized atomic rewrite.
  Same lock file (`.character_state.lock`) as character_project_state.py so writes
  to schedule.json and character_state.json serialize against each other.
- `life_stream.jsonl` — append-only ledger of what actually happened. Same
  atomic-append pattern as character_storage.py.

Three writers mutate the schedule:
- chat (character_agent post-stream extracts schedule_ops)
- off_screen agent
- daily resolver cron

All three call `apply_schedule_ops` with `source` set to one of those, which
appends a revision_history entry per op and writes a `schedule_change` entry
to life_stream as the audit trail.

Outcomes are rolled by `roll_event_outcome` — pure function, real RNG, no
model in the loop. Sonnet narrates the result; it doesn't pick the bucket.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import random
import tempfile
import uuid
from datetime import datetime
from typing import Iterable, Optional

try:
    import fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)

SCHEDULE_FILENAME = "schedule.json"
LIFE_STREAM_FILENAME = "life_stream.jsonl"

# Reuse character_state.json's lock file so writes to either file serialize.
_LOCK_FILENAME = ".character_state.lock"

EVENT_KINDS = ("work", "social", "family", "self_care", "admin", "anticipated", "sleep")
EVENT_STATUSES = ("planned", "as_planned", "modified", "cancelled")

# Forward-rolling lifecycle: events get a `pending_resolution` field set by
# the resolver's pre-roll phase (cron at time T_N rolls outcomes for events
# ending in [T_N, T_{N+1}]). Pending stays until either:
#   - The next cron stamps it (event_end has passed → status flips to outcome,
#     resolution dict is set, life_stream entry written, pending cleared)
#   - Chat-driven schedule_op mutation clears it (next cron re-rolls)
# Schedule injection treats events with pending + event_end_past as "past" so
# the model has access to the rolled outcome between event-end and next-cron-
# stamp. Recall and life_stream don't see the entry until officially stamped.
PENDING_RESOLUTION_FIELDS = ("outcome", "what_happened", "how_it_went", "mood_delta")
LIFE_STREAM_KINDS = ("resolved_planned", "unplanned", "schedule_change", "major_event")


@contextlib.contextmanager
def _exclusive_lock(project_dir: str):
    """Same lock infrastructure as character_project_state._exclusive_lock."""
    if not _HAS_FCNTL:
        yield
        return
    lock_path = os.path.join(project_dir, _LOCK_FILENAME)
    fh = None
    try:
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


def _schedule_path(project_dir: str) -> str:
    return os.path.join(project_dir, SCHEDULE_FILENAME)


def _life_stream_path(project_dir: str) -> str:
    return os.path.join(project_dir, LIFE_STREAM_FILENAME)


# ── Schedule read/write ──────────────────────────────────────────────

def load_schedule(project_dir: Optional[str]) -> Optional[dict]:
    """Read schedule.json. Returns None if missing or unreadable."""
    if not project_dir:
        return None
    path = _schedule_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"load_schedule: failed to read {path}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_schedule(project_dir: Optional[str], schedule: dict) -> bool:
    """Atomically write schedule.json under exclusive lock. Returns True on success."""
    if not project_dir or not isinstance(schedule, dict):
        return False
    path = _schedule_path(project_dir)
    try:
        with _exclusive_lock(project_dir):
            fd, tmp_path = tempfile.mkstemp(
                prefix=".schedule.", suffix=".tmp", dir=project_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(schedule, f, indent=2, ensure_ascii=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    except OSError as e:
        logger.error(f"save_schedule: failed to write {path}: {e}")
        return False
    return True


def init_schedule(week_of_iso: str, generated_at_iso: str) -> dict:
    """Fresh empty schedule for a given week."""
    return {
        "week_of": week_of_iso,
        "generated_at": generated_at_iso,
        "next_id": 1,
        "events": [],
    }


# ── Life-stream read/write ───────────────────────────────────────────

def append_life_stream(project_dir: Optional[str], entry: dict) -> bool:
    """Append a single entry to life_stream.jsonl. OS-atomic for entries under PIPE_BUF."""
    if not project_dir or not isinstance(entry, dict):
        return False
    path = _life_stream_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error(f"append_life_stream: failed to append to {path}: {e}")
        return False
    return True


def append_life_stream_many(project_dir: Optional[str], entries: Iterable[dict]) -> int:
    """Append multiple entries in a single open. Returns count appended."""
    if not project_dir:
        return 0
    path = _life_stream_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                if isinstance(e, dict):
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    n += 1
    except OSError as ex:
        logger.error(f"append_life_stream_many: failed to append to {path}: {ex}")
    return n


def read_life_stream_all(project_dir: Optional[str]) -> list[dict]:
    """Read every entry. Empty list if file missing."""
    if not project_dir:
        return []
    path = _life_stream_path(project_dir)
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        out.append(entry)
                except json.JSONDecodeError as e:
                    logger.warning(f"read_life_stream: bad line in {path}: {e}")
    except OSError as e:
        logger.error(f"read_life_stream: failed to read {path}: {e}")
    return out


def read_life_stream_tail(
    project_dir: Optional[str],
    since_iso: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Read entries with at_local >= since_iso. If since_iso None, return all (then cap by limit).

    Result is in file order (chronological, since the stream is append-only).
    """
    entries = read_life_stream_all(project_dir)
    if since_iso:
        entries = [e for e in entries if isinstance(e.get("at_local"), str) and e["at_local"] >= since_iso]
    if limit is not None and len(entries) > limit:
        entries = entries[-limit:]
    return entries


# ── Schedule mutations ───────────────────────────────────────────────

def _next_event_id(schedule: dict) -> str:
    n = int(schedule.get("next_id") or 1)
    schedule["next_id"] = n + 1
    return f"sch-{n}"


_IMMUTABLE_EVENT_FIELDS = frozenset({"id", "revision_history", "resolution"})

# Fields a chat-driven (or off-screen-driven) modify is allowed to change.
# Whitelist instead of blacklist: status/magnitude/resolver_hints are *technically*
# mutable from the resolver path (stamp_resolution / planner) but must NOT be
# touched by modify ops, otherwise the side agent could bypass the cancel
# distinction by emitting a modify with status=cancelled.
_MODIFY_ALLOWED_FIELDS = frozenset({
    "kind", "title", "with", "when_local", "duration_min", "location", "anticipation",
})


def _diff_summary(before: dict, after_fields: dict) -> dict:
    """Build a diff dict for revision_history showing field changes.

    Restricted to _MODIFY_ALLOWED_FIELDS so attempts to set status/magnitude/
    resolution/etc. via modify don't appear in the diff. Snapshots `from` via
    copy.deepcopy to avoid live-reference cycles when the same dict gets
    mutated downstream.
    """
    import copy
    diff = {}
    for k, new_v in after_fields.items():
        if k not in _MODIFY_ALLOWED_FIELDS:
            continue
        old_v = before.get(k)
        if old_v != new_v:
            diff[k] = {"from": copy.deepcopy(old_v), "to": copy.deepcopy(new_v)}
    return diff


def _append_revision(ev: dict, rev: dict) -> None:
    """Append a revision entry, defensively reinitializing revision_history if
    a manually-edited schedule.json left it as None or non-list."""
    hist = ev.get("revision_history")
    if not isinstance(hist, list):
        hist = []
        ev["revision_history"] = hist
    hist.append(rev)


def _ls_id(at_iso: str) -> str:
    """Generate a life-stream entry id."""
    date_part = at_iso[:10] if isinstance(at_iso, str) and len(at_iso) >= 10 else "0000-00-00"
    return f"ls-{date_part}-{uuid.uuid4().hex[:6]}"


def apply_schedule_ops(
    project_dir: str,
    ops: list[dict],
    *,
    source: str,
    now_iso: Optional[str] = None,
) -> dict:
    """Apply a list of cancel/modify/add ops to schedule.json under exclusive lock.

    Each op shape:
      {"op": "cancel", "event_id": "sch-N", "reason": "..."}
      {"op": "modify", "event_id": "sch-N", "fields": {...}, "reason": "..."}
      {"op": "add",    "fields": {...}, "reason": "..."}

    Appends a revision_history entry per modify/cancel; for add, the event is
    appended fresh with empty revision_history. Writes one schedule_change
    life_stream entry per op summarizing what happened.

    Returns the updated schedule dict (or the existing one unchanged if no ops
    applied or schedule.json missing).
    """
    if not project_dir or not ops:
        sched = load_schedule(project_dir)
        return sched or {}

    if now_iso is None:
        now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    life_stream_entries: list[dict] = []

    with _exclusive_lock(project_dir):
        # Re-read inside the lock so we apply against the latest disk state.
        path = _schedule_path(project_dir)
        if not os.path.isfile(path):
            logger.warning(f"apply_schedule_ops: {path} missing — skipping ops")
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                schedule = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"apply_schedule_ops: failed to read {path}: {e}")
            return {}

        events: list[dict] = schedule.get("events") or []
        events_by_id = {e.get("id"): e for e in events if isinstance(e, dict)}

        for op_dict in ops:
            if not isinstance(op_dict, dict):
                continue
            op_kind = op_dict.get("op")
            reason = op_dict.get("reason") or ""

            if op_kind == "cancel":
                eid = op_dict.get("event_id")
                ev = events_by_id.get(eid)
                if not ev:
                    logger.warning(f"apply_schedule_ops: cancel for unknown event_id {eid}")
                    continue
                rev = {
                    "at": now_iso,
                    "by": source,
                    "reason": reason,
                    "diff": {"status": {"from": ev.get("status"), "to": "cancelled"}},
                }
                _append_revision(ev, rev)
                ev["status"] = "cancelled"
                # Chat-driven cancel invalidates any cron pre-roll. The chat is
                # authoritative now; next cron sees status != planned and skips.
                ev.pop("pending_resolution", None)
                life_stream_entries.append({
                    "id": _ls_id(now_iso),
                    "at_local": now_iso,
                    "kind": "schedule_change",
                    "ref": eid,
                    "summary": f"Cancelled: {ev.get('title', '(untitled)')} — {reason}".strip(" —"),
                    "tone": "neutral",
                    "available_to_recall": True,
                })

            elif op_kind == "modify":
                eid = op_dict.get("event_id")
                ev = events_by_id.get(eid)
                if not ev:
                    logger.warning(f"apply_schedule_ops: modify for unknown event_id {eid}")
                    continue
                fields = op_dict.get("fields") or {}
                if not isinstance(fields, dict) or not fields:
                    continue
                diff = _diff_summary(ev, fields)
                if not diff:
                    continue
                rev = {"at": now_iso, "by": source, "reason": reason, "diff": diff}
                _append_revision(ev, rev)
                # Apply mutations: whitelist of fields modify is allowed to touch.
                # Keeps status/magnitude/resolution/id/revision_history out of reach —
                # those flow through cancel (status), planner (magnitude), or
                # stamp_resolution (resolution) instead.
                for k, v in fields.items():
                    if k not in _MODIFY_ALLOWED_FIELDS:
                        continue
                    ev[k] = v
                # Modify invalidates pre-rolled outcome — the rolled narrative
                # was conditioned on the old fields. Clear pending; next cron
                # re-rolls based on the modified state.
                ev.pop("pending_resolution", None)
                life_stream_entries.append({
                    "id": _ls_id(now_iso),
                    "at_local": now_iso,
                    "kind": "schedule_change",
                    "ref": eid,
                    "summary": f"Changed: {ev.get('title', '(untitled)')} — {reason}".strip(" —"),
                    "tone": "neutral",
                    "available_to_recall": True,
                })

            elif op_kind == "add":
                fields = op_dict.get("fields") or {}
                if not isinstance(fields, dict) or not fields.get("title"):
                    logger.warning("apply_schedule_ops: add missing title — skipping")
                    continue
                new_id = _next_event_id(schedule)
                new_event = {
                    "id": new_id,
                    "kind": fields.get("kind", "social"),
                    "title": fields.get("title"),
                    "with": fields.get("with") or [],
                    "when_local": fields.get("when_local"),
                    "duration_min": fields.get("duration_min"),
                    "location": fields.get("location"),
                    "anticipation": fields.get("anticipation", "neutral"),
                    "magnitude": fields.get("magnitude", "normal"),
                    "resolver_hints": fields.get("resolver_hints", ""),
                    "status": "planned",
                    "resolution": None,
                    "revision_history": [{
                        "at": now_iso,
                        "by": source,
                        "reason": reason or "added",
                        "diff": {"status": {"from": None, "to": "planned"}},
                    }],
                }
                events.append(new_event)
                events_by_id[new_id] = new_event
                life_stream_entries.append({
                    "id": _ls_id(now_iso),
                    "at_local": now_iso,
                    "kind": "schedule_change",
                    "ref": new_id,
                    "summary": f"Added: {new_event['title']} — {reason}".strip(" —"),
                    "tone": "neutral",
                    "available_to_recall": True,
                })

            else:
                logger.warning(f"apply_schedule_ops: unknown op {op_kind!r}")
                continue

        schedule["events"] = events

        # Write under the same lock
        fd, tmp_path = tempfile.mkstemp(
            prefix=".schedule.", suffix=".tmp", dir=project_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(schedule, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # Append life_stream entries OUTSIDE the lock (jsonl append is itself atomic)
    if life_stream_entries:
        append_life_stream_many(project_dir, life_stream_entries)

    return schedule


def stamp_resolution(
    project_dir: str,
    event_id: str,
    *,
    outcome: str,
    what_happened: str = "",
    how_it_went: str = "",
    mood_delta: int = 0,
    at_iso: Optional[str] = None,
) -> bool:
    """Resolver helper: mark an event resolved with an outcome and resolution dict.

    `outcome` ∈ {as_planned, modified, cancelled}. Status is set to outcome.
    Empty strings allowed for skip-roll auto-stamping (no narrative).
    """
    if outcome not in ("as_planned", "modified", "cancelled"):
        logger.warning(f"stamp_resolution: bad outcome {outcome!r}")
        return False
    if at_iso is None:
        at_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    with _exclusive_lock(project_dir):
        path = _schedule_path(project_dir)
        if not os.path.isfile(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                schedule = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False
        events = schedule.get("events") or []
        ev = next((e for e in events if isinstance(e, dict) and e.get("id") == event_id), None)
        if not ev:
            return False
        ev["status"] = outcome
        ev["resolution"] = {
            "at": at_iso,
            "outcome": outcome,
            "what_happened": what_happened,
            "how_it_went": how_it_went,
            "mood_delta": mood_delta,
        }
        # Atomic rewrite
        fd, tmp_path = tempfile.mkstemp(
            prefix=".schedule.", suffix=".tmp", dir=project_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(schedule, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return True


# ── Outcome roll ─────────────────────────────────────────────────────

# Mood × category modifiers applied on top of base flakiness_bands.
# Modifiers are deltas added to the named bucket; as_planned absorbs the
# remainder so the three buckets sum to 1.0. Clamped to [0, 1] per bucket.
RESOLVER_MOOD_MOD_TABLE: dict[str, dict[str, dict[str, float]]] = {
    "Excellent": {
        "social":    {"cancelled": -0.05},
        "self_care": {"cancelled": -0.10},
    },
    "Buoyant": {
        "social":    {"cancelled": -0.05},
    },
    "Even": {},
    "Frayed": {
        "social":    {"modified":  +0.05},
        "self_care": {"cancelled": +0.15},
        "admin":     {"modified":  +0.10},
    },
    "Rough": {
        "shae":      {"cancelled": +0.05},
        "social":    {"cancelled": +0.20},
        "self_care": {"cancelled": +0.30},
        "admin":     {"modified":  +0.15},
    },
}


def effective_bands(
    base_bands: dict,
    kind: str,
    mood_state: Optional[str] = None,
) -> dict:
    """Apply mood modifier to base flakiness bands for the given kind.

    Returns a new dict with as_planned/modified/cancelled summing to ~1.0,
    clamped to [0, 1] per bucket.
    """
    base = base_bands.get(kind) if isinstance(base_bands, dict) else None
    if not isinstance(base, dict):
        # Sensible default: definitely-as-planned
        return {"as_planned": 1.0, "modified": 0.0, "cancelled": 0.0}

    as_p = float(base.get("as_planned", 1.0))
    mod  = float(base.get("modified",   0.0))
    can  = float(base.get("cancelled",  0.0))

    mood_table = RESOLVER_MOOD_MOD_TABLE.get(mood_state or "Even") or {}
    deltas = mood_table.get(kind) or {}
    if "modified" in deltas:
        mod += deltas["modified"]
    if "cancelled" in deltas:
        can += deltas["cancelled"]
    # Clamp; remainder absorbed by as_planned
    mod = max(0.0, min(1.0, mod))
    can = max(0.0, min(1.0, can))
    if mod + can > 1.0:
        # Clip proportionally
        scale = 1.0 / (mod + can)
        mod *= scale
        can *= scale
    as_p = max(0.0, 1.0 - mod - can)
    return {"as_planned": as_p, "modified": mod, "cancelled": can}


def roll_event_outcome(
    event: dict,
    flakiness_bands: dict,
    mood_state: Optional[str] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """Backend-rolled outcome for a planned event. Returns one of:
    'as_planned' | 'modified' | 'cancelled'.

    Uses the event's `kind` to pick the band, applies mood modifier, then
    rolls random.random() against the cumulative bucket boundaries.
    """
    kind = event.get("kind", "social")
    bands = effective_bands(flakiness_bands, kind, mood_state=mood_state)
    r = (rng or random).random()
    if r < bands["cancelled"]:
        return "cancelled"
    if r < bands["cancelled"] + bands["modified"]:
        return "modified"
    return "as_planned"
