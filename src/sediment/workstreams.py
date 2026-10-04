"""Research dispatch via pebble's workstream API. *** NON-EDGE ROUTES. ***

pebble has no edge route to create a workstream or send it a message, so
`sediment research` uses the regular workstream API:

    POST /v1/api/workstreams/new            {"name": ...}    -> {ws_id, name, ...}
    POST /v1/api/workstreams/{ws_id}/send   {"message": ...} -> {status: ok|queued|queue_full|attachments_busy}
    GET  /v1/api/workstreams/{ws_id}/events (SSE, optional --follow)

These need the `write` scope and are NOT covered by edge authorization or by
`sediment doctor`'s capability listing. They are isolated in this module so
nobody mistakes them for edge calls. Orchestration stays on pebble: we only
dispatch, print the ws_id, and optionally watch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from sediment.envelope import request_json
from sediment.jsonish import JSON, get_str
from sediment.sse import iter_sse

WS = "/v1/api/workstreams"
ACCEPTED = frozenset({"ok", "queued"})


@dataclass(frozen=True)
class Dispatch:
    ws_id: str
    name: str
    status: str

    @property
    def accepted(self) -> bool:
        return self.status in ACCEPTED


@dataclass(frozen=True)
class FollowResult:
    ended: bool
    problem: str | None = None


class WorkstreamClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def dispatch(self, name: str, message: str) -> Dispatch:
        """Create a workstream and send it the question. Raises PebbleError on refusal."""
        # Legacy contract: no `ok` envelope; HTTP 2xx is success (see envelope.parse_response).
        created = request_json(
            self._http, "POST", f"{WS}/new", self._token, {"name": name}, expect_envelope=False
        )
        ws_id = get_str(created, "ws_id")
        if not ws_id:
            raise ValueError(f"workstreams/new succeeded but returned no ws_id: {created!r}")
        sent = request_json(
            self._http,
            "POST",
            f"{WS}/{quote(ws_id, safe='')}/send",
            self._token,
            {"message": message},
            expect_envelope=False,
        )
        return Dispatch(ws_id=ws_id, name=get_str(created, "name", name), status=get_str(sent, "status"))

    def follow(self, ws_id: str, emit: Callable[[str], None], timeout: float = 600.0) -> FollowResult:
        """Print `content` frames until `stream_end`. Any stream trouble is returned, not raised."""
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "text/event-stream"}
        path = f"{WS}/{quote(ws_id, safe='')}/events"
        try:
            with self._http.stream(
                "GET", path, headers=headers, timeout=httpx.Timeout(timeout, connect=10.0)
            ) as response:
                if not response.is_success:
                    response.read()
                    return FollowResult(False, f"HTTP {response.status_code}: {response.text[:300]}")
                for frame in iter_sse(response.iter_lines()):
                    kind, text = _classify(frame.event, frame.data)
                    if kind == "content" and text:
                        emit(text)
                    elif kind == "stream_end":
                        return FollowResult(True)
        except httpx.HTTPError as exc:
            return FollowResult(False, f"{type(exc).__name__}: {exc}")
        return FollowResult(False, "event stream closed before stream_end")


def _classify(event: str, data: str) -> tuple[str, str]:
    """Return (frame kind, printable text). The kind may come from `event:` or a `type` field."""
    payload: JSON
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        payload = data
    if isinstance(payload, dict):
        # An untyped frame that carries `content` is treated as content.
        fallback = "content" if "content" in payload else event
        kind = event if event != "message" else get_str(payload, "type", fallback)
        return kind, get_str(payload, "content")
    return event, payload if isinstance(payload, str) else ""
