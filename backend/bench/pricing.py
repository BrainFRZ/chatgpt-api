"""Cost metering with a hard per-provider spend cap.

The cap is a safety backstop so a benchmark run can't burn the user's API
balance: before/after every call we tally actual cost from the provider's own
pricing, and raise BudgetExceeded the moment a provider crosses its ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    """Raised when a provider's accumulated spend would exceed its cap."""


def cost_of(provider, usage: dict) -> float:
    """Compute the USD cost of one response from a provider's 'done' usage dict.

    usage carries input_tokens (TOTAL, incl. cache), cache_read_tokens,
    cache_creation_tokens, output_tokens, reasoning_tokens — the same shape
    every provider's send_request_stream emits.
    """
    p = provider.pricing
    inp = int(usage.get("input_tokens", 0) or 0)
    cr = int(usage.get("cache_read_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    rea = int(usage.get("reasoning_tokens", 0) or 0)
    non_cached = max(0, inp - cr - cw)
    return (
        non_cached * p.input_base / 1_000_000
        + cw * p.cache_write / 1_000_000
        + cr * p.cache_read / 1_000_000
        + out * p.output / 1_000_000
        + rea * p.reasoning / 1_000_000
    )


def estimate_cost(provider, input_tokens: int, output_tokens: int) -> float:
    """Conservative (no-cache) cost estimate for the dry run — overestimates so
    funding guidance has headroom."""
    p = provider.pricing
    return input_tokens * p.input_base / 1_000_000 + output_tokens * p.output / 1_000_000


# Default per-provider hard caps (USD). Funding above these just gives headroom;
# the harness aborts a provider's run before it can exceed its cap.
DEFAULT_CAPS = {
    "deepseek-v3.2": 8.0,
    "claude-sonnet-4.6": 20.0,
    "gemini-3.1-pro": 15.0,
}


@dataclass
class SpendMeter:
    caps: dict = field(default_factory=lambda: dict(DEFAULT_CAPS))
    spent: dict = field(default_factory=dict)

    def add(self, model_id: str, provider, usage: dict) -> float:
        c = cost_of(provider, usage)
        self.spent[model_id] = self.spent.get(model_id, 0.0) + c
        cap = self.caps.get(model_id)
        if cap is not None and self.spent[model_id] > cap:
            raise BudgetExceeded(
                f"{model_id} spend ${self.spent[model_id]:.2f} exceeded cap ${cap:.2f}"
            )
        return c

    def total(self) -> float:
        return sum(self.spent.values())
