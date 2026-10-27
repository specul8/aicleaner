"""The lifecycle log: how sessions start and end, what they leave behind, and when the machine paused.

One JSON line per event in `logs/lifecycle-<host>-<yyyymmdd>.jsonl` (spec 2026-10-24-session-lifecycle-
instrumentation.md). Written by the proxy and by each broker - a broker is its own process and outlives proxy
restarts, so it appends to the same file itself (a line written with one append call is not interleaved). Real
values, local only, like every other log here.

`Monitor` is the proxy's side: a heartbeat that notices pauses (a host asleep freezes the VM; the guest is not told),
and a snapshot every few minutes of memory, each session's job and any orphaned processes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import proctree

log = logging.getLogger(__name__)

HEARTBEAT_S = 30
PAUSE_S = 90                 # a heartbeat this late means the loop or the machine stopped
SNAPSHOT_S = 300
RESOURCES_S = 60             # each session's memory, for the Session page (stability spec S2)
LOW_MEMORY_MB = 2048         # commit headroom below which starting a session asks first
KEEP_DAYS = 30

_dir: Path | None = None
_host = (socket.gethostname().split(".")[0].lower() or "host")
# sessions.job_cleanup: whether a new session's job ends its processes when its broker goes. Off by default: with it
# off the job only counts and measures, and nothing behaves differently from before.
job_cleanup = False
# session.max_processes / session.max_memory_gb: each new session's job is capped at these (0 = no cap) - D208, a
# runaway in one session must not take the machine with it.
max_processes = 400
max_memory_mb = 16 * 1024


def configure(log_dir: Path | str | None, cleanup: bool = False, processes: int = 400, memory_gb: float = 16) -> None:
    global _dir, job_cleanup
    _dir = Path(log_dir) if log_dir else None
    job_cleanup = bool(cleanup)
    set_limits(processes, memory_gb)


def set_limits(processes: int, memory_gb: float) -> None:
    global max_processes, max_memory_mb
    max_processes = max(0, int(processes or 0))
    max_memory_mb = max(0, int(float(memory_gb or 0) * 1024))


def directory() -> str:
    return str(_dir) if _dir else ""


def path_for(folder: Path, when: float | None = None) -> Path:
    return folder / f"lifecycle-{_host}-{time.strftime('%Y%m%d', time.localtime(when))}.jsonl"


def record(event: str, folder: Path | str | None = None, **fields: Any) -> dict[str, Any]:
    """Append one event. Never raises: a log that cannot be written must not stop a session."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "t": round(time.time(), 3), "event": event, **fields}
    target = Path(folder) if folder else _dir
    if target is None:
        return entry
    try:
        target.mkdir(parents=True, exist_ok=True)
        with path_for(target).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.debug("lifecycle log not written: %s", exc)
    return entry


def read(folder: Path, days: int = 1, events: Iterable[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
    """The newest events of the last `days` days, newest first."""
    wanted = set(events) if events else None
    out: list[dict[str, Any]] = []
    now = time.time()
    for back in range(days):
        path = path_for(folder, now - back * 86400)
        if not path.is_file():
            continue
        for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if wanted is None or entry.get("event") in wanted:
                out.append(entry)
                if len(out) >= limit:
                    return out
    return out


def prune(folder: Path, keep_days: int = KEEP_DAYS) -> None:
    cutoff = time.time() - keep_days * 86400
    for path in folder.glob(f"lifecycle-{_host}-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def low_memory(memory: dict[str, Any], limit_mb: int = LOW_MEMORY_MB) -> bool:
    """Whether the machine is short enough of commit that starting another session should be asked about first."""
    if not memory.get("commit_limit_mb"):
        return False
    return memory["commit_limit_mb"] - memory.get("commit_mb", 0) < limit_mb


def pause_kind(gap_wall: float, gap_mono: float, limit: float = PAUSE_S) -> str:
    """What a late heartbeat means: "" (on time), "frozen" (the wall clock moved on far more than this process's
    own clock - a host asleep, a VM saved or paused), or "stalled" (both moved on: the loop or the machine was
    too busy to run it)."""
    if gap_wall <= limit and gap_mono <= limit:
        return ""
    return "frozen" if gap_wall - gap_mono > limit else "stalled"


def proc_view(proc: proctree.Proc) -> dict[str, Any]:
    return {"pid": proc.pid, "ppid": proc.ppid, "name": proc.name, "path": proc.path,
            "age_min": round((time.time() - proc.created) / 60, 1) if proc.created else None,
            "private_mb": proc.private_mb}


class Monitor:
    """The proxy's watcher. `sessions()` returns, for each live session, {"name", "broker_pid", "cli_pid", "job"};
    `owners()` the pids that legitimately own session processes (the proxy, the brokers)."""

    def __init__(self, sessions: Callable[[], list[dict[str, Any]]], owners: Callable[[], Iterable[int]],
                 venv: str = "") -> None:
        self.sessions = sessions
        self.owners = owners
        self.venv = venv or str(Path(sys.executable).resolve().parent.parent)
        self.known_orphans: dict[int, dict[str, Any]] = {}
        self.last_pause: dict[str, Any] | None = None
        self.last_snapshot: dict[str, Any] = {}
        self.on_resources: Callable[[dict[str, dict[str, Any]], dict[str, Any]], None] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        wall, mono = time.time(), time.monotonic()
        next_snapshot = 0.0
        next_resources = 0.0
        if _dir is not None:
            prune(_dir)
        record("proxy-start", pid=os.getpid(), memory=proctree.memory())
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            now_wall, now_mono = time.time(), time.monotonic()
            gap_wall, gap_mono = now_wall - wall, now_mono - mono
            pause = pause_kind(gap_wall, gap_mono)
            if pause:
                self.last_pause = record("pause", wall_s=round(gap_wall), monotonic_s=round(gap_mono), kind=pause,
                                         since=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(wall)))
                next_snapshot = 0.0           # what the pause left behind is the thing to see
            wall, mono = now_wall, now_mono
            if self.on_resources is not None and now_mono >= next_resources:
                next_resources = now_mono + RESOURCES_S
                try:
                    self.on_resources(*(await asyncio.to_thread(self.resources)))
                except Exception as exc:      # the page's numbers are a nicety; never the proxy's problem
                    log.debug("session resources not read: %s", exc)
            if now_mono >= next_snapshot:
                next_snapshot = now_mono + SNAPSHOT_S
                try:
                    await asyncio.to_thread(self.snapshot)
                except Exception as exc:      # the watcher must never take the proxy down
                    log.warning("lifecycle snapshot failed: %s", exc)

    def resources(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Each running session's processes and memory now (the private bytes of every process in its job), and
        the machine's memory - what the Session page shows beside each session and checks before a start."""
        sessions = self.sessions()
        snap = proctree.snapshot()            # every process: a job holds whatever its tools run
        out: dict[str, dict[str, Any]] = {}
        for view in sessions:
            job = view.get("job") or {}
            pids = [p for p in job.get("pids", []) if p in snap.procs]
            out[view["name"]] = {"processes": len(pids) if job else None,
                                 "memory_mb": round(sum(snap.procs[p].private_mb for p in pids)) if job else None,
                                 "peak_mb": job.get("peak_job_mb"), "at": snap.taken}
        return out, snap.memory

    def snapshot(self) -> dict[str, Any]:
        """Memory, each session's processes, and orphans new since the last look (logged once; `gone` when they end)."""
        snap = proctree.snapshot(proctree.SESSION_KINDS)
        sessions = self.sessions()
        owners = set(self.owners())
        for session in sessions:
            owners.update(p for p in (session.get("broker_pid"), session.get("cli_pid")) if p)
        found = {p.pid: p for p in proctree.orphans(snap, owners, self.venv)}
        for pid, proc in found.items():
            if pid not in self.known_orphans:
                view = proc_view(proc)
                view["tree"] = [proc_view(child) for child in proctree.descendants(snap, pid)]
                view["tree_mb"] = round(proc.private_mb + sum(c["private_mb"] for c in view["tree"]), 1)
                self.known_orphans[pid] = view
                record("orphan", **view)
        for pid in [p for p in self.known_orphans if p not in found]:
            record("gone", pid=pid, name=self.known_orphans.pop(pid)["name"])
        summary = {"memory": snap.memory, "sessions": sessions,
                   "orphans": list(self.known_orphans.values()),
                   "orphan_mb": round(sum(o.get("tree_mb", 0) for o in self.known_orphans.values()), 1)}
        record("snapshot", **summary)
        self.last_snapshot = {**summary, "t": snap.taken, "last_pause": self.last_pause}
        return self.last_snapshot
