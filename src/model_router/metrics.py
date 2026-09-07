"""Prometheus metrics. Labels are bounded (tenant ids, provider names, route
aliases, statuses), never request ids or model output, so cardinality stays
proportional to configuration rather than to traffic."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry()

REQUESTS = Counter(
    "router_requests_total",
    "Chat completion requests handled by the router",
    ["route", "provider", "status"],
    registry=REGISTRY,
)
LATENCY = Histogram(
    "router_upstream_latency_seconds",
    "Latency of successful upstream calls",
    ["provider", "model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
    registry=REGISTRY,
)
TOKENS = Counter(
    "router_tokens_total", "Tokens billed", ["provider", "model", "direction"], registry=REGISTRY
)
COST = Counter(
    "router_cost_usd_total",
    "Cost accrued in USD",
    ["tenant", "provider", "model"],
    registry=REGISTRY,
)
ATTEMPTS = Counter(
    "router_attempts_total",
    "Upstream attempts by outcome",
    ["provider", "outcome"],
    registry=REGISTRY,
)
BREAKER = Gauge(
    "router_breaker_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open",
    ["provider"],
    registry=REGISTRY,
)
REJECTED = Counter(
    "router_rejected_total", "Requests refused before routing", ["reason"], registry=REGISTRY
)

BREAKER_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def render() -> bytes:
    return generate_latest(REGISTRY)
