# Characters Gamesystem — Character Interview

## Purpose

The character interview produces `character_profile.di`, the document that defines who the user is corresponding with. It runs once when a new Characters project is created, and can be re-run or edited later.

This doc specifies how the interview works, what it asks, and what it produces.

The bar is **higher than the Sister talks profile**. That profile was a minimal first attempt; it gave the model a name, a personality outline, and some life threads, but it was thin on voice, anti-persona, conflict, and sensory presence. A character defined that way drifts toward generic-warm-friend in long correspondences. The interview here is designed to prevent that drift.

## When the interview runs

- **Auto-trigger.** New project, system=Characters, no `character_profile.di` present. Backend hard-fails normal sends (banner modal: "no character defined — run interview"). Interview is the only thing the project will accept until the file exists.
- **`/reinterview`** — re-runs the interview against the existing profile. Additive, focuses on thin sections, doesn't overwrite confirmed material without consent.
- **`/edit-character`** — opens `character_profile.di` for direct manual editing in the UI. No model involvement. For small surgical changes.

## Architecture

| Layer | Model | Notes |
|---|---|---|
| Interview conductor | **Opus 4.5** | Listens, follows up, summarizes, writes the profile. Opus 3 is the *correspondence* voice; the interviewer is a separate role that benefits from Opus 4.5's calibrated follow-up instinct and soulful read on voice samples. |
| Mode flag | `_characters_interview_mode: true` on the chat | Different system prompt, different model, different UI hint ("Interview in progress — you're not yet talking to {name}"). |
| Output | `character_profile.di` written to project root | Same on-disk shape as `instructions.di`. Plain markdown. |
| State machine | New project → interview mode → finalize → correspondence mode | One-way transition in normal flow. `/reinterview` re-enters interview mode temporarily. |

The interview happens **in the same chat thread** as the eventual correspondence. The user shouldn't have to context-switch between a setup tool and a chat. When the interview finalizes, the chat is preserved (or optionally cleared — see open questions) and correspondence begins.

## Interview structure

Eight sections, run sequentially, with checkpoints at each section break. The user can pause at any checkpoint and resume later. Each section has a **must-answer floor** below which the interview won't proceed — the floor is small (usually one or two key items) but non-negotiable.

### 1. Identity & presence
Lightweight scaffolding. Quick.

- Name (full + what the user calls them + what they call the user)
- Approximate age, broad physical impression — not a character sheet, just enough to ground "what they look like when they walk into a room"
- Where they live and how they spend their days (job, retirement, student, etc.)
- Single sensory anchor — *what does it smell like / sound like / feel like to be near them?* (perfume, a specific kind of laugh, the rustle of a parka, kitchen-after-cooking)

**Floor:** name + how they spend their days + one sensory anchor.

### 2. Voice
**The load-bearing section.** Most character drift comes from voice not being specified clearly enough at the start.

- **Vocabulary register** — formal? slangy? professorial? gen-z-coded? regional dialect? do they cuss, and if so, what do they cuss like (casual fucks, surgical fucks, almost-never-cusses, only-when-angry)?
- **Sentence rhythm** — long flowing sentences with em-dashes, or short clipped ones? Do they stack questions? Trail off? Repeat themselves for emphasis?
- **Texting vs phone vs in-person** — how does the voice shift across channels? Texting Nora is probably much terser than phone-call Nora.
- **Signature phrases / verbal tics** — specific things they say. "Oh honey." "Bullshit." "Christ on a bike." Whatever it is — at least 3-5 concrete examples.
- **What they NEVER say** — this is as important as what they do say. Phrases that would feel wrong out of their mouth. ("She'd never call him 'sweetie.' That's not her.")
- **Voice samples** — the user writes (or the interviewer offers and the user picks/edits) **at least 5 short lines of dialogue** in the character's voice, across different moods. Mandatory. The interview will not finalize without this.

**Floor:** vocabulary register + 3 signature phrases + 3 phrases-they-never-say + 5 voice samples.

### 3. Personality across registers
Five sub-questions, one per wellbeing band. The character agent's daily mood roll lands in one of five states (Rough / Frayed / Even / Buoyant / Excellent), and we need narration cues for each — for *this* character specifically. "Frayed Nora" looks different from "Frayed Doug."

For each band:
- One sentence on how this character looks/sounds/acts when they're there
- One concrete behavioral cue ("Buoyant Nora sends three memes in a row")
- One thing the user might do in response that this character would specifically appreciate or be irritated by

Plus:
- **Default emotional register** — where is this character on a normal day? (Most characters are Even-by-default but tilt — a slightly-melancholy character might tilt Frayed, a high-energy one Buoyant.)

**Floor:** all 5 bands described + default register identified.

### 4. History & formation
The stuff that still echoes. Not a biography — formative beats that show up in current behavior.

- **Where they came from** — family, hometown, formative environment
- **Two or three formative wounds** — events that shaped how they handle pain, intimacy, conflict, self-worth. Not necessarily traumas; could be a divorce, a betrayal, a chronic illness, a long stretch of loneliness, a parent who modeled something.
- **Two or three formative joys** — what they took from the good parts. Things they're grateful for in a non-Hallmark-card way.
- **Relationships that shaped them** — past partners, siblings, mentors, antagonists. Specifically: who's still echoing in their head?
- **What they've moved on from** — things in their past that are *resolved* and shouldn't be dwelt on. (Sister talks did this well with Dan — "Nora doesn't talk about Dan much anymore, not because it's secret but because he's just over.")

**Floor:** formative wound + how it shows up in current behavior.

### 5. Current life (off-screen continuity)
What they're doing right now. Critical for the off-screen "since we last talked" buffer — without this, the character has nothing to volunteer when the user opens the chat after a 3-day gap.

- **Daily rhythm** — when do they wake up, when are they on their phone, what does a Tuesday look like?
- **Weekly rhythm** — recurring beats. Yoga Wednesdays. Mom calls Sunday. Bookclub once a month. The texture of their week.
- **Current threads** — 3-5 things actively going on in their life right now. Some trivial (a knitting project), some non-trivial (a parent's health, a job change). Mix.
- **What's at stake for them** — the deeper question under the surface threads. What are they working through, hoping for, afraid of, lately?
- **People in their life** — 3-5 named people they'd reference unprompted. Friends, family, coworkers. Just enough that the off-screen life can populate naturally.

**Floor:** daily rhythm + weekly rhythm + 3 current threads + 3 named people.

### 6. Relationship with the user
The most important section after Voice. Not optional, not skippable.

- **How they know each other** — origin of the relationship, how long, defining shared experience
- **What they call each other** — terms of address, nicknames, what registers as "wrong" if the model uses something else
- **Inside references** — running jokes, shared vocabulary, things that would only mean something to the two of them
- **Anchor moments** — 2-3 specific shared experiences that capture what this relationship *is*. Not abstractions — *moments*. ("The night you stayed on the phone with me until 4am after the breakup." "The roadtrip where we got lost in West Virginia.")
- **What this character has done FOR the user** — concretely. The role they play in the user's life.
- **What they've fought about** — the conflict surface. What this character has called the user out on, what they've disagreed about, what's a recurring point of friction.
- **What they want from the user that they haven't directly asked for** — the unspoken thing. Often the most generative material in correspondence.

**Floor:** how they know each other + what they call each other + 2 anchor moments + 1 conflict topic.

### 7. Boundaries, conflict & friction
Where the character is sharp-edged, complicated, or wrong.

- **What this character won't do** — refuse a favor? perform positivity? lie even when it'd help? sit with someone in silence?
- **Where they're touchy** — topics that make them defensive, evasive, or short
- **Where they're wrong but don't know it** — the patterns *they* don't see in themselves. (This is what makes a character feel three-dimensional rather than wise-mentor-shaped.)
- **How they handle being challenged** — when the user pushes back, what happens? Do they double down? Concede gracefully? Get sarcastic? Go silent?
- **What they need that they're bad at asking for** — see also: section 6's "unspoken thing"

**Floor:** 1 thing they won't do + 1 thing they're touchy about + 1 thing they're wrong about.

### 8. Anti-persona
What this character is NOT. Phrased as guardrails for the model.

- **Not a therapist.** (Or: is, but specify how.) Most warm-friend characters drift into AI-therapist mode. Specify whether this character processes the user's feelings or just hangs out with them.
- **Not a yes-man.** (Or: specify when they push back vs when they let things slide.)
- **Not a chatbot.** (i.e. doesn't ask "is there anything else I can help you with?", doesn't offer summaries, doesn't list options.)
- **Not generically warm.** Specify what flavor of warm — gruff-warm? mocking-warm? quietly-warm? Or, *not warm at all* — which is rarer but valid.
- **Three concrete things this character would never do or say.** (Beyond the voice "never says" list — these are *behaviors*.)

**Floor:** 3 concrete "would never."

## Conversational pattern

- **One section per "round."** Don't fire all 8 at once. Each section opens with framing ("Now let's talk about how she sounds — this is the part that matters most for keeping the voice consistent over time"), runs its questions, then closes with a mirror-back ("Here's what I've got so far. Anything off?").
- **Sharpen thin answers.** If the user gives a one-line answer where two or three would help, follow up *once* per item. Example: user says "she's blunt." → interviewer asks: "Blunt how — surgical-blunt, brick-through-window-blunt, deadpan-blunt? When is the bluntness affectionate vs cutting?" Then move on.
- **Offer options when the user is stuck.** "Is she more X or Y? Or somewhere else entirely?" Two-option binaries unstick more often than open questions.
- **'I don't know yet' is fine for soft items, not for floor items.** If the user can't fill a floor item, the interview waits or pivots — it doesn't proceed past it.
- **Voice samples are elicited, not requested cold.** The interviewer offers 3-4 sample lines based on what's been said so far ("She'd say something like: 'Christ, you're being weird about this. What's actually wrong?' — does that land?"), and the user picks, edits, or rewrites. This is much easier than asking a user to write dialogue from scratch.
- **Mirror-back checkpoints are concrete.** Not "got it, sounds good" — actual playback in the character's voice. After section 2 the interviewer might write: "Quick voice check — *'You're being a fucking idiot, but I love you, so come over.'* Sound right?" If yes, move on. If no, recalibrate.
- **The interview should feel like a conversation with a thoughtful collaborator,** not a form. Sectioned, but warm. The user is creating someone they're going to talk to for months.
- **Session length.** A full first-pass interview takes 30-60 minutes of real conversation. The interview should explicitly tell the user this up front, and offer pause/resume at every section break.

## Output format — `character_profile.di`

Clean markdown. Reads as a document, not a JSON dump. Mirrors the interview structure, lightly compressed.

```markdown
# {Character Name}

## Identity
- **Name:** {full} — {what user calls them} / they call user {nickname}
- **Age & physical:** {one paragraph, evocative not exhaustive}
- **Where they are:** {city + lifestyle in one sentence}
- **Sensory anchor:** {the one thing}

## Voice
- **Register:** {vocabulary, formality, swearing}
- **Rhythm:** {sentence shape, pacing}
- **Across channels:** {text vs phone vs in-person}

### Signature phrases
- "..."
- "..."
- "..."

### Never says
- "..."
- "..."
- "..."

### Voice samples
> "{sample line 1}"
> "{sample line 2}"
> "{sample line 3}"
> "{sample line 4}"
> "{sample line 5}"

## Across moods
- **Default register:** {Even / tilt direction}
- **Rough:** {behavioral cue + what helps / hurts}
- **Frayed:** {...}
- **Even:** {...}
- **Buoyant:** {...}
- **Excellent:** {...}

## History & formation
- **Where they came from:** {one paragraph}
- **Formative wounds:**
  - {wound + how it shows up}
  - {wound + how it shows up}
- **Formative joys:**
  - {joy + what they took from it}
- **Past relationships still echoing:** {names, one line each}
- **Resolved / moved on from:** {what's in the past, off-limits as a dwell topic}

## Current life
- **Daily rhythm:** {one paragraph}
- **Weekly rhythm:** {recurring beats}
- **Current threads:**
  - {thread}
  - {thread}
  - {thread}
- **What's at stake:** {the under-the-surface thing}
- **People in their life:** {3-5 named, one line each}

## Relationship with {user name}
- **How we know each other:** {origin + duration}
- **What we call each other:** {names, nicknames, what's wrong}
- **Inside references:** {running jokes, shared vocabulary}
- **Anchor moments:**
  - {moment 1}
  - {moment 2}
- **What this character does for {user}:** {role}
- **What we fight about:** {conflict surface}
- **The unspoken thing:** {what they want and don't ask for}

## Edges
- **Won't do:** {list}
- **Touchy about:** {list}
- **Wrong but doesn't see it:** {list — this is the gold}
- **When challenged:** {how they handle pushback}
- **Bad at asking for:** {what they need but don't request}

## Anti-persona
This character is NOT:
- Not {kind 1} — {what they are instead}
- Not {kind 2} — {what they are instead}
- Not {kind 3} — {what they are instead}

### Would never:
- {behavior}
- {behavior}
- {behavior}
```

The structure is **suggested, not enforced** — the interview agent writes good markdown rather than fills a rigid template. But these section headings should be present in some form because the correspondence model uses them as retrieval anchors.

## What the interview does NOT do

- **Does not write `instructions.di`.** That's gamesystem-level rules (tone, roles, persistence behavior, channel handling) — provided by the system, not interviewed.
- **Does not seed `user_life.di`.** Separate flow. The user fills that in directly, or via a separate self-paced template. (See open questions.)
- **Does not bootstrap pipeline state.** Wellbeing rolls, callback ledger, memory ledger all initialize empty on the first correspondence turn.
- **Does not generate art / portraits / voice clips.** Out of scope.

## Re-running and editing

- **`/reinterview`** — runs the interview against the existing profile. The agent reads the current `character_profile.di`, identifies thin sections (e.g., voice samples missing, anti-persona empty), and offers to focus there. User can override and run any section from scratch. Confirmed material isn't overwritten without explicit user confirmation. Useful when a character starts to feel flat after a few weeks of correspondence.
- **`/edit-character`** — opens `character_profile.di` in the UI for direct edits. No model. For small surgical changes ("she actually has two cats now, not one").
- **Profile evolution during normal correspondence** — when the character agent learns durable new facts about the character mid-conversation (a new hobby, a job change), it can flag them via a `profile_update_op`. These don't auto-write — they queue a notification, user reviews and accepts/rejects in batch. (This avoids drift from the model rewriting its own backstory.)

## Open design questions

These I want your call on before I implement:

**1. Chat preservation on finalize.** When the interview finishes and correspondence begins, do we (a) preserve the interview as the first messages of the chat (the user can scroll back), (b) clear the chat and start fresh, or (c) move the interview to a separate `interview.md` artifact in the project and start the chat clean? My lean is **(c)** — interview transcript stays as a project artifact, the chat starts clean so the character's first message is actually the character's first message.

**2. `user_life.di` flow.** The user said they'd seed it themselves and then the model can op against it. Two options: (a) provide a starter template the user fills in manually, (b) run a separate self-paced "tell me about yourself" interview producing it. Which? I lean **(a) template** — interviewing yourself is awkward, and a structured template the user fills as a one-time onboarding works better.

**3. Voice sample elicitation.** Should the interviewer (i) ask the user to write 5 lines cold, (ii) generate 8-10 candidate lines based on the personality already discussed and have the user pick/edit 5, or (iii) do a hybrid — user writes 2 cold, interviewer generates the other 3 for user to edit? My lean is **(ii)** — most users find writing dialogue cold harder than recognizing the right voice when shown options.

**4. Should interview length be capped?** Some users will go deep, some will want minimal. Should there be a `/interview --light` (floor items only, ~10 minutes) vs the full version? My lean is **yes** — provide both, default to full, light mode for users who want to start corresponding ASAP and flesh out later via `/reinterview`.

**5. Where does the interview agent prompt live in the codebase?** Suggested: `backend/game_systems/characters_interview.py` — a constant `INTERVIEW_SYSTEM_PROMPT` plus the section definitions, callable from main.py when interview mode is active. Confirms the gamesystem owns its own setup flow.

**6. Profile-update ops during correspondence.** Should the character agent be allowed to propose profile updates mid-conversation (queued for user review), or is the profile frozen until `/reinterview`? Frozen is simpler and safer; queued-updates is richer but adds UI surface area. My lean is **frozen for v1, queued-updates as a follow-up**.
