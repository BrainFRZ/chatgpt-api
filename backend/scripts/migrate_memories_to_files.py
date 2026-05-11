"""One-shot migration: split character_memories.jsonl into index + body files.

Before: each line in character_memories.jsonl was a full memory entry with
`text` containing the prose.

After: each line is an index entry (no `text`), gaining `hook` (one-line
summary) + `body_file` (relative path). The prose moves to
`memories/<focus_slug>_<id:04d>.md` as a markdown file with YAML-ish
frontmatter for human browsing.

Idempotent: entries that already have `body_file` are skipped. Run multiple
times safely.

Safety:
- Snapshots the pre-migration jsonl into the project's backups/ dir before
  touching anything.
- Uses CharacterStore's atomic-rewrite, so concurrent reads during migration
  never see a partial file.
- --dry-run prints what would change without writing.

Run from the backend directory:
    cd backend && py scripts/migrate_memories_to_files.py
    cd backend && py scripts/migrate_memories_to_files.py --dry-run
    cd backend && py scripts/migrate_memories_to_files.py --project "data/users/printer/projects/Zara Chang"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from character_storage import (  # noqa: E402
    CharacterStore,
    KIND_MEMORIES,
    MEMORIES_BODY_DIR,
    _build_memory_md,
    _derive_hook,
    _memory_body_filename,
)


def discover_character_projects(data_root: str) -> list[str]:
    """Walk data/users/*/projects/* looking for those with a character_profile.di."""
    found: list[str] = []
    users_root = os.path.join(data_root, "users")
    if not os.path.isdir(users_root):
        return found
    for user in sorted(os.listdir(users_root)):
        projects_dir = os.path.join(users_root, user, "projects")
        if not os.path.isdir(projects_dir):
            continue
        for project in sorted(os.listdir(projects_dir)):
            pdir = os.path.join(projects_dir, project)
            if os.path.isfile(os.path.join(pdir, "character_profile.di")):
                found.append(pdir)
    return found


def migrate_one(project_dir: str, *, dry_run: bool = False) -> dict:
    """Migrate one project's character_memories.jsonl to the split-storage format.

    Returns a report dict: {project, scanned, migrated, skipped_already, errors}.
    """
    report = {
        "project": project_dir,
        "scanned": 0,
        "migrated": 0,
        "skipped_already": 0,
        "errors": 0,
    }
    store = CharacterStore(project_dir)
    if not store.exists(KIND_MEMORIES):
        return report

    entries = store.read_all(KIND_MEMORIES)
    report["scanned"] = len(entries)
    if not entries:
        return report

    needs_migration = [e for e in entries if isinstance(e, dict) and "text" in e and "body_file" not in e]
    if not needs_migration:
        report["skipped_already"] = len(entries)
        return report

    if dry_run:
        for e in needs_migration:
            mid = e.get("id")
            fname = _memory_body_filename(e)
            print(f"  [dry-run] would migrate id={mid} -> memories/{fname}")
        report["migrated"] = len(needs_migration)
        return report

    # Snapshot pre-migration jsonl into backups/<timestamp>_character_memories.jsonl
    backups_dir = os.path.join(project_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = os.path.join(backups_dir, f"{stamp}_character_memories.jsonl.pre_split")
    try:
        shutil.copy2(store._path(KIND_MEMORIES), snapshot_path)
        print(f"  snapshot -> {snapshot_path}")
    except OSError as e:
        print(f"  ERROR snapshotting: {e}")
        report["errors"] += 1
        return report

    # Migrate each entry: write body file, rewrite index line in place.
    upgraded: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            upgraded.append(e)
            continue
        if "body_file" in e or "text" not in e:
            # Already migrated or has nothing to migrate
            upgraded.append(e)
            continue
        body_text = e.get("text") or ""
        new_entry = dict(e)
        if not new_entry.get("hook"):
            new_entry["hook"] = _derive_hook(body_text)
        # Write body file. _write_memory_body uses entry's id + focus to build path.
        try:
            rel = store._write_memory_body(new_entry, body_text)
            new_entry["body_file"] = rel
            new_entry.pop("text", None)
            upgraded.append(new_entry)
            report["migrated"] += 1
            print(f"  migrated id={new_entry.get('id')} -> {rel}")
        except OSError as err:
            print(f"  ERROR writing body for id={e.get('id')}: {err}")
            report["errors"] += 1
            upgraded.append(e)  # keep legacy form rather than losing the entry

    store.rewrite(KIND_MEMORIES, upgraded)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; don't write.")
    parser.add_argument("--project", help="Migrate only the specified project dir (absolute or relative path).")
    parser.add_argument(
        "--data-root",
        default=os.path.join(os.path.dirname(BACKEND), "data"),
        help="Root data directory (default: ../data).",
    )
    args = parser.parse_args()

    if args.project:
        projects = [os.path.abspath(args.project)]
    else:
        projects = discover_character_projects(args.data_root)

    if not projects:
        print("No Characters projects found.")
        return 0

    print(f"Found {len(projects)} project(s):")
    for p in projects:
        print(f"  {p}")
    print()

    total = {"scanned": 0, "migrated": 0, "skipped_already": 0, "errors": 0}
    for pdir in projects:
        print(f"Project: {pdir}")
        rep = migrate_one(pdir, dry_run=args.dry_run)
        for k in ("scanned", "migrated", "skipped_already", "errors"):
            total[k] += rep[k]
        print(f"  -> scanned={rep['scanned']} migrated={rep['migrated']} "
              f"already={rep['skipped_already']} errors={rep['errors']}")
        print()

    print(f"Total: scanned={total['scanned']} migrated={total['migrated']} "
          f"already={total['skipped_already']} errors={total['errors']}")
    return 1 if total["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
