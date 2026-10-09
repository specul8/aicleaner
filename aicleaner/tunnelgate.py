"""The sealed door for the tunnel (feature 21, DECISIONS 57 and 60).

Runs after the guard, so `request[REMOTE]` is already decided. With `remote.encrypt` on, a request
through the tunnel may reach the API only as `POST /api/sealed`, under the session key the page and
the proxy agreed for this connection (tunnelcrypt). The envelope's plaintext is the real request -
method, path with its query, body - so names in paths and queries are sealed too. The real route's
handler answers it, and the answer goes back sealed. Plain, because they carry nothing secret: the
page shell, `/api/tunnel/params` (salt and count) and the two steps of the key exchange. Everything
else under /api/ is refused as locked.

On the machine itself nothing here applies.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from .guard import REMOTE, refused_remotely
from .tunnelcrypt import SealError, UnknownSession

PARAMS = "/api/tunnel/params"
HELLO = "/api/tunnel/hello"
CONFIRM = "/api/tunnel/confirm"
SEALED = "/api/sealed"
PLAIN = (PARAMS, HELLO, CONFIRM)
# A handler that writes to the connection itself cannot be sealed after the fact, so it is refused
# before it runs: the page polls instead of streaming while sealed.
STREAMED = ("/api/session/events",)
# A file sealed whole travels as base64 inside JSON and is held in the browser while it decrypts.
MAX_SEALED_FILE = 50 * 1024 * 1024
# Set on the inner request of a sealed envelope, so a handler that would stream answers in one piece.
INSIDE_TUNNEL = web.RequestKey("aicleaner_inside_tunnel", bool)
LOCKED = "locked: through the tunnel this page talks only in sealed requests. Enter the code."


def make_tunnel_gate(proxy: Any) -> Any:
    @web.middleware
    async def gate(request: web.Request, handler: Any) -> web.StreamResponse:
        if not request.get(REMOTE) or not proxy.cfg.remote_encrypt:
            return await handler(request)
        path = request.path
        if not path.startswith("/api/") or path in PLAIN:
            return await handler(request)
        if path != SEALED or request.method != "POST" or proxy.tunnel.key() is None:
            return web.json_response({"error": LOCKED}, status=403)
        envelope = await _read_stream(request, proxy.cfg.client_max_mb * 1024 * 1024)
        if envelope is None:
            return web.json_response({"error": "the sealed request is too large"}, status=413)
        try:
            sid, seq, plain = proxy.tunnel_sessions.open(json.loads(envelope))
            inner = json.loads(plain)
        except UnknownSession:
            # Restarted, or idle too long: the page runs the exchange again and resends.
            return web.json_response({"error": "unknown session"}, status=401)
        except (SealError, ValueError):
            return web.json_response({"error": "the sealed request did not open"}, status=400)
        answer = json.dumps(await _dispatch(request, inner)).encode("utf-8")
        try:
            return web.json_response(proxy.tunnel_sessions.seal_reply(sid, seq, answer))
        except UnknownSession:
            return web.json_response({"error": "unknown session"}, status=401)

    return gate


async def _read_stream(request: web.Request, limit: int) -> bytes | None:
    """The body, read from the stream rather than with request.read(): aiohttp will not clone a
    request whose content was read that way, and the inner request is a clone. Bounded as
    client_max_size would bound it; None when over."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _dispatch(request: web.Request, inner: Any) -> dict[str, Any]:
    """Run the request the envelope carries through the route it names; the answer as
    {status, type, body} with the body in base64, ready to seal."""
    if not isinstance(inner, dict):
        return _answer(400, {"error": "not a sealed request"})
    method = str(inner.get("method") or "GET").upper()
    target = str(inner.get("path") or "")
    bare = target.split("?", 1)[0]
    if not bare.startswith("/api/") or bare in (SEALED, *PLAIN) or refused_remotely(bare):
        return _answer(403, {"error": "local only: open this page on the machine aicleaner runs on to use it"})
    if bare in STREAMED:
        return _answer(403, {"error": "not through the encrypted tunnel: the page polls instead"})
    body = inner.get("body")
    clone = request.clone(method=method, rel_url=target)
    # The body the handler reads is the one inside the envelope. aiohttp reads a request's body
    # once into _read_bytes; setting it is how a clone carries a different one.
    clone._read_bytes = body.encode("utf-8") if isinstance(body, str) else b""
    # A handler that would stream (the zip) answers with its bytes instead, for sealing (D168).
    clone[INSIDE_TUNNEL] = True
    match = await request.app.router.resolve(clone)
    match.add_app(request.app)
    match.freeze()
    clone._match_info = match
    try:
        response = await match.handler(clone)
    except web.HTTPException as exc:
        return {"status": exc.status, "type": "text/plain",
                "body": base64.b64encode((exc.text or exc.reason or "").encode("utf-8")).decode("ascii")}
    if isinstance(response, web.FileResponse):
        # A file (a download, an image or PDF in a tab) has not been sent yet: read and sealed whole
        # (D168), up to a size the browser can hold while it decrypts.
        return await asyncio.to_thread(_file_answer, response)
    if not isinstance(response, web.Response):
        return _answer(403, {"error": "this answer cannot be sealed: open it on the machine"})
    raw = response.body if isinstance(response.body, (bytes, bytearray)) else b""
    return {"status": response.status, "type": response.content_type,
            "disposition": response.headers.get("Content-Disposition", ""),
            "body": base64.b64encode(bytes(raw)).decode("ascii")}


def _file_answer(response: web.FileResponse) -> dict[str, Any]:
    path = Path(response._path)
    try:
        size = path.stat().st_size
        if size > MAX_SEALED_FILE:
            return _answer(413, {"error": f"{path.name} is {size // (1024 * 1024)} MB; through the encrypted tunnel "
                                          f"the limit is {MAX_SEALED_FILE // (1024 * 1024)} MB - open it on the machine"})
        raw = path.read_bytes()
    except OSError as exc:
        return _answer(403, {"error": f"{path.name} could not be read: {exc}"})
    kind = response.headers.get("Content-Type") or "application/octet-stream"
    return {"status": response.status, "type": kind.split(";", 1)[0],
            "disposition": response.headers.get("Content-Disposition", ""),
            "body": base64.b64encode(raw).decode("ascii")}


def _answer(status: int, value: Any) -> dict[str, Any]:
    return {"status": status, "type": "application/json",
            "body": base64.b64encode(json.dumps(value).encode("utf-8")).decode("ascii")}
