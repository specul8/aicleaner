"""Move a conversation to another project session (plan item J3; Brad, 2026-10-20: "primarily ... from
casual chats to a specific client, but the same feature should be available anywhere").

A conversation belongs to a project session by where Claude Code files its transcript: under the
session's home folder. Moving it files it under the target's home instead (`history.refile`, never over
one already there), so it continues there - that client's folders, filters and brief. Its title goes
with it: titles are kept by conversation id.

With `files`, the files the conversation made are MOVED (not copied) too (Brad, 2026-10-20). "Made":
what its Write / Edit / MultiEdit / NotebookEdit tool calls wrote, read from its transcript, still on
disk and inside the source session's folders. Each keeps its place relative to the source folder it was
in, under the target's home; one whose name is taken there is left where it is, never overwritten.
Files a command made (Bash) cannot be told apart from the rest, so they are not moved.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Iterable

log = logging.getLogger("aicleaner.convmove")

WRITERS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def _under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def written_files(transcript: Path, folders: Iterable[str | Path]) -> list[tuple[Path, Path]]:
    """(file, the source folder it is under) for each file the conversation's tool calls wrote that is
    still there and inside one of `folders` - in the order first written, each once."""
    roots = [Path(f) for f in folders if f]
    found: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    try:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            key = WRITERS.get(str(block.get("name", "")))
            raw = (block.get("input") or {}).get(key) if key else None
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw)
            norm = os.path.normcase(str(path))
            if norm in seen or not path.is_file():
                continue
            root = next((r for r in roots if _under(path, r)), None)
            if root is None:
                continue
            seen.add(norm)
            found.append((path, root))
    return found


def move_files(files: list[tuple[Path, Path]], target_home: Path) -> tuple[list[str], list[str]]:
    """Each file to the same place under the target's home. Returns (moved to, left where they were)."""
    moved, left = [], []
    for path, root in files:
        destination = target_home / path.resolve().relative_to(root.resolve())
        if destination.exists():
            left.append(str(path))
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
            moved.append(str(destination))
        except OSError as exc:
            log.warning("not moved %s: %s", path, exc)
            left.append(str(path))
    return moved, left
