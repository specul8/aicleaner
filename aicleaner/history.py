"""Conversations Claude Code has already had, as it left them on disk.

The CLI writes one JSONL file per conversation under ~/.claude/projects/<folder>, where <folder>
is the working directory with everything that is not a letter or a digit turned into a dash, and
the file is named for the session id. Nothing here writes. The Session page reads the list so a
conversation can be picked up by its subject instead of by typing a UUID, and the id it hands
back goes through the same check as one typed by hand (H1): this widens what is easy, not what
is allowed.

A conversation still open in a terminal or an editor belongs to that process, and resuming it
from here would fork the transcript. Claude Code leaves no lock to read, so the only honest
signal is how recently the file was written, and one written in the last few minutes is reported
as busy rather than quietly hidden - the person looking at the list knows what else they have
open, and we do not.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

BUSY_WINDOW_S = 300             # written this recently: something may still have it open
MAX_TITLE = 140
SCAN_LINES = 500                # past the tool results that a resumed conversation opens with
MAX_LINE = 200_000              # one pasted file should not become the title
SKIP_PREFIXES = ("<command-name>", "<command-message>", "<local-command-stdout>",
                 "<system-reminder>", "<user-prompt-submit-hook>", "Caveat:",
                 # written ahead of a /clear or a slash command's output (the user, 2026-10-01)
                 "<local-command-caveat>", "<local-command-stderr>")
CONTINUED = "This session is being continued from a previous conversation"
ROBOT_ENTRYPOINTS = ("sdk-py", "sdk-ts", "sdk", "sdk-cli")   # a program asked, not a person
OPEN_FACTOR = 8                 # files read per row asked for, before the search gives up

UUID = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)
NOT_NAME = re.compile(r"[^A-Za-z0-9]")
log = logging.getLogger(__name__)

# Another agent's transcript folder for the code running inside `using_root` (item K). A context
# variable rather than an argument through every function: asyncio.to_thread copies it into the thread.
_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar("aicleaner_transcript_root", default=None)


@contextlib.contextmanager
def using_root(root: str | Path | None):
    """Inside it, transcript_root() is `root` (None: Claude Code's own, as outside)."""
    token = _ROOT.set(Path(root) if root else None)
    try:
        yield
    finally:
        _ROOT.reset(token)
SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class Past:
    """One conversation on disk, as the picker shows it."""
    id: str
    title: str
    project: str
    branch: str
    started_by: str
    updated: float
    size: int
    busy: bool
    last: str = ""      # the latest thing the person typed: tells apart conversations that open alike
    label: str = ""     # its title (item E, 2026-10-16): the model's, or one the person typed
    label_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# What a transcript opens with, and its latest message, kept for the life of the process (D86).
# Reading them cold took 9 s on the first list for a session: up to 200 files, 500 lines each, most
# of them robot transcripts dropped afterwards. A transcript is append-only, so an opening that has
# been found whole never changes and is kept by path alone; anything else is kept against the
# file's (size, mtime) and read again when either moves.
TAIL_BYTES = 65_536
MAX_REMEMBERED = 4096
_HEADS: dict[str, tuple[tuple[int, float] | None, tuple[str, str, str]]] = {}
_LATEST: dict[str, tuple[tuple[int, float], str]] = {}


def _remember(store: dict, key: str, value: Any) -> None:
    if len(store) >= MAX_REMEMBERED:
        store.clear()   # a crude bound; refilling costs one cold list, never a wrong answer
    store[key] = value


def forget_remembered() -> None:
    """Drop what is kept (tests, and nothing else needs to)."""
    _HEADS.clear()
    _LATEST.clear()


def transcript_root(home: str | Path = "") -> Path:
    """Where Claude Code keeps its transcripts, honouring CLAUDE_CONFIG_DIR - or, inside `using_root`,
    another agent's (item K: the bridge writes Copilot's and Codex's conversations in Claude Code's
    shape under aicleaner's state folder, so everything here reads them unchanged)."""
    if home:
        return Path(home) / "projects"
    chosen = _ROOT.get()
    if chosen is not None:
        return chosen
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(configured) if configured else Path.home() / ".claude"
    return base / "projects"


def folder_for(project: str | Path) -> str:
    """The folder name Claude Code derives from a working directory."""
    return NOT_NAME.sub("-", str(Path(project)))


def folders_for(project: str | Path, root: Path) -> list[Path]:
    """Every folder that answers to that name.

    Windows hands the same directory to Claude Code in whatever case the person typed it, so
    c:\\repos and C:\\Repos leave two folders side by side holding the same project's history.
    Both are the project's, so both are read.
    """
    wanted = folder_for(project).lower()
    try:
        return [child for child in root.iterdir()
                if child.is_dir() and child.name.lower() == wanted]
    except OSError:
        return []


def _text_of(message: Any) -> str:
    """The words of a user turn, whatever shape the record holds them in."""
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    from .origin import is_note
    # aicleaner's note on where a prompt was typed (origin.py) is not part of what was typed.
    return " ".join(block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text" and not is_note(block.get("text", "")))


def _describe(path: Path) -> tuple[str, str, str]:
    """The first thing the person actually asked, the branch, and what started the session."""
    title = ""
    branch = ""
    entry = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= SCAN_LINES:
                    break
                if len(line) > MAX_LINE:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                branch = branch or str(record.get("gitBranch") or "")
                entry = entry or str(record.get("entrypoint") or "")
                if title:
                    continue
                text = _typed(record)
                if not text:
                    continue
                if text.startswith(CONTINUED):
                    text = "(continued) " + text[len(CONTINUED):].lstrip(" .:,")
                title = text[:MAX_TITLE]
                if branch and entry:
                    break
    except OSError:
        return "", "", ""
    return title, branch, entry


def _typed(record: dict) -> str:
    """What a person typed in this record, or "" for anything else - a tool result, a hook's
    output, a command's echo, a side conversation."""
    if record.get("type") != "user" or record.get("isSidechain"):
        return ""
    text = SPACES.sub(" ", _text_of(record.get("message"))).strip()
    return "" if text.startswith(SKIP_PREFIXES) else text


def _head(path: Path, stamp: tuple[int, float]) -> tuple[str, str, str]:
    held = _HEADS.get(str(path))
    if held is not None and (held[0] is None or held[0] == stamp):
        return held[1]
    head = _describe(path)
    whole = bool(head[0] and head[2])   # the opening and what started it: neither can change now
    _remember(_HEADS, str(path), (None if whole else stamp, head))
    return head


def _latest(path: Path, stamp: tuple[int, float]) -> str:
    """The most recent message a person typed, from the end of the transcript."""
    held = _LATEST.get(str(path))
    if held is not None and held[0] == stamp:
        return held[1]
    last = ""
    try:
        with path.open("rb") as handle:
            start = max(0, stamp[0] - TAIL_BYTES)
            handle.seek(start)
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        if start:
            lines = lines[1:]   # the first line read is the end of one cut in half
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and (text := _typed(record)):
                last = text[:MAX_TITLE]
                break
    except OSError:
        return ""
    _remember(_LATEST, str(path), (stamp, last))
    return last


def sessions_for(project: str | Path, root: Path | None = None, limit: int = 25,
                 now: float = 0.0, include_robots: bool = False,
                 ours: set[str] | frozenset[str] = frozenset(), labels: dict | None = None) -> list[Past]:
    """The most recent conversations held in that folder, newest first.

    Anything a program started is left out. A repository that gets its pull requests reviewed
    collects a transcript per review, and in this one they outnumber the conversations a hundred
    to one: a picker full of them would be useless. The test is what the entry point is NOT,
    so a surface we have not met yet - an editor, a desktop app - still shows up.
    """
    root = root if root is not None else transcript_root()
    now = now or time.time()
    limit = max(1, int(limit))
    found: list[tuple[float, int, Path]] = []
    for folder in folders_for(project, root):
        for child in folder.glob("*.jsonl"):
            if not UUID.match(child.stem):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            found.append((stat.st_mtime, stat.st_size, child))
    found.sort(key=lambda row: row[0], reverse=True)
    past: list[Past] = []
    for opened, (updated, size, path) in enumerate(found):
        if len(past) >= limit or opened >= limit * OPEN_FACTOR:
            break
        stamp = (size, updated)
        title, branch, entry = _head(path, stamp)
        # The Session page runs Claude Code headless, which tags its transcripts sdk-cli - the same as
        # the robots - so every conversation started there was hidden from its own picker (reported
        # 2026-09-30). The ones aicleaner started are known by id (`ours`) and always shown.
        if not include_robots and entry in ROBOT_ENTRYPOINTS and path.stem.lower() not in ours:
            continue
        past.append(Past(id=path.stem, title=title or "(no opening message)",
                         project=str(project), branch=branch, started_by=entry,
                         updated=updated, size=size, busy=(now - updated) < BUSY_WINDOW_S,
                         last=_latest(path, stamp),
                         label=str(((labels or {}).get(path.stem.lower()) or {}).get("title") or ""),
                         label_source=str(((labels or {}).get(path.stem.lower()) or {}).get("source") or "")))
    return past


def conversations(projects: Iterable[str | Path], root: Path | None = None,
                  limit: int = 25, include_robots: bool = False,
                  ours: set[str] | frozenset[str] = frozenset(), labels: dict | None = None) -> list[dict[str, Any]]:
    """One entry per project folder, so the picker can group what it shows."""
    root = root if root is not None else transcript_root()
    return [{"project": str(project),
             "sessions": [item.as_dict() for item in sessions_for(project, root=root, limit=limit,
                                                                   include_robots=include_robots, ours=ours,
                                                                   labels=labels)]}
            for project in projects]


REGISTRY = "conversations.json"


def our_conversations(sessions_dir: Path | None) -> set[str]:
    """The conversation ids the Session page has started or continued (lower-cased)."""
    if sessions_dir is None:
        return set()
    try:
        return {str(i).lower() for i in json.loads((Path(sessions_dir) / REGISTRY).read_text(encoding="utf-8"))}
    except (OSError, ValueError, TypeError):
        return set()


def note_ours(sessions_dir: Path | None, conversation_id: str) -> None:
    """Remember a conversation the Session page is driving, so its picker lists it."""
    if sessions_dir is None or not conversation_id:
        return
    known = our_conversations(sessions_dir)
    if conversation_id.lower() in known:
        return
    folder = Path(sessions_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (REGISTRY + ".tmp")
    tmp.write_text(json.dumps(sorted(known | {conversation_id.lower()}), indent=2), encoding="utf-8")
    os.replace(tmp, folder / REGISTRY)


def _epoch(value: Any) -> float:
    """An ISO-8601 time from a transcript record as epoch seconds, or 0.0 when there is none."""
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def replay(project: str | Path, session_id: str, limit: int = 200,
           root: Path | None = None, before: float | None = None) -> list[dict[str, Any]]:
    """What was already said in a conversation, as events the Session page can render.

    Resuming hands the model its history but left the page blank, because the event buffer starts
    empty and nothing put the earlier turns back (D38). They are on disk in the same file the
    Continue list is built from, so this reads it and maps the records the transcript renders:

        {"type": "user", "message": {...}}       -> {"type": "prompt", "text": ...}
        {"type": "assistant", "message": {...}}  -> passed through as-is

    Everything else is skipped - sidechains belong to a subagent, tool results come back as user
    records with no words in them, and the command and reminder wrappers are machinery rather than
    conversation. The tail is what is returned: a long conversation would otherwise push the live
    session out of a buffer that holds MAX_EVENTS.

    `before` stops at a moment: a re-attached sitting is replayed by its broker, so only what was
    said before it began is wanted here (D109). A record with no time counts as earlier.
    """
    path = _transcript(project, session_id, root)
    if path is None:
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                event = _event_of(line)
                if event is None:
                    continue
                if before is not None and float(event.get("at") or 0) >= before:
                    continue
                events.append(event)
    except OSError:
        return []
    return events[-limit:] if limit and len(events) > limit else events


def holding(projects: Iterable[str | Path], session_id: str, root: Path | None = None) -> str:
    """Which of these folders a conversation is filed under, or "" (D113). Only the folders given -
    a profile's own - are looked in, never the whole transcript root, so one client's page cannot
    draw another client's conversation by naming its id."""
    for project in projects:
        if str(project).strip() and _transcript(project, session_id, root) is not None:
            return str(project)
    return ""


def refile(session_id: str, home: str | Path, projects: Iterable[str | Path],
           root: Path | None = None) -> tuple[Path, Path] | None:
    """Move a conversation filed under another folder to the home folder's, before it is resumed
    there: (from, to), or None when there was nothing to move.

    A conversation stays filed where it began, whichever folder resumes it (DECISIONS 64), and the
    old folder went on shaping it: one client's began in a OneDrive folder holding an empty git
    repository of its own, and Claude kept going back there to work and commit, away from the
    home folder's hooks and history (the user, 2026-10-12). Filed under the home folder, it is
    that project's conversation from then on. Its folder of the same name (subagent transcripts,
    tool output) goes with it. Never over a transcript already there; only the profile's own
    folders are looked in (`holding`).
    """
    root = root if root is not None else transcript_root()
    found_in = holding(projects, session_id, root)
    if not found_in:
        return None
    source = _transcript(found_in, session_id, root)
    if source is None:
        return None
    wanted = folder_for(home).lower()
    if source.parent.name.lower() == wanted:
        return None                                  # already the home folder's
    existing = folders_for(home, root)
    target_dir = existing[0] if existing else root / folder_for(home)
    target = target_dir / source.name
    if target.exists():
        log.warning("not refiling %s: %s is already there", source, target)
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    beside = source.parent / session_id
    if beside.is_dir():
        destination = target_dir / session_id
        if not destination.exists():
            shutil.move(str(beside), str(destination))
        else:                                        # merged, never overwriting
            for child in beside.iterdir():
                if not (destination / child.name).exists():
                    shutil.move(str(child), str(destination / child.name))
    log.info("refiled conversation %s from %s to %s", session_id, source.parent, target_dir)
    return source, target


def _transcript(project: str | Path, session_id: str, root: Path | None) -> Path | None:
    """The conversation's file under one of the project's folders, or None. The id must be a UUID
    (H1), so nothing typed can name a path."""
    root = root if root is not None else transcript_root()
    if not UUID.match(session_id or ""):
        return None
    for folder in folders_for(project, root):
        path = folder / f"{session_id}.jsonl"
        if path.is_file():
            return path
    return None


def _event_of(line: str) -> dict[str, Any] | None:
    """One transcript line as the event the Session page renders, or None for everything else
    (replay's rules)."""
    if len(line) > MAX_LINE:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("isSidechain"):
        return None
    kind = record.get("type")
    # When it was said (feature 66): the record's own time, so a resumed conversation does not read
    # as if all of it happened now.
    said = _epoch(record.get("timestamp"))
    stamp = {"at": said} if said else {}
    if kind == "assistant" and isinstance(record.get("message"), dict):
        return {"type": "assistant", "message": record["message"], **stamp}
    if kind == "user":
        text = SPACES.sub(" ", _text_of(record.get("message"))).strip()
        if text and not text.startswith(SKIP_PREFIXES):
            return {"type": "prompt", "text": text, **stamp}
    if kind == "system" and record.get("subtype") == "compact_boundary":
        # Where the conversation was compacted (E5.1): the transcript writes its numbers in camelCase,
        # the stream in snake_case - replayed in the stream's shape, so the page reads one form.
        meta = record.get("compactMetadata") or record.get("compact_metadata") or {}
        if isinstance(meta, str):
            meta = {}
        names = {"preTokens": "pre_tokens", "postTokens": "post_tokens", "durationMs": "duration_ms", "trigger": "trigger"}
        return {"type": "system", "subtype": "compact_boundary",
                "compact_metadata": {snake: meta.get(camel, meta.get(snake)) for camel, snake in names.items()},
                **stamp}
    return None
