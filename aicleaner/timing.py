"""Diagnostics: timing the pages' requests (the user, 2026-10-07; feature 91).

Switching sessions is sometimes slow, mostly when the user is remote. To see exactly where the time
goes before changing anything, the Settings page's Diagnostics card turns on "Record timings":
- every API response carries a `Server-Timing` header - how long aicleaner spent, and on what;
- the page measures each step it takes (a session switch, a poll, drawing the transcript, decrypting
  a sealed reply) and the browser's own resource timing (network time, bytes), and sends a summary
  here, written to `logs/timings-<date>.jsonl` on this machine.

Off by default, nothing extra happens. On, it switches itself off after TIMINGS_FOR_S, so it is
never left running by accident. Step names, durations, sizes and request paths only - never
content. Paths can carry session names, which are real names: the log stays on this machine, as
every local log here does.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

TIMINGS_FOR_S = 24 * 3600
MAX_ENTRIES = 200            # per report from the page
MAX_ENTRY_BYTES = 4096
REQUEST_PARTS = web.RequestKey("timing_parts", list)   # a handler adds its own parts here


def active(cfg: Any, now: float | None = None) -> bool:
    if not getattr(cfg, "debug_timings", False):
        return False
    since = int(getattr(cfg, "debug_timings_since", 0) or 0)
    return (now or time.time()) - since < TIMINGS_FOR_S


def until(cfg: Any) -> int:
    """When recording stops by itself (epoch seconds), 0 when it is off."""
    return int(getattr(cfg, "debug_timings_since", 0) or 0) + TIMINGS_FOR_S if active(cfg) else 0


def part(request: Any, name: str, started: float) -> None:
    """Note one part of a handler's work, from `started` (time.perf_counter) to now."""
    parts = request.get(REQUEST_PARTS) if hasattr(request, "get") else None
    if parts is not None:
        parts.append((name, (time.perf_counter() - started) * 1000))


def header(total_ms: float, parts: list[tuple[str, float]]) -> str:
    """A `Server-Timing` value: the whole handler as `app`, then each part it named."""
    items = [f"app;dur={total_ms:.1f}"] + [f"{name};dur={ms:.1f}" for name, ms in parts]
    return ", ".join(items)


class TimingLog:
    """logs/timings-YYYYMMDD.jsonl: one line per entry the page reports."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    def path(self) -> Path:
        return self.log_dir / f"timings-{datetime.now():%Y%m%d}.jsonl"

    def write(self, entries: list[Any], who: str = "") -> int:
        kept = []
        stamp = datetime.now().isoformat(timespec="milliseconds")
        for entry in (entries or [])[:MAX_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            line = json.dumps({"logged": stamp, "who": who, **entry}, ensure_ascii=False)
            if len(line.encode("utf-8")) <= MAX_ENTRY_BYTES:
                kept.append(line)
        if not kept:
            return 0
        with self._lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self.path().open("a", encoding="utf-8") as out:
                out.write("\n".join(kept) + "\n")
        return len(kept)
