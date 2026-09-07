from __future__ import annotations

import random
from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from model_router.config import Candidate, Strategy
from model_router.routing.policy import LatencyTracker, RoundRobinCounter, order_candidates
from tests.conftest import price

A, B, C = (
    Candidate(provider="a", model="m"),
    Candidate(provider="b", model="m"),
    Candidate(provider="c", model="m"),
)
PRICES = {"a/m": price("1", "1"), "b/m": price("0.1", "0.1"), "c/m": price("5", "5")}


def order(
    strategy: Strategy,
    candidates: list[Candidate],
    tracker: LatencyTracker | None = None,
    seed: int = 1,
    rr: RoundRobinCounter | None = None,
) -> list[str]:
    ordered = order_candidates(
        strategy,
        "r",
        candidates,
        tracker=tracker or LatencyTracker(),
        prices=PRICES,
        rng=random.Random(seed),
        rr=rr or RoundRobinCounter(),
    )
    return [c.provider for c in ordered]


def test_priority_keeps_file_order() -> None:
    assert order(Strategy.PRIORITY, [C, A, B]) == ["c", "a", "b"]


def test_round_robin_rotates_the_start_and_keeps_everyone() -> None:
    rr = RoundRobinCounter()
    assert order(Strategy.ROUND_ROBIN, [A, B, C], rr=rr) == ["a", "b", "c"]
    assert order(Strategy.ROUND_ROBIN, [A, B, C], rr=rr) == ["b", "c", "a"]
    assert order(Strategy.ROUND_ROBIN, [A, B, C], rr=rr) == ["c", "a", "b"]
    assert order(Strategy.ROUND_ROBIN, [A, B, C], rr=rr) == ["a", "b", "c"]


def test_cheapest_sorts_by_price_and_unpriced_last() -> None:
    unpriced = Candidate(provider="zzz", model="m")
    assert order(Strategy.CHEAPEST, [A, unpriced, C, B]) == ["b", "a", "c", "zzz"]


def test_least_latency_prefers_the_fastest_and_tries_unknowns_first() -> None:
    tracker = LatencyTracker()
    tracker.observe("a/m", 800)
    tracker.observe("b/m", 100)
    assert order(Strategy.LEAST_LATENCY, [A, B, C], tracker=tracker) == ["c", "b", "a"]


def test_ewma_forgets_a_bad_minute() -> None:
    tracker = LatencyTracker(alpha=0.5)
    tracker.observe("a/m", 1000)
    for _ in range(6):
        tracker.observe("a/m", 100)
    assert tracker.get("a/m") is not None and tracker.get("a/m") < 120  # type: ignore[operator]


def test_weighted_random_follows_the_weights() -> None:
    heavy, light = (
        Candidate(provider="h", model="m", weight=9),
        Candidate(provider="l", model="m", weight=1),
    )
    firsts = Counter(order(Strategy.WEIGHTED_RANDOM, [light, heavy], seed=s)[0] for s in range(500))
    assert 0.8 < firsts["h"] / 500 < 0.98


@settings(max_examples=200, deadline=None)
@given(
    strategy=st.sampled_from(list(Strategy)),
    names=st.lists(st.sampled_from(["a", "b", "c", "d"]), min_size=1, max_size=4, unique=True),
    seed=st.integers(0, 10_000),
)
def test_every_strategy_returns_a_permutation(
    strategy: Strategy, names: list[str], seed: int
) -> None:
    candidates = [Candidate(provider=n, model="m") for n in names]
    result = order(strategy, candidates, seed=seed)
    assert sorted(result) == sorted(names)
