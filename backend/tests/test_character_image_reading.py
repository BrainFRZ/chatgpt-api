"""Unit tests for character_image_reading — tool schema shape, formatter,
graceful failure modes, cost computation. No real Anthropic calls.
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _tool_use_block(name: str, input_dict: dict):
    """Build a fake tool_use content block. SimpleNamespace because MagicMock
    reserves the `name` attribute for its own repr — setting it via the
    constructor doesn't behave like a normal attribute set, and getattr returns
    the mock's auto-name. SimpleNamespace's name attribute behaves normally.
    """
    return SimpleNamespace(type="tool_use", name=name, input=input_dict)


def _text_block(text: str):
    return SimpleNamespace(type="text", text=text)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import character_image_reading as cir  # noqa: E402


# ── _read_images_tool schema ───────────────────────────────────────────────

class TestToolSchema:
    def test_required_fields(self):
        tool = cir._read_images_tool(3)
        assert tool["name"] == "report_images"
        s = tool["input_schema"]
        assert s["required"] == ["readings"]
        rs = s["properties"]["readings"]
        assert rs["type"] == "array"
        # Bound array length to N images
        assert rs["minItems"] == 3
        assert rs["maxItems"] == 3
        item_required = rs["items"]["required"]
        for f in ("format", "text_on_image", "visual_content", "intent"):
            assert f in item_required

    def test_array_bounds_track_n_images(self):
        for n in (1, 2, 5, 8):
            tool = cir._read_images_tool(n)
            rs = tool["input_schema"]["properties"]["readings"]
            assert rs["minItems"] == n
            assert rs["maxItems"] == n


# ── format_readings_for_injection ──────────────────────────────────────────

class TestFormatReadings:
    def test_empty_returns_empty_string(self):
        assert cir.format_readings_for_injection([]) == ""

    def test_none_returns_empty(self):
        # be defensive
        assert cir.format_readings_for_injection(None) == ""  # type: ignore

    def test_single_image_formatted(self):
        readings = [{
            "format": "meme",
            "text_on_image": "this is fine",
            "visual_content": "dog in burning room with coffee",
            "intent": "the meme is the self-aware admission that everything is going wrong",
            "source": "r/memes",
        }]
        out = cir.format_readings_for_injection(readings)
        assert "[IMAGES THE USER JUST SENT YOU" in out
        assert "meme" in out
        assert "this is fine" in out
        assert "dog in burning room" in out
        assert "self-aware admission" in out
        assert "r/memes" in out

    def test_multiple_images_numbered(self):
        readings = [
            {"format": "meme", "text_on_image": "tired", "visual_content": "cat", "intent": "relatable", "source": ""},
            {"format": "photo", "text_on_image": "", "visual_content": "sunset", "intent": "look at this", "source": ""},
        ]
        out = cir.format_readings_for_injection(readings)
        assert "Image 1" in out
        assert "Image 2" in out

    def test_optional_fields_omitted_cleanly(self):
        readings = [{"format": "photo", "text_on_image": "", "visual_content": "scene", "intent": "fyi", "source": ""}]
        out = cir.format_readings_for_injection(readings)
        # text/source are empty so those bullet lines should NOT appear
        assert "text on image:" not in out
        assert "source:" not in out
        assert "visual: scene" in out
        assert "intent: fyi" in out

    def test_react_to_not_describe_instruction(self):
        readings = [{"format": "meme", "text_on_image": "x", "visual_content": "y", "intent": "z", "source": ""}]
        out = cir.format_readings_for_injection(readings)
        # The instruction at the bottom matters — voice should react, not narrate
        assert "react" in out.lower()
        assert "don't describe the image back" in out.lower()


# ── compute_reading_cost ───────────────────────────────────────────────────

class TestComputeReadingCost:
    def test_zero_usage_zero_cost(self):
        assert cir.compute_reading_cost({}) == 0.0
        assert cir.compute_reading_cost({"input_tokens": 0, "output_tokens": 0}) == 0.0

    def test_default_opus_pricing(self):
        # 1M input, 1M output → $15 + $75 = $90 (Opus 4.5 default)
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        assert abs(cir.compute_reading_cost(usage) - 90.0) < 0.01

    def test_cache_read_is_cheaper(self):
        a = cir.compute_reading_cost({"input_tokens": 100_000, "output_tokens": 0,
                                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        b = cir.compute_reading_cost({"input_tokens": 0, "output_tokens": 0,
                                       "cache_read_input_tokens": 100_000, "cache_creation_input_tokens": 0})
        assert a > b > 0

    def test_non_dict_returns_zero(self):
        assert cir.compute_reading_cost(None) == 0.0
        assert cir.compute_reading_cost("not a dict") == 0.0


# ── read_images graceful failure ───────────────────────────────────────────

class TestReadImagesFailures:
    def test_no_image_blocks_returns_empty(self):
        readings, usage = cir.read_images("any-key", [])
        assert readings == []
        assert usage == {}

    def test_no_api_key_returns_empty_with_error(self):
        readings, usage = cir.read_images("", [{"type": "image", "source": {}}])
        assert readings == []
        assert "error" in usage

    def test_anthropic_exception_returns_empty_with_error(self):
        # Mock Anthropic to raise
        fake_client = MagicMock()
        fake_client.messages.create.side_effect = RuntimeError("BadRequestError")
        with patch("anthropic.Anthropic", return_value=fake_client):
            readings, usage = cir.read_images(
                "k", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}}]
            )
        assert readings == []
        assert "error" in usage
        assert "RuntimeError" in usage["error"] or "BadRequestError" in usage["error"]

    def test_anthropic_response_with_no_tool_use_returns_empty(self):
        # Response with only a text block, no tool_use
        fake_msg = MagicMock()
        fake_msg.content = [_text_block("I refuse")]
        fake_msg.usage = MagicMock(input_tokens=10, output_tokens=5,
                                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg
        with patch("anthropic.Anthropic", return_value=fake_client):
            readings, usage = cir.read_images(
                "k", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}}]
            )
        assert readings == []
        # Usage still recorded even though parse failed
        assert usage.get("input_tokens") == 10
        assert "error" in usage  # parse-failure error


# ── successful read_images flow ────────────────────────────────────────────

class TestReadImagesSuccess:
    def test_extracts_readings_from_tool_use(self):
        tool_use = _tool_use_block("report_images", {
            "readings": [{
                "format": "meme",
                "text_on_image": "this is fine",
                "visual_content": "dog in burning room",
                "intent": "self-aware joke about denial",
                "source": "r/memes",
            }]
        })
        fake_msg = MagicMock()
        fake_msg.content = [tool_use]
        fake_msg.usage = MagicMock(input_tokens=2000, output_tokens=200,
                                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg
        with patch("anthropic.Anthropic", return_value=fake_client):
            readings, usage = cir.read_images(
                "k", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}}]
            )
        assert len(readings) == 1
        assert readings[0]["format"] == "meme"
        assert readings[0]["text_on_image"] == "this is fine"
        assert readings[0]["intent"] == "self-aware joke about denial"
        assert usage["input_tokens"] == 2000
        assert usage["output_tokens"] == 200

    def test_truncates_overlong_fields(self):
        tool_use = _tool_use_block("report_images", {
            "readings": [{
                "format": "x" * 100,
                "text_on_image": "y" * 5000,
                "visual_content": "z" * 5000,
                "intent": "w" * 5000,
                "source": "s" * 500,
            }]
        })
        fake_msg = MagicMock()
        fake_msg.content = [tool_use]
        fake_msg.usage = MagicMock(input_tokens=10, output_tokens=10,
                                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg
        with patch("anthropic.Anthropic", return_value=fake_client):
            readings, usage = cir.read_images("k", [{"type": "image", "source": {}}])
        r = readings[0]
        assert len(r["format"]) <= 40
        assert len(r["text_on_image"]) <= 1500
        assert len(r["visual_content"]) <= 600
        assert len(r["intent"]) <= 600
        assert len(r["source"]) <= 120

    def test_mismatched_count_truncates(self):
        # Model returned 3 readings for 2 images — we trim to 2
        tool_use = _tool_use_block("report_images", {
            "readings": [
                {"format": "meme", "text_on_image": "", "visual_content": "a", "intent": "a"},
                {"format": "meme", "text_on_image": "", "visual_content": "b", "intent": "b"},
                {"format": "meme", "text_on_image": "", "visual_content": "c", "intent": "c"},
            ]
        })
        fake_msg = MagicMock()
        fake_msg.content = [tool_use]
        fake_msg.usage = MagicMock(input_tokens=10, output_tokens=10,
                                    cache_read_input_tokens=0, cache_creation_input_tokens=0)
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_msg
        with patch("anthropic.Anthropic", return_value=fake_client):
            readings, usage = cir.read_images(
                "k",
                [{"type": "image", "source": {}}, {"type": "image", "source": {}}],
            )
        assert len(readings) == 2
        assert readings[0]["visual_content"] == "a"
        assert readings[1]["visual_content"] == "b"
