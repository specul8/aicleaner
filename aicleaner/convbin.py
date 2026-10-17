"""Deleting a conversation, recoverably (2026-10-16, item D; spec 2026-10-16-d-delete-conversation-design.md).

A conversation's transcript `<id>.jsonl` and its folder `<id>\\` (sub-agents, tool results) are moved -
never deleted - to `<bin>\\<YYYYMMDD>\\<transcript folder name>\\`, with `<id>.json` beside them saying
what it was and where it came from. Put back moves them home again; after 30 days they go for good.
Refused while the conversation is running, or was written to in the last few minutes (a terminal or
an editor may have it open). The conversation registry keeps the id, so a conversation put back is
listed exactly as before.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from .history import BUSY_WINDOW_S, UUID, folders_for

KEEP_DAYS = 30


class BinError(RuntimeError):
    pass


def _find(projects: Iterable[str], conv_id: str, root: Path) -> Path | None:
    for project in projects:
        for folder in folders_for(project, root):
            path = folder / f"{conv_id}.jsonl"
            if path.is_file():
                return path
    return None


def delete(projects: Iterable[str], conv_id: str, root: Path, bin_root: Path, profile: str, title: str = "",
           running: Iterable[str] = (), now: float | None = None) -> dict[str, Any]:
    if not UUID.match(conv_id or ""):
        raise BinError("not a conversation id")
    if conv_id.lower() in {str(r).lower() for r in running}:
        raise BinError("that conversation is running in a session - stop it first")
    path = _find(projects, conv_id, root)
    if path is None:
        raise BinError("no such conversation in this profile's folders")
    now = now or time.time()
    if now - path.stat().st_mtime < BUSY_WINDOW_S:
        raise BinError("that conversation was written to in the last few minutes - a terminal or an editor "
                       "may still have it open; try again later")
    target = bin_root / time.strftime("%Y%m%d", time.localtime(now)) / path.parent.name
    target.mkdir(parents=True, exist_ok=True)
    meta = {"id": conv_id, "title": title, "profile": profile, "original": str(path),
            "folder": str(path.parent), "deleted_at": now, "size": path.stat().st_size}
    shutil.move(str(path), str(target / path.name))
    companion = path.parent / conv_id
    if companion.is_dir():
        shutil.move(str(companion), str(target / conv_id))
    (target / f"{conv_id}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def _entries(bin_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    if not bin_root.is_dir():
        return out
    for meta in bin_root.glob("*/*/*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append((meta, data))
    return out


def deleted(bin_root: Path, profile: str) -> list[dict[str, Any]]:
    rows = [d for _m, d in _entries(bin_root) if d.get("profile") == profile]
    return sorted(rows, key=lambda d: d.get("deleted_at", 0), reverse=True)


def _entry(bin_root: Path, profile: str, conv_id: str) -> tuple[Path, dict[str, Any]]:
    for meta, data in _entries(bin_root):
        if data.get("profile") == profile and str(data.get("id")).lower() == str(conv_id).lower():
            return meta, data
    raise BinError("no such conversation in Recently deleted")


def restore(bin_root: Path, profile: str, conv_id: str) -> dict[str, Any]:
    meta, data = _entry(bin_root, profile, conv_id)
    original = Path(data["original"])
    if original.exists():
        raise BinError(f"a conversation with that id is already back in {original.parent}")
    original.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(meta.with_suffix(".jsonl")), str(original))
    companion = meta.parent / data["id"]
    if companion.is_dir():
        shutil.move(str(companion), str(original.parent / data["id"]))
    meta.unlink()
    return data


def purge(bin_root: Path, profile: str, conv_id: str) -> dict[str, Any]:
    meta, data = _entry(bin_root, profile, conv_id)
    _remove(meta, data)
    return data


def _remove(meta: Path, data: dict[str, Any]) -> None:
    for path in (meta.with_suffix(".jsonl"), meta):
        path.unlink(missing_ok=True)
    companion = meta.parent / str(data.get("id"))
    if companion.is_dir():
        shutil.rmtree(companion, ignore_errors=True)


def clean(bin_root: Path, now: float | None = None, days: int = KEEP_DAYS) -> int:
    """Remove what has been in the bin longer than `days`; answers how many went."""
    now = now or time.time()
    gone = 0
    for meta, data in _entries(bin_root):
        if now - float(data.get("deleted_at") or now) > days * 86400:
            _remove(meta, data)
            gone += 1
    for folder in sorted(bin_root.glob("*/*"), reverse=True) if bin_root.is_dir() else []:
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return gone
