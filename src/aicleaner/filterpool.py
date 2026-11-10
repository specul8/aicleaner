"""The filter's worker processes (spec 2026-11-10-incremental-filtering.md part 4, decided by Brad 2026-11-10).

Python's threads share one core for this work, so a request with a lot of new text - the first after a restart, a
huge pasted log - is split across processes: min(8, cores) of them, started with AI Cleaner and kept (a ceiling,
not a reservation: an idle worker costs nothing). Each holds the mapping it filters with, sent once per filter
generation rather than with every request. A request's new units go out in contiguous batches and come back in
document order, so the result is the one a single thread gives.

Nothing here is needed for the filter to be right: a worker that dies is replaced and its batch is done in the
request's own thread; a pool that cannot start, or is busy with another request, leaves everything to that thread;
the canary's whole-body read never depends on a worker. Windows starts workers with spawn. A worker whose proxy
goes away reads the end of its pipe and leaves.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import threading
import time
from collections import OrderedDict, deque
from multiprocessing.connection import wait
from typing import Any, Callable

log = logging.getLogger("aicleaner.filterpool")

POOL_MAX = 8                    # Brad 2026-11-10: eight workers, or the machine's cores if fewer
KEEP_GENERATIONS = 4            # mappings a worker keeps (the base mapping, its placeholder ones, the last one)
BATCHES_PER_WORKER = 3          # smaller batches even out a worker that drew the long strings
DEADLINE_S = 120.0              # a batch not back by then is taken as lost: the worker is replaced
MAX_REPLACEMENTS = 20           # workers that die this often are not started again: the pool is off


def pool_size() -> int:
    return max(1, min(POOL_MAX, os.cpu_count() or 1))


class _Slot:
    """One worker process and its end of the pipe; the generations it holds, oldest first."""

    def __init__(self, process: Any, conn: Any) -> None:
        self.process = process
        self.conn = conn
        self.gens: deque[str] = deque(maxlen=KEEP_GENERATIONS)

    def alive(self) -> bool:
        return self.process is not None and self.process.is_alive()


class FilterPool:
    def __init__(self, size: int | None = None) -> None:
        self.size = size or pool_size()
        self._slots: list[_Slot | None] = []
        self._lock = threading.Lock()          # one request's batches at a time; another runs in its own thread
        self._state = threading.Lock()
        self.started = False
        self.broken = ""
        self.replaced = 0
        self.runs = 0

    @property
    def usable(self) -> bool:
        return self.started and not self.broken

    def start(self) -> None:
        with self._state:
            if self.started or self.broken:
                return
            try:
                self._slots = [self._spawn() for _ in range(self.size)]
                self.started = True
                log.info("filter pool: %d worker processes", self.size)
            except Exception as exc:  # noqa: BLE001 - the filter runs in one thread instead
                self.broken = f"{type(exc).__name__}"
                log.warning("filter pool could not start (%s): filtering in one thread", self.broken)
                self._close_slots()

    @staticmethod
    def _spawn() -> _Slot:
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        process = ctx.Process(target=_worker_main, args=(child,), name="aicleaner-filter", daemon=True)
        process.start()
        child.close()
        return _Slot(process, parent)

    def _replace(self, index: int) -> None:
        """A dead or lost worker ended and a new one started in its place."""
        slot = self._slots[index]
        if slot is not None:
            _end(slot)
        self._slots[index] = None
        self.replaced += 1
        if self.replaced > MAX_REPLACEMENTS:
            self.broken = "workers keep dying"
            log.warning("filter pool: %d workers replaced; filtering in one thread from now on", self.replaced)
            return
        try:
            self._slots[index] = self._spawn()
        except Exception as exc:  # noqa: BLE001
            log.warning("filter pool: a worker could not be replaced (%s)", type(exc).__name__)

    def warm(self, gen: str, payload: Callable[[], Any]) -> None:
        """Send a generation's mapping to every worker now, so the first request does not wait for it."""
        if not self.usable or not self._lock.acquire(blocking=False):
            return
        try:
            built = None
            for slot in self._slots:
                if slot is None or not slot.alive() or gen in slot.gens:
                    continue
                built = built if built is not None else payload()
                try:
                    slot.conn.send(("gen", gen, built))
                    slot.gens.append(gen)
                except (OSError, EOFError, ValueError):
                    pass
        finally:
            self._lock.release()

    def run(self, kind: str, gen: str | None, payload: Callable[[], Any] | None, items: list[Any],
            cancel: Any = None) -> list[Any] | None:
        """Each item's result in order, or None in place of one no worker returned; None for the whole call when the
        pool is not usable or another request has it. `cancel` (a threading.Event), once set, stops new batches being
        handed out - the client has gone - and the batches out are waited for; their results come back as usual."""
        if not self.usable or not items or not self._lock.acquire(blocking=False):
            return None
        try:
            self.runs += 1
            for i, slot in enumerate(self._slots):
                if slot is None or not slot.alive():
                    self._replace(i)
            if self.broken:
                return None
            return self._run(kind, gen, payload, items, cancel)
        finally:
            self._lock.release()

    def _run(self, kind: str, gen: str | None, payload: Callable[[], Any] | None, items: list[Any],
             cancel: Any = None) -> list[Any]:
        results: list[Any] = [None] * len(items)
        pending = deque(_batches(items, len(self._slots) * BATCHES_PER_WORKER))
        idle = [i for i, slot in enumerate(self._slots) if slot is not None and slot.alive()]
        busy: dict[Any, tuple[int, tuple[int, int]]] = {}
        built: Any = None
        deadline = time.monotonic() + DEADLINE_S
        lost: list[int] = []
        while pending or busy:
            if cancel is not None and cancel.is_set():
                pending.clear()               # nobody is waiting for the rest
            while pending and idle:
                index = idle.pop()
                slot = self._slots[index]
                start, end = pending.popleft()
                try:
                    if gen is not None and gen not in slot.gens:
                        built = built if built is not None else payload()
                        slot.conn.send(("gen", gen, built))
                        slot.gens.append(gen)
                    slot.conn.send(("run", kind, gen, items[start:end]))
                    busy[slot.conn] = (index, (start, end))
                except (OSError, EOFError, ValueError):
                    lost.append(index)        # its batch is done in the request's thread
            if not busy:
                break
            ready = wait(list(busy), timeout=max(0.0, deadline - time.monotonic()))
            if not ready:
                lost.extend(index for index, _batch in busy.values())
                log.warning("filter pool: %d batch(es) not back in %.0f s; done in one thread", len(busy), DEADLINE_S)
                busy.clear()
                break
            for conn in ready:
                index, (start, end) = busy.pop(conn)
                try:
                    status, out = conn.recv()
                except (OSError, EOFError):
                    lost.append(index)
                    continue
                if status == "ok" and len(out) == end - start:
                    results[start:end] = out
                else:
                    # The type of what went wrong, never its message: a mapping error names a real.
                    log.warning("filter pool: a worker could not do a batch (%s); done in one thread", out)
                idle.append(index)
        for index in set(lost):
            self._replace(index)
        return results

    def close(self) -> None:
        """Stop every worker: asked first, then ended (the proxy is stopping)."""
        with self._state:
            self._close_slots()
            self.started = False

    def _close_slots(self) -> None:
        for slot in self._slots:
            if slot is None:
                continue
            try:
                slot.conn.send(("stop",))
            except (OSError, EOFError, ValueError):
                pass
        for slot in self._slots:
            if slot is not None:
                _end(slot)
        self._slots = []


def _end(slot: _Slot) -> None:
    try:
        slot.process.join(timeout=2)
        if slot.process.is_alive():
            slot.process.terminate()
            slot.process.join(timeout=2)
    except Exception:  # noqa: BLE001 - stopping never fails the proxy's stop
        pass
    try:
        slot.conn.close()
    except OSError:
        pass


def _batches(items: list[Any], wanted: int) -> list[tuple[int, int]]:
    """Contiguous (start, end) ranges of about the same amount of text each, in document order."""
    sizes = [_item_size(item) for item in items]
    total = sum(sizes) or 1
    target = max(1, total // max(1, wanted))
    out: list[tuple[int, int]] = []
    start, running = 0, 0
    for i, size in enumerate(sizes):
        running += size
        if running >= target:
            out.append((start, i + 1))
            start, running = i + 1, 0
    if start < len(items):
        out.append((start, len(items)))
    return out


def _item_size(item: Any) -> int:
    if isinstance(item, str):
        return len(item) + 1
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], str):
        return len(item[1]) + 1
    return 1


def _worker_main(conn: Any) -> None:
    """A worker: build each generation's mapper as it arrives, keep the last few, filter batches with them. Ends when
    asked, or when the proxy's end of the pipe goes."""
    from .incremental import REDO, mapper_from_payload, run_unit
    from .mapping import VariantNeeded

    mappers: OrderedDict[str, Any] = OrderedDict()
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if not message or message[0] == "stop":
            return
        if message[0] == "gen":
            _op, gen, payload = message
            try:
                mappers[gen] = mapper_from_payload(payload)
            except Exception:  # noqa: BLE001 - reported when a batch asks for it
                mappers[gen] = None
            while len(mappers) > KEEP_GENERATIONS:
                mappers.popitem(last=False)
            continue
        _op, kind, gen, items = message
        mapper = mappers.get(gen) if gen is not None else None
        if gen is not None and mapper is None:
            _send(conn, ("error", "no mapping"))
            continue
        try:
            out = []
            for item in items:
                try:
                    out.append(run_unit(kind, mapper, item))
                except VariantNeeded:
                    out.append(REDO)
            _send(conn, ("ok", out))
        except Exception as exc:  # noqa: BLE001 - the batch is done in the proxy's thread instead
            _send(conn, ("error", type(exc).__name__))


def _send(conn: Any, message: Any) -> None:
    try:
        conn.send(message)
    except (OSError, EOFError):
        pass
