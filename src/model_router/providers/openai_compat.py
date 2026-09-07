"""Adapter for any OpenAI-compatible chat-completions API.

One adapter covers OpenAI itself and everything that copies its wire format:
Groq, Together, Fireworks, vLLM, Ollama, LM Studio. The only per-provider
differences are the base URL and whether a key is sent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from pydantic import ValidationError

from model_router.providers.base import ProviderError, classify_status, classify_transport
from model_router.schemas import ChatChunk, ChatRequest, ChatResponse


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        api_key: str | None,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._owns_client = client is None

    async def chat(self, request: ChatRequest, model: str) -> ChatResponse:
        payload = request.provider_payload(model)
        payload["stream"] = False
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=self._headers
            )
        except httpx.HTTPError as error:
            raise classify_transport(error) from error
        if response.status_code >= 400:
            raise classify_status(response.status_code, response.text)
        try:
            return ChatResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            # A 200 with an unusable body is the provider's fault, but it is not
            # transient, so trying the same provider again would not help.
            raise ProviderError(f"unparseable success body: {error}", retryable=False) from error

    async def stream(self, request: ChatRequest, model: str) -> AsyncIterator[ChatChunk]:
        payload = request.provider_payload(model)
        payload["stream"] = True
        # Ask for usage on the final chunk where the provider supports it; the
        # others ignore the option.
        payload.setdefault("stream_options", {"include_usage": True})
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=payload, headers=self._headers
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise classify_status(response.status_code, body)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    if not data:
                        continue
                    try:
                        yield ChatChunk.model_validate(json.loads(data))
                    except (ValueError, ValidationError) as error:
                        raise ProviderError(
                            f"unparseable stream chunk: {error}", retryable=False
                        ) from error
        except httpx.HTTPError as error:
            raise classify_transport(error) from error

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
