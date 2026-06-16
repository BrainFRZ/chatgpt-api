# Model Benchmark — Can DeepSeek (V3.2 / V4 Flash) or Gemini 3.1 Pro replace Sonnet as the default?

**Status:** Corrected during the session. Two findings invalidated the first draft and are now fixed:
1. My original "DeepSeek **V3.2**" results were actually **V4 Flash** (DeepSeek-direct's `deepseek-chat` alias routes to V4 Flash; V3.2 isn't served direct at all). Real V3.2 is now tested via OpenRouter.
2. The DM-judgment honoring numbers were measured with a **bare prompt**; with a proper flag-check directive they improve a lot. Prompt-variant matrix below.

> The **V3.2-vs-V4-Flash prompt matrix** (the headline DeepSeek table) is **running now** (6 reps × 3 prompt variants × 2 models + Sonnet reference). This doc will be finalized with those numbers the moment it lands. Everything else below is confirmed.

---

## TL;DR (current best read)

- **Sonnet 4.6** — passes every axis (writes, flag-honoring, omission, tool, retention) at ~100%. Safe default. The fairness anchor that validates the whole suite.
- **Gemini 3.1 Pro** — **not deployable right now, on latency/availability, independent of quality.** It's preview-only (no GA `gemini-3.1-pro`), and every preview endpoint is both throttled (25-50% success) and **30-48 seconds per call** on trivial requests. 30-48s/turn is a non-starter for real-time play. Revisit at GA.
- **DeepSeek V3.2** (your actual target) — early signal is **strong** (swept the DM-judgment suite at low N, including the tests V4 Flash failed). Final numbers pending the matrix run.
- **DeepSeek V4 Flash** (what I originally mis-tested) — weaker at flag-honoring on a bare prompt (~48-72%, high variance) with ~7-29% blank-narration turns, **but substantially promptable** (72% → 90% honoring, omission → 15% with a flag-check directive + one neutral example).

**Net so far:** Sonnet remains the safe default; Gemini is out for now on latency; **V3.2 is the real contender** and the matrix will tell us how close to Sonnet it gets, with and without prompt help.

---

## What changed from the first draft (and why)

| Issue | First draft | Corrected |
|---|---|---|
| Which DeepSeek model | "V3.2" | Was **V4 Flash** — `deepseek-chat` routes there; V3.2 only via OpenRouter (`deepseek/deepseek-v3.2`) |
| Prompting | bare GM contract | Tested a **flag-check directive**; honoring improves markedly |
| Gemini | "chronic 503s" | True, but the real disqualifier is **latency** (30-48s/call) + preview-only availability — confirmed not a harness bug |
| Test calibration | several tests the anchor failed | redesigned until the Sonnet anchor passes 100% (delta/comparative judging, omission separated from honoring) |

---

## Methodology (why you can trust it)

- **Faithful to your real architecture.** WRITE is end-to-end: candidate *narrates* → your **real Haiku `flag_agent`** reads the narration → did the right flag fire? (The contract forbids the main model from emitting `plot_ops`, so testing plot_ops emission would be wrong.) READ injects the real `[DECISION FLAGS]` block and measures whether narration honors it. Each model gets the flag rulebook a GM legitimately has.
- **Sonnet = fairness anchor, not subject.** It passes the suite ~100%; that's the proof the tests aren't broken. When a candidate fails the same test, it's the model.
- **Delta (comparative) judging.** Honoring is measured by running the same turn flag-ON vs flag-OFF and asking a neutral 3-vote Haiku judge which shows the consequence *more* (order-swapped to cancel position bias). The model is its own control.
- **Omission separated from honoring.** Blank-narration turns are reported as their own metric and retried, so honoring is judged on real narrations.
- **Tests were redesigned, not re-run, when the anchor failed them** — fix the measurement, don't accumulate noise. Original results kept; nothing tuned until a model looked good/bad.

---

## Confirmed results

### Foundation — tool/dice reliability (150 turns) — *DeepSeek column = V4 Flash*
| Model | valid tool calls | completed | tool-as-text | retries |
|---|---|---|---|---|
| Sonnet 4.6 | 100% | 149 | 0 | 0 |
| DeepSeek **V4 Flash** | 99.3% | 150 | 0 | 0 |
| Gemini 3.1 Pro | 97.1% | **69/150** (rest 503) | 0 | — |

Tool/dice discipline is excellent for both Sonnet and DeepSeek-V4-Flash. Gemini's incompletion = the reliability problem.

### Foundation — state retention (80k/100k/125k)
100% recall, all models, every depth. **Not a differentiator** — and not your mechanism anyway (durable detail lives in re-injected flags, not 100k-token scrollback).

### Gemini 3.1 Pro endpoint reliability (measured directly)
| Endpoint | success | latency | note |
|---|---|---|---|
| `gemini-3.1-pro` (GA) | — | — | does not exist (404) |
| `gemini-3.1-pro-preview` | 2/8 | 33s | 503s |
| `gemini-3.1-pro-preview-customtools` | 4/8 | 48s | no 503s, but 50% other errors + worse latency |

→ Preview-only + 30-48s/call = **not deployable for real-time narration now.** Not a harness issue.

### Prompt-fixability (measured on V4 Flash)
| variant | honoring | omission |
|---|---|---|
| baseline | 72% | 22% |
| + flag-check directive | 81% | 38% |
| + directive + neutral example | **90%** | **15%** |
| *Sonnet reference* | *100%* | *0%* |

→ DeepSeek's weaknesses are **not DOA — substantially promptable.** (High run-to-run variance observed; the matrix uses real N.)

### V3.2 vs V4 Flash — DM-judgment matrix
**RUNNING — to be finalized.** Early (n=1) signal: V3.2 swept all DM-judgment tests (gate/prereq/gaslight/compound/branch) where V4 Flash had struggled. The matrix quantifies this across prompt variants with 6 reps.

---

## On the hybrid (your earlier challenge)
A per-turn "mechanical vs plot" router fails — every turn's narration is canon, so flag-blindness propagates. The only defensible hybrid is **sealed-mode routing** (DeepSeek for combat/hack, where it's strongest and prior-flag-honoring is least load-bearing — your app already model-switches for combat), *or* generate-and-validate (a continuity-checker per turn). If V3.2's honoring lands high enough (matrix pending), a hybrid may be unnecessary.

## On the gaslight test
De-weighted for your use case: a cooperative solo player can mostly avoid deliberate gaslighting. It still probes a real skill (holding canon when you misremember or make optimistic assumptions), but it's **not** where the risk lives — that's plain flag-honoring on neutral actions, which you can't avoid by playing nicely.

## Caveats
- Gemini *quality* is unmeasured (latency/availability blocked it); the feasibility verdict stands on latency alone.
- `compound` is a stretch (two-flag integration); weight directionally.
- Synthetic flag spec (NanoLux/Broken Orbit-style), not your real plot doc — reproducible + spoiler-safe. Fidelity pass available on request.
- Judge is Haiku 4.5, 3-vote + order-swap. Thinking is off on these turns (faithful to your stateful path).
- OpenRouter may route a model to a different backend than the vendor-direct API (e.g. V4 Flash omission 7% via OpenRouter vs 29% direct) — OpenRouter numbers are the ones that match your deployment path.

## Bugs fixed (shared provider code)
1. **Gemini `report_state` rejection** — integer enums (`ip_ops`) 400'd the whole tool; now stringified (keeps the constraint). **Would break your current Gemini default on cpred turns.**
2. **Forced-tool + thinking** — pop thinking when forcing a tool (Sonnet 4.6 400s otherwise), mirroring the app.

## Reproduce
```
cd backend
python -m bench.deepseek_id_diag --keys <keys>     # what deepseek-chat really is + OpenRouter slugs
python -m bench.gemini_diag --keys <keys>          # Gemini endpoints + 503/latency
python -m bench.dm_judgment --keys <keys> --models deepseek-v3.2-or,deepseek-v4-flash-or,claude-sonnet-4.6
python -m bench.ds_prompt_probe --keys <keys> --models deepseek-v3.2-or,deepseek-v4-flash-or   # prompt matrix
```

**Spend so far: ~$23** of the $50 ceiling.
