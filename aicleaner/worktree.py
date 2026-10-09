"""A second chat session on a project, in a git worktree of its own (DECISIONS 66, feature 86).

Two Claude Codes in one working tree commit over each other, so a project has one chat session at a
time. A worktree is a second checkout of the same repo on its own branch: a session there can work
alongside the first, and its work comes back by merging the branch.

The worktree goes beside the repo, `<repo>.worktrees/<name>`, not inside it - inside, the first
session's file tree, searches and `git status` would all see it. The branch takes the same name.

Everything here runs git and says in words what went wrong; nothing is left half-done: a merge that
conflicts is aborted, and a branch with unmerged commits is kept when its worktree is removed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .shares import in_onedrive

# A branch and folder name: short, no spaces, nothing git or a path would read as an option or a step.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
_GIT_TIMEOUT_S = 120


class WorktreeError(ValueError):
    """Something git refused, or that was refused before git was asked, in words for the page."""


def _git(folder: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=_GIT_TIMEOUT_S, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorktreeError(f"git could not be run: {exc}") from exc
    if check and done.returncode != 0:
        raise WorktreeError((done.stderr or done.stdout).strip() or f"git {args[0]} failed")
    return done


def is_repo(folder: str | Path) -> bool:
    return (Path(folder) / ".git").exists()


def _synced(folder: str | Path) -> bool:
    """In OneDrive: this user's (from the environment) or any account's folder by its name. A repo
    there has its .git synced file by file, which corrupts it."""
    parts = [p.lower() for p in Path(folder).parts]
    return in_onedrive(folder) or any(p == "onedrive" or p.startswith("onedrive - ") for p in parts)


def repos_of(folders: list) -> list[str]:
    """The folders that are git repos, in order."""
    return [str(f) for f in folders if is_repo(f)]


def default_name(taken: list[str]) -> str:
    used = {t.lower() for t in taken}
    n = 1
    while f"wt-{n}" in used:
        n += 1
    return f"wt-{n}"


def place(repo: str | Path, name: str) -> Path:
    repo = Path(repo)
    return repo.parent / f"{repo.name}.worktrees" / name


def check_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME.fullmatch(name) or ".." in name or name.endswith((".", ".lock")):
        raise WorktreeError(f"{name or 'an empty name'}: a worktree name is up to 40 letters, digits, "
                            ". _ or -, starting with a letter or digit")
    return name


def init_repo(folder: str | Path) -> None:
    """Turn a plain folder into a repo with one commit of what is there, so it can have worktrees."""
    folder = Path(folder)
    if is_repo(folder):
        raise WorktreeError(f"{folder} is already a git repo")
    if _synced(folder):
        raise WorktreeError(f"{folder} is in OneDrive; a git repo there is corrupted by the sync. "
                            "Move the folder out of OneDrive first")
    _git(folder, "init")
    _git(folder, "add", "-A")
    done = _git(folder, "commit", "--allow-empty", "-m", "First commit", check=False)
    if done.returncode != 0:
        raise WorktreeError(f"{folder} is now a repo, but the first commit failed: "
                            f"{(done.stderr or done.stdout).strip()}")


def create(repo: str | Path, name: str) -> Path:
    """`git worktree add -b <name> <repo>.worktrees/<name>`. Returns the worktree's folder."""
    name = check_name(name)
    if not is_repo(repo):
        raise WorktreeError(f"{repo} is not a git repo")
    path = place(repo, name)
    if path.exists():
        raise WorktreeError(f"{path} already exists; choose another name")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0:
        raise WorktreeError(f"the branch {name} already exists in {repo}; choose another name")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", name, str(path))
    return path


def _uncommitted(folder: str | Path) -> list[str]:
    return [line[3:] for line in _git(folder, "status", "--porcelain").stdout.splitlines() if line.strip()]


def _refuse_uncommitted(folder: str | Path, what: str) -> None:
    changed = _uncommitted(folder)
    if changed:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        raise WorktreeError(f"{what} has uncommitted changes ({shown}); commit or discard them first")


def merge(repo: str | Path, name: str, path: str | Path) -> str:
    """Merge the worktree's branch into the repo's current branch. A conflict is reported, and the
    merge undone - resolving one is for a person or for Claude, not for a button."""
    name = check_name(name)
    _refuse_uncommitted(path, f"the worktree {name}")
    _refuse_uncommitted(repo, str(repo))
    done = _git(repo, "merge", "--no-edit", name, check=False)
    if done.returncode != 0:
        conflicted = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        _git(repo, "merge", "--abort", check=False)
        if conflicted:
            raise WorktreeError(f"merging {name} conflicts in {', '.join(conflicted)}; nothing was merged. "
                                "Resolve it in a terminal, or ask Claude to")
        raise WorktreeError((done.stderr or done.stdout).strip() or f"merging {name} failed")
    return f"merged {name} into {Path(repo).name}"


def remove(repo: str | Path, name: str, path: str | Path) -> str:
    """Remove the worktree, and its branch when the branch is merged. Unmerged commits are kept."""
    name = check_name(name)
    if Path(path).exists():
        _refuse_uncommitted(path, f"the worktree {name}")
        _git(repo, "worktree", "remove", str(path))
    _git(repo, "worktree", "prune", check=False)
    if _git(repo, "branch", "-d", name, check=False).returncode != 0:
        return (f"removed the worktree {name}; its branch is kept because it has commits not merged "
                f"into {Path(repo).name}")
    return f"removed the worktree {name} and its branch"
