"""
Novels game system — writing/editing mode with no TTRPG mechanics.
Single-agent only, no pipeline, no game state, no base_instructions.
"""

GAME_SYSTEM = {
    "id": "novels",
    "display_name": "Novels",

    # No contracts — pure writing mode
    "events_contract": "",
    "mechanics_contract": "",
    "narration_contract": "",
    "single_agent_contract": "",

    # No state tracking
    "state_report_tool": None,
    "init_game_state": lambda: {},
    "apply_game_state": lambda state, ops, turn=None: state,
    "build_game_injection": lambda state, **kw: "",

    # No combat
    "combat_contract": None,
    "combat_tool": None,

    # Feature flags
    "use_pipeline": False,
    "use_game_state": False,
    "use_base_instructions": False,
    "trimming": "pair",

    # Fallback when project has no instructions.di
    "default_instructions": "You're a best-selling novelist, not a helpful chatbot.",
}
