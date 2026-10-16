"""Feedback from inside aicleaner (spec docs/superpowers/specs/2026-10-16-feedback-design.md).

The proxy talks to the feedback relay for the page; the page never does. Nothing is sent before the
person agrees, and what is sent is filtered: their own text through the client-name filter, and the
system context assembled here - settings switched on or off, counts, versions, never values such as
folders, tool names, client names, hostnames or user names - then scrubbed against a deny-list.

**Identity:** a one-way hash of this Windows install's MachineGuid and the user's SID, so the same
machine and account always produce the same ID - a reinstall reconnects to the same tickets with
nothing kept - and the relay learns neither. **Consent** is a small file outside the app folder that
the uninstaller leaves behind, so a reinstall is already connected without asking again.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

RELAY = "https://aicleaner-feedback.biosconsulting.com.au"
TIMEOUT_S = 20
TITLE_MAX = 250
BODY_MAX = 20000
LOG_LINES = 20
# Enum settings worth knowing for a bug: which way something is set, never a path or a name.
ENUM_SETTINGS = ("session.permission_mode", "secrets.mode", "remote.key_storage", "editor.preview_dblclick")


class FeedbackError(RuntimeError):
    """The relay could not be reached, or refused; the message says which, in words."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ---- identity and consent ----------------------------------------------------------------------

def _machine_guid() -> str:
    if sys.platform != "win32":
        return socket.gethostname()
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        return str(winreg.QueryValueEx(key, "MachineGuid")[0])


def _user_sid() -> str:
    if sys.platform != "win32":
        return str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USER", "")
    done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    found = re.search(r"S-1-[0-9-]+", done.stdout or "")
    return found.group(0) if found else os.environ.get("USERNAME", "")


def derive_id(machine_guid: str, sid: str) -> str:
    """The same 32 hex characters for this machine and account, every time; neither can be read back."""
    return hashlib.sha256(f"aicleaner-feedback/v1|{machine_guid}|{sid}".encode("utf-8")).hexdigest()[:32]


_ID: str = ""


def client_id() -> str:
    global _ID
    if not _ID:
        _ID = derive_id(_machine_guid(), _user_sid())
    return _ID


def consent_file() -> Path:
    # AICLEANER_FEEDBACK_HOME: a test server's own folder, so a test never writes the real marker.
    override = os.environ.get("AICLEANER_FEEDBACK_HOME", "").strip()
    if override:
        return Path(override) / "consent.json"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "aicleaner-feedback" / "consent.json"


def consent(path: Path | None = None) -> dict[str, Any] | None:
    path = path or consent_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("client_id") else None
    except (OSError, ValueError):
        return None


def save_consent(cid: str, relay: str, at: str, path: Path | None = None) -> None:
    path = path or consent_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"consented_at": at, "client_id": cid, "relay": relay}, indent=1), encoding="utf-8")


def forget_consent(path: Path | None = None) -> None:
    path = path or consent_file()
    try:
        path.unlink()
    except OSError:
        pass


# ---- scrubbing -----------------------------------------------------------------------------------

def _deny_patterns() -> list[tuple[re.Pattern, str]]:
    names = {os.environ.get("USERNAME", ""), os.environ.get("USER", ""), Path.home().name,
             os.environ.get("COMPUTERNAME", ""), socket.gethostname().split(".")[0]}
    names = {n for n in names if len(n) >= 3}
    patterns = [
        (re.compile(r"(?i)\b[a-z]:[\\/][^\s\"'<>|]*"), "<path>"),
        (re.compile(r"\\\\[^\s\\\"']+\\[^\s\"']*"), "<path>"),
        (re.compile(r"(?<![\w.])/(?:home|users|mnt|var|etc|opt|srv)/[^\s\"']*", re.I), "<path>"),
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
        # Not after a slash or a letter: Edg/141.0.3537.71 is a browser version, not an address.
        (re.compile(r"(?<![\w/.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])(?<!127\.0\.0\.1)"), "<address>"),
        (re.compile(r"https?://[^\s\"'<>]+"), "<url>"),
    ]
    for name in sorted(names, key=len, reverse=True):
        patterns.append((re.compile(r"(?i)\b" + re.escape(name) + r"\b"), "<name>"))
    return patterns


def scrub(text: str) -> str:
    """Paths, e-mail addresses, IP addresses, web addresses, the user and machine names: replaced."""
    for pattern, stand_in in _deny_patterns():
        text = pattern.sub(stand_in, text)
    return text


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    return value


# ---- the context ---------------------------------------------------------------------------------

def settings_summary(settings: dict[str, Any]) -> dict[str, Any]:
    """What is switched on or off and the few enums; lists become their length; nothing else."""
    out: dict[str, Any] = {}
    for key, value in sorted(settings.items()):
        if isinstance(value, bool):
            out[key] = value
        elif key in ENUM_SETTINGS:
            out[key] = value
        elif isinstance(value, int) and key in ("session.max_running", "logs.detail_retention_days"):
            out[key] = value
        elif isinstance(value, list):
            out[key] = f"{len(value)} entries"
    return out


def route_only(address: str) -> str:
    """The page's route with any name in it replaced: #/session/Acme -> #/session/<name>."""
    hash_part = address.split("#", 1)[1] if "#" in address else ""
    parts = [p for p in hash_part.split("/") if p]
    if not parts:
        return "#/"
    return "#/" + "/".join(parts[:1] + ["<name>" for _ in parts[1:2]])


def channel_of(repo_url: str) -> str:
    return "dev" if repo_url.rstrip("/").lower().endswith("-dev") else "public"


def recent_log_lines(log_dir: Path, tokenise: Callable[[str], str]) -> list[str]:
    path = log_dir / "proxy-stderr.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_LINES:]
    except OSError:
        return []
    return [scrub(tokenise(line))[:400] for line in lines]


def build_context(page: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    """The page's part (as sent, narrowed to known keys) and the server's part, scrubbed together."""
    allowed_page = {"route", "panels", "cards", "focus", "browser", "screen", "theme", "layout"}
    from_page = {k: v for k, v in (page or {}).items() if k in allowed_page}
    if "route" in from_page:
        from_page["route"] = route_only(str(from_page["route"]))
    return scrub_value({**server, "page": from_page})


def system_info() -> dict[str, str]:
    return {"windows": platform.platform(terse=True), "python": platform.python_version()}


# ---- the relay -----------------------------------------------------------------------------------

def call(relay: str, method: str, path: str, cid: str, body: dict | None = None,
         opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(relay.rstrip("/") + path, data=data, method=method,
                                     headers={"content-type": "application/json", "x-client-id": cid,
                                              "user-agent": "aicleaner"})
    try:
        with opener(request, timeout=TIMEOUT_S) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read() or b"{}").get("error") or exc.reason
        except ValueError:
            said = exc.reason
        raise FeedbackError(f"the feedback system said: {said}", status=exc.code if exc.code in (400, 401, 403, 404, 429) else 502) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FeedbackError(f"could not reach the feedback system ({relay}): {getattr(exc, 'reason', exc)}") from exc
