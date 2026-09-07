"""Configuration: process settings from the environment, routing policy from YAML.

The two are kept apart on purpose. Settings are about *this process* (where the
database is, which port to bind, whether an admin token exists) and change per
deployment. The routing configuration is about *the fleet* (which providers
exist, how aliases map onto them, what each model costs) and is reviewed like
code, which is why it lives in a versioned file rather than in environment
variables that nobody diffs.
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(ValueError):
    """Raised when the routing configuration cannot be used as written."""


class Settings(BaseSettings):
    """Process-level settings, read from the environment (and `.env` in development)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: Literal["development", "test", "production"] = "development"
    HOST: str = "127.0.0.1"
    PORT: int = Field(default=8080, ge=1, le=65535)
    LOG_LEVEL: str = "INFO"
    ROUTER_CONFIG: Path = Path("router.yaml")
    DATABASE_URL: str = "sqlite:///./router.db"
    # Admin endpoints (creating tenants and keys) refuse every call until this is set.
    ADMIN_TOKEN: str | None = None
    REQUEST_TIMEOUT_S: float = Field(default=60.0, gt=0)
    # Stops one tenant's request from occupying the process indefinitely.
    MAX_REQUEST_BYTES: int = Field(default=1_000_000, gt=0)

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


class ProviderKind(StrEnum):
    OPENAI = "openai"  # any OpenAI-compatible API: OpenAI, Groq, Together, vLLM, Ollama...
    ANTHROPIC = "anthropic"


class Price(BaseModel):
    """USD per one million tokens. Zero is a legitimate price for a local model."""

    input_per_1m: Decimal = Field(ge=0)
    output_per_1m: Decimal = Field(ge=0)


class ProviderConfig(BaseModel):
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    kind: ProviderKind
    base_url: str
    # The *name* of the environment variable holding the key. The key itself
    # never appears in this file, so the file can be committed.
    api_key_env: str | None = None
    timeout_s: float = Field(default=60.0, gt=0)
    pricing: dict[str, Price] = Field(default_factory=dict)

    def api_key(self) -> str | None:
        if self.api_key_env is None:
            return None
        value = os.environ.get(self.api_key_env)
        return value or None


class Strategy(StrEnum):
    PRIORITY = "priority"  # candidates in the order written
    ROUND_ROBIN = "round_robin"  # rotate the starting candidate
    WEIGHTED_RANDOM = "weighted_random"  # sample the first by weight, then the rest
    LEAST_LATENCY = "least_latency"  # lowest observed latency first
    CHEAPEST = "cheapest"  # lowest priced first


class Candidate(BaseModel):
    provider: str
    model: str
    weight: float = Field(default=1.0, gt=0)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


class Route(BaseModel):
    alias: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
    strategy: Strategy = Strategy.PRIORITY
    candidates: list[Candidate] = Field(min_length=1)
    # When false, only the first chosen candidate is tried; failures surface at once.
    fallback: bool = True
    # Retries *per candidate* on retryable failures, before moving to the next one.
    max_retries: int = Field(default=1, ge=0, le=5)


class BreakerConfig(BaseModel):
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_s: float = Field(default=30.0, gt=0)
    half_open_max_calls: int = Field(default=1, ge=1)


class LimitsConfig(BaseModel):
    # Per tenant, unless the tenant overrides them.
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=20, ge=1)


class RouterConfig(BaseModel):
    providers: list[ProviderConfig] = Field(min_length=1)
    routes: list[Route] = Field(min_length=1)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @model_validator(mode="after")
    def _cross_check(self) -> Self:
        providers = {p.name: p for p in self.providers}
        if len(providers) != len(self.providers):
            raise ValueError("provider names must be unique")
        aliases = [r.alias for r in self.routes]
        if len(set(aliases)) != len(aliases):
            raise ValueError("route aliases must be unique")
        if set(aliases) & set(providers):
            raise ValueError("a route alias must not shadow a provider name")
        for route in self.routes:
            for candidate in route.candidates:
                provider = providers.get(candidate.provider)
                if provider is None:
                    raise ValueError(
                        f"route {route.alias!r} names unknown provider {candidate.provider!r}"
                    )
                if candidate.model not in provider.pricing:
                    # Cost accounting is not optional: a request that cannot be
                    # priced cannot be charged against a budget.
                    raise ValueError(
                        f"route {route.alias!r}: provider {candidate.provider!r} has no price "
                        f"for model {candidate.model!r}"
                    )
        return self

    def provider(self, name: str) -> ProviderConfig:
        for p in self.providers:
            if p.name == name:
                return p
        raise KeyError(name)

    def route(self, alias: str) -> Route | None:
        for r in self.routes:
            if r.alias == alias:
                return r
        return None


def load_router_config(path: Path) -> RouterConfig:
    """Parse and validate the routing file, turning every failure into a ConfigError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}") from error
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    try:
        return RouterConfig.model_validate(data)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        raise ConfigError(f"{path}: {problems}") from error
