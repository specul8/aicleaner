"""PostToolUse hook (feature 119): a message waiting on the Session page reaches Claude between tool
calls, as VS Code hands one over, instead of at the end of the turn.

Claude Code runs this after every tool call, for every session on the machine, and gives it the
session's id. It asks the local proxy whether that session - one the Session page drives - has
prompts waiting; the proxy takes them off its queue and answers with their text, which goes back as
additionalContext (read by the model on its next request, and filtered by the proxy like everything
else in it). Any other session, a proxy that is down, a slow answer: nothing is printed. Standard
library only, and it never fails or holds up a tool call.

    python deliver_queued.py <port>
"""
import json
import sys
import urllib.request

TIMEOUT_S = 3


def waiting_text(port: int, session_id: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/session/deliver",
        data=json.dumps({"session_id": session_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # Straight to loopback: a proxy variable in the environment must not route this anywhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=TIMEOUT_S) as answer:
        return str(json.loads(answer.read().decode("utf-8")).get("text") or "")


def main(argv: list[str]) -> int:
    try:
        port = int(argv[1])
        session_id = str(json.load(sys.stdin).get("session_id") or "")
        text = waiting_text(port, session_id) if session_id else ""
    except Exception:  # noqa: BLE001 - whatever goes wrong, the tool call carries on untouched
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
