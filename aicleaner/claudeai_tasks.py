"""Route B, safeguards 2 and 3 (spec 2026-10-21-desktop-chat-route-b.md, section 6): the PAC service and the
watchdog, registered when route B is switched on and removed when it is switched off.

The PAC file is written to the state folder (claudeai.pac) by aicleaner, and served by the PAC service
(pacserve.py, task aicleaner-pac) - so it is there even when aicleaner is not. The watchdog (task
aicleaner-watchdog, installer/Watch-AiCleaner.ps1, every 5 minutes) starts aicleaner again when it is down and
says so. Both are this user's own tasks: no administrator, no prompt - so they follow the switch remotely too.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("aicleaner.claudeai")

PAC_FILE = "claudeai.pac"
PAC_PORT = 8789
TIMEOUT_S = 60


class TaskError(RuntimeError):
    """The tasks could not be registered or removed, with a reason worth showing."""


def write_pac(state_dir: Path, text: str) -> Path:
    path = Path(state_dir) / PAC_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(PAC_FILE + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return path


def pac_url(port: int = PAC_PORT) -> str:
    return f"http://127.0.0.1:{port}/claudeai.pac"


def _script(app_root: Path) -> Path:
    return Path(app_root) / "installer" / "Watch-AiCleaner.ps1"


def applies(app_root: Path) -> bool:
    """Only an installed aicleaner on Windows, with the script beside it - never a test or a dev checkout's run."""
    return (sys.platform == "win32" and not os.environ.get("AICLEANER_CLIENT_HOME")
            and _script(app_root).exists())


def _run(app_root: Path, *args: str) -> str:
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(_script(app_root)), *args, "-LogPath", ""]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskError(f"the watchdog and PAC service tasks could not be changed ({exc})") from exc
    if done.returncode != 0:
        raise TaskError("the watchdog and PAC service tasks could not be changed: "
                        f"{(done.stdout + done.stderr).strip()[-300:]}")
    return done.stdout.strip()


def install(app_root: Path, state_dir: Path, proxy_port: int, pac_port: int = PAC_PORT) -> None:
    _run(app_root, "-Install", "-Port", str(proxy_port), "-PacPort", str(pac_port),
         "-PacFile", str(Path(state_dir) / PAC_FILE))
    log.info("route B: watchdog and PAC service registered (PAC on port %d)", pac_port)


def uninstall(app_root: Path) -> None:
    _run(app_root, "-Uninstall")
    log.info("route B: watchdog and PAC service removed")
