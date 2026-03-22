"""
Chats game system — plain chat mode with no TTRPG mechanics.
Single-agent only, no pipeline, no game state, no base_instructions.
Uses token-based sawtooth trimming (old method).
"""

GAME_SYSTEM = {
    "id": "chats",
    "display_name": "Chats",

    # No contracts — plain chat mode
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
    "trimming": "token",

    # When project has no instructions.di, fall back to user-level instructions.di
    "fallback_to_user_instructions": True,
}
