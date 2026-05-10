# Pinned Fights — Multi-Message Behavior

## What the contract says (single-turn case)

`character_agent.py` `SYSTEM_PROMPT` covers the single-turn pin cleanly:

> **Fights specifically** are part of real relationships, not failure modes.
> Every real fight (resolved this turn OR pinned for later) should land as a
> memory. Default impact = 2.
>
> If a fight was **pinned** (explicit mutual agreement to set it aside, not
> yet resolved), ALSO emit a `character_callbacks` entry with
> source="character" so the topic can re-surface naturally later. The memory
> captures *that the fight happened*; the callback keeps the *unresolved
> topic* open.

So a clean single-turn pin produces:

- `memory_ops: [{action: "add", text: "fight about X — pinned for later", impact: 2}]`
- `callback_ops: [{action: "add", source: "character", original_text: "unresolved: X"}]`

No `due_by` — fights aren't date-bound, they ripen via the existing
`roll_callback_ripeness` mechanic and surface when the character is moved
to bring them up. Auto-expiry doesn't apply.

## What the agent sees per turn

This is the load-bearing fact for all the multi-message scenarios below.
Per `_summarize_state` in `character_agent.py:268` and the volatile block
in `determine_character_ops`:

- **Stable** (cached): `character_profile.di`, `user_life.di`
- **State summary** (refreshed each turn):
  - `[WALL CLOCK]` — today's date + day-of-week
  - `[MEMORIES]` — active memory set, branch-filtered
  - `[USER LIFE]` — user_profile entries
  - `[GROWTH]` — character_growth entries
  - `[CALLBACKS — OPEN]` — id, source, created_date, original_text
  - `[WELLBEING]`, `[ARC]`, `[SCHEDULE]`
- **Volatile** (this turn only):
  - `[USER INPUT THIS TURN]` — the user's message(s) for this turn
  - `[CHARACTER REPLY THIS TURN]` — the character's reply

**The agent does NOT see prior turns' raw text.** Everything from earlier
turns reaches it only through what got encoded in state (memories, callbacks,
arc, growth, profile). If a fight signal got missed two turns ago and never
made it into state, the current-turn agent has no way to recover it.

## Scenarios

### A. Clean single-turn pin → addressed N turns later

Turn 1
- user: "we need to talk about how you treated my friend at the party."
- char: "yeah, we do. but i can't do this rn — i'm closing the cafe alone."

Turn 1 agent extracts:
- `memory_ops`: add, impact 2, "Shae raised the way Zara treated Kait at the party; Zara acknowledged but pinned for later."
- `callback_ops`: add, source=character, "Unresolved fight: how Zara treated Kait at the party."

Turns 2–4 (unrelated chat):
- Agent sees the callback in `[CALLBACKS — OPEN]`. Nothing this turn references it. No ops fire. ✅

Turn 5
- user: "okay, can we talk about Kait now?"
- char: "yeah. i was sharp because i'd been holding bad news about my mom and i misdirected it. that was unfair to you both."

Turn 5 agent should:
- `callback_ops`: `{action: "resolve", id: <callback id>, resolution_text: "Zara explained she was carrying bad news about her mom and apologized for misdirecting it at Kait."}`
- `memory_ops`: add a higher-impact memory (3-4) of the actual repair
- Possibly `wb_mod_ops: [+2]` for genuine repair

**Verdict:** works as designed. Confirmed in spirit by the chili-night
simulator — the agent does emit `resolve` cleanly when the conversation
makes the connection explicit.

### B. Pin context is split across turns 1 + 2 (no clean pin signal in either alone)

Turn 1
- user: "we should talk about something."
- char: "ok. what?"

Turn 2
- user: "the kait thing. but later — i'm at work."
- char: "yeah, ok. after dinner."

What the agent sees on turn 2:
- `[CALLBACKS — OPEN]` from turn 1 — *if* turn 1 even emitted one (probably not; "we should talk" is too vague to commit on).
- `[USER INPUT THIS TURN]`: "the kait thing. but later — i'm at work."
- `[CHARACTER REPLY THIS TURN]`: "yeah, ok. after dinner."

Turn 2 agent likely emits:
- `callback_ops: [{action: "add", source: "character", original_text: "promised to talk about 'the kait thing' after dinner"}]`
- Probably NO memory — "we'll talk later" alone isn't memory-worthy.

**Failure mode:** the agent sees a deferred conversation but does *not*
classify it as a *fight* because the conflict signal was in turn 1, which
turn-2 agent can't see. So it gets logged as a generic
character-promise callback, not a pinned-fight callback. The memory entry
that should have captured "the fight happened" never fires.

**Practical impact:** moderate. The topic is still tracked (callback exists),
so the character can bring it up later. But:
- The relationship arc doesn't shift (no "post-fight cooling-off")
- No memory entry tagged as a fight
- Default ripeness aging applies, but without the "fight" framing the
  character may bring it up too breezily

**Mitigation:** the agent could be told that *current-turn* deferral
language ("later, after dinner") combined with *context implying conflict*
in the user message ("the kait thing") should be treated as a pinned fight
even when the conflict signal predates this turn. But this trades restraint
for sensitivity — false positives on "the [topic] thing" being labeled a
fight.

### C. Same fight, pinned multiple times across turns

Turn 1: pin → callback id=5 added.

Turn 5
- user: "yo, the kait thing — you free to talk?"
- char: "still no. tomorrow?"

Turn 5 agent should emit:
- `callback_ops: [{action: "checkin", id: 5}]`
- No new memory (re-deferral is a beat in the existing thread, not a new
  one). The contract says "default impact 2" for fights but a *re-deferral*
  isn't itself a fight; the original is already memorialized.

**Risk:** the agent might re-add a memory or a duplicate callback if it
doesn't match the current turn's "kait thing" reference to the existing
`#5 ...how Zara treated Kait at the party`. Topic-match is the agent's job;
if the topic in the callback's `original_text` is phrased differently from
how the user/character refer to it now, dedup can fail.

**Practical impact:** low if `original_text` was written specifically.
Medium if it was abstract ("unresolved fight"). This is an argument for
making the agent write specific `original_text` — which the contract
already pushes ("Memories must be SPECIFIC and FUTURE-USEFUL"; same logic
applies to callback text).

### D. Re-pin with a different framing (topic drift)

Turn 1: pin → callback id=5 ("how Zara treated Kait at the party")

Turn 8
- user: "i still feel weird about how that night ended."
- char: "i know. me too. i don't have anything new yet."

Turn 8 agent sees `#5 how Zara treated Kait at the party`. Does it match
"that night ended" → "the party night"? Maybe. The agent has to make the
inference.

If the agent matches: `checkin id=5`. ✅
If the agent doesn't: it might add a new callback "Shae feels weird about
how that night ended" — a duplicate of #5 from a different angle.

**Practical impact:** medium. Two callbacks for the same underlying issue
won't break anything, but the user will see two separate "open threads"
banners in the right panel and `roll_callback_ripeness` rolls each
independently — so the topic surfaces twice as often as it should.

**Mitigation:** strengthening the system prompt's checkin guidance to
"prefer checkin when the topic is plausibly the same, even with different
wording" would help. There's a tradeoff with restraint, but the cost of
false-checkin (slightly wrong id stamp) is much lower than false-add
(duplicate ripeness rolls + duplicate user-visible thread).

### E. User dismisses the pin without resolving

Turn 5
- user: "you know what, forget the kait thing. doesn't matter anymore."
- char: "you sure? we can if you want."
- user (next turn): "yeah i'm sure."

Turn 5 agent: not clearly a resolve (user said "forget it" but character
offered to still talk). Cautious agent does nothing this turn; emits a
checkin if anything.

Turn 6 agent: user explicit dismissal → could emit `resolve` with
`resolution_text: "Shae let it go without further conversation."` But
strictly the contract says resolve is when "the conversation revealed it
played out" — letting go is a played-out outcome, just not the talk-through
kind. The agent will probably emit resolve in cautious cases; might emit
nothing in others.

**Practical impact:** low. Worst case, the callback stays open and the
character occasionally surfaces it; eventually `/dismiss` from the user
clears it manually.

### F. Character pins, then forgets / never raises again

Turn 1: pin → callback id=5.

Turns 2–N: character doesn't bring it up. User doesn't either.

Each turn, `roll_callback_ripeness` rolls a d10 against days-since-last-
checkin. As days accumulate, the probability of "ripe" rises. Eventually
the character's voice prompt will get a `[CALLBACKS — RIPE]` injection
nudging her to surface it.

**Practical impact:** this is *intended* behavior. The whole point of the
callback ledger is that unaddressed things ripen and re-emerge.

### G. Multiple distinct fights in flight at once

Turn 1: fight A pinned → callback id=5
Turn 7: fight B pinned → callback id=12

Each ripens independently. If both come ripe near the same time, the
character could plausibly raise either; the voice prompt sees both in
`[CALLBACKS — RIPE]` and Opus 3 picks. No special handling needed.

**Practical impact:** none. Works.

### H. Pin happens via batched user messages (rapid-fire)

User sends 4 quick messages without the character replying:
1. "what you said today was hurtful"
2. "i need you to know that"
3. "but i don't have it in me to do this tonight"
4. "tomorrow ok?"

The Characters batching collapses these into a single user message before
the character reply. The agent receives the full concatenated text in
`[USER INPUT THIS TURN]`, so the pin signal is visible in one shot.
Memory + callback fire on the same turn the character replies. ✅

**Practical impact:** none. The batching mechanism happens to make this
case easier, not harder.

## Summary — what works and what doesn't

| Scenario                                  | Behavior          | Notes                                                                            |
|-------------------------------------------|-------------------|----------------------------------------------------------------------------------|
| A. Clean pin → resolved later             | ✅ Works          | Single-turn case, well-specced                                                   |
| B. Pin context split across turns         | ✅ Fixed (rule 2) | Deferred-language-as-continuation rule lets the agent match via `[MEMORIES]`     |
| C. Re-pinned same fight, same wording     | ✅ Works          | Existing checkin path handled this already                                       |
| D. Re-pinned with different framing       | ✅ Fixed (rules 1,3) | Generous-match rule + `pinned-fight` focus tag give the agent two ways to dedup |
| E. User dismisses without resolving       | ⚠️ Cautious agent | Acceptable — callback lingers; user `/dismiss` clears                            |
| F. Character forgets, never raises again  | ✅ By design      | Ripeness re-surfaces it                                                          |
| G. Multiple concurrent fights             | ✅ Works          | Independent ripening                                                             |
| H. Batched user messages with pin         | ✅ Works          | Batching collapses messages before the agent sees them                           |

The two real gaps were B and D, both rooted in the same architectural
fact: the agent's per-turn view doesn't include prior turns' raw text. It
sees only what got encoded into state by previous turns' agents. The
prompt-level fixes below close them by giving the agent both more
matching laxity and an explicit cue for the pattern.

## Applied fixes

The first three mitigations are in (see `character_agent.py:62-114`):

1. **Generous checkin matching.** The contract now says: "when this turn
   references an open callback even with different wording from its
   `original_text`, prefer `checkin` over `add`." Examples like "the kait
   thing" → existing fight callback are called out explicitly. The HARD
   RULE bullet was tightened to match. Addresses Scenario D.

2. **Deferred-conversation language as continuation.** New explicit rule
   in the callbacks section: "later", "tomorrow", "after [X]", "not now",
   "when i can" applied to a topic that exists in `[MEMORIES]` (especially
   with `focus: "pinned-fight"`) or `[CALLBACKS — OPEN]` means continuation
   — emit checkin, not add. Addresses Scenario B.

3. **`focus: "pinned-fight"` tag on pinned-fight memories.** The fights
   paragraph now tells the agent to tag the memory with this focus when
   pinning, so future-turn agents can match topic-drifted references via
   memory text rather than relying on callback text alone. **Resolved
   fights deliberately do NOT get this tag** — the topic is closed; tagging
   them pinned would be wrong. Resolved fights either land as a memory with
   a descriptive focus ("fight resolved") or no focus, and if a prior
   pinned-fight callback exists the agent emits `resolve` on it instead of
   adding anything new. Addresses Scenarios C/D dedup.

### Not done (deferred)

4. **Pass prior 1-2 turns' raw text to the extractor on pin-relevant turns.**
   Bigger architectural change — breaks the per-turn isolation. The first
   three fixes plug the same gap softer-side via prompt; if a real chat
   still trips Scenario B after those, this becomes the next move. Defer
   until then.

The single-turn pin worked correctly before these fixes; the multi-message
gaps were latent. With the contract updates in, B and D should now resolve
correctly without architectural changes — the agent has been given both
the matching laxity (rule 1, 3) and the explicit pattern-recognition cue
(rule 2) it needs to thread the context across turns.
