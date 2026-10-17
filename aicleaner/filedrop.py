"""Dragging files and folders into the file tree (2026-10-16, item H2; spec 2026-10-16-h2-drag-drop-files-design.md).

A browser hands over a dropped item's contents, never its path, so a drop is a copy: each file is
uploaded into the folder it was dropped on, folders recreated. The folder is found through the file
tree's own allow-list; every name is checked; nothing is written through a link or junction (D171);
each file lands under a temporary name and is renamed into place once whole.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE = 200 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_FILES = 2000
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
MODES = ("replace", "keep", "skip")


class DropError(ValueError):
    """A drop, or one item of it, refused - with the reason the page shows."""


def parts_of(relative: str) -> list[str]:
    """A dropped item's path below the folder, as its parts - or DropError: no climbing out, nothing
    absolute, no reserved Windows names, no alternate data streams, no characters Windows refuses."""
    text = str(relative or "").replace("\\", "/")
    if not text.strip() or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise DropError(f"{relative!r} is not a name inside the folder")
    parts = list(PurePosixPath(text).parts)
    for part in parts:
        if part in ("", ".", ".."):
            raise DropError(f"{relative!r} climbs out of the folder")
        if BAD_CHARS.search(part):
            raise DropError(f"{part!r} has a character Windows does not allow in a name (a colon names a hidden stream)")
        if RESERVED.match(part) or part != part.rstrip(" ."):
            raise DropError(f"{part!r} is a name Windows reserves or will not keep")
    return parts


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def destination(folder: Path, relative: str) -> Path:
    """Where an item goes, checked: below the folder, and through no link or junction on the way."""
    target = folder
    for part in parts_of(relative):
        target = target / part
        if target.exists() and _linked(target):
            raise DropError(f"{target} is a link or junction; nothing is copied through one")
    return target


def check(folder: Path, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Before anything is sent: the limits, each name, and which files are there already."""
    files = [i for i in items if not i.get("dir")]
    total = sum(int(i.get("size") or 0) for i in files)
    problems = []
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files is more than {MAX_FILES} in one drop")
    if total > MAX_TOTAL:
        problems.append(f"{total // (1024 * 1024)} MB is more than {MAX_TOTAL // (1024 * 1024)} MB in one drop")
    for item in files:
        if int(item.get("size") or 0) > MAX_FILE:
            problems.append(f"{item.get('relative')} is more than {MAX_FILE // (1024 * 1024)} MB")
    clashes = []
    for item in items:
        try:
            target = destination(folder, str(item.get("relative", "")))
        except DropError as problem:
            problems.append(str(problem))
            continue
        if not item.get("dir") and target.exists():
            clashes.append(str(item["relative"]))
    return {"problems": problems, "clashes": clashes, "files": len(files), "bytes": total}


def kept_both(target: Path) -> Path:
    """'notes (2).md', '(3)' and so on: the first name not taken."""
    n = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _part_file(target: Path) -> Path:
    """The temporary name a file is written under until it is whole: short (Windows stops at 260)
    and the same for every piece of the same file."""
    digest = hashlib.sha1(str(target).lower().encode("utf-8")).hexdigest()[:10]
    return target.parent / f"~drop-{digest}.part"


def write_piece(folder: Path, relative: str, mode: str, offset: int, data: bytes, last: bool) -> dict[str, Any]:
    """One piece of one file, in order; on the last, renamed into place as the mode says."""
    if mode not in MODES:
        raise DropError(f"mode must be one of {', '.join(MODES)}")
    target = destination(folder, relative)
    if offset == 0 and target.exists() and mode == "skip":
        return {"relative": relative, "written": "", "skipped": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    destination(folder, relative)                          # the folders just made are not links either
    part = _part_file(target)
    held = part.stat().st_size if part.exists() else 0
    if offset == 0:
        part.write_bytes(b"")
        held = 0
    if offset != held:
        raise DropError(f"{relative}: a piece arrived out of order (at {offset}, expected {held})")
    if held + len(data) > MAX_FILE:
        part.unlink(missing_ok=True)
        raise DropError(f"{relative} is more than {MAX_FILE // (1024 * 1024)} MB")
    with open(part, "ab") as out:
        out.write(data)
    if not last:
        return {"relative": relative, "received": held + len(data)}
    final = kept_both(target) if (mode == "keep" and target.exists()) else target
    if final.is_dir():
        part.unlink(missing_ok=True)
        raise DropError(f"{final} is a folder; a file cannot replace it")
    os.replace(part, final)
    return {"relative": relative, "written": str(final), "skipped": False}


def make_folder(folder: Path, relative: str) -> dict[str, Any]:
    target = destination(folder, relative)
    if target.exists() and not target.is_dir():
        raise DropError(f"{target} is a file, so a folder of that name cannot be made")
    target.mkdir(parents=True, exist_ok=True)
    return {"relative": relative, "made": str(target)}
