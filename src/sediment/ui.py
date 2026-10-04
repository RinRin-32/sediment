"""Terminal output: model text on stdout, everything sediment itself says on stderr.

Keeping the two apart means `sediment chat ... > answer.md` captures only the
model's words, while approvals, warnings and tool activity stay visible.
Plain text only: calm, greppable, no hidden state behind colours.
"""

from __future__ import annotations

import sys
from typing import TextIO


class Console:
    def __init__(self, out: TextIO | None = None, err: TextIO | None = None):
        self._out = out or sys.stdout
        self._err = err or sys.stderr
        self._mid_line = False

    def stream(self, text: str) -> None:
        """Model text as it arrives."""
        self._out.write(text)
        self._out.flush()
        self._mid_line = not text.endswith("\n")

    def end_stream(self) -> None:
        if self._mid_line:
            self._out.write("\n")
            self._out.flush()
            self._mid_line = False

    def say(self, text: str) -> None:
        """A sediment message (status, results of local tools, approvals)."""
        self.end_stream()
        self._err.write(text + "\n")
        self._err.flush()

    def warn(self, text: str) -> None:
        self.say(f"WARNING: {text}")

    def result(self, text: str) -> None:
        """Command output meant to be captured (doctor tables, kb notes)."""
        self.end_stream()
        self._out.write(text + "\n")
        self._out.flush()
