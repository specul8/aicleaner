"""PostToolUse hook: tells the model that what it just wrote holds real client names, which it sees as
tokens (D199, 2026-10-21).

The model's context is filtered, so a file name it wrote with a client's name in it comes back to it as
the token. It then decided it had "invented a made-up form of the client's name" and renamed a correct
file. The proxy notes each tool call whose arguments it put real names into; this asks about the call
that just ran and, when names went in, adds a line the model reads straight after the result. Any
failure - proxy down, a slow answer - adds nothing. Standard library only.

    python names_restored.py <port>
"""
import json
import sys
import urllib.request

TIMEOUT_S = 3
SAID = ("aicleaner: this call was carried out with the real client names wherever you see tokens, so the "
        "file names, paths and text it wrote are correct on disk. Do not rename or rewrite anything to "
        "remove a token - leave it as written.")


def names_went_in(port: int, call_id: str) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tool/names",
        data=json.dumps({"tool_use_id": call_id}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # Straight to loopback: a proxy variable in the environment must not route this anywhere.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=TIMEOUT_S) as answer:
        return bool(json.loads(answer.read().decode("utf-8")).get("restored"))


def main(argv: list[str]) -> int:
    try:
        port = int(argv[1])
        call_id = str(json.load(sys.stdin).get("tool_use_id") or "")
        restored = names_went_in(port, call_id) if call_id else False
    except Exception:  # noqa: BLE001 - whatever goes wrong, nothing is added
        return 0
    if restored:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": SAID}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
