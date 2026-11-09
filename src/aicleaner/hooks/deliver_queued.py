"""PostToolUse hook (feature 119): a message waiting on the Session page reaches Claude between tool
calls, as VS Code hands one over, instead of at the end of the turn - to the main agent only, never a subagent (D223).

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


def from_subagent(payload: dict) -> bool:
    """True when the tool call was a subagent's (D223). A subagent's calls run this hook too, with the parent's
    session_id, so a message meant for the person's own agent went to whichever agent called a tool next - twice on
    2026-11-04 a background subagent took Brad's instructions. Claude Code marks a subagent's call with agent_id and
    agent_type (checked on 2.1.308 against real payloads, tests/data/d223); the main agent's has neither."""
    return bool(payload.get("agent_id") or payload.get("agent_type"))


def main(argv: list[str]) -> int:
    try:
        port = int(argv[1])
        payload = json.load(sys.stdin)
        # A subagent's tool call takes nothing: the message waits for the main agent's next one, or the turn's end.
        if not isinstance(payload, dict) or from_subagent(payload):
            return 0
        session_id = str(payload.get("session_id") or "")
        text = waiting_text(port, session_id) if session_id else ""
    except Exception:  # noqa: BLE001 - whatever goes wrong, the tool call carries on untouched
        return 0
    if text:
        # Also run after a tool call that failed (D233): Send now ends a foreground command's process, and the call
        # that fails with it opens the slot through PostToolUseFailure, which takes additionalContext the same way.
        event = "PostToolUseFailure" if payload.get("hook_event_name") == "PostToolUseFailure" else "PostToolUse"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
