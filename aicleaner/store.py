"""Where the mapping lives on disk, and every way it is changed.

Two layouts, merged when both exist:

  clients/<ID>.json     one client per file: {"id", "index", "expand_case", "forms", "ignore"}
  clients/_registry.json {"indexes": {"4": "CLIENT04"}}  allocation record; duplicates refused
  client-map.json       legacy single file (still supported; `aicleaner split` migrates it)
  ignore-list.json      global ignores (next to client-map.json)
  _global.json          Global: the user's own filters (next to client-map.json, never in clients/)
  <state>/case-variants.json  case variants met on this machine (per user, not shared)

The client file is the unit of sharing: give a colleague CLIENT04.json and their proxy emits
the same tokens, with the engagement's own ignore list travelling along. Every write is
validated by building a Mapper from the merged result before anything is replaced.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fileio
from . import stamp
from .provenance import Provenance
from .mapping import (FOUND_ORIGINS, GLOBAL_ID, ORIGIN_SCAN, SAFE_TOKEN_RE, SHAPED_TOKEN_CEILING,
                      SOURCE_MANUAL, TOKEN_SHAPES, Mapper, MappingError, classify, client_index,
                      client_tokens, email_rule_for, email_rules_of, embedded_token, parse_match,
                      rule_email_token)

log = logging.getLogger("aicleaner")

# A default that no caller could pass by accident, so "do not touch this" and "set this to
# nothing" stay distinguishable.
_UNSET = object()

REGISTRY = "_registry.json"
GLOBAL_FILE = "_global.json"  # <app folder>/_global.json, id "global", index 0; per user, never shared
# Every client id becomes a file name, so it must never be able to leave clients/ (review finding 2).
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# OneDrive names a conflicting copy <stem>-<COMPUTERNAME>.json, optionally with -<n>.
CONFLICT_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,14}(?:-\d+)?$")


@dataclass
class Loaded:
    data: dict[str, Any]                 # {"version": 1, "clients": [...]} for Mapper.from_dict
    ignore: set[str] = field(default_factory=set)   # global + every client's ignore, lowercased
    sources: dict[str, Path] = field(default_factory=dict)  # client id -> file it came from


@dataclass
class ClientReport:
    """What happened to one client on a tolerant load (see MappingStore.load_tolerant)."""
    id: str
    file: str
    state: list[str] = field(default_factory=list)  # ok | fallback | broken, plus hand-edited, conflict-copy
    error: dict[str, Any] | None = None             # {"kind": "syntax"|"rule", "message", line/column or index/real}
    fallback_at: str | None = None
    stamp: dict[str, Any] | None = None
    conflicts: list[str] = field(default_factory=list)
    # A change waiting for review (DECISIONS 51): {"added": [...], "removed": [...], "why": ...}
    held: dict[str, Any] | None = None
    entries: int = 0
    label: str = ""  # display name; the id when the file has none

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise MappingError(f"{p.name} is not valid JSON: {exc}") from exc


def _parse(p: Path) -> tuple[Any, dict[str, Any] | None]:
    """Parse a JSON file; a failure comes back as a structured syntax error, never an exception."""
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")), None
    except json.JSONDecodeError as exc:
        return None, {"kind": "syntax", "message": f"{p.name} is not valid JSON: {exc.msg}",
                      "line": exc.lineno, "column": exc.colno}
    except OSError as exc:
        return None, {"kind": "syntax", "message": f"{p.name} cannot be read: {exc}"}


def _shared_index(clients: list[dict[str, Any]]) -> tuple[str, str, int] | None:
    """Two clients deriving tokens from the same index, or None."""
    seen: dict[int, str] = {}
    for c in clients:
        try:
            idx = client_index(c["id"], c.get("index"))
        except MappingError:
            continue  # legacy client with explicit tokens and no digits: allowed
        if idx in seen and seen[idx] != c["id"]:
            return seen[idx], c["id"], idx
        seen[idx] = c["id"]
    return None


def _combine_error(clients: list[dict[str, Any]]) -> str | None:
    """Why these clients cannot be loaded together, or None (the strict load's checks).

    Only a real clash counts. A shared index used to be refused on its own, and it is only a seed
    for deriving tokens that are then stored in the file - so three healthy clients whose tokens
    were all distinct could be refused because two ids happened to hash to the same number, one
    client went `broken`, and the proxy refuses model traffic for EVERY client while one is broken.
    A total outage over a number neither client was using.

    So the tokens decide, and the index is used to explain them: a duplicate between two clients
    that share an index is that index's doing, and saying so turns "change these tokens" into
    "renumber one client", which is one field and disturbs no token already written down.
    """
    try:
        # Validation only: the substitution tables are not needed to say whether these clients
        # load together, and compiling them was most of a write's cost (feature 57).
        Mapper.from_dict({"version": 1, "clients": clients}, build=False)
    except (MappingError, KeyError, TypeError) as exc:
        shared = _shared_index(clients)
        if shared:
            first, second, idx = shared
            return (f"{exc} - {first} and {second} both derive tokens from index {idx}. "
                    f'Give one of them a different "index" (their existing tokens are stored in '
                    f"the file and do not change), or set the clashing token by hand.")
        return str(exc)
    return None


def _check_label(label: Any) -> str:
    """Display names are plain text for people: up to 80 characters, no control characters."""
    label = str(label or "").strip()
    if len(label) > 80 or any(ord(ch) < 32 for ch in label):
        raise MappingError("a display name is up to 80 characters with no control characters")
    return label


def _safe_kind(real: str) -> str:
    """classify(), but a value it cannot read is simply unclassified. A prune must never be the
    thing that raises on a file that is otherwise working."""
    try:
        return classify(real)
    except (MappingError, ValueError, TypeError):
        return ""


def _client_shape_error(doc: Any) -> dict[str, Any] | None:
    """The structural checks that cost nothing: shape, id, label, each form's real.

    Split out from `_validate_client` because these are dictionary tests, while everything below
    them builds a Mapper. `load_tolerant` runs these per client and the expensive part once for
    the whole set.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("forms", []), list):
        return {"kind": "rule", "message": "not a client document: needs an object with a forms list"}
    schema = doc.get("schema", 1)
    if not isinstance(schema, int) or schema > SCHEMA:
        # First, before anything else is judged: a newer file may hold what this build cannot read,
        # and that is a reason to update, not evidence the file is damaged (F14).
        return {"kind": "newer", "message": f"written by a newer aicleaner (schema {schema}, this one reads "
                                            f"{SCHEMA}): update aicleaner to use it"}
    cid = str(doc.get("id", ""))
    if cid != GLOBAL_ID and not CLIENT_ID_RE.match(cid):
        return {"kind": "rule", "message": f"client id {cid!r} is not valid"}
    if "label" in doc:
        try:
            _check_label(doc["label"])
        except MappingError as exc:
            return {"kind": "rule", "message": str(exc)}
    for i, f in enumerate(doc.get("forms", [])):
        if not isinstance(f, dict) or not isinstance(f.get("real"), str):
            return {"kind": "rule", "message": "each form needs a real string", "index": i}
        bad = shell_text(f["real"])
        if bad:
            return {"kind": "rule", "index": i, "real": f["real"],
                    "message": f"form {i} carries shell text ({bad}): a real is typed into the "
                               f"commands Claude runs, so it may not hold {SHELL_SHOWN}"}
    return None


# What a real may not hold (H19). Restoring a token puts its real into tool calls - `ls <token>` runs
# as `ls <real>` - so a real that is shell text runs on every PC that syncs the client's file.
# `&` is in some company names ("Smith & Jones"); it is refused all the same, and such a name is
# still caught by its other forms.
SHELL_CHARS = frozenset("`$;|&<>")
# The client-file schema this build writes and reads (F14). A file without the field is 1.
SCHEMA = 1
# How many forms a refused batch retries one by one (H13 side item; no cap on auto-add itself).
FALLBACK_CAP = 50
SHELL_SHOWN = "` $ ; | & < > or control characters"


def shell_text(real: str) -> str:
    """The first character that makes this real unsafe in a command, described; "" when there is none."""
    for ch in real:
        if ch in SHELL_CHARS:
            return repr(ch)
        if ord(ch) < 32 or ord(ch) == 127:
            return f"control character U+{ord(ch):04X}"
    return ""


def _validate_client(doc: Any) -> dict[str, Any] | None:
    """A structured reason this client document cannot be used on its own, or None. For a bad
    form the error names its index and real, so the UI can highlight the row."""
    shape = _client_shape_error(doc)
    if shape is not None:
        return shape
    forms = doc.get("forms", [])
    whole = _combine_error([doc])
    if whole is None:
        return None
    # Which form breaks it: the first prefix that fails. A prefix that fails stays failed as forms
    # are added, so a binary search finds it. The old walk built the whole mapper once per form -
    # 378 builds, 12 seconds, for one rejected auto-add to a large client, with the proxy frozen.
    lo, hi, why_at = 0, len(forms) - 1, whole
    while lo < hi:
        mid = (lo + hi) // 2
        why = _combine_error([{**doc, "forms": forms[: mid + 1]}])
        if why is None:
            lo = mid + 1
        else:
            hi, why_at = mid, why
    if lo < len(forms):
        why = _combine_error([{**doc, "forms": forms[: lo + 1]}]) or why_at
        return {"kind": "rule", "message": why, "index": lo, "real": forms[lo].get("real")}
    return {"kind": "rule", "message": whole}


def _write(p: Path, obj: Any, staging: Path | None = None) -> None:
    """Write the document atomically, staging the temp file outside the destination where asked.

    The clients folder is usually a OneDrive share. A temp file created there is picked up by the
    sync client the moment it appears, so a client file rewritten once per added form handed
    OneDrive hundreds of versions of a half-written document - which is how the application came to
    be refusing its own writes. Staged beside the app instead, the synced folder only ever sees a
    finished file arrive, in one move.

    `replace` is atomic within a volume and raises across one, so a staging folder on another drive
    falls back to the old behaviour rather than failing the write.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    if staging is not None:
        tmp = staging / f"{p.name}.{os.getpid()}.tmp"
        try:
            staging.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            fileio.replace(tmp, p)
            return
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    fileio.replace(tmp, p)


def _importable(doc: dict[str, Any], cid: str, idx: int) -> dict[str, Any]:
    """An imported client document as it will be written: every field kept, the stamp dropped (a
    new one is applied on write), and forms and ignores cleaned to their proper types."""
    entry = {k: v for k, v in doc.items() if k != stamp.KEY}
    entry.update({"id": cid, "index": idx, "expand_case": bool(doc.get("expand_case", True))})
    entry["forms"] = [{**{k: v for k, v in f.items()}, "real": str(f["real"]),
                       **({"token": str(f["token"])} if f.get("token") else {})}
                      for f in doc.get("forms", []) if isinstance(f, dict) and f.get("real")]
    entry["ignore"] = [str(v) for v in doc.get("ignore", []) or []]
    return entry


def _merged(local: dict[str, Any], theirs: dict[str, Any]) -> tuple[dict[str, Any], int, list[str]]:
    """Merge an imported client into the local one: (entry, forms added, values whose token differs).

    The local copy wins wherever both say something: its fields, its forms, its tokens. What the
    import adds is what the local copy does not have - new forms, new ignores, and fields a local
    form or the client lacks (a match rule, an origin, the primary repo). A different token for the
    same value is never taken: tokens already sent to the provider must keep rehydrating."""
    entry = {**theirs, **local}
    forms = [dict(f) for f in local.get("forms", []) or []]
    by_real = {f.get("real"): f for f in forms}
    added, conflicts = 0, []
    for f in theirs.get("forms", []):
        mine = by_real.get(f["real"])
        if mine is None:
            forms.append(dict(f))
            by_real[f["real"]] = forms[-1]
            added += 1
            continue
        if f.get("token") and mine.get("token") and f["token"] != mine["token"]:
            conflicts.append(f["real"])
        for key, value in f.items():
            mine.setdefault(key, value)
    entry["forms"] = forms
    entry["ignore"] = list(dict.fromkeys([*(local.get("ignore") or []), *(theirs.get("ignore") or [])]))
    return entry, added, conflicts


def _link_folder(link: Path, target: Path) -> None:
    """`link` made to show `target`'s contents (a joined shared client, DECISIONS 67). On Windows a
    directory junction - no elevation, and it follows a OneDrive folder like any other; elsewhere a
    symbolic link."""
    if os.name == "nt":
        import subprocess

        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if made.returncode != 0 or not link.exists():
            raise MappingError(f"could not link {link} to {target}: {(made.stderr or made.stdout).strip()}")
    else:
        link.symlink_to(target, target_is_directory=True)


def _write_client(p: Path, doc: dict[str, Any], staging: Path | None = None, key: str | None = None) -> None:
    """Client files carry an integrity stamp, so edits made outside aicleaner are detectable, and
    the schema they were written in (F14). With the client's key the stamp is signed (DECISIONS 51)."""
    doc["schema"] = SCHEMA
    if key:
        _write(p, stamp.apply(doc, key=key), staging)
        return
    _write(p, stamp.apply(doc), staging)


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at] in " \t\r\n":
        at += 1
    return at


def _recover_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object in `text` that stands up on its own, in order (feature 13).

    `json.loads` needs the whole document to be valid. `raw_decode` needs only a valid value at
    the one position it is handed, so walking the text and trying it at each `{` reads the
    objects on either side of the damage without ever having to understand what the damage was -
    a stray comma, a truncated write, a merge marker. The outer object fails (that is why we are
    here) and the entries inside it do not.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    at = text.find("{")
    while at != -1:
        try:
            obj, end = decoder.raw_decode(text, at)
        except ValueError:
            at = text.find("{", at + 1)      # damage starts inside this one; step over it
            continue
        if isinstance(obj, dict):
            found.append(obj)
            at = text.find("{", end)         # skip what was consumed, so nesting is not read twice
        else:
            at = text.find("{", at + 1)
    return found


def _recover_value(text: str, key: str) -> Any:
    """The first value for `key` that decodes on its own, or None.

    The top-level keys of a broken file - label, index, ignore - are never reached by
    `_recover_objects`, because the object holding them is the one that will not parse.
    """
    decoder = json.JSONDecoder()
    needle = f'"{key}"'
    at = text.find(needle)
    while at != -1:
        colon = text.find(":", at + len(needle))
        if colon != -1:
            try:
                return decoder.raw_decode(text, _skip_space(text, colon + 1))[0]
            except ValueError:
                pass
        at = text.find(needle, at + 1)
    return None


class MappingStore:
    def __init__(self, mapping_path: Path, clients_dir: Path | None = None, state_dir: Path | None = None,
                 global_path: Path | None = None, key_dir: Path | None = None,
                 conflicts_dir: Path | None = None):
        self.state_dir = Path(state_dir) if state_dir else None
        # Client keys and moved-aside conflict copies (DECISIONS 51): local, never the clients folder.
        from .keys import KeyStore
        keys_at = Path(key_dir) if key_dir else (self.state_dir / "keys" if self.state_dir else None)
        self.keys = KeyStore(keys_at) if keys_at else None
        self.conflicts_dir = (Path(conflicts_dir) if conflicts_dir
                              else (self.state_dir / "conflicts" if self.state_dir else None))
        # Last-good copies are also kept in memory, so a store without a state_dir (tests, the
        # legacy single-file layout) still survives a bad edit for the life of the process.
        self._memory_good: dict[str, dict[str, Any]] = {}
        self._memory_source: dict[str, str] = {}
        self.mapping_path = Path(mapping_path)
        self.clients_dir = Path(clients_dir) if clients_dir else self.mapping_path.parent / "clients"
        self.global_ignore_path = self.mapping_path.parent / "ignore-list.json"
        # Global is the user's own (their employer, vendors they use): it lives with the install,
        # never in the clients folder, which may be shared with colleagues.
        self.global_path = Path(global_path) if global_path else self.mapping_path.parent / GLOBAL_FILE
        # Temp files for client writes are built here, beside the app, and moved into the clients
        # folder finished. The clients folder is usually synced; the app folder never is.
        self.staging_dir = self.mapping_path.parent / ".staging"
        # Where each entry came from, in detail. Local state, never the clients folder: the
        # detail is evidence and the clients folder may be a share (feature 30).
        self.provenance = Provenance(self.state_dir)
        self.variants_path = (self.state_dir / "case-variants.json") if self.state_dir else None
        self._memory_variants: dict[str, dict[str, str]] = {}
        # Who adds a form, recorded on it (DECISIONS 67): a shared file is only as trusted as its
        # least careful editor. Set by the proxy from the local account; empty writes nothing.
        self.author = ""
        self.kept_path = (self.state_dir / "kept-additions.json") if self.state_dir else None
        self._memory_kept: list[list[str]] = []

    # ---- reading --------------------------------------------------------------

    def client_files(self) -> list[Path]:
        """Global (when it exists) first, then every client file: each client's own folder
        (`<clients>/<id>/`, DECISIONS 51) and, until they are moved, files written flat by earlier
        versions. A folder's other .json files are listed too - they are OneDrive's conflict
        copies, and the loader tells them apart."""
        self._adopt_legacy_global()
        files = [self.global_path] if self.global_path.is_file() else []
        if not self.clients_dir.is_dir():
            return files
        flat = sorted(p for p in self.clients_dir.glob("*.json") if p.name not in (REGISTRY, GLOBAL_FILE))
        foldered: list[Path] = []
        for folder in sorted(p for p in self.clients_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            if (folder / f"{folder.name}.json").is_file():
                foldered += sorted(folder.glob("*.json"))
        # A client both flat and in its folder (a colleague's shared folder synced before this PC
        # moved its own): the folder is the shared copy and wins; the flat one is left for the move
        # to report, never loaded as a second client.
        held = {p.parent.name for p in foldered}
        return files + foldered + [p for p in flat if p.stem not in held]

    def move_into_folders(self, backups: Path) -> list[str]:
        """Move each flat client file into its own folder (DECISIONS 51), after backing up every
        one it moves. Returns the ids moved. A client whose folder already holds a file is left
        as it is: that folder is the shared copy, and overwriting it could undo a colleague's work."""
        if not self.clients_dir.is_dir():
            return []
        flat = [p for p in sorted(self.clients_dir.glob("*.json"))
                if p.name not in (REGISTRY, GLOBAL_FILE) and CLIENT_ID_RE.match(p.stem)
                and not self.is_conflict_copy(p, {q.stem for q in self.clients_dir.glob("*.json")} - {p.stem})]
        todo = [p for p in flat if not (self.clients_dir / p.stem / p.name).exists()]
        for p in flat:
            if p not in todo:
                log.warning("%s is in its folder already; the flat %s is left in place - compare and delete it",
                            p.stem, p.name)
        if not todo:
            return []
        backup = Path(backups) / f"folders-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for p in todo:
            shutil.copy2(p, backup / p.name)
            target = self.clients_dir / p.stem / p.name
            target.parent.mkdir(exist_ok=True)
            fileio.replace(p, target)
            moved.append(p.stem)
        log.info("moved %d client file(s) into their own folders; backup at %s", len(moved), backup)
        return moved

    def _adopt_legacy_global(self) -> None:
        """Earlier versions kept Global in clients/. Move it next to the config once; when both
        exist the clients-folder copy is ignored (client_files never lists it)."""
        legacy = self.clients_dir / GLOBAL_FILE
        if legacy == self.global_path or self.global_path.exists() or not legacy.is_file():
            return
        self.global_path.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(self.global_path)

    def _is_client_path(self, path: Path) -> bool:
        """True for a one-client file (clients/<ID>/<ID>.json, a flat clients/<ID>.json, or
        Global), false for client-map.json."""
        return (path.parent == self.clients_dir or path.parent.parent == self.clients_dir
                or path == self.global_path)

    @staticmethod
    def _check_id(cid: str) -> str:
        """Refuse any id that is not a plain file-name-safe token; returns the stripped id."""
        cid = str(cid).strip()
        if cid != GLOBAL_ID and not CLIENT_ID_RE.match(cid):
            raise MappingError(f"client id {cid!r} must be 1-64 letters, digits, '-' or '_', "
                               "starting with a letter or digit")
        return cid

    def _file_for(self, cid: str) -> Path:
        """Where this client's file is: its own folder (DECISIONS 51), or the flat file an earlier
        version wrote while that is still where it is."""
        cid = self._check_id(cid)
        if cid == GLOBAL_ID:
            return self.global_path
        flat = self.clients_dir / f"{cid}.json"
        foldered = self.clients_dir / cid / f"{cid}.json"
        return flat if flat.is_file() and not foldered.is_file() else foldered

    def ensure_global(self) -> Path:
        """Create Global (<app folder>/_global.json, id "global", index 0) if it does not exist."""
        p = self._file_for(GLOBAL_ID)
        self._adopt_legacy_global()
        if not p.exists():
            self.clients_dir.mkdir(parents=True, exist_ok=True)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._register(GLOBAL_ID, 0)
            _write_client(p, {"id": GLOBAL_ID, "index": 0, "expand_case": True, "forms": [], "ignore": [],
                       "_comment": "Global: your own filters (your employer, vendors you use). Applies to every client on this machine and is never shared. Tokens are Org00...; ignores here apply everywhere."})
        return p

    def signature(self) -> tuple:
        """Changes whenever any mapping input changes; used for hot reload."""
        parts: list[tuple] = []
        inputs = [self.mapping_path, self.global_ignore_path, *self.client_files()]
        if self.variants_path is not None:
            inputs.append(self.variants_path)
        for p in inputs:
            try:
                parts.append((p.name, p.stat().st_mtime_ns))
            except FileNotFoundError:
                parts.append((p.name, None))
        return tuple(parts)

    def load(self) -> Loaded:
        clients: list[dict[str, Any]] = []
        ignore: set[str] = set()
        sources: dict[str, Path] = {}
        if self.mapping_path.exists():
            legacy = _read(self.mapping_path)
            if legacy.get("version") != 1:
                raise MappingError(f"{self.mapping_path.name}: mapping version must be 1")
            for c in legacy.get("clients", []):
                clients.append(c)
                sources[c.get("id", "")] = self.mapping_path
        for p in self.client_files():
            c = _read(p)
            cid = c.get("id") or p.stem
            c["id"] = cid
            if cid in sources:
                raise MappingError(f"client {cid} defined in both {sources[cid].name} and {p.name}")
            clients.append(c)
            sources[cid] = p
            ignore.update(str(v).lower() for v in c.get("ignore", []) or [])
        if not clients and not self.mapping_path.exists() and not self.clients_dir.is_dir():
            raise MappingError(f"no mapping: neither {self.mapping_path} nor {self.clients_dir}/ exists")
        # Index uniqueness is NOT checked here. The index only seeds the derivation of tokens that
        # are then written into the file, so two clients sharing one is harmless until it actually
        # produces the same token twice - which Mapper's own duplicate-token check catches, with
        # _combine_error naming the index as the cause. Refusing on the index alone took the whole
        # filter down over a number neither client was using. add_client still allocates through
        # _registry.json, so the common case never gets here.
        if self.global_ignore_path.exists():
            ignore.update(str(v).lower() for v in _read(self.global_ignore_path).get("ignore", []))
        return Loaded({"version": 1, "clients": self._with_variants(clients)}, ignore, sources)

    def mapper(self) -> Mapper:
        loaded = self.load()
        try:
            m = Mapper.from_dict(loaded.data)
        except MappingError as exc:
            # Same explanation the tolerant path gives: a duplicate token between two clients that
            # share a derivation index is that index's doing, and `aicleaner check` is exactly
            # where somebody is trying to find out why.
            raise MappingError(_combine_error(loaded.data["clients"]) or str(exc)) from None
        m.ignore = loaded.ignore
        m.email_rules = email_rules_of(loaded.data.get("clients", []))
        return m

    # ---- case variants (per user) -----------------------------------------------------------

    def variants(self) -> dict[str, dict[str, str]]:
        """{client id: {variant: token}} for case variants met on this machine.

        They are kept beside the last-good copies, never in the shared client file: two people
        meeting the same variant would both write to it and OneDrive would make conflict copies.
        The token is derived deterministically, so every machine ends up with the same one anyway.
        """
        if self.variants_path is None or not self.variants_path.exists():
            return {cid: dict(m) for cid, m in self._memory_variants.items()}
        doc, err = _parse(self.variants_path)
        if err is not None or not isinstance(doc, dict):
            return {cid: dict(m) for cid, m in self._memory_variants.items()}
        return {str(cid): {str(k): str(v) for k, v in (m or {}).items() if isinstance(m, dict)}
                for cid, m in doc.items()}

    def record_variant(self, client: str, variant: str, token: str) -> None:
        """Remember one case variant; called by the mapper the first time it meets it."""
        if self.variants_path is None:
            self._memory_variants.setdefault(client, {})[variant] = token
            return
        doc = self.variants()  # from disk, before the in-memory copy is touched
        self._memory_variants.setdefault(client, {})[variant] = token
        if doc.get(client, {}).get(variant) == token:
            return
        doc.setdefault(client, {})[variant] = token
        self.variants_path.parent.mkdir(parents=True, exist_ok=True)
        _write(self.variants_path, {cid: dict(sorted(m.items())) for cid, m in sorted(doc.items())})

    def _with_variants(self, clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add each client's learned case variants as ordinary forms with their stored token."""
        learned = self.variants()
        if not learned:
            return clients
        for client in clients:
            extra = learned.get(str(client.get("id", "")))
            if not extra:
                continue
            forms = client.get("forms") or []
            have = {str(f.get("real")) for f in forms if isinstance(f, dict)}
            primaries = {str(f.get("real", "")).lower() for f in forms if isinstance(f, dict)}
            client["forms"] = forms + [{"real": real, "token": token} for real, token in extra.items()
                                       if real not in have and real.lower() in primaries]
        return clients

    # ---- tolerant loading (runtime) -------------------------------------------------

    def _file_id(self, p: Path) -> str:
        return GLOBAL_ID if p == self.global_path else p.stem

    @staticmethod
    def is_conflict_copy(p: Path, stems: set[str]) -> bool:
        """True for OneDrive's <stem>-<COMPUTERNAME>[-<n>].json copy of an existing <stem>.json."""
        if p.stem in stems:
            return False
        return any(p.stem.startswith(stem + "-") and CONFLICT_SUFFIX_RE.match(p.stem[len(stem) + 1:])
                   for stem in stems)

    def lastgood_path(self, cid: str) -> Path | None:
        return self.state_dir / "lastgood" / f"{cid}.json" if self.state_dir else None

    def _lastgood(self, cid: str) -> tuple[dict[str, Any] | None, str | None]:
        """(last-good document, when it was written) from disk, else from memory."""
        p = self.lastgood_path(cid)
        if p is not None and p.exists():
            doc, err = _parse(p)
            if err is None and _validate_client(doc) is None:
                st = doc.get("_stamp") if isinstance(doc.get("_stamp"), dict) else {}
                at = st.get("at") or datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return doc, at
        doc = self._memory_good.get(cid)
        if doc is None:
            return None, None
        st = doc.get("_stamp") if isinstance(doc.get("_stamp"), dict) else {}
        return doc, st.get("at")

    def _record_good(self, cid: str, doc: dict[str, Any], source: Path) -> None:
        self._memory_good[cid] = json.loads(json.dumps(doc))
        self._memory_source[cid] = source.name
        p = self.lastgood_path(cid)
        if p is None:
            return
        if p.exists():
            old, err = _parse(p)
            if err is None and isinstance(old, dict) and stamp.canonical(old) == stamp.canonical(doc):
                return
        _write(p, doc)

    def load_tolerant(self, record: bool = True) -> tuple[Loaded, dict[str, ClientReport]]:
        """Load every client that can be used; never raises for a bad client file.

        Each client file is validated on its own, then against the clients already accepted. A
        file that fails falls back to its last-good copy (only that client degrades); with no copy
        it is `broken` and left out, and the proxy refuses model traffic until it is fixed. OneDrive
        conflict copies and duplicate ids are never loaded as clients. `record` stores last-good
        copies of files that loaded cleanly: the running proxy records, write validation does not.
        """
        reports: dict[str, ClientReport] = {}
        if record:
            # Only the real load writes: validation calls this too, and must not move files about.
            try:
                self._merge_conflict_copies()
            except OSError as exc:
                log.warning("could not merge conflict copies: %s", exc)
        candidates: list[tuple[str, dict[str, Any] | None, dict[str, Any] | None, Path]] = []
        if self.mapping_path.exists():
            legacy, err = _parse(self.mapping_path)
            if err is None and (not isinstance(legacy, dict) or legacy.get("version") != 1):
                err = {"kind": "rule", "message": f"{self.mapping_path.name}: mapping version must be 1"}
            if err is None:
                for c in legacy.get("clients", []):
                    candidates.append((str(c.get("id", "")), c, None, self.mapping_path))
            else:
                ids = [cid for cid, src in self._memory_source.items() if src == self.mapping_path.name]
                for cid in ids:
                    candidates.append((cid, None, err, self.mapping_path))
                if not ids:
                    reports[self.mapping_path.name] = ClientReport(self.mapping_path.name, self.mapping_path.name,
                                                                   ["broken"], err)
        files = self.client_files()
        stems = {f.stem for f in files}
        conflicts: dict[str, list[str]] = {}
        taken = {c[0] for c in candidates}
        for f in files:
            fid = self._file_id(f)
            doc, err = _parse(f)
            owner = None
            if isinstance(doc, dict):
                cid = str(doc.get("id") or fid)
                owner_stem = "_global" if cid == GLOBAL_ID else cid
                if cid != fid and owner_stem in stems:
                    owner = cid
                elif cid in taken:
                    owner = cid  # same id as a client in client-map.json
            elif self.is_conflict_copy(f, stems - {f.stem}):
                stem = next(st for st in sorted(stems, key=len, reverse=True)
                            if st != f.stem and f.stem.startswith(st + "-"))
                owner = GLOBAL_ID if stem == "_global" else stem
            if owner is not None:
                conflicts.setdefault(owner, []).append(f.name)
                continue
            if isinstance(doc, dict):
                doc = {**doc, "id": str(doc.get("id") or fid)}
                cid = doc["id"]
            else:
                cid, doc = fid, None
            taken.add(cid)
            candidates.append((cid, doc, err, f))

        # A client this PC has used whose file is no longer there (DECISIONS 67): a shared folder
        # unsynced, access removed, files not downloaded. It used to stop loading in silence, and its
        # names went out as written. Its last-good copy keeps it protected, and it is reported
        # `missing` - red on the status bar - until the file is back. aicleaner never deletes a
        # client, so a last-good copy with no file is always this.
        for good in sorted(self.state_dir.glob("lastgood/*.json")) if self.state_dir else []:
            cid = good.stem
            if cid in taken or cid == GLOBAL_ID or cid.startswith("_"):
                continue
            taken.add(cid)
            expected = self.clients_dir / cid / f"{cid}.json"
            candidates.append((cid, None, {"kind": "missing", "message": f"{expected} is missing - its last-good "
                                           "copy is in use. Check the folder is still shared with you, synced, and "
                                           "kept on this device."}, expected))

        used: list[dict[str, Any]] = []
        sources: dict[str, Path] = {}
        ordered = sorted(candidates, key=lambda c: (c[0] != GLOBAL_ID, c[0].lower()))

        # Everything below needs to know *which* client is at fault, and finding that out costs a
        # Mapper build per client and another per growing prefix - 2N builds, over sets that get
        # larger as it goes. Nothing is at fault in the ordinary case, so ask that question first:
        # cheap shape checks per client, then one combined build for the lot. When it holds there
        # is nobody to attribute and the per-client work is skipped. The order matters (Global
        # first, so its entries shadow), so the fast path uses the same order as the loop.
        healthy = [doc for _cid, doc, err, _path in ordered if doc is not None and err is None]
        all_clean = (all(_client_shape_error(d) is None for d in healthy)
                     and _combine_error(healthy) is None)

        for cid, doc, err, path in ordered:
            rep_ = ClientReport(cid, path.name)
            reports[cid] = rep_
            if doc is not None and err is None and not all_clean:
                err = _validate_client(doc)
            if doc is not None and err is None and not all_clean:
                why = _combine_error(used + [doc])
                if why:
                    err = {"kind": "rule", "message": why}
            rep_.label = str((doc or {}).get("label") or cid) if isinstance(doc, dict) else cid
            key = self._key(cid) if doc is not None and err is None and path != self.mapping_path else None
            if key is not None:
                verdict = stamp.verify(doc, key)
                if verdict == "unsigned" and record:
                    self._sign_if_trusted(cid, key)          # adopt matches (DECISIONS 51)
                    signed_now, _e = _parse(path)
                    if isinstance(signed_now, dict) and stamp.verify(signed_now, key) == "signed":
                        doc, verdict = {**signed_now, "id": cid}, "signed"
                if verdict != "signed":
                    # Changed without this client's key: held for review, last-good used meanwhile.
                    good, at = self._lastgood(cid)
                    rep_.held = self._held_diff(doc, good, verdict)
                    rep_.state.append("held")
                    if good is not None and _combine_error(used + [good]) is None:
                        rep_.fallback_at = at
                        rep_.entries = len(good.get("forms", []))
                        used.append(good)
                        sources[cid] = path
                    else:
                        rep_.state.append("broken")
                    continue
            if doc is not None and err is None:
                rep_.state.append("ok")
                if path != self.mapping_path:
                    if stamp.verify(doc) == "mismatch":
                        rep_.state.append("hand-edited")
                    rep_.stamp = doc.get("_stamp") if isinstance(doc.get("_stamp"), dict) else None
                rep_.entries = len(doc.get("forms", []))
                used.append(doc)
                sources[cid] = path
                if record:
                    self._record_good(cid, doc, path)
                continue
            rep_.error = err
            if err and err.get("kind") == "newer":
                rep_.state.append("newer")
            if err and err.get("kind") == "missing":
                rep_.state.append("missing")
            good, at = self._lastgood(cid)
            if good is not None and _combine_error(used + [good]) is None:
                rep_.state.append("fallback")
                rep_.fallback_at = at
                rep_.label = str(good.get("label") or cid)
                rep_.entries = len(good.get("forms", []))
                used.append(good)
                sources[cid] = path
            else:
                rep_.state.append("broken")
        for owner, names in conflicts.items():
            if owner in reports:
                reports[owner].state.append("conflict-copy")
                reports[owner].conflicts = sorted(names)
        ignore: set[str] = set()
        for doc in used:
            ignore.update(str(v).lower() for v in doc.get("ignore", []) or [])
        if self.global_ignore_path.exists():
            gdoc, gerr = _parse(self.global_ignore_path)
            if gerr is None and isinstance(gdoc, dict):
                ignore.update(str(v).lower() for v in gdoc.get("ignore", []))
        # Variants are merged after last-good copies are recorded, so they never end up in them.
        return Loaded({"version": 1, "clients": self._with_variants(used)}, ignore, sources), reports

    def restore(self, cid: str) -> None:
        """Put the last-good copy back in place of a broken client file (restamped)."""
        cid = self._check_id(cid)
        good, _at = self._lastgood(cid)
        if good is None:
            raise MappingError(f"no last good copy of {cid} on this machine")
        _write_client(self._file_for(cid), json.loads(json.dumps(good)), self.staging_dir, self._key(cid))

    # ---- client keys (DECISIONS 51) -------------------------------------------------------

    def _key(self, cid: Any) -> str | None:
        """This PC's key for the client, or None (no key store, Global, or none entered)."""
        if self.keys is None or not cid or cid == GLOBAL_ID:
            return None
        return self.keys.get(str(cid))

    def set_key(self, cid: str, key: str) -> str:
        """Enter (or generate, by passing keys.generate()) a client's key. The file is signed at
        once if it is exactly the copy this PC already trusts; otherwise it waits for review."""
        cid = self._check_id(cid)
        if self.keys is None:
            raise MappingError("this install has no place to keep client keys")
        key = self.keys.set(cid, key)
        self._sign_if_trusted(cid, key)
        return key

    def rotate_key(self, cid: str) -> str:
        """A new key; the file is re-signed if it verified with the old one. The old key stops
        verifying at once (DECISIONS 51): a colleague still on it has every change held."""
        cid = self._check_id(cid)
        old = self._key(cid)
        if self.keys is None or old is None:
            raise MappingError(f"{cid} has no key to rotate")
        path = self._file_for(cid)
        doc, err = _parse(path)
        new = self.keys.rotate(cid)
        if err is None and isinstance(doc, dict) and stamp.verify(doc, old) == "signed":
            _write_client(path, doc, self.staging_dir, new)
        return new

    def _sign_if_trusted(self, cid: str, key: str) -> None:
        """Adopt matches: sign an unsigned file identical to last-good."""
        path = self._file_for(cid)
        doc, err = _parse(path)
        good, _at = self._lastgood(cid)
        if err is not None or not isinstance(doc, dict) or good is None:
            return
        if stamp.verify(doc, key) == "unsigned" and stamp.canonical(doc) == stamp.canonical(
                {**good, "schema": doc.get("schema", good.get("schema"))}):
            _write_client(path, doc, self.staging_dir, key)

    def accept_held(self, cid: str) -> None:
        """A held change, reviewed and accepted: signed with this PC's key, and trusted from now."""
        cid = self._check_id(cid)
        key = self._key(cid)
        if key is None:
            raise MappingError(f"{cid} has no key on this machine")
        path = self._file_for(cid)
        doc, err = _parse(path)
        if err is not None:
            raise MappingError(f"{cid}'s file cannot be read: {err['message']}")
        doc = {**doc, "id": cid}
        self._validate_then_write(path, doc)
        self._record_good(cid, doc, path)

    def reject_held(self, cid: str) -> None:
        """A held change, reviewed and refused: last-good put back, signed."""
        self.restore(cid)

    def _held_diff(self, doc: dict[str, Any], good: dict[str, Any] | None, why: str) -> dict[str, Any]:
        now = [str(f.get("real")) for f in doc.get("forms", []) if isinstance(f, dict)]
        was = [str(f.get("real")) for f in (good or {}).get("forms", []) if isinstance(f, dict)]
        return {"added": [r for r in now if r not in set(was)],
                "removed": [r for r in was if r not in set(now)], "why": why}

    def _merge_conflict_copies(self) -> None:
        """OneDrive's conflict copies, merged (H19): a copy signed with the client's key goes into a
        main file that is itself signed - this PC's tokens win a clash - and is then moved to the
        local conflicts folder, never deleted. Anything unsigned stays where it is, flagged."""
        if self.keys is None or self.conflicts_dir is None or not self.clients_dir.is_dir():
            return
        for folder in (p for p in self.clients_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
            main = folder / f"{folder.name}.json"
            key = self._key(folder.name) if CLIENT_ID_RE.match(folder.name) else None
            if key is None or not main.is_file():
                continue
            for copy in sorted(folder.glob("*.json")):
                if copy == main or not self.is_conflict_copy(copy, {folder.name}):
                    continue
                theirs, err = _parse(copy)
                mine, merr = _parse(main)
                if (err or merr or not isinstance(theirs, dict) or stamp.verify(theirs, key) != "signed"
                        or stamp.verify(mine, key) != "signed"):
                    continue
                merged, added, clashes = _merged(mine, theirs)
                if _validate_client(merged) is not None:
                    continue
                _write_client(main, merged, self.staging_dir, key)
                self.conflicts_dir.mkdir(parents=True, exist_ok=True)
                target = self.conflicts_dir / f"{copy.stem}.{datetime.now():%Y%m%d-%H%M%S}.json"
                fileio.replace(copy, target)
                log.info("merged conflict copy %s into %s: %d form(s) added, %d token clash(es) kept local",
                         copy.name, main.name, added, len(clashes))

    def salvage(self, cid: str) -> dict[str, Any]:
        """Rebuild a client file from whatever entries are still readable in it (feature 13).

        Restore puts the last-good copy back, which is right when there is one and no help at
        all when there is not - and it throws away everything added since that copy. A file
        usually breaks in one place, and the entries either side of the break are still
        perfectly good JSON objects.

        The broken original is kept first, in the state folder and never beside the client
        file: the clients folder may be shared, and nobody else should receive a copy of
        somebody's failed write.
        """
        cid = self._check_id(cid)
        p = self._file_for(cid)
        if not p.exists():
            raise MappingError(f"unknown client {cid}")
        if self.state_dir is None:
            raise MappingError(
                "salvage needs somewhere to keep the broken file and this store has no state "
                "folder",
                why="Rebuilding the file overwrites it. Doing that without first putting the "
                    "original somewhere would destroy the only copy of whatever could not be "
                    "read, which is exactly the part worth looking at by hand.")
        text = p.read_text(encoding="utf-8-sig")
        _doc, parse_error = _parse(p)
        if parse_error is None:
            raise MappingError(f"{p.name} is valid JSON; there is nothing to salvage",
                               why="Salvage reads a file that will not parse. This one parses, "
                                   "so every entry in it is already in front of you.")

        found = [f for f in _recover_objects(text) if isinstance(f.get("real"), str)]
        rebuilt: dict[str, Any] = {"id": cid, "forms": found}
        label = _recover_value(text, "label")
        if isinstance(label, str):
            rebuilt["label"] = label
        index = _recover_value(text, "index")
        if isinstance(index, int) and not isinstance(index, bool):
            rebuilt["index"] = index
        ignore = _recover_value(text, "ignore")
        kept_ignore = sorted({v for v in ignore if isinstance(v, str)}) if isinstance(ignore, list) else []
        if kept_ignore:
            rebuilt["ignore"] = kept_ignore

        # A recovered entry can be readable and still unusable - a token that contains a real,
        # a duplicate, a shape that no longer validates. Drop those one at a time rather than
        # writing a file that is merely broken in a different way.
        dropped: list[str] = []
        while (why := _validate_client(rebuilt)) is not None:
            at = why.get("index")
            if at is None or not isinstance(at, int) or not (0 <= at < len(rebuilt["forms"])):
                raise MappingError(f"nothing usable could be rebuilt from {p.name}: {why['message']}")
            dropped.append(str(rebuilt["forms"].pop(at).get("real", "")))

        broken_dir = self.state_dir / "broken"
        broken_dir.mkdir(parents=True, exist_ok=True)
        kept = broken_dir / f"{cid}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        kept.write_text(text, encoding="utf-8")
        _write_client(p, rebuilt, self.staging_dir, self._key(rebuilt.get("id")))
        return {"forms": len(rebuilt["forms"]), "ignore": len(kept_ignore),
                "dropped": dropped, "kept": str(kept)}

    def client_view(self, cid: str) -> dict[str, Any]:
        """A client as the UI shows it: the current file when it parses (so a rejected row can be
        seen and fixed in place), else the last-good copy; every entry with its token and kind."""
        cid = self._check_id(cid)
        p = self._file_for(cid)
        doc: Any = None
        source = "file"
        if p.exists():
            doc, _err = _parse(p)
        if not isinstance(doc, dict):
            doc, _at = self._lastgood(cid)
            source = "lastgood"
        if doc is None:
            legacy = next((c for c in self.load_tolerant(record=False)[0].data["clients"] if c.get("id") == cid), None)
            if legacy is not None:
                doc, source = legacy, "client-map.json"
            elif cid == GLOBAL_ID:
                doc, source = {"id": GLOBAL_ID, "index": 0, "forms": [], "ignore": []}, "none"
            else:
                raise MappingError(f"unknown client {cid}")
        try:
            idx: int | None = client_index(cid, doc.get("index"))
        except MappingError:
            idx = None
        try:
            tokens = client_tokens({**doc, "id": cid})
        except (MappingError, ValueError, TypeError):
            tokens = {}
        global_reals = self._global_reals() if cid != GLOBAL_ID else set()
        rules = sorted({str(r).lower() for r in doc.get("email_rules") or []})
        forms = []
        for f in doc.get("forms", []) if isinstance(doc.get("forms"), list) else []:
            real = f.get("real") if isinstance(f, dict) else None
            # A kind written into the file is a correction somebody made and wins over the guess.
            declared = str(f.get("kind") or "") if isinstance(f, dict) else ""
            kind = declared
            if not kind and isinstance(real, str):
                try:
                    kind = classify(real)
                except (MappingError, ValueError):
                    pass
            # The matching rule as stored, unparsed. A rule the store could not honour must still
            # reach the page: the row is where somebody would go to correct it, so swallowing it
            # here would hide the only thing that explains why the form is not behaving.
            rule = f.get("match") if isinstance(f, dict) else None
            # How it got here, and - from local state, never from the file - exactly where from.
            # The page shows both: the first is what a colleague reading the file would see, the
            # second is what makes an entry defensible (feature 30).
            # NOT `source`: that name is already this function's "file" vs "lastgood".
            form_source = str(f.get("source") or "") if isinstance(f, dict) else ""
            detail = self.provenance.of(cid, str(real)) if isinstance(real, str) else {}
            forms.append({"real": real, "token": tokens.get(str(real), "") if real is not None else "", "kind": kind,
                          "kind_set": bool(declared),
                          "match": rule if rule else None,
                          "source": form_source or str(detail.get("source") or ""),
                          "seen": detail or None,
                          "by": str(f.get("by") or "") if isinstance(f, dict) else "",
                          "locked": isinstance(real, str) and real.lower() in global_reals,
                          "rule": email_rule_for(str(real), rules) if isinstance(real, str) else ""})
        # Each email rule with how many addresses it covers; the page lists those under the rule.
        email_rules = [{"domain": d, "count": sum(1 for f in forms if f["rule"] == d)} for d in rules]
        return {"id": cid, "index": idx, "label": str(doc.get("label") or ""), "forms": forms,
                "email_rules": email_rules,
                # The repo that carries this engagement's agent tooling, by name (D41).
                "primary_repo": str(doc.get("primary_repo") or ""),
                "ignore": list(doc.get("ignore", []) or []),
                "source": source, "file": p.name}

    def raw(self, cid: str) -> dict[str, Any]:
        """The client file as text plus its error, for the syntax-error view."""
        p = self._file_for(cid)
        if not p.exists():
            raise MappingError(f"unknown client {cid}")
        doc, err = _parse(p)
        if err is None:
            err = _validate_client({**doc, "id": doc.get("id") or cid} if isinstance(doc, dict) else doc)
        # "file_error", not "error": a top-level "error" key means the API call itself failed.
        return {"file": p.name, "text": p.read_text(encoding="utf-8-sig"), "file_error": err}

    # ---- registry ---------------------------------------------------------------

    def registry(self) -> dict[str, str]:
        p = self.clients_dir / REGISTRY
        return dict(_read(p).get("indexes", {})) if p.exists() else {}

    def next_index(self) -> int:
        used = {int(k) for k in self.registry()} | {
            client_index(c["id"], c.get("index")) for c in self.load_tolerant(record=False)[0].data["clients"]
            if c.get("index") is not None or any(ch.isdigit() for ch in c["id"])}
        return (max(used) + 1) if used else 1

    def _register(self, cid: str, idx: int) -> None:
        reg = self.registry()
        owner = reg.get(str(idx))
        if owner and owner != cid:
            raise MappingError(f"index {idx} is already allocated to {owner} in {REGISTRY}")
        reg[str(idx)] = cid
        _write(self.clients_dir / REGISTRY, {"indexes": dict(sorted(reg.items(), key=lambda kv: int(kv[0])))})

    # ---- locating a client for mutation ---------------------------------------

    def _dir_mode(self) -> bool:
        return self.clients_dir.is_dir() or not self.mapping_path.exists()

    def _client_doc(self, cid: str) -> tuple[Path, dict[str, Any], dict[str, Any] | None]:
        """(file, whole document, client entry) for cid, in either layout."""
        cid = self._check_id(cid)
        p = self._file_for(cid)
        if p.exists():
            doc = _read(p)
            doc["id"] = cid
            return p, doc, doc
        if self.mapping_path.exists():
            doc = _read(self.mapping_path)
            for c in doc.get("clients", []):
                if c.get("id") == cid:
                    return self.mapping_path, doc, c
        raise MappingError(f"unknown client {cid}")

    def _validate_then_write(self, path: Path, doc: dict[str, Any]) -> None:
        """Validate, then write. The document is checked on its own and against every other client
        as currently usable (tolerant), so one broken client never blocks fixing it or editing others."""
        loaded, _reports = self.load_tolerant(record=False)
        if self._is_client_path(path):
            err = _validate_client(doc)
            if err:
                raise MappingError(err["message"])
            others = [c for c in loaded.data["clients"] if c.get("id") != doc.get("id")]
            why = _combine_error(others + [doc])
            if why:
                raise MappingError(why)
            _write_client(path, doc, self.staging_dir, self._key(doc.get("id")))
            return
        others = [c for c in loaded.data["clients"] if loaded.sources.get(c.get("id")) != self.mapping_path]
        why = _combine_error(others + list(doc.get("clients", [])))
        if why:
            raise MappingError(why)
        _write(path, doc)

    # ---- mutations ------------------------------------------------------------------

    def join_shared(self, folder: str | Path) -> str:
        """A client a colleague shared, added from its synced folder (DECISIONS 67): the folder they
        shared in OneDrive, added to this user's OneDrive with "Add shortcut to My files".

        Linked into the clients location as a directory junction named after the client, so it is
        read, written, signed and watched exactly like a client created here - nothing else has to
        know it lives somewhere else. The folder must hold one client file; its index must be free on
        this PC (a clash says so, and the colleague's file is not touched). Returns the client id."""
        source = Path(folder)
        if not source.is_dir():
            raise MappingError(f"{source} is not a folder")
        named = source / f"{source.name}.json"
        files = [named] if named.is_file() else [p for p in source.glob("*.json") if not p.name.startswith("_")]
        docs = [(p, _parse(p)[0]) for p in files]
        docs = [(p, d) for p, d in docs if isinstance(d, dict) and d.get("id")]
        if len(docs) != 1:
            raise MappingError(f"{source} does not hold exactly one client file - choose the folder that was shared, "
                               "the one named after the client")
        path, doc = docs[0]
        cid = self._check_id(str(doc["id"]))
        if cid == GLOBAL_ID:
            raise MappingError("Global is never shared; that folder holds someone's Global")
        target = self.clients_dir / cid
        if target.exists():
            try:
                if target.resolve() == source.resolve():
                    return cid                        # already joined
            except OSError:
                pass
            raise MappingError(f"a client called {cid} already exists here, so the shared one cannot be added beside it")
        # An index only seeds new tokens; what matters is whether the tokens themselves clash with a
        # client here. Asked before anything is linked, with the same answer the loader gives.
        current = [c for c in self.load_tolerant(record=False)[0].data["clients"] if c.get("id") != cid]
        why = _combine_error(current + [{**doc, "id": cid}])
        if why:
            raise MappingError(f"{cid} cannot join: {why}")
        self.clients_dir.mkdir(parents=True, exist_ok=True)
        _link_folder(target, source)
        idx = client_index(cid, doc.get("index"))
        if not self.registry().get(str(idx)):
            self._register(cid, idx)
        return cid

    def renumber(self, cid: str, index: int) -> None:
        """Give a client a different index (DECISIONS 67): the number new tokens are derived from.
        Tokens already in its file are stored and do not change; only what is added from now on
        is derived from the new number - which is how two people's clients that landed on the same
        index stop producing the same tokens. Refused when another client here already has it."""
        cid = self._check_id(cid)
        if cid == GLOBAL_ID:
            raise MappingError("Global is always index 0")
        index = int(index)
        if index < 1:
            raise MappingError("an index is a whole number from 1")
        clients = self.load_tolerant(record=False)[0].data["clients"]
        taken = {client_index(c["id"], c.get("index")): c["id"] for c in clients if c["id"] != cid}
        if index in taken:
            raise MappingError(f"index {index} is {taken[index]}'s; the next free one is {self.next_index()}")
        path = self._file_for(cid)
        doc, err = _parse(path)
        if err is not None or not isinstance(doc, dict):
            raise MappingError(f"{cid}'s file cannot be read, so it cannot be renumbered")
        reg = {k: v for k, v in self.registry().items() if v != cid}
        reg[str(index)] = cid
        _write(self.clients_dir / REGISTRY, {"indexes": dict(sorted(reg.items(), key=lambda kv: int(kv[0])))})
        self._validate_then_write(path, {**doc, "index": index})

    def add_client(self, cid: str, index: int | None = None, expand_case: bool = True, label: str | None = None) -> int:
        cid = self._check_id(cid)
        label = _check_label(label)
        if cid in self.load_tolerant(record=False)[0].sources or (cid != GLOBAL_ID and self._file_for(cid).exists()):
            raise MappingError(f"client {cid} already exists")
        if cid == GLOBAL_ID:
            self.ensure_global()
            return 0
        idx = client_index(cid, index)
        if self._dir_mode():
            self._register(cid, idx)
            doc = {"id": cid, "index": idx, "expand_case": expand_case, "forms": [], "ignore": []}
            if label:
                doc["label"] = label
            self._validate_then_write(self._file_for(cid), doc)
        else:
            doc = _read(self.mapping_path)
            doc.setdefault("clients", []).append({"id": cid, "index": idx, "expand_case": expand_case, "forms": []})
            self._validate_then_write(self.mapping_path, doc)
        return idx

    def create_client(self, cid: str, label: str | None = None, reals: list[str] | None = None,
                      source: str = "", index: int | None = None) -> tuple[int, list[tuple[str, str]]]:
        """A new client and its first entries in ONE write (feature 111): the Add Client wizard's
        Save. Validated as a whole before anything is written, so a refusal leaves no client file
        and no index taken - nothing half made. Global (and the single-file layout) take the
        entries the usual way. Returns the index and the (real, token) pairs added."""
        cid = self._check_id(cid)
        wanted = [r.strip() for r in (reals or []) if r and r.strip()]
        if cid == GLOBAL_ID:
            return 0, self.add_forms(GLOBAL_ID, wanted, source=source)
        label = _check_label(label)
        if cid in self.load_tolerant(record=False)[0].sources or self._file_for(cid).exists():
            raise MappingError(f"client {cid} already exists")
        if not self._dir_mode():
            idx = self.add_client(cid, index, True, label)
            return idx, self.add_forms(cid, wanted, source=source)
        idx = client_index(cid, index)
        owner = self.registry().get(str(idx))
        if owner and owner != cid:
            raise MappingError(f"index {idx} is already allocated to {owner} in {REGISTRY}")
        doc: dict[str, Any] = {"id": cid, "index": idx, "expand_case": True, "forms": [], "ignore": []}
        if label:
            doc["label"] = label
        added: list[tuple[str, str]] = []
        seen: set[str] = set()
        for real in wanted:
            if real in seen:
                continue
            seen.add(real)
            added.append((real, self._append_form(cid, doc, real, None, "", source)))
        self._validate_then_write(self._file_for(cid), doc)
        self._register(cid, idx)
        return idx, added

    def _append_form(self, cid: str, client: dict[str, Any], real: str, token: str | None = None,
                     origin: str = "", source: str = "") -> str:
        """Derive one form's token and append it to the client document IN MEMORY.

        The one place the derivation order lives, so a form added on its own and the same form added
        as part of a scan get the same token. Derived against this client's existing tokens, then
        stored, so a second domain or name gets its own suffixed token and teammates sharing the
        file get the same one. A resource name carrying one of this client's forms keeps its shape
        (kvaueprdclient05management).
        """
        forms = client.setdefault("forms", [])
        if not token and cid != GLOBAL_ID and email_rule_for(real, client.get("email_rules") or []):
            # Under an email rule: eight digits of the address's own hash, the same on every machine
            # whatever order the addresses are met in (feature 96).
            taken = client_tokens({**client, "id": cid}).values()
            token = rule_email_token(real, client_index(cid, client.get("index")), taken=taken)
        tok = token.strip() if token else (self._embedded_token(cid, client, real) or
                                           client_tokens({**client, "id": cid, "forms": forms + [{"real": real}]})[real])
        entry: dict[str, Any] = {"real": real, "token": tok}
        # Recorded so the detector can tell what NAMES a client from what was merely found near
        # one: only the former is watched for inside longer words (D1, mapping.FOUND_ORIGINS).
        if origin:
            entry["origin"] = origin
        # How it got here, in the words a person reads (feature 30). Coarse on purpose: this is
        # the part that travels with a published file, so it says "Repo scan" and never which
        # repository. The detail goes to `self.provenance`, which never leaves the machine.
        if source:
            entry["source"] = source
        # Global is this person's own and never shared, so it has no one else to tell apart.
        if self.author and cid != GLOBAL_ID:
            entry["by"] = self.author
        forms.append(entry)
        if client.get("removed"):
            client["removed"] = [r for r in client["removed"] if r.get("real") != real]
        return tok

    def add_form(self, cid: str, real: str, token: str | None = None, origin: str = "",
                 source: str = "") -> str:
        # Global is where the things that belong to nobody in particular go, so it has to be there
        # the first time something needs it. On a fresh install pointed at an empty clients folder
        # it was not, and every GUID the proxy learned with no obvious owner was lost to
        # "unknown client global" until somebody happened to create it by hand.
        if cid == GLOBAL_ID:
            self.ensure_global()
        path, doc, client = self._client_doc(cid)
        real = real.strip()
        if any(f.get("real") == real for f in client.setdefault("forms", [])):
            raise MappingError(
                f"{real!r} is already a form of {cid}",
                why=f"{cid} already has this entry with a token of its own. Adding it twice "
                    f"would give the same value two tokens, and which one the proxy emitted "
                    f"would depend on which it read first.",
                offer={"action": "find", "label": "Show me the one that is there", "value": real},
            )
        tok = self._append_form(cid, client, real, token, origin, source)
        self._validate_then_write(path, doc)
        return tok

    def set_email_rule(self, cid: str, domain: str, on: bool) -> dict[str, Any]:
        """One rule for every address at one of a client's domains (feature 96), on or off.

        On: every address there is the client's - added automatically when met, whatever the email
        setting, with a token every machine derives alike - and the client page shows the rule, not
        the addresses one by one. The addresses already there keep their tokens, so transcripts
        written with them still restore. Off: they are listed again; nothing is removed.
        The domain must be one of the client's own domain or tenant entries: the rule rests on that
        proof of ownership."""
        if cid == GLOBAL_ID:
            raise MappingError("email rules are for a client's own domains, not Global")
        wanted = str(domain or "").strip().lower().lstrip("*").lstrip("@")
        path, doc, client = self._client_doc(cid)
        owned = {str(f.get("real", "")).lower() for f in client.get("forms", []) or []
                 if isinstance(f, dict) and classify(str(f.get("real", ""))) in ("domain", "tenant")}
        rules = [str(r).lower() for r in client.get("email_rules") or []]
        if on:
            if wanted not in owned:
                raise MappingError(f"{wanted} is not one of {cid}'s domains",
                                   why="A rule says every address at a domain is this client's; that "
                                       "holds only for a domain the client is known to own. Add the "
                                       "domain as an entry first.")
            if wanted not in rules:
                rules.append(wanted)
        else:
            rules = [r for r in rules if r != wanted]
        if rules:
            client["email_rules"] = sorted(rules)
        else:
            client.pop("email_rules", None)
        self._validate_then_write(path, doc)
        covered = sum(1 for f in client.get("forms", []) or []
                      if isinstance(f, dict) and email_rule_for(str(f.get("real", "")), [wanted]))
        return {"domain": wanted, "on": on, "covered": covered}

    def add_forms(self, cid: str, reals: list[str], origin: str = "",
                  source: str = "") -> list[tuple[str, str]]:
        """Add many forms in ONE validate-and-write. Returns the (real, token) pairs added.

        A scan used to call add_form once per find, and each call read every client file, built a
        complete Mapper - the whole trie, over every form - and rewrote the entire client document.
        For the 1,733 forms one repository produced that is 1,733 trie builds and 1,733 whole-file
        rewrites of a 160 KB document into a synced folder: quadratic in both CPU and I/O, about an
        hour, and five occasions where the application refused its own write.

        Tokens are still derived one at a time against the forms accumulated so far, so the result
        is identical to adding them in the same order singly - only the validating and the writing
        are done once. A real already present is skipped rather than raising, because a bulk add
        meeting one duplicate should not lose the other seventeen hundred.
        """
        if cid == GLOBAL_ID:
            self.ensure_global()
        path, doc, client = self._client_doc(cid)
        forms = client.setdefault("forms", [])
        keep = len(forms)
        have = {f.get("real") for f in forms}
        added: list[tuple[str, str]] = []
        for real in reals:
            real = (real or "").strip()
            if not real or real in have:
                continue
            added.append((real, self._append_form(cid, client, real, None, origin, source)))
            have.add(real)
        if not added:
            return []
        try:
            self._validate_then_write(path, doc)
            return added
        except MappingError as first:
            # One form the mapping will not have must cost one form, not the batch. A scan of a
            # real repository lost 573 entries to a single token the safety rule refused, and the
            # message named the token rather than the 572 that were fine. Rebuilt by adding them
            # one at a time and keeping what validates - the same shape Proxy._build uses for
            # placeholders, and for the same reason.
            log.warning("a batch of %d form(s) for %s was refused (%s); keeping the ones that hold",
                        len(added), cid, first)
        del forms[keep:]
        kept: list[tuple[str, str]] = []
        if len(added) > FALLBACK_CAP:
            # Each try is a whole validate-and-write of a synced file (H13): bounded, and what is
            # left out is still unmapped, so it is found again on a later request.
            log.warning("one at a time for the first %d of %d form(s) for %s; the rest are left for later",
                        FALLBACK_CAP, len(added), cid)
            added = added[:FALLBACK_CAP]
        for real, _token in added:
            token = self._append_form(cid, client, real, None, origin, source)
            try:
                self._validate_then_write(path, doc)
                kept.append((real, token))
            except MappingError as exc:
                forms.pop()
                log.warning("left out %r for %s: %s", real, cid, exc)
        return kept

    @staticmethod
    def _embedded_token(cid: str, client: dict[str, Any], real: str) -> str:
        """The structure-preserving token when `real` is a resource name carrying one of this
        client's forms, else "" (the caller then derives the usual token)."""
        if not real.replace("-", "").replace("_", "").isalnum():
            return ""
        from .detect import GENERIC_WORDS  # the same words detection looks for inside longer words

        low = real.lower()
        inside: list[str] = []
        for form in client.get("forms", []) or []:
            if not isinstance(form, dict):
                continue
            # What a scan or an auto-add found is not the client's name (D1): "<client> Key Vault" made
            # "Vault" a word to shape tokens around, giving KeyVault -> Key<token> (D149).
            if str(form.get("origin") or "") in FOUND_ORIGINS:
                continue
            text = str(form.get("real", ""))
            parts = [text] if text.isalnum() else re.split(r"[^A-Za-z0-9]+", text)
            for part in parts:
                if (len(part) >= 4 and part.isalnum() and part.lower() not in GENERIC_WORDS
                        and part.lower() in low and part.lower() != low):
                    inside.append(part)
        if not inside:
            return ""
        form = max(inside, key=len)
        index = client_index(cid, client.get("index"))
        prefix = "Org" if cid == GLOBAL_ID else "Client"
        try:
            token = embedded_token(real, form, index, prefix)
        except MappingError:
            return ""
        # A shape-preserving token is a nicety; a token the mapping will refuse is a lost form. The
        # real "_NORTHWINDsSettings" gave "_CLIENT05sSettings", which SAFE_TOKEN_RE rejects because it
        # does not start with a letter or digit - and the refusal surfaced hundreds of forms later,
        # as a whole scan's worth of adds failing. Falling back to the ordinary derivation keeps the
        # form; only its readability is lost.
        if not SAFE_TOKEN_RE.match(token):
            return ""
        # The shaped token can be longer than the real, and the thing being named may not allow
        # it: `stacmeprdaustraliaeast` (22, a valid storage account) became
        # `stclient05prdaustraliaeast` (26, which is not), and a model reviewing that config
        # correctly reports it as invalid while Terraform generated against it fails after
        # rehydration (D7).
        #
        # "Never grow" is too blunt and was tried and reverted: `kvaueprdtailwindmgmt` (20)
        # becomes `kvaueprdclient1912mgmt` (22) whenever the index has four digits, and that is
        # the canonical case the whole feature exists for. So the rule is a ceiling rather than a
        # comparison - decided 2026-09-27, and a stopgap until a form can declare what it is
        # (feature 23). 24 is the tightest common Azure limit (storage account, key vault); the
        # name itself never says which resource it is, so nothing here can be exactly right.
        if len(token) > SHAPED_TOKEN_CEILING >= len(real):
            return ""
        taken = {t.lower() for t in client_tokens({**client, "id": cid}).values()}
        return "" if token.lower() in taken else token

    # ---- Global precedence: copy, lock, labels -----------------------------------------------

    def _global_reals(self) -> set[str]:
        loaded, _reports = self.load_tolerant(record=False)
        return {str(f.get("real", "")).lower() for c in loaded.data["clients"] if c.get("id") == GLOBAL_ID
                for f in c.get("forms", []) if isinstance(f, dict)}

    def _check_unlocked(self, cid: str, real: str, global_reals: set[str] | None = None) -> None:
        """A client entry that Global also has is locked: changing or removing it here would change
        the shared client file for teammates while nothing changes on this machine.

        `global_reals`: Global's names, worked out once by a caller checking many entries. Working
        them out reads and parses every client file, and doing that per entry was most of a bulk
        ignore's second (feature 57, DECISIONS 58)."""
        if cid == GLOBAL_ID:
            return
        if real.lower() in (global_reals if global_reals is not None else self._global_reals()):
            raise MappingError(
                f"{real} is also in Global; remove it from Global to change it here",
                why="Global wins, so this row is not what the proxy is actually using - the "
                    "Global entry is. Changing the copy here would rewrite a client file that "
                    "gets shared while nothing at all changed on this machine.",
                offer={"action": "open_global", "label": "Open Global", "value": real},
            )

    def copy_to_global(self, cid: str, real: str) -> str:
        """Add a client's entry to Global (this machine only); the client entry stays, now locked."""
        cid = self._check_id(cid)
        if cid == GLOBAL_ID:
            raise MappingError(f"{real} is already in Global")
        _path, _doc, client = self._client_doc(cid)
        if not any(f.get("real") == real for f in client.get("forms", [])):
            raise MappingError(f"{real!r} is not a form of {cid}")
        if real.lower() in self._global_reals():
            raise MappingError(f"{real} is already in Global")
        self.ensure_global()
        return self.add_form(GLOBAL_ID, real)

    def duplicated_reals(self) -> dict[str, list[str]]:
        """Reals that more than one client claims, read straight from the files.

        Nothing local can produce this - add_form refuses a real another client already has - but a
        client file is a shared thing, and somebody else's environment has no way of knowing what
        our other clients hold. Their file arrives with a real we already have somewhere else, and
        it has to be allowed in, because the rest of that file is theirs and correct.
        """
        owners: dict[str, list[str]] = {}
        for path in self.client_files():
            doc, err = _parse(path)
            if err is not None or not isinstance(doc, dict):
                continue
            cid = str(doc.get("id") or self._file_id(path))
            if cid == GLOBAL_ID:
                continue
            for f in doc.get("forms", []) if isinstance(doc.get("forms"), list) else []:
                real = str(f.get("real", "")) if isinstance(f, dict) else ""
                if real:
                    owners.setdefault(real.lower(), [])
                    if cid not in owners[real.lower()]:
                        owners[real.lower()].append(cid)
        return {real: cids for real, cids in owners.items() if len(cids) > 1}

    def promote_duplicates(self) -> list[tuple[str, list[str]]]:
        """Put every real that two clients claim into Global, which settles it locally.

        Global wins on this machine, so one entry there makes both clients' copies shadowed and
        locked, and the mapper ingests exactly one token for the real instead of choosing between
        two. Without it the loser is dropped from the mapper while its page still shows its own
        token - a token the proxy never uses - or the client has no last-good copy and goes broken,
        which stops model traffic until somebody works out why.

        Only Global is written, and Global is this machine's own list: the clients' files are left
        exactly as their owners sent them.
        """
        duplicates = self.duplicated_reals()
        if not duplicates:
            return []
        already = self._global_reals()
        promoted: list[tuple[str, list[str]]] = []
        for low, cids in sorted(duplicates.items()):
            if low in already:
                continue          # Global already settles this one
            real = self._first_spelling(low, cids)
            try:
                self.ensure_global()
                self.add_form(GLOBAL_ID, real)
                promoted.append((real, cids))
            except MappingError:
                continue          # cannot settle it; load_tolerant still degrades safely
        return promoted

    def _first_spelling(self, low: str, cids: list[str]) -> str:
        """The real as the first client that claims it spells it: Global's entry has to be one of
        the spellings on file, and matching is case-insensitive anyway."""
        for path in self.client_files():
            doc, err = _parse(path)
            if err is not None or not isinstance(doc, dict):
                continue
            if str(doc.get("id") or self._file_id(path)) not in cids:
                continue
            for f in doc.get("forms", []) if isinstance(doc.get("forms"), list) else []:
                real = str(f.get("real", "")) if isinstance(f, dict) else ""
                if real.lower() == low:
                    return real
        return low

    def take_from_global(self, real: str, to_cid: str) -> str:
        """The reverse of copy_to_global: Global gives an identifier back to one client.

        Two shapes, because Global's list and a client's list can both hold the same real. When the
        client already has it, its row is locked only BECAUSE Global has it - so dropping Global's
        copy is the whole operation, and the client keeps the token it already had. When the client
        does not have it, it is an ordinary move: out of Global, into the client, new token.

        Returns the token the client is left with, which is the old one in the first case and a
        fresh one in the second.
        """
        to_cid = self._check_id(to_cid)
        if to_cid == GLOBAL_ID:
            raise MappingError("that is Global; name the client to give it to")
        if real.lower() not in self._global_reals():
            raise MappingError(f"{real!r} is not in Global")
        _p, _d, client = self._client_doc(to_cid)
        if any(f.get("real") == real for f in client.get("forms", [])):
            self.delete_form(GLOBAL_ID, real)          # unlocks the row the client already had
            return next(f["token"] for f in self.client_view(to_cid)["forms"] if f["real"] == real)
        return self.move_form(real, GLOBAL_ID, to_cid)

    def set_label(self, cid: str, label: str) -> None:
        """Set or (with an empty label) clear a client's display name."""
        label = _check_label(label)
        path, doc, client = self._client_doc(cid)
        if label:
            client["label"] = label
        else:
            client.pop("label", None)
        self._validate_then_write(path, doc)

    # A repository name may be anything a folder may be, minus the separators that would make it
    # a path. That is the point: this travels in a shared file, so it must not be one.
    PRIMARY_REPO_RE = re.compile(r"^[^\\/:*?\"<>|]{1,120}$")

    def set_primary_repo(self, cid: str, name: str) -> None:
        """Name the repository that carries this engagement's agent tooling (defect D41).

        **A name, never a path.** The client file is shared through OneDrive, and a teammate's
        `C:/Repos/acme` is not a path on their disk - `profiles.py` makes the same point about
        why folders live on the profile. A repo's name is a fact about the engagement that is
        true on everybody's machine, so it is the thing that can travel.

        Empty clears it, and the primary is then derived from the `.code-workspace` file.
        """
        name = str(name or "").strip()
        if name and not self.PRIMARY_REPO_RE.match(name):
            raise MappingError(
                f"{name!r} is not a repository name",
                why="This is the repo's NAME, not its path - it goes in the client file, which "
                    "is shared, and a path on your disk is not a path on a colleague's. It is "
                    "matched against the folder names in the profile.",
            )
        path, doc, client = self._client_doc(cid)
        if name:
            client["primary_repo"] = name
        else:
            client.pop("primary_repo", None)
        self._validate_then_write(path, doc)

    def update_form(self, cid: str, real: str, new_real: str | None = None, new_token: str | None = None,
                    new_kind: str | None = None, new_match: Any = _UNSET) -> str:
        """Edit an entry in place (inline edit, or fixing a rejected row). A changed real without an
        explicit token gets a freshly derived token; the old one stops being rehydrated.

        `new_kind` corrects the SHAPE the token is built from, which is the part that carries
        meaning past the filter. classify() guesses from the text and is right nearly always;
        "linkedin.com/in/janedoe" reads as a name, so it becomes a name's token, and "read my bio at
        Org00" means nothing to anybody. Correcting the shape to a domain makes it
        "org00.invalid", which still reads as somewhere to look.
        """
        self._check_unlocked(self._check_id(cid), real)
        path, doc, client = self._client_doc(cid)
        forms = client.setdefault("forms", [])
        form = next((f for f in forms if f.get("real") == real), None)
        if form is None:
            raise MappingError(f"{real!r} is not a form of {cid}")
        target = (new_real if new_real is not None else real).strip()
        if not target:
            raise MappingError("the real identifier is empty")
        if target != real and any(f.get("real") == target for f in forms):
            raise MappingError(f"{target!r} is already a form of {cid}")
        had_token = "token" in form
        form["real"] = target
        shape = (new_kind or "").strip().lower()
        if shape:
            if shape not in TOKEN_SHAPES:
                raise MappingError(
                    f"{shape!r} is not a shape a token can be built from "
                    f"({', '.join(sorted(TOKEN_SHAPES))})",
                    why="The shape decides what the replacement looks like, so it has to be one "
                        "the builder knows how to write. A domain becomes something.invalid, an "
                        "email becomes an address; a shape with no rule behind it would leave "
                        "the value with no token at all.",
                )
            # An explicit shape only means anything through a derived token, so a stored one goes:
            # keeping it would leave the row saying "domain" while emitting the name-shaped token.
            form["kind"] = shape
            form.pop("token", None)
            form["token"] = client_tokens({**client, "id": cid})[target]
        elif new_token and new_token.strip():
            form["token"] = new_token.strip()
        elif target != real and had_token:
            form.pop("token")
            form["token"] = client_tokens({**client, "id": cid})[target]
        # When this form counts as the client rather than as the ordinary word it looks like
        # (feature 23). _UNSET rather than None as the default, because None is a real value
        # here: it means "take the rule off", and a caller that simply is not editing the rule
        # must not silently remove one.
        if target != real:
            self.provenance.rename(cid, real, target)   # the evidence follows the name
        if new_match is not _UNSET:
            rule = parse_match(new_match)      # raises on a shape that could not be honoured
            form.pop("match", None)
            if rule is not None:
                form["match"] = rule.as_json()
        self._validate_then_write(path, doc)
        return form.get("token") or client_tokens({**client, "id": cid})[target]

    def rederive_form(self, cid: str, real: str) -> str:
        """Throw away this entry's stored token and derive it again under today's rules.

        `client_tokens` takes a stored token verbatim and only derives the rest, which is what
        keeps a token stable across versions - and it also means a file that has been through an
        older version keeps whatever it was given then, including tokens a later rule would now
        refuse. There was no way to say "this one, again" short of editing the file (D6).

        Deliberately one entry at a time, and deliberately not automatic. A token is what a
        colleague's copy of this file emits and what a model may already have written into a
        document; changing one is a decision about a specific entry, not a migration to run over
        everything.
        """
        self._check_unlocked(self._check_id(cid), real)
        path, doc, client = self._client_doc(cid)
        forms = client.setdefault("forms", [])
        form = next((f for f in forms if f.get("real") == real), None)
        if form is None:
            raise MappingError(f"{real!r} is not a form of {cid}")
        before = form.get("token", "")
        form.pop("token", None)
        # Derived against the other forms as they stand, so it avoids the tokens they hold - the
        # same path a form added today would take.
        form["token"] = client_tokens({**client, "id": cid})[real]
        if form["token"] == before:
            return before                      # already what today's rules give: nothing to write
        self._validate_then_write(path, doc)
        return form["token"]

    def move_form(self, real: str, from_cid: str, to_cid: str) -> str:
        """Move an identifier to another client (or global). The token is regenerated for the
        new owner; a transcript that still carries the old token cannot be rehydrated afterwards."""
        if self._check_id(to_cid) == GLOBAL_ID:
            raise MappingError("moving into Global is not offered; use Copy to Global")
        self._check_unlocked(self._check_id(from_cid), real)
        _p, _d, src = self._client_doc(from_cid)
        form = next((f for f in src.get("forms", []) if f.get("real") == real), None)
        if form is None:
            raise MappingError(f"{real!r} is not a form of {from_cid}")
        self.delete_form(from_cid, real)
        try:
            return self.add_form(to_cid, real, None)
        except MappingError:
            self.add_form(from_cid, real, form.get("token"))  # put it back
            raise

    def form_to_ignore(self, cid: str, real: str, scope: str) -> None:
        """Take an entry off a client's token list and put it on an ignore list (the client's own,
        or "global" for everywhere). A declared entry is always tokenised whatever any ignore list
        says - ignore lists only stop detection proposing a value - so both halves are needed."""
        scope = "global" if scope in ("global", GLOBAL_ID) else self._check_id(scope)
        try:
            self.delete_form(cid, real)
        except MappingError as exc:
            # The message a person met here was the whole of `"asp.net" is not a form of contoso`.
            # It named the rule and stopped. An ignore has to come off the list it was found on,
            # and this is not on this client's - but Global takes anything, and that is almost
            # always what was meant, so offer it rather than describing it.
            if getattr(exc, "offer", {}).get("action") != "reload":
                raise
            if scope == "global":
                why = (f"{real!r} is not on {cid}'s token list, so there was nothing to take off "
                       f"it. The global ignore list does not need it to be - it can take the "
                       f"value on its own.")
            else:
                why = (f"An ignore belongs to the list the value was found on. {real!r} is not on "
                       f"{cid}'s, so ignoring it there would say something about {cid} that is not "
                       f"true - and that file gets shared. The global list has no such claim to "
                       f"make.")
            raise MappingError(
                str(exc), why=why,
                offer={"action": "ignore_global", "label": "Ignore it globally instead",
                       "value": real},
            ) from exc
        try:
            self.add_ignore(real, scope)
        except MappingError:
            self.add_form(cid, real, None)   # put it back: never leave it neither tokenised nor ignored
            raise

    def move_ignore(self, value: str, from_scope: str, to_scope: str) -> None:
        if to_scope == GLOBAL_ID:
            to_scope = "global"
        if not self.remove_ignore(value, from_scope):
            raise MappingError(f"{value!r} is not on the {from_scope} ignore list")
        self.add_ignore(value, to_scope)

    def delete_form(self, cid: str, real: str) -> None:
        """Remove an entry, keeping a removal record so a later merge of two copies of the file
        (piece 6) does not bring it back."""
        self._check_unlocked(self._check_id(cid), real)
        path, doc, client = self._client_doc(cid)
        forms = client.get("forms", [])
        if not any(f.get("real") == real for f in forms):
            raise MappingError(
                f"{real!r} is not a form of {cid}",
                why=f"{cid}'s token list does not have this entry, so there is nothing here to "
                    f"remove. Usually the table is showing a row another window already deleted.",
                offer={"action": "reload", "label": "Reload the list"},
            )
        client["forms"] = [f for f in forms if f.get("real") != real]
        removed = [r for r in client.get("removed", []) or [] if r.get("real") != real]
        removed.append({"real": real, "at": stamp.now(), "by": stamp.machine()})
        client["removed"] = removed
        self._validate_then_write(path, doc)
        self.provenance.forget(cid, real)   # the entry is gone; its evidence goes with it

    def _kept(self) -> list[list[str]]:
        if self.kept_path is None:
            return self._memory_kept
        try:
            return json.loads(self.kept_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def others_additions(self) -> list[dict[str, str]]:
        """Forms someone else added to a client this PC uses, not yet kept here (DECISIONS 67).

        A form with no author predates authors and is not listed: there is no one to name.
        """
        kept = {tuple(k) for k in self._kept()}
        found: list[dict[str, str]] = []
        for path in self.client_files():
            doc, err = _parse(path)
            if err is not None or not isinstance(doc, dict):
                continue
            cid = str(doc.get("id") or self._file_id(path))
            if cid == GLOBAL_ID:
                continue
            for f in self.client_view(cid).get("forms", []):
                by = str(f.get("by") or "")
                if by and by != self.author and (cid, f["real"], by) not in kept:
                    found.append({"client": cid, "real": f["real"], "token": f["token"], "by": by})
        return found

    def keep_addition(self, cid: str, real: str) -> None:
        """This PC's answer that another person's form is right. Local: the shared file is unchanged,
        so each teammate decides for themselves."""
        by = next((str(f.get("by") or "") for f in self.client_view(cid).get("forms", [])
                   if f.get("real") == real), "")
        kept = [k for k in self._kept() if tuple(k) != (cid, real, by)] + [[cid, real, by]]
        if self.kept_path is None:
            self._memory_kept = kept
            return
        self.kept_path.parent.mkdir(parents=True, exist_ok=True)
        self.kept_path.write_text(json.dumps(kept, indent=1), encoding="utf-8")

    def delete_forms(self, cid: str, reals: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        """Remove many entries in ONE validate-and-write. Returns (removed, [(real, why), ...]).

        The page used to send one request per row, and each one revalidated and rebuilt the whole
        mapping (D36) - so clearing a few hundred entries took minutes. Worse, the loop stopped at
        the first refusal and left the rest untried while the page still showed the old state
        (D23). This does the lot at once and reports what would not go, rather than choosing
        between all and nothing.
        """
        self._check_id(cid)
        path, doc, client = self._client_doc(cid)
        forms = client.get("forms", [])
        present = {f.get("real") for f in forms}
        wanted, refused = [], []
        global_reals = self._global_reals() if cid != GLOBAL_ID else set()     # once, not per entry
        for real in reals:
            if real not in present:
                refused.append((real, f"{real!r} is not a form of {cid}"))
                continue
            try:
                self._check_unlocked(cid, real, global_reals)
            except MappingError as exc:
                refused.append((real, str(exc)))
                continue
            wanted.append(real)
        if not wanted:
            return [], refused
        going = set(wanted)
        client["forms"] = [f for f in forms if f.get("real") not in going]
        kept = [r for r in client.get("removed", []) or [] if r.get("real") not in going]
        at, by = stamp.now(), stamp.machine()
        client["removed"] = kept + [{"real": r, "at": at, "by": by} for r in wanted]
        self._validate_then_write(path, doc)
        return wanted, refused

    def forms_to_ignore(self, cid: str, reals: list[str],
                        scope: str) -> tuple[list[str], list[tuple[str, str]]]:
        """delete_forms, then add every one that went to an ignore list. One write each way."""
        scope = "global" if scope in ("global", GLOBAL_ID) else self._check_id(scope)
        removed, refused = self.delete_forms(cid, reals)
        if not removed:
            return [], refused
        try:
            self.add_ignores(removed, scope)             # one write for the lot (feature 57)
            return removed, refused
        except MappingError:
            pass
        # The list would not take them together: one at a time, so each refusal is its own and
        # none is left neither tokenised nor ignored - the guarantee form_to_ignore makes.
        ignored = []
        for real in removed:
            try:
                self.add_ignore(real, scope)
                ignored.append(real)
            except MappingError as exc:
                self.add_form(cid, real, None)
                refused.append((real, str(exc)))
        return ignored, refused

    def restore_forms(self, cid: str, entries: list[dict[str, str]]) -> tuple[list[str], list[tuple[str, str]]]:
        """Put entries back, with the tokens they had. The inverse of a bulk delete or ignore (D42).

        **The token is the point.** `add_forms` derives one, and a derived token is not
        necessarily the token the entry had - a later rule, a different neighbour set, or simply
        a different order gives a different answer. The old token may already be in a document or
        in a colleague's copy of the file, so putting an entry back with a *new* token would
        leave the original unrehydratable. The caller carries the pairs for exactly this reason.

        The removal tombstone goes too: `delete_form` writes one so a merge of two copies does
        not resurrect a deleted entry, and leaving it behind would have a merge delete this again.

        Best effort, not a transaction. Something may have changed in the meantime - the value may
        now be in Global, or another window may have re-added it - so this reports what went back
        and what refused, the same shape the bulk actions themselves use.
        """
        cid = self._check_id(cid)
        path, doc, client = self._client_doc(cid)
        forms = client.setdefault("forms", [])
        have = {f.get("real") for f in forms}
        keep = len(forms)
        wanted: list[tuple[str, str]] = []
        refused: list[tuple[str, str]] = []
        for entry in entries:
            real = str(entry.get("real", "")).strip()
            if not real:
                continue
            if real in have:
                refused.append((real, "it is already back"))
                continue
            wanted.append((real, str(entry.get("token", "")).strip()))
            have.add(real)
        for real, token in wanted:
            self._append_form(cid, client, real, token or None, source=SOURCE_MANUAL)
        removed = [r for r in client.get("removed", []) or []
                   if r.get("real") not in {real for real, _t in wanted}]
        client["removed"] = removed
        try:
            self._validate_then_write(path, doc)
            return [real for real, _t in wanted], refused
        except MappingError as first:
            # One at a time, keeping what validates - the same tolerance delete_forms uses, and
            # for the same reason: one entry that cannot go back must not strand the other
            # twenty-nine.
            log.warning("restoring %d form(s) for %s was refused (%s); keeping the ones that hold",
                        len(wanted), cid, first)
            del forms[keep:]
            back: list[str] = []
            for real, token in wanted:
                self._append_form(cid, client, real, token or None, source=SOURCE_MANUAL)
                try:
                    self._validate_then_write(path, doc)
                    back.append(real)
                except MappingError as exc:
                    refused.append((real, str(exc)))
                    forms[:] = [f for f in forms if f.get("real") != real]
            return back, refused

    def prune_preview(self, cid: str) -> list[tuple[str, str]]:
        """(real, why) for entries today's collection rules would not have collected (defect D5).

        Every collection fix governs what is collected *next*. A client file built before the fix
        keeps what it has, and the only remedy was to delete the client and scan again - which
        loses the entries somebody added by hand along with the junk.

        **Hand-added entries are never listed.** That is the whole reason this is safe to offer,
        and it is what `origin` is for: only a form marked as collected (`scan` or `auto`) is a
        candidate. A form with no origin was typed by a person, who meant it.

        The rules applied are the ones that can be checked against a stored value alone, without
        the file it came from:

        - the shipped public-identifier list has grown since (it is rebuilt from upstream), so a
          value that is public *now* would not be collected now;
        - so has the common-word list;
        - a domain collected by a scan would now go to Review instead (defect D3).

        Policy C - "in a machine-written file, collect a find only if it visibly belongs to a
        client" - cannot be re-applied here, because it is a question about the file the value
        was found in and that is not recorded. A re-scan is the only thing that answers it.
        """
        from .detect import is_common_word, is_public

        cid = self._check_id(cid)
        _path, _doc, client = self._client_doc(cid)
        out: list[tuple[str, str]] = []
        for form in client.get("forms", []) or []:
            if not isinstance(form, dict):
                continue
            real = str(form.get("real", ""))
            origin = str(form.get("origin") or "")
            if not real or origin not in FOUND_ORIGINS:
                continue          # typed by a person: not this tool's to second-guess
            kind = str(form.get("kind") or "") or _safe_kind(real)
            if is_public(real):
                out.append((real, "the public-identifier list now covers it: the same in every tenant"))
            elif is_common_word(real):
                out.append((real, "it is an ordinary word, which the collector now skips"))
            elif kind == "domain" and origin == ORIGIN_SCAN:
                out.append((real, "a scan no longer adds domains; they go to Review (D3)"))
        return out

    def prune(self, cid: str, reals: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
        """Drop the named entries. Deliberately takes an explicit list rather than re-deriving it,
        so what is removed is what was shown and confirmed, not what the rules say a moment later."""
        return self.delete_forms(cid, reals)

    def remove_ignores(self, values: list[str], scope: str = "global") -> list[str]:
        """Take several values off one ignore list. The other half of undoing a bulk ignore."""
        gone = [v for v in values if self.remove_ignore(v, scope)]
        return gone

    def ignores(self) -> dict[str, list[str]]:
        """{"global": [...], "<client id>": [...]} as stored (original case)."""
        out: dict[str, list[str]] = {"global": []}
        if self.global_ignore_path.exists():
            out["global"] = sorted(_read(self.global_ignore_path).get("ignore", []))
        for p in self.client_files():
            c, err = _parse(p)
            if err is not None or not isinstance(c, dict):
                continue  # a broken file has no usable ignores; the status reports it
            cid = c.get("id") or p.stem
            if cid == GLOBAL_ID:
                out["global"] = sorted(set(out["global"]) | set(c.get("ignore", []) or []))
            else:
                out[cid] = sorted(c.get("ignore", []) or [])
        return out

    def add_ignore(self, value: str, scope: str = "global") -> None:
        self.add_ignores([value], scope)

    def add_ignores(self, values: list[str], scope: str = "global") -> None:
        """Add several values to one ignore list in a single read and write (feature 57: a bulk
        ignore wrote the list once per entry)."""
        values = [v.strip() for v in values]
        if not values or not all(values):
            raise MappingError("empty value")
        if scope == "global":
            cur = set(_read(self.global_ignore_path).get("ignore", [])) if self.global_ignore_path.exists() else set()
            cur.update(values)
            _write(self.global_ignore_path, {"ignore": sorted(cur)})
            return
        path, doc, client = self._client_doc(scope)
        if path != self._file_for(scope):
            raise MappingError(f"per-client ignores need {scope} in {self.clients_dir.name}/; run `aicleaner split` first")
        lst = list(client.get("ignore", []) or [])
        lst.extend(v for v in values if v not in lst)
        client["ignore"] = sorted(lst)
        _write_client(path, doc, self.staging_dir, self._key(doc.get("id")))

    def remove_ignore(self, value: str, scope: str = "global") -> bool:
        if scope == "global":
            if not self.global_ignore_path.exists():
                return False
            cur = _read(self.global_ignore_path).get("ignore", [])
            new = [v for v in cur if v.lower() != value.lower()]
            _write(self.global_ignore_path, {"ignore": sorted(new)})
            return len(new) != len(cur)
        path, doc, client = self._client_doc(scope)
        cur = list(client.get("ignore", []) or [])
        new = [v for v in cur if v.lower() != value.lower()]
        client["ignore"] = new
        if self._is_client_path(path):
            _write_client(path, doc, self.staging_dir, self._key(doc.get("id")))
        else:
            _write(path, doc)
        return len(new) != len(cur)

    def archive_client(self, cid: str) -> Path:
        """Move a client out of the active set without losing it.

        clients/<ID>.json becomes clients/_archive/<ID>.deleted-<timestamp>.json (not loaded);
        a legacy single-file client is written to the same place and removed from client-map.json.
        The index stays allocated in _registry.json so its tokens are never reused.
        """
        from datetime import datetime

        path, doc, client = self._client_doc(cid)
        archive = self.clients_dir / "_archive"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / f"{cid}.deleted-{datetime.now():%Y%m%d-%H%M%S}.json"
        if path == self.mapping_path:
            _write(target, client)
            doc["clients"] = [c for c in doc.get("clients", []) if c.get("id") != cid]
            _write(self.mapping_path, doc)
        else:
            path.replace(target)
        return target

    def archived(self) -> list[str]:
        archive = self.clients_dir / "_archive"
        return sorted(p.name for p in archive.glob("*.json")) if archive.is_dir() else []

    # ---- import / export ----------------------------------------------------------------

    def export_client(self, cid: str) -> dict[str, Any]:
        _path, _doc, client = self._client_doc(cid)
        out = {"id": cid, "index": client_index(cid, client.get("index")), "expand_case": bool(client.get("expand_case", False)),
               "forms": client.get("forms", []), "ignore": client.get("ignore", []) or []}
        return out

    def export_all(self) -> dict[str, Any]:
        loaded, _reports = self.load_tolerant(record=False)
        return {"version": 1, "clients": [self.export_client(c["id"]) for c in loaded.data["clients"]]}

    def import_client(self, doc: dict[str, Any], mode: str = "merge") -> dict[str, Any]:
        """Validate and write a client document into clients/ (DECISIONS 44). Returns a report.

        new: refused when the id is held. merge: forms and ignores united, the local form kept for a
        value both hold, gaps in it filled from the import, a differing token reported not taken.
        replace: the import wins. Every field of the document is kept - it used to keep only real
        and token, so even a round trip lost origins, match rules and the primary repo. Merge and
        replace back the existing file up first.
        """
        if not isinstance(doc, dict) or not doc.get("id") or not isinstance(doc.get("forms", []), list):
            raise MappingError("not a client document: needs id and forms[]")
        cid = self._check_id(doc["id"])
        idx = client_index(cid, doc.get("index"))
        loaded, _reports = self.load_tolerant(record=False)
        reg = self.registry()
        owner = reg.get(str(idx))
        if owner and owner != cid:
            raise MappingError(f"index {idx} already belongs to {owner}; the import would make {cid} collide (rename one of them or set a different index)")
        for c in loaded.data["clients"]:
            if c["id"] != cid:
                try:
                    if client_index(c["id"], c.get("index")) == idx:
                        raise MappingError(f"index {idx} is used by {c['id']} in {loaded.sources[c['id']].name}")
                except MappingError:
                    raise
        exists = cid in loaded.sources
        imported = _importable(doc, cid, idx)
        if exists and mode == "new":
            # Held already: skipped when every form it brings is here with the same token (the user,
            # 2026-09-30) - a bundle that includes clients already held imports the rest - and
            # refused when it would change something, which is merge's or replace's job.
            _path, _whole, current = self._client_doc(cid)
            held = {(f.get("real"), f.get("token")) for f in (current or {}).get("forms", []) or []}
            held_reals = {real for real, _token in held}
            if all((f["real"], f["token"]) in held if f.get("token") else f["real"] in held_reals
                   for f in imported["forms"]):
                return {"id": cid, "index": idx, "mode": "skipped", "added": 0, "conflicts": []}
            raise MappingError(f"client {cid} already exists and differs; choose merge or replace to import into it")
        report: dict[str, Any] = {"id": cid, "index": idx, "mode": mode if exists else "new",
                                  "added": len(imported["forms"]), "conflicts": []}
        entry = imported
        if exists:
            _path, _whole, current = self._client_doc(cid)
            current = {k: v for k, v in dict(current or {}).items() if k != stamp.KEY}
            report["backup"] = str(self._backup(cid, current))
            if mode == "merge":
                entry, report["added"], report["conflicts"] = _merged(current, imported)
        if exists and loaded.sources[cid] == self.mapping_path:
            legacy = _read(self.mapping_path)
            legacy["clients"] = [c for c in legacy.get("clients", []) if c.get("id") != cid]
            _write(self.mapping_path, legacy)
        self.clients_dir.mkdir(parents=True, exist_ok=True)
        self._register(cid, idx)
        self._validate_then_write(self._file_for(cid), entry)
        return report

    def _backup(self, cid: str, current: dict[str, Any]) -> Path:
        """A copy of a client file before an import changes it. Local state, never the clients folder."""
        folder = (self.state_dir or self.mapping_path.parent) / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{cid}-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def import_bundle(self, doc: dict[str, Any], mode: str = "merge") -> list[dict[str, Any]]:
        """Import one client document or a bundle of them. mode: new | merge (default) | replace."""
        if mode not in ("new", "merge", "replace"):
            raise MappingError("mode must be new, merge or replace")
        if isinstance(doc, dict) and isinstance(doc.get("clients"), list):
            return [self.import_client(c, mode) for c in doc["clients"]]
        return [self.import_client(doc, mode)]

    # ---- migration ------------------------------------------------------------------

    def split(self) -> list[Path]:
        """Move every client in client-map.json into clients/<ID>.json; keeps tokens verbatim.

        The legacy file is renamed to client-map.json.migrated so nothing is loaded twice.
        """
        if not self.mapping_path.exists():
            raise MappingError(f"{self.mapping_path} not found; nothing to split")
        legacy = _read(self.mapping_path)
        written: list[Path] = []
        for c in legacy.get("clients", []):
            cid = c["id"]
            target = self._file_for(cid)
            if target.exists():
                raise MappingError(f"{target.name} already exists; resolve by hand")
            idx = client_index(cid, c.get("index")) if (c.get("index") is not None or any(ch.isdigit() for ch in cid)) else None
            entry = {"id": cid, "expand_case": bool(c.get("expand_case", False)), "forms": c.get("forms", []), "ignore": []}
            if idx is not None:
                entry["index"] = idx
                self._register(cid, idx)
            _write_client(target, entry, self.staging_dir, self._key(entry.get("id")))
            written.append(target)
        self.mapping_path.replace(self.mapping_path.with_suffix(".json.migrated"))
        self.mapper()  # validate the merged result
        return written
