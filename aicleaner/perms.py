"""Folders other people on this PC can write to (H18, DECISIONS 68): warned about, never refused.

The install folder holds the code the proxy runs and the Python it runs with; the clients folder
holds the mapping; the logs hold real-to-token pairs. On a PC where "Users", "Authenticated Users"
or "Everyone" may write to one of them, someone else on that PC could change what the proxy does or
read what it has seen. aicleaner is meant for one person's PC, so this only says so - on the status
bar - and never stops anything.

Read with Windows' own `icacls`, in the background: it is a process per folder, and the status bar
asks every few seconds. The answer is cached for an hour.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterable

TTL_S = 3600
# Who counts as "other people": the groups every local account is in. English names and their
# well-known SIDs, since icacls prints whichever it has.
OTHERS = re.compile(r"(BUILTIN\\Users|NT AUTHORITY\\Authenticated Users|\bEveryone|S-1-5-32-545|S-1-5-11|S-1-1-0):",
                    re.IGNORECASE)
# Rights that allow writing: full, modify, write, or the fine-grained write and append rights.
WRITES = re.compile(r"\((?:[^)]*,)?(F|M|W|WD|AD|GW|GA)(?:,[^)]*)?\)")

_cache: dict[str, object] = {"at": 0.0, "found": [], "running": False}
_lock = threading.Lock()


def writable_by_others(folder: str | Path) -> bool:
    """Whether the folder's own permissions let a group of other local users write to it."""
    if os.name != "nt" or not Path(folder).is_dir():
        return False
    try:
        out = subprocess.run(["icacls", str(folder)], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in out.splitlines():
        found = OTHERS.search(line)
        if found and WRITES.search(line[found.end():]):
            return True
    return False


def _refresh(folders: list[str]) -> None:
    try:
        found = [f for f in folders if writable_by_others(f)]
        with _lock:
            _cache.update(at=time.time(), found=found)
    finally:
        with _lock:
            _cache["running"] = False


def open_folders(folders: Iterable[str | Path]) -> list[str]:
    """The folders found writable by others at the last look; looks again in the background when that
    was over an hour ago. Empty until the first look has finished."""
    wanted = sorted({str(f) for f in folders if str(f).strip()})
    with _lock:
        stale = time.time() - float(_cache["at"]) > TTL_S or _cache.get("for") != wanted
        if stale and not _cache["running"]:
            _cache["running"] = True
            _cache["for"] = wanted
            threading.Thread(target=_refresh, args=(wanted,), daemon=True).start()
        return [f for f in _cache["found"] if f in wanted]  # type: ignore[union-attr]
