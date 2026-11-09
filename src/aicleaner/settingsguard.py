"""Installs or removes the settings guard in Claude Code's user settings (feature 70, AI Cleaner 1.1).

The guard (hooks/settings_guard.py) stops a tool call that would change AI Cleaner's settings other than through its
page: the installed config written directly, the API key read or sent (H2), the guard itself taken out. Always on - a
switch would be one more thing to change - so it is kept in step at every proxy start, like the other hooks, with
bashguard's care: a backup first, only its own entry touched, a settings file that does not parse left alone.
Removed by `aicleaner uninstall`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .bashguard import user_settings

log = logging.getLogger("aicleaner.settingsguard")

HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "settings_guard.py"
MARKER = "settings_guard.py"


class SettingsGuardError(RuntimeError):
    """The settings file could not be changed, with a reason worth showing."""


def hook_command(install: Path, state: Path, python: str | Path | None = None) -> str:
    """Forward slashes throughout: Claude Code runs hook commands through Git Bash on Windows, which would halve a
    Windows path's backslashes. The console Python, so the refusal reaches Claude on stderr."""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return (f'"{exe.as_posix()}" "{HOOK_SCRIPT.as_posix()}" "{Path(install).as_posix()}" '
            f'"{Path(state).as_posix()}"')


def _ours(group: object) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def updated(doc: dict, on: bool, install: Path, state: Path, python: str | Path | None = None) -> dict:
    out = dict(doc)
    hooks = dict(out.get("hooks") or {})
    groups = [g for g in (hooks.get("PreToolUse") or []) if not _ours(g)]
    if on:
        groups.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_command(install, state, python),
                                                  "timeout": 5}]})
    if groups:
        hooks["PreToolUse"] = groups
    else:
        hooks.pop("PreToolUse", None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def apply(on: bool, install: Path, state: Path, home: Path | None = None, python: str | Path | None = None) -> str:
    """Make the user settings match. Returns what was done (empty when nothing changed); raises SettingsGuardError
    when the file cannot be read as settings or written."""
    target = user_settings(home)
    try:
        doc = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise SettingsGuardError(f"{target} could not be read as JSON ({exc}); it was left as it is") from exc
    if not isinstance(doc, dict):
        raise SettingsGuardError(f"{target} is not a settings object; it was left as it is")
    new = updated(doc, on, install, state, python)
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
        raise SettingsGuardError(f"{target} could not be written ({exc})") from exc
    said = ("Claude can no longer change AI Cleaner's settings except through you (settings guard installed)" if on
            else "settings guard removed from Claude Code")
    log.info("%s (%s)", said, target)
    return said
