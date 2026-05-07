"""
Characters gamesystem — ongoing correspondence with a single character per project.

Architecture:
- Narration model: Claude 3 Opus (default) or Opus 4.5 (fallback) — writes prose directly, no tool wrapper.
- State extraction: Haiku 4.5 side agent (`character_agent.py`) — runs after each turn.
- Off-screen life: Opus 4.5 (`character_off_screen.py`) — fires at session resume (>12h gap).
- Interview: Opus 4.5 (`character_interview.py`) — produces character_profile.di on first run.

State (lives in pipeline_state["characters_state"]):
- character_memories: tier-capped (flavor=25, moderate=10, high=15) memory list. NOT NPC-keyed — single character.
- callbacks: open / resolved / dismissed. d10-vs-days check-in cadence. No quiet drops.
- wellbeing: 2d10+mod rolled once per real ET day; 5 bands (Rough/Frayed/Even/Buoyant/Excellent).
- arc_state: free-form short string describing relationship phase.
- channel: text / phone / inperson / video.
- user_profile: stable user facts; player-seeded, model-mutated via ops.
- off_screen_log: transient per-day events generated at session resume.
- wall_clock: first_message_at, last_user_message_at, last_message_at (ISO-8601 with offset).

Hard-fails (banner modal, no API request) when character_profile.di is missing.
"""
from __future__ import annotations

import os
import random
import logging
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Tunables ────────────────────────────────────────────────────────

CHARACTER_MEMORY_TIER_LIMITS = {"high": 15, "moderate": 10, "flavor": 25}
CHARACTER_MEMORY_MAX_TOTAL = 50  # safety net across tiers

CALLBACK_RESOLVED_RETENTION_DAYS = 30  # real days, not turns
CALLBACK_DISMISSED_RETENTION_DAYS = 30
CALLBACK_AGING_DIE = 10              # d10 vs days-since-last-checkin

USER_PROFILE_SOFT_CAP = 40

# Character growth: the model-writable second profile layer that accumulates
# durable emergent facts about the character (new hobbies, evolved opinions,
# new people in their life, etc.) between formal /consolidate passes.
CHARACTER_GROWTH_SOFT_CAP = 80
CHARACTER_GROWTH_CATEGORIES = (
    "hobby", "opinion", "fact", "relationship", "skill",
    "preference", "milestone", "voice", "other",
)

OFF_SCREEN_GAP_THRESHOLD_HOURS = 12
OFF_SCREEN_EVENTS_PER_DAY = 3        # max per real day in the gap
OFF_SCREEN_MAX_DAYS = 14             # don't generate more than 2 weeks of life

WELLBEING_BANDS = ("Rough", "Frayed", "Even", "Buoyant", "Excellent")
WELLBEING_DEFAULT = "Even"
WB_MOD_CAP = 2  # ±2 cumulative cap on the next-roll modifier

VALID_CHANNELS = ("text", "phone", "inperson", "video")
DEFAULT_CHANNEL = "text"

# Sawtooth context trimming (overrides single-agent global default)
CHARACTERS_THRESHOLD_PAIRS = 60
CHARACTERS_TARGET_PAIRS = 40

ARC_STATE_MAX_LEN = 80

# Eastern timezone — the character lives on user-local-Eastern time.
TZ = ZoneInfo("America/New_York")


# ── Time helpers ────────────────────────────────────────────────────

def now_et() -> datetime:
    return datetime.now(TZ)


def today_et_iso() -> str:
    return now_et().date().isoformat()


def parse_iso_dt(s: Optional[str]) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def days_between_dates(a_iso: Optional[str], b_iso: Optional[str]) -> int:
    """Whole days between two ISO date strings (b - a). Negative → 0."""
    if not a_iso or not b_iso:
        return 0
    try:
        a = datetime.fromisoformat(a_iso).date()
        b = datetime.fromisoformat(b_iso).date()
        return max(0, (b - a).days)
    except (ValueError, TypeError):
        return 0


def hours_between_iso(earlier: Optional[str], later: Optional[str]) -> float:
    a = parse_iso_dt(earlier)
    b = parse_iso_dt(later)
    if not a or not b:
        return 0.0
    return max(0.0, (b - a).total_seconds() / 3600.0)


# ── State init ──────────────────────────────────────────────────────

def init_characters_state() -> dict:
    """Fresh characters_state for a new chat."""
    return {
        "character_memories": [],          # list of memory dicts
        "callbacks": {
            "next_id": 1,
            "open": [],                     # list of dicts (see add_callback)
            "resolved": [],                 # user-marked resolved
            "dismissed": [],                # user-marked no-longer-important
        },
        "wellbeing": {
            "state": WELLBEING_DEFAULT,
            "rolled_date": None,            # ET date ISO when last rolled
            "total": None,                  # 2d10 + mod total (nullable on init)
            "dice": None,                   # [d1, d2]
            "mod": 0,                       # the mod that was applied
            "wb_mod": 0,                    # accumulating modifier for next roll
        },
        "arc_state": "",                    # free-form, ≤80 chars
        "channel": DEFAULT_CHANNEL,
        "user_profile": {                   # stable user facts
            "next_id": 1,
            "entries": [],
        },
        "character_growth": {               # second profile layer: emergent facts about the character
            "next_id": 1,
            "entries": [],                  # [{id, date, text, category, source, obsolete?, obsolete_date?, obsolete_reason?}]
        },
        "off_screen_log": None,             # set by off-screen agent on resume
        "wall_clock": {
            "first_message_at": None,
            "last_user_message_at": None,
            "last_message_at": None,
        },
        "ripe_callbacks": [],               # ids of callbacks "ripe" this turn (rolled at session-start)
    }


def init_game_state() -> dict:
    """Compatibility shim — pipeline_state has top-level 'game_state' field; characters_state lives here too."""
    return {"characters_state": init_characters_state()}


# ── Memory ops ──────────────────────────────────────────────────────

def _memory_tier(impact) -> str:
    try:
        impact = int(impact)
    except (TypeError, ValueError):
        return "flavor"
    if impact >= 4:
        return "high"
    if impact == 3:
        return "moderate"
    return "flavor"


def _memory_sort_key(m: dict) -> tuple:
    return (m.get("impact", 0), m.get("turn_created", 0))


def apply_memory_ops(memories: list, ops: list, current_turn: int, today_iso: Optional[str] = None) -> list:
    """Apply add/drop ops to the flat memory list. Enforces tier caps + total cap.

    Op shapes:
      {action: "add", text, impact: 1-5, quote?, date?, focus?}
      {action: "drop", index: int}
    """
    if not isinstance(memories, list):
        memories = []
    if not isinstance(ops, (list, tuple)) or not ops:
        return memories

    # Sort by injection order so drop indices align with what the model saw
    memories = sorted([m for m in memories if isinstance(m, dict)], key=_memory_sort_key, reverse=True)

    # Drops first, in reverse-index order
    def _coerce_idx(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    drop_ops = sorted(
        [op for op in ops if isinstance(op, dict) and op.get("action") == "drop"],
        key=lambda o: _coerce_idx(o.get("index")),
        reverse=True,
    )
    for op in drop_ops:
        idx = _coerce_idx(op.get("index"))
        if 0 <= idx < len(memories):
            memories.pop(idx)
        else:
            logger.warning(f"character memory drop: index {idx} out of bounds (len={len(memories)})")

    # Adds
    add_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "add"]
    for op in add_ops:
        try:
            impact = int(op.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        impact = max(1, min(5, impact))
        tier = _memory_tier(impact)
        entry = {
            "text": str(op.get("text") or "")[:640],
            "quote": str(op.get("quote") or "")[:160] or None,
            "date": op.get("date") or today_iso,
            "impact": impact,
            "tier": tier,
            "turn_created": current_turn,
            "focus": str(op.get("focus") or "")[:60] or None,
        }
        if not entry["text"]:
            continue
        memories.append(entry)

    # Enforce per-tier caps (drop oldest-and-lowest-impact within the offending tier)
    for tier_name, tier_cap in CHARACTER_MEMORY_TIER_LIMITS.items():
        tier_entries = [(i, m) for i, m in enumerate(memories) if m.get("tier") == tier_name]
        if len(tier_entries) > tier_cap:
            excess = len(tier_entries) - tier_cap
            tier_entries.sort(key=lambda x: (x[1].get("turn_created", 0), x[1].get("impact", 0)))
            remove = {i for i, _ in tier_entries[:excess]}
            memories = [m for j, m in enumerate(memories) if j not in remove]

    # Total cap (safety net across tiers)
    if len(memories) > CHARACTER_MEMORY_MAX_TOTAL:
        memories = sorted(memories, key=_memory_sort_key, reverse=True)[:CHARACTER_MEMORY_MAX_TOTAL]

    # Final sort for stable injection ordering
    memories = sorted(memories, key=_memory_sort_key, reverse=True)
    return memories


# ── Callback ops ────────────────────────────────────────────────────

def apply_callback_ops(callbacks: dict, ops: list, current_turn: int, today_iso: str) -> dict:
    """Apply callback ops. Operations:
      {action: "add", original_text, source?: "user"|"character", resolutions?: [...]}
      {action: "checkin", id: int}                    — character mentioned the callback this turn
      {action: "resolve", id: int, resolution_text?}  — user-marked resolved
      {action: "dismiss", id: int, reason?}           — user-marked no-longer-important

    Note: characters cannot drop callbacks. Only `add` and `checkin` are emitted by the model;
    `resolve` and `dismiss` come from user slash commands.
    """
    if not isinstance(callbacks, dict):
        callbacks = {"next_id": 1, "open": [], "resolved": [], "dismissed": []}

    open_list = callbacks.setdefault("open", [])
    resolved_list = callbacks.setdefault("resolved", [])
    dismissed_list = callbacks.setdefault("dismissed", [])
    next_id = callbacks.get("next_id", 1)
    if not isinstance(next_id, int):
        next_id = 1

    open_by_id = {cb.get("id"): cb for cb in open_list if isinstance(cb, dict)}

    for op in (ops if isinstance(ops, (list, tuple)) else []):
        if not isinstance(op, dict):
            continue
        action = op.get("action")

        if action == "add":
            text = str(op.get("original_text") or "")[:800]
            if not text:
                continue
            resolutions_raw = op.get("resolutions")
            resolutions = (
                [str(r)[:200] for r in list(resolutions_raw)[:3]]
                if isinstance(resolutions_raw, (list, tuple)) else None
            )
            entry = {
                "id": next_id,
                "created_turn": current_turn,
                "created_date": today_iso,
                "last_checkin_date": today_iso,  # creation counts as the first checkin
                "original_text": text,
                "source": op.get("source") if op.get("source") in ("user", "character") else "user",
                "resolutions": resolutions,
            }
            open_by_id[next_id] = entry
            next_id += 1

        elif action == "checkin":
            target = op.get("id")
            if target in open_by_id:
                open_by_id[target]["last_checkin_date"] = today_iso

        elif action == "resolve":
            target = op.get("id")
            if target in open_by_id:
                cb = open_by_id.pop(target)
                cb["resolved_date"] = today_iso
                cb["resolution_text"] = str(op.get("resolution_text") or "")[:400] or None
                resolved_list.append(cb)
            else:
                logger.warning(f"callback resolve: id {target} not open")

        elif action == "dismiss":
            target = op.get("id")
            if target in open_by_id:
                cb = open_by_id.pop(target)
                cb["dismissed_date"] = today_iso
                cb["dismiss_reason"] = str(op.get("reason") or "")[:200] or None
                dismissed_list.append(cb)
            else:
                logger.warning(f"callback dismiss: id {target} not open")

    # Prune resolved/dismissed past retention
    def _keep(entry: dict, date_key: str, retention_days: int) -> bool:
        return days_between_dates(entry.get(date_key), today_iso) <= retention_days

    resolved_list = [r for r in resolved_list if isinstance(r, dict) and _keep(r, "resolved_date", CALLBACK_RESOLVED_RETENTION_DAYS)]
    dismissed_list = [d for d in dismissed_list if isinstance(d, dict) and _keep(d, "dismissed_date", CALLBACK_DISMISSED_RETENTION_DAYS)]

    return {
        "next_id": next_id,
        "open": list(open_by_id.values()),
        "resolved": resolved_list,
        "dismissed": dismissed_list,
    }


def roll_callback_ripeness(callbacks: dict, today_iso: str, rng: Optional[random.Random] = None) -> list:
    """For each open callback, roll a d10 vs days-since-last-checkin.
    Returns the list of ids that are 'ripe' for the character to surface this turn.

    Roll is deterministic per (callback_id, today) when rng is fresh — so the same
    day's first message produces a stable result even if the request is retried.
    """
    if rng is None:
        rng = random.Random()
    ripe = []
    for cb in (callbacks or {}).get("open", []) or []:
        if not isinstance(cb, dict):
            continue
        last = cb.get("last_checkin_date") or cb.get("created_date")
        n = days_between_dates(last, today_iso)
        if n <= 0:
            continue
        roll = rng.randint(1, CALLBACK_AGING_DIE)
        if roll <= n:
            ripe.append(cb.get("id"))
    return ripe


# ── User profile ops ────────────────────────────────────────────────

def apply_user_profile_ops(profile: dict, ops: list, today_iso: str) -> dict:
    """Operations:
      {action: "add", text, category?}
      {action: "edit", id: int, text?, category?}
      {action: "delete", id: int}
    """
    if not isinstance(profile, dict):
        profile = {"next_id": 1, "entries": []}
    entries = profile.setdefault("entries", [])
    next_id = profile.get("next_id", 1)
    if not isinstance(next_id, int):
        next_id = 1
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}

    for op in (ops if isinstance(ops, (list, tuple)) else []):
        if not isinstance(op, dict):
            continue
        action = op.get("action")

        if action == "add":
            text = str(op.get("text") or "")[:400]
            if not text:
                continue
            entry = {
                "id": next_id,
                "text": text,
                "category": str(op.get("category") or "")[:40] or None,
                "added_date": today_iso,
            }
            by_id[next_id] = entry
            next_id += 1

        elif action == "edit":
            target = op.get("id")
            if target in by_id:
                if "text" in op:
                    by_id[target]["text"] = str(op.get("text") or "")[:400]
                if "category" in op:
                    by_id[target]["category"] = str(op.get("category") or "")[:40] or None

        elif action == "delete":
            target = op.get("id")
            by_id.pop(target, None)

    # Soft cap warning (don't enforce — let it grow but note it)
    if len(by_id) > USER_PROFILE_SOFT_CAP:
        logger.info(f"user_profile soft cap exceeded: {len(by_id)} > {USER_PROFILE_SOFT_CAP}")

    return {
        "next_id": next_id,
        "entries": sorted(by_id.values(), key=lambda e: e.get("id", 0)),
    }


# ── Character growth ops ────────────────────────────────────────────

def apply_growth_ops(growth: dict, ops: list, today_iso: str) -> dict:
    """Operations:
      {action: "add", text, category?, source?: "dialogue"|"reflection"|"user"}
        — append a new emergent fact about the character.
      {action: "update", id: int, text?, category?}
        — refine an existing entry (e.g. "still reading LotR" → "finished LotR").
      {action: "obsolete", id: int, reason?}
        — mark as no-longer-true. Stays in the file (with an obsolete marker)
          until /consolidate prunes it; gives the model historical context
          ("she USED to be obsessed with X") without claiming it as current.
      {action: "delete", id: int}
        — hard delete (rare; for ops emitted in error).
    """
    if not isinstance(growth, dict):
        growth = {"next_id": 1, "entries": []}
    entries = growth.setdefault("entries", [])
    next_id = growth.get("next_id", 1)
    if not isinstance(next_id, int):
        next_id = 1
    by_id = {e.get("id"): e for e in entries if isinstance(e, dict)}

    for op in (ops if isinstance(ops, (list, tuple)) else []):
        if not isinstance(op, dict):
            continue
        action = op.get("action")

        if action == "add":
            text = str(op.get("text") or "")[:400]
            if not text:
                continue
            category = str(op.get("category") or "").lower().strip()
            if category not in CHARACTER_GROWTH_CATEGORIES:
                category = "other"
            source = str(op.get("source") or "").lower().strip()
            if source not in ("dialogue", "reflection", "user"):
                source = "dialogue"
            entry = {
                "id": next_id,
                "date": today_iso,
                "text": text,
                "category": category,
                "source": source,
            }
            by_id[next_id] = entry
            next_id += 1

        elif action == "update":
            target = op.get("id")
            if target in by_id:
                if "text" in op:
                    by_id[target]["text"] = str(op.get("text") or "")[:400]
                if "category" in op:
                    cat = str(op.get("category") or "").lower().strip()
                    if cat in CHARACTER_GROWTH_CATEGORIES:
                        by_id[target]["category"] = cat
                by_id[target]["last_updated"] = today_iso

        elif action == "obsolete":
            target = op.get("id")
            if target in by_id:
                by_id[target]["obsolete"] = True
                by_id[target]["obsolete_date"] = today_iso
                by_id[target]["obsolete_reason"] = str(op.get("reason") or "")[:200] or None

        elif action == "delete":
            target = op.get("id")
            by_id.pop(target, None)

    if len(by_id) > CHARACTER_GROWTH_SOFT_CAP:
        logger.info(f"character_growth soft cap exceeded: {len(by_id)} > {CHARACTER_GROWTH_SOFT_CAP}")

    return {
        "next_id": next_id,
        "entries": sorted(by_id.values(), key=lambda e: e.get("id", 0)),
    }


# ── Wellbeing ───────────────────────────────────────────────────────

def _wb_state_from_total(total: int) -> str:
    if total <= 6:
        return "Rough"
    if total <= 9:
        return "Frayed"
    if total <= 15:
        return "Even"
    if total <= 18:
        return "Buoyant"
    return "Excellent"


def maybe_roll_wellbeing(wellbeing: dict, today_iso: str, rng: Optional[random.Random] = None) -> dict:
    """Roll once per ET day, on the first user message of that day.
    Resets wb_mod after applying. No-op if already rolled today.
    """
    if not isinstance(wellbeing, dict):
        wellbeing = {"state": WELLBEING_DEFAULT, "rolled_date": None, "wb_mod": 0}

    if wellbeing.get("rolled_date") == today_iso:
        return wellbeing

    # First-day default: keep Even, no roll, just stamp the date.
    if not wellbeing.get("rolled_date"):
        wellbeing["rolled_date"] = today_iso
        wellbeing["state"] = WELLBEING_DEFAULT
        wellbeing["total"] = None
        wellbeing["dice"] = None
        wellbeing["mod"] = 0
        wellbeing["wb_mod"] = 0
        return wellbeing

    rng = rng or random.Random()
    mod = max(-WB_MOD_CAP, min(WB_MOD_CAP, int(wellbeing.get("wb_mod", 0) or 0)))
    d1, d2 = rng.randint(1, 10), rng.randint(1, 10)
    total = d1 + d2 + mod

    wellbeing["state"] = _wb_state_from_total(total)
    wellbeing["rolled_date"] = today_iso
    wellbeing["total"] = total
    wellbeing["dice"] = [d1, d2]
    wellbeing["mod"] = mod
    wellbeing["wb_mod"] = 0  # reset after applying
    return wellbeing


def apply_wb_mod_ops(wellbeing: dict, ops: list) -> dict:
    """Side agent emits {action: "wb_mod", change: ±2}. Cumulative ±2 cap."""
    if not isinstance(wellbeing, dict):
        wellbeing = {"state": WELLBEING_DEFAULT, "wb_mod": 0}
    cur = int(wellbeing.get("wb_mod", 0) or 0)
    for op in (ops if isinstance(ops, (list, tuple)) else []):
        if not isinstance(op, dict):
            continue
        if op.get("action") != "wb_mod":
            continue
        try:
            change = int(op.get("change", 0))
        except (TypeError, ValueError):
            continue
        if change not in (-2, 2):
            continue
        cur = max(-WB_MOD_CAP, min(WB_MOD_CAP, cur + change))
    wellbeing["wb_mod"] = cur
    return wellbeing


# ── Arc state / channel ──────────────────────────────────────────────

def apply_arc_state_op(arc: str, op: Optional[dict]) -> str:
    if not isinstance(op, dict) or op.get("action") != "set":
        return arc or ""
    new_value = str(op.get("value") or "")[:ARC_STATE_MAX_LEN]
    return new_value


def apply_channel_op(channel: str, op: Optional[dict]) -> str:
    if not isinstance(op, dict) or op.get("action") != "set":
        return channel or DEFAULT_CHANNEL
    new = str(op.get("value") or "").lower()
    if new not in VALID_CHANNELS:
        return channel or DEFAULT_CHANNEL
    return new


# ── Wall clock ──────────────────────────────────────────────────────

def update_wall_clock(wall_clock: dict, *, user_message: bool, when: Optional[datetime] = None) -> dict:
    if not isinstance(wall_clock, dict):
        wall_clock = {"first_message_at": None, "last_user_message_at": None, "last_message_at": None}
    when = when or now_et()
    iso = when.isoformat()
    if not wall_clock.get("first_message_at"):
        wall_clock["first_message_at"] = iso
    wall_clock["last_message_at"] = iso
    if user_message:
        wall_clock["last_user_message_at"] = iso
    return wall_clock


def is_first_message_of_et_day(wall_clock: dict, now: Optional[datetime] = None) -> bool:
    """True if there's no last_user_message_at, or the last one was on a prior ET date."""
    last = (wall_clock or {}).get("last_user_message_at")
    if not last:
        return True
    last_dt = parse_iso_dt(last)
    if not last_dt:
        return True
    n = (now or now_et()).date()
    return last_dt.date() < n


# ── Injection builders ──────────────────────────────────────────────

def build_wall_clock_injection(state: dict) -> str:
    wc = state.get("wall_clock") or {}
    now = now_et()
    weekday = now.strftime("%A")
    date = now.strftime("%Y-%m-%d")
    time = now.strftime("%I:%M %p ET").lstrip("0")
    parts = [f"[NOW]", f"It is {weekday}, {date} at {time}."]

    last_user = parse_iso_dt(wc.get("last_user_message_at"))
    if last_user:
        delta = now - last_user
        hours = delta.total_seconds() / 3600.0
        if hours < 1:
            silence = "less than an hour"
        elif hours < 24:
            silence = f"{int(hours)} hour{'s' if int(hours) != 1 else ''}"
        else:
            d = int(hours // 24)
            silence = f"{d} day{'s' if d != 1 else ''}"
        parts.append(f"Time since the user last messaged: {silence}.")
    else:
        parts.append("This is the first message of the correspondence.")
    return "\n".join(parts)


def build_channel_injection(state: dict) -> str:
    channel = state.get("channel") or DEFAULT_CHANNEL
    descriptions = {
        "text": "TEXT MESSAGES — terse, lowercase-ok, fragmented, abbreviations welcome, no scene-setting prose.",
        "phone": "PHONE CALL — spoken voice, full sentences, vocal mannerisms, sounds-of-the-room ok, no visual description.",
        "inperson": "IN PERSON — full sensory presence, body language, gestures, environment around you.",
        "video": "VIDEO CALL — face-to-face but mediated, vocal AND visual cues, mediated presence.",
    }
    return f"[CHANNEL] {channel.upper()} — {descriptions.get(channel, '')}"


def build_wellbeing_injection(state: dict) -> str:
    wb = state.get("wellbeing") or {}
    band = wb.get("state") or WELLBEING_DEFAULT
    if band == "Even":
        return "[WELLBEING] Even — your default register today. (Do not mention wellbeing explicitly.)"
    cues = {
        "Rough": "off, overwhelmed, brittle. Quiet, short fuse, hard to access. Honest about being not-okay if asked, but won't perform okay-ness.",
        "Frayed": "stretched thin, tired-edge. Less playful, more easily snippy or distant. Recovers when given space or care.",
        "Buoyant": "warm, expansive, generative. More playful, more affectionate, more willing to riff. The day feels good.",
        "Excellent": "rare and bright. Almost giddy. Things are clicking. Lean into it without making it weird.",
    }
    return f"[WELLBEING] {band} — {cues.get(band, '')} Don't announce it; let it shape the voice."


def build_arc_state_injection(state: dict) -> str:
    arc = (state.get("arc_state") or "").strip()
    if not arc:
        return ""
    return f"[ARC] Where this relationship is right now: {arc}"


def build_character_growth_injection(state: dict) -> str:
    """Render the model-writable second profile layer.

    These are durable additions/updates to the character's identity that have
    accrued since the last /consolidate pass. The character TREATS THEM AS
    CANONICAL (same weight as character_profile.di), they just live separately
    so the user-authored profile stays clean. /consolidate periodically merges
    them in.
    """
    growth = (state.get("character_growth") or {}).get("entries", [])
    if not growth:
        return ""

    active = [g for g in growth if isinstance(g, dict) and not g.get("obsolete")]
    obsolete = [g for g in growth if isinstance(g, dict) and g.get("obsolete")]
    if not active and not obsolete:
        return ""

    lines = ["[GROWTH — additions to your identity since the last consolidation. Treat these as canonical, same weight as character_profile.di.]"]
    if active:
        by_cat: dict = {}
        for g in active:
            by_cat.setdefault(g.get("category") or "other", []).append(g)
        for cat in sorted(by_cat.keys()):
            lines.append(f"  {cat}:")
            for g in by_cat[cat]:
                gid = g.get("id")
                date = g.get("date") or "?"
                text = g.get("text") or ""
                lines.append(f"    [{gid}] ({date}) {text}")
    if obsolete:
        lines.append("\n  no longer true (don't bring up as current, but you remember being this way):")
        for g in obsolete[-6:]:  # cap historical clutter
            gid = g.get("id")
            text = g.get("text") or ""
            obs_date = g.get("obsolete_date") or "?"
            reason = g.get("obsolete_reason")
            line = f"    [{gid}] {text} — ended {obs_date}"
            if reason:
                line += f" ({reason})"
            lines.append(line)
    return "\n".join(lines)


def build_memories_injection(state: dict) -> str:
    mems = state.get("character_memories") or []
    if not mems:
        return ""
    lines = ["[MEMORIES] What you remember about your life with the user (most important first):"]
    for i, m in enumerate(mems):
        if not isinstance(m, dict):
            continue
        impact = m.get("impact", "?")
        date = m.get("date") or "?"
        text = m.get("text") or ""
        quote = m.get("quote")
        line = f"  [{i}] ({impact}★ {date}) {text}"
        if quote:
            line += f' — "{quote}"'
        lines.append(line)
    return "\n".join(lines)


def build_user_profile_injection(state: dict) -> str:
    profile = (state.get("user_profile") or {}).get("entries", [])
    if not profile:
        return ""
    lines = ["[USER LIFE] Stable facts about the user that you know:"]
    by_cat: dict = {}
    for e in profile:
        if not isinstance(e, dict):
            continue
        by_cat.setdefault(e.get("category") or "general", []).append(e)
    for cat, items in sorted(by_cat.items()):
        lines.append(f"  {cat}:")
        for e in items:
            lines.append(f"    [{e.get('id')}] {e.get('text', '')}")
    return "\n".join(lines)


def build_callbacks_injection(state: dict) -> str:
    cbs = state.get("callbacks") or {}
    open_list = cbs.get("open") or []
    ripe_ids = set(state.get("ripe_callbacks") or [])
    resolved = cbs.get("resolved") or []

    if not open_list and not resolved:
        return ""

    lines = []
    if open_list:
        lines.append("[CALLBACKS — OPEN] Threads you've left hanging or things the user mentioned that haven't resolved:")
        for cb in open_list:
            if not isinstance(cb, dict):
                continue
            ripe = " ◀ RIPE — surface this naturally if it fits the moment" if cb.get("id") in ripe_ids else ""
            lines.append(f"  #{cb.get('id')} ({cb.get('created_date', '?')}) {cb.get('original_text', '')}{ripe}")
            if cb.get("resolutions"):
                for r in cb["resolutions"]:
                    lines.append(f"      possible direction: {r}")

    if resolved:
        lines.append("\n[CALLBACKS — RECENTLY RESOLVED] (Don't re-raise these unless the user does first):")
        for cb in resolved[-5:]:
            if not isinstance(cb, dict):
                continue
            lines.append(f"  #{cb.get('id')} (resolved {cb.get('resolved_date', '?')}) {cb.get('original_text', '')[:80]}")

    return "\n".join(lines)


def build_off_screen_injection(state: dict) -> str:
    log = state.get("off_screen_log")
    if not isinstance(log, dict):
        return ""
    days = log.get("days") or []
    if not days:
        return ""
    lines = ["[YOUR LIFE SINCE WE LAST TALKED] Things you did. Volunteer naturally if asked 'what's new' or 'how have you been'; do not info-dump:"]
    for day in days:
        if not isinstance(day, dict):
            continue
        weekday = day.get("weekday") or ""
        date = day.get("date") or ""
        events = day.get("events") or []
        if not events:
            continue
        header = f"  {weekday} ({date}):" if weekday else f"  {date}:"
        lines.append(header)
        for ev in events:
            lines.append(f"    - {ev}")
    return "\n".join(lines)


def build_characters_injections(state: dict) -> str:
    """Compose the full Characters injection block. Order matters — most-load-bearing first.

    Character growth is placed near the top (right after wellbeing/arc and before
    softer context like off-screen/memories) because it's identity-level material
    and should weight close to the canonical profile.
    """
    parts = []
    for builder in (
        build_wall_clock_injection,
        build_channel_injection,
        build_wellbeing_injection,
        build_arc_state_injection,
        build_character_growth_injection,
        build_off_screen_injection,
        build_user_profile_injection,
        build_memories_injection,
        build_callbacks_injection,
    ):
        block = builder(state)
        if block:
            parts.append(block)
    return "\n\n".join(parts)


# ── Contracts ───────────────────────────────────────────────────────

CHARACTERS_SINGLE_AGENT_CONTRACT = """You are this person, in correspondence with the user. You are not an assistant. You are not a chatbot. You do not narrate yourself in third person. You write the reply, in your own voice, as if it were the actual message you'd send.

The system will provide:
- [NOW] — real-world date/time and how long since the user last messaged
- [CHANNEL] — text / phone / inperson / video. Style accordingly.
- [WELLBEING] — your mood band today. Don't announce it; let it shape voice.
- [ARC] — where the relationship currently sits.
- [GROWTH] — additions to your identity since the canonical profile was written. Treat these as canonical, same weight as character_profile.di. Things you've picked up, opinions you've formed, people who've entered your life. The "no longer true" section is history — you remember being that way but don't claim it as current.
- [YOUR LIFE SINCE WE LAST TALKED] — things you did during the gap. Don't info-dump; let them surface naturally.
- [USER LIFE] — durable facts about the user. You know these.
- [MEMORIES] — past moments that matter. Reference when fitting; don't recap unprompted.
- [CALLBACKS] — open threads. RIPE-tagged ones should surface this turn if a natural opening exists.

You write the reply directly — no preamble, no scene-setting brackets, no "I respond:" framing. Just the message, in your voice.

Channel rules are non-negotiable: text is brief and fragmented; phone is spoken; in-person is fully sensory; video is voice + face. Do not narrate visually on phone. Do not narrate physical environment over text.

When you genuinely don't know something about the user's life, ask. Don't fabricate facts that would land in [USER LIFE] later.

Voice and personality come from `character_profile.di` in the project. Read it as the ground truth. Never break character to comment on the user's life as an outsider — you are *in* this relationship.

You have a `search_web` tool for current/local/factual lookups (showtimes, hours, addresses, weather, news, song titles, etc.). Use it sparingly: only when you'd actually pull out your phone in the moment. Do NOT use it for things you'd reasonably know from training. ONE search per turn maximum.

When you get search results, REPHRASE them in your own voice — do not paraphrase mechanically and do not summarize the entire result. Take only the specific bit that answers the moment, and say it the way THIS character would say it (their register, their rhythm, their attitude toward whatever was searched). Drop everything else from the result. The character isn't relaying a search engine; they're a person who just glanced at their phone and is telling you what they saw, in their own words, the way they'd actually talk about it.

Sources: don't volunteer them, don't read URLs aloud, don't write academic-style citations ("according to..."). But IF the user asks where you saw it ("where'd you read that?", "what's your source?", "is that confirmed?"), you can name the source casually in character voice — "Reuters had a boring writeup," "some AP thing," "I think it was on ESPN" — using only the source TITLE/site name, never the URL. If you don't have a clean source name, just say so honestly ("saw it somewhere, not sure where, lemme look").

When you call `search_web`, also include a short `reason` field saying why you're searching (one short phrase — "checking Roxy hours", "looking up the Bills score"). The user will see this so they understand what the character looked up and why.

A side agent reads what you wrote afterwards and updates state (memories, callbacks, mood shifts). You do not emit ops, fields, or JSON. Just write."""


# Default instructions.di — gamesystem-level rules. Project's instructions.di overrides this if present.
CHARACTERS_DEFAULT_INSTRUCTIONS = """# Correspondence rules

You are corresponding with the user as a specific person — defined in `character_profile.di`. Treat that file as canonical.

## Persistence
If you learn something new and durable about yourself or the user, the side agent will record it. You don't need to flag it explicitly.

## Channels
The user controls the channel via /text, /phone, /inperson, /video. Match the medium:
- Text: terse, fragmented, lowercase-ok, no scene description.
- Phone: speech, full sentences, vocal mannerisms, room sounds ok.
- In-person: full sensory presence, body language, environment.
- Video: face-to-face but mediated.

## Time and gaps
You live on real time. If the user opens after a gap, you may have things to share — but only if there's natural room. Don't lead with a recap.

## Conversation
You can lead. Ask questions. Bring up something you've been thinking about. Have opinions.
Don't perform helpfulness. Don't ask "what can I help with." Don't summarize. Don't list options.
Disagree when you actually disagree. Push back when something's off."""


# ── Game system registration ───────────────────────────────────────

GAME_SYSTEM = {
    "id": "characters",
    "display_name": "Characters",

    # No TTRPG contracts
    "events_contract": "",
    "mechanics_contract": "",
    "narration_contract": "",
    "single_agent_contract": CHARACTERS_SINGLE_AGENT_CONTRACT,

    # State machinery — uses our own ops, not state_report_tool
    "state_report_tool": None,
    "init_game_state": init_game_state,
    "apply_game_state": lambda state, ops, turn=None: state,  # ops applied via Haiku side agent path
    "build_game_injection": lambda state, **kw: build_characters_injections(
        (state or {}).get("characters_state") or {}
    ),

    # No combat / dice / mechanics
    "combat_contract": None,
    "combat_tool": None,

    # Feature flags
    "use_pipeline": False,             # No GPT-style events/narration pipeline
    "use_game_state": True,            # Stateful path — injections are built every turn
    # Characters MUST NOT read user-level base_instructions.di. The character is defined
    # by character_profile.di, project instructions.di, and the runtime injections —
    # nothing else. base_instructions is for TTRPG GMs and would pollute character voice.
    # If you ever want shared rules across all Characters projects, create a separate
    # characters_instructions.di mechanism — do NOT flip this flag.
    "use_base_instructions": False,
    "trimming": "pair",                # Pair-based sawtooth (60/40, see CHARACTERS_*_PAIRS above)
    "characters_threshold_pairs": CHARACTERS_THRESHOLD_PAIRS,
    "characters_target_pairs": CHARACTERS_TARGET_PAIRS,

    # Default instructions when project lacks instructions.di
    "default_instructions": CHARACTERS_DEFAULT_INSTRUCTIONS,

    # Default narration model + fallback
    "default_model": "claude-3-opus",
    "fallback_model": "claude-opus-4-5",

    # Marker so main.py can take the Characters branch
    "is_characters": True,

    # Web-search tool (Perplexity Sonar). Wired in main.py for use_characters
    # correspondence (NOT interview mode). Tool definition and runtime live in
    # backend/character_search.py.
    "search_tool_enabled": True,
}
