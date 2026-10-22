"""Installs or removes the PostToolUse hook that hands waiting messages to Claude between tool calls
(feature 119; spec docs/superpowers/specs/2026-10-14-f119-deliver-between-tools-design.md).

A prompt sent on the Session page while a turn runs used to wait for the end of the turn - ten
minutes and more - because the CLI does not read its input mid-turn. VS Code hands a message over at
the next gap between tool calls; a PostToolUse hook can do the same: Claude Code runs it after every
tool call and puts what it prints in front of the model on the next request. Proven with the CLI
headless, 2026-10-14: a hook added to settings mid-session is picked up, so running sessions need
no restart.

Written into Claude Code's user settings with bashguard's care - a backup first, only our own entry
touched, a settings file that does not parse left alone and reported - and kept in step at every
proxy start, so a deploy installs it and another port or a moved install is corrected.
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

log = logging.getLogger("aicleaner.deliver")

HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "deliver_queued.py"
MARKER = "deliver_queued.py"
# The rename-collapse guard (bug report 2026-10-14): a PreToolUse hook, written with the same care.
COLLAPSE_SCRIPT = Path(__file__).resolve().parent / "hooks" / "check_collapse.py"
COLLAPSE_MARKER = "check_collapse.py"
# Its partner after the call (D199, 2026-10-21): tells the model that real names went into what it just
# wrote, so it stops "correcting" correct file names it sees as tokens. Installed and removed with it.
NAMES_SCRIPT = Path(__file__).resolve().parent / "hooks" / "names_restored.py"
NAMES_MARKER = "names_restored.py"


class DeliverError(RuntimeError):
    """The settings file could not be changed, with a reason worth showing."""


def hook_command(port: int, python: str | Path | None = None, script: Path = HOOK_SCRIPT) -> str:
    """The command Claude Code runs. Forward slashes throughout: on Windows hook commands go through
    Git Bash, which would halve a Windows path's backslashes. The windowless pythonw.exe is fine
    here - the hook answers on stdout - but its console twin is used when present, as bashguard does."""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return f'"{exe.as_posix()}" "{script.as_posix()}" {int(port)}'


def _ours(group: object, marker: str = MARKER) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(marker in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def updated(doc: dict, on: bool, port: int, python: str | Path | None = None, event: str = "PostToolUse",
            marker: str = MARKER, script: Path = HOOK_SCRIPT) -> dict:
    """The settings document with our entry for `event` removed and, when on, added afresh."""
    out = dict(doc)
    hooks = dict(out.get("hooks") or {})
    groups = [g for g in (hooks.get(event) or []) if not _ours(g, marker)]
    if on:
        groups.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_command(port, python, script),
                                                  "timeout": 5}]})
    if groups:
        hooks[event] = groups
    else:
        hooks.pop(event, None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def apply_collapse_guard(on: bool, port: int, home: Path | None = None, python: str | Path | None = None) -> str:
    """The PreToolUse hook that stops a call whose spellings collapse to one real name, and its
    PostToolUse partner that says a name the model sees as a token was written correctly (D199) - for
    every Claude Code session on the machine, since the proxy sees them all. As `apply`."""
    before = apply(on, port, home, python, event="PreToolUse", marker=COLLAPSE_MARKER, script=COLLAPSE_SCRIPT,
                   said=("tool calls that would write two names as one are stopped before they run",
                         "the rename-collapse guard is off"))
    after = apply(on, port, home, python, event="PostToolUse", marker=NAMES_MARKER, script=NAMES_SCRIPT,
                  said=("Claude is told when a name it sees as a token was written correctly",
                        "Claude is no longer told when a name it sees as a token was written correctly"))
    return " ".join(s for s in (before, after) if s)


def apply(on: bool, port: int, home: Path | None = None, python: str | Path | None = None,
          event: str = "PostToolUse", marker: str = MARKER, script: Path = HOOK_SCRIPT,
          said: tuple[str, str] = ("waiting messages now reach Claude between tool calls",
                                   "waiting messages go at the end of the turn again")) -> str:
    """Make the user settings match the switch. Returns what was done (empty when nothing changed);
    raises DeliverError when the file cannot be read as settings or written."""
    target = user_settings(home)
    try:
        doc = json.loads(target.read_text(encoding="utf-8-sig")) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise DeliverError(f"{target} could not be read as JSON ({exc}); it was left as it is") from exc
    if not isinstance(doc, dict):
        raise DeliverError(f"{target} is not a settings object; it was left as it is")
    new = updated(doc, on, port, python, event, marker, script)
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
        raise DeliverError(f"{target} could not be written ({exc})") from exc
    done = said[0] if on else said[1]
    log.info("%s (%s)", done, target)
    return done
