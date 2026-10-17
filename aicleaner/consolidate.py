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

# 2026-10-17: the prompt holds a high-level summary and names a file with the full ones.
OVERVIEW_PROMPT = """Below are summaries of {n} conversations between a person and an AI coding assistant, oldest
first. Write one high-level summary of them together, at most 200 words: what the work is, where it
stands, and what is still open. Plain prose or a few bullets, facts only, no preamble.

{summaries}"""
OVERVIEW_TIMEOUT_S = 120
SHORT_HEAD = ("This conversation continues {n} earlier conversations. Their full summaries, and the ids of the "
              "conversations they came from, are in `{path}` - read it first, then say in a few lines what you "
              "understand the current state to be, and wait for instructions.\n\nA high-level summary:\n\n{overview}")
FOLDER = "consolidations"


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


def overview(claude: str, base_url: str, sections: list[dict[str, Any]], run: Callable[..., Any] = subprocess.run) -> str:
    """One more isolated request: a high-level summary across the per-conversation ones. When it
    fails: each conversation's title with the first lines of its summary."""
    from .oneshot import ask
    ordered = sorted(sections, key=lambda s: s["updated"])
    text = "\n\n".join(f"## {s['title']}\n\n{s['summary']}" for s in ordered)
    try:
        return ask(claude, base_url, OVERVIEW_PROMPT.format(n=len(sections), summaries=text), OVERVIEW_TIMEOUT_S, run=run).strip()
    except RuntimeError:
        lines = []
        for s in ordered:
            words = [line.strip() for line in s["summary"].splitlines() if line.strip() and not line.strip().startswith("**")]
            lines.append(f"- **{s['title']}**: {' '.join(words)[:300]}")
        return "\n".join(lines)


def brief_file_text(sections: list[dict[str, Any]], summary: str, made: float) -> str:
    """The Markdown file: the conversations it came from first - positively identified, so they can be
    acted on once the result has been checked - then the high-level summary, then each in full."""
    ordered = sorted(sections, key=lambda s: s["updated"])
    stamp = time.strftime("%d/%m/%Y %H:%M", time.localtime(made))
    out = [f"# A new chat from {len(ordered)} earlier ones ({stamp})", "",
           "## The conversations it was made from", "",
           "Check this summary first. Once it is right, these can be deleted: on the Session page choose",
           "Delete..., and tick these ids (they go to Recently deleted for 30 days).", "",
           "| # | Conversation id | Title | Last active | Turns | Transcript |",
           "|---|---|---|---|---|---|"]
    for i, s in enumerate(ordered, start=1):
        when = time.strftime("%d/%m/%Y %H:%M", time.localtime(s["updated"]))
        title = str(s["title"]).replace("|", "/")
        out.append(f"| {i} | `{s['id']}` | {title} | {when} | {s['turns']} | `{s.get('path', '')}` |")
    out += ["", "## High-level summary", "", summary.strip(), ""]
    for i, s in enumerate(ordered, start=1):
        out += [f"## {i}. {s['title']}", "", s["summary"].strip(), ""]
    return "\n".join(out)


def write_brief_file(home: Path, text: str, made: float) -> Path:
    folder = home / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / time.strftime("%Y-%m-%d-%H%M%S-new-chat-brief.md", time.localtime(made))
    path.write_text(text, encoding="utf-8")
    return path


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
              run: Callable[..., Any] = subprocess.run, home: Path | None = None) -> str:
        """`home`: the profile's home folder. With one, the full summaries go to a Markdown file there
        and the brief is a high-level summary naming it (2026-10-17); without, the whole brief."""
        job = uuid.uuid4().hex[:12]
        self.jobs[job] = {"total": len(items), "done": 0, "status": "running", "brief": "", "failed": [],
                          "titles": [t for _p, t in items], "file": ""}

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
                                 "turns": len(turns), "summary": summary, "path": str(path)})
                with self.lock:
                    self.jobs[job]["done"] += 1
            text, written = brief(sections), ""
            if home is not None and home.is_dir():
                try:
                    made = time.time()
                    summary = overview(claude, base_url, sections, run)
                    written = str(write_brief_file(home, brief_file_text(sections, summary, made), made))
                    text = SHORT_HEAD.format(n=len(sections), path=written, overview=summary)
                except OSError as exc:
                    self.jobs[job]["failed"].append(f"the summary file could not be written ({exc}); the whole brief is in the prompt")
            with self.lock:
                self.jobs[job].update(status="done", brief=text, file=written)

        threading.Thread(target=work, name="aicleaner-consolidate", daemon=True).start()
        return job

    def get(self, job: str) -> dict[str, Any] | None:
        return self.jobs.get(job)
