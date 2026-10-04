"""Local tools: workspace confinement, truncation that says so, real exit codes."""

from __future__ import annotations

from pathlib import Path

from sediment.config import ToolPolicy
from sediment.tools import MUTATING, TOOL_SPECS, Workspace


def ws(tmp_path: Path, **policy: int) -> Workspace:
    return Workspace(tmp_path, ToolPolicy(**policy))


def test_specs_and_mutating_set() -> None:
    names = {spec["function"]["name"] for spec in TOOL_SPECS}  # type: ignore[index]
    assert names == {"read_file", "write_file", "list_dir", "search", "run_command"}
    assert {"write_file", "run_command"} == MUTATING


def test_paths_outside_workspace_are_refused(tmp_path: Path) -> None:
    w = ws(tmp_path)
    assert w.execute("read_file", {"path": "../etc/passwd"}).startswith("error:")
    assert "outside the workspace" in w.execute("write_file", {"path": "/tmp/x", "content": "y"})


def test_read_with_offset_and_limit(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\nd\n")
    assert ws(tmp_path).execute("read_file", {"path": "f.txt", "offset": 2, "limit": 2}) == "b\nc\n"


def test_output_truncation_is_announced(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 300)
    out = ws(tmp_path, max_output_chars=100).execute("read_file", {"path": "big.txt"})
    assert out.startswith("x" * 100) and "truncated: 200 more characters" in out


def test_list_dir_and_search(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def hello():\n    pass\n")
    (tmp_path / "notes.md").write_text("hello world\n")
    w = ws(tmp_path)
    assert w.execute("list_dir", {}) == "notes.md\npkg/"
    hits = w.execute("search", {"pattern": "hello", "glob": "*.py"})
    assert hits == "pkg/m.py:1: def hello():"
    assert w.execute("search", {"pattern": "("}).startswith("error: bad regex")


def test_run_command_reports_real_exit_code(tmp_path: Path) -> None:
    out = ws(tmp_path).execute("run_command", {"command": "echo hi; exit 3"})
    assert out == "exit_code: 3\nhi\n"


def test_run_command_timeout(tmp_path: Path) -> None:
    out = ws(tmp_path, command_timeout=1).execute("run_command", {"command": "sleep 5"})
    assert "timed out after 1s" in out


def test_unknown_tool(tmp_path: Path) -> None:
    assert ws(tmp_path).execute("rm_rf", {}) == "error: unknown tool 'rm_rf'"
