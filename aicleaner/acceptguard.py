"""A dropped connection must never stop a listener (D198, 2026-10-21).

On Windows, asyncio's proactor loop gives up on a listening socket the first time an accept fails: its
`_start_serving` catches the OSError, logs "Accept failed on a socket" and closes the socket. A client that
resets its connection before the accept completes is enough - Windows reports that as WinError 64 (the
network name is no longer available) - and the process stays up with nothing listening, so every AI tool
gets "Connection refused" until aicleaner is restarted. Seen on 2026-10-21 and reproduced: 400 connections
reset at once stopped port 8787 on the first try.

`install` wraps the loop's proactor `accept` so that a connection-level failure is answered by accepting
the next connection instead. Anything else - the listener itself closed, an error not on the list - is
passed on unchanged, so closing a server still works as before.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# Windows' and Winsock's codes for "this one connection went away", never "the listener is broken".
TRANSIENT_WINERRORS = {
    64,      # ERROR_NETNAME_DELETED: the client reset before the accept completed
    121,     # ERROR_SEM_TIMEOUT
    1236,    # ERROR_CONNECTION_ABORTED
    10053,   # WSAECONNABORTED
    10054,   # WSAECONNRESET
}


def transient(error: BaseException | None) -> bool:
    return isinstance(error, OSError) and getattr(error, "winerror", None) in TRANSIENT_WINERRORS


def install(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Guard the loop's accepts; True when installed, False on a loop with no proactor (not Windows) or
    one already guarded."""
    loop = loop or asyncio.get_running_loop()
    proactor = getattr(loop, "_proactor", None)
    if proactor is None or getattr(proactor, "_aicleaner_accept_guarded", False):
        return False
    real_accept = proactor.accept

    def accept(listener: Any) -> asyncio.Future:
        outer: asyncio.Future = loop.create_future()
        current: dict[str, asyncio.Future] = {}

        def attempt() -> None:
            inner = real_accept(listener)
            current["inner"] = inner
            inner.add_done_callback(finished)

        def finished(inner: asyncio.Future) -> None:
            if outer.done():                     # the server stopped meanwhile: close what arrived
                if not inner.cancelled() and inner.exception() is None:
                    inner.result()[0].close()
                return
            if inner.cancelled():
                outer.cancel()
                return
            error = inner.exception()
            if error is None:
                outer.set_result(inner.result())
            elif transient(error) and listener.fileno() != -1:
                log.info("a connection went away while it was being accepted (WinError %s); still listening",
                         error.winerror)        # type: ignore[attr-defined]
                attempt()
            else:
                outer.set_exception(error)

        outer.add_done_callback(lambda done: current["inner"].cancel() if done.cancelled() else None)
        attempt()
        return outer

    proactor.accept = accept
    proactor._aicleaner_accept_guarded = True
    _quieten(loop)
    return True


def _quieten(loop: asyncio.AbstractEventLoop) -> None:
    """The proactor's own accept helper is a task that re-raises the failure nobody awaits, so each dropped
    connection would also log "Task exception was never retrieved". Those - and only those, for the
    transient codes - are dropped; everything else goes to the handler that was there before."""
    previous = loop.get_exception_handler()

    def handler(this_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if context.get("message") == "Task exception was never retrieved" and transient(context.get("exception")):
            return
        if previous is not None:
            previous(this_loop, context)
        else:
            this_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
