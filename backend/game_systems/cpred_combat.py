"""CPRED combat mode — profile building, injection, state application, and shared meatspace helpers."""

import copy
import logging

from combat_state import replace_combat_dict_preserving_backend_keys
from .cpred_tables import VEHICLE_STATS, VEHICLE_UPGRADES
from .cpred_core import _safe_int, _render_transition, _update_seriously_wounded, _wound_flag

logger = logging.getLogger(__name__)


def _build_vehicle_reference_table() -> str:
    """Generate the vehicle reference markdown table from VEHICLE_STATS."""
    nw = max(len(v["name"]) for v in VEHICLE_STATS.values())
    lines = [
        "Vehicle reference table:",
        f"| {'Vehicle':<{nw}} | SDP | SP | Seats | MOVE    | Type |",
        f"|{'-' * (nw + 2)}|-----|----| ------|---------|------|",
    ]
    for v in VEHICLE_STATS.values():
        move = f"{v['move_min']}\u2013{v['move_max']}"
        lines.append(f"| {v['name']:<{nw}} | {v['sdp']:>3} | {v['sp']:>2} | {v['seats']:>5} | {move:<7} | {v['type']:<4} |")
    return "\n".join(lines)


def build_cpred_combat_profile(character_states, combat, game_state=None, **_kw):
    """Build CPRED-specific combatant roster from edgerunner state + character_states."""
    if not character_states and not (game_state and game_state.get("edgerunners")):
        return ""

    initiative_order = combat.get("initiative_order", []) if combat else []
    edgerunners = game_state.get("edgerunners", {}) if game_state else {}

    # Collect all combatant names
    all_names = set()
    all_names.update(initiative_order)
    all_names.update(character_states.keys() if character_states else [])
    all_names.update(edgerunners.keys())

    # Order by initiative, then remaining alphabetically
    if initiative_order:
        ordered_names = list(initiative_order)
        remaining = sorted(n for n in all_names if n not in initiative_order)
        ordered_names.extend(remaining)
    else:
        ordered_names = sorted(all_names)

    lines = ["[COMBATANT ROSTER]"]

    for name in ordered_names:
        er = edgerunners.get(name)
        cs_entry = (character_states or {}).get(name, {})
        d = cs_entry.get("data", cs_entry)
        combat_data = d.get("combat_data")

        if er:
            # PC — read from edgerunner state
            hp = er.get("hp", {})
            armor = er.get("armor", {})
            luck = er.get("luck", {})
            injuries = er.get("critical_injuries", [])
            weapons = er.get("weapons", [])
            cyberware = er.get("cyberware_effects", [])
            char_class = d.get("class", "")
            char_type = d.get("type", "pc")

            label = f"{char_class}" if char_class else char_type.upper()
            lines.append(f"  {name} ({label}):")
            lines.append(f"    HP: {hp.get('current', 0)}/{hp.get('max', 40)}{_wound_flag(hp.get('current', 0), seriously_wounded=hp.get('seriously_wounded'), long=True)}")
            lines.append(f"    Armor: Head SP {armor.get('head', 0)} | Body SP {armor.get('body', 0)}")
            lines.append(f"    Luck: {luck.get('current', 0)}/{luck.get('max', 0)}")

            if weapons:
                weapon_strs = []
                for w in weapons:
                    wname = w.get("name", "?")
                    wdmg = w.get("damage", "?")
                    if w.get("type") == "melee":
                        weapon_strs.append(f"{wname} ({wdmg}, {w.get('skill', 'Melee Weapon')})")
                    else:
                        loaded = w.get("loaded_type") or "basic"
                        ammo_tag = f", {loaded.upper()}" if loaded and loaded != "basic" else ""
                        weapon_strs.append(f"{wname} ({wdmg}, {w.get('current_ammo', 0)}/{w.get('max_ammo', 0)} ammo{ammo_tag})")
                lines.append(f"    Weapons: {'; '.join(weapon_strs)}")

            ammo_pool = er.get("ammo_pool", {}) or {}
            if isinstance(ammo_pool, dict) and ammo_pool:
                pool_strs = []
                for caliber, types in ammo_pool.items():
                    if isinstance(types, dict) and types:
                        type_strs = ", ".join(f"{t}:{n}" for t, n in types.items())
                        pool_strs.append(f"{caliber} [{type_strs}]")
                if pool_strs:
                    lines.append(f"    Ammo Reserves: {'; '.join(pool_strs)}")

            gear = er.get("gear", {}) or {}
            if isinstance(gear, dict) and gear:
                gear_strs = ", ".join(f"{n}× {item}" for item, n in gear.items())
                lines.append(f"    Gear: {gear_strs}")

            outfit = er.get("outfit")
            if isinstance(outfit, dict):
                desc = outfit.get("description") or ""
                rating = outfit.get("style_rating", 0)
                rating_tag = f"+{rating}" if rating > 0 else (str(rating) if rating else "0")
                lines.append(f"    Outfit: {desc} (Style {rating_tag})" if desc else f"    Outfit: Style {rating_tag}")

            if injuries:
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries if ci.get("status") != "quick_fixed")
                injury_strs = []
                for ci in injuries:
                    loc = ci.get("location", "body")
                    qf_tag = " [QF]" if ci.get("status") == "quick_fixed" else ""
                    injury_strs.append(f"{ci['name']}{qf_tag} ({loc}: {ci.get('effect', '')}, DS+{ci.get('dv_mod', 0)})")
                lines.append(f"    Critical Injuries (Death Save +{dv_total}): {'; '.join(injury_strs)}")

            death_saves = er.get("death_save_count", 0)
            if death_saves > 0:
                lines.append(f"    Death Saves: {death_saves} (cumulative +{death_saves})")

            if cyberware:
                lines.append(f"    Cyberware: {', '.join(cyberware)}")

        elif combat_data:
            # Enemy with structured combat_data
            char_type_label = "Enemy" if d.get("type") == "enemy" else d.get("type", "npc").upper()
            cd_hp_max = combat_data.get("hp_max", 0)
            cd_hp_cur = 0
            for v in d.get("vitals", []):
                if v.get("label") == "HP" and "current" in v:
                    cd_hp_cur = v["current"]
                    break
            cd_armor = combat_data.get("armor", {})
            cd_weapons = combat_data.get("weapons", [])
            cd_stats = combat_data.get("stats", {})

            lines.append(f"  {name} ({char_type_label}):")
            lines.append(f"    HP: {cd_hp_cur}/{cd_hp_max}{_wound_flag(cd_hp_cur, max_hp=cd_hp_max, long=True)}")
            lines.append(f"    Armor: Head SP {cd_armor.get('head', 0)} | Body SP {cd_armor.get('body', 0)}")

            if cd_weapons:
                weapon_strs = []
                for w in cd_weapons:
                    wname = w.get("name", "?")
                    wdmg = w.get("damage", "?")
                    ammo = w.get("ammo")
                    mag = w.get("magazine")
                    if ammo is not None and mag is not None:
                        weapon_strs.append(f"{wname} ({wdmg}, {ammo}/{mag} ammo)")
                    else:
                        weapon_strs.append(f"{wname} ({wdmg})")
                lines.append(f"    Weapons: {'; '.join(weapon_strs)}")

            if cd_stats:
                stat_strs = [f"{k} {v}" for k, v in cd_stats.items()]
                lines.append(f"    Stats: {', '.join(stat_strs)}")

            voice = d.get("voice")
            if voice:
                lines.append(f"    Voice: {voice}")

        else:
            # Fallback — NPC from normal mode, not yet bootstrapped with combat_data
            char_class = d.get("class", "")
            char_type = d.get("type", "npc")
            label = char_class if char_class else char_type.upper()
            parts = []
            for v in d.get("vitals", []):
                if v.get("label") == "HP" and "current" in v and "max" in v:
                    parts.append(f"HP {v['current']}/{v['max']}")
            conditions = d.get("conditions", [])
            if conditions:
                parts.append(f"[{', '.join(conditions)}]")
            status = " | ".join(parts) if parts else ""
            lines.append(f"  {name} ({label}): {status}")
            summary = d.get("summary", "")
            if summary:
                lines.append(f"    {summary}")
            voice = d.get("voice")
            if voice:
                lines.append(f"    Voice: {voice}")

    lines.append("[/COMBATANT ROSTER]")
    return "\n".join(lines)


def build_cpred_combat_injection(combat, pipeline_state):
    """Build [COMBAT STATE] injection for CPRED combat mode."""
    if not combat:
        return ""

    edgerunners = pipeline_state.get("game_state", {}).get("edgerunners", {})
    cs = pipeline_state.get("character_states", {})
    cover = combat.get("cover", {})
    current_turn = combat.get("current_turn", "")
    initiative_order = combat.get("initiative_order", [])

    lines = []

    lines.extend(_render_transition(combat.get("context")))

    lines.append("[COMBAT STATE]")
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
            # PC — read from edgerunner state
            hp = er.get("hp", {})
            armor = er.get("armor", {})
            luck = er.get("luck", {})
            injuries = er.get("critical_injuries", [])
            weapons = er.get("weapons", [])

            parts.append(f"HP {hp.get('current', 0)}/{hp.get('max', 40)}{_wound_flag(hp.get('current', 0), seriously_wounded=hp.get('seriously_wounded'))}")
            parts.append(f"SP H:{armor.get('head', 0)}/B:{armor.get('body', 0)}")
            parts.append(f"Luck {luck.get('current', 0)}/{luck.get('max', 0)}")

            if injuries:
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries if ci.get("status") != "quick_fixed")
                parts.append(f"Crits:{len(injuries)} DS+{dv_total}")

            if weapons:
                ammo_strs = []
                for w in weapons:
                    if w.get("type") != "melee":
                        ammo_strs.append(f"{w.get('name', '?')}:{w.get('current_ammo', 0)}")
                if ammo_strs:
                    parts.append(f"Ammo [{', '.join(ammo_strs)}]")

        elif combat_data:
            # Enemy with combat_data
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
            # Fallback
            for v in d.get("vitals", []):
                if v.get("label") == "HP" and "current" in v and "max" in v:
                    parts.append(f"HP {v['current']}/{v['max']}")
                    break

        # Cover info
        cov = cover.get(name, {})
        if cov.get("in_cover"):
            cov_type = cov.get("cover_type", "cover")
            cov_hp = cov.get("cover_hp")
            cover_str = f"Cover: {cov_type}"
            if cov_hp is not None:
                cover_str += f" {cov_hp}HP"
            parts.append(cover_str)

        status = " | ".join(parts) if parts else ""
        lines.append(f"  {name} ({status}){marker}")

    # Vehicle display
    lines.extend(_format_vehicle_lines(combat.get("vehicles", {})))

    lines.append("[/COMBAT STATE]")

    # Virus ledger: persistent across sessions, surfaced for tactical reference
    # (a player may trigger a previously-planted virus during meatspace combat
    # as a distraction or escalation).
    if isinstance(pipeline_state, dict):
        from pipeline import build_virus_ledger_injection
        _v = build_virus_ledger_injection(pipeline_state.get("virus_ledger", {}))
        if _v:
            lines.append("")
            lines.append(_v)

    return "\n".join(lines)


def _format_vehicle_lines(vehicles: dict) -> list:
    """Format vehicle state for injection display. Used by both combat and net_combat builders."""
    lines = []
    if not vehicles:
        return lines
    lines.append("Vehicles:")
    for vname, vdata in vehicles.items():
        if not isinstance(vdata, dict):
            continue
        v_status = vdata.get("status", "active")
        if v_status == "destroyed":
            lines.append(f"  {vname} (DESTROYED)")
            continue
        sdp_cur = vdata.get("sdp_current", 0)
        sdp_max = vdata.get("sdp_max", 0)
        sp = vdata.get("sp", 0)
        move = vdata.get("combat_move", 0)
        v_type = vdata.get("type", "land")
        driver = vdata.get("driver") or "?"
        passengers = [o for o in vdata.get("occupants", []) if o and o != driver]
        upgrades = vdata.get("upgrades", [])
        v_parts = [f"SDP {sdp_cur}/{sdp_max}", f"SP {sp}", f"MOVE {move}", v_type, f"Driver: {driver}"]
        if v_status == "disabled":
            v_parts.insert(0, "DISABLED")
        cover_hp = vdata.get("cover_hp")
        if cover_hp:
            v_parts.append(f"Glass: {cover_hp}HP")
        if passengers:
            v_parts.append(f"Passengers: {', '.join(passengers)}")
        if upgrades:
            v_parts.append(f"Upgrades: {', '.join(upgrades)}")
        lines.append(f"  {vname} ({' | '.join(v_parts)})")
    return lines


def _replace_combat_dict(pipeline_state: dict, new_combat: dict) -> None:
    """Replace pipeline_state['combat'] while preserving backend-owned keys.

    The model's combat dict only contains initiative data. Backend-owned keys
    (vehicles, cover, context, start_message_id) must survive replacement.
    """
    replace_combat_dict_preserving_backend_keys(pipeline_state, new_combat)


def _apply_vehicle_updates(combat_dict, vehicle_updates):
    """Apply vehicle_updates to combat["vehicles"] dict.

    Handles set_vehicle_stats (bootstrap), sdp_delta/sp_delta (from resolver),
    occupants/driver/status (model judgment).
    """
    if not isinstance(vehicle_updates, list) or not isinstance(combat_dict, dict):
        return
    vehicles = combat_dict.setdefault("vehicles", {})

    def _resolve_vehicle_key_case_insensitive(name):
        if not isinstance(name, str):
            return None
        n = name.strip()
        if not n:
            return None
        if n in vehicles:
            return n
        n_cf = n.casefold()
        for existing in vehicles.keys():
            if isinstance(existing, str) and existing.casefold() == n_cf:
                return existing
        return n

    def _normalize_occupants(values):
        if not isinstance(values, list):
            return []
        normalized = []
        for occ in values:
            if isinstance(occ, dict):
                name = occ.get("name")
                if isinstance(name, str):
                    name = name.strip()
                    if name:
                        normalized.append(name)
            elif isinstance(occ, str):
                occ = occ.strip()
                if occ:
                    normalized.append(occ)
        return normalized

    def _normalize_driver(value):
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            return value or None
        if isinstance(value, dict):
            name = value.get("name")
            if isinstance(name, str):
                name = name.strip()
                return name or None
            return None
        return None

    def _normalize_upgrades(values):
        if not isinstance(values, list):
            return []
        # Canonical lookup by key plus human-readable name.
        _by_name = {
            str(v.get("name", "")).strip().lower(): k
            for k, v in VEHICLE_UPGRADES.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
        normalized = []
        seen = set()
        for ug in values:
            if not isinstance(ug, str):
                continue
            raw = ug.strip()
            if not raw:
                continue
            key = raw.lower().replace("-", "_").replace(" ", "_")
            if key in VEHICLE_UPGRADES:
                canonical = key
            else:
                canonical = _by_name.get(raw.lower())
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            normalized.append(canonical)
        return normalized
    for vupd in vehicle_updates:
        if not isinstance(vupd, dict):
            continue
        vname = vupd.get("name")
        if isinstance(vname, str):
            vname = vname.strip()
        if not isinstance(vname, str) or not vname:
            continue
        vname = _resolve_vehicle_key_case_insensitive(vname)

        # Bootstrap via set_vehicle_stats
        svs = vupd.get("set_vehicle_stats")
        if svs and isinstance(svs, dict) and vname not in vehicles:
            sdp_max = max(0, _safe_int(svs.get("sdp_max", 0)))
            sp = max(0, _safe_int(svs.get("sp", 0)))
            combat_move = max(0, _safe_int(svs.get("combat_move", 0)))
            # Determine base seats: explicit in set_vehicle_stats, or lookup from
            # VEHICLE_STATS by matching sdp+type, or fallback to 2.
            _v_type = svs.get("type", "land")
            _base_seats = svs.get("seats")
            if _base_seats is None:
                _base_seats = 2  # fallback
                # Best-effort lookup: prefer name substring match, then first sdp+type match.
                _first_match_seats = None
                for _vs in VEHICLE_STATS.values():
                    if _vs.get("type") == _v_type and _vs.get("sdp") == sdp_max:
                        if _first_match_seats is None:
                            _first_match_seats = _vs.get("seats", 2)
                        if _vs.get("name", "").lower() in vname.lower():
                            _base_seats = _vs.get("seats", 2)
                            break  # name substring match — use this one
                else:
                    if _first_match_seats is not None:
                        _base_seats = _first_match_seats
            else:
                _base_seats = _safe_int(_base_seats, default=None)
                _base_seats = max(1, _base_seats) if _base_seats is not None else 2
            vehicles[vname] = {
                "type": _v_type,
                "sdp_max": sdp_max,
                "sdp_current": sdp_max,
                "sp": sp,
                "combat_move": combat_move,
                "seats": _base_seats,
                "occupants": _normalize_occupants(svs.get("occupants", [])),
                "driver": _normalize_driver(svs.get("driver")),
                "upgrades": _normalize_upgrades(svs.get("upgrades", [])),
                "status": "active",
            }
            # Apply mechanical upgrade bonuses at bootstrap time
            v = vehicles[vname]
            for ug_key in v.get("upgrades", []):
                ug = VEHICLE_UPGRADES.get(ug_key, {})
                if "sp" in ug and ug["sp"] >= v["sp"]:
                    v["sp"] = ug["sp"]
                sdp_bonus = ug.get("sdp_bonus", 0)
                if sdp_bonus > 0:
                    v["sdp_max"] += sdp_bonus
                    v["sdp_current"] += sdp_bonus
                if "cover_hp" in ug:
                    v["cover_hp"] = ug["cover_hp"]
                seats_bonus = ug.get("seats_bonus", 0)
                if seats_bonus > 0:
                    v["seats"] = v.get("seats", 0) + seats_bonus

        if vname not in vehicles:
            continue
        vdata = vehicles[vname]

        # SDP delta (from resolver state_ops merged by main.py)
        sdp_delta = vupd.get("sdp_delta")
        _repaired = False
        if sdp_delta is not None:
            sdp_delta = _safe_int(sdp_delta)
            vdata["sdp_current"] = max(0, min(vdata.get("sdp_max", 0), vdata.get("sdp_current", 0) + sdp_delta))
            _repaired = sdp_delta > 0

        # SP delta (from resolver state_ops merged by main.py)
        sp_delta = vupd.get("sp_delta")
        if sp_delta is not None:
            sp_delta = _safe_int(sp_delta)
            vdata["sp"] = max(0, vdata.get("sp", 0) + sp_delta)

        # Model judgment updates
        if "occupants" in vupd:
            occ = vupd["occupants"]
            if isinstance(occ, list):
                vdata["occupants"] = _normalize_occupants(occ)
        if "driver" in vupd:
            vdata["driver"] = _normalize_driver(vupd["driver"])
        if "status" in vupd:
            st = vupd["status"]
            if isinstance(st, str) and st in ("active", "disabled", "destroyed"):
                vdata["status"] = st
                if st == "destroyed":
                    vdata["sdp_current"] = 0

        # Auto-destroy when SDP hits 0
        if vdata.get("sdp_current", 0) <= 0:
            vdata["status"] = "destroyed"
        elif _repaired and vdata.get("status") == "destroyed":
            vdata["status"] = "active"


def apply_cpred_combat_state(pipeline_state, tool_input, game_state=None, **_kw):
    """Apply CPRED combat state updates from report_combat_state tool output.

    Routes PC updates to edgerunner state, enemy updates to character_states.
    """
    if not isinstance(tool_input, dict):
        logger.warning(
            "CPRED apply_cpred_combat_state: tool_input must be an object, got %s",
            type(tool_input).__name__
        )
        return

    # Snapshot vehicles BEFORE _apply_meatspace_shared — if combat_complete is
    # set, the helper clears pipeline_state["combat"] = None, but a same-call
    # initiate_chase below needs the existing vehicle SDP/SP/MOVE state to
    # carry over. Without this snapshot, mid-combat damage is lost on the
    # combat→chase transition.
    _pre_clear_vehicles_snapshot = copy.deepcopy(
        (pipeline_state.get("combat") or {}).get("vehicles") or {}
    )

    _apply_meatspace_shared(pipeline_state, tool_input, game_state=game_state)

    # --- initiate_net_combat ---
    net_combat = tool_input.get("initiate_net_combat")
    if net_combat and isinstance(net_combat, dict):
        _nc_trigger = {
            "active": True,
            "netrunner": net_combat.get("netrunner", ""),
            "target": net_combat.get("target", ""),
            "initiated_from": "combat"
        }
        if net_combat.get("context"):
            _nc_trigger["context"] = net_combat["context"]
        pipeline_state["net_combat"] = _nc_trigger

    # --- initiate_chase (combat -> Hot Pursuit chase) ---
    chase = tool_input.get("initiate_chase")
    if chase and isinstance(chase, dict):
        from .cpred_chase import init_chase_state
        # Build vehicles dict from initiate_chase payload, with sensible
        # fallbacks pulled from the pre-clear snapshot (so SDP/SP/MOVE carry
        # over from in-progress combat even when combat_complete=True cleared
        # the live combat slot just above).
        existing_vehicles = _pre_clear_vehicles_snapshot or (
            (pipeline_state.get("combat") or {}).get("vehicles") or {}
        )
        vehicles_in = chase.get("vehicles") or {}
        if not isinstance(vehicles_in, dict):
            vehicles_in = {}
        pursuer_set = set(chase.get("pursuer_vehicles") or [])
        vehicles_out = {}
        for vname, v in vehicles_in.items():
            if not isinstance(v, dict):
                continue
            existing = existing_vehicles.get(vname) or {}
            vehicles_out[vname] = {
                "operator": v.get("operator") or existing.get("driver") or "",
                "occupants": list(v.get("occupants") or existing.get("occupants") or []),
                "square": v.get("starting_square", 0),
                "combat_speed_move": v.get("combat_speed_move",
                                           existing.get("combat_move", 20)),
                "sdp_max": v.get("sdp_max", existing.get("sdp_max", 20)),
                "sdp_current": v.get("sdp_current",
                                     existing.get("sdp_current", v.get("sdp_max", 20))),
                "sp": v.get("sp", existing.get("sp", 0)),
                "type": v.get("type", existing.get("type", "land")),
                "upgrades": list(v.get("upgrades") or existing.get("upgrades") or []),
                "status": v.get("status", existing.get("status", "active")),
                "is_pursuer": vname in pursuer_set if pursuer_set else bool(v.get("is_pursuer", False)),
                "notes": v.get("notes", ""),
            }
        if vehicles_out:
            from .cpred_chase import stamp_chase_hud_overlay
            pipeline_state["chase"] = init_chase_state(
                grid_length=chase.get("grid_length", 8),
                vehicles=vehicles_out,
                started_from="combat",
                context=chase.get("context"),
            )
            if chase.get("route"):
                pipeline_state["chase"]["route"] = chase["route"]
            if chase.get("scene"):
                pipeline_state["chase"]["scene"] = chase["scene"]
            stamp_chase_hud_overlay(pipeline_state)


# ---------------------------------------------------------------------------
# Shared helpers (used by both combat and net_combat modes)
# ---------------------------------------------------------------------------

def _apply_character_updates_shared(pipeline_state, character_updates, game_state=None):
    """Shared logic for applying character_updates from combat/net_combat tool output.

    Handles hp_delta, armor_delta, luck_delta, ammo, critical_injury_add/remove,
    conditions_add/remove, set_combat_stats — routing PCs to edgerunner state
    and enemies to character_states.
    """
    if not isinstance(character_updates, list):
        logger.warning("CPRED character_updates: character_updates must be a list, got %s",
                       type(character_updates).__name__)
        return

    edgerunners = game_state.get("edgerunners", {}) if game_state else {}
    cs = pipeline_state.get("character_states", {})

    for upd in character_updates:
        if not isinstance(upd, dict):
            logger.warning("CPRED character_updates: skipping non-object character_update: %r", upd)
            continue
        name = upd.get("name")
        if not isinstance(name, str) or not name:
            if name is not None:
                logger.warning("CPRED character_updates: invalid character_update name: %r", name)
            continue

        is_pc = name in edgerunners

        # --- set_combat_stats (enemy bootstrap) ---
        scs = upd.get("set_combat_stats")
        if scs and not isinstance(scs, dict):
            logger.warning("CPRED character_updates: invalid set_combat_stats shape for %s: %r", name, scs)
            scs = None
        if scs and not is_pc:
            if name not in cs:
                cs[name] = {"data": {"type": "enemy", "class": "", "level": None, "vitals": [], "conditions": []}}
            entry = cs[name]
            d = entry.get("data", entry)
            has_existing_combat_data = bool(d.get("combat_data"))
            if has_existing_combat_data:
                logger.debug("CPRED character_updates: ignoring repeated set_combat_stats for %s", name)
            else:
                scs = copy.deepcopy(scs)
                hp_max = _safe_int(scs.get("hp_max", 0))
                if hp_max == 0 and scs.get("hp_max") not in (0, None, "0"):
                    logger.warning("CPRED character_updates: invalid set_combat_stats.hp_max for %s: %r",
                                   name, scs.get("hp_max"))
                hp_max = max(0, hp_max)
                scs["hp_max"] = hp_max
                d["combat_data"] = copy.deepcopy(scs)
                hp_vitals = d.get("vitals", [])
                hp_found = False
                for v in hp_vitals:
                    if v.get("label") == "HP":
                        if "current" not in v:
                            v["current"] = hp_max
                        v["max"] = hp_max
                        hp_found = True
                        break
                if not hp_found:
                    d.setdefault("vitals", []).append({"label": "HP", "current": hp_max, "max": hp_max})

        # --- hp_delta ---
        hp_delta = upd.get("hp_delta")
        if hp_delta is not None:
            hp_delta = _safe_int(hp_delta, default=None)
            if hp_delta is None:
                logger.warning("CPRED character_updates: invalid hp_delta for %s: %r",
                               name, upd.get("hp_delta"))
        if hp_delta is not None:
            if is_pc:
                er = edgerunners[name]
                er["hp"]["current"] = max(0, min(er["hp"]["max"], er["hp"]["current"] + hp_delta))
                _update_seriously_wounded(er)
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        v["current"] = er["hp"]["current"]
                        break
            else:
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        v["current"] = max(0, v["current"] + hp_delta)
                        break

        # --- armor_delta ---
        armor_delta = upd.get("armor_delta")
        if armor_delta and not isinstance(armor_delta, dict):
            logger.warning("CPRED character_updates: invalid armor_delta shape for %s: %r",
                           name, armor_delta)
            armor_delta = None
        if armor_delta:
            if is_pc:
                er = edgerunners[name]
                for loc in ("head", "body"):
                    delta = _safe_int(armor_delta.get(loc, 0), default=None)
                    if delta is None:
                        logger.warning("CPRED character_updates: invalid armor_delta for %s %s: %r",
                                       name, loc, armor_delta.get(loc))
                        continue
                    if delta:
                        er["armor"][loc] = max(0, er["armor"].get(loc, 0) + delta)
            else:
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                cd = d.get("combat_data")
                if cd:
                    cd_armor = cd.setdefault("armor", {})
                    for loc in ("head", "body"):
                        delta = _safe_int(armor_delta.get(loc, 0), default=None)
                        if delta is None:
                            logger.warning("CPRED character_updates: invalid armor_delta for %s %s: %r",
                                           name, loc, armor_delta.get(loc))
                            continue
                        if delta:
                            cd_armor[loc] = max(0, cd_armor.get(loc, 0) + delta)

        # --- luck_delta ---
        luck_delta = upd.get("luck_delta")
        if luck_delta is not None and is_pc:
            luck_delta = _safe_int(luck_delta, default=None)
            if luck_delta is None:
                logger.warning("CPRED character_updates: invalid luck_delta for %s: %r",
                               name, upd.get("luck_delta"))
        if luck_delta is not None and is_pc:
            er = edgerunners[name]
            er["luck"]["current"] = max(0, min(er["luck"]["max"], er["luck"]["current"] + luck_delta))

        # --- ammo ---
        ammo_updates = upd.get("ammo")
        if ammo_updates and not isinstance(ammo_updates, list):
            logger.warning("CPRED character_updates: invalid ammo shape for %s: %r",
                           name, ammo_updates)
            ammo_updates = None
        if ammo_updates:
            if is_pc:
                er = edgerunners[name]
                for au in ammo_updates:
                    if not isinstance(au, dict):
                        logger.warning("CPRED character_updates: skipping non-object ammo update for %s: %r",
                                       name, au)
                        continue
                    wname = au.get("weapon", "")
                    cur = _safe_int(au.get("current", 0), default=None)
                    if cur is None:
                        logger.warning("CPRED character_updates: invalid ammo.current for %s weapon %s: %r",
                                       name, wname, au.get("current"))
                        continue
                    for w in er.get("weapons", []):
                        if w.get("name") == wname:
                            w["current_ammo"] = max(0, cur)
                            break
            else:
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                cd = d.get("combat_data")
                if cd:
                    for au in ammo_updates:
                        if not isinstance(au, dict):
                            logger.warning("CPRED character_updates: skipping non-object ammo update for %s: %r",
                                           name, au)
                            continue
                        wname = au.get("weapon", "")
                        cur = _safe_int(au.get("current", 0), default=None)
                        if cur is None:
                            logger.warning("CPRED character_updates: invalid ammo.current for %s weapon %s: %r",
                                           name, wname, au.get("current"))
                            continue
                        for w in cd.get("weapons", []):
                            if w.get("name") == wname:
                                w["ammo"] = max(0, cur)
                                break

        # --- ammo_consumed (resolver-generated: subtract rounds from weapon) ---
        ammo_consumed = upd.get("ammo_consumed")
        if ammo_consumed and isinstance(ammo_consumed, list):
            if is_pc:
                er = edgerunners[name]
                for ac in ammo_consumed:
                    if not isinstance(ac, dict):
                        continue
                    wname = ac.get("weapon_name", "")
                    consumed = int(ac.get("rounds_consumed", 0))
                    for w in er.get("weapons", []):
                        if w.get("name") == wname:
                            w["current_ammo"] = max(0, w.get("current_ammo", 0) - consumed)
                            break
            else:
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                cd = d.get("combat_data")
                if cd:
                    for ac in ammo_consumed:
                        if not isinstance(ac, dict):
                            continue
                        wname = ac.get("weapon_name", "")
                        consumed = int(ac.get("rounds_consumed", 0))
                        for w in cd.get("weapons", []):
                            if w.get("name") == wname:
                                w["ammo"] = max(0, w.get("ammo", 0) - consumed)
                                break

        # --- critical_injury_add ---
        critical_injury_add = upd.get("critical_injury_add", [])
        if critical_injury_add and not isinstance(critical_injury_add, list):
            logger.warning("CPRED character_updates: invalid critical_injury_add shape for %s: %r",
                           name, critical_injury_add)
            critical_injury_add = []
        for ci in critical_injury_add:
            if not isinstance(ci, dict):
                logger.warning("CPRED character_updates: skipping non-object critical_injury_add for %s: %r",
                               name, ci)
                continue
            dv_mod = _safe_int(ci.get("dv_mod", 0), default=None)
            if dv_mod is None:
                logger.warning("CPRED character_updates: invalid critical injury dv_mod for %s: %r",
                               name, ci.get("dv_mod"))
                dv_mod = 1
            ci_entry = {
                "name": ci.get("name", "Unknown Injury"),
                "location": ci.get("location", "body"),
                "effect": ci.get("effect", ""),
                "dv_mod": dv_mod
            }
            if is_pc:
                er = edgerunners[name]
                er.setdefault("critical_injuries", []).append(ci_entry)
                cond_str = f"Critical Injury: {ci_entry['name']}"
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                conds = d.setdefault("conditions", [])
                if cond_str not in conds:
                    conds.append(cond_str)
            else:
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                cond_str = f"Critical Injury: {ci_entry['name']}"
                conds = d.setdefault("conditions", [])
                if cond_str not in conds:
                    conds.append(cond_str)

        # --- critical_injury_remove ---
        critical_injury_remove = upd.get("critical_injury_remove", [])
        if critical_injury_remove and not isinstance(critical_injury_remove, list):
            logger.warning("CPRED character_updates: invalid critical_injury_remove shape for %s: %r",
                           name, critical_injury_remove)
            critical_injury_remove = []
        for ci_name in critical_injury_remove:
            if is_pc:
                er = edgerunners[name]
                er["critical_injuries"] = [
                    ci for ci in er.get("critical_injuries", []) if ci.get("name") != ci_name
                ]
                cond_str = f"Critical Injury: {ci_name}"
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                conds = d.get("conditions", [])
                if cond_str in conds:
                    conds.remove(cond_str)
            else:
                cond_str = f"Critical Injury: {ci_name}"
                entry = cs.get(name, {})
                d = entry.get("data", entry)
                conds = d.get("conditions", [])
                if cond_str in conds:
                    conds.remove(cond_str)

        # --- conditions_add / conditions_remove ---
        entry = cs.get(name, {})
        d = entry.get("data", entry)
        conditions = d.setdefault("conditions", [])
        conditions_add = upd.get("conditions_add", [])
        if conditions_add and not isinstance(conditions_add, list):
            logger.warning("CPRED character_updates: invalid conditions_add shape for %s: %r",
                           name, conditions_add)
            conditions_add = []
        for cond in conditions_add:
            if cond not in conditions:
                conditions.append(cond)
        conditions_remove = upd.get("conditions_remove", [])
        if conditions_remove and not isinstance(conditions_remove, list):
            logger.warning("CPRED character_updates: invalid conditions_remove shape for %s: %r",
                           name, conditions_remove)
            conditions_remove = []
        for cond in conditions_remove:
            if cond in conditions:
                conditions.remove(cond)


def _apply_meatspace_shared(pipeline_state, tool_input, game_state=None):
    """Shared meatspace state: character_updates, cover, vehicles, combat dict."""
    # 1. Character updates
    _apply_character_updates_shared(
        pipeline_state,
        tool_input.get("character_updates", []),
        game_state=game_state
    )

    # 2. Cover state
    cover_updates = tool_input.get("cover_state")
    if cover_updates is not None and not isinstance(cover_updates, list):
        logger.warning("CPRED meatspace: cover_state must be a list, got %s",
                       type(cover_updates).__name__)
        cover_updates = None

    def _apply_cover_updates(combat_dict):
        if cover_updates is None or not isinstance(combat_dict, dict):
            return
        cover_dict = {}
        has_valid = False
        for cov in cover_updates:
            if not isinstance(cov, dict):
                logger.warning("CPRED meatspace: skipping non-object cover_state entry: %r", cov)
                continue
            cov_name = cov.get("name")
            if isinstance(cov_name, str) and cov_name:
                has_valid = True
                cover_dict[cov_name] = {
                    "in_cover": cov.get("in_cover", False),
                    "cover_type": cov.get("cover_type"),
                    "cover_hp": cov.get("cover_hp")
                }
            elif cov_name is not None:
                logger.warning("CPRED meatspace: invalid cover_state name: %r", cov_name)
        if has_valid or cover_updates == []:
            combat_dict["cover"] = cover_dict

    old_combat = pipeline_state.get("combat")
    if isinstance(old_combat, dict):
        _apply_cover_updates(old_combat)

    # 3. Combat dict replacement
    _has_combat_field = "combat" in tool_input
    new_combat = tool_input.get("combat")
    vehicle_updates = tool_input.get("vehicle_updates")
    if tool_input.get("combat_complete") or (_has_combat_field and new_combat is None):
        # Preserve legacy ordering: apply final vehicle deltas before clearing combat.
        old_combat = pipeline_state.get("combat")
        if vehicle_updates and isinstance(vehicle_updates, list) and isinstance(old_combat, dict):
            _apply_vehicle_updates(old_combat, vehicle_updates)
        pipeline_state["combat"] = None
    elif isinstance(new_combat, dict):
        _replace_combat_dict(pipeline_state, new_combat)
        combat_dict = pipeline_state.get("combat")
        if isinstance(combat_dict, dict):
            # Re-apply cover updates after replacement so same-turn initialization keeps cover state.
            _apply_cover_updates(combat_dict)
            # Apply deltas after replacement so they cannot be overwritten by model payload.
            if vehicle_updates and isinstance(vehicle_updates, list):
                _apply_vehicle_updates(combat_dict, vehicle_updates)
    elif vehicle_updates and isinstance(vehicle_updates, list):
        # No replacement this turn: apply to existing combat state if active.
        combat_dict = pipeline_state.get("combat")
        if isinstance(combat_dict, dict):
            _apply_vehicle_updates(combat_dict, vehicle_updates)
