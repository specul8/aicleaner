"""D239: an administrator step started by AI Cleaner opens on the signed-in person's desktop.

Started with the computer (feature 107), AI Cleaner runs as the start-up task in session 0, which has no desktop. A
PowerShell it starts with `Start-Process -Verb RunAs` ran there too, so Windows' administrator prompt - and the
script's password window after it - waited where nobody could see them (Brad, 2026-11-12: two left waiting).

So an administrator step is launched by `launch()`:

- AI Cleaner in a desktop session (signed in as usual): the hidden PowerShell that asks Windows is started directly,
  as before;
- AI Cleaner in session 0 and this user signed in (at the console or over Remote Desktop): a one-off scheduled task,
  this user's own, "run only when signed in" (LogonType Interactive, the limited token) runs a small launcher in that
  session. The launcher runs the same line, then removes its task and itself. No administrator is needed to register
  a task that runs as yourself;
- AI Cleaner in session 0 and nobody signed in: `NoDesktop`, carrying the command to run by hand in an elevated
  PowerShell. Nothing waits for a prompt nobody can see.

A test server (AICLEANER_CLIENT_HOME set) never registers a real task: it is given stand-ins, or is refused.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("aicleaner.deskrun")

TASK_PREFIX = "aicleaner-desktop-"
REGISTER_TIMEOUT_S = 60
WTS_ACTIVE = 0
WTS_USER_NAME = 5
WTS_DOMAIN_NAME = 7


class DesktopError(RuntimeError):
    """The step could not be handed to the desktop, with a reason worth showing."""


class NoDesktop(DesktopError):
    """Nobody is signed in to this computer, so no administrator prompt could be seen. `command` is what to run."""

    def __init__(self, message: str, command: str) -> None:
        super().__init__(message)
        self.command = command


@dataclass
class Handoff:
    """Where the step went: "here" (this process has a desktop) or "desktop" (session `session`, task `task`)."""
    where: str
    session: int | None = None
    task: str = ""


def powershell() -> str:
    """Windows PowerShell by its full path, never by search order."""
    return str(Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" /
               "powershell.exe")


# ---- which session -----------------------------------------------------------------------------

def own_session() -> int:
    """This process's Windows session id (0 = the services session, no desktop). -1 when not known."""
    if sys.platform != "win32":
        return -1
    found = ctypes.c_uint32()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(found)):  # type: ignore[attr-defined]
        return -1
    return int(found.value)


class _SessionInfo(ctypes.Structure):
    _fields_ = [("SessionId", ctypes.c_uint32), ("pWinStationName", ctypes.c_wchar_p), ("State", ctypes.c_int)]


def _session_text(wtsapi: Any, session: int, what: int) -> str:
    buffer = ctypes.c_wchar_p()
    size = ctypes.c_uint32()
    if not wtsapi.WTSQuerySessionInformationW(None, session, what, ctypes.byref(buffer), ctypes.byref(size)):
        return ""
    try:
        return buffer.value or ""
    finally:
        wtsapi.WTSFreeMemory(buffer)


def active_sessions() -> list[tuple[int, str, str]] | None:
    """(session, domain, user) of every active session on this computer; None when Windows would not say."""
    if sys.platform != "win32":
        return None
    try:
        wtsapi = ctypes.windll.wtsapi32  # type: ignore[attr-defined]
        listed = ctypes.POINTER(_SessionInfo)()
        count = ctypes.c_uint32()
        if not wtsapi.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(listed), ctypes.byref(count)):
            return None
        try:
            rows = [(listed[i].SessionId, listed[i].State) for i in range(count.value)]
        finally:
            wtsapi.WTSFreeMemory(listed)
        return [(sid, _session_text(wtsapi, sid, WTS_DOMAIN_NAME), _session_text(wtsapi, sid, WTS_USER_NAME))
                for sid, state in rows if state == WTS_ACTIVE and sid != 0]
    except (OSError, AttributeError) as exc:
        log.info("the signed-in sessions could not be listed: %s", exc)
        return None


def this_user() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")


def sessions_of(user: str, listed: list[tuple[int, str, str]] | None) -> list[int] | None:
    """The active sessions signed in as `user` (DOMAIN\\name or name); None when not known."""
    if listed is None:
        return None
    domain, _, name = user.rpartition("\\")
    return [sid for sid, their_domain, their_name in listed
            if their_name.lower() == name.lower() and (not domain or not their_domain
                                                        or their_domain.lower() == domain.lower())]


# ---- the hand-off ------------------------------------------------------------------------------

def _quote(text: str) -> str:
    """A single-quoted PowerShell string."""
    return "'" + str(text).replace("'", "''") + "'"


def launcher_text(launch_line: str, task: str) -> str:
    """The launcher the task runs in the person's session: the line, then its own task and file removed."""
    return (f"# {task}.ps1 - written by AI Cleaner (D239): hands one administrator step to this desktop, then removes\n"
            f"# its scheduled task and itself.\n"
            f"try {{\n    {launch_line}\n}}\n"
            f"finally {{\n"
            f"    Unregister-ScheduledTask -TaskName {_quote(task)} -Confirm:$false -ErrorAction SilentlyContinue\n"
            f"    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue\n"
            f"}}\n")


def register_text(task: str, launcher: Path, user: str) -> str:
    """The PowerShell that registers and starts the one-off task. Earlier ones not running are removed first."""
    argument = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{launcher}"'
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"Get-ScheduledTask -TaskName {_quote(TASK_PREFIX + '*')} -ErrorAction SilentlyContinue |",
        "    Where-Object { $_.State -ne 'Running' } |",
        "    ForEach-Object { Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false -ErrorAction SilentlyContinue }",
        f"$action = New-ScheduledTaskAction -Execute {_quote(powershell())} -Argument {_quote(argument)}",
        f"$principal = New-ScheduledTaskPrincipal -UserId {_quote(user)} -LogonType Interactive -RunLevel Limited",
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30)",
        f"Register-ScheduledTask -TaskName {_quote(task)} -Action $action -Principal $principal -Settings $settings "
        "-Force | Out-Null",
        f"Start-ScheduledTask -TaskName {_quote(task)}",
        "'started'",
    ])


def _run_powershell(script: str) -> tuple[int, str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", encoded], capture_output=True, text=True, timeout=REGISTER_TIMEOUT_S,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.returncode, (done.stdout or "") + (done.stderr or "")


@dataclass
class Desktop:
    """What `launch` asks Windows, each replaceable by a test: this process's session, the active sessions, how a
    hidden PowerShell is started here, and how a PowerShell script is run to register the task."""
    own: Callable[[], int] = own_session
    listed: Callable[[], list[tuple[int, str, str]] | None] = active_sessions
    popen: Callable[..., Any] = subprocess.Popen
    run: Callable[[str], tuple[int, str]] = _run_powershell
    user: str = field(default_factory=this_user)

    @classmethod
    def here_only(cls, popen: Callable[..., Any]) -> "Desktop":
        """A stand-in that always has a desktop (a test's launcher)."""
        return cls(own=lambda: 1, listed=lambda: [], popen=popen, run=lambda script: (1, "not for tests"))


def _real(desk: Desktop) -> bool:
    return desk.run is _run_powershell


def launch(launch_line: str, manual: str, folder: Path, desk: Desktop | None = None) -> Handoff:
    """Start `launch_line` (PowerShell that asks Windows for administrator) where the person can see it.

    `manual` is the command to show when nobody is signed in. `folder` holds the launcher for the task (AI Cleaner's
    state folder). Raises NoDesktop or DesktopError."""
    desk = desk or Desktop()
    session = desk.own()
    if session != 0:
        try:
            desk.popen([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                        launch_line], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise DesktopError(f"the change could not be started ({exc})") from exc
        return Handoff("here", session if session >= 0 else None)
    found = sessions_of(desk.user, desk.listed())
    if found is not None and not found:
        raise NoDesktop("AI Cleaner is running as the start-up task and nobody is signed in to this computer, so "
                        "Windows has nowhere to show the administrator prompt. Sign in to this computer (at it or by "
                        "Remote Desktop) and try again, or run this command in PowerShell as administrator:", manual)
    if _real(desk) and os.environ.get("AICLEANER_CLIENT_HOME", "").strip():
        raise DesktopError("a test server does not register scheduled tasks")
    task = f"{TASK_PREFIX}{uuid.uuid4().hex[:8]}"
    launcher = Path(folder) / f"{task}.ps1"
    try:
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(launcher_text(launch_line, task), encoding="utf-8-sig")
    except OSError as exc:
        raise DesktopError(f"the hand-over to your desktop could not be written in {folder} ({exc})") from exc
    try:
        code, said = desk.run(register_text(task, launcher, desk.user))
    except (OSError, subprocess.TimeoutExpired) as exc:
        code, said = 1, str(exc)
    if code != 0 or "started" not in said:
        launcher.unlink(missing_ok=True)
        log.warning("handing the administrator step to the desktop failed (%s): %s", code, said.strip()[-500:])
        raise DesktopError("AI Cleaner is running as the start-up task, and the step could not be handed to your "
                           f"desktop ({said.strip().splitlines()[-1] if said.strip() else f'exit {code}'}). Run this "
                           f"command in PowerShell as administrator instead: {manual}")
    log.info("administrator step handed to the desktop (task %s, session %s)", task, found or "unknown")
    return Handoff("desktop", found[0] if found else None, task)
