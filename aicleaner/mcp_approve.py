"""The approval host: Claude Code asks it for permission, and it asks you.

Claude Code in headless mode has nobody at the keyboard to answer "may I run this?". With
`--permission-prompts host --permission-prompt-tool mcp__aicleaner__approve` it asks a tool instead,
and this is that tool: a small MCP server over stdio that hands the question to the proxy, which
shows it on the Session page and waits for Allow or Deny.

It is started by the session host, not by you:

    python -m aicleaner.mcp_approve       # AICLEANER_APPROVE_URL and _TOKEN in the environment

Everything is newline-delimited JSON-RPC on stdin and stdout, which is all MCP over stdio is, so
there is no dependency to install. Anything that goes wrong is a denial: a permission host that
fails open would be worse than useless.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
TOOL = "approve"
# No deadline on the call to the proxy: a question waits for as long as the person takes (the
# user, 2026-10-01). A proxy that goes away breaks the connection, which the retry below handles.
ASK_TIMEOUT_S = None


# How long a question waits for a proxy that is restarting (DECISIONS 49). The session lives on in
# its broker; the new proxy re-attaches it within seconds and knows its token again.
RESTART_WAIT_S = float(os.environ.get("AICLEANER_APPROVE_RESTART_WAIT_S", "120"))
RETRY_EVERY_S = 2


def _ask_once(url: str, token: str, body: bytes) -> dict:
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Aicleaner-Approve": token})
    with urllib.request.urlopen(request, timeout=ASK_TIMEOUT_S) as response:  # noqa: S310 (loopback)
        return json.loads(response.read() or b"{}")


def ask(url: str, token: str, payload: dict) -> dict:
    """Put the question to the proxy and wait for the answer. While the proxy is restarting - not
    answering, or answering but not yet re-attached to this session (403) - it asks again every
    few seconds, for up to RESTART_WAIT_S, instead of denying straight away.

    The allowance runs from the first failure, not from when the question was asked: a question
    that waited an hour for its answer and was then caught by a restart would otherwise be past its
    allowance already, and denied without a single retry."""
    body = json.dumps(payload).encode("utf-8")
    deadline = None
    while True:
        try:
            return _ask_once(url, token, body)
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 502, 503):
                raise
            failure: Exception = exc
        except (urllib.error.URLError, ConnectionError) as exc:
            failure = exc
        now = time.monotonic()
        if deadline is None:
            deadline = now + RESTART_WAIT_S
        elif now >= deadline:
            if isinstance(failure, urllib.error.HTTPError):
                raise failure
            raise urllib.error.URLError(getattr(failure, "reason", failure)) from failure
        time.sleep(RETRY_EVERY_S)


def decide(arguments: dict) -> dict:
    """The tool's answer, in the shape Claude Code expects."""
    url = os.environ.get("AICLEANER_APPROVE_URL", "")
    token = os.environ.get("AICLEANER_APPROVE_TOKEN", "")
    tool = str(arguments.get("tool_name") or arguments.get("tool") or "a tool")
    if not url:
        return {"behavior": "deny", "message": "aicleaner: no approval host configured"}
    try:
        answer = ask(url, token, {"tool": tool, "input": arguments.get("input") or {},
                                  "tool_use_id": arguments.get("tool_use_id") or ""})
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # The proxy no longer knows this session's token: it has restarted since the session
            # began, and the CLI carried on without it (D58's "Forbidden").
            return {"behavior": "deny", "message": "aicleaner: the proxy restarted since this session "
                    "started, so its questions cannot reach the Session page. Stop it and start the session again."}
        return {"behavior": "deny", "message": f"aicleaner: could not reach the approval page ({exc.reason})"}
    except urllib.error.URLError as exc:
        return {"behavior": "deny", "message": f"aicleaner: could not reach the approval page ({exc.reason})"}
    except (OSError, ValueError) as exc:
        return {"behavior": "deny", "message": f"aicleaner: approval failed ({exc})"}
    if answer.get("allow"):
        given = arguments.get("input") or {}
        # A question is answered, not approved (D58): the choices go back in `answers`, keyed by
        # question text, which is how Claude Code's permission protocol returns them. Anything
        # else is passed back unchanged - an approval, not an edit.
        if isinstance(answer.get("answers"), dict):
            return {"behavior": "allow", "updatedInput": {**given, "answers": answer["answers"]}}
        return {"behavior": "allow", "updatedInput": given}
    return {"behavior": "deny", "message": str(answer.get("message") or "aicleaner: denied on the Session page")}


def handle(message: dict) -> dict | None:
    """One JSON-RPC message in, at most one response out."""
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aicleaner", "version": "1"}}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{
            "name": TOOL,
            "description": "Ask the person watching the Session page whether this tool may run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "the tool asking"},
                    "input": {"type": "object", "description": "what it wants to run"},
                    "tool_use_id": {"type": "string"},
                },
                "required": ["tool_name"],
            }}]}}
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": f"no tool called {params.get('name')!r}"}}
        answer = decide(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(answer)}]}}
    if request_id is None:
        return None  # a notification we do not know: nothing to answer
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unknown method {method!r}"}}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
