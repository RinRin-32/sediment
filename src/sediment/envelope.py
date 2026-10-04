"""pebble's response contracts, and the one place HTTP errors become exceptions.

The edge API answers `{"ok": true, ...}` on success and `{"ok": false, "error": ...}`
with a non-2xx status on failure. That envelope is promised for `/v1/api/edge/*`
only; older non-edge routes on the same host answer without an `ok` field, so
each request states which contract it expects. The failure body often carries the precise
reason (`missing_scope`, `missing_capability`, `refused_by`, `found`). We keep
the server's words verbatim and surface them; a client that paraphrases or
swallows an error makes the operator guess, which is exactly what we avoid.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from sediment.jsonish import JSON, JSONObject, get_bool, get_opt_str

_TEXT_PREVIEW = 500


@dataclass(eq=False)
class PebbleError(Exception):
    """A refused or failed call, carrying everything the server told us."""

    status: int
    method: str
    path: str
    error: str
    body: JSONObject = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.method} {self.path} -> HTTP {self.status}: {self.error}"

    @property
    def missing_scope(self) -> str | None:
        return get_opt_str(self.body, "missing_scope")

    @property
    def missing_capability(self) -> str | None:
        return get_opt_str(self.body, "missing_capability")

    @property
    def refused_by(self) -> str | None:
        return get_opt_str(self.body, "refused_by")

    @property
    def not_found(self) -> bool:
        return self.status == 404 and not get_bool(self.body, "found", default=True)

    def explain(self) -> str:
        """A multi-line, human explanation: server text first, then what it means."""
        lines = [str(self)]
        extra = {k: v for k, v in self.body.items() if k not in {"ok", "error"}}
        if extra:
            lines.append(f"  server details: {json.dumps(extra, sort_keys=True)}")
        lines.extend(f"  {hint}" for hint in self._hints())
        return "\n".join(lines)

    def _hints(self) -> list[str]:
        if self.status == 401:
            return ["Your token was not accepted. Check pebble.token (or SEDIMENT_PEBBLE_TOKEN)."]
        if self.status == 403:
            if self.missing_scope:
                return [f"Your token lacks the scope '{self.missing_scope}'."]
            if self.missing_capability:
                return [f"Your account lacks the capability '{self.missing_capability}'."]
            return ["Refused (403). Run `sediment doctor` to see what this token may do."]
        if self.status == 404 and self.path.startswith("/v1/api/edge/sessions/"):
            return ["Not found: edge clients can only reach their own sessions."]
        if self.status == 422 and self.refused_by == "policy":
            return ["Refused by pebble's policy (not a transport error; nothing was written)."]
        if self.status == 503:
            return ["pebble reports its storage is unavailable. Nothing was written; retry later."]
        if self.status >= 500:
            return ["pebble failed internally. Nothing here was retried automatically."]
        return []


@dataclass(eq=False)
class TransportError(Exception):
    """We could not talk to the server at all (DNS, refused, timeout)."""

    method: str
    url: str
    reason: str

    def __str__(self) -> str:
        return f"{self.method} {self.url} failed before any response: {self.reason}"


def parse_response(
    response: httpx.Response, method: str, path: str, *, expect_envelope: bool
) -> JSONObject:
    """Return the success body, or raise PebbleError with the server's own words.

    pebble serves two response contracts on one host, and the caller must say which:

    - `expect_envelope=True` (the edge API, `/v1/api/edge/*`): success is a 2xx
      *and* `"ok": true`. A 2xx with `ok` false or missing is still a failure.
    - `expect_envelope=False` (older non-edge routes such as the workstream API,
      which have no `ok` field at all): success is any 2xx whose body is a JSON
      object, or empty. Two exceptions: a body that explicitly says `"ok": false`
      is a failure, and a 2xx with non-JSON text (a proxy or login page, say) is
      refused, because we cannot honestly call that a pebble answer.

    Either way a non-2xx raises with the server's text, never a paraphrase.
    """
    payload: JSON
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None

    if not isinstance(payload, dict):
        text = response.text[:_TEXT_PREVIEW]
        if response.is_success:
            if not expect_envelope and not response.content.strip():
                return {}
            raise PebbleError(
                response.status_code, method, path, f"success body is not a JSON object: {text!r}"
            )
        raise PebbleError(response.status_code, method, path, text or response.reason_phrase)

    if response.is_success:
        if payload.get("ok") is True:
            return payload
        # Without the envelope, a missing `ok` is normal; an explicit `ok: false` is
        # still the server saying it failed, and we take it at its word.
        if not expect_envelope and payload.get("ok") is not False:
            return payload
        if "ok" not in payload:
            failure = "response has no 'ok' field (this route promises the edge envelope)"
        else:
            failure = "server returned ok=false without an error message"
    else:
        failure = response.reason_phrase or "no error message in body"

    error = payload.get("error")
    if not isinstance(error, str) or not error:
        error = failure
    raise PebbleError(response.status_code, method, path, error, payload)


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    token: str,
    body: JSONObject | None = None,
    params: dict[str, str] | None = None,
    *,
    expect_envelope: bool,
) -> JSONObject:
    """Send one authenticated request and check it under the stated response contract.

    `expect_envelope` has no default on purpose: every call site has to say which
    surface it is talking to (see parse_response).
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = client.request(method, path, headers=headers, json=body, params=params)
    except httpx.HTTPError as exc:
        raise TransportError(method, path, f"{type(exc).__name__}: {exc}") from exc
    return parse_response(response, method, path, expect_envelope=expect_envelope)
