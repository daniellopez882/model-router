"""A deterministic OpenAI-compatible upstream, for tests, demos and CI.

It answers `/v1/chat/completions` with an echo of the last user message and
token counts derived from word counts, so every number the router reports can
be checked exactly. Failure modes are selected by the model name, which lets a
test drive the router's retry and fallback paths without patching anything:

    fake-small, fake-large   succeed
    fail-500                 500 every time (retryable)
    fail-429                 429 every time (retryable, rate-limit shaped)
    fail-400                 400 every time (not retryable)
    flaky-N                  fail with 503 N times per process, then succeed
    slow-MS                  sleep MS milliseconds, then succeed
    hang                     never answer (exercises timeouts)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="fake-upstream")
_flaky_calls: Counter[str] = Counter()


def _tokens(text: str) -> int:
    return max(1, len(text.split()))


def _reply_for(messages: list[dict[str, Any]]) -> str:
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    content = last_user.get("content") if last_user else ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return f"echo: {content}"


def _failure(model: str) -> JSONResponse | None:
    if model == "fail-500":
        return JSONResponse(
            {"error": {"message": "fake internal error", "type": "server_error"}}, status_code=500
        )
    if model == "fail-429":
        return JSONResponse(
            {"error": {"message": "fake rate limit", "type": "rate_limit_error"}},
            status_code=429,
            headers={"Retry-After": "1"},
        )
    if model == "fail-400":
        return JSONResponse(
            {"error": {"message": "fake bad request", "type": "invalid_request_error"}},
            status_code=400,
        )
    match = re.fullmatch(r"flaky-(\d+)", model)
    if match:
        _flaky_calls[model] += 1
        if _flaky_calls[model] <= int(match.group(1)):
            return JSONResponse(
                {
                    "error": {
                        "message": f"fake outage {_flaky_calls[model]}",
                        "type": "server_error",
                    }
                },
                status_code=503,
            )
    return None


def reset_flaky() -> None:
    _flaky_calls.clear()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "fake-small", "object": "model"}, {"id": "fake-large", "object": "model"}],
    }


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Any:
    body = await request.json()
    model = str(body.get("model", "fake-small"))
    if model == "hang":
        await asyncio.sleep(3600)
    match = re.fullmatch(r"slow-(\d+)", model)
    if match:
        await asyncio.sleep(int(match.group(1)) / 1000)
    failure = _failure(model)
    if failure is not None:
        return failure

    messages = body.get("messages", [])
    reply = _reply_for(messages)
    prompt_tokens = sum(_tokens(str(m.get("content", ""))) for m in messages)
    completion_tokens = _tokens(reply)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    completion_id = f"chatcmpl-fake-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if body.get("stream"):

        async def events() -> AsyncIterator[bytes]:
            def chunk(
                delta: dict[str, Any],
                finish: str | None = None,
                usage_obj: dict[str, int] | None = None,
            ) -> bytes:
                payload: dict[str, Any] = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                if usage_obj is not None:
                    payload["usage"] = usage_obj
                return f"data: {json.dumps(payload)}\n\n".encode()

            yield chunk({"role": "assistant", "content": ""})
            for word in reply.split(" "):
                yield chunk({"content": word + " "})
            yield chunk({}, "stop", usage)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("FAKE_UPSTREAM_PORT", "9000")),
        log_level="warning",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
