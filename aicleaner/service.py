"""Feature 107 (spec 2026-10-22-f107-run-as-a-service.md, decided 2026-10-24): aicleaner started with the computer,
before anyone signs in, AS YOU - a start-up scheduled task that runs whether you are signed in or not, with your
password kept by Windows, at your limited (non-elevated) token as today. Switched from Settings > Running aicleaner.

The work is installer/Set-AiCleanerService.ps1. A boot trigger needs an administrator, and the password is asked for
on this machine, so the change is local only: the page is answered at once, Windows shows the administrator prompt
and then the script's window asks for the password. Reading the state needs no elevation (-Mode Status).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

TASK = "aicleaner-service"
TIMEOUT_S = 30
# The task's last result when Windows could not sign it in: the password is wrong (changed since it was stored),
# or the account may no longer log on as a batch job.
LOGON_FAILURES = {
    0x8007052E, 1326,          # ERROR_LOGON_FAILURE: the user name or password is incorrect
    0x80070569, 1385,          # ERROR_LOGON_TYPE_NOT_GRANTED
    0x80070532, 1330,          # ERROR_PASSWORD_EXPIRED
    0x80070533, 1331,          # ERROR_ACCOUNT_DISABLED
}


class ServiceError(RuntimeError):
    """The change could not be started, with a reason worth showing."""


def powershell() -> str:
    """Windows PowerShell by its full path, never by search order."""
    return str(Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" /
               "powershell.exe")


def script(root: Path) -> Path:
    return Path(root) / "installer" / "Set-AiCleanerService.ps1"


def logon_failed(result: Any) -> bool:
    """True when a task's last result says Windows could not sign it in. Accepts the signed or unsigned form."""
    try:
        code = int(result)
    except (TypeError, ValueError):
        return False
    return (code & 0xFFFFFFFF) in LOGON_FAILURES


def off(windows: bool = True) -> dict[str, Any]:
    return {"mode": "off", "windows": windows, "task": None, "tunnel": None, "password_problem": False}


def parse(stdout: str) -> dict[str, Any]:
    """The script's Status JSON (its last line that is JSON) as the page's state."""
    found: dict[str, Any] | None = None
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                found = json.loads(line)
                break
            except ValueError:
                continue
    if not found:
        return off()
    task = found.get("task") or None
    tunnel = found.get("tunnel") or None
    problem = bool(task) and (logon_failed(task.get("last_result")) or task.get("state") == "Disabled")
    return {"mode": "user" if task else "off", "windows": True, "task": task, "tunnel": tunnel,
            "password_problem": problem, "user": found.get("user", ""), "since": found.get("since", "")}


def _run_status(root: Path) -> str:
    done = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                           str(script(root)), "-Mode", "Status", "-LogPath", ""],
                          capture_output=True, text=True, timeout=TIMEOUT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.stdout


def state(root: Path, runner: Callable[[Path], str] | None = None) -> dict[str, Any]:
    """Whether the start-up task exists, its state and last result, read without elevation. A test server (its
    own client home) never asks Windows: it would read this machine's real task."""
    if runner is None:
        if sys.platform != "win32":
            return off(windows=False)
        if os.environ.get("AICLEANER_CLIENT_HOME") or not script(root).exists():
            return off()
        runner = _run_status
    try:
        return parse(runner(Path(root)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return off()


def command(root: Path, on: bool, port: int, user: str = "", profile: str = "") -> list[str]:
    """The hidden PowerShell that asks Windows to run the script elevated, in a window you can see - it asks for
    your password there. The account and profile are passed on, so an administrator who is someone else (a
    standard user's over-the-shoulder prompt) still sets the task up for you."""
    user = user or f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    profile = profile or os.environ.get("USERPROFILE", "")
    root = Path(root)

    def quoted(value: Any) -> str:
        # A double-quoted argument inside a single-quoted PowerShell string: the single quotes doubled.
        return '"' + str(value).replace("'", "''") + '"'

    inner = (f"-NoProfile -ExecutionPolicy Bypass -File {quoted(script(root))} -Mode {'User' if on else 'Off'} "
             f"-Port {int(port)} -AppFolder {quoted(root)} -UserName {quoted(user)} -UserProfile {quoted(profile)} "
             f"-PauseAtEnd")
    launch = (f"Start-Process -FilePath '{powershell()}' -Verb RunAs -WindowStyle Normal "
              f"-WorkingDirectory '{str(root).replace(chr(39), chr(39) * 2)}' -ArgumentList '{inner}'")
    return [powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", launch]


def change(root: Path, on: bool, port: int, launcher: Callable[..., Any] | None = None) -> str:
    """Start the elevated script on this machine and return at once with what happens next."""
    path = script(root)
    if sys.platform != "win32" and launcher is None:
        raise ServiceError("starting with the computer needs Windows")
    if not path.exists():
        raise ServiceError(f"{path.name} is not beside the app in {root}")
    launch = launcher or subprocess.Popen
    try:
        launch(command(root, on, port), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise ServiceError(f"the change could not be started ({exc})") from exc
    if on:
        return ("Windows is asking on this machine for administrator, and then a window asks for your Windows password. "
                "Once both are done, aicleaner restarts as the start-up task.")
    return ("Windows is asking on this machine for administrator. Once accepted, the start-up task is removed and "
            "aicleaner starts at your sign-in again.")
