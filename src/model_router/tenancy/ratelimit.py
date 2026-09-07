"""A token bucket per tenant.

`rate` tokens are added per second up to `capacity`; a request takes one. The
bucket is lazy -- refilled at read time from the elapsed interval -- so an idle
tenant costs nothing, and the clock is injected so the refill arithmetic can be
tested to the token without waiting.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: float
    clock: Callable[[], float] = time.monotonic
    tokens: float = -1.0
    updated_at: float = -1.0

    def __post_init__(self) -> None:
        if self.rate_per_s <= 0 or self.capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        if self.tokens < 0:
            self.tokens = self.capacity
        if self.updated_at < 0:
            self.updated_at = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_s)
        self.updated_at = now

    def try_acquire(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def seconds_until(self, n: float = 1.0) -> float:
        """How long until `n` tokens will be available; zero if they are now."""
        self._refill()
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.rate_per_s


class RateLimiter:
    """One bucket per key, created on first sight."""

    def __init__(
        self, requests_per_minute: int, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate_per_s = requests_per_minute / 60.0
        self.burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(
        self, key: str, requests_per_minute: int | None = None, burst: int | None = None
    ) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            rate = (requests_per_minute / 60.0) if requests_per_minute else self.rate_per_s
            bucket = TokenBucket(rate, float(burst or self.burst), clock=self._clock)
            self._buckets[key] = bucket
        return bucket

    def try_acquire(
        self, key: str, requests_per_minute: int | None = None, burst: int | None = None
    ) -> tuple[bool, float]:
        bucket = self.bucket(key, requests_per_minute, burst)
        if bucket.try_acquire():
            return True, 0.0
        return False, bucket.seconds_until()
