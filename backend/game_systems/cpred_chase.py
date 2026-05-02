"""CPRED Hot Pursuit chase mode (vehicle chase + ranged combat).

Mode wiring contract: this module exports HOT_PURSUIT_CONTRACT (system
prompt), REPORT_CHASE_STATE_TOOL (tool schema), apply_chase_state (state
applier), build_chase_profile (combatant + driver roster), and
build_chase_injection (per-turn injection). Mode dispatch in main.py keys
on `pipeline_state.chase.active`.



Implements the Hot Pursuit ruleset (Cyberpunk RED supplement, May 2024) for
vehicle chase scenes. The module is self-contained: it owns the chase grid,
Positioning Checks, the five Hot Pursuit maneuvers (NOS / PIT / Pull Ahead /
Pull in Close / Ramming), SDP cascade, and chase-end conditions.

State lives at `pipeline_state.chase`. Schema:

    chase = {
        "active": bool,
        "round": int,                 # 1-indexed Chase Round counter
        "grid_length": int,           # default 8 squares
        "vehicles": {                  # name -> vehicle dict
            "<name>": {
                "name": str,
                "operator": str,       # PC/NPC name driving
                "occupants": [str],
                "square": int,         # 0-indexed position on chase grid
                "facing": str,         # "forward" (default) — reserved for future reverse-to-combat detection
                "combat_speed_move": int,   # MOVE used to pick Positioning DV
                "sdp_current": int,
                "sdp_max": int,
                "sp": int,
                "type": str,           # land | air | sea (Hot Pursuit p.5)
                "upgrades": [str],
                "status": str,         # active | disabled | crashed
                "last_positioning": dict | None,
                "boarded_by": [str],   # personas riding outside / on roof
                "is_pursuer": bool,    # narrative role (chasing vs. fleeing)
                "notes": str,
                # Round-scoped flags (cleared by end_of_round_cleanup):
                "_pit_speed_drops_this_round": int,
                "_pull_in_close_until_end_of_round": bool,
                "_nos_lost_control": bool,
                "_pull_ahead_failed": bool,
            }
        },
        "operator_initiative": {        # rolled at chase start
            "<operator>": int,
        },
        "current_turn": str | None,    # operator currently acting
        "round_resolution": [           # this-round Positioning Check log
            {"vehicle", "operator", "intent", "moved", "dv", "roll_total",
             "success", "crashed", "narration"},
        ],
        "context": str | None,         # entry handoff context (2-3 paragraphs)
        "narrative_summary": str | None,    # exit handoff summary (up to 3 paragraphs)
        "ended": bool,
        "end_reason": str | None,
            # one of: max_separation | vehicle_disabled | vehicle_crashed |
            #         all_pursuers_disabled | quarry_disabled |
            #         voluntary_stop | reversed_to_combat
        "exit_target_mode": str | None,    # "combat" | "general"
        "started_from": str | None,         # general | combat | net_combat | plot_encounter | state_report
        "start_message_id": str | None,
        "route": str | None,                # surfaced in HUD as "Vehicle Chase: <route>"
        "scene": str | None,                # Plot Encounter scene label, if applicable
        "_pre_chase_location": str | None,  # HUD location before chase started — restored on mode end
        "_prev_combat_round": int,
    }

The 8-square grid uses Hot Pursuit's range-band table (page 2 of the
supplement):

    1 sq -> 0-6 m         5 sq -> 51-100 m
    2 sq -> 7-12 m        6 sq -> 101-200 m
    3 sq -> 13-25 m       7 sq -> 201-400 m
    4 sq -> 26-50 m       8 sq -> 401-800 m

Positioning Check DVs by Combat Speed (Hot Pursuit p.3):

    60 MOVE -> DV 13      15 MOVE -> DV 21
    40 MOVE -> DV 15      10 MOVE -> DV 24
    20 MOVE -> DV 17       8 MOVE -> DV 29

A vehicle at <= 1/2 SDP rolls Positioning at the next-lower Combat Speed
category. If that drop puts effective Combat Speed below 8 MOVE, the
vehicle spins out and crashes (CP:R p.192).
"""

import logging
from typing import Optional

from .cpred_core import _safe_int, _render_transition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hot Pursuit reference tables (canonical from supplement).
# ---------------------------------------------------------------------------

# Range bands keyed by grid distance (number of squares between vehicles).
# Source: Hot Pursuit p.2, "# of Squares -> Range Band" table.
SQUARE_RANGE_BANDS = {
    1: (0, 6),
    2: (7, 12),
    3: (13, 25),
    4: (26, 50),
    5: (51, 100),
    6: (101, 200),
    7: (201, 400),
    8: (401, 800),
}

# Positioning Check DVs by Combat Speed (in MOVE).
# Source: Hot Pursuit p.3, "Positioning Check DVs" table.
# Order matters: descending Combat Speed -> ascending DV.
POSITIONING_DV_BY_SPEED = [
    (60, 13),
    (40, 15),
    (20, 17),
    (15, 21),
    (10, 24),
    (8, 29),
]

# Maneuvers (Hot Pursuit p.5).
# Each entry: {"dv": int, "description": str, "requires_adjacent": bool, "uses": "action"|"move"}
HOT_PURSUIT_MANEUVERS = {
    "nos": {
        "dv": 13,
        "description": (
            "If a vehicle has NOS, the operator can activate it on their Turn "
            "as a standard Action. Doing so automatically moves the vehicle "
            "1 square forward on the Chase Grid. The Check is to determine "
            "if the operator can maintain control during the harsh acceleration."
        ),
        "requires_adjacent": False,
        "uses": "action",
        "advance_squares": 1,
    },
    "pit": {
        "dv": 15,
        "description": (
            "PIT Maneuver — operator's vehicle must be adjacent to the target. "
            "Operator uses their vehicle to tap the target. No damage but the "
            "target vehicle wobbles — at end of Round, target makes their "
            "Position Check as if one Combat Speed category lower. If that "
            "would reduce effective Combat Speed below 8 MOVE, target spins "
            "out and crashes (CP:R p.192)."
        ),
        "requires_adjacent": True,
        "uses": "action",
        "imposes_speed_category_drop_on_target": True,
    },
    "pull_ahead": {
        "dv": 17,
        "description": (
            "Pull Ahead — operator's vehicle must be adjacent to target. Risks "
            "skill-and-speed pull past target. Success: vehicle moves 1 square "
            "ahead of target on Chase Grid. Failure: spin out, possible "
            "collision with oncoming obstacle (CP:R p.192)."
        ),
        "requires_adjacent": True,
        "uses": "action",
        "advance_past_target": 1,
    },
    "pull_in_close": {
        "dv": 13,
        "description": (
            "Pull in Close — operator boosts speed, pushing vehicle to lower "
            "edge of current range band (e.g. 7m/yds instead of 12m/yds) until "
            "end of Round. Makes it easier for others to leap from one vehicle "
            "to another, and makes melee attacks against another vehicle (or "
            "occupants) possible."
        ),
        "requires_adjacent": False,
        "uses": "action",
        "drops_to_lower_band_edge": True,
    },
    "ramming": {
        "dv": 17,
        "description": (
            "Ramming in a chase requires a focused attempt to crash into the "
            "target while still maintaining control. Otherwise follows CP:R "
            "p.192. NOT automatic by being in the same square."
        ),
        "requires_adjacent": True,
        "uses": "action",
        "applies_collision_damage": True,
    },
}

# Maximum chase grid separation before the chase auto-ends (Hot Pursuit p.5).
MAX_CHASE_SEPARATION_SQUARES = 8

# Default chase grid length (squares spanned by the visible chase area).
DEFAULT_GRID_LENGTH = 8


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def squares_to_range_band(distance_squares: int, *, pull_in_close: bool = False) -> Optional[tuple]:
    """Return the (min_m, max_m) range band tuple for a grid distance, or None
    if the distance is out of range (>8 squares = chase ends).

    If `pull_in_close` is True, returns a collapsed band at the LOWER edge
    of the canonical range (e.g. (7, 7) instead of (7, 12)) — reflecting
    the Pull in Close maneuver's effect of pushing to the lower band edge
    until end of round."""
    if distance_squares < 1:
        return (0, 0)
    band = SQUARE_RANGE_BANDS.get(int(distance_squares))
    if band is None:
        return None
    if pull_in_close:
        return (band[0], band[0])
    return band


def positioning_dv_for_speed(combat_speed_move: int, *, dv_modifier: int = 0) -> int:
    """Return the Positioning Check DV for a given Combat Speed (MOVE).
    `dv_modifier` adds to the canonical DV (e.g. +2 from prior NOS lost-control)."""
    speed = _safe_int(combat_speed_move, default=20)
    # Descending walk: pick the entry whose speed >= our speed.
    base_dv = POSITIONING_DV_BY_SPEED[-1][1]  # worst DV by default
    for threshold, dv in POSITIONING_DV_BY_SPEED:
        if speed >= threshold:
            base_dv = dv
            break
    return base_dv + _safe_int(dv_modifier, default=0)


def lower_speed_category(combat_speed_move: int) -> Optional[int]:
    """Drop Combat Speed by one category. Returns None if dropping would put
    effective speed below 8 MOVE (caller should treat as spin-out)."""
    speed = _safe_int(combat_speed_move, default=20)
    speeds = [s for s, _dv in POSITIONING_DV_BY_SPEED]
    for i, s in enumerate(speeds):
        if speed >= s:
            # Find the next-lower category.
            if i + 1 < len(speeds):
                next_speed = speeds[i + 1]
                if next_speed < 8:
                    return None
                return next_speed
            return None
    return None


def effective_combat_speed(vehicle: dict) -> int:
    """Return the Combat Speed (MOVE) used for this vehicle's Positioning
    Check, accounting for SDP halving (Hot Pursuit p.3) and any active PIT
    speed-category drops applied earlier in the round."""
    if not isinstance(vehicle, dict):
        return 20
    base = _safe_int(vehicle.get("combat_speed_move"), default=20)
    sdp_cur = _safe_int(vehicle.get("sdp_current"), default=base)
    sdp_max = _safe_int(vehicle.get("sdp_max"), default=base)
    speed = base
    # SDP-halving cascade.
    if sdp_max > 0 and sdp_cur * 2 < sdp_max:
        dropped = lower_speed_category(speed)
        if dropped is None:
            return -1  # signal spin-out
        speed = dropped
    # PIT stacks on top.
    pit_drops = _safe_int(vehicle.get("_pit_speed_drops_this_round"), default=0)
    for _ in range(pit_drops):
        dropped = lower_speed_category(speed)
        if dropped is None:
            return -1
        speed = dropped
    return speed


def grid_distance(vehicle_a: dict, vehicle_b: dict) -> int:
    """Return absolute square distance between two vehicles on the grid."""
    a = _safe_int((vehicle_a or {}).get("square"), default=0)
    b = _safe_int((vehicle_b or {}).get("square"), default=0)
    return abs(a - b)


def vehicles_adjacent(vehicle_a: dict, vehicle_b: dict) -> bool:
    """Adjacent = same square or 1 square apart on the chase grid."""
    return grid_distance(vehicle_a, vehicle_b) <= 1


# ---------------------------------------------------------------------------
# State init / transition.
# ---------------------------------------------------------------------------

def init_chase_state(
    grid_length: int = DEFAULT_GRID_LENGTH,
    vehicles: Optional[dict] = None,
    started_from: str = "general",
    context: Optional[str] = None,
    start_message_id: Optional[str] = None,
    **_kw,
) -> dict:
    """Return a fresh chase state.

    `vehicles` is a dict keyed by vehicle name; each value must include at
    minimum: name, operator, square (0-indexed), combat_speed_move, sdp_max.
    Missing fields are defaulted.
    """
    veh_out = {}
    if isinstance(vehicles, dict):
        for vname, v in vehicles.items():
            if not isinstance(v, dict):
                continue
            sdp_max = _safe_int(v.get("sdp_max"), default=20)
            veh_out[vname] = {
                "name": str(v.get("name") or vname),
                "operator": str(v.get("operator") or ""),
                "occupants": list(v.get("occupants") or []),
                "square": _safe_int(v.get("square"), default=0),
                "facing": str(v.get("facing") or "forward"),
                "combat_speed_move": _safe_int(v.get("combat_speed_move"), default=20),
                "sdp_current": _safe_int(v.get("sdp_current"), default=sdp_max),
                "sdp_max": sdp_max,
                "sp": _safe_int(v.get("sp"), default=0),
                "type": str(v.get("type") or "land"),
                "upgrades": list(v.get("upgrades") or []),
                "status": str(v.get("status") or "active"),
                "last_positioning": v.get("last_positioning"),
                "boarded_by": list(v.get("boarded_by") or []),
                "is_pursuer": bool(v.get("is_pursuer", False)),
                "notes": str(v.get("notes") or ""),
                # Round-scoped flags — all four initialized so consumers don't
                # have to .get() with default; cleared by end_of_round_cleanup.
                "_pit_speed_drops_this_round": 0,
                "_pull_in_close_until_end_of_round": False,
                "_nos_lost_control": False,
                "_pull_ahead_failed": False,
            }
    return {
        "active": True,
        "round": 1,
        "grid_length": int(grid_length or DEFAULT_GRID_LENGTH),
        "vehicles": veh_out,
        "operator_initiative": {},
        "current_turn": None,
        "round_resolution": [],
        "context": context,
        "narrative_summary": None,
        "ended": False,
        "end_reason": None,
        "exit_target_mode": None,
        "started_from": started_from or "general",
        "start_message_id": start_message_id,
        # Schema completeness — set explicitly so the dict matches the
        # documented schema. Downstream factories may overwrite with real
        # values; consumers can rely on these keys existing.
        "route": None,
        "scene": None,
        "_pre_chase_location": None,
        "_prev_combat_round": 1,
    }


def synthesize_chase_handoff_from_encounter(materialized: dict) -> str:
    """Compose a 2-3 paragraph story-shaped handoff_summary from a materialized
    Plot Encounters vehicle entry. Used when a chase fires from /chase or a
    plot trigger that doesn't supply its own context — the YAML carries
    enough scene/route/legs/victory/failure data to assemble a usable
    handoff without an LLM call.
    """
    if not isinstance(materialized, dict):
        return ""
    parts = []
    scene = (materialized.get("scene") or "").strip()
    route = (materialized.get("route") or "").strip()
    vehicles = materialized.get("vehicles") or {}

    # Paragraph 1: what's going on right now (scene + immediate vehicle picture).
    p1_sentences = []
    if scene:
        p1_sentences.append(scene + ".")
    if route:
        p1_sentences.append(f"Route: {route}.")
    pursuer_names = [n for n, v in vehicles.items()
                     if isinstance(v, dict) and v.get("is_pursuer")]
    quarry_names = [n for n, v in vehicles.items()
                    if isinstance(v, dict) and not v.get("is_pursuer")]
    if pursuer_names and quarry_names:
        p1_sentences.append(
            f"{', '.join(quarry_names)} is being pursued by "
            f"{', '.join(pursuer_names)}."
        )
    elif vehicles:
        p1_sentences.append(f"Vehicles in motion: {', '.join(vehicles.keys())}.")
    if p1_sentences:
        parts.append(" ".join(p1_sentences))

    # Paragraph 2: why we're here (legs + escalation triggers).
    legs = materialized.get("legs") or []
    escalation = materialized.get("escalation_triggers") or []
    p2_parts = []
    if legs:
        leg_summaries = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            name = (leg.get("name") or "").strip()
            desc = (leg.get("description") or "").strip()
            if name and desc:
                leg_summaries.append(f"{name}: {desc}")
            elif name:
                leg_summaries.append(name)
        if leg_summaries:
            p2_parts.append("Chase phases: " + " | ".join(leg_summaries[:4]) + ".")
    if escalation:
        first_three = [str(e).strip() for e in escalation[:3] if e]
        if first_three:
            p2_parts.append("Triggers to watch: " + "; ".join(first_three) + ".")
    if p2_parts:
        parts.append(" ".join(p2_parts))

    # Paragraph 3: what the goal is (victory / failure conditions).
    p3_parts = []
    victory = (materialized.get("victory_condition") or "").strip()
    failure = (materialized.get("failure_condition") or "").strip()
    if victory:
        p3_parts.append(f"Success: {victory}.")
    if failure:
        p3_parts.append(f"Failure: {failure}.")
    if p3_parts:
        parts.append(" ".join(p3_parts))

    return "\n\n".join(p for p in parts if p)


def init_chase_from_plot_encounter(materialized: dict, *, context: Optional[str] = None,
                                    start_message_id: Optional[str] = None) -> dict:
    """Build a chase state from a Plot Encounters `kind: vehicle / mode: hot_pursuit`
    materialized dict (output of materialize_vehicle_encounter).

    The materialized dict carries a fully-formed `vehicles` mapping with
    pre-populated SDP/MOVE/operator/starting_square fields. This function
    hands it to init_chase_state with sensible defaults from the encounter's
    chase_grid, and synthesizes a 2-3 paragraph context from scene/route/
    legs/victory/failure data when none is supplied by the caller.
    """
    if not isinstance(materialized, dict):
        return init_chase_state()
    vehicles = materialized.get("vehicles") or {}
    grid = materialized.get("chase_grid") or {}
    if not context:
        context = synthesize_chase_handoff_from_encounter(materialized)
    state = init_chase_state(
        grid_length=int(grid.get("length_squares", DEFAULT_GRID_LENGTH) or DEFAULT_GRID_LENGTH),
        vehicles=vehicles,
        started_from="plot_encounter",
        context=context or materialized.get("scene") or materialized.get("route"),
        start_message_id=start_message_id,
    )
    # Stash route on the state so the HUD location injection (chase_location_label)
    # can surface it during the chase.
    if materialized.get("route"):
        state["route"] = materialized["route"]
    if materialized.get("scene"):
        state["scene"] = materialized["scene"]
    return state


def stamp_chase_hud_overlay(pipeline_state: dict) -> None:
    """Apply the chase route as the HUD location and stash the pre-chase
    location for later restoration. Idempotent — safe to call multiple times
    across the chase lifetime; only stashes _pre_chase_location on the first
    call. Called from every chase-seeding path (combat→chase apply,
    net_combat→chase apply, chase_trigger handler, /chase command, plot
    encounter trigger) so the HUD reflects the chase immediately rather than
    lagging until the first apply_chase_state call.
    """
    if not isinstance(pipeline_state, dict):
        return
    chase = pipeline_state.get("chase")
    if not isinstance(chase, dict) or not chase.get("active"):
        return
    hud = pipeline_state.get("hud_state")
    if not isinstance(hud, dict):
        return
    # Stash the pre-chase location once. After init_chase_state runs,
    # _pre_chase_location is None; on first overlay call we capture the
    # current HUD location. Subsequent calls leave it untouched.
    if chase.get("_pre_chase_location") is None:
        chase["_pre_chase_location"] = hud.get("location", "")
    label = chase_location_label(chase)
    if label:
        hud["location"] = label


def chase_location_label(chase_state: dict) -> Optional[str]:
    """Return a HUD-friendly location label for an active chase, or None.
    Used by the per-turn injection / HUD updater to show 'Vehicle Chase:
    <route>' instead of the static pre-chase location."""
    if not isinstance(chase_state, dict):
        return None
    if not chase_state.get("active"):
        return None
    route = (chase_state.get("route") or "").strip()
    if route:
        return f"Vehicle Chase: {route}"
    scene = (chase_state.get("scene") or "").strip()
    if scene:
        return f"Vehicle Chase ({scene})"
    return "Vehicle Chase (in progress)"


def init_chase_from_combat(combat_state: dict, chase_info: Optional[dict] = None) -> dict:
    """Create a chase from an in-progress combat — preserves vehicle state
    already tracked under combat['vehicles'] (SDP/SP/MOVE/driver/occupants).

    `chase_info` accepts:
      - starting_squares: dict[name -> int] (default 0 per missing vehicle)
      - pursuer_vehicles: list[name] explicitly marking pursuers (default empty)
      - grid_length, context, start_message_id (passthrough to init_chase_state)
    """
    chase_info = chase_info if isinstance(chase_info, dict) else {}
    vehicles_in = (combat_state or {}).get("vehicles") or {}
    vehicles_out = {}
    starting_squares = chase_info.get("starting_squares") or {}
    pursuer_set = set(chase_info.get("pursuer_vehicles") or [])
    for vname, v in vehicles_in.items():
        if not isinstance(v, dict):
            continue
        sdp_max = _safe_int(v.get("sdp_max"), default=20)
        vehicles_out[vname] = {
            "name": vname,
            "operator": v.get("driver") or "",
            "occupants": list(v.get("occupants") or []),
            "square": _safe_int(starting_squares.get(vname), default=0),
            "facing": "forward",
            "combat_speed_move": _safe_int(v.get("combat_move"), default=20),
            "sdp_current": _safe_int(v.get("sdp_current"), default=sdp_max),
            "sdp_max": sdp_max,
            "sp": _safe_int(v.get("sp"), default=0),
            "type": str(v.get("type") or "land"),
            "upgrades": list(v.get("upgrades") or []),
            "status": str(v.get("status") or "active"),
            "is_pursuer": vname in pursuer_set,
            "notes": "",
            "_pit_speed_drops_this_round": 0,
            "boarded_by": [],
            "last_positioning": None,
        }
    state = init_chase_state(
        grid_length=chase_info.get("grid_length", DEFAULT_GRID_LENGTH),
        vehicles=vehicles_out,
        started_from="combat",
        context=chase_info.get("context"),
        start_message_id=chase_info.get("start_message_id"),
    )
    # Carry route/scene from chase_info if supplied so HUD overlay works
    # for combat→chase transitions just like plot-encounter triggers.
    if chase_info.get("route"):
        state["route"] = chase_info["route"]
    if chase_info.get("scene"):
        state["scene"] = chase_info["scene"]
    return state


# ---------------------------------------------------------------------------
# Round resolution.
# ---------------------------------------------------------------------------

def resolve_positioning_check(
    chase_state: dict,
    vehicle_name: str,
    *,
    operator_skill_total: int,
    d10_roll: int,
    intent: str = "advance",
) -> dict:
    """Apply a Positioning Check at end of round.

    `intent`:
      - "advance":      try to move 1 square forward (toward target / front)
      - "maintain":     hold position; no check, no move
      - "fall_back":    voluntarily slow; -1 square; no check

    Returns a result dict including `moved` (signed int), `dv`, `roll_total`,
    `success`, and `crashed` flag if the vehicle spun out.
    """
    if chase_state is None or "vehicles" not in chase_state:
        return {"error": "no chase state"}
    veh = chase_state["vehicles"].get(vehicle_name)
    if not isinstance(veh, dict):
        return {"error": f"unknown vehicle {vehicle_name!r}"}

    intent = (intent or "advance").strip().lower()
    if intent == "maintain":
        result = {
            "vehicle": vehicle_name,
            "operator": veh.get("operator"),
            "intent": "maintain",
            "moved": 0,
            "dv": None,
            "roll_total": None,
            "success": True,
            "crashed": False,
            "narration": "Held position; no check required.",
        }
        chase_state["round_resolution"].append(result)
        veh["last_positioning"] = result
        return result
    if intent == "fall_back":
        veh["square"] = max(0, _safe_int(veh.get("square"), default=0) - 1)
        result = {
            "vehicle": vehicle_name,
            "operator": veh.get("operator"),
            "intent": "fall_back",
            "moved": -1,
            "dv": None,
            "roll_total": None,
            "success": True,
            "crashed": False,
            "narration": "Voluntarily fell back 1 square; no check required.",
        }
        chase_state["round_resolution"].append(result)
        veh["last_positioning"] = result
        return result

    # advance: standard Positioning Check.
    speed = effective_combat_speed(veh)
    if speed < 0:
        # Spin-out cascade triggered by SDP+PIT stacking.
        veh["status"] = "crashed"
        result = {
            "vehicle": vehicle_name,
            "operator": veh.get("operator"),
            "intent": "advance",
            "moved": 0,
            "dv": None,
            "roll_total": None,
            "success": False,
            "crashed": True,
            "narration": (
                "Effective Combat Speed dropped below 8 MOVE — vehicle "
                "spins out (CP:R p.192)."
            ),
        }
        chase_state["round_resolution"].append(result)
        veh["last_positioning"] = result
        return result

    # Apply per-round flag effects to the DV (e.g. NOS lost-control = +2).
    dv_mod = 0
    if veh.get("_nos_lost_control"):
        dv_mod += 2
    if veh.get("_pull_ahead_failed"):
        dv_mod += 2  # Failed Pull Ahead leaves operator destabilized
    dv = positioning_dv_for_speed(speed, dv_modifier=dv_mod)
    roll_total = _safe_int(operator_skill_total, default=0) + _safe_int(d10_roll, default=0)
    success = roll_total >= dv
    moved = 1 if success else 0
    if success:
        veh["square"] = _safe_int(veh.get("square"), default=0) + 1
    mod_note = f" (+{dv_mod} mod from prior failure)" if dv_mod else ""
    result = {
        "vehicle": vehicle_name,
        "operator": veh.get("operator"),
        "intent": "advance",
        "moved": moved,
        "dv": dv,
        "dv_modifier": dv_mod,
        "roll_total": roll_total,
        "effective_speed": speed,
        "success": success,
        "crashed": False,
        "narration": (
            f"Positioning Check vs DV {dv}{mod_note} (Combat Speed {speed} MOVE): "
            f"rolled {roll_total} — {'success, +1 square' if success else 'failed, holds position'}."
        ),
    }
    chase_state["round_resolution"].append(result)
    veh["last_positioning"] = result
    return result


def apply_maneuver(
    chase_state: dict,
    operator_vehicle_name: str,
    maneuver: str,
    *,
    target_vehicle_name: Optional[str] = None,
    operator_skill_total: int = 0,
    d10_roll: int = 0,
) -> dict:
    """Resolve a Hot Pursuit maneuver.

    Returns a dict with `success`, `effects`, narration. Mutates chase_state
    in place (vehicle squares, _pit_speed_drops_this_round, status).
    """
    if chase_state is None or "vehicles" not in chase_state:
        return {"error": "no chase state"}
    veh = chase_state["vehicles"].get(operator_vehicle_name)
    if not isinstance(veh, dict):
        return {"error": f"unknown vehicle {operator_vehicle_name!r}"}
    m_key = (maneuver or "").strip().lower()
    spec = HOT_PURSUIT_MANEUVERS.get(m_key)
    if not spec:
        return {"error": f"unknown maneuver {maneuver!r}"}

    target = None
    if spec.get("requires_adjacent"):
        if not target_vehicle_name:
            return {"error": f"maneuver {m_key!r} requires a target vehicle"}
        target = chase_state["vehicles"].get(target_vehicle_name)
        if not isinstance(target, dict):
            return {"error": f"unknown target vehicle {target_vehicle_name!r}"}
        if not vehicles_adjacent(veh, target):
            return {
                "error": (
                    f"maneuver {m_key!r} requires adjacency; "
                    f"vehicles are {grid_distance(veh, target)} squares apart"
                )
            }

    dv = spec["dv"]
    roll_total = _safe_int(operator_skill_total, default=0) + _safe_int(d10_roll, default=0)
    success = roll_total >= dv
    effects = []

    if m_key == "nos":
        # NOS: Auto +1 square; check is for control only.
        veh["square"] = _safe_int(veh.get("square"), default=0) + 1
        effects.append(f"{operator_vehicle_name} advances 1 square (NOS auto-advance).")
        if not success:
            # Failed control check: vehicle skids; CP:R p.192 — emergency
            # Maneuver next round at +2 DV. Stamp a flag the resolver reads.
            veh["_nos_lost_control"] = True
            effects.append(
                "Lost control on harsh acceleration — next Maneuver/Positioning "
                "Check at +2 DV (CP:R p.192)."
            )
    elif m_key == "pit":
        if success:
            target["_pit_speed_drops_this_round"] = (
                _safe_int(target.get("_pit_speed_drops_this_round"), default=0) + 1
            )
            effects.append(
                f"{target_vehicle_name} wobbles — Position Check this round "
                "treated at one Combat Speed category lower."
            )
            # If the drop would push effective speed under 8 MOVE, mark crash.
            new_speed = effective_combat_speed(target)
            if new_speed < 0:
                target["status"] = "crashed"
                effects.append(f"{target_vehicle_name} spins out and crashes.")
        else:
            effects.append("PIT failed; no effect on target.")
    elif m_key == "pull_ahead":
        if success:
            tgt_sq = _safe_int(target.get("square"), default=0)
            veh["square"] = tgt_sq + 1
            effects.append(
                f"{operator_vehicle_name} pulls 1 square ahead of "
                f"{target_vehicle_name} (now at square {veh['square']})."
            )
        else:
            effects.append(
                "Pull Ahead failed — possible spin-out / collision (CP:R p.192). "
                "Apply Maneuver consequences narratively."
            )
            veh["_pull_ahead_failed"] = True
    elif m_key == "pull_in_close":
        if success:
            veh["_pull_in_close_until_end_of_round"] = True
            effects.append(
                f"{operator_vehicle_name} drops to lower edge of current range "
                "band until end of Round — boarding/melee enabled if adjacent."
            )
        else:
            effects.append("Pull in Close failed — no boost; melee/boarding not enabled.")
    elif m_key == "ramming":
        if success:
            effects.append(
                f"Ramming hit. Resolve via resolve_mechanics action_type='ramming' "
                "(CP:R p.192): 6d6 damage to BOTH vehicles (8d6 if Combat Plow + NOS), "
                "occupants suffer Whiplash Critical Injury. The chase engine does NOT "
                "auto-apply this — model must call resolve_mechanics with "
                f"vehicle_name='{operator_vehicle_name}', target_name='{target_vehicle_name}', "
                "target_is_vehicle=true, plus the occupant lists, and the engine "
                "returns SDP deltas + Whiplash crit injury state_ops."
            )
        else:
            effects.append("Ramming failed — operator misjudged; no collision.")

    result = {
        "operator_vehicle": operator_vehicle_name,
        "target_vehicle": target_vehicle_name,
        "maneuver": m_key,
        "dv": dv,
        "roll_total": roll_total,
        "success": success,
        "effects": effects,
        # Mirror Positioning Check entries so build_chase_injection's
        # "this round so far" log surfaces maneuvers too.
        "vehicle": operator_vehicle_name,
        "operator": veh.get("operator"),
        "narration": (
            f"{operator_vehicle_name} {'succeeds' if success else 'fails'} "
            f"{m_key.upper()} (DV {dv}, rolled {roll_total})"
            + (f" against {target_vehicle_name}" if target_vehicle_name else "")
            + (": " + "; ".join(effects) if effects else ".")
        ),
    }
    chase_state.setdefault("round_resolution", []).append(result)
    return result


def apply_collision_damage(
    chase_state: dict,
    vehicle_name: str,
    sdp_damage: int,
) -> dict:
    """Apply SDP damage to a vehicle and propagate the SDP-halving cascade.

    Returns a dict describing post-damage state including any speed-category
    drop / spin-out.
    """
    veh = (chase_state or {}).get("vehicles", {}).get(vehicle_name)
    if not isinstance(veh, dict):
        return {"error": f"unknown vehicle {vehicle_name!r}"}
    sdp_cur = _safe_int(veh.get("sdp_current"), default=0)
    sdp_max = _safe_int(veh.get("sdp_max"), default=0)
    new_sdp = max(0, sdp_cur - _safe_int(sdp_damage, default=0))
    veh["sdp_current"] = new_sdp
    crossed_half = (sdp_cur * 2 >= sdp_max) and (new_sdp * 2 < sdp_max)
    crashed = False
    disabled = (new_sdp == 0)
    if disabled:
        veh["status"] = "disabled"
    elif crossed_half:
        # SDP-halving threshold crossed this hit; recompute effective speed.
        eff = effective_combat_speed(veh)
        if eff < 0:
            veh["status"] = "crashed"
            crashed = True
    return {
        "vehicle": vehicle_name,
        "sdp_before": sdp_cur,
        "sdp_after": new_sdp,
        "sdp_max": sdp_max,
        "crossed_half": crossed_half,
        "crashed": crashed,
        "disabled": disabled,
    }


def end_of_round_cleanup(chase_state: dict) -> None:
    """Reset per-round flags after all Positioning Checks resolve."""
    if not isinstance(chase_state, dict):
        return
    for v in (chase_state.get("vehicles") or {}).values():
        if not isinstance(v, dict):
            continue
        v["_pit_speed_drops_this_round"] = 0
        v["_pull_in_close_until_end_of_round"] = False
        v["_nos_lost_control"] = False
        v["_pull_ahead_failed"] = False
    chase_state["round_resolution"] = []
    chase_state["round"] = _safe_int(chase_state.get("round"), default=1) + 1


# ---------------------------------------------------------------------------
# Chase-end conditions.
# ---------------------------------------------------------------------------

def check_chase_end(chase_state: dict) -> Optional[dict]:
    """Inspect chase state and return an end-condition dict if the chase is
    over, or None if it continues. Does NOT mutate state — caller decides
    whether to set `ended`.

    End conditions (Hot Pursuit p.5):
      - max_separation: ALL active pursuer/quarry pairs are >8 squares apart
        (or any pair if is_pursuer flags are absent)
      - all_pursuers_disabled: every pursuer-flagged vehicle is disabled/crashed
      - quarry_disabled: every non-pursuer vehicle is disabled/crashed
      - vehicle_disabled / vehicle_crashed: fallback when no pursuer flags are
        set (legacy 1-vs-1 chases) — ends chase on first incapacitated vehicle
      - voluntary_stop: handled by the caller via explicit signal
      - reversed_to_combat: handled by the caller via explicit signal
    """
    vehicles = (chase_state or {}).get("vehicles") or {}
    if not vehicles:
        return None
    names = list(vehicles.keys())

    def _is_active(v):
        return (v or {}).get("status") not in ("disabled", "crashed")

    pursuers = [n for n, v in vehicles.items() if (v or {}).get("is_pursuer")]
    quarry = [n for n, v in vehicles.items() if not (v or {}).get("is_pursuer")]
    has_pursuer_flags = bool(pursuers) and bool(quarry)

    if has_pursuer_flags:
        # Multi-vehicle aware: chase ends when one side has zero active vehicles.
        active_pursuers = [n for n in pursuers if _is_active(vehicles[n])]
        active_quarry = [n for n in quarry if _is_active(vehicles[n])]
        if not active_pursuers:
            return {
                "ended": True,
                "reason": "all_pursuers_disabled",
                "side": "pursuers",
                "vehicles": pursuers,
            }
        if not active_quarry:
            return {
                "ended": True,
                "reason": "quarry_disabled",
                "side": "quarry",
                "vehicles": quarry,
            }
    else:
        # Legacy fallback: ANY disabled/crashed vehicle ends the chase
        # (1-vs-1 chases don't need partition logic).
        for n, v in vehicles.items():
            status = (v or {}).get("status")
            if status in ("disabled", "crashed"):
                return {
                    "ended": True,
                    "reason": "vehicle_disabled" if status == "disabled" else "vehicle_crashed",
                    "vehicle": n,
                }

    # Max separation: only ends the chase when EVERY active pursuer/quarry
    # pair exceeds 8 squares (single distant pair doesn't end if others are
    # still in range).
    if has_pursuer_flags:
        active_pursuers = [n for n in pursuers if _is_active(vehicles[n])]
        active_quarry = [n for n in quarry if _is_active(vehicles[n])]
        if active_pursuers and active_quarry:
            max_pair_sep = 0
            farthest_pair = None
            min_active_sep = None
            for p in active_pursuers:
                for q in active_quarry:
                    d = grid_distance(vehicles[p], vehicles[q])
                    if min_active_sep is None or d < min_active_sep:
                        min_active_sep = d
                    if d > max_pair_sep:
                        max_pair_sep = d
                        farthest_pair = (p, q)
            if min_active_sep is not None and min_active_sep > MAX_CHASE_SEPARATION_SQUARES:
                return {
                    "ended": True,
                    "reason": "max_separation",
                    "distance_squares": min_active_sep,
                    "pair": farthest_pair,
                }
    else:
        # Legacy: any pair > 8 sq ends the chase.
        max_sep = 0
        pair = None
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                d = grid_distance(vehicles[names[i]], vehicles[names[j]])
                if d > max_sep:
                    max_sep = d
                    pair = (names[i], names[j])
        if max_sep > MAX_CHASE_SEPARATION_SQUARES:
            return {
                "ended": True,
                "reason": "max_separation",
                "distance_squares": max_sep,
                "pair": pair,
            }
    return None


def determine_exit_mode(
    chase_state: dict,
    *,
    hostile_combatants_alive: bool,
    end_reason: Optional[str] = None,
) -> str:
    """Decide which mode to fall back to when the chase ends.

    Routing logic (per user spec + RAW intent):
      - end_reason in {max_separation, voluntary_stop} -> 'general'
        (everyone disengaged; pursuers far away or all parties stopped)
      - end_reason == 'reversed_to_combat' -> 'combat'
        (explicit signal: lead vehicle reversed and ran at follower; CP:R p.5)
      - otherwise (vehicle_disabled / vehicle_crashed / all_pursuers_disabled /
        quarry_disabled / unspecified) -> 'combat' if hostile_combatants_alive
        else 'general'

    `end_reason` defaults to chase_state['end_reason'] when not supplied.
    `hostile_combatants_alive` is computed by the caller from
    pipeline_state.combat / character_states / chase pursuer occupants.
    """
    if end_reason is None and isinstance(chase_state, dict):
        end_reason = chase_state.get("end_reason")
    # Terminal disengagement reasons override the hostile heuristic.
    if end_reason in ("max_separation", "voluntary_stop"):
        return "general"
    if end_reason == "reversed_to_combat":
        return "combat"
    # Ambiguous reasons (incapacitations, unspecified) consult the hostile-
    # alive heuristic to pick combat vs general.
    if hostile_combatants_alive:
        return "combat"
    return "general"


def end_chase(
    chase_state: dict,
    *,
    reason: str,
    narrative_summary: Optional[str] = None,
    exit_target_mode: str = "general",
) -> None:
    """Mark the chase as ended. Caller is responsible for the actual mode
    transition; this only stamps the chase state for handoff."""
    if not isinstance(chase_state, dict):
        return
    chase_state["active"] = False
    chase_state["ended"] = True
    chase_state["end_reason"] = reason
    chase_state["exit_target_mode"] = exit_target_mode
    if narrative_summary:
        chase_state["narrative_summary"] = narrative_summary


# ---------------------------------------------------------------------------
# Injection builder (for the system prompt).
# ---------------------------------------------------------------------------

def build_chase_injection(chase_state: dict, pipeline_state: dict) -> str:
    """Build the chase-state injection string for the per-turn user message."""
    if not isinstance(chase_state, dict) or not chase_state:
        return ""
    lines = []
    lines.extend(_render_transition(chase_state.get("context")))
    lines.append("[CHASE STATE]")
    if chase_state.get("ended"):
        lines.append(
            f"Chase ended ({chase_state.get('end_reason') or 'unspecified'}). "
            f"Exit target mode: {chase_state.get('exit_target_mode') or 'general'}."
        )
        if chase_state.get("narrative_summary"):
            lines.append(f"Summary: {chase_state['narrative_summary']}")
        lines.append("[/CHASE STATE]")
        return "\n".join(lines)
    lines.append(f"Round: {chase_state.get('round', 1)}")
    grid_len = chase_state.get("grid_length", DEFAULT_GRID_LENGTH)
    lines.append(f"Chase Grid: {grid_len} squares")
    if chase_state.get("current_turn"):
        lines.append(f"Current Turn: {chase_state['current_turn']}")
    init_map = chase_state.get("operator_initiative") or {}
    if isinstance(init_map, dict) and init_map:
        ordered = sorted(init_map.items(), key=lambda kv: -_safe_int(kv[1], default=0))
        lines.append(
            "Initiative: "
            + ", ".join(f"{name} ({score})" for name, score in ordered)
        )
    lines.append("Vehicles:")
    for vname, v in (chase_state.get("vehicles") or {}).items():
        if not isinstance(v, dict):
            continue
        sdp_cur = v.get("sdp_current", 0)
        sdp_max = v.get("sdp_max", 0)
        speed = v.get("combat_speed_move", 0)
        eff_speed = effective_combat_speed(v)
        sdp_flag = " (<=1/2 SDP, -1 speed cat)" if (sdp_max > 0 and sdp_cur * 2 < sdp_max) else ""
        speed_str = (
            f"{speed} MOVE" if eff_speed == speed
            else f"{speed} MOVE (eff. {eff_speed} MOVE{sdp_flag})"
        )
        status = v.get("status", "active")
        status_tag = "" if status == "active" else f" [{status.upper()}]"
        operator = v.get("operator") or "?"
        boarded = v.get("boarded_by") or []
        boarded_str = f" Boarders: {', '.join(boarded)}" if boarded else ""
        lines.append(
            f"  {vname}: square {v.get('square', 0)} | SDP {sdp_cur}/{sdp_max} | "
            f"SP {v.get('sp', 0)} | {speed_str} | Driver: {operator}{boarded_str}{status_tag}"
        )
    # Range table summary so the GM/model can read distances at a glance.
    veh_items = list((chase_state.get("vehicles") or {}).items())
    if len(veh_items) >= 2:
        lines.append("Distances:")
        for i in range(len(veh_items)):
            for j in range(i + 1, len(veh_items)):
                a_name, a_v = veh_items[i]
                b_name, b_v = veh_items[j]
                d = grid_distance(a_v, b_v)
                # Pull in Close (this round) collapses the band to its lower
                # edge for either participant.
                pic = bool(
                    a_v.get("_pull_in_close_until_end_of_round")
                    or b_v.get("_pull_in_close_until_end_of_round")
                )
                band = squares_to_range_band(d, pull_in_close=pic)
                if band:
                    if band[0] == band[1]:
                        lines.append(
                            f"  {a_name} <-> {b_name}: {d} sq (~{band[0]} m, Pull-in-Close active)"
                        )
                    else:
                        lines.append(f"  {a_name} <-> {b_name}: {d} sq ({band[0]}-{band[1]} m)")
                else:
                    lines.append(f"  {a_name} <-> {b_name}: {d} sq (out of band — chase ends if it stays >8)")
    # Pending obstacle (Hot Pursuit p.4) — ALL operators must spend their
    # standard Action on a CP:R p.192 Maneuver Check this round.
    obstacle = chase_state.get("pending_obstacle")
    if isinstance(obstacle, dict) and obstacle.get("description"):
        _mt = obstacle.get("maneuver_type", "swerve")
        _dv_map = {
            "swerve": 13, "sharp_turn": 13, "emergency_stop": 13,
            "bootleg_turn": 17, "do_a_jump": 17,
            "landing": 13, "aerobatic_maneuver": 17,
        }
        _dv = _dv_map.get(_mt, 13)
        _fail = "fall back 1 sq" if obstacle.get("fall_back_on_failure", True) else "Lose Control of Vehicle"
        lines.append(
            f"OBSTACLE THIS ROUND: {obstacle['description']} — all operators "
            f"must spend Action on Maneuver Check ({_mt.replace('_', ' ').title()} "
            f"DV {_dv}). Failure = {_fail}. Dispatch driving_check per operator."
        )
    # Round-resolution log.
    rr = chase_state.get("round_resolution") or []
    if rr:
        lines.append("This round so far:")
        for entry in rr:
            lines.append(f"  - {entry.get('narration', '')}")
    lines.append("[/CHASE STATE]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Mode contract (system prompt for the GM during a Hot Pursuit scene).
# ---------------------------------------------------------------------------

HOT_PURSUIT_CONTRACT = """You are the CHASE MASTER for a Cyberpunk RED session. A Hot Pursuit chase is underway.

YOUR ROLE: Adjudicate the chase using the Hot Pursuit supplement (May 2024 ruleset). You cover Events, Mechanics, and Narration in a single focused call each exchange. Use the Hot Pursuit chase grid — NOT free-form distance narration.

Call report_chase_state every exchange, then write your narrative response.

KEY RULES (Hot Pursuit supplement):

CHASE GRID:
- Each square represents one Range Band on the Range Table.
- 1 sq = 0-6 m | 2 sq = 7-12 m | 3 sq = 13-25 m | 4 sq = 26-50 m | 5 sq = 51-100 m | 6 sq = 101-200 m | 7 sq = 201-400 m | 8 sq = 401-800 m
- The chase ends when any two participating vehicles are MORE than 8 squares apart, when a vehicle is disabled/crashed, or when the chase voluntarily ends.

INITIATIVE:
- Operators (drivers) roll Initiative — they are NOT automatically at the top of the queue.
- On the FIRST chase exchange, roll Initiative for each operator (REF + d10) and report it via `operator_initiative: {operator_name: total}`. Engine stamps it onto chase state.
- Each subsequent exchange, set `current_turn` to the operator currently acting. Engine tracks this for the per-turn injection.

CHASE ROUND STRUCTURE:
- Each participant gets one Move Action and one standard Action per Chase Round.
- Move Action for operators is NOT used to move the vehicle — vehicle movement happens at end of Round via Positioning Check.
- Operators use Move Action to leave/enter vehicles, climb, or leap (using their personal MOVE stat against the chase-grid distance to the target square).
- If an operator's REF + Relevant Control Skill + bonuses < 9, they cannot perform a standard Action that round (all energy on control).

POSITIONING CHECK (end of every Chase Round):
- Each operator who wants to move forward on the grid must succeed at a Positioning Check.
- DV by Combat Speed (vehicle MOVE):
  60 MOVE -> DV 13 | 40 MOVE -> DV 15 | 20 MOVE -> DV 17 | 15 MOVE -> DV 21 | 10 MOVE -> DV 24 | 8 MOVE -> DV 29
- A vehicle at <= 1/2 SDP rolls as one Combat Speed category lower. If that drop puts effective Combat Speed below 8 MOVE, the vehicle spins out and crashes (CP:R p.192).
- Success: vehicle moves +1 square forward on the grid.
- Failure: vehicle remains where it is.
- Operator can voluntarily maintain (no check, no move) or fall back -1 square (no check).
- Positioning Check is NOT an Action and NOT a Move Action.
- The backend computes effective speed and DV automatically — you describe the action; the engine resolves the math.

MANEUVERS (use a standard Action; resolve as Drive Land Vehicle / Pilot check vs DV):
- NOS (DV 13): Auto-move +1 sq this Action (vehicle must have NOS upgrade). Check is for control only — failure = lost control flag (next check at +2 DV).
- PIT (DV 15, requires adjacent vehicle): No damage, but target's end-of-round Positioning Check is one Combat Speed category lower. Stacks with SDP halving.
- Pull Ahead (DV 17, requires adjacent vehicle): Move 1 square ahead of target. Failure may spin out or hit oncoming obstacle.
- Pull in Close (DV 13): Drop to lower edge of current range band until end of Round. Enables boarding/melee from this vehicle to the adjacent target.
- Ramming (DV 17, requires adjacent): Focused crash attempt. Both vehicles take 3d6; occupants 1d6 (Athletics DV 13). NOT automatic just from being in the same square.

CP:R CORE VEHICLE COMBAT (p.192) — INTERLEAVED WITH CHASE:
Hot Pursuit explicitly defers to CP:R p.192 for all damage/control mechanics. Use the existing resolve_mechanics action types:

- **Ramming** (after a successful Hot Pursuit Ramming maneuver, OR when a vehicle that's Lost Control impacts something):
    resolve_mechanics action_type="ramming". Backend handles 6d6 damage to BOTH vehicles, Combat Plow / NOS interaction (8d6 with both, Combat Plow negates attacker damage), and applies Whiplash Critical Injury to all occupants of both vehicles + any pedestrian struck. Pedestrians can dodge (DV 13 DEX + Evasion).

- **Maneuver Check / Lose Control consequences** (obstacles, sharp turns, dangerous terrain, NOS lost-control follow-through, Pull Ahead failure consequence, basic-driving DV10 when REF + Skill <= 9):
    resolve_mechanics action_type="driving_check" with maneuver: maintain_control / swerve / sharp_turn / emergency_stop / bootleg_turn / do_a_jump / landing / aerobatic_maneuver. DVs from CP:R p.192 (10, 13, 13, 13, 17, 17, 13, 17 respectively). Failure sets control_lost=true. When control is lost, GM (the model) decides the turn's full movement; if the vehicle impacts something, immediately resolve as ramming.

- **Aimed shot at vehicle weak point** (tires, engine, gas cap):
    resolve_mechanics action_type="vehicle_weak_point". Backend applies the -8 Aimed Shot penalty, the DV 13 hit-the-weak-point check (only if vehicle is moving), and the ×2 damage past SP rule.

CHASE-SPECIFIC TRIGGERS THAT DISPATCH INTO p.192:
- PIT or SDP-halving cascade reduces effective Combat Speed below 8 MOVE -> automatic spin-out -> immediately dispatch driving_check (the operator is fighting to maintain control; failure = Lose Control = ram into whatever's around).
- NOS used and control check failed -> _nos_lost_control flag set; on next standard Action or end-of-round, dispatch driving_check.
- Pull Ahead failed -> spin-out / oncoming-obstacle consequence per CP:R p.192. Dispatch driving_check or apply ramming directly if hitting a known obstacle.
- Obstacles, dangerous terrain, sharp turns thrown by GM -> ALL operators must spend their standard Action that round on a driving_check (Maneuver Skill Check, Hot Pursuit p.4).
- Failed Maneuver Check during a chase -> per Hot Pursuit p.4, vehicle either falls back 1 sq (slowed down) OR spins out of control (CP:R p.192 Lose Control). GM picks based on degree of failure / risk.

BOARDING / MELEE / RANGED ATTACKS:
- Hot Pursuit does NOT define its own ranged-attack DVs. Use the CP:R core RANGED_DV_TABLE (Combat Ruleset §3, weapon × range bracket).
- The chase grid is intentionally aligned with CP:R range brackets. Grid distance N squares maps to CP:R range bracket index N - 1:
    1 sq -> idx 0 (0-6 m)        5 sq -> idx 4 (51-100 m)
    2 sq -> idx 1 (7-12 m)       6 sq -> idx 5 (101-200 m)
    3 sq -> idx 2 (13-25 m)      7 sq -> idx 6 (201-400 m)
    4 sq -> idx 3 (26-50 m)      8 sq -> idx 7 (401-800 m)
- When resolving a ranged attack between vehicles, pass `range_bracket: N-1` to resolve_mechanics where N is the grid distance. The backend looks up the weapon-specific DV (Pistol 13/15/20/25/30/30/-/-, Assault Rifle 17/16/15/13/15/20/25/30, etc. — see Combat Ruleset).
- Pull in Close (this round) collapses the band to its lower edge. The injected [CHASE STATE] block already shows the active band per pair; treat it as the canonical range for attacks this round.
- Melee attacks against another vehicle (or its occupants) require adjacency (1 sq apart or same square) AND someone first performing Pull in Close that round.
- Leap from one vehicle to another: personal MOVE stat vs the grid distance, using the upper edge of the current band (Hot Pursuit p.3).

ENDING THE CHASE:
- If both vehicles voluntarily stop OR a vehicle is disabled/crashed OR separation > 8 squares: the chase ends.
- If the lead vehicle reverses course and runs directly at the follower, switch to standard CP:R combat — it isn't a chase anymore.
- The backend will auto-route to the next mode: combat (if hostile combatants are alive and engaged) or general.

STATE TRACKING via report_chase_state:
The backend tracks vehicle SDP cascade, Positioning Check resolution, range bands, and chase-end conditions automatically. Your job is to:
- Set vehicle_intents for each vehicle this round — array of objects, each with vehicle name, intent (advance / maintain / fall_back), operator_skill_total, d10_roll
- Declare any maneuver attempted this round (operator_vehicle, maneuver, target_vehicle, skill_total, d10_roll)
- Apply collision damage when ramming hits (sdp_damage to vehicles, hp_delta to occupants per Athletics check outcomes)
- Update vehicle status if disabled/crashed
- Set chase_complete: true when the chase has ended (specify end_reason and exit_target_mode if known)

NARRATION RULES:
- Describe range using Hot Pursuit's narrative ranges, not abstract numbers — "the Phoenix sedan is right on your bumper" beats "1 square away".
- Be specific about driving skill — Drive Land Vehicle, Pilot Air Vehicle, Pilot Sea Vehicle. Match the vehicle type.
- Maneuvers are dramatic. Narrate the failed PIT, the missed ram, the pulled-up alongside before the boarder leaps.
- Passenger Actions can use Complementary Skill Checks: Local Expert to shout directions (bonus to next Positioning Check), Perception for obstacles (bonus to next Maneuver), Tactics to direct ally fire.
- Throw obstacles, dangerous terrain, and sharp turns to force Maneuver Checks (CP:R p.192). Failure = spin out or fall back 1 sq.

The backend resolves Positioning Checks and SDP cascades. You set intent. The engine does the math.
"""


# ---------------------------------------------------------------------------
# Tool schema for report_chase_state.
# ---------------------------------------------------------------------------

REPORT_CHASE_STATE_TOOL = {
    "name": "report_chase_state",
    "description": (
        "Report the chase state at the end of this exchange. Backend resolves "
        "Positioning Checks, SDP cascade, range bands, and chase-end conditions; "
        "you supply intent and maneuver attempts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "current_turn": {
                "type": ["string", "null"],
                "description": (
                    "Operator currently acting (read by the engine to update "
                    "chase['current_turn'] for the next-round narration)."
                ),
            },
            "operator_initiative": {
                "type": ["object", "null"],
                "description": (
                    "Initial-round only: dict mapping operator name -> rolled "
                    "initiative value (REF + d10). Engine stamps "
                    "chase['operator_initiative'] and sorts current_turn."
                ),
                "additionalProperties": {"type": "integer"},
            },
            "vehicle_intents": {
                "type": "array",
                "description": "Each vehicle's end-of-round Positioning intent.",
                "items": {
                    "type": "object",
                    "required": ["vehicle", "intent"],
                    "properties": {
                        "vehicle": {"type": "string"},
                        "intent": {"enum": ["advance", "maintain", "fall_back"]},
                        "operator_skill_total": {"type": "integer", "description": "REF + Drive/Pilot skill (excl. d10)."},
                        "d10_roll": {"type": "integer"},
                    },
                },
            },
            "maneuvers": {
                "type": "array",
                "description": "Maneuvers attempted this round.",
                "items": {
                    "type": "object",
                    "required": ["operator_vehicle", "maneuver"],
                    "properties": {
                        "operator_vehicle": {"type": "string"},
                        "maneuver": {"enum": ["nos", "pit", "pull_ahead", "pull_in_close", "ramming"]},
                        "target_vehicle": {"type": ["string", "null"]},
                        "operator_skill_total": {"type": "integer"},
                        "d10_roll": {"type": "integer"},
                    },
                },
            },
            "vehicle_updates": {
                "type": "array",
                "description": "Per-vehicle state changes (square overrides, sdp_damage, status, occupants/boarders).",
                "items": {
                    "type": "object",
                    "required": ["vehicle"],
                    "properties": {
                        "vehicle": {"type": "string"},
                        "sdp_damage": {"type": "integer"},
                        "square": {"type": "integer"},
                        "status": {"enum": ["active", "disabled", "crashed"]},
                        "occupants": {"type": "array", "items": {"type": "string"}},
                        "boarders": {"type": "array", "items": {"type": "string"}},
                        "notes": {"type": "string"},
                    },
                },
            },
            "character_updates": {
                "type": "array",
                "description": "HP/condition changes for occupants (collision damage, gunfire, etc.).",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "hp_delta": {"type": "integer"},
                        "conditions_add": {"type": "array", "items": {"type": "string"}},
                        "conditions_remove": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "obstacle_maneuver": {
                "type": ["object", "null"],
                "description": (
                    "Set when GM throws an obstacle / dangerous terrain / sharp turn "
                    "into the chase round (Hot Pursuit p.4). All operators must spend "
                    "their standard Action on a Maneuver Skill Check (CP:R p.192). "
                    "The model SEPARATELY dispatches one resolve_mechanics action_type="
                    "'driving_check' per operator with the appropriate maneuver type. "
                    "This field is informational — the engine surfaces the obstacle in "
                    "the per-turn chase injection so the model knows ALL operators are "
                    "spending their Action this round on the Maneuver Check (not on "
                    "shooting / boarding / NOS / etc.)."
                ),
                "properties": {
                    "description": {"type": "string", "description": "What the obstacle is (e.g., 'oncoming tanker truck', 'shattered glass strewn across lane', 'sharp turn into Charter Hill')."},
                    "maneuver_type": {
                        "type": "string",
                        "enum": ["swerve", "sharp_turn", "emergency_stop", "bootleg_turn", "do_a_jump", "landing", "aerobatic_maneuver"],
                        "description": "Which CP:R p.192 Maneuver applies. Determines the DV (13 / 13 / 13 / 17 / 17 / 13 / 17 respectively).",
                    },
                    "fall_back_on_failure": {
                        "type": "boolean",
                        "description": "If true, failure = vehicle falls back 1 square (slowed down). If false, failure = Lose Control of Vehicle (CP:R p.192) — GM narrates the outcome and dispatches a ramming action_type if the vehicle impacts something.",
                    },
                },
            },
            "chase_complete": {"type": "boolean"},
            "end_reason": {
                "type": ["string", "null"],
                "enum": ["max_separation", "vehicle_disabled", "vehicle_crashed",
                         "voluntary_stop", "reversed_to_combat", None],
            },
            "exit_target_mode": {
                "type": ["string", "null"],
                "enum": ["combat", "general", None],
            },
            "narrative_summary": {
                "type": ["string", "null"],
                "description": (
                    "ONLY when chase_complete=true. Up to 3 paragraphs in story-shaped "
                    "prose: what happened across the chase, anything unexpected (lucky "
                    "Positioning Checks, costly maneuvers, surprise rams, vehicles "
                    "crashing), how it ended, and any unresolved tension or consequence "
                    "the next mode should carry. The receiving mode reads this as the "
                    "handoff so play picks up seamlessly."
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# State applier (called from main.py after report_chase_state returns).
# ---------------------------------------------------------------------------

def apply_chase_state(pipeline_state, tool_input, game_state=None, **_kw):
    """Apply chase state updates from report_chase_state output."""
    if not isinstance(tool_input, dict):
        logger.warning("apply_chase_state: tool_input must be an object, got %s",
                       type(tool_input).__name__)
        return
    chase = pipeline_state.get("chase", {})
    if not isinstance(chase, dict):
        chase = {}

    # Stash the pre-chase HUD location once on first apply (so mode-end can
    # restore it) and overlay the chase route as the active HUD location.
    # Idempotent — same logic as stamp_chase_hud_overlay; reused here so
    # apply runs work even when seeding paths didn't pre-stamp.
    stamp_chase_hud_overlay(pipeline_state)

    # Initiative + current_turn tracking (model-supplied, engine stores).
    op_init = tool_input.get("operator_initiative")
    if isinstance(op_init, dict) and op_init:
        chase["operator_initiative"] = {
            str(k): _safe_int(v, default=0) for k, v in op_init.items()
        }
    if "current_turn" in tool_input:
        ct = tool_input.get("current_turn")
        chase["current_turn"] = str(ct) if ct else None

    # Obstacle maneuver flag (Hot Pursuit p.4) — surfaced in next round's
    # injection so the model knows all operators are spending their Action
    # on Maneuver Check (CP:R p.192) rather than other Actions.
    obstacle = tool_input.get("obstacle_maneuver")
    if isinstance(obstacle, dict) and obstacle.get("description"):
        chase["pending_obstacle"] = {
            "description": str(obstacle.get("description") or ""),
            "maneuver_type": str(obstacle.get("maneuver_type") or "swerve"),
            "fall_back_on_failure": bool(obstacle.get("fall_back_on_failure", True)),
        }
    elif obstacle is None and "obstacle_maneuver" in tool_input:
        # Explicit null -> clear any pending obstacle.
        chase.pop("pending_obstacle", None)

    # Resolve maneuvers FIRST so PIT effects land before Positioning Checks.
    for m in (tool_input.get("maneuvers") or []):
        if not isinstance(m, dict):
            continue
        apply_maneuver(
            chase,
            m.get("operator_vehicle"),
            m.get("maneuver"),
            target_vehicle_name=m.get("target_vehicle"),
            operator_skill_total=_safe_int(m.get("operator_skill_total"), default=0),
            d10_roll=_safe_int(m.get("d10_roll"), default=0),
        )

    # End-of-round Positioning Checks (per intent).
    for vi in (tool_input.get("vehicle_intents") or []):
        if not isinstance(vi, dict):
            continue
        resolve_positioning_check(
            chase,
            vi.get("vehicle"),
            operator_skill_total=_safe_int(vi.get("operator_skill_total"), default=0),
            d10_roll=_safe_int(vi.get("d10_roll"), default=0),
            intent=vi.get("intent") or "advance",
        )

    # Vehicle updates (SDP damage, square overrides, status, occupants).
    for vu in (tool_input.get("vehicle_updates") or []):
        if not isinstance(vu, dict):
            continue
        vname = vu.get("vehicle")
        if not vname or vname not in chase.get("vehicles", {}):
            continue
        veh = chase["vehicles"][vname]
        if "sdp_damage" in vu:
            apply_collision_damage(chase, vname, _safe_int(vu.get("sdp_damage"), default=0))
        if "square" in vu:
            veh["square"] = _safe_int(vu.get("square"), default=veh.get("square", 0))
        if vu.get("status"):
            veh["status"] = str(vu["status"])
        if "occupants" in vu and isinstance(vu["occupants"], list):
            veh["occupants"] = list(vu["occupants"])
        if "boarders" in vu and isinstance(vu["boarders"], list):
            veh["boarded_by"] = list(vu["boarders"])
        if vu.get("notes"):
            veh["notes"] = str(vu["notes"])

    # Character updates (occupant HP / conditions). Pass a CURATED copy of
    # tool_input to the shared meatspace helper — chase mode owns its own
    # vehicle_updates schema (vehicle/sdp_damage/square/...), which would be
    # mis-applied if the helper read it as combat-shape vehicle_updates.
    # ALLOWLIST (must be updated when _apply_meatspace_shared adds new
    # tool_input keys it consumes — current consumers: character_updates,
    # cover_state, vehicle_updates, combat, combat_complete; chase only
    # forwards the first two):
    from .cpred_combat import _apply_meatspace_shared
    _SHARED_KEYS_FOR_CHASE = ("character_updates", "cover_state")
    _shared_input = {
        k: v for k, v in tool_input.items()
        if k in _SHARED_KEYS_FOR_CHASE
    }
    _apply_meatspace_shared(pipeline_state, _shared_input, game_state=game_state)

    # Compute exit target mode: routing depends on end_reason (terminal
    # disengagement reasons short-circuit) and falls back to hostile-alive
    # heuristic for ambiguous reasons. Done here so both auto-detect and
    # model-signaled paths get the same routing.
    _hostiles_alive = _hostile_combatants_alive(pipeline_state)

    if tool_input.get("chase_complete"):
        # Model-signaled end: full end_chase with all flags + narrative.
        _model_reason = tool_input.get("end_reason") or "voluntary_stop"
        _exit_mode = (
            tool_input.get("exit_target_mode")
            or determine_exit_mode(
                chase,
                hostile_combatants_alive=_hostiles_alive,
                end_reason=_model_reason,
            )
        )
        end_chase(
            chase,
            reason=_model_reason,
            narrative_summary=tool_input.get("narrative_summary"),
            exit_target_mode=_exit_mode,
        )
    else:
        # Engine auto-detect end: separation > 8 sq, all-pursuers-down, etc.
        # Use full end_chase so mode-end detection in main.py fires correctly
        # (active=False AND narrative_summary present).
        end_check = check_chase_end(chase)
        if end_check and end_check.get("ended"):
            _auto_reason = end_check["reason"]
            _exit_mode = (
                tool_input.get("exit_target_mode")
                or determine_exit_mode(
                    chase,
                    hostile_combatants_alive=_hostiles_alive,
                    end_reason=_auto_reason,
                )
            )
            _auto_summary = (
                f"The chase ended ({_auto_reason}). "
                + (f"Vehicle: {end_check['vehicle']}." if end_check.get("vehicle") else "")
                + (f" Final separation: {end_check['distance_squares']} squares."
                   if end_check.get("distance_squares") else "")
            ).strip()
            end_chase(
                chase,
                reason=_auto_reason,
                narrative_summary=tool_input.get("narrative_summary") or _auto_summary,
                exit_target_mode=_exit_mode,
            )

    # Reset per-round flags + advance round counter (only if chase still active).
    if not chase.get("ended"):
        end_of_round_cleanup(chase)

    pipeline_state["chase"] = chase


def _hostile_combatants_alive(pipeline_state: dict) -> bool:
    """Inspect pipeline_state to decide whether hostile combatants are alive
    and engaged. Used by determine_exit_mode at chase end.

    Walks three sources:
      1. pipeline_state['combat']['initiative_order'] (active combat hostiles)
      2. pipeline_state['character_states'] non-PC entries (NPCs in scene)
      3. pipeline_state['chase']['vehicles'][...]['occupants'] in active
         pursuer vehicles (NPCs still inside / on top of pursuit vehicles —
         these may not appear in combat or character_states if the chase
         was seeded fresh from a Plot Encounter).
    """
    if not isinstance(pipeline_state, dict):
        return False
    combat = pipeline_state.get("combat")
    chase = pipeline_state.get("chase")
    edgerunners = (pipeline_state.get("game_state") or {}).get("edgerunners") or {}
    cs = pipeline_state.get("character_states") or {}
    candidates = []
    if isinstance(combat, dict):
        order = combat.get("initiative_order") or []
        if isinstance(order, list):
            candidates.extend(n for n in order if isinstance(n, str))
    # Walk character_states for non-PC entries.
    for name, entry in cs.items():
        if not isinstance(entry, dict) or name in candidates:
            continue
        d = entry.get("data", entry)
        if isinstance(d, dict) and d.get("type") not in ("pc",):
            candidates.append(name)
    # Walk chase pursuer-vehicle occupants — these are typically non-PC NPCs
    # not registered in combat or character_states when chase came from a
    # plot encounter trigger.
    chase_pursuer_occupants = []
    if isinstance(chase, dict):
        for vname, v in (chase.get("vehicles") or {}).items():
            if not isinstance(v, dict):
                continue
            if not v.get("is_pursuer"):
                continue
            if v.get("status") in ("disabled", "crashed"):
                continue
            for occ in (v.get("occupants") or []):
                if isinstance(occ, str) and occ and occ not in candidates and occ not in edgerunners:
                    chase_pursuer_occupants.append(occ)
                    candidates.append(occ)

    for name in candidates:
        if name in edgerunners:
            continue  # PC, not hostile
        # Chase pursuer occupants without a character_states entry are presumed
        # hostile and alive (mooks not yet stat-blocked) — return True for
        # safety per RAW intent.
        if name in chase_pursuer_occupants and name not in cs:
            return True
        entry = cs.get(name) or {}
        d = entry.get("data", entry)
        if not isinstance(d, dict):
            # No stat block but they're a candidate hostile — assume alive.
            if name in chase_pursuer_occupants:
                return True
            continue
        for v in (d.get("vitals") or []):
            if isinstance(v, dict) and v.get("label") == "HP":
                cur = v.get("current", 0)
                try:
                    if int(cur or 0) > 0:
                        return True
                except (TypeError, ValueError):
                    continue
    return False


# ---------------------------------------------------------------------------
# Profile builder (drivers + occupant rosters surfaced in system prompt).
# ---------------------------------------------------------------------------

def build_chase_profile(character_states, chase_state, game_state=None, **_kw):
    """Return a roster string showing drivers, occupants, and their stats
    relevant to the chase (Drive/Pilot skill, REF, Athletics for collision)."""
    if not isinstance(chase_state, dict):
        return ""
    edgerunners = (game_state or {}).get("edgerunners", {})
    lines = ["[CHASE ROSTER]"]
    for vname, v in (chase_state.get("vehicles") or {}).items():
        if not isinstance(v, dict):
            continue
        operator = v.get("operator") or "?"
        occupants = v.get("occupants") or []
        boarders = v.get("boarded_by") or []
        lines.append(f"  {vname} ({v.get('type', 'land')}):")
        lines.append(
            f"    SDP {v.get('sdp_current', 0)}/{v.get('sdp_max', 0)} | "
            f"SP {v.get('sp', 0)} | Combat Speed {v.get('combat_speed_move', 0)} MOVE"
        )
        # Operator block
        op_er = edgerunners.get(operator)
        if op_er:
            stats = op_er.get("stats") or {}
            skills = op_er.get("skills") or {}
            ref = stats.get("REF", "?")
            drive = (
                skills.get("Drive Land Vehicle")
                or skills.get("Pilot Air Vehicle")
                or skills.get("Pilot Sea Vehicle")
                or skills.get("Drive Ground Vehicle")
                or "?"
            )
            athletics = skills.get("Athletics", "?")
            lines.append(
                f"    Operator: {operator} (REF {ref} | Drive/Pilot {drive} | Athletics {athletics})"
            )
        else:
            lines.append(f"    Operator: {operator}")
        # Occupants
        passenger_names = [o for o in occupants if o and o != operator]
        if passenger_names:
            lines.append(f"    Passengers: {', '.join(passenger_names)}")
        if boarders:
            lines.append(f"    Boarders (clinging on / inside): {', '.join(boarders)}")
    if len(lines) == 1:
        return ""
    lines.append("[/CHASE ROSTER]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry-handoff bootstrap (used by main.py when chase mode opens without a
# pre-supplied handoff_summary — e.g., a plot-encounter trigger that didn't
# include a story-shaped lead-in). Mirrors ship_combat's bootstrap pattern.
# ---------------------------------------------------------------------------

CHASE_BOOTSTRAP_SYSTEM = (
    "You generate a hidden Hot Pursuit chase handoff bootstrap for a TTRPG app. "
    "Return JSON only. Produce a canonical handoff_summary AND a short player-facing "
    "opening narration.\n\n"
    "HANDOFF SUMMARY (2-3 paragraphs in story-shaped prose):\n"
    "Cover, in order: (1) what's going on right now — who's chasing whom, where, in "
    "what kinds of vehicles, the immediate environment as the chase opens; "
    "(2) why we're here — the chain of choices and pressures from prior scenes that "
    "led to this chase (the heist that just went sideways, the deal that fell apart, "
    "the patrol that spotted the crew); "
    "(3) what the goal is — what reaching the end of the chase looks like (escape to "
    "a specific destination, disable the pursuers, board the lead vehicle), what "
    "failure looks like, and any constraints or stakes the GM should hold (NCPD "
    "response timer, civilian collateral, faction visibility). "
    "Reference the immediate prior scene's arc and any unresolved tension being "
    "carried forward. Write so the next mode picks up seamlessly without losing "
    "context. NOT a tactical snapshot of vehicle positions — the engine state "
    "already covers that.\n\n"
    "OPENING NARRATION (1 short paragraph):\n"
    "The first beat the player sees as the chase opens. Sensory, in-fiction, the "
    "feel of acceleration and the sound of the pursuers behind. No rules talk.\n\n"
    "Do not resolve the chase. Do not generate Positioning Checks, maneuvers, or "
    "outcomes."
)


def apply_chase_writeback(chase_state, pipeline_state):
    """After the chase ends, write back vehicle state (SDP, status) to
    pipeline_state.combat['vehicles'] so a subsequent combat scene inherits
    the post-chase vehicle condition.

    No-ops when no combat slot exists — the caller (main.py mode-end block)
    decides whether to seed combat based on exit_target_mode. If exit is
    "general," vehicle damage is intentionally dropped (the scene transition
    is narrative, not mechanical-tracking). If exit is "combat," main.py
    seeds combat first, then this writeback fills in the vehicle data.
    """
    if not isinstance(chase_state, dict) or not isinstance(pipeline_state, dict):
        return
    combat = pipeline_state.get("combat")
    if not isinstance(combat, dict):
        return
    combat_vehicles = combat.setdefault("vehicles", {})
    for vname, v in (chase_state.get("vehicles") or {}).items():
        if not isinstance(v, dict):
            continue
        cv = combat_vehicles.setdefault(vname, {})
        cv["sdp_current"] = v.get("sdp_current", cv.get("sdp_current", 0))
        cv["sdp_max"] = v.get("sdp_max", cv.get("sdp_max", 0))
        cv["sp"] = v.get("sp", cv.get("sp", 0))
        cv["combat_move"] = v.get("combat_speed_move", cv.get("combat_move", 0))
        cv["driver"] = v.get("operator", cv.get("driver", ""))
        cv["occupants"] = list(v.get("occupants") or cv.get("occupants", []))
        cv["status"] = v.get("status", cv.get("status", "active"))
        cv["type"] = v.get("type", cv.get("type", "land"))
        cv["upgrades"] = list(v.get("upgrades") or cv.get("upgrades", []))
