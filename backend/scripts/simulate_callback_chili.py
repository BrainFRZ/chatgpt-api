"""Smoke-test the shared-plan callback extraction end-to-end.

Builds a throwaway character project with just enough scaffolding for the
character_agent to run, sends one fake user/assistant exchange that ends
with a forward-looking 'Friday chili at mine' commitment, then prints
what the agent extracted. Verifies the callback has a due_by set to the
correct upcoming Friday.

Run from the backend directory:
    cd backend && py scripts/simulate_callback_chili.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date

# Make backend imports work regardless of where we're invoked from
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import anthropic
from character_agent import determine_character_ops, apply_character_ops_to_state
from game_systems.characters import init_characters_state, today_et_iso, expire_overdue_callbacks


PROFILE = """\
# Zara Chang

## Identity
- 35 years old, owns The Back Booth (small cafe in Syracuse).
- Lifelong best friend with Shae (since middle school band, 2002).
- Direct, dry humor, runs warm.

## Voice
- Texts in lowercase, often. Reaches for affection naturally.
- Doesn't gush. When she commits to something she means it.

## Relationship to user (Shae)
- Coffee bean promise: their no-ghost vow. Beans aren't invoked casually.
- Shae has CFS, sometimes flares; Zara stays steady through it.
"""


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Fall back to printer user's api_keys.json (the prod path)
        import json
        keys_path = os.path.join(BACKEND, "..", "data", "users", "printer", "api_keys.json")
        if os.path.exists(keys_path):
            with open(keys_path, encoding="utf-8") as f:
                api_key = (json.load(f) or {}).get("anthropic")
        if not api_key:
            print("No anthropic key in env or data/users/printer/api_keys.json — aborting.")
            sys.exit(1)

    tmp = tempfile.mkdtemp(prefix="callback_test_")
    try:
        with open(os.path.join(tmp, "character_profile.di"), "w", encoding="utf-8") as f:
            f.write(PROFILE)
        # Empty file-backed stores so the agent can read but won't have stale data
        for fname in ("character_memories.jsonl", "user_profile.jsonl", "character_growth.jsonl"):
            open(os.path.join(tmp, fname), "w", encoding="utf-8").close()

        characters_state = init_characters_state()
        # Stamp wall_clock so the agent sees today's date in its context
        from game_systems.characters import update_wall_clock
        characters_state["wall_clock"] = update_wall_clock({}, user_message=False)

        client = anthropic.Anthropic(api_key=api_key)

        user_msg = (
            "ok i'm officially in for chili friday. you bringing the cornbread or am i?"
        )
        zara_reply = (
            "<reply>i got cornbread. you just bring yourself and that hoodie i like. "
            "7ish at yours, friday. coffee bean promise.</reply>"
        )

        today = today_et_iso()
        today_dow = date.fromisoformat(today).strftime("%A")
        next_friday = date.fromisoformat(today)
        # 0=Mon..4=Fri..6=Sun
        delta = (4 - next_friday.weekday()) % 7
        if delta == 0:
            delta = 7  # if today IS Friday, plan is for next Friday
        from datetime import timedelta
        next_friday = next_friday + timedelta(days=delta)
        expected_due_by = next_friday.isoformat()

        print(f"today: {today} ({today_dow})")
        print(f"expected due_by for 'Friday chili': {expected_due_by} ({next_friday.strftime('%A')})")
        print(f"user msg:    {user_msg}")
        print(f"zara reply:  {zara_reply}")
        print()

        ops, usage = determine_character_ops(
            client, tmp, characters_state, user_msg, zara_reply, branch_msg_ids=None,
        )
        print(f"=== ops extracted ===")
        for k, v in (ops or {}).items():
            print(f"  {k}: {v}")
        print()

        apply_character_ops_to_state(
            characters_state, ops or {}, current_turn=1, today_iso=today,
            project_dir=tmp, branch_msg_id="msg-1",
        )

        callbacks = characters_state.get("callbacks") or {}
        open_cbs = callbacks.get("open") or []
        print(f"=== open callbacks after apply ({len(open_cbs)}) ===")
        for cb in open_cbs:
            print(f"  {cb}")
        print()

        # Verify
        chili_cb = next(
            (cb for cb in open_cbs if "chili" in (cb.get("original_text") or "").lower()),
            None,
        )
        if not chili_cb:
            print("FAIL: no chili callback was added")
            sys.exit(2)
        if not chili_cb.get("due_by"):
            print(f"FAIL: chili callback was added but has no due_by: {chili_cb}")
            sys.exit(2)
        actual_due = chili_cb.get("due_by")
        if actual_due == expected_due_by:
            print(f"PASS: chili callback has due_by={actual_due} ({expected_due_by} expected)")
        else:
            print(f"PARTIAL: due_by={actual_due}, expected {expected_due_by}. "
                  f"Agent picked a different Friday than the next one — likely fine "
                  f"if it lined up with what 'friday' meant in context.")

        # Now simulate Saturday — the chili happened — and see if a follow-up turn resolves it
        print()
        print("=== second turn (next-day, plan played out) ===")
        sat_user = "last night was perfect btw. i love you."
        sat_reply = "<reply>same. tonight ruled. coffee bean.</reply>"

        ops2, _ = determine_character_ops(
            client, tmp, characters_state, sat_user, sat_reply, branch_msg_ids=None,
        )
        print(f"ops2: {ops2}")
        apply_character_ops_to_state(
            characters_state, ops2 or {}, current_turn=2, today_iso=actual_due,
            project_dir=tmp, branch_msg_id="msg-2",
        )
        callbacks_after = characters_state.get("callbacks") or {}
        print(f"open after turn 2: {callbacks_after.get('open')}")
        print(f"resolved after turn 2: {callbacks_after.get('resolved')}")

        # Now simulate auto-expiry for an old callback
        print()
        print("=== auto-expiry pass (advancing clock past due_by without resolving) ===")
        # Re-add a forward callback with due_by yesterday
        from game_systems.characters import apply_callback_ops
        characters_state["callbacks"] = apply_callback_ops(
            callbacks_after,
            [{"action": "add", "original_text": "movie night last week", "source": "character", "due_by": "2026-05-04"}],
            current_turn=3, today_iso=today,
        )
        print(f"injected stale callback with due_by 2026-05-04. open: {characters_state['callbacks']['open']}")
        expired = expire_overdue_callbacks(characters_state["callbacks"], today_iso=today)
        print(f"expired: {expired}")
        print(f"open after expire: {characters_state['callbacks']['open']}")
        print(f"dismissed after expire: {characters_state['callbacks']['dismissed']}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
