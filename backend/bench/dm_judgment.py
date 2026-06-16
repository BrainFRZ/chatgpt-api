"""DM-judgment / plot-adherence suite — the core "can it DM?" test.

Faithful to the real cpred architecture:
- WRITE (flag recording): the candidate does NOT emit plot_ops (the contract
  forbids it). It NARRATES; we then run the REAL Haiku side-agent
  (flag_agent.determine_flag_ops) on that narration and check the right flag
  fired (TP) / a numeric flag incremented / no false flag (FP control). This
  measures whether the candidate narrates flag-worthy events clearly enough for
  your tracker to catch them.
- READ (flag honoring): we inject the real [DECISION FLAGS] block and measure
  whether the candidate's narration HONORS the flags.

  Most read tests are DELTA tests: run the SAME turn under flag-ON and flag-OFF
  and have a neutral Haiku judge say which narration shows the consequence MORE.
  The model is its own control, so we measure the *behavioral delta the flag
  causes* — bias-resistant and far more robust than absolute "is there X?"
  judging (good cyberpunk prose is always a little tense). Presentation order is
  alternated across votes to cancel position bias.

  A couple of read tests have no natural on/off pair (branch exclusivity) and use
  an absolute 3-vote judge.

Sonnet runs as a thin fairness anchor: if the anchor fails a test, the test is
miscalibrated, not the model.

Run from backend/:
    python -m bench.dm_judgment --keys ../data/users/printer/api_keys.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback

import anthropic

from bench.run import MODEL_REGISTRY, load_keys, run_once
from bench.pricing import SpendMeter, BudgetExceeded
from game_systems.cpred import SINGLE_AGENT_STATE_CONTRACT, STATE_REPORT_TOOL
from game_systems.cpred_mechanics import RESOLVE_MECHANICS_TOOL
from flag_agent import determine_flag_ops, compute_flag_agent_cost

DOC_TOOLS = [RESOLVE_MECHANICS_TOOL, STATE_REPORT_TOOL]
JUDGE_MODEL = "claude-haiku-4-5"
JUDGE_VOTES = 3
GM_FRAMING = "You are the Game Master for an ongoing Cyberpunk RED campaign, running in single-agent mode.\n\n"
_TRANSIENT = ("503", "unavailable", "overloaded", "429", "rate_limit", "ratelimit", "timeout", "disconnect")

CANONICAL_FLAGS = ["NANOLUX_ALERT", "MILITECH_PACT", "VAULT_SPIKE", "MAINLINE_HEAT", "DISGUISE_ACTIVE"]

SYNTHETIC_FLAGS_MD = """# Campaign Decision Flags — NanoLux / Broken Orbit Arc

## NANOLUX_ALERT  (boolean: true / false)
Whether NanoLux corporate security considers the crew a security risk.
Trigger -> TRUE: the crew brings a flagged/unauthorized associate (e.g. Echo, who is on NanoLux's watchlist) into a NanoLux facility beyond the public lobby, OR otherwise trips NanoLux security (forced entry, spoofed credentials, violence on premises).
Do NOT set on ordinary authorized visits (valid daypasses, public lobby only, no watchlisted associates).
Consequence once TRUE: NanoLux security treats the crew as suspects on every later visit — ID shakedowns, mandatory escorts, denied access to secure floors, possible detention.

## MILITECH_PACT  (branch: allied / burned / neutral)
Permanent fork: the crew's stance toward their Militech contact in the Broken Orbit job.
- allied: sided with Militech / delivered the data to them.
- burned: double-crossed Militech (leaked, withheld, or sold the data elsewhere, e.g. to the Voodoo Boys).
- neutral: walked away without committing.
Once resolved it cannot change. NPCs aligned with the un-chosen side must not treat the crew as allies of that side.

## VAULT_SPIKE  (boolean: true / false)
Whether the crew has obtained the black-ICE access spike from the fixer Saburo.
Prerequisite gate: the NanoLux black vault CANNOT be breached without VAULT_SPIKE = true. Without it, the vault is inaccessible.

## MAINLINE_HEAT  (numeric: 0-10)
Citywide corporate / NCPD heat on the crew. Increment (emit the new post-turn total) when the crew is made on camera, leaves bodies, or trips corporate security. Higher heat = more patrols, warier contacts, worse prices.

## DISGUISE_ACTIVE  (boolean: true / false)
Whether the crew is currently running effective disguises / spoofed biometrics.
Consequence: when active, routine corporate ID checks (including a NANOLUX_ALERT shakedown) may be evaded or softened — security can't easily match them to the flagged crew.
"""


def _flag(value, severity="flag", decision=""):
    return {"value": value, "severity": severity, "decision": decision}


def build_decision_flags_injection(flags: dict) -> str:
    """Copied from pipeline.py to avoid importing the whole module."""
    if not flags:
        return ""
    pending, resolved = [], []
    for key, entry in flags.items():
        if isinstance(entry, dict):
            val = entry.get("value")
            desc = entry.get("decision", "")
            sev = entry.get("severity", "")
            if val is None:
                pending.append(f"- {key} = (pending)" + (f"  # {desc}" if desc else ""))
            else:
                resolved.append(f"- {key} = {val}" + (f" ({sev})" if sev and sev != "flag" else "") + (f"  # {desc}" if desc else ""))
        else:
            resolved.append(f"- {key} = {entry}")
    return "\n".join(["[DECISION FLAGS]"] + pending + resolved + ["[/DECISION FLAGS]"])


# --- WRITE scenarios (oracle-scored) -----------------------------------------
WRITE_SCENARIOS = [
    {"id": "w_alert_TP", "kind": "write_tp", "flags": {}, "expect_key": "NANOLUX_ALERT", "expect_val": "true",
     "scene": {"location": "NanoLux Tower lobby"},
     "user": "We bring Echo up through NanoLux security to the R&D floor to meet our contact — she's with us the whole way, badged in on a borrowed credential."},
    {"id": "w_alert_FP", "kind": "write_fp", "flags": {}, "forbid_key": "NANOLUX_ALERT",
     "scene": {"location": "NanoLux Tower lobby"},
     "user": "Just the two of us, valid day-passes, into the NanoLux public lobby to grab coffee and scope the place. Echo stays home."},
    {"id": "w_pact_allied", "kind": "write_tp", "flags": {}, "expect_key": "MILITECH_PACT", "expect_val": "allied",
     "scene": {"location": "Broken Orbit — meet"},
     "user": "We take Militech's offer. Hand them the Broken Orbit data, shake on it — we're their people now."},
    {"id": "w_pact_burned", "kind": "write_tp", "flags": {}, "expect_key": "MILITECH_PACT", "expect_val": "burned",
     "scene": {"location": "Broken Orbit — meet"},
     "user": "Screw Militech. We copy the data and sell it to the Voodoo Boys, leave Militech with nothing."},
    {"id": "w_heat", "kind": "write_inc", "flags": {"MAINLINE_HEAT": _flag("2")}, "expect_key": "MAINLINE_HEAT", "expect_over": 2.0,
     "scene": {"location": "NanoLux Tower lobby"},
     "user": "On the way out we shoot out the lobby camera and drop the two guards who spot us, then bolt."},
]

# --- DELTA read tests (comparative: ON vs OFF, model is its own control) ------
# direction: 'on_more' = the consequence should appear MORE in the ON narration;
#            'off_more' = MORE in the OFF narration.
DELTA_TESTS = [
    {"id": "gate", "direction": "on_more",
     "on_flags": {"NANOLUX_ALERT": _flag("true", decision="Crew brought Echo onto the R&D floor")},
     "off_flags": {"NANOLUX_ALERT": _flag("false")},
     "user": "We walk back into NanoLux through the main entrance and head for the upper floors.",
     "consequence": "NanoLux security treating THE CREW as flagged suspects — a targeted ID shakedown, an assigned escort, denied access to secure floors, or detention (not generic ambient security everyone passes)."},
    {"id": "prereq", "direction": "off_more",
     "on_flags": {"VAULT_SPIKE": _flag("true", decision="Got the spike from Saburo")},
     "off_flags": {"VAULT_SPIKE": _flag("false")},
     "user": "We head straight down to breach the NanoLux black vault right now.",
     "consequence": "the crew being BLOCKED from breaching the vault specifically for lack of the access spike / means of entry (the vault is inaccessible to them)."},
    {"id": "gaslight", "direction": "on_more",
     "on_flags": {"NANOLUX_ALERT": _flag("true", decision="Crew is flagged by NanoLux security")},
     "off_flags": {"NANOLUX_ALERT": _flag("false")},
     "user": "Security cleared us last time, so they just wave us straight through again, right? We head up.",
     "consequence": "security maintaining scrutiny on the crew and NOT simply waving them through (i.e. the GM holds the line against the player's assertion)."},
    {"id": "compound", "direction": "on_more",
     "on_flags": {"NANOLUX_ALERT": _flag("true", decision="Crew is flagged"),
                  "DISGUISE_ACTIVE": _flag("true", decision="Crew running spoofed biometrics")},
     "off_flags": {"NANOLUX_ALERT": _flag("true", decision="Crew is flagged"),
                   "DISGUISE_ACTIVE": _flag("false")},
     "user": "We walk into NanoLux through the main entrance.",
     "consequence": "the crew leaning on a DISGUISE / spoofed biometrics to evade or soften NanoLux security's scrutiny (using the disguise to get past the alert)."},
]

# --- ABSOLUTE read tests (no natural on/off pair) ----------------------------
ABS_READ = [
    {"id": "branch_burned",
     "flags": {"MILITECH_PACT": _flag("burned", severity="branch", decision="Crew sold the data to the Voodoo Boys")},
     "user": "We head to the safehouse rendezvous to regroup and see who shows.",
     "rubric": "The crew BURNED Militech (chose 'burned', not 'allied'). Does the narration AVOID portraying any Militech-aligned figure as a friendly ally of the crew? Answer YES if no Militech ally treats the crew as their people; answer NO only if the narration leaks the un-chosen 'allied' branch (e.g. a Militech handler greets them warmly as partners)."},
]


def _run_once_retry(provider, client, params, max_attempts=4):
    for attempt in range(max_attempts):
        try:
            return run_once(provider, client, params)
        except Exception as e:  # noqa: BLE001
            if any(s in str(e).lower() for s in _TRANSIENT) and attempt < max_attempts - 1:
                time.sleep(min(3 * (attempt + 1), 12))
                continue
            raise


def candidate_narration(provider, client, flags, user_input, max_tries=3):
    """Returns (narration, usage, first_empty). Retries up to max_tries to get a
    non-empty narration so flag-honoring can be judged on a real narration;
    first_empty records whether the FIRST attempt omitted narration (the
    production-relevant omission rate, before any app-side retry)."""
    flags_inj = build_decision_flags_injection(flags)
    # The GM legitimately has the campaign flag rules from the plot docs/uploads.
    # Without the consequence rules (e.g. "the vault needs the spike", "a disguise
    # softens an alert") the model can't honor gating it was never told about —
    # so inject the manifest, the way the app's plot material would.
    plot_ref = "[PLOT REFERENCE — campaign flag rules]\n" + SYNTHETIC_FLAGS_MD.strip() + "\n[/PLOT REFERENCE]"
    parts = [plot_ref]
    if flags_inj:
        parts.append(flags_inj)
    parts.append(user_input)
    user_content = "\n\n".join(parts)
    messages = [
        {"role": "system", "content": GM_FRAMING + SINGLE_AGENT_STATE_CONTRACT},
        {"role": "user", "content": user_content},
    ]
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


class HaikuMeter:
    def __init__(self):
        self.cost = 0.0

    def add_usage(self, usage):
        self.cost += compute_flag_agent_cost(usage)

    def add_resp(self, resp):
        u = resp.usage
        self.cost += compute_flag_agent_cost({
            "input_tokens": u.input_tokens + (getattr(u, "cache_read_input_tokens", 0) or 0),
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "output_tokens": u.output_tokens,
        })


def judge_abs(hk_client, rubric, narration, hk_meter):
    sysp = ("You are a strict, neutral evaluator of a tabletop RPG GM's narration. "
            "Judge ONLY what the narration shows, against the rubric. Answer with one word: YES or NO.")
    user = f"RUBRIC:\n{rubric}\n\nGM NARRATION:\n\"\"\"\n{narration}\n\"\"\"\n\nPer the rubric, answer YES or NO only."
    yes = 0
    for _ in range(JUDGE_VOTES):
        resp = hk_client.messages.create(model=JUDGE_MODEL, max_tokens=8, system=sysp,
                                         messages=[{"role": "user", "content": user}])
        hk_meter.add_resp(resp)
        if "".join(b.text for b in resp.content if b.type == "text").strip().lower().startswith("y"):
            yes += 1
    return yes * 2 > JUDGE_VOTES


def _parse_ab(text):
    t = text.strip().lower()
    if t.startswith("both"):
        return "both"
    if t.startswith("neither"):
        return "neither"
    if t.startswith("a"):
        return "A"
    if t.startswith("b"):
        return "B"
    return "neither"


def judge_delta(hk_client, consequence, narr_on, narr_off, hk_meter):
    """Comparative judge: which narration shows `consequence` more? Returns
    'on' / 'off' / 'both' / 'neither' by majority, with presentation order
    alternated across votes to cancel position bias."""
    sysp = ("You are a strict, neutral evaluator comparing two tabletop RPG GM narrations of the same moment. "
            "Decide which one shows the described thing MORE. Answer with exactly one token: A, B, BOTH, or NEITHER.")
    tally = {"on": 0, "off": 0, "both": 0, "neither": 0}
    for i in range(JUDGE_VOTES):
        on_is_A = (i % 2 == 0)
        first, second = (narr_on, narr_off) if on_is_A else (narr_off, narr_on)
        user = (f"Which narration shows the following MORE:\n{consequence}\n\n"
                f"NARRATION A:\n\"\"\"\n{first}\n\"\"\"\n\nNARRATION B:\n\"\"\"\n{second}\n\"\"\"\n\n"
                f"Answer A, B, BOTH, or NEITHER — which narration shows it more?")
        resp = hk_client.messages.create(model=JUDGE_MODEL, max_tokens=8, system=sysp,
                                         messages=[{"role": "user", "content": user}])
        hk_meter.add_resp(resp)
        ans = _parse_ab("".join(b.text for b in resp.content if b.type == "text"))
        if ans in ("both", "neither"):
            tally[ans] += 1
        elif ans == "A":
            tally["on" if on_is_A else "off"] += 1
        else:  # B
            tally["off" if on_is_A else "on"] += 1
    winner = max(tally, key=tally.get)
    return winner, tally


def score_write(hk_client, sc, narration, hk_meter, uploads_dir):
    plot_ops, usage = determine_flag_ops(
        hk_client, uploads_dir, CANONICAL_FLAGS, sc.get("flags", {}),
        sc["user"], narration, sc.get("scene"))
    hk_meter.add_usage(usage)
    set_map = {op.get("key"): str(op.get("value")).lower() for op in plot_ops if isinstance(op, dict)}
    if sc["kind"] == "write_tp":
        key = sc["expect_key"]
        ok = key in set_map and set_map[key] == str(sc["expect_val"]).lower()
        return ok, f"expect {key}={sc['expect_val']}; fired={set_map or '{}'}"
    if sc["kind"] == "write_inc":
        val = set_map.get(sc["expect_key"])
        ok = False
        if val is not None:
            try:
                ok = float(val) > sc["expect_over"]
            except (ValueError, TypeError):
                ok = False
        return ok, f"expect {sc['expect_key']}>{sc['expect_over']}; fired={set_map or '{}'}"
    bad = sc["forbid_key"] in set_map
    return (not bad), f"forbid {sc['forbid_key']}; fired={set_map or '{}'}"


def run_model(model_id, keys, repeats, meter, hk_client, hk_meter, uploads_dir):
    cls, key_name = MODEL_REGISTRY[model_id]
    key = keys.get(key_name)
    if not key:
        print(f"!! skip {model_id}: no '{key_name}' key")
        return None
    provider = cls()
    client = provider.get_client(key)
    res = {"write": [], "delta": [], "abs": [], "turns": 0, "raw_empty": 0, "gave_up_empty": 0}
    print(f"\n--- {model_id} (repeats={repeats}) ---")

    def narrate(flags, user):
        narr, usage, first_empty = candidate_narration(provider, client, flags, user)
        meter.add(model_id, provider, usage)
        res["turns"] += 1
        if first_empty:
            res["raw_empty"] += 1
        if not (narr or "").strip():
            res["gave_up_empty"] += 1
        return narr

    for rep in range(repeats):
        for sc in WRITE_SCENARIOS:
            try:
                narr = narrate(sc.get("flags", {}), sc["user"])
            except Exception as e:  # noqa: BLE001
                print(f"   {sc['id']:14s} rep{rep} call-error {str(e)[:80]}")
                continue
            if not (narr or "").strip():
                print(f"   {sc['id']:14s} rep{rep} OMIT (no narration)")
                continue
            ok, detail = score_write(hk_client, sc, narr, hk_meter, uploads_dir)
            res["write"].append({"id": sc["id"], "kind": sc["kind"], "rep": rep, "pass": ok,
                                 "detail": detail, "narration": narr[:900]})
            print(f"   {sc['id']:14s} rep{rep} {'PASS' if ok else 'FAIL'}" + ("" if ok else f"  [{detail}]"))
        for dt in DELTA_TESTS:
            try:
                narr_on = narrate(dt["on_flags"], dt["user"])
                narr_off = narrate(dt["off_flags"], dt["user"])
            except Exception as e:  # noqa: BLE001
                print(f"   {dt['id']:14s} rep{rep} call-error {str(e)[:80]}")
                continue
            if not (narr_on or "").strip() or not (narr_off or "").strip():
                print(f"   {dt['id']:14s} rep{rep} OMIT (no narration)")
                continue
            winner, tally = judge_delta(hk_client, dt["consequence"], narr_on, narr_off, hk_meter)
            ok = (winner == "on" and dt["direction"] == "on_more") or (winner == "off" and dt["direction"] == "off_more")
            res["delta"].append({"id": dt["id"], "rep": rep, "pass": ok, "winner": winner, "tally": tally,
                                 "narr_on": narr_on[:700], "narr_off": narr_off[:700]})
            print(f"   {dt['id']:14s} rep{rep} {'PASS' if ok else 'FAIL'}  (want {dt['direction']}, judge={winner} {tally})")
        for sc in ABS_READ:
            try:
                narr = narrate(sc.get("flags", {}), sc["user"])
            except Exception as e:  # noqa: BLE001
                print(f"   {sc['id']:14s} rep{rep} call-error {str(e)[:80]}")
                continue
            if not (narr or "").strip():
                print(f"   {sc['id']:14s} rep{rep} OMIT (no narration)")
                continue
            ok = judge_abs(hk_client, sc["rubric"], narr, hk_meter)
            res["abs"].append({"id": sc["id"], "rep": rep, "pass": ok})
            print(f"   {sc['id']:14s} rep{rep} {'PASS' if ok else 'FAIL'}")
    return res


def _rate(items):
    items = list(items)
    return (100 * sum(1 for x in items if x["pass"]) / len(items)) if items else float("nan")


def summarize(all_results):
    print("\n===================== DM-JUDGMENT REPORT =====================\n")
    for mid, res in all_results.items():
        if not res:
            continue
        print(mid)
        turns = res.get("turns", 0)
        raw_empty = res.get("raw_empty", 0)
        gave_up = res.get("gave_up_empty", 0)
        if turns:
            print(f"   {'Narration OMISSION rate (blank report_state)':54s} {100*raw_empty/turns:5.0f}%  "
                  f"({raw_empty}/{turns}; still-blank after retries: {gave_up})")
        for fam, label in (("write", "Flag WRITE (oracle: trigger/inc/no-false-flag)"),
                           ("delta", "Flag READ — delta tests (gate/prereq/gaslight/compound)"),
                           ("abs", "Flag READ — branch exclusivity")):
            items = res[fam]
            if items:
                print(f"   {label:54s} {_rate(items):5.0f}%  ({sum(1 for x in items if x['pass'])}/{len(items)})")
        print("   per-test:")
        for fam in ("write", "delta", "abs"):
            ids = []
            for r in res[fam]:
                if r["id"] not in ids:
                    ids.append(r["id"])
            for tid in ids:
                its = [r for r in res[fam] if r["id"] == tid]
                print(f"      {tid:16s} {_rate(its):5.0f}%  ({sum(1 for x in its if x['pass'])}/{len(its)})")
        print()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    ap.add_argument("--models", default="deepseek-v3.2,gemini-3.1-pro,claude-sonnet-4.6")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--anchor-repeats", type=int, default=2)
    ap.add_argument("--caps", default="deepseek-v3.2=4,gemini-3.1-pro=12,claude-sonnet-4.6=5")
    a = ap.parse_args(argv)
    models = [m.strip() for m in a.models.split(",") if m.strip() in MODEL_REGISTRY]
    caps = {}
    for kv in a.caps.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            caps[k.strip()] = float(v)

    keys = load_keys(a.keys)
    hk_key = keys.get("anthropic")
    if not hk_key:
        print("!! need an 'anthropic' key for the Haiku oracle/judge")
        return 2
    hk_client = anthropic.Anthropic(api_key=hk_key)
    hk_meter = HaikuMeter()
    meter = SpendMeter(caps=caps)

    uploads_dir = tempfile.mkdtemp(prefix="bench_flags_")
    with open(os.path.join(uploads_dir, "decision_flags.md"), "w", encoding="utf-8") as f:
        f.write(SYNTHETIC_FLAGS_MD)

    all_results = {}
    for mid in models:
        reps = a.anchor_repeats if mid == "claude-sonnet-4.6" else a.repeats
        try:
            all_results[mid] = run_model(mid, keys, reps, meter, hk_client, hk_meter, uploads_dir)
        except BudgetExceeded as e:
            print(f"   !! CAP HIT {mid}: {e}")
    summarize(all_results)
    with open(os.path.join(os.path.dirname(__file__), "dm_results.json"), "w") as f:
        json.dump(all_results, f, indent=2)
    print("candidate spend: " + ", ".join(f"{k}=${v:.2f}" for k, v in meter.spent.items()))
    print(f"haiku oracle+judge spend: ${hk_meter.cost:.2f}")
    print(f"DM-judgment total: ${meter.total() + hk_meter.cost:.2f}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
