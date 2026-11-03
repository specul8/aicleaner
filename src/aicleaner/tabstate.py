"""Which file tabs a session has open (D136): a hard refresh used to lose them all.

Kept per session name beside the profiles on this machine, as the tree's open folders are
(treestate.py): it is how this person last looked at the files, and it follows them to another
device. Only where each file is and how it was shown - never its contents; opening one goes back
through the file endpoint, which holds it to the tree's own allow-list as ever.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import fileio

TABS_STATE_FILE = "tabs-state.json"
# Fed by a page, so bounded.
MAX_TABS = 30
MAX_FIELD = 1024
VIEWS = {"text", "image", "pdf", "download"}
MODES = {"source", "preview"}


def _clean(tab: Any) -> dict[str, str] | None:
    if not isinstance(tab, dict):
        return None
    root, path, name = (str(tab.get(k) or "") for k in ("root", "path", "name"))
    if not root or not path or not name or any(len(v) > MAX_FIELD for v in (root, path, name)):
        return None
    return {"root": root, "path": path, "name": name,
            "view": tab.get("view") if tab.get("view") in VIEWS else "text",
            "mode": tab.get("mode") if tab.get("mode") in MODES else "source"}


class TabState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
            return {str(k): v for k, v in sessions.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            return {}    # missing or unreadable: the tabs are forgotten, the page is not

    def load(self, session: str) -> dict[str, Any]:
        with self._lock:
            saved = self._read().get(session) or {}
        tabs = [t for t in (_clean(x) for x in saved.get("tabs") or []) if t][:MAX_TABS]
        keys = {f"{t['root']}|{t['path']}" for t in tabs}
        active = str(saved.get("active") or "")
        return {"tabs": tabs, "active": active if active in keys else ""}

    def save(self, session: str, tabs: object, active: object) -> dict[str, Any]:
        if not session:
            raise ValueError("a session name is required")
        if not isinstance(tabs, list):
            raise ValueError("tabs must be a list")
        kept = [t for t in (_clean(x) for x in tabs) if t][:MAX_TABS]
        keys = {f"{t['root']}|{t['path']}" for t in kept}
        entry = {"tabs": kept, "active": str(active or "") if str(active or "") in keys else ""}
        with self._lock:
            sessions = self._read()
            if kept:
                sessions[session] = entry
            else:
                sessions.pop(session, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": sessions}, indent=2, sort_keys=True), encoding="utf-8")
            fileio.replace(tmp, self.path)
        return entry
