"""PreToolUse hook: stops a tool call in which two different spellings would be written as the same
client name (the rename collapse, bug report 2026-10-14).

The model wrote a "rename" whose old and new names differed by a misspelling; both came back from
the proxy as the client's one real name, so writing the new file overwrote the old one and deleting
the old one deleted the only copy. The proxy notices while it restores the call's arguments and
holds the call's id; this asks about every call before it runs and, for a flagged one, denies it with
the proxy's reason, which the model reads and can act on. Any failure - proxy down, a slow answer -
lets the call run as it always did. Standard library only.

    python check_collapse.py <port>
"""
import json
import sys
import urllib.request

TIMEOUT_S = 3


def reason_to_stop(port: int, call_id: str) -> str:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tool/check",
        data=json.dumps({"tool_use_id": call_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # Straight to loopback: a proxy variable in the environment must not route this anywhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=TIMEOUT_S) as answer:
        said = json.loads(answer.read().decode("utf-8"))
    return str(said.get("reason") or "") if said.get("deny") else ""


def main(argv: list[str]) -> int:
    try:
        port = int(argv[1])
        call_id = str(json.load(sys.stdin).get("tool_use_id") or "")
        reason = reason_to_stop(port, call_id) if call_id else ""
    except Exception:  # noqa: BLE001 - whatever goes wrong, the call runs as it always did
        return 0
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
