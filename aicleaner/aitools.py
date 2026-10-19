"""The AI tools aicleaner knows, for the Settings card and the setup wizard (item H, 2026-10-19; spec
2026-10-18-ai-tools-page-design.md): for each, found on this machine or not, filtered by aicleaner or
not, signed in or not, and how to sign in - Claude Code's own sign-in driven from the page.

Read live on every call, as routing.py is: the files change outside aicleaner.

Test homes: with AICLEANER_CLIENT_HOME set (the tests' home) nothing is looked up on PATH unless
AICLEANER_CLAUDE names a stand-in for `claude`, and Claude Desktop's policy is read from
`<home>/desktop-policy.json` instead of the registry - so no test reports, or depends on, the real
machine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import routing
from .codetab_settings import home

log = logging.getLogger("aicleaner.aitools")

URL = re.compile(r"https://\S+/oauth/authorize\?\S+")
STATUS_TTL_S = 30          # `claude auth status` takes about a second; the card asks often
LOGIN_TIMEOUT_S = 600      # how long a started sign-in waits for its code
POLICY_KEY = r"SOFTWARE\Policies\Claude"


def _testing() -> bool:
    return bool(os.environ.get("AICLEANER_CLIENT_HOME", "").strip())


def _which(name: str) -> str:
    return "" if _testing() else (shutil.which(name) or "")


def claude_command() -> list[str]:
    """How to run `claude`: AICLEANER_CLAUDE (a stand-in; a .py runs with this Python), else PATH."""
    stand_in = os.environ.get("AICLEANER_CLAUDE", "").strip()
    if stand_in:
        return [sys.executable, stand_in] if stand_in.endswith(".py") else [stand_in]
    found = _which("claude")
    if not found:
        return []
    # A .cmd shim (npm installs) needs cmd to run it; the native installer's claude.exe does not.
    return ["cmd", "/c", found] if found.lower().endswith((".cmd", ".bat")) else [found]


# ---- signed in ---------------------------------------------------------------------------------

_status_cache: dict[str, Any] = {"at": 0.0, "value": None}


async def claude_status(fresh: bool = False) -> dict[str, Any] | None:
    """`claude auth status --json`, or None when it cannot be asked. Cached briefly."""
    if not fresh and time.time() - _status_cache["at"] < STATUS_TTL_S:
        return _status_cache["value"]
    command = claude_command()
    value = None
    if command:
        try:
            process = await asyncio.create_subprocess_exec(*command, "auth", "status", "--json",
                                                           stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            doc = json.loads(out.decode("utf-8", "replace") or "{}")
            value = doc if isinstance(doc, dict) else None
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            log.info("claude auth status not read: %s", exc)
    _status_cache.update(at=time.time(), value=value)
    return value


# ---- found and filtered ------------------------------------------------------------------------

def _extension(prefix: str, base: Path | None = None) -> bool:
    folder = home(base) / ".vscode" / "extensions"
    try:
        return any(entry.name.lower().startswith(prefix) for entry in folder.iterdir())
    except OSError:
        return False


def desktop_policy(base: Path | None = None) -> dict[str, str]:
    """Claude Desktop's policy values (what its installer writes), read without elevation."""
    if _testing():
        try:
            doc = json.loads((home(base) / "desktop-policy.json").read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in doc.items()} if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}
    if os.name != "nt":
        return {}
    import winreg

    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POLICY_KEY) as key:
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                out[name] = str(value)
                index += 1
    except OSError:
        pass
    return out


def desktop_found(base: Path | None = None) -> bool:
    if _testing():
        return (home(base) / "AppData" / "Local" / "AnthropicClaude").is_dir()
    local = Path(os.environ.get("LOCALAPPDATA") or home() / "AppData" / "Local")
    places = [local / "AnthropicClaude", local / "Programs" / "Claude" / "Claude.exe",
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Claude" / "Claude.exe"]
    if any(p.exists() for p in places):
        return True
    return any((local / "Microsoft" / "WindowsApps").glob("Claude_*"))


def _row(tool: str, name: str, found: bool, filtered: bool | None, filter_note: str,
         signed_in: bool | None, sign_in: dict[str, Any], detail: str = "") -> dict[str, Any]:
    return {"id": tool, "name": name, "found": found, "filtered": filtered, "filter_note": filter_note,
            "signed_in": signed_in, "sign_in": sign_in, "detail": detail}


async def rows(cfg: Any, copilot: dict[str, Any] | None = None, base: Path | None = None) -> list[dict[str, Any]]:
    """One row per tool aicleaner knows, in the order the page shows them."""
    port = int(getattr(cfg, "port", 8787))
    routed = {e["client"]: e["routed"] for e in routing.clients(port, home=base,
                                                                appdata=(home(base) / "AppData" / "Roaming") if base else None)}
    status = await claude_status()
    claude_found = bool(claude_command()) or (home(base) / ".claude").is_dir()
    claude_routed = bool(routed.get("Claude Code")) or any(v for k, v in routed.items() if k.startswith("VS Code"))
    who = ""
    if status and status.get("loggedIn"):
        who = ", ".join(str(x) for x in (status.get("email"), status.get("subscriptionType")) if x)
    out = [_row("claude-code", "Claude Code (VS Code and terminal)", claude_found, claude_routed,
                "routed through aicleaner" if claude_routed else "goes straight to Anthropic",
                None if status is None else bool(status.get("loggedIn")),
                {"kind": "drive", "label": "Sign in to Claude Code"} if claude_command() else
                {"kind": "steps", "steps": "Install Claude Code on this machine, then sign in here."},
                detail=who)]
    policy = desktop_policy(base)
    gateway = policy.get("inferenceGatewayBaseUrl", "")
    desktop_routed = bool(gateway) and routing._is_ours(gateway, port)
    out.append(_row("claude-desktop", "Claude Desktop (chat)", desktop_found(base), desktop_routed,
                    "routed through aicleaner (its gateway setting)" if desktop_routed else
                    "not routed - set up with the installer at the machine (it needs administrator)",
                    None, {"kind": "steps", "steps": "Claude Desktop signs in in its own window, on the machine."}))
    out.append(_row("codetab", "Claude Desktop's Code tab", desktop_found(base), bool(cfg.codetab_enabled),
                    "filtered (Settings > the Code tab)" if cfg.codetab_enabled else "not filtered - Settings > the Code tab",
                    None, {"kind": "steps", "steps": "Uses Claude Desktop's sign-in."}))
    copilot = copilot or {}
    vscode_copilot = _extension("github.copilot", base)
    pointed = bool((copilot.get("vscode") or {}).get("pointed"))
    out.append(_row("copilot-vscode", "GitHub Copilot in VS Code", vscode_copilot,
                    bool(cfg.copilot_enabled and pointed),
                    ("filtered" if pointed else (copilot.get("waiting") or "switched on, VS Code not pointed here yet"))
                    if cfg.copilot_enabled else "not filtered - Settings > GitHub Copilot in VS Code",
                    None, {"kind": "steps", "steps": "VS Code owns this sign-in: in VS Code, Accounts (the person icon, "
                                                     "bottom left) > Sign in with GitHub to use GitHub Copilot."}))
    copilot_cli = bool(_which("copilot")) or (home(base) / ".copilot").is_dir()
    out.append(_row("copilot-cli", "GitHub Copilot CLI", copilot_cli, False,
                    "not filtered yet (planned: sessions driven by it, item K)", None,
                    {"kind": "steps", "steps": "In a terminal on the machine: copilot, then /login - it shows a code "
                                               "to enter at github.com/login/device, which works from any device."}))
    codex = bool(_which("codex")) or (home(base) / ".codex").is_dir() or _extension("openai.chatgpt", base)
    codex_signed = (home(base) / ".codex" / "auth.json").is_file() if codex else None
    out.append(_row("codex", "Codex (ChatGPT's coding agent: VS Code and CLI)", codex, False,
                    "not filtered yet (needs a capture first, as Copilot's did)", codex_signed,
                    {"kind": "steps", "steps": "In VS Code's Codex panel: Sign in, with your ChatGPT account; or in a "
                                               "terminal on the machine: codex login."}))
    return out


# ---- Claude Code's sign-in, driven from the page ---------------------------------------------

class ClaudeLogin:
    """One `claude auth login` at a time. It prints a link whose sign-in page shows a code; the page
    passes that code back here and it is written to the waiting login (probed 2026-10-19)."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.state = "idle"            # idle | starting | waiting | checking | done | failed
        self.url = ""
        self.message = ""
        self.output: list[str] = []
        self.started = 0.0
        self._reader: asyncio.Task | None = None

    def view(self) -> dict[str, Any]:
        return {"state": self.state, "url": self.url, "message": self.message}

    async def start(self, console: bool = False) -> dict[str, Any]:
        if self.process is not None and self.process.returncode is None:
            if time.time() - self.started < LOGIN_TIMEOUT_S:
                return self.view()
            await self.cancel()
        command = claude_command()
        if not command:
            self.state, self.message = "failed", "Claude Code is not installed on this machine"
            return self.view()
        self.state, self.url, self.message, self.output = "starting", "", "", []
        self.started = time.time()
        args = [*command, "auth", "login"] + (["--console"] if console else [])
        env = {**os.environ, "BROWSER": "none"}      # the link is opened on the person's device, not here
        self.process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.STDOUT, env=env)
        self._reader = asyncio.create_task(self._read())
        deadline = time.time() + 30
        while not self.url and self.process.returncode is None and time.time() < deadline:
            await asyncio.sleep(0.1)
        if self.url:
            self.state = "waiting"
        elif self.state == "starting":
            self.state = "failed"
            self.message = "Claude Code did not show a sign-in link: " + " ".join(self.output[-3:])[-300:]
            await self.cancel(keep_state=True)
        return self.view()

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        buffer = ""
        while True:
            chunk = await self.process.stdout.read(1024)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            *lines, buffer = buffer.split("\n")
            self.output.extend(line.strip() for line in lines if line.strip())
            found = URL.search("\n".join(self.output[-5:]) + "\n" + buffer)
            if found and not self.url:
                self.url = found.group(0)
        if buffer.strip():
            self.output.append(buffer.strip())

    async def submit(self, code: str) -> dict[str, Any]:
        code = (code or "").strip()
        if self.state != "waiting" or self.process is None or self.process.stdin is None:
            self.message = "Start the sign-in first"
            return self.view()
        if not code:
            self.message = "Paste the code the sign-in page showed"
            return self.view()
        self.state, self.message = "checking", ""
        self.process.stdin.write((code + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=90)
        except asyncio.TimeoutError:
            await self.cancel(keep_state=True)
            self.state, self.message = "failed", "Claude Code did not finish signing in"
            return self.view()
        if self._reader is not None:
            await asyncio.wait_for(self._reader, timeout=5)
        status = await claude_status(fresh=True)
        if self.process.returncode == 0 and status and status.get("loggedIn"):
            self.state = "done"
            self.message = "Signed in" + (f" as {status.get('email')}" if status.get("email") else "")
        else:
            self.state = "failed"
            self.message = "Not signed in: " + (" ".join(self.output[-2:])[-300:] or "the code was not accepted")
        self.process = None
        return self.view()

    async def cancel(self, keep_state: bool = False) -> dict[str, Any]:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        self.process = None
        if not keep_state:
            self.state, self.url, self.message = "idle", "", ""
        return self.view()
