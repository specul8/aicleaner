"""Who has their hands on the wheel (DECISIONS 38).

Every event in the substitution log, and every change made through the UI, says who was driving:

- **Local**: the Windows account aicleaner's caller runs as - `DOMAIN\\user`, and the UPN where the
  machine is joined. Over VS Code Remote the account is still a real identity (SSH authenticated
  it), so `ssh_from` adds the source addresses of the SSH connections open at the time. That says
  who COULD be driving over SSH; it does not attribute a request to one of them, and never claims
  to.
- **Remote** (through the tunnel): the email from the Cloudflare Access token `guard.py` has already
  verified, the country from the same token, the client address Cloudflare saw, and the browser's
  user agent. A browser never reveals a device name; the user agent is the nearest thing.
- **The Session page**: model traffic comes from the Claude Code process the page started, not from
  the person, so the page's start and prompt calls note who made them (`DRIVERS`) and the proxy
  reads that back by session name.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any

from .guard import ACCESS_COUNTRY, ACCESS_EMAIL, REMOTE

log = logging.getLogger("aicleaner.who")

SSH_PORT = 22
SSH_CACHE_S = 15
_CMD_TIMEOUT_S = 3

_local: dict[str, Any] | None = None
_local_lock = threading.Lock()
_ssh: tuple[float, list[str]] = (0.0, [])
_ssh_lock = threading.Lock()


def _run(args: list[str]) -> str:
    """A command's output, or "" on any failure. Never raises: identity is evidence, not a gate."""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run(args, capture_output=True, text=True, timeout=_CMD_TIMEOUT_S,
                              creationflags=flags)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s: %s", args[0], exc)
        return ""


def local_identity() -> dict[str, str]:
    """The account this process runs as: DOMAIN\\user and, where the machine is joined, the UPN.

    Worked out once: neither changes while the process runs, and `whoami /upn` takes a moment.
    """
    global _local
    with _local_lock:
        if _local is None:
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            domain = os.environ.get("USERDOMAIN", "")
            upn = _run(["whoami", "/upn"]).strip() if sys.platform == "win32" else ""
            # whoami prints an error, not a UPN, on a machine that is not joined; returncode is
            # usually non-zero then, but the text is checked too.
            if "@" not in upn or " " in upn:
                upn = ""
            _local = {"user": f"{domain}\\{user}" if domain else user, "upn": upn}
        return dict(_local)


_NETSTAT_LINE = re.compile(r"^\s*TCP\s+(\S+)\s+(\S+)\s+ESTABLISHED\b", re.IGNORECASE)


def parse_ssh_sources(netstat: str, port: int = SSH_PORT) -> list[str]:
    """Source addresses of established connections to the local SSH port, from `netstat -ano`."""
    found: set[str] = set()
    for line in netstat.splitlines():
        m = _NETSTAT_LINE.match(line)
        if not m:
            continue
        local, remote = m.group(1), m.group(2)
        if not local.endswith(f":{port}"):
            continue
        address = remote.rsplit(":", 1)[0].strip("[]")
        if address:
            found.add(address)
    return sorted(found)


def ssh_sources() -> list[str]:
    """The SSH connections open now, cached for SSH_CACHE_S. Blocking: call it off the event loop."""
    global _ssh
    if sys.platform != "win32":
        return []
    with _ssh_lock:
        at, cached = _ssh
        if time.monotonic() - at < SSH_CACHE_S:
            return list(cached)
        sources = parse_ssh_sources(_run(["netstat", "-ano", "-p", "TCP"]))
        _ssh = (time.monotonic(), sources)
        return list(sources)


def remote_identity(request: Any) -> dict[str, str]:
    """Who sent a request that came through the tunnel. Only meaningful once the guard verified it."""
    headers = getattr(request, "headers", {}) or {}
    return {
        "via": "remote",
        "email": str(request.get(ACCESS_EMAIL, "") or ""),
        "country": str(request.get(ACCESS_COUNTRY, "") or ""),
        "ip": str(headers.get("Cf-Connecting-IP", "") or ""),
        "agent": str(headers.get("User-Agent", "") or ""),
    }


def identity_of(request: Any, ssh_from: list[str] | None = None) -> dict[str, Any]:
    """Who made a UI request: the verified remote person, or the local account."""
    if request.get(REMOTE):
        return remote_identity(request)
    who: dict[str, Any] = {"via": "local", **local_identity()}
    if ssh_from:
        who["ssh_from"] = list(ssh_from)
    agent = (getattr(request, "headers", {}) or {}).get("User-Agent", "")
    if agent:
        who["agent"] = agent
    return who


def form_author() -> str:
    """The name written on a form this PC adds to a client (DECISIONS 67): the UPN where the
    machine is joined - what a colleague recognises - else DOMAIN\\user."""
    me = local_identity()
    return me.get("upn") or me.get("user") or ""


def local_driver(ssh_from: list[str] | None = None) -> dict[str, Any]:
    """Who is driving model traffic that did not come from the Session page."""
    who: dict[str, Any] = {"via": "local", **local_identity()}
    if ssh_from:
        who["ssh_from"] = list(ssh_from)
    return who


class Drivers:
    """Who last started or prompted each Session-page session, by session name."""

    def __init__(self) -> None:
        self._by_name: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def note(self, name: str, who: dict[str, Any]) -> None:
        if name:
            with self._lock:
                self._by_name[name] = dict(who)

    def get(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            found = self._by_name.get(name)
            return dict(found) if found is not None else None


DRIVERS = Drivers()
