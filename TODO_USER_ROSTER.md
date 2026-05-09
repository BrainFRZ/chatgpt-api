# TODO: user_roster — character's view of user's important people

**Status:** not started. Design discussed 2026-05-09; deferred pending real-world evidence that the existing memory + profile setup isn't sufficient.

---

## The gap

Today the character's view of Shae's people (Heather, Marcus, Diana, Kira, etc.) is reconstructed each turn from:

- `character_memories.jsonl` — specific scenes ("Heather called drunk in 2017")
- `user_profile.jsonl` — stable facts ("user has sister Heather, Atlanta")
- The character's own profile (Zara's prose Follow-through section, etc.)

There's no synthesized, evolving "what Zara *thinks* of Heather" that calibrates turn-by-turn. So:

- Attitudes can drift between turns (Sonnet rebuilds the impression each time from scattered evidence)
- Small impressions don't compound (5 mentions of Heather being warm don't slowly shift Zara from "wary" to "fond" — each turn re-reads the same memories)
- Different characters can't have *different* views of the same person without a per-character store

---

## What it would do

Per-character `user_roster.jsonl` with one entry per important person:

```json
{
  "id": 1,
  "name": "Heather",
  "relation": "older sister",
  "facts": ["lives in Atlanta", "married, two kids"],
  "warmth": 1,
  "trust": 1,
  "mode": "wary",
  "notes": [
    {"date": "2026-04-01", "text": "Called crying about money — hasn't asked for help yet"},
    {"date": "2026-05-09", "text": "Showed up for Shae's birthday — first time in two years"}
  ],
  "salience": 4,
  "first_seen_date": "2026-04-01",
  "last_referenced_date": "2026-05-09"
}
```

Per-character (Zara has her own; another character of yours might think Heather's the steady one). Maintained by the existing post-stream Sonnet 4.6 character_agent via a new `roster_ops` op kind. Surfaced via Haiku-recall into a `[PEOPLE — what you know about them]` injection block.

---

## Design options

### Option A: LLM-restrained (recommended starting point)
- Sonnet 4.6 character_agent emits `roster_ops` post-stream (add / update / note / salience-bump / obsolete).
- Use **structured fields** (warmth: -2..+2, trust: -2..+2, mode: enum) — NOT free-text "view." Easier to update incrementally, doesn't drift narratively.
- Free-text `notes` array is **append-only**. Don't rewrite past notes.
- High bar for commits: most turns emit no roster_ops. Required justification quote from the chat for any field change.
- Build effort: **~1 day**.

### Option B: Classical ML + LLM hybrid (layer if drift surfaces)
- Add per-turn cheap signals (free, ~5-50ms): NER for mentions, sentiment classifier for user-side valence, topic classifier.
- Persist signal aggregates as additional structured fields (`recent_user_valence`, `recent_topics`).
- Sonnet update step is now constrained: "5 of last 7 mentions had user-valence: slightly negative, topic: family-stress. Should warmth move? trust?"
- Anomaly detection flags turns where the LLM update would shift more than the signal evidence supports.
- Build effort: **~2-3 days** (includes installing NLP deps, training/loading classifiers, integration tests).

### Option C: No roster — keep relying on memory + profile
- Status quo. Character's view of each person is reconstructed each turn from memories + profile.
- Loses the "calibrated accumulated take" property. Probably fine at single-user single-character chat volume.
- Build effort: **0**.

---

## Why classical ML alone wouldn't work

ML can't capture "Zara would feel about Heather given Zara's profile and history" without essentially being an LLM. Sentiment analysis on chat chunks measures *Shae's* tone, not Zara's interpretation. Classical ML's role is **guardrail** for the LLM's update step, not replacement of it.

---

## Risks (drift compounds, mostly)

1. **Compounding drift on free-text views** — view of Heather slides from "warm but high-strung" → "warm but anxious" → "judgmental" over hundreds of turns with no user input warranting it.
2. **Confabulation on update** — Sonnet "synthesizes" details that weren't in the chat.
3. **Recency bias** — one venting session bakes in a permanent negative impression; positive turns don't update because the model is restrained.
4. **Conflict with character_profile.di** — profile says "loyal but high-strung"; roster evolves to "judgmental" over time.
5. **Salience miscalibration** — surfaces too often (every turn) or never (forgotten).

---

## Mitigations (Option A's design baked in to address the above)

1. **Structured fields, not free-text views** — mode/warmth/trust/etc. are enums or small ints. Easier to update incrementally; no narrative drift.
2. **Append-only notes** — history accumulates; drift becomes recovery (old notes balance new ones). View is derived at read-time by Haiku summarizing recent notes.
3. **High-bar commits** — Sonnet's roster_ops require a justification quote. Most turns: no update. Same restraint pattern as `growth_ops` today.
4. **Real shifts → growth_op** — when Zara has a genuine "I trust Heather now" beat in conversation, that's a `growth_op` that locks the change as durable character identity. Roster is fluid; growth is durable.
5. **User-correctable** — `/roster-set Heather warmth=2 trust=1`, `/roster-reset Heather` slash commands for explicit override. Drift is fixable; entry is removable.

---

## Recommended path

Start with **Option A**. See if drift actually surfaces in real chat usage at single-user volume. The volume might not be enough to cause real drift before the user notices and corrects.

If drift IS a problem in practice, layer in **Option B**'s ML signals as a hardening pass — they're additive (signals constrain LLM updates), not replacement, so easy to add later.

The right answer becomes more ML-heavy if scale ever grows (multi-character, multi-user, high-volume) — drift compounds faster than humans can catch at scale.

---

## Open questions to resolve before building

1. **Does drift actually happen at single-user volume?** Empirical question. Maybe just chat with Zara for a month and see.
2. **Per-character or per-project?** Per-character (Zara's view of Heather may differ from another character's view).
3. **How does `/reinterview` interact?** Re-interview rebuilds the character profile but shouldn't necessarily wipe the roster (the user's people haven't changed). Probably leave roster untouched on re-interview.
4. **`/roster-set` and `/roster-reset` slash commands needed from day one?** Probably yes — drift is fixable, but only if there's a fix command.
5. **Should past memories link to roster entries?** I.e., the Heather-called-drunk-2017 memory references `roster:1` (Heather). Adds power (when Heather's mentioned, all linked memories surface) but adds maintenance complexity. Probably NO for v1 — keep separate, recall surfaces both via name match.

---

## Files to create / modify when we build

- `backend/character_roster.py` (new) — mirrors `character_storage.py` patterns: `RosterStore` with read/append/rewrite, atomic-rewrite jsonl
- `backend/character_agent.py` — extend `report_character_state` tool with `roster_ops` array; add prompt section
- `backend/characters_runtime.py` — dispatch `roster_ops` in `run_post_stream_extraction`
- `backend/character_recall.py` — extend candidate pool to include roster entries; surface as `recalled_roster`
- `backend/game_systems/characters.py` — `build_roster_injection` for `[PEOPLE]` block; add to builder tuple
- `backend/character_inner_state.py` — include `recalled_roster` in volatile context
- `backend/main.py` — `/roster-set` and `/roster-reset` slash command handlers
- `backend/tests/test_character_roster.py` (new) — store CRUD, op application, drift-resistance regression tests
- `.gitignore` — allowlist new files
