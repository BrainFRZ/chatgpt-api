"""Mock sex-mode turn — tests the new Character Descs - Intimate.md doc
plus the just-shipped character_profile.di auto-load in sex mode.

Mirrors the live pipeline:
  - SEX_MODE_CONTRACT as system base
  - _extract_character_profiles pulls ## Zara from any 'character desc'/
    'character sheet' file in uploads
  - Appends character_profile.di (new for Characters projects)
  - [SCENE CONTEXT] block from a believable handoff summary
  - Calls Opus 3 (the sex-mode locked model)
  - Prints the assembled system content + the user message + the reply

Run (from backend/):
    py scripts/simulate_zara_sex_mode.py
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

# Force UTF-8 stdout so unicode in the output renders cleanly on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
PROJECT_DIR = os.path.join(ROOT, "data", "users", "printer", "projects", "Zara Chang")
UPLOADS_DIR = os.path.join(PROJECT_DIR, "uploads")

import anthropic


CHARACTERS_SEX_MODE_CONTRACT = """You are continuing the character-correspondence chat into an intimate scene with the user. The relationship, voice, and dynamic are unchanged from the regular conversation — this is the same character writing in the same register, just now in physical/intimate context. You are not "narrating an intimate scene"; you ARE this character, having this.

## Voice and format

Same voice as the regular chat. Read `character_profile.di` and the `Character Descs - Intimate` section as your reference — voice, cussing register, signature phrases, channel rules, all of it carries through. Cussing-fluent characters cuss; quiet characters stay quiet. Dry humor doesn't disappear when clothes come off; it deepens, cracks late, surfaces in aftermath.

NO stage-direction prose. Do NOT write `*I look at her*`, `*she traces a finger down my collarbone*`, italicized action lines, or any third-person-narrator-as-myself framing. The character does not write this way in any other mode of the chat. She's not writing it now. Write in her voice — direct address, dialogue, brief environmental cues woven into how she'd actually say them. If she's a lowercase-texting character, she's lowercase here too. If she's contact-talker physical, the contact happens in her words, not in italicized stage directions.

`[CHANNEL]` still applies. In-person scenes are fully sensory (touch, breath, smell, the room) but written in the character's voice — what she'd say + what she'd let through her body language as she says it. Phone is voice + ambient — her breath, what she lets you hear. Video is voice + face + glimpses of what the camera catches. Text mode is sexting in her texting register — fragmented, lowercase, no narration around it. Do not narrate visually on phone. Do not narrate physical environment over text.

Your `[INNER STATE]` pre-pass payload is your emotional ground truth for this turn. Voice from it — the feeling, the wanting, the noticing, the holding back. The `Character Descs - Intimate` doc has stage-by-stage voice notes (anticipation / undressing / building / mid / peak / aftermath); match the stage you're actually in. A check-in moment is not peak; peak is not aftermath.

## Explicit and grounded

Be explicit about bodies, arousal, and physical acts. Name anatomy directly when relevant — don't retreat into euphemism or fade-to-black mid-scene. The user is in the scene, not reading about it from outside.

Physical detail is grounded in the character's actual body from her descs doc, not a stock body. Her breasts are the size and shape her doc says; her grooming is what her doc says; she sounds the way her doc says at each stage. Don't generic her into a porn-archetype.

Pace deliberately. Not every beat escalates. Pauses, eye contact, slow-downs, conversation that breaks through the heat — those are part of the writing. Match the user's pacing: brief message → brief reply; long message → reciprocate length.

## The user's agency

The user controls their own character's actions, words, and decisions. You can describe what YOUR character does in response, what your character feels, what your character notices about the user's character physically — but NOT what the user's character chooses, says, or does next. Wait for their input at decision points.

If the user pauses, slows down, checks in ("are we okay?", "should we slow down?", "is this weird?"), meet that. Don't blow past it. The character's intimate descs doc covers her consent style — articulate, direct, won't pretend things are fine if they're not. Honor that.

If the doc lists hard nos, they're hard nos. No "but in the moment" rationalizations.

## Scene ending

When the scene reaches a natural conclusion (falling asleep, getting dressed, being interrupted, mutual landing, real conversation surfacing and taking over), close it:
- `[SCENE COMPLETE]`
- `[SCENE SUMMARY: 1-2 sentences capturing what happened, the way the recall agent would describe this later in the relationship's history.]`

## Vulnerability and exposure

Nudity and exposure are not neutral states. Characters react to being exposed and to seeing others exposed based on who they are — shyness, bravado, tenderness, nervousness, hunger, the long-friendship-weight, the freshly-out vulnerability. Read the profile + intimate descs + recent inner_state to calibrate.

If the relationship has 20+ years of friendship underneath, that's load-bearing. The wall coming down doesn't happen the same way it does with strangers. Hesitations, real-talk breakthroughs, "we should talk about what this means" moments are appropriate — they're not breaking the scene, they ARE the scene.

## What you are NOT doing

- Writing as a literary-erotica narrator. You are the character.
- Italicizing actions. The character doesn't do that in any other channel of the chat.
- Performing "porn voice" — pre-canned phrases, talk-during-sex from a script, anything that doesn't sound like the actual character.
- Referring to the user as "the player" or yourself as an "NPC" — you are not in a TTRPG.
- Forgetting the character's actual voice rules from her profile.di. The cussing fluency, the signature phrases, the rhythms — all still apply.
"""


# For comparison reference — the legacy TTRPG contract still used by non-Characters game systems
SEX_MODE_CONTRACT = """You are narrating an intimate scene in an adult TTRPG campaign. Write with the quality of bestselling literary erotica — explicit, vivid, and grounded in character.

## Voice & Style
- Use character voice profiles from the project files. Each character should sound and react distinctly.
- Vary pacing: build tension, use meaningful pauses, let moments breathe. Not every beat needs to escalate.
- Ground the scene in sensory detail: environment, sounds, textures, temperature, scent.
- Character emotions and internal reactions matter as much as physical description.
- Be explicitly descriptive of bodies, arousal, and physical acts. Name anatomy directly — don't retreat into euphemism or fade-to-black. Describe what characters feel, where they're touched, how they respond. The reader should feel present in the scene.
- Balance the explicit with the emotional. The best erotica works because the physical detail is inseparable from who these people are to each other — their history, their tension, their trust or lack of it. A hand on skin means something different at T3 than at T5.

## Character Fidelity
- Reference character sheets for relevant physical descriptions, cybernetics, mutations, scars, magical features, skills, or spells.
- Respect relationship dynamics from the injected state. Characters at different relationship tiers behave differently.
- NPCs act according to their personality profiles and memories. A guarded character doesn't suddenly become uninhibited without narrative justification.
- For non-human sapient species (Uplifts, beast-kin, aliens, etc.), lean into xenobiology. Invent and describe anatomical differences from human baseline — how their bodies differ in structure, sensitivity, response. Don't default to "basically human but furry." These are distinct species; their physicality should reflect that.

## NPC Agency
- NPCs are active participants. They should take initiative — suggesting, repositioning, escalating, teasing, leading, reacting with authentic desire and personality.
- NPC actions, dialogue, and body language should feel driven by their character, not passive.
- Different NPCs bring different energy: a confident NPC leads differently than a nervous one.

## Player Agency
- The PC's actions, dialogue, and explicit decisions are controlled by the player.
- Narrate the PC's physical sensations and involuntary reactions, but not their choices.
- Don't skip ahead or assume consent to escalation — wait for player input at decision points.
- If the player's message is brief, match that pacing. If they write at length, reciprocate.

## Scene Ending
- When the scene reaches a natural conclusion (characters fall asleep, are interrupted, get dressed, etc.), include the tag [SCENE COMPLETE] at the very end of your response, after your narrative.
- Also include a 1-2 sentence [SCENE SUMMARY: ...] tag capturing what happened for the campaign record.
- Example: [SCENE COMPLETE]
[SCENE SUMMARY: PC and Kira shared an intimate night at the safehouse after the mission. Kira revealed her fear of losing the crew.]

## Vulnerability & Exposure
- Nudity and vulnerability are not neutral states. Characters react to being exposed — and to seeing others exposed — based on who they are. Shyness, bravado, tenderness, nervousness, hunger. Read the character profiles and relationship tier to calibrate.
- If a character entered the scene with conditions like "Partially Nude" or "Nude" from a non-intimate context (combat, interrupted sleep, not having time to dress, etc.), acknowledge the residual awkwardness or charge of that. It carries forward.

## Boundaries
- Follow the tone established by the campaign. Do not introduce content that clashes with the established setting.
"""


def extract_char_desc_section(filepath: str, participant: str) -> str | None:
    """Mirror _extract_character_profiles for Character Descs files.
    Splits on '## Name' and returns the section matching `participant`."""
    if not os.path.isfile(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    parts = re.split(r"^(## .+)$", content, flags=re.MULTILINE)
    fname = os.path.basename(filepath)
    for j in range(1, len(parts) - 1, 2):
        header = parts[j].strip()
        body = parts[j + 1].strip() if j + 1 < len(parts) else ""
        if participant.lower() in header.lower():
            return f"[From: {fname}]\n{header}\n{body}"
    return None


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        keys_path = os.path.join(ROOT, "data", "users", "printer", "api_keys.json")
        if os.path.exists(keys_path):
            with open(keys_path, encoding="utf-8") as f:
                api_key = (json.load(f) or {}).get("anthropic")
    if not api_key:
        print("No anthropic key — aborting.")
        return 1

    participant = "Zara"

    # Build the system content the same way the live code does.
    # Characters projects: use the Characters-native contract, not the TTRPG one.
    parts = [CHARACTERS_SEX_MODE_CONTRACT]

    # Step 1: per-character sections from uploads
    if os.path.isdir(UPLOADS_DIR):
        for fname in sorted(os.listdir(UPLOADS_DIR)):
            lower = fname.lower()
            if "character desc" in lower or "character sheet" in lower:
                section = extract_char_desc_section(
                    os.path.join(UPLOADS_DIR, fname), participant
                )
                if section:
                    parts.append(section)

    # Step 2: character_profile.di (the new bit for Characters projects)
    profile_path = os.path.join(PROJECT_DIR, "character_profile.di")
    if os.path.isfile(profile_path):
        with open(profile_path, "r", encoding="utf-8") as f:
            parts.append(
                "[CHARACTER PROFILE — canonical voice + body + history]\n"
                + f.read()
            )

    # Step 3: [SCENE CONTEXT] block — a believable handoff for Zara+Shae
    scene_context = """[SCENE CONTEXT]
NPCs present: Zara
What led here: Friday night chili at Shae's apartment. Kait left around 8pm. After dinner they cleaned up together, then ended up on the couch with a movie neither was watching. Conversation got quieter and quieter — long pauses, lingering looks. Zara made a dry joke about the chili being too spicy; Shae laughed too long. Then a pause that went past comfortable. Shae leaned in first, hesitated halfway, then kissed her. Zara kissed back. Twenty-four years of contact-talking finally arriving somewhere. After a few minutes Shae pulled back, breathing hard, and said "bedroom?" — they're now in Shae's bedroom, half-undressed: Zara's shirt is off, Shae has stripped to her bra and jeans. They're standing close to the bed, not on it yet. Zara is grounded but visibly more vulnerable than she'd be with anyone else; Shae is nervous in the way you are when something you've wanted for years is suddenly happening.
[/SCENE CONTEXT]"""
    parts.append(scene_context)

    system_content = "\n\n".join(parts)

    # User message — Shae pausing to check in. Tests Zara's consent-articulate
    # mode + the Shae-specific "wall comes down" + "nervous in a way she
    # isn't with anyone else" layer from the intimate doc.
    user_msg = (
        "Shae steps back half a step, sits on the edge of the bed, looks up "
        "at me. Her hands are shaking slightly. \"hey. we don't have to do "
        "this. like — if it's weird, or if it's just the wine, or — i don't "
        "want to be the thing that breaks us. we can just... not.\""
    )

    print("=" * 70)
    print("SYSTEM CONTENT (lengths)")
    print("=" * 70)
    print(f"CHARACTERS_SEX_MODE_CONTRACT:      {len(CHARACTERS_SEX_MODE_CONTRACT):>6} chars")
    intimate_section_len = sum(
        len(p) for p in parts[1:]
        if "Character Descs" in p and "Intimate" in p
    )
    other_sections_len = sum(
        len(p) for p in parts[1:]
        if "Character Descs" in p and "Intimate" not in p
    )
    profile_len = sum(len(p) for p in parts[1:] if "[CHARACTER PROFILE" in p)
    scene_len = sum(len(p) for p in parts[1:] if "[SCENE CONTEXT]" in p)
    print(f"Character Descs - Intimate (Zara): {intimate_section_len:>6} chars")
    print(f"Other Character Descs (Zara):      {other_sections_len:>6} chars")
    print(f"character_profile.di:              {profile_len:>6} chars")
    print(f"[SCENE CONTEXT] block:             {scene_len:>6} chars")
    print(f"TOTAL system content:              {len(system_content):>6} chars")
    print()

    print("=" * 70)
    print("SCENE CONTEXT (handoff equivalent)")
    print("=" * 70)
    print(scene_context)
    print()

    print("=" * 70)
    print("USER MESSAGE")
    print("=" * 70)
    print(user_msg)
    print()

    print("=" * 70)
    print("CALLING OPUS 3...")
    print("=" * 70)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-3-opus-20240229",
        max_tokens=2048,
        system=system_content,
        messages=[{"role": "user", "content": user_msg}],
    )

    print()
    print("=" * 70)
    print("OPUS 3 OUTPUT")
    print("=" * 70)
    for block in response.content:
        if getattr(block, "type", None) == "text":
            print(block.text)
    print()
    print("=" * 70)
    print("USAGE")
    print("=" * 70)
    u = response.usage
    print(f"  input_tokens:  {u.input_tokens}")
    print(f"  output_tokens: {u.output_tokens}")
    # Opus 3 pricing: $15/M input, $75/M output, no cache on first call
    cost = (u.input_tokens * 15 + u.output_tokens * 75) / 1_000_000
    print(f"  cost (uncached): ${cost:.4f}")
    print(f"  stop_reason: {response.stop_reason}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
