"""Client for an OpenAI-compatible `/chat/completions` endpoint with function calling.

`base_url` is the API root including its version prefix (e.g. `http://host/v1`).
We ask for streaming so text appears as it is produced. Endpoints that reject
streaming get one clean retry without it, and we say that we did; endpoints
that ignore `stream` and answer with plain JSON are handled the same as
non-streaming. A stream that breaks halfway is an error, not a silent retry,
because the user has already seen part of the answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import httpx

from sediment.config import ModelSettings
from sediment.jsonish import JSON, JSONObject, get_obj, get_obj_list, get_str
from sediment.sse import iter_sse

# Status codes that plausibly mean "I don't do streaming" rather than "you are wrong".
_STREAM_REFUSALS = {400, 404, 405, 415, 422, 501}


class ModelError(Exception):
    """The model endpoint failed; the message carries its own explanation."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


@dataclass
class AssistantTurn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def as_message(self) -> JSONObject:
        message: JSONObject = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class ChatModel:
    def __init__(
        self,
        settings: ModelSettings,
        transport: httpx.BaseTransport | None = None,
        stream: bool = True,
        notice: Callable[[str], None] = lambda _msg: None,
    ):
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(300.0, connect=15.0),
        )
        self._model = settings.model
        self._stream = stream
        self._notice = notice

    def close(self) -> None:
        self._http.close()

    def complete(
        self,
        messages: list[JSONObject],
        tools: list[JSONObject],
        on_text: Callable[[str], None],
    ) -> AssistantTurn:
        payload: JSONObject = {"model": self._model, "messages": list(messages)}
        if tools:
            payload["tools"] = list(tools)
        if self._stream:
            turn = self._complete_streaming(payload, on_text)
            if turn is not None:
                return turn
        turn = self._complete_plain(payload)
        if turn.content:
            on_text(turn.content)
        return turn

    def _complete_plain(self, payload: JSONObject) -> AssistantTurn:
        try:
            response = self._http.post("chat/completions", json={**payload, "stream": False})
        except httpx.HTTPError as exc:
            raise ModelError(f"model endpoint unreachable: {type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise ModelError(f"model endpoint HTTP {response.status_code}: {response.text[:500]}")
        return _parse_completion(_json_body(response))

    def _complete_streaming(
        self, payload: JSONObject, on_text: Callable[[str], None]
    ) -> AssistantTurn | None:
        """None means: the endpoint refused streaming; caller falls back."""
        try:
            with self._http.stream(
                "POST", "chat/completions", json={**payload, "stream": True}
            ) as response:
                if not response.is_success:
                    response.read()
                    if response.status_code in _STREAM_REFUSALS:
                        self._stream = False
                        self._notice(
                            f"model endpoint refused streaming (HTTP {response.status_code}: "
                            f"{response.text[:200]}); continuing without streaming"
                        )
                        return None
                    raise ModelError(
                        f"model endpoint HTTP {response.status_code}: {response.text[:500]}"
                    )
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    response.read()
                    turn = _parse_completion(_json_body(response))
                    if turn.content:
                        on_text(turn.content)
                    return turn
                return _accumulate_stream(response.iter_lines(), on_text)
        except httpx.HTTPError as exc:
            raise ModelError(f"model stream failed: {type(exc).__name__}: {exc}") from exc


def _json_body(response: httpx.Response) -> JSONObject:
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise ModelError(f"model endpoint returned non-JSON: {response.text[:300]!r}") from exc
    if not isinstance(body, dict):
        raise ModelError(f"model endpoint returned unexpected JSON: {body!r}")
    return body


def _parse_completion(body: JSONObject) -> AssistantTurn:
    choices = get_obj_list(body, "choices")
    if not choices:
        raise ModelError(f"model response has no choices: {json.dumps(body)[:300]}")
    message = get_obj(choices[0], "message") or {}
    calls = []
    for i, raw in enumerate(get_obj_list(message, "tool_calls")):
        function = get_obj(raw, "function") or {}
        calls.append(
            ToolCall(
                id=get_str(raw, "id") or f"call_{i}",
                name=get_str(function, "name"),
                arguments=get_str(function, "arguments") or "{}",
            )
        )
    return AssistantTurn(content=get_str(message, "content"), tool_calls=calls)


def _accumulate_stream(lines: Iterable[str], on_text: Callable[[str], None]) -> AssistantTurn:
    text: list[str] = []
    calls: dict[int, ToolCall] = {}
    for frame in iter_sse(lines):
        if frame.data.strip() == "[DONE]":
            break
        try:
            chunk: JSON = json.loads(frame.data)
        except json.JSONDecodeError as exc:
            raise ModelError(f"unparseable stream chunk: {frame.data[:200]!r}") from exc
        if not isinstance(chunk, dict):
            continue
        for choice in get_obj_list(chunk, "choices"):
            delta = get_obj(choice, "delta") or {}
            piece = get_str(delta, "content")
            if piece:
                text.append(piece)
                on_text(piece)
            for raw in get_obj_list(delta, "tool_calls"):
                index = raw.get("index")
                slot = index if isinstance(index, int) else len(calls)
                call = calls.setdefault(slot, ToolCall(id="", name="", arguments=""))
                call.id = call.id or get_str(raw, "id")
                function = get_obj(raw, "function") or {}
                call.name += get_str(function, "name")
                call.arguments += get_str(function, "arguments")
    ordered = [calls[k] for k in sorted(calls)]
    for i, call in enumerate(ordered):
        call.id = call.id or f"call_{i}"
        call.arguments = call.arguments or "{}"
    return AssistantTurn(content="".join(text), tool_calls=ordered)
