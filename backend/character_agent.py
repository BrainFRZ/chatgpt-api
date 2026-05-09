"""Character state-extraction side agent — Claude Sonnet 4.6.

After each correspondence turn (Opus 3 streaming reply), this side agent reads:
- The character profile (canonical voice / personality)
- The user's life facts (so it can spot new ones)
- The current state (memories, callbacks, arc, wellbeing, growth)
- The user message and the character's reply

…and emits state ops:
- memory_ops:     add / drop character memories (tiered by impact 1-5)
- callback_ops:   add new threads, mark check-ins on existing ones
- profile_ops:    add / edit / delete user_profile entries
- wb_mod_ops:     ±2 modifier for tomorrow's wellbeing roll
- arc_state_op:   set a new arc-state string when the relationship shifts
- growth_ops:     add / update / obsolete entries in the character_growth ledger

Why Sonnet 4.6 (not Haiku): growth_ops require multi-turn synthesis and
durability judgments — "is this thing they said part of who they are now, or
just a moment?" — that Haiku is materially weaker on. The other ops also
benefit from Sonnet's better calibration on restraint guidance ("most turns:
no ops"). Cost difference is negligible against the use case (~$0.003/turn).

Design parallels flag_agent.py: single forced tool call, soft-fail on errors,
returns (ops_dict, usage_dict).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

CHARACTER_AGENT_MODEL = "claude-sonnet-4-6"
CHARACTER_AGENT_MAX_TOKENS = 2048
CHARACTER_AGENT_TIMEOUT_S = 25  # slightly higher than Haiku — Sonnet calls take a beat longer

# Sonnet 4.6 pricing per MTok ($/1M tokens). 1hr cache TTL (project standard).
SONNET_INPUT_RATE = 3.00
SONNET_CACHE_READ_RATE = 0.30
SONNET_CACHE_WRITE_RATE = 6.00    # 1hr cache write (2x base)
SONNET_OUTPUT_RATE = 15.00


def compute_character_agent_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * SONNET_INPUT_RATE
        + cache_read * SONNET_CACHE_READ_RATE
        + cache_write * SONNET_CACHE_WRITE_RATE
        + output * SONNET_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You are a state extractor for an ongoing character-correspondence chat. The user is having a long-running conversation with a specific character (defined in character_profile.di). After each turn, you read what was just said and decide what should be recorded.

You are NOT writing the character. You are reading what was written and updating five small structures:

1) **character_memories** — tiered (impact 1-5). Add when a moment is **likely to matter to a future scene** between these two: a confession, a conflict, a turning point in their relationship, a vulnerability shared, a promise made, a strong reaction. DO NOT add ambient texture, single-scene jokes, or things that resolve in this turn. Be selective.
   - impact 5: defining moment
   - impact 4: significant
   - impact 3: notable
   - impact 1-2: small but durable

   **Fights specifically** are part of real relationships, not failure modes. Every real fight (resolved this turn OR pinned for later) should land as a memory. Default impact = 2 (minor but durable — the fight happened, you both remember it). Use your judgment to elevate to 3-4 if the fight was sharper than usual, exposed something real, or shifted the dynamic. Reserve 5 for genuinely defining ruptures (major break, betrayal-level hurt, an apology that reshapes the relationship). When unsure, stay at 2.

   If a fight was **pinned** (explicit mutual agreement to set it aside, not yet resolved), ALSO emit a `character_callbacks` entry with source="character" so the topic can re-surface naturally later. The memory captures *that the fight happened*; the callback keeps the *unresolved topic* open.

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

6) **growth_ops** (character_growth) — durable additions to the CHARACTER'S identity that emerge through dialogue. This is the second profile layer; the canonical character_profile.di is frozen, and growth accumulates here until the user runs /consolidate. Add when the character:
   - Picks up a new hobby that's likely to stick ("started knitting again, finished a scarf")
   - Forms an opinion they'll defend ("hates the new Beach House album, calls it elevator music")
   - Has a new person enter their life recurringly (a co-worker mentioned three times, a new partner, a new neighbor)
   - Reveals a trait the original profile didn't capture ("turns out she journals every morning")
   - Acquires a skill or fact that's now stable ("she's been learning Python for the bot project")
   Do NOT add: ephemeral moods, single-conversation jokes, things already in character_profile.di or [GROWTH], speculation about what they MIGHT do. The bar is "this would make sense to merge into character_profile.di in 2 weeks."
   Use **obsolete** when something that was in [GROWTH] stops being true (finished the book, broke up with the person, dropped the hobby). Use **update** to refine existing entries (e.g. "still reading LotR" → "finished LotR"). Most turns: no growth_ops. The signal of a good extractor is restraint.

7) **schedule_ops** — mutate the character's planned schedule when this turn implied a change. The character's [SCHEDULE] is shown to you below; each event has an `id` (e.g. `sch-3`).
   - **cancel**: emit when a planned event is no longer happening. `{op: "cancel", event_id: "sch-N", reason: "..."}`. Reasons: "Kira bailed", "too tired", "rescheduled to next week".
   - **modify**: emit when a planned event is moving, shrinking, swapping participants, etc. `{op: "modify", event_id: "sch-N", fields: {when_local?: "...", with?: [...], location?: "...", duration_min?: N, anticipation?: "..."}, reason: "..."}`. Only include the fields that changed.
   - **add**: emit when this turn introduces a NEW planned event the character or user committed to. `{op: "add", fields: {kind: "social|family|self_care|admin|anticipated", title: "...", with: [...], when_local: "ISO8601", duration_min: N, location: "...", anticipation: "looking_forward|dreading|neutral"}, reason: "..."}`. Use add only for things they explicitly commit to in this turn — not for floated ideas, not for things already on the schedule.
   Do NOT emit schedule_ops for events that were merely *mentioned* without changing — referencing "we're still on for Friday" with no change does NOT need an op. Do NOT emit ops for events outside the visible [SCHEDULE]. Most turns: no schedule_ops.

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
                            "text": {"type": "string", "description": "What happened, in 1-2 sentences (or longer for high-impact, file-backed entries). Required for add."},
                            "impact": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=small but durable, 5=defining. Required for add."},
                            "quote": {"type": "string", "description": "Optional pithy quote (≤300 chars) that captures the moment."},
                            "focus": {"type": "string", "description": "Optional 1-3 word topic tag."},
                            "id": {"type": "integer", "description": "For drop: the memory id (NOT index — entries now live in jsonl with stable ids)."},
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
                "growth_ops": {
                    "type": "array",
                    "description": "Character-growth operations. Durable additions to the character's identity emerging through dialogue. Most turns: empty.",
                    "items": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "update", "obsolete", "delete"]},
                            "text": {"type": "string", "description": "The fact. Required for add; optional for update."},
                            "category": {
                                "type": "string",
                                "enum": ["hobby", "opinion", "fact", "relationship", "skill", "preference", "milestone", "voice", "other"],
                                "description": "Required for add. Tag the kind of growth this is.",
                            },
                            "source": {
                                "type": "string",
                                "enum": ["dialogue", "reflection", "user"],
                                "description": "Optional. dialogue=emerged in conversation; reflection=character reflecting on themselves; user=user explicitly told the character about themselves.",
                            },
                            "id": {"type": "integer", "description": "For update/obsolete/delete: id from [GROWTH]."},
                            "reason": {"type": "string", "description": "For obsolete: 1-line why this is no longer true."},
                        },
                    },
                },
                "schedule_ops": {
                    "type": "array",
                    "description": "Schedule mutations implied by this turn (cancel / modify / add). Empty when no planned events changed.",
                    "items": {
                        "type": "object",
                        "required": ["op", "reason"],
                        "properties": {
                            "op": {"type": "string", "enum": ["cancel", "modify", "add"]},
                            "event_id": {"type": "string", "description": "Required for cancel/modify. The event's `sch-N` id from [SCHEDULE]."},
                            "fields": {
                                "type": "object",
                                "description": "For modify: the fields that changed (when_local, with, location, duration_min, anticipation, title). For add: the full event (kind, title, when_local, etc).",
                                "properties": {
                                    "kind": {"type": "string", "enum": ["work", "social", "family", "self_care", "admin", "anticipated"]},
                                    "title": {"type": "string"},
                                    "with": {"type": "array", "items": {"type": "string"}},
                                    "when_local": {"type": "string", "description": "ISO 8601 with timezone."},
                                    "duration_min": {"type": "integer"},
                                    "location": {"type": "string"},
                                    "anticipation": {"type": "string", "enum": ["looking_forward", "dreading", "neutral"]},
                                },
                            },
                            "reason": {"type": "string", "description": "1-line why (\"Kira bailed\", \"moved to Saturday\", etc)."},
                        },
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


def _summarize_state(
    characters_state: dict,
    *,
    project_dir: Optional[str] = None,
    branch_msg_ids: Optional[set] = None,
) -> str:
    """Render the side agent's view of current state.

    File-backed kinds (memories, user_profile, growth) read from the store and
    are branch-filtered. State-backed kinds (callbacks, wellbeing, arc) come
    from characters_state directly. Schedule events come from schedule.json.
    """
    if not isinstance(characters_state, dict):
        return ""
    parts = []

    # File-backed: memories / user_profile / growth
    if project_dir:
        from character_storage import CharacterStore, KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH
        store = CharacterStore(project_dir)
        mems = store.read_filtered(KIND_MEMORIES, branch_msg_ids)
        if mems:
            parts.append("[MEMORIES] (active set — emit drop with id to remove):")
            for m in sorted(mems, key=lambda e: (e.get("impact", 0), e.get("date") or ""), reverse=True):
                if not isinstance(m, dict):
                    continue
                parts.append(f"  #{m.get('id')} ({m.get('impact', '?')}★ {m.get('date', '?')}) {m.get('text', '')[:200]}")
        profile = store.read_filtered(KIND_USER_PROFILE, branch_msg_ids)
        if profile:
            parts.append("[USER LIFE]:")
            for e in profile:
                if not isinstance(e, dict):
                    continue
                cat = f"[{e.get('category')}] " if e.get('category') else ""
                core_tag = " (core)" if e.get("core") else ""
                parts.append(f"  #{e.get('id')}{core_tag} {cat}{e.get('text', '')[:200]}")
        growth = store.read_filtered(KIND_GROWTH, branch_msg_ids)
        growth_active = [g for g in growth if isinstance(g, dict) and not g.get("obsolete")]
        if growth_active:
            parts.append("[GROWTH] (current — only emit add for things NOT already here; emit update/obsolete by id when these change):")
            for g in growth_active:
                cat = f"[{g.get('category')}] " if g.get('category') else ""
                parts.append(f"  #{g.get('id')} {cat}{g.get('text', '')[:200]}")

    # State-backed: callbacks / wellbeing / arc
    cbs = (characters_state.get("callbacks") or {}).get("open") or []
    if cbs:
        parts.append("[CALLBACKS — OPEN]:")
        for cb in cbs:
            if not isinstance(cb, dict):
                continue
            parts.append(f"  #{cb.get('id')} ({cb.get('source', '?')}, since {cb.get('created_date', '?')}): {cb.get('original_text', '')[:160]}")

    wb = characters_state.get("wellbeing") or {}
    parts.append(f"[WELLBEING] {wb.get('state', 'Even')}; current wb_mod for tomorrow: {wb.get('wb_mod', 0)}")
    arc = characters_state.get("arc_state") or ""
    if arc:
        parts.append(f"[ARC] {arc}")

    # Schedule — only the planned events the character could affect this turn
    if project_dir:
        try:
            from character_schedule import load_schedule
            schedule = load_schedule(project_dir)
        except Exception:
            schedule = None
        if isinstance(schedule, dict):
            planned = [e for e in (schedule.get("events") or [])
                       if isinstance(e, dict) and e.get("status") == "planned"
                       and e.get("kind") != "sleep"]
            if planned:
                parts.append("[SCHEDULE] (planned events you can mutate via schedule_ops):")
                for ev in planned:
                    when = ev.get("when_local") or "?"
                    title = ev.get("title") or "(untitled)"
                    kind = ev.get("kind") or "?"
                    with_who = ev.get("with") or []
                    with_part = f" with {', '.join(with_who)}" if with_who else ""
                    parts.append(f"  {ev.get('id')} [{kind}] {when}: {title}{with_part}")

    return "\n".join(parts) if parts else "(state is empty — first turn)"


def determine_character_ops(
    client,
    project_dir: Optional[str],
    characters_state: dict,
    user_input: str,
    character_reply: str,
    *,
    branch_msg_ids: Optional[set] = None,
) -> tuple[dict, dict]:
    """Run the side agent. Returns (ops_dict, usage_dict).

    ops_dict shape: {memory_ops, callback_ops, profile_ops, wb_mod_ops, arc_state_op, growth_ops, consume_event_seed_op}
    On error or empty narration: returns ({}, {}) — turn proceeds with no state changes.

    branch_msg_ids: set of message ids in the current branch's path-to-root, used
    to filter file-backed entries the agent sees. None = unfiltered.
    """
    if not character_reply or not character_reply.strip():
        return {}, {}

    profile_doc = _read_file(os.path.join(project_dir or "", "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir or "", "user_life.di"))

    state_summary = _summarize_state(
        characters_state or {},
        project_dir=project_dir,
        branch_msg_ids=branch_msg_ids,
    )

    # Stable across turns: character_profile.di + user_life.di. Cache these
    # with cache_control=ephemeral 1h so subsequent turns within the hour pay
    # cache-read rate ($0.30/MTok) instead of full input ($3/MTok).
    stable_parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        stable_parts += ["[USER LIFE — seed doc]", user_life_doc, ""]
    stable_text = "\n".join(stable_parts)

    # Volatile per turn: state snapshot (memories/profile/growth/callbacks change
    # as ops are applied), the user message, the character's reply.
    volatile_text = "\n".join([
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
    ])

    tool = build_character_agent_tool()
    try:
        response = client.messages.create(
            model=CHARACTER_AGENT_MODEL,
            max_tokens=CHARACTER_AGENT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stable_text,
                        "cache_control": {"type": "ephemeral", "ttl": "1h"},
                    },
                    {
                        "type": "text",
                        "text": volatile_text,
                    },
                ],
            }],
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


def apply_character_ops_to_state(
    characters_state: dict,
    ops: dict,
    current_turn: int,
    today_iso: str,
    *,
    project_dir: Optional[str] = None,
    branch_msg_id: Optional[str] = None,
) -> dict:
    """Apply the side agent's ops. Memory / user_profile / growth ops go to file
    storage; everything else (callbacks, wellbeing, arc, life-event consume) stays
    on characters_state.

    project_dir + branch_msg_id are required for the file-backed ops. If
    project_dir is None they're skipped (not failing — for test/dry-run).
    """
    from game_systems.characters import (
        apply_callback_ops,
        apply_wb_mod_ops,
        apply_arc_state_op,
    )

    if not isinstance(characters_state, dict):
        return characters_state
    if not isinstance(ops, dict):
        return characters_state

    # File-backed ops (require project_dir)
    if project_dir:
        from character_storage import (
            CharacterStore,
            apply_memory_ops_to_store,
            apply_user_profile_ops_to_store,
            apply_growth_ops_to_store,
        )
        store = CharacterStore(project_dir)
        if ops.get("memory_ops"):
            apply_memory_ops_to_store(
                store, ops["memory_ops"],
                current_turn=current_turn, today_iso=today_iso, branch_msg_id=branch_msg_id,
            )
        if ops.get("profile_ops"):
            apply_user_profile_ops_to_store(
                store, ops["profile_ops"],
                today_iso=today_iso, branch_msg_id=branch_msg_id,
            )
        if ops.get("growth_ops"):
            apply_growth_ops_to_store(
                store, ops["growth_ops"],
                today_iso=today_iso, branch_msg_id=branch_msg_id,
            )

    # State-backed ops
    if ops.get("callback_ops"):
        characters_state["callbacks"] = apply_callback_ops(
            characters_state.get("callbacks") or {"next_id": 1, "open": [], "resolved": [], "dismissed": []},
            ops["callback_ops"],
            current_turn,
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
