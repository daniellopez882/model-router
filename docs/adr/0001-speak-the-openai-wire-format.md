# ADR 0001 — Speak the OpenAI wire format; route by alias

**Status:** accepted · **Date:** 2026-09-07

## Context

Every client library, agent framework and evaluation tool already speaks the
OpenAI chat-completions API. A gateway with its own request shape would need
its own SDKs and would be adopted by nobody. At the same time, clients that
name a provider's model directly (`gpt-4o-mini`, `claude-sonnet-5`) have
already made the routing decision themselves, which leaves nothing for a
router to do.

## Decision

The router exposes `/v1/chat/completions` and `/v1/models` exactly as OpenAI
does, including the SSE streaming format and the error envelope, so an
unmodified OpenAI SDK pointed at the router's base URL works. Fields the
router does not interpret are carried upstream untouched.

The `model` field names a **route alias** (`fast`, `smart`, `demo`), never a
provider model. The alias resolves to an ordered list of candidates through
a strategy. The response body echoes the alias back as `model` — SDKs that
copy the model name into the next request must keep working — and the
provider and model that actually answered are reported in
`x-router-provider` and `x-router-model` headers, with the cost in
`x-router-cost-usd`.

Anthropic's Messages API is translated in both directions, including tools,
tool results and streaming events, in pure functions that are unit-tested
without a network (`providers/anthropic.py`).

## Consequences

- Any OpenAI client is a client of the router; the demo in the README uses
  `curl` and the official SDK unchanged.
- A new provider that speaks the OpenAI format costs one YAML entry. One that
  does not costs an adapter with two translation functions and a stream
  event mapper.
- Aliases decouple clients from vendors: moving `fast` from one provider to
  another is a configuration change nobody has to redeploy for.
- The translation to Anthropic is lossy for features the OpenAI shape has no
  words for (Anthropic's `thinking` blocks, cache-control on content parts);
  those are documented as limits rather than approximated.
