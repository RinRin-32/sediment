"""What the local checkout looks like: repo name, file list, current commit.

pebble never sees the working tree, so anything that depends on it (skill path
globs, experiment commits) has to be computed here. Every helper degrades to a
plain answer ("" / directory walk) when git is absent instead of failing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}
MAX_TREE_FILES = 50000


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def toplevel(cwd: Path) -> Path | None:
    out = _git(cwd, "rev-parse", "--show-toplevel")
    return Path(out) if out else None


def repo_name_from_remote(url: str) -> str:
    """`git@host:org/name.git`, `https://host/org/name` -> `name`."""
    tail = url.rstrip("/").replace(":", "/").rsplit("/", 1)[-1]
    return tail.removesuffix(".git")


def infer_repo(cwd: Path, configured: str = "") -> str:
    """Config wins; else origin's repo name; else the checkout's directory name; else ""."""
    if configured:
        return configured
    remote = _git(cwd, "remote", "get-url", "origin")
    if remote:
        name = repo_name_from_remote(remote)
        if name:
            return name
    top = toplevel(cwd)
    return top.name if top else ""


def head_commit(cwd: Path) -> str | None:
    return _git(cwd, "rev-parse", "HEAD")


def list_tree(root: Path) -> list[str]:
    """Relative POSIX paths of files in the working tree (tracked + untracked, not ignored)."""
    listed = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if listed is not None:
        return listed.splitlines()[:MAX_TREE_FILES]
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            files.append((rel_dir / name).as_posix())
            if len(files) >= MAX_TREE_FILES:
                return files
    return files
