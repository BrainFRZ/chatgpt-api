"""CPRED combined NET + meatspace combat mode."""
import copy
import logging

from .cpred_core import _safe_int, _render_transition, _wound_flag
from .cpred_combat import (
    _apply_meatspace_shared,
    _format_vehicle_lines,
    build_cpred_combat_profile,
)
from .cpred_hack import (
    _apply_alert_ice_spawn,
    _apply_brain_damage_hp,
    _apply_initiate_unsafe_jack_out,
    _apply_net_model_fields,
    _apply_persistent_ice_effects,
    _apply_resolver_net_ops,
    _apply_trace_auto_increment,
    _expire_active_debuffs,
    _stamp_debuff_expirations,
    _check_forced_disconnect,
    _get_alert_name,
    _mark_forced_disconnect,
    _net_actions_for_rank,
    _render_active_effects,
    _render_active_programs,
    _render_alert_effects,
    _render_ice_status,
    _render_system_map,
    _render_tar_stacks,
    _render_trace_progress,
    _writeback_cycles,
    _writeback_destroyed_programs,
    build_netrunner_profile,
)

logger = logging.getLogger(__name__)


def init_net_combat_state(
    netrunner_name="",
    target="",
    interface_rank=4,
    cycles_max=3,
    sr=3,
    initiated_from="combat",
    **_kw
):
    """Return initial net_combat state for combined meatspace+NET mode."""
    net_actions = _net_actions_for_rank(interface_rank)
    return {
        "active": True,
        "netrunner": netrunner_name,
        "target": target,
        "initiated_from": initiated_from,
        "interface_rank": interface_rank,
        "net_actions_per_turn": net_actions,
        "start_message_id": None,
        # NET state fields (same as standalone hack)
        "sr": sr,
        "alert_level": 0,
        "cycles_remaining": cycles_max,
        "cycles_max": cycles_max,
        "active_programs": [],
        "installed_hardware": [],
        "current_node": "Gateway",
        "nodes_visited": ["Gateway"],
        "revealed_nodes": ["Gateway"],
        "ice_status": {},
        "trace_progress": None,
        "tar_stacks": 0,
        "brain_damage": 0,
        "system_map": None,
        "available_actions": [],
        # Completion flags
        "combat_complete": False,
        "net_complete": False,
        "narrative_summary": None,
        # Internal tracking for brain damage delta
        "_prev_brain_damage": 0,
    }


def init_net_combat_from_hack(hack_state, combat_info=None):
    """Create net_combat state by carrying over in-progress NET fields from hack_state.

    Unlike init_net_combat_state (fresh defaults), this preserves the mid-hack
    NET encounter — current node, alert level, ICE, programs, etc.
    """
    if not isinstance(hack_state, dict):
        hack_state = {}
    combat_info = combat_info if isinstance(combat_info, dict) else {}
    # Build combined context: original hack context + combat breakout reason
    _parts = []
    if hack_state.get("context"):
        _parts.append(str(hack_state["context"]))
    if combat_info.get("reason"):
        _parts.append(f"Combat breakout: {combat_info['reason']}")
    enemies = combat_info.get("enemies")
    if isinstance(enemies, list):
        enemy_names = [str(e) for e in enemies if e is not None and str(e)]
        if enemy_names:
            _parts.append(f"Hostiles: {', '.join(enemy_names)}")
    _combined_context = " ".join(_parts) if _parts else None

    interface_rank = _safe_int(hack_state.get("interface_rank", 4), default=4)
    cycles_remaining = _safe_int(hack_state.get("cycles_remaining", 3), default=3)
    cycles_max = _safe_int(hack_state.get("cycles_max", 3), default=3)
    alert_level = _safe_int(hack_state.get("alert_level", 0))
    tar_stacks = _safe_int(hack_state.get("tar_stacks", 0))
    brain_damage = _safe_int(hack_state.get("brain_damage", 0))
    nodes_visited = hack_state.get("nodes_visited", ["Gateway"])
    if not isinstance(nodes_visited, list):
        nodes_visited = ["Gateway"]
    active_programs = hack_state.get("active_programs", [])
    if not isinstance(active_programs, list):
        active_programs = []
    installed_hardware = hack_state.get("installed_hardware", [])
    if not isinstance(installed_hardware, list):
        installed_hardware = []
    available_actions = hack_state.get("available_actions", [])
    if not isinstance(available_actions, list):
        available_actions = []
    ice_status = hack_state.get("ice_status", {})
    if not isinstance(ice_status, dict):
        ice_status = {}
    revealed_nodes = hack_state.get("revealed_nodes", [])
    if not isinstance(revealed_nodes, list):
        revealed_nodes = list(nodes_visited)

    net_actions = _net_actions_for_rank(interface_rank)
    nc = {
        "active": True,
        "netrunner": hack_state.get("hacker_name", ""),
        # Standalone hack mode stores this field as target_system.
        "target": hack_state.get("target_system") or hack_state.get("target", ""),
        "initiated_from": "hack",
        "interface_rank": interface_rank,
        "net_actions_per_turn": net_actions,
        "start_message_id": hack_state.get("start_message_id"),
        # NET state carried over from hack (NOT reset to defaults)
        "alert_level": alert_level,
        "cycles_remaining": cycles_remaining,
        "cycles_max": cycles_max,
        "active_programs": copy.deepcopy(active_programs),
        "installed_hardware": list(installed_hardware),
        "current_node": hack_state.get("current_node", "Gateway"),
        "nodes_visited": list(nodes_visited),
        "revealed_nodes": list(revealed_nodes),
        "ice_status": copy.deepcopy(ice_status),
        "trace_progress": hack_state.get("trace_progress"),
        "tar_stacks": tar_stacks,
        "brain_damage": brain_damage,
        "system_map": copy.deepcopy(hack_state.get("system_map")),
        "available_actions": list(available_actions),
        # Hack-specific fields to carry over
        "sr": hack_state.get("sr"),
        "tier": hack_state.get("tier"),
        # Completion flags
        "combat_complete": False,
        "net_complete": False,
        "narrative_summary": None,
        # Brain damage delta tracking starts clean from current value
        "_prev_brain_damage": brain_damage,
        # Alert level threshold tracking for auto-spawn
        "_prev_alert_level": alert_level,
        # Combat breakout context for first-exchange injection
        "_combat_breakout": combat_info,
        # Per-ICE-type persistent effects (carried over from hack)
        "on_fire": hack_state.get("on_fire", False),
        "fire_rounds": hack_state.get("fire_rounds", 0),
        "movement_locked_by": hack_state.get("movement_locked_by"),
        "movement_locked_by_key": hack_state.get("movement_locked_by_key"),
        "slide_penalty": hack_state.get("slide_penalty", 0),
        "net_action_penalty": hack_state.get("net_action_penalty", 0),
        "active_debuffs": copy.deepcopy(hack_state.get("active_debuffs") if isinstance(hack_state.get("active_debuffs"), list) else []),
        "destroyed_programs": list(hack_state.get("destroyed_programs")) if isinstance(hack_state.get("destroyed_programs"), list) else [],
    }
    if _combined_context:
        nc["context"] = _combined_context
    return nc


def apply_net_combat_state(pipeline_state, tool_input, game_state=None, resolver_state_ops=None, **_kw):
    """Apply combined net_combat state updates from report_net_combat_state tool output."""
    if not isinstance(tool_input, dict):
        logger.warning("apply_net_combat_state: tool_input must be an object, got %s",
                       type(tool_input).__name__)
        return

    # --- Meatspace: character_updates, cover, vehicles, combat initiative ---
    _apply_meatspace_shared(pipeline_state, tool_input, game_state=game_state)

    # --- NET: update hack fields in net_combat ---
    nc = pipeline_state.get("net_combat", {})
    if not isinstance(nc, dict):
        nc = {}
    hs = tool_input.get("hack_state", {})

    _apply_net_model_fields(nc, hs, tool_input)
    _apply_resolver_net_ops(nc, resolver_state_ops, game_state)
    _apply_brain_damage_hp(nc, game_state, "netrunner", pipeline_state)

    # Determine if Netrunner took NET actions this exchange (used by multiple sections)
    _has_net_actions = isinstance(hs, dict) and _safe_int(hs.get("net_actions_used", 0)) > 0

    _apply_persistent_ice_effects(nc, hs, game_state, "netrunner", _has_net_actions)
    _apply_trace_auto_increment(nc, _has_net_actions)
    _apply_alert_ice_spawn(nc)
    _stamp_debuff_expirations(nc, pipeline_state)
    _expire_active_debuffs(nc, pipeline_state)

    # --- Completion flags ---
    nc["combat_complete"] = tool_input.get("combat_complete", nc.get("combat_complete", False))
    nc["net_complete"] = tool_input.get("net_complete", nc.get("net_complete", False))
    if nc.get("_forced_disconnect"):
        _mark_forced_disconnect(nc)

    # Model-signaled Unsafe Jack Out (ally unplugs / drags out / self-yanks).
    # Applied before flatline check so model-authored narrative takes precedence.
    if not nc.get("net_complete"):
        _apply_initiate_unsafe_jack_out(nc, tool_input, game_state, "netrunner", pipeline_state)

    # Forced disconnect on flatline only (RAW p.187)
    if not nc.get("net_complete"):
        _check_forced_disconnect(nc, game_state, "netrunner", pipeline_state)

    if nc["combat_complete"] and nc["net_complete"]:
        nc["active"] = False
        nc["narrative_summary"] = tool_input.get("narrative_summary", "Combined engagement concluded.")

    pipeline_state["net_combat"] = nc


def build_net_combat_injection(combat, net_combat, pipeline_state):
    """Build injection string for combined net_combat exchange user messages."""
    lines = []

    nc = net_combat or {}
    lines.extend(_render_transition(nc.get("context")))

    # Meatspace combat state
    if nc.get("combat_complete"):
        lines.append("[MEATSPACE COMBAT STATE]")
        lines.append("Meatspace combat resolved.")
        lines.append("[/MEATSPACE COMBAT STATE]")
    elif combat:
        edgerunners = pipeline_state.get("game_state", {}).get("edgerunners", {})
        cs = pipeline_state.get("character_states", {})
        cover = combat.get("cover", {})
        initiative_order = combat.get("initiative_order", [])
        current_turn = combat.get("current_turn", "")

        lines.append("[MEATSPACE COMBAT STATE]")
        lines.append(f"Round: {combat.get('round', 1)}")
        lines.append("Initiative Order:")
        for name in initiative_order:
            marker = " <- ACTING" if name == current_turn else ""
            parts = []
            er = edgerunners.get(name)
            entry = cs.get(name, {})
            d = entry.get("data", entry)
            combat_data = d.get("combat_data")

            if er:
                hp = er.get("hp", {})
                armor = er.get("armor", {})
                luck = er.get("luck", {})
                parts.append(f"HP {hp.get('current', 0)}/{hp.get('max', 40)}{_wound_flag(hp.get('current', 0), seriously_wounded=hp.get('seriously_wounded'))}")
                parts.append(f"SP H:{armor.get('head', 0)}/B:{armor.get('body', 0)}")
                parts.append(f"Luck {luck.get('current', 0)}/{luck.get('max', 0)}")
            elif combat_data:
                cd_hp_max = combat_data.get("hp_max", 0)
                cd_hp_cur = 0
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        cd_hp_cur = v["current"]
                        break
                cd_armor = combat_data.get("armor", {})
                parts.append(f"HP {cd_hp_cur}/{cd_hp_max}{_wound_flag(cd_hp_cur, max_hp=cd_hp_max)}")
                parts.append(f"SP H:{cd_armor.get('head', 0)}/B:{cd_armor.get('body', 0)}")
            else:
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v and "max" in v:
                        parts.append(f"HP {v['current']}/{v['max']}")
                        break

            cov = cover.get(name, {})
            if cov.get("in_cover"):
                cov_str = f"Cover: {cov.get('cover_type', 'cover')}"
                if cov.get("cover_hp") is not None:
                    cov_str += f" {cov['cover_hp']}HP"
                parts.append(cov_str)

            status = " | ".join(parts) if parts else ""
            lines.append(f"  {name} ({status}){marker}")

        # Vehicles in meatspace
        lines.extend(_format_vehicle_lines(combat.get("vehicles", {})))

        lines.append("[/MEATSPACE COMBAT STATE]")
    else:
        # Hack-originated transition: no combat state yet, agent must bootstrap
        lines.append("[MEATSPACE COMBAT STATE]")
        lines.append("No initiative set. Bootstrap enemies and roll initiative this exchange.")
        breakout = nc.get("_combat_breakout")
        if isinstance(breakout, dict):
            if breakout.get("reason"):
                lines.append(f"Trigger: {breakout['reason']}")
            enemies = breakout.get("enemies")
            if isinstance(enemies, list) and enemies:
                lines.append(f"Hostiles: {', '.join(str(e) for e in enemies)}")
        lines.append("[/MEATSPACE COMBAT STATE]")
    lines.append("")

    # NET state
    if nc.get("net_complete"):
        lines.append("[NET STATE]")
        lines.append("NET encounter resolved.")
        lines.append("[/NET STATE]")
    else:
        _nc_alert_level = _safe_int(nc.get("alert_level", 0))
        alert_name = _get_alert_name(_nc_alert_level)
        lines.append("[NET STATE]")
        lines.append(f"Netrunner: {nc.get('netrunner', '?')}")
        lines.append(f"Target: {nc.get('target', 'Unknown')}")
        lines.append(f"Interface Rank: {nc.get('interface_rank', 4)} ({nc.get('net_actions_per_turn', 3)} NET Actions/turn)")
        lines.append(_render_alert_effects(_nc_alert_level, alert_name, sr=_safe_int(nc.get("sr", 3))))
        lines.append(f"Cycles: {nc.get('cycles_remaining', 0)}/{nc.get('cycles_max', 3)}")
        lines.append(f"Current Node: {nc.get('current_node', 'Gateway')}")
        nodes_visited = nc.get("nodes_visited", ["Gateway"])
        if isinstance(nodes_visited, list):
            lines.append(f"Nodes Visited: {', '.join(str(n) for n in nodes_visited)}")
        else:
            lines.append(f"Nodes Visited: {nodes_visited}")

        # Only show programs if present (no "None" fallback in net_combat)
        programs = nc.get("active_programs", [])
        if isinstance(programs, list) and programs:
            lines.extend(_render_active_programs(programs))

        hardware = nc.get("installed_hardware", [])
        if isinstance(hardware, list) and hardware:
            lines.append(f"Installed Hardware: {', '.join(str(h) for h in hardware)}")

        lines.extend(_render_ice_status(nc.get("ice_status", {})))
        lines.extend(_render_trace_progress(nc, include_warnings=False))
        lines.extend(_render_tar_stacks(nc))

        bd = nc.get("brain_damage", 0)
        if bd:
            lines.append(f"Brain Damage This Run: {bd}")

        lines.extend(_render_active_effects(nc))
        lines.append("[/NET STATE]")

        map_parts = _render_system_map(nc, "report_net_combat_state")
        for mp in map_parts:
            lines.append(f"\n{mp}")

    return "\n".join(lines)


def build_net_combat_profile(character_states, combat, net_combat, game_state=None, **_kw):
    """Build combined profile: combatant roster + netrunner profile."""
    parts = []
    roster = build_cpred_combat_profile(character_states, combat, game_state=game_state)
    if roster:
        parts.append(roster)
    nr_profile = build_netrunner_profile(
        character_states,
        game_state=game_state,
        hack_state=net_combat
    )
    if nr_profile:
        parts.append(nr_profile)
    return "\n\n".join(parts)


def apply_net_combat_writeback(net_combat_state, pipeline_state):
    """Write back net_combat results to persistent state after both theaters complete."""
    netrunner_name = net_combat_state.get("netrunner")
    _writeback_cycles(netrunner_name, net_combat_state.get("cycles_remaining"), pipeline_state)
    _writeback_destroyed_programs(netrunner_name, net_combat_state, pipeline_state)
