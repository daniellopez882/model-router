"""The OpenAI chat-completions wire format, as much of it as the router speaks.

Clients already have SDKs for this shape, so the router speaks it rather than
inventing its own. Fields the router does not interpret (`tools`,
`response_format`, `seed`, ...) are carried through to the provider untouched:
`ChatRequest` allows extra fields and `provider_payload()` re-emits them.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool", "developer"]


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Role
    # A string, or a list of content parts (text/image) for multimodal requests.
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def text(self) -> str:
        """The message's text, flattening content parts; used for translation only."""
        if isinstance(self.content, str):
            return self.content
        if self.content is None:
            return ""
        return "".join(part.get("text", "") for part in self.content if part.get("type") == "text")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    user: str | None = None

    def provider_payload(self, model: str) -> dict[str, Any]:
        """The request as sent upstream: the alias replaced, unset fields dropped."""
        payload = self.model_dump(exclude_none=True, exclude={"model"})
        payload["model"] = model
        return payload


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def of(cls, prompt: int, completion: int) -> Usage:
        return cls(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        )


class Choice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    message: Message
    finish_reason: str | None = None


class ChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage | None = None

    @classmethod
    def new(cls, model: str, choices: list[Choice], usage: Usage | None) -> ChatResponse:
        return cls(
            id=f"chatcmpl-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=model,
            choices=choices,
            usage=usage,
        )


class ChunkChoice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    delta: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str | None = None


class ChatChunk(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice] = Field(default_factory=list)
    usage: Usage | None = None

    def sse(self) -> str:
        return f"data: {self.model_dump_json(exclude_none=True)}\n\n"


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "model-router"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


class ErrorBody(BaseModel):
    message: str
    type: str
    code: str | None = None


class ErrorResponse(BaseModel):
    """The OpenAI error envelope, so SDK clients raise their usual exceptions."""

    error: ErrorBody
