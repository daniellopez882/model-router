"""Token-bucket arithmetic against an injected clock."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from model_router.tenancy.ratelimit import RateLimiter, TokenBucket
from tests.conftest import FakeClock


class TestTokenBucket:
    def test_starts_full_and_drains(self, clock: FakeClock) -> None:
        bucket = TokenBucket(rate_per_s=1.0, capacity=3, clock=clock)
        assert [bucket.try_acquire() for _ in range(4)] == [True, True, True, False]

    def test_refills_at_the_rate(self, clock: FakeClock) -> None:
        bucket = TokenBucket(rate_per_s=2.0, capacity=2, clock=clock)
        assert bucket.try_acquire() and bucket.try_acquire()
        assert not bucket.try_acquire()
        clock.advance(0.5)  # one token back
        assert bucket.try_acquire()
        assert not bucket.try_acquire()

    def test_never_exceeds_capacity(self, clock: FakeClock) -> None:
        bucket = TokenBucket(rate_per_s=100.0, capacity=2, clock=clock)
        clock.advance(1000)
        assert bucket.try_acquire() and bucket.try_acquire()
        assert not bucket.try_acquire()

    def test_seconds_until_is_exact(self, clock: FakeClock) -> None:
        bucket = TokenBucket(rate_per_s=4.0, capacity=1, clock=clock)
        assert bucket.seconds_until() == 0
        bucket.try_acquire()
        assert bucket.seconds_until() == pytest.approx(0.25)

    def test_rejects_nonpositive_parameters(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_s=0, capacity=1, clock=clock)


class TestRateLimiter:
    def test_buckets_are_per_key(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60, burst=1, clock=clock)
        assert limiter.try_acquire("a")[0]
        assert not limiter.try_acquire("a")[0]
        assert limiter.try_acquire("b")[0]

    def test_a_key_can_override_the_defaults(self, clock: FakeClock) -> None:
        limiter = RateLimiter(requests_per_minute=60, burst=1, clock=clock)
        assert limiter.try_acquire("big", requests_per_minute=600, burst=3)[0]
        assert limiter.try_acquire("big")[0]
        assert limiter.try_acquire("big")[0]
        allowed, wait = limiter.try_acquire("big")
        assert not allowed and wait == pytest.approx(0.1)


@settings(max_examples=200, deadline=None)
@given(
    rate=st.floats(0.1, 100),
    capacity=st.floats(1, 50),
    steps=st.lists(st.tuples(st.floats(0, 5), st.integers(0, 10)), max_size=40),
)
def test_admitted_requests_never_exceed_capacity_plus_refill(
    rate: float, capacity: float, steps: list[tuple[float, int]]
) -> None:
    """Over any interval, admissions <= capacity + rate * elapsed.

    That inequality is the defining bound of a token bucket.
    """
    clock = FakeClock()
    bucket = TokenBucket(rate_per_s=rate, capacity=capacity, clock=clock)
    admitted = 0
    elapsed = 0.0
    for gap, attempts in steps:
        clock.advance(gap)
        elapsed += gap
        for _ in range(attempts):
            if bucket.try_acquire():
                admitted += 1
        assert admitted <= capacity + rate * elapsed + 1e-6
        assert 0 <= bucket.tokens <= capacity + 1e-9
