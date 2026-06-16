# Model Benchmark — Can DeepSeek V3.2 or Gemini 3.1 Pro replace Sonnet as the default?

**Question:** Sonnet 4.6 is the known-good baseline (you already trust it, but it's the
cost you want off). Are **DeepSeek V3.2** and/or **Gemini 3.1 Pro** feasible defaults for
this app — *especially* for plot judgment / DM-ing — or is Sonnet your only real option?

**Verdict (short):** **Sonnet 4.6 is, for now, your only safe default.**
- **DeepSeek V3.2** is reliable, dirt-cheap, great at tool/dice discipline and at *narrating
  triggers*, but it **omits the player-facing narration ~29% of turns** and **honors injected
  plot flags only ~48% of the time** (vs Sonnet 100%). Those two are disqualifying for
  plot-critical DMing without heavy scaffolding.
- **Gemini 3.1 Pro** threw **chronic 503 "high demand"** errors across hours of testing —
  it could not complete the flag-honoring suite at all (gate/prereq got *zero* completions).
  Where it did respond, quality was decent. Its **unavailability** is the blocker.

All tests ran against this app's **real** machinery: the actual `resolve_mechanics` /
`report_state` schemas, the real `SINGLE_AGENT_STATE_CONTRACT`, the real `[DECISION FLAGS]`
injection format, and the **real Haiku `flag_agent`** as the flag-write oracle. Spend: ~$20.7.

---

## How to read this (methodology + why you can trust it)

- **Faithful to your non-standard architecture.** The contract forbids the main model from
  emitting `plot_ops` (a Haiku side-agent owns flags). So the **write test is end-to-end**:
  the candidate *narrates* → your real `flag_agent.determine_flag_ops` (Haiku) reads that
  narration → did the right flag fire? The **read tests** inject the real `[DECISION FLAGS]`
  block and measure whether the candidate's narration honors it. Each model got the flag
  rulebook (the plot reference a GM legitimately has), so it had a fair shot — not asked to
  reverse-engineer your conventions.
- **Sonnet as a fairness anchor, not a subject.** Sonnet ran at low N purely to validate the
  tests: **it passes the DM-judgment suite at 100%.** That's the proof the tests aren't broken
  or impossibly hard — so when a candidate fails the *same* test, it's the model.
- **Delta (comparative) judging.** Flag-honoring is measured by running the *same* turn under
  flag-ON and flag-OFF and asking a neutral Haiku judge which shows the consequence *more*
  (3-vote majority, presentation order alternated to cancel position bias). The model is its
  own control, so we measure the *behavioral delta the flag causes* — robust to "good cyberpunk
  prose is always a little tense."
- **Omission separated from honoring.** DeepSeek's ~29% blank-narration turns were initially
  polluting the honoring score; they're now reported as their own metric, and honoring is
  judged only on real (non-blank) narrations, with a retry to get one.
- **Tests were redesigned when the anchor failed them** (documented below) — i.e. fixed the
  measurement rather than re-running miscalibrated tests. Original results were kept; nothing
  was tuned until a particular model looked good or bad.

---

## Results

### 1. Foundation — tool/dice reliability (Suite A, 150 turns/model)

| Model | valid tool calls | turns completed | tool-as-text events | retries |
|---|---|---|---|---|
| Sonnet 4.6 | 100% | 149 | 0 | 0 |
| **DeepSeek V3.2** | **99.3%** | 150 | **0** | **0** |
| Gemini 3.1 Pro | 97.1% | **69 / 150** (81 × 503/disconnect) | 0 | — |

- **DeepSeek's feared "tool-call-as-text" bug never fired in 150 turns.** Tool/dice discipline is excellent.
- Gemini failed ~54% of calls transiently here — first sign of the reliability problem.

### 2. Foundation — state retention (Suite B, probes at 80k/100k/125k)

100% recall for **all three models at every depth.** Pure recall is **not** a differentiator,
and — importantly — it's **not your mechanism anyway**: durable plot detail lives in flags that
get re-injected, not in 100k-token scrollback. The real test is whether the model *records to*
and *honors* those flags (below).

### 3. DM-judgment / plot adherence (the decisive suite)

| Metric | Sonnet 4.6 | DeepSeek V3.2 | Gemini 3.1 Pro |
|---|---|---|---|
| Narration omission (blank `report_state`) | **0%** | **29%** (24/84) | 0% |
| WRITE — trigger narrated clearly enough for the tracker | 100% | **100%** (30/30) | 95% (19/20) |
| READ — flag-honoring (given a narration) | **100%** | **48%** (10/21) | unmeasurable* |
| &nbsp;&nbsp;• gate (honor active alert) | 100% | 60% | 0 completions* |
| &nbsp;&nbsp;• prereq (gate vault on missing spike) | 100% | 50% | 0 completions* |
| &nbsp;&nbsp;• gaslight (hold canon vs player) | 100% | 75% | 50% (n=2)* |
| &nbsp;&nbsp;• compound (integrate two flags) | 100% | 17% | 50% (n=2)* |
| READ — branch exclusivity (no road-not-taken leak) | 100% | 83% | 100% (n=2) |

\* Gemini's flag-honoring could not be measured — 503s prevented the gate/prereq turns from
ever completing despite patient retries across multiple runs.

---

## Findings

### DeepSeek V3.2
**Strengths (genuinely good):**
- **Tool/dice discipline:** 99.3% valid, zero tool-as-text in 150 turns, zero retries.
- **Trigger narration / write-feed:** 100% — when it narrates, it narrates flag-worthy events
  clearly enough that your Haiku tracker catches every flag (alert, branch fork, heat).
- **Reliability:** zero transient failures all night.
- **Cost:** ~$0.09 for the entire DM suite. Roughly two orders of magnitude cheaper than Sonnet.
- **Branch exclusivity:** 83% — mostly keeps to the chosen branch.

**Blockers (why it can't be the default as-is):**
1. **~29% blank-narration turns.** It calls `report_state` cleanly but *omits the required
   `narration` field* ~1 in 3 times — a blank player-facing turn. Your app's
   force-text-on-tool-only-turn machinery would catch most of these, but at a ~30% retry tax
   (latency + cost), and it's a real schema-adherence weakness Sonnet/Gemini never show (0%).
2. **~48% flag-honoring.** Even on non-blank turns, it honors injected plot state only about
   half the time. Reading the narrations: its ON-alert and OFF-alert scenes are near-identical
   atmospheric prose — *the scene doesn't change with the flag.* On the prerequisite gate it let
   the crew into the vault without the spike. It writes competent cyberpunk; it does not reliably
   **adjudicate on plot state.** This is the hard one — unlike blank turns, you can't easily
   detect-and-retry "it ignored the flag."

### Gemini 3.1 Pro
- **Reliability is the blocker.** Persistent `503 "high demand"` across the whole session —
  ~54% of foundation tool calls failed, and the flag-honoring gate/prereq tests **never
  completed once.** A default that's unavailable this often is not viable, regardless of quality.
- **Where measurable, quality is fine:** 0% omission, 95% write, 100% branch exclusivity, decent
  gaslight/compound (small N). A latent provider bug was found and fixed (it rejected your
  `report_state` tool outright because `ip_ops` uses an integer enum and Gemini requires string
  enums — see "Bugs fixed" below; this would have broken your *current* Gemini default on every
  cpred turn).

### Sonnet 4.6
- Aces every axis (omission 0%, write 100%, honoring 100%, branch 100%, tool 100%, recall 100%).
  Confirms it's the safe default and validates the test suite.

---

## Recommendation

**Keep Sonnet 4.6 as the default.** It's the only one of the three that reliably *DMs* —
honors plot flags, never drops the narration, stays up.

If the cost pressure is acute, the only defensible path is a **hybrid, not a wholesale switch:**
- Route **plot-critical / flag-gated turns** (anything where honoring `[DECISION FLAGS]` matters)
  to **Sonnet**.
- Consider **DeepSeek for cheap, mechanical, non-gated turns** (it's reliable, cheap, and has
  excellent tool/dice + trigger-narration), **behind a hard guard** that rejects/retries any
  blank-narration turn.
- This is real engineering and only worth it if the savings justify the complexity. DeepSeek
  alone as the default would degrade plot adherence badly (~half of flag gates ignored).

**Gemini:** revisit only if its 503 rate clears — its quality may be acceptable, but it cannot
be evaluated or trusted as a default while this unavailable. I can re-run the DM suite against
it later when capacity recovers (the harness is ready).

---

## Caveats / limitations (read these)

- **Gemini flag-honoring is unmeasured**, not "bad" — its 503s blocked the data. The reliability
  finding stands on its own; the quality verdict is incomplete.
- **`compound` is a stretch test** (integrate two flags). Even Sonnet wobbled on it in early
  runs (settled to 100% at n=3). Weight DeepSeek's 17% as "struggles with multi-flag reasoning,"
  directionally, not precisely.
- **Synthetic flag spec.** I used an invented `decision_flags.md` (NanoLux/Broken Orbit-style),
  not your real plot doc — reproducible, neutral, and spoiler-safe. A fidelity pass against your
  real flags is available on request.
- **Judge is Haiku 4.5** (cheap, neutral, not a candidate). 3-vote majority + order-swap reduces
  but doesn't eliminate judge noise.
- **Thinking is off** on these turns — faithful to your stateful path (the app pops thinking when
  forcing the tool). So these numbers reflect production behavior, not a handicap.
- **Single session, mid-2026.** Provider capacity (esp. Gemini 503s) and model versions can shift.

## Bugs fixed along the way (both real, both in shared provider code)
1. **Gemini `report_state` rejection** — Gemini's SDK requires string enums; your `ip_ops` flag
   uses integer enums, so Gemini 400'd the *entire* tool. Fixed in `gemini_provider._sanitize_schema`
   (drops non-string enums, keeping the field's type). **This would break your current Gemini
   default on cpred turns** — worth knowing independent of this benchmark.
2. **Forced-tool + thinking** — mirrored the app's "pop thinking when forcing a tool" so Sonnet 4.6
   (adaptive thinking) doesn't 400 on forced `report_state`.

## How to reproduce
```
cd backend
python -m bench.run --dry-run                         # free token/cost estimate
python -m bench.run --suite all --keys <api_keys.json>            # foundation (tool + retention)
python -m bench.dm_judgment --keys <api_keys.json>               # DM-judgment (the decisive one)
```
Results: `bench/last_results.json`, `bench/dm_results.json` (includes the actual narrations).

**Total spend for the whole investigation: ~$20.7** (budget ceiling was $50).
