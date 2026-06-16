"""Benchmark runner. Run from the backend/ directory.

    # Free: estimate tokens + per-provider funding guidance, no API calls
    python -m bench.run --dry-run

    # Real run (spends money on your keys; hard-capped per provider)
    python -m bench.run --suite all --keys /path/to/api_keys.json

    # Single model smoke test
    python -m bench.run --suite tools --models deepseek-v3.2 --tool-turns 10 --keys ...

Keys: pass --keys pointing at an api_keys.json ({"anthropic": "...",
"deepseek": "...", "google": "..."}) or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY
/ GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

from providers.anthropic_provider import AnthropicSonnet46Provider
from providers.deepseek_provider import DeepSeekProvider
from providers.gemini_provider import GeminiProvider

from bench import scenarios as S
from bench.pricing import SpendMeter, BudgetExceeded, estimate_cost, cost_of, DEFAULT_CAPS

MODEL_REGISTRY = {
    "claude-sonnet-4.6": (AnthropicSonnet46Provider, "anthropic"),
    "deepseek-v3.2": (DeepSeekProvider, "deepseek"),
    "gemini-3.1-pro": (GeminiProvider, "google"),
}

EST_OUT = {"tool": 1500, "probe": 120}  # conservative per-call output token estimate


# ---- key loading ------------------------------------------------------------
def load_keys(path: str | None) -> dict:
    keys = {}
    if path and os.path.exists(path):
        with open(path) as f:
            keys.update(json.load(f))
    keys.setdefault("anthropic", os.environ.get("ANTHROPIC_API_KEY", ""))
    keys.setdefault("deepseek", os.environ.get("DEEPSEEK_API_KEY", ""))
    keys.setdefault("google", os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""))
    return keys


# ---- request planning (shared by dry-run and real run) ----------------------
def iter_requests(provider, suite, depths, tool_turns, retention_repeats, full_contract):
    """Yield dicts: {kind, meta, params}. kind in {'tool','probe'}."""
    if suite in ("tools", "all"):
        n = len(S.TOOL_SCENARIOS)
        for i in range(tool_turns):
            sc = S.TOOL_SCENARIOS[i % n]
            yield {"kind": "tool", "meta": sc,
                   "params": S.tool_request(provider, sc, full_contract=full_contract)}
    if suite in ("retention", "all"):
        for depth in depths:
            for rep in range(retention_repeats):
                base, probes = S.build_retention_context(provider, depth, seed=7 + rep)
                for p in probes:
                    yield {"kind": "probe", "meta": {"probe": p, "depth": depth, "rep": rep},
                           "params": S.retention_request(provider, base, p)}


# ---- serialization for token estimation -------------------------------------
def _system_text(system):
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
    return ""


def serialize_request(params) -> str:
    parts = [_system_text(params.get("system"))]
    for m in params.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("text"):
                    parts.append(b["text"])
    if params.get("tools"):
        parts.append(json.dumps(params["tools"]))
    return "\n".join(p for p in parts if p)


# ---- streaming execution ----------------------------------------------------
def run_once(provider, client, params) -> dict:
    content = []
    usage = None
    for ev in provider.send_request_stream(client, params):
        if ev.event_type == "content_delta" and ev.content:
            content.append(ev.content)
        elif ev.event_type == "done":
            usage = ev.usage or {}
    usage = usage or {}
    if not usage.get("content"):
        usage["content"] = "".join(content)
    return usage


# ---- dry run ----------------------------------------------------------------
def dry_run(models, args):
    print("\n=== DRY RUN (no API calls) -- token + cost estimate ===\n")
    grand = {}
    for model_id in models:
        cls, _ = MODEL_REGISTRY[model_id]
        provider = cls()
        in_tok = out_tok = 0
        ncalls = 0
        for req in iter_requests(provider, args.suite, args.depths, args.tool_turns,
                                 args.retention_repeats, args.full_contract):
            in_tok += provider.count_tokens(serialize_request(req["params"]))
            out_tok += EST_OUT[req["kind"]]
            ncalls += 1
        est = estimate_cost(provider, in_tok, out_tok)
        grand[model_id] = est
        cap = args.caps.get(model_id, DEFAULT_CAPS.get(model_id))
        fund = max(est * 1.5, (cap or 0))
        print(f"{model_id:20s}  calls={ncalls:4d}  in~{in_tok:>10,}  out~{out_tok:>8,}  "
              f"est~${est:6.2f}  (cap ${cap:.0f}; fund >= ${fund:5.0f})")
    print(f"\n  TOTAL est (no-cache, conservative): ${sum(grand.values()):.2f}")
    print("  Real cost is lower — retention reuses a cached prefix on all three providers.\n")
    print("  Recommended account funding (headroom over estimate):")
    print("    DeepSeek (new):  $10   |  Anthropic: ~$20 avail  |  Google AI: ~$15 avail\n")


# ---- real run ---------------------------------------------------------------
def real_run(models, keys, args):
    meter = SpendMeter(caps=dict(DEFAULT_CAPS, **args.caps))
    results = {}
    for model_id in models:
        cls, key_name = MODEL_REGISTRY[model_id]
        key = keys.get(key_name)
        if not key:
            print(f"!! skipping {model_id}: no '{key_name}' key")
            continue
        provider = cls()
        client = provider.get_client(key)
        tool_results, probe_results = [], []
        capped = False
        print(f"\n--- {model_id} ---")
        try:
            for req in iter_requests(provider, args.suite, args.depths, args.tool_turns,
                                     args.retention_repeats, args.full_contract):
                try:
                    usage = run_once(provider, client, req["params"])
                except Exception as e:  # noqa: BLE001 - one bad call shouldn't kill the run
                    print(f"   call error ({req['kind']}): {e}")
                    continue
                meter.add(model_id, provider, usage)
                if req["kind"] == "tool":
                    r = S.score_tool_turn(req["meta"], usage)
                    tool_results.append(r)
                    mark = "ok" if r["got_expected_tool"] and r["schema_valid"] else ("TEXT!" if not r["emitted_structured"] else "?")
                    print(f"   tool {r['scenario']:16s} {mark:5s} raw={r['raw_first_ok']} valid={r['schema_valid']} retries={r['retries']}")
                else:
                    p = req["meta"]
                    r = S.score_probe(p["probe"], usage)
                    r["depth"] = p["depth"]
                    probe_results.append(r)
                    print(f"   probe {r['key']:14s} depth={p['depth']:>7,} {'PASS' if r['correct'] else 'MISS'}  '{r['answer']}'")
        except BudgetExceeded as e:
            capped = True
            print(f"   !! CAP HIT — stopping {model_id}: {e}")
        results[model_id] = {"tools": tool_results, "probes": probe_results,
                             "spent": round(meter.spent.get(model_id, 0.0), 4), "capped": capped}
    print_report(results, meter)
    with open(os.path.join(os.path.dirname(__file__), "last_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved bench/last_results.json   |   TOTAL spent ${meter.total():.2f}")


def _rate(items, pred):
    items = list(items)
    return (sum(1 for x in items if pred(x)) / len(items)) if items else float("nan")


def print_report(results, meter):
    print("\n========================= REPORT =========================\n")
    print("SUITE A — tool / dice reliability")
    print(f"  {'model':20s} {'raw struct%':>11s} {'got-tool%':>10s} {'valid%':>8s} {'recovered':>10s} {'mean retries':>13s}")
    for mid, r in results.items():
        t = r["tools"]
        if not t:
            continue
        raw = _rate(t, lambda x: x["raw_first_ok"]) * 100
        gt = _rate(t, lambda x: x["got_expected_tool"]) * 100
        val = _rate(t, lambda x: x["schema_valid"]) * 100
        rec = sum(1 for x in t if x["recovered"])
        mr = sum(x["retries"] for x in t) / len(t)
        print(f"  {mid:20s} {raw:10.1f}% {gt:9.1f}% {val:7.1f}% {rec:7d}/{len(t):<4d} {mr:13.2f}")
    print("\nSUITE B — persistent-state / plot-flag retention (recall accuracy)")
    depths = sorted({p["depth"] for r in results.values() for p in r["probes"]})
    header = "  " + f"{'model':20s}" + "".join(f"{d//1000:>6d}k" for d in depths)
    print(header)
    for mid, r in results.items():
        if not r["probes"]:
            continue
        cells = []
        for d in depths:
            at = [p for p in r["probes"] if p["depth"] == d]
            acc = _rate(at, lambda x: x["correct"]) * 100
            cells.append(f"{acc:5.0f}%")
        print("  " + f"{mid:20s}" + "".join(f"{c:>7s}" for c in cells))
    print("\nSpend:")
    for mid, r in results.items():
        flag = "  (CAPPED)" if r.get("capped") else ""
        print(f"  {mid:20s} ${r['spent']:.2f}{flag}")


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", choices=["tools", "retention", "all"], default="all")
    ap.add_argument("--models", default=",".join(MODEL_REGISTRY),
                    help="comma-separated subset of: " + ", ".join(MODEL_REGISTRY))
    ap.add_argument("--keys", default=None, help="path to api_keys.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tool-turns", type=int, default=150)
    ap.add_argument("--retention-repeats", type=int, default=2)
    ap.add_argument("--depths", default="80000,100000,125000")
    ap.add_argument("--full-contract", action="store_true",
                    help="use the real (large) turn contract for Suite A")
    ap.add_argument("--caps", default="", help="override caps, e.g. deepseek-v3.2=5,gemini-3.1-pro=10")
    a = ap.parse_args(argv)
    a.depths = [int(x) for x in a.depths.split(",") if x.strip()]
    a.models = [m.strip() for m in a.models.split(",") if m.strip() in MODEL_REGISTRY]
    caps = {}
    for kv in a.caps.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            caps[k.strip()] = float(v)
    a.caps = caps
    return a


def main(argv):
    args = parse_args(argv)
    if not args.models:
        print("No valid models selected.")
        return 2
    if args.dry_run:
        dry_run(args.models, args)
        return 0
    keys = load_keys(args.keys)
    real_run(args.models, keys, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
