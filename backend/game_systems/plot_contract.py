"""
Shared plot-trigger contract text, injected into every TTRPG system's
SINGLE_AGENT_STATE_CONTRACT. Defines when plot_ops must fire, how to
author plot-doc triggers, how to pre-register pending flags, what the
`decision` field must look like, and how severities are used.

The content is intentionally redundant with shorter language that may
appear in EVENTS_CONTRACT blocks — the single-agent path (Opus) has no
Events agent to pre-process decisions, so the full discipline has to
live here.
"""

PLOT_TRIGGER_CONTRACT = """### Plot Triggers (plot_ops) — authoritative guide

`[DECISION FLAGS]` is the campaign's save-state read-out. The GM/player does NOT read the plot document — it contains spoilers they want to experience in play. You are their proxy: you watch the narrative, fire plot_ops when trigger conditions are met, and `[DECISION FLAGS]` becomes their non-spoilery view of where the campaign stands.

Because `[DECISION FLAGS]` IS the user-facing save state, the `decision` field on every fired plot_op MUST be a self-contained narrative sentence — readable standalone without the plot doc. Terse machine labels ("chose B", "yes", "set") are UNACCEPTABLE. See the required shape below.

#### When to fire

- Fire the moment a trigger condition from the plot doc is met in-narrative — **even mid-beat**. Triggers are sub-beat: two triggers can fire inside the same beat on different turns, and firing one does NOT imply the beat is complete. A branch can happen on something that happened earlier in the same beat.
- Multiple triggers can fire in a single turn → emit one plot_op per trigger.
- Do NOT fire for general narrative importance. Tense moments, emotional scenes, and creative choices do NOT qualify unless the plot doc specifically tracks them via a named flag, variable, or branch.
- Do NOT invent trigger keys not declared in the plot doc. If something feels plot-worthy but has no declared flag, capture it in the turn's prose, callbacks, or NPC memories — do NOT fabricate a flag.

#### Authoring formats the contract accepts

Plot documents may declare triggers in either style — both are valid:

- **Enumerated** (`Variable: X / Values: [a, b, c]` blocks per decision): use the exact `Variable` name as `key` and exactly one of the declared `Values` as `value`.
- **Expected Flags + inline tracking**: a block near the top of the plot doc (often `Expected Decision Flags`) listing flag IDs, plus inline `Tracking:` / `fire a plot_op: <key>` lines at the firing points. Use the listed ID verbatim as `key`; `value` is the declared enumeration if the inline line declares one, otherwise a concise narrative descriptor.

In both cases: `key` comes from the plot doc verbatim (same spelling, same punctuation), never a model paraphrase.

#### Pre-registration (pending flags)

On the first turn of a session, register every flag declared by the plot doc for that session by firing a plot_op with `value=null` (or `"pending"`). These appear as `(pending)` in `[DECISION FLAGS]` every turn, reminding you the decision has not yet been made. Backend guards:

- Pending cannot overwrite a resolved flag (idempotent re-registration is safe — it just no-ops).
- Pending cannot overwrite an earlier pending (preserves the original `decision` text).
- A resolved `branch` flag cannot be re-overwritten to a different value.

So: register pendings freely on session start; the guards keep your state clean.

#### Severities

- `branch` — a defined fork in the plot doc. Permanent once resolved; backend refuses to overwrite. Report which path was taken.
- `flag` — a fact or decision worth remembering that is not narrative-branching.
- `divergence` — the player went off-script but can be steered back to a defined path. Report the departure in `decision` text and continue normally. Do NOT halt, do NOT switch to OOC.

#### Required shape of the `decision` field

The `decision` field is the ONLY narrative the user sees in their save-state view. It must be readable without the plot doc. Rules:

- One sentence, roughly 12–40 words.
- Concrete: name the PC(s) and NPC(s) involved, the choice, the consequence-so-far if one already landed.
- Log voice — a neutral diary entry, not the prose of the scene. Reserve purple language for the turn's actual narrative.
- Include session/beat context only if it would be unclear otherwise.

Bad  → `"decision": "Chose option B"`   (machine label, useless out of context)
Bad  → `"decision": "Yes"`               (ambiguous)
Good → `"decision": "RedVelvet took the Pacifica detour before the broadcast; Sato handed over her personal recording as a primary source, accelerating Calloway's verification window"`

#### Irreconcilable plot break

If the player's choice is so far outside the plot doc that no defined branch or divergence can cover it, STOP and tell them OOCly so the plot doc can be updated before continuing. Do NOT invent keys or stretch an existing branch to paper over the gap."""
