"""OpenAI-compatible client: streaming, tool-call deltas, and the non-streaming fallback."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import chat_response, tool_call
from sediment.config import ModelSettings
from sediment.jsonish import JSONObject
from sediment.model import ChatModel, ModelError

SETTINGS = ModelSettings(base_url="https://model.test/v1", model="m", api_key="sk-test")


def sse(chunks: list[JSONObject]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def delta(**fields: object) -> JSONObject:
    return {"choices": [{"index": 0, "delta": fields}]}


def test_streams_text_and_assembles_tool_call_fragments() -> None:
    seen: list[JSONObject] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer sk-test"
        return sse(
            [
                delta(content="Let me "),
                delta(content="look."),
                delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "read_", "arguments": '{"pa'}}]),
                delta(tool_calls=[{"index": 0, "function": {"name": "file", "arguments": 'th": "a.txt"}'}}]),
            ]
        )

    pieces: list[str] = []
    model = ChatModel(SETTINGS, transport=httpx.MockTransport(handler))
    turn = model.complete([{"role": "user", "content": "hi"}], [], pieces.append)
    assert pieces == ["Let me ", "look."]
    assert turn.content == "Let me look."
    [call] = turn.tool_calls
    assert (call.id, call.name, json.loads(call.arguments)) == ("c1", "read_file", {"path": "a.txt"})
    assert seen[0]["stream"] is True and seen[0]["model"] == "m"


def test_falls_back_to_non_streaming_when_streaming_is_refused() -> None:
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        streaming = json.loads(request.content)["stream"]
        calls.append(streaming)
        if streaming:
            return httpx.Response(400, json={"error": "stream not supported"})
        return httpx.Response(200, json=chat_response("plain answer"))

    notices: list[str] = []
    pieces: list[str] = []
    model = ChatModel(SETTINGS, transport=httpx.MockTransport(handler), notice=notices.append)
    assert model.complete([], [], pieces.append).content == "plain answer"
    assert pieces == ["plain answer"]
    assert "refused streaming" in notices[0]
    model.complete([], [], pieces.append)
    assert calls == [True, False, False]  # remembered: no second streaming attempt


def test_endpoint_that_ignores_stream_flag() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response("", [tool_call("x", "list_dir", {})]))

    turn = ChatModel(SETTINGS, transport=httpx.MockTransport(handler)).complete([], [], print)
    assert turn.tool_calls[0].name == "list_dir"


def test_server_error_is_not_retried_silently() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model crashed")

    with pytest.raises(ModelError, match="model crashed"):
        ChatModel(SETTINGS, transport=httpx.MockTransport(handler)).complete([], [], print)


def test_as_message_round_trips_tool_calls() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=chat_response("", [tool_call("c9", "search", {"pattern": "x"})]))

    turn = ChatModel(SETTINGS, transport=httpx.MockTransport(handler), stream=False).complete([], [], print)
    message = turn.as_message()
    assert message["role"] == "assistant" and message["content"] is None
    assert message["tool_calls"] == [
        {"id": "c9", "type": "function", "function": {"name": "search", "arguments": '{"pattern": "x"}'}}
    ]
