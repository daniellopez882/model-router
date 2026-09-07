"""Adapter for the Anthropic Messages API, translated to and from the OpenAI shape.

The two APIs differ in five places that matter here: the system prompt is a
top-level field rather than a message; `max_tokens` is required; tool results
travel as content blocks inside a user turn; tool calls come back as
`tool_use` blocks rather than a `tool_calls` list; and the stream is a
sequence of typed events rather than deltas of the response object. Each
translation is a small pure function below so it can be tested without a
network.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from model_router.providers.base import ProviderError, classify_status, classify_transport
from model_router.schemas import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Message,
    Usage,
)

DEFAULT_MAX_TOKENS = 1024
STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def to_anthropic(request: ChatRequest, model: str) -> dict[str, Any]:
    """Build the Messages API body for an OpenAI-shaped request."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role in ("system", "developer"):
            system_parts.append(message.text())
        elif message.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.text(),
            }
            # Consecutive tool results belong in one user turn.
            if (
                messages
                and messages[-1]["role"] == "user"
                and isinstance(messages[-1]["content"], list)
            ):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        elif message.role == "assistant" and message.tool_calls:
            content: list[dict[str, Any]] = []
            if message.text():
                content.append({"type": "text", "text": message.text()})
            for call in message.tool_calls:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    arguments = {"_raw": function.get("arguments")}
                content.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": message.role, "content": message.content or ""})

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": request.max_tokens or request.max_completion_tokens or DEFAULT_MAX_TOKENS,
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.stop is not None:
        body["stop_sequences"] = [request.stop] if isinstance(request.stop, str) else request.stop
    if request.tools:
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"].get("parameters", {"type": "object"}),
            }
            for tool in request.tools
            if tool.get("type") == "function" and "function" in tool
        ]
    if isinstance(request.tool_choice, str):
        body["tool_choice"] = {"auto": {"type": "auto"}, "required": {"type": "any"}}.get(
            request.tool_choice, {"type": "auto"}
        )
    elif isinstance(request.tool_choice, dict) and "function" in request.tool_choice:
        body["tool_choice"] = {"type": "tool", "name": request.tool_choice["function"]["name"]}
    return body


def from_anthropic(data: dict[str, Any], model: str) -> ChatResponse:
    """Build an OpenAI-shaped response from a Messages API response."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
    message = Message(
        role="assistant", content="".join(text_parts) or None, tool_calls=tool_calls or None
    )
    usage_data = data.get("usage") or {}
    usage = Usage.of(
        int(usage_data.get("input_tokens", 0)), int(usage_data.get("output_tokens", 0))
    )
    finish = STOP_REASONS.get(str(data.get("stop_reason")), "stop")
    response = ChatResponse.new(
        model, [Choice(index=0, message=message, finish_reason=finish)], usage
    )
    if data.get("id"):
        response.id = str(data["id"])
    return response


class AnthropicProvider:
    def __init__(
        self,
        name: str,
        base_url: str = "https://api.anthropic.com",
        *,
        api_key: str | None,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
        version: str = "2023-06-01",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json", "anthropic-version": version}
        if api_key:
            self._headers["x-api-key"] = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=10.0))
        self._owns_client = client is None

    async def chat(self, request: ChatRequest, model: str) -> ChatResponse:
        body = to_anthropic(request, model)
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/messages", json=body, headers=self._headers
            )
        except httpx.HTTPError as error:
            raise classify_transport(error) from error
        if response.status_code >= 400:
            raise classify_status(response.status_code, response.text)
        try:
            return from_anthropic(response.json(), model)
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(f"unparseable success body: {error}", retryable=False) from error

    async def stream(self, request: ChatRequest, model: str) -> AsyncIterator[ChatChunk]:
        body = to_anthropic(request, model)
        body["stream"] = True
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())
        prompt_tokens = 0
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/v1/messages", json=body, headers=self._headers
            ) as response:
                if response.status_code >= 400:
                    raise classify_status(
                        response.status_code, (await response.aread()).decode("utf-8", "replace")
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip() or "{}")
                    except ValueError as error:
                        raise ProviderError(
                            f"unparseable stream event: {error}", retryable=False
                        ) from error
                    kind = event.get("type")
                    if kind == "message_start":
                        prompt_tokens = int(
                            event.get("message", {}).get("usage", {}).get("input_tokens", 0)
                        )
                        yield ChatChunk(
                            id=chunk_id,
                            created=created,
                            model=model,
                            choices=[ChunkChoice(delta={"role": "assistant"})],
                        )
                    elif kind == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield ChatChunk(
                                id=chunk_id,
                                created=created,
                                model=model,
                                choices=[ChunkChoice(delta={"content": delta.get("text", "")})],
                            )
                    elif kind == "message_delta":
                        finish = STOP_REASONS.get(
                            str(event.get("delta", {}).get("stop_reason")), "stop"
                        )
                        completion = int(event.get("usage", {}).get("output_tokens", 0))
                        yield ChatChunk(
                            id=chunk_id,
                            created=created,
                            model=model,
                            choices=[ChunkChoice(delta={}, finish_reason=finish)],
                            usage=Usage.of(prompt_tokens, completion),
                        )
                    elif kind == "error":
                        message = event.get("error", {}).get("message", "stream error")
                        raise ProviderError(f"upstream stream error: {message}", retryable=False)
        except httpx.HTTPError as error:
            raise classify_transport(error) from error

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
