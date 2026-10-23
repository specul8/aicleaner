"""Route B (spec 2026-10-21-desktop-chat-route-b.md): Claude Desktop's chat, signed in to claude.ai, filtered.

The forward proxy joins `claude.ai` to this reader (its own authority, constrained to claude.ai) once the person
switches route B on. Every request is one of:

- **the chat** (`/claudeai-rpc/...`, Connect-RPC, binary protobuf - `application/proto` unary or
  `application/connect+proto` enveloped): the text fields read without a schema (protowire), filtered as one JSON
  document by the same `_filter_body` every route uses, written back by position; then the canary over the final
  raw bytes, UTF-8 and UTF-16. Replies: every text field restored, frame by frame;
- **the JSON API** (`/api/...`: project instructions, memory, renames, knowledge as `{file_name, content}`):
  filtered as JSON; JSON replies restored;
- **uploads** (`multipart/form-data`): each file read on this machine (the attachment readers - PDF text or OCR,
  Office parts, images) and sent as filtered text, `<name>.txt`; a file that cannot be read is refused. claude.ai's
  `convert_document` is answered here, so that file never leaves;
- **analytics** (`/api/v2/rum`, `/api/event_logging`): sent as they are, through the canary;
- **Cloudflare checks and the Code tab's cloud side** (`/cdn-cgi/`, `/v1/code/`): passed through (spec, section 4);
- anything else with a body that cannot be read: refused (fail closed).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
import uuid
from collections import Counter
from typing import Any

import aiohttp
from aiohttp import web

from . import protowire as pw

log = logging.getLogger("aicleaner.claudeai")

# What the probes established (2026-10-21/22): the typed message is field 3 of PerformAction. The rest is learned
# from the structure log in a synthetic session; outside the map, protowire's guess applies.
FIELD_MAP: dict[str, dict[str, str]] = {"PerformAction": {"3": "text"}}
# Found 2026-10-23 in a synthetic chat's structure log: the finished reply's whole text is 1.1.5.9 of a StreamTimeline
# frame (while it streams, the pieces are 1.2.2), and 1.1.5.4 is a number beside it - who wrote it.
REPLY_TEXT = "1.1.5.9"
REPLY_SENDER = "1.1.5.4"
MARK = "\n\n(filtered by aicleaner)"
PASS_THROUGH = ("/cdn-cgi/", "/v1/code/")
ANALYTICS = ("/api/v2/rum", "/api/event_logging")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "content-length", "content-encoding", "accept-encoding"}
RESPONSE_SKIP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
TEXT_TYPES = re.compile(r"^(text/|application/(json|xml|x-yaml|yaml|javascript|x-sh|x-python|csv)\b)")
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".ps1", ".psm1", ".py", ".js", ".ts",
                   ".html", ".htm", ".css", ".sql", ".sh", ".bat", ".cmd", ".log", ".ini", ".cfg", ".conf", ".tf",
                   ".bicep", ".cs", ".java", ".go", ".rb", ".rs", ".c", ".h", ".cpp", ".toml", ".rtf"}
UPLOAD = re.compile(r"/upload$")
CONVERT = re.compile(r"/convert_document$")
MEDIA_BY_EXTENSION = {".pdf": "application/pdf",
                      ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                      ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                      ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                      ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                      ".webp": "image/webp"}


def method_of(path: str) -> str:
    """`PerformAction` from `/claudeai-rpc/anthropic.bard.api.v1alpha.ConversationService/PerformAction`."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def raw_canary(mapper: Any, data: bytes) -> list[str]:
    """Client ids whose real forms are in `data`, read as UTF-8 and as UTF-16 at both alignments: the net under
    a field read wrongly (a string taken for bytes, or a message taken for text)."""
    from .transform import canary

    found = set(canary(mapper, data))
    for offset in (0, 1):
        found.update(mapper.find_real(data[offset:].decode("utf-16-le", errors="replace")))
    return sorted(found)


# ---- multipart ---------------------------------------------------------------------------------------------------

class Part:
    def __init__(self, headers: list[tuple[str, str]], data: bytes) -> None:
        self.headers = headers
        self.data = data

    def header(self, name: str) -> str:
        return next((v for k, v in self.headers if k.lower() == name.lower()), "")

    def param(self, name: str) -> str:
        found = re.search(rf'(?:^|;)\s*{name}="([^"]*)"', self.header("Content-Disposition"), re.I)
        return found.group(1) if found else ""

    def serialise(self) -> bytes:
        head = "".join(f"{k}: {v}\r\n" for k, v in self.headers)
        return head.encode("utf-8") + b"\r\n" + self.data


def boundary_of(content_type: str) -> str:
    found = re.search(r'boundary="?([^";]+)"?', content_type, re.I)
    return found.group(1) if found else ""


def parse_multipart(body: bytes, boundary: str) -> list[Part]:
    marker = b"--" + boundary.encode("latin-1")
    pieces = body.split(marker)
    if len(pieces) < 3 or not pieces[-1].startswith(b"--"):
        raise ValueError("not a complete multipart body")
    parts = []
    for piece in pieces[1:-1]:
        piece = piece[2:] if piece.startswith(b"\r\n") else piece
        piece = piece[:-2] if piece.endswith(b"\r\n") else piece
        head, sep, data = piece.partition(b"\r\n\r\n")
        if not sep:
            raise ValueError("a part with no header end")
        headers = []
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                headers.append((key.strip(), value.strip()))
        parts.append(Part(headers, data))
    return parts


def build_multipart(parts: list[Part], boundary: str) -> bytes:
    marker = b"--" + boundary.encode("latin-1")
    return b"".join(marker + b"\r\n" + p.serialise() + b"\r\n" for p in parts) + marker + b"--\r\n"


def _extension(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def media_of(part: Part) -> str:
    media = part.header("Content-Type").split(";")[0].strip().lower()
    if not media or media == "application/octet-stream":
        media = MEDIA_BY_EXTENSION.get(_extension(part.param("filename")), media)
    return media


def is_text_file(part: Part) -> bool:
    return bool(TEXT_TYPES.match(media_of(part))) or _extension(part.param("filename")) in TEXT_EXTENSIONS


# ---- the reader --------------------------------------------------------------------------------------------------

class ClaudeAI:
    """The claude.ai reader's request handling, over the proxy's filter."""

    def __init__(self, proxy: Any) -> None:
        self.proxy = proxy
        self._client: aiohttp.ClientSession | None = None
        self.upstream = ""                          # tests: a fake claude.ai at http://127.0.0.1:<port>
        self.seen: Counter[str] = Counter()
        self.refused: Counter[str] = Counter()
        self.last_error = ""
        self.last_at = 0.0
        self.structure: list[str] = []              # the structure log's recent lines (paths and kinds only)
        problem = pw.self_test()
        self.broken = f"the protobuf reader failed its self-test ({problem})" if problem else ""
        if self.broken:
            log.error("claude.ai: %s - its chat is refused until aicleaner is updated", self.broken)

    async def client(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=20),
                                                 auto_decompress=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ---- shared ------------------------------------------------------------------------------------------------

    def _begin(self, request: web.Request) -> None:
        from .proxy import _REQ
        request[_REQ] = {"id": uuid.uuid4().hex[:12], "who": {"app": "Claude Desktop (claude.ai)"}, "project": "",
                         "held": None, "unprotected": [], "declined": [], "canary": []}

    def _filtering(self, request: web.Request, body: bytes, started: float) -> Any:
        from .proxy import SubCounter, _Filtering
        p = self.proxy
        return _Filtering(request, "claudeai", request.path.lstrip("/"), started, "", "", "application/json",
                          p.mapper, p.mapper, body, SubCounter(), SubCounter(), Counter(), [], SubCounter(), {}, {}, 0)

    def _refusal(self, request: web.Request, message: str, connect_stream: bool = False) -> web.Response:
        """A refusal in the form the caller reads: Connect's error for the chat, JSON otherwise."""
        self.refused[method_of(request.path)] += 1
        if connect_stream:
            # A Connect stream's error travels in its end-of-stream frame, with HTTP 200.
            frame = pw.envelope(pw.END_STREAM, json.dumps(
                {"error": {"code": "permission_denied", "message": message}}).encode("utf-8"))
            return web.Response(status=200, body=frame, content_type="application/connect+proto")
        return web.json_response({"code": "permission_denied", "message": message,
                                  "error": {"type": "aicleaner_refused", "message": message}}, status=403)

    def _record(self, request: web.Request, status: int, f: Any, inbound: Counter[str], started: float,
                note: str, streamed: bool = False) -> None:
        from .proxy import _fail_open_of
        leaked, said = _fail_open_of(request)
        self.proxy._record(request, dialect="claudeai", method=request.method, path=request.path, status=status,
                           outbound=f.outbound if f else Counter(), inbound=inbound, canary=leaked,
                           streamed=streamed, started=started, note=said or note,
                           secrets=f.secrets if f else None, auto_added=f.auto_added if f else None,
                           held_back=f.held_back if f else None, attachments=f.attach if f else None,
                           near_miss=f.near_miss if f else 0)

    def _headers(self, request: web.Request) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        headers["Accept-Encoding"] = "gzip, deflate"       # never br or zstd: replies must be readable here
        return headers

    def _url(self, request: web.Request) -> str:
        host = request.host.split(":")[0].lower()
        return f"{self.upstream or 'https://' + host}{request.path_qs}"

    async def _send(self, request: web.Request, body: bytes | None) -> aiohttp.ClientResponse:
        return await (await self.client()).request(request.method, self._url(request), headers=self._headers(request),
                                                   data=body or None, allow_redirects=False)

    def _canary_check(self, request: web.Request, data: bytes) -> str:
        """'' when `data` holds no real name, or the canary's fail-open switch is on; otherwise the refusal."""
        from .proxy import _FAIL_OPEN, _REQ
        leaked = raw_canary(self.proxy.mapper, data)
        if not leaked:
            return ""
        if self.proxy.cfg.canary_fail_open:
            log.warning("canary FAIL-OPEN: sent a claude.ai request the canary would have refused (clients: %s)",
                        ", ".join(leaked))
            request[_FAIL_OPEN] = leaked
            return ""
        request[_REQ]["canary"] = [{"path": request.path, "where": "raw body", "client": c} for c in leaked]
        log.warning("canary: a real name survived in a claude.ai request %s (clients: %s)", request.path,
                    ", ".join(leaked))
        return (f"aicleaner refused to send this: a real client identifier survived filtering (clients: "
                f"{', '.join(leaked)}). Check the mapping forms.")

    async def _filter_texts(self, request: web.Request, texts: list[Any], started: float) -> tuple[Any, list[Any] | None]:
        """`texts` (strings or attachment blocks) through the proxy's whole filter as one JSON document. Returns
        (filtering, filtered list) or (refusal response, None)."""
        f = self._filtering(request, json.dumps({"claudeai": texts}, ensure_ascii=False).encode("utf-8"), started)
        refused = await self.proxy._filter_body(f)
        if refused is not None:
            try:
                message = json.loads(refused.text)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = "aicleaner refused to send this request"
            return message, None
        return f, json.loads(f.body)["claudeai"]

    # ---- entry -------------------------------------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        from .proxy import _outside_body
        started = time.monotonic()
        self.last_at = time.time()
        self._begin(request)
        path = request.path
        self.seen[f"{request.method} {method_of(path) if '/claudeai-rpc/' in path else '/'.join(path.split('/')[:3])}"] += 1
        if request.headers.get("Upgrade", "").lower() == "websocket":
            log.warning("claude.ai: a WebSocket to %s was refused - it cannot be read yet", path)
            return self._refusal(request, "aicleaner: this claude.ai connection cannot be filtered yet, so it was "
                                          "not opened")
        if path.startswith(PASS_THROUGH):
            return await self._passthrough(request, started)
        found = self.proxy.mapper.find_real(_outside_body(request, path.lstrip("/")))
        if found:
            return self._refusal(request, f"aicleaner: a client name is in the address of this request (clients: "
                                          f"{', '.join(found)}), so it was not sent")
        body = await request.read()
        media = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if self.proxy.cfg.proxy_bypass:
            return await self._passthrough(request, started, body)
        if "/claudeai-rpc/" in path:
            return await self._rpc(request, body, media, started)
        if not body:
            return await self._forward(request, None, None, started, "no body")
        if path.startswith(ANALYTICS):
            refusal = self._canary_check(request, body)
            if refusal:
                return self._refusal(request, refusal)
            return await self._forward(request, body, None, started, "analytics: canary only")
        if media == "application/json":
            return await self._json(request, body, started)
        if media == "multipart/form-data":
            return await self._multipart(request, body, started)
        log.warning("claude.ai: refused %s %s - a %s body cannot be read", request.method, path, media or "untyped")
        return self._refusal(request, f"aicleaner: this request's body ({media or 'no type'}) cannot be checked for "
                                      "client names, so it was not sent")

    # ---- the routes ----------------------------------------------------------------------------------------------

    async def _passthrough(self, request: web.Request, started: float, body: bytes | None = None) -> web.StreamResponse:
        if body is None:
            body = await request.read()
        try:
            upstream = await self._send(request, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return self._unreachable(request, exc)
        response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
        for key, value in upstream.headers.items():
            if key.lower() not in RESPONSE_SKIP:
                response.headers.add(key, value)
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        finally:
            upstream.release()
        await response.write_eof()
        return response

    def _unreachable(self, request: web.Request, exc: BaseException) -> web.Response:
        host = request.host.split(":")[0]
        self.last_error = f"{host}: {exc or type(exc).__name__}"
        return web.json_response({"error": f"aicleaner could not reach {host}"}, status=502)

    async def _json(self, request: web.Request, body: bytes, started: float) -> web.StreamResponse:
        f = self._filtering(request, body, started)
        refused = await self.proxy._filter_body(f)
        if refused is not None:
            self.refused[request.path] += 1
            return refused
        return await self._forward(request, f.body, f, started, "JSON")

    async def _forward(self, request: web.Request, body: bytes | None, f: Any, started: float,
                       note: str) -> web.StreamResponse:
        """Sent on; a JSON reply restored, an event stream restored event by event, anything else as it came."""
        from .dialects import GenericStreamRewriter
        from .proxy import SubCounter
        from .sse import SSEParser
        from .transform import dumps, rehydrate_json
        mapper = f.reply_mapper if f is not None else self.proxy.mapper
        inbound: Counter[str] = SubCounter()
        try:
            upstream = await self._send(request, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return self._unreachable(request, exc)
        headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in RESPONSE_SKIP]
        kind = upstream.content_type
        try:
            if kind == "text/event-stream":
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
                for key, value in headers:
                    response.headers.add(key, value)
                await response.prepare(request)
                parser, rewriter = SSEParser(), GenericStreamRewriter(mapper, inbound)
                async for chunk in upstream.content.iter_any():
                    for event in parser.feed(chunk):
                        for out in rewriter.on_event(event):
                            await response.write(out.serialise())
                for event in parser.finish():
                    for out in rewriter.on_event(event):
                        await response.write(out.serialise())
                await response.write_eof()
                self._record(request, upstream.status, f, inbound, started, note, streamed=True)
                return response
            raw = await upstream.read()
        finally:
            upstream.release()
        if kind == "application/json" and raw:
            try:
                raw = dumps(rehydrate_json(mapper, json.loads(raw), inbound))
            except ValueError:
                pass
        self._record(request, upstream.status, f, inbound, started, note)
        response = web.Response(status=upstream.status, reason=upstream.reason, body=raw)
        for key, value in headers:
            response.headers.add(key, value)
        return response

    # ---- the chat: Connect-RPC protobuf ----------------------------------------------------------------------------

    def _log_structure(self, method: str, direction: str, message: bytes) -> None:
        if not self.proxy.cfg.claudeai_structure_log:
            return
        try:
            shape = " ".join(pw.structure(message, FIELD_MAP.get(method)))
        except pw.ProtoError as exc:
            shape = f"(not protobuf: {exc})"
        line = f"{method} {direction}: {shape}"
        self.structure = (self.structure + [line])[-200:]
        log.info("claude.ai structure %s", line[:4000])

    async def _rewrite_outbound(self, request: web.Request, method: str, message: bytes,
                                started: float) -> tuple[Any, bytes | str]:
        """One protobuf message filtered: (filtering, new bytes) or (None, refusal message)."""
        field_map = FIELD_MAP.get(method)
        self._log_structure(method, "request", message)
        found = pw.texts(message, field_map)
        if not found:
            return None, message
        f, filtered = await self._filter_texts(request, [t for _p, t in found], started)
        if filtered is None:
            return None, str(f)
        if len(filtered) != len(found):
            return None, "aicleaner: the message changed shape while it was filtered, so it was not sent"
        replacements = iter(str(t) for t in filtered)
        return f, pw.rewrite(message, lambda _p, _t: next(replacements), field_map)

    async def _rpc(self, request: web.Request, body: bytes, media: str, started: float) -> web.StreamResponse:
        method = method_of(request.path)
        stream = media == "application/connect+proto"
        if self.broken:
            return self._refusal(request, f"aicleaner: {self.broken}", stream)
        if media not in ("application/proto", "application/connect+proto"):
            if media == "application/json" and body:
                return await self._json(request, body, started)       # Connect's JSON codec: filtered as JSON
            if body:
                return self._refusal(request, f"aicleaner: a claude.ai call in {media or 'no type'} cannot be "
                                              "checked, so it was not sent", stream)
        f = None
        out = body
        try:
            if body and stream:
                frames = pw.FrameReader().feed(body)
                rebuilt = []
                for flag, message in frames:
                    if flag & pw.COMPRESSED:
                        return self._refusal(request, "aicleaner: a compressed claude.ai message cannot be "
                                                      "checked, so it was not sent", True)
                    if flag & pw.END_STREAM:
                        rebuilt.append(pw.envelope(flag, message))
                        continue
                    f_one, new = await self._rewrite_outbound(request, method, message, started)
                    if isinstance(new, str):
                        return self._refusal(request, new, True)
                    f = f_one or f
                    rebuilt.append(pw.envelope(flag, new))
                out = b"".join(rebuilt)
            elif body:
                f, new = await self._rewrite_outbound(request, method, body, started)
                if isinstance(new, str):
                    return self._refusal(request, new, stream)
                out = new
        except pw.ProtoError as exc:
            log.warning("claude.ai: %s could not be read as protobuf (%s); refused", method, exc)
            return self._refusal(request, f"aicleaner: this claude.ai message could not be read ({exc}), so it "
                                          "was not sent", stream)
        if out:
            refusal = self._canary_check(request, out)
            if refusal:
                return self._refusal(request, refusal, stream)
        return await self._rpc_forward(request, out, f, method, started)

    async def _rpc_forward(self, request: web.Request, body: bytes, f: Any, method: str,
                           started: float) -> web.StreamResponse:
        from .proxy import SubCounter
        from .transform import dumps, rehydrate_json
        mapper = f.reply_mapper if f is not None else self.proxy.mapper
        inbound: Counter[str] = SubCounter()
        field_map = FIELD_MAP.get(method)

        role = self.proxy.cfg.claudeai_mark_role

        def restore(message: bytes) -> bytes:
            self._log_structure(method, "reply", message)
            try:
                # The filtered mark (spec section 6, safeguard 1): the finished reply's whole text, from Claude - not
                # your own message, which comes back in the same shape - ends with it. A reply without it was not
                # restored by aicleaner. Off while the sender's value is not known (claudeai.mark_role 0).
                mark = bool(role) and method == "StreamTimeline" and pw.varints(message, REPLY_SENDER) == [role]
                return pw.rewrite(message, lambda p, t: mapper.rehydrate(t, inbound) + (
                    MARK if mark and p == REPLY_TEXT and t.strip() else ""), field_map)
            except pw.ProtoError:
                return message           # a reply that cannot be read is shown as it came (tokens, never a leak)

        try:
            upstream = await self._send(request, body)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            return self._unreachable(request, exc)
        headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in RESPONSE_SKIP]
        kind = upstream.content_type
        try:
            if kind == "application/connect+proto":
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
                for key, value in headers:
                    response.headers.add(key, value)
                await response.prepare(request)
                reader = pw.FrameReader()
                async for chunk in upstream.content.iter_any():
                    for flag, message in reader.feed(chunk):
                        if flag & pw.END_STREAM:
                            try:
                                message = dumps(rehydrate_json(mapper, json.loads(message), inbound))
                            except ValueError:
                                pass
                        elif not flag & pw.COMPRESSED:
                            message = restore(message)
                        await response.write(pw.envelope(flag, message))
                if reader.pending:
                    await response.write(reader.pending)
                await response.write_eof()
                self._record(request, upstream.status, f, inbound, started, f"claude.ai {method}", streamed=True)
                return response
            raw = await upstream.read()
        finally:
            upstream.release()
        if kind == "application/proto" and raw:
            raw = restore(raw)
        elif kind == "application/json" and raw:
            try:
                raw = dumps(rehydrate_json(mapper, json.loads(raw), inbound))
            except ValueError:
                pass
        self._record(request, upstream.status, f, inbound, started, f"claude.ai {method}")
        response = web.Response(status=upstream.status, reason=upstream.reason, body=raw)
        for key, value in headers:
            response.headers.add(key, value)
        return response

    # ---- uploads -----------------------------------------------------------------------------------------------------

    def _block(self, part: Part) -> Any:
        """A file part as what the filter reads: its text, or an attachment block the readers convert."""
        name = part.param("filename")
        if is_text_file(part):
            return part.data.decode("utf-8", errors="replace")
        return {"type": "document", "title": name,
                "source": {"type": "base64", "media_type": media_of(part),
                           "data": base64.b64encode(part.data).decode("ascii")}}

    async def _multipart(self, request: web.Request, body: bytes, started: float) -> web.StreamResponse:
        boundary = boundary_of(request.headers.get("Content-Type", ""))
        try:
            parts = parse_multipart(body, boundary) if boundary else []
        except ValueError as exc:
            return self._refusal(request, f"aicleaner: this upload could not be read ({exc}), so it was not sent")
        if not parts:
            return self._refusal(request, "aicleaner: this upload could not be read, so it was not sent")
        if CONVERT.search(request.path):
            return await self._convert_here(request, parts, started)
        if not UPLOAD.search(request.path):
            return self._refusal(request, f"aicleaner: uploads to {request.path} are not filtered yet, so this was "
                                          "not sent")
        items = [self._block(p) if p.param("filename") else p.data.decode("utf-8", errors="replace") for p in parts]
        f, filtered = await self._filter_texts(request, items, started)
        if filtered is None:
            return self._refusal(request, str(f))
        withheld = sum(v for k, v in (f.attach or {}).items() if k.endswith("_withheld"))
        if withheld:
            names = ", ".join(p.param("filename") for p in parts if p.param("filename"))
            return self._refusal(request, f"aicleaner: {names} could not be read on this machine, so it was not "
                                          "uploaded. Paste the parts you need, or save it as PDF, Word, Excel or "
                                          "PowerPoint.")
        rebuilt = []
        for part, item in zip(parts, filtered):
            name = part.param("filename")
            if isinstance(item, dict) and item.get("type") in ("image", "document"):
                rebuilt.append(part)                   # an image the person allowed to be sent as it is
                continue
            text = item.get("text", "") if isinstance(item, dict) else str(item)
            if not name:
                rebuilt.append(Part(part.headers, text.encode("utf-8")))
                continue
            new_name = name if is_text_file(part) else f"{name}.txt"
            field = part.param("name") or "file"
            rebuilt.append(Part([("Content-Disposition", f'form-data; name="{field}"; filename="{new_name}"'),
                                 ("Content-Type", "text/plain; charset=utf-8")], text.encode("utf-8")))
        out = build_multipart(rebuilt, boundary)
        refusal = self._canary_check(request, out)
        if refusal:
            return self._refusal(request, refusal)
        return await self._forward_multipart(request, out, f, started)

    async def _forward_multipart(self, request: web.Request, body: bytes, f: Any, started: float) -> web.StreamResponse:
        return await self._forward(request, body, f, started, "upload, sent as filtered text")

    async def _convert_here(self, request: web.Request, parts: list[Part], started: float) -> web.Response:
        """claude.ai's `convert_document`, answered on this machine in its own shape (probed 2026-10-22): the text
        goes back to the app, which posts it to the project's knowledge - filtered there like any JSON."""
        from .attachments import scrub
        part = next((p for p in parts if p.param("filename")), None)
        if part is None:
            return self._refusal(request, "aicleaner: no file in this conversion request")
        p = self.proxy
        item = self._block(part)
        if isinstance(item, str):
            text, counts = item, Counter()
        else:
            converted, counts = await asyncio.to_thread(
                lambda: scrub({"claudeai": [item]}, False, p._pdf_to_text, image_to_text=p._image_to_text,
                              ocr_max_pages=p.cfg.ocr_max_pages, ocr_timeout_s=p.cfg.ocr_timeout_s,
                              ocr_max_bytes=p.cfg.ocr_max_bytes))
            block = converted["claudeai"][0]
            text = block.get("text", "") if isinstance(block, dict) else ""
        name = part.param("filename")
        if any(k.endswith("_withheld") for k in counts) or not text:
            return self._refusal(request, f"aicleaner: {name} could not be read on this machine, so it was not "
                                          "converted")
        self._record(request, 200, None, Counter(), started, "convert_document answered on this machine")
        return web.json_response({"file_name": name, "file_size": len(part.data),
                                  "file_type": _extension(name).lstrip("."), "extracted_content": text})

    def summary(self) -> dict[str, Any]:
        return {"seen": dict(self.seen.most_common(20)), "refused": dict(self.refused), "last_error": self.last_error,
                "last_at": self.last_at, "broken": self.broken}
