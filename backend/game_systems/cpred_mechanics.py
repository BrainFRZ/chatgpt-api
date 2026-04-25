"""
Cyberpunk RED deterministic mechanics resolution.

Pure functions — each calls random.randint() internally for dice,
accepts structured input, and returns results with formatted strings.
No side effects, no state mutation.

Shared by both pipeline (Phase 2) and single-agent tool call (Phase 3).
"""
import logging
import random
from typing import Optional

from .cpred_identity import (
    attach_state_op_subject,
    build_relationship_context,
    normalize_cpred_name,
    state_op_subject_name,
)
from .cpred_tables import (
    CRIT_INJURY_BODY,
    CRIT_INJURY_HEAD,
    RANGED_DV_TABLE,
    AUTOFIRE_DV_TABLE,
    AIMED_SHOT_DV_PENALTY,
    ICE_STAT_BLOCKS,
    PROGRAM_STATS,
    DRIVING_CHECK_DVS,
    RAMMING_DAMAGE_DICE,
    RAMMING_NOS_BONUS_DICE,
    PEDESTRIAN_DODGE_DV,
    VEHICLE_WEAK_POINT_MOVING_DV,
    SPIKE_STRIP_DV,
    SPIKE_STRIP_DAMAGE_DICE,
    NIGHT_MARKET_DV,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DV tier map (skill_check difficulty names → numeric DVs)
# ---------------------------------------------------------------------------

_DV_TIERS = {
    "simple": 9,
    "everyday": 13,
    "difficult": 15,
    "professional": 17,
    "heroic": 21,
    "incredible": 24,
    "legendary": 29,
}

# ---------------------------------------------------------------------------
# NET Interface Abilities (closed enum)
# ---------------------------------------------------------------------------
# Required tag on NET-context skill_check / opposed_check actions. Identifies
# *which* Interface Ability is being rolled so program effect hooks (e.g.
# Worm +2 on Backdoor) can fire on the right roll. "Initiative" is included
# for Speedy Gonzalvez's NET-initiative bonus per CPRED p.205 — technically
# not an Interface Ability, but it shares the same hook surface.
INTERFACE_ABILITIES = frozenset({
    "Backdoor",
    "Cloak",
    "Control",
    "Eye-Dee",
    "Pathfinder",
    "Slide",
    "Virus",
    "Zap",
    "Initiative",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roll_d10() -> int:
    return random.randint(1, 10)


def _roll_d6() -> int:
    return random.randint(1, 6)


def _roll_check_die() -> dict:
    """Roll a d10 with exploding 10 and fumble 1 (non-chaining).

    Returns dict with {base, extra, total} where:
      - base: the initial d10 roll
      - extra: additional d10 (added if base=10, subtracted if base=1), or None
      - total: effective die result
    """
    base = _roll_d10()
    extra = None
    if base == 10:
        extra = _roll_d10()
        return {"base": base, "extra": extra, "total": base + extra}
    elif base == 1:
        extra = _roll_d10()
        return {"base": base, "extra": extra, "total": base - extra}
    return {"base": base, "extra": None, "total": base}


def _format_die(die_result: dict) -> str:
    """Format a check die result for display.

    Examples: d10[**7**], d10[**10** + **3**], d10[**1** − **4**]
    """
    base = die_result["base"]
    extra = die_result["extra"]
    if extra is not None:
        if base == 10:
            return f"d10[**{base}** + **{extra}**]"
        else:  # base == 1
            return f"d10[**{base}** − **{extra}**]"
    return f"d10[**{base}**]"


def _critical_injury_state_op(edgerunner: str, injury: dict, reason: str) -> dict:
    """Build a critical injury state op in apply_game_state-compatible shape."""
    return attach_state_op_subject({
        "op": "critical_injury",
        "action": "add",
        "name": injury.get("name", ""),
        "effect": injury.get("effect", ""),
        "dv_mod": int(injury.get("dv_mod", 0)),
        "location": injury.get("location", "body"),
        "reason": reason,
    }, "edgerunner", edgerunner)


def _luck_spend_state_op(edgerunner: str, luck_spent: int, reason: str) -> Optional[dict]:
    """Emit a Luck spend op for positive Luck usage."""
    spend = _to_int(luck_spent, 0, minimum=0)
    if spend <= 0:
        return None
    return attach_state_op_subject({
        "op": "luck",
        "change": -spend,
        "reason": reason,
    }, "edgerunner", edgerunner)


def _emit_luck_op_if_rolled(
    all_state_ops: list,
    result: dict,
    actor_name: str,
    luck_spent: int,
    reason: str,
) -> None:
    """Emit luck-spend state_op unless the result has an error or no roll occurred.

    Resolvers that auto-succeed without rolling set their die key to None
    (e.g. find_item for Cheap/Everyday).  This guard prevents Luck deduction
    when no die was actually rolled.
    """
    if result.get("error"):
        return
    luck_op = _luck_spend_state_op(actor_name, luck_spent, reason)
    if not luck_op:
        return
    # Auto-success guard: resolvers that skip rolling set die/roll to None
    for key in ("die", "roll", "attack_roll"):
        if key in result and result[key] is None:
            return
    all_state_ops.append(luck_op)


def _iter_critical_injuries(damage: dict) -> list:
    """Normalize damage result critical injuries to a list."""
    injuries = damage.get("critical_injuries")
    if isinstance(injuries, list):
        return [ci for ci in injuries if isinstance(ci, dict)]
    injury = damage.get("crit_injury")
    return [injury] if isinstance(injury, dict) else []


def _as_bool(value, default: bool = False) -> bool:
    """Best-effort boolean coercion for tool inputs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "y", "on"):
            return True
        if v in ("false", "0", "no", "n", "off", ""):
            return False
    return default


def _norm_name(value) -> str:
    """Normalize free-text entity names used as tracking keys."""
    return str(value or "").strip()


def _lookup_edgerunner(edgerunner_states: dict, name: str) -> dict | None:
    """Case-insensitive lookup into the edgerunners dict, returns state or None."""
    if not edgerunner_states or not name:
        return None
    key = _norm_name(name)
    if key in edgerunner_states:
        val = edgerunner_states[key]
        return val if isinstance(val, dict) else None
    key_cf = key.casefold()
    for k, v in edgerunner_states.items():
        if str(k).strip().casefold() == key_cf:
            return v if isinstance(v, dict) else None
    return None


def _find_character_state(name, edgerunner_states, character_states):
    """Look up a character in edgerunner_states then character_states.

    Returns (stats_dict, skills_dict, hp_current, hp_max, rep) with values
    from whichever source matches first, or (None, None, None, None, None)
    if not found.
    """
    if not isinstance(name, str) or not name.strip():
        return None, None, None, None, None

    er = _lookup_edgerunner(edgerunner_states, name) if edgerunner_states else None
    if isinstance(er, dict):
        stats = er.get("stats") or {}
        skills = er.get("skills") or {}
        hp = er.get("hp") or {}
        return stats, skills, hp.get("current"), hp.get("max", 40), er.get("rep", 0)

    if isinstance(character_states, dict):
        char_cf = name.strip().casefold()
        for cs_name, cs_val in character_states.items():
            if str(cs_name).strip().casefold() == char_cf:
                if isinstance(cs_val, dict):
                    d = cs_val.get("data", cs_val) if isinstance(cs_val.get("data"), dict) else cs_val
                    cd = d.get("combat_data") if isinstance(d, dict) else None
                    if isinstance(cd, dict):
                        stats = cd.get("stats") or {}
                        hp_max = cd.get("hp_max")
                        hp_cur = None
                        for v in (d.get("vitals") or []):
                            if isinstance(v, dict) and v.get("label") == "HP":
                                hp_cur = v.get("current")
                                break
                        return stats, {}, hp_cur, hp_max, 0
                break

    return None, None, None, None, None


def _derive_seriously_wounded(hp_current, hp_max):
    """Derive seriously_wounded flag from HP values. Returns bool or None if indeterminate."""
    if not isinstance(hp_current, (int, float)) or not isinstance(hp_max, (int, float)):
        return None
    hp_current = int(hp_current)
    hp_max = int(hp_max)
    if hp_current < 0 or hp_max <= 0:
        return None
    if hp_current == 0:
        return False  # mortally wounded — separate condition
    return hp_current < (hp_max + 1) // 2


def _lookup_stat_ci(stats_dict: dict, stat_name: str):
    """Case-insensitive stat lookup. Returns int value or None."""
    if not isinstance(stats_dict, dict) or not stat_name:
        return None
    # Try exact match first
    val = stats_dict.get(stat_name)
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError, OverflowError):
            return None
    # Case-insensitive fallback
    key_cf = stat_name.strip().casefold()
    for k, v in stats_dict.items():
        if str(k).strip().casefold() == key_cf:
            try:
                return int(v)
            except (TypeError, ValueError, OverflowError):
                return None
    return None


def _hydrate_stats_from_state(
    action: dict,
    edgerunner_states: dict,
    character_states: dict,
    *,
    character_key: str = "character",
    stat_value_key: str = "stat_value",
    skill_value_key: str = "skill_value",
    stat_name_key: str = "stat",
    skill_name_key: str = "skill",
    wounded_key: str = "seriously_wounded",
):
    """Resolve name-based stat/skill fields to numeric values from state.

    Mutates `action` in-place. Falls back to model-provided numeric values
    when the character is not found in any state source.
    """
    char_name = action.get(character_key)
    if not isinstance(char_name, str) or not char_name.strip():
        return

    stat_name = action.get(stat_name_key)
    skill_name = action.get(skill_name_key)

    # 1. Try edgerunner state (PCs)
    er = _lookup_edgerunner(edgerunner_states, char_name) if edgerunner_states else None
    if isinstance(er, dict):
        er_stats = er.get("stats") or {}
        er_skills = er.get("skills") or {}

        if isinstance(stat_name, str) and stat_name.strip():
            resolved = _lookup_stat_ci(er_stats, stat_name)
            if resolved is not None:
                action[stat_value_key] = resolved

        if isinstance(skill_name, str) and skill_name.strip():
            resolved = _lookup_stat_ci(er_skills, skill_name)
            if resolved is not None:
                action[skill_value_key] = resolved

        # Derive seriously_wounded from HP
        hp = er.get("hp")
        if isinstance(hp, dict):
            sw = _derive_seriously_wounded(hp.get("current", -1), hp.get("max", 40))
            if sw is not None:
                action[wounded_key] = sw
        return

    # 2. Try character_states (NPCs with combat_data)
    if isinstance(character_states, dict):
        cs_entry = None
        # Case-insensitive lookup
        char_cf = char_name.strip().casefold()
        for cs_name, cs_val in character_states.items():
            if str(cs_name).strip().casefold() == char_cf:
                cs_entry = cs_val
                break

        if isinstance(cs_entry, dict):
            d = cs_entry.get("data", cs_entry) if isinstance(cs_entry.get("data"), dict) else cs_entry
            cd = d.get("combat_data") if isinstance(d, dict) else None
            if isinstance(cd, dict):
                npc_stats = cd.get("stats") or {}

                if isinstance(stat_name, str) and stat_name.strip():
                    resolved = _lookup_stat_ci(npc_stats, stat_name)
                    if resolved is not None:
                        action[stat_value_key] = resolved

                if isinstance(skill_name, str) and skill_name.strip():
                    # NPCs typically don't have separate skills in combat_data.stats,
                    # but if present, use them
                    resolved = _lookup_stat_ci(npc_stats, skill_name)
                    if resolved is not None:
                        action[skill_value_key] = resolved

                # Derive seriously_wounded from vitals HP
                for v in d.get("vitals", []):
                    if isinstance(v, dict) and v.get("label") == "HP":
                        hp_cur = v.get("current")
                        hp_max = cd.get("hp_max")
                        sw = _derive_seriously_wounded(hp_cur, hp_max)
                        if sw is not None:
                            action[wounded_key] = sw
                        break
            return

    # 3. Not found — leave model-provided values as-is


def _norm_vehicle_track_key(name: str, sp: bool = False) -> str:
    """Canonical vehicle tracking key (case-insensitive, trimmed)."""
    base = _norm_name(name).casefold()
    if not base:
        return ""
    return f"{base}:sp" if sp else base


def _norm_hp_track_key(name: str) -> str:
    """Canonical HP tracking key (case-insensitive, trimmed)."""
    return _norm_name(name).casefold()


def _normalize_ranged_weapon_type(weapon_type: str) -> str:
    """Best-effort normalize weapon type names for DV table lookups."""
    raw = _norm_name(weapon_type)
    if not raw:
        return raw
    if raw in RANGED_DV_TABLE:
        return raw
    raw_cf = raw.casefold()
    for key in RANGED_DV_TABLE.keys():
        if isinstance(key, str) and key.casefold() == raw_cf:
            return key
    return raw


def _to_int(value, default: int = 0, minimum: Optional[int] = None) -> int:
    """Best-effort integer coercion with optional lower bound."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


# ---------------------------------------------------------------------------
# Pre-dispatch action normalization
# ---------------------------------------------------------------------------

_INT_FIELDS: frozenset = frozenset({
    "stat_value", "skill_value", "dv", "damage_dice", "rof", "target_sp",
    "range_bracket", "body_stat", "death_save_count",
    "attacker_stat", "defender_stat", "attacker_skill", "defender_skill",
    "autofire_multiplier", "luck_spent", "attacker_ref", "attacker_autofire",
    "stealth_stat", "stealth_skill", "interface_rank", "program_atk",
    "program_damage_dice", "target_def", "target_rez",
    "target_program_def", "target_program_rez",
    "pedestrian_dex", "pedestrian_evasion", "role_ability_rank", "payout",
    "rank", "buyer_cool", "buyer_trading", "vendor_cool", "vendor_trading",
    "item_price", "base_discount",
    "initiator_cool", "initiator_concentration", "initiator_rep",
    "opponent_cool", "opponent_concentration", "opponent_rep",
    "target_stat_value", "target_skill_value", "rel_bonus",
    "target_vehicle_sp", "roms_death_save_bonus",
})

_BOOL_FIELDS: frozenset = frozenset({
    "seriously_wounded", "seriously_wounded_attacker", "seriously_wounded_defender",
    "seriously_wounded_initiator", "seriously_wounded_opponent",
    "seriously_wounded_pedestrian", "seriously_wounded_target",
    "is_ap", "is_rubber", "is_brawling",
    "target_is_vehicle", "target_moving", "combat_plow", "nos_boosted",
    "pedestrian_dodge", "zap",
})

_NAME_FIELDS: frozenset = frozenset({
    "character", "target", "target_name", "weapon_name", "vehicle_name",
    "target_vehicle_name", "target_driver", "target_vehicle_type",
    "program_name", "ice_type", "item_name", "role", "price_category",
    "maneuver", "hit_location", "weapon_type", "check_context",
    "attacker_label", "defender_label", "attacker_skill_label",
    "defender_skill_label", "on_hit", "on_miss", "on_success", "on_failure",
})

_NESTED_SCHEMAS: dict = {
    "suppressive_fire": [("targets", {
        "name": _norm_name, "will": _to_int,
        "concentration": _to_int, "seriously_wounded": _as_bool,
    })],
    "ambush": [("targets", {
        "name": _norm_name, "perception_stat": _to_int,
        "perception_skill": _to_int,
    })],
    "initiative": [("combatants", {
        "name": _norm_name, "ref": _to_int,
    })],
}


def _normalize_action(action) -> dict | None:
    """Coerce action dict field types before dispatch.

    Returns a shallow copy with int/bool/name fields coerced and nested
    target lists filtered. Returns None for non-dict input.
    Only touches fields that are PRESENT — absent fields are left to
    the dispatcher's .get(key, default) calls.
    """
    if not isinstance(action, dict):
        return None
    a = dict(action)
    for key in _INT_FIELDS:
        if key in a:
            a[key] = _to_int(a[key], 0)
    for key in _BOOL_FIELDS:
        if key in a:
            a[key] = _as_bool(a[key], False)
    for key in _NAME_FIELDS:
        if key in a:
            a[key] = _norm_name(a[key])
    # Ramming: non-empty string target_is_vehicle defaults to True
    action_type = a.get("type")
    if action_type == "ramming" and "target_is_vehicle" in action:
        raw = action["target_is_vehicle"]
        if isinstance(raw, str) and raw.strip():
            a["target_is_vehicle"] = _as_bool(raw, True)
    # Normalize weapon_type for DV table lookups
    if "weapon_type" in a and a["weapon_type"]:
        a["weapon_type"] = _normalize_ranged_weapon_type(a["weapon_type"])
    # Nested list normalization (convert strings to name-dicts, coerce fields)
    for list_field, field_schema in _NESTED_SCHEMAS.get(action_type, []):
        raw_list = a.get(list_field)
        if not isinstance(raw_list, list):
            continue
        normalized = []
        for entry in raw_list:
            if isinstance(entry, str):
                entry = {"name": entry}
            elif not isinstance(entry, dict):
                normalized.append(entry)  # preserve for resolver to handle
                continue
            entry = dict(entry)
            for fname, coercer in field_schema.items():
                if fname in entry:
                    entry[fname] = coercer(entry[fname])
            normalized.append(entry)
        a[list_field] = normalized
    return a


def _lookup_crit_injury(hit_location: str) -> dict:
    """Roll 2d6 and look up a critical injury from the appropriate table."""
    d1 = _roll_d6()
    d2 = _roll_d6()
    total = d1 + d2
    table = CRIT_INJURY_HEAD if hit_location == "head" else CRIT_INJURY_BODY
    injury = table.get(total, table[7])  # default to Foreign Object
    return {
        "roll": [d1, d2],
        "total": total,
        "name": injury["name"],
        "effect": injury["effect"],
        "dv_mod": injury["dv_mod"],
        "location": hit_location,
    }


# ---------------------------------------------------------------------------
# resolve_check
# ---------------------------------------------------------------------------

def resolve_check(
    stat_value: int,
    skill_value: int,
    dv: int,
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    wb_boost: int = 0,
) -> dict:
    """Resolve a skill check: d10 + STAT + Skill vs DV.

    Returns dict with full breakdown and formatted string.
    """
    die = _roll_check_die()

    # Build modifier list
    modifiers = []
    total = die["total"]

    total += stat_value
    total += skill_value

    if seriously_wounded:
        total -= 2
        modifiers.append(("Wounded", -2))

    # Luck spends are uncapped (bounded by available Luck in state layer).
    # Relationship bonus is clamped to [-5, +5].
    # WB boost (+1 from Buoyant/Excellent NPC) counts against the +5 rel cap.
    clamped_luck = max(0, luck_spent)
    clamped_rel = max(-5, min(5, rel_bonus + (1 if wb_boost else 0)))

    if clamped_luck > 0:
        total += clamped_luck
        modifiers.append(("Luck", clamped_luck))
    if clamped_rel != 0:
        total += clamped_rel
        label = "RS"
        if wb_boost:
            label = "RS+WB"
        modifiers.append((label, clamped_rel))

    success = total > dv  # must BEAT the DV

    # Build formatted string
    parts = [_format_die(die)]
    parts.append(f"+STAT {stat_value}")
    parts.append(f"+Skill {skill_value}")
    for label, val in modifiers:
        parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
    parts.append(f"= {total} vs DV{dv}")
    parts.append("✓" if success else "✗")
    formatted = " ".join(parts)

    return {
        "die": die,
        "stat_value": stat_value,
        "skill_value": skill_value,
        "modifiers": modifiers,
        "total": total,
        "dv": dv,
        "success": success,
        "formatted": formatted,
    }


# ---------------------------------------------------------------------------
# resolve_hustle
# ---------------------------------------------------------------------------

def resolve_hustle(
    role: str,
    role_ability_rank: int,
    dv: int,
    payout: int,
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    character: str = "",
    on_success: str = "",
    on_failure: str = "",
) -> dict:
    """Resolve a Hustle downtime income roll: d10 + Role Ability Rank vs DV.

    On success, auto-emits a eurobucks state_op with the payout.
    Returns dict with full breakdown and formatted string.
    """
    die = _roll_check_die()

    modifiers = []
    total = die["total"] + role_ability_rank

    if seriously_wounded:
        total -= 2
        modifiers.append(("Wounded", -2))

    clamped_luck = max(0, _to_int(luck_spent, 0))
    if clamped_luck > 0:
        total += clamped_luck
        modifiers.append(("Luck", clamped_luck))

    success = total > dv  # must BEAT the DV

    # Build formatted string
    parts = [f"Hustle ({role}):", _format_die(die), f"+Role {role_ability_rank}"]
    for label, val in modifiers:
        parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
    parts.append(f"= {total} vs DV{dv}")

    state_ops = []
    effective_payout = max(0, payout) if success else 0
    if success:
        parts.append(f"✓ — earned {effective_payout}eb")
        state_ops.append({
            "edgerunner": character,
            "op": "eurobucks",
            "change": effective_payout,
            "reason": f"Hustle ({role})",
        })
    else:
        parts.append("✗ — no payout")

    formatted = " ".join(parts)
    on_outcome = on_success if success else on_failure

    return {
        "die": die,
        "role": role,
        "role_ability_rank": role_ability_rank,
        "modifiers": modifiers,
        "total": total,
        "dv": dv,
        "success": success,
        "payout": effective_payout,
        "state_ops": state_ops,
        "formatted": formatted,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_find_item  — Night market availability check
# ---------------------------------------------------------------------------

def resolve_find_item(
    rank: int,
    price_category: str,
    item_name: str = "",
    character: str = "",
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    on_success: str = "",
    on_failure: str = "",
) -> dict:
    """Roll d10 + Fixer Rank (or Streetwise) vs DV by price category.

    Auto-succeeds for Cheap/Everyday (DV 0).  Returns dict with breakdown.
    """
    dv = NIGHT_MARKET_DV.get(price_category, 17)  # default Expensive if unknown

    # Auto-success for DV 0 categories
    if dv == 0:
        return {
            "die": None,
            "rank": rank,
            "total": None,
            "dv": 0,
            "success": True,
            "item_name": item_name,
            "price_category": price_category,
            "state_ops": [],
            "formatted": f"Find Item ({item_name}): {price_category} — auto-success (no roll needed)",
            "on_outcome": on_success,
        }

    die = _roll_check_die()
    modifiers = []
    total = die["total"] + rank

    if seriously_wounded:
        total -= 2
        modifiers.append(("Wounded", -2))

    clamped_luck = max(0, _to_int(luck_spent, 0))
    if clamped_luck > 0:
        total += clamped_luck
        modifiers.append(("Luck", clamped_luck))

    success = total > dv  # must BEAT the DV

    label = item_name or price_category
    parts = [f"Find Item ({label}):", _format_die(die), f"+Rank {rank}"]
    for mod_label, val in modifiers:
        parts.append(f"+{mod_label} {val}" if val > 0 else f"{mod_label} {val}")
    parts.append(f"= {total} vs DV{dv}")

    if success:
        parts.append(f"✓ — {item_name or 'item'} available")
    else:
        parts.append(f"✗ — {item_name or 'item'} not found")

    formatted = " ".join(parts)
    on_outcome = on_success if success else on_failure

    return {
        "die": die,
        "rank": rank,
        "total": total,
        "dv": dv,
        "success": success,
        "item_name": item_name,
        "price_category": price_category,
        "state_ops": [],
        "formatted": formatted,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_haggle  — Opposed price negotiation
# ---------------------------------------------------------------------------

def _operator_discount_pct(operator_rank: int) -> int:
    """Fixed Operator discount table (CRB p.160).

    Ranks 1-8: 10% off
    Rank 9+:   20% off
    Ranks <=0: not eligible (caller should bail before this).
    """
    r = _to_int(operator_rank, 0)
    if r >= 9:
        return 20
    if r >= 1:
        return 10
    return 0


def resolve_haggle(
    buyer_cool: int,
    buyer_trading: int,
    vendor_cool: int,
    vendor_trading: int,
    operator_rank: int = 0,
    item_name: str = "",
    item_price: int = 0,
    character: str = "",
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    on_success: str = "",
    on_failure: str = "",
    # Legacy no-op (pre-RAW). Retained so old callers don't crash; ignored.
    base_discount: int = None,
) -> dict:
    """Negotiate a listed market price via the Fixer's Operator Role Ability (CRB p.160).

    RAW: only a Fixer can reduce a listed market price. Non-Fixers can use
    Trading for a "good bargain" (p.140) — bartering, negotiating services,
    non-listed goods, or resisting a Fixer's haggle — but not to shave the
    published eb price of an item. For those, use a plain skill_check with
    Trading instead of this op.

    Roll: d10 + buyer_cool + buyer_trading + operator_rank vs
          d10 + vendor_cool + vendor_trading

    Discount on success: fixed by Operator rank (not a sliding scale):
      - Ranks 1-8: 10% off
      - Rank 9+:   20% off

    If operator_rank <= 0: returns an "not_eligible" result. No roll is
    made, no dice are rolled, no state_ops are emitted. The model should
    not call haggle for a non-Fixer — if it does, this path fails soft
    with a narrative explanation instead of silently granting a discount.
    """
    rank = _to_int(operator_rank, 0)
    price = max(0, _to_int(item_price, 0))
    label = item_name or "item"

    # Non-Fixer gate: RAW-exclusive ability. Fail soft, no roll, no purchase.
    if rank <= 0:
        msg = (
            f"Haggle ({label}): NOT ELIGIBLE — reducing a listed market price "
            f"requires a Fixer's Operator Role Ability (rank 1+). Use a "
            f"skill_check with Trading for bartering, service negotiation, "
            f"or non-listed goods (RAW p.140)."
        )
        return {
            "buyer_die": None,
            "vendor_die": None,
            "buyer_total": 0,
            "vendor_total": 0,
            "success": False,
            "not_eligible": True,
            "operator_rank": rank,
            "discount_pct": 0,
            "original_price": price,
            "final_price": price,
            "savings": 0,
            "state_ops": [],
            "formatted": msg,
            "on_outcome": on_failure,
        }

    buyer_die = _roll_check_die()
    vendor_die = _roll_check_die()

    buyer_modifiers = []
    buyer_total = buyer_die["total"] + buyer_cool + buyer_trading + rank

    if seriously_wounded:
        buyer_total -= 2
        buyer_modifiers.append(("Wounded", -2))

    clamped_luck = max(0, _to_int(luck_spent, 0))
    if clamped_luck > 0:
        buyer_total += clamped_luck
        buyer_modifiers.append(("Luck", clamped_luck))

    vendor_total = vendor_die["total"] + vendor_cool + vendor_trading

    success = buyer_total > vendor_total

    discount_pct = _operator_discount_pct(rank) if success else 0
    if success:
        final_price = max(0, price - int(price * discount_pct / 100))
    else:
        final_price = price
    savings = price - final_price

    state_ops = [{
        "edgerunner": character,
        "op": "eurobucks",
        "change": -final_price,
        "reason": f"Purchased {item_name}" if item_name else "Purchase",
    }]

    parts = [f"Haggle ({label}):"]
    parts.append(
        f"Buyer {_format_die(buyer_die)} +COOL {buyer_cool} +Trading {buyer_trading} +Operator {rank}"
    )
    for mod_label, val in buyer_modifiers:
        parts.append(f"+{mod_label} {val}" if val > 0 else f"{mod_label} {val}")
    parts.append(f"= {buyer_total}")
    parts.append(
        f"vs Vendor {_format_die(vendor_die)} +COOL {vendor_cool} +Trading {vendor_trading} = {vendor_total}"
    )

    if success:
        parts.append(f"✓ — {discount_pct}% off (Operator rank {rank}). {price}eb → {final_price}eb (saved {savings}eb)")
    else:
        parts.append(f"✗ — no discount, paid {final_price}eb")

    formatted = " ".join(parts)
    on_outcome = on_success if success else on_failure

    return {
        "buyer_die": buyer_die,
        "vendor_die": vendor_die,
        "buyer_total": buyer_total,
        "vendor_total": vendor_total,
        "success": success,
        "not_eligible": False,
        "operator_rank": rank,
        "discount_pct": discount_pct,
        "original_price": price,
        "final_price": final_price,
        "savings": savings,
        "state_ops": state_ops,
        "formatted": formatted,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_facedown  — Reputation-based intimidation (CRB §11)
# ---------------------------------------------------------------------------

def resolve_facedown(
    initiator_cool: int,
    initiator_rep: int = 0,
    opponent_cool: int = 0,
    opponent_rep: int = 0,
    character: str = "",
    target: str = "",
    seriously_wounded_initiator: bool = False,
    seriously_wounded_opponent: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    on_success: str = "",
    on_failure: str = "",
) -> dict:
    """Resolve a Facedown (CRB p.195): COOL + Reputation + d10 vs same.

    RAW outcomes:
    - Tie: stalemate — both sides are unsure, nothing happens.
    - Winner/Loser: loser must back down OR take -2 to all actions vs the
      winner until they defeat the winner once.

    Returns *tie*, *winner*, *loser*, and *penalty_condition* fields so the
    caller (Events agent) can decide how the loser reacts.
    """
    initiator_die = _roll_check_die()
    opponent_die = _roll_check_die()

    initiator_modifiers = []
    initiator_total = initiator_die["total"] + initiator_cool + initiator_rep

    if seriously_wounded_initiator:
        initiator_total -= 2
        initiator_modifiers.append(("Wounded", -2))

    clamped_luck = max(0, _to_int(luck_spent, 0))
    if clamped_luck > 0:
        initiator_total += clamped_luck
        initiator_modifiers.append(("Luck", clamped_luck))

    clamped_rel = max(-5, min(5, _to_int(rel_bonus, 0)))
    if clamped_rel != 0:
        initiator_total += clamped_rel
        initiator_modifiers.append(("RS", clamped_rel))

    opponent_modifiers = []
    opponent_total = opponent_die["total"] + opponent_cool + opponent_rep

    if seriously_wounded_opponent:
        opponent_total -= 2
        opponent_modifiers.append(("Wounded", -2))

    margin = initiator_total - opponent_total
    is_tie = initiator_total == opponent_total

    # RAW: tie → stalemate (no winner/loser); otherwise higher total wins
    if is_tie:
        success = None
        winner = None
        loser = None
        penalty_condition = None
    else:
        success = initiator_total > opponent_total
        init_name_r = character or "Initiator"
        opp_name_r = target or "Opponent"
        winner = init_name_r if success else opp_name_r
        loser = opp_name_r if success else init_name_r
        penalty_condition = f"Facedown: -2 vs {winner}"

    # Format
    init_name = character or "Initiator"
    opp_name = target or "Opponent"

    parts = [f"Facedown:"]
    parts.append(f"{init_name} {_format_die(initiator_die)} +COOL {initiator_cool} +Rep {initiator_rep}")
    for label, val in initiator_modifiers:
        parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
    parts.append(f"= {initiator_total}")
    parts.append(f"vs {opp_name} {_format_die(opponent_die)} +COOL {opponent_cool} +Rep {opponent_rep}")
    for label, val in opponent_modifiers:
        parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
    parts.append(f"= {opponent_total}")

    if is_tie:
        parts.append("— Stalemate — both sides are unsure, nothing happens")
    elif success:
        parts.append(f"✓ — {opp_name} must back down or take -2 to all actions vs {init_name} until defeated")
    else:
        parts.append(f"✗ — {init_name} must back down or take -2 to all actions vs {opp_name} until defeated")

    formatted = " ".join(parts)
    on_outcome = (on_success if success else on_failure) if success is not None else ""

    return {
        "initiator_die": initiator_die,
        "opponent_die": opponent_die,
        "initiator_total": initiator_total,
        "opponent_total": opponent_total,
        "success": success,
        "tie": is_tie,
        "winner": winner,
        "loser": loser,
        "penalty_condition": penalty_condition,
        "margin": margin,
        "state_ops": [],
        "formatted": formatted,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_damage
# ---------------------------------------------------------------------------

def resolve_damage(
    damage_dice: int,
    hit_location: str,
    target_sp: int,
    is_melee: bool = False,
    is_brawling: bool = False,
    is_ap: bool = False,
    is_rubber: bool = False,
    aimed_shot: Optional[str] = None,
    target_hp_current: Optional[int] = None,
) -> dict:
    """Resolve damage: roll Nd6, check crits, apply SP, compute ablation.

    Returns dict with damage breakdown.
    """
    # Roll damage dice
    dice = [_roll_d6() for _ in range(damage_dice)]
    total_rolled = sum(dice)

    # Critical injury check: 2+ dice showing 6
    sixes = sum(1 for d in dice if d == 6)
    crit = sixes >= 2 and not is_rubber
    crit_bonus = 5 if crit else 0
    crit_injury = None

    critical_injuries = []
    if crit:
        crit_injury = _lookup_crit_injury(hit_location)
        critical_injuries.append(crit_injury)

    # Compute effective SP
    effective_sp = target_sp
    if is_melee and not is_brawling:
        effective_sp = -(-target_sp // 2)  # ceil(sp / 2) = halve, round up

    # Penetration check
    damage_past_sp = max(0, total_rolled - effective_sp)

    # Aimed shot head: 2x damage past SP
    aimed_effect = None
    if aimed_shot == "head" and damage_past_sp > 0:
        damage_past_sp *= 2
        aimed_effect = "head_2x"
    elif aimed_shot == "leg" and damage_past_sp > 0 and not is_rubber:
        aimed_effect = "broken_leg"
        broken_leg = {
            "roll": None,
            "total": None,
            "name": "Broken Leg",
            "effect": "Target suffers the Broken Leg critical injury.",
            "dv_mod": 0,
            "location": "body",
        }
        if not any(ci.get("name") == "Broken Leg" for ci in critical_injuries):
            critical_injuries.append(broken_leg)
        crit_injury = critical_injuries[0] if critical_injuries else broken_leg
    elif aimed_shot == "held_item" and damage_past_sp > 0:
        aimed_effect = "drop_item"

    # Total HP damage = damage past SP + crit bonus (crit bonus ignores SP)
    hp_damage = damage_past_sp + crit_bonus
    # Rubber ammo is non-lethal: do not reduce target below 1 HP when current HP is known.
    if is_rubber and isinstance(target_hp_current, int):
        hp_damage = min(hp_damage, max(0, target_hp_current - 1))

    # Ablation: only if armor was penetrated AND target takes HP damage (errata p.186)
    ablation = 0
    if damage_past_sp > 0 and not is_rubber:
        ablation = 2 if is_ap else 1

    # Build formatted damage string
    dice_str = ",".join(str(d) for d in dice)
    parts = [f"{damage_dice}d6[**{dice_str}**] = {total_rolled}"]
    if is_melee and not is_brawling:
        parts.append(f"→ SP {target_sp}÷2={effective_sp}")
    else:
        parts.append(f"→ SP {effective_sp}")

    if damage_past_sp > 0:
        parts.append(f"→ {damage_past_sp} past armor")
    else:
        parts.append("→ blocked by armor")

    if crit:
        parts.append(f"CRIT! +{crit_bonus} bonus")
    if aimed_effect:
        parts.append(f"(aimed: {aimed_effect})")
    if ablation > 0:
        parts.append(f"ablation -{ablation}")

    formatted = " ".join(parts)

    return {
        "dice": dice,
        "total_rolled": total_rolled,
        "effective_sp": effective_sp,
        "damage_past_sp": damage_past_sp,
        "crit": crit,
        "crit_bonus": crit_bonus,
        "crit_injury": crit_injury,
        "critical_injuries": critical_injuries,
        "hp_damage": hp_damage,
        "ablation": ablation,
        "aimed_effect": aimed_effect,
        "is_rubber": is_rubber,
        "formatted": formatted,
    }


# ---------------------------------------------------------------------------
# resolve_ranged_attack
# ---------------------------------------------------------------------------

def resolve_ranged_attack(
    stat_value: int,
    skill_value: int,
    weapon_type: str,
    damage_dice: int,
    rof: int,
    target_sp: int,
    range_bracket: int,
    hit_location: str = "body",
    is_ap: bool = False,
    is_rubber: bool = False,
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    aimed_shot: Optional[str] = None,
    target_hp_current: Optional[int] = None,
    target_name: str = "",
    character_name: str = "",
    weapon_name: str = "",
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a ranged attack (1+ shots based on ROF).

    range_bracket: index into RANGED_DV_TABLE (0=0-6m, 1=7-12m, ...)
    """
    # Look up DV from table
    dv_type = weapon_type
    dv_row = RANGED_DV_TABLE.get(dv_type)
    if not dv_row or range_bracket < 0 or range_bracket >= len(dv_row) or dv_row[range_bracket] is None:
        return {"error": f"Weapon '{weapon_type}' cannot fire at range bracket {range_bracket}"}

    base_dv = dv_row[range_bracket]
    dv = base_dv + (AIMED_SHOT_DV_PENALTY if aimed_shot else 0)

    attacks = []
    state_ops = []
    current_sp = target_sp
    current_hp = target_hp_current if isinstance(target_hp_current, int) else None

    for shot in range(rof):
        # Resolve attack roll
        roll = resolve_check(
            stat_value, skill_value, dv,
            seriously_wounded=seriously_wounded,
            luck_spent=luck_spent if shot == 0 else 0,  # Luck only on first shot
            rel_bonus=rel_bonus if shot == 0 else 0,
        )

        attack_result = {"roll": roll, "damage": None}

        if roll["success"]:
            # Resolve damage against current SP (ablation reduces for subsequent shots)
            damage = resolve_damage(
                damage_dice, hit_location, current_sp,
                is_ap=is_ap, is_rubber=is_rubber, aimed_shot=aimed_shot,
                target_hp_current=current_hp,
            )
            attack_result["damage"] = damage

            # Accumulate state ops
            if damage["hp_damage"] > 0:
                state_ops.append({
                    "edgerunner": target_name,
                    "op": "hp",
                    "change": -damage["hp_damage"],
                    "reason": f"{weapon_type} hit ({hit_location})",
                })
                if current_hp is not None:
                    hp_floor = 1 if is_rubber else 0
                    current_hp = max(hp_floor, current_hp - damage["hp_damage"])
            if damage["ablation"] > 0:
                state_ops.append({
                    "edgerunner": target_name,
                    "op": "armor",
                    "location": hit_location,
                    "change": -damage["ablation"],
                    "reason": "Ablation",
                })
                current_sp = max(0, current_sp - damage["ablation"])
            for injury in _iter_critical_injuries(damage):
                state_ops.append(_critical_injury_state_op(
                    edgerunner=target_name,
                    injury=injury,
                    reason=f"Critical injury from {weapon_type}",
                ))

        attacks.append(attack_result)

    any_hit = any(a["roll"]["success"] for a in attacks)
    on_outcome = on_hit if any_hit else on_miss

    # Ammo state op
    if weapon_name:
        state_ops.append({
            "edgerunner": character_name,
            "op": "ammo",
            "weapon_name": weapon_name,
            "rounds_consumed": rof,
            "reason": f"Fired {weapon_name}",
        })

    return {
        "type": "ranged_attack",
        "attacks": attacks,
        "dv": dv,
        "base_dv": base_dv,
        "state_ops": state_ops,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_melee_attack
# ---------------------------------------------------------------------------

def resolve_melee_attack(
    attacker_stat: int,
    attacker_skill: int,
    defender_stat: int,
    defender_skill: int,
    damage_dice: int,
    rof: int,
    target_sp: int,
    hit_location: str = "body",
    seriously_wounded_attacker: bool = False,
    seriously_wounded_defender: bool = False,
    is_brawling: bool = False,
    rel_bonus: int = 0,
    target_name: str = "",
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a melee attack: opposed roll (attacker vs defender Evasion)."""
    attacks = []
    state_ops = []
    current_sp = target_sp

    clamped_rel = max(-5, min(5, rel_bonus))

    for shot in range(rof):
        # Attacker roll — relationship bonus on first swing only
        atk_die = _roll_check_die()
        _shot_rel = clamped_rel if shot == 0 else 0
        atk_total = atk_die["total"] + attacker_stat + attacker_skill + _shot_rel
        if seriously_wounded_attacker:
            atk_total -= 2

        # Defender roll (Evasion)
        def_die = _roll_check_die()
        def_total = def_die["total"] + defender_stat + defender_skill
        if seriously_wounded_defender:
            def_total -= 2

        hit = atk_total > def_total  # attacker must beat defender

        _rel_part = f" +RS {_shot_rel}" if _shot_rel != 0 else ""
        atk_formatted = f"{_format_die(atk_die)} +DEX {attacker_stat} +Skill {attacker_skill}{_rel_part} = {atk_total}"
        def_formatted = f"{_format_die(def_die)} +DEX {defender_stat} +Evasion {defender_skill} = {def_total}"

        attack_result = {
            "attacker_roll": {"die": atk_die, "total": atk_total, "formatted": atk_formatted},
            "defender_roll": {"die": def_die, "total": def_total, "formatted": def_formatted},
            "hit": hit,
            "damage": None,
        }

        if hit:
            damage = resolve_damage(
                damage_dice, hit_location, current_sp,
                is_melee=True, is_brawling=is_brawling,
            )
            attack_result["damage"] = damage

            if damage["hp_damage"] > 0:
                state_ops.append({
                    "edgerunner": target_name,
                    "op": "hp",
                    "change": -damage["hp_damage"],
                    "reason": f"Melee hit ({hit_location})",
                })
            if damage["ablation"] > 0:
                state_ops.append({
                    "edgerunner": target_name,
                    "op": "armor",
                    "location": hit_location,
                    "change": -damage["ablation"],
                    "reason": "Ablation",
                })
                current_sp = max(0, current_sp - damage["ablation"])
            for injury in _iter_critical_injuries(damage):
                state_ops.append(_critical_injury_state_op(
                    edgerunner=target_name,
                    injury=injury,
                    reason="Critical injury from melee",
                ))

        attacks.append(attack_result)

    any_hit = any(a["hit"] for a in attacks)
    on_outcome = on_hit if any_hit else on_miss

    return {
        "type": "melee_attack",
        "attacks": attacks,
        "state_ops": state_ops,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_autofire
# ---------------------------------------------------------------------------

def resolve_autofire(
    stat_value: int,
    skill_value: int,
    weapon_type: str,
    autofire_multiplier: int,
    target_sp: int,
    range_bracket: int,
    hit_location: str = "body",
    is_ap: bool = False,
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    target_name: str = "",
    character_name: str = "",
    weapon_name: str = "",
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve an autofire attack.

    Damage on hit: 2d6 × margin of success, capped by autofire_multiplier.
    Consumes 10 rounds of ammo.
    """
    # Look up autofire DV
    dv_row = AUTOFIRE_DV_TABLE.get(weapon_type)
    if not dv_row or range_bracket < 0 or range_bracket >= len(dv_row):
        return {"error": f"Weapon '{weapon_type}' cannot autofire at range bracket {range_bracket}"}

    dv = dv_row[range_bracket]

    # Attack roll
    roll = resolve_check(
        stat_value, skill_value, dv,
        seriously_wounded=seriously_wounded,
        luck_spent=luck_spent,
        rel_bonus=rel_bonus,
    )

    state_ops = []
    damage_result = None

    if roll["success"]:
        margin = roll["total"] - dv
        # Roll 2d6 for damage multiplier
        d1 = _roll_d6()
        d2 = _roll_d6()
        raw_damage = (d1 + d2) * margin
        capped_damage = min(raw_damage, autofire_multiplier * (d1 + d2))
        crit = d1 == 6 and d2 == 6
        crit_bonus = 5 if crit else 0
        crit_injury = _lookup_crit_injury(hit_location) if crit else None

        # Apply against SP
        effective_sp = target_sp
        damage_past_sp = max(0, capped_damage - effective_sp)
        hp_damage = damage_past_sp + crit_bonus
        ablation = (2 if is_ap else 1) if damage_past_sp > 0 else 0

        damage_result = {
            "margin": margin,
            "damage_dice": [d1, d2],
            "raw_damage": raw_damage,
            "capped_damage": capped_damage,
            "effective_sp": effective_sp,
            "damage_past_sp": damage_past_sp,
            "hp_damage": hp_damage,
            "crit": crit,
            "crit_bonus": crit_bonus,
            "crit_injury": crit_injury,
            "critical_injuries": [crit_injury] if crit_injury else [],
            "ablation": ablation,
            "formatted": f"2d6[**{d1},{d2}**] × margin {margin} = {raw_damage}"
                         f"{f' (capped to {capped_damage})' if capped_damage < raw_damage else ''}"
                         f" → SP {effective_sp} → {damage_past_sp} HP damage"
                         f"{' +5 crit bonus' if crit else ''}",
        }

        if hp_damage > 0:
            state_ops.append({
                "edgerunner": target_name,
                "op": "hp",
                "change": -hp_damage,
                "reason": f"Autofire ({weapon_type})",
            })
        if ablation > 0:
            state_ops.append({
                "edgerunner": target_name,
                "op": "armor",
                "location": hit_location,
                "change": -ablation,
                "reason": "Ablation (autofire)",
            })
        if crit_injury:
            state_ops.append(_critical_injury_state_op(
                edgerunner=target_name,
                injury=crit_injury,
                reason=f"Critical injury from autofire ({weapon_type})",
            ))

    on_outcome = on_hit if roll["success"] else on_miss

    # Ammo state op
    if weapon_name:
        state_ops.append({
            "edgerunner": character_name,
            "op": "ammo",
            "weapon_name": weapon_name,
            "rounds_consumed": 10,
            "reason": f"Autofire {weapon_name}",
        })

    return {
        "type": "autofire",
        "roll": roll,
        "dv": dv,
        "hit": roll["success"],
        "damage": damage_result,
        "rounds_consumed": 10,
        "state_ops": state_ops,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_suppressive_fire  — Area denial (CRB p.174)
# ---------------------------------------------------------------------------

def resolve_suppressive_fire(
    attacker_ref: int,
    attacker_autofire: int,
    targets: list,
    seriously_wounded_attacker: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    character_name: str = "",
    weapon_name: str = "",
    tracked_edgerunners=None,
    on_success: str = "",
    on_failure: str = "",
) -> dict:
    """Resolve Suppressive Fire (CRB p.174): area denial via autofire.

    Attacker rolls d10 + REF + Autofire once. Each target rolls
    d10 + WILL + Concentration. Targets who fail (attacker > defender)
    are suppressed. Ties favor defender. Consumes 10 rounds. No damage.
    """
    attacker_die = _roll_check_die()
    attacker_total = attacker_die["total"] + _to_int(attacker_ref, 0) + _to_int(attacker_autofire, 0)
    attacker_mods = []

    if seriously_wounded_attacker:
        attacker_total -= 2
        attacker_mods.append(("Wounded", -2))

    clamped_luck = max(0, _to_int(luck_spent, 0))
    if clamped_luck > 0:
        attacker_total += clamped_luck
        attacker_mods.append(("Luck", clamped_luck))

    if rel_bonus:
        attacker_total += rel_bonus
        attacker_mods.append(("Rel", rel_bonus))

    # --- Per-target resolution ---
    target_results = []
    atk_name = character_name or "Attacker"

    fmt_parts = [f"Suppressive Fire: {atk_name} {_format_die(attacker_die)}"
                 f"+REF {attacker_ref}+Autofire {attacker_autofire}"]
    for label, val in attacker_mods:
        fmt_parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
    fmt_parts.append(f"= {attacker_total} |")

    for tgt in (targets or []):
        if isinstance(tgt, dict):
            tgt_data = tgt
            tgt_name = tgt_data.get("name", "Target")
        else:
            tgt_name = tgt if isinstance(tgt, str) and tgt else "Target"
            tgt_data = {"name": tgt_name}
        tgt_will = _to_int(tgt_data.get("will", 0), 0)
        tgt_conc = _to_int(tgt_data.get("concentration", 0), 0)
        tgt_wounded = _as_bool(tgt_data.get("seriously_wounded", False), False)

        def_die = _roll_check_die()
        def_total = def_die["total"] + tgt_will + tgt_conc
        def_mods = []
        if tgt_wounded:
            def_total -= 2
            def_mods.append(("Wounded", -2))

        # Ties favor defender (not suppressed)
        suppressed = attacker_total > def_total

        target_results.append({
            "name": tgt_name,
            "defender_die": def_die,
            "defender_total": def_total,
            "suppressed": suppressed,
        })

        tgt_fmt = f"{tgt_name} {_format_die(def_die)}+WILL {tgt_will}+Conc {tgt_conc}"
        for label, val in def_mods:
            tgt_fmt += f" {label} {val}" if val < 0 else f" +{label} {val}"
        tgt_fmt += f" = {def_total}"
        tgt_fmt += " ✗ SUPPRESSED" if suppressed else " ✓ resists"
        fmt_parts.append(tgt_fmt + " |")

    formatted = " ".join(fmt_parts).rstrip(" |")

    any_suppressed = any(t["suppressed"] for t in target_results) if target_results else False

    # Ammo state op
    state_ops = []
    tracked_names = {
        normalize_cpred_name(tracked_name)
        for tracked_name in (tracked_edgerunners or [])
        if normalize_cpred_name(tracked_name)
    } if isinstance(tracked_edgerunners, (list, tuple, set)) else set()
    attacker_name = normalize_cpred_name(character_name)
    if weapon_name and (not tracked_names or attacker_name in tracked_names):
        state_ops.append(attach_state_op_subject({
            "op": "ammo",
            "weapon_name": weapon_name,
            "rounds_consumed": 10,
            "reason": f"Suppressive Fire {weapon_name}",
        }, "edgerunner", character_name))
    for target_result in target_results:
        if target_result.get("suppressed") and target_result.get("name"):
            target_name = str(target_result["name"]).strip()
            if not target_name:
                continue
            condition_op = {
                "op": "add_condition",
                "condition": "suppressed",
                "reason": f"Suppressive Fire by {atk_name}",
            }
            if normalize_cpred_name(target_name) in tracked_names:
                condition_op = attach_state_op_subject(condition_op, "edgerunner", target_name)
            else:
                condition_op = attach_state_op_subject(condition_op, "character", target_name)
            state_ops.append(condition_op)

    on_outcome = on_success if any_suppressed else on_failure

    return {
        "type": "suppressive_fire",
        "success": any_suppressed,
        "attacker_total": attacker_total,
        "attacker_die": attacker_die,
        "targets": target_results,
        "any_suppressed": any_suppressed,
        "rounds_consumed": 10,
        "state_ops": state_ops,
        "formatted": formatted,
        "on_outcome": on_outcome,
    }


# ---------------------------------------------------------------------------
# resolve_death_save
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# resolve_opposed_check
# ---------------------------------------------------------------------------

def resolve_opposed_check(
    attacker_stat: int,
    defender_stat: int,
    attacker_label: str = "Attacker",
    defender_label: str = "Defender",
    attacker_skill: int = 0,
    defender_skill: int = 0,
    attacker_skill_label: str = "",
    defender_skill_label: str = "",
    seriously_wounded_attacker: bool = False,
    seriously_wounded_defender: bool = False,
    luck_spent: int = 0,
    rel_bonus: int = 0,
    wb_boost: int = 0,
) -> dict:
    """Resolve an opposed check: both sides roll d10 + stat (+ skill).

    Handles exploding 10s and fumble 1s. Ties go to defender.
    Supports full stat+skill opposed rolls (Stealth vs Perception, Persuasion
    vs Concentration, etc.) as well as stat-only NET checks (Zap vs DEF).

    Returns dict with full breakdown and formatted string.
    """
    atk_die = _roll_check_die()
    def_die = _roll_check_die()

    # --- Attacker total ---
    atk_total = atk_die["total"] + attacker_stat + attacker_skill
    atk_mods = []
    if seriously_wounded_attacker:
        atk_total -= 2
        atk_mods.append(("Wounded", -2))
    clamped_luck = max(0, luck_spent)
    if clamped_luck > 0:
        atk_total += clamped_luck
        atk_mods.append(("Luck", clamped_luck))
    clamped_rel = max(-5, min(5, rel_bonus + (1 if wb_boost else 0)))
    if clamped_rel != 0:
        atk_total += clamped_rel
        label = "RS+WB" if wb_boost else "RS"
        atk_mods.append((label, clamped_rel))

    # --- Defender total ---
    def_total = def_die["total"] + defender_stat + defender_skill
    def_mods = []
    if seriously_wounded_defender:
        def_total -= 2
        def_mods.append(("Wounded", -2))

    success = atk_total > def_total  # ties go to defender
    margin = atk_total - def_total

    # --- Formatted strings ---
    def _fmt_side(die, stat, stat_label, skill, skill_label, mods, total):
        parts = [_format_die(die)]
        parts.append(f"+{stat_label} {stat}")
        if skill or skill_label:
            parts.append(f"+{skill_label or 'Skill'} {skill}")
        for label, val in mods:
            parts.append(f"+{label} {val}" if val > 0 else f"{label} {val}")
        parts.append(f"= {total}")
        return " ".join(parts)

    atk_fmt = _fmt_side(atk_die, attacker_stat, attacker_label, attacker_skill,
                        attacker_skill_label, atk_mods, atk_total)
    def_fmt = _fmt_side(def_die, defender_stat, defender_label, defender_skill,
                        defender_skill_label, def_mods, def_total)
    formatted = f"{atk_fmt} vs {def_fmt} {'✓' if success else '✗'}"

    return {
        "type": "opposed_check",
        "attacker_roll": {"die": atk_die, "total": atk_total, "formatted": atk_fmt},
        "defender_roll": {"die": def_die, "total": def_total, "formatted": def_fmt},
        "attacker_total": atk_total,
        "defender_total": def_total,
        "success": success,
        "margin": margin,
        "formatted": formatted,
    }


# ---------------------------------------------------------------------------
# resolve_program_attack
# ---------------------------------------------------------------------------

def resolve_program_attack(
    interface_rank: int,
    program_atk: int,
    target_def: int,
    program_damage_dice: int,
    target_rez: int,
    program_name: str = "Program",
    target_name: str = "ICE",
    track_rez: bool = True,
    damage_kind: str = "REZ damage",
) -> dict:
    """Resolve a program attack: opposed check then damage if hit.

    Attack: Interface + Program ATK + d10 vs ICE DEF + d10
    Damage: Nd6 damage on hit.
    Returns full breakdown with hit/miss, damage, and rez tracking.
    """
    # Opposed check: attacker_stat = interface + program_atk, defender_stat = target_def
    combined_atk = interface_rank + program_atk
    roll_result = resolve_opposed_check(
        attacker_stat=combined_atk,
        defender_stat=target_def,
        attacker_label=f"Interface+{program_name}",
        defender_label=f"{target_name} DEF",
    )

    hit = roll_result["success"]
    damage_dice = []
    damage_total = 0
    rez_remaining = target_rez if track_rez else None
    derezzed = False

    if hit:
        damage_dice = [_roll_d6() for _ in range(program_damage_dice)]
        damage_total = sum(damage_dice)
        if track_rez:
            rez_remaining = max(0, target_rez - damage_total)
            derezzed = rez_remaining <= 0

    dice_str = ",".join(str(d) for d in damage_dice) if damage_dice else ""
    damage_fmt = ""
    if hit:
        damage_fmt = f" → {program_damage_dice}d6[**{dice_str}**] = {damage_total} {damage_kind}"
        if track_rez:
            if derezzed:
                damage_fmt += f" → {target_name} DEREZZED"
            else:
                damage_fmt += f" → {target_name} REZ {rez_remaining}/{target_rez}"

    formatted = roll_result["formatted"] + damage_fmt

    return {
        "type": "program_attack",
        "roll_result": roll_result,
        "hit": hit,
        "damage_dice": damage_dice,
        "damage_total": damage_total,
        "rez_remaining": rez_remaining,
        "derezzed": derezzed,
        "formatted": formatted,
    }


def resolve_death_save(
    body_stat: int,
    death_save_count: int,
    active_injuries: Optional[list] = None,
    roms_death_save_bonus: int = 0,
) -> dict:
    """Resolve a Death Save: d10 + count + injury mods - RomS bonus vs BODY.

    Natural 10 always fails. Must roll UNDER BODY to survive.
    roms_death_save_bonus: T2+ RomS grants -1 to the roll (max 1).
    """
    d10 = _roll_d10()
    injury_mod = 0
    for ci in (active_injuries or []):
        mod = ci.get("dv_mod", 0) if isinstance(ci, dict) else 0
        injury_mod += mod if isinstance(mod, (int, float)) else 0
    roms_mod = min(1, max(0, roms_death_save_bonus))
    effective_roll = d10 + death_save_count + injury_mod - roms_mod
    natural_10 = d10 == 10
    survived = not natural_10 and effective_roll < body_stat

    # Build formatted string
    parts = [f"d10[**{d10}**]"]
    if death_save_count > 0:
        parts.append(f"+cumulative {death_save_count}")
    if injury_mod > 0:
        parts.append(f"+injuries {injury_mod}")
    if roms_mod > 0:
        parts.append(f"−RomS {roms_mod}")
    parts.append(f"= {effective_roll} vs BODY {body_stat}")
    if natural_10:
        parts.append("→ NATURAL 10 — AUTO FAIL")
    elif survived:
        parts.append("→ SURVIVED")
    else:
        parts.append("→ FAILED")

    formatted = " ".join(parts)

    return {
        "type": "death_save",
        "d10": d10,
        "death_save_count": death_save_count,
        "injury_mod": injury_mod,
        "roms_death_save_bonus": roms_mod,
        "effective_roll": effective_roll,
        "threshold": body_stat,
        "natural_10": natural_10,
        "survived": survived,
        "formatted": formatted,
    }


# ---------------------------------------------------------------------------
# resolve_initiative
# ---------------------------------------------------------------------------

def resolve_initiative(combatants: list, surprised: list = None) -> list:
    """Resolve initiative for a list of combatants.

    Each combatant: {"name": str, "ref": int}
    surprised: optional list of combatant names who are surprised (skip round 1)
    Returns sorted list: [{"name", "ref", "d10", "total", "surprised"?}]
    """
    surprised_set = set(surprised or [])
    results = []
    for c in combatants:
        d10 = _roll_d10()
        name = c.get("name", "Unknown")
        total = d10 + c.get("ref", 0)
        entry = {
            "name": name,
            "ref": c.get("ref", 0),
            "d10": d10,
            "total": total,
        }
        if name in surprised_set:
            entry["surprised"] = True
        results.append(entry)

    # Sort descending by total, then tiebreak with additional d10s
    results.sort(key=lambda x: (-x["total"], -x["d10"]))

    # Resolve remaining ties
    i = 0
    while i < len(results) - 1:
        if results[i]["total"] == results[i + 1]["total"]:
            # Find all tied entries
            tie_val = results[i]["total"]
            tie_start = i
            while i < len(results) and results[i]["total"] == tie_val:
                i += 1
            # Roll tiebreakers
            tied = results[tie_start:i]
            for t in tied:
                t["tiebreak"] = _roll_d10()
            tied.sort(key=lambda x: -x["tiebreak"])
            results[tie_start:i] = tied
        else:
            i += 1

    return results


# ---------------------------------------------------------------------------
# next_combat_turn
# ---------------------------------------------------------------------------

def next_combat_turn(
    initiative_order: list,
    current_turn: int,
    eliminated: Optional[list] = None,
) -> dict:
    """Advance to the next non-eliminated combatant.

    initiative_order: list of names in initiative order
    current_turn: index of current combatant (0-based)
    eliminated: list of eliminated combatant names
    """
    eliminated_set = set(eliminated or [])
    n = len(initiative_order)
    if n == 0:
        return {"next_turn": 0, "round_incremented": False, "new_round": True}

    idx = (current_turn + 1) % n
    round_incremented = idx <= current_turn  # wrapped around
    attempts = 0

    while attempts < n:
        name = initiative_order[idx] if isinstance(initiative_order[idx], str) else initiative_order[idx].get("name", "")
        if name not in eliminated_set:
            return {
                "next_turn": idx,
                "round_incremented": round_incremented or (attempts > 0 and idx <= current_turn),
                "new_round": round_incremented,
            }
        idx = (idx + 1) % n
        if idx == 0:
            round_incremented = True
        attempts += 1

    # All eliminated
    return {"next_turn": current_turn, "round_incremented": False, "new_round": False}


# ---------------------------------------------------------------------------
# ICE type lookup and effect resolution
# ---------------------------------------------------------------------------

def _lookup_ice_type(ice_type_raw):
    """Normalize model string to ICE_STAT_BLOCKS key. Returns block or None."""
    if not ice_type_raw:
        return None
    key = str(ice_type_raw).strip().lower()
    return ICE_STAT_BLOCKS.get(key)


def _lookup_program_stats(program_name):
    """Normalize model string to PROGRAM_STATS entry. Returns block or None.

    Case-insensitive name match; tolerates "see ya" / "seeya" / "See Ya" variants.
    """
    if not program_name:
        return None
    needle = str(program_name).strip().lower().replace(" ", "")
    for canonical_name, block in PROGRAM_STATS.items():
        if canonical_name.lower().replace(" ", "") == needle:
            return block
    return None


def _resolve_ice_target_block(target, ice_status):
    """Look up an ice_status entry by key or by name.

    Accepts:
      - Exact key match (e.g. "Server Farm_Dragon")
      - Single name match (e.g. "Dragon" matches if exactly one ICE has name="Dragon")

    Returns the ice_status entry dict or None.
    """
    if not target or not isinstance(ice_status, dict):
        return None
    if isinstance(ice_status.get(target), dict):
        return ice_status[target]
    target_lower = str(target).strip().lower()
    matches = [
        v for v in ice_status.values()
        if isinstance(v, dict) and str(v.get("name", "")).strip().lower() == target_lower
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _hydrate_program_attack_from_state(action, ice_status):
    """Auto-derive mechanical stats for a program_attack action from state.

    Mutates `action` in place. Only fills fields the model didn't supply
    (backward compat: explicit model values always win).

    Derives:
      - program_atk from PROGRAM_STATS lookup of program_name / program
      - target_def from ICE_STAT_BLOCKS via ice_status[target].ice_type
      - target_rez from ice_status[target].rez_current

    Does NOT derive: interface_rank (lives on hack_state), program_damage_dice
    (asymmetric for Sword/Banhammer — Step 5 handles via on_program_damage_dice_select).
    """
    program_name = action.get("program_name") or action.get("program") or ""
    if program_name and "program_atk" not in action:
        prog_stats = _lookup_program_stats(program_name)
        if prog_stats is not None:
            action["program_atk"] = prog_stats.get("atk", 0)

    if "target_def" in action and "target_rez" in action:
        return

    target_block = _resolve_ice_target_block(action.get("target", ""), ice_status)
    if target_block is None:
        return

    if "target_def" not in action:
        ice_type = target_block.get("ice_type")
        if ice_type:
            ice_block = ICE_STAT_BLOCKS.get(str(ice_type).strip().lower())
            if ice_block is not None:
                action["target_def"] = ice_block.get("def", 0)

    if "target_rez" not in action:
        rez_cur = target_block.get("rez_current")
        if rez_cur is not None:
            action["target_rez"] = rez_cur


def _normalize_ice_key_candidate(raw_key, ice_status):
    """Return a valid ICE status key or None."""
    if not raw_key or not isinstance(ice_status, dict):
        return None
    if isinstance(ice_status.get(raw_key), dict):
        return raw_key
    return None


def resolve_ice_effect(ice_block, active_programs=None, installed_hardware=None,
                       ice_status=None, exclude_ice=None, exclude_ice_key=None,
                       source_ice_key=None, _depth=0):
    """Resolve the special effect of a Black ICE hit.

    Returns {effect, state_ops, formatted, annotations}.
    """
    effect = ice_block.get("effect", "")
    name = ice_block.get("name", "ICE")
    state_ops = []
    annotations = []
    formatted_parts = []

    _hw = installed_hardware if isinstance(installed_hardware, list) else []
    hw_list = [str(h).lower() for h in _hw]

    if effect in ("program_destroy", "program_derez"):
        is_derez = effect == "program_derez"
        targets_defender = ice_block.get("targets_defender", False)
        candidates = []
        if isinstance(active_programs, list):
            for p in active_programs:
                if isinstance(p, dict) and p.get("status") == "active":
                    if targets_defender and p.get("category") != "defender":
                        continue
                    candidates.append(p.get("name", "Unknown"))
        if candidates:
            chosen = random.choice(candidates)
            op_name = "program_derez" if is_derez else "program_destroy"
            state_op = {"op": op_name, "program_name": chosen, "source": name}
            if is_derez:
                state_op["status"] = "derezzed"
                state_op["reactivate_net_actions"] = 2
            state_ops.append(state_op)
            cat_note = " (Defender)" if targets_defender else ""
            verb = "derezzes" if is_derez else "destroys"
            formatted_parts.append(f"{name} {verb} {chosen}{cat_note}!")
        else:
            cat_note = " Defender" if targets_defender else ""
            verb = "derez" if is_derez else "destroy"
            formatted_parts.append(f"{name} finds no{cat_note} programs to {verb}.")
            annotations.append("no_targets")

    elif effect == "body_fire":
        if any("insulated wiring" in h for h in hw_list):
            formatted_parts.append(f"{name} fire blocked by Insulated Wiring!")
            annotations.append("blocked_by_hardware")
        else:
            state_ops.append({"op": "body_fire", "active": True,
                              "damage_per_turn": 2, "source": name})
            formatted_parts.append(f"{name} sets clothes on fire! 2 meat HP/turn until extinguished.")

    elif effect == "movement_lock":
        _lock_op = {"op": "movement_lock", "locked_by": name}
        if source_ice_key:
            _lock_op["locked_by_key"] = source_ice_key
        state_ops.append(_lock_op)
        formatted_parts.append(f"{name} locks movement — cannot move between nodes until derezzed!")

    elif effect == "stat_debuff":
        debuff_stats = ice_block.get("debuff_stats", [])
        if not isinstance(debuff_stats, list):
            debuff_stats = []
        amount = _roll_d6()
        state_ops.append({"op": "stat_debuff", "stats": debuff_stats,
                          "amount": amount, "source": name,
                          "duration": "1 hour"})
        formatted_parts.append(f"{name} debuffs {'/'.join(str(s) for s in debuff_stats)} by {amount} (1d6={amount}) for 1 hour!")

    elif effect == "slide_penalty":
        state_ops.append({"op": "slide_penalty", "penalty": -2, "source": name})
        formatted_parts.append(f"{name} imposes -2 to all Slide checks until derezzed!")

    elif effect == "net_action_penalty":
        state_ops.append({"op": "net_action_penalty", "penalty": 1, "source": name})
        formatted_parts.append(f"{name} steals 1 NET Action next turn!")

    elif effect == "forced_jack_out":
        # Check KRASH Barrier
        if any("krash barrier" in h for h in hw_list):
            formatted_parts.append(f"{name} forced Jack Out blocked by KRASH Barrier!")
            annotations.append("blocked_by_hardware")
        else:
            # Cascade: resolve all rezzed Black ICE effects
            cascade = _resolve_jack_out_cascade(
                ice_status or {}, active_programs, installed_hardware,
                exclude_ice=exclude_ice or name, exclude_ice_key=exclude_ice_key, _depth=_depth
            )
            state_ops.extend(cascade.get("state_ops", []))
            state_ops.append({"op": "forced_jack_out",
                              "cascade_results": cascade.get("cascade_results", []),
                              "source": name})
            formatted_parts.append(f"{name} forces unsafe Jack Out!")
            if cascade.get("formatted"):
                formatted_parts.append(f"Cascade: {cascade['formatted']}")

    elif effect == "program_rez_damage":
        # Handled in ice_attack_vs_program action type, not here
        pass

    return {
        "effect": effect,
        "state_ops": state_ops,
        "formatted": " ".join(formatted_parts),
        "annotations": annotations,
    }


def _resolve_jack_out_cascade(ice_status, active_programs=None, installed_hardware=None,
                               exclude_ice=None, exclude_ice_key=None, _depth=0):
    """Iterate rezzed Black ICE and resolve each effect for forced Jack Out cascade.

    Only processes ICE with status "active" AND behavior "black" (bypassed ICE never activated).
    Excludes the attacking Giant to prevent infinite recursion.
    """
    if _depth > 2:  # Safety: prevent deep recursion
        return {"state_ops": [], "formatted": "", "cascade_results": []}

    all_ops = []
    cascade_results = []
    formatted_parts = []

    if not isinstance(ice_status, dict):
        return {"state_ops": [], "formatted": "", "cascade_results": []}

    excluded_name_consumed = False
    # Iterate keys in sorted order so fallback exclusion-by-name is deterministic
    # even when input dict insertion order differs.
    for _key in sorted(ice_status.keys()):
        _ice = ice_status.get(_key)
        if not isinstance(_ice, dict):
            continue
        if _ice.get("status") != "active":
            continue
        if str(_ice.get("behavior", "")).lower() != "black":
            continue
        ice_name = _ice.get("name", "")
        # Prefer exact instance exclusion by key. Fallback to excluding a single
        # matching name to avoid dropping all duplicate ICE of the same type.
        if exclude_ice_key and _key == exclude_ice_key:
            continue
        if exclude_ice and ice_name == exclude_ice and not exclude_ice_key and not excluded_name_consumed:
            excluded_name_consumed = True
            continue

        block = _lookup_ice_type(ice_name)
        if not block:
            continue

        # Roll brain damage if applicable
        dmg_dice = block.get("damage_dice", 0)
        bd_total = 0
        if dmg_dice > 0:
            bd_total = sum(_roll_d6() for _ in range(dmg_dice))
            all_ops.append({"op": "brain_damage", "edgerunner": "",  # filled by caller
                            "change": bd_total,
                            "reason": f"Cascade: {ice_name} ({dmg_dice}d6)"})
            formatted_parts.append(f"{ice_name} {dmg_dice}d6={bd_total} brain dmg")

        # Resolve special effect (skip nested Giant to prevent infinite loop)
        if block.get("effect") != "forced_jack_out":
            fx = resolve_ice_effect(block, active_programs, installed_hardware,
                                    ice_status, exclude_ice=exclude_ice,
                                    exclude_ice_key=exclude_ice_key,
                                    source_ice_key=_key,
                                    _depth=_depth + 1)
            all_ops.extend(fx.get("state_ops", []))
            if fx.get("formatted"):
                formatted_parts.append(fx["formatted"])

        cascade_results.append({
            "ice_name": ice_name,
            "brain_damage": bd_total,
            "effect": block.get("effect", ""),
        })

    return {
        "state_ops": all_ops,
        "formatted": " | ".join(formatted_parts),
        "cascade_results": cascade_results,
    }


# ---------------------------------------------------------------------------
# Vehicle combat resolvers
# ---------------------------------------------------------------------------

def resolve_driving_check(
    stat_value: int,
    skill_value: int,
    maneuver: str = "maintain_control",
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a driving/piloting check against the maneuver DV.

    Uses the standard resolve_check flow.
    """
    stat_value = _to_int(stat_value, 0)
    skill_value = _to_int(skill_value, 0)
    luck_spent = _to_int(luck_spent, 0, minimum=0)
    seriously_wounded = _as_bool(seriously_wounded, False)
    maneuver = str(maneuver or "").strip().lower()
    if maneuver not in DRIVING_CHECK_DVS:
        return {
            "die": None,
            "stat_value": stat_value,
            "skill_value": skill_value,
            "modifiers": [],
            "total": None,
            "dv": None,
            "success": False,
            "type": "driving_check",
            "maneuver": maneuver,
            "control_lost": True,
            "state_ops": [],
            "on_outcome": on_miss,
            "error": f"Unknown driving maneuver: {maneuver}",
            "formatted": f"[Driving Check] Unknown maneuver '{maneuver}'",
        }
    dv = DRIVING_CHECK_DVS[maneuver]
    result = resolve_check(
        stat_value=stat_value,
        skill_value=skill_value,
        dv=dv,
        seriously_wounded=seriously_wounded,
        luck_spent=luck_spent,
    )
    result["type"] = "driving_check"
    result["maneuver"] = maneuver
    result["control_lost"] = not result["success"]
    result["state_ops"] = []
    result["on_outcome"] = on_hit if result["success"] else on_miss
    label = maneuver.replace("_", " ").title()
    result["formatted"] = f"[{label}] {result['formatted']}"
    return result


def resolve_ramming(
    vehicle_name: str,
    target_name: str,
    vehicle_sdp_current: int,
    vehicle_sp: int,
    target_hp_current: Optional[int],
    target_sp: int,
    target_is_vehicle: bool = False,
    target_sdp_current: Optional[int] = None,
    occupants: list = None,
    target_occupants: list = None,
    pedestrian_dodge: bool = False,
    pedestrian_dex: int = 0,
    pedestrian_evasion: int = 0,
    seriously_wounded_pedestrian: bool = False,
    combat_plow: bool = False,
    nos_boosted: bool = False,
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a ramming action (vehicle vs vehicle or vehicle vs pedestrian).

    6d6 base damage (8d6 with Combat Plow + NOS). Same damage applied to both sides
    unless Combat Plow negates attacker damage. Whiplash critical injury for occupants.
    """
    occupants = occupants or []
    target_occupants = target_occupants or []
    state_ops = []
    target_is_vehicle = _as_bool(target_is_vehicle, False)
    pedestrian_dodge = _as_bool(pedestrian_dodge, False)
    seriously_wounded_pedestrian = _as_bool(seriously_wounded_pedestrian, False)
    pedestrian_dex = _to_int(pedestrian_dex, 0)
    pedestrian_evasion = _to_int(pedestrian_evasion, 0)
    combat_plow = _as_bool(combat_plow, False)
    nos_boosted = _as_bool(nos_boosted, False)
    target_name = str(target_name or "").strip()
    vehicle_name = str(vehicle_name or "").strip()
    try:
        vehicle_sp = max(0, int(vehicle_sp))
    except (TypeError, ValueError, OverflowError):
        vehicle_sp = 0
    try:
        vehicle_sdp_current = max(0, int(vehicle_sdp_current))
    except (TypeError, ValueError, OverflowError):
        vehicle_sdp_current = None
    try:
        target_sp = max(0, int(target_sp))
    except (TypeError, ValueError, OverflowError):
        target_sp = 0
    target_sdp_current = _to_int(target_sdp_current, None) if target_sdp_current is not None else None
    target_hp_current = _to_int(target_hp_current, None) if target_hp_current is not None else None

    if not vehicle_name:
        return {
            "type": "ramming",
            "dodged": False,
            "state_ops": [],
            "error": "Ramming requires vehicle_name",
            "formatted": "Ramming: missing vehicle_name",
            "on_outcome": on_miss,
        }
    if not target_name:
        return {
            "type": "ramming",
            "dodged": False,
            "state_ops": [],
            "error": "Ramming requires target name",
            "formatted": "Ramming: missing target",
            "on_outcome": on_miss,
        }
    if isinstance(vehicle_sdp_current, int) and vehicle_sdp_current <= 0:
        _note = f"Action skipped: {vehicle_name} is already destroyed."
        return {
            "type": "ramming",
            "skipped": True,
            "reason": "vehicle_destroyed",
            "vehicle_name": vehicle_name,
            "target": target_name,
            "state_ops": [],
            "formatted": _note,
            "notification": _note,
            "on_outcome": on_miss,
        }

    # Pedestrian dodge attempt
    dodge_result = None
    if pedestrian_dodge and not target_is_vehicle:
        dodge_result = resolve_check(
            stat_value=pedestrian_dex,
            skill_value=pedestrian_evasion,
            dv=PEDESTRIAN_DODGE_DV,
            seriously_wounded=seriously_wounded_pedestrian,
        )
        if dodge_result["success"]:
            return {
                "type": "ramming",
                "dodge_result": dodge_result,
                "dodged": True,
                "combat_plow": combat_plow,
                "nos_boosted": nos_boosted,
                "ram_damage_dice": 0,
                "ram_damage_total": 0,
                "vehicle_damage": 0,
                "target_damage": 0,
                "vehicle_stopped": False,
                "target_ablation": 0,
                "vehicle_ablation": 0,
                "state_ops": [],
                "formatted": f"Ram {target_name} — dodge {dodge_result['formatted']} — DODGED!",
                "on_outcome": on_miss,
            }

    # Determine ram dice
    ram_dice = RAMMING_DAMAGE_DICE
    if combat_plow and nos_boosted:
        ram_dice = RAMMING_DAMAGE_DICE + RAMMING_NOS_BONUS_DICE

    # Roll ram damage
    dice = [_roll_d6() for _ in range(ram_dice)]
    ram_damage_total = sum(dice)
    dice_str = ",".join(str(d) for d in dice)

    # --- Apply damage to target ---
    target_effective_sp = target_sp
    target_damage_past_sp = max(0, ram_damage_total - target_effective_sp)
    target_ablation = 1 if target_damage_past_sp > 0 else 0

    if target_is_vehicle:
        # Damage to target vehicle SDP
        if target_damage_past_sp > 0:
            state_ops.append({
                "op": "vehicle_sdp",
                "vehicle": target_name,
                "change": -target_damage_past_sp,
                "reason": f"Rammed by {vehicle_name}",
            })
        if target_ablation > 0:
            state_ops.append({
                "op": "vehicle_sp",
                "vehicle": target_name,
                "change": -target_ablation,
                "reason": f"Ramming ablation from {vehicle_name}",
            })
    else:
        # Damage to pedestrian HP
        if target_damage_past_sp > 0:
            state_ops.append({
                "op": "hp",
                "edgerunner": target_name,
                "change": -target_damage_past_sp,
                "reason": f"Rammed by {vehicle_name}",
            })
        if target_ablation > 0:
            state_ops.append({
                "op": "armor",
                "edgerunner": target_name,
                "location": "body",
                "change": -target_ablation,
                "reason": f"Ramming ablation from {vehicle_name}",
            })

    # --- Apply damage to ramming vehicle (negated by Combat Plow) ---
    vehicle_damage_past_sp = 0
    vehicle_ablation = 0
    if not combat_plow:
        vehicle_damage_past_sp = max(0, ram_damage_total - vehicle_sp)
        vehicle_ablation = 1 if vehicle_damage_past_sp > 0 else 0
        if vehicle_damage_past_sp > 0:
            state_ops.append({
                "op": "vehicle_sdp",
                "vehicle": vehicle_name,
                "change": -vehicle_damage_past_sp,
                "reason": f"Ramming {target_name}",
            })
        if vehicle_ablation > 0:
            state_ops.append({
                "op": "vehicle_sp",
                "vehicle": vehicle_name,
                "change": -vehicle_ablation,
                "reason": f"Ramming ablation vs {target_name}",
            })

    # --- Whiplash critical injuries ---
    _whiplash = {
        "name": "Whiplash",
        "effect": "No additional effect beyond Death Save penalty.",
        "dv_mod": 1,
        "location": "head",
    }

    # Target occupants get Whiplash when the target is a vehicle.
    if target_is_vehicle:
        for occ in target_occupants:
            occ_name = occ.get("name", "") if isinstance(occ, dict) else str(occ)
            if occ_name:
                state_ops.append(_critical_injury_state_op(occ_name, _whiplash, f"Rammed by {vehicle_name}"))

    # Pedestrian target gets Whiplash too
    if not target_is_vehicle and target_name:
        state_ops.append(_critical_injury_state_op(target_name, _whiplash, f"Rammed by {vehicle_name}"))

    # Attacker occupants get Whiplash only if no Combat Plow
    if not combat_plow:
        for occ in occupants:
            occ_name = occ.get("name", "") if isinstance(occ, dict) else str(occ)
            if occ_name:
                state_ops.append(_critical_injury_state_op(occ_name, _whiplash, f"Ramming {target_name}"))

    # Vehicle stops if it didn't destroy the target.
    # When target durability wasn't provided, return None (unknown).
    if target_is_vehicle:
        vehicle_stopped = (target_sdp_current - target_damage_past_sp > 0) if isinstance(target_sdp_current, int) else None
    else:
        vehicle_stopped = (target_hp_current - target_damage_past_sp > 0) if isinstance(target_hp_current, int) else None

    formatted_parts = [f"Ram {target_name}: {ram_dice}d6[**{dice_str}**] = {ram_damage_total}"]
    if dodge_result:
        formatted_parts.insert(0, f"Dodge: {dodge_result['formatted']}")
    formatted_parts.append(f"→ target SP {target_effective_sp}, {target_damage_past_sp} past armor")
    if not combat_plow:
        formatted_parts.append(f"→ attacker SP {vehicle_sp}, {vehicle_damage_past_sp} self-damage")
    else:
        formatted_parts.append("→ Combat Plow: no self-damage")
    if vehicle_stopped is True:
        formatted_parts.append("→ vehicle STOPPED (target survived)")

    return {
        "type": "ramming",
        "dodge_result": dodge_result,
        "dodged": False,
        "combat_plow": combat_plow,
        "nos_boosted": nos_boosted,
        "ram_damage_dice": ram_dice,
        "ram_damage_total": ram_damage_total,
        "vehicle_damage": vehicle_damage_past_sp,
        "target_damage": target_damage_past_sp,
        "vehicle_stopped": vehicle_stopped,
        "target_ablation": target_ablation,
        "vehicle_ablation": vehicle_ablation,
        "state_ops": state_ops,
        "formatted": " | ".join(formatted_parts),
        "on_outcome": on_hit,
    }


def resolve_vehicle_weak_point(
    stat_value: int,
    skill_value: int,
    weapon_type: str,
    damage_dice: int,
    vehicle_sp: int,
    vehicle_name: str,
    range_bracket: int = 0,
    target_moving: bool = True,
    seriously_wounded: bool = False,
    luck_spent: int = 0,
    is_ap: bool = False,
    weapon_name: str = "",
    character_name: str = "",
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a weak point shot against a vehicle.

    Aimed shot penalty (+8 DV) if target is moving. Auto-hit if stationary.
    Damage past SP is doubled. SP ablates by 1 (2 for AP).
    """
    state_ops = []
    target_moving = _as_bool(target_moving, True)
    seriously_wounded = _as_bool(seriously_wounded, False)
    is_ap = _as_bool(is_ap, False)
    stat_value = _to_int(stat_value, 0)
    skill_value = _to_int(skill_value, 0)
    damage_dice = _to_int(damage_dice, 2, minimum=1)
    luck_spent = _to_int(luck_spent, 0, minimum=0)
    vehicle_name = _norm_name(vehicle_name)
    try:
        range_bracket = int(range_bracket)
    except (TypeError, ValueError, OverflowError):
        try:
            _err_sp = max(0, int(vehicle_sp))
        except (TypeError, ValueError, OverflowError):
            _err_sp = 0
        return {
            "type": "vehicle_weak_point",
            "attack_roll": None,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": _err_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "vehicle_name": vehicle_name,
            "state_ops": [],
            "error": f"Invalid range bracket: {range_bracket}",
            "formatted": f"Weak point shot at {vehicle_name or 'vehicle'}: invalid range bracket",
            "on_outcome": on_miss,
        }
    try:
        vehicle_sp = max(0, int(vehicle_sp))
    except (TypeError, ValueError, OverflowError):
        vehicle_sp = 0
    if not vehicle_name:
        return {
            "type": "vehicle_weak_point",
            "attack_roll": None,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "vehicle_name": "",
            "state_ops": [],
            "error": "Vehicle weak point shot requires vehicle_name",
            "formatted": "Vehicle weak point shot: missing vehicle_name",
            "on_outcome": on_miss,
        }

    # Validate ranged weapon/range compatibility for both moving and stationary targets.
    weapon_type = _normalize_ranged_weapon_type(weapon_type)
    dvs = RANGED_DV_TABLE.get(weapon_type)
    if not dvs or range_bracket < 0 or range_bracket >= len(dvs) or dvs[range_bracket] is None:
        return {
            "type": "vehicle_weak_point",
            "attack_roll": None,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "vehicle_name": vehicle_name,
            "state_ops": [],
            "error": f"Weapon '{weapon_type}' cannot fire at range bracket {range_bracket}",
            "formatted": (
                f"Weak point shot at {vehicle_name}: Weapon '{weapon_type}' "
                f"cannot fire at range bracket {range_bracket}"
            ),
            "on_outcome": on_miss,
        }

    # Attack roll (only if target is moving)
    # Ruleset §18: moving weak point = flat DV13 + aimed shot −8 = DV21,
    # NOT the range-based DV table.  Range validation above already rejected
    # weapons that can't reach this bracket; the hit DV is always 21.
    attack_roll = None
    hit = True
    if target_moving:
        aimed_dv = VEHICLE_WEAK_POINT_MOVING_DV + AIMED_SHOT_DV_PENALTY
        attack_roll = resolve_check(
            stat_value=stat_value,
            skill_value=skill_value,
            dv=aimed_dv,
            seriously_wounded=seriously_wounded,
            luck_spent=luck_spent,
        )
        hit = attack_roll["success"]

    if not hit:
        return {
            "type": "vehicle_weak_point",
            "attack_roll": attack_roll,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "vehicle_name": vehicle_name,
            "state_ops": [],
            "formatted": f"Weak point shot at {vehicle_name}: {attack_roll['formatted']} — MISS",
            "on_outcome": on_miss,
        }

    # Roll damage
    damage_dice = max(1, damage_dice)
    dice = [_roll_d6() for _ in range(damage_dice)]
    raw_damage = sum(dice)
    dice_str = ",".join(str(d) for d in dice)

    effective_sp = vehicle_sp
    damage_past_sp = max(0, raw_damage - effective_sp)
    doubled_damage = damage_past_sp * 2  # Weak point: doubled past SP
    ablation = 2 if is_ap else 1
    if damage_past_sp == 0:
        ablation = 0

    total_sdp_damage = doubled_damage

    if total_sdp_damage > 0:
        state_ops.append({
            "op": "vehicle_sdp",
            "vehicle": vehicle_name,
            "change": -total_sdp_damage,
            "reason": f"Weak point shot by {character_name or 'attacker'}",
        })
    if ablation > 0:
        state_ops.append({
            "op": "vehicle_sp",
            "vehicle": vehicle_name,
            "change": -ablation,
            "reason": f"Ablation from weak point shot by {character_name or 'attacker'}",
        })

    fmt_parts = []
    if attack_roll:
        fmt_parts.append(f"Weak point at {vehicle_name}: {attack_roll['formatted']}")
    else:
        fmt_parts.append(f"Weak point at {vehicle_name} (stationary, auto-hit)")
    fmt_parts.append(f"Damage: {damage_dice}d6[**{dice_str}**] = {raw_damage} → SP {effective_sp}")
    if damage_past_sp > 0:
        fmt_parts.append(f"{damage_past_sp} past SP ×2 = {doubled_damage} SDP damage")
    else:
        fmt_parts.append("blocked by armor")
    if ablation > 0:
        fmt_parts.append(f"SP ablation -{ablation}")

    return {
        "type": "vehicle_weak_point",
        "attack_roll": attack_roll,
        "hit": True,
        "raw_damage": raw_damage,
        "effective_sp": effective_sp,
        "damage_past_sp": damage_past_sp,
        "doubled_damage": doubled_damage,
        "ablation": ablation,
        "vehicle_name": vehicle_name,
        "state_ops": state_ops,
        "formatted": " | ".join(fmt_parts),
        "on_outcome": on_hit,
    }


def resolve_spike_strip(
    target_driver_name: str,
    stat_value: int,
    skill_value: int,
    target_vehicle_name: str,
    target_vehicle_sp: int,
    target_vehicle_type: str = "land",
    seriously_wounded: bool = False,
    on_hit: str = "",
    on_miss: str = "",
) -> dict:
    """Resolve a spike strip: target driver rolls DV17 Drive Land Vehicle.

    On failure, 4d6 weak point damage (doubled past SP, ablation).

    Note: semantics are inverted from attack resolvers — the *target* makes the
    check, so check success = strip avoided (on_miss), check failure = strip hit
    (on_hit). The result["hit"] field reflects whether the strip dealt damage.
    """
    target_vehicle_name = str(target_vehicle_name or "").strip()
    target_driver_name = _norm_name(target_driver_name)
    stat_value = _to_int(stat_value, 0)
    skill_value = _to_int(skill_value, 0)
    seriously_wounded = _as_bool(seriously_wounded, False)
    try:
        target_vehicle_sp = max(0, int(target_vehicle_sp))
    except (TypeError, ValueError, OverflowError):
        target_vehicle_sp = 0
    if not target_vehicle_name:
        return {
            "type": "spike_strip",
            "check_result": None,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": target_vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "target_vehicle_name": "",
            "state_ops": [],
            "on_outcome": on_miss,
            "error": "Spike strip requires target_vehicle_name",
            "formatted": "Spike strip: missing target_vehicle_name",
        }
    if str(target_vehicle_type or "").strip().lower() != "land":
        return {
            "type": "spike_strip",
            "check_result": None,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": target_vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "target_vehicle_name": target_vehicle_name,
            "state_ops": [],
            "on_outcome": on_miss,
            "error": f"Spike strips can only target land vehicles (got: {target_vehicle_type})",
            "formatted": f"Spike strip vs {target_driver_name}: INVALID TARGET ({target_vehicle_type}) — land vehicles only",
        }

    state_ops = []

    check = resolve_check(
        stat_value=stat_value,
        skill_value=skill_value,
        dv=SPIKE_STRIP_DV,
        seriously_wounded=seriously_wounded,
    )

    if check["success"]:
        return {
            "type": "spike_strip",
            "check_result": check,
            "hit": False,
            "raw_damage": 0,
            "effective_sp": target_vehicle_sp,
            "damage_past_sp": 0,
            "doubled_damage": 0,
            "ablation": 0,
            "target_vehicle_name": target_vehicle_name,
            "state_ops": [],
            "formatted": f"Spike strip vs {target_driver_name}: {check['formatted']} — AVOIDED",
            "on_outcome": on_miss,
        }

    # Failed — roll damage
    dice = [_roll_d6() for _ in range(SPIKE_STRIP_DAMAGE_DICE)]
    raw_damage = sum(dice)
    dice_str = ",".join(str(d) for d in dice)

    effective_sp = target_vehicle_sp
    damage_past_sp = max(0, raw_damage - effective_sp)
    doubled_damage = damage_past_sp * 2  # weak point: doubled
    ablation = 1 if damage_past_sp > 0 else 0

    if doubled_damage > 0:
        state_ops.append({
            "op": "vehicle_sdp",
            "vehicle": target_vehicle_name,
            "change": -doubled_damage,
            "reason": f"Spike strip hit on {target_vehicle_name}",
        })
    if ablation > 0:
        state_ops.append({
            "op": "vehicle_sp",
            "vehicle": target_vehicle_name,
            "change": -ablation,
            "reason": f"Spike strip ablation on {target_vehicle_name}",
        })

    fmt_parts = [f"Spike strip vs {target_driver_name}: {check['formatted']} — HIT!"]
    fmt_parts.append(f"Damage: {SPIKE_STRIP_DAMAGE_DICE}d6[**{dice_str}**] = {raw_damage} → SP {effective_sp}")
    if damage_past_sp > 0:
        fmt_parts.append(f"{damage_past_sp} past SP ×2 = {doubled_damage} SDP damage")
    else:
        fmt_parts.append("blocked by armor")
    if ablation > 0:
        fmt_parts.append(f"SP ablation -{ablation}")

    return {
        "type": "spike_strip",
        "check_result": check,
        "hit": True,
        "raw_damage": raw_damage,
        "effective_sp": effective_sp,
        "damage_past_sp": damage_past_sp,
        "doubled_damage": doubled_damage,
        "ablation": ablation,
        "target_vehicle_name": target_vehicle_name,
        "state_ops": state_ops,
        "formatted": " | ".join(fmt_parts),
        "on_outcome": on_hit,
    }


# ---------------------------------------------------------------------------
# resolve_actions — batch resolver
# ---------------------------------------------------------------------------

def resolve_actions(actions: list, relationships: dict = None, factions: dict = None,
                    sequential: bool = True, combatant_hp: dict = None,
                    tar_stacks: int = 0, alert_level: int = 0,
                    active_programs=None, installed_hardware=None,
                    ice_status=None, combatant_vehicle_sdp: dict = None,
                    relationship_owner: str = "", relationship_actor_names=None,
                    relationship_present_names=None, relationship_context: dict = None,
                    edgerunner_states: dict = None,
                    character_states: dict = None) -> dict:
    """Resolve a batch of mechanical actions.

    Each action is a dict with "type" and type-specific fields.
    When relationships/factions are provided, rel_bonus is auto-computed
    from the target's tier for actions that include a target and check_context.

    When sequential=True and combatant_hp is provided, tracks running HP:
    - After each action, subtract hp_damage from target's effective_hp
    - Before each action, check if actor's effective_hp <= 0 → skip (eliminated)
    - Pass current target HP to sub-resolvers for accurate rubber ammo capping

    When combatant_vehicle_sdp is provided, tracks running vehicle SDP/SP
    so sequential actions against the same vehicle use fresh values.

    Returns {"results": [...], "state_ops": [...]}.
    """
    from .cpred import compute_rel_bonus

    def _romantic_partner_names(min_roms: int) -> set[str]:
        partners = set()
        if not isinstance(relationships, dict):
            return partners
        for npc_name, npc_data in relationships.items():
            if not isinstance(npc_data, dict):
                continue
            try:
                roms = int(npc_data.get("roms", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if roms >= min_roms:
                partner_name = _norm_name(npc_name)
                if partner_name:
                    partners.add(partner_name)
        return partners

    _batch_chars = {
        _norm_name(a.get("character", ""))
        for a in actions
        if isinstance(a, dict) and a.get("character")
    }
    _batch_chars.discard("")
    if isinstance(relationship_context, dict):
        _relationship_context = build_relationship_context(
            actions=actions,
            relationship_owner=relationship_context.get("owner_name", relationship_owner),
            relationship_actor_names=relationship_context.get("actor_names", relationship_actor_names),
            relationship_present_names=relationship_context.get("present_names", relationship_present_names),
        )
    else:
        _relationship_context = build_relationship_context(
            actions=actions,
            relationship_owner=relationship_owner,
            relationship_actor_names=relationship_actor_names,
            relationship_present_names=relationship_present_names,
        )
    _presence_chars = _relationship_context["present_names"] or set(_batch_chars)
    _relationship_actor_names = _relationship_context["actor_names"]
    _relationship_owner_name = _relationship_context["owner_name"]

    _romantic_partners_t2 = _romantic_partner_names(25)
    _romantic_partners_t3 = _romantic_partner_names(45)
    fighting_together_chars: set[str] = set()
    if _relationship_owner_name and _relationship_owner_name in _presence_chars:
        for partner_name in _romantic_partners_t3:
            if partner_name in _presence_chars:
                fighting_together_chars.add(_relationship_owner_name)
                fighting_together_chars.add(partner_name)

    results = []
    all_state_ops = []
    effective_hp = {}
    if isinstance(combatant_hp, dict):
        for _k, _v in list(combatant_hp.items()):
            if not isinstance(_k, str):
                continue
            _hk = _norm_hp_track_key(_k)
            if not _hk:
                continue
            try:
                _parsed = max(0, int(_v))
            except (TypeError, ValueError, OverflowError):
                continue
            if _hk in effective_hp:
                effective_hp[_hk] = min(effective_hp[_hk], _parsed)
            else:
                effective_hp[_hk] = _parsed
    effective_armor_sp = {}
    effective_vehicle_sdp = dict(combatant_vehicle_sdp) if isinstance(combatant_vehicle_sdp, dict) else {}
    if effective_vehicle_sdp:
        _normalized_vehicle_sdp = {}
        for _k, _v in list(effective_vehicle_sdp.items()):
            if not isinstance(_k, str):
                continue
            _norm_key = _norm_name(_k).casefold()
            if not _norm_key:
                continue
            if _norm_key.endswith(":sp"):
                _norm_key = _norm_vehicle_track_key(_norm_key[:-3], sp=True)
                try:
                    _parsed = max(0, int(_v))
                except (TypeError, ValueError, OverflowError):
                    _parsed = 0
                _normalized_vehicle_sdp[_norm_key] = _parsed
            else:
                if _v is None:
                    _parsed = None
                else:
                    try:
                        _parsed = max(0, int(_v))
                    except (TypeError, ValueError, OverflowError):
                        _parsed = None
                if _norm_key in _normalized_vehicle_sdp and isinstance(_normalized_vehicle_sdp[_norm_key], int) and isinstance(_parsed, int):
                    _normalized_vehicle_sdp[_norm_key] = min(_normalized_vehicle_sdp[_norm_key], _parsed)
                else:
                    _normalized_vehicle_sdp[_norm_key] = _parsed
        effective_vehicle_sdp = _normalized_vehicle_sdp
    vehicle_tracking_enabled = combatant_vehicle_sdp is not None
    _tar_consumed = False

    for action in actions:
        _ops_before = len(all_state_ops)
        try:
            action = _normalize_action(action)
            if action is None:
                results.append({"type": None, "error": "Non-dict action skipped"})
                continue
            action_type = action.get("type")
            if action_type == "driving_check" and not str(action.get("vehicle_name", "")).strip() and not (sequential and vehicle_tracking_enabled):
                results.append({
                    "type": action_type,
                    "character": action.get("character", ""),
                    "error": "Driving check requires vehicle_name",
                })
                continue
            if action_type == "vehicle_weak_point" and not str(action.get("vehicle_name", "")).strip():
                results.append({
                    "type": action_type,
                    "character": action.get("character", ""),
                    "error": "Vehicle weak point shot requires vehicle_name",
                })
                continue
            if action_type == "spike_strip" and not str(action.get("target_vehicle_name", "")).strip():
                results.append({
                    "type": action_type,
                    "character": action.get("character", ""),
                    "error": "Spike strip requires target_vehicle_name",
                })
                continue
            if action_type == "ramming":
                if not str(action.get("vehicle_name", "")).strip():
                    results.append({
                        "type": action_type,
                        "character": action.get("character", ""),
                        "error": "Ramming requires vehicle_name",
                    })
                    continue
                if not str(action.get("target", "")).strip():
                    results.append({
                        "type": action_type,
                        "character": action.get("character", ""),
                        "error": "Ramming requires target",
                    })
                    continue

            # Seed vehicle tracking for newly introduced vehicles in this same batch.
            if sequential and vehicle_tracking_enabled and isinstance(action, dict):
                def _to_nonneg_int(_value, _default):
                    try:
                        return max(0, int(_value))
                    except (TypeError, ValueError, OverflowError):
                        return _default

                def _seed_vehicle_entry(_name, _sdp_value=None, _sp_value=None):
                    _vk = _norm_vehicle_track_key(_name)
                    if not _vk:
                        return
                    _parsed_sdp = _to_nonneg_int(_sdp_value, None)
                    if _vk not in effective_vehicle_sdp:
                        # Unknown SDP remains None so downstream logic can preserve "unknown"
                        # outcomes (e.g., vehicle_stopped=None) instead of fabricating certainty.
                        effective_vehicle_sdp[_vk] = max(0, _parsed_sdp) if isinstance(_parsed_sdp, int) else None
                    elif effective_vehicle_sdp[_vk] is None and isinstance(_parsed_sdp, int):
                        # Upgrade unknown tracking entry once a concrete SDP appears later.
                        effective_vehicle_sdp[_vk] = max(0, _parsed_sdp)
                    _sp_key = _norm_vehicle_track_key(_name, sp=True)
                    if _sp_key not in effective_vehicle_sdp:
                        effective_vehicle_sdp[_sp_key] = _to_nonneg_int(_sp_value, 0)

                if action_type == "ramming":
                    _seed_vehicle_entry(
                        action.get("vehicle_name", ""),
                        action.get("vehicle_sdp_current"),
                        action.get("vehicle_sp"),
                    )
                    if _as_bool(action.get("target_is_vehicle", False), False):
                        _seed_vehicle_entry(
                            action.get("target", ""),
                            action.get("target_sdp_current"),
                            action.get("target_sp"),
                        )
                elif action_type == "driving_check":
                    _seed_vehicle_entry(
                        action.get("vehicle_name", ""),
                        action.get("vehicle_sdp_current"),
                        action.get("vehicle_sp"),
                    )
                elif action_type == "vehicle_weak_point":
                    _seed_vehicle_entry(
                        action.get("vehicle_name", ""),
                        action.get("vehicle_sdp_current"),
                        action.get("vehicle_sp"),
                    )
                elif action_type == "spike_strip":
                    _seed_vehicle_entry(
                        action.get("target_vehicle_name", ""),
                        action.get("target_vehicle_sdp_current"),
                        action.get("target_vehicle_sp"),
                    )
            if sequential and isinstance(action, dict) and action_type == "ramming":
                if not _as_bool(action.get("target_is_vehicle", False), False):
                    _ped = _norm_name(action.get("target", "")).casefold()
                    if _ped and _ped not in effective_armor_sp:
                        try:
                            effective_armor_sp[_ped] = max(0, int(action.get("target_sp", 0)))
                        except (TypeError, ValueError, OverflowError):
                            effective_armor_sp[_ped] = 0

            # Sequential elimination: skip actors at 0 HP
            if sequential and effective_hp and action_type not in ("initiative", "ambush"):
                actor_name = action.get("character", "") if isinstance(action, dict) else ""
                actor_key = _norm_hp_track_key(actor_name)
                if actor_key and actor_key in effective_hp and effective_hp[actor_key] <= 0:
                    results.append({
                        "type": action_type, "character": actor_name,
                        "skipped": True, "reason": "eliminated",
                    })
                    continue

            # Sequential vehicle elimination: skip actions by/against destroyed vehicles
            if sequential and vehicle_tracking_enabled and action_type in ("ramming", "driving_check", "vehicle_weak_point", "spike_strip"):
                # Attacker's vehicle (ramming, driving_check)
                _act_vname = action.get("vehicle_name", "") if isinstance(action, dict) else ""
                # Target vehicle (vehicle_weak_point, spike_strip)
                if not _act_vname and action_type == "spike_strip":
                    _act_vname = action.get("target_vehicle_name", "") if isinstance(action, dict) else ""
                _act_vkey = _norm_vehicle_track_key(_act_vname)
                if action_type == "driving_check" and not _act_vname:
                    _tracked = [k for k in effective_vehicle_sdp.keys()
                                if isinstance(k, str) and not k.endswith(":sp")]
                    if len(_tracked) == 1:
                        _act_vname = _tracked[0]
                        _act_vkey = _norm_vehicle_track_key(_act_vname)
                    else:
                        _skip_note = "Driving check skipped: vehicle_name is required for sequential vehicle tracking."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "skipped": True,
                            "reason": "vehicle_name_required",
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
                if _act_vkey and _act_vkey in effective_vehicle_sdp and isinstance(effective_vehicle_sdp[_act_vkey], int) and effective_vehicle_sdp[_act_vkey] <= 0:
                    _skip_note = f"Action skipped: {_act_vname} is already destroyed."
                    results.append({
                        "type": action_type,
                        "character": action.get("character", ""),
                        "skipped": True,
                        "reason": "vehicle_destroyed",
                        "vehicle_name": _act_vname,
                        "notification": _skip_note,
                        "formatted": _skip_note,
                    })
                    continue
                if action_type == "ramming" and _as_bool(action.get("target_is_vehicle", False), False):
                    _target_vname = action.get("target", "") if isinstance(action, dict) else ""
                    _target_vkey = _norm_vehicle_track_key(_target_vname)
                    if _target_vkey and _target_vkey in effective_vehicle_sdp and isinstance(effective_vehicle_sdp[_target_vkey], int) and effective_vehicle_sdp[_target_vkey] <= 0:
                        _skip_note = f"Ramming skipped: target vehicle {_target_vname} is already destroyed."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "target": _target_vname,
                            "skipped": True,
                            "reason": "target_vehicle_destroyed",
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
            # Baseline destroyed-vehicle guard even when no tracking map is provided.
            if sequential and action_type in ("ramming", "driving_check", "vehicle_weak_point", "spike_strip"):
                def _as_nonneg_int_or_none(_v):
                    try:
                        return max(0, int(_v))
                    except (TypeError, ValueError, OverflowError):
                        return None

                def _is_destroyed_vehicle(_name, _fallback_sdp=None):
                    _vk = _norm_vehicle_track_key(_name)
                    if vehicle_tracking_enabled and _vk in effective_vehicle_sdp and isinstance(effective_vehicle_sdp[_vk], int):
                        return effective_vehicle_sdp[_vk] <= 0
                    _parsed = _as_nonneg_int_or_none(_fallback_sdp)
                    return isinstance(_parsed, int) and _parsed <= 0

                if action_type in ("ramming", "driving_check"):
                    _act_name = action.get("vehicle_name", "") if isinstance(action, dict) else ""
                    if _act_name and _is_destroyed_vehicle(_act_name, action.get("vehicle_sdp_current")):
                        _skip_note = f"Action skipped: {_act_name} is already destroyed."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "skipped": True,
                            "reason": "vehicle_destroyed",
                            "vehicle_name": _act_name,
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
                elif action_type == "vehicle_weak_point":
                    _target_name = action.get("vehicle_name", "") if isinstance(action, dict) else ""
                    if _target_name and _is_destroyed_vehicle(_target_name, action.get("vehicle_sdp_current")):
                        _skip_note = f"Action skipped: {_target_name} is already destroyed."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "skipped": True,
                            "reason": "vehicle_destroyed",
                            "vehicle_name": _target_name,
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
                elif action_type == "spike_strip":
                    _target_name = action.get("target_vehicle_name", "") if isinstance(action, dict) else ""
                    if _target_name and _is_destroyed_vehicle(_target_name, action.get("target_vehicle_sdp_current")):
                        _skip_note = f"Action skipped: {_target_name} is already destroyed."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "skipped": True,
                            "reason": "vehicle_destroyed",
                            "vehicle_name": _target_name,
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
                if action_type == "ramming" and _as_bool(action.get("target_is_vehicle", False), False):
                    _target_name = action.get("target", "") if isinstance(action, dict) else ""
                    if _target_name and _is_destroyed_vehicle(_target_name, action.get("target_sdp_current")):
                        _skip_note = f"Ramming skipped: target vehicle {_target_name} is already destroyed."
                        results.append({
                            "type": action_type,
                            "character": action.get("character", ""),
                            "target": _target_name,
                            "skipped": True,
                            "reason": "target_vehicle_destroyed",
                            "notification": _skip_note,
                            "formatted": _skip_note,
                        })
                        continue
            # ----- Hydrate stat/skill values from state -----
            # Resolve difficulty tier → numeric DV for skill_check
            if action_type == "skill_check":
                _difficulty = action.get("difficulty")
                if isinstance(_difficulty, str) and _difficulty.strip():
                    _tier_dv = _DV_TIERS.get(_difficulty.strip().lower())
                    if _tier_dv is not None:
                        action["dv"] = _tier_dv
                elif isinstance(_difficulty, (int, float)):
                    action["dv"] = int(_difficulty)

            # ----- Validate `ability` on NET-context skill / opposed checks -----
            # Required tag when net=true so Interface Ability hooks can fire on
            # the right roll (Step 4 boosters: Worm/Eraser/See Ya/Speedy Gonzalvez).
            # Closed enum — schema validation lives at the contract layer too,
            # but the model can drift, so reject + return a structured error here.
            if action_type in ("skill_check", "opposed_check") and action.get("net"):
                _ability = action.get("ability")
                if not isinstance(_ability, str) or _ability not in INTERFACE_ABILITIES:
                    _abilities_list = ", ".join(sorted(INTERFACE_ABILITIES))
                    results.append({
                        "type": action_type,
                        "character": action.get("character", ""),
                        "error": "missing_or_invalid_ability",
                        "reason": (
                            f"NET-context {action_type} requires an `ability` field "
                            f"matching one of: {_abilities_list}. Got: {_ability!r}"
                        ),
                        "formatted": f"⚠ NET {action_type} missing valid `ability` tag",
                    })
                    continue

            # Standard stat/skill hydration per action type
            if action_type in ("skill_check", "ranged_attack", "autofire", "driving_check", "vehicle_weak_point"):
                _hydrate_stats_from_state(
                    action, edgerunner_states, character_states,
                    character_key="character",
                    stat_value_key="stat_value", skill_value_key="skill_value",
                    stat_name_key="stat", skill_name_key="skill",
                    wounded_key="seriously_wounded",
                )
            elif action_type == "program_attack":
                # Intent-only: derive program_atk from PROGRAM_STATS, target DEF/REZ
                # from ice_status. Model can still pass explicit values to override.
                _hydrate_program_attack_from_state(action, ice_status)
            elif action_type == "opposed_check":
                # Hydrate attacker
                _hydrate_stats_from_state(
                    action, edgerunner_states, character_states,
                    character_key="character",
                    stat_value_key="attacker_stat", skill_value_key="attacker_skill",
                    stat_name_key="attacker_label", skill_name_key="attacker_skill_label",
                    wounded_key="seriously_wounded_attacker",
                )
                # Hydrate defender
                _hydrate_stats_from_state(
                    action, edgerunner_states, character_states,
                    character_key="target",
                    stat_value_key="defender_stat", skill_value_key="defender_skill",
                    stat_name_key="defender_label", skill_name_key="defender_skill_label",
                    wounded_key="seriously_wounded_defender",
                )
            elif action_type == "melee_attack":
                # Hydrate attacker
                _hydrate_stats_from_state(
                    action, edgerunner_states, character_states,
                    character_key="character",
                    stat_value_key="attacker_stat", skill_value_key="attacker_skill",
                    stat_name_key="attacker_label", skill_name_key="attacker_skill_label",
                    wounded_key="seriously_wounded_attacker",
                )
                # Hydrate defender
                _hydrate_stats_from_state(
                    action, edgerunner_states, character_states,
                    character_key="target",
                    stat_value_key="defender_stat", skill_value_key="defender_skill",
                    stat_name_key="defender_label", skill_name_key="defender_skill_label",
                    wounded_key="seriously_wounded_defender",
                )
            elif action_type == "facedown":
                # Initiator: COOL, rep, seriously_wounded
                _fd_i_stats, _, _fd_i_hp, _fd_i_hpmax, _fd_i_rep = _find_character_state(
                    action.get("character", ""), edgerunner_states, character_states)
                if _fd_i_stats is not None:
                    _v = _lookup_stat_ci(_fd_i_stats, "COOL")
                    if _v is not None:
                        action["initiator_cool"] = _v
                    # Only override rep when stats are bootstrapped (non-empty)
                    if _fd_i_stats and isinstance(_fd_i_rep, (int, float)):
                        action["initiator_rep"] = int(_fd_i_rep)
                    _sw = _derive_seriously_wounded(_fd_i_hp, _fd_i_hpmax)
                    if _sw is not None:
                        action["seriously_wounded_initiator"] = _sw
                # Opponent: COOL, rep, seriously_wounded
                _fd_o_stats, _, _fd_o_hp, _fd_o_hpmax, _fd_o_rep = _find_character_state(
                    action.get("target", ""), edgerunner_states, character_states)
                if _fd_o_stats is not None:
                    _v = _lookup_stat_ci(_fd_o_stats, "COOL")
                    if _v is not None:
                        action["opponent_cool"] = _v
                    if _fd_o_stats and isinstance(_fd_o_rep, (int, float)):
                        action["opponent_rep"] = int(_fd_o_rep)
                    _sw = _derive_seriously_wounded(_fd_o_hp, _fd_o_hpmax)
                    if _sw is not None:
                        action["seriously_wounded_opponent"] = _sw
            elif action_type == "suppressive_fire":
                # Attacker: REF, Autofire, seriously_wounded
                _sf_stats, _sf_skills, _sf_hp, _sf_hpmax, _ = _find_character_state(
                    action.get("character", ""), edgerunner_states, character_states)
                if _sf_stats is not None:
                    _v = _lookup_stat_ci(_sf_stats, "REF")
                    if _v is not None:
                        action["attacker_ref"] = _v
                    _v = _lookup_stat_ci(_sf_skills or _sf_stats, "Autofire")
                    if _v is not None:
                        action["attacker_autofire"] = _v
                    _sw = _derive_seriously_wounded(_sf_hp, _sf_hpmax)
                    if _sw is not None:
                        action["seriously_wounded_attacker"] = _sw
                # Each target: WILL, Concentration, seriously_wounded
                for _sf_tgt in action.get("targets", []):
                    if not isinstance(_sf_tgt, dict):
                        continue
                    _ts, _tsk, _thp, _thpmax, _ = _find_character_state(
                        _sf_tgt.get("name", ""), edgerunner_states, character_states)
                    if _ts is not None:
                        _v = _lookup_stat_ci(_ts, "WILL")
                        if _v is not None:
                            _sf_tgt["will"] = _v
                        _v = _lookup_stat_ci(_tsk or _ts, "Concentration")
                        if _v is not None:
                            _sf_tgt["concentration"] = _v
                        _sw = _derive_seriously_wounded(_thp, _thpmax)
                        if _sw is not None:
                            _sf_tgt["seriously_wounded"] = _sw
            elif action_type == "initiative":
                # Each combatant: REF
                for _init_c in action.get("combatants", []):
                    if not isinstance(_init_c, dict):
                        continue
                    _is, _, _, _, _ = _find_character_state(
                        _init_c.get("name", ""), edgerunner_states, character_states)
                    if _is is not None:
                        _v = _lookup_stat_ci(_is, "REF")
                        if _v is not None:
                            _init_c["ref"] = _v
            elif action_type == "ambush":
                # Ambusher: DEX, Stealth
                _as, _ask, _, _, _ = _find_character_state(
                    action.get("character", ""), edgerunner_states, character_states)
                if _as is not None:
                    _v = _lookup_stat_ci(_as, "DEX")
                    if _v is not None:
                        action["stealth_stat"] = _v
                    _v = _lookup_stat_ci(_ask or _as, "Stealth")
                    if _v is not None:
                        action["stealth_skill"] = _v
                # Each target: INT, Concentration
                for _amb_tgt in action.get("targets", []):
                    if not isinstance(_amb_tgt, dict):
                        continue
                    _ts, _tsk, _, _, _ = _find_character_state(
                        _amb_tgt.get("name", ""), edgerunner_states, character_states)
                    if _ts is not None:
                        _v = _lookup_stat_ci(_ts, "INT")
                        if _v is not None:
                            _amb_tgt["perception_stat"] = _v
                        _v = _lookup_stat_ci(_tsk or _ts, "Concentration")
                        if _v is not None:
                            _amb_tgt["perception_skill"] = _v
            elif action_type == "haggle":
                # Buyer: COOL, Trading, seriously_wounded
                _hs, _hsk, _hhp, _hhpmax, _ = _find_character_state(
                    action.get("character", ""), edgerunner_states, character_states)
                if _hs is not None:
                    _v = _lookup_stat_ci(_hs, "COOL")
                    if _v is not None:
                        action["buyer_cool"] = _v
                    _v = _lookup_stat_ci(_hsk or _hs, "Trading")
                    if _v is not None:
                        action["buyer_trading"] = _v
                    _sw = _derive_seriously_wounded(_hhp, _hhpmax)
                    if _sw is not None:
                        action["seriously_wounded"] = _sw
            elif action_type in ("hustle", "find_item"):
                # Only auto-derive seriously_wounded
                _, _, _hf_hp, _hf_hpmax, _ = _find_character_state(
                    action.get("character", ""), edgerunner_states, character_states)
                _sw = _derive_seriously_wounded(_hf_hp, _hf_hpmax)
                if _sw is not None:
                    action["seriously_wounded"] = _sw

            # Apply TAR penalty to next NET check only (auto-enforced by backend)
            if tar_stacks > 0 and action.get("net") and not _tar_consumed:
                _tar_penalty = tar_stacks * 2
                if action_type == "skill_check":
                    action["stat_value"] = action.get("stat_value", 0) - _tar_penalty
                    _tar_consumed = True
                    all_state_ops.append({"op": "tar_consumed"})
                elif action_type == "opposed_check":
                    action["attacker_stat"] = action.get("attacker_stat", 0) - _tar_penalty
                    _tar_consumed = True
                    all_state_ops.append({"op": "tar_consumed"})

            # Apply alert-level DV penalty to NET skill checks (+2 at alert 3+)
            if alert_level >= 3 and action.get("net") and action_type == "skill_check":
                action["dv"] = action.get("dv", 13) + 2

            if action_type == "skill_check":
                actor_name = action.get("character", "")
                # Auto-compute rel_bonus from relationships when target is provided
                rel_bonus = action.get("rel_bonus", 0)
                if relationships or factions:
                    target = action.get("target", "")
                    if target:
                        rel_bonus = compute_rel_bonus(
                            relationships, factions, target,
                            check_context=action.get("check_context"),
                        )
                # Wellbeing Boost: +1 if the named NPC has an available boost
                _wb_boost = 0
                _wb_boost_npc = action.get("wb_boost_used")
                if _wb_boost_npc and relationships:
                    _wb_npc_data = relationships.get(_wb_boost_npc, {})
                    if isinstance(_wb_npc_data, dict) and _wb_npc_data.get("wb_boost"):
                        _wb_boost = 1
                result = resolve_check(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    dv=action.get("dv", 13),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=rel_bonus,
                    wb_boost=_wb_boost,
                )
                result["type"] = "skill_check"
                result["character"] = actor_name
                if action.get("net"):
                    result["net"] = True
                    result["ability"] = action.get("ability")
                # Emit wb_boost_spend op to consume the boost
                if _wb_boost:
                    all_state_ops.append({
                        "type": "relationship_op",
                        "op": "wb_boost_spend",
                        "target": _wb_boost_npc,
                        "reason": f"Wellbeing boost spent on skill check by {actor_name}",
                    })
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on skill check")

            elif action_type == "ranged_attack":
                actor_name = action.get("character", "")
                actor_key = _norm_name(actor_name)
                _ra_target_name = action.get("target", "")
                # Auto-compute rel_bonus from relationships when target is provided
                ra_rel_bonus = action.get("rel_bonus", 0)
                if relationships or factions:
                    if _ra_target_name:
                        ra_rel_bonus = compute_rel_bonus(
                            relationships, factions, _ra_target_name,
                            check_context="combat",
                        )
                # T3 RomS: +1 when fighting together with romantic partner
                if actor_key in fighting_together_chars:
                    ra_rel_bonus += 1
                # Use tracked HP for target when sequential
                _ra_target_hp = action.get("target_hp_current")
                _ra_target_key = _norm_hp_track_key(_ra_target_name)
                if sequential and _ra_target_key in effective_hp:
                    _ra_target_hp = effective_hp[_ra_target_key]
                result = resolve_ranged_attack(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    weapon_type=action.get("weapon_type", "Pistol"),
                    damage_dice=action.get("damage_dice", 2),
                    rof=action.get("rof", 1),
                    target_sp=action.get("target_sp", 0),
                    range_bracket=action.get("range_bracket", 0),
                    hit_location=action.get("hit_location", "body"),
                    is_ap=action.get("is_ap", False),
                    is_rubber=action.get("is_rubber", False),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=ra_rel_bonus,
                    aimed_shot=action.get("aimed_shot"),
                    target_hp_current=_ra_target_hp,
                    target_name=_ra_target_name,
                    character_name=actor_name,
                    weapon_name=action.get("weapon_name", ""),
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                if not result.get("error"):
                    _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                            action.get("luck_spent", 0), "Luck spent on ranged attack")
                    all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "melee_attack":
                actor_name = action.get("character", "")
                actor_key = _norm_name(actor_name)
                _melee_target = action.get("target", "")
                _melee_rel_bonus = 0
                if relationships or factions:
                    if _melee_target:
                        _melee_rel_bonus = compute_rel_bonus(
                            relationships, factions, _melee_target,
                            check_context="combat",
                        )
                # T3 RomS: +1 when fighting together with romantic partner
                if actor_key in fighting_together_chars:
                    _melee_rel_bonus += 1
                result = resolve_melee_attack(
                    attacker_stat=action.get("attacker_stat", 0),
                    attacker_skill=action.get("attacker_skill", 0),
                    defender_stat=action.get("defender_stat", 0),
                    defender_skill=action.get("defender_skill", 0),
                    damage_dice=action.get("damage_dice", 2),
                    rof=action.get("rof", 1),
                    target_sp=action.get("target_sp", 0),
                    hit_location=action.get("hit_location", "body"),
                    seriously_wounded_attacker=action.get("seriously_wounded_attacker", False),
                    seriously_wounded_defender=action.get("seriously_wounded_defender", False),
                    is_brawling=action.get("is_brawling", False),
                    rel_bonus=_melee_rel_bonus,
                    target_name=_melee_target,
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "autofire":
                actor_name = action.get("character", "")
                actor_key = _norm_name(actor_name)
                _af_target = action.get("target", "")
                _af_rel_bonus = 0
                if relationships or factions:
                    if _af_target:
                        _af_rel_bonus = compute_rel_bonus(
                            relationships, factions, _af_target,
                            check_context="combat",
                        )
                # T3 RomS: +1 when fighting together with romantic partner
                if actor_key in fighting_together_chars:
                    _af_rel_bonus += 1
                result = resolve_autofire(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    weapon_type=action.get("weapon_type", "SMG"),
                    autofire_multiplier=action.get("autofire_multiplier", 3),
                    target_sp=action.get("target_sp", 0),
                    range_bracket=action.get("range_bracket", 0),
                    hit_location=action.get("hit_location", "body"),
                    is_ap=action.get("is_ap", False),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=_af_rel_bonus,
                    target_name=_af_target,
                    character_name=actor_name,
                    weapon_name=action.get("weapon_name", ""),
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                if not result.get("error"):
                    _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                            action.get("luck_spent", 0), "Luck spent on autofire")
                    all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "suppressive_fire":
                actor_name = action.get("character", "")
                actor_key = _norm_name(actor_name)
                _sf_rel_bonus = 0
                if actor_key in fighting_together_chars:
                    _sf_rel_bonus += 1
                result = resolve_suppressive_fire(
                    attacker_ref=action.get("attacker_ref", 0),
                    attacker_autofire=action.get("attacker_autofire", 0),
                    targets=action.get("targets", []),
                    seriously_wounded_attacker=action.get("seriously_wounded_attacker", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=_sf_rel_bonus,
                    character_name=actor_name,
                    weapon_name=action.get("weapon_name", ""),
                    tracked_edgerunners=_relationship_actor_names,
                    on_success=action.get("on_success", ""),
                    on_failure=action.get("on_failure", ""),
                )
                result["character"] = actor_name
                results.append(result)
                all_state_ops.extend(result.get("state_ops", []))
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on Suppressive Fire")

            elif action_type == "death_save":
                actor_name = action.get("character", "")
                actor_key = _norm_name(actor_name)
                # Auto-read death_save_count and active_injuries from edgerunner state
                _er_state = _lookup_edgerunner(edgerunner_states, actor_name) if edgerunner_states else None
                _ds_count = _er_state.get("death_save_count", 0) if _er_state else action.get("death_save_count", 0)
                _active_inj = None
                if _er_state:
                    _active_inj = [ci for ci in (_er_state.get("critical_injuries") or [])
                                   if isinstance(ci, dict) and ci.get("status") not in ("removed", "quick_fixed")]
                else:
                    _active_inj = action.get("active_injuries")
                # T2 RomS: -1 to Death Save rolls (having a T2+ romantic partner)
                _ds_roms_bonus = 1 if actor_key and actor_key == _relationship_owner_name and _romantic_partners_t2 else 0
                result = resolve_death_save(
                    body_stat=action.get("body_stat", 6),
                    death_save_count=_ds_count,
                    active_injuries=_active_inj,
                    roms_death_save_bonus=_ds_roms_bonus,
                )
                result["character"] = actor_name
                results.append(result)
                all_state_ops.append({
                    "edgerunner": actor_name,
                    "op": "death_save",
                    "reason": f"Death Save round {_ds_count + 1}",
                })
                # Auto-set "dead" condition on failed Death Save (RAW backstop)
                if not result.get("survived"):
                    all_state_ops.append({
                        "edgerunner": actor_name,
                        "op": "add_condition",
                        "condition": "dead",
                        "reason": "Failed Death Save",
                    })

            elif action_type == "opposed_check":
                actor_name = action.get("character", "")
                _opp_target = action.get("target", "")
                # Auto-compute rel_bonus from relationships
                _opp_rel = action.get("rel_bonus", 0)
                if relationships or factions:
                    if _opp_target:
                        _opp_rel = compute_rel_bonus(
                            relationships, factions, _opp_target,
                            check_context=action.get("check_context"),
                        )
                # Wellbeing Boost
                _opp_wb = 0
                _opp_wb_npc = action.get("wb_boost_used")
                if _opp_wb_npc and relationships:
                    _opp_wb_data = relationships.get(_opp_wb_npc, {})
                    if isinstance(_opp_wb_data, dict) and _opp_wb_data.get("wb_boost"):
                        _opp_wb = 1
                result = resolve_opposed_check(
                    attacker_stat=action.get("attacker_stat", 0),
                    defender_stat=action.get("defender_stat", 0),
                    attacker_label=action.get("attacker_label", "Attacker"),
                    defender_label=action.get("defender_label", "Defender"),
                    attacker_skill=action.get("attacker_skill", 0),
                    defender_skill=action.get("defender_skill", 0),
                    attacker_skill_label=action.get("attacker_skill_label", ""),
                    defender_skill_label=action.get("defender_skill_label", ""),
                    seriously_wounded_attacker=action.get("seriously_wounded_attacker", False),
                    seriously_wounded_defender=action.get("seriously_wounded_defender", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=_opp_rel,
                    wb_boost=_opp_wb,
                )
                result["character"] = actor_name
                if action.get("net"):
                    result["net"] = True
                    result["ability"] = action.get("ability")
                if _opp_wb:
                    all_state_ops.append({
                        "type": "relationship_op",
                        "op": "wb_boost_spend",
                        "target": _opp_wb_npc,
                        "reason": f"Wellbeing boost spent on opposed check by {actor_name}",
                    })
                # Zap damage: on hit, roll 1d6 (flat, per Hacking Rulebook §7)
                if action.get("zap") and result["success"]:
                    _zap_dice = 1
                    _zap_total = random.randint(1, 6)
                    result["zap_damage"] = _zap_total
                    result["zap_dice"] = _zap_dice
                    result["zap_note"] = f"Zap deals {_zap_total} damage (1d6)"
                    result["formatted"] += f" — Zap 1d6 = {_zap_total} dmg"
                    _target_ice = action.get("target", "")
                    _target_ice_key = None
                    for _key_candidate in (
                        action.get("target_ice_key"),
                        action.get("target_key"),
                        action.get("ice_key"),
                        action.get("ice_status_key"),
                    ):
                        _target_ice_key = _normalize_ice_key_candidate(_key_candidate, ice_status)
                        if _target_ice_key:
                            break
                    _target_node_hint = action.get("target_node")
                    if not _target_ice_key and _target_ice and isinstance(ice_status, dict):
                        _matching_keys = [
                            _k for _k, _v in ice_status.items()
                            if isinstance(_v, dict)
                            and _v.get("status") == "active"
                            and str(_v.get("name", "")).strip().lower() == str(_target_ice).strip().lower()
                        ]
                        if isinstance(_target_node_hint, str) and _target_node_hint:
                            _hint_matches = [
                                _k for _k in _matching_keys
                                if _k == _target_node_hint or _k.startswith(f"{_target_node_hint}_")
                            ]
                            if len(_hint_matches) == 1:
                                _target_ice_key = _hint_matches[0]
                        if len(_matching_keys) == 1:
                            _target_ice_key = _matching_keys[0]
                    if _target_ice or _target_ice_key:
                        _rez_op = {
                            "op": "rez_damage",
                            "target": _target_ice,
                            "damage": _zap_total,
                            "reason": f"Zap from {action.get('character', 'Netrunner')}",
                        }
                        if _target_ice_key:
                            _rez_op["target_key"] = _target_ice_key
                        elif isinstance(_target_node_hint, str) and _target_node_hint:
                            _rez_op["target_node"] = _target_node_hint
                        all_state_ops.append(_rez_op)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on opposed check")
                results.append(result)

            elif action_type == "program_attack":
                # Accept either "program_name" (legacy) or "program" (intent-only).
                _prog_name = action.get("program_name") or action.get("program") or "Program"
                result = resolve_program_attack(
                    interface_rank=action.get("interface_rank", 0),
                    program_atk=action.get("program_atk", 0),
                    target_def=action.get("target_def", 0),
                    program_damage_dice=action.get("program_damage_dice", 1),
                    target_rez=action.get("target_rez", 0),
                    program_name=_prog_name,
                    target_name=action.get("target", "ICE"),
                )
                result["character"] = action.get("character", "")
                # Annotate result for model visibility (incremental mode)
                if _prog_name and _prog_name != "Program":
                    result["program_deactivated"] = _prog_name
                    result["deactivation_note"] = f"{_prog_name} deactivated after attack (RAW). Costs 1 NET Action to Reactivate."
                    all_state_ops.append({
                        "op": "program_deactivate",
                        "program_name": _prog_name,
                    })
                results.append(result)

            elif action_type == "program_attack_vs_netrunner":
                # Black ICE attacking the Netrunner — damage = brain damage (HP, no armor)
                _ice_type_raw = action.get("ice_type")
                _ice_block = _lookup_ice_type(_ice_type_raw)
                # Override ATK and damage_dice from table if ICE type found
                _patk = _ice_block["atk"] if _ice_block else action.get("program_atk", 0)
                _pdmg = _ice_block["damage_dice"] if _ice_block else action.get("program_damage_dice", 1)
                result = resolve_program_attack(
                    interface_rank=0,  # ICE ATK is from stat block; Netrunner interface is not an attacker bonus.
                    program_atk=_patk,
                    target_def=action.get("target_def", 0),
                    program_damage_dice=_pdmg,
                    target_rez=action.get("target_rez", 0),
                    program_name=action.get("program_name", "Black ICE"),
                    target_name=action.get("target", "Netrunner"),
                    track_rez=False,
                    damage_kind="brain damage",
                )
                result["character"] = action.get("character", "")
                result["type"] = "program_attack_vs_netrunner"
                if _ice_block:
                    result["ice_type"] = _ice_block["name"]
                results.append(result)
                if result.get("hit"):
                    _nr_name = action.get("target", "")
                    # Brain damage (if damage_dice > 0)
                    if result.get("damage_total", 0) > 0:
                        all_state_ops.append({
                            "edgerunner": _nr_name,
                            "op": "brain_damage",
                            "change": result["damage_total"],
                            "reason": f"Black ICE {_ice_block['name'] if _ice_block else action.get('program_name', 'ICE')} attack",
                        })
                    # ICE special effect
                    if _ice_block:
                        _is_forced_jack_out = _ice_block.get("effect") == "forced_jack_out"
                        _is_movement_lock = _ice_block.get("effect") == "movement_lock"
                        _forced_jack_out_already = any(
                            isinstance(_op, dict) and _op.get("op") == "forced_jack_out"
                            for _op in all_state_ops
                        )
                        if _is_forced_jack_out and _forced_jack_out_already:
                            result["ice_effect"] = "Forced Jack Out already applied this sequence."
                            continue
                        _exclude_ice_key = None
                        for _key_candidate in (
                            action.get("ice_key"),
                            action.get("ice_status_key"),
                            action.get("source_ice_key"),
                        ):
                            _exclude_ice_key = _normalize_ice_key_candidate(_key_candidate, ice_status)
                            if _exclude_ice_key:
                                break
                        # If caller did not pass an ICE instance key, infer it only when unique.
                        _matching_keys = []
                        if not _exclude_ice_key and isinstance(ice_status, dict):
                            _matching_keys = [
                                _k for _k, _v in ice_status.items()
                                if isinstance(_v, dict)
                                and _v.get("status") == "active"
                                and str(_v.get("name", "")).strip().lower() == _ice_block["name"].lower()
                            ]
                            _source_node_hint = action.get("source_node") or action.get("ice_node") or action.get("node") or action.get("current_node")
                            if isinstance(_source_node_hint, str) and _source_node_hint:
                                _hint_matches = [
                                    _k for _k in _matching_keys
                                    if _k == _source_node_hint or _k.startswith(f"{_source_node_hint}_")
                                ]
                                if len(_hint_matches) == 1:
                                    _exclude_ice_key = _hint_matches[0]
                            if len(_matching_keys) == 1:
                                _exclude_ice_key = _matching_keys[0]
                        _requires_key_on_duplicate = _is_forced_jack_out or _is_movement_lock
                        _source_ice_key_for_effect = _exclude_ice_key
                        if _requires_key_on_duplicate and not _exclude_ice_key and len(_matching_keys) > 1:
                            # Ambiguous duplicate ICE source with no disambiguator:
                            # do not bind a guessed source key as authoritative.
                            result["ice_effect_warning"] = (
                                "Ambiguous source ICE; applying effect without instance binding."
                            )
                            _source_ice_key_for_effect = None
                        _fx = resolve_ice_effect(
                            _ice_block,
                            active_programs=active_programs,
                            installed_hardware=installed_hardware,
                            ice_status=ice_status,
                            exclude_ice=_ice_block.get("name"),
                            exclude_ice_key=_exclude_ice_key,
                            source_ice_key=_source_ice_key_for_effect,
                        )
                        # Fill in edgerunner name for cascade brain_damage ops before extending
                        _fx_ops = _fx.get("state_ops", [])
                        for _fxop in _fx_ops:
                            if isinstance(_fxop, dict) and _fxop.get("op") == "brain_damage" and not state_op_subject_name(_fxop, "edgerunner"):
                                attach_state_op_subject(_fxop, "edgerunner", _nr_name)
                        all_state_ops.extend(_fx_ops)
                        if _fx.get("formatted"):
                            result["ice_effect"] = _fx["formatted"]
                            result["formatted"] = result.get("formatted", "") + " — " + _fx["formatted"]
                        result["ice_effect_ops"] = _fx.get("state_ops", [])

            elif action_type == "ice_attack_vs_program":
                # Anti-program ICE (Dragon/Killer/Sabertooth) attacking a program
                _ice_type_raw = action.get("ice_type")
                _ice_block = _lookup_ice_type(_ice_type_raw)
                _iap_atk = _ice_block["atk"] if _ice_block else action.get("program_atk", 6)
                _iap_dmg_dice = _ice_block["damage_dice"] if _ice_block else action.get("program_damage_dice", 4)
                _target_prog = action.get("target_program", "Program")
                _target_def = action.get("target_program_def", 0)
                _target_rez = action.get("target_program_rez", 0)
                result = resolve_program_attack(
                    interface_rank=0,  # ICE doesn't use interface rank
                    program_atk=_iap_atk,
                    target_def=_target_def,
                    program_damage_dice=_iap_dmg_dice,
                    target_rez=_target_rez,
                    program_name=_ice_block["name"] if _ice_block else action.get("character", "ICE"),
                    target_name=_target_prog,
                )
                result["character"] = action.get("character", "")
                result["type"] = "ice_attack_vs_program"
                if _ice_block:
                    result["ice_type"] = _ice_block["name"]
                # Use actual current REZ from active_programs (may differ from model value if partially damaged)
                _actual_rez = _target_rez
                if isinstance(active_programs, list):
                    for _p in active_programs:
                        if isinstance(_p, dict) and _p.get("name") == _target_prog:
                            _actual_rez = int(_p.get("rez", _target_rez))
                            break
                _destroyed = result.get("hit") and result.get("damage_total", 0) >= _actual_rez and _actual_rez > 0
                if result.get("hit") and result.get("damage_total", 0) > 0:
                    all_state_ops.append({
                        "op": "program_rez_damage",
                        "program_name": _target_prog,
                        "damage": result["damage_total"],
                        "destroyed": _destroyed,
                        "source": _ice_block["name"] if _ice_block else action.get("character", "ICE"),
                    })
                    if _destroyed:
                        result["program_destroyed"] = True
                        result["formatted"] = result.get("formatted", "") + f" — {_target_prog} DESTROYED!"
                results.append(result)

            elif action_type == "ambush":
                # Stealth vs Perception opposed checks for each target
                _ambush_results = []
                _ambusher = action.get("character", "")
                _stealth_stat = action.get("stealth_stat", 0)
                _stealth_skill = action.get("stealth_skill", 0)
                for _tgt in action.get("targets", []):
                    _tgt_name = _tgt.get("name", "")
                    _perc_stat = _tgt.get("perception_stat", 0)
                    _perc_skill = _tgt.get("perception_skill", 0)
                    _opp = resolve_opposed_check(
                        attacker_stat=_stealth_stat,
                        defender_stat=_perc_stat,
                        attacker_label="DEX",
                        defender_label="INT",
                        attacker_skill=_stealth_skill,
                        defender_skill=_perc_skill,
                        attacker_skill_label="Stealth",
                        defender_skill_label="Concentration",
                    )
                    _ambush_results.append({
                        "target": _tgt_name,
                        "surprised": _opp["success"],
                        "attacker_total": _opp["attacker_total"],
                        "defender_total": _opp["defender_total"],
                        "formatted": _opp["formatted"],
                    })
                results.append({
                    "type": "ambush",
                    "character": _ambusher,
                    "results": _ambush_results,
                })

            elif action_type == "initiative":
                result = resolve_initiative(
                    action.get("combatants", []),
                    surprised=action.get("surprised"),
                )
                results.append({"type": "initiative", "order": result})

            elif action_type == "driving_check":
                actor_name = action.get("character", "")
                result = resolve_driving_check(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    maneuver=action.get("maneuver", "maintain_control"),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on driving check")
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "ramming":
                actor_name = action.get("character", "")
                _ram_vname = action.get("vehicle_name", "")
                _ram_tname = action.get("target", "")
                _ram_tis_v = action.get("target_is_vehicle", False)
                # Use sequential-tracked values when available
                _ram_vkey = _norm_vehicle_track_key(_ram_vname)
                _ram_tkey = _norm_vehicle_track_key(_ram_tname)
                if sequential and vehicle_tracking_enabled:
                    _ram_vsdp = effective_vehicle_sdp.get(_ram_vkey, action.get("vehicle_sdp_current"))
                    _ram_vsp = effective_vehicle_sdp.get(_norm_vehicle_track_key(_ram_vname, sp=True), action.get("vehicle_sp", 0))
                    _ram_tsdp = effective_vehicle_sdp.get(_ram_tkey, action.get("target_sdp_current")) if _ram_tis_v else action.get("target_sdp_current")
                    _ram_tsp = effective_vehicle_sdp.get(_norm_vehicle_track_key(_ram_tname, sp=True), action.get("target_sp", 0)) if _ram_tis_v else action.get("target_sp", 0)
                else:
                    _ram_vsdp = action.get("vehicle_sdp_current")
                    _ram_vsp = action.get("vehicle_sp", 0)
                    _ram_tsdp = action.get("target_sdp_current") if _ram_tis_v else action.get("target_sdp_current")
                    _ram_tsp = action.get("target_sp", 0)
                _ram_tname_key = _norm_hp_track_key(_ram_tname)
                if sequential and not _ram_tis_v and _ram_tname_key in effective_armor_sp:
                    _ram_tsp = effective_armor_sp[_ram_tname_key]
                _ram_thp = effective_hp.get(_ram_tname_key, action.get("target_hp_current")) if not _ram_tis_v else action.get("target_hp_current")
                result = resolve_ramming(
                    vehicle_name=_ram_vname,
                    target_name=_ram_tname,
                    vehicle_sdp_current=_ram_vsdp,
                    vehicle_sp=_ram_vsp,
                    target_hp_current=_ram_thp,
                    target_sp=_ram_tsp,
                    target_is_vehicle=_ram_tis_v,
                    target_sdp_current=_ram_tsdp,
                    occupants=action.get("occupants", []),
                    target_occupants=action.get("target_occupants", []),
                    pedestrian_dodge=action.get("pedestrian_dodge", False),
                    pedestrian_dex=action.get("pedestrian_dex", 0),
                    pedestrian_evasion=action.get("pedestrian_evasion", 0),
                    seriously_wounded_pedestrian=action.get("seriously_wounded_pedestrian", False),
                    combat_plow=action.get("combat_plow", False),
                    nos_boosted=action.get("nos_boosted", False),
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "vehicle_weak_point":
                actor_name = action.get("character", "")
                _wp_vname = action.get("vehicle_name", "")
                # Use sequential-tracked SP when available
                _wp_vsp = (
                    effective_vehicle_sdp.get(_norm_vehicle_track_key(_wp_vname, sp=True), action.get("vehicle_sp", 0))
                    if (sequential and vehicle_tracking_enabled)
                    else action.get("vehicle_sp", 0)
                )
                result = resolve_vehicle_weak_point(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    weapon_type=action.get("weapon_type", "Pistol"),
                    damage_dice=action.get("damage_dice", 2),
                    vehicle_sp=_wp_vsp,
                    vehicle_name=_wp_vname,
                    range_bracket=action.get("range_bracket", 0),
                    target_moving=action.get("target_moving", True),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    is_ap=action.get("is_ap", False),
                    weapon_name=action.get("weapon_name", ""),
                    character_name=actor_name,
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on vehicle weak point shot")
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "spike_strip":
                _ss_vname = action.get("target_vehicle_name", "")
                # Use sequential-tracked SP when available
                _ss_vsp = (
                    effective_vehicle_sdp.get(_norm_vehicle_track_key(_ss_vname, sp=True), action.get("target_vehicle_sp", 0))
                    if (sequential and vehicle_tracking_enabled)
                    else action.get("target_vehicle_sp", 0)
                )
                result = resolve_spike_strip(
                    target_driver_name=action.get("target_driver", ""),
                    stat_value=action.get("target_stat_value", 0),
                    skill_value=action.get("target_skill_value", 0),
                    target_vehicle_name=_ss_vname,
                    target_vehicle_sp=_ss_vsp,
                    target_vehicle_type=action.get("target_vehicle_type", "land"),
                    seriously_wounded=action.get("seriously_wounded_target", False),
                    on_hit=action.get("on_hit", ""),
                    on_miss=action.get("on_miss", ""),
                )
                result["character"] = action.get("character", "")
                results.append(result)
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "hustle":
                actor_name = action.get("character", "")
                result = resolve_hustle(
                    role=action.get("role", ""),
                    role_ability_rank=action.get("role_ability_rank", 0),
                    dv=action.get("dv", 13),
                    payout=action.get("payout", 0),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    character=actor_name,
                    on_success=action.get("on_success", ""),
                    on_failure=action.get("on_failure", ""),
                )
                result["type"] = "hustle"
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on Hustle")
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "find_item":
                actor_name = action.get("character", "")
                result = resolve_find_item(
                    rank=action.get("rank", 0),
                    price_category=action.get("price_category", "Costly"),
                    item_name=action.get("item_name", ""),
                    character=actor_name,
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    on_success=action.get("on_success", ""),
                    on_failure=action.get("on_failure", ""),
                )
                result["type"] = "find_item"
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on Find Item")
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "haggle":
                actor_name = action.get("character", "")
                result = resolve_haggle(
                    buyer_cool=action.get("buyer_cool", 0),
                    buyer_trading=action.get("buyer_trading", 0),
                    vendor_cool=action.get("vendor_cool", 0),
                    vendor_trading=action.get("vendor_trading", 0),
                    operator_rank=action.get("operator_rank", 0),
                    item_name=action.get("item_name", ""),
                    item_price=action.get("item_price", 0),
                    character=actor_name,
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    on_success=action.get("on_success", ""),
                    on_failure=action.get("on_failure", ""),
                )
                result["type"] = "haggle"
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on Haggle")
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "facedown":
                actor_name = action.get("character", "")
                _fd_target = action.get("target", "")
                _fd_rel_bonus = action.get("rel_bonus", 0)
                if relationships or factions:
                    if _fd_target:
                        _fd_rel_bonus = compute_rel_bonus(
                            relationships, factions, _fd_target,
                            check_context="intimidation",
                        )
                result = resolve_facedown(
                    initiator_cool=action.get("initiator_cool", 0),
                    initiator_rep=action.get("initiator_rep", 0),
                    opponent_cool=action.get("opponent_cool", 0),
                    opponent_rep=action.get("opponent_rep", 0),
                    character=actor_name,
                    target=_fd_target,
                    seriously_wounded_initiator=action.get("seriously_wounded_initiator", False),
                    seriously_wounded_opponent=action.get("seriously_wounded_opponent", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=_fd_rel_bonus,
                    on_success=action.get("on_success", ""),
                    on_failure=action.get("on_failure", ""),
                )
                result["type"] = "facedown"
                result["character"] = actor_name
                results.append(result)
                _emit_luck_op_if_rolled(all_state_ops, result, actor_name,
                                        action.get("luck_spent", 0), "Luck spent on Facedown")

            else:
                results.append({"type": action_type, "error": f"Unknown action type: {action_type}"})

            # Sequential HP tracking: update effective_hp from new state_ops
            if sequential:
                for _op in all_state_ops[_ops_before:]:
                    if not isinstance(_op, dict):
                        continue
                    _op_type = _op.get("op")
                    if _op_type == "hp" and effective_hp:
                        _t = _norm_hp_track_key(state_op_subject_name(_op, "edgerunner"))
                        if _t in effective_hp:
                            effective_hp[_t] = max(0, effective_hp[_t] + int(_op.get("change", 0)))
                    elif _op_type == "vehicle_sdp" and vehicle_tracking_enabled:
                        _vn = _norm_vehicle_track_key(state_op_subject_name(_op, "vehicle"))
                        if _vn in effective_vehicle_sdp and isinstance(effective_vehicle_sdp[_vn], int):
                            effective_vehicle_sdp[_vn] = max(0, effective_vehicle_sdp[_vn] + int(_op.get("change", 0)))
                    elif _op_type == "vehicle_sp" and vehicle_tracking_enabled:
                        _vehicle_name = state_op_subject_name(_op, "vehicle")
                        _vn = _norm_vehicle_track_key(_vehicle_name)
                        _sp_key = _norm_vehicle_track_key(_vehicle_name, sp=True)
                        if _sp_key in effective_vehicle_sdp:
                            effective_vehicle_sdp[_sp_key] = max(0, effective_vehicle_sdp[_sp_key] + int(_op.get("change", 0)))
                    elif _op_type == "armor" and effective_armor_sp:
                        _t = _norm_hp_track_key(state_op_subject_name(_op, "edgerunner"))
                        if _t in effective_armor_sp:
                            try:
                                effective_armor_sp[_t] = max(0, int(effective_armor_sp[_t]) + int(_op.get("change", 0)))
                            except (TypeError, ValueError, OverflowError):
                                continue

        except Exception as e:
            _char = action.get("character", "") if isinstance(action, dict) else ""
            logger.warning(f"resolve_actions: error resolving {action_type}: {e}")
            results.append({"type": action_type, "error": str(e), "character": _char})

    return {"results": results, "state_ops": all_state_ops, "tar_consumed": _tar_consumed}


# ---------------------------------------------------------------------------
# Tool definition for single-agent mode (Phase 3)
# ---------------------------------------------------------------------------

RESOLVE_MECHANICS_TOOL = {
    "name": "resolve_mechanics",
    "description": (
        "Resolve all mechanical actions for this turn. Call BEFORE writing narrative. "
        "Each action in the array is resolved deterministically with real dice rolls. "
        "Results include formatted roll strings for your 🎲 lines and state_ops for your edgerunner_ops.\n\n"
        "Action types:\n"
        "- skill_check: {type, character, stat_value, skill_value, dv, seriously_wounded?, luck_spent?, target?, check_context? (social/persuasion/combat/perception), "
        "net?: true (set on NET-context Interface checks), ability? (REQUIRED when net=true; one of: Backdoor/Cloak/Control/Eye-Dee/Pathfinder/Slide/Virus/Zap/Initiative — tags which Interface Ability is rolling so program effect bonuses apply correctly)}\n"
        "- ranged_attack: {type, character, stat_value, skill_value, weapon_type (Pistol/SMG/Shotgun/Assault Rifle/Sniper Rifle/Bows & Crossbow/Grenade Launcher/Rocket Launcher), damage_dice, rof, "
        "target, target_sp, range_bracket (0=0-6m,1=7-12m,2=13-25m,3=26-50m,4=51-100m,5=101-200m,6=201-400m,7=401-800m), "
        "hit_location (head/body), is_ap?, is_rubber?, seriously_wounded?, luck_spent?, aimed_shot? (head/leg/held_item), weapon_name?, on_hit?, on_miss?}\n"
        "- melee_attack: {type, character, attacker_stat, attacker_skill, defender_stat, defender_skill, "
        "damage_dice, rof, target, target_sp, hit_location, seriously_wounded_attacker?, seriously_wounded_defender?, "
        "is_brawling?, on_hit?, on_miss?}\n"
        "- autofire: {type, character, stat_value, skill_value, weapon_type (SMG/Assault Rifle), autofire_multiplier (3 SMG, 4 AR), "
        "target, target_sp, range_bracket (0-4 only, max 51-100m), hit_location, is_ap?, seriously_wounded?, luck_spent?, weapon_name?, on_hit?, on_miss?}\n"
        "- death_save: {type, character, body_stat}\n"
        "- initiative: {type, character: 'all', combatants: [{name, ref}]}\n"
        "- opposed_check: {type, character, attacker_stat, attacker_skill?, defender_stat, defender_skill?, "
        "attacker_label? (stat name), defender_label? (stat name), attacker_skill_label?, defender_skill_label?, "
        "seriously_wounded_attacker?, seriously_wounded_defender?, luck_spent?, target? (NPC name for rel bonus), "
        "check_context? (social/persuasion/combat/perception), "
        "net?: true (set on NET-context contests), ability? (REQUIRED when net=true; one of: Backdoor/Cloak/Control/Eye-Dee/Pathfinder/Slide/Virus/Zap/Initiative)} "
        "— for contested rolls (Stealth vs Concentration, Persuasion vs Concentration, etc.) and NET opposed checks (Zap/Slide with zap?: true, interface_rank?: N)\n"
        "- program_attack: {type, character, interface_rank, program_atk, target_def, program_damage_dice, target_rez, program_name?, target (ICE name)}\n"
        "- program_attack_vs_netrunner: {type, character (ICE name), ice_type (e.g. 'Hellhound'), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)}. Backend auto-reads ATK/damage from ICE table.\n"
        "- ice_attack_vs_program: {type, character (ICE name), ice_type (e.g. 'Dragon'), target_program, target_program_def, target_program_rez}. Backend auto-reads ATK/damage from ICE table.\n"
        "- ambush: {type, character, stealth_stat, stealth_skill, targets: [{name, perception_stat, perception_skill}]}\n"
        "- driving_check: {type, character, vehicle_name, stat_value (REF), skill_value, maneuver (maintain_control/swerve/sharp_turn/emergency_stop/bootleg_turn/do_a_jump/landing/aerobatic_maneuver), seriously_wounded?, luck_spent?, on_hit?, on_miss?}\n"
        "- ramming: {type, character (driver), vehicle_name, target, vehicle_sdp_current, vehicle_sp, target_hp_current, target_sp, target_is_vehicle?, target_sdp_current?, occupants: [{name}], target_occupants?: [{name}], pedestrian_dodge?, pedestrian_dex? (DEX stat), pedestrian_evasion? (Evasion skill), seriously_wounded_pedestrian?, combat_plow?, nos_boosted?, on_hit?, on_miss?}\n"
        "- vehicle_weak_point: {type, character, stat_value, skill_value, weapon_type, damage_dice, vehicle_sp, vehicle_name, range_bracket, target_moving?, seriously_wounded?, luck_spent?, is_ap?, weapon_name?, on_hit?, on_miss?}\n"
        "- spike_strip: {type, character (deployer), target_driver, target_stat_value (REF), target_skill_value (Drive Land Vehicle), target_vehicle_name, target_vehicle_sp, target_vehicle_type (land only), seriously_wounded_target?, on_hit?, on_miss?}\n"
        "- hustle: {type, character, role (e.g. 'Fixer'/'Solo'), role_ability_rank, dv, payout (eurobucks on success), seriously_wounded?, luck_spent?, on_success?, on_failure?} — Downtime income: d10 + Role Ability Rank vs DV. Resolver auto-emits eurobucks state_op on success. Do NOT emit a separate eurobucks edgerunner_op. Update character_states to reflect the new funds.\n"
        "- find_item: {type, character, rank (Fixer Operator rank or Streetwise skill), price_category (Cheap/Everyday/Costly/Premium/Expensive/Very Expensive/Luxury/Super Luxury), item_name, seriously_wounded?, luck_spent?, on_success?, on_failure?} — Night market availability: d10 + rank vs DV by price category. Auto-succeeds for Cheap/Everyday. Backend resolves the availability roll.\n"
        "- haggle: {type, character, buyer_cool, buyer_trading, vendor_cool, vendor_trading, operator_rank (Fixer's Operator Role Ability rank, REQUIRED — 0 means not a Fixer → resolver fails soft with no roll and no eurobucks deducted), item_name, item_price, seriously_wounded?, luck_spent?, on_success?, on_failure?} — RAW CRB p.160: haggling a listed market price is exclusive to the Fixer's Operator Role Ability. Roll: d10 + COOL + Trading + Operator Rank vs d10 + vendor COOL + vendor Trading. Discount on success is FIXED by rank (1-8: 10% / 9+: 20%), NOT a sliding scale. On success, resolver auto-emits eurobucks state_op (discounted price). On failure, auto-emits full price. Do NOT emit a separate eurobucks edgerunner_op. For non-Fixer bargaining (bartering, service negotiation, non-listed goods, resisting a Fixer's haggle per RAW p.140), use a plain skill_check with Trading instead — NOT haggle.\n"
        "- facedown: {type, character, target (opponent name), initiator_cool, initiator_rep (Rep level, 0 if none), opponent_cool, opponent_rep, seriously_wounded_initiator?, seriously_wounded_opponent?, luck_spent?, on_success?, on_failure?} — Facedown (CRB p.195): COOL + Reputation + d10 vs same. Tie = stalemate (nothing happens). Winner/loser: loser must back down or take -2 to all actions vs winner until defeated. Result includes tie, winner, loser, penalty_condition fields.\n"
        "- suppressive_fire: {type, character, attacker_ref, attacker_autofire, targets: [{name, will, concentration, seriously_wounded?}], seriously_wounded_attacker?, luck_spent?, weapon_name?, on_success?, on_failure?} — Suppressive Fire (p.174): Attacker rolls d10+REF+Autofire once. Each target rolls d10+WILL+Concentration. Targets who fail are suppressed (must stay in cover). Ties favor defender. Consumes 10 rounds. No damage dealt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {
                "type": "array",
                "description": "Array of mechanical actions to resolve this turn.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["skill_check", "ranged_attack", "melee_attack",
                                     "autofire", "death_save", "initiative",
                                     "opposed_check", "program_attack",
                                     "program_attack_vs_netrunner",
                                     "ice_attack_vs_program", "ambush",
                                     "driving_check", "ramming",
                                     "vehicle_weak_point", "spike_strip",
                                     "hustle", "find_item", "haggle", "facedown",
                                     "suppressive_fire"],
                        },
                        "character": {"type": "string"},
                    },
                    "required": ["type", "character"],
                },
            },
        },
        "required": ["actions"],
    },
}
