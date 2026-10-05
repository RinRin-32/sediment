"""Local half of skills: path filtering, the cache, loud warnings, prompt injection.

pebble decides *which* skills a repo has and enforces the token budget, but it
explicitly does not look at your working tree. Each skill may declare `paths`
globs; whether those match is a fact about the laptop, so sediment checks them
here with `fnmatch` and records why each skill is or isn't active.

Truncation is the failure mode to avoid: a budget-trimmed bundle that looks
complete. Every pull, list and chat start repeats `truncated` and `not_found`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from sediment.edge import Skill, SkillBundle
from sediment.jsonish import JSON


@dataclass(frozen=True)
class SkillMatch:
    skill: Skill
    active: bool
    reason: str


def match_skills(skills: list[Skill], tree: list[str]) -> list[SkillMatch]:
    """A skill with no `paths` applies everywhere; otherwise one matching file activates it."""
    results = []
    for skill in skills:
        if not skill.paths:
            results.append(SkillMatch(skill, True, "no paths declared; applies to the whole repo"))
            continue
        hit = _first_match(skill.paths, tree)
        if hit:
            pattern, path = hit
            results.append(SkillMatch(skill, True, f"glob {pattern!r} matched {path}"))
        else:
            globs = ", ".join(repr(p) for p in skill.paths)
            results.append(SkillMatch(skill, False, f"no file in the working tree matched {globs}"))
    return results


def _first_match(patterns: list[str], tree: list[str]) -> tuple[str, str] | None:
    for pattern in patterns:
        normalized = pattern.removeprefix("./").lstrip("/")
        for path in tree:
            # fnmatch's `*` already crosses `/`, so `src/**/*.py` matches `src/a/b.py`,
            # but not `src/b.py`. Trying the `**/`-collapsed form too gives the
            # zero-directories meaning people expect from `**`.
            if fnmatchcase(path, normalized) or fnmatchcase(path, normalized.replace("**/", "")):
                return pattern, path
    return None


def bundle_warnings(bundle: SkillBundle, requested_max: int | None = None) -> list[str]:
    """Everything the user must not miss about an incomplete bundle."""
    warnings = []
    if bundle.truncated:
        warnings.append(
            f"TRUNCATED: {len(bundle.truncated)} skill(s) were cut to fit the token budget "
            f"({bundle.token_estimate}/{bundle.token_budget} tokens): "
            + ", ".join(bundle.truncated)
        )
    if bundle.not_found:
        warnings.append(
            f"NOT FOUND: {len(bundle.not_found)} requested skill(s) do not exist on pebble: "
            + ", ".join(bundle.not_found)
        )
    if requested_max is not None and bundle.token_budget and bundle.token_budget < requested_max:
        warnings.append(
            f"BUDGET: asked for max_tokens={requested_max}, pebble applied {bundle.token_budget}."
        )
    return warnings


# -- cache ------------------------------------------------------------------


def _slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", repo) or "_no_repo_"


def cache_path(cache_dir: Path, repo: str) -> Path:
    return cache_dir / "skills" / f"{_slug(repo)}.json"


def save_bundle(cache_dir: Path, repo: str, bundle: SkillBundle) -> Path:
    path = cache_path(cache_dir, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, JSON] = {"repo": repo, "pulled_at": time.time(), "bundle": bundle.raw}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


@dataclass(frozen=True)
class CachedBundle:
    bundle: SkillBundle
    pulled_at: float
    path: Path


def load_bundle(cache_dir: Path, repo: str) -> CachedBundle | None:
    """None if nothing was pulled yet. A corrupt cache is an error, not an empty bundle."""
    path = cache_path(cache_dir, repo)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"skill cache {path} is unreadable ({exc}); re-run `sediment skills pull`") from exc
    raw = record.get("bundle") if isinstance(record, dict) else None
    if not isinstance(raw, dict):
        raise ValueError(f"skill cache {path} has no bundle; re-run `sediment skills pull`")
    pulled_at = record.get("pulled_at")
    return CachedBundle(
        bundle=SkillBundle.from_json(raw),
        pulled_at=float(pulled_at) if isinstance(pulled_at, int | float) else 0.0,
        path=path,
    )


# -- prompt -----------------------------------------------------------------


def render_skill_prompt(active: list[Skill]) -> str:
    if not active:
        return ""
    parts = [
        "# Skills from pebble",
        "These skills matched this working tree. When you apply one, first call the "
        "`use_skill` tool with its name so the use is recorded.",
    ]
    for skill in active:
        header = f"## Skill: {skill.name}"
        if skill.version:
            header += f" (v{skill.version})"
        parts.append(header)
        if skill.description:
            parts.append(skill.description)
        if skill.allowed_tools:
            parts.append("Declared tools: " + ", ".join(skill.allowed_tools))
        parts.append(skill.content)
    return "\n\n".join(parts)
