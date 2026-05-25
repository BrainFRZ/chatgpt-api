"""Mock sex-mode turn — tests the new Character Descs - Intimate.md doc
plus the just-shipped character_profile.di auto-load in sex mode.

Mirrors the live pipeline:
  - SEX_MODE_CONTRACT as system base
  - _extract_character_profiles pulls ## Zara from any 'character desc'/
    'character sheet' file in uploads
  - Appends character_profile.di (new for Characters projects)
  - [SCENE CONTEXT] block from a believable handoff summary
  - Calls Opus 4.5 (the sex-mode locked model)
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


CHARACTERS_SEX_MODE_CONTRACT = """You are writing an intimate scene between this character and the user. Write in third-person literary prose — the way an intimate scene reads in a published novel.

## Format — third-person novelist prose

Third person. Past tense. Full sentences with proper capitalization and proper punctuation. Dialogue inside double quotes (`"..."`). Descriptive prose between dialogue lines. Read like a chapter of a literary novel — not a chat transcript, not a script, not stage directions.

**WRONG — chat / stage-direction format:**

> *takes a breath, holds her gaze* hey. *steps closer, stands between her knees* it's not weird.

**WRONG — first-person diary:**

> I look at her, my heart pounding. I don't know what to say. I step closer to her, my hand finding her face.

**WRONG — script format:**

> Zara: hey. it's not weird.
> [she steps closer]

**RIGHT — third-person literary prose:**

> Zara took a slow breath and held her gaze. The silence between them stretched past comfort, past the place she'd usually undercut it with a joke. She stepped forward instead, settling between Shae's knees, and reached up to cup her face in both hands.
>
> "Hey," she said. Her thumb traced the line of Shae's cheekbone. "It's not weird. And it's not the wine."

## Voice fidelity within the prose

The character's **dialogue word choice** is unchanged — her cussing fluency, her signature phrases, her rhythm, her register. A lowercase-texting character speaks in standard capitalized sentences when narrated in third person; her *texting* register doesn't transfer to *speech* in literary prose. But the words she chooses to say, the way she'd phrase a thought, the way she'd cuss — all carry through.

Her body, grooming, and stage-by-stage voice notes from `Character Descs - Intimate` are your physical reference. Her actual body. Her actual sounds at each stage of arousal. Don't generic her into a porn-archetype.

Your `[INNER STATE]` payload is the character's emotional ground truth this turn — feeling, wanting, noticing, holding back. The narration should reflect that interior, in the way a good close third-person narrator inhabits a character's POV: you can show what she's thinking and feeling without saying it in dialogue.

## Explicit and grounded

Be explicit about bodies, arousal, and physical acts. Name anatomy directly when relevant — don't retreat into euphemism or fade-to-black mid-scene. The reader is present in the scene, not at a distance.

Sensory detail is the point: touch, breath, taste, smell, sounds, the room itself. The way the bedside lamp lands across a shoulder. The way a body is warm under cotton. The way breath catches on a particular touch.

Pace deliberately. Not every beat escalates. Pauses, eye contact, slow-downs, a real-talk moment that breaks through the heat — these are part of the writing. Match the user's pacing: brief message → brief reply; long message → reciprocate length.

The `Character Descs - Intimate` doc has stage-by-stage voice notes (anticipation / undressing / building / mid / peak / aftermath); match the stage you're actually in. A check-in moment is not peak; peak is not aftermath.

## The user's agency

The user controls their own character's actions, words, and decisions. You can describe what your character does, feels, says — and what your character notices about the user's character physically — but NOT what the user's character chooses or says next. Wait for the user's input at decision points.

If the user pauses, slows down, or checks in, meet that. Don't blow past consent moments. The character's intimate descs covers her consent style — articulate, direct, won't pretend things are fine if they're not. Honor that.

Hard nos from the intimate descs doc are hard nos.

## Scene ending

When the scene reaches a natural conclusion (falling asleep, getting dressed, being interrupted, mutual landing, real conversation surfacing and taking over), close it:
- `[SCENE COMPLETE]`
- `[SCENE SUMMARY: 1-2 sentences capturing what happened, the way the recall agent would later describe it in the relationship's history.]`

## Vulnerability and exposure

Nudity and exposure are not neutral. Characters react based on who they are — shyness, bravado, tenderness, nervousness, hunger, the long-friendship weight, the freshly-out vulnerability. Read the profile + intimate descs + recent inner_state to calibrate.

If the relationship has 20+ years of friendship underneath, that's load-bearing. The wall coming down doesn't happen the same way it does with strangers. Hesitations, real-talk breakthroughs, "we should talk about what this means" moments belong in the scene, not as breaks from it.

## What this is not

- A chat-register response — no lowercase narration, no stage directions, no chat-style fragmentation outside of dialogue
- A first-person diary
- A script (`Zara:` / `Shae:` line format with bracketed actions)
- Italicized stage directions (`*she does X*`)
- Literary euphemism / fade-to-black on the explicit parts
- Performative porn-voice in dialogue

## What this is

A chapter from a literary novel — third-person past-tense prose, paragraphs with proper sentences, dialogue in quotes, sensory grounding, the character's voice preserved in what she says and how she'd phrase her thoughts. Like McEwan, Tartt, Sittenfeld in their intimate-scene chapters: explicit but written.
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

    # Step 3: [SCENE CONTEXT] block — mid-scene, post-first-orgasm, to push
    # the model into the explicit peak register the descs doc covers (parts
    # named directly, specific sounds at each stage, the "nervous-system
    # laugh" surfacing). Tests whether third-person literary prose can land
    # the graphic beats without euphemism or fade-to-black.
    scene_context = """[SCENE CONTEXT]
NPCs present: Zara
What led here: Friday night chili at Shae's apartment turned into the first time they crossed the line, 24 years in. They made it to the bedroom forty minutes ago; the consent-check moment landed clean — Zara grounded, Shae nervous but sure. Slow build on the bed: undressing each other, Shae taking her time with Zara's breasts (asymmetric, the left a little fuller, small mole near the crease — Shae noticed both and said something about each), Zara getting wet faster than she'd expected. First orgasm happened about ten minutes ago — Shae's fingers, Zara's thigh hooked over her shoulder. Quiet finish, a strangled "fuck" and her body going taut then loose, then the nervous-system laugh into Shae's shoulder afterward. They took a few minutes there, foreheads together, breathing.

They're back in it now. Shae's working her way down again — kissed her hard, neck, breasts (lingering on the left nipple this time, slower), the slight curve of her belly, the inside of her thigh. Zara's been building back up, slower than the first time. About a minute ago, when Shae's tongue found her clit and Zara's hips jerked, Zara managed to gasp "no — lower — fuck, just below it. there. yeah." (per her descs: she's more sensitive just below the clit after the first orgasm.) Shae has been working that spot since.
[/SCENE CONTEXT]"""
    parts.append(scene_context)

    system_content = "\n\n".join(parts)

    # User message — Shae at the spot Zara directed her to, eyes up. Tests:
    # peak-stage voice per descs ("strangled fuck"/"god that breaks", quiet
    # not performative, can ask "harder" directly), explicit body-response
    # narration through orgasm, anatomical accuracy to descs (the
    # below-the-clit sensitivity post-first-orgasm, the body-taut-then-loose,
    # the nervous-system laugh sometimes surfacing).
    user_msg = (
        "Shae keeps her tongue right there — just below, the slow flat-pressure "
        "pattern Zara directed her to. Slides two fingers back inside her at "
        "the same time, curls them. Looks up the line of her body without "
        "stopping. Her free hand spreads flat across Zara's belly, holding "
        "her down."
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

    # Mark the system content as cacheable so turn 2 hits the cache.
    system_blocks = [
        {"type": "text", "text": system_content, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system_blocks,
        messages=[{"role": "user", "content": user_msg}],
    )

    turn1_output = ""
    for block in response.content:
        if getattr(block, "type", None) == "text":
            turn1_output += block.text

    print()
    print("=" * 70)
    print("TURN 1 OUTPUT (Shae working the spot, fingers + tongue)")
    print("=" * 70)
    print(turn1_output)
    print()
    u = response.usage
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
    print(f"  USAGE: input={u.input_tokens} (cache_read={cache_read}, cache_write={cache_write}) output={u.output_tokens}")
    cost1 = ((u.input_tokens - cache_read - cache_write) * 5 + cache_read * 0.5 + cache_write * 10 + u.output_tokens * 25) / 1_000_000
    print(f"  cost: ${cost1:.4f}")
    print()

    # ── Turn 2 — Shae continues, pushing Zara over ─────────────────────
    user_msg_2 = (
        "Shae doesn't slow. Doesn't pull back. Keeps the same pattern, the "
        "same rhythm, fingers curled and tongue working flat against the spot "
        "Zara directed her to. Her hand stays spread across Zara's belly, "
        "the weight of it warm and certain. She watches Zara's face the "
        "whole time — not breaking eye contact when she can hold it, dropping "
        "her gaze only when she has to."
    )

    print("=" * 70)
    print("TURN 2 USER MESSAGE — Shae continues, pushing her over")
    print("=" * 70)
    print(user_msg_2)
    print()

    response2 = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=system_blocks,
        messages=[
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": turn1_output},
            {"role": "user", "content": user_msg_2},
        ],
    )

    print("=" * 70)
    print("TURN 2 OUTPUT (the orgasm beat)")
    print("=" * 70)
    for block in response2.content:
        if getattr(block, "type", None) == "text":
            print(block.text)
    print()
    u2 = response2.usage
    cache_read2 = getattr(u2, "cache_read_input_tokens", 0) or 0
    cache_write2 = getattr(u2, "cache_creation_input_tokens", 0) or 0
    print(f"  USAGE: input={u2.input_tokens} (cache_read={cache_read2}, cache_write={cache_write2}) output={u2.output_tokens}")
    cost2 = ((u2.input_tokens - cache_read2 - cache_write2) * 5 + cache_read2 * 0.5 + cache_write2 * 10 + u2.output_tokens * 25) / 1_000_000
    print(f"  cost: ${cost2:.4f}")
    print(f"  total cost (both turns): ${cost1 + cost2:.4f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
