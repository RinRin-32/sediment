"""Minimal Server-Sent Events parser, shared by model streaming and research --follow.

Only what both consumers need: `event:` names, multi-line `data:`, blank-line
frame boundaries, comments ignored. No reconnection logic: callers report a
dropped stream honestly instead of retrying behind the user's back.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEFrame:
    event: str
    data: str


def iter_sse(lines: Iterable[str]) -> Iterator[SSEFrame]:
    event = ""
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            if data:
                yield SSEFrame(event or "message", "\n".join(data))
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield SSEFrame(event or "message", "\n".join(data))
