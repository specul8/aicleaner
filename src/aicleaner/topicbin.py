"""D258 (Brad, 2026-11-19): a topic deleted from its page goes to a bin it can be restored from for 30 days.

"How do I delete a Topic from the AI cleaner system? It does not seem possible currently ... add in a bin icon on the
topic page (with confirmation modal) that deletes a topic."

What a delete does - and does not:

- The topic's file (`clients/<id>/<id>.json`, a flat `clients/<id>.json` from an earlier version, or its entry in a
  legacy `client-map.json`) and its last-good copy move to `<state_dir>/topic-bin/<id>.<yyyyMMdd-HHmmss>/`, with
  `meta.json` saying what it was, when, where it came from and which project sessions were unlinked. The state folder
  is this machine's own, never synced.
- The topic's folder in the clients location, and anything else in it, is left alone; so is its project assets folder
  in OneDrive. Its index stays allocated in `_registry.json`, so its tokens are never reused by another topic.
- Restoring puts the file back where a topic's file lives now (its own folder), refused while a topic with that id
  exists again.
- Entries older than KEEP_DAYS are removed on every list, delete and restore.

The project sessions that named the topic are the caller's to change (ui.py) - this module only moves files.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner.topicbin")

BIN_FOLDER = "topic-bin"
KEEP_DAYS = 30
META = "meta.json"


class TopicBinError(ValueError):
    """A delete or restore that cannot be done, said for the person who asked."""


def bin_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / BIN_FOLDER


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def purge(state_dir: str | Path, keep_days: int = KEEP_DAYS, now: float | None = None) -> list[str]:
    """Bin entries deleted more than `keep_days` ago, removed for good. Returns their names."""
    root = bin_dir(state_dir)
    if not root.is_dir():
        return []
    limit = (now if now is not None else time.time()) - keep_days * 86400
    gone: list[str] = []
    for entry in root.iterdir():
        meta = _meta(entry)
        if meta is None:
            continue
        if float(meta.get("deleted_at") or 0) < limit:
            shutil.rmtree(entry, ignore_errors=True)
            gone.append(entry.name)
            log.info("topic bin: %s removed for good (older than %d days)", entry.name, keep_days)
    return gone


def _meta(entry: Path) -> dict[str, Any] | None:
    try:
        meta = json.loads((entry / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def listing(state_dir: str | Path) -> list[dict[str, Any]]:
    """The bin, newest first: {entry, id, label, deleted_at, sessions, purge_at}."""
    purge(state_dir)
    root = bin_dir(state_dir)
    rows: list[dict[str, Any]] = []
    for entry in root.iterdir() if root.is_dir() else []:
        meta = _meta(entry)
        if meta is None:
            continue
        at = float(meta.get("deleted_at") or 0)
        rows.append({"entry": entry.name, "id": meta.get("id", ""), "label": meta.get("label", ""),
                     "deleted_at": at, "sessions": list(meta.get("sessions") or []),
                     "purge_at": at + KEEP_DAYS * 86400})
    rows.sort(key=lambda row: row["deleted_at"], reverse=True)
    return rows


def delete(store: Any, cid: str, state_dir: str | Path, label: str = "", sessions: list[str] | None = None) -> dict:
    """Move topic `cid`'s file (and its last-good copy) into the bin. `store` is the MappingStore. Returns the meta
    written. TopicBinError for Global or an unknown topic; the file system's own errors pass through."""
    from .mapping import MappingError

    if str(cid).strip().lower() == "global":
        raise TopicBinError("Global is not a topic that can be deleted - it holds your own names for every topic")
    try:
        path, doc, client = store._client_doc(cid)
    except MappingError as exc:
        raise TopicBinError(str(exc)) from exc
    purge(state_dir)
    target = bin_dir(state_dir) / f"{cid}.{_stamp()}"
    n = 1
    while target.exists():
        n += 1
        target = bin_dir(state_dir) / f"{cid}.{_stamp()}-{n}"
    target.mkdir(parents=True)
    legacy = path == store.mapping_path
    if legacy:
        (target / f"{cid}.json").write_text(json.dumps(client, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        doc["clients"] = [c for c in doc.get("clients", []) if c.get("id") != cid]
        from .store import _write
        _write(store.mapping_path, doc)
    else:
        shutil.move(str(path), str(target / f"{cid}.json"))
    good = store.lastgood_path(cid) if getattr(store, "state_dir", None) else None
    if good is not None and good.is_file():
        shutil.move(str(good), str(target / "lastgood.json"))
    meta = {"id": cid, "label": label or cid, "deleted_at": time.time(),
            "from": "client-map.json" if legacy else str(path.relative_to(store.clients_dir))
            if path.is_relative_to(store.clients_dir) else str(path),
            "sessions": list(sessions or [])}
    (target / META).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("topic %s deleted to the bin (%s); %d project session(s) unlinked", cid, target.name, len(meta["sessions"]))
    return {**meta, "entry": target.name}


def restore(store: Any, state_dir: str | Path, entry: str) -> dict:
    """Put a deleted topic back. Returns its meta (with the sessions it had unlinked). TopicBinError when the entry is
    not in the bin, or a topic with the same id exists again."""
    from .mapping import MappingError

    purge(state_dir)
    name = Path(str(entry)).name                    # a bin entry's own name, never a path out of it
    folder = bin_dir(state_dir) / name
    meta = _meta(folder) if name and folder.is_dir() else None
    if meta is None:
        raise TopicBinError(f"{entry} is not in Recently deleted (it may be older than {KEEP_DAYS} days)")
    cid = str(meta.get("id") or "")
    try:
        store._client_doc(cid)
    except MappingError:
        pass
    else:
        raise TopicBinError(f"a topic called {cid} exists again - rename or delete it before restoring this one")
    target = store.clients_dir / cid / f"{cid}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder / f"{cid}.json"), str(target))
    good = store.lastgood_path(cid) if getattr(store, "state_dir", None) else None
    if good is not None and (folder / "lastgood.json").is_file() and not good.exists():
        good.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder / "lastgood.json"), str(good))
    shutil.rmtree(folder, ignore_errors=True)
    log.info("topic %s restored from the bin (%s)", cid, name)
    return meta
