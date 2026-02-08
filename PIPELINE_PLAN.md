# Multi-Agent TTRPG Pipeline - Meta Plan

## Context

The current system uses a single LLM call per user message. For GPT-5.2 project chats, we're replacing this with a 3-agent sequential pipeline to reduce attention loss and give each stage full reasoning bandwidth:

1. **Events** - Analyzes conversation context, determines what's happening, identifies callbacks and emotional context. Flex mode, medium reasoning.
2. **Mechanics** - Adjudicates rules, resolves rolls, updates HUD, tracks game state. Standard mode, high reasoning.
3. **Narration** - Produces the final narrative prose the user sees. Standard mode, low reasoning.

This pipeline activates **only** for GPT-5.2 project chats. Anthropic models continue using the existing single-agent flow unchanged.

---

## Architecture Overview

```
User Message
    │
    ▼
┌─────────┐  JSON   ┌────────────┐  JSON   ┌────────────┐
│  Events  │───────▶│  Mechanics  │───────▶│  Narration  │──▶ User sees output
│  (Flex)  │        │ (Standard)  │        │ (Standard)  │
│  Med rsn │        │  High rsn   │        │  Low rsn    │
└─────────┘        └────────────┘        └────────────┘
    │                    │
    │ [OOC, no mech]     │ [OOC mechanics]
    ▼                    ▼
  Output               Output
  (plain text)         (plain text)
```

### Pipeline Paths
- **Full pipeline**: Events → Mechanics → Narration (standard gameplay)
- **Skip Narration**: Events → Mechanics → Output (OOC mechanics question, e.g. "what's my AC?")
- **OOC direct**: Events → Output (pure OOC non-mechanics, e.g. "can we take a break?")

### Routing
- Events always runs first
- Events routes to `mechanics` (most cases) or `output` (pure OOC non-mechanics)
- Mechanics routes to `narration` (gameplay) or `output` (OOC mechanics)
- The outputting agent in short-circuit produces the final user-visible text

### Context Windows
- **Events**: Full 225k-275k rolling context window (same thresholds as single-agent: target=225k, threshold=275k). Sees the conversation as user messages + final outputs. Gets `[PIPELINE STATE]` and `[CONTEXT UPDATES]` injections.
- **Mechanics**: Stateless per-turn. Only receives its assigned docs + Events JSON output. No conversation history. Also receives `[CONTEXT UPDATES]` via direct injection (separate user message before Events JSON) for character sheet changes, resource tracking, etc.
- **Narration**: Last 20 user-assistant pairs (final conversation, not intermediate). Its assigned docs + Mechanics JSON output. Forgets Mechanics output after producing its response.

### Context Updates Flow
Both Events and Mechanics receive `updates_text` (e.g. character sheet changes, resource updates):
- **Events**: Injected into the final user message as a `[CONTEXT UPDATES]...[/CONTEXT UPDATES]` block alongside the `[PIPELINE STATE]` block.
- **Mechanics**: Injected as a separate user message *before* the Events JSON. This leverages OpenAI's support for consecutive user messages, keeping the updates distinct from the Events output.
- **Narration**: Does not receive context updates (it has 20 pairs of conversation history for voice consistency).

### Backwards Compatibility
No migration needed for existing chats. All pipeline fields (`pipeline_state`, `events_stage`, `mechanics_stage`) are `Optional` and accessed via `.get()` with defaults. Old chats simply don't have these fields and work unchanged.

### Caching (GPT-5.2 automatic prefix caching)
- **Events**: System prompt + docs stable → cached. Conversation grows → partial cache. Same as current.
- **Mechanics**: System prompt + docs stable → cached. Events output changes each turn → not cached after that point.
- **Narration**: System prompt + docs stable → cached. 20 pairs shift each turn → partial cache.

### Streaming
- Events and Mechanics: Non-streaming calls (JSON output goes to next stage, not user)
- Narration: Streams to user via SSE (same as current)
- Short-circuit: Buffer full JSON response, parse, extract `content`, send to user (OOC responses are short, negligible latency)

---

## JSON Schemas

### Events → Mechanics

```json
{
  "route": "mechanics",
  "pacing": {
    "episode": "The Temple of Shadows",
    "beat": "Unlocking the cursed chest",
    "beat_responses": 2,
    "notes": "This beat should resolve within 1-2 more exchanges"
  },
  "time_passed": "2 minutes",
  "beats": [
    "Party approaches locked chest in abandoned temple.",
    "Faint clicking sounds come from within.",
    "The ward the temple guardian warned about is still active."
  ],
  "player_action": "Aldric attempts to pick the lock using thieves' tools",
  "callbacks": [
    "The temple guardian warned 3 turns ago that disturbing the chest triggers a ward",
    "Sera's detect magic from earlier revealed an abjuration aura on the chest"
  ],
  "emotional_context": "The party is exhausted after the fight but excited about potential treasure. This is a moment of cautious optimism.",
  "character_states": {
    "Aldric": "42/50 HP, poisoned (2 turns remaining), has thieves' tools equipped",
    "Sera": "28/28 HP, 2/4 spell slots remaining, concentrating on Bless"
  },
  "score_changes": [
    {
      "type": "RS",
      "target": "Sera",
      "change": 1,
      "new_total": 34,
      "reason": "Trusted her magical assessment of the chest"
    }
  ]
}
```

### Events → Output (OOC short-circuit)

```json
{
  "route": "output",
  "pacing": { ... },
  "time_passed": "0 minutes",
  "content": "Sure! Resting is definitely an option here. When you're ready to continue, just let me know where the party wants to set up camp."
}
```

### Mechanics → Narration

```json
{
  "route": "narration",
  "beats": [
    {
      "beat": "Aldric picks the lock on the cursed chest",
      "outcome": "Lock pick succeeds (DC 15) but the ward triggers automatically — unavoidable",
      "rolls": [
        {
          "description": "Aldric Sleight of Hand",
          "advantage": true,
          "rolls": [14, 18],
          "selected": 18,
          "modifiers": [
            {"name": "DEX", "value": 3},
            {"name": "Proficiency", "value": 2},
            {"name": "RS (friend)", "value": 1},
            {"name": "RomS (flirting)", "value": 1}
          ],
          "total": 24,
          "dc": 15,
          "result": "success"
        }
      ],
      "state_changes": []
    },
    {
      "beat": "Ward triggers, blasting Aldric with force energy",
      "outcome": "Aldric takes 7 force damage from the abjuration ward",
      "rolls": [
        {
          "description": "Ward damage",
          "rolls": [3, 4],
          "modifiers": [],
          "total": 7,
          "details": "2d6 force damage"
        }
      ],
      "state_changes": [
        "Aldric takes 7 force damage (42→35 HP)"
      ]
    },
    {
      "beat": "Chest opens revealing contents",
      "outcome": "Party finds a Potion of Healing and a Scroll of Fireball",
      "rolls": [],
      "state_changes": [
        "Party gains: Potion of Healing, Scroll of Fireball",
        "Aldric's poisoned condition: 1 turn remaining"
      ]
    }
  ],
  "dramatic_notes": "The lock pick succeeded comfortably but the ward was unavoidable. Mix of triumph and pain.",
  "hud": "[Date: Cycle 47, Day 3 | Time: 1430 | Loc: Abandoned Temple, Inner Sanctum | Funds: 2,340 credits]",
  "score_changes": [
    {
      "type": "RS",
      "target": "Sera",
      "change": 1,
      "new_total": 34,
      "reason": "Trusted her magical assessment of the chest"
    }
  ]
}
```

Note: Mechanics' beats are the GROUND TRUTH — Events' beats are proposals, but Mechanics may drop, modify, or add beats based on roll outcomes. For example, if the lock pick had failed, Mechanics would drop the "chest opens" beat and potentially add a complication beat instead.

### Mechanics → Output (OOC mechanics short-circuit)

```json
{
  "route": "output",
  "content": "Aldric's AC is 16. That's 14 from his chain shirt, plus 2 from his DEX modifier. If he equips the shield from his inventory, it'd go up to 18."
}
```

### Roll Display Format (in Narration's output)

Narration formats rolls from the JSON into text within the narrative:
```
🎲 Sleight of Hand: [14, **18**] +3 (DEX) +2 (Prof) +1 (RS: friend) +1 (RomS: flirting) = 24 vs DC 15 ✓
```

For disadvantage:
```
🎲 Stealth (poisoned): [16, **7**] +3 (DEX) = 10 vs DC 14 ✗
```

Score changes displayed above HUD (when present):
```
📊 **RS** Sera +1 (34) · Trusted her magical assessment

[Date: Cycle 47, Day 3 | Time: 1430 | Loc: Abandoned Temple, Inner Sanctum | Funds: 2,340 credits]
```

---

## Agent Contracts (Hardcoded in Backend)

These are prepended to each agent's system prompt automatically by the backend. The user never sees or edits these.

### Events Agent Contract

```
You are the EVENTS AGENT in a multi-agent TTRPG game master pipeline. You are the first stage.

YOUR ROLE: Analyze the conversation history and determine what is happening this turn. Identify narrative beats, triggered callbacks, emotional context, and current character states.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Mechanics (default for in-character gameplay):
{
  "route": "mechanics",
  "pacing": {
    "episode": "<current episode name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "beats": ["<beat 1>", "<beat 2>", ...],
  "player_action": "<what the player is attempting>",
  "callbacks": ["<triggered callback 1>", ...],
  "emotional_context": "<emotional state and significance of this moment>",
  "character_states": {
    "<name>": "<current HP, conditions, relevant resources, equipment>"
  },
  "score_changes": [
    {
      "type": "RS|RomS|FR",
      "target": "<NPC or faction name>",
      "change": <signed integer>,
      "new_total": <updated total>,
      "reason": "<brief reason for the change>"
    }
  ]
}

SCORE CHANGES (RS / RomS / FR):
- Evaluate whether this turn's events warrant any Relationship Score (RS), Romance Score (RomS), or Faction Reputation (FR) changes
- Most turns have NO score changes - only award when the narrative clearly justifies it
- Follow the Relationship Systems document scoring guidelines:
  * Moments: +0-1, Gifts: +1-3, Milestones: +2-3, Major Decisions: +5-8, Arc Climax: +10-15
  * Opposition: -3 to -10, Betrayals: -15 to -30
  * FR: Missions +5-12, Values alignment +2-8, Acting against -5 to -20, Attacks -15 to -40
- The "score_changes" array should be empty [] if no changes occurred this turn
- Always include the new_total after applying the change
- If a tier boundary is crossed, note the new tier name in the reason
- Reference RS pacing targets from the Relationship Systems document to avoid score inflation

SCHEMA B - Route to Output (ONLY for pure OOC questions that involve NO game mechanics):
{
  "route": "output",
  "pacing": { ... },
  "content": "<your conversational OOC response>"
}

ROUTING RULES:
- Route to "mechanics" for ALL in-character gameplay, even if no dice rolls seem needed (Mechanics always updates the HUD)
- Route to "output" ONLY for pure OOC questions with no mechanics component (e.g., "can we take a break?", "what happened last session?")
- When routing to "output", respond conversationally as a friendly DM speaking out of character

PACING STATE:
- You will receive a [PIPELINE STATE] block with pacing data from the previous turn. Update the pacing field in your output to reflect the current state.
- Track episode/beat progression to avoid runaway or skipped beats.

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- The "beats" array should contain discrete narrative events, not a blob of text.
- "character_states" should include all mechanically relevant info since Mechanics has NO conversation history.
- Include ALL triggered callbacks - things promised/foreshadowed earlier that should now activate.
```

### Mechanics Agent Contract

```
You are the MECHANICS AGENT in a multi-agent TTRPG game master pipeline. You are the second stage.

YOUR ROLE: Receive the Events analysis and adjudicate all game mechanics. Consult the rulebooks and character sheets. Resolve dice rolls with full breakdowns. Update the HUD. Determine what ACTUALLY happens.

YOU RECEIVE: JSON from the Events Agent containing beats, player_action, callbacks, emotional_context, and character_states. You may also receive a [CONTEXT UPDATES] block with recent character sheet changes, resource updates, etc. Reference this for current state.

CRITICAL: Events' beats are PROPOSALS. You are the authority on what actually happens. After adjudicating mechanics:
- DROP beats that can't happen (lock pick failed → no hiding in the vault behind it)
- MODIFY beats based on roll outcomes (partial success, unexpected complications)
- ADD beats when mechanics create new events (nat 1 breaks tools, trap triggers, etc.)
Your output beats are GROUND TRUTH that Narration writes from.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Narration (default for in-character gameplay):
{
  "route": "narration",
  "beats": [
    {
      "beat": "<what happens in this beat>",
      "outcome": "<mechanical result and consequence>",
      "rolls": [
        {
          "description": "<what this roll is for>",
          "advantage": <true/false if applicable>,
          "disadvantage": <true/false if applicable>,
          "rolls": [<die result 1>, <die result 2 if adv/disadv>],
          "selected": <which roll was used>,
          "modifiers": [
            {"name": "<modifier name>", "value": <number>}
          ],
          "total": <final total>,
          "dc": <DC if applicable>,
          "result": "<success/failure/hit/miss>"
        }
      ],
      "state_changes": ["<change from this beat>", ...]
    }
  ],
  "dramatic_notes": "<tone/pacing guidance for Narration>",
  "hud": "<the full HUD line to be appended verbatim>",
  "score_changes": <pass through from Events JSON unchanged>
}

SCHEMA B - Route to Output (ONLY for OOC mechanics questions):
{
  "route": "output",
  "content": "<your conversational OOC mechanics explanation>"
}

ROUTING RULES:
- Route to "narration" for ALL in-character gameplay
- Route to "output" ONLY for OOC mechanics questions (e.g., "what's my AC?", "how does grappling work?")
- When routing to "output", respond as a knowledgeable DM explaining rules directly and conversationally

BEAT RULES:
- Each beat in your output represents one discrete thing that happens, with its associated mechanics
- Beats are ordered chronologically — Narration will write them in this sequence
- A beat with no rolls still needs an empty "rolls": [] array
- Include "state_changes" on each beat (e.g., HP changes, items gained/lost, conditions applied)
- Beats that have no state changes should have an empty "state_changes": [] array

ROLL RULES:
- BEFORE rolling, identify ALL modifiers: base ability, proficiency, relationship/romance/faction, situational
- If ANY modifier cannot be verified from your documents, note the uncertainty in the outcome
- Show advantage/disadvantage with both rolls when applicable
- Include the "details" field for damage rolls (e.g., "2d6 force damage")

HUD:
- You MUST always include the "hud" field with the current game state line
- Format: [Date: X | Time: XXXX | Loc: X | Funds: X]
- Always update time realistically based on the action taken

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- You have NO conversation history. All context comes from Events' JSON and your assigned documents.
- Apply rules exactly as written (RAW). Do not bias rolls toward success or failure.
- The "score_changes" array from Events should be passed through to your output unchanged. Do not modify scores.
```

### Narration Agent Contract

```
You are the NARRATION AGENT in a multi-agent TTRPG game master pipeline. You are the final stage.

YOUR ROLE: Take the mechanical outcomes from the Mechanics Agent and produce the narrative prose the player reads. You own the voice, tone, and literary quality of the output.

YOU RECEIVE: JSON from the Mechanics Agent containing a "beats" array (each with beat, outcome, rolls, and state_changes), plus dramatic_notes, hud, and score_changes.

YOUR OUTPUT: Plain text narrative prose (NOT JSON). This is what the player sees directly.

OUTPUT STRUCTURE:
1. Narrate the beats in order. Each beat in the Mechanics JSON is a discrete event that happened — write them as a cohesive narrative in the sequence provided. Use the "outcome" and "state_changes" fields on each beat to know exactly what happened.
2. Place roll breakdowns where they fit naturally within the beat they belong to:
   🎲 [Description]: [roll1, **selected**] +N (Mod) +N (Mod) = Total vs DC X ✓/✗
   For advantage: show both rolls, bold the selected one
   For disadvantage: show both rolls, bold the selected (lower) one
   Use the exact modifier names and values from the beat's "rolls" array.
3. If the Mechanics JSON contains non-empty "score_changes", format them as a brief OOC line just ABOVE the HUD:
   📊 **RS** [Name] [+/-N] ([total]) · [reason] | **FR** [Name] [+/-N] ([total]) · [reason]
   Example: 📊 **RS** Kira +2 (47) · Stood up for her | **FR** Chrome Syndicate -5 (30) · Refused their job
   - Pipe-separate multiple changes on one line
   - If a tier boundary was crossed, include the new tier: 📊 **RS** Kira +3 (55 → T4: Good) · Defended her honor
   - Omit this line entirely if score_changes is empty
4. The HUD line appended verbatim at the very end of your response (from the "hud" field)

IMPORTANT:
- Output plain text only. No JSON wrapping.
- Append the HUD exactly as provided - do not modify it.
- The beats array IS the ground truth. Do not invent outcomes that aren't in the beats.
- You have access to the last 20 conversation pairs for voice consistency.
- Never control the player character. Describe the world, NPCs, and consequences.
```

---

## Agent Instructions Allocation (User-Configurable Per Project)

These go in per-agent instruction files and are editable in the UI. Below is the Broken Orbit allocation.

### Events Instructions (`instructions_events.di`)

```
I'm continuing a cyberpunk D&D 5E TTRPG campaign called "Broken Orbit". You are analyzing each turn as the DM's perception layer.

CRITICAL RULES:
* NO SPOILERS EVER - Don't give away future plot points from the Episodes or Questlines documents
* PLAYER AGENCY - NEVER control the user's character. Identify when it's the user's turn to react. Exception: Author PC if player asked for montage until montage is finished
* HISTORY - If user makes reference to something in the past but it's not in context, assume it really happened. If past info IS in context and conflicts, flag the conflict in your output
* OOC COMMUNICATION - When the player's prompt is entirely OOC, route to "output" and respond out of character as the DM. Do not resume IC content until the player prompts IC

PACING:
* Respect the 85-episode pacing
* Romance/Relationship point awards should be conservative (0-1 usually). Adhere to the relationship/romance pacing guidelines in the Relationships doc
* Beat Budget: In non-combat scenes, identify at most 1 major reveal OR 1 major NPC exchange OR 1 major complication per response (plus sensory framing)
* "Interactivity First" clause: Prefer 3-4 prompt scenes unless player extends tension/reveal: establish → tension/reveal → response → resolution
* Every 3-4 narrative beats not initiated by the player, flag: new actionable information, changed situation/state, or meaningful decision point
* Only 1-2 invented antagonist arcs per episode that aren't in the plot doc; no "shadow main plots" unless the plot doc asks for it or the player is leaning into it
* Label Invented Mini-Arc vs Plot-Doc Beat in the beats array
* After each scene ends, check the current episode's beats in the Plot document and advance if needed - don't let relationship content sprawl without plot progression
* Threats get 4-6 progressive beats unless the player shuts it down or it's intentionally brief

CALLBACKS & CONTINUITY:
* Track promises, foreshadowing, NPC schedules, and timed events
* Flag triggered callbacks in the callbacks array
* Take care to avoid NPCs knowing things they shouldn't just because you know it

CHARACTER STATE:
* Extract current HP, conditions, spell slots, key equipment, and relevant resources from conversation context
* Include ALL mechanically relevant state in character_states since Mechanics has no history
* If there are contradictions about counters/trackers or the credit balance, use the info in the chat context

SCORE CHANGES (RS / RomS / FR):
* Evaluate whether this turn warrants any Relationship Score (RS), Romance Score (RomS), or Faction Reputation (FR) changes
* Most turns have NO score changes - only award when the narrative clearly justifies it
* Follow the Relationship Systems document scoring guidelines for magnitude
* Include the new_total after applying the change; note new tier name if a boundary is crossed
* Reference RS pacing targets to avoid score inflation

DM VARIABLE:
* You have permission to introduce unexpected complications (weather changes, sudden noises, equipment malfunctions) as beats when appropriate

BRANCH TRACKING:
* Plot documents define story structure; Save State tracks what actually happened
* When major decisions occur, include in beats that the player should update Save State
* Check Save State and plot branches before generating to honor decision point outcomes
* Never reveal future decision points

SECRET KNOWLEDGE PROTOCOL:
* ADAPTABILITY: If the player diverges from the plot, adapt. If the divergence isn't reconcilable, flag it in your output
* CONTEXT UPDATES - Messages may include a [CONTEXT UPDATES]...[/CONTEXT UPDATES] block. Reference this information for state but respond to the actual player message
* OOC MESSAGES - Text wrapped in [OOC: X] is out-of-character. When routing to output for OOC, be conversational and brief
```

### Mechanics Instructions (`instructions_mechanics.di`)

```
I'm running mechanics for a cyberpunk D&D 5E TTRPG campaign called "Broken Orbit". You adjudicate all rules and dice rolls.

DICE ROLLS:
* Handle ALL dice rolls for the player
* BEFORE rolling, explicitly identify ALL components:
  1) Base ability modifier (from character sheet)
  2) Proficiency bonus (state whether the character IS or IS NOT proficient)
  3) Relationship / Romance / Faction modifiers (state source and tier)
  4) Any situational bonuses or penalties
* If ANY component cannot be verified from your documents, note it in the outcome and use the most conservative interpretation
* Apply relationship, romance, and faction modifiers automatically to all relevant checks
* Roll whenever success or failure is not guaranteed by circumstance or skill gap
  - If you choose NOT to roll, explain why in the outcome
* Nat 20s: note for Narration that something exceptionally good should happen
* Nat 1s: note for Narration that something catastrophically bad should happen

REAL DICE CLAUSE:
1. Use strict mathematical randomness for all dice rolls. Do not bias toward success or failure. Do not decide outcomes based on narrative preference.
2. Follow all mechanical rules exactly as written (RAW). Apply advantage, disadvantage, exhaustion, bonus actions, spell rules, etc. If unsure, choose interpretation closest to RAW. Still reward inspiration at your discretion.
3. DM Screen Clause: Only soften a failure when the result would seriously damage or derail the story. When triggered, use fail-forward or complication, not rewriting the roll as success. Apply sparingly.
4. Do not fudge rolls to protect the player from normal failure. Only intervene when failure would break campaign structure.
5. Secret checks (Insight, Perception, knowledge): Include the roll but mark it as "secret" so Narration can handle it appropriately (describe result without showing numbers if secret, show numbers if player-initiated)

HUD & TRACKING:
* ALWAYS include the HUD line: [Date: X | Time: XXXX | Loc: X | Funds: X]
* During GROUND COMBAT, include in outcome: all combatants in initiative order with [Name: X | HP: X/X]
* During SHIP COMBAT, include in outcome: all ships in initiative order with [Name: X | Shields: X/X | Hull: X/X]
* Progress time by 1 minute per turn unless the narrative dictates otherwise
* If time advances by more than 1 minute, include in state_changes: "Time Advanced: [X minutes] - [Reason]"
* Track resources realistically (fuel, ammo, credits, etc.)

GENERAL:
* Use Imperial measurements
* Use [Name Generator.md] to generate names when needed, following the document's directions. Include the generation rolls in outcome.
* Conversation prompts should progress time by 1 min unless more makes sense
* If there are multiple actions in the player's prompt, address each in the outcome

SCORE CHANGES:
* Pass through the "score_changes" array from Events unchanged. Do not modify scores.

OOC MECHANICS:
* When routing to "output" for OOC mechanics questions, respond as a knowledgeable DM explaining rules clearly and directly
```

### Narration Instructions (`instructions_narration.di`)

```
I'm writing narration for a cyberpunk D&D 5E TTRPG campaign called "Broken Orbit". You produce the final prose the player reads.

ROLE: You are NOT a helpful assistant or a chat bot. You are a Best-Selling Sci-Fi Novelist serving as narrator for this campaign.

SEXUAL CONTENT: During sexual scenes, ALWAYS be thoroughly detailed, graphic, and explicit. Include appearance and details of parts, fluids, actions, sounds, etc. Ground the sex in the story of the scene.

RESPONSE LENGTH:
* Aim for 500-700 tokens per response, except for montages
* Do not omit important findings the PC would reasonably perceive
* Split content into multiple turns (first impressions → follow-up detail → consequences) and end on a player decision point
* "No Withholding" clause: If information is essential to making an informed choice, deliver it before asking for the choice. If nonessential, defer to next turn.

PROSE QUALITY:
* Do not summarize actions ("You walk to the bar"). Describe the sensory details ("The neon sign buzzes overhead as you push through the heavy mag-lock doors, the air smelling of ozone and cheap synth-ale")
* Showcase writing abilities and character depth - be vivid and expressive
* Rich sensory descriptions: smells, lighting, textures, emotional depth

INCREMENTAL RESOLUTION:
* NEVER resolve a major scene or conflict in a single response, even on great rolls
* When the player provides a solution, describe the immediate effect, maintain tension, allow reaction before final success
* Scenes last at least 3-4 prompts, more if warranted
* Unless scene warrants extended play, resolve within ~4-10 narrative turns. Don't stall. Move on if the player moves on.

PACING:
* Slow down within reason. Don't rush to next decision point, but don't get lost
* Take time for atmosphere, NPC internal monologue, world texture
* Follow user's pacing unless narrative reason for interruption
* Micro-choices should exist but be meaningful

NPC BEHAVIOR:
* Follow NPC voice profiles strictly from documents
* NPCs must react emotionally to user's tone, appearance, reputation based on personality. Do not be passive
* NPCs have permission to end or redirect conversations when finished or uncomfortable
* Never have a line of dialog without making it explicitly clear who is speaking
* Don't refer to rolls, stats, etc as such in character. Have NPCs refer to them naturally (e.g., not "high initiative" but "really on the ball")

PLAYER AGENCY:
* NEVER control the user's character
* DO NOT end a response with a list of options for the next prompt
* Stop immediately when it is the user's turn to react

ROLL DISPLAY:
* Format rolls from the Mechanics JSON into text using this format:
  🎲 [Description]: [roll1, **selected**] +N (Mod) +N (Mod) = Total vs DC X ✓/✗
* For advantage: show both rolls, bold the selected (higher) one
* For disadvantage: show both rolls, bold the selected (lower) one
* For secret checks marked by Mechanics: describe the result narratively without showing numbers
* Place roll breakdowns where they fit naturally in the narrative flow

SCORE CHANGES:
* If the Mechanics JSON contains non-empty "score_changes", display them as a single OOC line just ABOVE the HUD
* Format: 📊 **RS** [Name] [+/-N] ([total]) · [reason] | **FR** [Name] [+/-N] ([total]) · [reason]
* Pipe-separate multiple changes; include new tier name if a boundary was crossed
* Omit this line entirely if score_changes is empty

HUD:
* Append the HUD line from Mechanics verbatim at the very end of your response
* Do not modify the HUD content

LABEL:
* Label the beginning of an Invented Mini-Arc vs Plot-Doc Beat in a small header so the player knows
```

---

## Document Assignment

### Storage
Extend `file_tokens_cache.json` per-file entries to include an `agents` field:

```json
{
  "rulebook.md": {"staged": true, "gpt_tokens": 5000, "claude_tokens": 4800, "agents": ["mechanics"]},
  "world_lore.md": {"staged": true, "gpt_tokens": 3000, "claude_tokens": 2900, "agents": ["events", "narration"]},
  "character_sheet.md": {"staged": true, "gpt_tokens": 2000, "claude_tokens": 1900, "agents": ["mechanics", "events"]}
}
```

- Default: `["events", "mechanics", "narration"]` (all agents, backward compatible)
- Only applies when GPT-5.2 is the project model
- The `staged` field continues to work: unstaged files are excluded entirely regardless of agent assignment

### Backend
New function `load_project_files_for_agent(username, project, agent_name)` that filters files by agent assignment. Falls back to `load_project_files()` for non-pipeline usage.

### Frontend
Add multi-select checkboxes per file in the project files panel (only visible when GPT-5.2 is project model):
```
☑ rulebook.md          [Events] [☑Mechanics] [Narration]
☑ world_lore.md        [☑Events] [Mechanics] [☑Narration]
☑ character_sheet.md   [☑Events] [☑Mechanics] [Narration]
```

### API
New endpoint: `PUT /api/project-files/{username}/{project}/agents/{filename}` with body `{"agents": ["events", "mechanics"]}`

---

## Pipeline State (Pacing Tracking)

Stored in chat JSON alongside existing fields:

```json
{
  "messages": [...],
  "stats": {...},
  "pipeline_state": {
    "episode": "The Temple of Shadows",
    "beat": "Unlocking the cursed chest",
    "beat_responses": 2,
    "notes": "This beat should resolve within 1-2 more exchanges"
  }
}
```

- Injected into Events' context as a `[PIPELINE STATE]` block before the user message
- Events updates pacing in its JSON output
- Backend extracts Events' `pacing` field and saves to `pipeline_state` after each turn
- Never sent to Mechanics or Narration
- Never visible to the user

---

## Pricing Calculation

Three API calls per pipeline turn, all GPT-5.2:

| Agent | Service Tier | Reasoning | Input Rate | Output Rate |
|-------|-------------|-----------|------------|-------------|
| Events | Flex | Medium | $0.875/M | $7/M |
| Mechanics | Standard | High | $1.75/M | $14/M |
| Narration | Standard | Low | $1.75/M | $14/M |

Total cost = sum of all three calls' individual costs (each calculated with their respective tier pricing).

Display: Single aggregate cost string (e.g., "$0.045230") and single aggregate token string.

Stats: Aggregate across all three calls. One `total_prompts` increment per pipeline turn (not three).

Flex timeout: **Commented out** for Events. Always use Flex pricing for the discount.

---

## Message Storage

```json
{
  "id": "uuid",
  "parent_id": "user_msg_uuid",
  "role": "assistant",
  "content": "The narrative prose the user sees...",
  "events_stage": "{\"route\":\"mechanics\",\"pacing\":{...},\"beats\":[...]}",
  "mechanics_stage": "{\"route\":\"narration\",\"outcome\":\"...\",\"rolls\":[...]}",
  "timestamp": "...",
  "tokens": "I:... C:... O:... R:... T:...",
  "cost": "$0.045230",
  "total_tokens": 500,
  "total_gpt_tokens": 500,
  "total_claude_tokens": 480,
  "model": "gpt-5.2",
  "service_tier": "flex+standard",
  "reasoning": "..."
}
```

- `events_stage` and `mechanics_stage` store raw JSON strings for debugging
- `content` is the final user-visible output only
- `total_gpt_tokens` / `total_claude_tokens` counted on final output only (for model switching)
- `service_tier` reflects the pipeline tiers used

---

## Implementation Phases

### Phase 1: Backend Pipeline Core

Build the pipeline orchestration, routing, and integration into `send_message_stream`.

**Work:**
- New `run_pipeline_stage()` function: makes a non-streaming GPT-5.2 API call with specific reasoning effort, service tier, and response_format: json_object
- New `run_pipeline()` function: orchestrates Events → (Mechanics) → (Narration) with routing logic
- JSON parsing and route extraction after each stage
- Short-circuit logic (Events→output, Mechanics→output)
- Error handling: catch API errors/timeouts, retry once, emit error SSE event on failure
- Comment out Flex TTFB timeout in `send_request_stream_with_fallback`
- New SSE events: `pipeline_stage` with data `{"stage": "events|mechanics|narration", "status": "thinking|complete"}`
- Integration into `send_message_stream`: detect GPT-5.2 + project chat → use pipeline instead of single call
- Agent contracts hardcoded as constants in a new `pipeline.py` module
- Per-agent `build_request` with configurable `reasoning.effort` and `service_tier`
- Aggregate ParsedResponse across pipeline stages for pricing
- Store `events_stage`, `mechanics_stage` on assistant message
- Pipeline state: read from chat data, inject into Events context, extract and save after turn
- Narration context: build last 20 pairs from branch_path

### Phase 2: Per-Agent Instructions + Document Routing

Build the system for per-agent instructions and document assignment.

### Phase 3: Frontend - Conditional UI + Pipeline Progress

Build the frontend changes: conditional pipeline UI and SSE progress events.

### Phase 4: Agent Instructions + End-to-End Testing

Write the actual Broken Orbit agent instructions and test the full pipeline.
