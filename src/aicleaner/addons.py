"""Add-ons: features that live outside AI Cleaner, loaded into its process through one narrow, documented door (f185).

An add-on is a Python package kept somewhere else (its own repository and release), which AI Cleaner loads only when
the person has listed it, and only when every file of it is the file that was released:

- **The registry** `<state>\\aicleaner-addons.json` lists each add-on explicitly: its name, its package, the folder
  its release was unpacked to (`root`), its own home, the paths of its secrets and state (`protect`), and the SHA-256
  of its release manifest. An add-on's installer writes it; AI Cleaner never does. Nothing is discovered from a folder.
- **Sealed** (D247, addonseal.py): the installer seals the registry (HMAC under a DPAPI-protected key) after writing
  it; a registry whose seal does not hold loads nothing. One written before 1.3.1 is sealed once, at first start, when
  every entry verifies and lives in its installer's own folder (`sealed_or_why`).
- **Hash-pinned.** `<root>\\ADDON-MANIFEST` lists `<sha256>  <relative path>` for each file of the release. It must
  match the pin in the registry, every file it lists must match its line, and the package folder may hold nothing it
  does not list. The package is then imported through this module's own finder, which reads each module's source,
  checks its hash and compiles exactly the bytes it checked - no `.pyc` is read or written.
- **Guarded.** The settings guard (hooks/settings_guard.py) refuses a tool call that writes the registry, an add-on's
  code (`root`) or its `protect` paths - so Claude cannot change what is loaded or what it holds.
- **One API object** (AddonAPI below) is everything an add-on gets: routes under `/api/addons/<name>/` behind the same
  guard, API key and remote-access rules as the rest of `/api/`; a Settings card module; its own settings under
  `addons.<name>` in AI Cleaner's config file; the session host, narrowed; start and stop hooks; logging.

With no registry (or an empty one) nothing here does anything: no route, no card, no session, `/api/addons` is `[]`.
Spec: specs/2026-11-17-f185-messages-addon.md (tooling repository).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.abc
import importlib.util
import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import addonseal, fileio, origin
from .guard import REMOTE

log = logging.getLogger("aicleaner.addons")

API_VERSION = 1
REGISTRY = "aicleaner-addons.json"
MANIFEST = "ADDON-MANIFEST"
DESCRIPTION = "addon.json"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
PACKAGE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ROUTE_PATH = re.compile(r"^[A-Za-z0-9_{}:.\-/]+$")
WEB_TYPES = {".js": "text/javascript", ".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml",
             ".png": "image/png"}
SKIP_DIRS = {"__pycache__"}
_CONFIG_LOCK = threading.Lock()


class AddonError(RuntimeError):
    """An add-on cannot be loaded, with the reason the person is shown."""


# ---- the registry and the manifest ---------------------------------------------------------------------------------

def registry_path(state: Path) -> Path:
    return Path(state) / REGISTRY


def read_registry(state: Path, raw: bytes | None = None) -> list[dict[str, Any]]:
    """The listed add-ons (from `raw`, the file's bytes, when given). A missing file is none; one that cannot be read
    is AddonError - nothing loads from it."""
    path = registry_path(state)
    if raw is None and not path.exists():
        return []
    try:
        if raw is None:
            raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise AddonError(f"{path} could not be read ({exc}): no add-on was loaded") from exc
    found = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(found, list):
        raise AddonError(f"{path} has no \"addons\" list: no add-on was loaded")
    return [entry for entry in found if isinstance(entry, dict)]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    """`<sha256>  <relative path>` per line -> {posix path: sha256}."""
    lines: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _sep, rel = line.partition("  ")
        rel = rel.strip().replace("\\", "/")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not rel or rel.startswith("/") or ".." in rel.split("/"):
            raise AddonError(f"{MANIFEST} line {number} is not '<sha256>  <relative path>'")
        lines[rel] = digest
    return lines


def make_manifest(root: Path, folders: tuple[str, ...] = ("src", "installer")) -> str:
    """The manifest for a release folder (for an add-on's build tool and tests): every file under `folders`, sorted,
    `__pycache__` and `logs` left out."""
    root = Path(root)
    rows = []
    for top in folders:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            parts = set(path.relative_to(root).parts)
            if path.is_file() and not parts & (SKIP_DIRS | {"logs"}) and not path.name.endswith((".pyc", ".egg-info")) \
                    and not any(p.endswith(".egg-info") for p in parts):
                rows.append(f"{sha256(path.read_bytes())}  {path.relative_to(root).as_posix()}")
    return "\n".join(rows) + "\n"


def verified_files(root: Path, package: str, pinned: str) -> dict[str, str]:
    """The manifest, checked: its own hash is the pin, every file it lists matches, and the package folder holds
    nothing it does not list. {posix path: sha256}. AddonError says what does not match."""
    root = Path(root)
    manifest = root / MANIFEST
    try:
        raw = manifest.read_bytes()
    except OSError as exc:
        raise AddonError(f"{manifest} could not be read ({exc})") from exc
    if not re.fullmatch(r"[0-9a-f]{64}", str(pinned or "").lower()):
        raise AddonError("the registry gives no manifest_sha256 for it")
    if sha256(raw) != str(pinned).lower():
        raise AddonError(f"{MANIFEST} is not the one the registry pins (changed since it was installed)")
    lines = parse_manifest(raw.decode("utf-8"))
    for rel, digest in lines.items():
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError:
            raise AddonError(f"{rel} is in the manifest but missing") from None
        if sha256(data) != digest:
            raise AddonError(f"{rel} does not match the manifest (changed since it was released)")
    folder = root / "src" / package
    if not folder.is_dir():
        raise AddonError(f"its package folder {folder} is missing")
    for path in folder.rglob("*"):
        # A __pycache__ folder is passed over: the finder compiles the checked source and never reads bytecode.
        if path.is_file() and not set(path.relative_to(folder).parts) & SKIP_DIRS:
            rel = path.relative_to(root).as_posix()
            if rel not in lines:
                raise AddonError(f"{rel} is not in the manifest (a file added after the release)")
    return lines


# ---- the registry's seal (D247) ------------------------------------------------------------------------------------

RERUN = "run the add-on's installer again (it writes and seals the registry); until then no add-on is loaded"


def _installer_owned(entry: dict[str, Any]) -> str:
    """Why an entry of an unsealed registry is not one its installer wrote, or '' when it is: a valid name and package,
    its release unpacked under %LOCALAPPDATA%\\<add-on>\\releases (the installer's own folder), and every file of it
    the release its manifest pin names."""
    name, package = str(entry.get("name", "")), str(entry.get("package", ""))
    if not NAME.fullmatch(name) or not PACKAGE.fullmatch(package):
        return f"the entry {name or '(unnamed)'} is not a valid add-on entry"
    local = os.environ.get("LOCALAPPDATA", "")
    root = str(entry.get("root") or "")
    if not local or not root or not Path(root).is_absolute():
        return f"{name}'s release folder is not an installer's"
    owners = {name, f"aicleaner-{name}", package.replace("_", "-")}
    resolved = Path(os.path.realpath(root))
    bases = [Path(os.path.realpath(Path(local) / owner / "releases")) for owner in owners]
    if not any(base in resolved.parents for base in bases):
        return f"{name}'s release folder {root} is not under %LOCALAPPDATA%\\<add-on>\\releases"
    try:
        verified_files(resolved, package, str(entry.get("manifest_sha256", "")))
    except AddonError as exc:
        return f"{name}'s release does not verify ({exc})"
    return ""


def sealed_or_why(state: Path, entries: list[dict[str, Any]], raw: bytes | None = None) -> str:
    """'' when the registry may be loaded; else why not. A registry written before AI Cleaner sealed it (1.3.0 and an
    add-on installer of then) is sealed once here - only while no seal key exists yet, and only when every entry is
    one its installer wrote (`_installer_owned`). Once a key exists, an unsealed registry is refused."""
    found, why = addonseal.status(state, raw)
    if found == addonseal.SEALED:
        return ""
    if found != addonseal.UNSEALED:
        return f"{why}: {RERUN}"
    if addonseal.has_key(state):
        return f"{why}: {RERUN}"
    problems = [p for p in (_installer_owned(e) for e in entries) if p]
    if problems:
        return f"{why}, and it cannot be sealed as an earlier installer's: {'; '.join(problems)}. {RERUN[0].upper()}{RERUN[1:]}"
    try:
        addonseal.seal(state, raw)
    except addonseal.SealError as exc:
        return f"{why}, and sealing it failed ({exc}): {RERUN}"
    log.info("add-ons: sealed the existing registry %s (written by an add-on installer before AI Cleaner 1.3.1; every "
             "entry verified against its release)", registry_path(state))
    return ""


# ---- importing exactly the verified bytes -------------------------------------------------------------------------

class _Loader(importlib.abc.Loader):
    def __init__(self, path: Path, digest: str) -> None:
        self.path = path
        self.digest = digest

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        data = self.path.read_bytes()
        if sha256(data) != self.digest:
            raise ImportError(f"{self.path} changed after AI Cleaner checked it - not loaded")
        module.__file__ = str(self.path)
        exec(compile(data, str(self.path), "exec", dont_inherit=True), module.__dict__)  # noqa: S102


class _Finder(importlib.abc.MetaPathFinder):
    """Finds the add-on's package and its modules - and nothing else - in its verified folder."""

    def __init__(self, package: str, folder: Path, hashes: dict[Path, str]) -> None:
        self.package = package
        self.folder = folder
        self.hashes = hashes

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != self.package and not fullname.startswith(self.package + "."):
            return None
        base = self.folder.joinpath(*fullname.split(".")[1:])
        init = base / "__init__.py"
        module = base.with_suffix(".py")
        if init in self.hashes:
            spec = importlib.util.spec_from_loader(fullname, _Loader(init, self.hashes[init]), origin=str(init),
                                                   is_package=True)
            spec.submodule_search_locations = [str(base)]
        elif module in self.hashes:
            spec = importlib.util.spec_from_loader(fullname, _Loader(module, self.hashes[module]), origin=str(module))
        else:
            raise ModuleNotFoundError(f"{fullname} is not part of the add-on's release", name=fullname)
        spec.has_location = True
        return spec


def _import(package: str, root: Path, lines: dict[str, str], entry: str) -> Callable[..., Any]:
    folder = root / "src" / package
    hashes = {root / rel: digest for rel, digest in lines.items() if rel.startswith(f"src/{package}/")}
    for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
        del sys.modules[name]                     # a fresh copy: never a module imported from somewhere else
    sys.meta_path[:] = [f for f in sys.meta_path if not (isinstance(f, _Finder) and f.package == package)]
    sys.meta_path.insert(0, _Finder(package, folder, hashes))
    module_name, _sep, function = entry.partition(":")
    if not (module_name == package or module_name.startswith(package + ".")) or not function:
        raise AddonError(f"its entry {entry!r} is not '<module of {package}>:<function>'")
    module = importlib.import_module(module_name)
    found = getattr(module, function, None)
    if not callable(found):
        raise AddonError(f"its entry {entry!r} is not a function")
    return found


# ---- what an add-on is given ---------------------------------------------------------------------------------------

class AddonConfig:
    """The add-on's own settings: `addons.<name>` in AI Cleaner's config file (guarded with it). Values must be JSON."""

    def __init__(self, cfg: Any, name: str) -> None:
        self.cfg = cfg
        self.name = name
        self._values = self._read()

    def _file(self) -> dict[str, Any]:
        path = getattr(self.cfg, "path", None)
        if not path or not Path(path).exists():
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read(self) -> dict[str, Any]:
        section = (self._file().get("addons") or {}).get(self.name)
        return dict(section) if isinstance(section, dict) else {}

    def all(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._values))

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, changes: dict[str, Any]) -> dict[str, Any]:
        json.dumps(changes)                      # TypeError for a value that is not JSON, before anything changes
        with _CONFIG_LOCK:
            path = getattr(self.cfg, "path", None)
            if path:
                data = self._file()
                addons = data.get("addons")
                if not isinstance(addons, dict):
                    addons = data["addons"] = {}
                mine = addons.get(self.name)
                if not isinstance(mine, dict):
                    mine = addons[self.name] = {}
                mine.update(changes)
                tmp = Path(path).with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                fileio.replace(tmp, Path(path))
            self._values.update(changes)
        return self.all()


class Sessions:
    """The session host as an add-on sees it. `host_of()` is the running SessionHost or None (Project Sessions off)."""

    def __init__(self, addon: str, host_of: Callable[[], Any], proxy: Any = None) -> None:
        from .session import SessionError

        self.addon = addon
        self.host_of = host_of
        self.proxy = proxy
        self.SessionError = SessionError

    def live(self) -> dict[str, Any]:
        """This add-on's sessions that are running, by name."""
        host = self.host_of()
        if host is None:
            return {}
        return {name: s for name, s in host.sessions.items()
                if s.running and getattr(s.profile, "addon", "") == self.addon}

    def get(self, name: str) -> Any:
        host = self.host_of()
        return host.sessions.get(name) if host is not None else None

    async def start(self, name: str, resume: str = "", title: str = "") -> dict[str, Any]:
        host = self._host()
        return await host.start(name, resume=resume, title=title)

    async def stop(self, name: str, reason: str = "addon") -> None:
        await self._host().stop(name, reason=reason)

    async def prompt(self, name: str, text: str, origin: str = "", mark: dict[str, Any] | None = None) -> str:
        """A prompt in the session; a `mark` makes it a message from someone else, with its origin note always sent
        and drawn on the page as "Message from ...". "sent" or "queued"."""
        return await self._host().prompt(text, name=name, origin=origin, mark=mark)

    def _host(self) -> Any:
        host = self.host_of()
        if host is None:
            raise self.SessionError("Project Sessions are off")
        return host

    async def enable(self) -> None:
        """Project Sessions on (as Chat now does), when they are off."""
        if self.proxy is None:
            return
        if not self.proxy.cfg.session_enabled:
            self.proxy.cfg.update_settings({"session.enabled": True})
            await self.proxy.apply_session_enabled()

    def available(self) -> bool:
        """Sessions are on and Claude Code is on this machine."""
        host = self.host_of()
        return host is not None and host.available()

    def known(self, name: str) -> bool:
        host = self.host_of()
        return host is not None and any(p.name == name for p in host.profiles)

    def reload(self) -> None:
        if self.proxy is not None:
            self.proxy.reload_profiles()

    def stored(self, name: str) -> dict[str, Any] | None:
        """A project session as stored in the person's own list (profiles.json), or None."""
        if self.proxy is None:
            return None
        entries, _problem = self.proxy.profile_store.load()
        found = next((e for e in entries if e.get("name") == name), None)
        return json.loads(json.dumps(found)) if found is not None else None

    def ensure_profile(self, name: str, folder: Path, brief: str = "") -> str:
        """A project session of the person's own list (no topic), made or pointed at `folder`."""
        if self.proxy is None:
            raise self.SessionError("no AI Cleaner to make a project session in")
        store = self.proxy.profile_store
        entries, _problem = store.load()
        mine = next((e for e in entries if e["name"] == name), None)
        if mine is None:
            entries.append({"name": name, "client": "", "projects": [str(folder)], "workspaces": [], "brief": brief})
        elif [str(p) for p in mine.get("projects", [])] != [str(folder)]:
            mine["projects"] = [str(folder)]
        else:
            return name
        store.save(entries, set(self.proxy.client_labels()) or None)
        self.proxy.reload_profiles()
        return name

    def profile(self, name: str, label: str, folder: Path, brief: str = "", group: str = "") -> Any:
        """A derived session for the add-on's `profiles()` hook: its own folder as home and only project."""
        from .session import Profile

        return Profile(name=name, label=label, projects=[Path(folder)], brief=brief, home_override=str(folder),
                       addon=self.addon, group=group)

    @staticmethod
    def refile(conversation: str, to: Path, folders: list[str | Path]) -> Any:
        """A conversation's transcript moved to be filed under `to` (history.refile)."""
        from .history import refile

        return refile(conversation, to, folders)


@dataclass
class Route:
    method: str
    path: str
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    local_only: bool | None = None


class AddonAPI:
    """Everything an add-on is given (API version 1). See the module's docstring and the spec."""

    def __init__(self, proxy: Any, entry: dict[str, Any], description: dict[str, Any], lines: dict[str, str]) -> None:
        from . import deskrun

        self._proxy = proxy
        self._lines = lines
        self.name: str = entry["name"]
        self.version: str = str(entry.get("version") or description.get("version") or "")
        self.root = Path(entry["root"])
        self.home = Path(entry["home"])
        self.state_path = Path(proxy._state_path()) if proxy is not None else self.home
        self.log_dir = Path(proxy.cfg.log_dir) if proxy is not None and getattr(proxy.cfg, "log_dir", "") else self.home / "logs"
        self.log = logging.getLogger(f"aicleaner.addon.{self.name}")
        self.config = AddonConfig(proxy.cfg if proxy is not None else None, self.name)
        self.sessions = Sessions(self.name, lambda: getattr(proxy, "session", None), proxy)
        self.origin = SimpleNamespace(OPEN=origin.OPEN, CLOSE=origin.CLOSE, quoted=origin.quoted,
                                      forges_note=origin.forges_note)
        self.NoDesktop = deskrun.NoDesktop
        self.DesktopError = deskrun.DesktopError
        self.routes: list[Route] = []

    # ---- routes ----

    def route(self, method: str, path: str, handler: Callable[[web.Request], Awaitable[web.StreamResponse]], *,
              local_only: bool | None = None) -> None:
        """`handler` at /api/addons/<name>/<path>. Through remote access a GET is allowed unless local_only=True; any
        other method is refused unless local_only=False."""
        path = path.strip("/")
        if not path or not ROUTE_PATH.fullmatch(path) or ".." in path.split("/") or path.split("/")[0] == "web":
            raise AddonError(f"route path {path!r} is not allowed")
        self.routes.append(Route(method.upper(), path, handler, local_only))

    @staticmethod
    def remote(request: web.Request) -> bool:
        return bool(request.get(REMOTE))

    # ---- what AI Cleaner holds ----

    def legacy_section(self, name: str) -> dict[str, Any]:
        """A top-level section of AI Cleaner's config file, read-only: for moving a feature's settings to an add-on."""
        section = self.config._file().get(name)
        return json.loads(json.dumps(section)) if isinstance(section, dict) else {}

    def taken_ports(self) -> tuple[int, ...]:
        cfg = getattr(self._proxy, "cfg", None)
        return tuple(p for p in (getattr(cfg, "port", 0), getattr(cfg, "codetab_port", 0)) if p)

    def verified_path(self, rel: str) -> Path:
        """A file of the add-on's release, checked against the manifest now (before it is run, say elevated)."""
        rel = rel.replace("\\", "/")
        digest = self._lines.get(rel)
        path = self.root / rel
        if digest is None:
            raise AddonError(f"{rel} is not part of the add-on's release")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise AddonError(f"{rel} is missing ({exc})") from exc
        if sha256(data) != digest:
            raise AddonError(f"{rel} does not match the add-on's release")
        return path

    @staticmethod
    def dpapi(data: bytes, protect: bool) -> bytes:
        from .secrets import _dpapi

        return _dpapi(data, protect)

    def run_elevated(self, line: str, manual: str) -> Any:
        """D239: an administrator step started on the signed-in desktop (deskrun). NoDesktop carries the command."""
        from .deskrun import launch

        return launch(line, manual, self.state_path / "state" / "desktop")


@dataclass
class Addon:
    entry: dict[str, Any]
    state: str = "disabled"            # loaded, failed, disabled
    error: str = ""
    description: dict[str, Any] = field(default_factory=dict)
    api: AddonAPI | None = None
    instance: Any = None
    web: dict[str, bytes] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.entry.get("name", ""))

    def view(self) -> dict[str, Any]:
        cards = []
        if self.state == "loaded":
            for card in self.description.get("cards") or []:
                if isinstance(card, dict) and card.get("module") in self.web:
                    style = card.get("style")
                    cards.append({"key": str(card.get("key", "")), "title": str(card.get("title", "")),
                                  "group": str(card.get("group", "")), "words": list(card.get("words") or []),
                                  "module": f"/api/addons/{self.name}/web/{card['module']}",
                                  "style": f"/api/addons/{self.name}/web/{style}" if style in self.web else ""})
        return {"name": self.name, "title": str(self.description.get("title") or self.name),
                "version": str(self.entry.get("version") or ""), "state": self.state, "error": self.error,
                "cards": cards}


class AddonHost:
    """The add-ons AI Cleaner loaded, from its registry. Never raises: a broken add-on is marked failed."""

    def __init__(self, proxy: Any) -> None:
        self.proxy = proxy
        self.addons: list[Addon] = []
        self.error = ""

    def load(self) -> None:
        state = Path(self.proxy._state_path())
        raw = None
        try:
            if registry_path(state).exists():
                raw = registry_path(state).read_bytes()     # read once: what is parsed is what the seal is checked on
            entries = read_registry(state, raw)
        except (OSError, AddonError) as exc:
            self.error = str(exc) if isinstance(exc, AddonError) else \
                f"{registry_path(state)} could not be read ({exc}): no add-on was loaded"
            log.warning("%s", self.error)
            return
        if entries:
            refused = sealed_or_why(state, entries, raw)
            if refused:
                # D247: nothing from a registry its installer did not write. Each entry is listed, so the Add-ons card
                # shows (and says why) - none is loaded.
                self.error = refused
                log.warning("add-ons: %s", refused)
                self.addons = [Addon(entry=e, state="failed", error="not loaded: the registry is not sealed")
                               for e in entries]
                return
        seen: set[str] = set()
        for entry in entries:
            addon = Addon(entry=entry)
            self.addons.append(addon)
            if entry.get("enabled", True) is False:
                continue
            try:
                self._load_one(addon, seen)
            except Exception as exc:  # noqa: BLE001 - one add-on never stops AI Cleaner
                addon.state, addon.error = "failed", str(exc) or type(exc).__name__
                log.warning("add-on %s was not loaded: %s", addon.name or "(unnamed)", addon.error)

    def _load_one(self, addon: Addon, seen: set[str]) -> None:
        entry = addon.entry
        name, package = str(entry.get("name", "")), str(entry.get("package", ""))
        if not NAME.fullmatch(name):
            raise AddonError(f"its name {name!r} is not lower-case letters, digits and hyphens")
        if name in seen:
            raise AddonError(f"{name} is listed twice")
        seen.add(name)
        if not PACKAGE.fullmatch(package) or package == "aicleaner" or package in sys.builtin_module_names:
            raise AddonError(f"its package {package!r} is not allowed")
        for key in ("root", "home"):
            if not entry.get(key) or not Path(str(entry[key])).is_absolute():
                raise AddonError(f"its {key} must be a full path")
        if not all(isinstance(p, str) and Path(p).is_absolute() for p in entry.get("protect") or []):
            raise AddonError("each of its protect paths must be a full path")
        root = Path(str(entry["root"]))
        lines = verified_files(root, package, str(entry.get("manifest_sha256", "")))
        described = root / "src" / package / DESCRIPTION
        description = json.loads(described.read_text(encoding="utf-8"))
        if description.get("name") != name:
            raise AddonError(f"its {DESCRIPTION} names {description.get('name')!r}, not {name!r}")
        if description.get("api") != API_VERSION:
            raise AddonError(f"it needs add-on API {description.get('api')!r}; this AI Cleaner has {API_VERSION}")
        addon.description = description
        for card in description.get("cards") or []:
            if not isinstance(card, dict) or not card.get("module"):
                raise AddonError(f"a card in its {DESCRIPTION} names no module")
            for kind in ("module", "style"):
                file = str(card.get(kind) or "")
                if not file:
                    continue
                rel = f"src/{package}/{file}"
                if rel not in lines or Path(file).suffix not in WEB_TYPES or ".." in file.split("/"):
                    raise AddonError(f"its card {kind} {file!r} is not a file of its release")
                addon.web[file] = (root / rel).read_bytes()
                if sha256(addon.web[file]) != lines[rel]:
                    raise AddonError(f"{rel} changed while it was read")
        setup = _import(package, root, lines, str(description.get("entry", "")))
        addon.api = AddonAPI(self.proxy, entry, description, lines)
        addon.instance = setup(addon.api)
        addon.state = "loaded"
        log.info("add-on %s %s loaded from %s", name, addon.api.version, root)

    def loaded(self) -> list[Addon]:
        return [a for a in self.addons if a.state == "loaded"]

    def get(self, name: str) -> Addon | None:
        return next((a for a in self.addons if a.name == name), None)

    def view(self) -> list[dict[str, Any]]:
        return [a.view() for a in self.addons]

    # ---- hooks ----

    def profiles(self, entries: list[dict[str, Any]]) -> list[Any]:
        found: list[Any] = []
        for addon in self.loaded():
            hook = getattr(addon.instance, "profiles", None)
            if hook is None:
                continue
            try:
                for profile in hook(entries) or []:
                    profile.addon = addon.name          # whatever the add-on said: its sessions are its own
                    found.append(profile)
            except Exception as exc:  # noqa: BLE001
                log.warning("add-on %s: its sessions could not be listed: %s", addon.name, exc)
        return found

    async def start(self) -> None:
        for addon in self.loaded():
            hook = getattr(addon.instance, "start", None)
            if hook is None:
                continue
            try:
                await hook()
            except Exception as exc:  # noqa: BLE001
                addon.error = f"it did not start: {exc}"
                log.warning("add-on %s did not start: %s", addon.name, exc, exc_info=True)

    async def stop(self) -> None:
        for addon in reversed(self.loaded()):
            hook = getattr(addon.instance, "stop", None)
            if hook is None:
                continue
            try:
                await asyncio.wait_for(hook(), 30)
            except Exception as exc:  # noqa: BLE001
                log.warning("add-on %s did not stop cleanly: %s", addon.name, exc)

    # ---- the routes ----

    def mount(self, app: web.Application) -> None:
        """`GET /api/addons`, each loaded add-on's routes and card modules. Registered with the UI's routes, before the
        proxy's catch-all."""
        app.router.add_get("/api/addons", self._list)
        for addon in self.loaded():
            assert addon.api is not None
            app.router.add_get(f"/api/addons/{addon.name}/web/{{file:.+}}", self._web(addon))
            for route in addon.api.routes:
                app.router.add_route(route.method, f"/api/addons/{addon.name}/{route.path}",
                                     _guarded(addon.name, route))

    async def _list(self, request: web.Request) -> web.Response:
        # D247: why nothing was loaded is `problem`, not `error` - the page's getJSON takes an `error` in a 200 answer
        # for a failed request, and the Add-ons card (and its reason) vanished.
        return web.json_response({"addons": self.view(), "problem": self.error})

    @staticmethod
    def _web(addon: Addon) -> Callable[[web.Request], Awaitable[web.Response]]:
        async def serve(request: web.Request) -> web.Response:
            file = request.match_info["file"]
            data = addon.web.get(file)
            if data is None:
                return web.json_response({"error": "not found"}, status=404)
            return web.Response(body=data, content_type=WEB_TYPES[Path(file).suffix])
        return serve


def _guarded(name: str, route: Route) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """The remote-access rule on an add-on's route: through remote access only what it says may be done remotely."""
    async def handler(request: web.Request) -> web.StreamResponse:
        local_only = route.local_only if route.local_only is not None else request.method not in ("GET", "HEAD")
        if local_only and request.get(REMOTE):
            return web.json_response({"error": f"local only: the {name} add-on is changed at the machine where AI "
                                               "Cleaner is deployed"}, status=403)
        return await route.handler(request)
    return handler
