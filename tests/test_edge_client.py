"""Request shapes and response parsing for every documented edge route."""

from __future__ import annotations

import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment.edge import EdgeClient

E = "/v1/api/edge"


def client(pebble: FakePebble) -> EdgeClient:
    return EdgeClient(BASE, TOKEN, transport=pebble.transport)


def test_bearer_token_is_sent(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/search", (200, {"ok": True, "results": []}))
    client(pebble).kb_search("anything")
    assert pebble.last.headers["authorization"] == f"Bearer {TOKEN}"


def test_capabilities_get_and_parse(pebble: FakePebble) -> None:
    pebble.on(
        "GET",
        f"{E}/capabilities",
        (
            200,
            {
                "ok": True,
                "user_id": "u-1",
                "scopes": ["read"],
                "capabilities": None,
                "operations": [
                    {
                        "name": "kb.write",
                        "method": "POST",
                        "path": f"{E}/kb/write",
                        "requires": {"scope": "write", "capability": None},
                        "available": False,
                        "missing": ["scope:write"],
                    }
                ],
                "full_access": {"armed": False},
            },
        ),
    )
    caps = client(pebble).capabilities(ws_id="ws-9")
    assert pebble.last.method == "GET"
    assert pebble.last.body is None
    assert pebble.last.query == {"ws_id": "ws-9"}
    assert caps.user_id == "u-1"
    assert caps.capabilities is None
    op = caps.operations[0]
    assert (op.name, op.scope, op.capability, op.available, op.missing) == (
        "kb.write",
        "write",
        None,
        False,
        ["scope:write"],
    )
    assert caps.full_access == {"armed": False}


def test_skills_pull_body_and_bundle(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/skills/pull",
        (
            200,
            {
                "ok": True,
                "repo": "demo",
                "count": 1,
                "skills": [
                    {
                        "name": "py-style",
                        "description": "d",
                        "content": "use ruff",
                        "version": 3,
                        "repo": "demo",
                        "global": False,
                        "tags": ["py"],
                        "activation": "auto",
                        "token_estimate": 12,
                        "skill_id": 7,
                        "paths": ["**/*.py"],
                        "allowed_tools": ["read_file"],
                    }
                ],
                "token_estimate": 12,
                "token_budget": 20000,
                "truncated": ["big-skill"],
                "not_found": ["ghost"],
            },
        ),
    )
    bundle = client(pebble).skills_pull(repo="demo", names=["py-style", "ghost"], max_tokens=20000)
    assert pebble.last.body == {"repo": "demo", "names": ["py-style", "ghost"], "max_tokens": 20000}
    skill = bundle.skills[0]
    assert (skill.name, skill.version, skill.skill_id, skill.paths) == ("py-style", "3", "7", ["**/*.py"])
    assert bundle.truncated == ["big-skill"]
    assert bundle.not_found == ["ghost"]


def test_skills_pull_defaults(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/skills/pull", (200, {"ok": True, "skills": []}))
    client(pebble).skills_pull()
    assert pebble.last.body == {"repo": "", "names": []}


def test_skills_publish_body_never_sends_global(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/skills/publish", (200, {"ok": True, "published": True}))
    client(pebble).skills_publish("n", "b", "demo", description="d", tags=["t"], paths=["src/*"])
    assert pebble.last.body == {
        "name": "n",
        "body": "b",
        "repo": "demo",
        "description": "d",
        "tags": ["t"],
        "paths": ["src/*"],
    }


@pytest.mark.parametrize("missing", ["name", "body", "repo"])
def test_skills_publish_requires_fields(pebble: FakePebble, missing: str) -> None:
    args = {"name": "n", "body": "b", "repo": "r", missing: ""}
    with pytest.raises(ValueError, match=missing):
        client(pebble).skills_publish(**args)
    assert pebble.requests == []


def test_skills_hook_body(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/skills/hook", (200, {"ok": True, "settings_json": {}}))
    client(pebble).skills_hook()
    assert pebble.last.body == {}
    client(pebble).skills_hook(report_url="https://r.test/x")
    assert pebble.last.body == {"report_url": "https://r.test/x"}


def test_kb_search_body_and_results(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        f"{E}/kb/search",
        (
            200,
            {
                "ok": True,
                "query": "q",
                "count": 1,
                "results": [
                    {
                        "title": "T",
                        "kind": "note",
                        "repo": "demo",
                        "tags": ["a"],
                        "summary": "s",
                        "links": ["U"],
                        "score": 1.5,
                    }
                ],
            },
        ),
    )
    hits = client(pebble).kb_search("q", limit=5, repo="demo")
    assert pebble.last.body == {"query": "q", "limit": 5, "repo": "demo"}
    assert hits[0].title == "T" and hits[0].score == 1.5 and hits[0].links == ["U"]


def test_kb_search_validates_locally(pebble: FakePebble) -> None:
    with pytest.raises(ValueError):
        client(pebble).kb_search("  ")
    with pytest.raises(ValueError):
        client(pebble).kb_search("q", limit=51)
    assert pebble.requests == []


def test_kb_read_body(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/read", (200, {"ok": True, "title": "T", "body": "hello"}))
    note = client(pebble).kb_read("T")
    assert pebble.last.body == {"title": "T"}
    assert note["body"] == "hello"


def test_kb_write_body_minimal_and_full(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/write", (200, {"ok": True, "title": "T", "path": "p"}))
    c = client(pebble)
    c.kb_write("T")
    assert pebble.last.body == {"title": "T"}
    c.kb_write(
        "T", body="b", kind="note", summary="s", tags=["x"], repo="demo", append=True, color="red"
    )
    assert pebble.last.body == {
        "title": "T",
        "body": "b",
        "kind": "note",
        "summary": "s",
        "repo": "demo",
        "color": "red",
        "tags": ["x"],
        "append": True,
    }


def test_kb_write_requires_title(pebble: FakePebble) -> None:
    with pytest.raises(ValueError, match="title"):
        client(pebble).kb_write(" ")
    assert pebble.requests == []


def test_kb_experiment_body(pebble: FakePebble) -> None:
    pebble.on("POST", f"{E}/kb/experiment", (200, {"ok": True, "verdict": "pass", "path": "p"}))
    client(pebble).kb_experiment(
        title="T",
        command="pytest",
        exit_code=0,
        hypothesis="h",
        output="out",
        duration_seconds=1.5,
        repo="demo",
        commit="abc",
    )
    assert pebble.last.body == {
        "title": "T",
        "command": "pytest",
        "exit_code": 0,
        "hypothesis": "h",
        "output": "out",
        "duration_seconds": 1.5,
        "repo": "demo",
        "commit": "abc",
    }


def test_kb_experiment_exit_code_is_never_defaulted(pebble: FakePebble) -> None:
    c = client(pebble)
    with pytest.raises(TypeError):
        c.kb_experiment(title="T", command="pytest")  # type: ignore[call-arg]
    for bogus in (None, True, "0", 0.0):
        with pytest.raises(ValueError, match="exit_code"):
            c.kb_experiment(title="T", command="pytest", exit_code=bogus)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="command"):
        c.kb_experiment(title="T", command="", exit_code=0)
    with pytest.raises(ValueError, match="title"):
        c.kb_experiment(title="", command="x", exit_code=0)
    assert pebble.requests == []


@pytest.mark.parametrize(
    ("action", "route"),
    [
        ("arm", "sessions/arm-full-access"),
        ("disarm", "sessions/disarm-full-access"),
        ("status", "sessions/full-access-status"),
    ],
)
def test_full_access_routes(pebble: FakePebble, action: str, route: str) -> None:
    pebble.on("POST", f"{E}/{route}", (200, {"ok": True, "armed": action == "arm"}))
    client(pebble).full_access(action, "ws-1")
    assert pebble.last.path == f"{E}/{route}"
    assert pebble.last.body == {"ws_id": "ws-1"}
