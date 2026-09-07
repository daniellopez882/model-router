"""The router: pick a provider, send the request, retry and fall back on the
failures that deserve it, and account for what happened.

Two rules do most of the work:

* A failure moves to the next candidate only if it is *retryable*. A 400 from
  one provider is a 400 from all of them; sending it three more times costs
  money and tells the caller nothing new.
* A provider whose breaker is open is skipped without a network call. The
  breaker is per provider, not per route, because an outage is a property of
  the provider.

Streaming can only fall back before the first chunk. Once bytes have reached
the client, switching provider would splice two different answers together,
so a mid-stream failure is surfaced as the stream ending with an error event.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from model_router.config import Candidate, Price, Route, RouterConfig
from model_router.pricing import PriceBook, cost_usd
from model_router.providers.base import Provider, ProviderError
from model_router.routing.breaker import BreakerState, CircuitBreaker
from model_router.routing.policy import LatencyTracker, RoundRobinCounter, order_candidates
from model_router.schemas import ChatChunk, ChatRequest, ChatResponse, Usage


@dataclass(frozen=True)
class Attempt:
    provider: str
    model: str
    outcome: str  # "ok" | "error" | "skipped_open_breaker"
    latency_ms: float
    error: str | None = None
    status: int | None = None


@dataclass
class RoutedResponse:
    response: ChatResponse
    provider: str
    model: str
    route: str
    latency_ms: float
    cost_usd: Decimal
    attempts: list[Attempt] = field(default_factory=list)


@dataclass
class RoutedStream:
    chunks: AsyncIterator[ChatChunk]
    provider: str
    model: str
    route: str
    attempts: list[Attempt] = field(default_factory=list)


class NoRouteAvailable(Exception):
    """Every candidate failed or was skipped. `attempts` says why, per candidate."""

    def __init__(self, route: str, attempts: list[Attempt]) -> None:
        self.route = route
        self.attempts = attempts
        summary = "; ".join(
            f"{a.provider}/{a.model}: {a.outcome}" + (f" ({a.error})" if a.error else "")
            for a in attempts
        )
        super().__init__(f"no provider could serve route {route!r}: {summary}")
        # The caller gets a 502 unless every attempt was rejected for a reason
        # that says "later", in which case a 503 with Retry-After is truer.
        self.all_transient = bool(attempts) and all(
            a.outcome == "skipped_open_breaker" or a.status in (429, 503) for a in attempts
        )


class UnknownRoute(Exception):
    pass


Sleeper = Callable[[float], Awaitable[None]]


class Router:
    def __init__(
        self,
        config: RouterConfig,
        providers: dict[str, Provider],
        *,
        pricebook: PriceBook | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        rng: random.Random | None = None,
        backoff_base_s: float = 0.2,
    ) -> None:
        self.config = config
        self.providers = providers
        missing = {c.provider for r in config.routes for c in r.candidates} - set(providers)
        if missing:
            raise ValueError(f"no adapter for providers: {sorted(missing)}")
        self.pricebook = pricebook or PriceBook.from_config(config)
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(
                failure_threshold=config.breaker.failure_threshold,
                recovery_timeout_s=config.breaker.recovery_timeout_s,
                half_open_max_calls=config.breaker.half_open_max_calls,
                clock=clock,
            )
            for name in providers
        }
        self.tracker = LatencyTracker()
        self._rr = RoundRobinCounter()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._backoff_base_s = backoff_base_s

    # -- public -------------------------------------------------------------

    def route_for(self, alias: str) -> Route:
        route = self.config.route(alias)
        if route is None:
            raise UnknownRoute(alias)
        return route

    def aliases(self) -> list[str]:
        return [r.alias for r in self.config.routes]

    def breaker_states(self) -> dict[str, BreakerState]:
        return {name: b.state for name, b in self.breakers.items()}

    async def complete(self, request: ChatRequest) -> RoutedResponse:
        route = self.route_for(request.model)
        attempts: list[Attempt] = []
        for candidate in self._ordered(route):
            provider = self.providers[candidate.provider]
            breaker = self.breakers[candidate.provider]
            if not breaker.allow():
                attempts.append(
                    Attempt(candidate.provider, candidate.model, "skipped_open_breaker", 0.0)
                )
                if not route.fallback:
                    break
                continue
            for retry in range(route.max_retries + 1):
                started = self._clock()
                try:
                    response = await provider.chat(request, candidate.model)
                except ProviderError as error:
                    latency = (self._clock() - started) * 1000
                    attempts.append(
                        Attempt(
                            candidate.provider,
                            candidate.model,
                            "error",
                            latency,
                            str(error),
                            error.status,
                        )
                    )
                    breaker.record_failure()
                    if error.retryable and retry < route.max_retries:
                        await self._sleep(self._backoff(retry))
                        continue
                    break  # next candidate, or give up
                latency = (self._clock() - started) * 1000
                breaker.record_success()
                self.tracker.observe(candidate.key, latency)
                attempts.append(Attempt(candidate.provider, candidate.model, "ok", latency))
                usage = response.usage or Usage()
                cost = cost_usd(usage, self.pricebook.price(candidate.provider, candidate.model))
                # The client asked for the alias; tell it what actually answered
                # via headers, but keep the alias in the body so SDKs that echo
                # the model name back into the next request keep working.
                response.model = request.model
                return RoutedResponse(
                    response,
                    candidate.provider,
                    candidate.model,
                    route.alias,
                    latency,
                    cost,
                    attempts,
                )
            if not route.fallback:
                break
        raise NoRouteAvailable(route.alias, attempts)

    async def stream(self, request: ChatRequest) -> RoutedStream:
        """Open a stream, falling back until the first chunk has arrived."""
        route = self.route_for(request.model)
        attempts: list[Attempt] = []
        for candidate in self._ordered(route):
            provider = self.providers[candidate.provider]
            breaker = self.breakers[candidate.provider]
            if not breaker.allow():
                attempts.append(
                    Attempt(candidate.provider, candidate.model, "skipped_open_breaker", 0.0)
                )
                if not route.fallback:
                    break
                continue
            started = self._clock()
            iterator = provider.stream(request, candidate.model)
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                first = None
            except ProviderError as error:
                latency = (self._clock() - started) * 1000
                attempts.append(
                    Attempt(
                        candidate.provider,
                        candidate.model,
                        "error",
                        latency,
                        str(error),
                        error.status,
                    )
                )
                breaker.record_failure()
                if not route.fallback:
                    break
                continue
            latency = (self._clock() - started) * 1000
            attempts.append(Attempt(candidate.provider, candidate.model, "ok", latency))
            breaker.record_success()
            self.tracker.observe(candidate.key, latency)
            return RoutedStream(
                self._relabel(first, iterator, request.model),
                candidate.provider,
                candidate.model,
                route.alias,
                attempts,
            )
        raise NoRouteAvailable(route.alias, attempts)

    # -- internals ----------------------------------------------------------

    def _ordered(self, route: Route) -> list[Candidate]:
        return order_candidates(
            route.strategy,
            route.alias,
            route.candidates,
            tracker=self.tracker,
            prices=self.pricebook.by_key(),
            rng=self._rng,
            rr=self._rr,
        )

    def _backoff(self, retry: int) -> float:
        # Full jitter: uniform in [0, base * 2^retry]. Bounded so a misconfigured
        # retry count cannot turn into a long sleep inside a request.
        return self._rng.uniform(0, min(self._backoff_base_s * (2**retry), 2.0))

    @staticmethod
    async def _relabel(
        first: ChatChunk | None, rest: AsyncIterator[ChatChunk], alias: str
    ) -> AsyncIterator[ChatChunk]:
        if first is not None:
            first.model = alias
            yield first
        async for chunk in rest:
            chunk.model = alias
            yield chunk


def prices_for(config: RouterConfig) -> dict[str, Price]:
    return {
        f"{p.name}/{model}": price for p in config.providers for model, price in p.pricing.items()
    }
