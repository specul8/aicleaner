"""What each AI client on this machine is currently pointed at.

The Overview used to say "Client-identity data filtering: On" whenever the mapping was healthy,
which says the filter *could* work and nothing about whether anything goes through it. With
Claude Code set straight to api.anthropic.com the page still claimed every client identifier was
being replaced (D37) - a direct assurance about client data, and untrue at that moment.

Nothing here writes. `Enable-AiCleaner.ps1` owns these files; this reads the same locations back
so the page can say what is true. Read on every request rather than cached at startup, because
the entire point is that they change outside the application - `Disable-AiCleaner.ps1` rewrites
them, and so can a person with an editor.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The key every client uses, whatever shape its settings file takes.
BASE_URL = "ANTHROPIC_BASE_URL"


def _read_json(path: Path) -> Any:
    """The document, or None. A settings file people hand-edit is often mid-edit or absent."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _claude_code(home: Path) -> dict[str, Any]:
    """Always reported (H10): with no settings file Claude Code talks to the provider directly, so a
    missing file is "not routed", never left out as if there were nothing to say."""
    path = home / ".claude" / "settings.json"
    doc = _read_json(path)
    env = doc.get("env") if isinstance(doc, dict) else None
    value = env.get(BASE_URL) if isinstance(env, dict) else None
    return {"client": "Claude Code", "file": str(path), "value": str(value) if value else "",
            "present": True}


# Ways a project's own settings send Claude Code somewhere other than the base URL (H10).
_PROVIDER_SWITCHES = (("CLAUDE_CODE_USE_BEDROCK", "Bedrock"), ("CLAUDE_CODE_USE_VERTEX", "Vertex"),
                      ("CLAUDE_CODE_USE_FOUNDRY", "Foundry"))


def _override(doc: Any, port: int) -> str:
    """Why this project settings document routes around the proxy, or "" when it does not."""
    if not isinstance(doc, dict):
        return ""
    env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    base = str(env.get(BASE_URL) or "")
    if base and not _is_ours(base, port):
        return f"sets its own base URL ({base})"
    for key, name in _PROVIDER_SWITCHES:
        if str(env.get(key, "")).strip().lower() in ("1", "true", "yes"):
            return f"switches to {name} ({key})"
    if doc.get("apiKeyHelper"):
        return "sets an apiKeyHelper, which can supply its own endpoint and key"
    return ""


def overrides(port: int, projects: list[str]) -> list[dict[str, str]]:
    """Project settings files that route Claude Code around the proxy, one entry per file (H10).
    Read on every call, like the rest of this module: a pull or a branch switch changes them."""
    out = []
    for project in dict.fromkeys(str(p) for p in projects if p):
        for name in ("settings.json", "settings.local.json"):
            path = Path(project) / ".claude" / name
            if not path.is_file():
                continue
            why = _override(_read_json(path), port)
            if why:
                out.append({"file": str(path), "why": why})
    return out


def _vs_code(appdata: Path) -> list[dict[str, Any]]:
    """VS Code keeps it in a list of {name, value} rather than a map."""
    out = []
    for folder, label in (("Code", "VS Code"), ("Code - Insiders", "VS Code Insiders")):
        path = appdata / folder / "User" / "settings.json"
        if not path.is_file():
            continue
        doc = _read_json(path)
        value = ""
        if isinstance(doc, dict):
            for key, entries in doc.items():
                if not key.lower().startswith("claude") or not isinstance(entries, list):
                    continue
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("name") == BASE_URL:
                        value = str(entry.get("value") or "")
        out.append({"client": label, "file": str(path), "value": value, "present": True})
    return out


def _is_ours(value: str, port: int) -> bool:
    """True when that URL is this proxy: a loopback host on the port we serve."""
    low = (value or "").lower()
    if not low:
        return False
    return any(f"{host}:{port}" in low for host in ("127.0.0.1", "localhost", "[::1]"))


def clients(port: int, home: Path | None = None, appdata: Path | None = None) -> list[dict[str, Any]]:
    """One entry per client whose settings file exists, with what it currently points at.

    `home` is overridable by AICLEANER_CLIENT_HOME so a test server does not read - or report on -
    the settings of whoever happens to be running it.
    """
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    if override:
        # Both, or the override is useless: APPDATA would still resolve to the real profile and
        # VS Code's settings would be read from it, which is most of what this looks at.
        home = home or Path(override)
        appdata = appdata or Path(override) / "AppData" / "Roaming"
    else:
        home = home or Path.home()
        appdata = appdata or Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
    found = [_claude_code(home)] + _vs_code(appdata)
    for entry in found:
        entry["routed"] = _is_ours(entry["value"], port)
    return [e for e in found if e["present"]]


def summary(port: int, requests_seen: int = 0, home: Path | None = None,
            appdata: Path | None = None, projects: list[str] | None = None) -> dict[str, Any]:
    """Whether anything is actually pointed here, and whether anything has arrived.

    Three states the page can tell apart, which is the whole point:

        routed    at least one client points here - the filter is in the path
        idle      pointed here, but nothing has come through since this proxy started
        bypassed  nothing points here; whatever those clients send is unfiltered
    """
    found = clients(port, home=home, appdata=appdata)
    routed = [e for e in found if e["routed"]]
    if not found:
        state = "unknown"          # no settings file to read: say so rather than guess
    elif not routed:
        state = "bypassed"
    elif requests_seen:
        state = "routed"
    else:
        state = "idle"
    return {"state": state, "clients": found,
            "routed": [e["client"] for e in routed],
            "bypassed": [e["client"] for e in found if not e["routed"]],
            "overrides": overrides(port, projects or [])}
