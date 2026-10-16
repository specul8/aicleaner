"""Updates from the Settings page (2026-10-16): what is installed, what is available, and the update.

**Installed** is the newest record in ~/.aicleaner/deployment.json (the repository and tag
Deploy-AiCleaner.ps1 installed from), falling back to the package version.

**Available** is the highest vX.Y.Z tag on that repository, read with one `git ls-remote --tags` -
the same question Deploy-AiCleaner.ps1 asks before an update, answered with this machine's own git
credentials (a private repository needs nothing more). Remembered for an hour.

**The update** is the installed Deploy-AiCleaner.ps1 -Action Update -AssumeDefaults, the command a
person runs: it stops the proxy, installs the newest tag and starts it again - so, like Restart, it
is handed to a PowerShell that outlives this process (restart.spawn_detached).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .restart import RestartError, powershell, spawn_detached

CHECK_EVERY_S = 3600                 # the card's own check, at most this often
AUTO_EVERY_S = 6 * 3600              # update.auto: how often the proxy looks
TAG_RE = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
LOG_NAME = "update.log"


def deployment_file() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".aicleaner" / "deployment.json"


def installed(path: Path | None = None, root: Path | None = None) -> dict[str, str]:
    """The repository, tag and version this install came from: the record for this install's folder
    when there is one, else the last record. The version is the running code's own."""
    path = path or deployment_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        records = [r for r in data.get("deployments") or [] if isinstance(r, dict)]
    except (OSError, ValueError, AttributeError):
        records = []
    mine = [r for r in records if root and str(r.get("install_path") or "").rstrip("\\/").lower()
            == str(root).rstrip("\\/").lower()]
    record = (mine or records or [{}])[-1]
    return {"repo": str(record.get("repo_url") or ""), "ref": str(record.get("ref") or ""),
            "version": __version__}


def version_key(text: str) -> tuple[int, int, int] | None:
    found = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(n) for n in found.groups()) if found else None


def highest_tag(ls_remote: str) -> str:
    """The highest vX.Y.Z among `git ls-remote --tags` lines, '' when there is none."""
    best: tuple[int, int, int] | None = None
    for line in ls_remote.splitlines():
        found = TAG_RE.search(line.strip())
        if found:
            key = tuple(int(n) for n in found.groups())
            best = key if best is None or key > best else best
    return "v{}.{}.{}".format(*best) if best else ""


def latest_tag(repo: str, run: Callable[..., Any] = subprocess.run) -> str:
    """Ask the repository for its tags. Raises RuntimeError with git's own words when it cannot."""
    if not repo:
        raise RuntimeError("this install has no deployment record to say where it came from")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}        # never wait on a prompt nobody sees
    try:
        done = run(["git", "ls-remote", "--tags", "--refs", repo], capture_output=True, text=True,
                   timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not ask {repo} for its releases: {exc}") from exc
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "git ls-remote failed").strip()[-300:])
    return highest_tag(done.stdout)


@dataclass
class UpdateState:
    """The last check, kept by the proxy for the card and for update.auto."""
    checked_at: float = 0.0
    latest: str = ""
    error: str = ""
    started_at: float = 0.0          # an update handed off, so a second press does not start another

    def check(self, repo: str, force: bool = False, run: Callable[..., Any] = subprocess.run) -> None:
        if not force and self.checked_at and time.time() - self.checked_at < CHECK_EVERY_S:
            return
        try:
            self.latest, self.error = latest_tag(repo, run), ""
        except RuntimeError as exc:
            self.error = str(exc)
        self.checked_at = time.time()

    def view(self, mine: dict[str, str], auto: bool) -> dict[str, Any]:
        have, can = version_key(mine["version"]), version_key(self.latest)
        return {**mine, "latest": self.latest, "newer": bool(have and can and can > have),
                "checked_at": self.checked_at, "error": self.error, "auto": auto,
                "updating": bool(self.started_at and time.time() - self.started_at < 900)}


def command(root: Path) -> list[str]:
    """The update a person would run, unattended. A short pause first, so the page gets its answer."""
    script = root / "installer" / "Deploy-AiCleaner.ps1"
    quoted = "'" + str(script).replace("'", "''") + "'"
    return [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command",
            f"Start-Sleep -Seconds 1; & {quoted} -Action Update -AssumeDefaults"]


def start_update(root: Path, log_dir: Path) -> Path:
    """Hand the update off; the proxy stops partway through it and comes back on the new version."""
    script = root / "installer" / "Deploy-AiCleaner.ps1"
    if not script.is_file():
        raise RestartError(f"{script} is missing, so this install cannot update itself; run the installer by hand")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_NAME
    log_path.write_text("", encoding="utf-8")                      # this run's log only
    return spawn_detached(command(root), root, log_path)
