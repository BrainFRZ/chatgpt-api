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

from .cpred_tables import (
    CRIT_INJURY_BODY,
    CRIT_INJURY_HEAD,
    RANGED_DV_TABLE,
    AUTOFIRE_DV_TABLE,
    AIMED_SHOT_DV_PENALTY,
    ICE_STAT_BLOCKS,
)

logger = logging.getLogger(__name__)

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
    return {
        "edgerunner": edgerunner,
        "op": "critical_injury",
        "action": "add",
        "name": injury.get("name", ""),
        "effect": injury.get("effect", ""),
        "dv_mod": int(injury.get("dv_mod", 0)),
        "location": injury.get("location", "body"),
        "reason": reason,
    }


def _luck_spend_state_op(edgerunner: str, luck_spent: int, reason: str) -> Optional[dict]:
    """Emit a Luck spend op for positive Luck usage."""
    spend = max(0, int(luck_spent))
    if spend <= 0:
        return None
    return {
        "edgerunner": edgerunner,
        "op": "luck",
        "change": -spend,
        "reason": reason,
    }


def _iter_critical_injuries(damage: dict) -> list:
    """Normalize damage result critical injuries to a list."""
    injuries = damage.get("critical_injuries")
    if isinstance(injuries, list):
        return [ci for ci in injuries if isinstance(ci, dict)]
    injury = damage.get("crit_injury")
    return [injury] if isinstance(injury, dict) else []


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
    clamped_luck = max(0, luck_spent)
    clamped_rel = max(-5, min(5, rel_bonus))

    if clamped_luck > 0:
        total += clamped_luck
        modifiers.append(("Luck", clamped_luck))
    if clamped_rel != 0:
        total += clamped_rel
        modifiers.append(("RS", clamped_rel))

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
) -> dict:
    """Resolve an opposed check: both sides roll d10 + stat.

    Handles exploding 10s and fumble 1s. Ties go to defender.
    Returns dict with full breakdown and formatted string.

    Used for: Zap vs ICE DEF, Slide vs ICE PER, Program ATK vs ICE DEF.
    """
    atk_die = _roll_check_die()
    def_die = _roll_check_die()
    atk_total = atk_die["total"] + attacker_stat
    def_total = def_die["total"] + defender_stat
    success = atk_total > def_total  # ties go to defender
    margin = atk_total - def_total

    atk_fmt = f"{_format_die(atk_die)} +{attacker_label} {attacker_stat} = {atk_total}"
    def_fmt = f"{_format_die(def_die)} +{defender_label} {defender_stat} = {def_total}"
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
) -> dict:
    """Resolve a Death Save: d10 + count + injury mods vs BODY.

    Natural 10 always fails. Must roll UNDER BODY to survive.
    """
    d10 = _roll_d10()
    injury_mod = 0
    for ci in (active_injuries or []):
        mod = ci.get("dv_mod", 0) if isinstance(ci, dict) else 0
        injury_mod += mod if isinstance(mod, (int, float)) else 0
    effective_roll = d10 + death_save_count + injury_mod
    natural_10 = d10 == 10
    survived = not natural_10 and effective_roll < body_stat

    # Build formatted string
    parts = [f"d10[**{d10}**]"]
    if death_save_count > 0:
        parts.append(f"+cumulative {death_save_count}")
    if injury_mod > 0:
        parts.append(f"+injuries {injury_mod}")
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

    if effect == "program_destroy":
        targets_defender = ice_block.get("targets_defender", False)
        candidates = []
        if isinstance(active_programs, list):
            for p in active_programs:
                # Anti-program ICE can only destroy rezzed programs.
                if isinstance(p, dict) and p.get("status") == "active":
                    if targets_defender and p.get("category") != "defender":
                        continue
                    candidates.append(p.get("name", "Unknown"))
        if candidates:
            chosen = random.choice(candidates)
            state_ops.append({"op": "program_destroy", "program_name": chosen,
                              "source": name})
            cat_note = " (Defender)" if targets_defender else ""
            formatted_parts.append(f"{name} destroys {chosen}{cat_note}!")
        else:
            cat_note = " Defender" if targets_defender else ""
            formatted_parts.append(f"{name} finds no{cat_note} programs to destroy.")
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
# resolve_actions — batch resolver
# ---------------------------------------------------------------------------

def resolve_actions(actions: list, relationships: dict = None, factions: dict = None,
                    sequential: bool = True, combatant_hp: dict = None,
                    tar_stacks: int = 0, alert_level: int = 0,
                    active_programs=None, installed_hardware=None,
                    ice_status=None) -> dict:
    """Resolve a batch of mechanical actions.

    Each action is a dict with "type" and type-specific fields.
    When relationships/factions are provided, rel_bonus is auto-computed
    from the target's tier for actions that include a target and check_context.

    When sequential=True and combatant_hp is provided, tracks running HP:
    - After each action, subtract hp_damage from target's effective_hp
    - Before each action, check if actor's effective_hp <= 0 → skip (eliminated)
    - Pass current target HP to sub-resolvers for accurate rubber ammo capping

    Returns {"results": [...], "state_ops": [...]}.
    """
    from .cpred import compute_rel_bonus

    results = []
    all_state_ops = []
    effective_hp = dict(combatant_hp) if combatant_hp else {}
    _tar_consumed = False

    for action in actions:
        action_type = None
        _ops_before = len(all_state_ops)
        try:
            action_type = action.get("type") if isinstance(action, dict) else None

            # Sequential elimination: skip actors at 0 HP
            if sequential and effective_hp and action_type not in ("initiative", "ambush"):
                actor_name = action.get("character", "") if isinstance(action, dict) else ""
                if actor_name and actor_name in effective_hp and effective_hp[actor_name] <= 0:
                    results.append({
                        "type": action_type, "character": actor_name,
                        "skipped": True, "reason": "eliminated",
                    })
                    continue

            # Copy action before mutation to avoid modifying caller's dict
            if isinstance(action, dict) and (tar_stacks > 0 or alert_level >= 3) and action.get("net"):
                action = dict(action)

            # Apply TAR penalty to next NET check only (auto-enforced by backend)
            if isinstance(action, dict) and tar_stacks > 0 and action.get("net") and not _tar_consumed:
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
            if isinstance(action, dict) and alert_level >= 3 and action.get("net") and action_type == "skill_check":
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
                result = resolve_check(
                    stat_value=action.get("stat_value", 0),
                    skill_value=action.get("skill_value", 0),
                    dv=action.get("dv", 13),
                    seriously_wounded=action.get("seriously_wounded", False),
                    luck_spent=action.get("luck_spent", 0),
                    rel_bonus=rel_bonus,
                )
                result["type"] = "skill_check"
                result["character"] = actor_name
                results.append(result)
                luck_op = _luck_spend_state_op(
                    edgerunner=actor_name,
                    luck_spent=action.get("luck_spent", 0),
                    reason="Luck spent on skill check",
                )
                if luck_op:
                    all_state_ops.append(luck_op)

            elif action_type == "ranged_attack":
                actor_name = action.get("character", "")
                _ra_target_name = action.get("target", "")
                # Auto-compute rel_bonus from relationships when target is provided
                ra_rel_bonus = action.get("rel_bonus", 0)
                if relationships or factions:
                    if _ra_target_name:
                        ra_rel_bonus = compute_rel_bonus(
                            relationships, factions, _ra_target_name,
                            check_context="combat",
                        )
                # Use tracked HP for target when sequential
                _ra_target_hp = action.get("target_hp_current")
                if sequential and _ra_target_name in effective_hp:
                    _ra_target_hp = effective_hp[_ra_target_name]
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
                    luck_op = _luck_spend_state_op(
                        edgerunner=actor_name,
                        luck_spent=action.get("luck_spent", 0),
                        reason="Luck spent on ranged attack",
                    )
                    if luck_op:
                        all_state_ops.append(luck_op)
                    all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "melee_attack":
                _melee_target = action.get("target", "")
                _melee_rel_bonus = 0
                if relationships or factions:
                    if _melee_target:
                        _melee_rel_bonus = compute_rel_bonus(
                            relationships, factions, _melee_target,
                            check_context="combat",
                        )
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
                result["character"] = action.get("character", "")
                results.append(result)
                all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "autofire":
                actor_name = action.get("character", "")
                _af_target = action.get("target", "")
                _af_rel_bonus = 0
                if relationships or factions:
                    if _af_target:
                        _af_rel_bonus = compute_rel_bonus(
                            relationships, factions, _af_target,
                            check_context="combat",
                        )
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
                    luck_op = _luck_spend_state_op(
                        edgerunner=actor_name,
                        luck_spent=action.get("luck_spent", 0),
                        reason="Luck spent on autofire",
                    )
                    if luck_op:
                        all_state_ops.append(luck_op)
                    all_state_ops.extend(result.get("state_ops", []))

            elif action_type == "death_save":
                actor_name = action.get("character", "")
                result = resolve_death_save(
                    body_stat=action.get("body_stat", 6),
                    death_save_count=action.get("death_save_count", 0),
                    active_injuries=action.get("active_injuries"),
                )
                result["character"] = actor_name
                results.append(result)
                all_state_ops.append({
                    "edgerunner": actor_name,
                    "op": "death_save",
                    "reason": f"Death Save round {int(action.get('death_save_count', 0)) + 1}",
                })

            elif action_type == "opposed_check":
                result = resolve_opposed_check(
                    attacker_stat=action.get("attacker_stat", 0),
                    defender_stat=action.get("defender_stat", 0),
                    attacker_label=action.get("attacker_label", "Attacker"),
                    defender_label=action.get("defender_label", "Defender"),
                )
                result["character"] = action.get("character", "")
                # Zap damage: on hit, roll Interface rank d6 for REZ damage
                if action.get("zap") and result["success"]:
                    _zap_dice = max(1, action.get("interface_rank", 1))
                    _zap_total = sum(random.randint(1, 6) for _ in range(_zap_dice))
                    result["zap_damage"] = _zap_total
                    result["zap_dice"] = _zap_dice
                    result["zap_note"] = f"Zap deals {_zap_total} REZ damage ({_zap_dice}d6)"
                    result["formatted"] += f" — Zap {_zap_dice}d6 = {_zap_total} REZ dmg"
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
                results.append(result)

            elif action_type == "program_attack":
                result = resolve_program_attack(
                    interface_rank=action.get("interface_rank", 0),
                    program_atk=action.get("program_atk", 0),
                    target_def=action.get("target_def", 0),
                    program_damage_dice=action.get("program_damage_dice", 1),
                    target_rez=action.get("target_rez", 0),
                    program_name=action.get("program_name", "Program"),
                    target_name=action.get("target", "ICE"),
                )
                result["character"] = action.get("character", "")
                # Annotate result for model visibility (incremental mode)
                _prog_name = action.get("program_name", "")
                if _prog_name:
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
                            if isinstance(_fxop, dict) and _fxop.get("op") == "brain_damage" and not _fxop.get("edgerunner"):
                                _fxop["edgerunner"] = _nr_name
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
                        attacker_stat=_stealth_stat + _stealth_skill,
                        defender_stat=_perc_stat + _perc_skill,
                        attacker_label="Stealth",
                        defender_label="Perception",
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

            else:
                results.append({"type": action_type, "error": f"Unknown action type: {action_type}"})

            # Sequential HP tracking: update effective_hp from new state_ops
            if sequential and effective_hp:
                for _op in all_state_ops[_ops_before:]:
                    if isinstance(_op, dict) and _op.get("op") == "hp":
                        _t = _op.get("edgerunner", "")
                        if _t in effective_hp:
                            effective_hp[_t] = max(0, effective_hp[_t] + int(_op.get("change", 0)))

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
        "- skill_check: {type, character, stat_value, skill_value, dv, seriously_wounded?, luck_spent?, target?, check_context? (social/persuasion/combat/perception)}\n"
        "- ranged_attack: {type, character, stat_value, skill_value, weapon_type (Pistol/SMG/Shotgun/Assault Rifle/Sniper Rifle/Bows & Crossbow/Grenade Launcher/Rocket Launcher), damage_dice, rof, "
        "target, target_sp, range_bracket (0=0-6m,1=7-12m,2=13-25m,3=26-50m,4=51-100m,5=101-200m,6=201-400m,7=401-800m), "
        "hit_location (head/body), is_ap?, is_rubber?, seriously_wounded?, luck_spent?, aimed_shot? (head/leg/held_item), weapon_name?, on_hit?, on_miss?}\n"
        "- melee_attack: {type, character, attacker_stat, attacker_skill, defender_stat, defender_skill, "
        "damage_dice, rof, target, target_sp, hit_location, seriously_wounded_attacker?, seriously_wounded_defender?, "
        "is_brawling?, on_hit?, on_miss?}\n"
        "- autofire: {type, character, stat_value, skill_value, weapon_type (SMG/Assault Rifle), autofire_multiplier (3 SMG, 4 AR), "
        "target, target_sp, range_bracket (0-4 only, max 51-100m), hit_location, is_ap?, seriously_wounded?, luck_spent?, weapon_name?, on_hit?, on_miss?}\n"
        "- death_save: {type, character, body_stat, death_save_count, active_injuries? (array of {dv_mod})}\n"
        "- initiative: {type, character: 'all', combatants: [{name, ref}]}\n"
        "- opposed_check: {type, character, attacker_stat, defender_stat, attacker_label?, defender_label?}\n"
        "- program_attack: {type, character, interface_rank, program_atk, target_def, program_damage_dice, target_rez, program_name?, target (ICE name)}\n"
        "- program_attack_vs_netrunner: {type, character (ICE name), ice_type (e.g. 'Hellhound'), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)}. Backend auto-reads ATK/damage from ICE table.\n"
        "- ice_attack_vs_program: {type, character (ICE name), ice_type (e.g. 'Dragon'), target_program, target_program_def, target_program_rez}. Backend auto-reads ATK/damage from ICE table.\n"
        "- ambush: {type, character, stealth_stat, stealth_skill, targets: [{name, perception_stat, perception_skill}]}"
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
                                     "ice_attack_vs_program", "ambush"],
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
