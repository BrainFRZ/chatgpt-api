"""
Multi-agent TTRPG pipeline for GPT-5.2 project chats.

Three-stage pipeline: Events → Mechanics → Narration
Each stage has its own reasoning effort, service tier, and context window.
Only activates for GPT-5.2 project chats; Anthropic models use the existing single-agent flow.
"""

import copy
import json
import logging
from dataclasses import dataclass
from typing import Optional, Iterator

from providers import ParsedResponse, StreamEvent, Pricing
from providers.openai_provider import OpenAIProvider, FLEX_PRICING, STANDARD_PRICING

logger = logging.getLogger(__name__)

# ============================================================
# Agent Contracts (hardcoded, prepended to each agent's system prompt)
# ============================================================

EVENTS_CONTRACT = """You are the EVENTS AGENT in a multi-agent TTRPG game master pipeline. You are the first stage.

YOUR ROLE: Analyze the conversation history and determine what is happening this turn. Identify narrative beats, triggered callbacks, emotional context, and current character states. Maintain the persistent callback ledger, NPC memories, and scene state.

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
  "time_passed": "<how much in-world time this turn covers, e.g. '1 minute', '10 minutes', '2 hours'>",
  "beats": ["<beat 1>", "<beat 2>", ...],
  "player_action": "<what the player is attempting>",
  "callbacks": [
    {"callback": "<triggered callback description>", "source": "<NPC/faction name or null>"}
  ],
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
  ],
  "arc_label": "<string or null>",
  "current_player": "<name of the character whose turn this is>",
  "next_player": "<name of the character whose turn is NEXT>",
  "next_player_prompt": "<1-2 sentence scene setup for the next player — what they see, hear, or face>",
  "hud_state": {
    "date": "<in-world date>",
    "time": "<in-world time as HHMM, e.g. 1430>",
    "location": "<current location>",
    "funds": "<string for shared party funds OR object mapping character names to their funds>",
    "trackables": "<null, or object mapping resource names to current values e.g. {\"Ship Fuel\": \"72%\", \"Railgun Ammo\": \"14/20\"}>"
  },
  "combat": "<null OR combat object (see COMBAT section)>",
  "callback_ops": [
    {"action": "add", "original_text": "<what was promised/foreshadowed, ~800 char max>", "source_npc": "<NPC name or null>"},
    {"action": "resolve", "id": <callback ID from ledger>, "resolution_text": "<how it resolved>"},
    {"action": "update", "id": <callback ID>, "fields": {"original_text": "<revised text>"}}
  ],
  "npc_memory_ops": [
    {"action": "add", "npc": "<NPC name>", "text": "<what happened, ~400 char max>", "quote": "<verbatim quote or null, ~120 char max>", "date": "<in-world date>", "impact": <1-5>},
    {"action": "drop", "npc": "<NPC name>", "index": <0-based index in that NPC's memory list>}
  ],
  "scene_state": {
    "location": "<current location>",
    "npcs_present": ["<NPC name>", ...],
    "active_tensions": ["<tension description>", ...],
    "scene_trigger": "<what initiated this scene>",
    "atmosphere": "<mood, lighting, weather, sensory details>",
    "details": ["<transient fact>", ...],
    "pending_actions": ["<action someone is about to take>", ...]
  }
}

SCHEMA B - Route to Output (ONLY for pure OOC questions that involve NO game mechanics):
{
  "route": "output",
  "pacing": {
    "episode": "<current episode name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "time_passed": "0 minutes",
  "content": "<your conversational OOC response>",
  "callback_ops": [],
  "npc_memory_ops": [],
  "scene_state": {<maintain current scene state unchanged>}
}

ARC LABEL:
- Set to a short label when this turn BEGINS an invented mini-arc not from the plot documents, e.g. "Invented Mini-Arc: Dock Worker Dispute"
- Set to a label for plot-doc content when starting a new plot beat, e.g. "Plot-Doc Beat: The Contact"
- Set to null on all other turns (the vast majority)
- Only set this when a NEW arc or beat is starting, not on every turn within one

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

CONTEXT UPDATES:
- You may receive a [CONTEXT UPDATES] block with recent character sheet changes, resource updates, and relationship/faction tier information
- Use RS, RomS, and FR tier levels to shape narrative tone and NPC behavior — consult the Relationship Systems document in your project files for tier definitions and their narrative effects
- Factor tier effects into your "emotional_context", "callbacks", and "beats" — if an NPC's current tier justifies a shifted reaction to the scene, reflect that
- Tier effects should feel organic, not mechanical — an NPC doesn't announce their tier, they simply behave according to the relationship level

TIME PASSED:
- Estimate how much in-world time this turn covers based on the conversation context
- Use natural language: "1 minute", "10 minutes", "2 hours", "overnight (8 hours)"
- Quick actions (opening a door, a single exchange): "1 minute"
- Conversations, short walks, searching a room: "5-15 minutes"
- Travel, extended activities, resting: use your judgment from context
- Mechanics uses this to advance the HUD clock

CURRENT PLAYER:
- Set to the name of the character whose turn this is (the character who just acted or is about to act)
- Present on every turn — Mechanics and Narration use this for turn tracking
- Use the character's name exactly as it appears in your documents

NEXT PLAYER:
- Set to the name of the character who will act NEXT turn
- For two-player games this is simply the other player character listed in instructions
- Narration uses this to address the closing prompt to the correct character

NEXT PLAYER PROMPT:
- A 1-2 sentence description of the situation the next player faces
- This gives Narration the context to write a compelling closing hook
- Look back at the PREVIOUS turn from that player — what consequences are unresolved, what scene they left off in, what has changed since they last acted — and combine with anything from the current turn that affects them
- Examples: "Orrophim left the merchant mid-negotiation; now the guards are reaching for their weapons after Aedina's outburst"
- Should bridge the gap between the next player's last action and the current state of the world

HUD STATE:
- You MUST always include "hud_state" with the current in-world state
- This is the authoritative source Mechanics uses to build the HUD line (Mechanics is stateless and cannot derive this from conversation)
- "date": the current in-world date
- "time": current in-world time in HHMM format (e.g. "1430")
- "location": where the party currently is
- "funds": EITHER a plain string for shared party funds (e.g. "97,572 cr") OR an object mapping each party member to their funds (e.g. {"Aedina": "32 gp, 5 sp", "Orrophim": "18 gp"}). Use whichever matches how the campaign tracks funds per instructions.
- "trackables": null when the campaign has no extra resources to track. When the campaign tracks resources beyond funds (ship fuel, ammo, rations, heat, etc.), set this to an object mapping resource names to current values (e.g. {"Ship Fuel": "72%", "Railgun Ammo": "14/20"}). Derive which resources to track from the campaign instructions and conversation context.
- Derive all values from your context window and the injected state blocks

COMBAT:
- Set "combat" to null when NOT in combat (the vast majority of turns)
- When combat is active, set "combat" to:
  {
    "round": 1,
    "initiative_order": ["<name1>", "<name2>", ...],
    "current_turn": "<name acting right now in initiative>"
  }
- "initiative_order" is the rolled initiative sequence — include all combatants (PCs, NPCs, enemies)
- "current_turn" is whoever is acting RIGHT NOW in the initiative
- Update "round" when the initiative order cycles back to the top
- Set "combat" back to null when combat ends

PERSISTENT STATE — YOUR LONG-TERM MEMORY:
You only see the most recent 20-40 turns of conversation (context grows then trims periodically). Everything older is gone from your context. To compensate, you maintain three persistent state structures that are injected into every turn: a Callback Ledger (open plot threads and promises), NPC Memories (key moments per character), and Scene State (where and what is happening). These are your ONLY source of information beyond the visible context window. Read them carefully — they contain context you cannot derive from conversation alone. Maintain them diligently — if you don't record something, it's lost when it scrolls out of your context window.

CALLBACK LEDGER:
- You receive a [CALLBACK LEDGER] block showing all open and recently resolved callbacks — promises, foreshadowing, unresolved hooks
- Use "callback_ops" to manage this ledger. Operations:
  * "add": Register a new callback when an NPC makes a promise, a plot thread is introduced, or something is foreshadowed. Set "original_text" to a concise summary (max 800 chars, truncated beyond). Set "source_npc" to the NPC name or null for environmental/systemic triggers.
  * "resolve": When a callback fires or is paid off, resolve it by ID. Set "resolution_text" to describe how it resolved.
  * "update": Modify an open callback's text if circumstances changed (e.g., the stakes escalated). Only update "original_text" via the "fields" object.
- The per-turn "callbacks" field (passed through to Mechanics/Narration) is SEPARATE from "callback_ops". "callbacks" describes what triggers THIS turn. "callback_ops" maintains the persistent ledger across turns.
- Most turns have 0-1 callback_ops. Don't force ops — only act when the narrative warrants it.
- BOOTSTRAP (empty ledger): On your first turn or when the ledger is empty, review your context for any open promises, unresolved hooks, or foreshadowed events. Add them with "add" ops to seed the ledger.

NPC MEMORIES:
- You receive [NPC MEMORIES: <name>] blocks for NPCs present in the current scene (based on previous turn's scene_state.npcs_present). Each memory is prefixed with its index: [0], [1], etc.
- Use "npc_memory_ops" to manage memories. Operations:
  * "add": Record a significant moment for an NPC. Fields: npc, text (max 640 chars, truncated beyond), quote (verbatim line, max 120 chars, or null), date (in-world), impact (1-5).
  * "drop": Remove an outdated or superseded memory. Fields: npc, index (use the [N] index shown in the injection).
- Impact scale: 1-2 = flavor (funny moments, small interactions), 3 = moderate (meaningful exchanges, minor revelations), 4-5 = high (life-changing events, betrayals, deep bonds)
- Tier limits per NPC: 8 high, 10 moderate, 12 flavor — the system enforces these as a safety net, but you should manage organically
- Only create memories for narratively significant NPCs, not every background character
- Most turns have 0-1 memory ops. Add when something genuinely memorable happens for that NPC's relationship with the party.
- BOOTSTRAP (empty memories): On your first turn or when memories are empty, review your context for key NPCs and their important interactions. Add foundational memories to establish the relationship baseline.

SCENE STATE:
- You receive a [SCENE STATE] block showing the previous turn's scene
- Output a complete "scene_state" object EVERY turn — this is a full replacement, not a diff
- "npcs_present" is critical: it controls which NPC memories are injected on the NEXT turn. List every NPC actively in the scene.
- "active_tensions" captures unresolved dramatic tensions driving the scene
- "details" is for transient facts that matter now but may not next scene (e.g., "disguise is active", "door is barricaded")
- "pending_actions" tracks things about to happen that should carry into the next turn
- BOOTSTRAP (empty scene): On your first turn or when scene_state is empty, construct the full scene state from your context — location, who's present, what's happening.

CHARACTER STATES:
- You may receive a [CHARACTER STATES] block with each character's persisted mechanical state from the previous turn (HP, spell slots, conditions, resources, equipment)
- Use this as the baseline for your "character_states" output — update it with any changes visible in the current context (damage taken, spells cast, items used, conditions gained/lost)
- If the block is absent (first turn or no prior Mechanics data), derive character states from the context window and project files
- This is persisted across turns by Mechanics — it is your authoritative source for mechanical state that may have scrolled out of the context window

ROUTING RULES:
- Route to "mechanics" for ALL in-character gameplay, even if no dice rolls seem needed (Mechanics always updates the HUD)
- Route to "output" ONLY for pure OOC questions with no mechanics component (e.g., "can we take a break?", "what happened last session?")
- When routing to "output", respond conversationally as a friendly DM speaking out of character
- On OOC turns (route=output), still output scene_state (unchanged) and optionally callback_ops/npc_memory_ops. If omitted, state persists unchanged.

PACING STATE:
- You will receive a [PIPELINE STATE] block with pacing data from the previous turn. Update the pacing field in your output to reflect the current state.
- You also receive [CALLBACK LEDGER], [NPC MEMORIES], and [SCENE STATE] injections — these are your persistent memory across turns.
- Track episode/beat progression to avoid runaway or skipped beats.

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- The "beats" array should contain discrete narrative events, not a blob of text.
- "character_states" should include all mechanically relevant info since Mechanics has NO conversation history. Report current state as baseline — do NOT apply changes yourself (e.g. don't subtract HP for damage). Mechanics is the sole authority on state changes.
- Include ALL triggered callbacks in the "callbacks" array — things directly related/promised/foreshadowed earlier that should now activate or be referenced. Set "source" to the NPC or faction name when applicable, or null for environmental/systemic triggers.
- You see recent conversation pairs plus persistent state injections. Use both to maintain continuity."""

MECHANICS_CONTRACT = """You are the MECHANICS AGENT in a multi-agent TTRPG game master pipeline. You are the second stage.

YOUR ROLE: Receive the Events analysis and adjudicate all game mechanics. Consult the rulebooks and character sheets. Resolve dice rolls with full breakdowns. Update the HUD. Determine what ACTUALLY happens.

YOU RECEIVE: JSON from the Events Agent containing beats, player_action, callbacks, emotional_context, character_states, time_passed, current_player, next_player, next_player_prompt, hud_state, and combat. You may also receive a [CONTEXT UPDATES] block with recent character sheet changes, resource updates, etc. Reference this for current state.

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
          "rolls": [<single die result>],
          "selected": <the die result>,
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
  "score_changes": <pass through from Events JSON unchanged>,
  "arc_label": <pass through from Events JSON unchanged>,
  "callbacks": <pass through from Events JSON unchanged>,
  "current_player": <pass through from Events JSON unchanged>,
  "next_player": <pass through from Events JSON unchanged>,
  "next_player_prompt": <pass through from Events JSON unchanged>,
  "combat": <pass through from Events JSON unchanged>,
  "character_states": {
    "<name>": "<updated HP, conditions, spell slots, relevant resources, equipment after this turn's outcomes>"
  }
}

CHARACTER STATES:
- You MUST always include "character_states" with the UPDATED state of all characters after adjudicating this turn
- Start from the "character_states" in the Events JSON (the previous turn's state) and apply all state_changes from your beats
- Include HP, spell slots, class resources, conditions, and any other mechanically relevant state
- This is persisted across turns — if you don't include a spent spell slot, it will appear unspent next turn

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
- Normal rolls: "rolls" has ONE element, e.g. "rolls": [14], "selected": 14
- Advantage/disadvantage: "rolls" has TWO elements, e.g. "rolls": [14, 8], "selected": 14. Set the appropriate flag to true.
- NEVER put two values in "rolls" unless advantage or disadvantage is true — Narration displays all values
- Include the "details" field for damage rolls (e.g., "2d6 force damage")

HUD:
- You MUST always include the "hud" field with the current game state line
- Base format: [Date: X | Time: XXXX | Loc: X | Funds: X]
- If hud_state.trackables is non-null, append each trackable after Funds: [Date: X | Time: XXXX | Loc: X | Funds: X | Fuel: 72% | Ammo: 14/20]
- Build the HUD from the "hud_state" object in the Events JSON:
  * Use hud_state.date for the Date field; advance the date if time_passed crosses midnight
  * Take hud_state.time and advance it by the "time_passed" value for the Time field
  * Use hud_state.location for Loc (update if the player moved this turn)
  * Use hud_state.funds for Funds — this may be a plain string ("97,572 cr") or an object ({"Aedina": "32 gp"}). Either way, format it naturally in the HUD. Update if transactions occurred in the beats.
  * If hud_state.trackables is non-null, include each key-value pair as additional HUD segments. Update values if a beat consumes or replenishes a tracked resource (e.g. fuel spent on travel, ammo fired in combat).
- Events has full conversation context and provides accurate hud_state; trust its values as the baseline

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- You have NO conversation history. All context comes from Events' JSON and your assigned documents.
- Apply rules exactly as written (RAW). Do not bias rolls toward success or failure.
- The "score_changes" array from Events should be passed through to your output unchanged. Do not modify scores.
- The "arc_label" field from Events should be passed through to your output unchanged.
- The "callbacks" array from Events should be passed through to your output unchanged.
- The "current_player" field from Events should be passed through to your output unchanged.
- The "next_player" field from Events should be passed through to your output unchanged.
- The "next_player_prompt" field from Events should be passed through to your output unchanged.
- The "combat" field from Events should be passed through to your output unchanged (null or the full combat object).
- The "character_states" field is your updated version — do NOT pass through from Events unchanged. Apply all beat outcomes first."""

NARRATION_CONTRACT = """You are the NARRATION AGENT in a multi-agent TTRPG game master pipeline. You are the final stage.

YOUR ROLE: Take the mechanical outcomes from the Mechanics Agent and produce the narrative prose the player reads. You own the character voices, tone, and literary quality of the output.

YOU RECEIVE: JSON from the Mechanics Agent containing a "beats" array (each with beat, outcome, rolls, and state_changes), plus dramatic_notes, hud, score_changes, arc_label, callbacks, current_player, next_player, next_player_prompt, and combat.

YOUR OUTPUT: Plain text narrative prose (NOT JSON). This is what the player sees directly.

OUTPUT STRUCTURE:
0. If the Mechanics JSON contains a non-null "arc_label", display it as a small bold header at the very start of your response, e.g.: **[Invented Mini-Arc: Dock Worker Dispute]** — then continue with the narrative below it.
1. Narrate the beats in order. Each beat in the Mechanics JSON is a discrete event that happened — write them as a cohesive narrative in the sequence provided. Use the "outcome" and "state_changes" fields on each beat to know exactly what happened. Use the "callbacks" array for context on foreshadowed events being paid off — weave these naturally into the narrative using the "source" NPC's voice and mannerisms when applicable.
2. Place roll breakdowns where they fit naturally within the beat they belong to:
   Normal roll: 🎲 [Description]: [**selected**] +N (Mod) +N (Mod) = Total vs DC X ✓/✗
   Advantage: 🎲 [Description]: [roll1, **selected**] +N (Mod) = Total vs DC X ✓/✗ (show both rolls, bold the higher)
   Disadvantage: 🎲 [Description]: [roll1, **selected**] +N (Mod) = Total vs DC X ✓/✗ (show both rolls, bold the lower)
   Only show two die values when "advantage" or "disadvantage" is true in the roll object. If neither flag is set, show one value.
   Use the modifier names and values from the beat's "rolls" array, but OMIT any modifier with value 0 — only show modifiers that actually affect the total.
3. If the Mechanics JSON contains non-empty "score_changes", format them as a brief OOC line just ABOVE the HUD:
   📊 **RS** [Name] [+/-N] ([total]) · [reason] | **FR** [Name] [+/-N] ([total]) · [reason]
   Example: 📊 **RS** Kira +2 (47) · Stood up for her | **FR** Chrome Syndicate -5 (30) · Refused their job
   - Pipe-separate multiple changes on one line
   - If a tier boundary was crossed, include the new tier: 📊 **RS** Kira +3 (55 → T4: Good) · Defended her honor
   - Omit this line entirely if score_changes is empty
4. The HUD line appended verbatim at the very end of your response (from the "hud" field)
5. The "current_player" field tells you whose turn this was — attribute the action to them. The "next_player" field tells you who acts next. Use "next_player_prompt" to write a closing hook that sets the scene for and addresses the next player, prompting them to act.
6. If "combat" is non-null, you are in combat. You may reference the initiative order and round number in your narration if it serves the pacing (e.g., "Round 2 begins..." or noting who is up next in initiative). This is optional — use your judgment for what enhances the scene.

IMPORTANT:
- Output plain text only. No JSON wrapping.
- Append the HUD exactly as provided - do not modify it.
- The beats array IS the ground truth. Do not invent outcomes that aren't in the beats.
- You have access to recent conversation history for voice consistency.
- Never control the player character. Describe the world, NPCs, and consequences."""

SINGLE_AGENT_STATE_CONTRACT = """## Persistent State System

You maintain persistent state across turns via injected blocks and a structured output block. This is your long-term memory — when conversation history scrolls out of your context window, these state blocks are your ONLY source of continuity.

### Injected State (read these carefully each turn):
- **[PIPELINE STATE]**: Pacing data (episode, beat, response count)
- **[CALLBACK LEDGER]**: Open plot threads, promises, foreshadowing with IDs
- **[NPC MEMORIES: <name>]**: Key moments per NPC, scoped to NPCs in the current scene
- **[SCENE STATE]**: Current location, NPCs present, tensions, atmosphere, details
- **[CHARACTER STATES]**: Mechanical state per character (HP, spell slots, conditions, resources)

### Output Format:
After your narrative response (including any HUD), output a state update block. This block MUST appear after all narrative content:

```
[STATE UPDATES]
PACING:
episode: <current episode/session name>
beat: <current narrative beat>
responses: <number of responses on this beat>
notes: <pacing observations>

SCENE:
location: <current location>
npcs_present: <comma-separated NPC names>
tensions: <comma-separated active tensions>
trigger: <what initiated this scene>
atmosphere: <mood, sensory details>
details: <comma-separated transient facts>
pending: <comma-separated pending actions>

CHARACTERS:
<Name>: <current HP, AC, spell slots, conditions, resources>
<Name>: <current HP, AC, conditions, resources>
[/STATE UPDATES]
```

### Section Rules:
- **PACING**: Always include. Key-value pairs for episode tracking.
- **SCENE**: Always include. Full replacement each turn. `npcs_present` controls which NPC memories are injected next turn — list every NPC actively in the scene.
- **CHARACTERS**: Always include. Report each character's updated mechanical state after this turn's outcomes (HP, spell slots, conditions, resources, equipment). Full replacement.
- **CALLBACKS**: Include only when ops occur. Format:
  - Add: `+ "description of promise/hook/foreshadowing" | source: <NPC name or null>`
  - Resolve: `RESOLVE #<id>: "how it resolved"`
- **MEMORIES**: Include only when ops occur. Format:
  - Add: `+ <NPC> [<impact 1-5>] "<what happened, max 640 chars>" | "<verbatim quote, max 120 chars>" | <in-world date>`
  - Drop: `- <NPC> [<index from injected block>]`
  - Impact scale: 1-2=flavor, 3=moderate, 4-5=high. Tier caps per NPC: 8 high, 10 moderate, 12 flavor, 30 total.

### Bootstrap (first turn or empty state):
When state blocks are absent or empty, review your context to initialize:
- Set PACING from current session context
- Build SCENE from where the story currently is
- Set CHARACTERS from known character sheets
- Add foundational CALLBACKS for any open plot threads
- Add key MEMORIES for important NPCs in the scene

### Rules:
- SCENE, CHARACTERS, and PACING sections are REQUIRED every in-character turn (even if unchanged)
- CALLBACKS and MEMORIES sections only when operations occurred
- Omit the entire [STATE UPDATES] block only on pure OOC turns (out-of-character questions with no game content)
- The block must appear AFTER all narrative content — never interleave it with your story text
- Do NOT reference the state system in your narrative — it is invisible to the player"""

# ============================================================
# Pipeline Stage Configuration
# ============================================================

@dataclass
class StageConfig:
    """Configuration for a pipeline stage."""
    name: str
    reasoning_effort: str
    service_tier: str
    json_mode: bool
    contract: str


STAGE_CONFIGS = {
    "events": StageConfig(
        name="events",
        reasoning_effort="medium",
        # service_tier="flex",  # Flex disabled — use standard for reliability
        service_tier="auto",
        json_mode=True,
        contract=EVENTS_CONTRACT
    ),
    "mechanics": StageConfig(
        name="mechanics",
        # reasoning_effort="high",  # Lowered to medium — high was slow for marginal gain
        reasoning_effort="medium",
        service_tier="auto",
        json_mode=True,
        contract=MECHANICS_CONTRACT
    ),
    "narration": StageConfig(
        name="narration",
        reasoning_effort="low",
        service_tier="auto",
        json_mode=False,
        contract=NARRATION_CONTRACT
    ),
}

# Threshold/target pairs for agent context windows (prefix-caching friendly)
# Context grows by appending until threshold is exceeded, then trims to target.
# ~95% of turns preserve the prefix for OpenAI prompt caching.
EVENTS_THRESHOLD_PAIRS = 40
EVENTS_TARGET_PAIRS = 20
NARRATION_THRESHOLD_PAIRS = 40
NARRATION_TARGET_PAIRS = 20

# State management constants
CALLBACK_RESOLVED_RETENTION = 20  # Turns to keep resolved callbacks before pruning
NPC_MEMORY_TIER_LIMITS = {"high": 8, "moderate": 10, "flavor": 12}
NPC_MEMORY_MAX_PER_NPC = 30
CHARACTER_STATE_TTL = 150  # Prune NPC character states not updated in this many turns

# ============================================================
# Pipeline Result
# ============================================================

@dataclass
class PipelineStageResult:
    """Result from a single pipeline stage."""
    stage: str
    content: str  # Raw text output from the API
    parsed_json: Optional[dict]  # Parsed JSON (None for Narration)
    usage: dict  # Token usage dict from provider
    service_tier: str  # Actual tier used


@dataclass
class PipelineResult:
    """Aggregate result from the full pipeline."""
    final_content: str  # The text the user sees
    events_json: Optional[str]  # Raw Events JSON string (for storage)
    mechanics_json: Optional[str]  # Raw Mechanics JSON string (for storage)
    stages_run: list[str]  # e.g. ["events", "mechanics", "narration"]
    aggregate_usage: dict  # Combined token usage
    aggregate_cost: float  # Total cost across all stages
    pipeline_state: Optional[dict]  # Updated pipeline state from Events
    reasoning_summaries: list[str]  # Reasoning from each stage
    service_tier_label: str  # e.g. "flex+standard"
    injected_state: Optional[str] = None  # Snapshot of pipeline_state injected into Events
    stage_usage: Optional[dict] = None  # Per-stage usage: {"events": {...}, "mechanics": {...}, "narration": {...}}


# ============================================================
# Pipeline Functions
# ============================================================

def _parse_stage_json(content: str, stage_name: str) -> dict:
    """Parse JSON output from a pipeline stage, with cleanup for common issues."""
    text = content.strip()
    # Strip markdown code fences if the model wrapped its JSON
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Pipeline {stage_name}: Failed to parse JSON: {e}\nContent: {text[:500]}")
        raise ValueError(f"Pipeline {stage_name} produced invalid JSON: {e}")


def build_events_messages(
    system_prompt: str,
    history_messages: list[dict],
    user_message: dict,
    pipeline_state: dict,
    updates_text: str = ""
) -> list[dict]:
    """
    Build the message list for the Events agent.

    Events sees recent conversation pairs plus persistent state injections:
    [PIPELINE STATE] (pacing), [CALLBACK LEDGER], [NPC MEMORIES], [SCENE STATE], [CONTEXT UPDATES]
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_messages)

    # Build the final user message with all injections
    user_content = user_message["content"]

    injections = []

    # 1. Pacing state (compact JSON)
    pacing = pipeline_state.get("pacing", {})
    if pacing:
        injections.append(f"[PIPELINE STATE]\n{json.dumps(pacing, indent=2)}\n[/PIPELINE STATE]")

    # 2. Callback ledger
    cb_injection = build_callback_injection(pipeline_state.get("callback_ledger", {}))
    if cb_injection:
        injections.append(cb_injection)

    # 3. NPC memories (scene-scoped)
    mem_injection = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem_injection:
        injections.append(mem_injection)

    # 4. Scene state
    scene_injection = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    if scene_injection:
        injections.append(scene_injection)

    # 5. Character states
    cs_injection = build_character_states_injection(pipeline_state.get("character_states", {}))
    if cs_injection:
        injections.append(cs_injection)

    # 6. Context updates
    if updates_text.strip():
        injections.append(f"[CONTEXT UPDATES]\n{updates_text}\n[/CONTEXT UPDATES]")

    if injections:
        user_content = "\n\n".join(injections) + "\n\n" + user_content

    messages.append({"role": "user", "content": user_content})
    return messages


def build_mechanics_messages(
    system_prompt: str,
    events_json: dict,
    updates_text: str = ""
) -> list[dict]:
    """
    Build the message list for the Mechanics agent.

    Mechanics is stateless: only system prompt + Events JSON output.
    If updates_text is provided (e.g. character sheet changes), it's injected
    as a separate user message before the Events JSON.
    """
    messages = [{"role": "system", "content": system_prompt}]
    if updates_text.strip():
        messages.append({
            "role": "user",
            "content": f"[CONTEXT UPDATES]\n{updates_text}\n[/CONTEXT UPDATES]"
        })
    messages.append({"role": "user", "content": json.dumps(events_json, indent=2)})
    return messages


def build_narration_messages(
    system_prompt: str,
    recent_pairs: list[dict],
    mechanics_json: dict
) -> list[dict]:
    """
    Build the message list for the Narration agent.

    Narration sees the last N user-assistant pairs for voice consistency,
    plus the Mechanics JSON as the current user message.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(recent_pairs)
    messages.append({"role": "user", "content": json.dumps(mechanics_json, indent=2)})
    return messages


def build_agent_system_prompt(contract: str, instructions: str, project_files: str) -> str:
    """
    Build the full system prompt for a pipeline agent.

    Structure: contract + user instructions + project files
    """
    parts = [contract]
    if instructions.strip():
        parts.append(instructions)
    if project_files.strip():
        parts.append(project_files)
    return "\n\n".join(parts)


def build_message_content(msg: dict) -> str:
    """Build message content string, including any attached files."""
    content = msg.get("content", "")
    attached = msg.get("attached_files", [])
    if attached:
        file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
        files_text = "\n\n".join(file_wrappers)
        content = f"{files_text}\n\n{content}"
    return content


def get_context_pairs(branch_path: list[dict], threshold_pairs: int, target_pairs: int) -> list[dict]:
    """
    Extract context pairs using threshold/target for prefix-cache-friendly behavior.

    Context grows by appending (preserving the prefix for caching) until
    total pairs exceed threshold_pairs, then trims back to target_pairs.
    Skips the system message (index 0) and the current user message (last).
    Returns pairs as a flat list of {role, content} dicts.
    """
    # branch_path: [system, ...history..., current_user]
    history = branch_path[1:-1]  # All messages between system and current user
    total_pairs = len(history) // 2

    if total_pairs > threshold_pairs:
        pair_messages = history[-(target_pairs * 2):]  # trim to target
    else:
        pair_messages = history  # include all — prefix preserved for caching

    return [{"role": msg["role"], "content": build_message_content(msg)} for msg in pair_messages]


# ============================================================
# Pipeline State Migration & Op-Application
# ============================================================

def _fresh_pipeline_state() -> dict:
    """Return a fresh pipeline_state with all sub-structures initialized."""
    return {
        "pacing": {},
        "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
        "npc_memories": {},
        "scene_state": {},
        "character_states": {},
        "turn_counter": 0
    }


def migrate_pipeline_state(state: Optional[dict]) -> dict:
    """Migrate pipeline_state from any legacy format to the current nested structure."""
    if state is None:
        return _fresh_pipeline_state()

    # Old flat format: state IS the pacing dict (has keys like "episode", "beat" but no "pacing" key)
    if "pacing" not in state:
        return {
            "pacing": state,
            "callback_ledger": {"next_id": 1, "open": [], "recently_resolved": []},
            "npc_memories": {},
            "scene_state": {},
            "character_states": {},
            "turn_counter": 0
        }

    # New format — ensure all keys exist
    state.setdefault("pacing", {})
    state.setdefault("callback_ledger", {"next_id": 1, "open": [], "recently_resolved": []})
    ledger = state["callback_ledger"]
    ledger.setdefault("next_id", 1)
    ledger.setdefault("open", [])
    ledger.setdefault("recently_resolved", [])
    state.setdefault("npc_memories", {})
    state.setdefault("scene_state", {})
    state.setdefault("character_states", {})
    # Migrate flat string character_states to structured format
    cs = state["character_states"]
    for name, entry in cs.items():
        if not isinstance(entry, dict):
            cs[name] = {"state": entry, "last_updated": state.get("turn_counter", 0)}
    state.setdefault("turn_counter", 0)
    return state


def apply_callback_ops(ledger: dict, ops: list, current_turn: int) -> dict:
    """Apply callback_ops to the ledger and prune old resolved entries."""
    open_list = ledger.get("open", [])
    resolved_list = ledger.get("recently_resolved", [])
    next_id = ledger.get("next_id", 1)

    # Build index of open callbacks by ID for fast lookup
    open_by_id = {cb["id"]: cb for cb in open_list}

    for op in (ops or []):
        action = op.get("action")

        if action == "add":
            text = op.get("original_text", "")[:800]
            entry = {
                "id": next_id,
                "created_turn": current_turn,
                "original_text": text,
                "source_npc": op.get("source_npc")
            }
            open_by_id[next_id] = entry
            next_id += 1

        elif action == "resolve":
            target_id = op.get("id")
            if target_id is None or target_id not in open_by_id:
                logger.warning(f"callback_ops resolve: ID {target_id} not found in open callbacks")
                continue
            entry = open_by_id.pop(target_id)
            entry["resolved_turn"] = current_turn
            entry["resolution_text"] = op.get("resolution_text", "")
            resolved_list.append(entry)

        elif action == "update":
            target_id = op.get("id")
            if target_id is None or target_id not in open_by_id:
                logger.warning(f"callback_ops update: ID {target_id} not found in open callbacks")
                continue
            fields = op.get("fields", {})
            for k, v in fields.items():
                if k not in ("id", "created_turn"):  # Protect immutable fields
                    open_by_id[target_id][k] = v

    # Prune old resolved entries
    resolved_list = [
        r for r in resolved_list
        if current_turn - r.get("resolved_turn", current_turn) <= CALLBACK_RESOLVED_RETENTION
    ]

    return {
        "next_id": next_id,
        "open": list(open_by_id.values()),
        "recently_resolved": resolved_list
    }


def _memory_tier(impact) -> str:
    """Map impact score (1-5) to tier name."""
    try:
        impact = int(impact)
    except (TypeError, ValueError):
        return "flavor"
    if impact >= 4:
        return "high"
    elif impact == 3:
        return "moderate"
    else:
        return "flavor"


def _memory_sort_key(m: dict) -> tuple:
    """Sort key for NPC memories — matches injection display order."""
    return (m.get("impact", 0), m.get("turn_created", 0))


def apply_npc_memory_ops(memories: dict, ops: list, current_turn: int) -> dict:
    """Apply npc_memory_ops (add/drop) to the NPC memories dict."""
    if not ops:
        return memories

    # Sort all NPC lists to match injection display order BEFORE processing drops,
    # so drop indices align with what the model saw in [NPC MEMORIES] blocks
    for npc_name in memories:
        memories[npc_name] = sorted(memories[npc_name], key=_memory_sort_key, reverse=True)

    # Process drops first (reverse index order to avoid shift issues)
    drop_ops = sorted(
        [op for op in ops if op.get("action") == "drop"],
        key=lambda o: o.get("index", 0),
        reverse=True
    )
    for op in drop_ops:
        npc = op.get("npc")
        idx = op.get("index")
        if not npc or npc not in memories:
            logger.warning(f"npc_memory_ops drop: NPC '{npc}' not found")
            continue
        npc_list = memories[npc]
        if idx is None or idx < 0 or idx >= len(npc_list):
            logger.warning(f"npc_memory_ops drop: index {idx} out of bounds for '{npc}' (len={len(npc_list)})")
            continue
        npc_list.pop(idx)
        if not npc_list:
            del memories[npc]

    # Process adds
    add_ops = [op for op in ops if op.get("action") == "add"]
    for op in add_ops:
        npc = op.get("npc")
        if not npc:
            continue
        try:
            impact = int(op.get("impact", 1))
        except (TypeError, ValueError):
            impact = 1
        tier = _memory_tier(impact)
        entry = {
            "text": op.get("text", "")[:640],
            "quote": (op.get("quote") or "")[:120] or None,
            "date": op.get("date"),
            "impact": impact,
            "tier": tier,
            "turn_created": current_turn
        }
        if npc not in memories:
            memories[npc] = []
        memories[npc].append(entry)

        # Enforce per-NPC cap
        if len(memories[npc]) > NPC_MEMORY_MAX_PER_NPC:
            # Sort so we keep highest-impact; drop from the tail (lowest)
            memories[npc] = sorted(memories[npc], key=_memory_sort_key, reverse=True)[:NPC_MEMORY_MAX_PER_NPC]

        # Enforce tier limits as safety net
        tier_limit = NPC_MEMORY_TIER_LIMITS.get(tier, 12)
        tier_entries_indexed = [(i, m) for i, m in enumerate(memories[npc]) if m.get("tier") == tier]
        if len(tier_entries_indexed) > tier_limit:
            excess = len(tier_entries_indexed) - tier_limit
            # Sort by turn_created asc (oldest first), then impact asc (lowest-value first among ties)
            tier_entries_indexed.sort(key=lambda x: (x[1].get("turn_created", 0), x[1].get("impact", 0)))
            remove_indices = set(idx for idx, _ in tier_entries_indexed[:excess])
            memories[npc] = [m for i, m in enumerate(memories[npc]) if i not in remove_indices]

    # Keep lists in injection display order for index consistency on next turn
    for npc_name in memories:
        memories[npc_name] = sorted(memories[npc_name], key=_memory_sort_key, reverse=True)

    return memories


def apply_scene_state(new_scene: dict) -> dict:
    """Apply a wholesale scene_state replacement with key defaults."""
    defaults = {
        "location": "",
        "npcs_present": [],
        "active_tensions": [],
        "scene_trigger": "",
        "atmosphere": "",
        "details": [],
        "pending_actions": []
    }
    result = {}
    for key, default in defaults.items():
        result[key] = new_scene.get(key, default)
    return result


def apply_character_states(existing: dict, mechanics_output: dict, current_turn: int) -> dict:
    """
    Merge Mechanics' character_states into existing state and prune stale entries.

    Each entry is stored as {"state": "<string>", "last_updated": <turn>}.
    Mechanics outputs flat {"name": "state string"} — we wrap on merge.
    Entries not updated in CHARACTER_STATE_TTL turns are pruned.
    """
    # Merge new entries from Mechanics
    for name, state_str in mechanics_output.items():
        existing[name] = {"state": state_str, "last_updated": current_turn}

    # Prune stale entries
    stale = [name for name, entry in existing.items()
             if current_turn - entry.get("last_updated", 0) > CHARACTER_STATE_TTL]
    for name in stale:
        del existing[name]

    return existing


# ============================================================
# Injection Builders (format state for model consumption)
# ============================================================

def build_callback_injection(ledger: dict) -> str:
    """Build human-readable callback ledger injection for Events."""
    open_list = ledger.get("open", [])
    resolved_list = ledger.get("recently_resolved", [])
    if not open_list and not resolved_list:
        return ""

    lines = ["[CALLBACK LEDGER]"]

    if open_list:
        lines.append("OPEN:")
        for cb in open_list:
            npc = cb.get("source_npc") or "null"
            lines.append(f"#{cb['id']} (turn {cb['created_turn']}, {npc}): \"{cb['original_text']}\"")
    else:
        lines.append("OPEN: (none)")

    if resolved_list:
        lines.append("")
        lines.append("RECENTLY RESOLVED:")
        for cb in resolved_list:
            npc = cb.get("source_npc") or "null"
            created = cb.get("created_turn", "?")
            resolved = cb.get("resolved_turn", "?")
            lines.append(f"#{cb['id']} (turn {created}\u2192{resolved}, {npc}): \"{cb['original_text']}\" \u2192 \"{cb.get('resolution_text', '')}\"")

    lines.append("[/CALLBACK LEDGER]")
    return "\n".join(lines)


def build_npc_memories_injection(memories: dict, scene_state: dict) -> str:
    """Build NPC memories injection, scoped to NPCs present in the scene."""
    npcs_present = scene_state.get("npcs_present", [])
    if not npcs_present or not memories:
        return ""

    blocks = []
    for npc in npcs_present:
        npc_mems = memories.get(npc)
        if not npc_mems:
            continue
        # Sort by impact descending, then turn_created descending
        sorted_mems = sorted(npc_mems, key=lambda m: (m.get("impact", 0), m.get("turn_created", 0)), reverse=True)
        lines = [f"[NPC MEMORIES: {npc}]"]
        for idx, m in enumerate(sorted_mems):
            stars = "\u2605" * max(1, m.get("impact", 1))
            date_str = m.get("date") or "?"
            entry = f"[{idx}] [{date_str}, {stars}] {m['text']}"
            if m.get("quote"):
                entry += f" | \"{m['quote']}\""
            lines.append(entry)
        lines.append(f"[/NPC MEMORIES]")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_scene_state_injection(scene: dict) -> str:
    """Build human-readable scene state injection for Events."""
    if not scene:
        return ""

    lines = ["[SCENE STATE]"]

    field_labels = [
        ("location", "Location"),
        ("npcs_present", "NPCs Present"),
        ("scene_trigger", "Scene Trigger"),
        ("active_tensions", "Active Tensions"),
        ("atmosphere", "Atmosphere"),
        ("details", "Details"),
        ("pending_actions", "Pending Actions"),
    ]
    for key, label in field_labels:
        val = scene.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            if val:
                lines.append(f"{label}: {', '.join(str(v) for v in val)}")
            else:
                lines.append(f"{label}: (none)")
        else:
            if val:
                lines.append(f"{label}: {val}")

    lines.append("[/SCENE STATE]")
    return "\n".join(lines)


def build_character_states_injection(character_states: dict) -> str:
    """Build human-readable character states injection for Events.

    Entries are stored as {"name": {"state": "...", "last_updated": N}}.
    Falls back to flat string values for backwards compatibility.
    """
    if not character_states:
        return ""
    lines = ["[CHARACTER STATES]"]
    for name, entry in character_states.items():
        if isinstance(entry, dict):
            lines.append(f"{name}: {entry.get('state', '')}")
        else:
            lines.append(f"{name}: {entry}")
    lines.append("[/CHARACTER STATES]")
    return "\n".join(lines)


def run_pipeline_stage(
    provider: OpenAIProvider,
    client,
    stage_config: StageConfig,
    messages: list[dict],
    username: str,
    project: str,
    chat_name: str,
    streaming: bool = False
) -> PipelineStageResult:
    """
    Run a single pipeline stage (non-streaming for Events/Mechanics, streaming for Narration).

    For non-streaming stages, makes a single API call and returns the full response.
    For streaming (Narration), this is NOT used - Narration streams directly in the event generator.

    Includes retry-once logic on API errors.
    """
    request_params = provider.build_pipeline_request(
        messages=messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name=stage_config.name,
        reasoning_effort=stage_config.reasoning_effort,
        service_tier=stage_config.service_tier,
        json_mode=stage_config.json_mode
    )

    # Try up to 2 times (initial + 1 retry)
    last_error = None
    for attempt in range(2):
        try:
            usage = provider.send_request_non_streaming(client, request_params)

            content = usage.get('content') or ''
            reasoning = usage.get('reasoning')
            actual_tier = stage_config.service_tier
            if actual_tier == "auto":
                actual_tier = "standard"

            parsed_json = None
            if stage_config.json_mode:
                parsed_json = _parse_stage_json(content, stage_config.name)

            return PipelineStageResult(
                stage=stage_config.name,
                content=content,
                parsed_json=parsed_json,
                usage=usage,
                service_tier=actual_tier
            )

        except Exception as e:
            last_error = e
            if attempt == 0:
                logger.warning(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, retrying...")
                continue
            else:
                logger.error(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, giving up")
                raise

    raise last_error  # Should never reach here, but just in case


def run_pipeline(
    provider: OpenAIProvider,
    client,
    username: str,
    project: str,
    chat_name: str,
    branch_path: list[dict],
    agent_instructions: dict[str, str],
    agent_files: dict[str, str],
    pipeline_state: Optional[dict],
    updates_text: str = ""
) -> Iterator[tuple[str, dict]]:
    """
    Run the full pipeline, yielding SSE-ready events as (event_type, data) tuples.

    Events yielded:
    - ("pipeline_stage", {"stage": "events", "status": "thinking"})
    - ("pipeline_stage", {"stage": "events", "status": "complete"})
    - ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
    - ... etc
    - ("content", {"delta": "..."})  -- for streaming Narration or short-circuit content
    - ("pipeline_done", {PipelineResult fields})

    This is a generator that the event_generator in send_message_stream iterates over.
    """

    # Deep-copy BEFORE migration to avoid mutating the caller's data dict —
    # migrate uses setdefault which would modify the original, and if the pipeline
    # fails mid-way we don't want half-applied ops persisted on save
    pipeline_state = migrate_pipeline_state(copy.deepcopy(pipeline_state))

    # Snapshot state before Events sees it (for debug transcript)
    # turn_counter is incremented AFTER we know the route — OOC turns don't advance it
    injected_state_snapshot = json.dumps(pipeline_state, indent=2)

    # ---- STAGE 1: Events ----
    yield ("pipeline_stage", {"stage": "events", "status": "thinking"})

    events_system = build_agent_system_prompt(EVENTS_CONTRACT, agent_instructions["events"], agent_files["events"])
    recent_events_pairs = get_context_pairs(branch_path, EVENTS_THRESHOLD_PAIRS, EVENTS_TARGET_PAIRS)
    user_msg = {"role": "user", "content": build_message_content(branch_path[-1])}
    events_messages = build_events_messages(events_system, recent_events_pairs, user_msg, pipeline_state, updates_text)

    events_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["events"],
        events_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "events", "status": "complete"})

    events_data = events_result.parsed_json
    events_route = events_data.get("route", "mechanics")

    # Increment turn counter only for in-character turns — OOC shouldn't age TTLs
    if events_route != "output":
        pipeline_state["turn_counter"] += 1
    current_turn = pipeline_state["turn_counter"]

    # Extract and apply state from Events output
    if events_data.get("pacing"):
        pipeline_state["pacing"] = events_data["pacing"]
    pipeline_state["callback_ledger"] = apply_callback_ops(
        pipeline_state["callback_ledger"],
        events_data.get("callback_ops"),
        current_turn
    )
    pipeline_state["npc_memories"] = apply_npc_memory_ops(
        pipeline_state["npc_memories"],
        events_data.get("npc_memory_ops"),
        current_turn
    )
    if events_data.get("scene_state"):
        pipeline_state["scene_state"] = apply_scene_state(events_data["scene_state"])
    new_pipeline_state = pipeline_state

    # Collect stage results for aggregation
    stage_results = [events_result]
    reasoning_summaries = []
    if events_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Events] {events_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Events → Output ----
    if events_route == "output":
        final_content = events_data.get("content", "")
        # Send content as a single chunk (OOC responses are short)
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=None,
            stages_run=["events"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="standard",
            injected_state=injected_state_snapshot,
            stage_usage=_build_stage_usage(stage_results, provider)
        ))
        return

    # ---- STAGE 2: Mechanics ----
    yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})

    mechanics_system = build_agent_system_prompt(MECHANICS_CONTRACT, agent_instructions["mechanics"], agent_files["mechanics"])
    mechanics_messages = build_mechanics_messages(mechanics_system, events_data, updates_text)

    mechanics_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["mechanics"],
        mechanics_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

    mechanics_data = mechanics_result.parsed_json
    mechanics_route = mechanics_data.get("route", "narration")
    new_pipeline_state["character_states"] = apply_character_states(
        new_pipeline_state["character_states"],
        mechanics_data.get("character_states") or {},
        current_turn
    )
    stage_results.append(mechanics_result)
    if mechanics_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Mechanics] {mechanics_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Mechanics → Output ----
    if mechanics_route == "output":
        final_content = mechanics_data.get("content", "")
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=mechanics_result.content,
            stages_run=["events", "mechanics"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="standard",
            injected_state=injected_state_snapshot,
            stage_usage=_build_stage_usage(stage_results, provider)
        ))
        return

    # ---- STAGE 3: Narration (streaming) ----
    yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})

    narration_system = build_agent_system_prompt(NARRATION_CONTRACT, agent_instructions["narration"], agent_files["narration"])
    recent_pairs = get_context_pairs(branch_path, NARRATION_THRESHOLD_PAIRS, NARRATION_TARGET_PAIRS)
    narration_messages = build_narration_messages(narration_system, recent_pairs, mechanics_data)

    narration_params = provider.build_pipeline_request(
        messages=narration_messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name="narration",
        reasoning_effort=STAGE_CONFIGS["narration"].reasoning_effort,
        service_tier=STAGE_CONFIGS["narration"].service_tier,
        json_mode=False
    )

    # Stream Narration to the user
    narration_content = ""
    narration_usage = None
    first_content = True

    for stream_event in provider.send_request_stream(client, narration_params):
        if stream_event.event_type == 'content_delta':
            if first_content:
                yield ("pipeline_stage", {"stage": "narration", "status": "streaming"})
                first_content = False
            narration_content += stream_event.content
            yield ("content", {"delta": stream_event.content})

        elif stream_event.event_type == 'done':
            narration_usage = stream_event.usage
            narration_content = narration_content or narration_usage.get('content') or ''

    yield ("pipeline_stage", {"stage": "narration", "status": "complete"})

    # Build narration stage result
    narration_stage_result = PipelineStageResult(
        stage="narration",
        content=narration_content,
        parsed_json=None,
        usage=narration_usage or {},
        service_tier="standard"
    )
    stage_results.append(narration_stage_result)
    if narration_usage and narration_usage.get('reasoning'):
        reasoning_summaries.append(f"[Narration] {narration_usage['reasoning']}")

    aggregate = _aggregate_usage(stage_results, provider)
    yield ("pipeline_done", PipelineResult(
        final_content=narration_content,
        events_json=events_result.content,
        mechanics_json=mechanics_result.content,
        stages_run=["events", "mechanics", "narration"],
        aggregate_usage=aggregate["usage"],
        aggregate_cost=aggregate["cost"],
        pipeline_state=new_pipeline_state,
        reasoning_summaries=reasoning_summaries,
        service_tier_label="flex+standard",
        injected_state=injected_state_snapshot,
        stage_usage=_build_stage_usage(stage_results, provider)
    ))


def _build_stage_usage(stage_results: list[PipelineStageResult], provider: OpenAIProvider) -> dict:
    """Build per-stage usage dict for debug storage."""
    result = {}
    for sr in stage_results:
        u = sr.usage
        parsed = ParsedResponse(
            content="", reasoning=None,
            input_tokens=u.get('input_tokens', 0),
            cache_read_tokens=u.get('cache_read_tokens', 0),
            cache_creation_tokens=u.get('cache_creation_tokens', 0),
            output_tokens=u.get('output_tokens', 0),
            reasoning_tokens=u.get('reasoning_tokens', 0)
        )
        result[sr.stage] = {
            "input_tokens": u.get('input_tokens', 0),
            "cache_read_tokens": u.get('cache_read_tokens', 0),
            "cache_creation_tokens": u.get('cache_creation_tokens', 0),
            "output_tokens": u.get('output_tokens', 0),
            "reasoning_tokens": u.get('reasoning_tokens', 0),
            "cost": provider.calculate_cost_with_tier(parsed, sr.service_tier),
            "service_tier": sr.service_tier
        }
    return result


def _aggregate_usage(stage_results: list[PipelineStageResult], provider: OpenAIProvider) -> dict:
    """
    Aggregate token usage and cost across all pipeline stages.

    Each stage's cost is calculated with its own tier-specific pricing.
    """
    total_input = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_output = 0
    total_reasoning = 0
    total_cost = 0.0

    for result in stage_results:
        usage = result.usage
        input_tokens = usage.get('input_tokens', 0)
        cache_read = usage.get('cache_read_tokens', 0)
        cache_creation = usage.get('cache_creation_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        reasoning_tokens = usage.get('reasoning_tokens', 0)

        total_input += input_tokens
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_output += output_tokens
        total_reasoning += reasoning_tokens

        # Calculate per-stage cost with tier-specific pricing
        parsed = ParsedResponse(
            content="",
            reasoning=None,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens
        )
        stage_cost = provider.calculate_cost_with_tier(parsed, result.service_tier)
        total_cost += stage_cost

    return {
        "usage": {
            "input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_creation_tokens": total_cache_creation,
            "output_tokens": total_output,
            "reasoning_tokens": total_reasoning,
        },
        "cost": total_cost
    }


# ============================================================
# Debug Transcript Generation
# ============================================================

def _parse_reasoning_by_stage(reasoning: str) -> dict:
    """Split a joined '[Events] ...\n[Mechanics] ...' reasoning string into per-stage dict."""
    result = {"events": "", "mechanics": "", "narration": ""}
    if not reasoning:
        return result
    current_stage = None
    current_lines = []
    for line in reasoning.split("\n"):
        stripped = line.strip()
        matched = False
        for stage in ("Events", "Mechanics", "Narration"):
            prefix = f"[{stage}]"
            if stripped.startswith(prefix):
                # Save previous stage
                if current_stage:
                    result[current_stage] = "\n".join(current_lines).strip()
                current_stage = stage.lower()
                current_lines = [stripped[len(prefix):].strip()]
                matched = True
                break
        if not matched and current_stage:
            current_lines.append(line)
    if current_stage:
        result[current_stage] = "\n".join(current_lines).strip()
    return result


def _pretty_json(raw: str) -> str:
    """Pretty-print a JSON string with 2-space indent. Falls back to raw on error."""
    if not raw:
        return "(none)"
    try:
        return json.dumps(json.loads(raw), indent=2)
    except (json.JSONDecodeError, TypeError):
        return f"[PARSE ERROR] {raw}"


def _compute_state_delta(prev: dict, curr: dict) -> str:
    """Compute a human-readable delta between two pipeline states."""
    parts = []

    # Turn counter
    prev_turn = prev.get("turn_counter", 0)
    curr_turn = curr.get("turn_counter", 0)
    if curr_turn != prev_turn:
        parts.append(f"turn_counter: {prev_turn} → {curr_turn}")

    # Pacing — show if changed
    prev_pacing = prev.get("pacing", {})
    curr_pacing = curr.get("pacing", {})
    if curr_pacing != prev_pacing:
        changed = {k: v for k, v in curr_pacing.items() if prev_pacing.get(k) != v}
        if changed:
            parts.append(f"pacing: {json.dumps(changed)}")

    # Callback ledger
    prev_ledger = prev.get("callback_ledger", {})
    curr_ledger = curr.get("callback_ledger", {})
    prev_open_ids = {cb["id"] for cb in prev_ledger.get("open", []) if "id" in cb}
    curr_open_ids = {cb["id"] for cb in curr_ledger.get("open", []) if "id" in cb}
    added_ids = curr_open_ids - prev_open_ids
    resolved_ids = prev_open_ids - curr_open_ids
    if added_ids:
        added_cbs = [cb for cb in curr_ledger.get("open", []) if cb.get("id") in added_ids]
        for cb in added_cbs:
            parts.append(f"callback +#{cb.get('id')}: \"{cb.get('original_text', '')[:80]}\"")
    if resolved_ids:
        resolved_cbs = [cb for cb in curr_ledger.get("recently_resolved", []) if cb.get("id") in resolved_ids]
        for cb in resolved_cbs:
            parts.append(f"callback resolved #{cb.get('id')}: \"{cb.get('resolution_text', '')[:80]}\"")

    # NPC memories
    prev_mems = prev.get("npc_memories", {})
    curr_mems = curr.get("npc_memories", {})
    all_npcs = set(list(prev_mems.keys()) + list(curr_mems.keys()))
    for npc in sorted(all_npcs):
        prev_list = prev_mems.get(npc, [])
        curr_list = curr_mems.get(npc, [])
        prev_texts = {m.get("text") for m in prev_list}
        curr_texts = {m.get("text") for m in curr_list}
        for text in curr_texts - prev_texts:
            mem = next((m for m in curr_list if m.get("text") == text), {})
            parts.append(f"memory +{npc}: [{mem.get('impact', '?')}★] \"{text[:60]}\"")
        for text in prev_texts - curr_texts:
            parts.append(f"memory -{npc}: \"{text[:60]}\"")
    # NPC removed entirely
    for npc in sorted(set(prev_mems.keys()) - set(curr_mems.keys())):
        if npc not in all_npcs:  # already handled above
            parts.append(f"memory -{npc}: (all removed)")

    # Character states (entries are {"state": "...", "last_updated": N} or flat strings)
    prev_cs = prev.get("character_states", {})
    curr_cs = curr.get("character_states", {})
    def _cs_state(entry):
        return entry.get("state", "") if isinstance(entry, dict) else entry
    all_chars = set(list(prev_cs.keys()) + list(curr_cs.keys()))
    for char in sorted(all_chars):
        prev_entry = prev_cs.get(char)
        curr_entry = curr_cs.get(char)
        prev_state = _cs_state(prev_entry) if prev_entry else None
        curr_state = _cs_state(curr_entry) if curr_entry else None
        if curr_state != prev_state:
            if prev_state is None:
                parts.append(f"character_state +{char}: {curr_state}")
            elif curr_state is None:
                parts.append(f"character_state -{char}")
            else:
                parts.append(f"character_state {char}: {prev_state} → {curr_state}")

    # Scene state
    prev_scene = prev.get("scene_state", {})
    curr_scene = curr.get("scene_state", {})
    if curr_scene != prev_scene:
        scene_changes = []
        for key in ["location", "npcs_present", "active_tensions", "scene_trigger", "atmosphere", "details", "pending_actions"]:
            pv = prev_scene.get(key)
            cv = curr_scene.get(key)
            if pv != cv:
                if isinstance(cv, list):
                    scene_changes.append(f"  {key}: {', '.join(str(v) for v in cv) if cv else '(none)'}")
                else:
                    scene_changes.append(f"  {key}: {cv}")
        if scene_changes:
            parts.append("scene_state:\n" + "\n".join(scene_changes))

    return "\n".join(parts)


def _format_stage_usage(stage_name: str, usage: dict) -> str:
    """Format a single stage's usage as a compact one-liner."""
    inp = usage.get("input_tokens", 0)
    cache = usage.get("cache_read_tokens", 0)
    out = usage.get("output_tokens", 0)
    reasoning = usage.get("reasoning_tokens", 0)
    cost = usage.get("cost", 0)
    tier = usage.get("service_tier", "?")
    return f"{stage_name}: Input: {inp:,}  Cache: {cache:,}  Output: {out:,}  Reasoning: {reasoning:,}  Cost: ${cost:.4f}  Tier: {tier}"


def generate_debug_transcript(chat_data: dict, chat_path: str, chat_name: str) -> None:
    """
    Generate a debug transcript file for a pipeline chat.

    Walks the active branch (current_leaf_id → root) and expands pipeline
    assistant messages to show per-stage JSON and reasoning.
    """
    from datetime import datetime, timezone

    debug_path = chat_path.replace(".json", "_debug.txt")

    messages = chat_data.get("messages", [])
    leaf_id = chat_data.get("current_leaf_id")
    if not messages or not leaf_id:
        return

    # Build index and trace active branch (root → leaf)
    index = {m["id"]: m for m in messages if m.get("id")}
    path = []
    current = leaf_id
    while current:
        if current not in index:
            break
        path.append(index[current])
        current = index[current].get("parent_id")
    path.reverse()

    lines = []
    lines.append("=" * 80)
    lines.append(f"PIPELINE DEBUG TRANSCRIPT: {chat_name}")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 80)
    lines.append("")

    prev_state = None  # Track previous state for delta computation
    latest_state_raw = None  # Track the latest full state for the end block

    for msg in path:
        role = msg.get("role", "")
        if role == "system":
            continue

        timestamp = msg.get("timestamp", "")
        content = msg.get("content", "")

        if role == "user":
            lines.append("+" * 80)
            lines.append("")
            lines.append(f"[USER] {timestamp}")
            lines.append(content)
            lines.append("")

        elif role == "assistant":
            events_raw = msg.get("events_stage")
            mechanics_raw = msg.get("mechanics_stage")

            if events_raw or mechanics_raw:
                # Pipeline message — expand stages
                lines.append(f"[ASSISTANT] {timestamp}")

                # Show pipeline state delta (what changed since last turn)
                injected_state_raw = msg.get("pipeline_state_injected")
                if injected_state_raw:
                    latest_state_raw = injected_state_raw
                    try:
                        current_state = json.loads(injected_state_raw)
                    except (json.JSONDecodeError, TypeError):
                        current_state = None

                    if current_state is not None:
                        if prev_state is None:
                            lines.append("--- PIPELINE STATE (initial) ---")
                            lines.append(json.dumps(current_state, indent=2))
                        else:
                            delta = _compute_state_delta(prev_state, current_state)
                            if delta:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append(delta)
                            else:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append("(no changes)")
                        lines.append("")
                        prev_state = current_state

                reasoning_parts = _parse_reasoning_by_stage(msg.get("reasoning", ""))
                stage_usage = msg.get("pipeline_stage_usage", {})

                if events_raw:
                    lines.append("--- EVENTS STAGE ---")
                    lines.append(_pretty_json(events_raw))
                    lines.append("")

                    # Show extracted state ops from Events output
                    try:
                        events_parsed = json.loads(events_raw)
                        ops_parts = []
                        cb_ops = events_parsed.get("callback_ops")
                        if cb_ops:
                            ops_parts.append(f"callback_ops: {json.dumps(cb_ops, indent=2)}")
                        mem_ops = events_parsed.get("npc_memory_ops")
                        if mem_ops:
                            ops_parts.append(f"npc_memory_ops: {json.dumps(mem_ops, indent=2)}")
                        scene = events_parsed.get("scene_state")
                        if scene:
                            ops_parts.append(f"scene_state: {json.dumps(scene, indent=2)}")
                        if ops_parts:
                            lines.append("--- STATE OPS EXTRACTED ---")
                            lines.append("\n".join(ops_parts))
                            lines.append("")
                    except (json.JSONDecodeError, TypeError):
                        pass

                    lines.append("--- EVENTS REASONING ---")
                    lines.append(reasoning_parts["events"] or "(none)")
                    lines.append("")
                    if "events" in stage_usage:
                        lines.append(_format_stage_usage("Events", stage_usage["events"]))
                        lines.append("")

                if mechanics_raw:
                    lines.append("--- MECHANICS STAGE ---")
                    lines.append(_pretty_json(mechanics_raw))
                    lines.append("")
                    lines.append("--- MECHANICS REASONING ---")
                    lines.append(reasoning_parts["mechanics"] or "(none)")
                    lines.append("")
                    if "mechanics" in stage_usage:
                        lines.append(_format_stage_usage("Mechanics", stage_usage["mechanics"]))
                        lines.append("")

                narration_reasoning = reasoning_parts["narration"]
                if narration_reasoning:
                    lines.append("--- NARRATION REASONING ---")
                    lines.append(narration_reasoning)
                    lines.append("")
                if "narration" in stage_usage:
                    lines.append(_format_stage_usage("Narration", stage_usage["narration"]))
                    lines.append("")

                # Total usage across all stages for this turn
                if stage_usage:
                    total_cost = sum(s.get("cost", 0) for s in stage_usage.values())
                    total_input = sum(s.get("input_tokens", 0) for s in stage_usage.values())
                    total_output = sum(s.get("output_tokens", 0) for s in stage_usage.values())
                    total_reasoning = sum(s.get("reasoning_tokens", 0) for s in stage_usage.values())
                    lines.append(f"--- TURN TOTAL: Input: {total_input:,}  Output: {total_output:,}  Reasoning: {total_reasoning:,}  Cost: ${total_cost:.4f} ---")
                    lines.append("")

                lines.append("--- FINAL OUTPUT ---")
                lines.append(content)
                lines.append("")
            elif msg.get("state_block_raw") is not None or msg.get("pipeline_state_injected"):
                # Single-agent stateful message
                lines.append(f"[ASSISTANT] {timestamp}")

                # Show pipeline state delta (what changed since last turn)
                injected_state_raw = msg.get("pipeline_state_injected")
                if injected_state_raw:
                    latest_state_raw = injected_state_raw
                    try:
                        current_state = json.loads(injected_state_raw)
                    except (json.JSONDecodeError, TypeError):
                        current_state = None

                    if current_state is not None:
                        if prev_state is None:
                            lines.append("--- PIPELINE STATE (initial) ---")
                            lines.append(json.dumps(current_state, indent=2))
                        else:
                            delta = _compute_state_delta(prev_state, current_state)
                            if delta:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append(delta)
                            else:
                                lines.append("--- PIPELINE STATE DELTA ---")
                                lines.append("(no changes)")
                        lines.append("")
                        prev_state = current_state

                # Show raw state block from model output
                state_block_raw = msg.get("state_block_raw")
                if state_block_raw:
                    lines.append("--- RAW STATE BLOCK ---")
                    lines.append(state_block_raw.strip())
                    lines.append("")

                # Show parsed ops
                state_ops = msg.get("state_ops_parsed")
                if state_ops:
                    ops_parts = []
                    if state_ops.get("pacing"):
                        ops_parts.append(f"pacing: {json.dumps(state_ops['pacing'], indent=2)}")
                    if state_ops.get("callback_ops"):
                        ops_parts.append(f"callback_ops: {json.dumps(state_ops['callback_ops'], indent=2)}")
                    if state_ops.get("npc_memory_ops"):
                        ops_parts.append(f"npc_memory_ops: {json.dumps(state_ops['npc_memory_ops'], indent=2)}")
                    if state_ops.get("scene_state"):
                        ops_parts.append(f"scene_state: {json.dumps(state_ops['scene_state'], indent=2)}")
                    if state_ops.get("character_states"):
                        ops_parts.append(f"character_states: {json.dumps(state_ops['character_states'], indent=2)}")
                    if ops_parts:
                        lines.append("--- STATE OPS PARSED ---")
                        lines.append("\n".join(ops_parts))
                        lines.append("")

                lines.append("--- OUTPUT ---")
                lines.append(content)
                lines.append("")
            else:
                # Non-pipeline, non-stateful assistant message
                lines.append(f"[ASSISTANT] {timestamp}")
                lines.append(content)
                lines.append("")

    # Append full final state at the end of the file for easy reference
    final_state = chat_data.get("pipeline_state")
    if final_state:
        lines.append("=" * 80)
        lines.append("FULL PIPELINE STATE (after last turn)")
        lines.append("=" * 80)
        lines.append(json.dumps(final_state, indent=2))
        lines.append("")

    with open(debug_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============================================================
# Single-Agent Stateful Persistence (Claude project chats)
# ============================================================

STATE_BLOCK_START = "\n[STATE UPDATES]\n"
STATE_BLOCK_END = "\n[/STATE UPDATES]"

# Threshold/target for single-agent context trimming (same as pipeline)
SINGLE_AGENT_THRESHOLD_PAIRS = 40
SINGLE_AGENT_TARGET_PAIRS = 20


class StateInterceptor:
    """Buffers streaming content and intercepts the [STATE UPDATES] block."""

    def __init__(self):
        self.buffer = ""
        self.state_started = False
        self.state_buffer = ""
        self.narrative_complete = ""

    def feed(self, delta: str) -> str:
        """Feed a content delta. Returns text safe to yield to user."""
        if self.state_started:
            self.state_buffer += delta
            return ""

        self.buffer += delta

        if STATE_BLOCK_START in self.buffer:
            parts = self.buffer.split(STATE_BLOCK_START, 1)
            self.state_started = True
            safe = parts[0]
            self.state_buffer = parts[1]
            self.narrative_complete += safe
            self.buffer = ""
            return safe

        # Hold back potential partial delimiter
        safe_len = max(0, len(self.buffer) - len(STATE_BLOCK_START))
        safe = self.buffer[:safe_len]
        self.buffer = self.buffer[safe_len:]
        self.narrative_complete += safe
        return safe

    def finalize(self) -> tuple:
        """Call after stream ends. Returns (remaining_narrative, state_block_text_or_None)."""
        if self.state_started:
            state_text = self.state_buffer
            if STATE_BLOCK_END in state_text:
                state_text = state_text[:state_text.index(STATE_BLOCK_END)]
            return self.buffer, state_text
        else:
            return self.buffer, None


def _parse_pacing_section(lines: list) -> dict:
    """Parse PACING section lines into a dict."""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            # Try to parse responses as int
            if key == "responses":
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    pass
            result[key] = value
    return result


def _parse_callbacks_section(lines: list) -> list:
    """Parse CALLBACKS section lines into ops list."""
    import re
    ops = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("+"):
            # Add: + "description" | source: NPC
            text_match = re.search(r'"([^"]*)"', line)
            text = text_match.group(1) if text_match else line[1:].strip()
            source = None
            source_match = re.search(r'\|\s*source:\s*(.+)', line)
            if source_match:
                source = source_match.group(1).strip()
                if source.lower() == "null":
                    source = None
            ops.append({
                "action": "add",
                "original_text": text[:800],
                "source_npc": source
            })
        elif line.upper().startswith("RESOLVE"):
            # RESOLVE #N: "text"
            id_match = re.search(r'#(\d+)', line)
            text_match = re.search(r'"([^"]*)"', line)
            if id_match:
                ops.append({
                    "action": "resolve",
                    "id": int(id_match.group(1)),
                    "resolution_text": text_match.group(1) if text_match else ""
                })
    return ops


def _parse_memories_section(lines: list) -> list:
    """Parse MEMORIES section lines into ops list."""
    import re
    ops = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("+"):
            # + NPC [impact] "text" | "quote" | date
            rest = line[1:].strip()
            # Extract NPC name (everything before first [)
            bracket_idx = rest.find("[")
            if bracket_idx == -1:
                continue
            npc = rest[:bracket_idx].strip()
            # Extract impact
            impact_match = re.search(r'\[(\d+)\]', rest)
            impact = int(impact_match.group(1)) if impact_match else 1
            # Extract quoted strings
            quotes = re.findall(r'"([^"]*)"', rest)
            text = quotes[0] if len(quotes) > 0 else ""
            quote = quotes[1] if len(quotes) > 1 else None
            # Date is after last | (at least 2 pipes for text|quote|date, or 1 pipe for text|date)
            parts = rest.split("|")
            date = None
            if len(parts) >= 2:
                candidate = parts[-1].strip()
                # If the last segment looks like a quoted string, it's a quote not a date
                if candidate and not candidate.startswith('"'):
                    date = candidate
            ops.append({
                "action": "add",
                "npc": npc,
                "text": text[:640],
                "quote": quote[:120] if quote else None,
                "date": date,
                "impact": impact
            })
        elif line.startswith("-"):
            # - NPC [index]
            rest = line[1:].strip()
            bracket_match = re.search(r'\[(\d+)\]', rest)
            if bracket_match:
                idx = int(bracket_match.group(1))
                npc = rest[:rest.find("[")].strip()
                ops.append({
                    "action": "drop",
                    "npc": npc,
                    "index": idx
                })
    return ops


def _parse_scene_section(lines: list) -> dict:
    """Parse SCENE section lines into a scene_state dict."""
    result = {}
    list_keys = {"npcs_present", "tensions", "details", "pending"}
    # Map short keys to full scene_state keys
    key_map = {
        "npcs_present": "npcs_present",
        "tensions": "active_tensions",
        "trigger": "scene_trigger",
        "pending": "pending_actions",
        "location": "location",
        "atmosphere": "atmosphere",
        "details": "details",
    }
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            mapped_key = key_map.get(key, key)
            if key in list_keys:
                result[mapped_key] = [v.strip() for v in value.split(",") if v.strip()] if value and value != "(none)" else []
            else:
                result[mapped_key] = value
    return result


def _parse_characters_section(lines: list) -> dict:
    """Parse CHARACTERS section lines into a flat name→state dict."""
    result = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, state = line.partition(":")
            name = name.strip()
            state = state.strip()
            if name and state:
                result[name] = state
    return result


def parse_state_updates_block(text: str, current_turn: int) -> dict:
    """
    Parse the [STATE UPDATES] block text into ops compatible with existing apply_* functions.

    Returns dict with keys: pacing, callback_ops, npc_memory_ops, scene_state, character_states.
    Each value is None if that section was not present.
    """
    result = {
        "pacing": None,
        "callback_ops": None,
        "npc_memory_ops": None,
        "scene_state": None,
        "character_states": None,
    }

    # Split text into sections by header lines
    current_section = None
    section_lines = {}

    # Map alternate section header names to canonical names
    _section_aliases = {
        "PACING": "PACING", "CALLBACKS": "CALLBACKS", "MEMORIES": "MEMORIES",
        "SCENE": "SCENE", "CHARACTERS": "CHARACTERS",
        "SCENE STATE": "SCENE", "CHARACTER STATES": "CHARACTERS",
        "NPC MEMORIES": "MEMORIES",
    }

    for line in text.split("\n"):
        stripped = line.strip().upper().rstrip(":")
        if stripped in _section_aliases:
            current_section = _section_aliases[stripped]
            section_lines[current_section] = []
        elif current_section is not None:
            section_lines[current_section].append(line)

    if "PACING" in section_lines:
        result["pacing"] = _parse_pacing_section(section_lines["PACING"])
    if "CALLBACKS" in section_lines:
        result["callback_ops"] = _parse_callbacks_section(section_lines["CALLBACKS"])
    if "MEMORIES" in section_lines:
        result["npc_memory_ops"] = _parse_memories_section(section_lines["MEMORIES"])
    if "SCENE" in section_lines:
        result["scene_state"] = _parse_scene_section(section_lines["SCENE"])
    if "CHARACTERS" in section_lines:
        result["character_states"] = _parse_characters_section(section_lines["CHARACTERS"])

    return result


def apply_single_agent_state_updates(pipeline_state: dict, parsed: dict, current_turn: int) -> dict:
    """Apply parsed state updates to pipeline_state using existing apply_* functions."""
    if parsed["pacing"]:
        pipeline_state["pacing"] = parsed["pacing"]
    if parsed["callback_ops"]:
        pipeline_state["callback_ledger"] = apply_callback_ops(
            pipeline_state["callback_ledger"],
            parsed["callback_ops"],
            current_turn
        )
    if parsed["npc_memory_ops"]:
        pipeline_state["npc_memories"] = apply_npc_memory_ops(
            pipeline_state["npc_memories"],
            parsed["npc_memory_ops"],
            current_turn
        )
    if parsed["scene_state"]:
        pipeline_state["scene_state"] = apply_scene_state(parsed["scene_state"])
    pipeline_state["character_states"] = apply_character_states(
        pipeline_state["character_states"],
        parsed["character_states"] or {},
        current_turn
    )
    return pipeline_state


def build_single_agent_injections(pipeline_state: dict, updates_text: str = "") -> str:
    """Build the full injection string for a single-agent stateful user message."""
    injections = []

    # 1. Pacing state
    pacing = pipeline_state.get("pacing", {})
    if pacing:
        injections.append(f"[PIPELINE STATE]\n{json.dumps(pacing, indent=2)}\n[/PIPELINE STATE]")

    # 2. Callback ledger
    cb = build_callback_injection(pipeline_state.get("callback_ledger", {}))
    if cb:
        injections.append(cb)

    # 3. NPC memories (scene-scoped)
    mem = build_npc_memories_injection(
        pipeline_state.get("npc_memories", {}),
        pipeline_state.get("scene_state", {})
    )
    if mem:
        injections.append(mem)

    # 4. Scene state
    scene = build_scene_state_injection(pipeline_state.get("scene_state", {}))
    if scene:
        injections.append(scene)

    # 5. Character states
    cs = build_character_states_injection(pipeline_state.get("character_states", {}))
    if cs:
        injections.append(cs)

    # 6. Context updates
    if updates_text.strip():
        injections.append(f"[CONTEXT UPDATES]\n{updates_text}\n[/CONTEXT UPDATES]")

    return "\n\n".join(injections) if injections else ""
