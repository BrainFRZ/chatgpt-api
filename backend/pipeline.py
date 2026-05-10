"""
Multi-agent TTRPG pipeline for GPT-5.2 project chats.

Three-stage pipeline: Events → Mechanics → Narration
Each stage has its own reasoning effort, service tier, and context window.
Only activates for GPT-5.2 project chats; Anthropic models use the existing single-agent flow.
"""

import copy
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Optional, Iterator

from providers import ParsedResponse, StreamEvent, Pricing
from providers.openai_provider import OpenAIProvider, FLEX_PRICING, STANDARD_PRICING
from combat_state import replace_combat_dict_preserving_backend_keys
from game_systems.cpred_identity import (
    build_relationship_context,
    collect_relationship_present_names,
    state_op_has_subject_kind,
    state_op_subject_name,
)

logger = logging.getLogger(__name__)

# Contracts and state tools are now in game_systems/ modules.
# Imported here for backward compatibility with main.py imports.
from game_systems.dnd5e import (
    SINGLE_AGENT_STATE_CONTRACT,
    STATE_REPORT_TOOL,
)
from game_systems.plot_beats import derive_canonical_pacing

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
VIRUS_ARCHIVE_RETENTION = 200     # Turns to keep archived (discovered/purged) viruses before pruning
VIRUS_LOG_MAX = 5                 # Max consequence log entries per virus before oldest is dropped
DEFAULT_TURN_SECONDS = 30  # Default time per normal turn (all game systems)
NPC_MEMORY_TIER_LIMITS = {"high": 8, "moderate": 10, "flavor": 12}
NPC_MEMORY_MAX_PER_NPC = 30
CHARACTER_STATE_TTL = 150  # Prune NPC character states not updated in this many turns

# ============================================================
# In-Game Clock Helpers
# ============================================================

_TIME_PASSED_RE = re.compile(r'(\d+)\s*(second|minute|hour|day)s?', re.IGNORECASE)

def parse_time_passed(text: str) -> int:
    """Parse freeform duration text to total seconds.
    Handles: '30 seconds', '5 minutes', '2 hours', '1 day',
    '1 hour 30 minutes', etc.  Returns 0 on failure."""
    if not text or not isinstance(text, str):
        return 0
    multipliers = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    total = 0
    for match in _TIME_PASSED_RE.finditer(text):
        n = int(match.group(1))
        unit = match.group(2).lower()
        total += n * multipliers.get(unit, 0)
    return total


def advance_clock(time_str: str, date_str: str, seconds: int) -> tuple:
    """Advance HHMM time by *seconds*, returning (new_time, new_date).
    Handles midnight rollover.  Returns inputs unchanged if unparseable."""
    if not time_str or not isinstance(time_str, str):
        return time_str, date_str
    # Parse HHMM
    t = time_str.strip()
    if len(t) < 3 or not t.isdigit():
        return time_str, date_str
    try:
        h = int(t[:-2]) if len(t) > 2 else 0
        m = int(t[-2:])
    except ValueError:
        return time_str, date_str

    total_secs = h * 3600 + m * 60 + seconds
    days_added = total_secs // 86400
    remainder = total_secs % 86400
    new_h = remainder // 3600
    new_m = (remainder % 3600) // 60
    new_time = f"{new_h:02d}{new_m:02d}"

    new_date = date_str or ""
    if days_added > 0 and new_date:
        # Try to advance date (YYYY-MM-DD or similar)
        try:
            from datetime import datetime, timedelta
            for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y"):
                try:
                    dt = datetime.strptime(new_date.strip(), fmt)
                    dt += timedelta(days=days_added)
                    new_date = dt.strftime(fmt)
                    break
                except ValueError:
                    continue
        except Exception:
            pass  # Leave date unchanged if unparseable
    return new_time, new_date


def _advance_hud_clock(pipeline_state: dict, seconds: int) -> None:
    """Advance hud_state time/date by *seconds*, using the sub-minute buffer."""
    if seconds <= 0:
        return
    hud = pipeline_state.get("hud_state", {})
    old_time = hud.get("time", "")
    old_date = hud.get("date", "")
    if not old_time:  # No clock seed yet — skip
        return
    buf = pipeline_state.get("_clock_seconds_buffer", 0)
    total = buf + seconds
    minutes_to_add = total // 60
    pipeline_state["_clock_seconds_buffer"] = total % 60
    if minutes_to_add > 0:
        new_time, new_date = advance_clock(old_time, old_date, minutes_to_add * 60)
        hud["time"] = new_time
        if new_date:
            hud["date"] = new_date


def _parse_clock_date(s: str):
    """Parse a date string in any of our accepted formats. Returns datetime or None."""
    from datetime import datetime
    if not isinstance(s, str) or not s.strip():
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _parse_clock_time(s: str):
    """Parse an HHMM time string. Returns (h, m) tuple or None."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t or not t.isdigit() or len(t) > 4:
        return None
    try:
        if len(t) <= 2:
            h, m = 0, int(t)
        else:
            h, m = int(t[:-2]), int(t[-2:])
    except ValueError:
        return None
    if 0 <= h < 24 and 0 <= m < 60:
        return h, m
    return None


def _format_duration(seconds: int) -> str:
    """Human-readable duration: '15 minutes', '2 hours 30 minutes', '3 days 4 hours'."""
    if seconds <= 0:
        return "0 seconds"
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        out = f"{hours} hour{'s' if hours != 1 else ''}"
        if rem_min:
            out += f" {rem_min} minute{'s' if rem_min != 1 else ''}"
        return out
    days = hours // 24
    rem_hours = hours % 24
    out = f"{days} day{'s' if days != 1 else ''}"
    if rem_hours:
        out += f" {rem_hours} hour{'s' if rem_hours != 1 else ''}"
    return out


def _push_time_passed_notification(pipeline_state: dict, seconds: int, reason: str = "") -> None:
    """Append a time_passed notification for the frontend to render."""
    pipeline_state.setdefault("_pending_time_notifications", []).append({
        "type": "time_passed",
        "duration": _format_duration(seconds),
        "reason": reason or "",
    })


def _persist_hud_state_with_backend_clock(
    pipeline_state: dict,
    incoming_hud_state: Optional[dict] = None,
    *,
    seconds: int,
    is_ooc: bool = False,
    replace_snapshot: bool = True,
) -> bool:
    """Persist hud_state while keeping time/date backend-owned after initial seed.

    Once a clock seed exists, the model's incoming `time`/`date` are IGNORED — the
    clock advances only by the caller-supplied *seconds* (DEFAULT_TURN_SECONDS by
    default; overridden via `hud_state.time_override = {minutes, reason}`). This
    keeps the contract enforced rather than advisory: model drift on `time`/`date`
    becomes a no-op rather than a spurious skip.

    When *incoming_hud_state* is omitted, the existing HUD snapshot is preserved and
    only the clock is advanced. When *replace_snapshot* is False, incoming fields are
    merged over the existing HUD instead of replacing it. This is used by mode tools,
    which may only need to seed time/date without rebuilding the full HUD snapshot.

    Returns True when the backend advanced an existing clock seed this turn.
    """
    prev_hud = pipeline_state.get("hud_state", {})
    prev_time = prev_hud.get("time", "") if isinstance(prev_hud, dict) else ""
    prev_date = prev_hud.get("date", "") if isinstance(prev_hud, dict) else ""
    hud_base = copy.deepcopy(prev_hud) if isinstance(prev_hud, dict) else {}

    if incoming_hud_state is None:
        hud = hud_base
    elif isinstance(incoming_hud_state, dict):
        incoming = copy.deepcopy(incoming_hud_state)
        hud = {**hud_base, **incoming} if not replace_snapshot else incoming
    else:
        hud = {} if replace_snapshot else hud_base
    hud.pop("time_override", None)

    pipeline_state["hud_state"] = hud

    if prev_time:
        # Once seeded, time/date are backend-owned. Drop any model-supplied
        # values and restore the previous clock; advancement happens via
        # `seconds` (which the caller derived from `time_override`).
        hud["time"] = prev_time
        hud["date"] = prev_date
        if is_ooc:
            return False

        _advance_hud_clock(pipeline_state, seconds)
        return True

    # Fresh chat/bootstrap: accept the model-provided absolute clock once.
    if not hud.get("time"):
        hud.pop("time", None)
        hud.pop("date", None)
    return False


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
    provider: Optional["OpenAIProvider"] = None  # Provider that produced this stage; None = use the aggregator's default


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
    trim_anchor_id: Optional[str] = None  # Message ID of first message in trimmed context window
    enriched_events: Optional[dict] = None  # Post-apply events data (has backend-computed new_total, tier_transition)


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


# ============================================================
# Dice Pool Generation
# ============================================================

DICE_POOL_SPECS = {
    "dnd5e": [(20, 10), (12, 5), (10, 5), (8, 5), (6, 10), (4, 5)],
    "dnd5e_cyber": [(20, 10), (12, 5), (10, 5), (8, 5), (6, 10), (4, 5)],
    "coc7e": [(100, 10), (10, 5), (8, 5), (6, 5), (4, 5)],
    "sr6e": [(6, 40), (20, 5)],
    "cpred": [(10, 20), (6, 15)],
}


def generate_dice_pool(game_system_id: str) -> str:
    """Generate a pre-rolled dice pool for the given game system.

    Returns a formatted [DICE POOL] block with random values for each die type.
    Fresh pool every turn — no state carried across turns.
    """
    spec = DICE_POOL_SPECS.get(game_system_id, DICE_POOL_SPECS["dnd5e"])
    lines = []
    for sides, count in spec:
        rolls = [random.randint(1, sides) for _ in range(count)]
        lines.append(f"d{sides}: {', '.join(str(r) for r in rolls)}")

    return (
        "[DICE POOL]\n"
        "Use these pre-rolled results in order (left to right) for each die type.\n"
        "Do NOT invent your own rolls. If you exhaust a row, note it in your output.\n"
        "\n"
        + "\n".join(lines)
        + "\n[/DICE POOL]"
    )


# ============================================================
# Name Dice Generation (pre-rolled for name generator docs)
# ============================================================

_NAME_GEN_PATTERN = re.compile(r'name.*generator|generator.*name', re.IGNORECASE)
_DIE_SIZE_PATTERN = re.compile(r'\(1(?:-d?|[ ]d|d)(\d+)\)')  # matches (1-100), (1d50), (1d20), (1 d100) but NOT (100) or (1 100)

NAME_DICE_COUNT = 4  # rolls per die size


def generate_name_dice(uploads_dir: str) -> str:
    """Generate pre-rolled dice for name generator docs in a project's uploads.

    Scans uploads_dir for files whose name contains both "name" and "generator"
    (case-insensitive). Parses die sizes from markdown table headers, pre-rolls
    a small batch for each, and returns a formatted [NAME DICE] block.

    Returns empty string if no name generator file is found or no die sizes detected.
    """
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return ""

    # Find name generator file(s)
    name_gen_files = []
    try:
        for fname in os.listdir(uploads_dir):
            stem = os.path.splitext(fname)[0]
            if _NAME_GEN_PATTERN.search(stem):
                name_gen_files.append(os.path.join(uploads_dir, fname))
    except OSError:
        return ""

    if not name_gen_files:
        return ""

    # Parse die sizes from all matching files
    die_sizes = set()
    for fpath in name_gen_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for match in _DIE_SIZE_PATTERN.finditer(content):
                size = int(match.group(1))
                if size >= 1:
                    die_sizes.add(size)
        except (OSError, ValueError):
            continue

    if not die_sizes:
        return ""

    # Pre-roll dice, largest first for readability
    lines = []
    for sides in sorted(die_sizes, reverse=True):
        rolls = [random.randint(1, sides) for _ in range(NAME_DICE_COUNT)]
        lines.append(f"d{sides}: {', '.join(str(r) for r in rolls)}")

    return (
        "[NAME DICE]\n"
        "Use these with the Name Generator document when naming new NPCs.\n"
        "Consume left-to-right; do not skip or reuse.\n"
        + "\n".join(lines)
        + "\n[/NAME DICE]"
    )


# ============================================================
# Deterministic Mechanics Resolution (cpred only)
# ============================================================

def resolve_pipeline_mechanics(
    beats: list,
    game_state: dict,
    relationship_owner: str = "",
    relationship_present_names=None,
    character_states: dict = None,
    virus_ledger: dict = None,
    stealth_active: bool = False,
    quiet_jack_in_used: bool = False,
    stealth_broken_round=None,
    net_round: int = 1,
) -> tuple:
    """Resolve structured beats from Events using deterministic code.

    For each beat with a non-null "resolution", dispatch to the appropriate
    cpred_mechanics function. Annotate the beat with a "result" dict.

    Returns (annotated_beats, collected_state_ops).
    """
    from game_systems.cpred_mechanics import resolve_actions
    from game_systems.cpred import init_game_state as cpred_init_game_state, apply_game_state as cpred_apply_game_state

    annotated = []
    all_state_ops = []
    # Resolve beats against an evolving snapshot so later beats see prior outcomes.
    shadow_state = copy.deepcopy(game_state) if isinstance(game_state, dict) else {}
    if not shadow_state:
        shadow_state = cpred_init_game_state()
    shadow_edgerunner_names = set((shadow_state.get("edgerunners") or {}).keys())

    def _hydrate_action_from_state(action: dict) -> dict:
        action_type = action.get("type")
        target = action.get("target")
        if isinstance(target, str) and target:
            edgerunners = shadow_state.get("edgerunners", {})
            er = edgerunners.get(target) if isinstance(edgerunners, dict) else None
            if isinstance(er, dict):
                loc = action.get("hit_location", "body")
                if loc not in ("head", "body"):
                    loc = "body"
                armor = er.get("armor", {}) if isinstance(er.get("armor"), dict) else {}
                hp = er.get("hp", {}) if isinstance(er.get("hp"), dict) else {}
                if action_type in ("ranged_attack", "autofire", "melee_attack"):
                    action["target_sp"] = int(armor.get(loc, action.get("target_sp", 0)))
                action["target_hp_current"] = int(hp.get("current", action.get("target_hp_current", 0)))

        character = action.get("character")
        if action_type == "death_save" and isinstance(character, str) and character:
            edgerunners = shadow_state.get("edgerunners", {})
            er = edgerunners.get(character) if isinstance(edgerunners, dict) else None
            if isinstance(er, dict):
                action["death_save_count"] = int(er.get("death_save_count", action.get("death_save_count", 0)))
                action["active_injuries"] = copy.deepcopy(er.get("critical_injuries", action.get("active_injuries", [])))
                body_stat = er.get("body")
                if isinstance(body_stat, int):
                    action["body_stat"] = body_stat
        return action

    for beat in beats:
        if not isinstance(beat, dict):
            # Legacy string beat — pass through as narrative-only
            annotated.append({"beat": beat, "resolution": None})
            continue

        resolution = beat.get("resolution")
        if resolution is None:
            annotated.append(beat)
            continue

        # Build an action from the resolution request
        try:
            action = dict(resolution)  # copy so we don't mutate
            action = _hydrate_action_from_state(action)
            _relationship_context = build_relationship_context(
                actions=[action],
                relationship_owner=relationship_owner,
                relationship_actor_names=shadow_edgerunner_names,
                relationship_present_names=relationship_present_names,
            )
            result = resolve_actions(
                [action],
                relationships=shadow_state.get("relationships"),
                factions=shadow_state.get("factions"),
                relationship_context=_relationship_context,
                edgerunner_states=shadow_state.get("edgerunners"),
                character_states=character_states,
                virus_ledger=virus_ledger,
                stealth_active=stealth_active,
                quiet_jack_in_used=quiet_jack_in_used,
                stealth_broken_round=stealth_broken_round,
                net_round=net_round,
            )
            action_results = result.get("results", [])
            action_ops = result.get("state_ops", [])

            # Determine on_outcome from success/failure
            action_result = action_results[0] if action_results else {}
            on_outcome = ""
            if action_result.get("error"):
                on_outcome = f"Error: {action_result['error']}"
            elif action_result.get("success") is True:
                on_outcome = resolution.get("on_success", "success")
            elif action_result.get("success") is False:
                on_outcome = resolution.get("on_failure", "failure")
            elif action_result.get("type") == "initiative":
                on_outcome = "Initiative rolled"
            elif "hit" in action_result:
                # Autofire (top-level hit, no attacks array)
                on_outcome = resolution.get("on_hit", "hit") if action_result["hit"] else resolution.get("on_miss", "miss")
            elif action_result.get("survived") is True:
                on_outcome = "survived"
            elif action_result.get("survived") is False:
                on_outcome = "failed"
            else:
                # Attacks: check if any hit (melee has "hit", ranged has "roll.success")
                attacks = action_result.get("attacks", [])
                hits = sum(1 for a in attacks
                           if a.get("hit", a.get("roll", {}).get("success", False)))
                if hits > 0:
                    on_outcome = resolution.get("on_hit", f"{hits} hit(s)")
                elif attacks:
                    on_outcome = resolution.get("on_miss", "miss")
                else:
                    on_outcome = resolution.get("on_success", "resolved")

            action_result["on_outcome"] = on_outcome
            beat_copy = dict(beat)
            beat_copy["result"] = action_result
            annotated.append(beat_copy)
            all_state_ops.extend(action_ops)
            if action_ops:
                # Never create synthetic edgerunners in shadow state from NPC/enemy target ops.
                shadow_ops = [
                    op for op in action_ops
                    if isinstance(op, dict) and state_op_has_subject_kind(op, "edgerunner", shadow_edgerunner_names)
                ]
                if shadow_ops:
                    cpred_apply_game_state(shadow_state, {"edgerunner_ops": shadow_ops}, turn=0)

        except Exception as e:
            logger.warning(f"resolve_pipeline_mechanics: error resolving beat: {e}")
            beat_copy = dict(beat)
            beat_copy["result"] = {"error": str(e)}
            annotated.append(beat_copy)

    return annotated, all_state_ops


def _sync_cpred_character_states_from_game_state(
    character_states: dict,
    game_state: dict,
    current_turn: int,
    tracked_edgerunners: Optional[set[str]] = None,
) -> dict:
    """Mirror CPRED edgerunner vitals/resources/conditions into character_states."""
    if not isinstance(character_states, dict):
        character_states = {}
    edgerunners = game_state.get("edgerunners", {}) if isinstance(game_state, dict) else {}
    if not isinstance(edgerunners, dict) or not edgerunners:
        return character_states

    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    def _upsert_stat(items: list, label: str, current: int, maximum: int) -> list:
        out = [i for i in items if isinstance(i, dict)]
        for item in out:
            if item.get("label") == label:
                item["current"] = current
                item["max"] = maximum
                return out
        out.append({"label": label, "current": current, "max": maximum})
        return out

    allowed = tracked_edgerunners if isinstance(tracked_edgerunners, set) else set(edgerunners.keys())
    tracked_names = {name for name in character_states.keys() if name in edgerunners and name in allowed}
    if not tracked_names:
        return character_states

    updates = {}
    for name in tracked_names:
        er = edgerunners.get(name, {})
        if not isinstance(er, dict):
            continue

        existing_entry = character_states.get(name, {})
        existing_data = existing_entry.get("data", existing_entry) if isinstance(existing_entry, dict) else {}
        data = copy.deepcopy(existing_data) if isinstance(existing_data, dict) else {}

        hp = er.get("hp", {}) if isinstance(er.get("hp"), dict) else {}
        humanity = er.get("humanity", {}) if isinstance(er.get("humanity"), dict) else {}
        luck = er.get("luck", {}) if isinstance(er.get("luck"), dict) else {}

        data.setdefault("type", "pc")
        data.setdefault("class", "")
        data.setdefault("level", None)

        vitals = data.get("vitals", []) if isinstance(data.get("vitals"), list) else []
        vitals = _upsert_stat(vitals, "HP", _safe_int(hp.get("current", 0)), _safe_int(hp.get("max", 0)))
        vitals = _upsert_stat(vitals, "Humanity", _safe_int(humanity.get("current", 0)), _safe_int(humanity.get("max", 0)))
        data["vitals"] = vitals

        resources = data.get("resources", []) if isinstance(data.get("resources"), list) else []
        resources = _upsert_stat(resources, "Luck", _safe_int(luck.get("current", 0)), _safe_int(luck.get("max", 0)))
        # Armor is NOT mirrored into resources. The contract (cpred.py) says armor
        # is rendered from edgerunner state directly — the frontend reads
        # gameState.edgerunners[name].armor for tile + modal display. Mirroring it
        # here would (a) contradict the contract, (b) make armor show as pip-strip
        # resources which doesn't match what SP is semantically, and (c) fight
        # _merge_character_data's guard that strips armor* resource labels.
        # The data is always available via game_state.edgerunners — no mirror needed.
        data["resources"] = resources

        existing_conditions = data.get("conditions", [])
        if not isinstance(existing_conditions, list):
            existing_conditions = []
        synced_general_conditions = existing_entry.get("_synced_general_conditions", [])
        if not isinstance(synced_general_conditions, list):
            synced_general_conditions = []
        synced_general_conditions = {
            cond for cond in synced_general_conditions if isinstance(cond, str)
        }
        conditions = [
            c for c in existing_conditions
            if (
                isinstance(c, str)
                and c != "Seriously Wounded"
                and not c.startswith("Critical Injury: ")
                and c not in synced_general_conditions
            )
        ]
        # Sync general conditions from edgerunner state (partially_nude, unconscious, etc.)
        er_conditions = er.get("conditions", [])
        current_synced_general_conditions = []
        if isinstance(er_conditions, list):
            for cond in er_conditions:
                if isinstance(cond, str) and cond not in conditions:
                    conditions.append(cond)
                    current_synced_general_conditions.append(cond)
                elif isinstance(cond, str):
                    current_synced_general_conditions.append(cond)
        if er.get("seriously_wounded"):
            conditions.append("Seriously Wounded")
        for ci in er.get("critical_injuries", []):
            if isinstance(ci, dict) and ci.get("name"):
                conditions.append(f"Critical Injury: {ci['name']}")
        data["conditions"] = conditions

        updates[name] = data

    # Write directly — do NOT go through apply_character_states. Its
    # _merge_character_data has a "preserve old max" guard against model
    # hallucination, which would here undo our authoritative sync (e.g.
    # if the model previously emitted RedVelvet with HP 35/35 and Luck
    # 6/6 by hallucinating Delphi's stats, the merge would refuse to
    # accept the correct 30/30 + 8/8 from edgerunners). The values in
    # `updates` come from game_state.edgerunners and ARE the source of
    # truth; the merge guard is meant for model output, not this sync.
    result = character_states
    for name, data in updates.items():
        result[name] = {"data": data, "last_updated": current_turn}
    for name in tracked_names:
        entry = result.get(name)
        if not isinstance(entry, dict):
            continue
        er = edgerunners.get(name, {})
        er_conditions = er.get("conditions", []) if isinstance(er, dict) else []
        if isinstance(er_conditions, list):
            entry["_synced_general_conditions"] = [
                cond for cond in er_conditions if isinstance(cond, str)
            ]
        else:
            entry["_synced_general_conditions"] = []
    return result


def build_events_messages(
    system_prompt: str,
    history_messages: list[dict],
    user_message: dict,
    pipeline_state: dict,
    game_system: dict = None,
    name_dice: str = ""
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

    # 2b. Virus ledger (planted viruses, persistent across sessions)
    virus_injection = build_virus_ledger_injection(pipeline_state.get("virus_ledger", {}))
    if virus_injection:
        injections.append(virus_injection)

    # 3. Decision flags (persistent plot decisions / branch points)
    df_injection = build_decision_flags_injection(pipeline_state.get("decision_flags", {}))
    if df_injection:
        injections.append(df_injection)

    # 4. NPC memories (scene-scoped)
    mem_injection = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem_injection:
        injections.append(mem_injection)

    # 5. Scene state
    scene_injection = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    injections.append(scene_injection)

    # 5. Character states (scene-scoped)
    cs_injection = build_character_states_injection(
        pipeline_state.get("character_states", {}),
        pipeline_state.get("scene_state", {}))
    injections.append(cs_injection)

    # 5b. Character features (game-system specific, e.g. subclass features from conversion doc)
    if game_system and game_system.get("build_features_injection"):
        feat_inj = game_system["build_features_injection"](
            pipeline_state.get("character_states", {}),
            pipeline_state.get("game_state", {}))
        if feat_inj:
            injections.append(feat_inj)

    # 6. Game-specific state injection (e.g. [INVESTIGATOR STATE] for CoC 7E)
    if game_system and game_system.get("build_game_injection"):
        game_injection = game_system["build_game_injection"](pipeline_state.get("game_state", {}), pipeline_state.get("scene_state"))
        if game_injection:
            injections.append(game_injection)

    # 7. Name dice (pre-rolled for name generator doc, if present)
    if name_dice:
        injections.append(name_dice)

    if injections:
        user_content = "\n\n".join(injections) + "\n\n" + user_content

    messages.append({"role": "user", "content": user_content})
    return messages


def build_mechanics_messages(
    system_prompt: str,
    events_json: dict,
    dice_pool: str = "",
    game_injection: str = "",
) -> list[dict]:
    """
    Build the message list for the Mechanics agent.

    Mechanics receives the Events JSON output, an optional dice pool for
    external RNG, and the game-specific state injection (e.g.
    [RELATIONSHIP STATE]) so it can look up tier bonuses when resolving checks.
    """
    messages = [{"role": "system", "content": system_prompt}]
    user_content = json.dumps(events_json, indent=2)
    if game_injection:
        user_content += "\n\n" + game_injection
    if dice_pool:
        user_content += "\n\n" + dice_pool
    messages.append({"role": "user", "content": user_content})
    return messages


def build_narration_messages(
    system_prompt: str,
    recent_pairs: list[dict],
    mechanics_json: dict,
    npc_voices: str = ""
) -> list[dict]:
    """
    Build the message list for the Narration agent.

    Narration sees the last N user-assistant pairs for voice consistency,
    plus the Mechanics JSON as the current user message, optionally prefixed
    with [NPC VOICES] for dialogue consistency.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(recent_pairs)
    user_content = json.dumps(mechanics_json, indent=2)
    if npc_voices:
        user_content = npc_voices + "\n\n" + user_content
    messages.append({"role": "user", "content": user_content})
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
    """Build message content string, including any attached files.
    Image attachments use a placeholder ([image: filename]) so the historical
    context stays text-only — actual image content blocks for the latest user
    message are built separately in main.py via build_image_content_blocks."""
    content = msg.get("content", "")
    attached = msg.get("attached_files", [])
    if attached:
        wrappers = []
        for f in attached:
            mt = (f.get("mime_type") or "")
            if mt.startswith("image/"):
                wrappers.append(f"[image: {f['filename']}]")
            else:
                wrappers.append(f"====FILE: {f['filename']}====\n{f.get('content', '')}\n====END FILE====")
        files_text = "\n\n".join(wrappers)
        content = f"{files_text}\n\n{content}"
    return content


def collapse_hack_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse hack_mode messages into synthetic summary pairs for normal context building.

    Scans the branch_path for consecutive runs of hack_mode=true messages and replaces
    each run with a single user/assistant pair containing the hack result summary.
    System message (first) and current user message (last) are preserved unchanged.
    Non-hack messages are passed through unmodified.

    This ensures that post-hack normal turns don't include hack exchange details
    in the API context, keeping the narrative context clean and focused.
    """
    if len(branch_path) < 3:
        return branch_path

    # Check if there are any hack messages (fast path)
    history = branch_path[1:-1]
    if not any(msg.get("hack_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]  # Keep system message
    i = 0
    while i < len(history):
        msg = history[i]
        if not msg.get("hack_mode"):
            result.append(msg)
            i += 1
            continue

        # Found start of hack run — scan to end
        hack_summary = None
        j = i
        while j < len(history) and history[j].get("hack_mode"):
            # Check for narrative_summary in hack_tool_input on assistant messages
            tool_input = history[j].get("hack_tool_input", {})
            if tool_input and tool_input.get("narrative_summary"):
                hack_summary = tool_input["narrative_summary"]
            j += 1

        # Replace entire hack run with a synthetic summary pair
        if hack_summary:
            result.append({
                "role": "user",
                "content": "[The netrunner initiated a hack sequence.]"
            })
            result.append({
                "role": "assistant",
                "content": f"[HACK RESULT]\n{hack_summary}\n[/HACK RESULT]"
            })
        # else: incomplete hack with no summary — drop silently

        i = j  # Skip past all hack messages

    result.append(branch_path[-1])  # Keep current user message
    return result


def collapse_combat_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse combat_mode messages into synthetic summary pairs for normal context building.

    Scans the branch_path for consecutive runs of combat_mode=true messages and replaces
    each run with a single user/assistant pair containing the combat result summary.
    System message (first) and current user message (last) are preserved unchanged.
    Non-combat messages are passed through unmodified.

    This ensures that post-combat normal turns don't include blow-by-blow combat exchange
    details in the API context, keeping the narrative context clean and focused.
    """
    if len(branch_path) < 3:
        return branch_path

    # Check if there are any combat messages (fast path)
    history = branch_path[1:-1]
    if not any(msg.get("combat_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]  # Keep system message
    i = 0
    while i < len(history):
        msg = history[i]
        if not msg.get("combat_mode"):
            result.append(msg)
            i += 1
            continue

        # Found start of combat run — scan to end
        combat_summary = None
        j = i
        while j < len(history) and history[j].get("combat_mode"):
            # Check for narrative_summary in combat_tool_input on assistant messages
            tool_input = history[j].get("combat_tool_input", {})
            if tool_input and tool_input.get("narrative_summary"):
                combat_summary = tool_input["narrative_summary"]
            j += 1

        # Replace entire combat run with a synthetic summary pair
        if combat_summary:
            result.append({
                "role": "user",
                "content": "[A combat encounter took place.]"
            })
            result.append({
                "role": "assistant",
                "content": f"[COMBAT RESULT]\n{combat_summary}\n[/COMBAT RESULT]"
            })
        # else: incomplete combat with no summary — drop silently

        i = j  # Skip past all combat messages

    result.append(branch_path[-1])  # Keep current user message
    return result


def collapse_ship_combat_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse ship_combat_mode messages into synthetic summary pairs for normal context."""
    if len(branch_path) < 3:
        return branch_path

    history = branch_path[1:-1]
    if not any(msg.get("ship_combat_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]
    i = 0
    while i < len(history):
        msg = history[i]
        if not msg.get("ship_combat_mode"):
            result.append(msg)
            i += 1
            continue

        ship_combat_summary = None
        combat_outcome = None
        first_sc_msg = history[i]
        last_sc_msg = history[i]
        j = i
        while j < len(history) and history[j].get("ship_combat_mode"):
            last_sc_msg = history[j]
            tool_input = history[j].get("ship_combat_tool_input", {})
            if tool_input and tool_input.get("narrative_summary"):
                ship_combat_summary = tool_input["narrative_summary"]
            if tool_input and tool_input.get("combat_outcome"):
                combat_outcome = tool_input["combat_outcome"]
            # Also check dedicated field on message
            if history[j].get("ship_combat_combat_outcome"):
                combat_outcome = history[j]["ship_combat_combat_outcome"]
            j += 1

        if ship_combat_summary:
            collapsed_parts = ["[SHIP COMBAT RESULT]"]
            collapsed_parts.append(ship_combat_summary)
            if combat_outcome:
                collapsed_parts.append(f"Outcome: {combat_outcome.get('outcome', 'unknown')} — {combat_outcome.get('outcome_detail', '')}")
                collapsed_parts.append(f"Rounds: {combat_outcome.get('rounds_fought', '?')}")
                for ship in combat_outcome.get("ship_final_states", []):
                    collapsed_parts.append(f"  {ship.get('ship_name', '?')} ({ship.get('faction', '')}): {ship.get('status', '')} — {ship.get('hull_percent', '')}% hull")
                for evt in combat_outcome.get("notable_events", []):
                    collapsed_parts.append(f"  - {evt}")
            collapsed_parts.append("[/SHIP COMBAT RESULT]")
            result.append({
                "id": first_sc_msg.get("id"),
                "role": "user",
                "content": "[A ship combat encounter took place.]"
            })
            result.append({
                "id": last_sc_msg.get("id"),
                "role": "assistant",
                "content": "\n".join(collapsed_parts)
            })
        else:
            # Preserve a placeholder for unfinished/interrupted ship combat runs so
            # trimmed context doesn't silently lose the encounter.
            result.append({
                "id": first_sc_msg.get("id"),
                "role": "user",
                "content": "[A ship combat encounter is in progress.]"
            })
            result.append({
                "id": last_sc_msg.get("id"),
                "role": "assistant",
                "content": "[SHIP COMBAT STATUS]\nShip combat is ongoing; use current ship_combat state for details.\n[/SHIP COMBAT STATUS]"
            })

        i = j

    result.append(branch_path[-1])
    return result


def collapse_net_combat_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse net_combat_mode messages into synthetic summary pairs for normal context.

    Same pattern as collapse_combat_messages / collapse_hack_messages — scans for runs of
    net_combat_mode=true messages and replaces each run with a [NET COMBAT RESULT] summary.
    """
    if len(branch_path) < 3:
        return branch_path

    history = branch_path[1:-1]
    if not any(isinstance(msg, dict) and msg.get("net_combat_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]
    i = 0
    while i < len(history):
        msg = history[i]
        if not isinstance(msg, dict) or not msg.get("net_combat_mode"):
            result.append(msg)
            i += 1
            continue

        # Found start of net_combat run — scan to end
        nc_summary = None
        j = i
        while j < len(history):
            hmsg = history[j]
            if not isinstance(hmsg, dict) or not hmsg.get("net_combat_mode"):
                break
            tool_input = hmsg.get("net_combat_tool_input", {})
            if isinstance(tool_input, dict) and tool_input.get("narrative_summary"):
                nc_summary = tool_input["narrative_summary"]
            j += 1

        if nc_summary:
            result.append({
                "role": "user",
                "content": "[A combined meatspace + NET combat encounter took place.]"
            })
            result.append({
                "role": "assistant",
                "content": f"[NET COMBAT RESULT]\n{nc_summary}\n[/NET COMBAT RESULT]"
            })

        i = j

    result.append(branch_path[-1])
    return result


def collapse_chase_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse chase_mode (Hot Pursuit) messages into synthetic summary pairs.

    Same pattern as collapse_combat_messages / collapse_net_combat_messages —
    scans for runs of chase_mode=true messages and replaces each run with a
    single user/assistant pair containing the chase result summary.
    """
    if len(branch_path) < 3:
        return branch_path

    history = branch_path[1:-1]
    if not any(isinstance(msg, dict) and msg.get("chase_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]
    i = 0
    while i < len(history):
        msg = history[i]
        if not isinstance(msg, dict) or not msg.get("chase_mode"):
            result.append(msg)
            i += 1
            continue

        chase_summary = None
        end_reason = None
        j = i
        while j < len(history):
            hmsg = history[j]
            if not isinstance(hmsg, dict) or not hmsg.get("chase_mode"):
                break
            tool_input = hmsg.get("chase_tool_input", {})
            if isinstance(tool_input, dict) and tool_input.get("narrative_summary"):
                chase_summary = tool_input["narrative_summary"]
            if isinstance(tool_input, dict) and tool_input.get("end_reason"):
                end_reason = tool_input["end_reason"]
            j += 1

        if chase_summary:
            collapsed_parts = ["[CHASE RESULT]"]
            collapsed_parts.append(chase_summary)
            if end_reason:
                collapsed_parts.append(f"End: {end_reason}")
            collapsed_parts.append("[/CHASE RESULT]")
            result.append({
                "role": "user",
                "content": "[A vehicle chase took place.]"
            })
            result.append({
                "role": "assistant",
                "content": "\n".join(collapsed_parts)
            })
        # else: incomplete chase with no summary — drop silently

        i = j

    result.append(branch_path[-1])
    return result


def collapse_sex_messages(branch_path: list[dict]) -> list[dict]:
    """Collapse sex_mode messages into a discreet summary pair for normal context.

    Scans for consecutive runs of sex_mode=true messages and replaces each run
    with a FADE TO BLACK summary pair.
    """
    if len(branch_path) < 3:
        return branch_path

    history = branch_path[1:-1]
    if not any(msg.get("sex_mode") for msg in history):
        return branch_path

    result = [branch_path[0]]
    i = 0
    while i < len(history):
        msg = history[i]
        if not msg.get("sex_mode"):
            result.append(msg)
            i += 1
            continue

        # Found start of sex mode run — scan to end
        sex_summary = None
        j = i
        while j < len(history) and history[j].get("sex_mode"):
            if history[j].get("sex_scene_summary"):
                sex_summary = history[j]["sex_scene_summary"]
            j += 1

        # Replace entire sex mode run with discreet summary pair
        result.append({
            "role": "user",
            "content": "[An intimate scene took place.]"
        })
        if sex_summary:
            result.append({
                "role": "assistant",
                "content": f"[FADE TO BLACK]\n{sex_summary}\n[/FADE TO BLACK]"
            })
        else:
            result.append({
                "role": "assistant",
                "content": "[FADE TO BLACK]"
            })

        i = j

    result.append(branch_path[-1])
    return result


def _filter_unstaged_pairs(messages: list[dict]) -> list[dict]:
    """Remove message pairs where the user message has staged=False.
    Skips both the user and its following assistant message to keep pairs intact."""
    filtered = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "user" and msg.get("staged") is False:
            # Skip this user message and the next assistant message
            i += 2
        else:
            filtered.append(msg)
            i += 1
    return filtered


def get_context_pairs(
    branch_path: list[dict],
    threshold_pairs: int,
    target_pairs: int,
    trim_anchor_id: Optional[str] = None,
    manual_staging: bool = False
) -> tuple[list[dict], Optional[str], bool]:
    """
    Extract context pairs using a sawtooth trim pattern for cache efficiency.

    Uses a trim anchor (message ID) to maintain a stable context prefix.
    Context grows from the anchor until it exceeds threshold_pairs, then
    trims to target_pairs and sets a new anchor. The prefix stays stable
    between trims (~20 turns), maximizing Anthropic prompt cache hits.

    Args:
        manual_staging: If True, filter out message pairs where the user
            message has staged=False before counting pairs.

    Returns (pairs, new_anchor_id, did_trim):
    - pairs: flat list of {role, content} dicts
    - new_anchor_id: anchor ID for next turn (store in chat data)
    - did_trim: True if a trim happened this turn
    """
    # branch_path: [system, ...history..., current_user]
    history = branch_path[1:-1]  # All messages between system and current user

    # Find anchor position in history
    anchor_idx = 0
    if trim_anchor_id:
        found = False
        for i, msg in enumerate(history):
            if msg.get("id") == trim_anchor_id:
                anchor_idx = i
                found = True
                break
        if not found:
            # Anchor not in current branch (branch switch) — include all
            anchor_idx = 0
            trim_anchor_id = None

    context = history[anchor_idx:]

    # Filter out manually unstaged pairs (Novels system)
    if manual_staging:
        context = _filter_unstaged_pairs(context)

    context_pair_count = len(context) // 2

    if context_pair_count > threshold_pairs:
        # Trim to target_pairs from the end of context (already filtered)
        new_start = len(context) - target_pairs * 2
        pair_messages = context[new_start:]
        new_anchor_id = pair_messages[0].get("id") if pair_messages else None
        return (
            [{"role": msg["role"], "content": build_message_content(msg)} for msg in pair_messages],
            new_anchor_id,
            True
        )
    else:
        return (
            [{"role": msg["role"], "content": build_message_content(msg)} for msg in context],
            trim_anchor_id,
            False
        )


# ============================================================
# Pipeline State Migration & Op-Application
# ============================================================

def _fresh_pipeline_state() -> dict:
    """Return a fresh pipeline_state with all sub-structures initialized."""
    return {
        "pacing": {},
        "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
        "virus_ledger": {"next_id": 1, "active": [], "archived": []},
        "npc_memories": {},
        "scene_state": {},
        "character_states": {},
        "game_state": {},
        "hud_state": {},
        "decision_flags": {},
        "combat": None,
        "net_combat": None,
        "ship_combat": None,
        "chase": None,
        "sex_scene": None,
        "turn_counter": 0,
        "_clock_seconds_buffer": 0
    }


def migrate_pipeline_state(state: Optional[dict]) -> dict:
    """Migrate pipeline_state from any legacy format to the current nested structure."""
    if state is None or not isinstance(state, dict):
        return _fresh_pipeline_state()

    # Old flat format: state IS the pacing dict (has keys like "episode", "beat" but no "pacing" key)
    if "pacing" not in state:
        return {
            "pacing": state,
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "virus_ledger": {"next_id": 1, "active": [], "archived": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "game_state": {},
            "hud_state": {},
            "decision_flags": {},
            "combat": None,
            "net_combat": None,
            "ship_combat": None,
            "chase": None,
            "sex_scene": None,
            "turn_counter": 0,
            "_clock_seconds_buffer": 0
        }

    # New format — ensure all keys exist
    state.setdefault("pacing", {})
    if not isinstance(state.get("pacing"), dict):
        state["pacing"] = {}
    state.setdefault("callback_ledger", {"next_id": 1, "open": [], "recently_resolved": []})
    ledger = state["callback_ledger"]
    if not isinstance(ledger, dict):
        ledger = {"next_id": 1, "open": [], "recently_resolved": []}
        state["callback_ledger"] = ledger
    ledger.setdefault("next_id", 1)
    if not isinstance(ledger.get("next_id"), int):
        ledger["next_id"] = 1
    ledger.setdefault("open", [])
    if not isinstance(ledger.get("open"), list):
        ledger["open"] = []
    ledger.setdefault("recently_resolved", [])
    if not isinstance(ledger.get("recently_resolved"), list):
        ledger["recently_resolved"] = []
    state.setdefault("npc_memories", {})
    if not isinstance(state.get("npc_memories"), dict):
        state["npc_memories"] = {}
    state.setdefault("scene_state", {})
    if not isinstance(state.get("scene_state"), dict):
        state["scene_state"] = {}
    state.setdefault("character_states", {})
    if not isinstance(state.get("character_states"), dict):
        state["character_states"] = {}
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
    # Re-normalize ledger after character_state migration in case of alias-corruption.
    ledger = state.get("callback_ledger", {})
    if not isinstance(ledger, dict):
        ledger = {"next_id": 1, "open": [], "recently_resolved": []}
        state["callback_ledger"] = ledger
    if not isinstance(ledger.get("next_id"), int):
        ledger["next_id"] = 1
    if not isinstance(ledger.get("open"), list):
        ledger["open"] = []
    if not isinstance(ledger.get("recently_resolved"), list):
        ledger["recently_resolved"] = []
    state.setdefault("virus_ledger", {"next_id": 1, "active": [], "archived": []})
    vledger = state["virus_ledger"]
    if not isinstance(vledger, dict):
        vledger = {"next_id": 1, "active": [], "archived": []}
        state["virus_ledger"] = vledger
    vledger.setdefault("next_id", 1)
    if not isinstance(vledger.get("next_id"), int):
        vledger["next_id"] = 1
    vledger.setdefault("active", [])
    if not isinstance(vledger.get("active"), list):
        vledger["active"] = []
    vledger.setdefault("archived", [])
    if not isinstance(vledger.get("archived"), list):
        vledger["archived"] = []
    state.setdefault("game_state", {})
    if not isinstance(state.get("game_state"), dict):
        state["game_state"] = {}
    state.setdefault("hud_state", {})
    if not isinstance(state.get("hud_state"), dict):
        state["hud_state"] = {}
    state.setdefault("decision_flags", {})
    if not isinstance(state.get("decision_flags"), dict):
        state["decision_flags"] = {}
    state.setdefault("combat", None)
    state.setdefault("net_combat", None)
    state.setdefault("ship_combat", None)
    state.setdefault("chase", None)
    if state.get("chase") is not None and not isinstance(state.get("chase"), dict):
        state["chase"] = None
    if state.get("ship_combat") is not None and not isinstance(state.get("ship_combat"), dict):
        state["ship_combat"] = None
    if isinstance(state.get("ship_combat"), dict):
        sc = state["ship_combat"]
        sc.setdefault("round", 1)
        sc.setdefault("initiative_order", [])
        sc.setdefault("current_ship", None)
        sc.setdefault("current_role", None)
        sc.setdefault("environment", "Open Space")
        sc.setdefault("bootstrap_done", False)
        sc.setdefault("ship_combat_handoff_source", None)
        sc.setdefault("bootstrap_messages", [])
    state.setdefault("sex_scene", None)
    state.setdefault("turn_counter", 0)
    if not isinstance(state.get("turn_counter"), int):
        state["turn_counter"] = 0
    state.setdefault("_clock_seconds_buffer", 0)
    if not isinstance(state.get("_clock_seconds_buffer"), int):
        state["_clock_seconds_buffer"] = 0
    return state


def apply_callback_ops(ledger: dict, ops: list, current_turn: int) -> dict:
    """Apply callback_ops to the ledger and prune old resolved entries."""
    open_list = ledger.get("open", [])
    if not isinstance(open_list, list):
        open_list = []
    resolved_list = ledger.get("recently_resolved", [])
    if not isinstance(resolved_list, list):
        resolved_list = []
    next_id = ledger.get("next_id", 1)
    if not isinstance(next_id, int):
        next_id = 1

    # Build index of open callbacks by ID for fast lookup
    open_by_id = {}
    for cb in open_list:
        if not isinstance(cb, dict):
            continue
        cb_id = cb.get("id")
        try:
            hash(cb_id)
        except TypeError:
            continue
        open_by_id[cb_id] = cb

    ops_iter = ops if isinstance(ops, (list, tuple)) else []
    for op in ops_iter:
        if not isinstance(op, dict):
            continue
        action = op.get("action")

        if action == "add":
            text = str(op.get("original_text", "") or "")[:800]
            resolutions_raw = op.get("resolutions")
            if isinstance(resolutions_raw, (list, tuple)):
                resolutions = [str(r)[:200] for r in list(resolutions_raw)[:3]]
            else:
                resolutions = []
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
            if not isinstance(fields, dict):
                continue
            for k, v in fields.items():
                if k not in ("id", "created_turn"):  # Protect immutable fields
                    open_by_id[target_id][k] = v

    # Prune old resolved entries
    resolved_list = [
        r for r in resolved_list if isinstance(r, dict)
        if current_turn - r.get("resolved_turn", current_turn) <= CALLBACK_RESOLVED_RETENTION
    ]

    return {
        "next_id": next_id,
        "open": list(open_by_id.values()),
        "recently_resolved": resolved_list
    }


def apply_virus_ops(ledger: dict, ops: list, current_turn: int, current_date: str) -> dict:
    """Apply virus_ops to the virus_ledger.

    Operations:
      - plant: add a new virus to active. fields: target, planter, narrative
      - activate / discover / purge: status transitions (with optional log entry)
      - log: append a consequence entry to an active virus without status change
      - update: correct fields on an active virus (target, planter, narrative)

    Status transitions purge → archived after VIRUS_ARCHIVE_RETENTION turns from
    `archived_turn`. Discovered viruses stay in `active` until purged.
    """
    active_list = ledger.get("active", [])
    if not isinstance(active_list, list):
        active_list = []
    archived_list = ledger.get("archived", [])
    if not isinstance(archived_list, list):
        archived_list = []
    next_id = ledger.get("next_id", 1)
    if not isinstance(next_id, int):
        next_id = 1

    active_by_id = {}
    for v in active_list:
        if not isinstance(v, dict):
            continue
        vid = v.get("id")
        try:
            hash(vid)
        except TypeError:
            continue
        active_by_id[vid] = v

    ops_iter = ops if isinstance(ops, (list, tuple)) else []
    for op in ops_iter:
        if not isinstance(op, dict):
            continue
        action = op.get("action")

        if action == "plant":
            target = str(op.get("target", "") or "").strip()[:200]
            if not target:
                logger.warning("virus_ops plant: missing target; skipping")
                continue
            planter = str(op.get("planter", "") or "").strip()[:80] or "unknown"
            narrative = str(op.get("narrative", "") or "").strip()[:800]
            entry = {
                "id": next_id,
                "target": target,
                "planter": planter,
                "narrative": narrative,
                "planted_date": current_date or "",
                "planted_turn": current_turn,
                "status": "dormant",
                "log": []
            }
            active_by_id[next_id] = entry
            next_id += 1

        elif action in ("activate", "discover", "purge"):
            target_id = op.get("id")
            if target_id is None or target_id not in active_by_id:
                logger.warning(f"virus_ops {action}: ID {target_id} not found in active viruses")
                continue
            entry = active_by_id[target_id]
            new_status = {"activate": "activated", "discover": "discovered", "purge": "purged"}[action]
            entry["status"] = new_status
            log_text = str(op.get("log", "") or "").strip()[:400]
            if log_text:
                vlog = entry.setdefault("log", [])
                if not isinstance(vlog, list):
                    vlog = []
                    entry["log"] = vlog
                vlog.append(log_text)
                if len(vlog) > VIRUS_LOG_MAX:
                    entry["log"] = vlog[-VIRUS_LOG_MAX:]
            if action == "purge":
                # Move to archived with a stamp
                entry["archived_turn"] = current_turn
                entry["archived_date"] = current_date or ""
                archived_list.append(entry)
                active_by_id.pop(target_id, None)

        elif action == "log":
            target_id = op.get("id")
            if target_id is None or target_id not in active_by_id:
                logger.warning(f"virus_ops log: ID {target_id} not found in active viruses")
                continue
            entry = active_by_id[target_id]
            log_text = str(op.get("entry", "") or "").strip()[:400]
            if not log_text:
                continue
            vlog = entry.setdefault("log", [])
            if not isinstance(vlog, list):
                vlog = []
                entry["log"] = vlog
            vlog.append(log_text)
            if len(vlog) > VIRUS_LOG_MAX:
                entry["log"] = vlog[-VIRUS_LOG_MAX:]

        elif action == "update":
            target_id = op.get("id")
            if target_id is None or target_id not in active_by_id:
                logger.warning(f"virus_ops update: ID {target_id} not found in active viruses")
                continue
            fields = op.get("fields", {})
            if not isinstance(fields, dict):
                continue
            allowed = {"target", "planter", "narrative"}
            for k, v in fields.items():
                if k in allowed:
                    active_by_id[target_id][k] = str(v or "")[:800]

        else:
            logger.warning(f"virus_ops: unknown action {action!r}; skipping")

    # Prune archived past retention
    archived_list = [
        a for a in archived_list if isinstance(a, dict)
        if current_turn - a.get("archived_turn", current_turn) <= VIRUS_ARCHIVE_RETENTION
    ]

    return {
        "next_id": next_id,
        "active": list(active_by_id.values()),
        "archived": archived_list
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


def filter_ops_by_scene_scope(parsed: dict, scene_state: dict) -> None:
    """Filter npc_memory_ops and relationship_ops to only include NPCs in the current scene.

    Mutates parsed dict in-place (so downstream notification extraction sees filtered ops).
    Faction ops (fr) and bootstrap ops (set/npc_set) are always allowed through.
    Skips filtering only when scene_state itself is empty/uninitialized (first-turn bootstrap).
    An initialized scene with npcs_present=[] is valid and means no NPC ops are allowed.
    """
    if not isinstance(parsed, dict):
        return
    if not isinstance(scene_state, dict) or not scene_state:
        # No scene_state yet (first turn / bootstrap) — allow all ops through
        return

    raw_npcs_present = scene_state.get("npcs_present", [])
    if not isinstance(raw_npcs_present, list):
        raw_npcs_present = []
    npcs_present = {n for n in raw_npcs_present if isinstance(n, str) and n}
    skipped = 0

    # Filter npc_memory_ops
    mem_ops = parsed.get("npc_memory_ops")
    if isinstance(mem_ops, list):
        filtered = [op for op in mem_ops if isinstance(op, dict) and op.get("npc") in npcs_present]
        skipped += len(mem_ops) - len(filtered)
        parsed["npc_memory_ops"] = filtered

    # Filter relationship_ops — only rs/roms/npc_rs/npc_roms for NPCs not present
    # Allow: fr (factions aren't scene-scoped), set/npc_set (bootstrap)
    rel_ops = parsed.get("relationship_ops")
    if isinstance(rel_ops, list):
        filtered_rel = []
        for op in rel_ops:
            if not isinstance(op, dict):
                skipped += 1
                continue
            op_type = op.get("op")
            if op_type in ("set", "npc_set", "fr"):
                # Always allow bootstrap and faction ops
                filtered_rel.append(op)
            elif op_type in ("rs", "roms", "npc_rs", "npc_roms"):
                target = op.get("target", "")
                if isinstance(target, str) and target in npcs_present:
                    filtered_rel.append(op)
                else:
                    skipped += 1
                    logger.info(f"Filtered {op_type} op for '{target}' — not in scene")
            else:
                filtered_rel.append(op)
        parsed["relationship_ops"] = filtered_rel

    if skipped:
        logger.info(f"Scene-scope filter: dropped {skipped} ops for out-of-scene NPCs")


def apply_npc_memory_ops(memories: dict, ops: list, current_turn: int) -> dict:
    """Apply npc_memory_ops (add/drop) to the NPC memories dict."""
    if not isinstance(memories, dict):
        memories = {}
    if not isinstance(ops, (list, tuple)) or not ops:
        return memories

    # Sort all NPC lists to match injection display order BEFORE processing drops,
    # so drop indices align with what the model saw in [NPC MEMORIES] blocks
    for npc_name in list(memories.keys()):
        npc_entries = memories.get(npc_name)
        if not isinstance(npc_entries, list):
            memories[npc_name] = []
            continue
        memories[npc_name] = sorted(
            [m for m in npc_entries if isinstance(m, dict)],
            key=_memory_sort_key,
            reverse=True
        )

    # Process drops first (reverse index order to avoid shift issues)
    def _coerce_index(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    drop_ops = sorted(
        [op for op in ops if isinstance(op, dict) and op.get("action") == "drop"],
        key=lambda o: _coerce_index(o.get("index")),
        reverse=True
    )
    for op in drop_ops:
        npc = op.get("npc")
        idx = _coerce_index(op.get("index"))
        if not isinstance(npc, str) or not npc or npc not in memories:
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
    add_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "add"]
    for op in add_ops:
        npc = op.get("npc")
        if not isinstance(npc, str) or not npc:
            continue
        try:
            impact = int(op.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        tier = _memory_tier(impact)
        entry = {
            "text": str(op.get("text", "") or "")[:640],
            "quote": str(op.get("quote") or "")[:120] or None,
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


_PRESENCE_DELTA_KEYS = {
    "pcs_present": ("_pcs_present_add", "_pcs_present_remove"),
    "npcs_present": ("_npcs_present_add", "_npcs_present_remove"),
}


def _apply_presence_deltas(existing_list, new_scene: dict, field: str) -> list:
    """Apply add/remove delta ops for a presence list (pcs_present / npcs_present).

    Precedence:
      - If the model emits the full list (`pcs_present: [...]`), that is treated
        as authoritative — scene transitions / new scenes declare the full roster.
      - If the model emits only delta ops (`_pcs_present_add`, `_pcs_present_remove`),
        they apply on top of the retained existing list — use this during an
        ongoing scene where one person enters/leaves and the rest of the roster
        is stable. This prevents silent drops from "full replacement every turn."

    Both may be emitted together (rare but supported): the full list sets the
    baseline, then deltas apply on top. `remove` runs after `add`.
    """
    add_key, remove_key = _PRESENCE_DELTA_KEYS[field]
    if field in new_scene:
        base_list = list(new_scene[field]) if isinstance(new_scene[field], list) else []
    else:
        base_list = list(existing_list) if isinstance(existing_list, list) else []

    adds = new_scene.get(add_key)
    if isinstance(adds, list):
        for name in adds:
            if isinstance(name, str) and name and name not in base_list:
                base_list.append(name)

    removes = new_scene.get(remove_key)
    if isinstance(removes, list):
        remove_set = {n for n in removes if isinstance(n, str) and n}
        base_list = [n for n in base_list if n not in remove_set]

    return base_list


def apply_scene_state(new_scene: dict, existing_scene: dict = None) -> dict:
    """Apply scene_state with merge: new keys overwrite, absent keys retain existing values.

    Presence lists (pcs_present, npcs_present) support delta ops:
      - `_pcs_present_add` / `_pcs_present_remove` (list of names)
      - `_npcs_present_add` / `_npcs_present_remove` (list of names)
    When only deltas are emitted, the retained list is updated in-place — this
    prevents "full replacement every turn" from silently dropping NPCs on any
    turn where the model forgets to re-list them. When the full list IS emitted,
    it acts as a baseline (scene transitions, new scenes) and any deltas apply
    on top.
    """
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
    existing = existing_scene if isinstance(existing_scene, dict) else {}
    base = {k: existing.get(k, default) for k, default in defaults.items()}
    if not isinstance(new_scene, dict):
        return base
    for key in defaults:
        if key in _PRESENCE_DELTA_KEYS:
            base[key] = _apply_presence_deltas(base[key], new_scene, key)
        elif key in new_scene:
            base[key] = new_scene[key]
    return base


_CANONICAL_FLAG_LINE_RE = re.compile(
    r'^\s*[-*]\s+\*{0,2}([A-Z][A-Z0-9_]*)\*{0,2}\s*[:\-]', re.MULTILINE
)


def parse_canonical_flags(uploads_dir: str) -> list[str]:
    """Read decision_flags.md from a project's uploads dir.

    Returns the ordered list of canonical flag keys declared in the doc.
    Empty list if the file is missing — caller should treat this as "no
    enum constraint, accept any key" for backward compatibility.

    Format: each canonical flag appears as a bullet line beginning with
    `- **FLAG_NAME**:` or `- FLAG_NAME:` (uppercase letters, digits,
    underscores). Bold markers and a trailing colon or dash terminator
    are recognized.
    """
    if not uploads_dir:
        return []
    path = os.path.join(uploads_dir, "decision_flags.md")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except OSError:
        return []
    seen = []
    for match in _CANONICAL_FLAG_LINE_RE.finditer(content):
        key = match.group(1)
        if key not in seen:
            seen.append(key)
    return seen


def build_state_report_tool_with_flag_enum(base_tool: dict, canonical_flags: list[str]) -> dict:
    """Return a deep copy of `base_tool` with `plot_ops[].key` constrained
    to the project's canonical flag enum. If the input schema lacks the
    expected nested path or canonical_flags is empty, returns the input
    unchanged.
    """
    if not canonical_flags:
        return base_tool
    try:
        plot_ops_props = (base_tool["input_schema"]["properties"]["plot_ops"]
                          ["items"]["properties"])
    except (KeyError, TypeError):
        return base_tool
    if "key" not in plot_ops_props:
        return base_tool
    new_tool = copy.deepcopy(base_tool)
    key_schema = new_tool["input_schema"]["properties"]["plot_ops"]["items"]["properties"]["key"]
    # Allow null (for divergences with no matching plot variable) plus the canonical set.
    key_schema["enum"] = [None] + list(canonical_flags)
    return new_tool


def _normalize_flag_key(raw: str, canonical: list[str]) -> str:
    """Normalize an incoming flag key against the canonical list.

    Steps:
    - Strip whitespace, uppercase, replace internal whitespace with underscores
    - If the normalized form matches a canonical key (case-insensitive), use canonical
    - Otherwise return the normalized form (still stored, just unmatched)
    """
    if not isinstance(raw, str):
        return raw
    cleaned = raw.strip().upper().replace(" ", "_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not canonical:
        return cleaned
    canonical_upper = {c.upper(): c for c in canonical}
    if cleaned in canonical_upper:
        return canonical_upper[cleaned]
    return cleaned


def apply_decision_flags(existing_flags: dict, plot_ops: list, canonical_flags: list[str] | None = None) -> dict:
    """Apply plot_ops to the persistent decision_flags dict.

    Each plot op with a 'key' stores its value (and metadata) as a persistent flag.
    Flags survive scene transitions and context trims — they're the campaign's
    authoritative record of branch points, player decisions, and plot variables.

    Pre-registration: ops with value=null or value="pending" register a pending
    flag (value=None) that shows as "(pending)" in the injection. This acts as a
    persistent reminder that the decision hasn't been made yet.

    Guards:
    - Pending cannot overwrite resolved (idempotent re-registration).
    - Pending cannot overwrite pending (preserves original description).
    - Resolved "branch" flags cannot be overwritten once set (permanent decisions).

    If `canonical_flags` is provided, incoming keys are normalized
    (uppercase, whitespace-to-underscore) and case-insensitively matched
    to the canonical form to absorb minor spelling drift from the model.
    """
    canonical = canonical_flags or []
    flags = existing_flags if isinstance(existing_flags, dict) else {}
    if not plot_ops:
        return flags
    for op in plot_ops:
        key = op.get("key")
        if not key:
            continue
        # Normalize incoming key against the canonical list to absorb
        # minor spelling drift (case, spaces, double underscores). If
        # canonical_flags is empty, this just upper-cases + cleans.
        key = _normalize_flag_key(key, canonical)
        value = op.get("value")
        # Normalize common values
        if isinstance(value, str):
            vl = value.lower()
            if vl in ("true", "yes"):
                value = True
            elif vl in ("false", "no"):
                value = False
            elif vl in ("null", "pending", "none", ""):
                value = None
        existing = flags.get(key)
        if isinstance(existing, dict):
            existing_value = existing.get("value")
            # Don't overwrite any existing flag with a pending one
            if value is None:
                continue
            # Don't overwrite a resolved "branch" flag (permanent decisions)
            if existing_value is not None and existing.get("severity") == "branch":
                continue
        # Fall back to existing metadata if the op doesn't provide it
        existing_decision = existing.get("decision", "") if isinstance(existing, dict) else ""
        existing_severity = existing.get("severity", "") if isinstance(existing, dict) else ""
        existing_episode = existing.get("episode", "") if isinstance(existing, dict) else ""
        flags[key] = {
            "value": value,
            "decision": op.get("decision", "") or existing_decision,
            "severity": op.get("severity", "") or existing_severity or "flag",
            "episode": op.get("episode", "") or existing_episode,
        }
    return flags


def build_decision_flags_injection(flags: dict) -> str:
    """Build human-readable injection of persistent decision flags.

    Pending flags (value=None) are shown as "(pending)" with their description,
    acting as a persistent reminder to the model that this decision needs recording.
    Resolved flags show their value.
    """
    if not flags:
        return ""
    # Sort: pending flags first (reminders), then resolved
    pending = []
    resolved = []
    for key, entry in flags.items():
        if isinstance(entry, dict):
            val = entry.get("value")
            desc = entry.get("decision", "")
            sev = entry.get("severity", "")
            if val is None:
                line = f"- {key} = (pending)"
                if desc:
                    line += f"  # {desc}"
                pending.append(line)
            else:
                line = f"- {key} = {val}"
                if sev and sev != "flag":
                    line += f" ({sev})"
                if desc:
                    line += f"  # {desc}"
                resolved.append(line)
        else:
            resolved.append(f"- {key} = {entry}")

    lines = ["[DECISION FLAGS]"]
    lines.extend(pending)
    lines.extend(resolved)
    lines.append("[/DECISION FLAGS]")
    return "\n".join(lines)


def _canonicalize_character_name(existing_keys, incoming_name: str) -> str:
    """If incoming_name is an alias of a name already in existing_keys (or vice
    versa), return the canonical key to use. Otherwise return incoming_name.

    Alias heuristic: case-insensitive prefix match on a word boundary. E.g.,
    "Red" aliases "RedVelvet", "Red Velvet", or "Red the Netrunner". Prefers
    the longer name when an alias is detected, since the longer form is more
    specific and matches the canonical edgerunner key in most cases.
    """
    if not isinstance(incoming_name, str) or not incoming_name:
        return incoming_name
    if incoming_name in existing_keys:
        return incoming_name
    inc_lower = incoming_name.lower()
    # Look for a longer existing key that starts with incoming + word boundary
    for key in existing_keys:
        if not isinstance(key, str) or not key or key == incoming_name:
            continue
        key_lower = key.lower()
        if key_lower == inc_lower:
            return key  # case-only difference
        # incoming is a prefix of existing (e.g., "Red" -> "RedVelvet" / "Red Velvet")
        if key_lower.startswith(inc_lower) and len(key_lower) > len(inc_lower):
            sep = key_lower[len(inc_lower)]
            if not sep.isalnum():  # word boundary (space, punctuation) — reject "Redditor"
                return key
            # Also accept CamelCase boundary: incoming "Red" + existing "RedVelvet" → 'V' uppercase
            if key[len(inc_lower)].isupper():
                return key
        # existing is a prefix of incoming — prefer existing canonical name
        if inc_lower.startswith(key_lower) and len(inc_lower) > len(key_lower):
            sep = inc_lower[len(key_lower)]
            if not sep.isalnum() or incoming_name[len(key_lower)].isupper():
                return key
    return incoming_name


def _merge_character_data(old_data: dict, new_data: dict) -> dict:
    """Merge new_data into old_data, preserving identity fields and resources
    that the LLM omitted.

    - Identity fields (`type`, `class`) on an existing entry are NEVER replaced
      by a different value from the LLM (common hallucination after sex_mode /
      isolated contexts). New entries set them freely.
    - `level` preserved if new omits or sends null.
    - Vitals: `max` values are preserved per-label when the new update changes
      them without a matching label-specific `_max_override`. `current` follows
      the new update.
    - Resources (Armor, Luck, Spell Slots, …): merged by label. Entries present
      in old but absent from new are preserved. Max values preserved unless
      explicitly set.
    - Conditions: fully replaced by new (LLM re-asserts active conditions).
    """
    if not isinstance(old_data, dict):
        return dict(new_data) if isinstance(new_data, dict) else {}
    if not isinstance(new_data, dict):
        return dict(old_data)

    merged = dict(new_data)

    # Identity: never flip type/class on an existing entry
    for field in ("type", "class"):
        old_val = old_data.get(field)
        new_val = new_data.get(field)
        if old_val and (not new_val or new_val != old_val):
            merged[field] = old_val

    # Level: preserve if new omits or nulls
    if new_data.get("level") is None and old_data.get("level") is not None:
        merged["level"] = old_data.get("level")

    # Vitals: preserve max per label
    old_vitals_by_label = {v.get("label"): v for v in (old_data.get("vitals") or []) if isinstance(v, dict) and v.get("label")}
    new_vitals = new_data.get("vitals") or []
    merged_vitals = []
    seen_labels = set()
    for v in new_vitals:
        if not isinstance(v, dict) or not v.get("label"):
            merged_vitals.append(v)
            continue
        label = v["label"]
        seen_labels.add(label)
        old_v = old_vitals_by_label.get(label)
        if old_v and "max" in old_v and "max" in v and old_v["max"] != v["max"]:
            # Preserve old max; keep new current (clamped)
            new_entry = dict(v)
            new_entry["max"] = old_v["max"]
            if "current" in new_entry:
                new_entry["current"] = min(new_entry["current"], old_v["max"])
            merged_vitals.append(new_entry)
        else:
            merged_vitals.append(v)
    # Preserve old vitals whose labels are missing from new update
    for label, old_v in old_vitals_by_label.items():
        if label not in seen_labels:
            merged_vitals.append(dict(old_v))
    if merged_vitals or "vitals" in new_data:
        merged["vitals"] = merged_vitals

    # Resources: merge by label, preserving missing ones and old max
    old_res_by_label = {r.get("label"): r for r in (old_data.get("resources") or []) if isinstance(r, dict) and r.get("label")}
    new_res = new_data.get("resources") or []
    merged_res = []
    seen_res = set()
    for r in new_res:
        if not isinstance(r, dict) or not r.get("label"):
            merged_res.append(r)
            continue
        label = r["label"]
        seen_res.add(label)
        old_r = old_res_by_label.get(label)
        if old_r and "max" in old_r and "max" in r and old_r["max"] != r["max"]:
            new_entry = dict(r)
            new_entry["max"] = old_r["max"]
            if "current" in new_entry:
                new_entry["current"] = min(new_entry["current"], old_r["max"])
            merged_res.append(new_entry)
        else:
            merged_res.append(r)
    for label, old_r in old_res_by_label.items():
        if label not in seen_res:
            merged_res.append(dict(old_r))
    if merged_res or "resources" in new_data:
        # Strip resources that duplicate a vital label (e.g. Humanity as both
        # vital and resource) or that shadow edgerunner-owned armor tracking.
        # Model hallucinates these periodically; once they're in resources they
        # survive forever because _merge_character_data preserves missing
        # labels from the old snapshot.
        vital_labels = {
            v.get("label") for v in (merged.get("vitals") or [])
            if isinstance(v, dict) and v.get("label")
        }
        merged["resources"] = [
            r for r in merged_res
            if not (
                isinstance(r, dict)
                and (
                    r.get("label") in vital_labels
                    or (isinstance(r.get("label"), str) and r["label"].lower().startswith("armor"))
                )
            )
        ]

    return merged


def apply_character_states(existing: dict, mechanics_output: dict, current_turn: int, scene_state: dict = None) -> dict:
    """
    Merge Mechanics' character_states into existing state and prune stale entries.

    Each entry is stored as {"data": {structured_object}, "last_updated": <turn>}.
    Accepts both:
      - New structured format: {"name": {"type": "pc", "vitals": [...], ...}}
      - Old string format: {"name": "state string"} → wrapped as {"data": {"summary": str}, ...}

    Delta ops (applied on top of existing state before full replacement):
      - "_conditions_add": ["Poisoned", ...] → append to existing conditions
      - "_conditions_remove": ["Blessed", ...] → remove from existing conditions
      - "_resource_deltas": [{"label": "Spell Slots (1st)", "delta": -1}, ...] → adjust current value

    Canonicalization: incoming names are checked for alias matches against
    existing keys (e.g., "Red" -> "RedVelvet"); the LLM's name is remapped to
    the canonical key so duplicate entries don't proliferate.

    Identity protection: on updates to an existing entry, `type`, `class`, and
    `max` values on vitals/resources are preserved against LLM hallucination
    (common after sex_mode / isolated contexts). New entries set these freely.
    Resources omitted by the LLM are preserved from the old snapshot.

    Entries not updated in CHARACTER_STATE_TTL turns are pruned.
    """
    # Also canonicalize against scene_state names (they're authoritative for UI)
    alias_pool = set(existing.keys())
    if isinstance(scene_state, dict):
        for key in ("pcs_present", "npcs_present"):
            for n in scene_state.get(key) or []:
                if isinstance(n, str) and n:
                    alias_pool.add(n)

    # Merge new entries from Mechanics
    for raw_name, state_val in mechanics_output.items():
        name = _canonicalize_character_name(alias_pool, raw_name)
        if isinstance(state_val, dict):
            # Check for delta ops — apply against existing state
            cond_add = state_val.pop("_conditions_add", None)
            cond_remove = state_val.pop("_conditions_remove", None)
            res_deltas = state_val.pop("_resource_deltas", None)

            old_entry = existing.get(name)
            old_data = old_entry.get("data") if isinstance(old_entry, dict) else None

            if cond_add or cond_remove or res_deltas:
                # Start from existing data if available, merge new fields on top
                base = dict(old_data) if isinstance(old_data, dict) else {}
                # Overlay any non-delta fields the model provided (with identity protection)
                if state_val:
                    base = _merge_character_data(base, state_val) if base else dict(state_val)
                # Apply condition deltas
                conditions = list(base.get("conditions", []))
                if cond_add:
                    for c in cond_add:
                        if c not in conditions:
                            conditions.append(c)
                if cond_remove:
                    for c in cond_remove:
                        if c in conditions:
                            conditions.remove(c)
                base["conditions"] = conditions
                # Apply resource deltas
                if res_deltas and isinstance(res_deltas, list):
                    resources = base.get("resources", [])
                    for rd in res_deltas:
                        label = rd.get("label")
                        delta = rd.get("delta", 0)
                        if not label or not delta:
                            continue
                        for res in resources:
                            if res.get("label") == label and "current" in res:
                                res["current"] = max(0, res["current"] + delta)
                                if "max" in res:
                                    res["current"] = min(res["current"], res["max"])
                                break
                    base["resources"] = resources
                existing[name] = {"data": base, "last_updated": current_turn}
            else:
                # Full-update path — protect identity/max/resources when the
                # entry already exists; otherwise accept the new entry as-is.
                if isinstance(old_data, dict):
                    merged = _merge_character_data(old_data, state_val)
                    existing[name] = {"data": merged, "last_updated": current_turn}
                else:
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


def _apply_resolver_character_state_deltas(
    character_states: dict,
    resolver_ops: list,
    current_turn: int,
    tracked_edgerunners: Optional[set[str]] = None,
) -> dict:
    """Apply resolver condition deltas for non-edgerunners into character_states."""
    if not isinstance(character_states, dict):
        character_states = {}
    if not isinstance(resolver_ops, list) or not resolver_ops:
        return character_states

    tracked = tracked_edgerunners if isinstance(tracked_edgerunners, set) else set()
    deltas = {}
    for op in resolver_ops:
        if not isinstance(op, dict):
            continue
        name = state_op_subject_name(op, "character") or state_op_subject_name(op, "edgerunner")
        if not isinstance(name, str) or not name or name in tracked:
            continue
        op_type = op.get("op")
        condition = op.get("condition", "")
        if not isinstance(condition, str) or not condition:
            continue
        entry = deltas.setdefault(name, {})
        if op_type == "add_condition":
            entry.setdefault("_conditions_add", []).append(condition)
        elif op_type == "remove_condition":
            entry.setdefault("_conditions_remove", []).append(condition)

    if not deltas:
        return character_states
    return apply_character_states(character_states, deltas, current_turn)


# ============================================================
# Injection Builders (format state for model consumption)
# ============================================================

def scope_hud_funds(hud_state: dict, scene_state: dict, character_states: dict) -> dict:
    """Return hud_state with funds filtered to scene-present characters.
    Non-character keys (ship, party) pass through. String funds unchanged."""
    if not isinstance(hud_state, dict):
        return {}
    if not isinstance(scene_state, dict):
        scene_state = {}
    if not isinstance(character_states, dict):
        character_states = {}
    funds = hud_state.get("funds")
    pcs_present = scene_state.get("pcs_present")
    if not isinstance(pcs_present, list):
        pcs_present = []
    npcs_present = scene_state.get("npcs_present")
    if not isinstance(npcs_present, list):
        npcs_present = []
    present = {n for n in pcs_present + npcs_present if isinstance(n, str) and n}
    if not isinstance(funds, dict) or not present:
        return hud_state
    all_chars = {name for name in character_states.keys() if isinstance(name, str)}
    tracked_fund_keys = hud_state.get("_character_fund_keys", [])
    if isinstance(tracked_fund_keys, list):
        all_chars.update(name for name in tracked_fund_keys if isinstance(name, str))
    return {**hud_state, "funds": {
        k: v for k, v in funds.items() if k in present or k not in all_chars
    }}


def derive_funds_from_ship_credits(hud_state: dict, game_state: dict) -> dict:
    """If game_state has ship.credits, derive hud_state.funds from it (single source of truth).
    Returns hud_state (possibly with replaced funds). Non-ship game systems are unaffected."""
    if not isinstance(hud_state, dict):
        return {}
    if not isinstance(game_state, dict):
        return hud_state
    ship = game_state.get("ship", {})
    if not isinstance(ship, dict):
        return hud_state
    ship_credits = ship.get("credits")
    if not isinstance(ship_credits, dict):
        return hud_state
    derived = {
        account: f"{amount:,} cr" if isinstance(amount, (int, float)) else str(amount)
        for account, amount in ship_credits.items()
    }
    return {**hud_state, "funds": derived}


def _sync_hud_funds_from_edgerunners(hud_state: dict, game_state: dict) -> dict:
    """Sync per-edgerunner eurobucks from game_state into hud_state.funds.

    Parallels derive_funds_from_ship_credits but for CPRED edgerunner wallets.
    Preserves non-edgerunner fund keys (e.g. "crew fund") so shared pools remain
    model-managed.
    """
    if not isinstance(hud_state, dict):
        hud_state = {}
    if not isinstance(game_state, dict):
        return hud_state
    edgerunners = game_state.get("edgerunners", {})
    if not isinstance(edgerunners, dict) or not edgerunners:
        return hud_state
    er_funds = {}
    er_names = set()
    for name, er in edgerunners.items():
        er_names.add(name)
        if not isinstance(er, dict):
            continue
        eb = er.get("eurobucks")
        if isinstance(eb, (int, float)):
            er_funds[name] = f"{int(eb):,} eb"
    if not er_funds:
        return hud_state
    # Preserve non-edgerunner keys (shared pools like "crew fund")
    existing_funds = hud_state.get("funds", {})
    if not isinstance(existing_funds, dict):
        existing_funds = {}
    merged = {k: v for k, v in existing_funds.items() if k not in er_names}
    merged.update(er_funds)
    return {**hud_state, "funds": merged, "_character_fund_keys": sorted(er_names)}


def _sync_and_scope_cpred_hud_funds(
    hud_state: dict,
    game_state: dict,
    scene_state: dict,
    character_states: dict,
) -> dict:
    """Refresh CPRED wallet HUD from game_state, then re-apply scene scoping."""
    synced = _sync_hud_funds_from_edgerunners(hud_state, game_state)
    return scope_hud_funds(synced, scene_state, character_states)


def _rebuild_cpred_projections(
    pipeline_state: dict,
    current_turn: int,
    tracked_edgerunners: Optional[set[str]] = None,
) -> dict:
    """Rebuild CPRED derived views from authoritative game_state in one place.

    Order is intentional:
    1. character_states already contain model/resolver non-authoritative deltas
    2. sync authoritative edgerunner vitals/resources/conditions from game_state
    3. rebuild/scoped HUD funds from authoritative edgerunner balances
    """
    if not isinstance(pipeline_state, dict):
        return {"character_states": {}, "hud_state": {}}

    character_states = _sync_cpred_character_states_from_game_state(
        pipeline_state.get("character_states", {}),
        pipeline_state.get("game_state", {}),
        current_turn,
        tracked_edgerunners=tracked_edgerunners,
    )
    pipeline_state["character_states"] = character_states

    hud_state = _sync_and_scope_cpred_hud_funds(
        pipeline_state.get("hud_state", {}),
        pipeline_state.get("game_state", {}),
        pipeline_state.get("scene_state", {}),
        character_states,
    )
    pipeline_state["hud_state"] = hud_state

    return {
        "character_states": character_states,
        "hud_state": hud_state,
    }


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


def build_virus_ledger_injection(ledger: dict) -> str:
    """Build human-readable virus ledger injection.

    Always include all active viruses (no scoping). Archived entries shown
    in compact form for long-term continuity. Returns empty string if no
    active or archived viruses exist.
    """
    if not isinstance(ledger, dict):
        return ""
    active = ledger.get("active") or []
    archived = ledger.get("archived") or []
    if not active and not archived:
        return ""

    lines = ["[VIRUS LEDGER]"]

    if active:
        lines.append("ACTIVE:")
        for v in active:
            if not isinstance(v, dict):
                continue
            vid = v.get("id", "?")
            target = v.get("target", "?")
            planter = v.get("planter", "?")
            planted_date = v.get("planted_date") or "unknown"
            status = v.get("status", "dormant")
            narrative = v.get("narrative", "")
            lines.append(
                f"#{vid} (planted {planted_date} by {planter}, target: {target}, status: {status})"
            )
            if narrative:
                lines.append(f"   \"{narrative}\"")
            log_entries = v.get("log") or []
            for entry in log_entries:
                if isinstance(entry, str) and entry:
                    lines.append(f"   - {entry}")
    else:
        lines.append("ACTIVE: (none)")

    if archived:
        lines.append("")
        lines.append("ARCHIVED (for continuity):")
        for v in archived:
            if not isinstance(v, dict):
                continue
            vid = v.get("id", "?")
            target = v.get("target", "?")
            planted_date = v.get("planted_date") or "unknown"
            archived_date = v.get("archived_date") or "unknown"
            narrative = v.get("narrative", "")
            log_entries = v.get("log") or []
            last_log = log_entries[-1] if log_entries else ""
            line = f"#{vid} (planted {planted_date} → purged {archived_date}, target: {target}): \"{narrative}\""
            if last_log:
                line += f" → \"{last_log}\""
            lines.append(line)

    lines.append("[/VIRUS LEDGER]")
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
        return "[SCENE STATE]\n(empty — bootstrap from current story context)\n[/SCENE STATE]"

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
    # Identity (type + class) so the LLM doesn't have to guess role on re-report
    ident_bits = []
    if data.get("type"):
        ident_bits.append(str(data["type"]))
    if data.get("class"):
        ident_bits.append(str(data["class"]))
    if ident_bits:
        parts.append(" ".join(ident_bits))
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


def build_character_states_injection(character_states: dict, scene_state: dict = None) -> str:
    """Build human-readable character states injection for Events.

    Entries are stored as {"name": {"data": {...}, "last_updated": N}}.
    Falls back gracefully for old formats.
    When scene_state is provided, only includes characters in pcs_present + npcs_present.
    """
    if not character_states:
        return "[CHARACTER STATES]\n(empty — bootstrap from character sheets in system prompt, or begin interactive character creation if no sheets are available)\n[/CHARACTER STATES]"
    # Scene-scope filter: only inject characters present in the scene
    if scene_state:
        pcs_present = scene_state.get("pcs_present") or []
        npcs_present = scene_state.get("npcs_present") or []
        present = set(pcs_present + npcs_present)
        if present:
            character_states = {k: v for k, v in character_states.items() if k in present}
    if not character_states:
        return "[CHARACTER STATES]\n(no characters in scene)\n[/CHARACTER STATES]"
    lines = ["[CHARACTER STATES]"]
    for name, entry in character_states.items():
        if isinstance(entry, dict):
            data = entry.get("data") or entry.get("state", "")
            lines.append(f"{name}: {_format_character_data(data)}")
        else:
            lines.append(f"{name}: {entry}")
    lines.append("[/CHARACTER STATES]")
    return "\n".join(lines)


def build_npc_voices_injection(character_states: dict, scene_state: dict = None, doc_file_stems: set = None) -> str:
    """Build [NPC VOICES] injection with voice blurbs for improvised NPCs in the scene.

    Filters to scene-present NPCs/enemies that have a voice blurb and are NOT
    defined in project doc files (doc-defined NPCs have full profiles in the system prompt).
    """
    if not character_states:
        return ""

    # Scene-scope filter: only NPCs currently present
    if scene_state:
        present = set(scene_state.get("npcs_present") or [])
        if not present:
            return ""
    else:
        return ""

    doc_file_stems = doc_file_stems or set()

    lines = []
    for name, entry in character_states.items():
        if name not in present:
            continue
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") or entry.get("state")
        if not isinstance(data, dict):
            continue
        char_type = data.get("type", "")
        if char_type not in ("npc", "enemy"):
            continue
        voice = data.get("voice")
        if not voice:
            continue
        # Skip doc-defined NPCs (case-insensitive substring match)
        name_lower = name.lower()
        if doc_file_stems and any(name_lower in stem or stem in name_lower for stem in doc_file_stems):
            continue
        lines.append(f"{name}: {voice}")

    if not lines:
        return ""
    return "[NPC VOICES]\n" + "\n".join(lines) + "\n[/NPC VOICES]"


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
    game_system: str = "dnd5e",
    trim_anchor_id: Optional[str] = None,
    doc_file_stems: set = None,
    name_dice: str = "",
    uploads_dir: Optional[str] = None,
    planning_provider: OpenAIProvider = None,
    narration_provider: OpenAIProvider = None,
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

    `provider` is the legacy single-provider arg (used for usage aggregation
    fallbacks and as the default when planning_provider/narration_provider
    are not supplied). Pass `planning_provider` to run Events / model-driven
    Mechanics on a reasoning-strong model and `narration_provider` to run
    Narration on a prose-strong model.

    This is a generator that the event_generator in send_message_stream iterates over.
    """
    _planning_provider = planning_provider or provider
    _narration_provider = narration_provider or provider

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
    # Collapse hack and combat messages into summary pairs before context trimming
    branch_path_for_events = collapse_sex_messages(collapse_net_combat_messages(collapse_ship_combat_messages(collapse_combat_messages(collapse_hack_messages(branch_path)))))
    recent_events_pairs, new_trim_anchor_id, _did_trim = get_context_pairs(
        branch_path_for_events, EVENTS_THRESHOLD_PAIRS, EVENTS_TARGET_PAIRS, trim_anchor_id
    )
    user_msg = {"role": "user", "content": build_message_content(branch_path[-1])}
    events_messages = build_events_messages(events_system, recent_events_pairs, user_msg, pipeline_state, game_system=gs, name_dice=name_dice)

    events_result = run_pipeline_stage(
        _planning_provider, client, STAGE_CONFIGS["events"],
        events_messages, username, project, chat_name
    )
    events_result.provider = _planning_provider

    yield ("pipeline_stage", {"stage": "events", "status": "complete"})

    events_data = events_result.parsed_json
    events_route = events_data.get("route", "mechanics")

    # Increment turn counter only for in-character turns — OOC shouldn't age TTLs
    if events_route != "output":
        pipeline_state["turn_counter"] += 1
        _bump_beat_response_counter(pipeline_state)
    current_turn = pipeline_state["turn_counter"]

    # Extract and apply state from Events output
    if isinstance(events_data.get("pacing"), dict):
        pipeline_state["pacing"] = {**pipeline_state.get("pacing", {}), **events_data["pacing"]}
    _apply_canonical_pacing(pipeline_state, uploads_dir)
    pipeline_state["callback_ledger"] = apply_callback_ops(
        pipeline_state["callback_ledger"],
        events_data.get("callback_ops"),
        current_turn
    )
    pipeline_state["virus_ledger"] = apply_virus_ops(
        pipeline_state.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
        events_data.get("virus_ops"),
        current_turn,
        pipeline_state.get("hud_state", {}).get("date", "")
    )
    # Scene-scope filtering: drop memory/relationship ops for NPCs not in scene
    if events_data.get("npc_memory_ops") or events_data.get("relationship_ops"):
        filter_ops_by_scene_scope(events_data, pipeline_state.get("scene_state", {}))
    if events_data.get("npc_memory_ops"):
        pipeline_state["npc_memories"] = apply_npc_memory_ops(
            pipeline_state["npc_memories"],
            events_data["npc_memory_ops"],
            current_turn
        )
    if events_data.get("scene_state"):
        pipeline_state["scene_state"] = apply_scene_state(
            events_data["scene_state"],
            existing_scene=pipeline_state.get("scene_state")
        )
    if events_data.get("plot_ops"):
        pipeline_state["decision_flags"] = apply_decision_flags(
            pipeline_state.get("decision_flags", {}),
            events_data["plot_ops"],
            canonical_flags=parse_canonical_flags(uploads_dir),
        )

    # Apply game-specific state ops (relationship_ops already filtered by scene scope)
    if gs.get("apply_game_state"):
        if "game_state" not in pipeline_state:
            pipeline_state["game_state"] = gs["init_game_state"]()
        gs["apply_game_state"](pipeline_state["game_state"], events_data, current_turn)

    # Persist HUD state from Events. CPRED authoritative funds are rebuilt later
    # after character/game-state updates so model ordering cannot leak stale values.
    # Backend-driven clock: accept an initial seed once, then advance only by deltas.
    if "hud_state" in events_data or isinstance(pipeline_state.get("hud_state"), dict):
        tp_text = events_data.get("time_passed", "")
        tp_seconds = parse_time_passed(tp_text)
        if tp_seconds == 0:
            tp_seconds = DEFAULT_TURN_SECONDS
        # Snapshot pre-call notifications so we can detect whether the persist call
        # itself queued an implicit-advance notification (in which case we skip the
        # explicit one here to avoid duplicates).
        _notif_count_before = len(pipeline_state.get("_pending_time_notifications", []))
        advanced_clock = _persist_hud_state_with_backend_clock(
            pipeline_state,
            events_data.get("hud_state"),
            seconds=tp_seconds,
            is_ooc=(events_route == "output"),
        )
        _implicit_fired = len(pipeline_state.get("_pending_time_notifications", [])) > _notif_count_before
        if advanced_clock and tp_seconds != DEFAULT_TURN_SECONDS and not _implicit_fired:
            pipeline_state.setdefault("_pending_time_notifications", []).append({
                "type": "time_passed",
                "duration": tp_text,
                "reason": "",
            })

    # Persist combat state (initiative tracker) from Events
    if "combat" in events_data:
        _replace_combat_dict(pipeline_state, events_data["combat"])

    new_pipeline_state = pipeline_state

    # Collect stage results for aggregation
    stage_results = [events_result]
    reasoning_summaries = []
    if events_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Events] {events_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Events → Output ----
    if events_route == "output":
        # Apply character_states from Events (needed for OOC turns like character creation
        # where Mechanics never runs and would otherwise not persist character state)
        if isinstance(events_data.get("character_states"), dict):
            new_pipeline_state["character_states"] = apply_character_states(
                new_pipeline_state["character_states"],
                events_data["character_states"],
                current_turn,
                scene_state=events_data.get("scene_state") or new_pipeline_state.get("scene_state"),
            )
        if game_system == "cpred":
            _rebuild_cpred_projections(new_pipeline_state, current_turn)
        elif isinstance(new_pipeline_state.get("hud_state"), dict):
            new_pipeline_state["hud_state"] = derive_funds_from_ship_credits(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("game_state"))
            new_pipeline_state["hud_state"] = scope_hud_funds(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("scene_state", {}),
                new_pipeline_state.get("character_states", {}),
            )

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
            stage_usage=_build_stage_usage(stage_results, provider),
            trim_anchor_id=new_trim_anchor_id,
            enriched_events=events_data,
        ))
        return

    # ---- STAGE 2: Mechanics ----
    if gs.get("deterministic_mechanics", gs.get("mechanics_contract") is None):
        # Deterministic resolution — skip Mechanics API call (cpred)
        yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})

        canonical_edgerunners = set((new_pipeline_state.get("game_state", {}).get("edgerunners") or {}).keys())
        _scene_state = events_data.get("scene_state") if isinstance(events_data.get("scene_state"), dict) else {}
        _relationship_present_names = set()
        for _group in (_scene_state.get("pcs_present", []), _scene_state.get("npcs_present", [])):
            if not isinstance(_group, list):
                continue
            for _name in _group:
                if isinstance(_name, str) and _name.strip():
                    _relationship_present_names.add(_name.strip())
        resolved_beats, resolver_ops = resolve_pipeline_mechanics(
            events_data.get("beats", []),
            new_pipeline_state.get("game_state", {}),
            relationship_owner=events_data.get("current_player", ""),
            relationship_present_names=_relationship_present_names,
            character_states=new_pipeline_state.get("character_states"),
            virus_ledger=new_pipeline_state.get("virus_ledger"),
        )

        # Apply character_states from Events
        new_pipeline_state["character_states"] = apply_character_states(
            new_pipeline_state["character_states"],
            events_data.get("character_states") if isinstance(events_data.get("character_states"), dict) else {},
            current_turn,
            scene_state=events_data.get("scene_state") or new_pipeline_state.get("scene_state"),
        )

        # Apply resolver's state ops (HP, armor, crit injuries, etc.)
        if gs.get("apply_game_state") and resolver_ops:
            resolver_ops_for_state = [
                op for op in resolver_ops
                if isinstance(op, dict) and state_op_has_subject_kind(op, "edgerunner", canonical_edgerunners)
            ]
            resolver_rel_ops = [
                op for op in resolver_ops
                if isinstance(op, dict) and op.get("type") == "relationship_op"
            ]
        else:
            resolver_ops_for_state = []
            resolver_rel_ops = []
        if gs.get("apply_game_state") and (resolver_ops_for_state or resolver_rel_ops):
            gs["apply_game_state"](new_pipeline_state["game_state"],
                                    {"edgerunner_ops": resolver_ops_for_state,
                                     "relationship_ops": resolver_rel_ops}, current_turn)

        # virus_op state ops live at pipeline_state level — route them to
        # apply_virus_ops directly. Same pattern as the mode-pipeline post-
        # processing in main.py._apply_virus_op_state_ops.
        _v_payloads = []
        for _op in resolver_ops or []:
            if isinstance(_op, dict) and _op.get("op") == "virus_op":
                _v_inner = _op.get("virus_op")
                if isinstance(_v_inner, dict):
                    _v_payloads.append(_v_inner)
        if _v_payloads:
            new_pipeline_state["virus_ledger"] = apply_virus_ops(
                new_pipeline_state.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
                _v_payloads,
                current_turn,
                new_pipeline_state.get("hud_state", {}).get("date", "")
            )

        new_pipeline_state["character_states"] = _apply_resolver_character_state_deltas(
            new_pipeline_state.get("character_states", {}),
            resolver_ops,
            current_turn,
            tracked_edgerunners=canonical_edgerunners,
        )

        # Keep character_states and HUD funds synchronized with resolver-applied state.
        if game_system == "cpred":
            _cpred_views = _rebuild_cpred_projections(
                new_pipeline_state,
                current_turn,
                tracked_edgerunners=canonical_edgerunners,
            )
        elif isinstance(new_pipeline_state.get("hud_state"), dict):
            new_pipeline_state["hud_state"] = derive_funds_from_ship_credits(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("game_state"))
            new_pipeline_state["hud_state"] = scope_hud_funds(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("scene_state", {}),
                new_pipeline_state.get("character_states", {}),
            )
            _cpred_views = None
        else:
            _cpred_views = None

        # Build narration input matching what Narration expects
        mechanics_data = {
            "route": "narration",
            "beats": resolved_beats,
            "edgerunner_ops": (events_data.get("edgerunner_ops") or []) + [
                op for op in resolver_ops
                if isinstance(op, dict) and state_op_has_subject_kind(op, "edgerunner")
            ],
            "relationship_ops": events_data.get("relationship_ops") or [],
            "character_states": {
                name: (entry.get("data", entry) if isinstance(entry, dict) else entry)
                for name, entry in new_pipeline_state.get("character_states", {}).items()
            },
            "arc_label": events_data.get("arc_label"),
            "callbacks": events_data.get("callbacks") or [],
            "current_player": events_data.get("current_player"),
            "next_player": events_data.get("next_player"),
            "next_player_prompt": events_data.get("next_player_prompt"),
            "combat": events_data.get("combat"),
        }

        mechanics_result = PipelineStageResult(
            stage="mechanics", content=json.dumps(mechanics_data),
            parsed_json=mechanics_data, usage={}, service_tier="n/a"
        )
        stage_results.append(mechanics_result)

        yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

    else:
        # Standard Mechanics API call (other game systems)
        yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})

        dice_pool = generate_dice_pool(game_system)
        mechanics_system = build_agent_system_prompt(gs["mechanics_contract"], agent_instructions["mechanics"], agent_files["mechanics"])
        # Build game injection for Mechanics (relationship tiers, game-specific state)
        mechanics_game_injection = ""
        if gs.get("build_game_injection"):
            mechanics_game_injection = gs["build_game_injection"](new_pipeline_state.get("game_state", {}), new_pipeline_state.get("scene_state")) or ""
        mechanics_messages = build_mechanics_messages(mechanics_system, events_data, dice_pool=dice_pool, game_injection=mechanics_game_injection)

        mechanics_result = run_pipeline_stage(
            _planning_provider, client, STAGE_CONFIGS["mechanics"],
            mechanics_messages, username, project, chat_name
        )
        mechanics_result.provider = _planning_provider

        yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

        mechanics_data = mechanics_result.parsed_json
        new_pipeline_state["character_states"] = apply_character_states(
            new_pipeline_state["character_states"],
            mechanics_data.get("character_states") if isinstance(mechanics_data.get("character_states"), dict) else {},
            current_turn,
            scene_state=new_pipeline_state.get("scene_state"),
        )
        # Scene-scope filtering for Mechanics-emitted relationship ops
        if mechanics_data.get("relationship_ops"):
            filter_ops_by_scene_scope(mechanics_data, pipeline_state.get("scene_state", {}))
        # Apply game-specific state ops from Mechanics (roll-dependent outcomes)
        if gs.get("apply_game_state"):
            if "game_state" not in pipeline_state:
                pipeline_state["game_state"] = gs["init_game_state"]()
            gs["apply_game_state"](pipeline_state["game_state"], mechanics_data, current_turn)
        # Keep HUD funds synchronized after Mechanics applies game-state changes.
        if game_system != "cpred" and isinstance(new_pipeline_state.get("hud_state"), dict):
            new_pipeline_state["hud_state"] = derive_funds_from_ship_credits(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("game_state"))
            new_pipeline_state["hud_state"] = scope_hud_funds(
                new_pipeline_state.get("hud_state", {}),
                new_pipeline_state.get("scene_state", {}),
                new_pipeline_state.get("character_states", {}),
            )

        stage_results.append(mechanics_result)
        if mechanics_result.usage.get('reasoning'):
            reasoning_summaries.append(f"[Mechanics] {mechanics_result.usage['reasoning']}")

    mechanics_route = mechanics_data.get("route", "narration")

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
            stage_usage=_build_stage_usage(stage_results, provider),
            trim_anchor_id=new_trim_anchor_id,
            enriched_events=events_data,
        ))
        return

    # ---- STAGE 3: Narration (streaming) ----
    yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})

    narration_system = build_agent_system_prompt(gs["narration_contract"], agent_instructions["narration"], agent_files["narration"])
    # Reuse events pairs — same thresholds, same branch_path, same anchor → same result
    npc_voices = build_npc_voices_injection(
        pipeline_state.get("character_states", {}),
        pipeline_state.get("scene_state", {}),
        doc_file_stems)
    narration_messages = build_narration_messages(narration_system, recent_events_pairs, mechanics_data, npc_voices=npc_voices)

    narration_params = _narration_provider.build_pipeline_request(
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

    for stream_event in _narration_provider.send_request_stream(client, narration_params):
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
        service_tier="standard",
        provider=_narration_provider,
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
        stage_usage=_build_stage_usage(stage_results, provider),
        trim_anchor_id=new_trim_anchor_id,
        enriched_events=events_data,
    ))


@dataclass
class ModeResult:
    """Result from a 2-stage mode pipeline (combat/hack/net_combat)."""
    mode: str
    final_content: str
    planning_json: dict
    resolved_actions: list
    state_ops: list
    aggregate_usage: dict
    aggregate_cost: float
    reasoning_summaries: list[str]
    service_tier_label: str


def run_mode_pipeline(
    provider: OpenAIProvider,
    client,
    username: str,
    project: str,
    chat_name: str,
    mode: str,
    planning_system: str,
    narration_system: str,
    mode_messages: list[dict],
    user_content: str,
    planning_schema: dict,
    game_state: dict = None,
    character_states: dict = None,
    pipeline_state: dict = None,
    tar_stacks: int = 0,
    alert_level: int = 0,
    active_programs=None,
    installed_hardware=None,
    ice_status=None,
    net_actions_remaining=None,
    slide_used_this_turn: bool = False,
    system_map=None,
    current_node=None,
    virus_ledger=None,
    stealth_active: bool = False,
    quiet_jack_in_used: bool = False,
    stealth_broken_round=None,
    net_round: int = 1,
    planning_provider: OpenAIProvider = None,
    narration_provider: OpenAIProvider = None,
) -> Iterator[tuple[str, dict]]:
    """Run a 2-stage mode pipeline for combat/hack/net_combat.

    Stage 1 (Planning): Non-streaming JSON call — model proposes actions + state updates.
    Backend Resolution: resolve_actions() on the actions array.
    Stage 2 (Narration): Streaming call — model writes prose from resolved actions.

    `provider` is the legacy single-provider arg (used for usage aggregation and
    as the default when planning_provider/narration_provider are not supplied).
    Pass `planning_provider` and/or `narration_provider` to run the two stages
    on different OpenAI models — typically a reasoning-strong model for Stage 1
    and a prose-strong model for Stage 2.

    Yields same event types as run_pipeline(): pipeline_stage, content, pipeline_done.
    """
    _planning_provider = planning_provider or provider
    _narration_provider = narration_provider or provider
    from game_systems.cpred_mechanics import resolve_actions

    # ---- STAGE 1: Planning ----
    yield ("pipeline_stage", {"stage": "planning", "status": "thinking"})

    planning_messages = [
        {"role": "system", "content": planning_system
         + "\n\nYou MUST output valid JSON matching this schema:\n"
         + json.dumps(planning_schema, indent=2)},
    ] + mode_messages + [
        {"role": "user", "content": user_content}
    ]

    planning_config = StageConfig(
        name=f"{mode}_planning",
        reasoning_effort="medium",
        service_tier="auto",
        json_mode=True,
    )

    planning_result = run_pipeline_stage(
        _planning_provider, client, planning_config,
        planning_messages, username, project, chat_name
    )
    planning_result.provider = _planning_provider

    yield ("pipeline_stage", {"stage": "planning", "status": "complete"})

    planning_data = planning_result.parsed_json or {}
    reasoning_summaries = []
    if planning_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Planning] {planning_result.usage['reasoning']}")

    # ---- Backend Resolution (sequential with HP tracking) ----
    actions = planning_data.get("actions", [])
    resolved = {"results": [], "state_ops": []}
    if actions:
        def _clean_name(value):
            return value.strip() if isinstance(value, str) else ""

        try:
            _tar_stacks = int(tar_stacks)
        except (TypeError, ValueError, OverflowError):
            _tar_stacks = 0
        try:
            _alert_level = int(alert_level)
        except (TypeError, ValueError, OverflowError):
            _alert_level = 0

        # Separate initiative actions (resolve first for ordering)
        init_actions = [a for a in actions if a.get("type") == "initiative"]
        ambush_actions = [a for a in actions if a.get("type") == "ambush"]
        combat_actions = [a for a in actions if a.get("type") not in ("initiative", "ambush")]

        # Extract relationships/factions for bonus auto-computation
        _rels = game_state.get("relationships") if isinstance(game_state, dict) else None
        _facs = game_state.get("factions") if isinstance(game_state, dict) else None
        _relationship_actor_names = set((game_state.get("edgerunners") or {}).keys()) if isinstance(game_state, dict) else set()
        _round_participant_names = collect_relationship_present_names(
            actions=actions,
            combat=(pipeline_state or {}).get("combat") if isinstance(pipeline_state, dict) else None,
            character_states=(pipeline_state or {}).get("character_states") if isinstance(pipeline_state, dict) else None,
        )
        _relationship_context = build_relationship_context(
            actions=actions,
            relationship_owner=_clean_name(planning_data.get("current_player", "")),
            fallback_owner=_clean_name(((pipeline_state or {}).get("combat") or {}).get("current_turn", "")),
            relationship_actor_names=_relationship_actor_names,
            relationship_present_names=_round_participant_names,
        )

        # Resolve ambush first if present. TAR can only be consumed once across
        # the entire exchange, so carry the remaining stacks across phases.
        _phase_tar = _tar_stacks
        _er_states = game_state.get("edgerunners") if isinstance(game_state, dict) else None
        ambush_result = resolve_actions(
            ambush_actions,
            relationships=_rels,
            factions=_facs,
            tar_stacks=_phase_tar,
            alert_level=_alert_level,
            active_programs=active_programs,
            installed_hardware=installed_hardware,
            ice_status=ice_status,
            relationship_context=_relationship_context,
            edgerunner_states=_er_states,
            character_states=character_states,
            net_actions_remaining=net_actions_remaining,
        ) if ambush_actions else {"results": [], "state_ops": [], "tar_consumed": False}
        if ambush_result.get("tar_consumed"):
            _phase_tar = 0

        # Determine surprised combatants from ambush results
        _surprised_names = []
        for _ar in ambush_result.get("results", []):
            for _tr in _ar.get("results", []):
                if _tr.get("surprised"):
                    _surprised_names.append(_tr.get("target", ""))

        # Inject surprised list into initiative actions
        for _ia in init_actions:
            if _surprised_names:
                _ia["surprised"] = _surprised_names

        init_result = resolve_actions(
            init_actions,
            relationships=_rels,
            factions=_facs,
            tar_stacks=_phase_tar,
            alert_level=_alert_level,
            active_programs=active_programs,
            installed_hardware=installed_hardware,
            ice_status=ice_status,
            relationship_context=_relationship_context,
            edgerunner_states=_er_states,
            character_states=character_states,
            net_actions_remaining=net_actions_remaining,
        ) if init_actions else {"results": [], "state_ops": [], "tar_consumed": False}
        if init_result.get("tar_consumed"):
            _phase_tar = 0

        # Sort combat_actions by initiative order if available
        if init_result["results"]:
            _init_order = init_result["results"][0].get("order", [])
            _order_map = {e["name"]: i for i, e in enumerate(_init_order)}
            combat_actions.sort(key=lambda a: _order_map.get(a.get("character", ""), 999))

        # Extract combatant HP from game_state (PCs) and character_states (enemies)
        _combatant_hp = {}
        if game_state and isinstance(game_state, dict):
            for _er_name, _er_data in game_state.get("edgerunners", {}).items():
                _hp = _er_data.get("hp", {})
                if isinstance(_hp, dict) and "current" in _hp:
                    _combatant_hp[_er_name] = _hp["current"]
        if character_states and isinstance(character_states, dict):
            for _cs_name, _cs_entry in character_states.items():
                if _cs_name in _combatant_hp:
                    continue  # PC already tracked via edgerunners
                _d = _cs_entry.get("data", _cs_entry) if isinstance(_cs_entry, dict) else {}
                for _v in _d.get("vitals", []):
                    if _v.get("label") == "HP" and "current" in _v:
                        _combatant_hp[_cs_name] = _v["current"]
                        break

        # Extract vehicle SDP/SP from combat state for sequential tracking
        _combatant_vehicle_sdp = {}
        _raw_combat = pipeline_state.get("combat") if isinstance(pipeline_state, dict) else None
        _combat_vehicles = _raw_combat.get("vehicles", {}) if isinstance(_raw_combat, dict) else {}
        if isinstance(_combat_vehicles, dict):
            for _vn, _vd in _combat_vehicles.items():
                if not isinstance(_vd, dict):
                    continue
                # Include destroyed vehicles too so resolve_actions can emit skipped
                # results with narration-visible reasons/notifications.
                _is_destroyed = _vd.get("status") == "destroyed"
                try:
                    _sdp_current = int(_vd.get("sdp_current", 0))
                except (TypeError, ValueError, OverflowError):
                    _sdp_current = 0
                try:
                    _sp_current = int(_vd.get("sp", 0))
                except (TypeError, ValueError, OverflowError):
                    _sp_current = 0
                _combatant_vehicle_sdp[_vn] = 0 if _is_destroyed else max(0, _sdp_current)
                _combatant_vehicle_sdp[_vn + ":sp"] = max(0, _sp_current)

        # Resolve combat actions sequentially with HP and vehicle SDP tracking
        combat_resolved = resolve_actions(
            combat_actions, relationships=_rels, factions=_facs,
            sequential=True, combatant_hp=_combatant_hp,
            combatant_vehicle_sdp=_combatant_vehicle_sdp,
            tar_stacks=_phase_tar, alert_level=_alert_level,
            active_programs=active_programs, installed_hardware=installed_hardware,
            ice_status=ice_status,
            relationship_context=_relationship_context,
            edgerunner_states=_er_states,
            character_states=character_states,
            net_actions_remaining=net_actions_remaining,
            slide_used_this_turn=slide_used_this_turn,
            system_map=system_map,
            current_node=current_node,
            virus_ledger=virus_ledger,
            stealth_active=stealth_active,
            quiet_jack_in_used=quiet_jack_in_used,
            stealth_broken_round=stealth_broken_round,
            net_round=net_round,
        ) if combat_actions else {"results": [], "state_ops": [], "tar_consumed": False}

        # Merge all results
        resolved = {
            "results": ambush_result["results"] + init_result["results"] + combat_resolved["results"],
            "state_ops": ambush_result["state_ops"] + init_result["state_ops"] + combat_resolved["state_ops"],
        }

    # ---- STAGE 2: Narration (streaming) ----
    yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})

    # Build narration input: planning output + resolved actions
    narration_input = {
        "planning": planning_data,
        "resolved_actions": resolved["results"],
        "state_ops": resolved["state_ops"],
    }

    narration_messages = [
        {"role": "system", "content": narration_system},
    ] + mode_messages + [
        {"role": "user", "content": json.dumps(narration_input, indent=2)}
    ]

    narration_config = StageConfig(
        name=f"{mode}_narration",
        reasoning_effort="low",
        service_tier="auto",
        json_mode=False,
    )

    narration_params = _narration_provider.build_pipeline_request(
        messages=narration_messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name=f"{mode}_narration",
        reasoning_effort=narration_config.reasoning_effort,
        service_tier=narration_config.service_tier,
        json_mode=False
    )

    narration_content = ""
    narration_usage = None
    first_content = True

    for stream_event in _narration_provider.send_request_stream(client, narration_params):
        if stream_event.event_type == 'content_delta':
            if first_content:
                yield ("pipeline_stage", {"stage": "narration", "status": "streaming"})
                first_content = False
            narration_content += stream_event.content
            yield ("content", {"delta": stream_event.content})
        elif stream_event.event_type == 'done':
            narration_usage = stream_event.usage
            narration_content = narration_content or (narration_usage or {}).get('content') or ''

    yield ("pipeline_stage", {"stage": "narration", "status": "complete"})

    # Aggregate usage
    stage_results = [planning_result]
    narration_stage_result = PipelineStageResult(
        stage=f"{mode}_narration",
        content=narration_content,
        parsed_json=None,
        usage=narration_usage or {},
        service_tier="standard",
        provider=_narration_provider,
    )
    stage_results.append(narration_stage_result)
    if narration_usage and narration_usage.get('reasoning'):
        reasoning_summaries.append(f"[Narration] {narration_usage['reasoning']}")

    aggregate = _aggregate_usage(stage_results, provider)

    yield ("pipeline_done", ModeResult(
        mode=mode,
        final_content=narration_content,
        planning_json=planning_data,
        resolved_actions=resolved["results"],
        state_ops=resolved["state_ops"],
        aggregate_usage=aggregate["usage"],
        aggregate_cost=aggregate["cost"],
        reasoning_summaries=reasoning_summaries,
        service_tier_label="standard",
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
        _stage_provider = sr.provider or provider
        result[sr.stage] = {
            "input_tokens": u.get('input_tokens', 0),
            "cache_read_tokens": u.get('cache_read_tokens', 0),
            "cache_creation_tokens": u.get('cache_creation_tokens', 0),
            "output_tokens": u.get('output_tokens', 0),
            "reasoning_tokens": u.get('reasoning_tokens', 0),
            "cost": _stage_provider.calculate_cost_with_tier(parsed, sr.service_tier),
            "service_tier": sr.service_tier,
            "model": getattr(_stage_provider, "MODEL_NAME", None) or getattr(_stage_provider, "model_id", None),
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

        # Calculate per-stage cost with tier-specific pricing.
        # Use the stage's own provider if set (mode pipeline runs Stage 1 on
        # one model and Stage 2 on another), else fall back to the aggregator's
        # default provider.
        parsed = ParsedResponse(
            content="",
            reasoning=None,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens
        )
        _stage_provider = result.provider or provider
        stage_cost = _stage_provider.calculate_cost_with_tier(parsed, result.service_tier)
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

    # Decision flags
    prev_df = prev.get("decision_flags", {})
    curr_df = curr.get("decision_flags", {})
    if curr_df != prev_df:
        df_changes = []
        for key in curr_df:
            if key not in prev_df:
                entry = curr_df[key]
                val = entry.get("value") if isinstance(entry, dict) else entry
                df_changes.append(f"  +{key} = {val}")
            elif curr_df[key] != prev_df[key]:
                entry = curr_df[key]
                val = entry.get("value") if isinstance(entry, dict) else entry
                df_changes.append(f"  {key} → {val}")
        for key in prev_df:
            if key not in curr_df:
                df_changes.append(f"  -{key}")
        if df_changes:
            parts.append("decision_flags:\n" + "\n".join(df_changes))

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

    def _append_hidden_ship_bootstrap(lines_out: list, msg_obj: dict):
        hidden_bootstrap = msg_obj.get("ship_combat_bootstrap_messages")
        if not hidden_bootstrap:
            # Fallback: inspect pipeline_state_after.ship_combat.bootstrap_messages if present
            ps_after = msg_obj.get("pipeline_state_after")
            if isinstance(ps_after, dict):
                hidden_bootstrap = (((ps_after.get("ship_combat") or {}) if isinstance(ps_after.get("ship_combat"), dict) else {}).get("bootstrap_messages"))
        if hidden_bootstrap:
            lines_out.append("--- HIDDEN SHIP COMBAT BOOTSTRAP MESSAGES ---")
            try:
                lines_out.append(json.dumps(hidden_bootstrap, indent=2))
            except TypeError:
                lines_out.append(str(hidden_bootstrap))
            lines_out.append("")

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
                        v_ops = events_parsed.get("virus_ops")
                        if v_ops:
                            ops_parts.append(f"virus_ops: {json.dumps(v_ops, indent=2)}")
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

                _append_hidden_ship_bootstrap(lines, msg)

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
                        state_after = state_after_raw if isinstance(state_after_raw, dict) else json.loads(state_after_raw)
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
                    if state_ops.get("virus_ops"):
                        ops_parts.append(f"virus_ops: {json.dumps(state_ops['virus_ops'], indent=2)}")
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

                _append_hidden_ship_bootstrap(lines, msg)

                # Mode handoff summaries
                _handoff_summary = msg.get("sex_handoff_summary")
                if _handoff_summary:
                    lines.append("--- SEX MODE HANDOFF SUMMARY ---")
                    lines.append(_handoff_summary)
                    lines.append("")

                lines.append("--- OUTPUT ---")
                lines.append(content)
                lines.append("")
            else:
                # Non-pipeline, non-stateful assistant message
                cost = msg.get("cost", "")
                lines.append(f"[ASSISTANT] {timestamp}  {cost}")
                _append_hidden_ship_bootstrap(lines, msg)

                # Mode handoff summaries
                _handoff_summary = msg.get("sex_handoff_summary")
                if _handoff_summary:
                    lines.append("--- SEX MODE HANDOFF SUMMARY ---")
                    lines.append(_handoff_summary)
                    lines.append("")

                # Sex scene exit summary
                _sex_exit = msg.get("sex_scene_summary")
                if _sex_exit:
                    lines.append("--- SEX SCENE EXIT SUMMARY ---")
                    lines.append(_sex_exit)
                    lines.append("")

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


def generate_plain_transcript(chat_data: dict, chat_path: str, chat_name: str) -> None:
    """
    Generate a plain-text transcript with only user/assistant dialogue.
    No metadata, no pipeline internals — just the conversation.
    """
    transcript_path = chat_path.replace(".json", "_transcript.txt")

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
    for msg in path:
        role = msg.get("role", "")
        if role == "system":
            continue

        content = msg.get("content", "")

        if role == "user":
            lines.append("[USER]")
            lines.append(content)
            lines.append("")
        elif role == "assistant":
            lines.append("[ASSISTANT]")
            lines.append(content)
            lines.append("")

    with open(transcript_path, "w", encoding="utf-8") as f:
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


def _parse_virus_section(lines: list) -> list:
    """Parse VIRUSES section lines into virus_ops list.

    Recognized formats:
      + "narrative" | target: X | planter: Y                   (plant)
      ACTIVATE #N: "log entry"                                  (activate)
      DISCOVER #N: "log entry"                                  (discover)
      PURGE #N: "log entry"                                     (purge)
      LOG #N: "entry text"                                      (log append)
      UPDATE #N target: "X"  /  UPDATE #N narrative: "X"        (correction)
    """
    import re
    ops = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("+"):
            # Plant: + "narrative" | target: X | planter: Y
            text_match = re.search(r'"([^"]*)"', line)
            narrative = text_match.group(1) if text_match else line[1:].strip()
            target = ""
            tgt_match = re.search(r'\|\s*target:\s*([^|]+)', line, re.IGNORECASE)
            if tgt_match:
                target = tgt_match.group(1).strip()
            planter = ""
            plt_match = re.search(r'\|\s*planter:\s*([^|]+)', line, re.IGNORECASE)
            if plt_match:
                planter = plt_match.group(1).strip()
            if not target:
                # Skip malformed plants — apply_virus_ops would warn anyway
                continue
            ops.append({
                "action": "plant",
                "target": target[:200],
                "planter": planter[:80] or "unknown",
                "narrative": narrative[:800]
            })
            continue

        upper = line.upper()
        for keyword, action_name in (("ACTIVATE", "activate"), ("DISCOVER", "discover"),
                                       ("PURGE", "purge"), ("LOG", "log"), ("UPDATE", "update")):
            if upper.startswith(keyword):
                id_match = re.search(r'#(\d+)', line)
                if not id_match:
                    break
                vid = int(id_match.group(1))
                if action_name in ("activate", "discover", "purge"):
                    text_match = re.search(r'"([^"]*)"', line)
                    op = {"action": action_name, "id": vid}
                    if text_match:
                        op["log"] = text_match.group(1)[:400]
                    ops.append(op)
                elif action_name == "log":
                    text_match = re.search(r'"([^"]*)"', line)
                    if text_match:
                        ops.append({"action": "log", "id": vid, "entry": text_match.group(1)[:400]})
                elif action_name == "update":
                    fields = {}
                    for field_name in ("target", "planter", "narrative"):
                        m = re.search(rf'{field_name}:\s*"([^"]*)"', line, re.IGNORECASE)
                        if m:
                            fields[field_name] = m.group(1)[:800]
                    if fields:
                        ops.append({"action": "update", "id": vid, "fields": fields})
                break
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


def _parse_plot_section(lines: list) -> list:
    """Parse PLOT section lines into plot_ops list.

    Supports pipe-delimited format:
        decision text | key=value | severity | episode
    Also supports simpler formats with fewer fields.
    """
    ops = []
    for line in lines:
        line = line.strip().lstrip("- ")
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        op = {"decision": parts[0]}
        for part in parts[1:]:
            if "=" in part:
                k, _, v = part.partition("=")
                k = k.strip().lower()
                v = v.strip()
                if k == "key":
                    op["key"] = v if v.lower() != "null" else None
                elif k == "value":
                    op["value"] = v if v.lower() != "null" else None
                elif k == "severity":
                    op["severity"] = v
                elif k == "episode":
                    op["episode"] = v
                else:
                    op["key"] = k
                    op["value"] = v
            else:
                part_lower = part.lower()
                if part_lower in ("branch", "flag", "divergence"):
                    op["severity"] = part_lower
                else:
                    op["episode"] = part
        ops.append(op)
    return ops


def parse_state_updates_block(text: str, current_turn: int) -> dict:
    """
    Parse the [STATE UPDATES] block text into ops compatible with existing apply_* functions.

    Returns dict with keys: pacing, callback_ops, npc_memory_ops, scene_state, character_states.
    Each value is None if that section was not present.
    """
    result = {
        "pacing": None,
        "callback_ops": None,
        "virus_ops": None,
        "npc_memory_ops": None,
        "plot_ops": None,
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
        "PLOT": "PLOT", "PLOT OPS": "PLOT",
        "VIRUSES": "VIRUSES", "VIRUS LEDGER": "VIRUSES", "VIRUS OPS": "VIRUSES",
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
    if "VIRUSES" in section_lines:
        result["virus_ops"] = _parse_virus_section(section_lines["VIRUSES"])
    if "MEMORIES" in section_lines:
        result["npc_memory_ops"] = _parse_memories_section(section_lines["MEMORIES"])
    if "SCENE" in section_lines:
        result["scene_state"] = _parse_scene_section(section_lines["SCENE"])
    if "CHARACTERS" in section_lines:
        result["character_states"] = _parse_characters_section(section_lines["CHARACTERS"])
    if "PLOT" in section_lines:
        result["plot_ops"] = _parse_plot_section(section_lines["PLOT"])

    return result


def _replace_combat_dict(pipeline_state: dict, new_combat) -> None:
    """Replace pipeline_state['combat'] while preserving backend-owned keys.

    The model's combat dict only contains initiative data (round, initiative_order,
    current_turn). Backend-owned keys like vehicles, cover, context, and
    start_message_id must survive replacement. Any new backend-owned key added
    here is automatically preserved at ALL replacement sites.
    """
    replace_combat_dict_preserving_backend_keys(pipeline_state, new_combat)


def _bump_beat_response_counter(pipeline_state: dict) -> None:
    """Increment beat_state.beat_responses by 1. Called once per
    in-character turn (i.e. when turn_counter increments). The counter
    resets to 0 when the beat advances via /beat, /session, or planner
    signal. The pipeline mirrors it into pacing.responses every turn so
    the sidebar shows a true per-beat counter rather than the model's
    drift-prone total-since-session-start emit.
    """
    from game_systems.plot_beats import normalize_beat_state
    bs = normalize_beat_state(pipeline_state.get("beat_state"))
    bs["beat_responses"] = int(bs.get("beat_responses", 0) or 0) + 1
    # session_responses bumps alongside but doesn't reset on beat advance —
    # only on session rollover. Sidebar reads this for "(Y total)".
    bs["session_responses"] = int(bs.get("session_responses", 0) or 0) + 1
    pipeline_state["beat_state"] = bs


def _apply_canonical_pacing(pipeline_state: dict, uploads_dir: Optional[str]) -> None:
    """Override pacing.episode and pacing.beat with values parsed from the
    plot doc for the active session/beat, and mirror beat_state.beat_responses
    into pacing.responses (the sidebar's "Responses" counter). The model
    is allowed to emit any of these (the schema accepts them) but they're
    treated as advisory — the backend decides the canonical values.

    Called every turn after the model's pacing has been merged in, so any
    drift the model introduced is corrected before the state is saved.
    """
    pacing = pipeline_state.get("pacing")
    if not isinstance(pacing, dict):
        pacing = {}
        pipeline_state["pacing"] = pacing

    # Mirror the backend-tracked per-beat counter into pacing.responses.
    # Done unconditionally — this counter is correct even when no plot
    # doc exists (it just counts in-character turns since the last beat
    # advance, which defaults to since-chat-start).
    bs = pipeline_state.get("beat_state")
    if isinstance(bs, dict) and "beat_responses" in bs:
        try:
            pacing["responses"] = int(bs["beat_responses"])
        except (TypeError, ValueError):
            pass

    if not uploads_dir:
        return
    canonical = derive_canonical_pacing(bs or {}, uploads_dir)
    if not canonical:
        return
    if "episode" in canonical:
        pacing["episode"] = canonical["episode"]
    if "beat" in canonical:
        pacing["beat"] = canonical["beat"]


def apply_single_agent_state_updates(pipeline_state: dict, parsed: dict, current_turn: int, game_system: dict = None, uploads_dir: Optional[str] = None) -> dict:
    """Apply parsed state updates to pipeline_state using existing apply_* functions."""
    if not isinstance(parsed, dict):
        return pipeline_state
    if isinstance(parsed.get("pacing"), dict):
        pipeline_state["pacing"] = {**pipeline_state.get("pacing", {}), **parsed["pacing"]}
    _apply_canonical_pacing(pipeline_state, uploads_dir)
    if parsed.get("callback_ops"):
        pipeline_state["callback_ledger"] = apply_callback_ops(
            pipeline_state["callback_ledger"],
            parsed["callback_ops"],
            current_turn
        )
    if parsed.get("virus_ops"):
        pipeline_state["virus_ledger"] = apply_virus_ops(
            pipeline_state.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
            parsed["virus_ops"],
            current_turn,
            pipeline_state.get("hud_state", {}).get("date", "")
        )
    # Scene-scope filtering: drop memory/relationship ops for NPCs not in scene
    # Mutates parsed in-place so downstream notification extraction also sees filtered ops
    if parsed.get("npc_memory_ops") or parsed.get("relationship_ops"):
        filter_ops_by_scene_scope(parsed, pipeline_state.get("scene_state", {}))
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
    if parsed.get("plot_ops"):
        pipeline_state["decision_flags"] = apply_decision_flags(
            pipeline_state.get("decision_flags", {}),
            parsed["plot_ops"],
            canonical_flags=parse_canonical_flags(uploads_dir),
        )
    pipeline_state["character_states"] = apply_character_states(
        pipeline_state["character_states"],
        parsed.get("character_states") if isinstance(parsed.get("character_states"), dict) else {},
        current_turn,
        scene_state=parsed.get("scene_state") or pipeline_state.get("scene_state"),
    )
    # Apply game-specific state ops (relationship_ops already filtered by scene scope)
    if game_system and game_system.get("apply_game_state"):
        if "game_state" not in pipeline_state:
            pipeline_state["game_state"] = game_system["init_game_state"]()
        game_system["apply_game_state"](pipeline_state["game_state"], parsed, current_turn)
    # Persist HUD state from tool report.
    # Backend-driven clock: accept an initial seed once, then advance only by deltas.
    if "hud_state" in parsed or isinstance(pipeline_state.get("hud_state"), dict):
        is_ooc = parsed.get("is_ooc", False)
        raw_hud = parsed.get("hud_state")
        incoming_hud = raw_hud if isinstance(raw_hud, dict) else None
        time_override = (incoming_hud or {}).get("time_override")
        if time_override and isinstance(time_override, dict):
            try:
                override_minutes = float(time_override.get("minutes", 0) or 0)
            except (TypeError, ValueError):
                override_minutes = 0
            tp_seconds = int(override_minutes * 60) if override_minutes > 0 else DEFAULT_TURN_SECONDS
            override_reason = time_override.get("reason", "")
        else:
            tp_seconds = DEFAULT_TURN_SECONDS
            override_reason = None
        # Snapshot pre-call notifications so we can detect whether the persist call
        # itself queued an implicit-advance notification (in which case we skip the
        # explicit one here to avoid duplicates).
        _notif_count_before = len(pipeline_state.get("_pending_time_notifications", []))
        advanced_clock = _persist_hud_state_with_backend_clock(
            pipeline_state,
            incoming_hud,
            seconds=tp_seconds,
            is_ooc=is_ooc,
        )
        _implicit_fired = len(pipeline_state.get("_pending_time_notifications", [])) > _notif_count_before
        if advanced_clock and tp_seconds != DEFAULT_TURN_SECONDS and override_reason is not None and not _implicit_fired:
            pipeline_state.setdefault("_pending_time_notifications", []).append({
                "type": "time_passed",
                "duration": _format_duration(tp_seconds),
                "reason": override_reason or "",
            })
    # Sync CPRED character_states + HUD funds from authoritative game_state.
    # Runs AFTER model's hud_state is merged/scoped so game_state overrides stale values.
    if game_system and game_system.get("id") == "cpred":
        _rebuild_cpred_projections(pipeline_state, current_turn)
    elif isinstance(pipeline_state.get("hud_state"), dict):
        # Always refresh HUD projection when hud_state exists, even if the model
        # omitted hud_state this turn — clock advanced so funds/scoping must stay current.
        pipeline_state["hud_state"] = derive_funds_from_ship_credits(
            pipeline_state.get("hud_state", {}),
            pipeline_state.get("game_state", {}),
        )
        pipeline_state["hud_state"] = scope_hud_funds(
            pipeline_state.get("hud_state", {}),
            pipeline_state.get("scene_state", {}),
            pipeline_state.get("character_states", {}),
        )
    # Persist combat state (initiative tracker) from tool report
    if "combat" in parsed:
        _replace_combat_dict(pipeline_state, parsed["combat"])
    # Persist sex_scene state from tool report (never allow tool to null it out —
    # sex_scene is ended only by [SCENE COMPLETE] detection or the /sex endpoint)
    if "sex_scene" in parsed and parsed["sex_scene"]:
        pipeline_state["sex_scene"] = parsed["sex_scene"]
    return pipeline_state


def build_player_agency_reminder(user_message: str, character_states: dict) -> str:
    """Build a dynamic player-agency reminder for multi-PC campaigns.

    Detects which PC is prompting from the [tag] at the start of the message,
    then lists all other PCs as off-limits. Returns empty string for:
    - Single-player campaigns (0 or 1 PC)
    - Messages without a recognized player tag
    - OOC messages
    """
    # Collect all PCs from character_states
    pcs = []
    for name, cs in character_states.items():
        data = cs.get("data", cs)  # handle both {data: {type:...}} and flat {type:...}
        if data.get("type") == "pc":
            pcs.append(name)
    if len(pcs) <= 1:
        return ""

    # Parse [tag] from start of message
    match = re.match(r'\s*\[([^\]]+)\]', user_message)
    if not match:
        return ""
    tag = match.group(1).strip()

    # Skip OOC tags
    if tag.upper() in ("OOC",) or tag.upper().startswith("OOC:") or tag.upper().startswith("OOC "):
        return ""

    # Match tag to a PC name (fuzzy: [A] matches "Aedina", [Aedina] matches "Aedina Lumenvale")
    prompting_pc = None
    for pc_name in pcs:
        first_name = pc_name.split()[0]
        if first_name.lower().startswith(tag.lower()) or tag.lower().startswith(first_name.lower()):
            prompting_pc = pc_name
            break

    if not prompting_pc:
        return ""

    # Build reminder listing off-limits PCs
    off_limits = [name for name in pcs if name != prompting_pc]
    if not off_limits:
        return ""

    off_limits_names = ", ".join(off_limits)
    prompting_first = prompting_pc.split()[0]

    return (
        f"⚠️ PLAYER AGENCY: [{prompting_first}] is prompting. "
        f"Do NOT author {off_limits_names} — no actions, speech, thoughts, feelings, "
        f"body language, or physical reactions. Describe only the world and NPC reactions, "
        f"then prompt the next player."
    )


def build_single_agent_injections(pipeline_state: dict, game_system: dict = None, dice_pool: str = "", doc_file_stems: set = None, name_dice: str = "") -> str:
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

    # 2b. Virus ledger (planted viruses, persistent across sessions)
    virus = build_virus_ledger_injection(pipeline_state.get("virus_ledger", {}))
    if virus:
        injections.append(virus)

    # 3. Decision flags (persistent plot decisions / branch points)
    df = build_decision_flags_injection(pipeline_state.get("decision_flags", {}))
    if df:
        injections.append(df)

    # 4. NPC memories (scene-scoped)
    mem = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem:
        injections.append(mem)

    # 5. Scene state
    scene = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    injections.append(scene)

    # 5. Character states (scene-scoped)
    cs = build_character_states_injection(
        pipeline_state.get("character_states", {}),
        pipeline_state.get("scene_state", {}))
    injections.append(cs)

    # 5a. NPC voices (scene-scoped, improvised NPCs only)
    voices = build_npc_voices_injection(
        pipeline_state.get("character_states", {}),
        pipeline_state.get("scene_state", {}),
        doc_file_stems)
    if voices:
        injections.append(voices)

    # 5b. Character features (game-system specific, e.g. subclass features from conversion doc)
    if game_system and game_system.get("build_features_injection"):
        feat_inj = game_system["build_features_injection"](
            pipeline_state.get("character_states", {}),
            pipeline_state.get("game_state", {}))
        if feat_inj:
            injections.append(feat_inj)

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
        game_injection = game_system["build_game_injection"](pipeline_state.get("game_state", {}), pipeline_state.get("scene_state"))
        if game_injection:
            injections.append(game_injection)

    # 8. Dice pool (always last — model consumes these for rolls)
    if dice_pool:
        injections.append(dice_pool)

    # 9. Name dice (pre-rolled for name generator doc, if present)
    if name_dice:
        injections.append(name_dice)

    return "\n\n".join(injections) if injections else ""


def extract_state_notifications(ops_source: dict, npcs_present: set = None,
                                old_character_states: dict = None) -> list:
    """Extract user-visible notifications from relationship_ops and npc_memory_ops.

    Returns a list of notification dicts for the frontend to display.
    Skips bootstrap ops (set/npc_set) and drop actions — only meaningful changes.
    If npcs_present is provided, filters out ops targeting NPCs not in the scene.
    If old_character_states is provided (name → old voice string or None),
    emits voice_update notifications when voice is set or changed.
    """
    notifications = []

    for op in ops_source.get("relationship_ops", []):
        op_type = op.get("op")
        if op_type in ("set", "npc_set"):
            continue
        # Scene-scope filter for notifications (when called from pipeline path
        # where ops_source is re-parsed from raw JSON, not the filtered copy)
        if npcs_present is not None and op_type in ("rs", "roms", "npc_rs", "npc_roms"):
            if op.get("target") not in npcs_present:
                continue
        if op_type in ("rs", "roms", "fr"):
            notif = {
                "type": f"{op_type}_change",
                "target": op.get("target"),
                "change": op.get("change"),
                "new_total": op.get("new_total"),
                "reason": op.get("reason"),
            }
            if "tier_transition" in op:
                notif["tier_transition"] = op["tier_transition"]
            notifications.append(notif)
        elif op_type in ("npc_rs", "npc_roms"):
            notif = {
                "type": f"{op_type}_change",
                "target": op.get("target"),
                "other": op.get("other"),
                "change": op.get("change"),
                "new_total": op.get("new_total"),
                "reason": op.get("reason"),
            }
            if "tier_transition" in op:
                notif["tier_transition"] = op["tier_transition"]
            notifications.append(notif)

    for op in ops_source.get("npc_memory_ops", []):
        if op.get("action") != "add":
            continue
        # Scene-scope filter for memory notifications
        if npcs_present is not None and op.get("npc") not in npcs_present:
            continue
        notifications.append({
            "type": "npc_memory",
            "npc": op.get("npc"),
            "text": op.get("text"),
            "quote": op.get("quote"),
            "impact": op.get("impact"),
        })

    for op in ops_source.get("plot_ops", []):
        notifications.append({
            "type": "plot_decision",
            "key": op.get("key"),
            "value": op.get("value"),
            "decision": op.get("decision"),
            "severity": op.get("severity"),
            "episode": op.get("episode"),
        })

    # Voice profile updates for NPCs/enemies
    if old_character_states is not None:
        for name, cs_entry in ops_source.get("character_states", {}).items():
            cs_data = cs_entry.get("data", cs_entry) if isinstance(cs_entry, dict) else {}
            new_voice = cs_data.get("voice")
            if new_voice and cs_data.get("type") in ("npc", "enemy"):
                old_voice = old_character_states.get(name)
                if new_voice != old_voice:
                    notifications.append({
                        "type": "voice_update",
                        "npc": name,
                        "voice": new_voice,
                        "old_voice": old_voice,
                    })

    return notifications


def extract_character_agent_notifications(ops: dict) -> list:
    """Surface Characters character_agent ops as user-visible banners.

    The character_agent silently mutates state (memory file, callbacks,
    arc_state, user_profile, growth) on most turns. Without these banners
    the user has no way to see what the agent captured — they have to
    refresh and dig through the JSON. We notify on the meaningful ADD
    actions and arc_state shifts; quieter ops (checkin, update timestamps,
    obsolete marks) stay silent so we don't spam the chat.
    """
    if not isinstance(ops, dict):
        return []
    notifications = []

    for op in ops.get("memory_ops", []) or []:
        if op.get("action") != "add":
            continue
        notifications.append({
            "type": "character_memory",
            "text": op.get("text"),
            "quote": op.get("quote"),
            "impact": op.get("impact"),
            "focus": op.get("focus"),
        })

    for op in ops.get("callback_ops", []) or []:
        action = op.get("action")
        if action == "add":
            notifications.append({
                "type": "character_callback_added",
                "text": op.get("original_text") or op.get("text"),
                "due_by": op.get("due_by"),
                "source": op.get("source"),
            })
        elif action == "resolve":
            notifications.append({
                "type": "character_callback_resolved",
                "id": op.get("id"),
                "reason": op.get("resolution_text") or op.get("reason"),
            })

    arc_op = ops.get("arc_state_op")
    if isinstance(arc_op, dict) and arc_op.get("action") == "set":
        value = (arc_op.get("value") or "").strip()
        if value:
            notifications.append({
                "type": "character_arc_state",
                "value": value,
            })

    for op in ops.get("profile_ops", []) or []:
        if op.get("action") != "add":
            continue
        notifications.append({
            "type": "character_user_profile",
            "text": op.get("text"),
            "category": op.get("category"),
        })

    for op in ops.get("growth_ops", []) or []:
        if op.get("action") != "add":
            continue
        notifications.append({
            "type": "character_growth",
            "text": op.get("text"),
            "category": op.get("category"),
        })

    return notifications


def extract_ship_combat_notifications(ship_combat_tool_input: dict) -> list:
    """Convert ship combat npc_actions into user-visible state notification entries."""
    notifications = []
    for action in (ship_combat_tool_input or {}).get("npc_actions", []) or []:
        notifications.append({
            "type": "ship_npc_action",
            "ship_name": action.get("ship_name"),
            "role": action.get("role"),
            "character_name": action.get("character_name"),
            "action": action.get("action"),
            "effect": action.get("effect"),
        })
    return notifications
