"""Many files from the tree as one zip (feature 97).

The caller has already put every item through the tree's allow-list (`Session._locate`); this
walks folders, keeps each file's place relative to its project root, and refuses before writing a
byte when the whole would be too big. Links are never followed and a link out of its root is never
read; anything unreadable is listed in SKIPPED.txt inside the zip rather than going missing silently.
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_ZIP_FILES = 10_000
MAX_ZIP_BYTES = 1024 * 1024 * 1024          # before compression


class ZipTooBig(Exception):
    def __init__(self, files: int, size: int):
        self.files, self.size = files, size
        super().__init__(f"too big for one zip: {files:,} files, {size // (1024 * 1024):,} MB "
                         f"(the limit is {MAX_ZIP_FILES:,} files and {MAX_ZIP_BYTES // (1024 * 1024):,} MB)")


@dataclass
class ZipPlan:
    entries: list[tuple[str, Path]] = field(default_factory=list)    # (name in the zip, file)
    skipped: list[str] = field(default_factory=list)
    size: int = 0


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return False


def plan_zip(items: list[tuple[Path, Path]]) -> ZipPlan:
    """(root, target) pairs, already allowed, into what the zip will hold. Names are relative to
    the root with "/" separators; with items from two or more roots each starts with its root's
    folder name, so two projects' docs/README.md cannot collide. A file reached twice (a folder and
    a file inside it) is stored once."""
    plan = ZipPlan()
    several_roots = len({root for root, _ in items}) > 1
    seen: set[Path] = set()

    def name_for(root: Path, path: Path) -> str:
        relative = path.relative_to(root).as_posix()
        return f"{root.name}/{relative}" if several_roots else relative

    def add(root: Path, path: Path) -> None:
        shown = name_for(root, path)
        if path.is_symlink() and not _inside(path.resolve(), root):
            plan.skipped.append(f"{shown}: points outside the project")
            return
        real = path.resolve()
        if real in seen:
            return
        try:
            size = path.stat().st_size
        except OSError as problem:
            plan.skipped.append(f"{shown}: {problem.strerror or problem}")
            return
        seen.add(real)
        plan.entries.append((shown, path))
        plan.size += size
        if len(plan.entries) > MAX_ZIP_FILES or plan.size > MAX_ZIP_BYTES:
            raise ZipTooBig(len(plan.entries), plan.size)

    for root, target in items:
        if target.is_file():
            add(root, target)
            continue
        if target.is_symlink() and not _inside(target.resolve(), root):
            plan.skipped.append(f"{name_for(root, target)}: points outside the project")
            continue

        def unreadable(problem: OSError, root: Path = root) -> None:
            where = Path(problem.filename) if problem.filename else root
            plan.skipped.append(f"{name_for(root, where) if _inside(where, root) else where}: "
                                f"{problem.strerror or problem}")

        for folder, dirs, files in os.walk(target, onerror=unreadable, followlinks=False):
            # os.walk's followlinks does not cover a Windows junction (D171): one is walked like any
            # folder, wherever it leads. Every link or junction is left out and listed.
            for name in [d for d in dirs if _is_link(Path(folder) / d)]:
                dirs.remove(name)
                plan.skipped.append(f"{name_for(root, Path(folder) / name)}: a link or junction, not followed")
            dirs.sort(key=str.lower)
            for name in sorted(files, key=str.lower):
                add(root, Path(folder) / name)
    return plan


def write_zip(plan: ZipPlan, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for shown, path in plan.entries:
            try:
                archive.write(path, shown)
            except OSError as problem:
                plan.skipped.append(f"{shown}: {problem.strerror or problem}")
        if plan.skipped:
            archive.writestr("SKIPPED.txt", "Not included - could not be read:\n" + "\n".join(plan.skipped) + "\n")
