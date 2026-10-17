"""Conversation titles (2026-10-16, item E; spec 2026-10-16-e-relabel-conversation-design.md).

A title says what a conversation most recently worked on. The model writes one - when a session stops
(if the conversation grew by at least four turns since its last title) and on demand - and a title the
person typed always wins and is never replaced. Kept in `<state>\\conversation-labels.json`:
`{id: {title, source: "model"|"person", written_at, turns}}`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .convsearch import Turn, extract

LABELS = "conversation-labels.json"
GROWTH = 4                  # turns a conversation must gain before the model retitles it
LAST_TURNS = 30
MAX_WORDS = 6000
MAX_TITLE = 80
TIMEOUT_S = 60
_LOCK = threading.Lock()

PROMPT = """Below are the most recent turns of a conversation between a person and an AI coding assistant.
Write a title of at most 8 words naming what was most recently worked on. Plain words, no quotes, no
trailing full stop. Answer with the title only.

{turns}"""


def labels_file(state_dir: Path) -> Path:
    return state_dir / LABELS


def load(state_dir: Path | None) -> dict[str, dict[str, Any]]:
    if state_dir is None:
        return {}
    try:
        data = json.loads(labels_file(state_dir).read_text(encoding="utf-8"))
        return {str(k).lower(): v for k, v in data.items() if isinstance(v, dict) and v.get("title")}
    except (OSError, ValueError, AttributeError):
        return {}


def _save(state_dir: Path, labels: dict[str, dict[str, Any]]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = labels_file(state_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(labels, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, labels_file(state_dir))


def set_title(state_dir: Path, conv_id: str, title: str, source: str, turns: int = 0) -> dict[str, Any] | None:
    """Store a title; an empty person title clears theirs. A model title never replaces a person's."""
    with _LOCK:
        labels = load(state_dir)
        key = conv_id.lower()
        held = labels.get(key)
        if source == "model" and held and held.get("source") == "person":
            return held
        if not title.strip():
            labels.pop(key, None)
            _save(state_dir, labels)
            return None
        labels[key] = {"title": title.strip()[:MAX_TITLE], "source": source, "written_at": time.time(), "turns": turns}
        _save(state_dir, labels)
        return labels[key]


def clean_title(text: str) -> str:
    """The model's answer as a title: one line, no quotes or label, no full stop, cut at a word."""
    line = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    line = re.sub(r"^(title\s*:\s*)", "", line, flags=re.I).strip().strip("\"'`*").strip()
    line = line.rstrip(".")
    if len(line) > MAX_TITLE:
        line = line[:MAX_TITLE].rsplit(" ", 1)[0]
    return line


def prompt_for(turns: list[Turn]) -> str:
    recent = turns[-LAST_TURNS:]
    lines: list[str] = []
    words = 0
    for turn in reversed(recent):
        text = " ".join(turn.text.split())
        count = len(text.split())
        if words + count > MAX_WORDS:
            text = " ".join(text.split()[: max(0, MAX_WORDS - words)])
            count = len(text.split())
        if text:
            lines.append(f"{turn.role}: {text}")
        words += count
        if words >= MAX_WORDS:
            break
    return PROMPT.format(turns="\n\n".join(reversed(lines)))


def model_title(claude: str, base_url: str, turns: list[Turn], run: Callable[..., Any] = subprocess.run) -> str:
    """One isolated request to a small model (oneshot.ask), through this proxy. Raises RuntimeError."""
    from .oneshot import ask
    title = clean_title(ask(claude, base_url, prompt_for(turns), TIMEOUT_S, model="haiku", run=run))
    if not title:
        raise RuntimeError("the model gave no title")
    return title

def retitle(state_dir: Path, path: Path, claude: str, base_url: str, only_if_grown: bool = False,
            run: Callable[..., Any] = subprocess.run) -> dict[str, Any] | None:
    """Write the model's title for one transcript; with only_if_grown, only after GROWTH new turns."""
    conv_id = path.stem
    turns, _end = extract(path)
    if not turns:
        return None
    held = load(state_dir).get(conv_id.lower())
    if held and held.get("source") == "person":
        return held
    if only_if_grown and held and len(turns) - int(held.get("turns") or 0) < GROWTH:
        return held
    return set_title(state_dir, conv_id, model_title(claude, base_url, turns, run), "model", len(turns))
