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
import shutil
from datetime import datetime, date
from zoneinfo import ZoneInfo
import tiktoken
import logging
import fcntl
import uuid
import hashlib

# Provider imports
from providers import ProviderRegistry, ModelProvider
from providers.openai_provider import OpenAIProvider, add_updates_to_messages as openai_add_updates
from providers.anthropic_provider import AnthropicProvider, AnthropicOpusProvider, add_updates_to_messages as anthropic_add_updates

# Real-time sync imports
from sync_manager import sync_manager, SyncEvent, SyncEventType

# Pipeline imports
from pipeline import (
    run_pipeline, PipelineResult, generate_debug_transcript,
    SINGLE_AGENT_STATE_CONTRACT, STATE_REPORT_TOOL,
    apply_single_agent_state_updates,
    build_single_agent_injections, migrate_pipeline_state,
    get_context_pairs,
    SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS,
)

# Pipeline agent names (used for per-agent instructions and file routing)
PIPELINE_AGENT_NAMES = ["events", "mechanics", "narration"]

# Configure logging for debugging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

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
ProviderRegistry.register(AnthropicProvider())
ProviderRegistry.register(AnthropicOpusProvider())

# Default model for new chats
DEFAULT_MODEL = "gpt-5.2"

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
    return ProviderRegistry.get("claude-sonnet-4.5") or ProviderRegistry.get("claude-opus-4.6")


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
            file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached_files]
            content = "\n\n".join(file_wrappers) + "\n\n" + content
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
                file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
                content = "\n\n".join(file_wrappers) + "\n\n" + content
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
                file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
                content = "\n\n".join(file_wrappers) + "\n\n" + content
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
    last_accessed = data.get("stats", {}).get("last_accessed", datetime.now().isoformat())
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
                last_accessed = chat_data.get("stats", {}).get("last_accessed", "1970-01-01T00:00:00")
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
        "last_accessed": datetime.now().isoformat()
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

    # Append user-level base instructions if they exist (shared DM rules across all projects)
    if project:
        base_path = os.path.join(get_user_dir(username), "base_instructions.di")
        if os.path.exists(base_path):
            with open(base_path, 'r', encoding='utf-8') as f:
                base = f.read().strip()
            if base:
                instructions = instructions.rstrip() + "\n\n" + base

    return instructions

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
            "last_accessed": datetime.now().isoformat(),
            "model": DEFAULT_MODEL
        })
    
    return is_new

def load_project_files(username: str, project: str) -> str:
    """Load all staged project files from project's uploads folder"""
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

    return combined

def get_project_metadata_path(username: str, project: str) -> str:
    """Get path to project metadata file"""
    return os.path.join(get_project_dir(username, project), "metadata.json")

def load_project_metadata(username: str, project: str) -> dict:
    """Load project metadata (like last_accessed)"""
    metadata_path = get_project_metadata_path(username, project)
    if os.path.exists(metadata_path):
        with open(metadata_path, 'r') as f:
            return json.load(f)
    return {"last_accessed": "1970-01-01T00:00:00"}

def save_project_metadata(username: str, project: str, metadata: dict):
    """Save project metadata atomically."""
    metadata_path = get_project_metadata_path(username, project)
    atomic_write_json(metadata_path, metadata)

def update_project_last_accessed(username: str, project: str):
    """Update project's last_accessed timestamp"""
    metadata = load_project_metadata(username, project)
    metadata["last_accessed"] = datetime.now().isoformat()
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

class ApiKeysResponse(BaseModel):
    """Response showing which API keys are configured."""
    has_openai: bool
    has_anthropic: bool

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
    content: str

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
    has_key = get_api_key(username) is not None
    
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

    save_api_keys(username, keys)

    return ApiKeysResponse(
        has_openai=bool(keys.get("openai")),
        has_anthropic=bool(keys.get("anthropic"))
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
        has_anthropic=bool(keys.get("anthropic"))
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
                    file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
                    content = "\n\n".join(file_wrappers) + "\n\n" + base_content
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

    # Build system message (instructions first, then project files)
    if request.project:
        instructions = get_instructions(username, request.project)
        project_files = load_project_files(username, request.project)
        if project_files:
            system_content = instructions + "\n\n" + project_files
        else:
            system_content = instructions
    else:
        # Free chats also check for instructions
        system_content = get_instructions(username, None)

    data = {
        "messages": [{"role": "system", "content": system_content}],
        "stats": create_empty_stats()
    }

    # Set model: priority is request.model > project.model > DEFAULT_MODEL
    if request.model:
        data["model"] = request.model
    elif request.project:
        # Inherit from project's default model if set
        project_metadata = load_project_metadata(username, request.project)
        if project_metadata.get("model"):
            data["model"] = project_metadata["model"]

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
    data["stats"]["last_accessed"] = datetime.now().isoformat()

    all_messages = data["messages"]
    current_leaf = data.get("current_leaf_id")

    # Determine which leaf to show
    target_leaf = leaf_id or current_leaf

    # Get the branch path (messages from root to target leaf)
    if target_leaf and is_migrated(data):
        branch_messages = get_path_to_root(all_messages, target_leaf)
        # Update current_leaf_id if navigating to a different leaf
        if leaf_id and leaf_id != current_leaf:
            data["current_leaf_id"] = leaf_id
            # Regenerate debug transcript for the new branch
            try:
                debug_chat_path = get_chat_path(username, chat_name, project)
                generate_debug_transcript(data, debug_chat_path, chat_name)
            except Exception:
                pass
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

    return ChatResponse(
        messages=paginated_messages,
        all_messages=all_messages,  # Full tree for branch navigation
        stats=chat_stats,
        total_messages=total_messages,
        has_more_messages=has_more_messages,
        current_leaf_id=data.get("current_leaf_id"),
        model=data.get("model", DEFAULT_MODEL),
        anthropic_sync=data.get("anthropic_sync", True)
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
                    file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
                    content = "\n\n".join(file_wrappers) + "\n\n" + base_content
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
            {"filename": f.filename, "content": f.content}
            for f in request.attached_files
        ]
        # Count tokens for attached files (matching build_message_content format)
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

        # Helper to build message content with FILE wrappers
        def build_message_content(msg):
            content = msg["content"]
            attached = msg.get("attached_files", [])
            if attached:
                file_wrappers = []
                for f in attached:
                    file_wrappers.append(f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====")
                files_text = "\n\n".join(file_wrappers)
                content = f"{files_text}\n\n{content}"
            return content

        # Build messages for API: system + in-context history + new user message
        system_msg = {"role": branch_path[0]["role"], "content": branch_path[0]["content"]}
        history_msgs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in branch_path[context_start_index:-1]]

        # Build user content (with attached files only, no updates yet)
        user_content = build_message_content(branch_path[-1])
        new_user_msg = {"role": branch_path[-1]["role"], "content": user_content}

        # Build messages list
        messages_for_api = [system_msg] + history_msgs + [new_user_msg]

        # Add updates using provider-specific method
        # OpenAI: separate trailing user message (allowed)
        # Claude: concatenate into last user message (no consecutive user messages)
        updates_text = data.get("updates", "").strip()
        if updates_text:
            if model_id.startswith("claude"):
                messages_for_api = anthropic_add_updates(messages_for_api, updates_text)
            else:
                messages_for_api = openai_add_updates(messages_for_api, updates_text)

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
        gpt_provider = ProviderRegistry.get("gpt-5.2")
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

        # Count updates tokens if present (to subtract from native token calculation)
        # Updates are added to API call but not stored with messages, so we need to
        # exclude them from the cached user token count
        # Must count the FULL wrapped text as sent to API, not just raw updates_text
        updates_tokens = 0
        if updates_text:
            if model_id.startswith("claude"):
                # Claude wraps updates and prepends to last user message
                updates_wrapped = f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]\n\n"
                updates_tokens = claude_provider.count_tokens_api(updates_wrapped, api_key)
            else:
                # GPT adds updates as separate message before the user message
                updates_wrapped = f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]"
                updates_tokens = gpt_provider.count_tokens(updates_wrapped)

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
            assistant_gpt_tokens = gpt_provider.count_tokens(assistant_message)
        else:
            # GPT response: We have accurate GPT tokens from API
            # Calculate user message GPT tokens from API response (input - known - updates)
            # Use cached accurate tokens for system and history
            known_tokens = system_msg["total_gpt_tokens"]
            for msg in branch_path[context_start_index:-1]:
                # Prefer model-specific field, fall back to total_tokens
                # Use explicit None check (not `or`) since 0 is a valid token count
                gpt_tokens = msg.get("total_gpt_tokens")
                known_tokens += gpt_tokens if gpt_tokens is not None else (msg.get("total_tokens") or 0)
            # Ensure non-negative (old cached estimates may be inaccurate)
            user_gpt_tokens = max(0, parsed.input_tokens - known_tokens - updates_tokens)

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
        stats["last_accessed"] = datetime.now().isoformat()
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

        data["messages"].append(assistant_msg_data)

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


def _stateful_tool_retry(client, model_name: str, system_content, messages, narrative: str, thinking: str, tool_def: dict):
    """Non-streaming follow-up to force report_state when tool_choice: auto didn't produce it.
    Returns (tool_input_dict_or_None, retry_usage_dict).
    Thinking is included as plain text (not a thinking content block, which would require
    a cryptographic signature we don't have from streaming)."""
    if thinking:
        assistant_text = f"<reasoning>\n{thinking}\n</reasoning>\n\n{narrative}"
    else:
        assistant_text = narrative
    assistant_content = [{"type": "text", "text": assistant_text}]

    retry_messages = list(messages) + [
        {"role": "assistant", "content": assistant_content},
        {"role": "user", "content": "You did not call report_state. Call it now with the state updates for the turn you just wrote."}
    ]
    response = client.messages.create(
        model=model_name,
        max_tokens=4096,
        system=system_content,
        messages=retry_messages,
        tools=[tool_def],
        tool_choice={"type": "tool", "name": tool_def["name"]},
    )
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
        token_field = "total_claude_tokens" if model_id.startswith("claude") else "total_gpt_tokens"

        for msg in data["messages"]:
            if msg.get(token_field) is None:
                base_content = msg.get("content", "")
                attached = msg.get("attached_files", [])
                if attached:
                    file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
                    content = "\n\n".join(file_wrappers) + "\n\n" + base_content
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
            file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
            files_text = "\n\n".join(file_wrappers)
            content = f"{files_text}\n\n{content}"
        return content

    # Check if this is a stateful single-agent request (Claude + project chat, not pipeline)
    use_stateful = model_id.startswith("claude") and request.project and not (model_id == "gpt-5.2")
    stateful_pipeline_state = None
    stateful_injected_snapshot = None
    docs_refreshed = False

    updates_text = data.get("updates", "").strip()

    if use_stateful:
        # Pair-based context trimming (same approach as pipeline agents)
        import copy as _copy
        stateful_pipeline_state = migrate_pipeline_state(_copy.deepcopy(data.get("pipeline_state")))
        stateful_injected_snapshot = json.dumps(stateful_pipeline_state, indent=2)

        # Detect if trimming will fire — refresh system prompt from disk
        history = branch_path[1:-1]
        total_pairs = len(history) // 2
        if total_pairs > SINGLE_AGENT_THRESHOLD_PAIRS:
            docs_refreshed = True
            fresh_instructions = get_instructions(username, request.project)
            fresh_files = load_project_files(username, request.project)
            if fresh_files:
                fresh_system = fresh_instructions + "\n\n" + fresh_files
            else:
                fresh_system = fresh_instructions
            # branch_path[0] is the same dict ref as data["messages"][0] (via get_path_to_root)
            branch_path[0]["content"] = fresh_system
            branch_path[0].pop("total_tokens", None)
            branch_path[0].pop("total_gpt_tokens", None)
            branch_path[0].pop("total_claude_tokens", None)
            logger.info(f"Stateful: refreshed system prompt on context trim for {username}/{request.project}/{request.chat_name}")

        context_pairs = get_context_pairs(branch_path, SINGLE_AGENT_THRESHOLD_PAIRS, SINGLE_AGENT_TARGET_PAIRS)

        # Build injections for user message
        injections_str = build_single_agent_injections(stateful_pipeline_state, updates_text)

        # System prompt: contract + original
        system_content = SINGLE_AGENT_STATE_CONTRACT + "\n\n" + branch_path[0]["content"]
        system_msg = {"role": branch_path[0]["role"], "content": system_content}

        # User message with injections prepended
        user_content = build_message_content(branch_path[-1])
        if injections_str:
            user_content = injections_str + "\n\n" + user_content
        new_user_msg = {"role": "user", "content": user_content}

        messages_for_api = [system_msg] + context_pairs + [new_user_msg]
        # Updates already injected via build_single_agent_injections — don't double-inject
    else:
        system_msg = {"role": branch_path[0]["role"], "content": branch_path[0]["content"]}
        history_msgs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in branch_path[context_start_index:-1]]
        user_content = build_message_content(branch_path[-1])
        new_user_msg = {"role": branch_path[-1]["role"], "content": user_content}

        messages_for_api = [system_msg] + history_msgs + [new_user_msg]

        if updates_text:
            if model_id.startswith("claude"):
                messages_for_api = anthropic_add_updates(messages_for_api, updates_text)
            else:
                messages_for_api = openai_add_updates(messages_for_api, updates_text)

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

    if use_stateful:
        request_params["tools"] = [STATE_REPORT_TOOL]
        # Cannot use forced tool_choice (type: "tool") — incompatible with extended thinking.
        # Auto + strong contract instructions achieves the same result.
        request_params["tool_choice"] = {"type": "auto"}

    async def event_generator():
        accumulated_content = ""
        accumulated_thinking = ""

        try:
            # Send init event with user message ID
            yield f"event: init\ndata: {json.dumps({'user_message_id': user_msg_id})}\n\n"

            # Notify if docs were refreshed on context trim
            if docs_refreshed:
                yield f"event: docs_refreshed\ndata: {json.dumps({'message': 'Instructions and project files refreshed'})}\n\n"
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(type=SyncEventType.DOCS_REFRESHED, data={})
                )

            # Broadcast user message to other clients viewing this chat
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

            # Check if this is a pipeline-eligible request (GPT-5.2 + project chat)
            use_pipeline = model_id == "gpt-5.2" and request.project
            # use_stateful is computed in the outer scope (before event_generator)

            if use_pipeline:
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
                pipeline_state_prev = data.get("pipeline_state")

                pipeline_result = None
                pipeline_current_stage = "starting"

                # Use a sentinel to avoid StopIteration propagation in async generator (PEP 479)
                _PIPELINE_STOP = object()
                def _next_pipeline_event(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return _PIPELINE_STOP

                # Run pipeline in thread pool to avoid blocking the event loop
                # during synchronous API calls (Events/Mechanics stages)
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
                    updates_text=updates_text
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

                # Save pipeline state for next turn
                if pipeline_result.pipeline_state is not None:
                    data["pipeline_state"] = pipeline_result.pipeline_state

                # Get cross-model providers for token counting
                gpt_provider = ProviderRegistry.get("gpt-5.2")
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
                stats["last_accessed"] = datetime.now().isoformat()
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
                if pipeline_result.stage_usage is not None:
                    assistant_msg_data["pipeline_stage_usage"] = pipeline_result.stage_usage

                data["messages"].append(assistant_msg_data)
                data["current_leaf_id"] = assistant_msg_id

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
                    'pipeline_stages': pipeline_result.stages_run
                }
                if not client_disconnected:
                    yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                # Broadcast stream done to other clients
                await sync_manager.broadcast_to_chat(
                    chat_key,
                    SyncEvent(
                        type=SyncEventType.STREAM_DONE,
                        data={
                            "assistant_message": assistant_msg_data,
                            "user_message_id": user_msg_id,
                            "assistant_message_id": assistant_msg_id,
                            "current_leaf_id": assistant_msg_id,
                            "total_messages": branch_total_messages,
                            "stats": response_stats,
                            "context_start_index": context_start_index
                        }
                    )
                )

                logger.info(f"Pipeline: completed for user {username}, stages: {pipeline_result.stages_run}")

            else:
                # ============================================================
                # Standard single-agent path (existing behavior)
                # ============================================================
                event_count = 0
                client_disconnected = False
                if model_id == "gpt-5.2":
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

                        # Extract tool_use input for stateful state updates
                        stateful_tool_input = None
                        if use_stateful and stateful_pipeline_state is not None:
                            tool_input = usage.get('tool_use_input')
                            if tool_input:
                                is_ooc = tool_input.get("is_ooc", False)
                                if not is_ooc:
                                    stateful_pipeline_state["turn_counter"] += 1
                                    current_turn = stateful_pipeline_state["turn_counter"]
                                    apply_single_agent_state_updates(
                                        stateful_pipeline_state, tool_input, current_turn
                                    )
                                    data["pipeline_state"] = stateful_pipeline_state
                                    stateful_tool_input = tool_input
                                    logger.info(f"Stateful: applied tool state updates for user {username}, turn {current_turn}")
                                else:
                                    stateful_tool_input = tool_input
                                    logger.info(f"Stateful: OOC turn (tool is_ooc=true) for user {username}")
                            else:
                                logger.warning(f"Stateful: no tool_use_input, attempting retry for user {username}")
                                try:
                                    retry_result, retry_usage = await asyncio.to_thread(
                                        _stateful_tool_retry,
                                        client, provider.MODEL_NAME,
                                        request_params.get("system", []),
                                        request_params["messages"],
                                        accumulated_content,
                                        accumulated_thinking,
                                        STATE_REPORT_TOOL
                                    )
                                    if retry_usage:
                                        usage['input_tokens'] = usage.get('input_tokens', 0) + retry_usage['input_tokens']
                                        usage['cache_read_tokens'] = usage.get('cache_read_tokens', 0) + retry_usage['cache_read_tokens']
                                        usage['cache_creation_tokens'] = usage.get('cache_creation_tokens', 0) + retry_usage['cache_creation_tokens']
                                        usage['output_tokens'] = usage.get('output_tokens', 0) + retry_usage['output_tokens']
                                    if retry_result:
                                        is_ooc = retry_result.get("is_ooc", False)
                                        if not is_ooc:
                                            stateful_pipeline_state["turn_counter"] += 1
                                            current_turn = stateful_pipeline_state["turn_counter"]
                                            apply_single_agent_state_updates(
                                                stateful_pipeline_state, retry_result, current_turn
                                            )
                                            data["pipeline_state"] = stateful_pipeline_state
                                            stateful_tool_input = retry_result
                                            logger.info(f"Stateful: retry succeeded for user {username}, turn {current_turn}")
                                        else:
                                            stateful_tool_input = retry_result
                                            logger.info(f"Stateful: retry returned OOC for user {username}")
                                    else:
                                        logger.warning(f"Stateful: retry also failed for user {username}")
                                except Exception as retry_err:
                                    logger.error(f"Stateful: retry error for user {username}: {retry_err}")

                        # Use accumulated content as primary (we streamed it), fallback to usage content
                        assistant_message = accumulated_content or usage.get('content') or ''
                        reasoning_summary = accumulated_thinking or usage.get('reasoning')

                        # Get cross-model providers for token counting
                        gpt_provider = ProviderRegistry.get("gpt-5.2")
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

                        # Count updates tokens
                        updates_tokens = 0
                        if updates_text:
                            if model_id.startswith("claude"):
                                updates_wrapped = f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]\n\n"
                                updates_tokens = claude_provider.count_tokens_api(updates_wrapped, api_key)
                            else:
                                updates_wrapped = f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]"
                                updates_tokens = gpt_provider.count_tokens(updates_wrapped)

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

                            assistant_claude_tokens = usage['output_tokens']
                            assistant_gpt_tokens = gpt_provider.count_tokens(assistant_message)
                        else:
                            known_tokens = system_msg_ref.get("total_gpt_tokens", 0)
                            for msg in branch_path[context_start_index:-1]:
                                gpt_tokens = msg.get("total_gpt_tokens")
                                known_tokens += gpt_tokens if gpt_tokens is not None else (msg.get("total_tokens") or 0)
                            user_gpt_tokens = max(0, usage['input_tokens'] - known_tokens - updates_tokens)

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

                            assistant_gpt_tokens = usage['output_tokens']

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
                        if model_id == "gpt-5.2" and service_tier:
                            total_cost = provider.calculate_cost_with_tier(parsed, service_tier)
                        else:
                            total_cost = provider.calculate_cost(parsed)
                        tokens_str = provider.format_token_string(parsed)

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
                        stats["last_accessed"] = datetime.now().isoformat()
                        data["stats"] = stats

                        # Add assistant message
                        assistant_msg_id = generate_message_id()
                        assistant_msg_data = {
                            "id": assistant_msg_id,
                            "parent_id": user_msg_id,
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
                        if use_stateful and stateful_injected_snapshot is not None:
                            assistant_msg_data["pipeline_state_injected"] = stateful_injected_snapshot
                        if use_stateful and stateful_tool_input is not None:
                            assistant_msg_data["state_tool_input"] = stateful_tool_input

                        data["messages"].append(assistant_msg_data)
                        data["current_leaf_id"] = assistant_msg_id

                        save_chat(username, request.chat_name, data, request.project)
                        logger.info(f"Stream: saved chat for user {username}")

                        # Regenerate debug transcript if this is a project chat
                        if request.project:
                            try:
                                debug_chat_path = get_chat_path(username, request.chat_name, request.project)
                                generate_debug_transcript(data, debug_chat_path, request.chat_name)
                            except Exception as e:
                                logger.warning(f"Stream: failed to generate debug transcript: {e}")

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
                            'model': model_id
                        }
                        if service_tier:
                            done_data['service_tier'] = service_tier
                        if not client_disconnected:
                            yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

                        # Broadcast stream done to other clients
                        await sync_manager.broadcast_to_chat(
                            chat_key,
                            SyncEvent(
                                type=SyncEventType.STREAM_DONE,
                                data={
                                    "assistant_message": assistant_msg_data,
                                    "user_message_id": user_msg_id,
                                    "assistant_message_id": assistant_msg_id,
                                    "current_leaf_id": assistant_msg_id,
                                    "total_messages": branch_total_messages,
                                    "stats": response_stats,
                                    "context_start_index": context_start_index
                                }
                            )
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
                    data["messages"].append(assistant_msg_data)
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
                    data["messages"].append(assistant_msg_data)
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
    save_chat(username, chat_name, data, project)

    # Regenerate debug transcript for the new branch
    try:
        debug_chat_path = get_chat_path(username, chat_name, project)
        generate_debug_transcript(data, debug_chat_path, chat_name)
    except Exception as e:
        logger.warning(f"switch_branch: failed to generate debug transcript: {e}")

    # Broadcast branch switch to other clients
    chat_key = sync_manager.make_chat_key(username, project, chat_name)
    await sync_manager.broadcast_to_chat(
        chat_key,
        SyncEvent(
            type=SyncEventType.BRANCH_SWITCHED,
            data={
                "new_leaf_id": new_leaf_id,
                "target_message_id": target_message_id
            }
        )
    )

    return {
        "status": "ok",
        "new_leaf_id": new_leaf_id
    }


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
    old_entry = index.pop(request.old_name, {"last_accessed": datetime.now().isoformat()})
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

    # Rebuild system message (instructions first, then project files)
    if request.project:
        instructions = get_instructions(username, request.project)
        project_files = load_project_files(username, request.project)
        if project_files:
            system_content = instructions + "\n\n" + project_files
        else:
            system_content = instructions
    else:
        system_content = get_instructions(username, None)

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

            # Parse tokens string like "I:123 C:456 O:789 R:100 T:1468"
            tokens_str = msg.get("tokens", "")
            msg_input = msg_cached = msg_output = msg_reasoning = 0
            try:
                for part in tokens_str.split():
                    if part.startswith("I:"):
                        msg_input = int(part[2:])
                    elif part.startswith("C:"):
                        msg_cached = int(part[2:])
                    elif part.startswith("O:"):
                        msg_output = int(part[2:])
                    elif part.startswith("R:"):
                        msg_reasoning = int(part[2:])
            except (ValueError, AttributeError):
                # Silently default to 0 for malformed token strings
                msg_input = msg_cached = msg_output = msg_reasoning = 0

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
        "model": metadata.get("model", DEFAULT_MODEL)
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}