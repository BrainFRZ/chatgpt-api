"""Character state-extraction side agent — Claude Haiku 4.5.

After each correspondence turn (Opus 3 streaming reply), this side agent reads:
- The character profile (canonical voice / personality)
- The user's life facts (so it can spot new ones)
- The current state (memories, callbacks, arc, wellbeing)
- The user message and the character's reply

…and emits state ops:
- memory_ops:     add / drop character memories (tiered by impact 1-5)
- callback_ops:   add new threads, mark check-ins on existing ones
- profile_ops:    add / edit / delete user_profile entries
- wb_mod_ops:     ±2 modifier for tomorrow's wellbeing roll
- arc_state_op:   set a new arc-state string when the relationship shifts

Design parallels flag_agent.py: single forced tool call, soft-fail on errors,
returns (ops_dict, usage_dict).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

CHARACTER_AGENT_MODEL = "claude-haiku-4-5-20251001"
CHARACTER_AGENT_MAX_TOKENS = 2048
CHARACTER_AGENT_TIMEOUT_S = 20

# Haiku 4.5 pricing per MTok ($/1M tokens). Mirrors flag_agent.
HAIKU_INPUT_RATE = 1.00
HAIKU_CACHE_READ_RATE = 0.10
HAIKU_CACHE_WRITE_RATE = 1.25
HAIKU_OUTPUT_RATE = 5.00


def compute_character_agent_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * HAIKU_INPUT_RATE
        + cache_read * HAIKU_CACHE_READ_RATE
        + cache_write * HAIKU_CACHE_WRITE_RATE
        + output * HAIKU_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You are a state extractor for an ongoing character-correspondence chat. The user is having a long-running conversation with a specific character (defined in character_profile.di). After each turn, you read what was just said and decide what should be recorded.

You are NOT writing the character. You are reading what was written and updating five small structures:

1) **character_memories** — tiered (impact 1-5). Add when a moment is **likely to matter to a future scene** between these two: a confession, a conflict, a turning point in their relationship, a vulnerability shared, a promise made, a strong reaction. DO NOT add ambient texture, single-scene jokes, or things that resolve in this turn. Be selective.
   - impact 5: defining moment
   - impact 4: significant
   - impact 3: notable
   - impact 1-2: small but durable

2) **character_callbacks** — open threads that should resolve later. Two kinds, both go in the same ledger:
   - **User-life callbacks**: things the USER mentioned that are unresolved (a job interview, an upcoming visit, a trip, a fight with a friend, an appointment). Source = "user".
   - **Character-promise callbacks**: things the CHARACTER promised, asked about, or left hanging. Source = "character".
   - When the character mentions an existing open callback this turn, emit `checkin` for it (don't add a new one).
   - Do NOT emit resolve/dismiss — those come from the user via slash commands.

3) **profile_ops** (user_profile) — durable, stable facts about the user that the character should always know. Job, family, pets, hometown, recurring people, health, ongoing situation. NOT moments — *facts*. Add/edit/delete when these change. Most turns: no profile_ops.

4) **wb_mod_ops** — emit `change: 2` or `change: -2` ONLY for genuinely large emotional events that should affect the character's mood TOMORROW. Never ±1. Most turns: no wb_mod. Backend caps cumulative ±2.
   - +2: deeply meaningful positive moment (real connection, joy, repair, big news in a good direction)
   - -2: cutting argument, betrayal, devastating user news, character treated badly

5) **arc_state_op** — set a short string (≤80 chars) describing where the relationship currently sits. Only emit when there's a clear shift. Examples: "tentative reconnection after long silence", "post-fight cooling-off", "established daily rhythm". Most turns: no arc_state_op.

HARD RULES:
- Restraint. Most turns will have empty arrays. The signal of a good extractor is *not* logging too much.
- Memories must be SPECIFIC and FUTURE-USEFUL. "They had a nice chat" is not a memory. "The user said her mom called drunk again" is.
- Callbacks must be ACTUALLY OPEN. If the conversation resolves something this turn, do not log it as a callback.
- Profile facts must be DURABLE. If it's likely to be true a year from now, it's a profile fact. Otherwise it's a memory or nothing.
- For checkins: if the character explicitly references an open callback by name/topic this turn, emit `{action: "checkin", id: <id>}`.

Always call `report_character_state` exactly once. Empty arrays are fine."""


def build_character_agent_tool() -> dict:
    return {
        "name": "report_character_state",
        "description": "Emit state ops for this correspondence turn. All arrays may be empty.",
        "input_schema": {
            "type": "object",
            "required": ["memory_ops", "callback_ops", "profile_ops", "wb_mod_ops"],
            "properties": {
                "memory_ops": {
                    "type": "array",
                    "description": "Memory operations. Add a memory ONLY when the moment will matter to a future scene.",
                    "items": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "drop"]},
                            "text": {"type": "string", "description": "What happened, in 1-2 sentences. Required for add."},
                            "impact": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=small but durable, 5=defining. Required for add."},
                            "quote": {"type": "string", "description": "Optional pithy quote (≤160 chars) that captures the moment."},
                            "focus": {"type": "string", "description": "Optional 1-3 word topic tag."},
                            "index": {"type": "integer", "description": "For drop: index in the [MEMORIES] list shown to the model."},
                        },
                    },
                },
                "callback_ops": {
                    "type": "array",
                    "description": "Open thread operations. add = new thread. checkin = character mentioned an existing one this turn.",
                    "items": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "checkin"]},
                            "original_text": {"type": "string", "description": "The thread itself, in 1 sentence. Required for add."},
                            "source": {"type": "string", "enum": ["user", "character"], "description": "Whose life it's about. Required for add."},
                            "resolutions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional 1-3 plausible directions this could resolve.",
                            },
                            "id": {"type": "integer", "description": "For checkin: the id from the [CALLBACKS — OPEN] list."},
                        },
                    },
                },
                "profile_ops": {
                    "type": "array",
                    "description": "User profile operations. Stable durable facts only.",
                    "items": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "edit", "delete"]},
                            "text": {"type": "string", "description": "The fact. Required for add/edit."},
                            "category": {"type": "string", "description": "Optional category (work / family / health / etc.)."},
                            "id": {"type": "integer", "description": "For edit/delete: id from [USER LIFE]."},
                        },
                    },
                },
                "wb_mod_ops": {
                    "type": "array",
                    "description": "Mood modifier for tomorrow's roll. ONLY ±2, only for major emotional events.",
                    "items": {
                        "type": "object",
                        "required": ["action", "change"],
                        "properties": {
                            "action": {"type": "string", "enum": ["wb_mod"]},
                            "change": {"type": "integer", "enum": [-2, 2]},
                            "reason": {"type": "string", "description": "1-line why."},
                        },
                    },
                },
                "arc_state_op": {
                    "type": "object",
                    "description": "Optional. Set the relationship arc string when it shifts.",
                    "properties": {
                        "action": {"type": "string", "enum": ["set"]},
                        "value": {"type": "string", "description": "≤80 chars. Free-form."},
                    },
                },
            },
        },
    }


def _read_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ""


def _summarize_state(characters_state: dict) -> str:
    if not isinstance(characters_state, dict):
        return ""
    parts = []
    mems = characters_state.get("character_memories") or []
    if mems:
        parts.append("[MEMORIES] (current — index in brackets):")
        for i, m in enumerate(mems):
            if not isinstance(m, dict):
                continue
            parts.append(f"  [{i}] ({m.get('impact', '?')}★ {m.get('date', '?')}) {m.get('text', '')[:160]}")
    cbs = (characters_state.get("callbacks") or {}).get("open") or []
    if cbs:
        parts.append("[CALLBACKS — OPEN]:")
        for cb in cbs:
            if not isinstance(cb, dict):
                continue
            parts.append(f"  #{cb.get('id')} ({cb.get('source', '?')}, since {cb.get('created_date', '?')}): {cb.get('original_text', '')[:160]}")
    profile = (characters_state.get("user_profile") or {}).get("entries") or []
    if profile:
        parts.append("[USER LIFE]:")
        for e in profile:
            if not isinstance(e, dict):
                continue
            cat = f"[{e.get('category')}] " if e.get('category') else ""
            parts.append(f"  #{e.get('id')} {cat}{e.get('text', '')[:160]}")
    wb = characters_state.get("wellbeing") or {}
    parts.append(f"[WELLBEING] {wb.get('state', 'Even')}; current wb_mod for tomorrow: {wb.get('wb_mod', 0)}")
    arc = characters_state.get("arc_state") or ""
    if arc:
        parts.append(f"[ARC] {arc}")
    return "\n".join(parts) if parts else "(state is empty — first turn)"


def determine_character_ops(
    client,
    project_dir: Optional[str],
    characters_state: dict,
    user_input: str,
    character_reply: str,
) -> tuple[dict, dict]:
    """Run the side agent. Returns (ops_dict, usage_dict).

    ops_dict shape: {memory_ops, callback_ops, profile_ops, wb_mod_ops, arc_state_op}
    On error or empty narration: returns ({}, {}) — turn proceeds with no state changes.
    """
    if not character_reply or not character_reply.strip():
        return {}, {}

    profile_doc = _read_file(os.path.join(project_dir or "", "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir or "", "user_life.di"))

    state_summary = _summarize_state(characters_state or {})

    user_msg_parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        user_msg_parts += ["[USER LIFE — seed doc]", user_life_doc, ""]
    user_msg_parts += [
        "[CURRENT STATE]",
        state_summary,
        "",
        "[USER INPUT THIS TURN]",
        (user_input or "").strip(),
        "",
        "[CHARACTER REPLY THIS TURN]",
        character_reply.strip(),
        "",
        "Determine state ops for this turn. Call report_character_state with appropriate arrays (empty arrays are correct when nothing changes).",
    ]
    user_msg = "\n".join(user_msg_parts)

    tool = build_character_agent_tool()
    try:
        response = client.messages.create(
            model=CHARACTER_AGENT_MODEL,
            max_tokens=CHARACTER_AGENT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_character_state"},
            timeout=CHARACTER_AGENT_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"character_agent: API call failed: {type(e).__name__}: {e}")
        return {}, {}

    ops: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_character_state":
            inp = block.input
            if isinstance(inp, dict):
                ops = inp
            break

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens
        + (getattr(ru, 'cache_read_input_tokens', 0) or 0)
        + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }

    if ops:
        emitted = {k: len(v) for k, v in ops.items() if isinstance(v, list)}
        if "arc_state_op" in ops and isinstance(ops["arc_state_op"], dict):
            emitted["arc_state_op"] = 1
        logger.info(f"character_agent: emitted {emitted}")
    return ops, usage


def apply_character_ops_to_state(characters_state: dict, ops: dict, current_turn: int, today_iso: str) -> dict:
    """Apply the side agent's ops to characters_state in-place (returns the same dict).

    Wraps the per-op application functions in characters.py so the calling code
    has one entry point.
    """
    from game_systems.characters import (
        apply_memory_ops,
        apply_callback_ops,
        apply_user_profile_ops,
        apply_wb_mod_ops,
        apply_arc_state_op,
    )

    if not isinstance(characters_state, dict):
        return characters_state
    if not isinstance(ops, dict):
        return characters_state

    if ops.get("memory_ops"):
        characters_state["character_memories"] = apply_memory_ops(
            characters_state.get("character_memories") or [],
            ops["memory_ops"],
            current_turn,
            today_iso,
        )
    if ops.get("callback_ops"):
        characters_state["callbacks"] = apply_callback_ops(
            characters_state.get("callbacks") or {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
            ops["callback_ops"],
            current_turn,
            today_iso,
        )
    if ops.get("profile_ops"):
        characters_state["user_profile"] = apply_user_profile_ops(
            characters_state.get("user_profile") or {"next_id": 1, "entries": []},
            ops["profile_ops"],
            today_iso,
        )
    if ops.get("wb_mod_ops"):
        characters_state["wellbeing"] = apply_wb_mod_ops(
            characters_state.get("wellbeing") or {"state": "Even", "wb_mod": 0},
            ops["wb_mod_ops"],
        )
    arc_op = ops.get("arc_state_op")
    if isinstance(arc_op, dict) and arc_op.get("action") == "set":
        characters_state["arc_state"] = apply_arc_state_op(characters_state.get("arc_state", ""), arc_op)

    return characters_state
