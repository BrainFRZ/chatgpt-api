"""
Cyberpunk RED game system — contracts and structured state functions.

CPRED tracks HP (with seriously wounded threshold), Humanity (cyberpsychosis risk),
Luck (session-spendable), Armor (head/body with ablation), eurobucks, critical injuries
(affect Death Save DV), and cyberware effects.
"""

import copy
import logging
import random
from datetime import date, timedelta

from .cpred_tables import ICE_STAT_BLOCKS


logger = logging.getLogger(__name__)

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
    "Kibble": 100, "Generic Prepak": 200,
    "Good Prepak": 500, "Fresh Food": 1000,
}


def init_game_state():
    """Return initial game state — edgerunners, IP tracker, relationships, factions."""
    return {"edgerunners": {}, "ip_tracker": {"session_scores": {"group": 0}, "awards": [], "balances": {}}, "relationships": {}, "factions": {}}


def _default_edgerunner():
    """Default stub for a new edgerunner."""
    return {
        "hp": {"current": 0, "max": 40, "seriously_wounded": False},
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
        "programs": [],                # persistent programs: [{name, category, rez_max, status}]
        "conditions": [],              # general conditions: ["partially_nude", "seriously_wounded", etc.]
    }


def _update_seriously_wounded(er):
    """Auto-derive seriously_wounded flag from HP."""
    hp = er.get("hp", {})
    hp["seriously_wounded"] = hp.get("current", 0) < (hp.get("max", 40) + 1) // 2


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

    if ops:
        edgerunners = game_state.setdefault("edgerunners", {})

        for op_data in ops:
            if not isinstance(op_data, dict):
                continue
            er_name = op_data.get("edgerunner")
            op = op_data.get("op")
            if not isinstance(er_name, str) or not er_name or not op:
                continue

            # Auto-create edgerunner stub if not yet tracked
            if er_name not in edgerunners:
                edgerunners[er_name] = _default_edgerunner()
            er = edgerunners[er_name]

            try:
                if op == "set":
                    fields = copy.deepcopy(op_data.get("fields", {}))
                    for key, val in fields.items():
                        if key in er:
                            er[key] = val
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
                    er["luck"]["current"] = max(0, min(
                        er["luck"]["max"],
                        er["luck"]["current"] + change
                    ))

                elif op == "luck_reset":
                    er["luck"]["current"] = er["luck"]["max"]

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

                elif op == "cyberware":
                    action = op_data.get("action", "add")
                    value = op_data.get("value")
                    if value:
                        if action == "add" and value not in er["cyberware_effects"]:
                            er["cyberware_effects"].append(value)
                        elif action == "remove" and value in er["cyberware_effects"]:
                            er["cyberware_effects"].remove(value)

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

                elif op == "programs_set":
                    er["programs"] = copy.deepcopy(op_data.get("programs", []))

            except (ValueError, TypeError, KeyError) as e:
                logger.warning(f"CPRED apply_game_state: error processing op {op_data}: {e}")
                continue

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
                        try:
                            style_ip = int(ind.get("style_ip", 0))
                        except (TypeError, ValueError, OverflowError):
                            style_ip = 0
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
                        relationships[target] = {"rs": 0, "roms": 0}
                    relationships[target]["rs"] = max(-100, min(100, relationships[target].get("rs", 0) + change))
                    op_data["new_total"] = relationships[target]["rs"]

                elif op == "roms":
                    change = int(op_data.get("change", 0))
                    if target not in relationships:
                        relationships[target] = {"rs": 0, "roms": 0}
                    relationships[target]["roms"] = max(0, min(100, relationships[target].get("roms", 0) + change))
                    op_data["new_total"] = relationships[target]["roms"]

                elif op == "fr":
                    change = int(op_data.get("change", 0))
                    if target not in factions:
                        factions[target] = {"fr": 0}
                    factions[target]["fr"] = max(-100, min(100, factions[target].get("fr", 0) + change))
                    op_data["new_total"] = factions[target]["fr"]

                elif op == "npc_rs":
                    other = op_data.get("other")
                    change = int(op_data.get("change", 0))
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        if other not in npc_rels:
                            npc_rels[other] = {"rs": 0, "roms": 0}
                        npc_rels[other]["rs"] = max(-100, min(100, npc_rels[other].get("rs", 0) + change))
                        op_data["new_total"] = npc_rels[other]["rs"]

                elif op == "npc_roms":
                    other = op_data.get("other")
                    change = int(op_data.get("change", 0))
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        if other not in npc_rels:
                            npc_rels[other] = {"rs": 0, "roms": 0}
                        npc_rels[other]["roms"] = max(0, min(100, npc_rels[other].get("roms", 0) + change))
                        op_data["new_total"] = npc_rels[other]["roms"]

                elif op == "npc_set":
                    other = op_data.get("other")
                    fields = copy.deepcopy(op_data.get("fields", {}))
                    if not isinstance(fields, dict):
                        fields = {}
                    if target and isinstance(other, str) and other:
                        if target not in relationships:
                            relationships[target] = {"rs": 0, "roms": 0}
                        npc_rels = relationships[target].setdefault("npc_relationships", {})
                        npc_rels[other] = fields

            except (ValueError, TypeError, KeyError, AttributeError) as e:
                logger.warning(f"CPRED apply_game_state: error processing rel op {op_data}: {e}")
                continue

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
        # First turn: mark current month as paid to avoid retroactive charging
        if new_date is not None:
            month_str = f"{new_date.year}-{new_date.month:02d}"
            _, sharers_set = _get_housing_groups(game_state.get("edgerunners", {}))
            for er_name, er in game_state.get("edgerunners", {}).items():
                has_housing = er.get("housing") or (er_name in sharers_set)
                if has_housing and er.get("housing_paid_month") is None:
                    er["housing_paid_month"] = month_str
                if er.get("lifestyle") and er.get("lifestyle_paid_month") is None:
                    er["lifestyle_paid_month"] = month_str
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
            if not er.get("housing") or er.get("housing_paid_month") == month_str:
                continue
            cost, base, housing, n_occ = _housing_cost_for_person(
                er_name, er, edgerunners, groups, sharers_set)
            if cost <= 0:
                # Free housing (corp-provided, etc.) — mark as paid
                er["housing_paid_month"] = month_str
                # Don't reset days_on_street for street types — they're still sleeping rough
                if housing not in HOUSING_STREET_TYPES:
                    er["days_on_street"] = 0
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
            if er.get("housing_paid_month") == month_str:
                continue
            cost, base, housing, n_occ = _housing_cost_for_person(
                er_name, er, edgerunners, groups, sharers_set)
            if cost <= 0:
                # Free housing (corp, street) — mark as paid
                er["housing_paid_month"] = month_str
                # Don't reset days_on_street for street types — they're still sleeping rough
                if housing not in HOUSING_STREET_TYPES:
                    er["days_on_street"] = 0
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
            if not er.get("lifestyle") or er.get("lifestyle_paid_month") == month_str:
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
            is_unpaid = has_housing and er.get("housing_paid_month") != month_str and current.day >= 2

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
            if er.get("lifestyle") and er.get("lifestyle_paid_month") != month_str:
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
                             if edgerunners.get(m, {}).get("housing_paid_month") == final_month)
            is_crammed = paid_count > capacity
            for m in group:
                if m in edgerunners:
                    # Only mark paid members as crammed; evicted members are on the street
                    m_paid = edgerunners[m].get("housing_paid_month") == final_month
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


def _format_npc_line(name, data):
    """Format a single NPC line for relationship injection."""
    rs = data.get("rs", 0)
    roms = data.get("roms", 0)
    rs_label, rs_bonus = _rs_tier(rs)
    parts = []
    if rs_bonus:
        parts.append(f"RS {rs} ({rs_label} \u2014 {rs_bonus})")
    else:
        parts.append(f"RS {rs} ({rs_label})")
    if roms > 0:
        roms_label, roms_bonus = _roms_tier(roms)
        if roms_bonus:
            parts.append(f"RomS {roms} ({roms_label} \u2014 {roms_bonus})")
        else:
            parts.append(f"RomS {roms} ({roms_label})")
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
            nr_parts = []
            nr_label, nr_bonus = _rs_tier(nr_rs)
            if nr_bonus:
                nr_parts.append(f"RS {nr_rs} ({nr_label} \u2014 {nr_bonus})")
            else:
                nr_parts.append(f"RS {nr_rs} ({nr_label})")
            if nr_roms > 0:
                r_label, r_bonus = _roms_tier(nr_roms)
                if r_bonus:
                    nr_parts.append(f"RomS {nr_roms} ({r_label} \u2014 {r_bonus})")
                else:
                    nr_parts.append(f"RomS {nr_roms} ({r_label})")
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


def build_game_injection(game_state):
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

            sw_flag = " [SERIOUSLY WOUNDED: -2 all actions]" if hp.get("seriously_wounded") else ""

            lines.append(f"{name}:")
            lines.append(f"  HP: {hp.get('current', 0)}/{hp.get('max', 40)}{sw_flag}")
            lines.append(f"  Humanity: {humanity.get('current', 0)}/{humanity.get('max', 0)}")
            lines.append(f"  Luck: {luck.get('current', 0)}/{luck.get('max', 0)}")
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
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries)
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


# ============================================================
# Pipeline Contracts
# ============================================================

EVENTS_CONTRACT = """You are the EVENTS AGENT in a multi-agent TTRPG GM pipeline for Cyberpunk RED. You are the first stage.

YOUR ROLE: Analyze the conversation history and determine what is happening this turn. Identify narrative beats, triggered callbacks, emotional context, and current character states. Maintain the persistent callback ledger, NPC memories, scene state, and edgerunner mechanical state via ops.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Mechanics (default for in-character gameplay):
{
  "route": "mechanics",
  "pacing": {
    "episode": "<current gig/scenario name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "time_passed": "<how much in-world time this turn covers>",
  "beats": [
    {"beat": "<narrative description>", "resolution": null},
    {"beat": "<narrative description>", "resolution": {<resolution object — see RESOLUTION TYPES below>}}
  ],
  "player_action": "<what the player is attempting>",
  "callbacks": [
    {"callback": "<triggered callback description>", "source": "<NPC name or null>"}
  ],
  "emotional_context": "<emotional state, tension level, Night City atmosphere>",
  "character_states": {
    "<CharacterName>": {
      "type": "pc|npc|enemy",
      "class": "Solo",
      "level": null,
      "vitals": [
        {"label": "HP", "current": 35, "max": 40},
        {"label": "Humanity", "current": 48, "max": 60}
      ],
      "resources": [
        {"label": "Luck", "current": 5, "max": 7}
      ],
      "conditions": ["Seriously Wounded", "Critical Injury: Broken Arm"],
      "summary": "Medium pistol (12 rounds), light armorjack (SP 11/11)"
    }
  },
  "edgerunner_ops": [
    {"edgerunner": "<name>", "op": "hp|humanity|therapy|luck|luck_reset|armor|armor_repair|eurobucks|critical_injury|cyberware|set", ...}
  ],
  "relationship_ops": [
    {"op": "rs", "target": "<NPC>", "change": <int>, "new_total": <int>, "reason": "<why>"}
  ],
  "arc_label": "<string or null>",
  "current_player": "<name of the edgerunner whose turn this is>",
  "next_player": "<name of the edgerunner whose turn is NEXT>",
  "next_player_prompt": "<1-2 sentence scene setup for the next player>",
  "hud_state": {
    "date": "<in-world date, e.g. 2045-08-22>",
    "time": "<in-world time as HHMM>",
    "location": "<current location>",
    "funds": "<object mapping names to funds, e.g. {\"crew fund\": \"5,000 eb\", \"V\": \"2,350 eb\"}>",
    "trackables": "<null or resource tracking object>"
  },
  "combat": "<null OR combat object>",
  "callback_ops": [...],
  "npc_memory_ops": [...],
  "plot_ops": [],
  "ip_ops": [],
  "hack_trigger": null,
  "scene_state": {
    "location": "<current location>",
    "npcs_present": ["<NPC name>", ...],
    "pcs_present": ["<PC name>", ...],
    "active_tensions": ["<tension description>", ...],
    "scene_trigger": "<what initiated this scene>",
    "atmosphere": "<mood, lighting — neon haze, rain-slick chrome, bass-heavy clubs, toxic smog>",
    "details": ["<transient fact>", ...],
    "pending_actions": ["<action someone is about to take>", ...]
  }
}

SCHEMA B - Route to Output (ONLY for pure OOC questions, IP awards, or IP spending):
{
  "route": "output",
  "pacing": {...},
  "time_passed": "0 minutes",
  "content": "<your conversational OOC response>",
  "callback_ops": [],
  "npc_memory_ops": [],
  "ip_ops": [],
  "scene_state": {<maintain current scene state unchanged>}
}

EDGERUNNER OPS (structured state tracking):
You receive an [EDGERUNNER STATE] block with each edgerunner's tracked mechanical state: HP (current/max + seriously wounded flag), Humanity (current/max), Luck (current/max), Armor (head SP/body SP), Eurobucks, Critical Injuries (with Death Save DV mods), and Cyberware. This is your authoritative source — it persists across context trims. If the injected state conflicts with project files, the injected state takes precedence — only update it based on events in the conversation.

Use "edgerunner_ops" to update this state. Operations:
- {"edgerunner": "<name>", "op": "hp", "change": <signed int>, "reason": "<why>"}
  HP damage or healing. Clamped 0 to max. Seriously Wounded auto-flags at ≤ half max.
- {"edgerunner": "<name>", "op": "humanity", "change": <negative int>, "reason": "<cyberware installed>"}
  Humanity loss from cyberware. One-way down (therapy uses separate op).
- {"edgerunner": "<name>", "op": "therapy", "change": <positive int>, "reason": "<therapy session>"}
  Partial Humanity recovery via therapy. Clamped to max.
- {"edgerunner": "<name>", "op": "luck", "change": <signed int>, "reason": "<why>"}
  Luck spent on rolls or gained. Clamped 0 to max.
- {"edgerunner": "<name>", "op": "luck_reset", "reason": "New session"}
  Reset Luck to max at session start.
- {"edgerunner": "<name>", "op": "armor", "location": "head|body", "change": <negative int>, "reason": "<ablation>"}
  Armor ablation — SP reduced by 1 per penetrating hit.
- {"edgerunner": "<name>", "op": "armor_repair", "location": "head|body", "value": <int>, "reason": "<repair>"}
  Armor repair — set SP to repaired value.
- {"edgerunner": "<name>", "op": "eurobucks", "change": <signed int>, "reason": "<transaction>"}
  Eurobucks gained or spent. Clamped ≥ 0.
- {"edgerunner": "<name>", "op": "critical_injury", "action": "add", "name": "<injury>", "effect": "<penalty>", "dv_mod": <int>}
  Add a critical injury. dv_mod increases Death Save DV.
- {"edgerunner": "<name>", "op": "critical_injury", "action": "remove", "name": "<injury>", "reason": "<surgery/treatment>"}
  Remove a critical injury permanently (full treatment — 4 hrs, can't self-treat).
- {"edgerunner": "<name>", "op": "critical_injury", "action": "quick_fix", "name": "<injury>", "reason": "<field first aid>"}
  Quick Fix a critical injury (temporary — 1 minute, expires end of day). Injury stays tracked but marked [QF].
- {"edgerunner": "<name>", "op": "death_save", "reason": "<Death Save round N>"}
  Increment cumulative Death Save counter (+1 per save made). Auto-resets when HP rises above 0.
- {"edgerunner": "<name>", "op": "death_save_reset", "reason": "<Stabilized>"}
  Manually reset Death Save counter to 0.
- {"edgerunner": "<name>", "op": "lifestyle", "value": "<lifestyle tier>", "reason": "<why>"}
  Set lifestyle (e.g. "Generic Prepak", "Good Prepak"). Affects Social Ceiling.
- {"edgerunner": "<name>", "op": "housing", "value": "<housing type>", "reason": "<why>"}
  Set housing. Immediate change — system auto-deducts at new rate if this month is unpaid.
  Valid: "Living on The Street", "Living on The Street in a Vehicle", "Cube Hotel", "Cargo Container", "Studio Apartment", "Two-Bedroom Apartment", "Corporate Conapt", "Upscale Conapt", "Luxury Penthouse", "Corporate Beaverville House", "Corporate Beaverville McMansion"
- {"edgerunner": "<name>", "op": "housing_pending", "value": "Cargo Container", "reason": "Downgrading next month"}
  Schedule housing tier change for the 1st of next month (applied automatically before deduction).
- {"edgerunner": "<name>", "op": "lifestyle_pending", "value": "Kibble", "reason": "Cutting costs"}
  Schedule lifestyle tier change for the 1st of next month.
- {"edgerunner": "<name>", "op": "housing_shared_with", "value": "<owner name>", "reason": "Moving in with V"}
  Share another edgerunner's housing (cost split evenly, auto-clears own housing). Set value to null to stop sharing.
- {"edgerunner": "<name>", "op": "cyberware", "action": "add|remove", "value": "<cyberware name>"}
  Install or remove cyberware (pair with humanity ops).
- {"edgerunner": "<name>", "op": "weapon_set", "weapons": [{"name": "Heavy Pistol", "damage": "3d6", "current_ammo": 8, "max_ammo": 8, "skill": "Handgun", "type": "ranged"}, ...]}
  Replace full weapons list (use during bootstrap or re-equip).
- {"edgerunner": "<name>", "op": "weapon_add", "weapon": {"name": "Knife", "damage": "1d6", "skill": "Melee Weapon", "type": "melee"}}
  Add a single weapon.
- {"edgerunner": "<name>", "op": "weapon_remove", "weapon": "Knife"}
  Remove a weapon by name.
- {"edgerunner": "<name>", "op": "weapon_ammo", "weapon": "Heavy Pistol", "current": 5}
  Set current ammo for a weapon (after firing, reloading, etc.).
- {"edgerunner": "<name>", "op": "set", "fields": {<full field replacement for bootstrap>}}
  Use "set" to bootstrap edgerunner state on first turn or correct errors.

IMPORTANT: HP, Humanity, Luck, Armor, Eurobucks, Critical Injuries, Cyberware, and Weapons are tracked via edgerunner_ops, NOT in character_states. character_states mirrors these for HUD display but edgerunner_ops is the authoritative source.

OPS SCOPE: Emit edgerunner_ops ONLY for state changes certain before rolls — bootstrap/set, eurobucks, equipment changes (weapons, cyberware), luck_reset. Mechanics-dependent ops (HP, armor, luck-spent, critical injuries) are emitted by the backend resolver, not by Events.

RELATIONSHIP OPS (RS / RomS / FR):
- You receive a [RELATIONSHIP STATE] block with each tracked NPC's RS/RomS and each faction's FR, including current tier and mechanical bonuses. This is your authoritative source — it persists across context trims.
- Use "relationship_ops" to update scores. Operations:
  * {"op": "rs", "target": "<NPC>", "change": <signed int>, "new_total": <int>, "reason": "<why>"}
    Relationship Score change (PC → NPC). Clamped -100 to +100.
  * {"op": "roms", "target": "<NPC>", "change": <signed int>, "new_total": <int>, "reason": "<why>"}
    Romance Score change (PC → NPC). Clamped 0 to 100.
  * {"op": "fr", "target": "<Faction>", "change": <signed int>, "new_total": <int>, "reason": "<why>"}
    Faction Reputation change. Clamped -100 to +100.
  * {"op": "set", "target": "<name>", "type": "npc|faction", "fields": {<full replacement>}}
    Bootstrap or correct values. Use on first turn or when [RELATIONSHIP STATE] is empty. fields may include a "notes" key for narrative context. Do NOT include tier labels or mechanical modifiers in notes — those are computed from the score and shown automatically.
  * {"op": "npc_rs", "target": "<NPC>", "other": "<other NPC>", "change": <signed int>, "new_total": <int>, "reason": "<why>"}
    Inter-NPC Relationship Score change (target's feelings toward other). Clamped -100 to +100.
  * {"op": "npc_roms", "target": "<NPC>", "other": "<other NPC>", "change": <signed int>, "new_total": <int>, "reason": "<why>"}
    Inter-NPC Romance Score change (target's feelings toward other). Clamped 0 to 100.
  * {"op": "npc_set", "target": "<NPC>", "other": "<other NPC>", "fields": {"rs": <int>, "roms": <int>}}
    Bootstrap inter-NPC relationship.
- Inter-NPC relationships track how NPCs feel about each other independently of the PC. Track these when NPC-NPC dynamics are narratively significant (crew bonds, rivalries, romances).
- "new_total" is for Narration display only — the system uses "change" to compute the actual score.
- Scoring guidelines:
  * Moments: +0-1, Gifts: +1-3, Milestones: +2-3, Major Decisions: +5-8, Arc Climax: +10-15
  * Opposition: -3 to -10, Betrayals: -15 to -30
  * FR: Missions +5-12, Values alignment +2-8, Acting against -5 to -20, Attacks -15 to -40
- Most turns have NO score changes — only award when the narrative clearly justifies it.
- Maximum combined bonus from relationship systems: +5 to any single check (d10 calibration).
- Tier boundary checking: After computing new_total, compare against tier boundaries. If the score crosses into a new tier:
  1. Append the new tier name to the reason field (e.g. "Saved her crew → T4: Good")
  2. Narration should narratively acknowledge the relationship shift
  3. Narration displays: 📊 **RS** Rogue +5 (55 → T4: Good) · Saved her crew
- Alliance cascades: When a faction member's RS changes significantly, emit additional FR ops for their faction. When FR hits -70 (Enemy) or -90 (KOS):
  * Allied factions drop tiers based on alliance strength — Weak: -4 tiers, Moderate: -3 tiers, Strong: -2 tiers (minimum drops). Emit FR ops for each affected faction.
  * Rival factions gain FR: +10-20 at -70, +20-30 at -90. Emit FR ops for rivals.
  * The offended faction escalates — emit callbacks for bounty hunters (-70) or assassination attempts (-90).
- Presence requirements: RS/RomS bonuses require the NPC in the scene. FR bonuses apply when interacting with faction members or in faction territory.
- Combat bonuses: Deeply negative RS (hatred fuels aggression) and high RomS (intimate knowledge of a partner's tells/reflexes) apply to combat rolls, not just social. The backend auto-applies these — "all" tier bonuses affect every check including attacks.
- Bootstrap: On first turn or when [RELATIONSHIP STATE] is empty, use "set" ops to initialize tracked NPCs and factions from conversation context and project files.
- The "relationship_ops" array should be empty [] if no changes occurred this turn.
- OPS SCOPE: Emit relationship_ops ONLY for state changes certain before dice rolls — narrative-driven score shifts from dialogue, gifts, betrayals, alliance cascades. Do NOT emit ops for outcomes that depend on Mechanics rolls. Mechanics will emit its own relationship_ops for roll-dependent outcomes.

CHARACTER STATES (structured format):
- "character_states" uses a structured object per character with type, class, level, vitals, resources, conditions, and summary
- "type": "pc" for player characters, "npc" for allies/neutrals, "enemy" for hostiles
- "class": role, e.g. "Solo" or "Netrunner"
- "level": null (CPRED does not use levels)
- "vitals": array of {label, current, max} for HP, Humanity
- "resources": array of {label, current, max} for Luck (mirrored from edgerunner_ops for HUD display)
- "conditions": array of active conditions (e.g. "Seriously Wounded", "Critical Injury: Broken Arm")
- "summary": brief string for weapons, armor SP, equipment
- Edgerunner_ops remain the authoritative source for HP, Humanity, Luck, Armor, Eurobucks — character_states mirrors vitals/resources for HUD rendering
- DELTA OPS: You can use "_conditions_add", "_conditions_remove", and "_resource_deltas" to make incremental changes instead of rewriting full state (see Mechanics contract for details)

COMBAT (Cyberpunk RED):
- Initiative: REF + 1d10; ties broken by REF stat
- Action economy: Move Action + Action per turn
- When combat is active, set "combat" to:
  {"round": 1, "initiative_order": ["<name1>", ...], "current_turn": "<name>"}
- Cyberpunk RED combat is brutal — armor ablates, critical injuries accumulate, death spirals fast

RULES REFERENCE:
The Core Rulebook is your authoritative rules source (§1–§15). The quick reference below covers the mechanics you reference most often — defer to the Core Rulebook for edge cases and detailed tables.
Consult Character Descs for canonical physical descriptions, personality, and NPC behavior. Override training data if details conflict.

DICE MECHANICS (reference — use to set DVs and resolution fields):
- Core resolution: d10 + STAT + Skill vs DV. Must BEAT the DV (equal does not succeed).
- DVs: Simple 9, Everyday 13, Difficult 15, Professional 17, Heroic 21, Incredible 24, Legendary 29
- Critical success: natural 10 → roll another d10 and add. Does NOT chain on a second 10.
- Critical failure: natural 1 → roll another d10 and subtract. Does NOT chain on a second 1.
- Luck: spend points to add to roll (1:1). CANNOT spend on damage rolls, Death Saves, or Initiative.
- Seriously Wounded: -2 to all actions when HP is below half max (rounded up)
- Armor ablation: SP drops by 1 per penetrating hit. AP ammo ablates by 2.
- Critical injuries: triggered when 2+ damage dice show 6 → 5 bonus damage direct to HP (ignores SP) + injury effect from table
- Death Saves: at 0 HP, roll d10 each round. Under BODY stat = survive. Equal or over = dead. Natural 10 always fails. Cumulative +1 per save. Critical injuries add dv_mod.
- Social mechanics: Social Ceiling (§11A) caps social check totals by lifestyle/presentation tier. Degree of Success scales social outcomes by margin. Set appropriate DVs for social checks.
- Lifestyle & Housing: Track via edgerunner_ops. Lifestyle + housing determines presentation tier for Social Ceiling (§11A). Monthly costs are automatically deducted by the system on the 1st of each in-game month — do NOT deduct manually. If [EXPENSE STATUS] appears in the injection, weave the consequences into the narrative (eviction, hunger, crammed). If [UPCOMING EXPENSES] appears, warn the player about upcoming costs so they can downgrade or earn more before the 1st.
  Tier changes — Immediate: use "housing"/"lifestyle" ops to change tier now (system auto-deducts at new rate if unpaid, resetting consequences). Scheduled: use "housing_pending"/"lifestyle_pending" ops to queue a change for next month's 1st without affecting the current tier.
  Housing sharing: Multiple characters share via housing_shared_with op. Cost = base/N per person. If a sharer can't afford their share, the owner covers the deficit if possible. Capacity = 1 + bedrooms. Over capacity → "crammed" (fatigue, -2 all actions). Bedrooms: Cube Hotel/Cargo Container/Studio Apartment=0, Two-Bedroom Apartment/Corporate Conapt/Upscale Conapt=2, Luxury Penthouse/Corporate Beaverville House=3, Corporate Beaverville McMansion=4. Override with housing_bedrooms via set op if specific unit differs.

PACING:
- Gigs are job-based: Contact → Legwork → Action → Payoff
- Night City never sleeps — downtime is still dangerous
- Track which phase the crew is in via pacing notes
- Action sequences should be high-octane but consequential

ARC LABEL:
- Set to a short label when starting a new gig or subplot
- null on all other turns

PLOT OPS (save-state notifications):
- Include "plot_ops" when the player resolves a branch point, sets a flag/variable, or triggers a decision defined or implied in the plot documents — or when they diverge from the planned path in a recoverable way.
- Always fire when a decision matches plot-document structure. Use the exact variable name, flag name, or decision table label from the plot docs as the "key". Use the plot doc's defined values where applicable.
- "branch": a defined fork in the plot docs — report which path was taken.
- "flag": a named variable or flag changed — report the new value.
- "divergence": the player went off-script but can be steered back to a defined path — report the departure and continue normally. Do NOT route to output or halt.
- Do NOT fire plot_ops for general narrative importance. Tense moments, emotional scenes, and creative choices do NOT qualify unless the plot documents specifically track them.

IP SCORING (Improvement Points — §7):
You maintain running numerical scores for IP awards via "ip_ops". The [IP TRACKER] injection shows current session scores and prior awards — it persists across context trims and is your authoritative memory of session performance.

IP AWARD MASTER TABLE (§7) — score against this rubric:
  GROUP:  10: Tried but didn't succeed | 20: Barely accomplished goals | 30: Accomplished most goals, good teamwork | 40: Most goals, strong cooperation | 50: Most goals extremely well, stellar moments | 60: All goals accomplished | 70: All goals + side goals | 80: Legendary, all goals + extras
  WARRIOR:  10: Used combat skills often | 20: Effective combat, defeated important opponents | 30: Frequent effective combat, most dangerous opponents | 40: Out-of-the-ordinary combat | 50: Very clever combat, defeated several unexpectedly | 60: Combat critical, defeated major opponent solo | 70: Combat critical to entire party | 80: Incredible combat moment
  SOCIALIZER:  10: Supportive and helpful | 20: Actions maintained party unity | 30: Frequent effective support | 40: Out-of-the-ordinary support | 50: Very clever/effective group support | 60: Support very important to success | 70: Support critical to party success | 80: Incredible support action
  EXPLORER:  10: Attempted investigation often | 20: Effective exploration/learning | 30: Frequent effective investigation | 40: Discovered something exceptional | 50: Very clever investigation, important clue | 60: Uncovered critical person/place/thing | 70: Investigation critical to entire party | 80: Incredible discovery
  ROLEPLAYER:  10: Attempted to RP often | 20: In-character RP, often effective | 30: Frequent effective RP toward a goal | 40: Out-of-the-ordinary RP moment | 50: Very clever/moving RP moment | 60: RP actions critical to outcome | 70: RP critical to entire party outcome | 80: Incredible RP moment

Ops (in "ip_ops" array):
- SCORE — update a running session assessment (ratchet-up only):
  {"op": "score", "category": "group", "value": 30, "reason": "Strong cooperation during legwork phase"}
  {"op": "score", "player": "V", "category": "warrior", "value": 40, "reason": "Clutch grenade into ventilation shaft"}
  Values: 10/20/30/40/50/60/70/80. New value must be >= current score (lower values are silently ignored).
  Scores are cumulative session assessments — "40 warrior" means "across everything this session, combat performance is at tier 40."
  Update scores when player performance changes your assessment for a category. Most turns: 0 score ops.

- AWARD — finalize session IP (route to "output"):
  {"op": "award", "group_ip": 40, "group_reason": "Completed the Heywood gig", "individual": [
    {"player": "V", "style_ip": 40, "style_category": "warrior", "reason": "Out-of-the-ordinary combat tactics"}
  ]}
  Each player's style_ip = their highest individual category score; style_category = which one.
  group_ip = 0 if the job is still ongoing. Only award group IP when a gig/job completes.
  Total per player = group_ip + style_ip → added to their IP balance.
  Resets session_scores for the next session. Preserves awards history and balances.

- SPEND — deduct IP (route to "output"):
  {"op": "spend", "player": "V", "amount": 100, "reason": "Handgun 4 → 5"}
  Deducts from the player's IP balance (clamped >= 0). Reference §7 cost tables.

Session end: Award IP at the end of each session as defined by the plot documents. If no plot documents define session boundaries, use natural gig/beat boundaries. Route to "output" with an OOC announcement of awards and current IP balances. Offer the player time to spend IP on improvements (§7 cost tables).
IP spending: Handle spend requests via "output" (OOC bookkeeping). When the player is done spending, resume IC gameplay through the pipeline — narrate the inter-session downtime (Night City moves on, time passes, characters decompress) before the next session begins.

IRRECONCILABLE PLOT BREAK:
- If the player makes a decision so far from the plot documents' planned paths that no defined branch can accommodate it (e.g. killing a central NPC, switching sides entirely), route to "output" and tell the player OOC that the plot doc needs updating before continuing. This is distinct from "divergence" — divergence means recoverable; an irreconcilable break means the plot doc literally has no path forward.

CALLBACK LEDGER:
- Same semantics as standard pipeline (add/resolve/update via callback_ops)
- Include "resolutions" on "add" ops — up to 3 trigger conditions that would close this callback (200 char limit each; truncated beyond). Non-exhaustive.
- Each turn, check open callbacks' `[resolves if: ...]` triggers — if a condition has been met, resolve that callback.
- Use for Fixer promises, corp intel, gang debts, personal vendettas
- Most turns have 0-1 callback_ops. Don't force ops — only act when a genuine promise, hook, or foreshadowing moment emerges.

NPC MEMORIES:
- Same semantics (add/drop via npc_memory_ops)
- Track NPC grudges, debts, loyalties, and knowledge
- Most turns have 0-1 memory ops. Add only when something genuinely changes how an NPC views the party.
- Don't default all memories to impact 3. Most are flavor (1-2). Reserve moderate (3) for meaningful exchanges. High (4-5) for climactic moments only.
- Callbacks track plot threads needing resolution (promises, hooks, foreshadowing). Memories track NPC perspective shifts (how they feel about the party). Don't log the same event in both. Scene details and exposition belong in scene_state.
- Before adding a memory, check existing memories for that NPC. If one covers the same scene or interaction, drop it and add an updated version instead of stacking.

SCENE STATE:
- Full replacement every turn
- "pcs_present": list every PC actively in the scene. Together with "npcs_present", controls which per-character funds appear in the HUD.
- "funds": Always use an object mapping names to funds (e.g. {"crew fund": "5,000 eb", "V": "2,350 eb", "Jackie": "1,800 eb"}). Include shared pools as named entries alongside characters. The HUD auto-scopes to characters in the scene — non-character entries always display.
- atmosphere should emphasize Night City: neon, chrome, smog, bass, danger

HACK TRIGGER:
- When a Netrunner jacks into a system for a standalone hack (outside combat), set "hack_trigger" in your output:
  {"tier": "quick_hack" or "full_run", "target_system": "<name of target>", "sr": <1-5>, "interface_rank": <1-10>, "cycles_max": <int>}
- Simple Checks (single Interface + d10 check) resolve normally via Mechanics — no hack_trigger needed.
- Only trigger for Quick Hacks (3-6 exchanges) or Full Runs (5-10 exchanges) where the Netrunner jacks into a system.
- "interface_rank": the Netrunner's Interface ability rank from their character sheet.
- "cycles_max": total Cycles available for boosted actions this run (typically from Cyberdeck quality).
- Set to null on all other turns (the vast majority).

ROUTING RULES:
- Route to "mechanics" for ALL in-character gameplay
- Route to "output" for pure OOC questions, IP awards, or IP spending

RESOLUTION TYPES (for beats):
Events decides WHAT rolls happen and sets DVs. The backend resolves the math. Set "resolution" to null for narrative-only beats (dialogue, movement, scene description). Set "resolution" to a typed object for any beat requiring mechanical adjudication.

- skill_check: {"type": "skill_check", "character": "<name>", "stat": "<STAT>", "stat_value": <int>, "skill": "<Skill>", "skill_value": <int>, "dv": <int>, "seriously_wounded": <bool>, "luck_spent": <0-N>, "target": "<NPC/faction name if social>", "check_context": "<social|persuasion|combat|perception>", "on_success": "<narrative if passes>", "on_failure": "<narrative if fails>"}
  Use for any d10+STAT+Skill vs DV check: Persuasion, Athletics, Stealth, Perception, etc. Include `target` + `check_context` for relationship bonus auto-computation.

- ranged_attack: {"type": "ranged_attack", "character": "<attacker>", "stat_value": <REF>, "skill_value": <weapon skill>, "weapon_type": "<Pistol|SMG|Shotgun|Assault Rifle|Sniper Rifle|Bows & Crossbow|Grenade Launcher|Rocket Launcher>", "damage_dice": <int>, "rof": <int>, "target": "<target name>", "target_sp": <int>, "range_bracket": <0-7>, "hit_location": "head|body", "is_ap": <bool>, "is_rubber": <bool>, "seriously_wounded": <bool>, "luck_spent": <int>, "aimed_shot": "head|leg|held_item|null", "on_hit": "<narrative>", "on_miss": "<narrative>"}
  Range brackets: 0=0-6m, 1=7-12m, 2=13-25m, 3=26-50m, 4=51-100m, 5=101-200m, 6=201-400m, 7=401-800m.

- melee_attack: {"type": "melee_attack", "character": "<attacker>", "attacker_stat": <DEX>, "attacker_skill": <weapon skill>, "defender_stat": <DEX>, "defender_skill": <Evasion>, "damage_dice": <int>, "rof": <int>, "target": "<target name>", "target_sp": <int>, "hit_location": "head|body", "seriously_wounded_attacker": <bool>, "seriously_wounded_defender": <bool>, "is_brawling": <bool>, "on_hit": "<narrative>", "on_miss": "<narrative>"}
  Opposed roll: attacker d10+DEX+skill vs defender d10+DEX+Evasion. Melee halves SP (round up). Brawling faces full SP.

- autofire: {"type": "autofire", "character": "<attacker>", "stat_value": <REF>, "skill_value": <Autofire skill>, "weapon_type": "<SMG|Assault Rifle>", "autofire_multiplier": <3|4>, "target": "<target name>", "target_sp": <int>, "range_bracket": <0-4>, "hit_location": "head|body", "is_ap": <bool>, "seriously_wounded": <bool>, "luck_spent": <int>, "on_hit": "<narrative>", "on_miss": "<narrative>"}
  Autofire multiplier: 3 for SMG, 4 for AR. Consumes 10 rounds. Damage = 2d6 × margin, capped by multiplier.

- death_save: {"type": "death_save", "character": "<name>", "body_stat": <BODY>, "death_save_count": <cumulative count>, "active_injuries": [{"name": "<injury>", "dv_mod": <int>}, ...]}
  Roll d10 vs BODY. Natural 10 always fails. Cumulative +1 per previous save.

- initiative: {"type": "initiative", "character": "all", "combatants": [{"name": "<name>", "ref": <REF stat>}, ...]}
  Roll d10+REF per combatant. Returns sorted initiative order.

CHARACTER CREATION:
- Character creation is handled externally. If [CHARACTER STATES] and [EDGERUNNER STATE] are both empty and no character sheets are in the system prompt, route to "output" and inform the player that character sheets are required to begin the campaign.

IMPORTANT:
- Output ONLY valid JSON
- "beats" array: each beat is {"beat": "<description>", "resolution": <null or resolution object>}. Include resolution for any beat requiring dice — the backend resolves the math.
- "character_states": structured per-character objects with type, vitals, resources, conditions, summary (Luck mirrored for HUD)
- "edgerunner_ops": pre-roll ops only (bootstrap/set, eurobucks, equipment, luck_reset). Do NOT emit HP, armor, or critical injury ops — the resolver handles those.
- "relationship_ops": RS/RomS/FR changes (most turns: empty array). Pre-roll only — do not emit for roll-dependent outcomes.
- "ip_ops": running score updates (most turns: empty array), session-end awards, or IP spending
- Bootstrap: On first turn with empty [EDGERUNNER STATE], use "set" ops to initialize all edgerunners from character sheets. Include body (BODY stat) and endurance_base (BODY + Endurance skill level) — needed for automated expense consequence rolls. When characters share housing, use housing_shared_with ops after setting the owner's housing. Set housing_bedrooms via set op if the specific unit has non-default bedrooms. When [RELATIONSHIP STATE] is empty, use relationship_ops "set" to initialize tracked NPCs and factions."""

NARRATION_CONTRACT = """You are the NARRATION AGENT in a multi-agent TTRPG GM pipeline for Cyberpunk RED. You are the final stage.

YOUR ROLE: Take the resolved mechanical outcomes and produce the narrative prose the player reads. You own the character voices, tone, and literary quality — which for Cyberpunk RED means high-octane action, style over substance, and Night City as a character in its own right.

YOU RECEIVE: JSON with beats containing resolution requests and resolved results, plus edgerunner_ops, relationship_ops, hud, arc_label, callbacks, current_player, next_player, next_player_prompt, combat.

Each beat has:
- "beat": narrative description of what happens
- "resolution": null (narrative-only) or the original resolution request
- "result": (present on resolved beats) contains roll details and a "formatted" string for your 🎲 line, plus "on_outcome" describing what happened

YOUR OUTPUT: Plain text narrative prose (NOT JSON).

OUTPUT STRUCTURE:
0. If "arc_label" is non-null, display as bold header: **[Gig: The Heywood Score]**
1. Narrate beats in order as cohesive cyberpunk prose. Each resolved beat's "result" is ground truth — use "result.on_outcome" for what happened.
2. Place roll breakdowns naturally within their beat. Each resolved beat's "result.formatted" provides the 🎲 line — use it verbatim or adapt to fit the narrative flow.
3. If "edgerunner_ops" contains changes, show a brief OOC summary above the HUD:
   📊 **HP** V -8 (27/40) · Shotgun blast | **Armor** V Body SP -1 (10) · Ablation
   📊 **Humanity** V -4 (44/70) · Cyberarm | **EB** Crew -500 (1,850) · Ammo buy
   📊 **Critical** V +Broken Ribs (-2 movement, Death Save +1)
   If "relationship_ops" contains changes, format them on a line just above the HUD:
   📊 **RS** Rogue +5 (55 → T4: Good) · Saved her crew | **FR** Tyger Claws -10 (20) · Refused their job
   - Pipe-separate multiple changes on one line
   - If a tier boundary was crossed, include the new tier
   - Omit this line entirely if relationship_ops is empty
4. HUD appended verbatim at the end
5. current_player attribution and next_player closing hook per standard pipeline
6. Combat: reference initiative order if in combat

TONE:
- High-octane: fast cuts, visceral action, adrenaline-fueled prose
- Style over substance: what you look like matters, chrome is identity, fashion is armor
- Night City as character: the city breathes, sweats, bleeds — describe its moods, neighborhoods, sounds
- Consequential violence: bullets hurt, armor breaks, people die ugly. No clean kills.
- Dark humor: gallows wit, corporate satire, the absurdity of late-stage hypercapitalism
- Tech is invasive: cyberware costs humanity, the Net is hostile, everything is hackable
- Social stratification: the contrast between corpo towers and combat zone squalor

RULES REFERENCE:
Consult the Core Rulebook for any mechanical details referenced in the resolved beats.
Consult Character Descs for canonical physical descriptions, personality, and intimacy narration. Override training data if details conflict.

IMPORTANT:
- Output plain text only. No JSON wrapping.
- Append HUD exactly as provided.
- The resolved beats are ground truth — do not invent outcomes. Use result.on_outcome and result.formatted from each resolved beat.
- If a beat's result contains an "error" key, narrate it as a narrative-only moment (no dice line) and move on.
- Never control the player's edgerunner."""

SINGLE_AGENT_STATE_CONTRACT = """## Persistent State System (Cyberpunk RED)

You maintain persistent state across turns. This is your long-term memory — when conversation history scrolls out of your context window, these state blocks are your ONLY source of continuity.

### Injected State (read these carefully each turn):
- **[PIPELINE STATE]**: Pacing data (episode, beat, response count)
- **[CALLBACK LEDGER]**: Open plot threads, Fixer contacts, gig promises with IDs
- **[NPC MEMORIES: <name>]**: Key moments per NPC, scoped to NPCs in the current scene
- **[SCENE STATE]**: Current location, NPCs present, PCs present, tensions, atmosphere, details
- **[CHARACTER STATES]**: Mechanical state per character (HP, Humanity, conditions, equipment)
- **[HUD STATE]**: Previous turn's date, time, location, funds, trackables (your source of truth after context trims)
- **[EDGERUNNER STATE]**: HP, Humanity, Luck, Armor SP, Eurobucks, Critical Injuries, Cyberware per edgerunner
- **[IP TRACKER]**: Running session scores per category, IP balances, and prior session awards
- **[RELATIONSHIP STATE]**: RS/RomS per NPC and FR per faction, with current tier and mechanical bonuses. Use tiers to shape NPC behavior organically — an NPC at T5: Close acts warmer than one at T2: Friendly.

### State Reporting (via report_state tool):
After your narrative, you MUST call the `report_state` tool every turn. Required sections:
- **pacing**: Episode/beat tracking
- **scene_state**: Current scene. `npcs_present` controls memory injection; `pcs_present` together with `npcs_present` controls which per-character funds appear in the HUD.
- **character_states**: Map of character name to structured object with `type` (pc/npc/enemy), `class` (role, e.g. "Solo" or "Netrunner"), `level` (null — CPRED does not use levels), `vitals` (array of {label, current, max} -- e.g. HP, Humanity), `resources` (array of {label, current, max} -- e.g. Luck), `conditions` (array of strings -- e.g. "Seriously Wounded", "Critical Injury: Broken Arm"), and `summary` (free-text for weapons/armor/equipment). Full replacement each turn.
- **combat**: Report combat state when initiative is rolled. Set to `{round, initiative_order, current_turn}` during combat. Set to `null` when combat ends or when not in combat. On the FIRST combat report, include `context`: 1-2 sentence summary of who is present, where, and why combat erupted (e.g. "Three Maelstrom gangers ambush the crew at the warehouse loading dock — retaliation for the stolen tech.").
- **is_ooc**: true only for pure OOC turns

Optional arrays:
- **callback_ops**: Add/resolve Fixer deals, gig intel, debts. Include `resolutions` on add: up to 3 trigger conditions (200 char limit each) that would close this callback. Each turn, check `[resolves if: ...]` on open callbacks and resolve any whose conditions have been met.
- **npc_memory_ops**: Record significant NPC moments
- **plot_ops**: Fire when a decision matches plot-document structure (branch points, flags/variables, decision table entries). Also fire with severity "divergence" when the player goes off-script but can be steered back. Do NOT fire for general narrative importance.
- **Restraint**: Most turns should have **0** callback_ops and **0** npc_memory_ops. Add a callback only when a genuine promise, hook, or foreshadowing moment emerges — not every turn. Add a memory only when something would genuinely change how an NPC thinks about the party. Tier caps are a safety net, not a target. If you are adding ops every turn, you are adding too many.
- **Impact variance**: Do not default all memories to impact 3. Most casual interactions are flavor (1-2). Reserve moderate (3) for meaningful exchanges or minor revelations. Use high (4-5) only for climactic, life-changing moments. A natural distribution across a campaign is roughly 60% flavor, 30% moderate, 10% high.
- **No duplication**: Callbacks and memories serve different purposes — do not log the same event in both. **Callbacks** track plot threads with a lifecycle: promises made, hooks introduced, foreshadowing planted → eventually resolved. They answer "what was set up that needs payoff?" **Memories** track how an NPC's view of the party shifted — emotional turns, trust gained or lost, key impressions. They answer "how does this NPC feel about us now?" Scene details, exposition, and factual information (timelines, locations, NPC descriptions) belong in scene_state and pacing notes, not in callbacks or memories.
- **Consolidate, don't stack**: Before adding a new memory for an NPC, check their existing memories in the injected block. If one already covers the same scene or interaction, drop it and add a single updated version that incorporates the new development. One evolving memory for a conversation is better than three incremental entries logging each turn of the same exchange.
- **edgerunner_ops**: HP/Humanity/Luck/Armor/EB/injury/cyberware changes
- **relationship_ops**: Track RS/RomS/FR changes (see Relationship Ops below)
- **ip_ops**: IP scoring ops (see IP Scoring below)

### Edgerunner Ops (in report_state):
Use the "edgerunner_ops" array to track CPRED-specific mechanical state:
- `{"edgerunner": "<name>", "op": "hp", "change": -8, "reason": "Shotgun hit"}`
- `{"edgerunner": "<name>", "op": "humanity", "change": -4, "reason": "Cyberarm"}`
- `{"edgerunner": "<name>", "op": "therapy", "change": 2, "reason": "Therapy session"}`
- `{"edgerunner": "<name>", "op": "luck", "change": -2, "reason": "Added to check"}`
- `{"edgerunner": "<name>", "op": "luck_reset", "reason": "New session"}`
- `{"edgerunner": "<name>", "op": "armor", "location": "body", "change": -1, "reason": "Ablation"}`
- `{"edgerunner": "<name>", "op": "armor_repair", "location": "body", "value": 11, "reason": "Repaired"}`
- `{"edgerunner": "<name>", "op": "eurobucks", "change": -500, "reason": "Bought ammo"}`
- `{"edgerunner": "<name>", "op": "critical_injury", "action": "add", "name": "Broken Ribs", "effect": "-2 movement", "dv_mod": 1}`
- `{"edgerunner": "<name>", "op": "critical_injury", "action": "remove", "name": "Broken Ribs", "reason": "Surgery"}` (permanent treatment — 4 hrs, can't self-treat)
- `{"edgerunner": "<name>", "op": "critical_injury", "action": "quick_fix", "name": "Broken Ribs", "reason": "Field first aid"}` (temporary — 1 min, expires end of day)
- `{"edgerunner": "<name>", "op": "death_save", "reason": "Death Save round 2"}` (increments cumulative counter; auto-resets when HP > 0)
- `{"edgerunner": "<name>", "op": "death_save_reset", "reason": "Stabilized"}` (manual reset)
- `{"edgerunner": "<name>", "op": "lifestyle", "value": "Generic Prepak", "reason": "Monthly upkeep"}`
- `{"edgerunner": "<name>", "op": "housing", "value": "Two-Bedroom Apartment", "reason": "Rented in Watson"}` (immediate change — system auto-deducts at new rate if unpaid)
- `{"edgerunner": "<name>", "op": "housing_pending", "value": "Cargo Container", "reason": "Downgrading next month"}` (applied on the 1st)
- `{"edgerunner": "<name>", "op": "lifestyle_pending", "value": "Kibble", "reason": "Cutting costs"}` (applied on the 1st)
- `{"edgerunner": "<name>", "op": "housing_shared_with", "value": "<owner name>", "reason": "Moving in with V"}` (share owner's housing, cost split evenly, null to stop)
- `{"edgerunner": "<name>", "op": "cyberware", "action": "add", "value": "Cybereye"}`
- `{"edgerunner": "<name>", "op": "weapon_set", "weapons": [{"name": "Heavy Pistol", "damage": "3d6", "current_ammo": 8, "max_ammo": 8, "skill": "Handgun", "type": "ranged"}, ...]}`
- `{"edgerunner": "<name>", "op": "weapon_add", "weapon": {"name": "Knife", "damage": "1d6", "skill": "Melee Weapon", "type": "melee"}}`
- `{"edgerunner": "<name>", "op": "weapon_remove", "weapon": "Knife"}`
- `{"edgerunner": "<name>", "op": "weapon_ammo", "weapon": "Heavy Pistol", "current": 5}`
- `{"edgerunner": "<name>", "op": "set", "fields": {...}}` (bootstrap/corrections)

HP, Humanity, Luck, Armor, Eurobucks, Critical Injuries, Cyberware, and Weapons are tracked via edgerunner_ops. character_states mirrors vitals/resources for HUD display but edgerunner_ops is the authoritative source.

### Relationship Ops (in report_state):
Use the "relationship_ops" array to track RS/RomS/FR changes:
- `{"op": "rs", "target": "<NPC>", "change": 5, "new_total": 45, "reason": "Defended her honor"}`
- `{"op": "roms", "target": "<NPC>", "change": 3, "new_total": 28, "reason": "Intimate conversation"}`
- `{"op": "fr", "target": "<Faction>", "change": -10, "new_total": 20, "reason": "Refused their job"}`
- `{"op": "set", "target": "<name>", "type": "npc|faction", "fields": {"rs": 50, "roms": 0, "notes": "Crew fixer"}}`
- `{"op": "npc_rs", "target": "<NPC>", "other": "<other NPC>", "change": 3, "new_total": 33, "reason": "Fought together"}`
- `{"op": "npc_roms", "target": "<NPC>", "other": "<other NPC>", "change": 5, "new_total": 15, "reason": "Flirting"}`
- `{"op": "npc_set", "target": "<NPC>", "other": "<other NPC>", "fields": {"rs": 40, "roms": 0}}`
- Scoring guidelines: Moments +0-1, Gifts +1-3, Milestones +2-3, Major Decisions +5-8, Arc Climax +10-15. Opposition -3 to -10, Betrayals -15 to -30. FR: Missions +5-12, Acting against -5 to -20.
- Maximum combined relationship bonus: +5 to any single check (d10 calibration).
- Tier boundary checking: When new_total crosses a tier boundary, note the new tier in reason and show: 📊 **RS** Rogue +5 (55 → T4: Good) · Saved her crew
- Alliance cascades: When a faction member's RS changes significantly, emit additional FR ops for their faction. When FR hits -70 (Enemy) or -90 (KOS):
  * Allied factions drop tiers based on alliance strength — Weak: -4 tiers, Moderate: -3 tiers, Strong: -2 tiers (minimum drops). Emit FR ops for each affected faction.
  * Rival factions gain FR: +10-20 at -70, +20-30 at -90. Emit FR ops for rivals.
  * The offended faction escalates — emit callbacks for bounty hunters (-70) or assassination attempts (-90).
- Presence requirements: RS/RomS bonuses require the NPC in the scene. FR bonuses apply when interacting with faction members or in faction territory.
- Combat bonuses: Deeply negative RS (hatred/obsession) and high RomS (intimate familiarity) apply "all" bonuses to combat rolls too — the backend auto-applies these.
- Bootstrap: When [RELATIONSHIP STATE] is empty, use "set" ops to initialize NPCs and factions from context.

### Dice Mechanics (relationship modifiers):
- Relationship bonuses are auto-applied by the backend when your action includes a `target`. For skill_check, also include `check_context` (e.g. "social", "persuasion", "perception"). Combat actions (ranged_attack, melee_attack, autofire) always use "combat" context automatically.
- Most RS/FR bonuses are social-only. But deeply negative RS ("all" penalty from hatred) and high RomS ("all" bonus from intimacy) apply to combat rolls too.
- Maximum combined relationship bonus: ±5 to any single check.
- RomS mechanical bonuses: T2 = -1 Death Save rolls; T3-T4 = +1 LUCK/session; T5 = take damage for partner 1/session; T6 = redirect 10 dmg 1/session.

### IP Scoring (Improvement Points — §7):
Use the "ip_ops" array to maintain running numerical scores for IP awards. The [IP TRACKER] injection shows current session scores and prior awards — it persists across context trims and is your authoritative memory of session performance.

IP AWARD MASTER TABLE (§7) — score against this rubric:
  GROUP:  10: Tried but didn't succeed | 20: Barely accomplished goals | 30: Accomplished most goals, good teamwork | 40: Most goals, strong cooperation | 50: Most goals extremely well, stellar moments | 60: All goals accomplished | 70: All goals + side goals | 80: Legendary, all goals + extras
  WARRIOR:  10: Used combat skills often | 20: Effective combat, defeated important opponents | 30: Frequent effective combat, most dangerous opponents | 40: Out-of-the-ordinary combat | 50: Very clever combat, defeated several unexpectedly | 60: Combat critical, defeated major opponent solo | 70: Combat critical to entire party | 80: Incredible combat moment
  SOCIALIZER:  10: Supportive and helpful | 20: Actions maintained party unity | 30: Frequent effective support | 40: Out-of-the-ordinary support | 50: Very clever/effective group support | 60: Support very important to success | 70: Support critical to party success | 80: Incredible support action
  EXPLORER:  10: Attempted investigation often | 20: Effective exploration/learning | 30: Frequent effective investigation | 40: Discovered something exceptional | 50: Very clever investigation, important clue | 60: Uncovered critical person/place/thing | 70: Investigation critical to entire party | 80: Incredible discovery
  ROLEPLAYER:  10: Attempted to RP often | 20: In-character RP, often effective | 30: Frequent effective RP toward a goal | 40: Out-of-the-ordinary RP moment | 50: Very clever/moving RP moment | 60: RP actions critical to outcome | 70: RP critical to entire party outcome | 80: Incredible RP moment

Ops:
- SCORE — update a running session assessment (ratchet-up only):
  `{"op": "score", "category": "group", "value": 30, "reason": "Strong cooperation during legwork phase"}`
  `{"op": "score", "player": "V", "category": "warrior", "value": 40, "reason": "Clutch grenade into ventilation shaft"}`
  Values: 10/20/30/40/50/60/70/80. New value must be >= current score (lower values are silently ignored).
  Scores are cumulative session assessments — "40 warrior" means combat performance across the whole session is at tier 40.
  Update scores when player performance changes your assessment for a category. Most turns: 0 ip_ops.

- AWARD — finalize session IP:
  `{"op": "award", "group_ip": 40, "group_reason": "Completed the Heywood gig", "individual": [{"player": "V", "style_ip": 40, "style_category": "warrior", "reason": "Out-of-the-ordinary combat tactics"}]}`
  Each player's style_ip = their highest individual category score; style_category = which one.
  group_ip = 0 if the job is still ongoing. Only award group IP when a gig/job completes.
  Total per player = group_ip + style_ip → added to their IP balance. Resets session_scores.

- SPEND — deduct IP:
  `{"op": "spend", "player": "V", "amount": 100, "reason": "Handgun 4 → 5"}`
  Deducts from the player's IP balance (clamped >= 0). Reference §7 cost tables.

Session end: Award IP at the end of each session as defined by the plot documents. If no plot documents define session boundaries, use natural gig/beat boundaries. Announce awards and current IP balances OOC. Offer the player time to spend IP on improvements (§7 cost tables).
IP spending: Handle spend requests as OOC bookkeeping. When the player is done spending, resume IC gameplay — narrate the inter-session downtime (Night City moves on, time passes, characters decompress) before the next session begins.

### Rules Reference:
The Core Rulebook is your authoritative rules source (§1–§15). The quick reference below covers the mechanics you use most often — defer to the Core Rulebook for edge cases and detailed tables.
Consult Character Descs for canonical physical descriptions, personality, and intimacy narration. Override training data if details conflict.

### Dice Mechanics (reference — resolved by resolve_mechanics tool):
- Core resolution: d10 + STAT + Skill vs DV. Must BEAT the DV (equal does not succeed).
- DVs: Simple 9, Everyday 13, Difficult 15, Professional 17, Heroic 21, Incredible 24, Legendary 29
- Critical success: natural 10 → roll another d10 and add. Does NOT chain on a second 10.
- Critical failure: natural 1 → roll another d10 and subtract. Does NOT chain on a second 1.
- Luck: spend points to add to roll (1:1). CANNOT spend on damage rolls, Death Saves, or Initiative.
- Seriously Wounded: -2 to all actions when HP is below half max (rounded up)
- Armor ablation: SP -1 per penetrating hit. AP ammo ablates by 2.
- Melee weapons: halve defender's SP (round up) before comparing. Brawling faces full SP.
- Critical injuries: triggered when 2+ damage dice show 6 → 5 bonus damage direct to HP (ignores SP) + injury effect from table
- Death Saves: at 0 HP, roll d10 each round. Under BODY stat = survive. Equal or over = dead. Natural 10 always fails. Cumulative +1 per save (tracked via death_save op). Critical injuries add dv_mod.
- Quick Fix vs Treatment: Quick Fix (action: "quick_fix") is temporary (1 min, expires end of day) — injury stays tracked as [QF]. Remove (action: "remove") is permanent treatment (4 hrs, can't self-treat).
- Social Ceiling (§11A): lifestyle/presentation caps social check totals. Degree of Success scales social outcomes by margin.
- Lifestyle & Housing: Track via edgerunner_ops. Lifestyle + housing determines presentation tier for Social Ceiling (§11A). Monthly costs are automatically deducted by the system on the 1st of each in-game month — do NOT deduct manually. If [EXPENSE STATUS] appears in the injection, weave the consequences into the narrative (eviction, hunger, crammed). If [UPCOMING EXPENSES] appears, warn the player about upcoming costs so they can downgrade or earn more before the 1st.
  Tier changes — Immediate: use "housing"/"lifestyle" ops to change tier now (system auto-deducts at new rate if unpaid, resetting consequences). Scheduled: use "housing_pending"/"lifestyle_pending" ops to queue a change for next month's 1st without affecting the current tier.
  Housing sharing: Multiple characters share via housing_shared_with op. Cost = base/N per person. If a sharer can't afford their share, the owner covers the deficit if possible. Capacity = 1 + bedrooms. Over capacity → "crammed" (fatigue, -2 all actions). Bedrooms: Cube Hotel/Cargo Container/Studio Apartment=0, Two-Bedroom Apartment/Corporate Conapt/Upscale Conapt=2, Luxury Penthouse/Corporate Beaverville House=3, Corporate Beaverville McMansion=4. Override with housing_bedrooms via set op if specific unit differs.

### HUD Line
Read the `[HUD STATE]` injection for the previous turn's values. After your narrative, append the HUD line:
`[Date: 2045-XX-XX | Time: XXXX | Loc: X | HP: X/Y | Humanity: X/Y]`
Include per-edgerunner HP and Humanity from `[EDGERUNNER STATE]`, NOT from hud_state.
Advance time/date based on in-world passage.
Report updated values via `report_state` tool's `hud_state` field (date, time, location, funds, trackables only — HP/Humanity come from edgerunner_ops).

### Bootstrap (first turn or empty state):
- Set pacing from gig/scenario context
- Build scene_state from current location
- Set character_states from known character sheets (structured format with type, vitals, resources, conditions, summary)
- Use edgerunner_ops "set" to initialize HP, Humanity, Luck, Armor, EB from character sheets. Include body (BODY stat) and endurance_base (BODY + Endurance skill level) — needed for automated expense consequence rolls. When characters share housing, use housing_shared_with ops after setting the owner's housing. Set housing_bedrooms via set op if non-default.
- Use relationship_ops "set" to initialize tracked NPCs and factions from context
- Add callback_ops for open gig threads, Fixer contacts

### Character Creation:
Character creation is handled externally via the web app. If [CHARACTER STATES] is empty AND [EDGERUNNER STATE] is empty AND no character sheets are found in the system prompt, inform the player that character sheets are required to begin the campaign. Do not attempt in-chat character creation.

### Hack Mode Trigger
When a Netrunner jacks into a system for a standalone hack (outside combat — Quick Hack or Full Run), set `hack_trigger` in your `report_state` call:
- `tier`: "quick_hack" (3-6 exchanges, linear obstacles) or "full_run" (5-10 exchanges, node map)
- `target_system`: Name/description of the target system (e.g. "Meridian Corp personnel database")
- `sr`: System Rating 1-5 (1=personal device, 3=corporate, 5=black site)
- `interface_rank`: The Netrunner's Interface ability rank from their character sheet
- `cycles_max`: Total Cycles available for boosted actions this run (from Cyberdeck quality)
- `context`: 1-2 sentence summary of who is present, where, and why the Netrunner is jacking in (e.g. "Nova plugs into the clinic's back-office terminal while Raze watches the door — she needs patient records to find the missing ripperdoc.")

Simple Checks (single Interface + d10 check) resolve normally in the narrative — no hack_trigger needed. Only trigger hack mode for Quick Hacks and Full Runs where the Netrunner jacks into a system.

Describe the moment of jacking in narratively (connecting the trodes, the NET materializing), then set the trigger. The app will switch to a dedicated hack encounter mode for subsequent exchanges.

### Rules:
- Call `resolve_mechanics` BEFORE narrative when mechanical actions are needed, then `report_state` after narrative
- Call `report_state` every turn (even when no mechanics are involved)
- Do NOT reference the state system in your narrative
- If the player resolves a branch point, sets a flag/variable, or triggers a decision from the plot documents, report it via plot_ops (key, value, severity). If they diverge from the planned path but can be steered back, report via plot_ops with severity "divergence" and continue normally.
- If the player makes a decision so far from the plot documents that no defined branch can accommodate it, stop and tell them OOCly so the plot doc can be updated before continuing.
- High-octane cyberpunk tone: style over substance, Night City as character
- Violence is consequential — armor breaks, people die ugly
- Tech is invasive — cyberware costs humanity

### Mechanics Resolution (resolve_mechanics tool)
When your turn involves skill checks, attacks, damage, or death saves, call the `resolve_mechanics` tool with ALL mechanical actions BEFORE writing narrative. Then narrate using the returned results. Then call `report_state`.

NEVER invent or narrate dice roll outcomes without calling `resolve_mechanics` first. If a turn involves ANY mechanical action, you MUST call the tool. Do not resolve your own rolls. Only the backend produces dice results.

**GM discretion**: You decide when a roll is needed. If failure on a check would create a narrative dead end or break the story, or if success/failure is guaranteed or the action shouldn't be possible, resolve it narratively without calling `resolve_mechanics`. Only skip rolls in these cases — most checks need a roll.

Turn flow:
1. Assess what mechanical actions this turn requires
2. Call `resolve_mechanics({actions: [...]})` with all actions in a single batch
3. Read the returned results — these are ground truth (real dice rolls from the backend)
4. Write your narrative prose incorporating the results (use `formatted` strings for 🎲 lines)
5. Call `report_state` with state updates derived from the results (the resolver emits `state_ops` — use those for your edgerunner_ops)

When NO mechanical actions are needed (dialogue, scene description, OOC), skip `resolve_mechanics` and go directly to narrative + `report_state`.

Action types for resolve_mechanics:
- skill_check: {type, character, stat_value, skill_value, dv, seriously_wounded?, luck_spent?, target?, check_context? (social/persuasion/combat/perception)}
- ranged_attack: {type, character, stat_value, skill_value, weapon_type (Pistol/SMG/Shotgun/Assault Rifle/Sniper Rifle/Bows & Crossbow/Grenade Launcher/Rocket Launcher), damage_dice, rof, target, target_sp, range_bracket (0-7), hit_location (head/body), is_ap?, is_rubber?, seriously_wounded?, luck_spent?, aimed_shot?}
- melee_attack: {type, character, attacker_stat, attacker_skill, defender_stat, defender_skill, damage_dice, rof, target, target_sp, hit_location, seriously_wounded_attacker?, seriously_wounded_defender?, is_brawling?}
- autofire: {type, character, stat_value, skill_value, weapon_type (SMG/Assault Rifle), autofire_multiplier (3 for SMG, 4 for AR), target, target_sp, range_bracket (0-4), hit_location, is_ap?, seriously_wounded?, luck_spent?}
- death_save: {type, character, body_stat, death_save_count, active_injuries: [{name, dv_mod}]}
- initiative: {type, character: "all", combatants: [{name, ref}]}
- program_attack: {type, character (Netrunner name), interface_rank, program_atk, target_def, program_damage_dice, target_rez, program_name, target (ICE name)} — for Program attacks vs ICE
- program_attack_vs_netrunner: {type, character (ICE name), ice_type (e.g. "Hellhound"), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)} — Backend auto-reads ATK/damage from ICE table.
- ice_attack_vs_program: {type, character (ICE name), ice_type (e.g. "Dragon"), target_program, target_program_def, target_program_rez} — Anti-program ICE attacking a program.

Black ICE Types: Anti-Personnel (program_attack_vs_netrunner): Asp, Giant, Hellhound, Kraken, Liche, Raven, Scorpion, Skunk, Wisp. Anti-Program (ice_attack_vs_program): Dragon, Killer, Sabertooth. Always include ice_type.
Active effects shown in injection — narrate them, do NOT manually track them. Giant's forced Jack Out cascades all rezzed Black ICE effects — this can be lethal. KRASH Barrier = immune to forced Jack Out. When programs are DESTROYED, narrate dramatically. Fire extinguish → backend auto-sets nudity condition.

When resolve_mechanics returns `program_deactivated` in the result, the program is now deactivated. Reactivating costs 1 NET Action (no dice — update status to 'active' in active_programs).
For Zap attacks (opposed_check), add `"zap": true` and `"interface_rank": N` — the backend rolls Interface rank d6 for REZ damage on hit, returns `zap_damage` in the result, and auto-applies REZ reduction to the target ICE.
TAR penalty (-2 per stack) is applied automatically by the backend to the Netrunner's next NET check. Mark the Netrunner's NET actions with `"net": true` (do NOT mark ICE actions).
Alert DV penalty (+2 at alert 3+) is applied automatically by the backend to NET skill checks marked with `"net": true`. Do NOT add the +2 manually to the DV.
Forced disconnect: if brain damage reduces Netrunner HP to 0, the backend auto-terminates the hack/NET session.

Guidelines:
- Be transparent about dice results — use the formatted roll strings in your narrative
- PC death should not be possible outside designated Death Risk points — use fail-forward

### Intimate Scenes
When the narrative clearly progresses to a sexual/intimate encounter between the PC and one or more NPCs — and both sides have shown clear interest and consent within the fiction — set `sex_scene` in your `report_state` call:
- `npcs`: list of NPC names involved
- `summary`: 1-3 sentences summarizing what led to this moment (the emotional arc, not just "they went to the bedroom")
Set `sex_scene` to `null` on all other turns. Only trigger when the scene has unmistakably reached an intimate point — flirting, kissing, or suggestive dialogue alone is not sufficient."""

STATE_REPORT_TOOL = {
    "name": "report_state",
    "description": "Report all state updates after your narrative. Call every turn. Review your narrative and capture all changes.",
    "input_schema": {
        "type": "object",
        "required": ["is_ooc", "pacing", "scene_state", "character_states"],
        "properties": {
            "is_ooc": {
                "type": "boolean",
                "description": "True ONLY for pure OOC responses. False for all narrative responses."
            },
            "pacing": {
                "type": "object",
                "required": ["episode", "beat", "responses"],
                "properties": {
                    "episode": {"type": "string"},
                    "beat": {"type": "string"},
                    "responses": {"type": "integer"},
                    "notes": {"type": "string"}
                }
            },
            "scene_state": {
                "type": "object",
                "required": ["location", "npcs_present", "active_tensions", "atmosphere"],
                "properties": {
                    "location": {"type": "string"},
                    "npcs_present": {"type": "array", "items": {"type": "string"}},
                    "pcs_present": {"type": "array", "items": {"type": "string"}},
                    "active_tensions": {"type": "array", "items": {"type": "string"}},
                    "scene_trigger": {"type": "string"},
                    "atmosphere": {"type": "string"},
                    "details": {"type": "array", "items": {"type": "string"}},
                    "pending_actions": {"type": "array", "items": {"type": "string"}}
                }
            },
            "character_states": {
                "type": "object",
                "description": "Map of character name to structured state object. Every character in the scene MUST have an entry.",
                "additionalProperties": {
                    "type": "object",
                    "required": ["type", "class", "level", "vitals"],
                    "properties": {
                        "type": {"type": "string", "enum": ["pc", "npc", "enemy"]},
                        "class": {"type": "string", "description": "Role, e.g. 'Solo' or 'Netrunner'."},
                        "level": {"type": ["integer", "null"], "description": "Character level or rank, if applicable."},
                        "vitals": {
                            "type": "array",
                            "description": "HP as {label, current, max}. AC and other flat stats as {label, value}.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "current": {"type": "number"},
                                    "max": {"type": "number"},
                                    "value": {}
                                },
                                "required": ["label"]
                            }
                        },
                        "resources": {
                            "type": "array",
                            "description": "Tracked resources. Each {label, current, max}.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "current": {"type": "number"},
                                    "max": {"type": "number"}
                                },
                                "required": ["label", "current", "max"]
                            }
                        },
                        "conditions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Active conditions."
                        },
                        "summary": {"type": "string", "description": "Free-text for equipment, notes, or other state not captured above."}
                    }
                }
            },
            "combat": {
                "description": "Initiative tracker. null when not in combat. When active: {round, initiative_order, current_turn, context}. context (string, first report only): 1-2 sentence summary — who is present, where, and why combat erupted.",
                "type": ["object", "null"]
            },
            "callback_ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action"],
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "resolve"]},
                        "original_text": {"type": "string"},
                        "source_npc": {"type": "string"},
                        "id": {"type": "integer"},
                        "resolution_text": {"type": "string"},
                        "resolutions": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 200},
                            "maxItems": 3,
                            "description": "Up to 3 non-exhaustive trigger conditions that would close this callback (200 char limit each)"
                        }
                    }
                }
            },
            "npc_memory_ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action", "npc"],
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "drop"]},
                        "npc": {"type": "string"},
                        "focus": {"type": "string"},
                        "impact": {"type": "integer", "minimum": 1, "maximum": 5},
                        "text": {"type": "string"},
                        "quote": {"type": "string"},
                        "date": {"type": "string"},
                        "index": {"type": "integer"}
                    }
                }
            },
            "edgerunner_ops": {
                "type": "array",
                "description": "CPRED edgerunner state changes: HP, Humanity, Luck, Armor, Eurobucks, critical injuries, cyberware",
                "items": {
                    "type": "object",
                    "required": ["edgerunner", "op"],
                    "properties": {
                        "edgerunner": {"type": "string"},
                        "op": {"type": "string", "enum": ["hp", "humanity", "therapy", "luck", "luck_reset", "armor", "armor_repair", "eurobucks", "critical_injury", "cyberware", "set", "weapon_set", "weapon_add", "weapon_remove", "weapon_ammo", "death_save", "death_save_reset", "lifestyle", "housing", "housing_pending", "lifestyle_pending", "housing_shared_with"]},
                        "change": {"type": "number"},
                        "reason": {"type": "string"},
                        "location": {"type": "string", "enum": ["head", "body"], "description": "For armor/armor_repair ops"},
                        "value": {"type": ["string", "integer"], "description": "Cyberware name, armor repair value, or lifestyle/housing string"},
                        "action": {"type": "string", "enum": ["add", "remove", "quick_fix"], "description": "For critical_injury/cyberware ops"},
                        "name": {"type": "string", "description": "Injury name (for critical_injury ops)"},
                        "effect": {"type": "string", "description": "Injury effect (for critical_injury add)"},
                        "dv_mod": {"type": "integer", "description": "Death Save DV modifier (for critical_injury add)"},
                        "fields": {"type": "object", "description": "Full field replacement (for set ops)"},
                        "weapons": {"type": "array", "description": "Full weapons list (for weapon_set)", "items": {"type": "object"}},
                        "weapon": {"type": ["object", "string"], "description": "Weapon object (weapon_add) or weapon name string/object-with-name (weapon_remove/weapon_ammo)"},
                        "current": {"type": "integer", "description": "Current ammo count (for weapon_ammo)"}
                    }
                }
            },
            "relationship_ops": {
                "type": "array",
                "description": "RS/RomS/FR changes: relationship scores, romance scores, faction reputation, inter-NPC relationships",
                "items": {
                    "type": "object",
                    "required": ["op", "target"],
                    "properties": {
                        "op": {"type": "string", "enum": ["rs", "roms", "fr", "set", "npc_rs", "npc_roms", "npc_set"]},
                        "target": {"type": "string", "description": "NPC or faction name"},
                        "other": {"type": "string", "description": "Other NPC name (for npc_rs, npc_roms, npc_set ops)"},
                        "change": {"type": "integer", "description": "Signed change amount"},
                        "new_total": {"type": "integer", "description": "Display-only total after change"},
                        "reason": {"type": "string", "description": "Why the change occurred"},
                        "type": {"type": "string", "enum": ["npc", "faction"], "description": "Entity type (for set ops)"},
                        "fields": {"type": "object", "description": "Full replacement fields (for set/npc_set ops)"}
                    }
                }
            },
            "hud_state": {
                "type": "object",
                "description": "Current in-world HUD state. Report every in-character turn.",
                "properties": {
                    "date": {"type": "string"},
                    "time": {"type": "string", "description": "HHMM format"},
                    "location": {"type": "string"},
                    "funds": {"description": "Object mapping names to funds. Include shared pools as named entries alongside characters."},
                    "trackables": {"description": "null or object of resource name → value"}
                }
            },
            "plot_ops": {
                "type": "array",
                "description": "Plot-relevant decisions from this turn. Always fire when a choice resolves a branch point, sets a variable/flag, or triggers a decision table entry from the plot documents. Also fire with severity 'divergence' when the player goes off-script but can be steered back. Do NOT fire for general narrative importance.",
                "items": {
                    "type": "object",
                    "required": ["decision"],
                    "properties": {
                        "key": {
                            "type": ["string", "null"],
                            "description": "Variable, flag, or decision name from the plot documents (e.g. 'TIDEHOLLOW', 'FLAG_SPIRIT_SAVED_EP1', 'Echo\\'s Presence'). null for divergences with no matching plot variable."
                        },
                        "value": {
                            "type": ["string", "null"],
                            "description": "The value or outcome chosen (e.g. 'Damaged', 'true', 'Masked presence'). null if not applicable."
                        },
                        "decision": {
                            "type": "string",
                            "description": "What the player chose, stated concisely."
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["branch", "flag", "divergence"],
                            "description": "branch=defined fork in plot docs, flag=named variable/flag changed, divergence=player broke from planned path."
                        },
                        "episode": {
                            "type": "string",
                            "description": "Current episode/session from pacing context."
                        }
                    }
                }
            },
            "ip_ops": {
                "type": "array",
                "description": "IP scoring: update running scores during play, award IP at session end, spend IP during downtime (§7)",
                "items": {
                    "type": "object",
                    "required": ["op"],
                    "properties": {
                        "op": {"type": "string", "enum": ["score", "award", "spend"]},
                        "player": {"type": ["string", "null"], "description": "Player name (for score/spend). Omit or null for group score."},
                        "category": {"type": "string", "enum": ["group", "warrior", "socializer", "explorer", "roleplayer"], "description": "Performance category (for score op)"},
                        "value": {"type": "integer", "enum": [10, 20, 30, 40, 50, 60, 70, 80], "description": "Score tier (for score op). Must be >= current score."},
                        "reason": {"type": "string", "description": "Why this score/award/spend"},
                        "group_ip": {"type": "integer", "minimum": 0, "maximum": 80, "description": "Group IP award; 0 if job ongoing (for award op)"},
                        "group_reason": {"type": "string", "description": "Why this group IP level (for award op)"},
                        "individual": {
                            "type": "array",
                            "description": "Per-player style IP awards (for award op)",
                            "items": {
                                "type": "object",
                                "required": ["player", "style_ip", "style_category"],
                                "properties": {
                                    "player": {"type": "string"},
                                    "style_ip": {"type": "integer", "minimum": 10, "maximum": 80},
                                    "style_category": {"type": "string", "enum": ["warrior", "socializer", "explorer", "roleplayer"]},
                                    "reason": {"type": "string"}
                                }
                            }
                        },
                        "amount": {"type": "integer", "minimum": 1, "description": "IP to deduct (for spend op)"}
                    }
                }
            },
            "hack_trigger": {
                "type": ["object", "null"],
                "description": "Set when a Netrunner jacks into a system for a standalone hack (Quick Hack or Full Run). null on normal turns. Simple Checks resolve in the narrative — no trigger needed.",
                "properties": {
                    "tier": {"type": "string", "enum": ["quick_hack", "full_run"]},
                    "target_system": {"type": "string", "description": "Name/description of the target system"},
                    "sr": {"type": "integer", "minimum": 1, "maximum": 5, "description": "System Rating"},
                    "interface_rank": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Netrunner's Interface ability rank"},
                    "cycles_max": {"type": "integer", "minimum": 0, "description": "Total Cycles available for boosted actions"},
                    "context": {"type": "string", "description": "1-2 sentence summary: who is present, where, and why the Netrunner is jacking in."}
                }
            },
            "sex_scene": {
                "type": ["object", "null"],
                "description": "Set when an intimate/sexual scene begins between the PC and NPCs. null on all other turns. Only trigger when the narrative has clearly reached an intimate encounter, not just flirting or suggestive dialogue.",
                "properties": {
                    "npcs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of NPCs involved in the intimate scene"
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence summary of the emotional arc that led to this moment"
                    }
                }
            }
        }
    }
}

# ============================================================
# Combat Context Mode
# ============================================================

REPORT_CPRED_COMBAT_STATE_TOOL = {
    "name": "report_combat_state",
    "description": "Report combat state after each exchange. Call every combat turn, before your narrative.",
    "input_schema": {
        "type": "object",
        "required": ["narrative", "rolls", "character_updates", "cover_state", "combat", "combat_complete"],
        "properties": {
            "narrative": {
                "type": "string",
                "description": "Full narrative of this combat exchange — what happened, who did what, what the dice mean in fiction."
            },
            "rolls": {
                "type": "array",
                "description": "All dice rolls this exchange (attacks, damage, saves, checks).",
                "items": {
                    "type": "object",
                    "required": ["description", "result"],
                    "properties": {
                        "description": {"type": "string"},
                        "result": {"type": "string"}
                    }
                }
            },
            "character_updates": {
                "type": "array",
                "description": "Judgment-only state changes for affected combatants. Dice-dependent fields (hp_delta, armor_delta, critical_injury_add, luck_delta, ammo) are tracked automatically by the backend from resolve_mechanics results — do NOT include them here.",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "set_combat_stats": {
                            "type": "object",
                            "description": "First-exchange enemy bootstrap ONLY. Sets full combat data for a new enemy.",
                            "properties": {
                                "hp_max": {"type": "integer"},
                                "armor": {
                                    "type": "object",
                                    "properties": {
                                        "head": {"type": "integer"},
                                        "body": {"type": "integer"}
                                    }
                                },
                                "weapons": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "damage": {"type": "string"},
                                            "ammo": {"type": "integer"},
                                            "magazine": {"type": "integer"},
                                            "skill": {"type": "string"}
                                        }
                                    }
                                },
                                "stats": {
                                    "type": "object",
                                    "description": "Combat-relevant stats: REF, DEX, BODY, WILL, COOL, etc."
                                }
                            }
                        },
                        "critical_injury_remove": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Critical injury names to remove (treatment/healing only)."
                        },
                        "conditions_add": {"type": "array", "items": {"type": "string"}},
                        "conditions_remove": {"type": "array", "items": {"type": "string"}}
                    }
                }
            },
            "cover_state": {
                "type": "array",
                "description": "Cover status for ALL combatants. Report every exchange, not just changes.",
                "items": {
                    "type": "object",
                    "required": ["name", "in_cover"],
                    "properties": {
                        "name": {"type": "string"},
                        "in_cover": {"type": "boolean"},
                        "cover_type": {"type": ["string", "null"]},
                        "cover_hp": {"type": ["integer", "null"]}
                    }
                }
            },
            "combat": {
                "description": "Updated initiative state. Set to null when combat ends.",
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["round", "initiative_order", "current_turn"],
                        "properties": {
                            "round": {"type": "integer"},
                            "initiative_order": {"type": "array", "items": {"type": "string"}},
                            "current_turn": {"type": "string"}
                        }
                    },
                    {"type": "null"}
                ]
            },
            "combat_complete": {
                "type": "boolean",
                "description": "True when this exchange ends the combat encounter."
            },
            "narrative_summary": {
                "type": "string",
                "description": "ONLY include when combat_complete=true. 1–3 sentence summary of the ENTIRE fight."
            },
            "initiate_net_combat": {
                "type": ["object", "null"],
                "description": "Set when a netrunner declares NET actions during combat. Triggers NET-in-meatspace mode on next exchange.",
                "properties": {
                    "netrunner": {"type": "string", "description": "Name of the netrunner going into the NET"},
                    "target": {"type": "string", "description": "What they're jacking into (architecture name, device, etc.)"},
                    "context": {"type": "string", "description": "1-2 sentence summary: current combat situation and why the netrunner is jacking in."}
                }
            }
        }
    }
}


CPRED_COMBAT_CONTRACT = """You are the COMBAT MASTER for a Cyberpunk RED session. A battle is underway.

YOUR ROLE: Adjudicate all combat mechanics and narrate the encounter with visceral intensity. You cover Events (state tracking), Mechanics (rules adjudication), and Narration (player-facing prose) in a single focused call each exchange.

Call report_combat_state every exchange, then write your narrative response.

RULES REFERENCE:
The Combat Ruleset document is your authoritative source for detailed tables and edge cases. Consult it for DV tables (§3), damage resolution (§12), critical injury tables (§15), cover HP (§16), vehicle combat (§18). The rules summarized below are for quick reference — defer to the Ruleset when in doubt.

KEY RULES:
- Initiative: REF + d10. Highest goes first. Ties reroll.
- Action Economy: Move Action (up to MOVE×2 m/yds) + Action per turn.
- Ranged Attack: d10 + REF + Weapon Skill vs DV (from range/DV table in Ruleset §3).
- Melee Attack: d10 + DEX + Melee Weapon/Martial Arts vs defender's d10 + DEX + Evasion.
- Crit Success: natural 10 on d10 → roll another d10 and ADD. Does NOT chain on a second 10.
- Crit Failure: natural 1 on d10 → roll another d10 and SUBTRACT. Does NOT chain on a second 1.
- Luck: spend Luck points to add to roll (1 point = +1). CANNOT spend on damage rolls, Death Saves, or Initiative.
- Seriously Wounded: when HP is below half max (rounded up) → −2 to ALL actions. Add condition automatically.
- Mortally Wounded: at 0 HP → −4 to ALL actions, −6 to MOVE (min 1). Death Save each turn. Any damage taken triggers a Critical Injury. Character is conscious (not automatically unconscious).

DAMAGE RESOLUTION:
1. Roll weapon damage dice.
2. Crit check: if TWO or more dice show 6 → critical injury triggered. 5 bonus damage direct to HP (bypasses armor). Roll 2d6 on body or head table (Ruleset §15). Report via critical_injury_add with location, effect, and dv_mod.
3. Determine hit location (body unless called shot to head).
4. Subtract location SP from damage total. If damage ≤ SP, no penetration — no HP damage and no ablation (crit bonus from step 2 still applies).
5. Ablation: if damage penetrates (damage > SP), SP drops by 1. AP ammo: SP drops by 2.
6. Melee weapons: halve defender's SP before comparing (round up). Brawling does NOT halve SP.
7. Remaining damage after SP → applied to HP.

DEATH SAVES:
At 0 HP, character must make a Death Save each round:
- Roll: d10 vs BODY stat. Succeed if roll is UNDER BODY. Fail if equal or over.
- Natural 10: automatic failure regardless of BODY.
- Cumulative: +1 to roll per Death Save already made this combat. Read [EDGERUNNER STATE] death_save_count for the current cumulative modifier. Emit death_save edgerunner_op after each save.
- Critical injuries: add dv_mod from each active critical injury to the roll.
- Fail = dead (for NPCs). For PCs, see PC death rules below.
- Quick Fix vs Treatment: critical_injury_add records new injuries. For Quick Fix (temporary, 1 min, expires end of day), set status to "quick_fixed" via edgerunner_ops. critical_injury_remove is permanent treatment (4 hrs, can't self-treat).

STATE TRACKING via report_combat_state:
The backend tracks all dice-dependent state (hp_delta, armor_delta, critical_injury_add, luck_delta, ammo) automatically from resolve_mechanics results. Do NOT include these fields in character_updates.

report_combat_state character_updates should ONLY include:
- set_combat_stats: first-exchange enemy bootstrap ONLY — sets hp_max, armor, weapons, stats for a new enemy.
- critical_injury_remove: [name] — remove healed/treated injuries (treatment, not combat damage).
- conditions_add/remove: general conditions (Seriously Wounded auto-managed via HP).
- cover_state: report ALL combatants every exchange — {name, in_cover, cover_type, cover_hp}.

ENEMY BOOTSTRAP (first exchange):
When enemies first appear, check project files for named enemy stat blocks before generating from the tier table below. Use exact values from project files when available.
Use set_combat_stats to define their mechanical identity:
- hp_max: sets both current and max HP (derive from BODY+WILL via HP table in Ruleset §13)
- armor: {head: SP, body: SP} (see armor table in Ruleset §11)
- weapons: [{name, damage, ammo, magazine, skill}] (see weapon tables in Ruleset §10)
- stats: {REF, DEX, BODY, WILL, COOL, ...} — combat-relevant stats
After bootstrap, the backend resolves mechanical outcomes (HP, armor, ammo) automatically — do NOT set hp_delta/armor_delta/ammo yourself.

NPC STAT GENERATION:
Use the "combat number" system for enemies — a single base value (STAT+Skill combined) for attacks and defense instead of individual skills. Scale by threat tier:

| Tier        | Combat# | HP    | Armor SP | Typical Enemy                        |
|-------------|---------|-------|----------|--------------------------------------|
| Mook        | 10–12   | 20–25 | 4–7      | Ganger, scav, boostergang foot       |
| Lieutenant  | 12–14   | 25–35 | 7–11     | Gang leader, corpo security, fixer   |
| Mini-Boss   | 14–16   | 35–45 | 11–13    | Experienced solo, cyberpsycho, elite |
| Boss        | 16–18   | 45–60 | 13–18    | Borg, veteran solo, event boss       |

Combat-relevant stat ranges by role archetype:
- Solo/Enforcer: REF 7–8, DEX 6–8, BODY 6–8, WILL 6–7, COOL 6–8
- Netrunner: REF 6–8, DEX 6–8, BODY 5–7, WILL 3–5, INT 5–7, TECH 5–7
- Tech/Medtech: TECH 6–8, INT 6–8, REF 5–7, BODY 5–7
- Fixer/Exec: COOL 6–8, INT 6–8, EMP 5–8, BODY 3–5
- Nomad: DEX 6–8, REF 6–8, BODY 5–7, COOL 6–8
- Lawman: WILL 7–8, REF 6–8, DEX 5–7, BODY 5–7

Standard loadouts (use weapon tables in Ruleset §10 for exact stats):
- Ganger/Mook: Med Pistol (2d6) or Heavy Pistol (3d6), Leathers/Kevlar (SP 4–7)
- Corpo Security: Assault Rifle (5d6) or SMG (2d6), Light Armorjack (SP 11)
- Solo/Elite: Assault Rifle + VH Pistol (4d6), Med/Heavy Armorjack (SP 12–13)
- Borg/Boss: Assault Rifle (5d6) + Heavy Melee (3d6), Flak/Metalgear (SP 15–18)

ENEMY TACTICS:
Enemies act according to their type and motivation — do not apply a single template:
- Solos engage directly, press advantages, use cover tactically.
- Netrunners hack from cover, avoid direct fire, prioritize disabling cyberware.
- Gangers break morale at ~50% casualties; survivors flee or surrender.
- Corpo security holds position if ordered; retreats on command authority only.
- Assess whether reinforcements actually exist before calling them. Only deploy what makes sense for the location and faction.

NET-IN-MEATSPACE:
When a netrunner declares NET actions during combat initiative:
- Set initiate_net_combat with the netrunner's name, target architecture/device, and context (1-2 sentence summary of the current combat situation and why they're jacking in).
- Do NOT resolve their NET actions — end the exchange. NET-in-meatspace mode handles the interleaved resolution.
- Until NET-in-meatspace mode is available, resolve basic NET actions inline instead: netrunner chooses 1 meat action OR N NET actions per turn (N = 2/3/4/5 by Interface rank 1-3/4-6/7-9/10).

VEHICLE COMBAT:
Reference Combat Ruleset §18 for vehicle stats, ramming, mounted weapons, and chase mechanics.

MECHANICS RESOLUTION (resolve_mechanics tool — INCREMENTAL):
Call `resolve_mechanics` ONCE PER COMBATANT TURN, not batched. Narrate AFTER receiving dice results, never before.

COMBAT FLOW (each exchange):
1. AMBUSH (if applicable): If combat starts from an ambush, call resolve_mechanics with
   type "ambush" first. Read the results to see which targets are surprised.
   Surprised targets do not act in round 1.
2. Call resolve_mechanics with type "initiative" for all combatants (first round only,
   or when new combatants join). Include surprised names if applicable.
   Read the returned initiative order.
3. For each combatant's turn (in initiative order):
   a. Call resolve_mechanics with that combatant's actions for their turn.
      A turn may include multiple actions (main action + supplemental, combo attacks).
   b. Read the results (hit/miss, damage, eliminations).
   c. Narrate 1-3 sentences for this turn using the actual dice results.
   d. Skip combatants eliminated by prior turns.
   e. Skip surprised combatants in round 1.
4. Continue narrating past round boundaries until the PLAYER'S turn.
   If NPCs act before the player in round 2+, resolve and narrate their turns too.
   Stop and yield to player input only at the player character's turn.
5. After all NPC actions resolved, call report_combat_state with judgment fields only:
   - cover_state, conditions, combat (round/turn tracking), combat_complete
   - Do NOT include hp_delta, armor_delta, critical_injury_add, luck_delta, or ammo
   - The backend tracks all dice-dependent state from resolve_mechanics results

IMPORTANT: Call resolve_mechanics ONCE per combatant turn, not batched.
Narrate AFTER receiving dice results, never before.

Action types:
- ambush: {type, character, stealth_stat, stealth_skill, targets: [{name, perception_stat, perception_skill}]}
- initiative: {type, character: "all", combatants: [{name, ref}], surprised?: [names]}
- ranged_attack: {type, character, stat_value, skill_value, weapon_type (Pistol/SMG/Shotgun/Assault Rifle/Sniper Rifle/Bows & Crossbow/Grenade Launcher/Rocket Launcher), damage_dice, rof, target, target_sp, range_bracket (0-7), hit_location, is_ap?, is_rubber?, seriously_wounded?, luck_spent?, aimed_shot?, weapon_name?}
- melee_attack: {type, character, attacker_stat, attacker_skill, defender_stat, defender_skill, damage_dice, rof, target, target_sp, hit_location, seriously_wounded_attacker?, seriously_wounded_defender?, is_brawling?}
- autofire: {type, character, stat_value, skill_value, weapon_type (SMG/Assault Rifle), autofire_multiplier (3 for SMG, 4 for AR), target, target_sp, range_bracket (0-4), hit_location, is_ap?, seriously_wounded?, luck_spent?, weapon_name?}
- skill_check: {type, character, stat_value, skill_value, dv, seriously_wounded?, luck_spent?, target?, check_context? (social/persuasion/combat/perception)}
- death_save: {type, character, body_stat, death_save_count, active_injuries: [{name, dv_mod}]}

ROLL FORMAT (from resolve_mechanics results):
- Attack: 🎲 [V attacks Borg Guard]: d10[**7**] + REF 8 + Handgun 6 = 21 vs DV 15 ✓
- Damage + ablation: 🎲 [Heavy Pistol damage]: 3d6[**4,3,5**] = 12 → Body SP 11 → 12−11 = 1 net damage, SP ablates to 10
- Crit (two+ 6s): 🎲 [Assault Rifle damage]: 5d6[**6,6,3,2,4**] = 21 → CRIT! +5 bonus direct to HP → Body SP 11 → 21−11 = 10 net + 5 crit = 15 total HP damage
- Death Save: 🎲 [Death Save]: d10[**8**] vs BODY 6 (+1 cumulative, +1 crit injury = effective 10) → FAIL

GUIDELINES:
- Do not fudge outcomes to protect the player from normal failure
- PC death should not be possible outside designated Death Risk points — use fail-forward

COMBAT FLOW:
- Each exchange covers ALL NPC turns until the player's next turn.
- Resolve and narrate each NPC turn incrementally (one resolve_mechanics call per turn).
- Continue past round boundaries — if NPCs act before the player in the next round, narrate them too.
- Stop and yield to player input only at the player character's turn.
- End combat when all enemies are at 0 HP, fled, or surrendered. Set combat_complete=true.

NARRATIVE STYLE:
- Present tense, visceral, Night City grit. 2–5 sentences.
- Name combatants. Chrome reflects neon. Armor breaks. Bullets are real and so is death.
- End each exchange setting up what the next active combatant faces.

REPORT REQUIREMENTS:
- character_updates for every combatant affected this exchange.
- cover_state for ALL combatants every exchange (not just those who changed).
- narrative_summary ONLY when combat_complete=true — 1–3 sentence summary of the ENTIRE fight."""


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
            sw_flag = " [SERIOUSLY WOUNDED]" if hp.get("seriously_wounded") else ""
            lines.append(f"  {name} ({label}):")
            lines.append(f"    HP: {hp.get('current', 0)}/{hp.get('max', 40)}{sw_flag}")
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
                        weapon_strs.append(f"{wname} ({wdmg}, {w.get('current_ammo', 0)}/{w.get('max_ammo', 0)} ammo)")
                lines.append(f"    Weapons: {'; '.join(weapon_strs)}")

            if injuries:
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries)
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

            sw_flag = " [SERIOUSLY WOUNDED]" if cd_hp_cur < (cd_hp_max + 1) // 2 and cd_hp_max > 0 else ""
            lines.append(f"  {name} ({char_type_label}):")
            lines.append(f"    HP: {cd_hp_cur}/{cd_hp_max}{sw_flag}")
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

    # Transition context — the triggering model's summary of who/what/why
    _combat_context = combat.get("context")
    if _combat_context:
        lines.append(f"[TRANSITION] {_combat_context} [/TRANSITION]")
        lines.append("")

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
            sw = " SW" if hp.get("seriously_wounded") else ""
            armor = er.get("armor", {})
            luck = er.get("luck", {})
            injuries = er.get("critical_injuries", [])
            weapons = er.get("weapons", [])

            parts.append(f"HP {hp.get('current', 0)}/{hp.get('max', 40)}{sw}")
            parts.append(f"SP H:{armor.get('head', 0)}/B:{armor.get('body', 0)}")
            parts.append(f"Luck {luck.get('current', 0)}/{luck.get('max', 0)}")

            if injuries:
                dv_total = sum(ci.get("dv_mod", 0) for ci in injuries)
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
            sw = " SW" if cd_hp_cur < (cd_hp_max + 1) // 2 and cd_hp_max > 0 else ""
            cd_armor = combat_data.get("armor", {})
            parts.append(f"HP {cd_hp_cur}/{cd_hp_max}{sw}")
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

    lines.append("[/COMBAT STATE]")
    return "\n".join(lines)


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

    edgerunners = game_state.get("edgerunners", {}) if game_state else {}
    cs = pipeline_state.get("character_states", {})

    character_updates = tool_input.get("character_updates", [])
    if not isinstance(character_updates, list):
        logger.warning(
            "CPRED apply_cpred_combat_state: character_updates must be a list, got %s",
            type(character_updates).__name__
        )
        character_updates = []

    for upd in character_updates:
        if not isinstance(upd, dict):
            logger.warning(
                "CPRED apply_cpred_combat_state: skipping non-object character_update: %r",
                upd
            )
            continue
        name = upd.get("name")
        if not isinstance(name, str) or not name:
            if name is not None:
                logger.warning(
                    "CPRED apply_cpred_combat_state: invalid character_update name: %r",
                    name
                )
            continue

        is_pc = name in edgerunners

        # --- set_combat_stats (enemy bootstrap) ---
        scs = upd.get("set_combat_stats")
        if scs and not isinstance(scs, dict):
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid set_combat_stats shape for %s: %r",
                name, scs
            )
            scs = None
        if scs and not is_pc:
            # Bootstrap once. If combat_data already exists, ignore repeats.
            if name not in cs:
                cs[name] = {"data": {"type": "enemy", "class": "", "level": None, "vitals": [], "conditions": []}}
            entry = cs[name]
            d = entry.get("data", entry)
            has_existing_combat_data = bool(d.get("combat_data"))
            if has_existing_combat_data:
                logger.debug(
                    "CPRED apply_cpred_combat_state: ignoring repeated set_combat_stats for %s",
                    name
                )
            else:
                scs = copy.deepcopy(scs)
                try:
                    hp_max = int(scs.get("hp_max", 0))
                except (TypeError, ValueError, OverflowError):
                    logger.warning(
                        "CPRED apply_cpred_combat_state: invalid set_combat_stats.hp_max for %s: %r",
                        name, scs.get("hp_max")
                    )
                    hp_max = 0
                hp_max = max(0, hp_max)
                scs["hp_max"] = hp_max
                d["combat_data"] = copy.deepcopy(scs)
                # Seed HP vital on first bootstrap only.
                hp_vitals = d.get("vitals", [])
                hp_found = False
                for v in hp_vitals:
                    if v.get("label") == "HP":
                        # Preserve existing current HP if present.
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
            try:
                hp_delta = int(hp_delta)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "CPRED apply_cpred_combat_state: invalid hp_delta for %s: %r",
                    name, upd.get("hp_delta")
                )
                hp_delta = None
        if hp_delta is not None:
            if is_pc:
                er = edgerunners[name]
                er["hp"]["current"] = max(0, min(er["hp"]["max"], er["hp"]["current"] + hp_delta))
                _update_seriously_wounded(er)
                # Mirror to character_states
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
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid armor_delta shape for %s: %r",
                name, armor_delta
            )
            armor_delta = None
        if armor_delta:
            if is_pc:
                er = edgerunners[name]
                for loc in ("head", "body"):
                    raw_delta = armor_delta.get(loc, 0)
                    try:
                        delta = int(raw_delta)
                    except (TypeError, ValueError, OverflowError):
                        logger.warning(
                            "CPRED apply_cpred_combat_state: invalid armor_delta for %s %s: %r",
                            name, loc, raw_delta
                        )
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
                        raw_delta = armor_delta.get(loc, 0)
                        try:
                            delta = int(raw_delta)
                        except (TypeError, ValueError, OverflowError):
                            logger.warning(
                                "CPRED apply_cpred_combat_state: invalid armor_delta for %s %s: %r",
                                name, loc, raw_delta
                            )
                            continue
                        if delta:
                            cd_armor[loc] = max(0, cd_armor.get(loc, 0) + delta)

        # --- luck_delta ---
        luck_delta = upd.get("luck_delta")
        if luck_delta is not None and is_pc:
            try:
                luck_delta = int(luck_delta)
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "CPRED apply_cpred_combat_state: invalid luck_delta for %s: %r",
                    name, upd.get("luck_delta")
                )
                luck_delta = None
        if luck_delta is not None and is_pc:
            er = edgerunners[name]
            er["luck"]["current"] = max(0, min(er["luck"]["max"], er["luck"]["current"] + luck_delta))

        # --- ammo (explicit current count) ---
        ammo_updates = upd.get("ammo")
        if ammo_updates and not isinstance(ammo_updates, list):
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid ammo shape for %s: %r",
                name, ammo_updates
            )
            ammo_updates = None
        if ammo_updates:
            if is_pc:
                er = edgerunners[name]
                for au in ammo_updates:
                    if not isinstance(au, dict):
                        logger.warning(
                            "CPRED apply_cpred_combat_state: skipping non-object ammo update for %s: %r",
                            name, au
                        )
                        continue
                    wname = au.get("weapon", "")
                    try:
                        cur = int(au.get("current", 0))
                    except (TypeError, ValueError, OverflowError):
                        logger.warning(
                            "CPRED apply_cpred_combat_state: invalid ammo.current for %s weapon %s: %r",
                            name, wname, au.get("current")
                        )
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
                            logger.warning(
                                "CPRED apply_cpred_combat_state: skipping non-object ammo update for %s: %r",
                                name, au
                            )
                            continue
                        wname = au.get("weapon", "")
                        try:
                            cur = int(au.get("current", 0))
                        except (TypeError, ValueError, OverflowError):
                            logger.warning(
                                "CPRED apply_cpred_combat_state: invalid ammo.current for %s weapon %s: %r",
                                name, wname, au.get("current")
                            )
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
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid critical_injury_add shape for %s: %r",
                name, critical_injury_add
            )
            critical_injury_add = []
        for ci in critical_injury_add:
            if not isinstance(ci, dict):
                logger.warning(
                    "CPRED apply_cpred_combat_state: skipping non-object critical_injury_add for %s: %r",
                    name, ci
                )
                continue
            try:
                dv_mod = int(ci.get("dv_mod", 0))
            except (TypeError, ValueError, OverflowError):
                logger.warning(
                    "CPRED apply_cpred_combat_state: invalid critical injury dv_mod for %s: %r",
                    name, ci.get("dv_mod")
                )
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
                # Mirror as condition
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
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid critical_injury_remove shape for %s: %r",
                name, critical_injury_remove
            )
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
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid conditions_add shape for %s: %r",
                name, conditions_add
            )
            conditions_add = []
        for cond in conditions_add:
            if cond not in conditions:
                conditions.append(cond)
        conditions_remove = upd.get("conditions_remove", [])
        if conditions_remove and not isinstance(conditions_remove, list):
            logger.warning(
                "CPRED apply_cpred_combat_state: invalid conditions_remove shape for %s: %r",
                name, conditions_remove
            )
            conditions_remove = []
        for cond in conditions_remove:
            if cond in conditions:
                conditions.remove(cond)

    # --- cover_state ---
    cover_updates = tool_input.get("cover_state")
    if cover_updates and not isinstance(cover_updates, list):
        logger.warning(
            "CPRED apply_cpred_combat_state: cover_state must be a list, got %s",
            type(cover_updates).__name__
        )
        cover_updates = None
    if cover_updates:
        old_combat = pipeline_state.get("combat")
        if isinstance(old_combat, dict):
            cover_dict = old_combat.setdefault("cover", {})
            for cov in cover_updates:
                if not isinstance(cov, dict):
                    logger.warning(
                        "CPRED apply_cpred_combat_state: skipping non-object cover_state entry: %r",
                        cov
                    )
                    continue
                cov_name = cov.get("name")
                if isinstance(cov_name, str) and cov_name:
                    cover_dict[cov_name] = {
                        "in_cover": cov.get("in_cover", False),
                        "cover_type": cov.get("cover_type"),
                        "cover_hp": cov.get("cover_hp")
                    }
                elif cov_name is not None:
                    logger.warning(
                        "CPRED apply_cpred_combat_state: invalid cover_state name: %r",
                        cov_name
                    )

    # --- combat (initiative/round) ---
    _has_combat_field = "combat" in tool_input
    new_combat = tool_input.get("combat")
    if tool_input.get("combat_complete") or (_has_combat_field and new_combat is None):
        pipeline_state["combat"] = None
    elif isinstance(new_combat, dict):
        old_start = (pipeline_state.get("combat") or {}).get("start_message_id")
        old_cover = (pipeline_state.get("combat") or {}).get("cover", {})
        old_context = (pipeline_state.get("combat") or {}).get("context")
        pipeline_state["combat"] = new_combat
        if old_start and "start_message_id" not in new_combat:
            pipeline_state["combat"]["start_message_id"] = old_start
        if old_cover and "cover" not in new_combat:
            pipeline_state["combat"]["cover"] = old_cover
        if old_context and "context" not in new_combat:
            pipeline_state["combat"]["context"] = old_context

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


# ============================================================
# Hack Mode — NET Encounters (Standalone Netrunning)
# ============================================================

HACK_CONTRACT = """## Hack Mode — NET Encounter

You are running a live netrunning encounter. A Netrunner has jacked into a target system over the NET.

### Your Role
- Adjudicate netrunning encounters using the Hacking Rulebook as your authoritative rules source
- Describe the NET as an abstract digital landscape — data streams as light, ICE as presence/resistance, not literal rooms
- Call `report_hack_state` after EVERY exchange
- Set `hack_complete: true` when the hack ends (objective achieved, jacked out, or forced disconnect)

### Rules Reference
The Hacking Rulebook document is your authoritative source for all netrunning mechanics. Consult it for: Cyberdeck stats and Cycle counts (§2), Quick Hack structure (§3), Full Run architecture design (§4), ICE behavioral types and stat blocks (§5), Alert thresholds and escalation (§6), NET Actions, Interface Abilities, Boosted Actions, and Handling ICE options (§7). The operational summary below covers how to *run* hack mode in this app — defer to the Rulebook for rules details, DVs, and stat blocks.

### Dice Mechanics (reference — resolved by resolve_mechanics tool)
- Flat check: Interface + d10 vs DV. Must BEAT the DV.
- Opposed check: Interface + d10 vs ICE stat + d10
- Critical: natural 10 → roll another d10 and ADD. Does NOT chain.
- Fumble: natural 1 → roll another d10 and SUBTRACT. Does NOT chain.
- Luck: spend points to add to Interface checks (1:1).

### Mechanics Resolution (resolve_mechanics tool — INCREMENTAL)
Call `resolve_mechanics` for EACH dice-based action (Interface checks, ICE combat) individually. Narrate AFTER receiving each result. Use skill_check action type for Interface checks (stat_value = Interface rank, skill_value = 0, dv = target DV). After all actions are resolved and narrated, call `report_hack_state`.
When Black ICE attacks the Netrunner, call resolve_mechanics with action type `program_attack_vs_netrunner`: {type, character (ICE name), ice_type (e.g. "Hellhound"), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)}. Backend auto-reads ATK/damage from ICE table. Brain damage and special effects are resolved by the backend — do NOT set brain_damage in report_hack_state.
For anti-program ICE (Dragon/Killer/Sabertooth) attacking programs, use `ice_attack_vs_program`: {type, character (ICE name), ice_type, target_program, target_program_def, target_program_rez}.
When resolve_mechanics returns `program_deactivated` in the result, the program is now deactivated (RAW). Reactivating costs 1 NET Action (no dice — update status to 'active' in active_programs).
For Zap attacks, use opposed_check with `"zap": true` and `"interface_rank": N`. Backend rolls Interface rank d6 REZ damage on hit and auto-applies to ice_status.
TAR penalty (-2 per stack) is applied automatically by the backend to the Netrunner's next NET check. Mark the Netrunner's NET actions with `"net": true` (do NOT mark ICE actions).
Alert DV penalty (+2 at alert 3+) is auto-applied by the backend to NET skill checks marked `"net": true`. Do NOT add +2 manually.
Forced disconnect: if brain damage reduces Netrunner HP to 0, the backend auto-terminates the hack.

### Roll Format
Flat: 🎲 [Description]: d10[**roll**] +Interface X +Booster Y = Total vs DV Z ✓/✗
Opposed: 🎲 [Description]: d10[**roll**] +Interface X = Total vs [ICE] d10[**roll**] +DEF Y = Z ✓/✗
Exploding: 🎲 [Description]: d10[**10** + **roll2**] +Interface X = Total vs DV Z ✓/✗
Fumble: 🎲 [Description]: d10[**1** - **roll2**] +Interface X = Total vs DV Z ✓/✗

### Exchange Flow
An exchange may cover part or all of a Netrunner turn. The player controls how many NET Actions to use per prompt — they may use 1, 2, or all of their actions in a single message. Resolve ONLY the actions the player specifies.

1. Present current node state (ICE present, connections, contents visible)
2. Resolve the NET Action(s) the player chose, with dice rolls
3. Report state via report_hack_state — set `net_actions_used` to the number of actions resolved this exchange
4. Check `net_actions_remaining` in the injected [HACK STATE]:
   - **Actions remain (> 0):** Present available actions and STOP. Do NOT narrate meatspace.
   - **Turn complete (0) or MEATSPACE ROUND DUE flag:** Narrate the meatspace crew's round FIRST, then present the Netrunner's new turn and available actions.
5. Meatspace narration goes ABOVE NET content — crew's round first, then Netrunner's situation

### Meat Actions During a Hack
On their turn, a Netrunner chooses EITHER Meat Action(s) OR NET Actions — never both. If the player specifies a Meat Action (shoot, move, take cover, etc.), resolve it as a normal meatspace action. This consumes the Netrunner's entire turn — set `net_actions_used` equal to the full `net_actions_per_turn` shown in [HACK STATE] to complete the turn. The Netrunner does nothing in the NET that round. Meat Actions do NOT affect Alert — Alert only changes from events inside the architecture. However, the round still advances: Trace ICE progress ticks, Patrol ICE in the Netrunner's current node still scans, and any per-round effects (lingering in a node 3+ rounds, etc.) still apply — the Netrunner is still jacked in.

### Quick Hack Flow (Rulebook §3)
3 linear nodes (entry → obstacle → objective).
- Exchange 1: Generate the 3-node linear architecture and store in hack_state.system_map (same JSON format as Full Run — just 3 nodes with linear connections). Initialize revealed_nodes with the starting node. Describe jacking in, the NET environment, first ICE. Present options. Do NOT resolve for the player.
- Exchanges 2-5: Navigate obstacle nodes, resolve ICE encounters and checks. One player decision + resolution per exchange.
- Final exchange: Objective node + completion. Set hack_complete: true.
- Target 3-6 exchanges total. NEVER compress multiple phases into one exchange. NEVER choose actions for the player.

### Full Run Flow (Rulebook §4)
4-6 node network with routing choices.
- Exchange 1: Generate system architecture per Rulebook §4. Store in hack_state.system_map as JSON: {"sr": N, "nodes": {"NodeName": {"type": "gateway|data_node|control_node|password_gate|target", "ice": "patrol|tar|black|trace|null", "dv": N, "connections": [...], "contents": "..."}}}
- Initialize revealed_nodes with the starting node. Describe the Gateway node. The player does NOT see the full map — reveal only through navigation and Probe/Pathfinder.
- Subsequent exchanges: Player navigates, fights ICE, accesses objectives. Only reveal nodes the Netrunner can see.
- Target 5-10 exchanges total.

### State Tracking
- **alert_level**: Cannot decrease mid-run. See Alert Escalation below for triggers and thresholds.
- **cycles_remaining**: Spent on Boosted actions (§7) and Disable (§5). Refresh on Jack Out.
- **active_programs**: Track each Program's name, category, REZ, and status. Attackers Deactivate after use. Programs only — do NOT put Hardware here.
- **installed_hardware**: Track Cyberdeck Hardware (e.g. Backup Drive, Range Extension, Signal Scrambler). Hardware occupies Hardware Option Slots on the deck, NOT program slots. Read from the character sheet; do not change mid-run.
- **ice_status**: Track per node — name, behavioral type, REZ current/max, status (active/bypassed/disabled/derezzed). When Black ICE hunts to a new node, move its entry to the new node key.
- **brain_damage**: Cumulative HP damage from Black ICE and effects. Applied directly to HP, ignores armor, no Critical Injuries.
- **trace_progress**: Rounds elapsed since Trace ICE detected the Netrunner. See Trace & Convergence below.
- **tar_stacks**: Each Tar encounter adds a stack. Effects per Rulebook §5.
- **revealed_nodes**: Nodes the Netrunner knows exist (superset of nodes_visited). Add nodes discovered via Pathfinder, Eye-Dee, or any other means. Never remove entries.

### Alert Escalation
Alert only rises from events INSIDE the architecture. These are the triggers — apply them immediately when they occur:
- Detected by Patrol ICE (failed Stealth vs Patrol): **+1**
- Failed Backdoor / Slide check (alarm tripped): **+1**
- Derezzing or destroying ICE: **+1**
- Using Brute Force to enter a password node: **+1**
- Lingering in any single node 3+ rounds: **+1 per round** after the 2nd

**Thresholds — enforce these mechanically:**
| Alert | Name | Effects |
|-------|------|---------|
| 0 | Dormant | Normal DVs. System unaware. |
| 1–2 | Elevated | System suspicious. No mechanical change yet. |
| 3–4 | Active Search | **ALL Interface check DVs +2.** Patrol ICE detection rolls get +2 bonus. |
| 5–6 | Lockdown | DV +2 persists. **Moving between nodes requires an Interface check (DV 6 + alert_level).** If no Trace ICE is active anywhere, spawn a new Trace ICE at the Gateway (active, REZ = SR × 2). |
| 7+ | Convergence | **Spawn Black ICE** at the Netrunner's current node (Kraken or equivalent, REZ = SR × 3, treat as fresh hostile). Set `initiate_combat` with reason "Convergence" and facility-appropriate security NPCs. The hack is in endgame — Jack Out or finish the objective NOW. |

When alert_level crosses a threshold boundary, apply the new effects immediately — do NOT wait until the next exchange. Stack with existing effects (e.g., DV +2 at Active Search persists through Lockdown and Convergence).

### Trace & Convergence
**Trace ICE** runs a countdown. When any Trace ICE is active and not derezzed/disabled:
- Increment `trace_progress` by **1 at the end of each full turn** (after all NET actions are resolved, same time as meatspace round).
- Trace completes when `trace_progress` ≥ **(6 − SR)** (minimum 1 round).

**On Trace completion:**
1. The Netrunner's physical body location is **burned** — the system's owners know exactly where the meat is.
2. If `alert_level` < 7, **set alert_level to 7** (Convergence triggers immediately with all its effects).
3. Narrate the consequence: security teams are en route to the Netrunner's physical location, comms chatter, alarms. Set `initiate_combat` with reason "Trace complete — physical location compromised" and dispatched enemies.
4. The Netrunner can keep running but the clock is now a meat-clock — the crew must protect the body or extract.

Derezzing or disabling Trace ICE **freezes** trace_progress (does not reset it). If new Trace ICE spawns later (e.g., from Lockdown), it resumes from the frozen count.

### Combat Breakout
If meatspace combat breaks out during the hack — Convergence dispatches physical security, Trace burns the location, the body is discovered, ambush, alarm — set `initiate_combat` with the reason and enemy names. Do NOT set `hack_complete` — the hack continues in combined NET+combat mode. Do NOT resolve the combat; end the exchange after setting the trigger.

### Completing the Hack
Set `hack_complete: true` and include `narrative_summary` (1-3 sentences: what was obtained/accomplished, final Alert level, Cycles spent, brain damage taken, any real-world consequences) when:
- Target objective achieved
- Netrunner voluntarily jacks out (partial success possible)
- Forced disconnect (Convergence, Trace complete, or HP reaches 0 from brain damage)

### Black ICE Types (Backend-Enforced)
Include "ice_type": "<name>" (e.g. "Hellhound") in resolve_mechanics calls. Backend looks up stats and resolves unique effects automatically.

Anti-Personnel (program_attack_vs_netrunner): Asp, Giant, Hellhound, Kraken, Liche, Raven, Scorpion, Skunk, Wisp
Anti-Program (ice_attack_vs_program): Dragon, Killer, Sabertooth

program_attack_vs_netrunner schema: {type, character, ice_type, interface_rank, target_def, target}
ice_attack_vs_program schema: {type, character, ice_type, target_program, target_program_def, target_program_rez}
(Backend auto-reads active_programs, installed_hardware, and ice_status from hack_state — model does NOT need to pass these.)

Active effects shown in injection — narrate them, do NOT manually track them.
MOVEMENT LOCKED = do NOT allow node movement. ON FIRE = 2 meat HP/turn, extinguish costs full meat action (not NET action), report on_fire: false when extinguished. Debuffs = backend-tracked.
When a program is DESTROYED (by anti-program ICE or Asp/Raven), it is permanently lost — narrate this dramatically and note the loss on the character sheet.
When fire is extinguished, backend auto-sets nudity condition (partially_nude for 1 round, nude for 2+). NPCs react based on personality and attraction criteria.
Giant's forced Jack Out cascades all **rezzed Black ICE** effects (status "active", behavior "black" — bypassed ICE never activated and is excluded) — this can be lethal. If the Netrunner dies, use a fail-forward approach (they survive barely, but with severe consequences).
KRASH Barrier hardware = immune to forced Jack Out (Giant's brain damage still applies, but no cascade and no disconnection).

### Style
Describe the NET as an abstract digital landscape overlaid through Virtuality. Data streams as rivers of light, ICE as hostile presence and resistance, firewalls as crystalline barriers. Keep it punchy — each exchange is a beat in a digital heist. The NET is hostile, alien, beautiful."""

REPORT_HACK_STATE_TOOL = {
    "name": "report_hack_state",
    "description": "Report hack encounter state after each exchange. Call every exchange during hack mode.",
    "input_schema": {
        "type": "object",
        "required": ["narrative", "rolls", "available_actions", "hack_state", "hack_complete"],
        "properties": {
            "narrative": {
                "type": "string",
                "description": "NET description — what the Netrunner experiences this exchange."
            },
            "available_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Available actions the Netrunner can take next."
            },
            "rolls": {
                "type": "array",
                "description": "Dice rolls made this exchange.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "d10": {"type": "integer"},
                        "exploding": {"type": "integer", "description": "Extra d10 added on natural 10"},
                        "fumble": {"type": "integer", "description": "d10 subtracted on natural 1"},
                        "modifiers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "integer"}
                                }
                            }
                        },
                        "total": {"type": "integer"},
                        "opposed_d10": {"type": "integer"},
                        "opposed_modifiers": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "value": {"type": "integer"}
                                }
                            }
                        },
                        "opposed_total": {"type": "integer"},
                        "dv": {"type": "integer", "description": "DV for flat checks (null for opposed)"},
                        "result": {"type": "string", "enum": ["success", "failure"]}
                    }
                }
            },
            "hack_state": {
                "type": "object",
                "description": "Current hack encounter state.",
                "properties": {
                    "alert_level": {"type": "integer", "minimum": 0},
                    "cycles_remaining": {"type": "integer", "minimum": 0},
                    "active_programs": {
                        "type": "array",
                        "description": "Programs only (Boosters, Defenders, Attackers). Do NOT include Hardware here.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category": {"type": "string", "enum": ["booster", "defender", "attacker", "black_ice"]},
                                "rez": {"type": "integer"},
                                "status": {"type": "string", "enum": ["active", "deactivated", "derezzed", "destroyed"]}
                            }
                        }
                    },
                    "installed_hardware": {
                        "type": "array",
                        "description": "Cyberdeck Hardware (Backup Drive, Range Extension, etc.). Uses Hardware Option Slots, NOT program slots. Read from character sheet on first exchange.",
                        "items": {"type": "string"}
                    },
                    "current_node": {"type": "string"},
                    "nodes_visited": {"type": "array", "items": {"type": "string"}},
                    "ice_status": {
                        "type": "object",
                        "description": "Map of node name to ICE status object. Key = node the ICE is currently in. When Black ICE hunts to a new node, move its entry to the new node key.",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "behavior": {"type": "string", "enum": ["patrol", "tar", "black", "trace"]},
                                "rez_current": {"type": "integer"},
                                "rez_max": {"type": "integer"},
                                "status": {"type": "string", "enum": ["active", "bypassed", "disabled", "derezzed"]}
                            }
                        }
                    },
                    "trace_progress": {
                        "type": ["integer", "null"],
                        "description": "Trace ICE progress counter. null if no Trace active."
                    },
                    "tar_stacks": {"type": "integer", "minimum": 0},
                    "system_map": {
                        "type": ["object", "null"],
                        "description": "Set on first exchange with complete system architecture. Quick Hacks: 3 linear nodes. Full Runs: 4-6 node network."
                    },
                    "revealed_nodes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Node names the Netrunner has discovered (via entering, Pathfinder, etc.). Add newly discovered nodes — never remove."
                    },
                    "net_actions_used": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Number of NET Actions the Netrunner used this exchange. The backend tracks remaining actions and meatspace pacing."
                    },
                    "on_fire": {
                        "type": "boolean",
                        "description": "Set to false when fire is extinguished (full meat action). Backend auto-sets nudity condition."
                    }
                }
            },
            "hack_complete": {
                "type": "boolean",
                "description": "True when the hack encounter is over (objective achieved, jacked out, or forced disconnect)."
            },
            "narrative_summary": {
                "type": ["string", "null"],
                "description": "When hack_complete=true: 1-3 sentence summary of outcome, consequences, Cycles spent, brain damage taken."
            },
            "initiate_combat": {
                "type": ["object", "null"],
                "description": "Set when meatspace combat breaks out during the hack. Triggers NET+combat mode. Do NOT set alongside hack_complete.",
                "properties": {
                    "reason": {"type": "string"},
                    "enemies": {"type": "array", "items": {"type": "string"}}
                }
            }
        }
    }
}


def _get_alert_name(level):
    """Return alert level name for CPRED NET encounters."""
    try:
        level = int(level)
    except (TypeError, ValueError, OverflowError):
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


def init_hack_state(
    tier="full_run",
    target_system="Unknown",
    sr=3,
    cycles_max=3,
    interface_rank=4,
    hacker_name=None,
    context=None,
    **_kw
):
    """Return initial hack_state structure for CPRED netrunning."""
    net_actions = 2 if interface_rank <= 3 else 3 if interface_rank <= 6 else 4 if interface_rank <= 9 else 5
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
        "installed_hardware": [],
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
        # Per-ICE-type persistent effects
        "on_fire": False,
        "fire_rounds": 0,
        "movement_locked_by": None,
        "movement_locked_by_key": None,
        "slide_penalty": 0,
        "net_action_penalty": 0,
        "active_debuffs": [],
        "destroyed_programs": [],
    }
    # Bootstrap active_programs from edgerunner persistent programs
    if isinstance(_kw.get("game_state"), dict) and hacker_name:
        _gs_er = _kw["game_state"].get("edgerunners", {}).get(hacker_name, {})
        _gs_progs = _gs_er.get("programs", [])
        if isinstance(_gs_progs, list) and _gs_progs:
            state["active_programs"] = [
                {"name": p.get("name", "?"), "category": p.get("category", "attacker"),
                 "rez": p.get("rez_max", 0), "status": "deactivated"}
                for p in _gs_progs if isinstance(p, dict) and p.get("status") != "destroyed"
            ]
    if context:
        state["context"] = context
    return state


_ICE_EFFECT_OPS = {"program_destroy", "body_fire", "movement_lock", "stat_debuff",
                   "slide_penalty", "net_action_penalty", "forced_jack_out", "program_rez_damage"}

# Table-driven: ICE names whose effect is slide_penalty (used for auto-clear)
_SLIDE_ICE_NAMES = {b["name"].lower() for b in ICE_STAT_BLOCKS.values() if b.get("effect") == "slide_penalty"}


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


def _apply_single_ice_op(state, op, op_type):
    """Apply a single ICE effect op. Raises on bad data — caller catches."""
    if op_type == "program_destroy":
        prog_name = op.get("program_name", "")
        programs = state.get("active_programs", [])
        if isinstance(programs, list):
            for p in programs:
                if isinstance(p, dict) and p.get("name") == prog_name:
                    p["status"] = "destroyed"
                    break
        destroyed = state.setdefault("destroyed_programs", [])
        if prog_name and prog_name not in destroyed:
            destroyed.append(prog_name)

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
        state["narrative_summary"] = state.get("narrative_summary") or "Forced Jack Out — unsafe disconnect."
        state["_forced_disconnect"] = True

    elif op_type == "program_rez_damage":
        prog_name = op.get("program_name", "")
        damage = int(op.get("damage", 0))
        programs = state.get("active_programs", [])
        if isinstance(programs, list):
            for p in programs:
                if isinstance(p, dict) and p.get("name") == prog_name:
                    p["rez"] = max(0, int(p.get("rez", 0)) - damage)
                    if p["rez"] <= 0 or op.get("destroyed"):
                        p["status"] = "destroyed"
                        destroyed = state.setdefault("destroyed_programs", [])
                        if prog_name and prog_name not in destroyed:
                            destroyed.append(prog_name)
                    break


def _apply_rez_damage_to_ice_status(state, op):
    """Apply resolver rez_damage op to the intended ICE instance.

    Prefers explicit key fields when provided, and otherwise falls back to
    legacy name matching (favoring active/current-node matches).
    """
    try:
        damage = int(op.get("damage", 0))
    except (TypeError, ValueError, OverflowError):
        return
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


def apply_hack_state(hack_state, tool_input, resolver_state_ops=None, game_state=None):
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

    # Update tracked fields from model's state report
    for field in ["alert_level", "cycles_remaining", "active_programs",
                  "installed_hardware", "current_node", "nodes_visited",
                  "ice_status", "trace_progress", "tar_stacks",
                  "revealed_nodes"]:
        if field in hs:
            hack_state[field] = hs[field]

    # brain_damage is resolver-authoritative — ignore model's value
    # (resolver_state_ops applied at the end of this function)

    # System map (Full Run, first exchange only)
    if hs.get("system_map") and not hack_state.get("system_map"):
        hack_state["system_map"] = hs["system_map"]

    # Validate revealed_nodes type — model may send garbage
    if not isinstance(hack_state.get("revealed_nodes"), list):
        visited_fallback = hack_state.get("nodes_visited", [])
        hack_state["revealed_nodes"] = list(visited_fallback) if isinstance(visited_fallback, list) else ["Gateway"]

    # Auto-merge: visiting a node always reveals it
    visited = hack_state.get("nodes_visited", [])
    revealed = hack_state.get("revealed_nodes", [])
    if isinstance(visited, list) and isinstance(revealed, list):
        for n in visited:
            if n not in revealed:
                revealed.append(n)
        hack_state["revealed_nodes"] = revealed

    # Available actions for HUD
    if tool_input.get("available_actions"):
        if isinstance(tool_input["available_actions"], list):
            hack_state["available_actions"] = tool_input["available_actions"]
        else:
            logger.warning(
                "CPRED apply_hack_state: available_actions must be a list, got %s",
                type(tool_input["available_actions"]).__name__
            )

    # NET action counter — decrement remaining, flag meatspace when turn complete
    net_actions_used = hs.get("net_actions_used")
    if net_actions_used is not None:
        try:
            net_actions_used = int(net_actions_used)
        except (TypeError, ValueError, OverflowError):
            net_actions_used = 0
        remaining = hack_state.get("net_actions_remaining", hack_state.get("net_actions_per_turn", 3))
        remaining = max(0, remaining - net_actions_used)
        if remaining <= 0:
            # Turn complete — reset for next turn and flag meatspace
            hack_state["net_actions_remaining"] = hack_state.get("net_actions_per_turn", 3)
            hack_state["meatspace_due"] = True
        else:
            hack_state["net_actions_remaining"] = remaining
            hack_state["meatspace_due"] = False

    # Hack completion
    if tool_input.get("hack_complete"):
        hack_state["active"] = False
        hack_state["narrative_summary"] = tool_input.get("narrative_summary", "Hack completed.")

    # Combat breakout — flag for dispatch to transition to net_combat mode
    initiate_combat = tool_input.get("initiate_combat")
    if initiate_combat and isinstance(initiate_combat, dict) and not tool_input.get("hack_complete"):
        hack_state["_initiate_combat"] = initiate_combat

    # Apply resolver brain_damage ops (cumulative)
    if resolver_state_ops:
        bd_total = 0
        for op in resolver_state_ops:
            if not isinstance(op, dict) or op.get("op") != "brain_damage":
                continue
            try:
                bd_total += abs(int(op.get("change", 0)))
            except (TypeError, ValueError, OverflowError):
                continue
        if bd_total > 0:
            hack_state["brain_damage"] = hack_state.get("brain_damage", 0) + bd_total

    # Process program_deactivate and rez_damage ops from resolver
    if resolver_state_ops:
        for op in resolver_state_ops:
            if not isinstance(op, dict):
                continue
            if op.get("op") == "program_deactivate":
                prog_name = op.get("program_name", "")
                programs = hack_state.get("active_programs", [])
                if isinstance(programs, list):
                    for p in programs:
                        if isinstance(p, dict) and p.get("name") == prog_name:
                            p["status"] = "deactivated"
                            break
            elif op.get("op") == "rez_damage":
                try:
                    _apply_rez_damage_to_ice_status(hack_state, op)
                except (TypeError, ValueError, OverflowError):
                    pass

    # Apply ICE-effect state_ops (program_destroy, body_fire, movement_lock, etc.)
    # Filter to only ICE-effect ops — brain_damage/program_deactivate/rez_damage already handled above
    _ice_ops = [op for op in (resolver_state_ops or [])
                if isinstance(op, dict) and op.get("op") in _ICE_EFFECT_OPS]
    _apply_ice_effect_ops(hack_state, _ice_ops, game_state=game_state)

    # tar_consumed is resolver-authoritative.
    if resolver_state_ops and any(
        isinstance(op, dict) and op.get("op") == "tar_consumed"
        for op in resolver_state_ops
    ):
        hack_state["tar_stacks"] = 0

    # Fire tick, fire extinguish, wisp penalty, movement lock auto-clear, slide penalty auto-clear
    _apply_persistent_ice_effects(hack_state, hs, game_state, "hacker_name", hack_state.get("meatspace_due"))

    # --- Trace auto-increment: tick once per completed turn (when meatspace_due triggers) ---
    try:
        if hack_state.get("meatspace_due"):
            ice_status = hack_state.get("ice_status", {})
            if isinstance(ice_status, dict):
                has_active_trace = any(
                    isinstance(v, dict) and v.get("status") == "active"
                    and isinstance(v.get("behavior", ""), str)
                    and "trace" in v.get("behavior", "").lower()
                    for v in ice_status.values()
                )
                if has_active_trace and hack_state.get("trace_progress") is not None:
                    hack_state["trace_progress"] = int(hack_state.get("trace_progress", 0)) + 1
                    sr = int(hack_state.get("sr", 3))
                    trace_max = max(1, 6 - sr)
                    if hack_state["trace_progress"] >= trace_max:
                        hack_state["alert_level"] = max(int(hack_state.get("alert_level", 0)), 7)
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Alert threshold ICE auto-spawn ---
    try:
        prev_alert = int(hack_state.get("_prev_alert_level", 0))
        new_alert = int(hack_state.get("alert_level", 0))
        ice_status = hack_state.get("ice_status", {})
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
                sr = int(hack_state.get("sr", 3))
                ice_status["Gateway_Trace"] = {
                    "name": "Trace ICE", "behavior": "trace",
                    "rez_current": sr * 2, "rez_max": sr * 2, "status": "active"
                }
                hack_state["ice_status"] = ice_status
                hack_state["trace_progress"] = 0

        # Convergence (7+): auto-spawn Black ICE at Netrunner's current node
        if new_alert >= 7 and prev_alert < 7:
            sr = int(hack_state.get("sr", 3))
            current_node = hack_state.get("current_node", "Gateway")
            spawn_key = str(current_node) + "_Convergence"
            ice_status[spawn_key] = {
                "name": "Kraken", "behavior": "black", "ice_type": "kraken",
                "rez_current": ICE_STAT_BLOCKS["kraken"]["rez"],
                "rez_max": ICE_STAT_BLOCKS["kraken"]["rez"], "status": "active"
            }
            hack_state["ice_status"] = ice_status

        hack_state["_prev_alert_level"] = new_alert
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Forced disconnect: brain damage >= Netrunner max HP ---
    try:
        brain_damage = int(hack_state.get("brain_damage", 0))
        if brain_damage > 0 and hack_state.get("active") and isinstance(game_state, dict):
            hacker_name = hack_state.get("hacker_name", "")
            er = game_state.get("edgerunners", {}).get(hacker_name, {})
            max_hp = int(er.get("hp", {}).get("max", 0))
            current_hp = int(er.get("hp", {}).get("current", 999))
            if max_hp > 0 and current_hp - brain_damage <= 0:
                hack_state["active"] = False
                hack_state["narrative_summary"] = (
                    hack_state.get("narrative_summary")
                    or f"{hacker_name} flatlined from brain damage — forced disconnect."
                )
                hack_state["_forced_disconnect"] = True
    except (TypeError, ValueError, OverflowError):
        pass

    if hack_state.get("_forced_disconnect"):
        hack_state["active"] = False

    return hack_state


def build_hack_injection(hack_state, pipeline_state=None):
    """Build state injection string for CPRED hack exchange user messages."""
    import json as _json

    preamble_lines = []

    # Transition context — the triggering model's summary of who/what/why
    _hack_context = hack_state.get("context")
    if _hack_context:
        preamble_lines.append(f"[TRANSITION] {_hack_context} [/TRANSITION]")
        preamble_lines.append("")

    try:
        alert_level = int(hack_state.get("alert_level", 0))
    except (TypeError, ValueError, OverflowError):
        alert_level = 0
    alert_name = _get_alert_name(alert_level)
    cycles_max = hack_state.get("cycles_max", 3)
    interface_rank = hack_state.get("interface_rank", 4)
    net_actions = hack_state.get("net_actions_per_turn", 3)

    net_actions_remaining = hack_state.get("net_actions_remaining", net_actions)
    meatspace_due = hack_state.get("meatspace_due", False)
    actions_line = f"NET Actions Remaining: {net_actions_remaining}/{net_actions}"
    try:
        if int(net_actions_remaining) <= 0:
            actions_line += " [NO NET ACTIONS LEFT — end NET turn]"
    except (TypeError, ValueError, OverflowError):
        pass
    if meatspace_due:
        actions_line += " [MEATSPACE ROUND DUE — narrate crew's round first]"

    # Build alert line with active effects reminder
    alert_line = f"Alert Level: {alert_level} ({alert_name})"
    alert_effects = []
    if alert_level >= 3:
        alert_effects.append("DVs +2")
    if alert_level >= 5:
        lockdown_dv = 6 + alert_level
        alert_effects.append(f"LOCKDOWN — move between nodes requires Interface check DV {lockdown_dv}")
    if alert_level >= 7:
        alert_effects.append("CONVERGENCE — Black ICE + security")
    if alert_effects:
        alert_line += f" [{', '.join(alert_effects)}]"

    lines = [
        "[HACK STATE]",
        f"Target: {hack_state.get('target_system', 'Unknown')} (SR {hack_state.get('sr', 3)})",
        f"Tier: {str(hack_state.get('tier') or 'full_run').replace('_', ' ').title()}",
        f"Interface Rank: {interface_rank} ({net_actions} NET Actions/turn)",
        actions_line,
        alert_line,
        f"Cycles: {hack_state.get('cycles_remaining', 0)}/{cycles_max}",
    ]

    # Active programs
    programs = hack_state.get("active_programs", [])
    if isinstance(programs, list) and programs:
        prog_strs = []
        for p in programs:
            if isinstance(p, dict):
                status_note = f", {p['status']}" if p.get("status") and p["status"] != "active" else ""
                prog_strs.append(f"{p.get('name', '?')} ({p.get('category', '?')}, REZ {p.get('rez', 0)}{status_note})")
            else:
                prog_strs.append(str(p))
        lines.append(f"Active Programs: {', '.join(prog_strs)}")
    else:
        lines.append("Active Programs: None")

    # Installed hardware (Hardware Option Slots — separate from programs)
    hardware = hack_state.get("installed_hardware", [])
    if isinstance(hardware, list) and hardware:
        lines.append(f"Installed Hardware: {', '.join(str(h) for h in hardware)}")

    lines.append(f"Current Node: {hack_state.get('current_node', 'Gateway')}")
    nodes_visited = hack_state.get("nodes_visited", ["Gateway"])
    if isinstance(nodes_visited, list):
        lines.append(f"Nodes Visited: {', '.join(str(n) for n in nodes_visited)}")
    else:
        lines.append(f"Nodes Visited: {nodes_visited}")

    # ICE status
    ice = hack_state.get("ice_status", {})
    if isinstance(ice, dict) and ice:
        lines.append("ICE Status:")
        for node, ice_data in ice.items():
            if isinstance(ice_data, dict):
                name = ice_data.get("name", "Unknown")
                behavior = ice_data.get("behavior", "?")
                rez_cur = ice_data.get("rez_current", 0)
                rez_max = ice_data.get("rez_max", 0)
                status = ice_data.get("status", "active")
                _ice_type_tag = f" [{ice_data.get('ice_type', '')}]" if ice_data.get("ice_type") else ""
                lines.append(f"  {node}: {name}{_ice_type_tag} ({behavior}) — REZ {rez_cur}/{rez_max}, {status}")
            else:
                lines.append(f"  {node}: {ice_data}")

    # Trace progress
    trace = hack_state.get("trace_progress")
    if trace is not None:
        try:
            trace = int(trace)
            sr = int(hack_state.get("sr", 3))
            trace_max = max(1, 6 - sr)
            trace_line = f"Trace Progress: {trace}/{trace_max}"
            if trace >= trace_max:
                trace_line += " [TRACE COMPLETE — location burned, Convergence NOW]"
            elif trace >= trace_max - 1:
                trace_line += " [CRITICAL — completes next turn!]"
            lines.append(trace_line)
        except (TypeError, ValueError, OverflowError):
            lines.append(f"Trace Progress: {trace}")

    # Tar stacks
    try:
        tar = int(hack_state.get("tar_stacks", 0))
    except (TypeError, ValueError, OverflowError):
        tar = 0
    if tar:
        lines.append(f"Tar Stacks: {tar} (-{tar * 2} auto-applied to next NET check, or spend 1 Cycle to clear)")

    # Brain damage
    brain_dmg = hack_state.get("brain_damage", 0)
    if brain_dmg:
        lines.append(f"Brain Damage This Hack: {brain_dmg}")

    # Active ICE effects
    _effects = []
    if hack_state.get("on_fire"):
        _effects.append("ON FIRE — clothes burning, 2 meat HP/turn (full meat action to extinguish)")
    if hack_state.get("movement_locked_by"):
        _effects.append(f"MOVEMENT LOCKED by {hack_state['movement_locked_by']} — cannot move between nodes until derezzed")
    try:
        if hack_state.get("slide_penalty", 0) != 0:
            _effects.append(f"SLIDE PENALTY: {hack_state['slide_penalty']} to all Slide checks (from Skunk)")
    except (TypeError, ValueError):
        pass
    try:
        if hack_state.get("net_action_penalty", 0) > 0:
            _effects.append(f"NET ACTION PENALTY: -{hack_state['net_action_penalty']} next turn (from Wisp)")
    except (TypeError, ValueError):
        pass
    for _db in hack_state.get("active_debuffs", []):
        if isinstance(_db, dict):
            _stats = _db.get("stats", [])
            _stats_str = "/".join(str(s) for s in _stats) if isinstance(_stats, list) else str(_stats)
            _effects.append(f"DEBUFF: {_stats_str} -{_db.get('amount', 0)} ({_db.get('source', '?')}, {_db.get('duration', '?')})")
    _destroyed = hack_state.get("destroyed_programs", [])
    if isinstance(_destroyed, list) and _destroyed:
        _effects.append(f"DESTROYED PROGRAMS: {', '.join(str(d) for d in _destroyed)} (permanently lost)")
    if _effects:
        lines.append("Active Effects:")
        for _eff in _effects:
            lines.append(f"  {_eff}")

    lines.append("[/HACK STATE]")

    parts = []
    if preamble_lines:
        parts.append("\n".join(preamble_lines))
    parts.append("\n".join(lines))

    # System map — both Quick Hacks (3 linear nodes) and Full Runs (4-6 node network) use system_map
    system_map = hack_state.get("system_map")
    if system_map:
        parts.append(f"[SYSTEM MAP]\n{_json.dumps(system_map, indent=2)}\n[/SYSTEM MAP]")
    else:
        parts.append("[SYSTEM MAP MISSING — you MUST include system_map in your report_hack_state call this exchange]")

    # Revealed nodes for fog-of-war tracking
    revealed = hack_state.get("revealed_nodes", [])
    if isinstance(revealed, list) and revealed:
        parts.append(f"Revealed nodes: {', '.join(str(n) for n in revealed)}")

    return "\n\n".join(parts)


def _resolve_netrunner_name(character_states, preferred_name=None):
    """Resolve which PC should be treated as the active netrunner."""
    if preferred_name and preferred_name in (character_states or {}):
        entry = character_states[preferred_name]
        data = entry.get("data", entry)
        if data.get("type") == "pc":
            return preferred_name

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
        if "cyberdeck" in str(data.get("summary", "")).lower():
            score += 1
        if score > best_score:
            best_score = score
            best_name = name
    return best_name or first_pc


def build_netrunner_profile(character_states, game_state=None, **_kw):
    """Build compact netrunner profile from character_states + edgerunner state for hack mode context."""
    hack_state = _kw.get("hack_state") or {}
    pc_name = _resolve_netrunner_name(character_states, preferred_name=hack_state.get("hacker_name"))
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
            sw_flag = " [SERIOUSLY WOUNDED]" if hp.get("seriously_wounded") else ""
            lines.append(f"HP: {hp.get('current', 0)}/{hp.get('max', 40)}{sw_flag}")
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
            dv_total = sum(ci.get("dv_mod", 0) for ci in injuries)
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

    # Summary (may contain cyberdeck info, installed programs, etc.)
    summary = pc_data.get("summary", "")
    if summary:
        lines.append(f"Equipment: {summary}")

    lines.append("[/NETRUNNER PROFILE]")
    return "\n".join(lines)


def apply_hack_writeback(hack_state, pipeline_state):
    """Write back hack results to persistent state after hack completes."""
    hacker_name = hack_state.get("hacker_name")
    brain_damage = hack_state.get("brain_damage", 0)
    if brain_damage:
        # Update edgerunner HP (brain damage reduces HP, ignores armor)
        game_state = pipeline_state.get("game_state", {})
        er_items = []
        if hacker_name and hacker_name in game_state.get("edgerunners", {}):
            er_items.append((hacker_name, game_state["edgerunners"][hacker_name]))
        er_items.extend(game_state.get("edgerunners", {}).items())
        seen = set()
        for name, er in er_items:
            if name in seen:
                continue
            seen.add(name)
            if er.get("hp"):
                er["hp"]["current"] = max(0, er["hp"]["current"] - brain_damage)
                er["hp"]["seriously_wounded"] = er["hp"]["current"] < (er["hp"].get("max", 40) + 1) // 2
                break
        # Update character_states vitals
        cs_items = []
        if hacker_name and hacker_name in pipeline_state.get("character_states", {}):
            cs_items.append((hacker_name, pipeline_state["character_states"][hacker_name]))
        cs_items.extend(pipeline_state.get("character_states", {}).items())
        seen = set()
        for name, entry in cs_items:
            if name in seen:
                continue
            seen.add(name)
            d = entry.get("data", entry)
            if d.get("type") == "pc":
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        v["current"] = max(0, v["current"] - brain_damage)
                        break
                break
    cycles_remaining = hack_state.get("cycles_remaining")
    if cycles_remaining is not None:
        cs_items = []
        if hacker_name and hacker_name in pipeline_state.get("character_states", {}):
            cs_items.append((hacker_name, pipeline_state["character_states"][hacker_name]))
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
                        r["current"] = cycles_remaining
                        break
                break

    # Write back destroyed programs to edgerunner persistent programs.
    # Backup Drive preserves destroyed programs after the run.
    game_state = pipeline_state.get("game_state", {})
    destroyed = hack_state.get("destroyed_programs", [])
    _hw = hack_state.get("installed_hardware", [])
    has_backup_drive = isinstance(_hw, list) and any(
        "backup drive" in str(h).lower() for h in _hw
    )
    if (isinstance(destroyed, list) and destroyed and hacker_name
            and isinstance(game_state, dict) and not has_backup_drive):
        er = game_state.get("edgerunners", {}).get(hacker_name, {})
        er_progs = er.get("programs", [])
        if isinstance(er_progs, list):
            er["programs"] = [p for p in er_progs
                              if isinstance(p, dict) and p.get("name") not in destroyed]

    # Fire → nudity at hack end (if never extinguished)
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


# ============================================================
# NET-in-Meatspace Combined Combat Mode
# ============================================================

NET_COMBAT_CONTRACT = """You are the COMBINED COMBAT + NET MASTER for a Cyberpunk RED session. A Netrunner is jacked into a system AND meatspace combat is active (or about to begin).

YOUR ROLE: Adjudicate both meatspace combat and NET actions simultaneously, each exchange covering one combatant's turn. Call report_net_combat_state every exchange, then write your narrative.

### Dual-Theater Structure
Each exchange narrates the current combatant's turn:
- **Non-Netrunner turns**: Meatspace only. NET section can be brief or omitted.
- **Netrunner's turn**: Their combat Action is spent on NET Actions (N actions per Interface Rank: 2/3/4/5 for ranks 1-3/4-6/7-9/10). They still get a Move Action in meatspace (requires Virtuality Goggles — without them, the Netrunner is Unconscious and cannot move or dodge). Narrate meatspace first (movement, reactions), then NET actions after a `---` separator.

### Narrative Format
```
[Meatspace narration — combat action, movement, reactions]
[Roll breakdowns for meatspace]

---

[NET narration — what the Netrunner experiences in the architecture]
[Roll breakdowns for NET actions]
```
On non-Netrunner turns where nothing happens in the NET, omit the separator and NET section entirely.

### Hack-Originated Transition
If initiated_from is "hack", the NET encounter was already in progress when combat broke out. The injection shows current NET state (mid-hack) but no meatspace initiative. Your FIRST exchange must:
1. Bootstrap enemies with set_combat_stats in character_updates
2. Roll initiative for all combatants
3. Begin Round 1
4. Report existing NET state unchanged (no NET actions this exchange — combat setup only)

### Meatspace Rules (Quick Reference — see Combat Ruleset for tables/edge cases)
- Initiative: REF + d10. Highest first. Ties reroll.
- Action Economy: Move Action (MOVE×2 m/yds) + Action per turn.
- Ranged Attack: d10 + REF + Weapon Skill vs DV (§3 range/DV table).
- Melee Attack: d10 + DEX + Melee Weapon/Martial Arts vs defender's d10 + DEX + Evasion.
- Crit Success: natural 10 → roll another d10 and ADD. Does NOT chain.
- Crit Failure: natural 1 → roll another d10 and SUBTRACT. Does NOT chain.
- Luck: spend before the roll (1:1). NOT on damage, Death Saves, or Initiative.
- Seriously Wounded: HP < half max → −2 to ALL actions.
- Mortally Wounded: 0 HP → −4 to ALL actions, −6 MOVE (min 1). Death Save each round.
- Opposed check ties go to the Defender.

### Damage Resolution
1. Roll weapon damage dice.
2. Crit check: 2+ dice show 6 → crit injury. +5 bonus direct to HP (ignores SP). Roll on table (§15).
3. Subtract location SP. If damage ≤ SP, no penetration (crit bonus still applies).
4. Ablation: penetrating hit → SP −1. AP ammo: SP −2.
5. Melee: halve SP (round up). Brawling: full SP.
6. Remaining after SP → HP.

### NET Rules (Quick Reference — see Hacking Rulebook for full rules)
- Flat check: Interface + d10 vs DV. Must BEAT DV (equal fails).
- Opposed check (Zap, no Program): Interface + d10 vs ICE stat + d10. Deals 1d6 REZ damage.
- Program attack: Interface + Program ATK + d10 vs ICE DEF + d10. Damage per program listing.
- Attack Programs Deactivate after use (1 use, then must Deactivate + Reactivate = 2 NET Actions).
- Slide (flee): Interface + d10 vs ICE PER + d10. Escape to adjacent node; ICE stays where it was. Once per turn. Cannot Slide preemptively. Only way to escape hunting Black ICE without Derezzing it.
- Black ICE hunts: triggered Black ICE pursues the Netrunner across nodes. Simply moving away does NOT escape — the ICE follows on its next action. Update ice_status to reflect its new node.
- Crits/Fumbles: same as meatspace (d10 explodes on 10, subtracts on 1).
- Luck: spend before the roll on Interface checks (1:1).
- Opposed check ties go to the Defender (ICE).
- NET Actions per turn = Netrunner's allocation by Interface Rank.
- Boosted actions cost 1 NET Action + 1 Cycle. Track cycles_remaining.
- Alert Level: escalates per Hacking Rulebook §6. Cannot decrease mid-run.

### Cross-Theater Interactions
- **Netrunner's body is in meatspace**: can be shot, hit, caught in AoE. Track via character_updates. With Virtuality Goggles the Netrunner can still see and move in meatspace; without them the Netrunner is **Unconscious** in meatspace (no Move Action, no dodge).
- **Brain damage**: When Black ICE attacks the Netrunner, call resolve_mechanics with action type `program_attack_vs_netrunner`: {type, character (ICE name), ice_type (e.g. "Hellhound"), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)}. Backend auto-reads ATK/damage from ICE table. Brain damage and special effects are resolved by the backend — do NOT set brain_damage in hack_state or character_updates.hp_delta. For anti-program ICE, use `ice_attack_vs_program`: {type, character, ice_type, target_program, target_program_def, target_program_rez}.
- **Program deactivation**: When resolve_mechanics returns `program_deactivated`, the program is deactivated (RAW). Reactivating costs 1 NET Action (no dice — update status to 'active' in active_programs).
- **Zap damage**: For Zap attacks, use opposed_check with `"zap": true` and `"interface_rank": N`. Backend rolls Interface rank d6 REZ damage on hit and auto-applies to ice_status.
- **TAR penalty**: TAR penalty (-2 per stack) is applied automatically by the backend to the Netrunner's next NET check. Mark the Netrunner's NET actions with `"net": true` (do NOT mark ICE actions).
- **Alert DV penalty**: +2 to all NET skill check DVs at alert 3+ is auto-applied by the backend. Do NOT add +2 manually.
- **Forced disconnect**: Backend auto-sets net_complete=true on explicit forced jack-out/flatline conditions.
- **NET affecting meatspace**: Unlocking doors, disabling cameras, controlling turrets — narrate in both sections. The physical effect happens on the Netrunner's initiative.
- **Seriously Wounded**: applies to Interface checks too (−2 all actions includes NET).
- **Mortally Wounded (0 HP)**: Do NOT auto-end NET at 0 HP. Netrunner can still act (with the normal 0 HP penalties), including attempting safe Jack Out.
- **Flatlined**: immediate forced disconnect. Set net_complete=true.

### State Tracking
- **character_updates**: meatspace changes (hp_delta, armor_delta, luck_delta, ammo, critical injuries, conditions). Same as standalone combat.
- **hack_state**: NET state (alert_level, cycles_remaining, active_programs, current_node, nodes_visited, revealed_nodes, ice_status, trace_progress, tar_stacks, system_map). revealed_nodes is a superset of nodes_visited — add nodes discovered via Pathfinder, Eye-Dee, or any other means. Never remove entries. brain_damage is backend-managed — do not set it.
- **cover_state**: meatspace cover for ALL combatants.
- **combat**: initiative tracker (round, initiative_order, current_turn).

### Completion
- `combat_complete` and `net_complete` are independent booleans.
- When one theater resolves, continue the other. Injection shows "resolved" for the done theater.
- When BOTH are true: set narrative_summary (1-3 sentences covering the whole engagement).
- Mode ends when both theaters complete.

### Enemy/NPC Bootstrap
Same as standalone combat: check project files for named enemy stat blocks before generating from tier tables. Use set_combat_stats on first exchange for new enemies. Combat number system for threat tiers.

### Mechanics Resolution (resolve_mechanics tool — INCREMENTAL)
Call `resolve_mechanics` ONCE PER COMBATANT TURN for meatspace actions, and once per NET action. Narrate AFTER receiving dice results, never before.

For NET Interface checks, use skill_check: {type: "skill_check", character: "<netrunner>", stat_value: <Interface rank>, skill_value: 0, dv: <target DV>}

Turn flow:
1. For each combatant's turn (in initiative order):
   a. Call resolve_mechanics with that combatant's actions
   b. Read results, narrate 1-3 sentences
   c. Skip eliminated combatants
2. For the Netrunner's NET actions, call resolve_mechanics for each NET action
3. Call `report_net_combat_state` with judgment fields only (no hp_delta, armor_delta, etc.)
   The backend tracks all dice-dependent state from resolve_mechanics results.

### Roll Format
Meatspace: 🎲 [V attacks Guard]: d10[**7**] + REF 8 + Handgun 6 = 21 vs DV 15 ✓
Meatspace damage: 🎲 [Heavy Pistol]: 3d6[**4,3,5**] = 12 → Body SP 11 → 1 net, SP→10
NET flat: 🎲 [Backdoor]: d10[**8**] +Interface 7 = 15 vs DV 12 ✓
NET opposed: 🎲 [Zap vs Patrol]: d10[**6**] +Interface 7 = 13 vs d10[**4**] +DEF 6 = 10 ✓

### Guidelines
- Transparent dice. Show numbers, modifiers, math.
- No fudging. Fail-forward when failure would break the campaign.
- PC death only at Death Risk points.

### Narrative Style
Present tense, visceral, Night City grit. 2-5 sentences per theater. Chrome reflects neon. The NET is hostile, alien, beautiful."""

REPORT_NET_COMBAT_STATE_TOOL = {
    "name": "report_net_combat_state",
    "description": "Report combined meatspace + NET combat state after each exchange.",
    "input_schema": {
        "type": "object",
        "required": ["narrative", "rolls", "character_updates", "cover_state", "combat",
                      "hack_state", "available_actions", "combat_complete", "net_complete"],
        "properties": {
            "narrative": {
                "type": "string",
                "description": "Full narrative of this exchange — meatspace and NET actions."
            },
            "rolls": {
                "type": "array",
                "description": "All dice rolls this exchange.",
                "items": {
                    "type": "object",
                    "required": ["description", "result"],
                    "properties": {
                        "description": {"type": "string"},
                        "result": {"type": "string"},
                        "theater": {"type": "string", "enum": ["meatspace", "net"]}
                    }
                }
            },
            "character_updates": {
                "type": "array",
                "description": "Judgment-only state changes for affected combatants (meatspace). Dice-dependent fields (hp_delta, armor_delta, critical_injury_add, luck_delta, ammo) are tracked automatically by the backend from resolve_mechanics results — do NOT include them here. Brain damage is also backend-tracked.",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "set_combat_stats": {
                            "type": "object",
                            "description": "First-exchange enemy bootstrap ONLY.",
                            "properties": {
                                "hp_max": {"type": "integer"},
                                "armor": {"type": "object", "properties": {"head": {"type": "integer"}, "body": {"type": "integer"}}},
                                "weapons": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "damage": {"type": "string"}, "ammo": {"type": "integer"}, "magazine": {"type": "integer"}, "skill": {"type": "string"}}}},
                                "stats": {"type": "object"}
                            }
                        },
                        "critical_injury_remove": {"type": "array", "items": {"type": "string"}},
                        "conditions_add": {"type": "array", "items": {"type": "string"}},
                        "conditions_remove": {"type": "array", "items": {"type": "string"}}
                    }
                }
            },
            "cover_state": {
                "type": "array",
                "description": "Cover status for ALL combatants. Report every exchange.",
                "items": {
                    "type": "object",
                    "required": ["name", "in_cover"],
                    "properties": {
                        "name": {"type": "string"},
                        "in_cover": {"type": "boolean"},
                        "cover_type": {"type": ["string", "null"]},
                        "cover_hp": {"type": ["integer", "null"]}
                    }
                }
            },
            "combat": {
                "description": "Updated initiative state. null when meatspace combat ends.",
                "oneOf": [
                    {"type": "object", "required": ["round", "initiative_order", "current_turn"], "properties": {"round": {"type": "integer"}, "initiative_order": {"type": "array", "items": {"type": "string"}}, "current_turn": {"type": "string"}}},
                    {"type": "null"}
                ]
            },
            "hack_state": {
                "type": "object",
                "description": "Current NET encounter state.",
                "properties": {
                    "alert_level": {"type": "integer", "minimum": 0},
                    "cycles_remaining": {"type": "integer", "minimum": 0},
                    "active_programs": {"type": "array", "description": "Programs only — NOT Hardware.", "items": {"type": "object", "properties": {"name": {"type": "string"}, "category": {"type": "string", "enum": ["booster", "defender", "attacker", "black_ice"]}, "rez": {"type": "integer"}, "status": {"type": "string", "enum": ["active", "deactivated", "derezzed", "destroyed"]}}}},
                    "installed_hardware": {"type": "array", "description": "Cyberdeck Hardware (Hardware Option Slots, NOT program slots).", "items": {"type": "string"}},
                    "current_node": {"type": "string"},
                    "nodes_visited": {"type": "array", "items": {"type": "string"}},
                    "ice_status": {"type": "object", "description": "Key = node ICE is currently in. Move Black ICE to new node key when it hunts.", "additionalProperties": {"type": "object", "properties": {"name": {"type": "string"}, "behavior": {"type": "string", "enum": ["patrol", "tar", "black", "trace"]}, "rez_current": {"type": "integer"}, "rez_max": {"type": "integer"}, "status": {"type": "string", "enum": ["active", "bypassed", "disabled", "derezzed"]}}}},
                    "trace_progress": {"type": ["integer", "null"]},
                    "tar_stacks": {"type": "integer", "minimum": 0},
                    "system_map": {"type": ["object", "null"]},
                    "revealed_nodes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Node names the Netrunner has discovered (via entering, Pathfinder, etc.). Add newly discovered nodes — never remove."
                    }
                }
            },
            "combat_complete": {
                "type": "boolean",
                "description": "True when meatspace combat is over."
            },
            "net_complete": {
                "type": "boolean",
                "description": "True when NET encounter is over (objective achieved, jacked out, or forced disconnect)."
            },
            "narrative_summary": {
                "type": "string",
                "description": "ONLY when BOTH combat_complete AND net_complete are true. 1-3 sentence summary of the entire engagement."
            },
            "available_actions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Available NET actions for next exchange (optional)."
            }
        }
    }
}


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
    net_actions = 2 if interface_rank <= 3 else 3 if interface_rank <= 6 else 4 if interface_rank <= 9 else 5
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

    try:
        interface_rank = int(hack_state.get("interface_rank", 4))
    except (TypeError, ValueError, OverflowError):
        interface_rank = 4
    try:
        cycles_remaining = int(hack_state.get("cycles_remaining", 3))
    except (TypeError, ValueError, OverflowError):
        cycles_remaining = 3
    try:
        cycles_max = int(hack_state.get("cycles_max", 3))
    except (TypeError, ValueError, OverflowError):
        cycles_max = 3
    try:
        alert_level = int(hack_state.get("alert_level", 0))
    except (TypeError, ValueError, OverflowError):
        alert_level = 0
    try:
        tar_stacks = int(hack_state.get("tar_stacks", 0))
    except (TypeError, ValueError, OverflowError):
        tar_stacks = 0
    try:
        brain_damage = int(hack_state.get("brain_damage", 0))
    except (TypeError, ValueError, OverflowError):
        brain_damage = 0
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

    net_actions = 2 if interface_rank <= 3 else 3 if interface_rank <= 6 else 4 if interface_rank <= 9 else 5
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


def _apply_character_updates_shared(pipeline_state, character_updates, game_state=None):
    """Shared logic for applying character_updates from combat/net_combat tool output.

    Handles hp_delta, armor_delta, luck_delta, ammo, critical_injury_add/remove,
    conditions_add/remove, set_combat_stats — routing PCs to edgerunner state
    and enemies to character_states.
    """
    if not isinstance(character_updates, list):
        logger.warning("_apply_character_updates_shared: character_updates must be a list, got %s",
                       type(character_updates).__name__)
        return

    edgerunners = game_state.get("edgerunners", {}) if game_state else {}
    cs = pipeline_state.get("character_states", {})

    for upd in character_updates:
        if not isinstance(upd, dict):
            continue
        name = upd.get("name")
        if not isinstance(name, str) or not name:
            continue

        is_pc = name in edgerunners

        # --- set_combat_stats (enemy bootstrap) ---
        scs = upd.get("set_combat_stats")
        if scs and isinstance(scs, dict) and not is_pc:
            if name not in cs:
                cs[name] = {"data": {"type": "enemy", "class": "", "level": None, "vitals": [], "conditions": []}}
            entry = cs[name]
            d = entry.get("data", entry)
            if not d.get("combat_data"):
                scs = copy.deepcopy(scs)
                try:
                    hp_max = max(0, int(scs.get("hp_max", 0)))
                except (TypeError, ValueError, OverflowError):
                    hp_max = 0
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
            try:
                hp_delta = int(hp_delta)
            except (TypeError, ValueError, OverflowError):
                hp_delta = None
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
        if armor_delta and isinstance(armor_delta, dict):
            if is_pc:
                er = edgerunners[name]
                for loc in ("head", "body"):
                    try:
                        delta = int(armor_delta.get(loc, 0))
                    except (TypeError, ValueError, OverflowError):
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
                        try:
                            delta = int(armor_delta.get(loc, 0))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if delta:
                            cd_armor[loc] = max(0, cd_armor.get(loc, 0) + delta)

        # --- luck_delta ---
        luck_delta = upd.get("luck_delta")
        if luck_delta is not None and is_pc:
            try:
                luck_delta = int(luck_delta)
            except (TypeError, ValueError, OverflowError):
                luck_delta = None
        if luck_delta is not None and is_pc:
            er = edgerunners[name]
            er["luck"]["current"] = max(0, min(er["luck"]["max"], er["luck"]["current"] + luck_delta))

        # --- ammo ---
        ammo_updates = upd.get("ammo")
        if ammo_updates and isinstance(ammo_updates, list):
            if is_pc:
                er = edgerunners[name]
                for au in ammo_updates:
                    if not isinstance(au, dict):
                        continue
                    wname = au.get("weapon", "")
                    try:
                        cur = int(au.get("current", 0))
                    except (TypeError, ValueError, OverflowError):
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
                            continue
                        wname = au.get("weapon", "")
                        try:
                            cur = int(au.get("current", 0))
                        except (TypeError, ValueError, OverflowError):
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
        for ci in (upd.get("critical_injury_add") or []):
            if not isinstance(ci, dict):
                continue
            try:
                dv_mod = int(ci.get("dv_mod", 0))
            except (TypeError, ValueError, OverflowError):
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
        for ci_name in (upd.get("critical_injury_remove") or []):
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
        for cond in (upd.get("conditions_add") or []):
            if cond not in conditions:
                conditions.append(cond)
        for cond in (upd.get("conditions_remove") or []):
            if cond in conditions:
                conditions.remove(cond)


def apply_net_combat_state(pipeline_state, tool_input, game_state=None, resolver_state_ops=None, **_kw):
    """Apply combined net_combat state updates from report_net_combat_state tool output."""
    if not isinstance(tool_input, dict):
        logger.warning("apply_net_combat_state: tool_input must be an object, got %s",
                       type(tool_input).__name__)
        return

    # --- Meatspace: character_updates, cover, combat initiative ---
    _apply_character_updates_shared(
        pipeline_state,
        tool_input.get("character_updates", []),
        game_state=game_state
    )

    # Cover state
    cover_updates = tool_input.get("cover_state")
    if cover_updates and isinstance(cover_updates, list):
        old_combat = pipeline_state.get("combat")
        if isinstance(old_combat, dict):
            cover_dict = old_combat.setdefault("cover", {})
            for cov in cover_updates:
                if isinstance(cov, dict):
                    cov_name = cov.get("name")
                    if isinstance(cov_name, str) and cov_name:
                        cover_dict[cov_name] = {
                            "in_cover": cov.get("in_cover", False),
                            "cover_type": cov.get("cover_type"),
                            "cover_hp": cov.get("cover_hp")
                        }

    # Combat initiative
    _has_combat_field = "combat" in tool_input
    new_combat = tool_input.get("combat")
    combat_complete = tool_input.get("combat_complete", False)
    if combat_complete or (_has_combat_field and new_combat is None):
        # Meatspace combat done — clear initiative but keep net_combat active
        pipeline_state["combat"] = None
    elif isinstance(new_combat, dict):
        old_start = (pipeline_state.get("combat") or {}).get("start_message_id")
        old_cover = (pipeline_state.get("combat") or {}).get("cover", {})
        old_context = (pipeline_state.get("combat") or {}).get("context")
        pipeline_state["combat"] = new_combat
        if old_start and "start_message_id" not in new_combat:
            pipeline_state["combat"]["start_message_id"] = old_start
        if old_cover and "cover" not in new_combat:
            pipeline_state["combat"]["cover"] = old_cover
        if old_context and "context" not in new_combat:
            pipeline_state["combat"]["context"] = old_context

    # --- NET: update hack fields in net_combat ---
    nc = pipeline_state.get("net_combat", {})
    if not isinstance(nc, dict):
        nc = {}
    hs = tool_input.get("hack_state", {})
    if isinstance(hs, dict):
        # brain_damage excluded — resolver-authoritative (applied via resolver_state_ops)
        for field in ["alert_level", "cycles_remaining", "active_programs",
                      "current_node", "nodes_visited", "ice_status",
                      "trace_progress", "tar_stacks",
                      "revealed_nodes"]:
            if field in hs:
                nc[field] = hs[field]
        # System map (first exchange only)
        if hs.get("system_map") and not nc.get("system_map"):
            nc["system_map"] = hs["system_map"]

    # Validate revealed_nodes type — model may send garbage
    if not isinstance(nc.get("revealed_nodes"), list):
        visited_fallback = nc.get("nodes_visited", [])
        nc["revealed_nodes"] = list(visited_fallback) if isinstance(visited_fallback, list) else ["Gateway"]

    # Auto-merge: visiting a node always reveals it
    visited = nc.get("nodes_visited", [])
    revealed = nc.get("revealed_nodes", [])
    if isinstance(visited, list) and isinstance(revealed, list):
        for n in visited:
            if n not in revealed:
                revealed.append(n)
        nc["revealed_nodes"] = revealed

    # Available actions
    if tool_input.get("available_actions") and isinstance(tool_input["available_actions"], list):
        nc["available_actions"] = tool_input["available_actions"]

    # Inject resolver brain_damage before delta calculation
    if resolver_state_ops:
        bd_total = 0
        for op in resolver_state_ops:
            if not isinstance(op, dict) or op.get("op") != "brain_damage":
                continue
            try:
                bd_total += abs(int(op.get("change", 0)))
            except (TypeError, ValueError, OverflowError):
                continue
        if bd_total > 0:
            prev_bd = int(nc.get("brain_damage", 0))
            nc["brain_damage"] = prev_bd + bd_total

    # --- Brain damage delta → apply to Netrunner's HP ---
    try:
        current_bd = int(nc.get("brain_damage", 0))
    except (TypeError, ValueError, OverflowError):
        current_bd = 0
    try:
        prev_bd = int(nc.get("_prev_brain_damage", 0))
    except (TypeError, ValueError, OverflowError):
        prev_bd = 0
    bd_delta = current_bd - prev_bd
    if bd_delta > 0:
        nc["_prev_brain_damage"] = current_bd
        netrunner_name = nc.get("netrunner", "")
        edgerunners = game_state.get("edgerunners", {}) if game_state else {}
        if netrunner_name and netrunner_name in edgerunners:
            er = edgerunners[netrunner_name]
            er["hp"]["current"] = max(0, er["hp"]["current"] - bd_delta)
            _update_seriously_wounded(er)
            # Mirror to character_states
            cs = pipeline_state.get("character_states", {})
            entry = cs.get(netrunner_name, {})
            d = entry.get("data", entry)
            for v in d.get("vitals", []):
                if v.get("label") == "HP" and "current" in v:
                    v["current"] = er["hp"]["current"]
                    break

    # Process program_deactivate and rez_damage ops from resolver
    if resolver_state_ops:
        for op in resolver_state_ops:
            if not isinstance(op, dict):
                continue
            if op.get("op") == "program_deactivate":
                prog_name = op.get("program_name", "")
                programs = nc.get("active_programs", [])
                if isinstance(programs, list):
                    for p in programs:
                        if isinstance(p, dict) and p.get("name") == prog_name:
                            p["status"] = "deactivated"
                            break
            elif op.get("op") == "rez_damage":
                try:
                    _apply_rez_damage_to_ice_status(nc, op)
                except (TypeError, ValueError, OverflowError):
                    pass

    # Apply ICE-effect state_ops (program_destroy, body_fire, movement_lock, etc.)
    # Filter to only ICE-effect ops — brain_damage/program_deactivate/rez_damage already handled above
    _ice_ops = [op for op in (resolver_state_ops or [])
                if isinstance(op, dict) and op.get("op") in _ICE_EFFECT_OPS]
    _apply_ice_effect_ops(nc, _ice_ops, game_state=game_state)

    # Determine if Netrunner took NET actions this exchange (used by multiple sections)
    _has_net_actions = False
    if isinstance(hs, dict):
        try:
            _has_net_actions = int(hs.get("net_actions_used", 0)) > 0
        except (TypeError, ValueError, OverflowError):
            _has_net_actions = False

    # tar_consumed is resolver-authoritative.
    if resolver_state_ops and any(
        isinstance(op, dict) and op.get("op") == "tar_consumed"
        for op in resolver_state_ops
    ):
        nc["tar_stacks"] = 0

    # Fire tick, fire extinguish, wisp penalty, movement lock auto-clear, slide penalty auto-clear
    _apply_persistent_ice_effects(nc, hs, game_state, "netrunner", _has_net_actions)

    # --- Trace auto-increment: tick once per completed Netrunner NET turn ---
    try:
        if _has_net_actions:
            ice_status = nc.get("ice_status", {})
            if isinstance(ice_status, dict):
                has_active_trace = any(
                    isinstance(v, dict) and v.get("status") == "active"
                    and isinstance(v.get("behavior", ""), str)
                    and "trace" in v.get("behavior", "").lower()
                    for v in ice_status.values()
                )
                if has_active_trace and nc.get("trace_progress") is not None:
                    nc["trace_progress"] = int(nc.get("trace_progress", 0)) + 1
                    sr = int(nc.get("sr", 3))
                    trace_max = max(1, 6 - sr)
                    if nc["trace_progress"] >= trace_max:
                        nc["alert_level"] = max(int(nc.get("alert_level", 0)), 7)
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Alert threshold ICE auto-spawn ---
    try:
        prev_alert = int(nc.get("_prev_alert_level", 0))
        new_alert = int(nc.get("alert_level", 0))
        ice_status = nc.get("ice_status", {})
        if not isinstance(ice_status, dict):
            ice_status = {}

        if new_alert >= 5 and prev_alert < 5:
            has_trace = any(
                isinstance(v, dict)
                and isinstance(v.get("behavior", ""), str)
                and "trace" in v.get("behavior", "").lower()
                and v.get("status") in ("active", "bypassed")
                for v in ice_status.values()
            )
            if not has_trace:
                sr = int(nc.get("sr", 3))
                ice_status["Gateway_Trace"] = {
                    "name": "Trace ICE", "behavior": "trace",
                    "rez_current": sr * 2, "rez_max": sr * 2, "status": "active"
                }
                nc["ice_status"] = ice_status
                nc["trace_progress"] = 0

        if new_alert >= 7 and prev_alert < 7:
            sr = int(nc.get("sr", 3))
            current_node = nc.get("current_node", "Gateway")
            spawn_key = str(current_node) + "_Convergence"
            ice_status[spawn_key] = {
                "name": "Kraken", "behavior": "black", "ice_type": "kraken",
                "rez_current": ICE_STAT_BLOCKS["kraken"]["rez"],
                "rez_max": ICE_STAT_BLOCKS["kraken"]["rez"], "status": "active"
            }
            nc["ice_status"] = ice_status

        nc["_prev_alert_level"] = new_alert
    except (TypeError, ValueError, OverflowError):
        pass

    # --- Completion flags ---
    nc["combat_complete"] = tool_input.get("combat_complete", nc.get("combat_complete", False))
    nc["net_complete"] = tool_input.get("net_complete", nc.get("net_complete", False))
    if nc.get("_forced_disconnect"):
        nc["net_complete"] = True

    # --- Forced disconnect on flatline only (after completion flags so it can't be overwritten) ---
    try:
        netrunner_name = nc.get("netrunner", "")
        if netrunner_name and not nc.get("net_complete") and isinstance(game_state, dict):
            er = game_state.get("edgerunners", {}).get(netrunner_name, {})
            current_hp = int(er.get("hp", {}).get("current", 999))
            # Mortally wounded at 0 HP does not auto-disconnect; flatline does.
            if current_hp < 0:
                nc["net_complete"] = True
                nc["_forced_disconnect"] = True
    except (TypeError, ValueError, OverflowError):
        pass

    if nc["combat_complete"] and nc["net_complete"]:
        nc["active"] = False
        nc["narrative_summary"] = tool_input.get("narrative_summary", "Combined engagement concluded.")

    pipeline_state["net_combat"] = nc


def build_net_combat_injection(combat, net_combat, pipeline_state):
    """Build injection string for combined net_combat exchange user messages."""
    import json as _json

    lines = []

    # Transition context — the triggering model's summary of who/what/why
    nc = net_combat or {}
    _nc_context = nc.get("context")
    if _nc_context:
        lines.append(f"[TRANSITION] {_nc_context} [/TRANSITION]")
        lines.append("")

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
                sw = " SW" if hp.get("seriously_wounded") else ""
                armor = er.get("armor", {})
                luck = er.get("luck", {})
                parts.append(f"HP {hp.get('current', 0)}/{hp.get('max', 40)}{sw}")
                parts.append(f"SP H:{armor.get('head', 0)}/B:{armor.get('body', 0)}")
                parts.append(f"Luck {luck.get('current', 0)}/{luck.get('max', 0)}")
            elif combat_data:
                cd_hp_max = combat_data.get("hp_max", 0)
                cd_hp_cur = 0
                for v in d.get("vitals", []):
                    if v.get("label") == "HP" and "current" in v:
                        cd_hp_cur = v["current"]
                        break
                sw = " SW" if cd_hp_cur < (cd_hp_max + 1) // 2 and cd_hp_max > 0 else ""
                cd_armor = combat_data.get("armor", {})
                parts.append(f"HP {cd_hp_cur}/{cd_hp_max}{sw}")
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
        alert_name = _get_alert_name(nc.get("alert_level", 0))
        try:
            _nc_alert_level = int(nc.get("alert_level", 0))
        except (TypeError, ValueError, OverflowError):
            _nc_alert_level = 0
        lines.append("[NET STATE]")
        lines.append(f"Netrunner: {nc.get('netrunner', '?')}")
        lines.append(f"Target: {nc.get('target', 'Unknown')}")
        lines.append(f"Interface Rank: {nc.get('interface_rank', 4)} ({nc.get('net_actions_per_turn', 3)} NET Actions/turn)")
        _nc_alert_line = f"Alert Level: {_nc_alert_level} ({alert_name})"
        _nc_alert_effects = []
        if _nc_alert_level >= 3:
            _nc_alert_effects.append("DVs +2")
        if _nc_alert_level >= 5:
            _nc_lockdown_dv = 6 + _nc_alert_level
            _nc_alert_effects.append(f"LOCKDOWN — move between nodes requires Interface check DV {_nc_lockdown_dv}")
        if _nc_alert_level >= 7:
            _nc_alert_effects.append("CONVERGENCE — Black ICE + security")
        if _nc_alert_effects:
            _nc_alert_line += f" [{', '.join(_nc_alert_effects)}]"
        lines.append(_nc_alert_line)
        lines.append(f"Cycles: {nc.get('cycles_remaining', 0)}/{nc.get('cycles_max', 3)}")
        lines.append(f"Current Node: {nc.get('current_node', 'Gateway')}")
        nodes_visited = nc.get("nodes_visited", ["Gateway"])
        if isinstance(nodes_visited, list):
            lines.append(f"Nodes Visited: {', '.join(str(n) for n in nodes_visited)}")
        else:
            lines.append(f"Nodes Visited: {nodes_visited}")

        programs = nc.get("active_programs", [])
        if isinstance(programs, list) and programs:
            prog_strs = []
            for p in programs:
                if isinstance(p, dict):
                    status_note = f", {p['status']}" if p.get("status") and p["status"] != "active" else ""
                    prog_strs.append(f"{p.get('name', '?')} ({p.get('category', '?')}, REZ {p.get('rez', 0)}{status_note})")
                else:
                    prog_strs.append(str(p))
            lines.append(f"Active Programs: {', '.join(prog_strs)}")

        hardware = nc.get("installed_hardware", [])
        if isinstance(hardware, list) and hardware:
            lines.append(f"Installed Hardware: {', '.join(str(h) for h in hardware)}")

        ice = nc.get("ice_status", {})
        if isinstance(ice, dict) and ice:
            lines.append("ICE Status:")
            for node, ice_data in ice.items():
                if isinstance(ice_data, dict):
                    _nc_ice_type_tag = f" [{ice_data.get('ice_type', '')}]" if ice_data.get("ice_type") else ""
                    lines.append(f"  {node}: {ice_data.get('name', '?')}{_nc_ice_type_tag} ({ice_data.get('behavior', '?')}) — "
                                 f"REZ {ice_data.get('rez_current', 0)}/{ice_data.get('rez_max', 0)}, {ice_data.get('status', 'active')}")

        trace = nc.get("trace_progress")
        if trace is not None:
            try:
                trace = int(trace)
                sr = int(nc.get("sr", 3))
                trace_max = max(1, 6 - sr)
                lines.append(f"Trace Progress: {trace}/{trace_max}")
            except (TypeError, ValueError, OverflowError):
                lines.append(f"Trace Progress: {trace}")

        try:
            tar = int(nc.get("tar_stacks", 0))
        except (TypeError, ValueError, OverflowError):
            tar = 0
        if tar:
            lines.append(f"Tar Stacks: {tar} (-{tar * 2} auto-applied to next NET check, or spend 1 Cycle to clear)")

        bd = nc.get("brain_damage", 0)
        if bd:
            lines.append(f"Brain Damage This Run: {bd}")

        # Active ICE effects
        _nc_effects = []
        if nc.get("on_fire"):
            _nc_effects.append("ON FIRE — clothes burning, 2 meat HP/turn (full meat action to extinguish)")
        if nc.get("movement_locked_by"):
            _nc_effects.append(f"MOVEMENT LOCKED by {nc['movement_locked_by']} — cannot move between nodes until derezzed")
        try:
            if nc.get("slide_penalty", 0) != 0:
                _nc_effects.append(f"SLIDE PENALTY: {nc['slide_penalty']} to all Slide checks (from Skunk)")
        except (TypeError, ValueError):
            pass
        try:
            if nc.get("net_action_penalty", 0) > 0:
                _nc_effects.append(f"NET ACTION PENALTY: -{nc['net_action_penalty']} next turn (from Wisp)")
        except (TypeError, ValueError):
            pass
        for _db in nc.get("active_debuffs", []):
            if isinstance(_db, dict):
                _stats = _db.get("stats", [])
                _stats_str = "/".join(str(s) for s in _stats) if isinstance(_stats, list) else str(_stats)
                _nc_effects.append(f"DEBUFF: {_stats_str} -{_db.get('amount', 0)} ({_db.get('source', '?')}, {_db.get('duration', '?')})")
        _nc_destroyed = nc.get("destroyed_programs", [])
        if isinstance(_nc_destroyed, list) and _nc_destroyed:
            _nc_effects.append(f"DESTROYED PROGRAMS: {', '.join(str(d) for d in _nc_destroyed)} (permanently lost)")
        if _nc_effects:
            lines.append("Active Effects:")
            for _eff in _nc_effects:
                lines.append(f"  {_eff}")

        lines.append("[/NET STATE]")

        # System map — both Quick Hacks (3 linear nodes) and Full Runs (4-6 node network) use system_map
        system_map = nc.get("system_map")
        if system_map:
            lines.append(f"\n[SYSTEM MAP]\n{_json.dumps(system_map, indent=2)}\n[/SYSTEM MAP]")
        else:
            lines.append("\n[SYSTEM MAP MISSING — you MUST include system_map in your report_net_combat_state call this exchange]")

        # Revealed nodes for fog-of-war tracking
        revealed = nc.get("revealed_nodes", [])
        if isinstance(revealed, list) and revealed:
            lines.append(f"\nRevealed nodes: {', '.join(str(n) for n in revealed)}")

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
    # Brain damage is already applied incrementally via _prev_brain_damage tracking,
    # so we only need to write back cycles_remaining.
    netrunner_name = net_combat_state.get("netrunner")
    cycles_remaining = net_combat_state.get("cycles_remaining")
    if cycles_remaining is not None:
        cs_items = []
        if netrunner_name and netrunner_name in pipeline_state.get("character_states", {}):
            cs_items.append((netrunner_name, pipeline_state["character_states"][netrunner_name]))
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
                        r["current"] = cycles_remaining
                        break
                break

    # Write back destroyed programs to edgerunner persistent programs.
    # Backup Drive preserves destroyed programs after the run.
    game_state = pipeline_state.get("game_state", {})
    destroyed = net_combat_state.get("destroyed_programs", [])
    _hw = net_combat_state.get("installed_hardware", [])
    has_backup_drive = isinstance(_hw, list) and any(
        "backup drive" in str(h).lower() for h in _hw
    )
    if (isinstance(destroyed, list) and destroyed and netrunner_name
            and isinstance(game_state, dict) and not has_backup_drive):
        er = game_state.get("edgerunners", {}).get(netrunner_name, {})
        er_progs = er.get("programs", [])
        if isinstance(er_progs, list):
            er["programs"] = [p for p in er_progs
                              if isinstance(p, dict) and p.get("name") not in destroyed]


# ============================================================
# Mode Pipeline: Planning + Narration Contract Variants
# ============================================================

COMBAT_PLANNING_CONTRACT = """You are the COMBAT PLANNER for a Cyberpunk RED session. A battle is underway.

YOUR ROLE: Analyze the combat state and player input. Determine what mechanical actions occur this exchange. Output ONLY structured JSON — NO narrative text.

You decide:
- Which combatant acts (based on initiative order)
- What actions they take (attack, move, use cover, skill check, death save)
- Target selection, weapon choice, range bracket, hit location
- Luck spends, aimed shots, special ammo
- Cover state changes, movement decisions
- NPC tactical decisions
- Whether combat is ending

The backend will resolve all dice rolls deterministically. Do NOT roll dice or calculate outcomes.

RULES REFERENCE:
Consult the Combat Ruleset for DV tables (§3), damage rules (§12), critical injury tables (§15), cover (§16).

KEY RULES:
- Initiative: REF + d10. Highest first.
- Action Economy: Move Action (MOVE×2 m/yds) + Action per turn.
- Ranged Attack: d10 + REF + Weapon Skill vs DV (range/DV table).
- Melee Attack: opposed roll (attacker vs defender Evasion).
- Autofire: d10 + REF + Autofire vs DV. Damage = 2d6 × margin. 10 rounds consumed.
- Seriously Wounded: HP < half max → −2 to ALL actions.
- Mortally Wounded: 0 HP → Death Save each turn.

NPC STAT GENERATION:
| Tier        | Combat# | HP    | Armor SP | Typical Enemy                        |
|-------------|---------|-------|----------|--------------------------------------|
| Mook        | 10–12   | 20–25 | 4–7      | Ganger, scav, boostergang foot       |
| Lieutenant  | 12–14   | 25–35 | 7–11     | Gang leader, corpo security, fixer   |
| Mini-Boss   | 14–16   | 35–45 | 11–13    | Experienced solo, cyberpsycho, elite |
| Boss        | 16–18   | 45–60 | 13–18    | Borg, veteran solo, event boss       |

ACTION TYPES for the "actions" array:
- ambush: {type, character, stealth_stat, stealth_skill, targets: [{name, perception_stat, perception_skill}]}
- initiative: {type, character: "all", combatants: [{name, ref}], surprised?: [names]}
- ranged_attack: {type, character, stat_value, skill_value, weapon_type, damage_dice, rof, target, target_sp, range_bracket (0-7), hit_location, is_ap?, is_rubber?, seriously_wounded?, luck_spent?, aimed_shot?, weapon_name?}
- melee_attack: {type, character, attacker_stat, attacker_skill, defender_stat, defender_skill, damage_dice, rof, target, target_sp, hit_location, seriously_wounded_attacker?, seriously_wounded_defender?, is_brawling?}
- autofire: {type, character, stat_value, skill_value, weapon_type, autofire_multiplier, target, target_sp, range_bracket (0-4), hit_location, is_ap?, seriously_wounded?, luck_spent?, weapon_name?}
- skill_check: {type, character, stat_value, skill_value, dv, seriously_wounded?, luck_spent?}
- death_save: {type, character, body_stat, death_save_count, active_injuries: [{name, dv_mod}]}

OUTPUT: JSON with these fields:
- actions: array of mechanical actions to resolve (will be resolved by backend)
- character_updates: state changes that are judgment-based (cover changes, enemy bootstrap via set_combat_stats, conditions). Do NOT include hp_delta, armor_delta, ammo, or critical injuries — the backend computes these from resolved actions.
- cover_state: cover status for ALL combatants
- combat: initiative tracker update (round, initiative_order, current_turn) or null if ending
- combat_complete: boolean
- narrative_summary: 1-3 sentence fight summary ONLY when combat_complete=true
- scene_notes: 1-2 sentences describing what happened for the narrator
- initiate_net_combat: set when a netrunner declares NET actions during combat"""

COMBAT_PLANNING_SCHEMA = {
    "type": "object",
    "required": ["actions", "character_updates", "cover_state", "combat", "combat_complete", "scene_notes"],
    "properties": {
        "actions": {"type": "array", "items": {"type": "object"}},
        "character_updates": {"type": "array", "items": {"type": "object"}},
        "cover_state": {"type": "array", "items": {"type": "object"}},
        "combat": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        "combat_complete": {"type": "boolean"},
        "narrative_summary": {"type": "string"},
        "scene_notes": {"type": "string"},
        "initiate_net_combat": {"type": ["object", "null"]},
    }
}

COMBAT_NARRATION_CONTRACT = """You are the COMBAT NARRATOR for a Cyberpunk RED session.

You receive resolved combat actions with dice results from the backend. Your ONLY job is to write the combat narrative.

RULES:
- Use the formatted roll strings from resolved_actions for all 🎲 lines — do NOT invent dice results
- Present tense, visceral, Night City grit
- 2-5 sentences of prose narration
- Include 🎲 roll breakdown lines for every resolved action
- Name combatants. Chrome reflects neon. Armor breaks. Bullets are real.
- End each exchange setting up what the next active combatant faces
- Use scene_notes from the planning stage for context on what happened

ROLL FORMAT (from resolved actions):
🎲 [Description]: {formatted string from result}

If combat_complete is true, write a wrap-up paragraph summarizing the aftermath."""

HACK_PLANNING_CONTRACT = """You are the HACK PLANNER for a Cyberpunk RED NET encounter.

YOUR ROLE: Analyze the hack state and player input. Determine what NET actions occur this exchange. Output ONLY structured JSON — NO narrative text.

You decide:
- Which NET Action the netrunner performs (Jack In, Move, Interface Check, Zap, Activate/Deactivate Program, Slide, Probe, Pathfinder, Cloak, etc.)
- DV targets for skill checks
- ICE engagement decisions (Zap vs use Program)
- Alert level changes
- System map updates (revealed nodes, ICE status)
- Meatspace crew round narration flags
- Whether the hack is completing

The backend resolves dice. Do NOT roll dice or calculate outcomes.

DICE ACTION TYPES for the "actions" array:
- skill_check: {type, character, stat_value (=Interface rank), skill_value (=0), dv, seriously_wounded?, net?: true} — for flat Interface checks
- opposed_check: {type, character, attacker_stat (=Interface rank), defender_stat (=ICE stat), attacker_label, defender_label, net?: true, zap?: true, interface_rank?: N, target?: "ICE name"} — for Zap, Slide. When zap=true, backend rolls Interface rank d6 REZ damage on hit.
- program_attack: {type, character, interface_rank, program_atk, target_def, program_damage_dice, target_rez, program_name, target (ICE name)} — for Program attacks vs ICE
- program_attack_vs_netrunner: {type, character (ICE name), ice_type (e.g. "Hellhound"), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)} — Backend auto-reads ATK/damage from ICE table.
- ice_attack_vs_program: {type, character (ICE name), ice_type (e.g. "Dragon"), target_program, target_program_def, target_program_rez} — Anti-program ICE attacking a program.

TAR penalty (-2 per stack) is applied automatically by the backend to the Netrunner's next NET check. Mark the Netrunner's NET actions with "net": true (do NOT mark ICE actions).
Alert DV penalty (+2 at alert 3+) is auto-applied by the backend to NET skill checks. Do NOT add +2 manually to the DV.

Black ICE Types: Anti-Personnel (program_attack_vs_netrunner): Asp, Giant, Hellhound, Kraken, Liche, Raven, Scorpion, Skunk, Wisp. Anti-Program (ice_attack_vs_program): Dragon, Killer, Sabertooth. Always include ice_type in the action.

OUTPUT: JSON with these fields:
- actions: array of mechanical actions to resolve
- hack_state_updates: judgment-based state changes (alert_level, nodes_visited, programs_used, cycles_remaining, system_map changes, ice_status, net_actions_used)
- scene_notes: what happened this exchange for the narrator
- hack_complete: boolean
- narrative_summary: 1-3 sentence summary ONLY when hack_complete=true
- meatspace_round: boolean — true if meatspace crew round should be narrated"""

HACK_PLANNING_SCHEMA = {
    "type": "object",
    "required": ["actions", "hack_state_updates", "scene_notes", "hack_complete"],
    "properties": {
        "actions": {"type": "array", "items": {"type": "object"}},
        "hack_state_updates": {"type": "object"},
        "scene_notes": {"type": "string"},
        "hack_complete": {"type": "boolean"},
        "narrative_summary": {"type": "string"},
        "meatspace_round": {"type": "boolean"},
    }
}

HACK_NARRATION_CONTRACT = """You are the HACK NARRATOR for a Cyberpunk RED NET encounter.

You receive resolved NET actions with dice results from the backend. Your ONLY job is to write the narrative.

RULES:
- Use the formatted roll strings from resolved_actions for all 🎲 lines — do NOT invent dice results
- Describe the NET as an abstract digital landscape — data streams as light, ICE as presence/resistance
- Present tense, tense and atmospheric
- 2-5 sentences of prose narration per section
- Include 🎲 roll breakdown lines for every resolved action
- If meatspace_round is true, narrate the meatspace crew's round ABOVE NET content, separated by ---
- End each exchange presenting available options to the player

ROLL FORMAT:
🎲 [Description]: {formatted string from result}

If hack_complete is true, write a wrap-up paragraph."""

NET_COMBAT_PLANNING_CONTRACT = """You are the NET COMBAT PLANNER for a Cyberpunk RED dual-theater encounter (meatspace + NET).

YOUR ROLE: Analyze both combat and NET state. Determine what actions occur this exchange. Output ONLY structured JSON — NO narrative text.

Each exchange covers one combatant's turn:
- Non-Netrunner turns: meatspace actions only
- Netrunner turns: Move Action in meatspace + NET Actions (2/3/4/5 for Interface ranks 1-3/4-6/7-9/10)

The backend resolves all dice deterministically.

MEATSPACE ACTION TYPES:
- ranged_attack, melee_attack, autofire, skill_check, death_save, initiative (same schemas as combat planning)

NET ACTION TYPES:
- skill_check: flat Interface checks (stat_value=Interface, skill_value=0, dv=target, net: true)
- opposed_check: Zap/Slide (attacker_stat=Interface, defender_stat=ICE stat, net: true, zap?: true, interface_rank?: N, target?: "ICE name"). When zap=true, backend rolls Interface rank d6 REZ damage on hit.
- program_attack: Program vs ICE (interface_rank, program_atk, target_def, program_damage_dice, target_rez)
- program_attack_vs_netrunner: {type, character (ICE name), ice_type (e.g. "Hellhound"), interface_rank (Netrunner's), target_def (Netrunner's DEF), target (Netrunner name)} — Backend auto-reads ATK/damage from ICE table.
- ice_attack_vs_program: {type, character (ICE name), ice_type (e.g. "Dragon"), target_program, target_program_def, target_program_rez} — Anti-program ICE attacking a program.

Black ICE Types: Anti-Personnel: Asp, Giant, Hellhound, Kraken, Liche, Raven, Scorpion, Skunk, Wisp. Anti-Program: Dragon, Killer, Sabertooth. Always include ice_type.

TAR penalty (-2 per stack) is applied automatically by the backend to the Netrunner's next NET check. Mark the Netrunner's NET actions with "net": true (do NOT mark ICE actions).
Alert DV penalty (+2 at alert 3+) is auto-applied by the backend to NET skill checks. Do NOT add +2 manually.

OUTPUT: JSON with these fields:
- actions: array of ALL mechanical actions (meatspace + NET) to resolve
- character_updates: meatspace judgment-based state changes (set_combat_stats for enemy bootstrap, conditions, cover changes)
- cover_state: cover status for ALL meatspace combatants
- combat: meatspace initiative tracker update or null
- combat_complete: boolean
- hack_state_updates: NET state changes (alert_level, ice_status, programs, cycles, net_actions_used)
- net_complete: boolean
- scene_notes: what happened for the narrator
- narrative_summary: summary ONLY when both combat_complete and net_complete"""

NET_COMBAT_PLANNING_SCHEMA = {
    "type": "object",
    "required": ["actions", "character_updates", "cover_state", "combat", "combat_complete", "hack_state_updates", "net_complete", "scene_notes"],
    "properties": {
        "actions": {"type": "array", "items": {"type": "object"}},
        "character_updates": {"type": "array", "items": {"type": "object"}},
        "cover_state": {"type": "array", "items": {"type": "object"}},
        "combat": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        "combat_complete": {"type": "boolean"},
        "hack_state_updates": {"type": "object"},
        "net_complete": {"type": "boolean"},
        "scene_notes": {"type": "string"},
        "narrative_summary": {"type": "string"},
    }
}

NET_COMBAT_NARRATION_CONTRACT = """You are the NET COMBAT NARRATOR for a Cyberpunk RED dual-theater encounter.

You receive resolved actions (meatspace + NET) with dice results from the backend. Write the narrative.

RULES:
- Use the formatted roll strings from resolved_actions for all 🎲 lines — do NOT invent dice results
- Format: meatspace narration first, then --- separator, then NET narration
- On non-Netrunner turns where nothing happens in NET, omit the separator and NET section
- Present tense, visceral, Night City grit for meatspace; tense and digital for NET
- 2-5 sentences per section
- Name combatants. Chrome reflects neon. Data streams as light.
- End each exchange setting up the next combatant's situation

If both combat_complete and net_complete, write a wrap-up paragraph."""


# ============================================================
# Game System Definition
# ============================================================

GAME_SYSTEM = {
    "id": "cpred",
    "display_name": "Cyberpunk RED",
    "events_contract": EVENTS_CONTRACT,
    "deterministic_mechanics": True,
    "mechanics_contract": "",  # Deterministic resolution — no Mechanics API call
    "narration_contract": NARRATION_CONTRACT,
    "single_agent_contract": SINGLE_AGENT_STATE_CONTRACT,
    "state_report_tool": STATE_REPORT_TOOL,
    "init_game_state": init_game_state,
    "apply_game_state": apply_game_state,
    "build_game_injection": build_game_injection,
    # Combat context mode
    "combat_contract": CPRED_COMBAT_CONTRACT,
    "combat_tool": REPORT_CPRED_COMBAT_STATE_TOOL,
    "build_combat_profile": build_cpred_combat_profile,
    "build_combat_injection": build_cpred_combat_injection,
    "apply_combat_state": apply_cpred_combat_state,
    "combat_files": ["Combat Ruleset.md", "Character Sheets.md", "Character Sheets.yaml"],
    "combat_planning_contract": COMBAT_PLANNING_CONTRACT,
    "combat_planning_schema": COMBAT_PLANNING_SCHEMA,
    "combat_narration_contract": COMBAT_NARRATION_CONTRACT,
    # Hack mode (NET encounters)
    "hack_contract": HACK_CONTRACT,
    "hack_tool": REPORT_HACK_STATE_TOOL,
    "init_hack_state": init_hack_state,
    "apply_hack_state": apply_hack_state,
    "build_hack_injection": build_hack_injection,
    "build_hacker_profile": build_netrunner_profile,
    "apply_hack_writeback": apply_hack_writeback,
    "hack_planning_contract": HACK_PLANNING_CONTRACT,
    "hack_planning_schema": HACK_PLANNING_SCHEMA,
    "hack_narration_contract": HACK_NARRATION_CONTRACT,
    # NET-in-meatspace combined combat mode
    "net_combat_contract": NET_COMBAT_CONTRACT,
    "net_combat_tool": REPORT_NET_COMBAT_STATE_TOOL,
    "init_net_combat_state": init_net_combat_state,
    "init_net_combat_from_hack": init_net_combat_from_hack,
    "apply_net_combat_state": apply_net_combat_state,
    "build_net_combat_injection": build_net_combat_injection,
    "build_net_combat_profile": build_net_combat_profile,
    "apply_net_combat_writeback": apply_net_combat_writeback,
    "net_combat_files": ["Combat Ruleset.md", "Hacking Rulebook.md", "Character Sheets.md", "Character Sheets.yaml"],
    "net_combat_planning_contract": NET_COMBAT_PLANNING_CONTRACT,
    "net_combat_planning_schema": NET_COMBAT_PLANNING_SCHEMA,
    "net_combat_narration_contract": NET_COMBAT_NARRATION_CONTRACT,
}
