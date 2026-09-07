"""Turning a route's candidate list into the order to try them in.

Every strategy returns the *whole* list reordered, never a single pick: the
router walks the list on failure, so a strategy is a preference, not a
decision. The latency tracker is an exponentially weighted moving average per
candidate, which forgets a bad minute in a few dozen requests and needs no
storage beyond one float per candidate.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from decimal import Decimal

from model_router.config import Candidate, Price, Strategy


class LatencyTracker:
    def __init__(self, alpha: float = 0.2) -> None:
        if not 0 < alpha <= 1:
            raise ValueError("alpha must be in (0, 1]")
        self.alpha = alpha
        self._ewma: dict[str, float] = {}

    def observe(self, key: str, latency_ms: float) -> None:
        previous = self._ewma.get(key)
        self._ewma[key] = (
            latency_ms if previous is None else previous + self.alpha * (latency_ms - previous)
        )

    def get(self, key: str) -> float | None:
        return self._ewma.get(key)

    def snapshot(self) -> dict[str, float]:
        return dict(self._ewma)


class RoundRobinCounter:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def next(self, route_alias: str, size: int) -> int:
        current = self._counters.get(route_alias, 0)
        self._counters[route_alias] = (current + 1) % max(size, 1)
        return current % max(size, 1)


def order_candidates(
    strategy: Strategy,
    route_alias: str,
    candidates: Sequence[Candidate],
    *,
    tracker: LatencyTracker,
    prices: dict[str, Price],
    rng: random.Random,
    rr: RoundRobinCounter,
) -> list[Candidate]:
    items = list(candidates)
    if len(items) <= 1:
        return items

    if strategy is Strategy.PRIORITY:
        return items

    if strategy is Strategy.ROUND_ROBIN:
        start = rr.next(route_alias, len(items))
        return items[start:] + items[:start]

    if strategy is Strategy.WEIGHTED_RANDOM:
        # Sample without replacement, weight-proportionally, so the order is a
        # full preference list rather than one pick plus the tail in file order.
        remaining = items[:]
        ordered: list[Candidate] = []
        while remaining:
            pick = rng.choices(remaining, weights=[c.weight for c in remaining], k=1)[0]
            ordered.append(pick)
            remaining.remove(pick)
        return ordered

    if strategy is Strategy.LEAST_LATENCY:
        # A candidate with no observation yet sorts first: the only way to learn
        # its latency is to send it something. Ties keep file order.
        def latency(c: Candidate) -> float:
            observed = tracker.get(c.key)
            return -1.0 if observed is None else observed

        return sorted(items, key=latency)

    if strategy is Strategy.CHEAPEST:

        def cost(c: Candidate) -> Decimal:
            price = prices.get(c.key)
            if price is None:
                return Decimal("Infinity")
            return price.input_per_1m + price.output_per_1m

        return sorted(items, key=cost)

    raise ValueError(f"unknown strategy {strategy!r}")  # pragma: no cover - enum is closed
