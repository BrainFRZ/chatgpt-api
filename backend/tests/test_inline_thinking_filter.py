"""Tests for the InlineThinkingFilter — verifies that <thinking>...</thinking>
XML tags are correctly split out of streaming content text, including across
chunk boundaries (which is the whole reason this is stateful).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inline_thinking_filter import (  # noqa: E402
    InlineThinkingFilter,
    model_id_uses_inline_thinking,
)


# ── Single-chunk basics ────────────────────────────────────────────────────

class TestSingleChunk:
    def test_no_tags_passes_through(self):
        f = InlineThinkingFilter()
        c, t = f.feed("hello world, no tags here")
        assert c == "hello world, no tags here"
        assert t == ""
        assert f.flush() == ("", "")

    def test_complete_thinking_block_split_out(self):
        f = InlineThinkingFilter()
        c, t = f.feed("before<thinking>private</thinking>after")
        assert c == "beforeafter"
        assert t == "private"
        assert f.flush() == ("", "")

    def test_thinking_only_no_visible(self):
        f = InlineThinkingFilter()
        c, t = f.feed("<thinking>hidden</thinking>")
        assert c == ""
        assert t == "hidden"

    def test_two_thinking_blocks_in_one_chunk(self):
        f = InlineThinkingFilter()
        c, t = f.feed("a<thinking>x</thinking>b<thinking>y</thinking>c")
        assert c == "abc"
        assert t == "xy"

    def test_empty_chunk(self):
        f = InlineThinkingFilter()
        c, t = f.feed("")
        assert c == ""
        assert t == ""


# ── Cross-chunk boundary handling ──────────────────────────────────────────

class TestChunkBoundaries:
    def test_open_tag_split_across_chunks(self):
        f = InlineThinkingFilter()
        c1, t1 = f.feed("hello <thi")
        # Should have emitted "hello " as content; held "<thi"
        assert c1 == "hello "
        assert t1 == ""
        c2, t2 = f.feed("nking>private</thinking>tail")
        assert c2 == "tail"
        assert t2 == "private"

    def test_close_tag_split_across_chunks(self):
        f = InlineThinkingFilter()
        c1, t1 = f.feed("a<thinking>private</thi")
        # We're inside thinking; "private" should be emitted but not "</thi"
        assert c1 == "a"
        assert t1 == "private"
        c2, t2 = f.feed("nking>tail")
        assert c2 == "tail"
        assert t2 == ""

    def test_one_char_at_a_time(self):
        f = InlineThinkingFilter()
        full = "a<thinking>hidden</thinking>b"
        content_total = []
        thinking_total = []
        for ch in full:
            c, t = f.feed(ch)
            content_total.append(c)
            thinking_total.append(t)
        c, t = f.flush()
        content_total.append(c)
        thinking_total.append(t)
        assert "".join(content_total) == "ab"
        assert "".join(thinking_total) == "hidden"

    def test_partial_tag_mismatch_emitted_immediately(self):
        # "<the" CAN'T extend to "<thinking>" (e != i at position 3), so the
        # filter is smart enough to emit it right away rather than hold it.
        f = InlineThinkingFilter()
        c1, t1 = f.feed("foo <the")
        assert c1 == "foo <the"
        assert t1 == ""
        c2, t2 = f.feed("ater is closed")
        assert c2 == "ater is closed"
        assert t2 == ""

    def test_real_partial_tag_held(self):
        # "<thi" IS a real prefix of "<thinking>", so it gets held back
        f = InlineThinkingFilter()
        c1, t1 = f.feed("foo <thi")
        assert c1 == "foo "
        assert t1 == ""
        # Then it could resolve to a real thinking tag
        c2, t2 = f.feed("nking>private</thinking>tail")
        assert c2 == "tail"
        assert t2 == "private"

    def test_held_partial_resolves_as_content_when_not_a_tag(self):
        # "<thi" held, then a non-matching char arrives → emit "<thi" + new content
        f = InlineThinkingFilter()
        c1, t1 = f.feed("foo <thi")
        assert c1 == "foo "
        c2, t2 = f.feed("ck book")  # "<thick book" — not a tag
        assert c2 == "<thick book"
        assert t2 == ""


# ── Flush behavior ─────────────────────────────────────────────────────────

class TestFlush:
    def test_flush_with_unclosed_thinking(self):
        f = InlineThinkingFilter()
        c, t = f.feed("a<thinking>oops no close")
        assert c == "a"
        # "oops no close" doesn't end in any prefix of </thinking>, so it should be emitted
        assert t == "oops no close"
        # nothing held back
        c, t = f.flush()
        assert c == ""
        assert t == ""

    def test_flush_with_held_tail(self):
        f = InlineThinkingFilter()
        c1, t1 = f.feed("hello <thi")
        # "<thi" held in buffer
        assert c1 == "hello "
        # Stream ends — flush should release the held chars as content
        c2, t2 = f.flush()
        assert c2 == "<thi"
        assert t2 == ""

    def test_flush_with_held_close_tag_partial_inside_thinking(self):
        f = InlineThinkingFilter()
        c, t = f.feed("a<thinking>x</thi")
        assert c == "a"
        assert t == "x"
        # Stream ends mid-close-tag; "</thi" should flush as thinking
        # (we were inside thinking, so leftover is thinking)
        c2, t2 = f.flush()
        assert c2 == ""
        assert t2 == "</thi"

    def test_flush_idempotent(self):
        f = InlineThinkingFilter()
        f.feed("plain text")
        f.flush()
        # Second flush should return empty
        assert f.flush() == ("", "")


# ── Realistic Opus-3-style trace ───────────────────────────────────────────

class TestRealisticTraces:
    def test_opus3_tool_use_preamble(self):
        """Mimics what we saw in production — Opus 3 emits a thinking block
        before deciding to call a tool, then writes the visible reply after."""
        f = InlineThinkingFilter()
        # Simulate streaming chunks
        chunks = [
            "<thinking>\nThe user sent ",
            "a meme. I should react ",
            "and maybe send one back.\n</thinking>",
            "\n\nlmao stop",
            ", that's exactly my whole day",
        ]
        content_total = []
        thinking_total = []
        for ch in chunks:
            c, t = f.feed(ch)
            content_total.append(c)
            thinking_total.append(t)
        c_f, t_f = f.flush()
        content_total.append(c_f)
        thinking_total.append(t_f)
        assert (
            "".join(content_total).strip()
            == "lmao stop, that's exactly my whole day"
        )
        assert "user sent a meme" in "".join(thinking_total)
        assert "react" in "".join(thinking_total)

    def test_multiple_thinking_blocks_with_visible_text_between(self):
        f = InlineThinkingFilter()
        c, t = f.feed("<thinking>first thought</thinking>visible<thinking>second</thinking>more")
        assert c == "visiblemore"
        assert t == "first thoughtsecond"


# ── model_id_uses_inline_thinking ──────────────────────────────────────────

class TestModelIdCheck:
    def test_internal_id(self):
        assert model_id_uses_inline_thinking("claude-3-opus") is True

    def test_api_id(self):
        assert model_id_uses_inline_thinking("claude-3-opus-20240229") is True

    def test_case_insensitive(self):
        assert model_id_uses_inline_thinking("CLAUDE-3-OPUS") is True

    def test_modern_opus_does_not_match(self):
        assert model_id_uses_inline_thinking("claude-opus-4-5") is False
        assert model_id_uses_inline_thinking("claude-opus-4-7") is False

    def test_sonnet_does_not_match(self):
        assert model_id_uses_inline_thinking("claude-sonnet-4-6") is False

    def test_gpt_does_not_match(self):
        assert model_id_uses_inline_thinking("gpt-5.4") is False

    def test_non_string_does_not_match(self):
        assert model_id_uses_inline_thinking(None) is False  # type: ignore
        assert model_id_uses_inline_thinking(42) is False  # type: ignore
