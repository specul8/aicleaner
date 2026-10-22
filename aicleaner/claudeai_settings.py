"""Route B (spec 2026-10-21-desktop-chat-route-b.md, section 2): Claude Desktop's chat sent through aicleaner by
the Windows proxy setting.

Claude Desktop's window uses the user's Windows proxy setting (probed 2026-10-21). Switching route B on points
the user's proxy auto-config (`AutoConfigURL`, HKCU Internet Settings) at aicleaner's PAC file, which sends
`claude.ai` and its subdomains to the forward proxy and every other host where it went before:

- nothing set before: `DIRECT`;
- a fixed proxy before (`ProxyEnable` + `ProxyServer`): that proxy, and its bypass list honoured;
- a PAC before: its script fetched at switch-on and kept, with its `FindProxyForURL` renamed, so it still
  decides every other host.

claude.ai gets `PROXY` with **no DIRECT fallback**: with the forward proxy down, Desktop's chat fails rather than
going out unfiltered. The values that were there before are kept in a ledger and put back exactly at switch-off
and uninstall; WinINET-based programs are told the settings changed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner.claudeai")

LEDGER = "claudeai-ledger.json"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
# What switch-on may change, read and kept as they were: the PAC, the fixed proxy, its bypass list, auto-detect.
VALUES = ("AutoConfigURL", "ProxyEnable", "ProxyServer", "ProxyOverride", "AutoDetect")
HOSTS = ("claude.ai",)
FETCH_TIMEOUT_S = 8
MAX_SCRIPT = 512 * 1024


class SettingsError(RuntimeError):
    """The proxy setting could not be changed or put back, with a reason worth showing."""


# ---- where the values live: the registry, or a file in tests -------------------------------------------------

class Registry:
    """HKCU Internet Settings through winreg; WinINET told after every change."""

    def read(self) -> dict[str, Any]:
        import winreg

        out: dict[str, Any] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            for name in VALUES:
                try:
                    out[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    pass
        return out

    def write(self, values: dict[str, Any], remove: tuple[str, ...] = ()) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
                for name, value in values.items():
                    kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                    winreg.SetValueEx(key, name, 0, kind, value)
                for name in remove:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            raise SettingsError(f"the Windows proxy setting could not be changed ({exc})") from exc
        _tell_wininet()


def _tell_wininet() -> None:
    """INTERNET_OPTION_SETTINGS_CHANGED then INTERNET_OPTION_REFRESH: running programs re-read the setting."""
    try:
        import ctypes

        wininet = ctypes.windll.wininet
        wininet.InternetSetOptionW(None, 39, None, 0)
        wininet.InternetSetOptionW(None, 37, None, 0)
    except (OSError, AttributeError) as exc:      # not fatal: new connections read the registry anyway
        log.warning("could not tell running programs the proxy setting changed: %s", exc)


class FileRegistry:
    """A stand-in kept in the test home's folder: what the tests read and change."""

    def __init__(self, folder: Path) -> None:
        self.path = Path(folder) / "test-internet-settings.json"

    def read(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in doc.items() if k in VALUES}
        except (OSError, ValueError):
            return {}

    def write(self, values: dict[str, Any], remove: tuple[str, ...] = ()) -> None:
        doc = self.read()
        doc.update(values)
        for name in remove:
            doc.pop(name, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc), encoding="utf-8")


def registry() -> Any:
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    return FileRegistry(Path(override)) if override else Registry()


# ---- the PAC file ----------------------------------------------------------------------------------------------

def _js(text: str) -> str:
    return json.dumps(text)


def _fixed_proxy(server: str) -> str:
    """`ProxyServer` as a PAC answer: "host:port", or per scheme ("http=a:1;https=b:2") - the https one wins."""
    server = (server or "").strip()
    if "=" in server:
        schemes = dict(part.split("=", 1) for part in server.split(";") if "=" in part)
        server = schemes.get("https") or schemes.get("http") or next(iter(schemes.values()), "")
    server = re.sub(r"^[a-z]+://", "", server.strip(), flags=re.I)
    return f"PROXY {server}" if server else "DIRECT"


def _bypass_rules(override: str) -> list[str]:
    """`ProxyOverride` ("*.corp;<local>;10.*") as PAC conditions."""
    rules = []
    for item in (override or "").split(";"):
        item = item.strip().lower()
        if not item:
            continue
        if item == "<local>":
            rules.append('isPlainHostName(host)')
        else:
            rules.append(f"shExpMatch(host, {_js(item)})")
    return rules


def pac(forward_port: int, before: dict[str, Any], before_script: str = "") -> str:
    """The PAC file aicleaner serves while route B is on."""
    ours = " || ".join(f"host == {_js(h)} || dnsDomainIs(host, {_js('.' + h)})" for h in HOSTS)
    lines = ["// aicleaner (route B): claude.ai goes through aicleaner's filter; everything else as before.",
             "// Written by aicleaner; switch it off in aicleaner's Settings to put your own setting back."]
    if before_script:
        lines.append(before_script.replace("FindProxyForURL", "aicleanerBeforeFindProxyForURL"))
    lines += ["function FindProxyForURL(url, host) {",
              "  host = host.toLowerCase();",
              # No DIRECT after it: with aicleaner's forward proxy down, claude.ai fails instead of leaving unfiltered.
              f"  if ({ours}) return \"PROXY 127.0.0.1:{int(forward_port)}\";"]
    if before_script:
        lines.append("  return aicleanerBeforeFindProxyForURL(url, host);")
    elif before.get("ProxyEnable") and before.get("ProxyServer"):
        rules = _bypass_rules(str(before.get("ProxyOverride") or ""))
        if rules:
            lines.append(f"  if ({' || '.join(rules)}) return \"DIRECT\";")
        lines.append(f"  return {_js(_fixed_proxy(str(before['ProxyServer'])))};")
    else:
        lines.append('  return "DIRECT";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def fetch_script(url: str) -> str:
    """The PAC the person had before, fetched once at switch-on (a corporate one usually sits on a web server)."""
    if url.lower().startswith("file:"):
        path = urllib.request.url2pathname(url[5:].lstrip("/") if re.match(r"file:/+[a-z]:", url, re.I) else url[5:])
        text = Path(path).read_bytes()[:MAX_SCRIPT + 1]
    else:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as reply:   # noqa: S310 - the person's own PAC
            text = reply.read(MAX_SCRIPT + 1)
    if len(text) > MAX_SCRIPT:
        raise SettingsError(f"your existing proxy script at {url} is larger than {MAX_SCRIPT // 1024} KB")
    script = text.decode("utf-8", "replace")
    if "FindProxyForURL" not in script:
        raise SettingsError(f"your existing proxy script at {url} has no FindProxyForURL")
    return script


# ---- the ledger and the switch ----------------------------------------------------------------------------------

def _ledger(state_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads((Path(state_dir) / LEDGER).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(state_dir: Path, doc: dict[str, Any] | None) -> None:
    path = Path(state_dir) / LEDGER
    if not doc:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(LEDGER + ".tmp")
    temp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def kept(state_dir: Path) -> dict[str, Any]:
    """What switch-on recorded: the values before, and the earlier PAC's script (for the PAC file)."""
    return _ledger(state_dir)


def ours(url: str) -> bool:
    """A PAC address aicleaner itself set (its own port, or the PAC service's) - never kept as "before"."""
    return bool(re.match(r"^http://127\.0\.0\.1:\d+/claudeai\.pac$", url or ""))


def record(state_dir: Path, reg: Any = None) -> dict[str, Any]:
    """What the setting held before route B, recorded once - before anything changes, so off can always put it
    back - with an earlier PAC's script fetched. Returns the ledger (the PAC file is built from it)."""
    reg = reg or registry()
    book = _ledger(state_dir)
    if book:
        return book
    before = {k: v for k, v in reg.read().items()}
    if ours(str(before.get("AutoConfigURL") or "")):
        before.pop("AutoConfigURL")      # left by an earlier run that did not finish putting it back
    script = ""
    if before.get("AutoConfigURL"):
        try:
            script = fetch_script(str(before["AutoConfigURL"]))
        except (OSError, ValueError, SettingsError) as exc:
            raise SettingsError(f"your existing proxy script ({before['AutoConfigURL']}) could not be read "
                                f"({exc}), so it was left as it is: route B would have bypassed it") from exc
    book = {"before": before, "before_script": script, "pac_url": ""}
    _save_ledger(state_dir, book)
    return book


def apply(on: bool, state_dir: Path, pac_url: str, reg: Any = None) -> str:
    """The proxy setting pointed at aicleaner's PAC (on) or put back as it was (off). Returns what was done,
    for the page; empty when nothing changed."""
    reg = reg or registry()
    book = _ledger(state_dir)
    now = reg.read()
    if on:
        if book.get("pac_url") == pac_url and now.get("AutoConfigURL") == pac_url:
            return ""
        book = record(state_dir, reg)
        book["pac_url"] = pac_url
        _save_ledger(state_dir, book)
        reg.write({"AutoConfigURL": pac_url})
        return "Claude Desktop's chat now goes through aicleaner (the Windows proxy setting points at its PAC file)"
    if not book:
        return ""
    before = book.get("before") or {}
    if now.get("AutoConfigURL") == book.get("pac_url") or (ours(str(now.get("AutoConfigURL") or ""))
                                                          and not book.get("pac_url")):
        restore = {k: before[k] for k in VALUES if k in before}
        reg.write(restore, remove=tuple(k for k in ("AutoConfigURL",) if k not in before))
        said = "the Windows proxy setting is back as it was before route B"
    else:
        # Someone changed it since: theirs stays.
        said = "the Windows proxy setting was changed since route B was switched on, so it was left as it is"
    _save_ledger(state_dir, None)
    return said


def state(state_dir: Path, pac_url: str, reg: Any = None) -> dict[str, Any]:
    """Whether the setting points at aicleaner now, for the Settings card and the Overview."""
    reg = reg or registry()
    try:
        now = reg.read()
    except OSError:
        now = {}
    book = _ledger(state_dir)
    before = book.get("before") or {}
    return {"pointed": now.get("AutoConfigURL") == pac_url, "had_pac": bool(before.get("AutoConfigURL")),
            "had_proxy": bool(before.get("ProxyEnable") and before.get("ProxyServer"))}
