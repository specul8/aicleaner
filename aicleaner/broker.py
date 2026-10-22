"""A session broker: one Claude Code process, kept alive across proxy restarts (DECISIONS 49).

A Session-page session used to be a child of the proxy, talking to it through pipes. A proxy restart
- every deploy - left the child running headless with nobody reading it, and pressing Start again
began a second Claude Code on the same conversation. The user saw forked conversations and work
committed by a copy nobody could see.

Now the proxy starts this broker instead, detached from itself. The broker owns Claude Code's
stdin, stdout and stderr; numbers every line it sees and keeps them (in memory and in
`events.jsonl`); and serves them on 127.0.0.1 to whichever proxy is running, behind a per-broker
token. A restarted proxy finds `broker.json`, reconnects, and asks for everything after the last
line it had. The page never loses the session, and Start never makes a second one.

    python -m aicleaner.broker <session folder>

The folder holds `launch.json` (command, cwd, env) written by the proxy; the broker writes
`broker.json` (pid, port, token, cli_pid, started, exit code) and `events.jsonl`.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import web

TOKEN_HEADER = "X-Broker-Token"
# How long a broker stays after Claude Code exits, so a proxy that is restarting can still collect
# the last lines and the exit code.
LINGER_S = 600
LINE_LIMIT = 64 * 1024 * 1024


async def _read_line(stream: asyncio.StreamReader) -> bytes:
    """One line however long (the reason for DECISIONS 47): readline() gives up past the limit."""
    parts: list[bytes] = []
    while True:
        try:
            parts.append(await stream.readuntil(b"\n"))
            return b"".join(parts)
        except asyncio.IncompleteReadError as exc:
            parts.append(exc.partial)
            return b"".join(parts)
        except asyncio.LimitOverrunError as exc:
            parts.append(await stream.readexactly(exc.consumed))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Broker:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.launch = json.loads((folder / "launch.json").read_text(encoding="utf-8"))
        self.token = secrets.token_urlsafe(24)
        self.records: list[dict[str, Any]] = []
        self.changed = asyncio.Event()
        self.process: asyncio.subprocess.Process | None = None
        self.exit_code: int | None = None
        self.exit_delivered = asyncio.Event()       # a proxy has the exit: no reason to linger
        self.published: dict[str, Any] = {}
        self._log = (folder / "events.jsonl").open("a", encoding="utf-8")

    def _record(self, stream: str, line: str) -> None:
        record = {"seq": len(self.records) + 1, "s": stream, "line": line, "t": time.time()}
        self.records.append(record)
        self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._log.flush()
        self.changed.set()
        self.changed = asyncio.Event()

    async def _pump(self, stream: asyncio.StreamReader, name: str) -> None:
        while True:
            raw = await _read_line(stream)
            if not raw:
                return
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.strip():
                self._record(name, text)

    async def run(self) -> None:
        env = {**os.environ, **self.launch.get("env", {})}
        self.process = await asyncio.create_subprocess_exec(
            *self.launch["command"], cwd=self.launch.get("cwd") or None, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT)
        app = web.Application(client_max_size=LINE_LIMIT)
        app.router.add_get("/out", self.out)
        app.router.add_post("/in", self.stdin)
        app.router.add_post("/eof", self.eof)
        app.router.add_post("/kill", self.kill)
        app.router.add_get("/state", self.state)
        runner = web.AppRunner(app)
        await runner.setup()
        from . import acceptguard
        acceptguard.install()                # a dropped connection must not close the broker's port (D198)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]      # type: ignore[union-attr]
        self.published = {"pid": os.getpid(), "port": port, "token": self.token,
                     "cli_pid": self.process.pid, "started": time.time(), "exit_code": None}
        _write_json(self.folder / "broker.json", self.published)
        pumps = [asyncio.ensure_future(self._pump(self.process.stdout, "out")),
                 asyncio.ensure_future(self._pump(self.process.stderr, "err"))]
        self.exit_code = await self.process.wait()
        await asyncio.gather(*pumps, return_exceptions=True)
        self.published["exit_code"] = self.exit_code
        _write_json(self.folder / "broker.json", self.published)
        self.changed.set()
        try:
            await asyncio.wait_for(self.exit_delivered.wait(), timeout=LINGER_S)
            await asyncio.sleep(1)
        except asyncio.TimeoutError:
            pass
        await runner.cleanup()
        self._log.close()

    def _allowed(self, request: web.Request) -> bool:
        return secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), self.token)

    async def out(self, request: web.Request) -> web.StreamResponse:
        """Every line after ?after=N, then new ones as they come; ends with the exit record."""
        if not self._allowed(request):
            return web.Response(status=403)
        after = int(request.query.get("after", "0") or 0)
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        sent = after
        while True:
            waiter = self.changed
            for record in self.records[sent:]:
                await response.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
                sent = record["seq"]
            if self.exit_code is not None and sent >= len(self.records):
                await response.write((json.dumps({"exit": self.exit_code}) + "\n").encode("utf-8"))
                self.exit_delivered.set()
                return response
            try:
                await asyncio.wait_for(waiter.wait(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b"{}\n")          # a keep-alive, so a dead link shows itself

    async def stdin(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(status=403)
        data = await request.read()
        if self.process is None or self.process.stdin is None or self.exit_code is not None:
            return web.json_response({"error": "not running"}, status=409)
        self.process.stdin.write(data)
        await self.process.stdin.drain()
        for line in data.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                self._record("in", line)
        return web.json_response({"ok": True})

    async def eof(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(status=403)
        if self.process is not None and self.process.stdin is not None:
            self.process.stdin.close()
        return web.json_response({"ok": True})

    async def kill(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(status=403)
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
        return web.json_response({"ok": True})

    async def state(self, request: web.Request) -> web.Response:
        if not self._allowed(request):
            return web.Response(status=403)
        return web.json_response({**{k: v for k, v in self.published.items() if k != "token"},
                                  "exit_code": self.exit_code, "seq": len(self.records)})


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m aicleaner.broker <session folder>", file=sys.stderr)
        return 2
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(Broker(Path(argv[0])).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
