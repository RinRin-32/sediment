"""Skill-use reporting. NOT an edge route.

`POST /v1/api/skills/report` wants a token with the narrow `skills.report`
scope; pebble refuses ordinary read/write tokens there (403). The only way an
edge client can get such a token is `skills/hook`, and pebble returns it only
embedded in the curl command of a Claude Code hook config; there is no field
holding the raw token. So we dig it out of that command string, tolerantly,
and say so plainly when we can't. (Documented as a known edge-API gap.)

A failed report is shown to the user but never interrupts a session.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable

import httpx

from sediment.envelope import request_json
from sediment.jsonish import JSON, JSONObject

REPORT_PATH = "/v1/api/skills/report"

# Matches `Authorization: Bearer <tok>` inside -H '...', -H "...", or bare.
_BEARER = re.compile(r"Authorization:\s*Bearer\s+([^\s'\"\\]+)", re.IGNORECASE)


class ReportTokenError(Exception):
    """The hook config did not contain a token we could recognise."""


def _strings(value: JSON) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _strings(item)]
    return []


def extract_report_token(settings_json: JSON) -> str:
    """Find the bearer token inside the hook's curl command.

    `settings_json` may arrive as an object or as a JSON-encoded string; both are
    accepted. Shell variables (`$TOKEN`) are rejected: that would mean pebble
    changed the format and the literal token is no longer present.
    """
    if isinstance(settings_json, str):
        # If it isn't JSON, scan it as the raw command text itself.
        with contextlib.suppress(json.JSONDecodeError):
            settings_json = json.loads(settings_json)
    found = {m.group(1) for text in _strings(settings_json) for m in _BEARER.finditer(text)}
    literal = {tok for tok in found if not tok.startswith("$")}
    if len(literal) == 1:
        return literal.pop()
    if not literal:
        raise ReportTokenError(
            "skills/hook returned no literal 'Authorization: Bearer <token>' in its hook "
            "command; cannot report skill use. (pebble exposes no raw token field.)"
        )
    raise ReportTokenError(
        f"skills/hook command contained {len(literal)} different bearer tokens; "
        "refusing to guess which one is the report token."
    )


class SkillReporter:
    """Reports skill activations with a `skills.report` token minted on first use."""

    def __init__(
        self,
        base_url: str,
        mint_hook: Callable[[], JSONObject],
        session_id: str,
        repo: str,
        transport: httpx.BaseTransport | None = None,
    ):
        self._mint_hook = mint_hook
        self._session_id = session_id
        self._repo = repo
        self._token: str | None = None
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=15.0)

    def close(self) -> None:
        self._http.close()

    def report(self, name: str) -> None:
        """Raise on failure; the caller decides how to show it (it must not end the session)."""
        if self._token is None:
            hook = self._mint_hook()
            self._token = extract_report_token(hook.get("settings_json"))
        body: JSONObject = {"name": name, "session_id": self._session_id, "repo": self._repo}
        # Non-edge route with no documented response shape, so judge it by HTTP status,
        # not by the edge envelope (an explicit `"ok": false` still counts as failure).
        request_json(self._http, "POST", REPORT_PATH, self._token, body, expect_envelope=False)
