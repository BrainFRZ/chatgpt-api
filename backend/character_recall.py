"""Character recall pre-fetcher — Claude Haiku 4.5.

Runs BEFORE the correspondence model (Opus 3) streams its reply. Reads:
- The user's current message
- Branch-filtered indices of memories, user_profile, character_growth from the file store
- A short tail of recent dialogue for context

Decides which entries (by id) are relevant to surface this turn, on top of the
always-loaded "core" set. Returns full entries; the runtime stuffs them into
characters_state._render_payload before injection-build time.

Why Haiku (not Sonnet, not Opus): this is pattern-matching ("does this turn
touch on any of these topics?"), which Haiku excels at. Sonnet would be overkill;
Opus 3 is busy streaming. Per-turn cost is ~$0.001 cached.

Why pre-pass (not tool the correspondence model uses): keeps Opus 3 in pure prose
mode — no extra tool burden — and lets us batch-fetch, so the latency is one
short pause before streaming starts rather than 1-3 mid-stream tool-call pauses.

The tool is `report_recalls` with three id arrays. Empty arrays are correct most
turns — most exchanges are about whatever's currently in core, not deep recall.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

CHARACTER_RECALL_MODEL = "claude-haiku-4-5-20251001"
CHARACTER_RECALL_MAX_TOKENS = 512  # tool output is small (just id arrays)
CHARACTER_RECALL_TIMEOUT_S = 15

# Haiku 4.5 pricing
HAIKU_INPUT_RATE = 1.00
HAIKU_CACHE_READ_RATE = 0.10
HAIKU_CACHE_WRITE_RATE = 1.25
HAIKU_OUTPUT_RATE = 5.00

# Maximum ids to fetch per kind. The agent should be selective; this is a hard
# cap to prevent over-fetching.
RECALL_MAX_PER_KIND = 8

# Soft cap on how many index entries we expose to Haiku per kind. Past this we'd
# need a smarter index structure; for now scan the whole list.
INDEX_SOFT_CAP = 500


def compute_recall_cost(usage: dict) -> float:
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


SYSTEM_PROMPT = """You are a memory recall agent for a long-running character correspondence chat. Given the user's current message and an index of everything the character "knows" — memories of their relationship, durable facts about the user, and accumulated growth about themselves — decide which entries are likely to be relevant to THIS turn's reply.

You're not generating anything for the user. You're picking which of the character's deep memories should be brought to mind for THIS turn, on top of an always-loaded CORE set the character already has in context.

Be selective. Most turns will pull 0-3 entries total across all three indices. The character has a CORE always-loaded set of recent / high-impact items; you only need to add things outside that core that this turn specifically calls for.

Cues for relevance:
- User asks about a topic that matches an index entry (their job, a person, a past moment)
- User references an event the character should remember
- The character's reply will need to acknowledge a fact only stored deep
- The conversation is about to dip into a topic where extra context would prevent the character from contradicting themselves

DO NOT recall:
- Things obviously already in the character's profile (you don't have access to the profile, but if it'd be a "well duh, of course they know that" fact, it's probably already core or in the profile — leave it)
- Things tangentially related but not actually needed for this turn
- A long list of things "in case" — restraint is the point

Always call `report_recalls` exactly once. Empty arrays are common — most turns are about whatever's currently in core, not deep recall."""


def build_recall_tool() -> dict:
    return {
        "name": "report_recalls",
        "description": (
            "Pick relevant entries to bring to mind for this turn, in addition "
            "to the always-loaded core. Empty arrays are common."
        ),
        "input_schema": {
            "type": "object",
            "required": ["memory_ids", "profile_ids", "growth_ids"],
            "properties": {
                "memory_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": f"Memory ids to surface this turn ({0}-{RECALL_MAX_PER_KIND}). Empty array if nothing in the index applies.",
                },
                "profile_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": f"User profile ids to surface this turn ({0}-{RECALL_MAX_PER_KIND}).",
                },
                "growth_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": f"Growth ids to surface this turn ({0}-{RECALL_MAX_PER_KIND}).",
                },
            },
        },
    }


def _build_index_text(entries: list, kind: str, exclude_ids: Optional[set] = None) -> str:
    """Render a kind's index as a list of `id | summary` lines for the agent.

    Excludes entries whose ids are already in core (so the agent doesn't waste
    its quota re-recalling things already loaded). Caps at INDEX_SOFT_CAP.
    """
    if not entries:
        return "(none)"
    exclude_ids = exclude_ids or set()
    lines = []
    count = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = e.get("id")
        if eid in exclude_ids:
            continue
        if count >= INDEX_SOFT_CAP:
            lines.append(f"  ... ({len(entries) - count} more not shown)")
            break
        if kind == "memories":
            lines.append(
                f"  id={eid} | {e.get('impact', '?')}★ {e.get('date', '?')} | {e.get('text', '')[:140]}"
            )
        elif kind == "user_profile":
            cat = f"[{e.get('category')}] " if e.get('category') else ""
            lines.append(f"  id={eid} | {cat}{e.get('text', '')[:140]}")
        elif kind == "growth":
            if e.get("obsolete"):
                continue  # skip obsolete from recall consideration
            cat = f"[{e.get('category')}] " if e.get('category') else ""
            lines.append(f"  id={eid} | {cat}{e.get('text', '')[:140]}")
        count += 1
    return "\n".join(lines) if lines else "(none)"


def run_recall(
    client,
    project_dir: Optional[str],
    user_input: str,
    branch_msg_ids: Optional[set] = None,
    *,
    core_memory_ids: Optional[set] = None,
    core_profile_ids: Optional[set] = None,
    core_growth_ids: Optional[set] = None,
    recent_dialogue: Optional[list] = None,
) -> tuple[dict, dict]:
    """Run the Haiku recall pre-pass.

    Returns ({recalled_memories, recalled_profile, recalled_growth}, usage).

    Bails (returns empty) when:
    - project_dir is missing or has no store files yet (early in chat lifecycle)
    - All three indices are empty (no point asking the agent)
    - User input is empty
    - API call fails

    The "core" id sets are excluded from the index shown to the agent so it doesn't
    re-recall what's already loaded. Pass them in as the ids the runtime will
    include from store.core() regardless.

    recent_dialogue: optional list of recent exchanges to give the agent context.
    If None, the agent decides based on the current user input alone.
    """
    empty: dict = {"recalled_memories": [], "recalled_profile": [], "recalled_growth": []}
    if not user_input or not user_input.strip():
        return empty, {}
    if not project_dir:
        return empty, {}

    try:
        from character_storage import (
            CharacterStore, KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH,
        )
    except ImportError as e:
        logger.error(f"character_recall: failed to import storage: {e}")
        return empty, {}

    store = CharacterStore(project_dir)
    memories = store.read_filtered(KIND_MEMORIES, branch_msg_ids)
    profile = store.read_filtered(KIND_USER_PROFILE, branch_msg_ids)
    growth = store.read_filtered(KIND_GROWTH, branch_msg_ids)

    if not memories and not profile and not growth:
        # Nothing to recall — fresh chat
        return empty, {}

    # Build the user message for Haiku
    parts = [
        "[USER MESSAGE THIS TURN]",
        user_input.strip()[:2000],
        "",
    ]
    if recent_dialogue:
        # Render last few exchanges briefly for context
        parts.append("[RECENT DIALOGUE — last few exchanges]")
        for m in recent_dialogue[-6:]:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            parts.append(f"  {role}: {str(content)[:200]}")
        parts.append("")

    parts.append("[MEMORY INDEX — entries already in CORE are excluded; pick from these for additional recall]")
    parts.append(_build_index_text(memories, "memories", exclude_ids=core_memory_ids or set()))
    parts.append("")
    parts.append("[USER PROFILE INDEX — same: core excluded]")
    parts.append(_build_index_text(profile, "user_profile", exclude_ids=core_profile_ids or set()))
    parts.append("")
    parts.append("[GROWTH INDEX — core excluded; obsolete excluded]")
    parts.append(_build_index_text(growth, "growth", exclude_ids=core_growth_ids or set()))
    parts.append("")
    parts.append("Now decide which ids to recall. Empty arrays are correct most turns. Call report_recalls.")

    user_msg = "\n".join(parts)
    tool = build_recall_tool()

    try:
        response = client.messages.create(
            model=CHARACTER_RECALL_MODEL,
            max_tokens=CHARACTER_RECALL_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_recalls"},
            timeout=CHARACTER_RECALL_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"character_recall: API call failed: {type(e).__name__}: {e}")
        return empty, {}

    ops: dict = {}
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_recalls":
            inp = block.input
            if isinstance(inp, dict):
                ops = inp
            break

    memory_ids = ops.get("memory_ids") or []
    profile_ids = ops.get("profile_ids") or []
    growth_ids = ops.get("growth_ids") or []

    # Cap each list
    if not isinstance(memory_ids, list):
        memory_ids = []
    if not isinstance(profile_ids, list):
        profile_ids = []
    if not isinstance(growth_ids, list):
        growth_ids = []
    memory_ids = [int(i) for i in memory_ids[:RECALL_MAX_PER_KIND] if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]
    profile_ids = [int(i) for i in profile_ids[:RECALL_MAX_PER_KIND] if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]
    growth_ids = [int(i) for i in growth_ids[:RECALL_MAX_PER_KIND] if isinstance(i, (int, str)) and str(i).lstrip("-").isdigit()]

    # Fetch full entries (re-apply branch filter for safety)
    recalled_memories = store.fetch_by_ids(KIND_MEMORIES, memory_ids, branch_msg_ids)
    recalled_profile = store.fetch_by_ids(KIND_USER_PROFILE, profile_ids, branch_msg_ids)
    recalled_growth = store.fetch_by_ids(KIND_GROWTH, growth_ids, branch_msg_ids)

    # Bump last_referenced_date on surfaced memories. Keeps frequently-recalled
    # entries alive through the hygiene pass — even a 1★ memory that the
    # character keeps revisiting won't get archived because each recall renews it.
    if memory_ids:
        from datetime import datetime
        from game_systems.characters import TZ
        today_iso = datetime.now(TZ).date().isoformat()
        try:
            store.bump_last_referenced(KIND_MEMORIES, memory_ids, today_iso)
        except Exception as e:
            logger.warning(f"character_recall: bump_last_referenced failed: {e}")

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens
        + (getattr(ru, 'cache_read_input_tokens', 0) or 0)
        + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }

    if memory_ids or profile_ids or growth_ids:
        logger.info(
            f"character_recall: surfaced "
            f"memories={memory_ids}, profile={profile_ids}, growth={growth_ids}"
        )

    return (
        {
            "recalled_memories": recalled_memories,
            "recalled_profile": recalled_profile,
            "recalled_growth": recalled_growth,
            "ids_recalled": {
                "memory_ids": memory_ids,
                "profile_ids": profile_ids,
                "growth_ids": growth_ids,
            },
        },
        usage,
    )
