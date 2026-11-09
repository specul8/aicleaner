"""Fast Copilot suggestions in the file editor through GitHub's Copilot language server (feature 167, AI Cleaner 1.1).

GitHub publishes `@github/copilot-language-server` as the way editors other than VS Code get Copilot's own inline
completions. Proved 2026-11-08: GitHub accepts it with its own sign-in (a device code), and one document edited as an
editor edits it answers each suggestion in about 250-330 ms - against 3.6 s through the Copilot CLI.

One server per AI Cleaner process, kept warm, spoken to over stdio (JSON-RPC with Content-Length framing). It is only
ever started with the environment `Proxy.prepare_agent` gives a filtered Copilot process - AI Cleaner's forward proxy
and its narrow Copilot certificate - so its traffic goes through the filter; without that it is not started at all.
Spec: specs/2026-11-08-f167-copilot-language-server.md (tooling repository).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from collections import OrderedDict
from pathlib import Path, PureWindowsPath
from typing import Any

log = logging.getLogger("aicleaner.copilotls")

PACKAGE = "@github/copilot-language-server"
VERSION = "1.566.0"
IDLE_S = 600
MAX_DOCUMENTS = 20
REQUEST_S = 15.0
EDITOR = {"editorInfo": {"name": "AI Cleaner", "version": "1.1"},
          "editorPluginInfo": {"name": "aicleaner-editor", "version": "1.1"}}


def folder(state_dir: Path) -> Path:
    return Path(state_dir) / "agents" / "copilot-ls"


def script(state_dir: Path) -> Path:
    return folder(state_dir) / "node_modules" / "@github" / "copilot-language-server" / "dist" / "language-server.js"


def installed(state_dir: Path) -> bool:
    return script(state_dir).exists()


def store_exists() -> bool:
    """The language server's own sign-in store (it keeps the GitHub token there)."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return (Path(base) / "github-copilot" / "auth.db").exists() or (Path.home() / ".config" / "github-copilot"
                                                                     / "auth.db").exists()


async def install(state_dir: Path) -> None:
    """npm install of the pinned version into AI Cleaner's agents folder. Raises RuntimeError with npm's words."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("Node.js (npm) is needed for the fast Copilot suggestions and was not found")
    where = folder(state_dir)
    where.mkdir(parents=True, exist_ok=True)
    if not (where / "package.json").exists():
        (where / "package.json").write_text('{"name": "aicleaner-copilot-ls", "private": true}\n', encoding="utf-8")
    child = await asyncio.create_subprocess_exec(npm, "install", "--no-audit", "--no-fund", f"{PACKAGE}@{VERSION}",
                                                 cwd=str(where), stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(child.communicate(), timeout=300)
    if child.returncode != 0 or not installed(state_dir):
        raise RuntimeError(f"npm could not install {PACKAGE}: {out.decode(errors='replace')[-400:]}")


def uri_for(path: str) -> str:
    """A file:/// URI for the editor tab's path (a stand-in one for an unsaved tab)."""
    text = str(path or "untitled").replace("\\", "/")
    if PureWindowsPath(str(path or "")).drive:
        return "file:///" + text
    return "file:///aicleaner/" + text.lstrip("/")


def remainder(insert_text: str, line_start_text: str) -> str:
    """What to put at the cursor: the suggestion replaces the line from the range's start to the cursor, so the part
    after what is already typed - or nothing, when it does not start with what is typed."""
    if not insert_text.startswith(line_start_text):
        return ""
    return insert_text[len(line_start_text):]


class LanguageServer:
    """One warm language server; `command` is the process to start (node and the script, or a stand-in in tests)."""

    def __init__(self, command: list[str], env: dict[str, str] | None = None, idle_s: float = IDLE_S) -> None:
        self.command = command
        self.env = env or {}
        self.idle_s = idle_s
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next = 0
        self._waiting: dict[int, asyncio.Future[dict]] = {}
        self._documents: OrderedDict[str, int] = OrderedDict()
        self._lock = asyncio.Lock()
        self._ticket = 0
        self.last_used = 0.0
        self.user = ""
        self.signed_in: bool | None = None
        self.last_ms = 0
        self.answered = 0
        self.last_error = ""
        self.signing_in: asyncio.Task[None] | None = None

    # ---- the process ----------------------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            return
        self._documents.clear()
        self.proc = await asyncio.create_subprocess_exec(*self.command, "--stdio", stdin=asyncio.subprocess.PIPE,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL,
                                                         env={**os.environ, **self.env})
        self._reader = asyncio.create_task(self._read())
        await self.request("initialize", {"processId": os.getpid(), "rootUri": None, "capabilities": {"workspace": {}},
                                          "initializationOptions": EDITOR})
        self.notify("initialized", {})
        await self.status()

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(self._call(proc, "shutdown", {}), timeout=3)
        except Exception:  # noqa: BLE001 - it is being stopped either way
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        if self._reader is not None:
            self._reader.cancel()

    async def stop_if_idle(self) -> bool:
        if self.proc is not None and time.monotonic() - self.last_used > self.idle_s:
            await self.close()
            return True
        return False

    async def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        out = self.proc.stdout
        try:
            while True:
                header = await out.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":")[1]) for line in header.split(b"\r\n")
                              if line.lower().startswith(b"content-length"))
                message = json.loads(await out.readexactly(length))
                if "id" in message and ("result" in message or "error" in message):
                    waiting = self._waiting.pop(message["id"], None)
                    if waiting is not None and not waiting.done():
                        waiting.set_result(message)
                elif "id" in message:                                    # the server asking: answered plainly
                    self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})
        except (asyncio.IncompleteReadError, asyncio.CancelledError, ConnectionError, StopIteration, ValueError):
            pass
        finally:
            for waiting in self._waiting.values():
                if not waiting.done():
                    waiting.set_exception(ConnectionError("the Copilot language server stopped"))
            self._waiting.clear()

    def _write(self, message: dict) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise ConnectionError("the Copilot language server is not running")
        body = json.dumps(message).encode("utf-8")
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    async def _call(self, proc: Any, method: str, params: dict) -> dict:
        self._next += 1
        mine = self._next
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._waiting[mine] = future
        self._write({"jsonrpc": "2.0", "id": mine, "method": method, "params": params})
        return await future

    async def request(self, method: str, params: dict, timeout: float = REQUEST_S) -> dict:
        answer = await asyncio.wait_for(self._call(self.proc, method, params), timeout=timeout)
        if "error" in answer:
            raise RuntimeError(f"{method}: {answer['error'].get('message', answer['error'])}")
        return answer.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ---- sign-in ---------------------------------------------------------------------------------------------------

    async def status(self) -> dict:
        result = await self.request("checkStatus", {})
        self.signed_in = result.get("status") in ("OK", "AlreadySignedIn", "MaybeOk")
        self.user = str(result.get("user") or "") if self.signed_in else ""
        return result

    async def sign_in(self) -> dict:
        """The device code and its link; the finishing step waits in the background for the person."""
        await self.start()
        result = await self.request("signIn", {})
        if result.get("status") in ("AlreadySignedIn", "OK"):
            await self.status()
            return {"signed_in": True, "user": self.user}
        command = result.get("command") or {}

        async def finish() -> None:
            try:
                await self.request("workspace/executeCommand", {"command": command.get("command"),
                                                                "arguments": command.get("arguments") or []},
                                   timeout=float(result.get("expiresIn") or 900))
            except Exception as exc:  # noqa: BLE001 - reported through the status
                self.last_error = f"sign-in did not finish: {exc}"
            await self.status()

        if command:
            self.signing_in = asyncio.create_task(finish())
        return {"signed_in": False, "code": result.get("userCode", ""), "url": result.get("verificationUri", ""),
                "expires_s": result.get("expiresIn", 0)}

    async def sign_out(self) -> None:
        await self.start()
        await self.request("signOut", {})
        self.signed_in, self.user = False, ""

    # ---- suggestions -----------------------------------------------------------------------------------------------

    def _document(self, uri: str, language: str, text: str) -> int:
        version = self._documents.pop(uri, 0) + 1
        if version == 1:
            self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": language or "plaintext",
                                                                  "version": 1, "text": text}})
            while len(self._documents) >= MAX_DOCUMENTS:
                old, _v = self._documents.popitem(last=False)
                self.notify("textDocument/didClose", {"textDocument": {"uri": old}})
        else:
            self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                                   "contentChanges": [{"text": text}]})
        self._documents[uri] = version
        return version

    async def complete(self, path: str, language: str, before: str, after: str) -> str:
        """The code to put at the cursor, or '' (nothing fits, or a newer request took its place)."""
        self._ticket += 1
        mine = self._ticket
        async with self._lock:
            if mine != self._ticket:
                return ""
            started = time.monotonic()
            self.last_used = started
            await self.start()
            uri = uri_for(path)
            version = self._document(uri, language, before + after)
            self.notify("textDocument/didFocus", {"textDocument": {"uri": uri}})
            line = before.count("\n")
            column = len(before) - (before.rfind("\n") + 1)
            result = await self.request("textDocument/inlineCompletion", {
                "textDocument": {"uri": uri, "version": version}, "position": {"line": line, "character": column},
                "context": {"triggerKind": 2}, "formattingOptions": {"tabSize": 4, "insertSpaces": True}})
            items = (result.get("items") or []) if isinstance(result, dict) else []
            self.last_ms = int((time.monotonic() - started) * 1000)
            if not items:
                return ""
            item = items[0]
            start = ((item.get("range") or {}).get("start") or {}).get("character", column)
            typed = before[before.rfind("\n") + 1:][int(start):]
            text = remainder(str(item.get("insertText") or ""), typed)
            if text:
                self.notify("textDocument/didShowCompletion", {"item": item})
                self.answered += 1
            return text

    def summary(self) -> dict[str, Any]:
        return {"running": self.proc is not None and self.proc.returncode is None, "signed_in": self.signed_in,
                "user": self.user, "answered": self.answered, "last_ms": self.last_ms, "last_error": self.last_error,
                "signing_in": self.signing_in is not None and not self.signing_in.done()}
