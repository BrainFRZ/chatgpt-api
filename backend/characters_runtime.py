"""Characters gamesystem runtime — orchestration glue.

Top-level entry points used by main.py:
- is_characters_gamesystem(gs)
- preflight(...) — runs at the start of send-message-stream
- prepare_state(...) — daily rolls, wall clock, ripeness, off-screen
- build_messages(...) — builds system_msg + user_msg for streaming
- run_post_stream(...) — Haiku side agent + state persistence
- finalize_interview(...) — writes character_profile.di
- handle_local_slash(...) — slash commands that don't hit the model

Characters-specific state is split between two locations:
- Per-chat state (channel, wall_clock) lives in pipeline_state["characters_state"]
  on the chat itself.
- Project-wide state (callbacks, wellbeing, arc, life_events,
  last_ripeness_rolled_date) lives in `<project>/character_state.json`,
  managed by character_project_state. The runtime dict from
  get_characters_state() reflects the merged view.
File-backed kinds (memories, user_profile, character_growth) live in
their own jsonl files; see character_storage.py.
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional

from game_systems.characters import (
    init_characters_state,
    apply_callback_ops,
    apply_channel_op,
    apply_arc_state_op,
    maybe_roll_wellbeing,
    roll_callback_ripeness,
    update_wall_clock,
    is_first_message_of_et_day,
    build_characters_injections,
    today_et_iso,
    now_et,
    parse_iso_dt,
    hours_between_iso,
    OFF_SCREEN_GAP_THRESHOLD_HOURS,
    VALID_CHANNELS,
    DEFAULT_CHANNEL,
    CHARACTERS_DEFAULT_INSTRUCTIONS,
)

logger = logging.getLogger(__name__)


# ── Detection helpers ──────────────────────────────────────────────

def is_characters_gamesystem(gs: Optional[dict]) -> bool:
    return bool(gs and gs.get("is_characters"))


def is_in_interview_mode(data: dict) -> bool:
    return bool((data or {}).get("_characters_interview_mode"))


def has_character_profile(project_dir: Optional[str]) -> bool:
    if not project_dir:
        return False
    return os.path.isfile(os.path.join(project_dir, "character_profile.di"))


def get_characters_state(data: dict, project_dir: Optional[str] = None) -> dict:
    """Reach into pipeline_state for characters_state, initializing if needed.

    NOTE: character_memories / user_profile / character_growth used to live here as
    in-state lists. They're now file-backed (see character_storage.py). The fields
    are retained as empty placeholders for migration purposes — when migrate happens,
    legacy entries get copied to files and the fields get cleared.

    callbacks / wellbeing / arc_state / life_events are also project-wide now —
    backed by character_state.json via character_project_state. When project_dir
    is supplied, the file's contents are overlaid on top of the chat-state defaults
    so all chats in the project see the same canonical view of those fields.
    Channel and wall_clock legitimately stay per-chat.
    """
    ps = data.setdefault("pipeline_state", {})
    if not isinstance(ps, dict):
        ps = {}
        data["pipeline_state"] = ps
    cs = ps.get("characters_state")
    if not isinstance(cs, dict):
        cs = init_characters_state()
        ps["characters_state"] = cs
    # Backfill new fields if older state shape
    cs.setdefault("character_memories", [])
    cs.setdefault("callbacks", {"next_id": 1, "open": [], "resolved": [], "dismissed": []})
    cs.setdefault("wellbeing", {"state": "Even", "rolled_date": None, "wb_mod": 0})
    cs.setdefault("arc_state", "")
    cs.setdefault("channel", DEFAULT_CHANNEL)
    cs.setdefault("user_profile", {"next_id": 1, "entries": []})
    cs.setdefault("character_growth", {"next_id": 1, "entries": []})
    # life_events removed in Phase 3 — major events fold into schedule.json /
    # life_stream.jsonl via the planner. Don't backfill the deprecated field.
    cs.setdefault("off_screen_log", None)
    cs.setdefault("wall_clock", {"first_message_at": None, "last_user_message_at": None, "last_message_at": None})
    cs.setdefault("ripe_callbacks", [])
    # Overlay project-level fields from character_state.json. The file is the
    # canonical source of truth for these; chat-state copies are stale snapshots
    # that get refreshed every time this function runs with a project_dir.
    if project_dir:
        from character_project_state import overlay_project_state_into
        overlay_project_state_into(cs, project_dir)
    return cs


def populate_render_payload(
    characters_state: dict,
    project_dir: Optional[str],
    *,
    branch_msg_ids: Optional[set] = None,
    recall: Optional[dict] = None,
) -> None:
    """Stuff a transient _render_payload field into characters_state with file-loaded
    entries that the injection builders read.

    `recall` is the output of the recall agent (dict with memory_ids/profile_ids/
    growth_ids → full entries, branch-filtered). May be None on the very first
    request before recall has run.
    """
    if not isinstance(characters_state, dict):
        return
    payload = {
        "memories_core": [],
        "memories_recalled": [],
        "profile_core": [],
        "profile_recalled": [],
        "growth_active": [],
        "growth_obsolete": [],
        # Life-stream entries recall surfaced. Read by build_life_stream_injection
        # and by the inner_state pre-pass for emotional context.
        "life_stream_recalled": [],
        # Populated by run_inner_state AFTER this function runs (in main.py).
        # Default empty so build_inner_state_injection never KeyErrors when the
        # pre-pass soft-fails or hasn't fired yet on a given code path.
        "inner_state": {},
        # Populated in main.py by walking back through branch_path's recent
        # assistant messages and extracting their inner_state_payload entries.
        # Lets the character reference what she was carrying internally on
        # recent turns ("I was just thinking that"). Default empty list.
        "prior_inner_states": [],
        # Slice of schedule.json events visible to Opus this turn (planned-status
        # events in the current+near-future window). Loaded below from disk.
        "schedule": [],
    }
    if project_dir:
        try:
            from character_storage import CharacterStore, KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH
            store = CharacterStore(project_dir)
            payload["memories_core"] = store.core(KIND_MEMORIES, branch_msg_ids=branch_msg_ids, n=8)
            payload["profile_core"] = store.core(KIND_USER_PROFILE, branch_msg_ids=branch_msg_ids, n=12)
            growth_all = store.read_filtered(KIND_GROWTH, branch_msg_ids)
            payload["growth_active"] = [g for g in growth_all if not g.get("obsolete")]
            payload["growth_obsolete"] = [g for g in growth_all if g.get("obsolete")]
        except Exception as e:
            logger.warning(f"populate_render_payload: store read failed: {e}")

        # Schedule: load schedule.json and slice to two windows:
        #   - Forward (upcoming): planned events whose end is still in the future,
        #     out to 4 days. Includes in-progress events (start past, end future).
        #   - Backward (recently elapsed): as_planned / modified / cancelled
        #     events whose end falls within the last 24h. Critical for skip-roll
        #     days — without this slice, planned events that auto-stamp to
        #     as_planned with empty resolution silently disappear from the
        #     character's view, leaving her unaware that she had a cafe shift
        #     this morning. The injection tags these as past so the model knows
        #     they're behind her, not upcoming.
        try:
            from character_schedule import load_schedule
            schedule = load_schedule(project_dir)
            if isinstance(schedule, dict):
                events_all = schedule.get("events") or []
                now_dt = datetime.now().astimezone()
                forward_end = now_dt + timedelta(days=4)
                back_start = now_dt - timedelta(hours=24)
                in_window = []
                for ev in events_all:
                    if not isinstance(ev, dict):
                        continue
                    status = ev.get("status")
                    when_str = ev.get("when_local")
                    if not isinstance(when_str, str) or not when_str:
                        continue
                    try:
                        ev_start = datetime.fromisoformat(when_str)
                        if ev_start.tzinfo is None:
                            ev_start = ev_start.astimezone()
                        ev_end = ev_start + timedelta(minutes=int(ev.get("duration_min") or 60))
                    except (ValueError, TypeError):
                        continue

                    if status == "planned":
                        # Forward-looking: still upcoming or in-progress
                        if ev_end > now_dt and ev_start <= forward_end:
                            in_window.append(ev)
                    elif status in ("as_planned", "modified", "cancelled"):
                        # Backward-looking: elapsed within last 24h
                        if back_start <= ev_end <= now_dt:
                            in_window.append(ev)
                # Sort chronologically
                in_window.sort(key=lambda e: e.get("when_local") or "")
                payload["schedule"] = in_window
        except Exception as e:
            logger.warning(f"populate_render_payload: schedule load failed: {e}")
    if isinstance(recall, dict):
        payload["memories_recalled"] = recall.get("recalled_memories") or []
        payload["profile_recalled"] = recall.get("recalled_profile") or []
        payload["life_stream_recalled"] = recall.get("recalled_life_stream") or []
        # Growth: recall may add to active set (Haiku might surface obsolete entries
        # that are still narratively relevant — but in practice the recall agent
        # avoids these per its prompt; safe to ignore here).
    characters_state["_render_payload"] = payload


def clear_render_payload(characters_state: dict) -> None:
    """Strip the transient render payload before persisting state to disk."""
    if isinstance(characters_state, dict):
        characters_state.pop("_render_payload", None)


def maybe_migrate_storage(data: dict, project_dir: Optional[str]) -> dict:
    """One-shot migrate of legacy in-state memory/profile/growth → jsonl files.

    Idempotent — does nothing if files already exist with content. Returns the
    migration report (counts) for logging.
    """
    if not project_dir:
        return {}
    try:
        from character_storage import CharacterStore, migrate_state_to_files
        store = CharacterStore(project_dir)
        cs = get_characters_state(data, project_dir=project_dir)
        return migrate_state_to_files(cs, store)
    except Exception as e:
        logger.warning(f"maybe_migrate_storage: failed: {e}")
        return {}


def branch_msg_ids_from_branch_path(branch_path: list) -> set:
    """Extract message ids from a branch_path list. Used to build the branch-scope
    set for filtering file-backed entries.
    """
    out: set = set()
    if not isinstance(branch_path, list):
        return out
    for m in branch_path:
        if isinstance(m, dict):
            mid = m.get("id")
            if mid is not None:
                out.add(mid)
    return out


# ── Slash commands (no model call) ─────────────────────────────────

def handle_local_slash(message: str, data: dict, project_dir: Optional[str]) -> Optional[dict]:
    """Handle Characters slash commands that don't require the model.

    Returns None if not a local slash command (proceed normally).
    Returns a dict with structured result if handled:
      {"kind": "channel_set", "channel": str, "feedback": str}
      {"kind": "callback_resolved", "id": int, "feedback": str}
      {"kind": "callback_dismissed", "id": int, "feedback": str}
      {"kind": "reinterview_started", "feedback": str}
      {"kind": "error", "feedback": str}
    """
    if not isinstance(message, str):
        return None
    stripped = message.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Slash commands run BEFORE the main turn flow, so they're the earliest
    # mutation point — load with project overlay so any project-level fields
    # they touch (callbacks via /resolve and /dismiss, life_events via
    # /seed-event) start from the canonical view.
    cs = get_characters_state(data, project_dir=project_dir)
    today = today_et_iso()

    # Channel commands
    channel_aliases = {"/text": "text", "/phone": "phone", "/inperson": "inperson", "/video": "video"}
    if cmd in channel_aliases:
        new_channel = channel_aliases[cmd]
        cs["channel"] = apply_channel_op(cs.get("channel"), {"action": "set", "value": new_channel})
        return {
            "kind": "channel_set",
            "channel": new_channel,
            "feedback": f"Channel set to {new_channel.upper()}.",
        }

    if cmd == "/channel":
        new_channel = arg.lower().strip()
        if new_channel not in VALID_CHANNELS:
            return {
                "kind": "error",
                "feedback": f"Unknown channel '{arg}'. Valid: {', '.join(VALID_CHANNELS)}.",
            }
        cs["channel"] = apply_channel_op(cs.get("channel"), {"action": "set", "value": new_channel})
        return {
            "kind": "channel_set",
            "channel": new_channel,
            "feedback": f"Channel set to {new_channel.upper()}.",
        }

    # Callback resolve / dismiss
    if cmd in ("/resolve", "/dismiss"):
        try:
            cb_id = int(arg.split()[0]) if arg else None
        except (ValueError, IndexError):
            cb_id = None
        if cb_id is None:
            return {"kind": "error", "feedback": f"Usage: {cmd} <id> [text]"}
        rest = arg.split(maxsplit=1)[1] if len(arg.split(maxsplit=1)) > 1 else ""
        # Verify the callback actually exists on the open list before applying — otherwise
        # apply_callback_ops silently no-ops and the user gets misleading "resolved" feedback.
        open_ids = {
            cb.get("id")
            for cb in (cs.get("callbacks") or {}).get("open", [])
            if isinstance(cb, dict)
        }
        if cb_id not in open_ids:
            return {
                "kind": "error",
                "feedback": f"No open callback with id #{cb_id}. Use the [CALLBACKS — OPEN] list.",
            }
        action = "resolve" if cmd == "/resolve" else "dismiss"
        op = {"action": action, "id": cb_id}
        if action == "resolve":
            op["resolution_text"] = rest
        else:
            op["reason"] = rest
        cs["callbacks"] = apply_callback_ops(cs.get("callbacks") or {}, [op], 0, today)
        # Persist project-level state so the resolution is visible across all chats
        from character_project_state import persist_project_state_from
        persist_project_state_from(cs, project_dir)
        return {
            "kind": f"callback_{action}d",
            "id": cb_id,
            "feedback": f"Callback #{cb_id} {action}d.",
        }

    # Accept / reject a pending consolidation proposal — these are state-only,
    # no model call needed. The /consolidate command itself is short-circuited
    # by preflight.finalize-style routing in main.py since it requires an Opus call.
    if cmd == "/accept-consolidation":
        proposal = data.get("_consolidation_proposal")
        if not isinstance(proposal, dict) or not proposal.get("proposed_profile"):
            return {
                "kind": "error",
                "feedback": "No pending consolidation. Run /consolidate first.",
            }
        if not project_dir:
            return {"kind": "error", "feedback": "No project directory; cannot write profile."}
        profile_path = os.path.join(project_dir, "character_profile.di")
        # Back up the current profile before overwrite
        try:
            if os.path.isfile(profile_path):
                from datetime import datetime as _dt
                stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
                backup_path = os.path.join(project_dir, f"character_profile.di.bak.{stamp}")
                with open(profile_path, "r", encoding="utf-8") as fp_old:
                    old = fp_old.read()
                with open(backup_path, "w", encoding="utf-8") as fp_bak:
                    fp_bak.write(old)
            with open(profile_path, "w", encoding="utf-8") as fp_new:
                fp_new.write(proposal["proposed_profile"])
        except OSError as e:
            return {"kind": "error", "feedback": f"Failed to write profile: {e}"}

        # Drop merged growth entries from file storage (file-backed since storage migration)
        merged_ids = list(proposal.get("merged_growth_ids") or [])
        deleted_count = 0
        if merged_ids:
            try:
                from character_storage import CharacterStore, KIND_GROWTH
                store = CharacterStore(project_dir)
                deleted_count = store.delete_many(KIND_GROWTH, [int(i) for i in merged_ids if isinstance(i, (int, str))])
            except Exception as e:
                logger.warning(f"/accept-consolidation: growth delete failed: {e}")

        data.pop("_consolidation_proposal", None)
        return {
            "kind": "consolidation_accepted",
            "feedback": (
                f"Consolidation accepted. character_profile.di updated; "
                f"{deleted_count} growth entries merged in and cleared. "
                f"Backup saved alongside the original."
            ),
        }

    if cmd == "/reject-consolidation":
        proposal = data.get("_consolidation_proposal")
        if not isinstance(proposal, dict):
            return {
                "kind": "error",
                "feedback": "No pending consolidation to reject.",
            }
        data.pop("_consolidation_proposal", None)
        return {
            "kind": "consolidation_rejected",
            "feedback": "Consolidation discarded. Profile unchanged; growth entries kept.",
        }

    # Manual event seed: lets the user plant a specific life event for the
    # writes directly to life_stream as a major_event entry. Recall (Haiku) and
    # off-screen will surface it the same way they handle resolver-written events.
    if cmd == "/seed-event":
        hint = arg.strip()
        if not hint:
            return {
                "kind": "error",
                "feedback": "Usage: /seed-event <what happened to the character>. Example: /seed-event her cat ran away",
            }
        if not project_dir:
            return {"kind": "error", "feedback": "/seed-event requires a project."}
        try:
            from character_schedule import append_life_stream
            from datetime import datetime as _dt
            now_iso = _dt.now().astimezone().isoformat(timespec="seconds")
            append_life_stream(project_dir, {
                "id": f"ls-{today}-seed-{abs(hash(hint)) % 100000:05d}",
                "at_local": now_iso,
                "kind": "major_event",
                "ref": None,
                "summary": hint[:600],
                "tone": "even",
                "available_to_recall": True,
                "source": "manual",
            })
        except Exception as e:
            logger.error(f"/seed-event: failed to write life_stream entry: {e}")
            return {"kind": "error", "feedback": f"Failed to seed event: {e}"}
        return {
            "kind": "event_seeded",
            "feedback": (
                f"Event seeded: \"{hint[:120]}\". The character will incorporate this naturally "
                "via recall (when relevant to your next message) or via off-screen narration "
                "(if you come back after a gap)."
            ),
        }

    # Reinterview
    if cmd == "/reinterview":
        data["_characters_interview_mode"] = True
        # The interviewer gets a fresh context (no prior chat), so the OOC just needs
        # to flag this is a *re*-interview against an existing profile rather than a
        # cold-start. The interview agent will read character_profile.di on its own.
        return {
            "kind": "reinterview_started",
            "feedback": (
                "Re-interview started. An existing character_profile.di is in place — "
                "read it to identify thin sections (missing voice samples, anti-persona, "
                "etc.) and offer to focus the re-interview there. Type /finalize when done."
            ),
        }

    return None


# ── Preflight ──────────────────────────────────────────────────────

class CharactersPreflightResult:
    """Encapsulates the preflight decision."""
    __slots__ = ("hard_fail", "interview_mode", "finalize", "consolidate", "local_slash", "_message")

    def __init__(self):
        self.hard_fail: Optional[str] = None         # banner text if hard-failing
        self.interview_mode: bool = False             # route to interview agent
        self.finalize: bool = False                   # /finalize triggered
        self.consolidate: bool = False                # /consolidate triggered (Opus 4.5 call)
        self.local_slash: Optional[dict] = None       # slash result if handled locally
        self._message: str = ""

    def is_hard_fail(self) -> bool:
        return self.hard_fail is not None

    def is_short_circuit(self) -> bool:
        """Either we're hard-failing or a local slash handled the message."""
        return self.is_hard_fail() or self.local_slash is not None or self.finalize or self.consolidate


def preflight(gs: dict, data: dict, project_dir: Optional[str], message: str) -> CharactersPreflightResult:
    """Decide how the request should flow for a Characters chat. Mutates `data` for slash side effects."""
    result = CharactersPreflightResult()

    if not is_characters_gamesystem(gs):
        return result

    # Slash commands that don't need the model
    slash_result = handle_local_slash(message, data, project_dir)
    if slash_result is not None:
        result.local_slash = slash_result
        return result

    # /finalize during interview
    if is_in_interview_mode(data) and message.strip().lower().startswith("/finalize"):
        result.finalize = True
        return result

    # /consolidate (correspondence mode only — meaningless in interview)
    if not is_in_interview_mode(data) and message.strip().lower().startswith("/consolidate"):
        result.consolidate = True
        return result

    profile_exists = has_character_profile(project_dir)
    interview_active = is_in_interview_mode(data)

    # In interview mode → route to interview agent
    if interview_active:
        result.interview_mode = True
        return result

    # Not in interview mode + no profile → hard fail
    if not profile_exists:
        result.hard_fail = (
            "No character_profile.di found for this project. Type /reinterview to start the interview "
            "and create the profile."
        )
        return result

    # Normal correspondence
    return result


# ── State preparation (per-turn) ────────────────────────────────────

def prepare_state_for_turn(
    data: dict,
    project_dir: Optional[str],
    *,
    rng_seed: Optional[int] = None,
) -> dict:
    """Run daily rolls if first message of ET day; return characters_state.

    NOTE: This intentionally does NOT update wall_clock — that must happen
    AFTER `build_characters_injections` runs so the [NOW] injection's
    silence-streak calculation sees the *previous* last_user_message_at,
    not the timestamp we're about to overwrite. Caller invokes
    `stamp_user_turn()` after building injections.

    Off-screen log generation is NOT here — it's a separate API call run in
    build_messages (it needs the Anthropic client + must run before rolls so
    it sees the pre-turn wall_clock).
    """
    # Load with project-state overlay so wellbeing/callbacks/life_events
    # already reflect the canonical project view before we roll/mutate.
    cs = get_characters_state(data, project_dir=project_dir)
    wc = cs.get("wall_clock") or {}
    project_state_dirty = False
    today_iso = today_et_iso()

    # Wellbeing + life-event rolls already self-gate (rolled_date / last_rolled_year_week).
    # Ripeness needs an explicit project-level gate (last_ripeness_rolled_date) since the
    # roll function itself doesn't track date — without this, every chat's first-message-
    # of-ET-day rerolls the ripeness from the same callback pool, giving each chat a
    # different "ripe today" set for the same character. See bug review P1.2.
    if is_first_message_of_et_day(wc):
        rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
        cs["wellbeing"] = maybe_roll_wellbeing(cs.get("wellbeing") or {}, today_iso, rng=rng)
        # Project-wide ripeness gate
        if cs.get("last_ripeness_rolled_date") != today_iso:
            cs["ripe_callbacks"] = roll_callback_ripeness(cs.get("callbacks") or {}, today_iso, rng=rng)
            cs["last_ripeness_rolled_date"] = today_iso
        # Daily prune of resolved/dismissed callbacks (30-day retention) — runs even if
        # no callback_ops are emitted today, otherwise old resolved entries can linger
        # indefinitely on quiet days.
        cs["callbacks"] = apply_callback_ops(cs.get("callbacks") or {}, [], 0, today_iso)
        # Weekly major-event roll moved to character_planner (Phase 3) — fires
        # via the Sunday APScheduler cron, not on first-message-of-day.
        project_state_dirty = True  # wellbeing/callbacks/ripeness-date all touched
        # Hygiene: archive stale low-impact memories. Cheap (file rewrite, no
        # model calls). Memories with impact >= 3 are permanent; impact 1-2
        # memories untouched for 12+ months get soft-archived.
        if project_dir:
            try:
                from character_storage import CharacterStore, prune_stale_memories
                report = prune_stale_memories(CharacterStore(project_dir), today_iso)
                if report.get("archived"):
                    logger.info(f"prepare_state_for_turn: archived {report['archived']} stale memories")
            except Exception as e:
                logger.warning(f"prepare_state_for_turn: hygiene failed: {e}")
    else:
        # Mid-day: keep wellbeing as is; clear ripe_callbacks so they don't surface every turn
        cs.setdefault("ripe_callbacks", [])
        cs["ripe_callbacks"] = []

    if project_state_dirty:
        from character_project_state import persist_project_state_from
        persist_project_state_from(cs, project_dir)

    return cs


def stamp_user_turn(characters_state: dict) -> dict:
    """Stamp wall_clock for the user message we just processed. Call AFTER injections are built."""
    if not isinstance(characters_state, dict):
        return characters_state
    characters_state["wall_clock"] = update_wall_clock(
        characters_state.get("wall_clock") or {}, user_message=True
    )
    return characters_state


def maybe_generate_off_screen(client, characters_state: dict, project_dir: Optional[str]) -> Optional[dict]:
    """If gap since last_user_message_at exceeds threshold, build the off-screen
    log from life_stream entries (deterministic — no API call). Returns None
    when no gap or when life_stream had no entries for the gap window.

    This MUST run before prepare_state_for_turn updates last_user_message_at —
    caller is responsible for ordering. `client` arg is unused (kept for the
    pre-Phase-2 signature compatibility).
    """
    from character_off_screen import generate_off_screen_log, should_generate_off_screen
    wc = characters_state.get("wall_clock") or {}
    if not should_generate_off_screen(wc):
        return None
    log, usage = generate_off_screen_log(client, project_dir, characters_state)
    if log:
        characters_state["off_screen_log"] = log
    return {"log": log, "usage": usage} if log else None


# ── Message building ────────────────────────────────────────────────

def _read_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ""


def build_system_content(gs: dict, project_dir: Optional[str], original_system_content: str, *, interview_mode: bool) -> str:
    """Assemble the system prompt for a Characters turn.

    Correspondence mode layers (top to bottom):
    1. Single-agent contract
    2. instructions.di (or default if missing)
    3. character_profile.di
    4. user_life.di (if present)

    Interview mode layers:
    1. INTERVIEW_SYSTEM_PROMPT
    2. Existing character_profile.di — included only when present (signals re-interview).
       The interview agent reads it to identify thin sections rather than starting cold.
       On a fresh chat with no profile yet, this layer is omitted and the agent runs
       a first-time interview.
    """
    if interview_mode:
        from character_interview import INTERVIEW_SYSTEM_PROMPT
        parts = [INTERVIEW_SYSTEM_PROMPT]
        existing_profile = _read_file(os.path.join(project_dir or "", "character_profile.di"))
        if existing_profile:
            parts.append(
                "# Existing character_profile.di (this is a RE-INTERVIEW)\n\n"
                "Below is the current canonical profile for this character. Your job in this "
                "re-interview is to:\n"
                "1. Identify thin or missing sections (especially voice samples, anti-persona, "
                "edges, mood-band cues — common gaps from light first-pass interviews).\n"
                "2. Offer the user a choice of which section(s) to focus on, or run a quick "
                "audit pass across the whole profile.\n"
                "3. Preserve confirmed material — don't re-litigate sections the user is "
                "happy with. Confirm before overwriting any specific phrasing.\n\n"
                "When `/finalize` is called, the finalize step will write a NEW profile based "
                "on the full re-interview transcript, so make sure the user explicitly confirms "
                "anything from the existing profile that they want to keep verbatim — otherwise "
                "the finalize call may rewrite it.\n\n"
                "---\n\n"
                + existing_profile.strip()
            )
        existing_user_life = _read_file(os.path.join(project_dir or "", "user_life.di"))
        if existing_user_life:
            parts.append("# user_life.di (for consistency, do not contradict)\n\n" + existing_user_life.strip())
        return "\n\n".join(parts)

    parts = [gs.get("single_agent_contract", "")]

    # original_system_content is what build_system_content() in main.py produced —
    # for Characters projects this is typically the user's instructions.di, or
    # the gamesystem's default if absent. The gamesystem-level default lives in
    # CHARACTERS_DEFAULT_INSTRUCTIONS.
    if original_system_content and original_system_content.strip():
        parts.append(original_system_content.strip())
    else:
        parts.append(CHARACTERS_DEFAULT_INSTRUCTIONS)

    profile_doc = _read_file(os.path.join(project_dir or "", "character_profile.di"))
    if profile_doc:
        parts.append("# Character profile (canonical — read as ground truth)\n\n" + profile_doc.strip())

    user_life_doc = _read_file(os.path.join(project_dir or "", "user_life.di"))
    if user_life_doc:
        parts.append("# What you know about the user\n\n" + user_life_doc.strip())

    return "\n\n".join(p for p in parts if p)


def build_user_message_content(characters_state: dict, raw_user_content: str) -> str:
    """Prepend Characters injections to the user message."""
    injections = build_characters_injections(characters_state)
    if injections:
        return injections + "\n\n" + raw_user_content
    return raw_user_content


# ── Post-stream side agent ──────────────────────────────────────────

def run_post_stream_extraction(
    client,
    project_dir: Optional[str],
    characters_state: dict,
    user_input: str,
    character_reply: str,
    current_turn: int,
    *,
    branch_msg_id: Optional[str] = None,
    branch_msg_ids: Optional[set] = None,
) -> dict:
    """Run the character_agent (Sonnet 4.6) post-pass + apply ops.

    branch_msg_id: tags newly-created file-backed entries (memories/profile/growth)
      so they're scoped to this branch. Use the user_msg_id of this turn.
    branch_msg_ids: scope set the agent uses when reading existing entries to
      decide what to drop / update (avoid touching out-of-branch entries).
    """
    from character_agent import (
        determine_character_ops,
        apply_character_ops_to_state,
        compute_character_agent_cost,
    )
    from character_project_state import overlay_project_state_into, persist_project_state_from

    # Refresh project-level fields from disk before applying ops. The main
    # turn loaded these at turn-start, but Opus 3 streaming + recall + off-screen
    # took 30-180s in the meantime — another chat in the same project could
    # have updated callbacks/wellbeing/etc during that window. Re-overlaying
    # closes the staleness gap so our ops layer onto the freshest state.
    overlay_project_state_into(characters_state, project_dir)

    ops, usage = determine_character_ops(
        client,
        project_dir,
        characters_state,
        user_input,
        character_reply,
        branch_msg_ids=branch_msg_ids,
    )
    if ops:
        apply_character_ops_to_state(
            characters_state, ops, current_turn, today_et_iso(),
            project_dir=project_dir, branch_msg_id=branch_msg_id,
        )
        # Persist project-level fields (callbacks/wellbeing/arc/life_events) so
        # any side-agent ops that touched them propagate across all chats.
        persist_project_state_from(characters_state, project_dir)

        # Dispatch schedule_ops to schedule.json (separate file with its own
        # lock+atomic-rewrite pattern, so it doesn't ride on persist_project_state_from).
        sched_ops = ops.get("schedule_ops") if isinstance(ops, dict) else None
        if sched_ops and project_dir:
            try:
                from character_schedule import apply_schedule_ops
                apply_schedule_ops(project_dir, sched_ops, source="chat")
            except Exception as e:
                logger.warning(f"character_agent: schedule_ops dispatch failed: {e}")

    # Stamp the wall_clock with assistant message timestamp
    characters_state["wall_clock"] = update_wall_clock(
        characters_state.get("wall_clock") or {}, user_message=False
    )

    cost = compute_character_agent_cost(usage) if usage else 0.0
    return {
        "character_agent_ops": ops,
        "character_agent_usage": usage,
        "character_agent_cost": cost,
        "character_agent_model": "claude-sonnet-4-6",
    }


# ── Interview finalization ─────────────────────────────────────────

def run_consolidate(
    client,
    data: dict,
    project_dir: Optional[str],
    *,
    branch_msg_ids: Optional[set] = None,
) -> dict:
    """Run the consolidate Opus 4.5 call. Stores the proposal on data for /accept-consolidation
    or /reject-consolidation to act on later. Returns a dict for the synthetic message:
      {ok: bool, feedback: str, commentary: str, proposed_profile: str, merged_growth_ids: list, elevated_memory_ids: list, usage: dict, cost: float}

    branch_msg_ids: scope the consolidation to entries visible from the user's current
    branch. Excludes archived memories by default (those are explicitly faded from
    canon by the hygiene pass — they shouldn't graduate). If None, uses unfiltered
    reads (legacy / test path).
    """
    from character_consolidate import run_consolidation, compute_consolidate_cost

    if not project_dir:
        return {"ok": False, "feedback": "Cannot consolidate: no project directory."}
    profile_path = os.path.join(project_dir, "character_profile.di")
    profile_doc = _read_file(profile_path)
    if not profile_doc:
        return {"ok": False, "feedback": "Cannot consolidate: character_profile.di not found."}

    cs = get_characters_state(data, project_dir=project_dir)

    # Read growth + memories from file storage. Branch-filtered (so abandoned
    # experimental branches don't pollute canon) and excluding archived (those
    # are explicitly faded — we don't want them graduated back).
    from character_storage import CharacterStore, KIND_MEMORIES, KIND_GROWTH
    store = CharacterStore(project_dir)
    if branch_msg_ids is not None:
        growth_entries = store.read_filtered(KIND_GROWTH, branch_msg_ids)
        memories = store.read_filtered(KIND_MEMORIES, branch_msg_ids)
    else:
        # No branch context provided — still exclude archived
        growth_entries = [e for e in store.read_all(KIND_GROWTH) if not e.get("archived")]
        memories = [e for e in store.read_all(KIND_MEMORIES) if not e.get("archived")]

    # Reshape growth_entries into the legacy {next_id, entries} dict the
    # consolidate agent expects (since its prompt was written against that shape).
    growth_state = {"next_id": (max([g.get("id", 0) for g in growth_entries], default=0) + 1), "entries": growth_entries}

    # Bail early if there's literally nothing to consolidate
    growth_active = [g for g in growth_entries if isinstance(g, dict) and not g.get("obsolete")]
    if not growth_active and not memories:
        return {
            "ok": False,
            "feedback": (
                "Nothing to consolidate yet — no growth entries and no memories. "
                "Have a few correspondence turns first; the side agent will start logging."
            ),
        }

    result, usage = run_consolidation(client, profile_doc, growth_state, memories)
    cost = compute_consolidate_cost(usage) if usage else 0.0
    if not result:
        return {
            "ok": False,
            "usage": usage or {},
            "cost": cost,
            "feedback": "Consolidation call failed or returned no usable proposal. Try again later.",
        }

    proposal = {
        "proposed_profile": result["proposed_profile"],
        "merged_growth_ids": result.get("merged_growth_ids") or [],
        "elevated_memory_ids": result.get("elevated_memory_ids") or [],
        "commentary": result.get("commentary") or "",
        "created_at": now_et().isoformat(),
    }
    data["_consolidation_proposal"] = proposal

    feedback_parts = ["## Consolidation proposal\n"]
    if proposal["commentary"]:
        feedback_parts.append(proposal["commentary"].strip())
        feedback_parts.append("")
    feedback_parts.append("---\n")
    feedback_parts.append(proposal["proposed_profile"])
    feedback_parts.append("\n---\n")
    feedback_parts.append(
        "Type **/accept-consolidation** to commit (writes character_profile.di, backs up the old one, "
        "clears merged growth entries) or **/reject-consolidation** to discard."
    )
    feedback = "\n".join(feedback_parts)

    return {
        "ok": True,
        "feedback": feedback,
        "commentary": proposal["commentary"],
        "proposed_profile": proposal["proposed_profile"],
        "merged_growth_ids": proposal["merged_growth_ids"],
        "elevated_memory_ids": proposal["elevated_memory_ids"],
        "usage": usage or {},
        "cost": cost,
    }


def finalize_interview(client, data: dict, project_dir: Optional[str], transcript: list) -> dict:
    """Run finalize_profile, write character_profile.di, flip out of interview mode.

    On a re-interview the existing profile is loaded and passed to the finalize agent
    so untouched sections are preserved verbatim.

    Returns: {"ok": bool, "path": str, "profile": str, "usage": dict, "cost": float, "feedback": str}
    """
    from character_interview import finalize_profile, write_profile_file, compute_interview_cost

    if not project_dir:
        return {"ok": False, "feedback": "Cannot finalize: no project directory."}

    existing_profile = _read_file(os.path.join(project_dir, "character_profile.di")) or None
    profile, bands_proposal, usage = finalize_profile(client, transcript, existing_profile=existing_profile)
    cost = compute_interview_cost(usage) if usage else 0.0

    if not profile:
        return {
            "ok": False,
            "usage": usage,
            "cost": cost,
            "feedback": "Finalization failed — the interview agent did not produce a usable profile. Try again or continue the interview.",
        }

    # Auto-commit the bands proposal to character_state.json so the resolver
    # has something to work with even if the user never opens the modal. The
    # modal becomes a "review and adjust" surface; dismissing leaves the
    # auto-committed values in place. Defaults to None if the model didn't
    # emit a valid proposal — in that case the planner's seed call will
    # generate a week without major events (resolver bails on no bands per
    # Phase 2 P1 fix), and the user gets prompted to review anyway.
    if bands_proposal:
        try:
            from character_project_state import load_project_state, save_project_state
            existing_state = load_project_state(project_dir) or {}
            existing_state["flakiness_bands"] = bands_proposal
            save_project_state(project_dir, existing_state)
            logger.info(
                f"finalize_interview: auto-committed flakiness_bands proposal "
                f"({len(bands_proposal)} categories) for {os.path.basename(project_dir)}"
            )
        except Exception as e:
            logger.warning(f"finalize_interview: failed to auto-commit bands: {e}")

    try:
        path = write_profile_file(project_dir, profile)
    except Exception as e:
        logger.error(f"finalize_interview: write failed: {e}")
        return {
            "ok": False,
            "usage": usage,
            "cost": cost,
            "feedback": f"Finalization wrote the profile but file write failed: {e}",
        }

    data["_characters_interview_mode"] = False

    # Register the resolver + planner jobs for this newly-completed character.
    # Without this, scheduler.register_all_projects() only runs at app startup,
    # so a character interviewed mid-session wouldn't get scheduled jobs until
    # the next restart. Soft-fail: if the scheduler isn't running (apscheduler
    # not installed, or test context), this is a no-op.
    try:
        from scheduler import register_resolver_for_project, register_planner_for_project
        register_resolver_for_project(project_dir)
        register_planner_for_project(project_dir)
    except Exception as e:
        logger.warning(f"finalize_interview: failed to register scheduler jobs: {e}")

    # Seed the initial week immediately. Without this, a freshly-interviewed
    # character has no schedule until the next Sunday cron fires (up to 7
    # days away). Run synchronously with the existing client; cost is one
    # Sonnet 4.6 call (~$0.01-0.02). Stamps first_seen_date and generates
    # the current week's events.
    try:
        from character_planner import run_weekly_planner, this_week_monday
        from game_systems.characters import now_et
        seed_meta = run_weekly_planner(
            client, project_dir,
            now_dt=now_et(),
            week_of=this_week_monday(now_et()),
        )
        if seed_meta.get("skipped"):
            logger.info(f"finalize_interview: initial planner pass skipped — {seed_meta.get('reason')}")
        else:
            logger.info(
                f"finalize_interview: seeded initial week ({seed_meta.get('events_planned', 0)} events) "
                f"for {os.path.basename(project_dir)}"
            )
    except Exception as e:
        logger.warning(f"finalize_interview: initial planner seed failed: {e}")

    feedback = "Interview finalized. character_profile.di has been written. Send your next message to begin correspondence."
    if bands_proposal:
        feedback += " Click 'Review follow-through' below to adjust how reliable the character is across work, social plans, and other commitments."

    return {
        "ok": True,
        "path": path,
        "profile": profile,
        "flakiness_bands_proposal": bands_proposal,  # surfaced to ChatView for the modal
        "usage": usage,
        "cost": cost,
        "feedback": feedback,
    }
