"""SessionStart hook (feature 117): commands Claude Code runs connect directly, as they do today.

With the Code tab switch on, Claude Code's settings send its own traffic through aicleaner's
forward proxy (HTTPS_PROXY, NODE_EXTRA_CA_CERTS). The Bash and PowerShell tools read the file named
by CLAUDE_ENV_FILE before each command; this empties those variables there. Plain `export NAME=`
lines only: PowerShell takes the file's values only when every line is a plain assignment.
Standard library only, and it never fails a session: any problem, and it does nothing.
"""
import os
import sys

NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NODE_EXTRA_CA_CERTS")


def main() -> int:
    path = os.environ.get("CLAUDE_ENV_FILE", "")
    if not path:
        return 0
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as out:
            for name in NAMES:
                out.write(f"export {name}=\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdin.read()          # the hook's JSON, unused
    except (OSError, ValueError):
        pass
    sys.exit(main())
