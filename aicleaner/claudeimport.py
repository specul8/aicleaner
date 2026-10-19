"""Importing a project from claude.ai (2026-10-16, item H; spec 2026-10-16-h-claude-ai-import-design.md).

claude.ai's data export is a manifest naming one zip per category, each fetched from claude.ai with
the browser's own sign-in. The wizard reads the manifest here, the person downloads the zips (they
are picked up from Downloads on this machine, or uploaded through remote access), then one project,
its chats - chats carry no project link, so they are ranked by how much they are about it - and
where it goes. The import writes the project's instructions, documents and memories into the home
folder and turns each chosen chat into a Claude Code transcript, so Continue lists it.

Everything stays on this machine. The export holds every chat the account has: it is read only to
list and convert, and the copies taken are deleted when the import ends.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .convsearch import Transcript, Turn, search
from .history import folder_for

CATEGORIES = ("projects", "conversations", "memories", "light_metadata", "frames")
REQUIRED = ("projects", "conversations")
TICKED = ("projects", "conversations", "memories")
FOLDER = "claude-ai-import"
MAX_UPLOAD = 500 * 1024 * 1024
STABLE_S = 2.0
QUERY_WORDS = 80
MEMORY_WORDS = 60
TICK_SHARE = 0.5               # a chat is pre-ticked when it scores this share of the best one or more
SECTION = "## From claude.ai"
REMEMBERED = "last-export.json"   # the export used last, in <state>/claude-ai-import/
PICK_TIMEOUT_S = 240


class ClaudeImportError(ValueError):
    """Something about the export or the request that the person can fix."""


# ---- the manifest ---------------------------------------------------------------------------------

def _safe_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip", name or ""))


def read_manifest(doc: Any) -> dict[str, Any]:
    """The manifest's files, checked: claude.ai https addresses, known categories, plain zip names."""
    if not isinstance(doc, dict) or not isinstance(doc.get("data_files"), list):
        raise ClaudeImportError("that is not a claude.ai export manifest (no data_files)")
    files = []
    for entry in doc["data_files"]:
        if not isinstance(entry, dict):
            raise ClaudeImportError("the manifest has an entry that is not a file")
        category, name, url = str(entry.get("category", "")), str(entry.get("filename", "")), str(entry.get("export_url", ""))
        if category not in CATEGORIES:
            raise ClaudeImportError(f"the manifest names an unknown category: {category or '(none)'}")
        if not _safe_name(name):
            raise ClaudeImportError(f"the manifest names a file that is not a plain zip name: {name!r}")
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme != "https" or not (host == "claude.ai" or host.endswith(".claude.ai")):
            raise ClaudeImportError(f"{name}: its address is not on claude.ai over https, so it is refused")
        files.append({"category": category, "part": int(entry.get("part") or 0), "filename": name, "url": url,
                      "required": category in REQUIRED, "ticked": category in TICKED})
    missing = [c for c in REQUIRED if not any(f["category"] == c for f in files)]
    if missing:
        raise ClaudeImportError(f"the manifest has no {' or '.join(missing)} file")
    return {"created_at": str(doc.get("created_at", "")), "files": files}


# ---- the zips -------------------------------------------------------------------------------------

def zip_problem(path: Path, category: str) -> str:
    """'' when the zip opens and holds what its category should; otherwise what is wrong."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return "it is not a zip file (an expired link downloads a page, not the export)"
    if category == "conversations" and "conversations.json" not in [n.rsplit("/", 1)[-1] for n in names]:
        return "it holds no conversations.json"
    if category in ("projects", "memories") and not any(n.endswith(".json") for n in names):
        return "it holds no JSON files"
    return ""


def _json_members(path: Path) -> Iterable[Any]:
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".json"):
                yield name, json.loads(z.read(name))


def read_projects(path: Path) -> list[dict[str, Any]]:
    """One JSON per project (a list in one file is read too)."""
    found = []
    for _name, doc in _json_members(path):
        for project in doc if isinstance(doc, list) else [doc]:
            if isinstance(project, dict) and project.get("uuid") and "name" in project:
                found.append(project)
    return found


def read_conversations(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as z:
        member = next(n for n in z.namelist() if n.rsplit("/", 1)[-1] == "conversations.json")
        doc = json.loads(z.read(member))
    return [c for c in doc if isinstance(c, dict) and c.get("uuid")] if isinstance(doc, list) else []


def read_memories(path: Path) -> dict[str, Any]:
    for _name, doc in _json_members(path):
        if isinstance(doc, dict):
            return doc
    return {}


def memories_for(doc: dict[str, Any], project_uuid: str) -> str:
    """The project's own memory when the export keeps one per project; otherwise all of them, said so."""
    own = (doc.get("project_memories") or {}).get(project_uuid)
    if own:
        return f"# Memory from claude.ai\n\nThis project's own memory, as claude.ai kept it.\n\n{own.strip()}\n"
    parts = ["# Memory from claude.ai", "",
             "claude.ai kept no memory for this project on its own, so this is all of the account's memory."]
    if doc.get("conversations_memory"):
        parts += ["", "## Across conversations", "", str(doc["conversations_memory"]).strip()]
    for item in doc.get("memory_files") or []:
        if isinstance(item, dict) and item.get("content"):
            parts += ["", f"## {item.get('path') or 'memory'}", "", str(item["content"]).strip()]
    return "\n".join(parts) + "\n" if len(parts) > 3 else ""


# ---- chats ----------------------------------------------------------------------------------------

def _when(text: str) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def user_text(message: dict[str, Any]) -> str:
    parts = [message.get("text") or ""]
    for item in message.get("attachments") or []:
        parts.append(f"\n\n[Attachment: {item.get('file_name')}]\n{item.get('extracted_content') or ''}")
    for item in message.get("files") or []:
        parts.append(f"\n\n[File attached in claude.ai: {item.get('file_name', 'a file')} - not in the export]")
    return "".join(parts).strip()


def reply_blocks(message: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            out.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "tool_use":
            out.append({"type": "text", "text": f"[In claude.ai, used {block.get('name') or 'a tool'}]"})
    if not out and message.get("text"):
        out.append({"type": "text", "text": message["text"]})
    return out


def chat_turns(chat: dict[str, Any]) -> list[Turn]:
    turns = []
    for message in chat.get("chat_messages") or []:
        at = _when(message.get("created_at") or chat.get("created_at") or "")
        if message.get("sender") == "human":
            text = user_text(message)
            role = "you"
        else:
            text = "\n\n".join(b["text"] for b in reply_blocks(message))
            role = "claude"
        if text:
            turns.append(Turn(role, at, text))
    return turns


class _Given:
    """convsearch.search reads turns through a cache; these chats are already in memory."""

    def __init__(self, turns: dict[str, list[Turn]]) -> None:
        self.by_path = turns

    def turns(self, path: Path, _size: int, _updated: float) -> list[Turn]:
        return self.by_path[str(path)]


def project_query(project: dict[str, Any], memory: str = "") -> str:
    """What the project is about: its name, description, instructions, document names and - since
    many projects have no instructions - the memory claude.ai kept for it."""
    instructions = " ".join(str(project.get("prompt_template") or "").split()[:QUERY_WORDS])
    docs = " ".join(Path(str(d.get("filename", ""))).stem for d in project.get("docs") or [] if isinstance(d, dict))
    remembered = " ".join(str(memory or "").split()[:MEMORY_WORDS])
    return " ".join([str(project.get("name") or ""), str(project.get("description") or ""), instructions, docs, remembered])


def rank_chats(project: dict[str, Any], chats: list[dict[str, Any]], imported: set[str] = frozenset(),
               memory: str = "",
               query: str = "") -> list[dict[str, Any]]:
    """Every chat, the ones most about the project first; the clear ones ticked, imported ones not."""
    items, turns = [], {}
    created = {chat["uuid"]: chat.get("created_at") for chat in chats}
    for chat in chats:
        key = f"chat:{chat['uuid']}"
        turns[key] = chat_turns(chat)
        items.append(Transcript(id=chat["uuid"], project="", path=Path(key), size=0,
                                updated=_when(chat.get("updated_at") or chat.get("created_at") or ""),
                                title=str(chat.get("name") or "")))
    scored = {r["id"]: r for r in search(items, _Given(turns), query or project_query(project, memory), limit=len(items))}
    best = max((r["score"] for r in scored.values()), default=0.0)
    out = []
    for item in items:
        hit = scored.get(item.id)
        score = hit["score"] if hit else 0.0
        done = item.id.lower() in imported
        out.append({"id": item.id, "title": item.title or "(untitled chat)", "updated": item.updated,
                    # When it began, for sorting the list by it (E3 part 8, feedback #4).
                    "created": _when(created.get(item.id) or ""),
                    "messages": len(turns[str(item.path)]), "score": score,
                    "passage": (hit["passages"][0] if hit and hit["passages"] else None),
                    "imported": done, "ticked": bool(best and score >= best * TICK_SHARE and not done)})
    out.sort(key=lambda r: (-r["score"], -r["updated"]))
    return out


PICK_PROMPT = """A person is moving one project from claude.ai to a coding assistant. claude.ai does not
record which chats belonged to the project, so choose them from the list below.

The project:
{project}

The chats, numbered (title, date, then claude.ai's summary or the opening message):
{chats}

Answer with JSON only: {{"chats": [the numbers of the chats that are about this project's subject]}}.
Include a chat when its main subject is this project's; leave out chats about anything else."""


def pick_prompt(project: dict[str, Any], chats: list[dict[str, Any]], memory: str = "") -> str:
    about = [f"Name: {project.get('name')}"]
    for label, text, limit in (("Description", project.get("description"), 80), ("Instructions", project.get("prompt_template"), 150),
                               ("What claude.ai remembered about it", memory, 300)):
        words = " ".join(str(text or "").split()[:limit])
        if words:
            about.append(f"{label}: {words}")
    lines = []
    for n, chat in enumerate(chats, start=1):
        opening = next((m.get("text") for m in chat.get("chat_messages") or [] if m.get("sender") == "human"), "")
        gist = " ".join(str(chat.get("summary") or opening or "").split()[:60])
        lines.append(f"{n}. {chat.get('name') or '(untitled)'} ({str(chat.get('created_at') or '')[:10]}): {gist}")
    return PICK_PROMPT.format(project="\n".join(about), chats="\n".join(lines))


def parse_pick(text: str, chats: list[dict[str, Any]]) -> list[str]:
    """The ids of the chats the model chose, from its JSON; numbers outside the list are ignored."""
    found = re.search(r"\{.*\}", text, re.S)
    if not found:
        raise ValueError("no JSON in the answer")
    numbers = json.loads(found.group(0)).get("chats")
    if not isinstance(numbers, list):
        raise ValueError("no list of chats in the answer")
    return [chats[int(n) - 1]["uuid"] for n in numbers if str(n).isdigit() and 1 <= int(n) <= len(chats)]


def model_pick(claude: str, base_url: str, project: dict[str, Any], chats: list[dict[str, Any]], memory: str = "",
               run: Any = None) -> list[str]:
    """One isolated model request (oneshot.ask) choosing the project's chats. Raises RuntimeError."""
    import subprocess

    from .oneshot import ask
    text = ask(claude, base_url, pick_prompt(project, chats, memory), PICK_TIMEOUT_S, run=run or subprocess.run)
    try:
        return parse_pick(text, chats)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Claude did not answer as asked: {exc}") from exc


# ---- writing --------------------------------------------------------------------------------------

def repath(text: str, pairs: list[tuple[str, str]]) -> str:
    """Old folders named in the text, as the new ones - backslash and forward-slash forms, any case."""
    for old, new in pairs:
        old, new = old.rstrip("\\/"), new.rstrip("\\/")
        if not old:
            continue
        for a, b in ((old.replace("/", "\\"), new.replace("/", "\\")), (old.replace("\\", "/"), new.replace("\\", "/"))):
            text = re.sub(re.escape(a), lambda _m, b=b: b, text, flags=re.I)
    return text


def convert(chat: dict[str, Any], cwd: str, pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """One chat as Claude Code transcript lines: a summary first, then each message linked to the last."""
    sid = chat["uuid"]
    lines: list[dict[str, Any]] = []
    parent = None
    base = {"isSidechain": False, "userType": "external", "entrypoint": "claude-ai-import", "cwd": cwd,
            "sessionId": sid, "version": "claude-ai-import"}
    for message in chat.get("chat_messages") or []:
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sid}/{message.get('uuid') or len(lines)}"))
        when = message.get("created_at") or chat.get("created_at")
        if message.get("sender") == "human":
            line = {**base, "parentUuid": parent, "type": "user", "uuid": mid, "timestamp": when,
                    "message": {"role": "user", "content": repath(user_text(message), pairs) or "(empty message)"}}
        else:
            blocks = [{"type": "text", "text": repath(b["text"], pairs)} for b in reply_blocks(message)]
            line = {**base, "parentUuid": parent, "type": "assistant", "uuid": mid, "timestamp": when,
                    "message": {"id": f"msg_import_{mid[:8]}", "type": "message", "role": "assistant",
                                "model": "claude-ai-import",
                                "content": blocks or [{"type": "text", "text": "(no text in this reply)"}],
                                "stop_reason": "end_turn", "stop_sequence": None,
                                "usage": {"input_tokens": 0, "output_tokens": 0}}}
        lines.append(line)
        parent = mid
    title = chat.get("name") or "Imported claude.ai chat"
    lines.insert(0, {"type": "summary", "summary": f"{title} (from claude.ai)", "leafUuid": parent})
    return lines


def safe_file_name(name: str, taken: set[str]) -> str:
    """A document's name made safe for Windows, numbered when it clashes."""
    # The last part by hand: Path("a:b.txt").name is "b.txt" on Windows, "a:" read as a drive.
    last = re.split(r"[\\/]", str(name or "document"))[-1]
    stem = re.sub(r'[<>:"|?*\x00-\x1f]', "_", last).strip(" .") or "document"
    candidate, n = stem, 1
    while candidate.lower() in taken:
        n += 1
        root, dot, ext = stem.rpartition(".")
        candidate = f"{root} ({n}).{ext}" if dot and root else f"{stem} ({n})"
    taken.add(candidate.lower())
    return candidate


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write(path: Path, text: str, roots: list[Path]) -> None:
    """Written beside itself first, then swapped in; only ever inside the given folders."""
    if not any(_inside(path, root) for root in roots):
        raise ClaudeImportError(f"{path} is outside the folders this import may write")
    if os.name == "nt" and len(str(path.resolve())) >= 260:
        # Windows says only "cannot find the path"; a transcript folder is named after the home's
        # whole path, so a deep home makes one too long.
        raise ClaudeImportError(f"{path} is {len(str(path.resolve()))} characters, more than Windows allows (259): "
                                "choose a home folder with a shorter path")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short, not "<name>.importing": a transcript's path is already long, and Windows stops at 260.
    tmp = path.parent / f"~{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def claude_md_section(project: dict[str, Any]) -> str:
    name = str(project.get("name") or "the project")
    description = " ".join(str(project.get("description") or "").split())
    lines = [f"{SECTION}: {name}", ""]
    if description:
        lines += [description, ""]
    lines += ["This work began as a claude.ai project. Its instructions are in",
              "`claude-project/claude-ai-instructions.md` and its documents in `claude-project/docs/`;",
              "read them before starting.", ""]
    return "\n".join(lines)


def write_import(home: Path, transcripts: Path, project: dict[str, Any], chats: list[dict[str, Any]],
                 memories: str = "", pairs: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """The project's files into the home, each chosen chat into the home's transcript folder."""
    pairs = pairs or []
    if not home.is_dir():
        raise ClaudeImportError(f"the home folder {home} does not exist")
    out = home / "claude-project"
    target = transcripts / folder_for(str(home))
    roots = [home, target]
    written: list[str] = []
    instructions = repath(str(project.get("prompt_template") or ""), pairs)
    _write(out / "claude-ai-instructions.md",
           f"# {project.get('name')} - instructions from claude.ai\n\n" + (instructions.strip() or "_(The project had no instructions.)_") + "\n",
           roots)
    written.append(str(out / "claude-ai-instructions.md"))
    taken: set[str] = set()
    for doc in project.get("docs") or []:
        if isinstance(doc, dict):
            path = out / "docs" / safe_file_name(doc.get("filename", ""), taken)
            _write(path, repath(str(doc.get("content") or ""), pairs), roots)
            written.append(str(path))
    if memories:
        _write(out / "memories.md", repath(memories, pairs), roots)
        written.append(str(out / "memories.md"))
    claude_md = home / "CLAUDE.md"
    section = claude_md_section(project)
    held = claude_md.read_text(encoding="utf-8") if claude_md.is_file() else ""
    if section.splitlines()[0] not in held:
        _write(claude_md, (held.rstrip() + "\n\n" if held.strip() else "") + section, roots)
        written.append(str(claude_md))
    imported, kept = [], []
    for chat in sorted(chats, key=lambda c: c.get("created_at") or ""):
        path = target / f"{chat['uuid']}.jsonl"
        if path.exists():
            kept.append(chat["uuid"])
            continue
        lines = convert(chat, str(home), pairs)
        _write(path, "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), roots)
        # Dated as the chat was last updated on claude.ai (2026-10-17): Continue sorts by it, and a file
        # written moments ago reads as "busy" - delete refused it for five minutes after an import.
        last = _when(chat.get("updated_at") or chat.get("created_at") or "")
        if last:
            os.utime(path, (last, last))
        imported.append(chat["uuid"])
    return {"written": written, "chats_imported": imported, "chats_left_alone": kept, "transcripts": str(target)}


def imported_ids(home: Path, transcripts: Path) -> set[str]:
    folder = transcripts / folder_for(str(home))
    return {p.stem.lower() for p in folder.glob("*.jsonl")} if folder.is_dir() else set()


# ---- jobs -----------------------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    folder: Path
    manifest: dict[str, Any]
    started: float = field(default_factory=time.time)
    seen: dict[str, tuple[int, float]] = field(default_factory=dict)
    _conversations: list[dict[str, Any]] | None = None
    doc: Any = None                  # the manifest as chosen, kept to remember the export by
    extra: Path | None = None        # a folder this export's zips are looked for in, besides Downloads
    source: str = ""                 # the folder the zips were taken from, when one was

    def file(self, name: str) -> dict[str, Any]:
        for entry in self.manifest["files"]:
            if entry["filename"] == name:
                return entry
        raise ClaudeImportError(f"{name} is not one of this export's files")

    def path_of(self, category: str) -> Path | None:
        for entry in self.manifest["files"]:
            if entry["category"] == category and (self.folder / entry["filename"]).is_file():
                return self.folder / entry["filename"]
        return None

    def conversations(self) -> list[dict[str, Any]]:
        """Read once: conversations.json is the largest thing in the export."""
        if self._conversations is None:
            path = self.path_of("conversations")
            if path is None:
                raise ClaudeImportError("the conversations file has not arrived yet")
            self._conversations = read_conversations(path)
        return self._conversations


def downloads_folder() -> Path:
    """Where the browser saves: AICLEANER_DOWNLOADS when set (a test server's own folder), else
    Windows' Downloads known folder (it can be moved, to OneDrive or another drive), else ~/Downloads."""
    if os.environ.get("AICLEANER_DOWNLOADS"):
        return Path(os.environ["AICLEANER_DOWNLOADS"])
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

            downloads = GUID(0x374DE290, 0x123F, 0x4565, (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            found = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(downloads), 0, None, ctypes.byref(found)) == 0:
                path = Path(found.value)
                ctypes.windll.ole32.CoTaskMemFree(found)
                return path
        except (OSError, AttributeError):
            pass
    return Path.home() / "Downloads"


def _candidates(folder: Path, name: str) -> list[Path]:
    """The file by its exact name, or as a browser renames a second download: 'name (1).zip'."""
    stem = name[:-4]
    found = [p for p in folder.glob(glob_escape(stem) + "*.zip")
             if p.name == name or re.fullmatch(re.escape(stem) + r" \(\d+\)\.zip", p.name)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def glob_escape(text: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", text)


class Imports:
    """Imports under way, in memory, with their files in <state>/claude-ai-import/<job>/."""

    def __init__(self, state_dir: Path) -> None:
        self.root = state_dir / FOLDER
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def start(self, manifest_doc: Any, extra: Path | None = None) -> Job:
        manifest = read_manifest(manifest_doc)
        if extra is None:
            # The same export as last time (its manifest made at the same moment): its zips are looked
            # for where they were, so they are not downloaded again (the user, 2026-10-17).
            known = self.remembered()
            if known and known["complete"] and known["created_at"] and known["created_at"] == manifest["created_at"]:
                extra = Path(known["folder"])
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, self.root / job_id, manifest, doc=manifest_doc, extra=extra)
        job.folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[job_id] = job
        return job

    # ---- the export remembered (the user, 2026-10-17) ----
    # After an import, where its zips were is kept with its manifest, so the next import can use the
    # same export again without downloading it; a new manifest can always be chosen instead.

    def start_from_folder(self, folder: Path) -> Job:
        """An export already on this machine: the newest manifest-*.json in the folder, its zips beside it."""
        folder = Path(str(folder).strip().strip('"'))
        if not folder.is_dir():
            raise ClaudeImportError(f"{folder} is not a folder on this machine")
        manifests = sorted(folder.glob("manifest-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not manifests:
            raise ClaudeImportError(f"{folder} holds no manifest-....json from a claude.ai export")
        try:
            doc = json.loads(manifests[0].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClaudeImportError(f"{manifests[0].name} could not be read: {exc}") from exc
        return self.start(doc, extra=folder)

    def _remember(self, folder: str, doc: Any, created_at: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = {"folder": folder, "manifest": doc, "created_at": created_at, "remembered_at": time.time()}
        tmp = self.root / (REMEMBERED + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, self.root / REMEMBERED)

    def remembered(self) -> dict[str, Any] | None:
        """The export used last: its folder, when it was made, and whether its needed zips are still there."""
        try:
            record = json.loads((self.root / REMEMBERED).read_text(encoding="utf-8"))
            files = read_manifest(record["manifest"])["files"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        folder = Path(str(record.get("folder", "")))
        there = folder.is_dir() and all(_candidates(folder, f["filename"]) for f in files if f["required"])
        return {"folder": str(folder), "created_at": record.get("created_at", ""),
                "remembered_at": record.get("remembered_at", 0), "complete": there}

    def start_remembered(self) -> Job:
        found = self.remembered()
        if not found:
            raise ClaudeImportError("no earlier export is remembered: choose its manifest")
        if not found["complete"]:
            raise ClaudeImportError(f"the earlier export's zips are no longer all in {found['folder']}: choose a manifest")
        record = json.loads((self.root / REMEMBERED).read_text(encoding="utf-8"))
        return self.start(record["manifest"], extra=Path(found["folder"]))

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(str(job_id))
        if job is None:
            raise ClaudeImportError("no such import (the page may be older than the server: start again)")
        return job

    def status(self, job: Job, watch: list[Path] | None = None, now: float | None = None) -> list[dict[str, Any]]:
        """Each file: received, waiting or a problem. Files in the watched folders are taken once their
        size has held for STABLE_S between two looks and the zip checks out."""
        now = now or time.time()
        out = []
        for entry in job.manifest["files"]:
            name = entry["filename"]
            mine = job.folder / name
            state, problem = ("received", "") if mine.is_file() else ("waiting", "")
            if state == "waiting":
                for folder in [*([job.extra] if job.extra else []), *(watch or [])]:
                    found = _candidates(folder, name) if folder.is_dir() else []
                    if not found:
                        continue
                    stat = found[0].stat()
                    size = stat.st_size
                    before = job.seen.get(name)
                    # Already there and not being written (unchanged for STABLE_S): taken at once, so
                    # an export already on this machine is not waited on (2026-10-17).
                    settled = now - stat.st_mtime >= STABLE_S
                    if settled or (before and before[0] == size and now - before[1] >= STABLE_S):
                        problem = zip_problem(found[0], entry["category"])
                        if not problem:
                            shutil.copyfile(found[0], mine)
                            state = "received"
                            job.source = str(folder)
                        else:
                            state = "bad"
                    elif not before or before[0] != size:
                        job.seen[name] = (size, now)
                    break
            out.append({**entry, "state": state, "problem": problem})
        return out

    def upload(self, job: Job, name: str, chunks: Iterable[bytes]) -> dict[str, Any]:
        """A downloaded file sent from the page (remote access, or chosen by hand)."""
        entry = job.file(name)
        tmp = job.folder / (name + ".uploading")
        size = 0
        with open(tmp, "wb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > MAX_UPLOAD:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise ClaudeImportError(f"{name} is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
                out.write(chunk)
        problem = zip_problem(tmp, entry["category"])
        if problem:
            tmp.unlink(missing_ok=True)
            raise ClaudeImportError(f"{name}: {problem}")
        os.replace(tmp, job.folder / name)
        return {**entry, "state": "received", "problem": ""}

    def upload_chunk(self, job: Job, name: str, offset: int, data: bytes, last: bool) -> dict[str, Any]:
        """One piece of a file sent from the page, in order. Pieces, as base64 in JSON, so an upload
        works the same through the encrypted tunnel, which carries each request whole as text."""
        entry = job.file(name)
        tmp = job.folder / (name + ".uploading")
        held = tmp.stat().st_size if tmp.is_file() else 0
        if offset == 0:
            held = 0
            tmp.write_bytes(b"")
        if offset != held:
            raise ClaudeImportError(f"{name}: a piece arrived out of order (at {offset}, expected {held}); send it again")
        if held + len(data) > MAX_UPLOAD:
            tmp.unlink(missing_ok=True)
            raise ClaudeImportError(f"{name} is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
        with open(tmp, "ab") as out:
            out.write(data)
        if not last:
            return {**entry, "state": "uploading", "received": held + len(data), "problem": ""}
        problem = zip_problem(tmp, entry["category"])
        if problem:
            tmp.unlink(missing_ok=True)
            raise ClaudeImportError(f"{name}: {problem}")
        os.replace(tmp, job.folder / name)
        return {**entry, "state": "received", "problem": ""}

    def projects(self, job: Job) -> list[dict[str, Any]]:
        path = job.path_of("projects")
        if path is None:
            raise ClaudeImportError("the projects file has not arrived yet")
        out = []
        for p in read_projects(path):
            instructions = str(p.get("prompt_template") or "")
            out.append({"uuid": p["uuid"], "name": str(p.get("name") or ""), "description": str(p.get("description") or ""),
                        "instructions": "\n".join(instructions.strip().splitlines()[:6]),
                        "docs": len(p.get("docs") or []), "updated": str(p.get("updated_at") or "")})
        out.sort(key=lambda p: p["updated"], reverse=True)
        return out

    def project(self, job: Job, project_uuid: str) -> dict[str, Any]:
        path = job.path_of("projects")
        found = next((p for p in read_projects(path) if p["uuid"] == project_uuid), None) if path else None
        if found is None:
            raise ClaudeImportError("that project is not in the export")
        return found

    def chats(self, job: Job, project_uuid: str, imported: set[str] = frozenset(), query: str = "") -> list[dict[str, Any]]:
        path = job.path_of("memories")
        memory = (read_memories(path).get("project_memories") or {}).get(project_uuid, "") if path else ""
        return rank_chats(self.project(job, project_uuid), job.conversations(), imported, str(memory), query)

    def pick(self, job: Job, project_uuid: str, claude: str, base_url: str, run: Any = None) -> list[str]:
        """Claude's choice of the project's chats (one request, through this proxy)."""
        path = job.path_of("memories")
        memory = (read_memories(path).get("project_memories") or {}).get(project_uuid, "") if path else ""
        return model_pick(claude, base_url, self.project(job, project_uuid), job.conversations(), str(memory), run)

    def run(self, job: Job, project_uuid: str, chat_ids: list[str], home: Path, transcripts: Path,
            pairs: list[tuple[str, str]], with_memories: bool = True, keep_zips: bool = False) -> dict[str, Any]:
        project = self.project(job, project_uuid)
        wanted = {str(i).lower() for i in chat_ids}
        chats = [c for c in job.conversations() if c["uuid"].lower() in wanted]
        memories = ""
        if with_memories and job.path_of("memories") is not None:
            memories = memories_for(read_memories(job.path_of("memories")), project_uuid)
        report = write_import(home, transcripts, project, chats, memories, pairs)
        # Remembered for next time: the folder the zips came from, or - uploaded and kept - the copies.
        # Uploaded and not kept leaves nothing on this machine to point at, so the last record stays.
        kept_at = job.source or (str(job.folder) if keep_zips else "")
        if kept_at and job.doc is not None:
            self._remember(kept_at, job.doc, job.manifest.get("created_at", ""))
        report["remembered"] = kept_at
        if not keep_zips:
            self.finish(job)
        report["zips_kept"] = str(job.folder) if keep_zips else ""
        return report

    def finish(self, job: Job) -> None:
        """The copies of the export deleted: it holds every chat the account has."""
        with self.lock:
            self.jobs.pop(job.id, None)
        shutil.rmtree(job.folder, ignore_errors=True)
