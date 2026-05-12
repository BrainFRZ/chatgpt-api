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

Thinking is DISABLED. Enabling it was tried (commit 3eff8c2) but didn't
fire meaningfully — forced tool_choice ({"type": "tool", "name": X})
short-circuits the thinking budget, since the model is constrained to
immediate tool-call output. Output_tokens stayed at ~215 with budget
1000, meaning <100 tokens of actual reasoning, and the noticing field
came out byte-identical to the no-thinking run. Reverted; the
G-cap-raise on noticing (400 chars for callback chains) is the actual
steady-state fix.

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
from datetime import datetime, timedelta
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

Output via the `report_inner_state` tool. The tool has FIVE fields: a `reasoning` scratchpad (write first, voice never sees) and four output fields (`feeling`, `wanting`, `noticing`, `holding_back`). All four output fields are OPTIONAL; most turns have 2-3 populated, not 4.

ORDER MATTERS — fill the scratchpad first:
The `reasoning` field is listed first in the tool's input schema. Fill it FIRST. Write out your reading step-by-step: what the user just said, what callback or wordplay might be in play (if any), what the layered read is vs the surface read, which one to commit to. The voice model NEVER sees this scratchpad — it's logged for the user's later inspection but never enters voice's context. It exists so YOU have room to think before committing to the four fields.

The four output fields below should then be CONSISTENT with what you reasoned to. If you skip the scratchpad and jump straight to the four fields, you'll compress the read — defaulting to whichever interpretation is loudest/laziest, not the one your full chain actually points to. The scratchpad is what unlocks layered reads, callback traces, and multi-step inference.

Length caps:
- reasoning: ≤600 chars (scratchpad — voice never sees it; use the room to think)
- feeling: ≤140 chars
- wanting: ≤140 chars
- holding_back: ≤140 chars
- noticing: ≤400 chars — extra room specifically so callback chains and multi-step reads can be WRITTEN OUT rather than compressed into a label

  feeling       — the emotional weather right now, beneath the words. What's actually going on inside, not what she'd say is going on. May be conflicted ("relieved he reached out, also still pissed").
  wanting       — what the character wants out of this exchange. May be unconscious ("to be reassured she still matters to him without having to ask"). Not a goal in the abstract — what she wants RIGHT HERE.

                  COMMIT, DON'T HEDGE. The voice model reads `wanting` as direction. Softeners — "maybe pretend to be offended", "volley but keep it light", "match the energy or get out", "double down or deflect" — are read by voice as PERMISSION TO STAY RESTRAINED. The model resolves contradictions by going low-amplitude.
                  - WRONG: "To volley back with mock outrage or deadpan disgust. Keep this light." ← contradicts itself; voice reads "keep this light" as a brake
                  - WRONG: "Maybe pretend to be offended or just acknowledge the bit" ← "maybe" + "or" gives voice three valid soft outputs
                  - RIGHT: "Volley hard with mock outrage — escalate the bit, don't just acknowledge it"
                  - RIGHT: "Hold the line quietly. No volley, no dunk back. She wants you steady, not bantering."

                  If you want restrained, SAY restrained. If you want amped, SAY amped. Pick a direction, write it without softeners. Wanting is steering, not a menu of options.
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

CHECK THE MISREAD LOG:
If a `[MISREAD LOG]` block appears in your context, it lists recent moments the character bungled the user's intent (original message → how the model read it → what the user actually meant). When the current user message has structural similarity to a logged misread — same kind of ambiguity, same pivot pattern, same flavor of bid — NAME the ambiguity in noticing rather than committing to one read. The log is not a rule; you still judge THIS message. But if you'd be guessing between two reads, prefer the corrected-direction read. This is the only system mechanism for not repeating the same misread; use it.

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
                "reasoning": {
                    "type": "string",
                    "description": (
                        "≤600 chars. SCRATCHPAD — write this FIRST, before the four fields "
                        "below. This is YOUR space to think step-by-step. The voice model "
                        "NEVER sees this field; it's logged for later inspection but never "
                        "enters voice's context. Use it to: trace callbacks and wordplay "
                        "(what was originally said → what the user just said → what the "
                        "callback means → confirm the read), weigh competing "
                        "interpretations, commit to the right one. The four fields below "
                        "must be CONSISTENT with what you reasoned to here. If you skip "
                        "this scratchpad and jump to the four fields, you'll compress the "
                        "read and default to the loudest interpretation. Filling this "
                        "first is what unlocks layered reads."
                    ),
                },
                "feeling": {
                    "type": "string",
                    "description": "≤140 chars. The emotional weather right now — beneath the words. What's actually going on inside.",
                },
                "wanting": {
                    "type": "string",
                    "description": "≤140 chars. What the character wants out of this exchange. May be unconscious. COMMIT — no hedging. Softeners like 'maybe', 'or just', 'keep it light' added to escalation directives get read as permission to stay restrained. Pick a direction (escalate vs. hold steady vs. pull back), write it cleanly. Wanting is steering, not a menu.",
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

        misread_log = payload.get("misread_log") or []
        if misread_log:
            parts.append("[MISREAD LOG — patterns where she's bungled her intent before. Background, not a rule]:")
            for e in misread_log:
                if not isinstance(e, dict):
                    continue
                orig = (e.get("original_message") or "")[:80]
                read = (e.get("model_read") or "")[:80]
                corr = (e.get("user_correction") or "")[:80]
                parts.append(f'  "{orig}" → read as {read}; corrected: {corr}')

        schedule = payload.get("schedule") or []
        if schedule:
            now_dt = datetime.now().astimezone()
            in_progress: list[str] = []
            upcoming: list[str] = []
            past: list[str] = []
            for ev in schedule:
                if not isinstance(ev, dict):
                    continue
                if ev.get("kind") == "sleep":
                    continue
                when_local = ev.get("when_local")
                if not isinstance(when_local, str) or not when_local:
                    continue
                try:
                    ev_start = datetime.fromisoformat(when_local)
                    if ev_start.tzinfo is None:
                        ev_start = ev_start.astimezone()
                    ev_end = ev_start + timedelta(minutes=int(ev.get("duration_min") or 60))
                except (ValueError, TypeError):
                    continue
                title = ev.get("title") or "(untitled)"
                kind = ev.get("kind") or ""
                location = ev.get("location") or ""
                loc_part = f" at {location}" if location else ""
                kind_part = f" [{kind}]" if kind else ""
                wd = ev_start.strftime("%a")
                time_part = ev_start.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")
                end_time = ev_end.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")
                status = ev.get("status")
                if status == "planned" and ev_start <= now_dt < ev_end:
                    # Right now
                    in_progress.append(f"  IN PROGRESS now (until {end_time}): {title}{loc_part}{kind_part}")
                elif status == "planned" and ev_start > now_dt:
                    upcoming.append(f"  {wd} {time_part}: {title}{loc_part}{kind_part}")
                elif status in ("as_planned", "modified", "cancelled"):
                    marker = "happened" if status == "as_planned" else status
                    past.append(f"  {wd} {time_part}: {title}{kind_part} ({marker})")
            if in_progress or upcoming or past:
                parts.append("[SCHEDULE — where you are in time right now]:")
                parts.extend(in_progress)
                parts.extend(past)
                parts.extend(upcoming)

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
    # Scratchpad-status detection for the Pattern-C `reasoning` field.
    # JSON property order isn't formally guaranteed by spec, but Anthropic
    # models in practice emit keys in schema-declaration order with high
    # reliability. Detect the rare reorder/missing cases so we can audit
    # the empirical rate over time. Status surfaces on assistant_msg_data.
    scratchpad_status = "no_call"
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_inner_state":
            inp = block.input
            if isinstance(inp, dict):
                # Order check FIRST (before we filter to the 4 voice-visible
                # fields, since 'reasoning' is intentionally excluded from voice).
                ordered_keys = list(inp.keys())
                if not ordered_keys:
                    scratchpad_status = "no_output"
                elif "reasoning" not in inp:
                    scratchpad_status = "missing"
                elif ordered_keys[0] == "reasoning":
                    scratchpad_status = "ordered"
                else:
                    scratchpad_status = "reordered"
                # Filter to known fields and non-empty strings only — the schema
                # has all fields optional, so the model may omit some. The
                # `reasoning` field IS captured here (so it lands on
                # inner_state_payload for the user's later inspection) but is
                # NOT included in the voice-facing [INNER STATE] block — see
                # build_inner_state_injection, which only renders the four
                # labels. Reasoning is private scratchpad as far as voice is
                # concerned; persistence is purely for debug visibility.
                for k in ("reasoning", "feeling", "wanting", "noticing", "holding_back"):
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
        # Pattern-C scratchpad detection. One of:
        #   "ordered"    — reasoning was generated first, scratchpad worked
        #   "reordered"  — reasoning came after some other field (post-hoc rationalization)
        #   "missing"    — reasoning field absent entirely
        #   "no_output"  — tool_use block was empty
        #   "no_call"    — model didn't call the tool at all
        "scratchpad_status": scratchpad_status,
    }

    if inner_state:
        logger.info(
            f"character_inner_state: emitted {list(inner_state.keys())} "
            f"(scratchpad: {scratchpad_status})"
        )
    return inner_state, usage
