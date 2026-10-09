"""Swap a freshly written file into place, waiting out a sync client or scanner that holds it.

Every store writes a .tmp beside the file and replaces the file with it, so a reader never sees
half a document. On Windows the replace fails with "Access is denied" while anything has the
target open, and OneDrive opens a file it has just seen change to upload it: the second of two
quick writes to a client file in the shared folder failed, and what it carried was lost. The
lock lasts well under a second, so waiting briefly is enough.
"""

from __future__ import annotations

import time
from pathlib import Path

ATTEMPTS = 20
PAUSE_S = 0.1


def network_path(text: str) -> bool:
    """A UNC path (`\\\\host\\share`, `//host/share`) or a device path (`\\\\?\\`, `\\\\.\\`), judged
    on the raw string (H27). Resolving one makes Windows open it - and for a UNC path, authenticate
    to the host, handing it this user's NTLM hash - so it has to be refused before any resolve()."""
    text = str(text or "").strip().strip('"')
    return text.startswith(("\\\\", "//"))


def replace(tmp: Path, target: Path, attempts: int = ATTEMPTS, pause_s: float = PAUSE_S) -> None:
    """tmp.replace(target), retried for about two seconds while the target is locked."""
    for attempt in range(attempts):
        try:
            tmp.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause_s)
