"""f178 (Brad, 2026-11-14): New file, Rename and Delete from the files panel's menu (spec 2026-11-14-f178-files-menu.md).

The places come checked already - through the file tree's own allow-list and no link or junction (SessionHost); this
module checks the names and does the work. A name Windows would refuse is refused here with the reason, before any
file is touched. Delete moves to the Recycle Bin, never deletes for good: on a drive without one it is refused.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from .filedrop import BAD_CHARS, RESERVED

MAX_NAME = 255
MAX_DELETE = 500


class FileMenuError(ValueError):
    """A name or an action refused, with the reason the page shows."""


class NotThere(FileMenuError):
    """The item is no longer there (deleted or renamed meanwhile)."""


def check_name(name: str) -> str:
    """A file or folder name as Windows keeps it, or FileMenuError saying why not. Returned unchanged."""
    text = str(name or "")
    if not text.strip() or text in (".", ".."):
        raise FileMenuError("type a name")
    if "/" in text or "\\" in text:
        raise FileMenuError(f"{text!r} is not a path - type only the name; the file goes in the folder it was made in")
    if BAD_CHARS.search(text):
        raise FileMenuError(f"{text!r} has a character Windows does not allow in a name (< > : \" | ? * or a control "
                            "character)")
    if RESERVED.match(text):
        raise FileMenuError(f"{text!r} is a name Windows reserves for a device")
    if text != text.rstrip(" ."):
        raise FileMenuError(f"{text!r} ends in a space or a dot, which Windows drops")
    if len(text) > MAX_NAME:
        raise FileMenuError(f"that name is too long ({len(text)} characters; at most {MAX_NAME})")
    return text


def _taken(folder: Path, name: str, besides: Path | None = None) -> bool:
    """Whether the folder already holds something by this name, ignoring case as Windows does - except the item being
    renamed itself (a rename that only changes case)."""
    wanted = name.lower()
    try:
        for entry in os.scandir(folder):
            if entry.name.lower() == wanted and (besides is None or os.path.normcase(entry.path) != os.path.normcase(str(besides))):
                return True
    except OSError as exc:
        raise FileMenuError(f"{folder} cannot be read: {exc.strerror or exc}") from exc
    return False


def new_file(folder: Path, name: str) -> Path:
    """An empty file `name` in `folder`; never over anything already there."""
    name = check_name(name)
    if not folder.is_dir():
        raise FileMenuError(f"{folder.name or folder} is not a folder")
    if _taken(folder, name):
        raise FileMenuError(f"{name} is already in {folder.name or folder}")
    target = folder / name
    try:
        with open(target, "x", encoding="utf-8"):
            pass
    except FileExistsError:
        raise FileMenuError(f"{name} is already in {folder.name or folder}") from None
    except OSError as exc:
        raise FileMenuError(f"{name} could not be made: {exc.strerror or exc}") from exc
    return target


def rename(target: Path, name: str) -> Path:
    """`target` renamed to `name` in the same folder; never over something else."""
    name = check_name(name)
    if not target.exists():
        raise NotThere(f"{target.name} is no longer there")
    if name == target.name:
        return target
    if _taken(target.parent, name, besides=target):
        raise FileMenuError(f"{name} is already in {target.parent.name or target.parent}")
    destination = target.with_name(name)
    try:
        os.rename(target, destination)
    except OSError as exc:
        raise FileMenuError(f"{target.name} could not be renamed: {exc.strerror or exc}") from exc
    return destination


# ---- Delete: to the Recycle Bin ---------------------------------------------------------------------------------

def _has_recycle_bin(path: Path) -> bool:
    """A fixed local drive has a Recycle Bin; a removable or network drive does not, and Windows would delete for good."""
    if sys.platform != "win32":
        return False
    import ctypes
    drive = os.path.splitdrive(str(path.resolve()))[0]
    if not drive or drive.startswith("\\\\"):
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3          # DRIVE_FIXED


def _to_bin_folder(paths: list[Path], folder: Path) -> None:
    """Tests only (AICLEANER_TEST_RECYCLE_DIR): the bin is a folder, so a suite never fills the real one."""
    for path in paths:
        place = folder / uuid.uuid4().hex[:12]
        place.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(place / path.name))


def _shell_recycle(paths: list[Path]) -> None:
    """SHFileOperationW, FO_DELETE with FOF_ALLOWUNDO: to the Recycle Bin, silently, no dialogs."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete = 0x0003
    flags = 0x0040 | 0x0010 | 0x0004 | 0x0400            # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    names = "\0".join(str(p) for p in paths) + "\0\0"   # a double-NUL-terminated list
    operation = SHFILEOPSTRUCTW(None, fo_delete, names, None, flags, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise FileMenuError(f"Windows did not move them to the Recycle Bin (code {result:#x}); nothing was deleted "
                            "for good - try again, or delete in File Explorer")


def recycle(paths: list[Path]) -> int:
    """Every path moved to the Recycle Bin, or FileMenuError. Refused (nothing touched) when any is on a drive with no
    Recycle Bin - Windows would delete those for good without asking."""
    if not paths:
        raise FileMenuError("nothing to delete")
    if len(paths) > MAX_DELETE:
        raise FileMenuError(f"{len(paths)} items is more than one Delete takes ({MAX_DELETE})")
    diverted = os.environ.get("AICLEANER_TEST_RECYCLE_DIR", "")
    if diverted:
        _to_bin_folder(paths, Path(diverted))
        return len(paths)
    if sys.platform != "win32":
        raise FileMenuError("Delete moves to the Recycle Bin, which is on Windows only - delete it in your file manager")
    for path in paths:
        if not _has_recycle_bin(path):
            raise FileMenuError(f"{path.name} is on a drive with no Recycle Bin, where Windows would delete it for good - "
                                "delete it in File Explorer if you mean to")
    _shell_recycle(paths)
    still = [p.name for p in paths if p.exists()]
    if still:
        raise FileMenuError(f"{', '.join(still)} could not be moved to the Recycle Bin (in use?)")
    return len(paths)
