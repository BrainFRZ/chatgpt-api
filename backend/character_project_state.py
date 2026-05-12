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

Concurrency: uses fcntl.flock on POSIX for an exclusive write lock during
saves. Reads are unlocked (the worst case is reading a slightly stale view
which the next save will refresh). This prevents write-tearing across
multiple chats writing the file simultaneously. Note: this does NOT fully
prevent lost-update races where two chats both load → mutate → save in
overlapping turns; with a lock, the second writer's complete payload wins,
so the first writer's mutations to fields the second didn't touch are lost.
For single-user one-chat-at-a-time usage this is a non-issue.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import Optional

try:
    import fcntl  # POSIX
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

logger = logging.getLogger(__name__)

PROJECT_STATE_FILENAME = "character_state.json"

# Fields that live project-wide (single shared canonical view across all chats).
# last_ripeness_rolled_date tracks the daily ripe-callback roll so all chats in
# the project share one roll per ET day rather than each chat re-rolling on its
# own first-message-of-day.
PROJECT_LEVEL_FIELDS = (
    "callbacks",
    "wellbeing",
    "arc_state",
    "flakiness_bands",
    "scheduler_state",
    "last_ripeness_rolled_date",
    "custom_holidays",
    # Rolled/mandatory-free busy cycle state (see character_busy.py). Project-
    # level because Zara is one person — busy state is global across any open
    # chats with her, not per-chat.
    "work_busy_window",
)


@contextlib.contextmanager
def _exclusive_lock(project_dir: str):
    """Acquire an exclusive file lock for the project's character_state write.
    No-op on platforms without fcntl. Lock file is project_dir/.character_state.lock
    so the actual data file isn't held open across the lock duration.

    Syncthing note: the lock file IS in the project dir, which means it will be
    synced like any other file. It's a 0-byte sentinel so the sync cost is
    negligible, but it can occasionally trigger conflict-file creation if two
    devices touch it near-simultaneously. To suppress, add `.character_state.lock`
    to the project's `.stignore` (Syncthing per-folder ignore file)."""
    if not _HAS_FCNTL:
        yield
        return
    lock_path = os.path.join(project_dir, ".character_state.lock")
    fh = None
    try:
        # 'w' creates the lock file if absent; opening for write doesn't truncate
        # data we care about (it's just a sentinel).
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


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
    """Atomically write project-level fields from `state` to character_state.json
    under an exclusive file lock. Returns True on success, False otherwise.
    Pulls only the fields listed in PROJECT_LEVEL_FIELDS out of `state` so
    caller can pass the merged runtime dict without leaking chat-level fields
    into the file."""
    if not project_dir or not isinstance(state, dict):
        return False
    project_payload = {k: state[k] for k in PROJECT_LEVEL_FIELDS if k in state}
    path = _project_state_path(project_dir)
    try:
        with _exclusive_lock(project_dir):
            # Atomic rewrite via tempfile in same dir + os.replace, under the
            # lock so concurrent writers from other chats serialize cleanly.
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
        # P2: this is a real divergence — in-memory state has the new values
        # but disk doesn't. The next overlay will silently revert to the stale
        # file. Log at error level so it surfaces in monitoring; callers don't
        # currently react to a False return, but this is at least visible.
        logger.error(
            f"save_project_state: failed to write {path}: {e} — in-memory "
            f"mutations to {list(project_payload.keys())} will be lost on next overlay"
        )
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
