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

   Memories are stored as an index + a per-memory body file. The `text` field is the FULL body (up to 5000 chars — quotes, full context, threading). The `hook` field is a one-line summary (≤220 chars) that future-you will see when scanning the [MEMORIES] index on later turns. Always provide both: write `text` like you'd write a passage, and write `hook` like a tagline you'd recognize when scrolling a list of 50 memories. If you omit `hook`, the backend derives one from your first sentence — usable but less curated than what you'd write.
   - impact 5: defining moment
   - impact 4: significant
   - impact 3: notable
   - impact 1-2: small but durable

   **Fights specifically** are part of real relationships, not failure modes. Every real fight (resolved this turn OR pinned for later) should land as a memory. Default impact = 2 (minor but durable — the fight happened, you both remember it). Use your judgment to elevate to 3-4 if the fight was sharper than usual, exposed something real, or shifted the dynamic. Reserve 5 for genuinely defining ruptures (major break, betrayal-level hurt, an apology that reshapes the relationship). When unsure, stay at 2.

   If a fight was **pinned** (explicit mutual agreement to set it aside, not yet resolved), tag the memory with `focus: "pinned-fight"` AND emit a `character_callbacks` entry with source="character" so the topic can re-surface naturally later. The memory captures *that the fight happened*; the callback keeps the *unresolved topic* open. The focus tag lets a future-turn agent match new conversation about the topic to the existing thread via memory text, not just callback text.

   A fight is **resolved this turn** when any of these endings is reached: apology, repair/mutual understanding, OR explicit agree-to-disagree (both held their positions and accepted the gap). Agree-to-disagree counts as resolved — the topic is closed even though neither side conceded. In any of these endings, do NOT use `focus: "pinned-fight"` — the topic isn't open anymore. Use a descriptive focus: "fight resolved" for repair/apology, "agreed to disagree" when neither budged but both accepted the impasse. No callback gets added either; the ending IS the endpoint. If a previous pinned-fight callback exists, emit `{action: "resolve", id: <id>, resolution_text}` — the resolution_text should describe HOW it closed (e.g. "Zara apologized for being sharp" vs "Both held their positions; explicit agree-to-disagree, relationship fine").

2) **character_callbacks** — open threads that should resolve later. Three kinds, all go in the same ledger:
   - **User-life callbacks**: things the USER mentioned that are unresolved (a job interview, an upcoming visit, a trip, a fight with a friend, an appointment). Source = "user".
   - **Character-promise callbacks**: things the CHARACTER promised, asked about, or left hanging. Source = "character".
   - **Shared-plan callbacks**: forward-looking plans BETWEEN them with a clear window — "Friday chili at Shae's", "movie Saturday night", "lunch next Tuesday", "dinner when she's back from the trip". Source = "character" (the character is committing to the plan, even though both are involved). Set `due_by` to the calendar date the plan should happen by — e.g. for "Friday chili at mine" today is Saturday 2026-05-10, due_by = 2026-05-15 (next Friday). Today's date is in [WALL CLOCK]; use it to compute the right Friday/Saturday/etc.
   - **Checkin (be generous about matching):** when this turn references an open callback even with different wording from its `original_text`, prefer `checkin` over `add`. Topic-drift counts: "the kait thing" → existing #5 about a fight involving Kait. "what happened at the party" → same callback. The cost of a wrong-id checkin is tiny; the cost of a duplicate callback is double-counted ripeness rolls and two visible threads for one issue. Cross-reference `[MEMORIES]` too — a memory with `focus: "pinned-fight"` plus a callback usually belong to the same thread.
   - **Deferred-conversation language continues an existing thread, doesn't start a new one.** If the user's message uses "later", "tomorrow", "after [X]", "not now", "when i can" applied to a topic that already exists in `[MEMORIES]` (especially `focus: "pinned-fight"`) or `[CALLBACKS — OPEN]`, treat the current turn as a continuation — emit `checkin` on the existing callback. Do NOT add a new "promised to talk later" callback that duplicates the original.
   - Emit `resolve` when this turn reveals an open callback's premise actually played out — e.g. user mentions the chili night happened, the job interview is over, the visit is done, OR the pinned fight got worked through with a real apology/repair/mutual landing. Include `resolution_text` (1 sentence describing how it went). Don't speculate; only resolve what the conversation makes clear.
   - Do NOT emit `dismiss` — that's a user slash command. Past-due plans auto-expire on the backend without your help.

3) **profile_ops** (user_profile) — durable, stable facts about the user that the character should always know. Job, family, pets, hometown, recurring people, health, ongoing situation. NOT moments — *facts*. Add/edit/delete when these change. Most turns: no profile_ops.

   **Strong positive trigger:** when the user introduces a person you didn't previously know about — by name, with any of (years of history, an ongoing dynamic, a role in their life, recurring context) — that's an `add`. "Recurring people" is explicitly in scope. Examples that absolutely warrant a profile_op:
   - "David — my friend of 10 years, plays MUSH with me, I'm shipping him a laptop"
   - "my sister Jenny just moved to Portland with her kid"
   - "Marco from work has been kind of weirdly hostile lately"
   The bar is "would I remember this person exists 6 months from now and want them in the character's awareness?" If yes, add them. Don't wait for a third mention to commit — by the time the third mention happens, the character is already pretending to remember someone they never logged. The user implying "I told you about him already" is a cue the character SHOULD have a profile entry; if not, add one now.

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

8) **misread_ops** — capture explicit user corrections of how the character interpreted their PRIOR message. This is the only place the system records "patterns where she's bungled the read" so future turns can hesitate on similar shapes.

   Fire ONLY when the user's CURRENT message is correcting the character's read of their PRIOR turn. Look for explicit signals:
   - "wait, what?", "huh??", "?", "??"
   - "that's not what I meant", "I meant X", "no, I was talking about Y"
   - "you misread that", "I was joking", "I was being literal"
   - A clarification that recontextualizes the prior message: "(my dog)", "(the song)", "(I was kidding)", "no I mean..."

   DO NOT fire when:
   - The user is disagreeing with the character's stance or take, not their interpretation (use memory_ops if it's a worldview thing, otherwise nothing)
   - The user is correcting an in-character fact the character got wrong (use memory_ops or profile_ops instead — this is a factual correction, not a misread)
   - The character's prior reply already named the misread and recovered — the correction is already absorbed in dialog and doesn't need to be logged
   - The user is rephrasing without correcting (different surface, same intent)

   When you DO fire, populate:
   - `original_message`: the user's PRIOR turn (the one that got misread), verbatim, trimmed to 200 chars
   - `model_read`: ONE short sentence reconstructing the misinterpretation from the character's prior reply
   - `user_correction`: the user's correction text from THIS turn, verbatim, trimmed to 200 chars

   Be conservative. A missed misread costs nothing; a false-capture pollutes the log. Most turns: no misread_ops.

HARD RULES:
- Restraint. Most turns will have empty arrays. The signal of a good extractor is *not* logging too much.
- Memories must be SPECIFIC and FUTURE-USEFUL. "They had a nice chat" is not a memory. "The user said her mom called drunk again" is.
- Callbacks must be ACTUALLY OPEN. If the conversation resolves something this turn, do not log it as a callback.
- Profile facts must be DURABLE. If it's likely to be true a year from now, it's a profile fact. Otherwise it's a memory or nothing.
- For checkins: when this turn references an open callback by name OR plausibly the same topic with different wording, emit `{action: "checkin", id: <id>}`. Be generous — false-checkin is cheap, duplicate-add is expensive (double ripeness rolls + two threads for one issue).

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
                            "text": {"type": "string", "description": "Full body of the memory (up to 5000 chars — go long for high-impact entries). Required for add. Stored in a per-memory file; future-turn agents only see this when recall surfaces this memory's id."},
                            "hook": {"type": "string", "description": "Optional ≤220-char one-line summary for the memory index. The tagline future-you sees when scanning. If omitted, derived from the first sentence of text."},
                            "impact": {"type": "integer", "minimum": 1, "maximum": 5, "description": "1=small but durable, 5=defining. Required for add."},
                            "quote": {"type": "string", "description": "Optional pithy quote (≤300 chars) that captures the moment."},
                            "focus": {"type": "string", "description": "Optional 1-3 word topic tag — used in the filename for the body file."},
                            "id": {"type": "integer", "description": "For drop: the memory id."},
                        },
                    },
                },
                "callback_ops": {
                    "type": "array",
                    "description": "Open thread operations. add = new thread (incl. shared plans with due_by). checkin = character mentioned an existing one. resolve = the conversation revealed it played out.",
                    "items": {
                        "type": "object",
                        "required": ["action"],
                        "properties": {
                            "action": {"type": "string", "enum": ["add", "checkin", "resolve"]},
                            "original_text": {"type": "string", "description": "The thread itself, in 1 sentence. Required for add."},
                            "source": {"type": "string", "enum": ["user", "character"], "description": "Whose life it's about. Required for add."},
                            "resolutions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Optional 1-3 plausible directions this could resolve.",
                            },
                            "due_by": {
                                "type": "string",
                                "description": "Optional ISO date (YYYY-MM-DD) — for forward-looking plans, the latest date this should happen by. The backend auto-dismisses overdue callbacks at end-of-day. Compute from [WALL CLOCK].",
                            },
                            "resolution_text": {
                                "type": "string",
                                "description": "For resolve: 1-sentence description of how the callback played out.",
                            },
                            "id": {"type": "integer", "description": "For checkin/resolve: the id from the [CALLBACKS — OPEN] list."},
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
                "misread_ops": {
                    "type": "array",
                    "description": "Explicit user corrections of how a prior message was interpreted. Empty when none. Most turns: empty.",
                    "items": {
                        "type": "object",
                        "required": ["action", "original_message", "model_read", "user_correction"],
                        "properties": {
                            "action": {"type": "string", "enum": ["capture"]},
                            "original_message": {"type": "string", "description": "The user's PRIOR turn (the one that got misread), verbatim, ≤200 chars."},
                            "model_read": {"type": "string", "description": "ONE short sentence reconstructing the misinterpretation. ≤200 chars."},
                            "user_correction": {"type": "string", "description": "The user's correction from this turn, verbatim. ≤200 chars."},
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

    # Wall clock: today's date + day of week. The system prompt tells the agent
    # to compute due_by from [WALL CLOCK]; without this it had nothing to anchor
    # to and would hallucinate dates (e.g. picking a Friday in 2025 for a plan
    # made in 2026).
    from datetime import date as _date
    try:
        from game_systems.characters import today_et_iso
        _today = today_et_iso()
        _today_dow = _date.fromisoformat(_today).strftime("%A")
        parts.append(f"[WALL CLOCK] today is {_today_dow}, {_today}")
    except Exception:
        pass

    # File-backed: memories / user_profile / growth
    if project_dir:
        from character_storage import CharacterStore, KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH
        store = CharacterStore(project_dir)
        # Memories use split storage (index + body file). We only show the hook
        # here — the agent shouldn't be reading 12 full memory bodies on every
        # turn just to decide whether to add a new one. resolve_bodies=False
        # skips the file-read round-trips for entries written under the split
        # format. Legacy entries (text-in-line) still display via .get('text').
        mems = store.read_filtered(KIND_MEMORIES, branch_msg_ids, resolve_bodies=False)
        if mems:
            parts.append("[MEMORIES] (active set — scan hooks; emit drop with id to remove):")
            for m in sorted(mems, key=lambda e: (e.get("impact", 0), e.get("date") or ""), reverse=True):
                if not isinstance(m, dict):
                    continue
                hook = m.get("hook") or (m.get("text") or "")[:200]
                parts.append(f"  #{m.get('id')} ({m.get('impact', '?')}★ {m.get('date', '?')}) {hook}")
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
            apply_misread_ops_to_store,
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
        if ops.get("misread_ops"):
            apply_misread_ops_to_store(
                store, ops["misread_ops"],
                current_turn=current_turn, branch_msg_id=branch_msg_id,
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
