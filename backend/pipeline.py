"""
Multi-agent TTRPG pipeline for GPT-5.2 project chats.

Three-stage pipeline: Events → Mechanics → Narration
Each stage has its own reasoning effort, service tier, and context window.
Only activates for GPT-5.2 project chats; Anthropic models use the existing single-agent flow.
"""

import copy
import json
import logging
from dataclasses import dataclass
from typing import Optional, Iterator

from providers import ParsedResponse, StreamEvent, Pricing
from providers.openai_provider import OpenAIProvider, FLEX_PRICING, STANDARD_PRICING

logger = logging.getLogger(__name__)

# Contracts and state tools are now in game_systems/ modules.
# Imported here for backward compatibility with main.py imports.
from game_systems.dnd5e import (
    SINGLE_AGENT_STATE_CONTRACT,
    STATE_REPORT_TOOL,
)

# ============================================================
# Pipeline Stage Configuration
# ============================================================

@dataclass
class StageConfig:
    """Configuration for a pipeline stage."""
    name: str
    reasoning_effort: str
    service_tier: str
    json_mode: bool


STAGE_CONFIGS = {
    "events": StageConfig(
        name="events",
        reasoning_effort="medium",
        # service_tier="flex",  # Flex disabled — use standard for reliability
        service_tier="auto",
        json_mode=True,
    ),
    "mechanics": StageConfig(
        name="mechanics",
        # reasoning_effort="high",  # Lowered to medium — high was slow for marginal gain
        reasoning_effort="medium",
        service_tier="auto",
        json_mode=True,
    ),
    "narration": StageConfig(
        name="narration",
        reasoning_effort="low",
        service_tier="auto",
        json_mode=False,
    ),
}

# Threshold/target pairs for agent context windows (prefix-caching friendly)
# Context grows by appending until threshold is exceeded, then trims to target.
# ~95% of turns preserve the prefix for OpenAI prompt caching.
EVENTS_THRESHOLD_PAIRS = 40
EVENTS_TARGET_PAIRS = 20
NARRATION_THRESHOLD_PAIRS = 40
NARRATION_TARGET_PAIRS = 20

# State management constants
CALLBACK_RESOLVED_RETENTION = 20  # Turns to keep resolved callbacks before pruning
NPC_MEMORY_TIER_LIMITS = {"high": 8, "moderate": 10, "flavor": 12}
NPC_MEMORY_MAX_PER_NPC = 30
CHARACTER_STATE_TTL = 150  # Prune NPC character states not updated in this many turns

# ============================================================
# Pipeline Result
# ============================================================

@dataclass
class PipelineStageResult:
    """Result from a single pipeline stage."""
    stage: str
    content: str  # Raw text output from the API
    parsed_json: Optional[dict]  # Parsed JSON (None for Narration)
    usage: dict  # Token usage dict from provider
    service_tier: str  # Actual tier used


@dataclass
class PipelineResult:
    """Aggregate result from the full pipeline."""
    final_content: str  # The text the user sees
    events_json: Optional[str]  # Raw Events JSON string (for storage)
    mechanics_json: Optional[str]  # Raw Mechanics JSON string (for storage)
    stages_run: list[str]  # e.g. ["events", "mechanics", "narration"]
    aggregate_usage: dict  # Combined token usage
    aggregate_cost: float  # Total cost across all stages
    pipeline_state: Optional[dict]  # Updated pipeline state from Events
    reasoning_summaries: list[str]  # Reasoning from each stage
    service_tier_label: str  # e.g. "flex+standard"
    injected_state: Optional[str] = None  # Snapshot of pipeline_state injected into Events
    stage_usage: Optional[dict] = None  # Per-stage usage: {"events": {...}, "mechanics": {...}, "narration": {...}}


# ============================================================
# Pipeline Functions
# ============================================================

def _parse_stage_json(content: str, stage_name: str) -> dict:
    """Parse JSON output from a pipeline stage, with cleanup for common issues."""
    text = content.strip()
    # Strip markdown code fences if the model wrapped its JSON
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Pipeline {stage_name}: Failed to parse JSON: {e}\nContent: {text[:500]}")
        raise ValueError(f"Pipeline {stage_name} produced invalid JSON: {e}")


def build_events_messages(
    system_prompt: str,
    history_messages: list[dict],
    user_message: dict,
    pipeline_state: dict,
    game_system: dict = None
) -> list[dict]:
    """
    Build the message list for the Events agent.

    Events sees recent conversation pairs plus persistent state injections:
    [PIPELINE STATE] (pacing), [CALLBACK LEDGER], [NPC MEMORIES], [SCENE STATE], [CHARACTER STATES],
    [INVESTIGATOR STATE] (game-specific)
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_messages)

    # Build the final user message with all injections
    user_content = user_message["content"]

    injections = []

    # 1. Pacing state (compact JSON)
    pacing = pipeline_state.get("pacing", {})
    if pacing:
        injections.append(f"[PIPELINE STATE]\n{json.dumps(pacing, indent=2)}\n[/PIPELINE STATE]")

    # 2. Callback ledger
    cb_injection = build_callback_injection(
        pipeline_state.get("callback_ledger", {}),
        turn_counter=pipeline_state.get("turn_counter", 0)
    )
    if cb_injection:
        injections.append(cb_injection)

    # 3. NPC memories (scene-scoped)
    mem_injection = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem_injection:
        injections.append(mem_injection)

    # 4. Scene state
    scene_injection = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    if scene_injection:
        injections.append(scene_injection)

    # 5. Character states
    cs_injection = build_character_states_injection(pipeline_state.get("character_states", {}))
    if cs_injection:
        injections.append(cs_injection)

    # 6. Game-specific state injection (e.g. [INVESTIGATOR STATE] for CoC 7E)
    if game_system and game_system.get("build_game_injection"):
        game_injection = game_system["build_game_injection"](pipeline_state.get("game_state", {}))
        if game_injection:
            injections.append(game_injection)

    if injections:
        user_content = "\n\n".join(injections) + "\n\n" + user_content

    messages.append({"role": "user", "content": user_content})
    return messages


def build_mechanics_messages(
    system_prompt: str,
    events_json: dict,
) -> list[dict]:
    """
    Build the message list for the Mechanics agent.

    Mechanics is stateless: only system prompt + Events JSON output.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.append({"role": "user", "content": json.dumps(events_json, indent=2)})
    return messages


def build_narration_messages(
    system_prompt: str,
    recent_pairs: list[dict],
    mechanics_json: dict
) -> list[dict]:
    """
    Build the message list for the Narration agent.

    Narration sees the last N user-assistant pairs for voice consistency,
    plus the Mechanics JSON as the current user message.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(recent_pairs)
    messages.append({"role": "user", "content": json.dumps(mechanics_json, indent=2)})
    return messages


def build_agent_system_prompt(contract: str, instructions: str, project_files: str) -> str:
    """
    Build the full system prompt for a pipeline agent.

    Structure: contract + user instructions + project files
    """
    parts = [contract]
    if instructions.strip():
        parts.append(instructions)
    if project_files.strip():
        parts.append(project_files)
    return "\n\n".join(parts)


def build_message_content(msg: dict) -> str:
    """Build message content string, including any attached files."""
    content = msg.get("content", "")
    attached = msg.get("attached_files", [])
    if attached:
        file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
        files_text = "\n\n".join(file_wrappers)
        content = f"{files_text}\n\n{content}"
    return content


def get_context_pairs(branch_path: list[dict], threshold_pairs: int, target_pairs: int) -> list[dict]:
    """
    Extract context pairs using threshold/target for prefix-cache-friendly behavior.

    Context grows by appending (preserving the prefix for caching) until
    total pairs exceed threshold_pairs, then trims back to target_pairs.
    Skips the system message (index 0) and the current user message (last).
    Returns pairs as a flat list of {role, content} dicts.
    """
    # branch_path: [system, ...history..., current_user]
    history = branch_path[1:-1]  # All messages between system and current user
    total_pairs = len(history) // 2

    if total_pairs > threshold_pairs:
        pair_messages = history[-(target_pairs * 2):]  # trim to target
    else:
        pair_messages = history  # include all — prefix preserved for caching

    return [{"role": msg["role"], "content": build_message_content(msg)} for msg in pair_messages]


# ============================================================
# Pipeline State Migration & Op-Application
# ============================================================

def _fresh_pipeline_state() -> dict:
    """Return a fresh pipeline_state with all sub-structures initialized."""
    return {
        "pacing": {},
        "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
        "npc_memories": {},
        "scene_state": {},
        "character_states": {},
        "game_state": {},
        "hud_state": {},
        "combat": None,
        "turn_counter": 0
    }


def migrate_pipeline_state(state: Optional[dict]) -> dict:
    """Migrate pipeline_state from any legacy format to the current nested structure."""
    if state is None:
        return _fresh_pipeline_state()

    # Old flat format: state IS the pacing dict (has keys like "episode", "beat" but no "pacing" key)
    if "pacing" not in state:
        return {
            "pacing": state,
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "game_state": {},
            "hud_state": {},
            "combat": None,
            "turn_counter": 0
        }

    # New format — ensure all keys exist
    state.setdefault("pacing", {})
    state.setdefault("callback_ledger", {"next_id": 1, "open": [], "recently_resolved": []})
    ledger = state["callback_ledger"]
    ledger.setdefault("next_id", 1)
    ledger.setdefault("open", [])
    ledger.setdefault("recently_resolved", [])
    state.setdefault("npc_memories", {})
    state.setdefault("scene_state", {})
    state.setdefault("character_states", {})
    # Migrate character_states to current format: {"data": {...}, "last_updated": N}
    cs = state["character_states"]
    for name, entry in cs.items():
        if not isinstance(entry, dict):
            # Bare string → wrap
            cs[name] = {"data": {"summary": str(entry)}, "last_updated": state.get("turn_counter", 0)}
        elif "state" in entry and "data" not in entry:
            # Old {"state": str, "last_updated": N} → migrate to {"data": {...}, ...}
            old_state = entry.pop("state")
            if isinstance(old_state, dict):
                entry["data"] = old_state
            else:
                entry["data"] = {"summary": str(old_state)}
        elif "data" not in entry:
            # Dict without "data" key — treat entire dict as the data
            cs[name] = {"data": entry, "last_updated": state.get("turn_counter", 0)}
    state.setdefault("game_state", {})
    state.setdefault("hud_state", {})
    state.setdefault("combat", None)
    state.setdefault("turn_counter", 0)
    return state


def apply_callback_ops(ledger: dict, ops: list, current_turn: int) -> dict:
    """Apply callback_ops to the ledger and prune old resolved entries."""
    open_list = ledger.get("open", [])
    resolved_list = ledger.get("recently_resolved", [])
    next_id = ledger.get("next_id", 1)

    # Build index of open callbacks by ID for fast lookup
    open_by_id = {cb["id"]: cb for cb in open_list}

    for op in (ops or []):
        action = op.get("action")

        if action == "add":
            text = op.get("original_text", "")[:800]
            resolutions = op.get("resolutions") or []
            resolutions = [str(r)[:200] for r in resolutions[:3]]
            entry = {
                "id": next_id,
                "created_turn": current_turn,
                "original_text": text,
                "source_npc": op.get("source_npc"),
                "resolutions": resolutions or None
            }
            open_by_id[next_id] = entry
            next_id += 1

        elif action == "resolve":
            target_id = op.get("id")
            if target_id is None or target_id not in open_by_id:
                logger.warning(f"callback_ops resolve: ID {target_id} not found in open callbacks")
                continue
            entry = open_by_id.pop(target_id)
            entry["resolved_turn"] = current_turn
            entry["resolution_text"] = op.get("resolution_text", "")
            resolved_list.append(entry)

        elif action == "update":
            target_id = op.get("id")
            if target_id is None or target_id not in open_by_id:
                logger.warning(f"callback_ops update: ID {target_id} not found in open callbacks")
                continue
            fields = op.get("fields", {})
            for k, v in fields.items():
                if k not in ("id", "created_turn"):  # Protect immutable fields
                    open_by_id[target_id][k] = v

    # Prune old resolved entries
    resolved_list = [
        r for r in resolved_list
        if current_turn - r.get("resolved_turn", current_turn) <= CALLBACK_RESOLVED_RETENTION
    ]

    return {
        "next_id": next_id,
        "open": list(open_by_id.values()),
        "recently_resolved": resolved_list
    }


def _memory_tier(impact) -> str:
    """Map impact score (1-5) to tier name."""
    try:
        impact = int(impact)
    except (TypeError, ValueError):
        return "flavor"
    if impact >= 4:
        return "high"
    elif impact == 3:
        return "moderate"
    else:
        return "flavor"


def _memory_sort_key(m: dict) -> tuple:
    """Sort key for NPC memories — matches injection display order."""
    return (m.get("impact", 0), m.get("turn_created", 0))


def apply_npc_memory_ops(memories: dict, ops: list, current_turn: int) -> dict:
    """Apply npc_memory_ops (add/drop) to the NPC memories dict."""
    if not ops:
        return memories

    # Sort all NPC lists to match injection display order BEFORE processing drops,
    # so drop indices align with what the model saw in [NPC MEMORIES] blocks
    for npc_name in memories:
        memories[npc_name] = sorted(memories[npc_name], key=_memory_sort_key, reverse=True)

    # Process drops first (reverse index order to avoid shift issues)
    drop_ops = sorted(
        [op for op in ops if op.get("action") == "drop"],
        key=lambda o: o.get("index", 0),
        reverse=True
    )
    for op in drop_ops:
        npc = op.get("npc")
        idx = op.get("index")
        if not npc or npc not in memories:
            logger.warning(f"npc_memory_ops drop: NPC '{npc}' not found")
            continue
        npc_list = memories[npc]
        if idx is None or idx < 0 or idx >= len(npc_list):
            logger.warning(f"npc_memory_ops drop: index {idx} out of bounds for '{npc}' (len={len(npc_list)})")
            continue
        npc_list.pop(idx)
        if not npc_list:
            del memories[npc]

    # Process adds
    add_ops = [op for op in ops if op.get("action") == "add"]
    for op in add_ops:
        npc = op.get("npc")
        if not npc:
            continue
        try:
            impact = int(op.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        tier = _memory_tier(impact)
        entry = {
            "text": op.get("text", "")[:640],
            "quote": (op.get("quote") or "")[:120] or None,
            "date": op.get("date"),
            "impact": impact,
            "tier": tier,
            "turn_created": current_turn,
            "focus": op.get("focus")
        }
        if npc not in memories:
            memories[npc] = []
        memories[npc].append(entry)

        # Enforce per-NPC cap
        if len(memories[npc]) > NPC_MEMORY_MAX_PER_NPC:
            # Sort so we keep highest-impact; drop from the tail (lowest)
            memories[npc] = sorted(memories[npc], key=_memory_sort_key, reverse=True)[:NPC_MEMORY_MAX_PER_NPC]

        # Enforce tier limits as safety net
        tier_limit = NPC_MEMORY_TIER_LIMITS.get(tier, 12)
        tier_entries_indexed = [(i, m) for i, m in enumerate(memories[npc]) if m.get("tier") == tier]
        if len(tier_entries_indexed) > tier_limit:
            excess = len(tier_entries_indexed) - tier_limit
            # Sort by turn_created asc (oldest first), then impact asc (lowest-value first among ties)
            tier_entries_indexed.sort(key=lambda x: (x[1].get("turn_created", 0), x[1].get("impact", 0)))
            remove_indices = set(idx for idx, _ in tier_entries_indexed[:excess])
            memories[npc] = [m for i, m in enumerate(memories[npc]) if i not in remove_indices]

    # Keep lists in injection display order for index consistency on next turn
    for npc_name in memories:
        memories[npc_name] = sorted(memories[npc_name], key=_memory_sort_key, reverse=True)

    return memories


def apply_scene_state(new_scene: dict, existing_scene: dict = None) -> dict:
    """Apply scene_state with merge: new keys overwrite, absent keys retain existing values."""
    defaults = {
        "location": "",
        "npcs_present": [],
        "pcs_present": [],
        "active_tensions": [],
        "scene_trigger": "",
        "atmosphere": "",
        "details": [],
        "pending_actions": []
    }
    base = {k: (existing_scene or {}).get(k, default) for k, default in defaults.items()}
    for key in defaults:
        if key in new_scene:
            base[key] = new_scene[key]
    return base


def apply_character_states(existing: dict, mechanics_output: dict, current_turn: int) -> dict:
    """
    Merge Mechanics' character_states into existing state and prune stale entries.

    Each entry is stored as {"data": {structured_object}, "last_updated": <turn>}.
    Accepts both:
      - New structured format: {"name": {"type": "pc", "vitals": [...], ...}}
      - Old string format: {"name": "state string"} → wrapped as {"data": {"summary": str}, ...}
    Entries not updated in CHARACTER_STATE_TTL turns are pruned.
    """
    # Merge new entries from Mechanics
    for name, state_val in mechanics_output.items():
        if isinstance(state_val, dict):
            existing[name] = {"data": state_val, "last_updated": current_turn}
        else:
            # Old string format → wrap into structured format
            existing[name] = {"data": {"summary": str(state_val)}, "last_updated": current_turn}

    # Prune stale entries
    stale = [name for name, entry in existing.items()
             if not isinstance(entry, dict) or current_turn - entry.get("last_updated", 0) > CHARACTER_STATE_TTL]
    for name in stale:
        del existing[name]

    return existing


# ============================================================
# Injection Builders (format state for model consumption)
# ============================================================

def scope_hud_funds(hud_state: dict, scene_state: dict, character_states: dict) -> dict:
    """Return hud_state with funds filtered to scene-present characters.
    Non-character keys (ship, party) pass through. String funds unchanged."""
    funds = hud_state.get("funds")
    pcs_present = scene_state.get("pcs_present")
    if not isinstance(funds, dict) or not pcs_present:
        return hud_state
    present = set(pcs_present) | set(scene_state.get("npcs_present", []))
    all_chars = set(character_states.keys())
    return {**hud_state, "funds": {
        k: v for k, v in funds.items() if k in present or k not in all_chars
    }}


def derive_funds_from_ship_credits(hud_state: dict, game_state: dict) -> dict:
    """If game_state has ship.credits, derive hud_state.funds from it (single source of truth).
    Returns hud_state (possibly with replaced funds). Non-ship game systems are unaffected."""
    ship_credits = (game_state or {}).get("ship", {}).get("credits")
    if not isinstance(ship_credits, dict):
        return hud_state
    derived = {
        account: f"{amount:,} cr" if isinstance(amount, (int, float)) else str(amount)
        for account, amount in ship_credits.items()
    }
    return {**hud_state, "funds": derived}


def build_hud_state_injection(hud_state: dict, scene_state: dict, character_states: dict, game_state: dict = None) -> str:
    """Build [HUD STATE] injection with scene-scoped funds."""
    if not hud_state:
        return ""
    hud_state = derive_funds_from_ship_credits(hud_state, game_state)
    scoped = scope_hud_funds(hud_state, scene_state, character_states)
    lines = ["[HUD STATE]"]
    for key, label in [("date", "Date"), ("time", "Time"), ("location", "Location")]:
        if scoped.get(key) is not None:
            lines.append(f"{label}: {scoped[key]}")
    funds = scoped.get("funds")
    if funds is not None:
        if isinstance(funds, dict):
            lines.append(f"Funds: {', '.join(f'{k}: {v}' for k, v in funds.items())}")
        else:
            lines.append(f"Funds: {funds}")
    trackables = scoped.get("trackables")
    if trackables and isinstance(trackables, dict):
        for k, v in trackables.items():
            lines.append(f"{k}: {v}")
    lines.append("[/HUD STATE]")
    return "\n".join(lines)


def build_callback_injection(ledger: dict, turn_counter: int = 0) -> str:
    """Build human-readable callback ledger injection for Events."""
    open_list = ledger.get("open", [])
    resolved_list = ledger.get("recently_resolved", [])
    if not open_list and not resolved_list:
        return ""

    lines = ["[CALLBACK LEDGER]"]

    if open_list:
        lines.append("OPEN:")
        for cb in open_list:
            npc = cb.get("source_npc") or "null"
            line = f"#{cb['id']} (turn {cb['created_turn']}, {npc}): \"{cb['original_text']}\""
            resolutions = cb.get("resolutions")
            if resolutions:
                line += f" [resolves if: {'; '.join(resolutions)}]"
            if turn_counter and turn_counter - cb.get("created_turn", turn_counter) >= 40:
                age = turn_counter - cb["created_turn"]
                line += f" \u26a0 open {age} turns \u2014 consider resolving or folding into the narrative"
            lines.append(line)
    else:
        lines.append("OPEN: (none)")

    if resolved_list:
        lines.append("")
        lines.append("RECENTLY RESOLVED:")
        for cb in resolved_list:
            npc = cb.get("source_npc") or "null"
            created = cb.get("created_turn", "?")
            resolved = cb.get("resolved_turn", "?")
            lines.append(f"#{cb['id']} (turn {created}\u2192{resolved}, {npc}): \"{cb['original_text']}\" \u2192 \"{cb.get('resolution_text', '')}\"")

    lines.append("[/CALLBACK LEDGER]")
    return "\n".join(lines)


def build_npc_memories_injection(memories: dict, scene_state: dict) -> str:
    """Build NPC memories injection, scoped to NPCs present in the scene."""
    npcs_present = scene_state.get("npcs_present", [])
    if not npcs_present or not memories:
        return ""

    blocks = []
    for npc in npcs_present:
        npc_mems = memories.get(npc)
        if not npc_mems:
            continue
        # Sort by impact descending, then turn_created descending
        sorted_mems = sorted(npc_mems, key=lambda m: (m.get("impact", 0), m.get("turn_created", 0)), reverse=True)
        lines = [f"[NPC MEMORIES: {npc}]"]
        for idx, m in enumerate(sorted_mems):
            stars = "\u2605" * max(1, m.get("impact", 1))
            date_str = m.get("date") or "?"
            entry = f"[{idx}] [{date_str}, {stars}] {m['text']}"
            if m.get("quote"):
                entry += f" | \"{m['quote']}\""
            lines.append(entry)
        lines.append(f"[/NPC MEMORIES]")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_scene_state_injection(scene: dict) -> str:
    """Build human-readable scene state injection for Events."""
    if not scene:
        return ""

    lines = ["[SCENE STATE]"]

    field_labels = [
        ("location", "Location"),
        ("npcs_present", "NPCs Present"),
        ("pcs_present", "PCs Present"),
        ("scene_trigger", "Scene Trigger"),
        ("active_tensions", "Active Tensions"),
        ("atmosphere", "Atmosphere"),
        ("details", "Details"),
        ("pending_actions", "Pending Actions"),
    ]
    for key, label in field_labels:
        val = scene.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            if val:
                lines.append(f"{label}: {', '.join(str(v) for v in val)}")
            else:
                lines.append(f"{label}: (none)")
        else:
            if val:
                lines.append(f"{label}: {val}")

    lines.append("[/SCENE STATE]")
    return "\n".join(lines)


def _format_character_data(data) -> str:
    """Format a character's data dict into a readable one-liner for model injection."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return str(data)
    parts = []
    # Vitals
    for v in data.get("vitals", []):
        if "current" in v and "max" in v:
            parts.append(f"{v['label']}: {v['current']}/{v['max']}")
        elif "value" in v:
            parts.append(f"{v['label']}: {v['value']}")
    # Resources
    for r in data.get("resources", []):
        if "current" in r and "max" in r:
            parts.append(f"{r['label']}: {r['current']}/{r['max']}")
    # Conditions
    conds = data.get("conditions", [])
    if conds:
        parts.append(f"Conditions: {', '.join(conds)}")
    # Summary
    if data.get("summary"):
        parts.append(data["summary"])
    return " | ".join(parts) if parts else json.dumps(data)


def build_character_states_injection(character_states: dict) -> str:
    """Build human-readable character states injection for Events.

    Entries are stored as {"name": {"data": {...}, "last_updated": N}}.
    Falls back gracefully for old formats.
    """
    if not character_states:
        return ""
    lines = ["[CHARACTER STATES]"]
    for name, entry in character_states.items():
        if isinstance(entry, dict):
            data = entry.get("data") or entry.get("state", "")
            lines.append(f"{name}: {_format_character_data(data)}")
        else:
            lines.append(f"{name}: {entry}")
    lines.append("[/CHARACTER STATES]")
    return "\n".join(lines)


def run_pipeline_stage(
    provider: OpenAIProvider,
    client,
    stage_config: StageConfig,
    messages: list[dict],
    username: str,
    project: str,
    chat_name: str,
    streaming: bool = False
) -> PipelineStageResult:
    """
    Run a single pipeline stage (non-streaming for Events/Mechanics, streaming for Narration).

    For non-streaming stages, makes a single API call and returns the full response.
    For streaming (Narration), this is NOT used - Narration streams directly in the event generator.

    Includes retry-once logic on API errors.
    """
    request_params = provider.build_pipeline_request(
        messages=messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name=stage_config.name,
        reasoning_effort=stage_config.reasoning_effort,
        service_tier=stage_config.service_tier,
        json_mode=stage_config.json_mode
    )

    # Try up to 2 times (initial + 1 retry)
    last_error = None
    for attempt in range(2):
        try:
            usage = provider.send_request_non_streaming(client, request_params)

            content = usage.get('content') or ''
            reasoning = usage.get('reasoning')
            actual_tier = stage_config.service_tier
            if actual_tier == "auto":
                actual_tier = "standard"

            parsed_json = None
            if stage_config.json_mode:
                parsed_json = _parse_stage_json(content, stage_config.name)

            return PipelineStageResult(
                stage=stage_config.name,
                content=content,
                parsed_json=parsed_json,
                usage=usage,
                service_tier=actual_tier
            )

        except Exception as e:
            last_error = e
            if attempt == 0:
                logger.warning(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, retrying...")
                continue
            else:
                logger.error(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, giving up")
                raise

    raise last_error  # Should never reach here, but just in case


def run_pipeline(
    provider: OpenAIProvider,
    client,
    username: str,
    project: str,
    chat_name: str,
    branch_path: list[dict],
    agent_instructions: dict[str, str],
    agent_files: dict[str, str],
    pipeline_state: Optional[dict],
    game_system: str = "dnd5e"
) -> Iterator[tuple[str, dict]]:
    """
    Run the full pipeline, yielding SSE-ready events as (event_type, data) tuples.

    Events yielded:
    - ("pipeline_stage", {"stage": "events", "status": "thinking"})
    - ("pipeline_stage", {"stage": "events", "status": "complete"})
    - ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
    - ... etc
    - ("content", {"delta": "..."})  -- for streaming Narration or short-circuit content
    - ("pipeline_done", {PipelineResult fields})

    This is a generator that the event_generator in send_message_stream iterates over.
    """

    # Resolve game system contracts and state functions
    from game_systems import get_game_system
    gs = get_game_system(game_system)

    # Deep-copy BEFORE migration to avoid mutating the caller's data dict —
    # migrate uses setdefault which would modify the original, and if the pipeline
    # fails mid-way we don't want half-applied ops persisted on save
    pipeline_state = migrate_pipeline_state(copy.deepcopy(pipeline_state))

    # Snapshot state before Events sees it (for debug transcript)
    # turn_counter is incremented AFTER we know the route — OOC turns don't advance it
    injected_state_snapshot = json.dumps(pipeline_state, indent=2)

    # ---- STAGE 1: Events ----
    yield ("pipeline_stage", {"stage": "events", "status": "thinking"})

    events_system = build_agent_system_prompt(gs["events_contract"], agent_instructions["events"], agent_files["events"])
    recent_events_pairs = get_context_pairs(branch_path, EVENTS_THRESHOLD_PAIRS, EVENTS_TARGET_PAIRS)
    user_msg = {"role": "user", "content": build_message_content(branch_path[-1])}
    events_messages = build_events_messages(events_system, recent_events_pairs, user_msg, pipeline_state, game_system=gs)

    events_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["events"],
        events_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "events", "status": "complete"})

    events_data = events_result.parsed_json
    events_route = events_data.get("route", "mechanics")

    # Increment turn counter only for in-character turns — OOC shouldn't age TTLs
    if events_route != "output":
        pipeline_state["turn_counter"] += 1
    current_turn = pipeline_state["turn_counter"]

    # Extract and apply state from Events output
    if events_data.get("pacing"):
        pipeline_state["pacing"] = {**pipeline_state.get("pacing", {}), **events_data["pacing"]}
    pipeline_state["callback_ledger"] = apply_callback_ops(
        pipeline_state["callback_ledger"],
        events_data.get("callback_ops"),
        current_turn
    )
    pipeline_state["npc_memories"] = apply_npc_memory_ops(
        pipeline_state["npc_memories"],
        events_data.get("npc_memory_ops"),
        current_turn
    )
    if events_data.get("scene_state"):
        pipeline_state["scene_state"] = apply_scene_state(
            events_data["scene_state"],
            existing_scene=pipeline_state.get("scene_state")
        )

    # Apply game-specific state ops (e.g. investigator_ops for CoC 7E)
    if gs.get("apply_game_state"):
        if "game_state" not in pipeline_state:
            pipeline_state["game_state"] = gs["init_game_state"]()
        gs["apply_game_state"](pipeline_state["game_state"], events_data, current_turn)

    # Derive hud_state.funds from ship.credits (single source of truth), then scene-scope
    if "hud_state" in events_data:
        events_data["hud_state"] = derive_funds_from_ship_credits(
            events_data["hud_state"],
            pipeline_state.get("game_state"))
        events_data["hud_state"] = scope_hud_funds(
            events_data["hud_state"],
            pipeline_state.get("scene_state", {}),
            pipeline_state.get("character_states", {}))
        pipeline_state["hud_state"] = events_data["hud_state"]

    # Persist combat state (initiative tracker) from Events
    if "combat" in events_data:
        pipeline_state["combat"] = events_data["combat"]

    new_pipeline_state = pipeline_state

    # Collect stage results for aggregation
    stage_results = [events_result]
    reasoning_summaries = []
    if events_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Events] {events_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Events → Output ----
    if events_route == "output":
        final_content = events_data.get("content", "")
        # Send content as a single chunk (OOC responses are short)
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=None,
            stages_run=["events"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="standard",
            injected_state=injected_state_snapshot,
            stage_usage=_build_stage_usage(stage_results, provider)
        ))
        return

    # ---- STAGE 2: Mechanics ----
    yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})

    mechanics_system = build_agent_system_prompt(gs["mechanics_contract"], agent_instructions["mechanics"], agent_files["mechanics"])
    mechanics_messages = build_mechanics_messages(mechanics_system, events_data)

    mechanics_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["mechanics"],
        mechanics_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

    mechanics_data = mechanics_result.parsed_json
    mechanics_route = mechanics_data.get("route", "narration")
    new_pipeline_state["character_states"] = apply_character_states(
        new_pipeline_state["character_states"],
        mechanics_data.get("character_states") or {},
        current_turn
    )
    stage_results.append(mechanics_result)
    if mechanics_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Mechanics] {mechanics_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Mechanics → Output ----
    if mechanics_route == "output":
        final_content = mechanics_data.get("content", "")
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=mechanics_result.content,
            stages_run=["events", "mechanics"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="standard",
            injected_state=injected_state_snapshot,
            stage_usage=_build_stage_usage(stage_results, provider)
        ))
        return

    # ---- STAGE 3: Narration (streaming) ----
    yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})

    narration_system = build_agent_system_prompt(gs["narration_contract"], agent_instructions["narration"], agent_files["narration"])
    recent_pairs = get_context_pairs(branch_path, NARRATION_THRESHOLD_PAIRS, NARRATION_TARGET_PAIRS)
    narration_messages = build_narration_messages(narration_system, recent_pairs, mechanics_data)

    narration_params = provider.build_pipeline_request(
        messages=narration_messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name="narration",
        reasoning_effort=STAGE_CONFIGS["narration"].reasoning_effort,
        service_tier=STAGE_CONFIGS["narration"].service_tier,
        json_mode=False
    )

    # Stream Narration to the user
    narration_content = ""
    narration_usage = None
    first_content = True

    for stream_event in provider.send_request_stream(client, narration_params):
        if stream_event.event_type == 'content_delta':
            if first_content:
                yield ("pipeline_stage", {"stage": "narration", "status": "streaming"})
                first_content = False
            narration_content += stream_event.content
            yield ("content", {"delta": stream_event.content})

        elif stream_event.event_type == 'done':
            narration_usage = stream_event.usage
            narration_content = narration_content or narration_usage.get('content') or ''

    yield ("pipeline_stage", {"stage": "narration", "status": "complete"})

    # Build narration stage result
    narration_stage_result = PipelineStageResult(
        stage="narration",
        content=narration_content,
        parsed_json=None,
        usage=narration_usage or {},
        service_tier="standard"
    )
    stage_results.append(narration_stage_result)
    if narration_usage and narration_usage.get('reasoning'):
        reasoning_summaries.append(f"[Narration] {narration_usage['reasoning']}")

    aggregate = _aggregate_usage(stage_results, provider)
    yield ("pipeline_done", PipelineResult(
        final_content=narration_content,
        events_json=events_result.content,
        mechanics_json=mechanics_result.content,
        stages_run=["events", "mechanics", "narration"],
        aggregate_usage=aggregate["usage"],
        aggregate_cost=aggregate["cost"],
        pipeline_state=new_pipeline_state,
        reasoning_summaries=reasoning_summaries,
        service_tier_label="flex+standard",
        injected_state=injected_state_snapshot,
        stage_usage=_build_stage_usage(stage_results, provider)
    ))


def _build_stage_usage(stage_results: list[PipelineStageResult], provider: OpenAIProvider) -> dict:
    """Build per-stage usage dict for debug storage."""
    result = {}
    for sr in stage_results:
        u = sr.usage
        parsed = ParsedResponse(
            content="", reasoning=None,
            input_tokens=u.get('input_tokens', 0),
            cache_read_tokens=u.get('cache_read_tokens', 0),
            cache_creation_tokens=u.get('cache_creation_tokens', 0),
            output_tokens=u.get('output_tokens', 0),
            reasoning_tokens=u.get('reasoning_tokens', 0)
        )
        result[sr.stage] = {
            "input_tokens": u.get('input_tokens', 0),
            "cache_read_tokens": u.get('cache_read_tokens', 0),
            "cache_creation_tokens": u.get('cache_creation_tokens', 0),
            "output_tokens": u.get('output_tokens', 0),
            "reasoning_tokens": u.get('reasoning_tokens', 0),
            "cost": provider.calculate_cost_with_tier(parsed, sr.service_tier),
            "service_tier": sr.service_tier
        }
    return result


def _aggregate_usage(stage_results: list[PipelineStageResult], provider: OpenAIProvider) -> dict:
    """
    Aggregate token usage and cost across all pipeline stages.

    Each stage's cost is calculated with its own tier-specific pricing.
    """
    total_input = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_output = 0
    total_reasoning = 0
    total_cost = 0.0

    for result in stage_results:
        usage = result.usage
        input_tokens = usage.get('input_tokens', 0)
        cache_read = usage.get('cache_read_tokens', 0)
        cache_creation = usage.get('cache_creation_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        reasoning_tokens = usage.get('reasoning_tokens', 0)

        total_input += input_tokens
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_output += output_tokens
        total_reasoning += reasoning_tokens

        # Calculate per-stage cost with tier-specific pricing
        parsed = ParsedResponse(
            content="",
            reasoning=None,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens
        )
        stage_cost = provider.calculate_cost_with_tier(parsed, result.service_tier)
        total_cost += stage_cost

    return {
        "usage": {
            "input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_creation_tokens": total_cache_creation,
            "output_tokens": total_output,
            "reasoning_tokens": total_reasoning,
        },
        "cost": total_cost
    }


# ============================================================
# Debug Transcript Generation
# ============================================================

def _parse_reasoning_by_stage(reasoning: str) -> dict:
    """Split a joined '[Events] ...\n[Mechanics] ...' reasoning string into per-stage dict."""
    result = {"events": "", "mechanics": "", "narration": ""}
    if not reasoning:
        return result
    current_stage = None
    current_lines = []
    for line in reasoning.split("\n"):
        stripped = line.strip()
        matched = False
        for stage in ("Events", "Mechanics", "Narration"):
            prefix = f"[{stage}]"
            if stripped.startswith(prefix):
                # Save previous stage
                if current_stage:
                    result[current_stage] = "\n".join(current_lines).strip()
                current_stage = stage.lower()
                current_lines = [stripped[len(prefix):].strip()]
                matched = True
                break
        if not matched and current_stage:
            current_lines.append(line)
    if current_stage:
        result[current_stage] = "\n".join(current_lines).strip()
    return result


def _pretty_json(raw: str) -> str:
    """Pretty-print a JSON string with 2-space indent. Falls back to raw on error."""
    if not raw:
        return "(none)"
    try:
        return json.dumps(json.loads(raw), indent=2)
    except (json.JSONDecodeError, TypeError):
        return f"[PARSE ERROR] {raw}"


def _compute_state_delta(prev: dict, curr: dict) -> str:
    """Compute a human-readable delta between two pipeline states."""
    parts = []

    # Turn counter
    prev_turn = prev.get("turn_counter", 0)
    curr_turn = curr.get("turn_counter", 0)
    if curr_turn != prev_turn:
        parts.append(f"turn_counter: {prev_turn} → {curr_turn}")

    # Pacing — show if changed
    prev_pacing = prev.get("pacing", {})
    curr_pacing = curr.get("pacing", {})
    if curr_pacing != prev_pacing:
        changed = {k: v for k, v in curr_pacing.items() if prev_pacing.get(k) != v}
        if changed:
            parts.append(f"pacing: {json.dumps(changed)}")

    # Callback ledger
    prev_ledger = prev.get("callback_ledger", {})
    curr_ledger = curr.get("callback_ledger", {})
    prev_open_ids = {cb["id"] for cb in prev_ledger.get("open", []) if "id" in cb}
    curr_open_ids = {cb["id"] for cb in curr_ledger.get("open", []) if "id" in cb}
    added_ids = curr_open_ids - prev_open_ids
    resolved_ids = prev_open_ids - curr_open_ids
    if added_ids:
        added_cbs = [cb for cb in curr_ledger.get("open", []) if cb.get("id") in added_ids]
        for cb in added_cbs:
            parts.append(f"callback +#{cb.get('id')}: \"{cb.get('original_text', '')[:80]}\"")
    if resolved_ids:
        resolved_cbs = [cb for cb in curr_ledger.get("recently_resolved", []) if cb.get("id") in resolved_ids]
        for cb in resolved_cbs:
            parts.append(f"callback resolved #{cb.get('id')}: \"{cb.get('resolution_text', '')[:80]}\"")

    # NPC memories
    prev_mems = prev.get("npc_memories", {})
    curr_mems = curr.get("npc_memories", {})
    all_npcs = set(list(prev_mems.keys()) + list(curr_mems.keys()))
    for npc in sorted(all_npcs):
        prev_list = prev_mems.get(npc, [])
        curr_list = curr_mems.get(npc, [])
        prev_texts = {m.get("text") for m in prev_list}
        curr_texts = {m.get("text") for m in curr_list}
        for text in curr_texts - prev_texts:
            mem = next((m for m in curr_list if m.get("text") == text), {})
            parts.append(f"memory +{npc}: [{mem.get('impact', '?')}★] \"{text[:60]}\"")
        for text in prev_texts - curr_texts:
            parts.append(f"memory -{npc}: \"{text[:60]}\"")
    # NPC removed entirely
    for npc in sorted(set(prev_mems.keys()) - set(curr_mems.keys())):
        if npc not in all_npcs:  # already handled above
            parts.append(f"memory -{npc}: (all removed)")

    # Character states (entries are {"data": {...}, "last_updated": N} or flat strings)
    prev_cs = prev.get("character_states", {})
    curr_cs = curr.get("character_states", {})
    def _cs_state(entry):
        if isinstance(entry, dict):
            return entry.get("data", entry.get("state", ""))
        return entry
    all_chars = set(list(prev_cs.keys()) + list(curr_cs.keys()))
    for char in sorted(all_chars):
        prev_entry = prev_cs.get(char)
        curr_entry = curr_cs.get(char)
        prev_state = _cs_state(prev_entry) if prev_entry else None
        curr_state = _cs_state(curr_entry) if curr_entry else None
        if curr_state != prev_state:
            if prev_state is None:
                parts.append(f"character_state +{char}: {curr_state}")
            elif curr_state is None:
                parts.append(f"character_state -{char}")
            else:
                parts.append(f"character_state {char}: {prev_state} → {curr_state}")

    # Scene state
    prev_scene = prev.get("scene_state", {})
    curr_scene = curr.get("scene_state", {})
    if curr_scene != prev_scene:
        scene_changes = []
        for key in ["location", "npcs_present", "active_tensions", "scene_trigger", "atmosphere", "details", "pending_actions"]:
            pv = prev_scene.get(key)
            cv = curr_scene.get(key)
            if pv != cv:
                if isinstance(cv, list):
                    scene_changes.append(f"  {key}: {', '.join(str(v) for v in cv) if cv else '(none)'}")
                else:
                    scene_changes.append(f"  {key}: {cv}")
        if scene_changes:
            parts.append("scene_state:\n" + "\n".join(scene_changes))

    # Game-specific state (e.g. CoC investigators)
    prev_gs = prev.get("game_state", {})
    curr_gs = curr.get("game_state", {})
    if curr_gs != prev_gs:
        # Show per-investigator deltas if investigators dict exists
        prev_inv = prev_gs.get("investigators", {})
        curr_inv = curr_gs.get("investigators", {})
        if prev_inv != curr_inv:
            all_names = sorted(set(list(prev_inv.keys()) + list(curr_inv.keys())))
            for name in all_names:
                pi = prev_inv.get(name)
                ci = curr_inv.get(name)
                if pi is None and ci is not None:
                    san = ci.get("san", {})
                    parts.append(f"investigator +{name}: SAN {san.get('current', '?')}/{san.get('max', '?')}, Luck {ci.get('luck', '?')}, Mythos {ci.get('mythos', '?')}%")
                elif ci is None and pi is not None:
                    parts.append(f"investigator -{name}")
                elif pi != ci:
                    changes = []
                    # SAN
                    ps, cs = pi.get("san", {}), ci.get("san", {})
                    if ps != cs:
                        changes.append(f"SAN {ps.get('current', '?')}/{ps.get('max', '?')} → {cs.get('current', '?')}/{cs.get('max', '?')}")
                    # Luck
                    if pi.get("luck") != ci.get("luck"):
                        changes.append(f"Luck {pi.get('luck', '?')} → {ci.get('luck', '?')}")
                    # Mythos
                    if pi.get("mythos") != ci.get("mythos"):
                        changes.append(f"Mythos {pi.get('mythos', '?')}% → {ci.get('mythos', '?')}%")
                    # Bonds
                    if pi.get("bonds") != ci.get("bonds"):
                        bond_strs = [f"{b['name']}({b['value']})" for b in ci.get("bonds", [])]
                        changes.append(f"Bonds: {', '.join(bond_strs) if bond_strs else '(none)'}")
                    # Phobias/manias
                    if pi.get("phobias") != ci.get("phobias"):
                        changes.append(f"Phobias: {', '.join(ci.get('phobias', [])) or '(none)'}")
                    if pi.get("manias") != ci.get("manias"):
                        changes.append(f"Manias: {', '.join(ci.get('manias', [])) or '(none)'}")
                    # Skill marks
                    if pi.get("skill_marks") != ci.get("skill_marks"):
                        changes.append(f"Skill marks: {', '.join(ci.get('skill_marks', [])) or '(none)'}")
                    if changes:
                        parts.append(f"investigator {name}: {', '.join(changes)}")
        elif prev_gs != curr_gs:
            # Non-investigator game_state change — show raw diff
            parts.append(f"game_state: {json.dumps(curr_gs)}")

    return "\n".join(parts)


def _format_stage_usage(stage_name: str, usage: dict) -> str:
    """Format a single stage's usage as a compact one-liner."""
    inp = usage.get("input_tokens", 0)
    cache = usage.get("cache_read_tokens", 0)
    out = usage.get("output_tokens", 0)
    reasoning = usage.get("reasoning_tokens", 0)
    cost = usage.get("cost", 0)
    tier = usage.get("service_tier", "?")
    return f"{stage_name}: Input: {inp:,}  Cache: {cache:,}  Output: {out:,}  Reasoning: {reasoning:,}  Cost: ${cost:.4f}  Tier: {tier}"


def generate_debug_transcript(chat_data: dict, chat_path: str, chat_name: str) -> None:
    """
    Generate a debug transcript file for a pipeline chat.

    Walks the active branch (current_leaf_id → root) and expands pipeline
    assistant messages to show per-stage JSON and reasoning.
    """
    from datetime import datetime, timezone

    debug_path = chat_path.replace(".json", "_debug.txt")

    messages = chat_data.get("messages", [])
    leaf_id = chat_data.get("current_leaf_id")
    if not messages or not leaf_id:
        return

    # Build index and trace active branch (root → leaf)
    index = {m["id"]: m for m in messages if m.get("id")}
    path = []
    current = leaf_id
    while current:
        if current not in index:
            break
        path.append(index[current])
        current = index[current].get("parent_id")
    path.reverse()

    lines = []
    lines.append("=" * 80)
    lines.append(f"PIPELINE DEBUG TRANSCRIPT: {chat_name}")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 80)
    lines.append("")

    prev_state = None  # Track previous state for delta computation
    latest_state_raw = None  # Track the latest full state for the end block

    for msg in path:
        role = msg.get("role", "")
        if role == "system":
            continue

        timestamp = msg.get("timestamp", "")
        content = msg.get("content", "")

        if role == "user":
            lines.append("+" * 80)
            lines.append("")
            lines.append(f"[USER] {timestamp}")
            lines.append(content)
            lines.append("")

        elif role == "assistant":
            events_raw = msg.get("events_stage")
            mechanics_raw = msg.get("mechanics_stage")

            if events_raw or mechanics_raw:
                # Pipeline message — expand stages
                cost = msg.get("cost", "")
                lines.append(f"[ASSISTANT] {timestamp}  {cost}")

                # Show pipeline state delta (what changed since last turn)
                injected_state_raw = msg.get("pipeline_state_injected")
                if injected_state_raw:
                    latest_state_raw = injected_state_raw
                    try:
                        current_state = json.loads(injected_state_raw)
                    except (json.JSONDecodeError, TypeError):
                        current_state = None

                    if current_state is not None:
                        if prev_state is None:
                            lines.append("--- PIPELINE STATE (initial) ---")
                            lines.append(json.dumps(current_state, indent=2))
                        else:
                            delta = _compute_state_delta(prev_state, current_state)
                            if delta:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append(delta)
                            else:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append("(no changes)")
                        lines.append("")
                        prev_state = current_state

                reasoning_parts = _parse_reasoning_by_stage(msg.get("reasoning", ""))
                stage_usage = msg.get("pipeline_stage_usage", {})

                if events_raw:
                    lines.append("--- EVENTS STAGE ---")
                    lines.append(_pretty_json(events_raw))
                    lines.append("")

                    # Show extracted state ops from Events output
                    try:
                        events_parsed = json.loads(events_raw)
                        ops_parts = []
                        cb_ops = events_parsed.get("callback_ops")
                        if cb_ops:
                            ops_parts.append(f"callback_ops: {json.dumps(cb_ops, indent=2)}")
                        mem_ops = events_parsed.get("npc_memory_ops")
                        if mem_ops:
                            ops_parts.append(f"npc_memory_ops: {json.dumps(mem_ops, indent=2)}")
                        scene = events_parsed.get("scene_state")
                        if scene:
                            ops_parts.append(f"scene_state: {json.dumps(scene, indent=2)}")
                        if ops_parts:
                            lines.append("--- STATE OPS EXTRACTED ---")
                            lines.append("\n".join(ops_parts))
                            lines.append("")
                    except (json.JSONDecodeError, TypeError):
                        pass

                    lines.append("--- EVENTS REASONING ---")
                    lines.append(reasoning_parts["events"] or "(none)")
                    lines.append("")
                    if "events" in stage_usage:
                        lines.append(_format_stage_usage("Events", stage_usage["events"]))
                        lines.append("")

                if mechanics_raw:
                    lines.append("--- MECHANICS STAGE ---")
                    lines.append(_pretty_json(mechanics_raw))
                    lines.append("")
                    lines.append("--- MECHANICS REASONING ---")
                    lines.append(reasoning_parts["mechanics"] or "(none)")
                    lines.append("")
                    if "mechanics" in stage_usage:
                        lines.append(_format_stage_usage("Mechanics", stage_usage["mechanics"]))
                        lines.append("")

                narration_reasoning = reasoning_parts["narration"]
                if narration_reasoning:
                    lines.append("--- NARRATION REASONING ---")
                    lines.append(narration_reasoning)
                    lines.append("")
                if "narration" in stage_usage:
                    lines.append(_format_stage_usage("Narration", stage_usage["narration"]))
                    lines.append("")

                # Total usage across all stages for this turn
                if stage_usage:
                    total_cost = sum(s.get("cost", 0) for s in stage_usage.values())
                    total_input = sum(s.get("input_tokens", 0) for s in stage_usage.values())
                    total_output = sum(s.get("output_tokens", 0) for s in stage_usage.values())
                    total_reasoning = sum(s.get("reasoning_tokens", 0) for s in stage_usage.values())
                    lines.append(f"--- TURN TOTAL: Input: {total_input:,}  Output: {total_output:,}  Reasoning: {total_reasoning:,}  Cost: ${total_cost:.4f} ---")
                    lines.append("")

                lines.append("--- FINAL OUTPUT ---")
                lines.append(content)
                lines.append("")
            elif msg.get("state_block_raw") is not None or msg.get("state_tool_input") is not None or msg.get("pipeline_state_injected"):
                # Single-agent stateful message
                cost = msg.get("cost", "")
                lines.append(f"[ASSISTANT] {timestamp}  {cost}")

                # Show pipeline state delta (what changed since last turn)
                injected_state_raw = msg.get("pipeline_state_injected")
                if injected_state_raw:
                    latest_state_raw = injected_state_raw
                    try:
                        current_state = json.loads(injected_state_raw)
                    except (json.JSONDecodeError, TypeError):
                        current_state = None

                    if current_state is not None:
                        if prev_state is None:
                            lines.append("--- PIPELINE STATE (initial) ---")
                            lines.append(json.dumps(current_state, indent=2))
                        else:
                            delta = _compute_state_delta(prev_state, current_state)
                            if delta:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append(delta)
                            else:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append("(no changes)")
                        lines.append("")
                        prev_state = current_state

                # Show raw state block from model output (legacy text-based)
                state_block_raw = msg.get("state_block_raw")
                if state_block_raw:
                    lines.append("--- RAW STATE BLOCK ---")
                    lines.append(state_block_raw.strip())
                    lines.append("")

                # Show tool-based state input (new forced tool_use)
                state_tool = msg.get("state_tool_input")
                if state_tool:
                    retried = msg.get("state_tool_retried", False)
                    header = "--- STATE TOOL INPUT (retried) ---" if retried else "--- STATE TOOL INPUT ---"
                    lines.append(header)
                    lines.append(json.dumps(state_tool, indent=2))
                    lines.append("")

                # Show delta between injected state and state after ops applied
                state_after_raw = msg.get("pipeline_state_after")
                if state_after_raw and injected_state_raw:
                    try:
                        state_after = json.loads(state_after_raw)
                        state_before = json.loads(injected_state_raw)
                        applied_delta = _compute_state_delta(state_before, state_after)
                        if applied_delta:
                            lines.append("--- STATE CHANGES APPLIED ---")
                            lines.append(applied_delta)
                        else:
                            lines.append("--- STATE CHANGES APPLIED ---")
                            lines.append("(no changes)")
                        lines.append("")
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Show parsed ops
                state_ops = msg.get("state_ops_parsed")
                if state_ops:
                    ops_parts = []
                    if state_ops.get("pacing"):
                        ops_parts.append(f"pacing: {json.dumps(state_ops['pacing'], indent=2)}")
                    if state_ops.get("callback_ops"):
                        ops_parts.append(f"callback_ops: {json.dumps(state_ops['callback_ops'], indent=2)}")
                    if state_ops.get("npc_memory_ops"):
                        ops_parts.append(f"npc_memory_ops: {json.dumps(state_ops['npc_memory_ops'], indent=2)}")
                    if state_ops.get("scene_state"):
                        ops_parts.append(f"scene_state: {json.dumps(state_ops['scene_state'], indent=2)}")
                    if state_ops.get("character_states"):
                        ops_parts.append(f"character_states: {json.dumps(state_ops['character_states'], indent=2)}")
                    if ops_parts:
                        lines.append("--- STATE OPS PARSED ---")
                        lines.append("\n".join(ops_parts))
                        lines.append("")

                lines.append("--- OUTPUT ---")
                lines.append(content)
                lines.append("")
            else:
                # Non-pipeline, non-stateful assistant message
                cost = msg.get("cost", "")
                lines.append(f"[ASSISTANT] {timestamp}  {cost}")
                lines.append(content)
                lines.append("")

    # Append full final state at the end of the file for easy reference
    final_state = chat_data.get("pipeline_state")
    if final_state:
        lines.append("=" * 80)
        lines.append("FULL PIPELINE STATE (after last turn)")
        lines.append("=" * 80)
        lines.append(json.dumps(final_state, indent=2))
        lines.append("")

    with open(debug_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============================================================
# Single-Agent Stateful Persistence (Claude project chats)
# ============================================================

STATE_BLOCK_START = "\n[STATE UPDATES]\n"
STATE_BLOCK_END = "\n[/STATE UPDATES]"

# Threshold/target for single-agent context trimming (same as pipeline)
SINGLE_AGENT_THRESHOLD_PAIRS = 40
SINGLE_AGENT_TARGET_PAIRS = 20


class StateInterceptor:
    """Buffers streaming content and intercepts the [STATE UPDATES] block."""

    def __init__(self):
        self.buffer = ""
        self.state_started = False
        self.state_buffer = ""
        self.narrative_complete = ""

    def feed(self, delta: str) -> str:
        """Feed a content delta. Returns text safe to yield to user."""
        if self.state_started:
            self.state_buffer += delta
            return ""

        self.buffer += delta

        if STATE_BLOCK_START in self.buffer:
            parts = self.buffer.split(STATE_BLOCK_START, 1)
            self.state_started = True
            safe = parts[0]
            self.state_buffer = parts[1]
            self.narrative_complete += safe
            self.buffer = ""
            return safe

        # Hold back potential partial delimiter
        safe_len = max(0, len(self.buffer) - len(STATE_BLOCK_START))
        safe = self.buffer[:safe_len]
        self.buffer = self.buffer[safe_len:]
        self.narrative_complete += safe
        return safe

    def finalize(self) -> tuple:
        """Call after stream ends. Returns (remaining_narrative, state_block_text_or_None)."""
        if self.state_started:
            state_text = self.state_buffer
            if STATE_BLOCK_END in state_text:
                state_text = state_text[:state_text.index(STATE_BLOCK_END)]
            return self.buffer, state_text
        else:
            return self.buffer, None


def _parse_pacing_section(lines: list) -> dict:
    """Parse PACING section lines into a dict."""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            # Try to parse responses as int
            if key == "responses":
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    pass
            result[key] = value
    return result


def _parse_callbacks_section(lines: list) -> list:
    """Parse CALLBACKS section lines into ops list."""
    import re
    ops = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("+"):
            # Add: + "description" | source: NPC | resolutions: "a", "b"
            text_match = re.search(r'"([^"]*)"', line)
            text = text_match.group(1) if text_match else line[1:].strip()
            source = None
            source_match = re.search(r'\|\s*source:\s*([^|]+)', line)
            if source_match:
                source = source_match.group(1).strip()
                if source.lower() == "null":
                    source = None
            resolutions = None
            res_match = re.search(r'\|\s*resolutions:\s*(.+)$', line)
            if res_match:
                raw = res_match.group(1).strip()
                resolutions = [r.strip().strip('"').strip("'")[:200] for r in raw.split(",")][:3]
                resolutions = [r for r in resolutions if r]
            op = {
                "action": "add",
                "original_text": text[:800],
                "source_npc": source
            }
            if resolutions:
                op["resolutions"] = resolutions
            ops.append(op)
        elif line.upper().startswith("RESOLVE"):
            # RESOLVE #N: "text"
            id_match = re.search(r'#(\d+)', line)
            text_match = re.search(r'"([^"]*)"', line)
            if id_match:
                ops.append({
                    "action": "resolve",
                    "id": int(id_match.group(1)),
                    "resolution_text": text_match.group(1) if text_match else ""
                })
    return ops


def _parse_memories_section(lines: list) -> list:
    """Parse MEMORIES section lines into ops list."""
    import re
    ops = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("+"):
            # + NPC [impact] "text" | "quote" | date
            rest = line[1:].strip()
            # Extract NPC name (everything before first [)
            bracket_idx = rest.find("[")
            if bracket_idx == -1:
                continue
            npc = rest[:bracket_idx].strip()
            # Extract impact
            impact_match = re.search(r'\[(\d+)\]', rest)
            impact = int(impact_match.group(1)) if impact_match else 1
            # Extract quoted strings
            quotes = re.findall(r'"([^"]*)"', rest)
            text = quotes[0] if len(quotes) > 0 else ""
            quote = quotes[1] if len(quotes) > 1 else None
            # Date is after last | (at least 2 pipes for text|quote|date, or 1 pipe for text|date)
            parts = rest.split("|")
            date = None
            if len(parts) >= 2:
                candidate = parts[-1].strip()
                # If the last segment looks like a quoted string, it's a quote not a date
                if candidate and not candidate.startswith('"'):
                    date = candidate
            ops.append({
                "action": "add",
                "npc": npc,
                "text": text[:640],
                "quote": quote[:120] if quote else None,
                "date": date,
                "impact": impact
            })
        elif line.startswith("-"):
            # - NPC [index]
            rest = line[1:].strip()
            bracket_match = re.search(r'\[(\d+)\]', rest)
            if bracket_match:
                idx = int(bracket_match.group(1))
                npc = rest[:rest.find("[")].strip()
                ops.append({
                    "action": "drop",
                    "npc": npc,
                    "index": idx
                })
    return ops


def _parse_scene_section(lines: list) -> dict:
    """Parse SCENE section lines into a scene_state dict."""
    result = {}
    list_keys = {"npcs_present", "pcs_present", "tensions", "details", "pending"}
    # Map short keys to full scene_state keys
    key_map = {
        "npcs_present": "npcs_present",
        "pcs_present": "pcs_present",
        "tensions": "active_tensions",
        "trigger": "scene_trigger",
        "pending": "pending_actions",
        "location": "location",
        "atmosphere": "atmosphere",
        "details": "details",
    }
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            mapped_key = key_map.get(key, key)
            if key in list_keys:
                result[mapped_key] = [v.strip() for v in value.split(",") if v.strip()] if value and value != "(none)" else []
            else:
                result[mapped_key] = value
    return result


def _parse_characters_section(lines: list) -> dict:
    """Parse CHARACTERS section lines into a flat name→state dict."""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, state = line.partition(":")
            name = name.strip()
            state = state.strip()
            if name and state:
                result[name] = state
    return result


def parse_state_updates_block(text: str, current_turn: int) -> dict:
    """
    Parse the [STATE UPDATES] block text into ops compatible with existing apply_* functions.

    Returns dict with keys: pacing, callback_ops, npc_memory_ops, scene_state, character_states.
    Each value is None if that section was not present.
    """
    result = {
        "pacing": None,
        "callback_ops": None,
        "npc_memory_ops": None,
        "scene_state": None,
        "character_states": None,
    }

    # Split text into sections by header lines
    current_section = None
    section_lines = {}

    # Map alternate section header names to canonical names
    _section_aliases = {
        "PACING": "PACING", "CALLBACKS": "CALLBACKS", "MEMORIES": "MEMORIES",
        "SCENE": "SCENE", "CHARACTERS": "CHARACTERS",
        "SCENE STATE": "SCENE", "CHARACTER STATES": "CHARACTERS",
        "NPC MEMORIES": "MEMORIES",
    }

    for line in text.split("\n"):
        stripped = line.strip().upper().rstrip(":")
        if stripped in _section_aliases:
            current_section = _section_aliases[stripped]
            section_lines[current_section] = []
        elif current_section is not None:
            section_lines[current_section].append(line)

    if "PACING" in section_lines:
        result["pacing"] = _parse_pacing_section(section_lines["PACING"])
    if "CALLBACKS" in section_lines:
        result["callback_ops"] = _parse_callbacks_section(section_lines["CALLBACKS"])
    if "MEMORIES" in section_lines:
        result["npc_memory_ops"] = _parse_memories_section(section_lines["MEMORIES"])
    if "SCENE" in section_lines:
        result["scene_state"] = _parse_scene_section(section_lines["SCENE"])
    if "CHARACTERS" in section_lines:
        result["character_states"] = _parse_characters_section(section_lines["CHARACTERS"])

    return result


def apply_single_agent_state_updates(pipeline_state: dict, parsed: dict, current_turn: int, game_system: dict = None) -> dict:
    """Apply parsed state updates to pipeline_state using existing apply_* functions."""
    if parsed.get("pacing"):
        pipeline_state["pacing"] = {**pipeline_state.get("pacing", {}), **parsed["pacing"]}
    if parsed.get("callback_ops"):
        pipeline_state["callback_ledger"] = apply_callback_ops(
            pipeline_state["callback_ledger"],
            parsed["callback_ops"],
            current_turn
        )
    if parsed.get("npc_memory_ops"):
        pipeline_state["npc_memories"] = apply_npc_memory_ops(
            pipeline_state["npc_memories"],
            parsed["npc_memory_ops"],
            current_turn
        )
    if parsed.get("scene_state"):
        pipeline_state["scene_state"] = apply_scene_state(
            parsed["scene_state"],
            existing_scene=pipeline_state.get("scene_state")
        )
    pipeline_state["character_states"] = apply_character_states(
        pipeline_state["character_states"],
        parsed.get("character_states") or {},
        current_turn
    )
    # Apply game-specific state ops (e.g. investigator_ops for CoC 7E)
    if game_system and game_system.get("apply_game_state"):
        if "game_state" not in pipeline_state:
            pipeline_state["game_state"] = game_system["init_game_state"]()
        game_system["apply_game_state"](pipeline_state["game_state"], parsed, current_turn)
    # Persist HUD state from tool report
    if "hud_state" in parsed:
        pipeline_state["hud_state"] = parsed["hud_state"]
    # Persist combat state (initiative tracker) from tool report
    if "combat" in parsed:
        pipeline_state["combat"] = parsed["combat"]
    return pipeline_state


def build_single_agent_injections(pipeline_state: dict, game_system: dict = None) -> str:
    """Build the full injection string for a single-agent stateful user message."""
    injections = []

    # 1. Pacing state
    pacing = pipeline_state.get("pacing", {})
    if pacing:
        injections.append(f"[PIPELINE STATE]\n{json.dumps(pacing, indent=2)}\n[/PIPELINE STATE]")

    # 2. Callback ledger
    cb = build_callback_injection(
        pipeline_state.get("callback_ledger", {}),
        turn_counter=pipeline_state.get("turn_counter", 0)
    )
    if cb:
        injections.append(cb)

    # 3. NPC memories (scene-scoped)
    mem = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem:
        injections.append(mem)

    # 4. Scene state
    scene = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    if scene:
        injections.append(scene)

    # 5. Character states
    cs = build_character_states_injection(pipeline_state.get("character_states", {}))
    if cs:
        injections.append(cs)

    # 6. HUD state (with scene-scoped funds, backfilled from game_state)
    hud = build_hud_state_injection(
        pipeline_state.get("hud_state", {}),
        pipeline_state.get("scene_state", {}),
        pipeline_state.get("character_states", {}),
        game_state=pipeline_state.get("game_state"))
    if hud:
        injections.append(hud)

    # 7. Game-specific state injection (e.g. [INVESTIGATOR STATE] for CoC 7E)
    if game_system and game_system.get("build_game_injection"):
        game_injection = game_system["build_game_injection"](pipeline_state.get("game_state", {}))
        if game_injection:
            injections.append(game_injection)

    return "\n\n".join(injections) if injections else ""
