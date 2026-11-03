"""Each client's OneDrive share folder: how files cross between this machine and the one driving it.

A session runs here and is driven from somewhere else - a laptop, a phone. OneDrive already syncs
between the two, so a folder in it is the channel, one per client so that one client's files never
sit beside another's.

Stored in client-shares.json beside Global, never in the client file. A client file is client data,
the same for everyone on the engagement; where your OneDrive keeps your copy of a folder is a fact
about you and this machine, and a teammate's path would be wrong on your disk.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import fileio

SHARES_FILE = "client-shares.json"

# A work machine's business OneDrive first: that is where client files belong.
ONEDRIVE_VARIABLES = ("OneDriveCommercial", "OneDrive", "OneDriveConsumer")

# Characters Windows will not have in a folder name, plus the ones that make a path ambiguous.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ShareError(ValueError):
    """A share that cannot be set, with a message meant for the person setting it."""


def onedrive_root() -> Path | None:
    """The OneDrive folder on this machine, or None when OneDrive is not set up."""
    for name in ONEDRIVE_VARIABLES:
        value = os.environ.get(name, "").strip()
        if value and Path(value).is_dir():
            return Path(value)
    return None


def in_onedrive(path: str | Path) -> bool:
    """Whether a folder is inside one of this user's OneDrive folders (DECISIONS 51): the clients
    location should be, so each client's folder can be shared. Compared as whole path segments, so
    `OneDrive-old` is not inside `OneDrive`. Warned about when false, never refused."""
    target = Path(os.path.abspath(os.path.expandvars(str(path)))).expanduser()
    for name in ONEDRIVE_VARIABLES:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        root = Path(os.path.abspath(value))
        try:
            target.relative_to(root)
            return True
        except ValueError:
            # Windows paths compare case-insensitively.
            if os.name == "nt" and str(target).lower().startswith(str(root).lower().rstrip("\\") + "\\"):
                return True
    return False


def onedrive_web(folder: str | Path) -> dict[str, str]:
    """Where a folder in this user's OneDrive opens on the web, for its Share button (DECISIONS 67).

    A work account records its web address (`UserUrl`) and its folder (`UserFolder`) in the
    registry, so the folder's own page is worked out with no sign-in: the account's
    `_layouts/15/onedrive.aspx?id=` plus the folder's server path. A personal account records no
    address the folder's page can be built from - that needs Microsoft's API - so it opens OneDrive
    on the web, and the page shows the folder's path to find. {"url", "exact", "relative"}; an empty
    url when the folder is not in OneDrive."""
    import urllib.parse

    target = Path(os.path.abspath(str(folder)))
    for account in _onedrive_accounts():
        root = Path(os.path.abspath(account.get("UserFolder", "")))
        try:
            relative = target.relative_to(root)
        except ValueError:
            if os.name == "nt" and str(target).lower().startswith(str(root).lower().rstrip("\\") + "\\"):
                relative = Path(str(target)[len(str(root)):].lstrip("\\"))
            else:
                continue
        rel = relative.as_posix()
        user_url = account.get("UserUrl", "").rstrip("/")
        if user_url:
            server = urllib.parse.urlsplit(user_url).path.rstrip("/") + "/Documents/" + rel
            return {"url": f"{user_url}/_layouts/15/onedrive.aspx?id={urllib.parse.quote(server)}",
                    "exact": "1", "relative": rel}
        return {"url": "https://onedrive.live.com/", "exact": "", "relative": rel}
    return {"url": "", "exact": "", "relative": ""}


def _onedrive_accounts() -> list[dict[str, str]]:
    """Each signed-in OneDrive account's UserFolder and UserUrl, from the registry (Windows only)."""
    if os.name != "nt":
        return []
    import winreg

    found: list[dict[str, str]] = []
    try:
        accounts = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts")
    except OSError:
        return []
    with accounts:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(accounts, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(accounts, name) as key:
                    values = {}
                    for field in ("UserFolder", "UserUrl"):
                        try:
                            values[field] = str(winreg.QueryValueEx(key, field)[0])
                        except OSError:
                            values[field] = ""
            except OSError:
                continue
            if values["UserFolder"]:
                found.append(values)
    return found


def folder_name(label: str) -> str:
    """A client's label as a folder name: the characters Windows refuses become a dash."""
    cleaned = _UNSAFE.sub("-", label).strip(" .")
    if not cleaned:
        raise ShareError("that client has no name that can be used for a folder")
    return cleaned


def check(path: str) -> Path:
    """The folder as it will be stored, or ShareError saying what is wrong with it."""
    text = os.path.expandvars(str(path or "")).strip().strip('"')
    if not text:
        raise ShareError("give a folder, or use Create")
    # Judged on the text, before resolve() reaches out to the host (H27). The OneDrive folder is a
    # local path; a share given as \\server\... would also be read by every session.
    if fileio.network_path(text):
        raise ShareError("a network path cannot be a share folder; use the OneDrive folder on this machine")
    folder = Path(text).expanduser()
    if not folder.is_absolute():
        raise ShareError(f"{text} is not a full path; give one that starts with a drive or a share")
    folder = folder.resolve()
    if folder == Path(folder.anchor):
        # Not a trust boundary: a share of C:\ is always a typo, and it would hand the whole disk
        # to the file tree and to the session.
        raise ShareError(f"{folder} is a whole drive; pick the client's own folder")
    if not folder.exists():
        raise ShareError(f"{folder} does not exist; make it first, or use Create")
    if not folder.is_dir():
        raise ShareError(f"{folder} is a file, not a folder")
    return folder


class ShareStore:
    """Reads and writes client-shares.json. A client with no entry has no share."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, str]:
        """Client id -> folder. An unreadable file reads as no shares: the pages say none is set,
        which is true enough to act on, rather than failing to draw."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        shares = raw.get("shares") if isinstance(raw, dict) else None
        if not isinstance(shares, dict):
            return {}
        return {str(cid): str(folder) for cid, folder in shares.items() if cid and folder}

    def get(self, client: str) -> str:
        if not client:
            return ""
        wanted = client.lower()
        return next((folder for cid, folder in self.load().items() if cid.lower() == wanted), "")

    def _write(self, shares: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "shares": dict(sorted(shares.items()))}, indent=2,
                                  ensure_ascii=False) + "\n", encoding="utf-8")
        fileio.replace(tmp, self.path)

    def set(self, client: str, path: str) -> str:
        """Point a client at an existing folder. Nothing is written unless the folder checks out."""
        if not client:
            raise ShareError("say which client")
        folder = str(check(path))
        shares = {cid: f for cid, f in self.load().items() if cid.lower() != client.lower()}
        shares[client] = folder
        self._write(shares)
        return folder

    def create(self, client: str, label: str, root: Path | None = None) -> str:
        """Make <OneDrive>\\<label> (or reuse it) and point the client at it."""
        base = root or onedrive_root()
        if base is None:
            raise ShareError("OneDrive is not set up on this machine (no OneDrive folder was found); "
                             "give a folder instead")
        folder = base / folder_name(label or client)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as problem:
            raise ShareError(f"{folder} could not be created: {problem}") from problem
        return self.set(client, str(folder))

    def clear(self, client: str) -> None:
        """Forget the setting. The folder and everything in it are left exactly where they are."""
        shares = {cid: f for cid, f in self.load().items() if cid.lower() != client.lower()}
        self._write(shares)
