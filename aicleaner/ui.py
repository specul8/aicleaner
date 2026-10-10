"""The UI: serves the single-page app in web/ and the JSON API behind it. Loopback only (guard.py).

Pages: / (the app shell; /ui, /config and /secrets redirect to its #/ routes), /web/* static files.
API (JSON; writes need Content-Type application/json):
  GET  /api/status                         banner, status stack, per-client state, contact
  GET  /api/clients/<id>                   a client as the UI shows it (entries with tokens and kinds)
  GET  /api/clients/<id>/raw               the file text and its error (syntax-error view)
  POST /api/clients/<id>/restore           put the last-good copy back
  GET  /api/clients/<id>/key               {has_key, key_id} - never the key (DECISIONS 51)
  POST /api/clients/<id>/key               {action: generate|enter|rotate, key?}; local only
  POST /api/clients/<id>/held              {accept: bool} - a held change, reviewed
  POST /api/mcp/approve                    {pattern} - real names for a remote MCP server; local only
  POST /api/clients/<id>/salvage           rebuild from the entries still readable in a broken file
  GET  /api/clients/<id>/share             that client's OneDrive share, and where OneDrive is
  POST /api/clients/<id>/share             {path} use a folder, {create: true} make one, {path: ""} forget it
  GET  /api/session                        every profile, and the state of each session (?name=)
  POST /api/session/start                  {name, project, resume?, model?}
  POST /api/session/prompt                 {name, text}
  POST /api/session/stop                   {name} end that session
  POST /api/session/interrupt              {name} stop the turn, keep the session
  POST /api/session/queue/send             {name, id} send a waiting prompt now (D176)
  POST /api/session/queue/cancel           {name, id} drop a waiting prompt (D176)
  POST /api/session/permission-mode        {name, mode} how it asks before it acts
  POST /api/session/permission             {id, allow} answer a waiting permission request
  POST /api/session/permission/request     the approval host asking; waits for the answer
  GET  /api/session/events                 server-sent events for one session (?name=)
  GET  /api/session/profiles               the profiles, and the clients one can be pointed at
  POST /api/session/profiles               {profiles: [...]} replace them (validate, then commit)
  GET  /api/candidates                     pending review items
  POST /api/candidates/resolve             {value, action: map|ignore, client?, index?, real?}
  POST /api/candidates/public              {value} the same in every tenant: ignore it everywhere
  POST /api/candidates/snooze              {kind, undo?} "Not now" on a kind offer, back after 50 more ignores
                                           and take it out of the client that had it
  GET  /api/mapping                        clients and forms in use
  POST /api/mapping/client                 {id, index?, expand_case?}
  POST /api/mapping/form                   {client, real, token?}   token derived (suffixed if taken)
  POST /api/mapping/form/copy-to-global    {client, real}   the client entry stays, locked
  POST /api/mapping/form/from-global       {real, to}   Global gives it back: unlocks, or moves
  POST /api/mapping/form/move              {client, real, to}   never into Global
  POST /api/mapping/client/label           {id, label}   display name (empty clears it)
  POST /api/mapping/form/update            {client, real, new_real?, new_token?, new_kind?,
                                           match?}  match absent = leave the rule alone,
                                           null = remove it (feature 23)
  POST /api/mapping/form/rederive          {client, real}   derive its token again, today's rules
  POST /api/mapping/form/delete            {client, real}   leaves a removal record
  POST /api/mapping/forms/delete           {client, reals[]} the same, in one write
  POST /api/mapping/forms/restore          {client, entries[{real,token}], unignore?, from_global?}
                                           put a bulk action back, tokens and all (D42)
  POST /api/mapping/forms/ignore           {client, reals[], scope} off the token list, onto an ignore list
  POST /api/mapping/client/email-rule      {client, domain, on} every address at a client domain (F96)
  GET  /api/ignore, POST /api/ignore, /api/ignore/delete, /api/ignore/move,
       /api/ignore/tokenise  {value, scope} stop ignoring it and tokenise it instead
  GET  /api/evidence?n=50                  last n evidence lines from this host
  GET  /api/folders?path=<p>               folders a picker may show there (folders.roots)
  GET  /api/folders/roots                  the allowed roots, and whether this side may set them
  POST /api/folders/new                    {parent, name} a new folder inside an allowed one
  GET  /api/models[?refresh=1]             model names this machine could use (offered, not enforced)
  GET  /api/public                         what the shipped public-identifier list holds
  GET  /api/export/public                  that list itself, as a download
  /api/config, /api/secrets, /api/secrets/mode, /api/export/*, /api/import

Every mapping write is validated against the other clients as currently usable before the file
is replaced, so the UI can never leave a client file in a state the proxy would refuse.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .shares import in_onedrive
from typing import Any

from aiohttp import web

from .config import Config
from .contact import ContactSource
from .detect import CandidateStore
from .guard import FILE_CSP, REMOTE, is_loopback_listen

# What may be changed through the tunnel: the ones that alter how you are TOLD about things, not
# what is done about them (H3). Everything else is set on the machine, which keeps proxy.bypass,
# secrets.mode, detect.enabled and attachments.allow_images out of reach of a
# stolen phone or a mis-scoped Access policy - each of those switches a protection off, persistently
# and silently, and none of them is something anybody needs to do from a train.
#
# Widened 2026-09-27, after asking where the line actually sits. What stays local is what
# WEAKENS THE FILTER or exposes a credential - proxy.bypass, detect.enabled,
# keep_tokenised_tools, secrets.mode, remote.enabled, and attachments.allow_images now that it
# also rewrites the user-level CLAUDE.md. A filter switched off from a phone is a different
# class of thing from a client file downloaded to one.
#
# What moved across is client-data CURATION - what a scan collects, what is auto-added, which
# projects a session may use. That is the same kind of decision as editing a client file, which
# has always been allowed remotely, and triaging a noisy scan from somewhere else is a real use.
#
# folders.roots moved too, for a stated case: realising at a client site that a folder holding
# client content was never added. It is de-risked rather than trusted - `folders.blocked_reason`
# refuses a system folder outright, here and locally, because client work is never in one and a
# hurried entry from a train is exactly how Program Files ends up behind the picker.
#
# Refused by the API, never merely hidden by the page: the page is not the boundary, and a direct
# POST has to meet the same answer.
REMOTE_SETTINGS = ("detect.footer", "detect.toast", "logs.detail", "logs.detail_retention_days",
                   "logs.detail_every_request",
                   # The canary's emergency switch reaches too (DECISIONS 40, 2026-09-30). The canary
                   # is a last-ditch check that has never fired. If it ever fires on a false positive
                   # the real text is already in the conversation, so EVERY later request is refused
                   # - and someone away from the desk had no way out. That is not the same class as
                   # bypass or turning detection off: those open a path that is never tokenised;
                   # this lets through what tokenising has already been applied to, one refusal at a
                   # time, each one logged with its context and the person who turned it on.
                   "canary.fail_open",
                   "highlight.terminal", "session.enabled",
                   "detect.auto_map_client_resources",
                   "detect.near_miss", "detect.placeholder_kinds",
                   # Accepted from Review's offer, which is where triage happens - on the phone as
                   # much as the desk. Same standing as placeholder_kinds, which it narrows.
                   "detect.quiet_kinds",
                   "detect.owner_add_kinds",
                   "scan.generated_markers", "session.projects", "folders.roots",
                   # Every mode, bypassPermissions included (decided 2026-09-28). It is the one
                   # setting whose absence was felt hardest from the phone: the default is the
                   # safest mode, and with no control it was also the only reachable one.
                   "session.permission_mode",
                   # Diagnostics (feature 91): only records timings, so it reaches as well.
                   "debug.timings", "debug.timings_since",
                   # The user, 2026-10-07: on needs a code made on the machine; off arrives only
                   # sealed (with it on, the tunnel takes nothing else under /api/).
                   "remote.encrypt",
                   # A display preference (feature 92).
                   "editor.preview_dblclick",
                   # Prompt autocorrect (feature 94): a typing aid.
                   "prompt.autocorrect", "prompt.autocorrect_skip", "prompt.autocorrect_words",
                   "prompt.autocorrect_replace")
LOCAL_ONLY_SETTINGS = tuple(k for k in Config.EDITABLE if k not in REMOTE_SETTINGS)
from .mapping import (GLOBAL_ID, SOURCE_MANUAL, SOURCE_REVIEW, MappingError)
from . import bashguard, rules
from .models import ModelList
from .restart import RestartError, restart
from .store import _UNSET, MappingStore

log = logging.getLogger("aicleaner.ui")

# The model list is cached for the day; one per process (feature 45).
model_list = ModelList()


# The sign-in methods the wizard can set up unattended (D177; part D of the 2026-10-10 spec adds more).
REMOTE_IDENTITIES = ("OneTimePin",)


def remote_setup_command(shell: str, script: Path | str, hostname: str, email: str,
                         identity: str = "OneTimePin") -> list[str]:
    """How the setup wizard runs Enable-RemoteControl.ps1 (D177). Only parameters the script has -
    it was passed -LogPath, which it never had, so the run stopped before it started - and
    -AssumeDefaults, so no question is read from a closed input. The token travels in the
    environment, never here."""
    return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script),
            "-Setup", "api", "-Hostname", hostname, "-Email", email, "-Identity", identity, "-AssumeDefaults"]


def _health_state(cfg) -> dict | None:
    """What the health check last found, or None when it has never run (feature 17).

    A file the scheduled task writes, read here. Not re-run on the status poll: the checks shell
    out to PowerShell and read the registry, and doing that every ten seconds on every open page
    is the sort of thing that made the interface feel slow in the first place.

    Stale is said rather than hidden: a state file from three days ago describes a machine that
    has been switched on since, and reporting it as current would be worse than reporting
    nothing.
    """
    import time

    if cfg is None or not getattr(cfg, "state_dir", None):
        return None
    path = Path(cfg.state_dir).parent / "health.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # The task runs every 15 minutes, so an hour old means it has not been running.
    age = max(0.0, time.time() - path.stat().st_mtime)
    data["stale"] = age > 3600
    data["age_minutes"] = int(age // 60)
    return data


def _open_folders(cfg) -> list[str]:
    """The install, clients, logs and state folders, if other local users can write them (H18)."""
    if cfg is None:
        return []
    from .perms import open_folders

    app_root = Path(__file__).resolve().parent.parent
    places = [app_root, cfg.clients_dir, cfg.log_dir, getattr(cfg, "state_dir", "") or ""]
    return open_folders([str(p) for p in places if str(p).strip()])


def _share_of(entry: dict, shares: dict | None) -> str:
    folded = {str(cid).lower(): folder for cid, folder in (shares or {}).items()}
    return str(folded.get(str(entry.get("client") or "").lower()) or "")


def _settle_homes_and_shares(wanted: list, stored: list, shares: dict | None) -> list[str]:
    """Before a save, in place (DECISIONS 64): a profile whose home moves keeps the old one in
    `homes_before`, so its conversations stay listed; and a hand-added folder that is the client's
    OneDrive share is dropped - the share is always included - unless it is the profile's home,
    which it then stays as. Returns what was done, in words, for the page to say."""
    from . import workspace

    def home(entry: dict) -> str:
        try:
            return workspace.home_of(list(entry.get("workspaces") or []), list(entry.get("projects") or []),
                                     str(entry.get("home_override") or "")).get("folder", "")
        except Exception:  # noqa: BLE001 - a hint, never a reason to refuse the save
            return ""

    before = {str(e.get("name", "")).lower(): e for e in stored}
    names = {str(e.get("name", "")).lower() for e in wanted if isinstance(e, dict)}
    notes: list[str] = []
    for entry in wanted:
        if not isinstance(entry, dict):
            continue
        share = _share_of(entry, shares)
        if share:
            now = home(entry)
            folders = list(entry.get("projects") or [])
            kept = [f for f in folders if not workspace._same(f, share) or workspace._same(f, now)]
            if len(kept) != len(folders):
                entry["projects"] = kept
                notes.append(f"{entry.get('name')}: {share} was removed from the folders - it is the "
                             "client's OneDrive share, which is always included.")
        old = before.get(str(entry.get("name", "")).lower())
        # A renamed profile keeps its history (the user, 2026-10-06): `was` names the stored profile
        # it came from, honoured only when that name has left the list - renamed, not copied. What
        # it inherits is the server's own list, still held to the allowed folders where it is read.
        was_name = str(entry.pop("was", "") or "").lower()
        if old is None and was_name and was_name not in names:
            old = before.get(was_name)
        # Only ever from the stored profile and the home worked out here - never from the request:
        # a folder in this list is read for conversations, so a page that could name one could
        # have another client's conversations listed and drawn (security review, 2026-10-06).
        history = list((old or {}).get("homes_before") or [])
        was, now = (home(old) if old else ""), home(entry)
        if was and now and not workspace._same(was, now) and not any(workspace._same(was, h) for h in history):
            history.append(was)
            notes.append(f"{entry.get('name')}: the home folder moved to {now}; conversations started "
                         f"in {was} stay listed under Continue.")
        entry["homes_before"] = [h for h in history if not (now and workspace._same(h, now))]
        # Worktrees are made and removed by the server (DECISIONS 66), never named by a page: a
        # folder in this list becomes a session's home.
        entry.pop("worktrees", None)
        if (old or {}).get("worktrees"):
            entry["worktrees"] = list(old["worktrees"])
    return notes


def _share_hint(entry: dict, shares: dict | None, folders: list, home: str) -> dict:
    from . import workspace

    share = _share_of(entry, shares)
    repeated = bool(share) and any(workspace._same(f, share) for f in folders)
    return {"share": share, "share_in_folders": repeated,
            "share_is_home": repeated and bool(home) and workspace._same(share, home)}


def _workspace_view(entries: list, mf: MappingStore | None = None, shares: dict | None = None) -> dict:
    """Per profile (D99): its home folder and how it was worked out, the workspace files in use
    (its own, or for an older profile the one found inside a folder), what a workspace file lists
    that the profile has not got (D41), and the two offers - the cwd setting when the first
    workspace file lacks it (feature 71), or a new workspace file when there is none.

    Best effort throughout. This reads the disk, and a folder that has been renamed or unplugged
    should cost the hint, never the page. `mf` is unused since D99 (the client file holds nothing
    about this machine's folders) and kept for the callers' signature.
    """
    from . import workspace

    out: dict[str, dict] = {}
    for entry in entries:
        folders = list(entry.get("projects") or [])
        own = list(entry.get("workspaces") or [])
        if not folders and not own:
            continue
        try:
            workspaces = own or workspace.implicit_workspaces(folders)
            home = workspace.home_of(workspaces, folders, str(entry.get("home_override") or ""))
            first = workspaces[0] if workspaces else ""
            cwd = (workspace.cwd_offer(first, home["folder"]) if first and home.get("folder")
                   else {"current": "", "suggested": ""})
            create = None
            if not workspaces and home.get("folder"):
                path, text = workspace.new_workspace(str(entry.get("name", "")), home["folder"], folders)
                if not path.exists():
                    create = {"path": str(path), "text": text}
            out[entry.get("name", "")] = {
                "home": home, "home_derived_was": str(entry.get("home_derived_was") or ""),
                "workspaces": workspaces, "implicit": bool(workspaces and not own),
                "missing": workspace.missing_from_workspaces(workspaces, folders), "workspace": first, "cwd": cwd, "create": create,
                # The client's share, and whether a hand-added folder repeats it (DECISIONS 64).
                **_share_hint(entry, shares, folders, home.get("folder", "")),
                # The old names, kept for the page while it moves over.
                "folder": home.get("folder", ""), "how": home.get("how", ""),
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("workspace hint for %s failed: %s", entry.get("name"), exc)
    return out


def _record_provenance(mf: MappingStore, cid: str, real: str, source: str,
                       found: Any = None, note: str = "") -> None:
    """Keep the evidence for one entry, locally (feature 30).

    Best effort by design: provenance is evidence about a decision, never part of it, so a
    failure here must not lose the mapping that has already been written.
    """
    detail: dict[str, Any] = {"note": note}
    if found is not None:
        detail.update(kind=getattr(found, "kind", ""), confidence=getattr(found, "confidence", ""),
                      count=getattr(found, "count", 0), near=getattr(found, "near", ""),
                      first_seen=getattr(found, "first_seen", ""),
                      last_seen=getattr(found, "last_seen", ""),
                      paths=getattr(found, "sources", ()), projects=getattr(found, "projects", ()),
                      sessions=getattr(found, "sessions", ()),
                      seen_with=getattr(found, "seen_with", None))
    try:
        mf.provenance.record(cid, real, source, **detail)
    except Exception as exc:  # noqa: BLE001 - evidence is never worth failing a write for
        log.warning("could not record provenance for %s/%s: %s", cid, real, exc)

WEB = Path(__file__).resolve().parent / "web"  # the static single-page app (index.html, css/, js/)
# A wiki page's name: what docs/wiki holds, and nothing that could step out of it.
WIKI_PAGE = re.compile(r"[A-Za-z0-9-]{1,60}")
REDIRECTS = {"/ui": "/#/review", "/ui/": "/#/review", "/config": "/#/settings", "/secrets": "/#/secrets"}

def build_ui_routes(app: web.Application, mf: MappingStore, store: CandidateStore, log_dir: Path, host: str,
                    contact: ContactSource | None = None, user_secrets: Any = None, proxy: Any = None) -> None:
    contact = contact or ContactSource()

    def err(exc: Exception, status: int = 400, suggest: str | None = None) -> web.Response:
        body: dict[str, Any] = {"error": str(exc)}
        # A structured refusal (MappingError with why/offer, feature 41) carries the rule it is
        # protecting and an action the page can put behind a button. Bare exceptions have
        # neither and are flattened exactly as before.
        why = getattr(exc, "why", "")
        offer = getattr(exc, "offer", None)
        if why:
            body["why"] = why
        if offer:
            body["offer"] = offer
        if suggest:
            body["suggest"] = suggest  # what the UI can offer instead (see treat_as_public)
        return web.json_response(body, status=status)

    async def candidates(request: web.Request) -> web.Response:
        # kind_offers: kinds ignored far more often than kept, which Review offers to stop (31).
        return web.json_response({"pending": store.pending(), "kind_offers": store.kind_offers()})

    # ---- each client's OneDrive share (shares.py) ---------------------------------------

    def share_label(cid: str) -> str:
        """The name a person reads, which is what a created folder is called."""
        try:
            for client in mf.load().data.get("clients", []):
                if str(client.get("id", "")).lower() == cid.lower():
                    return str(client.get("label") or client.get("id") or cid)
        except Exception:                                    # a broken mapping is reported elsewhere
            pass
        return cid

    def share_view(cid: str) -> dict[str, Any]:
        from .shares import onedrive_root

        root = onedrive_root()
        return {"client": cid, "share": proxy.share_store.get(cid) if proxy else "",
                "onedrive": str(root) if root else "", "suggested": share_label(cid)}

    async def client_onedrive(request: web.Request) -> web.Response:
        """Where this client's folder opens in OneDrive on the web, for its Share button (DECISIONS 67)."""
        from .shares import onedrive_web

        cid = request.match_info["cid"]
        folder = Path(proxy.cfg.clients_dir) / cid
        answer = await asyncio.to_thread(onedrive_web, folder)
        return web.json_response({**answer, "folder": str(folder)})

    async def client_share(request: web.Request) -> web.Response:
        cid = request.match_info["cid"]
        if cid.lower() == GLOBAL_ID.lower():
            return err(ValueError("Global is not an engagement, so it has no share"), status=404)
        return web.json_response(share_view(cid))

    async def client_share_save(request: web.Request) -> web.Response:
        """{path} to use a folder that exists, {create: true} to make one, {path: ""} to forget it."""
        from .shares import ShareError

        cid = request.match_info["cid"]
        if cid.lower() == GLOBAL_ID.lower():
            return err(ValueError("Global is not an engagement, so it has no share"), status=404)
        from .folders import FolderError, FolderOutside, check_usable

        body = await request.json()
        try:
            if body.get("create"):
                # Created inside OneDrive by the store, so it lands where OneDrive is; it still has
                # to come out inside an allowed root, or it is a folder the pages may not reach.
                proxy.share_store.create(cid, share_label(cid))
                check_usable(proxy.share_store.get(cid), folder_roots(), "share folder")
            elif str(body.get("path", "")).strip():
                # boundary only: the share store says "does not exist" better than this can
                check_usable(str(body["path"]), folder_roots(), "share folder", must_exist=False)
                proxy.share_store.set(cid, str(body["path"]))
            else:
                proxy.share_store.clear(cid)
        except FolderOutside as problem:
            return err(problem, status=403)
        except FolderError as problem:
            return err(problem)
        except ShareError as problem:
            return err(problem)
        # Sessions read it from their profile; a running one keeps what it was started with.
        proxy.reload_profiles()
        return web.json_response(share_view(cid))

    # ---- driving a session (session.py) -------------------------------------------------

    def session_host():
        """The session host, or a clear error when driving a session is switched off."""
        from .session import SessionError

        session = getattr(proxy, "session", None)
        if session is None:
            raise SessionError("driving a session is switched off (session.enabled)")
        return session

    async def session_profiles(request: web.Request) -> web.Response:
        """The profiles, and the clients one can be pointed at - by label, never by id."""
        from .session import SessionError

        try:
            host = session_host()
        except SessionError as problem:
            return err(problem, status=404)
        entries = proxy.read_profiles()
        running = [n for n, sess in host.sessions.items() if sess.running]
        labels = proxy.client_labels()
        return web.json_response({
            "profiles": entries,
            # by the name a person reads, since that is the only thing the picker shows
            "clients": [{"id": cid, "label": label}
                        for cid, label in sorted(labels.items(), key=lambda kv: kv[1].lower())
                        if cid and cid.lower() != GLOBAL_ID.lower()],
            "running": running,
            "max_running": host.max_running,
            "problem": proxy.profile_problem,
            "path": str(proxy.profile_store.path),
            # Which folder is the primary repo, on what grounds, and what its workspace file
            # lists that this profile has not got (D41). Computed here rather than on the page
            # because it reads the disk, and a page that read the disk would do it per keystroke.
            "workspace": await asyncio.to_thread(_workspace_view, entries, mf, proxy.share_store.load()),
        })

    async def session_profiles_save(request: web.Request) -> web.Response:
        """Validate, then commit - the same path a client file takes, for the same reason."""
        from .profiles import ProfileError
        from .session import SessionError

        try:
            host = session_host()
        except SessionError as problem:
            return err(problem, status=404)
        body = await request.json()
        wanted = body.get("profiles")
        if not isinstance(wanted, list):
            return err(ValueError("send {profiles: [...]}"))
        kept = {str(entry.get("name", "")).lower() for entry in wanted if isinstance(entry, dict)}
        for name, sess in host.sessions.items():
            if sess.running and name.lower() not in kept:
                return err(ValueError(f"{name} is running; stop it before removing its profile"))
        # A profile's project folders are folders a session may read and write, so they answer to
        # folders.roots like anything else. Checked before the file is written rather than when a
        # session is started, so a profile cannot be saved naming somewhere it may never go.
        from .folders import FolderError, FolderOutside, check_usable

        roots = folder_roots()
        for entry in wanted:
            if not isinstance(entry, dict):
                continue
            # The folders, the workspace files' folders and the home override are all held to the
            # same allowed locations (D99): a workspace file must not widen what the pages reach.
            places = [(p, "project folder") for p in entry.get("projects") or []]
            places += [(str(Path(w).parent), "workspace file's folder") for w in entry.get("workspaces") or []]
            if str(entry.get("home_override") or "").strip():
                places.append((entry["home_override"], "home folder"))
            for project, what in places:
                if not str(project).strip():
                    continue
                try:
                    check_usable(project, roots, what)
                except FolderOutside as problem:
                    return err(ValueError(f"{entry.get('name', 'profile')}: {problem}"), status=403)
                except FolderError as problem:
                    return err(ValueError(f"{entry.get('name', 'profile')}: {problem}"))
            for ws in entry.get("workspaces") or []:
                if str(ws).strip() and not Path(str(ws)).is_file():
                    return err(ValueError(f"{entry.get('name', 'profile')}: {ws} does not exist"))
        notes = await asyncio.to_thread(_settle_homes_and_shares, wanted, proxy.read_profiles(),
                                        proxy.share_store.load())
        try:
            saved = proxy.profile_store.save(wanted, set(proxy.client_labels()))
        except (ProfileError, OSError) as problem:
            return err(problem)
        proxy.reload_profiles()
        return web.json_response({"profiles": saved, "notes": notes})

    async def session_profile_add_folder(request: web.Request) -> web.Response:
        """Add one of the client's folders to a profile, from Continue (feature 50).

        Only a folder the profile's tree already shows - the client's workspace folders (D72) -
        and only inside the allowed locations, so this can widen a profile to its own client's
        repos and to nothing else."""
        from .folders import FolderError, check_usable
        from .profiles import ProfileError
        from .session import SessionError
        from .workspace import _same

        body = await request.json()
        name, folder = str(body.get("name", "")).strip(), str(body.get("folder", "")).strip()
        try:
            host = session_host()
            profile = host.profile(name)
        except SessionError as problem:
            return err(problem, status=404)
        if not any(_same(folder, f) for f in host.tree_of(profile)):
            return err(ValueError(f"{folder} is not one of {profile.name}'s client folders"), status=403)
        try:
            check_usable(folder, folder_roots(), "project folder")
        except FolderError as problem:
            return err(problem, status=403)
        entries = proxy.read_profiles()
        for entry in entries:
            if entry.get("name") == profile.name and not any(_same(folder, p) for p in entry.get("projects") or []):
                entry["projects"] = list(entry.get("projects") or []) + [folder]
        try:
            proxy.profile_store.save(entries, set(proxy.client_labels()))
        except (ProfileError, OSError) as problem:
            return err(problem)
        proxy.reload_profiles()
        return web.json_response({"ok": True, "folder": folder})

    # ---- worktree sessions (DECISIONS 66, feature 86) -------------------------------------------

    def _save_worktrees(parent: str, change) -> None:
        """Apply `change(list)` to the stored profile's worktrees, then save and reload."""
        entries = proxy.read_profiles()
        for entry in entries:
            if entry.get("name") == parent:
                kept = change(list(entry.get("worktrees") or []))
                entry.pop("worktrees", None)
                if kept:
                    entry["worktrees"] = kept
        proxy.profile_store.save(entries, set(proxy.client_labels()))
        proxy.reload_profiles()

    def _worktree_session(name: str):
        """The stopped worktree session called `name`, or why not."""
        from .session import SessionError

        profile = session_host().profile(name)
        if not profile.worktree_of:
            raise SessionError(f"{profile.name} is not a worktree session")
        running = session_host().sessions.get(profile.name)
        if running is not None and running.running:
            raise SessionError(f"{profile.name} is running; stop it first")
        return profile

    async def session_worktree_offer(request: web.Request) -> web.Response:
        """What "Create a worktree" can offer: the profile's git repos, its other folders that could
        be made one (not in OneDrive), and a default name."""
        from . import worktree
        from .session import SessionError

        try:
            profile = session_host().profile(request.query.get("name", ""))
        except SessionError as problem:
            return err(problem, status=404)
        if profile.worktree_of:
            return err(ValueError(f"{profile.name} is already a worktree session"))
        folders = [str(p) for p in profile.projects if Path(p).is_dir()]
        repos = worktree.repos_of(folders)
        plain = [f for f in folders if f not in repos and not worktree._synced(f)]
        taken = [p.worktree.get("name", "") for p in session_host().profiles if p.worktree_of == profile.name]
        return web.json_response({"repos": repos, "folders": plain, "default": worktree.default_name(taken)})

    async def session_worktree_create(request: web.Request) -> web.Response:
        """git worktree add beside the repo, and a session `<profile> @ <name>` homed there. With
        `init`, a plain folder of the profile is made a repo first."""
        from . import worktree
        from .folders import FolderError, check_usable
        from .profiles import ProfileError
        from .session import SessionError
        from .workspace import _same

        body = await request.json()
        try:
            profile = session_host().profile(str(body.get("name", "")))
            if profile.worktree_of:
                raise SessionError(f"{profile.name} is already a worktree session")
            repo = str(body.get("repo", "")).strip()
            # Only one of the profile's own folders: those were checked against the allowed
            # locations when it was saved, and this one gains a sibling.
            if not any(_same(repo, p) for p in profile.projects):
                raise SessionError(f"{repo} is not one of {profile.name}'s folders")
            name = worktree.check_name(str(body.get("branch", "")))
            # It goes beside the repo, so the folder holding the repo must be allowed too.
            check_usable(str(Path(repo).parent), folder_roots(), "folder the worktree goes in")
            if body.get("init") and not worktree.is_repo(repo):
                await asyncio.to_thread(worktree.init_repo, repo)
            path = await asyncio.to_thread(worktree.create, repo, name)
            _save_worktrees(profile.name, lambda ws: ws + [{"name": name, "repo": repo, "path": str(path)}])
        except (SessionError, worktree.WorktreeError, FolderError, ProfileError, OSError) as problem:
            return err(problem)
        return web.json_response({"session": f"{profile.name} @ {name}", "path": str(path)})

    async def session_worktree_merge(request: web.Request) -> web.Response:
        """Merge a stopped worktree session's branch into its repo. Conflicts are reported, not resolved."""
        from . import worktree
        from .session import SessionError

        body = await request.json()
        try:
            profile = _worktree_session(str(body.get("name", "")))
            w = profile.worktree
            said = await asyncio.to_thread(worktree.merge, w["repo"], w["name"], w["path"])
        except (SessionError, worktree.WorktreeError) as problem:
            return err(problem)
        return web.json_response({"ok": True, "message": said})

    async def session_worktree_remove(request: web.Request) -> web.Response:
        """Remove a stopped worktree session: the worktree, its branch when merged, and the session."""
        from . import worktree
        from .profiles import ProfileError
        from .session import SessionError

        body = await request.json()
        try:
            profile = _worktree_session(str(body.get("name", "")))
            w = profile.worktree
            said = await asyncio.to_thread(worktree.remove, w["repo"], w["name"], w["path"])
            _save_worktrees(profile.worktree_of, lambda ws: [x for x in ws if x.get("name") != w["name"]])
            session_host().sessions.pop(profile.name, None)
        except (SessionError, worktree.WorktreeError, ProfileError, OSError) as problem:
            return err(problem)
        return web.json_response({"ok": True, "message": said, "session": profile.worktree_of})

    async def session_state(request: web.Request) -> web.Response:
        from .session import SessionError

        from . import timing

        parts = request.get(timing.REQUEST_PARTS)        # set only while timings are recorded
        # Only what the page has not seen (D140): the last event number it holds, and whose.
        try:
            since = int(request.query["since"]) if "since" in request.query else None
        except ValueError:
            since = None
        try:
            state = session_host().state(request.query.get("name", ""), parts=parts, since=since,
                                         epoch=request.query.get("epoch", ""))
        except SessionError as exc:   # switched off: say so, rather than a 500
            return err(exc)
        # The model request this session has open, if any (DECISIONS 47).
        if proxy is not None and hasattr(proxy, "model_requests"):
            from .proxy import request_state_for
            state["model_request"] = request_state_for(state.get("selected") or state.get("name", ""),
                                                       proxy.model_requests(), time.time())
        if parts is None:
            return web.json_response(state)
        started = time.perf_counter()
        text = json.dumps(state)
        timing.part(request, "json", started)
        parts.append(("events", float(len(state.get("events") or []))))   # a count, not a time
        return web.Response(text=text, content_type="application/json")

    async def session_past(request: web.Request) -> web.Response:
        """What this profile has talked about before, so a conversation can be picked up."""
        from .session import SessionError

        name = request.query.get("name", "")
        try:
            # Off the loop: this opens and reads transcripts looking for their opening message.
            past = await asyncio.to_thread(session_host().past, name, 25, request.query.get("all") == "1")
            return web.json_response({"projects": past})
        except SessionError as problem:
            return err(problem, status=404)

    async def session_earlier(request: web.Request) -> web.Response:
        """The 500 events before what the page shows, for "Show earlier" (feature 82)."""
        from .session import SessionError

        try:
            before = float(request.query.get("before") or 0)
        except ValueError:
            return err(ValueError("before must be a time"))
        try:
            answer = await asyncio.to_thread(session_host().earlier, request.query.get("name", ""), before)
            return web.json_response(answer)
        except SessionError as problem:
            return err(problem, status=404)

    async def tunnel_params(request: web.Request) -> web.Response:
        """What a device needs to derive the long-term key (feature 21). Plain by design: a salt
        and a count, nothing that opens or checks anything. `sealed` is true only through the
        tunnel with encryption on and a code set."""
        sealed = bool(request.get(REMOTE)) and proxy.cfg.remote_encrypt and proxy.tunnel.key() is not None
        out: dict[str, Any] = {"sealed": sealed, "storage": proxy.cfg.remote_key_storage}
        if sealed:
            out.update(proxy.tunnel.params())
        return web.json_response(out)

    async def tunnel_hello(request: web.Request) -> web.Response:
        """Step one of a connection's key exchange (DECISIONS 60): the page's throwaway public key
        and nonce in, the proxy's out, with the proxy's proof that it holds the code."""
        from .tunnelcrypt import SealError

        key = proxy.tunnel.key()
        if key is None:
            return err(ValueError("no tunnel code is set"), status=403)
        body = await request.json()
        try:
            return web.json_response(proxy.tunnel_sessions.hello(key, str(body.get("client_pub", "")),
                                                                 str(body.get("client_nonce", ""))))
        except SealError as exc:
            return err(exc)

    async def tunnel_confirm(request: web.Request) -> web.Response:
        """Step two: the page proves it holds the code too. Until then the session carries nothing."""
        from .tunnelcrypt import SealError

        key = proxy.tunnel.key()
        if key is None:
            return err(ValueError("no tunnel code is set"), status=403)
        body = await request.json()
        try:
            proxy.tunnel_sessions.confirm(key, str(body.get("sid", "")), str(body.get("proof", "")))
        except SealError as exc:
            return err(exc, status=403)
        return web.json_response({"ok": True})

    async def tunnel_secret(request: web.Request) -> web.Response:
        """Whether a code is set, and the two settings. On this machine only."""
        if request.get(REMOTE):
            return err(ValueError("the tunnel code is set on the machine itself"), status=403)
        return web.json_response({"set": proxy.tunnel.is_set(), "legacy": proxy.tunnel.legacy(),
                                  "encrypt": proxy.cfg.remote_encrypt, "storage": proxy.cfg.remote_key_storage})

    async def tunnel_secret_set(request: web.Request) -> web.Response:
        """Generate a new code, returned this once (DECISIONS 60: codes only, no chosen passphrase).
        On this machine only; every connection is dropped and every device must enter the new one."""
        if request.get(REMOTE):
            return err(ValueError("the tunnel code is set on the machine itself"), status=403)
        body = await request.json()
        if not body.get("generate"):
            return err(ValueError("only a generated code can be set - choose Generate"))
        try:
            code = proxy.tunnel.generate()
        except (ValueError, RuntimeError) as exc:
            return err(exc)
        proxy.tunnel_sessions.forget_all()
        return web.json_response({"ok": True, "code": code})

    async def session_commands(request: web.Request) -> web.Response:
        """What can be typed after a slash here: the files on this machine, not a terminal's menu."""
        from .commands import available
        from .session import SessionError

        host = session_host()
        try:
            profile = host.profile(request.query.get("name", ""))
        except SessionError as problem:
            return err(problem, status=404)
        # What the running session said it accepts, when one has (feature 84).
        running = host.sessions.get(profile.name)
        offered = running.slash_commands if running is not None and running.slash_commands else None
        return web.json_response({"commands": available(profile.projects, offered=offered)})

    # ---- file tabs (feature 54, 74; DECISIONS 50) -------------------------------------------
    #
    # The first endpoints that return a project file's CONTENT - as it is on disk, real names and
    # all, because it goes to the person, not the AI service. Held to the tree's own allow-list.

    def _file_args(request: web.Request) -> tuple[str, str, str]:
        q = request.query
        return q.get("name", ""), q.get("root", ""), q.get("path", "")

    async def session_file(request: web.Request) -> web.Response:
        from .session import SessionError

        name, root, path = _file_args(request)
        try:
            answer = await asyncio.to_thread(session_host().read_file, name, root, path)
        except (SessionError, OSError) as problem:
            return web.json_response({"error": str(problem)}, status=403)
        return web.json_response(answer)

    async def session_file_write(request: web.Request) -> web.Response:
        """Save a file tab (F54, DECISIONS 53). 409 with the file's time now when it changed on disk
        since the tab read it; sending that time back with the same text overwrites."""
        from .session import SessionError, StaleWrite

        body = await request.json()
        try:
            answer = await asyncio.to_thread(
                session_host().write_file, str(body.get("name", "")), str(body.get("root", "")),
                str(body.get("path", "")), str(body.get("text", "")), float(body.get("modified", 0) or 0))
        except StaleWrite as stale:
            return web.json_response({"error": str(stale), "stale": True, "modified": stale.modified}, status=409)
        except (SessionError, OSError, ValueError) as problem:
            return web.json_response({"error": str(problem)}, status=403)
        return web.json_response(answer)

    async def _send_file(request: web.Request, inline: bool) -> web.StreamResponse:
        from .session import MAX_FILE_SEND, SessionError, file_kind, inline_type

        name, root, path = _file_args(request)
        try:
            target = await asyncio.to_thread(session_host().locate_file, name, root, path)
            size = target.stat().st_size
        except (SessionError, OSError) as problem:
            return web.json_response({"error": str(problem)}, status=403)
        if size > MAX_FILE_SEND:
            return web.json_response({"error": f"{target.name} is {size // (1024 * 1024)} MB; "
                                               f"the limit is {MAX_FILE_SEND // (1024 * 1024)} MB"}, status=413)
        if inline:
            kind = inline_type(target.name)
            if not kind or file_kind(target.name)[1] == "download":
                return web.json_response({"error": f"{target.name} cannot be shown in the page"}, status=403)
            response = web.FileResponse(target, headers={"Content-Type": kind})
            # Framed by the page only, in place of the guard's own policy, which forbids all framing.
            # An image is sandboxed outright. A PDF is not: Chromium's viewer will not run in a
            # sandboxed document or under default-src 'none', and it runs no script from the file.
            request[FILE_CSP] = ("frame-ancestors 'self'" if kind == "application/pdf" else
                                 "sandbox; default-src 'none'; img-src 'self'; frame-ancestors 'self'")
            return response
        quoted = urllib.parse.quote(target.name)
        return web.FileResponse(target, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})

    async def session_file_raw(request: web.Request) -> web.StreamResponse:
        return await _send_file(request, inline=True)

    async def session_file_download(request: web.Request) -> web.StreamResponse:
        return await _send_file(request, inline=False)

    async def session_files_zip(request: web.Request) -> web.StreamResponse:
        """Many files and folders as one zip (feature 97): paths kept relative to their root,
        refused (413) before anything is written when the whole is too big."""
        import tempfile

        from .filezip import ZipTooBig, plan_zip, write_zip
        from .session import SessionError

        body = await request.json()
        items = [i for i in (body.get("items") or []) if isinstance(i, dict)][:1000]
        if not items:
            return web.json_response({"error": "nothing chosen to download"}, status=400)
        try:
            located = await asyncio.to_thread(session_host().locate_items, str(body.get("name", "")), items)
            plan = await asyncio.to_thread(plan_zip, located)
        except SessionError as problem:
            return web.json_response({"error": str(problem)}, status=403)
        except ZipTooBig as big:
            return web.json_response({"error": str(big), "files": big.files, "size": big.size}, status=413)
        handle, temp = tempfile.mkstemp(suffix=".zip", prefix="aicleaner-")
        os.close(handle)
        dest = Path(temp)
        try:
            await asyncio.to_thread(write_zip, plan, dest)
            single = located[0][1] if len(located) == 1 and located[0][1].is_dir() else None
            filename = f"{single.name}.zip" if single else f"files-{datetime.now():%Y%m%d-%H%M%S}.zip"
            from .tunnelgate import INSIDE_TUNNEL, MAX_SEALED_FILE
            if request.get(INSIDE_TUNNEL):
                # Inside a sealed envelope: the whole zip as one answer, to be sealed (D168).
                size = dest.stat().st_size
                if size > MAX_SEALED_FILE:
                    return web.json_response({"error": f"the zip is {size // (1024 * 1024)} MB; through the encrypted "
                                                       f"tunnel the limit is {MAX_SEALED_FILE // (1024 * 1024)} MB"}, status=413)
                return web.Response(body=await asyncio.to_thread(dest.read_bytes), content_type="application/zip",
                                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}"})
            response = web.StreamResponse(headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}",
                "Content-Length": str(dest.stat().st_size)})
            await response.prepare(request)
            with dest.open("rb") as fh:
                while chunk := fh.read(256 * 1024):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            dest.unlink(missing_ok=True)

    async def session_file_stat(request: web.Request) -> web.Response:
        """Many files at once: the page's 15-second changed-on-disk check."""
        from .session import SessionError

        body = await request.json()
        files = [f for f in (body.get("files") or []) if isinstance(f, dict)]
        try:
            answer = await asyncio.to_thread(session_host().stat_files, str(body.get("name", "")), files)
        except SessionError as problem:
            return web.json_response({"error": str(problem)}, status=403)
        return web.json_response({"files": answer, "checked_at": time.time()})

    async def session_files(request: web.Request) -> web.Response:
        """One folder of one configured project, so a path can be pointed at rather than typed."""
        from .session import SessionError

        name, root = request.query.get("name", ""), request.query.get("root", "")
        path = request.query.get("path", "")
        try:
            # Off the loop: a folder listing is filesystem work, and the share is normally a
            # OneDrive folder that can take seconds when the OS has nothing cached. Left in the
            # coroutine it stopped the single event loop serving anything - the other UI requests,
            # the poll, and the proxy, which shares it.
            listing = await asyncio.to_thread(
                lambda: session_host().browse(name=name, root=root, path=path))
        except SessionError as problem:
            return web.json_response({"error": str(problem)}, status=403)
        return web.json_response(listing)

    async def autocorrect_words(request: web.Request) -> web.Response:
        """The prompt autocorrect's word list (feature 94), grouped by SCOWL band (10 = most common),
        each band one space-separated string. About 75,000 words: cached by the browser for a day,
        revalidated against the file's ETag, compressed on the wire."""
        path = Path(__file__).resolve().parent / "data" / "autocorrect-words.txt"
        # Words it knows with their capitals - days, months, product names (feature 100).
        terms_path = path.with_name("autocorrect-terms.txt")
        try:
            stat = path.stat()
            terms_stat = terms_path.stat()
        except OSError:
            return web.json_response({"bands": {}, "terms": []})
        tag = f'"{int(stat.st_mtime)}-{stat.st_size}-{int(terms_stat.st_mtime)}-{terms_stat.st_size}"'
        if request.headers.get("If-None-Match") == tag:
            return web.Response(status=304, headers={"ETag": tag})

        def read() -> dict[str, list[str]]:
            bands: dict[str, list[str]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and not line.startswith("#") and "\t" in line:
                    word, band = line.split("\t", 1)
                    bands.setdefault(band.strip(), []).append(word)
            return bands

        bands = await asyncio.to_thread(read)
        terms = [line.strip() for line in terms_path.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        response = web.json_response({"bands": {b: " ".join(ws) for b, ws in bands.items()}, "terms": terms},
                                     headers={"ETag": tag, "Cache-Control": "private, max-age=86400"})
        response.enable_compression()
        return response

    async def autocorrect_clients(request: web.Request) -> web.Response:
        """The single words in the filter's client names (feature 94), for autocorrect to leave alone
        when the user chose to - real names, as the rest of the page shows them."""
        words: set[str] = set()
        handle = getattr(proxy, "_handle", None) if proxy is not None else None
        mapper = handle.mapper if handle is not None else None
        for form in (getattr(mapper, "forms", None) or []):
            for word in re.findall(r"[A-Za-z][A-Za-z']+", str(getattr(form, "real", ""))):
                words.add(word.lower())
        return web.json_response({"words": sorted(words)})

    async def debug_timings(request: web.Request) -> web.Response:
        """The page's timing report (feature 91), written to logs/timings-<date>.jsonl - only while
        Diagnostics records timings; otherwise accepted and dropped."""
        from . import timing


        if proxy is None or not timing.active(proxy.cfg):
            return web.json_response({"ok": True, "kept": 0})
        body = await request.json()
        who = "remote" if request.get(REMOTE) else "local"
        kept = await asyncio.to_thread(proxy.timing_log.write, body.get("entries") or [], who)
        return web.json_response({"ok": True, "kept": kept})

    async def editor_ps_commands(request: web.Request) -> web.Response:
        """The PowerShell commands on this machine, for the file editor's completion (feature 80).
        Compact, and cached by the page against an ETag: about 40,000 with Az and Graph."""
        ps = proxy.ps_commands
        tag = ps.version()
        if request.headers.get("If-None-Match") == tag:
            return web.Response(status=304, headers={"ETag": tag})
        answer = await asyncio.to_thread(ps.compact)
        return web.json_response(answer, headers={"ETag": ps.version(), "Cache-Control": "no-cache"})

    async def editor_ps_parameters(request: web.Request) -> web.Response:
        """One command's own parameters (feature 80), fetched when the editor first needs them."""
        try:
            found = await asyncio.to_thread(proxy.ps_commands.parameters, request.query.get("command", ""))
        except ValueError as problem:
            return err(problem)
        return web.json_response({"command": request.query.get("command", ""), "parameters": found})

    async def session_search(request: web.Request) -> web.Response:
        """Files and folders by name across every folder a session's tree shows (feature 90)."""
        from .session import SessionError

        name, query = request.query.get("name", ""), request.query.get("q", "")
        try:
            more = request.query.get("more") == "1"          # carry on past the time limit (D146)
            found = await asyncio.to_thread(lambda: session_host().search(name, query, more=more))
        except SessionError as problem:
            return err(problem)
        return web.json_response(found)

    # Which folders are open in a session's tree, per client (D73). View state, on this machine,
    # beside profiles.json - never in the shared client file.
    def tree_state():
        from .treestate import TREE_STATE_FILE, TreeState
        return TreeState(mf.mapping_path.parent / TREE_STATE_FILE)

    async def session_tree_state(request: web.Request) -> web.Response:
        client = request.query.get("client", "").strip()
        return web.json_response({"client": client, "open": tree_state().load(client) if client else []})

    async def session_tree_state_save(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            kept = tree_state().save(str(body.get("client", "")).strip(), body.get("open"))
        except ValueError as exc:
            return err(exc)
        return web.json_response({"ok": True, "open": kept})

    # Which file tabs each session has open (D136), beside the tree's state, never in a client file.
    def tabs_state():
        from .tabstate import TABS_STATE_FILE, TabState
        return TabState(mf.mapping_path.parent / TABS_STATE_FILE)

    async def session_tabs_state(request: web.Request) -> web.Response:
        name = request.query.get("name", "").strip()
        return web.json_response({"name": name, **(tabs_state().load(name) if name else {"tabs": [], "active": ""})})

    async def session_tabs_state_save(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            kept = tabs_state().save(str(body.get("name", "")).strip(), body.get("tabs"), body.get("active"))
        except ValueError as exc:
            return err(exc)
        return web.json_response({"ok": True, **kept})

    async def session_workspace_cwd(request: web.Request) -> web.Response:
        """Add the terminal working directory to a profile's workspace file (feature 71).

        Offered on the page, written only here and only when asked. Recomputed from the profile
        rather than trusting a path from the page: the home folder (D99), named as the profile's
        first workspace file names it. A setting already there is never overwritten."""
        from . import workspace

        body = await request.json()
        name = str(body.get("name", "")).strip()
        # A folder chosen on the page (D131), for a profile saved or not: written only when the
        # file is a workspace file inside the allowed folders and lists that name itself.
        chosen_ws, chosen = str(body.get("workspace") or "").strip(), str(body.get("folder") or "").strip()
        if chosen_ws or chosen:
            from .folders import FolderError, check_usable

            try:
                check_usable(str(Path(chosen_ws).parent), folder_roots(), "workspace file's folder")
            except FolderError as problem:
                return err(problem, status=403)
            if not chosen_ws.lower().endswith(".code-workspace") or not Path(chosen_ws).is_file():
                return err(ValueError(f"{chosen_ws} is not a workspace file"))
            names = [n for n, _f in await asyncio.to_thread(workspace.cwd_choices, chosen_ws)]
            if chosen not in names:
                return err(ValueError(f"{Path(chosen_ws).name} does not list a folder called {chosen}"))
            value = "${workspaceFolder:" + chosen + "}"
            try:
                await asyncio.to_thread(workspace.add_cwd_setting, chosen_ws, value)
            except (OSError, ValueError) as exc:
                return err(exc)
            return web.json_response({"ok": True, "workspace": chosen_ws, "value": value})
        entry = next((e for e in proxy.read_profiles() if e.get("name") == name), None) if proxy else None
        if entry is None:
            return err(ValueError(f"no profile named {name!r}"))
        view = (await asyncio.to_thread(_workspace_view, [entry], mf)).get(name) or {}
        folder, found = view.get("folder", ""), view.get("workspace", "")
        suggested = (view.get("cwd") or {}).get("suggested", "")
        if not folder or not found or not suggested:
            return err(ValueError("nothing to set: no workspace file lists the home folder, or it already has the setting"))
        try:
            await asyncio.to_thread(workspace.add_cwd_setting, found, suggested)
        except (OSError, ValueError) as exc:
            return err(exc)
        return web.json_response({"ok": True, "workspace": found, "value": suggested})

    async def session_workspace_files(request: web.Request) -> web.Response:
        """The `.code-workspace` files directly in a folder the pages may reach, for the profile
        editor's Browse (D99). Names only, never contents."""
        from .fileio import network_path
        from .folders import FolderError, check_usable
        from .workspace import WORKSPACE_GLOB

        folder = str(request.query.get("folder", "")).strip()
        if not folder or network_path(folder):
            return err(ValueError("give a folder on this machine"))
        try:
            check_usable(folder, folder_roots(), "folder")
        except FolderError as problem:
            return err(problem, status=403)
        found = await asyncio.to_thread(lambda: sorted(str(p) for p in Path(folder).glob(WORKSPACE_GLOB)))
        return web.json_response({"files": found})

    async def session_workspace_view(request: web.Request) -> web.Response:
        """One profile's workspace hints worked out from what the editor holds now, saved or not
        (the user, 2026-10-04): removing a folder has to take its workspace file's offer with it,
        and the page cannot read the disk to know. Every place is held to folders.roots first, as
        saving holds it; one outside them is left out of the answer rather than read."""
        from .fileio import network_path
        from .folders import FolderError, check_usable

        body = await request.json()
        entry = body.get("profile") if isinstance(body, dict) else None
        if not isinstance(entry, dict):
            return err(ValueError("send {profile: {...}}"))
        roots = folder_roots()

        def allowed(path: str, what: str) -> bool:
            if not str(path).strip() or network_path(str(path)):
                return False
            try:
                check_usable(str(path), roots, what)
            except FolderError:
                return False
            return True

        clean = {
            "name": str(entry.get("name", "")),
            "client": str(entry.get("client", "")),
            "projects": [p for p in entry.get("projects") or [] if allowed(p, "project folder")],
            "workspaces": [w for w in entry.get("workspaces") or []
                           if allowed(str(Path(str(w)).parent), "workspace file's folder")],
            "home_override": (entry.get("home_override") or "")
            if allowed(entry.get("home_override") or "", "home folder") else "",
            "home_derived_was": str(entry.get("home_derived_was") or ""),
        }
        view = await asyncio.to_thread(_workspace_view, [clean], None, proxy.share_store.load())
        return web.json_response({"workspace": view.get(clean["name"])})

    async def session_workspace_create(request: web.Request) -> web.Response:
        """Write the new workspace file the page offered (D99) and make it the profile's first.

        Recomputed here, never taken from the page; never over an existing file; only inside the
        allowed folder locations. The profile is saved with the new file first."""
        from . import workspace
        from .folders import is_usable

        name = str((await request.json()).get("name", "")).strip()
        entries = proxy.read_profiles() if proxy else []
        entry = next((e for e in entries if e.get("name") == name), None)
        if entry is None:
            return err(ValueError(f"no profile named {name!r}"))
        create = ((await asyncio.to_thread(_workspace_view, [entry], mf)).get(name) or {}).get("create")
        if not create:
            return err(ValueError("nothing to create: the profile already has a workspace file, or no home folder"))
        path = Path(create["path"])
        if not is_usable(path.parent, proxy._allowed_roots()):
            return err(ValueError(f"{path.parent} is outside the folders the pages may reach"), status=403)
        try:
            await asyncio.to_thread(workspace.write_new_workspace, path, create["text"])
            updated = [{**e, "workspaces": [str(path)] + list(e.get("workspaces") or [])} if e is entry else e
                       for e in entries]
            proxy.profile_store.save(updated, set(proxy.client_labels()))
            proxy.reload_profiles()
        except (OSError, ValueError) as exc:
            return err(exc)
        return web.json_response({"ok": True, "workspace": str(path)})

    async def session_start(request: web.Request) -> web.Response:
        from .session import SessionError

        body = await request.json()
        if proxy is not None:
            # A session may open a project whose .mcp.json is new: audit before it starts (H11).
            await proxy.audit_mcp()
        try:
            state = await session_host().start(name=str(body.get("name", "")),
                                               project=str(body.get("project", "")),
                                               resume=str(body.get("resume", "")),
                                               model=str(body.get("model", "")))
        except SessionError as exc:
            return err(exc)
        return web.json_response(state)

    async def session_prompt(request: web.Request) -> web.Response:
        from .session import SessionError

        body = await request.json()
        try:
            # "queued": a turn is running, so it waits on the page until that turn ends (D176).
            outcome = await session_host().prompt(str(body.get("text", "")), name=str(body.get("name", "")))
        except SessionError as exc:
            return err(exc)
        return web.json_response({"ok": True, "outcome": outcome})

    async def session_queue_send(request: web.Request) -> web.Response:
        """A waiting prompt sent now: first in line, and the running turn interrupted (D176)."""
        from .session import SessionError

        body = await request.json()
        try:
            outcome = await session_host().send_queued_now(str(body.get("id", "")), name=str(body.get("name", "")))
        except SessionError as exc:
            return err(exc)
        return web.json_response({"ok": True, "outcome": outcome})

    async def session_queue_cancel(request: web.Request) -> web.Response:
        from .session import SessionError

        body = await request.json()
        try:
            await session_host().cancel_queued(str(body.get("id", "")), name=str(body.get("name", "")))
        except SessionError as exc:
            return err(exc)
        return web.json_response({"ok": True})

    async def session_stop(request: web.Request) -> web.Response:
        from .session import SessionError

        body = await request.json()
        try:
            await session_host().stop(name=str(body.get("name", "")))
        except SessionError as exc:
            return err(exc)
        return web.json_response({"ok": True})

    async def session_interrupt(request: web.Request) -> web.Response:
        """Stop the turn that is running and keep the session alive (feature 48, Escape).

        Deliberately not session_stop with a nicer name. Stop closes stdin and kills the child;
        this sends the CLI's own interrupt control request, so the conversation, its context and
        its permission grants all survive. Binding Escape to Stop would have been worse than
        leaving Escape unbound, which is why this had to exist before the key could.
        """
        from .session import SessionError

        body = await request.json()
        try:
            state = await session_host().interrupt(name=str(body.get("name", "")),
                                                   cancel_queued=body.get("cancel_queued", True) is not False)
        except SessionError as exc:
            return err(exc)
        # "pending": sent, not yet answered. The page says so and waits for the event stream to
        # report the answer, rather than calling it a failure.
        return web.json_response({"ok": True, "state": state})

    async def session_permission_mode(request: web.Request) -> web.Response:
        """Change how this session asks before it acts, without restarting it.

        Allowed over the tunnel for every mode including bypassPermissions (decided 2026-09-28).
        He needs it from the phone, and the remote environment is one already trusted for client
        work - the same reasoning as feature 54. The mode is validated in the session before it
        reaches the CLI, so a typo comes back as a message rather than a session that will not
        start.
        """
        from .session import SessionError

        body = await request.json()
        mode, live = str(body.get("mode", "")), True
        try:
            await session_host().set_permission_mode(name=str(body.get("name", "")), mode=mode)
        except SessionError as exc:
            # A stopped session is not an error here: setting the mode before starting one is the
            # ordinary way to use this, and refusing would mean the only way to leave "manual" was
            # to start a session in it first. An unknown mode still fails, because that is a typo.
            if "no session is running" not in str(exc):
                return err(exc)
            live = False
        # Remember it, so the next session starts the way this one ended rather than reverting to
        # the built-in default. Persisting is best-effort: the live change already worked, and
        # failing the request would say otherwise.
        if proxy is not None and body.get("remember", True) is not False:
            try:
                proxy.cfg.update_settings({"session.permission_mode": mode})
            except ValueError as exc:
                return err(exc)                      # an unknown mode, and nothing was applied
            except OSError as exc:
                log.warning("permission mode changed but not saved: %s", exc)
        return web.json_response({"ok": True, "mode": mode, "live": live})

    async def session_permission_request(request: web.Request) -> web.Response:
        """Claude Code, through the approval host, asking whether a tool may run. The request waits
        here until someone answers on the page or the wait runs out."""
        from .session import SessionError

        # The token says which session is asking. With several running it is the routing as well as
        # the proof: a request can only ever be parked in the transcript that raised it.
        token = request.headers.get("X-Aicleaner-Approve", "")
        try:
            session = session_host().session_for_token(token)
        except SessionError:
            return web.json_response({"allow": False, "message": "aicleaner: not this session's approval host"},
                                     status=403)
        body = await request.json()
        request_id = f"{session.profile.name if session.profile else 'ask'}-{session._sequence + 1}"
        allowed = await session.ask_permission(request_id, str(body.get("tool", "a tool")),
                                               body.get("input") or {})
        # A question comes back answered (D58): the choices ride along with the allow.
        if isinstance(allowed, dict):
            return web.json_response({"allow": bool(allowed.get("allow")), "answers": allowed.get("answers")})
        return web.json_response({"allow": bool(allowed)})

    async def session_permission(request: web.Request) -> web.Response:
        from .session import SessionError

        body = await request.json()
        try:
            # The page says which session it is answering in, so an answer to a question that
            # has already closed is kept in that transcript rather than refused (2026-10-01).
            outcome = session_host().answer_permission(str(body.get("id", "")), bool(body.get("allow")),
                                                       body.get("answers"), name=str(body.get("name", "")))
        except (SessionError, ValueError) as exc:
            return err(exc)
        return web.json_response({"ok": True, "late": outcome == "late"})

    async def session_events(request: web.Request) -> web.Response:
        """Everything the session says, as it says it."""
        from .session import SessionError

        name = request.query.get("name", "")
        try:
            session = session_host()._session(name)
        except SessionError as exc:
            return err(exc)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                               "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        queue = session.watch()
        try:
            for event in session.state()["events"]:
                await response.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    await response.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    await response.write(b": keep-alive\n\n")  # through proxies and tunnels
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            session.unwatch(queue)
        return response

    async def resolve(request: web.Request) -> web.Response:
        body = await request.json()
        value = str(body.get("value", "")).strip()
        action = body.get("action")
        try:
            if action == "map":
                cid = str(body["client"]).strip()
                if cid == GLOBAL_ID:
                    mf.ensure_global()
                elif cid not in mf.load_tolerant(record=False)[0].sources:
                    mf.add_client(cid, body.get("index"))
                real = body.get("real") or value
                # Look before resolving: `resolve` discards the candidate, and the candidate is
                # where the evidence is - how often it was seen, in which projects and sessions,
                # alongside whose names (feature 30).
                found = store.find(value)
                tok = mf.add_form(cid, real, body.get("token"), source=SOURCE_REVIEW)
                _record_provenance(mf, cid, real, SOURCE_REVIEW, found,
                                   note=f"accepted in Review as {cid}")
                store.resolve(value, "mapped")
                return web.json_response({"ok": True, "token": tok})
            if action in ("ignore", "dismiss"):
                store.resolve(value, action)
                return web.json_response({"ok": True})
            return err(ValueError("action must be map, ignore or dismiss"))
        except (MappingError, KeyError, ValueError) as exc:
            # "x is in both A and B" usually means x is public (a built-in role id, a vendor host):
            # offer that instead of making the user choose which client owns it.
            suggest = "public" if "is in both" in str(exc) else None
            return err(exc, suggest=suggest)

    async def restore_candidates(request: web.Request) -> web.Response:
        """Review's undo (D45): put candidates back as they were, after undoing what was done.

        Body: `candidates` - the records exactly as /api/candidates listed them before the action;
        at most one of `unignore` (the scope they were ignored in) or `unmap` (the client they were
        mapped to). An entry another window has already taken out needs no inverse, so it is not a
        refusal: the candidate coming back is what was asked for.
        """
        body = await request.json()
        records = body.get("candidates")
        unignore = str(body.get("unignore") or "").strip()
        unmap = str(body.get("unmap") or "").strip()
        if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
            return err(ValueError("candidates must be a list of the records /api/candidates listed"))
        if unignore and unmap:
            return err(ValueError("a candidate is undone from one action: unignore or unmap, not both"))
        mapped: set[str] = set()
        if unmap:
            try:
                mapped = {str(f.get("real", "")) for f in mf.export_client(unmap).get("forms", [])}
            except (MappingError, KeyError, ValueError):
                mapped = set()           # the client itself is gone: nothing of it to take out
        done: list[str] = []
        refused: list[dict[str, str]] = []
        for record in records:
            value = str(record.get("value") or "").strip()
            if not value:
                refused.append({"value": "", "why": "a record without a value cannot be put back"})
                continue
            try:
                if unignore:
                    mf.remove_ignore(value, unignore)
                if unmap and value in mapped:
                    mf.delete_form(unmap, value)
                # The kind's count goes back too, or an undone mis-click still counts towards
                # quieting the kind (feature 31).
                store.restore({**record, "value": value},
                              undoing="ignored" if unignore else "kept" if unmap else "")
                done.append(value)
            except (MappingError, KeyError, ValueError, TypeError) as exc:
                refused.append({"value": value, "why": str(exc)})
        return web.json_response({"ok": not refused, "done": done, "refused": refused})

    async def snooze_kind_offer(request: web.Request) -> web.Response:
        """"Not now" on a kind offer: hidden until the kind is ignored 50 more times (DECISIONS 42).
        `undo` wakes it again."""
        body = await request.json()
        kind = str(body.get("kind", ""))
        result = store.snooze_kind(kind, undo=bool(body.get("undo")))
        if result is False:
            return err(ValueError(f"{kind or 'that kind'} is not being offered, so there is nothing to snooze"))
        return web.json_response({"ok": True, "kind": kind,
                                  **({"back_after": result} if result is not True else {})})

    async def treat_as_public(request: web.Request) -> web.Response:
        """An identifier that is the same in every tenant: ignore it everywhere and remove the form
        from whichever clients hold it (Global is per user, so it is not promoted there)."""
        value = str((await request.json()).get("value", "")).strip()
        if not value:
            return err(ValueError("value is required"))
        removed: list[str] = []
        try:
            loaded, _reports = mf.load_tolerant(record=False)
            for client in loaded.data["clients"]:
                cid = str(client.get("id", ""))
                if any(str(f.get("real", "")).lower() == value.lower() for f in client.get("forms", []) or []):
                    mf.delete_form(cid, value)
                    removed.append(cid)
            mf.add_ignore(value, GLOBAL_ID)
            store.add_ignore(value)
        except (MappingError, KeyError, ValueError) as exc:
            return err(exc)
        return web.json_response({"ok": True, "removed_from": removed})

    async def mapping(request: web.Request) -> web.Response:
        loaded, _reports = mf.load_tolerant(record=False)
        for c in loaded.data["clients"]:
            c["_source"] = loaded.sources.get(c["id"], Path("?")).name
        ids = [c["id"] for c in loaded.data["clients"]]
        return web.json_response({**loaded.data, "layout": "clients/" if mf.client_files() else "client-map.json",
                                  "next_index": mf.next_index(), "archived": mf.archived(),
                                  "targets": ([GLOBAL_ID] if GLOBAL_ID not in ids else []) + ids})

    async def renumber_client(request: web.Request) -> web.Response:
        """A client's index changed (DECISIONS 67), before it is shared. Local only."""
        if request.get(REMOTE):
            return err(ValueError("renumbering works only on this machine"), status=403)
        body = await request.json()
        try:
            await asyncio.to_thread(mf.renumber, request.match_info["cid"], int(body.get("index", 0)))
        except (MappingError, ValueError) as problem:
            return err(problem)
        return web.json_response({"ok": True})

    async def wiki_page(request: web.Request) -> web.Response:
        """A page of the wiki (docs/wiki/<page>.md) from this install's own copy (2026-10-07). The
        name is held to letters, digits and hyphens, so nothing outside that folder can be named."""
        page = request.match_info["page"]
        path = Path(__file__).resolve().parent.parent / "docs" / "wiki" / f"{page}.md"
        if not WIKI_PAGE.fullmatch(page) or not path.is_file():
            return web.json_response({"error": f"there is no wiki page called {page}"}, status=404)
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return web.json_response({"page": page, "text": text})

    async def others_additions(request: web.Request) -> web.Response:
        """Forms someone else added to a shared client, not yet kept on this PC (DECISIONS 67)."""
        return web.json_response({"me": mf.author,
                                  "additions": await asyncio.to_thread(mf.others_additions)})

    async def answer_addition(request: web.Request) -> web.Response:
        """Keep another person's form (remembered here) or remove it from the shared file."""
        body = await request.json()
        cid, real, action = str(body.get("client", "")), str(body.get("real", "")), body.get("action")
        try:
            if action == "keep":
                await asyncio.to_thread(mf.keep_addition, cid, real)
            elif action == "drop":
                await asyncio.to_thread(mf.delete_form, cid, real)
            else:
                raise ValueError("action is keep or drop")
        except (MappingError, ValueError) as problem:
            return err(problem)
        return web.json_response({"ok": True})

    async def join_client(request: web.Request) -> web.Response:
        """A client a colleague shared, from its synced folder (DECISIONS 67). The folder is held to
        the allowed folders like any other a page names. Local only: it changes what this PC loads."""
        from .folders import FolderError, check_usable

        if request.get(REMOTE):
            return err(ValueError("adding a shared client works only on this machine"), status=403)
        body = await request.json()
        folder = str(body.get("folder", "")).strip()
        try:
            check_usable(folder, folder_roots(), "shared client folder")
            cid = await asyncio.to_thread(mf.join_shared, folder)
        except FolderError as problem:
            return err(problem, status=403)
        except MappingError as problem:
            return err(problem)
        return web.json_response({"ok": True, "client": cid})

    async def add_client(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mf.add_client(str(body["id"]).strip(), body.get("index"), bool(body.get("expand_case", True)), body.get("label"))
            return web.json_response({"ok": True})
        except (MappingError, KeyError, ValueError) as exc:
            return err(exc)

    async def add_form(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            cid, real = str(body["client"]), str(body["real"])
            tok = mf.add_form(cid, real, body.get("token"), source=SOURCE_MANUAL)
            _record_provenance(mf, cid, real, SOURCE_MANUAL, note="typed in on the Clients page")
            return web.json_response({"ok": True, "token": tok})
        except (MappingError, KeyError, ValueError) as exc:
            return err(exc)

    async def move_form(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            tok = mf.move_form(str(body["real"]), str(body["client"]), str(body["to"]))
            return web.json_response({"ok": True, "token": tok})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def form_to_ignore(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mf.form_to_ignore(str(body["client"]), str(body["real"]), str(body.get("scope", "global")))
            store.add_ignore(str(body["real"]))   # and out of Review, if it is there
            return web.json_response({"ok": True})
        except (MappingError, KeyError, ValueError) as exc:
            return err(exc)

    async def move_ignore(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mf.move_ignore(str(body["value"]), str(body.get("scope", "global")), str(body["to"]))
            return web.json_response({"ok": True})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def archive_client(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            target = mf.archive_client(str(body["id"]).strip())
            return web.json_response({"ok": True, "archived_to": str(target)})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def delete_form(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mf.delete_form(str(body["client"]), str(body["real"]))
            return web.json_response({"ok": True})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def delete_forms(request: web.Request) -> web.Response:
        """Many entries, one write. The page sends a selection, not a row at a time."""
        body = await request.json()
        try:
            reals = [str(r) for r in body["reals"]]
            done, refused = mf.delete_forms(str(body["client"]), reals)
            return web.json_response({"ok": True, "done": done,
                                      "refused": [{"real": r, "why": w} for r, w in refused]})
        except (MappingError, KeyError, TypeError) as exc:
            return err(exc)

    async def restore_forms(request: web.Request) -> web.Response:
        """Put a bulk action back (D42).

        One endpoint rather than an inverse per action, because every inverse is some of the same
        three steps: take the values off an ignore list, take them out of Global, and put the
        entries back with the tokens they had. The page says which apply; it knows, because it
        took the snapshot before it acted.
        """
        body = await request.json()
        try:
            cid = str(body["client"])
            entries = [{"real": str(e.get("real", "")), "token": str(e.get("token", ""))}
                       for e in body.get("entries", [])]
            unignore = str(body.get("unignore") or "")
            if unignore:
                mf.remove_ignores([e["real"] for e in entries], unignore)
            if body.get("from_global"):
                for entry in entries:
                    with contextlib.suppress(MappingError):
                        mf.delete_form(GLOBAL_ID, entry["real"])
            # Undoing a copy-to-Global is the one case with nothing to put back: the client's own
            # entry never left, only the Global copy was added. Restoring would report every one
            # as "already back", which is true and useless.
            restore = entries if body.get("restore", True) else []
            done, refused = mf.restore_forms(cid, restore) if restore else (
                [e["real"] for e in entries] if body.get("from_global") else [], [])
            return web.json_response({"ok": True, "done": done,
                                      "refused": [{"real": r, "why": w} for r, w in refused]})
        except (MappingError, KeyError, TypeError, ValueError) as exc:
            return err(exc)

    async def forms_to_ignore(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            reals = [str(r) for r in body["reals"]]
            done, refused = mf.forms_to_ignore(str(body["client"]), reals,
                                               str(body.get("scope", "global")))
            for real in done:
                store.add_ignore(real)      # and out of Review, as the single-form route does
            return web.json_response({"ok": True, "done": done,
                                      "refused": [{"real": r, "why": w} for r, w in refused]})
        except (MappingError, KeyError, TypeError, ValueError) as exc:
            return err(exc)

    async def ignore_list(request: web.Request) -> web.Response:
        # The proxy-local list (store) and the shared files (mf) are shown together.
        scoped = mf.ignores()
        scoped["global"] = sorted(set(scoped["global"]) | set(store.ignore_entries()))
        return web.json_response({"ignore": scoped["global"], "scopes": scoped})

    async def ignore_add(request: web.Request) -> web.Response:
        body = await request.json()
        value = str(body.get("value", "")).strip()
        scope = str(body.get("scope", "global")).strip() or "global"
        try:
            mf.add_ignore(value, scope)
            store.add_ignore(value)  # drop from the queue now; the file reload covers the future
            return web.json_response({"ok": True})
        except (MappingError, ValueError) as exc:
            return err(exc)

    async def ignore_delete(request: web.Request) -> web.Response:
        body = await request.json()
        value = str(body.get("value", "")).strip()
        scope = str(body.get("scope", "global")).strip() or "global"
        try:
            removed = mf.remove_ignore(value, scope)
            # The queue's own suppression is dropped whatever the scope, because ignore_add sets it
            # whatever the scope. Cleaning it only for "global" meant un-ignoring a string a client
            # had ignored left it suppressed from Review for good: off that client's list, still
            # never proposed, and showing under "global" in the listing because that is where the
            # queue's entries are merged. The client's own ignore still applies to that client.
            removed = store.remove_ignore(value) or removed
            return web.json_response({"ok": removed})
        except MappingError as exc:
            return err(exc)

    def in_use_folders() -> list[str]:
        """Folders already configured before this list existed: a profile's project folders and a
        client's share.

        They stay reachable, because turning the setting on must not silently stop a session that
        works today - and they grant nothing new, since a session could already read and write
        them. They are NOT written into folders.roots: the Settings card shows them separately as
        in use but unlisted, so the list can be corrected deliberately rather than inherited from a
        guess this code made.
        """
        found: list[str] = []
        if proxy is None:
            return found
        try:
            entries, _problem = proxy.profile_store.load()
            for entry in entries:
                if isinstance(entry, dict):
                    found.extend(str(p) for p in (entry.get("projects") or []))
        except Exception:                # never let housekeeping stop a request being answered
            pass
        try:
            found.extend(str(p) for p in proxy.share_store.load().values())
        except Exception:
            pass
        return [f.strip() for f in found if str(f).strip()]

    def folder_roots() -> list:
        """The allowed roots, resolved. Read fresh each time so changing the list takes effect
        without a restart - the whole point of it being a setting rather than a constant."""
        from .folders import blocked_roots, roots_of

        configured = list(proxy.cfg.folder_roots) if proxy is not None else []
        return roots_of(configured + in_use_folders())

    async def folders_list(request: web.Request) -> web.Response:
        """What a picker may show at ?path=, which is never more than the roots allow.

        An ancestor of a root lists only the children that lead to one, so a picker can open at the
        drive without that being a disclosure: nothing on the way down mentions what else is there.
        """
        from .folders import FolderError, listing

        try:
            return web.json_response(listing(request.query.get("path", ""), folder_roots()))
        except FolderError as problem:
            return err(problem)

    async def folders_new(request: web.Request) -> web.Response:
        """A new folder inside an allowed one, from the picker (2026-09-30). Checked like any folder."""
        from .folders import FolderError, make_folder

        body = await request.json()
        try:
            made = make_folder(str(body.get("parent", "")), str(body.get("name", "")), folder_roots())
        except FolderError as problem:
            return err(problem)
        return web.json_response({"ok": True, "path": str(made)})

    async def models(request: web.Request) -> web.Response:
        """Model names this machine could use (feature 45).

        Offered, never enforced: the field stays free text, because a list that cannot be
        fetched must not become a box you cannot type in.
        """
        answer = await asyncio.to_thread(model_list.get, request.query.get("refresh") == "1")
        return web.json_response(answer)

    async def folders_roots(request: web.Request) -> web.Response:
        """The configured roots, and the system folders none of them may be."""
        from .folders import blocked_roots
        configured = list(proxy.cfg.folder_roots) if proxy is not None else []
        known = {str(c).strip().lower() for c in configured}
        # Reachable because they were already in use, not because anybody listed them. Shown so the
        # list can be corrected deliberately rather than quietly inherited.
        in_use = sorted({f for f in in_use_folders() if f.strip().lower() not in known})
        return web.json_response({
            "roots": configured,
            "in_use": in_use,
            # Settable from either side since 2026-09-27: the case is realising at a client site
            # that a folder holding client content was never added. The de-risking is the system
            # folder refusal in `folders.blocked_reason`, not a locked control.
            "editable": True,
            "blocked": [str(p) for p in blocked_roots()],
        })

    async def export_public(request: web.Request) -> web.Response:
        """The list shipped with the install: identifiers that are the same in every tenant.

        It is not a client's list and cannot be edited from here - it is rebuilt by
        tools/build_public_identifiers.py and shipped - but it decides what is never queued and
        never tokenised, so it should be readable by whoever is wondering why something was
        skipped.
        """
        from .detect import PUBLIC_PATH

        try:
            text = PUBLIC_PATH.read_text(encoding="utf-8")
        except OSError as problem:
            return err(problem, status=404)
        return web.Response(body=text.encode("utf-8"), content_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="public-identifiers.json"'})

    async def public_summary(request: web.Request) -> web.Response:
        """What the shipped list holds, for the Settings card - without sending the whole thing."""
        from .detect import public_identifiers, PUBLIC_PATH
        import json as _json

        guids, domains = public_identifiers()
        generated, sources = "", []
        try:
            doc = _json.loads(PUBLIC_PATH.read_text(encoding="utf-8"))
            generated, sources = str(doc.get("generated", "")), [str(x) for x in doc.get("sources", [])]
        except (OSError, ValueError):
            pass
        return web.json_response({"guids": len(guids), "domains": len(domains),
                                  "generated": generated, "sources": sources,
                                  # What the list has actually taken out since the proxy started.
                                  # The counterweight to a list that has three times nearly eaten
                                  # real client data, silently, because a suppressed value simply
                                  # never appears anywhere (D13).
                                  "suppressed": store.suppressed()[:50]})

    async def form_from_global(request: web.Request) -> web.Response:
        """Global gives an identifier back to one client: {real, to}.

        Either the client already has it - in which case its row is locked only because Global has
        it, so dropping Global's copy unlocks it and the client keeps its token - or it does not,
        and this is an ordinary move out of Global with a new token. The store decides which.
        """
        body = await request.json()
        try:
            token = mf.take_from_global(str(body["real"]), str(body["to"]))
            return web.json_response({"ok": True, "token": token, "client": str(body["to"])})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def ignore_tokenise(request: web.Request) -> web.Response:
        """Move a string off an ignore list and onto the token list in the same scope.

        The reverse (token -> ignore) already existed a row at a time; this direction did not, so a
        string ignored by mistake had to be un-ignored and then typed back in by hand. One call
        rather than two from the page: the form is added first, because a declared entry is
        tokenised whatever any ignore list says - so if the second half failed the string would be
        protected but listed oddly, which is the safe way round to fail.
        """
        body = await request.json()
        value = str(body.get("value", "")).strip()
        scope = str(body.get("scope", "global")).strip() or "global"
        if not value:
            return err(ValueError("name the string to tokenise"))
        try:
            token = mf.add_form(scope, value)
            mf.remove_ignore(value, scope)
            store.remove_ignore(value)      # whatever the scope: see ignore_delete
            return web.json_response({"ok": True, "value": value, "token": token, "client": scope})
        except MappingError as exc:
            return err(exc)

    async def secrets_list(request: web.Request) -> web.Response:
        if user_secrets is None:
            return web.json_response({"entries": [], "enabled": False})
        user_secrets.maybe_reload()
        return web.json_response({"entries": user_secrets.entries(), "enabled": True, "path": str(user_secrets.path)})

    async def secrets_add(request: web.Request) -> web.Response:
        if user_secrets is None:
            return err(ValueError("secrets redaction is off in aicleaner-config.json"))
        body = await request.json()
        try:
            user_secrets.add(str(body.get("label", "")), str(body.get("value", "")))
            return web.json_response({"ok": True})
        except (ValueError, OSError) as exc:
            return err(exc)

    async def secrets_mode(request: web.Request) -> web.Response:
        if proxy is None:
            return err(ValueError("mode switch unavailable"))
        if request.method == "GET":
            return web.json_response({"mode": proxy.cfg.secrets_mode})
        body = await request.json()
        try:
            proxy.set_secrets_mode(str(body.get("mode", "")))
            return web.json_response({"ok": True, "mode": proxy.cfg.secrets_mode})
        except (ValueError, OSError) as exc:
            return err(exc)

    async def scan_state(request: web.Request) -> web.Response:
        """How the one scan there is went, or that there has not been one."""
        if proxy is None:
            return err(ValueError("scanning is unavailable"))
        return web.json_response(proxy.scans.state())

    async def scan_start(request: web.Request) -> web.Response:
        """Run the scan the command line would run, from the page that knows the client.

        Startable from anywhere, including through the tunnel: it always reads the machine the
        proxy runs on, so a folder named from somewhere else is not ambiguous. What bounds it is
        folders.roots, checked here - the picker only offers allowed folders, but a convenience
        cannot be a control, and a typed path or a direct POST arrives at this same line.
        """
        from .folders import FolderError, FolderOutside, check_usable
        from .scanjob import ScanError

        if proxy is None:
            return err(ValueError("scanning is unavailable"))
        body = await request.json()
        try:
            folder = check_usable(str(body.get("folder", "")), folder_roots(), "folder to scan")
        except FolderOutside as problem:
            return err(problem, status=403)      # a decision to make in Settings
        except FolderError as problem:
            return err(problem)                  # a typo: 400, the same as it always was
        try:
            started = proxy.scans.start(kind=str(body.get("kind", "scan")),
                                        folder=str(folder),
                                        client=str(body.get("client", "")),
                                        mode=str(body.get("mode", "report")),
                                        known=set(proxy.client_labels()))
        except ScanError as problem:
            return err(problem)
        return web.json_response(started)

    async def config_get(request: web.Request) -> web.Response:
        if proxy is None:
            return err(ValueError("config unavailable"))
        from .detect import SELECTABLE_KINDS
        # Everything the Settings page shows comes from here: /healthz is not a UI route, so
        # through the tunnel it is refused, and a second fetch of it failed the whole page.
        return web.json_response({"settings": proxy.cfg.settings(), "kinds": SELECTABLE_KINDS,
                                  "dialects": sorted(proxy.cfg.upstreams),
                                  # Read-only, and shown whether it is empty or not: an entry here
                                  # turns off every check this proxy performs, for that endpoint,
                                  # so it should never be something you have to go looking for.
                                  "allow_non_json_paths": list(proxy.cfg.allow_non_json_paths),
                                  "remote": bool(request.get(REMOTE)), "remote_settings": list(REMOTE_SETTINGS),
                                  "windows": bashguard.applies(),
                                  "config_path": str(proxy.cfg.path or ""), "contact_file": contact.get().source, "clients": [c["id"] for c in mf.load_tolerant(record=False)[0].data["clients"]]})

    async def config_post(request: web.Request) -> web.Response:
        if proxy is None:
            return err(ValueError("config unavailable"))
        body = await request.json()
        changes = dict(body.get("settings", {}))
        if request.get(REMOTE):
            local_only = sorted(k for k in changes if k not in REMOTE_SETTINGS)
            if local_only:
                return err(ValueError(f"local only: {', '.join(local_only)} can only be changed on the "
                                      "machine aicleaner runs on"), status=403)
        was_binaries = proxy.cfg.allow_images
        # Turning timings on starts its 24 hours now (feature 91); the page never sets the clock.
        changes.pop("debug.timings_since", None)
        if changes.get("debug.timings") is True:
            changes["debug.timings_since"] = int(time.time())
        if changes.get("remote.encrypt") is True and not proxy.tunnel.is_set():
            # On with no secret would lock the tunnel with no way in (feature 21).
            return err(ValueError("generate a tunnel code first, then turn encryption on"))
        try:
            out = proxy.cfg.update_settings(changes)
            if proxy.store is not None:
                proxy.store.quiet_kinds = frozenset(proxy.cfg.quiet_kinds)
            proxy.notifier.footer_enabled = proxy.cfg.notify_footer
            proxy.notifier.toast_enabled = proxy.cfg.notify_toast
            await proxy.apply_session_enabled()
            # One control (feature 36). The setting governs the pipe and the CLAUDE.md rule
            # governs the agent, and only the rule is in front of the model - so leaving them to
            # disagree meant the setting appeared to do nothing. Flipping it rewrites the block.
            #
            # Never on a remote request: this writes a file in the user's home folder, and the
            # remote side is a browser on a phone. And never as part of the failure path - the
            # setting is saved either way, and a rules write that fails is reported rather than
            # rolled back, because the two are separate facts about the machine.
            rules_said = ""
            if "attachments.allow_images" in changes and proxy.cfg.allow_images != was_binaries \
                    and not request.get(REMOTE):
                try:
                    rules_said = rules.apply(proxy.cfg.allow_images)
                except rules.RulesError as exc:
                    rules_said = f"the setting was saved, but {exc}"
                    log.warning("rules block not updated: %s", exc)
            # The Bash guard (feature 98) is a hook in Claude Code's settings, kept in step with its
            # switch here - on this machine only, for the same reason as the rules block.
            if "claude.bash_guard" in changes and not request.get(REMOTE):
                try:
                    said = bashguard.apply(proxy.cfg.claude_bash_guard)
                except bashguard.GuardError as exc:
                    said = f"the setting was saved, but {exc}"
                    log.warning("bash guard not updated: %s", exc)
                rules_said = " ".join(s for s in (rules_said, said) if s)
            return web.json_response({"ok": True, "settings": out, "rules": rules_said})
        except (ValueError, OSError) as exc:
            return err(exc)

    async def restart_proxy(request: web.Request) -> web.Response:
        if proxy is None or proxy.cfg.path is None:
            return err(ValueError("this proxy was not started from an installed config; restart it by hand"))
        try:
            log_path = restart(proxy.cfg.path.parent, proxy.cfg.port, Path(proxy.cfg.log_dir))
        except RestartError as exc:
            return err(exc, status=409)
        return web.json_response({"ok": True, "log": str(log_path)})

    # ---- the setup wizard (feature 106) - local only, see guard.REMOTE_REFUSED_PREFIXES ----------

    async def setup_dns(request: web.Request) -> web.Response:
        """What a domain's DNS looks like now, for the remote-access step. Read-only."""
        from .dnsguide import look_up
        report = await asyncio.to_thread(look_up, request.query.get("domain", ""))
        return web.json_response(report.as_dict())

    async def setup_enable(request: web.Request) -> web.Response:
        """Route the AI tools through aicleaner: the same Enable-AiCleaner.ps1 a person would run,
        readiness checks and all. Run without blocking the server - the script asks this proxy for
        /healthz and sends a request through it while it runs."""
        if proxy is None or proxy.cfg.path is None:
            return err(ValueError("this proxy was not started from an installed config"))
        root = proxy.cfg.path.parent
        script = root / "installer" / "Enable-AiCleaner.ps1"
        if not script.exists():
            script = root / "Enable-AiCleaner.ps1"                    # an install from before the move
        from .restart import powershell
        child = await asyncio.create_subprocess_exec(
            powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-LogPath", "",
            cwd=str(root), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(child.communicate(), timeout=180)
        except asyncio.TimeoutError:
            child.kill()
            return err(ValueError("Enable-AiCleaner.ps1 did not finish within three minutes"), status=504)
        text = out.decode("utf-8", errors="replace")
        return web.json_response({"ok": child.returncode == 0, "output": text[-6000:]})

    async def setup_remote(request: web.Request) -> web.Response:
        """Remote access from the wizard: Enable-RemoteControl.ps1's API route, unattended. Detached,
        because it restarts this proxy at the end; the page reads its log and waits for it back."""
        if proxy is None or proxy.cfg.path is None:
            return err(ValueError("this proxy was not started from an installed config"))
        body = await request.json()
        hostname = str(body.get("hostname") or "").strip().lower()
        email = str(body.get("email") or "").strip()
        token = str(body.get("token") or "").strip()
        from .dnsguide import DOMAIN_RE
        if not DOMAIN_RE.match(hostname) or hostname.count(".") < 2:
            return err(ValueError("give the full address for aicleaner, for example ai.example.com"))
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return err(ValueError("give the email address that may sign in"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{30,}", token):
            return err(ValueError("that does not look like a Cloudflare API token"))
        identity = str(body.get("identity") or "OneTimePin")
        if identity not in REMOTE_IDENTITIES:
            return err(ValueError(f"sign-in method must be one of: {', '.join(REMOTE_IDENTITIES)}"))
        root = proxy.cfg.path.parent
        script = root / "installer" / "Enable-RemoteControl.ps1"
        from .restart import RestartError, powershell, spawn_detached
        log_path = Path(proxy.cfg.log_dir) / "remote-setup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")                    # this run's log only
        try:
            spawn_detached(remote_setup_command(powershell(), script, hostname, email, identity), root, log_path,
                           env={"AICLEANER_CLOUDFLARE_TOKEN": token})
        except RestartError as exc:
            return err(exc, status=409)
        return web.json_response({"ok": True})

    async def setup_remote_log(request: web.Request) -> web.Response:
        """What the remote-access run has said so far."""
        if proxy is None:
            return err(ValueError("unavailable"))
        path = Path(proxy.cfg.log_dir) / "remote-setup.log"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        failed = "FullyQualifiedErrorId" in text or "CategoryInfo" in text
        return web.json_response({"text": text[-8000:], "ok": "Remote control is on:" in text, "failed": failed,
                                  "done": "Remote control is on:" in text or failed})

    async def export_client(request: web.Request) -> web.Response:
        cid = request.match_info["cid"]
        try:
            doc = mf.export_client(cid)
        except MappingError as exc:
            return err(exc, 404 if str(exc).startswith("unknown client") else 400)
        return web.Response(text=json.dumps(doc, indent=2, ensure_ascii=False), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{cid}.json"'})

    async def export_all(request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(mf.export_all(), indent=2, ensure_ascii=False), content_type="application/json",
                            headers={"Content-Disposition": 'attachment; filename="aicleaner-clients.json"'})

    async def import_clients(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            doc = body.get("document")
            if isinstance(doc, str):
                doc = json.loads(doc)
            # mode: new | merge (default) | replace (DECISIONS 44). The old `replace` flag still works.
            mode = str(body.get("mode") or ("replace" if body.get("replace") else "merge"))
            done = mf.import_bundle(doc, mode=mode)
            return web.json_response({"ok": True, "imported": done})
        except (MappingError, ValueError, KeyError, TypeError) as exc:
            return err(exc)

    async def secrets_delete(request: web.Request) -> web.Response:
        if user_secrets is None:
            return err(ValueError("secrets redaction is off in aicleaner-config.json"))
        body = await request.json()
        return web.json_response({"ok": user_secrets.remove(str(body.get("label", "")))})

    def last_request() -> dict[str, Any] | None:
        files = sorted(log_dir.glob(f"evidence-{host}-*.jsonl"))
        for f in reversed(files[-2:]):
            lines = [line for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                e = json.loads(lines[-1])
                return {"ts": e.get("ts"), "path": e.get("path"), "status": e.get("status"),
                        "outbound": sum(abs(int(v)) for v in (e.get("outbound") or {}).values()),
                        "inbound": sum(abs(int(v)) for v in (e.get("inbound") or {}).values()),
                        "note": e.get("note")}
        return None

    async def status(request: web.Request) -> web.Response:
        """One call for the status stack, banner, badges and client list (spec section 4)."""
        handle = getattr(proxy, "_handle", None) if proxy is not None else None

        def read_mapping() -> dict:
            """Off the loop: this stats every client file, and reloads if one moved.

            `signature()` globs the clients folder and stats each file in it, and that folder is
            normally a OneDrive share - a reparse point over network-backed storage, slow while
            the OS has nothing cached. Every open page asks for this every ten seconds, and it
            was running in the coroutine, so for its duration the single event loop served
            nobody: not the other requests, not the poll, and not the proxy, which shares it
            (D35). A reload is heavier still, though v0.9.30-32 made it far cheaper.
            """
            if handle is not None:
                handle.current()
                return handle.reports
            return mf.load_tolerant(record=False)[1]

        reports = await asyncio.to_thread(read_mapping)
        rows = [r.as_dict() for _cid, r in sorted(reports.items(), key=lambda kv: kv[0].lower())]
        items = await asyncio.to_thread(store.pending)
        pending = len(items)
        held = sum(1 for c in items if c.get("placeholder"))
        # Meant to be held back, and could not be: the value went out as it is. Counted here
        # because the number of things held back was already on the page and the number that
        # FAILED to be held back was nowhere (D12).
        unprotected = sum(1 for c in items if c.get("unprotected") and not c.get("placeholder"))
        states = [set(r["state"]) for r in rows]
        cfg = proxy.cfg if proxy is not None else None

        # Whether anything is actually pointed at us, not merely whether the mapping is sound
        # (D37). A healthy mapping with every client aimed elsewhere used to read "filtering: On"
        # while nothing was being filtered at all.
        from .routing import summary as routing_summary

        seen = getattr(proxy, "requests_seen", 0) if proxy is not None else 0
        # Every folder a session can run in, so a project whose own .claude settings route around
        # the proxy is reported (H10).
        projects = proxy.session_folders() if proxy is not None else []
        routing = await asyncio.to_thread(routing_summary, cfg.port if cfg else 0, seen, None, None, projects)

        # `filtering` stays what it has always meant - whether the mapping is sound - and routing
        # travels beside it rather than folded in. Two reasons: the value is a contract several
        # callers read, and the routing answer depends on files in the user's own home directory,
        # which a test server has no business deciding a headline from. The Overview combines the
        # two (see bannerContent), which is where the judgement belongs.
        if any("broken" in st for st in states):
            filtering = "stopped"
        elif pending or any(st & {"fallback", "conflict-copy"} for st in states):
            filtering = "attention"
        else:
            filtering = "on"
        from . import timing

        return web.json_response({
            "filtering": filtering, "pending": pending, "held_back": held, "sent_as_is": pending - held,
            "unprotected": unprotected,
            # Bypass forwards everything untouched (D148): the headline must say so, whatever the
            # mapping's state - "Filtering on" over a bypass is an assurance that is untrue.
            "bypass": bool(cfg.proxy_bypass) if cfg else False,
            # Diagnostics (feature 91): whether the page should time what it does, and until when.
            "debug_timings": timing.active(cfg) if cfg else False,
            "debug_timings_until": timing.until(cfg) if cfg else 0,
            "clients": [r for r in rows if r["id"] != GLOBAL_ID],
            "global": next((r for r in rows if r["id"] == GLOBAL_ID), None),
            "secrets_mode": cfg.secrets_mode if cfg else "redact",
            "listen": cfg.listen if cfg else "127.0.0.1", "port": cfg.port if cfg else 0,
            # The machine the proxy runs on. Through the tunnel the page is read from
            # somewhere else, so anything that says "this machine" has to name it instead.
            "host": host,
            "loopback": is_loopback_listen(cfg.listen) if cfg else True,
            # Read through the tunnel (the guard verified it), so the page can grey out what only
            # works on the machine itself - Secrets, for one (the user, 2026-10-01).
            "remote": bool(request.get(REMOTE)),
            # The MCP audit's latest answer (H11): which servers get real names, which wait.
            "mcp": proxy.mcp.summary(list(proxy.cfg.keep_tokenised_tools)) if proxy is not None else [],
            # What the health check last found (feature 17). Read from the file it writes rather
            # than re-run here: it shells out to PowerShell and reads the registry, which is not
            # something to do on a poll that fires every ten seconds.
            "health": _health_state(cfg),
            # Each client's folder is shared in OneDrive (DECISIONS 51): an amber note when the
            # clients location is not in it, since nothing there syncs to colleagues.
            "clients_in_onedrive": in_onedrive(Path(cfg.clients_dir)) if cfg else True,
            # Folders other people on this PC could write to (H18): warned about, never refused.
            "open_folders": _open_folders(cfg),
            "last_request": last_request(), "contact": contact.get().as_dict(),
            # What each client is pointed at, and when this proxy started - so the page can say
            # whether filtering is happening, and whether a Restart actually took (feature 60).
            "routing": routing,
            "started_at": getattr(proxy, "started_at", 0.0) if proxy is not None else 0.0,
            "requests_seen": seen,
            "activity": activity(),
        })

    def activity() -> dict:
        """What is going on that a page elsewhere may want to announce when it lands (feature 44):
        each session's finished-turn count, whether a turn is running, and how many questions wait;
        and the scan job. Counters only move forward, so the page compares one poll with the next."""
        host = getattr(proxy, "session", None) if proxy is not None else None
        sessions = [s.activity() for s in host.sessions.values()] if host is not None else []
        scans = getattr(proxy, "scans", None) if proxy is not None else None
        job = (scans.state().get("job") if scans is not None else None) or None
        scan = ({k: job.get(k) for k in ("id", "state", "client", "kind", "folder", "finished")}
                if job else None)
        opened = proxy.model_requests() if proxy is not None and hasattr(proxy, "model_requests") else []
        return {"sessions": sessions, "scan": scan, "model_requests": opened}

    async def client_view(request: web.Request) -> web.Response:
        try:
            return web.json_response(mf.client_view(request.match_info["cid"]))
        except MappingError as exc:
            return err(exc, 404 if str(exc).startswith("unknown client") else 400)

    async def client_raw(request: web.Request) -> web.Response:
        try:
            return web.json_response(mf.raw(request.match_info["cid"]))
        except MappingError as exc:
            return err(exc, 404 if str(exc).startswith("unknown client") else 400)

    async def client_restore(request: web.Request) -> web.Response:
        try:
            mf.restore(request.match_info["cid"])
            return web.json_response({"ok": True})
        except MappingError as exc:
            return err(exc)

    # ---- client keys and held changes (DECISIONS 51) ----------------------------------------

    async def client_key_get(request: web.Request) -> web.Response:
        """Whether this PC holds the client's key, and its id - never the key itself."""
        from .keys import key_id
        cid = request.match_info["cid"]
        key = mf._key(cid)
        return web.json_response({"has_key": key is not None, "key_id": key_id(key) if key else ""})

    async def client_key_post(request: web.Request) -> web.Response:
        """generate (shown once, to send to the engagement's people), enter (pasted), rotate.
        Local only: a key is the one thing that makes a change to a shared file trusted, and it is
        not created, shown or replaced through the tunnel."""
        from .keys import KeyError_, generate, key_id
        if request.get(REMOTE):
            return err(ValueError("local only: client keys can only be set on the machine aicleaner runs on"),
                       status=403)
        cid = request.match_info["cid"]
        body = await request.json()
        action = str(body.get("action", ""))
        try:
            if action == "generate":
                if mf._key(cid) is not None:
                    return err(ValueError(f"{cid} already has a key on this machine; rotate it instead"))
                return web.json_response({"ok": True, "key": mf.set_key(cid, generate())})
            if action == "enter":
                return web.json_response({"ok": True, "key_id": key_id(mf.set_key(cid, str(body.get("key", ""))))})
            if action == "rotate":
                return web.json_response({"ok": True, "key": mf.rotate_key(cid)})
        except (MappingError, KeyError_) as exc:
            return err(exc)
        return err(ValueError("action must be generate, enter or rotate"))

    async def client_held(request: web.Request) -> web.Response:
        """A held change, reviewed: accept (sign it, trust it) or reject (last-good back)."""
        cid = request.match_info["cid"]
        body = await request.json()
        try:
            (mf.accept_held if body.get("accept") else mf.reject_held)(cid)
            return web.json_response({"ok": True})
        except MappingError as exc:
            return err(exc)

    async def mcp_approve_server(request: web.Request) -> web.Response:
        """Real names for an MCP server reached over http(s) (H11, DECISIONS 52). Local only: who
        receives client names is decided on the machine aicleaner runs on."""
        if request.get(REMOTE):
            return err(ValueError("local only: MCP servers can only be approved on the machine aicleaner runs on"),
                       status=403)
        if proxy is None:
            return err(ValueError("no proxy"))
        pattern = str((await request.json()).get("pattern", ""))
        if not any(row["pattern"] == pattern and row["approvable"]
                   for row in proxy.mcp.summary(list(proxy.cfg.keep_tokenised_tools))):
            return err(ValueError(f"{pattern or 'that'} is not a server waiting for approval"))
        proxy.mcp.approve(pattern)
        return web.json_response({"ok": True})

    async def client_salvage(request: web.Request) -> web.Response:
        try:
            return web.json_response({"ok": True, **mf.salvage(request.match_info["cid"])})
        except MappingError as exc:
            return err(exc)

    async def copy_to_global(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            return web.json_response({"ok": True, "token": mf.copy_to_global(str(body["client"]), str(body["real"]))})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def set_label(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            mf.set_label(str(body["id"]), str(body.get("label") or ""))
            return web.json_response({"ok": True})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def set_email_rule(request: web.Request) -> web.Response:
        """One rule for every address at a client's domain, on or off (feature 96)."""
        body = await request.json()
        try:
            answer = await asyncio.to_thread(mf.set_email_rule, str(body["client"]), str(body["domain"]),
                                             bool(body.get("on", True)))
            return web.json_response({"ok": True, **answer})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def update_form(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            # "match" absent means the caller is not editing the rule; "match": null means take
            # it off. The two must not collapse, or an inline edit of the name would silently
            # drop a rule somebody had set (feature 23).
            match = body["match"] if "match" in body else _UNSET
            tok = mf.update_form(str(body["client"]), str(body["real"]),
                                 body.get("new_real"), body.get("new_token"), body.get("new_kind"),
                                 new_match=match)
            return web.json_response({"ok": True, "token": tok})
        except (MappingError, KeyError) as exc:
            return err(exc)

    # POST /api/mapping/client/primary-repo is gone (D99): the home folder comes from each
    # profile's own workspace files on this machine, so the client file names no repo.

    async def rederive_form(request: web.Request) -> web.Response:
        """Derive this entry's token again under today's rules (D6)."""
        body = await request.json()
        try:
            tok = mf.rederive_form(str(body["client"]), str(body["real"]))
            return web.json_response({"ok": True, "token": tok})
        except (MappingError, KeyError) as exc:
            return err(exc)

    async def evidence(request: web.Request) -> web.Response:
        try:
            n = max(1, min(int(request.query.get("n", "50")), 5000))
        except ValueError:
            return err(ValueError("n must be a whole number"))
        files = sorted(log_dir.glob(f"evidence-{host}-*.jsonl"))
        lines: list[str] = []
        for f in files[-2:]:
            lines.extend(f.read_text(encoding="utf-8").splitlines())
        return web.json_response({"lines": [json.loads(l) for l in lines[-n:] if l.strip()]})

    async def substitution_log(request: web.Request) -> web.Response:
        """The substitution log for one day, newest first, filtered (DECISIONS 38).

        Read here and shown on the Log page; never handed over as a file. Lines written before
        the event format are counted in `skipped_old`, not shown.
        """
        q = request.query
        day = q.get("day") or f"{datetime.now():%Y%m%d}"
        if not re.fullmatch(r"\d{8}", day):
            return err(ValueError("day must be YYYYMMDD"))
        try:
            n = max(1, min(int(q.get("n", "200")), 1000))
            start = max(0, int(q.get("before", "0") or 0))
        except ValueError:
            return err(ValueError("n and before must be whole numbers"))
        detail = log_dir / "detail"
        days = sorted((p.name[len(f"substitutions-{host}-"):][:8]
                       for p in detail.glob(f"substitutions-{host}-*.jsonl.gz")), reverse=True)
        path = detail / f"substitutions-{host}-{day}.jsonl.gz"

        def read() -> tuple[list[dict[str, Any]], int]:
            if not path.exists():
                return [], 0
            events, old = [], 0
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if "type" in event:
                        events.append(event)
                    else:
                        old += 1
            return events, old

        events, old = await asyncio.to_thread(read)
        events.reverse()
        action, client, session, text = (q.get("action", ""), q.get("client", "").lower(),
                                         q.get("session", ""), q.get("q", "").lower())

        def wanted(e: dict[str, Any]) -> bool:
            if action == "ui":
                if e.get("type") != "ui":
                    return False
            elif action and e.get("action") != action:
                return False
            if client and str(e.get("client", "")).lower() != client:
                return False
            if session and e.get("session", "") != session:
                return False
            return not text or text in json.dumps(e, ensure_ascii=False).lower()

        matched = [e for e in events if wanted(e)]
        page = matched[start:start + n]
        after = start + n if start + n < len(matched) else None
        return web.json_response({"day": day, "days": days, "events": page, "next": after,
                                  "total": len(matched), "skipped_old": old})

    async def shell(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB / "index.html")

    async def old_page(request: web.Request) -> web.Response:
        raise web.HTTPFound(REDIRECTS[request.path])

    app.router.add_get("/", shell)
    for old in REDIRECTS:
        app.router.add_get(old, old_page)
    app.router.add_static("/web/", WEB, follow_symlinks=False, show_index=False)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/clients/{cid}", client_view)
    app.router.add_get("/api/clients/{cid}/raw", client_raw)
    app.router.add_post("/api/clients/{cid}/restore", client_restore)
    app.router.add_get("/api/clients/{cid}/key", client_key_get)
    app.router.add_post("/api/clients/{cid}/key", client_key_post)
    app.router.add_post("/api/clients/{cid}/held", client_held)
    app.router.add_post("/api/mcp/approve", mcp_approve_server)
    app.router.add_post("/api/clients/{cid}/salvage", client_salvage)
    if proxy is not None:
        app.router.add_get("/api/clients/{cid}/share", client_share)
        app.router.add_get("/api/clients/{cid}/onedrive", client_onedrive)
        app.router.add_post("/api/clients/{cid}/share", client_share_save)
    app.router.add_post("/api/mapping/form/update", update_form)
    app.router.add_post("/api/mapping/form/rederive", rederive_form)
    app.router.add_post("/api/mapping/form/copy-to-global", copy_to_global)
    app.router.add_post("/api/mapping/form/from-global", form_from_global)
    app.router.add_post("/api/mapping/client/label", set_label)
    app.router.add_post("/api/mapping/client/email-rule", set_email_rule)
    # Registered whether or not the session host exists yet. Routes were once added only when it
    # already did, which froze the setting at whatever it was when the proxy started: aiohttp
    # freezes its router once the app runs, so apply_session_enabled could build the host at
    # runtime but nothing served its URLs, and every call fell through to the proxy's catch-all as
    # "unknown dialect 'api'". Switching the Session page on did nothing, with no error anywhere.
    # Each handler asks session_host(), which refuses with a reason when it is off, so registering
    # them always costs nothing and makes the switch mean what the card says it means.
    if proxy is not None:
        app.router.add_get("/api/session", session_state)
        app.router.add_get("/api/session/worktree", session_worktree_offer)
        app.router.add_post("/api/session/worktree", session_worktree_create)
        app.router.add_post("/api/session/worktree/merge", session_worktree_merge)
        app.router.add_post("/api/session/worktree/remove", session_worktree_remove)
        app.router.add_post("/api/session/start", session_start)
        app.router.add_post("/api/session/prompt", session_prompt)
        app.router.add_post("/api/session/stop", session_stop)
        app.router.add_post("/api/session/interrupt", session_interrupt)
        app.router.add_post("/api/session/queue/send", session_queue_send)
        app.router.add_post("/api/session/queue/cancel", session_queue_cancel)
        app.router.add_post("/api/session/permission-mode", session_permission_mode)
        app.router.add_post("/api/session/permission", session_permission)
        app.router.add_post("/api/session/permission/request", session_permission_request)
        app.router.add_get("/api/session/events", session_events)
        app.router.add_get("/api/session/files", session_files)
        app.router.add_get("/api/session/search", session_search)
        app.router.add_get("/api/editor/powershell/commands", editor_ps_commands)
        app.router.add_post("/api/debug/timings", debug_timings)
        app.router.add_get("/api/autocorrect/words", autocorrect_words)
        app.router.add_get("/api/autocorrect/clients", autocorrect_clients)
        app.router.add_get("/api/editor/powershell/parameters", editor_ps_parameters)
        app.router.add_get("/api/session/file", session_file)
        app.router.add_post("/api/session/file/write", session_file_write)
        app.router.add_get("/api/session/file/raw", session_file_raw)
        app.router.add_get("/api/session/file/download", session_file_download)
        app.router.add_post("/api/session/file/stat", session_file_stat)
        app.router.add_post("/api/session/files/zip", session_files_zip)
        app.router.add_get("/api/session/tree-state", session_tree_state)
        app.router.add_post("/api/session/workspace-cwd", session_workspace_cwd)
        app.router.add_post("/api/session/workspace-create", session_workspace_create)
        app.router.add_get("/api/session/workspace-files", session_workspace_files)
        app.router.add_post("/api/session/workspace-view", session_workspace_view)
        app.router.add_post("/api/session/tree-state", session_tree_state_save)
        app.router.add_get("/api/session/tabs-state", session_tabs_state)
        app.router.add_post("/api/session/tabs-state", session_tabs_state_save)
        app.router.add_get("/api/session/past", session_past)
        app.router.add_get("/api/session/earlier", session_earlier)
        app.router.add_get("/api/tunnel/params", tunnel_params)
        app.router.add_post("/api/tunnel/hello", tunnel_hello)
        app.router.add_post("/api/tunnel/confirm", tunnel_confirm)
        app.router.add_get("/api/tunnel/secret", tunnel_secret)
        app.router.add_post("/api/tunnel/secret", tunnel_secret_set)
        app.router.add_post("/api/session/profile/add-folder", session_profile_add_folder)
        app.router.add_get("/api/session/commands", session_commands)
        app.router.add_get("/api/session/profiles", session_profiles)
        app.router.add_post("/api/session/profiles", session_profiles_save)
    app.router.add_get("/api/candidates", candidates)
    app.router.add_post("/api/candidates/resolve", resolve)
    app.router.add_post("/api/candidates/restore", restore_candidates)
    app.router.add_post("/api/candidates/public", treat_as_public)
    app.router.add_post("/api/candidates/snooze", snooze_kind_offer)
    app.router.add_get("/api/mapping", mapping)
    app.router.add_post("/api/mapping/client", add_client)
    app.router.add_post("/api/clients/join", join_client)
    app.router.add_post("/api/clients/{cid}/index", renumber_client)
    app.router.add_get("/api/additions", others_additions)
    app.router.add_get("/api/wiki/{page}", wiki_page)
    app.router.add_post("/api/additions", answer_addition)
    app.router.add_post("/api/mapping/form", add_form)
    app.router.add_post("/api/mapping/form/delete", delete_form)
    app.router.add_post("/api/mapping/forms/delete", delete_forms)
    app.router.add_post("/api/mapping/forms/restore", restore_forms)
    app.router.add_post("/api/mapping/client/archive", archive_client)
    app.router.add_post("/api/mapping/form/move", move_form)
    app.router.add_post("/api/mapping/form/ignore", form_to_ignore)
    app.router.add_post("/api/mapping/forms/ignore", forms_to_ignore)
    app.router.add_post("/api/ignore/move", move_ignore)
    app.router.add_get("/api/evidence", evidence)
    app.router.add_get("/api/log", substitution_log)
    app.router.add_get("/api/scan", scan_state)
    app.router.add_post("/api/scan", scan_start)
    app.router.add_get("/api/config", config_get)
    app.router.add_post("/api/config", config_post)
    app.router.add_post("/api/restart", restart_proxy)
    app.router.add_get("/api/setup/dns", setup_dns)
    app.router.add_post("/api/setup/enable", setup_enable)
    app.router.add_post("/api/setup/remote", setup_remote)
    app.router.add_get("/api/setup/remote/log", setup_remote_log)
    app.router.add_get("/api/export/client/{cid}", export_client)
    app.router.add_get("/api/export/all", export_all)
    app.router.add_get("/api/export/public", export_public)
    app.router.add_get("/api/public", public_summary)
    app.router.add_get("/api/folders", folders_list)
    app.router.add_get("/api/folders/roots", folders_roots)
    app.router.add_post("/api/folders/new", folders_new)
    app.router.add_get("/api/models", models)
    app.router.add_post("/api/import", import_clients)
    app.router.add_get("/api/secrets", secrets_list)
    app.router.add_post("/api/secrets", secrets_add)
    app.router.add_post("/api/secrets/delete", secrets_delete)
    app.router.add_get("/api/secrets/mode", secrets_mode)
    app.router.add_post("/api/secrets/mode", secrets_mode)
    app.router.add_get("/api/ignore", ignore_list)
    app.router.add_post("/api/ignore", ignore_add)
    app.router.add_post("/api/ignore/delete", ignore_delete)
    app.router.add_post("/api/ignore/tokenise", ignore_tokenise)
