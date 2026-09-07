"""The breaker state machine, example by example and then for every sequence."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from model_router.routing.breaker import BreakerState, CircuitBreaker
from tests.conftest import FakeClock


def make(
    clock: FakeClock, threshold: int = 3, recovery: float = 10.0, half_open: int = 1
) -> CircuitBreaker:
    return CircuitBreaker(
        failure_threshold=threshold,
        recovery_timeout_s=recovery,
        half_open_max_calls=half_open,
        clock=clock,
    )


class TestTransitions:
    def test_starts_closed_and_allows(self, clock: FakeClock) -> None:
        breaker = make(clock)
        assert breaker.state is BreakerState.CLOSED
        assert breaker.allow()

    def test_opens_after_threshold_consecutive_failures(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert not breaker.allow()

    def test_a_success_resets_the_failure_count(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is BreakerState.CLOSED

    def test_half_open_after_the_recovery_timeout(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=1, recovery=10)
        breaker.record_failure()
        assert not breaker.allow()
        clock.advance(9.9)
        assert not breaker.allow()
        clock.advance(0.1)
        assert breaker.state is BreakerState.HALF_OPEN
        assert breaker.allow()  # the single trial
        assert not breaker.allow()  # and no more until it resolves

    def test_half_open_success_closes(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=1, recovery=10)
        breaker.record_failure()
        clock.advance(10)
        assert breaker.allow()
        breaker.record_success()
        assert breaker.state is BreakerState.CLOSED
        assert breaker.consecutive_failures == 0

    def test_half_open_failure_reopens_for_a_full_timeout(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=1, recovery=10)
        breaker.record_failure()
        clock.advance(10)
        assert breaker.allow()
        breaker.record_failure()
        assert breaker.state is BreakerState.OPEN
        assert breaker.seconds_until_retry() == 10
        clock.advance(5)
        assert not breaker.allow()

    def test_seconds_until_retry_counts_down(self, clock: FakeClock) -> None:
        breaker = make(clock, threshold=1, recovery=10)
        assert breaker.seconds_until_retry() == 0
        breaker.record_failure()
        clock.advance(4)
        assert breaker.seconds_until_retry() == 6

    def test_parameters_must_be_positive(self, clock: FakeClock) -> None:
        import pytest

        with pytest.raises(ValueError):
            make(clock, threshold=0)
        with pytest.raises(ValueError):
            make(clock, recovery=0)


# -- the invariants, for every sequence of events -----------------------------

Event = st.sampled_from(["ok", "fail", "tick"])


@settings(max_examples=300, deadline=None)
@given(
    events=st.lists(Event, max_size=60), threshold=st.integers(1, 5), recovery=st.floats(0.5, 20)
)
def test_an_open_breaker_never_lets_a_request_through_before_its_timeout(
    events: list[str], threshold: int, recovery: float
) -> None:
    clock = FakeClock()
    breaker = make(clock, threshold=threshold, recovery=recovery)
    opened_at: float | None = None
    for event in events:
        if event == "tick":
            clock.advance(recovery / 3)
        elif event == "fail":
            breaker.record_failure()
        else:
            breaker.record_success()
        state = breaker.state
        if state is BreakerState.OPEN:
            if opened_at is None:
                opened_at = clock.now
            assert not breaker.allow()
        else:
            opened_at = None
        # Closed means the failure count is below threshold, always.
        if state is BreakerState.CLOSED:
            assert breaker.consecutive_failures < threshold


@settings(max_examples=200, deadline=None)
@given(failures=st.integers(0, 20), threshold=st.integers(1, 10))
def test_closed_until_exactly_threshold_failures(failures: int, threshold: int) -> None:
    breaker = make(FakeClock(), threshold=threshold)
    for _ in range(failures):
        breaker.record_failure()
    assert (breaker.state is BreakerState.OPEN) == (failures >= threshold)
