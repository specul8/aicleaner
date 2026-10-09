"""Which folders are open in a session's file tree, per client (D73).

Expand or collapse a folder, and the next session for the same client opens the same way - as VS
Code keeps a workspace's tree. Nothing saved means every root starts collapsed (D70).

Kept beside the profiles on this machine, not in the shared client file: it is how this person
last looked at the folders, not a fact about the engagement. A key is `<root>|<relative path>`,
the same key the page's tree uses, with an empty relative path for the root itself.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import fileio

TREE_STATE_FILE = "tree-state.json"
# Fed by a page, so bounded: a tree with this many folders open is a fault, not a view.
MAX_KEYS = 500
MAX_KEY_LEN = 1024


class TreeState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            clients = data.get("clients", {}) if isinstance(data, dict) else {}
            return {str(k): [str(x) for x in v] for k, v in clients.items() if isinstance(v, list)}
        except (OSError, ValueError, AttributeError):
            return {}    # missing or unreadable: the memory is lost, the page is not

    def load(self, client: str) -> list[str]:
        with self._lock:
            return self._read().get(client, [])

    def save(self, client: str, open_keys: object) -> list[str]:
        if not client:
            raise ValueError("a client is required")
        if not isinstance(open_keys, list) or not all(isinstance(k, str) for k in open_keys):
            raise ValueError("open must be a list of folder keys")
        keys = list(dict.fromkeys(k for k in open_keys if k and len(k) <= MAX_KEY_LEN))[:MAX_KEYS]
        with self._lock:
            clients = self._read()
            clients[client] = keys
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"clients": clients}, indent=2, sort_keys=True), encoding="utf-8")
            fileio.replace(tmp, self.path)
        return keys
