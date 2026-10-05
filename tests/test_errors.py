"""Every documented failure shape becomes a PebbleError carrying the server's own words."""

from __future__ import annotations

import httpx
import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment.edge import EdgeClient
from sediment.envelope import PebbleError, TransportError

E = "/v1/api/edge"


def call_kb_read(pebble: FakePebble) -> PebbleError:
    with pytest.raises(PebbleError) as info:
        EdgeClient(BASE, TOKEN, transport=pebble.transport).kb_read("T")
    return info.value


def test_400_names_bad_field(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/read", (400, {"ok": False, "error": "'title' is required"}))
    err = call_kb_read(pebble)
    assert err.status == 400
    assert "'title' is required" in err.explain()


def test_401(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/read", (401, {"ok": False, "error": "invalid token"}))
    err = call_kb_read(pebble)
    assert err.status == 401
    text = err.explain()
    assert "invalid token" in text and "not accepted" in text
    assert TOKEN not in text


def test_403_missing_scope(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/kb/read",
        (403, {"ok": False, "error": "token lacks scope", "missing_scope": "read"}),
    )
    err = call_kb_read(pebble)
    assert err.missing_scope == "read"
    assert "lacks the scope 'read'" in err.explain()
    assert "token lacks scope" in err.explain()


def test_403_missing_capability(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/sessions/arm-full-access",
        (403, {"ok": False, "error": "no", "missing_capability": "full_access"}),
    )
    with pytest.raises(PebbleError) as info:
        EdgeClient(BASE, TOKEN, transport=pebble.transport).full_access("arm", "ws")
    assert info.value.missing_capability == "full_access"
    assert "capability 'full_access'" in info.value.explain()


def test_404_kb_read_not_found(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/kb/read",
        (404, {"ok": False, "found": False, "title": "T", "error": "note not found"}),
    )
    err = call_kb_read(pebble)
    assert err.not_found
    assert err.body["title"] == "T"


def test_404_session_not_yours(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/sessions/full-access-status",
        (404, {"ok": False, "error": "unknown session"}),
    )
    with pytest.raises(PebbleError) as info:
        EdgeClient(BASE, TOKEN, transport=pebble.transport).full_access("status", "ws-x")
    assert not info.value.not_found  # no `found: false`: this is the "not yours" 404
    assert "own sessions" in info.value.explain()


def test_422_policy_refusal(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/skills/publish",
        (
            422,
            {
                "ok": False,
                "error": "skill body contains a secret",
                "refused_by": "policy",
                "verdict": "deny",
            },
        ),
    )
    with pytest.raises(PebbleError) as info:
        EdgeClient(BASE, TOKEN, transport=pebble.transport).skills_publish("n", "b", "r")
    err = info.value
    assert err.refused_by == "policy"
    text = err.explain()
    assert "skill body contains a secret" in text
    assert "policy" in text and '"verdict": "deny"' in text


@pytest.mark.parametrize(("status", "hint"), [(500, "internally"), (503, "storage")])
def test_5xx(pebble: FakePebble, status: int, hint: str) -> None:
    pebble.on("POST", f"{E}/kb/read", (status, {"ok": False, "error": "boom"}))
    err = call_kb_read(pebble)
    assert err.status == status
    assert "boom" in err.explain() and hint in err.explain()


def test_non_json_error_body_is_shown_verbatim(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/read", lambda _r: httpx.Response(502, text="<h1>Bad Gateway</h1>"))
    err = call_kb_read(pebble)
    assert err.status == 502
    assert "<h1>Bad Gateway</h1>" in err.error


def test_ok_false_with_200_is_still_an_error(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/read", (200, {"ok": False, "error": "weird"}))
    err = call_kb_read(pebble)
    assert err.error == "weird"


def test_transport_failure() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TransportError, match="refused"):
        EdgeClient(BASE, TOKEN, transport=httpx.MockTransport(boom)).kb_read("T")
