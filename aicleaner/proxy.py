"""The proxy itself.

Routes:
  /{dialect}/{tail}   forward to cfg.upstreams[dialect] + "/" + tail
  /healthz            liveness plus mapping stats (client ids only)
  /api/tokenise       POST text/plain -> tokenised text (for hooks / testing)
  /api/rehydrate      POST text/plain -> rehydrated text

Policy, in order:
  1. JSON request body: every string leaf tokenised; then the canary reads every
     decoded string and key (not signatures or base64, which are not text); any
     real form left -> 403, nothing sent upstream.
  2. Non-JSON request body: refused (403) unless the path is allow-listed.
     A body we cannot inspect is a body we do not forward.
  3. Responses: text/event-stream is rewritten event by event via the
     dialect; JSON bodies are rehydrated leaf by leaf; anything else passes.
"""

from __future__ import annotations

import asyncio

import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from typing import Any

import aiohttp
from aiohttp import web
from pathlib import Path

from .config import Config, session_profiles
from .attachments import scrub
from .convert import image_converter, pdftotext_converter
from .contact import ContactSource
from . import bashguard, who
from .guard import REMOTE, make_guard
from .tunnelcrypt import Sessions as TunnelSessions, TunnelSecret
from .tunnelgate import make_tunnel_gate
from .detect import (STATELESS_KINDS, CandidateStore, SeenContext,
                     carries_client_real, client_resources, find_candidates, make_placeholder, near_miss_words,
                     owned_emails, owned_values, request_project, token_names)
from .dialects import get_stream_rewriter
from .evidence import EvidenceLog, SubCounter
from .mapping import (ORIGIN_AUTO, PENDING_PREFIX, GLOBAL_ID, SOURCE_AUTO, Form, Mapper,
                      MapperHandle, MappingError, email_rule_for)
from .notify import Notifier, strip_footers_from_history
from .profiles import PROFILES_FILE, ProfileStore
from .shares import SHARES_FILE, ShareStore
from .redact import find_families, redact
from .secrets import read_secret
from .sse import SSEParser
from .store import MappingStore
from .transform import (canary, canary_matches, canary_sites, rule_declined, detection_text, dumps, rehydrate_message, strip_marks, tokenise_json,
                        walk_strings)
from .ui import build_ui_routes
from .usersecrets import UserSecrets

log = logging.getLogger("aicleaner")

# How Claude Code introduces itself, in the terminal and in the VS Code extension alike.
CLAUDE_CODE_AGENT = "claude-cli/"

# Hop-by-hop and framing headers are never forwarded in either direction.
_STRIP_REQUEST = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade",
    # The body is sent decoded (aiohttp inflated it), so its old encoding would make it unreadable;
    # and a browser's cookies are this machine's business, not the provider's (H30).
    "content-encoding", "cookie",
    # The client asks for br, gzip. Passing that on has the provider answer in Brotli, which
    # aiohttp cannot decode without a package that is not installed - so every reply came back
    # "Can not decode content-encoding: brotli (br)" and every request failed. Dropped here so
    # aiohttp asks for what it can actually read, which is the only thing this proxy can rehydrate
    # anyway: a body it cannot decompress is a body it cannot put names back into.
    "accept-encoding",
}
# Headers a hop added, never the client: Cloudflare's and any proxy's forwarding trail (H30).
_STRIP_REQUEST_PREFIXES = ("cf-", "x-forwarded-")
# Set when the emergency switch sent a request the canary would have refused, so the one evidence
# line for that request says so (H24).
_FAIL_OPEN = web.RequestKey("aicleaner_fail_open", list)
# Set when proxy.bypass forwarded a request untouched, so its evidence line says so.
_BYPASS = web.RequestKey("aicleaner_bypass", bool)
# Stream events that are the reply itself (DECISIONS 47). Not `ping` or `message_start`: a stream
# that has sent only those has not started answering.
_REPLY_EVENTS = frozenset({"content_block_start", "content_block_delta", "content_block_stop",
                           "message_delta", "message_stop", "error"})
# What the substitution log needs about one request: id, who, project, held and unprotected pairs.
_REQ = web.RequestKey("aicleaner_request", dict)
# UI calls that record view state, not a decision: logging them would bury the decisions.
_NOT_UI_ACTIONS = ("/api/tokenise", "/api/rehydrate", "/api/session/tree-state",
                   "/api/session/permission/request",
                   # the file tabs' 15-second changed-on-disk check: a poll, not a person
                   "/api/session/file/stat")
# Reads that are logged although they change nothing: a project file's content leaving the machine
# for a browser (DECISIONS 50). Who read or downloaded what is exactly what the log is for.
_LOGGED_READS = ("/api/session/file", "/api/session/file/raw", "/api/session/file/download")
_FILE_WRITE = "/api/session/file/write"
_READ_IMAGE = "/api/session/read-image"
# Where a body value is a credential. Everything on the Secrets endpoints is; elsewhere, a key
# that names one. "token" is withheld except where it is a client's token, which is not a secret.
_SECRET_PATHS = ("/api/secrets",)
_SECRET_KEY = re.compile(r"secret|password|passwd|credential|api_?key", re.IGNORECASE)
_TOKEN_IS_DATA = ("/api/clients", "/api/global", "/api/candidates", "/api/ignore", "/api/import")
# A mapper holding nothing: it replaces nothing on the way out and restores nothing on the way
# back, which is exactly what bypass means. Built once.
INERT = Mapper([])


# Credentials: read by nobody here, and a key that happens to spell a client's name is not a leak.
_CREDENTIAL_HEADERS = {"authorization", "x-api-key", "anthropic-api-key"}


def _outside_body(request: web.Request, tail: str) -> str:
    """Everything a request sends that is not its body - path tail, query, forwarded header values -
    URL-decoded, one item per line, for the canary (H6). None of it is tokenised: an upstream's URL
    and headers are not ours to rewrite, so a client name there means the request is refused."""
    from urllib.parse import unquote_plus
    parts = [unquote_plus(tail), unquote_plus(request.query_string or "")]
    parts += [value for name, value in request.headers.items()
              if _forwardable(name) and name.lower() not in _CREDENTIAL_HEADERS]
    return "\n".join(p for p in parts if p)


def _forwardable(name: str) -> bool:
    low = name.lower()
    return low not in _STRIP_REQUEST and not low.startswith(_STRIP_REQUEST_PREFIXES)


def _fail_open_of(request: web.Request) -> tuple[list[str], str | None]:
    leaked = request.get(_FAIL_OPEN, [])
    if request.get(_BYPASS):
        return leaked, "PROXY BYPASS: forwarded and returned untouched"
    return leaked, ("canary FAIL-OPEN: sent anyway" if leaked else None)
_STRIP_RESPONSE = {
    "content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive",
}


def _withheld(body: Any, path: str) -> Any:
    """A UI request body with its credentials replaced by "(withheld)", for the substitution log."""
    everything = path.startswith(_SECRET_PATHS)
    token_is_data = path.startswith(_TOKEN_IS_DATA)

    def walk(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {k: walk(v, str(k)) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        secret = everything or _SECRET_KEY.search(key) or (key.lower() == "token" and not token_is_data)
        return "(withheld)" if secret and node not in (None, "") else node

    logged = walk(body)
    # A saved file tab is logged by which file and how much, not by its content (DECISIONS 53):
    # autosave writes every few seconds, and the log would become a copy of every file edited.
    if path == _FILE_WRITE and isinstance(logged, dict) and isinstance(logged.get("text"), str):
        logged = {**logged, "text": f"({len(logged['text'])} characters)"}
    # An image read for the prompt (feature 113) is logged by name and size, not as base64.
    if path == _READ_IMAGE and isinstance(logged, dict) and isinstance(logged.get("data"), str):
        logged = {**logged, "data": f"({len(logged['data']) * 3 // 4} bytes)"}
    return logged


def request_state_for(session: str, open_requests: list[dict[str, Any]], now: float) -> dict[str, Any] | None:
    """The oldest open model request of one Session-page session, for its page (DECISIONS 47):
    when it started, when reply content last arrived, and how long it has waited for any."""
    mine = [r for r in open_requests if session and r.get("session") == session]
    if not mine:
        return None
    oldest = min(mine, key=lambda r: r["started"])
    since = oldest["content_at"] or oldest["started"]
    return {"started": oldest["started"], "content_at": oldest["content_at"], "waiting_s": round(now - since)}


def _is_json(content_type: str) -> bool:
    return "json" in content_type.lower()


def _without_our_marks(body: bytes, content_type: str, dialect: str) -> bytes:
    """The request with the green marks and review footers aicleaner added to earlier replies taken
    out of its history, and otherwise exactly as it came (D151).

    Bypass forwards traffic untouched, but these are not the client's: the filter put them there,
    and with it on they never reach the provider. Left in under bypass, the model copied the marks
    into its own replies as visible "[32;22m" text. Nothing of ours in it: the bytes go unchanged.
    """
    if not body or not _is_json(content_type):
        return body
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        return body
    cleaned = walk_strings(obj, strip_marks)
    if dialect == "anthropic":
        cleaned = strip_footers_from_history(cleaned)
    return body if cleaned == obj else dumps(cleaned)


class Proxy:
    def __init__(self, cfg: Config, mapper: Mapper | MapperHandle, evidence: EvidenceLog):
        self.cfg = cfg
        # A bare Mapper (tests) never reloads; a MapperHandle re-reads on mtime change.
        self._handle = mapper if isinstance(mapper, MapperHandle) else None
        self._static = mapper if isinstance(mapper, Mapper) else None
        if self._handle is not None:
            self._handle.store.author = who.form_author()
        self.evidence = evidence
        self._session: aiohttp.ClientSession | None = None
        # The MCP audit, re-run at start and at each session start (H11, DECISIONS 52). Until the
        # first run it knows no servers, and keep_patterns is the configured list plus connectors.
        from .mcp_watch import McpWatch
        # The same home the routing check reads, so a test server audits its own fixture and not
        # the machine of whoever runs the suite.
        client_home = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
        self.mcp = McpWatch(Path(cfg.state_dir) if cfg.state_dir else None,
                            home=Path(client_home) if client_home else None)
        # Candidates are per host: the folder may be OneDrive-synced between machines.
        self.store = (CandidateStore(Path(cfg.mapping).parent / f"candidates-{evidence.host}.jsonl")
                      if cfg.detect_enabled else None)
        if self.store is not None:
            self.store.quiet_kinds = frozenset(cfg.quiet_kinds)   # feature 31; the settings save keeps it current
            # Feature 76: the mapper as last built, read each time, so a client added after a wildcard
            # was accepted is protected from then on. Not self.mapper: that may reload client files
            # from disk, and this is asked with the store's lock held.
            self.store.protected = lambda value: carries_client_real(
                value, self._handle.mapper if self._handle is not None else self._static)
        self.notifier = Notifier(cfg.review_url, footer=cfg.notify_footer, toast=cfg.notify_toast)
        # The encrypted tunnel's secret and key (feature 21); the key is derived on first use.
        self.tunnel = TunnelSecret(Path(cfg.state_dir) if cfg.state_dir else Path(cfg.mapping).parent / "state")
        # The connections agreed through the tunnel, each with its own key; in memory only.
        self.tunnel_sessions = TunnelSessions()
        # Driving a Claude Code session from the UI, with its traffic pointed back at this proxy so
        # a session driven from a phone is filtered like one driven from the terminal.
        self.session = None
        # The file editor's PowerShell commands, compiled from this machine (feature 80). Started in
        # run(), not here: a proxy built by a test must not launch PowerShell.
        from .pscommands import PowerShellCommands
        self.ps_commands = PowerShellCommands(cfg.state_dir, enabled=cfg.editor_compile_powershell)
        # Diagnostics' timing log (feature 91), written only while timings are being recorded.
        from .timing import TimingLog
        self.timing_log = TimingLog(Path(cfg.log_dir))
        # Beside Global, for the same reason Global is there: a profile names folders on this
        # machine, and the clients folder is shared.
        self.profile_store = ProfileStore(Path(cfg.mapping).parent / PROFILES_FILE)
        # Each client's OneDrive share, beside the profiles for the same reason: it is where this
        # person's OneDrive keeps a folder, not client data.
        self.share_store = ShareStore(Path(cfg.mapping).parent / SHARES_FILE)
        self.profile_problem = ""
        # What the Overview needs to say something true about whether filtering is happening, as
        # opposed to whether it *could* (D37). A healthy mapping means the filter works; it says
        # nothing about whether anything is coming through it. Restart is also the remedy offered
        # for several things, and until now there was no way to tell whether one had happened.
        self.started_at = time.time()
        self.requests_seen = 0
        self.last_request_at = 0.0
        # Scans started from a page rather than a terminal: one at a time, off the loop.
        from .scanjob import ScanRunner

        self.scans = ScanRunner(cfg, queue_store=self.store)
        if cfg.session_enabled:
            self._build_session_host()
        # Always constructed so the mode can be switched on at runtime; consulted only when mode is redact.
        self.user_secrets = UserSecrets(cfg.user_secrets_path)
        # (mapping id, placeholder version) -> (placeholder-only mapper, mapping + placeholders)
        self._ph_key: tuple[int, int, int] | None = None
        self._ph_mappers: tuple[Mapper | None, Mapper | None] = (None, None)
        # Stateless stand-ins (DECISIONS 38): value (lower-case) -> (kind, stand-in), in memory for
        # the life of the process and never written. The stand-in is a hash, so a restart mints the
        # same one again; this table only exists so a reply can be restored.
        self._stood_in: dict[str, tuple[str, str]] = {}
        self._stood_in_version = 0
        # Model requests open now: request id -> {session, started, content_at} (DECISIONS 47).
        # What the Session page reads to say "waiting on the AI service" - "connected" only ever
        # meant the page could reach aicleaner.
        self._open: dict[str, dict[str, Any]] = {}
        self._pdf_to_text = pdftotext_converter(cfg.pdftotext or None)
        # Probed once at startup, like pdftotext: the shared reader (Snipping Tool's engine when the
        # user has it, feature 113; otherwise Windows', with Tesseract where installed), otherwise
        # Tesseract alone, otherwise none and images stay a notice. This is also what reads an image
        # pasted into Claude Code in VS Code: it arrives here inside the request.
        self._image_to_text = image_converter(cfg.ocr_timeout_s) if cfg.ocr else None
        if cfg.ocr and self._image_to_text is None:
            log.info("no OCR engine on this machine: images and scanned PDFs stay withheld")
        # dialect -> (gateway token, upstream key); loaded once at startup.
        self._swaps: dict[str, tuple[str, str]] = {}
        for dialect, spec in cfg.credentials.items():
            token = read_secret(spec.get("gateway_token_file"), spec.get("gateway_token_env"))
            key = read_secret(spec.get("upstream_key_file"), spec.get("upstream_key_env"))
            if token and key:
                self._swaps[dialect] = (token, key)
            else:
                log.info("credential swap for %s not active (token %s, key %s)", dialect,
                         "found" if token else "missing", "found" if key else "missing")

    def client_labels(self) -> dict[str, str]:
        """Client id -> the name a person reads. Every interface a human sees uses the label; the
        id is plumbing for the filter and the file name."""
        try:
            return {c.get("id", ""): (c.get("label") or c.get("id", ""))
                    for c in self._mapping_store().load().data.get("clients", [])}
        except Exception:                                     # a broken mapping is reported elsewhere
            return {}

    def _as_profiles(self, entries: list[dict]) -> list:
        from .session import Profile

        labels = self.client_labels()
        shares = self.share_store.load()
        folded = {cid.lower(): folder for cid, folder in shares.items()}
        profiles = []
        for e in entries:
            share = Path(folded[e["client"].lower()]) if folded.get(e.get("client", "").lower()) else None
            label = labels.get(e.get("client", ""), "") or e["name"]
            profiles.append(Profile(name=e["name"], client=e.get("client", ""), label=label,
                                    projects=[Path(f) for f in e.get("projects", [])],
                                    model=e.get("model", ""), brief=e.get("brief", ""), share=share,
                                    workspaces=[Path(w) for w in e.get("workspaces", [])],
                                    home_override=e.get("home_override", ""),
                                    homes_before=list(e.get("homes_before") or [])))
            # Each worktree is a session of its own, `<profile> @ <name>` (DECISIONS 66): everything
            # keyed by a session's name - reattach, the menu, notices, the address - works unchanged.
            # Its home is the worktree, and only the worktree: the repo it came from is the other
            # session's, and passing it too would let this one write there.
            for w in e.get("worktrees") or []:
                profiles.append(Profile(name=f"{e['name']} @ {w['name']}", client=e.get("client", ""),
                                        label=label, projects=[Path(w["path"])], model=e.get("model", ""),
                                        brief=e.get("brief", ""), share=share, home_override=w["path"],
                                        worktree_of=e["name"], worktree=dict(w)))
        return profiles

    def read_profiles(self) -> list[dict]:
        """The stored profiles, with anything wrong with them recorded for the page to show.

        A profile naming a client the mapping does not hold is reported here - at load - because
        at start it would look like the session is broken rather than like a name that needs
        fixing. It is not refused: the other profiles still work, and the page says which one to
        put right.
        """
        entries, problem = self.profile_store.load()
        labels = self.client_labels()
        if labels:                     # an unreadable mapping is reported on its own page
            unknown = sorted({e["client"] for e in entries if e.get("client") and e["client"] not in labels})
            if unknown:
                missing = ", ".join(unknown)
                problem = (f"{problem} " if problem else "") + (
                    f"no client called {missing} is in the mapping, so the profiles naming it "
                    "filter for Global only")
        self.profile_problem = problem
        return entries

    def reload_profiles(self) -> list[dict]:
        """Pick up an edited profiles.json without a restart.

        A session already running keeps its own process and transcript; what changes is the list
        the picker shows and the label a new session is logged under.
        """
        entries = self.read_profiles()
        if self.session is not None:
            self.session.profiles = self._as_profiles(entries)
        return entries

    def _apply_credential(self, dialect: str, headers: dict[str, str]) -> str:
        """Replace a local gateway token with the real upstream key.

        Returns the auth mode for the evidence log: "subscription" (OAuth
        bearer from Claude Code), "gateway-swap" (thick client using the local
        token), or "passthrough" (anything else, forwarded untouched).
        """
        auth_key = next((k for k in headers if k.lower() == "authorization"), None)
        api_key = next((k for k in headers if k.lower() == "x-api-key"), None)
        bearer = headers[auth_key][7:].strip() if auth_key and headers[auth_key].lower().startswith("bearer ") else None
        swap = self._swaps.get(dialect)
        if swap and (bearer == swap[0] or (api_key and headers[api_key] == swap[0])):
            if auth_key:
                del headers[auth_key]
            if api_key:
                del headers[api_key]
            headers["x-api-key"] = swap[1]
            return "gateway-swap"
        if bearer and not api_key:
            return "subscription"
        return "passthrough"

    def _access_verifier(self):
        """The Cloudflare Access check for requests arriving through the tunnel, or None when
        remote access is switched off."""
        if not (self.cfg.remote_enabled and self.cfg.remote_hostname and self.cfg.remote_team_domain
                and self.cfg.remote_audience):
            return None
        from .access import AccessVerifier

        log.info("remote access on for %s (Access team %s)", self.cfg.remote_hostname, self.cfg.remote_team_domain)
        return AccessVerifier(self.cfg.remote_team_domain, self.cfg.remote_audience,
                              self.cfg.remote_allowed_emails)

    @staticmethod
    def _build(forms: list[Form], existing: list[Form] | None = None) -> Mapper:
        """A Mapper from the forms that validate; one bad placeholder form never stops the rest.

        Screened first, against each other and the mapping's own forms, for the two things that make
        a mapper refuse a form (a real containing a token, a token containing a real) and duplicates:
        otherwise one bad placeholder meant a rebuild per placeholder - some 1,400 of them."""
        forms = Proxy._screen(forms, existing or [])
        try:
            return Mapper(forms)
        except MappingError:
            good: list[Form] = []
            for form in forms:
                try:
                    Mapper(good + [form])
                    good.append(form)
                except MappingError as exc:
                    log.warning("placeholder for a %s left out: %s", form.client, exc)
            return Mapper(good)

    @staticmethod
    def _screen(forms: list[Form], existing: list[Form]) -> list[Form]:
        everyone = existing + forms
        tokens = sorted({f.token.lower() for f in everyone if f.token}, key=len, reverse=True)
        reals = sorted({f.real.lower() for f in everyone if f.real}, key=len, reverse=True)
        has_token = re.compile("|".join(map(re.escape, tokens))) if tokens else None
        has_real = re.compile("|".join(map(re.escape, reals))) if reals else None
        kept: list[Form] = []
        seen: set[str] = set()
        for f in forms:
            real, token = f.real.lower(), f.token.lower()
            if real in seen:
                continue
            if has_token is not None and any(m.group(0) != token for m in has_token.finditer(real)):
                log.warning("placeholder for a %s left out: its value contains a token", f.client)
                continue
            if has_real is not None and any(m.group(0) != real for m in has_real.finditer(token)):
                log.warning("placeholder for a %s left out: its placeholder contains a real form", f.client)
                continue
            seen.add(real)
            kept.append(f)
        return kept

    def _placeholder_mappers(self, base: Mapper) -> tuple[Mapper | None, Mapper | None]:
        """(placeholders only, mapping + placeholders), cached until either changes. Placeholder
        forms carry client "pending:<kind>" so evidence can count what was held back per kind."""
        if self.store is None:
            return None, None
        key = (id(base), self.store.placeholder_version, self._stood_in_version)
        if key != self._ph_key:
            forms: list[Form] = []
            for value, placeholder, kind in self.store.placeholder_forms():
                for variant in dict.fromkeys((value, value.lower())):
                    forms.append(Form(variant, placeholder, f"{PENDING_PREFIX}{kind}"))
            for value, (kind, stand_in) in self._stood_in.items():
                for variant in dict.fromkeys((value, value.upper())):
                    forms.append(Form(variant, stand_in, f"{PENDING_PREFIX}{kind}"))
            if forms:
                only = self._build(forms, list(base.forms))
                try:
                    combined = Mapper(list(base.forms) + list(only.forms))
                except MappingError as exc:   # screened, so rare: hold nothing back rather than fail
                    log.warning("placeholders could not join the mapping: %s", exc)
                    only, combined = Mapper([]), base
                combined.ignore = base.ignore
                self._ph_mappers = (only, combined)
            else:
                self._ph_mappers = (None, None)
            self._ph_key = key
        return self._ph_mappers

    async def _driver(self, session_name: str) -> dict[str, Any]:
        """Who is driving this model request (DECISIONS 38): whoever last started or prompted the
        Session-page session it came from, else the local account and the SSH connections open."""
        if session_name:
            noted = who.DRIVERS.get(session_name)
            if noted is not None:
                return {**noted, "source": "session-page"}
        return who.local_driver(await asyncio.to_thread(who.ssh_sources))

    def _waiting_unheld(self, value: str) -> bool:
        """True for a value queued for Review and not held back: it travels as it is."""
        found = self.store.find(value) if self.store is not None else None
        return found is not None and not found.placeholder

    def model_requests(self) -> list[dict[str, Any]]:
        """Model requests open now, oldest first (DECISIONS 47)."""
        return sorted((dict(v) for v in self._open.values()), key=lambda v: v["started"])

    def _record(self, request: web.Request, **kw: Any) -> None:
        """The evidence record, with what the substitution log needs about this request. Every
        path that finishes a request comes through here, so it also closes the open-request entry."""
        info = request.get(_REQ) or {}
        self._open.pop(info.get("id", ""), None)
        self.evidence.record(**kw, request_id=info.get("id", ""), who=info.get("who"),
                             held=info.get("held"), project=info.get("project", ""),
                             unprotected=info.get("unprotected"), declined=info.get("declined"),
                             canary_matches=info.get("canary"))

    def _timing(self) -> Any:
        """Middleware (feature 91): while Diagnostics records timings, each API request is timed -
        a `Server-Timing` header for the browser, and a line in logs/timings-<date>.jsonl, which
        also covers sealed replies whose headers the browser never sees. Off, it costs a flag check."""
        from . import timing

        @web.middleware
        async def timed(request: web.Request, handler: Any) -> web.StreamResponse:
            if (not request.path.startswith("/api/") or request.path == "/api/debug/timings"
                    or not timing.active(self.cfg)):
                return await handler(request)
            request[timing.REQUEST_PARTS] = []
            started = time.perf_counter()
            response = await handler(request)
            if response.prepared:                 # a stream: its length is not a request's time
                return response
            total = (time.perf_counter() - started) * 1000
            parts = request[timing.REQUEST_PARTS]
            response.headers["Server-Timing"] = timing.header(total, parts)
            body = getattr(response, "body", None)
            await asyncio.to_thread(self.timing_log.write, [{
                "source": "server", "method": request.method, "path": request.path_qs[:300],
                "status": response.status, "ms": round(total, 1),
                "parts": {name: round(ms, 1) for name, ms in parts},
                "bytes": len(body) if isinstance(body, (bytes, bytearray)) else None}])
            return response
        return timed

    def _audit(self) -> Any:
        """Middleware: every change made through the UI goes in the substitution log, with who."""
        @web.middleware
        async def audit(request: web.Request, handler: Any) -> web.StreamResponse:
            path = request.path
            logged_read = request.method == "GET" and path in _LOGGED_READS
            if not logged_read and (request.method not in ("POST", "PUT", "PATCH", "DELETE")
                                    or not path.startswith("/api/") or path in _NOT_UI_ACTIONS):
                return await handler(request)
            raw = await request.read() if request.can_read_body else b""
            status = 500
            try:
                response = await handler(request)
                status = response.status
                return response
            except web.HTTPException as exc:
                status = exc.status
                raise
            finally:
                try:
                    await self._note_ui(request, raw, status)
                except Exception as exc:  # noqa: BLE001 - the log never breaks the action it records
                    log.warning("could not log a UI action on %s: %s", path, exc)
        return audit

    async def _note_ui(self, request: web.Request, raw: bytes, status: int) -> None:
        path = request.path
        ssh_from = [] if request.get(REMOTE) else await asyncio.to_thread(who.ssh_sources)
        person = who.identity_of(request, ssh_from)
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", errors="replace")
        if body is None and request.method == "GET":
            body = dict(request.query)          # a logged read: which file, from which folder
        body = _withheld(body, path)
        if path in ("/api/session/start", "/api/session/prompt", "/api/session/queue/send") \
                and 200 <= status < 300 and self.session:
            name = str((body or {}).get("name", "")) if isinstance(body, dict) else ""
            try:
                name = self.session.profile(name).name
            except Exception:  # noqa: BLE001 - an unknown name is noted as given
                pass
            who.DRIVERS.note(name, person)
        self.evidence.ui(who=person, method=request.method, path=path, status=status, body=body)

    def kind_modes(self) -> dict[str, str]:
        """What each kind does with something new (DECISIONS 46): auto | review | ignore.

        Read from the settings that already existed: quiet_kinds is Auto-tokenise, placeholder_kinds
        is Tokenise & Review, a kind in neither is Ignore."""
        from .detect import SELECTABLE_KINDS

        quiet = self.store.quiet_kinds if self.store is not None else frozenset(self.cfg.quiet_kinds)
        review = set(self.cfg.placeholder_kinds)
        return {k: "auto" if k in quiet else "review" if k in review else "ignore" for k in SELECTABLE_KINDS}

    def owner_add_kinds(self, modes: dict[str, str]) -> set[str]:
        """The Auto-add ticks that apply: only with Tokenise & Review."""
        return {k for k in self.cfg.owner_add_kinds if modes.get(k) == "review"}

    def _owned_email_forms(self, text: str, mapper: Mapper, mode: str, source: str,
                           context: SeenContext) -> list[Form]:
        """Forms that stand in or hold back each address at a mapped domain (DECISIONS 46)."""
        forms: list[Form] = []
        for real, client in owned_emails(text, mapper, self.store.ignore_set()):
            token = ""
            if mode == "auto":
                known = self._stood_in.get(real.lower())
                if known is None:
                    taken = ({f.token.lower() for f in mapper.forms}
                             | {t.lower() for _k, t in self._stood_in.values()})
                    token = make_placeholder(real, "email", taken)
                    if token:
                        self._stood_in[real.lower()] = ("email", token)
                        self._stood_in_version += 1
                else:
                    token = known[1]
            else:
                # Waits in Review with the domain's client suggested, held back meanwhile.
                self.store.add_candidate(real, "email", "high", client, source,
                                         self.cfg.placeholder_kinds, context)
                found = self.store.find(real)
                token = found.placeholder if found is not None else ""
            if token:
                forms.append(Form(real.lower(), token, f"{PENDING_PREFIX}email"))
        return forms

    def _tokenise_with(self, original: Any, mapper: Mapper, forms: list[Form]) -> tuple[Any, Counter[str], Counter[str]]:
        """Tokenise the original body with the mapping plus these stand-ins, from scratch, so an
        address is matched whole before its domain is. Returns (body, outbound, held): the stand-ins'
        counts are moved from outbound to held, as the placeholder pass keeps them."""
        extra = self._build(forms, list(mapper.forms))
        outbound: Counter[str] = SubCounter()
        try:
            mixed = Mapper(list(mapper.forms) + list(extra.forms))
        except MappingError as exc:   # screened, so rare: keep the mapping's own tokenising
            log.warning("stand-ins for addresses at mapped domains left out: %s", exc)
            return tokenise_json(mapper, original, outbound), outbound, SubCounter()
        body = tokenise_json(mixed, original, outbound)
        held: Counter[str] = SubCounter()
        for key in [k for k in outbound if k.startswith(PENDING_PREFIX)]:
            held[key] = outbound.pop(key)
        for pair in [p for p in outbound.pairs if p[0].startswith(PENDING_PREFIX)]:
            held.pairs[pair] = outbound.pairs.pop(pair)
        return body, outbound, held

    def stood_in_kinds(self) -> frozenset[str]:
        """Kinds replaced by a stateless stand-in: always GUID, plus every kind you stopped proposing."""
        quiet = self.store.quiet_kinds if self.store is not None else frozenset()
        return frozenset(STATELESS_KINDS | {k for k in quiet if k != "name"})

    def _ignored(self, value: str, base: Mapper) -> bool:
        """On an ignore list, public, or an ordinary word: a person or the lists said it is not one."""
        from .detect import ignored_by, is_common_word, is_public

        own = self.store.ignore_set() if self.store is not None else set()
        protect = lambda v: carries_client_real(v, base)   # noqa: E731 - feature 76
        return (ignored_by(value, own, protect) or ignored_by(value, getattr(base, "ignore", ()) or (), protect)
                or is_public(value) or is_common_word(value))

    def _stand_in(self, sweep: list[tuple[str, str, str, str]], base: Mapper) -> None:
        """Give every unmapped value of a stateless kind its stand-in (DECISIONS 38).

        The sweep read the tokenised text, so a GUID mapped by hand is already its token here and
        is skipped as ours; so is a stand-in coming round again. A new value bumps the version,
        which rebuilds the placeholder mappers once; a repeat costs nothing.
        """
        # A kind you stop proposing is tokenised automatically, the same way (DECISIONS 43): "don't
        # ask me" must never mean "don't protect". Only high confidence, and never `name` - the low
        # classes are held back by nothing else either, and would put rubbish in every request.
        automatic = self.stood_in_kinds()
        wanted = [(v.lower(), k) for v, k, c, _o in sweep
                  if k in STATELESS_KINDS or (k in automatic and c == "high")]
        wanted = [(v, k) for v, k in wanted if k in STATELESS_KINDS or not self._ignored(v, base)]
        if not wanted:
            return
        ours = {f.token.lower() for f in base.forms} | {f.real.lower() for f in base.forms}
        taken = ours | {stand_in.lower() for _k, stand_in in self._stood_in.values()}
        for value, kind in wanted:
            if value in self._stood_in or value in ours or value in taken:
                continue
            stand_in = make_placeholder(value, kind, taken)
            if stand_in:
                self._stood_in[value] = (kind, stand_in)
                taken.add(stand_in.lower())
                self._stood_in_version += 1

    @property
    def mapper(self) -> Mapper:
        if self._handle is not None:
            before = self._handle.reloads
            m = self._handle.current()
            if self._handle.reloads != before:
                log.info("mapping reloaded: clients=%s forms=%d", ",".join(m.clients), len(m.forms))
            if self._handle.reloads != before and self._handle.last_error:
                log.warning("mapping problems (last good copies in use where possible): %s", self._handle.last_error)
            return m
        assert self._static is not None
        return self._static

    def stopped_message(self, cid: str) -> str:
        """The red-state text (spec 5.3) returned to the tool, with a link to fix it."""
        base = f"http://{self.cfg.listen}:{self.cfg.port}"
        if cid == GLOBAL_ID:
            what, link = "The global filter mapping is invalid.", f"{base}/#/global"
        else:
            what, link = f"The filter mapping is invalid for {cid}.", f"{base}/#/clients/{cid}"
        return (f"Client-identity data filtering: Stopped. {what} AI communication stopped until resolved. "
                f"Show the error: {link}")

    # ---- lifecycle --------------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=self.cfg.client_max_mb * 1024 * 1024,
                              middlewares=[make_guard(self.cfg.listen, self.cfg.remote_hostname,
                                                      self._access_verifier()),
                                           # after the guard, which decides what is remote (feature 21)
                                           make_tunnel_gate(self), self._audit(), self._timing()])
        app.router.add_get("/healthz", self.healthz)
        app.router.add_post("/api/tokenise", self.api_tokenise)
        app.router.add_post("/api/rehydrate", self.api_rehydrate)
        if self.store is not None:
            # Share the running handle's store so the UI sees the same last-good copies.
            mf = (self._handle.store if self._handle is not None
                  else MappingStore(Path(self.cfg.mapping), Path(self.cfg.clients_dir), self.cfg.state_dir))
            build_ui_routes(app, mf, self.store,
                            Path(self.cfg.log_dir), self.evidence.host, ContactSource(self.cfg.contact_dir),
                            self.user_secrets, self)
        # A session's traffic arrives under its own profile, which is how the filter knows whose
        # engagement this is. Registered first: /session/ACME/anthropic/... is not a dialect.
        app.router.add_route("*", "/session/{profile}/{dialect}/{tail:.*}", self.handle)
        app.router.add_route("*", "/{dialect}/{tail:.*}", self.handle)
        app.on_startup.append(self._start)
        app.on_cleanup.append(self._stop)
        return app

    def keep_tools(self) -> list[str]:
        """Tool patterns whose arguments stay tokenised: configured, plus what the MCP audit found
        reached over http(s) and not yet approved (H11)."""
        return self.mcp.keep_patterns(list(self.cfg.keep_tokenised_tools))

    def session_folders(self) -> list[str]:
        """Every folder a session can run in: configured projects and each profile's."""
        folders = [str(p) for p in self.cfg.session_projects]
        try:
            folders += [str(p) for e in self.profile_store.load()[0] for p in (e.get("projects") or [])]
        except Exception as exc:  # noqa: BLE001 - profiles that cannot be read are reported elsewhere
            log.debug("session folders: profiles unreadable: %s", exc)
        return folders

    async def audit_mcp(self) -> None:
        """Re-run the MCP audit off the loop; a failure is logged and the last answer kept."""
        try:
            await asyncio.to_thread(self.mcp.audit, self.session_folders())
        except Exception as exc:  # noqa: BLE001 - an audit failure must never stop the proxy
            log.warning("MCP audit failed: %s", exc)

    async def _start(self, app: web.Application) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=self.cfg.upstream_timeout_s)
        self._session = aiohttp.ClientSession(timeout=timeout, auto_decompress=True)
        await self.audit_mcp()
        # The Bash guard's hook kept in step with its switch at every start (feature 98), so a deploy
        # installs it - on by default - and points it at this install's Python and script.
        try:
            said = await asyncio.to_thread(bashguard.apply, self.cfg.claude_bash_guard)
            if said:
                log.info(said)
        except bashguard.GuardError as exc:
            log.warning("bash guard not applied: %s", exc)
        if self.session is not None:
            # Sessions a previous proxy left running in their brokers (DECISIONS 49): picked up,
            # not restarted, so a deploy no longer ends one or forks its conversation.
            try:
                await self.session.reattach_all()
            except Exception as exc:  # noqa: BLE001 - a proxy that cannot re-attach still starts
                log.warning("could not re-attach running sessions: %s", exc)

    def _add_forms(self, items: list[tuple[str, str, str]]) -> list[dict[str, str]]:
        """Auto-add every form, (client, value, kind) each; run in a thread. No cap (DECISIONS 38):
        a cap made the same message show different stand-ins on different turns, because the rest
        waited for a later request - the history rewrote itself under the model.

        Grouped by client and written once each. Adding singly meant every one of up to ten forms
        re-read and re-parsed EVERY client file, validated the whole set, and wrote the client
        document into the synced folder - ten full cycles, and ten chances to meet a sync lock, on
        the request path. A typical request has one client, so ten writes become one.
        """
        wanted: dict[str, list[tuple[str, str]]] = {}
        for client, value, kind in items:
            wanted.setdefault(client, []).append((value, kind))
        added: list[dict[str, str]] = []
        for client, pairs in wanted.items():
            kinds = {value: kind for value, kind in pairs}
            try:
                store_ = self._mapping_store()
                for real, tok in store_.add_forms(
                        client, [v for v, _k in pairs], origin=ORIGIN_AUTO,
                        source=SOURCE_AUTO):
                    # Where it came from, locally (feature 30). The client file says only
                    # "Auto-add"; which session and which request said it is evidence.
                    store_.provenance.record(client, real, SOURCE_AUTO,
                                             kind=kinds.get(real, ""),
                                             note="added by the proxy mid-request")
                    added.append({"client": client, "kind": kinds.get(real, ""), "token": tok, "real": real})
                    log.info("auto-added %s form %r for %s -> %s", kinds.get(real, ""), real, client, tok)
            except Exception as exc:  # never let bookkeeping break a request
                log.warning("auto-add of %d form(s) for %s failed: %s", len(pairs), client, exc)
        return added

    def _sessions_dir(self) -> Path:
        """Where brokered sessions keep their state: local, never the synced clients folder."""
        base = Path(self.cfg.state_dir) if self.cfg.state_dir else Path(self.cfg.mapping).parent / "state"
        return base / "sessions"

    def _build_session_host(self) -> None:
        from .session import SessionHost

        cfg = self.cfg
        # A Config built in code carries the folders but has never been through the file
        # reader, so ask the same helper the reader uses rather than starting with no profile.
        declared = cfg.session_profiles or session_profiles({}, cfg.session_projects)
        self.profile_store.bootstrap(declared)
        entries = self.read_profiles()
        self.session = SessionHost(claude=cfg.session_claude,
                                   base_url=f"http://127.0.0.1:{cfg.port}/anthropic",
                                   profiles=self._as_profiles(entries),
                                   permission_mode=cfg.session_permission_mode,
                                   max_running=cfg.session_max_running,
                                   approve_url=f"http://127.0.0.1:{cfg.port}/api/session/permission/request",
                                   sessions_dir=self._sessions_dir(),
                                   thinking_display=cfg.session_thinking_display)
        # The tree draws from the profile's workspace files (D99), within the allowed folder locations.
        self.session.allowed_roots = self._allowed_roots

    async def apply_session_enabled(self) -> None:
        """Follow session.enabled without a restart: on builds the host, off stops every running
        session first, so switching it off never leaves a Claude process behind."""
        if self.cfg.session_enabled and self.session is None:
            self._build_session_host()
        elif not self.cfg.session_enabled and self.session is not None:
            host, self.session = self.session, None
            await host.stop_all()

    async def _stop(self, app: web.Application) -> None:
        if self.session is not None:
            # A session lives in its own broker (DECISIONS 49): this proxy lets go and the next one
            # re-attaches. Its model requests fail while no proxy answers, and Claude Code retries.
            await self.session.detach_all()
        if self._session:
            await self._session.close()

    # ---- utility endpoints -------------------------------------------------

    async def healthz(self, request: web.Request) -> web.Response:
        m = self.mapper
        body: dict[str, Any] = {
            "ok": True,
            "clients": m.clients,
            "forms": len(m.forms),
            "dialects": sorted(self.cfg.upstreams),
            "keep_tokenised_tools": self.cfg.keep_tokenised_tools,
        }
        if self._handle is not None:
            body["mapping_reloads"] = self._handle.reloads
            body["mapping_error"] = self._handle.last_error
            body["broken"] = self._handle.broken
        body["credential_swap"] = sorted(self._swaps)
        body["candidates_pending"] = self.store.pending_count() if self.store else 0
        body["secrets_mode"] = self.cfg.secrets_mode
        # Whether the filter is switched off, by the API or by hand (H2's visible part, DECISIONS
        # 52): the API stays open for now, so what it was used to change has to be seen.
        body["canary_fail_open"] = self.cfg.canary_fail_open
        body["proxy_bypass"] = self.cfg.proxy_bypass
        body["detect_enabled"] = self.cfg.detect_enabled
        body["user_secrets"] = len(self.user_secrets.entries()) if self.user_secrets else 0
        body["review_url"] = self.cfg.review_url
        body["listen"] = self.cfg.listen
        body["port"] = self.cfg.port
        body["loopback_only"] = self.cfg.listen in ("127.0.0.1", "::1", "localhost")
        # Each client's folder is shared in OneDrive (DECISIONS 51); outside it nothing syncs.
        from .shares import in_onedrive
        body["clients_in_onedrive"] = in_onedrive(Path(self.cfg.clients_dir))
        return web.json_response(body)

    async def api_tokenise(self, request: web.Request) -> web.Response:
        text = (await request.read()).decode("utf-8", errors="replace")
        return web.Response(text=self.mapper.tokenise(text), content_type="text/plain")

    async def api_rehydrate(self, request: web.Request) -> web.Response:
        text = (await request.read()).decode("utf-8", errors="replace")
        return web.Response(text=self.mapper.rehydrate(text), content_type="text/plain")

    # ---- main path --------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        started = time.monotonic()
        # Counted before anything can refuse it: the question this answers is "is a client
        # pointed at us", not "did the request succeed".
        self.requests_seen += 1
        self.last_request_at = time.time()
        dialect = request.match_info["dialect"]
        tail = request.match_info["tail"]
        upstream = self.cfg.upstreams.get(dialect)
        if upstream is None:
            return web.json_response({"error": f"unknown dialect '{dialect}'"}, status=404)
        url = f"{upstream}/{tail}"
        if request.query_string:
            url += "?" + request.query_string

        mapper = self.mapper  # one snapshot per request, so a reload mid-flight is consistent
        reply_mapper = mapper
        # Which engagement this belonged to, for the evidence log. It names the request; it never
        # narrows what is replaced - every client's forms apply in every session.
        session_name, session_client = self._session_of(request)
        # What the substitution log needs about this request (DECISIONS 38): an id shared with its
        # evidence record, and who was driving.
        request[_REQ] = {"id": uuid.uuid4().hex[:12], "who": await self._driver(session_name),
                         "project": "", "held": None, "unprotected": [], "declined": [], "canary": []}
        self._open[request[_REQ]["id"]] = {"session": session_name, "started": time.time(), "content_at": None}
        if self._handle is not None and self._handle.broken:
            return self._refuse(dialect, request, started, SubCounter(), self.stopped_message(self._handle.broken[0]))
        around = _outside_body(request, tail)
        found = mapper.find_real(around) if around else []
        if found and "/" + tail not in self.cfg.allow_non_json_paths:
            log.warning("canary: a client name in the URL or headers of %s /%s (clients: %s)",
                        request.method, tail, ", ".join(found))
            return self._refuse(dialect, request, started, SubCounter(),
                                f"a client name is in the URL or a header (clients: {', '.join(found)}); "
                                "those are sent as written, so the request was not sent")
        outbound: Counter[str] = SubCounter()
        inbound: Counter[str] = SubCounter()
        secrets: Counter[str] = Counter()
        auto_added: list[dict[str, str]] = []
        pre_held: Counter[str] = SubCounter()   # stood in or held back before the placeholder pass
        held_back: dict[str, int] = {}
        attach: dict[str, int] = {}
        near_miss = 0
        body = await request.read()
        content_type = request.headers.get("Content-Type", "")
        if self.cfg.proxy_bypass:
            # Nothing below runs: the body is not read for identifiers, replaced, detected, held
            # back, redacted or checked, and the reply is restored with a mapper holding nothing.
            request[_BYPASS] = True
            mapper = reply_mapper = INERT
            log.warning("proxy.bypass is ON: %s /%s forwarded untouched", dialect, tail)
            body = _without_our_marks(body, content_type, dialect)

        if body and not self.cfg.proxy_bypass:
            if _is_json(content_type):
                try:
                    obj = json.loads(body)
                except json.JSONDecodeError:
                    return self._refuse(dialect, request, started, outbound, "request body is not valid JSON")
                # The history comes back carrying the green marks we added to earlier replies. They
                # are ours: removed before anything is tokenised, so the provider never sees them.
                obj = walk_strings(obj, strip_marks)
                # The review footer likewise: left in the history, the model learns to write it
                # and does, until the output cap (D63).
                if dialect == "anthropic":
                    obj = strip_footers_from_history(obj)
                # Attachments first (review finding 4): text PDFs become text that is filtered below;
                # images, scanned PDFs and anything uninspectable are replaced by a notice.
                #
                # Near-miss repair rides along with the conversion, because that is the only place
                # a misreading is expected: an engine turns rn into m, a person typing does not.
                # The correction puts the real form back, so the tokenising below replaces it the
                # ordinary way. `read` keeps what was converted, so the candidate pass beneath
                # looks at that text and not at the whole request.
                near: Counter[str] = SubCounter()
                read: list[str] = []

                def repair(text: str) -> str:
                    fixed = mapper.repair_near_misses(text, near) if self.cfg.near_miss else text
                    read.append(fixed)
                    return fixed

                obj, attach_counts = await asyncio.to_thread(
                    lambda: scrub(obj, self.cfg.allow_images, self._pdf_to_text,
                                  image_to_text=self._image_to_text, repair=repair,
                                  ocr_max_pages=self.cfg.ocr_max_pages,
                                  ocr_timeout_s=self.cfg.ocr_timeout_s,
                                  ocr_max_bytes=self.cfg.ocr_max_bytes))
                attach = dict(attach_counts)
                original = obj
                project = request_project(obj)
                request[_REQ]["project"] = project
                obj = tokenise_json(mapper, obj, outbound)
                # One sweep of the tokenised text, off the event loop, shared by everything below:
                # auto-add, resource naming and the review queue used to sweep it three times each,
                # on the loop, so one large request froze every other one while it ran.
                sweep: list[tuple[str, str, str, str]] = []
                if self.store is not None:
                    text = detection_text(obj)
                    sweep = await asyncio.to_thread(lambda: list(find_candidates(text, mapper)))
                    # What each kind does with something new (DECISIONS 46): auto-tokenise, tokenise
                    # & review, or ignore; and, for review, whether a provable owner adds it at once.
                    modes = self.kind_modes()
                    self.store.ignore_kinds = frozenset(k for k, m in modes.items() if m == "ignore")
                    owner_add = self.owner_add_kinds(modes)
                    # Added by itself only where the value proves its owner (DECISIONS 38). There
                    # is no "chosen kinds" route: it guessed the owner from the conversation.
                    wanted: list[tuple[str, str, str]] = []
                    if self.cfg.auto_map_client_resources and modes.get("client-resource") == "review":
                        # A resource name that carries one of a client's own forms: the owner is certain.
                        wanted += [(client, value, "client-resource") for value, client in
                                   client_resources(text, mapper, sweep)]
                    # A name carrying one client's token: the token proves the owner (DECISIONS 37).
                    wanted += [(client, value, "name") for value, client in
                               token_names(text, mapper, sweep, self.store.ignore_set())]
                    if "email" in owner_add:
                        # An email at a client's mapped domain: the domain proves the owner, and
                        # without this the mailbox name went out in clear (DECISIONS 45).
                        wanted += [(client, value, "email") for value, client in
                                   owned_emails(text, mapper, self.store.ignore_set())]
                    elif mapper.email_rules:
                        # A domain with an email rule (feature 96) adds its addresses whatever the
                        # email setting: the person said every address there is the client's.
                        wanted += [(client, value, "email") for value, client in
                                   owned_emails(text, mapper, self.store.ignore_set())
                                   if email_rule_for(value, mapper.email_rules)]
                    # Tenant, SharePoint, domain and DevOps: their own proofs (DECISIONS 46).
                    wanted += [(client, value, kind) for value, kind, client in
                               owned_values(text, mapper, sweep, owner_add - {"email"}, self.store.ignore_set())]
                    if wanted:
                        auto_added += await asyncio.to_thread(self._add_forms, wanted)
                    if auto_added:
                        # The mapping was reloaded mid-request, so the snapshot is stale: re-read
                        # it and tokenise again, so THIS request is covered by what was just added.
                        mapper = self.mapper
                        outbound = SubCounter()
                        obj = tokenise_json(mapper, original, outbound)
                    if modes.get("email") in ("auto", "review") and "email" not in owner_add:
                        # An address at a mapped domain has already lost its domain to the mapping,
                        # so nothing downstream sees it; its mailbox name would go out. Stood in or
                        # held back whole here, from the original text (DECISIONS 46).
                        forms = self._owned_email_forms(detection_text(obj), mapper, modes["email"],
                                                        f"{dialect} /{tail}",
                                                        SeenContext(dict(outbound), project, session_name))
                        if forms:
                            obj, outbound, pre_held = self._tokenise_with(original, mapper, forms)
                if self.cfg.near_miss and read:
                    # Already repaired above, inside the conversion. What is left is the
                    # bookkeeping: count what was put right, and hold back a word that is almost a
                    # client's name (where the mapping holds the longer form) for review.
                    near_miss = sum(near.values())
                    if near_miss:
                        outbound.update(near)
                    if self.store is not None:
                        was_read = "\n".join(read)
                        misses = await asyncio.to_thread(near_miss_words, was_read, mapper)
                        for word, client, near in misses:
                            if self.store.add_candidate(word, "client-resource", "high", client,
                                                        f"{dialect} /{tail}", self.cfg.placeholder_kinds,
                                                        SeenContext(dict(outbound), project, session_name),
                                                        near=near):
                                near_miss += 1
                                log.info("near miss of a %s identifier held back: %r", client, word)
                if self.cfg.secrets_mode == "block":
                    fams = find_families(detection_text(obj))
                    if fams:
                        return self._refuse(dialect, request, started, outbound,
                                            f"credential material present ({', '.join(fams)}); remove it and retry")
                elif self.cfg.secrets_mode == "redact":
                    if self.user_secrets is not None:
                        self.user_secrets.maybe_reload()
                        obj = walk_strings(obj, lambda t: self.user_secrets.redact(t, secrets))
                    obj = walk_strings(obj, lambda t: redact(t, secrets))
                body = dumps(obj)
                reply_mapper = mapper
                if self.store is not None:
                    # Learning mode: anything identifier-shaped left after tokenisation is unmapped.
                    # Read from the decoded strings, never the raw JSON: signatures and base64 are not
                    # text, and fragments of them queued for review were mapped as "identifiers".
                    final_text = detection_text(obj)
                    # What auto-add mapped is now known and skipped; what near-miss or redaction took
                    # out of the text is no longer in it, and is not queued.
                    still = [c for c in sweep if c[0] in final_text]
                    # In a thread (H12): it runs the detectors and writes the candidates file, and on
                    # the loop it held up every other request - the proxy and the page alike. The
                    # store locks each method, so concurrent requests are safe.
                    new = await asyncio.to_thread(
                        self.store.record, final_text, mapper, f"{dialect} /{tail}", self.cfg.placeholder_kinds,
                        SeenContext(dict(outbound), project, session_name), still)
                    if new:
                        self.notifier.maybe_toast(self.store.pending_count(), self.store.version)
                    # Fail closed: unknown identifiers of the chosen kinds go out as placeholders, and
                    # replies are rehydrated with them too, so you and local tools see real values.
                    self._stand_in(sweep, mapper)
                    only, combined = self._placeholder_mappers(mapper)
                    if only is not None and combined is not None:
                        held: Counter[str] = SubCounter()
                        obj = tokenise_json(only, obj, held)
                        held.update(pre_held)
                        held.pairs.update(pre_held.pairs)
                        body = dumps(obj)
                        held_back = {k.split(":", 1)[1]: v for k, v in held.items()}
                        reply_mapper = combined
                        request[_REQ]["held"] = held
                    # Queued and not held back goes out as it is while it waits. Logged, so it is
                    # seen rather than assumed (DECISIONS 38).
                    sent_text = detection_text(obj)
                    request[_REQ]["unprotected"] = [(v, k) for v, k, _c, _o in still
                                                    if v in sent_text and self._waiting_unheld(v)]
                leaked = canary(mapper, body)
                # What a match rule declined to replace went out as written, by the rule's own
                # decision; the canary no longer refuses it, so it is reported instead (DECISIONS 39).
                declined = rule_declined(mapper, body)
                if declined:
                    request[_REQ]["declined"] = declined
                    for cid, found, _real in declined:
                        log.info("match rule let %r through for %s (not the client, by the rule)", found, cid)
                if leaked:
                    for site in canary_sites(mapper, body):
                        log.warning("canary match at %s", site)
                    # The same matches, with their surroundings, for the substitution log.
                    request[_REQ]["canary"] = canary_matches(mapper, body)
                if leaked and self.cfg.canary_fail_open:
                    # The emergency switch on the Settings page: send it anyway, and say so loudly.
                    log.warning("canary FAIL-OPEN: sent a request the canary would have refused (clients: %s)",
                                ", ".join(leaked))
                    request[_FAIL_OPEN] = leaked   # the request's one evidence line will say so
                elif leaked:
                    self._record(
                        request,
                        dialect=dialect, method=request.method, path="/" + tail, status=403,
                        outbound=outbound, inbound=inbound, canary=leaked, streamed=False,
                        started=started, note="canary refused request",
                        session=session_name, session_client=session_client,
                    )
                    return web.json_response(
                        {"type": "error", "error": {"type": "aicleaner_canary",
                         "message": "aicleaner refused to send this request: a real client "
                                    f"identifier survived tokenisation (clients: {', '.join(leaked)}). "
                                    "Check the mapping forms."}},
                        status=403,
                    )
            elif "/" + tail not in self.cfg.allow_non_json_paths:
                return self._refuse(
                    dialect, request, started, outbound,
                    f"non-JSON request body ({content_type or 'no content-type'}) is not inspectable",
                )
            else:
                # An exempt endpoint is the ONE path through this proxy with nothing under it:
                # no names replaced, no identifiers detected, nothing held back, no canary. It
                # exists as an escape hatch for the afternoon a client moves to the Files API and
                # every attachment starts meeting a 403 (feature 26).
                #
                # Decided 2026-09-27 not to build the multipart parser yet - it has never fired
                # here, 131 logged requests across two endpoints, both JSON - but to make using
                # the hatch impossible to do quietly. Every request through it says so in the
                # evidence log, because "nothing was checked" is the most important thing that
                # can be true of a request.
                log.warning("UNFILTERED: %s /%s is on allow_non_json_paths, so this request was "
                            "not inspected at all - no names replaced, no canary", request.method, tail)
                self._record(
                    request,
                    dialect=dialect, method=request.method, path="/" + tail, status=0,
                    outbound=outbound, inbound=Counter(), canary=[], streamed=False,
                    started=started, note="UNFILTERED: exempt path, nothing was inspected",
                    session=self._session_of(request)[0], session_client=self._session_of(request)[1],
                )

        mapper = reply_mapper if body and _is_json(content_type) else mapper
        headers = {k: v for k, v in request.headers.items() if _forwardable(k)}
        auth_mode = self._apply_credential(dialect, headers)
        assert self._session is not None
        try:
            upstream_resp = await self._session.request(
                request.method, url, headers=headers, data=body if body else None, allow_redirects=False
            )
        except aiohttp.ClientError as exc:
            log.warning("upstream error for %s: %s", url, exc)
            # Every request leaves an evidence line, including the ones the provider never saw.
            self._record(
                request,
                dialect=dialect, method=request.method, path="/" + tail, status=502,
                outbound=outbound, inbound=inbound, canary=_fail_open_of(request)[0], streamed=False,
                started=started, note=f"upstream unreachable: {type(exc).__name__}", auth=auth_mode, secrets=secrets,
                auto_added=auto_added, held_back=held_back, attachments=attach, near_miss=near_miss,
                session=session_name, session_client=session_client,
            )
            return web.json_response(
                {"type": "error", "error": {"type": "aicleaner_upstream", "message": str(exc)}},
                status=502,
            )

        async with upstream_resp:
            resp_headers = {
                k: v for k, v in upstream_resp.headers.items() if k.lower() not in _STRIP_RESPONSE
            }
            self.evidence.detail = self.cfg.detail_log
            self.evidence.every_request = self.cfg.detail_every_request
            self.evidence.stood_in_kinds = self.stood_in_kinds()
            if upstream_resp.content_type == "text/event-stream":
                return await self._stream(request, upstream_resp, resp_headers, dialect, tail,
                                          mapper, outbound, inbound, started, auth_mode, secrets, auto_added,
                                          held_back, attach, near_miss)

            raw = await upstream_resp.read()
            kept = 0
            if _is_json(upstream_resp.content_type) and raw:
                try:
                    obj, kept = rehydrate_message(mapper, json.loads(raw), inbound, self.keep_tools(),
                                                  mark=self._marks_for(request))
                    footer = self._footer(request, dialect, tail, len(inbound))
                    if footer:
                        obj = self.notifier.append_to_json(obj, footer)
                    raw = dumps(obj)
                except json.JSONDecodeError:
                    pass
            self._record(
                request,
                dialect=dialect, method=request.method, path="/" + tail,
                status=upstream_resp.status, outbound=outbound, inbound=inbound,
                canary=_fail_open_of(request)[0], note=_fail_open_of(request)[1],
                streamed=False, started=started, kept=kept, auth=auth_mode, secrets=secrets,
                auto_added=auto_added, held_back=held_back, attachments=attach, near_miss=near_miss,
                session=session_name, session_client=session_client,
            )
            return web.Response(status=upstream_resp.status, headers=resp_headers, body=raw)

    async def _stream(
        self, request: web.Request, upstream_resp: aiohttp.ClientResponse,
        resp_headers: dict[str, str], dialect: str, tail: str, mapper: Mapper,
        outbound: Counter[str], inbound: Counter[str], started: float, auth_mode: str,
        secrets: Counter[str] | None = None, auto_added: list[dict[str, str]] | None = None,
        held_back: dict[str, int] | None = None, attach: dict[str, int] | None = None,
        near_miss: int = 0,
    ) -> web.StreamResponse:
        session_name, session_client = self._session_of(request)
        response = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
        await response.prepare(request)
        parser = SSEParser()
        rewriter = get_stream_rewriter(dialect, mapper, inbound, self.keep_tools(),
                                       mark=self._marks_for(request))
        max_index = -1
        footer_done = False
        try:
            opened = self._open.get((request.get(_REQ) or {}).get("id", ""))
            async for chunk in upstream_resp.content.iter_any():
                for ev in parser.feed(chunk):
                    # Only the reply itself counts; a stream of pings (after message_start) is stuck.
                    if opened is not None and ev.event in _REPLY_EVENTS:
                        opened["content_at"] = time.time()
                    if dialect == "anthropic" and ev.event == "content_block_start":
                        try:
                            max_index = max(max_index, int(json.loads(ev.data or "{}").get("index", -1)))
                        except (ValueError, TypeError):
                            pass
                    if dialect == "anthropic" and ev.event == "message_delta" and not footer_done:
                        footer_done = True
                        # Flush held text first: it both keeps our block last and finishes
                        # filling `inbound`, so the restored count below is complete.
                        for out in rewriter.finish():
                            await response.write(out.serialise())
                        footer = self._footer(request, dialect, tail, len(inbound))
                        if footer:
                            for out in self.notifier.stream_events(footer, max_index + 1):
                                await response.write(out.serialise())
                    for out in rewriter.on_event(ev):
                        await response.write(out.serialise())
            for ev in parser.finish():
                for out in rewriter.on_event(ev):
                    await response.write(out.serialise())
            for out in rewriter.finish():
                await response.write(out.serialise())
        finally:
            if rewriter.fragments:
                # The safety net under D87: a token written in a form swap-back does not know went
                # to a tool as it was. Logged with what it was; the toast is rate-limited.
                log.warning("tool call kept part of a token after swap-back (session %s): %s",
                            session_name or "-", ", ".join(sorted(set(rewriter.fragments))))
                self.notifier.fragment_toast(rewriter.fragments)
            self._record(
                request,
                dialect=dialect, method=request.method, path="/" + tail,
                status=upstream_resp.status, outbound=outbound, inbound=inbound,
                canary=_fail_open_of(request)[0], note=_fail_open_of(request)[1],
                streamed=True, started=started, kept=rewriter.kept, auth=auth_mode, secrets=secrets,
                auto_added=auto_added, held_back=held_back, attachments=attach, near_miss=near_miss,
                session=session_name, session_client=session_client,
            )
        await response.write_eof()
        return response

    def _marks_for(self, request: web.Request) -> bool:
        """Whether restored names in this reply are marked green for the reader.

        A session's traffic always is: it arrives on its own address, so the proxy knows the reply
        is going to our page, which draws the marks. Claude Code in a terminal or VS Code is marked
        only when highlight.terminal is on, because what the VS Code panel does with an ANSI
        sequence in model text has to be seen before it is trusted. Anything else - Claude Desktop,
        an SDK - is never marked, since nothing there would draw it.
        """
        if request.match_info.get("profile"):
            return self.cfg.highlight_session
        agent = request.headers.get("User-Agent", "")
        return self.cfg.highlight_terminal and agent.startswith(CLAUDE_CODE_AGENT)

    def _session_of(self, request: web.Request) -> tuple[str, str]:
        """Whose session this request is: the profile name a person reads, and the client id it
        filters for. Empty for traffic that did not come from a driven session."""
        name = request.match_info.get("profile", "")
        if not name or self.session is None:
            return "", ""
        try:
            profile = self.session.profile(name)
        except Exception:
            return name, ""          # named a session we do not know; record what was asked for
        return profile.name, profile.client

    def _allowed_roots(self) -> list[Path]:
        """folders.roots plus the folders already in use, resolved - what a folder picker may
        show, and so what a workspace file may add to a session's tree (D72)."""
        from .folders import roots_of
        in_use = [str(p) for entry in self.read_profiles() for p in (entry.get("projects") or [])]
        return roots_of(list(self.cfg.folder_roots) + in_use)

    def _mapping_store(self) -> MappingStore:
        if self._handle is not None:
            return self._handle.store
        return MappingStore(Path(self.cfg.mapping), Path(self.cfg.clients_dir))

    def set_secrets_mode(self, mode: str) -> None:
        self.cfg.set_secrets_mode(mode)
        log.info("secrets mode set to %s", self.cfg.secrets_mode)

    def _footer(self, request: web.Request, dialect: str, tail: str, restored: int = 0) -> str | None:
        """Footer text for a completed model reply, or None.

        `restored` is how many distinct names were put back on the way in. Where this audience
        will not see them drawn in colour - Claude Desktop, a plain SDK - the count is the only
        signal that anything was restored.

        Never for a session driven from the page (feature 67): its status stack already shows
        the count. Never under proxy.bypass: that request was forwarded untouched, so a footer
        added to its reply could not be removed from the next one (D63).
        """
        if self.store is None or dialect != "anthropic" or "messages" not in tail or "count_tokens" in tail:
            return None
        if request.match_info.get("profile") or request.get(_BYPASS):
            return None
        return self.notifier.footer_text(self.store.pending_count(), self.store.version,
                                         restored=restored, marked=self._marks_for(request))

    def _refuse(self, dialect: str, request: web.Request, started: float,
                outbound: Counter[str], reason: str) -> web.Response:
        session_name, session_client = self._session_of(request)
        self._record(
            request,
            dialect=dialect, method=request.method, path=request.path, status=403,
            outbound=outbound, inbound=Counter(), canary=[], streamed=False,
            started=started, note=f"refused: {reason}",
            session=session_name, session_client=session_client,
        )
        return web.json_response(
            {"type": "error", "error": {"type": "aicleaner_refused", "message": f"aicleaner: {reason}"}},
            status=403,
        )


def run(cfg: Config) -> None:
    # A folder per client (DECISIONS 51): files earlier versions wrote flat move into their folders
    # before anything reads them, backed up first. A failure here costs the move, never the start.
    try:
        MappingStore(Path(cfg.mapping), Path(cfg.clients_dir), cfg.state_dir).move_into_folders(
            Path(cfg.state_dir) / "backups")
    except OSError as exc:
        log.warning("could not move client files into their folders: %s", exc)
    from .shares import in_onedrive
    if not in_onedrive(Path(cfg.clients_dir)):
        log.warning("the clients location %s is not in OneDrive: client folders will not sync to colleagues",
                    cfg.clients_dir)
    handle = MapperHandle(cfg.mapping, cfg.clients_dir, cfg.state_dir)
    if handle.broken:
        log.warning("client files with no usable copy (model traffic refused until fixed): %s", ", ".join(handle.broken))
    evidence = EvidenceLog(cfg.log_dir, detail=cfg.detail_log, detail_retention_days=cfg.detail_retention_days,
                           every_request=cfg.detail_every_request)
    proxy = Proxy(cfg, handle, evidence)
    proxy.ps_commands.start()        # in the background; the proxy does not wait for it (feature 80)
    mapper = handle.mapper
    log.info("aicleaner listening on http://%s:%d  clients=%s  forms=%d  keep_tokenised_tools=%s",
             cfg.listen, cfg.port, ",".join(mapper.clients), len(mapper.forms),
             cfg.keep_tokenised_tools or "none")
    # The exemption list is empty by default and is the only way to switch the filter off for an
    # endpoint entirely. An entry in it should never be something that sits in a config file
    # unremarked, so it is said at every start, in the words of what it actually does (26).
    for path in cfg.allow_non_json_paths:
        log.warning("UNFILTERED PATH: %s is exempt from every check this proxy performs - "
                    "no names replaced, no identifiers detected, nothing held back, no canary. "
                    "Remove it from allow_non_json_paths unless you are working around a "
                    "specific refusal right now.", path)
    web.run_app(proxy.build_app(), host=cfg.listen, port=cfg.port, print=None,
                access_log=None)
