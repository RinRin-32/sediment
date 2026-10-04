"""`sediment research`: non-edge workstream dispatch, refusals, and --follow over SSE.

The workstream API is NOT the edge API and has no `{"ok": ...}` envelope. The
fakes here deliberately answer in that legacy shape (as observed from a real
pebble: `{ws_id, name, message_count, resumed}`), because fakes that added
`"ok": true` are exactly what once hid a bug that rejected every real dispatch.
"""

from __future__ import annotations

import argparse
import io

import httpx
import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment import cli
from sediment.config import Config
from sediment.edge import EdgeClient
from sediment.envelope import PebbleError
from sediment.jsonish import JSONObject
from sediment.ui import Console
from sediment.workstreams import WorkstreamClient

WS = "/v1/api/workstreams"


def created(ws_id: str, name: str = "research: q") -> JSONObject:
    """Legacy `workstreams/new` success body: no `ok` key."""
    return {"ws_id": ws_id, "name": name, "message_count": 0, "resumed": False}


def sent(status: str) -> JSONObject:
    """Legacy `workstreams/{id}/send` success body: no `ok` key."""
    return {"status": status}


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, pebble: FakePebble, config: Config):
    monkeypatch.setattr(
        cli, "WorkstreamClient", lambda url, token: WorkstreamClient(url, token, transport=pebble.transport)
    )

    def go(*question: str, follow: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = argparse.Namespace(question=list(question), name=None, follow=follow, timeout=5.0)
        code = cli.cmd_research(args, config, Console(out, err))
        return code, out.getvalue(), err.getvalue()

    return go


def test_dispatch_creates_then_sends(run, pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, created("ws-42", "research: why")))
    pebble.on("POST", f"{WS}/ws-42/send", (200, sent("queued")))
    code, out, _err = run("why", "is", "it", "slow?")
    assert code == 0
    assert "ws_id: ws-42" in out
    assert [r.path for r in pebble.requests] == [f"{WS}/new", f"{WS}/ws-42/send"]
    assert pebble.requests[0].body == {"name": "research: why is it slow?"}
    assert pebble.requests[1].body == {"message": "why is it slow?"}
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in pebble.requests)
    assert not any("/edge/" in r.path for r in pebble.requests)


def test_dispatch_accepts_the_legacy_shape_without_ok(pebble: FakePebble) -> None:
    """Regression: a 200 with no `ok` field is success on the workstream surface."""
    pebble.on("POST", f"{WS}/new", (200, created("ws_research_1", "research: x")))
    pebble.on("POST", f"{WS}/ws_research_1/send", (200, sent("ok")))
    dispatch = WorkstreamClient(BASE, TOKEN, transport=pebble.transport).dispatch("research: x", "x")
    assert (dispatch.ws_id, dispatch.name, dispatch.status) == ("ws_research_1", "research: x", "ok")
    assert dispatch.accepted


def test_same_host_still_enforces_the_envelope_on_edge_routes(pebble: FakePebble) -> None:
    """The fix is per-surface: an edge route answering 200 without `ok: true` still fails."""
    pebble.on("POST", f"{WS}/new", (200, created("w")))
    pebble.on("POST", f"{WS}/w/send", (200, sent("queued")))
    pebble.on("POST", "/v1/api/edge/kb/read", (200, {"ok": False, "error": "edge said no"}))
    pebble.on("POST", "/v1/api/edge/kb/search", (200, {"query": "q", "results": []}))

    assert WorkstreamClient(BASE, TOKEN, transport=pebble.transport).dispatch("n", "m").accepted

    edge = EdgeClient(BASE, TOKEN, transport=pebble.transport)
    with pytest.raises(PebbleError, match="edge said no") as refused:
        edge.kb_read("T")
    assert refused.value.status == 200
    with pytest.raises(PebbleError, match="no 'ok' field"):
        edge.kb_search("q")


def test_workstream_body_that_says_ok_false_is_still_a_failure(pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, {"ok": False, "error": "workstreams disabled"}))
    with pytest.raises(PebbleError, match="workstreams disabled"):
        WorkstreamClient(BASE, TOKEN, transport=pebble.transport).dispatch("n", "m")


def test_workstream_2xx_that_is_not_json_is_refused(pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", lambda _r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(PebbleError, match="not a JSON object"):
        WorkstreamClient(BASE, TOKEN, transport=pebble.transport).dispatch("n", "m")


def test_queue_full_is_not_success(run, pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, created("ws-1")))
    pebble.on("POST", f"{WS}/ws-1/send", (200, sent("queue_full")))
    code, out, err = run("q")
    assert code == 1
    assert "ws-1" in out and "NOT accepted" in err


def test_refusal_prints_missing_scope(run, pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{WS}/new",
        (403, {"error": "write scope required", "missing_scope": "write"}),
    )
    code, _out, err = run("q")
    assert code == 1
    assert "lacks the scope 'write'" in err and "write scope required" in err
    assert "not an edge route" in err


def sse_reply(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def test_follow_prints_content_until_stream_end(run, pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, created("w")))
    pebble.on("POST", f"{WS}/w/send", (200, sent("ok")))
    body = (
        'event: content\ndata: {"content": "Found "}\n\n'
        ": keepalive\n\n"
        'data: {"type": "content", "content": "it."}\n\n'
        'data: {"type": "stream_end"}\n\n'
        'data: {"type": "content", "content": "AFTER END"}\n\n'
    )
    pebble.on("GET", f"{WS}/w/events", lambda _r: sse_reply(body))
    code, out, err = run("q", follow=True)
    assert code == 0
    assert "Found it." in out and "AFTER END" not in out
    assert "unavailable" not in err


def test_flaky_sse_still_exits_zero_with_ws_id(run, pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, created("w")))
    pebble.on("POST", f"{WS}/w/send", (200, sent("ok")))

    def drop(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    pebble.on("GET", f"{WS}/w/events", drop)
    code, out, err = run("q", follow=True)
    assert code == 0
    assert "ws_id: w" in out
    assert "stream unavailable" in err and "dispatched regardless" in err


def test_stream_closing_early_is_reported(run, pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, created("w")))
    pebble.on("POST", f"{WS}/w/send", (200, sent("ok")))
    pebble.on("GET", f"{WS}/w/events", lambda _r: sse_reply('data: {"content": "part"}\n\n'))
    code, _out, err = run("q", follow=True)
    assert code == 0 and "before stream_end" in err


def test_new_without_ws_id_is_an_error(pebble: FakePebble) -> None:
    pebble.on("POST", f"{WS}/new", (200, {"name": "n", "message_count": 0}))
    with pytest.raises(ValueError, match="no ws_id"):
        WorkstreamClient(BASE, TOKEN, transport=pebble.transport).dispatch("n", "m")
