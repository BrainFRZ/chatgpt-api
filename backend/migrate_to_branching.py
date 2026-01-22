#!/usr/bin/env python3
"""
Migration script: Convert linear chat format to branching format.

This script adds 'id' and 'parent_id' fields to each message in existing chats,
converting them from a linear array to a tree structure (where linear is just
a tree with no branches).

Safe to run multiple times (idempotent) - skips already-migrated chats.

Usage:
    python migrate_to_branching.py [--dry-run]
"""

import json
import uuid
import argparse
from pathlib import Path
from datetime import datetime

DATA_DIR = Path("/home/chatgpt/data/users")


def migrate_chat(filepath: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Migrate a single chat file to the branching format.

    Returns:
        (migrated: bool, message: str)
    """
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except Exception as e:
        return False, f"Could not read: {e}"

    messages = data.get("messages", [])

    # Skip if already migrated (first message has 'id' field)
    if messages and "id" in messages[0]:
        return False, "Already migrated"

    if not messages:
        return False, "No messages to migrate"

    # Migrate: add id and parent_id to each message
    prev_id = None
    for msg in messages:
        msg["id"] = str(uuid.uuid4())
        msg["parent_id"] = prev_id
        prev_id = msg["id"]

    # Track the current leaf (last message in the linear path)
    data["current_leaf_id"] = messages[-1]["id"] if messages else None

    if dry_run:
        return True, f"Would migrate {len(messages)} messages"

    # Create backup before modifying
    backup_dir = filepath.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{filepath.stem}_pre_branching_{timestamp}.json"

    # Save backup
    with open(backup_path, 'w') as f:
        # Read original file again to preserve exact format
        with open(filepath, 'r') as orig:
            f.write(orig.read())

    # Save migrated file
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)

    return True, f"Migrated {len(messages)} messages (backup: {backup_path.name})"


def migrate_all_chats(dry_run: bool = False) -> dict:
    """
    Migrate all chat files for all users.

    Returns:
        Dictionary with migration statistics
    """
    stats = {
        "total_files": 0,
        "migrated": 0,
        "skipped": 0,
        "errors": 0,
        "details": []
    }

    if not DATA_DIR.exists():
        print(f"Data directory not found: {DATA_DIR}")
        return stats

    # Process all users
    for user_dir in DATA_DIR.iterdir():
        if not user_dir.is_dir():
            continue

        # Process root-level chats
        for chat_file in user_dir.glob("chat_*.json"):
            stats["total_files"] += 1
            migrated, message = migrate_chat(chat_file, dry_run)

            if "error" in message.lower() or "could not" in message.lower():
                stats["errors"] += 1
            elif migrated:
                stats["migrated"] += 1
            else:
                stats["skipped"] += 1

            stats["details"].append({
                "file": str(chat_file.relative_to(DATA_DIR)),
                "migrated": migrated,
                "message": message
            })

        # Process project chats
        projects_dir = user_dir / "projects"
        if projects_dir.exists():
            for project_dir in projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue

                for chat_file in project_dir.glob("chat_*.json"):
                    stats["total_files"] += 1
                    migrated, message = migrate_chat(chat_file, dry_run)

                    if "error" in message.lower() or "could not" in message.lower():
                        stats["errors"] += 1
                    elif migrated:
                        stats["migrated"] += 1
                    else:
                        stats["skipped"] += 1

                    stats["details"].append({
                        "file": str(chat_file.relative_to(DATA_DIR)),
                        "migrated": migrated,
                        "message": message
                    })

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Migrate chat files from linear to branching format"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show details for each file"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("Chat Migration: Linear -> Branching Format")
    print("=" * 60)

    if args.dry_run:
        print("\n*** DRY RUN MODE - No files will be modified ***\n")

    stats = migrate_all_chats(dry_run=args.dry_run)

    # Print results
    if args.verbose:
        print("\nDetails:")
        print("-" * 40)
        for detail in stats["details"]:
            status = "✓" if detail["migrated"] else "·"
            print(f"  {status} {detail['file']}: {detail['message']}")

    print("\n" + "=" * 60)
    print("Summary:")
    print(f"  Total files found: {stats['total_files']}")
    print(f"  Migrated:          {stats['migrated']}")
    print(f"  Skipped:           {stats['skipped']}")
    print(f"  Errors:            {stats['errors']}")
    print("=" * 60)

    if args.dry_run and stats["migrated"] > 0:
        print("\nRun without --dry-run to apply migrations.")


if __name__ == "__main__":
    main()
