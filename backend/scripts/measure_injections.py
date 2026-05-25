#!/usr/bin/env python3
"""
Measure token breakdown for a single-agent Claude request.

Usage:
    python backend/scripts/measure_injections.py <user> <project> <chat_name>
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock missing optional dependencies so pipeline.py can import
import types
class _MockModule(types.ModuleType):
    """Module stub that returns a dummy for any attribute access."""
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        m = _MockModule(f"{self.__name__}.{name}")
        setattr(self, name, m)
        return m
    def __call__(self, *a, **kw):
        return self

for mod_name in ("openai", "anthropic", "tiktoken", "fastapi", "pydantic", "starlette",
                 "sse_starlette", "filelock", "uvicorn", "dotenv"):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = _MockModule(mod_name)


def estimate_tokens(text: str) -> int:
    """~4 chars per token for English/JSON."""
    return len(text) // 4


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    user, project, chat_name = sys.argv[1], sys.argv[2], sys.argv[3]
    data_root = os.path.join(os.path.dirname(__file__), "..", "..", "data", "users")
    chat_path = os.path.join(data_root, user, "projects", project, f"chat_{chat_name}.json")
    meta_path = os.path.join(data_root, user, "projects", project, "metadata.json")
    proj_dir = os.path.join(data_root, user, "projects", project)

    with open(chat_path, "r", encoding="utf-8") as f:
        chat = json.load(f)

    pipeline_state = chat.get("pipeline_state", {})
    if not pipeline_state:
        print("No pipeline_state found.")
        sys.exit(1)

    game_system_id = None
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            game_system_id = json.load(f).get("game_system")

    scene_state = pipeline_state.get("scene_state", {})
    turn_counter = pipeline_state.get("turn_counter", 0)

    # ============================================================
    # UNCACHED: Build each injection block individually
    # (These go into the user message, not cached)
    # ============================================================
    uncached_blocks = []

    # --- 1. Pacing ---
    pacing = pipeline_state.get("pacing", {})
    if pacing:
        text = f"[PIPELINE STATE]\n{json.dumps(pacing, indent=2)}\n[/PIPELINE STATE]"
        uncached_blocks.append(("PIPELINE STATE (pacing)", text))

    # --- 2. Callback ledger ---
    from pipeline import build_callback_injection
    cb = build_callback_injection(pipeline_state.get("callback_ledger", {}), turn_counter)
    if cb:
        uncached_blocks.append(("CALLBACK LEDGER", cb))

    # --- 3. Decision flags ---
    from pipeline import build_decision_flags_injection
    df = build_decision_flags_injection(pipeline_state.get("decision_flags", {}))
    if df:
        uncached_blocks.append(("DECISION FLAGS", df))

    # --- 4. NPC memories ---
    from pipeline import build_npc_memories_injection
    mem = build_npc_memories_injection(pipeline_state.get("npc_memories", {}), scene_state)
    if mem:
        uncached_blocks.append(("NPC MEMORIES", mem))

    # --- 5. Scene state ---
    from pipeline import build_scene_state_injection
    uncached_blocks.append(("SCENE STATE", build_scene_state_injection(scene_state)))

    # --- 6. Character states ---
    from pipeline import build_character_states_injection
    uncached_blocks.append(("CHARACTER STATES", build_character_states_injection(
        pipeline_state.get("character_states", {}), scene_state)))

    # --- 6a. NPC voices ---
    from pipeline import build_npc_voices_injection
    voices = build_npc_voices_injection(
        pipeline_state.get("character_states", {}), scene_state)
    if voices:
        uncached_blocks.append(("NPC VOICES", voices))

    # --- 7. HUD state ---
    from pipeline import build_hud_state_injection
    hud = build_hud_state_injection(
        pipeline_state.get("hud_state", {}), scene_state,
        pipeline_state.get("character_states", {}),
        game_state=pipeline_state.get("game_state"))
    if hud:
        uncached_blocks.append(("HUD STATE", hud))

    # --- 8. Game injection (CPRED: edgerunner + relationships + IP tracker) ---
    if game_system_id == "cpred":
        from game_systems.cpred_core import build_game_injection
        game_inj = build_game_injection(pipeline_state.get("game_state", {}), scene_state)
        if game_inj:
            uncached_blocks.append(("GAME INJECTION (cpred full)", game_inj))

    # --- 9. Player agency reminder (approximate) ---
    # Small, skip for now

    # ============================================================
    # CACHED: System prompt components
    # ============================================================
    cached_blocks = []

    # Single agent contract
    if game_system_id == "cpred":
        from game_systems.cpred import SINGLE_AGENT_STATE_CONTRACT
        cached_blocks.append(("single_agent_contract", SINGLE_AGENT_STATE_CONTRACT))

    # Original system message (branch_path[0]) = instructions + project files
    # instructions.di
    inst_path = os.path.join(proj_dir, "instructions.di")
    if os.path.exists(inst_path):
        with open(inst_path, "r", encoding="utf-8") as f:
            cached_blocks.append(("instructions.di", f.read()))

    # Base instructions
    base_inst_path = os.path.join(data_root, user, "base_instructions.di")
    if os.path.exists(base_inst_path):
        with open(base_inst_path, "r", encoding="utf-8") as f:
            cached_blocks.append(("base_instructions.di", f.read()))

    # Project files (staged docs)
    for fname in sorted(os.listdir(proj_dir)):
        if fname.endswith(".md") and not fname.startswith("chat_"):
            fpath = os.path.join(proj_dir, fname)
            with open(fpath, "r", encoding="utf-8") as f:
                cached_blocks.append((f"doc: {fname}", f.read()))

    # Uploads (rulebooks etc)
    uploads_dir = os.path.join(proj_dir, "uploads")
    if os.path.isdir(uploads_dir):
        for fname in sorted(os.listdir(uploads_dir)):
            fpath = os.path.join(uploads_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    cached_blocks.append((f"upload: {fname}", f.read()))

    # ============================================================
    # Conversation history (cached up to last assistant)
    # ============================================================
    messages = chat.get("messages", [])
    # Find conversation messages (non-system)
    conv_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
    conv_text = sum(len(m.get("content", "")) for m in conv_msgs[:-1])  # exclude last user msg
    conv_tokens = conv_text // 4

    # Last user message (the actual player text, uncached)
    if conv_msgs and conv_msgs[-1]["role"] == "user":
        last_user = conv_msgs[-1].get("content", "")
    else:
        last_user = "(could not identify last user message)"

    # ============================================================
    # Print results
    # ============================================================
    print(f"\n{'='*65}")
    print(f"TOKEN BREAKDOWN: {project} / {chat_name}")
    print(f"{'='*65}")

    print(f"\n--- UNCACHED (user message injections) ---")
    print(f"{'Block':<40} {'Chars':>8} {'~Tokens':>8}")
    print("-" * 60)

    uncached_total_chars = 0
    uncached_total_tokens = 0
    for label, text in uncached_blocks:
        chars = len(text)
        tokens = estimate_tokens(text)
        uncached_total_chars += chars
        uncached_total_tokens += tokens
        print(f"{label:<40} {chars:>8,} {tokens:>8,}")

    player_chars = len(last_user) if isinstance(last_user, str) else 0
    player_tokens = player_chars // 4
    uncached_total_chars += player_chars
    uncached_total_tokens += player_tokens
    print(f"{'Player message text':<40} {player_chars:>8,} {player_tokens:>8,}")

    print("-" * 60)
    print(f"{'UNCACHED TOTAL (I: field)':<40} {uncached_total_chars:>8,} {uncached_total_tokens:>8,}")

    print(f"\n--- CACHED (system prompt + conversation history) ---")
    print(f"{'Block':<40} {'Chars':>8} {'~Tokens':>8}")
    print("-" * 60)

    cached_total_chars = 0
    cached_total_tokens = 0
    for label, text in cached_blocks:
        chars = len(text)
        tokens = estimate_tokens(text)
        cached_total_chars += chars
        cached_total_tokens += tokens
        print(f"{label:<40} {chars:>8,} {tokens:>8,}")

    print(f"{'Conversation history (est)':<40} {conv_text:>8,} {conv_tokens:>8,}")
    cached_total_chars += conv_text
    cached_total_tokens += conv_tokens

    print("-" * 60)
    print(f"{'CACHED TOTAL (C: field)':<40} {cached_total_chars:>8,} {cached_total_tokens:>8,}")

    print(f"\n--- SUMMARY ---")
    grand_total = uncached_total_tokens + cached_total_tokens
    print(f"Estimated total input: ~{grand_total:,} tokens")
    print(f"Estimated uncached (I): ~{uncached_total_tokens:,} tokens")
    print(f"Estimated cached (C): ~{cached_total_tokens:,} tokens")
    print(f"Actual from last turn: I:17176 C:0 (or I:17270 C:60160 with cache hit)")
    print()

    # Show biggest uncached block
    if uncached_blocks:
        biggest = max(uncached_blocks, key=lambda b: len(b[1]))
        print(f"=== Largest uncached block: {biggest[0]} ({len(biggest[1]):,} chars, ~{estimate_tokens(biggest[1]):,} tokens) ===")
        preview = biggest[1]
        if len(preview) > 2000:
            preview = preview[:2000] + f"\n... ({len(biggest[1]) - 2000:,} more chars)"
        print(preview)


if __name__ == "__main__":
    main()
