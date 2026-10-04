"""The approval prompt: default No, full arguments shown, edit, always."""

from __future__ import annotations

from sediment.approval import Approver, Decision


def approver(answers: list[str], allow_always: bool = True) -> tuple[Approver, list[str]]:
    shown: list[str] = []
    replies = iter(answers)

    def ask(_text: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    return Approver(ask, lambda: True, shown.append, allow_always), shown


def review(answers: list[str], tool: str = "run_command", **args: str) -> tuple[Decision, list[str]]:
    a, shown = approver(answers)
    return a.review(tool, dict(args) or {"command": "make test"}), shown


def test_enter_means_no() -> None:
    decision, _ = review([""])
    assert not decision.approved


def test_yes() -> None:
    decision, shown = review(["Y"])
    assert decision.approved
    assert '"make test"' in shown[0]


def test_unknown_answer_is_no() -> None:
    decision, shown = review(["sure"])
    assert not decision.approved and "treating as No" in shown[-1]


def test_full_multiline_content_is_shown() -> None:
    content = "line one\nline two\n" + "x" * 500
    _, shown = review(["n"], tool="write_file", path="a.txt", content=content)
    assert content in shown[0]


def test_edit_command_then_approve() -> None:
    decision, shown = review(["e", "make lint", "y"])
    assert decision.approved
    assert decision.arguments == {"command": "make lint"}
    assert decision.note == "edited by the human"
    assert '"make lint"' in shown[-1]  # the edited call is shown again before approving


def test_edit_json_for_other_tools_and_bad_json_keeps_current() -> None:
    decision, shown = review(
        ["e", "{bad", "e", '{"path": "b.txt", "content": "z"}', "y"],
        tool="write_file",
        path="a.txt",
        content="x",
    )
    assert any("not valid JSON" in s for s in shown)
    assert decision.approved and decision.arguments == {"path": "b.txt", "content": "z"}


def test_always_is_per_tool_and_per_session() -> None:
    a, shown = approver(["a"])
    assert a.review("run_command", {"command": "ls"}).approved
    assert a.review("run_command", {"command": "pwd"}).approved  # no prompt left: always
    assert any("chose 'always'" in s for s in shown)
    assert not a.review("write_file", {"path": "x", "content": ""}).approved  # EOF -> no


def test_always_can_be_disabled() -> None:
    a, _ = approver(["a"], allow_always=False)
    assert not a.review("run_command", {"command": "ls"}).approved
