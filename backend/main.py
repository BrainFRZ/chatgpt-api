"""
ChatGPT Web Interface - Backend API
"""

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from pathlib import Path
from contextlib import contextmanager
import os
import json
import shutil
from datetime import datetime, date
from zoneinfo import ZoneInfo
import tiktoken
import logging
import fcntl
import uuid

# Provider imports
from providers import ProviderRegistry, ModelProvider
from providers.openai_provider import OpenAIProvider, add_updates_to_messages as openai_add_updates
from providers.anthropic_provider import AnthropicProvider, add_updates_to_messages as anthropic_add_updates

# Configure logging for debugging
logger = logging.getLogger(__name__)

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
    temp_path = path + '.tmp'
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
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
        "total_output_tokens": 0,
        "total_cost": 0.0,
        "first_prompt_date": None
    }

def update_persistent_stats(username: str, input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int, cost: float):
    """Add to lifetime stats (never subtract). Uses file locking for concurrent access safety."""
    path = get_persistent_stats_path(username)

    with file_lock(path):
        stats = load_persistent_stats(username)
        stats["total_prompts"] += 1
        stats["total_input_tokens"] += input_tokens
        stats["total_cached_tokens"] += cached_tokens
        stats["total_output_tokens"] += output_tokens
        stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + reasoning_tokens
        stats["total_cost"] += cost

        # Track first prompt date
        if stats["first_prompt_date"] is None:
            stats["first_prompt_date"] = date.today().isoformat()

        atomic_write_json(path, stats)

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
    target: int = CONTEXT_WINDOW_TARGET
) -> int:
    """
    Calculate context_start_index for rolling context window.

    Returns the index of the first message to include in context (after system).
    Messages structure: [system, msg1, msg2, ..., msgN, new_user_msg]

    Logic:
    - If total tokens <= threshold (275k), include everything (return 1)
    - If over threshold, find cut point to get back to target (225k)
    - Count from newest messages backwards, include as many as fit in target
    - Always cut on user message boundaries (never leave orphaned assistant responses)
    """
    if len(messages) <= 2:
        # Just system + one user message, include everything
        return 1

    # Count system tokens
    system_tokens = count_tokens(messages[0]["content"])

    # Count new user message tokens (last message), including attached files
    last_msg = messages[-1]
    new_user_tokens = last_msg.get("total_tokens")
    if new_user_tokens is None:
        new_user_tokens = count_tokens(last_msg["content"])
        # Add tokens for attached files if present (matching the API call format)
        attached_files = last_msg.get("attached_files", [])
        for f in attached_files:
            wrapper_text = f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====\n\n"
            new_user_tokens += count_tokens(wrapper_text)

    # Base tokens that are always included
    base_tokens = system_tokens + new_user_tokens

    # History is everything except system (index 0) and new user msg (index -1)
    history = messages[1:-1]

    # First pass: count total to see if we exceed threshold
    total_tokens = base_tokens
    for msg in history:
        msg_tokens = msg.get("total_tokens") or count_tokens(msg["content"])
        total_tokens += msg_tokens

    if total_tokens <= threshold:
        # Under threshold, include everything
        return 1

    # We exceed threshold, need to find cut point to get to target
    # Count from newest to oldest until we hit target
    total_tokens = base_tokens
    included_from_end = 0

    for msg in reversed(history):
        msg_tokens = msg.get("total_tokens") or count_tokens(msg["content"])
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
        "total_cost": 0.0,
        "total_prompts": 0,
        "first_prompt_date": datetime.now(ZoneInfo('America/New_York')).date().isoformat(),
        "last_accessed": datetime.now().isoformat()
    }

def get_daily_usage_path(username: str) -> str:
    """Get path to daily usage tracking file"""
    return os.path.join(get_user_dir(username), "daily_usage.json")

def load_daily_usage(username: str) -> dict:
    """Load daily usage data, reset if new day (UTC)"""
    path = get_daily_usage_path(username)
    today_utc = datetime.now(ZoneInfo('UTC')).date().isoformat()
    
    if os.path.exists(path):
        with open(path, 'r') as f:
            data = json.load(f)
            # Reset if new day
            if data.get("date") != today_utc:
                data = {"date": today_utc, "tokens_used": 0}
            return data
    
    return {"date": today_utc, "tokens_used": 0}

def save_daily_usage(username: str, data: dict):
    """Save daily usage data atomically with file locking."""
    path = get_daily_usage_path(username)
    with file_lock(path):
        atomic_write_json(path, data)

def apply_free_tokens(username: str, total_tokens: int, full_cost: float) -> tuple[float, str]:
    """
    Apply free tokens (resets at 0:00 UTC).
    Returns: (actual_cost, cost_display_string)
    """
    # Load current usage
    usage = load_daily_usage(username)
    tokens_used = usage["tokens_used"]
    remaining_free = max(0, FREE_TOKENS_PER_DAY - tokens_used)
    
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
    
    # Update usage
    usage["tokens_used"] = tokens_used + total_tokens
    save_daily_usage(username, usage)
    
    return actual_cost, cost_str

def get_instructions(username: str, project: str = None) -> str:
    if project:
        path = os.path.join(get_user_dir(username), "projects", project, "instructions.di")
    else:
        path = os.path.join(get_user_dir(username), "instructions.di")
    
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read()
    return "You are a helpful assistant."

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
        with open(os.path.join(project_dir, "instructions.di"), 'w') as f:
            f.write("You are a helpful assistant.")
        # Create initial metadata
        save_project_metadata(username, project, {"last_accessed": datetime.now().isoformat()})
    
    return is_new

def load_project_files(username: str, project: str) -> str:
    """Load all .md files from project's uploads folder"""
    uploads_dir = os.path.join(get_project_dir(username, project), "uploads")
    
    if not os.path.exists(uploads_dir):
        return ""
    
    md_files = [f for f in os.listdir(uploads_dir) if f.endswith('.md')]
    if not md_files:
        return ""
    
    combined = ""
    for filename in sorted(md_files):
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
    total_tokens: int | None = None
    attached_files: list[AttachedFile] | None = None  # Files attached to this message

class ChatResponse(BaseModel):
    messages: list[ChatMessage]  # Current branch path (paginated, for display)
    all_messages: list[ChatMessage] | None = None  # Full message tree (for branch navigation)
    stats: dict
    total_messages: int
    has_more_messages: bool
    current_leaf_id: str | None = None  # ID of the current leaf message (for branching)
    model: str | None = None  # Model used for this chat (gpt-5.2 or claude-sonnet-4.5)

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

class ProjectFileInfo(BaseModel):
    filename: str
    tokens: int
    size_bytes: int

class ProjectFilesResponse(BaseModel):
    files: List[ProjectFileInfo]
    total_tokens: int

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
def set_chat_model(request: SetChatModelRequest):
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

    data["model"] = request.model
    save_chat(username, request.chat_name, data, request.project)

    return {"status": "ok", "model": request.model}

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
def create_chat(request: CreateChatRequest):
    username = request.username.strip().lower()
    chat_name = request.chat_name.strip()
    
    if not validate_name(chat_name):
        raise HTTPException(status_code=400, detail="Invalid chat name. Names cannot contain / \\ or control characters.")
    
    path = get_chat_path(username, chat_name, request.project)
    if os.path.exists(path):
        raise HTTPException(status_code=400, detail="Chat already exists")
    
    # Build system message
    if request.project:
        instructions = get_instructions(username, request.project)
        project_files = load_project_files(username, request.project)
        if project_files:
            system_content = instructions + "\n\n" + project_files
        else:
            system_content = instructions
    else:
        # Free chats also check for instructions.di
        system_content = get_instructions(username, None)
    
    data = {
        "messages": [{"role": "system", "content": system_content}],
        "stats": create_empty_stats()
    }

    # Set model if provided (inherit from current selection)
    if request.model:
        data["model"] = request.model

    save_chat(username, chat_name, data, request.project)
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

    return ChatResponse(
        messages=paginated_messages,
        all_messages=all_messages,  # Full tree for branch navigation
        stats=data.get("stats", create_empty_stats()),
        total_messages=total_messages,
        has_more_messages=has_more_messages,
        current_leaf_id=data.get("current_leaf_id"),
        model=data.get("model", DEFAULT_MODEL)
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
    if request.model and request.model != data.get("model"):
        data["model"] = request.model

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
    user_message_tokens = count_tokens(request.message)
    
    # Include attached files if present, and add their tokens to the count
    attached_files_data = None
    if request.attached_files:
        attached_files_data = [
            {"filename": f.filename, "content": f.content}
            for f in request.attached_files
        ]
        # Count tokens for attached files (including the wrapper text)
        for f in request.attached_files:
            wrapper_text = f"====FILE: {f.filename}====\n{f.content}\n====END FILE====\n\n"
            user_message_tokens += count_tokens(wrapper_text)
    
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

        # Calculate context window using provider-specific limits
        context_limits = provider.context_limits
        context_start_index = calculate_context_window(
            branch_path,
            threshold=context_limits.threshold,
            target=context_limits.target
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
        request_params = provider.build_request(
            messages=messages_for_api,
            username=username,
            project=request.project,
            chat_name=request.chat_name,
            is_free_chat=is_free_chat
        )

        # Send request and parse response
        response = provider.send_request(client, request_params)
        parsed = provider.parse_response(response)

        assistant_message = parsed.content
        reasoning_summary = parsed.reasoning

        # Calculate tokens and cost using provider pricing
        new_input_tokens = parsed.input_tokens - parsed.cached_tokens
        total_tokens = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens
        total_cost = provider.calculate_cost(parsed)
        tokens_str = provider.format_token_string(parsed)

        # Apply free tokens only for GPT (resets 0:00 UTC)
        # Claude/Anthropic usage is always billed at full cost
        if model_id.startswith('gpt'):
            actual_cost, cost_str = apply_free_tokens(username, total_tokens, total_cost)
        else:
            actual_cost = total_cost
            cost_str = f"${actual_cost:.6f}"

        # Update stats
        stats = data.get("stats", create_empty_stats())
        stats["total_input_tokens"] += new_input_tokens
        stats["total_cached_tokens"] += parsed.cached_tokens
        stats["total_output_tokens"] += parsed.output_tokens
        stats["total_reasoning_tokens"] = stats.get("total_reasoning_tokens", 0) + parsed.reasoning_tokens
        stats["total_cost"] += actual_cost  # Use actual cost after free tokens
        stats["total_prompts"] += 1
        stats["last_accessed"] = datetime.now().isoformat()
        data["stats"] = stats

        # Update persistent lifetime stats (survives chat deletion)
        update_persistent_stats(username, new_input_tokens, parsed.cached_tokens, parsed.output_tokens, parsed.reasoning_tokens, actual_cost)

        # Add assistant message with branching fields
        assistant_msg_id = generate_message_id()
        assistant_msg_data = {
            "id": assistant_msg_id,
            "parent_id": user_msg_id,  # Assistant is child of user message
            "role": "assistant",
            "content": assistant_message,
            "timestamp": datetime.now(ZoneInfo('America/New_York')).isoformat(),
            "tokens": tokens_str,
            "cost": cost_str,
            "total_tokens": parsed.output_tokens
        }
        if reasoning_summary:
            assistant_msg_data["reasoning"] = reasoning_summary

        data["messages"].append(assistant_msg_data)

        # Update current_leaf_id to the new assistant message
        data["current_leaf_id"] = assistant_msg_id

        save_chat(username, request.chat_name, data, request.project)

        # Calculate total messages in the new branch for frontend pagination
        branch_path = get_path_to_root(data["messages"], assistant_msg_id)
        branch_total_messages = len(branch_path)

        return MessageResponse(
            assistant_message=assistant_message,
            tokens=tokens_str,
            cost=cost_str,
            stats=stats,
            context_start_index=context_start_index,
            reasoning=reasoning_summary,
            user_message_id=user_msg_id,
            assistant_message_id=assistant_msg_id,
            current_leaf_id=assistant_msg_id,
            total_messages=branch_total_messages
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
def switch_branch(username: str, chat_name: str, target_message_id: str, project: str = None):
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
    lifetime_input_tokens: int
    lifetime_cached_tokens: int
    lifetime_output_tokens: int
    lifetime_reasoning_tokens: int
    lifetime_cost: float
    lifetime_cache_miss_percent: float
    
    monthly_active_days: int
    monthly_prompts: int
    monthly_input_tokens: int
    monthly_cached_tokens: int
    monthly_output_tokens: int
    monthly_reasoning_tokens: int
    monthly_total_tokens: int
    monthly_cost: float
    
    today_prompts: int
    today_input_tokens: int
    today_cached_tokens: int
    today_output_tokens: int
    today_reasoning_tokens: int
    today_total_tokens: int
    today_cost: float
    
    avg_prompts_per_day: float
    avg_input_per_day: float
    avg_cached_per_day: float
    avg_output_per_day: float
    avg_reasoning_per_day: float
    avg_total_per_day: float
    avg_cost_per_day: float
    
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
def delete_chat(request: DeleteChatRequest):
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

    return {"status": "ok"}

@app.post("/api/reload-chat")
def reload_chat(request: ReloadChatRequest):
    """Reload instructions and project files, rebuilding the system prompt"""
    username = request.username.strip().lower()
    
    # Load the chat
    data = load_chat(username, request.chat_name, request.project)
    if not data:
        raise HTTPException(status_code=404, detail="Chat not found")
    
    # Rebuild system message with current instructions and files
    if request.project:
        instructions = get_instructions(username, request.project)
        project_files = load_project_files(username, request.project)
        if project_files:
            system_content = instructions + "\n\n" + project_files
        else:
            system_content = instructions
    else:
        system_content = get_instructions(username, None)
    
    # Replace the first message (system message) with rebuilt version
    data["messages"][0] = {"role": "system", "content": system_content}
    
    # Save updated chat
    save_chat(username, request.chat_name, data, request.project)
    
    return {"status": "ok", "message": "System prompt reloaded"}

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
    monthly_input = 0
    monthly_cached = 0
    monthly_output = 0
    monthly_reasoning = 0
    monthly_cost = 0.0
    
    # Track today's activity
    today_prompts = 0
    today_input = 0
    today_cached = 0
    today_output = 0
    today_reasoning = 0
    today_cost = 0.0
    
    # Helper to process a chat's stats (only for monthly/today, not lifetime)
    def process_chat(chat_data):
        nonlocal monthly_days, monthly_prompts, monthly_input, monthly_cached, monthly_output, monthly_reasoning, monthly_cost
        nonlocal today_prompts, today_input, today_cached, today_output, today_reasoning, today_cost
        
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
            
            # Parse tokens string like "I:123 C:456 O:789 R:100 T:1468"
            tokens_str = msg.get("tokens", "")
            msg_input = msg_cached = msg_output = msg_reasoning = 0
            for part in tokens_str.split():
                if part.startswith("I:"):
                    msg_input = int(part[2:])
                elif part.startswith("C:"):
                    msg_cached = int(part[2:])
                elif part.startswith("O:"):
                    msg_output = int(part[2:])
                elif part.startswith("R:"):
                    msg_reasoning = int(part[2:])
            
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
                monthly_input += msg_input
                monthly_cached += msg_cached
                monthly_output += msg_output
                monthly_reasoning += msg_reasoning
                monthly_cost += msg_cost
            
            # Add to today totals
            if msg_date == today:
                today_prompts += 1
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
    avg_input = total_input_tokens / days_since_first
    avg_cached = total_cached_tokens / days_since_first
    avg_output = total_output_tokens / days_since_first
    avg_reasoning = total_reasoning_tokens / days_since_first
    avg_total = total_total_tokens / days_since_first
    avg_cost = total_cost / days_since_first
    
    return UserStatsResponse(
        lifetime_prompts=total_prompts,
        lifetime_input_tokens=total_input_tokens,
        lifetime_cached_tokens=total_cached_tokens,
        lifetime_output_tokens=total_output_tokens,
        lifetime_reasoning_tokens=total_reasoning_tokens,
        lifetime_cost=total_cost,
        lifetime_cache_miss_percent=cache_miss_percent,
        
        monthly_active_days=len(monthly_days),
        monthly_prompts=monthly_prompts,
        monthly_input_tokens=monthly_input,
        monthly_cached_tokens=monthly_cached,
        monthly_output_tokens=monthly_output,
        monthly_reasoning_tokens=monthly_reasoning,
        monthly_total_tokens=monthly_total,
        monthly_cost=monthly_cost,
        
        today_prompts=today_prompts,
        today_input_tokens=today_input,
        today_cached_tokens=today_cached,
        today_output_tokens=today_output,
        today_reasoning_tokens=today_reasoning,
        today_total_tokens=today_input + today_cached + today_output + today_reasoning,
        today_cost=today_cost,
        
        avg_prompts_per_day=avg_prompts,
        avg_input_per_day=avg_input,
        avg_cached_per_day=avg_cached,
        avg_output_per_day=avg_output,
        avg_reasoning_per_day=avg_reasoning,
        avg_total_per_day=avg_total,
        avg_cost_per_day=avg_cost,
        
        days_since_first=days_since_first
    )

@app.get("/api/free-tokens/{username}")
def get_free_tokens(username: str):
    """Get remaining free tokens for today"""
    usage = load_daily_usage(username)
    tokens_used = usage["tokens_used"]
    remaining = max(0, FREE_TOKENS_PER_DAY - tokens_used)
    
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

ALLOWED_FILE_EXTENSIONS = {'.txt', '.md'}

@app.get("/api/project-files/{username}/{project}", response_model=ProjectFilesResponse)
def list_project_files(username: str, project: str):
    """List all files in a project's uploads folder with token counts"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)
    
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    uploads_dir = os.path.join(project_dir, "uploads")
    
    # Create uploads dir if it doesn't exist
    if not os.path.exists(uploads_dir):
        os.makedirs(uploads_dir)
    
    files = []
    total_tokens = 0
    
    for filename in sorted(os.listdir(uploads_dir)):
        filepath = os.path.join(uploads_dir, filename)
        if os.path.isfile(filepath):
            # Get file extension
            ext = os.path.splitext(filename)[1].lower()
            if ext not in ALLOWED_FILE_EXTENSIONS:
                continue
            
            # Read file and count tokens
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                tokens = count_tokens(content)
                size_bytes = os.path.getsize(filepath)
                
                files.append(ProjectFileInfo(
                    filename=filename,
                    tokens=tokens,
                    size_bytes=size_bytes
                ))
                total_tokens += tokens
            except Exception as e:
                # Skip files we can't read
                print(f"Could not read file {filename}: {e}")
                continue
    
    return ProjectFilesResponse(files=files, total_tokens=total_tokens)

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
            errors.append(f"{file.filename}: Only .txt and .md files are allowed")
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

            tokens = count_tokens(text_content)
            uploaded.append({
                "filename": file.filename,
                "tokens": tokens,
                "size_bytes": len(content),
                "overwritten": was_overwritten
            })
        except Exception as e:
            errors.append(f"{file.filename}: Could not save file - {str(e)}")
            continue

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
    return {"status": "ok", "deleted": filename}

@app.get("/api/project-instructions/{username}/{project}", response_model=ProjectInstructionsResponse)
def get_project_instructions(username: str, project: str):
    """Get the instructions.di content for a project"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)
    
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    instructions_path = os.path.join(project_dir, "instructions.di")
    
    if os.path.exists(instructions_path):
        with open(instructions_path, 'r', encoding='utf-8') as f:
            instructions = f.read()
    else:
        instructions = "You are a helpful assistant."
    
    tokens = count_tokens(instructions)
    
    return ProjectInstructionsResponse(instructions=instructions, tokens=tokens)

@app.put("/api/project-instructions/{username}/{project}")
def update_project_instructions(username: str, project: str, request: UpdateInstructionsRequest):
    """Update the instructions.di content for a project"""
    username = username.strip().lower()
    project_dir = get_project_dir(username, project)
    
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    
    instructions_path = os.path.join(project_dir, "instructions.di")
    
    with open(instructions_path, 'w', encoding='utf-8') as f:
        f.write(request.instructions)
    
    tokens = count_tokens(request.instructions)
    
    return {"status": "ok", "tokens": tokens}


@app.get("/health")
def health_check():
    return {"status": "healthy"}