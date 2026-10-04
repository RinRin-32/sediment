"""CLI commands that wrap edge calls: doctor, kb, experiment, hook, and top-level errors."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment import cli
from sediment.config import Config
from sediment.edge import EdgeClient
from sediment.ui import Console

E = "/v1/api/edge"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, pebble: FakePebble) -> FakePebble:
    monkeypatch.setattr(cli, "_edge", lambda _c: EdgeClient(BASE, TOKEN, transport=pebble.transport))
    return pebble


def run(handler: cli.Handler, config: Config, **args: object) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = handler(argparse.Namespace(**args), config, Console(out, err))
    return code, out.getvalue(), err.getvalue()


def test_doctor_shows_identity_scopes_and_operations(wired: FakePebble, config: Config) -> None:
    wired.on(
        "GET",
        f"{E}/capabilities",
        (
            200,
            {
                "ok": True,
                "user_id": "alice",
                "scopes": ["read"],
                "capabilities": None,
                "operations": [
                    {
                        "name": "kb.search",
                        "method": "POST",
                        "path": f"{E}/kb/search",
                        "requires": {"scope": "read"},
                        "available": True,
                        "missing": [],
                    },
                    {
                        "name": "sessions.arm",
                        "method": "POST",
                        "path": f"{E}/sessions/arm-full-access",
                        "requires": {"scope": "write", "capability": "full_access"},
                        "available": False,
                        "missing": ["scope:write", "capability:full_access"],
                    },
                ],
                "full_access": {"can_arm": False},
            },
        ),
    )
    code, out, _ = run(cli.cmd_doctor, config, ws_id=None)
    assert code == 0
    assert "user_id:  alice" in out and "scopes:   read" in out
    assert "null" in out
    assert "[yes] kb.search" in out
    assert "[NO ] sessions.arm" in out and "missing: scope:write, capability:full_access" in out
    assert "1/2 available" in out
    assert TOKEN not in out and "len 25" in out
    assert "workstreams" in out  # non-edge routes are called out


def test_kb_read_not_found_is_calm(wired: FakePebble, config: Config) -> None:
    wired.on("POST", f"{E}/kb/read", (404, {"ok": False, "found": False, "title": "X", "error": "not found"}))
    code, _out, err = run(cli.cmd_kb_read, config, title="X")
    assert code == 1 and "no KB note titled 'X'" in err and "not found" in err


def test_kb_write_attributes_repo(wired: FakePebble, config: Config) -> None:
    wired.on("POST", f"{E}/kb/write", (200, {"ok": True, "title": "T", "path": "kb/T.md", "appended": True}))
    code, out, _ = run(
        cli.cmd_kb_write,
        config,
        title="T",
        body="b",
        file=None,
        kind="note",
        summary=None,
        tag=["x"],
        repo=None,
        append=True,
        color=None,
    )
    assert code == 0 and "appended to T -> kb/T.md" in out
    assert wired.last.body == {
        "title": "T",
        "body": "b",
        "kind": "note",
        "repo": "demo",
        "tags": ["x"],
        "append": True,
    }


def test_kb_experiment_records_the_real_exit_code(
    wired: FakePebble, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    wired.on("POST", f"{E}/kb/experiment", (200, {"ok": True, "title": "T", "path": "p", "verdict": "fail"}))
    command = ["--", sys.executable, "-c", "print('measured'); raise SystemExit(4)"]
    code, out, err = run(cli.cmd_kb_experiment, config, title="T", hypothesis="h", command=command)
    assert code == 0
    body = wired.last.body
    assert isinstance(body, dict)
    assert body["exit_code"] == 4
    assert body["output"] == "measured\n"
    assert body["hypothesis"] == "h" and body["repo"] == "demo"
    assert "SystemExit(4)" in str(body["command"])
    assert isinstance(body["duration_seconds"], float)
    assert "measured" in err and "verdict: fail" in out


def test_kb_experiment_that_cannot_start_records_nothing(
    wired: FakePebble, config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _out, err = run(
        cli.cmd_kb_experiment, config, title="T", hypothesis=None, command=["definitely-not-a-binary-xyz"]
    )
    assert code == 2 and "nothing recorded" in err
    assert wired.requests == []


def test_skills_hook_merges_into_settings_file(wired: FakePebble, config: Config, tmp_path: Path) -> None:
    secret = "pbr_hooktoken"
    wired.on(
        "POST",
        f"{E}/skills/hook",
        (
            200,
            {
                "ok": True,
                "settings_json": json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {
                                    "matcher": "Skill",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": f"curl -H 'Authorization: Bearer {secret}'",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ),
                "expires_hours": 12,
                "note": "expires in 12h",
            },
        ),
    )
    target = tmp_path / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"model": "x", "hooks": {"PostToolUse": [{"matcher": "Old"}]}}))
    code, out, err = run(cli.cmd_skills_hook, config, report_url=None, out=str(target))
    assert code == 0
    merged = json.loads(target.read_text())
    assert merged["model"] == "x"
    assert [h["matcher"] for h in merged["hooks"]["PostToolUse"]] == ["Old", "Skill"]
    assert target.stat().st_mode & 0o777 == 0o600
    assert secret not in out + err
    assert "12 hours" in err and "expires in 12h" in err


def test_main_reports_config_problems_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("SEDIMENT_CONFIG", "SEDIAMENT_CONFIG", "SEDIMENT_PEBBLE_URL", "SEDIMENT_PEBBLE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cli.main(["doctor"]) == 2
    err = capsys.readouterr().err
    assert "missing required settings" in err and "Traceback" not in err


def test_main_prints_server_explanation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pebble: FakePebble
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SEDIMENT_CONFIG", raising=False)
    monkeypatch.setenv("SEDIMENT_PEBBLE_URL", BASE)
    monkeypatch.setenv("SEDIMENT_PEBBLE_TOKEN", TOKEN)
    monkeypatch.setattr(cli, "_edge", lambda _c: EdgeClient(BASE, TOKEN, transport=pebble.transport))
    pebble.on("POST", f"{E}/kb/search", (403, {"ok": False, "error": "nope", "missing_scope": "read"}))
    assert cli.main(["kb", "search", "q"]) == 1
    err = capsys.readouterr().err
    assert "HTTP 403: nope" in err and "lacks the scope 'read'" in err
