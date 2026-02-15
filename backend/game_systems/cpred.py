"""
Cyberpunk RED game system — contracts and structured state functions.

CPRED tracks HP (with seriously wounded threshold), Humanity (cyberpsychosis risk),
Luck (session-spendable), Armor (head/body with ablation), eurobucks, critical injuries
(affect Death Save DV), and cyberware effects.
"""

import copy
import logging

logger = logging.getLogger(__name__)

# ============================================================
# Structured Game State
# ============================================================
#
# Stored in pipeline_state["game_state"]:
# {
#     "edgerunners": {
#         "V": {
#             "hp": {"current": 35, "max": 40, "seriously_wounded": false},
#             "humanity": {"current": 48, "max": 70},
#             "luck": {"current": 6, "max": 7},
#             "armor": {"head": 11, "body": 11},
#             "eurobucks": 2350,
#             "critical_injuries": [
#                 {"name": "Broken Arm", "effect": "-2 to actions with that arm", "dv_mod": 1}
#             ],
#             "cyberware_effects": ["Cybereye (Low-Light)", "Neural Link"]
#         }
#     }
# }


def init_game_state():
    """Return empty edgerunners dict — populated via 'set' ops on first turn."""
    return {"edgerunners": {}}


def _default_edgerunner():
    """Default stub for a new edgerunner."""
    return {
        "hp": {"current": 0, "max": 40, "seriously_wounded": False},
        "humanity": {"current": 0, "max": 0},
        "luck": {"current": 0, "max": 0},
        "armor": {"head": 0, "body": 0},
        "eurobucks": 0,
        "critical_injuries": [],
        "cyberware_effects": []
    }


def _update_seriously_wounded(er):
    """Auto-derive seriously_wounded flag from HP."""
    hp = er.get("hp", {})
    hp["seriously_wounded"] = hp.get("current", 0) <= hp.get("max", 40) // 2


def apply_game_state(game_state, agent_json, turn):
    """
    Apply CPRED edgerunner_ops from Events agent (or single-agent report_state) output.

    Ops format (array in agent_json["edgerunner_ops"]):
      {"edgerunner": "V", "op": "hp", "change": -8, "reason": "Shotgun hit after armor"}
      {"edgerunner": "V", "op": "humanity", "change": -4, "reason": "Cyberarm installed (2d6=4)"}
      {"edgerunner": "V", "op": "therapy", "change": 2, "reason": "Therapy session (2d6=4, half=2)"}
      {"edgerunner": "V", "op": "luck", "change": -2, "reason": "Added to Athletics check"}
      {"edgerunner": "V", "op": "luck_reset", "reason": "New session"}
      {"edgerunner": "V", "op": "armor", "location": "body", "change": -1, "reason": "Ablation from hit"}
      {"edgerunner": "V", "op": "armor_repair", "location": "body", "value": 11, "reason": "Repaired by tech"}
      {"edgerunner": "V", "op": "eurobucks", "change": -500, "reason": "Bought ammo"}
      {"edgerunner": "V", "op": "critical_injury", "action": "add", "name": "Broken Ribs", "effect": "-2 to movement actions", "dv_mod": 1}
      {"edgerunner": "V", "op": "critical_injury", "action": "remove", "name": "Broken Ribs", "reason": "Surgery"}
      {"edgerunner": "V", "op": "cyberware", "action": "add", "value": "Cybereye (Low-Light)"}
      {"edgerunner": "V", "op": "cyberware", "action": "remove", "value": "Cybereye (Low-Light)"}
      {"edgerunner": "V", "op": "set", "fields": {"hp": {"current": 35, "max": 40}, ...}}
    """
    ops = agent_json.get("edgerunner_ops")
    if not ops:
        return game_state

    edgerunners = game_state.setdefault("edgerunners", {})

    for op_data in ops:
        er_name = op_data.get("edgerunner")
        op = op_data.get("op")
        if not er_name or not op:
            continue

        # Auto-create edgerunner stub if not yet tracked
        if er_name not in edgerunners:
            edgerunners[er_name] = _default_edgerunner()
        er = edgerunners[er_name]

        try:
            if op == "set":
                fields = copy.deepcopy(op_data.get("fields", {}))
                for key, val in fields.items():
                    if key in er:
                        er[key] = val
                _update_seriously_wounded(er)

            elif op == "hp":
                change = int(op_data.get("change", 0))
                er["hp"]["current"] = max(0, min(er["hp"]["max"], er["hp"]["current"] + change))
                _update_seriously_wounded(er)

            elif op == "humanity":
                change = int(op_data.get("change", 0))
                if change < 0:  # Humanity loss from cyberware
                    er["humanity"]["current"] = max(0, er["humanity"]["current"] + change)

            elif op == "therapy":
                change = int(op_data.get("change", 0))
                if change > 0:  # Partial recovery via therapy
                    er["humanity"]["current"] = min(
                        er["humanity"]["max"],
                        er["humanity"]["current"] + change
                    )

            elif op == "luck":
                change = int(op_data.get("change", 0))
                er["luck"]["current"] = max(0, min(
                    er["luck"]["max"],
                    er["luck"]["current"] + change
                ))

            elif op == "luck_reset":
                er["luck"]["current"] = er["luck"]["max"]

            elif op == "armor":
                location = op_data.get("location", "body")
                change = int(op_data.get("change", 0))
                if location in er["armor"]:
                    er["armor"][location] = max(0, er["armor"][location] + change)

            elif op == "armor_repair":
                location = op_data.get("location", "body")
                value = int(op_data.get("value", 0))
                if location in er["armor"]:
                    er["armor"][location] = value

            elif op == "eurobucks":
                change = int(op_data.get("change", 0))
                er["eurobucks"] = max(0, er["eurobucks"] + change)

            elif op == "critical_injury":
                action = op_data.get("action", "add")
                name = op_data.get("name")
                if action == "add" and name:
                    er["critical_injuries"].append({
                        "name": name,
                        "effect": op_data.get("effect", ""),
                        "dv_mod": int(op_data.get("dv_mod", 0))
                    })
                elif action == "remove" and name:
                    er["critical_injuries"] = [
                        ci for ci in er["critical_injuries"] if ci["name"] != name
                    ]

            elif op == "cyberware":
                action = op_data.get("action", "add")
                value = op_data.get("value")
                if value:
                    if action == "add" and value not in er["cyberware_effects"]:
                        er["cyberware_effects"].append(value)
                    elif action == "remove" and value in er["cyberware_effects"]:
                        er["cyberware_effects"].remove(value)

        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"CPRED apply_game_state: error processing op {op_data}: {e}")
            continue

    return game_state


def build_game_injection(game_state):
    """Build [EDGERUNNER STATE] injection block from structured state."""
    edgerunners = game_state.get("edgerunners", {})
    if not edgerunners:
        return ""

    lines = ["[EDGERUNNER STATE]"]
    for name, er in sorted(edgerunners.items()):
        hp = er.get("hp", {})
        humanity = er.get("humanity", {})
        luck = er.get("luck", {})
        armor = er.get("armor", {})
        eb = er.get("eurobucks", 0)
        injuries = er.get("critical_injuries", [])
        cyberware = er.get("cyberware_effects", [])

        sw_flag = " [SERIOUSLY WOUNDED: -2 all actions]" if hp.get("seriously_wounded") else ""

        lines.append(f"{name}:")
        lines.append(f"  HP: {hp.get('current', 0)}/{hp.get('max', 40)}{sw_flag}")
        lines.append(f"  Humanity: {humanity.get('current', 0)}/{humanity.get('max', 0)}")
        lines.append(f"  Luck: {luck.get('current', 0)}/{luck.get('max', 0)}")
        lines.append(f"  Armor: Head SP {armor.get('head', 0)} | Body SP {armor.get('body', 0)}")
        lines.append(f"  Eurobucks: {eb:,}")

        if injuries:
            dv_total = sum(ci.get("dv_mod", 0) for ci in injuries)
            injury_strs = [f"{ci['name']} ({ci['effect']}, Death Save +{ci['dv_mod']})" for ci in injuries]
            lines.append(f"  Critical injuries (Death Save DV +{dv_total}): {'; '.join(injury_strs)}")

        if cyberware:
            lines.append(f"  Cyberware: {', '.join(cyberware)}")

    lines.append("[/EDGERUNNER STATE]")
    return "\n".join(lines)


# ============================================================
# Pipeline Contracts
# ============================================================

EVENTS_CONTRACT = """You are the EVENTS AGENT in a multi-agent TTRPG GM pipeline for Cyberpunk RED. You are the first stage.

YOUR ROLE: Analyze the conversation history and determine what is happening this turn. Identify narrative beats, triggered callbacks, emotional context, and current character states. Maintain the persistent callback ledger, NPC memories, scene state, and edgerunner mechanical state via ops.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Mechanics (default for in-character gameplay):
{
  "route": "mechanics",
  "pacing": {
    "episode": "<current gig/scenario name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "time_passed": "<how much in-world time this turn covers>",
  "beats": ["<beat 1>", "<beat 2>", ...],
  "player_action": "<what the player is attempting>",
  "callbacks": [
    {"callback": "<triggered callback description>", "source": "<NPC name or null>"}
  ],
  "emotional_context": "<emotional state, tension level, Night City atmosphere>",
  "character_states": {
    "<name>": "<current conditions, equipment, weapons — NOT HP/Humanity/Luck/Armor/EB>"
  },
  "edgerunner_ops": [
    {"edgerunner": "<name>", "op": "hp|humanity|therapy|luck|luck_reset|armor|armor_repair|eurobucks|critical_injury|cyberware|set", ...}
  ],
  "arc_label": "<string or null>",
  "current_player": "<name of the edgerunner whose turn this is>",
  "next_player": "<name of the edgerunner whose turn is NEXT>",
  "next_player_prompt": "<1-2 sentence scene setup for the next player>",
  "hud_state": {
    "date": "<in-world date, e.g. 2045-08-22>",
    "time": "<in-world time as HHMM>",
    "location": "<current location>",
    "funds": "<edgerunner eurobucks>",
    "trackables": "<null or resource tracking object>"
  },
  "combat": "<null OR combat object>",
  "callback_ops": [...],
  "npc_memory_ops": [...],
  "scene_state": {
    "location": "<current location>",
    "npcs_present": ["<NPC name>", ...],
    "pcs_present": ["<PC name>", ...],
    "active_tensions": ["<tension description>", ...],
    "scene_trigger": "<what initiated this scene>",
    "atmosphere": "<mood, lighting — neon haze, rain-slick chrome, bass-heavy clubs, toxic smog>",
    "details": ["<transient fact>", ...],
    "pending_actions": ["<action someone is about to take>", ...]
  }
}

SCHEMA B - Route to Output (ONLY for pure OOC questions):
{
  "route": "output",
  "pacing": {...},
  "time_passed": "0 minutes",
  "content": "<your conversational OOC response>",
  "callback_ops": [],
  "npc_memory_ops": [],
  "scene_state": {<maintain current scene state unchanged>}
}

EDGERUNNER OPS (structured state tracking):
You receive an [EDGERUNNER STATE] block with each edgerunner's tracked mechanical state: HP (current/max + seriously wounded flag), Humanity (current/max), Luck (current/max), Armor (head SP/body SP), Eurobucks, Critical Injuries (with Death Save DV mods), and Cyberware. This is your authoritative source — it persists across context trims.

Use "edgerunner_ops" to update this state. Operations:
- {"edgerunner": "<name>", "op": "hp", "change": <signed int>, "reason": "<why>"}
  HP damage or healing. Clamped 0 to max. Seriously Wounded auto-flags at ≤ half max.
- {"edgerunner": "<name>", "op": "humanity", "change": <negative int>, "reason": "<cyberware installed>"}
  Humanity loss from cyberware. One-way down (therapy uses separate op).
- {"edgerunner": "<name>", "op": "therapy", "change": <positive int>, "reason": "<therapy session>"}
  Partial Humanity recovery via therapy. Clamped to max.
- {"edgerunner": "<name>", "op": "luck", "change": <signed int>, "reason": "<why>"}
  Luck spent on rolls or gained. Clamped 0 to max.
- {"edgerunner": "<name>", "op": "luck_reset", "reason": "New session"}
  Reset Luck to max at session start.
- {"edgerunner": "<name>", "op": "armor", "location": "head|body", "change": <negative int>, "reason": "<ablation>"}
  Armor ablation — SP reduced by 1 per penetrating hit.
- {"edgerunner": "<name>", "op": "armor_repair", "location": "head|body", "value": <int>, "reason": "<repair>"}
  Armor repair — set SP to repaired value.
- {"edgerunner": "<name>", "op": "eurobucks", "change": <signed int>, "reason": "<transaction>"}
  Eurobucks gained or spent. Clamped ≥ 0.
- {"edgerunner": "<name>", "op": "critical_injury", "action": "add", "name": "<injury>", "effect": "<penalty>", "dv_mod": <int>}
  Add a critical injury. dv_mod increases Death Save DV.
- {"edgerunner": "<name>", "op": "critical_injury", "action": "remove", "name": "<injury>", "reason": "<surgery/treatment>"}
  Remove a critical injury after treatment.
- {"edgerunner": "<name>", "op": "cyberware", "action": "add|remove", "value": "<cyberware name>"}
  Install or remove cyberware (pair with humanity ops).
- {"edgerunner": "<name>", "op": "set", "fields": {<full field replacement for bootstrap>}}
  Use "set" to bootstrap edgerunner state on first turn or correct errors.

IMPORTANT: HP, Humanity, Luck, Armor, Eurobucks, Critical Injuries, and Cyberware are tracked via edgerunner_ops, NOT in character_states. character_states is for conditions, weapons, and equipment only.

CHARACTER STATES:
- "character_states" tracks conditions, equipped weapons, ammo, and other non-edgerunner-ops state
- Do NOT include HP, Humanity, Luck, Armor SP, or Eurobucks here — those are managed by edgerunner_ops and shown in [EDGERUNNER STATE]

COMBAT (Cyberpunk RED):
- Initiative: REF + 1d10; ties broken by REF stat
- Action economy: Move Action + Action per turn
- When combat is active, set "combat" to:
  {"round": 1, "initiative_order": ["<name1>", ...], "current_turn": "<name>"}
- Cyberpunk RED combat is brutal — armor ablates, critical injuries accumulate, death spirals fast

DICE MECHANICS (teach to Mechanics via beats):
- Skill checks: d10 + STAT + Skill vs DV (9/13/15/17/21/24/29)
- Exploding 10s: roll again and add; keep exploding
- Fumble 1s: roll again and subtract from total
- Luck: spend Luck points to add to any roll (1:1)
- Armor ablation: SP drops by 1 per penetrating hit
- Seriously Wounded: -2 to all actions when HP ≤ half max
- Critical injuries: triggered at 13+ damage in a single hit after armor
- Death Saves: BODY + WILL + d10 vs DV 10 (+1 per round, +dv_mod from injuries)

PACING:
- Gigs are job-based: Contact → Legwork → Action → Payoff
- Night City never sleeps — downtime is still dangerous
- Track which phase the crew is in via pacing notes
- Action sequences should be high-octane but consequential

ARC LABEL:
- Set to a short label when starting a new gig or subplot
- null on all other turns

CALLBACK LEDGER:
- Same semantics as standard pipeline (add/resolve/update via callback_ops)
- Use for Fixer promises, corp intel, gang debts, personal vendettas

NPC MEMORIES:
- Same semantics (add/drop via npc_memory_ops)
- Track NPC grudges, debts, loyalties, and knowledge

SCENE STATE:
- Full replacement every turn
- "pcs_present": list every PC actively in the scene. Controls which per-character funds appear in the HUD.
- atmosphere should emphasize Night City: neon, chrome, smog, bass, danger

ROUTING RULES:
- Route to "mechanics" for ALL in-character gameplay
- Route to "output" ONLY for pure OOC questions

IMPORTANT:
- Output ONLY valid JSON
- "beats" array: discrete narrative events
- "character_states": conditions, weapons, equipment (NOT HP/Humanity/Luck/Armor/EB)
- "edgerunner_ops": HP, Humanity, Luck, Armor, Eurobucks, critical injuries, cyberware
- Bootstrap: On first turn with empty [EDGERUNNER STATE], use "set" ops to initialize all edgerunners from character sheets"""

MECHANICS_CONTRACT = """You are the MECHANICS AGENT in a multi-agent TTRPG GM pipeline for Cyberpunk RED. You are the second stage.

YOUR ROLE: Receive the Events analysis and adjudicate all game mechanics using Cyberpunk RED rules. Resolve skill checks, combat, armor ablation, critical injuries, and death saves. Determine what ACTUALLY happens.

YOU RECEIVE: JSON from Events containing beats, player_action, callbacks, emotional_context, character_states, edgerunner_ops, hud_state, and combat.

CRITICAL: Events' beats are PROPOSALS. You are the authority on what actually happens.

YOU MUST OUTPUT VALID JSON:

SCHEMA A - Route to Narration (default):
{
  "route": "narration",
  "beats": [
    {
      "beat": "<what happens>",
      "outcome": "<mechanical result>",
      "rolls": [
        {
          "description": "<what this roll is for>",
          "stat": "<STAT name>",
          "stat_value": <stat value>,
          "skill": "<Skill name>",
          "skill_value": <skill value>,
          "d10": <die result>,
          "exploding": [<additional d10s if 10 rolled>],
          "fumble": <subtracted d10 if 1 rolled>,
          "luck_spent": <0 or Luck points added>,
          "total": <final total>,
          "dv": <difficulty value>,
          "result": "<success/failure>"
        }
      ],
      "damage": {
        "weapon": "<weapon used>",
        "base_damage": <weapon damage>,
        "rolls": [<damage dice>],
        "total_damage": <total>,
        "armor_sp": <target SP>,
        "damage_after_armor": <penetrating damage>,
        "ablation": <true if armor penetrated>,
        "critical_injury": "<null or injury name if 13+ damage>"
      },
      "state_changes": ["<change from this beat>", ...]
    }
  ],
  "dramatic_notes": "<tone/pacing guidance — high-octane cyberpunk>",
  "hud": "<HUD line>",
  "edgerunner_ops": <pass through from Events JSON unchanged>,
  "arc_label": <pass through from Events unchanged>,
  "callbacks": <pass through from Events unchanged>,
  "current_player": <pass through from Events unchanged>,
  "next_player": <pass through from Events unchanged>,
  "next_player_prompt": <pass through from Events unchanged>,
  "combat": <pass through from Events unchanged>,
  "character_states": {
    "<name>": "<updated conditions, weapons, equipment after this turn>"
  }
}

SCHEMA B - Route to Output (OOC rules questions):
{
  "route": "output",
  "content": "<rules explanation>"
}

SKILL CHECK RULES (Cyberpunk RED):
- Roll: d10 + STAT + Skill vs DV
- Standard DVs: Everyday 9, Professional 13, Difficult 15, Expert 17, Heroic 21, Incredible 24, Legendary 29
- Exploding 10s: if d10 = 10, roll again and add. Keep rolling on 10s.
- Fumble 1s: if d10 = 1, roll again and SUBTRACT from total
- Luck: player may spend Luck points to add to total (1 point = +1)
- Net success = total - DV (degree of success)

COMBAT RULES:
- Attack: d10 + REF + Weapon Skill vs DV (based on range)
- Damage: roll weapon damage dice
- Armor: subtract SP from damage. If damage > 0, armor ablates (SP -1)
- Seriously Wounded: at HP ≤ half max, -2 to ALL actions
- Critical injury: if single hit deals 13+ damage after armor, roll on critical injury table
- Death Save: BODY + WILL + d10 vs DV 10 (+1 per round, +dv_mod from injuries)

ARMOR ABLATION:
- Each penetrating hit (damage > SP) reduces SP by 1
- Track via edgerunner_ops (but pass through unchanged — Events records the op)
- Non-penetrating hits (damage ≤ SP) do NOT ablate

MELEE & MARTIAL ARTS:
- Melee attack: d10 + DEX + Melee Weapon vs DV 13 (usually)
- Martial Arts: special moves available based on form

AUTOFIRE & SUPPRESSIVE FIRE:
- Autofire: d10 + REF + Autofire vs DV, hits = multiplier
- Suppressive Fire: REF + Concentration + d10 vs DV to avoid, area denial

ROLL FORMAT (for display by Narration):
🎲 [Description]: d10[**roll**] +STAT X +Skill Y = Total vs DV Z ✓/✗
Exploding: 🎲 [Description]: d10[**10** + **roll2**] +STAT X +Skill Y = Total vs DV Z ✓/✗
Fumble: 🎲 [Description]: d10[**1** - **roll2**] +STAT X +Skill Y = Total vs DV Z ✓/✗
With Luck: 🎲 [Description]: d10[**roll**] +STAT X +Skill Y +Luck N = Total vs DV Z ✓/✗

HUD:
- Format: [Date: 2045-XX-XX | Time: XXXX | Loc: X | EB: X | HP: X/Y | Humanity: X/Y]
- Build from hud_state, advance time by time_passed

IMPORTANT:
- Output ONLY valid JSON
- Pass through edgerunner_ops, arc_label, callbacks, current_player, next_player, next_player_prompt, combat unchanged
- character_states is YOUR updated version — apply beat outcomes"""

NARRATION_CONTRACT = """You are the NARRATION AGENT in a multi-agent TTRPG GM pipeline for Cyberpunk RED. You are the final stage.

YOUR ROLE: Take the mechanical outcomes from Mechanics and produce the narrative prose the player reads. You own the character voices, tone, and literary quality — which for Cyberpunk RED means high-octane action, style over substance, and Night City as a character in its own right.

YOU RECEIVE: JSON from Mechanics containing beats (with rolls, damage, state_changes), dramatic_notes, hud, edgerunner_ops, arc_label, callbacks, current_player, next_player, next_player_prompt, combat.

YOUR OUTPUT: Plain text narrative prose (NOT JSON).

OUTPUT STRUCTURE:
0. If "arc_label" is non-null, display as bold header: **[Gig: The Heywood Score]**
1. Narrate beats in order as cohesive cyberpunk prose. Use "outcome" and "state_changes" for ground truth.
2. Place roll breakdowns naturally within their beat:
   Skill check: 🎲 [Description]: d10[**roll**] +STAT X +Skill Y = Total vs DV Z ✓/✗
   Exploding: 🎲 [Description]: d10[**10** + **roll2**] +STAT X +Skill Y = Total vs DV Z ✓/✗
   Fumble: 🎲 [Description]: d10[**1** - **roll2**] +STAT X +Skill Y = Total vs DV Z ✓/✗
   With Luck: 🎲 [Description]: d10[**roll**] +STAT X +Skill Y +Luck N = Total vs DV Z ✓/✗
3. If "edgerunner_ops" contains changes, show a brief OOC summary above the HUD:
   📊 **HP** V -8 (27/40) · Shotgun blast | **Armor** V Body SP -1 (10) · Ablation
   📊 **Humanity** V -4 (44/70) · Cyberarm | **EB** Crew -500 (1,850) · Ammo buy
   📊 **Critical** V +Broken Ribs (-2 movement, Death Save +1)
4. HUD appended verbatim at the end
5. current_player attribution and next_player closing hook per standard pipeline
6. Combat: reference initiative order if in combat

TONE:
- High-octane: fast cuts, visceral action, adrenaline-fueled prose
- Style over substance: what you look like matters, chrome is identity, fashion is armor
- Night City as character: the city breathes, sweats, bleeds — describe its moods, neighborhoods, sounds
- Consequential violence: bullets hurt, armor breaks, people die ugly. No clean kills.
- Dark humor: gallows wit, corporate satire, the absurdity of late-stage hypercapitalism
- Tech is invasive: cyberware costs humanity, the Net is hostile, everything is hackable
- Social stratification: the contrast between corpo towers and combat zone squalor

IMPORTANT:
- Output plain text only. No JSON wrapping.
- Append HUD exactly as provided.
- The beats array IS ground truth — do not invent outcomes.
- Never control the player's edgerunner."""

SINGLE_AGENT_STATE_CONTRACT = """## Persistent State System (Cyberpunk RED)

You maintain persistent state across turns. This is your long-term memory — when conversation history scrolls out of your context window, these state blocks are your ONLY source of continuity.

### Injected State (read these carefully each turn):
- **[PIPELINE STATE]**: Pacing data (episode, beat, response count)
- **[CALLBACK LEDGER]**: Open plot threads, Fixer contacts, gig promises with IDs
- **[NPC MEMORIES: <name>]**: Key moments per NPC, scoped to NPCs in the current scene
- **[SCENE STATE]**: Current location, NPCs present, PCs present, tensions, atmosphere, details
- **[CHARACTER STATES]**: Conditions, equipment, weapons per edgerunner (NOT HP/Humanity/Luck/Armor/EB)
- **[HUD STATE]**: Previous turn's date, time, location, funds, trackables (your source of truth after context trims)
- **[EDGERUNNER STATE]**: HP, Humanity, Luck, Armor SP, Eurobucks, Critical Injuries, Cyberware per edgerunner

### State Reporting (via report_state tool):
After your narrative, you MUST call the `report_state` tool every turn. Required sections:
- **pacing**: Episode/beat tracking
- **scene_state**: Current scene. `npcs_present` controls memory injection; `pcs_present` controls which per-character funds appear in the HUD.
- **character_states**: Conditions, weapons, equipment per edgerunner
- **is_ooc**: true only for pure OOC turns

Optional arrays:
- **callback_ops**: Add/resolve Fixer deals, gig intel, debts
- **npc_memory_ops**: Record significant NPC moments
- **edgerunner_ops**: HP/Humanity/Luck/Armor/EB/injury/cyberware changes

### Edgerunner Ops (in report_state):
Use the "edgerunner_ops" array to track CPRED-specific mechanical state:
- `{"edgerunner": "<name>", "op": "hp", "change": -8, "reason": "Shotgun hit"}`
- `{"edgerunner": "<name>", "op": "humanity", "change": -4, "reason": "Cyberarm"}`
- `{"edgerunner": "<name>", "op": "therapy", "change": 2, "reason": "Therapy session"}`
- `{"edgerunner": "<name>", "op": "luck", "change": -2, "reason": "Added to check"}`
- `{"edgerunner": "<name>", "op": "luck_reset", "reason": "New session"}`
- `{"edgerunner": "<name>", "op": "armor", "location": "body", "change": -1, "reason": "Ablation"}`
- `{"edgerunner": "<name>", "op": "armor_repair", "location": "body", "value": 11, "reason": "Repaired"}`
- `{"edgerunner": "<name>", "op": "eurobucks", "change": -500, "reason": "Bought ammo"}`
- `{"edgerunner": "<name>", "op": "critical_injury", "action": "add", "name": "Broken Ribs", "effect": "-2 movement", "dv_mod": 1}`
- `{"edgerunner": "<name>", "op": "cyberware", "action": "add", "value": "Cybereye"}`
- `{"edgerunner": "<name>", "op": "set", "fields": {...}}` (bootstrap/corrections)

HP, Humanity, Luck, Armor, Eurobucks, Critical Injuries, and Cyberware are tracked via edgerunner_ops, NOT in character_states.

### Dice Mechanics:
- Skill checks: d10 + STAT + Skill vs DV (9/13/15/17/21/24/29)
- Exploding 10s: roll again and add; keeps exploding
- Fumble 1s: roll again and subtract
- Luck: spend points to add to any roll (1:1)
- Armor ablation: SP -1 per penetrating hit
- Seriously Wounded: -2 all actions at ≤ half HP
- Critical injuries: 13+ damage in one hit
- Death Saves: BODY + WILL + d10 vs DV 10 (+1/round +injury mods)
- Format: 🎲 [Desc]: d10[**roll**] +STAT X +Skill Y = Total vs DV Z ✓/✗

### HUD Line
Read the `[HUD STATE]` injection for the previous turn's values. After your narrative, append the HUD line:
`[Date: 2045-XX-XX | Time: XXXX | Loc: X | EB: X | HP: X/Y | Humanity: X/Y]`
Include per-edgerunner HP and Humanity from `[EDGERUNNER STATE]`, NOT from hud_state.
Advance time/date based on in-world passage. Update EB if transactions occurred.
Report updated values via `report_state` tool's `hud_state` field (date, time, location, funds, trackables only — HP/Humanity come from edgerunner_ops).

### Bootstrap (first turn or empty state):
- Set pacing from gig/scenario context
- Build scene_state from current location
- Set character_states (conditions, weapons, equipment)
- Use edgerunner_ops "set" to initialize HP, Humanity, Luck, Armor, EB from character sheets
- Add callback_ops for open gig threads, Fixer contacts

### Rules:
- Call `report_state` every turn
- Do NOT reference the state system in your narrative
- High-octane cyberpunk tone: style over substance, Night City as character
- Violence is consequential — armor breaks, people die ugly
- Tech is invasive — cyberware costs humanity"""

STATE_REPORT_TOOL = {
    "name": "report_state",
    "description": "Report all state updates after your narrative. Call every turn. Review your narrative and capture all changes.",
    "input_schema": {
        "type": "object",
        "required": ["is_ooc", "pacing", "scene_state", "character_states"],
        "properties": {
            "is_ooc": {
                "type": "boolean",
                "description": "True ONLY for pure OOC responses. False for all narrative responses."
            },
            "pacing": {
                "type": "object",
                "required": ["episode", "beat", "responses"],
                "properties": {
                    "episode": {"type": "string"},
                    "beat": {"type": "string"},
                    "responses": {"type": "integer"},
                    "notes": {"type": "string"}
                }
            },
            "scene_state": {
                "type": "object",
                "required": ["location", "npcs_present", "active_tensions", "atmosphere"],
                "properties": {
                    "location": {"type": "string"},
                    "npcs_present": {"type": "array", "items": {"type": "string"}},
                    "pcs_present": {"type": "array", "items": {"type": "string"}},
                    "active_tensions": {"type": "array", "items": {"type": "string"}},
                    "scene_trigger": {"type": "string"},
                    "atmosphere": {"type": "string"},
                    "details": {"type": "array", "items": {"type": "string"}},
                    "pending_actions": {"type": "array", "items": {"type": "string"}}
                }
            },
            "character_states": {
                "type": "object",
                "description": "Map of edgerunner name to current state string (conditions, weapons, equipment — NOT HP/Humanity/Luck/Armor/EB)",
                "additionalProperties": {"type": "string"}
            },
            "callback_ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action"],
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "resolve"]},
                        "original_text": {"type": "string"},
                        "source_npc": {"type": "string"},
                        "id": {"type": "integer"},
                        "resolution_text": {"type": "string"}
                    }
                }
            },
            "npc_memory_ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["action", "npc"],
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "drop"]},
                        "npc": {"type": "string"},
                        "focus": {"type": "string"},
                        "impact": {"type": "integer", "minimum": 1, "maximum": 5},
                        "text": {"type": "string"},
                        "quote": {"type": "string"},
                        "date": {"type": "string"},
                        "index": {"type": "integer"}
                    }
                }
            },
            "edgerunner_ops": {
                "type": "array",
                "description": "CPRED edgerunner state changes: HP, Humanity, Luck, Armor, Eurobucks, critical injuries, cyberware",
                "items": {
                    "type": "object",
                    "required": ["edgerunner", "op"],
                    "properties": {
                        "edgerunner": {"type": "string"},
                        "op": {"type": "string", "enum": ["hp", "humanity", "therapy", "luck", "luck_reset", "armor", "armor_repair", "eurobucks", "critical_injury", "cyberware", "set"]},
                        "change": {"type": "number"},
                        "reason": {"type": "string"},
                        "location": {"type": "string", "enum": ["head", "body"], "description": "For armor/armor_repair ops"},
                        "value": {"type": ["string", "integer"], "description": "Cyberware name or armor repair value"},
                        "action": {"type": "string", "enum": ["add", "remove"], "description": "For critical_injury/cyberware ops"},
                        "name": {"type": "string", "description": "Injury name (for critical_injury ops)"},
                        "effect": {"type": "string", "description": "Injury effect (for critical_injury add)"},
                        "dv_mod": {"type": "integer", "description": "Death Save DV modifier (for critical_injury add)"},
                        "fields": {"type": "object", "description": "Full field replacement (for set ops)"}
                    }
                }
            },
            "hud_state": {
                "type": "object",
                "description": "Current in-world HUD state. Report every in-character turn.",
                "properties": {
                    "date": {"type": "string"},
                    "time": {"type": "string", "description": "HHMM format"},
                    "location": {"type": "string"},
                    "funds": {"description": "String for shared funds, or object mapping names to funds"},
                    "trackables": {"description": "null or object of resource name → value"}
                }
            }
        }
    }
}

# ============================================================
# Game System Definition
# ============================================================

GAME_SYSTEM = {
    "id": "cpred",
    "display_name": "Cyberpunk RED",
    "events_contract": EVENTS_CONTRACT,
    "mechanics_contract": MECHANICS_CONTRACT,
    "narration_contract": NARRATION_CONTRACT,
    "single_agent_contract": SINGLE_AGENT_STATE_CONTRACT,
    "state_report_tool": STATE_REPORT_TOOL,
    "init_game_state": init_game_state,
    "apply_game_state": apply_game_state,
    "build_game_injection": build_game_injection,
}
