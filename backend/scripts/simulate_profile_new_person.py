"""Smoke-test the contract's new positive trigger for profile_ops.

Builds a throwaway temp project with an empty user_profile.jsonl, runs one
turn against the live API where the user introduces a new significant
person mid-conversation, and asserts the character_agent emits a
profile_ops add for that person.

Tmp project is auto-cleaned via shutil.rmtree in the finally block — no
pollution to Zara or any real project. If anything went wrong, the temp
dir name is printed so the user can verify.

Run:
    cd backend && py scripts/simulate_profile_new_person.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

# Force UTF-8 stdout so emojis in fake replies don't blow up Windows cp1252.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import anthropic
from character_agent import determine_character_ops, apply_character_ops_to_state
from game_systems.characters import init_characters_state, today_et_iso, update_wall_clock


PROFILE = """\
# Zara Chang

## Identity
- 35 years old, owns The Back Booth (small cafe in Syracuse).
- Lifelong best friend with Shae (since middle school band, 2002).
- Direct, dry humor, runs warm.

## Voice
- Texts in lowercase, often.
- Doesn't gush. Affection comes through tone, not declarations.

## Relationship to user (Shae)
- Coffee bean promise: their no-ghost vow. Beans aren't invoked casually.
- Shae has CFS, sometimes flares; Zara stays steady through it.
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        keys_path = os.path.join(BACKEND, "..", "data", "users", "printer", "api_keys.json")
        if os.path.exists(keys_path):
            with open(keys_path, encoding="utf-8") as f:
                api_key = (json.load(f) or {}).get("anthropic")
        if not api_key:
            print("No anthropic key in env or data/users/printer/api_keys.json — aborting.")
            return 1

    tmp = tempfile.mkdtemp(prefix="profile_test_")
    print(f"tmp project: {tmp}")
    try:
        with open(os.path.join(tmp, "character_profile.di"), "w", encoding="utf-8") as f:
            f.write(PROFILE)
        # Empty file-backed stores so the agent starts fresh
        for fname in ("character_memories.jsonl", "user_profile.jsonl", "character_growth.jsonl"):
            open(os.path.join(tmp, fname), "w", encoding="utf-8").close()

        characters_state = init_characters_state()
        characters_state["wall_clock"] = update_wall_clock({}, user_message=False)

        client = anthropic.Anthropic(api_key=api_key)

        # A turn that introduces a new significant person — exactly the pattern
        # the new contract section is meant to catch.
        user_msg = (
            "oh i don't think you've met my friend marcus yet. we've known each "
            "other since college. he just moved back to syracuse from austin and "
            "we're gonna do brunch saturday — first time in person in like 4 years"
        )
        zara_reply = (
            "<reply>oh shit, marcus! welcome back to the gray skies, buddy 🌧️\n\n"
            "syracuse>austin imo (i am very biased) but also brunch saturday? "
            "you absolutely have to bring him through the booth at some point. "
            "i need to vibe-check this guy properly\n\n"
            "have a great time babe</reply>"
        )

        today = today_et_iso()
        print(f"today: {today}")
        print(f"user msg:   {user_msg}")
        print(f"zara reply: {zara_reply}")
        print()

        ops, usage = determine_character_ops(
            client, tmp, characters_state, user_msg, zara_reply, branch_msg_ids=None,
        )
        print("=== ops extracted ===")
        for k, v in (ops or {}).items():
            if v:
                print(f"  {k}: {v}")
        print()

        # Verify a profile_ops add fired for Marcus
        profile_ops = (ops or {}).get("profile_ops") or []
        marcus_add = next(
            (op for op in profile_ops
             if isinstance(op, dict)
             and op.get("action") == "add"
             and "marcus" in (op.get("text") or "").lower()),
            None,
        )
        if marcus_add:
            print(f"PASS: profile_ops.add fired for Marcus")
            print(f"      text: {marcus_add.get('text')}")
            print(f"      category: {marcus_add.get('category')}")
        else:
            print(f"FAIL: no profile_ops.add for Marcus")
            print(f"      ops: {ops}")
            return 2

        # Apply ops to verify they write through (against the temp project's
        # store; this does NOT touch Zara's data)
        apply_character_ops_to_state(
            characters_state, ops or {}, current_turn=1, today_iso=today,
            project_dir=tmp, branch_msg_id="msg-1",
        )

        # Verify user_profile.jsonl now has the entry
        profile_path = os.path.join(tmp, "user_profile.jsonl")
        with open(profile_path, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        print()
        print(f"user_profile.jsonl after apply: {len(lines)} entries")
        for e in lines:
            print(f"  #{e.get('id')} [{e.get('category', '?')}] {(e.get('text') or '')[:200]}")

        # Sanity: also check whether a memory was added (it shouldn't be — Marcus
        # is profile, not memory — but log what happened for context)
        mem_ops = (ops or {}).get("memory_ops") or []
        if mem_ops:
            print()
            print(f"memory_ops also fired ({len(mem_ops)}):")
            for m in mem_ops:
                print(f"  {m.get('action')} impact={m.get('impact')} hook={m.get('hook')!r}")

        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"\ncleanup: tmp project removed ({tmp})")


if __name__ == "__main__":
    sys.exit(main())
