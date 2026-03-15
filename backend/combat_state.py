"""Shared combat-state helpers used across pipelines and game systems."""

from __future__ import annotations


BACKEND_OWNED_COMBAT_KEYS = ("vehicles", "cover", "context", "start_message_id")


def replace_combat_dict_preserving_backend_keys(pipeline_state: dict, new_combat) -> None:
    """Replace pipeline_state['combat'] and preserve backend-owned combat keys.

    Backend-owned keys are authoritative persisted state and should survive
    model-provided combat payload replacement.
    """
    if not isinstance(pipeline_state, dict):
        return
    old_combat = pipeline_state.get("combat")
    saved = {}
    if isinstance(old_combat, dict):
        for key in BACKEND_OWNED_COMBAT_KEYS:
            val = old_combat.get(key)
            if val is not None:
                saved[key] = val
    pipeline_state["combat"] = new_combat
    if saved and isinstance(new_combat, dict):
        for key, val in saved.items():
            pipeline_state["combat"][key] = val
