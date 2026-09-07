"""Cost accounting.

Prices are Decimals end to end. Token counts are integers and prices are
quoted per million tokens, so a float would already be wrong at the fourth
request; budgets are compared against these numbers, and a budget that drifts
is not a budget.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from model_router.config import Price, RouterConfig
from model_router.schemas import Usage

MILLION = Decimal(1_000_000)
CENT_PRECISION = Decimal(
    "0.00000001"
)  # eight places: a single token of a cheap model is ~1.5e-7 USD


def cost_usd(usage: Usage, price: Price) -> Decimal:
    prompt = Decimal(usage.prompt_tokens) * price.input_per_1m / MILLION
    completion = Decimal(usage.completion_tokens) * price.output_per_1m / MILLION
    return (prompt + completion).quantize(CENT_PRECISION, rounding=ROUND_HALF_UP)


class PriceBook:
    def __init__(self, prices: dict[str, Price]) -> None:
        self._prices = dict(prices)

    @classmethod
    def from_config(cls, config: RouterConfig) -> PriceBook:
        return cls(
            {
                f"{p.name}/{model}": price
                for p in config.providers
                for model, price in p.pricing.items()
            }
        )

    def price(self, provider: str, model: str) -> Price:
        try:
            return self._prices[f"{provider}/{model}"]
        except KeyError:
            # Configuration validation guarantees every routed model has a price;
            # reaching this means a provider was used outside a route.
            raise KeyError(f"no price for {provider}/{model}") from None

    def by_key(self) -> dict[str, Price]:
        return dict(self._prices)
