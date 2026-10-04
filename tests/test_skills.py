"""Local path filtering, cache round-trip, and loud reporting of incomplete bundles."""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pytest

from conftest import BASE, TOKEN, FakePebble
from sediment import cli
from sediment.config import Config
from sediment.edge import EdgeClient, Skill, SkillBundle
from sediment.jsonish import JSONObject
from sediment.skills import (
    bundle_warnings,
    load_bundle,
    match_skills,
    render_skill_prompt,
    save_bundle,
)
from sediment.ui import Console


def skill(name: str, paths: list[str], content: str = "body") -> Skill:
    return Skill.from_json({"name": name, "paths": list(paths), "content": content})


TREE = ["README.md", "src/app/main.py", "src/util.py", "web/index.ts"]


def test_skill_without_paths_is_always_active() -> None:
    [m] = match_skills([skill("general", [])], TREE)
    assert m.active and "no paths" in m.reason


def test_glob_matches_and_reason_names_the_file() -> None:
    [m] = match_skills([skill("py", ["src/**/*.py"])], TREE)
    assert m.active
    assert "src/**/*.py" in m.reason and "src/app/main.py" in m.reason


def test_double_star_also_matches_zero_directories() -> None:
    [m] = match_skills([skill("py", ["src/**/util.py"])], TREE)
    assert m.active and "src/util.py" in m.reason


def test_leading_dot_slash_is_ignored() -> None:
    [m] = match_skills([skill("ts", ["./web/*.ts"])], TREE)
    assert m.active


def test_no_match_is_inactive_and_says_why() -> None:
    [m] = match_skills([skill("rust", ["**/*.rs", "Cargo.toml"])], TREE)
    assert not m.active
    assert "'**/*.rs'" in m.reason and "'Cargo.toml'" in m.reason


def bundle(raw: JSONObject) -> SkillBundle:
    return SkillBundle.from_json(raw)


def test_warnings_for_truncation_not_found_and_budget() -> None:
    b = bundle(
        {
            "skills": [],
            "token_estimate": 29000,
            "token_budget": 20000,
            "truncated": ["a", "b"],
            "not_found": ["ghost"],
        }
    )
    warnings = bundle_warnings(b, requested_max=30000)
    joined = "\n".join(warnings)
    assert "TRUNCATED: 2" in joined and "a, b" in joined
    assert "NOT FOUND: 1" in joined and "ghost" in joined
    assert "max_tokens=30000" in joined and "20000" in joined


def test_complete_bundle_has_no_warnings() -> None:
    assert bundle_warnings(bundle({"skills": [], "token_budget": 30000}), 30000) == []


def test_cache_round_trip(tmp_path: Path) -> None:
    raw: JSONObject = {"ok": True, "repo": "r/x", "skills": [{"name": "s"}], "truncated": ["t"]}
    path = save_bundle(tmp_path, "r/x", bundle(raw))
    assert path.name == "r_x.json"
    cached = load_bundle(tmp_path, "r/x")
    assert cached is not None
    assert [s.name for s in cached.bundle.skills] == ["s"]
    assert cached.bundle.truncated == ["t"]
    assert load_bundle(tmp_path, "other") is None


def test_corrupt_cache_is_an_error_not_an_empty_bundle(tmp_path: Path) -> None:
    path = save_bundle(tmp_path, "r", bundle({"skills": []}))
    path.write_text("{nope")
    with pytest.raises(ValueError, match="skills pull"):
        load_bundle(tmp_path, "r")


def test_prompt_contains_active_skills_and_use_skill_instruction() -> None:
    text = render_skill_prompt([skill("py", [], content="Always run ruff.")])
    assert "## Skill: py" in text and "Always run ruff." in text and "use_skill" in text
    assert render_skill_prompt([]) == ""


def test_cli_pull_filters_caches_and_reports_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pebble: FakePebble, config: Config
) -> None:
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "main.py").write_text("print()")
    monkeypatch.chdir(work)
    pebble.on(
        "POST",
        "/v1/api/edge/skills/pull",
        (
            200,
            {
                "ok": True,
                "repo": "demo",
                "skills": [
                    {"name": "py", "paths": ["src/*.py"], "content": "x"},
                    {"name": "rust", "paths": ["**/*.rs"], "content": "y"},
                ],
                "token_estimate": 100,
                "token_budget": 30000,
                "truncated": ["huge-skill"],
                "not_found": ["missing-skill"],
            },
        ),
    )
    monkeypatch.setattr(cli, "_edge", lambda _c: EdgeClient(BASE, TOKEN, transport=pebble.transport))
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(name=["py", "missing-skill"], max_tokens=None)
    assert cli.cmd_skills_pull(args, config, Console(out, err)) == 0

    assert pebble.last.body == {
        "repo": "demo",
        "names": ["py", "missing-skill"],
        "max_tokens": config.max_skill_tokens,
    }
    stdout, stderr = out.getvalue(), err.getvalue()
    assert "ACTIVE   py" in stdout and "inactive rust" in stdout
    assert "WARNING: TRUNCATED" in stderr and "huge-skill" in stderr
    assert "WARNING: NOT FOUND" in stderr and "missing-skill" in stderr

    # `skills list` re-reads the cache and repeats the warnings.
    out, err = io.StringIO(), io.StringIO()
    assert cli.cmd_skills_list(argparse.Namespace(), config, Console(out, err)) == 0
    assert "ACTIVE   py" in out.getvalue()
    assert "huge-skill" in err.getvalue()
