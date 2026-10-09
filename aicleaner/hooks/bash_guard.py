"""Claude Code PreToolUse hook: stop a Bash tool call whose command holds a doubled backslash
(feature 98).

On Windows, Claude Code's Bash tool halves every doubled backslash before Git Bash sees it - even
inside a quoted heredoc - so '\\n' written to a file becomes a real newline, silently. Write, Edit
and the PowerShell tool are byte-exact. Standard library only: Claude Code runs this with aicleaner's
own Python, and it must never need anything installed. Anything it cannot read is let through: a
broken guard must not stop all shell use.
"""
from __future__ import annotations

import json
import sys

DOUBLED = "\\" * 2

ADVICE = (
    "aicleaner blocked this Bash call: it contains a doubled backslash, and on Windows Claude Code's "
    "Bash tool halves doubled backslashes before the shell sees them (even in a quoted heredoc), so "
    "the command would run with different text from what you wrote. Use the PowerShell tool instead "
    "(Windows PowerShell 5.1 preferred, or 7), or the Write/Edit tools for file contents - all three "
    "keep backslashes exactly. A script holding backslashes: write it with Write, then run the file."
)


def verdict(payload: object) -> int:
    """2 to block (with ADVICE on stderr), 0 to let the call through."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return 2 if isinstance(command, str) and DOUBLED in command else 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except ValueError:
        return 0
    code = verdict(payload)
    if code:
        sys.stderr.write(ADVICE + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
