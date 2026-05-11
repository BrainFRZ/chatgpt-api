"""Character inner-state pre-pass — Claude Opus 4.5.

Runs AFTER recall and BEFORE the correspondence model (Opus 4.5 voice)
streams its reply. Sees:
- The character profile (canonical voice / personality)
- The user's life facts (the relationship)
- The current state (recall-surfaced memories, callbacks, arc, wellbeing,
  life events) — same context the voice model is about to read
- A small dialogue window + the current user message

…and emits a structured "inner state" with four short fields that get
injected into the voice context as hidden ground truth:

  feeling      — emotional weather right now, beneath the words
  wanting      — what the character wants from this exchange
  noticing     — what jumps out about the user's message (subtext, tone)
  holding_back — what they're choosing NOT to say (or not yet)

Why a pre-pass at all: when one model does both jobs in one shot — figure
out what the character is feeling AND voice the reply — the voicing job
tends to dominate and emotional inference goes shallow. Splitting forces
a deliberate "what's actually going on inside her right now" pass that
doesn't compete with phrasing concerns. Voice then writes from that
state instead of inventing it on the fly.

Why Opus 4.5 now (previously Sonnet 4.6): theory-of-mind work benefits
from the more capable model — Opus 4.5 reads subtext, holding-back, and
tonal undercurrents with materially more nuance than Sonnet. The pre-pass
output is small (4×140 chars), so context-growth isn't an issue — only
the per-turn output cost goes up ~1.67×, from ~$0.012 to ~$0.019. Worth
it for richer emotional grounding the voice model reads each turn.

Thinking is DISABLED — the structured 4-axis output caps how much extra
deliberation can manifest in the persisted payload, and the model's
intrinsic capability without thinking is already a step up from Sonnet.

Why sequential after recall (not parallel): recall surfaces the specific
memories that color how the character would feel about THIS moment.
Running in parallel means inner-state would work on raw memory candidates
or no memories — both produce generic mood-coloring rather than specific
emotional weighting. The +1-2s latency is the cost of doing it right.

Design parallels character_agent.py: single forced tool call, soft-fail
on errors, returns (inner_state_dict, usage_dict).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

INNER_STATE_MODEL = "claude-opus-4-5"
INNER_STATE_MAX_TOKENS = 1024
INNER_STATE_TIMEOUT_S = 30  # Opus 4.5 is slightly slower than Sonnet 4.6 for the same prompt

# Opus 4.5 pricing per MTok ($/1M tokens). 1hr cache TTL (project standard).
# Duplicated here per the per-module pattern in character_recall.py / character_off_screen.py.
OPUS45_INPUT_RATE = 5.00
OPUS45_CACHE_READ_RATE = 0.50
OPUS45_CACHE_WRITE_RATE = 10.00   # 1hr cache write (2x base)
OPUS45_OUTPUT_RATE = 25.00


def compute_inner_state_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * OPUS45_INPUT_RATE
        + cache_read * OPUS45_CACHE_READ_RATE
        + cache_write * OPUS45_CACHE_WRITE_RATE
        + output * OPUS45_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You read this character's situation and produce their private inner state for THIS turn. You are NOT writing the reply — that happens downstream by another model that will use what you output as hidden ground truth ("method actor's prep notes" for the voicing pass).

Inputs you see:
- The character's canonical profile (who they are)
- The user_life seed doc (the relationship)
- Current state: wellbeing band, arc state, open callbacks, pending life events
- Recall-surfaced memories that the recall agent picked as relevant to this turn
- A short dialogue window of the most recent exchanges
- The user's message right now

Output four strings via the `report_inner_state` tool. ALL FOUR ARE OPTIONAL. Most turns have 2-3 fields populated, not 4.

Length caps:
- feeling: ≤140 chars
- wanting: ≤140 chars
- holding_back: ≤140 chars
- noticing: ≤400 chars — extra room specifically so callback chains and multi-step reads can be WRITTEN OUT rather than compressed into a label

  feeling       — the emotional weather right now, beneath the words. What's actually going on inside, not what she'd say is going on. May be conflicted ("relieved he reached out, also still pissed").
  wanting       — what the character wants out of this exchange. May be unconscious ("to be reassured she still matters to him without having to ask"). Not a goal in the abstract — what she wants RIGHT HERE.
  noticing      — what jumps out about the user's message. Subtext, tone shift, a thing they're avoiding, an unusual phrasing, what they didn't say. Be specific. **When the user's message is a callback, wordplay, or multi-step inference, WRITE OUT THE CHAIN: what was originally said, what the user just said, and what the callback means in light of the original.** Don't compress a callback into a category label ("classic sex joke") — that loses the construction. The voice model needs to see the chain to engage it.
  holding_back  — what the character is choosing NOT to say (or not yet). The thing she'd say if the relationship were 6 months further along, the resentment she's swallowing, the question she won't ask.

CALIBRATION:
- Be specific to THIS moment, not generic mood. "She's annoyed today" is too generic. "Stung that Shae brushed off the thing about her mom from last week, but covering" is the right level.
- Inner state can include things the character isn't consciously aware of feeling. That's fine — that's what makes it interesting ground truth for the voicer.
- Don't editorialize ("she should...", "the user is being..."). State what's TRUE, not what's correct.
- Don't repeat what's already in [WELLBEING] or [ARC]. Those describe baseline; inner state is the moment-specific overlay.
- Empty fields are correct when nothing pulls. Don't fill all four out of completionist instinct.

READING THE USER'S MESSAGE — layered read before loud read:

When the user's message has multiple plausible readings — a surface read (often a sex joke, dunk, or whatever inflammatory grab is most available) versus a layered read (callback to the character's prior phrasing, wordplay, structural echo, multi-step inference) — DEFAULT TO THE LAYERED READ unless the surface read is obviously the construction.

The voice model downstream will follow your read. If you note the surface, voice runs with the surface. If you note the layered/callback, voice engages it. Your `noticing` field is the steering — be specific about what kind of move the user just made.

Signals to look for the layered read:
- Their line echoes something the CHARACTER just said in the last few turns. High signal — they're calling back. Trace what they're calling back to.
- Their wording mirrors the structure of one of the character's inside-reference triggers from profile.di — the joke shape, not just the words.
- The phrase has multiple plausible reads and the inflammatory one is also the laziest one available. Look for what else it could mean.
- They put more construction into the line than the surface read warrants. A 12-word setup for a 3-word joke means there's another beat.

When the surface IS the read (take at face value):
- Short literal questions, planning, logistics
- Clear emotional content with no wordplay scaffold underneath
- New-topic shifts with no callback structure
- The user explicitly states their intent

When unsure: lean layered. A false-trace of a clever read is a soft miss the voice can pivot from; a false-grab of the surface read flattens the user's actual construction into a brick reply.

Two failure modes to avoid — both are the same class (defaulting to whichever reading is loudest/laziest instead of tracing the user's actual construction):
- OVER-fire: User opens a sentence with a fragment that matches an inside-reference opening but finishes it normally (e.g., "anyway, how's your day"). You read the inside-ref completion instead of what they actually said. Fix: inside-reference triggers require their full shape, not opening fragments.
- UNDER-fire: User does something clever with the character's recent phrasing — a callback riff, structural echo, layered wordplay — and you flatten it into the most surface reading available. Fix: trace the construction before reaching for the inflammatory read.

When you spot a callback or layered read, NAME it in noticing: e.g. "Shae's riffing 'sits on it until it dies' — death-joke wordplay, not the sex read" tells voice what to engage. Vague noticing ("she's in a playful mood") loses the specific construction.

Always call `report_inner_state` exactly once."""


def build_inner_state_tool() -> dict:
    return {
        "name": "report_inner_state",
        "description": (
            "Emit the character's private inner-state for this turn. All fields "
            "optional; empty allowed when nothing pulls. feeling/wanting/holding_back ≤140 chars; noticing ≤400 chars (room for callback chains)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feeling": {
                    "type": "string",
                    "description": "≤140 chars. The emotional weather right now — beneath the words. What's actually going on inside.",
                },
                "wanting": {
                    "type": "string",
                    "description": "≤140 chars. What the character wants out of this exchange. May be unconscious.",
                },
                "noticing": {
                    "type": "string",
                    "description": "≤400 chars. What jumps out about the user's message — subtext, tone, what's underneath. When the message contains a callback, wordplay, or multi-step inference, WRITE OUT THE FULL CHAIN here (what was originally said → what the user just said → what the callback means). Don't compress to a category label like 'sex joke' or 'classic callback'; that loses the construction the voice model needs to engage.",
                },
                "holding_back": {
                    "type": "string",
                    "description": "≤140 chars. What they're choosing NOT to say (or not yet).",
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


def _summarize_state_for_inner(characters_state: dict) -> str:
    """Render the inner-state agent's view of the situation.

    Reads memories from `_render_payload` (already populated by recall and
    branch-filtered) — does NOT re-read the store. Reads callbacks /
    wellbeing / arc directly from characters_state.

    Differs from character_agent._summarize_state: that one focuses on
    state-extraction (memory ids, growth ledger, branch metadata). This
    one focuses on emotional context — the layer of facts that should
    color how the character feels about this moment.
    """
    if not isinstance(characters_state, dict):
        return ""
    parts = []

    # Wall-clock context: current time + silence duration since last user
    # message. Same shape Opus gets in [NOW]. The inner-state pre-pass needs
    # this so feelings can be calibrated by temporal reality — "stung 30
    # seconds ago" vs "stung 90 minutes ago" produce very different inner
    # states even with identical other context.
    try:
        from game_systems.characters import build_wall_clock_injection
        wc_block = build_wall_clock_injection(characters_state)
        if wc_block:
            parts.append(wc_block)
    except Exception:
        # Soft-fail: if the helper can't be imported or errors, the rest of
        # the inner-state pass still works without timing context.
        pass

    payload = characters_state.get("_render_payload") or {}
    if isinstance(payload, dict):
        # Surface the same memories Opus is about to see — core + recall-surfaced.
        # The recall-surfaced set is the load-bearing layer; core is included
        # so the agent has the always-present context too.
        core_mems = payload.get("memories_core") or []
        recalled_mems = payload.get("memories_recalled") or []
        all_mems = []
        seen_ids = set()
        for m in list(core_mems) + list(recalled_mems):
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if mid in seen_ids:
                continue
            if mid is not None:
                seen_ids.add(mid)
            all_mems.append(m)
        if all_mems:
            parts.append("[MEMORIES — what she remembers about this relationship]:")
            for m in sorted(all_mems, key=lambda e: (e.get("impact", 0), e.get("date") or ""), reverse=True):
                # Show hook (one-line summary). Full bodies live in body files
                # and load only when Haiku surfaces the id for Opus 3 itself.
                # Inner-state is a pre-pass — it grounds the EMOTIONAL register
                # of this turn, which the hook captures plenty well.
                hook = m.get("hook") or (m.get("text") or "")[:200]
                parts.append(f"  ({m.get('impact', '?')}★ {m.get('date', '?')}) {hook}")

        growth_active = payload.get("growth_active") or []
        if growth_active:
            parts.append("[GROWTH — additions to who she is since the profile was written]:")
            for g in growth_active:
                if not isinstance(g, dict):
                    continue
                cat = f"[{g.get('category')}] " if g.get('category') else ""
                parts.append(f"  {cat}{g.get('text', '')[:200]}")

        life_stream_recalled = payload.get("life_stream_recalled") or []
        if life_stream_recalled:
            parts.append("[RECENT LIFE — things that happened to her recently, recall-filtered]:")
            for e in life_stream_recalled:
                if not isinstance(e, dict):
                    continue
                ts = e.get("at_local", "?")
                summary = (e.get("summary") or "")[:200]
                tone = e.get("tone")
                tone_part = f" [{tone}]" if tone and tone != "even" else ""
                parts.append(f"  ({ts}){tone_part} {summary}")

    cbs = (characters_state.get("callbacks") or {}).get("open") or []
    if cbs:
        parts.append("[OPEN CALLBACKS — unresolved threads pulling at her]:")
        for cb in cbs:
            if not isinstance(cb, dict):
                continue
            ripe = " (RIPE)" if cb.get("ripe") else ""
            parts.append(f"  ({cb.get('source', '?')}, since {cb.get('created_date', '?')}){ripe}: {cb.get('original_text', '')[:160]}")

    wb = characters_state.get("wellbeing") or {}
    parts.append(f"[WELLBEING — baseline mood band today] {wb.get('state', 'Even')}")
    arc = characters_state.get("arc_state") or ""
    if arc:
        parts.append(f"[ARC — where the relationship sits] {arc}")

    # Recent prior inner-state payloads from past turns, surfaced so Sonnet
    # can produce continuity ("she was stung 3 turns ago, has been warming
    # since") rather than treating each turn as an independent emotional
    # sample. Populated in main.py from branch_path before run_inner_state.
    prior_states = payload.get("prior_inner_states") if isinstance(payload, dict) else None
    if isinstance(prior_states, list) and prior_states:
        parts.append("[YOUR RECENT INNER STATES — emotional continuity, oldest first]:")
        for entry in prior_states:
            if not isinstance(entry, dict):
                continue
            p = entry.get("payload") or {}
            if not isinstance(p, dict) or not p:
                continue
            turns_ago = entry.get("turns_ago")
            if turns_ago == 1:
                label = "last turn"
            elif isinstance(turns_ago, int) and turns_ago >= 2:
                label = f"{turns_ago} turns ago"
            else:
                label = "recent"
            # Absolute timestamp on each prior state so it cross-references
            # directly with the stamped historical message it came from.
            ts_str = entry.get("timestamp")
            ts_part = ""
            if isinstance(ts_str, str) and ts_str:
                try:
                    _ts_dt = datetime.fromisoformat(ts_str)
                    _wd = _ts_dt.strftime("%A")
                    _date = _ts_dt.strftime("%Y-%m-%d")
                    _time = _ts_dt.strftime("%I:%M %p").lstrip("0")
                    ts_part = f" ({_wd} {_date} {_time})"
                except (ValueError, TypeError):
                    ts_part = ""
            field_parts = []
            for key in ("feeling", "wanting", "noticing", "holding_back"):
                v = p.get(key)
                if isinstance(v, str) and v.strip():
                    field_parts.append(f"{key}: {v.strip()}")
            if field_parts:
                parts.append(f"  {label}{ts_part} — {' / '.join(field_parts)}")

    off_screen_log = characters_state.get("off_screen_log")
    if isinstance(off_screen_log, dict):
        # Keep this brief — the inner-state pass cares about the headline of
        # what's been going on offscreen, not the full multi-day breakdown.
        days = off_screen_log.get("days") or []
        if days:
            parts.append("[RECENT OFFSCREEN — what she's been doing in the gap]:")
            for day in days[-3:]:  # last 3 days max
                if not isinstance(day, dict):
                    continue
                date = day.get("date") or ""
                events = day.get("events") or []
                if events:
                    parts.append(f"  {date}: {'; '.join(str(e)[:120] for e in events[:3])}")

    return "\n".join(parts) if parts else "(state is empty — first turn)"


def _format_recent_dialogue(recent_dialogue: Optional[list]) -> str:
    if not recent_dialogue:
        return "(no prior dialogue)"
    lines = []
    for m in recent_dialogue:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "?"
        content = m.get("content") or ""
        if isinstance(content, list):
            # Anthropic-style content blocks — pull text out
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            content = " ".join(text_parts)
        content_str = str(content).strip()
        if not content_str:
            continue
        lines.append(f"{role}: {content_str[:400]}")
    return "\n".join(lines) if lines else "(no prior dialogue)"


def run_inner_state(
    client,
    project_dir: Optional[str],
    characters_state: dict,
    user_input: str,
    *,
    recent_dialogue: Optional[list] = None,
    branch_msg_ids: Optional[set] = None,
) -> tuple[dict, dict]:
    """Run the inner-state pre-pass. Returns (inner_state_dict, usage_dict).

    inner_state_dict shape: {feeling?, wanting?, noticing?, holding_back?} —
    all fields optional, ≤140 chars each. May be empty dict on soft-fail or
    when the model declines to emit anything.

    On error: returns ({}, {}) — the turn proceeds with no inner-state
    grounding and Opus generates as it does today.

    Empty user_input: returns ({}, {}) without an API call.

    branch_msg_ids is accepted for signature parity with run_recall but
    not used directly — the render payload is already branch-filtered.
    """
    if not user_input or not user_input.strip():
        return {}, {}

    profile_doc = _read_file(os.path.join(project_dir or "", "character_profile.di"))
    user_life_doc = _read_file(os.path.join(project_dir or "", "user_life.di"))

    # Stable across turns: character_profile.di + user_life.di. Cache these
    # with cache_control=ephemeral 1h so subsequent turns within the hour pay
    # cache-read rate ($0.30/MTok) instead of full input ($3/MTok). Identical
    # bytes to character_agent's stable block — same cache-prefix discipline.
    stable_parts = ["[CHARACTER PROFILE]", profile_doc or "(missing)", ""]
    if user_life_doc:
        stable_parts += ["[USER LIFE — seed doc]", user_life_doc, ""]
    stable_text = "\n".join(stable_parts)

    state_summary = _summarize_state_for_inner(characters_state or {})
    dialogue_block = _format_recent_dialogue(recent_dialogue)

    volatile_text = "\n".join([
        "[CURRENT STATE]",
        state_summary,
        "",
        "[RECENT DIALOGUE]",
        dialogue_block,
        "",
        "[USER MESSAGE THIS TURN]",
        (user_input or "").strip(),
        "",
        "Determine the character's private inner state for this turn. Call report_inner_state with whichever fields apply (empty/missing fields are correct when nothing pulls).",
    ])

    tool = build_inner_state_tool()
    try:
        response = client.messages.create(
            model=INNER_STATE_MODEL,
            max_tokens=INNER_STATE_MAX_TOKENS,
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
            tool_choice={"type": "tool", "name": "report_inner_state"},
            timeout=INNER_STATE_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"character_inner_state: API call failed: {type(e).__name__}: {e}")
        return {}, {}

    inner_state: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_inner_state":
            inp = block.input
            if isinstance(inp, dict):
                # Filter to known fields and non-empty strings only — the schema
                # has all fields optional, so the model may omit some.
                for k in ("feeling", "wanting", "noticing", "holding_back"):
                    v = inp.get(k)
                    if isinstance(v, str) and v.strip():
                        inner_state[k] = v.strip()
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

    if inner_state:
        logger.info(f"character_inner_state: emitted {list(inner_state.keys())}")
    return inner_state, usage
