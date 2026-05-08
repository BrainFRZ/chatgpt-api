"""Character consolidation agent — Claude Opus 4.5.

When invoked via /consolidate, this reads:
- The current canonical character_profile.di (as written by the user via interview)
- The character_growth state (emergent facts the Haiku side agent has logged)
- High-impact, persistent character memories (candidates for graduation)

…and proposes an updated character_profile.di that merges the durable additions in
while preserving all the user-curated material verbatim. Returns the proposal plus
the lists of growth ids and memory ids it pulled in, so the runtime can clear /
mark-graduated only the ones the model actually used.

This is the "long-term character maintenance" loop: rather than rewriting the
profile silently mid-conversation (drift risk), growth + memories accumulate
between consolidations, then the user explicitly invokes /consolidate, reviews,
and accepts.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

CONSOLIDATE_MODEL = "claude-opus-4.5"
CONSOLIDATE_MAX_TOKENS = 8192
CONSOLIDATE_TIMEOUT_S = 60

OPUS_45_INPUT_RATE = 5.00
OPUS_45_CACHE_READ_RATE = 0.50
OPUS_45_CACHE_WRITE_RATE = 10.00   # 1hr cache write (2x base)
OPUS_45_OUTPUT_RATE = 25.00


def compute_consolidate_cost(usage: dict) -> float:
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    uncached_input = max(0, raw_input - cache_read - cache_write)
    return (
        uncached_input * OPUS_45_INPUT_RATE
        + cache_read * OPUS_45_CACHE_READ_RATE
        + cache_write * OPUS_45_CACHE_WRITE_RATE
        + output * OPUS_45_OUTPUT_RATE
    ) / 1_000_000.0


SYSTEM_PROMPT = """You are consolidating a character profile after a long stretch of correspondence. The user authored an original character_profile.di through an interview. Since then, two things have accumulated:

1. **GROWTH ENTRIES** — facts about the character that emerged through dialogue (new hobbies, opinions, relationships, evolved preferences) and were logged turn-by-turn by a side agent.

2. **MEMORY CANDIDATES** — high-impact, long-persisting moments from the conversation that may rise to identity-level rather than mere memory.

Your job: produce an UPDATED character_profile.di that integrates the durable additions while preserving everything the user wrote verbatim.

# What to merge

- **Growth entries that are durable.** A finished hobby that's still active months later, a hot take they've held for weeks, a person who's clearly part of their life now. If a growth entry is recent (last 1-2 weeks) and might still be ephemeral, do NOT merge — leave it for next consolidation.
- **Obsolete growth entries** drop out of the profile entirely. Don't preserve "she used to read LotR" unless it's narratively significant.
- **Memory candidates that have become identity-shaping.** A repeated dynamic ("she always defends the user when their mom is mentioned"), a confession that recontextualizes the relationship, a turning point that has visibly altered tone. NOT one-off intense moments unless they truly changed who the character is.

# What NOT to do

- **Do not paraphrase or rewrite user-authored sections.** If the user wrote "she gets softer when Terry is hurting — teasing stops, presence stays," that exact line stays. You may add adjacent material. You may NOT rephrase.
- **Do not invent.** Only use material from the inputs.
- **Do not delete user-authored material** unless an obsolete growth entry clearly contradicts it (e.g. user wrote "single" but the character has been in a relationship for months — flag in commentary, propose update).
- **Do not over-merge.** Restraint is the signal of good consolidation. Most growth entries shouldn't be merged on the first consolidation pass. Better to wait and confirm durability.

# Output

You MUST emit two pieces, in this exact order, separated by the line `===PROFILE===`:

1. A short COMMENTARY (~3-8 lines) describing what you did:
   - Which growth ids you merged in (list the ids)
   - Which memory ids you elevated to profile-level (list the ids)
   - Anything you flagged as a contradiction or considered but skipped
   - Anything you'd recommend the user reconsider

2. The literal updated character_profile.di markdown — full file content. First line is the `# {Name}` header, last line is the last line of the profile.

Example of the separator usage:

```
COMMENTARY:
- Merged growth ids: [4, 7, 12] — knitting milestone, the new co-worker is now established, hot take on Beach House
- Elevated memory ids: [3] — the late-night call after the breakup is now a relationship anchor in the Relationship section
- Skipped growth id 8 (only 4 days old, unclear if durable)
- Note: growth id 9 ("works night shifts now") contradicts the original "9-to-5 corporate writer" — propose updating but leaving prior phrasing intact for now
===PROFILE===
# Nora

## Identity
- **Name:** Nora ...
[full updated profile]
```

If there's literally nothing worth merging on this pass, output the commentary explaining why, then `===PROFILE===`, then the existing profile UNCHANGED. Do not return an empty profile."""


def _format_growth_for_prompt(growth_state: dict) -> str:
    if not isinstance(growth_state, dict):
        return "(no growth entries)"
    entries = growth_state.get("entries") or []
    active = [g for g in entries if isinstance(g, dict) and not g.get("obsolete")]
    obsolete = [g for g in entries if isinstance(g, dict) and g.get("obsolete")]
    if not active and not obsolete:
        return "(no growth entries)"
    lines = []
    if active:
        lines.append("ACTIVE growth entries:")
        for g in active:
            cat = g.get("category") or "other"
            date = g.get("date") or "?"
            text = g.get("text") or ""
            src = g.get("source") or ""
            updated = f" [updated {g['last_updated']}]" if g.get("last_updated") else ""
            lines.append(f"  id={g.get('id')} | {date} | {cat} | source={src}{updated} | {text}")
    if obsolete:
        lines.append("\nOBSOLETE growth entries (no longer true; drop unless narratively significant):")
        for g in obsolete:
            text = g.get("text") or ""
            obs_date = g.get("obsolete_date") or "?"
            reason = g.get("obsolete_reason") or ""
            lines.append(f"  id={g.get('id')} | ended {obs_date} | was: {text} | reason: {reason}")
    return "\n".join(lines)


def _format_memories_for_prompt(memories: list) -> str:
    if not isinstance(memories, list) or not memories:
        return "(no memory candidates)"
    # Filter to high-tier, surviving memories — graduation candidates
    candidates = [
        m for m in memories
        if isinstance(m, dict)
        and (m.get("tier") in ("high", "moderate") or (m.get("impact") or 0) >= 3)
    ]
    if not candidates:
        return "(no high-impact memory candidates — only flavor-tier present)"
    lines = ["High-impact memories (graduation candidates):"]
    for m in candidates[:25]:  # cap to keep the prompt bounded
        impact = m.get("impact", "?")
        date = m.get("date") or "?"
        text = m.get("text") or ""
        focus = f" [{m.get('focus')}]" if m.get("focus") else ""
        quote = f' — "{m.get("quote")}"' if m.get("quote") else ""
        lines.append(f"  ({impact}★ {date}){focus} {text}{quote}")
    return "\n".join(lines)


def _parse_consolidate_output(text: str) -> tuple[str, str, list, list]:
    """Split the model's output into (commentary, proposed_profile, merged_growth_ids, elevated_memory_ids).

    Robust to minor formatting deviations: looks for the ===PROFILE=== sentinel,
    falls back to splitting on the first line that starts with '#' followed by an
    H1 heading if the sentinel is absent.
    """
    if not text:
        return "", "", [], []
    text = text.strip()

    # Primary split on the sentinel
    if "===PROFILE===" in text:
        commentary, _, profile = text.partition("===PROFILE===")
    else:
        # Fallback: find the first line that's an H1 (`# X`) — that's where the profile starts
        m = re.search(r"^#\s+\S", text, re.MULTILINE)
        if m:
            commentary = text[:m.start()].strip()
            profile = text[m.start():].strip()
        else:
            # No clear split; treat the whole thing as profile, no commentary
            commentary = ""
            profile = text

    commentary = commentary.strip()
    profile = profile.strip()

    # Strip wrapping code fences if present
    profile = re.sub(r"^```(?:markdown|md)?\s*\n", "", profile)
    profile = re.sub(r"\n```\s*$", "", profile)

    # Extract id lists from the commentary
    merged_growth_ids = _extract_ids(commentary, r"merged\s+growth\s+ids?\s*:?\s*\[?([\d,\s]+)\]?")
    elevated_memory_ids = _extract_ids(commentary, r"elevated\s+memory\s+ids?\s*:?\s*\[?([\d,\s]+)\]?")

    return commentary, profile, merged_growth_ids, elevated_memory_ids


def _extract_ids(text: str, pattern: str) -> list:
    ids: list = []
    if not text:
        return ids
    for match in re.finditer(pattern, text, re.IGNORECASE):
        for piece in re.split(r"[,\s]+", match.group(1) or ""):
            piece = piece.strip()
            if piece.isdigit():
                ids.append(int(piece))
    # de-dupe preserving order
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def run_consolidation(
    client,
    profile_doc: str,
    growth_state: dict,
    memories: list,
) -> tuple[Optional[dict], dict]:
    """Run the consolidate call. Returns (result_or_None, usage_dict).

    result shape:
        {
          "commentary": str,
          "proposed_profile": str,         # full markdown
          "merged_growth_ids": [int],
          "elevated_memory_ids": [int],
        }

    Returns (None, {}) on missing inputs or hard errors.
    """
    if not profile_doc or not profile_doc.strip():
        return None, {}

    growth_text = _format_growth_for_prompt(growth_state or {})
    memories_text = _format_memories_for_prompt(memories or [])

    user_msg = (
        "[CURRENT character_profile.di — preserve verbatim where not contradicted by growth]\n\n"
        + profile_doc.strip()
        + "\n\n"
        + "[GROWTH STATE]\n\n"
        + growth_text
        + "\n\n"
        + "[MEMORIES]\n\n"
        + memories_text
        + "\n\n"
        + "Now produce the consolidation: commentary first, then `===PROFILE===` separator, then the updated profile. "
        + "Restraint is the signal of good consolidation."
    )

    try:
        response = client.messages.create(
            model=CONSOLIDATE_MODEL,
            max_tokens=CONSOLIDATE_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            timeout=CONSOLIDATE_TIMEOUT_S,
        )
    except Exception as e:
        logger.error(f"character_consolidate: API call failed: {type(e).__name__}: {e}")
        return None, {}

    raw_text = ""
    for block in response.content:
        if block.type == "text":
            raw_text += block.text
    raw_text = raw_text.strip()

    commentary, proposed_profile, merged_ids, elevated_ids = _parse_consolidate_output(raw_text)

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens
        + (getattr(ru, 'cache_read_input_tokens', 0) or 0)
        + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }

    if not proposed_profile or len(proposed_profile) < 200:
        logger.warning(
            f"character_consolidate: proposed profile suspiciously short ({len(proposed_profile)} chars); "
            f"raw output starts: {raw_text[:200]!r}"
        )
        return None, usage

    return (
        {
            "commentary": commentary,
            "proposed_profile": proposed_profile,
            "merged_growth_ids": merged_ids,
            "elevated_memory_ids": elevated_ids,
        },
        usage,
    )
