"""Streaming filter that splits inline reasoning XML tags out of content text
and routes them to a separate thinking stream.

Why this exists: extended thinking (the structured thinking-block API) is a
4.x-and-later feature. Opus 3, our voice/correspondence model, doesn't support
it — when given tools and asked to reason, it emits chain-of-thought via XML
tags inline in regular text. Without filtering, those tags leak into what the
user sees as the character's reply.

Two modes:

1. **Reply-wrap mode (recommended for Opus 3)** — model is instructed by the
   contract to wrap its actual visible reply in <reply>...</reply> tags.
   EVERYTHING outside those tags is treated as reasoning. The wrap tags
   themselves are stripped before the user sees the content. This is robust
   to any reasoning tag pattern the model might invent — known or unknown,
   we don't care, only what's inside <reply> is visible.

   Activate with: InlineThinkingFilter(narration_tag="reply")

2. **Denylist mode (legacy fallback)** — content is visible by default;
   specific tag pairs are routed to thinking. Used when we can't or don't
   instruct the model to wrap its reply.

   Activate with: InlineThinkingFilter()  # uses DEFAULT_THINKING_TAGS

Stateful because tags can split across stream chunks — `"<rep"` may arrive in
one chunk and `"ly>"` in the next. The filter holds back trailing characters
that could be a partial tag start until the next chunk arrives or the stream
ends.
"""
from __future__ import annotations

from typing import Optional


# Default tag names whose content should be treated as thinking. Each name X
# matches the pair <X>...</X>. Add new ones here as we identify other Opus 3
# reasoning markers that leak.
DEFAULT_THINKING_TAGS: tuple[str, ...] = (
    "thinking",
    "search_quality_reflection",
    "search_quality_score",
)


class InlineThinkingFilter:
    def __init__(
        self,
        tag_names: Optional[tuple[str, ...] | list[str]] = None,
        *,
        narration_tag: Optional[str] = None,
    ) -> None:
        """
        narration_tag: when set, switches to reply-wrap mode. The single named
            tag pair (<X>...</X>) wraps the visible reply; everything else is
            reasoning. Tag markers themselves are stripped from the output.

        tag_names: when narration_tag is None, the legacy denylist of tags
            whose contents go to reasoning. Defaults to DEFAULT_THINKING_TAGS.
        """
        self._narration_mode = narration_tag is not None
        if self._narration_mode:
            self._narration_open = f"<{narration_tag}>"
            self._narration_close = f"</{narration_tag}>"
            self._opens = (self._narration_open,)
            self._closes = (self._narration_close,)
        else:
            names = tuple(tag_names) if tag_names is not None else DEFAULT_THINKING_TAGS
            self._tags = names
            self._opens = tuple(f"<{n}>" for n in names)
            self._closes = tuple(f"</{n}>" for n in names)
        self._in_tag_index = -1   # -1 = not inside any tag (denylist mode)
        # Narration mode: True when currently inside <narration_tag>...</narration_tag>
        self._in_narration = False
        self._buffer = ""
        # Narration-mode fallback: if the model never emits the narration tag
        # at all, the entire response should be treated as the message (not
        # silently routed to reasoning). We buffer outside-tag content until
        # we see the first narration open tag — at that point we know
        # narration mode is real, the buffer flushes as thinking, and live
        # mode kicks in. If end-of-stream hits first without the tag, the
        # buffer flushes as CONTENT instead. Streaming consequence: thinking
        # output is delayed slightly until the first <reply> arrives, but
        # the model never gets to disappear because it forgot to wrap.
        self._pre_tag_buffer = ""
        self._saw_narration_tag = False

    @staticmethod
    def _safe_emit_end(s: str, tag: str) -> int:
        """Return the index up to which `s` is safe to emit without risking
        being a partial `tag` match. Anything after this index is held back.
        """
        max_n = min(len(tag) - 1, len(s))
        for n in range(max_n, 0, -1):
            if s.endswith(tag[:n]):
                return len(s) - n
        return len(s)

    def _safe_emit_end_multi(self, s: str) -> int:
        """Across all configured open tags, the safest index to emit through.
        We hold back the longest possible partial match across all tags.
        """
        min_safe = len(s)
        for tag in self._opens:
            safe = self._safe_emit_end(s, tag)
            if safe < min_safe:
                min_safe = safe
        return min_safe

    def _find_earliest_open(self, s: str) -> tuple[int, int]:
        """Return (index, tag_index) for the earliest open tag found, or
        (-1, -1) if none found.
        """
        earliest_idx = -1
        earliest_tag = -1
        for i, tag in enumerate(self._opens):
            idx = s.find(tag)
            if idx >= 0 and (earliest_idx == -1 or idx < earliest_idx):
                earliest_idx = idx
                earliest_tag = i
        return earliest_idx, earliest_tag

    def feed(self, chunk: str) -> tuple[str, str]:
        """Feed a chunk of streamed text. Returns (content, thinking) — either
        may be empty. Held-back partial-tag tail is retained internally.
        """
        if not chunk:
            return "", ""
        self._buffer += chunk
        if self._narration_mode:
            return self._feed_narration_mode()
        return self._feed_denylist_mode()

    def _feed_narration_mode(self) -> tuple[str, str]:
        """Reply-wrap mode: only content inside <narration_tag>...</narration_tag>
        is visible. Fallback: if the narration tag is NEVER seen by end of
        stream, emit accumulated outside-tag content as the message.

        Pre-tag content is buffered (not emitted) until either:
        - first <narration_tag> arrives → buffer flushes as thinking, then
          the model is in real narration mode going forward
        - end of stream → flush() emits buffer as CONTENT (fallback path)
        """
        content_out: list[str] = []
        thinking_out: list[str] = []
        while self._buffer:
            if self._in_narration:
                idx = self._buffer.find(self._narration_close)
                if idx >= 0:
                    content_out.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(self._narration_close):]
                    self._in_narration = False
                else:
                    safe = self._safe_emit_end(self._buffer, self._narration_close)
                    if safe > 0:
                        content_out.append(self._buffer[:safe])
                        self._buffer = self._buffer[safe:]
                    break
            else:
                idx = self._buffer.find(self._narration_open)
                if idx >= 0:
                    # Pre-tag content for THIS chunk
                    pre_tag = self._buffer[:idx]
                    if not self._saw_narration_tag:
                        # First narration open ever — flush pre-tag buffer as
                        # thinking (now we know we're in real narration mode)
                        if self._pre_tag_buffer:
                            thinking_out.append(self._pre_tag_buffer)
                            self._pre_tag_buffer = ""
                        if pre_tag:
                            thinking_out.append(pre_tag)
                        self._saw_narration_tag = True
                    else:
                        if pre_tag:
                            thinking_out.append(pre_tag)
                    self._buffer = self._buffer[idx + len(self._narration_open):]
                    self._in_narration = True
                else:
                    safe = self._safe_emit_end(self._buffer, self._narration_open)
                    if safe > 0:
                        chunk = self._buffer[:safe]
                        if self._saw_narration_tag:
                            # We've already seen the narration tag — outside-
                            # tag content is real reasoning, emit live
                            thinking_out.append(chunk)
                        else:
                            # No narration tag seen yet — buffer in case the
                            # model never wraps and we need fallback
                            self._pre_tag_buffer += chunk
                        self._buffer = self._buffer[safe:]
                    break
        return "".join(content_out), "".join(thinking_out)

    def _feed_denylist_mode(self) -> tuple[str, str]:
        content_out: list[str] = []
        thinking_out: list[str] = []
        while self._buffer:
            if self._in_tag_index >= 0:
                close = self._closes[self._in_tag_index]
                idx = self._buffer.find(close)
                if idx >= 0:
                    thinking_out.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(close):]
                    self._in_tag_index = -1
                else:
                    safe = self._safe_emit_end(self._buffer, close)
                    if safe > 0:
                        thinking_out.append(self._buffer[:safe])
                        self._buffer = self._buffer[safe:]
                    break
            else:
                idx, tag_index = self._find_earliest_open(self._buffer)
                if idx >= 0:
                    content_out.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(self._opens[tag_index]):]
                    self._in_tag_index = tag_index
                else:
                    safe = self._safe_emit_end_multi(self._buffer)
                    if safe > 0:
                        content_out.append(self._buffer[:safe])
                        self._buffer = self._buffer[safe:]
                    break
        return "".join(content_out), "".join(thinking_out)

    def flush(self) -> tuple[str, str]:
        """Drain any remaining buffered text after the stream ends. Routing
        depends on what state we ended in — see mode comments."""
        leftover = self._buffer
        self._buffer = ""
        if self._narration_mode:
            if self._in_narration:
                # Mid-reply truncation — leftover is content
                return (leftover, "")
            if self._saw_narration_tag:
                # Already saw narration tag, post-narration → leftover is thinking
                return ("", leftover)
            # Fallback: no <reply> tag was ever seen across the whole stream.
            # The model emitted bare text (probably with some <thinking>...
            # </thinking> blocks mixed in). Run the accumulated buffer through
            # denylist parsing so <thinking>/<search_quality_*>/etc. still get
            # routed to reasoning — without this we'd dump the whole thing
            # (thinking AND reply) into content.
            buffer_total = self._pre_tag_buffer + leftover
            self._pre_tag_buffer = ""
            denylist = InlineThinkingFilter()  # DEFAULT_THINKING_TAGS
            content, thinking = denylist.feed(buffer_total)
            tail_content, tail_thinking = denylist.flush()
            return (content + tail_content, thinking + tail_thinking)
        # Denylist mode unchanged
        if not leftover:
            return "", ""
        return ("", leftover) if self._in_tag_index >= 0 else (leftover, "")

    @property
    def in_thinking(self) -> bool:
        """True when currently inside a thinking-routed region (denylist mode)
        or OUTSIDE the narration block (narration mode)."""
        if self._narration_mode:
            return not self._in_narration
        return self._in_tag_index >= 0


# Models known to emit inline reasoning XML tags rather than native
# thinking_delta blocks. Add new ones here as we identify them.
_INLINE_THINKING_MODELS = (
    "claude-3-opus",            # internal id used by ProviderRegistry
    "claude-3-opus-20240229",   # Anthropic API name
)


def model_id_uses_inline_thinking(model_id: str) -> bool:
    if not isinstance(model_id, str):
        return False
    m = model_id.lower()
    return any(m == k or m.startswith(k + "-") for k in _INLINE_THINKING_MODELS)
