"""Feature 143 (spec 2026-10-23-f143-inline-suggestions-on-pause.md): code suggestions as you pause, from Claude
Haiku, through aicleaner.

A new `claude --print` for each suggestion took 7-10 seconds (measured 2026-10-23), nearly all of it starting
Claude Code. So one Claude Code process is kept warm and fed prompt after prompt as stream-json on stdin, with
extended thinking off: about 1-2 seconds a suggestion (thinking on made it 6-14). It runs with oneshot's isolation (an empty
folder, no settings files, no tools, no MCP, no skills, not saved) and a short system prompt of its own, through
aicleaner like every request - marked with a header so no footer, mark or origin note is added to it.

Its conversation grows with every suggestion (and the delay with it), so it is replaced after RECYCLE_AFTER
suggestions; it stops after IDLE_S without one. One suggestion runs at a time: a request that arrives while one
runs takes the place of any already waiting, and the one it replaced answers empty at once.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any

log = logging.getLogger("aicleaner.completion")

HEADER = "X-Aicleaner-Purpose"
PURPOSE = "completion"
RECYCLE_AFTER = 8
IDLE_S = 600
TIMEOUT_S = 25
BEFORE_CHARS = 3000
AFTER_CHARS = 1000
MAX_LINES = 30
ISOLATION = ["--tools", "", "--restricted", "--strict-mcp-config", "--no-session-persistence",
             "--disable-slash-commands"]
SYSTEM = ("You are a code completion engine inside a code editor. You are shown the code before and after the "
          "cursor. Reply with ONLY the code to insert at the cursor - no explanation, no code fences, no repeat of "
          "the code before or after it. Continue exactly from the cursor, matching the file's indentation and "
          "style. Prefer a short, complete step: the rest of the line, or a few lines that finish the block. If "
          "nothing useful fits, reply with an empty message.")
PROSE = re.compile(r"^(here|sure|this|the |i |i'|certainly|okay|ok[,.]|to |you |note|based on)", re.I)
FENCE = re.compile(r"^```[\w+-]*\s*\n?|\n?```\s*$")


def prompt_for(name: str, language: str, before: str, after: str) -> str:
    """What the worker is asked: the file, the code either side of the cursor (trimmed to whole lines at the far
    ends), and the request for the insertion only."""
    before = before[-BEFORE_CHARS:]
    after = after[:AFTER_CHARS]
    if len(before) == BEFORE_CHARS and "\n" in before:
        before = before.split("\n", 1)[1]
    if len(after) == AFTER_CHARS and "\n" in after:
        after = after.rsplit("\n", 1)[0]
    return (f"File: {name or 'untitled'} ({language or 'plain text'})\n\n"
            f"Code before the cursor:\n<before>\n{before}</before>\n\n"
            f"Code after the cursor:\n<after>{after}\n</after>\n\n"
            "Reply with only the code to insert at the cursor.")


def clean(answer: str, before: str, after: str) -> str:
    """The model's answer as text to insert: fences and prose off, what already follows the cursor trimmed from
    its end, at most MAX_LINES lines. Empty when it is not code."""
    text = (answer or "").replace("\r\n", "\n")
    text = FENCE.sub("", text.strip("\n"))
    if not text.strip() or PROSE.match(text.lstrip()):
        return ""
    # The model repeated the line it was continuing: keep only what is new.
    last_line = before.rsplit("\n", 1)[-1]
    if last_line.strip() and text.startswith(last_line):
        text = text[len(last_line):]
    # The line the file already has straight after the cursor, written again as the answer's last line. A whole
    # line only: a block's own closing brace ("... }") is not the function's "}" that follows.
    follow = after.lstrip("\n").split("\n", 1)[0].strip()
    lines = text.rstrip().split("\n")
    if follow and len(lines) > 1 and lines[-1].strip() == follow:
        text = "\n".join(lines[:-1]) + "\n"
    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        text = "\n".join(lines[:MAX_LINES])
    return text if text.strip() else ""


class Worker:
    """One warm Claude Code process."""

    def __init__(self, command: list[str], env: dict[str, str]) -> None:
        self.command = command
        self.env = env
        self.process: asyncio.subprocess.Process | None = None
        self.folder = ""
        self.turns = 0
        self.stderr_tail = ""
        self._drain: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        self.folder = tempfile.mkdtemp(prefix="aicleaner-complete-")
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.folder, env=self.env, limit=4 * 1024 * 1024)
        self._drain = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        with contextlib.suppress(Exception):
            async for line in self.process.stderr:
                self.stderr_tail = (self.stderr_tail + line.decode("utf-8", "replace"))[-2000:]

    async def ask(self, prompt: str, timeout: float = TIMEOUT_S) -> str:
        if not self.alive:
            raise RuntimeError(f"the completion worker is not running ({self.stderr_tail[-300:].strip()})")
        assert self.process is not None and self.process.stdin is not None and self.process.stdout is not None
        message = {"type": "user", "message": {"role": "user", "content": prompt}}
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        self.turns += 1

        async def answer() -> str:
            assert self.process is not None and self.process.stdout is not None
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise RuntimeError(f"the completion worker ended ({self.stderr_tail[-300:].strip()})")
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "result":
                    if event.get("is_error"):
                        raise RuntimeError(str(event.get("result") or "the model returned an error")[:300])
                    return str(event.get("result") or "")

        return await asyncio.wait_for(answer(), timeout)

    async def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(Exception):
                if self.process.stdin is not None:
                    self.process.stdin.close()
                await asyncio.wait_for(self.process.wait(), 3)
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
        if self._drain is not None:
            self._drain.cancel()
        if self.folder:
            shutil.rmtree(self.folder, ignore_errors=True)


class Completions:
    """The worker kept warm, one suggestion at a time, the latest request winning."""

    def __init__(self, claude: str, base_url: str, model: str = "haiku", recycle_after: int = RECYCLE_AFTER,
                 idle_s: float = IDLE_S) -> None:
        self.claude = claude
        self.base_url = base_url
        self.model = model
        self.recycle_after = recycle_after
        self.idle_s = idle_s
        self._worker: Worker | None = None
        self._spare: asyncio.Task[Worker] | None = None
        self._lock = asyncio.Lock()
        self._ticket = 0
        self._idle: asyncio.Task[None] | None = None
        self.answered = 0
        self.superseded = 0
        self.failed = 0
        self.last_error = ""
        self.last_ms = 0

    def command(self) -> list[str]:
        cmd = [self.claude, "--print", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", *ISOLATION, "--system-prompt", SYSTEM]
        return cmd + (["--model", self.model] if self.model else [])

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["ANTHROPIC_BASE_URL"] = self.base_url
        # The proxy knows the request by this: no footer, no marks, no origin note, labelled in the evidence log.
        env["ANTHROPIC_CUSTOM_HEADERS"] = f"{HEADER}: {PURPOSE}"
        # No extended thinking: measured 2026-10-23, it made each suggestion 6-14 s; without it 0.9-2 s.
        env["MAX_THINKING_TOKENS"] = "0"
        return env

    async def _fresh(self) -> Worker:
        worker = Worker(self.command(), self.env())
        await worker.start()
        return worker

    async def _current(self) -> Worker:
        if self._worker is not None and self._worker.alive:
            return self._worker
        if self._worker is not None:
            await self._worker.stop()
        if self._spare is not None:
            spare, self._spare = self._spare, None
            with contextlib.suppress(Exception):
                self._worker = await spare
                if self._worker.alive:
                    return self._worker
        self._worker = await self._fresh()
        return self._worker

    def _rest(self) -> None:
        """The idle timer restarted: the worker stops when no suggestion is asked for a while."""
        if self._idle is not None:
            self._idle.cancel()

        async def later() -> None:
            await asyncio.sleep(self.idle_s)
            await self.close()

        self._idle = asyncio.create_task(later())

    async def complete(self, prompt: str) -> str:
        """The model's answer to `prompt`, or '' when a newer request took its place."""
        self._ticket += 1
        mine = self._ticket
        async with self._lock:
            if mine != self._ticket:
                self.superseded += 1
                return ""
            started = time.monotonic()
            try:
                worker = await self._current()
                text = await worker.ask(prompt)
            except (RuntimeError, asyncio.TimeoutError, OSError) as exc:
                self.failed += 1
                self.last_error = str(exc) or type(exc).__name__
                log.warning("file editor suggestion failed: %s", self.last_error)
                if self._worker is not None:
                    await self._worker.stop()
                    self._worker = None
                return ""
            finally:
                self._rest()
            self.answered += 1
            self.last_ms = int((time.monotonic() - started) * 1000)
            if worker.turns >= self.recycle_after:
                # Replaced by a fresh one started now, so the next suggestion does not wait for it.
                old, self._worker = worker, None
                self._spare = asyncio.create_task(self._fresh())
                asyncio.create_task(old.stop())
            return text

    async def close(self) -> None:
        if self._idle is not None and self._idle is not asyncio.current_task():
            self._idle.cancel()
        if self._worker is not None:
            await self._worker.stop()
            self._worker = None
        if self._spare is not None:
            spare, self._spare = self._spare, None
            with contextlib.suppress(Exception):
                await (await spare).stop()

    def summary(self) -> dict[str, Any]:
        return {"answered": self.answered, "superseded": self.superseded, "failed": self.failed,
                "last_error": self.last_error, "last_ms": self.last_ms, "warm": bool(self._worker and self._worker.alive),
                "model": self.model}
