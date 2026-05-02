"""Tests for cpred_chase (Hot Pursuit chase resolver)."""

import pytest

from game_systems.cpred_chase import (
    DEFAULT_GRID_LENGTH,
    HOT_PURSUIT_MANEUVERS,
    MAX_CHASE_SEPARATION_SQUARES,
    POSITIONING_DV_BY_SPEED,
    SQUARE_RANGE_BANDS,
    apply_collision_damage,
    apply_maneuver,
    build_chase_injection,
    check_chase_end,
    determine_exit_mode,
    effective_combat_speed,
    end_chase,
    end_of_round_cleanup,
    grid_distance,
    init_chase_from_combat,
    init_chase_state,
    lower_speed_category,
    positioning_dv_for_speed,
    resolve_positioning_check,
    squares_to_range_band,
    vehicles_adjacent,
)


# --- Reference-table sanity checks ------------------------------------------

def test_range_band_table_matches_hot_pursuit_p2():
    assert SQUARE_RANGE_BANDS[1] == (0, 6)
    assert SQUARE_RANGE_BANDS[2] == (7, 12)
    assert SQUARE_RANGE_BANDS[3] == (13, 25)
    assert SQUARE_RANGE_BANDS[4] == (26, 50)
    assert SQUARE_RANGE_BANDS[5] == (51, 100)
    assert SQUARE_RANGE_BANDS[6] == (101, 200)
    assert SQUARE_RANGE_BANDS[7] == (201, 400)
    assert SQUARE_RANGE_BANDS[8] == (401, 800)


def test_positioning_dv_table_matches_hot_pursuit_p3():
    expected = [(60, 13), (40, 15), (20, 17), (15, 21), (10, 24), (8, 29)]
    assert POSITIONING_DV_BY_SPEED == expected


def test_positioning_dv_for_speed_picks_correct_bucket():
    # Canonical CP:R vehicle MOVE values bucket exactly.
    assert positioning_dv_for_speed(60) == 13
    assert positioning_dv_for_speed(40) == 15
    assert positioning_dv_for_speed(20) == 17
    assert positioning_dv_for_speed(15) == 21
    assert positioning_dv_for_speed(10) == 24
    assert positioning_dv_for_speed(8) == 29
    # Non-canonical values round DOWN to the nearest category (more punishing).
    # 35 < 40 -> falls to 20 MOVE bucket -> DV 17.
    assert positioning_dv_for_speed(35) == 17
    # 41 >= 40 -> 40 MOVE bucket -> DV 15.
    assert positioning_dv_for_speed(41) == 15
    # Below 8 = worst DV (caller treats as auto-spinout).
    assert positioning_dv_for_speed(7) == 29


def test_squares_to_range_band():
    assert squares_to_range_band(1) == (0, 6)
    assert squares_to_range_band(8) == (401, 800)
    assert squares_to_range_band(9) is None
    assert squares_to_range_band(0) == (0, 0)
    assert squares_to_range_band(-1) == (0, 0)


def test_lower_speed_category():
    assert lower_speed_category(60) == 40
    assert lower_speed_category(40) == 20
    assert lower_speed_category(20) == 15
    assert lower_speed_category(15) == 10
    assert lower_speed_category(10) == 8
    # Dropping from 8 would put us below 8 = spin-out signal.
    assert lower_speed_category(8) is None


def test_hot_pursuit_maneuver_dvs_match_supplement():
    assert HOT_PURSUIT_MANEUVERS["nos"]["dv"] == 13
    assert HOT_PURSUIT_MANEUVERS["pit"]["dv"] == 15
    assert HOT_PURSUIT_MANEUVERS["pull_ahead"]["dv"] == 17
    assert HOT_PURSUIT_MANEUVERS["pull_in_close"]["dv"] == 13
    assert HOT_PURSUIT_MANEUVERS["ramming"]["dv"] == 17


# --- State init -------------------------------------------------------------

def test_init_chase_state_defaults():
    s = init_chase_state()
    assert s["active"] is True
    assert s["round"] == 1
    assert s["grid_length"] == DEFAULT_GRID_LENGTH
    assert s["vehicles"] == {}
    assert s["ended"] is False
    assert s["narrative_summary"] is None


def test_init_chase_state_with_vehicles():
    s = init_chase_state(vehicles={
        "Crew Car": {
            "operator": "Kessler",
            "square": 0,
            "combat_speed_move": 40,
            "sdp_max": 50,
        },
        "Phoenix Lead": {
            "operator": "Phoenix Driver 1",
            "square": 3,
            "combat_speed_move": 40,
            "sdp_max": 50,
            "is_pursuer": True,
        },
    })
    assert "Crew Car" in s["vehicles"]
    assert s["vehicles"]["Crew Car"]["sdp_current"] == 50  # defaulted to max
    assert s["vehicles"]["Phoenix Lead"]["is_pursuer"] is True


def test_init_chase_from_combat_carries_vehicle_state():
    combat = {
        "vehicles": {
            "Crew Sedan": {
                "sdp_current": 35,
                "sdp_max": 50,
                "sp": 7,
                "combat_move": 40,
                "driver": "Kessler",
                "occupants": ["Kessler", "RedVelvet", "Delphi"],
                "type": "land",
                "status": "active",
            },
        },
    }
    chase = init_chase_from_combat(combat, chase_info={
        "starting_squares": {"Crew Sedan": 2},
    })
    veh = chase["vehicles"]["Crew Sedan"]
    assert veh["sdp_current"] == 35
    assert veh["combat_speed_move"] == 40
    assert veh["operator"] == "Kessler"
    assert veh["occupants"] == ["Kessler", "RedVelvet", "Delphi"]
    assert veh["square"] == 2
    assert chase["started_from"] == "combat"


# --- Positioning Checks -----------------------------------------------------

def test_positioning_check_success_advances_one_square():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 40, "sdp_max": 50}
    })
    # 40 MOVE -> DV 15. Skill 8 + roll 7 = 15 -> success.
    r = resolve_positioning_check(s, "X", operator_skill_total=8, d10_roll=7)
    assert r["success"] is True
    assert r["dv"] == 15
    assert r["moved"] == 1
    assert s["vehicles"]["X"]["square"] == 1


def test_positioning_check_failure_holds_position():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 5, "combat_speed_move": 40, "sdp_max": 50}
    })
    r = resolve_positioning_check(s, "X", operator_skill_total=5, d10_roll=2)
    assert r["success"] is False
    assert r["moved"] == 0
    assert s["vehicles"]["X"]["square"] == 5


def test_positioning_check_maintain_no_move_no_check():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 4, "combat_speed_move": 40, "sdp_max": 50}
    })
    r = resolve_positioning_check(s, "X", operator_skill_total=0, d10_roll=0, intent="maintain")
    assert r["success"] is True
    assert r["dv"] is None
    assert r["moved"] == 0


def test_positioning_check_fall_back_decrements_square():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 4, "combat_speed_move": 40, "sdp_max": 50}
    })
    r = resolve_positioning_check(s, "X", operator_skill_total=0, d10_roll=0, intent="fall_back")
    assert r["moved"] == -1
    assert s["vehicles"]["X"]["square"] == 3


def test_positioning_check_fall_back_clamps_at_zero():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 40, "sdp_max": 50}
    })
    resolve_positioning_check(s, "X", operator_skill_total=0, d10_roll=0, intent="fall_back")
    assert s["vehicles"]["X"]["square"] == 0


# --- SDP cascade ------------------------------------------------------------

def test_sdp_halving_drops_speed_one_category():
    # 40 MOVE vehicle at 24/50 SDP (< half) -> rolls as 20 MOVE -> DV 17.
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 40,
              "sdp_current": 24, "sdp_max": 50}
    })
    eff = effective_combat_speed(s["vehicles"]["X"])
    assert eff == 20
    r = resolve_positioning_check(s, "X", operator_skill_total=10, d10_roll=7)  # 17
    assert r["dv"] == 17
    assert r["success"] is True


def test_sdp_halving_can_chain_to_spinout():
    # 10 MOVE vehicle, dropped one cat by SDP halving = 8 MOVE.
    # If a PIT also drops it: 8 -> below 8 -> spin out.
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 10,
              "sdp_current": 4, "sdp_max": 50}  # < 1/2 SDP
    })
    s["vehicles"]["X"]["_pit_speed_drops_this_round"] = 1
    eff = effective_combat_speed(s["vehicles"]["X"])
    assert eff == -1  # signal spin-out
    r = resolve_positioning_check(s, "X", operator_skill_total=20, d10_roll=10)
    assert r["crashed"] is True
    assert s["vehicles"]["X"]["status"] == "crashed"


def test_apply_collision_damage_crosses_half_threshold():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 40,
              "sdp_current": 50, "sdp_max": 50}
    })
    r = apply_collision_damage(s, "X", 30)
    assert r["sdp_after"] == 20
    assert r["crossed_half"] is True
    assert r["disabled"] is False


def test_apply_collision_damage_disables_at_zero():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0, "combat_speed_move": 40,
              "sdp_current": 10, "sdp_max": 50}
    })
    r = apply_collision_damage(s, "X", 50)
    assert r["sdp_after"] == 0
    assert r["disabled"] is True
    assert s["vehicles"]["X"]["status"] == "disabled"


# --- Maneuvers --------------------------------------------------------------

def test_nos_auto_advances_and_runs_control_check():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 2, "combat_speed_move": 40, "sdp_max": 50}
    })
    r = apply_maneuver(s, "X", "nos", operator_skill_total=10, d10_roll=5)
    assert r["success"] is True  # 15 vs DV 13
    assert s["vehicles"]["X"]["square"] == 3  # advanced regardless


def test_nos_failed_control_flags_penalty():
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 2, "combat_speed_move": 40, "sdp_max": 50}
    })
    r = apply_maneuver(s, "X", "nos", operator_skill_total=2, d10_roll=3)  # 5 vs 13
    assert r["success"] is False
    assert s["vehicles"]["X"]["square"] == 3  # still auto-advances
    assert s["vehicles"]["X"]["_nos_lost_control"] is True


def test_pit_requires_adjacency():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 5, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = apply_maneuver(s, "A", "pit", target_vehicle_name="B",
                        operator_skill_total=20, d10_roll=10)
    assert "error" in r


def test_pit_success_drops_target_speed_cat_for_round():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 1, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = apply_maneuver(s, "A", "pit", target_vehicle_name="B",
                        operator_skill_total=10, d10_roll=5)  # 15 vs DV 15
    assert r["success"] is True
    assert s["vehicles"]["B"]["_pit_speed_drops_this_round"] == 1
    # B's effective speed for end-of-round Positioning Check is one cat lower.
    assert effective_combat_speed(s["vehicles"]["B"]) == 20


def test_pull_ahead_success_jumps_past_target():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 5, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 6, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = apply_maneuver(s, "A", "pull_ahead", target_vehicle_name="B",
                        operator_skill_total=12, d10_roll=8)  # 20 vs DV 17
    assert r["success"] is True
    assert s["vehicles"]["A"]["square"] == 7  # target square + 1


def test_pull_in_close_drops_to_lower_band_edge():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 3, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = apply_maneuver(s, "A", "pull_in_close", operator_skill_total=8, d10_roll=5)
    assert r["success"] is True  # 13 vs DV 13
    assert s["vehicles"]["A"]["_pull_in_close_until_end_of_round"] is True


def test_unknown_maneuver_returns_error():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = apply_maneuver(s, "A", "barrel_roll")
    assert "error" in r


# --- End-of-round cleanup ---------------------------------------------------

def test_end_of_round_cleanup_resets_round_flags():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 1, "combat_speed_move": 40, "sdp_max": 50},
    })
    s["vehicles"]["A"]["_pull_in_close_until_end_of_round"] = True
    s["vehicles"]["B"]["_pit_speed_drops_this_round"] = 2
    s["round_resolution"] = [{"narration": "X"}]
    end_of_round_cleanup(s)
    assert s["vehicles"]["A"]["_pull_in_close_until_end_of_round"] is False
    assert s["vehicles"]["B"]["_pit_speed_drops_this_round"] == 0
    assert s["round_resolution"] == []
    assert s["round"] == 2


# --- Chase-end conditions ---------------------------------------------------

def test_check_chase_end_max_separation():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 9, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = check_chase_end(s)
    assert r is not None
    assert r["reason"] == "max_separation"
    assert r["distance_squares"] == 9


def test_check_chase_end_within_range_returns_none():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 5, "combat_speed_move": 40, "sdp_max": 50},
    })
    assert check_chase_end(s) is None


def test_check_chase_end_disabled_vehicle():
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50,
              "status": "disabled"},
        "B": {"operator": "Tgt", "square": 2, "combat_speed_move": 40, "sdp_max": 50},
    })
    r = check_chase_end(s)
    assert r["reason"] == "vehicle_disabled"
    assert r["vehicle"] == "A"


def test_determine_exit_mode_routes_to_combat_when_hostiles_alive():
    assert determine_exit_mode({}, hostile_combatants_alive=True) == "combat"
    assert determine_exit_mode({}, hostile_combatants_alive=False) == "general"


def test_determine_exit_mode_max_separation_short_circuits_to_general():
    """P1 fix: max_separation means the chase is over by escape; even if
    hostile occupants are still 'alive' (in distant vehicles), they're
    disengaged and shouldn't trigger combat mode."""
    assert determine_exit_mode(
        {}, hostile_combatants_alive=True, end_reason="max_separation"
    ) == "general"
    assert determine_exit_mode(
        {}, hostile_combatants_alive=False, end_reason="max_separation"
    ) == "general"


def test_determine_exit_mode_voluntary_stop_short_circuits_to_general():
    """P1 fix: voluntary_stop means everyone agreed to stop. Don't escalate
    to combat even if hostiles are technically present."""
    assert determine_exit_mode(
        {}, hostile_combatants_alive=True, end_reason="voluntary_stop"
    ) == "general"


def test_determine_exit_mode_reversed_to_combat_explicit():
    """P1 fix: reversed_to_combat is the explicit 'switch to combat' signal
    per Hot Pursuit p.5."""
    assert determine_exit_mode(
        {}, hostile_combatants_alive=False, end_reason="reversed_to_combat"
    ) == "combat"


def test_determine_exit_mode_disabled_consults_hostile_heuristic():
    """P1 fix: vehicle_disabled is ambiguous — falls back to hostile-alive
    heuristic (the operators on foot are still hostile)."""
    assert determine_exit_mode(
        {}, hostile_combatants_alive=True, end_reason="vehicle_disabled"
    ) == "combat"
    assert determine_exit_mode(
        {}, hostile_combatants_alive=False, end_reason="vehicle_disabled"
    ) == "general"


def test_determine_exit_mode_reads_end_reason_from_chase_state():
    """P2 fix: when end_reason kwarg is None, function reads chase_state['end_reason']."""
    assert determine_exit_mode(
        {"end_reason": "max_separation"}, hostile_combatants_alive=True
    ) == "general"
    assert determine_exit_mode(
        {"end_reason": "reversed_to_combat"}, hostile_combatants_alive=False
    ) == "combat"


def test_apply_chase_state_max_separation_routes_to_general():
    """P1 fix: when chase auto-ends via max_separation with pursuer occupants
    still 'alive' in distant vehicles, exit_target_mode should be 'general'
    (escaped pursuers aren't engaging)."""
    from game_systems.cpred_chase import apply_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "Crew": {"operator": "K", "square": 0,
                     "combat_speed_move": 40, "sdp_max": 50,
                     "is_pursuer": False, "occupants": ["K", "Red"]},
            "Phoenix": {"operator": "P", "square": 9,  # 9 sq apart -> max_sep
                        "combat_speed_move": 40, "sdp_max": 50,
                        "is_pursuer": True, "occupants": ["P"], "status": "active"},
        }),
        "character_states": {},
        "game_state": {"edgerunners": {"K": {}, "Red": {}}},
    }
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "chase_complete": False,
    }, game_state=pipeline_state["game_state"])
    chase = pipeline_state["chase"]
    assert chase["end_reason"] == "max_separation"
    assert chase["exit_target_mode"] == "general", \
        "max_separation should route to general even with active pursuers"


def test_end_chase_stamps_handoff_fields():
    s = init_chase_state()
    end_chase(s, reason="max_separation",
              narrative_summary="Got away clean.",
              exit_target_mode="general")
    assert s["active"] is False
    assert s["ended"] is True
    assert s["end_reason"] == "max_separation"
    assert s["narrative_summary"] == "Got away clean."
    assert s["exit_target_mode"] == "general"


# --- Adjacency / distance ---------------------------------------------------

def test_grid_distance_and_adjacency():
    a = {"square": 3}
    b = {"square": 3}
    c = {"square": 4}
    d = {"square": 7}
    assert grid_distance(a, b) == 0
    assert vehicles_adjacent(a, b) is True
    assert vehicles_adjacent(a, c) is True
    assert vehicles_adjacent(a, d) is False


# --- Injection builder ------------------------------------------------------

def test_build_chase_injection_active_renders_grid_and_distances():
    s = init_chase_state(vehicles={
        "Crew Sedan": {"operator": "Kessler", "square": 4,
                       "combat_speed_move": 40, "sdp_max": 50},
        "Phoenix Lead": {"operator": "Phoenix Driver", "square": 6,
                          "combat_speed_move": 40, "sdp_max": 50,
                          "is_pursuer": True},
    })
    out = build_chase_injection(s, {})
    assert "[CHASE STATE]" in out
    assert "Round: 1" in out
    assert "Crew Sedan" in out
    assert "Phoenix Lead" in out
    assert "2 sq" in out  # distance between them
    assert "7-12 m" in out  # range band for 2 sq


def test_apply_maneuver_appends_to_round_resolution():
    """P1 fix: maneuver outcomes must surface in build_chase_injection's
    'this round so far' log via round_resolution."""
    s = init_chase_state(vehicles={
        "A": {"operator": "Op", "square": 0, "combat_speed_move": 40, "sdp_max": 50},
        "B": {"operator": "Tgt", "square": 1, "combat_speed_move": 40, "sdp_max": 50},
    })
    apply_maneuver(s, "A", "pit", target_vehicle_name="B",
                    operator_skill_total=10, d10_roll=6)  # 16 vs DV 15 -> success
    assert len(s["round_resolution"]) == 1
    entry = s["round_resolution"][0]
    assert entry["vehicle"] == "A"
    assert "PIT" in entry["narration"]


def test_apply_chase_state_handles_missing_game_state():
    """P1 fix: apply_chase_state must always invoke _apply_meatspace_shared
    even when game_state=None (helper handles it; HP updates shouldn't be
    silently dropped)."""
    from game_systems.cpred_chase import apply_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "A": {"operator": "Op", "square": 0, "combat_speed_move": 40,
                  "sdp_max": 50, "occupants": ["Op"]},
        }),
        "character_states": {"Op": {"data": {"vitals": [{"label": "HP", "current": 30, "max": 30}]}}},
    }
    # game_state intentionally None
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "chase_complete": False,
    }, game_state=None)
    # Doesn't crash; chase still active.
    assert pipeline_state["chase"]["active"] is True


def test_apply_chase_state_auto_end_sets_active_false_and_summary():
    """P0 fix: when check_chase_end auto-detects ended (e.g. separation>8),
    apply_chase_state must call end_chase() so active=False AND
    narrative_summary are set — otherwise the mode-end detection in main.py
    never fires and the chase becomes a zombie."""
    from game_systems.cpred_chase import apply_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "Crew": {"operator": "K", "square": 0,
                     "combat_speed_move": 40, "sdp_max": 50,
                     "is_pursuer": False},
            "Phoenix": {"operator": "P", "square": 9,
                        "combat_speed_move": 40, "sdp_max": 50,
                        "is_pursuer": True},
        }),
    }
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "chase_complete": False,  # model didn't signal — engine must auto-detect
    }, game_state=None)
    chase = pipeline_state["chase"]
    assert chase["active"] is False, "active should be False after auto-detect end"
    assert chase["narrative_summary"], "narrative_summary should be auto-stamped"
    assert chase["end_reason"] == "max_separation"
    assert chase["exit_target_mode"] in ("combat", "general")


def test_check_chase_end_with_pursuer_flags_continues_when_other_pursuer_alive():
    """P1 fix: with multi-pursuer chases, disabling one pursuer doesn't end
    the chase if other pursuers are still active."""
    s = init_chase_state(vehicles={
        "Crew": {"operator": "K", "square": 4,
                 "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": False},
        "Phoenix Lead": {"operator": "P1", "square": 3,
                         "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": True,
                         "status": "disabled"},
        "Phoenix Trailing": {"operator": "P2", "square": 2,
                             "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": True},
    })
    result = check_chase_end(s)
    assert result is None, "chase should continue while other pursuer alive"


def test_check_chase_end_with_pursuer_flags_all_pursuers_disabled():
    """P1 fix: when EVERY pursuer is disabled, chase ends."""
    s = init_chase_state(vehicles={
        "Crew": {"operator": "K", "square": 4,
                 "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": False},
        "Phoenix Lead": {"operator": "P1", "square": 3,
                         "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": True,
                         "status": "disabled"},
        "Phoenix Trailing": {"operator": "P2", "square": 2,
                             "combat_speed_move": 40, "sdp_max": 50, "is_pursuer": True,
                             "status": "crashed"},
    })
    result = check_chase_end(s)
    assert result is not None
    assert result["reason"] == "all_pursuers_disabled"


def test_pull_in_close_collapses_range_band_to_lower_edge():
    """P1 fix: Pull in Close should pull the range band to its lower edge
    (e.g. 7m instead of 7-12m for 2-square distance)."""
    band_normal = squares_to_range_band(2)
    assert band_normal == (7, 12)
    band_pic = squares_to_range_band(2, pull_in_close=True)
    assert band_pic == (7, 7)


def test_nos_lost_control_adds_dv_modifier_to_next_positioning_check():
    """P1 fix: a failed NOS control check should impose +2 DV on the next
    Positioning Check via the _nos_lost_control flag."""
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50}
    })
    s["vehicles"]["X"]["_nos_lost_control"] = True
    r = resolve_positioning_check(s, "X", operator_skill_total=8, d10_roll=8)  # 16
    # 40 MOVE -> DV 15. With +2 mod -> DV 17. 16 < 17 -> failure.
    assert r["dv"] == 17
    assert r["dv_modifier"] == 2
    assert r["success"] is False


def test_apply_chase_state_reads_operator_initiative_and_current_turn():
    """P1 fix: schema declares operator_initiative + current_turn — the
    apply must persist them so they're not silently dropped."""
    from game_systems.cpred_chase import apply_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "X": {"operator": "Op", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50}
        }),
    }
    apply_chase_state(pipeline_state, {
        "operator_initiative": {"Op": 14},
        "current_turn": "Op",
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
    }, game_state=None)
    chase = pipeline_state["chase"]
    assert chase["operator_initiative"] == {"Op": 14}
    assert chase["current_turn"] == "Op"


def test_apply_chase_state_strips_chase_specific_fields_from_meatspace_helper():
    """P0 fix: schema overlap — chase's vehicle_updates schema must NOT be
    fed to _apply_meatspace_shared which expects combat-shape vehicle_updates.
    Reaching this test means apply_chase_state didn't crash on cross-schema
    vehicle_updates."""
    from game_systems.cpred_chase import apply_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "Crew": {"operator": "K", "square": 0,
                     "combat_speed_move": 40, "sdp_max": 50,
                     "occupants": ["K"]},
        }),
        "combat": {"vehicles": {}},  # combat slot exists; would be cross-fed
    }
    # This was the bug: chase's vehicle_updates with sdp_damage would get
    # mis-applied as combat-shape vehicle_updates.
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [
            {"vehicle": "Crew", "sdp_damage": 5}
        ],
        "character_updates": [],
    }, game_state=None)
    chase_veh = pipeline_state["chase"]["vehicles"]["Crew"]
    # Chase damage was applied (45 = 50 - 5).
    assert chase_veh["sdp_current"] == 45
    # Combat['vehicles'] was NOT cross-contaminated.
    assert pipeline_state["combat"]["vehicles"] == {}


def test_init_chase_from_plot_encounter_seeds_state():
    """P0 fix: plot encounter materializer feeds into init_chase_from_plot_encounter
    so chase mode can be activated from a kind=vehicle entry."""
    from game_systems.cpred_chase import init_chase_from_plot_encounter
    materialized = {
        "scene": "Test scene",
        "chase_grid": {"length_squares": 8},
        "vehicles": {
            "A": {
                "operator": "Op",
                "occupants": ["Op"],
                "square": 4,
                "combat_speed_move": 40,
                "sdp_max": 50,
                "sdp_current": 50,
                "sp": 7,
                "type": "land",
                "is_pursuer": False,
            },
        },
    }
    chase = init_chase_from_plot_encounter(materialized)
    assert chase["active"] is True
    assert chase["grid_length"] == 8
    assert "A" in chase["vehicles"]
    assert chase["vehicles"]["A"]["combat_speed_move"] == 40
    assert chase["started_from"] == "plot_encounter"


def test_synthesize_chase_handoff_from_encounter_produces_paragraphs():
    """P1 fix: handoff prose synthesized from materialized YAML data so
    Plot-Encounter-triggered chases get a 2-3 paragraph context without an
    LLM call."""
    from game_systems.cpred_chase import synthesize_chase_handoff_from_encounter
    materialized = {
        "scene": "S2 Beat 6: The Extraction",
        "route": "Lower Kabuki -> Watson border -> Watson safehouse",
        "vehicles": {
            "Crew Sedan": {"is_pursuer": False},
            "Phoenix Lead": {"is_pursuer": True},
            "Phoenix Trailing": {"is_pursuer": True},
        },
        "legs": [
            {"name": "Leg 1", "description": "Lower Kabuki narrow streets"},
            {"name": "Leg 2", "description": "Highway interchange"},
        ],
        "victory_condition": "Reach Watson safehouse",
        "failure_condition": "Crew vehicle disabled while pursuers active",
        "escalation_triggers": ["Phoenix vehicles disabled = decisive"],
    }
    handoff = synthesize_chase_handoff_from_encounter(materialized)
    assert "S2 Beat 6" in handoff
    assert "Lower Kabuki" in handoff
    assert "Watson safehouse" in handoff
    assert "Crew Sedan" in handoff
    assert "Phoenix Lead" in handoff
    # Should be multi-paragraph
    assert handoff.count("\n\n") >= 1


def test_init_chase_from_plot_encounter_synthesizes_context_when_missing():
    """P1 fix: init_chase_from_plot_encounter auto-synthesizes context if
    none is supplied — the chase state has a real handoff, not just a
    label."""
    from game_systems.cpred_chase import init_chase_from_plot_encounter
    materialized = {
        "scene": "Test scene",
        "route": "Point A -> Point B",
        "chase_grid": {"length_squares": 8},
        "vehicles": {
            "A": {"operator": "X", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50},
        },
        "victory_condition": "Reach Point B",
        "legs": [{"name": "L1", "description": "Open road"}],
    }
    chase = init_chase_from_plot_encounter(materialized)
    assert "Point A" in chase["context"]
    assert "Reach Point B" in chase["context"]
    # Route is stashed for HUD overlay.
    assert chase["route"] == "Point A -> Point B"


def test_chase_location_label_active_returns_route_label():
    """P1 fix: HUD location updates during chase via chase_location_label."""
    from game_systems.cpred_chase import (
        init_chase_state, init_chase_from_plot_encounter, chase_location_label,
    )
    chase = init_chase_from_plot_encounter({
        "scene": "Test",
        "route": "A -> B",
        "chase_grid": {"length_squares": 8},
        "vehicles": {
            "A": {"operator": "X", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50},
        },
    })
    assert chase_location_label(chase) == "Vehicle Chase: A -> B"
    chase["active"] = False
    assert chase_location_label(chase) is None


def test_apply_chase_state_overlays_hud_location_and_stashes_pre_chase():
    """P1 fix: apply_chase_state stamps chase route into hud_state.location
    and stashes the pre-chase location for restoration on mode end."""
    from game_systems.cpred_chase import apply_chase_state, init_chase_from_plot_encounter
    chase = init_chase_from_plot_encounter({
        "scene": "S",
        "route": "Lower Kabuki -> Watson",
        "chase_grid": {"length_squares": 8},
        "vehicles": {
            "A": {"operator": "X", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50},
        },
    })
    pipeline_state = {
        "chase": chase,
        "hud_state": {"location": "Mama Lu's, Watson"},
    }
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "chase_complete": False,
    }, game_state=None)
    assert pipeline_state["hud_state"]["location"] == "Vehicle Chase: Lower Kabuki -> Watson"
    assert pipeline_state["chase"]["_pre_chase_location"] == "Mama Lu's, Watson"


def test_hostile_combatants_alive_walks_chase_pursuer_occupants():
    """P1 fix: chase pursuer-vehicle occupants count as hostile even when not
    in combat.initiative_order or character_states."""
    from game_systems.cpred_chase import _hostile_combatants_alive
    pipeline_state = {
        "combat": None,
        "character_states": {},
        "game_state": {"edgerunners": {"RedVelvet": {}}},
        "chase": {
            "vehicles": {
                "Phoenix Lead": {
                    "is_pursuer": True,
                    "status": "active",
                    "occupants": ["Phoenix Driver", "Phoenix Shooter A"],
                },
                "Crew Sedan": {
                    "is_pursuer": False,
                    "status": "active",
                    "occupants": ["RedVelvet", "Kessler"],
                },
            },
        },
    }
    assert _hostile_combatants_alive(pipeline_state) is True


def test_hostile_combatants_alive_returns_false_when_all_pursuers_disabled():
    """P1 fix: when all pursuer vehicles are disabled and no other hostiles,
    returns False -> exit_target_mode = general."""
    from game_systems.cpred_chase import _hostile_combatants_alive
    pipeline_state = {
        "combat": None,
        "character_states": {},
        "game_state": {"edgerunners": {"RedVelvet": {}}},
        "chase": {
            "vehicles": {
                "Phoenix Lead": {
                    "is_pursuer": True,
                    "status": "disabled",
                    "occupants": ["Phoenix Driver"],
                },
            },
        },
    }
    assert _hostile_combatants_alive(pipeline_state) is False


def test_apply_chase_writeback_no_ops_without_combat():
    """P0 fix: writeback no-ops cleanly when combat slot is None — caller
    decides whether to seed combat. Vehicle damage is intentionally dropped
    when exit is general."""
    from game_systems.cpred_chase import apply_chase_writeback, init_chase_state
    pipeline_state = {
        "combat": None,
        "chase": init_chase_state(vehicles={
            "A": {"operator": "X", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50,
                  "sdp_current": 30},  # damaged
        }),
    }
    apply_chase_writeback(pipeline_state["chase"], pipeline_state)
    # Combat slot stays None.
    assert pipeline_state["combat"] is None


def test_apply_chase_writeback_writes_to_existing_combat():
    """P0 fix: when combat slot exists, writeback syncs vehicle state."""
    from game_systems.cpred_chase import apply_chase_writeback, init_chase_state
    pipeline_state = {
        "combat": {"vehicles": {}},
        "chase": init_chase_state(vehicles={
            "A": {"operator": "X", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50,
                  "sdp_current": 30, "status": "active"},
        }),
    }
    apply_chase_writeback(pipeline_state["chase"], pipeline_state)
    assert "A" in pipeline_state["combat"]["vehicles"]
    assert pipeline_state["combat"]["vehicles"]["A"]["sdp_current"] == 30


def test_state_report_tool_has_chase_trigger():
    """P1 fix: cpred state_report_tool must expose chase_trigger so the
    stateful single-agent path can activate chase from a normal turn."""
    from game_systems.cpred import STATE_REPORT_TOOL
    schema = STATE_REPORT_TOOL["input_schema"]["properties"]
    assert "chase_trigger" in schema, "state_report_tool missing chase_trigger"
    chase_trigger_schema = schema["chase_trigger"]
    # Validate basic shape
    assert chase_trigger_schema["type"] == ["object", "null"]
    props = chase_trigger_schema["properties"]
    assert "vehicles" in props
    assert "context" in props
    assert "pursuer_vehicles" in props


def test_init_chase_from_combat_carries_route_and_scene():
    """P2 fix: combat->chase transitions surface route/scene for HUD overlay."""
    from game_systems.cpred_chase import init_chase_from_combat
    combat = {"vehicles": {
        "Crew Sedan": {"sdp_max": 50, "sdp_current": 50,
                       "combat_move": 40, "driver": "Kessler"},
    }}
    chase = init_chase_from_combat(combat, chase_info={
        "starting_squares": {"Crew Sedan": 0},
        "route": "Lower Kabuki -> Watson",
        "scene": "S2 Beat 6 ad-hoc chase",
    })
    assert chase["route"] == "Lower Kabuki -> Watson"
    assert chase["scene"] == "S2 Beat 6 ad-hoc chase"


def test_apply_chase_state_persists_pending_obstacle():
    """Hot Pursuit p.4 obstacle handling: model sets obstacle_maneuver, engine
    persists it onto chase state for next-round injection."""
    from game_systems.cpred_chase import apply_chase_state, init_chase_state
    pipeline_state = {
        "chase": init_chase_state(vehicles={
            "X": {"operator": "A", "square": 0,
                  "combat_speed_move": 40, "sdp_max": 50},
        }),
    }
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "obstacle_maneuver": {
            "description": "Oncoming tanker truck on Charter Hill ramp",
            "maneuver_type": "swerve",
            "fall_back_on_failure": False,
        },
    }, game_state=None)
    obstacle = pipeline_state["chase"]["pending_obstacle"]
    assert obstacle["description"] == "Oncoming tanker truck on Charter Hill ramp"
    assert obstacle["maneuver_type"] == "swerve"
    assert obstacle["fall_back_on_failure"] is False


def test_obstacle_renders_in_chase_injection():
    """Pending obstacle should surface in [CHASE STATE] block with the
    correct DV for the maneuver type and failure consequence."""
    from game_systems.cpred_chase import build_chase_injection, init_chase_state
    chase = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50},
    })
    chase["pending_obstacle"] = {
        "description": "Construction zone barriers",
        "maneuver_type": "bootleg_turn",
        "fall_back_on_failure": True,
    }
    out = build_chase_injection(chase, {})
    assert "OBSTACLE THIS ROUND" in out
    assert "Construction zone barriers" in out
    assert "DV 17" in out  # Bootleg Turn DV
    assert "fall back 1 sq" in out


def test_apply_chase_state_clears_pending_obstacle_when_explicitly_null():
    """When the model passes obstacle_maneuver: null, engine clears it."""
    from game_systems.cpred_chase import apply_chase_state, init_chase_state
    chase = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50},
    })
    chase["pending_obstacle"] = {"description": "old obstacle", "maneuver_type": "swerve"}
    pipeline_state = {"chase": chase}
    apply_chase_state(pipeline_state, {
        "vehicle_intents": [],
        "maneuvers": [],
        "vehicle_updates": [],
        "character_updates": [],
        "obstacle_maneuver": None,
    }, game_state=None)
    assert "pending_obstacle" not in pipeline_state["chase"]


def test_init_chase_state_includes_all_documented_fields():
    """P2 fix: init_chase_state initializes route, scene, _pre_chase_location
    upfront so the dict matches the schema docstring. Round-scoped vehicle
    flags are all initialized too."""
    s = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50},
    })
    assert "route" in s
    assert "scene" in s
    assert "_pre_chase_location" in s
    # All round-scoped vehicle flags initialized.
    veh = s["vehicles"]["X"]
    assert "_pit_speed_drops_this_round" in veh
    assert "_pull_in_close_until_end_of_round" in veh
    assert "_nos_lost_control" in veh
    assert "_pull_ahead_failed" in veh


def test_stamp_chase_hud_overlay_at_seeding_time():
    """P1 fix: stamp_chase_hud_overlay updates HUD immediately on chase seed,
    not lazily on first apply_chase_state."""
    from game_systems.cpred_chase import (
        init_chase_state,
        stamp_chase_hud_overlay,
    )
    chase = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50},
    })
    chase["route"] = "Lower Kabuki -> Watson"
    pipeline_state = {
        "chase": chase,
        "hud_state": {"location": "Mama Lu's"},
    }
    stamp_chase_hud_overlay(pipeline_state)
    assert pipeline_state["hud_state"]["location"] == "Vehicle Chase: Lower Kabuki -> Watson"
    assert pipeline_state["chase"]["_pre_chase_location"] == "Mama Lu's"


def test_stamp_chase_hud_overlay_idempotent():
    """P2 fix: stamp_chase_hud_overlay is idempotent — second call doesn't
    overwrite the captured pre-chase location."""
    from game_systems.cpred_chase import (
        init_chase_state,
        stamp_chase_hud_overlay,
    )
    chase = init_chase_state(vehicles={
        "X": {"operator": "A", "square": 0,
              "combat_speed_move": 40, "sdp_max": 50},
    })
    chase["route"] = "A -> B"
    pipeline_state = {
        "chase": chase,
        "hud_state": {"location": "Origin Location"},
    }
    stamp_chase_hud_overlay(pipeline_state)
    captured = pipeline_state["chase"]["_pre_chase_location"]
    # Simulate a second stamp (e.g. from apply_chase_state on next exchange).
    pipeline_state["hud_state"]["location"] = "Vehicle Chase: A -> B"
    stamp_chase_hud_overlay(pipeline_state)
    # Pre-chase location is still the original, not the chase label.
    assert pipeline_state["chase"]["_pre_chase_location"] == captured == "Origin Location"


def test_combat_to_chase_preserves_vehicle_state_when_combat_complete():
    """P1 fix: when combat_complete=True AND initiate_chase fire in same tool
    call, the pre-clear vehicle snapshot preserves SDP/SP/MOVE state for the
    chase even though _apply_meatspace_shared cleared the combat slot."""
    from game_systems.cpred_combat import apply_cpred_combat_state
    pipeline_state = {
        "combat": {
            "round": 3,
            "initiative_order": ["Kessler", "Phoenix Op"],
            "vehicles": {
                "Crew Sedan": {
                    "sdp_max": 50,
                    "sdp_current": 32,  # damaged mid-combat
                    "combat_move": 40,
                    "sp": 7,
                    "driver": "Kessler",
                    "occupants": ["Kessler", "RedVelvet"],
                    "type": "land",
                    "status": "active",
                },
            },
        },
        "character_states": {},
        "game_state": {"edgerunners": {}},
        "hud_state": {"location": "Mama Lu's"},
    }
    apply_cpred_combat_state(pipeline_state, {
        "character_updates": [],
        "cover_state": [],
        "combat_complete": True,  # CLEARS combat slot first
        "initiate_chase": {
            "vehicles": {
                "Crew Sedan": {"starting_square": 4},  # No SDP/MOVE supplied
            },
            "context": "Crew flees the scene",
        },
    }, game_state=pipeline_state["game_state"])
    # Combat cleared.
    assert pipeline_state["combat"] is None
    # Chase seeded with vehicle state preserved from pre-clear snapshot.
    chase_veh = pipeline_state["chase"]["vehicles"]["Crew Sedan"]
    assert chase_veh["sdp_current"] == 32  # damaged state survived
    assert chase_veh["sdp_max"] == 50
    assert chase_veh["combat_speed_move"] == 40
    assert chase_veh["sp"] == 7
    assert chase_veh["operator"] == "Kessler"


def test_init_chase_from_combat_pursuer_marker():
    """P2 fix: pursuer_vehicles list explicitly marks pursuers (not derived
    from a buggy starting_squares < 0 heuristic)."""
    combat = {"vehicles": {
        "Crew Car": {"sdp_max": 50, "sdp_current": 50, "combat_move": 40, "driver": "K"},
        "Phoenix Lead": {"sdp_max": 50, "sdp_current": 50, "combat_move": 40, "driver": "P"},
    }}
    chase = init_chase_from_combat(combat, chase_info={
        "starting_squares": {"Crew Car": 4, "Phoenix Lead": 3},
        "pursuer_vehicles": ["Phoenix Lead"],
    })
    assert chase["vehicles"]["Crew Car"]["is_pursuer"] is False
    assert chase["vehicles"]["Phoenix Lead"]["is_pursuer"] is True


def test_build_chase_injection_ended_shows_summary():
    s = init_chase_state()
    end_chase(s, reason="vehicle_disabled",
              narrative_summary="Phoenix lead vehicle in flames; crew breaks contact.",
              exit_target_mode="combat")
    out = build_chase_injection(s, {})
    assert "Chase ended" in out
    assert "vehicle_disabled" in out
    assert "Phoenix lead vehicle in flames" in out
