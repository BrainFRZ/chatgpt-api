"""Context-rot test on REAL Broken Orbit context.

Pours the real Chapter 1 context (system + history) + project uploads into the
model up to 103k and 125k tokens, with "needle" continuity facts inserted at
depths 10%-85%, then probes:
  - RETRIEVAL: verbatim recall of a buried fact
  - REASONING: derive an answer over a buried fact (math / time / consequence)
Control = needles only (minimal context) = the ceiling; rot = control - haystack.

Models: V3.2 (OpenRouter), V4 Flash (DeepSeek-direct, full ctx), Sonnet (ref).

    python -m bench.context_rot_v32 --keys ../data/users/printer/api_keys.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time

import tiktoken

from bench.run import load_keys

ENC = tiktoken.get_encoding("cl100k_base")
def tk(s): return len(ENC.encode(s or ""))

BASE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "users", "printer", "projects", "Broken Orbit")

NEUTRAL_SYS = ("You are a careful assistant. Below is the full context of an ongoing tabletop "
               "RPG campaign: rules, character info, and the play log. Read all of it, then answer "
               "the QUESTIONS at the end accurately and specifically. If a fact appears anywhere in "
               "the context, use it.")

# (id, depth fraction, the continuity note inserted into the haystack)
NEEDLES = [
    ("airlock", 0.10, "[DM continuity note: the Helios cargo airlock manual override code is 7-7-3-1-Q. Sara scrawled it on the inside of her wrist.]"),
    ("allergy", 0.28, "[Continuity: Sara's cousin Mara is severely allergic to synth-citrus — even a trace triggers anaphylaxis.]"),
    ("verge",   0.48, "[Continuity: the Verge fixer will only take a meeting between 02:00 and 03:00 ship-time — never outside that window.]"),
    ("oxygen",  0.66, "[Continuity: the lifepod holds exactly 3 spare oxygen candles, and each candle lasts 40 minutes.]"),
    ("codeword",0.84, "[Continuity: the crew's emergency abort codeword for this op is GLASSHOUSE.]"),
]

def _norm(a): return (a or "").lower()
PROBES = [
    ("airlock",  "retrieval", "What is the Helios cargo airlock manual override code?",
     lambda a: "7731" in _norm(a).replace("-", "").replace(" ", "")),
    ("allergy",  "retrieval", "What is Sara's cousin Mara allergic to?",
     lambda a: "citrus" in _norm(a)),
    ("verge",    "retrieval", "What ship-time window will the Verge fixer take a meeting in?",
     lambda a: "2:00" in _norm(a) or "02:00" in _norm(a) or ("2" in a and "3" in a and "ship" in _norm(a))),
    ("oxygen",   "retrieval", "How many spare oxygen candles does the lifepod have, and how long does each last?",
     lambda a: "3" in a and "40" in a),
    ("codeword", "retrieval", "What is the crew's emergency abort codeword for this op?",
     lambda a: "glasshouse" in _norm(a)),
    ("oxy_math", "reasoning", "If all the lifepod's spare oxygen candles are burned back-to-back, how many total minutes of breathable air do they provide? Give the number.",
     lambda a: "120" in a),
    ("verge_now","reasoning", "It is currently 04:30 ship-time. Can the crew still meet the Verge fixer right now? Answer yes or no and explain.",
     # robust: must conclude they CANNOT, and reference the window/time. (The
     # markdown-formatted "**No.**" at item 7 broke an earlier first-40-chars check.)
     lambda a: any(s in _norm(a) for s in ("no.", "no,", "no ", "no—", "no–", "**no", "cannot", "can't", "missed", "outside", "passed", "closed"))
               and ("02:00" in a or "2:00" in a or "03:00" in a or "3:00" in a or "window" in _norm(a))),
    ("allergy_rx","reasoning", "The crew wants to order Sara a synth-citrus cocktail with Mara present. Is there a problem? Explain.",
     lambda a: "citrus" in _norm(a) and ("aller" in _norm(a) or "mara" in _norm(a) or "anaphyla" in _norm(a))),
]


def load_segments():
    chat = json.load(open(os.path.join(BASE, "chat_Chapter 1.json"), encoding="utf-8"))
    byid = {m["id"]: m for m in chat["messages"]}
    cur, path = chat["current_leaf_id"], []
    while cur:
        m = byid.get(cur)
        if not m:
            break
        path.append(m); cur = m.get("parent_id")
    path.reverse()
    system = path[0]["content"]
    segs = []
    for m in path[1:]:
        who = "SARA" if m["role"] == "user" else "DM"
        segs.append(f"{who}: {m['content']}")
    for u in sorted(glob.glob(os.path.join(BASE, "uploads", "*.md"))):
        if "sync-conflict" in u:
            continue
        segs.append(f"[REFERENCE DOC: {os.path.basename(u)}]\n" + open(u, encoding="utf-8").read())
    return system, segs


def build_haystack(segs, target_tokens):
    acc, toks = [], 0
    for s in segs:
        acc.append(s); toks += tk(s)
        if toks >= target_tokens:
            break
    # insert needles at depth (descending so indices stay valid)
    for nid, depth, note in sorted(NEEDLES, key=lambda x: -x[1]):
        idx = max(0, min(len(acc), int(depth * len(acc))))
        acc.insert(idx, note)
    return "\n\n".join(acc)


def probe_block():
    lines = ["=== QUESTIONS (answer each, numbered) ==="]
    for i, (pid, kind, q, _) in enumerate(PROBES, 1):
        lines.append(f"{i}. {q}")
    return "\n".join(lines)


def ask_openai(client, model, system, user, max_tokens=1200):
    r = client.chat.completions.create(model=model, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return (r.choices[0].message.content or "").strip()


def ask_anthropic(client, model, system, user, max_tokens=1200):
    r = client.messages.create(model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if b.type == "text").strip()


def score(answer):
    return {pid: bool(chk(answer)) for pid, kind, q, chk in PROBES}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    ap.add_argument("--targets", default="103000,125000")
    a = ap.parse_args(argv)
    keys = load_keys(a.keys)
    targets = [int(x) for x in a.targets.split(",")]

    from openai import OpenAI
    import anthropic
    or_client = OpenAI(api_key=keys["openrouter"], base_url="https://openrouter.ai/api/v1")
    ds_client = OpenAI(api_key=keys["deepseek"], base_url="https://api.deepseek.com")
    an_client = anthropic.Anthropic(api_key=keys["anthropic"])

    def call(model_key, system, user):
        if model_key == "v3.2":
            return ask_openai(or_client, "deepseek/deepseek-v3.2", system, user)
        if model_key == "v4flash":
            return ask_openai(ds_client, "deepseek-v4-flash", system, user)
        if model_key == "sonnet":
            return ask_anthropic(an_client, "claude-sonnet-4-6", system, user)

    system, segs = load_segments()
    # Include the real ~47k chat system prompt as part of the haystack content so
    # we actually reach 103k/125k (history+uploads alone only total ~83k). The
    # NEUTRAL_SYS instruction stays as the system role; the real system prompt is
    # realistic context the model would see in deployment.
    all_segs = [f"[CAMPAIGN SYSTEM / RULES]\n{system}"] + segs
    probes_txt = probe_block()
    needle_only = "\n\n".join(n[2] for n in NEEDLES) + "\n\n" + probes_txt

    models = [("v3.2", "DeepSeek V3.2"), ("v4flash", "DeepSeek V4 Flash"), ("sonnet", "Claude Sonnet 4.6 (ref)")]
    conditions = [("control", None)] + [(f"{t//1000}k", t) for t in targets]

    results = {}
    for mkey, mdisp in models:
        results[mkey] = {}
        for cname, target in conditions:
            if target is None:
                user = needle_only
                size = tk(NEUTRAL_SYS + user)
            else:
                hay = build_haystack(all_segs, target)
                user = hay + "\n\n" + probes_txt
                size = tk(NEUTRAL_SYS + user)
            try:
                ans = call(mkey, NEUTRAL_SYS, user)
                sc = score(ans)
            except Exception as e:  # noqa: BLE001
                print(f"  {mdisp} @ {cname}: ERROR {str(e)[:120]}")
                results[mkey][cname] = None
                continue
            results[mkey][cname] = {"score": sc, "size": size, "answer": ans[:1500]}
            ret = [pid for pid, k, q, c in PROBES if k == "retrieval"]
            rea = [pid for pid, k, q, c in PROBES if k == "reasoning"]
            rret = sum(sc[p] for p in ret); rrea = sum(sc[p] for p in rea)
            print(f"  {mdisp:24s} @ {cname:8s} (~{size//1000}k tok)  "
                  f"retrieval {rret}/{len(ret)}  reasoning {rrea}/{len(rea)}  "
                  f"miss={[p for p in sc if not sc[p]]}")
            time.sleep(0.3)

    # report
    print("\n===================== CONTEXT-ROT REPORT (V3.2 focus) =====================")
    ret_ids = [p for p, k, q, c in PROBES if k == "retrieval"]
    rea_ids = [p for p, k, q, c in PROBES if k == "reasoning"]
    print(f"{'model':26s} {'condition':10s} {'size':>6s} {'retrieval':>10s} {'reasoning':>10s}")
    for mkey, mdisp in models:
        for cname, _ in conditions:
            r = results[mkey].get(cname)
            if not r:
                print(f"{mdisp:26s} {cname:10s}   (failed)")
                continue
            sc = r["score"]
            rr = f"{sum(sc[p] for p in ret_ids)}/{len(ret_ids)}"
            re_ = f"{sum(sc[p] for p in rea_ids)}/{len(rea_ids)}"
            print(f"{mdisp:26s} {cname:10s} {r['size']//1000:>5}k {rr:>10s} {re_:>10s}")
    with open(os.path.join(os.path.dirname(__file__), "context_rot_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print("\nsaved bench/context_rot_results.json (includes full answers)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
