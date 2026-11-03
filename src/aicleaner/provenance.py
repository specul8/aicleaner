"""Where each mapped entry came from, in detail, and never off this machine (feature 30).

A client file says *what* was mapped and, since v0.9.57, *how* it arrived - "Repo scan", "Manual
entry", "Auto-add". That much travels: the file is published per audience and dropped in a share,
and a colleague reading it should be able to tell a scanned entry from a typed one.

The rest stays here. Which repository path a value was found in, which project folders and
sessions it appeared in, how many times and between which dates - that is the evidence, and it is
also exactly the material that should not be written into a file somebody else receives. The
machine is the evidence collection zone; the shared file is not.

Kept beside the other local state (`lastgood/`, `broken/`), never in the clients folder, which
may be a synced share. One JSON file per client, so a write touches only the client it concerns
and a corrupt one costs one client's provenance rather than all of it.

Losing this file loses evidence and nothing else: no mapping, no token and no decision depends on
it, which is why every read here returns a default rather than raising.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from . import stamp

log = logging.getLogger("aicleaner.provenance")

# Bounds, because this is fed by traffic and by scans of whole repositories.
MAX_PATHS = 20
MAX_NOTES = 10


def _clean(values: Any, limit: int) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for v in values:
        text = str(v).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


class Provenance:
    """Per-client detail about where each entry came from. Local only."""

    def __init__(self, state_dir: Path | None):
        self.dir = Path(state_dir) / "provenance" if state_dir else None
        self._lock = threading.Lock()

    def _path(self, cid: str) -> Path | None:
        return self.dir / f"{cid}.json" if self.dir else None

    def _read(self, cid: str) -> dict[str, Any]:
        p = self._path(cid)
        if p is None or not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("provenance for %s unreadable: %s", cid, exc)
            return {}
        return data.get("entries", {}) if isinstance(data, dict) else {}

    def _write(self, cid: str, entries: dict[str, Any]) -> None:
        p = self._path(cid)
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"client": cid, "entries": entries}, indent=2, ensure_ascii=False),
                         encoding="utf-8")
        except OSError as exc:      # evidence is worth keeping, never worth failing a write for
            log.warning("could not record provenance for %s: %s", cid, exc)

    # ---- writing -----------------------------------------------------------------------

    def record(self, cid: str, real: str, source: str, *, paths: Any = (), projects: Any = (),
               sessions: Any = (), seen_with: dict[str, int] | None = None, count: int = 0,
               first_seen: str = "", last_seen: str = "", confidence: str = "", kind: str = "",
               near: str = "", note: str = "") -> None:
        """Record how one entry arrived. Merges with what is already known about it.

        Called on every path that creates a form, so an entry never exists without at least the
        date it appeared and what put it there.
        """
        if not real:
            return
        with self._lock:
            entries = self._read(cid)
            was = entries.get(real) or {}
            now = stamp.now()
            entry: dict[str, Any] = {
                "source": source or was.get("source", ""),
                "added": was.get("added") or now,
                "by": was.get("by") or stamp.machine(),
                "last": now,
            }
            for key, value in (("kind", kind), ("confidence", confidence), ("near", near),
                               ("first_seen", first_seen), ("last_seen", last_seen)):
                if value or was.get(key):
                    entry[key] = value or was.get(key, "")
            if count or was.get("count"):
                entry["count"] = max(int(count or 0), int(was.get("count") or 0))
            merged_paths = _clean(list(was.get("paths") or []) + _clean(paths, MAX_PATHS), MAX_PATHS)
            if merged_paths:
                entry["paths"] = merged_paths
            for key, value in (("projects", projects), ("sessions", sessions)):
                merged = _clean(list(was.get(key) or []) + _clean(value, MAX_PATHS), MAX_PATHS)
                if merged:
                    entry[key] = merged
            if seen_with or was.get("seen_with"):
                combined = dict(was.get("seen_with") or {})
                for k, v in (seen_with or {}).items():
                    combined[k] = max(int(v), int(combined.get(k, 0)))
                entry["seen_with"] = combined
            notes = _clean(list(was.get("notes") or []) + ([note] if note else []), MAX_NOTES)
            if notes:
                entry["notes"] = notes
            entries[real] = entry
            self._write(cid, entries)

    def record_many(self, cid: str, reals: list[str], source: str, **kw: Any) -> None:
        """The same, for a scan's worth of entries, in one read and one write."""
        for real in reals:
            self.record(cid, real, source, **kw)

    def forget(self, cid: str, real: str) -> None:
        with self._lock:
            entries = self._read(cid)
            if entries.pop(real, None) is not None:
                self._write(cid, entries)

    def rename(self, cid: str, real: str, target: str) -> None:
        if real == target:
            return
        with self._lock:
            entries = self._read(cid)
            if real in entries:
                entries[target] = entries.pop(real)
                self._write(cid, entries)

    # ---- reading -----------------------------------------------------------------------

    def of(self, cid: str, real: str) -> dict[str, Any]:
        return self._read(cid).get(real) or {}

    def all_for(self, cid: str) -> dict[str, Any]:
        return self._read(cid)
