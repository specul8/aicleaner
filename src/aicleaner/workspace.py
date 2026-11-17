"""Which repository is the primary one, and what else belongs with it (defect D41).

A workspace can hold many repositories - one engagement here has eighteen - and Claude Code runs
*from* one of them. That one carries the Claude configuration, the hooks and the settings. The
others are delivery repositories that get pushed to the client's own site and deliberately do not
carry any of that, by design and by policy.

AI Cleaner had no representation of this at all: every folder in a profile was the same kind of
thing. So the moment it wrote anything into a repository it could not know whether it was writing
into the one that keeps its tooling private or one that ships to the client.

**What this restricts, and what it does not.** Claude's own job on a large engagement is to write
and update files across *all* the client's repositories, and nothing here interferes with that.
The restriction is only on **AI Cleaner's own writes of Claude behaviour and settings files** - a
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
                return {"folder": str(path), "how": f"named in the topic file as {declared}",
                        "workspace": ""}
        return {"folder": "", "how": f"the topic file names {declared}, which is not in this "
                                     f"project session's folders", "workspace": ""}
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
    return {"folder": "", "how": "no .code-workspace file and nothing named in the topic file",
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
    """"" when AI Cleaner may write Claude behaviour or settings files here, else why not.

    Only the primary repo. The delivery repos deliberately do not carry that tooling, and writing
    it into one would push it to the client - which is the arrangement this product exists to
    protect. Claude's own edits to project files are not this and are not restricted.
    """
    if not primary:
        return ("no primary repo is known for this topic, so there is nowhere safe to write "
                "agent settings. Name one in the topic file, or open the workspace that says.")
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
    return {"folder": first, "how": "first folder of the project session" if first else "no folders yet"}


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


def _usable(folder: str | Path, allowed: list[Path]) -> bool:
    """is_usable, False for a path that cannot even be read as one."""
    from .folders import FolderError, is_usable

    try:
        return is_usable(folder, allowed)
    except (FolderError, OSError, ValueError):
        return False


def profile_tree(workspaces: list[str], projects: list[str], allowed: list[Path] | None = None,
                 home: str = "") -> list[str]:
    """The home folder (f181), then every folder of every workspace file that exists and lies inside
    the allowed roots, in each file's order, then the manual folders in their saved order (those too
    inside the roots when any are set, D245) - each once. The home leads only when it is shown at
    all: one of these, or a folder inside the allowed roots."""
    from .folders import is_usable

    out: list[str] = []

    def add(folder: str | Path) -> None:
        if not any(_same(folder, known) for known in out):
            out.append(str(folder))

    for ws in [w for w in workspaces if str(w).strip()]:
        for _name, folder in workspace_folders(ws):
            if folder.is_dir() and allowed and is_usable(folder, allowed):
                add(folder)
    # D245: the profile's own folders were checked when saved, but the allowed folders can narrow since, and a
    # tree root is a folder the session reaches - so they are held to the roots here too (when any are set).
    for folder in projects:
        if str(folder).strip() and (not allowed or _usable(folder, allowed)):
            add(folder)
    if str(home).strip():
        shown = next((known for known in out if _same(known, home)), "")
        if shown:
            out.remove(shown)
            out.insert(0, shown)
        elif Path(home).is_dir() and allowed and is_usable(home, allowed):
            out.insert(0, str(home))
    return out


def editor_order(workspaces: list[str], projects: list[str], home: str = "") -> list[str]:
    """The order the profile editor lists its folders in (f181): the tree's order, without the
    allowed-roots check (every folder named here was checked when it was saved)."""
    out: list[str] = []

    def add(folder: str | Path) -> None:
        if str(folder).strip() and not any(_same(folder, known) for known in out):
            out.append(str(folder))

    add(home)
    for ws in [w for w in workspaces if str(w).strip()]:
        for _name, folder in workspace_folders(ws):
            add(folder)
    for folder in projects:
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


# ---- the home folder leads the workspace file (f181) --------------------------------------------
#
# Brad, 2026-11-15: the home folder is always the workspace file's first folder and its terminal
# working directory, and the folders are in the order he sets. The file is JSON with comments
# (JSONC) and it is the person's own, so it is never re-serialised: its folder entries are moved as
# pieces of text, each with its own comments, and the one setting is replaced in place.

class WorkspaceRewriteError(ValueError):
    """A workspace file that was left alone, with why, for the person to read."""


BACKUPS_KEPT = 10                    # per workspace file, in AI Cleaner's state folder


def stamp_of(raw: bytes) -> str:
    """What the file held when it was read, short: compared before it is written (changed on disk)."""
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:16]


def file_stamp(path: str | Path) -> str:
    try:
        return stamp_of(Path(path).read_bytes())
    except OSError:
        return ""


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """The significant tokens of a JSONC text, (kind, start, end): kinds `{ } [ ] , :`, `str` and
    `lit`. Whitespace and comments are skipped - they stay in the text between the tokens."""
    out: list[tuple[str, int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n" or c == "﻿":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise ValueError("a comment is not closed")
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ValueError("a string is not closed")
            out.append(("str", i, j + 1))
            i = j + 1
        elif c in "{}[],:":
            out.append((c, i, i + 1))
            i += 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}[],:"/':
                j += 1
            if j == i:
                raise ValueError(f"unexpected {c!r}")
            out.append(("lit", i, j))
            i = j
    return out


def _close(toks: list, i: int) -> int:
    depth = 0
    for k in range(i, len(toks)):
        if toks[k][0] in "{[":
            depth += 1
        elif toks[k][0] in "}]":
            depth -= 1
            if depth == 0:
                return k
    raise ValueError("a bracket is not closed")


def _value_end(toks: list, k: int) -> int:
    return _close(toks, k) if toks[k][0] in "{[" else k


def _members(text: str, toks: list, i: int) -> dict[str, tuple[int, int, int]]:
    """An object's own members: key -> (key token, value's first token, value's last token)."""
    end, k, out = _close(toks, i), i + 1, {}
    while k < end:
        if toks[k][0] == ",":
            k += 1
            continue
        if toks[k][0] != "str" or k + 2 > end or toks[k + 1][0] != ":":
            raise ValueError("not a JSON object")
        last = _value_end(toks, k + 2)
        out.setdefault(json.loads(text[toks[k][1]:toks[k][2]]), (k, k + 2, last))
        k = last + 1
    return out


_SAME_LINE_NOTE = re.compile(r"[ \t]*(//[^\r\n]*|/\*.*?\*/)", re.S)


def _array_pieces(text: str, toks: list, i: int) -> dict:
    """The folders array as pieces: each element's leading text (whitespace and comments above it),
    its body, and a note on its own line after it; the text before `]`, and whether the last element
    has a trailing comma."""
    end = _close(toks, i)
    items, k = [], i + 1
    while k < end:
        if toks[k][0] == ",":
            k += 1
            continue
        last = _value_end(toks, k)
        items.append((k, last))
        k = last + 1
    leads: list[str] = []
    bodies: list[str] = []
    tails: list[str] = []
    at = toks[i][2]
    trailing = False
    for index, (first, last) in enumerate(items):
        lead = text[at:toks[first][1]]
        if index:
            note = _SAME_LINE_NOTE.match(lead)
            if note:
                tails[-1] += lead[:note.end()]
                lead = lead[note.end():]
        leads.append(lead)
        bodies.append(text[toks[first][1]:toks[last][2]])
        tails.append("")
        if last + 1 < end and toks[last + 1][0] == ",":
            at = toks[last + 1][2]
            trailing = index == len(items) - 1
        else:
            at = toks[last][2]
    closing = text[at:toks[end][1]]
    if items:
        note = _SAME_LINE_NOTE.match(closing)
        if note:
            tails[-1] += closing[:note.end()]
            closing = closing[note.end():]
    return {"leads": leads, "bodies": bodies, "tails": tails, "closing": closing,
            "trailing": trailing, "start": toks[i][1], "end": toks[end][2]}


def _new_body(template: str, rel: str, newline: str) -> str:
    """A new folder entry written as the file writes its first one: on several lines, or on one."""
    if "\n" in template:
        inner = re.search(r"\n([ \t]*)\"", template)
        outer = re.search(r"\n([ \t]*)\}\s*$", template)
        return ("{" + newline + (inner.group(1) if inner else "  ") + '"path": ' + json.dumps(rel)
                + newline + (outer.group(1) if outer else "") + "}")
    return '{ "path": ' + json.dumps(rel) + " }"


def unique_folders(folders: list[str]) -> list[str]:
    """The folders in order, each once, blanks dropped."""
    out: list[str] = []
    for folder in folders:
        if str(folder).strip() and not any(_same(folder, known) for known in out):
            out.append(str(folder).strip())
    return out


def plan_order(entries: list[Path | None], folders: list[str]) -> list:
    """The folders array's new content (f181, Brad 2026-11-15 19:35: the file's folder list is
    exactly the editor's list): an existing element by its index - so its own text, name, comments
    and relative path go with it - or a new folder by its path. A folder the list no longer has is
    left out; an element that is not a folder entry at all is kept, after the folders."""
    def find(folder: str) -> int | None:
        return next((i for i, p in enumerate(entries) if p is not None and _same(p, folder)), None)

    final: list = []
    for folder in unique_folders(folders):
        index = find(folder)
        final.append(index if index is not None else folder)
    return final + [i for i, p in enumerate(entries) if p is None]


def reorder_within(full: list[str], shown: list[str]) -> list[str]:
    """`full` with the folders that are in `shown` put in `shown`'s order, each in a place one of
    them held; the rest (a folder the tree does not show - not on this disk, say) keep their places.
    The first of `shown` then leads, since first place is the home (f181)."""
    full = unique_folders(full)
    shown = [f for f in unique_folders(shown) if any(_same(f, k) for k in full)]
    queue = list(shown)
    out = [queue.pop(0) if any(_same(f, s) for s in shown) else f for f in full]
    if shown:
        out = [shown[0]] + [f for f in out if not _same(f, shown[0])]
    return out


def _cwd_value(old: str, home_name: str, home: Path, file_dir: Path) -> str | None:
    """The terminal working directory, pointing at the home in the form the file already uses; None
    when the file's value is some other variable, which then decides nothing (D99) and stays."""
    import os

    if _FOLDER_REF.match(old):
        value = "${workspaceFolder:" + home_name + "}"
        if not _CWD_VALUE.match(value):
            raise WorkspaceRewriteError(f"the folder name {home_name!r} cannot be written as the terminal's working directory")
        return value
    if "${" in old:
        return None
    if Path(old).is_absolute():
        return str(home)
    try:
        return Path(os.path.relpath(home, file_dir)).as_posix()
    except ValueError:
        return str(home)


def _holds_repos(file_dir: Path, entries: list[Path | None]) -> bool:
    """The file's own folder holds folders the file lists (D131): a multi-repo parent."""
    own = str(file_dir.resolve()).lower().rstrip("\\/")
    return any(p is not None and str(p).lower().startswith(own + ("\\" if "\\" in own else "/"))
               for p in entries)


def lead_text(text: str, file_dir: Path, folders: list[str]) -> str:
    """The workspace file's text with exactly these folders, in this order - the first is the home,
    and the terminal working directory points at it when the file sets one - and nothing else
    changed. No writing. WorkspaceRewriteError when it cannot be done safely."""
    import os

    folders = unique_folders(folders)
    if not folders:
        raise WorkspaceRewriteError("a workspace file needs at least one folder")
    home = folders[0]

    try:
        toks = _tokens(text)
        if not toks or toks[0][0] != "{":
            raise ValueError("it is not a JSON object")
        top = _members(text, toks, 0)
    except ValueError as exc:
        raise WorkspaceRewriteError(f"it could not be read ({exc})") from exc
    if "folders" not in top or toks[top["folders"][1]][0] != "[":
        raise WorkspaceRewriteError("it has no folders list")
    pieces = _array_pieces(text, toks, top["folders"][1])
    entries: list[Path | None] = []
    names: list[str] = []
    relative = not pieces["bodies"]
    for body in pieces["bodies"]:
        try:
            value = _loads(body) if body.lstrip().startswith("{") else {}
        except ValueError:
            value = {}
        raw = str(value.get("path", "")).strip()
        entries.append(_resolve(file_dir, raw) if raw else None)
        names.append(str(value.get("name") or "").strip())
        if raw and not Path(raw).is_absolute():
            relative = True
    if _same(Path(home).resolve(), file_dir.resolve()) and _holds_repos(file_dir, entries):
        raise WorkspaceRewriteError("the home folder is the folder that holds the workspace's repos, "
                                    "which is never a home (D131)")
    final = plan_order(entries, folders)
    newline = "\r\n" if "\r\n" in text else "\n"
    leads = pieces["leads"]
    found_lead = re.search(r"\r?\n[ \t]*$", leads[1] if len(leads) > 1 else (leads[0] if leads else ""))
    default_lead = found_lead.group(0) if found_lead else (newline + "    ")
    template = pieces["bodies"][0] if pieces["bodies"] else ""

    def path_text(folder: str) -> str:
        if relative:
            try:
                return Path(os.path.relpath(Path(folder), file_dir)).as_posix()
            except ValueError:                   # another drive: it can only be written in full
                pass
        return str(Path(folder))

    parts = ["["]
    for k, item in enumerate(final):
        if isinstance(item, int):
            parts.append(leads[item] + pieces["bodies"][item])
            tail = pieces["tails"][item]
        else:
            parts.append(default_lead + _new_body(template, path_text(item), newline))
            tail = ""
        if k < len(final) - 1 or pieces["trailing"]:
            parts.append(",")
        parts.append(tail)
    parts.append(pieces["closing"] + "]")
    new = text[:pieces["start"]] + "".join(parts) + text[pieces["end"]:]
    # The terminal's working directory, in place, when the file sets one (D99 reads it before the
    # first folder). With none, the first folder decides - which is now the home.
    if "settings" in top and toks[top["settings"][1]][0] == "{":
        toks2 = _tokens(new)
        inner = _members(new, toks2, _members(new, toks2, 0)["settings"][1])
        if CWD_KEY in inner and toks2[inner[CWD_KEY][1]][0] == "str":
            value_tok = toks2[inner[CWD_KEY][1]]
            old = str(json.loads(new[value_tok[1]:value_tok[2]]))
            home_index = final[0] if isinstance(final[0], int) else None
            home_name = (names[home_index] if home_index is not None else "") or Path(home).resolve().name
            value = _cwd_value(old, home_name, Path(home), file_dir)
            if value is not None and value != old:
                new = new[:value_tok[1]] + json.dumps(value) + new[value_tok[2]:]
    return new


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _backup(path: Path, raw: bytes, backup_dir: Path) -> Path:
    """A copy of the file as it was, in AI Cleaner's state folder - never beside it, where a repo
    would pick it up. The newest BACKUPS_KEPT per file are kept."""
    import time

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{path.stem}.{stamp}{path.suffix}"
    n = 1
    while target.exists():
        n += 1
        target = backup_dir / f"{path.stem}.{stamp}-{n}{path.suffix}"
    target.write_bytes(raw)
    older = sorted(backup_dir.glob(f"{glob_escape(path.stem)}.*{path.suffix}"), key=lambda f: f.name)
    for stale in older[:-BACKUPS_KEPT]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def lead_with(path: str | Path, folders: list[str], *, expect: str = "",
              backup_dir: str | Path | None = None, allowed: list[Path] | None = None) -> dict:
    """Make this workspace file list exactly these folders, the first (the home) leading (f181).

    {"changed": bool, "backup": path or ""}. Refused - WorkspaceRewriteError, the file untouched -
    when it is outside the allowed folders, read-only, not readable as a workspace file, or changed
    on disk since it was read (`expect`, the stamp the page was given; and again just before the
    write). Backed up first; written to a temporary file beside it, proved, then moved over it.
    """
    import os

    from .folders import is_usable

    p = Path(path)
    if allowed is not None and not is_usable(p.parent, allowed):
        raise WorkspaceRewriteError(f"{p.parent} is outside the folders AI Cleaner may reach")
    # D245: nor is a folder outside them ever written into the file - the file names what a session reaches.
    for folder in unique_folders(folders) if allowed is not None else []:
        if not _usable(folder, allowed):
            raise WorkspaceRewriteError(f"{folder} is outside the folders AI Cleaner may reach, so it is not "
                                        "written into the file")
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise WorkspaceRewriteError(f"it could not be read ({exc})") from exc
    if expect and expect != stamp_of(raw):
        raise WorkspaceRewriteError("it changed on disk since this page read it - reload the page and save again")
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw[3 if bom else 0:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceRewriteError("it is not UTF-8 text") from exc
    new = lead_text(text, p.parent, folders)
    home = unique_folders(folders)[0]
    if new == text:
        return {"changed": False, "backup": ""}
    if not os.access(p, os.W_OK):
        raise WorkspaceRewriteError("it is read-only")
    tmp = p.with_name(p.name + ".aicleaner-tmp")
    backup = None
    try:
        tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
        # Proved before it replaces anything: still a workspace file, and its home the one asked for.
        _loads(new)
        if not _same(derived_home([str(tmp)], [])["folder"], Path(home).resolve()):
            raise WorkspaceRewriteError("the result would not make it the home folder")
        if p.read_bytes() != raw:
            raise WorkspaceRewriteError("it changed on disk while it was being rewritten - nothing was written")
        backup = _backup(p, raw, Path(backup_dir)) if backup_dir else None
        os.replace(tmp, p)
    except (OSError, ValueError) as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        if isinstance(exc, WorkspaceRewriteError):
            raise
        raise WorkspaceRewriteError(str(exc)) from exc
    log.info("workspace file %s now leads with %s (backup %s)", p, home, backup)
    return {"changed": True, "backup": str(backup) if backup else ""}
