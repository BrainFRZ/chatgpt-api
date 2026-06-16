"""Combat orchestration benchmark — drives the REAL cpred combat engine.

The backend does the work: `cpred_mechanics.resolve_actions` resolves dice + RAW +
state deterministically. The MODEL's job is orchestration — pick the right
`resolve_mechanics` action types with correct params, read results, advance the
fight. This harness runs that agentic loop against the real engine and scores the
model's orchestration. V3.2 vs GPT-5.4 (the incumbent — also the fairness anchor:
it runs combat in production today).

    python -m bench.combat_bench --keys ../data/users/printer/api_keys.json --dry-run
    python -m bench.combat_bench --keys ../data/users/printer/api_keys.json   # real run

NOTE: tests the resolve_mechanics single-agent combat path — the path V3.2 would
use if it took over combat (instead of auto-switching to GPT-5.4).
"""
from __future__ import annotations

import argparse
import json
import sys

import tiktoken

from bench.run import load_keys
from game_systems.cpred_mechanics import resolve_actions, RESOLVE_MECHANICS_TOOL
from game_systems.cpred import CPRED_COMBAT_CONTRACT
from game_systems.cpred_combat import build_cpred_combat_injection

ENC = tiktoken.get_encoding("cl100k_base")
def tk(s): return len(ENC.encode(s or ""))

# (input, output) $/M. GPT-5.4 is an estimate — verify against OpenAI's sheet.
PRICES = {
    "deepseek/deepseek-v3.2": (0.27, 0.40),
    "deepseek/deepseek-v4-pro": (0.55, 1.20),   # OpenRouter pass-through
    "deepseek-v4-pro": (0.40, 1.00),            # DeepSeek-direct (first-party, cheaper) — est.
    "gpt-5.4": (1.25, 10.0),
}

# Targeted guards for the two failure modes V3.2 showed under the terse prompt
# (re-rolling initiative; targetless attacks). Mirrors the prompt-fix that lifted
# flag-honoring 72%->90%. Appended to the full contract in the "guards" variant.
COMBAT_GUARDS = """

CRITICAL ORCHESTRATION RULES (follow exactly):
- Roll initiative EXACTLY ONCE for the whole encounter. After you receive the initiative order back, do NOT call resolve_mechanics with type "initiative" again — proceed immediately to resolve combatant turns in that order.
- Every attack action (ranged_attack / autofire / melee_attack) MUST include a "target" field naming who is being attacked.
- Resolve ONE combatant's turn per resolve_mechanics call, advancing through the initiative order. When the turn reaches Kessler (the player), call end_exchange (combat_complete=false). Call end_exchange with combat_complete=true once all enemies are at 0 HP.
- To finish the exchange you MUST call the end_exchange tool — do not just stop."""


def firefight_scenario():
    """A PC + 2 gangers, point-blank. Exercises initiative -> ranged attacks ->
    damage -> elimination."""
    edgerunner_states = {
        "Kessler": {
            "name": "Kessler", "hp": {"current": 40, "max": 40, "seriously_wounded": False},
            "armor": {"head": 11, "body": 11}, "luck": {"current": 6, "max": 6},
            "stats": {"REF": 8, "DEX": 7, "BODY": 7, "WILL": 6, "INT": 6, "COOL": 7, "TECH": 5, "EMP": 5},
            "skills": {"Handgun": 6, "Autofire": 4, "Assault Rifle": 6, "Evasion": 6, "Martial Arts": 4},
            "weapons": [
                {"name": "Militech M-10AF", "weapon_type": "Pistol", "damage_dice": 2, "rof": 2, "magazine": 12, "current_ammo": 12},
                {"name": "Ajax AR", "weapon_type": "Assault Rifle", "damage_dice": 5, "rof": 1, "magazine": 25, "current_ammo": 25},
            ],
        },
    }
    character_states = {}
    for n in ("Maelstrom Ganger A", "Maelstrom Ganger B"):
        character_states[n] = {"data": {"type": "enemy", "combat_data": {
            "hp_max": 30, "armor": {"head": 4, "body": 4},
            "stats": {"REF": 5, "DEX": 5, "BODY": 5, "WILL": 4, "INT": 4, "COOL": 4},
            "skills": {"Handgun": 4, "Evasion": 3, "Autofire": 2},
            "weapons": [{"name": "Budget SMG", "weapon_type": "SMG", "damage_dice": 2, "rof": 1, "magazine": 30, "current_ammo": 30}],
        }, "vitals": [{"label": "HP", "current": 30, "max": 30}]}}
    hp = {"Kessler": 40, "Maelstrom Ganger A": 30, "Maelstrom Ganger B": 30}
    state_block = ("[COMBAT STATE]\nRound: 1 (combat just started — roll initiative)\n"
                   "Combatants:\n"
                   "  Kessler (PC) — HP 40/40, SP H:11/B:11, Militech M-10AF pistol + Ajax AR\n"
                   "  Maelstrom Ganger A — HP 30/30, SP 4, Budget SMG\n"
                   "  Maelstrom Ganger B — HP 30/30, SP 4, Budget SMG\n"
                   "Range: point-blank (range_bracket 0). The two gangers ambushed Kessler in a stairwell.\n"
                   "[/COMBAT STATE]")
    kickoff = (state_block + "\n\nCombat begins. Roll initiative for all three, then resolve every NPC "
               "(ganger) turn in order through to Kessler's turn. One resolve_mechanics call per turn. "
               "When it reaches Kessler (the player), call end_exchange.")
    return {"id": "firefight", "edgerunner_states": edgerunner_states,
            "character_states": character_states, "hp": hp, "kickoff": kickoff}


def midcombat_scenario():
    """Mid-combat: the backend has ALREADY injected the initiative order + current
    actor (via the real build_cpred_combat_injection). The model just resolves the
    NPC turns from the injected order — no rolling. Tests the common-case combat
    turn where the backend feeds whose-up-now."""
    sc = firefight_scenario()
    combat = {"round": 2,
              "initiative_order": ["Maelstrom Ganger A", "Maelstrom Ganger B", "Kessler"],
              "current_turn": "Maelstrom Ganger A", "cover": {}}
    ps = {"game_state": {"edgerunners": sc["edgerunner_states"]}, "character_states": sc["character_states"]}
    inj = build_cpred_combat_injection(combat, ps)
    sc = dict(sc)
    sc["id"] = "midcombat"
    sc["kickoff"] = (inj + "\n\nThe initiative order is ALREADY SET above (do NOT roll initiative). "
                     "Resolve each NPC turn in order starting from the ACTING combatant (Maelstrom Ganger A), "
                     "then Maelstrom Ganger B, through to Kessler. One resolve_mechanics call per combatant turn; "
                     "every attack MUST include a target. When the turn reaches Kessler (the player), call end_exchange.")
    return sc


def elimination_scenario():
    """Retarget / stop-on-dead test. A strong ally (Rook) vs 3 fragile gangers
    (6 HP, SP 4) — one AR hit drops a ganger. The model resolves Rook's turns and
    must (a) pick a LIVE target each turn, (b) skip combatants already at 0 HP,
    (c) end_exchange(combat_complete=true) once all 3 are down. Measures
    acted_on_eliminated (shooting corpses) — the one behavior the firefight never
    exercised because nobody died."""
    er = {"Rook": {
        "name": "Rook", "hp": {"current": 45, "max": 45, "seriously_wounded": False},
        "armor": {"head": 11, "body": 11}, "luck": {"current": 6, "max": 6},
        "stats": {"REF": 8, "DEX": 7, "BODY": 7, "WILL": 6, "INT": 6, "COOL": 7, "TECH": 5, "EMP": 5},
        "skills": {"Assault Rifle": 6, "Handgun": 6, "Autofire": 6, "Evasion": 6},
        "weapons": [{"name": "Ajax AR", "weapon_type": "Assault Rifle", "damage_dice": 5, "rof": 1, "magazine": 25, "current_ammo": 25}],
    }}
    cs = {}
    for n in ("Maelstrom Ganger A", "Maelstrom Ganger B", "Maelstrom Ganger C"):
        cs[n] = {"data": {"type": "enemy", "combat_data": {
            "hp_max": 6, "armor": {"head": 4, "body": 4},
            "stats": {"REF": 5, "DEX": 5, "BODY": 4, "WILL": 4, "INT": 4, "COOL": 4},
            "skills": {"Handgun": 4, "Evasion": 3},
            "weapons": [{"name": "Budget SMG", "weapon_type": "SMG", "damage_dice": 2, "rof": 1, "magazine": 30, "current_ammo": 30}],
        }, "vitals": [{"label": "HP", "current": 6, "max": 6}]}}
    hp = {"Rook": 45, "Maelstrom Ganger A": 6, "Maelstrom Ganger B": 6, "Maelstrom Ganger C": 6}
    combat = {"round": 2,
              "initiative_order": ["Rook", "Maelstrom Ganger A", "Maelstrom Ganger B", "Maelstrom Ganger C"],
              "current_turn": "Rook", "cover": {}}
    ps = {"game_state": {"edgerunners": er}, "character_states": cs}
    inj = build_cpred_combat_injection(combat, ps)
    kickoff = (inj + "\n\nRook (the player's ally solo) is mopping up three near-dead gangers. "
               "Resolve each combatant's turn in initiative order, round by round. On Rook's turn, attack a "
               "ganger who is still standing (HP > 0) with the Ajax AR. SKIP any combatant already at 0 HP — "
               "do NOT target or act for a downed ganger. One resolve_mechanics call per turn; every attack MUST "
               "name a living target. When all three gangers are at 0 HP, call end_exchange with combat_complete=true.")
    return {"id": "elimination", "edgerunner_states": er, "character_states": cs, "hp": hp, "kickoff": kickoff}


def openai_tools():
    return [
        {"type": "function", "function": {
            "name": "resolve_mechanics", "description": RESOLVE_MECHANICS_TOOL["description"][:900],
            "parameters": RESOLVE_MECHANICS_TOOL["input_schema"]}},
        {"type": "function", "function": {
            "name": "end_exchange",
            "description": "Call when the exchange reaches the player (Kessler)'s turn, or combat is complete.",
            "parameters": {"type": "object", "properties": {
                "combat_complete": {"type": "boolean"},
                "summary": {"type": "string"}}, "required": ["combat_complete"]}}},
    ]


def _apply_hp_ops(hp_map, state_ops):
    for op in state_ops or []:
        if isinstance(op, dict) and op.get("op") == "hp":
            name = op.get("edgerunner") or op.get("target") or op.get("name")
            if name in hp_map:
                hp_map[name] = max(0, int(hp_map[name]) + int(op.get("change", 0)))


def run_combat(client, model, extra_body, contract, scenario, max_iter=20):
    sc = scenario
    hp = dict(sc["hp"])
    messages = [
        {"role": "system", "content": contract},
        {"role": "user", "content": sc["kickoff"]},
    ]
    tools = openai_tools()
    metrics = {"model": model, "resolve_calls": 0, "actions_total": 0, "actions_valid": 0,
               "action_types": {}, "initiative_first": None, "acted_on_eliminated": 0,
               "completed": False, "iterations": 0, "in_tok": 0, "out_tok": 0, "errors": [],
               "turn_log": []}
    eliminated = set()
    # GPT-5-series chat.completions rejects max_tokens — wants max_completion_tokens.
    tok_param = {"max_completion_tokens": 1500} if model.startswith("gpt-5") else {"max_tokens": 1500}
    for it in range(max_iter):
        metrics["iterations"] = it + 1
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=tools, tool_choice="auto",
                **tok_param, **extra_body)
        except Exception as e:  # noqa: BLE001
            metrics["errors"].append(str(e)[:160]); break
        u = resp.usage
        metrics["in_tok"] += getattr(u, "prompt_tokens", 0) or 0
        metrics["out_tok"] += getattr(u, "completion_tokens", 0) or 0
        msg = resp.choices[0].message
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                        for tc in (msg.tool_calls or [])] or None})
        if not msg.tool_calls:
            break  # model narrated without a tool call — stop
        done = False
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            if name == "end_exchange":
                metrics["completed"] = True
                done = True
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "ack"})
                continue
            if name != "resolve_mechanics":
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "unknown tool"})
                continue
            metrics["resolve_calls"] += 1
            actions = args.get("actions") or []
            metrics["turn_log"].append([f"{a.get('type')}({a.get('character','?')}→{a.get('target','-')}"
                                        f" stat={a.get('stat','∅')}/{a.get('stat_value','∅')}"
                                        f" skill={a.get('skill','∅')}/{a.get('skill_value','∅')}"
                                        f" wpn={a.get('weapon','∅')})" for a in actions])
            if metrics["initiative_first"] is None:
                metrics["initiative_first"] = bool(actions) and actions[0].get("type") == "initiative"
            res = resolve_actions(actions, combatant_hp=hp,
                                  edgerunner_states=sc["edgerunner_states"],
                                  character_states=sc["character_states"])
            for a in actions:
                metrics["actions_total"] += 1
                metrics["action_types"][a.get("type", "?")] = metrics["action_types"].get(a.get("type", "?"), 0) + 1
                if a.get("character") in eliminated or a.get("target") in eliminated:
                    metrics["acted_on_eliminated"] += 1
            for r in (res.get("results") or []):
                if isinstance(r, dict) and not (r.get("error") or r.get("player_error") or r.get("invalid")):
                    metrics["actions_valid"] += 1
            _apply_hp_ops(hp, res.get("state_ops"))
            for nm, h in hp.items():
                if h <= 0:
                    eliminated.add(nm)
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps({"results": res.get("results"), "hp": hp})[:4000]})
        if done:
            break
        if all(hp[n] <= 0 for n in hp if n != "Kessler"):
            metrics["completed"] = True
            break
    metrics["final_hp"] = hp
    return metrics


def dry_run(models):
    print("\n=== COMBAT BENCH — DRY RUN (no API calls) ===\n")
    sc = firefight_scenario()
    sys_tok = tk(CPRED_COMBAT_CONTRACT + COMBAT_GUARDS)
    kickoff_tok = tk(sc["kickoff"])
    R = 9          # assumed resolve_mechanics turns (1 initiative + ~8 attacks over 2-3 rounds)
    avg_tool_result = 350   # tokens per tool result fed back
    avg_out = 300           # output tokens per model turn
    # input grows each iteration (history accumulates): iter i input ~ base + i*(action+result)
    base = sys_tok + kickoff_tok
    total_in = sum(base + i * (avg_tool_result + avg_out + 80) for i in range(R + 1))
    total_out = (R + 1) * avg_out
    print(f"  assumptions: ~{R} resolve turns, system+contract={sys_tok} tok, growing history\n")
    grand = 0.0
    for model in models:
        pin, pout = PRICES.get(model, (1.0, 5.0))
        cost = total_in * pin / 1e6 + total_out * pout / 1e6
        grand += cost
        print(f"  {model:26s} in~{total_in:>8,}  out~{total_out:>6,}  est~${cost:5.2f}")
    print(f"\n  per firefight encounter, 1 run each: ~${grand:.2f}")
    print(f"  Suggest 3 encounters x 2-3 reps for stable scoring -> ~${grand*7:.2f} total (still small).")
    print("  (GPT-5.4 price is an estimate; V3.2 is the cheap one.)\n")


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    ap.add_argument("--models", default="deepseek/deepseek-v3.2,gpt-5.4")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args(argv)
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if a.dry_run:
        dry_run(models)
        return 0

    from openai import OpenAI
    keys = load_keys(a.keys)
    or_client = OpenAI(api_key=keys["openrouter"], base_url="https://openrouter.ai/api/v1")
    or_route = {"extra_body": {"provider": {"ignore": ["sambanova"], "order": ["deepinfra", "novita", "fireworks"], "allow_fallbacks": True}}}
    ds_direct = OpenAI(api_key=keys["deepseek"], base_url="https://api.deepseek.com")
    clients = {
        "deepseek/deepseek-v3.2": (or_client, or_route),
        "deepseek/deepseek-v4-pro": (or_client, or_route),
        "deepseek-v4-pro": (ds_direct, {}),     # first-party direct
        "gpt-5.4": (OpenAI(api_key=keys["openai"]), {}),
    }
    ff = firefight_scenario()
    mc = midcombat_scenario()
    el = elimination_scenario()
    guarded = CPRED_COMBAT_CONTRACT + COMBAT_GUARDS
    # V4 Pro on DeepSeek-DIRECT (first-party, no third-party quantization/version drift).
    # GPT-5.4 (incumbent) + V3.2 numbers already recorded in REPORT.md.
    # NO-guards first-exchange = native-competence test (V3.2's init death-loop).
    # elimination = retarget / stop-on-dead test (enemies actually drop).
    configs = [
        ("v4-pro-direct elimination (retarget/stop-on-dead)", "deepseek-v4-pro", guarded, el),
        ("v4-pro-direct mid-combat (order injected)", "deepseek-v4-pro", guarded, mc),
        ("v4-pro-direct first-exchange (+guards)", "deepseek-v4-pro", guarded, ff),
        ("v4-pro-direct first-exchange (NO guards)", "deepseek-v4-pro", CPRED_COMBAT_CONTRACT, ff),
    ]
    all_runs = {}
    for label, model, contract, scenario in configs:
        client, extra = clients.get(model, (None, {}))
        if client is None:
            print(f"!! no client for {model}"); continue
        runs = []
        for rep in range(a.reps):
            print(f"\n--- {label} rep{rep} ---")
            m = run_combat(client, model, extra, contract, scenario)
            runs.append(m)
            print(f"   resolve_calls={m['resolve_calls']} valid={m['actions_valid']}/{m['actions_total']} "
                  f"init_first={m['initiative_first']} redundant_init={max(0, m['action_types'].get('initiative',0)-1)} "
                  f"acted_on_dead={m['acted_on_eliminated']} completed={m['completed']} iters={m['iterations']}")
            for i, t in enumerate(m["turn_log"]):
                print(f"      turn {i}: {t}")
            print(f"      final_hp: {m['final_hp']}")
            if m["errors"]:
                print(f"   errors: {m['errors'][:2]}")
        all_runs[label] = runs

    print("\n===================== COMBAT REPORT =====================")
    for label, runs in all_runs.items():
        n = len(runs)
        if not n:
            continue
        model = runs[0]["model"]
        valid = sum(r["actions_valid"] for r in runs)
        tot = sum(r["actions_total"] for r in runs)
        comp = sum(1 for r in runs if r["completed"])
        initf = sum(1 for r in runs if r["initiative_first"])
        redun = sum(max(0, r["action_types"].get("initiative", 0) - 1) for r in runs)
        dead = sum(r["acted_on_eliminated"] for r in runs)
        cost = sum(r["in_tok"] * PRICES.get(model, (1, 5))[0] / 1e6 + r["out_tok"] * PRICES.get(model, (1, 5))[1] / 1e6 for r in runs)
        print(f"  {label:26s} valid {100*valid/tot if tot else float('nan'):4.0f}% ({valid}/{tot})  "
              f"init-first {initf}/{n}  redundant-init {redun}  completed {comp}/{n}  acted-on-dead {dead}  ~${cost:.2f}")
    with open("bench/combat_results.json", "w") as f:
        json.dump(all_runs, f, indent=2, default=str)
    print("\nsaved bench/combat_results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
