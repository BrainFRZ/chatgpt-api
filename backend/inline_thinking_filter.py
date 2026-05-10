"""Streaming filter that splits inline <thinking>...</thinking> XML tags out
of content text and routes them to a separate thinking stream.

Why this exists: extended thinking (the structured thinking-block API) is a
4.x-and-later feature. Opus 3, our voice/correspondence model, doesn't support
it — when given tools and asked to reason about whether/how to call them, it
emits chain-of-thought via XML tags inline in regular text. Without filtering,
those tags leak into what the user sees as the character's reply ("<thinking>I
should send a meme...</thinking>lol that's so true").

This filter runs across the streamed content_delta events, splitting each chunk
into content + thinking pieces, so the frontend gets:
  - content events containing only the visible reply
  - thinking events containing the chain-of-thought, displayed in the same
    Reasoning UI used for native thinking_delta from other models

Stateful because tags can split across stream chunks — `"<thi"` may arrive in
one chunk and `"nking>"` in the next. The filter holds back any trailing
characters that could be a partial tag start until the next chunk arrives or
the stream ends.
"""
from __future__ import annotations


class InlineThinkingFilter:
    OPEN_TAG = "<thinking>"
    CLOSE_TAG = "</thinking>"

    def __init__(self) -> None:
        self.in_thinking = False
        self._buffer = ""

    @staticmethod
    def _safe_emit_end(s: str, tag: str) -> int:
        """Return the index up to which `s` is safe to emit without risking
        being a partial `tag` match. Anything after this index is held back.

        E.g. if s='hello <thi' and tag='<thinking>', the 'hello ' part is safe
        to emit (returns index 6) and '<thi' is held to see if it completes.
        """
        # Check decreasing-length suffixes of s against prefixes of tag
        max_n = min(len(tag) - 1, len(s))
        for n in range(max_n, 0, -1):
            if s.endswith(tag[:n]):
                return len(s) - n
        return len(s)

    def feed(self, chunk: str) -> tuple[str, str]:
        """Feed a chunk of streamed text. Returns (content, thinking) — either
        may be empty. Held-back partial-tag tail is retained internally.
        """
        if not chunk:
            return "", ""
        self._buffer += chunk
        content_out: list[str] = []
        thinking_out: list[str] = []

        while self._buffer:
            if self.in_thinking:
                idx = self._buffer.find(self.CLOSE_TAG)
                if idx >= 0:
                    thinking_out.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(self.CLOSE_TAG):]
                    self.in_thinking = False
                else:
                    safe = self._safe_emit_end(self._buffer, self.CLOSE_TAG)
                    if safe > 0:
                        thinking_out.append(self._buffer[:safe])
                        self._buffer = self._buffer[safe:]
                    break
            else:
                idx = self._buffer.find(self.OPEN_TAG)
                if idx >= 0:
                    content_out.append(self._buffer[:idx])
                    self._buffer = self._buffer[idx + len(self.OPEN_TAG):]
                    self.in_thinking = True
                else:
                    safe = self._safe_emit_end(self._buffer, self.OPEN_TAG)
                    if safe > 0:
                        content_out.append(self._buffer[:safe])
                        self._buffer = self._buffer[safe:]
                    break

        return "".join(content_out), "".join(thinking_out)

    def flush(self) -> tuple[str, str]:
        """Drain any remaining buffered text after the stream ends. If we end
        mid-thinking-block (model truncated or didn't close the tag), the rest
        gets reported as thinking. If we end mid-content, the rest is content.
        """
        if not self._buffer:
            return "", ""
        leftover = self._buffer
        self._buffer = ""
        if self.in_thinking:
            return "", leftover
        return leftover, ""

    def applies_to_model(self, model_id: str) -> bool:
        """Hint for callers: which model_ids should use this filter."""
        return model_id_uses_inline_thinking(model_id)


# Models known to emit inline <thinking> XML tags rather than native
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
