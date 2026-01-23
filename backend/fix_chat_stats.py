#!/usr/bin/env python3
"""
Diagnostic and repair script for chat stats.

This script verifies that chat stats match the sum of individual message token values.
If there's a mismatch, it recalculates the stats from the messages.

Usage:
    python fix_chat_stats.py /path/to/chat.json           # Check and fix single chat
    python fix_chat_stats.py /path/to/chats/directory     # Check and fix all chats in directory
    python fix_chat_stats.py /path/to/chat.json --dry-run # Check only, don't modify

Examples:
    python fix_chat_stats.py "/home/chatgpt/data/users/printer/chat_mychat.json"
    python fix_chat_stats.py "/home/chatgpt/data/users/printer/projects/MyProject" --dry-run
"""

import json
import re
import sys
from pathlib import Path


def parse_tokens_string(tokens_str: str) -> dict:
    """Parse a tokens string like 'I:1021 C:74368 O:585 R:82 T:76056' into a dict."""
    result = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}

    if not tokens_str:
        return result

    for part in tokens_str.split():
        if part.startswith("I:"):
            result["input"] = int(part[2:])
        elif part.startswith("C:"):
            result["cached"] = int(part[2:])
        elif part.startswith("O:"):
            result["output"] = int(part[2:])
        elif part.startswith("R:"):
            result["reasoning"] = int(part[2:])
        elif part.startswith("T:"):
            result["total"] = int(part[2:])

    return result


def calculate_stats_from_messages(messages: list) -> dict:
    """Calculate stats by summing token values from all assistant messages."""
    stats = {
        "total_input_tokens": 0,
        "total_cached_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": 0,
        "total_prompts": 0,
    }

    for msg in messages:
        if msg.get("role") != "assistant":
            continue

        tokens_str = msg.get("tokens", "")
        if not tokens_str:
            continue

        parsed = parse_tokens_string(tokens_str)
        stats["total_input_tokens"] += parsed["input"]
        stats["total_cached_tokens"] += parsed["cached"]
        stats["total_output_tokens"] += parsed["output"]
        stats["total_reasoning_tokens"] += parsed["reasoning"]
        stats["total_prompts"] += 1

    return stats


def verify_chat_stats(filepath: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Verify and optionally fix chat stats.

    Returns (was_fixed, report_string)
    """
    with open(filepath, 'r') as f:
        data = json.load(f)

    messages = data.get("messages", [])
    stored_stats = data.get("stats", {})

    # Calculate what the stats should be based on messages
    calculated = calculate_stats_from_messages(messages)

    # Compare
    discrepancies = []

    fields = [
        ("total_input_tokens", "Input tokens"),
        ("total_cached_tokens", "Cached tokens"),
        ("total_output_tokens", "Output tokens"),
        ("total_reasoning_tokens", "Reasoning tokens"),
        ("total_prompts", "Prompts"),
    ]

    for field, label in fields:
        stored_val = stored_stats.get(field, 0)
        calc_val = calculated[field]
        if stored_val != calc_val:
            discrepancies.append(f"  {label}: stored={stored_val:,} vs calculated={calc_val:,} (diff={stored_val - calc_val:+,})")

    if not discrepancies:
        return False, f"{filepath.name}: OK (prompts={calculated['total_prompts']})"

    # Build report
    report_lines = [f"{filepath.name}: MISMATCH FOUND"]
    report_lines.extend(discrepancies)

    if dry_run:
        report_lines.append("  (dry-run: not modified)")
    else:
        # Fix the stats
        for field, _ in fields:
            stored_stats[field] = calculated[field]

        # Preserve other stats fields (first_prompt_date, last_accessed, total_cost)
        data["stats"] = stored_stats

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

        report_lines.append("  FIXED: Stats recalculated from messages")

    return True, "\n".join(report_lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python fix_chat_stats.py <path> [--dry-run]")
        print("  <path> can be a .json chat file or a directory containing chat files")
        print("  --dry-run: Check only, don't modify files")
        sys.exit(1)

    target = Path(sys.argv[1])
    dry_run = "--dry-run" in sys.argv

    if not target.exists():
        print(f"Path not found: {target}")
        sys.exit(1)

    total_checked = 0
    total_fixed = 0

    if target.is_file() and target.suffix == '.json':
        # Single file
        was_fixed, report = verify_chat_stats(target, dry_run)
        print(report)
        total_checked = 1
        total_fixed = 1 if was_fixed else 0
    elif target.is_dir():
        # Directory - process all chat_*.json files
        for chat_file in sorted(target.glob("chat_*.json")):
            was_fixed, report = verify_chat_stats(chat_file, dry_run)
            print(report)
            total_checked += 1
            if was_fixed:
                total_fixed += 1
    else:
        print(f"Invalid path (must be .json file or directory): {target}")
        sys.exit(1)

    print(f"\nSummary: Checked {total_checked} chat(s), {'would fix' if dry_run else 'fixed'} {total_fixed}")


if __name__ == "__main__":
    main()
