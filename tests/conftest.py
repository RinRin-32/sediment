"""Shared fakes: an in-memory pebble and an OpenAI-compatible model, both via MockTransport.

Nothing in the test suite touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

import httpx
import pytest

from sediment.config import Config, ToolPolicy
from sediment.jsonish import JSON, JSONObject

TOKEN = "pbl_test_token_0123456789"
BASE = "https://pebble.test"

Reply: TypeAlias = "tuple[int, JSONObject] | Callable[[httpx.Request], httpx.Response]"


@dataclass
class Recorded:
    method: str
    path: str
    headers: httpx.Headers
    body: JSON
    query: dict[str, str]


@dataclass
class FakePebble:
    routes: dict[tuple[str, str], Reply] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)

    def on(self, method: str, path: str, reply: Reply) -> FakePebble:
        self.routes[(method, path)] = reply
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: JSON = json.loads(request.content) if request.content else None
        self.requests.append(
            Recorded(
                request.method,
                request.url.path,
                request.headers,
                body,
                dict(request.url.params),
            )
        )
        reply = self.routes.get((request.method, request.url.path))
        if reply is None:
            return httpx.Response(404, json={"ok": False, "error": "no such route in fake"})
        if callable(reply):
            return reply(request)
        status, payload = reply
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def last(self) -> Recorded:
        return self.requests[-1]


@pytest.fixture
def pebble() -> FakePebble:
    return FakePebble()


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        pebble_url=BASE,
        pebble_token=TOKEN,
        model_base_url="https://model.test/v1",
        model_name="fake-model",
        repo="demo",
        cache_dir=tmp_path / "cache",
        tools=ToolPolicy(max_turns=6, command_timeout=10, max_output_chars=5000),
    )


def chat_response(content: str = "", tool_calls: list[JSONObject] | None = None) -> JSONObject:
    message: JSONObject = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def tool_call(call_id: str, name: str, arguments: JSONObject) -> JSONObject:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@dataclass
class FakeModel:
    """Scripted OpenAI-compatible endpoint. Answers non-streaming JSON regardless of `stream`."""

    script: list[JSONObject]
    requests: list[JSONObject] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        self.requests.append(json.loads(request.content))
        if not self.script:
            return httpx.Response(500, text="fake model script exhausted")
        return httpx.Response(200, json=self.script.pop(0))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
