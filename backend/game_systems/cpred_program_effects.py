"""
Cyberpunk RED program / hardware effect registry.

Each entry in PROGRAM_EFFECTS declares hooks that fire at well-defined
pipeline points (interface check, brain damage inbound, program attack hit,
alert increase, turn start/end, program status change). The pipeline driver
runs hooks in declared `order` (lower runs first), filtered to programs that
are currently active (status == "active") and to installed hardware.

This module ships in two phases:
- Step 1 (this commit): the registry framework + pipeline drivers, no entries.
  Imports cleanly, all driver functions return no-op results when the registry
  is empty.
- Steps 2-6: effect entries are added (Insulated Wiring, KRASH Barrier,
  Backup Drive, Shield, Armor, Fortify, Worm/Eraser/See Ya/Speedy, Hellbolt,
  Vrizzbolt, Sword/Banhammer, Flak, DNA Lock, Hardened Circuitry, Surge,
  Mask, Overclock, Spoof Signal, DeckKRASH).

Entry shape:
    {
        "category":     "booster" | "defender" | "attacker" | "hardware" | "boosted_action",
        "is_hardware":  bool,         # True → checked against installed_hardware
        "order":        int,          # lower runs first; tied → registration order
        "hooks":        {hook_name: callable, ...},
    }

Hook signatures (return shape; malformed return → warning log + no-op):
    on_interface_check          (ability, base_total, prog, hack_state)
                                → (bonus_int, modifier_label_or_None)
    on_brain_damage_inbound     (amount, prog, hack_state, game_state)
                                → (new_amount_int, list_of_state_ops)
    on_program_attack_hit       (attack_result, prog, hack_state)
                                → list_of_state_ops
    on_alert_increase           (delta, source, hack_state)
                                → (new_delta_int, list_of_state_ops)
    on_turn_start / on_turn_end (hack_state, game_state)
                                → list_of_state_ops
    on_program_status_change    (program_name, old_status, new_status, hack_state, game_state)
                                → (rewritten_new_status_str, list_of_state_ops)
    on_program_damage_dice_select (target_ice_block, prog, hack_state)
                                → (damage_dice_int, modifier_label_or_None)
    on_ice_effect_inbound       (effect_name, ice_block, hack_state, game_state)
                                → (blocked_bool, replacement_ops_list, label_or_None)
    on_ice_attack_inbound       (attacker_ice_block, current_atk, hack_state, game_state)
                                → (modified_atk_int, label_or_None)
    on_jack_in (NOT WIRED YET)  (deck_owner, jacker_name, hack_state, game_state)
                                → (allowed_bool, required_dv_int_or_None, label_or_None)
"""
import copy
import logging
import random

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry — populated in later steps. Keys are canonical program / hardware
# names matching PROGRAM_STATS / HARDWARE_STATS spelling.
# ---------------------------------------------------------------------------
PROGRAM_EFFECTS: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Hook implementations (Step 2: 3 hardware effects)
# ---------------------------------------------------------------------------

def _insulated_wiring_on_ice_effect(effect, ice_block, hack_state, game_state):
    """Block body_fire effects (e.g. from Asp). RAW: Insulated Wiring
    cyberdeck hardware prevents the Netrunner's clothes from catching fire."""
    if effect == "body_fire":
        return True, [], "Insulated Wiring"
    return False, [], None


def _krash_barrier_on_ice_effect(effect, ice_block, hack_state, game_state):
    """Block forced_jack_out (e.g. from Giant). RAW: KRASH Barrier hardware
    is immune to forced disconnection — brain damage still applies, but no
    cascade and no disconnection."""
    if effect == "forced_jack_out":
        return True, [], "KRASH Barrier"
    return False, [], None


def _backup_drive_on_program_status_change(program_name, old_status, new_status,
                                            hack_state, game_state):
    """Intercept program destruction. RAW: Backup Drive hardware saves a
    program from being permanently Destroyed by reducing it to Deactivated
    instead. The save fires AT the moment of destruction (not at hack
    writeback) so the program is recoverable mid-hack via reinstall_program."""
    if new_status == "destroyed" and old_status != "destroyed":
        return "deactivated", []
    return new_status, []


# ---------------------------------------------------------------------------
# Hook implementations (Step 3: Defenders — Shield, Armor, Fortify)
# ---------------------------------------------------------------------------

def _shield_on_brain_damage_inbound(amount, prog, hack_state, game_state):
    """RAW (Hacking Rulebook §4): Shield reduces incoming brain damage from
    a single attack to 0. After preventing damage, the Shield is Derezzed.

    One-shot per encounter. Only fires when there's actually damage to
    block (amount > 0); otherwise no derez.
    """
    if amount <= 0:
        return amount, []
    derez_op = {
        "op": "program_derez",
        "program_name": "Shield",
        "source": "self-consumed Shield",
    }
    return 0, [derez_op]


def _armor_on_brain_damage_inbound(amount, prog, hack_state, game_state):
    """RAW: Armor reduces all incoming brain damage by 4. Persistent — does
    not derez on use."""
    return max(0, amount - 4), []


def _fortify_on_brain_damage_inbound(amount, prog, hack_state, game_state):
    """RAW: Fortify is activated as a Boosted Action (1 NA + 1 Cycle) and
    grants +4 brain damage reduction for the current turn (= a temporary
    Armor). The Boosted-Action handler (Step 6b) sets
    active_boosts.fortify_pending; on_turn_end clears it.

    Until activated, Fortify provides no defense (the program is loaded but
    inert). This matches RAW where the program must be actively boosted to
    stack with passive Armor.
    """
    boosts = (hack_state or {}).get("active_boosts") or {}
    if not boosts.get("fortify_pending"):
        return amount, []
    return max(0, amount - 4), []


def _fortify_on_turn_end(hack_state, game_state):
    """Clear the one-turn fortify_pending flag at the end of each turn."""
    boosts = (hack_state or {}).get("active_boosts") or {}
    if boosts.get("fortify_pending"):
        return [{"op": "active_boost_clear", "boost": "fortify_pending",
                 "reason": "Fortify duration expired"}]
    return []


# ---------------------------------------------------------------------------
# Hook implementations (Step 4: Boosters — Worm, Eraser, See Ya, Speedy)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Hook implementations (Step 5: Attacker kickers + auto-Deactivate)
# ---------------------------------------------------------------------------

def _is_black_ice_block(ice_block):
    """RAW: Black ICE = anti_personnel + anti_program classes."""
    if not isinstance(ice_block, dict):
        return False
    cls = str(ice_block.get("class", "")).strip().lower()
    return cls in ("anti_personnel", "anti_program")


def _hellbolt_on_attack_hit(attack_result, prog, hack_state):
    """Hellbolt kicker: emit body_fire op so the target Netrunner's deck
    catches fire (2 meat HP/turn until extinguished). PROGRAM_STATS effect:
    "2d6 brain + deck fire". Brain damage is handled by the standard
    program_attack damage roll; this hook only emits the deck-fire kicker.
    Auto-Deactivate is resolver-driven (fires hit-or-miss per RAW)."""
    target = attack_result.get("target_name") or attack_result.get("target", "")
    return [{"op": "body_fire", "active": True, "damage_per_turn": 2,
             "source": "Hellbolt", "target": target}]


def _vrizzbolt_on_attack_hit(attack_result, prog, hack_state):
    """Vrizzbolt kicker: emit net_action_penalty for the target's next
    turn. PROGRAM_STATS effect: "1d6 brain; -1 NET Action". Brain damage
    handled by the standard attack roll; this hook only emits the kicker.
    Auto-Deactivate is resolver-driven (fires hit-or-miss per RAW)."""
    target = attack_result.get("target_name") or attack_result.get("target", "")
    return [{"op": "net_action_penalty", "penalty": 1, "source": "Vrizzbolt",
             "target": target}]


def _sword_on_damage_dice_select(target_ice_block, prog, hack_state):
    """Sword: '3d6 REZ Black; 2d6 non-Black' (PROGRAM_STATS effect)."""
    if _is_black_ice_block(target_ice_block):
        return 3, "Sword vs Black"
    return 2, "Sword vs non-Black"


def _banhammer_on_damage_dice_select(target_ice_block, prog, hack_state):
    """Banhammer: '3d6 REZ non-Black; 2d6 Black' (PROGRAM_STATS effect) —
    inverse of Sword."""
    if _is_black_ice_block(target_ice_block):
        return 2, "Banhammer vs Black"
    return 3, "Banhammer vs non-Black"


# ---------------------------------------------------------------------------
# Hook implementations (Step 6a: passive bespoke — Flak, DNA Lock, Hardened Circuitry)
# ---------------------------------------------------------------------------

def _flak_on_ice_attack_inbound(attacker_ice_block, current_atk, prog, hack_state, game_state):
    """RAW (PROGRAM_STATS): 'Enemy non-Black-ICE ATK → 0'. Flak is a Defender
    program that nullifies attacks from non-Black ICE (Tar, Patrol, etc.) by
    reducing the attacker's ATK to 0. Black ICE attacks are unaffected."""
    if _is_black_ice_block(attacker_ice_block):
        return current_atk, None
    return 0, "Flak (non-Black ICE → 0 ATK)"


def _hardened_circuitry_on_ice_effect(effect, ice_block, hack_state, game_state):
    """RAW: Hardened Circuitry hardware makes the deck immune to EMP. No
    current ICE in ICE_STAT_BLOCKS uses an 'emp' effect, but the hook is
    here for any future EMP-emitting effect (e.g. homebrew or cyberware
    EMP weapons that target the deck)."""
    if effect == "emp":
        return True, [], "Hardened Circuitry"
    return False, [], None


def _dna_lock_on_jack_in(deck_owner, jacker_name, hack_state, game_state):
    """RAW (HARDWARE_STATS): 'DV17 Electronics/Security Tech to use deck'.
    DNA Lock gates the deck so only the registered owner can jack in.
    Anyone else must succeed at an Electronics/Security Tech vs DV17
    check to bypass the lock.

    NOTE: NOT YET WIRED. This hook signature is registered for future
    integration with a jack_in resolver action. Currently no caller
    invokes run_jack_in_hooks (no such driver exists yet) — this entry
    is metadata-only for discoverability."""
    if not deck_owner or not jacker_name:
        return True, None, None
    if str(deck_owner).strip().lower() == str(jacker_name).strip().lower():
        return True, None, None
    return False, 17, "DNA Lock — Electronics/Security Tech DV17 to bypass"


# ---------------------------------------------------------------------------
# Hook implementations (Step 6b: action-economy bespoke — Surge, Mask,
#                       Spoof Signal, DeckKRASH; Overclock deferred)
# ---------------------------------------------------------------------------

def _surge_on_interface_check(ability, base_total, prog, hack_state):
    """RAW: Surge is a Boosted Action (1 NA + 1 Cycle to activate). Once
    active, the next Interface check gets +4. Single-shot — clears the
    pending flag after firing."""
    boosts = (hack_state or {}).get("active_boosts") or {}
    if not boosts.get("surge_pending"):
        return 0, None
    # Clear in-place so subsequent checks don't double-fire. The proxy
    # passed by the resolver shares the active_boosts dict reference with
    # the live hack_state, so this mutation persists.
    if isinstance(hack_state, dict) and isinstance(hack_state.get("active_boosts"), dict):
        hack_state["active_boosts"].pop("surge_pending", None)
    return 4, "Surge"


def _mask_on_alert_increase(delta, source, hack_state):
    """RAW: Mask is a Boosted Action that suppresses the next Alert increase
    this turn (sets the delta to 0). Single-shot — clears after firing."""
    boosts = (hack_state or {}).get("active_boosts") or {}
    if not boosts.get("mask_pending"):
        return delta, []
    if isinstance(hack_state, dict) and isinstance(hack_state.get("active_boosts"), dict):
        hack_state["active_boosts"].pop("mask_pending", None)
    return 0, [{"op": "alert_suppressed", "by": "Mask",
                "original_delta": delta, "source": source}]


def _spoof_signal_on_turn_start(hack_state, game_state):
    """RAW: Spoof Signal redirects Patrol ICE detection target for 2 rounds
    after activation. Implementation is partial — the registry entry exists
    so a future Patrol-detection routing layer can read
    active_boosts.spoof_signal_rounds_remaining. The on_turn_start hook
    decrements that counter so the effect expires correctly.

    For Step 6b: the boosted_action handler sets
    active_boosts.spoof_signal_rounds_remaining = 2 when activated; this
    hook decrements it; on reaching 0 it's cleared."""
    boosts = (hack_state or {}).get("active_boosts") or {}
    rounds_left = boosts.get("spoof_signal_rounds_remaining")
    if not rounds_left:
        return []
    new_rounds = max(0, int(rounds_left) - 1)
    if isinstance(hack_state, dict) and isinstance(hack_state.get("active_boosts"), dict):
        if new_rounds > 0:
            hack_state["active_boosts"]["spoof_signal_rounds_remaining"] = new_rounds
        else:
            hack_state["active_boosts"].pop("spoof_signal_rounds_remaining", None)
    return [{"op": "spoof_signal_tick", "rounds_remaining": new_rounds}]


def _nervescrub_on_attack_hit(attack_result, prog, hack_state):
    """RAW (PROGRAM_STATS): 'INT/REF/DEX -1d6 1hr'. Nervescrub is an Attacker
    program that scrubs the target Netrunner's neural reflexes — apply a
    -1d6 penalty to INT, REF, and DEX for 1 hour. Re-uses the existing
    stat_debuff op (already consumed by _apply_single_ice_op for ICE-emitted
    debuffs from Skunk et al.)."""
    target = attack_result.get("target_name") or attack_result.get("target", "")
    amount = random.randint(1, 6)
    return [{
        "op": "stat_debuff",
        "stats": ["INT", "REF", "DEX"],
        "amount": amount,
        "source": "Nervescrub",
        "duration": "1 hour",
        "target": target,
    }]


def _poison_flatline_on_attack_hit(attack_result, prog, hack_state):
    """RAW (PROGRAM_STATS): 'Destroy random non-Black program'. Poison
    Flatline is an Attacker program that, on hit, picks a random non-Black
    program in the target's deck and destroys it.

    Implementation note: All player-loadable programs are 'non-Black' (the
    Black classification applies to ICE classes — anti_personnel and
    anti_program). The hook receives the firing Netrunner's hack_state via
    the prog/hack_state args; for the target's active_programs we emit a
    program_destroy op carrying a target_player field. The writeback layer
    (_apply_resolver_net_ops) routes program_destroy through
    apply_program_status_change so Backup Drive interception applies
    automatically."""
    # Use the firing Netrunner's hack_state.active_programs as the candidate
    # pool when no separate target-state lookup is available. (PvP-correct
    # impl would pass the target's hack_state; cross-Netrunner state is not
    # currently plumbed.) The ops the hook emits target an active program
    # name; the resolver+writeback layer applies them to whichever
    # hack_state is active when ops are processed.
    target = attack_result.get("target_name") or attack_result.get("target", "")
    candidates = []
    for p in _snapshot_active_programs((hack_state or {}).get("active_programs")):
        # Defensive: skip the firing program itself, skip Black-classified
        # entries (none currently exist among player programs but
        # future-proof).
        if p.get("name") == (prog or {}).get("name"):
            continue
        candidates.append(p.get("name"))
    if not candidates:
        return [{"op": "poison_flatline_no_target",
                 "target": target, "source": "Poison Flatline",
                 "reason": "No non-Black programs available to destroy."}]
    chosen = random.choice(candidates)
    return [{
        "op": "program_destroy",
        "program_name": chosen,
        "source": "Poison Flatline",
        "target_player": target,
    }]


def _superglue_on_attack_hit(attack_result, prog, hack_state):
    """RAW (PROGRAM_STATS): 'No move/jack out 1d6 rounds'. Superglue locks
    the target Netrunner from moving between nodes AND from voluntarily
    jacking out for 1d6 rounds. Forced disconnects (ally-yanked, flatline)
    bypass — physical severance can't be glued.

    Emits movement_lock (existing op) and jack_out_lock (new op consumed by
    the Step 6c jack-out gate predicate)."""
    target = attack_result.get("target_name") or attack_result.get("target", "")
    rounds = random.randint(1, 6)
    return [
        {"op": "movement_lock",
         "locked_by": "Superglue", "target": target,
         "duration_rounds": rounds},
        {"op": "jack_out_lock",
         "rounds_remaining": rounds, "source": "Superglue",
         "target": target,
         "reason": f"Superglue: no jack out for {rounds} rounds"},
    ]


def _overclock_on_turn_end(hack_state, game_state):
    """Overclock pending flag persists until consumed by the next turn's
    NA reset (apply_hack_state turn-boundary code reads
    active_boosts.overclock_pending and applies +1 NA). on_turn_end is a
    no-op for the flag itself; the reset path handles consumption.

    This hook exists so Overclock has a presence in the turn-end pipeline
    if future logic needs to inspect or expire it. For now: pure no-op."""
    return []


def _deckkrash_on_attack_hit(attack_result, prog, hack_state):
    """RAW (PROGRAM_STATS): 'Unsafe jack out'. DeckKRASH is an Attacker
    program that — on hit — forces the target Netrunner through an Unsafe
    Jack Out cascade (all rezzed Black ICE effects fire on the way out).
    Emits an initiate_unsafe_jack_out op the writeback layer consumes
    (cpred_hack._apply_initiate_unsafe_jack_out). Auto-Deactivate is
    resolver-driven; this hook only emits the kicker."""
    target = attack_result.get("target_name") or attack_result.get("target", "")
    return [{
        "op": "initiate_unsafe_jack_out",
        "cause": "deckkrash_attack",
        "actor": "DeckKRASH",
        "target": target,
        "reason": f"DeckKRASH hit {target} — forced Unsafe Jack Out cascade",
    }]


def _booster_bonus_on_ability(target_ability, bonus, label):
    """Curry an on_interface_check hook that fires +bonus when the rolling
    Interface Ability matches `target_ability` (case-insensitive).

    Returns a function suitable for the on_interface_check hook signature:
        (ability, base_total, prog, hack_state) → (bonus_int, label_or_None)
    """
    target_lc = target_ability.strip().lower()

    def _hook(ability, base_total, prog, hack_state):
        if not isinstance(ability, str):
            return 0, None
        if ability.strip().lower() == target_lc:
            return bonus, label
        return 0, None

    return _hook


# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------
PROGRAM_EFFECTS["Insulated Wiring"] = {
    "category": "hardware",
    "is_hardware": True,
    "order": 10,
    "hooks": {"on_ice_effect_inbound": _insulated_wiring_on_ice_effect},
}

PROGRAM_EFFECTS["KRASH Barrier"] = {
    "category": "hardware",
    "is_hardware": True,
    "order": 10,
    "hooks": {"on_ice_effect_inbound": _krash_barrier_on_ice_effect},
}

PROGRAM_EFFECTS["Backup Drive"] = {
    "category": "hardware",
    "is_hardware": True,
    "order": 10,
    "hooks": {"on_program_status_change": _backup_drive_on_program_status_change},
}

# Step 3: Defender programs. Order 10 → 20 → 30 ensures Shield (one-shot
# absorber) consumes itself before Armor's flat -4 applies; Armor before
# Fortify so a partially-derezzed defender stack still works correctly.
PROGRAM_EFFECTS["Shield"] = {
    "category": "defender",
    "is_hardware": False,
    "order": 10,
    "hooks": {"on_brain_damage_inbound": _shield_on_brain_damage_inbound},
}

PROGRAM_EFFECTS["Armor"] = {
    "category": "defender",
    "is_hardware": False,
    "order": 20,
    "hooks": {"on_brain_damage_inbound": _armor_on_brain_damage_inbound},
}

PROGRAM_EFFECTS["Fortify"] = {
    "category": "defender",
    "is_hardware": False,
    "order": 30,
    "hooks": {
        "on_brain_damage_inbound": _fortify_on_brain_damage_inbound,
        "on_turn_end": _fortify_on_turn_end,
    },
}

# Step 4: Booster programs. All order=50 (after Defenders' 10/20/30); they
# fire on Interface Ability checks matching their target ability.
#   Worm                +2 Backdoor    (PROGRAM_STATS effect: "+2 Backdoor")
#   Eraser              +2 Cloak       (PROGRAM_STATS effect: "+2 Cloak")
#   See Ya              +2 Pathfinder  (PROGRAM_STATS effect: "+2 Pathfinder")
#   Speedy Gonzalvez    +2 Initiative  (CPRED p.205 — NET initiative bonus,
#                                       PROGRAM_STATS effect: "+2 Speed")
PROGRAM_EFFECTS["Worm"] = {
    "category": "booster",
    "is_hardware": False,
    "order": 50,
    "hooks": {"on_interface_check":
              _booster_bonus_on_ability("Backdoor", 2, "Worm")},
}

PROGRAM_EFFECTS["Eraser"] = {
    "category": "booster",
    "is_hardware": False,
    "order": 50,
    "hooks": {"on_interface_check":
              _booster_bonus_on_ability("Cloak", 2, "Eraser")},
}

PROGRAM_EFFECTS["See Ya"] = {
    "category": "booster",
    "is_hardware": False,
    "order": 50,
    "hooks": {"on_interface_check":
              _booster_bonus_on_ability("Pathfinder", 2, "See Ya")},
}

PROGRAM_EFFECTS["Speedy Gonzalvez"] = {
    "category": "booster",
    "is_hardware": False,
    "order": 50,
    "hooks": {"on_interface_check":
              _booster_bonus_on_ability("Initiative", 2, "Speedy")},
}

# Step 5: Attacker programs.
#   Auto-Deactivate is resolver-driven (cpred_mechanics emits a
#   program_status_change op for every program_attack regardless of hit/miss
#   per RAW). Hooks here only emit per-attacker on-hit kicker effects.
#   on_program_damage_dice_select (Sword/Banhammer) runs before the damage
#   roll to pick 2d6 vs 3d6 based on target ICE class.
PROGRAM_EFFECTS["Sword"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {
        "on_program_damage_dice_select": _sword_on_damage_dice_select,
    },
}

PROGRAM_EFFECTS["Banhammer"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {
        "on_program_damage_dice_select": _banhammer_on_damage_dice_select,
    },
}

PROGRAM_EFFECTS["Hellbolt"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _hellbolt_on_attack_hit},
}

PROGRAM_EFFECTS["Vrizzbolt"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _vrizzbolt_on_attack_hit},
}

# Step 6a: passive bespoke effects.
#   Flak — Defender program that zeros non-Black-ICE attacker ATK.
#   Hardened Circuitry — hardware blocking 'emp' ICE effect (currently no
#       ICE emits this; entry is preemptive for homebrew/future content).
#   DNA Lock — hardware gating jack-in to the deck owner. NOT WIRED yet
#       (no jack-in resolver action exists); registered for future use.
PROGRAM_EFFECTS["Flak"] = {
    "category": "defender",
    "is_hardware": False,
    "order": 25,  # between Armor (20) and Fortify (30) in the defender stack
    "hooks": {"on_ice_attack_inbound": _flak_on_ice_attack_inbound},
}

PROGRAM_EFFECTS["Hardened Circuitry"] = {
    "category": "hardware",
    "is_hardware": True,
    "order": 10,
    "hooks": {"on_ice_effect_inbound": _hardened_circuitry_on_ice_effect},
}

PROGRAM_EFFECTS["DNA Lock"] = {
    "category": "hardware",
    "is_hardware": True,
    "order": 10,
    "hooks": {"on_jack_in": _dna_lock_on_jack_in},
}

# Step 6b: action-economy bespoke programs.
#   Surge / Mask / Spoof Signal — Boosted-Action programs activated via
#       the boosted_action resolver action type (1 NA + 1 Cycle each, atomic).
#       Hook fires once on the next matching event, clears the flag.
#   DeckKRASH — Attacker program that, on hit, forces target Netrunner
#       through an Unsafe Jack Out cascade. Wired via on_program_attack_hit.
#   Overclock — DEFERRED. RAW gives extra NET Action(s) next turn; would
#       need a deferred-action mechanism that mutates net_actions_per_turn
#       transiently. Not in this batch.
PROGRAM_EFFECTS["Surge"] = {
    "category": "boosted_action",
    "is_hardware": False,
    "order": 60,  # after passive Boosters (50) so explicit user activation
                  # appears after stable boosters in modifier label order
    "hooks": {"on_interface_check": _surge_on_interface_check},
}

PROGRAM_EFFECTS["Mask"] = {
    "category": "boosted_action",
    "is_hardware": False,
    "order": 60,
    "hooks": {"on_alert_increase": _mask_on_alert_increase},
}

PROGRAM_EFFECTS["Spoof Signal"] = {
    "category": "boosted_action",
    "is_hardware": False,
    "order": 60,
    "hooks": {"on_turn_start": _spoof_signal_on_turn_start},
}

PROGRAM_EFFECTS["DeckKRASH"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _deckkrash_on_attack_hit},
}

# Step 6c: remaining four Attacker / Boosted-Action programs.
#   Nervescrub — Attacker, INT/REF/DEX -1d6 1hr (re-uses stat_debuff op).
#   Poison Flatline — Attacker, destroy random non-Black program.
#   Superglue — Attacker, lock movement + jack-out for 1d6 rounds. Backed
#       by the Step 6c jack-out gate (cpred_hack._check_jack_out_allowed).
#   Overclock — Boosted-Action, +1 NA on next turn (consumed at the
#       turn-boundary NA reset in apply_hack_state).
PROGRAM_EFFECTS["Nervescrub"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _nervescrub_on_attack_hit},
}

PROGRAM_EFFECTS["Poison Flatline"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _poison_flatline_on_attack_hit},
}

PROGRAM_EFFECTS["Superglue"] = {
    "category": "attacker",
    "is_hardware": False,
    "order": 100,
    "hooks": {"on_program_attack_hit": _superglue_on_attack_hit},
}

PROGRAM_EFFECTS["Overclock"] = {
    "category": "boosted_action",
    "is_hardware": False,
    "order": 60,
    "hooks": {"on_turn_end": _overclock_on_turn_end},
}


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_active_programs(active_programs):
    """Return a list of shallow-copied dict entries with status == 'active'.

    Same-name duplicates in the deck are skipped with a warning (deck slots
    should forbid duplicates, but defend against malformed state).
    """
    if not isinstance(active_programs, list):
        return []
    seen = set()
    out = []
    for p in active_programs:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name", "")).strip()
        if not name:
            continue
        status = str(p.get("status", "")).strip().lower()
        if status != "active":
            continue
        if name.lower() in seen:
            logger.warning(
                "cpred_program_effects: duplicate active program %r — skipping second instance.",
                name,
            )
            continue
        seen.add(name.lower())
        out.append(dict(p))
    return out


def _snapshot_installed_hardware(installed_hardware):
    """Return a list of hardware name strings.

    Production stores installed_hardware as a list of strings (matching
    init_hack_state). This helper tolerates list-of-dicts (with name key)
    for forward compat.
    """
    if not isinstance(installed_hardware, list):
        return []
    out = []
    for h in installed_hardware:
        if isinstance(h, str):
            s = h.strip()
            if s:
                out.append(s)
        elif isinstance(h, dict):
            n = h.get("name")
            if isinstance(n, str) and n.strip():
                out.append(n.strip())
    return out


# ---------------------------------------------------------------------------
# Entry filter / ordering
# ---------------------------------------------------------------------------

def _matching_entries(snapshot_programs, snapshot_hardware, hook_name):
    """Return list of (canonical_name, entry, prog_data_or_None) tuples.

    An entry matches if it has the named hook AND (it's hardware whose name
    substring-matches an installed hardware entry, OR it's a program with a
    matching active program in the snapshot).

    Sorted by entry.order ascending; ties broken by registration order
    (PROGRAM_EFFECTS dict insertion order, guaranteed in Python 3.7+).
    """
    prog_by_name_lc = {p["name"].lower(): p for p in snapshot_programs}
    hw_lc = [h.lower() for h in snapshot_hardware]
    registration_order = list(PROGRAM_EFFECTS.keys())
    out = []
    for canonical, entry in PROGRAM_EFFECTS.items():
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks", {})
        if hook_name not in hooks or not callable(hooks[hook_name]):
            continue
        canonical_lc = canonical.lower()
        if entry.get("is_hardware"):
            if any(canonical_lc in h for h in hw_lc):
                out.append((canonical, entry, None))
        else:
            prog = prog_by_name_lc.get(canonical_lc)
            if prog is not None:
                out.append((canonical, entry, prog))
    out.sort(key=lambda t: (t[1].get("order", 100),
                            registration_order.index(t[0])))
    return out


def _safe_call(hook, args, hook_label):
    """Invoke hook(*args). Log + return None on exception."""
    try:
        return hook(*args)
    except Exception:
        logger.exception("cpred_program_effects: %s raised — treating as no-op.", hook_label)
        return None


def _coerce_int(value):
    """Return value as int, or None if not a finite numeric."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return None
    return None


# ---------------------------------------------------------------------------
# Pipeline drivers
# ---------------------------------------------------------------------------

def run_interface_check_hooks(ability, base_total, hack_state):
    """Run on_interface_check hooks (Boosters: Worm, Eraser, See Ya, Speedy).

    Args:
        ability: The Interface Ability rolling (e.g. "Backdoor"). Hooks key
                 their bonuses on this — Worm only fires on Backdoor.
        base_total: The d10 + Interface rank total before booster bonuses.
        hack_state: The hack_state dict (provides active_programs +
                    installed_hardware).

    Returns: (total_bonus_int, modifier_labels) where modifier_labels is a
    list of (label, bonus_int) tuples in execution order.
    """
    if not ability:
        return 0, []
    hs = hack_state if isinstance(hack_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_interface_check")
    total_bonus = 0
    labels = []
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_interface_check"]
        ret = _safe_call(
            hook,
            (ability, base_total + total_bonus, prog, hs),
            f"{canonical}.on_interface_check",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_interface_check returned %r; expected (bonus, label).",
                    canonical, ret,
                )
            continue
        bonus, label = ret
        bonus_int = _coerce_int(bonus)
        if bonus_int is None or bonus_int == 0:
            continue
        total_bonus += bonus_int
        labels.append((label or canonical, bonus_int))
    return total_bonus, labels


def run_brain_damage_hooks(amount, hack_state, game_state):
    """Run on_brain_damage_inbound hooks (Defenders: Shield, Armor, Fortify, Flak).

    Args:
        amount: Incoming brain damage before defender mitigation.
        hack_state: The hack_state dict.
        game_state: The game_state dict (some hooks need cross-state info).

    Damage hooks short-circuit: once amount drops to 0 the loop stops.
    Each hook receives the *current* (post-prior-hook) amount, not the
    original.

    Returns: (final_amount, state_ops, trace).
        trace = [{prog, hook, before, after, state_ops}, ...] for narration.
    """
    amt = _coerce_int(amount)
    if amt is None or amt <= 0:
        return 0, [], []
    hs = hack_state if isinstance(hack_state, dict) else {}
    gs = game_state if isinstance(game_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_brain_damage_inbound")
    state_ops = []
    trace = []
    current = amt
    for canonical, entry, prog in entries:
        if current <= 0:
            break
        hook = entry["hooks"]["on_brain_damage_inbound"]
        before = current
        ret = _safe_call(
            hook, (current, prog, hs, gs),
            f"{canonical}.on_brain_damage_inbound",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_brain_damage_inbound returned %r; expected (amount, ops).",
                    canonical, ret,
                )
            continue
        new_amount, ops = ret
        new_int = _coerce_int(new_amount)
        if new_int is None:
            continue
        ops_copy = copy.deepcopy(ops) if isinstance(ops, list) else []
        state_ops.extend(ops_copy)
        trace.append({
            "prog": canonical, "hook": "on_brain_damage_inbound",
            "before": before, "after": max(0, new_int), "state_ops": ops_copy,
        })
        current = max(0, new_int)
    return current, state_ops, trace


def run_program_attack_hit_hooks(attack_result, hack_state, firing_program_name=None):
    """Run on_program_attack_hit hooks (Attacker kickers + auto-Deactivate).

    Filters to the firing program only (per RAW: Hellbolt's fire-on-hit
    fires only when Hellbolt itself attacks, not when any active attacker
    fires). Caller passes firing_program_name from the action.

    Args:
        attack_result: The resolved program_attack result dict (hit, damage,
                       target, etc.).
        hack_state: The hack_state dict.
        firing_program_name: Name of the program that fired this attack.
                             If None, falls back to firing all active
                             attackers' hooks (legacy behavior).

    Returns: (state_ops, trace).
    """
    hs = hack_state if isinstance(hack_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_program_attack_hit")
    if firing_program_name:
        firing_lc = str(firing_program_name).strip().lower()
        entries = [(c, e, p) for c, e, p in entries
                   if c.lower() == firing_lc]
    state_ops = []
    trace = []
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_program_attack_hit"]
        ret = _safe_call(
            hook, (attack_result, prog, hs),
            f"{canonical}.on_program_attack_hit",
        )
        if ret is None:
            continue
        if not isinstance(ret, list):
            logger.warning(
                "cpred_program_effects: %s.on_program_attack_hit returned %r; expected list of state ops.",
                canonical, ret,
            )
            continue
        ops_copy = copy.deepcopy(ret)
        state_ops.extend(ops_copy)
        trace.append({"prog": canonical, "hook": "on_program_attack_hit",
                      "state_ops": ops_copy})
    return state_ops, trace


def run_alert_increase_hooks(delta, source, hack_state):
    """Run on_alert_increase hooks (Mask, future suppressors).

    Args:
        delta: Proposed alert level increase.
        source: String describing the trigger ('failed_backdoor',
                'derez_ice', etc.) — hooks may key on this.
        hack_state: The hack_state dict.

    Returns: (final_delta, state_ops, trace).
    """
    delta_int = _coerce_int(delta)
    if delta_int is None:
        return 0, [], []
    hs = hack_state if isinstance(hack_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_alert_increase")
    state_ops = []
    trace = []
    current = delta_int
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_alert_increase"]
        before = current
        ret = _safe_call(
            hook, (current, source, hs),
            f"{canonical}.on_alert_increase",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_alert_increase returned %r; expected (delta, ops).",
                    canonical, ret,
                )
            continue
        new_delta, ops = ret
        new_int = _coerce_int(new_delta)
        if new_int is None:
            continue
        ops_copy = copy.deepcopy(ops) if isinstance(ops, list) else []
        state_ops.extend(ops_copy)
        trace.append({"prog": canonical, "hook": "on_alert_increase",
                      "before": before, "after": new_int, "state_ops": ops_copy})
        current = new_int
    return current, state_ops, trace


def run_turn_start_hooks(hack_state, game_state):
    """Run on_turn_start hooks. Returns (state_ops, trace)."""
    return _run_turn_hooks("on_turn_start", hack_state, game_state)


def run_turn_end_hooks(hack_state, game_state):
    """Run on_turn_end hooks (Fortify expiry, Spoof Signal countdown).

    Returns (state_ops, trace).
    """
    return _run_turn_hooks("on_turn_end", hack_state, game_state)


def _run_turn_hooks(hook_name, hack_state, game_state):
    hs = hack_state if isinstance(hack_state, dict) else {}
    gs = game_state if isinstance(game_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, hook_name)
    state_ops = []
    trace = []
    for canonical, entry, prog in entries:
        hook = entry["hooks"][hook_name]
        ret = _safe_call(hook, (hs, gs), f"{canonical}.{hook_name}")
        if ret is None:
            continue
        if not isinstance(ret, list):
            logger.warning(
                "cpred_program_effects: %s.%s returned %r; expected list of state ops.",
                canonical, hook_name, ret,
            )
            continue
        ops_copy = copy.deepcopy(ret)
        state_ops.extend(ops_copy)
        trace.append({"prog": canonical, "hook": hook_name, "state_ops": ops_copy})
    return state_ops, trace


def run_program_status_change_hooks(program_name, old_status, new_status,
                                    hack_state, game_state):
    """Run on_program_status_change hooks (Backup Drive interception, future
    teammate-react effects).

    Hooks may rewrite new_status (e.g. Backup Drive: 'destroyed' →
    'deactivated' to save the program). The chain runs in registration
    order; each hook sees the rewritten status from prior hooks.

    Returns (final_new_status, state_ops, trace).
    """
    hs = hack_state if isinstance(hack_state, dict) else {}
    gs = game_state if isinstance(game_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_program_status_change")
    state_ops = []
    trace = []
    current_new = new_status
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_program_status_change"]
        before = current_new
        ret = _safe_call(
            hook,
            (program_name, old_status, current_new, hs, gs),
            f"{canonical}.on_program_status_change",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_program_status_change returned %r; expected (new_status, ops).",
                    canonical, ret,
                )
            continue
        rewritten, ops = ret
        ops_copy = copy.deepcopy(ops) if isinstance(ops, list) else []
        state_ops.extend(ops_copy)
        trace.append({
            "prog": canonical, "hook": "on_program_status_change",
            "before": before, "after": rewritten, "state_ops": ops_copy,
        })
        if isinstance(rewritten, str) and rewritten:
            current_new = rewritten
    return current_new, state_ops, trace


def run_ice_effect_inbound_hooks(effect, ice_block, hack_state, game_state):
    """Run on_ice_effect_inbound hooks (Insulated Wiring blocks body_fire,
    KRASH Barrier blocks forced_jack_out, future Hardened Circuitry blocks EMP).

    Args:
        effect: The ICE effect type being applied (e.g. "body_fire",
                "forced_jack_out", "movement_lock").
        ice_block: The ICE stat block emitting the effect.
        hack_state: The hack_state dict.
        game_state: The game_state dict.

    Returns: (blocked, replacement_ops, label, trace).
        blocked = True if any hook returned blocked=True; first blocker wins
                  the label.
        replacement_ops = aggregated state ops from all hooks (e.g. an
                  alternative annotation op a hook wants to emit instead).
        label = first non-None blocker label (used for narration).
        trace = [{prog, hook, blocked, label, state_ops}, ...].
    """
    hs = hack_state if isinstance(hack_state, dict) else {}
    gs = game_state if isinstance(game_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_ice_effect_inbound")
    blocked = False
    label = None
    state_ops = []
    trace = []
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_ice_effect_inbound"]
        ret = _safe_call(
            hook, (effect, ice_block, hs, gs),
            f"{canonical}.on_ice_effect_inbound",
        )
        if not isinstance(ret, tuple) or len(ret) != 3:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_ice_effect_inbound returned %r; expected (blocked, ops, label).",
                    canonical, ret,
                )
            continue
        b, ops, lbl = ret
        ops_copy = copy.deepcopy(ops) if isinstance(ops, list) else []
        state_ops.extend(ops_copy)
        if b and not blocked:
            blocked = True
            label = lbl or canonical
        trace.append({
            "prog": canonical, "hook": "on_ice_effect_inbound",
            "blocked": bool(b), "label": lbl, "state_ops": ops_copy,
        })
    return blocked, state_ops, label, trace


def run_ice_attack_inbound_hooks(attacker_ice_block, current_atk, hack_state, game_state):
    """Run on_ice_attack_inbound hooks (Flak: zero out non-Black ICE ATK).

    Hooks may modify the attacker's ATK before the opposed-roll resolution.
    Chain in registry order; each hook receives the current (post-prior-hook)
    ATK value.

    Args:
        attacker_ice_block: ICE stat block of the attacker (used by Flak to
                            classify Black vs non-Black).
        current_atk: Current ATK value (before any hook modification).
        hack_state: hack_state dict (active_programs, installed_hardware).
        game_state: game_state dict.

    Returns: (final_atk, modifier_labels, trace) where modifier_labels is a
    list of (label, before, after) tuples in execution order.
    """
    atk_int = _coerce_int(current_atk)
    if atk_int is None:
        return current_atk, [], []
    hs = hack_state if isinstance(hack_state, dict) else {}
    gs = game_state if isinstance(game_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_ice_attack_inbound")
    state_ops_unused = []  # reserved if a future hook needs to emit
    labels = []
    trace = []
    current = atk_int
    for canonical, entry, prog in entries:
        hook = entry["hooks"]["on_ice_attack_inbound"]
        before = current
        ret = _safe_call(
            hook, (attacker_ice_block, current, prog, hs, gs),
            f"{canonical}.on_ice_attack_inbound",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_ice_attack_inbound returned %r; expected (atk, label).",
                    canonical, ret,
                )
            continue
        new_atk, label = ret
        new_int = _coerce_int(new_atk)
        if new_int is None:
            continue
        if new_int == current:
            continue
        labels.append((label or canonical, before, new_int))
        trace.append({
            "prog": canonical, "hook": "on_ice_attack_inbound",
            "before": before, "after": new_int, "label": label,
        })
        current = new_int
    return current, labels, trace


def run_program_damage_dice_select_hooks(target_ice_block, prog, hack_state):
    """Run on_program_damage_dice_select hooks (Sword/Banhammer Black-vs-non-Black scaling).

    Args:
        target_ice_block: ICE stat block of the attack target.
        prog: The firing program's active_programs entry.
        hack_state: The hack_state dict.

    Returns (damage_dice_int_or_None, modifier_label_or_None). Caller uses
    the returned dice count if not None, else the resolver default.
    """
    hs = hack_state if isinstance(hack_state, dict) else {}
    progs = _snapshot_active_programs(hs.get("active_programs"))
    hw = _snapshot_installed_hardware(hs.get("installed_hardware"))
    entries = _matching_entries(progs, hw, "on_program_damage_dice_select")
    # Filter to the firing program only — this hook is per-attack, not chained
    # across all active programs.
    if prog and isinstance(prog, dict):
        firing_name_lc = str(prog.get("name", "")).strip().lower()
        entries = [(c, e, p) for c, e, p in entries
                   if c.lower() == firing_name_lc]
    for canonical, entry, _prog in entries:
        hook = entry["hooks"]["on_program_damage_dice_select"]
        ret = _safe_call(
            hook, (target_ice_block, prog, hs),
            f"{canonical}.on_program_damage_dice_select",
        )
        if not isinstance(ret, tuple) or len(ret) != 2:
            if ret is not None:
                logger.warning(
                    "cpred_program_effects: %s.on_program_damage_dice_select returned %r; expected (dice, label).",
                    canonical, ret,
                )
            continue
        dice, label = ret
        dice_int = _coerce_int(dice)
        if dice_int is None or dice_int <= 0:
            continue
        return dice_int, label
    return None, None
