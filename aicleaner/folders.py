"""Which folders may be reached, and what a folder picker is allowed to show.

A folder used to be typed into a box and read as given, which is why a scan was refused through the
tunnel: not because the path was ambiguous - a scan can only ever read this machine - but because
nothing bounded it. Refusing the feature was the wrong answer. Reaching your own environment from
somewhere else is the point of the tunnel; what needed bounding was the reach, not the reacher.

So there is a list of allowed roots, set on this machine, and two questions with written answers:

    usable  - a root, or anything under one. This is what may be chosen.
    visible - usable, or an ancestor of a root. An ancestor lists only the children that lead to a
              root, never its siblings, so the tree reads from the drive down and no branch that is
              not on the way to something allowed is even mentioned.

The second is what makes a picker possible without the picker itself being a disclosure. With
C:\\Users\\brads\\OneDrive allowed, C:\\ offers Users and nothing else, C:\\Users offers brads and
nothing else, and the OneDrive folder is fully browsable. Nothing hints at what else the disk holds.

Links are resolved before either question is asked, so a junction inside an allowed folder cannot
be used to step outside one.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath


class FolderError(ValueError):
    """A folder that cannot be used, with the reason a person needs."""


class FolderOutside(FolderError):
    """The folder is not one these pages may reach.

    Kept apart from the rest because it is a different problem with a different answer: a folder
    that does not exist is a typo, and a folder outside the list is a decision somebody has to make
    in Settings. Sharing one status made a mistyped path read as a permission refusal.
    """


def _resolved(path: str | Path) -> Path:
    """The real path, links followed. strict=False so a folder that is gone still resolves to
    something comparable rather than raising - it is refused for not existing, further down, which
    is the message somebody can act on."""
    try:
        return Path(str(path).strip().strip('"').strip("'")).expanduser().resolve(strict=False)
    except (OSError, ValueError) as problem:      # a malformed path is a refusal, never a crash
        raise FolderError(f"not a usable path: {path}") from problem


def _within(child: Path, parent: Path) -> bool:
    """True when child is parent or sits under it. Case-insensitive on Windows, which PurePath
    already knows: comparing strings here would make C:\\Users and c:\\users different folders."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# Folders that are never a client's work, and their descendants. Setting a root here would put
# the whole of Windows or every installed program behind the picker for no gain, and it is the
# one thing that makes `folders.roots` worth de-risking now that it can be set remotely
# (decided 2026-09-27) - from a train, in a hurry, with autocomplete.
#
# Read from the environment rather than hard-coded to C:, because a machine whose Windows is on
# D: would otherwise be checked against paths that do not exist there.
_BLOCKED_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "SystemRoot", "windir")
_BLOCKED_FALLBACKS = (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\ProgramData", r"C:\Windows")


def blocked_roots() -> list[Path]:
    """The system folders a root may not be, or be inside."""
    found: list[Path] = []
    seen: set[str] = set()
    for value in [os.environ.get(name, "") for name in _BLOCKED_VARS] + list(_BLOCKED_FALLBACKS):
        text = (value or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
        except (OSError, ValueError):
            continue
        key = str(path).rstrip("\\/").lower()
        if key and key not in seen:
            seen.add(key)
            found.append(path)
    return found


def blocked_reason(path: str | Path) -> str:
    """Why this folder may not be a root, or "" when it may.

    Checked on the text rather than after resolving, and again after: a reparse point or a short
    name (PROGRA~1) would otherwise walk straight past a comparison done only one way.
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(str(path)).expanduser())
    except (OSError, ValueError):
        return ""
    try:
        candidates.append(candidates[0].resolve())
    except (OSError, ValueError, RuntimeError):
        pass
    for blocked in blocked_roots():
        for candidate in candidates:
            if _within(candidate, blocked) or str(candidate).rstrip("\\/").lower() == str(blocked).rstrip("\\/").lower():
                return (f"{blocked} is a system folder. Client work is never in there, and a root "
                        f"there would put every file under it behind the picker.")
    return ""


def roots_of(configured: list[str]) -> list[Path]:
    """The configured roots, resolved, with unusable entries dropped rather than fatal.

    A root that has been renamed or unplugged should narrow what can be reached, not stop the proxy
    from starting: the rest of the list still means what it said.
    """
    out: list[Path] = []
    for entry in configured or []:
        text = str(entry).strip()
        if not text:
            continue
        try:
            out.append(_resolved(text))
        except FolderError:
            continue
    return out


def is_usable(path: str | Path, roots: list[Path]) -> bool:
    """May this folder be chosen? True for a root and anything under one."""
    if not roots:
        return False
    target = _resolved(path)
    return any(_within(target, root) for root in roots)


def is_visible(path: str | Path, roots: list[Path]) -> bool:
    """May this folder be looked at? Usable, or on the way to something usable."""
    if not roots:
        return False
    target = _resolved(path)
    return any(_within(target, root) or _within(root, target) for root in roots)


def check_usable(path: str | Path, roots: list[Path], what: str = "folder",
                 must_exist: bool = True) -> Path:
    """The resolved folder, or a refusal that says what to do about it.

    Every endpoint taking a folder calls this. The picker only shows what is allowed, but a
    convenience cannot be a control: a typed path, a page left open since before the list changed,
    and a direct POST all arrive here.

    `must_exist` is off where the caller already has a better answer for a folder that is not there
    - the share store says "does not exist" and names it - so this only decides the boundary and
    leaves the rest of the message to whoever knows more about it.
    """
    if not str(path or "").strip():
        raise FolderError(f"name the {what}")
    if not roots:
        raise FolderOutside(
            f"no folders are allowed yet, so no {what} can be chosen. "
            "Add one on this machine under Settings > Folders the pages may reach.")
    target = _resolved(path)
    if not is_usable(target, roots):
        allowed = ", ".join(str(r) for r in roots)
        raise FolderOutside(f"{target} is outside the folders these pages may reach ({allowed})")
    if must_exist and not target.is_dir():
        raise FolderError(f"not a folder on this machine: {target}")
    return target


def listing(path: str | Path | None, roots: list[Path]) -> dict:
    """What a picker may show at `path`: the folders inside it that are visible.

    At an ancestor, that is only the children leading to a root. At or under a root, it is
    everything. Files are never listed: nothing here wants one.
    """
    if not roots:
        raise FolderOutside("no folders are allowed yet. Add one on this machine under "
                            "Settings > Folders the pages may reach.")
    target = _resolved(path) if str(path or "").strip() else _common_top(roots)
    if not is_visible(target, roots):
        raise FolderOutside(f"{target} is outside the folders these pages may reach")

    names: list[dict] = []
    if is_usable(target, roots):
        try:
            children = sorted((c for c in target.iterdir() if c.is_dir()), key=lambda c: c.name.lower())
        except OSError as problem:
            raise FolderError(f"cannot read {target}: {problem}") from problem
        names = [{"name": c.name, "path": str(c), "usable": True} for c in children]
    else:
        # An ancestor: name only the next step towards each root, never what else is alongside it.
        seen: set[str] = set()
        for root in roots:
            if not _within(root, target):
                continue
            step = root.parts[len(target.parts)] if len(root.parts) > len(target.parts) else None
            if step and step.lower() not in seen:
                seen.add(step.lower())
                child = target / step
                names.append({"name": step, "path": str(child), "usable": is_usable(child, roots)})
        names.sort(key=lambda entry: entry["name"].lower())

    parent = str(target.parent) if target.parent != target and is_visible(target.parent, roots) else ""
    return {"path": str(target), "parent": parent, "entries": names,
            "usable": is_usable(target, roots)}


def _common_top(roots: list[Path]) -> Path:
    """Where a picker opens with nothing to go on: the drive of the first root, so the tree reads
    from the top down the way somebody expects a file dialog to."""
    first = roots[0]
    return Path(first.anchor) if first.anchor else first


# Names Windows reserves for devices, with or without an extension (CON, CON.txt).
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_BAD_CHARACTERS = set('<>:"/\|?*')


def make_folder(parent: str | Path, name: str, roots: list[Path]) -> Path:
    """Make one new folder inside an allowed one, for the picker (2026-09-30).

    `name` is a single plain folder name: no separators, no `.` or `..`, nothing Windows refuses, no
    trailing dot or space. The parent must be usable, and the result is checked again after it is
    resolved - so a name cannot be made to point anywhere but directly inside the parent.
    """
    folder_name = str(name or "").strip()
    if not folder_name:
        raise FolderError("name the new folder")
    if (folder_name in (".", "..") or any(c in _BAD_CHARACTERS or ord(c) < 32 for c in folder_name)
            or folder_name.endswith((".", " ")) or folder_name.split(".", 1)[0].lower() in _RESERVED):
        raise FolderError(f"{folder_name!r} cannot be a folder name here: use letters, digits, spaces, "
                          "dashes and dots, with no slashes and no trailing dot")
    base = check_usable(parent, roots)
    target = _resolved(base / folder_name)
    if target.parent != base or not is_usable(target, roots):
        raise FolderOutside(f"{target} would be outside the folders these pages may reach")
    if target.exists():
        raise FolderError(f"{target} already exists; open it instead")
    try:
        target.mkdir()
    except OSError as problem:
        raise FolderError(f"could not make {target}: {problem.strerror or problem}") from problem
    return target
