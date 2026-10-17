"""Consolidating conversations (2026-10-16, item F; spec 2026-10-16-f-consolidate-conversations-design.md).

Transcripts are chains of linked records, and a stitched file is not something `claude --resume` is
known to accept, so conversations are never merged. Instead each chosen one is summarised - decisions,
what was built, what is open, files and repositories named - by a one-off `claude --print` with no
tools, through the proxy, and the summaries become the brief that opens a new conversation. The
originals are not touched.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .convsearch import Turn, extract

LAST_TURNS = 200
MAX_WORDS = 12_000
TIMEOUT_S = 180
MIN_IDS, MAX_IDS = 2, 50             # 50: "All" ticks every one listed (the user, 2026-10-17)

SUMMARY_PROMPT = """Summarise this conversation between a person and an AI coding assistant so that a new
conversation can carry on from it. Use short Markdown sections: **Decisions**, **Built or changed**,
**Still open**, **Files and repositories named**. Facts only, no preamble.

{turns}"""

BRIEF_HEAD = ("This conversation continues {n} earlier ones, summarised below (oldest first). Read them, say "
              "in a few lines what you understand the current state to be, and wait for instructions.")


def turns_text(turns: list[Turn]) -> str:
    lines: list[str] = []
    words = 0
    for turn in reversed(turns[-LAST_TURNS:]):
        text = " ".join(turn.text.split())
        count = len(text.split())
        if words + count > MAX_WORDS:
            text = " ".join(text.split()[: max(0, MAX_WORDS - words)])
        if text:
            lines.append(f"{turn.role}: {text}")
        words += count
        if words >= MAX_WORDS:
            break
    return "\n\n".join(reversed(lines))


def summarise(claude: str, base_url: str, turns: list[Turn], run: Callable[..., Any] = subprocess.run) -> str:
    """One isolated model request (oneshot.ask), through this proxy. A conversation with no turns is
    said to be empty without asking (a live check on 2026-10-17 had the model summarise nothing)."""
    from .oneshot import ask
    if not turns:
        return "_(This conversation has no turns: nothing was said in it.)_"
    return ask(claude, base_url, SUMMARY_PROMPT.format(turns=turns_text(turns)), TIMEOUT_S, run=run).strip()

def fallback(turns: list[Turn]) -> str:
    """When a summary fails: what it opened with, what was asked last, and the last replies, marked."""
    yours = [t for t in turns if t.role == "you"]
    replies = [t for t in turns if t.role == "claude"][-3:]
    out = ["_(No summary could be written; the opening, the last request and the last replies instead.)_"]
    if yours:
        out.append(f"**Opened with:** {' '.join(yours[0].text.split())[:600]}")
        out.append(f"**Last asked:** {' '.join(yours[-1].text.split())[:600]}")
    for reply in replies:
        out.append(f"**Reply:** {' '.join(reply.text.split())[:800]}")
    return "\n\n".join(out)


def brief(sections: list[dict[str, Any]]) -> str:
    parts = [BRIEF_HEAD.format(n=len(sections))]
    for i, s in enumerate(sorted(sections, key=lambda s: s["updated"]), start=1):
        when = time.strftime("%d/%m/%Y", time.localtime(s["updated"]))
        parts.append(f"## {i}. {s['title']} ({when}, {s['turns']} turns, id {s['id'][:8]})\n\n{s['summary']}")
    return "\n\n".join(parts)


class Jobs:
    """Consolidations under way, in memory: progress for the page, and the brief when done."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, items: list[tuple[Path, str]], claude: str, base_url: str,
              run: Callable[..., Any] = subprocess.run) -> str:
        job = uuid.uuid4().hex[:12]
        self.jobs[job] = {"total": len(items), "done": 0, "status": "running", "brief": "", "failed": [],
                          "titles": [t for _p, t in items]}

        def work() -> None:
            sections = []
            for path, title in items:
                turns, _end = extract(path)
                try:
                    summary = summarise(claude, base_url, turns, run)
                except RuntimeError as exc:
                    summary = fallback(turns)
                    self.jobs[job]["failed"].append(f"{title}: {exc}")
                sections.append({"id": path.stem, "title": title, "updated": path.stat().st_mtime,
                                 "turns": len(turns), "summary": summary})
                with self.lock:
                    self.jobs[job]["done"] += 1
            with self.lock:
                self.jobs[job].update(status="done", brief=brief(sections))

        threading.Thread(target=work, name="aicleaner-consolidate", daemon=True).start()
        return job

    def get(self, job: str) -> dict[str, Any] | None:
        return self.jobs.get(job)
