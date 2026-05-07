"""Characters gamesystem runtime — orchestration glue.

Top-level entry points used by main.py:
- is_characters_gamesystem(gs)
- preflight(...) — runs at the start of send-message-stream
- prepare_state(...) — daily rolls, wall clock, ripeness, off-screen
- build_messages(...) — builds system_msg + user_msg for streaming
- run_post_stream(...) — Haiku side agent + state persistence
- finalize_interview(...) — writes character_profile.di
- handle_local_slash(...) — slash commands that don't hit the model

All Characters-specific state lives in pipeline_state["characters_state"].
"""
from __future__ import annotations

import logging
import os
import random
from datetime import datetime
from typing import Optional

from game_systems.characters import (
    init_characters_state,
    apply_callback_ops,
    apply_channel_op,
    apply_arc_state_op,
    apply_user_profile_ops,
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


def get_characters_state(data: dict) -> dict:
    """Reach into pipeline_state for characters_state, initializing if needed."""
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
    cs.setdefault("off_screen_log", None)
    cs.setdefault("wall_clock", {"first_message_at": None, "last_user_message_at": None, "last_message_at": None})
    cs.setdefault("ripe_callbacks", [])
    return cs


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

    cs = get_characters_state(data)
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

        # Drop merged growth entries from state
        merged_ids = set(proposal.get("merged_growth_ids") or [])
        if merged_ids:
            growth = cs.get("character_growth") or {"next_id": 1, "entries": []}
            growth["entries"] = [
                g for g in growth.get("entries", [])
                if isinstance(g, dict) and g.get("id") not in merged_ids
            ]
            cs["character_growth"] = growth

        data.pop("_consolidation_proposal", None)
        return {
            "kind": "consolidation_accepted",
            "feedback": (
                f"Consolidation accepted. character_profile.di updated; "
                f"{len(merged_ids)} growth entries merged in and cleared. "
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
    cs = get_characters_state(data)
    wc = cs.get("wall_clock") or {}

    if is_first_message_of_et_day(wc):
        rng = random.Random(rng_seed) if rng_seed is not None else random.Random()
        cs["wellbeing"] = maybe_roll_wellbeing(cs.get("wellbeing") or {}, today_et_iso(), rng=rng)
        cs["ripe_callbacks"] = roll_callback_ripeness(cs.get("callbacks") or {}, today_et_iso(), rng=rng)
        # Daily prune of resolved/dismissed callbacks (30-day retention) — runs even if
        # no callback_ops are emitted today, otherwise old resolved entries can linger
        # indefinitely on quiet days.
        cs["callbacks"] = apply_callback_ops(cs.get("callbacks") or {}, [], 0, today_et_iso())
    else:
        # Mid-day: keep wellbeing as is; clear ripe_callbacks so they don't surface every turn
        cs.setdefault("ripe_callbacks", [])
        cs["ripe_callbacks"] = []

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
    """If gap since last_user_message_at exceeds threshold, call Opus 4.5 to fill in.

    This MUST run before prepare_state_for_turn updates last_user_message_at — caller
    is responsible for ordering.
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
) -> dict:
    """Run Haiku 4.5 character_agent + apply ops. Returns metadata dict for telemetry."""
    from character_agent import (
        determine_character_ops,
        apply_character_ops_to_state,
        compute_character_agent_cost,
    )

    ops, usage = determine_character_ops(
        client,
        project_dir,
        characters_state,
        user_input,
        character_reply,
    )
    if ops:
        apply_character_ops_to_state(characters_state, ops, current_turn, today_et_iso())

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

def run_consolidate(client, data: dict, project_dir: Optional[str]) -> dict:
    """Run the consolidate Opus 4.5 call. Stores the proposal on data for /accept-consolidation
    or /reject-consolidation to act on later. Returns a dict for the synthetic message:
      {ok: bool, feedback: str, commentary: str, proposed_profile: str, merged_growth_ids: list, elevated_memory_ids: list, usage: dict, cost: float}
    """
    from character_consolidate import run_consolidation, compute_consolidate_cost

    if not project_dir:
        return {"ok": False, "feedback": "Cannot consolidate: no project directory."}
    profile_path = os.path.join(project_dir, "character_profile.di")
    profile_doc = _read_file(profile_path)
    if not profile_doc:
        return {"ok": False, "feedback": "Cannot consolidate: character_profile.di not found."}

    cs = get_characters_state(data)
    growth_state = cs.get("character_growth") or {"next_id": 1, "entries": []}
    memories = cs.get("character_memories") or []

    # Bail early if there's literally nothing to consolidate
    growth_active = [g for g in growth_state.get("entries", []) if isinstance(g, dict) and not g.get("obsolete")]
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
    profile, usage = finalize_profile(client, transcript, existing_profile=existing_profile)
    cost = compute_interview_cost(usage) if usage else 0.0

    if not profile:
        return {
            "ok": False,
            "usage": usage,
            "cost": cost,
            "feedback": "Finalization failed — the interview agent did not produce a usable profile. Try again or continue the interview.",
        }

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
    return {
        "ok": True,
        "path": path,
        "profile": profile,
        "usage": usage,
        "cost": cost,
        "feedback": "Interview finalized. character_profile.di has been written. Send your next message to begin correspondence.",
    }
