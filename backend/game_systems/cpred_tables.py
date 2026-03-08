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
# Black ICE / Anti-Program ICE stat blocks (CRB pp. 204-208)
# ---------------------------------------------------------------------------
ICE_STAT_BLOCKS = {
    "asp":        {"name": "Asp",        "class": "anti_personnel", "per": 4, "spd": 6, "atk": 2, "def": 2, "rez": 15, "damage_dice": 3, "effect": "program_destroy",   "effect_desc": "Destroys 1 random rezzed Program"},
    "giant":      {"name": "Giant",      "class": "anti_personnel", "per": 2, "spd": 2, "atk": 8, "def": 4, "rez": 25, "damage_dice": 3, "effect": "forced_jack_out",   "effect_desc": "3d6 brain damage + forced unsafe Jack Out"},
    "hellhound":  {"name": "Hellhound",  "class": "anti_personnel", "per": 6, "spd": 6, "atk": 6, "def": 2, "rez": 20, "damage_dice": 2, "effect": "body_fire",         "effect_desc": "2d6 brain damage + clothes catch fire (2 meat HP/turn)", "defense_hardware": "Insulated Wiring"},
    "kraken":     {"name": "Kraken",     "class": "anti_personnel", "per": 6, "spd": 2, "atk": 8, "def": 4, "rez": 30, "damage_dice": 3, "effect": "movement_lock",      "effect_desc": "3d6 brain damage + cannot move nodes"},
    "liche":      {"name": "Liche",      "class": "anti_personnel", "per": 8, "spd": 2, "atk": 6, "def": 2, "rez": 25, "damage_dice": 0, "effect": "stat_debuff",        "effect_desc": "Reduces INT, REF, DEX by 1d6 each", "debuff_stats": ["INT", "REF", "DEX"]},
    "raven":      {"name": "Raven",      "class": "anti_personnel", "per": 6, "spd": 4, "atk": 4, "def": 2, "rez": 15, "damage_dice": 1, "effect": "program_destroy",    "effect_desc": "1d6 brain damage + destroys 1 Defender program", "targets_defender": True},
    "scorpion":   {"name": "Scorpion",   "class": "anti_personnel", "per": 2, "spd": 6, "atk": 2, "def": 2, "rez": 15, "damage_dice": 0, "effect": "stat_debuff",        "effect_desc": "Reduces MOVE by 1d6", "debuff_stats": ["MOVE"]},
    "skunk":      {"name": "Skunk",      "class": "anti_personnel", "per": 2, "spd": 4, "atk": 4, "def": 2, "rez": 10, "damage_dice": 0, "effect": "slide_penalty",      "effect_desc": "-2 to all Slide checks until derezzed"},
    "wisp":       {"name": "Wisp",       "class": "anti_personnel", "per": 4, "spd": 4, "atk": 4, "def": 2, "rez": 15, "damage_dice": 1, "effect": "net_action_penalty", "effect_desc": "1d6 brain damage + lose 1 NET Action next turn"},
    "dragon":     {"name": "Dragon",     "class": "anti_program",   "per": 6, "spd": 4, "atk": 6, "def": 6, "rez": 30, "damage_dice": 6, "effect": "program_rez_damage", "effect_desc": "6d6 REZ damage to target program"},
    "killer":     {"name": "Killer",     "class": "anti_program",   "per": 4, "spd": 8, "atk": 6, "def": 2, "rez": 20, "damage_dice": 4, "effect": "program_rez_damage", "effect_desc": "4d6 REZ damage to target program"},
    "sabertooth": {"name": "Sabertooth", "class": "anti_program",   "per": 8, "spd": 6, "atk": 6, "def": 2, "rez": 25, "damage_dice": 6, "effect": "program_rez_damage", "effect_desc": "6d6 REZ damage to target program"},
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

# ---------------------------------------------------------------------------
# Program stats (CRB p. 204, Hacking Rulebook §2)
# ---------------------------------------------------------------------------
PROGRAM_STATS = {
    "Eraser":           {"category": "booster",  "atk": 0, "def": 0, "rez": 7, "effect": "+2 Cloak",                          "cost": 20},
    "SeeYa":            {"category": "booster",  "atk": 0, "def": 0, "rez": 7, "effect": "+2 Pathfinder",                     "cost": 20},
    "Speedy Gonzalvez": {"category": "booster",  "atk": 0, "def": 0, "rez": 7, "effect": "+2 Speed",                          "cost": 100},
    "Worm":             {"category": "booster",  "atk": 0, "def": 0, "rez": 7, "effect": "+2 Backdoor",                       "cost": 50},
    "Armor":            {"category": "defender", "atk": 0, "def": 0, "rez": 7, "effect": "Reduce brain dmg by 4",             "cost": 50},
    "Flak":             {"category": "defender", "atk": 0, "def": 0, "rez": 7, "effect": "Enemy non-Black-ICE ATK → 0",       "cost": 50},
    "Shield":           {"category": "defender", "atk": 0, "def": 0, "rez": 7, "effect": "Block first brain dmg then derez",  "cost": 20},
    "Banhammer":        {"category": "attacker", "atk": 1, "def": 0, "rez": 0, "effect": "3d6 REZ non-Black; 2d6 Black",     "cost": 50},
    "Sword":            {"category": "attacker", "atk": 1, "def": 0, "rez": 0, "effect": "3d6 REZ Black; 2d6 non-Black",     "cost": 50},
    "DeckKRASH":        {"category": "attacker", "atk": 0, "def": 0, "rez": 0, "effect": "Unsafe jack out",                   "cost": 100},
    "Hellbolt":         {"category": "attacker", "atk": 2, "def": 0, "rez": 0, "effect": "2d6 brain + deck fire",             "cost": 100},
    "Nervescrub":       {"category": "attacker", "atk": 0, "def": 0, "rez": 0, "effect": "INT/REF/DEX -1d6 1hr",             "cost": 100},
    "Poison Flatline":  {"category": "attacker", "atk": 0, "def": 0, "rez": 0, "effect": "Destroy random non-Black program", "cost": 100},
    "Superglue":        {"category": "attacker", "atk": 2, "def": 0, "rez": 0, "effect": "No move/jack out 1d6 rounds",      "cost": 100},
    "Vrizzbolt":        {"category": "attacker", "atk": 1, "def": 0, "rez": 0, "effect": "1d6 brain; -1 NET Action",         "cost": 50},
}

# ---------------------------------------------------------------------------
# Cyberdeck stats (CRB p. 204, Hacking Rulebook §2)
# ---------------------------------------------------------------------------
CYBERDECK_STATS = {
    "Poor":      {"slots": 5, "cycles": 2, "cost": 100},
    "Standard":  {"slots": 7, "cycles": 3, "cost": 500},
    "Excellent": {"slots": 9, "cycles": 4, "cost": 1000},
}

# ---------------------------------------------------------------------------
# Hardware stats (CRB p. 204, Hacking Rulebook §2)
# ---------------------------------------------------------------------------
HARDWARE_STATS = {
    "Backup Drive":       {"slots": 2, "effect": "Save destroyed programs; re-install as Meat Action"},
    "DNA Lock":           {"slots": 2, "effect": "DV17 Electronics/Security Tech to use deck"},
    "Hardened Circuitry": {"slots": 1, "effect": "Deck immune to EMP"},
    "Insulated Wiring":   {"slots": 1, "effect": "Deck+user immune to fire from Programs"},
    "KRASH Barrier":      {"slots": 2, "effect": "Immune to forced unsafe Jack Out"},
    "Range Upgrade":      {"slots": 1, "effect": "Access points from 8m (default 6m)"},
}

# ---------------------------------------------------------------------------
# Cyberware master table (CRB pp. 110-117, §9.1)
# ceiling_cost: 2 = standard, 4 = borgware, 0 = medical exempt
# ---------------------------------------------------------------------------
CYBERWARE_TABLE = {
    # --- Neuralware (Foundational: Neural Link) ---
    "Neural Link":          {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": True,  "option_slots": 5, "requires": None,         "description": "Required for Neuralware/Subdermal Grips"},
    "Braindance Recorder":  {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Neural Link", "description": "Record experiences to Memory Chip"},
    "Chipware Socket":      {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Neural Link", "description": "Slot for one piece of Chipware"},
    "Interface Plugs":      {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Neural Link", "description": "Connect to Smartguns/Vehicles"},
    "Kerenzikov":           {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Neural Link", "description": "+2 Initiative"},
    "Sandevistan":          {"category": "neuralware", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Neural Link", "description": "Action: +3 Initiative 1 min (1 hr cooldown)"},
    "Pain Editor":          {"category": "neuralware", "install": "N/A",       "cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Chipware Socket", "description": "Chipware. Ignore Seriously Wounded penalties"},
    "Skill Chip":           {"category": "neuralware", "install": "N/A",       "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Chipware Socket", "description": "Chipware. Sets Skill to Level 3"},
    # --- Cyberoptics (Foundational: Cybereye) ---
    "Cybereye":             {"category": "cyberoptic", "install": "Clinic",    "cost": 100,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": True,  "option_slots": 3, "requires": None,         "description": "Artificial eye"},
    "Anti-Dazzle":          {"category": "cyberoptic", "install": "Mall",      "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "Immune to flashes/flashbangs. Pair required"},
    "Dartgun (Eye)":        {"category": "cyberoptic", "install": "Clinic",    "cost": 500,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "1-shot exotic weapon in eye (3 slots)"},
    "Image Enhance":        {"category": "cyberoptic", "install": "Mall",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "+2 Perception, Lip Reading, Conceal/Reveal. Pair"},
    "Infrared/UV":          {"category": "cyberoptic", "install": "Mall",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "Ignore darkness/smoke penalties. Pair (2 slots ea)"},
    "Targeting Scope":      {"category": "cyberoptic", "install": "Clinic",    "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "+1 Aimed Shots"},
    "Virtuality":           {"category": "cyberoptic", "install": "Mall",      "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cybereye",   "description": "Projects NET imagery over reality. Pair"},
    # --- Cyberaudio (Foundational: Cyberaudio Suite) ---
    "Cyberaudio Suite":     {"category": "cyberaudio", "install": "Clinic",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": True,  "option_slots": 3, "requires": None,         "description": "Internalized audio system"},
    "Amplified Hearing":    {"category": "cyberaudio", "install": "Mall",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberaudio Suite", "description": "+2 hearing-based Perception"},
    "Internal Agent":       {"category": "cyberaudio", "install": "Mall",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberaudio Suite", "description": "Fully functional Agent; voice-controlled"},
    "Level Damper":         {"category": "cyberaudio", "install": "Mall",      "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberaudio Suite", "description": "Immune to deafness from loud noises"},
    "Voice Stress Analyzer":{"category": "cyberaudio", "install": "Mall",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberaudio Suite", "description": "+2 Human Perception and Interrogation"},
    # --- Internal Body Cyberware ---
    "AudioVox":             {"category": "internal_body", "install": "Clinic",  "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Voice synth. +2 Acting and Play Instrument (Singing)"},
    "Contraceptive Implant":{"category": "internal_body", "install": "Mall",    "cost": 10,    "hl_fixed": 0,  "hl_dice": "N/A", "ceiling_cost": 0, "foundational": False, "option_slots": 0, "requires": None, "description": "Prevents pregnancy"},
    "Enhanced Antibodies":  {"category": "internal_body", "install": "Mall",    "cost": 500,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Heal BODY x2 per day resting"},
    "Cybersnake":           {"category": "internal_body", "install": "Hospital","cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Esophagus weapon (4d6, 1 ROF). Concealable"},
    "Gills":                {"category": "internal_body", "install": "Hospital","cost": 1000,  "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Breathe underwater"},
    "Grafted Muscle/Bone Lace": {"category": "internal_body", "install": "Hospital","cost": 1000, "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "BODY +2 (max 10). Changes HP/Wound stats"},
    "Independent Air Supply":{"category": "internal_body", "install": "Hospital","cost": 1000,  "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "30 min oxygen. 1 hr refill"},
    "Midnight Lady / Mr. Studd": {"category": "internal_body", "install": "Clinic","cost": 100, "hl_fixed": 7, "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Sexual implants"},
    "Nasal Filters":        {"category": "internal_body", "install": "Clinic",  "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Immune to toxic gases/fumes"},
    "Radar/Sonar Implant":  {"category": "internal_body", "install": "Clinic",  "cost": 1000,  "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Scan terrain 50m. Detects moving objects"},
    "Toxin Binders":        {"category": "internal_body", "install": "Clinic",  "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "+2 Resist Torture/Drugs"},
    "Vampyres":             {"category": "internal_body", "install": "Clinic",  "cost": 500,   "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Mouth fangs (1d6, 2 ROF). Store Poison/Biotoxin"},
    # --- External Body Cyberware ---
    "Hidden Holster":       {"category": "external_body", "install": "Clinic",  "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Conceal weapon inside body without roll"},
    "Skin Weave":           {"category": "external_body", "install": "Hospital","cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Armor SP7. Ablates. Heals 1 SP/day"},
    "Subdermal Armor":      {"category": "external_body", "install": "Hospital","cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Armor SP11. Ablates. Heals 1 SP/day"},
    "Subdermal Pocket":     {"category": "external_body", "install": "Clinic",  "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "2x4 inch hidden storage"},
    # --- Cyberlimbs ---
    "Cyberarm":             {"category": "cyberlimb", "install": "Hospital",    "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": True,  "option_slots": 4, "requires": None, "description": "Replacement arm. Includes Standard Hand"},
    "Cyberleg":             {"category": "cyberlimb", "install": "Hospital",    "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": True,  "option_slots": 3, "requires": None, "description": "Replacement leg. Includes Standard Foot"},
    "Standard Hand":        {"category": "cyberlimb", "install": "Clinic",      "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Replacement hand. No slot cost"},
    "Standard Foot":        {"category": "cyberlimb", "install": "Clinic",      "cost": 100,   "hl_fixed": 2,  "hl_dice": "1d6/2", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": None, "description": "Replacement foot. No slot cost"},
    "Big Knucks":           {"category": "cyberlimb", "install": "Clinic",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "2d6 Melee. Concealable"},
    "Grapple Hand":         {"category": "cyberlimb", "install": "Clinic",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "30m grapple line"},
    "Popup Grenade Launcher":{"category": "cyberlimb", "install": "Clinic",     "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "1-shot GL. 2 slots"},
    "Popup Melee Weapon":   {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "Light/Med/Heavy Melee. 2 slots"},
    "Popup Shield":         {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "Bulletproof shield in arm. 3 slots"},
    "Popup Ranged Weapon":  {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "One-handed ranged weapon. 2 slots"},
    "Rippers":              {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "Carbo-glass claws (2d6). Concealable"},
    "Slice 'N Dice":        {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "Monofilament whip (2d6)"},
    "Tool Hand":            {"category": "cyberlimb", "install": "Clinic",      "cost": 100,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "Fingers = screwdriver, wrench, drill"},
    "Wolvers":              {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 7,  "hl_dice": "2d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberarm", "description": "3d6 Melee. Concealable"},
    "Jump Booster":         {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberleg", "description": "Negate jump movement penalty. Pair"},
    "Skate Foot":           {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberleg", "description": "+6m MOVE with Run Action. Pair"},
    "Talon Foot":           {"category": "cyberlimb", "install": "Clinic",      "cost": 500,   "hl_fixed": 3,  "hl_dice": "1d6", "ceiling_cost": 2, "foundational": False, "option_slots": 0, "requires": "Cyberleg", "description": "Light Melee Weapon in foot"},
    # --- Borgware ---
    "Artificial Shoulder Mount": {"category": "borgware", "install": "Hospital","cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 4, "foundational": False, "option_slots": 0, "requires": None, "description": "Mount 2 extra Cyberarms under first set"},
    "Linear Frame Sigma":   {"category": "borgware", "install": "Hospital",     "cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 4, "foundational": False, "option_slots": 0, "requires": "Grafted Muscle/Bone Lace", "description": "BODY to 12. Req BODY 6"},
    "Linear Frame Beta":    {"category": "borgware", "install": "Hospital",     "cost": 5000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 4, "foundational": False, "option_slots": 0, "requires": "Grafted Muscle/Bone Lace", "description": "BODY to 14. Req BODY 8 + 2x Grafted"},
    "MultiOptic Mount":     {"category": "borgware", "install": "Hospital",     "cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 4, "foundational": False, "option_slots": 0, "requires": None, "description": "Mount up to 5 additional Cybereyes"},
    "Sensor Array":         {"category": "borgware", "install": "Clinic",       "cost": 1000,  "hl_fixed": 14, "hl_dice": "4d6", "ceiling_cost": 4, "foundational": False, "option_slots": 0, "requires": None, "description": "Install 5 additional Cyberaudio options"},
}

# ---------------------------------------------------------------------------
# Skills by governing stat + x2 skills (CRB §4, pp. 88-117)
# ---------------------------------------------------------------------------
SKILL_STAT_MAP = {
    # Awareness (INT)
    "Conceal/Reveal Object": "INT", "Lip Reading": "INT", "Perception": "INT", "Tracking": "INT",
    # Body (DEX / WILL)
    "Athletics": "DEX", "Contortionist": "DEX", "Dance": "DEX", "Stealth": "DEX",
    "Concentration": "WILL", "Endurance": "WILL", "Resist Torture/Drugs": "WILL",
    # Control (REF)
    "Drive Land Vehicle": "REF", "Pilot Air Vehicle": "REF", "Pilot Sea Vehicle": "REF", "Riding": "REF",
    # Education (INT)
    "Accounting": "INT", "Animal Handling": "INT", "Bureaucracy": "INT", "Business": "INT",
    "Composition": "INT", "Criminology": "INT", "Cryptography": "INT", "Deduction": "INT",
    "Education": "INT", "Gamble": "INT", "Language": "INT", "Library Search": "INT",
    "Local Expert": "INT", "Science": "INT", "Tactics": "INT", "Wilderness Survival": "INT",
    # Fighting (DEX)
    "Brawling": "DEX", "Evasion": "DEX", "Martial Arts": "DEX", "Melee Weapon": "DEX",
    # Performance (COOL / TECH)
    "Acting": "COOL", "Play Instrument": "TECH", "Paint/Draw/Sculpt": "TECH",
    "Photography/Film": "TECH", "Forgery": "TECH",
    # Ranged Weapon (REF)
    "Archery": "REF", "Autofire": "REF", "Handgun": "REF", "Heavy Weapons": "REF", "Shoulder Arms": "REF",
    # Social (COOL / EMP)
    "Bribery": "COOL", "Conversation": "EMP", "Human Perception": "EMP",
    "Interrogation": "COOL", "Personal Grooming": "COOL", "Persuasion": "COOL",
    "Streetwise": "COOL", "Trading": "COOL", "Wardrobe & Style": "COOL",
    # Technique (TECH)
    "Air Vehicle Tech": "TECH", "Basic Tech": "TECH", "Cybertech": "TECH",
    "Demolitions": "TECH", "Electronics/Security Tech": "TECH", "First Aid": "TECH",
    "Land Vehicle Tech": "TECH", "Paramedic": "TECH", "Pick Lock": "TECH",
    "Pick Pocket": "TECH", "Sea Vehicle Tech": "TECH", "Weaponstech": "TECH",
}

X2_SKILLS = {
    "Martial Arts", "Pilot Air Vehicle", "Autofire", "Heavy Weapons",
    "Demolitions", "Electronics/Security Tech", "Paramedic",
}

# ---------------------------------------------------------------------------
# Price categories (CRB §10)
# ---------------------------------------------------------------------------
PRICE_CATEGORIES = {
    "Cheap": 10, "Everyday": 20, "Costly": 50, "Premium": 100,
    "Expensive": 500, "Very Expensive": 1000, "Luxury": 5000, "Super Luxury": 10000,
}

# ---------------------------------------------------------------------------
# Job pay per person (CRB §10)
# ---------------------------------------------------------------------------
JOB_PAY = {"Easy": 500, "Typical": 1000, "Dangerous": 2000}

# ---------------------------------------------------------------------------
# Healing / therapy (CRB §8)
# ---------------------------------------------------------------------------
HEALING_TABLE = {
    "stabilization_dv": {"lightly": 10, "seriously": 13, "mortally": 15},
    "hospital_costs": {10: 50, 13: 100, 15: 500, 17: 1000},
    "therapy": {
        "standard": {"cost": 500, "recovery": "2d6", "dv": 15},
        "extreme":  {"cost": 1000, "recovery": "4d6", "dv": 17},
        "addiction": {"cost": 1000, "dv": 15},
    },
}

# ---------------------------------------------------------------------------
# Solo Combat Awareness allocation (CRB §14 / Combat Ruleset §14)
# ---------------------------------------------------------------------------
SOLO_ALLOCATION = {
    "damage_deflection": {2: -1, 4: -2, 6: -3, 8: -4, 10: -5},
    "fumble_recovery": 4,
    "initiative_reaction": 1,
    "precision_attack": {3: 1, 6: 2, 9: 3},
    "spot_weakness": 1,
    "threat_detection": 1,
}

# ---------------------------------------------------------------------------
# Netrunner actions per Interface rank (CRB §6.3)
# ---------------------------------------------------------------------------
NETRUNNER_ACTIONS_PER_RANK = {1: 2, 2: 2, 3: 2, 4: 3, 5: 3, 6: 3, 7: 4, 8: 4, 9: 4, 10: 5}

# ---------------------------------------------------------------------------
# IP cost tables (CRB §7)
# ---------------------------------------------------------------------------
IP_COST_TABLES = {
    "typical":   {i: i * 20 for i in range(1, 11)},
    "difficult": {i: i * 40 for i in range(1, 11)},
    "role":      {i: i * 60 for i in range(1, 11)},
}

# ---------------------------------------------------------------------------
# Vehicle stats (CRB §18 / Combat Ruleset §18)
# SP defaults to 0; Armored Chassis upgrade sets SP=13 at bootstrap.
# Heavy Chassis adds +20 SDP at bootstrap.
# ---------------------------------------------------------------------------
VEHICLE_STATS = {
    "roadbike":         {"name": "Roadbike",              "sdp": 35,  "sp": 0, "seats": 2, "move_min": 20, "move_max": 30, "type": "land"},
    "superbike":        {"name": "Superbike",             "sdp": 35,  "sp": 0, "seats": 2, "move_min": 40, "move_max": 60, "type": "land"},
    "jetski":           {"name": "Jetski",                "sdp": 35,  "sp": 0, "seats": 2, "move_min": 20, "move_max": 40, "type": "sea"},
    "gyrocopter":       {"name": "Gyrocopter",            "sdp": 35,  "sp": 0, "seats": 2, "move_min": 30, "move_max": 50, "type": "air"},
    "compact":          {"name": "Compact Groundcar",     "sdp": 50,  "sp": 0, "seats": 4, "move_min": 20, "move_max": 30, "type": "land"},
    "high_performance": {"name": "High Perf. Groundcar",  "sdp": 50,  "sp": 0, "seats": 2, "move_min": 40, "move_max": 60, "type": "land"},
    "super_groundcar":  {"name": "Super Groundcar",       "sdp": 50,  "sp": 0, "seats": 2, "move_min": 50, "move_max": 60, "type": "land"},
    "speedboat":        {"name": "Speedboat",             "sdp": 50,  "sp": 0, "seats": 4, "move_min": 30, "move_max": 40, "type": "sea"},
    "helicopter":       {"name": "Helicopter",            "sdp": 55,  "sp": 0, "seats": 4, "move_min": 30, "move_max": 40, "type": "air"},
    "av_9":             {"name": "AV-9 Aerodyne",         "sdp": 60,  "sp": 0, "seats": 4, "move_min": 20, "move_max": 40, "type": "air"},
    "av_4":             {"name": "AV-4 Aerodyne",         "sdp": 100, "sp": 0, "seats": 6, "move_min": 20, "move_max": 40, "type": "air"},
    "yacht":            {"name": "Yacht",                 "sdp": 100, "sp": 0, "seats": 6, "move_min": 10, "move_max": 20, "type": "sea"},
    "aerozep":          {"name": "Aerozep",               "sdp": 100, "sp": 0, "seats": 4, "move_min": 10, "move_max": 20, "type": "air"},
}

DRIVING_CHECK_DVS = {
    "maintain_control": 10,
    "swerve": 13,
    "sharp_turn": 13,
    "emergency_stop": 13,
    "bootleg_turn": 17,
    "do_a_jump": 17,
    "landing": 13,
    "aerobatic_maneuver": 17,
}

RAMMING_DAMAGE_DICE = 6   # 6d6
RAMMING_NOS_BONUS_DICE = 2  # +2d6 when NOS-boosted with Combat Plow
PEDESTRIAN_DODGE_DV = 13  # DEX + Evasion
SPIKE_STRIP_DV = 17       # DV17 Drive Land Vehicle
SPIKE_STRIP_DAMAGE_DICE = 4  # 4d6, treated as weak point (doubled past SP)

VEHICLE_UPGRADES = {
    # Universal
    "armored_chassis":       {"name": "Armored Chassis",       "sp": 13, "sdp_bonus": 0,  "rank": 5},
    "bulletproof_glass_thin":{"name": "Bulletproof Glass (Thin)","cover_hp": 15, "rank": 1},
    "bulletproof_glass_thick":{"name": "Bulletproof Glass (Thick)","cover_hp": 30, "rank": 1},
    "nos":                   {"name": "NOS",                   "rank": 1},
    "onboard_flamethrower":  {"name": "Onboard Flamethrower",  "rank": 1},
    "onboard_machine_gun":   {"name": "Onboard Machine Gun",   "rank": 1},
    "seating_upgrade":       {"name": "Seating Upgrade",       "seats_bonus": 2, "rank": 1},
    # Land & Sea
    "onboard_melee_weapon":  {"name": "Onboard Melee Weapon",  "rank": 1},
    # All except Bikes/Jetskis/Gyrocopters
    "heavy_chassis":         {"name": "Heavy Chassis",         "sdp_bonus": 20, "rank": 1},
    "onboard_rocket_pod":    {"name": "Onboard Rocket Pod",    "rank": 5},
    "heavy_weapon_mount":    {"name": "Vehicle Heavy Weapon Mount", "rank": 5},
    # Land & Sea except Bikes/Jetskis
    "combat_plow":           {"name": "Combat Plow",           "rank": 1},
    # Groundcars only
    "deployable_spike_strip":{"name": "Deployable Spike Strip","rank": 1},
    # Bikes only
    "enhanced_interface":    {"name": "Enhanced Interface Plug Integration", "rank": 5},
}
