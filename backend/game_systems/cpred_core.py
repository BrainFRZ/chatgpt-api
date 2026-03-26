"""
CPRED core state — edgerunner state, expenses, relationships, IP tracker, and shared helpers.

Contains init_game_state, apply_game_state, build_game_injection and all their
supporting helpers. Shared utility functions (_update_seriously_wounded, _wound_flag,
compute_rel_bonus) are imported by other cpred_* modules.
"""

import copy
import logging
import random
import re
from datetime import date, timedelta

from .cpred_identity import state_op_subject
from .cpred_tables import CYBERWARE_TABLE


logger = logging.getLogger(__name__)


def _safe_int(val, default=0):
    """Coerce val to int, returning default on failure."""
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


def _render_transition(context):
    """Return transition context lines if truthy, else empty list."""
    if context:
        return [f"[TRANSITION] {context} [/TRANSITION]", ""]
    return []


# Pre-built lowercase lookup for cyberware ceiling costs
_CYBERWARE_CEILING_LOOKUP = {k.lower(): v.get("ceiling_cost", 2) for k, v in CYBERWARE_TABLE.items()}

# Aliases for common name variants the model may use
_CYBERWARE_ALIASES = {
    "cyberaudio": "cyberaudio suite",
    "cybereyes": "cybereye",
    "cyberarms": "cyberarm",
    "cyberlegs": "cyberleg",
}


def _normalize_cyberware_name(value: str) -> str:
    """Return a canonical lowercase cyberware key for costing/removal matching."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    if not normalized:
        return ""

    base = re.sub(r'\s*\(.*?\)\s*$', '', normalized).strip()
    alias = _CYBERWARE_ALIASES.get(base) or _CYBERWARE_ALIASES.get(normalized)
    if alias:
        return alias

    if base in _CYBERWARE_CEILING_LOOKUP:
        return base
    if normalized in _CYBERWARE_CEILING_LOOKUP:
        return normalized
    return base or normalized


def _normalize_cyberware_entry_name(value: str) -> str:
    """Return canonical entry identity for add de-duplication.

    Parenthetical qualifiers are preserved so distinct instances like
    "(Left)" and "(Right)" can coexist, while aliases/casing collapse.
    """
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    if not normalized:
        return ""

    qualifier = ""
    match = re.search(r'\s*(\(.*?\))\s*$', normalized)
    if match:
        qualifier = f" {match.group(1).strip()}"
    base = re.sub(r'\s*\(.*?\)\s*$', '', normalized).strip()
    if not base:
        return ""

    aliased_base = _CYBERWARE_ALIASES.get(base) or base
    return f"{aliased_base}{qualifier}".strip()


def _cyberware_has_qualifier(value: str) -> bool:
    """Return True when a cyberware name includes a trailing qualifier, e.g. '(Right)'."""
    if not isinstance(value, str):
        return False
    return re.search(r'\s*\(.*?\)\s*$', value.strip()) is not None


def _cyberware_ceiling_cost(value: str) -> int:
    """Return the humanity ceiling cost for a cyberware item (2 standard, 4 borgware, 0 medical)."""
    normalized = _normalize_cyberware_name(value)
    if not normalized:
        return 0
    if normalized in _CYBERWARE_CEILING_LOOKUP:
        return _CYBERWARE_CEILING_LOOKUP[normalized]
    return 2  # default for unknown/homebrew items


def _normalize_cyberware_list(values):
    """Return a cleaned cyberware list with non-empty string names only."""
    if not isinstance(values, list):
        return []
    cleaned = []
    for item in values:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name:
            cleaned.append(name)
    return cleaned


def _needs_humanity_baseline(er):
    """True when humanity ceiling baseline metadata is missing."""
    return not (
        isinstance(er, dict)
        and "_humanity_base_max" in er
    )


def _recompute_humanity_ceiling(er, reset_base=False, reset_base_max=False):
    """Recompute humanity ceiling from baseline metadata and current cyberware list.

    Baseline model:
    - _humanity_base_max: humanity max at baseline time.
    - _humanity_base_ceiling_total: total installed cyberware ceiling at baseline time.
    Current humanity max is derived by applying only the delta from that baseline:
      new_max = base_max + base_ceiling_total - current_ceiling_total
    """
    if not isinstance(er, dict):
        return
    humanity = er.get("humanity")
    if not isinstance(humanity, dict):
        return

    humanity_max = _safe_int(humanity.get("max", 0))
    humanity_current = _safe_int(humanity.get("current", 0))

    if (reset_base and reset_base_max) or "_humanity_base_max" not in er:
        er["_humanity_base_max"] = max(0, humanity_max)

    base_max = _safe_int(er.get("_humanity_base_max", humanity_max), default=None)
    if base_max is None:
        base_max = humanity_max
        er["_humanity_base_max"] = max(0, base_max)
    base_max = max(0, base_max)

    cyberware = _normalize_cyberware_list(er.get("cyberware_effects", []))
    er["cyberware_effects"] = cyberware
    total_ceiling = max(0, sum(_cyberware_ceiling_cost(cw) for cw in cyberware))

    if reset_base:
        er["_humanity_base_ceiling_total"] = total_ceiling
    elif "_humanity_base_ceiling_total" not in er:
        # Legacy compatibility: pre-baseline saves may have only _humanity_base_max
        # from the old model (new_max = base_max - current_total).
        # Treat that as base_ceiling_total=0 so behavior stays consistent.
        if "_humanity_base_max" in er:
            er["_humanity_base_ceiling_total"] = 0
        else:
            er["_humanity_base_ceiling_total"] = total_ceiling
    base_ceiling_total = _safe_int(er.get("_humanity_base_ceiling_total", total_ceiling), default=None)
    if base_ceiling_total is None:
        base_ceiling_total = total_ceiling
        er["_humanity_base_ceiling_total"] = base_ceiling_total
    base_ceiling_total = max(0, base_ceiling_total)

    new_max = max(0, min(base_max, base_max + base_ceiling_total - total_ceiling))
    humanity["max"] = new_max
    humanity["current"] = max(0, min(humanity_current, new_max))
    er["_humanity_ceiling_migrated"] = True


def _apply_ceiling_migration(edgerunners):
    """One-time pass: initialize baseline metadata for unmigrated edgerunners.

    This migration is intentionally non-destructive: it preserves current humanity max
    and establishes baseline metadata so future add/remove ops are delta-correct.
    """
    if not isinstance(edgerunners, dict):
        return
    for _, er in edgerunners.items():
        if not isinstance(er, dict):
            continue
        if er.get("_humanity_ceiling_migrated"):
            continue
        if not er.get("cyberware_effects"):
            continue
        has_base_max = "_humanity_base_max" in er
        _recompute_humanity_ceiling(
            er,
            # Legacy saves that already have _humanity_base_max should keep
            # their historical semantics (base_ceiling_total defaults to 0).
            reset_base=not has_base_max,
            reset_base_max=not has_base_max,
        )

# ============================================================
# Tier Derivation Helpers
# ============================================================

def _rs_tier(score):
    """Return (tier_label, bonus_text) for a CPRED Relationship Score."""
    if score >= 95:  return ("T7: Ride/Die", "+3 all social; fight together; share all intel")
    if score >= 85:  return ("T6: Ally", "+3 social; auto-success Persuasion DV 9-13; armed backup")
    if score >= 70:  return ("T5: Close", "+2 social; +3 Persuasion and Acting")
    if score >= 55:  return ("T4: Good", "+2 social; +3 Persuasion")
    if score >= 40:  return ("T3: Friend", "+1 Persuasion and Human Perception; favors no roll")
    if score >= 25:  return ("T2: Friendly", "+1 Persuasion")
    if score >= 10:  return ("T1: Acquaintance", "no social penalties")
    if score >= -9:  return ("Neutral", "")
    if score >= -24: return ("-T1: Annoyed", "-3 Persuasion")
    if score >= -39: return ("-T2: Disliked", "-1 all social")
    if score >= -54: return ("-T3: Enemy", "-2 all social; -3 w/ contacts; passive sabotage")
    if score >= -69: return ("-T4: Adversary", "-2 all checks; 1 obstacle/session")
    if score >= -84: return ("-T5: Nemesis", "-3 all checks; 2 complications/session")
    if score >= -94: return ("-T6: Sworn", "-3 all checks; ambushes; poisons mutual contacts")
    return ("-T7: Hatred", "-4 all checks; attacks regardless of odds")


def _roms_tier(score):
    """Return (tier_label, bonus_text) for a CPRED Romance Score."""
    if score >= 95: return ("T6: Unbreakable", "+3 all checks; redirect 10 dmg 1/session; free comms implant")
    if score >= 85: return ("T5: Married", "+3 all checks; take damage for partner 1/session; +3 vs Interrogation/intimidation")
    if score >= 65: return ("T4: Engaged", "+2 all checks; gain 1 NPC skill at half rank; +1 LUCK/session")
    if score >= 45: return ("T3: Partner", "+2 social; fight together +1 attacks adjacent; +1 LUCK/session")
    if score >= 25: return ("T2: Dating", "+1 social; +1 Human Perception; -1 Death Save rolls")
    if score >= 10: return ("T1: Flirting", "+1 Persuasion; receptive to advances")
    return ("None", "")


def _fr_tier(score):
    """Return (tier_label, bonus_text) for a CPRED Faction Reputation."""
    if score >= 90:  return ("T5: Champion", "+3 social; 40% discount; Solo rank 6+ backup; protected")
    if score >= 70:  return ("T4: Honored", "+2 social; 30% discount; 3-6 armed backup; cover minor crimes")
    if score >= 50:  return ("T3: Valued", "+2 social; 20% discount; 2-4 backup; advance warnings")
    if score >= 30:  return ("T2: Accepted", "+1 social; 10% discount; basic assistance")
    if score >= 10:  return ("T1: Known", "5% discount; non-hostile")
    if score >= -9:  return ("Neutral", "")
    if score >= -29: return ("-T1: Suspicious", "-1 social; prices +10%")
    if score >= -49: return ("-T2: Unwelcome", "-1 social; escorted out")
    if score >= -69: return ("-T3: Hostile", "-2 social; obstacles; 25% harassment/session")
    if score >= -89: return ("-T4: Enemy", "-2 social; bounty hunters 50%/week; allies grow suspicious")
    return ("-T5: KOS", "-3 social; assassination attempts every 3 days; allies turn hostile")


def _enrich_rel_op(op_data, old_score, new_score, tier_fn):
    """Write new_total and tier_transition onto op_data."""
    op_data["new_total"] = new_score
    old_label, _ = tier_fn(old_score)
    new_label, _ = tier_fn(new_score)
    if old_label != new_label:
        op_data["tier_transition"] = {"old": old_label, "new": new_label}


# ============================================================
# Structured Tier Bonuses (context → bonus mapping)
# ============================================================

def _rs_bonus(score):
    """Return {context: bonus} for an RS score.

    "social" = social-adjacent skills only (persuasion, conversation, etc.)
    "all" = every check including combat (extreme hatred fuels focus/aggression)
    """
    if score >= 95:  return {"social": 3}
    if score >= 85:  return {"social": 3, "persuasion": 3}
    if score >= 70:  return {"social": 2, "persuasion": 3, "acting": 3}
    if score >= 55:  return {"social": 2, "persuasion": 3}
    if score >= 40:  return {"persuasion": 1, "human_perception": 1}
    if score >= 25:  return {"persuasion": 1}
    if score >= 10:  return {}
    if score >= -9:  return {}
    if score >= -24: return {"persuasion": -3}
    if score >= -39: return {"social": -1}
    if score >= -54: return {"social": -2, "contacts": -3}
    if score >= -69: return {"all": -2}
    if score >= -84: return {"all": -3}
    if score >= -94: return {"all": -3}
    return {"all": -4}


def _roms_bonus(score):
    """Return {context: bonus} for a RomS score.

    High RomS ("all") applies to combat too — intimate knowledge of a
    partner's movements, reflexes, and tells translates to tactical advantage.
    """
    if score >= 95: return {"all": 3}
    if score >= 85: return {"all": 3}
    if score >= 65: return {"all": 2}
    if score >= 45: return {"social": 2}
    if score >= 25: return {"social": 1, "human_perception": 1}
    if score >= 10: return {"persuasion": 1}
    return {}


def max_roms_score(relationships: dict) -> int:
    """Return the highest RomS value across all NPCs in relationships."""
    if not isinstance(relationships, dict):
        return 0
    best = 0
    for npc_data in relationships.values():
        if isinstance(npc_data, dict):
            roms = npc_data.get("roms", 0)
            if isinstance(roms, (int, float)) and roms > best:
                best = int(roms)
    return best


def _fr_bonus(score):
    """Return {context: bonus} for a FR score."""
    if score >= 90:  return {"social": 3}
    if score >= 70:  return {"social": 2}
    if score >= 50:  return {"social": 2}
    if score >= 30:  return {"social": 1}
    if score >= 10:  return {}
    if score >= -9:  return {}
    if score >= -29: return {"social": -1}
    if score >= -49: return {"social": -1}
    if score >= -69: return {"social": -2}
    if score >= -89: return {"social": -2}
    return {"social": -3}


_SOCIAL_ADJACENT = frozenset({
    "social", "persuasion", "acting", "human_perception", "contacts",
    "conversation", "interrogation", "intimidation",
})


def _resolve_bonus_for_context(bonus_dict, check_context):
    """Resolve a single bonus dict against a check_context.

    Priority: exact match > "social" (only for social-adjacent contexts) > "all" > 0.
    """
    if not bonus_dict or not check_context:
        return bonus_dict.get("all", 0) if bonus_dict else 0
    # Exact match first
    if check_context in bonus_dict:
        return bonus_dict[check_context]
    # "social" fallback only for social-adjacent contexts
    if check_context in _SOCIAL_ADJACENT and "social" in bonus_dict:
        return bonus_dict["social"]
    return bonus_dict.get("all", 0)


def compute_rel_bonus(relationships, factions, target, check_context=None):
    """Compute the combined relationship bonus for an action targeting an NPC/faction.

    Looks up target in relationships and factions, resolves context-specific bonuses
    from RS, RomS, and FR tiers, and returns the clamped [-5, +5] integer.
    """
    if not target:
        return 0

    total = 0

    if relationships and isinstance(relationships, dict):
        target_lower = target.lower()
        for npc_name, npc_data in relationships.items():
            if npc_name.lower() == target_lower:
                if not isinstance(npc_data, dict):
                    continue
                rs = npc_data.get("rs")
                if isinstance(rs, (int, float)):
                    total += _resolve_bonus_for_context(_rs_bonus(rs), check_context)
                roms = npc_data.get("roms")
                if isinstance(roms, (int, float)):
                    total += _resolve_bonus_for_context(_roms_bonus(roms), check_context)
                break

    if factions and isinstance(factions, dict):
        target_lower = target.lower()
        for fac_name, fac_data in factions.items():
            if fac_name.lower() == target_lower:
                if not isinstance(fac_data, dict):
                    continue
                fr = fac_data.get("fr")
                if isinstance(fr, (int, float)):
                    total += _resolve_bonus_for_context(_fr_bonus(fr), check_context)
                break

    return max(-5, min(5, total))


# ============================================================
# Structured Game State
# ============================================================
#
# Stored in pipeline_state["game_state"]:
# {
#     "edgerunners": {
#         "V": {
#             "hp": {"current": 35, "max": 40, "seriously_wounded": false},
#             "humanity": {"current": 48, "max": 70},
#             "luck": {"current": 6, "max": 7},
#             "armor": {"head": 11, "body": 11},
#             "eurobucks": 2350,
#             "death_save_count": 0,
#             "critical_injuries": [
#                 {"name": "Broken Arm", "effect": "-2 to actions with that arm", "dv_mod": 1, "status": "active"}
#             ],
#             "cyberware_effects": ["Cybereye (Low-Light)", "Neural Link"],
#             "lifestyle": null,
#             "housing": null
#         }
#     }
# }


# Monthly rent from Core Rulebook p.379 + Errata p.4 (eb/month)
HOUSING_COSTS = {
    "Living on The Street": 0,
    "Living on The Street in a Vehicle": 0,
    "Cube Hotel": 500,
    "Cargo Container": 1000,
    "Studio Apartment": 1500,
    "Two-Bedroom Apartment": 2500,
    "Corporate Conapt": 0,                  # Given by corp (Exec Teamwork)
    "Upscale Conapt": 7500,
    "Luxury Penthouse": 15000,
    "Corporate Beaverville House": 0,       # Given by corp
    "Corporate Beaverville McMansion": 0,   # Given by corp
}
# Bedrooms per housing type (CRB p.378-381); capacity = 1 + bedrooms
HOUSING_BEDROOMS = {
    "Living on The Street": 0,
    "Living on The Street in a Vehicle": 0,
    "Cube Hotel": 0,
    "Cargo Container": 0,
    "Studio Apartment": 0,
    "Two-Bedroom Apartment": 2,
    "Corporate Conapt": 2,
    "Upscale Conapt": 2,
    "Luxury Penthouse": 3,
    "Corporate Beaverville House": 3,
    "Corporate Beaverville McMansion": 4,
}
# Housing types where occupant faces street-sleeping consequences
# despite "paying" (cost is 0 because sleeping rough, not because it's free)
HOUSING_STREET_TYPES = {"Living on The Street", "Living on The Street in a Vehicle"}

LIFESTYLE_COSTS = {
    "Kibble": 100, "Generic Prepak": 300,
    "Good Prepak": 600, "Fresh Food": 1500,
}


def init_game_state():
    """Return initial game state — edgerunners, IP tracker, relationships, factions."""
    return {"edgerunners": {}, "ip_tracker": {"session_scores": {"group": 0}, "awards": [], "balances": {}}, "relationships": {}, "factions": {}}


def _default_edgerunner():
    """Default stub for a new edgerunner."""
    return {
        "hp": {"current": -1, "max": 40, "seriously_wounded": False},
        "humanity": {"current": 0, "max": 0},
        "luck": {"current": 0, "max": 0},
        "armor": {"head": 0, "body": 0},
        "eurobucks": 0,
        "death_save_count": 0,
        "critical_injuries": [],
        "cyberware_effects": [],
        "weapons": [],
        "lifestyle": None,
        "housing": None,
        "body": 0,
        "endurance_base": 0,
        "housing_paid_month": None,
        "lifestyle_paid_month": None,
        "days_on_street": 0,
        "days_without_food": 0,
        "housing_pending": None,
        "lifestyle_pending": None,
        "housing_shared_with": None,   # name of owner whose housing this edgerunner shares
        "housing_bedrooms": None,      # per-unit bedroom override (None = use HOUSING_BEDROOMS table)
        "crammed": False,              # True if occupants > capacity at this housing
        "deck_slots": [],              # unified cyberdeck slots: programs, hardware, continuations, or null
        "cyberdeck": None,             # when set: {"tier": "Standard", "slots": 7, "cycles": 3}
        "conditions": [],              # general conditions: ["partially_nude", "seriously_wounded", etc.]
        "stats": {},                   # {"INT": 7, "REF": 8, "DEX": 8, "TECH": 5, "COOL": 6, "WILL": 5, "LUCK": 7, "BODY": 6, "EMP": 6, "MOVE": 6}
        "skills": {},                  # {"Handgun": 6, "Evasion": 6, "Athletics": 4, "Stealth": 2, "Interface": 6, ...}
        "rep": 0,                      # Reputation rank (for facedowns)
    }


def get_deck_slots(er):
    """Return the edgerunner's deck_slots, falling back to legacy 'programs' field."""
    return er.get("deck_slots") or er.get("programs") or []


def _update_seriously_wounded(er):
    """Auto-derive seriously_wounded flag from HP."""
    hp = er.get("hp", {})
    cur = hp.get("current", 0)
    hp["seriously_wounded"] = cur > 0 and cur < (hp.get("max", 40) + 1) // 2


def _wound_flag(current_hp, max_hp=None, seriously_wounded=None, long=False):
    """Return wound-state suffix for HP display lines.

    Works for both PC paths (pass seriously_wounded from hp dict) and
    NPC paths (pass current_hp and max_hp for inline calc).
    ``long`` controls whether to use the verbose or compact label.
    """
    if current_hp < 0:
        return ""  # uninitialized stub — no flag
    if current_hp == 0:
        return " [MORTALLY WOUNDED: -4 all actions]" if long else " MW"
    if seriously_wounded is not None:
        sw = seriously_wounded
    elif max_hp is not None:
        sw = current_hp < (max_hp + 1) // 2
    else:
        sw = False
    if sw:
        return " [SERIOUSLY WOUNDED: -2 all actions]" if long else " SW"
    return ""


def apply_game_state(game_state, agent_json, turn):
    """
    Apply CPRED edgerunner_ops from Events agent (or single-agent report_state) output.

    Ops format (array in agent_json["edgerunner_ops"]):
      {"edgerunner": "V", "op": "hp", "change": -8, "reason": "Shotgun hit after armor"}
      {"edgerunner": "V", "op": "humanity", "change": -4, "reason": "Cyberarm installed (2d6=4)"}
      {"edgerunner": "V", "op": "therapy", "change": 2, "reason": "Therapy session (2d6=4, half=2)"}
      {"edgerunner": "V", "op": "luck", "change": -2, "reason": "Added to Athletics check"}
      {"edgerunner": "V", "op": "luck_reset", "reason": "New session"}
      {"edgerunner": "V", "op": "armor", "location": "body", "change": -1, "reason": "Ablation from hit"}
      {"edgerunner": "V", "op": "armor_repair", "location": "body", "value": 11, "reason": "Repaired by tech"}
      {"edgerunner": "V", "op": "eurobucks", "change": -500, "reason": "Bought ammo"}
      {"edgerunner": "V", "op": "critical_injury", "action": "add", "name": "Broken Ribs", "effect": "-2 to movement actions", "dv_mod": 1}
      {"edgerunner": "V", "op": "critical_injury", "action": "remove", "name": "Broken Ribs", "reason": "Surgery"}
      {"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye (Low-Light)"}
      {"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye (Low-Light)"}
      {"edgerunner": "V", "op": "set", "fields": {"hp": {"current": 35, "max": 40}, ...}}
    """
    ops = agent_json.get("edgerunner_ops")

    # --- Pre-ops ceiling migration (handles pre-existing cyberware) ---
    _apply_ceiling_migration(game_state.get("edgerunners", {}))

    if ops:
        edgerunners = game_state.setdefault("edgerunners", {})

        for op_data in ops:
            if not isinstance(op_data, dict):
                continue
            subject = state_op_subject(op_data)
            if not subject or subject["kind"] != "edgerunner":
                continue
            er_name = subject["name"]
            op = op_data.get("op")
            if not isinstance(er_name, str) or not er_name or not op:
                continue

            # Auto-create edgerunner stub if not yet tracked
            if er_name not in edgerunners:
                edgerunners[er_name] = _default_edgerunner()
            er = edgerunners[er_name]

            try:
                if op == "set":
                    prev_humanity_max = _safe_int((er.get("humanity") or {}).get("max"), default=None)
                    fields = copy.deepcopy(op_data.get("fields", {}))
                    # Migrate legacy programs → deck_slots in set ops
                    if "programs" in fields and "deck_slots" not in fields:
                        old_progs = fields.pop("programs")
                        if isinstance(old_progs, list):
                            fields["deck_slots"] = [
                                {**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p
                                for p in old_progs
                            ]
                    for key, val in fields.items():
                        if key in er:
                            er[key] = val
                    # Keep humanity ceiling baseline in sync for set operations that touch
                    # humanity max and/or replace the cyberware list wholesale.
                    humanity_set = fields.get("humanity")
                    humanity_has_max = (
                        isinstance(humanity_set, dict)
                        and "max" in humanity_set
                    )
                    incoming_humanity_max = _safe_int(humanity_set.get("max"), default=None) if humanity_has_max else None
                    baseline_missing = _needs_humanity_baseline(er)
                    humanity_max_changed = (
                        humanity_has_max
                        and incoming_humanity_max is not None
                        and (
                            prev_humanity_max is None
                            or incoming_humanity_max != prev_humanity_max
                        )
                    )
                    if "cyberware_effects" in fields or humanity_has_max:
                        _recompute_humanity_ceiling(
                            er,
                            reset_base=(
                                humanity_max_changed
                                or baseline_missing
                            ),
                            reset_base_max=(
                                humanity_has_max
                                and (baseline_missing or humanity_max_changed)
                            ),
                        )
                    _update_seriously_wounded(er)
                    # Enforce housing_shared_with invariants if set via fields
                    if "housing_shared_with" in fields:
                        hsw = er.get("housing_shared_with")
                        if hsw is not None and hsw == er_name:
                            er["housing_shared_with"] = None  # Reject self-ref
                        elif hsw is not None:
                            er["housing"] = None
                            er["housing_pending"] = None
                    # Inverse: setting housing clears sharing (matches housing op)
                    if "housing" in fields and er.get("housing") is not None and er.get("housing_shared_with"):
                        er["housing_shared_with"] = None

                elif op == "hp":
                    change = int(op_data.get("change", 0))
                    er["hp"]["current"] = max(0, min(er["hp"]["max"], er["hp"]["current"] + change))
                    _update_seriously_wounded(er)
                    # Auto-reset death save counter when HP rises above 0
                    if er["hp"]["current"] > 0:
                        er["death_save_count"] = 0

                elif op == "humanity":
                    change = int(op_data.get("change", 0))
                    if change < 0:  # Humanity loss from cyberware
                        er["humanity"]["current"] = max(0, er["humanity"]["current"] + change)

                elif op == "therapy":
                    change = int(op_data.get("change", 0))
                    if change > 0:  # Partial recovery via therapy
                        er["humanity"]["current"] = min(
                            er["humanity"]["max"],
                            er["humanity"]["current"] + change
                        )

                elif op == "luck":
                    change = int(op_data.get("change", 0))
                    ceiling = er["luck"]["max"] * 2
                    er["luck"]["current"] = max(0, min(
                        ceiling,
                        er["luck"]["current"] + change
                    ))

                elif op == "luck_reset":
                    base = er["luck"]["max"]
                    # T3/T4 RomS bonus: +1 LUCK/session
                    if max_roms_score(game_state.get("relationships", {})) >= 45:
                        base += 1
                    er["luck"]["current"] = min(er["luck"]["max"] * 2, base)

                elif op == "armor":
                    location = op_data.get("location", "body")
                    change = int(op_data.get("change", 0))
                    if location in er["armor"]:
                        er["armor"][location] = max(0, er["armor"][location] + change)

                elif op == "armor_repair":
                    location = op_data.get("location", "body")
                    value = int(op_data.get("value", 0))
                    if location in er["armor"]:
                        er["armor"][location] = value

                elif op == "eurobucks":
                    change = int(op_data.get("change", 0))
                    er["eurobucks"] = max(0, er["eurobucks"] + change)

                elif op == "critical_injury":
                    action = op_data.get("action", "add")
                    name = op_data.get("name")
                    if action == "add" and name:
                        er["critical_injuries"].append({
                            "name": name,
                            "effect": op_data.get("effect", ""),
                            "dv_mod": int(op_data.get("dv_mod", 0)),
                            "status": "active"
                        })
                    elif action == "remove" and name:
                        er["critical_injuries"] = [
                            ci for ci in er["critical_injuries"] if ci["name"] != name
                        ]
                    elif action == "quick_fix" and name:
                        for ci in er["critical_injuries"]:
                            if ci["name"] == name:
                                ci["status"] = "quick_fixed"
                                break
                    elif action == "expire_qf" and name:
                        for ci in er["critical_injuries"]:
                            if ci["name"] == name and ci.get("status") == "quick_fixed":
                                ci["status"] = "active"
                                break

                elif op == "cyberware":
                    action = op_data.get("action", "add")
                    value = op_data.get("value")
                    if isinstance(value, str):
                        value = value.strip()
                    if value:
                        if _needs_humanity_baseline(er):
                            # Establish baseline before mutating list so first explicit
                            # add/remove applies only the new delta.
                            _recompute_humanity_ceiling(er, reset_base=True)
                        if action == "add":
                            add_key = _normalize_cyberware_entry_name(value)
                            if add_key and all(
                                _normalize_cyberware_entry_name(existing) != add_key
                                for existing in er["cyberware_effects"]
                            ):
                                er["cyberware_effects"].append(value)
                                _recompute_humanity_ceiling(er)
                        elif action == "remove":
                            removed = False
                            remove_entry_key = _normalize_cyberware_entry_name(value)
                            if remove_entry_key:
                                for idx, existing in enumerate(er["cyberware_effects"]):
                                    if _normalize_cyberware_entry_name(existing) == remove_entry_key:
                                        er["cyberware_effects"].pop(idx)
                                        _recompute_humanity_ceiling(er)
                                        removed = True
                                        break

                            # Broad fallback (base/alias) is only safe for unqualified input.
                            # If multiple qualified variants share the same base, do not guess.
                            if not removed and not _cyberware_has_qualifier(value):
                                remove_key = _normalize_cyberware_name(value)
                                if remove_key:
                                    candidate_indices = [
                                        idx
                                        for idx, existing in enumerate(er["cyberware_effects"])
                                        if _normalize_cyberware_name(existing) == remove_key
                                    ]
                                    if len(candidate_indices) == 1:
                                        er["cyberware_effects"].pop(candidate_indices[0])
                                        _recompute_humanity_ceiling(er)

                elif op == "weapon_set":
                    er["weapons"] = copy.deepcopy(op_data.get("weapons", []))

                elif op == "weapon_add":
                    weapon = copy.deepcopy(op_data.get("weapon", {}))
                    if weapon.get("name"):
                        er.setdefault("weapons", []).append(weapon)

                elif op == "weapon_remove":
                    weapon_ref = op_data.get("weapon", "")
                    if isinstance(weapon_ref, dict):
                        wname = weapon_ref.get("name", "")
                    else:
                        wname = weapon_ref
                    er["weapons"] = [w for w in er.get("weapons", []) if w.get("name") != wname]

                elif op == "weapon_ammo":
                    weapon_ref = op_data.get("weapon", "")
                    if isinstance(weapon_ref, dict):
                        wname = weapon_ref.get("name", "")
                    else:
                        wname = weapon_ref
                    current = int(op_data.get("current", 0))
                    for w in er.get("weapons", []):
                        if w.get("name") == wname:
                            w["current_ammo"] = max(0, current)
                            break

                elif op == "ammo":
                    # Resolver-generated: subtract rounds_consumed from weapon
                    wname = op_data.get("weapon_name", "")
                    consumed = int(op_data.get("rounds_consumed", 0))
                    if wname and consumed > 0:
                        for w in er.get("weapons", []):
                            if w.get("name") == wname:
                                w["current_ammo"] = max(0, w.get("current_ammo", 0) - consumed)
                                break

                elif op == "death_save":
                    er["death_save_count"] = er.get("death_save_count", 0) + 1

                elif op == "death_save_reset":
                    er["death_save_count"] = 0
                    # RAW: stabilized character at 0 HP is unconscious
                    if er.get("hp", {}).get("current", -1) == 0:
                        conditions = er.setdefault("conditions", [])
                        if "unconscious" not in conditions:
                            conditions.append("unconscious")

                elif op == "lifestyle":
                    er["lifestyle"] = op_data.get("value")

                elif op == "housing":
                    er["housing"] = op_data.get("value")
                    # Setting own housing cancels sharing (symmetric with housing_shared_with clearing housing)
                    if er.get("housing_shared_with"):
                        er["housing_shared_with"] = None

                elif op == "housing_pending":
                    er["housing_pending"] = op_data.get("value")

                elif op == "lifestyle_pending":
                    er["lifestyle_pending"] = op_data.get("value")

                elif op == "housing_shared_with":
                    value = op_data.get("value")
                    if value is None or (isinstance(value, str) and value):
                        # Reject self-referencing (would corrupt housing data)
                        if value is not None and value == er_name:
                            continue
                        er["housing_shared_with"] = value
                        # Sharing clears own housing (they use the owner's)
                        if value is not None:
                            er["housing"] = None
                            er["housing_pending"] = None

                elif op == "add_condition":
                    condition = op_data.get("condition", "")
                    if condition:
                        conditions = er.setdefault("conditions", [])
                        if condition not in conditions:
                            conditions.append(condition)

                elif op == "remove_condition":
                    condition = op_data.get("condition", "")
                    if condition:
                        conditions = er.get("conditions", [])
                        if condition in conditions:
                            conditions.remove(condition)

                elif op == "deck_slots_set":
                    er["deck_slots"] = copy.deepcopy(op_data.get("deck_slots", []))

                elif op == "programs_set":
                    # Backward compat: convert old programs_set to deck_slots
                    old_progs = op_data.get("programs", [])
                    er["deck_slots"] = [
                        {**p, "type": "program"} if isinstance(p, dict) and "type" not in p else p
                        for p in copy.deepcopy(old_progs)
                    ]

            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"CPRED apply_game_state: error processing op {op_data}: {e}")
                continue

    # --- Post-ops ceiling pass (catches cyberware added via 'set' op) ---
    _apply_ceiling_migration(game_state.get("edgerunners", {}))

    # --- IP tracking ops ---
    ip_ops = agent_json.get("ip_ops")
    if ip_ops:
        tracker = game_state.setdefault("ip_tracker", {"session_scores": {"group": 0}, "awards": [], "balances": {}})
        # Migrate old format if needed
        if "observations" in tracker:
            tracker.pop("observations", None)
            tracker.setdefault("session_scores", {"group": 0})
            tracker.setdefault("awards", [])
            tracker.setdefault("balances", {})
        valid_tiers = {10, 20, 30, 40, 50, 60, 70, 80}
        scores = tracker.setdefault("session_scores", {"group": 0})
        balances = tracker.setdefault("balances", {})
        for op_data in ip_ops:
            if not isinstance(op_data, dict):
                continue
            op = op_data.get("op")
            if not op:
                continue
            try:
                if op == "score":
                    value = int(op_data.get("value", 0))
                    if value not in valid_tiers:
                        continue
                    reason = op_data.get("reason", "")
                    category = op_data.get("category", "")
                    player = op_data.get("player")
                    if category == "group":
                        # Group score
                        cur = scores.get("group", 0) if isinstance(scores.get("group"), int) else scores.get("group", {}).get("value", 0)
                        if value >= cur:
                            scores["group"] = {"value": value, "reason": reason}
                    else:
                        if not isinstance(player, str) or not player:
                            continue
                        if not isinstance(category, str) or not category:
                            continue
                        # Individual player score
                        if player not in scores or not isinstance(scores[player], dict):
                            scores[player] = {}
                        player_scores = scores[player]
                        cur_entry = player_scores.get(category, {})
                        cur_val = cur_entry.get("value", 0) if isinstance(cur_entry, dict) else 0
                        if value >= cur_val:
                            player_scores[category] = {"value": value, "reason": reason}
                elif op == "award":
                    group_ip = int(op_data.get("group_ip", 0))
                    group_reason = op_data.get("group_reason", "")
                    individual = op_data.get("individual", [])
                    if not isinstance(individual, list):
                        individual = []
                    # Enforce group_ip from running group score (0 = job ongoing, respect that)
                    if group_ip > 0:
                        group_entry = scores.get("group", 0)
                        running_group = group_entry.get("value", 0) if isinstance(group_entry, dict) else (group_entry if isinstance(group_entry, int) else 0)
                        if running_group > 0:
                            group_ip = running_group
                    # Snapshot current scores
                    final_scores = copy.deepcopy(scores)
                    # Enforce: derive style_ip/style_category from running scores
                    enforced_individual = []
                    for ind in individual:
                        if not isinstance(ind, dict):
                            continue
                        pname = ind.get("player", "")
                        if not isinstance(pname, str) or not pname:
                            continue
                        player_scores = scores.get(pname, {})
                        if isinstance(player_scores, dict) and player_scores:
                            best_cat = max(
                                ((c, e.get("value", 0)) for c, e in player_scores.items() if isinstance(e, dict)),
                                key=lambda x: x[1],
                                default=(ind.get("style_category", ""), 0)
                            )
                            enforced_individual.append({
                                "player": pname,
                                "style_ip": best_cat[1],
                                "style_category": best_cat[0],
                                "reason": ind.get("reason", "")
                            })
                        else:
                            # No scores tracked for this player — use model's values
                            enforced_individual.append(ind)
                    # Sanitize fallback/model-provided entries so award apply is atomic
                    # even if style_ip/style_category are malformed.
                    normalized_individual = []
                    for ind in enforced_individual:
                        if not isinstance(ind, dict):
                            continue
                        pname = ind.get("player", "")
                        if not isinstance(pname, str) or not pname:
                            continue
                        style_ip = _safe_int(ind.get("style_ip", 0))
                        style_cat = ind.get("style_category", "")
                        if not isinstance(style_cat, str):
                            style_cat = _safe_text(style_cat)
                        reason = ind.get("reason", "")
                        if not isinstance(reason, str):
                            reason = _safe_text(reason)
                        normalized_individual.append({
                            "player": pname,
                            "style_ip": max(0, style_ip),
                            "style_category": style_cat,
                            "reason": reason,
                        })
                    award = {
                        "session": len(tracker["awards"]) + 1,
                        "group_ip": group_ip,
                        "group_reason": group_reason,
                        "individual": normalized_individual,
                        "final_scores": final_scores
                    }
                    tracker["awards"].append(award)
                    # Update balances
                    for ind in normalized_individual:
                        pname = ind.get("player", "")
                        style_ip = ind.get("style_ip", 0)
                        total = group_ip + style_ip
                        balances[pname] = balances.get(pname, 0) + total
                    # Reset session scores
                    tracker["session_scores"] = {"group": 0}
                    scores = tracker["session_scores"]
                elif op == "spend":
                    player = op_data.get("player", "")
                    amount = int(op_data.get("amount", 0))
                    if isinstance(player, str) and player and amount > 0:
                        balances[player] = max(0, balances.get(player, 0) - amount)
            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"CPRED apply_game_state: error processing ip_op {op_data}: {e}")
                continue

    # --- Relationship ops ---
    rel_ops = agent_json.get("relationship_ops")
    if rel_ops:
        relationships = game_state.setdefault("relationships", {})
        factions = game_state.setdefault("factions", {})
        _cascade_ops = []

        for op_data in rel_ops:
            if not isinstance(op_data, dict):
                continue
            op = op_data.get("op")
            target = op_data.get("target")
            if not isinstance(target, str) or not target or not op:
                continue

            try:
                if op == "set":
                    entity_type = op_data.get("type", "npc")
                    fields = copy.deepcopy(op_data.get("fields", {}))
                    if not isinstance(fields, dict):
                        fields = {}
                    if entity_type == "faction":
                        factions[target] = fields
                    else:
                        relationships[target] = fields

                elif op == "rs":
                    change = int(op_data.get("change", 0))
                    if target not in relationships:
                        relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                    old_score = relationships[target].get("rs", 0)
                    new_score = max(-100, min(100, old_score + change))
                    relationships[target]["rs"] = new_score
                    _enrich_rel_op(op_data, old_score, new_score, _rs_tier)
                    # Auto-cascade RS change to affiliated faction
                    _npc_faction = relationships[target].get("faction")
                    if _npc_faction and isinstance(_npc_faction, str) and _npc_faction in factions:
                        _cascade_change = int(change / 2)  # truncate toward zero
                        if _cascade_change != 0:
                            _old_fr = factions[_npc_faction].get("fr", 0)
                            _new_fr = max(-100, min(100, _old_fr + _cascade_change))
                            factions[_npc_faction]["fr"] = _new_fr
                            _sign = "+" if change > 0 else ""
                            _cascade_op = {
                                "op": "fr", "target": _npc_faction,
                                "change": _cascade_change,
                                "reason": f"Alliance cascade: {target} RS {_sign}{change}",
                                "auto_cascade": True,
                            }
                            _enrich_rel_op(_cascade_op, _old_fr, _new_fr, _fr_tier)
                            _cascade_ops.append(_cascade_op)

                elif op == "roms":
                    change = int(op_data.get("change", 0))
                    if target not in relationships:
                        relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                    old_score = relationships[target].get("roms", 0)
                    new_score = max(0, min(100, old_score + change))
                    relationships[target]["roms"] = new_score
                    _enrich_rel_op(op_data, old_score, new_score, _roms_tier)

                elif op == "fr":
                    change = int(op_data.get("change", 0))
                    if target not in factions:
                        factions[target] = {"fr": 0}
                    old_score = factions[target].get("fr", 0)
                    new_score = max(-100, min(100, old_score + change))
                    factions[target]["fr"] = new_score
                    _enrich_rel_op(op_data, old_score, new_score, _fr_tier)

                elif op == "npc_rs":
                    other = op_data.get("other")
                    change = int(op_data.get("change", 0))
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        if other not in npc_rels:
                            npc_rels[other] = {"rs": 0, "roms": 0}
                        old_score = npc_rels[other].get("rs", 0)
                        new_score = max(-100, min(100, old_score + change))
                        npc_rels[other]["rs"] = new_score
                        _enrich_rel_op(op_data, old_score, new_score, _rs_tier)

                elif op == "npc_roms":
                    other = op_data.get("other")
                    change = int(op_data.get("change", 0))
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        if other not in npc_rels:
                            npc_rels[other] = {"rs": 0, "roms": 0}
                        old_score = npc_rels[other].get("roms", 0)
                        new_score = max(0, min(100, old_score + change))
                        npc_rels[other]["roms"] = new_score
                        _enrich_rel_op(op_data, old_score, new_score, _roms_tier)

                elif op == "npc_set":
                    other = op_data.get("other")
                    fields = copy.deepcopy(op_data.get("fields", {}))
                    if not isinstance(fields, dict):
                        fields = {}
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        npc_rels[other] = fields

                elif op == "wb_mod":
                    change = int(op_data.get("change", 0))
                    if target not in relationships:
                        relationships[target] = {"rs": 0, "roms": 0, "wb_mod": 0}
                    current_mod = relationships[target].get("wb_mod", 0)
                    relationships[target]["wb_mod"] = current_mod + change

                elif op == "wb_boost_spend":
                    if target in relationships:
                        relationships[target]["wb_boost"] = False

            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.warning(f"CPRED apply_game_state: error processing rel op {op_data}: {e}")
                continue

        if _cascade_ops:
            rel_ops.extend(_cascade_ops)

    # --- Automated monthly expense processing ---
    hud = agent_json.get("hud_state")
    if isinstance(hud, dict) and isinstance(hud.get("date"), str):
        _process_expenses(game_state, hud["date"])

    return game_state


def _parse_game_date(date_str):
    """Parse a YYYY-MM-DD date string, returning None on failure."""
    if not isinstance(date_str, str):
        return None
    try:
        parts = date_str.strip().split("-")
        if len(parts) >= 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        pass
    return None


def _get_housing_groups(edgerunners):
    """Build housing groups from housing_shared_with fields.

    Returns:
        groups: dict mapping owner_name -> [owner_name, sharer1, sharer2, ...]
        sharers: set of names who share someone else's housing
    """
    groups = {}
    sharers = set()

    for name, er in edgerunners.items():
        shared_with = er.get("housing_shared_with")
        if shared_with and isinstance(shared_with, str):
            # Validate: owner must exist and have housing, and not itself be a sharer
            owner_er = edgerunners.get(shared_with, {})
            if (shared_with in edgerunners
                    and owner_er.get("housing")
                    and not owner_er.get("housing_shared_with")):
                groups.setdefault(shared_with, [shared_with])
                if name not in groups[shared_with]:
                    groups[shared_with].append(name)
                sharers.add(name)

    # Ensure solo owners (with housing, not sharing) also appear
    for name, er in edgerunners.items():
        if er.get("housing") and name not in sharers and name not in groups:
            groups[name] = [name]

    return groups, sharers


def _housing_cost_for_person(er_name, er, edgerunners, groups, sharers):
    """Compute one person's housing cost share.

    Returns: (cost_for_person, base_cost, housing_str, n_occupants)
    """
    if er_name in sharers:
        # Sharer: look up owner's housing
        owner_name = er.get("housing_shared_with")
        owner_er = edgerunners.get(owner_name, {})
        housing = owner_er.get("housing")
        base_cost = HOUSING_COSTS.get(housing, 0) if housing else 0
        group = groups.get(owner_name, [owner_name])
        n = len(group)
        cost = base_cost // n if n > 0 else 0
        return cost, base_cost, housing, n
    elif er.get("housing"):
        # Owner (solo or with sharers)
        housing = er["housing"]
        base_cost = HOUSING_COSTS.get(housing, 0)
        group = groups.get(er_name, [er_name])
        n = len(group)
        if n > 1:
            cost = base_cost - (n - 1) * (base_cost // n)
        else:
            cost = base_cost
        return cost, base_cost, housing, n
    return 0, 0, None, 0


def _mark_housing_free(er, month_str, housing):
    """Mark free housing (corp-provided, etc.) as paid for the month."""
    er["housing_paid_month"] = month_str
    # Don't reset days_on_street for street types — they're still sleeping rough
    if housing not in HOUSING_STREET_TYPES:
        er["days_on_street"] = 0


def _wb_state_from_roll(total):
    """Map a 2d10+mod total to a Wellbeing state string."""
    if total <= 3:
        return "Rough"
    if total <= 6:
        return "Frayed"
    if total <= 15:
        return "Even"
    if total <= 18:
        return "Buoyant"
    return "Excellent"


def _process_expenses(game_state, new_date_str):
    """
    Process monthly expense deductions and daily consequences.
    Called from apply_game_state() when hud_state.date changes.

    Restructured as per-day → per-edgerunner to support housing sharing
    (owner covers sharer shortfall requires accurate owner balance per-day).

    Mutates edgerunner state (eurobucks, HP, tracking fields).
    Appends notification dicts to game_state["_pending_notifications"].
    """
    old_date = _parse_game_date(game_state.get("current_date"))
    new_date = _parse_game_date(new_date_str)

    # Always update current_date even if parsing fails
    game_state["current_date"] = new_date_str

    if old_date is None or new_date is None:
        # First turn: CPR "first month free" — mark paid through NEXT month so
        # the free period covers at least one full calendar month regardless of
        # what day the game starts (e.g. starting March 28 → free through April).
        if new_date is not None:
            next_m = new_date.month + 1
            next_y = new_date.year
            if next_m > 12:
                next_m = 1
                next_y += 1
            free_through = f"{next_y}-{next_m:02d}"
            _, sharers_set = _get_housing_groups(game_state.get("edgerunners", {}))
            for er_name, er in game_state.get("edgerunners", {}).items():
                has_housing = er.get("housing") or (er_name in sharers_set)
                if has_housing and er.get("housing_paid_month") is None:
                    er["housing_paid_month"] = free_through
                if er.get("lifestyle") and er.get("lifestyle_paid_month") is None:
                    er["lifestyle_paid_month"] = free_through
        return  # First turn or unparseable — skip
    if new_date <= old_date:
        return  # No time advance

    notifs = game_state.setdefault("_pending_notifications", [])
    edgerunners = game_state.get("edgerunners", {})
    groups, sharers_set = _get_housing_groups(edgerunners)

    # Per-edgerunner tracking for batched notifications
    er_track = {}
    for n in edgerunners:
        er_track[n] = {"paid": [], "unpaid": [], "consequences": [], "dead": False}

    # Process day by day across all edgerunners
    current = old_date
    while current < new_date:
        current += timedelta(days=1)
        month_str = f"{current.year}-{current.month:02d}"

        # --- Month boundary: apply pending tier changes, then recompute groups ---
        if current.day == 1:
            for er_name, er in sorted(edgerunners.items()):
                if er_track[er_name]["dead"]:
                    continue
                if er.get("housing_pending") is not None:
                    er["housing"] = er["housing_pending"]
                    er["housing_pending"] = None
                    # Setting own housing cancels sharing
                    if er["housing"] is not None and er.get("housing_shared_with"):
                        er["housing_shared_with"] = None
                if er.get("lifestyle_pending") is not None:
                    er["lifestyle"] = er["lifestyle_pending"]
                    er["lifestyle_pending"] = None
            # Recompute groups after pending changes
            groups, sharers_set = _get_housing_groups(edgerunners)

        # --- Housing payment (owner first, then sharers with shortfall coverage) ---
        # Pass 1: owners/solo pay their housing share
        for er_name in sorted(edgerunners.keys()):
            er = edgerunners[er_name]
            if er_track[er_name]["dead"] or er_name in sharers_set:
                continue
            if not er.get("housing") or (er.get("housing_paid_month") or "") >= month_str:
                continue
            cost, base, housing, n_occ = _housing_cost_for_person(
                er_name, er, edgerunners, groups, sharers_set)
            if cost <= 0:
                _mark_housing_free(er, month_str, housing)
                continue
            if er.get("eurobucks", 0) >= cost:
                er["eurobucks"] -= cost
                er["housing_paid_month"] = month_str
                er["days_on_street"] = 0
                label = f"Housing ({housing}"
                if n_occ > 1:
                    label += f", {n_occ} occupants, {cost}eb/{base}eb"
                label += f"): -{cost}eb"
                if current.day != 1:
                    label += " (catch-up)"
                er_track[er_name]["paid"].append(label)
            elif current.day == 1:
                er_track[er_name]["unpaid"].append(
                    f"Housing ({housing}): {cost}eb — can't afford (balance: {er.get('eurobucks', 0)}eb)")

        # Pass 2: sharers try to pay, with owner covering shortfall
        for er_name in sorted(edgerunners.keys()):
            er = edgerunners[er_name]
            if er_track[er_name]["dead"] or er_name not in sharers_set:
                continue
            if (er.get("housing_paid_month") or "") >= month_str:
                continue
            cost, base, housing, n_occ = _housing_cost_for_person(
                er_name, er, edgerunners, groups, sharers_set)
            if cost <= 0:
                _mark_housing_free(er, month_str, housing)
                continue
            sharer_bal = er.get("eurobucks", 0)
            if sharer_bal >= cost:
                # Sharer can afford on their own
                er["eurobucks"] -= cost
                er["housing_paid_month"] = month_str
                er["days_on_street"] = 0
                owner_name = er.get("housing_shared_with", "?")
                label = f"Housing share ({owner_name}'s {housing}, {cost}eb/{base}eb): -{cost}eb"
                if current.day != 1:
                    label += " (catch-up)"
                er_track[er_name]["paid"].append(label)
            else:
                # Sharer can't fully afford — try owner shortfall coverage
                deficit = cost - sharer_bal
                owner_name = er.get("housing_shared_with")
                owner_er = edgerunners.get(owner_name, {}) if owner_name else {}
                owner_bal = owner_er.get("eurobucks", 0)
                if owner_name and not er_track.get(owner_name, {}).get("dead") and owner_bal >= deficit:
                    # Owner covers the shortfall
                    if sharer_bal > 0:
                        er["eurobucks"] = 0
                    owner_er["eurobucks"] -= deficit
                    er["housing_paid_month"] = month_str
                    er["days_on_street"] = 0
                    label = f"Housing share ({owner_name}'s {housing}, {cost}eb/{base}eb): -{sharer_bal}eb"
                    if current.day != 1:
                        label += " (catch-up)"
                    er_track[er_name]["paid"].append(label)
                    er_track[owner_name]["paid"].append(
                        f"{owner_name} covered {er_name}'s housing shortfall: -{deficit}eb")
                elif current.day == 1:
                    er_track[er_name]["unpaid"].append(
                        f"Housing share ({owner_name}'s {housing}): {cost}eb — can't afford (balance: {sharer_bal}eb)")

        # --- Lifestyle payment (no sharing, standard per-edgerunner) ---
        for er_name in sorted(edgerunners.keys()):
            er = edgerunners[er_name]
            if er_track[er_name]["dead"]:
                continue
            if not er.get("lifestyle") or (er.get("lifestyle_paid_month") or "") >= month_str:
                continue
            cost = LIFESTYLE_COSTS.get(er["lifestyle"], 0)
            if cost > 0 and er.get("eurobucks", 0) >= cost:
                er["eurobucks"] -= cost
                er["lifestyle_paid_month"] = month_str
                er["days_without_food"] = 0
                label = f"Lifestyle ({er['lifestyle']}): -{cost}eb"
                if current.day != 1:
                    label += " (catch-up)"
                er_track[er_name]["paid"].append(label)
            elif cost > 0 and current.day == 1:
                er_track[er_name]["unpaid"].append(
                    f"Lifestyle ({er['lifestyle']}): {cost}eb — can't afford (balance: {er.get('eurobucks', 0)}eb)")

        # --- Daily consequences ---
        for er_name in sorted(edgerunners.keys()):
            er = edgerunners[er_name]
            if er_track[er_name]["dead"]:
                continue

            # Housing consequences: street-sleeping endurance checks
            # Determine if this edgerunner's housing situation means sleeping rough
            has_housing = er.get("housing") or er_name in sharers_set
            is_street_type = er.get("housing") in HOUSING_STREET_TYPES
            # Sharers who share a street-type housing also face consequences
            if er_name in sharers_set and not is_street_type:
                owner_name = er.get("housing_shared_with")
                owner_er = edgerunners.get(owner_name, {})
                is_street_type = owner_er.get("housing") in HOUSING_STREET_TYPES
            is_unpaid = has_housing and (er.get("housing_paid_month") or "") < month_str and current.day >= 2

            if is_street_type or is_unpaid:
                er["days_on_street"] = er.get("days_on_street", 0) + 1
                endurance_base = er.get("endurance_base", 0)
                if endurance_base > 0:
                    d10 = random.randint(1, 10)
                    total = d10 + endurance_base
                    if total <= 15:  # Must BEAT DV15
                        hp_loss = random.randint(1, 6)
                        hp = er.get("hp", {"current": 0, "max": 40})
                        hp["current"] = max(0, hp["current"] - hp_loss)
                        _update_seriously_wounded(er)
                        er_track[er_name]["consequences"].append(
                            f"Street sleep: d10[{d10}]+{endurance_base}={total} vs DV15 FAIL → -{hp_loss} HP")
                    else:
                        er_track[er_name]["consequences"].append(
                            f"Street sleep: d10[{d10}]+{endurance_base}={total} vs DV15 ✓")
                elif er.get("days_on_street") == 1:
                    er_track[er_name]["consequences"].append(
                        "Evicted — endurance_base not set, skipping roll (set via bootstrap)")

            # Lifestyle consequences: starvation death saves
            if er.get("lifestyle") and (er.get("lifestyle_paid_month") or "") < month_str:
                er["days_without_food"] = er.get("days_without_food", 0) + 1
                if er["days_without_food"] > 7:
                    body = er.get("body", 0)
                    if body > 0:
                        d10 = random.randint(1, 10)
                        if d10 >= body or d10 == 10:
                            er["hp"]["current"] = 0
                            _update_seriously_wounded(er)
                            er_track[er_name]["consequences"].append(
                                f"Starvation Death Save: d10[{d10}] vs BODY {body} — DEAD")
                            er_track[er_name]["dead"] = True
                        else:
                            er_track[er_name]["consequences"].append(
                                f"Starvation Death Save: d10[{d10}] vs BODY {body} ✓")
                    elif er["days_without_food"] == 8:
                        er_track[er_name]["consequences"].append(
                            "Starving (day 8+) — body stat not set, skipping death save (set via bootstrap)")

        # --- Daily Wellbeing (6AM) ---
        relationships = game_state.get("relationships", {})
        wb_parts = []
        luck_granted = False
        for npc_name in sorted(relationships.keys()):
            npc = relationships[npc_name]
            if not isinstance(npc, dict):
                continue
            d1 = random.randint(1, 10)
            d2 = random.randint(1, 10)
            raw = d1 + d2
            mod = max(-2, min(2, npc.get("wb_mod", 0)))
            total = max(2, min(20, raw + mod))
            state = _wb_state_from_roll(total)
            npc["wb"] = state
            npc["wb_boost"] = state in ("Buoyant", "Excellent")
            npc["wb_mod"] = 0  # reset after applying

            mod_str = f"{mod:+d}" if mod else ""
            part = f"{npc_name} 2d10[{d1},{d2}]{mod_str}={total} → {state}"

            # Excellent + T3+ romance: grant +1 bonus LUCK to first PC only
            if state == "Excellent" and npc.get("roms", 0) >= 45:
                for er_name in sorted(edgerunners.keys()):
                    er = edgerunners[er_name]
                    luck = er.get("luck", {})
                    ceiling = luck.get("max", 0) * 2
                    if luck.get("current", 0) < ceiling:
                        luck["current"] = min(ceiling, luck.get("current", 0) + 1)
                        luck_granted = True
                        part += " (+1 LUCK)"
                    break  # only first PC

            wb_parts.append(part)

        if wb_parts:
            notifs.append({
                "type": "wellbeing_rolled",
                "summary": "WB: " + "; ".join(wb_parts),
                "luck_granted": luck_granted,
            })

    # --- Crammed post-pass ---
    if new_date > old_date:
        final_month = f"{new_date.year}-{new_date.month:02d}"
        for owner_name, group in groups.items():
            owner_er = edgerunners.get(owner_name, {})
            housing = owner_er.get("housing")
            if not housing or housing in HOUSING_STREET_TYPES:
                for m in group:
                    if m in edgerunners:
                        edgerunners[m]["crammed"] = False
                continue
            bedrooms = owner_er.get("housing_bedrooms")
            if bedrooms is None:
                bedrooms = HOUSING_BEDROOMS.get(housing, 0)
            if not isinstance(bedrooms, int) or bedrooms < 0:
                bedrooms = 0
            capacity = 1 + bedrooms
            paid_count = sum(1 for m in group
                             if (edgerunners.get(m, {}).get("housing_paid_month") or "") >= final_month)
            is_crammed = paid_count > capacity
            for m in group:
                if m in edgerunners:
                    # Only mark paid members as crammed; evicted members are on the street
                    m_paid = (edgerunners[m].get("housing_paid_month") or "") >= final_month
                    edgerunners[m]["crammed"] = is_crammed and m_paid
            if is_crammed:
                notifs.append({
                    "type": "housing_crammed",
                    "owner": owner_name,
                    "occupants": paid_count,
                    "capacity": capacity,
                    "summary": f"{owner_name}'s {housing}: {paid_count}/{capacity} capacity — all crammed (-2 all actions)",
                })
        # Reset crammed for edgerunners not in any group
        for er_name, er in edgerunners.items():
            if er_name not in sharers_set and er_name not in groups:
                er["crammed"] = False

    # --- Emit batched notifications ---
    for er_name in sorted(edgerunners.keys()):
        t = er_track[er_name]
        er = edgerunners[er_name]
        if t["paid"]:
            notifs.append({
                "type": "expense_paid",
                "edgerunner": er_name,
                "summary": "; ".join(t["paid"]),
                "new_balance": er.get("eurobucks", 0),
            })
        if t["unpaid"]:
            notifs.append({
                "type": "expense_unpaid",
                "edgerunner": er_name,
                "summary": "; ".join(t["unpaid"]),
            })
        if t["consequences"]:
            is_death = any("DEAD" in e for e in t["consequences"])
            notifs.append({
                "type": "expense_consequence",
                "edgerunner": er_name,
                "summary": "; ".join(t["consequences"]),
                "result": "dead" if is_death else "ongoing",
                "days_on_street": er.get("days_on_street", 0),
                "days_without_food": er.get("days_without_food", 0),
            })


def _format_score_with_bonus(label, score, tier_fn):
    """Format a score label with tier and optional bonus, e.g. 'RS 5 (Warm — +1)'."""
    tier_label, bonus = tier_fn(score)
    if bonus:
        return f"{label} {score} ({tier_label} \u2014 {bonus})"
    return f"{label} {score} ({tier_label})"


def _format_npc_line(name, data):
    """Format a single NPC line for relationship injection."""
    rs = data.get("rs", 0)
    roms = data.get("roms", 0)
    parts = [_format_score_with_bonus("RS", rs, _rs_tier)]
    if roms > 0:
        parts.append(_format_score_with_bonus("RomS", roms, _roms_tier))
    wb = data.get("wb")
    if wb and wb != "Even":
        if wb == "Buoyant":
            wb_label = "Buoyant (+1 social, once)" if data.get("wb_boost") else "Buoyant (spent)"
        elif wb == "Excellent":
            wb_label = "Excellent (+1 social, once; +1 LUCK)" if data.get("wb_boost") else "Excellent (spent)"
        else:
            wb_label = wb
        parts.append(f"WB: {wb_label}")
    faction = data.get("faction")
    if faction:
        line = f"  {name} [{faction}]: {' | '.join(parts)}"
    else:
        line = f"  {name}: {' | '.join(parts)}"
    notes = data.get("notes")
    if notes:
        line += f"\n    notes: {notes}"

    npc_rels = data.get("npc_relationships", {})
    if npc_rels:
        for other in sorted(npc_rels):
            nr = npc_rels[other]
            nr_rs = nr.get("rs", 0)
            nr_roms = nr.get("roms", 0)
            nr_parts = [_format_score_with_bonus("RS", nr_rs, _rs_tier)]
            if nr_roms > 0:
                nr_parts.append(_format_score_with_bonus("RomS", nr_roms, _roms_tier))
            line += f"\n    \u2192 {other}: {' | '.join(nr_parts)}"

    return line


def _format_faction_line(name, data):
    """Format a single faction line for relationship injection."""
    fr = data.get("fr", 0)
    fr_label, fr_bonus = _fr_tier(fr)
    if fr_bonus:
        line = f"  {name}: FR {fr} ({fr_label} \u2014 {fr_bonus})"
    else:
        line = f"  {name}: FR {fr} ({fr_label})"
    notes = data.get("notes")
    if notes:
        line += f"\n    notes: {notes}"
    return line


def _build_relationship_injection(game_state):
    """Build [RELATIONSHIP STATE] injection block from game_state."""
    relationships = game_state.get("relationships", {})
    factions = game_state.get("factions", {})
    if not relationships and not factions:
        return "[RELATIONSHIP STATE]\n(empty \u2014 bootstrap with relationship_ops \"set\" after character creation is complete)\n[/RELATIONSHIP STATE]"

    lines = ["[RELATIONSHIP STATE]"]
    if relationships:
        lines.append("NPCs:")
        for name in sorted(relationships):
            lines.append(_format_npc_line(name, relationships[name]))
    if factions:
        lines.append("Factions:")
        for name in sorted(factions):
            lines.append(_format_faction_line(name, factions[name]))
    lines.append("[/RELATIONSHIP STATE]")
    return "\n".join(lines)


def build_game_injection(game_state, scene_state=None):
    """Build [EDGERUNNER STATE] injection block from structured state."""
    edgerunners = game_state.get("edgerunners", {})
    if not edgerunners:
        result = "[EDGERUNNER STATE]\n(empty — bootstrap from character sheets, or initialize via character creation)\n[/EDGERUNNER STATE]"
    else:
        lines = ["[EDGERUNNER STATE]"]
        for name, er in sorted(edgerunners.items()):
            hp = er.get("hp", {})
            humanity = er.get("humanity", {})
            luck = er.get("luck", {})
            armor = er.get("armor", {})
            eb = er.get("eurobucks", 0)
            injuries = er.get("critical_injuries", [])
            cyberware = er.get("cyberware_effects", [])

            lines.append(f"{name}:")
            lines.append(f"  HP: {hp.get('current', 0)}/{hp.get('max', 40)}{_wound_flag(hp.get('current', 0), seriously_wounded=hp.get('seriously_wounded'), long=True)}")
            lines.append(f"  Humanity: {humanity.get('current', 0)}/{humanity.get('max', 0)}")
            lines.append(f"  Luck: {luck.get('current', 0)}/{luck.get('max', 0)}")
            # Wellbeing Boosts: consumable +1 from Buoyant/Excellent NPCs (scene-scoped)
            _npcs_present = set(scene_state.get("npcs_present", [])) if scene_state else None
            wb_boosts = []
            for npc_name, npc_data in sorted(game_state.get("relationships", {}).items()):
                if isinstance(npc_data, dict) and npc_data.get("wb_boost"):
                    if _npcs_present is not None and npc_name not in _npcs_present:
                        continue
                    wb_boosts.append(f"{npc_name} ({npc_data.get('wb', '?')})")
            if wb_boosts:
                lines.append(f"  Wellbeing Boosts: {', '.join(wb_boosts)}")
            lines.append(f"  Armor: Head SP {armor.get('head', 0)} | Body SP {armor.get('body', 0)}")
            lines.append(f"  Eurobucks: {eb:,}")

            lifestyle = er.get("lifestyle")
            housing = er.get("housing")
            shared_with = er.get("housing_shared_with")
            if lifestyle or housing or shared_with:
                parts = []
                if lifestyle:
                    parts.append(f"Lifestyle: {lifestyle}")
                # Check shared_with first (matches expense system priority)
                if shared_with and shared_with in edgerunners:
                    owner_er = edgerunners[shared_with]
                    owner_housing = owner_er.get("housing")
                    if owner_housing and not owner_er.get("housing_shared_with"):
                        parts.append(f"Housing: sharing {shared_with}'s {owner_housing}")
                    else:
                        parts.append(f"Housing: sharing {shared_with} (INVALID — owner has no housing)")
                elif housing:
                    # Owner: show bedroom count and sharers if any
                    bedrooms = er.get("housing_bedrooms")
                    if bedrooms is None:
                        bedrooms = HOUSING_BEDROOMS.get(housing, 0)
                    owner_sharers = [n for n, e in sorted(edgerunners.items())
                                     if e.get("housing_shared_with") == name and n != name]
                    if owner_sharers:
                        parts.append(f"Housing: {housing} ({bedrooms}BR, shared with {', '.join(owner_sharers)})")
                    else:
                        parts.append(f"Housing: {housing}")
                if er.get("crammed"):
                    parts.append("CRAMMED (-2 all actions)")
                lines.append(f"  {' | '.join(parts)}")

            if injuries:
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries if ci.get("status") != "quick_fixed")
                injury_strs = []
                for ci in injuries:
                    qf_tag = " [QF]" if ci.get("status") == "quick_fixed" else ""
                    injury_strs.append(f"{ci['name']}{qf_tag} ({ci['effect']}, Death Save +{ci['dv_mod']})")
                lines.append(f"  Critical injuries (Death Save DV +{dv_total}): {'; '.join(injury_strs)}")

            death_saves = er.get("death_save_count", 0)
            if death_saves > 0:
                lines.append(f"  Death Saves: {death_saves} (cumulative +{death_saves})")

            weapons = er.get("weapons", [])
            if weapons:
                weapon_strs = []
                for w in weapons:
                    wname = w.get("name", "?")
                    wdmg = w.get("damage", "?")
                    wtype = w.get("type", "ranged")
                    wskill = w.get("skill", "")
                    if wtype == "melee":
                        weapon_strs.append(f"{wname} ({wdmg}, {wskill})" if wskill else f"{wname} ({wdmg}, Melee Weapon)")
                    else:
                        cur = w.get("current_ammo", 0)
                        mx = w.get("max_ammo", 0)
                        skill_part = f", {wskill}" if wskill else ""
                        weapon_strs.append(f"{wname} ({wdmg}, {cur}/{mx} ammo{skill_part})")
                lines.append(f"  Weapons: {'; '.join(weapon_strs)}")

            if cyberware:
                lines.append(f"  Cyberware: {', '.join(cyberware)}")

            cyberdeck = er.get("cyberdeck")
            if isinstance(cyberdeck, dict):
                deck_slots = get_deck_slots(er)
                total_slots = len(deck_slots) if deck_slots else cyberdeck.get("slots", 0)
                used_slots = sum(1 for s in deck_slots if s is not None) if deck_slots else 0
                lines.append(f"  Cyberdeck: {cyberdeck.get('tier', '?')} | {used_slots}/{total_slots} slots | {cyberdeck.get('cycles', 0)} cycles")
                # Extract programs and hardware from unified deck_slots
                programs = [s for s in deck_slots if isinstance(s, dict) and s.get("type", "program") == "program" and not s.get("_continuation_of")]
                hardware = [s for s in deck_slots if isinstance(s, dict) and s.get("type") == "hardware"]
                if programs:
                    prog_strs = []
                    for p in programs:
                        pname = p.get("name", "?")
                        pcat = p.get("category", "")
                        pstatus = p.get("status", "stored")
                        prog_strs.append(f"{pname} ({pcat}, {pstatus})")
                    lines.append(f"  Programs: {'; '.join(prog_strs)}")
                if hardware:
                    hw_strs = [f"{h.get('name', '?')} ({h.get('slots_used', 1)} slots)" for h in hardware]
                    lines.append(f"  Hardware: {'; '.join(hw_strs)}")

            conditions = er.get("conditions", [])
            if conditions:
                lines.append(f"  Conditions: {', '.join(conditions)}")

        lines.append("[/EDGERUNNER STATE]")
        result = "\n".join(lines)

    # Append IP tracker if present
    ip_block = _build_ip_tracker_injection(game_state)
    if ip_block:
        result += "\n\n" + ip_block

    # Append relationship state
    rel_block = _build_relationship_injection(game_state)
    result += "\n\n" + rel_block

    # Append expense status if any edgerunner has active consequences
    expense_lines = []
    for name, er in sorted(edgerunners.items()):
        parts = []
        if er.get("days_on_street", 0) > 0:
            parts.append(f"sleeping on street ({er['days_on_street']} days)")
        if er.get("days_without_food", 0) > 0:
            grace = er["days_without_food"] <= 7
            parts.append(f"no food ({er['days_without_food']} days{', in grace period' if grace else ', DAILY DEATH SAVES'})")
        if er.get("crammed"):
            parts.append("crammed housing (-2 all actions)")
        if parts:
            expense_lines.append(f"  {name}: {'; '.join(parts)}")
    if expense_lines:
        result += "\n\n[EXPENSE STATUS]\n" + "\n".join(expense_lines) + "\n[/EXPENSE STATUS]"

    # Pre-month warning: upcoming expenses in last 5 days of month
    # Uses pending values (housing_pending/lifestyle_pending) since those apply on the 1st
    current_date = _parse_game_date(game_state.get("current_date"))
    if current_date and current_date.day >= 26:
        # Build effective edgerunner view with pending changes applied
        eff_ers = {}
        for n, er in edgerunners.items():
            eff = dict(er)
            if eff.get("housing_pending") is not None:
                eff["housing"] = eff["housing_pending"]
                # Setting own housing cancels sharing
                if eff["housing"] is not None and eff.get("housing_shared_with"):
                    eff["housing_shared_with"] = None
            if eff.get("lifestyle_pending") is not None:
                eff["lifestyle"] = eff["lifestyle_pending"]
            eff_ers[n] = eff
        eff_groups, eff_sharers = _get_housing_groups(eff_ers)
        warning_lines = []
        for name, eff in sorted(eff_ers.items()):
            h_cost, _, h_housing, _ = _housing_cost_for_person(name, eff, eff_ers, eff_groups, eff_sharers)
            l_cost = LIFESTYLE_COSTS.get(eff.get("lifestyle"), 0) if eff.get("lifestyle") else 0
            total = h_cost + l_cost
            if total <= 0:
                continue
            balance = edgerunners[name].get("eurobucks", 0)  # Use real balance, not effective
            affordable = "OK" if balance >= total else "CANNOT AFFORD"
            parts = []
            if h_cost:
                if name in eff_sharers:
                    owner_name = eff.get("housing_shared_with", "?")
                    parts.append(f"Housing share ({owner_name}'s {h_housing}): {h_cost}eb")
                elif eff.get("housing"):
                    parts.append(f"Housing ({eff['housing']}): {h_cost}eb")
            if l_cost:
                parts.append(f"Lifestyle ({eff['lifestyle']}): {l_cost}eb")
            warning_lines.append(
                f"  {name}: {' + '.join(parts)} = {total}eb. Balance: {balance}eb. {affordable}."
            )
        if warning_lines:
            result += "\n\n[UPCOMING EXPENSES \u2014 due on the 1st]\n"
            result += "\n".join(warning_lines)
            result += "\n  \u2192 Mention this to the player so they can downgrade or earn more before the 1st."
            result += "\n  \u2192 Use housing_pending / lifestyle_pending ops to schedule a tier change for next month."
            result += "\n[/UPCOMING EXPENSES]"

    # Bootstrap validation: warn if critical fields are missing (needed for expense consequence rolls)
    bootstrap_warnings = []
    for name, er in sorted(edgerunners.items()):
        missing = []
        if not er.get("body"):
            missing.append("body")
        if not er.get("endurance_base"):
            missing.append("endurance_base")
        if not er.get("hp", {}).get("max"):
            missing.append("hp.max")
        if missing:
            fields_hint = ", ".join(f"{f}: <value>" for f in missing)
            bootstrap_warnings.append(
                f"[WARNING: {name} missing {', '.join(missing)} — expense consequences will be skipped. "
                f'Set via: {{op: "set", edgerunner: "{name}", fields: {{{fields_hint}}}}}]'
            )
    if bootstrap_warnings:
        result += "\n\n" + "\n".join(bootstrap_warnings)

    return result


_CATEGORY_ABBREV = {"warrior": "WAR", "socializer": "SOC", "explorer": "EXP", "roleplayer": "ROL"}


def _safe_text(value):
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _category_abbrev(category):
    cat = _safe_text(category)
    if not cat:
        return "?"
    return _CATEGORY_ABBREV.get(cat, cat[:3].upper())


def _build_ip_tracker_injection(game_state):
    """Build [IP TRACKER] injection block from running scores and awards."""
    tracker = game_state.get("ip_tracker", {})
    if not isinstance(tracker, dict):
        return ""
    scores = tracker.get("session_scores", {})
    awards = tracker.get("awards", [])
    balances = tracker.get("balances", {})
    if not isinstance(scores, dict):
        scores = {}
    if not isinstance(awards, list):
        awards = []
    if not isinstance(balances, dict):
        balances = {}

    # Check if there's anything to show
    has_scores = False
    group_entry = scores.get("group", 0)
    group_val = group_entry.get("value", 0) if isinstance(group_entry, dict) else (group_entry if isinstance(group_entry, int) else 0)
    if group_val > 0:
        has_scores = True
    if not has_scores:
        for k, v in scores.items():
            if k == "group":
                continue
            if isinstance(v, dict) and any(isinstance(cv, dict) and cv.get("value", 0) > 0 for cv in v.values()):
                has_scores = True
                break
    has_balance = any(v > 0 for v in balances.values()) if balances else False
    if not has_scores and not awards and not has_balance:
        return ""

    lines = ["[IP TRACKER]"]

    # Group score
    if group_val > 0:
        group_reason = group_entry.get("reason", "") if isinstance(group_entry, dict) else ""
        reason_part = f" — {group_reason}" if group_reason else ""
        lines.append(f"Group: {group_val}{reason_part}")

    # Per-player scores
    for player_name in sorted(scores.keys(), key=lambda x: _safe_text(x)):
        if player_name == "group":
            continue
        player_cats = scores[player_name]
        if not isinstance(player_cats, dict):
            continue
        # Collect active categories sorted by score descending
        cat_parts = []
        for cat_name, entry in sorted(player_cats.items(), key=lambda x: -(x[1].get("value", 0) if isinstance(x[1], dict) else 0)):
            if not isinstance(entry, dict):
                continue
            val = entry.get("value", 0)
            if val <= 0:
                continue
            abbrev = _category_abbrev(cat_name)
            reason = _safe_text(entry.get("reason", ""))
            reason_part = f" ({reason[:60]})" if reason else ""
            cat_parts.append(f"{abbrev} {val}{reason_part}")
        if cat_parts:
            lines.append(f"  {_safe_text(player_name)}: {' | '.join(cat_parts)}")

    # Balances (only show players with IP > 0)
    if has_balance:
        bal_parts = [
            f"{_safe_text(name)}: {ip} IP"
            for name, ip in sorted(balances.items(), key=lambda kv: _safe_text(kv[0]))
            if isinstance(ip, int) and ip > 0
        ]
        lines.append(f"Balances: {' | '.join(bal_parts)}")

    # Prior awards
    if awards:
        lines.append("Prior awards:")
        for aw in awards:
            if not isinstance(aw, dict):
                continue
            parts = [f"Group {aw.get('group_ip', 0)}"]
            if aw.get("group_reason"):
                parts[0] += f" ({_safe_text(aw['group_reason'])[:40]})"
            individuals = aw.get("individual", [])
            if not isinstance(individuals, list):
                individuals = []
            for ind in individuals:
                if not isinstance(ind, dict):
                    continue
                name = _safe_text(ind.get("player", "?")) or "?"
                style_ip_raw = ind.get("style_ip", 0)
                style_ip = style_ip_raw if isinstance(style_ip_raw, int) else 0
                cat = ind.get("style_category", "?")
                abbrev = _category_abbrev(cat)
                total = aw.get("group_ip", 0) + style_ip
                parts.append(f"{name} +{style_ip} {abbrev} (={total})")
            lines.append(f"  S{aw.get('session', '?')}: {' | '.join(parts)}")

    lines.append("[/IP TRACKER]")
    return "\n".join(lines)
