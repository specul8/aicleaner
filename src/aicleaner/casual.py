"""Casual chats (plan item J, feedback #5; spec 2026-10-20-casual-chats-and-chat-now.md): a folder for chat
assets that belong to no project, and a "Casual chats" project session that lives in it.

The folder is usually in OneDrive or Google Drive, so what the conversations make is synced. It is
offered from what this machine has; choosing one creates it when missing and adds it to the allowed
folders when it is outside them (a system folder is refused as everywhere else).
"""
from __future__ import annotations

import os
import string
from pathlib import Path
from typing import Any

from .folders import blocked_reason

PROFILE = "Casual chats"
SUBFOLDER = "AI Chats"


class CasualError(ValueError):
    """A folder that cannot be the casual-chats folder, with a reason meant for the page."""


def _home() -> Path:
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    return Path(override) if override else Path.home()


def suggestions() -> list[dict[str, str]]:
    """OneDrive and Google Drive folders this machine has, each with an "AI Chats" folder proposed."""
    found: list[tuple[str, Path]] = []
    testing = bool(os.environ.get("AICLEANER_CLIENT_HOME", "").strip())
    if not testing:
        for var, label in (("OneDriveCommercial", "OneDrive (work)"), ("OneDriveConsumer", "OneDrive (personal)"),
                           ("OneDrive", "OneDrive")):
            value = os.environ.get(var, "").strip()
            if value:
                found.append((label, Path(value)))
        if os.name == "nt":
            for letter in string.ascii_uppercase[3:]:          # D: onwards; Google Drive mounts a letter
                drive = Path(f"{letter}:/My Drive")
                try:
                    if drive.is_dir():
                        found.append((f"Google Drive ({letter}:)", drive))
                except OSError:
                    continue
    for name, label in (("Google Drive", "Google Drive"), ("OneDrive", "OneDrive")):
        candidate = _home() / name
        if candidate.is_dir():
            found.append((label, candidate))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, root in found:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "path": str(root / SUBFOLDER)})
    return out


def prepare(folder: str) -> Path:
    """The folder made ready: an absolute path, not a system folder, created if missing."""
    text = os.path.expandvars(str(folder or "")).strip().strip('"')
    if not text:
        raise CasualError("choose a folder for casual chats")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise CasualError(f"{text} is not a full path (it should start with a drive or a folder root)")
    why = blocked_reason(path)
    if why:
        raise CasualError(f"{path} cannot hold casual chats: {why}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CasualError(f"{path} could not be created ({exc})") from exc
    return path


def inside(path: Path, roots: list[str]) -> bool:
    """True when the folder is one of the allowed roots or inside one."""
    target = os.path.normcase(os.path.abspath(path))
    for root in roots:
        base = os.path.normcase(os.path.abspath(os.path.expandvars(root)))
        if target == base or target.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def profile_entry(folder: Path) -> dict[str, Any]:
    """The Casual chats project session: no client, its one folder the casual-chats folder."""
    return {"name": PROFILE, "client": "", "projects": [str(folder)], "workspaces": [],
            "brief": "Casual chats: conversations that belong to no client or project. Files made here are "
                     "saved in this folder."}
