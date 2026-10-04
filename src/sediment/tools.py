"""The local tools the model may call, confined to one workspace directory.

Read-only tools (read_file, list_dir, search) run without asking. Tools that
mutate or execute (write_file, run_command) are listed in MUTATING and the
agent loop will not call them without an explicit human approval; this module
just does the work once that decision is made.

Tool failures are returned to the model as plain text ("error: ..."), so it can
react, rather than raised, which would end the session over a typo'd path.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

from sediment.config import ToolPolicy
from sediment.jsonish import JSONObject, get_int, get_str

MUTATING = frozenset({"write_file", "run_command"})

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
_MAX_SEARCH_HITS = 200
_MAX_SEARCH_FILE_BYTES = 2_000_000


def _fn(name: str, description: str, properties: JSONObject, required: list[str]) -> JSONObject:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


TOOL_SPECS: list[JSONObject] = [
    _fn(
        "read_file",
        "Read a UTF-8 text file in the workspace. Optional 1-based line offset and line limit.",
        {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        ["path"],
    ),
    _fn(
        "write_file",
        "Create or overwrite a file in the workspace. Requires human approval.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _fn(
        "list_dir",
        "List entries of a workspace directory (directories end with '/').",
        {"path": {"type": "string"}},
        [],
    ),
    _fn(
        "search",
        "Regex search over workspace text files. Optional sub-path and filename glob.",
        {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
        },
        ["pattern"],
    ),
    _fn(
        "run_command",
        "Run a shell command in the workspace root. Requires human approval.",
        {"command": {"type": "string"}},
        ["command"],
    ),
]


class ToolError(Exception):
    """A tool could not do what was asked; the message goes back to the model."""


class Workspace:
    def __init__(self, root: Path, policy: ToolPolicy):
        self.root = root.resolve()
        self.policy = policy

    def execute(self, name: str, args: JSONObject) -> str:
        handlers = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "list_dir": self._list_dir,
            "search": self._search,
            "run_command": self._run_command,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"error: unknown tool {name!r}"
        try:
            return handler(args)
        except ToolError as exc:
            return f"error: {exc}"
        except OSError as exc:
            return f"error: {type(exc).__name__}: {exc}"

    def _resolve(self, raw: str) -> Path:
        path = (self.root / (raw or ".")).resolve()
        if not path.is_relative_to(self.root):
            raise ToolError(f"{raw!r} is outside the workspace {self.root}")
        return path

    def _clip(self, text: str) -> str:
        limit = self.policy.max_output_chars
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n[... truncated: {len(text) - limit} more characters not shown]"

    def _read_file(self, args: JSONObject) -> str:
        path = self._resolve(get_str(args, "path"))
        if not path.is_file():
            raise ToolError(f"{get_str(args, 'path')!r} is not a file")
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ToolError(f"{path.name} is not UTF-8 text") from exc
        offset = max(get_int(args, "offset", 1), 1)
        limit = get_int(args, "limit", 0)
        chosen = lines[offset - 1 : offset - 1 + limit] if limit > 0 else lines[offset - 1 :]
        return self._clip("".join(chosen))

    def _write_file(self, args: JSONObject) -> str:
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("write_file needs string 'content'")
        path = self._resolve(get_str(args, "path"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content.encode('utf-8'))} bytes to {path.relative_to(self.root)}"

    def _list_dir(self, args: JSONObject) -> str:
        path = self._resolve(get_str(args, "path", "."))
        if not path.is_dir():
            raise ToolError(f"{get_str(args, 'path')!r} is not a directory")
        entries = sorted(path.iterdir(), key=lambda p: p.name)
        return "\n".join(p.name + ("/" if p.is_dir() else "") for p in entries) or "(empty)"

    def _search(self, args: JSONObject) -> str:
        try:
            regex = re.compile(get_str(args, "pattern"))
        except re.error as exc:
            raise ToolError(f"bad regex: {exc}") from exc
        base = self._resolve(get_str(args, "path", "."))
        glob = get_str(args, "glob")
        hits: list[str] = []
        for file in self._walk(base):
            if glob and not fnmatchcase(file.name, glob):
                continue
            if file.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(self.root)}:{number}: {line.strip()}")
                    if len(hits) >= _MAX_SEARCH_HITS:
                        hits.append(f"[stopped at {_MAX_SEARCH_HITS} matches; narrow the search]")
                        return self._clip("\n".join(hits))
        return self._clip("\n".join(hits)) if hits else "no matches"

    def _walk(self, base: Path) -> list[Path]:
        if base.is_file():
            return [base]
        found = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            found.extend(Path(dirpath) / name for name in sorted(filenames))
        return found

    def _run_command(self, args: JSONObject) -> str:
        command = get_str(args, "command")
        if not command.strip():
            raise ToolError("run_command needs a non-empty 'command'")
        timeout = self.policy.command_timeout
        # Own process group, so a timeout kills the shell *and* whatever it spawned;
        # otherwise a lingering child keeps the pipe open and we hang anyway.
        with subprocess.Popen(
            command,
            shell=True,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        ) as proc:
            try:
                output, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                output, _ = proc.communicate()
                return self._clip(f"command timed out after {timeout}s (killed)\n{output}")
            except KeyboardInterrupt:
                os.killpg(proc.pid, signal.SIGKILL)
                raise
        return self._clip(f"exit_code: {proc.returncode}\n{output}")
