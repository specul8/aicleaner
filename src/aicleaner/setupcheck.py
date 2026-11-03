"""Is a project set up? (2026-10-16, item B; spec 2026-10-16-b-project-setup-check-design.md)

The project model (2026-10-16): a project folder holds its repositories side by side and one
local-only `_<project>-tooling` repository, the session's home, which carries CLAUDE.md. This says
what is missing - the home not a git repository, no CLAUDE.md there, a hand-added folder outside any
repository, a workspace file's folder gone - and does the two fixes it offers: `git init` in the home,
and a starter CLAUDE.md. Findings never block saving a profile.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

CLAUDE_MD = ("CLAUDE.md", ".claude/CLAUDE.md")


def _same(a: str, b: str) -> bool:
    return str(Path(a)).rstrip("\\/").lower() == str(Path(b)).rstrip("\\/").lower()


def in_repository(folder: Path) -> bool:
    """The folder is a repository, or inside one (a .git folder or file at it or above it)."""
    for candidate in [folder, *folder.parents]:
        if (candidate / ".git").exists():
            return True
    return False


def findings(home: str, folders: list[str], share: str = "", workspaces: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if home and Path(home).is_dir():
        h = Path(home)
        if not (h / ".git").exists():
            out.append({"folder": home, "kind": "not_repo",
                        "fix": None if in_repository(h) else "git_init",
                        "text": (f"{home} is not a git repository" + (" (it is inside one)" if in_repository(h) else ""))})
        if not any((h / name).is_file() for name in CLAUDE_MD):
            out.append({"folder": home, "kind": "no_claude_md", "fix": "claude_md",
                        "text": f"No CLAUDE.md in the home folder {home}"})
    for folder in folders:
        if not folder or (home and _same(folder, home)) or (share and _same(folder, share)):
            continue
        path = Path(folder)
        if path.is_dir() and not in_repository(path):
            out.append({"folder": folder, "kind": "not_in_repo", "fix": None,
                        "text": f"{folder} is not in a git repository"})
    for file in workspaces or []:
        if file and not Path(file).parent.is_dir():
            out.append({"folder": str(Path(file).parent), "kind": "missing", "fix": None,
                        "text": f"The workspace file's folder {Path(file).parent} is not there"})
    return out


def git_init(folder: Path) -> str:
    done = subprocess.run(["git", "init", "-q", str(folder)], capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "git init failed").strip()[-300:])
    return f"{folder} is now a git repository"


def claude_md_text(profile: str, home: str, folders: list[str], share: str = "") -> str:
    repos = [f for f in folders if f and not _same(f, home) and not (share and _same(f, share))]
    lines = [f"# {profile}", "",
             f"This folder (`{home}`) is the session's home: local notes, plans and this file. "
             "The work itself is in the repositories below.", ""]
    if repos:
        lines += ["## Repositories", ""] + [f"- `{r}`" for r in repos] + [""]
    if share:
        lines += ["## Shared files", "", f"`{share}` is the OneDrive folder for files shared with the person - "
                  "documents to read, files to hand over. It is not a repository.", ""]
    lines += ["## Working notes", "", "- (add how this project is built, tested and released)", ""]
    return "\n".join(lines)


def write_claude_md(home: Path, text: str) -> str:
    target = home / "CLAUDE.md"
    if target.exists():
        raise RuntimeError(f"{target} is already there")
    target.write_text(text, encoding="utf-8")
    return f"Wrote {target}"
