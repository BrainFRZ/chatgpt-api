"""Accurate combat-cost estimate for the REAL cpred 2-stage mode pipeline.

Combat = run_mode_pipeline (NOT the 3-LLM generic pipeline):
  Stage 1  Planning   — LLM, gpt-5.2 (PIPELINE_PLANNING_MODEL), medium effort, JSON
  Backend  Resolution — resolve_actions(), deterministic, FREE (0 tokens)
  Stage 2  Narration  — LLM, deepseek-v3.2-or (PIPELINE_NARRATION_MODEL), low effort, prose

One mode-pipeline run = ONE full combat round (planner proposes the whole round's
actions for every combatant; backend resolves; narrator writes the round).
So rounds ~= player combat messages ~= mode-pipeline runs.

  python -m bench.combat_cost                  # measured sizes + modeled estimate
  python -m bench.combat_cost --live --keys ../data/users/printer/api_keys.json
       ^ makes ONE real gpt-5.2 planning call + ONE real v3.2 narration call to
         measure actual output/reasoning tokens (the dominant cost driver).
"""
from __future__ import annotations
import argparse
import json
import sys
import tiktoken

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ENC = tiktoken.get_encoding("cl100k_base")  # app's encoder (main.get_token_encoder)
def tk(s) -> int:
    return len(ENC.encode(s if isinstance(s, str) else json.dumps(s)))

from game_systems.cpred import (
    COMBAT_PLANNING_CONTRACT, COMBAT_PLANNING_SCHEMA, COMBAT_NARRATION_CONTRACT,
)
from game_systems.cpred_combat import build_cpred_combat_injection

# Real pricing, $/1M (input, cached_in, output) — from the provider classes.
PRICE = {
    "gpt-5.2":          {"in": 1.75, "cached": 0.175, "out": 14.0},   # OpenAIProvider.pricing
    "deepseek-v3.2-or": {"in": 0.27, "cached": 0.04,  "out": 0.40},   # OpenRouterDeepSeekV32.pricing
}


def _scenario():
    combat = {"round": 2,
              "initiative_order": ["Maelstrom Ganger A", "Maelstrom Ganger B", "Maelstrom Ganger C", "Kessler"],
              "current_turn": "Maelstrom Ganger A", "cover": {}}
    er = {"Kessler": {"name": "Kessler", "hp": {"current": 32, "max": 40},
                      "armor": {"head": 11, "body": 11}, "luck": {"current": 4, "max": 6},
                      "stats": {"REF": 8, "DEX": 7, "BODY": 7, "WILL": 6, "INT": 6, "COOL": 7},
                      "skills": {"Handgun": 6, "Autofire": 4, "Evasion": 6},
                      "weapons": [{"name": "Militech M-10AF", "weapon_type": "Pistol", "damage_dice": 2,
                                   "rof": 2, "magazine": 12, "current_ammo": 9}]}}
    cs = {n: {"data": {"type": "enemy", "combat_data": {
        "hp_max": 30, "armor": {"head": 4, "body": 4},
        "stats": {"REF": 5, "DEX": 5, "BODY": 5}, "skills": {"Handgun": 4, "Evasion": 3},
        "weapons": [{"name": "Budget SMG", "weapon_type": "SMG", "damage_dice": 2}]},
        "vitals": [{"label": "HP", "current": 22, "max": 30}]}}
        for n in ("Maelstrom Ganger A", "Maelstrom Ganger B", "Maelstrom Ganger C")}
    ps = {"game_state": {"edgerunners": er}, "character_states": cs, "combat": combat}
    inj = build_cpred_combat_injection(combat, ps)
    return inj


def measure_fixed():
    inj = _scenario()
    return {
        "plan_contract": tk(COMBAT_PLANNING_CONTRACT),
        "plan_schema": tk(json.dumps(COMBAT_PLANNING_SCHEMA, indent=2)),
        "narr_contract": tk(COMBAT_NARRATION_CONTRACT),
        "injection": tk(inj),
        "_inj": inj,
    }


def cost(fresh, cached, out, p):
    return (fresh * p["in"] + cached * p["cached"] + out * p["out"]) / 1e6


# --- assumptions for the variable pieces (used when not measured live) ---
A = {
    "appended_files_cached": 6000,   # char sheets + combat doc on planning_system; CACHED prefix
    "context_pairs": 8, "tok_per_pair": 220,
    "plan_out_json": 700,            # actions JSON for a full round (PC + 3 NPCs)
    "plan_reasoning": 1500,          # gpt-5.2 medium reasoning (billed as output) -- MEASURED with --live
    "narr_resolved_blob": 900, "narr_out_prose": 650,
}


def live_measure(keys_path):
    from bench.run import load_keys
    from openai import OpenAI
    keys = load_keys(keys_path)
    f = measure_fixed()
    player_action = ("I dump the spent mag, dive behind the concrete planter for cover, and snap off two "
                     "shots from my Militech at the nearest ganger pinning us down. Crank's still bleeding "
                     "out behind me — I need these gonks down NOW.")
    mode_stub = [
        {"role": "user", "content": "We kick the stairwell door and the two gangers open up. I take cover and return fire."},
        {"role": "assistant", "content": "[COMBAT round 1 resolved: Kessler 40->32 (SMG graze), Ganger A 30->22. Initiative: gangers, then Kessler.]"},
    ]
    plan_system = COMBAT_PLANNING_CONTRACT + "\n\nYou MUST output valid JSON matching this schema:\n" + json.dumps(COMBAT_PLANNING_SCHEMA, indent=2)
    plan_user = f["_inj"] + "\n\n" + player_action
    plan_messages = [{"role": "system", "content": plan_system}] + mode_stub + [{"role": "user", "content": plan_user}]

    oc = OpenAI(api_key=keys["openai"])
    print("  [live] calling gpt-5.2 planner (medium effort, json)...")
    kw = {}
    try:
        r = oc.chat.completions.create(model="gpt-5.2", messages=plan_messages,
                                       reasoning_effort="medium", response_format={"type": "json_object"},
                                       max_completion_tokens=4000)
    except Exception as e:
        print(f"  [live] reasoning_effort path failed ({str(e)[:80]}); retrying plain")
        r = oc.chat.completions.create(model="gpt-5.2", messages=plan_messages,
                                       response_format={"type": "json_object"}, max_completion_tokens=4000)
    u = r.usage
    det = getattr(u, "completion_tokens_details", None)
    reasoning = getattr(det, "reasoning_tokens", 0) or 0
    plan_in = u.prompt_tokens
    plan_out = u.completion_tokens
    cached_in = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    print(f"  [live] planner: prompt={plan_in} (cached {cached_in}) completion={plan_out} (reasoning {reasoning})")
    plan_json = r.choices[0].message.content or ""
    n_actions = 0
    try:
        n_actions = len(json.loads(plan_json).get("actions", []))
    except Exception:
        pass
    print(f"  [live] planner produced {n_actions} actions, {tk(plan_json)} tok of JSON")

    # Narration (v3.2) — feed the resolved blob + contract
    nc = OpenAI(api_key=keys["openrouter"], base_url="https://openrouter.ai/api/v1")
    narr_messages = [
        {"role": "system", "content": COMBAT_NARRATION_CONTRACT},
        *mode_stub,
        {"role": "user", "content": "Resolved this round:\n" + plan_json[:1500] +
         "\n\nNarrate this combat round vividly in second person, present tense."},
    ]
    print("  [live] calling v3.2 narrator...")
    nr = nc.chat.completions.create(model="deepseek/deepseek-v3.2", messages=narr_messages, max_tokens=1200,
                                    extra_body={"provider": {"ignore": ["sambanova"], "order": ["deepinfra", "novita"], "allow_fallbacks": True}})
    nu = nr.usage
    narr_in, narr_out = nu.prompt_tokens, nu.completion_tokens
    print(f"  [live] narrator: prompt={narr_in} completion={narr_out}")
    return {"plan_in": plan_in, "plan_cached": cached_in, "plan_out": plan_out, "plan_reasoning": reasoning,
            "narr_in": narr_in, "narr_out": narr_out, "fixed": f}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--keys", default=None)
    a = ap.parse_args(argv)

    f = measure_fixed()
    print("\n=== MEASURED FIXED SIZES (tiktoken) ===")
    for k in ("plan_contract", "plan_schema", "narr_contract", "injection"):
        print(f"  {k:16s} {f[k]:>6,} tok")
    print(f"\n=== PRICING ($/1M) ===\n  gpt-5.2: {PRICE['gpt-5.2']}\n  v3.2:    {PRICE['deepseek-v3.2-or']}")

    pP, pN = PRICE["gpt-5.2"], PRICE["deepseek-v3.2-or"]

    if a.live:
        print("\n=== LIVE MEASUREMENT (real calls) ===")
        m = live_measure(a.keys)
        # Planner: split prompt into cached vs fresh per the API's own cached count
        plan_cost = cost(m["plan_in"] - m["plan_cached"], m["plan_cached"], m["plan_out"], pP)
        narr_cost = cost(m["narr_in"], 0, m["narr_out"], pN)
        per_round = plan_cost + narr_cost
        print("\n=== PER-ROUND COST (measured) ===")
        print(f"  Planner gpt-5.2 : in={m['plan_in']:,}(cached {m['plan_cached']:,}) out={m['plan_out']:,}(reason {m['plan_reasoning']:,}) -> ${plan_cost:.5f}")
        print(f"  Narrator v3.2   : in={m['narr_in']:,} out={m['narr_out']:,} -> ${narr_cost:.5f}")
        print(f"  PER ROUND       : ${per_round:.5f}  (planner {100*plan_cost/per_round:.0f}%)")
    else:
        plan_cached = f["plan_contract"] + f["plan_schema"] + A["appended_files_cached"]
        plan_fresh = f["injection"] + A["context_pairs"] * A["tok_per_pair"] + 120
        plan_out = A["plan_out_json"] + A["plan_reasoning"]
        plan_cost = cost(plan_fresh, plan_cached, plan_out, pP)
        narr_fresh = A["context_pairs"] * A["tok_per_pair"] + A["narr_resolved_blob"] + 120
        narr_cost = cost(narr_fresh, f["narr_contract"], A["narr_out_prose"], pN)
        per_round = plan_cost + narr_cost
        print("\n=== PER-ROUND COST (modeled) ===")
        print(f"  Planner gpt-5.2 : cached={plan_cached:,} fresh={plan_fresh:,} out={plan_out:,} -> ${plan_cost:.5f}")
        print(f"  Narrator v3.2   : -> ${narr_cost:.5f}")
        print(f"  PER ROUND       : ${per_round:.5f}  (planner {100*plan_cost/per_round:.0f}%)")

    print("\n=== PER-COMBAT (by rounds) ===")
    for rounds in (3, 4, 5, 6, 8):
        print(f"  {rounds} rounds: ${per_round*rounds:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
