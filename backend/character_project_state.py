"""Project-level dynamic state for Characters.

Memories, user_profile, and character_growth already live in jsonl files
in the project dir (see character_storage.py). The remaining dynamic state
— callbacks, wellbeing, arc state, life events — used to live per-chat
in pipeline_state.characters_state, which made multi-chat projects feel
inconsistent (the same character would have different open callbacks in
different chats, different mood today, etc).

This module promotes that shared state to a single project-level JSON file
(`character_state.json`) with atomic-rewrite read/write. Per-chat state
that legitimately stays per-chat (channel, wall_clock) is unaffected.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_STATE_FILENAME = "character_state.json"

# Fields that live project-wide (single shared canonical view across all chats).
PROJECT_LEVEL_FIELDS = ("callbacks", "wellbeing", "arc_state", "life_events")


def _project_state_path(project_dir: str) -> str:
    return os.path.join(project_dir, PROJECT_STATE_FILENAME)


def load_project_state(project_dir: Optional[str]) -> Optional[dict]:
    """Read character_state.json from project_dir. Returns None if missing or
    unreadable. Returns a dict with whatever fields were persisted (subset of
    PROJECT_LEVEL_FIELDS)."""
    if not project_dir:
        return None
    path = _project_state_path(project_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"load_project_state: failed to read {path}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_project_state(project_dir: Optional[str], state: dict) -> bool:
    """Atomically write project-level fields from `state` to character_state.json.
    Returns True on success, False otherwise. Pulls only the fields listed in
    PROJECT_LEVEL_FIELDS out of `state` so caller can pass the merged runtime dict
    without leaking chat-level fields into the file."""
    if not project_dir or not isinstance(state, dict):
        return False
    project_payload = {k: state[k] for k in PROJECT_LEVEL_FIELDS if k in state}
    path = _project_state_path(project_dir)
    try:
        # Atomic rewrite via tempfile in same dir + os.replace.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".character_state.", suffix=".tmp", dir=project_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(project_payload, f, indent=2, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.warning(f"save_project_state: failed to write {path}: {e}")
        return False
    return True


def overlay_project_state_into(characters_state: dict, project_dir: Optional[str]) -> None:
    """Read the project state file and overlay its fields into the in-memory
    characters_state dict. No-op if the file is missing — chat-state defaults
    will continue to apply (so older chats that predate the file just keep
    their existing per-chat state until the next save promotes it to the file).
    """
    if not isinstance(characters_state, dict) or not project_dir:
        return
    file_state = load_project_state(project_dir)
    if not file_state:
        return
    for k in PROJECT_LEVEL_FIELDS:
        if k in file_state:
            characters_state[k] = file_state[k]


def persist_project_state_from(characters_state: dict, project_dir: Optional[str]) -> None:
    """Helper: write project-level fields from `characters_state` to file.
    Wraps save_project_state with the same signature shape callers use."""
    if not isinstance(characters_state, dict):
        return
    save_project_state(project_dir, characters_state)
