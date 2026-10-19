"""Feature 117: the forward proxy the Claude Desktop app's Code tab is pointed at (HTTPS_PROXY).

Two listeners, both on 127.0.0.1:

- **The proxy** (`codetab.port`, default 8788) answers only `CONNECT host:port`:
  - api.anthropic.com:443 is joined to the second listener, so the call is read and filtered;
  - Datadog's intake (*.datadoghq.com) is refused - Claude Code's error logs would carry client text;
  - anything else is joined to the real host and passed through untouched (sign-in, updates, the
    services MCP servers and commands reach when they do use the proxy).
  Anything but CONNECT is refused (405), and so is a caller that is not on this machine.
- **The reader** (an ephemeral port, never advertised) presents a certificate for api.anthropic.com
  from the Code tab authority (codetab_ca) and hands every request to `Proxy.handle` as the
  `anthropic` dialect - the same path VS Code's traffic takes, so filtering, the evidence log, images
  and the footer are exactly those.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from collections import Counter
from typing import Any

from aiohttp import web

from .codetab_ca import HOST, Authority
from .copilot import is_copilot

log = logging.getLogger("aicleaner.codetab")

# A request key: set on everything the reader takes in, so the proxy knows the dialect (there is
# none in the path) and the evidence log can say where it came from.
CODETAB = web.RequestKey("aicleaner_codetab", bool)
# Datadog log and browser intake: http-intake.logs.us5.datadoghq.com, browser-intake-us5-datadoghq.com.
BLOCKED = re.compile(r"(^|[.-])datadoghq\.(com|eu)$")
HEAD_LIMIT = 16 * 1024
CONNECT_TIMEOUT_S = 20
RENEW_CHECK_S = 6 * 3600


def blocked(host: str) -> bool:
    return bool(BLOCKED.search(host.lower().rstrip(".")))


def parse_connect(head: bytes) -> tuple[str, str, int]:
    """(method, host, port) from a request head; host '' when it is not a CONNECT to host:port."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) != 3:
        return "", "", 0
    method, target = parts[0].upper(), parts[1]
    if method != "CONNECT":
        return method, "", 0
    host, _, port = target.rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        return method, "", 0
    return method, host, int(port)


def _loopback(peer: Any) -> bool:
    try:
        return ipaddress.ip_address(peer[0]).is_loopback
    except (TypeError, ValueError, IndexError):
        return False


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except (OSError, RuntimeError):
        pass


class CodeTab:
    """Both listeners for one proxy. `handle(request)` is Proxy.handle."""

    def __init__(self, handle: Any, authority: Authority | None, port: int, client_max_mb: int = 64) -> None:
        self._handle = handle
        # None: the Code tab is off and only Copilot is read (2026-10-18); api.anthropic.com passes through.
        self.authority = authority
        # Copilot's reader (2026-10-18): its own authority, constrained to Copilot's hosts; started by
        # set_copilot when the copilot.enabled switch is on.
        self._copilot_runner: web.AppRunner | None = None
        self._copilot: tuple[Authority, Any] | None = None
        self.copilot_port = 0
        self.port = port
        self._client_max = client_max_mb * 1024 * 1024
        self._server: asyncio.base_events.Server | None = None
        self._runner: web.AppRunner | None = None
        self._context = None
        self._renewer: asyncio.Task[None] | None = None
        self.reader_port = 0
        self.tunnels: Counter[str] = Counter()
        self.refused: Counter[str] = Counter()
        self.last_error = ""
        self.started_at = 0.0

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self.authority is not None:
            self._context = await asyncio.to_thread(self.authority.server_context)
            app = web.Application(client_max_size=self._client_max, middlewares=[self._mark])
            app.router.add_route("*", "/{tail:.*}", self._handle)
            self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self._context)
            await site.start()
            self.reader_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        try:
            self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port, limit=HEAD_LIMIT)
        except OSError:
            await self._runner.cleanup()
            self._runner = None
            raise
        self._renewer = asyncio.create_task(self._renew())
        self.started_at = time.time()
        log.info("Code tab proxy listening on 127.0.0.1:%d (reader on %d)", self.port, self.reader_port)

    async def set_copilot(self, authority: Authority | None, handle: Any = None) -> None:
        """Copilot's reader started (an authority and a handler given) or stopped (None)."""
        if authority is None or handle is None:
            self._copilot = None
            if self._copilot_runner is not None:
                await self._copilot_runner.cleanup()
                self._copilot_runner = None
                self.copilot_port = 0
            return
        self._copilot = (authority, handle)
        if self._copilot_runner is not None:
            return
        context = await asyncio.to_thread(authority.server_context)
        app = web.Application(client_max_size=self._client_max)
        app.router.add_route("*", "/{tail:.*}", handle)
        self._copilot_runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._copilot_runner.setup()
        site = web.TCPSite(self._copilot_runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        self.copilot_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        log.info("Copilot reader listening on 127.0.0.1:%d", self.copilot_port)

    def reader_for(self, host: str, port: int) -> tuple[str, int] | None:
        """Where a CONNECT to host:port is joined when it is read here, or None to pass it through."""
        name = host.lower().rstrip(".")
        if port != 443:
            return None
        if name == HOST and self._runner is not None:
            return ("127.0.0.1", self.reader_port)
        if self.copilot_port and is_copilot(name):
            return ("127.0.0.1", self.copilot_port)
        return None

    async def stop(self) -> None:
        await self.set_copilot(None)
        if self._renewer is not None:
            self._renewer.cancel()
            self._renewer = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _mark(self, request: web.Request, handler: Any) -> web.StreamResponse:
        request[CODETAB] = True
        return await handler(request)

    async def _renew(self) -> None:
        """The server certificate renewed in place before it ends; the proxy may run for months."""
        while True:
            await asyncio.sleep(RENEW_CHECK_S)
            if self._context is not None and self.authority is not None and self.authority.needs_renewal():
                try:
                    await asyncio.to_thread(self.authority.load_into, self._context)
                    log.info("Code tab server certificate renewed")
                except Exception as exc:  # noqa: BLE001 - the old one is still good for weeks
                    log.warning("Code tab certificate renewal failed: %s", exc)
            if self._copilot is not None and self._copilot[0].needs_renewal():
                authority, handle = self._copilot
                try:
                    await self.set_copilot(None)
                    await self.set_copilot(authority, handle)
                    log.info("Copilot server certificate renewed")
                except Exception as exc:  # noqa: BLE001 - logged; the next check tries again
                    log.warning("Copilot certificate renewal failed: %s", exc)

    async def _answer(self, writer: asyncio.StreamWriter, status: str, why: str = "") -> None:
        body = (why + "\n").encode("utf-8") if why else b""
        writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     "Connection: close\r\n\r\n".encode("latin-1") + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        await _close(writer)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not _loopback(writer.get_extra_info("peername")):
            self.refused["not this machine"] += 1
            await _close(writer)
            return
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError, OSError):
            await _close(writer)
            return
        method, host, port = parse_connect(head)
        if method != "CONNECT":
            self.refused[f"method {method or '?'}"] += 1
            await self._answer(writer, "405 Method Not Allowed", "aicleaner: this proxy only tunnels (CONNECT)")
            return
        if not host:
            await self._answer(writer, "400 Bad Request", "aicleaner: CONNECT needs host:port")
            return
        if blocked(host):
            self.refused[host] += 1
            log.info("Code tab: refused %s (blocked: error and log intake)", host)
            await self._answer(writer, "403 Forbidden", f"aicleaner: {host} is blocked")
            return
        # Not `reader`: that is the client's stream, piped below.
        joined = self.reader_for(host, port)
        read_here = joined is not None
        target = joined or (host, port)
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(*target), CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as exc:
            self.last_error = f"{host}:{port}: {exc or type(exc).__name__}"
            await self._answer(writer, "502 Bad Gateway", f"aicleaner: could not reach {host}:{port}")
            return
        self.tunnels[host.lower() if read_here else f"{host.lower()} (passed through)"] += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        try:
            await writer.drain()
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        except (ConnectionError, OSError):
            pass
        finally:
            await _close(up_writer)
            await _close(writer)

    def summary(self) -> dict[str, Any]:
        return {"listening": self.running, "port": self.port, "tunnels": dict(self.tunnels.most_common(20)),
                "refused": dict(self.refused), "last_error": self.last_error, "copilot_reader": bool(self.copilot_port),
                **(self.authority.summary() if self.authority is not None else {})}
