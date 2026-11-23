"""f187 (Brad, 2026-11-19): the roles a project session's folders play.

Three independent roles, set in the profile editor by a toggle button in front of each folder:

- **Client assets** (`assets`): the files that cross between the person and the client - documents, designs,
  exports. Usually the OneDrive folder. Exactly one per project session (Save needs it).
- **AI tooling (CWD)** (`tooling`): where the AI agent runs and its commands start. At most one. Sessions start
  here; with none, in the assets folder.
- **Client code** (`client_code`): folders that hold the client-facing solution. A label only, any number.

A folder may hold any combination, including none (a template repo from elsewhere: reachable, no role). Roles never
order anything - the folder list is in the person's order everywhere (Brad, 2026-11-19).

The assets folder is the TOPIC's (`client-shares.json`; Brad, 2026-11-19: "each project session reads the client
assets folder from the topic. It can be changed either in the topic or the project session configuration, but it
changes it in both places (as well as any other project session)"). A session with no topic keeps one of its own. On
upgrade (`upgrade`) each project session's current home folder becomes its AI tooling folder, so nothing moves.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

ROLE_TEXT = {
    "assets": ("Client assets: the files that cross between you and the client - documents, designs, exports. "
               "Usually your OneDrive folder. Exactly one per project session."),
    "tooling": ("AI tooling (working folder): where the AI agent runs and its commands start; used to build "
                "non-client-facing tooling, operating rules and scripts to deliver the client outcomes. At most one."),
    "code": ("Client code: folders that hold the client-facing solution - code and configuration delivered to the "
             "client. A label only; any number."),
    "none": "Other folder - accessible to the session, no role.",
}

NEEDS_ASSETS = "choose the Client Assets folder - press Assets in front of one of its folders - before saving"


def same(left: str | Path, right: str | Path) -> bool:
    """One folder, however it is written (case, slashes, a trailing separator) - as workspace._same."""
    return (str(left).replace("/", "\\").rstrip("\\").lower()
            == str(right).replace("/", "\\").rstrip("\\").lower())


def _path(value: Any) -> str:
    return os.path.expandvars(str(value or "")).strip()


def clean(entry: dict[str, Any], projects: list[str]) -> tuple[list[str], dict[str, Any]]:
    """(the folder list, the role fields) as stored. An assets or tooling folder not in the list joins it at the end;
    a client-code path that is not one of the folders is dropped. The list is never reordered."""
    folders = list(projects)
    assets = _path(entry.get("assets"))
    tooling = _path(entry.get("tooling"))
    for folder in (assets, tooling):
        if folder and not any(same(folder, known) for known in folders):
            folders.append(folder)
    code: list[str] = []
    for raw in entry.get("client_code") or []:
        folder = _path(raw)
        if not folder or any(same(folder, known) for known in code):
            continue
        listed = next((known for known in folders if same(known, folder)), "")
        if listed:
            code.append(listed)
    fields: dict[str, Any] = {}
    if assets:
        fields["assets"] = next(known for known in folders if same(known, assets))
    if tooling:
        fields["tooling"] = next(known for known in folders if same(known, tooling))
    if code:
        fields["client_code"] = code
    return folders, fields


def has_roles(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("assets") or "").strip() or str(entry.get("tooling") or "").strip()
                or entry.get("client_code"))


def cwd_of(entry: dict[str, Any]) -> str:
    """Where sessions start: the AI tooling folder, else the client assets folder; "" when neither is set."""
    return str(entry.get("tooling") or "").strip() or str(entry.get("assets") or "").strip()


def roles_of(entry: dict[str, Any], folder: str | Path) -> list[str]:
    """The roles one folder plays ("assets", "tooling", "code"), in that order."""
    out = []
    if entry.get("assets") and same(entry["assets"], folder):
        out.append("assets")
    if entry.get("tooling") and same(entry["tooling"], folder):
        out.append("tooling")
    if any(same(code, folder) for code in entry.get("client_code") or []):
        out.append("code")
    return out


def assets_problem(entry: dict[str, Any]) -> str:
    """Why this profile cannot be saved from the editor as it is, or "": it needs exactly one assets folder, and it must
    be one of its folders (clean() makes it one)."""
    if not str(entry.get("assets") or "").strip():
        return f"{entry.get('name') or 'This project session'}: {NEEDS_ASSETS}"
    return ""


def upgrade(entries: list[dict[str, Any]], home_of: Callable[[dict[str, Any]], str]) -> list[str]:
    """A version-1 profiles file, upgraded in place (f187): each profile with no AI tooling folder takes its current
    home folder (`home_of`, worked out as before f187) as its tooling folder, so its sessions keep starting exactly
    where they did. Client code stays off. Returns the names of the profiles changed."""
    changed: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("tooling") or "").strip():
            continue
        folders = [str(f) for f in entry.get("projects") or [] if str(f).strip()]
        if not folders and not entry.get("workspaces"):
            continue                       # f186: no folders - it starts in the assets folder, as before
        try:
            home = str(home_of(entry) or "").strip()
        except Exception:  # noqa: BLE001 - a home that cannot be worked out is the first folder
            home = folders[0] if folders else ""
        if not home:
            continue
        if not any(same(home, known) for known in folders):
            folders.append(home)
        entry["projects"] = folders
        entry["tooling"] = next(known for known in folders if same(known, home))
        changed.append(str(entry.get("name") or ""))
    return changed


def apply_topic_assets(entries: list[dict[str, Any]], shares: dict[str, str]) -> None:
    """In place, on every read (Brad, 2026-11-19: "each project session reads the client assets folder from the
    topic"): a profile with a topic takes the topic's assets folder (`shares`, topic id -> folder) as its Client Assets
    folder, or none when the topic has none. The folder joins its list first when it is not in it yet; where it sits
    after that is the person's choice. A profile with no topic keeps its own (`assets`, set in the editor)."""
    folded = {str(cid).lower(): str(folder) for cid, folder in (shares or {}).items() if folder}
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("client") or "").strip():
            continue
        share = folded.get(str(entry["client"]).lower(), "")
        folders = [str(f) for f in entry.get("projects") or [] if str(f).strip()]
        if share and not any(same(share, known) for known in folders):
            folders.insert(0, share)
        entry["projects"] = folders
        entry["assets"] = next((known for known in folders if same(known, share)), "") if share else ""


def replace_folder(entry: dict[str, Any], old: str, new: str) -> bool:
    """A topic's assets folder changed from `old` to `new` (either may be ""): `new` takes `old`'s place in this
    profile's list (first when `old` is not in it); a cleared one stays in the list with no role. True when changed."""
    folders = [str(f) for f in entry.get("projects") or [] if str(f).strip()]
    before = list(folders)
    if new:
        at = next((i for i, known in enumerate(folders) if old and same(known, old)), None)
        rest = [f for f in folders if not same(f, new)]
        if at is not None:
            spot = sum(1 for f in folders[:at] if not same(f, new))
            rest = [f for f in rest if not same(f, old)]
            rest.insert(spot, new)
        else:
            rest.insert(0, new)
        folders = rest
        if entry.get("tooling") and old and same(entry["tooling"], old):
            entry["tooling"] = new
        entry["client_code"] = [new if (old and same(c, old)) else c for c in entry.get("client_code") or []]
    entry["projects"] = folders
    entry["assets"] = new
    return folders != before


def start_note(folders: list[str | Path], assets: str, tooling: str, code: list[str]) -> str:
    """What Claude is told with the first prompt (f187): each folder of the session and its roles. "" when the session
    has no assets folder (a profile from before f187 with no topic folder)."""
    if not str(assets or "").strip():
        return ""
    entry = {"assets": assets, "tooling": tooling, "client_code": list(code or [])}
    words = {"assets": "Client assets (files that cross between you and the client - documents, designs, exports; "
                       "usually OneDrive)",
             "tooling": "AI tooling, your working folder (where you run; for non-client-facing tooling, operating "
                        "rules and scripts)",
             "code": "Client code (the client-facing solution - code and configuration delivered to the client)"}
    lines = ["Project session folders (from AI Cleaner) and their roles:"]
    seen: list[str] = []
    for folder in [*folders, assets, tooling]:
        text = str(folder or "").strip()
        if not text or any(same(text, known) for known in seen):
            continue
        seen.append(text)
        roles = roles_of(entry, text)
        said = "; ".join(words[r] for r in roles) if roles else "no role (reachable, not part of this client's solution)"
        lines.append(f"- {text} - {said}")
    if not str(tooling or "").strip():
        lines.append("There is no AI tooling folder: you run in the client assets folder.")
    return "\n".join(lines)
