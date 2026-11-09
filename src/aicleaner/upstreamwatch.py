"""Monitoring between AI Cleaner and the AI service (Brad, 2026-11-09): "We need actual data. If the AI Service is under
load, we should be able to tell. If the request is timing out, we should be able to find out why."

One line per model request in logs/upstream-<date>.jsonl, with the time each phase took, measured here:
- inside AI Cleaner: filtering the body, waiting for a pooled connection, opening one (DNS, TCP, TLS) or reusing one,
  sending the body;
- at the AI service: response headers (time to first byte), `message_start` (the model has the request), the first
  content event (text, thinking or a tool call), the end of the answer; the keep-alive `ping` events it sends while
  it works (pings before message_start mean the request is waiting at the service);
- what the service says: status, request id, rate-limit and retry headers, error events (overloaded, rate limit),
  token usage (input, cache read, cache write, output) and stop reason;
- how it ended: complete, the service ended it part-way, or the client (Claude Code) went away - and when.

Nothing in a line is client data: no prompt or answer text, only sizes, times, counts and the service's own headers.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner.upstreamwatch")

# Response headers worth keeping: who answered, how loaded it says it is, and when to retry.
KEEP_HEADERS = re.compile(r"^(request-id|x-request-id|cf-ray|retry-after|x-should-retry|anthropic-ratelimit-.*|"
                          r"x-ratelimit-.*|openai-processing-ms|x-envoy-upstream-service-time|server-timing|"
                          r"anthropic-organization-id)$", re.I)
_SESSION = re.compile(rb'session_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
_MODEL = re.compile(rb'"model"\s*:\s*"([^"]{1,80})"')


def _ms(since: float) -> float:
    return round((time.monotonic() - since) * 1000, 1)


class Watch:
    """The timings of one request, filled in as it goes, written once when it ends."""

    def __init__(self, request_id: str, dialect: str, path: str, started: float, body: bytes, client: str) -> None:
        self.started = started
        tail = body[-4096:] if body else b""
        session = _SESSION.search(tail) or (_SESSION.search(body[:4096]) if body else None)
        model = _MODEL.search(body[:4096]) if body else None
        self.rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "request": request_id, "dialect": dialect, "path": path[:120], "client": client[:60],
            "claude_session": session.group(1).decode() if session else "",
            "model": model.group(1).decode(errors="replace") if model else "",
            "body_bytes": len(body or b""), "ms": {}, "pings_before_start": 0, "pings_after_start": 0,
            "events": 0, "outcome": "",
        }
        self._ms = self.rec["ms"]
        self._message_started = False
        self._content = False

    def mark(self, name: str) -> None:
        """The time since the request reached AI Cleaner that `name` happened, first time only."""
        self._ms.setdefault(name, _ms(self.started))

    def headers(self, status: int, headers: Any) -> None:
        self.mark("response_headers")
        self.rec["status"] = status
        self.rec["headers"] = {k.lower(): v for k, v in headers.items() if KEEP_HEADERS.match(k)}

    def event(self, name: str, data: str) -> None:
        """One SSE event from the service: its time if it is a milestone, its usage if it carries one."""
        self.rec["events"] += 1
        if name == "ping":
            self.rec["pings_after_start" if self._message_started else "pings_before_start"] += 1
            self.mark("first_ping")
            return
        if name == "message_start":
            self._message_started = True
            self.mark("message_start")
            usage = _json(data).get("message", {}).get("usage") or {}
            self.rec["usage_in"] = {k: usage.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                                              "cache_creation_input_tokens") if k in usage}
        elif name in ("content_block_start", "content_block_delta") and not self._content:
            self._content = True
            self.mark("first_content")
        elif name == "message_delta":
            obj = _json(data)
            self.rec["stop_reason"] = (obj.get("delta") or {}).get("stop_reason")
            self.rec["output_tokens"] = (obj.get("usage") or {}).get("output_tokens")
        elif name == "message_stop":
            self.mark("message_stop")
        elif name == "error":
            err = _json(data).get("error") or {}
            self.rec["stream_error"] = {"type": err.get("type"), "message": str(err.get("message", ""))[:200]}
            self.mark("stream_error")

    def end(self, outcome: str, detail: str = "") -> None:
        self.mark("end")
        self.rec["outcome"] = self.rec["outcome"] or outcome
        if detail:
            self.rec["detail"] = detail[:200]


def _json(data: str) -> dict[str, Any]:
    try:
        obj = json.loads(data or "{}")
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


class UpstreamLog:
    """logs/upstream-YYYYMMDD.jsonl, appended a line at a time."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = Path(log_dir)
        self._lock = threading.Lock()

    def write(self, watch: Watch) -> None:
        line = json.dumps(watch.rec, ensure_ascii=False)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with self._lock, (self.log_dir / f"upstream-{datetime.now():%Y%m%d}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:            # monitoring never fails a request
            log.warning("upstream monitoring line not written: %s", exc)


def trace_config() -> Any:
    """aiohttp's connection-level timings for the request's Watch (passed as trace_request_ctx)."""
    import aiohttp

    trace = aiohttp.TraceConfig()

    def hook(name: str):
        async def on(_session: Any, ctx: Any, _params: Any) -> None:
            watch = getattr(ctx, "trace_request_ctx", None)
            if isinstance(watch, Watch):
                watch.mark(name)
        return on

    trace.on_connection_queued_start.append(hook("pool_wait_start"))
    trace.on_connection_queued_end.append(hook("pool_wait_end"))
    trace.on_dns_resolvehost_start.append(hook("dns_start"))
    trace.on_dns_resolvehost_end.append(hook("dns_end"))
    trace.on_connection_create_start.append(hook("connect_start"))
    trace.on_connection_create_end.append(hook("connect_end"))
    trace.on_connection_reuseconn.append(hook("connection_reused"))
    trace.on_request_headers_sent.append(hook("request_headers_sent"))

    async def chunk(_session: Any, ctx: Any, _params: Any) -> None:
        watch = getattr(ctx, "trace_request_ctx", None)
        if isinstance(watch, Watch):
            watch._ms["body_sent"] = _ms(watch.started)          # the last chunk's time wins

    trace.on_request_chunk_sent.append(chunk)
    return trace
