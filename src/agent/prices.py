"""What model calls cost, for the daily cost limit (SPEC-0012 B-0010, B-0011).

Prices are flat USD per million tokens: cached-input discounts are ignored, so
spend can only be overstated. A model whose price changes with prompt size is
left out until the table supports tiers. Check a price before changing it, and
update its date.
"""

import math
from dataclasses import dataclass
from datetime import date

MICRO_USD = 1_000_000


@dataclass(frozen=True)
class Price:
    input_per_million: float
    output_per_million: float
    checked: date | None = None


FREE = Price(0, 0)

# (provider, model) -> price.
PRICES: dict[tuple[str, str], Price] = {
    # https://platform.claude.com/docs/en/about-claude/pricing
    ("anthropic", "claude-sonnet-5-5"): Price(2.00, 10.00, date(2026, 11, 8)),
    # https://aistudio.google.com/docs/pricing
    ("google", "gemini-3.5-flash-lite"): Price(0.30, 2.50, date(2026, 11, 8)),
    # https://developers.openai.com/api/docs/pricing
    ("openai", "gpt-6.1-terra"): Price(1.00, 5.00, date(2026, 11, 8)),
}


def price_of(provider: str, model: str) -> Price | None:
    """The model's price; every Ollama model is free. None when unpriced."""
    if provider == "ollama":
        return FREE
    return PRICES.get((provider, model))


def cost_micro_usd(price: Price, input_tokens: int, output_tokens: int) -> int:
    """The cost of one call in whole micro-dollars, rounded up."""
    usd_millionths = (
        input_tokens * price.input_per_million
        + output_tokens * price.output_per_million
    )
    # Price per million tokens x tokens is already in micro-dollars.
    return math.ceil(usd_millionths - 1e-9)
