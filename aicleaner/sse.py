"""Server-sent-events plumbing and the tail-buffered rewriter.

A token can arrive split across two stream deltas ("CLIEN" then "T04 Pty").
TailRewriter holds back the last longest-match-length characters until more
text or a flush arrives, so every substitution sees the whole token and the
character after it (whole-word matching needs to know what follows). The last
character already emitted is kept as look-behind context for the next part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

StrFn = Callable[[str], str]


class TailRewriter:
    def __init__(self, pattern: re.Pattern[str], rewrite: StrFn, max_len: int):
        self._pattern = pattern
        self._rewrite = rewrite
        self._keep = max(0, max_len)
        self._buf = ""
        self._prev = ""  # last source character emitted: look-behind context for whole words

    def feed(self, text: str) -> str:
        """Return the rewritten text that is safe to emit now."""
        self._buf += text
        split = max(0, len(self._buf) - self._keep)
        if split == 0:
            return ""
        # Any match starting before `split` is fully contained in the buffer with at least one
        # character after it (its length is at most keep), but it may end after `split`. Move the
        # cut past the last such match so we never emit half of one.
        ctx = self._prev
        full = ctx + self._buf
        cut = split
        for m in self._pattern.finditer(full, len(ctx)):
            start, end = m.start() - len(ctx), m.end() - len(ctx)
            if start >= split:
                break
            cut = max(cut, end)
        segment = self._buf[:cut]
        out = self._rewrite(ctx + segment)[len(ctx):]
        if segment:
            self._prev = segment[-1]
        self._buf = self._buf[cut:]
        return out

    def flush(self) -> str:
        ctx = self._prev
        out = self._rewrite(ctx + self._buf)[len(ctx):]
        if self._buf:
            self._prev = self._buf[-1]
        self._buf = ""
        return out


@dataclass
class SSEEvent:
    """One SSE event as a list of raw field lines (without the blank line)."""

    lines: list[str] = field(default_factory=list)

    @property
    def data(self) -> str | None:
        parts = [ln[5:].lstrip(" ") for ln in self.lines if ln.startswith("data:")]
        return "\n".join(parts) if parts else None

    @property
    def event(self) -> str | None:
        for ln in self.lines:
            if ln.startswith("event:"):
                return ln[6:].strip()
        return None

    def with_data(self, data: str) -> "SSEEvent":
        kept = [ln for ln in self.lines if not ln.startswith("data:")]
        return SSEEvent(kept + [f"data: {data}"])

    def serialise(self) -> bytes:
        return ("\n".join(self.lines) + "\n\n").encode("utf-8")


def make_event(name: str | None, data: str) -> SSEEvent:
    lines = [f"event: {name}"] if name else []
    lines.append(f"data: {data}")
    return SSEEvent(lines)


class SSEParser:
    """Feed raw bytes, get complete events. Handles chunk boundaries anywhere."""

    def __init__(self) -> None:
        self._pending = b""
        self._current: list[str] = []

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        self._pending += chunk
        events: list[SSEEvent] = []
        while True:
            nl = self._pending.find(b"\n")
            if nl < 0:
                break
            raw = self._pending[:nl]
            self._pending = self._pending[nl + 1 :]
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line == "":
                if self._current:
                    events.append(SSEEvent(self._current))
                    self._current = []
                continue
            self._current.append(line)
        return events

    def finish(self) -> list[SSEEvent]:
        """Return any trailing event that had no terminating blank line."""
        if self._pending:
            line = self._pending.decode("utf-8", errors="replace").rstrip("\r")
            if line:
                self._current.append(line)
            self._pending = b""
        if self._current:
            ev = SSEEvent(self._current)
            self._current = []
            return [ev]
        return []
