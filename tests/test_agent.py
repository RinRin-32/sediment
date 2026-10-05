"""End-to-end agent loop against a fake OpenAI-compatible endpoint: deny, approve, caps."""

from __future__ import annotations

import io
import json
from pathlib import Path

from conftest import FakeModel, chat_response, tool_call
from sediment.agent import Agent
from sediment.approval import Approver
from sediment.config import ModelSettings, ToolPolicy
from sediment.envelope import PebbleError
from sediment.model import ChatModel
from sediment.tools import Workspace
from sediment.ui import Console


def make_agent(
    tmp_path: Path,
    fake: FakeModel,
    answers: list[str],
    interactive: bool = True,
    max_turns: int = 6,
    **kwargs: object,
) -> tuple[Agent, io.StringIO, list[str]]:
    replies = iter(answers)
    prompts: list[str] = []

    def ask(text: str) -> str:
        prompts.append(text)
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    err = io.StringIO()
    console = Console(io.StringIO(), err)
    model = ChatModel(
        ModelSettings("https://model.test/v1", "fake"), transport=fake.transport, stream=False
    )
    agent = Agent(
        model=model,
        workspace=Workspace(tmp_path, ToolPolicy(command_timeout=10)),
        approver=Approver(ask, lambda: interactive, console.say),
        console=console,
        max_turns=max_turns,
        **kwargs,  # type: ignore[arg-type]
    )
    return agent, err, prompts


def tool_messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request["messages"]
    assert isinstance(messages, list)
    return [m for m in messages if m["role"] == "tool"]


def test_deny_then_approve_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "victim.txt").write_text("keep me")
    fake = FakeModel(
        [
            chat_response("Cleaning up.", [tool_call("c1", "run_command", {"command": "rm -f victim.txt"})]),
            chat_response("", [tool_call("c2", "write_file", {"path": "notes/out.md", "content": "hello\n"})]),
            chat_response("Done: wrote notes/out.md, did not delete anything."),
        ]
    )
    agent, err, prompts = make_agent(tmp_path, fake, answers=["n", "y"])
    result = agent.ask("tidy up")

    assert result.completed and result.turns == 3
    assert "did not delete" in result.final_text
    # denied command did not run; approved write happened
    assert (tmp_path / "victim.txt").read_text() == "keep me"
    assert (tmp_path / "notes/out.md").read_text() == "hello\n"
    # the approval prompt showed the full command text, verbatim
    log = err.getvalue()
    assert '`run_command`' in log and '"rm -f victim.txt"' in log
    assert "hello" in log  # full file content shown before approving the write
    assert len(prompts) == 2
    # the model was told about the denial as a tool result
    second = tool_messages(fake.requests[1])
    assert second[0]["tool_call_id"] == "c1"
    assert "DENIED" in str(second[0]["content"]) and "NOT executed" in str(second[0]["content"])
    third = tool_messages(fake.requests[2])
    assert "wrote 6 bytes" in str(third[1]["content"])


def test_non_interactive_stdin_denies_without_asking(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            chat_response("", [tool_call("c1", "run_command", {"command": "touch made"})]),
            chat_response("ok, I could not run it"),
        ]
    )
    agent, err, prompts = make_agent(tmp_path, fake, answers=["y"], interactive=False)
    agent.ask("go")
    assert prompts == []
    assert not (tmp_path / "made").exists()
    assert "not an interactive terminal" in err.getvalue()
    assert "no interactive terminal" in str(tool_messages(fake.requests[1])[0]["content"])


def test_eof_at_prompt_is_a_deny(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            chat_response("", [tool_call("c1", "run_command", {"command": "touch made"})]),
            chat_response("understood"),
        ]
    )
    agent, _err, _prompts = make_agent(tmp_path, fake, answers=[])
    agent.ask("go")
    assert not (tmp_path / "made").exists()
    assert "end of input" in str(tool_messages(fake.requests[1])[0]["content"])


def test_read_only_tools_run_without_approval(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    fake = FakeModel(
        [
            chat_response(
                "",
                [
                    tool_call("c1", "read_file", {"path": "a.py"}),
                    tool_call("c2", "search", {"pattern": "x ="}),
                ],
            ),
            chat_response("x is 1"),
        ]
    )
    agent, _err, prompts = make_agent(tmp_path, fake, answers=[])
    agent.ask("what is x")
    assert prompts == []
    results = [str(m["content"]) for m in tool_messages(fake.requests[1])]
    assert results[0] == "x = 1\n"
    assert "a.py:1: x = 1" in results[1]


def test_turn_cap_stops_honestly(tmp_path: Path) -> None:
    fake = FakeModel([chat_response("", [tool_call(f"c{i}", "list_dir", {})]) for i in range(5)])
    agent, err, _ = make_agent(tmp_path, fake, answers=[], max_turns=2)
    result = agent.ask("loop forever")
    assert not result.completed and result.turns == 2
    assert len(fake.requests) == 2
    assert "stopped after 2 model turns" in err.getvalue()


def test_bad_tool_arguments_are_reported_to_the_model(tmp_path: Path) -> None:
    bad = {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": "{oops"}}
    fake = FakeModel([chat_response("", [bad]), chat_response("sorry")])
    agent, _err, prompts = make_agent(tmp_path, fake, answers=["y"])
    agent.ask("go")
    assert prompts == []  # never reached the approval prompt
    assert "not valid JSON" in str(tool_messages(fake.requests[1])[0]["content"])


class FailingReporter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def report(self, name: str) -> None:
        self.calls.append(name)
        raise PebbleError(503, "POST", "/v1/api/skills/report", "storage down")


def test_skill_report_failure_is_visible_but_session_continues(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            chat_response("", [tool_call("c1", "use_skill", {"name": "py-style"})]),
            chat_response("applied py-style"),
        ]
    )
    reporter = FailingReporter()
    agent, err, _ = make_agent(
        tmp_path,
        fake,
        answers=[],
        skill_prompt="## Skill: py-style",
        skill_names=frozenset({"py-style"}),
        reporter=reporter,
    )
    result = agent.ask("style it")
    assert result.completed and result.final_text == "applied py-style"
    assert reporter.calls == ["py-style"]
    assert "could not report skill use" in err.getvalue() and "storage down" in err.getvalue()
    assert "reporting to pebble failed" in str(tool_messages(fake.requests[1])[0]["content"])
    tools = [t["function"]["name"] for t in fake.requests[0]["tools"]]  # type: ignore[index]
    assert "use_skill" in tools
    assert "## Skill: py-style" in json.dumps(fake.requests[0]["messages"][0])


def test_use_skill_is_not_offered_and_refused_cleanly_without_skills(tmp_path: Path) -> None:
    fake = FakeModel(
        [
            chat_response("", [tool_call("c1", "use_skill", {"name": "py-style"})]),
            chat_response("ok, no skills"),
        ]
    )
    reporter = FailingReporter()
    agent, err, prompts = make_agent(tmp_path, fake, answers=[], reporter=reporter)
    result = agent.ask("style it")
    assert result.completed and result.final_text == "ok, no skills"
    tools = [t["function"]["name"] for t in fake.requests[0]["tools"]]  # type: ignore[index]
    assert "use_skill" not in tools
    content = str(tool_messages(fake.requests[1])[0]["content"])
    assert content == "error: use_skill is unavailable: no skills are active in this session"
    assert reporter.calls == [] and prompts == []
    assert "no skills are active" in err.getvalue()
