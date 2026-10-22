"""Request guard: loopback Host, same-origin writes, JSON-only API writes, UI off unless loopback.

Closes review finding 1 (drive-by CSRF and DNS rebinding against the UI API). The port is not
compared: an attacking page always sends its own host name, and anything on a loopback port
already runs as this user. /api/tokenise and /api/rehydrate keep text/plain (the hook contract):
they change nothing and their answers cannot be read cross-origin.

One exception, off unless configured: a request arriving at the hostname of a Cloudflare tunnel
with a valid Access token (see access.py) may use the UI and the session API, so a session can be
driven from a phone. It may never use the model routes: the tunnel is for driving a session, not
for proxying somebody else's traffic through this mapping.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .access import AccessError

log = logging.getLogger("aicleaner.guard")

ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
# Who Access says this is, for the handlers that want to log it.
ACCESS_EMAIL = web.RequestKey("access_email", str)
# The country Access records in the same token, for the substitution log's "who" (DECISIONS 38).
ACCESS_COUNTRY = web.RequestKey("access_country", str)
# True when the request came through the tunnel, for handlers that allow less from there.
REMOTE = web.RequestKey("remote", bool)
# A response's own security policy, for the one kind that is framed by the page: an image or PDF
# shown in a file tab (DECISIONS 50). Everything else gets CSP, which forbids all framing.
FILE_CSP = web.RequestKey("file_csp", str)

# Through the tunnel you can drive a session and sort the queue. You cannot take names out in bulk,
# put a mapping in, read a secret, or weaken the filter (H3). Restart stays: away from the machine
# it is often the only remedy, and it discloses nothing. Settings are narrowed per key in ui.py
# (REMOTE_SETTINGS).
#
# These were empty for a while on purpose, so a fault could be chased from a phone exactly as from
# the desk while the filter was being debugged. That is over: the tunnel is something to rely on
# now, and an open surface behind a better sign-in is a better lock on a propped-open door.
#
#   /api/secrets   switching redaction off (the mode) and deleting a saved secret - the list and adding
#                  one reach since H2 (Brad, 2026-10-19; REMOTE_WRITES below)
#   /api/rehydrate a token turned back into a name on demand - bulk extraction, one call at a time
#   /api/tokenise  confirms whether a given string is a client's, the same question backwards
#
# Export and import were here and are not any more (2026-09-26). The argument for refusing them
# was that export hands over every real identifier of a client in one call - true, but it is the
# wrong frame for this product. aicleaner is a personal tool: one person, their own machine,
# their own client data already sitting on it in the clients folder. A client file downloaded
# through the tunnel gets exactly the care every other copy of that data gets, because it is the
# same person looking after it. Refusing it bought no protection and cost a real job - trimming a
# client file down by hand to triage a bug is what export is for.
#
# Secrets stays refused where it weakens redaction, and the distinction is deliberate: an API key is
# not client data. Losing one is a different kind of loss, recoverable only by rotating it, and
# nobody needs to read a credential from a train. Since H2 (Brad, 2026-10-19): "one should be able to
# enter new secrets remotely and see which secrets are being replaced. In no situation can it be
# switched off remotely, and there's no way to 'see' secrets previously entered." The list carries
# labels and lengths only - no value ever comes back out of usersecrets - so listing and adding reach;
# the mode and deleting do not.
#
# Rehydrate and tokenise stay refused, settled 2026-09-26, and not by the same test as export.
# The question is not "is this data the person is entitled to" - it is - but "does reaching it
# from the tunnel do any good". Somebody working remotely has a browser on a phone or a laptop and
# no tooling; an endpoint whose value is being iterated over is of no use to them. So these are
# capability with no matching purpose, which is exactly the kind worth not exposing. Export is the
# opposite: a person clicks it once and gets a file they wanted.
REMOTE_REFUSED_PREFIXES: tuple[str, ...] = (
    "/api/secrets", "/api/tokenise", "/api/rehydrate",
    "/api/setup")      # the setup wizard runs this machine's scripts (feature 106)


# The reads the setup wizard draws itself with (E4, Brad 2026-10-19: "the setup wizard ... should be
# visible remotely" - what acts on this machine shown, but disabled). Reads only: every write under
# /api/setup, and its log, stay on this machine.
REMOTE_READS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/setup/remote/state"), ("GET", "/api/setup/dns"),
    # H2 (2026-10-19): which secrets are being replaced (labels only), and the mode, read not changed.
    ("GET", "/api/secrets"), ("GET", "/api/secrets/mode")})
# Writes under a refused prefix that reach anyway, each one named (H2): adding a saved secret strengthens
# redaction; it never weakens it, and the value goes in, never out.
REMOTE_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/secrets"),
    # Wizard step 4 (J2, Brad 2026-10-20): routing the AI tools through aicleaner - settings files only,
    # no prompt on the machine. Taking a tool OFF stays local (J4, Brad 2026-10-20: "it allows the AI
    # tool to bypass the proxy"), as does the remote-access set-up itself.
    ("POST", "/api/setup/enable")})


def refused_remotely(path: str, method: str = "") -> bool:
    """True for what is never served through remote access. A caller that names no method is refused
    as before - only a named read or write on the lists is let through."""
    if (method.upper(), path) in REMOTE_READS | REMOTE_WRITES:
        return False
    return path.startswith(REMOTE_REFUSED_PREFIXES)

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
UI_EXACT = {"/", "/ui", "/ui/", "/config", "/secrets"}
TEXT_API = {"/api/tokenise", "/api/rehydrate"}
# blob: for images and frames (D168): through the encrypted tunnel a file tab's image or PDF arrives
# sealed and is shown from an object URL the page made itself - nothing from outside can name one.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "frame-src 'self' blob:; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
# The page itself (feature 139): a fresh nonce per load, which the file editor (Monaco) puts on the <style>
# elements it makes. Inline styles stay forbidden - only an element carrying this load's nonce is allowed.
STYLE_NONCE = web.RequestKey("style_nonce", str)


def csp_for(request: web.Request) -> str:
    """The page's policy (feature 139, Monaco). <style> elements: only this load's nonce. Style *attributes*
    allowed - Monaco positions every line of text with one, in HTML it builds, with no hook to avoid it; an
    attribute cannot fetch anything (images, fonts and connections stay 'self'), so injected markup still could
    not reach out. Workers from blob: URLs, which is how Monaco starts its worker. Scripts unchanged: 'self'."""
    nonce = request.get(STYLE_NONCE)
    if not nonce:
        return CSP
    return (CSP.replace("style-src 'self'", f"style-src 'self' 'nonce-{nonce}'; style-src-attr 'unsafe-inline'")
            .replace("worker-src 'self'", "worker-src 'self' blob:"))


def host_name(value: str) -> str:
    """'localhost:8787' -> 'localhost'; '[::1]:8787' -> '::1'; lower-cased."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.index("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def is_loopback_listen(listen: str) -> bool:
    if listen.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(listen.strip()).is_loopback
    except ValueError:
        return False


def is_ui_path(path: str) -> bool:
    return path in UI_EXACT or path.startswith(("/api/", "/web/"))


def origin_ok(origin: str, remote_hostname: str = "") -> bool:
    """Same-origin writes only: a loopback page, or the tunnel's own hostname over https."""
    parts = urlsplit(origin)
    host = (parts.hostname or "").lower()
    if remote_hostname and host == remote_hostname.lower():
        return parts.scheme == "https"
    return parts.scheme == "http" and host in LOOPBACK_NAMES


def make_guard(listen: str, remote_hostname: str = "", verifier: Any = None) -> Any:
    """Build the middleware for a given listen address (fixed for the life of the process).

    `remote_hostname` and `verifier` switch on the tunnel exception; without them nothing changes.
    """
    loopback = is_loopback_listen(listen)
    remote_hostname = (remote_hostname or "").strip().lower()

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        path = request.path
        ui = is_ui_path(path)
        arriving = host_name(request.headers.get("Host", ""))
        remote = bool(verifier) and bool(remote_hostname) and arriving == remote_hostname
        if remote:
            # Reached through the tunnel: the session page and its API, nothing else, and only with
            # a token Cloudflare Access signed for this application.
            if not ui:
                return web.Response(status=403, text="aicleaner: only the UI is reachable from outside this "
                                                     "machine; model traffic stays local")
            try:
                # Off the loop (H15): a token naming a key not yet seen makes the verifier fetch the
                # team's keys, up to ten seconds, which held every request on the proxy meanwhile.
                claims = await asyncio.to_thread(verifier.verify, request.headers.get(ACCESS_HEADER, ""))
            except AccessError as exc:
                log.warning("refused a request at %s: %s", arriving, exc)
                return web.Response(status=403, text=f"aicleaner: {exc}")
            except (OSError, ValueError) as exc:
                # Cloudflare's signing keys could not be fetched (a network blip, a bad answer): not a
                # refusal of this person, so not a 403 - a 503 the page treats as "try again" - and
                # never the uncaught 500 it used to be (2026-10-14 review).
                log.warning("could not check a request at %s: %s", arriving, exc)
                return web.json_response({"error": "aicleaner could not check the sign-in just now"}, status=503)
            request[ACCESS_EMAIL] = str(claims.get("email", "")).lower()
            request[ACCESS_COUNTRY] = str(claims.get("country", ""))
            request[REMOTE] = True
            if refused_remotely(path, request.method):
                return web.json_response({"error": "local only: open this page on the machine aicleaner runs "
                                                   "on to use it"}, status=403)
        if ui and not loopback and not remote:
            return web.Response(status=403, text="aicleaner: the UI and API are only served when listen is a "
                                                 "loopback address (see aicleaner-config.json)")
        if loopback and not remote and arriving not in LOOPBACK_NAMES:
            return web.Response(status=421, text="aicleaner: use a loopback host name (127.0.0.1, localhost or [::1])")
        # A read another site's page asks for - an <img> or <script> pointing at the API - never
        # reaches a handler (H27). Browsers say so; the page itself, a typed address and curl do not.
        if path.startswith("/api/") and request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return web.Response(status=403, text="aicleaner: cross-site request refused")
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None and not origin_ok(origin, remote_hostname if remote else ""):
                return web.Response(status=403, text="aicleaner: cross-origin request refused")
            if path.startswith("/api/") and path not in TEXT_API and request.content_type != "application/json":
                return web.Response(status=415, text="aicleaner: API writes must be application/json")
        response = await handler(request)
        if ui:
            response.headers["Content-Security-Policy"] = request.get(FILE_CSP) or csp_for(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            # no-store for the page's own files too, not just the API: through the tunnel, Cloudflare
            # and the remote browser each kept a copy, and a released page stayed invisible there while
            # this machine showed it. The files are small and local; nothing is gained by keeping them.
            response.headers["Cache-Control"] = "no-store"
        return response

    return guard
