"""Git kept out of OneDrive (feature 186): the check before a start, the target check and the move plan.

A project session's project assets folder is in OneDrive. Git in there is trouble: a repository is thousands of small
files that change together, OneDrive syncs them one by one, locks files Git is writing, and a half-synced object store
is a corrupted repository. Before a start the assets folder is scanned for `.git` anywhere inside it (`scan`); when one
is there the page asks what to do, and one answer is to move the repositories to a folder outside OneDrive. This module
works out that move without writing anything (`plan_move`); `gitmigrate` carries it out.

The scan has a time budget, because it runs while someone waits for Start: a scan cut off is "unknown", never "clean".
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .fileio import network_path
from .folders import FolderError, FolderOutside, blocked_reason, is_usable
from .shares import in_onedrive as _in_onedrive_env

SCAN_BUDGET_S = 2.0             # Start is never held longer than this by the check (Brad, 2026-11-18)
BACKGROUND_BUDGET_S = 120.0     # a cut-off scan finishes in the background, within this
BACKGROUND_KEEP_S = 600.0       # ... and its complete answer stands for the next start this long
EDITOR_KEEP_S = 60.0            # the editor's warning asks at most this often per folder
GIT_TIMEOUT_S = 120
CLASH_LIST_MAX = 50             # clashes listed to the page; the count is always the whole number
NOTE_NAME = "where did my files go.md"
CHOICES = ("remind", "ignore")


class MoveError(ValueError):
    """A move that cannot be planned or done, in words for the page."""


# ---- where OneDrive is -------------------------------------------------------------------------


def in_onedrive(path: str | Path) -> bool:
    """Inside one of this user's OneDrive folders (the environment's), or any path with a segment containing
    "onedrive" - another account's folder, or one synced under another name (Brad: "not containing the word
    OneDrive")."""
    text = str(path or "").strip()
    if not text:
        return False
    if any("onedrive" in part.lower() for part in Path(text).parts):
        return True
    try:
        return _in_onedrive_env(text)
    except (OSError, ValueError):
        return False


def migrate_possible(roots: list[Path]) -> bool:
    """Whether any allowed folder lies outside OneDrive - somewhere a repository may be moved to."""
    return any(not in_onedrive(root) for root in roots or [])


# ---- the scan ----------------------------------------------------------------------------------
#
# The first check of a folder lists every directory in it (a full scan) and keeps, in AI Cleaner's state folder, each
# directory's modified time, its sub-directory names and the `.git` entries it holds. Later checks walk that tree:
# a directory whose modified time is what was kept is not listed again (one stat instead of one listing); a changed
# or new one is listed afresh. On NTFS a directory's modified time changes whenever an entry is created, deleted or
# renamed in it, so a `.git` made anywhere changes its own folder's time, and a folder moved or copied in changes its
# new parent's - and, being new at that place, is scanned in full. The time is read from the directory itself
# (`lstat`), never from its parent's listing: NTFS does not update the copy held in the parent's index.

CACHE_VERSION = 3                # 2: full_s (1.3.7, Brad's options 1 + 2); 3: .git kinds dir / file / skeleton (D259)
CACHE_FOLDER = "git-scan"
REBASELINE_S = 7 * 24 * 3600.0  # a full scan at least this often, whatever the kept tree says
# Only a folder whose full scan takes longer than this is checked incrementally; a quicker one is scanned in full every
# time - exact, because entries OneDrive syncs down from another device do not change their folder's times.
INCREMENTAL_FROM_S = 0.5
RACY_NS = 2_000_000_000          # a directory changed this recently is listed again next time (see `_walk`)
_scandir = os.scandir            # tests count the listings through this
_lstat = os.lstat
_LINK_TAGS = {getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
              getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)}


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        junction = getattr(entry, "is_junction", None)
        return bool(junction and junction())
    except OSError:
        return True                                   # unreadable: never followed


def _stat_is_link(st: os.stat_result) -> bool:
    """A symbolic link or a junction (a OneDrive placeholder's cloud reparse tag is neither)."""
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_TAGS


def _listing(path: str) -> tuple[list[str], list[str], int] | None:
    """(sub-directory names, the kinds of its `.git` entries, entries that could not be read), or None when the
    directory cannot be listed. Links and junctions are left out; `.git` is never a sub-directory to walk."""
    try:
        with _scandir(path) as found:
            entries = list(found)
    except OSError:
        return None
    subs: list[str] = []
    gits: list[str] = []
    unreadable = 0
    for entry in entries:
        if entry.name.lower() == ".git":
            gits.append(git_kind(entry.path))
            continue
        if _is_link(entry):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                subs.append(entry.name)
        except OSError:
            unreadable += 1
    return subs, gits, unreadable


def git_kind(dot_git: str | Path) -> str:
    """What a `.git` entry is (D259): "dir" - a repository (a `.git` folder holding a HEAD file); "file" - a linked
    worktree or a submodule (a `.git` file starting with "gitdir:"); "skeleton" - neither: an empty `.git` folder tree
    OneDrive keeps after a repository was moved out (no HEAD), or a `.git` file that names no Git data. Git itself
    cannot open a skeleton, so it is reported as "safe to delete" and never blocks Start."""
    path = str(dot_git)
    try:
        st = _lstat(path)
    except OSError:
        return "skeleton"
    if stat.S_ISDIR(st.st_mode) and not _stat_is_link(st):
        return "dir" if os.path.isfile(os.path.join(path, "HEAD")) else "skeleton"
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
    except OSError:
        return "skeleton"
    return "file" if head.lstrip(b"\xef\xbb\xbf").lstrip().lower().startswith(b"gitdir:") else "skeleton"


def _walk(root: Path, budget_s: float, clock: Callable[[], float], previous: dict[str, list] | None,
          record: bool) -> tuple[dict[str, Any], dict[str, list]]:
    """Breadth first from `root`. With `previous` (rel -> [mtime_ns, sub-directory names, .git kinds]) a directory
    whose modified time is unchanged is taken from it unlisted; with `record` each directory's entry is returned for
    keeping. Returns (the scan's answer, the tree)."""
    started = clock()
    queue: deque[tuple[str, str]] = deque([("", str(root))])
    tree: dict[str, list] = {}
    repos: list[dict[str, str]] = []
    skeletons: list[dict[str, str]] = []
    checked = listed = unreadable = 0
    complete = True
    while queue:
        if clock() - started > budget_s:
            complete = False
            break
        rel, path = queue.popleft()
        checked += 1
        mtime: int | None = None
        if record or previous is not None:
            try:
                st = _lstat(path)
                if rel and (_stat_is_link(st) or not stat.S_ISDIR(st.st_mode)):
                    continue                          # turned into a link (or a file) since it was listed
                mtime = st.st_mtime_ns
            except OSError:
                mtime = None                          # listed afresh below; unreadable there too -> counted
        kept = previous.get(rel) if previous is not None and mtime is not None else None
        if kept is not None and kept[0] == mtime:
            subs = kept[1]
            # D259: a kept `.git` is looked at again (a stat or two) - a skeleton made a repository again (HEAD written
            # inside it) changes the `.git` folder's time, not this directory's.
            gits = [git_kind(os.path.join(path, ".git")) for _kind in kept[2]]
        else:
            got = _listing(path)
            if got is None:
                unreadable += 1
                continue                              # not kept: listed again next time
            subs, gits, bad = got
            listed += 1
            unreadable += bad
        if record and mtime is not None:
            # Racy: NTFS stamps times from a clock that moves in steps of up to ~16 ms, so a change made just after
            # this listing can leave the same time behind. A directory changed in the last two seconds is kept with
            # time 0, which never matches - it is listed again next time (git's "racy" rule, for the same reason).
            fresh = time.time_ns() - mtime < RACY_NS
            tree[rel] = [0 if fresh else mtime, subs, gits]
        for kind in gits:
            (skeletons if kind == "skeleton" else repos).append({"path": path, "rel": rel, "kind": kind})
        for name in subs:
            queue.append((f"{rel}/{name}" if rel else name, os.path.join(path, name)))
    repos.sort(key=lambda r: (r["rel"].count("/"), r["rel"].lower()))
    skeletons.sort(key=lambda r: (r["rel"].count("/"), r["rel"].lower()))
    answer = {"folder": str(root), "repos": repos, "skeletons": skeletons, "complete": complete, "folders": checked, "listed": listed,
              "unreadable": unreadable, "elapsed": round(clock() - started, 3), "budget": budget_s}
    return answer, tree


def scan(folder: str | Path, budget_s: float = SCAN_BUDGET_S,
         clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Every `.git` (folder or file) anywhere under `folder`, breadth first, within `budget_s` - a full scan, nothing
    kept. `repos` are real Git (`git_kind` "dir" / "file"); `skeletons` the empty `.git` folders left behind (D259).

    A `.git` file is a linked worktree or a submodule. Never descends into `.git`, never follows a link or a junction
    (OneDrive's own folder can be one), and goes on below a repository so nested ones are found. Over budget it stops:
    `complete` False, and how many folders it did check."""
    answer, _tree = _walk(Path(folder), budget_s, clock, None, record=False)
    return answer


# ---- the kept tree (the state folder, never OneDrive) --------------------------------------------------------------

_volumes: dict[str, bool] = {}


def _volume_keeps_dir_times(folder: str | Path) -> bool:
    """Whether a directory's modified time can be trusted to change with its entries: a local NTFS or ReFS volume.
    FAT/exFAT do not update it, and a network share's times are the server's - those are always scanned in full."""
    text = str(folder)
    if network_path(text):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        volume = ctypes.create_unicode_buffer(1024)
        if not kernel.GetVolumePathNameW(text, volume, 1024):
            return False
        root = volume.value
        if root in _volumes:
            return _volumes[root]
        fs_name = ctypes.create_unicode_buffer(64)
        ok = (kernel.GetDriveTypeW(root) != 4             # DRIVE_REMOTE: a mapped network drive
              and kernel.GetVolumeInformationW(root, None, 0, None, None, None, fs_name, 64)
              and fs_name.value.upper() in ("NTFS", "REFS"))
        _volumes[root] = bool(ok)
        return bool(ok)
    except (OSError, AttributeError, ValueError):
        return False


def cache_file(cache_dir: str | Path, folder: str | Path) -> Path:
    """Where the tree kept for `folder` lives: `<state>/git-scan/<hash of the folder's path>.json`."""
    digest = hashlib.sha256(_key(folder).encode("utf-8")).hexdigest()[:32]
    return Path(cache_dir) / CACHE_FOLDER / f"{digest}.json"


def _load_tree(cache_dir: str | Path, folder: str | Path,
               now: float) -> tuple[dict[str, list] | None, float, float, str]:
    """(the kept tree or None, when its full scan was, how long that full scan took, why a full scan is needed - ''
    when none is)."""
    path = cache_file(cache_dir, folder)
    if not path.is_file():
        return None, 0.0, 0.0, "first check"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("version") != CACHE_VERSION:
            return None, 0.0, 0.0, "kept tree from another version"
        if doc.get("folder_key") != _key(folder):
            return None, 0.0, 0.0, "kept tree is for another folder"
        baseline = float(doc["baseline"])
        full_s = float(doc["full_s"])
        dirs = doc["dirs"]
        if not isinstance(dirs, dict) or not isinstance(dirs.get(""), list):
            return None, 0.0, 0.0, "kept tree unreadable"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, 0.0, 0.0, "kept tree unreadable"
    if not 0 <= now - baseline < REBASELINE_S:
        return None, 0.0, 0.0, "re-baseline (7 days)"
    if full_s <= INCREMENTAL_FROM_S:
        return None, baseline, full_s, "quick folder: a full scan every check"
    return dirs, baseline, full_s, ""


def _save_tree(cache_dir: str | Path, folder: str | Path, tree: dict[str, list], baseline: float, full_s: float,
               answer: dict[str, Any]) -> None:
    path = cache_file(cache_dir, folder)
    doc = {"version": CACHE_VERSION, "folder": str(folder), "folder_key": _key(folder), "baseline": baseline,
           "full_s": round(full_s, 3), "completed": time.time(), "found_git": bool(answer["repos"]), "dirs": tree}
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)                   # a tree not kept only means a full scan next time


def _usable_tree(previous: dict[str, list] | None) -> dict[str, list] | None:
    """The kept tree with every malformed entry dropped (such a directory is listed afresh)."""
    if previous is None:
        return None
    good: dict[str, list] = {}
    for rel, kept in previous.items():
        if (isinstance(kept, list) and len(kept) == 3 and isinstance(kept[0], int) and isinstance(kept[1], list)
                and isinstance(kept[2], list)):
            good[rel] = kept
    return good


def scan_kept(folder: str | Path, cache_dir: str | Path | None, budget_s: float = SCAN_BUDGET_S,
              clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time,
              full: bool = False) -> dict[str, Any]:
    """The scan, incremental when it can be: with a tree kept from an earlier complete check of `folder` (in
    `cache_dir`, at most 7 days since its full scan, the same version) whose full scan took more than
    INCREMENTAL_FROM_S, only changed or new directories are listed. Otherwise - `full` asked for, no cache folder, no
    tree, an unreadable one, a quick folder, a volume whose directory times cannot be trusted - a full scan. A complete
    answer's tree is kept for the next check; a cut-off one keeps nothing and is "unknown" as ever."""
    root = Path(folder)
    if cache_dir is None:
        return {**scan(root, budget_s, clock), "mode": "full", "why": "no state folder"}
    if not _volume_keeps_dir_times(root):
        return {**scan(root, budget_s, clock), "mode": "full", "why": "folder times not trusted on this volume"}
    at = now()
    previous, baseline, full_s, why = _load_tree(cache_dir, root, at)
    if full and previous is not None:
        previous, why = None, "full scan asked for (the background check)"
    answer, tree = _walk(root, budget_s, clock, _usable_tree(previous), record=True)
    if answer["complete"]:
        if previous is None:
            # A full scan starts a new 7 days and records how long a full scan of this folder takes - which decides
            # whether its later checks are incremental.
            _save_tree(cache_dir, root, tree, at, answer["elapsed"], answer)
        elif answer["listed"] or len(tree) != len(previous):
            # Nothing listed means nothing changed: every directory was taken from the kept tree as it was (a folder
            # removed or added changes its parent, which is then listed), so the file is left as it is.
            _save_tree(cache_dir, root, tree, baseline, full_s, answer)
    return {**answer, "mode": "incremental" if previous is not None else "full", "why": why}


def _rel(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return str(path)
    text = rel.as_posix()
    return "" if text == "." else text


# ---- scans kept for a moment (the editor) and finished in the background (a cut-off start) -----

_lock = threading.Lock()
_kept: dict[str, tuple[float, float, dict[str, Any]]] = {}     # key -> (when, keep_s, result)
_running: set[str] = set()


def _key(folder: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(folder)))


def forget(folder: str | Path) -> None:
    """Drop what is known about a folder (after a move, its answer has changed)."""
    with _lock:
        _kept.pop(_key(folder), None)


def _keep(folder: str | Path, result: dict[str, Any], keep_s: float) -> None:
    with _lock:
        _kept[_key(folder)] = (time.monotonic(), keep_s, result)


def _known(folder: str | Path) -> dict[str, Any] | None:
    with _lock:
        hit = _kept.get(_key(folder))
    if hit and time.monotonic() - hit[0] < hit[1]:
        return dict(hit[2])
    return None


def _lower_priority() -> None:
    """This thread at background priority (CPU and disk), so a full scan behind a start never competes with it."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetThreadPriority(kernel.GetCurrentThread(), 0x00010000)     # THREAD_MODE_BACKGROUND_BEGIN
    except (OSError, AttributeError):
        pass


def _finish_in_background(folder: str | Path, cache_dir: str | Path | None = None) -> None:
    """A FULL scan of `folder` in the background (one per folder at a time, background priority, within 120 s): it
    finishes a cut-off check, and after an incremental one it catches what the folder times cannot show - a `.git`
    OneDrive synced down from another device. Its complete answer stands for 10 minutes and re-baselines the kept
    tree."""
    key = _key(folder)
    with _lock:
        if key in _running:
            return
        _running.add(key)

    def work() -> None:
        try:
            _lower_priority()
            result = scan_kept(folder, cache_dir, budget_s=BACKGROUND_BUDGET_S, full=True)
            if result["complete"]:
                _keep(folder, {**result, "background": True}, BACKGROUND_KEEP_S)
        except Exception:  # noqa: BLE001 - a background answer is a convenience; the next start scans again
            pass
        finally:
            with _lock:
                _running.discard(key)

    threading.Thread(target=work, name="git-onedrive-scan", daemon=True).start()


def background_running(folder: str | Path) -> bool:
    """Whether a background scan of `folder` is still running."""
    with _lock:
        return _key(folder) in _running


def check_for_start(folder: str | Path, budget_s: float = SCAN_BUDGET_S,
                    cache_dir: str | Path | None = None) -> dict[str, Any]:
    """The check a start makes: a fresh scan within the budget - incremental when a tree is kept in `cache_dir` (the
    state folder), see `scan_kept`. An incremental answer is given at once and a full scan follows in the background
    (a `.git` synced down from another device leaves no trace in the folder times). Cut off, a complete answer from an
    earlier background scan stands in when there is one, and a background scan is started too (when none is running),
    so a large folder's answer - and its kept tree - is ready for the starts after this one."""
    result = scan_kept(folder, cache_dir, budget_s=budget_s)
    if result["complete"]:
        _keep(folder, result, EDITOR_KEEP_S)
        if result.get("mode") == "incremental":
            _finish_in_background(folder, cache_dir)
        return result
    known = _known(folder)
    _finish_in_background(folder, cache_dir)
    if known and known.get("complete"):
        return known
    return result


def outer_folders(folders: list[str | Path]) -> list[str]:
    """f187: the folders to scan for a session - each once, and none that lies inside another (the outer scan covers
    it). In the order given."""
    out: list[str] = []
    for folder in folders:
        text = str(folder).strip()
        if not text or any(_key(text) == _key(known) for known in out):
            continue
        out.append(text)
    keys = [Path(_key(f)) for f in out]
    return [f for f, k in zip(out, keys) if not any(other != k and k.is_relative_to(other) for other in keys)]


def check_folders(folders: list[str | Path], budget_s: float = SCAN_BUDGET_S, cache_dir: str | Path | None = None,
                  editor: bool = False) -> dict[str, Any]:
    """f187: the check over every OneDrive folder of a session (Brad: "Same .git checks apply across all OneDrive
    folders"), sharing one time limit - each folder gets what is left of it (at least a quarter of a second). Each
    folder keeps its own incremental tree and background finish (1.3.7). The answer: `checked` (one result per folder,
    as `check_for_start` gives it), `repos` (all of them, each with its `root`), `skeletons` (D259: empty `.git`
    folders left behind, each with its `root` - never a reason to hold Start), `complete` (every folder complete),
    `folders` (how many folders were looked at in all), `budget`, and `first` - the first folder holding Git, or cut
    off (the one Migrate acts on)."""
    started = time.monotonic()
    checked: list[dict[str, Any]] = []
    for folder in outer_folders(folders):
        left = max(0.25, budget_s - (time.monotonic() - started))
        result = check_for_editor(folder, left, cache_dir) if editor else check_for_start(folder, left, cache_dir)
        checked.append({**result, "folder": str(folder)})
    repos = [{**repo, "root": item["folder"]} for item in checked for repo in item.get("repos") or []]
    skeletons = [{**dot, "root": item["folder"]} for item in checked for dot in item.get("skeletons") or []]
    first = next((item["folder"] for item in checked if item.get("repos")), "") or next(
        (item["folder"] for item in checked if not item.get("complete")), "")
    return {"checked": checked, "repos": repos, "skeletons": skeletons, "complete": all(item.get("complete") for item in checked),
            "folders": sum(int(item.get("folders") or 0) for item in checked), "budget": budget_s, "first": first}


def check_for_editor(folder: str | Path, budget_s: float = 1.0,
                     cache_dir: str | Path | None = None) -> dict[str, Any]:
    """The editor's warning: what was found within the last minute, else a scan (shorter budget - nothing waits on
    it, and a cut-off one is finished in the background)."""
    known = _known(folder)
    if known is not None:
        return known
    return check_for_start(folder, budget_s, cache_dir)


# ---- the target ---------------------------------------------------------------------------------


def _resolved(path: str | Path) -> Path:
    try:
        return Path(str(path).strip().strip('"')).expanduser().resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise FolderError(f"not a usable path: {path}") from exc


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_target(target: str | Path, share: str | Path, roots: list[Path]) -> Path:
    """The folder the repositories may move to, resolved, or FolderError saying why not: in OneDrive, inside the
    assets folder, a network path, a system folder, or outside the allowed folders (FolderOutside). It need not
    exist yet; it may not be a file."""
    text = str(target or "").strip()
    if not text:
        raise FolderError("choose a folder to move the repositories to")
    if network_path(text):
        raise FolderError("a network or device path cannot take the repositories - choose a folder on this machine")
    if in_onedrive(text):
        raise FolderError(f"{text} is in OneDrive - choose a folder outside OneDrive, or the move gains nothing")
    resolved = _resolved(text)
    if in_onedrive(resolved):
        raise FolderError(f"{text} leads into OneDrive ({resolved}) - choose a folder outside OneDrive")
    if share and _within(resolved, _resolved(share)):
        raise FolderError(f"{resolved} is inside the project assets folder - choose a folder outside OneDrive")
    why = blocked_reason(resolved)
    if why:
        raise FolderError(why)
    if not roots:
        raise FolderOutside("no folders are allowed yet, so there is nowhere to move the repositories to. Add one "
                            "on this machine under Settings > Remote access > Folders the pages can open.")
    if not is_usable(resolved, roots):
        raise FolderOutside(f"{resolved} is outside the folders these pages may reach "
                            f"({', '.join(str(r) for r in roots)})")
    if resolved.exists() and not resolved.is_dir():
        raise FolderError(f"{resolved} is a file, not a folder")
    return resolved


def free_subfolder(target: Path, name: str) -> Path:
    """`<target>/<name>`, or `<name>-2`, `-3`... - the first that does not exist."""
    base = name.strip() or "repos"
    candidate = target / base
    number = 2
    while candidate.exists():
        candidate = target / f"{base}-{number}"
        number += 1
    return candidate


# ---- git ----------------------------------------------------------------------------------------


def git_program() -> str:
    return shutil.which("git") or ""


def git(folder: str | Path, *args: str, check: bool = True, timeout: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """git in `folder`, its output as bytes (paths come back -z separated and may not be text in the console's code
    page). MoveError when it cannot run or (with check) fails."""
    program = git_program()
    if not program:
        raise MoveError("git is not installed on this machine (or not on PATH), so the repositories cannot be read")
    try:
        done = subprocess.run([program, "-c", "core.quotepath=off", "-C", str(folder), *args], capture_output=True,
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise MoveError(f"git could not be run in {folder}: {exc}") from exc
    if check and done.returncode != 0:
        said = (done.stderr or done.stdout or b"").decode("utf-8", "replace").strip()
        raise MoveError(f"git {args[0]} failed in {folder}: {said[-400:] or done.returncode}")
    return done


def _z(raw: bytes) -> list[str]:
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def tracked(repo: Path) -> list[str]:
    """Every path git tracks in `repo`, once (a conflicted file has several stages), submodules left out - each is a
    repository of its own and planned as one."""
    out: list[str] = []
    seen: set[str] = set()
    for line in _z(git(repo, "ls-files", "-z", "--stage").stdout):
        meta, _, path = line.partition("\t")
        if not path or meta.startswith("160000") or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def ignored(repo: Path) -> list[str]:
    """Every ignored file in `repo` (git lists them file by file; a nested repository shows as `name/`)."""
    return _z(git(repo, "ls-files", "-z", "--others", "--ignored", "--exclude-standard").stdout)


def untracked(repo: Path) -> list[str]:
    """Untracked files that are not ignored - these stay where they are."""
    return _z(git(repo, "ls-files", "-z", "--others", "--exclude-standard").stdout)


def status(repo: Path) -> list[str]:
    """Uncommitted changes to tracked files (untracked ones are not counted - they stay behind)."""
    return _z(git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no").stdout)


def head(repo: Path) -> str:
    done = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return done.stdout.decode("ascii", "replace").strip() if done.returncode == 0 else ""


def gitdir_of(repo: Path) -> tuple[str, Path | None, bool]:
    """(kind, where its Git data is, whether the .git file names it by a full path): ("dir", None, False) for a .git
    folder, ("file", path or None, absolute) for a .git file, ("missing", None, False)."""
    dot = repo / ".git"
    if dot.is_dir():
        return "dir", None, False
    if dot.is_file():
        try:
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return "file", None, False
        if text.lower().startswith("gitdir:"):
            path = Path(text[len("gitdir:"):].strip())
            return "file", (path if path.is_absolute() else (repo / path)).resolve(strict=False), path.is_absolute()
        return "file", None, False
    return "missing", None, False


# ---- the plan -----------------------------------------------------------------------------------


def _walk_files(folder: Path) -> list[Path]:
    """Every file under `folder` (links are copied as what they are not; they are skipped)."""
    out: list[Path] = []
    for here, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(here) / d).is_symlink()]
        out.extend(Path(here) / f for f in files if not (Path(here) / f).is_symlink())
    return out


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _plan_repo(repo: Path, share: Path, target: Path, repos: list[Path]) -> dict[str, Any]:
    """One repository's part of the move: what goes (its .git, tracked and ignored files), what stays, and the
    problems that stop it."""
    rel = _rel(repo, share)
    dest = target / rel if rel else target
    nested = [r for r in repos if r != repo and _within(r, repo)]
    info: dict[str, Any] = {"repo": str(repo), "rel": rel, "dest": str(dest), "kind": "dir", "files": 0, "bytes": 0,
                            "tracked": 0, "ignored": 0, "untracked_left": 0, "dirty": 0, "problem": "",
                            "worktree": False, "moves": []}
    kind, pointed, absolute = gitdir_of(repo)
    info["kind"] = kind
    if kind == "file":
        if pointed is None:
            info["problem"] = f"{repo / '.git'} does not say where its Git data is - move it by hand"
            return info
        moving = any(_within(pointed, r / ".git") for r in repos)
        if moving and absolute:
            info["problem"] = (f"it is a linked worktree of a repository that is moving too ({pointed}) - move it by "
                               "hand, or remove the worktree first")
            return info
        if not moving:
            if _within(pointed, share):
                info["problem"] = (f"its Git data is at {pointed}, inside OneDrive but in no repository being moved "
                                   "- move it by hand")
                return info
            # A linked worktree of a repository elsewhere: reconnected with `git worktree repair` after the copy.
            info["worktree"] = True
    try:
        files = tracked(repo)
        extra = ignored(repo)
        staying = untracked(repo)
        info["dirty"] = len(status(repo))
    except MoveError as exc:
        info["problem"] = str(exc)
        return info

    def inside_nested(path: Path) -> bool:
        return any(path == n or _within(path, n) for n in nested)

    moves: list[tuple[Path, Path]] = []
    for name in files:
        source = repo / name
        if source.is_file() and not inside_nested(source):
            moves.append((source, dest / name))
            info["tracked"] += 1
    for name in extra:
        source = repo / name.rstrip("/")
        if inside_nested(source):
            continue
        if name.endswith("/") and source.is_dir():
            for found in _walk_files(source):
                moves.append((found, dest / found.relative_to(repo)))
                info["ignored"] += 1
        elif source.is_file():
            moves.append((source, dest / name))
            info["ignored"] += 1
    info["untracked_left"] = sum(1 for n in staying if not inside_nested(repo / n.rstrip("/")))
    dot = repo / ".git"
    if dot.is_dir():
        for found in _walk_files(dot):
            moves.append((found, dest / found.relative_to(repo)))
    elif dot.is_file():
        moves.append((dot, dest / ".git"))
    info["moves"] = moves
    info["files"] = len(moves)
    info["bytes"] = sum(_size(s) for s, _ in moves)
    return info


def _clashes(plan_repos: list[dict[str, Any]], target: Path) -> list[dict[str, str]]:
    """What is already where the move would write: a repository already there, a destination folder that holds
    anything (moving in would be a merge), a file in the way."""
    out: list[dict[str, str]] = []
    if (target / ".git").exists():
        out.append({"path": str(target), "why": "already holds a Git repository"})
    repo_paths = [Path(r["repo"]) for r in plan_repos]
    for repo in plan_repos:
        dest = Path(repo["dest"])
        outer = not any(other != Path(repo["repo"]) and _within(Path(repo["repo"]), other) for other in repo_paths)
        if dest.is_file():
            out.append({"path": str(dest), "why": "is a file where the repository's folder would go"})
            continue
        if dest != target and (dest / ".git").exists():
            out.append({"path": str(dest), "why": "already holds a Git repository"})
        if outer and dest.is_dir() and any(dest.iterdir()):
            out.append({"path": str(dest), "why": "already holds files - moving the repository in would mix it "
                                                  "with them"})
            continue
        for _source, to in repo["moves"]:
            if to.exists() or to.is_symlink():
                out.append({"path": str(to), "why": "a file of this name is already there"})
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for clash in out:
        key = (clash["path"].lower(), clash["why"])
        if key not in seen:
            seen.add(key)
            unique.append(clash)
    return unique


def plan_move(share: str | Path, repos: list[str | Path], target: str | Path, roots: list[Path]) -> dict[str, Any]:
    """The whole move, worked out and checked - nothing written. MoveError / FolderError when the target is refused;
    otherwise a plan whose `ok` says whether it can go ahead, with its `clashes` and `problems`.

    Each repository goes to `<target>/<its path inside the assets folder>` (one at the assets folder's top goes into
    the target itself): its `.git`, its tracked files and its ignored files. Untracked files stay."""
    share_path = _resolved(share)
    target_path = check_target(target, share_path, roots)
    found = sorted({_resolved(r) for r in repos}, key=lambda p: (len(p.parts), str(p).lower()))
    found = [r for r in found if r == share_path or _within(r, share_path)]
    if not found:
        raise MoveError("there is no Git repository in the project assets folder to move")
    if not git_program():
        raise MoveError("git is not installed on this machine (or not on PATH), so the repositories cannot be read")
    plan_repos = [_plan_repo(r, share_path, target_path, found) for r in found]
    # A file listed by an outer repository and by a nested one moves once.
    seen: set[str] = set()
    for repo in plan_repos:
        kept: list[tuple[Path, Path]] = []
        for source, to in repo["moves"]:
            key = os.path.normcase(str(source))
            if key not in seen:
                seen.add(key)
                kept.append((source, to))
        repo["moves"] = kept
    problems = [f"{r['rel'] or Path(r['repo']).name}: {r['problem']}" for r in plan_repos if r["problem"]]
    clashes = _clashes(plan_repos, target_path)
    suggest = ""
    if clashes:
        suggest = str(free_subfolder(target_path, share_path.name))
    return {
        "share": str(share_path), "target": str(target_path), "target_exists": target_path.is_dir(),
        "repos": plan_repos,
        "files": sum(r["files"] for r in plan_repos), "bytes": sum(r["bytes"] for r in plan_repos),
        "untracked_left": sum(r["untracked_left"] for r in plan_repos),
        "dirty": sum(r["dirty"] for r in plan_repos),
        "clashes": clashes[:CLASH_LIST_MAX], "clash_count": len(clashes), "suggest": suggest,
        "problems": problems, "ok": not clashes and not problems,
    }


def plan_view(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan as the page gets it: everything but the file-by-file list."""
    return {**{k: v for k, v in plan.items() if k != "repos"},
            "repos": [{k: v for k, v in r.items() if k != "moves"} for r in plan["repos"]]}


# ---- the remembered answer ----------------------------------------------------------------------


def choice_of(entry: dict[str, Any] | None) -> str:
    """"ignore" when this project session said "Don't remind me again", else "remind" (the default)."""
    return "ignore" if str((entry or {}).get("git_onedrive") or "").strip().lower() == "ignore" else "remind"
