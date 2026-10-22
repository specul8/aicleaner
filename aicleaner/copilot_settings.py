"""GitHub Copilot release 6 (2026-10-19; spec 2026-10-19-copilot-6-wiring.md): what the Copilot switch
writes into VS Code's settings and the Windows certificate store, and takes out again.

VS Code's `http.proxy` points at aicleaner's forward proxy (codetab.py), whose Copilot reader presents
a certificate from the Copilot authority (codetab_ca.py, kind "copilot" - name-constrained to Copilot's
hosts). VS Code checks certificates against the Windows store, so that authority is added to the
CurrentUser Root store; Windows asks the person to confirm, which is why it is only ever added when
someone at this machine asked for it.

VS Code's settings files are JSON with comments, so they are edited as text: comments and layout stay,
only the two members change. What was there before is kept in `<state>/copilot-settings.json` and put
back when the switch goes off - where the value is still ours, so a later change by the person stands.

AICLEANER_CLIENT_HOME (the tests' home, as codetab_settings honours it) also stands in for APPDATA and
for the certificate store, which is then a file in that home: no test reaches the real store.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .codetab_settings import SettingsError, home

log = logging.getLogger("aicleaner.copilot")

LEDGER = "copilot-settings.json"
KEYS = ("http.proxy", "http.proxySupport")
SUPPORT = "override"        # VS Code's default, written so a person's "off" does not leave Copilot unrouted
ADD_TIMEOUT_S = 300         # Windows' confirmation waits for the person


# ---- where VS Code keeps its settings ----------------------------------------------------------

def _appdata(base: Path | None = None) -> Path:
    if base is not None or os.environ.get("AICLEANER_CLIENT_HOME", "").strip():
        return home(base) / "AppData" / "Roaming"
    return Path(os.environ.get("APPDATA") or home() / "AppData" / "Roaming")


def settings_files(base: Path | None = None) -> list[Path]:
    """Each VS Code on this machine: desktop and Insiders where their User folder exists, and a VS Code
    Server's machine settings where one lives here (Copilot's extension then runs here too)."""
    found = [_appdata(base) / product / "User" / "settings.json" for product in ("Code", "Code - Insiders")
             if (_appdata(base) / product / "User").is_dir()]
    for server in (".vscode-server", ".vscode-server-insiders"):
        machine = home(base) / server / "data" / "Machine"
        if machine.is_dir():
            found.append(machine / "settings.json")
    return found


# ---- JSON with comments, edited in place -------------------------------------------------------

def _mask(text: str) -> str:
    """The text with comments and trailing commas blanked to spaces (line breaks kept), so it is plain
    JSON at the same offsets: a position found in it is the same position in the original."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            for j in range(i, end):
                if text[j] != "\n":
                    out[j] = " "
            i = end
            continue
        i += 1
    masked = "".join(out)
    # Trailing commas, now that no comment can hide what follows one.
    out = list(masked)
    i = 0
    while i < n:
        c = masked[i]
        if c == '"':
            i += 1
            while i < n and masked[i] != '"':
                i += 2 if masked[i] == "\\" else 1
        elif c == ",":
            j = i + 1
            while j < n and masked[j].isspace():
                j += 1
            if j < n and masked[j] in "}]":
                out[i] = " "
        i += 1
    return "".join(out)


def _skip(masked: str, i: int) -> int:
    while i < len(masked) and masked[i].isspace():
        i += 1
    return i


def _members(masked: str) -> tuple[int, list[tuple[str, int, int, int]]]:
    """The top-level object's opening brace and each member: (key, key start, value start, value end)."""
    decoder = json.JSONDecoder()
    i = _skip(masked, 0)
    if i >= len(masked) or masked[i] != "{":
        raise ValueError("the settings are not an object")
    brace, i = i, _skip(masked, i + 1)
    found: list[tuple[str, int, int, int]] = []
    while i < len(masked) and masked[i] != "}":
        key, after = decoder.raw_decode(masked, i)
        colon = _skip(masked, after)
        if masked[colon:colon + 1] != ":":
            raise ValueError(f"no ':' after {key!r}")
        start = _skip(masked, colon + 1)
        _value, end = decoder.raw_decode(masked, start)
        found.append((key, i, start, end))
        i = _skip(masked, end)
        if masked[i:i + 1] == ",":
            i = _skip(masked, i + 1)
    return brace, found


def parse(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    doc = json.loads(_mask(text))
    if not isinstance(doc, dict):
        raise ValueError("the settings are not an object")
    return doc


def set_member(text: str, key: str, value: Any) -> str:
    """`key` set to `value`: its value replaced where it is, or a new first member."""
    if not text.strip():
        return "{\n    " + json.dumps(key) + ": " + json.dumps(value) + "\n}\n"
    masked = _mask(text)
    brace, members = _members(masked)
    for name, _start, value_start, value_end in members:
        if name == key:
            return text[:value_start] + json.dumps(value) + text[value_end:]
    indent = "    "
    if members:
        line_start = text.rfind("\n", 0, members[0][1]) + 1
        lead = text[line_start:members[0][1]]
        indent = lead if lead.strip() == "" and lead else indent
    comma = "," if members else ""
    return text[:brace + 1] + "\n" + indent + json.dumps(key) + ": " + json.dumps(value) + comma + text[brace + 1:]


def remove_member(text: str, key: str) -> str:
    """`key` taken out, with its comma, and its line if nothing else was on it."""
    masked = _mask(text)
    _brace, members = _members(masked)
    for index, (name, key_start, _value_start, value_end) in enumerate(members):
        if name != key:
            continue
        start, end = key_start, value_end
        after = _skip(masked, end)
        if masked[after:after + 1] == ",":
            end = after + 1
        elif index > 0:
            comma = masked.rfind(",", members[index - 1][3], key_start)
            if comma >= 0:
                text = text[:comma] + text[comma + 1:]
                masked = masked[:comma] + masked[comma + 1:]
                start, end = start - 1, end - 1
        line_start = text.rfind("\n", 0, start) + 1
        if text[line_start:start].strip() == "":
            start = line_start
            line_end = text.find("\n", end)
            if line_end >= 0 and text[end:line_end].strip() == "":
                end = line_end + 1
        return text[:start] + text[end:]
    return text


# ---- reading and writing a settings file -------------------------------------------------------

def _read(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        return text, parse(text)
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path} could not be read ({exc}); it was left as it is") from exc


def _write(path: Path, text: str) -> None:
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(f"{path.name}.{datetime.now():%Y%m%dT%H%M%S}.bak"))
        except OSError as exc:
            log.warning("could not back up %s: %s", path, exc)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.aicleaner-tmp")
        temp.write_text(text, encoding="utf-8")
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
    path = state_dir / LEDGER
    if not doc.get("files"):
        path.unlink(missing_ok=True)
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{LEDGER}.aicleaner-tmp")
    temp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _point(path: Path, proxy_url: str, before: dict[str, Any] | None) -> dict[str, Any]:
    """One file pointed at aicleaner; returns what it held before (kept from an earlier `before`)."""
    text, doc = _read(path)
    was = before or {key: ({"had": True, "value": doc[key]} if key in doc else {"had": False}) for key in KEYS}
    wanted = {"http.proxy": proxy_url, "http.proxySupport": SUPPORT}
    if all(doc.get(key) == value for key, value in wanted.items()):
        return was
    new = text
    for key, value in wanted.items():
        new = set_member(new, key, value)
    try:
        landed = parse(new)
    except ValueError as exc:
        raise SettingsError(f"{path} would not parse after the change ({exc}); it was left as it is") from exc
    if any(landed.get(key) != value for key, value in wanted.items()):
        raise SettingsError(f"{path} holds http.proxy in a form aicleaner cannot edit safely; it was left as it is")
    _write(path, new)
    return was


def _unpoint(path: Path, proxy_url: str, before: dict[str, Any]) -> bool:
    """What was there before put back - only for a member still holding what aicleaner wrote."""
    if not path.exists():
        return False
    text, doc = _read(path)
    ours = {"http.proxy": proxy_url, "http.proxySupport": SUPPORT}
    new = text
    for key in KEYS:
        if doc.get(key) != ours[key]:
            continue
        earlier = before.get(key) or {"had": False}
        new = set_member(new, key, earlier["value"]) if earlier.get("had") else remove_member(new, key)
    if new == text:
        return False
    try:
        parse(new)
    except ValueError as exc:
        raise SettingsError(f"{path} would not parse after the change ({exc}); it was left as it is") from exc
    _write(path, new)
    return True


def apply(on: bool, state_dir: Path, proxy_url: str, base: Path | None = None) -> str:
    """VS Code pointed at aicleaner's forward proxy (on) or put back as it was (off). Returns what was
    done, for the page; empty when nothing changed."""
    book = _ledger(state_dir)
    files: dict[str, Any] = book.get("files") or {}
    if on:
        targets = settings_files(base)
        if not targets:
            return "no VS Code found on this machine, so nothing was pointed at aicleaner"
        problems: list[str] = []
        for path in targets:
            try:
                files[str(path)] = _point(path, proxy_url, files.get(str(path)))
            except SettingsError as exc:
                problems.append(str(exc))
        book.update({"files": files, "proxy": proxy_url})
        _save_ledger(state_dir, book)
        if problems:
            raise SettingsError("; ".join(problems))
        return f"VS Code points at aicleaner ({len(targets)} settings file{'s' if len(targets) != 1 else ''})"
    if not files:
        return ""
    url = book.get("proxy") or proxy_url
    changed = 0
    for name, before in list(files.items()):
        try:
            changed += _unpoint(Path(name), url, before)
        except SettingsError as exc:
            log.warning("%s", exc)
            continue
        files.pop(name)
    book["files"] = files
    _save_ledger(state_dir, book)
    return f"VS Code's proxy setting taken out ({changed} file{'s' if changed != 1 else ''})" if changed else ""


def state(state_dir: Path, proxy_url: str, base: Path | None = None) -> dict[str, Any]:
    """Which VS Code settings files point here now, for the Settings card and the Overview."""
    rows = []
    for path in settings_files(base):
        try:
            points = _read(path)[1].get("http.proxy") == proxy_url
        except SettingsError:
            points = False
        rows.append({"path": str(path), "points_here": points})
    return {"files": rows, "vscode_found": bool(rows), "pointed": sum(r["points_here"] for r in rows)}


# ---- the certificate in the person's Windows store ---------------------------------------------

def thumbprint(ca_path: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(Path(ca_path).read_bytes())
    return cert.fingerprint(hashes.SHA1()).hex().upper()


class CertStore:
    """The CurrentUser Root store, through certutil. Reading never prompts; adding and removing do."""

    def has(self, thumb: str) -> bool:
        # The Root store as this user sees it (their own and the machine's), read without a prompt.
        # Not `certutil -store`: it answered 0 for a thumbprint that was not there (2026-10-19).
        import hashlib
        import ssl

        wanted = thumb.upper()
        return any(encoding == "x509_asn" and hashlib.sha1(der).hexdigest().upper() == wanted  # noqa: S324 - a thumbprint
                   for der, encoding, _trust in ssl.enum_certificates("ROOT"))

    def add(self, ca_path: Path, label: str = "Copilot") -> None:
        try:
            done = subprocess.run(["certutil", "-user", "-addstore", "Root", str(ca_path)], capture_output=True,
                                  text=True, timeout=ADD_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise SettingsError(f"Windows' confirmation was not answered, so the {label} certificate was not added") from exc
        if done.returncode != 0:
            raise SettingsError(f"the {label} certificate was not added to your Windows certificates "
                                f"(declined, or certutil failed: {(done.stdout or done.stderr).strip()[-200:]})")

    def remove(self, thumb: str) -> None:
        done = subprocess.run(["certutil", "-user", "-delstore", "Root", thumb], capture_output=True, text=True,
                              timeout=ADD_TIMEOUT_S)
        if done.returncode != 0:
            raise SettingsError(f"the Copilot certificate was not removed ({(done.stdout or done.stderr).strip()[-200:]})")


class FileStore:
    """A stand-in for the store, kept in the test home's folder: what the tests add and remove."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / "test-root-store.json"

    def _read(self) -> list[str]:
        try:
            return list(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return []

    def has(self, thumb: str) -> bool:
        return thumb in self._read()

    def add(self, ca_path: Path, label: str = "Copilot") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(set(self._read()) | {thumbprint(ca_path)})), encoding="utf-8")

    def remove(self, thumb: str) -> None:
        if self.path.exists():
            self.path.write_text(json.dumps([t for t in self._read() if t != thumb]), encoding="utf-8")


def cert_store() -> Any:
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    return FileStore(Path(override)) if override else CertStore()
