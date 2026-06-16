"""Prompt-engineering probe: can DeepSeek V3.2's flag-honoring (~48%) and
narration-omission (~29%) be improved by prompting, or is it a capability wall?

Same delta tests + neutral comparative judge as dm_judgment, run on DeepSeek
under three prompt variants:
  - baseline:          current setup (no extra directive)
  - flagcheck:         explicit pre-narration directive (check flags, honor active
                       ones, narration required + non-empty)
  - flagcheck_example: flagcheck + a NEUTRAL worked example using a flag that is
                       NOT in the test set (CURFEW_ACTIVE) — no answer leakage

Sonnet runs baseline-only as the reference ceiling. Reports honoring% (delta
pass rate) and omission% per variant.

    python -m bench.ds_prompt_probe --keys ../data/users/printer/api_keys.json
"""
from __future__ import annotations

import argparse
import sys
import traceback

import anthropic

from bench.run import MODEL_REGISTRY, load_keys, run_once
from bench.pricing import SpendMeter
from bench.dm_judgment import (
    DELTA_TESTS, DOC_TOOLS, GM_FRAMING, SYNTHETIC_FLAGS_MD,
    build_decision_flags_injection, judge_delta, HaikuMeter, _run_once_retry,
)
from game_systems.cpred import SINGLE_AGENT_STATE_CONTRACT

DIRECTIVE = (
    "[GM DIRECTIVE — read before narrating]\n"
    "1. Check the [DECISION FLAGS] block. Any flag that is ACTIVE (true / a set value) and whose "
    "consequence bears on what the player is doing THIS turn MUST visibly shape your narration — "
    "narrate the consequence; never narrate as if an active flag were unset or absent.\n"
    "2. Your `narration` field is REQUIRED and must be non-empty, in-fiction prose for every in-fiction turn.\n"
)
NEUTRAL_EXAMPLE = (
    "Worked example (unrelated flag): if a flag like CURFEW_ACTIVE = true is set and the crew walks the "
    "streets at night, a patrol stops them for breaking curfew — you would NOT narrate an uneventful stroll. "
    "Apply that same discipline to whatever flags are actually active now.\n"
)

VARIANTS = {
    "baseline": "",
    "flagcheck": DIRECTIVE,
    "flagcheck_example": DIRECTIVE + NEUTRAL_EXAMPLE,
}


def narrate(provider, client, flags, user_input, directive, max_tries=3):
    flags_inj = build_decision_flags_injection(flags)
    plot_ref = "[PLOT REFERENCE — campaign flag rules]\n" + SYNTHETIC_FLAGS_MD.strip() + "\n[/PLOT REFERENCE]"
    system = GM_FRAMING + (directive + "\n" if directive else "") + SINGLE_AGENT_STATE_CONTRACT
    parts = [plot_ref]
    if flags_inj:
        parts.append(flags_inj)
    parts.append(user_input)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(parts)}]
    params = provider.build_request(messages, "bench", None, "bench", is_free_chat=False)
    params["tools"] = DOC_TOOLS
    params.pop("thinking", None)
    params.pop("output_config", None)
    params["tool_choice"] = {"type": "tool", "name": "report_state"}

    def extract(u):
        for t in (u.get("tool_uses") or []):
            if t.get("name") == "report_state":
                return (t.get("input") or {}).get("narration") or ""
        return u.get("content") or ""

    first_empty = False
    best, usage = "", {}
    for attempt in range(max_tries):
        usage = _run_once_retry(provider, client, params)
        n = extract(usage)
        if attempt == 0 and not n.strip():
            first_empty = True
        if n.strip():
            best = n
            break
    return best, usage, first_empty


def run_variant(model_id, provider, client, directive, reps, meter, hk_client, hk_meter):
    turns = raw_empty = 0
    delta_pass = delta_total = 0
    for dt in DELTA_TESTS:
        for rep in range(reps):
            try:
                non, u1, fe1 = narrate(provider, client, dt["on_flags"], dt["user"], directive)
                meter.add(model_id, provider, u1)
                noff, u2, fe2 = narrate(provider, client, dt["off_flags"], dt["user"], directive)
                meter.add(model_id, provider, u2)
            except Exception as e:  # noqa: BLE001
                print(f"      {dt['id']} rep{rep} call-error {str(e)[:80]}")
                continue
            turns += 2
            raw_empty += int(fe1) + int(fe2)
            if not non.strip() or not noff.strip():
                continue
            winner, _ = judge_delta(hk_client, dt["consequence"], non, noff, hk_meter)
            ok = (winner == "on" and dt["direction"] == "on_more") or (winner == "off" and dt["direction"] == "off_more")
            delta_total += 1
            delta_pass += int(ok)
    honor = 100 * delta_pass / delta_total if delta_total else float("nan")
    omit = 100 * raw_empty / turns if turns else float("nan")
    return honor, omit, delta_pass, delta_total, raw_empty, turns


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    ap.add_argument("--models", default="deepseek-v3.2-or,deepseek-v4-flash-or")
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--caps", default="deepseek-v3.2-or=5,deepseek-v4-flash-or=5,claude-sonnet-4.6=5")
    a = ap.parse_args(argv)
    caps = {}
    for kv in a.caps.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            caps[k.strip()] = float(v)
    keys = load_keys(a.keys)
    hk_client = anthropic.Anthropic(api_key=keys["anthropic"])
    hk_meter = HaikuMeter()
    meter = SpendMeter(caps=caps)

    rows = []
    for mid in [m.strip() for m in a.models.split(",") if m.strip() in MODEL_REGISTRY]:
        cls, kn = MODEL_REGISTRY[mid]
        prov = cls(); cli = prov.get_client(keys[kn])
        for vname, directive in VARIANTS.items():
            print(f"\n=== {mid} / {vname} ===")
            honor, omit, dp_, dt_, re_, tu_ = run_variant(mid, prov, cli, directive, a.reps, meter, hk_client, hk_meter)
            rows.append((mid, vname, honor, omit, dp_, dt_, re_, tu_))
            print(f"   honoring={honor:.0f}% ({dp_}/{dt_})   omission={omit:.0f}% ({re_}/{tu_})")
    # Sonnet baseline reference
    scls, skey = MODEL_REGISTRY["claude-sonnet-4.6"]
    sp = scls(); sc = sp.get_client(keys[skey])
    print(f"\n=== claude-sonnet-4.6 / baseline (reference) ===")
    honor, omit, dp_, dt_, re_, tu_ = run_variant("claude-sonnet-4.6", sp, sc, "", max(2, a.reps // 2), meter, hk_client, hk_meter)
    rows.append(("claude-sonnet-4.6", "baseline", honor, omit, dp_, dt_, re_, tu_))
    print(f"   honoring={honor:.0f}% ({dp_}/{dt_})   omission={omit:.0f}% ({re_}/{tu_})")

    print("\n===================== PROMPT PROBE =====================")
    print(f"{'model':20s} {'variant':18s} {'honoring':>9s} {'omission':>9s}")
    for m, v, h, o, dpv, dtv, rev, tuv in rows:
        print(f"{m:20s} {v:18s} {h:8.0f}% {o:8.0f}%")
    print(f"\ncandidate spend: " + ", ".join(f"{k}=${v:.2f}" for k, v in meter.spent.items()))
    print(f"haiku judge spend: ${hk_meter.cost:.2f}")
    print(f"probe total: ${meter.total() + hk_meter.cost:.2f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
