"""Feature 117: what the Code tab switch writes into Claude Code's own files, and takes out again.

The Claude Desktop app's Code tab reads Claude Code's user settings (`~/.claude/settings.json`), as
VS Code's Claude Code does. With the switch on, its `env` gains:

- `HTTPS_PROXY` - aicleaner's forward proxy (codetab.py), which the Code tab honours;
- `NODE_EXTRA_CA_CERTS` - the Code tab authority (codetab_ca.py), so Claude Code - and only Claude
  Code - accepts aicleaner's certificate for api.anthropic.com;
- `NO_PROXY` - aicleaner's own address, so VS Code and the Session page, already routed there by
  ANTHROPIC_BASE_URL, carry on exactly as before.

Commands Claude Code runs would inherit those, so a SessionStart hook (hooks/clear_proxy.py) empties
them in the environment file Claude Code's Bash and PowerShell tools read (CLAUDE_ENV_FILE); git, npm
and curl connect directly as they do today. MCP servers do not read that file: each local server
defined in `~/.claude.json` gets empty proxy values in its own `env` instead (the user, 2026-10-14:
"exactly as today"). Servers from a project's .mcp.json or a plugin are not edited - one lives in the
person's repository, the other is rewritten by every plugin update - so they keep the setting and
their traffic passes through the proxy untouched.

Everything added is recorded in `<state>/codetab-settings.json`, and the switch turned off removes
exactly that and puts back what was there. As bashguard does: a backup first, an atomic write, and a
file that does not parse is left alone and reported.

Attribution (decision 8) is separate from the switch: `"attribution": {"commit": "", "pr": ""}` is
merged into the user settings and into each profile folder's `.claude/settings.local.json` when
missing, at every start and when a session starts in a folder.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner.codetab")

HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "clear_proxy.py"
MARKER = "clear_proxy.py"
LEDGER = "codetab-settings.json"
PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
ATTRIBUTION = {"commit": "", "pr": ""}
POWERSHELL_FROM = (2, 1, 296)   # the first Claude Code whose PowerShell tool reads CLAUDE_ENV_FILE


class SettingsError(RuntimeError):
    """A file could not be changed, with a reason worth showing."""


def applies() -> bool:
    return os.name == "nt"


def home(base: Path | None = None) -> Path:
    """The person's home; AICLEANER_CLIENT_HOME for tests, as bashguard and rules honour it."""
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    return base or (Path(override) if override else Path.home())


def user_settings(base: Path | None = None) -> Path:
    return home(base) / ".claude" / "settings.json"


def claude_json(base: Path | None = None) -> Path:
    return home(base) / ".claude.json"


def hook_command(python: str | Path | None = None) -> str:
    """Forward slashes throughout: hook commands go through Git Bash on Windows (bashguard's note)."""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return f'"{exe.as_posix()}" "{HOOK_SCRIPT.as_posix()}"'


# ---- reading and writing JSON with care --------------------------------------------------------

def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} could not be read as JSON ({exc}); it was left as it is") from exc
    if not isinstance(doc, dict):
        raise SettingsError(f"{path} is not a settings object; it was left as it is")
    return doc


def _write(path: Path, doc: dict[str, Any], backup: bool = True, backup_dir: Path | None = None) -> None:
    """Backed up first: beside the file, or in `backup_dir` - for a file inside a project folder,
    whose backup beside it was committed with the project (v0.9.226, the user's own repo)."""
    if backup and path.exists():
        stamp = f"{datetime.now():%Y%m%dT%H%M%S}"
        if backup_dir is not None:
            # The project folder's name in the backup's, so two projects' copies stay apart.
            target = backup_dir / f"{path.parent.parent.name}.{path.name}.{stamp}.bak"
        else:
            target = path.with_name(f"{path.name}.{stamp}.bak")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        except OSError as exc:
            log.warning("could not back up %s: %s", path, exc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.aicleaner-tmp")
        temp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise SettingsError(f"{path} could not be written ({exc})") from exc


def _ledger(state_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads((state_dir / LEDGER).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(state_dir: Path, doc: dict[str, Any]) -> None:
    if not doc.get("env") and not doc.get("mcp") and not doc.get("hook"):
        (state_dir / LEDGER).unlink(missing_ok=True)
        return
    _write(state_dir / LEDGER, doc, backup=False)


# ---- the user settings: env and the hook -------------------------------------------------------

def _ours(group: object) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(MARKER in str(h.get("command", "")) for h in hooks if isinstance(h, dict))


def no_proxy_value(existing: str, listen: str) -> str:
    """aicleaner's own addresses added to whatever NO_PROXY already says."""
    have = [part.strip() for part in existing.split(",") if part.strip()]
    for name in (listen, "127.0.0.1", "localhost", "::1"):
        if name and name not in have:
            have.append(name)
    return ",".join(have)


def updated_settings(doc: dict[str, Any], ledger: dict[str, Any], on: bool, proxy_url: str, ca_path: str,
                     listen: str, python: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(settings, ledger) after the switch. Values are put back from the ledger when going off; a value
    changed by hand since is the person's and is left."""
    out = dict(doc)
    env = dict(out.get("env") or {})
    hooks = dict(out.get("hooks") or {})
    groups = [g for g in (hooks.get("SessionStart") or []) if not _ours(g)]
    book = dict(ledger)
    before = dict(book.get("env") or {})          # name -> {"was": value-or-None, "set": value}
    if on:
        current = str(env.get("HTTPS_PROXY", "") or "")
        if current and current != proxy_url and "HTTPS_PROXY" not in before:
            raise SettingsError(f"Claude Code's settings already send its traffic through {current}; "
                                "aicleaner will not replace another proxy - remove it there first")
        # NO_PROXY built on what was there before aicleaner first touched it, so a re-apply does not
        # keep appending.
        own_no_proxy = (before["NO_PROXY"].get("was") if "NO_PROXY" in before else env.get("NO_PROXY")) or ""
        wanted = {"HTTPS_PROXY": proxy_url, "NODE_EXTRA_CA_CERTS": ca_path,
                  "NO_PROXY": no_proxy_value(str(own_no_proxy), listen)}
        for name, value in wanted.items():
            if name not in before:
                before[name] = {"was": env.get(name)}
            before[name]["set"] = value
            env[name] = value
        groups.append({"hooks": [{"type": "command", "command": hook_command(python), "timeout": 10}]})
        book["hook"] = True
    else:
        for name, entry in before.items():
            if env.get(name) != entry.get("set"):
                continue                           # changed by hand since: theirs now
            if entry.get("was") is None:
                env.pop(name, None)
            else:
                env[name] = entry["was"]
        before = {}
        book.pop("hook", None)
    book["env"] = before
    if env:
        out["env"] = env
    else:
        out.pop("env", None)
    if groups:
        hooks["SessionStart"] = groups
    else:
        hooks.pop("SessionStart", None)
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out, book


# ---- MCP servers in ~/.claude.json -------------------------------------------------------------

def _local_server(server: object) -> bool:
    """A server Claude Code starts here (stdio): it inherits the settings' env."""
    return isinstance(server, dict) and bool(server.get("command")) and server.get("type", "stdio") == "stdio"


def _server_tables(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(where, mcpServers table) for the user scope and every project's local scope."""
    tables: list[tuple[str, dict[str, Any]]] = []
    if isinstance(doc.get("mcpServers"), dict):
        tables.append(("", doc["mcpServers"]))
    for project, entry in (doc.get("projects") or {}).items():
        if isinstance(entry, dict) and isinstance(entry.get("mcpServers"), dict):
            tables.append((project, entry["mcpServers"]))
    return tables


def updated_mcp(doc: dict[str, Any], ledger: dict[str, Any], on: bool) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """(claude.json, ledger, changed). On: empty proxy values added to each local server's env where
    it has none of its own. Off: the ones added, still empty, taken out again."""
    book = dict(ledger)
    added: list[dict[str, Any]] = list(book.get("mcp") or [])
    changed = False
    if on:
        known = {(a["where"], a["name"]) for a in added}
        for where, table in _server_tables(doc):
            for name, server in table.items():
                if not _local_server(server):
                    continue
                env = server.get("env") if isinstance(server.get("env"), dict) else {}
                keys = [k for k in PROXY_KEYS if k not in env]
                if not keys:
                    continue
                server["env"] = {**env, **{k: "" for k in keys}}
                changed = True
                if (where, name) in known:
                    entry = next(a for a in added if a["where"] == where and a["name"] == name)
                    entry["keys"] = sorted(set(entry["keys"]) | set(keys))
                else:
                    added.append({"where": where, "name": name, "keys": keys})
    else:
        tables = dict(_server_tables(doc))
        for entry in added:
            server = (tables.get(entry["where"]) or {}).get(entry["name"])
            if not isinstance(server, dict) or not isinstance(server.get("env"), dict):
                continue
            env = dict(server["env"])
            for key in entry["keys"]:
                if env.get(key) == "":
                    env.pop(key)
                    changed = True
            if env:
                server["env"] = env
            else:
                server.pop("env", None)
        added = []
    book["mcp"] = added
    return doc, book, changed


# ---- the switch --------------------------------------------------------------------------------

def apply(on: bool, state_dir: Path, proxy_url: str, ca_path: str, listen: str,
          base: Path | None = None, python: str | Path | None = None) -> str:
    """Make Claude Code's files match the switch. Returns what was done, for the page; raises
    SettingsError when a file cannot be read or written."""
    if not applies():
        return ""
    state_dir.mkdir(parents=True, exist_ok=True)
    book = _ledger(state_dir)
    if not on and not book:
        return ""
    target = user_settings(base)
    doc = _read(target)
    new, book = updated_settings(doc, book, on, proxy_url, ca_path, listen, python)
    if new != doc:
        _write(target, new)
    said = apply_mcp(on, state_dir, base, book)
    return ("Claude Code set to send its traffic through aicleaner's Code tab proxy" if on
            else "aicleaner's Code tab proxy removed from Claude Code's settings") + (f"; {said}" if said else "") + \
        ". Restart the Claude Desktop app and any Claude Code sessions for it to take effect."


def apply_mcp(on: bool, state_dir: Path, base: Path | None = None, book: dict[str, Any] | None = None) -> str:
    """The MCP part alone - also run whenever the MCP watcher looks again, for servers added since."""
    if not applies():
        return ""
    book = dict(book if book is not None else _ledger(state_dir))
    path = claude_json(base)
    said = ""
    if path.exists():
        doc = _read(path)
        doc, book, changed = updated_mcp(doc, book, on)
        if changed:
            # Claude Code rewrites this file often: read just above, written at once, no backup copy
            # of a file that also holds its history.
            _write(path, doc, backup=False)
            count = len(book.get("mcp") or [])
            said = f"{count} local MCP server(s) kept off the proxy" if on else "MCP servers put back as they were"
    elif not on:
        book["mcp"] = []
    _save_ledger(state_dir, book)
    return said


def state(state_dir: Path, base: Path | None = None) -> dict[str, Any]:
    """What is in place now, for the health check and the Settings card."""
    book = _ledger(state_dir)
    try:
        env = (_read(user_settings(base)).get("env") or {})
        hooks = (_read(user_settings(base)).get("hooks") or {}).get("SessionStart") or []
    except SettingsError:
        env, hooks = {}, []
    return {"proxy_set": bool(env.get("HTTPS_PROXY")) and "HTTPS_PROXY" in (book.get("env") or {}),
            "ca_set": bool(env.get("NODE_EXTRA_CA_CERTS")),
            "hook": any(_ours(g) for g in hooks),
            "mcp_servers": len(book.get("mcp") or [])}


# ---- Claude Code's version ---------------------------------------------------------------------

def claude_version(timeout: float = 15) -> tuple[int, ...] | None:
    exe = shutil.which("claude")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(n) for n in found.groups()) if found else None


def powershell_ok(version: tuple[int, ...] | None) -> bool | None:
    return None if version is None else version >= POWERSHELL_FROM


# ---- attribution (decision 8) ------------------------------------------------------------------

def _merge_attribution(path: Path, backup_dir: Path | None = None) -> bool:
    doc = _read(path)
    if "attribution" in doc:
        return False                               # set already, by us or by them: theirs
    doc["attribution"] = dict(ATTRIBUTION)
    _write(path, doc, backup=path.exists(), backup_dir=backup_dir)
    return True


def apply_attribution(folders: list[str], base: Path | None = None) -> list[str]:
    """Merged where missing: the user settings and each folder's .claude/settings.local.json. The
    files changed; a folder that does not exist is skipped, a file that does not parse is reported."""
    changed: list[str] = []
    home = user_settings(base)
    # A project's file is backed up outside the project, never beside it where it can be committed.
    away = home.parent / "aicleaner-backups"
    targets = [(home, None)] + [(Path(f) / ".claude" / "settings.local.json", away)
                                for f in dict.fromkeys(folders) if f and Path(f).is_dir()]
    for path, backup_dir in targets:
        try:
            if _merge_attribution(path, backup_dir):
                changed.append(str(path))
        except SettingsError as exc:
            log.warning("attribution not set: %s", exc)
    return changed
