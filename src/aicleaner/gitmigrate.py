"""Moving Git repositories out of OneDrive (feature 186): copy, verify, and only then delete - or put it all back.

`gitonedrive.plan_move` says what goes where; this carries it out. The order is the safety:

1. each repository's state is recorded (HEAD, and the status of its tracked files);
2. every planned file is COPIED to the target - never over a file already there;
3. the copies are VERIFIED: the same HEAD and status in the new place, and `git fsck` clean;
4. only then are the originals DELETED from OneDrive (files, then the folders the move emptied);
5. a note, "where did my files go.md", is left where each repository was.

A failure while copying or verifying removes every copy: OneDrive is as it was. A failure while deleting copies every
file already deleted back from the target first, then removes the copies: again as it was. Every step is logged
(real paths - the log stays on this machine).
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import gitonedrive
from .gitonedrive import MoveError

LOG_KEEP_DAYS = 14


class MigrateLog:
    """One tab-separated log per move (`git-migrate_<yyyyMMdd-HHmmss>.log`), never overwriting an earlier one."""

    COLUMNS = ("UtcTime", "LocalTime", "Level", "RunId", "Computer", "User", "Script", "Phase", "Message")

    def __init__(self, folder: Path | None):
        self.run_id = uuid.uuid4().hex[:8]
        self.lines: list[str] = []
        self.path: Path | None = None
        if folder is not None:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                self._prune(folder)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                self.path = folder / f"git-migrate_{stamp}.log"
                n = 2
                while self.path.exists():
                    self.path = folder / f"git-migrate_{stamp}-{n}.log"
                    n += 1
                self.path.write_text("\t".join(self.COLUMNS) + "\n", encoding="utf-8")
            except OSError:
                self.path = None

    @staticmethod
    def _prune(folder: Path) -> None:
        cutoff = time.time() - LOG_KEEP_DAYS * 86400
        for old in folder.glob("git-migrate_*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def write(self, phase: str, message: str, level: str = "INFO") -> None:
        now = datetime.now(timezone.utc)
        row = [now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.astimezone().strftime("%Y-%m-%d %H:%M:%S"), level,
               self.run_id, socket.gethostname(), os.environ.get("USERNAME", ""), "gitmigrate", phase,
               message.replace("\t", " ").replace("\n", " ")]
        line = "\t".join(row)
        self.lines.append(line)
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as out:
                    out.write(line + "\n")
            except OSError:
                pass


# ---- file operations (kept apart so a test can make one fail) -----------------------------------


def _writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def copy_one(source: Path, dest: Path) -> None:
    """One file copied, never over another: an existing destination is an error (it appeared after the plan)."""
    if dest.exists() or dest.is_symlink():
        raise MoveError(f"{dest} appeared after the move was planned - nothing is overwritten")
    shutil.copy2(source, dest)
    if dest.stat().st_size != source.stat().st_size:
        raise MoveError(f"{dest} is not the same size as {source} after copying")


def delete_one(path: Path) -> None:
    """One file deleted; Git's object files are read-only, so the flag is cleared first."""
    try:
        path.unlink()
    except PermissionError:
        _writable(path)
        path.unlink()


# ---- the move -----------------------------------------------------------------------------------


def _before(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["repo"]: {"head": gitonedrive.head(Path(r["repo"])), "status": sorted(gitonedrive.status(Path(r["repo"])))}
            for r in plan["repos"]}


def verify(plan: dict[str, Any], before: dict[str, dict[str, Any]]) -> None:
    """The copies are the same repositories: the same HEAD, the same status of tracked files, and fsck clean.
    MoveError naming the first that is not."""
    for repo in plan["repos"]:
        dest = Path(repo["dest"])
        was = before[repo["repo"]]
        now_head = gitonedrive.head(dest)
        if now_head != was["head"]:
            raise MoveError(f"{dest}: HEAD is {now_head or 'missing'} after copying, was {was['head'] or 'missing'}")
        now_status = sorted(gitonedrive.status(dest))
        if now_status != was["status"]:
            raise MoveError(f"{dest}: git status differs after copying ({len(now_status)} changes, was "
                            f"{len(was['status'])})")
        if was["head"]:
            gitonedrive.git(dest, "fsck", "--connectivity-only", "--no-dangling", "--no-progress")


def _repair(folders: list[str], log: MigrateLog, phase: str) -> None:
    """`git worktree repair` in each folder: reconnects a linked worktree with its repository (and a repository with
    its linked worktrees) after either moved."""
    for folder in folders:
        done = gitonedrive.git(folder, "worktree", "repair", check=False)
        log.write(phase, f"git worktree repair in {folder}: exit {done.returncode}")


def _make_parents(dest: Path, made: list[Path]) -> None:
    missing: list[Path] = []
    at = dest.parent
    while not at.exists():
        missing.append(at)
        at = at.parent
    for folder in reversed(missing):
        folder.mkdir()
        made.append(folder)


def _remove_copies(copied: list[Path], made: list[Path], log: MigrateLog) -> list[str]:
    """Every copy removed, then every folder the move made (when empty). What could not be removed, in words."""
    left: list[str] = []
    for path in reversed(copied):
        try:
            delete_one(path)
        except OSError as exc:
            left.append(f"{path}: {exc}")
    for folder in sorted(made, key=lambda p: len(p.parts), reverse=True):
        try:
            folder.rmdir()
        except OSError:
            pass
    log.write("rollback", f"removed {len(copied) - len(left)} copied files; {len(left)} could not be removed",
              "WARN" if left else "INFO")
    return left


def _git_folders(root: Path) -> list[Path]:
    """Every folder of a repository's .git folder, itself included - empty ones too (refs/tags, objects/info), which
    no file in the plan leads to."""
    dot = root / ".git"
    if not dot.is_dir() or dot.is_symlink():
        return []
    out = [dot]
    for here, dirs, _files in os.walk(dot, followlinks=False):
        out.extend(Path(here) / d for d in dirs)
    return out


def _emptied_folders(plan: dict[str, Any]) -> list[Path]:
    """The source folders the move may have emptied, deepest first - never a repository's own folder (the note goes
    there) nor anything above it."""
    folders: set[Path] = set()
    for repo in plan["repos"]:
        root = Path(repo["repo"])
        folders.update(_git_folders(root))
        for source, _to in repo["moves"]:
            at = source.parent
            while at != root and gitonedrive._within(at, root):
                folders.add(at)
                at = at.parent
    return sorted(folders, key=lambda p: len(p.parts), reverse=True)


def execute(plan: dict[str, Any], log_folder: Path | None = None,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Carry out a plan from `gitonedrive.plan_move` (made just before, on the server). Returns what was done; raises
    MoveError after putting everything back when it could not be done."""
    log = MigrateLog(log_folder)
    say = progress or (lambda _text: None)
    if not plan.get("ok"):
        raise MoveError("the move has clashes or problems to settle first: "
                        + "; ".join([*plan.get("problems", []), *[c["path"] + " " + c["why"] for c in plan.get("clashes", [])]]))
    target = Path(plan["target"])
    log.write("start", f"moving {len(plan['repos'])} repositories ({plan['files']} files, {plan['bytes']} bytes) from "
                       f"{plan['share']} to {target}")
    say("Reading the repositories")
    before = _before(plan)
    for repo, state in before.items():
        log.write("before", f"{repo}: HEAD {state['head'] or '(none)'}, {len(state['status'])} uncommitted changes")
    copied: list[Path] = []
    made: list[Path] = []
    repaired: list[str] = []
    try:
        if not target.exists():
            _make_parents(target / "x", made)
        say(f"Copying {plan['files']} files")
        for repo in plan["repos"]:
            root, dest_root = Path(repo["repo"]), Path(repo["dest"])
            for folder in _git_folders(root):                       # .git's folders, empty ones too
                _make_parents(dest_root / folder.relative_to(root) / "x", made)
            for source, dest in repo["moves"]:
                _make_parents(dest, made)
                copy_one(source, dest)
                copied.append(dest)
            log.write("copy", f"{repo['repo']} -> {repo['dest']}: {len(repo['moves'])} files copied")
        say("Checking the copies")
        repaired = [r["dest"] for r in plan["repos"]
                    if r.get("worktree") or (Path(r["dest"]) / ".git" / "worktrees").is_dir()]
        _repair(repaired, log, "verify")
        verify(plan, before)
        log.write("verify", "every copy has the same HEAD and status, and fsck is clean")
    except Exception as exc:  # noqa: BLE001 - anything at all: put it back, then say what
        log.write("copy", f"failed: {exc}", "ERROR")
        if repaired:
            _repair([r["repo"] for r in plan["repos"] if r["dest"] in repaired], log, "rollback")
        left = _remove_copies(copied, made, log)
        raise MoveError(f"nothing was moved - {exc}" + (f" (and {len(left)} copies could not be removed from "
                                                         f"{target}; see {log.path})" if left else "")) from exc
    deleted: list[tuple[Path, Path]] = []
    say("Removing the originals from OneDrive")
    try:
        for repo in plan["repos"]:
            for source, dest in repo["moves"]:
                delete_one(source)
                deleted.append((source, dest))
        log.write("delete", f"{len(deleted)} files removed from OneDrive")
    except Exception as exc:  # noqa: BLE001 - put back what was deleted, then remove the copies
        log.write("delete", f"failed after {len(deleted)} files: {exc}", "ERROR")
        not_back = _restore(deleted, log)
        if not_back:
            raise MoveError(f"the move stopped part way ({exc}); {len(not_back)} files could not be put back in "
                            f"OneDrive, so every copy is kept in {target} - see {log.path}") from exc
        if repaired:
            _repair([r["repo"] for r in plan["repos"] if r["dest"] in repaired], log, "rollback")
        _remove_copies(copied, made, log)
        raise MoveError(f"nothing was moved - removing the originals failed ({exc}), so they were put back") from exc
    for folder in _emptied_folders(plan):
        try:
            folder.rmdir()
        except OSError:
            pass
    notes = write_notes(plan, log)
    gitonedrive.forget(plan["share"])
    log.write("done", f"moved {len(plan['repos'])} repositories to {target}")
    return {"ok": True, "target": str(target), "files": len(deleted), "bytes": plan["bytes"],
            "repos": [{"repo": r["repo"], "dest": r["dest"], "rel": r["rel"], "files": r["files"],
                       "untracked_left": r["untracked_left"]} for r in plan["repos"]],
            "notes": [str(n) for n in notes], "log": str(log.path or "")}


def _restore(deleted: list[tuple[Path, Path]], log: MigrateLog) -> list[str]:
    """Every deleted original copied back from its copy; what could not be, in words."""
    not_back: list[str] = []
    for source, dest in reversed(deleted):
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, source)
        except OSError as exc:
            not_back.append(f"{source}: {exc}")
    log.write("rollback", f"{len(deleted) - len(not_back)} originals put back; {len(not_back)} could not be",
              "ERROR" if not_back else "INFO")
    return not_back


# ---- the note left behind -----------------------------------------------------------------------


def _human(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def _ps(path: str) -> str:
    """A path as a single-quoted PowerShell string."""
    return "'" + path.replace("'", "''") + "'"


def note_text(repo: dict[str, Any], nested: list[dict[str, Any]], when: str) -> str:
    """The note left in a repository's former OneDrive folder: what moved where, why, and how to put it back."""
    source, dest = repo["repo"], repo["dest"]
    lines = [
        "# Where did my files go?",
        "",
        f"On {when}, AI Cleaner moved the Git repository that was in this folder out of OneDrive, at your request.",
        "",
        "## What moved, and where to",
        "",
        f"- From: `{source}`",
        f"- To: `{dest}`",
        f"- {repo['files']} files ({_human(repo['bytes'])}): the repository's `.git` folder, every file Git tracks "
        "(with any changes you had not committed yet) and every file your `.gitignore` covers (build output, "
        "packages, local settings).",
    ]
    for inner in nested:
        lines.append(f"- The repository in `{inner['rel']}` went with it, to `{inner['dest']}`.")
    lines += [
        f"- Left here: {repo['untracked_left']} untracked files - files that were never added to the repository "
        "and are not ignored. They are yours, not Git's, so they stayed in OneDrive.",
        "",
        "AI Cleaner's project session now uses the new folder. Open the repository from there from now on "
        "(VS Code, a terminal, GitHub Desktop).",
        "",
        "## Why Git in OneDrive is a bad idea",
        "",
        "- **Thousands of small files.** A repository's `.git` folder holds an object for every version of every "
        "file. OneDrive syncs each one separately, so a single commit or checkout can mean hundreds of uploads, and "
        "sync falls behind.",
        "- **Sync conflicts.** When two machines (or OneDrive and Git on one machine) change the same file, OneDrive "
        "keeps both, renaming one with the computer's name. Git does not know about the copy and the repository "
        "is no longer what it thinks it is.",
        "- **Half-synced repositories corrupt.** Git writes several files for one change and expects all of them. "
        "OneDrive can sync some before the others, or bring down an old copy of one, and the object store breaks "
        "(\"bad object\", \"missing blob\").",
        "- **Locked files.** OneDrive opens files to upload them; Git then cannot rename or replace them, and "
        "commits, pulls and checkouts fail part way.",
        "",
        "Your remote (GitHub, Azure DevOps) is the backup for a repository; OneDrive is the place for documents.",
        "",
        "## How to put it back",
        "",
        "Only if you need to. First stop any AI Cleaner session using the folder, and close VS Code or anything "
        "else that has it open. Then, in PowerShell:",
        "",
        "```powershell",
        f"robocopy {_ps(dest)} {_ps(source)} /E /MOVE",
        "```",
        "",
        "That moves everything in the new folder - including any work done there since - back here, and removes "
        "the new folder. robocopy's exit codes 0 to 7 mean success.",
    ]
    if repo.get("worktree"):
        lines += ["", "This was a linked worktree. After moving it back, reconnect it to its repository:", "",
                  "```powershell", f"git -C {_ps(source)} worktree repair", "```"]
    lines += [
        "",
        f"Then, in AI Cleaner, open the project session (Edit project sessions) and remove `{dest}` from its folders.",
        "Starting the session will warn about Git in OneDrive again; answer \"No, Don't remind me again (this "
        "project)\" if you want it there.",
        "",
    ]
    return "\n".join(lines)


def write_notes(plan: dict[str, Any], log: MigrateLog) -> list[Path]:
    """The note in each outermost repository's former folder (a nested one is listed in its outer one's)."""
    when = datetime.now().strftime("%d %B %Y at %H:%M")
    paths = [Path(r["repo"]) for r in plan["repos"]]
    written: list[Path] = []
    for repo in plan["repos"]:
        here = Path(repo["repo"])
        if any(other != here and gitonedrive._within(here, other) for other in paths):
            continue
        nested = [r for r in plan["repos"] if Path(r["repo"]) != here and gitonedrive._within(Path(r["repo"]), here)]
        note = here / gitonedrive.NOTE_NAME
        n = 2
        while note.exists():
            note = here / f"where did my files go ({n}).md"
            n += 1
        try:
            here.mkdir(parents=True, exist_ok=True)
            note.write_text(note_text(repo, nested, when), encoding="utf-8")
            written.append(note)
            log.write("note", f"wrote {note}")
        except OSError as exc:
            log.write("note", f"could not write {note}: {exc}", "WARN")
    return written
