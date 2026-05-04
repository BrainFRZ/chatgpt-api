"""Decision-flag side agent — Claude Haiku 4.5.

After the main turn completes, this side agent reads the narration that
was produced, the user's latest input, the canonical flag list, and
the current decision-flag state. It emits a plot_ops array determining
which flags should be set or updated for this turn.

Sole owner of plot_ops on the stateful path. The main report_state
tool no longer carries plot_ops; the main model focuses on narration +
scene/character state and is not asked to do flag bookkeeping.

Internal-only — not exposed in the model dropdown. Always Haiku 4.5;
no fallback model, no per-project model selection.
"""
from __future__ import annotations

import json
import logging
import os
import copy
from typing import Optional

logger = logging.getLogger(__name__)

FLAG_AGENT_MODEL = "claude-haiku-4-5-20251001"
FLAG_AGENT_MAX_TOKENS = 1024
FLAG_AGENT_TIMEOUT_S = 15

# Haiku 4.5 pricing per MTok ($/1M tokens). Update if Anthropic changes.
HAIKU_INPUT_RATE = 1.00          # uncached input
HAIKU_CACHE_READ_RATE = 0.10     # cached input read
HAIKU_CACHE_WRITE_RATE = 1.25    # 5min cache write (1hr is 2.00 — flag agent doesn't use cache_control today)
HAIKU_OUTPUT_RATE = 5.00         # output


def compute_flag_agent_cost(usage: dict) -> float:
    """Dollar cost of a flag-agent call given its usage dict (same shape
    other usage dicts in this codebase use). Returns 0.0 on empty input.
    """
    if not isinstance(usage, dict) or not usage:
        return 0.0
    raw_input = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_tokens", 0) or 0
    cache_write = usage.get("cache_creation_tokens", 0) or 0
    output = usage.get("output_tokens", 0) or 0
    # `input_tokens` here is the codebase's normalized total (raw + cache_read + cache_write);
    # subtract the cached portions to get the uncached input.
    uncached_input = max(0, raw_input - cache_read - cache_write)
    cost = (
        (uncached_input * HAIKU_INPUT_RATE
         + cache_read * HAIKU_CACHE_READ_RATE
         + cache_write * HAIKU_CACHE_WRITE_RATE
         + output * HAIKU_OUTPUT_RATE)
        / 1_000_000.0
    )
    return cost

SYSTEM_PROMPT = """You are a decision-flag tracker for a tabletop RPG campaign. After each turn, you read what just happened and determine which campaign decision flags should be set, updated, or pre-registered.

You receive:
- The canonical flag manifest (the only flag keys you may emit; descriptions tell you what each flag tracks)
- The current state of all flags
- Scene state (active_tensions captures multi-turn dynamics like "Red has been pressing Delphi" — load-bearing for gradient flags)
- The user's latest input
- The narration the GM just produced

Your job is narrow:
1. Recognize when an event in the narration matches a flag's trigger condition.
2. Emit a plot_op for that flag with the correct value.
3. If a flag is canonical but missing from the current state, pre-register it as pending (`value: "pending"`).
4. Skip flags that don't need to change. Most turns have zero flag changes — that's fine.

Hard rules:
- NEVER emit a flag key that isn't in the canonical list. The schema enum will reject unknown keys, but don't try.
- Do NOT over-emit. If a flag is already at the correct value, don't re-set it.
- Resolved branch flags can NOT be overwritten — don't try to change them.
- Pre-register only canonical flags that are MISSING from current state, not flags already pending.
- For numeric flags (e.g., MAINLINE_HEAT), the new value should reflect the post-turn total, not the delta. If MAINLINE_HEAT is currently 2 and the turn adds 1, emit `value: "3"`.
- For enum flags, use ONLY the values listed in the flag's description.
- For boolean flags, emit `value: "true"` or `value: "false"`.
- Severity: `branch` for permanent fork resolutions, `flag` for trackable variables (most cases), `divergence` only if the player went off-script.

Always call `report_flag_ops` exactly once per turn — even if the array is empty."""


def _build_canonical_flags_text(canonical_flags_md: str) -> str:
    """Pass through the canonical flags doc — descriptions matter for judgment."""
    return canonical_flags_md.strip()


def _build_state_text(decision_flags: dict) -> str:
    """Render current decision_flags as a compact human-readable list."""
    if not decision_flags:
        return "(no flags currently set or pre-registered)"
    lines = []
    for key, entry in decision_flags.items():
        if isinstance(entry, dict):
            v = entry.get("value")
            sev = entry.get("severity", "")
            if v is None:
                lines.append(f"- {key}: pending" + (f" ({sev})" if sev and sev != "flag" else ""))
            else:
                lines.append(f"- {key}: {v}" + (f" ({sev})" if sev and sev != "flag" else ""))
        else:
            lines.append(f"- {key}: {entry}")
    return "\n".join(lines)


def _build_scene_text(scene_state: dict) -> str:
    """Render the relevant slice of scene_state for flag judgment.

    Includes only fields useful for evaluating multi-turn dynamics:
    active_tensions (the multi-turn pattern capture), atmosphere,
    location, details, pending_actions. Strips presence lists and
    other noise that doesn't inform flag decisions.
    """
    if not isinstance(scene_state, dict) or not scene_state:
        return "(no scene state)"
    parts = []
    location = scene_state.get("location")
    if location:
        parts.append(f"Location: {location}")
    atmosphere = scene_state.get("atmosphere")
    if atmosphere:
        parts.append(f"Atmosphere: {atmosphere}")
    tensions = scene_state.get("active_tensions") or []
    if isinstance(tensions, list) and tensions:
        parts.append("Active tensions (multi-turn dynamics in this scene):")
        for t in tensions:
            parts.append(f"  - {t}")
    details = scene_state.get("details") or []
    if isinstance(details, list) and details:
        parts.append("Scene details:")
        for d in details[:8]:  # cap to keep it tight
            parts.append(f"  - {d}")
    pending = scene_state.get("pending_actions") or []
    if isinstance(pending, list) and pending:
        parts.append("Pending actions:")
        for p in pending[:6]:
            parts.append(f"  - {p}")
    if not parts:
        return "(no scene state)"
    return "\n".join(parts)


def _read_canonical_flags_md(uploads_dir: str) -> str:
    """Load decision_flags.md content for the side agent's context."""
    if not uploads_dir:
        return ""
    path = os.path.join(uploads_dir, "decision_flags.md")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ""


def build_flag_agent_tool(canonical_flags: list[str]) -> dict:
    """Tool definition for the side agent. Enum on `key` is the schema-level
    constraint that reinforces the system prompt's "never invent keys" rule.
    """
    return {
        "name": "report_flag_ops",
        "description": "Emit decision flag operations for this turn. Empty plot_ops array is fine when no flags change.",
        "input_schema": {
            "type": "object",
            "required": ["plot_ops"],
            "properties": {
                "plot_ops": {
                    "type": "array",
                    "description": "Decision flag operations from this turn. Empty array if no flags changed.",
                    "items": {
                        "type": "object",
                        "required": ["key", "decision"],
                        "properties": {
                            "key": {
                                "type": "string",
                                "enum": list(canonical_flags) if canonical_flags else [],
                                "description": "Canonical flag name. MUST be one of the listed enum values."
                            },
                            "value": {
                                "type": ["string", "null"],
                                "description": "The new value (string form). Use 'pending' to pre-register an unset flag. null to clear."
                            },
                            "decision": {
                                "type": "string",
                                "description": "Brief description of what triggered this flag change (1 sentence)."
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["branch", "flag", "divergence"],
                                "description": "branch=permanent fork resolution, flag=trackable variable (most cases), divergence=player off-script."
                            }
                        }
                    }
                }
            }
        }
    }


def determine_flag_ops(
    client,
    uploads_dir: Optional[str],
    canonical_flags: list[str],
    current_decision_flags: dict,
    user_input: str,
    narration: str,
    scene_state: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """Run the side agent. Returns (plot_ops_list, usage_dict).

    On any failure (no canonical flags, API error, timeout, malformed output)
    returns ([], {}) — caller treats as "no flag changes this turn." This is
    a soft fallback rather than failing the whole turn.

    scene_state captures multi-turn dynamics (active_tensions, atmosphere)
    that single-turn narration misses. Pass it for gradient flags like
    MORI_TRUST and COMPARTMENT_STRAIN where the relevant signal is "is
    this dynamic active in the scene?" rather than "did it just happen?"
    """
    if not canonical_flags:
        return [], {}
    if not narration or not narration.strip():
        return [], {}

    canonical_md = _read_canonical_flags_md(uploads_dir or "")
    if not canonical_md:
        # Can't run without context
        return [], {}

    user_msg = f"""[CANONICAL FLAGS]
{canonical_md}

[CURRENT FLAG STATE]
{_build_state_text(current_decision_flags or {})}

[SCENE STATE]
{_build_scene_text(scene_state or {})}

[USER INPUT THIS TURN]
{(user_input or '').strip()}

[GM NARRATION THIS TURN]
{narration.strip()}

Determine flag ops for this turn. Call report_flag_ops with the appropriate plot_ops array (empty array if no flags change)."""

    tool = build_flag_agent_tool(canonical_flags)
    try:
        response = client.messages.create(
            model=FLAG_AGENT_MODEL,
            max_tokens=FLAG_AGENT_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_flag_ops"},
            timeout=FLAG_AGENT_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"flag_agent: API call failed: {type(e).__name__}: {e}")
        return [], {}

    plot_ops = []
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_flag_ops":
            inp = block.input
            if isinstance(inp, dict):
                ops = inp.get("plot_ops")
                if isinstance(ops, list):
                    plot_ops = ops
            break

    ru = response.usage
    usage = {
        "input_tokens": ru.input_tokens + (getattr(ru, 'cache_read_input_tokens', 0) or 0) + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }

    if plot_ops:
        logger.info(f"flag_agent: emitted {len(plot_ops)} plot_op(s); keys={[op.get('key') for op in plot_ops if isinstance(op, dict)]}")
    return plot_ops, usage
