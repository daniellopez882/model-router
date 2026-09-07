# ADR 0002 — Retry only what can succeed; break circuits per provider; fall back only before the first byte

**Status:** accepted · **Date:** 2026-09-07

## Context

A gateway that retries everything turns one bad request into four billed
ones and hides a client bug behind a slow 502. A gateway that retries nothing
drops requests on every transient upstream hiccup. The difference between
the two cases is knowable from the failure itself, so it should be decided
once, in one place, rather than by each caller.

Streaming adds a second question: once part of an answer has reached the
client, a different provider cannot pick up where the first left off.

## Decision

1. **Classification lives in the adapter base.** `ProviderError.retryable`
   is set by `classify_status` (408, 409, 425, 429 and every 5xx are
   retryable; every other 4xx is final) and by `classify_transport`
   (timeouts and connection failures are retryable). Adapters never decide
   case by case.
2. **Retries are per candidate and bounded** (`max_retries`, at most 5) with
   full-jitter backoff capped at two seconds, so a misconfiguration cannot
   become a long sleep inside a request.
3. **Fallback moves to the next candidate on any failure once retries are
   spent** — including final failures, because a 400 from one provider says
   nothing certain about another's tokenizer or content policy — unless the
   route sets `fallback: false`.
4. **One circuit breaker per provider**, shared by every route that uses it.
   An outage is a property of the provider, not of the alias. Open breakers
   are skipped without a network call and reported in the attempt log as
   `skipped_open_breaker`; the breaker moves to half-open after the recovery
   timeout and admits a bounded number of trials.
5. **Streams fall back only before the first chunk.** After that, a failure
   ends the stream with an error event so the client can see the answer is
   incomplete, and the partial usage is still accounted.
6. **The client is told the truth about why.** If every attempt failed for a
   "later" reason (open breaker, 429, 503) the response is 503 with
   `Retry-After`; otherwise 502. `x-router-attempts` counts what was tried.

## Consequences

- A permanent failure costs exactly one upstream call per candidate.
- A provider that is down costs zero upstream calls after the threshold, for
  the length of the recovery timeout, across every tenant.
- The tests drive every path with a scripted provider and an injected clock,
  and the breaker's invariants hold under Hypothesis for every event
  sequence, so the policy is checked rather than hoped for.
- Mid-stream failures are visible to clients as an SSE error event; a client
  that wants a whole answer or nothing should not stream.
