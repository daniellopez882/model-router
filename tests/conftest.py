"""Shared fixtures: an in-process fake upstream reached over real HTTP
semantics (httpx's ASGI transport), an in-memory database, and an app whose
every collaborator is injected. Nothing in the suite opens a socket."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from model_router import db, fake_upstream
from model_router.api.app import create_app
from model_router.config import (
    BreakerConfig,
    Candidate,
    LimitsConfig,
    Price,
    ProviderConfig,
    ProviderKind,
    Route,
    RouterConfig,
    Settings,
    Strategy,
)
from model_router.providers.base import Provider, ProviderError
from model_router.providers.openai_compat import OpenAICompatProvider
from model_router.routing.router import Router
from model_router.schemas import ChatChunk, ChatRequest, ChatResponse, Choice, Message, Usage

ADMIN_TOKEN = "test-admin-token"


# -- clocks and sleepers ------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# -- a scripted provider for router-level tests ------------------------------


class ScriptedProvider:
    """Answers from a script of responses and exceptions, in order, and records calls."""

    def __init__(
        self,
        name: str,
        script: list[Any] | None = None,
        latency_s: float = 0.0,
        clock: FakeClock | None = None,
    ) -> None:
        self.name = name
        self.script = list(script or [])
        self.calls: list[tuple[str, ChatRequest]] = []
        self.latency_s = latency_s
        self.clock = clock
        self.closed = False

    def _next(self, model: str, request: ChatRequest) -> Any:
        self.calls.append((model, request))
        if self.clock is not None:
            self.clock.advance(self.latency_s)
        if not self.script:
            return ok_response(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def chat(self, request: ChatRequest, model: str) -> ChatResponse:
        result = self._next(model, request)
        assert isinstance(result, ChatResponse)
        return result

    async def stream(self, request: ChatRequest, model: str) -> AsyncIterator[ChatChunk]:
        result = self._next(model, request)
        if isinstance(result, ChatResponse):
            text = result.choices[0].message.text()
            for word in text.split(" "):
                yield ChatChunk(
                    id=result.id,
                    created=result.created,
                    model=model,
                    choices=[{"index": 0, "delta": {"content": word + " "}}],
                )  # type: ignore[list-item]
            yield ChatChunk(
                id=result.id,
                created=result.created,
                model=model,
                choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                usage=result.usage,
            )  # type: ignore[list-item]
        else:  # a list of chunks, possibly ending in an exception
            for chunk in result:
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

    async def aclose(self) -> None:
        self.closed = True


def ok_response(
    model: str, text: str = "hello there", prompt: int = 10, completion: int = 2
) -> ChatResponse:
    return ChatResponse.new(
        model,
        [Choice(index=0, message=Message(role="assistant", content=text), finish_reason="stop")],
        Usage.of(prompt, completion),
    )


def retryable(status: int = 503) -> ProviderError:
    return ProviderError(f"upstream returned {status}", retryable=True, status=status)


def final(status: int = 400) -> ProviderError:
    return ProviderError(f"upstream returned {status}", retryable=False, status=status)


def request(model: str = "r", text: str = "hi", **extra: Any) -> ChatRequest:
    return ChatRequest(model=model, messages=[Message(role="user", content=text)], **extra)


# -- configuration ------------------------------------------------------------


def price(i: str = "1.00", o: str = "2.00") -> Price:
    return Price(input_per_1m=Decimal(i), output_per_1m=Decimal(o))


def make_config(
    routes: list[Route] | None = None,
    providers: list[ProviderConfig] | None = None,
    breaker: BreakerConfig | None = None,
) -> RouterConfig:
    providers = providers or [
        ProviderConfig(
            name="a",
            kind=ProviderKind.OPENAI,
            base_url="http://a/v1",
            pricing={"m1": price(), "m2": price("0.10", "0.20")},
        ),
        ProviderConfig(
            name="b",
            kind=ProviderKind.OPENAI,
            base_url="http://b/v1",
            pricing={"m1": price("5", "5"), "m3": price("0", "0")},
        ),
    ]
    routes = routes or [
        Route(
            alias="r",
            strategy=Strategy.PRIORITY,
            candidates=[Candidate(provider="a", model="m1"), Candidate(provider="b", model="m1")],
            max_retries=1,
        ),
    ]
    return RouterConfig(
        providers=providers,
        routes=routes,
        breaker=breaker or BreakerConfig(failure_threshold=2, recovery_timeout_s=10),
        limits=LimitsConfig(requests_per_minute=600, burst=100),
    )


def make_router(
    config: RouterConfig, providers: dict[str, Provider], clock: FakeClock, seed: int = 7
) -> Router:
    return Router(config, providers, clock=clock, sleep=no_sleep, rng=random.Random(seed))


# -- HTTP-level fixtures: fake upstream over ASGI, in-memory DB, the app -------


@pytest.fixture
def fake_client() -> Iterator[httpx.AsyncClient]:
    fake_upstream.reset_flaky()
    transport = httpx.ASGITransport(app=fake_upstream.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://fake-upstream", timeout=5.0)
    yield client


@pytest.fixture
def api_config() -> RouterConfig:
    fake = ProviderConfig(
        name="fake",
        kind=ProviderKind.OPENAI,
        base_url="http://fake-upstream/v1",
        pricing={
            "fake-small": price("1.00", "2.00"),
            "fake-large": price("10.00", "20.00"),
            "fail-500": price(),
            "fail-429": price(),
            "fail-400": price(),
            "flaky-1": price(),
        },
    )
    return RouterConfig(
        providers=[fake],
        routes=[
            Route(alias="demo", candidates=[Candidate(provider="fake", model="fake-small")]),
            Route(alias="expensive", candidates=[Candidate(provider="fake", model="fake-large")]),
            Route(
                alias="broken",
                candidates=[Candidate(provider="fake", model="fail-500")],
                max_retries=0,
            ),
            Route(
                alias="throttled",
                candidates=[Candidate(provider="fake", model="fail-429")],
                max_retries=0,
            ),
            Route(
                alias="rejected",
                candidates=[Candidate(provider="fake", model="fail-400")],
                max_retries=0,
            ),
            Route(
                alias="recovering",
                candidates=[
                    Candidate(provider="fake", model="flaky-1"),
                    Candidate(provider="fake", model="fake-small"),
                ],
                max_retries=0,
            ),
        ],
        breaker=BreakerConfig(failure_threshold=100, recovery_timeout_s=1),
        limits=LimitsConfig(requests_per_minute=600, burst=1000),
    )


@pytest.fixture
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def client(
    api_config: RouterConfig, fake_client: httpx.AsyncClient, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    providers: dict[str, Provider] = {
        "fake": OpenAICompatProvider(
            "fake", "http://fake-upstream/v1", api_key=None, client=fake_client
        )
    }
    router = Router(api_config, providers, sleep=no_sleep)
    settings = Settings(
        ENV="test", ADMIN_TOKEN=ADMIN_TOKEN, DATABASE_URL="sqlite://", _env_file=None
    )  # type: ignore[call-arg]
    app = create_app(
        settings,
        config=api_config,
        providers=providers,
        router=router,
        session_factory=session_factory,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin() -> dict[str, str]:
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture
def tenant(client: TestClient, admin: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/admin/tenants", json={"name": "acme", "monthly_budget_usd": "1.00"}, headers=admin
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


@pytest.fixture
def auth(tenant: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant['api_key']}"}
