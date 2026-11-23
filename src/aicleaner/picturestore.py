"""D261 (Brad, 2026-11-20): what the picture reader said, kept on disk, so a picture is read again only when it must be.

"The system should only re-redact previously redacted images if the reals have changed since the redaction was
applied... keep a redacted copy of each image in the cache with a date and time stamp in the file name of when it was
first sent to the AI service so you can tell which image redactions are stale."

What is kept is what the reader (Read-ImageText.ps1, a PowerShell process and an OCR pass, 2-6 s each) said - never a
decision:

    read   the lines and boxes read from some picture bytes (an original, or a redacted copy)
    fill   the copy of a picture with given boxes filled, at a given size cap (or that no copy fitted the cap)
    text   the text read from a picture for the text that goes in its place

Each is a pure function of the bytes (and the boxes and cap), and of the reader: the key carries the reader's
fingerprint (the script's hash and the OCR engines found), so a new reader starts afresh. The decision - which boxes,
and whether anything is still legible in the copy - is made again by `picture.redact_picture` against the terms of the
moment whenever the in-memory outcome is not for this mapping. So when the terms change, a picture whose boxes are the
same costs no reader run (its copy and the copy's reading are here), and only a picture whose boxes changed - a new or
changed term on it - is filled and read again: stale means "the boxes worked out now are not the ones filled", decided
per picture by its own text. Nothing stale is ever sent: a copy is used only for exactly the boxes worked out now, and
it passes the legibility check against the current terms first, as a fresh one does.

Stored under AI Cleaner's state folder (`<state>\\pictures`, never OneDrive), one file per entry, named
`<yyyyMMdd-HHmmss>_<kind>_<key>.dpapi` - the time it was first stored, i.e. first sent. The content is client data
(the text read from a picture includes the names it holds; a copy is client imagery with the names blacked out):
on Windows it is encrypted with DPAPI for the current user, so the file is useless to another account or machine;
elsewhere it is a 0600 file. A file that cannot be read or decrypted is a miss, deleted, and the reader asked again.
Bounded: entries not used for RETENTION_DAYS are deleted, and the least recently used go first beyond MAX_BYTES
(a hit refreshes the file's modified time).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner")

RETENTION_DAYS = 14
MAX_BYTES = 256_000_000
PRUNE_EVERY = 50                 # stores between prunes (and one when the store opens)
SUFFIX = ".dpapi"
KINDS = ("read", "fill", "text")


def _protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        from .secrets import _dpapi
        return _dpapi(data, protect=True)
    return data


def _unprotect(data: bytes) -> bytes:
    if sys.platform == "win32":
        from .secrets import _dpapi
        return _dpapi(data, protect=False)
    return data


def reader_print(*parts: Any) -> str:
    """A reader's fingerprint: what decides what it says (the script's bytes, the engines found)."""
    return hashlib.sha256(repr(parts).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def key_of(*parts: Any) -> str:
    """An entry's key: the hash of what it is about (bytes as their SHA-256, then boxes, cap, media, reader)."""
    h = hashlib.sha256()
    for part in parts:
        h.update(hashlib.sha256(part).digest() if isinstance(part, (bytes, bytearray)) else
                 json.dumps(part, sort_keys=True, default=str).encode("utf-8", "surrogatepass"))
        h.update(b"\x00")
    return h.hexdigest()


class PictureStore:
    """The entries on disk, with an index in memory. Thread-safe; every failure is a miss, never an error."""

    def __init__(self, folder: Path, max_bytes: int = MAX_BYTES, retention_days: float = RETENTION_DAYS) -> None:
        self.folder = Path(folder)
        self.max_bytes = max(1, int(max_bytes))
        self.retention_s = float(retention_days) * 86400
        self._lock = threading.Lock()
        self._index: dict[tuple[str, str], Path] | None = None
        self._puts = 0
        self.hits = 0
        self.misses = 0
        self.stored = 0

    # -- the index --

    def _load(self) -> dict[tuple[str, str], Path]:
        if self._index is None:
            index: dict[tuple[str, str], Path] = {}
            try:
                for path in self.folder.glob(f"*{SUFFIX}"):
                    parts = path.name[:-len(SUFFIX)].split("_")
                    if len(parts) == 3 and parts[1] in KINDS:
                        index[(parts[1], parts[2])] = path
            except OSError:
                pass
            self._index = index
            self._prune_locked()
        return self._index

    # -- entries --

    def get(self, kind: str, key: str) -> Any:
        """The entry's value (JSON), or None."""
        with self._lock:
            path = self._load().get((kind, key))
            if path is None:
                self.misses += 1
                return None
            try:
                value = json.loads(_unprotect(path.read_bytes()).decode("utf-8"))
                os.utime(path)                       # last used: what retention and the size bound go by
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                log.warning("a stored picture reading could not be read, so it is read again: %s", type(exc).__name__)
                self._index.pop((kind, key), None)   # type: ignore[union-attr]
                try:
                    path.unlink()
                except OSError:
                    pass
                self.misses += 1
                return None
            self.hits += 1
            return value

    def put(self, kind: str, key: str, value: Any) -> None:
        """Store an entry, named with the time it was first stored; a failure to write is logged and forgotten."""
        with self._lock:
            index = self._load()
            if (kind, key) in index:
                return
            name = f"{datetime.now():%Y%m%d-%H%M%S}_{kind}_{key}{SUFFIX}"
            path = self.folder / name
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                data = _protect(json.dumps(value).encode("utf-8"))
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_bytes(data)
                if sys.platform != "win32":
                    os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except OSError as exc:
                log.warning("a picture reading could not be stored: %s", exc)
                return
            index[(kind, key)] = path
            self.stored += 1
            self._puts += 1
            if self._puts % PRUNE_EVERY == 0:
                self._prune_locked()

    def prune(self) -> int:
        with self._lock:
            self._load()
            return self._prune_locked()

    def _prune_locked(self) -> int:
        """Delete entries not used within the retention, then the least recently used beyond the size bound."""
        index = self._index or {}
        now = time.time()
        found = []
        for ident, path in list(index.items()):
            try:
                st = path.stat()
            except OSError:
                index.pop(ident, None)
                continue
            found.append((st.st_mtime, st.st_size, ident, path))
        found.sort()
        total = sum(size for _m, size, _i, _p in found)
        gone = 0
        for mtime, size, ident, path in found:
            if now - mtime <= self.retention_s and total <= self.max_bytes:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            index.pop(ident, None)
            total -= size
            gone += 1
        return gone

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())


def store_folder(state_dir: Path | str | None) -> Path | None:
    """`<state>\\pictures`; under a test home (AICLEANER_CLIENT_HOME) when the state folder is the real default, so a
    test or a probe never writes the person's own."""
    if not state_dir:
        return None
    folder = Path(state_dir)
    test_home = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    if test_home:
        try:
            real = (Path.home() / ".aicleaner").resolve()
            if folder.resolve() == real or real in folder.resolve().parents:
                folder = Path(test_home) / ".aicleaner"
        except OSError:
            folder = Path(test_home) / ".aicleaner"
    return folder / "pictures"
