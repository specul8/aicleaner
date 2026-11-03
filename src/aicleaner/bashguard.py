"""Installs or removes the Bash guard hook in Claude Code's user settings (feature 98).

On Windows, Claude Code's Bash tool halves every doubled backslash before Git Bash sees it, even
inside a quoted heredoc; Write, Edit and the PowerShell tool do not. The guard (hooks/bash_guard.py)
stops a Bash call holding `\\` and tells the model to use those instead. A hook cannot tell Claude's
own Bash calls from a skill's or a plugin's, so this is the guard only - Bash itself stays (decided
with the user 2026-10-08).

Like `rules`, this writes a file in the person's home folder from a settings switch, deliberately,
with the installer's care: a backup first, only our own entry touched, and a settings file that does
not parse is left alone and reported. With the guard on, CLAUDE_CODE_USE_POWERSHELL_TOOL=1 is added
when missing - a Bash deny rule would otherwise turn the PowerShell tool off too, and it is the tool
the advice points to. Off leaves that variable alone: it may be the person's own.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

log = logging.getLogger("aicleaner.bashguard")

HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "bash_guard.py"
MARKER = "bash_guard.py"
POWERSHELL_TOOL = "CLAUDE_CODE_USE_POWERSHELL_TOOL"


class GuardError(RuntimeError):
    """The settings file could not be changed, with a reason worth showing."""


def applies() -> bool:
    """Only Windows has the fault; elsewhere the switch is hidden and nothing is written."""
    return os.name == "nt"


def user_settings(home: Path | None = None) -> Path:
    """Claude Code's user settings. Honours AICLEANER_CLIENT_HOME, as `rules` and `routing` do, so a
    test server never rewrites the settings of whoever is running it."""
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    base = home or (Path(override) if override else Path.home())
    return base / ".claude" / "settings.json"


def hook_command(python: str | Path | None = None) -> str:
    """The command Claude Code runs. Forward slashes throughout: on Windows hook commands go through
    Git Bash, which would halve the backslashes of a Windows path - the very fault this guards."""
    exe = Path(python or sys.executable)
    # The proxy may run windowless (pythonw.exe), which has no stderr - and stderr is how the advice
    # reaches Claude. Its console twin sits beside it.
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return f'"{exe.as_posix()}" "{HOOK_SCRIPT.as_posix()}"'


def _ours(group: object) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def updated(doc: dict, on: bool, python: str | Path | None = None) -> dict:
    """The settings document with our PreToolUse entry removed and, when on, added afresh (so a moved
    install or another Python is corrected), and the PowerShell tool switched on when missing."""
    out = dict(doc)
    hooks = dict(out.get("hooks") or {})
    groups = [g for g in (hooks.get("PreToolUse") or []) if not _ours(g)]
    if on:
        groups.append({"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command(python), "timeout": 10}]})
        env = dict(out.get("env") or {})
        env.setdefault(POWERSHELL_TOOL, "1")
        out["env"] = env
    if groups:
        hooks["PreToolUse"] = groups
    else:
        hooks.pop("PreToolUse", None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def apply(on: bool, home: Path | None = None, python: str | Path | None = None) -> str:
    """Make the user settings match the switch. Returns what was done, for the page; raises
    GuardError when the file cannot be read as settings or written."""
    if not applies():
        return ""
    target = user_settings(home)
    try:
        doc = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise GuardError(f"{target} could not be read as JSON ({exc}); it was left as it is") from exc
    if not isinstance(doc, dict):
        raise GuardError(f"{target} is not a settings object; it was left as it is")
    new = updated(doc, on, python)
    if new == doc:
        return ""
    if target.exists():
        stamped = target.with_name(f"{target.name}.{datetime.now():%Y%m%dT%H%M%S}.bak")
        try:
            shutil.copy2(target, stamped)
        except OSError as exc:     # worth saying, not worth stopping for
            log.warning("could not back up %s: %s", target, exc)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{target.name}.aicleaner-tmp")
        temp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, target)
    except OSError as exc:
        raise GuardError(f"{target} could not be written ({exc})") from exc
    said = "Bash guard installed in Claude Code" if on else "Bash guard removed from Claude Code"
    log.info("%s (%s)", said, target)
    return f"{said}; restart Claude Code sessions for it to take effect."
