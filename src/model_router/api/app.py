"""The HTTP surface: OpenAI-compatible completions, model listing, probes,
metrics, and a token-guarded admin API for tenants and keys.

Request handling is a fixed sequence, and each step refuses with the status
that tells the client what to do: 401 (no or unknown key), 403 (revoked),
429 with Retry-After (rate), 402 (budget), 400 (unknown route or bad body),
502 (every provider failed), 503 with Retry-After (every provider is cooling
off or rate-limiting). Bodies never carry provider error text verbatim beyond
a short snippet, and never the prompt.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from model_router import __version__, db, metrics
from model_router.config import ConfigError, RouterConfig, Settings, load_router_config
from model_router.logging_config import log, request_id_var, setup_logging
from model_router.providers import Provider, build_provider
from model_router.routing import NoRouteAvailable, RoutedResponse, Router, UnknownRoute
from model_router.schemas import ChatRequest, ErrorBody, ErrorResponse, ModelCard, ModelList, Usage
from model_router.tenancy import RateLimiter, generate_key, hash_key, key_hint

logger = logging.getLogger("model_router.api")


# -- state --------------------------------------------------------------------


class AppState:
    def __init__(
        self,
        settings: Settings,
        config: RouterConfig,
        router: Router,
        session_factory: sessionmaker[Session],
        limiter: RateLimiter,
    ) -> None:
        self.settings = settings
        self.config = config
        self.router = router
        self.session_factory = session_factory
        self.limiter = limiter


def _state(request: Request) -> AppState:
    state: AppState = request.app.state.router_state
    return state


State = Annotated[AppState, Depends(_state)]


def _error(
    status: int,
    message: str,
    kind: str,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(message=message, type=kind, code=code))
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


class AuthFailure(Exception):
    def __init__(
        self, status: int, message: str, code: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.message = message
        self.code = code
        self.headers = headers


# -- auth ---------------------------------------------------------------------


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def get_tenant(state: State, authorization: Annotated[str | None, Header()] = None) -> db.Tenant:
    raw = _bearer(authorization)
    if raw is None:
        metrics.REJECTED.labels(reason="no_key").inc()
        raise AuthFailure(
            401, "Missing bearer token", "missing_api_key", {"WWW-Authenticate": "Bearer"}
        )
    with state.session_factory() as session:
        key = session.execute(
            select(db.ApiKey).where(db.ApiKey.key_hash == hash_key(raw))
        ).scalar_one_or_none()
        if key is None:
            metrics.REJECTED.labels(reason="unknown_key").inc()
            raise AuthFailure(
                401, "Invalid API key", "invalid_api_key", {"WWW-Authenticate": "Bearer"}
            )
        if key.revoked_at is not None:
            metrics.REJECTED.labels(reason="revoked_key").inc()
            raise AuthFailure(403, "This API key has been revoked", "revoked_api_key")
        tenant = session.get(db.Tenant, key.tenant_id)
        assert tenant is not None  # foreign key
        return tenant


Tenant = Annotated[db.Tenant, Depends(get_tenant)]


def require_admin(state: State, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    import hmac

    expected = state.settings.ADMIN_TOKEN
    if not expected:
        raise AuthFailure(
            503,
            "Admin API is not configured on this server (ADMIN_TOKEN unset)",
            "admin_unconfigured",
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise AuthFailure(401, "Invalid admin token", "invalid_admin_token")


# -- gates --------------------------------------------------------------------


def check_rate(state: AppState, tenant: db.Tenant) -> None:
    allowed, wait = state.limiter.try_acquire(tenant.id, tenant.requests_per_minute, tenant.burst)
    if not allowed:
        metrics.REJECTED.labels(reason="rate_limited").inc()
        raise AuthFailure(
            429,
            "Rate limit exceeded for this tenant",
            "rate_limited",
            {"Retry-After": str(max(1, math.ceil(wait)))},
        )


def check_budget(state: AppState, tenant: db.Tenant) -> Decimal:
    with state.session_factory() as session:
        spent = db.month_spend(session, tenant.id)
    if tenant.monthly_budget_usd is not None and spent >= tenant.monthly_budget_usd:
        metrics.REJECTED.labels(reason="budget_exhausted").inc()
        raise AuthFailure(
            402,
            f"Monthly budget exhausted: spent {spent:.4f} of {tenant.monthly_budget_usd:.4f} USD",
            "budget_exhausted",
        )
    return spent


def record_usage(
    state: AppState,
    tenant: db.Tenant,
    request_id: str,
    route: str,
    provider: str,
    model: str,
    status: str,
    usage: Usage,
    cost: Decimal,
    latency_ms: float,
) -> None:
    with state.session_factory() as session:
        session.add(
            db.UsageRecord(
                tenant_id=tenant.id,
                request_id=request_id,
                route=route,
                provider=provider,
                model=model,
                status=status,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=cost,
                latency_ms=int(latency_ms),
            )
        )
        session.commit()
    metrics.REQUESTS.labels(route=route, provider=provider, status=status).inc()
    if status == "ok":
        metrics.LATENCY.labels(provider=provider, model=model).observe(latency_ms / 1000)
        metrics.TOKENS.labels(provider=provider, model=model, direction="prompt").inc(
            usage.prompt_tokens
        )
        metrics.TOKENS.labels(provider=provider, model=model, direction="completion").inc(
            usage.completion_tokens
        )
        metrics.COST.labels(tenant=tenant.id, provider=provider, model=model).inc(float(cost))


def _observe_attempts(state: AppState, attempts: list[Any]) -> None:
    for attempt in attempts:
        metrics.ATTEMPTS.labels(provider=attempt.provider, outcome=attempt.outcome).inc()
    for provider, breaker_state in state.router.breaker_states().items():
        metrics.BREAKER.labels(provider=provider).set(metrics.BREAKER_VALUES[breaker_state.value])


# -- admin schemas ------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    monthly_budget_usd: Decimal | None = Field(default=None, ge=0)
    requests_per_minute: int | None = Field(default=None, ge=1)
    burst: int | None = Field(default=None, ge=1)


class TenantCreated(BaseModel):
    id: str
    name: str
    monthly_budget_usd: Decimal | None
    api_key: str  # shown once


class KeyCreated(BaseModel):
    id: str
    hint: str
    api_key: str


class UsageSummary(BaseModel):
    tenant_id: str
    month_start: dt.datetime
    spent_usd: Decimal
    monthly_budget_usd: Decimal | None
    requests: int


# -- app ----------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    *,
    config: RouterConfig | None = None,
    providers: dict[str, Provider] | None = None,
    router: Router | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """Build the application. Every collaborator can be injected, which is how
    the tests run the whole HTTP surface against a fake provider and an
    in-memory database."""
    settings = settings or Settings()
    setup_logging(settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = config or load_router_config(settings.ROUTER_CONFIG)
        if session_factory is None:
            db.init_schema(settings.DATABASE_URL)
            factory = db.make_session_factory(db.make_engine(settings.DATABASE_URL))
        else:
            factory = session_factory
        provs = (
            providers
            if providers is not None
            else {p.name: build_provider(p) for p in cfg.providers}
        )
        rt = router or Router(cfg, provs)
        limiter = RateLimiter(cfg.limits.requests_per_minute, cfg.limits.burst)
        app.state.router_state = AppState(settings, cfg, rt, factory, limiter)
        log(
            logger,
            logging.INFO,
            "router started",
            routes=rt.aliases(),
            providers=sorted(provs),
            version=__version__,
        )
        try:
            yield
        finally:
            for provider in provs.values():
                await provider.aclose()

    app = FastAPI(
        title="model-router",
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            if request.method in ("POST", "PUT", "PATCH"):
                length = request.headers.get("content-length")
                if length and length.isdigit() and int(length) > settings.MAX_REQUEST_BYTES:
                    return _error(
                        413, "Request body too large", "invalid_request_error", "body_too_large"
                    )
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(AuthFailure)
    async def auth_failure(_: Request, error: AuthFailure) -> JSONResponse:
        kind = (
            "authentication_error"
            if error.status in (401, 403)
            else "rate_limit_error"
            if error.status == 429
            else "insufficient_quota"
            if error.status == 402
            else "server_error"
        )
        return _error(error.status, error.message, kind, error.code, error.headers)

    @app.exception_handler(ConfigError)
    async def config_failure(_: Request, error: ConfigError) -> JSONResponse:
        return _error(500, "Server configuration error", "server_error", "config_error")

    @app.exception_handler(Exception)
    async def unhandled(_: Request, error: Exception) -> JSONResponse:
        log(logger, logging.ERROR, "unhandled error", error=repr(error))
        return _error(500, "Internal server error", "server_error", request_id_var.get())

    # -- probes ---------------------------------------------------------------

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready", include_in_schema=False)
    async def ready(state: State) -> JSONResponse:
        checks: dict[str, Any] = {"config": True, "providers": sorted(state.router.providers)}
        try:
            with state.session_factory() as session:
                session.execute(select(db.Tenant.id).limit(1))
            checks["database"] = True
        except Exception as error:
            checks["database"] = False
            log(logger, logging.ERROR, "database not ready", error=repr(error))
        checks["breakers"] = {k: v.value for k, v in state.router.breaker_states().items()}
        ok = bool(checks["database"])
        return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, **checks})

    @app.get("/metrics", include_in_schema=False)
    async def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    # -- OpenAI-compatible ----------------------------------------------------

    @app.get("/v1/models")
    async def list_models(state: State, tenant: Tenant) -> ModelList:
        return ModelList(data=[ModelCard(id=alias) for alias in state.router.aliases()])

    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, state: State, tenant: Tenant) -> Response:
        request_id = request_id_var.get()
        check_rate(state, tenant)
        check_budget(state, tenant)
        try:
            state.router.route_for(body.model)
        except UnknownRoute:
            metrics.REJECTED.labels(reason="unknown_route").inc()
            return _error(
                400,
                f"Unknown model alias {body.model!r}",
                "invalid_request_error",
                "model_not_found",
            )

        if body.stream:
            return await _stream(state, tenant, body, request_id)

        try:
            routed = await state.router.complete(body)
        except NoRouteAvailable as error:
            _observe_attempts(state, error.attempts)
            last = error.attempts[-1] if error.attempts else None
            record_usage(
                state,
                tenant,
                request_id,
                body.model,
                last.provider if last else "-",
                last.model if last else "-",
                "error",
                Usage(),
                Decimal(0),
                0,
            )
            log(
                logger,
                logging.WARNING,
                "no route available",
                route=body.model,
                attempts=[a.__dict__ for a in error.attempts],
            )
            if error.all_transient:
                return _error(
                    503,
                    "All providers for this route are unavailable; retry later",
                    "server_error",
                    "providers_unavailable",
                    {"Retry-After": "5"},
                )
            return _error(
                502, "Upstream providers failed for this route", "server_error", "upstream_failed"
            )

        _finish(state, tenant, body, request_id, routed)
        headers = {
            "x-router-provider": routed.provider,
            "x-router-model": routed.model,
            "x-router-cost-usd": f"{routed.cost_usd:.8f}",
            "x-router-attempts": str(len(routed.attempts)),
        }
        return JSONResponse(content=routed.response.model_dump(exclude_none=True), headers=headers)

    def _finish(
        state: AppState,
        tenant: db.Tenant,
        body: ChatRequest,
        request_id: str,
        routed: RoutedResponse,
    ) -> None:
        _observe_attempts(state, routed.attempts)
        usage = routed.response.usage or Usage()
        record_usage(
            state,
            tenant,
            request_id,
            body.model,
            routed.provider,
            routed.model,
            "ok",
            usage,
            routed.cost_usd,
            routed.latency_ms,
        )
        log(
            logger,
            logging.INFO,
            "completed",
            route=body.model,
            provider=routed.provider,
            model=routed.model,
            latency_ms=round(routed.latency_ms, 1),
            cost_usd=str(routed.cost_usd),
            attempts=len(routed.attempts),
            tenant=tenant.id,
        )

    async def _stream(
        state: AppState, tenant: db.Tenant, body: ChatRequest, request_id: str
    ) -> Response:
        try:
            routed = await state.router.stream(body)
        except NoRouteAvailable as error:
            _observe_attempts(state, error.attempts)
            if error.all_transient:
                return _error(
                    503,
                    "All providers for this route are unavailable; retry later",
                    "server_error",
                    "providers_unavailable",
                    {"Retry-After": "5"},
                )
            return _error(
                502, "Upstream providers failed for this route", "server_error", "upstream_failed"
            )

        async def body_iter() -> AsyncIterator[bytes]:
            usage = Usage()
            status = "ok"
            try:
                async for chunk in routed.chunks:
                    if chunk.usage is not None:
                        usage = chunk.usage
                    yield chunk.sse().encode("utf-8")
                yield b"data: [DONE]\n\n"
            except Exception as error:
                status = "error"
                log(
                    logger,
                    logging.WARNING,
                    "stream failed mid-way",
                    provider=routed.provider,
                    error=repr(error),
                )
                yield (
                    "data: "
                    + ErrorResponse(
                        error=ErrorBody(
                            message="upstream stream failed",
                            type="server_error",
                            code="stream_failed",
                        )
                    ).model_dump_json()
                    + "\n\n"
                ).encode("utf-8")
            finally:
                price = state.router.pricebook.price(routed.provider, routed.model)
                from model_router.pricing import cost_usd

                cost = cost_usd(usage, price)
                _observe_attempts(state, routed.attempts)
                record_usage(
                    state,
                    tenant,
                    request_id,
                    body.model,
                    routed.provider,
                    routed.model,
                    status,
                    usage,
                    cost,
                    routed.attempts[-1].latency_ms if routed.attempts else 0,
                )

        headers = {
            "x-router-provider": routed.provider,
            "x-router-model": routed.model,
            "Cache-Control": "no-cache",
        }
        return StreamingResponse(body_iter(), media_type="text/event-stream", headers=headers)

    # -- admin ----------------------------------------------------------------

    @app.post("/admin/tenants", status_code=201, dependencies=[Depends(require_admin)])
    async def create_tenant(payload: TenantCreate, state: State) -> TenantCreated:
        raw = generate_key()
        with state.session_factory() as session:
            if session.execute(
                select(db.Tenant).where(db.Tenant.name == payload.name)
            ).scalar_one_or_none():
                raise HTTPException(409, "A tenant with that name exists")
            tenant = db.Tenant(
                name=payload.name,
                monthly_budget_usd=payload.monthly_budget_usd,
                requests_per_minute=payload.requests_per_minute,
                burst=payload.burst,
            )
            session.add(tenant)
            session.flush()
            session.add(
                db.ApiKey(
                    tenant_id=tenant.id, key_hash=hash_key(raw), hint=key_hint(raw), label="initial"
                )
            )
            session.commit()
            return TenantCreated(
                id=tenant.id,
                name=tenant.name,
                monthly_budget_usd=tenant.monthly_budget_usd,
                api_key=raw,
            )

    @app.post(
        "/admin/tenants/{tenant_id}/keys", status_code=201, dependencies=[Depends(require_admin)]
    )
    async def create_key(tenant_id: str, state: State, label: str = "") -> KeyCreated:
        raw = generate_key()
        with state.session_factory() as session:
            if session.get(db.Tenant, tenant_id) is None:
                raise HTTPException(404, "No such tenant")
            key = db.ApiKey(
                tenant_id=tenant_id, key_hash=hash_key(raw), hint=key_hint(raw), label=label[:128]
            )
            session.add(key)
            session.commit()
            return KeyCreated(id=key.id, hint=key.hint, api_key=raw)

    @app.delete("/admin/keys/{key_id}", status_code=204, dependencies=[Depends(require_admin)])
    async def revoke_key(key_id: str, state: State) -> Response:
        with state.session_factory() as session:
            key = session.get(db.ApiKey, key_id)
            if key is None:
                raise HTTPException(404, "No such key")
            if key.revoked_at is None:
                key.revoked_at = db.utcnow()
                session.commit()
        return Response(status_code=204)

    @app.get("/admin/tenants/{tenant_id}/usage", dependencies=[Depends(require_admin)])
    async def tenant_usage(tenant_id: str, state: State) -> UsageSummary:
        with state.session_factory() as session:
            tenant = session.get(db.Tenant, tenant_id)
            if tenant is None:
                raise HTTPException(404, "No such tenant")
            spent = db.month_spend(session, tenant_id)
            count = session.execute(
                select(db.UsageRecord.id).where(
                    db.UsageRecord.tenant_id == tenant_id, db.UsageRecord.ts >= db.month_start()
                )
            ).all()
            return UsageSummary(
                tenant_id=tenant_id,
                month_start=db.month_start(),
                spent_usd=spent,
                monthly_budget_usd=tenant.monthly_budget_usd,
                requests=len(count),
            )

    return app
