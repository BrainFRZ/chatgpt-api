#!/usr/bin/env python3
"""
Migration script to fix total_tokens on assistant messages.

The bug: total_tokens was set to output_tokens (including reasoning),
but should be text_output_tokens (excluding reasoning).

This script parses the O:XXX value from the tokens string and updates
total_tokens to match.

Usage:
    python fix_total_tokens.py /path/to/chats/directory
    python fix_total_tokens.py /path/to/specific/chat.json

Examples:
    python fix_total_tokens.py "/home/chatgpt/data/users/printer/projects/Stellar Drift"
    python fix_total_tokens.py "/home/chatgpt/data/users/printer/projects/Stellar Drift/mychat.json"
"""

import json
import re
import sys
from pathlib import Path


def fix_chat_file(filepath: Path) -> tuple[int, int]:
    """Fix total_tokens in a chat file. Returns (messages_checked, messages_fixed)."""

    with open(filepath, 'r') as f:
        data = json.load(f)

    messages = data.get("messages", [])
    checked = 0
    fixed = 0

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        tokens_str = msg.get("tokens", "")
        total_tokens = msg.get("total_tokens")

        if not tokens_str or total_tokens is None:
            continue

        checked += 1

        # Parse O:XXX from tokens string like "I:1021 C:74368 O:585 R:82 T:76056"
        match = re.search(r'O:(\d+)', tokens_str)
        if not match:
            continue

        correct_total = int(match.group(1))

        if total_tokens != correct_total:
            msg["total_tokens"] = correct_total
            fixed += 1

    if fixed > 0:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    return checked, fixed


def main():
    if len(sys.argv) < 2:
        print("Usage: python fix_total_tokens.py <path>")
        print("  <path> can be a directory containing .json chat files, or a single .json file")
        sys.exit(1)

    target = Path(sys.argv[1])

    if not target.exists():
        print(f"Path not found: {target}")
        sys.exit(1)

    total_checked = 0
    total_fixed = 0
    files_modified = 0

    if target.is_file() and target.suffix == '.json':
        # Single file
        checked, fixed = fix_chat_file(target)
        total_checked += checked
        total_fixed += fixed
        if fixed > 0:
            files_modified += 1
            print(f"Fixed {fixed} messages in {target}")
    elif target.is_dir():
        # Directory - process all .json files
        for chat_file in target.glob("*.json"):
            checked, fixed = fix_chat_file(chat_file)
            total_checked += checked
            total_fixed += fixed
            if fixed > 0:
                files_modified += 1
                print(f"Fixed {fixed} messages in {chat_file}")
    else:
        print(f"Invalid path: {target}")
        sys.exit(1)

    print(f"\nDone! Checked {total_checked} assistant messages, fixed {total_fixed} in {files_modified} files.")


if __name__ == "__main__":
    main()
