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


class FlexTimeoutError(Exception):
    """Raised when flex mode TTFB timeout is exceeded."""
    pass


# Pricing constants for GPT-5.2 service tiers
FLEX_PRICING = Pricing(
    input_base=0.875, cache_write=0.875, cache_read=0.0875, output=7.0, reasoning=7.0
)
STANDARD_PRICING = Pricing(
    input_base=1.75, cache_write=1.75, cache_read=0.175, output=14.0, reasoning=14.0
)


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
        is_free_chat: bool,
        service_tier: str = "flex"
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
            },
            "service_tier": service_tier
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

    def send_request_stream_with_fallback(
        self, client: Any, request_params: dict, ttfb_timeout: float = 15.0
    ) -> Iterator[StreamEvent]:
        """
        Stream with flex mode, auto-fallback to standard on timeout/error.

        If TTFB exceeds ttfb_timeout seconds before receiving content,
        cancels the stream and retries with standard tier.
        """
        import logging
        import time
        logger = logging.getLogger(__name__)

        # Ensure flex mode for initial attempt
        request_params['service_tier'] = 'flex'
        request_params['stream'] = True

        try:
            stream = client.responses.create(**request_params)
            start_time = time.time()
            first_content = False

            for event in stream:
                if not first_content:
                    if event.type == "response.output_text.delta":
                        first_content = True
                    elif time.time() - start_time > ttfb_timeout:
                        logger.warning(f"Flex mode TTFB timeout ({ttfb_timeout}s) exceeded, falling back to standard")
                        stream.close()
                        raise FlexTimeoutError()

                if event.type == "response.output_text.delta":
                    yield StreamEvent('content_delta', content=event.delta)
                elif event.type == "response.completed":
                    logger.info(f"OpenAI stream (flex): got response.completed event")
                    usage = self._extract_usage(event.response)
                    usage['service_tier'] = 'flex'
                    yield StreamEvent('done', usage=usage)
                elif event.type == "response.incomplete":
                    logger.warning(f"OpenAI stream (flex): got response.incomplete event")
                    usage = self._extract_usage(event.response)
                    usage['service_tier'] = 'flex'
                    yield StreamEvent('done', usage=usage)

        except FlexTimeoutError:
            # Retry with standard tier
            logger.info("Retrying request with standard tier")
            request_params['service_tier'] = 'auto'
            for stream_event in self._stream_standard(client, request_params):
                yield stream_event

        except Exception as e:
            # Check if it's a 429 or similar recoverable error
            if hasattr(e, 'status_code') and e.status_code == 429:
                logger.warning(f"Flex mode got 429, falling back to standard")
                request_params['service_tier'] = 'auto'
                for stream_event in self._stream_standard(client, request_params):
                    yield stream_event
            else:
                raise

    def _stream_standard(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        """Stream with standard tier (used as fallback)."""
        import logging
        logger = logging.getLogger(__name__)

        request_params['stream'] = True
        stream = client.responses.create(**request_params)

        for event in stream:
            if event.type == "response.output_text.delta":
                yield StreamEvent('content_delta', content=event.delta)
            elif event.type == "response.completed":
                logger.info(f"OpenAI stream (standard): got response.completed event")
                usage = self._extract_usage(event.response)
                usage['service_tier'] = 'standard'
                yield StreamEvent('done', usage=usage)
            elif event.type == "response.incomplete":
                logger.warning(f"OpenAI stream (standard): got response.incomplete event")
                usage = self._extract_usage(event.response)
                usage['service_tier'] = 'standard'
                yield StreamEvent('done', usage=usage)

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

    def get_pricing_for_tier(self, tier: str) -> Pricing:
        """Get pricing based on service tier."""
        return FLEX_PRICING if tier == 'flex' else STANDARD_PRICING

    def calculate_cost_with_tier(self, parsed: ParsedResponse, tier: str) -> float:
        """Calculate cost using tier-specific pricing."""
        p = self.get_pricing_for_tier(tier)
        non_cached = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
        return (
            non_cached * p.input_base +
            parsed.cache_creation_tokens * p.cache_write +
            parsed.cache_read_tokens * p.cache_read +
            parsed.output_tokens * p.output +
            parsed.reasoning_tokens * p.reasoning
        ) / 1_000_000


def add_updates_to_messages(messages: list[dict], updates_text: str) -> list[dict]:
    """
    Add context updates to the message list for OpenAI.

    OpenAI allows consecutive user messages, so updates are added
    as a separate user message before the last user message.
    """
    if not updates_text.strip():
        return messages

    updates_msg = {
        "role": "user",
        "content": f"[CONTEXT UPDATES - Reference as needed for the user message below]\n{updates_text}\n[/CONTEXT UPDATES]"
    }

    # Insert before the last message (which should be the user's new message)
    if messages:
        return messages[:-1] + [updates_msg, messages[-1]]
    return [updates_msg]
