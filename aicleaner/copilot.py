"""GitHub Copilot in VS Code through aicleaner (2026-10-18; spec 2026-10-18-copilot-filtering-design.md).

VS Code's proxy setting points at the forward proxy the Claude Desktop Code tab uses (codetab.py). For
Copilot's hosts the proxy hands the connection to its reader, which presents a certificate from
aicleaner's local authority; the reader passes each request to the proxy (Proxy.copilot_handle), which
filters the paths that carry the person's text and code and sends the rest here, forwarded to the real
host untouched - plain HTTP and WebSocket alike.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from typing import Any

import aiohttp
from aiohttp import web

log = logging.getLogger("aicleaner.copilot")

# The hosts Copilot's chat, agent, completions, code matching and telemetry use (two captures,
# 2026-10-18). Plan-specific hosts carry individual / business / enterprise in the middle.
HOSTS = re.compile(r"^(api|proxy|origin-tracker|telemetry)\.((individual|business|enterprise)\.)?githubcopilot\.com$"
                   r"|^copilot-telemetry\.githubusercontent\.com$")
# The names the reader's certificate must carry for them (a wildcard covers one label).
CERT_NAMES = ["*.githubcopilot.com", "*.individual.githubcopilot.com", "*.business.githubcopilot.com",
              "*.enterprise.githubcopilot.com", "copilot-telemetry.githubusercontent.com"]
# Not forwarded: they describe this hop, not the request (RFC 9110 hop-by-hop, plus what aiohttp sets).
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "content-length"}
WS_SKIP = HOP | {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-accept"}
# A request's body arrives inflated (aiohttp's server decodes it), so its old encoding is not sent on:
# gzip telemetry went upstream as plain bytes labelled gzip and was refused (2026-10-19). A reply is
# not decoded (auto_decompress=False), so its encoding stays.
REQUEST_SKIP = HOP | {"content-encoding"}
# Copilot's telemetry hosts: every event read, its strings tokenised, nothing restored (release 5).
TELEMETRY = re.compile(r"^telemetry\.((individual|business|enterprise)\.)?githubcopilot\.com$"
                       r"|^copilot-telemetry\.githubusercontent\.com$")
# Inline completions (OpenAI's legacy Completions) and public code matching, on proxy.* / origin-tracker.*.
COMPLETIONS = re.compile(r"^/v1/engines/[^/]+/completions$")
CODE_MATCH = "/twirp/github.snippy.v1.SnippyAPI/Match"


def _bare(host: str) -> str:
    return (host or "").lower().rstrip(".").split(":")[0]


def is_copilot(host: str) -> bool:
    return bool(HOSTS.match(_bare(host)))


def is_telemetry(host: str) -> bool:
    return bool(TELEMETRY.match(_bare(host)))


def tokenise_events(body: bytes, tokenise: Any) -> tuple[bytes, int, int]:
    """A telemetry post (application/x-json-stream: one JSON event per line) with every string in each
    event passed through `tokenise` (an object in, an object out). A line that does not parse is sent
    on as it was and counted, so a new shape shows on the Overview instead of being dropped (spec,
    "Telemetry carries code"). Returns the body, the events read and the lines that were not JSON."""
    out: list[bytes] = []
    events = not_json = 0
    for line in body.split(b"\n"):
        if not line.strip():
            out.append(line)
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            out.append(line)
            not_json += 1
            continue
        out.append(json.dumps(tokenise(event), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        events += 1
    return b"\n".join(out), events, not_json


class Forwarder:
    """Copilot's requests sent on to the host they named, and what comes back returned as it came."""

    def __init__(self, upstream: str = "") -> None:
        # `upstream`: tests only - a fake Copilot at http://127.0.0.1:<port> instead of the host asked for.
        self.upstream = upstream.rstrip("/")
        self._client: aiohttp.ClientSession | None = None
        self.seen: Counter[str] = Counter()
        self.last_error = ""
        # Telemetry read (release 5): posts, events tokenised, lines that were not JSON and went as they were.
        self.telemetry: Counter[str] = Counter({"posts": 0, "events": 0, "not_json": 0})

    async def client(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            # No timeout on the whole: a streamed reply or a conversation socket lasts as long as it lasts.
            self._client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20),
                                                 auto_decompress=False)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def handle(self, request: web.Request, body: bytes | None = None) -> web.StreamResponse:
        """Sent on as it came; `body`, when given, is sent in place of the request's own (telemetry)."""
        host = request.host.split(":")[0].lower()
        self.seen[f"{request.method} {host}{request.path}"] += 1
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._socket(request, host)
        return await self._http(request, host, body)

    async def _http(self, request: web.Request, host: str, body: bytes | None = None) -> web.StreamResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_SKIP}
        # A client that asks for no encoding gets none (2026-10-20): aiohttp otherwise adds its own
        # "Accept-Encoding: gzip, deflate", the reply comes back gzipped, and this forwards it as it came -
        # to a client (Codex) that never asked for gzip and cannot read it.
        if not any(k.lower() == "accept-encoding" for k in headers):
            headers["Accept-Encoding"] = "identity"
        if body is None:
            body = await request.read()
        url = f"{self.upstream or 'https://' + host}{request.path_qs}"
        try:
            upstream = await (await self.client()).request(request.method, url, headers=headers, data=body or None,
                                                           allow_redirects=False)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.last_error = f"{host}: {exc or type(exc).__name__}"
            return web.json_response({"error": f"aicleaner could not reach {host}"}, status=502)
        response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
        for key, value in upstream.headers.items():
            if key.lower() not in HOP:
                response.headers.add(key, value)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        finally:
            upstream.release()
        await response.write_eof()
        return response

    async def _socket(self, request: web.Request, host: str) -> web.StreamResponse:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in WS_SKIP}
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        headers.pop("Sec-WebSocket-Protocol", None)
        base = self.upstream.replace("http://", "ws://", 1) if self.upstream else f"wss://{host}"
        url = f"{base}{request.path_qs}"
        try:
            upstream = await (await self.client()).ws_connect(url, headers=headers, protocols=protocols,
                                                              max_msg_size=0, autoping=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.last_error = f"{host} (socket): {exc or type(exc).__name__}"
            return web.json_response({"error": f"aicleaner could not reach {host}"}, status=502)
        here = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else (), max_msg_size=0,
                                     autoping=True)
        await here.prepare(request)

        async def pump(source: Any, target: Any) -> None:
            async for message in source:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await target.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await target.send_bytes(message.data)
                elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    break
            await target.close()

        try:
            await asyncio.gather(pump(here, upstream), pump(upstream, here), return_exceptions=True)
        finally:
            await upstream.close()
            await here.close()
        return here

    def summary(self) -> dict[str, Any]:
        return {"requests": dict(self.seen.most_common(20)), "last_error": self.last_error,
                "telemetry": dict(self.telemetry)}
