# Changelog

## 0.1.0 — 2026-09-07

First release.

- OpenAI-compatible `/v1/chat/completions` (JSON and SSE streaming) and `/v1/models`.
- Provider adapters: any OpenAI-compatible API, and the Anthropic Messages API
  with full request/response/stream translation including tools.
- Route aliases with five strategies: priority, round-robin, weighted random,
  least-latency (EWMA) and cheapest.
- Per-candidate retries on retryable failures only; fallback across candidates;
  a circuit breaker per provider with a half-open trial.
- Tenants with hashed API keys, per-tenant token-bucket rate limits and monthly
  USD budgets enforced before and accounted after every request.
- Decimal cost accounting from a per-model price book; usage ledger in SQLite
  or PostgreSQL via Alembic migrations.
- Prometheus metrics, JSON logs with request ids, `/health` and `/ready`.
- A deterministic fake upstream (`python -m model_router.fake_upstream`) so
  the whole system runs and is tested with no provider account.
