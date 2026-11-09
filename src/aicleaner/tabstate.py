"""Which file tabs a session has open (D136): a hard refresh used to lose them all.

Kept per session name beside the profiles on this machine, as the tree's open folders are
(treestate.py): it is how this person last looked at the files, and it follows them to another
device. Only where each file is and how it was shown - never its contents; opening one goes back
through the file endpoint, which holds it to the tree's own allow-list as ever.

Feature 171 (Brad, 2026-11-09): the tabs closed in the last 24 hours are kept here too, newest first
and at most MAX_CLOSED, each with where it stood in the strip, so "Reopen closed tab" survives a
reload. When each was closed is this machine's clock, not the page's: a time the page sends is kept
only for a tab already on the list, and a newly closed one is stamped now.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import fileio

TABS_STATE_FILE = "tabs-state.json"
# Fed by a page, so bounded.
MAX_TABS = 30
MAX_FIELD = 1024
# "plain": a file of a type nothing here knows, opened as plain text (feature 172).
VIEWS = {"text", "image", "pdf", "download", "plain"}
MODES = {"source", "preview"}
MAX_CLOSED = 20
CLOSED_FOR_S = 24 * 60 * 60


def _clean(tab: Any) -> dict[str, str] | None:
    if not isinstance(tab, dict):
        return None
    root, path, name = (str(tab.get(k) or "") for k in ("root", "path", "name"))
    if not root or not path or not name or any(len(v) > MAX_FIELD for v in (root, path, name)):
        return None
    return {"root": root, "path": path, "name": name,
            "view": tab.get("view") if tab.get("view") in VIEWS else "text",
            "mode": tab.get("mode") if tab.get("mode") in MODES else "source"}


def _key(tab: dict[str, Any]) -> str:
    return f"{tab['root']}|{tab['path']}"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number >= 0 else None     # not NaN, not negative


def _closed(entries: Any, open_keys: set[str], before: dict[str, float], now: float) -> list[dict[str, Any]]:
    """The closed list as kept: cleaned, none still open, none twice, none older than a day, newest
    first, at most MAX_CLOSED. `before`: the times already on record, by key."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        tab = _clean(entry)
        if not tab or _key(tab) in open_keys or _key(tab) in seen:
            continue
        seen.add(_key(tab))
        index = _number(entry.get("index"))
        tab["index"] = int(min(index, MAX_TABS)) if index is not None else 0
        tab["closed_at"] = before.get(_key(tab), now)
        if now - tab["closed_at"] < CLOSED_FOR_S:
            kept.append(tab)
    kept.sort(key=lambda t: t["closed_at"], reverse=True)
    return kept[:MAX_CLOSED]


class TabState:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._clock = clock

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
            return {str(k): v for k, v in sessions.items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            return {}    # missing or unreadable: the tabs are forgotten, the page is not

    @staticmethod
    def _times(saved: dict[str, Any]) -> dict[str, float]:
        times: dict[str, float] = {}
        for entry in saved.get("closed") or []:
            tab = _clean(entry)
            stamp = _number(entry.get("closed_at")) if isinstance(entry, dict) else None
            if tab and stamp is not None:
                times[_key(tab)] = stamp
        return times

    def load(self, session: str) -> dict[str, Any]:
        with self._lock:
            saved = self._read().get(session) or {}
        tabs = [t for t in (_clean(x) for x in saved.get("tabs") or []) if t][:MAX_TABS]
        keys = {_key(t) for t in tabs}
        active = str(saved.get("active") or "")
        closed = _closed(saved.get("closed"), keys, self._times(saved), self._clock())
        return {"tabs": tabs, "active": active if active in keys else "", "closed": closed}

    def save(self, session: str, tabs: object, active: object, closed: object = None) -> dict[str, Any]:
        if not session:
            raise ValueError("a session name is required")
        if not isinstance(tabs, list):
            raise ValueError("tabs must be a list")
        kept = [t for t in (_clean(x) for x in tabs) if t][:MAX_TABS]
        keys = {_key(t) for t in kept}
        with self._lock:
            sessions = self._read()
            previous = sessions.get(session) or {}
            # A page from before feature 171 sends no closed list: what is on record stays.
            wanted = previous.get("closed") if closed is None else closed
            entry = {"tabs": kept, "active": str(active or "") if str(active or "") in keys else "",
                     "closed": _closed(wanted, keys, self._times(previous), self._clock())}
            if kept or entry["closed"]:
                sessions[session] = entry
            else:
                sessions.pop(session, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"sessions": sessions}, indent=2, sort_keys=True), encoding="utf-8")
            fileio.replace(tmp, self.path)
        return entry
