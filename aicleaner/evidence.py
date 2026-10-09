"""Evidence log: one JSON line per proxied request, counts only, never content.

This is the artefact that shows client identifiers did not leave the machine:
how many substitutions happened per client id in each direction, and whether
the outbound canary found anything.

Beside it, the substitution log (logs/detail/substitutions-<host>-<date>.jsonl.gz) holds the
values: one JSON line per event - what was replaced and by what, whose it is, what was done, and
who was driving - plus one line per change made through the UI (DECISIONS 38). It stays on this
machine, is read only by the Log page, and is deleted after logs.detail_retention_days.
"""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SubCounter(Counter):
    """Per-client counts plus the exact (client, real, token) pairs behind them."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.pairs: Counter[tuple[str, str, str]] = Counter()


class EvidenceLog:
    def __init__(self, log_dir: Path, detail: bool = True, detail_retention_days: int = 90,
                 every_request: bool = False):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.detail = detail
        # The substitution log records a value the first time a session sends it and again only when
        # what happens to it changes (DECISIONS 38): Claude Code re-sends the whole conversation every
        # turn, so logging every request logs the same replacements on every one. True logs them all.
        self.every_request = every_request
        self._seen: dict[tuple[str, str, str], tuple[str, str]] = {}
        # Kinds stood in automatically beyond GUIDs (the quiet ones, DECISIONS 43); set by the proxy.
        self.stood_in_kinds: frozenset[str] = frozenset()
        self.detail_dir = self.log_dir / "detail"
        self._lock = threading.Lock()
        if self.detail:
            self.detail_dir.mkdir(parents=True, exist_ok=True)
            self._prune(detail_retention_days)
        # Per-host file names: the log folder may be synced between machines.
        self.host = socket.gethostname().split(".")[0].lower() or "host"

    def _path(self) -> Path:
        return self.log_dir / f"evidence-{self.host}-{datetime.now():%Y%m%d}.jsonl"

    def _detail_path(self) -> Path:
        return self.detail_dir / f"substitutions-{self.host}-{datetime.now():%Y%m%d}.jsonl.gz"

    def _prune(self, days: int) -> None:
        """Delete detail files older than the retention window (they hold real identifiers)."""
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        for p in self.detail_dir.glob("substitutions-*.jsonl.gz"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    @staticmethod
    def read_detail(path: Path) -> list[dict[str, Any]]:
        """Decode a detail file (gzip members appended per request are read as one stream)."""
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def record(
        self,
        *,
        dialect: str,
        method: str,
        path: str,
        status: int,
        outbound: Counter[str],
        inbound: Counter[str],
        canary: list[str],
        streamed: bool,
        started: float,
        note: str | None = None,
        kept: int = 0,
        auth: str = "passthrough",
        secrets: Counter[str] | None = None,
        auto_added: list[dict[str, str]] | None = None,
        held_back: dict[str, int] | None = None,
        attachments: dict[str, int] | None = None,
        near_miss: int = 0,
        session: str = "",
        session_client: str = "",
        request_id: str = "",
        who: dict[str, Any] | None = None,
        held: Counter[str] | None = None,
        project: str = "",
        unprotected: list[tuple[str, str]] | None = None,
        declined: list[tuple[str, str, str]] | None = None,
        canary_matches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "host": self.host,
            "dialect": dialect,
            "auth": auth,
            "method": method,
            "path": path,
            "status": status,
            "outbound": dict(outbound),
            "inbound": dict(inbound),
            "canary": "leak" if canary else "clean",
            "leaked_clients": canary,
            "streamed": streamed,
            "kept_tokenised_tool_calls": kept,
            "secrets_redacted": dict(secrets or {}),
            "auto_added": [{"client": a["client"], "kind": a["kind"]} for a in (auto_added or [])],
            "held_back": dict(held_back or {}),
            "attachments": dict(attachments or {}),
            "near_miss": near_miss,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if session:
            # A log is evidence, so it names the engagement the way the owner does (ACME), with
            # the client id beside it for anything that reads by id. Absent for ordinary traffic.
            entry["session"] = session
            entry["session_client"] = session_client
        if note:
            entry["note"] = note
        if request_id:
            entry["request"] = request_id
        # Per client, how many occurrences a match rule declined to replace - they went out as
        # written, by the rule's decision, and the canary let them through (DECISIONS 39). A count,
        # so this log still carries no values.
        entry["rule_declined"] = dict(Counter(cid for cid, _found, _real in declined or []))
        if who:
            # Who was driving (DECISIONS 38). An operator's identity, not client data, so it belongs
            # here as well as in the substitution log.
            entry["who"] = who
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self._path().open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self.detail:
                base = {"ts": _local_now(), "session": session, "project": project,
                        "request": request_id, "dialect": dialect, "who": who or {}}
                events = self._substitutions(base, outbound, inbound, held, auto_added, unprotected, declined)
                # One event per match, with its surroundings (DECISIONS 40); a request the emergency
                # switch sent anyway is marked as such rather than as refused.
                action = "canary-refused" if status == 403 else "canary-fail-open"
                if canary_matches:
                    events += [{**base, "type": "substitution", "action": action, "direction": "out",
                                "client": m["client"], "real": m["found"], "token": "", "kind": "",
                                "where": f"{m['path']} ({m['where']})", "before": m["before"],
                                "after": m["after"]} for m in canary_matches]
                else:
                    events += [{**base, "type": "substitution", "action": action, "direction": "out",
                                "client": cid, "real": "", "token": "", "kind": ""} for cid in canary]
                if secrets:
                    events.append({**base, "type": "redacted", "counts": dict(secrets)})
                self._write(events)
        return entry

    def _substitutions(self, base: dict[str, Any], outbound: Counter[str], inbound: Counter[str],
                       held: Counter[str] | None, auto_added: list[dict[str, str]] | None,
                       unprotected: list[tuple[str, str]] | None,
                       declined: list[tuple[str, str, str]] | None = None) -> list[dict[str, Any]]:
        """One event per value and direction, less the ones this session has already logged the
        same way (unless every_request). Called with the lock held."""
        from .detect import STATELESS_KINDS
        from .mapping import PENDING_PREFIX, classify

        auto = {a.get("real", ""): a.get("kind", "") for a in auto_added or [] if a.get("real")}
        rows: list[tuple[str, str, str, str, int]] = []
        for source, direction in ((outbound, "out"), (held, "out"), (inbound, "back")):
            for (client, real, token), count in getattr(source, "pairs", {}).items():
                rows.append((direction, client, real, token, count))
        events: list[dict[str, Any]] = []
        for direction, client, real, token, count in rows:
            route = ""
            if client.startswith(PENDING_PREFIX):
                kind = client[len(PENDING_PREFIX):]
                client = ""
                action = "stand-in" if kind in (STATELESS_KINDS | self.stood_in_kinds) else "held-back"
            else:
                kind = classify(real)
                action = "replaced"
                if real in auto:
                    action = "auto-added"
                    route = "token-name" if auto[real] == "name" else auto[real]
            if direction == "back":
                action = "restored"
            event = {**base, "type": "substitution", "direction": direction, "real": real, "token": token,
                     "client": client, "kind": kind, "action": action, "count": count}
            if route:
                event["route"] = route
            events.append(event)
        for value, kind in unprotected or []:
            events.append({**base, "type": "substitution", "direction": "out", "real": value, "token": "",
                           "client": "", "kind": kind, "action": "sent-unprotected", "count": 1})
        for count_key, n in Counter(declined or []).items():
            cid, found, real = count_key
            events.append({**base, "type": "substitution", "direction": "out", "real": found, "token": "",
                           "client": cid, "kind": classify(real), "action": "rule-declined", "count": n,
                           "declared": real})
        if self.every_request:
            return events
        kept = []
        for event in events:
            key = (base["session"], event["direction"], event["real"])
            now = (event["token"], event["action"])
            if self._seen.get(key) != now:
                self._seen[key] = now
                kept.append(event)
        return kept

    def ui(self, *, who: dict[str, Any], method: str, path: str, status: int, body: Any) -> None:
        """A change made through the UI: who, what they called, what came back, what they sent."""
        if not self.detail:
            return
        event = {"ts": _local_now(), "type": "ui", "who": who, "method": method, "path": path,
                 "status": status, "body": body}
        with self._lock:
            self._write([event])

    def _write(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        with gzip.open(self._detail_path(), "at", encoding="utf-8") as gz:
            for event in events:
                gz.write(json.dumps(event, ensure_ascii=False) + "\n")


def _local_now() -> str:
    """Local time with its offset: a person reading the log reads local time."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
