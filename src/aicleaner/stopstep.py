"""D233 (Brad, 2026-11-09; spec 2026-11-09-d233-send-now.md): Send now stops only the foreground command.

With background agents running, Claude Code's own interrupt is not an option - it ends the agents with the turn (D205,
probed again 2026-11-09). When the step Claude is waiting on is a Bash or PowerShell command, that command's process
tree is ended instead: the tool call fails, the slot between tool calls opens, and the waiting prompt is handed over
there. Nothing else is touched - not the CLI, not a background shell, not an agent.

Which process is the foreground command is worked out from what AI Cleaner already sees: the tool call's command text
(from the stream's tool_use) and the command texts of the shells started in the background (run_in_background), matched
against the command lines of the shells under the CLI. Quoting and escaping differ between what Claude wrote and what
the shell was started with, so both are compared reduced to their letters and digits; a PowerShell -EncodedCommand is
decoded first. When the match is not exactly one process tree - none, or more than one - nothing is stopped.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import Callable, Iterable

from . import proctree

log = logging.getLogger("aicleaner.stopstep")

# The shells Claude Code starts a Bash or PowerShell tool call in. Only these are candidates: an MCP server or the
# CLI's own helpers are never ended, whatever their command line says.
SHELL_NAMES = frozenset({"bash.exe", "sh.exe", "zsh.exe", "pwsh.exe", "powershell.exe", "cmd.exe",
                         "bash", "sh", "zsh", "pwsh"})
# A command shorter than this, reduced, is too likely to appear in any shell's command line (the CLI's own wrapper
# included) to pick one out by it.
MIN_LETTERS = 3

_ENCODED = re.compile(r"(?:^|\s)-(?:e|ec|enc|encodedcommand)\s+\"?([A-Za-z0-9+/=]{8,})", re.IGNORECASE)


def letters(text: str) -> str:
    """The text reduced to its letters and digits, lower case: quoting and escaping no longer matter."""
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())


def readable(command_line: str) -> str:
    """A command line with any PowerShell -EncodedCommand payload decoded beside it."""
    out = [command_line]
    for found in _ENCODED.finditer(command_line or ""):
        try:
            out.append(base64.b64decode(found.group(1), validate=True).decode("utf-16-le", errors="ignore"))
        except (binascii.Error, ValueError):
            continue
    return "\n".join(out)


def find(snap: proctree.Snapshot, cli_pid: int, command: str, background: Iterable[str],
         read: Callable[[int], str] = proctree.command_line) -> tuple[int, str]:
    """The pid of the one shell under `cli_pid` running `command`, or (0, why not).

    A shell whose command line a background command explains at least as well (as long or longer) is left out; of the
    shells that match, the top of each tree is what counts - a shell starting another with the same text is one tree."""
    target = letters(command)
    if len(target) < MIN_LETTERS:
        return 0, "the command is too short to tell its process apart"
    others = [b for b in (letters(c) for c in background) if b]
    matched = []
    for proc in proctree.descendants(snap, int(cli_pid)):
        if proc.pid == cli_pid or proc.name.lower() not in SHELL_NAMES:
            continue
        line = letters(readable(read(proc.pid)))
        if target not in line:
            continue
        if any(len(other) >= len(target) and other in line for other in others):
            continue
        matched.append(proc)
    ids = {proc.pid for proc in matched}
    tops = [proc for proc in matched if proc.ppid not in ids]
    if not tops:
        return 0, "no running shell under Claude Code matches the command"
    if len(tops) > 1:
        return 0, f"{len(tops)} running shells match the command, so none was stopped"
    return tops[0].pid, ""


def stop(cli_pid: int, command: str, background: Iterable[str],
         snapshot: Callable[[], proctree.Snapshot] | None = None,
         read: Callable[[int], str] = proctree.command_line,
         kill: Callable[[int], bool] = proctree.kill_tree) -> tuple[int, str]:
    """End the foreground command's process tree. Returns (pid ended, "") or (0, why nothing was ended). Blocking:
    called off the event loop."""
    if not cli_pid:
        return 0, "Claude Code's process is not known"
    snap = (snapshot or (lambda: proctree.snapshot(details_for=SHELL_NAMES | proctree.SESSION_KINDS)))()
    pid, why = find(snap, int(cli_pid), command, background, read)
    if not pid:
        log.info("Send now: the foreground command was not stopped (%s): %s", why, command[:200])
        return 0, why
    if not kill(pid):
        log.warning("Send now: Windows did not end process %s for the command: %s", pid, command[:200])
        return 0, "Windows did not end the command's process"
    log.info("Send now: ended process tree %s (the foreground command: %s)", pid, command[:200])
    return pid, ""
