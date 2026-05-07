"""File-backed storage for character entries.

Why files instead of in-state lists:
- character_memories grow unboundedly over years; in-state caps force evictions
  that silently lose long-tail texture.
- Per-entry depth: an in-state memory caps at ~640 chars. A file-backed memory
  can be 5000 chars (quotes, threading, full context) because it's only loaded
  on demand by the recall agent.
- Inspectable: jsonl files in the project dir can be read/edited directly.

Branching:
- Each entry tags itself with the message_id where it was created (`branch_id`)
  for audit/debugging purposes. As of the cross-chat fix, branch_id is NOT used
  for filtering reads — the character is a project-level entity, and memories
  should be shared across all chats with that character regardless of which
  branch within any chat created them. Within-chat branching is a rare power-
  user feature and accepts memory leakage between sibling branches as a tradeoff
  for the much more common cross-chat use case.
- Migrated entries (from pre-file state) get branch_id=None.
- New entries written by the side agent get branch_id=current user_msg_id.

Storage shape: one jsonl file per kind, lines are full entry JSON.
- character_memories.jsonl
- user_profile.jsonl
- character_growth.jsonl

Atomicity: rewrite() uses write-to-temp + os.replace() to avoid partial reads
during concurrent updates (multi-tab usage). append() is OS-atomic for entries
under PIPE_BUF (typically 4KB) — well above any individual entry's serialized
size — so concurrent appends from different tabs are safe.

There's no separate index file — entries are small enough that the recall
agent can scan the full file each turn at low cost. If files grow past a few
thousand entries we can add an index file later.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

KIND_MEMORIES = "memories"
KIND_USER_PROFILE = "user_profile"
KIND_GROWTH = "growth"

VALID_KINDS = (KIND_MEMORIES, KIND_USER_PROFILE, KIND_GROWTH)

# File-name suffix per kind. character_memories / user_profile / character_growth
# match what the user would expect to see in the project directory.
_KIND_FILE = {
    KIND_MEMORIES: "character_memories.jsonl",
    KIND_USER_PROFILE: "user_profile.jsonl",
    KIND_GROWTH: "character_growth.jsonl",
}


class CharacterStore:
    """Thin jsonl-backed CRUD for character entries. Per-project."""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir

    # ── Path helpers ────────────────────────────────────────────────

    def _path(self, kind: str) -> str:
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown kind: {kind!r}")
        return os.path.join(self.project_dir, _KIND_FILE[kind])

    def exists(self, kind: str) -> bool:
        return os.path.isfile(self._path(kind))

    # ── Read ────────────────────────────────────────────────────────

    def read_all(self, kind: str) -> list[dict]:
        """Read all entries (ignores branch). Empty list if file missing."""
        path = self._path(kind)
        if not os.path.isfile(path):
            return []
        out: list[dict] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            out.append(entry)
                    except json.JSONDecodeError as e:
                        logger.warning(f"character_storage: bad line in {path}: {e}")
                        continue
        except OSError as e:
            logger.error(f"character_storage: failed to read {path}: {e}")
        return out

    def read_filtered(
        self,
        kind: str,
        branch_msg_ids: Optional[set] = None,
        *,
        include_archived: bool = False,
    ) -> list[dict]:
        """Read entries excluding archived by default.

        branch_msg_ids parameter is preserved for API compatibility but no
        longer applies a filter — character memories/profile/growth are project-
        global so they're shared across all chats and branches with that character.
        See module docstring for rationale.

        include_archived: when False (default), entries with archived=True are
        skipped. Hygiene-archived memories are excluded from normal reads, recall
        indices, and rendering — they stay on disk for forensics only.
        """
        out = []
        for e in self.read_all(kind):
            if not include_archived and e.get("archived"):
                continue
            out.append(e)
        return out

    def fetch_by_ids(
        self,
        kind: str,
        ids: list[int],
        branch_msg_ids: Optional[set] = None,
        *,
        include_archived: bool = False,
    ) -> list[dict]:
        """Read entries matching the given ids, optionally branch-filtered. Skips archived by default."""
        if not ids:
            return []
        ids_set = set(ids)
        if branch_msg_ids is not None:
            candidates = self.read_filtered(kind, branch_msg_ids, include_archived=include_archived)
        else:
            candidates = [e for e in self.read_all(kind) if include_archived or not e.get("archived")]
        return [e for e in candidates if e.get("id") in ids_set]

    def bump_last_referenced(self, kind: str, ids: list[int], today_iso: str) -> int:
        """Update last_referenced_date on the given ids. Used by the recall agent
        whenever it surfaces a memory — keeps frequently-recalled memories alive
        through the hygiene pass even if their impact is low."""
        if not ids:
            return 0
        ids_set = set(ids)
        entries = self.read_all(kind)
        bumped = 0
        for e in entries:
            if e.get("id") in ids_set:
                e["last_referenced_date"] = today_iso
                bumped += 1
        if bumped:
            self.rewrite(kind, entries)
        return bumped

    def next_id(self, kind: str) -> int:
        """Next available id (max existing id + 1, starting at 1)."""
        entries = self.read_all(kind)
        if not entries:
            return 1
        return max((e.get("id", 0) for e in entries if isinstance(e.get("id"), int)), default=0) + 1

    # ── Write ───────────────────────────────────────────────────────

    def append(self, kind: str, entry: dict) -> None:
        """Append a single entry."""
        if not isinstance(entry, dict):
            return
        path = self._path(kind)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def append_many(self, kind: str, entries: list[dict]) -> None:
        if not entries:
            return
        path = self._path(kind)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                if isinstance(e, dict):
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def rewrite(self, kind: str, entries: list[dict]) -> None:
        """Replace the entire file with the given entries — atomically.

        Writes to a uniquely-named temp file then uses os.replace() to swap.
        Avoids partial-read hazards if a concurrent reader runs during the
        rewrite (matters for multi-tab usage where multiple requests could
        be writing simultaneously).
        """
        path = self._path(kind)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp_path = f"{path}.tmp.{uuid.uuid4().hex[:8]}"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                for e in entries:
                    if isinstance(e, dict):
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(temp_path, path)  # atomic on POSIX
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def update(self, kind: str, id: int, fields: dict) -> bool:
        """Update fields on the entry with the given id. Returns True if found."""
        if not isinstance(fields, dict):
            return False
        entries = self.read_all(kind)
        found = False
        for e in entries:
            if e.get("id") == id:
                e.update(fields)
                found = True
        if found:
            self.rewrite(kind, entries)
        return found

    def delete(self, kind: str, id: int) -> bool:
        """Hard-delete by id. Returns True if found."""
        entries = self.read_all(kind)
        before = len(entries)
        entries = [e for e in entries if e.get("id") != id]
        if len(entries) != before:
            self.rewrite(kind, entries)
            return True
        return False

    def delete_many(self, kind: str, ids: list[int]) -> int:
        """Hard-delete multiple by id. Returns count deleted."""
        if not ids:
            return 0
        ids_set = set(ids)
        entries = self.read_all(kind)
        kept = [e for e in entries if e.get("id") not in ids_set]
        deleted = len(entries) - len(kept)
        if deleted > 0:
            self.rewrite(kind, kept)
        return deleted

    # ── Always-loaded "core" ────────────────────────────────────────

    def core(self, kind: str, branch_msg_ids: Optional[set] = None, n: int = 8) -> list[dict]:
        """Return the top-N entries that should always be loaded into context.

        Heuristics differ by kind:
        - memories: highest impact, then most recent date. Active memories only
          (no obsolete tier).
        - user_profile: entries flagged core=True first, then most recent.
        - growth: ALL active (non-obsolete) entries returned, regardless of n.
          Growth is the second profile layer; we want it all in context until
          /consolidate prunes it. n is ignored.
        """
        entries = self.read_filtered(kind, branch_msg_ids)
        if kind == KIND_MEMORIES:
            entries = sorted(
                entries,
                key=lambda e: (e.get("impact", 0), e.get("date") or ""),
                reverse=True,
            )
            return entries[:n]
        if kind == KIND_USER_PROFILE:
            core_flagged = [e for e in entries if e.get("core")]
            core_flagged.sort(key=lambda e: e.get("id", 0))
            others = sorted(
                [e for e in entries if not e.get("core")],
                key=lambda e: e.get("added_date") or "",
                reverse=True,
            )
            return (core_flagged + others)[:n]
        # growth
        actives = [e for e in entries if not e.get("obsolete")]
        actives.sort(key=lambda e: e.get("date") or "", reverse=True)
        return actives


# ── Hygiene: archive stale low-impact memories ─────────────────────

def prune_stale_memories(store: CharacterStore, today_iso: str) -> dict:
    """Soft-archive memories that are old, low-impact, and unreferenced.

    Run daily from prepare_state_for_turn (first-message-of-ET-day). Uses a
    no-cost file rewrite — no model calls. Cheap.

    Rules (see CHARACTER_MEMORY_HYGIENE_DAYS / CHARACTER_MEMORY_HYGIENE_MIN_IMPACT
    in game_systems/characters.py):
    - Memory impact >= MIN_IMPACT (default 3): never archived. Permanent.
    - Memory impact < MIN_IMPACT AND age >= HYGIENE_DAYS: archived.
      "Age" measured from last_referenced_date (or `date` if never referenced).
    - Already-archived entries: skipped (don't re-process).

    Archived entries get archived=True + archived_date set, but stay on disk.
    They're excluded from default reads, recall indices, and rendering. Could
    theoretically be unarchived manually by editing the jsonl.
    """
    from game_systems.characters import (
        CHARACTER_MEMORY_HYGIENE_DAYS,
        CHARACTER_MEMORY_HYGIENE_MIN_IMPACT,
        days_between_dates,
    )

    entries = store.read_all(KIND_MEMORIES)
    if not entries:
        return {"archived": 0, "scanned": 0}

    archived_count = 0
    changed = False
    for e in entries:
        if e.get("archived"):
            continue
        try:
            impact = int(e.get("impact", 0))
        except (TypeError, ValueError):
            impact = 0
        if impact >= CHARACTER_MEMORY_HYGIENE_MIN_IMPACT:
            continue
        ref_date = e.get("last_referenced_date") or e.get("date")
        if not ref_date:
            continue
        age = days_between_dates(ref_date, today_iso)
        if age >= CHARACTER_MEMORY_HYGIENE_DAYS:
            e["archived"] = True
            e["archived_date"] = today_iso
            e["archived_reason"] = f"hygiene: impact {impact}, last referenced {age} days ago"
            archived_count += 1
            changed = True

    if changed:
        store.rewrite(KIND_MEMORIES, entries)
        logger.info(f"prune_stale_memories: archived {archived_count} of {len(entries)} memories")

    return {"archived": archived_count, "scanned": len(entries)}


# ── Migration: state → files ────────────────────────────────────────

def migrate_state_to_files(characters_state: dict, store: CharacterStore) -> dict:
    """One-shot migration of legacy in-state entries to file storage.

    Idempotent: if files already exist with content, this is a no-op (we don't
    want to double-migrate). If files are empty/missing AND state has legacy
    entries, copy them to files (with branch_id=None for "always in scope")
    and clear the legacy state lists.

    Returns a dict reporting what was migrated.
    """
    if not isinstance(characters_state, dict):
        return {"migrated": 0}

    report = {"memories": 0, "user_profile": 0, "growth": 0}

    # Memories — was a flat list at state["character_memories"]
    legacy_memories = characters_state.get("character_memories")
    if isinstance(legacy_memories, list) and legacy_memories and not store.exists(KIND_MEMORIES):
        next_id = 1
        rows = []
        for old in legacy_memories:
            if not isinstance(old, dict):
                continue
            row = dict(old)
            if "id" not in row or not isinstance(row.get("id"), int):
                row["id"] = next_id
            next_id = max(next_id, row["id"]) + 1
            row.setdefault("branch_id", None)  # legacy → always-in-scope
            rows.append(row)
        if rows:
            store.append_many(KIND_MEMORIES, rows)
            report["memories"] = len(rows)
        characters_state["character_memories"] = []

    # User_profile — was {next_id, entries} dict
    legacy_profile = characters_state.get("user_profile")
    if isinstance(legacy_profile, dict) and legacy_profile.get("entries") and not store.exists(KIND_USER_PROFILE):
        rows = []
        for old in legacy_profile["entries"]:
            if not isinstance(old, dict):
                continue
            row = dict(old)
            row.setdefault("branch_id", None)
            rows.append(row)
        if rows:
            store.append_many(KIND_USER_PROFILE, rows)
            report["user_profile"] = len(rows)
        characters_state["user_profile"] = {"next_id": 1, "entries": []}

    # Growth — was {next_id, entries} dict
    legacy_growth = characters_state.get("character_growth")
    if isinstance(legacy_growth, dict) and legacy_growth.get("entries") and not store.exists(KIND_GROWTH):
        rows = []
        for old in legacy_growth["entries"]:
            if not isinstance(old, dict):
                continue
            row = dict(old)
            row.setdefault("branch_id", None)
            rows.append(row)
        if rows:
            store.append_many(KIND_GROWTH, rows)
            report["growth"] = len(rows)
        characters_state["character_growth"] = {"next_id": 1, "entries": []}

    if any(v > 0 for v in report.values()):
        logger.info(f"character_storage: migrated state → files: {report}")
    return report


# ── Op application — moves the per-kind apply_*_ops here ───────────

def apply_memory_ops_to_store(
    store: CharacterStore,
    ops: list,
    *,
    current_turn: int,
    today_iso: str,
    branch_msg_id: Optional[str],
    branch_msg_ids: Optional[set] = None,
) -> int:
    """Apply memory ops to the file store.

    Op shapes:
      {action: "add", text, impact: 1-5, quote?, date?, focus?}
      {action: "drop", id: int}

    Returns the count of mutating ops applied.

    NOTE: file storage is unbounded — no tier caps, no total cap. Memories
    accumulate over time. Old, low-impact, never-referenced memories get
    soft-archived by the hygiene pass (see prune_stale_memories) instead.
    """
    if not ops:
        return 0

    from game_systems.characters import _memory_tier

    mutated = 0

    # Handle drops first (they don't depend on adds)
    drop_ids = []
    for op in ops:
        if isinstance(op, dict) and op.get("action") == "drop":
            try:
                drop_ids.append(int(op.get("id")))
            except (TypeError, ValueError):
                continue
    if drop_ids:
        n = store.delete_many(KIND_MEMORIES, drop_ids)
        mutated += n

    # Adds
    new_entries: list[dict] = []
    next_id = store.next_id(KIND_MEMORIES)
    for op in ops:
        if not isinstance(op, dict) or op.get("action") != "add":
            continue
        try:
            impact = int(op.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        impact = max(1, min(5, impact))
        text = str(op.get("text") or "")[:1500]
        if not text:
            continue
        date = op.get("date") or today_iso
        entry = {
            "id": next_id,
            "branch_id": branch_msg_id,
            "text": text,
            "quote": str(op.get("quote") or "")[:300] or None,
            "date": date,
            "impact": impact,
            "tier": _memory_tier(impact),
            "turn_created": current_turn,
            "focus": str(op.get("focus") or "")[:60] or None,
            "last_referenced_date": date,  # bumped by recall agent on each surface
        }
        new_entries.append(entry)
        next_id += 1
    if new_entries:
        store.append_many(KIND_MEMORIES, new_entries)
        mutated += len(new_entries)

    return mutated


def apply_user_profile_ops_to_store(
    store: CharacterStore,
    ops: list,
    *,
    today_iso: str,
    branch_msg_id: Optional[str],
) -> int:
    """Apply user_profile ops. Op shapes match the existing apply_user_profile_ops."""
    if not ops:
        return 0
    mutated = 0

    add_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "add"]
    edit_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "edit"]
    delete_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "delete"]

    if add_ops:
        new_entries = []
        next_id = store.next_id(KIND_USER_PROFILE)
        for op in add_ops:
            text = str(op.get("text") or "")[:800]
            if not text:
                continue
            new_entries.append({
                "id": next_id,
                "branch_id": branch_msg_id,
                "text": text,
                "category": str(op.get("category") or "")[:40] or None,
                "added_date": today_iso,
                "core": bool(op.get("core")),
            })
            next_id += 1
        if new_entries:
            store.append_many(KIND_USER_PROFILE, new_entries)
            mutated += len(new_entries)

    for op in edit_ops:
        try:
            target = int(op.get("id"))
        except (TypeError, ValueError):
            continue
        fields: dict = {}
        if "text" in op:
            fields["text"] = str(op.get("text") or "")[:800]
        if "category" in op:
            fields["category"] = str(op.get("category") or "")[:40] or None
        if "core" in op:
            fields["core"] = bool(op.get("core"))
        if fields and store.update(KIND_USER_PROFILE, target, fields):
            mutated += 1

    for op in delete_ops:
        try:
            target = int(op.get("id"))
        except (TypeError, ValueError):
            continue
        if store.delete(KIND_USER_PROFILE, target):
            mutated += 1

    return mutated


def apply_growth_ops_to_store(
    store: CharacterStore,
    ops: list,
    *,
    today_iso: str,
    branch_msg_id: Optional[str],
) -> int:
    if not ops:
        return 0

    from game_systems.characters import CHARACTER_GROWTH_CATEGORIES

    mutated = 0

    # Adds
    add_ops = [op for op in ops if isinstance(op, dict) and op.get("action") == "add"]
    if add_ops:
        new_entries = []
        next_id = store.next_id(KIND_GROWTH)
        for op in add_ops:
            text = str(op.get("text") or "")[:800]
            if not text:
                continue
            cat = str(op.get("category") or "").lower().strip()
            if cat not in CHARACTER_GROWTH_CATEGORIES:
                cat = "other"
            src = str(op.get("source") or "").lower().strip()
            if src not in ("dialogue", "reflection", "user"):
                src = "dialogue"
            new_entries.append({
                "id": next_id,
                "branch_id": branch_msg_id,
                "date": today_iso,
                "text": text,
                "category": cat,
                "source": src,
            })
            next_id += 1
        if new_entries:
            store.append_many(KIND_GROWTH, new_entries)
            mutated += len(new_entries)

    # Updates / obsolete / delete
    for op in ops:
        if not isinstance(op, dict):
            continue
        action = op.get("action")
        if action == "update":
            try:
                target = int(op.get("id"))
            except (TypeError, ValueError):
                continue
            fields: dict = {"last_updated": today_iso}
            if "text" in op:
                fields["text"] = str(op.get("text") or "")[:800]
            if "category" in op:
                cat = str(op.get("category") or "").lower().strip()
                if cat in CHARACTER_GROWTH_CATEGORIES:
                    fields["category"] = cat
            if store.update(KIND_GROWTH, target, fields):
                mutated += 1
        elif action == "obsolete":
            try:
                target = int(op.get("id"))
            except (TypeError, ValueError):
                continue
            fields = {
                "obsolete": True,
                "obsolete_date": today_iso,
                "obsolete_reason": str(op.get("reason") or "")[:200] or None,
            }
            if store.update(KIND_GROWTH, target, fields):
                mutated += 1
        elif action == "delete":
            try:
                target = int(op.get("id"))
            except (TypeError, ValueError):
                continue
            if store.delete(KIND_GROWTH, target):
                mutated += 1

    return mutated
