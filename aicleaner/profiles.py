"""Session profiles: the engagements this machine can open a session for.

A profile is a named way of working - which client, which folders, which model, what standing
brief - and it is what the session picker, the permission cards and the evidence log all say.

The file lives beside Global in the app folder, not in the clients folder. Client files are shared
through OneDrive; a profile names folders on *this* machine, and a teammate's C:/Repos/acme is not
a path on your disk. Global lives there for the same reason.

Writes go through validate-then-commit, as client files do: a half-written profiles.json would
take the session picker with it, and it is edited from a phone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import fileio

PROFILES_FILE = "profiles.json"

# Enough to be a name a person can say, short enough to fit a session card.
MAX_NAME = 40


class ProfileError(ValueError):
    """A profile that cannot be saved, with a message meant for the person editing it."""


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    """One profile, with every field the rest of the code expects present and of the right type."""
    if not isinstance(entry, dict):
        raise ProfileError("each profile must be an object")
    name = str(entry.get("name", "")).strip()
    if not name:
        raise ProfileError("every profile needs a name")
    if len(name) > MAX_NAME:
        raise ProfileError(f"{name[:MAX_NAME]}...: a profile name is at most {MAX_NAME} characters")
    if "/" in name or "\\" in name:
        # The name travels in the session's own URL, and is a key everywhere else.
        raise ProfileError(f"{name}: a profile name cannot contain a slash")
    if any(c in name for c in "?#%") or name.strip(".") == "":
        # Each of these changes what the session's address means (H31).
        raise ProfileError(f"{name}: a profile name cannot contain ? # or %, or be only dots")
    folders = [os.path.expandvars(str(p)).strip() for p in (entry.get("projects") or [])]
    # Workspace files, in order - the first sets the home folder (D99). All of this is about this
    # machine's disk, which is why it lives here and not in the shared client file.
    workspaces = [os.path.expandvars(str(p)).strip() for p in (entry.get("workspaces") or [])]
    workspaces = [w for w in workspaces if w]
    for ws in workspaces:
        if not ws.lower().endswith(".code-workspace"):
            raise ProfileError(f"{name}: {ws} is not a .code-workspace file")
    # Worktrees of this profile's repos, each a session of its own (DECISIONS 66). Written only by
    # the server (create and remove), carried over on an editor save; omitted when there are none.
    worktrees = [{"name": str(w.get("name", "")).strip(), "repo": str(w.get("repo", "")).strip(),
                  "path": str(w.get("path", "")).strip()}
                 for w in (entry.get("worktrees") or []) if isinstance(w, dict)]
    worktrees = [w for w in worktrees if w["name"] and w["repo"] and w["path"]]
    return {
        **({"worktrees": worktrees} if worktrees else {}),
        "name": name,
        "client": str(entry.get("client", "")).strip(),
        "projects": [f for f in folders if f],
        "workspaces": workspaces,
        # A home folder the user chose over the derived one, and what the derived one was then;
        # empty = derived (D99). Reset is clearing the override.
        "home_override": os.path.expandvars(str(entry.get("home_override") or "")).strip(),
        "home_derived_was": str(entry.get("home_derived_was") or "").strip(),
        # Folders that were this profile's home before it moved (DECISIONS 64): conversations stay
        # in the folder they began in, so Continue keeps listing these.
        "homes_before": [str(h).strip() for h in (entry.get("homes_before") or []) if str(h).strip()],
        "model": str(entry.get("model", "")).strip(),
        "brief": str(entry.get("brief", "")),
    }


def validate(entries: list[dict[str, Any]], known_clients: set[str] | None = None) -> list[dict[str, Any]]:
    """The profiles as they will be stored, or ProfileError saying what is wrong with them.

    `known_clients` is the client ids the mapping currently holds. A profile naming one that is not
    there is refused here - at the point of editing, where it can be fixed - rather than at start,
    where it would look like the session is broken.
    """
    cleaned: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for entry in entries or []:
        profile = _clean(entry)
        lowered = profile["name"].lower()
        if lowered in seen:
            raise ProfileError(f"two profiles are called {profile['name']}; names are how sessions "
                               "are told apart")
        seen[lowered] = profile["name"]
        if profile["client"] and known_clients is not None and profile["client"] not in known_clients:
            raise ProfileError(f"{profile['name']} names client {profile['client']}, which is not in "
                               "the mapping; add the client first or leave it blank")
        cleaned.append(profile)
    return cleaned


class ProfileStore:
    """Reads and writes profiles.json, and says what is wrong rather than throwing it away."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> tuple[list[dict[str, Any]], str]:
        """(profiles, problem). A file that cannot be read gives no profiles and a message: the
        session page then says what to fix instead of showing an empty picker."""
        if not self.path.is_file():
            return [], ""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [], f"{self.path.name}: {exc}"
        entries = raw.get("profiles") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return [], f"{self.path.name}: expected a list of profiles"
        try:
            return validate(entries), ""
        except ProfileError as exc:
            return [], f"{self.path.name}: {exc}"

    def save(self, entries: list[dict[str, Any]], known_clients: set[str] | None = None) -> list[dict[str, Any]]:
        """Validate, then commit. Nothing is written unless every profile is storable."""
        cleaned = validate(entries, known_clients)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "profiles": cleaned}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        fileio.replace(tmp, self.path)
        return cleaned

    def bootstrap(self, from_config: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Write the config's profiles out the first time, so an existing install keeps working.

        Only when there is no file yet: after that the file is the truth, because it is what the UI
        edits. A config that cannot be written (a read-only app folder) is used in memory instead.
        """
        if self.exists() or not from_config:
            return self.load()[0]
        try:
            return self.save(from_config)
        except (ProfileError, OSError):
            try:
                return validate(from_config)
            except ProfileError:
                return []
