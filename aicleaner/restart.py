"""Restart the proxy from its own page: Stop-AiCleaner.ps1, then Start-AiCleaner.ps1 -Detach.

The proxy cannot restart itself in-process - the config, the guard and the mapping watchers are
all built at start - so it hands the job to a PowerShell that outlives it. That child is detached
from this process (and, where Windows allows, from its job object), because the first thing it
does is stop the process that started it. It runs the same scripts a person would, beside the
installed app, and starts the logon task instead when there is one, as Deploy-AiCleaner.ps1 does.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TASK_NAME = "aicleaner-proxy"
LOG_NAME = "restart.log"


class RestartError(RuntimeError):
    """The restart could not be handed off; the proxy is still running as it was."""


def scripts(root: Path) -> tuple[Path, Path]:
    # In installer\ since feature 102; an install from before the move keeps them at the root.
    folder = root / "installer" if (root / "installer" / "Start-AiCleaner.ps1").exists() else root
    return folder / "Stop-AiCleaner.ps1", folder / "Start-AiCleaner.ps1"


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def powershell() -> str:
    """Windows PowerShell by its full path, never by search order (planting, H29)."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def command(root: Path, port: int) -> list[str]:
    """The PowerShell the child runs. A short pause first, so the page gets its answer."""
    stop, start = scripts(root)
    script = (
        "Start-Sleep -Seconds 1; "
        f"& {_quote(stop)} -Port {int(port)}; "
        f"if (Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue) "
        f"{{ Start-ScheduledTask -TaskName '{TASK_NAME}' }} "
        f"else {{ & {_quote(start)} -Port {int(port)} -Detach }}"
    )
    return [powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-Command", script]


def restart(root: Path, port: int, log_dir: Path) -> Path:
    """Hand the restart to a detached PowerShell and return the log it writes to."""
    if sys.platform != "win32":
        raise RestartError("restarting from the page needs Windows; run the Stop and Start scripts")
    stop, start = scripts(root)
    missing = [p.name for p in (stop, start) if not p.is_file()]
    if missing:
        raise RestartError(f"{', '.join(missing)} not found beside the app in {root}; restart it by hand")
    log_dir.mkdir(parents=True, exist_ok=True)
    return spawn_detached(command(root, port), root, log_dir / LOG_NAME)


def spawn_detached(cmd: list[str], cwd: Path, log_path: Path, env: dict[str, str] | None = None) -> Path:
    """Start a PowerShell that outlives this process - one that will stop the proxy, or wait for it
    to restart - writing to log_path. `env` is added to the child's environment only (a secret it
    needs goes there, never on its command line)."""
    child_env = {**os.environ, **env} if env else None
    # CREATE_NO_WINDOW, never DETACHED_PROCESS. They read like synonyms and are not: DETACHED_PROCESS
    # gives the child no console object at all, and powershell.exe is a console application, so its
    # host has nothing to attach to. It then exits 0 without running the command - success, silently,
    # having done nothing. That is exactly what this did: the page said "Restarting", restart.log was
    # created empty by the open() below, and the proxy never went anywhere.
    # CREATE_NO_WINDOW gives it a real console that is simply never shown, which is what is wanted.
    # The child still outlives us: on Windows a process is not killed when its parent dies, and
    # BREAKAWAY covers the one thing that would kill it, a job object closing with the proxy.
    hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: survive a job that closes with the proxy
    with open(log_path, "ab") as out:
        for flags in (hidden | breakaway, hidden):
            try:
                subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, creationflags=flags,
                                 close_fds=True, env=child_env)
                return log_path
            except OSError as exc:  # a job that forbids breakaway refuses the first attempt
                last = exc
    raise RestartError(f"could not start PowerShell to restart the proxy: {last}")
