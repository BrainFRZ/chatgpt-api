"""
Provider abstraction layer for multi-model support.

This module provides a base class for AI model providers and a registry
to manage available providers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ParsedResponse:
    """Standardized response from any model provider."""
    content: str
    reasoning: Optional[str]
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int


@dataclass
class Pricing:
    """Pricing per million tokens."""
    input_new: float      # Cache miss / new input
    input_cached: float   # Cache hit / cached input
    output: float         # Output text tokens
    reasoning: float      # Reasoning/thinking tokens


@dataclass
class ContextLimits:
    """Context window management limits."""
    threshold: int  # Start trimming at this token count
    target: int     # Trim down to this token count


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
        is_free_chat: bool
    ) -> dict:
        """
        Build the API request parameters.

        Args:
            messages: List of message dicts with 'role' and 'content'
            username: Current user's username
            project: Project name or None
            chat_name: Current chat name
            is_free_chat: Whether this is a free (non-project) chat

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

    def calculate_cost(self, parsed: ParsedResponse) -> float:
        """Calculate the total cost for a response."""
        p = self.pricing
        new_input = parsed.input_tokens - parsed.cached_tokens
        return (
            new_input * p.input_new / 1_000_000 +
            parsed.cached_tokens * p.input_cached / 1_000_000 +
            parsed.output_tokens * p.output / 1_000_000 +
            parsed.reasoning_tokens * p.reasoning / 1_000_000
        )

    def format_token_string(self, parsed: ParsedResponse) -> str:
        """Format tokens as 'I:X C:Y O:Z R:W T:N' string."""
        new_input = parsed.input_tokens - parsed.cached_tokens
        total = parsed.input_tokens + parsed.output_tokens + parsed.reasoning_tokens
        return f"I:{new_input} C:{parsed.cached_tokens} O:{parsed.output_tokens} R:{parsed.reasoning_tokens} T:{total}"

    def get_metadata(self) -> dict:
        """Return model metadata for the /api/models endpoint."""
        return {
            "id": self.model_id,
            "name": self.display_name,
            "pricing": {
                "input_new": self.pricing.input_new,
                "input_cached": self.pricing.input_cached,
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
    _default_model: str = "gpt-5.2"

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
        """Get which API key is required for a model ('openai' or 'anthropic')."""
        if model_id.startswith("claude"):
            return "anthropic"
        return "openai"
