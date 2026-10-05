"""Client for pebble's edge API (`/v1/api/edge/*`) and nothing else.

Every method here maps to exactly one documented edge route; request bodies
contain only documented fields. Routes that are *not* edge routes (workstreams,
skill-use reporting) live in their own modules so it is obvious, when reading
the code, which calls fall outside edge authorization.

Validation that protects honesty happens here, before any bytes leave: e.g. an
experiment without a real exit code is refused locally, because a defaulted
exit code would record a pass nobody measured.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from sediment.envelope import request_json
from sediment.jsonish import (
    JSON,
    JSONObject,
    get_bool,
    get_int,
    get_obj,
    get_obj_list,
    get_opt_str,
    get_str,
    get_str_list,
)

EDGE = "/v1/api/edge"


@dataclass(frozen=True)
class Operation:
    name: str
    method: str
    path: str
    scope: str | None
    capability: str | None
    available: bool
    missing: list[str]


@dataclass(frozen=True)
class Capabilities:
    user_id: str
    scopes: list[str]
    capabilities: JSON
    operations: list[Operation]
    full_access: JSONObject | None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    version: str
    repo: str
    is_global: bool
    tags: list[str]
    activation: str
    token_estimate: int
    skill_id: str
    paths: list[str]
    allowed_tools: list[str]

    @classmethod
    def from_json(cls, raw: JSONObject) -> Skill:
        version = raw.get("version")
        return cls(
            name=get_str(raw, "name"),
            description=get_str(raw, "description"),
            content=get_str(raw, "content"),
            version="" if version is None else str(version),
            repo=get_str(raw, "repo"),
            is_global=get_bool(raw, "global"),
            tags=get_str_list(raw, "tags"),
            activation=get_str(raw, "activation"),
            token_estimate=get_int(raw, "token_estimate"),
            skill_id=str(raw.get("skill_id") or ""),
            paths=get_str_list(raw, "paths"),
            allowed_tools=get_str_list(raw, "allowed_tools"),
        )


@dataclass(frozen=True)
class SkillBundle:
    repo: str
    skills: list[Skill]
    token_estimate: int
    token_budget: int
    truncated: list[str]
    not_found: list[str]
    raw: JSONObject

    @classmethod
    def from_json(cls, raw: JSONObject) -> SkillBundle:
        """Also used to rebuild a bundle from the local cache, which stores the raw body."""
        return cls(
            repo=get_str(raw, "repo"),
            skills=[Skill.from_json(s) for s in get_obj_list(raw, "skills")],
            token_estimate=get_int(raw, "token_estimate"),
            token_budget=get_int(raw, "token_budget"),
            truncated=get_str_list(raw, "truncated"),
            not_found=get_str_list(raw, "not_found"),
            raw=raw,
        )


@dataclass(frozen=True)
class KbHit:
    title: str
    kind: str
    repo: str
    tags: list[str]
    summary: str
    links: list[str]
    score: float


class EdgeClient:
    """Thin, typed wrapper over the edge routes. Owns no state beyond the HTTP client."""

    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    def _post(self, route: str, body: JSONObject) -> JSONObject:
        return request_json(
            self._http, "POST", f"{EDGE}/{route}", self._token, body, expect_envelope=True
        )

    # -- discovery -----------------------------------------------------------

    def capabilities(self, ws_id: str | None = None) -> Capabilities:
        params = {"ws_id": ws_id} if ws_id else None
        raw = request_json(
            self._http,
            "GET",
            f"{EDGE}/capabilities",
            self._token,
            params=params,
            expect_envelope=True,
        )
        operations = []
        for op in get_obj_list(raw, "operations"):
            requires = get_obj(op, "requires") or {}
            operations.append(
                Operation(
                    name=get_str(op, "name"),
                    method=get_str(op, "method"),
                    path=get_str(op, "path"),
                    scope=get_opt_str(requires, "scope"),
                    capability=get_opt_str(requires, "capability"),
                    available=get_bool(op, "available"),
                    missing=get_str_list(op, "missing"),
                )
            )
        return Capabilities(
            user_id=str(raw.get("user_id") or ""),
            scopes=get_str_list(raw, "scopes"),
            capabilities=raw.get("capabilities"),
            operations=operations,
            full_access=get_obj(raw, "full_access"),
        )

    # -- skills --------------------------------------------------------------

    def skills_pull(
        self, repo: str = "", names: list[str] | None = None, max_tokens: int | None = None
    ) -> SkillBundle:
        body: JSONObject = {"repo": repo, "names": list(names or [])}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return SkillBundle.from_json(self._post("skills/pull", body))

    def skills_publish(
        self,
        name: str,
        body: str,
        repo: str,
        description: str | None = None,
        tags: list[str] | None = None,
        paths: list[str] | None = None,
    ) -> JSONObject:
        for field_name, value in (("name", name), ("body", body), ("repo", repo)):
            if not value:
                raise ValueError(f"skills/publish requires a non-empty '{field_name}'")
        payload: JSONObject = {"name": name, "body": body, "repo": repo}
        if description is not None:
            payload["description"] = description
        if tags:
            payload["tags"] = list(tags)
        if paths:
            payload["paths"] = list(paths)
        return self._post("skills/publish", payload)

    def skills_hook(self, report_url: str | None = None) -> JSONObject:
        payload: JSONObject = {"report_url": report_url} if report_url else {}
        return self._post("skills/hook", payload)

    # -- knowledge base ------------------------------------------------------

    def kb_search(self, query: str, limit: int = 10, repo: str | None = None) -> list[KbHit]:
        if not query.strip():
            raise ValueError("kb/search requires a non-empty 'query'")
        if not 1 <= limit <= 50:
            raise ValueError("kb/search 'limit' must be between 1 and 50")
        payload: JSONObject = {"query": query, "limit": limit}
        if repo:
            payload["repo"] = repo
        raw = self._post("kb/search", payload)
        hits = []
        for r in get_obj_list(raw, "results"):
            score = r.get("score")
            hits.append(
                KbHit(
                    title=get_str(r, "title"),
                    kind=get_str(r, "kind"),
                    repo=get_str(r, "repo"),
                    tags=get_str_list(r, "tags"),
                    summary=get_str(r, "summary"),
                    links=get_str_list(r, "links"),
                    score=float(score) if isinstance(score, int | float) else 0.0,
                )
            )
        return hits

    def kb_read(self, title: str) -> JSONObject:
        """Returns the note. A missing note raises PebbleError with `.not_found` true."""
        if not title.strip():
            raise ValueError("kb/read requires a non-empty 'title'")
        return self._post("kb/read", {"title": title})

    def kb_write(
        self,
        title: str,
        body: str | None = None,
        kind: str | None = None,
        summary: str | None = None,
        tags: list[str] | None = None,
        repo: str | None = None,
        append: bool = False,
        color: str | None = None,
    ) -> JSONObject:
        if not title.strip():
            raise ValueError("kb/write requires a non-empty 'title'")
        payload: JSONObject = {"title": title}
        optional: dict[str, str | None] = {
            "body": body,
            "kind": kind,
            "summary": summary,
            "repo": repo,
            "color": color,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        if tags:
            payload["tags"] = list(tags)
        if append:
            payload["append"] = True
        return self._post("kb/write", payload)

    def kb_experiment(
        self,
        *,
        title: str,
        command: str,
        exit_code: int,
        hypothesis: str | None = None,
        output: str | None = None,
        duration_seconds: float | None = None,
        repo: str | None = None,
        commit: str | None = None,
    ) -> JSONObject:
        """Record a command that was really run. `exit_code` has no default, on purpose."""
        if not title.strip():
            raise ValueError("kb/experiment requires a non-empty 'title'")
        if not command.strip():
            raise ValueError("kb/experiment requires a non-empty 'command'")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError(
                "kb/experiment requires a measured integer 'exit_code'; refusing to guess one"
            )
        payload: JSONObject = {"title": title, "command": command, "exit_code": exit_code}
        if hypothesis is not None:
            payload["hypothesis"] = hypothesis
        if output is not None:
            payload["output"] = output
        if duration_seconds is not None:
            payload["duration_seconds"] = duration_seconds
        if repo:
            payload["repo"] = repo
        if commit:
            payload["commit"] = commit
        return self._post("kb/experiment", payload)

    # -- full-access sessions -------------------------------------------------

    def full_access(self, action: str, ws_id: str) -> JSONObject:
        """`action` is one of arm, disarm, status. Needs write scope + full_access capability."""
        routes = {
            "arm": "sessions/arm-full-access",
            "disarm": "sessions/disarm-full-access",
            "status": "sessions/full-access-status",
        }
        if action not in routes:
            raise ValueError(f"unknown full-access action {action!r}")
        if not ws_id:
            raise ValueError("full-access calls require a 'ws_id'")
        return self._post(routes[action], {"ws_id": ws_id})
