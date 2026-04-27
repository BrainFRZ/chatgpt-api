"""CPRED Architecture generator (CRB p.210-211).

Backend generates the structural skeleton of a Netrunning Architecture from
SR + tier + (optional) narrative hint.  The planner overlays narrative names
and contents on top — but topology, DVs, ICE counts, and ICE species pool
are RAW-determined here.

Why backend-generated structure:
  - CRB has random tables for floors, node types, and ICE placement; the
    planner is bad at sticking to them.  Curating a Giant out of an SR1
    system, enforcing 3-node Quick Hacks, and assigning canonical DVs were
    all repeated apply-time defenses against planner non-compliance.
    Generating from tables collapses that defense surface.
  - When the planner provides only `{sr, tier}` (or no system_map at all),
    `generate_architecture` produces a complete, RAW-legal map.
  - When the planner provides a full map, the apply-time normalizers
    (_autofill_missing_node_dvs, _validate_ice_tier_eligibility,
    _ensure_minimum_architecture) clean it up rather than regenerating —
    the planner's narrative choices win, the engine just enforces RAW.
"""

import random
from typing import Optional

from .cpred_tables import (
    ARCHITECTURE_DIFFICULTY_DV,
    ARCHITECTURE_FLOOR_RANGE,
    ARCHITECTURE_ICE_COUNT,
    ARCHITECTURE_TRACE_CHANCE,
    ARCHITECTURE_BRANCH_CHANCE,
    CORE_NODE_TYPE_WEIGHTS,
    CONVERGENCE_ICE_BY_SR,
    ICE_STAT_BLOCKS,
    LOBBY_NODE_TABLE,
    SR_DIFFICULTY_RATING,
    ice_pool_for_sr,
)


def _weighted_pick(weights: dict, rng: random.Random) -> str:
    """Pick a key from a weight dict.  Falls back to first key on empty."""
    if not weights:
        return ""
    keys = list(weights.keys())
    vals = [max(0, int(weights[k])) for k in keys]
    total = sum(vals)
    if total <= 0:
        return keys[0]
    return rng.choices(keys, weights=vals, k=1)[0]


def _sr_to_difficulty(sr: int) -> str:
    return SR_DIFFICULTY_RATING.get(int(sr or 2), "standard")


def _gen_quick_hack(sr: int, rng: random.Random) -> dict:
    """Quick Hack: 3 linear nodes — Gateway → Obstacle → Objective.

    Per project rulebook §3.  Quick Hacks have NO Lobby table draw — they're
    too short for it.  Single canonical DV from the tier.
    """
    difficulty = _sr_to_difficulty(sr)
    dv = ARCHITECTURE_DIFFICULTY_DV.get(difficulty, 8)
    nodes = {
        "Gateway":   {"type": "gateway",  "dv": dv, "connections": ["Obstacle"],  "contents": ""},
        "Obstacle":  {"type": "password", "dv": dv, "connections": ["Gateway", "Objective"], "contents": ""},
        "Objective": {"type": "target",   "dv": dv, "connections": ["Obstacle"],  "contents": ""},
    }
    return {
        "sr": sr,
        "tier": "quick_hack",
        "difficulty": difficulty,
        "topology": "linear",
        "nodes": nodes,
    }


def _gen_full_run(sr: int, hint: Optional[str], rng: random.Random) -> dict:
    """Full Run: Lobby (2 nodes) + Core + Objective per CRB p.211.

    Floor count, ICE count, branch chance, and Trace presence are all rolled
    from per-difficulty tables.
    """
    difficulty = _sr_to_difficulty(sr)
    base_dv = ARCHITECTURE_DIFFICULTY_DV.get(difficulty, 8)
    floor_lo, floor_hi = ARCHITECTURE_FLOOR_RANGE.get(difficulty, (5, 6))
    total_floors = rng.randint(floor_lo, floor_hi)
    nodes: dict = {}

    # ----- Lobby (first 2 floors) -----
    # CRB p.211: lobby may use lighter content.  Roll from LOBBY_NODE_TABLE.
    lobby_picks = [rng.choice(LOBBY_NODE_TABLE) for _ in range(2)]
    nodes["Gateway"] = {
        "type": "gateway",
        "dv": base_dv,
        "connections": ["Lobby 1"],
        "contents": "",
    }
    nodes["Lobby 1"] = {
        "type": lobby_picks[0]["type"],
        "dv": lobby_picks[0]["dv"],
        "connections": ["Gateway", "Lobby 2"],
        "contents": "",
    }
    nodes["Lobby 2"] = {
        "type": lobby_picks[1]["type"],
        "dv": lobby_picks[1]["dv"],
        "connections": ["Lobby 1"],
        "contents": "",
    }

    # ----- Core (remaining floors before Objective) -----
    core_count = max(0, total_floors - 3)  # minus Gateway + 2 Lobby + 1 Obj
    core_weights = CORE_NODE_TYPE_WEIGHTS.get(
        difficulty, CORE_NODE_TYPE_WEIGHTS["standard"]
    )
    core_names = []
    prev = "Lobby 2"
    for i in range(core_count):
        name = f"Floor {i + 1}"
        node_type = _weighted_pick(core_weights, rng)
        nodes[name] = {
            "type": node_type,
            "dv": base_dv,
            "connections": [prev],
            "contents": "",
        }
        nodes[prev].setdefault("connections", []).append(name)
        prev = name
        core_names.append(name)

    # ----- Objective (deepest node) -----
    nodes["Objective"] = {
        "type": "target",
        "dv": base_dv,
        "connections": [prev],
        "contents": "",
    }
    nodes[prev].setdefault("connections", []).append("Objective")

    # ----- Optional branch (side path off a core node) -----
    topology = "linear"
    if core_names and rng.random() < ARCHITECTURE_BRANCH_CHANCE.get(difficulty, 0.0):
        topology = "branched"
        branch_anchor = rng.choice(core_names)
        branch_name = "Side File"
        nodes[branch_name] = {
            "type": "file",
            "dv": base_dv,
            "connections": [branch_anchor],
            "contents": "",
        }
        nodes[branch_anchor].setdefault("connections", []).append(branch_name)

    # ----- ICE placement -----
    ice_status: dict = {}
    eligible_ice = ice_pool_for_sr(sr)

    # 1) Convergence Black ICE — RAW-fixed by SR; placed at the Objective.
    conv_key = (CONVERGENCE_ICE_BY_SR.get(sr) or "").lower()
    if conv_key and conv_key in ICE_STAT_BLOCKS:
        block = ICE_STAT_BLOCKS[conv_key]
        ice_status[f"Objective::{block['name']}"] = {
            **block,
            "species": conv_key,
            "behavior": "convergence",
            "node": "Objective",
            "status": "active",
        }

    # 2) Standard ICE distributed across core + lobby (NOT Gateway).
    ice_lo, ice_hi = ARCHITECTURE_ICE_COUNT.get(difficulty, (2, 3))
    standard_count = rng.randint(ice_lo, ice_hi)
    placement_pool = ["Lobby 1", "Lobby 2"] + core_names
    if not placement_pool:
        placement_pool = ["Lobby 2"]
    for _ in range(standard_count):
        if not eligible_ice:
            break
        species = rng.choice(eligible_ice)
        block = ICE_STAT_BLOCKS.get(species, {})
        if not block:
            continue
        node_name = rng.choice(placement_pool)
        # Allow stacking — CRB permits multiple ICE per floor.
        key = f"{node_name}::{block.get('name', species.title())}"
        suffix = 2
        while key in ice_status:
            key = f"{node_name}::{block.get('name', species.title())} #{suffix}"
            suffix += 1
        ice_status[key] = {
            **block,
            "species": species,
            "behavior": "patrol",  # default; planner can promote on its turn
            "node": node_name,
            "status": "active",
        }

    # 3) Trace ICE — independent roll; placed mid-core when present.
    trace_chance = ARCHITECTURE_TRACE_CHANCE.get(difficulty, 0.0)
    if rng.random() < trace_chance and core_names:
        # Mid-core placement so the runner has time to detect it.
        trace_node = core_names[len(core_names) // 2]
        ice_status[f"{trace_node}::Trace"] = {
            "name": "Trace",
            "species": "trace",
            "behavior": "trace",
            "class": "trace",
            "per": 6, "spd": 4, "atk": 0, "def": 4, "rez": 15,
            "damage_dice": 0,
            "effect": "trace_progress",
            "effect_desc": "Each turn rezzed, Trace progresses; on completion, corp learns runner location.",
            "node": trace_node,
            "status": "active",
        }

    return {
        "sr": sr,
        "tier": "full_run",
        "difficulty": difficulty,
        "topology": topology,
        "nodes": nodes,
        "ice_status": ice_status,
    }


def generate_architecture(
    sr: int = 2,
    tier: str = "full_run",
    hint: Optional[str] = None,
    target_system: Optional[str] = None,
    seed: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> dict:
    """Generate a complete RAW-legal Architecture for the given SR + tier.

    Returns a dict with two top-level keys ready to drop into hack_state:
      - `system_map`: {sr, tier, difficulty, topology, nodes}
      - `ice_status`: {key: ICE entry}  (empty dict for Quick Hacks)

    Args:
      sr:    Security Rating 1-5.  Drives DVs, ICE pool, floor count.
      tier:  "quick_hack" | "full_run".  Quick Hack is fixed 3-node linear.
      hint:  Optional narrative shape hint ("linear" / "branched") to
             override the random branch roll.  Currently advisory; not yet
             enforced for Quick Hack.
      target_system: Optional name (e.g. "Arasaka HR Server") — stored on
             the system_map for the planner to use in narration.
      seed:  Optional RNG seed for deterministic generation (tests).
      rng:   Optional pre-built Random instance (overrides seed).
    """
    if rng is None:
        rng = random.Random(seed) if seed is not None else random.Random()
    sr = int(sr or 2)
    if sr < 1:
        sr = 1
    elif sr > 5:
        sr = 5
    tier = str(tier or "full_run").strip().lower()

    if tier == "quick_hack":
        sm = _gen_quick_hack(sr, rng)
        ice = {}
    else:
        result = _gen_full_run(sr, hint, rng)
        ice = result.pop("ice_status", {})
        sm = result

    if target_system:
        sm["target_system"] = target_system
    if hint:
        sm["structure_hint"] = hint

    return {"system_map": sm, "ice_status": ice}
