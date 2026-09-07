"""What a provider adapter promises the router, and how it reports failure.

The router's whole retry-and-fallback logic rests on one bit: `retryable`.
A provider that lies about it either hammers an upstream that has already
said "no" (a 400 marked retryable) or gives up on a hiccup (a timeout marked
final). So the classification lives here, once, and adapters call it rather
than deciding case by case.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from model_router.schemas import ChatChunk, ChatRequest, ChatResponse


class ProviderError(Exception):
    """A request to a provider failed.

    `retryable` says whether trying again -- against this provider or the next
    candidate -- could reasonably succeed. `status` is the upstream HTTP status
    when there was one.
    """

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


# 408 request timeout, 409 conflict (rare, transient), 425 too early, 429 rate
# limited, and every 5xx: the request may well succeed elsewhere or later.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def classify_status(status: int, body: str) -> ProviderError:
    retryable = status in RETRYABLE_STATUSES
    snippet = body.strip().replace("\n", " ")[:300]
    return ProviderError(
        f"upstream returned {status}: {snippet}", retryable=retryable, status=status
    )


def classify_transport(error: httpx.HTTPError) -> ProviderError:
    # Timeouts and connection failures say nothing about the request itself.
    return ProviderError(f"transport failure: {type(error).__name__}: {error}", retryable=True)


class Provider(Protocol):
    """A model provider the router can send chat completions to."""

    name: str

    async def chat(self, request: ChatRequest, model: str) -> ChatResponse: ...

    def stream(self, request: ChatRequest, model: str) -> AsyncIterator[ChatChunk]: ...

    async def aclose(self) -> None: ...
