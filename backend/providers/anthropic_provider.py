"""
Anthropic Claude Sonnet 4.5 provider implementation with extended thinking.
"""

from typing import Any, Optional
import anthropic

from . import ModelProvider, ParsedResponse, Pricing, ContextLimits


class AnthropicProvider(ModelProvider):
    """Provider for Claude Sonnet 4.5 with extended thinking."""

    MODEL_NAME = "claude-sonnet-4-5-20250929"
    THINKING_BUDGET = 3000
    MAX_TOKENS = 16000  # Max output tokens (including thinking)

    # Beta headers for extended context and context management
    BETA_HEADERS = [
        "context-1m-2025-08-07",           # Enables 1M token context
        "context-management-2025-06-27"    # Clears old thought blocks from history
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
            input_new=6.00,      # $/1M tokens (cache miss/write)
            input_cached=0.30,   # $/1M tokens (cache hit/read)
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
        is_free_chat: bool
    ) -> dict:
        """
        Build Anthropic API request parameters.

        Claude does NOT allow consecutive user messages, so updates must be
        concatenated into the last user message content.

        Cache breakpoint strategy (1hr TTL):
        - Place breakpoints: (1) after system, (2) after second-to-last assistant
        - This ensures cache hits on stable prefixes
        """
        # Separate system message from conversation
        system_content = None
        conversation_messages = []

        for msg in messages:
            if msg["role"] == "system":
                # Only set system with cache_control if content is non-empty
                content = msg.get("content", "").strip()
                if content:
                    system_content = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
                        }
                    ]
            else:
                conversation_messages.append(msg)

        # Convert messages to Anthropic format and add cache breakpoints
        anthropic_messages = self._convert_messages_with_cache(conversation_messages)

        params = {
            "model": self.MODEL_NAME,
            "max_tokens": self.MAX_TOKENS,
            "messages": anthropic_messages,
            "thinking": {
                "type": "enabled",
                "budget_tokens": self.THINKING_BUDGET
            }
        }

        # Only include system if it has content
        if system_content:
            params["system"] = system_content

        return params

    def _convert_messages_with_cache(self, messages: list[dict]) -> list[dict]:
        """
        Convert messages to Anthropic format with cache breakpoints.

        Cache breakpoint strategy:
        - Breakpoint 1: After system message (handled in build_request)
        - Breakpoint 2: After second-to-last assistant message

        Why this works:
        - The prefix up to breakpoint 2 is stable (Updates already removed from previous turns)
        - Future rounds match the cached prefix -> cache HIT
        - Only current exchange is cache miss (unavoidable)
        """
        result = []

        # Find indices of all assistant messages
        assistant_indices = [i for i, msg in enumerate(messages) if msg["role"] == "assistant"]

        # Second-to-last assistant index (for cache breakpoint)
        cache_breakpoint_index = assistant_indices[-2] if len(assistant_indices) >= 2 else None

        for i, msg in enumerate(messages):
            content = msg["content"]

            # Check if this is the cache breakpoint position
            is_cache_breakpoint = (cache_breakpoint_index is not None and i == cache_breakpoint_index)

            if is_cache_breakpoint:
                # Add cache control to this assistant message
                anthropic_msg = {
                    "role": msg["role"],
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"}
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
        return client.messages.create(**request_params)

    def parse_response(self, response: Any) -> ParsedResponse:
        """Parse Anthropic response into standardized format."""
        content = None
        reasoning = None

        # Extract content and thinking from response
        for block in response.content:
            if block.type == "text":
                content = block.text
            elif block.type == "thinking":
                reasoning = block.thinking

        if content is None:
            raise ValueError("No text content in Anthropic response")

        # Extract token usage
        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens

        # Cache tokens (cache_read_input_tokens for hits)
        cached_tokens = getattr(usage, 'cache_read_input_tokens', 0) or 0

        # With extended thinking, the API provides thinking tokens in cache_creation_input_tokens
        # when thinking is included, but the primary way to get thinking token count
        # is from the thinking block's token count if available
        thinking_tokens = 0

        # Try to get thinking tokens from the response content blocks
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
            cached_tokens=cached_tokens,
            output_tokens=text_output_tokens,
            reasoning_tokens=thinking_tokens
        )

    def count_tokens(self, text: str) -> int:
        """
        Count tokens for Claude using character-based estimation.

        Uses ~3.8 characters per token which works well for conversational
        content (~4% error). For dense content like project files, use
        count_tokens_api() for accurate counts.
        """
        return int(len(text) / 3.8)

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
    concatenated INTO the last user message (preserving the wrapper format).
    """
    if not updates_text.strip():
        return messages

    if not messages:
        return messages

    # Find the last user message
    result = messages.copy()
    for i in range(len(result) - 1, -1, -1):
        if result[i]["role"] == "user":
            # Append updates to this user message's content
            original_content = result[i]["content"]
            updates_block = f"\n\n[CONTEXT UPDATES - Reference as needed for the message above]\n{updates_text}\n[/CONTEXT UPDATES]"
            result[i] = {
                **result[i],
                "content": original_content + updates_block
            }
            break

    return result
