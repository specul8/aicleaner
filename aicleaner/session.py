"""Claude Code sessions, driven from the UI instead of a terminal.

Claude Code's own remote control relays the session through Anthropic's servers, which is what this
project exists to avoid. This runs the same Claude Code on this machine, as a child process in
headless streaming mode, and turns its output into events the UI can show. The child is started
with ANTHROPIC_BASE_URL pointing back at the proxy, so a session driven from a phone is filtered
exactly like one driven from the terminal: prompts are tokenised on the way out and the reply is
rehydrated on the way back.

    claude --print --output-format stream-json --input-format stream-json ...

Sessions are named after the engagement, not the folder, and several run at once: SessionHost
holds one Session per profile, each with its own process, transcript, permission queue and
approval token. A session's traffic goes to its own address on the proxy, so the evidence log can
say which engagement a request belonged to. Filtering is not affected: every client's forms are
replaced in every session.

When an approval host is configured (mcp_approve.py) every permission prompt appears on the
Session page and waits for Allow or Deny; without one, anything that would prompt is denied, so a
session driven this way can read and plan but cannot quietly edit.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import secrets as secrets_module
import shutil
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Any, Awaitable, Callable

from .fileio import network_path

log = logging.getLogger("aicleaner.session")

# How long one line of Claude Code's output may be before the pipe's buffer gives up (DECISIONS 47).
# asyncio's default is 64 KiB, and after an Edit Claude Code prints the whole original file on one
# line: the reader stopped at the first large file edited, nobody emptied the pipe, and the session
# froze while the page still said Connected. A line past even this is read in pieces, not refused.
STREAM_LINE_LIMIT = 64 * 1024 * 1024


async def _read_line(stream: asyncio.StreamReader) -> bytes:
    """One line, however long. readline() raises past the stream's limit and leaves the reader
    stuck; this reads the rest of the line in pieces and joins it."""
    parts: list[bytes] = []
    while True:
        try:
            parts.append(await stream.readuntil(b"\n"))
            return b"".join(parts)
        except asyncio.IncompleteReadError as exc:          # end of output, no final newline
            parts.append(exc.partial)
            return b"".join(parts)
        except asyncio.LimitOverrunError as exc:           # longer than the buffer: take what is there
            parts.append(await stream.readexactly(exc.consumed))


# ---- a session held by a broker (DECISIONS 49) ------------------------------------------------
#
# The proxy used to own Claude Code's pipes, so a proxy restart orphaned it and Start made a second
# one. A broker (broker.py) now holds the process, detached from the proxy; this is the proxy's side
# of it, shaped like the asyncio Process Session always used - stdin, stdout, stderr, wait, kill -
# so the session's own logic is unchanged whether it started the broker or found it after a restart.

BROKER_START_S = 15
APPROVE_MCP_TIMEOUT_MS = 12 * 60 * 60 * 1000   # how long Claude Code waits on a permission or question: 12 hours


class _BrokerStdin:
    def __init__(self, child: "BrokerChild") -> None:
        self._child = child
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer += data

    async def drain(self) -> None:
        if self._buffer:
            data, self._buffer = bytes(self._buffer), bytearray()
            await self._child.post("/in", data)

    def close(self) -> None:
        asyncio.ensure_future(self._child.post("/eof"))


# Fed into a re-attached session's output once the broker has replayed everything it kept, so the
# session knows the rest is live (D179): a turn that ended during the replay is history, one that
# ends after it is now.
REPLAY_DONE = '{"type":"aicleaner_replay_done"}'


class BrokerChild:
    """Claude Code as seen through its broker. Lines the broker has kept are replayed first, then
    new ones follow; `on_input` hears the prompts that were sent before this proxy started, so a
    re-attached transcript shows them."""

    def __init__(self, folder: Path, published: dict[str, Any]) -> None:
        self.folder = folder
        self.url = f"http://127.0.0.1:{published['port']}"
        self.token = published["token"]
        self.pid = published.get("cli_pid")
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
        self.stderr = asyncio.StreamReader(limit=STREAM_LINE_LIMIT)
        self.stdin = _BrokerStdin(self)
        self.times: deque[float] = deque()          # when each stdout line was seen, in order
        self.on_input: Callable[[str, float], None] | None = None
        self.replay_upto = 0
        self._exited = asyncio.Event()
        self._http: Any = None
        self._pump: asyncio.Task | None = None
        self._detached = False
        self._replay_marked = False

    def _headers(self) -> dict[str, str]:
        from .broker import TOKEN_HEADER
        return {TOKEN_HEADER: self.token}

    async def connect(self) -> None:
        import aiohttp

        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        async with self._http.get(f"{self.url}/state", headers=self._headers()) as r:
            if r.status != 200:
                await self._http.close()
                raise SessionError(f"the session broker refused this proxy ({r.status})")
            self.replay_upto = int((await r.json()).get("seq", 0))
        if self.replay_upto == 0:
            self._mark_replay_done()          # nothing to replay: everything from here is live
        self._pump = asyncio.ensure_future(self._read())

    def _mark_replay_done(self) -> None:
        if not self._replay_marked:
            self._replay_marked = True
            self.stdout.feed_data((REPLAY_DONE + "\n").encode("utf-8"))

    async def _read(self) -> None:
        buffer = b""
        try:
            async with self._http.get(f"{self.url}/out?after=0", headers=self._headers()) as r:
                async for chunk in r.content.iter_chunked(65536):
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        if self._take(raw):
                            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broker that has gone ends the session, never the proxy
            log.warning("session broker link lost: %s", exc)
        finally:
            if not self._detached:
                if self.returncode is None:
                    self.returncode = -1
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self._exited.set()
                # The process is gone and nothing more will be read or sent: let go of the link to its
                # broker. Only detach() closed it, so every session that ended left an unclosed client
                # session behind for the garbage collector to complain about (2026-10-17).
                http, self._http = self._http, None
                if http is not None:
                    await http.close()

    def _take(self, raw: bytes) -> bool:
        """One record from the broker; True when it says Claude Code has exited."""
        if not raw.strip():
            return False
        record = json.loads(raw)
        if "exit" in record:
            self.returncode = int(record["exit"])
            return True
        stream, line, at = record.get("s"), str(record.get("line", "")), float(record.get("t") or 0)
        if stream == "out":
            self.times.append(at)
            self.stdout.feed_data((line + "\n").encode("utf-8"))
        elif stream == "err":
            self.stderr.feed_data((line + "\n").encode("utf-8"))
        elif stream == "in" and self.on_input is not None and int(record.get("seq", 0)) <= self.replay_upto:
            self.on_input(line, at)
        if int(record.get("seq", 0)) >= self.replay_upto:
            self._mark_replay_done()
        return False

    async def post(self, path: str, data: bytes = b"") -> None:
        if self._http is None:
            raise SessionError("the session broker is not connected")
        async with self._http.post(f"{self.url}{path}", data=data, headers=self._headers()) as r:
            if r.status >= 400:
                raise SessionError(f"the session broker refused {path} ({r.status})")

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else -1

    def kill(self) -> None:
        asyncio.ensure_future(self.post("/kill"))

    async def detach(self) -> None:
        """Let go without ending it: the proxy is going, the session is not."""
        self._detached = True
        if self._pump is not None:
            self._pump.cancel()
        if self._http is not None:
            await self._http.close()


def spawn_broker(folder: Path) -> None:
    """Start a broker for the session in `folder`, detached from this process so a proxy restart
    leaves it running (a new process group, and out of any job that closes with the proxy)."""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    command = [sys.executable, "-m", "aicleaner.broker", str(folder)]
    errors = (folder / "broker-stderr.log").open("ab")
    kwargs: dict[str, Any] = {"cwd": str(root), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                              "stderr": errors, "close_fds": True}
    try:
        if sys.platform == "win32":
            base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                subprocess.Popen(command, creationflags=base | 0x01000000, **kwargs)   # BREAKAWAY_FROM_JOB
            except OSError:                   # a job that forbids breakaway refuses the first attempt
                subprocess.Popen(command, creationflags=base, **kwargs)
        else:
            subprocess.Popen(command, start_new_session=True, **kwargs)
    finally:
        errors.close()


async def published_by(folder: Path, timeout: float = BROKER_START_S) -> dict[str, Any]:
    """The broker's broker.json once it is listening, or SessionError after `timeout`."""
    deadline = time.monotonic() + timeout
    path = folder / "broker.json"
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            await asyncio.sleep(0.1)
    detail = ""
    try:
        detail = (folder / "broker-stderr.log").read_text(encoding="utf-8", errors="replace")[-400:]
    except OSError:
        pass
    raise SessionError(f"the session broker did not start within {int(timeout)} s. {detail}".strip())


# ---- what a file tab can show (feature 54, DECISIONS 50) ----------------------------------------

MAX_TAB_TEXT = 2 * 1024 * 1024          # text beyond this is offered as a download, not shown
MAX_FILE_SEND = 200 * 1024 * 1024       # the most a download or an inline image/PDF will send

_KINDS = {
    "markdown": {".md", ".markdown", ".mdx"},
    "code": {".py", ".ps1", ".psm1", ".psd1", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go",
             ".rs", ".c", ".h", ".cpp", ".sh", ".bat", ".cmd", ".sql", ".kql", ".tf", ".hcl", ".bicep",
             ".css", ".scss", ".html", ".htm", ".vue", ".rb", ".php"},
    "config": {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".env",
               ".properties", ".csproj", ".sln", ".tfvars", ".editorconfig", ".gitignore"},
    "text": {".txt", ".log", ".csv", ".tsv", ".rst", ""},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"},
    "pdf": {".pdf"},
    "office": {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".vsdx", ".msg", ".one"},
}
# Images a browser shows by itself. SVG is left out on purpose: it can carry script.
_INLINE_IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                  ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon"}


# Searching the Files tree (feature 90): enough results to scroll, and an answer within seconds.
SEARCH_LIMIT = 500
SEARCH_BUDGET_S = 30.0          # the user, 2026-10-08 (was 10)
# Folders that hold installed dependencies or tools, not work - "no value searching a repo folder
# containing Node modules or the node app" (the user, 2026-10-07) - and git's own database.
DEPENDENCY_FOLDERS = frozenset({"node_modules", "bower_components", "jspm_packages", "site-packages",
                                "__pycache__", ".terraform", ".git",
                                # Tool caches and test leftovers (2026-10-16): generated, never work, and
                                # one test temp folder held a deep enough tree to use up a search's time.
                                ".pytest_cache", ".pytest-tmp", ".mypy_cache", ".ruff_cache", ".tox", ".nox"})


class SearchWalk:
    """One name search over the tree's folders, which can stop and carry on (feature 90, D146).

    It stops at a number of results ("limit": narrow the search) or when its time is up ("time":
    the folders not yet walked are still here, and `run` again picks up where it stopped - the
    page's "Keep searching").

    **Every root's shallow folders first** (2026-10-16): one queue across all the roots, breadth
    first, so the top of every folder is searched before the depths of any. It used to walk one root
    to the bottom before starting the next, and a file at the top of the OneDrive share was never
    reached in 30 seconds because a repository listed before it held a deep tree of test leftovers.
    """

    def __init__(self, needle: str, roots: list[Path]):
        self.needle = needle
        self.roots = list(roots)
        self.pending: deque[tuple[Path, Path]] = deque((root, root) for root in self.roots)
        self.skipped = 0
        self.found = 0

    @property
    def done(self) -> bool:
        return not self.pending

    def run(self, limit: int, budget_s: float) -> tuple[list[dict[str, Any]], str]:
        """(results found this time, why it stopped: "limit", "time", or "" when every folder was walked)."""
        results: list[dict[str, Any]] = []
        deadline = time.monotonic() + budget_s
        while True:
            if not self.pending:
                return results, ""
            root, folder = self.pending.popleft()
            try:
                with os.scandir(folder) as listed:
                    items = sorted(listed, key=lambda e: e.name.lower())
            except OSError:
                self.skipped += 1
                continue
            for item in items:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if self.needle in item.name.lower():
                    relative = Path(item.path).relative_to(root).as_posix()
                    results.append({"root": str(root), "path": relative, "name": item.name, "dir": is_dir,
                                    **({} if is_dir else {"view": file_kind(item.name)[1]})})
                # Never into a link or a junction (D171): where it leads is searched as its own root,
                # or not at all; walking through it found the same files twice, or left the folders.
                if is_dir and not item.is_symlink() and not item.is_junction():
                    if _dependency_folder(Path(item.path)):
                        self.skipped += 1
                    else:
                        self.pending.append((root, Path(item.path)))
            if len(results) >= limit:
                return results[:limit], "limit"
            if time.monotonic() > deadline:
                return results, "time"

    def answer(self, query: str, ran: tuple[list[dict[str, Any]], str], continued: bool) -> dict[str, Any]:
        results, stopped = ran
        if stopped == "time" and self.done:
            stopped = ""                 # out of time on the very last folder: nothing was left
        self.found += len(results)
        return {"query": query, "results": results, "truncated": bool(stopped), "stopped": stopped,
                "more": stopped == "time" and not self.done, "continued": continued, "skipped": self.skipped,
                "found": self.found}


def _dependency_folder(folder: Path) -> bool:
    """A folder of installed dependencies: one of DEPENDENCY_FOLDERS by name, or a Python virtual
    environment, known by its pyvenv.cfg whatever it is called."""
    if folder.name.lower() in DEPENDENCY_FOLDERS:
        return True
    try:
        return (folder / "pyvenv.cfg").is_file()
    except OSError:
        return False


def file_kind(name: str) -> tuple[str, str]:
    """(kind, view) for a file name. `view` is text, image, pdf or download: what its tab does.
    Download-only covers Office files, archives and anything else binary. SVG and HTML open as
    their source text, to read and edit (the user, 2026-10-12): they can carry script, so they are
    never rendered - inline_type refuses them, and /file/raw with it."""
    suffix = Path(name).suffix.lower()
    if not suffix and name.startswith("."):
        suffix = name.lower()                           # .gitignore, .env
    kind = next((k for k, suffixes in _KINDS.items() if suffix in suffixes), "other")
    if suffix in (".html", ".htm", ".svg"):
        return kind, "text"
    if kind in ("markdown", "code", "config", "text"):
        return kind, "text"
    if suffix in _INLINE_IMAGES:
        return kind, "image"
    if kind == "pdf":
        return kind, "pdf"
    return kind, "download"


def inline_type(name: str) -> str:
    """The Content-Type for showing a file in the page, or "" when it must not be shown inline."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    return _INLINE_IMAGES.get(suffix, "")


MAX_EVENTS = 500          # what the page can scroll back through
EARLIER_LIMIT = 500       # what "Show earlier" adds, once: never more than twice the buffer (feature 82)

# What the buffer keeps: the events the page draws or reads back. Everything is broadcast to
# watchers, because that is what the activity indicator reads (feature 46), but only these are
# KEPT in a buffer whose comment reads "what the page can scroll back through".
#
# A keep-list, since D109. D29 dropped the streaming fragments by name and kept the rest; then
# --thinking-display sent a `thinking_tokens` count about ten times per reply, as `type: system`,
# and 344 of the last 500 kept events were those - 53 were replies. A new event kind from the CLI
# is now not kept until someone decides it should be: the direction that cannot shrink the history
# without anybody noticing. `user` carries the tool results the background panel reads, and the
# system subtypes listed are the ones it joins on (`finishedIds`); `result` bounds a turn.
KEPT_EVENTS = frozenset({
    "prompt", "assistant", "user", "brief", "result", "text",
    "permission", "permission_timeout", "permission_answered", "permission_mode",
    "interrupt_pending", "interrupted", "interrupt_refused", "exited", "stopped",
})
KEPT_SYSTEM = frozenset({
    "init", "task_started", "task_notification", "task_updated", "background_tasks_changed",
    "compact_boundary",          # drawn in the transcript where the conversation was compacted (E5.1)
})


TASK_ID_MARK = re.compile(r"tool[-_]use[-_]id", re.IGNORECASE)


def slim(event: dict[str, Any]) -> dict[str, Any]:
    """The copy of an event the buffer keeps (D111). A tool result is kept for one reason - the
    background panel looks in it for a finished job's tool-use id (`finishedIds`) - but it arrived
    whole, with a second full copy in `tool_use_result`: 147 of them were 6.3 MB of one session's
    7.2, and the page fetched every session's buffer every five seconds (17 MB a poll). Kept now:
    text blocks, and a tool result's content only where it names a tool-use id. Watchers still get
    the event whole."""
    if event.get("type") != "user":
        return event
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    blocks = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            content = block.get("content")
            keep = content if isinstance(content, str) and TASK_ID_MARK.search(content) else ""
            blocks.append({"type": "tool_result", "tool_use_id": block.get("tool_use_id", ""), "content": keep})
        else:
            blocks.append(block)
    out = {k: v for k, v in event.items() if k != "tool_use_result"}
    out["message"] = {**message, "content": blocks}
    return out


def _same_folder(a: str | Path, b: str | Path) -> bool:
    """One folder however it was written: case and a trailing separator aside (Windows)."""
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    return norm(a) == norm(b)


def kept(event: dict[str, Any]) -> bool:
    """Whether an event belongs in the history the page scrolls back through (KEPT_EVENTS)."""
    kind = event.get("type")
    if kind == "system":
        return event.get("subtype") in KEPT_SYSTEM
    return kind in KEPT_EVENTS


COMPACTING = "compacting the conversation..."


def doing_after(doing: str, event: dict[str, Any]) -> str:
    """What Claude is doing once this event has happened, worded as the page's stream does it.

    A block starting says what kind; only a turn ending (or the session stopping) clears it, since a
    delta says nothing about whether more is coming. The tool's name only, never its input: an
    input is a command line or a path, the sort of string that carries a client name.
    """
    # Compacting (E5.1, Brad 2026-10-19: "I have no idea when you are compacting"): the CLI says when it
    # starts (status "compacting") and when it ends (status None, then compact_boundary) - a minute or
    # two in which nothing else arrives.
    if event.get("type") == "system":
        if event.get("subtype") == "status":
            if event.get("status") == "compacting":
                return COMPACTING
            return "" if doing == COMPACTING else doing
        if event.get("subtype") == "compact_boundary":
            return ""
    # The CLI wraps the API's events in `stream_event` (--include-partial-messages).
    inner = event.get("event") if isinstance(event.get("event"), dict) else event
    kind = inner.get("type")
    if kind == "content_block_start":
        block = inner.get("content_block") or {}
        if block.get("type") == "thinking":
            return "thinking..."
        if block.get("type") == "tool_use":
            return f"{block.get('name') or 'a tool'}..."
        if block.get("type") == "text":
            return "writing..."
        return doing
    if kind in ("result", "message_stop") or event.get("type") in ("assistant", "exited", "stopped"):
        return ""
    return doing


START_TIMEOUT_S = 30      # how long to wait for the child to announce itself
INTERRUPT_TIMEOUT = 5     # a control request is answered at once or not at all (feature 48)
QUESTION_WAIT_MS = 7 * 24 * 3600 * 1000   # Claude Code's MCP tool limit for a waiting question

# What `claude --permission-mode` accepts, read off its own --help rather than guessed. A value it
# rejects fails at launch, and a session that will not start is a worse way to learn about a typo
# than a message naming the six. "manual" asks about everything; "auto" sends each action to
# Anthropic to be judged; the rest sit between.
PERMISSION_MODES = ("manual", "auto", "acceptEdits", "dontAsk", "bypassPermissions", "plan")


class SessionError(RuntimeError):
    """The session could not be started, or is not running."""


class StaleWrite(SessionError):
    """A save refused because the file changed on disk after the tab read it (DECISIONS 53).
    `modified` is the file's time now: sending it back with the same text means "overwrite"."""

    def __init__(self, modified: float):
        super().__init__("the file changed on disk since it was opened")
        self.modified = modified


class ControlPending(SessionError):
    """A control request was written but not yet answered. It has not failed: it is in the
    CLI's input and will be acted on when the CLI next reads it."""


def _inside(path: Path, folder: Path) -> bool:
    """True when path is folder, or sits under it.

    Not a string prefix: /srv/project-old must not pass as inside /srv/project.
    """
    try:
        path.relative_to(folder)
    except ValueError:
        return False
    return True


def is_link(path: Path) -> bool:
    """A symbolic link or a Windows junction. Python's is_symlink() says False for a junction, so
    one passed as an ordinary folder (D171: a tooling repo's `repos\\` of junctions to its siblings)."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return False



# What --resume and --model may be given. The CLI's --resume takes an optional value, so a "session
# id" of "--dangerously-skip-permissions" was read as that flag, and "--settings=..." or
# "--mcp-config=..." ran commands at start: all from one field of POST /api/session/start (H1).
SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,99}$")


def check_resume(resume: str) -> None:
    if resume and not SESSION_ID.fullmatch(resume):
        raise SessionError("resume takes a session id (a UUID), nothing else")


def check_model(model: str) -> None:
    if model and not MODEL_NAME.fullmatch(model):
        raise SessionError("a model name is letters, digits and . _ : - [ ] only, and cannot start with -")


@dataclass
class Profile:
    """A named way of working: which client, which folders, which model, what standing brief.

    `client` is a client id (CLIENT05) because that is what the mapping is keyed by, and `label`
    is what a person reads (ACME). Every interface a human sees uses the label; the id is
    plumbing for the filter and the file name.
    """

    name: str
    client: str = ""
    label: str = ""
    projects: list[Path] = field(default_factory=list)
    model: str = ""
    brief: str = ""
    # The client's OneDrive share (shares.py), for files crossing to the machine driving this.
    share: Path | None = None
    # Workspace files, in order (the first sets home), and the user's home override (D99).
    workspaces: list[Path] = field(default_factory=list)
    home_override: str = ""
    # Earlier homes (DECISIONS 64): their conversations stay listed.
    homes_before: list[str] = field(default_factory=list)
    # A worktree session (DECISIONS 66): the profile it belongs to, and its {"name", "repo", "path"}.
    worktree_of: str = ""
    worktree: dict[str, str] = field(default_factory=dict)
    # The AI service its conversations use (item K): claude, copilot or codex.
    agent: str = "claude"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "client": self.client, "label": self.label or self.name,
                "projects": [str(p) for p in self.projects], "model": self.model, "brief": self.brief,
                "share": str(self.share) if self.share else "",
                "workspaces": [str(w) for w in self.workspaces], "home_override": self.home_override,
                "homes_before": list(self.homes_before), "agent": self.agent,
                "worktree_of": self.worktree_of, "worktree": dict(self.worktree)}


@dataclass
class Pending:
    """A permission request waiting for someone to answer it."""
    id: str
    tool: str
    detail: dict[str, Any]
    asked_at: float = field(default_factory=time.time)
    answer: asyncio.Future | None = None


def resolve_cli(name: str) -> str:
    """The path to run, not the name to look up.

    shutil.which applies PATHEXT, so it finds claude.CMD from "claude". Windows CreateProcess does
    not, so handing the bare name to Popen fails with "The system cannot find the file specified" -
    and the Session page said the CLI was available, because availability was checked with which
    and the launch was not. The check and the launch have to ask the same question.
    """
    found = shutil.which(name)
    if found:
        return found
    return name          # an explicit path that which cannot see: let the launch report it


class Session:
    """Runs one Claude Code session and fans its events out to whoever is watching.

    One of these per profile. What used to be global to the proxy - the process, the transcript,
    the permission queue, the approval token - lives here, which is what lets several run at once.
    """

    def __init__(self, claude: str = "claude", base_url: str = "", projects: list[Path] | None = None,
                 permission_mode: str = "manual", approve_timeout_s: float = 0,
                 approve_url: str = "", profile: "Profile | None" = None,
                 sessions_dir: Path | None = None, thinking_display: str = "summarized") -> None:
        self.profile = profile
        # Another agent's environment and transcript folder (item K), set by the host before each start.
        self.agent_env: dict[str, str] = {}
        self.transcripts_root: Path | None = None
        # Current models omit thinking text unless asked; "summarized" is what the transcript's
        # thinking line shows (the user, 2026-10-01). Empty leaves the CLI's default.
        self.thinking_display = thinking_display
        # Where brokers keep their sessions (DECISIONS 49). Without one, Claude Code is a child of
        # this process as it always was - which a proxy restart orphans.
        self.sessions_dir = Path(sessions_dir) if sessions_dir else None
        self.folder: Path | None = None
        self.claude = resolve_cli(claude or "claude")
        self.base_url = base_url
        # Where the approval host sends its questions, and the secret it must present. The token is
        # new for every session, so a stale process cannot answer for the current one.
        self.approve_url = approve_url
        self.approve_token = ""
        self.projects = [Path(p) for p in (projects or [])]
        self.permission_mode = permission_mode
        self.approve_timeout_s = approve_timeout_s
        self.origin = ""               # where the prompts are being typed (origin.py), set per prompt
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        # Sending the page only what it has not seen (D140): which buffer an event number belongs
        # to, and the newest number that has fallen off its front - a page holding an older one
        # would have a gap, so it is sent the whole buffer instead.
        self.epoch = ""
        self._dropped_upto = 0
        self._new_epoch()
        self.slash_commands: list[str] = []      # what the CLI's init names (feature 84)
        self.transcript_folders: list[str] = []   # where its conversations may be filed (DECISIONS 64)
        # A new conversation's title (E3 part 10): {'name': typed} or {'auto': True}, with a count of
        # its replies; None for a resumed one. on_reply is the host's handler.
        self.title_plan: dict[str, Any] | None = None
        self.on_reply: Any = None
        self.pending: dict[str, Pending] = {}
        # For the status poll (feature 44): turns finished so far, which only counts up, and
        # whether one is in progress. A page compares one poll with the next to say "it is done".
        self.turns = 0
        # The finished-turn count somebody has looked at, on any page (D200): a "Claude has finished"
        # alert up to here is cleared on every computer, not only the one it was opened on.
        self.seen_turns = 0
        self.seen_waiting = 0       # questions waiting when it was last looked at; reported capped at what waits now
        self.background = 0         # background tasks still running (D202), from the CLI's live list
        self.busy = False
        # What Claude is doing now ("thinking...", a tool's name), from the fragments the buffer
        # drops (D29). The stream shows it as it comes; a page polling through the tunnel has no
        # stream, so the state carries it (the user, 2026-10-12).
        self.doing = ""
        self.session_id = ""
        self.project = ""
        self.started_at = 0.0
        self.last_error = ""
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._watchers: set[asyncio.Queue] = set()
        self._sequence = 0
        self._roots_key: tuple | None = None       # see _allowed_roots
        self._roots_cache: tuple[list[Path], list[Path]] = ([], [])
        self._briefed = False      # the profile's standing brief travels with the first prompt
        # In-flight control requests, by request_id: the CLI's own protocol, used for the
        # interrupt that Escape needs (feature 48). Not the same map as self.pending, which
        # is tool approvals waiting on a person.
        self._control: dict[str, asyncio.Future] = {}
        # The interrupt still waiting for its answer, if any. A second Escape joins it rather than
        # writing another request that would also land, later.
        self._interrupt_pending: asyncio.Future | None = None
        # Prompts sent while a turn was running (D176): held here, shown on the page as waiting,
        # and sent one at a time as each turn ends - not written to stdin, where the CLI held them
        # out of sight until the turn finished. `held` ones came back from before a proxy restart
        # and are sent only when someone says so.
        self.queued: list[dict[str, Any]] = []
        self._flushing: asyncio.Task | None = None
        # True while a re-attached broker replays what it kept (D179): a turn ending in the replay is
        # history, so it sends nothing; the end of the replay decides.
        self._replaying = False

    # ---- what the UI asks for ---------------------------------------------------------

    def available(self) -> bool:
        """True when the Claude Code CLI is on this machine."""
        return bool(shutil.which(self.claude) or Path(self.claude).exists())

    def activity(self) -> dict[str, Any]:
        """The few numbers the status poll carries for this session (feature 44)."""
        return {"name": self.profile.name if self.profile else "", "running": self.running,
                "busy": self.busy, "turns": self.turns, "waiting": len(self.pending),
                "seen_turns": self.seen_turns, "seen_waiting": min(self.seen_waiting, len(self.pending)),
                "background": self.background if self.running else 0}

    def mark_seen(self, turns: int) -> None:
        """A page looked at this session with `turns` finished (D200). Only moves forward, and never
        past what has actually finished; the questions waiting now count as seen too."""
        self.seen_turns = max(self.seen_turns, min(int(turns), self.turns))
        self.seen_waiting = len(self.pending)

    def _new_epoch(self) -> None:
        """The buffer was emptied (a start, a reattach): numbers the page holds no longer apply."""
        self.epoch = uuid.uuid4().hex[:12]
        self._dropped_upto = 0

    def catch_up(self, projects: list[str]) -> bool:
        """As _catch_up, reading this session's agent's transcripts (item K)."""
        from .history import using_root

        with using_root(self.transcripts_root):
            return self._catch_up(projects)

    def _catch_up(self, projects: list[str]) -> bool:
        """A stopped session's buffer from the transcript of the conversation it last ran, when that
        file has changed (E3, Brad 2026-10-19). One client's pane showed its last sitting on this page
        (09/10) while the conversation had gone on to 16/10 in VS Code. Read only when the file's
        size or time moves, so a long transcript is not parsed on every poll. True when refilled."""
        if self.running or not self.session_id:
            return False
        from .history import _transcript, holding, replay

        folder = holding([*projects, *self.transcript_folders], self.session_id)
        path = _transcript(folder, self.session_id, None) if folder else None
        if path is None:
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        stamp = (stat.st_size, stat.st_mtime)
        if stamp == getattr(self, "_caught_up", None):
            return False
        self._caught_up = stamp
        events = replay(folder, self.session_id, MAX_EVENTS)
        if not events:
            return False
        self.events.clear()
        self._new_epoch()                       # the page's numbers no longer apply: drawn afresh
        self._replaying = True
        try:
            for event in events:
                self._emit({**event, "replayed": True})
        finally:
            self._replaying = False
        return True

    def events_since(self, since: int) -> list[dict[str, Any]] | None:
        """The kept events after `since`, or None when some may have been dropped since then."""
        if since < self._dropped_upto:
            return None
        return [e for e in self.events if e.get("seq", 0) > since]

    def state(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "name": self.profile.name if self.profile else "",
            "client_label": self.profile.label if self.profile else "",
            "brief": self.profile.brief if self.profile else "",
            "running": self.running,
            "approvals": bool(self.approve_url),
            "available": self.available(),
            "session_id": self.session_id,
            "project": self.project,
            "projects": [str(p) for p in self.projects],
            "started_at": self.started_at,
            # When this session last did anything (2026-10-16): the page opens on the running
            # session with the newest, when the address names none.
            "last_active": float((self.events[-1].get("at") if self.events else 0) or self.started_at or 0),
            "permission_mode": self.permission_mode,
            "pending": [{"id": p.id, "tool": p.tool, "detail": p.detail, "asked_at": p.asked_at}
                        for p in self.pending.values()],
            "busy": self.busy,
            # Why the conversation cannot be switched now (item J); "" when it can, or not running.
            "switch_blocked": work_in_progress(self) if self.running else "",
            "doing": self.doing,
            "queued": [dict(item) for item in self.queued],
            "events": list(self.events),
            "error": self.last_error,
        }

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    # ---- the child process ------------------------------------------------------------


    def _command(self, project: Path, resume: str, model: str) -> list[str]:
        check_resume(resume)
        check_model(model)
        # Another AI service (item K): aicleaner's bridge stands where Claude Code would, given the same
        # arguments, and speaks the agent's own protocol on its other side (agents/bridge.py).
        program = ([sys.executable, "-m", "aicleaner.agents.bridge", "--agent", self.agent]
                   if self.agent != "claude" else [self.claude])
        command = [*program, "--print", "--verbose", "--output-format", "stream-json",
                   "--input-format", "stream-json", "--include-partial-messages",
                   "--permission-mode", self.permission_mode]
        if self.approve_url:
            # Permission prompts go to our own MCP tool, which puts them on the Session page.
            config = {"mcpServers": {"aicleaner": {
                "command": sys.executable,
                "args": ["-m", "aicleaner.mcp_approve"],
                # A question waits for the person, not for Claude Code's 30-minute MCP idle limit
                # (the user, 2026-10-18: two questions were cut off; "it should be 12 hours or so").
                "timeout": APPROVE_MCP_TIMEOUT_MS,
                "env": {"AICLEANER_APPROVE_URL": self.approve_url,
                        "AICLEANER_APPROVE_TOKEN": self.approve_token,
                        "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
            }}}
            command += ["--permission-prompts", "host",
                        "--permission-prompt-tool", "mcp__aicleaner__approve",
                        "--mcp-config", json.dumps(config)]
        else:
            # Nobody is at the keyboard and nothing to ask: deny rather than hang.
            command += ["--permission-prompts", "none"]
        # Every folder of the profile, and the client's share, beside the one it starts in
        # (DECISIONS 64): "the real value lies in which folders Claude can access". Each once - the
        # share is often also one of the folders - and never the starting folder itself. The share
        # is how a file you drop in gets read and a file Claude writes gets collected. Prompts
        # still apply.
        share = self.profile.share if self.profile else None
        extra: list[str] = []
        for folder in [*[str(f) for f in self.projects], *([str(share)] if share else [])]:
            if _same_folder(folder, project) or any(_same_folder(folder, known) for known in extra):
                continue
            extra.append(folder)
        for folder in extra:
            command += ["--add-dir", folder]
        if self.thinking_display:
            # Undocumented in --help (CLI 2.1.286) but accepted; without it every thinking block
            # arrives empty. Checked 2026-10-01.
            command += ["--thinking-display", self.thinking_display]
        # One argument each, "--flag=value": the value can never be read as a flag of its own.
        if resume:
            command.append(f"--resume={resume}")
        if model:
            command.append(f"--model={model}")
        return command

    def _child_environment(self) -> dict[str, str]:
        """What Claude Code is started with.

        MCP_TOOL_TIMEOUT: a question waits on the page for as long as it takes (the user,
        2026-10-01), and the approval tool is an MCP tool call, so Claude Code's own limit on one
        is lifted to a week. Not higher: Node's timers cap at about 24.8 days and a larger value
        fires at once. A value you set yourself is left alone.

        CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT: a second, separate limit - a call with no response or
        progress - which aborted a question after 30 minutes (D102). 0 turns it off."""
        environment = dict(os.environ)
        if self.base_url:
            # The whole point: what the session sends goes through the filter like anything else.
            environment["ANTHROPIC_BASE_URL"] = self.base_url
        environment.setdefault("MCP_TOOL_TIMEOUT", str(QUESTION_WAIT_MS))
        environment.setdefault("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "0")
        # Tool search, which Claude Code turns off for a base URL that is not Anthropic's unless told the
        # proxy forwards tool_reference blocks - aicleaner does (checked live, 2026-10-20).
        environment.setdefault("ENABLE_TOOL_SEARCH", "true")
        environment.update(self.agent_env)
        return environment

    @property
    def agent(self) -> str:
        """The AI service this session's conversations use (item K)."""
        return (self.profile.agent if self.profile is not None else "claude") or "claude"

    async def start(self, project: str, resume: str = "", model: str = "") -> dict[str, Any]:
        """Start a session in one of the configured project folders."""
        if self.running:
            raise SessionError("a session is already running; stop it first")
        if not self.available():
            raise SessionError(f"the Claude Code CLI ({self.claude}) is not on this machine")
        folder = self._project_folder(project)
        environment = self._child_environment()
        self.events.clear()
        self._new_epoch()
        # Continuing a conversation hands the model its history; the page had none of it, because
        # the buffer starts empty and the earlier turns live in the CLI's own transcript. Seed
        # them, so resuming looks like picking something up rather than starting over. Off the
        # loop: it opens and parses a file that can be long.
        # Another agent replays a resumed conversation itself, through the bridge (item K): seeding it
        # from its transcript here as well would draw every earlier turn twice.
        if resume and self.agent == "claude":
            # Filed under the folder it began in, a conversation kept going back there (the user,
            # 2026-10-12): moved under this folder first, it is this project's from now on.
            from .history import refile
            try:
                await asyncio.to_thread(refile, resume, folder, [str(folder), *self.transcript_folders])
            except OSError as exc:
                log.warning("could not refile conversation %s under %s: %s", resume, folder, exc)
            await self._seed(folder, resume)
        self.pending.clear()
        self.seen_waiting = 0
        self.approve_token = secrets_module.token_urlsafe(24)
        self.session_id = ""
        self.last_error = ""
        self._briefed = False
        self._replaying = False
        self.project = str(folder)
        self.started_at = time.time()
        command = self._command(folder, resume, model)
        log.info("session starting in %s: %s", folder, " ".join(command[:6]))
        if self.sessions_dir is not None:
            await self._start_brokered(command, folder, resume, model)
            return self.state()
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command, cwd=str(folder), env=environment,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=STREAM_LINE_LIMIT)
        except OSError as exc:
            self.project = ""
            raise SessionError(f"could not start {self.claude}: {exc}") from exc
        self._reader = asyncio.ensure_future(self._read_stdout())
        asyncio.ensure_future(self._read_stderr())
        return self.state()

    async def _start_brokered(self, command: list[str], folder: Path, resume: str, model: str) -> None:
        """Start Claude Code inside a broker of its own (DECISIONS 49)."""
        self.folder = self.sessions_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.folder.mkdir(parents=True, exist_ok=True)
        env = {"ANTHROPIC_BASE_URL": self.base_url} if self.base_url else {}
        env.update(self.agent_env)                      # another agent's (item K): proxy, certificate, command
        launch = {"command": command, "cwd": str(folder), "env": env,
                  "profile": self.profile.name if self.profile else "", "project": str(folder),
                  "resume": resume, "model": model, "approve_token": self.approve_token,
                  "started_at": self.started_at}
        (self.folder / "launch.json").write_text(json.dumps(launch, indent=2), encoding="utf-8")
        try:
            spawn_broker(self.folder)
            child = BrokerChild(self.folder, await published_by(self.folder))
            await child.connect()
        except (OSError, SessionError) as exc:
            self.project = ""
            raise SessionError(f"could not start {self.claude}: {exc}") from exc
        self._attach(child)

    async def _seed(self, project: Path, resume: str, before: float | None = None) -> None:
        """Put a resumed conversation's earlier turns back in the buffer, read from the CLI's
        transcript. Off the loop: it opens and parses a file that can be long. Up to the whole
        buffer (D109; it was 200): the seed comes first, so anything newer still wins the space."""
        from .history import holding, replay

        # Wherever the conversation is filed: it stays in the folder it began in, which need not be
        # the one this session starts in (D113 - the page drew a blank when it looked only there).
        project = Path(await asyncio.to_thread(holding, [str(project), *self.transcript_folders], resume)
                       or project)
        for event in await asyncio.to_thread(replay, project, resume, MAX_EVENTS, None, before):
            self._emit({**event, "replayed": True})

    def _attach(self, child: "BrokerChild") -> None:
        child.on_input = self._replayed_input
        self._process = child          # type: ignore[assignment]
        self._reader = asyncio.ensure_future(self._read_stdout())
        asyncio.ensure_future(self._read_stderr())

    def _replayed_input(self, line: str, at: float) -> None:
        """A prompt sent before this proxy started, read back from the broker so the transcript
        still shows what was asked."""
        try:
            message = json.loads(line)
        except ValueError:
            return
        if message.get("type") != "user":
            return
        text = " ".join(b.get("text", "") for b in (message.get("message") or {}).get("content") or []
                        if isinstance(b, dict) and b.get("type") == "text")
        if text:
            # A turn was asked for before the restart; a replayed result after it means it ended.
            # Whatever is left when the replay ends is whether a turn is still running (D179).
            self.busy = True
            self._emit({"type": "prompt", "text": text, "at": at})

    async def reattach(self, folder: Path, launch: dict[str, Any], published: dict[str, Any]) -> None:
        """Pick up a session a previous proxy started, still running in its broker (DECISIONS 49).
        Everything it said is replayed, so the page's transcript is whole again."""
        self.folder = folder
        self.project = str(launch.get("project", ""))
        self.approve_token = str(launch.get("approve_token", ""))
        self.started_at = float(launch.get("started_at") or time.time())
        self.events.clear()
        self._new_epoch()
        self.pending.clear()
        self.seen_waiting = 0
        self.last_error = ""
        self._briefed = True           # the brief went with the first prompt, before the restart
        self._load_queue()
        self._replaying = True
        resume = str(launch.get("resume") or "")
        # Another agent's earlier turns were replayed by its bridge inside this sitting (item K), which the
        # broker replays now: seeding them as well would show them twice.
        if resume and self.project and self.agent == "claude":
            # Only what came before this sitting: the resumed conversation writes to the file the
            # seed reads, and the broker replays the sitting itself - seeding it too showed it
            # twice (D109).
            await self._seed(Path(self.project), resume, before=self.started_at)
        child = BrokerChild(folder, published)
        await child.connect()
        self._attach(child)

    async def detach(self) -> None:
        """Let go of a brokered session without ending it: this proxy is stopping, it is not."""
        process = self._process
        if isinstance(process, BrokerChild):
            self._process = None
            if self._reader is not None:
                self._reader.cancel()
                self._reader = None
            await process.detach()

    def _project_folder(self, project: str) -> Path:
        """The folder to run in: one of the configured projects, never anywhere else."""
        wanted = Path(project).expanduser() if project else (self.projects[0] if self.projects else None)
        if wanted is None:
            raise SessionError("no project folders are configured (session.projects)")
        if network_path(str(project)):
            raise SessionError("a session cannot run in a network or device path")
        wanted = wanted.resolve()
        allowed = [p.resolve() for p in self.projects]
        # No configured folders means nowhere, never "anywhere the caller names" (H28).
        if wanted not in allowed:
            raise SessionError(f"{wanted} is not one of the configured project folders")
        if not wanted.is_dir():
            raise SessionError(f"{wanted} is not a folder")
        return wanted

    def _allowed_roots(self, share: Path | str | None) -> tuple[list[Path], list[Path]]:
        """(project roots, everything browsable) resolved, worked out once per folder list.

        Two lists because they mean different things to the caller: the response advertises the
        *project* roots, while the share is browsable without being one of them.

        `resolve()` touches the filesystem and follows reparse points, and the share is normally a
        OneDrive folder, which is one. Every listing of every folder was re-resolving *all* the
        roots and not just the one being browsed, so opening a tree cost that repeatedly - slow
        while the OS metadata cache was cold, free once it was warm, which is what made the page
        feel worse for the first few minutes after a start.

        Keyed on the folder list itself, so a profile edited mid-session resolves afresh rather
        than browsing yesterday's folders.
        """
        key = (tuple(str(p) for p in self.projects), str(share or ""))
        if self._roots_key != key:
            roots = [p.resolve() for p in self.projects]
            # The share browses like a repo folder; it is simply another root this session may show.
            allowed = roots + ([Path(share).resolve()] if share else [])
            self._roots_cache = (roots, allowed)
            self._roots_key = key
        return self._roots_cache

    def _locate(self, root: str, path: str) -> tuple[list[Path], Path, Path]:
        """(project roots, the root chosen, the target) for a root and a path within it - the one
        allow-list for listing a folder AND for reading a file (DECISIONS 50), so the two can never
        drift apart. Resolved before it is checked: "aicleaner/../.." must not be judged by how it
        reads, and a link is judged by where it goes."""
        share = self.profile.share if self.profile else None
        if not self.projects and not share:
            raise SessionError("no project folders are configured (session.projects)")
        # Before anything resolves it (H27): resolving a UNC root authenticates to that host.
        if network_path(root) or network_path(path):
            raise SessionError("a network or device path cannot be browsed from here")
        roots, allowed = self._allowed_roots(share)
        chosen = allowed[0]
        if root:
            wanted = Path(root).expanduser().resolve()
            if wanted not in allowed:
                raise SessionError(f"{wanted} is not one of the configured project folders")
            chosen = wanted
        target = (chosen / path).resolve() if path else chosen
        # A link (or junction) may lead into another of the session's own folders, which it may
        # browse anyway (D171); anywhere else is refused.
        if not _inside(target, chosen) and not any(_inside(target, other) for other in allowed):
            raise SessionError(f"{target} is outside {chosen}")
        return roots, chosen, target

    def locate_file(self, root: str, path: str) -> Path:
        """A file inside the session's folders, for the file tabs; never a folder."""
        _roots, _chosen, target = self._locate(root, path)
        if not target.is_file():
            raise SessionError(f"{target} is not a file")
        return target

    def read_file(self, root: str, path: str) -> dict[str, Any]:
        """What a file tab shows (DECISIONS 50): its text when it is text and small enough, and
        otherwise why not - never the bytes of something a tab cannot show."""
        target = self.locate_file(root, path)
        stat = target.stat()
        kind, view = file_kind(target.name)
        answer: dict[str, Any] = {"name": target.name, "path": path, "root": root, "size": stat.st_size,
                                  "modified": stat.st_mtime, "read_at": time.time(), "kind": kind, "view": view}
        if view == "download":
            return {**answer, "download_only": True}
        if view != "text":
            return answer                       # an image or a PDF: the page asks /file/raw for it
        if stat.st_size > MAX_TAB_TEXT:
            return {**answer, "too_big": True}
        with target.open("rb") as fh:
            data = fh.read(MAX_TAB_TEXT + 1)
        if b"\x00" in data[:8192]:
            return {**answer, "binary": True}
        # Editable only when it reads as UTF-8 exactly (DECISIONS 53): saving text that was decoded
        # with replacement characters would write those characters back over the original bytes.
        try:
            return {**answer, "text": data.decode("utf-8-sig"), "editable": True}
        except UnicodeDecodeError:
            return {**answer, "text": data.decode("utf-8", errors="replace"), "editable": False}

    def write_file(self, root: str, path: str, text: str, read_modified: float) -> dict[str, Any]:
        """Save a file tab (F54, DECISIONS 53): an existing, editable text file inside the session's
        folders, through the same allow-list as reading. Refused with StaleWrite when the file is
        newer than the time the tab read - Claude edits these files too, and nothing locks them.
        Line endings and a byte-order mark are kept as they were: a browser hands back LF."""
        target = self.locate_file(root, path)
        if not target.is_file():
            raise SessionError(f"{path} is not an existing file; files are created from the tree, not saved new")
        current = self.read_file(root, path)
        if current.get("view") != "text" or not current.get("editable"):
            raise SessionError(f"{path} is not a text file that can be edited here")
        if len(text.encode("utf-8")) > MAX_TAB_TEXT:
            raise SessionError("that is more text than a file tab saves")
        if current["modified"] > float(read_modified) + 0.001:
            raise StaleWrite(current["modified"])
        raw = target.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        crlf = b"\r\n" in raw[:65536]
        body = text.replace("\r\n", "\n")
        if crlf:
            body = body.replace("\n", "\r\n")
        data = (b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8")
        tmp = target.with_name(f".{target.name}.aicleaner-save")
        tmp.write_bytes(data)
        from .fileio import replace
        replace(tmp, target)
        stat = target.stat()
        return {"root": root, "path": path, "modified": stat.st_mtime, "size": stat.st_size}

    def stat_file(self, root: str, path: str) -> dict[str, Any]:
        """Whether a file has changed since it was read (the 15-second check, DECISIONS 50). The
        same answer shape a watcher will push when editing lands."""
        try:
            target = self.locate_file(root, path)
            stat = target.stat()
        except (SessionError, OSError):
            return {"root": root, "path": path, "exists": False}
        return {"root": root, "path": path, "exists": True, "modified": stat.st_mtime, "size": stat.st_size}

    def browse(self, root: str = "", path: str = "") -> dict:
        """One folder of one configured project: names, never contents.

        Saying where to look is half of driving a session, and a phone has no terminal beside it
        to run `ls` in. Nothing is filtered - a dotfile or a gitignored file is exactly the sort of
        thing a question gets asked about, and a browser that hides things cannot answer the
        question it exists for.

        A browser is also how a sandbox gets escaped, so the path is resolved first, with `..` and
        symlinks and all, and only then checked to be inside a project folder.
        """
        roots, chosen, target = self._locate(root, path)
        if not target.is_dir():
            raise SessionError(f"{target} is not a folder")

        entries = []
        try:
            found = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as problem:
            raise SessionError(f"{target} cannot be read: {problem}") from problem
        for item in found:
            entry = {"name": item.name, "dir": item.is_dir()}
            if item.is_dir():
                # A folder nobody can read is listed and marked: a silent gap is worse than a note.
                try:
                    next(item.iterdir(), None)
                except OSError:
                    entry["error"] = "unreadable"
            else:
                try:
                    entry["size"] = item.stat().st_size
                except OSError:
                    entry["error"] = "unreadable"
                # What its tab can do - text, image, pdf or download - decided here, once, so the
                # page shows View only where it will work (DECISIONS 50).
                entry["view"] = file_kind(item.name)[1]
            # A link out of the project is the oldest trick there is; say so rather than follow it.
            # Junctions too (D171), and a link into another of the session's folders is fine.
            if is_link(item):
                try:
                    lands = item.resolve()
                except OSError:
                    lands = None
                if lands is None or not any(_inside(lands, root) for root in self._allowed_roots(
                        self.profile.share if self.profile else None)[1]):
                    entry["error"] = "points outside the project"
            entries.append(entry)

        # Through a link the folder is named by the path it was asked for, under its own root.
        if _inside(target, chosen):
            relative = "" if target == chosen else target.relative_to(chosen).as_posix()
        else:
            relative = "/".join(p for p in str(path).replace("\\", "/").split("/") if p and p != ".")
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        return {
            "roots": [str(p) for p in roots],
            "root": str(chosen),
            "path": relative,
            "parent": parent,
            "separator": "\\" if chosen.drive else "/",
            "entries": entries,
        }

    def search(self, query: str, limit: int = SEARCH_LIMIT, budget_s: float = SEARCH_BUDGET_S) -> dict:
        """Files and folders whose names contain `query`, across every folder the tree shows
        (feature 90): case-insensitive, full paths, names never contents.

        Skipped, as the user decided: a folder that cannot be read, and one holding installed
        dependencies or tools rather than work (`_dependency_folder`). Links are not followed, so a
        link out of the project is never walked. Stops at `limit` results or `budget_s` seconds and
        says so, rather than holding the page on a folder of a million files.
        """
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            raise SessionError("type at least 2 characters to search")
        share = self.profile.share if self.profile else None
        if not self.projects and not share:
            raise SessionError("no project folders are configured (session.projects)")
        _roots, allowed = self._allowed_roots(share)
        walk = SearchWalk(needle, allowed)
        return walk.answer(query, walk.run(limit, budget_s), continued=False)

    def search_walk(self, query: str) -> "SearchWalk":
        """A fresh walk for `query` over the tree's folders, for SessionHost to keep and continue."""
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            raise SessionError("type at least 2 characters to search")
        share = self.profile.share if self.profile else None
        if not self.projects and not share:
            raise SessionError("no project folders are configured (session.projects)")
        return SearchWalk(needle, self._allowed_roots(share)[1])

    async def prompt(self, text: str) -> str:
        """Send one user message; the reply arrives as events. While a turn is running it waits
        in `queued` instead (D176) and goes when the turn ends. Returns "sent" or "queued"."""
        if not self.running or self._process is None or self._process.stdin is None:
            raise SessionError("no session is running")
        if not text.strip():
            raise SessionError("nothing to send")
        if self.busy:
            self.queued.append({"id": uuid.uuid4().hex[:12], "text": text, "at": time.time(), "held": False})
            self._queue_changed()
            return "queued"
        await self._send(text)
        return "sent"

    def _queue_changed(self) -> None:
        """Keep the waiting prompts on disk beside the broker, and tell watchers the list changed."""
        self._save_queue()
        self._emit({"type": "queue", "queued": [dict(item) for item in self.queued]})

    def _queue_file(self) -> Path | None:
        return self.folder / "queued.json" if self.folder is not None else None

    def _save_queue(self) -> None:
        path = self._queue_file()
        if path is None:
            return
        try:
            path.write_text(json.dumps(self.queued, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("could not keep the waiting prompts in %s: %s", path, exc)

    def _load_queue(self) -> None:
        """Waiting prompts from before a proxy restart come back, and go when the turn they waited
        on ends - or, if it ended while the proxy was down, as soon as the replay shows it (D179).
        They came back held at first, which stranded a prompt across every deploy made mid-turn."""
        path = self._queue_file()
        self.queued = []
        if path is None or not path.is_file():
            return
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read the waiting prompts in %s: %s", path, exc)
            return
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("id"):
                self.queued.append({"id": str(item["id"]), "text": item["text"],
                                    "at": float(item.get("at") or 0), "held": bool(item.get("held"))})

    async def cancel_queued(self, item_id: str) -> None:
        """Drop one waiting prompt."""
        before = len(self.queued)
        self.queued = [item for item in self.queued if item["id"] != item_id]
        if len(self.queued) == before:
            raise SessionError("that prompt is no longer waiting")
        self._queue_changed()

    async def send_queued_now(self, item_id: str) -> str:
        """Send one waiting prompt now: to the front, and the running turn interrupted so it goes
        as soon as the CLI ends it. Not busy: it is sent at once. Returns "sent" or "interrupting"."""
        found = [item for item in self.queued if item["id"] == item_id]
        if not found:
            raise SessionError("that prompt is no longer waiting")
        item = {**found[0], "held": False}
        self.queued = [item] + [other for other in self.queued if other["id"] != item_id]
        if not self.busy:
            self.queued.pop(0)
            self._queue_changed()
            await self._send(item["text"])
            return "sent"
        self._queue_changed()
        await self.interrupt(cancel_queued=True)
        return "interrupting"

    def take_waiting(self) -> list[str]:
        """The waiting prompts, handed to Claude between tool calls (feature 119): every one not
        held, in order, off the queue and shown as sent. Asked for by the PostToolUse hook, so only
        mid-turn; a held prompt and everything after it stay, as they would at the turn's end."""
        if not self.busy or not self.running:
            return []
        taken: list[str] = []
        while self.queued and not self.queued[0].get("held"):
            taken.append(self.queued.pop(0)["text"])
        if taken:
            self._queue_changed()
            for text in taken:
                self._emit({"type": "prompt", "text": text, "between_tools": True})
        return taken

    def _flush_soon(self) -> None:
        """A turn ended: send the next waiting prompt, unless it is held or something else is."""
        if not self.queued or self.queued[0].get("held") or not self.running:
            return
        if self._flushing is not None and not self._flushing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flushing = loop.create_task(self._send_next())

    async def _send_next(self) -> None:
        if self.busy or not self.queued or self.queued[0].get("held") or not self.running:
            return
        item = self.queued.pop(0)
        self._queue_changed()
        try:
            await self._send(item["text"])
        except (SessionError, OSError) as exc:
            # Put back rather than lost: it is still on screen, and Send now can try again.
            self.queued.insert(0, {**item, "held": True})
            self.last_error = f"a waiting prompt could not be sent: {exc}"
            self._queue_changed()

    async def _send(self, text: str) -> None:
        if not self.running or self._process is None or self._process.stdin is None:
            raise SessionError("no session is running")
        sent = text
        self.busy = True
        brief = (self.profile.brief if self.profile else "").strip()
        if brief and not self._briefed:
            # What this engagement is, and what not to touch. Sent with the first prompt rather
            # than as a turn of its own, so it costs no extra reply, and shown in the transcript,
            # because what went out is what the transcript has to say went out.
            sent = f"{brief}\n\n{text}"
            self._briefed = True
            self._emit({"type": "brief", "text": brief})
        content = [{"type": "text", "text": sent}]
        # Not with a slash command: sent as typed, or the CLI does not recognise it as one (the user,
        # 2026-10-18: /ralph-loop:help arrived as an ordinary message with the note after it).
        if self.origin and not text.lstrip().startswith("/"):
            # Where it was typed (origin.py, 2026-10-18): a block of its own, left out of the page's view.
            content.append({"type": "text", "text": self.origin})
        message = {"type": "user", "message": {"role": "user", "content": content}}
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._process.stdin.drain()
        self._emit({"type": "prompt", "text": text})

    async def interrupt(self, cancel_queued: bool = True) -> None:
        """Stop the turn that is running, and keep the session alive.

        Not the same thing as stop(), which closes stdin and kills the process. This is the
        control request the CLI's own stream-json input protocol defines, which is why the
        session had to be started with --input-format stream-json in the first place:

            {"type": "control_request", "request_id": "...",
             "request": {"subtype": "interrupt", "cancel_queued": true}}

        The reply is a control_response echoing request_id. It is waited for rather than fired
        and forgotten, because the caller is a person who pressed Escape and needs to be told
        whether anything happened - and because an interrupt that silently did nothing is worse
        than a button that says it cannot.

        cancel_queued drops prompts sent while the turn was still running. Escape means "stop",
        and leaving a queue to run on after it would be the opposite of what was asked.

        Returns "done" when the CLI answered in time, "pending" when it has not yet. Pending is
        not failure: a CLI in the middle of a tool call reads its input late, and the request is
        already there. This used to raise "did not answer" and forget the request, and the CLI
        then cut the turn off a few seconds later with nothing on the page to say why - which
        looked exactly like a dropped connection. The late answer is now reported when it lands.
        """
        if self._interrupt_pending is not None and not self._interrupt_pending.done():
            return "pending"
        try:
            await self._ask_control("interrupt", "interrupt", on_late=self._interrupt_answered_late,
                                    cancel_queued=bool(cancel_queued))
        except ControlPending:
            self._emit({"type": "interrupt_pending"})
            return "pending"
        self._emit({"type": "interrupted"})
        return "done"

    def _interrupt_answered_late(self, answer: dict) -> None:
        self._interrupt_pending = None
        if str(answer.get("subtype", "")) == "success":
            self._emit({"type": "interrupted", "late": True})
        elif self.running:
            # Refused, or released because the session is stopping; stop() reports the latter.
            self._emit({"type": "interrupt_refused", "error": str(answer.get("error", "refused"))})

    async def set_permission_mode(self, mode: str) -> None:
        """Change how this session asks before it acts, without restarting it.

        The mode is passed as --permission-mode when the child starts, so before this existed
        the only way to change it was to stop the session and start a new one - and the only way
        to change the default was to hand-edit aicleaner-config.json, because it was not in
        Config.EDITABLE either. A person driving a session from this page had no way to see which
        mode they were in, let alone leave it.

        `/permissions` is not the answer for us: it opens a picker, and commands.py deliberately
        lists only the built-ins that survive --print. This is the same control channel the
        interrupt uses, which the CLI drives from its own setPermissionMode().
        """
        if mode not in PERMISSION_MODES:
            raise SessionError(f"{mode!r} is not a permission mode; one of: {', '.join(PERMISSION_MODES)}")
        def applied(answer: dict) -> None:
            if str(answer.get("subtype", "")) == "success":
                self.permission_mode = mode
                self._emit({"type": "permission_mode", "mode": mode})

        await self._ask_control("set_permission_mode", "mode change", on_late=applied, mode=mode)
        applied({"subtype": "success"})

    async def _ask_control(self, subtype: str, what: str,
                           on_late: Callable[[dict], None] | None = None, **fields: Any) -> dict:
        """One control request on the CLI's stream-json channel, waited out for its answer.

        Shared by every control request rather than copied per caller: the wait, the timeout and
        the refusal all have to behave the same way, and a second copy is where they drift.

        A request that times out is still in the CLI's input and will still be acted on, so it
        stays tracked: `on_late` is called with the answer when it arrives, and the caller is
        told ControlPending rather than a failure that is not true. Without `on_late` the old
        behaviour stands - forget it and report that it was not answered.
        """
        if not self.running or self._process is None or self._process.stdin is None:
            raise SessionError("no session is running")
        request_id = f"{subtype}-{uuid.uuid4().hex[:12]}"
        waiting: asyncio.Future = asyncio.get_running_loop().create_future()
        self._control[request_id] = waiting
        message = {"type": "control_request", "request_id": request_id,
                   "request": {"subtype": subtype, **fields}}
        try:
            self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            self._control.pop(request_id, None)
            raise SessionError(f"the session did not take the {what}: {exc}") from exc
        try:
            # Short, because a person who just pressed something should be told something within
            # seconds. Shielded, because wait_for cancels what it waits on when it gives up, and
            # a cancelled future can never deliver the late answer.
            answer = await asyncio.wait_for(asyncio.shield(waiting), timeout=INTERRUPT_TIMEOUT)
        except asyncio.TimeoutError as exc:
            if on_late is None:
                self._control.pop(request_id, None)
                raise SessionError(f"the session did not answer the {what}") from exc
            waiting.add_done_callback(lambda done: on_late(done.result()))
            if subtype == "interrupt":
                self._interrupt_pending = waiting
            raise ControlPending(f"the {what} was sent; the session has not answered yet") from exc
        if str(answer.get("subtype", "")) != "success":
            raise SessionError(str(answer.get("error", f"the session refused the {what}")))
        return answer

    def _release_control(self, why: str) -> None:
        """Wake every in-flight control request with a refusal rather than leaving it to time out."""
        for waiting in list(self._control.values()):
            if not waiting.done():
                waiting.set_result({"subtype": "error", "error": why})
        self._control.clear()

    def _take_control_response(self, event: dict) -> bool:
        """A control_response belongs to whoever is waiting on its request_id, not the page.

        Returns True when it was claimed, so the reader does not emit it: the transcript is what
        was said, and protocol acknowledgements are not part of that.
        """
        if event.get("type") != "control_response":
            return False
        request_id = str(event.get("request_id", "") or event.get("response", {}).get("request_id", ""))
        waiting = self._control.pop(request_id, None)
        if waiting is not None and not waiting.done():
            response = event.get("response")
            waiting.set_result(response if isinstance(response, dict) else {"subtype": "success"})
        elif waiting is None:
            # Nobody asked for this, or asked and forgot. It used to vanish here without a trace.
            log.warning("control_response for an unknown request %r: %s", request_id, event.get("response"))
        return True

    async def stop(self) -> None:
        """End the session; the child is asked first and killed if it will not go."""
        process, self._process = self._process, None
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        # Anyone waiting on a control response is waiting on a process that is going away, and
        # the reader that would have woken them is already cancelled. Left alone they wait out
        # the full timeout for an answer that can no longer arrive.
        self._release_control("the session was stopped")
        if process is None or process.returncode is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            await asyncio.wait_for(process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError, ConnectionResetError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for waiting in list(self.pending.values()):
            if waiting.answer is not None and not waiting.answer.done():
                waiting.answer.set_result(False)
        self.pending.clear()
        self.seen_waiting = 0
        self._emit({"type": "stopped"})

    # ---- reading the child ------------------------------------------------------------

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                line = await _read_line(process.stdout)
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                if text == REPLAY_DONE:
                    # Live from here (D179): idle now means the prompts that waited can go.
                    self._replaying = False
                    self._flush_soon()
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = {"type": "text", "text": text}
                # A brokered line knows when it was said, which matters once it is replayed.
                times = getattr(process, "times", None)
                if times and isinstance(event, dict):
                    event.setdefault("at", times.popleft())
                self._note_session_id(event)
                if isinstance(event, dict):
                    self._note_commands(event)
                if self._take_control_response(event):
                    continue
                self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken pipe must not take the proxy with it
            log.warning("session reader stopped: %s", exc)
        finally:
            if process.returncode is not None:
                self._emit({"type": "exited", "code": process.returncode})

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            message = line.decode("utf-8", errors="replace").strip()
            if message:
                self.last_error = message
                log.warning("session: %s", message)

    def _note_commands(self, event: dict[str, Any]) -> None:
        """What the CLI says it accepts after a slash, from its `init` event (feature 84)."""
        if event.get("type") == "system" and event.get("subtype") == "init":
            names = event.get("slash_commands")
            if isinstance(names, list):
                self.slash_commands = [str(name) for name in names if isinstance(name, str)]

    def _note_session_id(self, event: dict[str, Any]) -> None:
        found = event.get("session_id") or (event.get("message") or {}).get("session_id")
        if isinstance(found, str) and found:
            if found != self.session_id and self.sessions_dir is not None:
                # So the picker lists it: its transcript is tagged like a robot's (sdk-cli).
                from .history import note_ours
                try:
                    note_ours(self.sessions_dir, found)
                except OSError as exc:
                    log.warning("could not note conversation %s: %s", found, exc)
            self.session_id = found

    def _emit(self, event: dict[str, Any]) -> None:
        self._sequence += 1
        # An event that already knows when it happened keeps it - a replayed message was said
        # when the transcript says, not now (feature 66).
        event = {**event, "seq": self._sequence, "at": event.get("at") or time.time()}
        # What runs in the background (D202): the CLI's own live list. A turn that ends with one running
        # is not "finished" - its result starts the agent again - so the alert waits for the list to empty.
        if event.get("type") == "system" and event.get("subtype") == "background_tasks_changed" \
                and isinstance(event.get("tasks"), list) and not self._replaying:
            self.background = len(event["tasks"])
        # The CLI's `result` closes a turn; an exit ends one without finishing it.
        if event.get("type") == "result":
            self.turns += 1
            self.busy = False
            if not self._replaying:
                self._flush_soon()             # the next waiting prompt, if any (D176)
                if self.on_reply is not None:
                    self.on_reply(self)        # a new conversation's title (E3 part 10)
        elif event.get("type") in ("exited", "stopped"):
            self.busy = False
        self.doing = doing_after(self.doing, event)
        # Everything is broadcast; only what the page draws or reads back is kept (KEPT_EVENTS).
        if kept(event):
            if len(self.events) == self.events.maxlen:
                self._dropped_upto = self.events[0].get("seq", 0)    # about to fall off (D140)
            self.events.append(slim(event))
        for queue in list(self._watchers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ---- watching ---------------------------------------------------------------------

    def watch(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._watchers.add(queue)
        return queue

    def unwatch(self, queue: asyncio.Queue) -> None:
        self._watchers.discard(queue)

    # ---- permissions -------------------------------------------------------------------

    async def ask_permission(self, request_id: str, tool: str, detail: dict[str, Any]) -> Any:
        """Called by the approval host: park the request and wait for someone to answer.

        For as long as it takes, by default (the user, 2026-10-01): Claude Code in VS Code does not
        give up on a question, and five minutes was not enough to write an answer under Other.
        A positive approve_timeout_s still sets a deadline for whoever wants one."""
        loop = asyncio.get_running_loop()
        waiting = Pending(id=request_id, tool=tool, detail=detail, answer=loop.create_future())
        self.pending[request_id] = waiting
        self._emit({"type": "permission", "id": request_id, "tool": tool, "detail": detail})
        try:
            if not self.approve_timeout_s or self.approve_timeout_s <= 0:
                return await waiting.answer
            return await asyncio.wait_for(waiting.answer, timeout=self.approve_timeout_s)
        except asyncio.TimeoutError:
            self._emit({"type": "permission_timeout", "id": request_id, "tool": tool})
            return False
        finally:
            self.pending.pop(request_id, None)
            if not self.pending:
                self.seen_waiting = 0       # the next question is new to every page (D200)

    def answer_permission(self, request_id: str, allow: bool, answers: Any = None) -> bool:
        """Answer a waiting request; False when there is nothing by that id.

        `answers` is for AskUserQuestion (D58): the choices, keyed by question text, which go back
        to Claude Code as the question's answers rather than as a permission. Checked against the
        question it answers, so a page cannot answer something that was not asked."""
        waiting = self.pending.get(request_id)
        if waiting is None or waiting.answer is None or waiting.answer.done():
            return False
        if answers is not None:
            asked = {str(q.get("question", "")) for q in (waiting.detail or {}).get("questions") or []
                     if isinstance(q, dict)}
            if (not isinstance(answers, dict) or not answers
                    or not all(isinstance(k, str) and k in asked and isinstance(v, str) and len(v) <= 2000
                               for k, v in answers.items())):
                raise ValueError("answers must name the questions asked, each with a text answer")
            waiting.answer.set_result({"allow": bool(allow), "answers": dict(answers)})
        else:
            waiting.answer.set_result(bool(allow))
        answered: dict[str, Any] = {"type": "permission_answered", "id": request_id, "allow": bool(allow)}
        if answers is not None:
            # Kept so the transcript can show what was chosen, not only that it was (D80).
            answered["answers"] = dict(answers)
        self._emit(answered)
        return True

    def record_late_answer(self, request_id: str, allow: bool, answers: Any) -> None:
        """An answer to a question that had already closed (the user, 2026-10-01): Claude Code
        has moved on and cannot be given it, but what was written is kept in the transcript
        instead of being refused and lost. Shape-checked like a live answer; the question itself
        is gone, so which questions it names cannot be."""
        if (not isinstance(answers, dict) or not answers
                or not all(isinstance(k, str) and isinstance(v, str) and len(k) <= 2000 and len(v) <= 2000
                           for k, v in answers.items())):
            raise ValueError("answers must be question text, each with a text answer")
        self._emit({"type": "permission_answered", "id": request_id, "allow": bool(allow),
                    "answers": dict(answers), "late": True})


def work_in_progress(session: Any) -> str:
    """Why this running session cannot change conversation now, or "" when it can (item J): a turn in
    progress, a prompt queued, or a permission question waiting for an answer."""
    if session.busy:
        return "a turn is in progress - let it finish, or stop it, to switch conversation"
    if session.queued:
        return "a prompt is queued - let it go, or cancel it, to switch conversation"
    if session.pending:
        return "a question is waiting for an answer - answer it to switch conversation"
    return ""


def _agent_history(method: Callable) -> Callable:
    """A SessionHost method that reads a profile's conversations, run reading them where that profile's
    agent keeps them (item K): Claude Code's own folder, or the bridge's for Copilot and Codex."""
    @functools.wraps(method)
    def wrapper(self: "SessionHost", name: str = "", *args: Any, **kwargs: Any) -> Any:
        try:
            profile = self.profile(name)
        except SessionError:
            return method(self, name, *args, **kwargs)
        with self.history_of(profile):
            return method(self, name, *args, **kwargs)
    return wrapper


class SessionHost:
    """Every profile, and the sessions running for them.

    Work is organised by engagement, so the sessions are too: one per profile, several at once,
    each with its own process, transcript, permission queue and approval token. The name a person
    reads is the profile's (ACME); the client id behind it is what the filter uses.
    """

    def __init__(self, claude: str = "claude", base_url: str = "", profiles: list[Profile] | None = None,
                 permission_mode: str = "manual", approve_timeout_s: float = 0,
                 approve_url: str = "", max_running: int = 4, sessions_dir: Path | None = None,
                 thinking_display: str = "summarized") -> None:
        self.sessions_dir = Path(sessions_dir) if sessions_dir else None
        self.thinking_display = thinking_display
        self.claude = resolve_cli(claude or "claude")
        self.base_url = base_url
        self.permission_mode = permission_mode
        self.approve_timeout_s = approve_timeout_s
        self.approve_url = approve_url
        self.max_running = max(0, int(max_running or 0))     # 0 = no cap: the machine is the limit
        self.auto_titles = True       # session.auto_titles (item E): set by the proxy from the config
        # Another AI service's filtering, made ready by the proxy before such a session starts (item K):
        # async (agent) -> the environment its process needs - the forward proxy, its certificate.
        self.prepare_agent: Any = None
        # Where agents other than Claude Code file their conversations: under the proxy's state folder.
        self.agents_dir: Path | None = None
        self.profiles: list[Profile] = list(profiles or [])
        self.sessions: dict[str, Session] = {}
        # What the tree needs from outside the host, set by whoever owns it: the allowed folder
        # locations (D99 - a workspace file's folders are shown only inside them). The default
        # leaves the tree as the profile's own folders, which is what a host with no owner shows.
        self.allowed_roots: Callable[[], list[Path]] = lambda: []

    # ---- profiles ---------------------------------------------------------------------

    def profile(self, name: str) -> Profile:
        """The profile by name, or the only one when nothing is named."""
        if not name:
            if len(self.profiles) == 1:
                return self.profiles[0]
            if not self.profiles:
                raise SessionError("no session profiles are configured (session.profiles)")
            running = [n for n, s in self.sessions.items() if s.running]
            if len(running) == 1:
                return self.profile(running[0])
            raise SessionError("say which session: " + ", ".join(p.name for p in self.profiles))
        for profile in self.profiles:
            if profile.name.lower() == name.lower():
                return profile
        raise SessionError(f"no profile called {name}")

    def available(self) -> bool:
        """True when the Claude Code CLI is on this machine."""
        return bool(shutil.which(self.claude) or Path(self.claude).exists())

    def _session(self, name: str, create: bool = False) -> Session:
        profile = self.profile(name)
        existing = self.sessions.get(profile.name)
        if existing is not None:
            return existing
        if not create:
            raise SessionError(f"no session is running for {profile.name}")
        # Its own base URL, so every request it makes says which engagement it belongs to. That
        # is for the evidence log, not for the filter: what is replaced is the same in every
        # session.
        base = self.base_url
        if base:
            scheme, _, rest = base.partition("://")
            host, _, path = rest.partition("/")
            # Percent-encoded, because a profile name is what a person typed - "North Wind
            # Traders", not a slug. Interpolated raw, the space made the address invalid, the
            # request never matched /session/{profile}/... and arrived on the generic route
            # instead. Nothing errored: the proxy simply stopped knowing whose traffic it was,
            # so restored names went unmarked and attribution had no client to land on (D59).
            base = f"{scheme}://{host}/session/{quote(profile.name, safe='')}/{path}"
        # The home folder first, so a session with no folder chosen runs there (D99).
        session = Session(claude=self.claude, base_url=base, projects=self.run_folders(profile),
                          permission_mode=self.permission_mode, approve_timeout_s=self.approve_timeout_s,
                          approve_url=self.approve_url, profile=profile, sessions_dir=self.sessions_dir,
                          thinking_display=self.thinking_display)
        self.sessions[profile.name] = session
        return session

    # ---- surviving a proxy restart (DECISIONS 49) ----------------------------------------

    async def reattach_all(self) -> list[str]:
        """Find every session a previous proxy left running in its broker and pick it up. Returns
        the profile names re-attached. A broker whose Claude Code has exited, or that no longer
        answers, is left alone; one whose profile is gone is left running and logged."""
        if self.sessions_dir is None or not self.sessions_dir.is_dir():
            return []
        found: list[str] = []
        for folder in sorted(self.sessions_dir.iterdir()):
            try:
                published = json.loads((folder / "broker.json").read_text(encoding="utf-8"))
                launch = json.loads((folder / "launch.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if published.get("exit_code") is not None:
                continue
            name = str(launch.get("profile", ""))
            try:
                session = self._session(name, create=True)
            except SessionError:
                log.warning("a running session for profile %r has no profile any more; left running in %s",
                            name, folder)
                continue
            if session.running:
                continue
            session.transcript_folders = self.conversation_folders(session.profile)
            try:
                await session.reattach(folder, launch, published)
            except Exception as exc:  # noqa: BLE001 - a broker that has gone is not a proxy failure
                log.info("session broker in %s did not answer (%s); not re-attached", folder, exc)
                continue
            log.info("re-attached the running %s session (Claude Code pid %s)", name, published.get("cli_pid"))
            found.append(name)
        return found

    async def detach_all(self) -> None:
        """This proxy is stopping: let go of every brokered session without ending any."""
        for session in list(self.sessions.values()):
            await session.detach()

    # ---- what the UI asks for ---------------------------------------------------------

    def state(self, name: str = "", parts: list | None = None, since: int | None = None,
              epoch: str = "") -> dict[str, Any]:
        """Every profile, the state of each session, and which one the page is looking at.

        `running` is the list of names, because with several sessions the question is which ones.
        The selected session's own view is merged in at the top level so a page that only ever
        looks at one session reads the same fields it always did.
        """
        running = [n for n, s in self.sessions.items() if s.running]
        selected = ""
        try:
            selected = self.profile(name).name
        except SessionError:
            selected = running[0] if running else (self.profiles[0].name if self.profiles else "")
        clock = time.perf_counter()
        # The one the page shows, if stopped, brought up to date with its conversation's transcript
        # (E3): it may have gone on outside the page since it last ran here.
        shown = self.sessions.get(selected)
        if shown is not None and not shown.running:
            shown.catch_up([str(p) for p in self._profile_projects(selected)])
        sessions = {n: s.state() for n, s in self.sessions.items()}
        if parts is not None:                    # Diagnostics (feature 91): what this answer cost
            parts.append(("sessions", (time.perf_counter() - clock) * 1000))
        current_events = (sessions.get(selected) or {}).get("events", [])
        # Only what the page has not seen (D140): it says the last event number it holds and which
        # buffer that was; the same buffer with nothing dropped since answers with the rest only.
        delta = False
        chosen = self.sessions.get(selected)
        if since is not None and chosen is not None and epoch and epoch == chosen.epoch:
            newer = chosen.events_since(since)
            if newer is not None:
                current_events, delta = newer, True
        # One buffer in the answer, the selected session's, once - at the top level (D111). Every
        # session's went out, the selected one twice, on every five-second poll: 17 MB. The page
        # draws only the session it shows; another session's waiting questions are in `pending`.
        for state in sessions.values():
            state["events"] = []
        # A profile nobody has started yet still has folders to show and a transcript to fill, so
        # the page gets the same shape whether or not a process is behind it.
        idle: dict[str, Any] = {"name": selected, "events": [], "pending": [], "project": "",
                                "session_id": "", "started_at": 0.0, "error": "",
                                "projects": [str(p) for p in self._profile_projects(selected)]}
        current = sessions.get(selected) or idle
        # Anything the host answers for itself stays the host's: one session's flags must not
        # stand in for every session's.
        host_owned = ("running", "available", "approvals", "permission_mode", "profiles", "sessions")
        clock = time.perf_counter()
        profiles = [{**p.as_dict(), "tree": self.tree_of(p), "home": self.home_of(p)} for p in self.profiles]
        if parts is not None:
            parts.append(("profiles", (time.perf_counter() - clock) * 1000))
        return {
            "profiles": profiles,
            "sessions": sessions,
            "selected": selected,
            "running": running,
            "max_running": self.max_running,
            "available": self.available(),
            "approvals": bool(self.approve_url),
            "permission_mode": self.permission_mode,
            **{k: v for k, v in current.items() if k not in host_owned},
            "events": current_events,
            "delta": delta,
            # How far this answer covers: the page asks from here next time. Not the newest event
            # it holds - the stream adds only what it draws, so a notice can be missing below it.
            "through": (chosen.events[-1].get("seq", 0) if chosen is not None and chosen.events else 0),
        }

    def workspaces_of(self, profile: Profile) -> list[str]:
        """The profile's workspace files; for one saved before D99, a workspace file found directly
        inside one of its folders, as the old lookup did - so nothing changes for it."""
        from .workspace import implicit_workspaces

        if profile.workspaces:
            return [str(w) for w in profile.workspaces]
        return implicit_workspaces([str(p) for p in profile.projects])

    def tree_of(self, profile: Profile) -> list[str]:
        """The folders a profile's file tree shows (D99): every folder of every workspace file,
        then the manual folders, each once. Best effort - a workspace file must never cost the page."""
        from .workspace import profile_tree

        folders = [str(p) for p in profile.projects]
        try:
            return profile_tree(self.workspaces_of(profile), folders, self.allowed_roots())
        except Exception as exc:  # noqa: BLE001 - a hint, never a failure
            log.debug("tree roots for %s: %s", profile.name, exc)
            return folders

    def home_of(self, profile: Profile) -> dict[str, Any]:
        """Where this profile's work is homed (D99): derived from the first workspace file, or the
        user's override. {"folder", "how", "derived", "overridden"}."""
        from .workspace import home_of

        try:
            return home_of(self.workspaces_of(profile), [str(p) for p in profile.projects], profile.home_override)
        except Exception as exc:  # noqa: BLE001 - the page says it does not know, never fails
            log.debug("home of %s: %s", profile.name, exc)
            first = str(profile.projects[0]) if profile.projects else ""
            return {"folder": first, "how": "first folder of the profile", "derived": first, "overridden": False}

    def run_folders(self, profile: Profile) -> list[Path]:
        """The folders a session may run in: the home folder first (the default), then the rest of
        the tree. Nowhere else."""
        from .workspace import _same

        from .folders import is_usable

        # The home can come from a workspace file (its first folder, or its terminal cwd), which
        # nothing else holds to the allowed folders. Since every session starts there and the
        # folders all go to Claude Code (DECISIONS 64), each is checked here: inside the allowed
        # roots, or one of the profile's own folders, which were checked when it was saved
        # (security review of v0.9.155, 2026-10-06).
        roots = self.allowed_roots()

        def allowed(folder: str | Path) -> bool:
            return bool(roots and is_usable(Path(folder), roots)) or any(_same(folder, p) for p in profile.projects)

        home = self.home_of(profile).get("folder") or ""
        folders = [Path(f) for f in self.tree_of(profile) if allowed(f)]
        if home and Path(home).is_dir() and allowed(home):
            folders = [Path(home)] + [f for f in folders if not _same(f, home)]
        return folders or list(profile.projects)

    def conversation_folders(self, profile: Profile | None) -> list[str]:
        """Where this profile's conversations may be filed, home first: the home, its earlier homes,
        then every folder of its tree (DECISIONS 64). A conversation stays in the folder it began
        in, whatever folder resumes it - checked 2026-10-06 - so all of these are read."""
        from .workspace import _same

        if profile is None:
            return []
        from .folders import is_usable

        home = self.home_of(profile).get("folder") or ""
        # An earlier home is read only while it is inside the allowed folders, as every other
        # folder of a profile is: the list is the server's own, but the roots can narrow since.
        roots = self.allowed_roots()
        earlier = [h for h in profile.homes_before if roots and is_usable(Path(h), roots)]
        # The share too: it is always passed to Claude Code, and a hand-added copy of it is dropped
        # on save (D115) - one client's conversations are filed under its share, which stopped being
        # its home before earlier homes were recorded, and would otherwise have left Continue.
        share = [str(profile.share)] if profile.share else []
        found: list[str] = []
        for folder in [home, *earlier, *self.tree_of(profile), *[str(p) for p in profile.projects], *share]:
            if str(folder).strip() and not any(_same(folder, known) for known in found):
                found.append(str(folder))
        return found

    def _profile_projects(self, name: str) -> list[Path]:
        """The folders a profile can run in, whether or not it has ever been started - the home
        folder first, so the page's Folder choice defaults to it (D99)."""
        try:
            return self.run_folders(self.profile(name))
        except SessionError:
            return []

    async def start(self, name: str = "", project: str = "", resume: str = "", model: str = "",
                    title: str = "") -> dict[str, Any]:
        profile = self.profile(name)
        existing = self.sessions.get(profile.name)
        if existing is not None and existing.running:
            # Start on the conversation that is already running reconnects to it (DECISIONS 49):
            # the page shows conversations, not processes, and a second Claude Code on one
            # conversation is how it forked.
            if not resume or resume == existing.session_id:
                return existing.state()
            raise SessionError(f"{profile.name} is already running a different conversation; stop it first")
        running = sum(1 for s in self.sessions.values() if s.running)
        if self.max_running and running >= self.max_running:      # 0 = no cap (2026-10-16)
            raise SessionError(
                f"{running} sessions are already running and the limit is {self.max_running} "
                "(session.max_running - Settings, Session page card); stop one first")
        session = self._session(profile.name, create=True)
        # A session object outlives its process, so it may still hold the profile as it was at its
        # first start. Every start uses the profile as it is now: an edited brief, folder list or
        # share applies from the next start, as the page says it will.
        session.profile = profile
        await self._ready_agent(session, profile)
        # One starting folder per profile, its home (DECISIONS 64): every conversation is filed in
        # one place. The rest of the profile's folders go to Claude Code with --add-dir. The page's
        # `project` is no longer a choice and is ignored.
        folders = self.run_folders(profile)
        session.projects = folders
        session.transcript_folders = self.conversation_folders(profile)
        home = folders[0] if folders else None
        # No co-author lines from a folder new since the last start (feature 117, decision 8).
        if home is not None:
            from . import codetab_settings
            if codetab_settings.applies():
                await asyncio.to_thread(codetab_settings.apply_attribution, [str(home)])
        self.plan_title(session, title, resume)          # its title: typed, or written by the model (E3)
        return await session.start(project=str(home) if home else project, resume=resume,
                                   model=model or profile.model)

    async def switch(self, name: str = "", resume: str = "", title: str = "") -> dict[str, Any]:
        """Another conversation in a running session (item J): the current one stopped - titled as a
        stop titles it - and the chosen one started, or a new one when `resume` is empty. Refused
        while there is work in progress. Not running: an ordinary start."""
        profile = self.profile(name)
        existing = self.sessions.get(profile.name)
        if existing is None or not existing.running:
            return await self.start(profile.name, resume=resume, title=title)
        if resume and resume == existing.session_id:
            return existing.state()
        why = work_in_progress(existing)
        if why:
            raise SessionError(f"{profile.name}: {why}")
        await self.stop(profile.name)
        return await self.start(profile.name, resume=resume, title=title)

    def agent_root(self, profile: Profile | None) -> Path | None:
        """Where this profile's agent files its conversations: None for Claude Code (its own folder)."""
        from .agents import agent, transcripts_root

        if profile is None or self.agents_dir is None:
            return None
        return transcripts_root(self.agents_dir, agent(profile.agent))

    def history_of(self, profile: Profile | None):
        """Read this profile's conversations where its agent keeps them (item K): a `with` block."""
        from .history import using_root

        return using_root(self.agent_root(profile))

    async def _ready_agent(self, session: "Session", profile: Profile) -> None:
        """For another AI service (item K): its command found, its filtering made ready by the proxy,
        and the bridge's environment set. A session whose traffic cannot be filtered does not start."""
        from .agents import agent, command_for, protocol_args

        which = agent(profile.agent)
        session.agent_env = {}
        session.transcripts_root = None
        if which.id == "claude":
            return
        command = command_for(which)
        if not command:
            raise SessionError(f"{which.label} is not installed on this machine - install it from the AI tools card "
                               "(Settings) and start again")
        if self.prepare_agent is None:
            raise SessionError(f"{which.label} cannot be filtered here, so it is not started")
        environment = await self.prepare_agent(which)
        root = self.agent_root(profile)
        session.transcripts_root = root
        session.agent_env = {**environment,
                             "AICLEANER_AGENT_COMMAND": json.dumps([*command, *protocol_args(which)]),
                             "AICLEANER_AGENT_TRANSCRIPTS": str(root) if root else "",
                             "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}

    def _found_transcript(self, profile: Profile, conv_id: str) -> Path | None:
        from . import history

        folder = history.holding(self.conversation_folders(profile), conv_id)
        return history._transcript(folder, conv_id, None) if folder else None

    @_agent_history
    def files_of_conversation(self, name: str, conv_id: str) -> list[str]:
        """The files the conversation made that a move could take with it (J3) - listed before it does."""
        from . import convmove

        profile = self.profile(name)
        transcript = self._found_transcript(profile, conv_id)
        if transcript is None:
            raise SessionError(f"that conversation is not one of {profile.name}'s")
        return [str(p) for p, _root in convmove.written_files(transcript, self.run_folders(profile) + (
            [profile.share] if profile.share else []))]

    @_agent_history
    def move_conversation(self, name: str, conv_id: str, to: str, files: bool = False) -> dict[str, Any]:
        """A conversation filed under another project session's home (J3), and with `files` the files it
        made moved there too. Refused while that conversation is running here."""
        from . import convmove, history

        source = self.profile(name)
        target = self.profile(to)
        if source.name == target.name:
            raise SessionError("choose another project session to move it to")
        running = self.sessions.get(source.name)
        if running is not None and running.running and running.session_id == conv_id:
            raise SessionError("that conversation is running - stop it, then move it")
        transcript = self._found_transcript(source, conv_id)
        if transcript is None:
            raise SessionError(f"that conversation is not one of {source.name}'s")
        home_text = self.home_of(target).get("folder") or ""
        folders = self.run_folders(target)
        home = Path(home_text) if home_text else (folders[0] if folders else None)
        if home is None:
            raise SessionError(f"{target.name} has no home folder to move it to")
        moved_files: list[str] = []
        left_files: list[str] = []
        if files:
            found = convmove.written_files(transcript, self.run_folders(source) + ([source.share] if source.share else []))
            moved_files, left_files = convmove.move_files(found, home)
        done = history.refile(conv_id, home, self.conversation_folders(source))
        if done is None and not history.holding([str(home)], conv_id):
            raise SessionError(f"the conversation could not be filed under {home} (one with its id may be there already)")
        log.info("conversation %s moved from %s to %s (%d file(s) moved, %d left)", conv_id, source.name,
                 target.name, len(moved_files), len(left_files))
        return {"ok": True, "to": target.name, "home": str(home), "files_moved": moved_files, "files_left": left_files}

    async def prompt(self, text: str, name: str = "", origin: str = "") -> str:
        session = self._session(name)
        if origin:
            session.origin = origin
        return await session.prompt(text)

    async def send_queued_now(self, item_id: str, name: str = "") -> str:
        return await self._session(name).send_queued_now(item_id)

    async def cancel_queued(self, item_id: str, name: str = "") -> None:
        await self._session(name).cancel_queued(item_id)

    def take_waiting(self, session_id: str) -> list[str]:
        """Feature 119: the waiting prompts of the running session with this conversation id, or
        nothing - the hook asks for every Claude Code session on the machine, VS Code's included."""
        if not session_id:
            return []
        for session in self.sessions.values():
            if session.running and session.session_id == session_id:
                return session.take_waiting()
        return []

    def origin_of(self, session_id: str) -> str:
        """Where the running session with this conversation id is being typed from (origin.py)."""
        for session in self.sessions.values():
            if session.running and session.session_id == session_id:
                return session.origin
        return ""

    async def stop(self, name: str = "") -> None:
        session = self._session(name)
        conv_id = session.session_id
        await session.stop()
        # The conversation that just ended gets a title when it grew (item E, session.auto_titles).
        if session.profile is not None:
            self.title_after_stop(session.profile.name, conv_id)

    async def interrupt(self, name: str = "", cancel_queued: bool = True) -> str:
        return await self._session(name).interrupt(cancel_queued=cancel_queued)

    async def set_permission_mode(self, name: str = "", mode: str = "") -> None:
        await self._session(name).set_permission_mode(mode)

    async def stop_all(self) -> None:
        for session in list(self.sessions.values()):
            if session.running:
                try:
                    await session.stop()
                except SessionError:
                    pass

    @_agent_history
    def earlier(self, name: str, before: float, limit: int = EARLIER_LIMIT) -> dict[str, Any]:
        """The conversation from before what the page already shows (feature 82): up to `limit` of
        the transcript's events said before `before`, read from whichever of the profile's folders
        holds it. A one-off for "Show earlier" - nothing here is kept, so the scrollable history
        goes back to the buffer as new events arrive. Only the session's own conversation is read."""
        from .history import holding, replay

        profile = self.profile(name)
        session = self.sessions.get(profile.name)
        conversation = session.session_id if session is not None else ""
        if not conversation:
            return {"events": [], "more": False}
        folder = holding(self.conversation_folders(profile), conversation)
        if not folder:
            return {"events": [], "more": False}
        wanted = max(1, min(int(limit), EARLIER_LIMIT))
        events = replay(Path(folder), conversation, wanted + 1, None, before or None)
        more = len(events) > wanted
        return {"events": events[-wanted:], "more": more}

    @_agent_history
    def past(self, name: str = "", limit: int = 25, include_robots: bool = False) -> list[dict[str, Any]]:
        """Conversations already held in this profile's folders, newest first.

        The page names a profile, never a path, so nothing a browser sends can point this at
        somewhere else on the machine - the folders come from the profile, as they do for the
        file tree. The id it hands back still goes through check_resume on the way in.
        """
        from .history import conversations, our_conversations

        profile = self.profile(name)
        ours = our_conversations(self.sessions_dir)
        # Every folder a conversation of this profile may be filed in, home first (DECISIONS 64).
        # They were split into the profile's folders and "not in this profile yet" (feature 50),
        # with an offer to add the folder: a session runs in its home now and every folder is
        # passed to Claude Code, so any of these conversations can be continued as it is.
        from .titles import load as load_titles
        return conversations(self.conversation_folders(profile), limit=limit,
                             include_robots=include_robots, ours=ours, labels=load_titles(self.state_dir()))

    @_agent_history
    def search_conversations(self, name: str, query: str, everywhere: bool = False) -> dict[str, Any]:
        """Conversations of this profile (or of every profile) ranked for a topic (2026-10-16).

        As past(): the page names a profile, never a path, so only folders a profile already has are
        read."""
        from .convsearch import Cache, search, transcripts
        from .history import our_conversations, transcript_root

        clock = time.perf_counter()
        profiles = self.profiles if everywhere else [self.profile(name)]
        folders: list[str] = []
        for profile in profiles:
            for folder in self.conversation_folders(profile):
                if folder.lower() not in {f.lower() for f in folders}:
                    folders.append(folder)
        items = transcripts(folders, transcript_root(), our_conversations(self.sessions_dir))
        cache = Cache(self.sessions_dir.parent / "search" if self.sessions_dir else None)
        results = search(items, cache, query)
        from .titles import load as load_titles
        labels = load_titles(self.state_dir())
        for row in results:                                  # titles (item E) on the cards too
            row["label"] = str((labels.get(row["id"].lower()) or {}).get("title") or "")
        if everywhere:
            cache.prune(item.path for item in items)
        return {"results": results, "searched": len(items), "read": cache.read,
                "ms": round((time.perf_counter() - clock) * 1000)}

    def state_dir(self) -> Path | None:
        return self.sessions_dir.parent if self.sessions_dir else None

    def bin_root(self) -> Path:
        return (self.sessions_dir.parent if self.sessions_dir else Path.home() / ".aicleaner") / "conversation-bin"

    # ---- conversation titles (2026-10-16, item E) ---------------------------------------------------

    def _transcript_of(self, name: str, conv_id: str) -> Path:
        from .convbin import _find
        from .history import UUID, transcript_root
        if not UUID.match(conv_id or ""):
            raise SessionError("not a conversation id")
        path = _find(self.conversation_folders(self.profile(name)), conv_id, transcript_root())
        if path is None:
            raise SessionError("no such conversation in this profile's folders")
        return path

    @_agent_history
    def rename_conversation(self, name: str, conv_id: str, title: str) -> dict[str, Any]:
        """The person's title, which always wins; empty puts the model's (or the first prompt) back."""
        from .titles import set_title
        self._transcript_of(name, conv_id)
        if self.state_dir() is None:
            raise SessionError("titles need a state folder")
        return {"label": set_title(self.state_dir(), conv_id, title, "person")}

    @_agent_history
    def retitle_conversation(self, name: str, conv_id: str, only_if_grown: bool = False) -> dict[str, Any]:
        from .titles import retitle
        if self.state_dir() is None:
            raise SessionError("titles need a state folder")
        try:
            return {"label": retitle(self.state_dir(), self._transcript_of(name, conv_id), self.claude, self.base_url,
                                     only_if_grown=only_if_grown)}
        except RuntimeError as exc:
            raise SessionError(str(exc)) from exc

    def plan_title(self, session: "Session", title: str = "", resume: str = "") -> None:
        """How a conversation started from the page gets its title (E3 part 10, feedback #3; Brad
        2026-10-19): a Session Name typed at the start becomes the person's title once it has an id (its
        first reply); left blank, the model writes one after the first reply and again after the fifth.
        A resumed conversation keeps whatever title it has."""
        name = str(title or "").strip()[:200]
        session.title_plan = None if resume else ({"name": name, "replies": 0} if name else {"auto": True, "replies": 0})
        session.on_reply = self._title_after_reply

    def _title_after_reply(self, session: "Session") -> None:
        plan = session.title_plan
        if not plan or not session.session_id:
            return
        plan["replies"] += 1
        replies = plan["replies"]
        try:
            if plan.get("name"):
                self.rename_conversation(session.profile.name, session.session_id, plan["name"])
                session.title_plan = None
            elif replies in (1, 5):
                self.title_queue().add(session.profile.name, [session.session_id])
                if replies >= 5:
                    session.title_plan = None
        except SessionError as exc:          # no title is no reason to disturb the conversation
            log.info("no title for %s yet: %s", session.session_id, exc)

    def suggest_titles(self, name: str, every: bool = False, ids: list[str] | None = None) -> int:
        """Titles, in a background thread. With `ids` (E3, Brad 2026-10-19: the Suggest titles dialog's
        ticks): exactly those, titled or not - and only conversations this profile lists as its own, so
        a page cannot queue another client's by naming it. Without: the untitled ones Continue lists,
        the newest 25 or with "all" ticked every one (the user, 2026-10-17)."""
        from .titles import load
        listed = self.past(name, 5000 if (every or ids is not None) else 25)
        own = [row["id"] for group in listed if not group.get("outside") for row in group["sessions"]]
        if ids is not None:
            wanted = {str(i).lower() for i in ids}
            return self.title_queue().add(name, [conv for conv in own if conv.lower() in wanted])
        labels = load(self.state_dir())
        return self.title_queue().add(name, [conv for conv in own if conv.lower() not in labels])

    def title_queue(self) -> Any:
        """The background titles, four at a time, with the progress the page's toast shows (2026-10-17)."""
        if getattr(self, "_title_queue", None) is None:
            from .titles import TitleQueue

            def one(name: str, conv_id: str) -> None:
                try:
                    self.retitle_conversation(name, conv_id)
                except SessionError as exc:
                    log.info("no title for %s: %s", conv_id, exc)
                    raise
            self._title_queue = TitleQueue(one)
        return self._title_queue

    # ---- consolidating conversations (2026-10-16, item F) ---------------------------------------------

    def consolidate(self, name: str, ids: list[str]) -> str:
        from .consolidate import MAX_IDS, MIN_IDS, Jobs
        from .history import _head
        ids = list(dict.fromkeys(str(i) for i in ids))
        if not MIN_IDS <= len(ids) <= MAX_IDS:
            raise SessionError(f"choose {MIN_IDS} to {MAX_IDS} conversations")
        from .titles import load as load_titles
        labels = load_titles(self.state_dir())
        items = []
        for conv_id in ids:
            path = self._transcript_of(name, conv_id)
            label = (labels.get(conv_id.lower()) or {}).get("title")
            items.append((path, label or _head(path, (path.stat().st_size, path.stat().st_mtime))[0] or conv_id))
        if not hasattr(self, "_consolidations"):
            self._consolidations = Jobs()
        # The full summaries go to a file in the home, which the short brief names (2026-10-17).
        home = self.home_of(self.profile(name)).get("folder") or ""
        return self._consolidations.start(items, self.claude, self.base_url, home=Path(home) if home else None)

    def consolidation(self, job: str) -> dict[str, Any]:
        found = getattr(self, "_consolidations", None)
        state = found.get(job) if found else None
        if state is None:
            raise SessionError("no such consolidation")
        return dict(state)

    @_agent_history
    def title_after_stop(self, name: str, conv_id: str) -> None:
        """session.auto_titles: the conversation that just ended, if it grew, retitled in the background."""
        if not self.auto_titles or not conv_id:
            return
        import threading

        def work() -> None:
            try:
                self.retitle_conversation(name, conv_id, only_if_grown=True)
            except SessionError as exc:
                log.info("no title after stop for %s: %s", conv_id, exc)

        threading.Thread(target=work, name="aicleaner-title-on-stop", daemon=True).start()

    def delete_conversation(self, name: str, conv_id: str) -> dict[str, Any]:
        """Move a conversation to Recently deleted (2026-10-16, item D); refused while it runs."""
        from . import convbin
        from .convsearch import Cache
        from .history import _head, transcript_root

        profile = self.profile(name)
        running = [s.session_id for s in self.sessions.values() if s.running and s.session_id]
        root = transcript_root()
        path = convbin._find(self.conversation_folders(profile), conv_id, root)
        title = _head(path, (path.stat().st_size, path.stat().st_mtime))[0] if path else ""
        meta = convbin.delete(self.conversation_folders(profile), conv_id, root, self.bin_root(), profile.name,
                              title, running)
        if path is not None and self.sessions_dir:
            cached = Cache(self.sessions_dir.parent / "search")._file(path)   # its search cache goes too
            if cached is not None:
                cached.unlink(missing_ok=True)
        convbin.clean(self.bin_root())
        return meta

    def deleted_conversations(self, name: str) -> list[dict[str, Any]]:
        from . import convbin
        profile = self.profile(name)
        convbin.clean(self.bin_root())
        return convbin.deleted(self.bin_root(), profile.name)

    def restore_conversation(self, name: str, conv_id: str) -> dict[str, Any]:
        from . import convbin
        return convbin.restore(self.bin_root(), self.profile(name).name, conv_id)

    def purge_conversation(self, name: str, conv_id: str) -> dict[str, Any]:
        from . import convbin
        return convbin.purge(self.bin_root(), self.profile(name).name, conv_id)

    @_agent_history
    def agent_report(self, name: str, tool_id: str) -> dict[str, Any]:
        """A sub-agent's report, found by the Agent call's id in this profile's folders (2026-10-16)."""
        from .agentreport import report
        from .history import transcript_root

        profile = self.profile(name)
        session = self.sessions.get(profile.name)
        prefer = (session.session_id if session is not None else "") or ""
        return report(self.conversation_folders(profile), tool_id, transcript_root(), prefer)

    def rank_conversations(self, query: str, results: list[dict[str, Any]]) -> list[dict[str, str]]:
        """The model's order and reasons for results the page already holds."""
        from .convsearch import rank_with_model

        return rank_with_model(self.claude, self.base_url, query, results)

    def browse(self, name: str = "", root: str = "", path: str = "") -> dict:
        """The file tree of a profile's folders, whether or not its session is running."""
        return self._browser(name).browse(root=root, path=path)

    def _browser(self, name: str) -> "Session":
        """A browse-only Session over exactly the folders the file tree shows - for listing a folder
        and for reading a file (DECISIONS 50) alike.

        The tree shows the primary repo's workspace folders as well as the profile's (D72), so
        browsing permits those; a running session's own folders stay browsable too. Built from the
        profile as it is now, so a share set a moment ago appears straight away - the running
        process is never touched by a listing."""
        profile = self.profile(name)
        session = self.sessions.get(profile.name)
        folders = [Path(f) for f in self.tree_of(profile)]
        if session is not None and session.running:
            folders += [p for p in session.projects if all(str(p) != str(f) for f in folders)]
        return Session(claude=self.claude, projects=folders, profile=profile)

    def read_file(self, name: str, root: str, path: str) -> dict[str, Any]:
        return self._browser(name).read_file(root, path)

    def search(self, name: str, query: str, limit: int = SEARCH_LIMIT, more: bool = False,
               budget_s: float = SEARCH_BUDGET_S) -> dict[str, Any]:
        """Names in every folder the tree shows that contain `query` (feature 90). With `more`, the
        walk that last ran out of time for this session and query carries on where it stopped
        (D146) and answers with the results it finds from there; otherwise a fresh walk."""
        if not hasattr(self, "_search_walks"):
            self._search_walks: dict[str, SearchWalk] = {}
        profile = self.profile(name)
        needle = (query or "").strip().lower()
        walk = self._search_walks.get(profile.name)
        continued = bool(more and walk is not None and walk.needle == needle and not walk.done)
        if not continued:
            walk = self._browser(name).search_walk(query)
            self._search_walks[profile.name] = walk
        return walk.answer(query, walk.run(limit, budget_s), continued=continued)

    def write_file(self, name: str, root: str, path: str, text: str, read_modified: float) -> dict[str, Any]:
        return self._browser(name).write_file(root, path, text, read_modified)

    def locate_file(self, name: str, root: str, path: str) -> Path:
        return self._browser(name).locate_file(root, path)

    def drop_folder(self, name: str, root: str, path: str) -> Path:
        """The folder a drop copies into (item H2): through the tree's own allow-list, a folder, and
        reached through no link or junction (D171) - resolved, it is still inside its root."""
        _roots, chosen, target = self._browser(name)._locate(root, path)
        if not target.is_dir():
            raise SessionError(f"{target} is not a folder")
        if not _inside(target, chosen):
            raise SessionError(f"{path} leads through a link or junction; nothing is copied through one")
        return target

    def locate_items(self, name: str, items: list[dict[str, str]]) -> list[tuple[Path, Path]]:
        """(root, target) for each of many files or folders (feature 97's zip), every one through
        the tree's own allow-list - so a zip can never hold what a single download could not."""
        browser = self._browser(name)
        found = []
        for item in items:
            path = str(item.get("path", ""))
            _roots, chosen, target = browser._locate(str(item.get("root", "")), path)
            if not target.exists():
                raise SessionError(f"{target} is no longer there")
            # Named in the zip where it sits, not where a junction leads (D171); the check above is
            # what allowed it.
            found.append((chosen, target if _inside(target, chosen) else chosen / path))
        return found

    def stat_files(self, name: str, files: list[dict[str, str]]) -> list[dict[str, Any]]:
        browser = self._browser(name)
        return [browser.stat_file(str(f.get("root", "")), str(f.get("path", ""))) for f in files[:200]]

    def watch(self, name: str = ""):
        return self._session(name).watch()

    # ---- permissions ------------------------------------------------------------------

    def owner_of(self, request_id: str) -> Session:
        """Which session asked. The id is minted by the session that raised the request, so a
        permission can never be answered into the wrong transcript."""
        for session in self.sessions.values():
            if request_id in session.pending:
                return session
        raise SessionError("that request is not waiting for an answer")

    def answer_permission(self, request_id: str, allow: bool, answers: Any = None, name: str = "") -> str:
        """"answered", or "late" when a question had already closed and its answers were kept in
        the named session's transcript instead (the user, 2026-10-01). A late Allow or Deny has
        nothing worth keeping and is still refused."""
        try:
            owner = self.owner_of(request_id)
        except SessionError:
            if answers is None or not name:
                raise
            self._session(name).record_late_answer(request_id, allow, answers)
            return "late"
        owner.answer_permission(request_id, allow, answers)
        return "answered"

    def session_for_token(self, token: str) -> Session:
        """Which session an approval host is speaking for.

        The token is minted per session and never leaves the process it was given to, so it is the
        routing: a stale host, or one belonging to another session, cannot park a request in a
        transcript that is not its own.
        """
        for session in self.sessions.values():
            if session.approve_token and token and secrets_module.compare_digest(session.approve_token, token):
                return session
        raise SessionError("that token belongs to no running session")
