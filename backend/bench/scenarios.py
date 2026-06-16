"""Benchmark scenarios built on the app's REAL tool schemas.

Suite A — tool/dice reliability: turns engineered to require a resolve_mechanics
or report_state call. Measures whether the model emits a valid STRUCTURED tool
call (vs narrating it as text — the DeepSeek failure mode) and whether the
arguments validate against the real schema.

Suite B — persistent-state / plot-flag retention: a long synthetic Cyberpunk RED
session grown to a target token size, with facts/flags planted early and probed
at the end. Measures recall accuracy and drift at 80k / 100k / 125k tokens.
"""
from __future__ import annotations

import random

from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
from game_systems.cpred import STATE_REPORT_TOOL

DOC_TOOLS = [RESOLVE_MECHANICS_TOOL, STATE_REPORT_TOOL]

# Compact but representative stand-in for the single-agent turn contract. The
# real app ships a much larger contract; pass --full-contract to load it and
# test reliability under realistic prompt size.
COMPACT_SYSTEM = """You are the Game Master for a Cyberpunk RED tabletop session, running in single-agent mode.

TURN CONTRACT (every turn):
- When the turn involves any dice — skill checks, attacks, damage, death saves, initiative, contests — you MUST call the `resolve_mechanics` tool BEFORE narrating, with ALL actions for the turn in the `actions` array. Never narrate a dice result yourself; the backend rolls.
- After mechanics (or immediately, when no dice are needed) call `report_state` to emit the turn: the `narration` field is the player-facing prose, plus the state fields it describes (is_ooc, pacing, scene_state, character_states).
- A tool call is forced every turn. Put player-facing prose in report_state.narration, never only in plain text.

Keep characters consistent and track state faithfully."""


def full_system():
    """Load the real, full single-agent contract for a faithful-size test."""
    from game_systems.cpred import SINGLE_AGENT_STATE_CONTRACT
    return ("You are the Game Master for a Cyberpunk RED tabletop session, running "
            "in single-agent mode.\n\n" + SINGLE_AGENT_STATE_CONTRACT)


# --- Suite A: tool/dice reliability scenarios --------------------------------
# expect: which tool the turn should elicit first.
TOOL_SCENARIOS = [
    {"id": "ranged_attack", "expect": "resolve_mechanics",
     "user": "Kessler snaps up her Militech M-10AF pistol and fires two rounds at the gonk in the leather jacket charging her — he's about 10 meters out, no cover. Resolve it."},
    {"id": "skill_check", "expect": "resolve_mechanics",
     "user": "RedVelvet tries to pick the maglock on the server-room door. She's got Pick Lock at +12. DV is 15. Does she get in?"},
    {"id": "melee", "expect": "resolve_mechanics",
     "user": "Delphi swings her cyber-katana at the boostergang member grappling her. Resolve the attack."},
    {"id": "opposed_stealth", "expect": "resolve_mechanics",
     "user": "Kessler sneaks past the dozing guard. Stealth vs his Perception — roll the contest."},
    {"id": "initiative", "expect": "resolve_mechanics",
     "user": "Three Maelstrom gangers kick the door in and open fire. Roll initiative for everyone and start combat."},
    {"id": "death_save", "expect": "resolve_mechanics",
     "user": "Delphi just dropped to mortally wounded. Roll her Death Save."},
    {"id": "social_scene", "expect": "report_state",
     "user": "Kessler just walks up to the bar and orders a synth-whiskey, watching the room. Nothing mechanical — just set the scene and her read of the place."},
    {"id": "downtime_haggle", "expect": "resolve_mechanics",
     "user": "Delphi (a Fixer, Operator rank 6) haggles with the ripperdoc over a Sandevistan listed at 5,000eb. Resolve the haggle."},
    {"id": "scene_transition", "expect": "report_state",
     "user": "They leave the night market and head back to the safehouse. No checks — just narrate the walk back and update where everyone is."},
    {"id": "autofire", "expect": "resolve_mechanics",
     "user": "Kessler lets rip with her Ajax assault rifle on full auto at the armored Militech trooper 20m away. Resolve the autofire."},
]


def tool_request(provider, scenario, full_contract=False):
    """Build an Anthropic-contract request for one Suite A scenario."""
    system = full_system() if full_contract else COMPACT_SYSTEM
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": scenario["user"]},
    ]
    params = provider.build_request(messages, "bench", None, "bench", is_free_chat=False)
    params["tools"] = DOC_TOOLS
    params["tool_choice"] = {"type": "any"}
    return params


def score_tool_turn(scenario, usage: dict) -> dict:
    """Score one Suite A response."""
    tool_uses = usage.get("tool_uses") or []
    names = [t.get("name") for t in tool_uses]
    emitted = bool(tool_uses)
    got_expected = scenario["expect"] in names
    valid = False
    for t in tool_uses:
        if t.get("name") == scenario["expect"]:
            valid = _validate_tool_input(t.get("name"), t.get("input") or {})
            break
    return {
        "scenario": scenario["id"],
        "expect": scenario["expect"],
        "emitted_structured": emitted,            # any structured tool call at all
        "raw_first_ok": bool(usage.get("_bench_raw_tool_ok", emitted)),
        "got_expected_tool": got_expected,
        "schema_valid": valid,
        "recovered": bool(usage.get("_bench_recovered", False)),
        "retries": int(usage.get("_bench_retries", 0)),
        "names": names,
    }


def _validate_tool_input(name: str, inp: dict) -> bool:
    if name == "resolve_mechanics":
        actions = inp.get("actions")
        if not isinstance(actions, list) or not actions:
            return False
        for a in actions:
            if not isinstance(a, dict) or not a.get("type") or not a.get("character"):
                return False
        return True
    if name == "report_state":
        required = ("narration", "is_ooc", "pacing", "scene_state", "character_states")
        return all(k in inp for k in required)
    return False


# --- Suite B: persistent-state / plot-flag retention -------------------------
# Facts planted early; each has a probe whose correct answer contains a unique
# verifiable token, so scoring is deterministic (no LLM judge needed).
PLANTED_FACTS = [
    {"key": "ramslots",
     "fact": "Established fact: Kessler's cyberdeck has exactly 7 RAM slots.",
     "probe": "How many RAM slots does Kessler's cyberdeck have? Answer with just the number.",
     "answer": "7"},
    {"key": "debt",
     "fact": "Established fact: Delphi owes the fixer Hong exactly 4200 eurobucks.",
     "probe": "Exactly how many eurobucks does Delphi owe Hong? Answer with just the number.",
     "answer": "4200"},
    {"key": "doorcode",
     "fact": "Established fact: the safehouse door code is 8841.",
     "probe": "What is the safehouse door code? Answer with just the number.",
     "answer": "8841"},
    {"key": "flag_betrayed",
     "fact": "PLOT FLAG SET: `betrayed_militech` = TRUE (the crew burned their Militech contact in Episode 1).",
     "probe": "Is the plot flag betrayed_militech true or false? Answer with just the word true or false.",
     "answer": "true"},
    {"key": "scar",
     "fact": "Established fact: RedVelvet has a distinctive scar over her LEFT eye.",
     "probe": "Over which eye does RedVelvet have her scar — left or right? Answer with just the word.",
     "answer": "left"},
    {"key": "rep",
     "fact": "Established fact: Kessler's Reputation rank is 5.",
     "probe": "What is Kessler's Reputation rank? Answer with just the number.",
     "answer": "5"},
]

_FILLER_BEATS = [
    "The crew moves through the rain-slick Watson backstreets, neon bleeding across wet asphalt.",
    "A AV thunders overhead, spotlight sweeping the alley before moving on.",
    "Static crackles on the comms as Delphi checks in from the rooftop overwatch.",
    "The night market hums — vendors hawking synth-noodles, black-market cyberware, hot data shards.",
    "Kessler scans the crowd, cataloguing exits and the two boosters loitering by the ramen stall.",
    "RedVelvet jacks into a public data terminal, fingers dancing as she sifts traffic.",
    "A Trauma Team AV screams past, someone's platinum subscription getting its money's worth.",
    "The fixer's voice comes through, clipped: the buyer wants the shard tonight, no excuses.",
    "Somewhere uptown a corp tower glitters, indifferent to the gutter politics below.",
    "The crew regroups in a noodle bar booth, comparing notes over steaming bowls.",
]


def _filler_turn(i: int, rng) -> list:
    beat = _FILLER_BEATS[i % len(_FILLER_BEATS)]
    user = f"(Turn {i}) The crew keeps moving. {rng.choice(['What happens next?', 'Continue.', 'Press on.'])}"
    narr = " ".join(rng.sample(_FILLER_BEATS, k=4)) + f" {beat} (beat {i})"
    return [{"role": "user", "content": user},
            {"role": "assistant", "content": narr}]


def build_retention_context(provider, target_tokens: int, seed: int = 7):
    """Grow a synthetic session to ~target_tokens with facts planted early.
    Returns (base_messages_without_probe, probes)."""
    rng = random.Random(seed)
    system = ("You are the GM of an ongoing Cyberpunk RED campaign. Track all "
              "established facts and plot flags faithfully across the whole session.")
    messages = [{"role": "system", "content": system}]

    # Plant facts as GM-acknowledged turns near the START (the hard case: recall
    # them after a large amount of intervening context).
    for f in PLANTED_FACTS:
        messages.append({"role": "user", "content": f"GM, note this for the record: {f['fact']}"})
        messages.append({"role": "assistant", "content": f"Noted and locked in. {f['fact']}"})

    # Count incrementally (O(n)) — re-tokenizing the whole transcript each
    # iteration is O(n^2) and crawls at 125k tokens.
    running = provider.count_tokens("\n".join(m["content"] for m in messages))
    i = 0
    while running < target_tokens:
        turn = _filler_turn(i, rng)
        messages.extend(turn)
        running += provider.count_tokens(turn[0]["content"] + "\n" + turn[1]["content"])
        i += 1
    return messages, PLANTED_FACTS


def retention_request(provider, base_messages, probe):
    messages = list(base_messages) + [
        {"role": "user", "content": "Recall check (answer from established facts only): " + probe["probe"]}
    ]
    params = provider.build_request(messages, "bench", None, "bench", is_free_chat=False)
    # No tools on a probe — we want a short text answer.
    params["tool_choice"] = {"type": "none"}
    return params


def score_probe(probe, usage: dict) -> dict:
    answer = (usage.get("content") or "").strip().lower()
    correct = probe["answer"].lower() in answer
    return {"key": probe["key"], "correct": correct, "answer": answer[:80]}
