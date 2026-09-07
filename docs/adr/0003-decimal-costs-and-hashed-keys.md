# ADR 0003 — Decimal cost accounting against a mandatory price book; keys stored hashed; budgets enforced before and after

**Status:** accepted · **Date:** 2026-09-07

## Context

A router that fronts paid APIs is a place where money is spent on someone's
behalf. Two things follow: every request must be attributable to a tenant,
and every tenant must be stoppable. Both are only as good as the arithmetic
and the key handling underneath them.

## Decision

**Prices are configuration, and they are required.** A route candidate whose
model has no entry in its provider's `pricing` fails configuration
validation. A request that cannot be priced cannot be charged to a budget,
so it must not be routable.

**Costs are `Decimal`, quantised to eight places.** Prices are quoted per
million tokens; at $0.15 per million a single token is 1.5e-7 USD, which a
float would represent inexactly and a two-place currency type would round
to zero. Eight places keep a single token of the cheapest model, and sums of
them, exact. The usage ledger stores `Numeric(18, 8)`.

**Budgets are checked before and accounted after.** A request is refused
with 402 when the month's spend already meets the budget; the cost of an
admitted request is recorded when the response (or the last stream chunk
with usage) arrives. A tenant can overshoot by at most one request, which is
stated in the README rather than hidden behind a pre-authorisation estimate
that would have to guess the completion length.

**API keys are shown once and stored as SHA-256.** The database holds the
hash, a display hint (`mr-abcd…wxyz`) and revocation time. Lookup is by hash;
a stolen database is not a stolen set of keys. Keys are `mr-` plus 32 bytes
from `secrets.token_urlsafe`.

**Rate limits are token buckets per tenant**, refilled lazily from an
injected clock, so an idle tenant costs nothing and the arithmetic is tested
exactly.

## Consequences

- Adding a model means adding its price, or the configuration refuses to
  load. This is friction on purpose.
- The `Retry-After` on 429 and the `spent … of …` message on 402 are computed
  from the same numbers the ledger holds; there is no second source of truth.
- Streaming responses that die mid-way are still accounted for with whatever
  usage arrived, so a tenant cannot avoid charges by disconnecting.
- Revoking a key takes effect on the next request; there is no session
  cache to invalidate because there is no session.
