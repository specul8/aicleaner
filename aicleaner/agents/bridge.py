"""The bridge: Claude Code's stream-json on stdin/stdout, another agent's protocol on the other side
(plan item K; spec 2026-10-20-agents-in-project-sessions.md).

Started by the Session in Claude Code's place, with the same arguments it would give `claude` - only
what matters here is read: --permission-mode, --mcp-config (for the approval host's address and token),
--resume=<id>, --model=<name>, --add-dir. The agent's own command comes in AICLEANER_AGENT_COMMAND (a JSON
list), and its traffic already goes through aicleaner: the Session gives this process HTTPS_PROXY and the
agent's certificate variable, and the agent inherits them.

Out, in Claude Code's shapes, so the page draws it as it draws Claude: `system/init` with the
conversation's id, a `stream_event` content_block_start when text, thinking or a tool call begins (the
status line), an `assistant` message when each is complete, a `user` message with the tool's result, and
`result` at the end of the turn. In: `user` messages become prompts; a control_request `interrupt`
cancels the turn; `set_permission_mode` is acknowledged. A permission the agent asks for goes to the
approval host exactly as Claude Code's MCP approval tool sends it, so it is a card on the page.

Every turn is also written as a Claude Code-shaped transcript under AICLEANER_AGENT_TRANSCRIPTS, so the
Continue list, replay and titles read it as they read Claude's.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

OUT_LOCK = threading.Lock()


def emit(obj: dict[str, Any]) -> None:
    with OUT_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--agent", default="copilot")
    p.add_argument("--permission-mode", default="manual")
    p.add_argument("--mcp-config", default="")
    p.add_argument("--resume", default="")
    p.add_argument("--model", default="")
    p.add_argument("--add-dir", action="append", default=[])
    known, _rest = p.parse_known_args(argv)
    return known


def approval_of(mcp_config: str) -> tuple[str, str]:
    """The approval host's URL and token, from the MCP config the Session gives Claude Code."""
    try:
        servers = (json.loads(mcp_config) if mcp_config else {}).get("mcpServers") or {}
        env = (servers.get("aicleaner") or {}).get("env") or {}
        return str(env.get("AICLEANER_APPROVE_URL", "")), str(env.get("AICLEANER_APPROVE_TOKEN", ""))
    except (ValueError, AttributeError):
        return "", ""


def ask_approval(url: str, token: str, tool: str, given: dict[str, Any], tool_use_id: str) -> dict[str, Any]:
    """The page's answer, asked as mcp_approve asks it: {allow, answers?, message?}."""
    if not url:
        return {"allow": False, "message": "aicleaner: no approval host configured"}
    request = urllib.request.Request(url, data=json.dumps({"tool": tool, "input": given, "tool_use_id": tool_use_id})
                                     .encode("utf-8"), headers={"Content-Type": "application/json",
                                                                "X-Aicleaner-Approve": token}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=7 * 24 * 3600) as reply:
            return json.loads(reply.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"allow": False, "message": f"aicleaner: could not reach the approval page ({exc})"}


class Transcript:
    """The conversation written as Claude Code writes one, so history.py reads it unchanged."""

    def __init__(self, root: str, cwd: str) -> None:
        self.root = Path(root) if root else None
        self.cwd = cwd
        self.path: Path | None = None

    def open(self, conv_id: str) -> None:
        if self.root is None:
            return
        from ..history import folder_for
        folder = self.root / folder_for(self.cwd)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"{conv_id}.jsonl"

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {**record, "cwd": self.cwd, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())}
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


class AcpBridge:
    """GitHub Copilot CLI over the Agent Client Protocol (`copilot --acp`), probed 2026-10-20."""

    def __init__(self, args: argparse.Namespace, command: list[str]) -> None:
        self.args = args
        self.cwd = os.getcwd()
        self.url, self.token = approval_of(args.mcp_config)
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
                                     cwd=self.cwd, bufsize=0)
        self.next_id = 0
        self.pending: dict[int, dict[str, Any]] = {}
        self.ready: dict[int, threading.Event] = {}
        self.session_id = ""
        self.transcript = Transcript(os.environ.get("AICLEANER_AGENT_TRANSCRIPTS", ""), self.cwd)
        self.turn_lock = threading.Lock()
        self.text: list[str] = []
        self.thought: list[str] = []
        self.tools: dict[str, dict[str, Any]] = {}
        self.loading = False

    # ---- JSON-RPC to the agent ---------------------------------------------------------------------

    def _send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        self.next_id += 1
        rid = self.next_id
        self.ready[rid] = threading.Event()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not self.ready[rid].wait(timeout):
            raise TimeoutError(f"{method} was not answered")
        self.ready.pop(rid, None)
        return self.pending.pop(rid, {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- what the agent says ------------------------------------------------------------------------

    def read_agent(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message and ("result" in message or "error" in message) and "method" not in message:
                rid = message["id"]
                self.pending[rid] = message
                if rid in self.ready:
                    self.ready[rid].set()
                continue
            method = message.get("method")
            if method == "session/update":
                self.on_update((message.get("params") or {}).get("update") or {})
            elif method == "session/request_permission" and "id" in message:
                threading.Thread(target=self.on_permission, args=(message,), daemon=True).start()
            elif "id" in message:
                # A request this bridge does not serve (files, terminals): said no to, never left hanging.
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not offered by aicleaner"}})
        # The agent went away: a turn in progress ends saying so, and the bridge goes with it.
        if self.turn_lock.locked():
            emit({"type": "result", "subtype": "error_during_execution", "session_id": self.session_id,
                  "result": "the agent stopped"})
        os._exit(0)

    def _flush_text(self) -> None:
        blocks = []
        if self.thought:
            blocks.append({"type": "thinking", "thinking": "".join(self.thought)})
        if self.text:
            blocks.append({"type": "text", "text": "".join(self.text)})
        self.thought, self.text = [], []
        if blocks:
            self._assistant(blocks)

    def _assistant(self, blocks: list[dict[str, Any]]) -> None:
        message = {"role": "assistant", "content": blocks}
        emit({"type": "assistant", "session_id": self.session_id, "message": message})
        if not self.loading:                 # a replayed turn is in the transcript already
            self.transcript.write({"type": "assistant", "sessionId": self.session_id, "message": message})

    def _start(self, block: dict[str, Any]) -> None:
        emit({"type": "stream_event", "session_id": self.session_id,
              "event": {"type": "content_block_start", "content_block": block}})

    def on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        content = update.get("content") or {}
        if kind == "user_message_chunk" and self.loading:
            text = content.get("text", "") if isinstance(content, dict) else ""
            if text:
                emit({"type": "user", "session_id": self.session_id, "message": {"role": "user", "content": text}})
            return
        if kind == "agent_message_chunk":
            if not self.text:
                self._start({"type": "text"})
            self.text.append(content.get("text", "") if isinstance(content, dict) else "")
        elif kind == "agent_thought_chunk":
            if not self.thought:
                self._start({"type": "thinking"})
            self.thought.append(content.get("text", "") if isinstance(content, dict) else "")
        elif kind == "tool_call":
            self._flush_text()
            tid = str(update.get("toolCallId") or uuid.uuid4().hex)
            name = str(update.get("title") or update.get("kind") or "tool")
            self.tools[tid] = {"name": name}
            self._start({"type": "tool_use", "name": name})
            self._assistant([{"type": "tool_use", "id": tid, "name": name, "input": update.get("rawInput") or {}}])
        elif kind == "tool_call_update":
            tid = str(update.get("toolCallId") or "")
            if update.get("status") in ("completed", "failed") and tid in self.tools:
                self.tools.pop(tid, None)
                text = _content_text(update.get("content"))
                message = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text,
                                                        "is_error": update.get("status") == "failed"}]}
                emit({"type": "user", "session_id": self.session_id, "message": message})
                self.transcript.write({"type": "user", "sessionId": self.session_id, "message": message})
        elif kind == "plan":
            entries = update.get("entries") or []
            lines = [f"- [{'x' if e.get('status') == 'completed' else ' '}] {e.get('content', '')}" for e in entries]
            if lines:
                self._flush_text()
                self._assistant([{"type": "text", "text": "Plan:\n" + "\n".join(lines)}])

    def on_permission(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        call = params.get("toolCall") or {}
        options = params.get("options") or []
        tool = str(call.get("title") or call.get("kind") or "a tool")
        answer = ask_approval(self.url, self.token, tool, call.get("rawInput") or {}, str(call.get("toolCallId") or ""))
        wanted = ("allow_once", "allow_always") if answer.get("allow") else ("reject_once", "reject_always")
        chosen = next((o for kind in wanted for o in options if o.get("kind") == kind), None)
        outcome = {"outcome": "selected", "optionId": chosen["optionId"]} if chosen else {"outcome": "cancelled"}
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}})

    # ---- what the Session says ----------------------------------------------------------------------

    def begin(self) -> None:
        threading.Thread(target=self.read_agent, daemon=True).start()
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "aicleaner", "version": "1"}}, timeout=60)
        servers: list[Any] = []
        if self.args.resume:
            self.session_id = self.args.resume
            self.loading = True
            emit({"type": "system", "subtype": "init", "session_id": self.session_id})
            reply = self.call("session/load", {"sessionId": self.session_id, "cwd": self.cwd, "mcpServers": servers},
                              timeout=120)
            self._flush_text()               # still loading: drawn, not written to the transcript again
            self.loading = False
            if "error" in reply:
                emit({"type": "result", "subtype": "error_during_execution", "session_id": self.session_id,
                      "result": f"could not continue that conversation: {reply['error'].get('message', '')}"})
        else:
            reply = self.call("session/new", {"cwd": self.cwd, "mcpServers": servers}, timeout=120)
            self.session_id = str((reply.get("result") or {}).get("sessionId") or uuid.uuid4())
            emit({"type": "system", "subtype": "init", "session_id": self.session_id})
        self.transcript.open(self.session_id)
        if self.args.model:
            self.call("session/set_config_option", {"sessionId": self.session_id, "configId": "model",
                                                   "value": self.args.model}, timeout=30)

    def prompt(self, text: str) -> None:
        with self.turn_lock:
            self.transcript.write({"type": "user", "sessionId": self.session_id,
                                   "message": {"role": "user", "content": text}})
            reply = self.call("session/prompt", {"sessionId": self.session_id,
                                                 "prompt": [{"type": "text", "text": text}]})
            self._flush_text()
            result = reply.get("result") or {}
            error = reply.get("error") or {}
            emit({"type": "result", "subtype": "error_during_execution" if error else "success",
                  "session_id": self.session_id, "stop_reason": result.get("stopReason", ""),
                  "result": error.get("message", "") if error else ""})

    def serve(self) -> None:
        self.begin()
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "control_request":
                subtype = (message.get("request") or {}).get("subtype")
                if subtype == "interrupt":
                    self.notify("session/cancel", {"sessionId": self.session_id})
                emit({"type": "control_response", "request_id": message.get("request_id"),
                      "response": {"subtype": "success", "request_id": message.get("request_id")}})
                continue
            if message.get("type") == "user":
                text = _user_text(message)
                if text:
                    threading.Thread(target=self.prompt, args=(text,), daemon=True).start()
        self.proc.terminate()


class CodexBridge(AcpBridge):
    """Codex over its app-server (`codex app-server`; its schema, generated by the CLI on 2026-10-20, is in
    tooling docs/agent-protocols/codex-app-server): initialize, thread/start or thread/resume, turn/start;
    item/started and item/completed carry the turn's messages, reasoning, commands and file changes;
    item/agentMessage/delta and item/reasoning/summaryTextDelta stream them; turn/completed ends it.
    Command and file approvals are requests to the client, answered accept or decline."""

    def __init__(self, args: argparse.Namespace, command: list[str]) -> None:
        super().__init__(args, command)
        self.turn_id = ""
        self.turn_done = threading.Event()
        self.turn_error = ""

    def read_agent(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message and ("result" in message or "error" in message) and "method" not in message:
                rid = message["id"]
                self.pending[rid] = message
                if rid in self.ready:
                    self.ready[rid].set()
                continue
            method = str(message.get("method") or "")
            params = message.get("params") or {}
            if "id" in message and method.endswith("requestApproval"):
                threading.Thread(target=self.on_approval, args=(message,), daemon=True).start()
            elif "id" in message:
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not offered by aicleaner"}})
            else:
                self.on_notice(method, params)
        if self.turn_lock.locked():
            emit({"type": "result", "subtype": "error_during_execution", "session_id": self.session_id,
                  "result": "Codex stopped"})
        os._exit(0)

    def on_notice(self, method: str, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if method == "turn/started":
            self.turn_id = str((params.get("turn") or {}).get("id") or self.turn_id)
        elif method == "item/agentMessage/delta":
            if not self.text:
                self._start({"type": "text"})
            self.text.append(str(params.get("delta", "")))
        elif method == "item/reasoning/summaryTextDelta":
            if not self.thought:
                self._start({"type": "thinking"})
            self.thought.append(str(params.get("delta", "")))
        elif method == "item/started" and kind in ("commandExecution", "fileChange", "mcpToolCall", "webSearch"):
            self._flush_text()
            name, given = _codex_tool(item)
            self.tools[str(item.get("id"))] = {"name": name}
            self._start({"type": "tool_use", "name": name})
            self._assistant([{"type": "tool_use", "id": str(item.get("id")), "name": name, "input": given}])
        elif method == "item/completed":
            iid = str(item.get("id") or "")
            if kind == "agentMessage" and not self.text and item.get("text"):
                self.text.append(str(item["text"]))         # a message that came whole, without deltas
            elif kind == "reasoning" and not self.thought:
                summary = item.get("summary") or []
                if summary:
                    self.thought.append("\n".join(str(s) for s in summary))
            elif iid in self.tools:
                self.tools.pop(iid, None)
                failed = item.get("status") in ("failed", "declined")
                body = str(item.get("aggregatedOutput") or item.get("result") or item.get("error") or item.get("status") or "")
                message = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": iid, "content": body,
                                                        "is_error": failed}]}
                emit({"type": "user", "session_id": self.session_id, "message": message})
                self.transcript.write({"type": "user", "sessionId": self.session_id, "message": message})
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            self.turn_error = str((turn.get("error") or {}).get("message") or "") if isinstance(turn.get("error"), dict) else ""
            self.turn_done.set()
        elif method == "error":
            self.turn_error = str((params.get("error") or {}).get("message") or params.get("message") or "error")

    def on_approval(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        if "commandExecution" in str(message.get("method")):
            tool, given = "Bash", {"command": params.get("command") or "", "cwd": params.get("cwd") or ""}
        else:
            tool, given = "Edit", {"reason": params.get("reason") or "", "grantRoot": params.get("grantRoot") or ""}
        answer = ask_approval(self.url, self.token, tool, given, str(params.get("itemId") or ""))
        self._send({"jsonrpc": "2.0", "id": message["id"],
                    "result": {"decision": "accept" if answer.get("allow") else "decline"}})

    def begin(self) -> None:
        threading.Thread(target=self.read_agent, daemon=True).start()
        self.call("initialize", {"clientInfo": {"name": "aicleaner", "title": "aicleaner", "version": "1"}}, timeout=60)
        self.notify("initialized", {})
        settings: dict[str, Any] = {"cwd": self.cwd, "approvalPolicy": "on-request"}
        if self.args.model:
            settings["model"] = self.args.model
        if self.args.resume:
            self.session_id = self.args.resume
            emit({"type": "system", "subtype": "init", "session_id": self.session_id})
            reply = self.call("thread/resume", {"threadId": self.session_id, **settings}, timeout=120)
            for turn in ((reply.get("result") or {}).get("thread") or {}).get("turns") or []:
                for item in turn.get("items") or []:
                    if item.get("type") == "userMessage":
                        text = "\n".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict))
                        emit({"type": "user", "session_id": self.session_id, "message": {"role": "user", "content": text}})
                    elif item.get("type") == "agentMessage" and item.get("text"):
                        emit({"type": "assistant", "session_id": self.session_id,
                              "message": {"role": "assistant", "content": [{"type": "text", "text": item["text"]}]}})
            if "error" in reply:
                emit({"type": "result", "subtype": "error_during_execution", "session_id": self.session_id,
                      "result": f"could not continue that conversation: {reply['error'].get('message', '')}"})
        else:
            reply = self.call("thread/start", settings, timeout=120)
            thread = (reply.get("result") or {}).get("thread") or {}
            self.session_id = str(thread.get("id") or uuid.uuid4())
            emit({"type": "system", "subtype": "init", "session_id": self.session_id})
        self.transcript.open(self.session_id)

    def prompt(self, text: str) -> None:
        with self.turn_lock:
            self.transcript.write({"type": "user", "sessionId": self.session_id,
                                   "message": {"role": "user", "content": text}})
            self.turn_done.clear()
            self.turn_error = ""
            reply = self.call("turn/start", {"threadId": self.session_id, "input": [{"type": "text", "text": text}]},
                              timeout=120)
            self.turn_id = str(((reply.get("result") or {}).get("turn") or {}).get("id") or self.turn_id)
            error = (reply.get("error") or {}).get("message", "")
            if not error:
                self.turn_done.wait()
            self._flush_text()
            error = error or self.turn_error
            emit({"type": "result", "subtype": "error_during_execution" if error else "success",
                  "session_id": self.session_id, "result": error})

    def serve(self) -> None:
        self.begin()
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "control_request":
                if (message.get("request") or {}).get("subtype") == "interrupt" and self.turn_id:
                    self._send({"jsonrpc": "2.0", "id": f"interrupt-{uuid.uuid4().hex[:8]}", "method": "turn/interrupt",
                                "params": {"threadId": self.session_id, "turnId": self.turn_id}})
                emit({"type": "control_response", "request_id": message.get("request_id"),
                      "response": {"subtype": "success", "request_id": message.get("request_id")}})
                continue
            if message.get("type") == "user":
                text = _user_text(message)
                if text:
                    threading.Thread(target=self.prompt, args=(text,), daemon=True).start()
        self.proc.terminate()


def _codex_tool(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A Codex item drawn as the tool call it is: a command as Bash, a file change as Edit, MCP as itself."""
    kind = item.get("type")
    if kind == "commandExecution":
        return "Bash", {"command": item.get("command") or "", "cwd": item.get("cwd") or ""}
    if kind == "fileChange":
        paths = [c.get("path", "") for c in item.get("changes") or [] if isinstance(c, dict)]
        return "Edit", {"file_path": ", ".join(p for p in paths if p) or "files"}
    if kind == "mcpToolCall":
        return f"mcp__{item.get('server', 'mcp')}__{item.get('tool', 'tool')}", item.get("arguments") or {}
    return "WebSearch", {"query": item.get("query") or ""}


def _user_text(message: dict[str, Any]) -> str:
    content = (message.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _content_text(content: Any) -> str:
    """A tool result's content as text: ACP content blocks, diffs and terminals flattened."""
    out = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        inner = item.get("content") if isinstance(item.get("content"), dict) else item
        if inner.get("type") == "text":
            out.append(str(inner.get("text", "")))
        elif item.get("type") == "diff":
            out.append(f"(changed {item.get('path', 'a file')})")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv if argv is not None else sys.argv[1:])
    command = json.loads(os.environ.get("AICLEANER_AGENT_COMMAND") or "[]")
    if not command:
        emit({"type": "result", "subtype": "error_during_execution", "result": "no agent command given"})
        return 2
    if args.agent == "copilot":
        AcpBridge(args, command).serve()
        return 0
    if args.agent == "codex":
        CodexBridge(args, command).serve()
        return 0
    emit({"type": "result", "subtype": "error_during_execution", "result": f"{args.agent} is not supported yet"})
    return 2


if __name__ == "__main__":
    sys.exit(main())
