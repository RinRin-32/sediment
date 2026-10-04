"""Explicit human approval for every tool call that mutates or executes.

The prompt shows the tool name and every argument in full (the whole command,
the whole file content), never a summary, because a summary is where surprises
hide. The default answer is No. End-of-input or a non-interactive stdin is No.
"always" applies to one tool name for the rest of this session only, and can
be disabled in config (tools.allow_always = false).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from sediment.jsonish import JSON, JSONObject


@dataclass(frozen=True)
class Decision:
    approved: bool
    arguments: JSONObject
    note: str = ""


def render_call(tool: str, args: JSONObject) -> str:
    lines = [f"sediment wants to call `{tool}` with:"]
    for key, value in args.items():
        if isinstance(value, str) and ("\n" in value or len(value) > 60):
            lines.append(f"  {key}: <<<")
            lines.append(value)
            lines.append("  >>>")
        else:
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines)


class Approver:
    def __init__(
        self,
        ask: Callable[[str], str],
        is_interactive: Callable[[], bool],
        show: Callable[[str], None],
        allow_always: bool = True,
    ):
        self._ask = ask
        self._is_interactive = is_interactive
        self._show = show
        self._allow_always = allow_always
        self._always: set[str] = set()

    def review(self, tool: str, args: JSONObject) -> Decision:
        if tool in self._always:
            self._show(render_call(tool, args))
            self._show(f"(approved: you chose 'always' for `{tool}` this session)")
            return Decision(True, args)
        if not self._is_interactive():
            self._show(render_call(tool, args))
            self._show("(denied: stdin is not an interactive terminal, so nobody can approve)")
            return Decision(False, args, "no interactive terminal to ask for approval")

        current = args
        edited = False
        while True:
            self._show(render_call(tool, current))
            options = "[y]es / [N]o / [e]dit" + (" / [a]lways" if self._allow_always else "")
            answer = self._prompt(f"approve? {options} > ")
            if answer is None:
                return Decision(False, current, "no answer (end of input)")
            choice = answer.strip().lower()
            if choice in {"y", "yes"}:
                return Decision(True, current, "edited by the human" if edited else "")
            if choice in {"a", "always"} and self._allow_always:
                self._always.add(tool)
                return Decision(True, current, "edited by the human" if edited else "")
            if choice in {"e", "edit"}:
                replacement = self._edit(tool, current)
                if replacement is not None:
                    current, edited = replacement, True
                continue
            if choice in {"", "n", "no"}:
                return Decision(False, current)
            self._show(f"(unrecognised answer {answer!r}; treating as No)")
            return Decision(False, current)

    def _prompt(self, text: str) -> str | None:
        try:
            return self._ask(text)
        except (EOFError, KeyboardInterrupt):
            return None

    def _edit(self, tool: str, args: JSONObject) -> JSONObject | None:
        if tool == "run_command":
            new = self._prompt("new command (empty keeps the current one): ")
            if not new:
                return None
            return {**args, "command": new}
        raw = self._prompt("new arguments as one line of JSON (empty keeps current): ")
        if not raw:
            return None
        try:
            parsed: JSON = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._show(f"(not valid JSON: {exc}; keeping the current arguments)")
            return None
        if not isinstance(parsed, dict):
            self._show("(arguments must be a JSON object; keeping the current arguments)")
            return None
        return parsed
