r"""Strip printed HUD bracket blocks from chat message content.

Background: prior to the "remove printed HUD" change, the model was instructed to
append a `[Date: ... | Time: ... | Loc: ... | ...]` line at the end of every
narration. The model frequently fabricated wrong values for the time/date,
producing a HUD that didn't match the backend clock. We removed the printed HUD
entirely (date/time/loc now live in the sidebar), but old chat messages still
contain those bracket blocks. This script walks chat JSONs, strips matching
blocks from assistant message content, and (optionally) re-syncs the
chat-level `pipeline_state` to the latest message's `pipeline_state_after`.

Usage:
    python -m scripts.strip_printed_hud --dry-run  "data/users/printer/projects/Dead Air"
    python -m scripts.strip_printed_hud            "data/users/printer/projects/Dead Air"

By default writes a `<chat>.json.bak` next to each modified file. Use
--no-backup to skip backups (not recommended).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Conservative regex: bracket block containing both Date: and Time: with a HHMM-ish value.
# Match the model's variants by allowing arbitrary content between/around the keys.
HUD_RE = re.compile(
    r'\[[^\[\]]*Date:[^\[\]]*Time:[^\[\]]*\]',
    re.DOTALL,
)

# Looser fallback for "Time: ..." brackets that lack a Date: field (rare but seen).
HUD_TIME_ONLY_RE = re.compile(
    r'\[[^\[\]]*Time:\s*\d{3,4}[^\[\]]*\]',
    re.DOTALL,
)


def strip_hud_from_content(content: str) -> tuple[str, int]:
    """Remove HUD bracket blocks from content. Returns (new_content, removed_count)."""
    if not isinstance(content, str) or not content:
        return content, 0
    new, n_main = HUD_RE.subn("", content)
    new, n_fallback = HUD_TIME_ONLY_RE.subn("", new)
    if n_main + n_fallback == 0:
        return content, 0
    # Tidy up: collapse runs of trailing whitespace/blank lines we just created.
    new = re.sub(r"[ \t]+\n", "\n", new)
    new = re.sub(r"\n{3,}", "\n\n", new)
    new = new.rstrip() + ("\n" if content.endswith("\n") else "")
    return new, n_main + n_fallback


def latest_pipeline_state_after(messages: list) -> dict | None:
    """Return a deep copy of the most recent assistant message's pipeline_state_after."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and "pipeline_state_after" in msg:
            psa = msg["pipeline_state_after"]
            if isinstance(psa, str):
                try:
                    psa = json.loads(psa)
                except json.JSONDecodeError:
                    continue
            if isinstance(psa, dict):
                return json.loads(json.dumps(psa))  # cheap deep copy
    return None


def process_chat_file(path: Path, *, dry_run: bool, backup: bool, resync_top_level: bool) -> dict:
    """Strip HUD blocks from one chat JSON. Returns a stats dict."""
    stats = {
        "path": str(path),
        "messages_scanned": 0,
        "messages_modified": 0,
        "blocks_stripped": 0,
        "top_level_resynced": False,
        "wrote": False,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ! skip {path.name}: {e}", file=sys.stderr)
        return stats

    messages = data.get("messages") or []
    if not isinstance(messages, list):
        return stats

    modified = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        stats["messages_scanned"] += 1
        content = msg.get("content", "")
        new_content, n = strip_hud_from_content(content)
        if n > 0:
            msg["content"] = new_content
            stats["messages_modified"] += 1
            stats["blocks_stripped"] += n
            modified = True

    # Re-sync top-level pipeline_state from latest psa if requested.
    if resync_top_level:
        latest_psa = latest_pipeline_state_after(messages)
        if latest_psa is not None:
            top = data.get("pipeline_state")
            if top != latest_psa:
                data["pipeline_state"] = latest_psa
                stats["top_level_resynced"] = True
                modified = True

    if modified and not dry_run:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        stats["wrote"] = True

    return stats


def find_chat_files(target: Path) -> list[Path]:
    """Find chat_*.json files under target (file or directory). Excludes chat_index.json."""
    def is_chat(p: Path) -> bool:
        return (
            p.name.startswith("chat_")
            and p.suffix == ".json"
            and p.name != "chat_index.json"
            and not p.name.endswith(".bak")
            and ".tmp." not in p.name
        )
    if target.is_file():
        return [target] if is_chat(target) else []
    return [p for p in target.rglob("chat_*.json") if is_chat(p)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="Chat JSON files or directories to walk.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    parser.add_argument("--no-backup", action="store_true", help="Skip writing .bak files.")
    parser.add_argument(
        "--no-resync",
        action="store_true",
        help="Skip re-syncing top-level pipeline_state from latest message psa.",
    )
    args = parser.parse_args()

    all_files: list[Path] = []
    for t in args.targets:
        p = Path(t)
        if not p.exists():
            print(f"! not found: {p}", file=sys.stderr)
            continue
        all_files.extend(find_chat_files(p))

    if not all_files:
        print("No chat_*.json files found.", file=sys.stderr)
        return 1

    print(f"{'DRY RUN — ' if args.dry_run else ''}Processing {len(all_files)} chat file(s)\n")

    grand_total = {"files_modified": 0, "messages_modified": 0, "blocks_stripped": 0, "top_level_resynced": 0}
    for path in sorted(all_files):
        s = process_chat_file(
            path,
            dry_run=args.dry_run,
            backup=not args.no_backup,
            resync_top_level=not args.no_resync,
        )
        if s["blocks_stripped"] > 0 or s["top_level_resynced"]:
            print(
                f"  {path.name}: scanned={s['messages_scanned']} "
                f"modified={s['messages_modified']} stripped={s['blocks_stripped']} "
                f"resynced={s['top_level_resynced']}"
            )
            if s["wrote"] or args.dry_run:
                grand_total["files_modified"] += 1
                grand_total["messages_modified"] += s["messages_modified"]
                grand_total["blocks_stripped"] += s["blocks_stripped"]
                if s["top_level_resynced"]:
                    grand_total["top_level_resynced"] += 1

    print(
        f"\n{'Would modify' if args.dry_run else 'Modified'} "
        f"{grand_total['files_modified']} file(s), "
        f"{grand_total['messages_modified']} message(s), "
        f"stripped {grand_total['blocks_stripped']} HUD block(s), "
        f"resynced {grand_total['top_level_resynced']} top-level state(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
