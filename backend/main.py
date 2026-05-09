"""
ChatGPT Web Interface - Backend API
"""

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
from pathlib import Path
from contextlib import contextmanager
import asyncio
import os
import json
import re
import shutil
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
import tiktoken
import logging
import logging.handlers
import fcntl
import uuid
import hashlib
import copy
import inspect
import threading

# Provider imports
from providers import ProviderRegistry, ModelProvider
from providers.openai_provider import OpenAIProvider, OpenAI54Provider, OpenAI55Provider
from providers.anthropic_provider import AnthropicProvider, AnthropicOpus45Provider, AnthropicOpusProvider, AnthropicOpus3Provider
from combat_state import replace_combat_dict_preserving_backend_keys

# Real-time sync imports
from sync_manager import sync_manager, SyncEvent, SyncEventType

# Pipeline imports
from pipeline import (
    run_pipeline, run_mode_pipeline, PipelineResult, ModeResult, generate_debug_transcript,
    generate_plain_transcript, apply_single_agent_state_updates,
    build_single_agent_injections, build_player_agency_reminder,
    generate_dice_pool, generate_name_dice,
    migrate_pipeline_state,
    get_context_pairs, _filter_unstaged_pairs, extract_state_notifications, extract_ship_combat_notifications,
    collapse_hack_messages,
    collapse_combat_messages,
    collapse_ship_combat_messages,
    collapse_net_combat_messages,
    collapse_chase_messages,
    collapse_sex_messages,
    SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS,
    _persist_hud_state_with_backend_clock,
    _advance_hud_clock,
    _format_duration,
    _bump_beat_response_counter,
    _rebuild_cpred_projections,
)
from game_systems import get_game_system, list_game_systems, DEFAULT_GAME_SYSTEM
from game_systems.cpred_identity import (
    build_relationship_context,
    collect_relationship_present_names,
    state_op_has_subject_kind,
    state_op_subject,
    state_op_subject_name,
)

# Pipeline agent names (used for per-agent instructions and file routing)
PIPELINE_AGENT_NAMES = ["events", "mechanics", "narration"]

# Configure logging for debugging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# File handler so logs are readable by tooling (rotates at 5MB, keeps 2 backups)
_log_dir = os.path.join(os.path.dirname(__file__), "..", "data", "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_log_dir, "backend.log"), maxBytes=5_000_000, backupCount=2, encoding="utf-8"
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)


def _advance_mode_hud_clock(pipeline_state: dict, seconds: Optional[int]) -> None:
    """Advance mode clocks by fixed round duration. No model input needed."""
    if not seconds or not isinstance(pipeline_state, dict):
        return
    _persist_hud_state_with_backend_clock(
        pipeline_state,
        None,
        seconds=seconds,
        replace_snapshot=False,
    )


def _hud_for_done_event(assistant_msg_data: dict, live_pipeline_state: dict | None = None) -> dict | None:
    """Pull the hud_state slice for the SSE `done` event so the frontend can
    stamp the new message's per-message timestamp header
    (ChatView.getMessageHudLine) immediately, without waiting for a page
    reload to fetch the saved pipeline_state_after.

    Source-of-truth precedence (matches what the sidebar reads):
    1. `live_pipeline_state["hud_state"]` if provided — this is the post-pipeline
       state the sidebar shows. Always preferred so the message stamp matches the
       sidebar at the moment of save.
    2. `assistant_msg_data["pipeline_state_after"]["hud_state"]` as fallback for
       paths that haven't wired live_pipeline_state through yet.

    Without (1), there are paths (e.g., stateful turns where
    `pipeline_state_after` hasn't been populated yet, or mode handoffs that
    don't snapshot the full state) where the stamp falls back to walk-back in
    `ChatView.getMessageHudLine` and shows a stale prior message's HUD.
    """
    import copy
    if isinstance(live_pipeline_state, dict):
        live_hud = live_pipeline_state.get("hud_state")
        if isinstance(live_hud, dict):
            return copy.deepcopy(live_hud)
    if not isinstance(assistant_msg_data, dict):
        return None
    psa = assistant_msg_data.get("pipeline_state_after")
    if not isinstance(psa, dict):
        return None
    hud = psa.get("hud_state")
    return copy.deepcopy(hud) if isinstance(hud, dict) else None


def _stamp_message_hud_snapshot(assistant_msg_data: dict, pipeline_state_source: dict | None) -> None:
    """Ensure an assistant message carries a hud_state snapshot for the
    frontend per-message timestamp header (ChatView.getMessageHudLine).

    Stateful normal-mode messages get a full pipeline_state_after via the
    standard save path; mode messages (sex / hack / combat / net_combat /
    ship_combat) don't, but the live `pipeline_state.hud_state` is current
    by the time we save (mode dispatchers call `_advance_mode_hud_clock`
    before save). This snapshots just the hud_state slice into
    pipeline_state_after so the frontend can read date/time/location.

    Idempotent — won't overwrite an existing pipeline_state_after that
    already has hud_state populated.
    """
    import copy
    if not isinstance(assistant_msg_data, dict):
        return
    src = pipeline_state_source if isinstance(pipeline_state_source, dict) else {}
    hud = src.get("hud_state")
    if not isinstance(hud, dict):
        return
    psa = assistant_msg_data.get("pipeline_state_after")
    if isinstance(psa, dict):
        if isinstance(psa.get("hud_state"), dict):
            return  # already populated
        psa["hud_state"] = copy.deepcopy(hud)
    else:
        assistant_msg_data["pipeline_state_after"] = {"hud_state": copy.deepcopy(hud)}

# ============================================================
# Configuration Constants
# ============================================================

# File storage
DATA_DIR = Path("/home/chatgpt/data/users")

# Context window management
CONTEXT_WINDOW_THRESHOLD = 275_000  # Start trimming when exceeding this
CONTEXT_WINDOW_TARGET = 225_000     # Trim down to this target

# Free token allowance
FREE_TOKENS_PER_DAY = 250_000

# Output limits
MAX_OUTPUT_TOKENS_FREE_CHAT = 1200  # Only applies to non-project chats

# Model configuration
MODEL_NAME = "gpt-5.2"
PROMPT_CACHE_RETENTION = "24h"

# Pricing (per million tokens) - DEPRECATED: Now managed by providers
PRICING = {
    "input_new": 1.75,
    "input_cached": 0.175,
    "output": 14.0,
    "reasoning": 14.0,
}

# ============================================================
# Provider Registration
# ============================================================

# Register available model providers
ProviderRegistry.register(OpenAIProvider())
ProviderRegistry.register(OpenAI54Provider())
ProviderRegistry.register(OpenAI55Provider())
ProviderRegistry.register(AnthropicProvider())
ProviderRegistry.register(AnthropicOpus45Provider())
ProviderRegistry.register(AnthropicOpusProvider())
ProviderRegistry.register(AnthropicOpus3Provider())

# Default model for new chats
DEFAULT_MODEL = "claude-3-opus"

# Model used for auto-switching during combat/hack/net_combat/ship_combat
COMBAT_AUTO_SWITCH_MODEL = "gpt-5.4"

# Per-stage models for both pipelines.
# Reasoning stages (Events / Mechanics-as-model / Planning) are JSON-only with
# heavy rules reasoning — 5.2's strength. Narration is streaming prose with
# light reasoning — 5.4's strength. These override the caller-picked model
# inside the pipelines but only when both stage providers are registered.
PIPELINE_PLANNING_MODEL = "gpt-5.2"
PIPELINE_NARRATION_MODEL = "gpt-5.4"


def get_default_model_for_user(username: str) -> str:
    """Return claude-3-opus if user has Anthropic key, else gpt-5.4 if OpenAI key."""
    if get_api_key(username, "anthropic"):
        return DEFAULT_MODEL  # claude-3-opus
    if get_api_key(username, "openai"):
        return "gpt-5.4"
    return DEFAULT_MODEL

# ============================================================
# Utility Functions
# ============================================================

# Cache the tiktoken encoder for performance (avoid creating new instance on every call)
_token_encoder = None


def get_token_encoder():
    """Get cached tiktoken encoder instance"""
    global _token_encoder
    if _token_encoder is None:
        _token_encoder = tiktoken.get_encoding("cl100k_base")
    return _token_encoder


def get_claude_provider():
    """Get any Claude provider for token counting (they use the same tokenizer)."""
    return ProviderRegistry.get("claude-sonnet-4.5") or ProviderRegistry.get("claude-opus-4.5")


def get_gpt_provider():
    """Get any GPT provider for token counting (they use the same tokenizer)."""
    return ProviderRegistry.get("gpt-5.4") or ProviderRegistry.get("gpt-5.5") or ProviderRegistry.get("gpt-5.2")


@contextmanager
def file_lock(path: str, exclusive: bool = True):
    """
    Context manager for file locking to prevent race conditions.
    Uses fcntl for POSIX file locking.

    Args:
        path: Path to the file to lock (creates .lock file)
        exclusive: True for write lock, False for read lock
    """
    lock_path = path + '.lock'
    lock_file = open(lock_path, 'w')
    try:
        if exclusive:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def atomic_write_json(path: str, data: dict, indent: int = 2) -> None:
    """
    Atomically write JSON data to a file using write-to-temp-then-rename.
    This prevents file corruption if the process crashes mid-write.

    Args:
        path: Destination file path
        data: Dictionary to serialize as JSON
        indent: JSON indentation (default 2)
    """
    # Use unique temp file name to prevent race conditions when multiple
    # requests try to update the same file simultaneously
    temp_path = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
    try:
        with open(temp_path, 'w') as f:
            json.dump(data, f, indent=indent)
        os.replace(temp_path, path)  # Atomic on POSIX systems
    except Exception:
        # Clean up temp file if write/rename failed
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# Helper Functions
# ============================================================

def add_chat_prefix(chat_name: str) -> str:
    """Add chat_ prefix if not already present"""
    return chat_name if chat_name.startswith('chat_') else f'chat_{chat_name}'

def remove_chat_prefix(chat_name: str) -> str:
    """Remove chat_ prefix if present"""
    return chat_name.removeprefix('chat_')

def get_user_dir(username: str) -> str:
    return os.path.join(DATA_DIR, username)

def ensure_user_exists(username: str) -> bool:
    user_dir = get_user_dir(username)
    is_new = not os.path.exists(user_dir)
    
    if is_new:
        os.makedirs(user_dir)
        os.makedirs(os.path.join(user_dir, "projects"))
    
    return is_new

def get_api_keys_path(username: str) -> str:
    """Get path to the API keys JSON file."""
    return os.path.join(get_user_dir(username), "api_keys.json")

def load_api_keys(username: str) -> dict:
    """
    Load API keys from api_keys.json, with auto-migration from legacy api_key.txt.

    Returns dict like {"openai": "sk-...", "anthropic": "sk-ant-..."}
    """
    keys_path = get_api_keys_path(username)
    user_dir = get_user_dir(username)
    legacy_path = os.path.join(user_dir, "api_key.txt")

    # Check for existing api_keys.json
    if os.path.exists(keys_path):
        with open(keys_path, 'r') as f:
            return json.load(f)

    # Auto-migrate from legacy api_key.txt
    if os.path.exists(legacy_path):
        with open(legacy_path, 'r') as f:
            openai_key = f.read().strip()
        if openai_key:
            keys = {"openai": openai_key, "anthropic": ""}
            save_api_keys(username, keys)
            return keys

    return {"openai": "", "anthropic": ""}

def save_api_keys(username: str, keys: dict):
    """Save API keys to api_keys.json."""
    keys_path = get_api_keys_path(username)
    atomic_write_json(keys_path, keys)

def get_api_key(username: str, provider: str = "openai") -> str | None:
    """Get API key for a specific provider."""
    keys = load_api_keys(username)
    key = keys.get(provider, "")
    return key if key else None

def save_api_key(username: str, api_key: str):
    """Legacy function for backwards compatibility - saves OpenAI key."""
    keys = load_api_keys(username)
    keys["openai"] = api_key
    save_api_keys(username, keys)

def get_persistent_stats_path(username: str) -> str:
    return os.path.join(get_user_dir(username), "lifetime_stats.json")

def load_persistent_stats(username: str) -> dict:
    """Load lifetime stats that persist even when chats are deleted"""
    path = get_persistent_stats_path(username)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    
    # File doesn't exist - check if we need to migrate from existing chats
    user_dir = get_user_dir(username)
    if os.path.exists(user_dir):
        # One-time migration: aggregate from all existing chats
        migrated = {
            "total_prompts": 0,
            "total_input_tokens": 0,
            "total_cached_tokens": 0,
            "total_output_tokens": 0,
            "total_reasoning_tokens": 0,
            "total_cost": 0.0,
            "first_prompt_date": None
        }
        
        # Process root chats
        for f in os.listdir(user_dir):
            if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
                chat_path = os.path.join(user_dir, f)
                try:
                    with open(chat_path, 'r') as cf:
                        chat_data = json.load(cf)
                    stats = chat_data.get("stats", {})
                    migrated["total_prompts"] += stats.get("total_prompts", 0)
                    migrated["total_input_tokens"] += stats.get("total_input_tokens", 0)
                    migrated["total_cached_tokens"] += stats.get("total_cached_tokens", 0)
                    migrated["total_output_tokens"] += stats.get("total_output_tokens", 0)
                    migrated["total_reasoning_tokens"] += stats.get("total_reasoning_tokens", 0)
                    migrated["total_cost"] += stats.get("total_cost", 0.0)
                    first_prompt = stats.get("first_prompt_date")
                    if first_prompt:
                        if migrated["first_prompt_date"] is None or first_prompt < migrated["first_prompt_date"]:
                            migrated["first_prompt_date"] = first_prompt
                except Exception as e:
                    logger.warning(f"Failed to migrate stats from chat {f}: {e}")

        # Process project chats
        projects_dir = os.path.join(user_dir, "projects")
        if os.path.exists(projects_dir):
            for project in os.listdir(projects_dir):
                project_path = os.path.join(projects_dir, project)
                if os.path.isdir(project_path):
                    for f in os.listdir(project_path):
                        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
                            chat_path = os.path.join(project_path, f)
                            try:
                                with open(chat_path, 'r') as cf:
                                    chat_data = json.load(cf)
                                stats = chat_data.get("stats", {})
                                migrated["total_prompts"] += stats.get("total_prompts", 0)
                                migrated["total_input_tokens"] += stats.get("total_input_tokens", 0)
                                migrated["total_cached_tokens"] += stats.get("total_cached_tokens", 0)
                                migrated["total_output_tokens"] += stats.get("total_output_tokens", 0)
                                migrated["total_reasoning_tokens"] += stats.get("total_reasoning_tokens", 0)
                                migrated["total_cost"] += stats.get("total_cost", 0.0)
                                first_prompt = stats.get("first_prompt_date")
                                if first_prompt:
                                    if migrated["first_prompt_date"] is None or first_prompt < migrated["first_prompt_date"]:
                                        migrated["first_prompt_date"] = first_prompt
                            except Exception as e:
                                logger.warning(f"Failed to migrate stats from project chat {f}: {e}")
        
        # Save migrated stats if we found any
        if migrated["total_prompts"] > 0:
            atomic_write_json(path, migrated)
            return migrated
    
    return {
        "total_prompts": 0,
        "total_gpt_prompts": 0,
        "total_sonnet_prompts": 0,
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cost": 0.0,
        "first_prompt_date": None,
        "total_gpt_context_tokens": 0,
        "total_sonnet_context_tokens": 0
    }

def update_persistent_stats(username: str, input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int, cost: float, model: str = None, context_tokens: int = 0):
    """Add to lifetime stats (never subtract). Uses file locking for concurrent access safety."""
    path = get_persistent_stats_path(username)

    with file_lock(path):
        stats = load_persistent_stats(username)
        old_prompts = stats["total_prompts"]
        stats["total_prompts"] += 1
        stats["total_input_tokens"] += input_tokens
        stats["total_cached_tokens"] += cached_tokens
        stats["total_output_tokens"] += output_tokens
        stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + reasoning_tokens
        stats["total_cost"] += cost

        # Track model-specific prompts and context tokens
        is_sonnet = model and model.startswith("claude")
        if is_sonnet:
            stats["total_sonnet_prompts"] = stats.get("total_sonnet_prompts", 0) + 1
            stats["total_sonnet_context_tokens"] = stats.get("total_sonnet_context_tokens", 0) + context_tokens
        else:
            stats["total_gpt_prompts"] = stats.get("total_gpt_prompts", 0) + 1
            stats["total_gpt_context_tokens"] = stats.get("total_gpt_context_tokens", 0) + context_tokens

        # Track first prompt date
        if stats["first_prompt_date"] is None:
            stats["first_prompt_date"] = date.today().isoformat()

        atomic_write_json(path, stats)
        logger.info(f"update_persistent_stats: user={username}, prompts {old_prompts} -> {stats['total_prompts']}, cost={cost}")

def get_chat_path(username: str, chat_name: str, project: str = None) -> str:
    if project:
        return os.path.join(get_user_dir(username), "projects", project, f"chat_{chat_name}.json")
    return os.path.join(get_user_dir(username), f"chat_{chat_name}.json")

def validate_name(name: str) -> bool:
    """Validate chat/project names - block path separators and control characters"""
    if not name or not name.strip():
        return False
    # Block path separators
    if '/' in name or '\\' in name:
        return False
    # Block control characters (anything below space)
    if any(ord(c) < 32 for c in name):
        return False
    # Block reserved names that would collide with internal files
    # "index" would create chat_index.json which is the chat index file
    if name.lower() == "index":
        return False
    return True

def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken (cl100k_base encoding for GPT-4+)"""
    enc = get_token_encoder()
    return len(enc.encode(text))

def calculate_context_window(
    messages: list,
    threshold: int = CONTEXT_WINDOW_THRESHOLD,
    target: int = CONTEXT_WINDOW_TARGET,
    count_tokens_fn: callable = None
) -> int:
    """
    Calculate context_start_index for rolling context window.

    Returns the index of the first message to include in context (after system).
    Messages structure: [system, msg1, msg2, ..., msgN, new_user_msg]

    Args:
        messages: List of messages with system first, new user message last
        threshold: Token count to trigger trimming
        target: Token count to trim down to
        count_tokens_fn: Provider-specific token counting function (defaults to global)

    Logic:
    - If total tokens <= threshold (275k), include everything (return 1)
    - If over threshold, find cut point to get back to target (225k)
    - Count from newest messages backwards, include as many as fit in target
    - Always cut on user message boundaries (never leave orphaned assistant responses)
    """
    # Use provider's token counter if provided, else fall back to global
    token_counter = count_tokens_fn or count_tokens

    if len(messages) <= 2:
        # Just system + one user message, include everything
        return 1

    # Count system tokens (use cached if available)
    system_msg = messages[0]
    system_tokens = system_msg.get("total_tokens")
    if system_tokens is None:
        system_tokens = token_counter(system_msg.get("content", ""))

    # Count new user message tokens (last message), including attached files
    # Build combined content first, then count once (BPE tokenizers are non-additive)
    last_msg = messages[-1]
    new_user_tokens = last_msg.get("total_tokens")
    if new_user_tokens is None:
        content = last_msg["content"]
        attached_files = last_msg.get("attached_files", [])
        if attached_files:
            wrappers = [_attached_file_text_repr(f) for f in attached_files]
            content = "\n\n".join(wrappers) + "\n\n" + content
        new_user_tokens = token_counter(content)

    # Base tokens that are always included
    base_tokens = system_tokens + new_user_tokens

    # History is everything except system (index 0) and new user msg (index -1)
    history = messages[1:-1]

    # First pass: count total to see if we exceed threshold
    total_tokens = base_tokens
    for msg in history:
        # Use explicit None check since 0 is a valid token count
        msg_tokens = msg.get("total_tokens")
        if msg_tokens is None:
            # Count content + attached files (matching build_message_content format)
            content = msg.get("content", "")
            attached = msg.get("attached_files", [])
            if attached:
                wrappers = [_attached_file_text_repr(f) for f in attached]
                content = "\n\n".join(wrappers) + "\n\n" + content
            msg_tokens = token_counter(content)
        total_tokens += msg_tokens

    if total_tokens <= threshold:
        # Under threshold, include everything
        return 1

    # We exceed threshold, need to find cut point to get to target
    # Count from newest to oldest until we hit target
    total_tokens = base_tokens
    included_from_end = 0

    for msg in reversed(history):
        # Use explicit None check since 0 is a valid token count
        msg_tokens = msg.get("total_tokens")
        if msg_tokens is None:
            # Count content + attached files (matching build_message_content format)
            content = msg.get("content", "")
            attached = msg.get("attached_files", [])
            if attached:
                wrappers = [_attached_file_text_repr(f) for f in attached]
                content = "\n\n".join(wrappers) + "\n\n" + content
            msg_tokens = token_counter(content)
        if total_tokens + msg_tokens > target:
            # Including this message would exceed target, stop here
            break
        total_tokens += msg_tokens
        included_from_end += 1

    # context_start_index: where we start including messages
    # history starts at index 1, so if we include N from end:
    # start = len(history) - included_from_end + 1
    context_start_index = len(history) - included_from_end + 1

    # Ensure we cut on a user message boundary
    # Check by role instead of index parity (handles missing assistant messages)
    if context_start_index > 1 and context_start_index < len(messages):
        # If we landed on an assistant message, move forward to next user message
        while context_start_index < len(messages) - 1 and messages[context_start_index].get("role") == "assistant":
            context_start_index += 1

    return context_start_index

# ============================================================
# Tree/Branching Utility Functions
# ============================================================

def generate_message_id() -> str:
    """Generate a unique message ID"""
    return str(uuid.uuid4())


def build_message_index(messages: list) -> dict:
    """
    Build an index of messages by ID for O(1) lookup.

    Returns:
        dict mapping message_id -> message dict
    """
    return {msg["id"]: msg for msg in messages if "id" in msg}


def get_path_to_root(messages: list, leaf_id: str) -> list[dict]:
    """
    Get the path from root to the specified leaf message.

    Returns:
        List of messages in order from root (system) to leaf
    """
    if not messages or not leaf_id:
        return []

    index = build_message_index(messages)

    # Walk backwards from leaf to root
    path = []
    current_id = leaf_id
    while current_id:
        if current_id not in index:
            break
        msg = index[current_id]
        path.append(msg)
        current_id = msg.get("parent_id")

    # Reverse to get root-to-leaf order
    path.reverse()
    return path


def get_children(messages: list, parent_id: str | None) -> list[dict]:
    """
    Get all direct children of a message.

    Args:
        messages: List of all messages
        parent_id: ID of the parent message (None for root's children)

    Returns:
        List of messages that have this parent_id
    """
    return [msg for msg in messages if msg.get("parent_id") == parent_id]


def get_siblings(messages: list, message_id: str) -> list[dict]:
    """
    Get all siblings of a message (messages with the same parent).

    Returns:
        List of sibling messages (including the message itself), sorted by timestamp
    """
    if not messages or not message_id:
        return []

    index = build_message_index(messages)
    if message_id not in index:
        return []

    target_msg = index[message_id]
    parent_id = target_msg.get("parent_id")

    # Find all messages with the same parent
    siblings = [msg for msg in messages if msg.get("parent_id") == parent_id]

    # Sort by timestamp (oldest first) for consistent ordering
    siblings.sort(key=lambda m: m.get("timestamp", ""))

    return siblings


def get_deepest_leaf(messages: list, start_id: str) -> str:
    """
    Find the deepest leaf in the subtree starting from start_id.

    For branch navigation: when switching to a sibling branch, we want to
    navigate to the most recent message in that branch.

    Returns:
        ID of the deepest leaf message in the subtree
    """
    if not messages or not start_id:
        return start_id

    index = build_message_index(messages)
    if start_id not in index:
        return start_id

    # Build children lookup
    children_map: dict[str | None, list[dict]] = {}
    for msg in messages:
        parent = msg.get("parent_id")
        if parent not in children_map:
            children_map[parent] = []
        children_map[parent].append(msg)

    # Sort children by timestamp (most recent last) so we follow the "main" path
    for children in children_map.values():
        children.sort(key=lambda m: m.get("timestamp", ""))

    # Walk down, always taking the last (most recent) child
    current_id = start_id
    while current_id in children_map and children_map[current_id]:
        children = children_map[current_id]
        current_id = children[-1]["id"]  # Take most recent child

    return current_id


def is_migrated(data: dict) -> bool:
    """Check if a chat has been migrated to branching format"""
    messages = data.get("messages", [])
    return bool(messages and "id" in messages[0])


def migrate_chat_inline(data: dict) -> dict:
    """
    Migrate a chat to branching format inline (without saving).
    Used for on-the-fly migration when loading old chats.
    """
    messages = data.get("messages", [])

    if not messages or "id" in messages[0]:
        return data  # Already migrated or empty

    prev_id = None
    for msg in messages:
        msg["id"] = generate_message_id()
        msg["parent_id"] = prev_id
        prev_id = msg["id"]

    if messages:
        data["current_leaf_id"] = messages[-1]["id"]

    return data


def load_chat(username: str, chat_name: str, project: str = None) -> dict:
    # Block reserved names that collide with internal files
    if chat_name.lower() == "index":
        return None
    path = get_chat_path(username, chat_name, project)
    if os.path.exists(path):
        with open(path, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                data = {"messages": data, "stats": create_empty_stats()}
            # Auto-migrate on load if not yet migrated
            if not is_migrated(data):
                data = migrate_chat_inline(data)
            return data
    return None

def save_chat(username: str, chat_name: str, data: dict, project: str = None):
    """Save chat data atomically with file locking for concurrent access safety."""
    # Block reserved names that collide with internal files
    if chat_name.lower() == "index":
        return
    path = get_chat_path(username, chat_name, project)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with file_lock(path):
        atomic_write_json(path, data)

    # Update the chat index with last_accessed timestamp
    last_accessed = data.get("stats", {}).get("last_accessed", datetime.now(timezone.utc).isoformat())
    update_chat_index(username, chat_name, last_accessed, project)


def get_chat_index_path(username: str, project: str = None) -> str:
    """Get path to the chat index file for a user or project."""
    if project:
        return os.path.join(get_project_dir(username, project), "chat_index.json")
    return os.path.join(get_user_dir(username), "chat_index.json")


def load_chat_index(username: str, project: str = None) -> dict:
    """Load the chat index, returning empty dict if not found."""
    path = get_chat_index_path(username, project)
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_chat_index(username: str, index: dict, project: str = None):
    """Save the chat index atomically."""
    path = get_chat_index_path(username, project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with file_lock(path):
        atomic_write_json(path, index)


def update_chat_index(username: str, chat_name: str, last_accessed: str, project: str = None):
    """Update a single chat's entry in the index."""
    index = load_chat_index(username, project)
    index[chat_name] = {"last_accessed": last_accessed}
    save_chat_index(username, index, project)


def remove_from_chat_index(username: str, chat_name: str, project: str = None):
    """Remove a chat from the index (when deleted)."""
    index = load_chat_index(username, project)
    if chat_name in index:
        del index[chat_name]
        save_chat_index(username, index, project)


def rebuild_chat_index(username: str, project: str = None) -> dict:
    """Rebuild the chat index by scanning all chat files. Used as fallback."""
    if project:
        chat_dir = get_project_dir(username, project)
    else:
        chat_dir = get_user_dir(username)

    if not os.path.exists(chat_dir):
        return {}

    index = {}
    for f in os.listdir(chat_dir):
        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
            chat_name = f[5:-5]
            chat_data = load_chat(username, chat_name, project)
            if chat_data:
                last_accessed = chat_data.get("stats", {}).get("last_accessed", "1970-01-01T00:00:00+00:00")
                index[chat_name] = {"last_accessed": last_accessed}

    save_chat_index(username, index, project)
    return index


def create_backup(username: str, chat_name: str, project: str = None):
    """Create timestamped backup before destructive operation"""
    # Load current chat
    data = load_chat(username, chat_name, project)
    if not data:
        return
    
    # Create backup directory
    if project:
        backup_dir = os.path.join(get_user_dir(username), "projects", project, "backups")
    else:
        backup_dir = os.path.join(get_user_dir(username), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    
    # Generate backup filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"chat_{chat_name}_{timestamp}.json"
    backup_path = os.path.join(backup_dir, backup_filename)
    
    # Save backup
    with open(backup_path, 'w') as f:
        json.dump(data, f, indent=2)

def create_empty_stats() -> dict:
    return {
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_cost": 0.0,
        "total_prompts": 0,
        "first_prompt_date": datetime.now(ZoneInfo('America/New_York')).date().isoformat(),
        "last_accessed": datetime.now(timezone.utc).isoformat()
    }

def get_daily_usage_path(username: str) -> str:
    """Get path to daily usage tracking file"""
    return os.path.join(get_user_dir(username), "daily_usage.json")

def load_daily_usage(username: str, save_if_reset: bool = False) -> dict:
    """Load daily usage data, reset if new day (UTC)

    Args:
        username: The user
        save_if_reset: If True, immediately save reset data to disk when day changes.
                       This prevents race conditions where concurrent requests might
                       read stale data before the caller saves.
    """
    path = get_daily_usage_path(username)
    today_utc = datetime.now(ZoneInfo('UTC')).date().isoformat()

    if os.path.exists(path):
        with open(path, 'r') as f:
            data = json.load(f)
            # Reset if new day
            if data.get("date") != today_utc:
                data = {"date": today_utc, "tokens_used": 0}
                if save_if_reset:
                    # Immediately persist the reset to prevent stale reads
                    save_daily_usage(username, data)
                    logger.info(f"load_daily_usage: reset to new day {today_utc} for user {username}")
            return data

    data = {"date": today_utc, "tokens_used": 0}
    if save_if_reset:
        save_daily_usage(username, data)
        logger.info(f"load_daily_usage: created new usage file for user {username}")
    return data

def save_daily_usage(username: str, data: dict):
    """Save daily usage data atomically with file locking."""
    path = get_daily_usage_path(username)
    with file_lock(path):
        atomic_write_json(path, data)

def apply_free_tokens(username: str, total_tokens: int, full_cost: float, commit: bool = True) -> tuple[float, str, dict | None]:
    """
    Apply free tokens (resets at 0:00 UTC).

    Args:
        username: User to apply free tokens for
        total_tokens: Total tokens used in this request
        full_cost: Full cost before free token discount
        commit: If True, save usage immediately. If False, return usage dict for later commit.

    Returns: (actual_cost, cost_display_string, usage_to_commit)
        - usage_to_commit is None if commit=True, otherwise the usage dict to save later
    """
    # Load current usage (save immediately if day reset to prevent race conditions)
    usage = load_daily_usage(username, save_if_reset=True)
    tokens_used_before = usage["tokens_used"]
    remaining_free = max(0, FREE_TOKENS_PER_DAY - tokens_used_before)

    logger.info(f"apply_free_tokens: user={username}, total_tokens={total_tokens}, "
                f"tokens_used_before={tokens_used_before}, remaining_free={remaining_free}, "
                f"usage_date={usage.get('date')}")

    # Apply free tokens
    if total_tokens <= remaining_free:
        # Entirely free
        actual_cost = 0.0
        cost_str = "free"
    elif remaining_free > 0:
        # Partially free
        paid_tokens = total_tokens - remaining_free
        actual_cost = full_cost * (paid_tokens / total_tokens)
        cost_str = f"${actual_cost:.6f} (reduced from ${full_cost:.6f})"
    else:
        # No free tokens left
        actual_cost = full_cost
        cost_str = f"${actual_cost:.6f}"

    # Update usage in memory
    usage["tokens_used"] = tokens_used_before + total_tokens

    if commit:
        save_daily_usage(username, usage)
        logger.info(f"apply_free_tokens: saved tokens_used={usage['tokens_used']}, cost_str={cost_str}")
        return actual_cost, cost_str, None
    else:
        logger.info(f"apply_free_tokens: deferred save, tokens_used={usage['tokens_used']}, cost_str={cost_str}")
        return actual_cost, cost_str, usage

def get_instructions(username: str, project: str = None) -> str:
    if project:
        path = os.path.join(get_user_dir(username), "projects", project, "instructions.di")
    else:
        path = os.path.join(get_user_dir(username), "instructions.di")

    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            instructions = f.read()
    else:
        instructions = "You are a helpful assistant."

    return instructions

def get_base_instructions(username: str) -> str:
    """Load user-level base instructions (shared DM rules across all projects).
    Returned separately so callers can place them at the END of the system prompt
    (after project files) for maximum salience."""
    base_path = os.path.join(get_user_dir(username), "base_instructions.di")
    if os.path.exists(base_path):
        with open(base_path, 'r', encoding='utf-8') as f:
            base = f.read().strip()
        if base:
            return base
    return ""

def build_system_content(username: str, project: str, include_base: bool = True,
                         default_instructions: str = None,
                         fallback_to_user_instructions: bool = False,
                         beat_state: dict | None = None) -> str:
    """Build the full system prompt: instructions + project files + base instructions.
    Base instructions go LAST (after project files) for maximum end-of-prompt salience.

    If `beat_state` is supplied AND a `Plot - Session N.md` file exists for
    the active session, a `[CURRENT BEAT]` block is appended at the very end
    (after base_instructions) — last-thing-the-model-sees salience for the
    pacing rule "stay on this beat until its goals are met."

    Args:
        include_base: If False, skip base_instructions.di entirely.
        default_instructions: Custom fallback when project has no instructions.di.
        fallback_to_user_instructions: If True and project instructions.di is missing,
            fall back to user-level instructions.di before the hardcoded default.
        beat_state: pipeline_state.beat_state dict (current_session, current_beat, etc).
    """
    instructions = get_instructions(username, project)
    # Layering: gamesystem-supplied default_instructions are ALWAYS loaded as
    # baseline (when provided). Project's own instructions.di appends on top
    # for project-specific additions. Without this, providing any project
    # instructions.di would silently nuke the gamesystem's baseline rules.
    #
    # ensure_project_exists writes a stub "You are a helpful assistant." when a
    # project is first created. Treat that exact stub as "no real project
    # rules" so it doesn't pollute the layered prompt for fresh projects.
    PLACEHOLDER_INSTRUCTIONS = "You are a helpful assistant."
    if project:
        project_instructions_path = os.path.join(get_user_dir(username), "projects", project, "instructions.di")
        has_project_instructions = os.path.exists(project_instructions_path)
        is_real_project_content = (
            has_project_instructions and instructions.strip() != PLACEHOLDER_INSTRUCTIONS
        )
        if is_real_project_content:
            # Real project content — append to gamesystem default (if any).
            if default_instructions:
                instructions = default_instructions + "\n\n" + instructions
            # else: just `instructions` (the project file content) as-is
        else:
            # No real project content (file missing or just the stub) — use fallback chain.
            if fallback_to_user_instructions:
                instructions = get_instructions(username, None)
            elif default_instructions:
                instructions = default_instructions

        project_files = load_project_files(username, project)
        parts = [instructions]
        if project_files:
            parts.append(project_files)
        if include_base:
            base = get_base_instructions(username)
            if base:
                parts.append(base)
        # [CURRENT BEAT] injection — appended LAST so it sits closest to the
        # turn boundary (highest salience).  Only fires when beat_state is
        # supplied AND a Plot - Session N.md is parseable for that session.
        if beat_state:
            try:
                from game_systems.plot_beats import build_current_beat_injection
                uploads_dir = os.path.join(get_project_dir(username, project), "uploads")
                beat_block = build_current_beat_injection(beat_state, uploads_dir)
                if beat_block:
                    parts.append(beat_block)
            except Exception as e:  # noqa: BLE001 — never break system prompt
                logger.warning("build_system_content: beat injection failed: %s", e)
        return "\n\n".join(parts)
    else:
        return instructions

def _system_content_kwargs(gs) -> dict:
    """Extract build_system_content kwargs from a game system dict."""
    if not gs:
        return {}
    return {
        "include_base": gs.get("use_base_instructions", True),
        "default_instructions": gs.get("default_instructions"),
        "fallback_to_user_instructions": gs.get("fallback_to_user_instructions", False),
    }


def _build_artifact_summary(artifacts: dict) -> str:
    """Build a [DOCUMENTS] summary block for injecting into user messages."""
    if not artifacts:
        return ""
    lines = ["[DOCUMENTS]"]
    for doc_id, doc in artifacts.items():
        word_count = len(doc.get("content", "").split())
        doc_type = doc.get("type", "prose")
        version = doc.get("version", 1)
        pinned_tag = " [pinned]" if doc.get("pinned") else ""
        lines.append(f'- {doc_id}: "{doc.get("title", doc_id)}" ({doc_type}, ~{word_count} words, v{version}){pinned_tag}')
    lines.append("[/DOCUMENTS]")
    return "\n".join(lines)


def _build_pinned_artifacts(artifacts: dict) -> str:
    """Build a block containing full content of pinned documents for system prompt injection."""
    if not artifacts:
        return ""
    parts = []
    for doc_id, doc in artifacts.items():
        if doc.get("pinned"):
            title = doc.get("title", doc_id)
            content = doc.get("content", "")
            parts.append(f"[PINNED: {doc_id} — {title}]\n{content}\n[/PINNED: {doc_id}]")
    if not parts:
        return ""
    return "[PINNED DOCUMENTS]\n" + "\n\n".join(parts) + "\n[/PINNED DOCUMENTS]"


def _process_doc_tool_calls(tool_uses: list, artifacts: dict) -> list:
    """Process document tool calls, mutating the artifacts dict.

    Returns a list of {action, doc_id, title, version, error?} dicts describing what happened,
    plus any read_doc results that need tool_result follow-up.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    ops = []

    for tool_call in tool_uses:
        name = tool_call.get("name", "")
        inp = tool_call.get("input", {})
        tool_id = tool_call.get("id", "")

        if name == "create_doc":
            doc_id = inp.get("doc_id", "")
            if not doc_id:
                continue
            artifacts[doc_id] = {
                "doc_id": doc_id,
                "title": inp.get("title", doc_id),
                "content": inp.get("content", ""),
                "type": inp.get("type", "prose"),
                "format": inp.get("format"),
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }
            ops.append({"action": "created", "doc_id": doc_id, "title": artifacts[doc_id]["title"],
                         "version": 1, "tool_use_id": tool_id})

        elif name == "replace_doc":
            doc_id = inp.get("doc_id", "")
            if doc_id not in artifacts:
                ops.append({"action": "error", "doc_id": doc_id, "error": "not found", "tool_use_id": tool_id})
                continue
            doc = artifacts[doc_id]
            if inp.get("title"):
                doc["title"] = inp["title"]
            doc["content"] = inp.get("content", "")
            doc["version"] = doc.get("version", 1) + 1
            doc["updated_at"] = now
            ops.append({"action": "replaced", "doc_id": doc_id, "title": doc["title"],
                         "version": doc["version"], "tool_use_id": tool_id})

        elif name == "edit_doc":
            doc_id = inp.get("doc_id", "")
            if doc_id not in artifacts:
                ops.append({"action": "error", "doc_id": doc_id, "error": "not found", "tool_use_id": tool_id})
                continue
            doc = artifacts[doc_id]
            content = doc["content"]
            edit_count = 0
            for edit in inp.get("edits", []):
                old_text = edit.get("old_text", "")
                new_text = edit.get("new_text", "")
                if old_text and old_text in content:
                    content = content.replace(old_text, new_text, 1)
                    edit_count += 1
            if edit_count > 0:
                doc["content"] = content
                doc["version"] = doc.get("version", 1) + 1
                doc["updated_at"] = now
            ops.append({"action": "edited", "doc_id": doc_id, "title": doc["title"],
                         "version": doc["version"], "edit_count": edit_count, "tool_use_id": tool_id})

        elif name == "read_doc":
            doc_id = inp.get("doc_id", "")
            if doc_id in artifacts:
                doc = artifacts[doc_id]
                ops.append({"action": "read", "doc_id": doc_id, "title": doc["title"],
                             "content": doc["content"], "tool_use_id": tool_id})
            else:
                ops.append({"action": "error", "doc_id": doc_id, "error": "not found", "tool_use_id": tool_id})

    return ops


def get_instructions_for_agent(username: str, project: str, agent_name: str) -> str:
    """Load per-agent instructions file, falling back to shared instructions.di."""
    agent_path = os.path.join(get_user_dir(username), "projects", project, f"instructions_{agent_name}.di")
    if os.path.exists(agent_path):
        with open(agent_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if content.strip():
            return content
    # Fall back to shared instructions
    return get_instructions(username, project)

def get_project_dir(username: str, project: str) -> str:
    """Get path to a project directory"""
    return os.path.join(get_user_dir(username), "projects", project)

def ensure_project_exists(username: str, project: str) -> bool:
    """Create project directory if it doesn't exist. Returns True if new."""
    project_dir = get_project_dir(username, project)
    is_new = not os.path.exists(project_dir)

    if is_new:
        os.makedirs(project_dir)
        os.makedirs(os.path.join(project_dir, "uploads"))
        # Create default instructions.di
        with open(os.path.join(project_dir, "instructions.di"), 'w', encoding='utf-8') as f:
            f.write("You are a helpful assistant.")
        # Create initial metadata with default model
        save_project_metadata(username, project, {
            "last_accessed": datetime.now(timezone.utc).isoformat(),
            "model": DEFAULT_MODEL
        })

    return is_new


def _maybe_copy_base_user_life(username: str, project_dir: str) -> bool:
    """Auto-copy {user_dir}/base_user_life.di → {project_dir}/user_life.di if:
      - base file exists
      - target doesn't already exist (preserves any per-project customization)

    Returns True if a copy was made. Logs but doesn't raise on file errors.
    Used by Characters projects (gamesystem set + chat creation) to seed each
    project with the user's curated base.
    """
    base_path = os.path.join(get_user_dir(username), "base_user_life.di")
    target_path = os.path.join(project_dir, "user_life.di")
    if not os.path.isfile(base_path):
        return False
    if os.path.exists(target_path):
        return False
    try:
        with open(base_path, "r", encoding="utf-8") as src:
            content = src.read()
        with open(target_path, "w", encoding="utf-8") as dst:
            dst.write(content)
        logger.info(f"Auto-copied base_user_life.di → {target_path}")
        return True
    except OSError as e:
        logger.warning(f"Failed to auto-copy base_user_life.di for {username}/{os.path.basename(project_dir)}: {e}")
        return False

HACK_ONLY_FILES = {"Hacking Rulebook.md"}
COMBAT_ONLY_FILES = {"Combat Ruleset.md"}

def load_project_files(username: str, project: str) -> str:
    """Load all staged project files from project's uploads folder.
    Hack-only files (e.g. Hacking Rulebook.md) and combat-only files are excluded
    — they are injected separately during their respective modes."""
    uploads_dir = os.path.join(get_project_dir(username, project), "uploads")

    if not os.path.exists(uploads_dir):
        return ""

    # Load token cache to check staged status
    tokens_cache = load_file_tokens_cache(username, project)

    project_files = [f for f in os.listdir(uploads_dir) if os.path.splitext(f)[1].lower() in ALLOWED_FILE_EXTENSIONS]
    if not project_files:
        return ""

    combined = ""
    for filename in sorted(project_files):
        # Skip mode-specific files (injected separately during hack/combat mode)
        if filename in HACK_ONLY_FILES or filename in COMBAT_ONLY_FILES:
            continue
        # Skip files that are not staged (default to True for backward compat)
        if not tokens_cache.get(filename, {}).get("staged", True):
            continue

        filepath = os.path.join(uploads_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            combined += f"\n\n{'='*60}\n"
            combined += f"FILE: {filename}\n"
            combined += f"{'='*60}\n\n"
            combined += f.read()

    return combined


def _consume_plot_prefix(msg: dict) -> bool:
    """Detect and strip a leading `/plot` slash-command on a user message.

    The /plot command is a per-turn opt-in to inject project plot/lore docs
    into mode pipelines (hack/combat/net_combat/sex) where they're normally
    excluded for token efficiency. Mutates msg['content'] in place to remove
    the prefix so the saved chat history doesn't preserve the slash command.

    Also stamps `_plot_used: True` on the message dict so the chat history
    preserves a marker that /plot fired this turn (useful for diagnostics).

    Recognized forms (case-insensitive):
      "/plot"             → strip, return True (empty body)
      "/plot foo"         → strip, msg.content = "foo", return True
      "/plot\nfoo"        → strip, msg.content = "foo", return True

    Returns True if the prefix was consumed; False if no /plot command.
    """
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.lstrip()
    if not stripped:
        return False
    # Match `/plot` followed by space, newline, or end-of-string
    lower = stripped.lower()
    if not lower.startswith("/plot"):
        return False
    after_cmd = stripped[len("/plot"):]
    if after_cmd and after_cmd[0] not in (" ", "\t", "\n", "\r"):
        return False
    # Strip the prefix and any leading whitespace from the remainder
    msg["content"] = after_cmd.lstrip()
    msg["_plot_used"] = True
    return True


def _consume_beat_prefix(msg: dict) -> dict | None:
    """Detect and strip a `/beat <N>` or `/beat next` prefix from a user
    message.  Returns a directive dict for the caller to apply to
    pipeline_state.beat_state, or None if no /beat command.

    Forms (case-insensitive):
      "/beat next"  → {"action": "next"}
      "/beat 3"     → {"action": "set", "beat": 3}
      "/beat reset" → {"action": "reset"}
      "/beat"       → {"action": "show"}   (no-op; future: render summary)

    Mutates msg['content'] in place to remove the prefix.
    """
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    if not stripped:
        return None
    lower = stripped.lower()
    if not lower.startswith("/beat"):
        return None
    after_cmd = stripped[len("/beat"):]
    if after_cmd and after_cmd[0] not in (" ", "\t", "\n", "\r"):
        return None
    rest = after_cmd.strip()
    msg["_beat_used"] = True
    # Parse the argument off the front of `rest`; whatever's after stays.
    parts = rest.split(None, 1)
    arg = (parts[0].lower() if parts else "")
    remainder = (parts[1] if len(parts) > 1 else "")
    msg["content"] = remainder
    if not arg:
        return {"action": "show"}
    if arg in ("next", "advance", "complete"):
        return {"action": "next"}
    if arg in ("reset", "restart"):
        return {"action": "reset"}
    if arg.isdigit():
        return {"action": "set", "beat": int(arg)}
    return {"action": "unknown", "raw": arg}


def _consume_session_prefix(msg: dict) -> dict | None:
    """`/session <N>` slash command — switch to session N's plot doc.
    Resets current_beat to 1 and clears completed_beats for the new session.
    """
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if not isinstance(content, str):
        return None
    stripped = content.lstrip()
    if not stripped:
        return None
    lower = stripped.lower()
    if not lower.startswith("/session"):
        return None
    after_cmd = stripped[len("/session"):]
    if after_cmd and after_cmd[0] not in (" ", "\t", "\n", "\r"):
        return None
    rest = after_cmd.strip()
    parts = rest.split(None, 1)
    arg = (parts[0] if parts else "")
    remainder = (parts[1] if len(parts) > 1 else "")
    msg["content"] = remainder
    msg["_session_used"] = True
    if arg.isdigit():
        return {"action": "set", "session": int(arg)}
    return {"action": "unknown", "raw": arg}


def _apply_beat_directive(pipeline_state: dict, directive: dict,
                          uploads_dir: str | None = None) -> str:
    """Apply a /beat or /session directive to pipeline_state.beat_state.
    Returns a short OOC confirmation string for the next system message
    so the player sees "Beat 2 → 3" feedback.
    """
    from game_systems.plot_beats import (
        normalize_beat_state, advance_beat, set_beat, set_session,
    )
    bs = normalize_beat_state(pipeline_state.get("beat_state"))
    action = directive.get("action") if isinstance(directive, dict) else None
    if action == "next":
        new_bs = advance_beat(bs, uploads_dir=uploads_dir)
        msg = (f"[OOC] Beat advanced: Session {bs['current_session']}, "
               f"Beat {bs['current_beat']} → "
               f"Session {new_bs['current_session']}, "
               f"Beat {new_bs['current_beat']}.")
        bs = new_bs
    elif action == "set":
        if "beat" in directive:
            new_bs = set_beat(bs, directive["beat"])
            msg = (f"[OOC] Beat set: Session {bs['current_session']}, "
                   f"Beat {bs['current_beat']} → Beat {new_bs['current_beat']}.")
            bs = new_bs
        elif "session" in directive:
            new_bs = set_session(bs, directive["session"])
            msg = (f"[OOC] Session switched: {bs['current_session']} → "
                   f"{new_bs['current_session']}, Beat 1.")
            bs = new_bs
        else:
            msg = "[OOC] Beat directive missing target."
    elif action == "reset":
        from game_systems.plot_beats import init_beat_state
        bs = init_beat_state()
        msg = "[OOC] Beat state reset to Session 1, Beat 1."
    elif action == "show":
        msg = (f"[OOC] Beat state: Session {bs['current_session']}, "
               f"Beat {bs['current_beat']}. "
               f"Completed this session: "
               f"{bs['completed_beats'] or 'none'}.")
    else:
        msg = f"[OOC] Unknown beat directive: {directive!r}"
    pipeline_state["beat_state"] = bs
    return msg
    """Load plot docs only for one-shot injection via /plot command.

    Filters to filenames starting with "plot" (case-insensitive) — typically
    `Plot - Session N.md` style files. Excludes rulebooks, character sheets,
    enemy templates, and other staged project docs that aren't plot/lore.

    Honors the staging cache: unstaged plot docs (e.g., a future-session
    plot file the player wants to keep spoiler-free) are skipped, so users
    can control spoiler exposure via the file manager.
    """
    if not username or not project:
        return ""
    try:
        uploads_dir = os.path.join(get_project_dir(username, project), "uploads")
        if not os.path.exists(uploads_dir):
            return ""
        tokens_cache = load_file_tokens_cache(username, project)
        plot_files = []
        for filename in os.listdir(uploads_dir):
            if os.path.splitext(filename)[1].lower() not in ALLOWED_FILE_EXTENSIONS:
                continue
            if not filename.lower().startswith("plot"):
                continue
            if not tokens_cache.get(filename, {}).get("staged", True):
                continue
            plot_files.append(filename)
        if not plot_files:
            return ""
        chunks = []
        for filename in sorted(plot_files):
            filepath = os.path.join(uploads_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    chunks.append(
                        f"\n\n{'='*60}\nFILE: {filename}\n{'='*60}\n\n" + f.read()
                    )
            except (OSError, UnicodeDecodeError):
                continue
        if not chunks:
            return ""
        blob = "".join(chunks)
    except Exception:
        return ""
    return (
        "[PLOT DOCS — one-shot injection via /plot, NOT persisted in history]\n"
        "Use this context for THIS reply only. Reference what's in here to ground "
        "your narration; do not echo the docs back verbatim.\n"
        f"{blob}\n"
        "[/PLOT DOCS]"
    )


def get_staged_project_filenames(username: str, project: str) -> set:
    """Return a set of lowercased filename stems (no extensions) for all staged, non-mode-specific project files."""
    uploads_dir = os.path.join(get_project_dir(username, project), "uploads")
    if not os.path.exists(uploads_dir):
        return set()
    tokens_cache = load_file_tokens_cache(username, project)
    stems = set()
    for filename in os.listdir(uploads_dir):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_FILE_EXTENSIONS:
            continue
        if filename in HACK_ONLY_FILES or filename in COMBAT_ONLY_FILES:
            continue
        if not tokens_cache.get(filename, {}).get("staged", True):
            continue
        stems.add(os.path.splitext(filename)[0].lower())
    return stems


def extract_project_file_stems(project_files_blob: str) -> set:
    """Extract lowercased filename stems from `FILE: ...` headers in a combined project-files blob."""
    if not project_files_blob:
        return set()
    stems = set()
    for line in project_files_blob.splitlines():
        if not line.startswith("FILE: "):
            continue
        filename = line[len("FILE: "):].strip()
        if not filename:
            continue
        stems.add(os.path.splitext(filename)[0].lower())
    return stems


def load_project_files_for_agent(username: str, project: str, agent_name: str) -> str:
    """Load staged files filtered to a specific pipeline agent."""
    uploads_dir = os.path.join(get_project_dir(username, project), "uploads")

    if not os.path.exists(uploads_dir):
        return ""

    tokens_cache = load_file_tokens_cache(username, project)

    project_files = [f for f in os.listdir(uploads_dir) if os.path.splitext(f)[1].lower() in ALLOWED_FILE_EXTENSIONS]
    if not project_files:
        return ""

    combined = ""
    for filename in sorted(project_files):
        # Skip mode-specific files (injected separately during hack/combat mode)
        if filename in HACK_ONLY_FILES or filename in COMBAT_ONLY_FILES:
            continue
        file_cache = tokens_cache.get(filename, {})
        # Skip files that are not staged
        if not file_cache.get("staged", True):
            continue
        # Skip files not assigned to this agent (default to all agents)
        file_agents = file_cache.get("agents", PIPELINE_AGENT_NAMES)
        if agent_name not in file_agents:
            continue

        filepath = os.path.join(uploads_dir, filename)
        with open(filepath, 'r', encoding='utf-8') as f:
            combined += f"\n\n{'='*60}\n"
            combined += f"FILE: {filename}\n"
            combined += f"{'='*60}\n\n"
            combined += f.read()

    if not combined.strip() and project_files:
        skipped_reasons = []
        for filename in sorted(project_files):
            file_cache = tokens_cache.get(filename, {})
            staged = file_cache.get("staged", True)
            file_agents = file_cache.get("agents", PIPELINE_AGENT_NAMES)
            if not staged:
                skipped_reasons.append(f"{filename}: unstaged")
            elif agent_name not in file_agents:
                skipped_reasons.append(f"{filename}: agents={file_agents}, need={agent_name}")
        logger.warning(f"load_project_files_for_agent({agent_name}): 0 files loaded from {len(project_files)} on disk. Skipped: {skipped_reasons}")

    return combined

def get_project_metadata_path(username: str, project: str) -> str:
    """Get path to project metadata file"""
    return os.path.join(get_project_dir(username, project), "metadata.json")

def load_project_metadata(username: str, project: str) -> dict:
    """Load project metadata (like last_accessed). Auto-migrates game_system if missing."""
    metadata_path = get_project_metadata_path(username, project)
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        # Auto-migrate: ensure game_system exists
        if "game_system" not in metadata:
            metadata["game_system"] = DEFAULT_GAME_SYSTEM
            save_project_metadata(username, project, metadata)
        return metadata
    return {"last_accessed": "1970-01-01T00:00:00", "game_system": DEFAULT_GAME_SYSTEM}

def save_project_metadata(username: str, project: str, metadata: dict):
    """Save project metadata atomically."""
    metadata_path = get_project_metadata_path(username, project)
    atomic_write_json(metadata_path, metadata)

def update_project_last_accessed(username: str, project: str):
    """Update project's last_accessed timestamp"""
    metadata = load_project_metadata(username, project)
    metadata["last_accessed"] = datetime.now(timezone.utc).isoformat()
    save_project_metadata(username, project, metadata)

# ============================================================
# Request/Response Models
# ============================================================

class LoginRequest(BaseModel):
    username: str

class LoginResponse(BaseModel):
    username: str
    has_api_key: bool
    is_new_user: bool

class ApiKeyRequest(BaseModel):
    username: str
    api_key: str

class ApiKeysRequest(BaseModel):
    """Request to set multiple API keys."""
    username: str
    openai_key: str | None = None
    anthropic_key: str | None = None
    perplexity_key: str | None = None

class ApiKeysResponse(BaseModel):
    """Response showing which API keys are configured."""
    has_openai: bool
    has_anthropic: bool
    has_perplexity: bool = False

class SetChatModelRequest(BaseModel):
    """Request to change a chat's model."""
    username: str
    chat_name: str
    project: str | None = None
    model: str

class SetProjectModelRequest(BaseModel):
    """Request to change a project's default model."""
    username: str
    project: str
    model: str

class SetProjectGameSystemRequest(BaseModel):
    """Request to change a project's game system."""
    username: str
    project: str
    game_system: str

class ModelInfo(BaseModel):
    """Information about an available model."""
    id: str
    name: str
    pricing: dict
    context_limits: dict

class ChatListResponse(BaseModel):
    chats: list[str]
    projects: list[str]
    total: int
    has_more: bool

class CreateChatRequest(BaseModel):
    username: str
    chat_name: str
    project: str | None = None
    model: str | None = None  # Optional: inherit model selection when creating chat

class CreateProjectRequest(BaseModel):
    username: str
    project_name: str

class ProjectChatsResponse(BaseModel):
    chats: list[str]
    total: int
    has_more: bool

class ChatSummary(BaseModel):
    name: str
    last_message: str
    last_active: str
    message_count: int
    cost: float

class ProjectChatsDetailedResponse(BaseModel):
    chats: list[ChatSummary]
    total: int
    has_more: bool

class AttachedFile(BaseModel):
    filename: str
    content: str  # text body for text files; base64-encoded data for binary (images)
    mime_type: str | None = None  # e.g. "image/png" — when set with image/* type, treated as image attachment


def _is_image_attachment(f: dict) -> bool:
    """True if `f` is an image attached_file (mime_type begins with 'image/')."""
    return (f.get("mime_type") or "").startswith("image/")


def _attached_file_text_repr(f: dict) -> str:
    """Text representation of an attached file. For images, just a placeholder
    so token counters don't choke on base64 blobs and history rendering reads
    cleanly. For text files, the existing ====FILE:==== wrapper format."""
    if _is_image_attachment(f):
        return f"[image: {f['filename']}]"
    return f"====FILE: {f['filename']}====\n{f.get('content', '')}\n====END FILE===="


def build_image_content_blocks(msg: dict) -> list[dict]:
    """Pull image attachments off a message and convert to Anthropic image
    content blocks (base64 source). Returns [] if none. The latest user message
    sends these as real image blocks; historical messages use the text
    placeholder above so we don't pay for vision tokens on every turn."""
    attached = msg.get("attached_files") or []
    blocks = []
    for f in attached:
        if not _is_image_attachment(f):
            continue
        data = f.get("content")
        mt = f.get("mime_type")
        if not data or not mt:
            continue
        blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mt, "data": data},
        })
    return blocks


# Image URLs the user pastes directly in their message ("hey check this
# out https://i.imgur.com/abc.png") — server-side fetched and added as
# image content blocks alongside the text. Same pipeline as a file attach.
_IMAGE_URL_REGEX = re.compile(
    r'https?://[^\s<>"\']+\.(?:jpe?g|png|gif|webp)(?:\?[^\s<>"\']*)?',
    re.IGNORECASE
)
_IMGUR_DIRECT_REGEX = re.compile(
    r'https?://i\.imgur\.com/[A-Za-z0-9]+(?:\.[a-z]{3,4})?(?:\?[^\s<>"\']*)?',
    re.IGNORECASE
)
_IMAGE_FETCH_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_FETCH_TIMEOUT_S = 6
_MAX_IMAGES_PER_MESSAGE = 3


def extract_image_urls_from_text(text: str) -> list[str]:
    """Return distinct image URLs that appear in `text`. Conservative — only
    URLs that look like direct image links (extension-bearing or i.imgur.com)."""
    if not text or not isinstance(text, str):
        return []
    found: list[str] = []
    seen = set()
    for m in _IMAGE_URL_REGEX.finditer(text):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            found.append(u)
    for m in _IMGUR_DIRECT_REGEX.finditer(text):
        u = m.group(0)
        if u not in seen:
            seen.add(u)
            found.append(u)
    return found


def fetch_image_url_as_block(url: str) -> dict | None:
    """Fetch an image URL and convert to an Anthropic base64 image content block.
    Returns None on any failure (network, non-image content-type, oversize)."""
    import requests
    import base64
    try:
        resp = requests.get(
            url, timeout=_IMAGE_FETCH_TIMEOUT_S, stream=True, allow_redirects=True,
            headers={"User-Agent": "Chorus-Characters/1.0"},
        )
    except Exception as e:
        logger.warning(f"Image URL fetch failed: {url} — {e}")
        return None
    if resp.status_code != 200:
        logger.info(f"Image URL non-200: {url} → {resp.status_code}")
        return None
    ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
    if not ct.startswith("image/"):
        logger.info(f"Image URL non-image content-type ({ct}): {url}")
        return None
    if ct == "image/jpg":
        ct = "image/jpeg"
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            break
        total += len(chunk)
        if total > _IMAGE_FETCH_MAX_BYTES:
            logger.info(f"Image URL too large (>{_IMAGE_FETCH_MAX_BYTES} bytes): {url}")
            return None
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        return None
    b64 = base64.b64encode(data).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": ct, "data": b64},
    }

class SendMessageRequest(BaseModel):
    username: str
    chat_name: str
    message: str
    project: str | None = None
    parent_id: str | None = None  # If provided, create message as child of this parent (for branching edits)
    truncate_to_index: int | None = None  # DEPRECATED: kept for backwards compatibility, prefer parent_id
    attached_files: list[AttachedFile] | None = None  # Optional list of files to attach to this message
    model: str | None = None  # Model to use (defaults to chat's saved model or gpt-5.2)

class ChatMessage(BaseModel):
    model_config = {"extra": "allow"}  # Pass through mode flags, debug fields, etc.

    id: str | None = None  # Unique message ID (for branching)
    parent_id: str | None = None  # ID of parent message (for branching)
    role: str
    content: str
    timestamp: str | None = None
    tokens: str | None = None
    cost: str | None = None
    reasoning: str | None = None
    total_tokens: int | None = None  # Current model's token count (for backwards compat)
    total_gpt_tokens: int | None = None  # Accurate GPT tokens (tiktoken)
    total_claude_tokens: int | None = None  # Accurate Claude tokens (API tokenizer)
    attached_files: list[AttachedFile] | None = None  # Files attached to this message
    model: str | None = None  # Model used for this message (for multi-model chats)
    service_tier: str | None = None  # OpenAI service tier (flex or standard)
    bookmark: str | None = None  # User-defined bookmark annotation
    events_stage: str | None = None  # Pipeline: raw Events JSON (for debugging)
    mechanics_stage: str | None = None  # Pipeline: raw Mechanics JSON (for debugging)

class ChatResponse(BaseModel):
    messages: list[ChatMessage]  # Current branch path (paginated, for display)
    all_messages: list[ChatMessage] | None = None  # Full message tree (for branch navigation)
    stats: dict
    total_messages: int
    has_more_messages: bool
    current_leaf_id: str | None = None  # ID of the current leaf message (for branching)
    model: str | None = None  # Model used for this chat (gpt-5.2 or claude-sonnet-4.5)
    anthropic_sync: bool | None = None  # Whether Anthropic caching is enabled (sync mode)
    pipeline_state: dict | None = None  # Character/scene state for right panel
    game_system: str | None = None  # Game system ID for this chat's project
    hack_state: dict | None = None  # Active hack encounter state for overlay
    artifacts: dict | None = None  # Document artifacts for Novels system

class MessageResponse(BaseModel):
    assistant_message: str
    tokens: str
    cost: str
    stats: dict
    context_start_index: int  # Index of first message in context (for frontend graying)
    reasoning: Optional[str] = None  # Reasoning summary from model
    user_message_id: Optional[str] = None  # ID of the user message (for branching)
    assistant_message_id: Optional[str] = None  # ID of the assistant message (for branching)
    current_leaf_id: Optional[str] = None  # ID of the new current leaf
    total_messages: Optional[int] = None  # Total messages in the new branch (for pagination after edits)
    model: Optional[str] = None  # Model used for this response

class ProjectFileInfo(BaseModel):
    filename: str
    tokens: int
    size_bytes: int
    staged: bool = True
    agents: list[str] = PIPELINE_AGENT_NAMES

class UpdateFileAgentsRequest(BaseModel):
    agents: list[str]

class ProjectFilesResponse(BaseModel):
    files: List[ProjectFileInfo]
    total_tokens: int
    staged_tokens: int

class ProjectInstructionsResponse(BaseModel):
    instructions: str
    tokens: int

class UpdateInstructionsRequest(BaseModel):
    instructions: str

# ============================================================
# API Endpoints
# ============================================================

@app.get("/")
def root():
    return {"status": "ok", "message": "ChatGPT Web Interface API"}


# ============================================================
# WebSocket Endpoint for Real-Time Chat Sync
# ============================================================

@app.websocket("/api/ws/user/{username}")
async def user_websocket(websocket: WebSocket, username: str):
    """
    WebSocket endpoint for user-level events (chat list changes).

    Receives events like chat_created and chat_deleted without needing
    a specific chat open. Always connected when user is logged in.
    """
    username = username.strip().lower()

    await websocket.accept()

    connection_id = await sync_manager.register_user_connection(username, websocket)

    try:
        # Handle incoming messages (ping/pong for keepalive)
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)

                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                # Ignore malformed messages
                pass

    finally:
        await sync_manager.unregister_user_connection(username, connection_id)


@app.websocket("/api/ws/chat/{username}/{chat_name}")
async def chat_websocket(websocket: WebSocket, username: str, chat_name: str, project: str = None):
    """
    WebSocket endpoint for real-time chat synchronization.

    Enables multiple browser instances viewing the same chat to stay in sync.
    Broadcasts events when messages are added, edited, or branches are switched.
    """
    username = username.strip().lower()

    # Verify chat exists
    data = load_chat(username, chat_name, project)
    if not data:
        await websocket.close(code=4004, reason="Chat not found")
        return

    await websocket.accept()

    chat_key = sync_manager.make_chat_key(username, project, chat_name)
    connection_id = await sync_manager.register_connection(chat_key, username, websocket)

    try:
        # Broadcast join event to other clients
        count = await sync_manager.get_connection_count(chat_key)
        await sync_manager.broadcast_to_chat(
            chat_key,
            SyncEvent(
                type=SyncEventType.CLIENT_JOINED,
                data={"connection_count": count}
            ),
            exclude_connection_id=None  # Send to all including the new client
        )

        # Handle incoming messages (ping/pong for keepalive)
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)

                if data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))

            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                # Ignore malformed messages
                pass

    finally:
        # Unregister and broadcast leave event
        remaining = await sync_manager.unregister_connection(chat_key, username, connection_id)

        # Broadcast leave event to remaining clients
        if remaining > 0:
            await sync_manager.broadcast_to_chat(
                chat_key,
                SyncEvent(
                    type=SyncEventType.CLIENT_LEFT,
                    data={"connection_count": remaining}
                )
            )


@app.post("/api/login", response_model=LoginResponse)
def login(request: LoginRequest):
    username = request.username.strip().lower()
    
    if not username or len(username) < 2:
        raise HTTPException(status_code=400, detail="Username must be at least 2 characters")
    
    if not username.isalnum():
        raise HTTPException(status_code=400, detail="Username must be alphanumeric")
    
    is_new = ensure_user_exists(username)
    has_key = get_api_key(username, "openai") is not None or get_api_key(username, "anthropic") is not None
    
    return LoginResponse(username=username, has_api_key=has_key, is_new_user=is_new)

@app.post("/api/set-api-key")
def set_api_key_endpoint(request: ApiKeyRequest):
    """Legacy endpoint for backwards compatibility - sets OpenAI key only."""
    username = request.username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    save_api_key(username, request.api_key)
    return {"status": "ok"}

@app.post("/api/set-api-keys", response_model=ApiKeysResponse)
def set_api_keys(request: ApiKeysRequest):
    """Set API keys for multiple providers."""
    username = request.username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    keys = load_api_keys(username)

    # Update only the keys that were provided
    if request.openai_key is not None:
        keys["openai"] = request.openai_key
    if request.anthropic_key is not None:
        keys["anthropic"] = request.anthropic_key
    if request.perplexity_key is not None:
        keys["perplexity"] = request.perplexity_key

    save_api_keys(username, keys)

    return ApiKeysResponse(
        has_openai=bool(keys.get("openai")),
        has_anthropic=bool(keys.get("anthropic")),
        has_perplexity=bool(keys.get("perplexity")),
    )

@app.get("/api/api-keys/{username}", response_model=ApiKeysResponse)
def get_api_keys_status(username: str):
    """Get which API keys are configured (not the keys themselves)."""
    username = username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    keys = load_api_keys(username)
    return ApiKeysResponse(
        has_openai=bool(keys.get("openai")),
        has_anthropic=bool(keys.get("anthropic")),
        has_perplexity=bool(keys.get("perplexity")),
    )

@app.get("/api/models", response_model=list[ModelInfo])
def list_models():
    """List all available AI models."""
    return ProviderRegistry.list_models()

@app.post("/api/set-chat-model")
async def set_chat_model(request: SetChatModelRequest):
    """Set the model for a specific chat."""
    username = request.username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    # Validate model exists
    provider = ProviderRegistry.get(request.model)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model}")

    # Check if user has the required API key
    required_key = ProviderRegistry.get_required_api_key(request.model)
    if not get_api_key(username, required_key):
        raise HTTPException(
            status_code=400,
            detail=f"API key for {required_key} not configured"
        )

    # Load and update chat
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    old_model = data.get("model")
    data["model"] = request.model

    # If model changed, use cached dual tokens (or backfill if missing)
    if request.model != old_model:
        model_id = request.model
        token_field = "total_claude_tokens" if model_id.startswith("claude") else "total_gpt_tokens"

        # Get API key for Claude API tokenizer if switching to Claude
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))

        for msg in data["messages"]:
            if msg.get(token_field) is None:
                # Old message without this token field - count accurately and cache
                # Build content in the same format as build_message_content (files before message)
                base_content = msg.get("content", "")
                attached = msg.get("attached_files", [])
                if attached:
                    wrappers = [_attached_file_text_repr(f) for f in attached]
                    content = "\n\n".join(wrappers) + "\n\n" + base_content
                else:
                    content = base_content

                if model_id.startswith("claude"):
                    # Use API tokenizer for Claude (accurate)
                    tokens = provider.count_tokens_api(content, api_key)
                else:
                    # Use tiktoken for GPT (accurate)
                    tokens = provider.count_tokens(content)
                msg[token_field] = tokens

            # Update total_tokens to point to current model's count
            msg["total_tokens"] = msg.get(token_field)

    save_chat(username, request.chat_name, data, request.project)

    # Calculate new context_start_index for the UI gray out effect
    context_start_index = 1
    if len(data["messages"]) > 1:
        # Get the current branch path (from root to current leaf)
        current_leaf_id = data.get("current_leaf_id")
        if current_leaf_id:
            branch_path = get_path_to_root(data["messages"], current_leaf_id)
        else:
            branch_path = data["messages"]

        context_limits = provider.context_limits
        token_counter = getattr(provider, 'count_tokens_buffered', provider.count_tokens)
        context_start_index = calculate_context_window(
            branch_path,
            threshold=context_limits.target,  # Use target as threshold on switch to avoid immediate re-trim
            target=context_limits.target,
            count_tokens_fn=token_counter
        )

    # Broadcast model change to other sessions viewing this chat
    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.CHAT_SETTINGS_CHANGED,
            data={"model": request.model, "context_start_index": context_start_index}
        )
    )

    return {"status": "ok", "model": request.model, "context_start_index": context_start_index}

class SetBookmarkRequest(BaseModel):
    username: str
    chat_name: str
    message_id: str
    bookmark: str  # Empty string = remove bookmark
    project: str | None = None

@app.post("/api/set-bookmark")
async def set_bookmark(request: SetBookmarkRequest):
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    found = False
    for msg in data.get("messages", []):
        if msg.get("id") == request.message_id:
            if request.bookmark.strip():
                msg["bookmark"] = request.bookmark.strip()
            else:
                msg.pop("bookmark", None)
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail="Message not found")

    save_chat(username, request.chat_name, data, request.project)

    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,
            data={"message_id": request.message_id, "bookmark": request.bookmark.strip()}
        )
    )

    return {"success": True}

class SetMessageStagedRequest(BaseModel):
    username: str
    chat_name: str
    message_id: str
    staged: bool
    project: str | None = None

@app.post("/api/set-message-staged")
async def set_message_staged(request: SetMessageStagedRequest):
    """Toggle whether a message pair is included in API context (Novels manual staging)."""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    found = False
    for msg in data.get("messages", []):
        if msg.get("id") == request.message_id:
            if request.staged:
                msg.pop("staged", None)  # Default is staged — remove field
            else:
                msg["staged"] = False
            found = True
            break

    if not found:
        raise HTTPException(status_code=404, detail="Message not found")

    save_chat(username, request.chat_name, data, request.project)

    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,  # Reuse existing event type for message field updates
            data={"message_id": request.message_id, "staged": request.staged}
        )
    )

    return {"success": True}


class UnstageAllRequest(BaseModel):
    username: str
    chat_name: str
    project: str | None = None

@app.post("/api/unstage-all-messages")
async def unstage_all_messages(request: UnstageAllRequest):
    """Set all non-system messages to staged=False (Novels manual staging)."""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    count = 0
    for msg in data.get("messages", []):
        if msg.get("role") != "system" and msg.get("staged") is not False:
            msg["staged"] = False
            count += 1

    if count > 0:
        save_chat(username, request.chat_name, data, request.project)

    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,
            data={"unstage_all": True}
        )
    )

    return {"success": True, "count": count}


class SetArtifactPinnedRequest(BaseModel):
    username: str
    chat_name: str
    doc_id: str
    pinned: bool
    project: str | None = None

@app.post("/api/set-artifact-pinned")
async def set_artifact_pinned(request: SetArtifactPinnedRequest):
    """Toggle whether an artifact's full content is pinned in the system prompt."""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    artifacts = data.get("artifacts", {})
    if request.doc_id not in artifacts:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if request.pinned:
        artifacts[request.doc_id]["pinned"] = True
    else:
        artifacts[request.doc_id].pop("pinned", None)

    save_chat(username, request.chat_name, data, request.project)

    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BOOKMARK_UPDATED,
            data={"artifact_pinned": request.doc_id, "pinned": request.pinned}
        )
    )

    return {"success": True}


class SetAnthropicSyncRequest(BaseModel):
    username: str
    chat_name: str
    project: str | None = None
    sync: bool


@app.post("/api/set-anthropic-sync")
async def set_anthropic_sync(request: SetAnthropicSyncRequest):
    """Toggle Anthropic prompt caching (sync=True means caching on)."""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")
    data["anthropic_sync"] = request.sync
    save_chat(username, request.chat_name, data, request.project)

    # Broadcast to other sessions viewing this chat
    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.CHAT_SETTINGS_CHANGED,
            data={"anthropic_sync": request.sync}
        )
    )

    return {"status": "ok", "anthropic_sync": request.sync}

@app.get("/api/chats/{username}", response_model=ChatListResponse)
def list_chats(username: str, limit: int = 20, offset: int = 0):
    user_dir = get_user_dir(username)

    if not os.path.exists(user_dir):
        raise HTTPException(status_code=404, detail="User not found")

    # Get actual chat files on disk
    chat_files = set()
    for f in os.listdir(user_dir):
        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
            chat_files.add(f[5:-5])  # Remove "chat_" prefix and ".json" suffix

    # Try to use the index for efficiency
    index = load_chat_index(username, None)

    # Rebuild index if it's missing entries or has stale entries
    indexed_chats = set(index.keys())
    if chat_files != indexed_chats:
        index = rebuild_chat_index(username, None)

    # Build sorted list from index
    chats_with_time = [
        (name, data.get("last_accessed", "1970-01-01T00:00:00"))
        for name, data in index.items()
        if name in chat_files  # Only include chats that still exist
    ]

    # Sort by last_accessed (most recent first)
    chats_with_time.sort(key=lambda x: x[1], reverse=True)
    all_chats = [name for name, _ in chats_with_time]

    # Apply pagination
    total = len(all_chats)
    paginated_chats = all_chats[offset:offset + limit]
    has_more = (offset + limit) < total
    
    # Get projects with their last_accessed timestamps
    projects_with_time = []
    projects_dir = os.path.join(user_dir, "projects")
    if os.path.exists(projects_dir):
        for d in os.listdir(projects_dir):
            project_path = os.path.join(projects_dir, d)
            if os.path.isdir(project_path):
                metadata = load_project_metadata(username, d)
                last_accessed = metadata.get("last_accessed", "1970-01-01T00:00:00")
                projects_with_time.append((d, last_accessed))
    
    # Sort by last_accessed (most recent first)
    projects_with_time.sort(key=lambda x: x[1], reverse=True)
    projects = [name for name, _ in projects_with_time]
    
    return ChatListResponse(chats=paginated_chats, projects=projects, total=total, has_more=has_more)

@app.post("/api/create-chat")
async def create_chat(request: CreateChatRequest):
    username = request.username.strip().lower()
    chat_name = request.chat_name.strip()

    if not validate_name(chat_name):
        raise HTTPException(status_code=400, detail="Invalid chat name. Names cannot contain / \\ or control characters.")

    path = get_chat_path(username, chat_name, request.project)
    if os.path.exists(path):
        raise HTTPException(status_code=400, detail="Chat already exists")

    # Look up game system for build_system_content flags
    gs_kwargs = {}
    if request.project:
        proj_meta = load_project_metadata(username, request.project)
        gs = get_game_system(proj_meta.get("game_system", DEFAULT_GAME_SYSTEM))
        gs_kwargs = _system_content_kwargs(gs)

    # Build system message: instructions + project files + base instructions (at end for salience)
    system_content = build_system_content(username, request.project, **gs_kwargs)

    data = {
        "messages": [{"role": "system", "content": system_content}],
        "stats": create_empty_stats()
    }

    # Set model: priority is request.model > project.model > gamesystem default > user-default
    if request.model:
        data["model"] = request.model
    elif request.project:
        # Inherit from project's default model if set (proj_meta loaded above)
        if proj_meta.get("model"):
            data["model"] = proj_meta["model"]
        elif gs.get("default_model"):
            data["model"] = gs["default_model"]
        else:
            data["model"] = get_default_model_for_user(username)
    else:
        data["model"] = get_default_model_for_user(username)

    # Characters gamesystem: enter interview mode if no profile exists yet
    if request.project and gs.get("is_characters"):
        from characters_runtime import has_character_profile
        project_dir = get_project_dir(username, request.project)
        if not has_character_profile(project_dir):
            data["_characters_interview_mode"] = True
        # Safety-net auto-copy of base_user_life.di → user_life.di. Mostly a
        # no-op (set_project_game_system handles the primary copy when the
        # user picks Characters), but covers projects created before this
        # change shipped, or any path that bypasses the gamesystem-set flow.
        _maybe_copy_base_user_life(username, project_dir)

    save_chat(username, chat_name, data, request.project)

    # Broadcast chat creation to all user's connected clients
    await sync_manager.broadcast_to_user(
        username,
        SyncEvent(
            type=SyncEventType.CHAT_CREATED,
            data={
                "chat_name": chat_name,
                "project": request.project
            }
        )
    )

    return {"status": "ok"}

@app.get("/api/chat/{username}/{chat_name}", response_model=ChatResponse)
def get_chat(username: str, chat_name: str, project: str = None, leaf_id: str = None, limit: int = 30, offset: int = 0):
    """
    Get messages from a chat with pagination.

    For branching support:
    - If leaf_id is provided, returns the path from root to that leaf
    - If not provided, uses current_leaf_id (the last viewed branch)

    Pagination:
    - offset=0 means "last 'limit' messages of the branch"
    - offset=30 means "30 messages before the last 30"
    """
    data = load_chat(username, chat_name, project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Update last_accessed timestamp
    if "stats" not in data:
        data["stats"] = create_empty_stats()
    data["stats"]["last_accessed"] = datetime.now(timezone.utc).isoformat()

    all_messages = data["messages"]
    current_leaf = data.get("current_leaf_id")

    # Determine which leaf to show
    target_leaf = leaf_id or current_leaf

    # Get the branch path (messages from root to target leaf)
    if target_leaf and is_migrated(data):
        branch_messages = get_path_to_root(all_messages, target_leaf)
        # Fallback: if target_leaf points to a deleted/missing message, use last message
        if not branch_messages and all_messages:
            fallback_leaf = all_messages[-1].get("id")
            if fallback_leaf:
                branch_messages = get_path_to_root(all_messages, fallback_leaf)
                data["current_leaf_id"] = fallback_leaf
            else:
                branch_messages = all_messages
    else:
        # Fallback for non-migrated chats or no leaf specified: use linear order
        branch_messages = all_messages

    total_messages = len(branch_messages)

    # Calculate slice for LAST N messages
    # offset=0 means "last 30 messages"
    # offset=30 means "30 messages before the last 30" (i.e., messages 30-60 from end)
    end_idx = max(0, total_messages - offset)  # Ensure non-negative
    start_idx = max(0, end_idx - limit)

    # Return empty list if offset is beyond all messages
    if end_idx <= 0:
        paginated_messages = []
        has_more_messages = False
    else:
        paginated_messages = branch_messages[start_idx:end_idx]
        has_more_messages = start_idx > 0

    save_chat(username, chat_name, data, project)

    # Calculate model-specific stats from messages
    chat_stats = data.get("stats", create_empty_stats()).copy()
    gpt_prompts = 0
    sonnet_prompts = 0
    gpt_context_tokens = 0
    sonnet_context_tokens = 0
    all_chat_messages = data.get("messages", [])
    messages_by_id = {m.get("id"): m for m in all_chat_messages if m.get("id")}
    for msg in all_chat_messages:
        if msg.get("role") == "assistant":
            model = msg.get("model", "")
            is_sonnet = model.startswith("claude")
            parent_id = msg.get("parent_id")
            if is_sonnet:
                sonnet_prompts += 1
                # Use Claude token counts
                assistant_tokens = msg.get("total_claude_tokens") or msg.get("total_tokens", 0) or 0
                user_tokens = 0
                if parent_id and parent_id in messages_by_id:
                    parent = messages_by_id[parent_id]
                    user_tokens = parent.get("total_claude_tokens") or parent.get("total_tokens", 0) or 0
                sonnet_context_tokens += user_tokens + assistant_tokens
            else:
                gpt_prompts += 1
                # Use GPT token counts
                assistant_tokens = msg.get("total_gpt_tokens") or msg.get("total_tokens", 0) or 0
                user_tokens = 0
                if parent_id and parent_id in messages_by_id:
                    parent = messages_by_id[parent_id]
                    user_tokens = parent.get("total_gpt_tokens") or parent.get("total_tokens", 0) or 0
                gpt_context_tokens += user_tokens + assistant_tokens
    chat_stats["gpt_prompts"] = gpt_prompts
    chat_stats["sonnet_prompts"] = sonnet_prompts
    chat_stats["avg_gpt_context_growth"] = gpt_context_tokens / gpt_prompts if gpt_prompts > 0 else 0
    chat_stats["avg_sonnet_context_growth"] = sonnet_context_tokens / sonnet_prompts if sonnet_prompts > 0 else 0

    # Resolve game_system from project metadata
    chat_game_system = None
    if project:
        proj_meta = load_project_metadata(username, project)
        chat_game_system = proj_meta.get("game_system")

    # Auto-recover pipeline_state if it was wiped (e.g., by deleting a mode message)
    if data.get("pipeline_state") is None and branch_messages:
        for msg in reversed(branch_messages):
            if msg.get("role") == "assistant" and "pipeline_state_after" in msg:
                recovered = msg["pipeline_state_after"]
                if isinstance(recovered, str):
                    recovered = json.loads(recovered)
                data["pipeline_state"] = copy.deepcopy(recovered)
                logger.info(f"Auto-recovered pipeline_state for {username}/{chat_name} from message {msg.get('id', '?')}")
                save_chat(username, chat_name, data, project)
                break

    # Only return hack_state if the hack is still active
    active_hack_state = data.get("hack_state")
    if active_hack_state and not active_hack_state.get("active"):
        active_hack_state = None

    # Slim down per-message state metadata in the response. Frontend only reads
    # `msg.pipeline_state_after.hud_state` for the per-message timestamp header.
    # Everything else in `pipeline_state_after` (~10-50 KB/msg) and ALL of
    # `pipeline_state_injected` (~25-50 KB/msg as JSON string) is dead payload —
    # used only for debug-transcript replay, never by the live UI. Stripping
    # cuts response size by ~1.5-2 MB on long chats and removes the per-load
    # parse/render hang. Make a SHALLOW copy of each msg so we don't mutate
    # the on-disk data.
    def _slim_msg(msg: dict) -> dict:
        if not isinstance(msg, dict):
            return msg
        if "pipeline_state_after" not in msg and "pipeline_state_injected" not in msg:
            return msg
        slim = {k: v for k, v in msg.items() if k != "pipeline_state_injected"}
        psa = slim.get("pipeline_state_after")
        if isinstance(psa, dict):
            hud = psa.get("hud_state")
            slim["pipeline_state_after"] = {"hud_state": hud} if isinstance(hud, dict) else None
        elif isinstance(psa, str):
            # String form (older path) — drop it entirely; hud lives elsewhere or in next msg
            slim["pipeline_state_after"] = None
        return slim

    paginated_messages = [_slim_msg(m) for m in paginated_messages]
    # all_messages is used for branch-tree navigation; same slim treatment.
    all_messages = [_slim_msg(m) for m in all_messages]

    # Rebuild-on-read: regenerate CPRED character_states + HUD projections from
    # authoritative edgerunner state. Eliminates the stale-mirror class of bug
    # when game_state is edited out-of-band (manual JSON patches, sync recovery,
    # admin tools). Cost: small dict walk; safe and idempotent.
    pipeline_state_out = data.get("pipeline_state")
    if chat_game_system == "cpred" and isinstance(pipeline_state_out, dict):
        try:
            current_turn = int(pipeline_state_out.get("turn_counter") or 0)
            _rebuild_cpred_projections(pipeline_state_out, current_turn)
        except Exception as e:
            logger.warning(f"get_chat: rebuild_cpred_projections failed for {username}/{chat_name}: {e}")

    return ChatResponse(
        messages=paginated_messages,
        all_messages=all_messages,  # Full tree for branch navigation
        stats=chat_stats,
        total_messages=total_messages,
        has_more_messages=has_more_messages,
        current_leaf_id=data.get("current_leaf_id"),
        model=data.get("model", DEFAULT_MODEL),
        anthropic_sync=data.get("anthropic_sync", True),
        pipeline_state=pipeline_state_out,
        game_system=chat_game_system,
        hack_state=active_hack_state,
        artifacts=data.get("artifacts")
    )

@app.post("/api/send-message", response_model=MessageResponse)
def send_message(request: SendMessageRequest):
    username = request.username.strip().lower()

    # Determine which model to use
    # Priority: request.model > chat.model > DEFAULT_MODEL
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    model_id = request.model or data.get("model", DEFAULT_MODEL)

    # Get the provider for this model
    provider = ProviderRegistry.get(model_id)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")

    # Get the appropriate API key for this provider
    required_key_type = ProviderRegistry.get_required_api_key(model_id)
    api_key = get_api_key(username, required_key_type)

    if not api_key:
        raise HTTPException(status_code=400, detail=f"API key for {required_key_type} not set")

    # Save the model choice to the chat if it was specified in the request
    # When switching models, use cached dual tokens (or backfill if missing)
    old_model = data.get("model")
    model_switched = request.model and request.model != old_model
    if model_switched:
        data["model"] = request.model
        # Determine which token field to use for the new model
        token_field = "total_claude_tokens" if model_id.startswith("claude") else "total_gpt_tokens"

        for msg in data["messages"]:
            if msg.get(token_field) is None:
                # Old message without this token field - count accurately and cache
                # Build content in the same format as build_message_content (files before message)
                base_content = msg.get("content", "")
                attached = msg.get("attached_files", [])
                if attached:
                    wrappers = [_attached_file_text_repr(f) for f in attached]
                    content = "\n\n".join(wrappers) + "\n\n" + base_content
                else:
                    content = base_content

                if model_id.startswith("claude"):
                    # Use API tokenizer for Claude (accurate)
                    tokens = provider.count_tokens_api(content, api_key)
                else:
                    # Use tiktoken for GPT (accurate)
                    tokens = provider.count_tokens(content)
                msg[token_field] = tokens

            # Update total_tokens to point to current model's count
            msg["total_tokens"] = msg.get(token_field)

    all_messages = data["messages"]

    # Determine the parent for the new message (branching support)
    if request.parent_id is not None:
        # Branching: create message as child of specified parent
        parent_id = request.parent_id
        # Validate parent exists
        index = build_message_index(all_messages)
        if parent_id not in index:
            raise HTTPException(
                status_code=400,
                detail=f"Parent message {parent_id} not found"
            )
    elif request.truncate_to_index is not None:
        # DEPRECATED: Legacy truncation mode for backwards compatibility
        # This will be removed in a future version
        total_msgs = len(all_messages)

        if request.truncate_to_index < 1:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid truncation index {request.truncate_to_index}. Must be >= 1 to preserve system message."
            )

        if request.truncate_to_index >= total_msgs:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid truncation index {request.truncate_to_index}. Chat only has {total_msgs} messages."
            )

        # Create backup before destructive truncation
        create_backup(username, request.chat_name, request.project)

        # Find the parent_id of the message at truncate_to_index
        # This is the message BEFORE where we're truncating
        truncate_msg = all_messages[request.truncate_to_index]
        parent_id = truncate_msg.get("parent_id")

        # Actually truncate for backwards compat
        data["messages"] = all_messages[:request.truncate_to_index]
        all_messages = data["messages"]
    else:
        # Normal message: append to current leaf
        current_leaf_id = data.get("current_leaf_id")
        if current_leaf_id:
            parent_id = current_leaf_id
        elif all_messages:
            # Fallback: use last message as parent
            parent_id = all_messages[-1].get("id")
        else:
            parent_id = None

    # Add user message with branching fields
    # Use provider's tokenizer for consistent token counting
    # For Claude, use buffered estimation (fast, conservative for trimming decisions)
    # Accurate count will be calculated after API response
    if model_id.startswith("claude") and hasattr(provider, 'count_tokens_buffered'):
        user_message_tokens = provider.count_tokens_buffered(request.message)
    else:
        user_message_tokens = provider.count_tokens(request.message)

    # Include attached files if present, and add their tokens to the count
    attached_files_data = None
    if request.attached_files:
        attached_files_data = [
            {"filename": f.filename, "content": f.content, "mime_type": f.mime_type}
            for f in request.attached_files
        ]
        # Count tokens for attached files (matching build_message_content format).
        # Image attachments use a placeholder so base64 doesn't get text-counted —
        # vision tokens come back from the actual API call.
        wrappers = [
            _attached_file_text_repr({"filename": f.filename, "content": f.content, "mime_type": f.mime_type})
            for f in request.attached_files
        ]
        files_text = "\n\n".join(wrappers) + "\n\n"
        if model_id.startswith("claude") and hasattr(provider, 'count_tokens_buffered'):
            user_message_tokens += provider.count_tokens_buffered(files_text)
        else:
            user_message_tokens += provider.count_tokens(files_text)
    
    user_msg_id = generate_message_id()
    user_msg_data = {
        "id": user_msg_id,
        "parent_id": parent_id,
        "role": "user",
        "content": request.message,
        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
        "total_tokens": user_message_tokens
    }

    if attached_files_data:
        user_msg_data["attached_files"] = attached_files_data

    data["messages"].append(user_msg_data)
    save_chat(username, request.chat_name, data, request.project)  # Persist user msg before streaming

    # Get provider client
    client = provider.get_client(api_key)

    try:
        # Get the branch path from root to new user message
        # This is the linear conversation that will be sent to the API
        branch_path = get_path_to_root(data["messages"], user_msg_id)

        # Calculate context window using provider-specific limits and tokenizer
        # On model switch, use target as threshold to avoid immediate re-trim on next message
        # Use buffered estimator for trimming to avoid undercounting edge cases
        context_limits = provider.context_limits
        threshold = context_limits.target if model_switched else context_limits.threshold
        token_counter = getattr(provider, 'count_tokens_buffered', provider.count_tokens)
        context_start_index = calculate_context_window(
            branch_path,
            threshold=threshold,
            target=context_limits.target,
            count_tokens_fn=token_counter
        )

        # Helper to build message content with FILE wrappers (and image placeholders).
        # Real image content blocks are built separately via build_image_content_blocks.
        def build_message_content(msg):
            content = msg["content"]
            attached = msg.get("attached_files", [])
            if attached:
                wrappers = [_attached_file_text_repr(f) for f in attached]
                files_text = "\n\n".join(wrappers)
                content = f"{files_text}\n\n{content}"
            return content

        def build_ship_combat_hidden_init_message(parent_id: str, opening_override: str | None = None) -> dict:
            sc_state = (data.get("pipeline_state", {}).get("ship_combat") or {})
            handoff_summary = str(sc_state.get("handoff_summary") or "").strip()
            opening_hint = str(opening_override if opening_override is not None else (sc_state.get("opening_narration") or "")).strip()
            hidden_payload = {
                "handoff_summary": handoff_summary or None,
                "environment": sc_state.get("environment"),
                "encounter_type": sc_state.get("encounter_type"),
                "objective": sc_state.get("objective"),
                "positioning": sc_state.get("positioning"),
                "immediate_complications": sc_state.get("immediate_complications") or [],
                "enemy_ships": sc_state.get("enemy_ships") or [],
            }
            hidden_lines = [
                "This is the current situation for ship combat initialization.",
            ]
            if handoff_summary:
                hidden_lines.append(f"Handoff summary (canonical): {handoff_summary}")
            hidden_lines.append(
                "Initialize ship combat mode: generate participating ships, crews/role coverage, and initiative order based on the fiction, then describe the opening exchange state."
            )
            if opening_hint:
                hidden_lines.append(f"Opening narration hint (optional): {opening_hint}")
            hidden_lines.append("")
            hidden_lines.append(json.dumps(hidden_payload, indent=2))
            return {
                "id": generate_message_id(),
                "parent_id": parent_id,
                "role": "user",
                "content": "\n".join(hidden_lines),
                "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                "ship_combat_mode": True,
                "ship_combat_system_init": True,
                "ship_combat_hidden_init": True,
            }

        # Build messages for API: system + in-context history + new user message
        system_msg = {"role": branch_path[0]["role"], "content": branch_path[0]["content"]}
        history_msgs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in branch_path[context_start_index:-1]]

        # Build user content (with attached files)
        user_content = build_message_content(branch_path[-1])
        new_user_msg = {"role": branch_path[-1]["role"], "content": user_content}

        # Build messages list
        messages_for_api = [system_msg] + history_msgs + [new_user_msg]

        # Build provider-specific request
        is_free_chat = not request.project
        use_cache = data.get("anthropic_sync", True)
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=is_free_chat,
            use_cache=use_cache
        )

        # Send request and parse response
        response = provider.send_request(client, request_params)
        parsed = provider.parse_response(response)

        assistant_message = parsed.content
        reasoning_summary = parsed.reasoning

        # Post-response: Calculate and store dual token counts for both models
        # This enables instant model switching without recounting

        # Get cross-model providers for token counting
        gpt_provider = get_gpt_provider()
        claude_provider = get_claude_provider()
        claude_api_key = get_api_key(username, "anthropic")

        # Build user content (with attached files) for cross-model counting
        # Use same format as build_message_content (files before message)
        if request.attached_files:
            file_wrappers = [f"====FILE: {f.filename}====\n{f.content}\n====END FILE====" for f in request.attached_files]
            user_content_for_counting = "\n\n".join(file_wrappers) + "\n\n" + request.message
        else:
            user_content_for_counting = request.message

        # Ensure system message has accurate dual tokens cached
        # branch_path[0] is a reference to the system message in data["messages"]
        system_msg = branch_path[0]
        system_content = system_msg.get("content", "")
        if system_msg.get("total_gpt_tokens") is None:
            system_msg["total_gpt_tokens"] = gpt_provider.count_tokens(system_content)
        if system_msg.get("total_claude_tokens") is None and claude_api_key:
            # Only cache Claude tokens if we have API key - don't cache estimates
            system_msg["total_claude_tokens"] = claude_provider.count_tokens_api(system_content, claude_api_key)
        # Update system's total_tokens to current model
        if model_id.startswith("claude"):
            system_msg["total_tokens"] = system_msg["total_claude_tokens"]
        else:
            system_msg["total_tokens"] = system_msg["total_gpt_tokens"]

        if model_id.startswith("claude"):
            # Claude response: count user tokens directly via API (accurate)
            # Don't derive from input_tokens - known_tokens, as known_tokens may be
            # inaccurate when switching models (older msgs may have GPT counts only)
            user_claude_tokens = claude_provider.count_tokens_api(user_content_for_counting, api_key)

            # GPT tokens: use tiktoken (accurate, fast)
            user_gpt_tokens = gpt_provider.count_tokens(user_content_for_counting)

            # Update user message with dual token counts
            for msg in data["messages"]:
                if msg.get("id") == user_msg_id:
                    msg["total_claude_tokens"] = user_claude_tokens
                    msg["total_gpt_tokens"] = user_gpt_tokens
                    msg["total_tokens"] = user_claude_tokens  # Current model
                    break

            # Assistant message tokens
            assistant_claude_tokens = parsed.output_tokens
            assistant_gpt_tokens = gpt_provider.count_tokens(parsed.full_output_text or assistant_message)
        else:
            # GPT response: We have accurate GPT tokens from API
            # Calculate user message GPT tokens from API response (input - known)
            # Use cached accurate tokens for system and history
            known_tokens = system_msg["total_gpt_tokens"]
            for msg in branch_path[context_start_index:-1]:
                # Prefer model-specific field, fall back to total_tokens
                # Use explicit None check (not `or`) since 0 is a valid token count
                gpt_tokens = msg.get("total_gpt_tokens")
                known_tokens += gpt_tokens if gpt_tokens is not None else (msg.get("total_tokens") or 0)
            # Ensure non-negative (old cached estimates may be inaccurate)
            user_gpt_tokens = max(0, parsed.input_tokens - known_tokens)

            # Claude tokens: use API tokenizer (accurate, post-response so no latency impact)
            # Only cache if we have API key - don't cache estimates so they'll be
            # recounted accurately when user adds API key and switches to Claude
            if claude_api_key:
                user_claude_tokens = claude_provider.count_tokens_api(user_content_for_counting, claude_api_key)
                assistant_claude_tokens = claude_provider.count_tokens_api(assistant_message, claude_api_key)
            else:
                # No API key - don't cache, will be backfilled on first switch to Claude
                user_claude_tokens = None
                assistant_claude_tokens = None

            # Update user message with dual token counts
            for msg in data["messages"]:
                if msg.get("id") == user_msg_id:
                    msg["total_gpt_tokens"] = user_gpt_tokens
                    if user_claude_tokens is not None:
                        msg["total_claude_tokens"] = user_claude_tokens
                    msg["total_tokens"] = user_gpt_tokens  # Current model
                    break

            # Assistant message tokens
            assistant_gpt_tokens = parsed.output_tokens

        # Calculate tokens and cost using provider pricing
        # input_tokens is TOTAL (including cache), so subtract cache to get non-cached
        new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
        total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens
        total_cost = provider.calculate_cost(parsed)
        tokens_str = provider.format_token_string(parsed)

        # Apply free tokens only for GPT (resets 0:00 UTC)
        # Claude/Anthropic usage is always billed at full cost
        # Defer commit until after save_chat to prevent consuming tokens if save fails
        if model_id.startswith('gpt'):
            actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)
        else:
            actual_cost = total_cost
            cost_str = f"${actual_cost:.6f}"
            pending_usage = None

        # Update stats
        stats = data.get("stats", create_empty_stats())
        stats["total_input_tokens"] += new_input_tokens
        stats["total_cached_tokens"] += parsed.cache_read_tokens
        stats["total_output_tokens"] += parsed.output_tokens
        stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
        stats["total_cost"] += actual_cost  # Use actual cost after free tokens
        stats["total_prompts"] += 1
        stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
        data["stats"] = stats

        # Add assistant message with branching fields and dual token counts
        assistant_msg_id = generate_message_id()
        assistant_msg_data = {
            "id": assistant_msg_id,
            "parent_id": user_msg_id,  # Assistant is child of user message
            "role": "assistant",
            "content": assistant_message,
            "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
            "tokens": tokens_str,
            "cost": cost_str,
            "total_tokens": assistant_claude_tokens if model_id.startswith("claude") else assistant_gpt_tokens,
            "total_gpt_tokens": assistant_gpt_tokens,
            "model": model_id
        }
        # Only store Claude tokens if we have accurate count (not estimation)
        if assistant_claude_tokens is not None:
            assistant_msg_data["total_claude_tokens"] = assistant_claude_tokens
        if reasoning_summary:
            assistant_msg_data["reasoning"] = reasoning_summary

        _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)

        # Update current_leaf_id to the new assistant message
        data["current_leaf_id"] = assistant_msg_id

        save_chat(username, request.chat_name, data, request.project)

        # Commit deferred updates AFTER save succeeds (prevents double-counting on retry)
        if pending_usage is not None:
            save_daily_usage(username, pending_usage)
        # Calculate context tokens (user + assistant) for this prompt
        if model_id.startswith("claude"):
            user_total = user_claude_tokens if user_claude_tokens is not None else 0
            assistant_total = assistant_claude_tokens if assistant_claude_tokens is not None else 0
        else:
            user_total = user_gpt_tokens if user_gpt_tokens is not None else 0
            assistant_total = assistant_gpt_tokens if assistant_gpt_tokens is not None else 0
        context_tokens = user_total + assistant_total
        update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens, parsed.output_tokens, parsed.reasoning_tokens, actual_cost, model=model_id, context_tokens=context_tokens)

        # Calculate total messages in the new branch for frontend pagination
        branch_path = get_path_to_root(data["messages"], assistant_msg_id)
        branch_total_messages = len(branch_path)

        # Calculate model-specific stats for response
        response_stats = stats.copy()
        gpt_prompts = 0
        sonnet_prompts = 0
        gpt_context_tokens = 0
        sonnet_context_tokens = 0
        all_chat_messages = data.get("messages", [])
        messages_by_id = {m.get("id"): m for m in all_chat_messages if m.get("id")}
        for msg in all_chat_messages:
            if msg.get("role") == "assistant":
                msg_model = msg.get("model", "")
                is_sonnet = msg_model.startswith("claude")
                parent_id = msg.get("parent_id")
                if is_sonnet:
                    sonnet_prompts += 1
                    assistant_tokens = msg.get("total_claude_tokens") or msg.get("total_tokens", 0) or 0
                    user_tokens = 0
                    if parent_id and parent_id in messages_by_id:
                        parent = messages_by_id[parent_id]
                        user_tokens = parent.get("total_claude_tokens") or parent.get("total_tokens", 0) or 0
                    sonnet_context_tokens += user_tokens + assistant_tokens
                else:
                    gpt_prompts += 1
                    assistant_tokens = msg.get("total_gpt_tokens") or msg.get("total_tokens", 0) or 0
                    user_tokens = 0
                    if parent_id and parent_id in messages_by_id:
                        parent = messages_by_id[parent_id]
                        user_tokens = parent.get("total_gpt_tokens") or parent.get("total_tokens", 0) or 0
                    gpt_context_tokens += user_tokens + assistant_tokens
        response_stats["gpt_prompts"] = gpt_prompts
        response_stats["sonnet_prompts"] = sonnet_prompts
        response_stats["avg_gpt_context_growth"] = gpt_context_tokens / gpt_prompts if gpt_prompts > 0 else 0
        response_stats["avg_sonnet_context_growth"] = sonnet_context_tokens / sonnet_prompts if sonnet_prompts > 0 else 0

        return MessageResponse(
            assistant_message=assistant_message,
            tokens=tokens_str,
            cost=cost_str,
            stats=response_stats,
            context_start_index=context_start_index,
            reasoning=reasoning_summary,
            user_message_id=user_msg_id,
            assistant_message_id=assistant_msg_id,
            current_leaf_id=assistant_msg_id,
            total_messages=branch_total_messages,
            model=model_id
        )
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is (they have sanitized messages)
        data["messages"].pop()
        raise
    except Exception as e:
        # Remove the user message we added since it failed
        data["messages"].pop()
        # Log full error for debugging, but return sanitized message to client
        logger.error(f"Error in send_message for user {username}: {e}", exc_info=True)
        # Provide user-friendly error without exposing internal details
        error_msg = "Failed to get response from AI. Please try again."
        if "api_key" in str(e).lower() or "authentication" in str(e).lower():
            error_msg = "API key error. Please check your API key is valid."
        elif "rate" in str(e).lower() or "limit" in str(e).lower():
            error_msg = "Rate limit exceeded. Please wait a moment and try again."
        elif "timeout" in str(e).lower():
            error_msg = "Request timed out. Please try again."
        raise HTTPException(status_code=500, detail=error_msg)


def _safe_int(val, default=0):
    """Safe int cast — returns default on any conversion failure."""
    try:
        return int(val)
    except (TypeError, ValueError, OverflowError):
        return default


def _tool_input_valid(tool_input: dict, tool_def: dict) -> bool:
    """Check that all required top-level fields from the tool schema are present."""
    schema = tool_def.get("input_schema") or {}
    required = schema.get("required", [])
    for field in required:
        if field not in tool_input:
            return False
    return True


def _stateful_tool_retry(client, model_name: str, narrative: str, thinking: str, tool_def: dict, state_contract: str = ""):
    """Non-streaming follow-up to force report_state when tool_choice: auto didn't produce it.
    Returns (tool_input_dict_or_None, retry_usage_dict).
    Only sends the state contract + tool def + last narrative — no full conversation history."""
    if thinking:
        assistant_text = f"<reasoning>\n{thinking}\n</reasoning>\n\n{narrative}"
    else:
        assistant_text = narrative

    tool_name = str(tool_def.get("name") or "report_state")
    retry_messages = [
        {"role": "user", "content": f"Here is the narrative from the turn you just wrote:\n\n{assistant_text}\n\nCall {tool_name} now with the state updates for this turn."},
    ]
    # Minimal system prompt: just the state contract so the model knows the schema
    params = {
        "model": model_name,
        "max_tokens": 4096,
        "messages": retry_messages,
        "tools": [tool_def],
        "tool_choice": {"type": "tool", "name": tool_def["name"]},
    }
    if state_contract:
        params["system"] = state_contract
    response = client.messages.create(**params)
    tool_input = None
    for block in response.content:
        if block.type == "tool_use":
            tool_input = block.input
            break
    ru = response.usage
    retry_usage = {
        "input_tokens": ru.input_tokens + (getattr(ru, 'cache_read_input_tokens', 0) or 0) + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }
    return tool_input, retry_usage


def _stateful_narration_recovery(client, model_name: str, contract_system: str,
                                  last_user_text: str, state_tool_input: dict,
                                  tool_use_id: str, tool_name: str):
    """Emergency fallback: model emitted report_state but no narration text.
    Generate the narration based on the state it just produced.
    Returns (narration_text_or_None, recovery_usage_dict).

    Triggered when extended thinking is off and the model elects a tool-only
    response. The contract forbids this on in-fiction turns, but we defend
    against it anyway so the player never sees an empty bubble.
    """
    fallback_id = tool_use_id or "tool_use_recovery"
    fallback_name = tool_name or "report_state"
    messages = [
        {"role": "user", "content": last_user_text or "(continue)"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": fallback_id, "name": fallback_name, "input": state_tool_input},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": fallback_id, "content": "ok"},
            {"type": "text", "text": "You produced the state update above but no narration prose, leaving the player with an empty message. Write the in-fiction narration for this turn now — what the player sees, NPC reactions, scene texture. The narration must be consistent with the state you just emitted. Do NOT call any tools. Output narration prose only."},
        ]},
    ]
    params = {
        "model": model_name,
        "max_tokens": 4096,
        "messages": messages,
    }
    if contract_system:
        params["system"] = contract_system
    response = client.messages.create(**params)
    text = None
    for block in response.content:
        if block.type == "text":
            text = block.text
            break
    ru = response.usage
    recovery_usage = {
        "input_tokens": ru.input_tokens + (getattr(ru, 'cache_read_input_tokens', 0) or 0) + (getattr(ru, 'cache_creation_input_tokens', 0) or 0),
        "cache_read_tokens": getattr(ru, 'cache_read_input_tokens', 0) or 0,
        "cache_creation_tokens": getattr(ru, 'cache_creation_input_tokens', 0) or 0,
        "output_tokens": ru.output_tokens,
    }
    return text, recovery_usage


def _apply_hack_state_compat(apply_fn, hack_state, tool_input, resolver_state_ops=None,
                              game_state=None, pipeline_state=None,
                              username=None, project=None):
    """Call game-system apply_hack_state with only supported kwargs.

    Stuffs `_username` / `_project` into `tool_input` so the plot-doc
    encounter loader (cpred_plot_encounters.load_plot_encounters) can
    resolve the project's uploads dir without changing the apply_fn
    signature across game systems.
    """
    if isinstance(tool_input, dict):
        if username and "_username" not in tool_input:
            tool_input["_username"] = username
        if project and "_project" not in tool_input:
            tool_input["_project"] = project
    kwargs = {}
    try:
        sig = inspect.signature(apply_fn)
        params = sig.parameters
        accepts_var_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if accepts_var_kwargs or "resolver_state_ops" in params:
            kwargs["resolver_state_ops"] = resolver_state_ops
        if accepts_var_kwargs or "game_state" in params:
            kwargs["game_state"] = game_state
        if accepts_var_kwargs or "pipeline_state" in params:
            kwargs["pipeline_state"] = pipeline_state
    except (TypeError, ValueError):
        pass
    return apply_fn(hack_state, tool_input, **kwargs)


def _init_hack_from_trigger(gs, ht, character_states, pipeline_state=None):
    """Initialize hack state from a hack trigger dict. Works for all game systems."""
    def _guess_hacker_name(states):
        """Best-effort picker for which PC is doing the hack."""
        best_name = None
        best_score = -1
        first_pc = None
        for name, entry in (states or {}).items():
            d = entry.get("data", entry)
            if d.get("type") != "pc":
                continue
            if first_pc is None:
                first_pc = name
            score = 0
            cls = str(d.get("class", "")).lower()
            if "netrunner" in cls:
                score += 4
            summary = str(d.get("summary", "")).lower()
            if any(kw in summary for kw in ("cyberdeck", "interface", "neural", "net")):
                score += 1
            for r in d.get("resources", []):
                label = str(r.get("label", "")).lower()
                if any(kw in label for kw in ("cycle", "process", "interface", "program")):
                    score += 2
            if score > best_score:
                best_score = score
                best_name = name
        return best_name or first_pc

    hacker_name = (
        ht.get("hacker_name")
        or ht.get("hacker")
        or ht.get("actor")
        or ht.get("current_player")
        or _guess_hacker_name(character_states)
    )

    # CPRED triggers include cycles/interface directly; others scan resources
    cycles_max = ht.get("cycles_max")
    if cycles_max is None:
        cycles_max = 4
        _candidate_entries = []
        if hacker_name and hacker_name in (character_states or {}):
            _candidate_entries.append((hacker_name, character_states[hacker_name]))
        _candidate_entries.extend((character_states or {}).items())
        _seen = set()
        for _n, _e in _candidate_entries:
            if _n in _seen:
                continue
            _seen.add(_n)
            _d = _e.get("data", _e)
            if _d.get("type") != "pc":
                continue
            _found_cycles = False
            for _r in _d.get("resources", []):
                _rlabel = _r.get("label", "").lower()
                if "cycle" in _rlabel or "process" in _rlabel:
                    cycles_max = _r.get("max", 4)
                    _found_cycles = True
                    break
            if _found_cycles:
                break
    # Extract deck_slots from edgerunner persistent state for hardware auto-population
    deck_slots = None
    if hacker_name and isinstance(pipeline_state, dict):
        _er = pipeline_state.get("game_state", {}).get("edgerunners", {}).get(hacker_name, {})
        deck_slots = _er.get("deck_slots") if isinstance(_er, dict) else None

    return gs["init_hack_state"](
        tier=ht.get("tier", "full_run"),
        target_system=ht.get("target_system", "Unknown"),
        sr=ht.get("sr", 3),
        cycles_max=cycles_max,
        processes_max=cycles_max,
        interface_rank=ht.get("interface_rank") or 4,
        hacker_name=hacker_name,
        context=ht.get("context"),
        deck_slots=deck_slots,
        game_state=pipeline_state.get("game_state", {}) if isinstance(pipeline_state, dict) else {},
    )


def _apply_combat_state(gs, pipeline_state, tool_input):
    """Apply combat updates using game-system handler when available, else legacy fallback."""
    apply_fn = gs.get("apply_combat_state") if gs else None
    if apply_fn:
        apply_fn(pipeline_state, tool_input, game_state=pipeline_state.get("game_state"))
        return

    # Legacy fallback used by systems that only expose contracts/tools.
    for upd in tool_input.get("character_updates", []):
        name = upd.get("name")
        if not name:
            continue
        entry = pipeline_state.get("character_states", {}).get(name)
        if entry is None:
            continue
        d = entry.get("data", entry)

        hp_delta = upd.get("hp_delta")
        if hp_delta is not None:
            vl = upd.get("vital_label", "HP")
            for v in d.get("vitals", []):
                if v.get("label") == vl and "current" in v:
                    v["current"] = max(0, v["current"] + hp_delta)
                    break

        conditions = d.setdefault("conditions", [])
        for cond in upd.get("conditions_add", []):
            if cond not in conditions:
                conditions.append(cond)
        for cond in upd.get("conditions_remove", []):
            if cond in conditions:
                conditions.remove(cond)

    vehicle_updates = tool_input.get("vehicle_updates")
    _has_combat_field = "combat" in tool_input
    new_combat = tool_input.get("combat")
    if tool_input.get("combat_complete") or (_has_combat_field and new_combat is None):
        # Preserve final vehicle deltas before clearing combat.
        _st_combat = pipeline_state.get("combat")
        if vehicle_updates and isinstance(vehicle_updates, list) and isinstance(_st_combat, dict):
            _apply_veh_fn = gs.get("apply_vehicle_updates") if gs else None
            if _apply_veh_fn:
                _apply_veh_fn(_st_combat, vehicle_updates)
            else:
                _apply_vehicle_updates_fallback(_st_combat, vehicle_updates)
        pipeline_state["combat"] = None
    elif isinstance(new_combat, dict):
        _replace_combat_dict_legacy(pipeline_state, new_combat)
        # Apply vehicle deltas after combat replacement to avoid losing updates.
        if vehicle_updates and isinstance(vehicle_updates, list):
            _st_combat = pipeline_state.get("combat")
            if isinstance(_st_combat, dict):
                _apply_veh_fn = gs.get("apply_vehicle_updates") if gs else None
                if _apply_veh_fn:
                    _apply_veh_fn(_st_combat, vehicle_updates)
                else:
                    _apply_vehicle_updates_fallback(_st_combat, vehicle_updates)
    elif vehicle_updates and isinstance(vehicle_updates, list):
        _st_combat = pipeline_state.get("combat")
        if isinstance(_st_combat, dict):
            _apply_veh_fn = gs.get("apply_vehicle_updates") if gs else None
            if _apply_veh_fn:
                _apply_veh_fn(_st_combat, vehicle_updates)
            else:
                _apply_vehicle_updates_fallback(_st_combat, vehicle_updates)


def _replace_combat_dict_legacy(pipeline_state: dict, new_combat: dict) -> None:
    """Replace pipeline_state['combat'] preserving backend-owned keys (legacy fallback).

    Mirrors the same logic in pipeline._replace_combat_dict and cpred._replace_combat_dict.
    Any new backend-owned key must be added to _BACKEND_OWNED_KEYS here too.
    """
    replace_combat_dict_preserving_backend_keys(pipeline_state, new_combat)


def _is_combat_marked_complete(tool_input: dict) -> bool:
    """Return True only when combat is explicitly ended in this tool payload."""
    if not isinstance(tool_input, dict):
        return False
    return bool(tool_input.get("combat_complete")) or (
        "combat" in tool_input and tool_input.get("combat") is None
    )


def _is_net_combat_marked_complete(tool_input: dict, pipeline_state: dict = None) -> bool:
    """Return True when net combat is complete, preferring post-apply pipeline state."""
    if isinstance(pipeline_state, dict):
        nc = pipeline_state.get("net_combat")
        if isinstance(nc, dict):
            if bool(nc.get("combat_complete")) and bool(nc.get("net_complete")):
                return True
            if nc.get("active") is False and bool(nc.get("net_complete")):
                return True
    if not isinstance(tool_input, dict):
        return False
    _nc_combat_done = bool(tool_input.get("combat_complete")) or (
        "combat" in tool_input and tool_input.get("combat") is None
    )
    return bool(_nc_combat_done and tool_input.get("net_complete"))


def _combat_file_list(gs):
    """Combat file order with backward-compatible fallback."""
    if gs and gs.get("combat_files"):
        return gs["combat_files"]
    return ["Core Conversion.md", "Character Sheets.md", "Character Sheets.yaml"]


def _convert_state_ops_to_character_updates(state_ops: list) -> list:
    """Convert resolver state_ops (hp, armor, critical_injury, luck) to character_updates format.

    Groups ops by edgerunner name and builds a character_update dict per combatant.
    """
    by_name = {}
    for op in state_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        subject = state_op_subject(op)
        if not subject or subject["kind"] not in ("edgerunner", "character"):
            continue
        if op_type not in ("add_condition", "remove_condition") and subject["kind"] != "edgerunner":
            continue
        name = subject["name"]
        if name not in by_name:
            by_name[name] = {"name": name}
        upd = by_name[name]

        if op_type == "hp":
            upd["hp_delta"] = upd.get("hp_delta", 0) + int(op.get("change", 0))
        elif op_type == "armor":
            loc = op.get("location", "body")
            armor_delta = upd.get("armor_delta", {})
            armor_delta[loc] = armor_delta.get(loc, 0) + int(op.get("change", 0))
            upd["armor_delta"] = armor_delta
        elif op_type == "critical_injury":
            injuries = upd.get("critical_injury_add", [])
            injuries.append({
                "name": op.get("name", ""),
                "location": op.get("location", "body"),
                "effect": op.get("effect", ""),
                "dv_mod": int(op.get("dv_mod", 0)),
            })
            upd["critical_injury_add"] = injuries
        elif op_type == "luck":
            upd["luck_delta"] = upd.get("luck_delta", 0) + int(op.get("change", 0))
        elif op_type == "ammo":
            ammo_list = upd.get("ammo_consumed", [])
            ammo_list.append({
                "weapon_name": op.get("weapon_name", ""),
                "rounds_consumed": int(op.get("rounds_consumed", 0)),
            })
            upd["ammo_consumed"] = ammo_list
        elif op_type == "add_condition":
            condition = str(op.get("condition", "")).strip()
            if condition:
                upd.setdefault("conditions_add", []).append(condition)
        elif op_type == "remove_condition":
            condition = str(op.get("condition", "")).strip()
            if condition:
                upd.setdefault("conditions_remove", []).append(condition)

    return list(by_name.values())


def _convert_state_ops_to_character_state_deltas(state_ops: list, tracked_edgerunners=None) -> dict:
    """Convert non-authoritative resolver condition ops into character_states deltas."""
    deltas = {}
    tracked_names = set(tracked_edgerunners or []) if isinstance(tracked_edgerunners, (list, tuple, set)) else set()
    for op in state_ops or []:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        if op_type not in ("add_condition", "remove_condition"):
            continue
        subject = state_op_subject(op)
        if not subject or subject["kind"] not in ("character", "edgerunner"):
            continue
        if subject["kind"] == "edgerunner" and subject["name"] in tracked_names:
            continue
        name = subject["name"]
        condition = str(op.get("condition", "")).strip()
        if not name or not condition:
            continue
        entry = deltas.setdefault(name, {})
        if op_type == "add_condition":
            entry.setdefault("_conditions_add", []).append(condition)
        else:
            entry.setdefault("_conditions_remove", []).append(condition)
    return deltas


def _detect_near_duplicate_name(new_name: str, existing_names) -> str | None:
    """Return an existing name if ``new_name`` looks like a variant of it.

    Heuristic: case-insensitive substring match in either direction, tokenized
    on whitespace so "Red" / "RedVelvet" match but "Red" / "Fred" don't.
    Used for logging only — never for auto-merging.
    """
    if not isinstance(new_name, str) or not new_name:
        return None
    new_lower = new_name.lower().strip()
    new_tokens = set(new_lower.split())
    for existing in existing_names:
        if not isinstance(existing, str) or not existing:
            continue
        if existing == new_name:
            continue
        ex_lower = existing.lower().strip()
        if new_lower == ex_lower:
            continue
        ex_tokens = set(ex_lower.split())
        # Shared-token match: any whitespace-delimited token in common is
        # strong evidence of the same character ("Red" vs "RedVelvet",
        # "Kessler" vs "Marcus Kessler", "Shae" vs "Shae Sinclair").
        if new_tokens & ex_tokens:
            return existing
        # No-whitespace merged-name match: "RedVelvet" contains "Red",
        # "MamaLu" contains "Mama".
        if new_lower in ex_lower or ex_lower in new_lower:
            return existing
    return None


def _merge_character_state_deltas(existing: dict, resolver_deltas: dict) -> dict:
    """Merge resolver-generated character condition deltas into character_states payloads."""
    merged = {}
    if isinstance(existing, dict):
        for name, value in existing.items():
            merged[name] = dict(value) if isinstance(value, dict) else value
    for name, delta in (resolver_deltas or {}).items():
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(delta, dict):
            continue
        # Flag near-duplicate names (logging only — do not auto-merge).
        if name not in merged:
            dup = _detect_near_duplicate_name(name, merged.keys())
            if dup:
                logger.warning(
                    "character_states: new key %r looks like a variant of existing key %r; "
                    "state will drift. Update the contract/prompt or clean up manually.",
                    name, dup,
                )
        current = merged.get(name)
        if not isinstance(current, dict):
            current = {}
        else:
            current = dict(current)
        if isinstance(delta.get("_conditions_add"), list):
            current.setdefault("_conditions_add", []).extend(delta["_conditions_add"])
        if isinstance(delta.get("_conditions_remove"), list):
            current.setdefault("_conditions_remove", []).extend(delta["_conditions_remove"])
        merged[name] = current
    return merged


def _merge_character_updates(existing: list, resolver_updates: list) -> list:
    """Merge resolver-generated character_updates into planning-generated ones.

    For matching names, merge deltas additively. New names are appended.
    """
    by_name = {}
    for upd in (existing or []):
        if isinstance(upd, dict) and upd.get("name"):
            by_name[upd["name"]] = upd

    for r_upd in (resolver_updates or []):
        if not isinstance(r_upd, dict):
            continue
        name = r_upd.get("name", "")
        if not name:
            continue
        if name in by_name:
            target = by_name[name]
            # Merge hp_delta
            if "hp_delta" in r_upd:
                try:
                    target["hp_delta"] = int(target.get("hp_delta", 0)) + int(r_upd["hp_delta"])
                except (TypeError, ValueError, OverflowError):
                    pass
            # Merge armor_delta
            if "armor_delta" in r_upd and isinstance(r_upd["armor_delta"], dict):
                t_armor = target.get("armor_delta", {})
                if not isinstance(t_armor, dict):
                    t_armor = {}
                for loc, val in r_upd["armor_delta"].items():
                    try:
                        t_armor[loc] = int(t_armor.get(loc, 0)) + int(val)
                    except (TypeError, ValueError, OverflowError):
                        continue
                target["armor_delta"] = t_armor
            # Merge critical_injury_add
            if "critical_injury_add" in r_upd and isinstance(r_upd["critical_injury_add"], list):
                target.setdefault("critical_injury_add", []).extend(r_upd["critical_injury_add"])
            # Merge luck_delta
            if "luck_delta" in r_upd:
                try:
                    target["luck_delta"] = int(target.get("luck_delta", 0)) + int(r_upd["luck_delta"])
                except (TypeError, ValueError, OverflowError):
                    pass
            # Merge ammo_consumed
            if "ammo_consumed" in r_upd and isinstance(r_upd["ammo_consumed"], list):
                target.setdefault("ammo_consumed", []).extend(r_upd["ammo_consumed"])
            # Merge condition deltas
            if "conditions_add" in r_upd and isinstance(r_upd["conditions_add"], list):
                target.setdefault("conditions_add", []).extend(r_upd["conditions_add"])
            if "conditions_remove" in r_upd and isinstance(r_upd["conditions_remove"], list):
                target.setdefault("conditions_remove", []).extend(r_upd["conditions_remove"])
        else:
            by_name[name] = r_upd

    return list(by_name.values())


def _convert_state_ops_to_vehicle_updates(state_ops: list) -> list:
    """Convert resolver state_ops (vehicle_sdp, vehicle_sp) to vehicle_updates format."""
    by_vehicle = {}
    for op in state_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        if op_type not in ("vehicle_sdp", "vehicle_sp"):
            continue
        vname = state_op_subject_name(op, "vehicle")
        if not vname:
            continue
        vkey = vname.casefold()
        if vkey not in by_vehicle:
            by_vehicle[vkey] = {"name": vname}
        vupd = by_vehicle[vkey]
        if op_type == "vehicle_sdp":
            try:
                vupd["sdp_delta"] = vupd.get("sdp_delta", 0) + int(op.get("change", 0))
            except (TypeError, ValueError, OverflowError):
                pass
        elif op_type == "vehicle_sp":
            try:
                vupd["sp_delta"] = vupd.get("sp_delta", 0) + int(op.get("change", 0))
            except (TypeError, ValueError, OverflowError):
                pass
    return list(by_vehicle.values())


def _collect_relationship_present_names(actions: list, pipeline_state: dict) -> set[str]:
    """Collect current combat participants for relationship mechanics."""
    return collect_relationship_present_names(
        actions=actions,
        combat=pipeline_state.get("combat") if isinstance(pipeline_state, dict) else None,
        character_states=pipeline_state.get("character_states") if isinstance(pipeline_state, dict) else None,
    )



def _merge_vehicle_updates(existing: list, resolver_updates: list) -> list:
    """Merge resolver-generated vehicle_updates into planning-generated ones.

    For matching names, merge sdp_delta/sp_delta additively. New names are appended.
    Preserves model judgment fields (occupants, driver, status, set_vehicle_stats).
    """
    by_name = {}

    def _merge_into(target: dict, src: dict) -> None:
        if not isinstance(target, dict) or not isinstance(src, dict):
            return
        if "sdp_delta" in src:
            try:
                target["sdp_delta"] = int(target.get("sdp_delta", 0)) + int(src["sdp_delta"])
            except (TypeError, ValueError, OverflowError):
                pass
        if "sp_delta" in src:
            try:
                target["sp_delta"] = int(target.get("sp_delta", 0)) + int(src["sp_delta"])
            except (TypeError, ValueError, OverflowError):
                pass
        for k, v in src.items():
            if k in ("name", "sdp_delta", "sp_delta"):
                continue
            if k not in target:
                target[k] = v

    for upd in (existing or []):
        if isinstance(upd, dict) and upd.get("name"):
            _name = str(upd.get("name", "")).strip()
            if not _name:
                continue
            _key = _name.casefold()
            if _key in by_name:
                _merge_into(by_name[_key], upd)
            else:
                by_name[_key] = upd

    for r_upd in (resolver_updates or []):
        if not isinstance(r_upd, dict):
            continue
        name = str(r_upd.get("name", "")).strip()
        if not name:
            continue
        name_key = name.casefold()
        if name_key in by_name:
            _merge_into(by_name[name_key], r_upd)
        else:
            by_name[name_key] = r_upd

    return list(by_name.values())


def _strip_and_merge_resolver_ops(tool_input: dict, state_ops: list) -> None:
    """Strip dice-dependent fields from model output and merge resolver-computed updates.

    Centralizes the strip+merge logic for modes that use character_updates/vehicle_updates
    tool schemas (combat, net_combat, hack). Always strips dice-dependent fields when
    resolver ran, even if no ops of that type were produced.
    """
    if not isinstance(tool_input, dict):
        return
    state_ops = state_ops or []
    # Strip dice-dependent fields from model's character_updates
    for upd in tool_input.get("character_updates", []):
        if isinstance(upd, dict):
            upd.pop("hp_delta", None)
            upd.pop("armor_delta", None)
            upd.pop("critical_injury_add", None)
            upd.pop("luck_delta", None)
            upd.pop("ammo", None)
            upd.pop("ammo_consumed", None)
    # Strip dice-dependent fields from model's vehicle_updates
    for vupd in tool_input.get("vehicle_updates", []):
        if isinstance(vupd, dict):
            vupd.pop("sdp_delta", None)
            vupd.pop("sp_delta", None)
    # Merge resolver-computed character updates
    resolver_char = _convert_state_ops_to_character_updates(state_ops)
    if resolver_char:
        tool_input["character_updates"] = _merge_character_updates(
            tool_input.get("character_updates", []), resolver_char
        )
    # Merge resolver-computed vehicle updates
    resolver_veh = _convert_state_ops_to_vehicle_updates(state_ops)
    if resolver_veh:
        tool_input["vehicle_updates"] = _merge_vehicle_updates(
            tool_input.get("vehicle_updates", []), resolver_veh
        )
    # Merge resolver-computed edgerunner ops (death_save, etc.)
    _RESOLVER_ER_OPS = {"death_save"}
    existing_er = tool_input.get("edgerunner_ops") or []
    if existing_er:
        tool_input["edgerunner_ops"] = [
            op for op in existing_er
            if not (isinstance(op, dict) and op.get("op") in _RESOLVER_ER_OPS)
        ]
    resolver_er = [op for op in state_ops if isinstance(op, dict) and op.get("op") in _RESOLVER_ER_OPS]
    if resolver_er:
        tool_input["edgerunner_ops"] = (tool_input.get("edgerunner_ops") or []) + resolver_er


def _apply_vehicle_updates_fallback(combat_dict: dict, vehicle_updates: list) -> None:
    """Best-effort fallback applier for vehicle SDP/SP deltas when no hook is present."""
    if not isinstance(combat_dict, dict) or not isinstance(vehicle_updates, list):
        return
    vehicles = combat_dict.setdefault("vehicles", {})
    if not isinstance(vehicles, dict):
        vehicles = {}
        combat_dict["vehicles"] = vehicles

    def _resolve_vehicle_key_case_insensitive(name: str) -> str:
        n = str(name or "").strip()
        if not n:
            return ""
        if n in vehicles:
            return n
        n_cf = n.casefold()
        for existing in vehicles.keys():
            if isinstance(existing, str) and existing.casefold() == n_cf:
                return existing
        return n

    def _normalize_occupants(values):
        if not isinstance(values, list):
            return []
        normalized = []
        for occ in values:
            if isinstance(occ, dict):
                n = occ.get("name")
                if isinstance(n, str):
                    n = n.strip()
                    if n:
                        normalized.append(n)
            elif isinstance(occ, str):
                n = occ.strip()
                if n:
                    normalized.append(n)
        return normalized

    def _normalize_driver(value):
        if value is None:
            return None
        if isinstance(value, str):
            v = value.strip()
            return v or None
        if isinstance(value, dict):
            n = value.get("name")
            if isinstance(n, str):
                n = n.strip()
                return n or None
            return None
        return None

    for upd in vehicle_updates:
        if not isinstance(upd, dict):
            continue
        name = upd.get("name")
        if isinstance(name, str):
            name = name.strip()
        if not isinstance(name, str) or not name:
            continue
        name = _resolve_vehicle_key_case_insensitive(name)

        svs = upd.get("set_vehicle_stats")
        if not isinstance(svs, dict):
            svs = None
        has_sdp_delta = "sdp_delta" in upd
        has_sp_delta = "sp_delta" in upd

        v = vehicles.get(name)
        if not isinstance(v, dict):
            if svs:
                try:
                    sdp_max = max(0, int(svs.get("sdp_max", 0)))
                except (TypeError, ValueError, OverflowError):
                    sdp_max = 0
                try:
                    sp = max(0, int(svs.get("sp", 0)))
                except (TypeError, ValueError, OverflowError):
                    sp = 0
                try:
                    combat_move = max(0, int(svs.get("combat_move", 0)))
                except (TypeError, ValueError, OverflowError):
                    combat_move = 0
                v = {
                    "type": str(svs.get("type", "land") or "land"),
                    "sdp_current": sdp_max,
                    "sdp_max": sdp_max,
                    "sp": sp,
                    "combat_move": combat_move,
                    "occupants": _normalize_occupants(svs.get("occupants", [])),
                    "driver": _normalize_driver(svs.get("driver")),
                    "upgrades": svs.get("upgrades", []) if isinstance(svs.get("upgrades"), list) else [],
                    "status": "active",
                }
                vehicles[name] = v
            elif has_sdp_delta or has_sp_delta:
                # Unknown target for delta-only update: ignore to avoid phantom vehicles.
                continue
            else:
                # Unknown target with no mechanical deltas: ignore.
                continue

        if has_sdp_delta:
            try:
                sdp_delta = int(upd.get("sdp_delta", 0))
            except (TypeError, ValueError, OverflowError):
                sdp_delta = 0
            cur = max(0, int(v.get("sdp_current", 0)))
            new_cur = max(0, cur + sdp_delta)
            v["sdp_current"] = new_cur
            try:
                cur_max = int(v.get("sdp_max", 0))
            except (TypeError, ValueError, OverflowError):
                cur_max = 0
            v["sdp_max"] = max(new_cur, cur_max)
        if has_sp_delta:
            try:
                sp_delta = int(upd.get("sp_delta", 0))
            except (TypeError, ValueError, OverflowError):
                sp_delta = 0
            try:
                cur_sp = int(v.get("sp", 0))
            except (TypeError, ValueError, OverflowError):
                cur_sp = 0
            v["sp"] = max(0, cur_sp + sp_delta)
        if "occupants" in upd and isinstance(upd.get("occupants"), list):
            v["occupants"] = _normalize_occupants(upd.get("occupants"))
        if "driver" in upd:
            v["driver"] = _normalize_driver(upd.get("driver"))
        if "status" in upd:
            st = upd.get("status")
            if isinstance(st, str) and st in ("active", "disabled", "destroyed"):
                v["status"] = st
                if st == "destroyed":
                    v["sdp_current"] = 0
        if int(v.get("sdp_current", 0)) <= 0:
            v["status"] = "destroyed"
        elif v.get("status") not in ("active", "disabled", "destroyed"):
            v["status"] = "active"


def _canonicalize_vehicle_tracking_map(vehicle_map: dict) -> None:
    """Coalesce vehicle tracking keys case-insensitively in place."""
    if not isinstance(vehicle_map, dict) or not vehicle_map:
        return
    merged = {}
    key_style = {}
    for raw_key, raw_val in list(vehicle_map.items()):
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key:
            continue
        canon = key.casefold()
        if canon not in key_style:
            key_style[canon] = key
        out_key = key_style[canon]
        if canon.endswith(":sp"):
            try:
                parsed = max(0, int(raw_val))
            except (TypeError, ValueError, OverflowError):
                parsed = 0
            if out_key in merged and isinstance(merged[out_key], int):
                merged[out_key] = min(merged[out_key], parsed)
            else:
                merged[out_key] = parsed
        else:
            if raw_val is None:
                parsed = None
            else:
                try:
                    parsed = max(0, int(raw_val))
                except (TypeError, ValueError, OverflowError):
                    parsed = None
            if out_key in merged and isinstance(merged[out_key], int) and isinstance(parsed, int):
                merged[out_key] = min(merged[out_key], parsed)
            elif out_key not in merged:
                merged[out_key] = parsed
            elif merged[out_key] is None and isinstance(parsed, int):
                merged[out_key] = parsed
    vehicle_map.clear()
    vehicle_map.update(merged)


def _find_vehicle_tracking_key(vehicle_map: dict, name: str) -> str:
    """Resolve an existing vehicle key case-insensitively."""
    n = str(name or "").strip()
    if not n:
        return ""
    if n in vehicle_map:
        return n
    target = n.casefold()
    for k in vehicle_map.keys():
        if isinstance(k, str) and not k.endswith(":sp") and k.casefold() == target:
            return k
    return ""


def _find_vehicle_tracking_sp_key(vehicle_map: dict, name: str) -> str:
    """Resolve an existing vehicle SP key case-insensitively."""
    base = _find_vehicle_tracking_key(vehicle_map, name) or str(name or "").strip()
    if not base:
        return ""
    sp_key = base + ":sp"
    if sp_key in vehicle_map:
        return sp_key
    target = sp_key.casefold()
    for k in vehicle_map.keys():
        if isinstance(k, str) and k.endswith(":sp") and k.casefold() == target:
            return k
    return ""


def _canonicalize_hp_tracking_map(hp_map: dict) -> None:
    """Coalesce HP tracking keys case-insensitively in place."""
    if not isinstance(hp_map, dict) or not hp_map:
        return
    merged = {}
    key_style = {}
    for raw_key, raw_val in list(hp_map.items()):
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if not key:
            continue
        canon = key.casefold()
        if canon not in key_style:
            key_style[canon] = key
        out_key = key_style[canon]
        try:
            parsed = max(0, int(raw_val))
        except (TypeError, ValueError, OverflowError):
            continue
        if out_key in merged:
            merged[out_key] = min(merged[out_key], parsed)
        else:
            merged[out_key] = parsed
    hp_map.clear()
    hp_map.update(merged)


def _find_hp_tracking_key(hp_map: dict, name: str) -> str:
    """Resolve an existing HP key case-insensitively."""
    n = str(name or "").strip()
    if not n:
        return ""
    if n in hp_map:
        return n
    target = n.casefold()
    for k in hp_map.keys():
        if isinstance(k, str) and k.casefold() == target:
            return k
    return ""


def _extract_resolve_mechanics_tracking_state(pipeline_state: dict) -> tuple[dict, dict]:
    """Extract HP + vehicle SDP/SP tracking maps for sequential resolver calls."""
    hp_map = {}
    vehicle_map = {}
    if not isinstance(pipeline_state, dict):
        return hp_map, vehicle_map

    game_state = pipeline_state.get("game_state")
    if isinstance(game_state, dict):
        for er_name, er_data in game_state.get("edgerunners", {}).items():
            if not isinstance(er_data, dict):
                continue
            hp = er_data.get("hp", {})
            if isinstance(hp, dict) and "current" in hp:
                try:
                    hp_map[er_name] = int(hp.get("current", 0))
                except (TypeError, ValueError, OverflowError):
                    continue

    character_states = pipeline_state.get("character_states")
    if isinstance(character_states, dict):
        for cs_name, cs_entry in character_states.items():
            if cs_name in hp_map:
                continue
            d = cs_entry.get("data", cs_entry) if isinstance(cs_entry, dict) else {}
            for v in d.get("vitals", []):
                if v.get("label") == "HP" and "current" in v:
                    try:
                        hp_map[cs_name] = int(v.get("current", 0))
                    except (TypeError, ValueError, OverflowError):
                        pass
                    break

    combat = pipeline_state.get("combat")
    vehicles = combat.get("vehicles", {}) if isinstance(combat, dict) else {}
    if isinstance(vehicles, dict):
        for vname, vdata in vehicles.items():
            if not isinstance(vdata, dict):
                continue
            vname = str(vname or "").strip()
            if not vname:
                continue
            try:
                sdp_current = int(vdata.get("sdp_current", 0))
            except (TypeError, ValueError, OverflowError):
                sdp_current = 0
            try:
                sp_current = int(vdata.get("sp", 0))
            except (TypeError, ValueError, OverflowError):
                sp_current = 0
            is_destroyed = vdata.get("status") == "destroyed"
            vehicle_map[vname] = 0 if is_destroyed else max(0, sdp_current)
            vehicle_map[vname + ":sp"] = max(0, sp_current)

    _canonicalize_hp_tracking_map(hp_map)
    _canonicalize_vehicle_tracking_map(vehicle_map)
    return hp_map, vehicle_map


def _advance_tracking_maps_from_state_ops(hp_map: dict, vehicle_map: dict, state_ops: list) -> None:
    """Advance running HP/vehicle tracking maps from resolver state_ops."""
    if not isinstance(state_ops, list):
        return
    _canonicalize_hp_tracking_map(hp_map)
    _canonicalize_vehicle_tracking_map(vehicle_map)
    for op in state_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        if op_type == "hp" and isinstance(hp_map, dict):
            target_name = state_op_subject_name(op, "edgerunner")
            target = _find_hp_tracking_key(hp_map, target_name) or target_name
            if target in hp_map:
                try:
                    hp_map[target] = max(0, int(hp_map[target]) + int(op.get("change", 0)))
                except (TypeError, ValueError, OverflowError):
                    continue
        elif op_type == "vehicle_sdp" and isinstance(vehicle_map, dict):
            vname = state_op_subject_name(op, "vehicle")
            vkey = _find_vehicle_tracking_key(vehicle_map, vname) or vname
            if vkey and vkey not in vehicle_map:
                vehicle_map[vkey] = 0
            if vkey in vehicle_map and isinstance(vehicle_map[vkey], int):
                try:
                    vehicle_map[vkey] = max(0, int(vehicle_map[vkey]) + int(op.get("change", 0)))
                except (TypeError, ValueError, OverflowError):
                    continue
        elif op_type == "vehicle_sp" and isinstance(vehicle_map, dict):
            vname = state_op_subject_name(op, "vehicle")
            vkey = _find_vehicle_tracking_key(vehicle_map, vname) or vname
            sp_key = _find_vehicle_tracking_sp_key(vehicle_map, vkey) or (vkey + ":sp" if vkey else "")
            if vkey and sp_key not in vehicle_map:
                vehicle_map[sp_key] = 0
            if sp_key in vehicle_map and isinstance(vehicle_map[sp_key], int):
                try:
                    vehicle_map[sp_key] = max(0, int(vehicle_map[sp_key]) + int(op.get("change", 0)))
                except (TypeError, ValueError, OverflowError):
                    continue


def _seed_vehicle_tracking_map_from_actions(vehicle_map: dict, actions: list) -> None:
    """Seed running vehicle tracking map from resolve_mechanics action payloads."""
    if not isinstance(vehicle_map, dict) or not isinstance(actions, list):
        return
    _canonicalize_vehicle_tracking_map(vehicle_map)

    def _norm_name(v):
        return str(v or "").strip()

    def _to_nonneg_int(v, default):
        try:
            return max(0, int(v))
        except (TypeError, ValueError, OverflowError):
            return default

    def _as_bool(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "y", "on")
        return False

    def _seed(name, sdp_value=None, sp_value=None):
        n = _norm_name(name)
        if not n:
            return
        k = _find_vehicle_tracking_key(vehicle_map, n) or n
        if k not in vehicle_map:
            vehicle_map[k] = _to_nonneg_int(sdp_value, None)
        elif vehicle_map[k] is None:
            _parsed = _to_nonneg_int(sdp_value, None)
            if isinstance(_parsed, int):
                vehicle_map[k] = _parsed
        sp_key = _find_vehicle_tracking_sp_key(vehicle_map, k) or (k + ":sp")
        if sp_key not in vehicle_map:
            vehicle_map[sp_key] = _to_nonneg_int(sp_value, 0)

    for action in actions:
        if not isinstance(action, dict):
            continue
        a_type = action.get("type")
        if a_type == "ramming":
            _seed(action.get("vehicle_name"), action.get("vehicle_sdp_current"), action.get("vehicle_sp"))
            if _as_bool(action.get("target_is_vehicle", False)):
                _seed(action.get("target"), action.get("target_sdp_current"), action.get("target_sp"))
        elif a_type == "driving_check":
            _seed(action.get("vehicle_name"), action.get("vehicle_sdp_current"), action.get("vehicle_sp"))
        elif a_type == "vehicle_weak_point":
            _seed(action.get("vehicle_name"), action.get("vehicle_sdp_current"), action.get("vehicle_sp"))
        elif a_type == "spike_strip":
            _seed(action.get("target_vehicle_name"), action.get("target_vehicle_sdp_current"), action.get("target_vehicle_sp"))


def _seed_hp_tracking_map_from_actions(hp_map: dict, actions: list) -> None:
    """Seed running HP tracking map from resolve_mechanics action payloads."""
    if not isinstance(hp_map, dict) or not isinstance(actions, list):
        return
    _canonicalize_hp_tracking_map(hp_map)
    for action in actions:
        if not isinstance(action, dict):
            continue
        target = str(action.get("target", "")).strip()
        if not target:
            continue
        target_key = _find_hp_tracking_key(hp_map, target) or target
        if target_key in hp_map:
            continue
        if "target_hp_current" in action:
            try:
                hp_map[target_key] = max(0, int(action.get("target_hp_current", 0)))
            except (TypeError, ValueError, OverflowError):
                continue


def _apply_tar_consumed_state_ops(pipeline_state: dict, state_ops: list) -> None:
    """Apply tar_consumed resolver ops to active hack/net state."""
    if not isinstance(pipeline_state, dict) or not isinstance(state_ops, list):
        return
    if not any(isinstance(op, dict) and op.get("op") == "tar_consumed" for op in state_ops):
        return
    for key in ("hack_state", "net_combat"):
        st = pipeline_state.get(key)
        if isinstance(st, dict) and st.get("active"):
            st["tar_stacks"] = 0


def _apply_virus_op_state_ops(pipeline_state: dict, state_ops: list) -> None:
    """Extract virus_op state ops from resolver state_ops and apply to virus_ledger.

    virus_ledger lives at pipeline_state level, NOT hack_state — so this is
    the routing for resolver-emitted virus_op entries that the standard
    apply_hack_state / apply_net_combat_state helpers don't handle.
    """
    if not isinstance(pipeline_state, dict) or not isinstance(state_ops, list):
        return
    payloads = []
    for op in state_ops:
        if not isinstance(op, dict) or op.get("op") != "virus_op":
            continue
        v_op = op.get("virus_op")
        if isinstance(v_op, dict):
            payloads.append(v_op)
    if not payloads:
        return
    from pipeline import apply_virus_ops
    pipeline_state["virus_ledger"] = apply_virus_ops(
        pipeline_state.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
        payloads,
        pipeline_state.get("turn_counter", 0),
        pipeline_state.get("hud_state", {}).get("date", "")
    )


def _inject_resolver_ops_stateful(tool_input: dict, state_ops: list, pipeline_state: dict, gs: dict) -> None:
    """Inject resolver state_ops into a stateful tool_input dict.

    Strips dice-dependent ops the model may have guessed in edgerunner_ops,
    then merges authoritative resolver ops. Character ops go to edgerunner_ops;
    vehicle ops are applied directly to pipeline_state["combat"]["vehicles"]
    since stateful mode has no vehicle_updates schema.
    """
    if not state_ops:
        return
    # Strip dice-dependent ops the model may have included
    _DICE_OPS = {"hp", "armor", "critical_injury", "luck", "ammo", "vehicle_sdp", "vehicle_sp", "death_save"}
    existing_er_ops = tool_input.get("edgerunner_ops") or []
    if existing_er_ops:
        tool_input["edgerunner_ops"] = [
            op for op in existing_er_ops
            if not (isinstance(op, dict) and op.get("op") in _DICE_OPS)
        ]
    _tracked_edgerunners = set()
    if isinstance(pipeline_state, dict):
        _tracked_edgerunners = set((((pipeline_state.get("game_state") or {}).get("edgerunners")) or {}).keys())
    # Add resolver-authoritative edgerunner ops only; NPC-only condition ops
    # belong in character_states so they cannot synthesize bogus edgerunners.
    _char_ops = [
        op for op in state_ops
        if isinstance(op, dict)
        and op.get("op") not in ("vehicle_sdp", "vehicle_sp")
        and state_op_has_subject_kind(op, "edgerunner", _tracked_edgerunners if _tracked_edgerunners else None)
    ]
    if _char_ops:
        tool_input["edgerunner_ops"] = (tool_input.get("edgerunner_ops") or []) + _char_ops
    _character_state_deltas = _convert_state_ops_to_character_state_deltas(state_ops, tracked_edgerunners=_tracked_edgerunners)
    if _character_state_deltas:
        tool_input["character_states"] = _merge_character_state_deltas(
            tool_input.get("character_states"),
            _character_state_deltas,
        )
    # virus_op state ops (from resolver activate_virus action) → flow into
    # tool_input["virus_ops"] so apply_single_agent_state_updates routes them
    # through apply_virus_ops on the same turn.
    _virus_ops_from_resolver = []
    for _op in state_ops:
        if not isinstance(_op, dict):
            continue
        if _op.get("op") == "virus_op":
            _payload = _op.get("virus_op")
            if isinstance(_payload, dict):
                _virus_ops_from_resolver.append(_payload)
    if _virus_ops_from_resolver:
        existing_v_ops = tool_input.get("virus_ops") or []
        if not isinstance(existing_v_ops, list):
            existing_v_ops = []
        tool_input["virus_ops"] = existing_v_ops + _virus_ops_from_resolver
    # Apply vehicle ops directly to combat state when available.
    # If combat is initialized later in this same tool_input, defer until after apply.
    _veh_updates = _convert_state_ops_to_vehicle_updates(state_ops)
    if _veh_updates:
        _st_combat = pipeline_state.get("combat")
        if isinstance(_st_combat, dict):
            _apply_veh_fn = gs.get("apply_vehicle_updates") if gs else None
            if _apply_veh_fn:
                _apply_veh_fn(_st_combat, _veh_updates)
            else:
                _apply_vehicle_updates_fallback(_st_combat, _veh_updates)
        else:
            tool_input["_resolver_vehicle_updates"] = _merge_vehicle_updates(
                tool_input.get("_resolver_vehicle_updates", []),
                _veh_updates,
            )


def _apply_deferred_stateful_vehicle_updates(tool_input: dict, pipeline_state: dict, gs: dict) -> None:
    """Apply deferred resolver vehicle updates after stateful combat replacement."""
    if not isinstance(tool_input, dict):
        return
    deferred_updates = tool_input.get("_resolver_vehicle_updates")
    if not deferred_updates:
        return
    _st_combat = pipeline_state.get("combat") if isinstance(pipeline_state, dict) else None
    if not isinstance(_st_combat, dict):
        return
    _apply_veh_fn = gs.get("apply_vehicle_updates") if gs else None
    if _apply_veh_fn:
        _apply_veh_fn(_st_combat, deferred_updates)
    else:
        _apply_vehicle_updates_fallback(_st_combat, deferred_updates)
    tool_input.pop("_resolver_vehicle_updates", None)


# ============================================================
# Sex Mode
# ============================================================

SEX_MODE_CONTRACT = """You are narrating an intimate scene in an adult TTRPG campaign. Write with the quality of bestselling literary erotica — explicit, vivid, and grounded in character.

## Voice & Style
- Use character voice profiles from the project files. Each character should sound and react distinctly.
- Vary pacing: build tension, use meaningful pauses, let moments breathe. Not every beat needs to escalate.
- Ground the scene in sensory detail: environment, sounds, textures, temperature, scent.
- Character emotions and internal reactions matter as much as physical description.
- Be explicitly descriptive of bodies, arousal, and physical acts. Name anatomy directly — don't retreat into euphemism or fade-to-black. Describe what characters feel, where they're touched, how they respond. The reader should feel present in the scene.
- Balance the explicit with the emotional. The best erotica works because the physical detail is inseparable from who these people are to each other — their history, their tension, their trust or lack of it. A hand on skin means something different at T3 than at T5.

## Character Fidelity
- Reference character sheets for relevant physical descriptions, cybernetics, mutations, scars, magical features, skills, or spells.
- Respect relationship dynamics from the injected state. Characters at different relationship tiers behave differently.
- NPCs act according to their personality profiles and memories. A guarded character doesn't suddenly become uninhibited without narrative justification.
- For non-human sapient species (Uplifts, beast-kin, aliens, etc.), lean into xenobiology. Invent and describe anatomical differences from human baseline — how their bodies differ in structure, sensitivity, response. Don't default to "basically human but furry." These are distinct species; their physicality should reflect that.

## NPC Agency
- NPCs are active participants. They should take initiative — suggesting, repositioning, escalating, teasing, leading, reacting with authentic desire and personality.
- NPC actions, dialogue, and body language should feel driven by their character, not passive.
- Different NPCs bring different energy: a confident NPC leads differently than a nervous one.

## Player Agency
- The PC's actions, dialogue, and explicit decisions are controlled by the player.
- Narrate the PC's physical sensations and involuntary reactions, but not their choices.
- Don't skip ahead or assume consent to escalation — wait for player input at decision points.
- If the player's message is brief, match that pacing. If they write at length, reciprocate.

## Scene Ending
- When the scene reaches a natural conclusion (characters fall asleep, are interrupted, get dressed, etc.), include the tag [SCENE COMPLETE] at the very end of your response, after your narrative.
- Also include a 1-2 sentence [SCENE SUMMARY: ...] tag capturing what happened for the campaign record.
- Example: [SCENE COMPLETE]
[SCENE SUMMARY: PC and Kira shared an intimate night at the safehouse after the mission. Kira revealed her fear of losing the crew.]

## Vulnerability & Exposure
- Nudity and vulnerability are not neutral states. Characters react to being exposed — and to seeing others exposed — based on who they are. Shyness, bravado, tenderness, nervousness, hunger. Read the character profiles and relationship tier to calibrate.
- If a character entered the scene with conditions like "Partially Nude" or "Nude" from a non-intimate context (combat, interrupted sleep, not having time to dress, etc.), acknowledge the residual awkwardness or charge of that. It carries forward.

## Boundaries
- Follow the tone established by the campaign. Do not introduce content that clashes with the established setting.
"""


def _generate_sex_scene_summary(
    api_key: str,
    scene_messages: list[dict],
    npc_names: list[str],
    handoff_summary: str | None,
) -> str | None:
    """Generate a multi-paragraph summary of a sex scene for context collapse.

    Called when the user manually exits sex mode via /sex so that
    collapse_sex_messages() can produce an informative FADE TO BLACK block
    instead of a bare one.
    """
    try:
        import anthropic

        system_prompt = (
            "You are a campaign record-keeper for an adult TTRPG. "
            "Write a story-shaped exit handoff for the intimate scene below — up to 3 "
            "paragraphs as needed. This summary is the next mode's ONLY context for what "
            "happened during the scene, so it must let the next narrative turn pick up "
            "seamlessly.\n\n"
            "Cover, in order:\n"
            "(1) What happened — the emotional and narrative arc of the scene at a high "
            "level (not a play-by-play). Significant dialogue, confessions, revelations, "
            "promises made. Acknowledge that intimacy occurred and at the highest level "
            "what kind ('they shared a tender night', 'a passionate encounter', 'an "
            "interrupted moment').\n"
            "(2) Anything unexpected — relationship shifts, things said for the first "
            "time, vulnerabilities surfaced, walls that came down or stayed up, choices "
            "that surprised either character.\n"
            "(3) How it ended — where the characters are now (location, emotional state, "
            "physical state at the level of 'dressed/undressed/tangled/asleep'), any "
            "unresolved tension or commitment carried forward, decisions that affect the "
            "story going forward, and any callbacks the next scene should honor.\n\n"
            "What NOT to include:\n"
            "- **No explicit content.** No graphic physical descriptions, no anatomical "
            "detail, no description of acts in progress. This is a campaign record, not "
            "erotica.\n"
            "- No play-by-play of physical actions. Acknowledge that intimacy occurred; "
            "do not narrate it.\n\n"
            "Write in past tense, third person, story-shaped prose. Be specific about "
            "character names. Use additional paragraphs only when the scene had distinct "
            "plot-relevant beats — don't pad."
        )

        # Build user message with handoff context + full scene
        parts = []
        if handoff_summary:
            parts.append(f"Scene context (how the scene began):\n{handoff_summary}")
        if npc_names:
            parts.append(f"NPC participants: {', '.join(npc_names)}")
        parts.append("--- Full scene transcript ---")
        for msg in scene_messages:
            role_label = "Player" if msg["role"] == "user" else "Narrator"
            parts.append(f"{role_label}: {msg['content']}")

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=2048,  # Up to 3 paragraphs in story-shaped prose
            system=system_prompt,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
        summary = response.content[0].text.strip()
        logger.info(f"Sex scene summary generated: {len(summary)} chars")
        return summary
    except Exception:
        logger.exception("Failed to generate sex scene summary")
        return None


def _extract_character_profiles(uploads_dir: str, participants: list[str]) -> str:
    """Extract per-character profile sections from project files for scene participants.

    Parses character-relevant files (Character Descs, Character Sheets, Campaign Bible,
    NPC docs) and returns only the sections matching the given participant names.
    """
    if not participants or not os.path.exists(uploads_dir):
        return ""

    participants_lower = [p.lower() for p in participants]

    def _name_matches_header(header: str) -> bool:
        header_lower = header.lower()
        return any(p in header_lower for p in participants_lower)

    sections: list[str] = []
    seen_keys: set[str] = set()  # (fname_lower, participant) dedup

    for fname in sorted(os.listdir(uploads_dir)):
        lower = fname.lower()
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_FILE_EXTENSIONS:
            continue

        # Determine file category
        is_char_desc = "character desc" in lower
        is_char_sheet = "character sheet" in lower
        is_npc_or_bible = any(kw in lower for kw in ("npc", "campaign bible"))

        if not (is_char_desc or is_char_sheet or is_npc_or_bible):
            continue

        fpath = os.path.join(uploads_dir, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, 'r', encoding='utf-8') as f:
            content = f.read()

        if is_char_sheet:
            # Try YAML format first: split on `# ===` separator lines.
            blocks = re.split(r'^# ={3,}.*$', content, flags=re.MULTILINE)
            if len(blocks) > 1:
                # YAML character sheets with separator lines.
                # Name headers (e.g. "#  REDVELVET — PC Netrunner") are short blocks
                # (1-2 lines of YAML comments) between separators; the next block is
                # the character data.  Skip large blocks (data/metadata) to avoid
                # false matches on names embedded in YAML values.
                for i, block in enumerate(blocks):
                    header_block = block.strip()
                    if not header_block:
                        continue
                    # Name headers are short comment-only blocks (≤3 lines)
                    header_lines = header_block.split('\n')
                    if len(header_lines) > 3:
                        continue
                    if _name_matches_header(header_block):
                        dedup_key = (lower, header_block[:60].lower())
                        if dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            body_block = blocks[i + 1].strip() if i + 1 < len(blocks) else ""
                            merged = header_block + "\n" + body_block if body_block else header_block
                            sections.append(f"[From: {fname}]\n{merged}")
            else:
                # Markdown character sheets: split on h2 headers (## Name)
                parts = re.split(r'^(## .+)$', content, flags=re.MULTILINE)
                for j in range(1, len(parts) - 1, 2):
                    header = parts[j].strip()
                    body = parts[j + 1].strip() if j + 1 < len(parts) else ""
                    if _name_matches_header(header):
                        dedup_key = (lower, header[:60].lower())
                        if dedup_key not in seen_keys:
                            seen_keys.add(dedup_key)
                            sections.append(f"[From: {fname}]\n{header}\n{body}")

        elif is_char_desc:
            # Markdown character descs: split on h2 headers (## NAME)
            parts = re.split(r'^(## .+)$', content, flags=re.MULTILINE)
            # parts: [preamble, header1, body1, header2, body2, ...]
            for j in range(1, len(parts) - 1, 2):
                header = parts[j].strip()
                body = parts[j + 1].strip() if j + 1 < len(parts) else ""
                if _name_matches_header(header):
                    dedup_key = (lower, header[:60].lower())
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        sections.append(f"[From: {fname}]\n{header}\n{body}")

        elif is_npc_or_bible:
            # Campaign Bible / NPC docs: split on h3 headers (### NPC Name)
            parts = re.split(r'^(### .+)$', content, flags=re.MULTILINE)
            for j in range(1, len(parts) - 1, 2):
                header = parts[j].strip()
                body = parts[j + 1].strip() if j + 1 < len(parts) else ""
                if _name_matches_header(header):
                    dedup_key = (lower, header[:60].lower())
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        sections.append(f"[From: {fname}]\n{header}\n{body}")

    if not sections:
        return ""
    return "=" * 60 + "\nCHARACTER PROFILES (Scene Participants)\n" + "=" * 60 + "\n\n" + "\n\n---\n\n".join(sections)


def _build_sex_injection(pipeline_state: dict, sex_scene: dict) -> str:
    """Build injection string for sex mode user messages.

    Includes: scene context, character conditions, NPC memories, relationship state, callback ledger.
    Excludes: pacing, HUD, dice pool, vitals/resources/equipment.
    """
    parts = []
    npcs = sex_scene.get("npcs", [])
    summary = sex_scene.get("summary", "")

    # Scene context
    if summary or npcs:
        scene_lines = ["[SCENE CONTEXT]"]
        if npcs:
            scene_lines.append(f"NPCs present: {', '.join(npcs)}")
        if summary:
            scene_lines.append(f"What led here: {summary}")
        scene_lines.append("[/SCENE CONTEXT]")
        parts.append("\n".join(scene_lines))

    # Character conditions (nudity, injuries, cyberware effects — narratively relevant)
    character_states = pipeline_state.get("character_states", {})
    cond_lines = []
    # Collect conditions for all characters involved (NPCs + PCs)
    for char_name, char_data in character_states.items():
        if not isinstance(char_data, dict):
            continue
        is_npc_in_scene = char_name in npcs
        is_pc = char_data.get("type") == "pc"
        if not (is_npc_in_scene or is_pc):
            continue
        conditions = char_data.get("conditions")
        if conditions:
            cond_lines.append(f"  {char_name}: {', '.join(conditions)}")
    if cond_lines:
        parts.append("[CHARACTER CONDITIONS]\n" + "\n".join(cond_lines) + "\n[/CHARACTER CONDITIONS]")

    # NPC memories (only for NPCs in the scene)
    npc_memories = pipeline_state.get("npc_memories", {})
    for npc_name in npcs:
        memories = npc_memories.get(npc_name, [])
        if memories:
            mem_lines = [f"[NPC MEMORIES: {npc_name}]"]
            for idx, m in enumerate(memories):
                mem_lines.append(f"  [{idx}] (impact {m.get('impact', '?')}) {m.get('text', '')}")
                if m.get("quote"):
                    mem_lines.append(f"       \"{m['quote']}\"")
            mem_lines.append(f"[/NPC MEMORIES: {npc_name}]")
            parts.append("\n".join(mem_lines))

    # Relationship state (for involved NPCs)
    game_state = pipeline_state.get("game_state", {})
    relationships = game_state.get("relationships", {})
    rel_parts = []
    for npc_name in npcs:
        rel = relationships.get(npc_name)
        if rel:
            rs = rel.get("rs", 0)
            roms = rel.get("roms", 0)
            tier = rel.get("tier", "")
            line = f"  {npc_name}: RS {rs}"
            if roms:
                line += f", RomS {roms}"
            if tier:
                line += f" ({tier})"
            rel_parts.append(line)
    if rel_parts:
        parts.append("[RELATIONSHIP STATE]\n" + "\n".join(rel_parts) + "\n[/RELATIONSHIP STATE]")

    # Callback ledger (plot threads may be relevant)
    callback_ledger = pipeline_state.get("callback_ledger")
    callbacks = []
    if isinstance(callback_ledger, dict):
        callbacks = callback_ledger.get("open") or []
    elif isinstance(callback_ledger, list):
        # Backward compatibility for any legacy list-shaped callback state
        callbacks = callback_ledger
    if callbacks:
        cb_lines = ["[CALLBACK LEDGER]"]
        for cb in callbacks:
            if not isinstance(cb, dict):
                continue
            status = cb.get("status", "open")
            if status == "open":
                cb_lines.append(f"  - [{cb.get('id', '?')}] {cb.get('description', '')}")
        cb_lines.append("[/CALLBACK LEDGER]")
        if len(cb_lines) > 2:  # Has at least one open callback
            parts.append("\n".join(cb_lines))

    # Virus ledger: rare in intimate scenes but include for parity — a planted
    # virus may be referenced in pillow talk or post-coital scheming.
    from pipeline import build_virus_ledger_injection
    _v = build_virus_ledger_injection(pipeline_state.get("virus_ledger", {}))
    if _v:
        parts.append(_v)

    return "\n\n".join(parts) if parts else ""


@app.post("/api/send-message-stream")
async def send_message_stream(request: SendMessageRequest, http_request: Request):
    """
    Stream LLM response using Server-Sent Events (SSE).

    Events:
    - init: {user_message_id} - User message created
    - content: {delta} - Text token(s)
    - thinking: {delta} - Reasoning token(s)
    - done: {tokens, cost, stats, ...} - Stream complete
    - error: {detail} - Error occurred
    """
    username = request.username.strip().lower()

    # Determine which model to use
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    model_id = request.model or data.get("model", DEFAULT_MODEL)

    # Get the provider for this model
    provider = ProviderRegistry.get(model_id)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model_id}")

    # Get the appropriate API key for this provider
    required_key_type = ProviderRegistry.get_required_api_key(model_id)
    api_key = get_api_key(username, required_key_type)

    if not api_key:
        raise HTTPException(status_code=400, detail=f"API key for {required_key_type} not set")

    # Save the model choice to the chat if it was specified in the request
    old_model = data.get("model")
    model_switched = request.model and request.model != old_model
    if model_switched:
        data["model"] = request.model
        # User explicitly picked a model — drop any stashed mode-auto-switch
        # original so it doesn't restore over their choice on mode end.
        data.pop("_auto_switched_from", None)
        token_field = "total_claude_tokens" if model_id.startswith("claude") else "total_gpt_tokens"

        for msg in data["messages"]:
            if msg.get(token_field) is None:
                base_content = msg.get("content", "")
                attached = msg.get("attached_files", [])
                if attached:
                    wrappers = [_attached_file_text_repr(f) for f in attached]
                    content = "\n\n".join(wrappers) + "\n\n" + base_content
                else:
                    content = base_content

                if model_id.startswith("claude"):
                    tokens = provider.count_tokens_api(content, api_key)
                else:
                    tokens = provider.count_tokens(content)
                msg[token_field] = tokens

            msg["total_tokens"] = msg.get(token_field)

    all_messages = data["messages"]

    # Determine the parent for the new message (branching support)
    if request.parent_id is not None:
        parent_id = request.parent_id
        index = build_message_index(all_messages)
        if parent_id not in index:
            raise HTTPException(status_code=400, detail=f"Parent message {parent_id} not found")
    elif request.truncate_to_index is not None:
        # DEPRECATED: Legacy truncation mode
        total_msgs = len(all_messages)
        if request.truncate_to_index < 1:
            raise HTTPException(status_code=400, detail=f"Invalid truncation index {request.truncate_to_index}. Must be >= 1.")
        if request.truncate_to_index >= total_msgs:
            raise HTTPException(status_code=400, detail=f"Invalid truncation index {request.truncate_to_index}.")

        create_backup(username, request.chat_name, request.project)
        truncate_msg = all_messages[request.truncate_to_index]
        parent_id = truncate_msg.get("parent_id")
        data["messages"] = all_messages[:request.truncate_to_index]
        all_messages = data["messages"]
    else:
        current_leaf_id = data.get("current_leaf_id")
        if current_leaf_id:
            parent_id = current_leaf_id
        elif all_messages:
            parent_id = all_messages[-1].get("id")
        else:
            parent_id = None

    # Count user message tokens
    if model_id.startswith("claude") and hasattr(provider, 'count_tokens_buffered'):
        user_message_tokens = provider.count_tokens_buffered(request.message)
    else:
        user_message_tokens = provider.count_tokens(request.message)

    # Include attached files
    attached_files_data = None
    if request.attached_files:
        attached_files_data = [{"filename": f.filename, "content": f.content} for f in request.attached_files]
        file_wrappers = [f"====FILE: {f.filename}====\n{f.content}\n====END FILE====" for f in request.attached_files]
        files_text = "\n\n".join(file_wrappers) + "\n\n"
        if model_id.startswith("claude") and hasattr(provider, 'count_tokens_buffered'):
            user_message_tokens += provider.count_tokens_buffered(files_text)
        else:
            user_message_tokens += provider.count_tokens(files_text)

    user_msg_id = generate_message_id()
    user_msg_data = {
        "id": user_msg_id,
        "parent_id": parent_id,
        "role": "user",
        "content": request.message,
        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
        "total_tokens": user_message_tokens
    }

    if attached_files_data:
        user_msg_data["attached_files"] = attached_files_data

    data["messages"].append(user_msg_data)

    # Create chat key for sync broadcasts
    chat_key = sync_manager.make_chat_key(username, request.project, request.chat_name)

    # Get provider client
    client = provider.get_client(api_key)

    # Prepare request parameters before starting the generator
    branch_path = get_path_to_root(data["messages"], user_msg_id)

    # /beat and /session slash-command consumers — fire BEFORE mode dispatch
    # so they work in any mode.  Beat advancement is recorded on the chat's
    # pipeline_state.beat_state and an OOC confirmation line is appended to
    # the user message so the next turn shows what changed.  Sex / hack /
    # combat modes still won't see [CURRENT BEAT] in their prompts (those
    # paths don't include it), so the directive is purely a state-mutation
    # that becomes visible when normal mode resumes.
    try:
        _beat_directive = _consume_beat_prefix(branch_path[-1])
        _session_directive = _consume_session_prefix(branch_path[-1]) if not _beat_directive else None
        if _beat_directive or _session_directive:
            _ps_for_beat = data.setdefault("pipeline_state", {})
            _uploads_for_beat = (
                os.path.join(get_project_dir(username, request.project), "uploads")
                if request.project else None
            )
            _confirm = _apply_beat_directive(
                _ps_for_beat,
                _beat_directive or _session_directive,
                uploads_dir=_uploads_for_beat,
            )
            # Prepend OOC confirmation to the user message so it's visible
            # in this turn's narration ("Beat 2 → 3, model: continue").
            _existing = branch_path[-1].get("content") or ""
            if isinstance(_existing, str):
                branch_path[-1]["content"] = (
                    _confirm + ("\n\n" + _existing if _existing.strip() else "")
                )
    except Exception as _e:
        logger.warning(f"/beat or /session directive failed: {_e}")

    context_limits = provider.context_limits
    threshold = context_limits.target if model_switched else context_limits.threshold
    token_counter = getattr(provider, 'count_tokens_buffered', provider.count_tokens)
    context_start_index = calculate_context_window(
        branch_path,
        threshold=threshold,
        target=context_limits.target,
        count_tokens_fn=token_counter
    )

    def build_message_content(msg):
        content = msg["content"]
        attached = msg.get("attached_files", [])
        if attached:
            wrappers = [_attached_file_text_repr(f) for f in attached]
            files_text = "\n\n".join(wrappers)
            content = f"{files_text}\n\n{content}"
        return content

    def build_ship_combat_hidden_init_message(parent_id: str, opening_override: str | None = None) -> dict:
        sc_state = (data.get("pipeline_state", {}).get("ship_combat") or {})
        handoff_summary = str(sc_state.get("handoff_summary") or "").strip()
        opening_hint = str(opening_override if opening_override is not None else (sc_state.get("opening_narration") or "")).strip()
        hidden_payload = {
            "handoff_summary": handoff_summary or None,
            "environment": sc_state.get("environment"),
            "encounter_type": sc_state.get("encounter_type"),
            "objective": sc_state.get("objective"),
            "positioning": sc_state.get("positioning"),
            "immediate_complications": sc_state.get("immediate_complications") or [],
            "enemy_ships": sc_state.get("enemy_ships") or [],
        }
        hidden_lines = [
            "This is the current situation for ship combat initialization.",
        ]
        if handoff_summary:
            hidden_lines.append(f"Handoff summary (canonical): {handoff_summary}")
        hidden_lines.append(
            "Initialize ship combat mode: generate participating ships, crews/role coverage, and initiative order based on the fiction, then describe the opening exchange state."
        )
        if opening_hint:
            hidden_lines.append(f"Opening narration hint (optional): {opening_hint}")
        hidden_lines.append("")
        hidden_lines.append(json.dumps(hidden_payload, indent=2))
        return {
            "id": generate_message_id(),
            "parent_id": parent_id,
            "role": "user",
            "content": "\n".join(hidden_lines),
            "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
            "ship_combat_mode": True,
            "ship_combat_system_init": True,
            "ship_combat_hidden_init": True,
        }

    def build_chase_hidden_init_message(parent_id: str, opening_override: str | None = None) -> dict:
        """Mirror of build_ship_combat_hidden_init_message for Hot Pursuit.
        Surfaces the chase state's handoff_summary, opening_narration, and
        seeded vehicles/grid for the model on the first chase exchange."""
        ch_state = (data.get("pipeline_state", {}).get("chase") or {})
        handoff_summary = str(ch_state.get("handoff_summary") or ch_state.get("context") or "").strip()
        opening_hint = str(opening_override if opening_override is not None else (ch_state.get("opening_narration") or "")).strip()
        hidden_payload = {
            "handoff_summary": handoff_summary or None,
            "grid_length": ch_state.get("grid_length"),
            "started_from": ch_state.get("started_from"),
            "vehicles": {
                vname: {
                    "operator": v.get("operator"),
                    "occupants": v.get("occupants"),
                    "starting_square": v.get("square"),
                    "combat_speed_move": v.get("combat_speed_move"),
                    "sdp": f"{v.get('sdp_current')}/{v.get('sdp_max')}",
                    "is_pursuer": v.get("is_pursuer"),
                    "type": v.get("type"),
                }
                for vname, v in (ch_state.get("vehicles") or {}).items()
                if isinstance(v, dict)
            },
        }
        hidden_lines = [
            "This is the current situation for Hot Pursuit chase initialization.",
        ]
        if handoff_summary:
            hidden_lines.append(f"Handoff summary (canonical): {handoff_summary}")
        hidden_lines.append(
            "Initialize chase mode: roll Initiative for each operator, narrate the "
            "opening beat (acceleration, environment, immediate threat), and describe "
            "the current grid state. Wait for the player's first action before "
            "resolving Positioning Checks."
        )
        if opening_hint:
            hidden_lines.append(f"Opening narration hint (optional): {opening_hint}")
        hidden_lines.append("")
        hidden_lines.append(json.dumps(hidden_payload, indent=2))
        return {
            "id": generate_message_id(),
            "parent_id": parent_id,
            "role": "user",
            "content": "\n".join(hidden_lines),
            "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
            "chase_mode": True,
            "chase_system_init": True,
            "chase_hidden_init": True,
        }

    # Load game system for this project
    gs = None
    if request.project:
        proj_meta = load_project_metadata(username, request.project)
        gs = get_game_system(proj_meta.get("game_system", DEFAULT_GAME_SYSTEM))
    else:
        gs = get_game_system(DEFAULT_GAME_SYSTEM)

    # Characters gamesystem preflight ────────────────────────────────────
    # Hard-fail on missing character_profile.di, route slash commands,
    # detect interview mode, detect /finalize.
    use_characters = False
    use_characters_interview = False
    characters_finalize_pending = False
    characters_consolidate_pending = False
    characters_slash_feedback: Optional[str] = None
    if gs.get("is_characters") and request.project:
        from characters_runtime import preflight as _characters_preflight
        _characters_project_dir = get_project_dir(username, request.project)
        _pf = _characters_preflight(gs, data, _characters_project_dir, request.message or "")
        if _pf.is_hard_fail():
            # Roll back the appended user message; nothing was saved yet.
            if data["messages"] and data["messages"][-1].get("id") == user_msg_id:
                data["messages"].pop()
            raise HTTPException(
                status_code=412,
                detail={
                    "kind": "characters_profile_missing",
                    "banner": _pf.hard_fail,
                },
            )
        if _pf.local_slash is not None:
            # Channel/resolve/dismiss/reinterview applied state. Prepend OOC feedback
            # to the user message so the character can acknowledge naturally.
            characters_slash_feedback = _pf.local_slash.get("feedback") or ""
            if characters_slash_feedback:
                _existing = branch_path[-1].get("content") or ""
                ooc_line = f"[OOC: {characters_slash_feedback}]"
                branch_path[-1]["content"] = (
                    ooc_line + ("\n\n" + _existing if isinstance(_existing, str) and _existing.strip() else "")
                )
            # Reinterview slash flips us into interview mode for the rest of this turn
            if _pf.local_slash.get("kind") == "reinterview_started":
                use_characters_interview = True
                use_characters = False
                # Mark this message as the start of a fresh interview session so
                # the interview-mode context filter scopes to "since this point."
                branch_path[-1]["_characters_reinterview_boundary"] = True
            else:
                use_characters = True
        elif _pf.finalize:
            characters_finalize_pending = True
            use_characters_interview = True  # we're still in interview mode until finalize succeeds
        elif _pf.consolidate:
            characters_consolidate_pending = True
            use_characters = True  # not interview — consolidate is a correspondence-mode command
        elif _pf.interview_mode:
            use_characters_interview = True
        else:
            use_characters = True

        # Tag the user message with interview mode so context-trimming for correspondence
        # turns can filter out interview Q&A. Without this, prior interview content would
        # leak into the correspondence model's context and confuse it.
        if use_characters_interview:
            branch_path[-1]["_characters_interview_mode"] = True

    # Check if hack mode (matrix encounter) is active or just completed
    use_hack_mode = False
    _hack_to_net_combat = False
    hack_state = data.get("hack_state")

    if hack_state:
        if hack_state.get("active") and gs and gs.get("hack_contract"):
            if hack_state.get("_initiate_combat") and gs.get("init_net_combat_from_hack"):
                _hack_to_net_combat = True
            else:
                use_hack_mode = True
        elif not hack_state.get("active") and hack_state.get("narrative_summary"):
            # Write back hack results to persistent state
            if gs and gs.get("apply_hack_writeback"):
                gs["apply_hack_writeback"](hack_state, data.get("pipeline_state", {}))
            data["hack_state"] = None
            # Restore user's original model choice now that hack mode ended
            # (covers chat-reload-after-mode-end before next message).
            if data.get("_auto_switched_from"):
                _restored = data.pop("_auto_switched_from")
                data["model"] = _restored
                model_id = _restored
                provider = ProviderRegistry.get(model_id)
                api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
                logger.info(f"Hack ended: restored model to {_restored} for {username}")
            logger.info(f"Hack: cleared completed hack_state for {username}")

    # Auto-switch Claude to GPT for hack mode (preserves Anthropic prompt cache)
    _original_model = None
    if use_hack_mode and model_id.startswith("claude"):
        _original_model = model_id
        model_id = COMBAT_AUTO_SWITCH_MODEL
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Hack-mode auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Hack-mode auto-switch: {_original_model} → {model_id} for {username}"
            )
            # Persist so chat reload + dropdown reflect the actually-running
            # model. Stash the user's original choice so we can restore it
            # on mode end (handles the "reload chat after mode ended but
            # before next message" edge case).
            data["model"] = model_id
            data["_auto_switched_from"] = _original_model

    if use_hack_mode:
        # Flag the user message as a hack exchange
        user_msg_data["hack_mode"] = True

    # Hack → NET combat transition: combat breaks out during standalone hack
    if _hack_to_net_combat and hack_state:
        _ps = data.get("pipeline_state", {})
        _combat_info = hack_state.get("_initiate_combat", {})
        # Write back hack results (cycles, destroyed programs) before transitioning
        if gs.get("apply_hack_writeback"):
            gs["apply_hack_writeback"](hack_state, _ps)
        # Create net_combat state with carried-over NET state
        _nc_from_hack = gs["init_net_combat_from_hack"](hack_state, _combat_info)
        _ps["net_combat"] = _nc_from_hack
        # Preserve hack start_message_id for context history
        if hack_state.get("start_message_id"):
            _ps["_hack_start_message_id"] = hack_state["start_message_id"]
        # Clear hack_state — NET state now lives in net_combat
        data["hack_state"] = None
        hack_state = None
        logger.info(f"Hack->NetCombat transition for {username}")

    # NET-in-meatspace combined combat mode
    _ps_for_combat = data.get("pipeline_state", {})
    _net_combat = _ps_for_combat.get("net_combat")
    _combat = _ps_for_combat.get("combat")
    use_net_combat_mode = False
    if (not use_hack_mode) and _net_combat and _net_combat.get("active") and gs and gs.get("net_combat_contract"):
        use_net_combat_mode = True
    elif (not use_hack_mode) and _net_combat and not _net_combat.get("active") and _net_combat.get("narrative_summary"):
        # Net combat completed — write back and clear
        if gs and gs.get("apply_net_combat_writeback"):
            gs["apply_net_combat_writeback"](_net_combat, _ps_for_combat)
        _ps_for_combat["net_combat"] = None
        _ps_for_combat.pop("_hack_start_message_id", None)
        # Restore user's original model on mode end (chat-reload edge case).
        if data.get("_auto_switched_from"):
            _restored = data.pop("_auto_switched_from")
            data["model"] = _restored
            model_id = _restored
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            logger.info(f"Net combat ended: restored model to {_restored} for {username}")
        logger.info(f"Net combat: cleared completed net_combat for {username}")

    # First-exchange initialization: expand trigger fields into full state
    if use_net_combat_mode and _net_combat and "interface_rank" not in _net_combat and gs.get("init_net_combat_state"):
        # net_combat was set by initiate_net_combat from combat mode (just trigger fields)
        nr_name = _net_combat.get("netrunner", "")
        nr_target = _net_combat.get("target", "")
        # Look up interface_rank and cycles from character state
        _nr_cs = _ps_for_combat.get("character_states", {}).get(nr_name, {})
        _nr_d = _nr_cs.get("data", _nr_cs)
        _nr_iface = 4
        _nr_cycles = 3
        for r in _nr_d.get("resources", []):
            rlabel = r.get("label", "").lower()
            if "interface" in rlabel:
                _nr_iface = r.get("current", 4)
            if "cycle" in rlabel:
                _nr_cycles = r.get("current", 3)
        _expanded = gs["init_net_combat_state"](
            netrunner_name=nr_name,
            target=nr_target,
            interface_rank=_nr_iface,
            cycles_max=_nr_cycles,
            initiated_from=_net_combat.get("initiated_from", "combat"),
            game_state=_ps_for_combat.get("game_state", {}),
        )
        # Carry over context from trigger for first-exchange injection
        if _net_combat.get("context"):
            _expanded["context"] = _net_combat["context"]
        # Carry over combat's start_message_id so prior combat context is preserved
        _combat_start = (_combat or {}).get("start_message_id")
        if _combat_start:
            _expanded["start_message_id"] = _combat_start
        _ps_for_combat["net_combat"] = _expanded
        _net_combat = _expanded

    # Auto-switch Claude to GPT for net_combat mode
    if use_net_combat_mode and model_id.startswith("claude"):
        _original_model = model_id
        model_id = COMBAT_AUTO_SWITCH_MODEL
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Net-combat auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Net-combat auto-switch: {_original_model} → {model_id} for {username}"
            )
            data["model"] = model_id

    if use_net_combat_mode:
        user_msg_data["net_combat_mode"] = True

    # Check if combat context mode is active (not when net_combat supersedes)
    use_combat_mode = False
    if (not use_hack_mode) and (not use_net_combat_mode) and _combat and request.project and gs and gs.get("combat_contract"):
        use_combat_mode = True

    # Auto-switch Claude to GPT for combat mode (preserves Anthropic prompt cache)
    if use_combat_mode and model_id.startswith("claude"):
        _original_model = model_id
        model_id = COMBAT_AUTO_SWITCH_MODEL
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Combat auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Combat auto-switch: {_original_model} → {model_id} for {username}"
            )
            data["model"] = model_id

    if use_combat_mode:
        # Flag the user message as a combat exchange
        user_msg_data["combat_mode"] = True

    # Check if ship combat mode is active
    use_ship_combat_mode = False
    _ps_for_ship_combat = data.get("pipeline_state", {})
    _ship_combat = _ps_for_ship_combat.get("ship_combat")
    if (not use_hack_mode) and (not use_combat_mode) and (not use_net_combat_mode) and _ship_combat and request.project and gs and gs.get("ship_combat_contract"):
        use_ship_combat_mode = True

    # Auto-switch Claude to GPT for ship combat mode
    if use_ship_combat_mode and model_id.startswith("claude"):
        _original_model = model_id
        model_id = COMBAT_AUTO_SWITCH_MODEL
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Ship-combat auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Ship-combat auto-switch: {_original_model} → {model_id} for {username}"
            )
            data["model"] = model_id

    if use_ship_combat_mode:
        user_msg_data["ship_combat_mode"] = True

    # ── Hot Pursuit chase mode detection ──
    use_chase_mode = False
    _ps_for_chase = data.get("pipeline_state", {})
    _chase_state = _ps_for_chase.get("chase")
    if (not use_hack_mode) and (not use_combat_mode) and (not use_net_combat_mode) and (not use_ship_combat_mode) and _chase_state and _chase_state.get("active") and request.project and gs and gs.get("chase_contract"):
        use_chase_mode = True
    elif (not use_hack_mode) and (not use_combat_mode) and (not use_net_combat_mode) and (not use_ship_combat_mode) and _chase_state and not _chase_state.get("active") and _chase_state.get("narrative_summary"):
        # Chase completed — route per exit_target_mode, write back vehicle
        # state, clear the chase slot, restore user's original model.
        _exit_target = _chase_state.get("exit_target_mode") or "general"

        # Routing per spec: hostiles alive => combat. If exit is "combat" but
        # no combat slot exists yet (chase started fresh from /chase or
        # general), seed a minimal combat dict so combat mode dispatches on
        # the next exchange. We deliberately leave `initiative_order` empty:
        # the combat injection builder reads HP/armor from edgerunners +
        # character_states, and chase-vehicle occupants typically have neither
        # (mooks bootstrapped only through the chase pursuer roster). Seeding
        # them with names but no stat blocks would render "HP 0/0" in the
        # injection. Instead, the model bootstraps initiative on first combat
        # exchange using the chase narrative_summary (in conversation history)
        # plus the chase->combat context note we set here.
        # This MUST happen before apply_chase_writeback so the writeback has a
        # combat slot to write vehicle state into.
        if _exit_target == "combat":
            existing_combat = _ps_for_chase.get("combat")
            if not isinstance(existing_combat, dict):
                # Build a hint listing hostile occupants — surfaced in the
                # combat context so the model knows who to bootstrap. Includes
                # disabled (but not crashed) vehicles' occupants since "vehicle
                # disabled" doesn't mean "occupants dead" per RAW.
                _hostile_hints = []
                _ger = (_ps_for_chase.get("game_state") or {}).get("edgerunners") or {}
                _pc_names = set(_ger.keys())
                for vname, v in (_chase_state.get("vehicles") or {}).items():
                    if not isinstance(v, dict):
                        continue
                    if v.get("status") == "crashed":
                        continue  # crashed vehicles' occupants likely incapacitated
                    if not v.get("is_pursuer"):
                        continue
                    veh_label = "(disabled, on foot)" if v.get("status") == "disabled" else "(in vehicle)"
                    for occ in (v.get("occupants") or []):
                        if isinstance(occ, str) and occ and occ not in _pc_names:
                            _hostile_hints.append(f"{occ} {veh_label}")
                _hostiles_summary = (
                    "Likely hostiles: " + "; ".join(_hostile_hints) + "."
                    if _hostile_hints else
                    "Hostiles to be determined from chase narrative."
                )
                _ps_for_chase["combat"] = {
                    "round": 1,
                    "initiative_order": [],  # model bootstraps via set_combat_stats
                    "current_turn": "",
                    "vehicles": {},
                    "context": (
                        f"Chase ended ({_chase_state.get('end_reason') or 'unknown'}); "
                        f"hostile combatants are still alive and engaged. {_hostiles_summary} "
                        "Bootstrap initiative and stat-block any unstated NPCs on the first "
                        "exchange via set_combat_stats."
                    ),
                }
                logger.info(
                    f"Chase->combat handoff: seeded combat ({len(_hostile_hints)} hostile hints) "
                    f"for {username}"
                )

        if gs and gs.get("apply_chase_writeback"):
            gs["apply_chase_writeback"](_chase_state, _ps_for_chase)

        # Restore pre-chase HUD location (stamped on first chase seeding via
        # stamp_chase_hud_overlay or first apply_chase_state). After
        # init_chase_state, _pre_chase_location is None initially and gets
        # set to the captured HUD location on first stamp; only restore if
        # we have a captured value.
        _hud = _ps_for_chase.get("hud_state")
        _captured = _chase_state.get("_pre_chase_location")
        if isinstance(_hud, dict) and _captured is not None:
            _hud["location"] = _captured

        _ps_for_chase["chase"] = None
        if data.get("_auto_switched_from"):
            _restored = data.pop("_auto_switched_from")
            data["model"] = _restored
            model_id = _restored
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            logger.info(f"Chase ended: restored model to {_restored} for {username}")
        logger.info(
            f"Chase: cleared completed chase state for {username}, "
            f"exit_target={_exit_target}"
        )

    # Auto-switch to GPT-5.4 for chase mode (per spec; deterministic ruleset
    # benefits from GPT-5.4's fast-resolution profile)
    if use_chase_mode and not model_id.startswith("gpt"):
        _original_model = model_id
        model_id = "gpt-5.4"
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Chase-mode auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Chase-mode auto-switch: {_original_model} → {model_id} for {username}"
            )
            data["model"] = model_id
            data["_auto_switched_from"] = _original_model

    if use_chase_mode:
        user_msg_data["chase_mode"] = True

    # ── Sex mode detection ──
    use_sex_mode = False
    _sex_scene = data.get("pipeline_state", {}).get("sex_scene")
    if (not use_hack_mode) and (not use_combat_mode) and (not use_net_combat_mode) and (not use_ship_combat_mode) and (not use_chase_mode) and _sex_scene and _sex_scene.get("npcs"):
        use_sex_mode = True

    # Auto-switch to Opus for sex mode (regardless of current model)
    if use_sex_mode and model_id != "claude-3-opus":
        _original_model = model_id
        model_id = "claude-3-opus"
        provider = ProviderRegistry.get(model_id)
        api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
        if not api_key:
            logger.warning(
                f"Sex-mode auto-switch reverted for {username}: no "
                f"{ProviderRegistry.get_required_api_key(model_id)} key — "
                f"running on {_original_model} instead of {model_id}."
            )
            model_id = _original_model
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            _original_model = None
        else:
            logger.info(
                f"Sex-mode auto-switch: {_original_model} → {model_id} for {username}"
            )
            data["model"] = model_id

    if use_sex_mode:
        user_msg_data["sex_mode"] = True

    # ── /chase command detection — manual chase trigger from a Plot Encounter id ──
    # Format: "/chase <encounter_id_or_alias>" pulls a kind=vehicle entry from
    # Plot Encounters.yaml, materializes it, and seeds pipeline_state["chase"].
    # Takes effect this turn — chase mode dispatch fires on the SAME exchange.
    user_text_raw = request.message.strip()
    if (
        user_text_raw.lower().startswith("/chase ")
        and not use_chase_mode
        and request.project
        and gs
        and gs.get("chase_contract")
    ):
        _chase_cmd_arg = user_text_raw[7:].strip()
        if _chase_cmd_arg:
            try:
                from game_systems.cpred_plot_encounters import (
                    load_plot_encounters,
                    find_encounter_for,
                    materialize_vehicle_encounter,
                )
                _data_dir = os.environ.get("CHATGPT_DATA_DIR", "/home/chatgpt/data/users")
                _uploads_dir = os.path.join(
                    _data_dir, username, "projects", request.project, "uploads"
                )
                _all_encs = load_plot_encounters(_uploads_dir)
                _match = find_encounter_for(_chase_cmd_arg, _all_encs, kind="vehicle")
                if _match:
                    _materialized = materialize_vehicle_encounter(_match)
                    if _materialized and gs.get("init_chase_from_plot_encounter"):
                        # init_chase_from_plot_encounter synthesizes a 2-3 paragraph
                        # context from scene/route/legs/victory/failure when no
                        # context is supplied. Pass None to invoke that synthesis.
                        _ps_for_chase["chase"] = gs["init_chase_from_plot_encounter"](
                            _materialized,
                            context=None,
                        )
                        _chase_state = _ps_for_chase["chase"]
                        use_chase_mode = True
                        user_msg_data["chase_mode"] = True

                        # Stamp HUD overlay immediately (the per-turn dispatch
                        # builds chase_injection from chase_state right after
                        # this; HUD must already reflect the chase route).
                        from game_systems.cpred_chase import stamp_chase_hud_overlay
                        stamp_chase_hud_overlay(_ps_for_chase)

                        # Auto-switch to GPT-5.4 (parallel to the auto-detect path
                        # at line 4951 — /chase is a parallel activation route
                        # and must run the same setup).
                        if not model_id.startswith("gpt"):
                            _orig_chase_cmd_model = model_id
                            _gpt_provider = ProviderRegistry.get("gpt-5.4")
                            _gpt_api_key = get_api_key(
                                username,
                                ProviderRegistry.get_required_api_key("gpt-5.4"),
                            )
                            if _gpt_api_key:
                                model_id = "gpt-5.4"
                                provider = _gpt_provider
                                api_key = _gpt_api_key
                                data["model"] = model_id
                                data["_auto_switched_from"] = _orig_chase_cmd_model
                                logger.info(
                                    f"/chase auto-switch: {_orig_chase_cmd_model} → "
                                    f"{model_id} for {username}"
                                )
                            else:
                                logger.warning(
                                    f"/chase auto-switch reverted for {username}: no "
                                    f"OpenAI key — running on {_orig_chase_cmd_model}"
                                )

                        # Replace the literal "/chase encounter_id" command
                        # text with a story-shaped trigger directive so chase
                        # history doesn't carry a raw command line.
                        user_msg_data["content"] = (
                            f"[CHASE TRIGGER: {_match.get('id', _chase_cmd_arg)}]\n"
                            f"The scene transitions into a Hot Pursuit chase. "
                            f"Initialize the encounter using the chase state "
                            f"context and roll Initiative for each operator."
                        )

                        logger.info(
                            f"/chase: seeded chase from plot encounter {_match.get('id')!r} "
                            f"for {username}"
                        )
                    else:
                        logger.warning(
                            f"/chase: encounter {_match.get('id')!r} failed to materialize for {username}"
                        )
                else:
                    logger.warning(
                        f"/chase: no matching kind=vehicle encounter for {_chase_cmd_arg!r} "
                        f"in {username}/{request.project}"
                    )
            except Exception as _chase_cmd_err:
                logger.exception(f"/chase command failed for {username}: {_chase_cmd_err}")

    # ── /sex command detection for handoff turn ──
    # Detect /sex NPC_LIST prefix in user message to inject handoff directive
    _sex_handoff_npcs = None
    if user_text_raw.lower().startswith("/sex ") and not use_sex_mode:
        _sex_handoff_npcs = [n.strip() for n in user_text_raw[5:].split(",") if n.strip()]

    def _build_sex_handoff_directive(npc_list: str) -> str:
        return (
            f"\n\n[INTIMATE SCENE TRANSITION: {npc_list}]\n"
            "Write your final narrative beat leading into the intimate scene. "
            "At the end, generate a detailed handoff summary for the scene that follows. This summary is the ONLY context "
            "the next model will have about what just happened — it won't see any chat history before this point.\n"
            "[SCENE HANDOFF]\n"
            "Write 2-3 paragraphs in story-shaped prose covering, in order:\n"
            "(1) What's going on right now — the immediate situation as the intimate scene opens "
            "(situation, mood, tension; the last few exchanges that brought them here).\n"
            "(2) Why we're here — the emotional arc between the characters (how they got from where "
            "they were to this moment; what was said or unsaid; the thread of intimacy or vulnerability "
            "that's been building).\n"
            "(3) What the goal is — what the scene is reaching for, the unresolved emotional subtext, "
            "what success looks like and what failure looks like, plus physical/environmental "
            "details (where they are, what they're wearing or not, lighting, atmosphere) so the "
            "next model can pick up seamlessly.\n"
            "Reference any callbacks, prior beats, or tension being carried forward. Write so the "
            "next mode can continue the scene without losing context — the receiving model has "
            "nothing else.\n"
            "[/SCENE HANDOFF]"
        )

    # Refresh client if model was auto-switched
    if _original_model:
        client = provider.get_client(api_key)

    # Characters interview mode: force model to Opus 4.5 regardless of user's selection.
    # Consolidate uses the same model — force-swap on either path so the client is correct.
    # NOTE: INTERVIEW_MODEL is the Anthropic API name (dashed: "claude-opus-4-5"),
    # used directly in character_interview.py's API calls. ProviderRegistry uses the
    # Chorus-internal id (dotted: "claude-opus-4.5") — different convention. Look up
    # via the dotted form here.
    if use_characters_interview or characters_consolidate_pending:
        _interview_registry_id = "claude-opus-4.5"
        if model_id != _interview_registry_id:
            model_id = _interview_registry_id
            provider = ProviderRegistry.get(model_id)
            api_key = get_api_key(username, ProviderRegistry.get_required_api_key(model_id))
            if api_key:
                client = provider.get_client(api_key)
                logger.info(f"Characters: forced model to {model_id} for {username} (interview/consolidate)")
            else:
                logger.warning(f"Characters: no API key for {model_id}; falling back to {request.model or data.get('model')}")

    # Check if this is a stateful single-agent request (Claude + project chat, not pipeline)
    # Token-based trimming systems (e.g., "chats") fall through to the non-stateful else branch
    use_stateful = (not use_hack_mode) and (not use_combat_mode) and (not use_net_combat_mode) and (not use_ship_combat_mode) and (not use_chase_mode) and (not use_sex_mode) and (not _sex_handoff_npcs) and model_id.startswith("claude") and request.project and gs.get("trimming", "pair") == "pair"
    stateful_pipeline_state = None
    stateful_injected_snapshot = None
    _sex_first_exchange = False
    docs_refreshed = False
    _novels_context_error = None

    # GPT-5.2 hack request params (non-streaming JSON call, built separately)
    hack_gpt_request_params = None

    # GPT-5.2 combat request params (non-streaming JSON call, built separately)
    combat_gpt_request_params = None
    # GPT-5.2 net_combat request params (non-streaming JSON call, built separately)
    net_combat_gpt_request_params = None
    # GPT-5.2 ship combat request params (non-streaming JSON call, built separately)
    ship_combat_gpt_request_params = None
    ship_combat_init_hidden_message_prebuilt = None
    # GPT chase mode request params (non-streaming JSON call, built separately)
    chase_gpt_request_params = None
    chase_init_hidden_message_prebuilt = None

    if use_hack_mode:
        # ============================================================
        # Hack mode: stripped context with hack contract + hacker profile
        # ============================================================
        hack_ps = data.get("pipeline_state", {})

        # Load conversion doc for feature injection in hack mode
        hack_conversion_doc = None
        if request.project:
            conv_path = os.path.join(get_project_dir(username, request.project), "uploads", "Core Conversion.md")
            if os.path.exists(conv_path):
                with open(conv_path, 'r', encoding='utf-8') as f:
                    hack_conversion_doc = f.read()

        # Build system prompt: hack contract + hacker profile
        hack_contract = gs["hack_contract"]
        hacker_profile = gs["build_hacker_profile"](
            hack_ps.get("character_states", {}),
            conversion_doc=hack_conversion_doc,
            game_state=hack_ps.get("game_state"),
            hack_state=hack_state,
        )
        hack_injection = gs["build_hack_injection"](hack_state, pipeline_state=hack_ps)

        hack_system_content = hack_contract
        if hacker_profile:
            hack_system_content += "\n\n" + hacker_profile

        # Inject Hacking Rulebook if present in this project's uploads
        if request.project:
            rulebook_path = os.path.join(get_project_dir(username, request.project), "uploads", "Hacking Rulebook.md")
            if os.path.exists(rulebook_path):
                with open(rulebook_path, 'r', encoding='utf-8') as f:
                    hack_system_content += f"\n\n{'='*60}\nFILE: Hacking Rulebook.md\n{'='*60}\n\n" + f.read()

        system_msg = {"role": "system", "content": hack_system_content}

        # Only include hack-flagged messages (from start_message_id onward)
        hack_start_id = hack_state.get("start_message_id")
        hack_history = []
        found_start = not hack_start_id  # if no start_id, include all hack messages
        for msg in branch_path[1:-1]:
            if not found_start and msg.get("id") == hack_start_id:
                found_start = True
            if found_start and msg.get("hack_mode"):
                hack_history.append({"role": msg["role"], "content": msg["content"]})

        # User message with hack state injection + dice pool prepended
        hack_dice_pool = "" if (gs and gs.get("id") == "cpred") else (generate_dice_pool(gs["id"]) if gs else "")
        # /plot one-shot: strip prefix from saved message, prepend plot docs to user_content only
        _hack_plot_requested = _consume_plot_prefix(branch_path[-1])
        user_content = build_message_content(branch_path[-1])
        user_content = hack_injection + "\n\n" + (hack_dice_pool + "\n\n" if hack_dice_pool else "") + user_content
        if _hack_plot_requested:
            _hack_plot_blob = _build_plot_injection(username, request.project or "")
            if _hack_plot_blob:
                user_content = _hack_plot_blob + "\n\n" + user_content
                logger.info(f"Hack mode: /plot injection requested by {username}, prepended {len(_hack_plot_blob)} chars")
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + hack_history + [new_user_msg]

        # Context start index: only hack messages are in context
        context_start_index = max(1, len(branch_path) - len(hack_history) - 1)

        logger.info(f"Hack mode: {hack_state.get('tier')} for {username}, "
                     f"SR {hack_state.get('sr')}, {len(hack_history)} prior hack exchanges")

        if model_id.startswith("gpt"):
            # GPT: build non-streaming JSON request via pipeline request builder
            gpt_hack_messages = [
                {"role": "system", "content": hack_system_content
                 + "\n\nYou MUST output valid JSON matching the report_hack_state schema:\n"
                 + json.dumps(gs["hack_tool"]["input_schema"], indent=2)},
            ] + hack_history + [new_user_msg]
            hack_gpt_request_params = provider.build_pipeline_request(
                messages=gpt_hack_messages,
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="hack",
                reasoning_effort="medium",
                json_mode=True,
            )

    elif use_combat_mode:
        # ============================================================
        # Combat mode: stripped context with combat contract + roster
        # ============================================================
        combat_ps = data.get("pipeline_state", {})
        combat = combat_ps.get("combat", {})

        combat_contract = gs["combat_contract"]
        combat_profile = gs["build_combat_profile"](combat_ps.get("character_states", {}), combat, game_state=combat_ps.get("game_state", {}))
        combat_injection = gs["build_combat_injection"](combat, combat_ps)

        combat_system_content = combat_contract
        if combat_profile:
            combat_system_content += "\n\n" + combat_profile

        # Inject combat-specific project files if present
        if request.project:
            uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
            tokens_cache = load_file_tokens_cache(username, request.project)
            char_sheet_loaded = False
            for fname in _combat_file_list(gs):
                # Character Sheets: load first found (.md preferred over .yaml)
                is_char_sheet = fname.startswith("Character Sheets")
                if is_char_sheet and char_sheet_loaded:
                    continue
                if not tokens_cache.get(fname, {}).get("staged", True):
                    continue
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        combat_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                    if is_char_sheet:
                        char_sheet_loaded = True

        system_msg = {"role": "system", "content": combat_system_content}

        # Only include combat-flagged messages from combat start
        combat_start_id = combat.get("start_message_id")
        combat_history = []
        found_start = not combat_start_id
        for msg in branch_path[1:-1]:
            if not found_start and msg.get("id") == combat_start_id:
                found_start = True
            if found_start and msg.get("combat_mode"):
                combat_history.append({"role": msg["role"], "content": msg["content"]})

        # /plot one-shot: strip prefix from saved message, prepend plot docs to user_content only
        _combat_plot_requested = _consume_plot_prefix(branch_path[-1])
        user_content = build_message_content(branch_path[-1])
        combat_dice_pool = "" if (gs and gs.get("id") == "cpred") else (generate_dice_pool(gs["id"]) if gs else "")
        user_content = combat_injection + "\n\n" + (combat_dice_pool + "\n\n" if combat_dice_pool else "") + user_content
        if _combat_plot_requested:
            _combat_plot_blob = _build_plot_injection(username, request.project or "")
            if _combat_plot_blob:
                user_content = _combat_plot_blob + "\n\n" + user_content
                logger.info(f"Combat mode: /plot injection requested by {username}, prepended {len(_combat_plot_blob)} chars")
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + combat_history + [new_user_msg]
        context_start_index = max(1, len(branch_path) - len(combat_history) - 1)

        logger.info(f"Combat mode: round {combat.get('round', 1)} for {username}, "
                    f"{len(combat_history)} prior combat exchanges")

        if model_id.startswith("gpt"):
            # GPT: build non-streaming JSON request via pipeline request builder
            gpt_combat_messages = [
                {"role": "system", "content": combat_system_content
                 + "\n\nYou MUST output valid JSON matching the report_combat_state schema:\n"
                 + json.dumps(gs["combat_tool"]["input_schema"], indent=2)},
            ] + combat_history + [new_user_msg]
            combat_gpt_request_params = provider.build_pipeline_request(
                messages=gpt_combat_messages,
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="combat",
                reasoning_effort="medium",
                json_mode=True,
            )

    elif use_net_combat_mode:
        # ============================================================
        # NET-in-meatspace combined combat mode
        # ============================================================
        nc_ps = data.get("pipeline_state", {})
        nc_combat = nc_ps.get("combat", {})
        nc_state = nc_ps.get("net_combat", {})

        nc_contract = gs["net_combat_contract"]
        nc_profile = gs["build_net_combat_profile"](
            nc_ps.get("character_states", {}), nc_combat, nc_state,
            game_state=nc_ps.get("game_state", {}))
        nc_injection = gs["build_net_combat_injection"](nc_combat, nc_state, nc_ps)

        nc_system_content = nc_contract
        if nc_profile:
            nc_system_content += "\n\n" + nc_profile

        # Inject project files: Combat Ruleset, Hacking Rulebook, Character Sheets
        if request.project:
            uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
            tokens_cache = load_file_tokens_cache(username, request.project)
            char_sheet_loaded = False
            for fname in (gs.get("net_combat_files") or []):
                is_char_sheet = fname.startswith("Character Sheets")
                if is_char_sheet and char_sheet_loaded:
                    continue
                if not tokens_cache.get(fname, {}).get("staged", True):
                    continue
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        nc_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                    if is_char_sheet:
                        char_sheet_loaded = True

        system_msg = {"role": "system", "content": nc_system_content}

        # Include combat_mode, hack_mode, AND net_combat_mode messages from start
        # Preserve all prior context from whichever mode transitioned into net_combat
        _hack_st = data.get("hack_state") or {}
        _hack_start_fallback = nc_ps.get("_hack_start_message_id")
        nc_start_candidates = [
            nc_state.get("start_message_id"),
            nc_combat.get("start_message_id") if nc_combat else None,
            _hack_st.get("start_message_id"),
            _hack_start_fallback,
        ]
        # Use earliest available start_message_id — find the one that appears first in branch_path
        _msg_id_order = {msg.get("id"): idx for idx, msg in enumerate(branch_path) if msg.get("id")}
        nc_start_id = None
        _earliest_idx = len(branch_path)
        for cand in nc_start_candidates:
            if cand and cand in _msg_id_order and _msg_id_order[cand] < _earliest_idx:
                _earliest_idx = _msg_id_order[cand]
                nc_start_id = cand
        nc_history = []
        found_start = not nc_start_id
        for msg in branch_path[1:-1]:
            if not found_start and msg.get("id") == nc_start_id:
                found_start = True
            if found_start and (msg.get("combat_mode") or msg.get("net_combat_mode") or msg.get("hack_mode")):
                nc_history.append({"role": msg["role"], "content": msg["content"]})

        # /plot one-shot: strip prefix from saved message, prepend plot docs to user_content only
        _nc_plot_requested = _consume_plot_prefix(branch_path[-1])
        user_content = build_message_content(branch_path[-1])
        nc_dice_pool = "" if (gs and gs.get("id") == "cpred") else (generate_dice_pool(gs["id"]) if gs else "")
        user_content = nc_injection + "\n\n" + (nc_dice_pool + "\n\n" if nc_dice_pool else "") + user_content
        if _nc_plot_requested:
            _nc_plot_blob = _build_plot_injection(username, request.project or "")
            if _nc_plot_blob:
                user_content = _nc_plot_blob + "\n\n" + user_content
                logger.info(f"Net combat mode: /plot injection requested by {username}, prepended {len(_nc_plot_blob)} chars")
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + nc_history + [new_user_msg]
        context_start_index = max(1, len(branch_path) - len(nc_history) - 1)

        logger.info(f"Net combat mode: round {nc_combat.get('round', 1)} for {username}, "
                    f"netrunner={nc_state.get('netrunner', '?')}, {len(nc_history)} prior exchanges")

        if model_id.startswith("gpt"):
            gpt_nc_messages = [
                {"role": "system", "content": nc_system_content
                 + "\n\nYou MUST output valid JSON matching the report_net_combat_state schema:\n"
                 + json.dumps(gs["net_combat_tool"]["input_schema"], indent=2)},
            ] + nc_history + [new_user_msg]
            net_combat_gpt_request_params = provider.build_pipeline_request(
                messages=gpt_nc_messages,
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="net_combat",
                reasoning_effort="medium",
                json_mode=True,
            )

    elif use_ship_combat_mode:
        # ============================================================
        # Ship combat mode: stripped context with ship combat contract + roster
        # ============================================================
        ship_combat_ps = data.get("pipeline_state", {})
        ship_combat = ship_combat_ps.get("ship_combat", {})

        ship_combat_contract = gs["ship_combat_contract"]
        ship_profile = gs["build_ship_combat_profile"](ship_combat_ps.get("character_states", {}), ship_combat)
        ship_injection = gs["build_ship_combat_injection"](ship_combat, ship_combat_ps)

        ship_combat_system_content = ship_combat_contract
        if ship_profile:
            ship_combat_system_content += "\n\n" + ship_profile

        if request.project:
            uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
            tokens_cache = load_file_tokens_cache(username, request.project)
            for fname in ["Ship Systems.md", "Core Conversion.md"]:
                if not tokens_cache.get(fname, {}).get("staged", True):
                    continue
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        ship_combat_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
            for fname in ["Character Sheets.md", "Character Sheets.yaml"]:
                if not tokens_cache.get(fname, {}).get("staged", True):
                    continue
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        ship_combat_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                    break

        system_msg = {"role": "system", "content": ship_combat_system_content}

        ship_combat_start_id = ship_combat.get("start_message_id")
        ship_combat_history = []
        for _bm in (ship_combat.get("bootstrap_messages") or []):
            if isinstance(_bm, dict) and _bm.get("role") in ("user", "assistant") and isinstance(_bm.get("content"), str):
                ship_combat_history.append({"role": _bm["role"], "content": _bm["content"]})
        found_start = not ship_combat_start_id
        visible_ship_combat_history_count = 0
        for msg in branch_path[1:-1]:
            if not found_start and msg.get("id") == ship_combat_start_id:
                found_start = True
            if found_start and msg.get("ship_combat_mode"):
                ship_combat_history.append({"role": msg["role"], "content": msg["content"]})
                visible_ship_combat_history_count += 1

        user_content = build_message_content(branch_path[-1])
        sc_dice_pool = generate_dice_pool(gs["id"]) if gs else ""
        _ship_visible_user_content = ship_injection + "\n\n" + (sc_dice_pool + "\n\n" if sc_dice_pool else "") + user_content
        _is_first_ship_exchange_outer = not any(m.get("ship_combat_mode") for m in branch_path[1:-1])
        if _is_first_ship_exchange_outer:
            ship_combat_init_hidden_message_prebuilt = build_ship_combat_hidden_init_message(user_msg_id)
            user_content = ship_combat_init_hidden_message_prebuilt["content"]
        else:
            user_content = _ship_visible_user_content
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + ship_combat_history + [new_user_msg]
        # Context index is for visible chat history only (branch_path); hidden bootstrap
        # messages are injected into API history but do not exist in branch_path.
        context_start_index = max(1, len(branch_path) - visible_ship_combat_history_count - 1)

        logger.info(f"Ship combat mode: round {ship_combat.get('round', 1)} for {username}, "
                    f"{len(ship_combat_history)} prior ship combat exchanges")

        if model_id.startswith("gpt"):
            gpt_ship_combat_messages = [
                {"role": "system", "content": ship_combat_system_content
                 + "\n\nYou MUST output valid JSON matching the report_ship_combat_state schema:\n"
                 + json.dumps(gs["ship_combat_tool"]["input_schema"], indent=2)},
            ] + ship_combat_history + [new_user_msg]
            ship_combat_gpt_request_params = provider.build_pipeline_request(
                messages=gpt_ship_combat_messages,
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="ship_combat",
                reasoning_effort="medium",
                json_mode=True,
            )

    elif use_chase_mode:
        # ============================================================
        # Hot Pursuit chase mode: stripped context with chase contract
        # + chase roster + scene files. Mirrors ship_combat dispatch.
        # ============================================================
        chase_ps = data.get("pipeline_state", {})
        chase_state = chase_ps.get("chase", {})

        chase_contract = gs["chase_contract"]
        chase_profile = gs["build_chase_profile"](
            chase_ps.get("character_states", {}),
            chase_state,
            game_state=chase_ps.get("game_state"),
        )
        chase_injection = gs["build_chase_injection"](chase_state, chase_ps)

        chase_system_content = chase_contract
        if chase_profile:
            chase_system_content += "\n\n" + chase_profile

        if request.project:
            uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
            tokens_cache = load_file_tokens_cache(username, request.project)
            for fname in (gs.get("chase_files") or []):
                if not tokens_cache.get(fname, {}).get("staged", True):
                    continue
                fpath = os.path.join(uploads_dir, fname)
                if os.path.exists(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        chase_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                    if fname in ("Character Sheets.md", "Character Sheets.yaml"):
                        # Only include the first matching character-sheet variant.
                        break

        system_msg = {"role": "system", "content": chase_system_content}

        chase_start_id = chase_state.get("start_message_id")
        chase_history = []
        for _bm in (chase_state.get("bootstrap_messages") or []):
            if isinstance(_bm, dict) and _bm.get("role") in ("user", "assistant") and isinstance(_bm.get("content"), str):
                chase_history.append({"role": _bm["role"], "content": _bm["content"]})
        found_start = not chase_start_id
        visible_chase_history_count = 0
        for msg in branch_path[1:-1]:
            if not found_start and msg.get("id") == chase_start_id:
                found_start = True
            if found_start and msg.get("chase_mode"):
                chase_history.append({"role": msg["role"], "content": msg["content"]})
                visible_chase_history_count += 1

        user_content = build_message_content(branch_path[-1])
        chase_dice_pool = generate_dice_pool(gs["id"]) if gs else ""
        _chase_visible_user_content = (
            chase_injection + "\n\n"
            + (chase_dice_pool + "\n\n" if chase_dice_pool else "")
            + user_content
        )

        # First-exchange detection: are there any prior chase_mode-tagged
        # messages in branch_path? If not, this is the chase opener — use the
        # hidden init message to surface handoff_summary + seeded vehicles.
        _is_first_chase_exchange = not any(
            m.get("chase_mode") for m in branch_path[1:-1]
        )
        if _is_first_chase_exchange:
            chase_init_hidden_message_prebuilt = build_chase_hidden_init_message(user_msg_id)
            user_content = chase_init_hidden_message_prebuilt["content"]
        else:
            user_content = _chase_visible_user_content
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + chase_history + [new_user_msg]
        context_start_index = max(1, len(branch_path) - visible_chase_history_count - 1)

        logger.info(
            f"Chase mode: round {chase_state.get('round', 1)} for {username}, "
            f"{len(chase_history)} prior chase exchanges, "
            f"first_exchange={_is_first_chase_exchange}"
        )

        if model_id.startswith("gpt"):
            gpt_chase_messages = [
                {"role": "system", "content": chase_system_content
                 + "\n\nYou MUST output valid JSON matching the report_chase_state schema:\n"
                 + json.dumps(gs["chase_tool"]["input_schema"], indent=2)},
            ] + chase_history + [new_user_msg]
            chase_gpt_request_params = provider.build_pipeline_request(
                messages=gpt_chase_messages,
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="chase",
                reasoning_effort="medium",
                json_mode=True,
            )

    elif use_sex_mode:
        # ============================================================
        # Sex mode: isolated intimate scene context with Opus
        # ============================================================
        sex_ps = data.get("pipeline_state", {})
        sex_scene = sex_ps.get("sex_scene", {})
        # Ensure we can restore model when scene ends (report_state-started scenes may omit this)
        if isinstance(sex_scene, dict) and not sex_scene.get("original_model"):
            inferred_original_model = _original_model
            if not inferred_original_model:
                current_chat_model = data.get("model")
                if current_chat_model and current_chat_model != "claude-3-opus":
                    inferred_original_model = current_chat_model
            if inferred_original_model:
                sex_scene["original_model"] = inferred_original_model

        # Build system prompt: sex contract + selected project files only
        sex_system_content = SEX_MODE_CONTRACT

        if request.project:
            uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
            # Resolve short NPC names (e.g. "Lydia") to full canonical names
            # from character_states (e.g. "Commander Lydia Cross") for profile matching
            raw_npcs = list(sex_scene.get("npcs", []))
            all_cs_names = list(sex_ps.get("character_states", {}).keys())
            participants = []
            for short_name in raw_npcs:
                matched = False
                for full_name in all_cs_names:
                    if short_name.lower() in full_name.lower():
                        if full_name not in participants:
                            participants.append(full_name)
                        matched = True
                        break
                if not matched and short_name not in participants:
                    participants.append(short_name)
            # Include PCs — check for type=="pc" or infer from character sheet data
            for name, cs in sex_ps.get("character_states", {}).items():
                if isinstance(cs, dict) and name not in participants:
                    cs_data = cs.get("data", cs)
                    if cs_data.get("type") == "pc" or cs_data.get("class") or cs_data.get("level"):
                        participants.append(name)
            profiles = _extract_character_profiles(uploads_dir, participants)
            if profiles:
                sex_system_content += "\n\n" + profiles

        # Append scene context to system prompt (cacheable — stable across the scene)
        sex_injection = _build_sex_injection(sex_ps, sex_scene)
        if sex_injection:
            sex_system_content += "\n\n" + sex_injection

        system_msg = {"role": "system", "content": sex_system_content}

        # Context isolation: only sex_mode messages from start_message_id,
        # plus the immediately-preceding user/assistant pair as a seed so the
        # model can see what the first sex-mode turn is replying to.
        sex_start_id = sex_scene.get("start_message_id")
        sex_history = []
        seed_pair = []
        # Locate the boundary index in branch_path[1:-1] where sex mode begins.
        # On the first exchange, start_message_id isn't set yet, so the boundary
        # is the new user message (branch_path[-1]).
        inner = branch_path[1:-1]
        if sex_start_id:
            boundary_idx = next((i for i, m in enumerate(inner) if m.get("id") == sex_start_id), len(inner))
        else:
            boundary_idx = len(inner)
        # Seed pair: last user/assistant pair strictly before the boundary.
        if boundary_idx >= 2:
            prev_a = inner[boundary_idx - 1]
            prev_u = inner[boundary_idx - 2]
            if prev_u.get("role") == "user" and prev_a.get("role") == "assistant":
                seed_pair = [
                    {"role": "user", "content": prev_u["content"]},
                    {"role": "assistant", "content": prev_a["content"]},
                ]
        # Sex-mode history from start_message_id onward.
        found_start = not sex_start_id
        for msg in inner:
            if not found_start and msg.get("id") == sex_start_id:
                found_start = True
            if found_start and msg.get("sex_mode"):
                sex_history.append({"role": msg["role"], "content": msg["content"]})

        # /plot one-shot: strip prefix from saved message, prepend plot docs to user_content only
        _sex_plot_requested = _consume_plot_prefix(branch_path[-1])
        user_content = build_message_content(branch_path[-1])
        if _sex_plot_requested:
            _sex_plot_blob = _build_plot_injection(username, request.project or "")
            if _sex_plot_blob:
                user_content = _sex_plot_blob + "\n\n" + user_content
                logger.info(f"Sex mode: /plot injection requested by {username}, prepended {len(_sex_plot_blob)} chars")
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + seed_pair + sex_history + [new_user_msg]
        context_start_index = max(1, len(branch_path) - len(sex_history) - len(seed_pair) - 1)

        # Set start_message_id on first sex mode exchange
        _sex_first_exchange = not sex_start_id
        if _sex_first_exchange:
            sex_scene["start_message_id"] = user_msg_id

        logger.info(f"Sex mode: {len(sex_scene.get('npcs', []))} NPCs for {username}, "
                    f"{len(sex_history)} prior exchanges")

    elif use_stateful:
        # Pair-based context trimming (sawtooth pattern for cache efficiency)
        _has_game_state = gs.get("use_game_state", True)

        if _has_game_state:
            stateful_pipeline_state = migrate_pipeline_state(copy.deepcopy(data.get("pipeline_state")))
            stateful_injected_snapshot = json.dumps(stateful_pipeline_state, indent=2)

            # Load conversion doc for feature injection (transient — after snapshot, stripped after injection)
            if gs and request.project:
                conv_path = os.path.join(get_project_dir(username, request.project), "uploads", "Core Conversion.md")
                if os.path.exists(conv_path):
                    with open(conv_path, 'r', encoding='utf-8') as f:
                        stateful_pipeline_state.setdefault("game_state", {})["_conversion_doc"] = f.read()

        if gs.get("manual_staging"):
            # Novels: staging-driven context with hard token ceiling (no sawtooth)
            history = branch_path[1:-1]
            staged_history = _filter_unstaged_pairs(history)
            context_pairs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in staged_history]
            context_start_index = 1  # Dimming handled by staged field, not index
        else:
            # TTRPG systems: pair-based sawtooth context trimming
            trim_anchor_id = data.get("_trim_anchor_id")
            # Collapse hack and combat messages into summary pairs before context trimming
            branch_path_for_context = collapse_sex_messages(collapse_chase_messages(collapse_net_combat_messages(collapse_ship_combat_messages(collapse_combat_messages(collapse_hack_messages(branch_path))))))
            # Characters: each mode gets its own fresh context view of the chat —
            # correspondence sees only correspondence turns; the interviewer sees only
            # the current interview session (since the most recent /reinterview, or
            # since chat start if never re-interviewed). System message and the current
            # user message are always preserved.
            if use_characters and not use_characters_interview:
                branch_path_for_context = (
                    [branch_path_for_context[0]]
                    + [
                        m for m in branch_path_for_context[1:-1]
                        if not m.get("_characters_interview_mode") and not m.get("characters_finalize")
                    ]
                    + [branch_path_for_context[-1]]
                )
            elif use_characters_interview:
                # Scope interview context to the latest /reinterview boundary onward —
                # so a re-interview starts fresh rather than seeing the prior interview
                # session or any correspondence that happened in between.
                boundary_idx = 1  # default: start of history
                for _i, _m in enumerate(branch_path_for_context):
                    if _m.get("_characters_reinterview_boundary"):
                        boundary_idx = _i
                _history_slice = (
                    branch_path_for_context[boundary_idx:-1]
                    if boundary_idx < len(branch_path_for_context) - 1
                    else []
                )
                branch_path_for_context = (
                    [branch_path_for_context[0]]
                    + [
                        m for m in _history_slice
                        if m.get("_characters_interview_mode") and not m.get("characters_finalize")
                    ]
                    + [branch_path_for_context[-1]]
                )
            if use_characters_interview:
                # Interview mode does NOT sawtooth. The interviewer has to
                # remember the entire session — which sections have been
                # asked, what was answered, where we are in the flow. Total
                # interview volume tops out at maybe 30K tokens (a rich-tier
                # 90-min interview), well inside Opus 4.5's window. Trimming
                # mid-interview drops earlier sections and the agent loses
                # the thread. Send the entire filtered history every turn.
                context_pairs = [
                    {"role": msg["role"], "content": build_message_content(msg)}
                    for msg in branch_path_for_context[1:-1]
                ]
                new_anchor_id = None
                did_trim = False
                # Clear any stale anchor from a previously trimmed interview
                # turn so subsequent turns also start fresh.
                data.pop("_trim_anchor_id", None)
            else:
                # Correspondence mode: pair-based sawtooth (gamesystems can override).
                _threshold_pairs = gs.get("characters_threshold_pairs", SINGLE_AGENT_THRESHOLD_PAIRS) if use_characters else SINGLE_AGENT_THRESHOLD_PAIRS
                _target_pairs = gs.get("characters_target_pairs", SINGLE_AGENT_TARGET_PAIRS) if use_characters else SINGLE_AGENT_TARGET_PAIRS
                context_pairs, new_anchor_id, did_trim = get_context_pairs(
                    branch_path_for_context, _threshold_pairs, _target_pairs, trim_anchor_id
                )
                data["_trim_anchor_id"] = new_anchor_id
            data.pop("_stateful_trimming", None)  # clean up legacy flag

            if did_trim:
                docs_refreshed = True
                fresh_system = build_system_content(username, request.project, **_system_content_kwargs(gs))
                branch_path[0]["content"] = fresh_system
                branch_path[0].pop("total_tokens", None)
                branch_path[0].pop("total_gpt_tokens", None)
                branch_path[0].pop("total_claude_tokens", None)
                logger.info(f"Stateful: refreshed system prompt on context trim for {username}/{request.project}/{request.chat_name}")

            # Override token-based context_start_index with pair-based value
            # so the frontend greys out messages matching what the API actually sees
            if new_anchor_id:
                context_start_index = 1
                for idx, msg in enumerate(branch_path):
                    if msg.get("id") == new_anchor_id:
                        context_start_index = idx
                        break
            else:
                context_start_index = 1

        if _has_game_state and (use_characters or use_characters_interview):
            # ── Characters gamesystem path ──────────────────────────────
            from characters_runtime import (
                get_characters_state,
                prepare_state_for_turn,
                maybe_generate_off_screen,
                maybe_migrate_storage,
                populate_render_payload,
                clear_render_payload,
                branch_msg_ids_from_branch_path,
                build_system_content as _characters_build_system,
                build_user_message_content as _characters_build_user,
            )
            _project_dir = get_project_dir(username, request.project)

            # Phase 3 migration: if this chat has legacy in-state memory/profile/growth
            # entries, copy them to jsonl files. Idempotent — runs once per chat.
            try:
                _migration_report = maybe_migrate_storage(data, _project_dir)
                if any((_migration_report or {}).values()):
                    logger.info(f"Characters: migrated state→files for {username}/{request.project}: {_migration_report}")
            except Exception as _mig_err:
                logger.warning(f"Characters storage migration failed: {_mig_err}")

            characters_state = get_characters_state(data, project_dir=_project_dir)
            stateful_pipeline_state.setdefault("characters_state", characters_state)
            _branch_msg_ids = branch_msg_ids_from_branch_path(branch_path)

            if use_characters_interview:
                # Interview mode: no injections, no off-screen, no daily rolls, no recall.
                injections_str = ""
                _interview_system = _characters_build_system(gs, _project_dir, branch_path[0]["content"], interview_mode=True)
                system_msg = {"role": branch_path[0]["role"], "content": _interview_system}
                user_content = build_message_content(branch_path[-1])
                new_user_msg = {"role": "user", "content": user_content}
            else:
                # Concurrent pre-pass: off-screen generation + recall agent. Both
                # use the *pre-turn* state, so we can run them in parallel.
                from character_recall import run_recall, compute_recall_cost
                from character_storage import CharacterStore, KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH
                _store = CharacterStore(_project_dir)
                _core_memory_ids = {m.get("id") for m in _store.core(KIND_MEMORIES, branch_msg_ids=_branch_msg_ids, n=8) if isinstance(m.get("id"), int)}
                _core_profile_ids = {m.get("id") for m in _store.core(KIND_USER_PROFILE, branch_msg_ids=_branch_msg_ids, n=12) if isinstance(m.get("id"), int)}
                _core_growth_ids = {m.get("id") for m in _store.core(KIND_GROWTH, branch_msg_ids=_branch_msg_ids) if isinstance(m.get("id"), int)}

                _user_input_text = build_message_content(branch_path[-1])
                # Recall sees a SMALL dialogue window (last few exchanges) — just
                # enough to understand the immediate topic. Cross-turn references
                # ("did you finish that book you mentioned?") are handled by the
                # memory index, not by reconstructing 30 turns of conversation.
                # The whole point of a memory is "this is the distilled summary
                # of what's worth retrieving"; recall picks from that, with the
                # current dialogue chunk just for topic continuity.
                _recent_dialogue = [
                    {"role": m.get("role"), "content": m.get("content", "")}
                    for m in branch_path[-7:-1] if isinstance(m, dict)
                ]

                async def _run_off_screen():
                    try:
                        result = await asyncio.to_thread(
                            maybe_generate_off_screen, client, characters_state, _project_dir
                        )
                        # off-screen consumes the life-event seed in characters_state.life_events;
                        # since life_events is now project-level, persist after the consumption.
                        # NB: persist runs to_thread because save_project_state does blocking
                        # file IO + fcntl.flock acquire — running it on the asyncio main thread
                        # would stall the event loop under lock contention from another chat.
                        if result is not None:
                            from character_project_state import persist_project_state_from
                            await asyncio.to_thread(
                                persist_project_state_from, characters_state, _project_dir
                            )
                        return result
                    except Exception as e:
                        logger.warning(f"Characters off-screen failed: {e}")
                        return None

                async def _run_recall():
                    try:
                        return await asyncio.to_thread(
                            run_recall,
                            client, _project_dir, _user_input_text, _branch_msg_ids,
                            core_memory_ids=_core_memory_ids,
                            core_profile_ids=_core_profile_ids,
                            core_growth_ids=_core_growth_ids,
                            recent_dialogue=_recent_dialogue,
                        )
                    except Exception as e:
                        logger.warning(f"Characters recall failed: {e}")
                        return ({}, {})

                _osc_meta, _recall_result = await asyncio.gather(_run_off_screen(), _run_recall())

                # Off-screen telemetry
                if isinstance(_osc_meta, dict) and _osc_meta.get("usage"):
                    from character_off_screen import compute_off_screen_cost
                    data["off_screen_usage"] = _osc_meta["usage"]
                    data["off_screen_cost"] = compute_off_screen_cost(_osc_meta["usage"])
                    data["off_screen_model"] = "claude-opus-4.5"

                # Recall telemetry
                _recall_payload, _recall_usage = _recall_result if isinstance(_recall_result, tuple) else ({}, {})
                if _recall_usage:
                    data["recall_usage"] = _recall_usage
                    data["recall_cost"] = compute_recall_cost(_recall_usage)
                    data["recall_model"] = "claude-haiku-4-5"
                    if isinstance(_recall_payload, dict) and _recall_payload.get("ids_recalled"):
                        data["recall_ids"] = _recall_payload["ids_recalled"]

                # Daily rolls (does NOT touch wall_clock — stamp_user_turn after injection build)
                prepare_state_for_turn(data, _project_dir)
                characters_state = get_characters_state(data, project_dir=_project_dir)
                stateful_pipeline_state["characters_state"] = characters_state

                # Populate the render payload from file storage + recall results.
                # build_*_injection functions read from characters_state["_render_payload"].
                populate_render_payload(
                    characters_state, _project_dir,
                    branch_msg_ids=_branch_msg_ids,
                    recall=_recall_payload if isinstance(_recall_payload, dict) else None,
                )

                # Inner-state pre-pass — Sonnet 4.6 produces hidden emotional ground
                # truth for Opus to voice from. SEQUENTIAL after recall (not parallel)
                # because it depends on recall's filtered memory set as part of its
                # context: a character's feelings about THIS moment are colored by
                # which past memories are currently active, not by raw mood.
                # +1-2s real latency on top of current pipeline; cost ~$0.005/turn.
                from character_inner_state import run_inner_state, compute_inner_state_cost
                try:
                    _inner_state_payload, _inner_state_usage = await asyncio.to_thread(
                        run_inner_state,
                        client, _project_dir, characters_state, _user_input_text,
                        recent_dialogue=_recent_dialogue,
                        branch_msg_ids=_branch_msg_ids,
                    )
                except Exception as e:
                    logger.warning(f"Characters inner_state failed: {e}")
                    _inner_state_payload, _inner_state_usage = {}, {}

                # Stash inner-state result on the render payload so build_inner_state_injection
                # finds it. populate_render_payload already initialized the slot to {}.
                if isinstance(characters_state.get("_render_payload"), dict):
                    characters_state["_render_payload"]["inner_state"] = _inner_state_payload or {}

                # Inner-state telemetry — mirrors recall block above.
                if _inner_state_usage:
                    data["inner_state_usage"] = _inner_state_usage
                    data["inner_state_cost"] = compute_inner_state_cost(_inner_state_usage)
                    data["inner_state_model"] = "claude-sonnet-4-6"

                _system_full = _characters_build_system(gs, _project_dir, branch_path[0]["content"], interview_mode=False)
                system_msg = {"role": branch_path[0]["role"], "content": _system_full}

                user_content = build_message_content(branch_path[-1])
                user_content = _characters_build_user(characters_state, user_content)
                new_user_msg = {"role": "user", "content": user_content}

                # Stamp wall_clock and clear the render payload (transient, don't persist)
                from characters_runtime import stamp_user_turn as _characters_stamp
                _characters_stamp(characters_state)
                clear_render_payload(characters_state)
                injections_str = ""

            # messages_for_api is built unconditionally below; we just populated system_msg + new_user_msg.
        elif _has_game_state:
            # Build injections for user message (game systems only)
            if gs and gs.get("id") == "cpred":
                sa_dice_pool = ""  # No pool needed — resolve_mechanics tool uses direct RNG
            else:
                sa_dice_pool = generate_dice_pool(gs["id"]) if gs else ""
            sa_doc_stems = get_staged_project_filenames(username, request.project)
            sa_name_dice = generate_name_dice(os.path.join(get_project_dir(username, request.project), "uploads"))
            injections_str = build_single_agent_injections(stateful_pipeline_state, game_system=gs, dice_pool=sa_dice_pool, doc_file_stems=sa_doc_stems, name_dice=sa_name_dice)

            # [CURRENT BEAT] injection — appended after the standard
            # injection bundle so the pacing rule sits closest to the
            # user message.  Sex / hack / combat / net_combat modes use
            # different code paths and don't include this — beats only
            # advance in normal narrative mode.
            try:
                from game_systems.plot_beats import build_current_beat_injection
                _beat_block = build_current_beat_injection(
                    stateful_pipeline_state.get("beat_state"),
                    os.path.join(get_project_dir(username, request.project), "uploads"),
                )
                if _beat_block:
                    injections_str = (injections_str + "\n\n" + _beat_block) if injections_str else _beat_block
            except Exception as _e:
                logger.warning(f"Beat injection failed: {_e}")

            # Strip transient _conversion_doc before it can be persisted
            _gs_state = stateful_pipeline_state.get("game_state")
            if _gs_state:
                _gs_state.pop("_conversion_doc", None)

            # System prompt: contract + original
            system_content = gs["single_agent_contract"] + "\n\n" + branch_path[0]["content"]
            system_msg = {"role": branch_path[0]["role"], "content": system_content}

            # User message with injections prepended + player agency reminder for multi-PC
            user_content = build_message_content(branch_path[-1])

            # /sex command: inject handoff directive into user message
            if _sex_handoff_npcs:
                user_content += _build_sex_handoff_directive(", ".join(_sex_handoff_npcs))

            agency_reminder = build_player_agency_reminder(
                user_content, stateful_pipeline_state.get("character_states", {}))
            parts = []
            if injections_str:
                parts.append(injections_str)
            if agency_reminder:
                parts.append(agency_reminder)
            parts.append(user_content)
            user_content = "\n\n".join(parts)
            # Image attachments: send as actual image content blocks alongside
            # the text. Only the latest user message gets full image blocks;
            # historical messages keep the [image: filename] placeholder so we
            # don't pay vision-token costs every turn.
            _image_blocks = build_image_content_blocks(branch_path[-1])
            # Image URLs in the user's text: server-side fetch + base64.
            # Cap total images at _MAX_IMAGES_PER_MESSAGE (attachments + URLs).
            _slots_left = _MAX_IMAGES_PER_MESSAGE - len(_image_blocks)
            if _slots_left > 0:
                _user_text_for_urls = (branch_path[-1].get("content") or "")
                _found_urls = extract_image_urls_from_text(_user_text_for_urls)
                for _u in _found_urls[:_slots_left]:
                    _b = fetch_image_url_as_block(_u)
                    if _b:
                        _image_blocks.append(_b)
            if _image_blocks:
                new_user_msg = {
                    "role": "user",
                    "content": _image_blocks + [{"type": "text", "text": user_content}],
                }
            else:
                new_user_msg = {"role": "user", "content": user_content}
        else:
            # No game state (e.g., Novels) — contract + pinned artifacts in system, no game injections
            base_content = branch_path[0]["content"]
            contract = gs.get("single_agent_contract", "")
            if contract:
                base_content = contract + "\n\n" + base_content
            # Inject pinned artifact content into system prompt (cached across turns)
            if gs.get("doc_tools") and model_id.startswith("claude"):
                pinned = _build_pinned_artifacts(data.get("artifacts", {}))
                if pinned:
                    base_content = base_content + "\n\n" + pinned
            system_msg = {"role": branch_path[0]["role"], "content": base_content}

            user_content = build_message_content(branch_path[-1])
            # Inject artifact summary so Claude knows what docs exist (Claude only — GPT doesn't get doc tools)
            if gs.get("doc_tools") and model_id.startswith("claude"):
                doc_summary = _build_artifact_summary(data.get("artifacts", {}))
                if doc_summary:
                    user_content = doc_summary + "\n\n" + user_content
            new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + context_pairs + [new_user_msg]

        # Novels: hard token ceiling check (no auto-trimming — user controls context via staging)
        # Sum from source messages (cached tokens) + estimate fresh system/user messages
        if gs.get("manual_staging") and model_id.startswith("claude"):
            _history_tokens = sum(
                m.get("total_claude_tokens") or m.get("total_tokens") or count_tokens(m.get("content", ""))
                for m in staged_history
            )
            _system_tokens = count_tokens(system_msg.get("content", ""))
            _user_tokens = count_tokens(new_user_msg.get("content", ""))
            _total_tokens = _system_tokens + _history_tokens + _user_tokens
            _ceiling = provider.context_limits.threshold
            if _total_tokens > _ceiling:
                _novels_context_error = f"Context is ~{_total_tokens:,} tokens, exceeding the {_ceiling:,} token limit. Unstage some messages to continue."

    else:
        system_msg = {"role": branch_path[0]["role"], "content": branch_path[0]["content"]}
        # Collapse hack and combat messages in non-stateful path too
        bp_filtered = collapse_sex_messages(collapse_chase_messages(collapse_net_combat_messages(collapse_ship_combat_messages(collapse_combat_messages(collapse_hack_messages(branch_path))))))
        history_msgs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in bp_filtered[context_start_index:-1]]
        user_content = build_message_content(branch_path[-1])

        # /sex command: inject handoff directive into user message (non-stateful path)
        if _sex_handoff_npcs:
            user_content += _build_sex_handoff_directive(", ".join(_sex_handoff_npcs))

        new_user_msg = {"role": branch_path[-1]["role"], "content": user_content}

        messages_for_api = [system_msg] + history_msgs + [new_user_msg]

    is_free_chat = not request.project
    use_cache = data.get("anthropic_sync", True)
    if use_hack_mode and model_id.startswith("claude"):
        # Claude hack mode: use standard build_request with hack tool
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
        hack_tools = [gs["hack_tool"]]
        if gs.get("id") == "cpred":
            from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
            hack_tools.insert(0, RESOLVE_MECHANICS_TOOL)
        request_params["tools"] = hack_tools
        request_params["tool_choice"] = {"type": "auto"}
    elif use_hack_mode:
        # GPT-5.2 hack mode: request_params not used (hack_gpt_request_params used instead)
        request_params = {}
    elif use_net_combat_mode and model_id.startswith("claude"):
        # Claude net_combat mode: use standard build_request with net_combat tool
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
        net_combat_tools = [gs["net_combat_tool"]]
        if gs.get("id") == "cpred":
            from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
            net_combat_tools.insert(0, RESOLVE_MECHANICS_TOOL)
        request_params["tools"] = net_combat_tools
        request_params["tool_choice"] = {"type": "auto"}
    elif use_net_combat_mode:
        # GPT-5.2 net_combat mode: request_params not used
        request_params = {}
    elif use_combat_mode and model_id.startswith("claude"):
        # Claude combat mode: use standard build_request with combat tool
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
        combat_tools = [gs["combat_tool"]]
        if gs.get("id") == "cpred":
            from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
            combat_tools.insert(0, RESOLVE_MECHANICS_TOOL)
        request_params["tools"] = combat_tools
        request_params["tool_choice"] = {"type": "auto"}
    elif use_combat_mode:
        # GPT-5.2 combat mode: request_params not used (combat_gpt_request_params used instead)
        request_params = {}
    elif use_ship_combat_mode and model_id.startswith("claude"):
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
        request_params["tools"] = [gs["ship_combat_tool"]]
        request_params["tool_choice"] = {"type": "auto"}
    elif use_ship_combat_mode:
        request_params = {}
    elif use_chase_mode and model_id.startswith("claude"):
        # Claude chase mode: standard build_request with chase tool
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
        request_params["tools"] = [gs["chase_tool"]]
        request_params["tool_choice"] = {"type": "auto"}
    elif use_chase_mode:
        # GPT chase mode: request_params not used (chase_gpt_request_params used instead)
        request_params = {}
    elif use_sex_mode:
        # Sex mode: pure Opus streaming, no tools, cache enabled
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=False,
            use_cache=True
        )
    else:
        # /sex handoff: cache is wasted — this one-off context won't be reused
        # (next turn switches to the sex mode contract + isolated history)
        _else_use_cache = False if _sex_handoff_npcs else use_cache
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=is_free_chat,
            use_cache=_else_use_cache
        )
        if use_stateful and gs.get("use_game_state", True) and gs.get("state_report_tool"):
            # Build a per-project variant of the state_report tool with plot_ops.key
            # constrained to the project's canonical flag enum (decision_flags.md).
            # This stops the model from inventing flag names mid-session.
            from pipeline import parse_canonical_flags, build_state_report_tool_with_flag_enum
            _project_uploads = (
                os.path.join(get_project_dir(username, request.project), "uploads")
                if request.project else ""
            )
            _canonical_flags = parse_canonical_flags(_project_uploads)
            state_report_tool = build_state_report_tool_with_flag_enum(
                gs["state_report_tool"], _canonical_flags
            )
            tools = [state_report_tool]
            if gs.get("id") == "cpred":
                from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
                tools.insert(0, RESOLVE_MECHANICS_TOOL)
            request_params["tools"] = tools
            # Disable extended thinking on stateful path. Modes (combat, hack, chase, sex,
            # ship combat) handle crunch elsewhere; here Claude's job is narration + state.
            # Reasoning audit showed ~half the thinking was shadow-doing what report_state
            # and build_current_beat_injection already do deterministically, plus occasional
            # model-rolled dice (anti-pattern).
            request_params.pop("thinking", None)
            request_params.pop("output_config", None)
            # Force the model to use SOME tool every turn (any), letting it pick between
            # resolve_mechanics (when crunch is needed) and report_state (the wrap-up).
            # Forcing report_state specifically would suppress resolve_mechanics calls and
            # break the deterministic dice loop. Under `any`, the resolve_mechanics loop
            # iterates as before; the model naturally moves to report_state once mechanics
            # are resolved (only tool left + contract demands it as the turn's wrap-up).
            # Narration is now a required field on report_state, so emitting it is
            # schema-enforced rather than contract-trusted.
            request_params["tool_choice"] = {"type": "any"}
        elif gs.get("doc_tools") and model_id.startswith("claude"):
            request_params["tools"] = gs["doc_tools"]
            request_params["tool_choice"] = {"type": "auto"}
        elif use_characters and not use_characters_interview and gs.get("search_tool_enabled"):
            # Characters correspondence: expose Sonar search + fetch_url. Auto choice —
            # the model decides per turn. Interview mode does not get the tools (interview
            # is about defining the character, not pulling external info).
            from character_search import SONAR_SEARCH_TOOL
            from character_fetch_url import FETCH_URL_TOOL
            request_params["tools"] = [SONAR_SEARCH_TOOL, FETCH_URL_TOOL]
            request_params["tool_choice"] = {"type": "auto"}
            # Opus 3 doesn't support extended thinking; explicit pop is harmless on other models.
            request_params.pop("thinking", None)
            request_params.pop("output_config", None)

    # Store for use inside event_generator (assignments there make it local)
    _outer_context_start_index = context_start_index

    async def event_generator():
        nonlocal model_id, provider, client
        context_start_index = _outer_context_start_index
        accumulated_content = ""
        accumulated_thinking = ""

        # Characters /finalize: write character_profile.di from interview transcript,
        # exit interview mode, and yield a synthetic assistant message. Bypasses the
        # streaming model entirely.
        if characters_finalize_pending:
            from characters_runtime import finalize_interview as _characters_finalize
            _characters_dir = (
                get_project_dir(username, request.project)
                if request.project else None
            )
            # Build transcript scoped to the CURRENT interview session only — same
            # boundary logic as the interview-mode context filter. Without this, prior
            # finalized interviews and any correspondence in between would be sent to
            # the finalize agent as if they were part of this re-interview.
            _boundary_idx = 1
            for _i, _m in enumerate(branch_path):
                if _m.get("_characters_reinterview_boundary"):
                    _boundary_idx = _i
            _transcript_slice = branch_path[_boundary_idx:-1] if _boundary_idx < len(branch_path) - 1 else []
            _transcript = []
            for _m in _transcript_slice:
                _role = _m.get("role")
                _content = _m.get("content") or ""
                if (_role in ("user", "assistant")
                        and _content
                        and _m.get("_characters_interview_mode")
                        and not _m.get("characters_finalize")):
                    _transcript.append({"role": _role, "content": _content})

            yield f"event: init\ndata: {json.dumps({'user_message_id': user_msg_id})}\n\n"

            # Finalize takes 60-180s for an 8K-token profile. Run it in a task
            # so we can emit SSE keepalives every 10s — without them, nginx
            # (or any reverse proxy with a default ~60s idle timeout) kills the
            # connection mid-call and the frontend sees "network error" even
            # though the model is still generating successfully.
            _finalize_task = asyncio.create_task(
                asyncio.to_thread(_characters_finalize, client, data, _characters_dir, _transcript)
            )
            while not _finalize_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(_finalize_task), timeout=10.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
            _result = _finalize_task.result()
            _feedback = _result.get("feedback") or "Finalization completed."
            _ok = bool(_result.get("ok"))
            _usage = _result.get("usage") or {}
            _input_t = _usage.get("input_tokens", 0) or 0
            _output_t = _usage.get("output_tokens", 0) or 0
            _cost_value = float(_result.get("cost", 0.0) or 0.0)
            _cost_str = f"${_cost_value:.6f}"
            _tokens_str = f"{_input_t}↑ {_output_t}↓"
            _finalize_model = "claude-opus-4.5"

            # Synthesize an assistant message containing the feedback. Mirrors the field
            # shape of normal assistant messages so the frontend's done-event handler and
            # branch-switching logic don't break on missing keys.
            _assistant_msg_id = generate_message_id()
            _assistant_msg = {
                "id": _assistant_msg_id,
                "parent_id": user_msg_id,
                "role": "assistant",
                "content": _feedback,
                "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                "tokens": _tokens_str,
                "cost": _cost_str,
                "model": _finalize_model,
                "total_tokens": _input_t + _output_t,
                "total_claude_tokens": _input_t + _output_t,
                "characters_finalize": True,
                "characters_finalize_ok": _ok,
                # Interview-mode tag: keep this message + the user's /finalize msg out of
                # the correspondence model's context window on subsequent turns.
                "_characters_interview_mode": True,
            }
            if _ok and _result.get("path"):
                _assistant_msg["characters_profile_path"] = _result["path"]
            data["messages"].append(_assistant_msg)
            data["current_leaf_id"] = _assistant_msg_id

            # Update stats so the cost is reflected in the chat totals
            try:
                _stats = data.setdefault("stats", create_empty_stats())
                _stats["total_cost"] = float(_stats.get("total_cost", 0.0) or 0.0) + _cost_value
                _stats["total_input_tokens"] = int(_stats.get("total_input_tokens", 0) or 0) + _input_t
                _stats["total_output_tokens"] = int(_stats.get("total_output_tokens", 0) or 0) + _output_t
            except Exception:
                pass

            save_chat(username, request.chat_name, data, request.project)

            yield f"event: content\ndata: {json.dumps({'delta': _feedback})}\n\n"

            _branch_path_final = get_path_to_root(data["messages"], _assistant_msg_id)
            _done_data = {
                "assistant_message": _feedback,
                "tokens": _tokens_str,
                "cost": _cost_str,
                "stats": data.get("stats", create_empty_stats()),
                "context_start_index": 1,
                "reasoning": None,
                "user_message_id": user_msg_id,
                "assistant_message_id": _assistant_msg_id,
                "current_leaf_id": _assistant_msg_id,
                "total_messages": len(_branch_path_final),
                "model": _finalize_model,
                "hud_state": None,
                "characters_finalize_ok": _ok,
                "profile_path": _result.get("path"),
                "_characters_interview_mode": True,
            }
            yield f"event: done\ndata: {json.dumps(_done_data)}\n\n"
            return

        # Characters /consolidate: Opus 4.5 reads profile + growth + memories,
        # proposes a merged profile. Stored on data for /accept-consolidation
        # or /reject-consolidation. Bypasses streaming entirely (mirrors /finalize).
        if characters_consolidate_pending:
            from characters_runtime import run_consolidate as _characters_consolidate, branch_msg_ids_from_branch_path
            _characters_dir = (
                get_project_dir(username, request.project)
                if request.project else None
            )
            _consolidate_branch_ids = branch_msg_ids_from_branch_path(branch_path)

            yield f"event: init\ndata: {json.dumps({'user_message_id': user_msg_id})}\n\n"

            # Same long-call keepalive pattern as finalize. Consolidate also
            # takes 60-180s and was hitting the proxy idle timeout.
            _consolidate_task = asyncio.create_task(
                asyncio.to_thread(
                    _characters_consolidate, client, data, _characters_dir,
                    branch_msg_ids=_consolidate_branch_ids,
                )
            )
            while not _consolidate_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(_consolidate_task), timeout=10.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
            _result = _consolidate_task.result()
            _feedback = _result.get("feedback") or "Consolidation completed."
            _ok = bool(_result.get("ok"))
            _usage = _result.get("usage") or {}
            _input_t = _usage.get("input_tokens", 0) or 0
            _output_t = _usage.get("output_tokens", 0) or 0
            _cost_value = float(_result.get("cost", 0.0) or 0.0)
            _cost_str = f"${_cost_value:.6f}"
            _tokens_str = f"{_input_t}↑ {_output_t}↓"
            _consolidate_model = "claude-opus-4.5"

            _assistant_msg_id = generate_message_id()
            _assistant_msg = {
                "id": _assistant_msg_id,
                "parent_id": user_msg_id,
                "role": "assistant",
                "content": _feedback,
                "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                "tokens": _tokens_str,
                "cost": _cost_str,
                "model": _consolidate_model,
                "total_tokens": _input_t + _output_t,
                "total_claude_tokens": _input_t + _output_t,
                "characters_consolidate": True,
                "characters_consolidate_ok": _ok,
            }
            data["messages"].append(_assistant_msg)
            data["current_leaf_id"] = _assistant_msg_id

            try:
                _stats = data.setdefault("stats", create_empty_stats())
                _stats["total_cost"] = float(_stats.get("total_cost", 0.0) or 0.0) + _cost_value
                _stats["total_input_tokens"] = int(_stats.get("total_input_tokens", 0) or 0) + _input_t
                _stats["total_output_tokens"] = int(_stats.get("total_output_tokens", 0) or 0) + _output_t
            except Exception:
                pass

            save_chat(username, request.chat_name, data, request.project)

            yield f"event: content\ndata: {json.dumps({'delta': _feedback})}\n\n"

            _branch_path_final = get_path_to_root(data["messages"], _assistant_msg_id)
            _done_data = {
                "assistant_message": _feedback,
                "tokens": _tokens_str,
                "cost": _cost_str,
                "stats": data.get("stats", create_empty_stats()),
                "context_start_index": 1,
                "reasoning": None,
                "user_message_id": user_msg_id,
                "assistant_message_id": _assistant_msg_id,
                "current_leaf_id": _assistant_msg_id,
                "total_messages": len(_branch_path_final),
                "model": _consolidate_model,
                "hud_state": None,
                "characters_consolidate_ok": _ok,
            }
            yield f"event: done\ndata: {json.dumps(_done_data)}\n\n"
            return

        ship_combat_triggered_this_turn = False
        ship_combat_started_this_turn = bool(use_ship_combat_mode and not any(m.get("ship_combat_mode") for m in branch_path[1:-1]))
        ship_combat_opening_narration_hint = (((data.get("pipeline_state") or {}).get("ship_combat") or {}).get("opening_narration")
                                              if use_ship_combat_mode else None)
        ship_combat_bootstrap_messages_snapshot = None
        ship_combat_init_hidden_message_data = copy.deepcopy(ship_combat_init_hidden_message_prebuilt) if ship_combat_init_hidden_message_prebuilt else None
        # Chase mode first-exchange tracking (parallel to ship_combat above).
        chase_started_this_turn = bool(use_chase_mode and not any(m.get("chase_mode") for m in branch_path[1:-1]))
        chase_init_hidden_message_data = copy.deepcopy(chase_init_hidden_message_prebuilt) if chase_init_hidden_message_prebuilt else None

        def _ship_combat_trigger_is_strong(sc: dict) -> bool:
            if not isinstance(sc, dict):
                return False
            if not sc.get("handoff_summary"):
                return False
            detail_fields = [
                sc.get("encounter_type"),
                sc.get("objective"),
                sc.get("positioning"),
                sc.get("immediate_complications"),
                sc.get("enemy_ships"),
            ]
            return any(bool(v) for v in detail_fields)

        def _ship_opening_embedded(opening: str | None, content: str | None) -> bool:
            if not opening or not content:
                return False
            norm = lambda s: " ".join(str(s).lower().split())
            return norm(opening) in norm(content)

        async def _run_ship_combat_gpt_exchange(
            parent_msg_id: str,
            is_first_exchange: bool,
            opening_narration_hint: str | None,
            result_out: dict,
        ):
            """Run a single ship combat GPT-5.2 exchange (bootstrap + API call + state apply).

            Yields SSE event strings. Mutates data in place. Writes results into result_out.
            Must be called via `async for event in _run_ship_combat_gpt_exchange(...)`.
            Closure over event_generator locals: data, gs, provider, client, username, request,
            branch_path, chat_key, sync_manager.
            """
            nonlocal context_start_index

            if data.get("pipeline_state"):
                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

            _sc_state = (data.get("pipeline_state") or {}).get("ship_combat") or {}
            _bootstrap_done = bool(_sc_state.get("bootstrap_done"))
            bootstrap_usage = {
                'input_tokens': 0, 'cache_read_tokens': 0,
                'cache_creation_tokens': 0, 'output_tokens': 0, 'reasoning_tokens': 0,
            }
            _local_opening_narration_hint = opening_narration_hint
            _local_bootstrap_messages_snapshot = None
            _local_hidden_init_data = None

            try:
                if not _bootstrap_done:
                    if _ship_combat_trigger_is_strong(_sc_state):
                        _sc_state["bootstrap_done"] = True
                        _sc_state["ship_combat_handoff_source"] = "trigger"
                        if not _sc_state.get("bootstrap_messages"):
                            _trigger_brief = {
                                "environment": _sc_state.get("environment"),
                                "encounter_type": _sc_state.get("encounter_type"),
                                "objective": _sc_state.get("objective"),
                                "positioning": _sc_state.get("positioning"),
                                "immediate_complications": _sc_state.get("immediate_complications", []),
                                "enemy_ships": _sc_state.get("enemy_ships", []),
                                "handoff_summary": _sc_state.get("handoff_summary"),
                            }
                            _assistant_bootstrap_text = (_sc_state.get("opening_narration") or "").strip()
                            if _sc_state.get("handoff_summary"):
                                _assistant_bootstrap_text = (
                                    f"[HIDDEN SHIP COMBAT HANDOFF SUMMARY]\n{_sc_state.get('handoff_summary')}\n[/HIDDEN SHIP COMBAT HANDOFF SUMMARY]\n\n"
                                    + _assistant_bootstrap_text
                                ).strip()
                            _sc_state["bootstrap_messages"] = [
                                {
                                    "role": "user",
                                    "content": "Generate a set-up for ship combat mode briefly summarizing the immediate scene and lead-in.\n"
                                               + json.dumps(_trigger_brief, indent=2),
                                    "ship_combat_bootstrap_hidden": True,
                                },
                                {
                                    "role": "assistant",
                                    "content": _assistant_bootstrap_text or (_sc_state.get("handoff_summary") or ""),
                                    "ship_combat_bootstrap_hidden": True,
                                },
                            ]
                    else:
                        bootstrap_schema = {
                            "type": "object",
                            "required": ["handoff_summary", "opening_narration"],
                            "properties": {
                                "handoff_summary": {"type": "string"},
                                "opening_narration": {"type": "string"},
                                "encounter_type": {"type": ["string", "null"]},
                                "objective": {"type": ["string", "null"]},
                                "positioning": {"type": ["string", "null"]},
                                "immediate_complications": {"type": "array", "items": {"type": "string"}}
                            }
                        }
                        bootstrap_system = (
                            "You generate a hidden ship combat handoff bootstrap for a TTRPG app. "
                            "Return JSON only. Produce a canonical handoff_summary AND a short player-facing opening narration. "
                            "\n\n"
                            "HANDOFF SUMMARY (2-3 paragraphs):\n"
                            "Cover, in order: (1) what's going on right now — the immediate situation as the scene opens, "
                            "(2) why we're here — the chain of choices and pressures from prior scenes that brought the crew "
                            "to this moment, and (3) what the goal is — what success looks like, what failure looks like, "
                            "and any constraints or stakes the GM should hold. Write in flowing prose, not bullets. The "
                            "summary should read like a story DM telling another DM 'here's where we are' so the next mode "
                            "can pick up seamlessly without losing context. Reference the immediate prior scene's arc and "
                            "any unresolved tension being carried forward. NOT a tactical snapshot of the battlefield; the "
                            "engine state already covers that.\n"
                            "\n"
                            "OPENING NARRATION (1 short paragraph):\n"
                            "The first beat the player sees as combat opens. Sensory, in-fiction, no rules talk.\n"
                            "\n"
                            "Do not resolve combat. Do not generate initiative, ship stats, or outcomes."
                        )
                        bootstrap_user_payload = {
                            "task": "Create a hidden ship-combat handoff bootstrap because the trigger lacks detail.",
                            "current_user_message": build_message_content(branch_path[-1]),
                            "ship_combat_trigger_state": _sc_state,
                        }
                        bootstrap_messages = [
                            {"role": "system", "content": bootstrap_system + "\n\nSchema:\n" + json.dumps(bootstrap_schema, indent=2)},
                            {"role": "user", "content": json.dumps(bootstrap_user_payload, indent=2)},
                        ]
                        bootstrap_params = provider.build_pipeline_request(
                            messages=bootstrap_messages,
                            username=username,
                            project=request.project or "",
                            chat_name=request.chat_name,
                            stage_name="ship_combat_bootstrap",
                            reasoning_effort="low",
                            json_mode=True,
                        )
                        bootstrap_json = {}
                        bootstrap_ok = False
                        # Bounded same-turn retry with minimal bootstrap-only context.
                        for bootstrap_attempt in range(2):
                            bootstrap_resp = await asyncio.to_thread(
                                provider.send_request_non_streaming,
                                client, bootstrap_params, 45.0
                            )
                            for k in bootstrap_usage:
                                bootstrap_usage[k] += bootstrap_resp.get(k, 0) or 0
                            bootstrap_content = bootstrap_resp.get("content", "")
                            try:
                                bootstrap_json = json.loads(bootstrap_content) if bootstrap_content else {}
                            except json.JSONDecodeError:
                                logger.warning(
                                    f"Ship combat bootstrap parse failed for {username} "
                                    f"(attempt {bootstrap_attempt + 1}/2): {bootstrap_content[:200]}"
                                )
                                bootstrap_json = {}
                                continue

                            if not isinstance(bootstrap_json, dict):
                                logger.warning(
                                    f"Ship combat bootstrap returned non-object JSON for {username} "
                                    f"(attempt {bootstrap_attempt + 1}/2)"
                                )
                                bootstrap_json = {}
                                continue

                            if bootstrap_json.get("handoff_summary") or bootstrap_json.get("opening_narration"):
                                bootstrap_ok = True
                                break

                            logger.warning(
                                f"Ship combat bootstrap missing handoff/opening for {username} "
                                f"(attempt {bootstrap_attempt + 1}/2)"
                            )

                        if bootstrap_ok:
                            if bootstrap_json.get("handoff_summary"):
                                _sc_state["handoff_summary"] = bootstrap_json.get("handoff_summary")
                            if bootstrap_json.get("opening_narration"):
                                _sc_state["opening_narration"] = bootstrap_json.get("opening_narration")
                            for _f in ("encounter_type", "objective", "positioning"):
                                if not _sc_state.get(_f) and bootstrap_json.get(_f):
                                    _sc_state[_f] = bootstrap_json.get(_f)
                            if not _sc_state.get("immediate_complications") and bootstrap_json.get("immediate_complications"):
                                _sc_state["immediate_complications"] = bootstrap_json.get("immediate_complications")
                            _sc_state["bootstrap_done"] = True
                            _sc_state["ship_combat_handoff_source"] = "bootstrap"
                            _sc_state["bootstrap_messages"] = [
                                {
                                    "role": "user",
                                    "content": "Generate a set-up for ship combat mode briefly summarizing the immediate scene and lead-in.\n"
                                               + json.dumps(bootstrap_user_payload, indent=2),
                                    "ship_combat_bootstrap_hidden": True,
                                },
                                {
                                    "role": "assistant",
                                    "content": (
                                        f"[HIDDEN SHIP COMBAT HANDOFF SUMMARY]\n{bootstrap_json.get('handoff_summary', '')}\n[/HIDDEN SHIP COMBAT HANDOFF SUMMARY]\n\n"
                                        f"{bootstrap_json.get('opening_narration', '')}"
                                    ).strip(),
                                    "ship_combat_bootstrap_hidden": True,
                                },
                            ]
                            logger.info(f"Ship combat hidden bootstrap generated for {username}")
                        else:
                            logger.warning(
                                f"Ship combat hidden bootstrap unavailable for {username}; "
                                "leaving bootstrap pending for next ship combat exchange"
                            )

                    _local_opening_narration_hint = _sc_state.get("opening_narration") or _local_opening_narration_hint
            except Exception as _sc_bootstrap_err:
                logger.warning(f"Ship combat bootstrap/handoff prep failed for {username}: {_sc_bootstrap_err}")

            # Build ship combat system content and request
            _ps = data.get("pipeline_state", {})
            _sc = _ps.get("ship_combat", {})
            _ship_contract = gs["ship_combat_contract"]
            _ship_profile = gs["build_ship_combat_profile"](_ps.get("character_states", {}), _sc)
            _ship_injection = gs["build_ship_combat_injection"](_sc, _ps)
            _ship_system_content = _ship_contract + ("\n\n" + _ship_profile if _ship_profile else "")
            if request.project:
                uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
                tokens_cache = load_file_tokens_cache(username, request.project)
                for fname in ["Ship Systems.md", "Core Conversion.md"]:
                    if not tokens_cache.get(fname, {}).get("staged", True):
                        continue
                    fpath = os.path.join(uploads_dir, fname)
                    if os.path.exists(fpath):
                        with open(fpath, 'r', encoding='utf-8') as f:
                            _ship_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                for fname in ["Character Sheets.md", "Character Sheets.yaml"]:
                    if not tokens_cache.get(fname, {}).get("staged", True):
                        continue
                    fpath = os.path.join(uploads_dir, fname)
                    if os.path.exists(fpath):
                        with open(fpath, 'r', encoding='utf-8') as f:
                            _ship_system_content += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                        break

            # Build history
            _ship_history = []
            for _bm in (_sc.get("bootstrap_messages") or []):
                if isinstance(_bm, dict) and _bm.get("role") in ("user", "assistant") and isinstance(_bm.get("content"), str):
                    _ship_history.append({"role": _bm["role"], "content": _bm["content"]})
            _found_start = not _sc.get("start_message_id")
            for _m in branch_path[1:-1]:
                if not _found_start and _m.get("id") == _sc.get("start_message_id"):
                    _found_start = True
                if _found_start and _m.get("ship_combat_mode"):
                    _ship_history.append({"role": _m["role"], "content": _m["content"]})

            # Build user message
            _user_content = build_message_content(branch_path[-1])
            _dice = generate_dice_pool(gs["id"]) if gs else ""
            if is_first_exchange:
                _local_hidden_init_data = build_ship_combat_hidden_init_message(
                    parent_msg_id,
                    opening_override=_local_opening_narration_hint
                )
                _new_user_msg = {"role": "user", "content": _local_hidden_init_data["content"]}
            else:
                _visible_ship_user_content = _ship_injection + "\n\n" + (_dice + "\n\n" if _dice else "") + _user_content
                _new_user_msg = {"role": "user", "content": _visible_ship_user_content}

            ship_combat_request_params = provider.build_pipeline_request(
                messages=[
                    {"role": "system", "content": _ship_system_content + "\n\nYou MUST output valid JSON matching the report_ship_combat_state schema:\n" + json.dumps(gs["ship_combat_tool"]["input_schema"], indent=2)}
                ] + _ship_history + [_new_user_msg],
                username=username,
                project=request.project or "",
                chat_name=request.chat_name,
                stage_name="ship_combat",
                reasoning_effort="medium",
                json_mode=True,
            )

            # Make API call
            ship_combat_response = await asyncio.to_thread(
                provider.send_request_non_streaming,
                client, ship_combat_request_params, 60.0
            )

            ship_combat_content = ship_combat_response.get('content', '')
            ship_combat_reasoning = ship_combat_response.get('reasoning')

            ship_combat_json_valid = True
            try:
                ship_combat_json = json.loads(ship_combat_content)
            except json.JSONDecodeError:
                logger.error(f"Ship combat mode: failed to parse JSON: {ship_combat_content[:200]}")
                ship_combat_json_valid = False
                ship_combat_json = {}

            narrative = ship_combat_json.get("narrative", ship_combat_content)
            if narrative:
                yield f"event: content\ndata: {json.dumps({'delta': narrative})}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": narrative})
                )

            # Snapshot bootstrap messages before state apply
            if is_first_exchange:
                _sc_dbg_pre = (data.get("pipeline_state", {}).get("ship_combat") or {})
                if _sc_dbg_pre.get("bootstrap_messages"):
                    _local_bootstrap_messages_snapshot = copy.deepcopy(_sc_dbg_pre.get("bootstrap_messages"))

            # Apply state only if ship-combat JSON parsed successfully.
            # A malformed/truncated model response should not clear the active encounter.
            if ship_combat_json_valid:
                ship_combat_ps = data.get("pipeline_state", {})
                gs["apply_ship_combat_state"](ship_combat_ps, ship_combat_json)
                # Ship combat clock: advance by game-system-defined ship round duration
                _ship_secs = gs.get("ship_combat_round_seconds") if gs else None
                _advance_mode_hud_clock(ship_combat_ps, _ship_secs)
                data["pipeline_state"] = ship_combat_ps

                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                )

                ship_notifs = extract_ship_combat_notifications(ship_combat_json)
                if ship_notifs:
                    yield f"event: state_notifications\ndata: {json.dumps(ship_notifs)}\n\n"
                    await sync_manager.broadcast_to_chat(
                        chat_key,
                        SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": ship_notifs})
                    )

            # Calculate costs
            usage = {
                'input_tokens': ship_combat_response.get('input_tokens', 0) + bootstrap_usage.get('input_tokens', 0),
                'cache_read_tokens': ship_combat_response.get('cache_read_tokens', 0) + bootstrap_usage.get('cache_read_tokens', 0),
                'cache_creation_tokens': ship_combat_response.get('cache_creation_tokens', 0) + bootstrap_usage.get('cache_creation_tokens', 0),
                'output_tokens': ship_combat_response.get('output_tokens', 0) + bootstrap_usage.get('output_tokens', 0),
                'reasoning_tokens': ship_combat_response.get('reasoning_tokens', 0) + bootstrap_usage.get('reasoning_tokens', 0),
            }

            from providers import ParsedResponse
            parsed = ParsedResponse(
                content=narrative,
                reasoning=ship_combat_reasoning,
                input_tokens=usage['input_tokens'],
                cache_read_tokens=usage['cache_read_tokens'],
                cache_creation_tokens=usage['cache_creation_tokens'],
                output_tokens=usage['output_tokens'],
                reasoning_tokens=usage['reasoning_tokens']
            )

            service_tier = ship_combat_response.get('service_tier')
            new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
            total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

            if service_tier:
                total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
            else:
                total_cost = provider.calculate_cost(parsed)
            tokens_str = provider.format_token_string(parsed)

            actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

            stats = data.get("stats", create_empty_stats())
            stats["total_input_tokens"] += new_input_tokens
            stats["total_cached_tokens"] += parsed.cache_read_tokens
            stats["total_output_tokens"] += parsed.output_tokens
            stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
            stats["total_cost"] += actual_cost
            stats["total_prompts"] += 1
            stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
            data["stats"] = stats

            # Build assistant message
            assistant_msg_id = generate_message_id()
            assistant_parent_id = parent_msg_id
            if is_first_exchange and _local_hidden_init_data:
                data["messages"].append(_local_hidden_init_data)
                assistant_parent_id = _local_hidden_init_data["id"]

            assistant_msg_data = {
                "id": assistant_msg_id,
                "parent_id": assistant_parent_id,
                "role": "assistant",
                "content": narrative,
                "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                "tokens": tokens_str,
                "cost": cost_str,
                "total_tokens": usage['output_tokens'],
                "total_gpt_tokens": usage['output_tokens'],
                "model": model_id,
                "ship_combat_mode": True,
                "ship_combat_tool_input": ship_combat_json,
            }
            if ship_combat_json_valid and ship_combat_json.get("combat_outcome"):
                assistant_msg_data["ship_combat_combat_outcome"] = ship_combat_json["combat_outcome"]
            if is_first_exchange:
                assistant_msg_data["ship_combat_started"] = True
                if _local_opening_narration_hint:
                    assistant_msg_data["ship_combat_opening_narration"] = _local_opening_narration_hint
                    assistant_msg_data["ship_combat_opening_embedded"] = _ship_opening_embedded(
                        _local_opening_narration_hint, narrative
                    )
                _sc_dbg = (data.get("pipeline_state", {}).get("ship_combat") or {})
                if _sc_dbg.get("bootstrap_messages"):
                    assistant_msg_data["ship_combat_bootstrap_messages"] = copy.deepcopy(_sc_dbg.get("bootstrap_messages"))
                elif _local_bootstrap_messages_snapshot:
                    assistant_msg_data["ship_combat_bootstrap_messages"] = copy.deepcopy(_local_bootstrap_messages_snapshot)
            if ship_combat_reasoning:
                assistant_msg_data["reasoning"] = ship_combat_reasoning
            if service_tier:
                assistant_msg_data["service_tier"] = service_tier
            if data.get("pipeline_state"):
                assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])

            _active_ship_combat = data.get("pipeline_state", {}).get("ship_combat")
            if _active_ship_combat and "start_message_id" not in _active_ship_combat:
                _active_ship_combat["start_message_id"] = assistant_msg_id

            _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
            data["current_leaf_id"] = assistant_msg_id
            save_chat(username, request.chat_name, data, request.project)

            if pending_usage is not None:
                save_daily_usage(username, pending_usage)

            update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens,
                                    parsed.output_tokens, parsed.reasoning_tokens, actual_cost,
                                    model=model_id, context_tokens=0)

            branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)

            # Write results for the caller
            result_out.update({
                "assistant_msg_id": assistant_msg_id,
                "assistant_msg_data": assistant_msg_data,
                "hidden_init_data": _local_hidden_init_data,
                "narrative": narrative,
                "reasoning": ship_combat_reasoning,
                "tokens_str": tokens_str,
                "cost_str": cost_str,
                "stats": stats,
                "service_tier": service_tier,
                "opening_narration_hint": _local_opening_narration_hint,
                "ship_combat_json": ship_combat_json,
                "branch_path_final": branch_path_final,
            })

        try:
            # Send init event with user message ID
            yield f"event: init\ndata: {json.dumps({'user_message_id': user_msg_id})}\n\n"

            # Novels: hard stop if context exceeds token ceiling
            if _novels_context_error:
                yield f"event: error\ndata: {json.dumps({'detail': _novels_context_error})}\n\n"
                # Remove the optimistically-added user message
                if user_msg_id:
                    data["messages"] = [m for m in data["messages"] if m.get("id") != user_msg_id]
                    save_chat(username, request.chat_name, data, request.project)
                return

            # Notify if docs were refreshed on context trim
            if docs_refreshed:
                yield f"event: docs_refreshed\ndata: {json.dumps({'message': 'Instructions and project files refreshed'})}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.DOCS_REFRESHED, data={})
                )

            # user_message_added broadcast moved before StreamingResponse

            # Check if this is a pipeline-eligible request (GPT-5.2 + project chat)
            # Hack mode and combat mode bypass the pipeline — use single-agent calls instead
            use_pipeline = model_id.startswith("gpt") and request.project and not use_hack_mode and not use_combat_mode and not use_net_combat_mode and not use_ship_combat_mode and not use_chase_mode and gs.get("use_pipeline", True)
            # use_stateful, use_hack_mode, use_combat_mode, use_ship_combat_mode are computed in the outer scope (before event_generator)
            use_mode_pipeline = model_id.startswith("gpt") and (use_hack_mode or use_combat_mode or use_net_combat_mode) and gs and gs.get("deterministic_mechanics") and gs.get("use_pipeline", True)

            if use_mode_pipeline and use_hack_mode:
                # ============================================================
                # GPT-5.2 Hack mode: 2-stage mode pipeline (Planning → Narration)
                # ============================================================
                logger.info(f"Hack mode pipeline (GPT-5.2): starting for {username}")

                # Emit hack_mode_active event
                yield f"event: hack_state_update\ndata: {json.dumps(hack_state)}\n\n"

                # Build planning system prompt (hack contract + hacker profile + project files)
                hack_planning_system = gs["hack_planning_contract"]
                if hacker_profile:
                    hack_planning_system += "\n\n" + hacker_profile
                if request.project:
                    rulebook_path = os.path.join(get_project_dir(username, request.project), "uploads", "Hacking Rulebook.md")
                    if os.path.exists(rulebook_path):
                        with open(rulebook_path, 'r', encoding='utf-8') as f:
                            hack_planning_system += f"\n\n{'='*60}\nFILE: Hacking Rulebook.md\n{'='*60}\n\n" + f.read()

                hack_narration_system = gs["hack_narration_contract"]

                # Run mode pipeline
                _PIPELINE_STOP = object()
                def _next_mode_event(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return _PIPELINE_STOP

                _planning_prov = ProviderRegistry.get(PIPELINE_PLANNING_MODEL) or provider
                _narration_prov = ProviderRegistry.get(PIPELINE_NARRATION_MODEL) or provider
                logger.info(
                    f"Hack mode pipeline: planner={getattr(_planning_prov, 'MODEL_NAME', '?')}, "
                    f"narrator={getattr(_narration_prov, 'MODEL_NAME', '?')}"
                )
                mode_gen = run_mode_pipeline(
                    provider=provider, client=client,
                    planning_provider=_planning_prov,
                    narration_provider=_narration_prov,
                    username=username, project=request.project or "",
                    chat_name=request.chat_name, mode="hack",
                    planning_system=hack_planning_system,
                    narration_system=hack_narration_system,
                    mode_messages=hack_history,
                    user_content=user_content,
                    planning_schema=gs["hack_planning_schema"],
                    game_state=hack_ps.get("game_state"),
                    character_states=hack_ps.get("character_states"),
                    pipeline_state=hack_ps,
                    tar_stacks=_safe_int(hack_state.get("tar_stacks", 0)) if isinstance(hack_state, dict) else 0,
                    alert_level=_safe_int(hack_state.get("alert_level", 0)) if isinstance(hack_state, dict) else 0,
                    active_programs=hack_state.get("active_programs") if isinstance(hack_state, dict) else None,
                    installed_hardware=hack_state.get("installed_hardware") if isinstance(hack_state, dict) else None,
                    ice_status=hack_state.get("ice_status") if isinstance(hack_state, dict) else None,
                    net_actions_remaining=(_safe_int(hack_state.get("net_actions_remaining"))
                                           if isinstance(hack_state, dict) and hack_state.get("net_actions_remaining") is not None
                                           else None),
                    slide_used_this_turn=bool(hack_state.get("slide_used_this_turn", False))
                                          if isinstance(hack_state, dict) else False,
                    system_map=hack_state.get("system_map") if isinstance(hack_state, dict) else None,
                    current_node=hack_state.get("current_node") if isinstance(hack_state, dict) else None,
                    virus_ledger=hack_ps.get("virus_ledger") if isinstance(hack_ps, dict) else None,
                    stealth_active=bool(hack_state.get("stealth_active", False))
                                    if isinstance(hack_state, dict) else False,
                    quiet_jack_in_used=bool(hack_state.get("quiet_jack_in_used", False))
                                       if isinstance(hack_state, dict) else False,
                    stealth_broken_round=hack_state.get("stealth_broken_round")
                                         if isinstance(hack_state, dict) else None,
                    net_round=_safe_int(hack_state.get("net_round", 1) or 1)
                              if isinstance(hack_state, dict) else 1,
                    backdoor_grace=hack_state.get("backdoor_grace")
                                   if isinstance(hack_state, dict) else None,
                )

                mode_result = None
                while True:
                    pipeline_future = asyncio.ensure_future(
                        asyncio.to_thread(_next_mode_event, mode_gen))
                    while not pipeline_future.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(pipeline_future), timeout=15.0)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                    result = pipeline_future.result()
                    if result is _PIPELINE_STOP:
                        break
                    event_type, event_data = result
                    if event_type == "pipeline_stage":
                        yield f"event: pipeline_stage\ndata: {json.dumps(event_data)}\n\n"
                    elif event_type == "content":
                        accumulated_content += event_data["delta"]
                        yield f"event: content\ndata: {json.dumps(event_data)}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": event_data["delta"]}))
                    elif event_type == "pipeline_done":
                        mode_result = event_data

                if mode_result is None:
                    raise Exception("Hack mode pipeline completed without result")

                # Build hack_json compatible structure from planning output + resolved actions
                hack_json = mode_result.planning_json
                hack_json["narrative"] = mode_result.final_content
                # Nest hack_state_updates under hack_state key for apply_hack_state
                hack_state_updates = hack_json.get("hack_state_updates", {})
                if hack_state_updates:
                    hack_json["hack_state"] = hack_state_updates
                narrative = mode_result.final_content
                hack_reasoning = "\n".join(mode_result.reasoning_summaries) if mode_result.reasoning_summaries else None

                if hack_reasoning:
                    accumulated_thinking = hack_reasoning

                # Apply hack state updates
                _apply_hack_state_compat(
                    gs["apply_hack_state"],
                    hack_state,
                    hack_json,
                    resolver_state_ops=mode_result.state_ops,
                    game_state=hack_ps.get("game_state"),
                    pipeline_state=hack_ps,
                    username=username,
                    project=request.project or "",
                )
                # virus_op state ops live at pipeline_state level, not hack_state —
                # apply_hack_state ignores them. Route them through here.
                _apply_virus_op_state_ops(hack_ps, mode_result.state_ops)
                # Also accept any virus_ops the planner emitted directly in hack_json
                # (only effective once virus_ops is in HACK_PLANNING_SCHEMA — P2).
                _planner_v_ops = hack_json.get("virus_ops") if isinstance(hack_json, dict) else None
                if isinstance(_planner_v_ops, list) and _planner_v_ops:
                    from pipeline import apply_virus_ops as _apply_v_ops
                    hack_ps["virus_ledger"] = _apply_v_ops(
                        hack_ps.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
                        _planner_v_ops,
                        hack_ps.get("turn_counter", 0),
                        hack_ps.get("hud_state", {}).get("date", "")
                    )
                # Explicit re-assign for parity with combat/net_combat blocks.
                # hack_ps is a reference to data["pipeline_state"], so mutations
                # already propagate — but a future refactor swapping in a deep
                # copy would silently drop them. Belt-and-suspenders.
                data["pipeline_state"] = hack_ps
                data["hack_state"] = hack_state

                # Combat clock: advance by game-system-defined round duration
                _combat_secs = gs.get("combat_round_seconds") if gs else None
                _advance_mode_hud_clock(hack_ps, _combat_secs)

                # Emit updated hack state
                yield f"event: hack_state_update\ndata: {json.dumps(hack_state)}\n\n"

                if hack_json.get("hack_complete"):
                    yield f"event: hack_complete\ndata: {json.dumps({'summary': hack_state.get('narrative_summary', '')})}\n\n"
                    logger.info(f"Hack mode: completed for {username}: {hack_state.get('narrative_summary', '')[:100]}")

                # Build usage from mode pipeline aggregate
                usage = mode_result.aggregate_usage
                usage['content'] = narrative
                usage['reasoning'] = hack_reasoning

                assistant_message = narrative
                reasoning_summary = hack_reasoning
                service_tier = mode_result.service_tier_label

                from providers import ParsedResponse
                parsed = ParsedResponse(
                    content=assistant_message,
                    reasoning=reasoning_summary,
                    input_tokens=usage['input_tokens'],
                    cache_read_tokens=usage['cache_read_tokens'],
                    cache_creation_tokens=usage['cache_creation_tokens'],
                    output_tokens=usage['output_tokens'],
                    reasoning_tokens=usage['reasoning_tokens']
                )

                new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                if service_tier:
                    total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                else:
                    total_cost = provider.calculate_cost(parsed)
                tokens_str = provider.format_token_string(parsed)

                actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

                # Update stats
                stats = data.get("stats", create_empty_stats())
                stats["total_input_tokens"] += new_input_tokens
                stats["total_cached_tokens"] += parsed.cache_read_tokens
                stats["total_output_tokens"] += parsed.output_tokens
                stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                stats["total_cost"] += actual_cost
                stats["total_prompts"] += 1
                stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                data["stats"] = stats

                # Create assistant message (flagged as hack_mode)
                assistant_msg_id = generate_message_id()
                assistant_parent_id = user_msg_id
                if ship_combat_started_this_turn:
                    _sc_for_hidden = (data.get("pipeline_state", {}).get("ship_combat") or {})
                    _hidden_summary = (_sc_for_hidden.get("handoff_summary") or "").strip()
                    _hidden_opening = (ship_combat_opening_narration_hint or "").strip()
                    _hidden_payload = {
                        "summary": _hidden_summary,
                        "objective": _sc_for_hidden.get("objective"),
                        "positioning": _sc_for_hidden.get("positioning"),
                    }
                    hidden_content = (
                        "This is the current situation: "
                        f"{_hidden_summary or 'Use the ship combat trigger context and hidden handoff summary.'}\n"
                        "Initialize ship combat mode: generate participating ships, crews/role coverage, and initiative order based on the fiction, "
                        "then describe the opening exchange state."
                    )
                    if _hidden_opening:
                        hidden_content += f"\nOpening narration hint: {_hidden_opening}"
                    hidden_content += "\n\n" + json.dumps(_hidden_payload, indent=2)
                    hidden_init_msg_id = generate_message_id()
                    ship_combat_init_hidden_message_data = {
                        "id": hidden_init_msg_id,
                        "parent_id": user_msg_id,
                        "role": "user",
                        "content": hidden_content,
                        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                        "ship_combat_mode": True,
                        "ship_combat_system_init": True,
                        "ship_combat_hidden_init": True,
                    }
                    data["messages"].append(ship_combat_init_hidden_message_data)
                    assistant_parent_id = hidden_init_msg_id
                assistant_msg_data = {
                    "id": assistant_msg_id,
                    "parent_id": assistant_parent_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                    "tokens": tokens_str,
                    "cost": cost_str,
                    "total_tokens": usage['output_tokens'],
                    "total_gpt_tokens": usage['output_tokens'],
                    "model": model_id,
                    "hack_mode": True,
                    "hack_tool_input": hack_json,
                }
                if reasoning_summary:
                    assistant_msg_data["reasoning"] = reasoning_summary
                if service_tier:
                    assistant_msg_data["service_tier"] = service_tier
                if data.get("pipeline_state"):
                    assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])
                if data.get("hack_state"):
                    assistant_msg_data["hack_state_after"] = copy.deepcopy(data["hack_state"])

                _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id
                save_chat(username, request.chat_name, data, request.project)

                if pending_usage is not None:
                    save_daily_usage(username, pending_usage)

                update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens,
                                        parsed.output_tokens, parsed.reasoning_tokens, actual_cost,
                                        model=model_id, context_tokens=0)

                branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                done_data = {
                    'assistant_message': assistant_message,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': stats,
                    'context_start_index': context_start_index,
                    'reasoning': reasoning_summary,
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': len(branch_path_final),
                    'model': model_id,
                    'hack_mode': True,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if service_tier:
                    done_data['service_tier'] = service_tier
                if _original_model:
                    done_data['original_model'] = _original_model
                if hack_json.get("hack_complete"):
                    done_data['hack_complete'] = True
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": len(branch_path_final),
                            "stats": stats,
                            "context_start_index": context_start_index
                        }
                    )
                )

                logger.info(f"Hack mode (GPT-5.2): completed for {username}")

            elif use_mode_pipeline and use_combat_mode:
                # ============================================================
                # GPT-5.2 Combat mode: 2-stage mode pipeline (Planning → Narration)
                # ============================================================
                logger.info(f"Combat mode pipeline (GPT-5.2): starting for {username}")

                # Emit state_update so character panel refreshes with current combat state
                if data.get("pipeline_state"):
                    yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

                # Build planning system prompt (combat contract + profile + project files)
                combat_planning_system = gs["combat_planning_contract"]
                if combat_profile:
                    combat_planning_system += "\n\n" + combat_profile
                if request.project:
                    uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
                    tokens_cache = load_file_tokens_cache(username, request.project)
                    char_sheet_loaded = False
                    for fname in _combat_file_list(gs):
                        is_char_sheet = fname.startswith("Character Sheets")
                        if is_char_sheet and char_sheet_loaded:
                            continue
                        if not tokens_cache.get(fname, {}).get("staged", True):
                            continue
                        fpath = os.path.join(uploads_dir, fname)
                        if os.path.exists(fpath):
                            with open(fpath, 'r', encoding='utf-8') as f:
                                combat_planning_system += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                            if is_char_sheet:
                                char_sheet_loaded = True

                combat_narration_system = gs["combat_narration_contract"]

                _PIPELINE_STOP = object()
                def _next_mode_event(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return _PIPELINE_STOP

                _planning_prov = ProviderRegistry.get(PIPELINE_PLANNING_MODEL) or provider
                _narration_prov = ProviderRegistry.get(PIPELINE_NARRATION_MODEL) or provider
                logger.info(
                    f"Combat mode pipeline: planner={getattr(_planning_prov, 'MODEL_NAME', '?')}, "
                    f"narrator={getattr(_narration_prov, 'MODEL_NAME', '?')}"
                )
                mode_gen = run_mode_pipeline(
                    provider=provider, client=client,
                    planning_provider=_planning_prov,
                    narration_provider=_narration_prov,
                    username=username, project=request.project or "",
                    chat_name=request.chat_name, mode="combat",
                    planning_system=combat_planning_system,
                    narration_system=combat_narration_system,
                    mode_messages=combat_history,
                    user_content=user_content,
                    planning_schema=gs["combat_planning_schema"],
                    game_state=combat_ps.get("game_state"),
                    character_states=combat_ps.get("character_states"),
                    pipeline_state=combat_ps,
                    tar_stacks=_safe_int((_net_combat or {}).get("tar_stacks", 0)) if isinstance(_net_combat, dict) and _net_combat.get("active") else 0,
                    alert_level=_safe_int((_net_combat or {}).get("alert_level", 0)) if isinstance(_net_combat, dict) and _net_combat.get("active") else 0,
                    active_programs=(_net_combat or {}).get("active_programs") if isinstance(_net_combat, dict) and _net_combat.get("active") else None,
                    installed_hardware=(_net_combat or {}).get("installed_hardware") if isinstance(_net_combat, dict) and _net_combat.get("active") else None,
                    ice_status=(_net_combat or {}).get("ice_status") if isinstance(_net_combat, dict) and _net_combat.get("active") else None,
                    net_actions_remaining=(_safe_int((_net_combat or {}).get("net_actions_remaining"))
                                           if isinstance(_net_combat, dict) and _net_combat.get("active")
                                              and _net_combat.get("net_actions_remaining") is not None
                                           else None),
                    slide_used_this_turn=bool((_net_combat or {}).get("slide_used_this_turn", False))
                                          if isinstance(_net_combat, dict) and _net_combat.get("active")
                                          else False,
                    system_map=(_net_combat or {}).get("system_map")
                               if isinstance(_net_combat, dict) and _net_combat.get("active")
                               else None,
                    current_node=(_net_combat or {}).get("current_node")
                                 if isinstance(_net_combat, dict) and _net_combat.get("active")
                                 else None,
                    virus_ledger=combat_ps.get("virus_ledger") if isinstance(combat_ps, dict) else None,
                    stealth_active=bool((_net_combat or {}).get("stealth_active", False))
                                   if isinstance(_net_combat, dict) and _net_combat.get("active") else False,
                    quiet_jack_in_used=bool((_net_combat or {}).get("quiet_jack_in_used", False))
                                       if isinstance(_net_combat, dict) and _net_combat.get("active") else False,
                    stealth_broken_round=(_net_combat or {}).get("stealth_broken_round")
                                         if isinstance(_net_combat, dict) and _net_combat.get("active") else None,
                    net_round=_safe_int((_net_combat or {}).get("net_round", 1) or 1)
                              if isinstance(_net_combat, dict) and _net_combat.get("active") else 1,
                    backdoor_grace=(_net_combat or {}).get("backdoor_grace")
                                   if isinstance(_net_combat, dict) and _net_combat.get("active") else None,
                )

                mode_result = None
                while True:
                    pipeline_future = asyncio.ensure_future(
                        asyncio.to_thread(_next_mode_event, mode_gen))
                    while not pipeline_future.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(pipeline_future), timeout=15.0)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                    result = pipeline_future.result()
                    if result is _PIPELINE_STOP:
                        break
                    event_type, event_data = result
                    if event_type == "pipeline_stage":
                        yield f"event: pipeline_stage\ndata: {json.dumps(event_data)}\n\n"
                    elif event_type == "content":
                        accumulated_content += event_data["delta"]
                        yield f"event: content\ndata: {json.dumps(event_data)}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": event_data["delta"]}))
                    elif event_type == "pipeline_done":
                        mode_result = event_data

                if mode_result is None:
                    raise Exception("Combat mode pipeline completed without result")

                # Build combat_json compatible structure from planning output + resolved actions
                combat_json = mode_result.planning_json
                combat_json["narrative"] = mode_result.final_content
                _strip_and_merge_resolver_ops(combat_json, mode_result.state_ops)

                narrative = mode_result.final_content
                combat_reasoning = "\n".join(mode_result.reasoning_summaries) if mode_result.reasoning_summaries else None

                if combat_reasoning:
                    accumulated_thinking = combat_reasoning

                # Apply combat state updates
                combat_ps = data.get("pipeline_state", {})
                _apply_combat_state(gs, combat_ps, combat_json)
                _apply_tar_consumed_state_ops(combat_ps, mode_result.state_ops)
                _apply_virus_op_state_ops(combat_ps, mode_result.state_ops)
                data["pipeline_state"] = combat_ps

                # Combat clock: advance by game-system-defined round duration
                _combat_secs = gs.get("combat_round_seconds") if gs else None
                _advance_mode_hud_clock(combat_ps, _combat_secs)

                # Emit state_update SSE event with updated pipeline_state
                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                )

                # Build usage from mode pipeline aggregate
                usage = mode_result.aggregate_usage
                usage['content'] = narrative
                usage['reasoning'] = combat_reasoning

                assistant_message = narrative
                reasoning_summary = combat_reasoning
                service_tier = mode_result.service_tier_label

                from providers import ParsedResponse
                parsed = ParsedResponse(
                    content=assistant_message,
                    reasoning=reasoning_summary,
                    input_tokens=usage['input_tokens'],
                    cache_read_tokens=usage['cache_read_tokens'],
                    cache_creation_tokens=usage['cache_creation_tokens'],
                    output_tokens=usage['output_tokens'],
                    reasoning_tokens=usage['reasoning_tokens']
                )

                new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                if service_tier:
                    total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                else:
                    total_cost = provider.calculate_cost(parsed)
                tokens_str = provider.format_token_string(parsed)

                actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

                # Update stats
                stats = data.get("stats", create_empty_stats())
                stats["total_input_tokens"] += new_input_tokens
                stats["total_cached_tokens"] += parsed.cache_read_tokens
                stats["total_output_tokens"] += parsed.output_tokens
                stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                stats["total_cost"] += actual_cost
                stats["total_prompts"] += 1
                stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                data["stats"] = stats

                # Create assistant message (flagged as combat_mode)
                assistant_msg_id = generate_message_id()
                assistant_parent_id = user_msg_id
                if ship_combat_started_this_turn:
                    _sc_for_hidden = (data.get("pipeline_state", {}).get("ship_combat") or {})
                    _hidden_summary = (_sc_for_hidden.get("handoff_summary") or "").strip()
                    _hidden_opening = (ship_combat_opening_narration_hint or "").strip()
                    _hidden_payload = {
                        "summary": _hidden_summary,
                        "objective": _sc_for_hidden.get("objective"),
                        "positioning": _sc_for_hidden.get("positioning"),
                    }
                    hidden_content = (
                        "This is the current situation: "
                        f"{_hidden_summary or 'Use the ship combat trigger context and hidden handoff summary.'}\n"
                        "Initialize ship combat mode: generate participating ships, crews/role coverage, and initiative order based on the fiction, "
                        "then describe the opening exchange state."
                    )
                    if _hidden_opening:
                        hidden_content += f"\nOpening narration hint: {_hidden_opening}"
                    hidden_content += "\n\n" + json.dumps(_hidden_payload, indent=2)
                    hidden_init_msg_id = generate_message_id()
                    ship_combat_init_hidden_message_data = {
                        "id": hidden_init_msg_id,
                        "parent_id": user_msg_id,
                        "role": "user",
                        "content": hidden_content,
                        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                        "ship_combat_mode": True,
                        "ship_combat_system_init": True,
                        "ship_combat_hidden_init": True,
                    }
                    data["messages"].append(ship_combat_init_hidden_message_data)
                    assistant_parent_id = hidden_init_msg_id
                assistant_msg_data = {
                    "id": assistant_msg_id,
                    "parent_id": assistant_parent_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                    "tokens": tokens_str,
                    "cost": cost_str,
                    "total_tokens": usage['output_tokens'],
                    "total_gpt_tokens": usage['output_tokens'],
                    "model": model_id,
                    "combat_mode": True,
                    "combat_tool_input": combat_json,
                }
                if reasoning_summary:
                    assistant_msg_data["reasoning"] = reasoning_summary
                if service_tier:
                    assistant_msg_data["service_tier"] = service_tier
                if data.get("pipeline_state"):
                    assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])

                # Track combat start_message_id (fires when combat first becomes active)
                active_combat = data.get("pipeline_state", {}).get("combat")
                if active_combat and "start_message_id" not in active_combat:
                    active_combat["start_message_id"] = assistant_msg_id

                _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id
                save_chat(username, request.chat_name, data, request.project)

                if pending_usage is not None:
                    save_daily_usage(username, pending_usage)

                update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens,
                                        parsed.output_tokens, parsed.reasoning_tokens, actual_cost,
                                        model=model_id, context_tokens=0)

                branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                done_data = {
                    'assistant_message': assistant_message,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': stats,
                    'context_start_index': context_start_index,
                    'reasoning': reasoning_summary,
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': len(branch_path_final),
                    'model': model_id,
                    'combat_mode': True,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if service_tier:
                    done_data['service_tier'] = service_tier
                if _original_model:
                    done_data['original_model'] = _original_model
                if _is_combat_marked_complete(combat_json):
                    done_data['combat_complete'] = True
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": len(branch_path_final),
                            "stats": stats,
                            "context_start_index": context_start_index,
                            "pipeline_state": data.get("pipeline_state")
                        }
                    )
                )

                logger.info(f"Combat mode (GPT-5.2): completed for {username}, "
                            f"combat_complete={combat_json.get('combat_complete', False)}")

            elif use_mode_pipeline and use_net_combat_mode:
                # ============================================================
                # GPT-5.2 NET Combat mode: 2-stage mode pipeline (Planning → Narration)
                # ============================================================
                logger.info(f"Net combat mode pipeline (GPT-5.2): starting for {username}")

                if data.get("pipeline_state"):
                    yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

                # Build planning system prompt (net_combat contract + profile + project files)
                nc_planning_system = gs["net_combat_planning_contract"]
                if nc_profile:
                    nc_planning_system += "\n\n" + nc_profile
                if request.project:
                    uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
                    tokens_cache = load_file_tokens_cache(username, request.project)
                    char_sheet_loaded = False
                    for fname in (gs.get("net_combat_files") or []):
                        is_char_sheet = fname.startswith("Character Sheets")
                        if is_char_sheet and char_sheet_loaded:
                            continue
                        if not tokens_cache.get(fname, {}).get("staged", True):
                            continue
                        fpath = os.path.join(uploads_dir, fname)
                        if os.path.exists(fpath):
                            with open(fpath, 'r', encoding='utf-8') as f:
                                nc_planning_system += f"\n\n{'='*60}\nFILE: {fname}\n{'='*60}\n\n" + f.read()
                            if is_char_sheet:
                                char_sheet_loaded = True

                nc_narration_system = gs["net_combat_narration_contract"]

                _PIPELINE_STOP = object()
                def _next_mode_event(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return _PIPELINE_STOP

                _planning_prov = ProviderRegistry.get(PIPELINE_PLANNING_MODEL) or provider
                _narration_prov = ProviderRegistry.get(PIPELINE_NARRATION_MODEL) or provider
                logger.info(
                    f"Net combat mode pipeline: planner={getattr(_planning_prov, 'MODEL_NAME', '?')}, "
                    f"narrator={getattr(_narration_prov, 'MODEL_NAME', '?')}"
                )
                mode_gen = run_mode_pipeline(
                    provider=provider, client=client,
                    planning_provider=_planning_prov,
                    narration_provider=_narration_prov,
                    username=username, project=request.project or "",
                    chat_name=request.chat_name, mode="net_combat",
                    planning_system=nc_planning_system,
                    narration_system=nc_narration_system,
                    mode_messages=nc_history,
                    user_content=user_content,
                    planning_schema=gs["net_combat_planning_schema"],
                    game_state=nc_ps.get("game_state"),
                    character_states=nc_ps.get("character_states"),
                    pipeline_state=nc_ps,
                    tar_stacks=_safe_int(nc_state.get("tar_stacks", 0)) if isinstance(nc_state, dict) else 0,
                    alert_level=_safe_int(nc_state.get("alert_level", 0)) if isinstance(nc_state, dict) else 0,
                    active_programs=nc_state.get("active_programs") if isinstance(nc_state, dict) else None,
                    installed_hardware=nc_state.get("installed_hardware") if isinstance(nc_state, dict) else None,
                    ice_status=nc_state.get("ice_status") if isinstance(nc_state, dict) else None,
                    net_actions_remaining=(_safe_int(nc_state.get("net_actions_remaining"))
                                           if isinstance(nc_state, dict) and nc_state.get("net_actions_remaining") is not None
                                           else None),
                    slide_used_this_turn=bool(nc_state.get("slide_used_this_turn", False))
                                          if isinstance(nc_state, dict) else False,
                    system_map=nc_state.get("system_map") if isinstance(nc_state, dict) else None,
                    current_node=nc_state.get("current_node") if isinstance(nc_state, dict) else None,
                    virus_ledger=nc_ps.get("virus_ledger") if isinstance(nc_ps, dict) else None,
                    stealth_active=bool(nc_state.get("stealth_active", False))
                                   if isinstance(nc_state, dict) else False,
                    quiet_jack_in_used=bool(nc_state.get("quiet_jack_in_used", False))
                                       if isinstance(nc_state, dict) else False,
                    stealth_broken_round=nc_state.get("stealth_broken_round")
                                         if isinstance(nc_state, dict) else None,
                    net_round=_safe_int(nc_state.get("net_round", 1) or 1)
                              if isinstance(nc_state, dict) else 1,
                    backdoor_grace=nc_state.get("backdoor_grace")
                                   if isinstance(nc_state, dict) else None,
                )

                mode_result = None
                while True:
                    pipeline_future = asyncio.ensure_future(
                        asyncio.to_thread(_next_mode_event, mode_gen))
                    while not pipeline_future.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(pipeline_future), timeout=15.0)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                    result = pipeline_future.result()
                    if result is _PIPELINE_STOP:
                        break
                    event_type, event_data = result
                    if event_type == "pipeline_stage":
                        yield f"event: pipeline_stage\ndata: {json.dumps(event_data)}\n\n"
                    elif event_type == "content":
                        accumulated_content += event_data["delta"]
                        yield f"event: content\ndata: {json.dumps(event_data)}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": event_data["delta"]}))
                    elif event_type == "pipeline_done":
                        mode_result = event_data

                if mode_result is None:
                    raise Exception("Net combat mode pipeline completed without result")

                # Build nc_json compatible structure from planning output + resolved actions
                nc_json = mode_result.planning_json
                nc_json["narrative"] = mode_result.final_content
                _strip_and_merge_resolver_ops(nc_json, mode_result.state_ops)

                # Nest hack_state_updates under hack_state key for apply_net_combat_state
                hack_updates = nc_json.get("hack_state_updates", {})
                if hack_updates:
                    nc_json["hack_state"] = hack_updates
                narrative = mode_result.final_content
                nc_reasoning = "\n".join(mode_result.reasoning_summaries) if mode_result.reasoning_summaries else None

                if nc_reasoning:
                    accumulated_thinking = nc_reasoning

                # Apply net_combat state updates
                nc_ps = data.get("pipeline_state", {})
                gs["apply_net_combat_state"](nc_ps, nc_json, game_state=nc_ps.get("game_state"),
                                             resolver_state_ops=mode_result.state_ops)
                _apply_virus_op_state_ops(nc_ps, mode_result.state_ops)
                # Also accept any virus_ops the planner emitted directly in nc_json.
                _planner_v_ops = nc_json.get("virus_ops") if isinstance(nc_json, dict) else None
                if isinstance(_planner_v_ops, list) and _planner_v_ops:
                    from pipeline import apply_virus_ops as _apply_v_ops
                    nc_ps["virus_ledger"] = _apply_v_ops(
                        nc_ps.get("virus_ledger", {"next_id": 1, "active": [], "archived": []}),
                        _planner_v_ops,
                        nc_ps.get("turn_counter", 0),
                        nc_ps.get("hud_state", {}).get("date", "")
                    )
                data["pipeline_state"] = nc_ps

                # Combat clock: advance by game-system-defined round duration
                _combat_secs = gs.get("combat_round_seconds") if gs else None
                _advance_mode_hud_clock(nc_ps, _combat_secs)

                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                )

                # Build usage from mode pipeline aggregate
                usage = mode_result.aggregate_usage
                usage['content'] = narrative
                usage['reasoning'] = nc_reasoning

                assistant_message = narrative
                reasoning_summary = nc_reasoning
                service_tier = mode_result.service_tier_label

                from providers import ParsedResponse
                parsed = ParsedResponse(
                    content=assistant_message,
                    reasoning=reasoning_summary,
                    input_tokens=usage['input_tokens'],
                    cache_read_tokens=usage['cache_read_tokens'],
                    cache_creation_tokens=usage['cache_creation_tokens'],
                    output_tokens=usage['output_tokens'],
                    reasoning_tokens=usage['reasoning_tokens']
                )

                new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                if service_tier:
                    total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                else:
                    total_cost = provider.calculate_cost(parsed)
                tokens_str = provider.format_token_string(parsed)

                actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

                stats = data.get("stats", create_empty_stats())
                stats["total_input_tokens"] += new_input_tokens
                stats["total_cached_tokens"] += parsed.cache_read_tokens
                stats["total_output_tokens"] += parsed.output_tokens
                stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                stats["total_cost"] += actual_cost
                stats["total_prompts"] += 1
                stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                data["stats"] = stats

                assistant_msg_id = generate_message_id()
                assistant_parent_id = user_msg_id
                assistant_msg_data = {
                    "id": assistant_msg_id,
                    "parent_id": assistant_parent_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                    "tokens": tokens_str,
                    "cost": cost_str,
                    "total_tokens": usage['output_tokens'],
                    "total_gpt_tokens": usage['output_tokens'],
                    "model": model_id,
                    "net_combat_mode": True,
                    "net_combat_tool_input": nc_json,
                }
                if reasoning_summary:
                    assistant_msg_data["reasoning"] = reasoning_summary
                if service_tier:
                    assistant_msg_data["service_tier"] = service_tier
                if data.get("pipeline_state"):
                    assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])

                # Track net_combat start_message_id
                active_nc = data.get("pipeline_state", {}).get("net_combat")
                if active_nc and "start_message_id" not in active_nc:
                    active_nc["start_message_id"] = assistant_msg_id
                # Also track combat start_message_id if not set
                active_combat = data.get("pipeline_state", {}).get("combat")
                if active_combat and "start_message_id" not in active_combat:
                    active_combat["start_message_id"] = assistant_msg_id

                _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id
                save_chat(username, request.chat_name, data, request.project)

                if pending_usage is not None:
                    save_daily_usage(username, pending_usage)

                update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens,
                                        parsed.output_tokens, parsed.reasoning_tokens, actual_cost,
                                        model=model_id, context_tokens=0)

                branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                _nc_both_done = _is_net_combat_marked_complete(nc_json, data.get("pipeline_state", {}))
                done_data = {
                    'assistant_message': assistant_message,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': stats,
                    'context_start_index': context_start_index,
                    'reasoning': reasoning_summary,
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': len(branch_path_final),
                    'model': model_id,
                    'net_combat_mode': True,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if service_tier:
                    done_data['service_tier'] = service_tier
                if _original_model:
                    done_data['original_model'] = _original_model
                if _nc_both_done:
                    done_data['net_combat_complete'] = True
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": len(branch_path_final),
                            "stats": stats,
                            "context_start_index": context_start_index,
                            "pipeline_state": data.get("pipeline_state")
                        }
                    )
                )

                logger.info(f"Net combat mode (GPT-5.2): completed for {username}, "
                            f"combat_complete={nc_json.get('combat_complete', False)}, "
                            f"net_complete={nc_json.get('net_complete', False)}")

            elif (use_hack_mode or use_combat_mode or use_net_combat_mode or use_chase_mode) and model_id.startswith("gpt") and not use_mode_pipeline:
                # ============================================================
                # GPT-5.2 mode fallback: non-deterministic game systems use single-shot JSON
                # ============================================================
                if use_hack_mode:
                    _mode_label = "hack"
                    _mode_request_params = hack_gpt_request_params
                elif use_combat_mode:
                    _mode_label = "combat"
                    _mode_request_params = combat_gpt_request_params
                elif use_chase_mode:
                    _mode_label = "chase"
                    _mode_request_params = chase_gpt_request_params
                else:
                    _mode_label = "net_combat"
                    _mode_request_params = net_combat_gpt_request_params

                logger.info(f"{_mode_label} mode (GPT-5.2 fallback): starting for {username}")

                _mode_response = await asyncio.to_thread(
                    provider.send_request_non_streaming,
                    client, _mode_request_params, 60.0
                )

                _mode_content = _mode_response.get('content', '')
                _mode_reasoning = _mode_response.get('reasoning')

                _mode_json_valid = True
                try:
                    _mode_json = json.loads(_mode_content)
                except json.JSONDecodeError:
                    logger.error(f"{_mode_label} mode: failed to parse JSON: {_mode_content[:200]}")
                    _mode_json = {"narrative": _mode_content}
                    _mode_json_valid = False
                if not isinstance(_mode_json, dict):
                    logger.error(f"{_mode_label} mode: parsed JSON must be object, got {type(_mode_json).__name__}")
                    _mode_json = {"narrative": _mode_content}
                    _mode_json_valid = False

                narrative = _mode_json.get("narrative", _mode_content)
                accumulated_content = narrative
                if narrative:
                    yield f"event: content\ndata: {json.dumps({'delta': narrative})}\n\n"
                    await sync_manager.broadcast_to_chat(
                        chat_key,
                        SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": narrative}))

                if _mode_reasoning:
                    accumulated_thinking = _mode_reasoning

                # Apply mode-specific state
                if use_hack_mode:
                    hack_json = _mode_json
                    if _mode_json_valid:
                        _apply_hack_state_compat(
                            gs["apply_hack_state"],
                            hack_state,
                            hack_json,
                            game_state=hack_ps.get("game_state"),
                            pipeline_state=hack_ps,
                            username=username,
                            project=request.project or "",
                        )
                        data["hack_state"] = hack_state
                        yield f"event: hack_state_update\ndata: {json.dumps(hack_state)}\n\n"
                        if hack_json.get("hack_complete"):
                            yield f"event: hack_complete\ndata: {json.dumps({'summary': hack_state.get('narrative_summary', '')})}\n\n"
                elif use_combat_mode:
                    combat_json = _mode_json
                    _cps = data.get("pipeline_state", {})
                    if _mode_json_valid:
                        _apply_combat_state(gs, _cps, combat_json)
                        data["pipeline_state"] = _cps
                        yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                elif use_chase_mode:
                    chase_json = _mode_json
                    _chps = data.get("pipeline_state", {})
                    if _mode_json_valid:
                        gs["apply_chase_state"](_chps, chase_json, game_state=_chps.get("game_state"))
                        _chase_secs = gs.get("chase_round_seconds") if gs else None
                        _advance_mode_hud_clock(_chps, _chase_secs)
                        data["pipeline_state"] = _chps
                        chase_tool_input = chase_json
                        yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                else:
                    nc_json = _mode_json
                    _nps = data.get("pipeline_state", {})
                    if _mode_json_valid:
                        gs["apply_net_combat_state"](_nps, nc_json, game_state=_nps.get("game_state"))
                        data["pipeline_state"] = _nps
                        yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

                usage = {
                    'input_tokens': _mode_response.get('input_tokens', 0),
                    'cache_read_tokens': _mode_response.get('cache_read_tokens', 0),
                    'cache_creation_tokens': _mode_response.get('cache_creation_tokens', 0),
                    'output_tokens': _mode_response.get('output_tokens', 0),
                    'reasoning_tokens': _mode_response.get('reasoning_tokens', 0),
                    'content': narrative,
                    'reasoning': _mode_reasoning,
                    'service_tier': _mode_response.get('service_tier'),
                }

                assistant_message = narrative
                reasoning_summary = _mode_reasoning
                service_tier = _mode_response.get('service_tier')

                from providers import ParsedResponse
                parsed = ParsedResponse(
                    content=assistant_message,
                    reasoning=reasoning_summary,
                    input_tokens=usage['input_tokens'],
                    cache_read_tokens=usage['cache_read_tokens'],
                    cache_creation_tokens=usage['cache_creation_tokens'],
                    output_tokens=usage['output_tokens'],
                    reasoning_tokens=usage['reasoning_tokens']
                )

                new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                if service_tier:
                    total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                else:
                    total_cost = provider.calculate_cost(parsed)
                tokens_str = provider.format_token_string(parsed)

                actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

                stats = data.get("stats", create_empty_stats())
                stats["total_input_tokens"] += new_input_tokens
                stats["total_cached_tokens"] += parsed.cache_read_tokens
                stats["total_output_tokens"] += parsed.output_tokens
                stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                stats["total_cost"] += actual_cost
                stats["total_prompts"] += 1
                stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                data["stats"] = stats

                assistant_msg_id = generate_message_id()
                assistant_parent_id = user_msg_id
                if use_hack_mode:
                    _mode_flag_key = "hack_mode"
                    _mode_tool_key = "hack_tool_input"
                elif use_combat_mode:
                    _mode_flag_key = "combat_mode"
                    _mode_tool_key = "combat_tool_input"
                elif use_chase_mode:
                    _mode_flag_key = "chase_mode"
                    _mode_tool_key = "chase_tool_input"
                else:
                    _mode_flag_key = "net_combat_mode"
                    _mode_tool_key = "net_combat_tool_input"
                assistant_msg_data = {
                    "id": assistant_msg_id,
                    "parent_id": assistant_parent_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                    "tokens": tokens_str,
                    "cost": cost_str,
                    "total_tokens": usage['output_tokens'],
                    "total_gpt_tokens": usage['output_tokens'],
                    "model": model_id,
                    _mode_flag_key: True,
                    _mode_tool_key: _mode_json,
                }
                if reasoning_summary:
                    assistant_msg_data["reasoning"] = reasoning_summary
                if service_tier:
                    assistant_msg_data["service_tier"] = service_tier
                if data.get("pipeline_state"):
                    assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])

                # Chase mode: persist the hidden init message for first-exchange
                # opening (mirrors the Claude path at line 9307).
                if use_chase_mode and chase_started_this_turn:
                    assistant_msg_data["chase_started"] = True
                    if chase_init_hidden_message_data:
                        data["messages"].append(chase_init_hidden_message_data)
                        assistant_msg_data["parent_id"] = chase_init_hidden_message_data["id"]

                _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id
                # Stamp chase start_message_id (mirrors Claude path).
                _active_chase_gpt = data.get("pipeline_state", {}).get("chase")
                if (use_chase_mode and isinstance(_active_chase_gpt, dict)
                    and not _active_chase_gpt.get("start_message_id")):
                    _active_chase_gpt["start_message_id"] = assistant_msg_id
                save_chat(username, request.chat_name, data, request.project)

                if pending_usage is not None:
                    save_daily_usage(username, pending_usage)

                update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens,
                                        parsed.output_tokens, parsed.reasoning_tokens, actual_cost,
                                        model=model_id, context_tokens=0)

                branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                done_data = {
                    'assistant_message': assistant_message,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': stats,
                    'context_start_index': context_start_index,
                    'reasoning': reasoning_summary,
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': len(branch_path_final),
                    'model': model_id,
                    _mode_flag_key: True,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if service_tier:
                    done_data['service_tier'] = service_tier
                if _original_model:
                    done_data['original_model'] = _original_model
                if use_hack_mode and _mode_json.get("hack_complete"):
                    done_data['hack_complete'] = True
                if use_combat_mode and (
                    _mode_json.get("combat_complete")
                    or ("combat" in _mode_json and _mode_json.get("combat") is None)
                ):
                    done_data['combat_complete'] = True
                if use_net_combat_mode:
                    _nc_done = False
                    _nc_state = data.get("pipeline_state", {}).get("net_combat")
                    if isinstance(_nc_state, dict):
                        _nc_done = not _nc_state.get("active", True)
                    elif _mode_json.get("combat_complete") and _mode_json.get("net_complete"):
                        _nc_done = True
                    if _nc_done:
                        done_data['net_combat_complete'] = True
                if use_chase_mode:
                    _chase_done = False
                    _chase_state = data.get("pipeline_state", {}).get("chase")
                    if isinstance(_chase_state, dict):
                        _chase_done = not _chase_state.get("active", True)
                    elif _mode_json.get("chase_complete"):
                        _chase_done = True
                    if _chase_done:
                        done_data['chase_complete'] = True
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": len(branch_path_final),
                            "stats": stats,
                            "context_start_index": context_start_index,
                            "pipeline_state": data.get("pipeline_state")
                        }
                    )
                )

                logger.info(f"{_mode_label} mode (GPT-5.2 fallback): completed for {username}")

            elif use_ship_combat_mode and model_id.startswith("gpt"):
                # ============================================================
                # GPT-5.2 Ship combat mode: non-streaming JSON call
                # ============================================================
                logger.info(f"Ship combat mode (GPT-5.2): starting for {username}")

                _sc_result = {}
                async for sse_event in _run_ship_combat_gpt_exchange(
                    parent_msg_id=user_msg_id,
                    is_first_exchange=ship_combat_started_this_turn,
                    opening_narration_hint=ship_combat_opening_narration_hint,
                    result_out=_sc_result,
                ):
                    yield sse_event

                accumulated_content = _sc_result.get("narrative", "")
                if _sc_result.get("reasoning"):
                    accumulated_thinking = _sc_result["reasoning"]
                ship_combat_init_hidden_message_data = _sc_result.get("hidden_init_data")
                ship_combat_opening_narration_hint = _sc_result.get("opening_narration_hint", ship_combat_opening_narration_hint)

                assistant_msg_id = _sc_result["assistant_msg_id"]
                assistant_msg_data = _sc_result["assistant_msg_data"]
                branch_path_final = _sc_result["branch_path_final"]
                stats = _sc_result["stats"]
                tokens_str = _sc_result["tokens_str"]
                cost_str = _sc_result["cost_str"]
                service_tier = _sc_result.get("service_tier")
                ship_combat_json = _sc_result.get("ship_combat_json", {})

                done_data = {
                    'assistant_message': accumulated_content,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': stats,
                    'context_start_index': context_start_index,
                    'reasoning': _sc_result.get("reasoning"),
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': len(branch_path_final),
                    'model': model_id,
                    'ship_combat_mode': True,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if ship_combat_started_this_turn:
                    done_data['ship_combat_started'] = True
                    done_data['ship_combat_system_init'] = True
                    if ship_combat_init_hidden_message_data:
                        done_data['ship_combat_init_message'] = copy.deepcopy(ship_combat_init_hidden_message_data)
                    if ship_combat_opening_narration_hint:
                        done_data['ship_combat_opening_narration'] = ship_combat_opening_narration_hint
                        done_data['ship_combat_opening_embedded'] = _ship_opening_embedded(
                            ship_combat_opening_narration_hint, accumulated_content
                        )
                if service_tier:
                    done_data['service_tier'] = service_tier
                if _original_model:
                    done_data['original_model'] = _original_model
                if (
                    isinstance(ship_combat_json, dict)
                    and ("ship_combat" in ship_combat_json)
                    and (ship_combat_json.get("ship_combat_complete") or ship_combat_json.get("ship_combat") is None)
                ):
                    done_data['ship_combat_complete'] = True
                yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "ship_combat_init_message": copy.deepcopy(ship_combat_init_hidden_message_data) if ship_combat_init_hidden_message_data else None,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": len(branch_path_final),
                            "stats": stats,
                            "context_start_index": context_start_index,
                            "pipeline_state": data.get("pipeline_state")
                        }
                    )
                )

                logger.info(f"Ship combat mode (GPT-5.2): completed for {username}")

            elif use_pipeline:
                # ============================================================
                # Multi-agent pipeline path (Events → Mechanics → Narration)
                # ============================================================
                logger.info(f"Pipeline: starting for user {username}, project {request.project}")

                # Build per-agent instructions and files for the pipeline
                agent_instructions = {}
                agent_files = {}
                for agent_name in PIPELINE_AGENT_NAMES:
                    agent_instructions[agent_name] = get_instructions_for_agent(username, request.project, agent_name)
                    agent_files[agent_name] = load_project_files_for_agent(username, request.project, agent_name)

                # Diagnostic: warn if pipeline loaded no files but files exist on disk
                uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads")
                if os.path.exists(uploads_dir):
                    all_files = [f for f in os.listdir(uploads_dir) if os.path.splitext(f)[1].lower() in ALLOWED_FILE_EXTENSIONS]
                    any_agent_has_files = any(agent_files[a].strip() for a in PIPELINE_AGENT_NAMES)
                    if all_files and not any_agent_has_files:
                        tokens_cache = load_file_tokens_cache(username, request.project)
                        cache_snapshot = {f: {k: v for k, v in tokens_cache.get(f, {}).items() if k in ("staged", "agents")} for f in all_files}
                        logger.warning(f"Pipeline: NO files loaded for ANY agent despite {len(all_files)} files on disk. "
                                       f"Project model: {load_project_metadata(username, request.project).get('model')}. "
                                       f"Cache state: {cache_snapshot}")
                        # DEFENSIVE FALLBACK: load all staged files for all agents
                        all_staged = load_project_files(username, request.project)
                        if all_staged.strip():
                            for agent_name in PIPELINE_AGENT_NAMES:
                                agent_files[agent_name] = all_staged
                            logger.info(f"Pipeline: defensive fallback loaded {len(all_staged)} chars for all agents")

                pipeline_state_prev = data.get("pipeline_state")
                pipeline_trim_anchor_id = data.get("_trim_anchor_id")

                # Load conversion doc for feature injection (transient — injected into a
                # copy of game_state so we never mutate data["pipeline_state"])
                if pipeline_state_prev and request.project:
                    conv_path = os.path.join(get_project_dir(username, request.project), "uploads", "Core Conversion.md")
                    if os.path.exists(conv_path):
                        with open(conv_path, 'r', encoding='utf-8') as f:
                            orig_gs = pipeline_state_prev.get("game_state", {})
                            pipeline_state_prev = {**pipeline_state_prev, "game_state": {**orig_gs, "_conversion_doc": f.read()}}

                pipeline_result = None
                pipeline_current_stage = "starting"
                # Snapshot old voice values for voice_update notifications
                _old_cs = pipeline_state_prev.get("character_states", {}) if pipeline_state_prev else {}
                pipeline_old_voice_snapshot = {
                    name: entry.get("data", entry).get("voice")
                    for name, entry in _old_cs.items()
                }

                # Use a sentinel to avoid StopIteration propagation in async generator (PEP 479)
                _PIPELINE_STOP = object()
                def _next_pipeline_event(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return _PIPELINE_STOP

                # Run pipeline in thread pool to avoid blocking the event loop
                # during synchronous API calls (Events/Mechanics stages)
                doc_stems = extract_project_file_stems(agent_files.get("narration", ""))
                pipeline_uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads") if request.project else None
                pipeline_name_dice = generate_name_dice(pipeline_uploads_dir or "")
                _planning_prov = ProviderRegistry.get(PIPELINE_PLANNING_MODEL) or provider
                _narration_prov = ProviderRegistry.get(PIPELINE_NARRATION_MODEL) or provider
                logger.info(
                    f"Pipeline: planner={getattr(_planning_prov, 'MODEL_NAME', '?')}, "
                    f"narrator={getattr(_narration_prov, 'MODEL_NAME', '?')}"
                )
                pipeline_gen = run_pipeline(
                    provider=provider,
                    client=client,
                    username=username,
                    project=request.project,
                    chat_name=request.chat_name,
                    branch_path=branch_path,
                    agent_instructions=agent_instructions,
                    agent_files=agent_files,
                    pipeline_state=pipeline_state_prev,
                    game_system=gs["id"],
                    trim_anchor_id=pipeline_trim_anchor_id,
                    doc_file_stems=doc_stems,
                    name_dice=pipeline_name_dice,
                    uploads_dir=pipeline_uploads_dir,
                    planning_provider=_planning_prov,
                    narration_provider=_narration_prov,
                )

                client_disconnected = False

                while True:
                    # Run pipeline step in thread, sending SSE keepalive comments
                    # every 15s to prevent proxy/browser timeouts during long API calls
                    pipeline_future = asyncio.ensure_future(
                        asyncio.to_thread(_next_pipeline_event, pipeline_gen)
                    )
                    while not pipeline_future.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(pipeline_future), timeout=15.0)
                        except asyncio.TimeoutError:
                            if not client_disconnected:
                                yield ": keepalive\n\n"
                    result = pipeline_future.result()

                    if result is _PIPELINE_STOP:
                        break
                    event_type, event_data = result

                    if event_type == "pipeline_stage":
                        pipeline_current_stage = f"{event_data.get('stage', '?')}:{event_data.get('status', '?')}"
                        if not client_disconnected:
                            yield f"event: pipeline_stage\ndata: {json.dumps(event_data)}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.PIPELINE_STAGE, data=event_data)
                        )

                    elif event_type == "content":
                        # Check for client disconnect (soft: tab switch/background)
                        if not client_disconnected and await http_request.is_disconnected():
                            client_disconnected = True
                            logger.warning(f"Pipeline: client disconnected during streaming for user {username}, continuing to consume stream")
                        accumulated_content += event_data["delta"]
                        if not client_disconnected:
                            yield f"event: content\ndata: {json.dumps(event_data)}\n\n"
                        # Broadcast content delta to other clients
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(
                                type=SyncEventType.STREAM_CONTENT,
                                data={"delta": event_data["delta"]}
                            )
                        )

                    elif event_type == "pipeline_done":
                        pipeline_result = event_data

                if pipeline_result is None:
                    raise Exception(f"Pipeline completed without producing a result (last stage: {pipeline_current_stage})")

                # Process pipeline result (similar to single-agent done handler)
                assistant_message = pipeline_result.final_content
                reasoning_summary = "\n".join(pipeline_result.reasoning_summaries) if pipeline_result.reasoning_summaries else None
                usage = pipeline_result.aggregate_usage
                service_tier = pipeline_result.service_tier_label

                # Save trim anchor for sawtooth context trimming
                data["_trim_anchor_id"] = pipeline_result.trim_anchor_id

                # Override context_start_index from trim anchor
                if pipeline_result.trim_anchor_id:
                    context_start_index = 1
                    for idx, msg in enumerate(branch_path):
                        if msg.get("id") == pipeline_result.trim_anchor_id:
                            context_start_index = idx
                            break
                else:
                    context_start_index = 1  # No trim anchor — include all history

                # Save pipeline state for next turn
                if pipeline_result.pipeline_state is not None:
                    # Strip transient _conversion_doc before persisting
                    gs_state = pipeline_result.pipeline_state.get("game_state")
                    if gs_state:
                        gs_state.pop("_conversion_doc", None)
                    data["pipeline_state"] = pipeline_result.pipeline_state
                    # Send state_update SSE event for right panel
                    yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                    await sync_manager.broadcast_to_chat(
                        chat_key,
                        SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                    )

                # Emit state change notifications (pipeline path)
                # Use enriched_events (post-apply_game_state) which has backend-computed
                # new_total and tier_transition.  Falls back to re-parsing raw JSON for
                # backward compat (enriched_events is already scene-scope filtered).
                notif_events = pipeline_result.enriched_events
                if not notif_events and pipeline_result.events_json:
                    try:
                        notif_events = json.loads(pipeline_result.events_json)
                    except (json.JSONDecodeError, TypeError):
                        notif_events = None
                if notif_events:
                    ps_scene = (pipeline_result.pipeline_state or {}).get("scene_state", {})
                    notif_npcs = set(ps_scene.get("npcs_present", []))
                    notifs = extract_state_notifications(
                        notif_events, npcs_present=notif_npcs,
                        old_character_states=pipeline_old_voice_snapshot)
                    if notifs:
                        yield f"event: state_notifications\ndata: {json.dumps(notifs)}\n\n"
                        await sync_manager.broadcast_to_chat(chat_key,
                            SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": notifs}))

                # Emit backend-generated notifications from game system (pipeline path)
                ps = pipeline_result.pipeline_state or {}
                if "game_state" in ps:
                    game_notifs = ps["game_state"].pop("_pending_notifications", [])
                    if game_notifs:
                        yield f"event: state_notifications\ndata: {json.dumps(game_notifs)}\n\n"
                        await sync_manager.broadcast_to_chat(chat_key,
                            SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": game_notifs}))

                # Emit time-override notifications (pipeline path)
                time_notifs = ps.pop("_pending_time_notifications", [])
                if time_notifs:
                    yield f"event: state_notifications\ndata: {json.dumps(time_notifs)}\n\n"
                    await sync_manager.broadcast_to_chat(chat_key,
                        SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": time_notifs}))

                # Get cross-model providers for token counting
                gpt_provider = get_gpt_provider()
                claude_provider = get_claude_provider()
                claude_api_key = get_api_key(username, "anthropic")

                # Count tokens on final output only (for model switching)
                assistant_gpt_tokens = gpt_provider.count_tokens(assistant_message)
                if claude_api_key:
                    assistant_claude_tokens = claude_provider.count_tokens_api(assistant_message, claude_api_key)
                else:
                    assistant_claude_tokens = None

                # Count user message tokens for cross-model
                if request.attached_files:
                    file_wrappers = [f"====FILE: {f.filename}====\n{f.content}\n====END FILE====" for f in request.attached_files]
                    user_content_for_counting = "\n\n".join(file_wrappers) + "\n\n" + request.message
                else:
                    user_content_for_counting = request.message
                user_gpt_tokens = gpt_provider.count_tokens(user_content_for_counting)
                if claude_api_key:
                    user_claude_tokens = claude_provider.count_tokens_api(user_content_for_counting, claude_api_key)
                else:
                    user_claude_tokens = None

                for msg in data["messages"]:
                    if msg.get("id") == user_msg_id:
                        msg["total_gpt_tokens"] = user_gpt_tokens
                        if user_claude_tokens is not None:
                            msg["total_claude_tokens"] = user_claude_tokens
                        msg["total_tokens"] = user_gpt_tokens
                        break

                # Build aggregate ParsedResponse for cost/stats
                from providers import ParsedResponse
                parsed = ParsedResponse(
                    content=assistant_message,
                    reasoning=reasoning_summary,
                    input_tokens=usage['input_tokens'],
                    cache_read_tokens=usage['cache_read_tokens'],
                    cache_creation_tokens=usage['cache_creation_tokens'],
                    output_tokens=usage['output_tokens'],
                    reasoning_tokens=usage['reasoning_tokens']
                )

                new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                total_cost = pipeline_result.aggregate_cost
                tokens_str = provider.format_token_string(parsed)

                # Apply free tokens
                actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)

                # Update stats (one prompt increment per pipeline turn)
                stats = data.get("stats", create_empty_stats())
                stats["total_input_tokens"] += new_input_tokens
                stats["total_cached_tokens"] += parsed.cache_read_tokens
                stats["total_output_tokens"] += parsed.output_tokens
                stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                stats["total_cost"] += actual_cost
                stats["total_prompts"] += 1
                stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                data["stats"] = stats

                # Add assistant message with pipeline stage data
                assistant_msg_id = generate_message_id()
                assistant_msg_data = {
                    "id": assistant_msg_id,
                    "parent_id": user_msg_id,
                    "role": "assistant",
                    "content": assistant_message,
                    "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                    "tokens": tokens_str,
                    "cost": cost_str,
                    "total_tokens": assistant_gpt_tokens,
                    "total_gpt_tokens": assistant_gpt_tokens,
                    "model": model_id,
                    "service_tier": service_tier
                }
                if assistant_claude_tokens is not None:
                    assistant_msg_data["total_claude_tokens"] = assistant_claude_tokens
                if reasoning_summary:
                    assistant_msg_data["reasoning"] = reasoning_summary
                if pipeline_result.events_json:
                    assistant_msg_data["events_stage"] = pipeline_result.events_json
                if pipeline_result.mechanics_json:
                    assistant_msg_data["mechanics_stage"] = pipeline_result.mechanics_json
                if pipeline_result.injected_state is not None:
                    assistant_msg_data["pipeline_state_injected"] = pipeline_result.injected_state
                if pipeline_result.pipeline_state is not None:
                    assistant_msg_data["pipeline_state_after"] = copy.deepcopy(data["pipeline_state"])
                if pipeline_result.stage_usage is not None:
                    assistant_msg_data["pipeline_stage_usage"] = pipeline_result.stage_usage

                _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id

                # Track combat start_message_id when combat begins via pipeline
                _pipeline_combat = data.get("pipeline_state", {}).get("combat")
                if _pipeline_combat and "start_message_id" not in _pipeline_combat:
                    _pipeline_combat["start_message_id"] = assistant_msg_id
                _pipeline_ship_combat = data.get("pipeline_state", {}).get("ship_combat")
                if _pipeline_ship_combat and "start_message_id" not in _pipeline_ship_combat:
                    _pipeline_ship_combat["start_message_id"] = assistant_msg_id

                # Check for hack_trigger from pipeline events
                if pipeline_result.events_json and gs and gs.get("init_hack_state"):
                    try:
                        _evts = json.loads(pipeline_result.events_json) if isinstance(pipeline_result.events_json, str) else pipeline_result.events_json
                        if _evts.get("hack_trigger"):
                            _cs = data.get("pipeline_state", {}).get("character_states", {})
                            data["hack_state"] = _init_hack_from_trigger(gs, _evts["hack_trigger"], _cs, pipeline_state=data.get("pipeline_state", {}))
                            data["hack_state"]["start_message_id"] = assistant_msg_id
                            yield f"event: hack_mode_start\ndata: {json.dumps(data['hack_state'])}\n\n"
                            logger.info(f"Pipeline hack trigger: {_evts['hack_trigger'].get('tier')} on "
                                        f"{_evts['hack_trigger'].get('target_system')} SR{_evts['hack_trigger'].get('sr')} for {username}")
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Check for chase_trigger from pipeline events
                if pipeline_result.events_json and gs and gs.get("init_chase_state"):
                    try:
                        _evts_chase = json.loads(pipeline_result.events_json) if isinstance(pipeline_result.events_json, str) else pipeline_result.events_json
                        if _evts_chase.get("chase_trigger"):
                            ct = _evts_chase["chase_trigger"]
                            _ps = data.get("pipeline_state", {})
                            _ct_vehicles = ct.get("vehicles") if isinstance(ct.get("vehicles"), dict) else {}
                            _ct_pursuers = set(ct.get("pursuer_vehicles") or [])
                            _seeded_vehicles = {}
                            for vname, v in _ct_vehicles.items():
                                if not isinstance(v, dict):
                                    continue
                                _seeded_vehicles[str(vname)] = {
                                    "operator": v.get("operator", ""),
                                    "occupants": list(v.get("occupants") or []),
                                    "square": int(v.get("starting_square", 0) or 0),
                                    "combat_speed_move": int(v.get("combat_speed_move", 20) or 20),
                                    "sdp_max": int(v.get("sdp_max", 20) or 20),
                                    "sdp_current": int(v.get("sdp_current", v.get("sdp_max", 20)) or 20),
                                    "sp": int(v.get("sp", 0) or 0),
                                    "type": str(v.get("type") or "land"),
                                    "is_pursuer": vname in _ct_pursuers if _ct_pursuers else bool(v.get("is_pursuer", False)),
                                    "notes": v.get("notes", ""),
                                }
                            _seeded_chase = gs["init_chase_state"](
                                grid_length=int(ct.get("grid_length", 8) or 8),
                                vehicles=_seeded_vehicles,
                                started_from="pipeline_events",
                                context=ct.get("context"),
                                start_message_id=assistant_msg_id,
                            )
                            if ct.get("route"):
                                _seeded_chase["route"] = ct["route"]
                            _ps["chase"] = _seeded_chase
                            # Stamp HUD overlay immediately so the next
                            # exchange's HUD reflects the chase route, not
                            # the stale pre-chase location.
                            from game_systems.cpred_chase import stamp_chase_hud_overlay
                            stamp_chase_hud_overlay(_ps)
                            yield f"event: chase_mode_start\ndata: {json.dumps(_seeded_chase)}\n\n"
                            logger.info(
                                f"Pipeline chase trigger: {len(_seeded_vehicles)} vehicles, "
                                f"{len(_ct_pursuers) or sum(1 for v in _seeded_vehicles.values() if v['is_pursuer'])} pursuers "
                                f"for {username}"
                            )
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Check for ship_combat_trigger from pipeline events
                if pipeline_result.events_json and gs and gs.get("ship_combat_contract"):
                    try:
                        _evts = json.loads(pipeline_result.events_json) if isinstance(pipeline_result.events_json, str) else pipeline_result.events_json
                        if _evts.get("ship_combat_trigger"):
                            sct = _evts["ship_combat_trigger"]
                            _ps = data.get("pipeline_state", {})
                            _ps["ship_combat"] = {
                                "round": 1,
                                "initiative_order": [],
                                "current_ship": None,
                                "current_role": None,
                                "environment": sct.get("environment", "Open Space"),
                                "handoff_summary": sct.get("handoff_summary"),
                                "opening_narration": sct.get("opening_narration"),
                                "encounter_type": sct.get("encounter_type"),
                                "objective": sct.get("objective"),
                                "positioning": sct.get("positioning"),
                                "immediate_complications": sct.get("immediate_complications", []),
                                "enemy_ships": sct.get("enemy_ships", []),
                                "bootstrap_done": False,
                                "ship_combat_handoff_source": "trigger" if sct.get("handoff_summary") else None,
                                "bootstrap_messages": [],
                                "start_message_id": assistant_msg_id,
                            }
                            data["pipeline_state"] = _ps
                            ship_combat_triggered_this_turn = True
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            logger.info(f"Pipeline ship_combat trigger: {sct.get('environment')} "
                                        f"enemies={sct.get('enemy_ships', [])} for {username}")
                    except (json.JSONDecodeError, TypeError):
                        pass

                # Update system message tokens
                system_msg_ref = branch_path[0]
                system_content_val = system_msg_ref.get("content", "")
                if system_msg_ref.get("total_gpt_tokens") is None:
                    system_msg_ref["total_gpt_tokens"] = gpt_provider.count_tokens(system_content_val)
                if system_msg_ref.get("total_claude_tokens") is None and claude_api_key:
                    system_msg_ref["total_claude_tokens"] = claude_provider.count_tokens_api(system_content_val, claude_api_key)
                system_msg_ref["total_tokens"] = system_msg_ref.get("total_gpt_tokens")

                save_chat(username, request.chat_name, data, request.project)
                logger.info(f"Pipeline: saved chat for user {username}")

                try:
                    debug_chat_path = get_chat_path(username, request.chat_name, request.project)
                    generate_debug_transcript(data, debug_chat_path, request.chat_name)
                    generate_plain_transcript(data, debug_chat_path, request.chat_name)
                except Exception as e:
                    logger.warning(f"Pipeline: failed to generate debug transcript: {e}")

                # Commit deferred updates
                if pending_usage is not None:
                    save_daily_usage(username, pending_usage)

                context_tokens = (user_gpt_tokens or 0) + (assistant_gpt_tokens or 0)
                update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens, parsed.output_tokens, parsed.reasoning_tokens, actual_cost, model=model_id, context_tokens=context_tokens)

                # Calculate branch info
                branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                branch_total_messages = len(branch_path_final)

                # Calculate model-specific stats
                response_stats = stats.copy()
                gpt_prompts = 0
                sonnet_prompts = 0
                gpt_context_tokens = 0
                sonnet_context_tokens = 0
                all_chat_messages = data.get("messages", [])
                messages_by_id = {m.get("id"): m for m in all_chat_messages if m.get("id")}
                for msg in all_chat_messages:
                    if msg.get("role") == "assistant":
                        msg_model = msg.get("model", "")
                        is_sonnet = msg_model.startswith("claude")
                        p_id = msg.get("parent_id")
                        if is_sonnet:
                            sonnet_prompts += 1
                            a_tokens = msg.get("total_claude_tokens") or msg.get("total_tokens", 0) or 0
                            u_tokens = 0
                            if p_id and p_id in messages_by_id:
                                parent = messages_by_id[p_id]
                                u_tokens = parent.get("total_claude_tokens") or parent.get("total_tokens", 0) or 0
                            sonnet_context_tokens += u_tokens + a_tokens
                        else:
                            gpt_prompts += 1
                            a_tokens = msg.get("total_gpt_tokens") or msg.get("total_tokens", 0) or 0
                            u_tokens = 0
                            if p_id and p_id in messages_by_id:
                                parent = messages_by_id[p_id]
                                u_tokens = parent.get("total_gpt_tokens") or parent.get("total_tokens", 0) or 0
                            gpt_context_tokens += u_tokens + a_tokens
                response_stats["gpt_prompts"] = gpt_prompts
                response_stats["sonnet_prompts"] = sonnet_prompts
                response_stats["avg_gpt_context_growth"] = gpt_context_tokens / gpt_prompts if gpt_prompts > 0 else 0
                response_stats["avg_sonnet_context_growth"] = sonnet_context_tokens / sonnet_prompts if sonnet_prompts > 0 else 0

                # Send done event
                done_data = {
                    'assistant_message': assistant_message,
                    'tokens': tokens_str,
                    'cost': cost_str,
                    'stats': response_stats,
                    'context_start_index': context_start_index,
                    'reasoning': reasoning_summary,
                    'user_message_id': user_msg_id,
                    'assistant_message_id': assistant_msg_id,
                    'current_leaf_id': assistant_msg_id,
                    'total_messages': branch_total_messages,
                    'model': model_id,
                    'service_tier': service_tier,
                    'pipeline_stages': pipeline_result.stages_run,
                    'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                }
                if not client_disconnected:
                    yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                # Broadcast stream done to other clients
                pipeline_stream_done_data = {
                    "assistant_message": assistant_msg_data,
                    "user_message_id": user_msg_id,
                    "assistant_message_id": assistant_msg_id,
                    "current_leaf_id": assistant_msg_id,
                    "total_messages": branch_total_messages,
                    "stats": response_stats,
                    "context_start_index": context_start_index
                }
                if data.get("pipeline_state"):
                    pipeline_stream_done_data["pipeline_state"] = data["pipeline_state"]
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.STREAM_DONE, data=pipeline_stream_done_data)
                )

                # Chain ship combat init within same SSE stream if triggered this turn
                if ship_combat_triggered_this_turn and gs and gs.get("ship_combat_contract") and not client_disconnected:
                    logger.info(f"Pipeline: chaining ship combat init for {username}")
                    try:
                        yield f"event: ship_combat_auto_init\ndata: {json.dumps({'parent_id': assistant_msg_id})}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.SHIP_COMBAT_AUTO_INIT, data={"parent_id": assistant_msg_id})
                        )

                        _chain_result = {}
                        _chain_opening = ((data.get("pipeline_state") or {}).get("ship_combat") or {}).get("opening_narration")
                        async for sse_event in _run_ship_combat_gpt_exchange(
                            parent_msg_id=assistant_msg_id,
                            is_first_exchange=True,
                            opening_narration_hint=_chain_opening,
                            result_out=_chain_result,
                        ):
                            yield sse_event

                        _chain_assistant_msg_id = _chain_result["assistant_msg_id"]
                        _chain_assistant_msg_data = _chain_result["assistant_msg_data"]
                        _chain_hidden_init = _chain_result.get("hidden_init_data")
                        _chain_branch_path = _chain_result["branch_path_final"]
                        _chain_opening_hint = _chain_result.get("opening_narration_hint")
                        _chain_narrative = _chain_result.get("narrative", "")
                        _chain_sc_json = _chain_result.get("ship_combat_json", {})

                        ship_combat_done_data = {
                            'assistant_message': _chain_narrative,
                            'tokens': _chain_result["tokens_str"],
                            'cost': _chain_result["cost_str"],
                            'stats': _chain_result["stats"],
                            'context_start_index': context_start_index,
                            'reasoning': _chain_result.get("reasoning"),
                            'user_message_id': user_msg_id,
                            'assistant_message_id': _chain_assistant_msg_id,
                            'current_leaf_id': _chain_assistant_msg_id,
                            'total_messages': len(_chain_branch_path),
                            'model': model_id,
                            'ship_combat_mode': True,
                            'ship_combat_started': True,
                            'ship_combat_system_init': True,
                            'hud_state': _hud_for_done_event(_chain_assistant_msg_data, data.get('pipeline_state')),
                        }
                        if _chain_hidden_init:
                            ship_combat_done_data['ship_combat_init_message'] = copy.deepcopy(_chain_hidden_init)
                        if _chain_opening_hint:
                            ship_combat_done_data['ship_combat_opening_narration'] = _chain_opening_hint
                            ship_combat_done_data['ship_combat_opening_embedded'] = _ship_opening_embedded(
                                _chain_opening_hint, _chain_narrative
                            )
                        if _chain_result.get("service_tier"):
                            ship_combat_done_data['service_tier'] = _chain_result["service_tier"]
                        if _original_model:
                            ship_combat_done_data['original_model'] = _original_model
                        if (
                            isinstance(_chain_sc_json, dict)
                            and ("ship_combat" in _chain_sc_json)
                            and (_chain_sc_json.get("ship_combat_complete") or _chain_sc_json.get("ship_combat") is None)
                        ):
                            ship_combat_done_data['ship_combat_complete'] = True

                        yield f"event: ship_combat_done\ndata: {json.dumps(ship_combat_done_data)}\n\n"

                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(
                                type=SyncEventType.STREAM_DONE,
                                data={
                                    "ship_combat_auto_init": True,
                                    "assistant_message": _chain_assistant_msg_data,
                                    "ship_combat_init_message": copy.deepcopy(_chain_hidden_init) if _chain_hidden_init else None,
                                    "user_message_id": user_msg_id,
                                    "assistant_message_id": _chain_assistant_msg_id,
                                    "current_leaf_id": _chain_assistant_msg_id,
                                    "total_messages": len(_chain_branch_path),
                                    "stats": _chain_result["stats"],
                                    "context_start_index": context_start_index,
                                    "pipeline_state": data.get("pipeline_state")
                                }
                            )
                        )
                        logger.info(f"Pipeline: ship combat chained init completed for {username}")
                    except Exception as chain_err:
                        logger.error(f"Pipeline: ship combat chaining failed for {username}: {chain_err}")
                        _chain_err_detail = f"Ship combat init failed: {chain_err}"
                        yield f"event: ship_combat_error\ndata: {json.dumps({'detail': _chain_err_detail})}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.STREAM_ERROR, data={"detail": _chain_err_detail, "ship_combat_auto_init": True})
                        )

                logger.info(f"Pipeline: completed for user {username}, stages: {pipeline_result.stages_run}")

            else:
                # ============================================================
                # Standard single-agent path (existing behavior)
                # ============================================================
                event_count = 0
                client_disconnected = False
                resolve_mechanics_extra_usage = None  # Track usage from resolve_mechanics round-trip (cpred)
                if model_id.startswith("gpt"):
                    stream_iter = provider.send_request_stream_with_fallback(client, request_params)
                else:
                    stream_iter = provider.send_request_stream(client, request_params)
                for stream_event in stream_iter:
                    event_count += 1
                    # Check for client disconnect (soft: tab switch/background)
                    if not client_disconnected and stream_event.event_type != 'done' and await http_request.is_disconnected():
                        client_disconnected = True
                        logger.warning(f"Client disconnected after {event_count} events for user {username}, continuing to consume stream")

                    if stream_event.event_type == 'content_delta':
                        # With forced tool_use, all text deltas are narrative (tool_use deltas are partial_json, not text)
                        accumulated_content += stream_event.content
                        if not client_disconnected:
                            yield f"event: content\ndata: {json.dumps({'delta': stream_event.content})}\n\n"
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(
                                type=SyncEventType.STREAM_CONTENT,
                                data={"delta": stream_event.content}
                            )
                        )

                    elif stream_event.event_type == 'thinking_delta':
                        accumulated_thinking += stream_event.content
                        if not client_disconnected:
                            yield f"event: thinking\ndata: {json.dumps({'delta': stream_event.content})}\n\n"
                        # Broadcast thinking delta to other clients
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(
                                type=SyncEventType.STREAM_THINKING,
                                data={"delta": stream_event.content}
                            )
                        )

                    elif stream_event.event_type == 'done':
                        logger.info(f"Stream done event received for user {username}")
                        usage = stream_event.usage

                        # Incremental resolve_mechanics loop (cpred deterministic resolution).
                        # Opus calls resolve_mechanics once per combatant turn; we loop
                        # until the model stops calling it and calls report_*_state instead.
                        accumulated_rm_state_ops = []
                        resolve_mechanics_ran = False
                        if gs and gs.get("id") == "cpred":
                            from game_systems.cpred_mechanics import resolve_actions as _rm_resolve_actions

                            def _find_rm_call(_usage):
                                """Extract resolve_mechanics tool_use from usage."""
                                for _tc in (_usage.get('tool_uses') or []):
                                    if _tc.get("name") == "resolve_mechanics":
                                        return _tc
                                if _usage.get('tool_use_name') == 'resolve_mechanics':
                                    return {"name": _usage['tool_use_name'], "input": _usage.get('tool_use_input'), "id": _usage.get('tool_use_id')}
                                return None

                            _rm_request_params = copy.deepcopy(request_params)
                            _rm_iteration = 0
                            _rm_max_iterations = 64
                            _rm_running_hp_map = None
                            _rm_running_vehicle_map = None

                            while True:
                                if _rm_iteration >= _rm_max_iterations:
                                    logger.warning("resolve_mechanics loop hit max iterations (%d) for %s", _rm_max_iterations, username)
                                    break
                                _rm_call = _find_rm_call(usage)
                                if _rm_call is None:
                                    break  # No more resolve_mechanics — fall through to report_*_state
                                resolve_mechanics_ran = True

                                _rm_input = _rm_call.get("input") or {}
                                if not isinstance(_rm_input, dict):
                                    logger.warning("resolve_mechanics tool input must be object, got %s", type(_rm_input).__name__)
                                    _rm_input = {}
                                # Source resolver context from active mode state to avoid
                                # pulling empty stateful_pipeline_state in hack/net-combat modes.
                                _rm_tar = 0
                                _rm_alert = 0
                                _rm_active_progs = None
                                _rm_hw = None
                                _rm_ice = None
                                _rm_gs = {}
                                _rm_active_state = None
                                if use_hack_mode:
                                    _rm_ps = data.get("pipeline_state", {})
                                    _rm_gs = _rm_ps.get("game_state", {}) if isinstance(_rm_ps, dict) else {}
                                    if isinstance(hack_state, dict) and hack_state.get("active"):
                                        _rm_active_state = hack_state
                                elif use_net_combat_mode:
                                    _rm_ps = data.get("pipeline_state", {})
                                    _rm_gs = _rm_ps.get("game_state", {}) if isinstance(_rm_ps, dict) else {}
                                    _rm_nc_mode = _rm_ps.get("net_combat") if isinstance(_rm_ps, dict) else None
                                    if isinstance(_rm_nc_mode, dict) and _rm_nc_mode.get("active"):
                                        _rm_active_state = _rm_nc_mode
                                    elif isinstance(nc_state, dict) and nc_state.get("active"):
                                        _rm_active_state = nc_state
                                else:
                                    _rm_sps = stateful_pipeline_state or {}
                                    _rm_gs = _rm_sps.get("game_state", {})
                                    _rm_hs = _rm_sps.get("hack_state")
                                    _rm_nc = _rm_sps.get("net_combat")
                                    if isinstance(_rm_hs, dict) and _rm_hs.get("active"):
                                        _rm_active_state = _rm_hs
                                    elif isinstance(_rm_nc, dict) and _rm_nc.get("active"):
                                        _rm_active_state = _rm_nc
                                _rm_net_actions_remaining = None
                                _rm_active_boosts = None
                                _rm_cycles_remaining = None
                                _rm_slide_used = False
                                _rm_system_map = None
                                _rm_current_node = None
                                _rm_stealth_active = False
                                _rm_quiet_jack_in_used = False
                                _rm_stealth_broken_round = None
                                _rm_net_round = 1
                                _rm_pathfinder_used = False
                                _rm_revealed_nodes = []
                                if isinstance(_rm_active_state, dict):
                                    _rm_tar = _safe_int(_rm_active_state.get("tar_stacks", 0))
                                    _rm_alert = _safe_int(_rm_active_state.get("alert_level", 0))
                                    _rm_active_progs = _rm_active_state.get("active_programs")
                                    _rm_hw = _rm_active_state.get("installed_hardware")
                                    _rm_ice = _rm_active_state.get("ice_status")
                                    _rm_nar_raw = _rm_active_state.get("net_actions_remaining")
                                    if _rm_nar_raw is not None:
                                        _rm_net_actions_remaining = _safe_int(_rm_nar_raw)
                                    _rm_active_boosts = _rm_active_state.get("active_boosts")
                                    _rm_cycles_raw = _rm_active_state.get("cycles_remaining")
                                    if _rm_cycles_raw is not None:
                                        _rm_cycles_remaining = _safe_int(_rm_cycles_raw)
                                    _rm_slide_used = bool(_rm_active_state.get("slide_used_this_turn", False))
                                    _rm_system_map = _rm_active_state.get("system_map")
                                    _rm_current_node = _rm_active_state.get("current_node")
                                    _rm_stealth_active = bool(_rm_active_state.get("stealth_active", False))
                                    _rm_quiet_jack_in_used = bool(_rm_active_state.get("quiet_jack_in_used", False))
                                    _rm_stealth_broken_round = _rm_active_state.get("stealth_broken_round")
                                    _rm_net_round = _safe_int(_rm_active_state.get("net_round", 1)) or 1
                                    _rm_pathfinder_used = bool(_rm_active_state.get("pathfinder_used", False))
                                    _rm_rn = _rm_active_state.get("revealed_nodes")
                                    if isinstance(_rm_rn, list):
                                        _rm_revealed_nodes = list(_rm_rn)
                                if not isinstance(_rm_gs, dict):
                                    _rm_gs = {}
                                _rm_tracking_ps = data.get("pipeline_state") if isinstance(data.get("pipeline_state"), dict) else stateful_pipeline_state
                                if _rm_running_hp_map is None or _rm_running_vehicle_map is None:
                                    _rm_running_hp_map, _rm_running_vehicle_map = _extract_resolve_mechanics_tracking_state(_rm_tracking_ps or {})
                                _seed_vehicle_tracking_map_from_actions(
                                    _rm_running_vehicle_map,
                                    _rm_input.get("actions", []),
                                )
                                _seed_hp_tracking_map_from_actions(
                                    _rm_running_hp_map,
                                    _rm_input.get("actions", []),
                                )
                                _rm_relationship_context = build_relationship_context(
                                    actions=_rm_input.get("actions", []),
                                    relationship_owner=str(_rm_input.get("current_player") or ""),
                                    fallback_owner=str(((_rm_tracking_ps or {}).get("combat") or {}).get("current_turn") or ""),
                                    relationship_actor_names=set((_rm_gs.get("edgerunners") or {}).keys()) if isinstance(_rm_gs.get("edgerunners"), dict) else set(),
                                    relationship_present_names=_collect_relationship_present_names(
                                        _rm_input.get("actions", []),
                                        _rm_tracking_ps or {},
                                    ),
                                )
                                # Pathfinder once-per-run + revealed_nodes need to flow
                                # to the action handler. Inject into pathfinder actions
                                # so the handler sees the current state.
                                for _act in (_rm_input.get("actions") or []):
                                    if not isinstance(_act, dict):
                                        continue
                                    if _act.get("type") == "pathfinder":
                                        _act["_pathfinder_used"] = _rm_pathfinder_used
                                        if "revealed_nodes" not in _act:
                                            _act["revealed_nodes"] = list(_rm_revealed_nodes)
                                _rm_result = _rm_resolve_actions(
                                    _rm_input.get("actions", []),
                                    relationships=_rm_gs.get("relationships"),
                                    factions=_rm_gs.get("factions"),
                                    tar_stacks=_rm_tar,
                                    alert_level=_rm_alert,
                                    active_programs=_rm_active_progs,
                                    installed_hardware=_rm_hw,
                                    ice_status=_rm_ice,
                                    combatant_hp=_rm_running_hp_map,
                                    combatant_vehicle_sdp=_rm_running_vehicle_map,
                                    relationship_context=_rm_relationship_context,
                                    edgerunner_states=_rm_gs.get("edgerunners") or {},
                                    character_states=(_rm_tracking_ps or {}).get("character_states"),
                                    net_actions_remaining=_rm_net_actions_remaining,
                                    active_boosts=_rm_active_boosts,
                                    cycles_remaining=_rm_cycles_remaining,
                                    slide_used_this_turn=_rm_slide_used,
                                    system_map=_rm_system_map,
                                    current_node=_rm_current_node,
                                    virus_ledger=(_rm_tracking_ps or {}).get("virus_ledger"),
                                    stealth_active=_rm_stealth_active,
                                    quiet_jack_in_used=_rm_quiet_jack_in_used,
                                    stealth_broken_round=_rm_stealth_broken_round,
                                    net_round=_rm_net_round,
                                )
                                accumulated_rm_state_ops.extend(_rm_result.get("state_ops", []))
                                _advance_tracking_maps_from_state_ops(
                                    _rm_running_hp_map,
                                    _rm_running_vehicle_map,
                                    _rm_result.get("state_ops", []),
                                )
                                _apply_tar_consumed_state_ops(_rm_tracking_ps or {}, _rm_result.get("state_ops", []))
                                # Propagate Slide-used state across iterations so a
                                # second resolve_mechanics call in the same turn sees
                                # the prior Slide and fail-softs (RAW p.205 once-per-turn).
                                if any(isinstance(_op, dict) and _op.get("op") == "slide_used"
                                       for _op in _rm_result.get("state_ops", [])):
                                    _rm_slide_used = True
                                # Propagate node moves across iterations so a chained
                                # move + entered-node action in the next iteration
                                # validates against the new position. Last node_change
                                # in the iteration's ops is authoritative.
                                for _op in _rm_result.get("state_ops", []):
                                    if (isinstance(_op, dict) and _op.get("op") == "node_change"
                                            and isinstance(_op.get("to_node"), str)):
                                        _rm_current_node = _op["to_node"]
                                # Propagate Going Quiet stealth state across iterations.
                                for _op in _rm_result.get("state_ops", []):
                                    if not isinstance(_op, dict):
                                        continue
                                    _opt = _op.get("op")
                                    if _opt == "stealth_set_active":
                                        _rm_stealth_active = True
                                        _rm_quiet_jack_in_used = True
                                    elif _opt == "quiet_jack_in_used":
                                        _rm_quiet_jack_in_used = True
                                    elif _opt == "break_stealth":
                                        _rm_stealth_active = False
                                        _rm_stealth_broken_round = _rm_net_round
                                    elif _opt == "pathfinder_used":
                                        _rm_pathfinder_used = True
                                    elif _opt == "node_reveal":
                                        _new_nodes = _op.get("nodes") or []
                                        if isinstance(_new_nodes, list):
                                            for _nn in _new_nodes:
                                                if isinstance(_nn, str) and _nn not in _rm_revealed_nodes:
                                                    _rm_revealed_nodes.append(_nn)
                                logger.info(f"resolve_mechanics[{_rm_iteration}]: resolved {len(_rm_input.get('actions', []))} actions for {username}")

                                # Accumulate usage from this call
                                _rm_usage_snapshot = {
                                    k: (usage.get(k) or 0) for k in
                                    ['input_tokens', 'output_tokens', 'cache_read_tokens',
                                     'cache_creation_tokens', 'reasoning_tokens']
                                }
                                if resolve_mechanics_extra_usage is None:
                                    resolve_mechanics_extra_usage = _rm_usage_snapshot
                                else:
                                    for _k in _rm_usage_snapshot:
                                        resolve_mechanics_extra_usage[_k] = resolve_mechanics_extra_usage.get(_k, 0) + _rm_usage_snapshot[_k]

                                # Build continuation: assistant + tool_result
                                _resolved_tool_use_id = _rm_call.get("id")
                                if not _resolved_tool_use_id:
                                    logger.warning("resolve_mechanics call missing tool_use_id; skipping continuation for %s", username)
                                    break
                                _rm_assistant = []
                                for _blk in (usage.get('content_blocks') or []):
                                    if _blk.type == "thinking":
                                        _rm_assistant.append({
                                            "type": "thinking",
                                            "thinking": _blk.thinking or "",
                                            "signature": getattr(_blk, 'signature', ''),
                                        })
                                    elif _blk.type == "redacted_thinking":
                                        _rm_assistant.append({
                                            "type": "redacted_thinking",
                                            "data": getattr(_blk, 'data', ''),
                                        })
                                    elif _blk.type == "text":
                                        _rm_assistant.append({"type": "text", "text": _blk.text})
                                    elif _blk.type == "tool_use":
                                        if _blk.id != _resolved_tool_use_id:
                                            continue
                                        _rm_assistant.append({
                                            "type": "tool_use", "id": _blk.id,
                                            "name": _blk.name, "input": _blk.input
                                        })

                                _rm_request_params["messages"] = _rm_request_params["messages"] + [
                                    {"role": "assistant", "content": _rm_assistant},
                                    {"role": "user", "content": [{
                                        "type": "tool_result",
                                        "tool_use_id": _resolved_tool_use_id,
                                        "content": json.dumps(_rm_result)
                                    }]}
                                ]
                                # Keep resolve_mechanics in tools — model may call it again

                                if not client_disconnected:
                                    yield f"event: mechanics_resolved\ndata: {json.dumps(_rm_result)}\n\n"

                                # Stream continuation — model narrates this action, may call resolve_mechanics again
                                _rm_stream = provider.send_request_stream(client, _rm_request_params)
                                _rm_done_received = False
                                for _rm_event in _rm_stream:
                                    if _rm_event.event_type == 'content_delta':
                                        accumulated_content += _rm_event.content
                                        if not client_disconnected:
                                            yield f"event: content\ndata: {json.dumps({'delta': _rm_event.content})}\n\n"
                                        await sync_manager.broadcast_to_chat(
                                            chat_key,
                                            SyncEvent(type=SyncEventType.STREAM_CONTENT, data={"delta": _rm_event.content})
                                        )
                                    elif _rm_event.event_type == 'thinking_delta':
                                        accumulated_thinking += _rm_event.content
                                        if not client_disconnected:
                                            yield f"event: thinking\ndata: {json.dumps({'delta': _rm_event.content})}\n\n"
                                        await sync_manager.broadcast_to_chat(
                                            chat_key,
                                            SyncEvent(type=SyncEventType.STREAM_THINKING, data={"delta": _rm_event.content})
                                        )
                                    elif _rm_event.event_type == 'done':
                                        usage = _rm_event.usage
                                        _rm_done_received = True
                                if not _rm_done_received:
                                    logger.warning("resolve_mechanics continuation ended without done event for %s", username)
                                    break

                                _rm_iteration += 1

                            # Merge accumulated usage from all resolve_mechanics iterations
                            if resolve_mechanics_extra_usage:
                                for _k in ['input_tokens', 'output_tokens', 'cache_read_tokens',
                                           'cache_creation_tokens', 'reasoning_tokens']:
                                    usage[_k] = (usage.get(_k) or 0) + resolve_mechanics_extra_usage.get(_k, 0)
                            # Fall through to report_*_state handling with final usage

                        # Handle hack mode tool output (Claude hack mode)
                        hack_tool_input = None
                        if use_hack_mode and model_id.startswith("claude"):
                            _hack_ps = data.get("pipeline_state", {})
                            _hack_gs = _hack_ps.get("game_state") if isinstance(_hack_ps, dict) else None
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["hack_tool"]):
                                if resolve_mechanics_ran:
                                    _strip_and_merge_resolver_ops(tool_input, accumulated_rm_state_ops)
                                _apply_hack_state_compat(
                                    gs["apply_hack_state"],
                                    hack_state,
                                    tool_input,
                                    resolver_state_ops=accumulated_rm_state_ops,
                                    game_state=_hack_gs,
                                    pipeline_state=_hack_ps,
                                    username=username,
                                    project=request.project or "",
                                )
                                data["hack_state"] = hack_state
                                hack_tool_input = tool_input
                                # Combat clock: advance by game-system-defined round duration
                                _combat_secs = gs.get("combat_round_seconds") if gs else None
                                _advance_mode_hud_clock(_hack_ps, _combat_secs)
                                logger.info(f"Hack mode: applied hack state for {username}, "
                                            f"alert={hack_state.get('alert_level')}, "
                                            f"node={hack_state.get('current_node')}, "
                                            f"complete={tool_input.get('hack_complete', False)}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Hack mode: {_reason}, attempting retry for {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["hack_tool"],
                                        gs.get("hack_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        if resolve_mechanics_ran:
                                            _strip_and_merge_resolver_ops(retry_result, accumulated_rm_state_ops)
                                        _apply_hack_state_compat(
                                            gs["apply_hack_state"],
                                            hack_state,
                                            retry_result,
                                            resolver_state_ops=accumulated_rm_state_ops,
                                            game_state=_hack_gs,
                                            pipeline_state=_hack_ps,
                                            username=username,
                                            project=request.project or "",
                                        )
                                        data["hack_state"] = hack_state
                                        hack_tool_input = retry_result
                                        # Combat clock: advance by game-system-defined round duration (retry)
                                        _combat_secs = gs.get("combat_round_seconds") if gs else None
                                        _advance_mode_hud_clock(_hack_ps, _combat_secs)
                                        logger.info(f"Hack mode: retry succeeded for {username}")
                                    else:
                                        logger.warning(f"Hack mode: retry also failed for {username}")
                                except Exception as retry_err:
                                    logger.error(f"Hack mode: retry error for {username}: {retry_err}")

                        # Handle net_combat mode tool output (Claude net_combat mode)
                        net_combat_tool_input = None
                        if use_net_combat_mode and model_id.startswith("claude"):
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["net_combat_tool"]):
                                if resolve_mechanics_ran:
                                    _strip_and_merge_resolver_ops(tool_input, accumulated_rm_state_ops)
                                net_combat_tool_input = tool_input
                                _nc_ps = data.get("pipeline_state", {})
                                gs["apply_net_combat_state"](_nc_ps, tool_input, game_state=_nc_ps.get("game_state"),
                                                             resolver_state_ops=accumulated_rm_state_ops)
                                data["pipeline_state"] = _nc_ps
                                # Combat clock: advance by game-system-defined round duration
                                _combat_secs = gs.get("combat_round_seconds") if gs else None
                                _advance_mode_hud_clock(_nc_ps, _combat_secs)
                                logger.info(f"Net combat mode: applied state for {username}, "
                                            f"combat_complete={tool_input.get('combat_complete', False)}, "
                                            f"net_complete={tool_input.get('net_complete', False)}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Net combat mode: {_reason}, attempting retry for {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["net_combat_tool"],
                                        gs.get("net_combat_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        if resolve_mechanics_ran:
                                            _strip_and_merge_resolver_ops(retry_result, accumulated_rm_state_ops)
                                        net_combat_tool_input = retry_result
                                        _nc_ps = data.get("pipeline_state", {})
                                        gs["apply_net_combat_state"](_nc_ps, retry_result, game_state=_nc_ps.get("game_state"),
                                                                     resolver_state_ops=accumulated_rm_state_ops)
                                        data["pipeline_state"] = _nc_ps
                                        # Combat clock: advance by game-system-defined round duration (retry)
                                        _combat_secs = gs.get("combat_round_seconds") if gs else None
                                        _advance_mode_hud_clock(_nc_ps, _combat_secs)
                                        logger.info(f"Net combat mode: retry succeeded for {username}")
                                    else:
                                        logger.warning(f"Net combat mode: retry also failed for {username}")
                                except Exception as retry_err:
                                    logger.error(f"Net combat mode: retry error for {username}: {retry_err}")

                        # Handle combat mode tool output (Claude combat mode)
                        combat_tool_input = None
                        ship_combat_tool_input = None
                        if use_combat_mode and model_id.startswith("claude"):
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["combat_tool"]):
                                if resolve_mechanics_ran:
                                    _strip_and_merge_resolver_ops(tool_input, accumulated_rm_state_ops)
                                combat_tool_input = tool_input
                                # Apply combat state updates
                                _combat_ps = data.get("pipeline_state", {})
                                _apply_combat_state(gs, _combat_ps, tool_input)
                                data["pipeline_state"] = _combat_ps
                                # Combat clock: advance by game-system-defined round duration
                                _combat_secs = gs.get("combat_round_seconds") if gs else None
                                _advance_mode_hud_clock(_combat_ps, _combat_secs)
                                logger.info(f"Combat mode: applied state for {username}, "
                                            f"complete={tool_input.get('combat_complete', False)}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Combat mode: {_reason}, attempting retry for {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["combat_tool"],
                                        gs.get("combat_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        if resolve_mechanics_ran:
                                            _strip_and_merge_resolver_ops(retry_result, accumulated_rm_state_ops)
                                        combat_tool_input = retry_result
                                        # Apply state from retry result
                                        _combat_ps = data.get("pipeline_state", {})
                                        _apply_combat_state(gs, _combat_ps, retry_result)
                                        data["pipeline_state"] = _combat_ps
                                        # Combat clock: advance by game-system-defined round duration (retry)
                                        _combat_secs = gs.get("combat_round_seconds") if gs else None
                                        _advance_mode_hud_clock(_combat_ps, _combat_secs)
                                        logger.info(f"Combat mode: retry succeeded for {username}")
                                    else:
                                        logger.warning(f"Combat mode: retry also failed for {username}")
                                except Exception as retry_err:
                                    logger.error(f"Combat mode: retry error for {username}: {retry_err}")

                        # Handle chase mode tool output (Claude chase mode)
                        chase_tool_input = None
                        if use_chase_mode and model_id.startswith("claude"):
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["chase_tool"]):
                                chase_tool_input = tool_input
                                _chase_ps = data.get("pipeline_state", {})
                                gs["apply_chase_state"](_chase_ps, tool_input,
                                                        game_state=_chase_ps.get("game_state"))
                                # Chase clock: advance by chase_round_seconds (default 3 sec)
                                _chase_secs = gs.get("chase_round_seconds") if gs else None
                                _advance_mode_hud_clock(_chase_ps, _chase_secs)
                                data["pipeline_state"] = _chase_ps
                                logger.info(f"Chase mode: applied state for {username}, "
                                            f"complete={tool_input.get('chase_complete', False)}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Chase mode: {_reason}, attempting retry for {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["chase_tool"],
                                        gs.get("chase_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        chase_tool_input = retry_result
                                        _chase_ps = data.get("pipeline_state", {})
                                        gs["apply_chase_state"](_chase_ps, retry_result,
                                                                game_state=_chase_ps.get("game_state"))
                                        _chase_secs = gs.get("chase_round_seconds") if gs else None
                                        _advance_mode_hud_clock(_chase_ps, _chase_secs)
                                        data["pipeline_state"] = _chase_ps
                                        logger.info(f"Chase mode: retry succeeded for {username}")
                                    else:
                                        logger.warning(f"Chase mode: retry also failed for {username}")
                                except Exception as retry_err:
                                    logger.error(f"Chase mode: retry error for {username}: {retry_err}")

                        # Handle ship combat mode tool output (Claude ship combat mode)
                        if use_ship_combat_mode and model_id.startswith("claude"):
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["ship_combat_tool"]):
                                ship_combat_tool_input = tool_input
                                _ship_combat_ps = data.get("pipeline_state", {})
                                if ship_combat_started_this_turn:
                                    _sc_dbg_pre = (_ship_combat_ps.get("ship_combat") or {})
                                    if _sc_dbg_pre.get("bootstrap_messages"):
                                        ship_combat_bootstrap_messages_snapshot = copy.deepcopy(_sc_dbg_pre.get("bootstrap_messages"))
                                gs["apply_ship_combat_state"](_ship_combat_ps, tool_input)
                                # Ship combat clock: advance by game-system-defined ship round duration
                                _ship_secs = gs.get("ship_combat_round_seconds") if gs else None
                                _advance_mode_hud_clock(_ship_combat_ps, _ship_secs)
                                data["pipeline_state"] = _ship_combat_ps
                                logger.info(f"Ship combat mode: applied state for {username}, "
                                            f"complete={tool_input.get('ship_combat_complete', False)}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Ship combat mode: {_reason}, attempting retry for {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["ship_combat_tool"],
                                        gs.get("ship_combat_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        ship_combat_tool_input = retry_result
                                        _ship_combat_ps = data.get("pipeline_state", {})
                                        if ship_combat_started_this_turn:
                                            _sc_dbg_pre = (_ship_combat_ps.get("ship_combat") or {})
                                            if _sc_dbg_pre.get("bootstrap_messages"):
                                                ship_combat_bootstrap_messages_snapshot = copy.deepcopy(_sc_dbg_pre.get("bootstrap_messages"))
                                        gs["apply_ship_combat_state"](_ship_combat_ps, retry_result)
                                        # Ship combat clock: advance by game-system-defined ship round duration (retry)
                                        _ship_secs = gs.get("ship_combat_round_seconds") if gs else None
                                        _advance_mode_hud_clock(_ship_combat_ps, _ship_secs)
                                        data["pipeline_state"] = _ship_combat_ps
                                        logger.info(f"Ship combat mode: retry succeeded for {username}")
                                    else:
                                        logger.warning(f"Ship combat mode: retry also failed for {username}")
                                except Exception as retry_err:
                                    logger.error(f"Ship combat mode: retry error for {username}: {retry_err}")

                        # Extract tool_use input for stateful state updates.
                        # Gated on state_report_tool being defined: gamesystems without one
                        # (Characters, future search-only systems) skip this entire block,
                        # which prevents a non-state tool_input (e.g., Tavily query) from
                        # being misinterpreted as a state report.
                        stateful_tool_input = None
                        stateful_tool_retried = False
                        old_voice_snapshot = None
                        if (use_stateful and stateful_pipeline_state is not None
                                and gs.get("use_game_state", True)
                                and gs.get("state_report_tool")):
                            # Snapshot old voice values for voice_update notifications
                            old_voice_snapshot = {
                                name: entry.get("data", entry).get("voice")
                                for name, entry in stateful_pipeline_state.get("character_states", {}).items()
                            }
                            tool_input = usage.get('tool_use_input')
                            if tool_input and _tool_input_valid(tool_input, gs["state_report_tool"]):
                                # Narration field carries the player-facing prose (streamed live
                                # during the tool call via partial_json parsing). Pull it into
                                # accumulated_content for storage, then strip from tool_input so the
                                # state record doesn't double-store the narrative text.
                                if isinstance(tool_input, dict) and "narration" in tool_input:
                                    narration_text = tool_input.pop("narration") or ""
                                    if narration_text and not (accumulated_content or "").strip():
                                        accumulated_content = narration_text
                                _inject_resolver_ops_stateful(tool_input, accumulated_rm_state_ops, stateful_pipeline_state, gs)
                                is_ooc = tool_input.get("is_ooc", False)
                                if not is_ooc:
                                    stateful_pipeline_state["turn_counter"] += 1
                                    _bump_beat_response_counter(stateful_pipeline_state)
                                current_turn = stateful_pipeline_state["turn_counter"]
                                _stateful_uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads") if request.project else None
                                apply_single_agent_state_updates(
                                    stateful_pipeline_state, tool_input, current_turn, game_system=gs,
                                    uploads_dir=_stateful_uploads_dir,
                                )
                                _apply_deferred_stateful_vehicle_updates(tool_input, stateful_pipeline_state, gs)
                                data["pipeline_state"] = stateful_pipeline_state
                                stateful_tool_input = tool_input
                                if is_ooc:
                                    logger.info(f"Stateful: OOC turn for user {username}, state applied at turn {current_turn}")
                                else:
                                    logger.info(f"Stateful: applied tool state updates for user {username}, turn {current_turn}")
                            else:
                                _reason = "malformed tool_use_input" if tool_input else "no tool_use_input"
                                logger.warning(f"Stateful: {_reason}, attempting retry for user {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        accumulated_content,
                                        accumulated_thinking,
                                        gs["state_report_tool"],
                                        gs.get("single_agent_contract", "")
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        # Same narration extraction as the success path: pull
                                        # narration into accumulated_content, strip from tool_input.
                                        if isinstance(retry_result, dict) and "narration" in retry_result:
                                            narration_text = retry_result.pop("narration") or ""
                                            if narration_text and not (accumulated_content or "").strip():
                                                accumulated_content = narration_text
                                                yield f"event: content\ndata: {json.dumps({'delta': narration_text})}\n\n"
                                        _inject_resolver_ops_stateful(retry_result, accumulated_rm_state_ops, stateful_pipeline_state, gs)
                                        is_ooc = retry_result.get("is_ooc", False)
                                        if not is_ooc:
                                            stateful_pipeline_state["turn_counter"] += 1
                                            _bump_beat_response_counter(stateful_pipeline_state)
                                        current_turn = stateful_pipeline_state["turn_counter"]
                                        _stateful_uploads_dir = os.path.join(get_project_dir(username, request.project), "uploads") if request.project else None
                                        apply_single_agent_state_updates(
                                            stateful_pipeline_state, retry_result, current_turn, game_system=gs,
                                            uploads_dir=_stateful_uploads_dir,
                                        )
                                        _apply_deferred_stateful_vehicle_updates(retry_result, stateful_pipeline_state, gs)
                                        data["pipeline_state"] = stateful_pipeline_state
                                        stateful_tool_input = retry_result
                                        stateful_tool_retried = True
                                        if is_ooc:
                                            logger.info(f"Stateful: retry OOC for user {username}, state applied at turn {current_turn}")
                                        else:
                                            logger.info(f"Stateful: retry succeeded for user {username}, turn {current_turn}")
                                    else:
                                        logger.warning(f"Stateful: retry also failed for user {username}")
                                except Exception as retry_err:
                                    logger.error(f"Stateful: retry error for user {username}: {retry_err}")

                        # Narration recovery: if model emitted state but no prose on a non-OOC
                        # turn, the player sees an empty message bubble. Generate narration from
                        # the state so the turn always renders. Defends against tool-only responses
                        # which can occur when extended thinking is disabled and the model elects
                        # to skip text output even though the contract forbids it.
                        if (use_stateful and stateful_tool_input is not None
                                and not (accumulated_content or "").strip()
                                and not bool(stateful_tool_input.get("is_ooc"))):
                            logger.warning(f"Stateful: empty narration with state present, attempting narration recovery for {username}")
                            try:
                                recovery_text, recovery_usage = await asyncio.to_thread(
                                    _stateful_narration_recovery,
                                    client, provider.MODEL_NAME,
                                    gs.get("single_agent_contract", ""),
                                    request.message or "",
                                    stateful_tool_input,
                                    usage.get('tool_use_id') or "",
                                    usage.get('tool_use_name') or "report_state",
                                )
                                if recovery_usage:
                                    usage['input_tokens'] = usage.get('input_tokens', 0) + recovery_usage['input_tokens']
                                    usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + recovery_usage['cache_read_tokens']
                                    usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + recovery_usage['cache_creation_tokens']
                                    usage['output_tokens'] = usage.get('output_tokens', 0) + recovery_usage['output_tokens']
                                if recovery_text:
                                    accumulated_content = recovery_text
                                    yield f"event: content\ndata: {json.dumps({'delta': recovery_text})}\n\n"
                                    logger.info(f"Stateful: narration recovery succeeded for {username}, {len(recovery_text)} chars")
                                else:
                                    logger.warning(f"Stateful: narration recovery returned no text for {username}")
                            except Exception as recov_err:
                                logger.error(f"Stateful: narration recovery error for {username}: {recov_err}")

                        # Decision-flag side agent (Haiku 4.5). Sole owner of plot_ops on the
                        # stateful path — main report_state schema no longer carries plot_ops.
                        # Runs after narration is finalized; reads narration + user input +
                        # canonical flag manifest + current flag state, emits plot_ops, and
                        # those are applied via apply_decision_flags (with normalization).
                        # Soft-fails: any error returns ([], {}) and the turn proceeds with
                        # no flag updates. Logged as a warning when that happens.
                        if (use_stateful and stateful_tool_input is not None
                                and stateful_pipeline_state is not None
                                and not bool(stateful_tool_input.get("is_ooc"))
                                and (accumulated_content or "").strip()):
                            try:
                                from flag_agent import determine_flag_ops
                                from pipeline import apply_decision_flags
                                _flag_uploads = (
                                    os.path.join(get_project_dir(username, request.project), "uploads")
                                    if request.project else ""
                                )
                                if _canonical_flags:
                                    flag_ops, flag_usage = await asyncio.to_thread(
                                        determine_flag_ops,
                                        client,
                                        _flag_uploads,
                                        _canonical_flags,
                                        stateful_pipeline_state.get("decision_flags", {}),
                                        request.message or "",
                                        accumulated_content,
                                        stateful_pipeline_state.get("scene_state", {}),
                                    )
                                    if flag_usage:
                                        # Surface side-agent usage AND its computed Haiku cost
                                        # separately on the response so the frontend can show
                                        # what flag-tracking is costing per turn. The cost is
                                        # also folded into the message's total cost below
                                        # (with Haiku pricing, not main-model pricing).
                                        from flag_agent import compute_flag_agent_cost
                                        data["flag_agent_usage"] = flag_usage
                                        data["flag_agent_cost"] = compute_flag_agent_cost(flag_usage)
                                        data["flag_agent_model"] = "claude-haiku-4-5"
                                    if flag_ops:
                                        stateful_pipeline_state["decision_flags"] = apply_decision_flags(
                                            stateful_pipeline_state.get("decision_flags", {}),
                                            flag_ops,
                                            canonical_flags=_canonical_flags,
                                        )
                                        data["pipeline_state"] = stateful_pipeline_state
                            except Exception as fa_err:
                                logger.error(f"flag_agent: error for {username}: {fa_err}")

                        # Characters gamesystem: post-stream Haiku side agent
                        # Extracts memory_ops, callback_ops, profile_ops, wb_mod_ops from the
                        # stream that just completed. Runs when (a) Characters mode active,
                        # (b) NOT in interview mode (interview produces no state), and
                        # (c) we got actual content out of the model.
                        if (use_characters and not use_characters_interview
                                and stateful_pipeline_state is not None
                                and (accumulated_content or "").strip()):
                            try:
                                from characters_runtime import run_post_stream_extraction, branch_msg_ids_from_branch_path
                                _characters_dir = (
                                    get_project_dir(username, request.project)
                                    if request.project else None
                                )
                                _characters_state = stateful_pipeline_state.setdefault(
                                    "characters_state",
                                    stateful_pipeline_state.get("characters_state") or {},
                                )
                                _branch_msg_ids_post = branch_msg_ids_from_branch_path(branch_path)
                                _ca_meta = await asyncio.to_thread(
                                    run_post_stream_extraction,
                                    client,
                                    _characters_dir,
                                    _characters_state,
                                    request.message or "",
                                    accumulated_content,
                                    len(branch_path),  # turn counter
                                    branch_msg_id=user_msg_id,
                                    branch_msg_ids=_branch_msg_ids_post,
                                )
                                if _ca_meta.get("character_agent_usage"):
                                    data["character_agent_usage"] = _ca_meta["character_agent_usage"]
                                    data["character_agent_cost"] = _ca_meta["character_agent_cost"]
                                    data["character_agent_model"] = _ca_meta["character_agent_model"]
                                    data["character_agent_ops"] = _ca_meta["character_agent_ops"]
                                # State was mutated in place; persist back
                                stateful_pipeline_state["characters_state"] = _characters_state
                                data["pipeline_state"] = stateful_pipeline_state
                            except Exception as _ca_err:
                                logger.error(f"character_agent: error for {username}: {_ca_err}")

                        # Snapshot state after ops applied (for debug transcript delta).
                        # For TTRPG: snapshot when the model emitted state via tool.
                        # For Characters: always snapshot when state exists — Haiku side
                        # agent may have mutated state without a tool call from Opus.
                        stateful_after_snapshot = None
                        if use_stateful and stateful_pipeline_state is not None and (
                            stateful_tool_input is not None or use_characters
                        ):
                            stateful_after_snapshot = copy.deepcopy(stateful_pipeline_state)

                        # Send state_update SSE event for right panel (single-agent path)
                        if use_stateful and data.get("pipeline_state"):
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            await sync_manager.broadcast_to_chat(
                                chat_key,
                                SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                            )

                        # Emit state_update for Claude net_combat mode
                        if use_net_combat_mode and model_id.startswith("claude") and data.get("pipeline_state"):
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            await sync_manager.broadcast_to_chat(
                                chat_key,
                                SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                            )

                        # Emit state_update for Claude combat mode
                        if use_combat_mode and model_id.startswith("claude") and data.get("pipeline_state"):
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            await sync_manager.broadcast_to_chat(
                                chat_key,
                                SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                            )

                        # Emit state_update for Claude ship combat mode
                        if use_ship_combat_mode and model_id.startswith("claude") and data.get("pipeline_state"):
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            await sync_manager.broadcast_to_chat(
                                chat_key,
                                SyncEvent(type=SyncEventType.STATE_UPDATE, data={"pipeline_state": data["pipeline_state"]})
                            )

                        # Emit hack state update SSE events (Claude hack mode)
                        if use_hack_mode and hack_tool_input:
                            yield f"event: hack_state_update\ndata: {json.dumps(hack_state)}\n\n"
                            await sync_manager.broadcast_to_chat(
                                chat_key,
                                SyncEvent(type=SyncEventType.STATE_UPDATE, data={"hack_state": hack_state})
                            )
                            if hack_tool_input.get("hack_complete"):
                                yield f"event: hack_complete\ndata: {json.dumps({'summary': hack_state.get('narrative_summary', '')})}\n\n"
                                logger.info(f"Hack mode: completed for {username}: "
                                            f"{hack_state.get('narrative_summary', '')[:100]}")

                        # Emit state change notifications (single-agent path)
                        if stateful_tool_input:
                            notifs = extract_state_notifications(
                                stateful_tool_input,
                                old_character_states=old_voice_snapshot)
                            if notifs:
                                yield f"event: state_notifications\ndata: {json.dumps(notifs)}\n\n"
                                await sync_manager.broadcast_to_chat(chat_key,
                                    SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": notifs}))

                        # Emit ship combat NPC action banners (Claude ship combat path)
                        if ship_combat_tool_input:
                            ship_notifs = extract_ship_combat_notifications(ship_combat_tool_input)
                            if ship_notifs:
                                yield f"event: state_notifications\ndata: {json.dumps(ship_notifs)}\n\n"
                                await sync_manager.broadcast_to_chat(
                                    chat_key,
                                    SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": ship_notifs})
                                )

                        # Emit backend-generated notifications (automated expenses, etc.)
                        if stateful_pipeline_state and "game_state" in stateful_pipeline_state:
                            game_notifs = stateful_pipeline_state["game_state"].pop("_pending_notifications", [])
                            if game_notifs:
                                yield f"event: state_notifications\ndata: {json.dumps(game_notifs)}\n\n"
                                await sync_manager.broadcast_to_chat(chat_key,
                                    SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": game_notifs}))

                        # Emit time-override notifications (single-agent path)
                        if stateful_pipeline_state:
                            time_notifs = stateful_pipeline_state.pop("_pending_time_notifications", [])
                            if time_notifs:
                                yield f"event: state_notifications\ndata: {json.dumps(time_notifs)}\n\n"
                                await sync_manager.broadcast_to_chat(chat_key,
                                    SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": time_notifs}))

                        # ── Characters: tool follow-up loop (search_web + fetch_url, Claude only) ──
                        # When the model fires search_web or fetch_url mid-stream, run the tool,
                        # append a tool_result, and re-stream so the model can continue its reply
                        # with the answer in context. Bounded loop in case the model fires
                        # multiple tools (we cap at 2 hops per turn).
                        if (use_characters and not use_characters_interview
                                and model_id.startswith("claude")
                                and usage.get('tool_uses')):
                            from character_search import (
                                SONAR_SEARCH_TOOL,
                                run_sonar_search,
                                format_tool_result_text as _format_search_result,
                            )
                            from character_fetch_url import (
                                FETCH_URL_TOOL,
                                run_fetch_url,
                                format_tool_result_text as _format_fetch_result,
                            )
                            _search_tool_name = SONAR_SEARCH_TOOL["name"]
                            _fetch_tool_name = FETCH_URL_TOOL["name"]
                            _character_tool_names = {_search_tool_name, _fetch_tool_name}
                            _search_calls_total = 0
                            _search_cost_total = 0.0
                            _search_usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "num_search_queries": 0}
                            _search_log: list = []  # [{query, reason, ok, sources_count}] per call — surfaced to UI + persisted
                            _fetch_calls_total = 0
                            _fetch_log: list = []   # [{url, reason, ok, title, error}] per call
                            _max_search_hops = 2
                            _hop = 0
                            _current_tool_uses = usage.get('tool_uses') or []
                            _current_content_blocks = usage.get('content_blocks') or []
                            _followup_messages_base = list(messages_for_api)
                            _perplexity_key = get_api_key(username, "perplexity")

                            while _hop < _max_search_hops:
                                _tool_calls = [t for t in _current_tool_uses if t.get("name") in _character_tool_names]
                                if not _tool_calls:
                                    break
                                _hop += 1
                                _tool_results = []
                                for _call in _tool_calls:
                                    _name = _call.get("name")
                                    _input = _call.get("input") if isinstance(_call.get("input"), dict) else {}
                                    if _name == _search_tool_name:
                                        _query = str(_input.get("query") or "").strip()
                                        _reason = str(_input.get("reason") or "").strip()[:80]
                                        _result = await asyncio.to_thread(run_sonar_search, _perplexity_key, _query)
                                        _search_calls_total += 1
                                        _search_cost_total += float(_result.get("cost", 0.0) or 0.0)
                                        _u = _result.get("usage") or {}
                                        for _k in ("prompt_tokens", "completion_tokens", "total_tokens", "num_search_queries"):
                                            _search_usage_total[_k] = _search_usage_total.get(_k, 0) + (_u.get(_k, 0) or 0)
                                        _search_log.append({
                                            "query": _query, "reason": _reason,
                                            "ok": bool(_result.get("ok")),
                                            "sources_count": len(_result.get("sources") or []),
                                            "error": _result.get("error") if not _result.get("ok") else None,
                                        })
                                        _notif = {
                                            "type": "character_search",
                                            "query": _query, "reason": _reason,
                                            "ok": bool(_result.get("ok")),
                                        }
                                        if not _result.get("ok") and _result.get("error"):
                                            _notif["error"] = str(_result.get("error"))[:200]
                                        _content = _format_search_result(_result)
                                    else:
                                        # fetch_url
                                        _url = str(_input.get("url") or "").strip()
                                        _reason = str(_input.get("reason") or "").strip()[:80]
                                        _result = await asyncio.to_thread(run_fetch_url, _url)
                                        _fetch_calls_total += 1
                                        _fetch_log.append({
                                            "url": _result.get("url") or _url,
                                            "reason": _reason,
                                            "ok": bool(_result.get("ok")),
                                            "title": _result.get("title"),
                                            "error": _result.get("error") if not _result.get("ok") else None,
                                        })
                                        _notif = {
                                            "type": "character_fetch_url",
                                            "url": _result.get("url") or _url,
                                            "reason": _reason,
                                            "title": _result.get("title"),
                                            "ok": bool(_result.get("ok")),
                                        }
                                        if not _result.get("ok") and _result.get("error"):
                                            _notif["error"] = str(_result.get("error"))[:200]
                                        _content = _format_fetch_result(_result)
                                    if not client_disconnected:
                                        yield f"event: state_notifications\ndata: {json.dumps([_notif])}\n\n"
                                        await sync_manager.broadcast_to_chat(
                                            chat_key,
                                            SyncEvent(type=SyncEventType.STATE_NOTIFICATIONS, data={"notifications": [_notif]})
                                        )
                                    _tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": _call.get("id"),
                                        "content": _content,
                                        **({"is_error": True} if not _result.get("ok") else {}),
                                    })

                                # Build follow-up: assistant turn (with tool_use blocks) + user tool_results.
                                _followup_assistant_content = []
                                for _block in _current_content_blocks:
                                    _btype = getattr(_block, 'type', None)
                                    if _btype == 'text':
                                        _followup_assistant_content.append({"type": "text", "text": _block.text})
                                    elif _btype == 'tool_use':
                                        _followup_assistant_content.append({
                                            "type": "tool_use",
                                            "id": _block.id,
                                            "name": _block.name,
                                            "input": _block.input,
                                        })
                                    elif _btype == 'thinking':
                                        _followup_assistant_content.append({
                                            "type": "thinking",
                                            "thinking": _block.thinking,
                                            "signature": getattr(_block, 'signature', ''),
                                        })
                                    elif _btype == 'redacted_thinking':
                                        _followup_assistant_content.append({
                                            "type": "redacted_thinking",
                                            "data": getattr(_block, 'data', ''),
                                        })
                                _followup_messages = _followup_messages_base + [
                                    {"role": "assistant", "content": _followup_assistant_content},
                                    {"role": "user", "content": _tool_results},
                                ]
                                _followup_messages_base = _followup_messages

                                try:
                                    _followup_params = provider.build_request(
                                        messages=_followup_messages,
                                        username=username,
                                        project=request.project,
                                        chat_name=request.chat_name,
                                        is_free_chat=is_free_chat,
                                        use_cache=False,
                                    )
                                    _followup_params["tools"] = [SONAR_SEARCH_TOOL, FETCH_URL_TOOL]
                                    _followup_params["tool_choice"] = {"type": "auto"}
                                    _followup_params.pop("thinking", None)
                                    _followup_params.pop("output_config", None)
                                    _followup_stream = provider.send_request_stream(client, _followup_params)
                                    _next_tool_uses: list = []
                                    _next_content_blocks: list = []
                                    for _ev in _followup_stream:
                                        if _ev.event_type == 'content_delta' and not client_disconnected:
                                            accumulated_content += _ev.content
                                            yield f"event: content\ndata: {json.dumps({'delta': _ev.content})}\n\n"
                                        elif _ev.event_type == 'thinking_delta' and not client_disconnected:
                                            accumulated_thinking += _ev.content
                                            yield f"event: thinking\ndata: {json.dumps({'delta': _ev.content})}\n\n"
                                        elif _ev.event_type == 'done':
                                            _fu_usage = _ev.usage
                                            usage['input_tokens'] = usage.get('input_tokens', 0) + _fu_usage.get('input_tokens', 0)
                                            usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + _fu_usage.get('cache_read_tokens', 0)
                                            usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + _fu_usage.get('cache_creation_tokens', 0)
                                            usage['output_tokens'] = usage.get('output_tokens', 0) + _fu_usage.get('output_tokens', 0)
                                            _next_tool_uses = _fu_usage.get('tool_uses') or []
                                            _next_content_blocks = _fu_usage.get('content_blocks') or []
                                    _current_tool_uses = _next_tool_uses
                                    _current_content_blocks = _next_content_blocks
                                except Exception as _search_followup_err:
                                    logger.error(f"sonar search follow-up error for {username}: {_search_followup_err}")
                                    break

                            if _search_calls_total > 0:
                                data["search_usage"] = _search_usage_total
                                data["search_cost"] = _search_cost_total
                                data["search_calls"] = _search_calls_total
                                data["search_model"] = "sonar"
                                # Persist the per-call log so the assistant message can render
                                # "Nora looked up X" badges after the turn streams (matching how
                                # state_notifications fired live during streaming).
                                data["search_log"] = _search_log
                            if _fetch_calls_total > 0:
                                data["fetch_url_calls"] = _fetch_calls_total
                                data["fetch_url_log"] = _fetch_log

                        # ── Artifact/doc tool processing (Novels system, Claude only) ──
                        artifact_ops = []
                        if gs.get("doc_tools") and model_id.startswith("claude") and usage.get('tool_uses'):
                            doc_tool_names = {t["name"] for t in gs["doc_tools"]}
                            doc_calls = [t for t in usage['tool_uses'] if t.get("name") in doc_tool_names]
                            if doc_calls:
                                artifacts = data.setdefault("artifacts", {})
                                artifact_ops = _process_doc_tool_calls(doc_calls, artifacts)
                                # Emit artifact updates for the frontend panel
                                for op in artifact_ops:
                                    if op["action"] in ("created", "replaced", "edited"):
                                        doc = artifacts.get(op["doc_id"])
                                        if doc and not client_disconnected:
                                            yield f"event: artifact_update\ndata: {json.dumps(doc)}\n\n"
                                # Handle read_doc follow-up: send tool_results and get continuation
                                read_ops = [op for op in artifact_ops if op["action"] == "read"]
                                error_ops = [op for op in artifact_ops if op["action"] == "error"]
                                if read_ops or error_ops:
                                    tool_results = []
                                    for op in read_ops:
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": op["tool_use_id"],
                                            "content": op["content"]
                                        })
                                    for op in error_ops:
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": op["tool_use_id"],
                                            "content": f"Error: document '{op['doc_id']}' not found.",
                                            "is_error": True
                                        })
                                    # Build follow-up messages: assistant tool_use + user tool_results
                                    # Convert SDK content blocks to plain dicts for the API
                                    followup_content = []
                                    for block in (usage.get('content_blocks') or []):
                                        if getattr(block, 'type', None) == 'thinking':
                                            followup_content.append({"type": "thinking", "thinking": block.thinking,
                                                                      "signature": getattr(block, 'signature', '')})
                                        elif getattr(block, 'type', None) == 'redacted_thinking':
                                            followup_content.append({"type": "redacted_thinking",
                                                                      "data": getattr(block, 'data', '')})
                                        elif getattr(block, 'type', None) == 'text':
                                            followup_content.append({"type": "text", "text": block.text})
                                        elif getattr(block, 'type', None) == 'tool_use':
                                            followup_content.append({"type": "tool_use", "id": block.id,
                                                                      "name": block.name, "input": block.input})
                                    followup_assistant = {"role": "assistant", "content": followup_content}
                                    followup_user = {"role": "user", "content": tool_results}
                                    followup_messages = messages_for_api + [followup_assistant, followup_user]
                                    try:
                                        followup_params = provider.build_request(
                                            messages=followup_messages,
                                            username=username,
                                            project=request.project,
                                            chat_name=request.chat_name,
                                            is_free_chat=is_free_chat,
                                            use_cache=False
                                        )
                                        followup_params["tools"] = gs["doc_tools"]
                                        followup_params["tool_choice"] = {"type": "auto"}
                                        followup_stream = provider.send_request_stream(client, followup_params)
                                        for followup_event in followup_stream:
                                            if followup_event.event_type == 'content_delta' and not client_disconnected:
                                                accumulated_content += followup_event.content
                                                yield f"event: content\ndata: {json.dumps({'delta': followup_event.content})}\n\n"
                                            elif followup_event.event_type == 'thinking_delta' and not client_disconnected:
                                                accumulated_thinking += followup_event.content
                                                yield f"event: thinking\ndata: {json.dumps({'delta': followup_event.content})}\n\n"
                                            elif followup_event.event_type == 'done':
                                                followup_usage = followup_event.usage
                                                usage['input_tokens'] = usage.get('input_tokens', 0) + followup_usage.get('input_tokens', 0)
                                                usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + followup_usage.get('cache_read_tokens', 0)
                                                usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + followup_usage.get('cache_creation_tokens', 0)
                                                usage['output_tokens'] = usage.get('output_tokens', 0) + followup_usage.get('output_tokens', 0)
                                                # Process any additional doc tool calls from the follow-up
                                                followup_tool_uses = followup_usage.get('tool_uses', [])
                                                followup_doc_calls = [t for t in followup_tool_uses if t.get("name") in doc_tool_names]
                                                if followup_doc_calls:
                                                    followup_ops = _process_doc_tool_calls(followup_doc_calls, artifacts)
                                                    artifact_ops.extend(followup_ops)
                                                    for op in followup_ops:
                                                        if op["action"] in ("created", "replaced", "edited"):
                                                            doc = artifacts.get(op["doc_id"])
                                                            if doc and not client_disconnected:
                                                                yield f"event: artifact_update\ndata: {json.dumps(doc)}\n\n"
                                    except Exception as followup_err:
                                        logger.error(f"Doc tool follow-up error for {username}: {followup_err}")

                        # Use accumulated content as primary (we streamed it), fallback to usage content
                        assistant_message = accumulated_content or usage.get('content') or ''
                        reasoning_summary = accumulated_thinking or usage.get('reasoning')

                        # ── Sex mode: detect [SCENE COMPLETE] and [SCENE HANDOFF] ──
                        sex_scene_complete = False
                        _sex_handoff_summary_for_log = None
                        sex_scene_summary_text = None
                        sex_restore_model = None
                        # Captured for the background richer-summary generator (kicked off after save_chat).
                        _sex_completed_start_id = None
                        _sex_completed_npcs: list[str] = []
                        _sex_completed_handoff_summary = None
                        if use_sex_mode and "[SCENE COMPLETE]" in assistant_message:
                            sex_scene_complete = True
                            _sm = re.search(r'\[SCENE SUMMARY:\s*(.*?)\]', assistant_message, re.DOTALL)
                            if _sm:
                                sex_scene_summary_text = _sm.group(1).strip()
                            # Strip tags from displayed content
                            assistant_message = assistant_message.replace("[SCENE COMPLETE]", "").strip()
                            assistant_message = re.sub(r'\[SCENE SUMMARY:.*?\]', '', assistant_message, flags=re.DOTALL).strip()
                            # Capture sex_scene metadata BEFORE clearing it so the background
                            # summarizer thread (kicked off after save_chat) can re-load the chat
                            # and gather the full transcript.
                            ps = data.get("pipeline_state", {})
                            _ss = ps.get("sex_scene") or {}
                            if isinstance(_ss, dict):
                                _sex_completed_start_id = _ss.get("start_message_id")
                                _sex_completed_npcs = list(_ss.get("npcs", []) or [])
                                _sex_completed_handoff_summary = _ss.get("summary")
                            sex_restore_model = _ss.get("original_model") if isinstance(_ss, dict) else None
                            ps["sex_scene"] = None
                            data["pipeline_state"] = ps
                            if sex_restore_model:
                                data["model"] = sex_restore_model
                            # Emit state_update so CharacterPanel clears sex scene indicator
                            if not client_disconnected:
                                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

                        # /sex handoff: detect [SCENE HANDOFF] block from the handoff turn
                        _sex_handoff_detected = False
                        if _sex_handoff_npcs and "[SCENE HANDOFF]" in assistant_message:
                            _sex_handoff_detected = True
                            _hm = re.search(r'\[SCENE HANDOFF\](.*?)\[/SCENE HANDOFF\]', assistant_message, re.DOTALL)
                            _handoff_summary = _hm.group(1).strip() if _hm else f"An intimate scene begins with {', '.join(_sex_handoff_npcs)}."
                            # Set sex_scene state for next turn
                            ps = data.get("pipeline_state", {})
                            ps["sex_scene"] = {
                                "npcs": _sex_handoff_npcs,
                                "summary": _handoff_summary,
                                "original_model": data.get("model", DEFAULT_MODEL),
                            }
                            data["pipeline_state"] = ps
                            _sex_handoff_summary_for_log = _handoff_summary
                            # Delete handoff messages — remove user msg and don't append assistant msg
                            data["messages"] = [m for m in data["messages"] if m.get("id") != user_msg_id]
                            data["current_leaf_id"] = parent_id
                            # Persist immediately — handoff messages are deleted, so if anything
                            # fails before the normal save_chat at end-of-stream, sex_scene
                            # would be lost with no trace in the chat
                            save_chat(username, request.chat_name, data, request.project)
                            logger.info(f"Sex handoff: saved sex_scene state for {username}, "
                                        f"NPCs={_sex_handoff_npcs}, summary={_handoff_summary[:80]}")
                            # Emit state_update so CharacterPanel shows sex scene indicator immediately
                            if not client_disconnected:
                                yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"

                        # Get cross-model providers for token counting
                        gpt_provider = get_gpt_provider()
                        claude_provider = get_claude_provider()
                        claude_api_key = get_api_key(username, "anthropic")

                        # Build user content for cross-model counting
                        if request.attached_files:
                            file_wrappers = [f"====FILE: {f.filename}====\n{f.content}\n====END FILE====" for f in request.attached_files]
                            user_content_for_counting = "\n\n".join(file_wrappers) + "\n\n" + request.message
                        else:
                            user_content_for_counting = request.message

                        # Update system message tokens
                        system_msg_ref = branch_path[0]
                        system_content = system_msg_ref.get("content", "")
                        if system_msg_ref.get("total_gpt_tokens") is None:
                            system_msg_ref["total_gpt_tokens"] = gpt_provider.count_tokens(system_content)
                        if system_msg_ref.get("total_claude_tokens") is None and claude_api_key:
                            system_msg_ref["total_claude_tokens"] = claude_provider.count_tokens_api(system_content, claude_api_key)
                        if model_id.startswith("claude"):
                            system_msg_ref["total_tokens"] = system_msg_ref.get("total_claude_tokens")
                        else:
                            system_msg_ref["total_tokens"] = system_msg_ref.get("total_gpt_tokens")

                        # Calculate dual token counts
                        if model_id.startswith("claude"):
                            user_claude_tokens = claude_provider.count_tokens_api(user_content_for_counting, api_key)
                            user_gpt_tokens = gpt_provider.count_tokens(user_content_for_counting)

                            for msg in data["messages"]:
                                if msg.get("id") == user_msg_id:
                                    msg["total_claude_tokens"] = user_claude_tokens
                                    msg["total_gpt_tokens"] = user_gpt_tokens
                                    msg["total_tokens"] = user_claude_tokens
                                    break

                            # Count tokens on content only (tool_use JSON is stripped from stored content)
                            assistant_claude_tokens = claude_provider.count_tokens_api(assistant_message, api_key)
                            assistant_gpt_tokens = gpt_provider.count_tokens(assistant_message)
                        else:
                            known_tokens = system_msg_ref.get("total_gpt_tokens", 0)
                            for msg in branch_path[context_start_index:-1]:
                                gpt_tokens = msg.get("total_gpt_tokens")
                                known_tokens += gpt_tokens if gpt_tokens is not None else (msg.get("total_tokens") or 0)
                            user_gpt_tokens = max(0, usage['input_tokens'] - known_tokens)

                            if claude_api_key:
                                user_claude_tokens = claude_provider.count_tokens_api(user_content_for_counting, claude_api_key)
                                assistant_claude_tokens = claude_provider.count_tokens_api(assistant_message, claude_api_key)
                            else:
                                user_claude_tokens = None
                                assistant_claude_tokens = None

                            for msg in data["messages"]:
                                if msg.get("id") == user_msg_id:
                                    msg["total_gpt_tokens"] = user_gpt_tokens
                                    if user_claude_tokens is not None:
                                        msg["total_claude_tokens"] = user_claude_tokens
                                    msg["total_tokens"] = user_gpt_tokens
                                    break

                            # Count tokens on content only (tool_use JSON is stripped from stored content)
                            assistant_gpt_tokens = gpt_provider.count_tokens(assistant_message)

                        # Create ParsedResponse for cost calculation
                        from providers import ParsedResponse
                        parsed = ParsedResponse(
                            content=assistant_message,
                            reasoning=reasoning_summary,
                            input_tokens=usage['input_tokens'],
                            cache_read_tokens=usage['cache_read_tokens'],
                            cache_creation_tokens=usage['cache_creation_tokens'],
                            output_tokens=usage['output_tokens'],
                            reasoning_tokens=usage['reasoning_tokens']
                        )

                        new_input_tokens = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
                        total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens

                        # Extract service tier for GPT-5.2 (flex vs standard)
                        service_tier = usage.get('service_tier')

                        # Calculate cost using tier-aware pricing for GPT-5.2
                        if model_id.startswith("gpt") and service_tier:
                            total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                        else:
                            total_cost = provider.calculate_cost(parsed)
                        tokens_str = provider.format_token_string(parsed)

                        # Fold the flag-agent's Haiku cost into total cost. Haiku has its own
                        # pricing ($1/$5/MTok) which differs from the main model's, so it's
                        # computed separately and added rather than mixed into `parsed`.
                        flag_agent_cost = float(data.get("flag_agent_cost", 0.0) or 0.0)
                        total_cost += flag_agent_cost
                        # Same fold-in for Characters' side agents (Sonnet extraction + Opus 4.5 off-screen + Haiku recall + Sonnet inner-state pre-pass).
                        total_cost += float(data.get("character_agent_cost", 0.0) or 0.0)
                        total_cost += float(data.get("off_screen_cost", 0.0) or 0.0)
                        total_cost += float(data.get("recall_cost", 0.0) or 0.0)
                        total_cost += float(data.get("inner_state_cost", 0.0) or 0.0)
                        # Sonar search cost (Perplexity returns dollars directly via usage.cost.total_cost).
                        total_cost += float(data.get("search_cost", 0.0) or 0.0)

                        # Apply free tokens
                        if model_id.startswith('gpt'):
                            actual_cost, cost_str, pending_usage = apply_free_tokens(username, total_tokens, total_cost, commit=False)
                        else:
                            actual_cost = total_cost
                            cost_str = f"${actual_cost:.6f}"
                            pending_usage = None

                        # Update stats
                        stats = data.get("stats", create_empty_stats())
                        stats["total_input_tokens"] += new_input_tokens
                        stats["total_cached_tokens"] += parsed.cache_read_tokens
                        stats["total_output_tokens"] += parsed.output_tokens
                        stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
                        stats["total_cost"] += actual_cost
                        stats["total_prompts"] += 1
                        stats["last_accessed"] = datetime.now(timezone.utc).isoformat()
                        data["stats"] = stats

                        # Add assistant message
                        assistant_msg_id = generate_message_id()
                        assistant_parent_id = user_msg_id
                        ship_combat_init_hidden_message_data = None
                        if use_ship_combat_mode and ship_combat_started_this_turn:
                            ship_combat_init_hidden_message_data = build_ship_combat_hidden_init_message(
                                user_msg_id,
                                opening_override=ship_combat_opening_narration_hint
                            )
                            data["messages"].append(ship_combat_init_hidden_message_data)
                            assistant_parent_id = ship_combat_init_hidden_message_data["id"]
                        assistant_msg_data = {
                            "id": assistant_msg_id,
                            "parent_id": assistant_parent_id,
                            "role": "assistant",
                            "content": assistant_message,
                            "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                            "tokens": tokens_str,
                            "cost": cost_str,
                            "total_tokens": assistant_claude_tokens if model_id.startswith("claude") else assistant_gpt_tokens,
                            "total_gpt_tokens": assistant_gpt_tokens,
                            "model": model_id
                        }
                        if assistant_claude_tokens is not None:
                            assistant_msg_data["total_claude_tokens"] = assistant_claude_tokens
                        if reasoning_summary:
                            assistant_msg_data["reasoning"] = reasoning_summary
                        if service_tier:
                            assistant_msg_data["service_tier"] = service_tier
                        # Tag interview-mode assistant messages so subsequent correspondence
                        # turns filter them out of context (parallels the user-message tag).
                        if use_characters_interview:
                            assistant_msg_data["_characters_interview_mode"] = True
                        if use_stateful and stateful_injected_snapshot is not None:
                            assistant_msg_data["pipeline_state_injected"] = stateful_injected_snapshot
                        if use_stateful and stateful_tool_input is not None:
                            assistant_msg_data["state_tool_input"] = stateful_tool_input
                            if stateful_tool_retried:
                                assistant_msg_data["state_tool_retried"] = True
                        # Per-message visibility into the flag side agent: usage + cost
                        # so the frontend can render a separate line item.
                        if data.get("flag_agent_usage"):
                            assistant_msg_data["flag_agent_usage"] = data["flag_agent_usage"]
                            assistant_msg_data["flag_agent_cost"] = float(data.get("flag_agent_cost", 0.0) or 0.0)
                            assistant_msg_data["flag_agent_model"] = data.get("flag_agent_model", "claude-haiku-4-5")
                        # Same per-message visibility for Characters' Haiku side agent
                        # and Opus 4.5 off-screen generator.
                        if data.get("character_agent_usage"):
                            assistant_msg_data["character_agent_usage"] = data["character_agent_usage"]
                            assistant_msg_data["character_agent_cost"] = float(data.get("character_agent_cost", 0.0) or 0.0)
                            assistant_msg_data["character_agent_model"] = data.get("character_agent_model", "claude-sonnet-4-6")
                            if data.get("character_agent_ops"):
                                assistant_msg_data["character_agent_ops"] = data["character_agent_ops"]
                        if data.get("off_screen_usage"):
                            assistant_msg_data["off_screen_usage"] = data["off_screen_usage"]
                            assistant_msg_data["off_screen_cost"] = float(data.get("off_screen_cost", 0.0) or 0.0)
                            assistant_msg_data["off_screen_model"] = data.get("off_screen_model", "claude-opus-4.5")
                        if data.get("recall_usage"):
                            assistant_msg_data["recall_usage"] = data["recall_usage"]
                            assistant_msg_data["recall_cost"] = float(data.get("recall_cost", 0.0) or 0.0)
                            assistant_msg_data["recall_model"] = data.get("recall_model", "claude-haiku-4-5")
                            if data.get("recall_ids"):
                                assistant_msg_data["recall_ids"] = data["recall_ids"]
                        if data.get("inner_state_usage"):
                            assistant_msg_data["inner_state_usage"] = data["inner_state_usage"]
                            assistant_msg_data["inner_state_cost"] = float(data.get("inner_state_cost", 0.0) or 0.0)
                            assistant_msg_data["inner_state_model"] = data.get("inner_state_model", "claude-sonnet-4-6")
                        if data.get("search_calls"):
                            assistant_msg_data["search_usage"] = data.get("search_usage", {})
                            assistant_msg_data["search_cost"] = float(data.get("search_cost", 0.0) or 0.0)
                            assistant_msg_data["search_calls"] = int(data.get("search_calls", 0) or 0)
                            assistant_msg_data["search_model"] = data.get("search_model", "sonar")
                            if data.get("search_log"):
                                assistant_msg_data["search_log"] = data["search_log"]
                        if data.get("fetch_url_calls"):
                            assistant_msg_data["fetch_url_calls"] = int(data.get("fetch_url_calls", 0) or 0)
                            if data.get("fetch_url_log"):
                                assistant_msg_data["fetch_url_log"] = data["fetch_url_log"]
                        if use_stateful and stateful_after_snapshot is not None:
                            assistant_msg_data["pipeline_state_after"] = stateful_after_snapshot

                        # Flag artifact operations on message (for inline cards)
                        if artifact_ops:
                            assistant_msg_data["artifact_ops"] = [
                                {k: v for k, v in op.items() if k != 'content'}
                                for op in artifact_ops
                            ]

                        # Flag hack mode messages
                        if use_hack_mode:
                            assistant_msg_data["hack_mode"] = True
                            if hack_tool_input:
                                assistant_msg_data["hack_tool_input"] = hack_tool_input
                            if data.get("hack_state"):
                                assistant_msg_data["hack_state_after"] = copy.deepcopy(data["hack_state"])

                        # Flag net_combat mode messages (Claude path)
                        if use_net_combat_mode:
                            assistant_msg_data["net_combat_mode"] = True
                            if net_combat_tool_input:
                                assistant_msg_data["net_combat_tool_input"] = net_combat_tool_input

                        # Flag combat mode messages (Claude path)
                        if use_combat_mode:
                            assistant_msg_data["combat_mode"] = True
                            if combat_tool_input:
                                assistant_msg_data["combat_tool_input"] = combat_tool_input

                        # Flag chase mode messages (Hot Pursuit, Claude + GPT paths)
                        if use_chase_mode:
                            assistant_msg_data["chase_mode"] = True
                            if chase_tool_input:
                                assistant_msg_data["chase_tool_input"] = chase_tool_input
                            if chase_started_this_turn:
                                assistant_msg_data["chase_started"] = True
                                # Persist the hidden init message into the branch
                                # so future exchanges can read the chase opener.
                                if chase_init_hidden_message_data:
                                    data["messages"].append(chase_init_hidden_message_data)
                                    assistant_msg_data["parent_id"] = chase_init_hidden_message_data["id"]

                        # Flag sex mode messages
                        if use_sex_mode:
                            assistant_msg_data["sex_mode"] = True
                            if sex_scene_summary_text:
                                assistant_msg_data["sex_scene_summary"] = sex_scene_summary_text
                            # Stamp handoff summary on first sex mode message for debug transcript
                            if _sex_first_exchange:
                                _hs = (data.get("pipeline_state", {}).get("sex_scene") or {}).get("summary")
                                if _hs:
                                    assistant_msg_data["sex_handoff_summary"] = _hs

                        # Flag ship combat mode messages (Claude path)
                        if use_ship_combat_mode:
                            assistant_msg_data["ship_combat_mode"] = True
                            if ship_combat_tool_input:
                                assistant_msg_data["ship_combat_tool_input"] = ship_combat_tool_input
                                if ship_combat_tool_input.get("combat_outcome"):
                                    assistant_msg_data["ship_combat_combat_outcome"] = ship_combat_tool_input["combat_outcome"]
                            if ship_combat_started_this_turn:
                                assistant_msg_data["ship_combat_started"] = True
                                if ship_combat_opening_narration_hint:
                                    assistant_msg_data["ship_combat_opening_narration"] = ship_combat_opening_narration_hint
                                    assistant_msg_data["ship_combat_opening_embedded"] = _ship_opening_embedded(
                                        ship_combat_opening_narration_hint, assistant_message
                                    )
                                _sc_dbg = (data.get("pipeline_state", {}).get("ship_combat") or {})
                                if _sc_dbg.get("bootstrap_messages"):
                                    assistant_msg_data["ship_combat_bootstrap_messages"] = copy.deepcopy(_sc_dbg.get("bootstrap_messages"))
                                elif ship_combat_bootstrap_messages_snapshot:
                                    assistant_msg_data["ship_combat_bootstrap_messages"] = copy.deepcopy(ship_combat_bootstrap_messages_snapshot)

                        if not _sex_handoff_detected:
                            _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                            data["current_leaf_id"] = assistant_msg_id

                        # Track combat start_message_id when combat begins
                        _active_combat = data.get("pipeline_state", {}).get("combat")
                        if _active_combat and "start_message_id" not in _active_combat:
                            _active_combat["start_message_id"] = assistant_msg_id
                        _active_nc = data.get("pipeline_state", {}).get("net_combat")
                        if _active_nc and "start_message_id" not in _active_nc:
                            _active_nc["start_message_id"] = assistant_msg_id
                        _active_ship_combat = data.get("pipeline_state", {}).get("ship_combat")
                        if _active_ship_combat and "start_message_id" not in _active_ship_combat:
                            _active_ship_combat["start_message_id"] = assistant_msg_id
                        _active_chase = data.get("pipeline_state", {}).get("chase")
                        if (_active_chase
                            and isinstance(_active_chase, dict)
                            and not _active_chase.get("start_message_id")):
                            _active_chase["start_message_id"] = assistant_msg_id

                        # Check for hack_trigger in normal stateful tool output
                        if (stateful_tool_input
                            and stateful_tool_input.get("hack_trigger")
                            and gs and gs.get("init_hack_state")):
                            ht = stateful_tool_input["hack_trigger"]
                            _cs = (stateful_pipeline_state.get("character_states", {})
                                   if stateful_pipeline_state else
                                   data.get("pipeline_state", {}).get("character_states", {}))
                            _ps = stateful_pipeline_state or data.get("pipeline_state", {})
                            data["hack_state"] = _init_hack_from_trigger(gs, ht, _cs, pipeline_state=_ps)
                            data["hack_state"]["start_message_id"] = assistant_msg_id
                            yield f"event: hack_mode_start\ndata: {json.dumps(data['hack_state'])}\n\n"
                            logger.info(f"Hack trigger: {ht.get('tier')} on {ht.get('target_system')} "
                                        f"SR{ht.get('sr')} for {username}")

                        # Check for chase_trigger in normal stateful tool output
                        if (stateful_tool_input
                            and stateful_tool_input.get("chase_trigger")
                            and gs and gs.get("chase_contract")
                            and gs.get("init_chase_state")):
                            ct = stateful_tool_input["chase_trigger"]
                            _ps = stateful_pipeline_state or data.get("pipeline_state", {})
                            _ct_vehicles = ct.get("vehicles") if isinstance(ct.get("vehicles"), dict) else {}
                            _ct_pursuers = set(ct.get("pursuer_vehicles") or [])
                            _seeded_vehicles = {}
                            for vname, v in _ct_vehicles.items():
                                if not isinstance(v, dict):
                                    continue
                                _seeded_vehicles[str(vname)] = {
                                    "operator": v.get("operator", ""),
                                    "occupants": list(v.get("occupants") or []),
                                    "square": int(v.get("starting_square", 0) or 0),
                                    "combat_speed_move": int(v.get("combat_speed_move", 20) or 20),
                                    "sdp_max": int(v.get("sdp_max", 20) or 20),
                                    "sdp_current": int(v.get("sdp_current", v.get("sdp_max", 20)) or 20),
                                    "sp": int(v.get("sp", 0) or 0),
                                    "type": str(v.get("type") or "land"),
                                    "is_pursuer": vname in _ct_pursuers if _ct_pursuers else bool(v.get("is_pursuer", False)),
                                    "notes": v.get("notes", ""),
                                }
                            _seeded_chase = gs["init_chase_state"](
                                grid_length=int(ct.get("grid_length", 8) or 8),
                                vehicles=_seeded_vehicles,
                                started_from="state_report",
                                context=ct.get("context"),
                                start_message_id=assistant_msg_id,
                            )
                            if ct.get("route"):
                                _seeded_chase["route"] = ct["route"]
                            _ps["chase"] = _seeded_chase
                            # Stamp HUD overlay immediately so the next
                            # exchange's HUD reflects the chase route.
                            from game_systems.cpred_chase import stamp_chase_hud_overlay
                            stamp_chase_hud_overlay(_ps)
                            yield f"event: chase_mode_start\ndata: {json.dumps(_seeded_chase)}\n\n"
                            logger.info(
                                f"Chase trigger: {len(_seeded_vehicles)} vehicles, "
                                f"{len(_ct_pursuers) or sum(1 for v in _seeded_vehicles.values() if v['is_pursuer'])} pursuers "
                                f"for {username}"
                            )

                        # Check for ship_combat_trigger in normal stateful tool output
                        if (stateful_tool_input
                            and stateful_tool_input.get("ship_combat_trigger")
                            and gs and gs.get("ship_combat_contract")):
                            sct = stateful_tool_input["ship_combat_trigger"]
                            _sc_ps = data.get("pipeline_state", {})
                            _sc_ps["ship_combat"] = {
                                "round": 1,
                                "initiative_order": [],
                                "current_ship": None,
                                "current_role": None,
                                "environment": sct.get("environment", "Open Space"),
                                "handoff_summary": sct.get("handoff_summary"),
                                "opening_narration": sct.get("opening_narration"),
                                "encounter_type": sct.get("encounter_type"),
                                "objective": sct.get("objective"),
                                "positioning": sct.get("positioning"),
                                "immediate_complications": sct.get("immediate_complications", []),
                                "enemy_ships": sct.get("enemy_ships", []),
                                "bootstrap_done": False,
                                "ship_combat_handoff_source": "trigger" if sct.get("handoff_summary") else None,
                                "bootstrap_messages": [],
                                "start_message_id": assistant_msg_id,
                            }
                            data["pipeline_state"] = _sc_ps
                            ship_combat_triggered_this_turn = True
                            yield f"event: state_update\ndata: {json.dumps(data['pipeline_state'])}\n\n"
                            logger.info(f"Ship combat trigger: {sct.get('environment')} "
                                        f"enemies={sct.get('enemy_ships', [])} for {username}")

                        save_chat(username, request.chat_name, data, request.project)
                        logger.info(f"Stream: saved chat for user {username}")

                        # Regenerate debug transcript if this is a project chat
                        if request.project:
                            try:
                                debug_chat_path = get_chat_path(username, request.chat_name, request.project)
                                generate_debug_transcript(data, debug_chat_path, request.chat_name)
                                generate_plain_transcript(data, debug_chat_path, request.chat_name)
                            except Exception as e:
                                logger.warning(f"Stream: failed to generate debug transcript: {e}")

                        # ── Sex mode auto-exit: kick off background richer-summary generator ──
                        # When the model auto-exits with [SCENE COMPLETE], the inline [SCENE SUMMARY: ...]
                        # tag is only 1-2 sentences. Generate an up-to-3-paragraph story-shaped summary
                        # in the background using the same path /sex manual exit uses, and stamp it on
                        # the just-saved assistant message — overwriting the inline one. This makes
                        # auto-exit and manual-exit produce equivalent FADE TO BLACK content for the
                        # next normal turn.
                        if sex_scene_complete and _sex_completed_start_id:
                            _sex_api_key = get_api_key(username, "anthropic")
                            if _sex_api_key:
                                # Gather scene messages from the just-saved chat (current message is on disk now).
                                _scene_msgs = []
                                _branch = get_path_to_root(data.get("messages", []), assistant_msg_id)
                                _found_start = False
                                for _m in _branch:
                                    if not _found_start and _m.get("id") == _sex_completed_start_id:
                                        _found_start = True
                                    if _found_start and _m.get("sex_mode"):
                                        _scene_msgs.append({"role": _m["role"], "content": _m["content"]})
                                if _scene_msgs:
                                    _bg_chat_name = request.chat_name
                                    _bg_project = request.project
                                    _bg_username = username
                                    _bg_assistant_id = assistant_msg_id
                                    _bg_npcs = list(_sex_completed_npcs)
                                    _bg_handoff = _sex_completed_handoff_summary
                                    def _bg_sex_summary():
                                        try:
                                            _summary = _generate_sex_scene_summary(
                                                _sex_api_key, _scene_msgs, _bg_npcs, _bg_handoff
                                            )
                                            if not _summary:
                                                return
                                            _bg_data = load_chat(_bg_username, _bg_chat_name, _bg_project)
                                            if not _bg_data:
                                                return
                                            for _msg in _bg_data.get("messages", []):
                                                if _msg.get("id") == _bg_assistant_id:
                                                    _msg["sex_scene_summary"] = _summary
                                                    break
                                            save_chat(_bg_username, _bg_chat_name, _bg_data, _bg_project)
                                            logger.info(
                                                f"Auto-exit sex scene summary saved for "
                                                f"{_bg_username}/{_bg_chat_name} ({len(_summary)} chars)"
                                            )
                                        except Exception:
                                            logger.exception("Background sex scene summary (auto-exit) failed")
                                    threading.Thread(target=_bg_sex_summary, daemon=True).start()

                        # Commit deferred updates
                        if pending_usage is not None:
                            save_daily_usage(username, pending_usage)

                        if model_id.startswith("claude"):
                            user_total = user_claude_tokens if user_claude_tokens is not None else 0
                            assistant_total = assistant_claude_tokens if assistant_claude_tokens is not None else 0
                        else:
                            user_total = user_gpt_tokens if user_gpt_tokens is not None else 0
                            assistant_total = assistant_gpt_tokens if assistant_gpt_tokens is not None else 0
                        context_tokens = user_total + assistant_total
                        update_persistent_stats(username, new_input_tokens, parsed.cache_read_tokens, parsed.output_tokens, parsed.reasoning_tokens, actual_cost, model=model_id, context_tokens=context_tokens)

                        # Calculate branch info
                        if _sex_handoff_detected:
                            branch_path_final = get_path_to_root(data["messages"], parent_id) if parent_id else data["messages"][:1]
                        else:
                            branch_path_final = get_path_to_root(data["messages"], assistant_msg_id)
                        branch_total_messages = len(branch_path_final)

                        # Calculate model-specific stats
                        response_stats = stats.copy()
                        gpt_prompts = 0
                        sonnet_prompts = 0
                        gpt_context_tokens = 0
                        sonnet_context_tokens = 0
                        all_chat_messages = data.get("messages", [])
                        messages_by_id = {m.get("id"): m for m in all_chat_messages if m.get("id")}
                        for msg in all_chat_messages:
                            if msg.get("role") == "assistant":
                                msg_model = msg.get("model", "")
                                is_sonnet = msg_model.startswith("claude")
                                p_id = msg.get("parent_id")
                                if is_sonnet:
                                    sonnet_prompts += 1
                                    a_tokens = msg.get("total_claude_tokens") or msg.get("total_tokens", 0) or 0
                                    u_tokens = 0
                                    if p_id and p_id in messages_by_id:
                                        parent = messages_by_id[p_id]
                                        u_tokens = parent.get("total_claude_tokens") or parent.get("total_tokens", 0) or 0
                                    sonnet_context_tokens += u_tokens + a_tokens
                                else:
                                    gpt_prompts += 1
                                    a_tokens = msg.get("total_gpt_tokens") or msg.get("total_tokens", 0) or 0
                                    u_tokens = 0
                                    if p_id and p_id in messages_by_id:
                                        parent = messages_by_id[p_id]
                                        u_tokens = parent.get("total_gpt_tokens") or parent.get("total_tokens", 0) or 0
                                    gpt_context_tokens += u_tokens + a_tokens
                        response_stats["gpt_prompts"] = gpt_prompts
                        response_stats["sonnet_prompts"] = sonnet_prompts
                        response_stats["avg_gpt_context_growth"] = gpt_context_tokens / gpt_prompts if gpt_prompts > 0 else 0
                        response_stats["avg_sonnet_context_growth"] = sonnet_context_tokens / sonnet_prompts if sonnet_prompts > 0 else 0

                        # Send done event with all metadata
                        done_data = {
                            'assistant_message': '' if _sex_handoff_detected else assistant_message,
                            'tokens': tokens_str,
                            'cost': cost_str,
                            'stats': response_stats,
                            'context_start_index': context_start_index,
                            'reasoning': reasoning_summary,
                            'user_message_id': user_msg_id,
                            'assistant_message_id': assistant_msg_id,
                            'current_leaf_id': parent_id if _sex_handoff_detected else assistant_msg_id,
                            'total_messages': branch_total_messages,
                            'model': model_id,
                            'hud_state': _hud_for_done_event(assistant_msg_data, data.get('pipeline_state')),
                        }
                        if service_tier:
                            done_data['service_tier'] = service_tier
                        if use_hack_mode:
                            done_data['hack_mode'] = True
                        if use_net_combat_mode:
                            done_data['net_combat_mode'] = True
                        if use_combat_mode:
                            done_data['combat_mode'] = True
                        if use_sex_mode:
                            done_data['sex_mode'] = True
                            if sex_scene_complete:
                                done_data['sex_complete'] = True
                        if use_characters_interview:
                            done_data['_characters_interview_mode'] = True
                        if _sex_handoff_detected:
                            done_data['sex_mode_handoff'] = True
                            done_data['sex_handoff_npcs'] = _sex_handoff_npcs
                        if use_ship_combat_mode:
                            done_data['ship_combat_mode'] = True
                            if ship_combat_started_this_turn:
                                done_data['ship_combat_started'] = True
                                done_data['ship_combat_system_init'] = True
                                if ship_combat_init_hidden_message_data:
                                    done_data['ship_combat_init_message'] = copy.deepcopy(ship_combat_init_hidden_message_data)
                                if ship_combat_opening_narration_hint:
                                    done_data['ship_combat_opening_narration'] = ship_combat_opening_narration_hint
                                    done_data['ship_combat_opening_embedded'] = _ship_opening_embedded(
                                        ship_combat_opening_narration_hint, assistant_message
                                    )
                        if _original_model:
                            done_data['original_model'] = _original_model
                        elif sex_restore_model:
                            done_data['original_model'] = sex_restore_model
                        if use_hack_mode and hack_tool_input and hack_tool_input.get("hack_complete"):
                            done_data['hack_complete'] = True
                        if use_net_combat_mode and net_combat_tool_input:
                            if _is_net_combat_marked_complete(net_combat_tool_input, data.get("pipeline_state", {})):
                                done_data['net_combat_complete'] = True
                        if use_combat_mode and combat_tool_input:
                            if _is_combat_marked_complete(combat_tool_input):
                                done_data['combat_complete'] = True
                        if use_ship_combat_mode and ship_combat_tool_input:
                            if ship_combat_tool_input.get("ship_combat_complete") or ship_combat_tool_input.get("ship_combat") is None:
                                done_data['ship_combat_complete'] = True
                        if artifact_ops:
                            # Include artifact operations summary (without full content) for inline cards
                            done_data['artifact_ops'] = [
                                {k: v for k, v in op.items() if k != 'content'}
                                for op in artifact_ops
                            ]
                            # Include full artifacts dict for panel state
                            done_data['artifacts'] = data.get("artifacts", {})
                        if not client_disconnected:
                            yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                        # Broadcast stream done to other clients
                        stream_done_data = {
                                "assistant_message": assistant_msg_data,
                                "ship_combat_init_message": copy.deepcopy(ship_combat_init_hidden_message_data) if (use_ship_combat_mode and ship_combat_started_this_turn and ship_combat_init_hidden_message_data) else None,
                                "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": parent_id if _sex_handoff_detected else assistant_msg_id,
                            "total_messages": branch_total_messages,
                            "stats": response_stats,
                            "context_start_index": context_start_index,
                            "sex_mode_handoff": True if _sex_handoff_detected else None,
                        }
                        if data.get("pipeline_state"):
                            stream_done_data["pipeline_state"] = data["pipeline_state"]
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(type=SyncEventType.STREAM_DONE, data=stream_done_data)
                        )

                        # Chain ship combat init within same SSE stream if triggered this turn
                        if ship_combat_triggered_this_turn and not use_ship_combat_mode and gs and gs.get("ship_combat_contract") and not client_disconnected:
                            logger.info(f"Chaining ship combat init for {username}")
                            try:
                                # Signal frontend to create a new assistant message placeholder
                                yield f"event: ship_combat_auto_init\ndata: {json.dumps({'parent_id': assistant_msg_id})}\n\n"
                                await sync_manager.broadcast_to_chat(
                                    chat_key,
                                    SyncEvent(type=SyncEventType.SHIP_COMBAT_AUTO_INIT, data={"parent_id": assistant_msg_id})
                                )

                                _chain_result = {}
                                _chain_opening = ((data.get("pipeline_state") or {}).get("ship_combat") or {}).get("opening_narration")
                                _chain_prev_model_id = model_id
                                _chain_prev_provider = provider
                                _chain_prev_client = client
                                _chain_original_model = None
                                _chain_actual_model_id = model_id
                                if model_id.startswith("claude"):
                                    _chain_original_model = model_id
                                    _chain_model_id = COMBAT_AUTO_SWITCH_MODEL
                                    _chain_provider = ProviderRegistry.get(_chain_model_id)
                                    _chain_api_key = get_api_key(username, ProviderRegistry.get_required_api_key(_chain_model_id))
                                    if not _chain_provider or not _chain_api_key:
                                        raise RuntimeError("Ship combat auto-init requires a GPT provider/API key")
                                    model_id = _chain_model_id
                                    _chain_actual_model_id = _chain_model_id
                                    provider = _chain_provider
                                    client = provider.get_client(_chain_api_key)
                                try:
                                    async for sse_event in _run_ship_combat_gpt_exchange(
                                        parent_msg_id=assistant_msg_id,
                                        is_first_exchange=True,
                                        opening_narration_hint=_chain_opening,
                                        result_out=_chain_result,
                                    ):
                                        yield sse_event
                                finally:
                                    model_id = _chain_prev_model_id
                                    provider = _chain_prev_provider
                                    client = _chain_prev_client

                                _chain_assistant_msg_id = _chain_result["assistant_msg_id"]
                                _chain_assistant_msg_data = _chain_result["assistant_msg_data"]
                                _chain_hidden_init = _chain_result.get("hidden_init_data")
                                _chain_branch_path = _chain_result["branch_path_final"]
                                _chain_opening_hint = _chain_result.get("opening_narration_hint")
                                _chain_narrative = _chain_result.get("narrative", "")
                                _chain_sc_json = _chain_result.get("ship_combat_json", {})

                                ship_combat_done_data = {
                                    'assistant_message': _chain_narrative,
                                    'tokens': _chain_result["tokens_str"],
                                    'cost': _chain_result["cost_str"],
                                    'stats': _chain_result["stats"],
                                    'context_start_index': context_start_index,
                                    'reasoning': _chain_result.get("reasoning"),
                                    'user_message_id': user_msg_id,
                                    'assistant_message_id': _chain_assistant_msg_id,
                                    'current_leaf_id': _chain_assistant_msg_id,
                                    'total_messages': len(_chain_branch_path),
                                    'model': _chain_actual_model_id,
                                    'ship_combat_mode': True,
                                    'ship_combat_started': True,
                                    'ship_combat_system_init': True,
                                    'hud_state': _hud_for_done_event(_chain_assistant_msg_data, data.get('pipeline_state')),
                                }
                                if _chain_hidden_init:
                                    ship_combat_done_data['ship_combat_init_message'] = copy.deepcopy(_chain_hidden_init)
                                if _chain_opening_hint:
                                    ship_combat_done_data['ship_combat_opening_narration'] = _chain_opening_hint
                                    ship_combat_done_data['ship_combat_opening_embedded'] = _ship_opening_embedded(
                                        _chain_opening_hint, _chain_narrative
                                    )
                                if _chain_result.get("service_tier"):
                                    ship_combat_done_data['service_tier'] = _chain_result["service_tier"]
                                if _chain_original_model or _original_model:
                                    ship_combat_done_data['original_model'] = _chain_original_model or _original_model
                                if (
                                    isinstance(_chain_sc_json, dict)
                                    and ("ship_combat" in _chain_sc_json)
                                    and (_chain_sc_json.get("ship_combat_complete") or _chain_sc_json.get("ship_combat") is None)
                                ):
                                    ship_combat_done_data['ship_combat_complete'] = True

                                yield f"event: ship_combat_done\ndata: {json.dumps(ship_combat_done_data)}\n\n"

                                await sync_manager.broadcast_to_chat(
                                    chat_key,
                                    SyncEvent(
                                        type=SyncEventType.STREAM_DONE,
                                        data={
                                            "ship_combat_auto_init": True,
                                            "assistant_message": _chain_assistant_msg_data,
                                            "ship_combat_init_message": copy.deepcopy(_chain_hidden_init) if _chain_hidden_init else None,
                                            "user_message_id": user_msg_id,
                                            "assistant_message_id": _chain_assistant_msg_id,
                                            "current_leaf_id": _chain_assistant_msg_id,
                                            "total_messages": len(_chain_branch_path),
                                            "stats": _chain_result["stats"],
                                            "context_start_index": context_start_index,
                                            "pipeline_state": data.get("pipeline_state")
                                        }
                                    )
                                )
                                logger.info(f"Ship combat chained init completed for {username}")
                            except Exception as chain_err:
                                logger.error(f"Ship combat chaining failed for {username}: {chain_err}")
                                _chain_err_detail = f"Ship combat init failed: {chain_err}"
                                yield f"event: ship_combat_error\ndata: {json.dumps({'detail': _chain_err_detail})}\n\n"
                                await sync_manager.broadcast_to_chat(
                                    chat_key,
                                    SyncEvent(type=SyncEventType.STREAM_ERROR, data={"detail": _chain_err_detail, "ship_combat_auto_init": True})
                                )

                logger.info(f"Stream loop completed for user {username}")

        except asyncio.CancelledError:
            # Client disconnected (hard: tab closed) while pipeline/stream was blocking.
            # CancelledError inherits from BaseException, not Exception, so needs its own handler.
            stage_info = f" (stage: {pipeline_current_stage})" if use_pipeline and pipeline_current_stage else ""
            logger.warning(f"Client disconnected (cancelled) for user {username}{stage_info}")
            if accumulated_content:
                # Save partial response
                try:
                    assistant_msg_id = generate_message_id()
                    assistant_msg_data = {
                        "id": assistant_msg_id,
                        "parent_id": user_msg_id,
                        "role": "assistant",
                        "content": accumulated_content,
                        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                        "tokens": "partial",
                        "cost": "unknown",
                        "model": model_id
                    }
                    _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                    data["current_leaf_id"] = assistant_msg_id
                    save_chat(username, request.chat_name, data, request.project)
                    logger.info(f"CancelledError: saved partial response ({len(accumulated_content)} chars) for user {username}")
                except Exception as save_err:
                    logger.error(f"CancelledError: failed to save partial response: {save_err}")
            else:
                # No content yet — keep user message so user can see they sent it and retry
                logger.info(f"CancelledError: no content accumulated, keeping user message for user {username}")
            # Can't yield SSE events or do async broadcasts — connection is dead

        except Exception as e:
            logger.error(f"Streaming error for user {username}: {e}", exc_info=True)

            # Never delete user message — preserve it so user can see what they sent
            if accumulated_content:
                # We got content but done handler failed - save what we have
                logger.info(f"Stream: saving partial response for user {username}")
                try:
                    assistant_msg_id = generate_message_id()
                    assistant_msg_data = {
                        "id": assistant_msg_id,
                        "parent_id": user_msg_id,
                        "role": "assistant",
                        "content": accumulated_content,
                        "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
                        "tokens": "unknown",
                        "cost": "unknown",
                        "model": model_id
                    }
                    _stamp_message_hud_snapshot(assistant_msg_data, data.get("pipeline_state")); data["messages"].append(assistant_msg_data)
                    data["current_leaf_id"] = assistant_msg_id
                    save_chat(username, request.chat_name, data, request.project)
                except Exception as save_err:
                    logger.error(f"Failed to save partial response: {save_err}")

            error_msg = "Failed to get response from AI. Please try again."
            if "api_key" in str(e).lower() or "authentication" in str(e).lower():
                error_msg = "API key error. Please check your API key is valid."
            elif "rate" in str(e).lower() or "limit" in str(e).lower():
                error_msg = "Rate limit exceeded. Please wait a moment and try again."
            elif "timeout" in str(e).lower():
                error_msg = "Request timed out. Please try again."

            yield f"event: error\ndata: {json.dumps({'detail': error_msg})}\n\n"

            # Broadcast error to other clients
            await sync_manager.broadcast_to_chat(
                chat_key,
                SyncEvent(
                    type=SyncEventType.STREAM_ERROR,
                    data={"detail": error_msg}
                )
            )

    # Broadcast user message to WS clients BEFORE starting the SSE stream,
    # so other clients see it immediately (not gated by generator execution)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.USER_MESSAGE_ADDED,
            data={
                "message": user_msg_data,
                "current_leaf_id": user_msg_id
            }
        )
    )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
            "Connection": "keep-alive"
        }
    )


class SiblingInfo(BaseModel):
    id: str
    index: int  # 0-based index among siblings
    total: int  # Total number of siblings


class BranchInfoResponse(BaseModel):
    siblings: list[SiblingInfo]
    current_index: int
    total_siblings: int


@app.get("/api/branch-info/{username}/{chat_name}")
def get_branch_info(username: str, chat_name: str, message_id: str, project: str = None):
    """
    Get sibling information for a message (for branch navigation UI).

    Returns the siblings of the specified message and the current message's
    position among them.
    """
    data = load_chat(username, chat_name, project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    all_messages = data["messages"]

    # Get siblings of the specified message
    siblings = get_siblings(all_messages, message_id)

    if not siblings:
        # Message not found or no siblings
        return BranchInfoResponse(
            siblings=[],
            current_index=0,
            total_siblings=1
        )

    # Build response with sibling info
    sibling_infos = []
    current_index = 0
    for i, sib in enumerate(siblings):
        sibling_infos.append(SiblingInfo(
            id=sib["id"],
            index=i,
            total=len(siblings)
        ))
        if sib["id"] == message_id:
            current_index = i

    return BranchInfoResponse(
        siblings=sibling_infos,
        current_index=current_index,
        total_siblings=len(siblings)
    )


@app.post("/api/switch-branch/{username}/{chat_name}")
async def switch_branch(username: str, chat_name: str, target_message_id: str, project: str = None):
    """
    Switch to a different branch by navigating to a sibling message.

    Updates current_leaf_id to the deepest leaf in the target message's subtree.
    """
    data = load_chat(username, chat_name, project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    all_messages = data["messages"]
    index = build_message_index(all_messages)

    if target_message_id not in index:
        raise HTTPException(status_code=404, detail="Target message not found")

    # Find the deepest leaf in the target message's subtree
    new_leaf_id = get_deepest_leaf(all_messages, target_message_id)

    # Update current_leaf_id
    data["current_leaf_id"] = new_leaf_id

    # Restore pipeline_state from the last assistant message on the target branch
    restored_state = None
    branch_path = get_path_to_root(all_messages, new_leaf_id)
    for msg in reversed(branch_path):
        if msg.get("role") == "assistant" and "pipeline_state_after" in msg:
            restored = msg["pipeline_state_after"]
            if isinstance(restored, str):  # Legacy JSON-string format
                restored = json.loads(restored)
            data["pipeline_state"] = copy.deepcopy(restored)
            restored_state = data["pipeline_state"]
            break

    # Restore hack_state from the target branch's most recent hack snapshot
    restored_hack = None
    for msg in reversed(branch_path):
        if msg.get("role") != "assistant":
            continue
        if "hack_state_after" in msg:
            hs = msg["hack_state_after"]
            if isinstance(hs, dict) and (hs.get("active") or hs.get("narrative_summary")):
                restored_hack = copy.deepcopy(hs)
            break  # Found most recent assistant msg with hack snapshot
        if not msg.get("hack_mode"):
            break  # Hit a non-hack assistant message — no active hack on this branch
    data["hack_state"] = restored_hack

    # Reset trim anchor (meaningless across branches)
    data["_trim_anchor_id"] = None

    save_chat(username, chat_name, data, project)

    # Regenerate debug transcript for the new branch
    try:
        debug_chat_path = get_chat_path(username, chat_name, project)
        generate_debug_transcript(data, debug_chat_path, chat_name)
        generate_plain_transcript(data, debug_chat_path, chat_name)
    except Exception as e:
        logger.warning(f"switch_branch: failed to generate debug transcript: {e}")

    # Broadcast branch switch to other clients
    broadcast_data = {
        "new_leaf_id": new_leaf_id,
        "target_message_id": target_message_id
    }
    if restored_state is not None:
        broadcast_data["pipeline_state"] = restored_state
    if restored_hack is not None:
        broadcast_data["hack_state"] = restored_hack
    chat_key = sync_manager.make_chat_key(username, project, chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BRANCH_SWITCHED,
            data=broadcast_data
        )
    )

    response = {
        "status": "ok",
        "new_leaf_id": new_leaf_id
    }
    if restored_state is not None:
        response["pipeline_state"] = restored_state
    response["hack_state"] = restored_hack
    return response


@app.post("/api/delete-message-pair/{username}/{chat_name}")
async def delete_message_pair(username: str, chat_name: str, message_id: str, project: str = None):
    """
    Delete a user message and everything after it by creating a new branch
    that ends at the previous assistant message.

    For non-first messages: duplicates the previous user+assistant pair into a
    new branch with no children (the deleted message and successors don't exist
    on this branch). The original branch remains accessible via branch navigation.

    For first messages: sets current_leaf_id to None (empty chat view).
    """
    data = load_chat(username, chat_name, project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    all_messages = data["messages"]
    index = build_message_index(all_messages)

    if message_id not in index:
        raise HTTPException(status_code=404, detail="Message not found")

    target_msg = index[message_id]
    if target_msg.get("role") != "user":
        raise HTTPException(status_code=400, detail="Can only delete user messages")

    a_prev_id = target_msg.get("parent_id")

    # --- First message case: no parent ---
    if a_prev_id is None:
        data["current_leaf_id"] = None
        data["pipeline_state"] = None
        data["hack_state"] = None
        data["_trim_anchor_id"] = None
        save_chat(username, chat_name, data, project)
        try:
            debug_chat_path = get_chat_path(username, chat_name, project)
            generate_debug_transcript(data, debug_chat_path, chat_name)
            generate_plain_transcript(data, debug_chat_path, chat_name)
        except Exception as e:
            logger.warning(f"delete_message_pair: failed to generate debug transcript: {e}")

        chat_key = sync_manager.make_chat_key(username, project, chat_name)
        await sync_manager.broadcast_to_chat(
            chat_key,
            SyncEvent(type=SyncEventType.BRANCH_SWITCHED, data={"new_leaf_id": None})
        )
        return {"status": "ok", "new_leaf_id": None, "pipeline_state": None, "hack_state": None}

    # --- Non-first message: duplicate the previous pair into a new branch ---
    a_prev = index.get(a_prev_id)
    if not a_prev:
        raise HTTPException(status_code=400, detail="Previous assistant message not found")

    u_prev_id = a_prev.get("parent_id")
    u_prev = index.get(u_prev_id) if u_prev_id else None
    if not u_prev:
        raise HTTPException(status_code=400, detail="Previous user message not found")

    now_ts = datetime.now(ZoneInfo('America/New_York')).isoformat()

    # Copy previous user message as a sibling (same parent_id)
    # Deep-copy all fields except id/parent_id/timestamp which get new values
    u_prev_copy_id = str(uuid.uuid4())
    u_prev_copy = copy.deepcopy(u_prev)
    u_prev_copy["id"] = u_prev_copy_id
    u_prev_copy["parent_id"] = u_prev.get("parent_id")
    u_prev_copy["timestamp"] = now_ts

    # Copy previous assistant message as child of the copied user message
    a_prev_copy_id = str(uuid.uuid4())
    a_prev_copy = copy.deepcopy(a_prev)
    a_prev_copy["id"] = a_prev_copy_id
    a_prev_copy["parent_id"] = u_prev_copy_id
    a_prev_copy["timestamp"] = now_ts

    all_messages.append(u_prev_copy)
    all_messages.append(a_prev_copy)

    # a_prev_copy has no children — this branch ends here
    data["current_leaf_id"] = a_prev_copy_id

    # Restore pipeline_state from the nearest ancestor with a snapshot
    # (sex/hack/combat mode messages lack pipeline_state_after, so walk back)
    branch_path = get_path_to_root(all_messages, a_prev_copy_id)
    data["pipeline_state"] = None
    for msg in reversed(branch_path):
        if msg.get("role") == "assistant" and "pipeline_state_after" in msg:
            restored = msg["pipeline_state_after"]
            if isinstance(restored, str):
                restored = json.loads(restored)
            data["pipeline_state"] = copy.deepcopy(restored)
            break

    # Restore hack_state from the nearest ancestor with a snapshot
    data["hack_state"] = None
    for msg in reversed(branch_path):
        if msg.get("role") != "assistant":
            continue
        if "hack_state_after" in msg:
            hs = msg["hack_state_after"]
            if isinstance(hs, dict) and (hs.get("active") or hs.get("narrative_summary")):
                data["hack_state"] = copy.deepcopy(hs)
            break

    data["_trim_anchor_id"] = None

    save_chat(username, chat_name, data, project)

    try:
        debug_chat_path = get_chat_path(username, chat_name, project)
        generate_debug_transcript(data, debug_chat_path, chat_name)
        generate_plain_transcript(data, debug_chat_path, chat_name)
    except Exception as e:
        logger.warning(f"delete_message_pair: failed to generate debug transcript: {e}")

    # Broadcast to other clients
    broadcast_data = {"new_leaf_id": a_prev_copy_id}
    if data.get("pipeline_state") is not None:
        broadcast_data["pipeline_state"] = data["pipeline_state"]
    if data.get("hack_state") is not None:
        broadcast_data["hack_state"] = data["hack_state"]
    chat_key = sync_manager.make_chat_key(username, project, chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(type=SyncEventType.BRANCH_SWITCHED, data=broadcast_data)
    )

    response = {
        "status": "ok",
        "new_leaf_id": a_prev_copy_id,
    }
    if data.get("pipeline_state") is not None:
        response["pipeline_state"] = data["pipeline_state"]
    response["hack_state"] = data.get("hack_state")
    return response


class RenameChatRequest(BaseModel):
    username: str
    old_name: str
    new_name: str
    project: str | None = None

class DeleteChatRequest(BaseModel):
    username: str
    chat_name: str
    project: str | None = None

class RenameProjectRequest(BaseModel):
    username: str
    old_name: str
    new_name: str

class DeleteProjectRequest(BaseModel):
    username: str
    project_name: str

class ReloadChatRequest(BaseModel):
    username: str
    chat_name: str
    project: Optional[str] = None

class SaveUpdatesRequest(BaseModel):
    username: str
    chat_name: str
    updates: str
    project: Optional[str] = None

@app.get("/api/updates/{username}/{chat_name}")
def get_updates(username: str, chat_name: str, project: str | None = None):
    """Get the current updates text for a chat"""
    username = username.strip().lower()
    data = load_chat(username, chat_name, project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")
    
    return {"updates": data.get("updates", "")}

@app.post("/api/save-updates")
def save_updates(request: SaveUpdatesRequest):
    """Save updates text for a chat"""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")
    
    data["updates"] = request.updates
    save_chat(username, request.chat_name, data, request.project)
    
    return {"success": True}

class CountTokensRequest(BaseModel):
    text: str

@app.post("/api/count-tokens")
def count_tokens_endpoint(request: CountTokensRequest):
    """Count tokens in text using tiktoken"""
    token_count = count_tokens(request.text)
    return {"tokens": token_count}

class UserStatsResponse(BaseModel):
    lifetime_prompts: int
    lifetime_gpt_prompts: int
    lifetime_sonnet_prompts: int
    lifetime_input_tokens: int
    lifetime_cached_tokens: int
    lifetime_output_tokens: int
    lifetime_reasoning_tokens: int
    lifetime_cost: float
    lifetime_cache_miss_percent: float

    monthly_active_days: int
    monthly_prompts: int
    monthly_gpt_prompts: int
    monthly_sonnet_prompts: int
    monthly_input_tokens: int
    monthly_cached_tokens: int
    monthly_output_tokens: int
    monthly_reasoning_tokens: int
    monthly_total_tokens: int
    monthly_cost: float

    today_prompts: int
    today_gpt_prompts: int
    today_sonnet_prompts: int
    today_input_tokens: int
    today_cached_tokens: int
    today_output_tokens: int
    today_reasoning_tokens: int
    today_total_tokens: int
    today_cost: float

    avg_prompts_per_day: float
    avg_gpt_prompts_per_day: float
    avg_sonnet_prompts_per_day: float
    avg_input_per_day: float
    avg_cached_per_day: float
    avg_output_per_day: float
    avg_reasoning_per_day: float
    avg_total_per_day: float
    avg_cost_per_day: float

    avg_gpt_context_growth: float
    avg_sonnet_context_growth: float

    days_since_first: int

@app.post("/api/rename-chat")
def rename_chat(request: RenameChatRequest):
    username = request.username.strip().lower()

    if not validate_name(request.new_name):
        raise HTTPException(status_code=400, detail="Invalid chat name. Names cannot contain / \\ or control characters.")

    old_path = get_chat_path(username, request.old_name, request.project)
    new_path = get_chat_path(username, request.new_name, request.project)

    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail="Chat not found")

    if os.path.exists(new_path):
        raise HTTPException(status_code=400, detail="A chat with that name already exists")

    # Create backup before rename
    create_backup(username, request.old_name, request.project)

    # Rename JSON file
    os.rename(old_path, new_path)

    # Update chat index: remove old name, add new name with same timestamp
    index = load_chat_index(username, request.project)
    old_entry = index.pop(request.old_name, {"last_accessed": datetime.now(timezone.utc).isoformat()})
    index[request.new_name] = old_entry
    save_chat_index(username, index, request.project)

    return {"status": "ok"}

@app.post("/api/delete-chat")
async def delete_chat(request: DeleteChatRequest):
    username = request.username.strip().lower()
    path = get_chat_path(username, request.chat_name, request.project)

    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Chat not found")

    # Create backup before deletion
    create_backup(username, request.chat_name, request.project)

    # Delete JSON file
    os.remove(path)

    # Remove from chat index
    remove_from_chat_index(username, request.chat_name, request.project)

    # Broadcast chat deletion to all user's connected clients
    await sync_manager.broadcast_to_user(
        username,
        SyncEvent(
            type=SyncEventType.CHAT_DELETED,
            data={
                "chat_name": request.chat_name,
                "project": request.project
            }
        )
    )

    return {"status": "ok"}

@app.post("/api/reload-chat")
def reload_chat(request: ReloadChatRequest):
    """Reload instructions and project files, rebuilding the system prompt"""
    username = request.username.strip().lower()

    # Load the chat
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Look up game system for build_system_content flags
    gs_kwargs = {}
    if request.project:
        proj_meta = load_project_metadata(username, request.project)
        gs = get_game_system(proj_meta.get("game_system", DEFAULT_GAME_SYSTEM))
        gs_kwargs = _system_content_kwargs(gs)

    # Rebuild system message: instructions + project files + base instructions (at end for salience)
    system_content = build_system_content(username, request.project, **gs_kwargs)

    # Update the system message content while preserving other fields (id, parent_id, tokens)
    # This maintains the tree structure and cached token counts
    old_system_msg = data["messages"][0]
    old_system_msg["content"] = system_content
    # Clear cached token counts since content changed - will be recalculated on next message
    old_system_msg.pop("total_tokens", None)
    old_system_msg.pop("total_gpt_tokens", None)
    old_system_msg.pop("total_claude_tokens", None)

    # Save updated chat
    save_chat(username, request.chat_name, data, request.project)

    # Calculate new context_start_index based on updated system message
    model = data.get("model", DEFAULT_MODEL)
    provider = ProviderRegistry.get(model)
    context_limits = provider.context_limits

    # Build the current branch path for context calculation
    current_leaf_id = data.get("current_leaf_id")
    if current_leaf_id:
        branch_path = get_path_to_root(data["messages"], current_leaf_id)
    else:
        branch_path = data["messages"]

    context_start_index = calculate_context_window(
        branch_path,
        threshold=context_limits.target,  # Use target as threshold since no new message
        target=context_limits.target,
        count_tokens_fn=provider.count_tokens if provider else None
    )

    return {"status": "ok", "message": "System prompt reloaded", "context_start_index": context_start_index}

@app.post("/api/rename-project")
def rename_project(request: RenameProjectRequest):
    username = request.username.strip().lower()
    
    if not validate_name(request.new_name):
        raise HTTPException(status_code=400, detail="Invalid project name. Names cannot contain / \\ or control characters.")
    
    old_dir = get_project_dir(username, request.old_name)
    new_dir = get_project_dir(username, request.new_name)
    
    if not os.path.exists(old_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    if os.path.exists(new_dir):
        raise HTTPException(status_code=400, detail="A project with that name already exists")
    
    os.rename(old_dir, new_dir)
    return {"status": "ok"}

@app.post("/api/delete-project")
def delete_project(request: DeleteProjectRequest):
    username = request.username.strip().lower()
    project_dir = get_project_dir(username, request.project_name)
    
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Delete the entire project directory (including all chats, uploads, etc.)
    shutil.rmtree(project_dir)
    return {"status": "ok"}

@app.post("/api/create-project")
def create_project(request: CreateProjectRequest):
    username = request.username.strip().lower()
    project_name = request.project_name.strip()
    
    if not validate_name(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name. Names cannot contain / \\ or control characters.")
    
    if not project_name.replace("-", "").replace("_", "").replace(" ", "").isalnum():
        raise HTTPException(status_code=400, detail="Project name can only contain letters, numbers, spaces, hyphens, and underscores")
    
    project_dir = get_project_dir(username, project_name)
    if os.path.exists(project_dir):
        raise HTTPException(status_code=400, detail="Project already exists")
    
    ensure_project_exists(username, project_name)
    return {"status": "ok"}

@app.get("/api/project-chats/{username}/{project}", response_model=ProjectChatsResponse)
def list_project_chats(username: str, project: str, limit: int = 20, offset: int = 0):
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Update project's last_accessed timestamp
    update_project_last_accessed(username, project)

    # Get actual chat files on disk
    chat_files = set()
    for f in os.listdir(project_dir):
        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
            chat_files.add(f[5:-5])

    # Try to use the index for efficiency
    index = load_chat_index(username, project)

    # Rebuild index if it's missing entries or has stale entries
    indexed_chats = set(index.keys())
    if chat_files != indexed_chats:
        index = rebuild_chat_index(username, project)

    # Build sorted list from index
    chats_with_time = [
        (name, data.get("last_accessed", "1970-01-01T00:00:00"))
        for name, data in index.items()
        if name in chat_files
    ]

    # Sort by last_accessed (most recent first)
    chats_with_time.sort(key=lambda x: x[1], reverse=True)
    all_chats = [name for name, _ in chats_with_time]

    # Apply pagination
    total = len(all_chats)
    paginated_chats = all_chats[offset:offset + limit]
    has_more = (offset + limit) < total
    
    return ProjectChatsResponse(chats=paginated_chats, total=total, has_more=has_more)

@app.get("/api/project-chats-detailed/{username}/{project}", response_model=ProjectChatsDetailedResponse)
def list_project_chats_detailed(username: str, project: str, limit: int = 50, offset: int = 0):
    """
    Get detailed chat summaries for a project in a single request.
    Returns chat name, last message preview, last active time, message count, and cost.
    """
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Update project's last_accessed timestamp
    update_project_last_accessed(username, project)

    # Get detailed info for each chat
    chat_summaries = []
    for f in os.listdir(project_dir):
        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
            chat_name = f[5:-5]  # Remove "chat_" prefix and ".json" suffix
            chat_data = load_chat(username, chat_name, project)
            if chat_data:
                stats = chat_data.get("stats", {})
                last_accessed = stats.get("last_accessed", "1970-01-01T00:00:00")

                # Get last non-system message for preview
                all_messages = chat_data.get("messages", [])
                current_leaf = chat_data.get("current_leaf_id")

                # Get branch messages if migrated, else use linear order
                if current_leaf and is_migrated(chat_data):
                    branch_messages = get_path_to_root(all_messages, current_leaf)
                else:
                    branch_messages = all_messages

                # Find last non-system message
                last_message = ""
                for msg in reversed(branch_messages):
                    if msg.get("role") != "system":
                        content = msg.get("content", "")
                        last_message = content[:100] if content else ""
                        break

                chat_summaries.append({
                    "name": chat_name,
                    "last_message": last_message,
                    "last_active": last_accessed,
                    "message_count": stats.get("total_prompts", 0),
                    "cost": stats.get("total_cost", 0.0)
                })

    # Sort by last_active (most recent first)
    chat_summaries.sort(key=lambda x: x["last_active"], reverse=True)

    # Apply pagination
    total = len(chat_summaries)
    paginated = chat_summaries[offset:offset + limit]
    has_more = (offset + limit) < total

    return ProjectChatsDetailedResponse(
        chats=[ChatSummary(**c) for c in paginated],
        total=total,
        has_more=has_more
    )

@app.get("/api/user-stats/{username}", response_model=UserStatsResponse)
def get_user_stats(username: str):
    """Aggregate statistics across all chats for a user"""
    user_dir = get_user_dir(username)
    
    if not os.path.exists(user_dir):
        raise HTTPException(status_code=404, detail="User not found")
    
    # Load persistent lifetime stats (survives chat deletion)
    lifetime = load_persistent_stats(username)
    total_prompts = lifetime["total_prompts"]
    total_gpt_prompts = lifetime.get("total_gpt_prompts", 0)
    total_sonnet_prompts = lifetime.get("total_sonnet_prompts", 0)
    total_gpt_context_tokens = lifetime.get("total_gpt_context_tokens", 0)
    total_sonnet_context_tokens = lifetime.get("total_sonnet_context_tokens", 0)
    total_input_tokens = lifetime["total_input_tokens"]
    total_cached_tokens = lifetime["total_cached_tokens"]
    total_output_tokens = lifetime["total_output_tokens"]
    total_reasoning_tokens = lifetime.get("total_reasoning_tokens", 0)
    total_cost = lifetime["total_cost"]
    earliest_date = date.fromisoformat(lifetime["first_prompt_date"]) if lifetime["first_prompt_date"] else None

    # Track daily activity for current month (Eastern Time)
    eastern = ZoneInfo('America/New_York')
    today = datetime.now(eastern).date()
    current_month_start = today.replace(day=1)
    monthly_days = set()  # Track which days had activity
    monthly_prompts = 0
    monthly_gpt_prompts = 0
    monthly_sonnet_prompts = 0
    monthly_input = 0
    monthly_cached = 0
    monthly_output = 0
    monthly_reasoning = 0
    monthly_cost = 0.0

    # Track today's activity
    today_prompts = 0
    today_gpt_prompts = 0
    today_sonnet_prompts = 0
    today_input = 0
    today_cached = 0
    today_output = 0
    today_reasoning = 0
    today_cost = 0.0
    
    # Helper to process a chat's stats (only for monthly/today, not lifetime)
    def process_chat(chat_data):
        nonlocal monthly_days, monthly_prompts, monthly_gpt_prompts, monthly_sonnet_prompts
        nonlocal monthly_input, monthly_cached, monthly_output, monthly_reasoning, monthly_cost
        nonlocal today_prompts, today_gpt_prompts, today_sonnet_prompts
        nonlocal today_input, today_cached, today_output, today_reasoning, today_cost

        # Process individual messages to get today/monthly breakdowns
        messages = chat_data.get("messages", [])
        for msg in messages:
            if not msg.get("timestamp"):
                continue
            try:
                msg_date = datetime.fromisoformat(msg["timestamp"]).date()
            except (ValueError, TypeError):
                # Skip messages with malformed timestamps
                continue

            # Track active days
            if msg_date >= current_month_start:
                monthly_days.add(msg_date)

            # Only assistant messages have token/cost data
            if msg.get("role") != "assistant" or not msg.get("tokens"):
                continue

            # Determine model: claude = Sonnet, else = GPT
            model = msg.get("model", "")
            is_sonnet = model.startswith("claude")

            # Parse tokens string like "I:123 C:456 W:789 O:101 R:100 T:1468"
            # (W = cache_creation; legacy strings without W parse fine.)
            tokens_str = msg.get("tokens", "")
            msg_input = msg_cached = msg_cache_write = msg_output = msg_reasoning = 0
            try:
                for part in tokens_str.split():
                    if part.startswith("I:"):
                        msg_input = int(part[2:])
                    elif part.startswith("C:"):
                        msg_cached = int(part[2:])
                    elif part.startswith("W:"):
                        msg_cache_write = int(part[2:])
                    elif part.startswith("O:"):
                        msg_output = int(part[2:])
                    elif part.startswith("R:"):
                        msg_reasoning = int(part[2:])
            except (ValueError, AttributeError):
                # Silently default to 0 for malformed token strings
                msg_input = msg_cached = msg_cache_write = msg_output = msg_reasoning = 0

            # Parse cost string like "$0.0123" or "$0.0123 (free: $0.01)" or "free"
            cost_str = msg.get("cost", "$0")
            msg_cost = 0.0
            if cost_str and cost_str != "free":
                try:
                    # Extract just the first dollar amount
                    cost_part = cost_str.split()[0].replace("$", "")
                    msg_cost = float(cost_part)
                except (ValueError, IndexError, AttributeError):
                    # Silently default to 0 for malformed cost strings
                    pass

            # Add to monthly totals
            if msg_date >= current_month_start:
                monthly_prompts += 1
                if is_sonnet:
                    monthly_sonnet_prompts += 1
                else:
                    monthly_gpt_prompts += 1
                monthly_input += msg_input
                monthly_cached += msg_cached
                monthly_output += msg_output
                monthly_reasoning += msg_reasoning
                monthly_cost += msg_cost

            # Add to today totals
            if msg_date == today:
                today_prompts += 1
                if is_sonnet:
                    today_sonnet_prompts += 1
                else:
                    today_gpt_prompts += 1
                today_input += msg_input
                today_cached += msg_cached
                today_output += msg_output
                today_reasoning += msg_reasoning
                today_cost += msg_cost
    
    # Process root chats
    for f in os.listdir(user_dir):
        if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
            chat_name = f[5:-5]
            chat_data = load_chat(username, chat_name, None)
            if chat_data:
                process_chat(chat_data)

    # Process project chats
    projects_dir = os.path.join(user_dir, "projects")
    if os.path.exists(projects_dir):
        for project_name in os.listdir(projects_dir):
            project_path = os.path.join(projects_dir, project_name)
            if os.path.isdir(project_path):
                for f in os.listdir(project_path):
                    if f.startswith("chat_") and f.endswith(".json") and f != "chat_index.json":
                        chat_name = f[5:-5]
                        chat_data = load_chat(username, chat_name, project_name)
                        if chat_data:
                            process_chat(chat_data)
    
    # Ensure non-negative values (fixes corrupted data from earlier token counting bug)
    total_input_tokens = max(0, total_input_tokens)
    total_cached_tokens = max(0, total_cached_tokens)
    monthly_input = max(0, monthly_input)
    monthly_cached = max(0, monthly_cached)

    # Calculate derived stats
    total_total_tokens = total_input_tokens + total_cached_tokens + total_output_tokens + total_reasoning_tokens
    monthly_total = monthly_input + monthly_cached + monthly_output + monthly_reasoning

    # Cache miss percentage
    total_input_to_api = total_input_tokens + total_cached_tokens
    cache_miss_percent = (total_input_tokens / total_input_to_api * 100) if total_input_to_api > 0 else 0.0
    
    # Calculate days since first prompt (including inactive days)
    if earliest_date:
        days_since_first = (today - earliest_date).days + 1
    else:
        days_since_first = 1
    
    # Calculate daily averages
    avg_prompts = total_prompts / days_since_first
    avg_gpt_prompts = total_gpt_prompts / days_since_first
    avg_sonnet_prompts = total_sonnet_prompts / days_since_first
    avg_input = total_input_tokens / days_since_first
    avg_cached = total_cached_tokens / days_since_first
    avg_output = total_output_tokens / days_since_first
    avg_reasoning = total_reasoning_tokens / days_since_first
    avg_total = total_total_tokens / days_since_first
    avg_cost = total_cost / days_since_first

    # Calculate average context growth per model
    avg_gpt_context = total_gpt_context_tokens / total_gpt_prompts if total_gpt_prompts > 0 else 0.0
    avg_sonnet_context = total_sonnet_context_tokens / total_sonnet_prompts if total_sonnet_prompts > 0 else 0.0
    
    return UserStatsResponse(
        lifetime_prompts=total_prompts,
        lifetime_gpt_prompts=total_gpt_prompts,
        lifetime_sonnet_prompts=total_sonnet_prompts,
        lifetime_input_tokens=total_input_tokens,
        lifetime_cached_tokens=total_cached_tokens,
        lifetime_output_tokens=total_output_tokens,
        lifetime_reasoning_tokens=total_reasoning_tokens,
        lifetime_cost=total_cost,
        lifetime_cache_miss_percent=cache_miss_percent,

        monthly_active_days=len(monthly_days),
        monthly_prompts=monthly_prompts,
        monthly_gpt_prompts=monthly_gpt_prompts,
        monthly_sonnet_prompts=monthly_sonnet_prompts,
        monthly_input_tokens=monthly_input,
        monthly_cached_tokens=monthly_cached,
        monthly_output_tokens=monthly_output,
        monthly_reasoning_tokens=monthly_reasoning,
        monthly_total_tokens=monthly_total,
        monthly_cost=monthly_cost,

        today_prompts=today_prompts,
        today_gpt_prompts=today_gpt_prompts,
        today_sonnet_prompts=today_sonnet_prompts,
        today_input_tokens=today_input,
        today_cached_tokens=today_cached,
        today_output_tokens=today_output,
        today_reasoning_tokens=today_reasoning,
        today_total_tokens=today_input + today_cached + today_output + today_reasoning,
        today_cost=today_cost,

        avg_prompts_per_day=avg_prompts,
        avg_gpt_prompts_per_day=avg_gpt_prompts,
        avg_sonnet_prompts_per_day=avg_sonnet_prompts,
        avg_input_per_day=avg_input,
        avg_cached_per_day=avg_cached,
        avg_output_per_day=avg_output,
        avg_reasoning_per_day=avg_reasoning,
        avg_total_per_day=avg_total,
        avg_cost_per_day=avg_cost,

        avg_gpt_context_growth=avg_gpt_context,
        avg_sonnet_context_growth=avg_sonnet_context,

        days_since_first=days_since_first
    )

@app.get("/api/free-tokens/{username}")
def get_free_tokens(username: str):
    """Get remaining free tokens for today"""
    usage = load_daily_usage(username)
    tokens_used = usage["tokens_used"]
    remaining = max(0, FREE_TOKENS_PER_DAY - tokens_used)

    logger.info(f"get_free_tokens: user={username}, date={usage.get('date')}, used={tokens_used}, remaining={remaining}")

    # Calculate next UTC midnight and convert to Eastern Time
    from datetime import timedelta
    today_utc = datetime.now(ZoneInfo('UTC')).date()
    next_midnight_utc = datetime.combine(today_utc + timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo('UTC'))
    next_midnight_eastern = next_midnight_utc.astimezone(ZoneInfo('America/New_York'))

    return {
        "total_free": FREE_TOKENS_PER_DAY,
        "used": tokens_used,
        "remaining": remaining,
        "resets_at_eastern": next_midnight_eastern.strftime("%I:%M %p %Z")  # e.g., "07:00 PM EST" or "08:00 PM EDT"
    }

# ============================================================
# Project File Management Endpoints
# ============================================================

ALLOWED_FILE_EXTENSIONS = {'.txt', '.md', '.yaml', '.yml'}

@app.get("/api/project-files/{username}/{project}", response_model=ProjectFilesResponse)
def list_project_files(username: str, project: str, model: str = None):
    """List all files in a project's uploads folder with token counts"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Determine which tokenizer to use
    # Priority: query param > project metadata > DEFAULT_MODEL
    if not model:
        metadata = load_project_metadata(username, project)
        model = metadata.get("model", DEFAULT_MODEL)

    provider = ProviderRegistry.get(model)
    use_api_counting = model.startswith("claude")

    # Get API key for Claude API-based counting
    api_key = None
    if use_api_counting:
        api_key = get_api_key(username, "anthropic")

    # Load token cache
    tokens_cache = load_file_tokens_cache(username, project)
    cache_updated = False

    uploads_dir = os.path.join(project_dir, "uploads")

    # Create uploads dir if it doesn't exist
    if not os.path.exists(uploads_dir):
        os.makedirs(uploads_dir)

    files = []
    total_tokens = 0
    staged_tokens = 0

    for filename in sorted(os.listdir(uploads_dir)):
        filepath = os.path.join(uploads_dir, filename)
        if os.path.isfile(filepath):
            # Get file extension
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_FILE_EXTENSIONS:
                continue

            size_bytes = os.path.getsize(filepath)
            mtime = os.path.getmtime(filepath)

            # Check cache for this file and model (include mtime to detect edits)
            cached = tokens_cache.get(filename)
            if (cached and cached.get("model") == model and
                cached.get("size_bytes") == size_bytes and cached.get("mtime") == mtime):
                # Cache hit - use cached token count
                tokens = cached["tokens"]
                # Ensure staged field exists and is a proper boolean (migration for old cache entries)
                if "staged" not in cached or cached.get("staged") is None:
                    cached["staged"] = True
                    cache_updated = True
                staged = cached.get("staged", True)
                # Extra safety: ensure staged is actually True/False
                if staged is not True and staged is not False:
                    staged = True
                    cached["staged"] = True
                    cache_updated = True
            else:
                # Cache miss - need to count tokens
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # Use API counting for Claude, estimator for others
                    if use_api_counting and api_key and hasattr(provider, 'count_tokens_api'):
                        try:
                            tokens = provider.count_tokens_api(content, api_key)
                        except Exception as e:
                            logger.warning(f"API token count failed for {filename}, using estimator: {e}")
                            tokens = provider.count_tokens(content) if provider else count_tokens(content)
                    else:
                        tokens = provider.count_tokens(content) if provider else count_tokens(content)

                    # Update cache (preserve staged if it exists, but ensure it's a proper boolean)
                    existing_staged = cached.get("staged") if cached else None
                    if existing_staged is not True and existing_staged is not False:
                        existing_staged = True
                    existing_agents = cached.get("agents", PIPELINE_AGENT_NAMES) if cached else PIPELINE_AGENT_NAMES
                    tokens_cache[filename] = {
                        "tokens": tokens,
                        "model": model,
                        "size_bytes": size_bytes,
                        "mtime": mtime,
                        "staged": existing_staged,
                        "agents": existing_agents
                    }
                    cache_updated = True
                    staged = existing_staged

                except Exception as e:
                    # Skip files we can't read
                    print(f"Could not read file {filename}: {e}")
                    continue

            agents = cached.get("agents", PIPELINE_AGENT_NAMES) if cached else PIPELINE_AGENT_NAMES
            files.append(ProjectFileInfo(
                filename=filename,
                tokens=tokens,
                size_bytes=size_bytes,
                staged=staged,
                agents=agents
            ))
            total_tokens += tokens
            if staged:
                staged_tokens += tokens

    # Save cache if updated
    if cache_updated:
        save_file_tokens_cache(username, project, tokens_cache)

    return ProjectFilesResponse(files=files, total_tokens=total_tokens, staged_tokens=staged_tokens)

def get_file_tokens_cache_path(username: str, project: str) -> str:
    """Get path to file tokens cache for a project."""
    return os.path.join(get_project_dir(username, project), "file_tokens.json")


def load_file_tokens_cache(username: str, project: str) -> dict:
    """Load cached file token counts."""
    cache_path = get_file_tokens_cache_path(username, project)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_file_tokens_cache(username: str, project: str, cache: dict):
    """Save file token counts to cache."""
    cache_path = get_file_tokens_cache_path(username, project)
    atomic_write_json(cache_path, cache)


@app.post("/api/project-files/{username}/{project}")
async def upload_project_files(username: str, project: str, files: List[UploadFile] = File(...)):
    """Upload one or more files to a project's uploads folder"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    uploads_dir = os.path.join(project_dir, "uploads")

    # Create uploads dir if it doesn't exist
    if not os.path.exists(uploads_dir):
        os.makedirs(uploads_dir)

    # Get project model and API key for accurate Claude token counting
    metadata = load_project_metadata(username, project)
    project_model = metadata.get("model", DEFAULT_MODEL)
    provider = ProviderRegistry.get(project_model)

    # For Claude models, use API-based token counting
    use_api_counting = project_model.startswith("claude")
    api_key = None
    if use_api_counting:
        api_key = get_api_key(username, "anthropic")

    # Load existing token cache
    tokens_cache = load_file_tokens_cache(username, project)

    uploaded = []
    errors = []

    for file in files:
        # Validate filename
        if not file.filename:
            errors.append("Empty filename")
            continue

        # Check extension
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_FILE_EXTENSIONS:
            errors.append(f"{file.filename}: Only .txt, .md, .yaml, and .yml files are allowed")
            continue

        # Validate filename doesn't have path separators
        if '/' in file.filename or '\\' in file.filename:
            errors.append(f"{file.filename}: Invalid filename")
            continue

        # Read file content
        try:
            content = await file.read()
            # Try to decode as UTF-8 text
            text_content = content.decode('utf-8')
        except UnicodeDecodeError:
            errors.append(f"{file.filename}: File must be valid UTF-8 text")
            continue
        except Exception as e:
            errors.append(f"{file.filename}: Could not read file - {str(e)}")
            continue

        # Save file (check if overwriting)
        filepath = os.path.join(uploads_dir, file.filename)
        was_overwritten = os.path.exists(filepath)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(text_content)

            # Count tokens - use API for Claude, estimator for others
            if use_api_counting and api_key and hasattr(provider, 'count_tokens_api'):
                try:
                    tokens = provider.count_tokens_api(text_content, api_key)
                except Exception as e:
                    logger.warning(f"API token count failed for {file.filename}, using estimator: {e}")
                    tokens = provider.count_tokens(text_content) if provider else count_tokens(text_content)
            else:
                tokens = provider.count_tokens(text_content) if provider else count_tokens(text_content)

            # Cache the token count with model info (preserve existing staged/agents on overwrite)
            existing_entry = tokens_cache.get(file.filename, {})
            tokens_cache[file.filename] = {
                "tokens": tokens,
                "model": project_model,
                "size_bytes": len(content),
                "staged": existing_entry.get("staged", True),
                "agents": existing_entry.get("agents", PIPELINE_AGENT_NAMES)
            }

            uploaded.append({
                "filename": file.filename,
                "tokens": tokens,
                "size_bytes": len(content),
                "overwritten": was_overwritten
            })
        except Exception as e:
            errors.append(f"{file.filename}: Could not save file - {str(e)}")
            continue

    # Save updated token cache
    if uploaded:
        save_file_tokens_cache(username, project, tokens_cache)

    return {
        "uploaded": uploaded,
        "errors": errors,
        "total_uploaded": len(uploaded),
        "total_overwritten": sum(1 for f in uploaded if f.get("overwritten", False))
    }

@app.delete("/api/project-files/{username}/{project}/{filename}")
def delete_project_file(username: str, project: str, filename: str):
    """Delete a file from a project's uploads folder"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)
    
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Validate filename doesn't have path separators (security)
    if '/' in filename or '\\' in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    uploads_dir = os.path.join(project_dir, "uploads")
    filepath = os.path.join(uploads_dir, filename)
    
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Make sure it's actually a file (not a directory)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=400, detail="Not a file")
    
    os.remove(filepath)

    # Remove from token cache
    tokens_cache = load_file_tokens_cache(username, project)
    if filename in tokens_cache:
        del tokens_cache[filename]
        save_file_tokens_cache(username, project, tokens_cache)

    return {"status": "ok", "deleted": filename}

class UpdateStagedRequest(BaseModel):
    staged: bool

@app.patch("/api/project-files/{username}/{project}/staged/{filename:path}")
def update_file_staged(username: str, project: str, filename: str, request: UpdateStagedRequest):
    """Update the staged status of a project file"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    uploads_dir = os.path.join(project_dir, "uploads")
    filepath = os.path.join(uploads_dir, filename)

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    # Update the staged field in the token cache
    tokens_cache = load_file_tokens_cache(username, project)

    if filename in tokens_cache:
        tokens_cache[filename]["staged"] = request.staged
    else:
        # File exists but not in cache - add a minimal entry
        tokens_cache[filename] = {"staged": request.staged}

    save_file_tokens_cache(username, project, tokens_cache)

    return {"status": "ok", "filename": filename, "staged": request.staged}

@app.put("/api/project-files/{username}/{project}/agents/{filename:path}")
def update_file_agents(username: str, project: str, filename: str, request: UpdateFileAgentsRequest):
    """Update which pipeline agents a project file is assigned to."""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate agent names
    for agent in request.agents:
        if agent not in PIPELINE_AGENT_NAMES:
            raise HTTPException(status_code=400, detail=f"Invalid agent name: {agent}")

    if not request.agents:
        raise HTTPException(status_code=400, detail="At least one agent is required")

    uploads_dir = os.path.join(project_dir, "uploads")
    filepath = os.path.join(uploads_dir, filename)

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    tokens_cache = load_file_tokens_cache(username, project)

    if filename in tokens_cache:
        tokens_cache[filename]["agents"] = request.agents
    else:
        tokens_cache[filename] = {"agents": request.agents}

    save_file_tokens_cache(username, project, tokens_cache)

    return {"status": "ok", "filename": filename, "agents": request.agents}

def get_instructions_tokens_cache_path(username: str, project: str) -> str:
    """Get path to instructions tokens cache for a project."""
    return os.path.join(get_project_dir(username, project), "instructions_tokens.json")


def load_instructions_tokens_cache(username: str, project: str) -> dict:
    """Load cached instructions token count."""
    cache_path = get_instructions_tokens_cache_path(username, project)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_instructions_tokens_cache(username: str, project: str, cache: dict):
    """Save instructions token count to cache."""
    cache_path = get_instructions_tokens_cache_path(username, project)
    atomic_write_json(cache_path, cache)


@app.get("/api/project-instructions/{username}/{project}", response_model=ProjectInstructionsResponse)
def get_project_instructions(username: str, project: str, model: str = None):
    """Get the instructions.di content for a project"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Determine which tokenizer to use
    # Priority: query param > project metadata > DEFAULT_MODEL
    if not model:
        metadata = load_project_metadata(username, project)
        model = metadata.get("model", DEFAULT_MODEL)

    provider = ProviderRegistry.get(model)
    use_api_counting = model.startswith("claude")

    instructions_path = os.path.join(project_dir, "instructions.di")

    if os.path.exists(instructions_path):
        with open(instructions_path, 'r', encoding='utf-8') as f:
            instructions = f.read()
    else:
        instructions = "You are a helpful assistant."

    # Check cache for token count (use stable hash that persists across restarts)
    content_hash = hashlib.sha256(instructions.encode()).hexdigest()
    cache = load_instructions_tokens_cache(username, project)

    if cache.get("model") == model and cache.get("content_hash") == content_hash:
        # Cache hit
        tokens = cache["tokens"]
    else:
        # Cache miss - count tokens
        if use_api_counting:
            api_key = get_api_key(username, "anthropic")
            if api_key and hasattr(provider, 'count_tokens_api'):
                try:
                    tokens = provider.count_tokens_api(instructions, api_key)
                except Exception as e:
                    logger.warning(f"API token count failed for instructions, using estimator: {e}")
                    tokens = provider.count_tokens(instructions) if provider else count_tokens(instructions)
            else:
                tokens = provider.count_tokens(instructions) if provider else count_tokens(instructions)
        else:
            tokens = provider.count_tokens(instructions) if provider else count_tokens(instructions)

        # Update cache
        save_instructions_tokens_cache(username, project, {
            "tokens": tokens,
            "model": model,
            "content_hash": content_hash
        })

    return ProjectInstructionsResponse(instructions=instructions, tokens=tokens)


@app.put("/api/project-instructions/{username}/{project}")
def update_project_instructions(username: str, project: str, request: UpdateInstructionsRequest):
    """Update the instructions.di content for a project"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Get project model for token counting
    metadata = load_project_metadata(username, project)
    model = metadata.get("model", DEFAULT_MODEL)
    provider = ProviderRegistry.get(model)
    use_api_counting = model.startswith("claude")

    instructions_path = os.path.join(project_dir, "instructions.di")

    with open(instructions_path, 'w', encoding='utf-8') as f:
        f.write(request.instructions)

    # Count tokens using API for Claude, estimator for others
    if use_api_counting:
        api_key = get_api_key(username, "anthropic")
        if api_key and hasattr(provider, 'count_tokens_api'):
            try:
                tokens = provider.count_tokens_api(request.instructions, api_key)
            except Exception as e:
                logger.warning(f"API token count failed for instructions update, using estimator: {e}")
                tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)
        else:
            tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)
    else:
        tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)

    # Update cache (use stable hash that persists across restarts)
    save_instructions_tokens_cache(username, project, {
        "tokens": tokens,
        "model": model,
        "content_hash": hashlib.sha256(request.instructions.encode()).hexdigest()
    })

    return {"status": "ok", "tokens": tokens}


@app.get("/api/project-instructions/{username}/{project}/agents")
def get_agent_instructions(username: str, project: str):
    """Get per-agent instructions for a pipeline project."""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Get project model for token counting
    metadata = load_project_metadata(username, project)
    model = metadata.get("model", DEFAULT_MODEL)
    provider = ProviderRegistry.get(model)
    use_api_counting = model.startswith("claude")

    api_key = None
    if use_api_counting:
        api_key = get_api_key(username, "anthropic")

    cache = load_instructions_tokens_cache(username, project)
    cache_updated = False

    result = {}
    for agent_name in PIPELINE_AGENT_NAMES:
        agent_path = os.path.join(project_dir, f"instructions_{agent_name}.di")
        if os.path.exists(agent_path):
            with open(agent_path, 'r', encoding='utf-8') as f:
                instructions = f.read()
        else:
            instructions = ""

        # Check cache with namespaced key
        cache_key = f"agent_{agent_name}"
        content_hash = hashlib.sha256(instructions.encode()).hexdigest()
        cached = cache.get(cache_key, {})

        if cached.get("model") == model and cached.get("content_hash") == content_hash:
            tokens = cached["tokens"]
        else:
            if instructions.strip():
                if use_api_counting and api_key and hasattr(provider, 'count_tokens_api'):
                    try:
                        tokens = provider.count_tokens_api(instructions, api_key)
                    except Exception:
                        tokens = provider.count_tokens(instructions) if provider else count_tokens(instructions)
                else:
                    tokens = provider.count_tokens(instructions) if provider else count_tokens(instructions)
            else:
                tokens = 0
            cache[cache_key] = {"tokens": tokens, "model": model, "content_hash": content_hash}
            cache_updated = True

        result[agent_name] = {"instructions": instructions, "tokens": tokens}

    if cache_updated:
        save_instructions_tokens_cache(username, project, cache)

    return result


@app.put("/api/project-instructions/{username}/{project}/agents/{agent_name}")
def update_agent_instructions(username: str, project: str, agent_name: str, request: UpdateInstructionsRequest):
    """Update per-agent instructions for a pipeline project."""
    username = username.strip().lower()

    if agent_name not in PIPELINE_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"Invalid agent name: {agent_name}")

    project_dir = get_project_dir(username, project)
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Get project model for token counting
    metadata = load_project_metadata(username, project)
    model = metadata.get("model", DEFAULT_MODEL)
    provider = ProviderRegistry.get(model)
    use_api_counting = model.startswith("claude")

    agent_path = os.path.join(project_dir, f"instructions_{agent_name}.di")
    with open(agent_path, 'w', encoding='utf-8') as f:
        f.write(request.instructions)

    # Count tokens
    if request.instructions.strip():
        if use_api_counting:
            api_key = get_api_key(username, "anthropic")
            if api_key and hasattr(provider, 'count_tokens_api'):
                try:
                    tokens = provider.count_tokens_api(request.instructions, api_key)
                except Exception:
                    tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)
            else:
                tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)
        else:
            tokens = provider.count_tokens(request.instructions) if provider else count_tokens(request.instructions)
    else:
        tokens = 0

    # Update cache with namespaced key
    cache = load_instructions_tokens_cache(username, project)
    cache[f"agent_{agent_name}"] = {
        "tokens": tokens,
        "model": model,
        "content_hash": hashlib.sha256(request.instructions.encode()).hexdigest()
    }
    save_instructions_tokens_cache(username, project, cache)

    return {"status": "ok", "tokens": tokens}


@app.post("/api/set-project-model")
def set_project_model(request: SetProjectModelRequest):
    """Set the default model for a project."""
    username = request.username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    project_dir = get_project_dir(username, request.project)
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    # Validate model exists
    provider = ProviderRegistry.get(request.model)
    if not provider:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model}")

    # Check if user has the required API key
    required_key = ProviderRegistry.get_required_api_key(request.model)
    if not get_api_key(username, required_key):
        raise HTTPException(
            status_code=400,
            detail=f"API key for {required_key} not configured"
        )

    # Update project metadata with new model
    metadata = load_project_metadata(username, request.project)
    metadata["model"] = request.model
    save_project_metadata(username, request.project, metadata)

    return {"status": "ok", "model": request.model}


@app.get("/api/project-metadata/{username}/{project}")
def get_project_metadata_endpoint(username: str, project: str):
    """Get project metadata including default model."""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)

    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    metadata = load_project_metadata(username, project)
    return {
        "last_accessed": metadata.get("last_accessed"),
        "model": metadata.get("model", DEFAULT_MODEL),
        "game_system": metadata.get("game_system", DEFAULT_GAME_SYSTEM)
    }


@app.get("/api/game-systems")
def get_game_systems():
    """Return available game systems for frontend dropdown."""
    return list_game_systems()


@app.post("/api/set-project-game-system")
def set_project_game_system(request: SetProjectGameSystemRequest):
    """Set the game system for a project."""
    from game_systems import GAME_SYSTEMS
    username = request.username.strip().lower()

    if not os.path.exists(get_user_dir(username)):
        raise HTTPException(status_code=404, detail="User not found")

    project_dir = get_project_dir(username, request.project)
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")

    if request.game_system not in GAME_SYSTEMS:
        raise HTTPException(status_code=400, detail=f"Unknown game system: {request.game_system}")

    metadata = load_project_metadata(username, request.project)
    metadata["game_system"] = request.game_system
    save_project_metadata(username, request.project, metadata)

    # Characters projects: auto-seed user_life.di from base_user_life.di if missing.
    # Fires when the user picks Characters in the gamesystem dropdown. Idempotent —
    # won't overwrite an existing user_life.di or run if no base file exists.
    if request.game_system == "characters":
        _maybe_copy_base_user_life(username, project_dir)

    return {"status": "ok", "game_system": request.game_system}


class EndSexSceneRequest(BaseModel):
    username: str
    chat_name: str
    project: str | None = None


@app.post("/api/end-sex-scene")
def end_sex_scene(request: EndSexSceneRequest):
    """Manually end sex mode via /sex command (no args)."""
    username = request.username.strip().lower()
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")

    ps = data.get("pipeline_state") or {}
    sex_scene = ps.get("sex_scene")
    if not sex_scene:
        raise HTTPException(status_code=400, detail="No active sex scene")

    # --- Gather scene info before clearing state ---
    sex_start_id = sex_scene.get("start_message_id") if isinstance(sex_scene, dict) else None
    handoff_summary = sex_scene.get("summary") if isinstance(sex_scene, dict) else None
    npc_names = sex_scene.get("npcs", []) if isinstance(sex_scene, dict) else []

    # Collect scene messages for background summary generation
    scene_messages = []
    if sex_start_id:
        current_leaf = data.get("current_leaf_id")
        branch = get_path_to_root(data.get("messages", []), current_leaf) if current_leaf else data.get("messages", [])
        found_start = False
        for msg in branch:
            if not found_start and msg.get("id") == sex_start_id:
                found_start = True
            if found_start and msg.get("sex_mode"):
                scene_messages.append({"role": msg["role"], "content": msg["content"]})

    # --- Restore model and clear sex scene immediately ---
    restore_model = sex_scene.get("original_model") if isinstance(sex_scene, dict) else None
    if restore_model:
        data["model"] = restore_model
    ps["sex_scene"] = None
    save_chat(username, request.chat_name, data, request.project)

    # --- Generate summary in background thread ---
    if scene_messages:
        api_key = get_api_key(username, "anthropic")
        if api_key:
            chat_name = request.chat_name
            project = request.project
            def _bg_summary():
                try:
                    summary = _generate_sex_scene_summary(
                        api_key, scene_messages, npc_names, handoff_summary
                    )
                    if not summary:
                        return
                    # Re-load chat, stamp summary, re-save
                    bg_data = load_chat(username, chat_name, project)
                    if not bg_data:
                        return
                    leaf = bg_data.get("current_leaf_id")
                    msgs = get_path_to_root(bg_data.get("messages", []), leaf) if leaf else bg_data.get("messages", [])
                    for msg in reversed(msgs):
                        if msg.get("sex_mode") and msg.get("role") == "assistant":
                            msg["sex_scene_summary"] = summary
                            break
                    save_chat(username, chat_name, bg_data, project)
                    logger.info(f"Background sex scene summary saved for {username}/{chat_name}")
                except Exception:
                    logger.exception("Background sex scene summary failed")
            threading.Thread(target=_bg_summary, daemon=True).start()

    return {"status": "ok", "model": data.get("model")}


@app.get("/api/character-sheet/{username}/{project}")
def get_character_sheet(username: str, project: str):
    """Return character sheet file(s) from project uploads with filename metadata."""
    username = username.strip().lower()
    uploads_dir = os.path.join(get_project_dir(username, project), "uploads")
    if not os.path.exists(uploads_dir):
        return {"files": []}

    # Find files matching *character*sheet* (case-insensitive)
    import fnmatch
    matches = [f for f in os.listdir(uploads_dir)
               if fnmatch.fnmatch(f.lower(), "*character*sheet*")]
    if not matches:
        return {"files": []}

    files = []
    real_uploads = os.path.realpath(uploads_dir)
    for filename in sorted(matches):
        filepath = os.path.join(uploads_dir, filename)
        # Ensure resolved path stays within uploads directory
        if not os.path.realpath(filepath).startswith(real_uploads + os.sep):
            continue
        if os.path.isfile(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                files.append({"name": filename, "content": f.read()})
    return {"files": files}


@app.get("/health")
def health_check():
    return {"status": "healthy"}
