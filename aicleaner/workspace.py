"""Which repository is the primary one, and what else belongs with it (defect D41).

A workspace can hold many repositories - one engagement here has eighteen - and Claude Code runs
*from* one of them. That one carries the Claude configuration, the hooks and the settings. The
others are delivery repositories that get pushed to the client's own site and deliberately do not
carry any of that, by design and by policy.

aicleaner had no representation of this at all: every folder in a profile was the same kind of
thing. So the moment it wrote anything into a repository it could not know whether it was writing
into the one that keeps its tooling private or one that ships to the client.

**What this restricts, and what it does not.** Claude's own job on a large engagement is to write
and update files across *all* the client's repositories, and nothing here interferes with that.
The restriction is only on **aicleaner's own writes of Claude behaviour and settings files** - a
rules block, a hook, a settings merge. Those belong in the primary repo alone.

**How the primary repo is decided** (decided 2026-09-27), first answer wins:

1. `primary_repo` in the client file - the repo's **name**, not its path. The client file is
   shared through OneDrive and must not carry machine paths (`profiles.py` says so); a name is a
   fact about the engagement that is the same for every teammate.
2. The folder that *holds* a `.code-workspace` file. A multi-root workspace file conventionally
   lives in the repo the work is driven from.
3. The first folder that workspace file lists, where that folder is one of the profile's.

Nothing is guessed beyond that: with no workspace file and no setting, there is no primary repo
and the caller is told so rather than handed a folder that might be a delivery repo.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("aicleaner.workspace")

# VS Code's multi-root workspace file. Note the order of the words: `.code-workspace`. A glob
# written the other way round matches nothing at all, and does so silently.
WORKSPACE_GLOB = "*.code-workspace"

# A workspace file is JSON with comments and trailing commas allowed, which `json` is not.
_LINE_COMMENT = re.compile(r"(?m)(?<![:\w])//[^\n]*$")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _loads(text: str) -> dict:
    """Parse a workspace file. VS Code accepts comments and trailing commas here, so a file that
    has them is valid and must not be treated as damage."""
    stripped = _TRAILING_COMMA.sub(r"\1", _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text)))
    value = json.loads(stripped)
    return value if isinstance(value, dict) else {}


def workspace_file(folder: str | Path) -> Path | None:
    """The `.code-workspace` file directly in this folder, or None. The first by name, so two of
    them give a stable answer rather than whichever the filesystem happened to list first."""
    try:
        found = sorted(Path(folder).glob(WORKSPACE_GLOB))
    except OSError:
        return None
    return found[0] if found else None


def folders_in(path: str | Path) -> list[Path]:
    """The folders a workspace file lists, resolved against the file's own directory.

    Unreadable or malformed returns nothing: a workspace file is a convenience here, and failing
    to read one must not stop a profile working.
    """
    p = Path(path)
    try:
        data = _loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.debug("workspace file %s unreadable: %s", p, exc)
        return []
    out: list[Path] = []
    for entry in data.get("folders") or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("path", "")).strip()
        if not raw:
            continue
        try:
            resolved = (p.parent / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        except (OSError, ValueError, RuntimeError):
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def _same(left: str | Path, right: str | Path) -> bool:
    return str(left).replace("/", "\\").rstrip("\\").lower() == str(right).replace("/", "\\").rstrip("\\").lower()


def primary_of(folders: list[str], declared: str = "") -> dict:
    """Which of these folders is the primary repo, and why.

    Returns {"folder": str, "how": str, "workspace": str} - `folder` empty when nothing says.
    `how` is for the interface to show, because "this is your primary repo" is not a claim to
    make without saying on what grounds.
    """
    paths = [Path(f) for f in folders if str(f).strip()]
    if declared.strip():
        for path in paths:
            if path.name.lower() == declared.strip().lower():
                return {"folder": str(path), "how": f"named in the client file as {declared}",
                        "workspace": ""}
        return {"folder": "", "how": f"the client file names {declared}, which is not in this "
                                     f"profile's folders", "workspace": ""}
    holding = [path for path in paths if workspace_file(path) is not None]
    if len(holding) == 1:
        found = workspace_file(holding[0])
        return {"folder": str(holding[0]), "how": f"it holds {found.name}", "workspace": str(found)}
    for path in holding:      # more than one: the first listed in the first workspace file wins
        found = workspace_file(path)
        listed = folders_in(found) if found else []
        for candidate in listed:
            for known in paths:
                if _same(candidate, known):
                    return {"folder": str(known),
                            "how": f"first folder listed in {found.name}", "workspace": str(found)}
    return {"folder": "", "how": "no .code-workspace file and nothing named in the client file",
            "workspace": ""}


def tree_roots(folders: list[str], declared: str = "", allowed: list[Path] | None = None) -> list[str]:
    """The folders a session's tree shows (D72): the primary repo's workspace file, as VS Code
    opens it, then any of the profile's folders it does not list.

    Folders a workspace file lists are shown only when they exist and lie inside the allowed
    locations (`folders.roots`, resolved) - the rule a profile folder already obeys, so a
    workspace file cannot widen what the tree may show. With no primary repo, no workspace file,
    or one that cannot be read, the profile's folders come back as they are.
    """
    from .folders import is_usable

    base = [str(f) for f in folders if str(f).strip()]
    primary = primary_of(base, declared)
    listed: list[str] = []
    workspace = primary.get("workspace") or ""
    if not workspace and primary.get("folder"):
        found = workspace_file(primary["folder"])
        workspace = str(found) if found else ""
    if workspace:
        for path in folders_in(workspace):
            if path.is_dir() and allowed and is_usable(path, allowed):
                listed.append(str(path))
    if not listed:
        return base
    return listed + [f for f in base if not any(_same(f, l) for l in listed)]


CWD_KEY = "terminal.integrated.cwd"
# What may be written as the value: a `${workspaceFolder:<name>}` reference to a folder name.
_CWD_VALUE = re.compile(r"^\$\{workspaceFolder:[^\"\\\r\n{}]{1,120}\}$")


def cwd_offer(workspace: str | Path, primary: str | Path) -> dict:
    """What the workspace file's terminal working directory is, and what to offer when it has
    none (feature 71): the primary repo's folder, by the name the workspace gives it.

    {"current": value or "", "suggested": value or ""} - `suggested` is empty when there is
    already a setting, or when the file cannot be read or does not list the primary repo.
    """
    path = Path(workspace)
    try:
        data = _loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"current": "", "suggested": "", "choices": []}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    current = str(settings.get(CWD_KEY) or "")
    if current:
        return {"current": current, "suggested": "", "choices": []}
    # Every folder the file lists, by the name it gives it, except the file's own folder (D131):
    # the page offers them to choose from, the home's preselected.
    listed = cwd_choices(path)
    choices = [name for name, _folder in listed]
    for name, folder in listed:
        if _same(folder, Path(primary).resolve()):
            return {"current": "", "suggested": "${workspaceFolder:" + name + "}", "choices": choices}
    return {"current": "", "suggested": "", "choices": choices}


def container_of(workspace: str | Path) -> Path | None:
    """The workspace file's own folder when it holds other folders the file lists - a multi-repo
    parent, which is never a home (D131) - else None: a file kept inside a repo, listing it as "."
    beside "../web", is in its home."""
    path = Path(workspace)
    own = path.resolve().parent
    listed = [folder for _name, folder in workspace_folders(path) if not _same(folder, own)]
    inside = [f for f in listed if str(f).lower().startswith(str(own).lower().rstrip("\\/") + "\\")
              or str(f).lower().startswith(str(own).lower().rstrip("\\/") + "/")]
    return own if inside else None


def cwd_choices(workspace: str | Path) -> list[tuple[str, Path]]:
    """(name, folder) a workspace file's terminal cwd may point at: what it lists, by the name it
    gives each, without a multi-repo parent (D131) and without names the setting could not carry."""
    path = Path(workspace)
    parent = container_of(path)
    return [(name, folder) for name, folder in workspace_folders(path)
            if not (parent and _same(folder, parent)) and _CWD_VALUE.match("${workspaceFolder:" + name + "}")]


def add_cwd_setting(workspace: str | Path, value: str) -> None:
    """Add `terminal.integrated.cwd` to a workspace file, changing nothing else (feature 71).

    An insertion into the text, never a re-serialisation: the file may hold comments and
    trailing commas, and they are the person's. Into the `settings` object when there is one,
    otherwise a new `settings` object before the file's closing brace. Refused - and the file
    left alone - when it cannot be read, already has the setting, or the value is not a plain
    `${workspaceFolder:<name>}`.
    """
    if not _CWD_VALUE.match(value):
        raise ValueError(f"{value!r} is not a ${{workspaceFolder:<name>}} reference")
    path = Path(workspace)
    text = path.read_text(encoding="utf-8-sig")
    data = _loads(text)                                   # ValueError if it is not a workspace file
    settings = data.get("settings")
    if isinstance(settings, dict) and CWD_KEY in settings:
        raise ValueError(f"{path.name} already sets {CWD_KEY}")
    pair = f'"{CWD_KEY}": "{value}"'
    found = re.search(r'"settings"\s*:\s*\{', text)
    if found and isinstance(settings, dict):
        empty = not settings
        new = text[:found.end()] + f"\n    {pair}{'' if empty else ','}" + text[found.end():]
    else:
        end = text.rstrip().rfind("}")
        if end < 0:
            raise ValueError(f"{path.name} has no closing brace")
        body = text[:end].rstrip()
        comma = "" if body.endswith(("{", ",")) else ","
        new = body + comma + f'\n    "settings": {{\n        {pair}\n    }}\n' + text[end:]
    if _loads(new).get("settings", {}).get(CWD_KEY) != value:        # proved before it is written
        raise ValueError(f"could not add {CWD_KEY} to {path.name} safely")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)


def missing_from(folders: list[str]) -> list[dict]:
    """Folders a workspace file lists that are not in this profile's list yet (D41, part 3).

    One entry per workspace file that has any, so the page can say which file is asking.
    """
    known = [Path(f) for f in folders if str(f).strip()]
    out: list[dict] = []
    for folder in known:
        found = workspace_file(folder)
        if found is None:
            continue
        gaps = [str(listed) for listed in folders_in(found)
                if not any(_same(listed, k) for k in known)]
        if gaps:
            out.append({"workspace": found.name, "in": str(folder), "folders": gaps})
    return out


def missing_from_workspaces(workspaces: list[str], folders: list[str]) -> list[dict]:
    """Folders the profile's own workspace files list that are not in its folder list yet (D132).
    `missing_from` only reads a workspace file found inside a folder, the lookup from before D99."""
    known = [Path(f) for f in folders if str(f).strip()]
    out: list[dict] = []
    for ws in [w for w in workspaces if str(w).strip()]:
        gaps = [str(folder) for _name, folder in workspace_folders(ws)
                if not any(_same(folder, k) for k in known)]
        if gaps:
            out.append({"workspace": Path(ws).name, "in": str(ws), "folders": gaps})
    return out


def may_write_agent_files(folder: str | Path, primary: str) -> str:
    """"" when aicleaner may write Claude behaviour or settings files here, else why not.

    Only the primary repo. The delivery repos deliberately do not carry that tooling, and writing
    it into one would push it to the client - which is the arrangement this product exists to
    protect. Claude's own edits to project files are not this and are not restricted.
    """
    if not primary:
        return ("no primary repo is known for this client, so there is nowhere safe to write "
                "agent settings. Name one in the client file, or open the workspace that says.")
    if _same(folder, primary):
        return ""
    return (f"{Path(folder).name} is not the primary repo ({Path(primary).name}). Agent settings "
            f"go in the primary repo only: a delivery repo is pushed to the client, and this "
            f"tooling is deliberately not part of what they receive.")


# ---- workspace files and the home folder, per profile (D99) -------------------------------------
#
# A profile holds workspace files and folders, on this machine only. The home folder - where a
# session runs and where aicleaner's agent writes go - is derived from the first workspace file,
# and can be overridden. It replaces the shared `primary_repo` (a repo path is a fact about one
# device, so there was nothing in it to share).

def _resolve(base: Path, raw: str) -> Path | None:
    try:
        return Path(raw).resolve() if Path(raw).is_absolute() else (base / raw).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def workspace_folders(path: str | Path) -> list[tuple[str, Path]]:
    """(name, folder) for every folder a workspace file lists, the name as the file spells it (or
    the folder's own name). Unreadable or malformed returns nothing."""
    p = Path(path)
    try:
        data = _loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.debug("workspace file %s unreadable: %s", p, exc)
        return []
    out: list[tuple[str, Path]] = []
    for entry in data.get("folders") or []:
        raw = str(entry.get("path", "")).strip() if isinstance(entry, dict) else ""
        resolved = _resolve(p.parent, raw) if raw else None
        if resolved is None or any(_same(resolved, known) for _n, known in out):
            continue
        out.append((str(entry.get("name") or "").strip() or resolved.name, resolved))
    return out


def _cwd_of(path: Path) -> str:
    try:
        settings = _loads(path.read_text(encoding="utf-8-sig")).get("settings")
    except (OSError, ValueError):
        return ""
    return str(settings.get(CWD_KEY) or "").strip() if isinstance(settings, dict) else ""


_FOLDER_REF = re.compile(r"^\$\{workspaceFolder:(?P<name>.+)\}$")


def derived_home(workspaces: list[str], projects: list[str]) -> dict:
    """{"folder", "how"}: the first workspace file's terminal.integrated.cwd - a
    `${workspaceFolder:<name>}` matched against that file's folder names exactly as spelled, or a
    path relative to the file - else that file's first folder; with no workspace file, the first
    manual folder. `folder` is "" when nothing says."""
    for ws in [w for w in workspaces if str(w).strip()]:
        path = Path(ws)
        # A multi-repo parent is never the home (D131): the workspace file sits in the folder that
        # holds its repos, and often lists that folder (".") first or points its cwd at it.
        parent = container_of(path)
        folders = [(name, folder) for name, folder in workspace_folders(path)
                   if not (parent and _same(folder, parent))]
        cwd = _cwd_of(path)
        if cwd:
            ref = _FOLDER_REF.match(cwd)
            if ref:
                for name, folder in folders:
                    if name == ref.group("name"):
                        return {"folder": str(folder), "how": f"terminal.integrated.cwd in {path.name}"}
            elif "${" not in cwd:
                resolved = _resolve(path.parent, cwd)
                if resolved is not None and not (parent and _same(resolved, parent)):
                    return {"folder": str(resolved), "how": f"terminal.integrated.cwd in {path.name}"}
        if folders:
            return {"folder": str(folders[0][1]), "how": f"first folder in {path.name}"}
        break                                   # only the first workspace file decides
    first = next((str(p) for p in projects if str(p).strip()), "")
    return {"folder": first, "how": "first folder of the profile" if first else "no folders yet"}


def home_of(workspaces: list[str], projects: list[str], override: str = "") -> dict:
    """{"folder", "how", "derived", "overridden"}: the user's override when it still exists, else
    the derived home. Reset is clearing the override - the derived value is worked out afresh each
    time, so it is always the one to go back to."""
    derived = derived_home(workspaces, projects)
    if override.strip():
        if Path(override).is_dir():
            return {"folder": str(Path(override)), "how": "set by you", "derived": derived["folder"],
                    "overridden": True}
        return {**derived, "how": f"{derived['how']} (your choice {override} no longer exists)",
                "derived": derived["folder"], "overridden": False}
    return {**derived, "derived": derived["folder"], "overridden": False}


def profile_tree(workspaces: list[str], projects: list[str], allowed: list[Path] | None = None) -> list[str]:
    """Every folder of every workspace file that exists and lies inside the allowed roots, then
    the manual folders - in order, each once."""
    from .folders import is_usable

    out: list[str] = []

    def add(folder: str | Path) -> None:
        if not any(_same(folder, known) for known in out):
            out.append(str(folder))

    for ws in [w for w in workspaces if str(w).strip()]:
        for _name, folder in workspace_folders(ws):
            if folder.is_dir() and allowed and is_usable(folder, allowed):
                add(folder)
    for folder in projects:
        if str(folder).strip():
            add(folder)
    return out


def implicit_workspaces(projects: list[str]) -> list[str]:
    """For a profile saved before D99 (no workspace files of its own): a workspace file found
    directly inside one of its folders, as the old lookup found it - so nothing changes for it."""
    for folder in projects:
        found = workspace_file(folder) if str(folder).strip() else None
        if found is not None:
            return [str(found)]
    return []


def new_workspace(profile: str, home: str, folders: list[str]) -> tuple[Path, str]:
    """(where, text) for a new `<profile>.code-workspace` in the home folder's parent, listing the
    folders (relative to it where they can be) with the cwd set to home. Shown before written."""
    parent = Path(home).parent
    entries = []
    home_name = ""
    for folder in [home] + [f for f in folders if not _same(f, home)]:
        path = Path(folder)
        try:
            rel = path.resolve().relative_to(parent.resolve()).as_posix()
        except ValueError:
            rel = str(path)
        entries.append({"path": rel})
        if _same(folder, home):
            home_name = path.name
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", profile).strip(" .") or "workspace"
    doc = {"folders": entries, "settings": {CWD_KEY: "${workspaceFolder:" + home_name + "}"}}
    return parent / f"{safe}.code-workspace", json.dumps(doc, indent=2) + "\n"


def write_new_workspace(path: str | Path, text: str) -> None:
    """Write a workspace file that does not exist yet - never over one."""
    p = Path(path)
    if p.exists():
        raise ValueError(f"{p.name} already exists; it is not replaced")
    _loads(text)                                 # proved readable before it is written
    p.write_text(text, encoding="utf-8")
