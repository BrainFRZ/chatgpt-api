"""
Provider abstraction layer for multi-model support.

This module provides a base class for AI model providers and a registry
to manage available providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Iterator


@dataclass
class ParsedResponse:
    """Standardized response from any model provider."""
    content: str
    reasoning: Optional[str]
    input_tokens: int
    cache_read_tokens: int       # Cache hit tokens
    cache_creation_tokens: int   # Tokens written to cache
    output_tokens: int
    reasoning_tokens: int
    full_output_text: Optional[str] = None  # Full output including tool calls (for cross-model token counting)


@dataclass
class Pricing:
    """Pricing per million tokens."""
    input_base: float     # Non-cached input (base rate)
    cache_write: float    # Cache write tokens
    cache_read: float     # Cache hit tokens
    output: float         # Output text tokens
    reasoning: float      # Reasoning/thinking tokens


@dataclass
class ContextLimits:
    """Context window management limits."""
    threshold: int  # Start trimming at this token count
    target: int     # Trim down to this token count


@dataclass
class StreamEvent:
    """Event emitted during streaming response."""
    event_type: str  # 'content_delta', 'thinking_delta', 'done', 'error'
    content: Optional[str] = None
    usage: Optional[dict] = None
    error: Optional[str] = None


class ModelProvider(ABC):
    """Abstract base class for AI model providers."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Unique identifier for the model (e.g., 'gpt-5.2', 'claude-sonnet-4.5')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name for the model."""
        pass

    @property
    @abstractmethod
    def pricing(self) -> Pricing:
        """Pricing information for the model."""
        pass

    @property
    @abstractmethod
    def context_limits(self) -> ContextLimits:
        """Context window threshold and target for trimming."""
        pass

    @abstractmethod
    def get_client(self, api_key: str) -> Any:
        """Create and return an API client instance."""
        pass

    @abstractmethod
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
        Build the API request parameters.

        Args:
            messages: List of message dicts with 'role' and 'content'
            username: Current user's username
            project: Project name or None
            chat_name: Current chat name
            is_free_chat: Whether this is a free (non-project) chat
            use_cache: Whether to use prompt caching (Anthropic only)

        Returns:
            Dict of parameters to pass to the API
        """
        pass

    @abstractmethod
    def send_request(self, client: Any, request_params: dict) -> Any:
        """Send the request to the API and return the raw response."""
        pass

    @abstractmethod
    def parse_response(self, response: Any) -> ParsedResponse:
        """Parse the API response into a standardized format."""
        pass

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in the given text."""
        pass

    @abstractmethod
    def send_request_stream(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        """Stream response events from the API."""
        pass

    def calculate_cost(self, parsed: ParsedResponse) -> float:
        """Calculate the total cost for a response.

        ParsedResponse.input_tokens should be TOTAL input tokens (including cache).
        non_cached = input_tokens - cache_read - cache_creation
        """
        p = self.pricing
        non_cached = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
        return (
            non_cached * p.input_base / 1_000_000 +
            parsed.cache_creation_tokens * p.cache_write / 1_000_000 +
            parsed.cache_read_tokens * p.cache_read / 1_000_000 +
            parsed.output_tokens * p.output / 1_000_000 +
            parsed.reasoning_tokens * p.reasoning / 1_000_000
        )

    def format_token_string(self, parsed: ParsedResponse) -> str:
        """Format tokens as 'I:X C:Y W:Z O:Q R:R T:N' string.

        ParsedResponse.input_tokens is the sum (non-cached + cache_read +
        cache_creation) as extracted by providers. For display:
          I = non-cached fresh input
          C = cache_read_tokens  (charged at cache-read rate, ~0.1x base)
          W = cache_creation_tokens (charged at cache-write rate)
          O = output_tokens
          R = reasoning_tokens
          T = distinct total tokens processed (see cache-overlap note below)

        T subtlety: on Anthropic 1h-extended-cache refresh events, the API
        reports the SAME cached content under both cache_read AND
        cache_creation (read from old cache, written to new cache for TTL
        extension). Naively summing the two inflates T by the refresh amount,
        which is misleading even though billing is correctly double-charged.
        When read and creation are within 10% of each other in magnitude, we
        treat them as overlap and count the content once. Otherwise (the
        common case where one is near-zero or they're clearly disjoint) we
        sum normally.
        """
        non_cached = parsed.input_tokens - parsed.cache_read_tokens - parsed.cache_creation_tokens
        r = parsed.cache_read_tokens
        w = parsed.cache_creation_tokens
        if r > 0 and w > 0 and abs(r - w) < 0.1 * max(r, w):
            cached_portion = max(r, w)  # refresh-overlap: same content in both buckets
        else:
            cached_portion = r + w
        total = non_cached + cached_portion + parsed.output_tokens + parsed.reasoning_tokens
        return (
            f"I:{non_cached} "
            f"C:{r} "
            f"W:{w} "
            f"O:{parsed.output_tokens} "
            f"R:{parsed.reasoning_tokens} "
            f"T:{total}"
        )

    def get_metadata(self) -> dict:
        """Return model metadata for the /api/models endpoint."""
        return {
            "id": self.model_id,
            "name": self.display_name,
            "pricing": {
                "input_base": self.pricing.input_base,
                "cache_write": self.pricing.cache_write,
                "cache_read": self.pricing.cache_read,
                "output": self.pricing.output,
                "reasoning": self.pricing.reasoning,
            },
            "context_limits": {
                "threshold": self.context_limits.threshold,
                "target": self.context_limits.target,
            }
        }


class ProviderRegistry:
    """Registry for managing available model providers."""

    _providers: dict[str, ModelProvider] = {}
    _default_model: str = "gemini-3.1-pro"

    @classmethod
    def register(cls, provider: ModelProvider) -> None:
        """Register a provider instance."""
        cls._providers[provider.model_id] = provider

    @classmethod
    def get(cls, model_id: str) -> Optional[ModelProvider]:
        """Get a provider by model ID."""
        return cls._providers.get(model_id)

    @classmethod
    def get_default(cls) -> ModelProvider:
        """Get the default provider."""
        return cls._providers[cls._default_model]

    @classmethod
    def list_models(cls) -> list[dict]:
        """List all available models with their metadata."""
        return [p.get_metadata() for p in cls._providers.values()]

    @classmethod
    def get_required_api_key(cls, model_id: str) -> str:
        """Get which API key is required for a model ('openai', 'anthropic', or 'google')."""
        if model_id.startswith("claude"):
            return "anthropic"
        if model_id.startswith("gemini"):
            return "google"
        return "openai"
