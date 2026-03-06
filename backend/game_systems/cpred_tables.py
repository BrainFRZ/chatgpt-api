"""
Cyberpunk RED CRB reference tables.

Sources: Core Full Rulebook.md, Combat Ruleset.md, Cyberpunk RED Errata.md
Errata v1.25 corrections applied where noted.
"""
import math

# ---------------------------------------------------------------------------
# Range brackets (index → distance range in metres)
# ---------------------------------------------------------------------------
RANGE_BRACKETS = [
    (0, 6),       # 0
    (7, 12),      # 1
    (13, 25),     # 2
    (26, 50),     # 3
    (51, 100),    # 4
    (101, 200),   # 5
    (201, 400),   # 6
    (401, 800),   # 7
]

# ---------------------------------------------------------------------------
# Ranged DV table  (weapon type → list of DV per range bracket index)
# None = weapon cannot fire at that range
# ---------------------------------------------------------------------------
RANGED_DV_TABLE = {
    "Pistol":           [13, 15, 20, 25, 30, 30, None, None],
    "SMG":              [15, 13, 15, 20, 25, 25, 30,   None],
    "Shotgun":          [13, 15, 20, 25, 30, 35, None, None],
    "Assault Rifle":    [17, 16, 15, 13, 15, 20, 25,   30],
    "Sniper Rifle":     [30, 25, 25, 20, 15, 16, 17,   20],
    "Bows & Crossbow":  [15, 13, 15, 17, 20, 22, None, None],
    "Grenade Launcher": [16, 15, 15, 17, 20, 22, 25,   None],
    "Rocket Launcher":  [17, 16, 15, 15, 20, 20, 25,   30],
}

# Autofire DV table (errata-corrected, p.173)
# Only 5 range brackets for autofire (0-6 through 51-100)
AUTOFIRE_DV_TABLE = {
    "SMG":           [20, 17, 20, 25, 30],
    "Assault Rifle": [22, 20, 17, 20, 25],
}

# ---------------------------------------------------------------------------
# Weapon stats — ranged
# ---------------------------------------------------------------------------
WEAPON_STATS_RANGED = {
    "Medium Pistol":    {"skill": "Handgun",       "damage_dice": 2, "mag": 12, "rof": 2, "hands": 1, "concealable": True,  "cost": 50,   "dv_type": "Pistol"},
    "Heavy Pistol":     {"skill": "Handgun",       "damage_dice": 3, "mag": 8,  "rof": 2, "hands": 1, "concealable": True,  "cost": 100,  "dv_type": "Pistol"},
    "V. Heavy Pistol":  {"skill": "Handgun",       "damage_dice": 4, "mag": 8,  "rof": 1, "hands": 1, "concealable": False, "cost": 100,  "dv_type": "Pistol"},
    "SMG":              {"skill": "Handgun",        "damage_dice": 2, "mag": 30, "rof": 1, "hands": 1, "concealable": True,  "cost": 100,  "dv_type": "SMG",         "autofire_multiplier": 3},
    "Heavy SMG":        {"skill": "Handgun",        "damage_dice": 3, "mag": 40, "rof": 1, "hands": 1, "concealable": False, "cost": 100,  "dv_type": "SMG",         "autofire_multiplier": 3},
    "Shotgun":          {"skill": "Shoulder Arms",  "damage_dice": 5, "mag": 4,  "rof": 1, "hands": 2, "concealable": False, "cost": 500,  "dv_type": "Shotgun"},
    "Assault Rifle":    {"skill": "Shoulder Arms",  "damage_dice": 5, "mag": 25, "rof": 1, "hands": 2, "concealable": False, "cost": 500,  "dv_type": "Assault Rifle", "autofire_multiplier": 4},
    "Sniper Rifle":     {"skill": "Shoulder Arms",  "damage_dice": 5, "mag": 4,  "rof": 1, "hands": 2, "concealable": False, "cost": 500,  "dv_type": "Sniper Rifle"},
    "Bows & Crossbow":  {"skill": "Archery",        "damage_dice": 4, "mag": 1,  "rof": 1, "hands": 2, "concealable": False, "cost": 100,  "dv_type": "Bows & Crossbow"},
    "Grenade Launcher": {"skill": "Heavy Weapons",  "damage_dice": 6, "mag": 2,  "rof": 1, "hands": 2, "concealable": False, "cost": 500,  "dv_type": "Grenade Launcher"},
    "Rocket Launcher":  {"skill": "Heavy Weapons",  "damage_dice": 8, "mag": 1,  "rof": 1, "hands": 2, "concealable": False, "cost": 500,  "dv_type": "Rocket Launcher"},
}

# ---------------------------------------------------------------------------
# Weapon stats — melee
# ---------------------------------------------------------------------------
WEAPON_STATS_MELEE = {
    "Light Melee":   {"damage_dice": 1, "rof": 2, "hands": 1, "concealable": True,  "cost": 50,  "note": None},
    "Medium Melee":  {"damage_dice": 2, "rof": 2, "hands": 1, "concealable": False, "cost": 50,  "note": None},
    "Heavy Melee":   {"damage_dice": 3, "rof": 2, "hands": 2, "concealable": False, "cost": 100, "note": "1-handed if BODY 8+"},
    "V. Heavy Melee":{"damage_dice": 4, "rof": 1, "hands": 2, "concealable": False, "cost": 500, "note": "1-handed if BODY 8+"},
}

# ---------------------------------------------------------------------------
# Exotic weapons (CRB pp. 347-350)
# ---------------------------------------------------------------------------
EXOTIC_WEAPONS = {
    "Air Pistol":              {"base_type": "Medium Pistol",    "damage_dice": 2, "mag": 12, "rof": 2, "skill": "Handgun",       "special": "Fires paint/acid balls. Acid lowers target SP by 1 on hit."},
    "Battleglove":             {"base_type": "Heavy Melee",      "damage_dice": 3, "mag": None, "rof": 2, "skill": "Melee Weapon", "special": "3 Cyberlimb option slots. Cannot be concealed."},
    "Constitution Hurricane":  {"base_type": "Shotgun",          "damage_dice": 5, "mag": 16, "rof": 2, "skill": "Shoulder Arms",  "special": "Requires BODY 11+. No Aimed Shots."},
    "Dartgun":                 {"base_type": "V. Heavy Pistol",  "damage_dice": 4, "mag": 8,  "rof": 1, "skill": "Handgun",       "special": "Fires Non-Basic Arrows. Mag 8."},
    "Flamethrower":            {"base_type": "Shotgun",          "damage_dice": 5, "mag": 4,  "rof": 1, "skill": "Heavy Weapons",  "special": "Ignites targets (4 dmg/turn). No Aimed Shots."},
    "Kendachi Mono-Three":     {"base_type": "V. Heavy Melee",   "damage_dice": 4, "mag": None, "rof": 1, "skill": "Melee Weapon", "special": "Ignores armor < SP11. Requires Biometric Key."},
    "Malorian Arms 3516":      {"base_type": "V. Heavy Pistol",  "damage_dice": 5, "mag": 8,  "rof": 1, "skill": "Handgun",       "special": "5d6 damage. Permanent Smartgun Link."},
    "Microwaver":              {"base_type": "V. Heavy Pistol",  "damage_dice": 0, "mag": 8,  "rof": 1, "skill": "Handgun",       "special": "No damage. Hit = DV15 Cybertech or 2 cyber-items fail 1 min."},
    "Militech Cowboy":         {"base_type": "Grenade Launcher", "damage_dice": 6, "mag": 4,  "rof": 2, "skill": "Heavy Weapons",  "special": "Requires BODY 11+."},
    "Rhinemetall EMG-86":      {"base_type": "Assault Rifle",    "damage_dice": 5, "mag": 25, "rof": 1, "skill": "Shoulder Arms",  "special": "Ignores armor < SP11. No Autofire/Aimed Shots. BODY 11+."},
    "Shrieker":                {"base_type": "V. Heavy Pistol",  "damage_dice": 4, "mag": 8,  "rof": 1, "skill": "Handgun",       "special": "Hit = DV15 Resist T/D or Damaged Ear crit injury."},
    "Stun Baton":              {"base_type": "Medium Melee",     "damage_dice": 2, "mag": None, "rof": 2, "skill": "Melee Weapon", "special": "No crit/ablation. Reduces target to 1 HP (Unconscious)."},
    "Stun Gun":                {"base_type": "Heavy Pistol",     "damage_dice": 3, "mag": 8,  "rof": 1, "skill": "Handgun",       "special": "No crit/ablation. Reduces target to 1 HP (Unconscious)."},
    "Tsunami Arms Helix":      {"base_type": "Assault Rifle",    "damage_dice": 5, "mag": 40, "rof": 1, "skill": "Shoulder Arms",  "special": "Autofire only. Multiplier 5. BODY 11+.", "autofire_multiplier": 5},
}

# ---------------------------------------------------------------------------
# Armor table
# ---------------------------------------------------------------------------
ARMOR_TABLE = {
    "Leathers":            {"sp": 4,  "penalty": 0, "cost": 20},
    "Kevlar":              {"sp": 7,  "penalty": 0, "cost": 50},
    "Light Armorjack":     {"sp": 11, "penalty": 0, "cost": 100},
    "Bodyweight Suit":     {"sp": 11, "penalty": 0, "cost": 1000},
    "Medium Armorjack":    {"sp": 12, "penalty": -2, "cost": 100},
    "Heavy Armorjack":     {"sp": 13, "penalty": -2, "cost": 500},
    "Flak":                {"sp": 15, "penalty": -4, "cost": 500},
    "Metalgear":           {"sp": 18, "penalty": -4, "cost": 5000},
    "Bulletproof Shield":  {"sp": 10, "penalty": 0, "cost": 100, "note": "shield, +10 HP cover"},
}

# ---------------------------------------------------------------------------
# Critical injury tables (2d6)
# dv_mod = Death Save penalty from this injury
# ---------------------------------------------------------------------------
CRIT_INJURY_BODY = {
    2:  {"name": "Dismembered Arm",  "effect": "Arm is gone. Drop items held in it.",                          "dv_mod": 1},
    3:  {"name": "Dismembered Hand", "effect": "Hand is gone. Drop items held in it.",                         "dv_mod": 1},
    4:  {"name": "Collapsed Lung",   "effect": "-2 MOVE (min 1).",                                             "dv_mod": 1},
    5:  {"name": "Broken Ribs",      "effect": "Moving >4m on foot re-deals crit bonus damage.",               "dv_mod": 0},
    6:  {"name": "Broken Arm",       "effect": "Arm cannot be used. Drop items in that hand.",                 "dv_mod": 0},
    7:  {"name": "Foreign Object",   "effect": "Moving >4m on foot re-deals crit bonus damage.",               "dv_mod": 0},
    8:  {"name": "Broken Leg",       "effect": "-4 MOVE (min 1).",                                             "dv_mod": 0},
    9:  {"name": "Torn Muscle",      "effect": "-2 to Melee Attacks.",                                         "dv_mod": 0},
    10: {"name": "Spinal Injury",    "effect": "Next turn: no Action (only Move).",                            "dv_mod": 1},
    11: {"name": "Crushed Fingers",  "effect": "-4 to all Actions involving that hand.",                       "dv_mod": 0},
    12: {"name": "Dismembered Leg",  "effect": "Leg is gone. -6 MOVE. Cannot dodge.",                          "dv_mod": 1},
}

CRIT_INJURY_HEAD = {
    2:  {"name": "Lost Eye",         "effect": "-4 Ranged Attacks and Perception checks involving vision.",    "dv_mod": 1},
    3:  {"name": "Brain Injury",     "effect": "-2 to all Actions.",                                            "dv_mod": 1},
    4:  {"name": "Damaged Eye",      "effect": "-2 Ranged Attacks and Perception checks involving vision.",    "dv_mod": 0},
    5:  {"name": "Concussion",       "effect": "-2 to all Actions.",                                            "dv_mod": 0},
    6:  {"name": "Broken Jaw",       "effect": "-4 to all Actions involving speech.",                           "dv_mod": 0},
    7:  {"name": "Foreign Object",   "effect": "Moving >4m on foot re-deals crit bonus damage.",               "dv_mod": 0},
    8:  {"name": "Whiplash",         "effect": "No additional effect beyond Death Save penalty.",               "dv_mod": 1},
    9:  {"name": "Cracked Skull",    "effect": "Aimed Shots to head = 3x damage past SP (instead of 2x).",    "dv_mod": 1},
    10: {"name": "Damaged Ear",      "effect": "Moving >4m = no Move Action next turn. -2 Perception (Hearing).", "dv_mod": 0},
    11: {"name": "Crushed Windpipe", "effect": "Cannot speak.",                                                 "dv_mod": 1},
    12: {"name": "Lost Ear",         "effect": "-4 to Perception checks involving hearing.",                    "dv_mod": 1},
}

# ---------------------------------------------------------------------------
# HP formula: 10 + 5 * ceil((BODY + WILL) / 2)
# ---------------------------------------------------------------------------
def calculate_hp(body: int, will: int) -> int:
    """Calculate max HP from BODY and WILL stats."""
    return 10 + 5 * math.ceil((body + will) / 2)


# ---------------------------------------------------------------------------
# Seriously wounded threshold: HP < ceil(max_hp / 2)
# ---------------------------------------------------------------------------
def seriously_wounded_threshold(max_hp: int) -> int:
    """HP value at or below which a character is seriously wounded."""
    return (max_hp + 1) // 2 - 1  # wounded when current < ceil(max/2)


# ---------------------------------------------------------------------------
# Difficulty Values
# ---------------------------------------------------------------------------
DV_TABLE = {
    "Simple": 9,
    "Everyday": 13,
    "Difficult": 15,
    "Professional": 17,
    "Heroic": 21,
    "Incredible": 24,
    "Legendary": 29,
}

# ---------------------------------------------------------------------------
# Ammunition types
# ---------------------------------------------------------------------------
AMMO_TYPES = {
    "Basic":           {"ablation_mod": 0, "special": None},
    "Armor-Piercing":  {"ablation_mod": 1, "special": "Ablates armor by 2 instead of 1."},
    "Biotoxin":        {"ablation_mod": 0, "special": "Arrows/Grenades only. No dmg. DV15 Resist or 3d6 direct HP."},
    "EMP":             {"ablation_mod": 0, "special": "Grenades only. No dmg. DV15 Cybertech or 2 cyber-items fail 1 min."},
    "Expansive":       {"ablation_mod": 0, "special": "On Foreign Object crit, re-roll for a different body crit."},
    "Flashbang":       {"ablation_mod": 0, "special": "Grenades only. No dmg. DV15 Resist or Damaged Eye + Ear 1 min."},
    "Incendiary":      {"ablation_mod": 0, "special": "If damage through armor, target On Fire (Mild: 2 dmg/turn)."},
    "Poison":          {"ablation_mod": 0, "special": "No dmg. DV13 Resist or 2d6 direct HP."},
    "Rubber":          {"ablation_mod": 0, "special": "No crit, no ablation. Cannot reduce HP below 1."},
    "Sleep":           {"ablation_mod": 0, "special": "No dmg. DV13 Resist or Prone + Unconscious 1 min."},
    "Smart":           {"ablation_mod": 0, "special": "Miss by ≤4 → second chance: 10 + 1d10 vs original DV."},
    "Smoke":           {"ablation_mod": 0, "special": "No dmg. 10m×10m smoke cloud 1 min. Obscured tasks: -4."},
}

# ---------------------------------------------------------------------------
# Wound states
# ---------------------------------------------------------------------------
WOUND_STATES = [
    {"name": "Lightly Wounded",  "check": lambda cur, mx: cur < mx and cur >= (mx + 1) // 2, "effect": "None"},
    {"name": "Seriously Wounded", "check": lambda cur, mx: cur > 0 and cur < (mx + 1) // 2,  "effect": "-2 to all Actions"},
    {"name": "Mortally Wounded",  "check": lambda cur, mx: cur <= 0,                          "effect": "-4 to all Actions, -6 MOVE (min 1), Death Save each turn"},
]

# ---------------------------------------------------------------------------
# Cover HP
# ---------------------------------------------------------------------------
COVER_HP = {
    "Steel (thick)":                 50,
    "Steel (thin)":                  25,
    "Stone (thick)":                 40,
    "Stone (thin)":                  20,
    "Bulletproof Glass (thick)":     30,
    "Bulletproof Glass (thin)":      15,
    "Concrete (thick)":              25,
    "Concrete (thin)":               10,
    "Wood (thick)":                  20,
    "Wood (thin)":                   5,
    "Plaster/Foam/Plastic (thick)":  15,
    "Plaster/Foam/Plastic (thin)":   0,
}

# ---------------------------------------------------------------------------
# Fire damage (environmental, per turn)
# Errata: environmental damage CANNOT cause Critical Injuries
# ---------------------------------------------------------------------------
FIRE_DAMAGE = {
    "Mild":   2,   # Wood fire
    "Strong": 4,   # Gasoline fire
    "Deadly": 6,   # Thermite
}

# ---------------------------------------------------------------------------
# Aimed shot effects
# All aimed shots add +8 to the attack DV
# ---------------------------------------------------------------------------
AIMED_SHOT_DV_PENALTY = 8   # Added to the DV for the attack roll

AIMED_SHOT_EFFECTS = {
    "head":      "Multiply damage past head SP by 2.",
    "held_item": "If any damage past body SP, target drops one held item.",
    "leg":       "If any damage past body SP, target suffers Broken Leg crit (if legs remain).",
}

# ---------------------------------------------------------------------------
# Errata corrections summary (applied in the tables above)
# ---------------------------------------------------------------------------
# p.173: Autofire DV table corrected (AUTOFIRE_DV_TABLE uses errata values)
# p.186: Ablation ONLY occurs if armor is penetrated AND target takes HP damage
# p.181: Environmental damage (fire, radiation, poisons, drugs, biotoxins) CANNOT cause Critical Injuries
# p.204: Brain damage bypasses armor, cannot cause Critical Injuries
# p.345: AP ammo is standard/basic for Grenades and Rockets
# p.412: Boosterganger BODY and Death Save changed to 2
