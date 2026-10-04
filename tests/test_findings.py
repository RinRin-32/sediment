"""Session findings: transcript round-trip, summary via the model, note written to the KB."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from conftest import BASE, TOKEN, FakeModel, FakePebble, chat_response
from sediment import cli
from sediment.config import Config, ModelSettings
from sediment.edge import EdgeClient
from sediment.findings import Transcript, load_transcript, render_for_summary, save_transcript
from sediment.model import ChatModel
from sediment.ui import Console

MESSAGES = [
    {"role": "system", "content": "SYSTEM PROMPT WITH SKILLS"},
    {"role": "user", "content": "why is the build slow?"},
    {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}
    ]},
    {"role": "tool", "tool_call_id": "c1", "content": "DENIED: ..."},
    {"role": "assistant", "content": "Probably the lockfile."},
]


def test_transcript_round_trip_and_latest(tmp_path: Path) -> None:
    save_transcript(tmp_path, Transcript("aaa", "demo", 1.0, MESSAGES))
    loaded = load_transcript(tmp_path, "aaa")
    assert loaded.repo == "demo" and loaded.messages == MESSAGES
    assert load_transcript(tmp_path, None).session_id == "aaa"
    with pytest.raises(ValueError, match="no saved sessions"):
        load_transcript(tmp_path / "empty", None)


def test_summary_input_excludes_system_prompt() -> None:
    text = render_for_summary(MESSAGES)
    assert "SYSTEM PROMPT" not in text
    assert "why is the build slow?" in text and "run_command" in text and "DENIED" in text


def test_save_findings_writes_a_note_attributed_to_the_repo(
    monkeypatch: pytest.MonkeyPatch, pebble: FakePebble, config: Config
) -> None:
    fake = FakeModel([chat_response("- Build is slow because of the lockfile (not verified).")])

    def model(settings: ModelSettings, notice: object) -> ChatModel:
        return ChatModel(settings, transport=fake.transport, stream=False)

    monkeypatch.setattr(cli, "ChatModel", model)
    monkeypatch.setattr(cli, "_edge", lambda _c: EdgeClient(BASE, TOKEN, transport=pebble.transport))
    pebble.on("POST", "/v1/api/edge/kb/write", (200, {"ok": True, "title": "t", "path": "kb/t.md"}))

    out = io.StringIO()
    transcript = Transcript("abcdef0123", "demo", 0.0, MESSAGES)
    code = cli.save_findings(config, Console(out, io.StringIO()), transcript, None, assume_yes=True)

    assert code == 0 and "kb/t.md" in out.getvalue()
    body = pebble.last.body
    assert isinstance(body, dict)
    assert body["kind"] == "note" and body["repo"] == "demo"
    assert body["body"] == "- Build is slow because of the lockfile (not verified)."
    assert "abcdef01" in str(body["title"])
    # the summary request carried the transcript but no tools
    assert "tools" not in fake.requests[0]
