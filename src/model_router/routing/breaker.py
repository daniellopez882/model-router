"""A circuit breaker per provider.

Closed: requests flow; consecutive failures are counted. Open: requests are
refused without being sent, until the recovery timeout passes. Half-open: a
bounded number of trial requests are let through; one success closes the
breaker, one failure re-opens it.

The clock is injected so the state machine can be tested exhaustively without
sleeping, and so the same breaker works under any event loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_timeout_s <= 0 or half_open_max_calls < 1:
            raise ValueError("breaker parameters must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> BreakerState:
        # The open -> half-open transition is driven by time, not by an event,
        # so it is resolved whenever the state is observed.
        if (
            self._state is BreakerState.OPEN
            and self._clock() - self._opened_at >= self.recovery_timeout_s
        ):
            self._state = BreakerState.HALF_OPEN
            self._half_open_calls = 0
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def allow(self) -> bool:
        """Whether a request may be sent now. Half-open trials are counted here."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.OPEN:
            return False
        if self._half_open_calls < self.half_open_max_calls:
            self._half_open_calls += 1
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._state = BreakerState.CLOSED
        self._half_open_calls = 0

    def record_failure(self) -> None:
        state = self.state
        if state is BreakerState.HALF_OPEN:
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        self._half_open_calls = 0

    def seconds_until_retry(self) -> float:
        if self.state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self.recovery_timeout_s - (self._clock() - self._opened_at))
