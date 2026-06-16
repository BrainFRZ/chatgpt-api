"""
Anthropic Claude Sonnet 4.5 provider implementation with extended thinking.
"""

import json
from typing import Any, Optional, Iterator
import anthropic

from . import ModelProvider, ParsedResponse, Pricing, ContextLimits, StreamEvent


class _NarrationFieldStreamer:
    """Streams the value of a top-level `narration` string field out of a
    JSON object as the JSON is fed in incrementally (partial_json deltas).

    Yields characters as they arrive so the frontend can render prose live
    while the tool input is still being built. Handles JSON string escapes
    (\\n, \\t, \\\", \\\\, \\/, \\b, \\f, \\uXXXX).

    States:
      before_field: scanning for the literal `"narration"` key
      seeking_quote: found the key; skipping `:` and whitespace until opening `"`
      in_value: emitting characters of the value
      escape: just saw `\\`, the next char determines the escape
      unicode: just saw `\\u`, accumulating 4 hex digits
      done: closing `"` reached; ignore further input
    """
    KEY_TOKEN = '"narration"'
    ESCAPE_MAP = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\',
                  '/': '/', 'b': '\b', 'f': '\f'}

    def __init__(self):
        self.buffer = ""
        self.state = "before_field"
        self.unicode_hex = ""

    def feed(self, partial_json: str) -> str:
        out = []
        for ch in partial_json:
            if self.state == "before_field":
                self.buffer += ch
                idx = self.buffer.find(self.KEY_TOKEN)
                if idx >= 0:
                    self.buffer = ""
                    self.state = "seeking_quote"
                elif len(self.buffer) > 4096:
                    # Cap buffer to avoid unbounded growth if narration never arrives
                    self.buffer = self.buffer[-1024:]
            elif self.state == "seeking_quote":
                if ch == '"':
                    self.state = "in_value"
                # otherwise it's `:` or whitespace; skip
            elif self.state == "in_value":
                if ch == '\\':
                    self.state = "escape"
                elif ch == '"':
                    self.state = "done"
                    break
                else:
                    out.append(ch)
            elif self.state == "escape":
                if ch == 'u':
                    self.state = "unicode"
                    self.unicode_hex = ""
                else:
                    out.append(self.ESCAPE_MAP.get(ch, ch))
                    self.state = "in_value"
            elif self.state == "unicode":
                self.unicode_hex += ch
                if len(self.unicode_hex) == 4:
                    try:
                        out.append(chr(int(self.unicode_hex, 16)))
                    except ValueError:
                        out.append("?")
                    self.unicode_hex = ""
                    self.state = "in_value"
            # done: drop remaining chars in this delta
        return "".join(out)


class AnthropicProvider(ModelProvider):
    """Provider for Claude Sonnet 4.5 with extended thinking."""

    MODEL_NAME = "claude-sonnet-4-5-20250929"
    THINKING_BUDGET = 10000
    MAX_TOKENS = 16000  # Max output tokens (must be > THINKING_BUDGET)

    # Beta headers for extended context, context management, and 1hr caching
    BETA_HEADERS = [
        "context-1m-2025-08-07",           # Enables 1M token context
        "context-management-2025-06-27",   # Clears old thought blocks from history
        "extended-cache-ttl-2025-04-11"    # Enables 1hr cache TTL
    ]

    @property
    def model_id(self) -> str:
        return "claude-sonnet-4.5"

    @property
    def display_name(self) -> str:
        return "Claude Sonnet 4.5"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=3.00,     # $/1M tokens (non-cached input)
            cache_write=6.00,    # $/1M tokens (1hr cache write, 2x base)
            cache_read=0.30,     # $/1M tokens (cache hit, 0.1x base)
            output=15.0,         # $/1M tokens
            reasoning=15.0       # $/1M tokens (thinking)
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(
            threshold=195_000,  # Tighter threshold with accurate counting
            target=160_000      # More context retained
        )

    def get_client(self, api_key: str) -> Any:
        """Create Anthropic client."""
        return anthropic.Anthropic(api_key=api_key)

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool,
        use_cache: bool = True
    ) -> dict:
        """
        Build Anthropic API request parameters.

        Cache breakpoint strategy (1hr TTL):
        - Place breakpoints: (1) after system, (2) after last assistant
        - This ensures cache hits on stable prefixes
        - When use_cache=False (async mode), all cache breakpoints are omitted
        """
        # Separate system message from conversation
        system_content = None
        conversation_messages = []

        for msg in messages:
            if msg["role"] == "system":
                # Only set system with cache_control if content is non-empty
                content = msg.get("content", "").strip()
                if content:
                    if use_cache:
                        system_content = [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral", "ttl": "1h"}
                            }
                        ]
                    else:
                        system_content = [
                            {
                                "type": "text",
                                "text": content
                            }
                        ]
            else:
                conversation_messages.append(msg)

        # Convert messages to Anthropic format and add cache breakpoints
        anthropic_messages = self._convert_messages_with_cache(conversation_messages, use_cache=use_cache)

        params = {
            "model": self.MODEL_NAME,
            "max_tokens": self.MAX_TOKENS,
            "messages": anthropic_messages,
            "thinking": {
                "type": "enabled",
                "budget_tokens": self.THINKING_BUDGET
            }
        }

        # Inject model identity - Claude models are trained before release, so don't know themselves
        model_identity = f"You are {self.display_name}, made by Anthropic."
        if system_content:
            system_content[0]["text"] = model_identity + "\n\n" + system_content[0]["text"]
            params["system"] = system_content
        else:
            params["system"] = [{"type": "text", "text": model_identity}]

        return params

    def _convert_messages_with_cache(self, messages: list[dict], use_cache: bool = True) -> list[dict]:
        """
        Convert messages to Anthropic format with cache breakpoints.

        Cache breakpoint strategy:
        - Breakpoint 1: After system message (handled in build_request)
        - Breakpoint 2: After last assistant message

        Why this works:
        - No content is injected into user messages, so the prefix up to
          the last assistant message is stable across turns
        - Future rounds match the cached prefix -> cache HIT
        - Only the new user message + response is a cache miss (unavoidable)

        When use_cache=False, all messages are plain format (no breakpoints).
        """
        result = []

        # Find indices of all assistant messages
        assistant_indices = [i for i, msg in enumerate(messages) if msg["role"] == "assistant"]

        # Last assistant index (for cache breakpoint)
        cache_breakpoint_index = assistant_indices[-1] if len(assistant_indices) >= 1 and use_cache else None

        for i, msg in enumerate(messages):
            content = msg["content"]

            # Check if this is the cache breakpoint position
            is_cache_breakpoint = (cache_breakpoint_index is not None and i == cache_breakpoint_index)

            if is_cache_breakpoint:
                # Add cache control to this assistant message.
                if isinstance(content, list):
                    # Follow-up turns: the last assistant message is already a
                    # list of content blocks (text + tool_use). Re-wrapping it as
                    # {"type":"text","text": <list>} sets `text` to a list and the
                    # API rejects it (content.0.text.text: Input should be a valid
                    # string), silently 400-ing every tool-using turn. Instead,
                    # attach the breakpoint to the last block in place — cache_control
                    # is valid on any block type (text, tool_use, etc.).
                    new_blocks = [dict(b) if isinstance(b, dict) else b for b in content]
                    if new_blocks and isinstance(new_blocks[-1], dict):
                        new_blocks[-1] = {
                            **new_blocks[-1],
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    anthropic_msg = {"role": msg["role"], "content": new_blocks}
                else:
                    anthropic_msg = {
                        "role": msg["role"],
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral", "ttl": "1h"}
                            }
                        ]
                    }
            else:
                # Regular message without cache control
                anthropic_msg = {
                    "role": msg["role"],
                    "content": content
                }

            result.append(anthropic_msg)

        return result

    def send_request(self, client: Any, request_params: dict) -> Any:
        """Send request to Anthropic API."""
        return client.beta.messages.create(**request_params, betas=self.BETA_HEADERS)

    def send_request_stream(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        """Stream response events from Anthropic API.

        Tool calls that include a `narration` field (e.g., report_state) have
        their narration value streamed live as content_delta events by parsing
        the partial JSON as it arrives. This lets the player see prose render
        in real time even though the prose is technically inside a tool input.
        """
        narration_streamers = {}
        with client.beta.messages.stream(**request_params, betas=self.BETA_HEADERS) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    # Only activate narration streaming for the report_state tool.
                    # Other tools (resolve_mechanics, etc.) don't carry narration and
                    # could spuriously match the "narration" substring inside their inputs.
                    if (getattr(block, "type", None) == "tool_use"
                            and getattr(block, "name", None) == "report_state"):
                        narration_streamers[event.index] = _NarrationFieldStreamer()
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, 'text'):
                        yield StreamEvent('content_delta', content=event.delta.text)
                    elif hasattr(event.delta, 'thinking'):
                        yield StreamEvent('thinking_delta', content=event.delta.thinking)
                    elif hasattr(event.delta, 'partial_json'):
                        streamer = narration_streamers.get(event.index)
                        if streamer is not None:
                            new_text = streamer.feed(event.delta.partial_json)
                            if new_text:
                                yield StreamEvent('content_delta', content=new_text)
                elif event.type == "content_block_stop":
                    narration_streamers.pop(event.index, None)
                elif event.type == "message_stop":
                    message = stream.get_final_message()
                    yield StreamEvent('done', usage=self._extract_usage(message))

    def _extract_usage(self, response: Any) -> dict:
        """Extract usage information from Anthropic response for streaming."""
        usage = response.usage
        raw_input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read_tokens = getattr(usage, 'cache_read_input_tokens', 0) or 0
        cache_creation_tokens = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        input_tokens = raw_input_tokens + cache_read_tokens + cache_creation_tokens

        # Extract thinking tokens and tool_use
        thinking_tokens = 0
        reasoning = None
        content = None
        tool_uses = []
        for block in response.content:
            if block.type == "thinking":
                reasoning = block.thinking
                if hasattr(block, 'thinking_tokens'):
                    thinking_tokens = block.thinking_tokens
                elif reasoning:
                    thinking_tokens = len(reasoning) // 4
            elif block.type == "text":
                content = block.text
            elif block.type == "tool_use":
                tool_uses.append({
                    "input": block.input,
                    "name": block.name,
                    "id": block.id,
                })

        # Keep legacy single-tool fields for downstream compatibility.
        # Use the first call because CPRED can emit resolve_mechanics before report_state.
        first_tool_use = tool_uses[0] if tool_uses else {}
        tool_use_input = first_tool_use.get("input")
        tool_use_name = first_tool_use.get("name")
        tool_use_id = first_tool_use.get("id")

        text_output_tokens = max(0, output_tokens - thinking_tokens)

        return {
            'input_tokens': input_tokens,
            'cache_read_tokens': cache_read_tokens,
            'cache_creation_tokens': cache_creation_tokens,
            'output_tokens': text_output_tokens,
            'reasoning_tokens': thinking_tokens,
            'content': content,
            'reasoning': reasoning,
            'tool_use_input': tool_use_input,
            'tool_use_name': tool_use_name,
            'tool_use_id': tool_use_id,
            'tool_uses': tool_uses,
            'stop_reason': getattr(response, 'stop_reason', None),
            'content_blocks': response.content,
        }

    def parse_response(self, response: Any) -> ParsedResponse:
        """Parse Anthropic response into standardized format."""
        content = None
        reasoning = None
        tool_use_parts = []

        # Extract content and thinking from response
        for block in response.content:
            if block.type == "text":
                content = block.text
            elif block.type == "thinking":
                reasoning = block.thinking
            elif block.type == "tool_use":
                tool_use_parts.append(json.dumps(block.input))

        if content is None:
            raise ValueError("No text content in Anthropic response")

        # Build full output text (narrative + tool call JSON) for cross-model token counting
        full_output_text = content
        if tool_use_parts:
            full_output_text = content + "\n" + "\n".join(tool_use_parts)

        # Extract token usage
        usage = response.usage
        raw_input_tokens = usage.input_tokens  # Anthropic returns non-cached only
        output_tokens = usage.output_tokens

        # Cache tokens
        cache_read_tokens = getattr(usage, 'cache_read_input_tokens', 0) or 0
        cache_creation_tokens = getattr(usage, 'cache_creation_input_tokens', 0) or 0

        # Normalize input_tokens to TOTAL (including cache) for consistency with OpenAI
        # This ensures base class calculate_cost and format_token_string work correctly
        input_tokens = raw_input_tokens + cache_read_tokens + cache_creation_tokens

        # With extended thinking, the API provides thinking tokens
        # Try to get from the thinking block's token count if available
        thinking_tokens = 0

        for block in response.content:
            if block.type == "thinking":
                # Some API versions include token count on the block
                if hasattr(block, 'thinking_tokens'):
                    thinking_tokens = block.thinking_tokens
                elif reasoning:
                    # Fallback: estimate based on content (~4 chars per token)
                    thinking_tokens = len(reasoning) // 4
                break

        # Text output tokens (excluding thinking)
        text_output_tokens = max(0, output_tokens - thinking_tokens)

        return ParsedResponse(
            content=content,
            reasoning=reasoning,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            output_tokens=text_output_tokens,
            reasoning_tokens=thinking_tokens,
            full_output_text=full_output_text if tool_use_parts else None
        )

    def calculate_cost(self, parsed: ParsedResponse) -> float:
        """Calculate cost with extended context pricing for >200K tokens."""
        p = self.pricing

        # parsed.input_tokens is normalized to TOTAL (including cache)
        # Check if extended context pricing applies
        if parsed.input_tokens > 200_000:
            # Extended context: 2x input, 1.5x output
            input_base = p.input_base * 2        # $6
            cache_write = p.cache_write * 2      # $12
            cache_read = p.cache_read * 2        # $0.60
            output = p.output * 1.5              # $22.50
            reasoning = p.reasoning * 1.5        # $22.50
        else:
            input_base = p.input_base
            cache_write = p.cache_write
            cache_read = p.cache_read
            output = p.output
            reasoning = p.reasoning

        # non_cached = total - cache_read - cache_creation
        non_cached = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens

        return (
            non_cached * input_base / 1_000_000 +
            parsed.cache_creation_tokens * cache_write / 1_000_000 +
            parsed.cache_read_tokens * cache_read / 1_000_000 +
            parsed.output_tokens * output / 1_000_000 +
            parsed.reasoning_tokens * reasoning / 1_000_000
        )

    def count_tokens(self, text: str) -> int:
        """
        Count tokens for Claude using character-based estimation.

        Uses ~3.8 characters per token which works well for conversational
        content (~4% error). For dense content like project files, use
        count_tokens_api() for accurate counts.
        """
        return int(len(text) / 3.8)

    def count_tokens_buffered(self, text: str) -> int:
        """
        Estimate tokens with 15% buffer for trimming decisions.

        Uses chars/3.3 (~15% higher than 3.8) to avoid undercounting.
        Safe margin ensures we don't exceed 200k when estimating 160-195k.
        """
        return int(len(text) / 3.3)

    def count_tokens_api(self, text: str, api_key: str) -> int:
        """
        Count tokens using Claude's actual token counting API.

        This is accurate but requires an API call. Use for:
        - Project file uploads (dense content)
        - System instructions updates

        Args:
            text: The text to count tokens for
            api_key: Anthropic API key

        Returns:
            Exact token count from Claude API
        """
        # Handle empty text - API requires non-empty content
        if not text or not text.strip():
            return 0

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        # Strip date suffix for count_tokens — some dated model IDs
        # (e.g. claude-opus-4-5-20250918) are not recognized by the
        # count_tokens endpoint even though they work for messages.
        import re
        count_model = re.sub(r'-\d{8}$', '', self.MODEL_NAME)

        response = client.messages.count_tokens(
            model=count_model,
            messages=[{"role": "user", "content": text}]
        )
        return response.input_tokens


class AnthropicOpus45Provider(AnthropicProvider):
    """Provider for Claude Opus 4.5."""

    MODEL_NAME = "claude-opus-4-5"
    MAX_TOKENS = 16000

    BETA_HEADERS = [
        "context-1m-2025-08-07",
        "context-management-2025-06-27",
        "extended-cache-ttl-2025-04-11"
    ]

    @property
    def model_id(self) -> str:
        return "claude-opus-4.5"

    @property
    def display_name(self) -> str:
        return "Claude Opus 4.5"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=5.00,
            cache_write=10.00,
            cache_read=0.50,
            output=25.0,
            reasoning=25.0
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(
            threshold=80_000,
            target=55_000
        )

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool,
        use_cache: bool = True
    ) -> dict:
        """Build request for Opus 4.5 (standard extended thinking)."""
        return super().build_request(messages, username, project, chat_name, is_free_chat, use_cache=use_cache)


class AnthropicOpusProvider(AnthropicProvider):
    """Provider for Claude Opus 4.6 (inactive, kept for potential future use)."""

    MODEL_NAME = "claude-opus-4-6"
    MAX_TOKENS = 16000        # Same as Sonnet (SDK requires streaming for higher values)

    # Opus 4.6 now supports 1M context like Sonnet
    BETA_HEADERS = [
        "context-1m-2025-08-07",           # Enables 1M token context (NEW for Opus 4.6)
        "context-management-2025-06-27",   # Clears old thought blocks from history
        "extended-cache-ttl-2025-04-11"    # Enables 1hr cache TTL
    ]

    @property
    def model_id(self) -> str:
        return "claude-opus-4.6"

    @property
    def display_name(self) -> str:
        return "Claude Opus 4.6"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=5.00,     # $5/1M tokens
            cache_write=10.00,   # 2x base for 1hr cache write
            cache_read=0.50,     # 90% discount
            output=25.0,         # $25/1M tokens
            reasoning=25.0       # $25/1M tokens
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(
            threshold=80_000,    # User-specified rolling window
            target=55_000
        )

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool,
        use_cache: bool = True
    ) -> dict:
        """
        Build Anthropic API request with adaptive thinking for Opus 4.6.

        Opus 4.6 uses adaptive thinking with effort parameter instead of
        explicit thinking budget.
        """
        # Call parent to get base params (system, messages, cache breakpoints)
        params = super().build_request(messages, username, project, chat_name, is_free_chat, use_cache=use_cache)

        # Override thinking to use adaptive mode with effort
        params["thinking"] = {"type": "adaptive"}
        params["output_config"] = {"effort": "high"}

        return params


class AnthropicSonnet46Provider(AnthropicProvider):
    """Provider for Claude Sonnet 4.6 (adaptive thinking).

    Sonnet 4.6 removed the fixed thinking budget — `budget_tokens` 400s — so it
    uses adaptive thinking + the effort parameter, like Opus 4.6. Same Sonnet
    price tier as 4.5 ($3/$15), 1M context, 64K max output."""

    MODEL_NAME = "claude-sonnet-4-6"
    MAX_TOKENS = 16000

    @property
    def model_id(self) -> str:
        return "claude-sonnet-4.6"

    @property
    def display_name(self) -> str:
        return "Claude Sonnet 4.6"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=3.00,     # $3/1M input
            cache_write=6.00,    # 2x base for 1hr cache write
            cache_read=0.30,     # 0.1x base
            output=15.0,         # $15/1M output
            reasoning=15.0       # $15/1M thinking
        )

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool,
        use_cache: bool = True
    ) -> dict:
        """Sonnet 4.6 uses adaptive thinking + effort instead of a fixed budget."""
        params = super().build_request(messages, username, project, chat_name, is_free_chat, use_cache=use_cache)
        params["thinking"] = {"type": "adaptive"}
        params["output_config"] = {"effort": "high"}
        return params


class AnthropicOpus3Provider(AnthropicProvider):
    """Provider for Claude 3 Opus (2024).

    Older model preferred by some users for narrative/writing quality.
    Notable differences from 4.x Opus:
      - No extended thinking support
      - 200k context only (no 1M beta)
      - 4096 max output tokens
      - 1hr cache TTL via extended-cache-ttl beta (2x base for cache writes)
    """

    MODEL_NAME = "claude-3-opus-20240229"
    MAX_TOKENS = 4096

    BETA_HEADERS = [
        "extended-cache-ttl-2025-04-11"
    ]

    @property
    def model_id(self) -> str:
        return "claude-3-opus"

    @property
    def display_name(self) -> str:
        return "Claude 3 Opus"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=15.00,
            cache_write=30.00,    # 1hr cache write: 2x base
            cache_read=1.50,      # 0.1x base
            output=75.0,
            reasoning=75.0
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(
            threshold=80_000,
            target=55_000
        )

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool,
        use_cache: bool = True
    ) -> dict:
        """Build request for Claude 3 Opus.

        Strips extended thinking (not supported by Claude 3 Opus). Keeps
        the parent's 1hr cache_control breakpoints since the
        extended-cache-ttl beta is enabled.
        """
        params = super().build_request(messages, username, project, chat_name, is_free_chat, use_cache=use_cache)
        params.pop("thinking", None)
        return params

    def calculate_cost(self, parsed: ParsedResponse) -> float:
        """Claude 3 Opus has flat pricing — no extended-context tier."""
        p = self.pricing
        non_cached = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
        return (
            non_cached * p.input_base / 1_000_000 +
            parsed.cache_creation_tokens * p.cache_write / 1_000_000 +
            parsed.cache_read_tokens * p.cache_read / 1_000_000 +
            parsed.output_tokens * p.output / 1_000_000 +
            parsed.reasoning_tokens * p.reasoning / 1_000_000
        )

    def count_tokens_api(self, text: str, api_key: str) -> int:
        """Count tokens using Claude's count_tokens endpoint.

        Override: Claude 3 Opus's canonical model name is the dated form
        (claude-3-opus-20240229) and the count_tokens endpoint accepts it.
        The parent's regex strip would yield a non-existent 'claude-3-opus'.
        """
        if not text or not text.strip():
            return 0

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.count_tokens(
            model=self.MODEL_NAME,
            messages=[{"role": "user", "content": text}]
        )
        return response.input_tokens


def add_updates_to_messages(messages: list[dict], updates_text: str) -> list[dict]:
    """
    Add context updates to the message list for Claude.

    Claude does NOT allow consecutive user messages, so updates must be
    concatenated INTO the last user message. Updates are prepended so they
    appear before the user's message content.
    """
    if not updates_text.strip():
        return messages

    if not messages:
        return messages

    # Find the last user message
    result = messages.copy()
    for i in range(len(result) - 1, -1, -1):
        if result[i]["role"] == "user":
            # Prepend updates to this user message's content
            original_content = result[i]["content"]
            updates_block = f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]\n\n"
            result[i] = {
                **result[i],
                "content": updates_block + original_content
            }
            break

    return result
