"""One folder scan at a time, started from a page instead of a terminal.

The commands that maintain the mapping - `scan`, `preflight` - are not hard to run. They are easy
to forget, because the moment they are worth running is the moment a client is created or a
second repo turns up, and at that moment you are on that client's page rather than in a terminal
thinking about maintenance.

A scan reads a whole folder, so it is a job rather than a request: it is started, it runs off the
event loop, and the page asks how it went. One runs at a time - a second start while one is
running is refused rather than queued, because two scans writing to the mapping at once is a
question nobody needs to have answered.

The report is the command line's own. `_scan_roots` takes a collector, so what a page shows is
what `aicleaner scan` would have printed, not a second implementation of it that can drift.
"""
from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("scan", "preflight")
MODES = ("report", "add", "queue")
MAX_LINES = 400          # a report, not a transcript
# What a job is doing while its state is "running". Kept apart from the state on purpose: state is
# the lifecycle every caller already tests for, and folding a fourth value into it made every
# `state == "running"` check read a busy job as a finished one.
PHASES = ("counting", "reading")


class ScanError(RuntimeError):
    """The request was refused: a bad folder, an unknown client, or one already running."""


@dataclass
class Job:
    id: str
    kind: str
    folder: str
    client: str
    mode: str
    state: str = "running"            # running | done | failed
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    code: int | None = None
    error: str = ""
    lines: list[str] = field(default_factory=list)
    # Measured over the files that will actually be read, so the bar means something. Counting is
    # called out as a phase because on a large repository it takes long enough to look like nothing
    # is happening, which is the complaint this whole thing exists to answer.
    phase: str = "counting"           # counting | reading | writing, while state is running
    total_files: int = 0
    total_bytes: int = 0
    done_files: int = 0
    done_bytes: int = 0
    now: str = ""
    # Writing is its own phase because it is not free: the bar reached 100% at the end of reading
    # and then sat there, for the better part of an hour, while the mapping was still being built.
    total_writes: int = 0
    done_writes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "folder": self.folder, "client": self.client,
                "mode": self.mode, "state": self.state, "started": self.started,
                "finished": self.finished, "code": self.code, "error": self.error,
                "lines": self.lines[:MAX_LINES],
                "truncated": len(self.lines) > MAX_LINES,
                "total_files": self.total_files, "total_bytes": self.total_bytes,
                "done_files": self.done_files, "done_bytes": self.done_bytes, "now": self.now,
                "total_writes": self.total_writes, "done_writes": self.done_writes,
                "phase": self.phase}


class ScanRunner:
    """Holds the one job there is. Nothing here touches the event loop."""

    def __init__(self, cfg, queue_store=None) -> None:
        self.cfg = cfg
        # The proxy's own candidate store, so a queued find is one the running proxy already has.
        # A second store over the same file writes the whole thing from its own copy and drops
        # whatever the first had.
        self.queue_store = queue_store
        self._lock = threading.Lock()
        self.job: Job | None = None

    # ---- what the page may ask for ------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"job": self.job.as_dict() if self.job else None,
                    "running": bool(self.job and self.job.state == "running")}

    def start(self, kind: str, folder: str, client: str = "", mode: str = "report",
              known: set[str] | None = None) -> dict[str, Any]:
        """Check everything that can be checked before anything is read, then run it."""
        if kind not in KINDS:
            raise ScanError(f"scan kind must be one of {', '.join(KINDS)}")
        if mode not in MODES:
            raise ScanError(f"mode must be one of {', '.join(MODES)}")
        where = Path(folder.strip('"').strip("'").strip()) if folder else Path()
        if not folder.strip() or not where.is_dir():
            raise ScanError(f"not a folder on this machine: {folder}")
        if mode in ("add", "queue") and not client:
            raise ScanError(f"{mode} needs a client")
        if client and known is not None and client not in known:
            raise ScanError(f"no client called {client}")
        with self._lock:
            if self.job and self.job.state == "running":
                raise ScanError(f"a {self.job.kind} of {self.job.folder} is still running")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, folder=str(where), client=client, mode=mode)
            self.job = job
        threading.Thread(target=self._run, args=(job,), name=f"aicleaner-{kind}", daemon=True).start()
        return job.as_dict()

    # ---- the work ------------------------------------------------------------------------

    def _run(self, job: Job) -> None:
        """Run the scan as a separate process, below normal priority, and follow its progress.

        In-process, even with `proxy.bypass` on, replies slowed to a crawl while a scan ran:
        bypass removes the filtering work, but the scan still held the GIL through its regex work
        and hammered the disk in the process the proxy serves from (D14). A thread cannot fix
        that - Python threads give no parallelism for CPU-bound work, and one cannot usefully be
        prioritised below another while they share an interpreter. A separate process can be
        descheduled by the operating system, which is the whole point.

        The command is the one the CLI already offers, so what the page shows is what
        `aicleaner scan` prints rather than a second implementation that can drift. Progress
        crosses on stderr as JSON lines (__main__._progress_channel); stdout is the report.
        """
        import subprocess

        from .scan import iter_text_files, transcript_folders

        child = None
        try:
            root = Path(job.folder)
            # The denominator, worked out here rather than in the child: it reads no file
            # contents, only directory entries and sizes, so it is cheap - and having it before
            # the scan starts is what lets the bar mean anything from the first file.
            roots = [root, *transcript_folders(root)] if job.kind == "preflight" else [root]

            def count(_path: Path, size: int) -> None:
                job.total_files += 1
                job.total_bytes += size

            for where in roots:
                for _path in iter_text_files(where, None, count):
                    pass
            job.phase = "reading"

            command = [sys.executable, "-m", "aicleaner",
                       "preflight" if job.kind == "preflight" else "scan", str(root),
                       "--min-count", "2", "--top", str(MAX_LINES), "--progress"]
            if getattr(self.cfg, "path", None):
                command += ["--config", str(self.cfg.path)]
            if job.mode == "add":
                command += [f"--add={job.client}"]
            elif job.mode == "queue":
                command += [f"--queue={job.client}"]

            # Below normal so the proxy keeps the CPU when both want it. CREATE_NO_WINDOW rather
            # than DETACHED_PROCESS: the latter leaves a console application with no console at
            # all, and a child started that way exits 0 having run nothing.
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW

            child = subprocess.Popen(
                command, cwd=str(Path(__file__).resolve().parent.parent),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", creationflags=flags)

            # communicate() would read stderr itself, racing the watcher for the same pipe and
            # losing most of the progress. So each stream has one reader: the watcher takes
            # stderr, this thread takes stdout, and wait() collects the code once both are done.
            watcher = threading.Thread(target=self._follow_progress, args=(job, child),
                                       name="aicleaner-scan-progress", daemon=True)
            watcher.start()
            out = child.stdout.read() if child.stdout else ""
            child.wait()
            watcher.join(timeout=5)

            for line in (out or "").splitlines():
                if len(job.lines) < MAX_LINES:
                    job.lines.append(line)
            job.code = child.returncode
            # The child queued its finds through a store of its own, so this one is a scan behind.
            # Without this the Review page shows nothing new until the proxy restarts.
            if job.mode == "queue" and self.queue_store is not None:
                try:
                    self.queue_store.reload()
                except Exception:             # a stale view is better than a failed scan
                    pass
            job.now = ""
            job.phase = ""
            job.state = "done" if child.returncode == 0 else "failed"
            if child.returncode != 0 and not job.error:
                job.error = f"the scan exited with code {child.returncode}"
        except Exception as exc:              # a scan must never take the proxy with it
            job.state = "failed"
            job.phase = ""
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished = time.time()

    def _follow_progress(self, job: Job, child) -> None:
        """Turn the child's progress lines into the numbers the page is already drawing."""
        import json

        from .__main__ import PROGRESS_MARK

        try:
            for line in child.stderr:
                if not line.startswith(PROGRESS_MARK):
                    continue                  # a warning or a traceback: not ours to read
                try:
                    note = json.loads(line[len(PROGRESS_MARK):])
                except ValueError:
                    continue
                if note.get("kind") == "file":
                    job.phase = "reading"
                    job.done_files += 1
                    job.done_bytes += int(note.get("size") or 0)
                    job.now = str(note.get("name") or "")
                elif note.get("kind") == "write":
                    job.phase = "writing"
                    job.total_writes = int(note.get("total") or 0)
                    job.done_writes = int(note.get("done") or 0)
                    job.now = str(note.get("client") or "")
        except Exception:                     # following progress must never fail the scan
            pass
