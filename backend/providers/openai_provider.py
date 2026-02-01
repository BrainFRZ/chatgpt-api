"""
OpenAI GPT-5.2 provider implementation.
"""

from typing import Any, Optional, Iterator
from openai import OpenAI
import tiktoken

from . import ModelProvider, ParsedResponse, Pricing, ContextLimits, StreamEvent


# Cache the tiktoken encoder for performance
_token_encoder = None


def get_token_encoder():
    """Get cached tiktoken encoder instance."""
    global _token_encoder
    if _token_encoder is None:
        _token_encoder = tiktoken.get_encoding("cl100k_base")
    return _token_encoder


class OpenAIProvider(ModelProvider):
    """Provider for OpenAI GPT-5.2 model."""

    MODEL_NAME = "gpt-5.2"
    PROMPT_CACHE_RETENTION = "24h"
    MAX_OUTPUT_TOKENS_FREE_CHAT = 1200

    @property
    def model_id(self) -> str:
        return "gpt-5.2"

    @property
    def display_name(self) -> str:
        return "GPT-5.2"

    @property
    def pricing(self) -> Pricing:
        return Pricing(
            input_base=1.75,     # $/1M tokens (non-cached input)
            cache_write=1.75,    # $/1M tokens (OpenAI doesn't differentiate)
            cache_read=0.175,    # $/1M tokens (cache hit)
            output=14.0,         # $/1M tokens
            reasoning=14.0       # $/1M tokens
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(
            threshold=275_000,  # Start trimming at this token count
            target=225_000      # Trim down to this target
        )

    def get_client(self, api_key: str) -> Any:
        """Create OpenAI client."""
        return OpenAI(api_key=api_key)

    def build_request(
        self,
        messages: list[dict],
        username: str,
        project: Optional[str],
        chat_name: str,
        is_free_chat: bool
    ) -> dict:
        """
        Build OpenAI API request parameters.

        OpenAI allows consecutive user messages, so updates can be sent
        as a separate trailing user message.
        """
        # Sanitize project name for cache key
        project_part = (project or "root").replace(" ", "-").replace("/", "-").replace("\\", "-")

        params = {
            "model": self.MODEL_NAME,
            "input": messages,
            "store": False,
            "prompt_cache_retention": self.PROMPT_CACHE_RETENTION,
            "prompt_cache_key": f"redvelveteer-86171435-{username}-{project_part}-{chat_name}",
            "reasoning": {
                "effort": "medium",
                "summary": "auto"
            }
        }

        # Add output token limit only for free chats
        if is_free_chat:
            params["max_output_tokens"] = self.MAX_OUTPUT_TOKENS_FREE_CHAT

        return params

    def send_request(self, client: Any, request_params: dict) -> Any:
        """Send request to OpenAI API."""
        return client.responses.create(**request_params)

    def send_request_stream(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        """Stream response events from OpenAI API."""
        import logging
        logger = logging.getLogger(__name__)

        request_params['stream'] = True
        stream = client.responses.create(**request_params)
        last_event_type = None
        final_response = None
        for event in stream:
            last_event_type = event.type
            if event.type == "response.output_text.delta":
                yield StreamEvent('content_delta', content=event.delta)
            elif event.type == "response.completed":
                logger.info(f"OpenAI stream: got response.completed event")
                yield StreamEvent('done', usage=self._extract_usage(event.response))
                final_response = event.response
            elif event.type == "response.incomplete":
                # Handle incomplete responses (e.g., hit token limit)
                logger.warning(f"OpenAI stream: got response.incomplete event")
                yield StreamEvent('done', usage=self._extract_usage(event.response))
                final_response = event.response

        logger.info(f"OpenAI stream: loop ended, last event was {last_event_type}")

    def _extract_usage(self, response: Any) -> dict:
        """Extract usage information from OpenAI response for streaming."""
        # Extract message content and reasoning
        content = None
        reasoning = None

        for item in response.output:
            if item.type == "message":
                # Extract content regardless of status (may be "completed" or "incomplete")
                for content_item in item.content:
                    if content_item.type == "output_text":
                        content = content_item.text
                        break
            elif item.type == "reasoning":
                if hasattr(item, 'summary') and item.summary:
                    for summary_item in item.summary:
                        if hasattr(summary_item, 'text'):
                            reasoning = summary_item.text
                            break

        usage = response.usage
        input_tokens = usage.input_tokens
        cached_tokens = 0
        if hasattr(usage, 'input_tokens_details') and hasattr(usage.input_tokens_details, 'cached_tokens'):
            cached_tokens = usage.input_tokens_details.cached_tokens or 0

        output_tokens = usage.output_tokens
        reasoning_tokens = 0
        if hasattr(usage, 'output_tokens_details') and usage.output_tokens_details:
            reasoning_tokens = getattr(usage.output_tokens_details, 'reasoning_tokens', 0) or 0

        text_output_tokens = max(0, output_tokens - reasoning_tokens)

        return {
            'input_tokens': input_tokens,
            'cache_read_tokens': cached_tokens,
            'cache_creation_tokens': 0,
            'output_tokens': text_output_tokens,
            'reasoning_tokens': reasoning_tokens,
            'content': content,
            'reasoning': reasoning
        }

    def parse_response(self, response: Any) -> ParsedResponse:
        """Parse OpenAI response into standardized format."""
        # Extract message content and reasoning
        content = None
        reasoning = None

        for item in response.output:
            if item.type == "message":
                if item.status == "completed":
                    for content_item in item.content:
                        if content_item.type == "output_text":
                            content = content_item.text
                            break
            elif item.type == "reasoning":
                # Capture reasoning summary if available
                if hasattr(item, 'summary') and item.summary:
                    for summary_item in item.summary:
                        if hasattr(summary_item, 'text'):
                            reasoning = summary_item.text
                            break

        if content is None:
            raise ValueError("No message content in OpenAI response")

        # Extract token usage
        usage = response.usage
        input_tokens = usage.input_tokens
        cached_tokens = 0
        if hasattr(usage, 'input_tokens_details') and hasattr(usage.input_tokens_details, 'cached_tokens'):
            cached_tokens = usage.input_tokens_details.cached_tokens or 0

        output_tokens = usage.output_tokens
        reasoning_tokens = 0
        if hasattr(usage, 'output_tokens_details') and usage.output_tokens_details:
            reasoning_tokens = getattr(usage.output_tokens_details, 'reasoning_tokens', 0) or 0

        # Text output tokens (non-reasoning)
        text_output_tokens = max(0, output_tokens - reasoning_tokens)

        return ParsedResponse(
            content=content,
            reasoning=reasoning,
            input_tokens=input_tokens,
            cache_read_tokens=cached_tokens,
            cache_creation_tokens=0,  # OpenAI doesn't report this separately
            output_tokens=text_output_tokens,
            reasoning_tokens=reasoning_tokens
        )

    def count_tokens(self, text: str) -> int:
        """Count tokens using tiktoken."""
        encoder = get_token_encoder()
        return len(encoder.encode(text))


def add_updates_to_messages(messages: list[dict], updates_text: str) -> list[dict]:
    """
    Add context updates to the message list for OpenAI.

    OpenAI allows consecutive user messages, so updates are added
    as a separate trailing user message.
    """
    if not updates_text.strip():
        return messages

    updates_msg = {
        "role": "user",
        "content": f"[CONTEXT UPDATES - Reference as needed for the user message above]\n{updates_text}\n[/CONTEXT UPDATES]"
    }
    return messages + [updates_msg]
