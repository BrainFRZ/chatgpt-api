"""OpenRouter-backed DeepSeek providers for explicit version pinning.

DeepSeek-direct's `deepseek-chat` alias routes to V4 Flash, and the direct API
only serves V4 Flash / V4 Pro — DeepSeek V3.2 is NOT available there. OpenRouter
exposes explicit slugs (deepseek/deepseek-v3.2, -v4-flash, -v4-pro), so we pin
versions here. All are OpenAI-compatible, so they reuse DeepSeekProvider's
request/response translation + tool-call-as-text recovery + bench counters;
only base_url, model name, ids, and pricing differ.

Pricing is approximate — verify against OpenRouter's current price sheet; it
only affects the benchmark cost meter, not pass/fail.
"""
from . import Pricing, ContextLimits
from .deepseek_provider import DeepSeekProvider


class _OpenRouterBase(DeepSeekProvider):
    BASE_URL = "https://openrouter.ai/api/v1"
    MAX_TOKENS = 16000  # room for full report_state when used as the main model
    # Never route to SambaNova (32k context cap — too small for ~125k turns);
    # prefer large-context, high-quality hosts, but allow fallbacks to others.
    PROVIDER_ROUTING = {
        "ignore": ["sambanova"],
        "order": ["deepinfra", "siliconflow", "novita", "baidu"],
        "allow_fallbacks": True,
    }


class OpenRouterDeepSeekV32(_OpenRouterBase):
    MODEL_NAME = "deepseek/deepseek-v3.2"
    # Lead with Baidu: fp8 (vs deepinfra's fp4 — quality), reliable prompt caching
    # (10x discount $0.025/M, probed surviving 45+ min), and ~2x faster generation
    # (~18 vs ~8 tok/s) than the prior deepinfra-led order. siliconflow/novita are
    # fp8 fallbacks; allow_fallbacks keeps uptime if Baidu is briefly unavailable.
    PROVIDER_ROUTING = {
        "ignore": ["sambanova"],
        "order": ["baidu", "siliconflow", "novita"],
        "allow_fallbacks": True,
    }

    @property
    def model_id(self) -> str:
        return "deepseek-v3.2-or"

    @property
    def display_name(self) -> str:
        return "DeepSeek V3.2 (OpenRouter)"

    @property
    def pricing(self) -> Pricing:
        return Pricing(input_base=0.27, cache_write=0.27, cache_read=0.04, output=0.40, reasoning=0.40)

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(threshold=110_000, target=88_000)  # 131k ctx


class OpenRouterDeepSeekV4Flash(_OpenRouterBase):
    MODEL_NAME = "deepseek/deepseek-v4-flash"

    @property
    def model_id(self) -> str:
        return "deepseek-v4-flash-or"

    @property
    def display_name(self) -> str:
        return "DeepSeek V4 Flash (OpenRouter)"

    @property
    def pricing(self) -> Pricing:
        return Pricing(input_base=0.28, cache_write=0.28, cache_read=0.028, output=0.42, reasoning=0.42)

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(threshold=900_000, target=700_000)  # 1M ctx


class OpenRouterDeepSeekV4Pro(_OpenRouterBase):
    MODEL_NAME = "deepseek/deepseek-v4-pro"

    @property
    def model_id(self) -> str:
        return "deepseek-v4-pro-or"

    @property
    def display_name(self) -> str:
        return "DeepSeek V4 Pro (OpenRouter)"

    @property
    def pricing(self) -> Pricing:
        return Pricing(input_base=0.55, cache_write=0.55, cache_read=0.07, output=1.20, reasoning=1.20)

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(threshold=900_000, target=700_000)  # 1M ctx
