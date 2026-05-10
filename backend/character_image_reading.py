"""Image-reading pre-pass for the Characters voice agent.

Why: the voice/correspondence model is Opus 3 (chosen for personality/voice
strength) but Opus 3's vision is the older stack — text-in-image reading is
shaky, fine detail gets missed. When the user attaches an image (or pastes
an image URL in their message text), we run a current-gen vision model
(Opus 4.5 by default) over the image first to produce a structured reading,
then feed that reading into Opus 3's context as TEXT instead of attaching
the raw image block.

Architecture mirrors the other Characters pre-passes (recall via Haiku 4.5,
inner-state via Sonnet 4.6): a separate model handles a specific cognitive
task and surfaces a structured payload to the voice slot. Voice stays Opus 3.

Why Opus 4.5 by default (not Sonnet 4.6): for memes specifically the "intent"
field — the joke, the irony, the comedic tone — benefits from Opus's nuance.
Sonnet would describe contents fine but undersell the punchline. ~5x the
per-image cost (~$0.04 vs ~$0.008) for noticeably richer reading. Override
via CHARACTER_IMAGE_READING_MODEL env var if you want to drop to Sonnet.

Output is stashed on `characters_state["_render_payload"]["image_readings"]`
as a list of {format, text_on_image, visual_content, intent, source}, one per
image. The voice agent reads them via build_image_reading_injection.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# Vision model for the reading pass. Opus 4.5 is default — best at intent /
# tone / "what's the joke" reading. Set CHARACTER_IMAGE_READING_MODEL to
# override (e.g. claude-sonnet-4-6 for ~5x cost reduction with somewhat-blander
# intent reads).
IMAGE_READING_MODEL = os.environ.get("CHARACTER_IMAGE_READING_MODEL", "claude-opus-4-5")
IMAGE_READING_MAX_TOKENS = 1500   # rich-enough output for several images
IMAGE_READING_TIMEOUT_S = 30


def _read_images_tool(n_images: int) -> dict:
    """Forced tool schema. We pass N image blocks; the model returns N readings.

    The model is told to keep array length == N so we don't have to re-correlate.
    """
    return {
        "name": "report_images",
        "description": "Report a structured reading for each image, in the same order they were shown.",
        "input_schema": {
            "type": "object",
            "required": ["readings"],
            "properties": {
                "readings": {
                    "type": "array",
                    "minItems": n_images,
                    "maxItems": n_images,
                    "description": f"Exactly {n_images} reading(s), one per image, in the same order.",
                    "items": {
                        "type": "object",
                        "required": ["format", "text_on_image", "visual_content", "intent"],
                        "properties": {
                            "format": {
                                "type": "string",
                                "description": "What KIND of image is this — pick one: 'meme', 'photo', 'screenshot', 'gif', 'illustration', 'selfie', 'other'.",
                            },
                            "text_on_image": {
                                "type": "string",
                                "description": "All visible text on the image, transcribed VERBATIM (preserve typos, caps, line breaks). Empty string '' if no text.",
                            },
                            "visual_content": {
                                "type": "string",
                                "description": "What's actually depicted: subjects, expressions, scene, objects, action. 1-3 sentences. Faithful to what's there, no editorializing.",
                            },
                            "intent": {
                                "type": "string",
                                "description": "What the sender is conveying by sending this — the joke, the emotional point, the irony, the relatable thing. Capture WHY this image lands as a thing to send a friend, not just what it shows. 1-2 sentences.",
                            },
                            "source": {
                                "type": "string",
                                "description": "Visible attribution — subreddit watermark, signature, brand, app UI hint (e.g. 'Twitter screenshot'). Empty string '' if none visible.",
                            },
                        },
                    },
                }
            },
        },
    }


_READING_SYSTEM_PROMPT = (
    "You're producing a structured reading of images that one friend sent another in chat. "
    "The receiver's reply will be voiced by a different model that won't see the image — "
    "it will only see your reading. So you're the eyes for that voice. Be faithful and "
    "specific.\n\n"
    "Important:\n"
    "- text_on_image: VERBATIM transcription. If the joke is in the text, the wording matters.\n"
    "- visual_content: what's in the frame, not what it means. Subjects, expressions, scene.\n"
    "- intent: WHY this image is something a friend would send. The joke, the relatable beat, "
    "the irony, the sympathy gesture. This is the field the voice model will react to most, so "
    "capture flavor — don't write 'a humorous image about being tired'; write 'the meme is the "
    "self-aware admission that her brain has fully clocked out, sent as the kind of relatable "
    "self-roast you forward when you've given up.'\n"
    "- source: subreddit watermark / signature / app UI / etc., if visible.\n"
    "- Keep the array length exactly equal to the number of images shown, in order."
)


def read_images(
    api_key: str,
    image_blocks: list[dict],
) -> tuple[list[dict], dict]:
    """Run the vision-reading pass over a list of Anthropic image content blocks.

    Returns (readings, usage):
      - readings: list of {format, text_on_image, visual_content, intent, source},
        one per input image. Empty list on failure.
      - usage: {input_tokens, cache_read_input_tokens, cache_creation_input_tokens,
        output_tokens, error?} for telemetry.
    """
    if not image_blocks:
        return [], {}
    if not api_key:
        logger.warning("image-reading: no anthropic key, skipping")
        return [], {"error": "missing anthropic key"}

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed; skipping image reading")
        return [], {"error": "anthropic SDK not installed"}

    # Build the user content: alternate "Image N:" labels and each image block,
    # so the model has anchors when reporting in order.
    content: list[dict] = []
    content.append({
        "type": "text",
        "text": (
            f"Read each of the following {len(image_blocks)} image(s) and report "
            f"using the report_images tool. Keep the array length exactly "
            f"{len(image_blocks)}, in the order shown."
        ),
    })
    for i, block in enumerate(image_blocks, 1):
        content.append({"type": "text", "text": f"--- Image {i}:"})
        content.append(block)

    tool = _read_images_tool(len(image_blocks))

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=IMAGE_READING_MODEL,
            max_tokens=IMAGE_READING_MAX_TOKENS,
            system=_READING_SYSTEM_PROMPT,
            tools=[tool],
            tool_choice={"type": "tool", "name": "report_images"},
            messages=[{"role": "user", "content": content}],
            timeout=IMAGE_READING_TIMEOUT_S,
        )
    except Exception as e:
        logger.warning(f"image-reading vision call failed: {type(e).__name__}: {e}")
        return [], {"error": f"{type(e).__name__}: {e}"}

    # Extract usage
    usage_dict: dict = {}
    try:
        u = msg.usage
        usage_dict = {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
        }
    except Exception:
        pass

    # Extract the report_images tool_use payload
    readings: list[dict] = []
    try:
        for block in (msg.content or []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "report_images":
                inp = block.input or {}
                rs = inp.get("readings")
                if isinstance(rs, list):
                    for r in rs:
                        if not isinstance(r, dict):
                            continue
                        readings.append({
                            "format": str(r.get("format") or "")[:40],
                            "text_on_image": str(r.get("text_on_image") or "")[:1500],
                            "visual_content": str(r.get("visual_content") or "")[:600],
                            "intent": str(r.get("intent") or "")[:600],
                            "source": str(r.get("source") or "")[:120],
                        })
                break
    except Exception as e:
        logger.warning(f"image-reading parse failed: {e}")

    if not readings:
        return [], {**usage_dict, "error": "no readings extracted from response"}

    # Pad/trim if the model gave wrong count (defensive)
    if len(readings) != len(image_blocks):
        logger.warning(
            f"image-reading: expected {len(image_blocks)} readings, got {len(readings)}"
        )
        # Take min length so each reading still corresponds to a real input image
        readings = readings[:len(image_blocks)]

    return readings, usage_dict


def compute_reading_cost(usage: dict) -> float:
    """Cost in dollars given a usage dict. Pricing depends on the model in use.

    Defaults to Opus 4.5 pricing ($15/M input, $1.50/M cache-read, $18.75/M
    cache-write, $75/M output). If the user overrode IMAGE_READING_MODEL,
    actual cost may differ — this is the headline number for the default.
    """
    if not isinstance(usage, dict):
        return 0.0
    model = IMAGE_READING_MODEL.lower()
    # Opus 4.5 default
    in_rate, cr_rate, cw_rate, out_rate = 15.0, 1.5, 18.75, 75.0
    if "sonnet" in model:
        in_rate, cr_rate, cw_rate, out_rate = 3.0, 0.3, 3.75, 15.0
    elif "haiku" in model:
        in_rate, cr_rate, cw_rate, out_rate = 1.0, 0.1, 1.25, 5.0
    return (
        (usage.get("input_tokens", 0) or 0) * in_rate
        + (usage.get("cache_read_input_tokens", 0) or 0) * cr_rate
        + (usage.get("cache_creation_input_tokens", 0) or 0) * cw_rate
        + (usage.get("output_tokens", 0) or 0) * out_rate
    ) / 1_000_000


def format_readings_for_injection(readings: list[dict]) -> str:
    """Format the structured readings into the [IMAGES] injection block."""
    if not readings:
        return ""
    lines = ["[IMAGES THE USER JUST SENT YOU — what's actually in them]"]
    for i, r in enumerate(readings, 1):
        prefix = f"Image {i}" if len(readings) > 1 else "Image"
        lines.append(f"- {prefix} ({r.get('format') or 'image'}):")
        text = r.get("text_on_image") or ""
        if text:
            lines.append(f"  text on image: {text!r}")
        visual = r.get("visual_content") or ""
        if visual:
            lines.append(f"  visual: {visual}")
        intent = r.get("intent") or ""
        if intent:
            lines.append(f"  intent: {intent}")
        source = r.get("source") or ""
        if source:
            lines.append(f"  source: {source}")
    lines.append(
        "\n(React to what's actually there — the visual + the intent + the text. Don't "
        "describe the image back to them; respond like you saw it. If it's a meme/joke, "
        "react TO the joke, not to the fact that they sent a meme.)"
    )
    return "\n".join(lines)
