"""The local coding-agent loop.

One user message -> up to `max_turns` model calls. Each model call may request
tools; read-only tools run, mutating ones go through the Approver, and every
outcome (including "the human denied this") goes back to the model as a tool
result, so the model never believes something ran when it did not. If the turn
cap is hit we stop and say so instead of pretending the task finished.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sediment.approval import Approver, render_call
from sediment.envelope import PebbleError, TransportError
from sediment.jsonish import JSON, JSONObject, get_str
from sediment.model import AssistantTurn, ToolCall
from sediment.reporting import ReportTokenError
from sediment.tools import MUTATING, TOOL_SPECS, Workspace
from sediment.ui import Console

BASE_PROMPT = (
    "You are sediment, a careful coding agent running on the user's machine. "
    "You can read, list and search files freely. Writing files and running commands "
    "require the human's approval; if a call is denied, do not retry it unchanged; "
    "explain what you wanted and ask. Never claim something was run or tested unless "
    "a tool result shows it."
)

USE_SKILL_SPEC: JSONObject = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": "Declare that you are applying one of the skills in the system prompt.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
}

_PREVIEW_CHARS = 400


class Model(Protocol):
    def complete(
        self,
        messages: list[JSONObject],
        tools: list[JSONObject],
        on_text: Callable[[str], None],
    ) -> AssistantTurn: ...


class Reporter(Protocol):
    def report(self, name: str) -> None: ...


@dataclass(frozen=True)
class AgentResult:
    final_text: str
    turns: int
    completed: bool


def denial_message(tool: str, note: str) -> str:
    reason = f" ({note})" if note else ""
    return (
        f"DENIED: the human did not approve this `{tool}` call{reason}. "
        "It was NOT executed and nothing changed. Do not retry it unchanged."
    )


class Agent:
    def __init__(
        self,
        model: Model,
        workspace: Workspace,
        approver: Approver,
        console: Console,
        max_turns: int,
        skill_prompt: str = "",
        skill_names: frozenset[str] = frozenset(),
        reporter: Reporter | None = None,
    ):
        self._model = model
        self._workspace = workspace
        self._approver = approver
        self._console = console
        self._max_turns = max_turns
        self._skill_names = skill_names
        self._reporter = reporter
        self._tools = [*TOOL_SPECS, USE_SKILL_SPEC] if skill_names else list(TOOL_SPECS)
        system = BASE_PROMPT + (f"\n\n{skill_prompt}" if skill_prompt else "")
        self.messages: list[JSONObject] = [{"role": "system", "content": system}]

    def ask(self, text: str) -> AgentResult:
        self.messages.append({"role": "user", "content": text})
        for turn_number in range(1, self._max_turns + 1):
            turn = self._model.complete(self.messages, self._tools, self._console.stream)
            self._console.end_stream()
            self.messages.append(turn.as_message())
            if not turn.tool_calls:
                return AgentResult(turn.content, turn_number, completed=True)
            for call in turn.tool_calls:
                content = self._handle(call)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        self._console.warn(
            f"stopped after {self._max_turns} model turns without a final answer "
            "(tools.max_turns). The task may be unfinished."
        )
        return AgentResult("", self._max_turns, completed=False)

    def _handle(self, call: ToolCall) -> str:
        try:
            parsed: JSON = json.loads(call.arguments)
        except json.JSONDecodeError as exc:
            self._console.say(f"model sent unparseable arguments for `{call.name}`: {exc}")
            return f"error: arguments were not valid JSON ({exc}); nothing was executed"
        if not isinstance(parsed, dict):
            return "error: arguments must be a JSON object; nothing was executed"

        if call.name == "use_skill":
            if not self._skill_names:
                # Not offered this session; a model may still hallucinate the call.
                self._console.say("model called `use_skill`, but no skills are active; refused")
                return "error: use_skill is unavailable: no skills are active in this session"
            return self._use_skill(get_str(parsed, "name"))

        if call.name in MUTATING:
            decision = self._approver.review(call.name, parsed)
            if not decision.approved:
                self._console.say(f"denied `{call.name}`; telling the model")
                return denial_message(call.name, decision.note)
            result = self._workspace.execute(call.name, decision.arguments)
            self._console.say(f"[{call.name} result]\n{result}")
            if decision.note:
                executed = json.dumps(decision.arguments, ensure_ascii=False)
                return f"NOTE: {decision.note}; executed with {executed}\n{result}"
            return result

        self._console.say(render_call(call.name, parsed))
        result = self._workspace.execute(call.name, parsed)
        preview = result if len(result) <= _PREVIEW_CHARS else result[:_PREVIEW_CHARS] + (
            f"\n[... {len(result) - _PREVIEW_CHARS} more characters sent to the model]"
        )
        self._console.say(f"[{call.name} result]\n{preview}")
        return result

    def _use_skill(self, name: str) -> str:
        if name not in self._skill_names:
            return f"error: no active skill named {name!r}"
        self._console.say(f"skill activated: {name}")
        if self._reporter is None:
            self._console.say("  (skill use not reported to pebble: reporting is off)")
            return f"skill {name!r} activated; use was not reported (reporting is off)"
        try:
            self._reporter.report(name)
        except (PebbleError, TransportError, ReportTokenError) as exc:
            detail = exc.explain() if isinstance(exc, PebbleError) else str(exc)
            self._console.warn(f"could not report skill use to pebble; continuing.\n{detail}")
            return f"skill {name!r} activated; reporting to pebble failed (session continues)"
        self._console.say("  (reported to pebble)")
        return f"skill {name!r} activated and reported"
