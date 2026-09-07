"""The router's decisions: fallback, retry, breaker interaction, accounting."""

from __future__ import annotations

from decimal import Decimal

import pytest

from model_router.config import Candidate, Route, Strategy
from model_router.routing.breaker import BreakerState
from model_router.routing.router import NoRouteAvailable, UnknownRoute
from model_router.schemas import ChatChunk
from tests.conftest import (
    FakeClock,
    ScriptedProvider,
    final,
    make_config,
    make_router,
    ok_response,
    request,
    retryable,
)


class TestComplete:
    async def test_first_candidate_answers_and_cost_is_recorded(self, clock: FakeClock) -> None:
        a, b = (
            ScriptedProvider("a", [ok_response("m1", prompt=1_000, completion=500)]),
            ScriptedProvider("b"),
        )
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        routed = await router.complete(request())
        assert routed.provider == "a" and routed.model == "m1"
        assert routed.cost_usd == Decimal("0.00200000")  # 1000*1/M + 500*2/M
        assert routed.response.model == "r"  # the alias, not the upstream model
        assert [x.outcome for x in routed.attempts] == ["ok"]
        assert b.calls == []

    async def test_retryable_failure_is_retried_then_falls_back(self, clock: FakeClock) -> None:
        a = ScriptedProvider("a", [retryable(503), retryable(503)])
        b = ScriptedProvider("b", [ok_response("m1")])
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        routed = await router.complete(request())
        assert routed.provider == "b"
        assert [(x.provider, x.outcome) for x in routed.attempts] == [
            ("a", "error"),
            ("a", "error"),
            ("b", "ok"),
        ]
        assert len(a.calls) == 2  # one retry, as configured

    async def test_a_final_failure_is_not_retried_but_still_falls_back(
        self, clock: FakeClock
    ) -> None:
        a = ScriptedProvider("a", [final(400)])
        b = ScriptedProvider("b", [ok_response("m1")])
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        routed = await router.complete(request())
        assert routed.provider == "b"
        assert len(a.calls) == 1

    async def test_no_fallback_surfaces_the_first_failure(self, clock: FakeClock) -> None:
        cfg = make_config(
            routes=[
                Route(
                    alias="r",
                    candidates=[
                        Candidate(provider="a", model="m1"),
                        Candidate(provider="b", model="m1"),
                    ],
                    fallback=False,
                    max_retries=0,
                )
            ]
        )
        a, b = ScriptedProvider("a", [retryable(503)]), ScriptedProvider("b")
        router = make_router(cfg, {"a": a, "b": b}, clock)
        with pytest.raises(NoRouteAvailable) as info:
            await router.complete(request())
        assert len(info.value.attempts) == 1 and b.calls == []

    async def test_every_candidate_failing_raises_with_the_full_story(
        self, clock: FakeClock
    ) -> None:
        a, b = ScriptedProvider("a", [final(400)]), ScriptedProvider("b", [final(401)])
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        with pytest.raises(NoRouteAvailable) as info:
            await router.complete(request())
        assert [(x.provider, x.status) for x in info.value.attempts] == [("a", 400), ("b", 401)]
        assert not info.value.all_transient

    async def test_all_transient_is_true_only_for_later_shaped_failures(
        self, clock: FakeClock
    ) -> None:
        a, b = (
            ScriptedProvider("a", [retryable(429), retryable(429)]),
            ScriptedProvider("b", [retryable(503), retryable(503)]),
        )
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        with pytest.raises(NoRouteAvailable) as info:
            await router.complete(request())
        assert info.value.all_transient

    async def test_unknown_alias(self, clock: FakeClock) -> None:
        router = make_router(
            make_config(), {"a": ScriptedProvider("a"), "b": ScriptedProvider("b")}, clock
        )
        with pytest.raises(UnknownRoute):
            await router.complete(request(model="nope"))

    async def test_missing_adapter_is_a_construction_error(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError, match="no adapter"):
            make_router(make_config(), {"a": ScriptedProvider("a")}, clock)


class TestBreakers:
    async def test_breaker_opens_and_the_provider_is_skipped_without_a_call(
        self, clock: FakeClock
    ) -> None:
        # threshold is 2 in make_config; max_retries=1 means one request produces two failures
        a = ScriptedProvider("a", [retryable(503), retryable(503), ok_response("m1")])
        b = ScriptedProvider("b", [ok_response("m1"), ok_response("m1")])
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        await router.complete(request())
        assert router.breaker_states()["a"] is BreakerState.OPEN
        routed = await router.complete(request())
        assert routed.attempts[0].outcome == "skipped_open_breaker"
        assert routed.provider == "b"
        assert len(a.calls) == 2  # not called while open

    async def test_breaker_recovers_after_the_timeout(self, clock: FakeClock) -> None:
        a = ScriptedProvider("a", [retryable(503), retryable(503), ok_response("m1")])
        b = ScriptedProvider("b")
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        await router.complete(request())
        clock.advance(10)  # recovery_timeout_s in make_config
        routed = await router.complete(request())
        assert routed.provider == "a"
        assert router.breaker_states()["a"] is BreakerState.CLOSED


class TestLatencyAndStrategies:
    async def test_least_latency_learns_from_observations(self, clock: FakeClock) -> None:
        cfg = make_config(
            routes=[
                Route(
                    alias="r",
                    strategy=Strategy.LEAST_LATENCY,
                    candidates=[
                        Candidate(provider="a", model="m1"),
                        Candidate(provider="b", model="m1"),
                    ],
                )
            ]
        )
        a = ScriptedProvider("a", latency_s=0.9, clock=clock)
        b = ScriptedProvider("b", latency_s=0.1, clock=clock)
        router = make_router(cfg, {"a": a, "b": b}, clock)
        first = await router.complete(request())  # unknowns first: file order -> a
        second = await router.complete(request())  # b still unknown -> b
        third = await router.complete(request())  # both known: b is faster
        assert [first.provider, second.provider, third.provider] == ["a", "b", "b"]
        assert router.tracker.get("a/m1") == pytest.approx(900)

    async def test_cheapest_prefers_the_cheaper_model(self, clock: FakeClock) -> None:
        cfg = make_config(
            routes=[
                Route(
                    alias="r",
                    strategy=Strategy.CHEAPEST,
                    candidates=[
                        Candidate(provider="a", model="m1"),
                        Candidate(provider="b", model="m3"),
                    ],
                )
            ]
        )
        router = make_router(cfg, {"a": ScriptedProvider("a"), "b": ScriptedProvider("b")}, clock)
        routed = await router.complete(request())
        assert (routed.provider, routed.model) == ("b", "m3")


class TestStream:
    async def test_streams_relabelled_with_the_alias(self, clock: FakeClock) -> None:
        a = ScriptedProvider("a", [ok_response("m1", "one two three")])
        router = make_router(make_config(), {"a": a, "b": ScriptedProvider("b")}, clock)
        stream = await router.stream(request())
        chunks = [c async for c in stream.chunks]
        assert all(c.model == "r" for c in chunks)
        assert (
            "".join(c.choices[0].delta.get("content", "") for c in chunks).strip()
            == "one two three"
        )
        assert chunks[-1].usage is not None and chunks[-1].choices[0].finish_reason == "stop"

    async def test_falls_back_before_the_first_chunk(self, clock: FakeClock) -> None:
        a = ScriptedProvider("a", [retryable(503)])
        b = ScriptedProvider("b", [ok_response("m1", "from b")])
        router = make_router(make_config(), {"a": a, "b": b}, clock)
        stream = await router.stream(request())
        assert stream.provider == "b"
        text = "".join([c.choices[0].delta.get("content", "") async for c in stream.chunks])
        assert text.strip() == "from b"

    async def test_a_mid_stream_failure_propagates(self, clock: FakeClock) -> None:
        chunk = ChatChunk(
            id="x", created=0, model="m1", choices=[{"index": 0, "delta": {"content": "partial"}}]
        )  # type: ignore[list-item]
        a = ScriptedProvider("a", [[chunk, retryable(502)]])
        router = make_router(make_config(), {"a": a, "b": ScriptedProvider("b")}, clock)
        stream = await router.stream(request())
        received = []
        with pytest.raises(Exception, match="502"):
            async for c in stream.chunks:
                received.append(c)
        assert len(received) == 1  # the client got what arrived before the failure
