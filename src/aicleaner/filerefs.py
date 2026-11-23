"""f188 (Brad, 2026-11-20): which file names and paths in the Session transcript are files in the session's folders.

The page finds what looks like a file reference in a reply or a prompt - an absolute path, a relative one, or a bare
name with an extension - and asks here, in batches, which are real. Only an existing file inside the Files panel's own
allow-list is ever an answer: every match goes through `Session._locate`, the one check the tree, the file tabs and
the downloads use (DECISIONS 50, D245, D171), so a link can never open what a double-click in the tree could not.

A bare name is looked up in a per-session index of names, built by walking the session's folders breadth first,
bounded (depth, file count, time per request - a walk out of time carries on at the next request) and skipping
dependency folders and git's database as the tree search does. The index is kept for a while and rebuilt when the
page says its Files tree changed. Display only: nothing here changes the transcript or what Claude sees.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .fileio import network_path

if TYPE_CHECKING:
    from .session import Session

MAX_REFS = 200                 # one request's batch
MAX_REF_CHARS = 400            # longer than any real path a reply names
MAX_MATCHES = 20               # of one name; the chooser lists them
INDEX_DEPTH = 12               # folders below a root
INDEX_FILES = 200_000          # names held per session
INDEX_BUDGET_S = 3.0           # walking per request; the next request carries on
INDEX_TTL_S = 600.0            # an index this old is walked again
FRESH_GRACE_S = 2.0            # two pages saying "the tree changed" at once rebuild once

# A line reference after a path - "session.js:630", "notes.md:12-20", "x.py#L4" - is not part of the file's name.
LINE_SUFFIX = re.compile(r"(?::\d+(?:[-:]\d+)?|#L\d+(?:-L?\d+)?)$")
DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
BARE_NAME = re.compile(r"^[^\\/:*?\"<>|]+\.[A-Za-z][A-Za-z0-9_-]{0,9}$")


def _normcase(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _under(literal: Path, root: Path) -> str | None:
    """`literal`'s path under `root` (posix), "" for the root itself, or None when it is not under it. Compared as the
    filesystem compares (case-insensitive on Windows), on the path as written - not where a link leads."""
    lit, base = _normcase(literal), _normcase(root)
    if lit == base:
        return ""
    prefix = base if base.endswith(os.sep) else base + os.sep
    if not lit.startswith(prefix):
        return None
    return str(os.path.normpath(str(literal)))[len(prefix):].replace("\\", "/")


class NameIndex:
    """Every file's name in a session's folders, lower-cased, to the places it is: [(root, file)].

    Breadth first across all the roots together (as SearchWalk), so the top of every folder is known before the depths
    of any; never into a link or junction, a dependency folder or git's database. `advance` walks for a time and stops;
    the next call carries on."""

    def __init__(self, roots: list[Path], depth: int = INDEX_DEPTH, max_files: int = INDEX_FILES):
        self.roots = list(roots)
        self.key = tuple(_normcase(r) for r in self.roots)
        self.depth = depth
        self.max_files = max_files
        self.names: dict[str, list[tuple[Path, Path]]] = {}
        self.files = 0
        self.pending: deque[tuple[Path, Path, int]] = deque((root, root, 0) for root in self.roots)
        self.built_at = time.monotonic()
        self.lock = threading.Lock()

    @property
    def complete(self) -> bool:
        return not self.pending or self.files >= self.max_files

    def advance(self, budget_s: float = INDEX_BUDGET_S) -> bool:
        """Walk until done or out of time; True when the whole tree (to the limits) is indexed."""
        from .session import _dependency_folder

        deadline = time.monotonic() + budget_s
        while self.pending and self.files < self.max_files:
            root, folder, level = self.pending.popleft()
            try:
                with os.scandir(folder) as listed:
                    items = list(listed)
            except OSError:
                continue
            for item in items:
                try:
                    if item.is_dir(follow_symlinks=False):
                        if level + 1 <= self.depth and not item.is_symlink() and not item.is_junction() \
                                and not _dependency_folder(Path(item.path)):
                            self.pending.append((root, Path(item.path), level + 1))
                        continue
                    if not item.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if self.files >= self.max_files:
                    break
                self.names.setdefault(item.name.lower(), []).append((root, Path(item.path)))
                self.files += 1
            if time.monotonic() > deadline:
                break
        return self.complete

    def lookup(self, name: str) -> list[tuple[Path, Path]]:
        return list(self.names.get(name.lower(), ()))


def clean_ref(ref: str) -> str:
    """The file a reference names: quotes, a line reference and a leading ./ dropped."""
    text = str(ref or "").strip().strip("'\"\u2018\u2019\u201c\u201d")
    text = LINE_SUFFIX.sub("", text)
    while text.startswith(("./", ".\\")):
        text = text[2:]
    return text


def ref_kind(ref: str) -> str:
    """'absolute', 'relative', 'bare', or '' for something that is not a file reference this resolves."""
    if not ref or len(ref) > MAX_REF_CHARS or "\x00" in ref or "://" in ref:
        return ""
    if network_path(ref) or ref.startswith(("\\\\", "//")):
        return ""                 # never resolved: resolving a UNC path authenticates to its host (H27)
    if DRIVE_PATH.match(ref):
        return "absolute"
    if ref.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", ref):
        return ""                 # rooted without a drive, or drive-relative: neither says where
    if "/" in ref or "\\" in ref:
        return "relative"
    return "bare" if BARE_NAME.match(ref) else ""


class Resolver:
    """One batch's answers for one session: its browse-only Session (the allow-list), its roots, its working folder
    and its name index."""

    def __init__(self, browser: "Session", roots: list[Path], cwd: Path | None, index: NameIndex | None):
        self.browser = browser
        self.roots = list(roots)
        self.cwd = cwd
        self.index = index

    def match(self, literal: Path) -> dict[str, Any] | None:
        """The file at `literal` as the tree would name it - {root, path, name, view, full} - when it is an existing
        file inside the allowed folders; else None. The allow-list is `_locate`'s, nothing of its own."""
        from .session import SessionError, file_kind

        literal = Path(os.path.normpath(str(literal)))
        for root in self.roots:
            relative = _under(literal, root)
            if not relative:
                continue
            try:
                _roots, _chosen, target = self.browser._locate(str(root), relative)
                if not target.is_file():
                    return None
            except (SessionError, OSError, ValueError):
                return None
            return {"root": str(root), "path": relative, "name": literal.name, "view": file_kind(literal.name)[1],
                    "full": str(literal)}
        return None

    def _first_places(self) -> list[Path]:
        places = [self.cwd] if self.cwd is not None else []
        return places + [r for r in self.roots if self.cwd is None or _normcase(r) != _normcase(self.cwd)]

    def resolve(self, ref: str) -> list[dict[str, Any]]:
        text = clean_ref(ref)
        kind = ref_kind(text)
        if kind == "absolute":
            found = self.match(Path(text))
            return [found] if found else []
        if kind == "relative":
            return self._relative(text)
        if kind == "bare":
            return self._bare(text)
        return []

    def _relative(self, text: str) -> list[dict[str, Any]]:
        parts = [p for p in re.split(r"[\\/]+", text) if p]
        if not parts:
            return []
        if self.cwd is not None:
            found = self.match(self.cwd.joinpath(*parts))
            if found:
                return [found]                         # the working folder first: that is what Claude meant
        out: list[dict[str, Any]] = []
        for root in self.roots:
            self._add(out, self.match(root.joinpath(*parts)))
        if out or ".." in parts:
            return out[:MAX_MATCHES]
        # "web/js/markdown.js" said from a folder above it: a file of that name whose path ends so.
        tail = "/" + "/".join(parts).lower()
        for _root, file in self._indexed(parts[-1]):
            if ("/" + str(file).replace("\\", "/").lower()).endswith(tail):
                self._add(out, self.match(file))
        return out[:MAX_MATCHES]

    def _bare(self, name: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for place in self._first_places():
            self._add(out, self.match(place / name))
        for _root, file in self._indexed(name):
            if len(out) >= MAX_MATCHES:
                break
            self._add(out, self.match(file))
        return out[:MAX_MATCHES]

    def _indexed(self, name: str) -> list[tuple[Path, Path]]:
        return self.index.lookup(name) if self.index is not None else []

    @staticmethod
    def _add(out: list[dict[str, Any]], found: dict[str, Any] | None) -> None:
        if found and all(_normcase(f["full"]) != _normcase(found["full"]) for f in out):
            out.append(found)


def wanted(refs: Any) -> list[str]:
    """The batch as asked: strings, each once, at most MAX_REFS, none longer than MAX_REF_CHARS."""
    out: list[str] = []
    for ref in refs if isinstance(refs, list) else []:
        if isinstance(ref, str) and 0 < len(ref) <= MAX_REF_CHARS and ref not in out:
            out.append(ref)
        if len(out) >= MAX_REFS:
            break
    return out


class RefIndexes:
    """Each session's name index, kept for INDEX_TTL_S and rebuilt when its folders change or the page says the Files
    tree changed (`fresh`). Thread-safe: requests run off the event loop."""

    def __init__(self, ttl_s: float = INDEX_TTL_S, budget_s: float = INDEX_BUDGET_S, depth: int = INDEX_DEPTH,
                 max_files: int = INDEX_FILES):
        self.ttl_s = ttl_s
        self.budget_s = budget_s
        self.depth = depth
        self.max_files = max_files
        self._by_name: dict[str, NameIndex] = {}
        self._lock = threading.Lock()

    def get(self, name: str, roots: list[Path], fresh: bool = False) -> NameIndex:
        key = tuple(_normcase(r) for r in roots)
        now = time.monotonic()
        with self._lock:
            index = self._by_name.get(name)
            stale = (index is None or index.key != key or now - index.built_at > self.ttl_s
                     or (fresh and now - index.built_at > FRESH_GRACE_S))
            if stale:
                index = NameIndex(roots, depth=self.depth, max_files=self.max_files)
                self._by_name[name] = index
            return index

    def forget(self, name: str | None = None) -> None:
        with self._lock:
            if name is None:
                self._by_name.clear()
            else:
                self._by_name.pop(name, None)


def resolve_refs(browser: "Session", roots: list[Path], cwd: Path | None, refs: list[str],
                 index: NameIndex | None, budget_s: float = INDEX_BUDGET_S) -> dict[str, Any]:
    """{matches: {ref: [match, ...]} for every ref with at least one, complete: whether the name index is whole}.
    The index is walked (for up to `budget_s`) only when a bare name or an unfound relative path needs it."""
    asked = wanted(refs)
    needs_index = any(ref_kind(clean_ref(r)) in ("bare", "relative") for r in asked)
    if index is None or not needs_index:
        resolver = Resolver(browser, roots, cwd, index if index is not None and index.complete else None)
        return _answer(resolver, asked, index.complete if index is not None else True)
    with index.lock:
        index.advance(budget_s)
        return _answer(Resolver(browser, roots, cwd, index), asked, index.complete)


def _answer(resolver: Resolver, asked: list[str], complete: bool) -> dict[str, Any]:
    matches: dict[str, list[dict[str, Any]]] = {}
    for ref in asked:
        found = resolver.resolve(ref)
        if found:
            matches[ref] = found
    return {"matches": matches, "complete": complete}
