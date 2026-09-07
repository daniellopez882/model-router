"""The OpenAI <-> Anthropic translation, as pure functions and over a mocked wire."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from model_router.providers.anthropic import AnthropicProvider, from_anthropic, to_anthropic
from model_router.providers.base import ProviderError
from model_router.schemas import ChatRequest, Message


def req(*messages: Message, **extra: object) -> ChatRequest:
    return ChatRequest(model="alias", messages=list(messages), **extra)  # type: ignore[arg-type]


class TestRequestTranslation:
    def test_system_moves_to_the_top_level_and_max_tokens_is_required(self) -> None:
        body = to_anthropic(
            req(Message(role="system", content="be brief"), Message(role="user", content="hi")),
            "claude-x",
        )
        assert body["system"] == "be brief"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert body["max_tokens"] == 1024
        assert body["model"] == "claude-x"

    def test_sampling_parameters_and_stop_sequences(self) -> None:
        body = to_anthropic(
            req(
                Message(role="user", content="hi"),
                temperature=0.2,
                top_p=0.9,
                max_tokens=50,
                stop="END",
            ),
            "m",
        )
        assert body["temperature"] == 0.2 and body["top_p"] == 0.9
        assert body["max_tokens"] == 50 and body["stop_sequences"] == ["END"]

    def test_tools_become_input_schemas_and_tool_choice_maps(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }
        ]
        body = to_anthropic(
            req(Message(role="user", content="hi"), tools=tools, tool_choice="required"), "m"
        )
        assert body["tools"] == [
            {
                "name": "lookup",
                "description": "d",
                "input_schema": tools[0]["function"]["parameters"],
            }
        ]
        assert body["tool_choice"] == {"type": "any"}
        forced = to_anthropic(
            req(
                Message(role="user", content="hi"),
                tools=tools,
                tool_choice={"type": "function", "function": {"name": "lookup"}},
            ),
            "m",
        )
        assert forced["tool_choice"] == {"type": "tool", "name": "lookup"}

    def test_assistant_tool_calls_and_tool_results_round_trip(self) -> None:
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q": "x"}'},
            }
        ]
        body = to_anthropic(
            req(
                Message(role="user", content="find x"),
                Message(role="assistant", content=None, tool_calls=calls),
                Message(role="tool", tool_call_id="call_1", content="found it"),
                Message(role="tool", tool_call_id="call_2", content="also this"),
            ),
            "m",
        )
        assert body["messages"][1] == {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {"q": "x"}}
            ],
        }
        # Two tool results collapse into one user turn.
        assert body["messages"][2]["role"] == "user"
        assert [b["tool_use_id"] for b in body["messages"][2]["content"]] == ["call_1", "call_2"]
        assert len(body["messages"]) == 3


class TestResponseTranslation:
    def test_text_and_usage_and_stop_reason(self) -> None:
        data = {
            "id": "msg_1",
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }
        response = from_anthropic(data, "claude-x")
        assert response.id == "msg_1"
        assert response.choices[0].message.content == "hello"
        assert response.choices[0].finish_reason == "stop"
        assert response.usage is not None and (
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            response.usage.total_tokens,
        ) == (7, 3, 10)

    def test_tool_use_becomes_tool_calls_with_json_arguments(self) -> None:
        data = {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}}
            ],
            "stop_reason": "tool_use",
            "usage": {},
        }
        response = from_anthropic(data, "m")
        message = response.choices[0].message
        assert message.content is None
        assert message.tool_calls == [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": json.dumps({"q": "x"})},
            }
        ]
        assert response.choices[0].finish_reason == "tool_calls"

    def test_max_tokens_maps_to_length(self) -> None:
        assert (
            from_anthropic({"content": [], "stop_reason": "max_tokens"}, "m")
            .choices[0]
            .finish_reason
            == "length"
        )


class TestOverTheWire:
    @respx.mock
    async def test_chat_sends_headers_and_parses(self) -> None:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg",
                    "content": [{"type": "text", "text": "hey"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        )
        provider = AnthropicProvider("anthropic", api_key="sk-ant-test")
        response = await provider.chat(req(Message(role="user", content="hi")), "claude-x")
        await provider.aclose()
        assert response.choices[0].message.content == "hey"
        sent = route.calls.last.request
        assert sent.headers["x-api-key"] == "sk-ant-test"
        assert sent.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(sent.content)["max_tokens"] == 1024

    @respx.mock
    async def test_status_classification(self) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(529, json={"error": {"message": "overloaded"}})
        )
        provider = AnthropicProvider("anthropic", api_key="k")
        with pytest.raises(ProviderError) as info:
            await provider.chat(req(Message(role="user", content="hi")), "m")
        assert (
            info.value.status == 529 and info.value.retryable is False
        )  # 529 is not in the retryable set; the router moves on
        respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(429))
        with pytest.raises(ProviderError) as info2:
            await provider.chat(req(Message(role="user", content="hi")), "m")
        assert info2.value.retryable is True
        await provider.aclose()

    @respx.mock
    async def test_transport_errors_are_retryable(self) -> None:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=httpx.ConnectTimeout("slow")
        )
        provider = AnthropicProvider("anthropic", api_key="k")
        with pytest.raises(ProviderError) as info:
            await provider.chat(req(Message(role="user", content="hi")), "m")
        assert info.value.retryable is True
        await provider.aclose()

    @respx.mock
    async def test_stream_events_become_chunks(self) -> None:
        events = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        )
        provider = AnthropicProvider("anthropic", api_key="k")
        chunks = [c async for c in provider.stream(req(Message(role="user", content="hi")), "m")]
        await provider.aclose()
        text = "".join(c.choices[0].delta.get("content", "") for c in chunks)
        assert text == "Hello"
        assert chunks[0].choices[0].delta == {"role": "assistant"}
        last = chunks[-1]
        assert last.choices[0].finish_reason == "stop"
        assert last.usage is not None and (
            last.usage.prompt_tokens,
            last.usage.completion_tokens,
        ) == (5, 2)
