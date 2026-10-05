"""Session transcripts and writing what was learned back to the KB.

Transcripts are saved locally (cache_dir/sessions/<id>.json) after every
exchange so `sediment kb save` can summarize a session after the fact. The
summary is written by the model, so a human reviews the exact note before it
is sent to pebble, through the same approval prompt as any other mutation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from sediment.agent import Model
from sediment.jsonish import JSON, JSONObject, get_obj_list, get_str

_TOOL_RESULT_CHARS = 2000

SUMMARY_PROMPT = (
    "Summarize what was learned in this coding session as a short markdown note for a "
    "team knowledge base. Include only facts supported by the transcript: what was "
    "found, what was changed, what was run and its real outcome. Explicitly list "
    "anything that was attempted but not verified, and any tool calls the human denied. "
    "No preamble."
)


@dataclass(frozen=True)
class Transcript:
    session_id: str
    repo: str
    started_at: float
    messages: list[JSONObject]


def sessions_dir(cache_dir: Path) -> Path:
    return cache_dir / "sessions"


def save_transcript(cache_dir: Path, transcript: Transcript) -> Path:
    directory = sessions_dir(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{transcript.session_id}.json"
    record: dict[str, JSON] = {
        "session_id": transcript.session_id,
        "repo": transcript.repo,
        "started_at": transcript.started_at,
        "messages": list(transcript.messages),
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def load_transcript(cache_dir: Path, session_id: str | None) -> Transcript:
    """Latest session when `session_id` is None. Raises ValueError with a clear reason."""
    directory = sessions_dir(cache_dir)
    if session_id:
        path = directory / f"{session_id}.json"
    else:
        candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise ValueError(f"no saved sessions in {directory}; run `sediment chat` first")
        path = candidates[-1]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read session {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"session file {path} is malformed")
    started = record.get("started_at")
    return Transcript(
        session_id=get_str(record, "session_id", path.stem),
        repo=get_str(record, "repo"),
        started_at=float(started) if isinstance(started, int | float) else 0.0,
        messages=get_obj_list(record, "messages"),
    )


def render_for_summary(messages: list[JSONObject]) -> str:
    """Flatten a transcript to text; the system prompt (skills) is left out."""
    parts = []
    for message in messages:
        role = get_str(message, "role")
        if role == "system":
            continue
        content = get_str(message, "content")
        if role == "tool" and len(content) > _TOOL_RESULT_CHARS:
            content = content[:_TOOL_RESULT_CHARS] + " [...clipped]"
        calls = get_obj_list(message, "tool_calls")
        if calls:
            content += "\n" + "\n".join(f"(tool call) {json.dumps(c.get('function'))}" for c in calls)
        parts.append(f"[{role}] {content}".rstrip())
    return "\n\n".join(parts)


def summarize(model: Model, transcript: Transcript) -> str:
    """Not streamed: the full note is shown in the approval prompt before anything is sent."""
    messages: list[JSONObject] = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": render_for_summary(transcript.messages)},
    ]
    turn = model.complete(messages, [], lambda _t: None)
    return turn.content.strip()


def default_title(transcript: Transcript) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(transcript.started_at or time.time()))
    repo = f" {transcript.repo}" if transcript.repo else ""
    return f"sediment session {day}{repo} {transcript.session_id[:8]}: findings"
