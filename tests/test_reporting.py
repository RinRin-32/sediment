"""Report-token extraction from the hook's curl command, and the non-edge report call."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment.edge import EdgeClient
from sediment.envelope import PebbleError
from sediment.jsonish import JSONObject
from sediment.reporting import REPORT_PATH, ReportTokenError, SkillReporter, extract_report_token

REPORT_TOKEN = "pbr_report.Tok-123_abc"


def hook_settings(command: str) -> JSONObject:
    return {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Skill", "hooks": [{"type": "command", "command": command}]}
            ]
        }
    }


@pytest.mark.parametrize(
    "command",
    [
        f"curl -s -X POST {BASE}{REPORT_PATH} -H 'Authorization: Bearer {REPORT_TOKEN}' -d @-",
        f'curl -s -H "Authorization: Bearer {REPORT_TOKEN}" -H "Content-Type: application/json"',
        f"curl -sH authorization:bearer {REPORT_TOKEN} {BASE}{REPORT_PATH}",
    ],
)
def test_extracts_token_from_dict_settings(command: str) -> None:
    assert extract_report_token(hook_settings(command)) == REPORT_TOKEN


def test_extracts_token_when_settings_json_is_a_string() -> None:
    encoded = json.dumps(hook_settings(f'curl -H "Authorization: Bearer {REPORT_TOKEN}" x'))
    assert extract_report_token(encoded) == REPORT_TOKEN


def test_shell_variable_is_not_a_token() -> None:
    with pytest.raises(ReportTokenError, match="no literal"):
        extract_report_token(hook_settings('curl -H "Authorization: Bearer $PEBBLE_TOKEN" x'))


def test_missing_token_is_a_clear_error() -> None:
    with pytest.raises(ReportTokenError, match="raw token"):
        extract_report_token(hook_settings("curl https://example.test"))
    with pytest.raises(ReportTokenError):
        extract_report_token(None)


def test_ambiguous_tokens_are_refused() -> None:
    settings = hook_settings("curl -H 'Authorization: Bearer one' && curl -H 'Authorization: Bearer two'")
    with pytest.raises(ReportTokenError, match="2 different"):
        extract_report_token(settings)


def test_reporter_mints_once_and_posts_with_report_token(pebble: FakePebble) -> None:
    pebble.on(
        "POST",
        "/v1/api/edge/skills/hook",
        (
            200,
            {
                "ok": True,
                "settings_json": hook_settings(f"curl -H 'Authorization: Bearer {REPORT_TOKEN}'"),
                "expires_hours": 12,
            },
        ),
    )
    pebble.on("POST", REPORT_PATH, (200, {"ok": True}))
    edge = EdgeClient(BASE, TOKEN, transport=pebble.transport)
    reporter = SkillReporter(BASE, edge.skills_hook, "sess-1", "demo", transport=pebble.transport)
    reporter.report("py-style")
    reporter.report("py-style")

    paths = [r.path for r in pebble.requests]
    assert paths.count("/v1/api/edge/skills/hook") == 1
    reports = [r for r in pebble.requests if r.path == REPORT_PATH]
    assert len(reports) == 2
    assert reports[0].headers["authorization"] == f"Bearer {REPORT_TOKEN}"
    assert reports[0].body == {"name": "py-style", "session_id": "sess-1", "repo": "demo"}
    # the hook was minted with the ordinary token, never the report token
    hook = next(r for r in pebble.requests if r.path.endswith("skills/hook"))
    assert hook.headers["authorization"] == f"Bearer {TOKEN}"


def report_only_reporter(pebble: FakePebble) -> SkillReporter:
    return SkillReporter(
        BASE,
        lambda: {"settings_json": hook_settings(f"-H 'Authorization: Bearer {REPORT_TOKEN}'")},
        "s",
        "r",
        transport=pebble.transport,
    )


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"recorded": True}),  # no `ok`: not an edge route
        httpx.Response(200, json={}),
        httpx.Response(204),
    ],
)
def test_report_success_is_judged_by_status_not_the_edge_envelope(
    pebble: FakePebble, reply: httpx.Response
) -> None:
    pebble.on("POST", REPORT_PATH, lambda _r: reply)
    report_only_reporter(pebble).report("x")  # must not raise
    assert pebble.last.path == REPORT_PATH


def test_report_body_that_says_ok_false_is_a_failure(pebble: FakePebble) -> None:
    pebble.on("POST", REPORT_PATH, (200, {"ok": False, "error": "unknown skill"}))
    with pytest.raises(PebbleError, match="unknown skill"):
        report_only_reporter(pebble).report("x")


def test_reporter_surfaces_a_403(pebble: FakePebble) -> None:
    pebble.on("POST", REPORT_PATH, (403, {"ok": False, "error": "wrong scope", "missing_scope": "skills.report"}))
    reporter = SkillReporter(
        BASE,
        lambda: {"settings_json": hook_settings(f"-H 'Authorization: Bearer {REPORT_TOKEN}'")},
        "s",
        "r",
        transport=pebble.transport,
    )
    with pytest.raises(PebbleError) as info:
        reporter.report("x")
    assert info.value.missing_scope == "skills.report"
