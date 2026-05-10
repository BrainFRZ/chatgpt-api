"""Tests for the InlineThinkingFilter — verifies that <thinking>...</thinking>
XML tags are correctly split out of streaming content text, including across
chunk boundaries (which is the whole reason this is stateful).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inline_thinking_filter import (  # noqa: E402
    DEFAULT_THINKING_TAGS,
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


# ── Multi-tag behavior ─────────────────────────────────────────────────────

class TestMultiTag:
    def test_default_tags_include_search_quality(self):
        # The default config catches the Opus 3 search-related leaks
        assert "thinking" in DEFAULT_THINKING_TAGS
        assert "search_quality_reflection" in DEFAULT_THINKING_TAGS
        assert "search_quality_score" in DEFAULT_THINKING_TAGS

    def test_search_quality_reflection_stripped(self):
        f = InlineThinkingFilter()
        c, t = f.feed(
            "<search_quality_reflection>good results</search_quality_reflection>"
            "lmao here's the answer"
        )
        assert c == "lmao here's the answer"
        assert t == "good results"

    def test_search_quality_score_stripped(self):
        f = InlineThinkingFilter()
        c, t = f.feed(
            "<search_quality_score>4</search_quality_score>"
            "actually informative"
        )
        assert c == "actually informative"
        assert t == "4"

    def test_multiple_different_tags_in_sequence(self):
        f = InlineThinkingFilter()
        c, t = f.feed(
            "<thinking>let me think</thinking>"
            "<search_quality_reflection>solid sources</search_quality_reflection>"
            "<search_quality_score>4</search_quality_score>"
            "\n\nokay so here's the deal"
        )
        assert c == "\n\nokay so here's the deal"
        assert "let me think" in t
        assert "solid sources" in t
        assert "4" in t

    def test_real_opus3_search_trace(self):
        """The exact pattern Opus 3 emitted in the production search test —
        thinking block first, then search_quality_reflection + score, then
        the visible reply."""
        f = InlineThinkingFilter()
        chunks = [
            "<thinking>\nTo answer the question I'd search the web.\n</thinking>",
            "\n\n<search_quality_reflection>\nThe results look ",
            "comprehensive enough.\n</search_quality_reflection>",
            "\n<search_quality_score>4</search_quality_score>",
            "\n\nlmao of course you don't trust your weather app",
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

        full_content = "".join(content_total)
        full_thinking = "".join(thinking_total)
        assert "lmao of course" in full_content
        # No tag remnants in visible content
        assert "<thinking>" not in full_content
        assert "<search_quality" not in full_content
        # Reasoning captured all three blocks
        assert "search the web" in full_thinking
        assert "comprehensive" in full_thinking
        assert "4" in full_thinking

    def test_custom_tag_list(self):
        f = InlineThinkingFilter(tag_names=["scratchpad"])
        c, t = f.feed("<scratchpad>working</scratchpad>visible")
        assert c == "visible"
        assert t == "working"
        # Default tags are NOT applied when custom list given
        f2 = InlineThinkingFilter(tag_names=["scratchpad"])
        c2, t2 = f2.feed("<thinking>this should NOT be stripped</thinking>visible")
        assert "<thinking>" in c2
        assert "visible" in c2

    def test_partial_open_held_for_any_tag(self):
        # "<search" could be the start of "<search_quality_reflection>" so it's held
        f = InlineThinkingFilter()
        c, t = f.feed("intro <search")
        assert c == "intro "
        assert t == ""
        c2, t2 = f.feed("_quality_reflection>x</search_quality_reflection>tail")
        assert c2 == "tail"
        assert t2 == "x"


# ── Reply-wrap (narration) mode ────────────────────────────────────────────

class TestNarrationMode:
    def test_basic_reply_extraction(self):
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("<reply>hello world</reply>")
        assert c == "hello world"
        assert t == ""

    def test_thinking_before_reply_routed_to_thinking(self):
        # Pre-narration content is now BUFFERED until the first <reply>
        # arrives — at which point it flushes as thinking. End result
        # across the full stream is the same: pre-tag content → thinking.
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("let me think about this<reply>actual response</reply>")
        # Final flush to ensure no leftover
        c2, t2 = f.flush()
        assert (c + c2) == "actual response"
        assert (t + t2) == "let me think about this"

    def test_text_after_reply_also_routed_to_thinking(self):
        # After the reply tag is seen at least once, post-narration content
        # streams live as thinking (no buffering needed)
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("<reply>visible</reply>some afterthought")
        assert c == "visible"
        assert t == "some afterthought"

    def test_xml_tags_outside_reply_treated_as_thinking(self):
        # Whatever XML pattern the model emits outside <reply>, it's all reasoning.
        # With buffering, the pre-<reply> content all flushes as thinking once
        # <reply> arrives.
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed(
            "<thinking>step 1</thinking>"
            "<search_quality_reflection>good</search_quality_reflection>"
            "<some_random_tag>whatever</some_random_tag>"
            "<reply>the actual reply</reply>"
        )
        c2, t2 = f.flush()
        full_c = c + c2
        full_t = t + t2
        assert full_c == "the actual reply"
        assert "step 1" in full_t
        assert "good" in full_t
        assert "whatever" in full_t
        assert "<thinking>" in full_t

    def test_chunk_split_across_reply_open_tag(self):
        # Pre-tag content is now BUFFERED (not emitted) until first <reply>.
        # On the chunk where <reply> finally arrives, the buffer flushes.
        f = InlineThinkingFilter(narration_tag="reply")
        c1, t1 = f.feed("planning... <rep")
        # "planning... " is buffered, NOT emitted as thinking yet
        assert c1 == ""
        assert t1 == ""
        c2, t2 = f.feed("ly>visible</reply>more thinking")
        # Now the buffer flushes as thinking, and post-reply content streams live
        assert c2 == "visible"
        assert t2 == "planning... more thinking"

    def test_chunk_split_across_reply_close_tag(self):
        f = InlineThinkingFilter(narration_tag="reply")
        c1, t1 = f.feed("<reply>part one and part two</rep")
        assert c1 == "part one and part two"
        assert t1 == ""
        c2, t2 = f.feed("ly>after thoughts")
        assert c2 == ""
        assert t2 == "after thoughts"

    def test_no_reply_tag_at_all_falls_back_to_content(self):
        # FALLBACK behavior: if the model never emits <reply> tags at all,
        # the entire response is treated as the message rather than silently
        # routed to reasoning. Better to show the content (even if it bleeds
        # what would have been reasoning) than to show an empty chat bubble.
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("the model just wrote a reply with no wrap tags")
        c_f, t_f = f.flush()
        assert (c + c_f) == "the model just wrote a reply with no wrap tags"
        assert (t + t_f) == ""

    def test_no_reply_tag_with_other_xml_still_falls_back(self):
        # If the model emitted <thinking> but no <reply>, fallback still kicks
        # in — all content (including the thinking markers) goes to message.
        # Acceptable failure mode — the tags will look weird but the user
        # at least sees the actual reply text.
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("<thinking>just thinking</thinking>plain reply text")
        c_f, t_f = f.flush()
        full = c + c_f
        assert "plain reply text" in full
        assert (t + t_f) == ""

    def test_pre_tag_content_buffered_until_reply_arrives(self):
        # Real-time streaming behavior: pre-tag thinking content is held in
        # the buffer (not yielded) until <reply> is seen. This lets us fall
        # back to "everything is content" if <reply> never comes.
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("first thoughts ")
        # No emission yet — buffered
        assert c == ""
        assert t == ""
        c2, t2 = f.feed("more thinking ")
        assert c2 == ""
        assert t2 == ""
        c3, t3 = f.feed("<reply>actual response")
        # Now buffer flushes as thinking, "<reply>" stripped
        assert c3 == "actual response"
        assert t3 == "first thoughts more thinking "

    def test_flush_inside_reply_emits_as_content(self):
        f = InlineThinkingFilter(narration_tag="reply")
        c, t = f.feed("<reply>truncated mid-reply")
        # Stream cuts off without close — flush emits as content
        c2, t2 = f.flush()
        assert c == "truncated mid-reply"
        assert (c + c2) == "truncated mid-reply"

    def test_in_thinking_property_inverted_for_narration_mode(self):
        f = InlineThinkingFilter(narration_tag="reply")
        # Initially outside reply → in_thinking should be True
        assert f.in_thinking is True
        f.feed("<reply>")
        # Now inside reply → in_thinking should be False
        assert f.in_thinking is False
        f.feed("</reply>")
        # Outside again
        assert f.in_thinking is True

    def test_realistic_opus3_with_wrap(self):
        f = InlineThinkingFilter(narration_tag="reply")
        chunks = [
            "<thinking>\nThe user sent a meme. ",
            "I should react to the joke.\n</thinking>\n\n",
            "<search_quality_reflection>\nN/A\n</search_quality_reflection>\n",
            "<reply>\nlmao that's exactly my whole day\n",
            "i need a vacation from humans\n</reply>",
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
        full_content = "".join(content_total).strip()
        full_thinking = "".join(thinking_total)
        assert "lmao that's exactly my whole day" in full_content
        assert "i need a vacation from humans" in full_content
        # No tag markers leaked into content
        assert "<reply>" not in full_content
        assert "<thinking>" not in full_content
        # Reasoning captured the chain-of-thought
        assert "user sent a meme" in full_thinking

    def test_realistic_opus3_forgot_to_wrap_falls_back(self):
        # The exact failure mode from the busy-state test: model gets
        # absorbed in dramatic narration and never emits <reply> tags.
        # Fallback ensures the user sees the message anyway.
        f = InlineThinkingFilter(narration_tag="reply")
        chunks = [
            "*phone buzzes repeatedly* ",
            "shae. hey. what? slow down. ",
            "i was asleep. kait is what?",
        ]
        content_total = []
        for ch in chunks:
            c, t = f.feed(ch)
            content_total.append(c)
        c_f, t_f = f.flush()
        content_total.append(c_f)
        full_content = "".join(content_total)
        # Without the fallback, full_content would be "" and the user would
        # see an empty chat. With fallback, the content shows up.
        assert "shae. hey. what?" in full_content
        assert "i was asleep" in full_content


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
