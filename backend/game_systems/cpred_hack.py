"""CPRED hack mode — standalone NET encounters (Quick Hacks and Full Runs)."""

import logging
import random

from .cpred_mechanics import _resolve_jack_out_cascade
from .cpred_tables import ICE_STAT_BLOCKS, SR_BASE_DV, CONVERGENCE_ICE_BY_SR
from .cpred_core import _safe_int, _render_transition, _update_seriously_wounded, _wound_flag, get_deck_slots

logger = logging.getLogger(__name__)


def _get_alert_name(level):
    """Return alert level name for CPRED NET encounters."""
    level = _safe_int(level, default=None)
    if level is None:
        return "Unknown"
    if level <= 0:
        return "Dormant"
    if level <= 2:
        return "Elevated"
    if level <= 4:
        return "Active Search"
    if level <= 6:
        return "Lockdown"
    return "Convergence"


def _net_actions_for_rank(interface_rank):
    """Return NET actions per turn based on Interface rank."""
    return 2 if interface_rank <= 3 else 3 if interface_rank <= 6 else 4 if interface_rank <= 9 else 5


def init_hack_state(
    tier="full_run",
    target_system="Unknown",
    sr=3,
    cycles_max=3,
    interface_rank=4,
    hacker_name=None,
    context=None,
    deck_slots=None,
    **_kw
):
    """Return initial hack_state structure for CPRED netrunning."""
    net_actions = _net_actions_for_rank(interface_rank)
    # Auto-extract installed hardware from deck_slots
    installed_hw = []
    if isinstance(deck_slots, list):
        installed_hw = [
            s.get("name") for s in deck_slots
            if isinstance(s, dict) and s.get("type") == "hardware" and s.get("name")
        ]
    state = {
        "active": True,
        "tier": tier,
        "target_system": target_system,
        "hacker_name": hacker_name,
        "sr": sr,
        "interface_rank": interface_rank,
        "net_actions_per_turn": net_actions,
        "start_message_id": None,
        "system_map": None,
        "revealed_nodes": ["Gateway"],
        "alert_level": 0,
        "cycles_remaining": cycles_max,
        "cycles_max": cycles_max,
        "active_programs": [],
        "installed_hardware": installed_hw,
        "current_node": "Gateway",
        "nodes_visited": ["Gateway"],
        "ice_status": {},
        "trace_progress": None,
        "tar_stacks": 0,
        "brain_damage": 0,
        "narrative_summary": None,
        "available_actions": [],
        "net_actions_remaining": net_actions,
        "meatspace_due": False,
        "_prev_alert_level": 0,
        "_prev_brain_damage": 0,
        # Per-ICE-type persistent effects
        "on_fire": False,
        "fire_rounds": 0,
        "movement_locked_by": None,
        "movement_locked_by_key": None,
        "slide_penalty": 0,
        "slide_used_this_turn": False,
        "net_action_penalty": 0,
        "active_debuffs": [],
        "destroyed_programs": [],
        # Boosted-action / Defender state. Step 3 uses fortify_pending; Step 6b
        # adds surge_pending, mask_pending, etc. on_turn_end clears one-shot
        # flags after they fire.
        "active_boosts": {},
    }
    # Bootstrap active_programs from edgerunner persistent deck_slots
    if isinstance(_kw.get("game_state"), dict) and hacker_name:
        _gs_er = _kw["game_state"].get("edgerunners", {}).get(hacker_name, {})
        _gs_slots = get_deck_slots(_gs_er)
        _gs_progs = [s for s in _gs_slots if isinstance(s, dict)
                     and s.get("type", "program") == "program" and not s.get("_continuation_of")]
        if _gs_progs:
            state["active_programs"] = [
                {"name": p.get("name", "?"), "category": p.get("category", "attacker"),
                 "rez": p.get("rez_max", 0), "status": "deactivated"}
                for p in _gs_progs if p.get("status") != "destroyed"
            ]
        # Bootstrap active_debuffs from edgerunner so 1-hour Liche/Scorpion/
        # Nervescrub effects survive hack boundaries (RAW: meatspace effect).
        # Source-of-truth lives on the edgerunner; hack_state.active_debuffs
        # is a working mirror that gets synced back at the end of each
        # apply_hack_state call.
        _gs_dbs = _gs_er.get("active_debuffs", [])
        if isinstance(_gs_dbs, list) and _gs_dbs:
            state["active_debuffs"] = list(_gs_dbs)
    if context:
        state["context"] = context
    return state


_ICE_EFFECT_OPS = {"program_destroy", "program_derez", "body_fire", "movement_lock", "stat_debuff",
                   "slide_penalty", "net_action_penalty", "forced_jack_out", "program_rez_damage"}

# Table-driven: ICE names whose effect is slide_penalty (used for auto-clear)
_SLIDE_ICE_NAMES = {b["name"].lower() for b in ICE_STAT_BLOCKS.values() if b.get("effect") == "slide_penalty"}


def _hud_to_datetime(time_str, date_str):
    """Convert HUD HHMM time + date string to a comparable datetime.

    Returns None if the time is unparseable. Falls back to a synthetic
    epoch date when the date string is missing or unparseable, so two
    same-day comparisons still work.
    """
    from datetime import datetime
    if not time_str or not isinstance(time_str, str):
        return None
    t = time_str.strip()
    if not t.isdigit() or len(t) > 4 or len(t) < 1:
        return None
    try:
        if len(t) <= 2:
            h, m = 0, int(t)
        else:
            h, m = int(t[:-2]), int(t[-2:])
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    if isinstance(date_str, str) and date_str.strip():
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y"):
            try:
                d = datetime.strptime(date_str.strip(), fmt)
                return d.replace(hour=h, minute=m)
            except ValueError:
                continue
    return datetime(2000, 1, 1, h, m)


def _stamp_debuff_expirations(state, pipeline_state):
    """Stamp expires_at_time/date on any active_debuffs missing it.

    Computed from the debuff's duration string (e.g. '1 hour') plus the
    current HUD clock. Idempotent — debuffs already stamped are skipped.
    No-op when the HUD clock has no seed yet.
    """
    debuffs = state.get("active_debuffs")
    if not isinstance(debuffs, list):
        return
    if not isinstance(pipeline_state, dict):
        return
    hud = pipeline_state.get("hud_state", {})
    if not isinstance(hud, dict):
        return
    time_str = hud.get("time", "")
    date_str = hud.get("date", "")
    if not time_str:
        return
    # Inline import: pipeline imports from game_systems, so a top-level
    # import would be circular.
    from pipeline import advance_clock, parse_time_passed
    for db in debuffs:
        if not isinstance(db, dict):
            continue
        if db.get("expires_at_time"):
            continue
        duration = db.get("duration", "")
        if not isinstance(duration, str):
            continue
        seconds = parse_time_passed(duration)
        if seconds <= 0:
            continue
        new_time, new_date = advance_clock(time_str, date_str, seconds)
        db["expires_at_time"] = new_time
        if new_date:
            db["expires_at_date"] = new_date


def _expire_active_debuffs(state, pipeline_state):
    """Drop active_debuffs whose expires_at has passed per the HUD clock.

    Debuffs without expires_at (legacy entries or ones with unparseable
    durations) are kept — silent no-ops are safer than dropping them.
    """
    debuffs = state.get("active_debuffs")
    if not isinstance(debuffs, list) or not debuffs:
        return
    if not isinstance(pipeline_state, dict):
        return
    hud = pipeline_state.get("hud_state", {})
    if not isinstance(hud, dict):
        return
    now = _hud_to_datetime(hud.get("time", ""), hud.get("date", ""))
    if now is None:
        return
    surviving = []
    for db in debuffs:
        if not isinstance(db, dict):
            surviving.append(db)
            continue
        exp_time = db.get("expires_at_time")
        if not exp_time:
            surviving.append(db)
            continue
        exp_date = db.get("expires_at_date") or hud.get("date", "")
        exp_dt = _hud_to_datetime(exp_time, exp_date)
        if exp_dt is None:
            surviving.append(db)
            continue
        if exp_dt > now:
            surviving.append(db)
    state["active_debuffs"] = surviving


def _sync_debuffs_to_edgerunner(state, game_state, name_key="hacker_name"):
    """Mirror state.active_debuffs back to game_state.edgerunners[name].active_debuffs.

    The edgerunner copy is the persistent source of truth (survives across
    hack/net_combat boundaries); state.active_debuffs is the live working
    list that fresh ICE ops mutate. Called at the tail of apply_hack_state
    and apply_net_combat_state so writes during the call propagate.
    """
    if not isinstance(game_state, dict):
        return
    name = state.get(name_key)
    if not name:
        return
    edgerunners = game_state.get("edgerunners")
    if not isinstance(edgerunners, dict):
        return
    er = edgerunners.get(name)
    if not isinstance(er, dict):
        return
    debuffs = state.get("active_debuffs")
    er["active_debuffs"] = list(debuffs) if isinstance(debuffs, list) else []


def _apply_persistent_ice_effects(state, model_hs, game_state, name_key, tick_condition):
    """Shared logic for fire tick/extinguish, wisp penalty, movement lock auto-clear, slide penalty auto-clear.

    Args:
        state: hack_state or net_combat dict (mutated)
        model_hs: the tool_input's hack_state sub-dict (for reading on_fire: false etc.)
        game_state: game_state dict
        name_key: field name for the netrunner in state ("hacker_name" or "netrunner")
        tick_condition: whether to tick fire/wisp this call (meatspace_due or _has_net_actions)
    """
    # --- Fire tick ---
    # Fire ticks once per completed NET turn (same call that sets meatspace_due). Intentional.
    try:
        if tick_condition and state.get("on_fire") and isinstance(game_state, dict):
            runner_name = state.get(name_key, "")
            er = game_state.get("edgerunners", {}).get(runner_name, {})
            if er.get("hp"):
                er["hp"]["current"] = max(0, er["hp"]["current"] - 2)
                _update_seriously_wounded(er)
                state["fire_rounds"] = state.get("fire_rounds", 0) + 1
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Fire extinguish: model reports on_fire: false ---
    if isinstance(model_hs, dict) and "on_fire" in model_hs and not model_hs.get("on_fire") and state.get("on_fire"):
        state["on_fire"] = False
        fire_rounds = state.get("fire_rounds", 0)
        nudity = "nude" if fire_rounds >= 2 else "partially_nude" if fire_rounds >= 1 else None
        if nudity and isinstance(game_state, dict):
            runner_name = state.get(name_key, "")
            er = game_state.get("edgerunners", {}).get(runner_name, {})
            conditions = er.setdefault("conditions", [])
            for old_c in ["partially_nude", "nude"]:
                if old_c in conditions:
                    conditions.remove(old_c)
            conditions.append(nudity)
        state["fire_rounds"] = 0

    # --- Wisp penalty: subtract from net_actions_remaining ---
    if tick_condition and state.get("net_action_penalty", 0) > 0:
        penalty = state["net_action_penalty"]
        state["net_actions_remaining"] = max(0, state.get("net_actions_remaining",
                                                           state.get("net_actions_per_turn", 3)) - penalty)
        state["net_action_penalty"] = 0

    # --- Movement lock auto-clear: if locking ICE is derezzed ---
    try:
        locked_by = state.get("movement_locked_by")
        if locked_by:
            ice_status = state.get("ice_status", {})
            if isinstance(ice_status, dict):
                locked_by_key = state.get("movement_locked_by_key")
                if locked_by_key:
                    # Keyed lock is authoritative: if key is missing or inactive, lock clears.
                    _source_ice = ice_status.get(locked_by_key, {})
                    still_locked = isinstance(_source_ice, dict) and _source_ice.get("status") == "active"
                    if not still_locked and locked_by_key not in ice_status:
                        # If key vanished (e.g., ICE moved node and key changed), allow a
                        # safe rebind only when a single active same-name source exists.
                        _same_name_active = [
                            _k for _k, _v in ice_status.items()
                            if isinstance(_v, dict)
                            and _v.get("status") == "active"
                            and _v.get("name") == locked_by
                        ]
                        if len(_same_name_active) == 1:
                            state["movement_locked_by_key"] = _same_name_active[0]
                            still_locked = True
                else:
                    still_locked = any(
                        isinstance(v, dict) and v.get("name") == locked_by and v.get("status") == "active"
                        for v in ice_status.values()
                    )
                if not still_locked:
                    state["movement_locked_by"] = None
                    state["movement_locked_by_key"] = None
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Slide penalty auto-clear: if no active ICE with slide_penalty effect ---
    try:
        if state.get("slide_penalty", 0) != 0:
            ice_status = state.get("ice_status", {})
            if isinstance(ice_status, dict):
                has_slide_source = any(
                    isinstance(v, dict) and v.get("status") == "active"
                    and str(v.get("name", "")).lower() in _SLIDE_ICE_NAMES
                    for v in ice_status.values()
                )
                if not has_slide_source:
                    state["slide_penalty"] = 0
    except (TypeError, ValueError, OverflowError):
        pass


def _apply_ice_effect_ops(state, resolver_state_ops, game_state=None):
    """Apply ICE-effect state_ops to hack_state or net_combat state. Shared helper."""
    if not resolver_state_ops:
        return
    for op in resolver_state_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        try:
            _apply_single_ice_op(state, op, op_type)
        except (TypeError, ValueError, OverflowError, KeyError):
            continue


def _mark_forced_disconnect(state, summary=None):
    """Apply a consistent forced-disconnect state transition.

    Hack-only state: immediate encounter end (active=False).
    Net-combat state: mark net track complete, but keep encounter active until
    both combat and net tracks are complete.
    """
    if summary and not state.get("narrative_summary"):
        state["narrative_summary"] = summary
    state["_forced_disconnect"] = True

    if "net_complete" in state:
        state["net_complete"] = True
    else:
        state["active"] = False


def _has_condition(character_name, keywords, game_state=None, pipeline_state=None):
    """Return True if character has a condition matching any of the given keywords."""
    target = str(character_name or "").strip()
    if not target:
        return False

    def _match(values):
        if not isinstance(values, list):
            return False
        for cond in values:
            if isinstance(cond, str):
                low = cond.lower()
                if any(kw in low for kw in keywords):
                    return True
        return False

    # Persistent game_state conditions
    if isinstance(game_state, dict):
        er = game_state.get("edgerunners", {}).get(target, {})
        if _match(er.get("conditions", [])):
            return True

    # Live character_state conditions
    if isinstance(pipeline_state, dict):
        cs_entry = pipeline_state.get("character_states", {}).get(target, {})
        data = cs_entry.get("data", cs_entry) if isinstance(cs_entry, dict) else {}
        if _match(data.get("conditions", [])):
            return True

    return False


def _has_unconscious_condition(character_name, game_state=None, pipeline_state=None):
    """Return True if character has an active unconscious condition marker."""
    return _has_condition(character_name, ("unconscious",), game_state=game_state, pipeline_state=pipeline_state)


def apply_program_status_change(state, program_name, old_status, new_status,
                                 game_state=None):
    """Apply a program status transition through on_program_status_change hooks.

    Hooks may rewrite new_status (e.g. Backup Drive: 'destroyed' →
    'deactivated' to save the program from permanent loss). The chain runs
    via the registry; final_new_status is then applied to active_programs
    plus destroyed_programs / REZ invariants are maintained:
      - status flips to final_new_status
      - if final_new_status == 'destroyed' AND not already there: append to
        destroyed_programs
      - if recovering FROM destroyed (final != 'destroyed' but old was):
        remove from destroyed_programs
      - if recovering FROM derezzed/destroyed TO active/deactivated: restore
        REZ from PROGRAM_STATS

    Returns (final_new_status, hook_state_ops, trace).
    """
    from .cpred_program_effects import run_program_status_change_hooks
    from .cpred_tables import PROGRAM_STATS

    final_new, hook_ops, trace = run_program_status_change_hooks(
        program_name, old_status, new_status, state, game_state)

    # Apply to active_programs
    programs = state.get("active_programs", [])
    if isinstance(programs, list):
        for p in programs:
            if isinstance(p, dict) and p.get("name") == program_name:
                p["status"] = final_new
                # Restore REZ when recovering from a derezzed/destroyed state.
                if old_status in ("derezzed", "destroyed") and final_new != old_status:
                    needle = str(program_name).strip().lower().replace(" ", "")
                    for canonical, block in PROGRAM_STATS.items():
                        if canonical.lower().replace(" ", "") == needle:
                            p["rez"] = block.get("rez", p.get("rez", 0))
                            break
                break

    # Maintain destroyed_programs invariant
    destroyed = state.setdefault("destroyed_programs", [])
    if isinstance(destroyed, list):
        if final_new == "destroyed":
            if program_name and program_name not in destroyed:
                destroyed.append(program_name)
        else:
            if program_name in destroyed:
                destroyed.remove(program_name)

    return final_new, hook_ops, trace


def _apply_single_ice_op(state, op, op_type):
    """Apply a single ICE effect op. Raises on bad data — caller catches."""
    if op_type == "program_destroy":
        prog_name = op.get("program_name", "")
        # Step 2: route through hooks so Backup Drive can intercept.
        # Look up current status to record the transition origin.
        old_status = "active"
        for p in state.get("active_programs", []) or []:
            if isinstance(p, dict) and p.get("name") == prog_name:
                old_status = str(p.get("status", "active")).strip().lower() or "active"
                break
        apply_program_status_change(state, prog_name, old_status, "destroyed")

    elif op_type == "program_derez":
        # Derez is recoverable with 2 NET Actions, but it still uses the
        # documented recoverable status contract rather than a private marker.
        prog_name = op.get("program_name", "")
        old_status = "active"
        for p in state.get("active_programs", []) or []:
            if isinstance(p, dict) and p.get("name") == prog_name:
                old_status = str(p.get("status", "active")).strip().lower() or "active"
                break
        apply_program_status_change(state, prog_name, old_status, "derezzed")

    elif op_type == "body_fire":
        state["on_fire"] = True

    elif op_type == "movement_lock":
        state["movement_locked_by"] = op.get("locked_by")
        state["movement_locked_by_key"] = op.get("locked_by_key")

    elif op_type == "stat_debuff":
        debuffs = state.setdefault("active_debuffs", [])
        debuffs.append({
            "stats": op.get("stats", []),
            "amount": op.get("amount", 0),
            "source": op.get("source", ""),
            "duration": op.get("duration", "1 hour"),
        })

    elif op_type == "slide_penalty":
        state["slide_penalty"] = state.get("slide_penalty", 0) + op.get("penalty", -2)

    elif op_type == "net_action_penalty":
        state["net_action_penalty"] = state.get("net_action_penalty", 0) + op.get("penalty", 1)

    elif op_type == "forced_jack_out":
        _mark_forced_disconnect(state, summary="Forced Jack Out — unsafe disconnect.")
        state["_cascade_applied"] = True  # resolver already cascaded all rezzed ICE

    elif op_type == "program_rez_damage":
        prog_name = op.get("program_name", "")
        damage = int(op.get("damage", 0))
        programs = state.get("active_programs", [])
        if isinstance(programs, list):
            for p in programs:
                if isinstance(p, dict) and p.get("name") == prog_name:
                    p["rez"] = max(0, int(p.get("rez", 0)) - damage)
                    if p["rez"] <= 0 or op.get("destroyed"):
                        # Step 2: route through hooks so Backup Drive can
                        # intercept anti-program-ICE destruction.
                        old_status = str(p.get("status", "active")).strip().lower() or "active"
                        apply_program_status_change(state, prog_name, old_status, "destroyed")
                    break


def _apply_rez_damage_to_ice_status(state, op):
    """Apply resolver rez_damage op to the intended ICE instance.

    Prefers explicit key fields when provided, and otherwise falls back to
    legacy name matching (favoring active/current-node matches).
    """
    damage = _safe_int(op.get("damage", 0))
    if damage <= 0:
        return

    ice_status = state.get("ice_status", {})
    if not isinstance(ice_status, dict):
        return

    target_key = (
        op.get("target_key")
        or op.get("target_ice_key")
        or op.get("ice_key")
        or op.get("ice_status_key")
    )
    target_node_hint = op.get("target_node")
    target_name = op.get("target", "")
    target_name_norm = str(target_name).strip().lower() if isinstance(target_name, str) else ""

    victim = None
    if target_key and isinstance(ice_status.get(target_key), dict):
        victim = ice_status.get(target_key)

    if victim is None and target_name_norm:
        matches = [
            (k, v) for k, v in ice_status.items()
            if isinstance(v, dict) and str(v.get("name", "")).strip().lower() == target_name_norm
        ]
        if not matches:
            return
        active_matches = [(k, v) for (k, v) in matches if v.get("status") == "active"]
        candidates = active_matches or matches
        _has_target_node_hint = isinstance(target_node_hint, str) and bool(target_node_hint)
        if _has_target_node_hint:
            hint_candidates = [
                (k, v) for (k, v) in candidates
                if k == target_node_hint or k.startswith(f"{target_node_hint}_")
            ]
            if len(hint_candidates) == 1:
                victim = hint_candidates[0][1]
            elif len(hint_candidates) > 1:
                # Explicit target node hint is ambiguous; do not guess via other fallbacks.
                return
        current_node = state.get("current_node")
        if victim is None and not _has_target_node_hint and isinstance(current_node, str):
            for k, v in candidates:
                if k == current_node:
                    victim = v
                    break
        if victim is None and not _has_target_node_hint and len(candidates) == 1:
            victim = candidates[0][1]
        if victim is None:
            # Ambiguous duplicate-name target with no explicit key.
            return
    if victim is None:
        return

    if not isinstance(victim, dict):
        return
    victim["rez_current"] = max(0, int(victim.get("rez_current", 0)) - damage)
    if victim["rez_current"] <= 0:
        victim["status"] = "derezzed"
        # Derezzed Black ICE can't hunt — clear engagement list so any
        # subsequent Slide validation against this ICE fails preemptively
        # (correctly: there's nothing to escape from anymore).
        if isinstance(victim.get("hunting"), list):
            victim["hunting"] = []


def _apply_net_model_fields(state, hs, tool_input):
    """Apply model-reported NET fields, system map, revealed_nodes, available_actions."""
    if isinstance(hs, dict):
        # Step 6b: alert_level changes route through on_alert_increase hooks
        # so Mask can suppress detection, etc. Compute delta from current
        # state and feed the registry; final delta becomes the actual change.
        if "alert_level" in hs:
            try:
                _new_alert = int(hs["alert_level"])
            except (TypeError, ValueError):
                _new_alert = state.get("alert_level", 0)
            _cur_alert = int(state.get("alert_level", 0))
            _delta = _new_alert - _cur_alert
            if _delta > 0:
                from .cpred_program_effects import run_alert_increase_hooks
                _final_delta, _alert_ops, _alert_trace = run_alert_increase_hooks(
                    _delta, "model_report", state)
                state["alert_level"] = _cur_alert + max(0, _final_delta)
                if _alert_trace:
                    state["_last_alert_trace"] = _alert_trace
                # Process any state ops returned by alert hooks (Mask emits
                # alert_suppressed annotation; future suppressors may emit
                # other ops).
                for _op in _alert_ops:
                    if isinstance(_op, dict) and _op.get("op") == "alert_suppressed":
                        # Annotation only — no state mutation; surfaced in
                        # _last_alert_trace for narration.
                        pass
            else:
                state["alert_level"] = _new_alert
        for field in ["cycles_remaining", "active_programs",
                      "installed_hardware", "current_node", "nodes_visited",
                      "ice_status", "trace_progress", "tar_stacks",
                      "revealed_nodes", "slide_used_this_turn"]:
            if field in hs:
                state[field] = hs[field]
        if hs.get("system_map") and not state.get("system_map"):
            state["system_map"] = hs["system_map"]
    # revealed_nodes validation + auto-merge
    if not isinstance(state.get("revealed_nodes"), list):
        visited_fallback = state.get("nodes_visited", [])
        state["revealed_nodes"] = list(visited_fallback) if isinstance(visited_fallback, list) else ["Gateway"]
    visited = state.get("nodes_visited", [])
    revealed = state.get("revealed_nodes", [])
    if isinstance(visited, list) and isinstance(revealed, list):
        for n in visited:
            if n not in revealed:
                revealed.append(n)
        state["revealed_nodes"] = revealed
    # Available actions
    if tool_input.get("available_actions"):
        if isinstance(tool_input["available_actions"], list):
            state["available_actions"] = tool_input["available_actions"]
        else:
            logger.warning("CPRED NET: available_actions must be a list, got %s",
                           type(tool_input["available_actions"]).__name__)


def _apply_resolver_net_ops(state, resolver_state_ops, game_state=None):
    """Apply resolver state_ops shared across NET modes:
    brain_damage accumulation, program_deactivate, rez_damage, ICE effects, tar_consumed."""
    if not resolver_state_ops:
        return
    # Brain damage accumulation (resolver-authoritative). Track attack count
    # alongside total damage so _apply_brain_damage_hp can apply the RAW p.189
    # Death Save penalty correctly (+1 per attack received while already MW,
    # NOT +1 per damage point).
    bd_total = 0
    bd_attacks = 0
    for op in resolver_state_ops:
        if not isinstance(op, dict) or op.get("op") != "brain_damage":
            continue
        amt = abs(_safe_int(op.get("change", 0)))
        if amt > 0:
            bd_total += amt
            bd_attacks += 1
    if bd_total > 0:
        state["brain_damage"] = state.get("brain_damage", 0) + bd_total
        state["_pending_bd_attacks"] = state.get("_pending_bd_attacks", 0) + bd_attacks
    # program_deactivate + program_status_change + rez_damage
    for op in resolver_state_ops:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "program_deactivate":
            prog_name = op.get("program_name", "")
            programs = state.get("active_programs", [])
            if isinstance(programs, list):
                for p in programs:
                    if isinstance(p, dict) and p.get("name") == prog_name:
                        p["status"] = "deactivated"
                        break
        elif op.get("op") == "program_status_change":
            # Step 0c emits this for player-choice transitions (activate /
            # deactivate / reactivate / reinstall). Step 2 routes through the
            # apply_program_status_change helper so on_program_status_change
            # hooks (Backup Drive etc.) fire consistently with cascade-driven
            # destruction paths.
            prog_name = op.get("program_name", "")
            new_status = op.get("new_status", "")
            old_status = op.get("old_status", "")
            if prog_name and new_status:
                apply_program_status_change(
                    state, prog_name, old_status, new_status, game_state)
        elif op.get("op") == "rez_damage":
            try:
                _apply_rez_damage_to_ice_status(state, op)
            except (TypeError, ValueError, OverflowError):
                pass
    # ICE-effect ops (program_destroy, body_fire, movement_lock, etc.)
    _ice_ops = [op for op in resolver_state_ops
                if isinstance(op, dict) and op.get("op") in _ICE_EFFECT_OPS]
    _apply_ice_effect_ops(state, _ice_ops, game_state=game_state)
    # tar_consumed is resolver-authoritative
    if any(isinstance(op, dict) and op.get("op") == "tar_consumed" for op in resolver_state_ops):
        state["tar_stacks"] = 0
    # active_boost_clear: clear one-shot Boosted-Action flags (Fortify, Surge,
    # Mask). Emitted by on_turn_end hooks when a flag has expired.
    for op in resolver_state_ops:
        if isinstance(op, dict) and op.get("op") == "active_boost_clear":
            boost_key = op.get("boost")
            boosts = state.setdefault("active_boosts", {})
            if isinstance(boosts, dict) and boost_key:
                boosts.pop(boost_key, None)
    # Step 6b: active_boost_set — boosted_action handler activates a flag
    # (and optional duration counter for multi-turn boosts like Spoof Signal).
    for op in resolver_state_ops:
        if isinstance(op, dict) and op.get("op") == "active_boost_set":
            boost_key = op.get("boost")
            boosts = state.setdefault("active_boosts", {})
            if isinstance(boosts, dict) and boost_key:
                boosts[boost_key] = op.get("value", True)
                df = op.get("duration_field")
                dv = op.get("duration_value")
                if df and dv:
                    boosts[df] = dv
    # Step 6b: cycle_consumed — debit cycles_remaining atomically.
    for op in resolver_state_ops:
        if isinstance(op, dict) and op.get("op") == "cycle_consumed":
            try:
                amount = int(op.get("amount", 1))
            except (TypeError, ValueError):
                amount = 1
            cur = state.get("cycles_remaining", state.get("cycles_max", 0))
            try:
                state["cycles_remaining"] = max(0, int(cur) - amount)
            except (TypeError, ValueError):
                pass
    # Slide / hunt enforcement (RAW p.205): hunt_start adds the Netrunner
    # to a Black ICE's hunting list when the ICE engages; hunt_clear
    # removes them on a successful Slide; slide_used flags one-Slide-
    # per-turn enforcement (cleared at meatspace_due in apply_hack_state).
    ice_status = state.setdefault("ice_status", {})
    for op in resolver_state_ops:
        if not isinstance(op, dict):
            continue
        op_t = op.get("op")
        if op_t == "hunt_start":
            ice_key = op.get("ice_key")
            nr_name = op.get("netrunner")
            if ice_key and nr_name and isinstance(ice_status, dict):
                entry = ice_status.get(ice_key)
                if isinstance(entry, dict):
                    h = entry.setdefault("hunting", [])
                    if isinstance(h, list) and nr_name not in h:
                        h.append(nr_name)
        elif op_t == "hunt_clear":
            ice_key = op.get("ice_key")
            nr_name = op.get("netrunner")
            if ice_key and nr_name and isinstance(ice_status, dict):
                entry = ice_status.get(ice_key)
                if isinstance(entry, dict):
                    h = entry.get("hunting", [])
                    if isinstance(h, list) and nr_name in h:
                        h.remove(nr_name)
        elif op_t == "slide_used":
            state["slide_used_this_turn"] = True
    # Step 6d: initiate_unsafe_jack_out — emitted by DeckKRASH's
    # on_program_attack_hit hook. Re-uses the same code path as the
    # model-supplied tool_input.initiate_unsafe_jack_out: pass through
    # the gate (ally/connection_severed bypass; self_unplugged blocked
    # by Superglue) and cascade if allowed. The resolver-emitted op is
    # synthesized into the tool_input shape that
    # _apply_initiate_unsafe_jack_out expects.
    for op in resolver_state_ops:
        if isinstance(op, dict) and op.get("op") == "initiate_unsafe_jack_out":
            synthesized = {"initiate_unsafe_jack_out": {
                "cause": op.get("cause", "other"),
                "actor": op.get("actor", ""),
                "reason": op.get("reason", ""),
            }}
            try:
                _apply_initiate_unsafe_jack_out(
                    state, synthesized, game_state, "hacker_name")
            except (TypeError, ValueError, OverflowError, KeyError):
                pass
    # Step 6c: jack_out_lock — Superglue and similar effects set a
    # rounds_remaining countdown on active_boosts. The
    # _check_jack_out_allowed gate reads it; on_turn_end (or equivalent
    # decrement) lowers it each round.
    for op in resolver_state_ops:
        if isinstance(op, dict) and op.get("op") == "jack_out_lock":
            try:
                rounds = int(op.get("rounds_remaining", 0))
            except (TypeError, ValueError):
                rounds = 0
            if rounds > 0:
                boosts = state.setdefault("active_boosts", {})
                if isinstance(boosts, dict):
                    cur = 0
                    try:
                        cur = int(boosts.get("jack_out_lock_rounds_remaining", 0))
                    except (TypeError, ValueError):
                        cur = 0
                    # Stack durations? RAW is silent; pick max so a fresh
                    # Superglue refreshes rather than truncates.
                    boosts["jack_out_lock_rounds_remaining"] = max(cur, rounds)


def _apply_brain_damage_hp(state, game_state, name_key, pipeline_state=None):
    """Apply brain damage delta to netrunner's HP incrementally.

    Death Save Penalty (RAW p.189): when a Mortally Wounded character (at 0 HP)
    is damaged by an attack, their Death Save Penalty increases by +1 (per
    attack, NOT per damage point). The attack that first brings them to 0 HP
    is processed while they are still in their previous wound state — it does
    NOT itself trigger the penalty.

    Approximation: individual brain_damage ops are aggregated into a single
    delta, so we don't know exact ordering. We count ops via
    _pending_bd_attacks and apply the penalty with the assumption that the
    first attack in a batch is the one that brings the Netrunner to MW (if
    they weren't already). This matches RAW exactly for single-attack
    exchanges (the common case) and is a close approximation for multi-ICE
    cascades where ordering is ambiguous.
    """
    current_bd = _safe_int(state.get("brain_damage", 0))
    prev_bd = _safe_int(state.get("_prev_brain_damage", 0))
    bd_delta = current_bd - prev_bd
    if bd_delta <= 0:
        return
    state["_prev_brain_damage"] = current_bd
    n_attacks = _safe_int(state.pop("_pending_bd_attacks", 0))
    try:
        char_name = state.get(name_key, "")
        edgerunners = game_state.get("edgerunners", {}) if isinstance(game_state, dict) else {}
        if char_name and char_name in edgerunners:
            er = edgerunners[char_name]
            if not er.get("hp"):
                return
            old_hp = er["hp"]["current"]
            new_hp = max(0, old_hp - bd_delta)
            er["hp"]["current"] = new_hp
            # RAW p.189: +1 DS penalty per attack received while ALREADY at
            # 0 HP. Attack that brings you TO 0 HP does not count (it makes
            # you Mortally Wounded; subsequent attacks trigger the penalty).
            if n_attacks > 0:
                if old_hp == 0:
                    # Already MW going in — every attack in this batch is a
                    # post-MW hit.
                    ds_penalty_delta = n_attacks
                elif new_hp == 0:
                    # Brought to MW by this batch — first attack makes them
                    # MW (no penalty), remaining attacks are post-MW (+1 each).
                    ds_penalty_delta = max(0, n_attacks - 1)
                else:
                    ds_penalty_delta = 0
                if ds_penalty_delta > 0:
                    er["death_save_count"] = er.get("death_save_count", 0) + ds_penalty_delta
            _update_seriously_wounded(er)
            if isinstance(pipeline_state, dict):
                cs = pipeline_state.get("character_states", {})
                entry = cs.get(char_name, {})
                d = entry.get("data", entry)
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        v["current"] = er["hp"]["current"]
                        break
    except (TypeError, ValueError, OverflowError):
        pass


def _has_dead_condition(character_name, game_state=None, pipeline_state=None):
    """Return True if character has a dead/flatlined condition marker (failed Death Save)."""
    return _has_condition(character_name, ("dead", "flatline"), game_state=game_state, pipeline_state=pipeline_state)


def _apply_disconnect_cascade(state, game_state, name_key, pipeline_state=None):
    """Fire all rezzed Black ICE effects on backend-initiated forced disconnect.

    Triggered only on flatline (failed Death Save). Mirrors the Giant
    forced_jack_out cascade but bypasses KRASH Barrier — physical
    disconnection from death cannot be blocked by software.
    """
    try:
        ice_status = state.get("ice_status")
        if not isinstance(ice_status, dict) or not ice_status:
            return
        active_programs = state.get("active_programs")
        installed_hardware = state.get("installed_hardware")

        cascade = _resolve_jack_out_cascade(
            ice_status, active_programs, installed_hardware,
            exclude_ice=None, exclude_ice_key=None, _depth=0,
            active_boosts=state.get("active_boosts"),
        )

        cascade_ops = cascade.get("state_ops", [])
        if cascade_ops:
            _apply_resolver_net_ops(state, cascade_ops, game_state)
            _apply_brain_damage_hp(state, game_state, name_key, pipeline_state)
    except (TypeError, ValueError, OverflowError):
        pass


# Step 6c: jack-out gate
# ---------------------------------------------------------------------------
# Causes that BYPASS the gate — physical severance can't be glued, neural
# collapse from flatline overrides any program effect.
_JACK_OUT_BYPASS_CAUSES = frozenset({
    "ally_unplugged",
    "ally_dragged_out_of_range",
    "connection_severed",
    "flatline",  # backend-internal cause used by _check_forced_disconnect
})

# Causes that ARE gated — voluntary self-initiated jack-outs.
_JACK_OUT_GATED_CAUSES = frozenset({
    "self_unplugged",
    "voluntary_safe",  # used by the hack_complete=True voluntary path
})


def _check_jack_out_allowed(state, cause=None):
    """Return (allowed: bool, reason_or_None: str) for a jack-out attempt.

    Step 6c gate: Superglue (and future programs) lock voluntary jack-out
    by setting active_boosts.jack_out_lock_rounds_remaining > 0. Forced
    disconnects (ally physically unplugs, ally drags body, flatline)
    bypass the gate — physical severance can't be blocked by software.

    Args:
        state: hack_state dict.
        cause: jack-out cause (one of _JACK_OUT_BYPASS_CAUSES or
               _JACK_OUT_GATED_CAUSES). If None, treated as gated
               (defensive — unknown causes are blocked).

    Returns:
        (True, None) if the jack-out is allowed.
        (False, reason_str) if the gate blocks it.
    """
    if cause in _JACK_OUT_BYPASS_CAUSES:
        return True, None
    boosts = state.get("active_boosts", {}) if isinstance(state, dict) else {}
    if not isinstance(boosts, dict):
        return True, None
    rounds_left = boosts.get("jack_out_lock_rounds_remaining", 0)
    try:
        rounds_int = int(rounds_left)
    except (TypeError, ValueError):
        rounds_int = 0
    if rounds_int > 0:
        return False, (
            f"Jack-out blocked by Superglue ({rounds_int} round"
            f"{'s' if rounds_int != 1 else ''} remaining)."
        )
    return True, None


def _apply_initiate_unsafe_jack_out(state, tool_input, game_state, name_key, pipeline_state=None):
    """Handle model-signaled Unsafe Jack Out.

    Fired when the crew physically severs the Netrunner's connection while
    they're stuck in the NET — e.g. an ally spends an Action to unplug the
    deck or drag an unconscious Netrunner's body out of access-point range,
    or the Netrunner yanks their own plug mid-hack knowing the cost.

    Applies the same cascade as flatline: all rezzed Black ICE effects hit
    the Netrunner on the way out, then marks the hack as forcibly ended.
    Idempotent — _cascade_applied guards against double-hits if the model
    re-emits this across retries.

    Step 6c: routed through _check_jack_out_allowed. Forced/external
    causes (ally_*, connection_severed) bypass the Superglue lock;
    self_unplugged is gated.

    Expected tool_input shape:
        "initiate_unsafe_jack_out": {
            "cause": "ally_unplugged" | "ally_dragged_out_of_range" |
                     "self_unplugged" | "connection_severed" | "other",
            "actor": "<name of who did it, or 'self'>",
            "reason": "<short narrative description for summary display>"
        }
    """
    ujo = tool_input.get("initiate_unsafe_jack_out") if isinstance(tool_input, dict) else None
    if not isinstance(ujo, dict):
        return
    cause = ujo.get("cause")
    allowed, gate_reason = _check_jack_out_allowed(state, cause=cause)
    if not allowed:
        # Surface a structured rejection on hack_state. The model receives
        # this on its next injection and can narrate the failed escape.
        state["_jack_out_rejected"] = {
            "cause": cause,
            "actor": ujo.get("actor"),
            "attempted_reason": ujo.get("reason"),
            "gate_reason": gate_reason,
        }
        return
    try:
        if not state.get("_cascade_applied"):
            state["_cascade_applied"] = True
            _apply_disconnect_cascade(state, game_state, name_key, pipeline_state)
        reason = ujo.get("reason") or "Connection severed — unsafe jack out."
        actor = ujo.get("actor")
        if actor and actor != "self":
            summary = f"Unsafe Jack Out ({actor}): {reason}"
        else:
            summary = f"Unsafe Jack Out: {reason}"
        _mark_forced_disconnect(state, summary=summary)
    except (TypeError, ValueError, OverflowError, KeyError):
        pass


def _check_forced_disconnect(state, game_state, name_key, pipeline_state=None):
    """Force-disconnect netrunner ONLY on flatline (failed Death Save = dead).

    RAW (p.187):
      - 0 HP = Mortally Wounded, still conscious — no auto-disconnect.
      - Unconscious (sleep ammo, KO, etc.) does NOT disconnect either — the
        Netrunner is stuck jacked in as a sitting duck, unable to take NET
        actions, until an ally physically unplugs them or drags the body
        out of access-point range (which is an Unsafe Jack Out, handled by
        the model via narration + edgerunner_ops / hack_complete, not by
        this auto-check).
      - Only a failed Death Save (flatline) causes the backend to
        auto-terminate the hack and cascade all rezzed Black ICE.
    """
    try:
        char_name = state.get(name_key, "")
        if not char_name:
            return
        gs = game_state if isinstance(game_state, dict) else None
        dead = _has_dead_condition(char_name, game_state=gs, pipeline_state=pipeline_state)
        if dead:
            _mark_forced_disconnect(
                state,
                summary=f"{char_name} is dead — forced disconnect.",
            )

        # On forced disconnect (flatline only), cascade all rezzed Black ICE effects
        if state.get("_forced_disconnect") and not state.get("_cascade_applied"):
            state["_cascade_applied"] = True
            _apply_disconnect_cascade(state, game_state, name_key, pipeline_state)
    except (TypeError, ValueError, OverflowError):
        pass


def _apply_trace_auto_increment(state, tick_condition):
    """Tick trace progress once per completed NET turn if active Trace ICE exists."""
    try:
        if not tick_condition:
            return
        ice_status = state.get("ice_status", {})
        if not isinstance(ice_status, dict):
            return
        has_active_trace = any(
            isinstance(v, dict) and v.get("status") == "active"
            and isinstance(v.get("behavior", ""), str)
            and "trace" in v.get("behavior", "").lower()
            for v in ice_status.values()
        )
        if has_active_trace and state.get("trace_progress") is not None:
            state["trace_progress"] = int(state.get("trace_progress", 0)) + 1
            sr = int(state.get("sr", 3))
            trace_max = max(1, 6 - sr)
            if state["trace_progress"] >= trace_max:
                state["alert_level"] = max(int(state.get("alert_level", 0)), 7)
    except (TypeError, ValueError, OverflowError):
        pass


def _apply_alert_ice_spawn(state):
    """Auto-spawn ICE at alert thresholds: Trace at 5+, Black ICE (by SR) at 7+."""
    try:
        prev_alert = int(state.get("_prev_alert_level", 0))
        new_alert = int(state.get("alert_level", 0))
        ice_status = state.get("ice_status", {})
        if not isinstance(ice_status, dict):
            ice_status = {}
        # Lockdown (5+): auto-spawn Trace ICE at Gateway if none exists
        if new_alert >= 5 and prev_alert < 5:
            has_trace = any(
                isinstance(v, dict)
                and isinstance(v.get("behavior", ""), str)
                and "trace" in v.get("behavior", "").lower()
                and v.get("status") in ("active", "bypassed")
                for v in ice_status.values()
            )
            if not has_trace:
                sr = int(state.get("sr", 3))
                ice_status["Gateway_Trace"] = {
                    "name": "Trace ICE", "behavior": "trace",
                    "rez_current": sr * 2, "rez_max": sr * 2, "status": "active"
                }
                state["ice_status"] = ice_status
                state["trace_progress"] = 0
        # Convergence (7+): auto-spawn Black ICE at Netrunner's current node
        # ICE type scales with SR (Hacking Rulebook §6)
        if new_alert >= 7 and prev_alert < 7:
            sr = int(state.get("sr", 3))
            current_node = state.get("current_node", "Gateway")
            spawn_key = str(current_node) + "_Convergence"
            ice_key = CONVERGENCE_ICE_BY_SR.get(sr, "kraken")
            ice_block = ICE_STAT_BLOCKS.get(ice_key, ICE_STAT_BLOCKS["kraken"])
            ice_status[spawn_key] = {
                "name": ice_block["name"], "behavior": "black", "ice_type": ice_key,
                "rez_current": ice_block["rez"],
                "rez_max": ice_block["rez"], "status": "active"
            }
            state["ice_status"] = ice_status
        state["_prev_alert_level"] = new_alert
    except (TypeError, ValueError, OverflowError):
        pass


def apply_hack_state(hack_state, tool_input, resolver_state_ops=None, game_state=None, pipeline_state=None):
    """Apply report_hack_state tool output to hack_state. Returns updated hack_state."""
    if not isinstance(tool_input, dict):
        logger.warning(
            "CPRED apply_hack_state: tool_input must be an object, got %s",
            type(tool_input).__name__
        )
        return hack_state

    hs = tool_input.get("hack_state", {})
    if not isinstance(hs, dict):
        logger.warning(
            "CPRED apply_hack_state: hack_state must be an object, got %s",
            type(hs).__name__
        )
        hs = {}

    _apply_net_model_fields(hack_state, hs, tool_input)

    # NET action counter — decrement remaining, flag meatspace when turn complete
    net_actions_used = hs.get("net_actions_used")
    if net_actions_used is not None:
        net_actions_used = _safe_int(net_actions_used)
        remaining = hack_state.get("net_actions_remaining", hack_state.get("net_actions_per_turn", 3))
        remaining = max(0, remaining - net_actions_used)
        if remaining <= 0:
            # Turn complete — reset for next turn and flag meatspace.
            # Step 6c: Overclock grants +1 NA on the upcoming turn. Consume
            # the active_boosts.overclock_pending flag here at the boundary.
            base_na = hack_state.get("net_actions_per_turn", 3)
            boosts = hack_state.get("active_boosts", {}) if isinstance(hack_state.get("active_boosts"), dict) else {}
            if boosts.get("overclock_pending"):
                hack_state["net_actions_remaining"] = base_na + 1
                # Clear the flag (single-shot bonus)
                boosts.pop("overclock_pending", None)
            else:
                hack_state["net_actions_remaining"] = base_na
            # Step 6c: tick the Superglue jack-out lock countdown each
            # turn boundary. The lock lives on the TARGET's state (not on
            # the deck of whoever fired Superglue), so decrement is
            # backend-driven here rather than via a registry on_turn_end
            # hook (which would never fire — the target doesn't load
            # Superglue).
            if isinstance(boosts, dict):
                _glue_left = boosts.get("jack_out_lock_rounds_remaining", 0)
                try:
                    _glue_int = int(_glue_left)
                except (TypeError, ValueError):
                    _glue_int = 0
                if _glue_int > 0:
                    if _glue_int - 1 > 0:
                        boosts["jack_out_lock_rounds_remaining"] = _glue_int - 1
                    else:
                        boosts.pop("jack_out_lock_rounds_remaining", None)
            # Slide is once-per-turn (RAW p.205); reset at turn boundary so
            # the Netrunner can Slide again next turn if a Black ICE is
            # still on them.
            hack_state["slide_used_this_turn"] = False
            hack_state["meatspace_due"] = True
        else:
            hack_state["net_actions_remaining"] = remaining
            hack_state["meatspace_due"] = False

    # Hack completion (voluntary safe Jack Out). Step 6c: gated by
    # _check_jack_out_allowed — Superglue blocks voluntary jack-outs.
    if tool_input.get("hack_complete"):
        _allowed, _gate_reason = _check_jack_out_allowed(hack_state, cause="voluntary_safe")
        if _allowed:
            hack_state["active"] = False
            hack_state["narrative_summary"] = tool_input.get("narrative_summary", "Hack completed.")
        else:
            hack_state["_jack_out_rejected"] = {
                "cause": "voluntary_safe",
                "actor": "self",
                "attempted_reason": tool_input.get("narrative_summary", ""),
                "gate_reason": _gate_reason,
            }

    # Combat breakout — flag for dispatch to transition to net_combat mode
    initiate_combat = tool_input.get("initiate_combat")
    if initiate_combat and isinstance(initiate_combat, dict) and not tool_input.get("hack_complete"):
        hack_state["_initiate_combat"] = initiate_combat

    _apply_resolver_net_ops(hack_state, resolver_state_ops, game_state)
    _apply_brain_damage_hp(hack_state, game_state, "hacker_name", pipeline_state)
    _apply_persistent_ice_effects(hack_state, hs, game_state, "hacker_name", hack_state.get("meatspace_due"))
    _apply_trace_auto_increment(hack_state, hack_state.get("meatspace_due"))
    _apply_alert_ice_spawn(hack_state)
    _stamp_debuff_expirations(hack_state, pipeline_state)
    _expire_active_debuffs(hack_state, pipeline_state)
    _sync_debuffs_to_edgerunner(hack_state, game_state, "hacker_name")

    # Model-signaled Unsafe Jack Out (ally unplugs / drags out of range / self-yanks).
    # Applied before flatline check so an ally rescuing an unconscious Netrunner
    # takes precedence and gets authored narrative context in the summary.
    if hack_state.get("active"):
        _apply_initiate_unsafe_jack_out(hack_state, tool_input, game_state, "hacker_name", pipeline_state)

    # Forced disconnect (flatline only — see _check_forced_disconnect)
    if hack_state.get("active"):
        _check_forced_disconnect(hack_state, game_state, "hacker_name", pipeline_state)
    if hack_state.get("_forced_disconnect"):
        hack_state["active"] = False

    return hack_state


def _render_alert_effects(alert_level, alert_name, sr=3):
    """Return formatted alert line string with active effects."""
    alert_line = f"Alert Level: {alert_level} ({alert_name})"
    effects = []
    if alert_level >= 3:
        effects.append("DVs +2")
    if alert_level >= 5:
        lockdown_dv = SR_BASE_DV.get(sr, 15)
        effects.append(f"LOCKDOWN — move between nodes requires Interface check DV {lockdown_dv}")
    if alert_level >= 7:
        effects.append("CONVERGENCE — Black ICE + security")
    if effects:
        alert_line += f" [{', '.join(effects)}]"
    return alert_line


def _render_active_programs(programs):
    """Return list of formatted program strings, or ['Active Programs: None']."""
    if isinstance(programs, list) and programs:
        prog_strs = []
        for p in programs:
            if isinstance(p, dict):
                status_note = f", {p['status']}" if p.get("status") and p["status"] != "active" else ""
                prog_strs.append(f"{p.get('name', '?')} ({p.get('category', '?')}, REZ {p.get('rez', 0)}{status_note})")
            else:
                prog_strs.append(str(p))
        return [f"Active Programs: {', '.join(prog_strs)}"]
    return ["Active Programs: None"]


def _render_ice_status(ice_dict):
    """Return formatted ICE status lines."""
    lines = []
    if isinstance(ice_dict, dict) and ice_dict:
        lines.append("ICE Status:")
        for node, ice_data in ice_dict.items():
            if isinstance(ice_data, dict):
                name = ice_data.get("name", "Unknown")
                behavior = ice_data.get("behavior", "?")
                rez_cur = ice_data.get("rez_current", 0)
                rez_max = ice_data.get("rez_max", 0)
                status = ice_data.get("status", "active")
                type_tag = f" [{ice_data.get('ice_type', '')}]" if ice_data.get("ice_type") else ""
                lines.append(f"  {node}: {name}{type_tag} ({behavior}) — REZ {rez_cur}/{rez_max}, {status}")
            else:
                lines.append(f"  {node}: {ice_data}")
    return lines


def _render_trace_progress(state, include_warnings=True):
    """Return formatted trace progress lines."""
    lines = []
    trace = state.get("trace_progress")
    if trace is not None:
        trace_int = _safe_int(trace, default=None)
        if trace_int is not None:
            sr = _safe_int(state.get("sr", 3), default=3)
            trace_max = max(1, 6 - sr)
            trace_line = f"Trace Progress: {trace_int}/{trace_max}"
            if include_warnings:
                if trace_int >= trace_max:
                    trace_line += " [TRACE COMPLETE — location burned, Convergence NOW]"
                elif trace_int >= trace_max - 1:
                    trace_line += " [CRITICAL — completes next turn!]"
            lines.append(trace_line)
        else:
            lines.append(f"Trace Progress: {trace}")
    return lines


def _render_tar_stacks(state):
    """Return formatted tar stack lines."""
    tar = _safe_int(state.get("tar_stacks", 0))
    if tar:
        return [f"Tar Stacks: {tar} (-{tar * 2} auto-applied to next NET check, or spend 1 Cycle to clear)"]
    return []


def _render_active_effects(state):
    """Return formatted active ICE effect lines."""
    effects = []
    if state.get("on_fire"):
        effects.append("ON FIRE — clothes burning, 2 meat HP/turn (full meat action to extinguish)")
    if state.get("movement_locked_by"):
        effects.append(f"MOVEMENT LOCKED by {state['movement_locked_by']} — cannot move between nodes until derezzed")
    slide = _safe_int(state.get("slide_penalty", 0))
    if slide != 0:
        effects.append(f"SLIDE PENALTY: {state['slide_penalty']} to all Slide checks (from Skunk)")
    net_penalty = _safe_int(state.get("net_action_penalty", 0))
    if net_penalty > 0:
        effects.append(f"NET ACTION PENALTY: -{state['net_action_penalty']} next turn (from Wisp)")
    for _db in state.get("active_debuffs", []):
        if isinstance(_db, dict):
            _stats = _db.get("stats", [])
            _stats_str = "/".join(str(s) for s in _stats) if isinstance(_stats, list) else str(_stats)
            _exp_t = _db.get("expires_at_time")
            _exp_d = _db.get("expires_at_date")
            if _exp_t:
                _when = f"expires {_exp_d} {_exp_t}" if _exp_d else f"expires {_exp_t}"
            else:
                _when = _db.get("duration", "?")
            effects.append(f"DEBUFF: {_stats_str} -{_db.get('amount', 0)} ({_db.get('source', '?')}, {_when})")
    destroyed = state.get("destroyed_programs", [])
    if isinstance(destroyed, list) and destroyed:
        effects.append(f"DESTROYED PROGRAMS: {', '.join(str(d) for d in destroyed)} (permanently lost)")
    lines = []
    if effects:
        lines.append("Active Effects:")
        for eff in effects:
            lines.append(f"  {eff}")
    return lines


def _render_system_map(state, tool_name):
    """Return formatted system map lines."""
    import json as _json
    parts = []
    system_map = state.get("system_map")
    if system_map:
        parts.append(f"[SYSTEM MAP]\n{_json.dumps(system_map, indent=2)}\n[/SYSTEM MAP]")
    else:
        parts.append(f"[SYSTEM MAP MISSING — you MUST include system_map in your {tool_name} call this exchange]")
    revealed = state.get("revealed_nodes", [])
    if isinstance(revealed, list) and revealed:
        parts.append(f"Revealed nodes: {', '.join(str(n) for n in revealed)}")
    return parts


def build_hack_injection(hack_state, pipeline_state=None):
    """Build state injection string for CPRED hack exchange user messages."""
    preamble_lines = list(_render_transition(hack_state.get("context")))

    alert_level = _safe_int(hack_state.get("alert_level", 0))
    alert_name = _get_alert_name(alert_level)
    cycles_max = hack_state.get("cycles_max", 3)
    interface_rank = hack_state.get("interface_rank", 4)
    net_actions = hack_state.get("net_actions_per_turn", 3)

    net_actions_remaining = hack_state.get("net_actions_remaining", net_actions)
    meatspace_due = hack_state.get("meatspace_due", False)
    actions_line = f"NET Actions Remaining: {net_actions_remaining}/{net_actions}"
    if _safe_int(net_actions_remaining) <= 0:
        actions_line += " [NO NET ACTIONS LEFT — end NET turn]"
    if meatspace_due:
        actions_line += " [MEATSPACE ROUND DUE — narrate crew's round first]"

    lines = [
        "[HACK STATE]",
        f"Target: {hack_state.get('target_system', 'Unknown')} (SR {hack_state.get('sr', 3)})",
        f"Tier: {str(hack_state.get('tier') or 'full_run').replace('_', ' ').title()}",
        f"Interface Rank: {interface_rank} ({net_actions} NET Actions/turn)",
        actions_line,
        _render_alert_effects(alert_level, alert_name, sr=_safe_int(hack_state.get("sr", 3))),
        f"Cycles: {hack_state.get('cycles_remaining', 0)}/{cycles_max}",
    ]

    lines.extend(_render_active_programs(hack_state.get("active_programs", [])))

    hardware = hack_state.get("installed_hardware", [])
    if isinstance(hardware, list) and hardware:
        lines.append(f"Installed Hardware: {', '.join(str(h) for h in hardware)}")

    lines.append(f"Current Node: {hack_state.get('current_node', 'Gateway')}")
    nodes_visited = hack_state.get("nodes_visited", ["Gateway"])
    if isinstance(nodes_visited, list):
        lines.append(f"Nodes Visited: {', '.join(str(n) for n in nodes_visited)}")
    else:
        lines.append(f"Nodes Visited: {nodes_visited}")

    lines.extend(_render_ice_status(hack_state.get("ice_status", {})))
    lines.extend(_render_trace_progress(hack_state))
    lines.extend(_render_tar_stacks(hack_state))

    brain_dmg = hack_state.get("brain_damage", 0)
    if brain_dmg:
        lines.append(f"Brain Damage This Hack: {brain_dmg}")

    lines.extend(_render_active_effects(hack_state))
    lines.append("[/HACK STATE]")

    parts = []
    if preamble_lines:
        parts.append("\n".join(preamble_lines))
    parts.append("\n".join(lines))
    parts.extend(_render_system_map(hack_state, "report_hack_state"))

    return "\n\n".join(parts)


def _resolve_netrunner_name(character_states, preferred_name=None, game_state=None):
    """Resolve which PC should be treated as the active netrunner."""
    if preferred_name and preferred_name in (character_states or {}):
        entry = character_states[preferred_name]
        data = entry.get("data", entry)
        if data.get("type") == "pc":
            return preferred_name

    edgerunners = game_state.get("edgerunners", {}) if isinstance(game_state, dict) else {}
    first_pc = None
    best_name = None
    best_score = -1
    for name, entry in (character_states or {}).items():
        data = entry.get("data", entry)
        if data.get("type") != "pc":
            continue
        if first_pc is None:
            first_pc = name
        score = 0
        if "netrunner" in str(data.get("class", "")).lower():
            score += 4
        for r in data.get("resources", []):
            label = str(r.get("label", "")).lower()
            if "cycle" in label or "interface" in label or "program" in label:
                score += 2
        er = edgerunners.get(name, {})
        if isinstance(er, dict) and isinstance(er.get("cyberdeck"), dict):
            score += 1
        if score > best_score:
            best_score = score
            best_name = name
    return best_name or first_pc


def build_netrunner_profile(character_states, game_state=None, **_kw):
    """Build compact netrunner profile from character_states + edgerunner state for hack mode context."""
    hack_state = _kw.get("hack_state") or {}
    pc_name = _resolve_netrunner_name(character_states, preferred_name=hack_state.get("hacker_name"), game_state=game_state)
    pc_data = None
    if pc_name:
        entry = (character_states or {}).get(pc_name, {})
        pc_data = entry.get("data", entry)

    if not pc_data:
        return ""

    lines = ["[NETRUNNER PROFILE]"]
    lines.append(f"Name: {pc_name}")

    # Role
    cls = pc_data.get("class", "Unknown")
    lines.append(f"Role: {cls}")

    # Vitals (HP, Humanity)
    vitals_parts = []
    for v in pc_data.get("vitals", []):
        vlabel = v.get("label", "")
        if "current" in v and "max" in v:
            vitals_parts.append(f"{vlabel}: {v['current']}/{v['max']}")
        elif "value" in v:
            vitals_parts.append(f"{vlabel}: {v['value']}")
    if vitals_parts:
        lines.append(" | ".join(vitals_parts))

    # Edgerunner state (HP, Luck, cyberware) if available
    edgerunners = game_state.get("edgerunners", {}) if game_state else {}
    er = edgerunners.get(pc_name, {})
    if er:
        hp = er.get("hp", {})
        luck = er.get("luck", {})
        if hp.get("max"):
            lines.append(f"HP: {hp.get('current', 0)}/{hp.get('max', 40)}{_wound_flag(hp.get('current', 0), seriously_wounded=hp.get('seriously_wounded'), long=True)}")
        if luck.get("max"):
            lines.append(f"Luck: {luck.get('current', 0)}/{luck.get('max', 0)}")
        # Armor
        armor = er.get("armor", {})
        if armor.get("head") or armor.get("body"):
            lines.append(f"Armor: Head SP {armor.get('head', 0)} | Body SP {armor.get('body', 0)}")
        # Eurobucks
        eb = er.get("eurobucks", 0)
        if eb:
            lines.append(f"Eurobucks: {eb:,}")
        # Lifestyle / Housing
        lifestyle = er.get("lifestyle")
        housing = er.get("housing")
        if lifestyle or housing:
            lh_parts = []
            if lifestyle:
                lh_parts.append(f"Lifestyle: {lifestyle}")
            if housing:
                lh_parts.append(f"Housing: {housing}")
            lines.append(" | ".join(lh_parts))
        # Critical Injuries
        injuries = er.get("critical_injuries", [])
        if injuries:
            dv_total = sum(ci.get("dv_mod", 0) for ci in injuries if ci.get("status") != "quick_fixed")
            injury_strs = []
            for ci in injuries:
                qf_tag = " [QF]" if ci.get("status") == "quick_fixed" else ""
                injury_strs.append(f"{ci['name']}{qf_tag} ({ci['effect']}, Death Save +{ci['dv_mod']})")
            lines.append(f"Critical Injuries (Death Save DV +{dv_total}): {'; '.join(injury_strs)}")
        # Death Save count
        death_saves = er.get("death_save_count", 0)
        if death_saves > 0:
            lines.append(f"Death Saves: {death_saves} (cumulative +{death_saves})")
        # Weapons
        weapons = er.get("weapons", [])
        if weapons:
            weapon_strs = []
            for w in weapons:
                wname = w.get("name", "?")
                wdmg = w.get("damage", "?")
                if w.get("type") == "melee":
                    weapon_strs.append(f"{wname} ({wdmg}, {w.get('skill', 'Melee Weapon')})")
                else:
                    weapon_strs.append(f"{wname} ({wdmg}, {w.get('current_ammo', 0)}/{w.get('max_ammo', 0)} ammo)")
            lines.append(f"Weapons: {'; '.join(weapon_strs)}")
        cyberware = er.get("cyberware_effects", [])
        # Filter to NET-relevant cyberware
        net_relevant = [cw for cw in cyberware if any(
            kw in cw.lower() for kw in ["neural", "interface", "virtuality", "cyberdeck", "chipware"]
        )]
        if net_relevant:
            lines.append(f"Relevant Cyberware: {', '.join(net_relevant)}")

    # Hacking-relevant resources (Cycles, Interface, etc.)
    for r in pc_data.get("resources", []):
        rlabel = r.get("label", "")
        if any(kw in rlabel.lower() for kw in ["cycle", "interface", "program", "cyberdeck"]):
            lines.append(f"{rlabel}: {r.get('current', 0)}/{r.get('max', 0)}")

    # Conditions
    conditions = pc_data.get("conditions", [])
    if conditions:
        lines.append(f"Conditions: {', '.join(conditions)}")

    # Cyberdeck + Deck Slots from edgerunner state
    if er:
        cyberdeck = er.get("cyberdeck")
        if isinstance(cyberdeck, dict):
            deck_slots = get_deck_slots(er)
            total_slots = len(deck_slots) if deck_slots else cyberdeck.get("slots", 0)
            used_slots = sum(1 for s in deck_slots if s is not None) if deck_slots else 0
            lines.append(f"Cyberdeck: {cyberdeck.get('tier', '?')} | {used_slots}/{total_slots} slots | {cyberdeck.get('cycles', 0)} cycles")
            programs = [s for s in deck_slots if isinstance(s, dict)
                        and s.get("type", "program") == "program" and not s.get("_continuation_of")]
            if programs:
                prog_strs = [f"{p.get('name', '?')} ({p.get('category', '?')}, {p.get('status', 'stored')})" for p in programs]
                lines.append(f"Programs: {'; '.join(prog_strs)}")
            hardware = [s for s in deck_slots if isinstance(s, dict) and s.get("type") == "hardware"]
            if hardware:
                hw_strs = [f"{h.get('name', '?')} ({h.get('slots_used', 1)} slots)" for h in hardware]
                lines.append(f"Hardware: {'; '.join(hw_strs)}")

    lines.append("[/NETRUNNER PROFILE]")
    return "\n".join(lines)


def _writeback_cycles(runner_name, cycles, pipeline_state):
    """Write remaining cycles back to the PC's character_states resource."""
    if cycles is None:
        return
    cs_items = []
    if runner_name and runner_name in pipeline_state.get("character_states", {}):
        cs_items.append((runner_name, pipeline_state["character_states"][runner_name]))
    cs_items.extend(pipeline_state.get("character_states", {}).items())
    seen = set()
    for name, entry in cs_items:
        if name in seen:
            continue
        seen.add(name)
        d = entry.get("data", entry)
        if d.get("type") == "pc":
            for r in d.get("resources", []):
                if "cycle" in r.get("label", "").lower():
                    r["current"] = cycles
                    break
            break


def _writeback_destroyed_programs(runner_name, state, pipeline_state):
    """Remove permanently-destroyed programs from edgerunner persistent deck_slots.

    Step 2: the Backup Drive save now runs at status-change time via the
    on_program_status_change registry hook (cpred_program_effects.Backup
    Drive), so by the time we reach hack writeback, destroyed_programs
    contains ONLY programs that were not saved. The legacy in-place Backup
    Drive check here is therefore redundant — leaving it as a defensive
    safety net would mask hook regressions, so we drop it.
    """
    game_state = pipeline_state.get("game_state", {})
    destroyed = state.get("destroyed_programs", [])
    if not (isinstance(destroyed, list) and destroyed and runner_name and isinstance(game_state, dict)):
        return
    er = game_state.get("edgerunners", {}).get(runner_name, {})
    deck_slots = get_deck_slots(er)
    if not isinstance(deck_slots, list):
        return
    # Replace destroyed programs with null (preserving slot positions)
    er["deck_slots"] = [
        None if (isinstance(s, dict) and s.get("type", "program") == "program"
                 and s.get("name") in destroyed) else s
        for s in deck_slots
    ]


def apply_hack_writeback(hack_state, pipeline_state):
    """Write back hack results to persistent state after hack completes.
    Note: brain_damage→HP is now applied incrementally during each exchange
    by _apply_brain_damage_hp, so it is NOT re-applied here."""
    hacker_name = hack_state.get("hacker_name")
    _writeback_cycles(hacker_name, hack_state.get("cycles_remaining"), pipeline_state)
    _writeback_destroyed_programs(hacker_name, hack_state, pipeline_state)

    # Fire → nudity at hack end (if never extinguished)
    game_state = pipeline_state.get("game_state", {})
    if hack_state.get("on_fire") and hacker_name and isinstance(game_state, dict):
        fire_rounds = hack_state.get("fire_rounds", 0)
        nudity = "nude" if fire_rounds >= 2 else "partially_nude" if fire_rounds >= 1 else None
        if nudity:
            er = game_state.get("edgerunners", {}).get(hacker_name, {})
            conditions = er.setdefault("conditions", [])
            for old_c in ["partially_nude", "nude"]:
                if old_c in conditions:
                    conditions.remove(old_c)
            conditions.append(nudity)
