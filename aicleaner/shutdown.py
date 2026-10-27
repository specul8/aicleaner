"""Leaving promptly when Windows logs off or shuts down (stability spec S4).

The proxy and the brokers are console processes in hidden windows. Windows tells a console process about a logoff
or shutdown with a console control event; Python leaves those to the default handler, which ended the process only
after Windows had waited on it - 22 "is delaying system shutdown" events in five days, nine aicleaner processes at
once on 2026-10-24. Here a handler answers the event: it records why (the lifecycle log), gives the caller a moment
to finish writing, and ends the process at once.

Windows only; elsewhere `install` does nothing.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

CTRL_CLOSE = 2
CTRL_LOGOFF = 5
CTRL_SHUTDOWN = 6
EVENTS = {CTRL_CLOSE: "close", CTRL_LOGOFF: "logoff", CTRL_SHUTDOWN: "shutdown"}
BUDGET_S = 1.5               # Windows waits about five seconds; the work here must take well under that

_installed: list[Any] = []   # the ctypes callback, held so it is not collected while Windows can still call it


def in_service() -> bool:
    """Run by the start-up task (feature 107): not in anyone's sign-in session, so a sign-out is not ours to answer.
    Windows sends a console process the logoff event whoever signs out - the reason services ignore it."""
    return bool(os.environ.get("AICLEANER_SERVICE", "").strip())


def handler(on_exit: Callable[[str], None], exit_now: Callable[[int], None] = os._exit,
            ignore_logoff: bool = False) -> Callable[[int], bool]:
    """The function Windows calls. Unknown events (Ctrl+C, Ctrl+Break) are left to Python - returns False. With
    ignore_logoff, a logoff is answered (so the default handler does not end the process) and nothing else done."""
    def handle(event: int) -> bool:
        kind = EVENTS.get(int(event))
        if kind is None:
            return False
        if kind == "logoff" and ignore_logoff:
            return True
        worker = threading.Thread(target=_safely, args=(on_exit, kind), daemon=True)
        worker.start()
        worker.join(BUDGET_S)
        exit_now(0)
        return True
    return handle


def _safely(on_exit: Callable[[str], None], kind: str) -> None:
    try:
        on_exit(kind)
    except Exception as exc:          # leaving matters more than the record of leaving
        log.debug("shutdown handler failed: %s", exc)


def install(on_exit: Callable[[str], None]) -> bool:
    """Answer logoff, shutdown and console-close events with `on_exit(kind)`, then leave. True when installed."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    routine = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler(on_exit, ignore_logoff=in_service()))
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(routine, True):
        return False
    _installed.append(routine)
    return True
