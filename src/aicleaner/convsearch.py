"""Finding a conversation by topic (2026-10-16; spec 2026-10-16-conversation-search-design.md).

Local and fast by default: what a person typed and Claude's replies, extracted from each transcript
once and cached beside the state (re-read when the file's size or time changes), ranked with BM25 plus
a phrase bonus and a small recency factor, with the best passages and where the words are in them.

Optionally the top results go to a one-off `claude --print` with no tools, started through this
proxy so it is filtered like every request, to put them in order and say why each matches.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .history import ROBOT_ENTRYPOINTS, UUID, _epoch, _head, _latest, _typed, folders_for

CACHE_VERSION = 2              # 2: the offset read up to, for reading only what was added
MAX_RESULTS = 20
PASSAGES = 3
WINDOW = 120                  # characters either side of a passage's first match
K1, B = 1.2, 0.75             # BM25
RECENCY_HALF_LIFE_DAYS = 90
RECENCY_WEIGHT = 0.15
RANK_CANDIDATES = 15
RANK_TIMEOUT_S = 30
WORD = re.compile(r"[a-z0-9][a-z0-9_'-]*")
STOP = frozenset("""a an and are as at be but by can could did do does for from had has have he her his
how i if in into is it its me my no not of on or our she so than that the their them then there these
they this to us was we were what when where which who why will with would you your about just also
any been more most some such too very""".split())


def words(text: str) -> list[str]:
    """Search words: lower case, two characters or more, common English words left out."""
    return [w.strip("'-") for w in WORD.findall(text.lower()) if len(w.strip("'-")) >= 2 and w not in STOP]


def stem(term: str) -> str:
    """A query word without a plural ending, so it matches as a prefix: certificates finds
    certificate, policies finds policy. Words in the text are matched by prefix, so they need none."""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and term.endswith("es") and term[-3] in "sxz":
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


@dataclass
class Turn:
    role: str                 # "you" or "claude"
    at: float
    text: str


@dataclass
class Transcript:
    id: str
    project: str
    path: Path
    size: int
    updated: float
    title: str = ""
    branch: str = ""
    entry: str = ""
    last: str = ""
    turns: list[Turn] = field(default_factory=list)


def _reply_text(record: dict) -> str:
    """Claude's own words in an assistant record - not tool calls, not thinking."""
    if record.get("type") != "assistant" or record.get("isSidechain"):
        return ""
    content = (record.get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(block.get("text", "") for block in content
                     if isinstance(block, dict) and block.get("type") == "text").strip()


def extract(path: Path, start: int = 0) -> tuple[list[Turn], int]:
    """The conversation as it reads - each typed prompt and each reply, in order - from byte `start`,
    and the offset just past the last complete line read (where to carry on next time).

    Transcripts only ever grow at the end, so a search after another turn reads the new lines only:
    a long conversation's transcript passes 100 MB, and reading it whole on every search took most
    of a 25-second search (2026-10-16). Tool results - most of those megabytes - are recognised by
    their tool_use_id and skipped before they are parsed."""
    turns: list[Turn] = []
    end = start
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break                                   # a line still being written: next time
                end += len(raw)
                if b'"tool_use_id"' in raw and b'"type":"user"' in raw.replace(b'": "', b'":"'):
                    continue
                try:
                    record = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                at = _epoch(record.get("timestamp"))
                typed = _typed(record)
                if typed:
                    turns.append(Turn("you", at, typed))
                    continue
                reply = _reply_text(record)
                if reply:
                    turns.append(Turn("claude", at, reply))
    except OSError:
        return [], start
    return turns, end


class Cache:
    """Extracted turns per transcript, on disk, keyed by the path and checked by size and time."""

    def __init__(self, folder: Path | None):
        self.folder = folder
        self.read = 0                                 # transcripts read (not served from the cache)

    def _file(self, path: Path) -> Path | None:
        if self.folder is None:
            return None
        return self.folder / (hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest() + ".json")

    def turns(self, path: Path, size: int, updated: float) -> list[Turn]:
        cached = self._file(path)
        held: list[Turn] = []
        start = 0
        if cached is not None and cached.is_file():
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                if data.get("v") == CACHE_VERSION:
                    if data.get("size") == size and data.get("updated") == updated:
                        return [Turn(*t) for t in data.get("turns", [])]
                    # Grown, not rewritten: carry on from where the last read stopped.
                    offset = int(data.get("offset") or 0)
                    if 0 < offset <= size and size > int(data.get("size") or 0):
                        held, start = [Turn(*t) for t in data.get("turns", [])], offset
            except (OSError, ValueError, TypeError):
                held, start = [], 0
        added, offset = extract(path, start)
        turns = held + added
        self.read += 1
        if cached is not None:
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps({"v": CACHE_VERSION, "path": str(path), "size": size, "updated": updated,
                                              "offset": offset, "turns": [[t.role, t.at, t.text] for t in turns]}),
                                  encoding="utf-8")
            except OSError:
                pass
        return turns

    def prune(self, keep: Iterable[Path]) -> None:
        """Forget transcripts that are no longer there."""
        if self.folder is None or not self.folder.is_dir():
            return
        wanted = {self._file(p).name for p in keep}
        for child in self.folder.glob("*.json"):
            if child.name not in wanted:
                try:
                    child.unlink()
                except OSError:
                    pass


def transcripts(projects: Iterable[str], root: Path, ours: set[str] | frozenset[str] = frozenset()) -> list[Transcript]:
    """Every conversation transcript filed under these project folders, robots left out."""
    found: list[Transcript] = []
    seen: set[str] = set()
    for project in projects:
        for folder in folders_for(project, root):
            for child in folder.glob("*.jsonl"):
                if not UUID.match(child.stem) or str(child).lower() in seen:
                    continue
                seen.add(str(child).lower())
                try:
                    stat = child.stat()
                except OSError:
                    continue
                stamp = (stat.st_size, stat.st_mtime)
                title, branch, entry = _head(child, stamp)
                if entry in ROBOT_ENTRYPOINTS and child.stem.lower() not in ours:
                    continue
                found.append(Transcript(id=child.stem, project=str(project), path=child, size=stat.st_size,
                                        updated=stat.st_mtime, title=title or "(no opening message)",
                                        branch=branch, entry=entry, last=_latest(child, stamp)))
    return found


def _marks(text: str, terms: list[str]) -> list[tuple[int, int]]:
    low = text.lower()
    spans: list[tuple[int, int]] = []
    for term in terms:
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(term) + r"[a-z0-9]*", low):
            spans.append((m.start(), m.end()))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def passage(turn: Turn, terms: list[str]) -> dict[str, Any] | None:
    """The part of a turn around its first match, with where each match is in it."""
    spans = _marks(turn.text, terms)
    if not spans:
        return None
    first = spans[0][0]
    start = max(0, first - WINDOW)
    end = min(len(turn.text), first + WINDOW + (spans[0][1] - spans[0][0]))
    # Whole words at the edges.
    if start > 0:
        space = turn.text.find(" ", start)
        start = space + 1 if 0 <= space < first else start
    if end < len(turn.text):
        space = turn.text.rfind(" ", spans[0][1], end)
        end = space if space > spans[0][1] else end
    text = turn.text[start:end].replace("\r", "")
    lead = "..." if start > 0 else ""
    tail = "..." if end < len(turn.text) else ""
    shift = len(lead) - start
    marks = [[s + shift, e + shift] for s, e in spans if s >= start and e <= end]
    return {"role": turn.role, "at": turn.at, "text": lead + text + tail, "marks": marks}


def search(items: list[Transcript], cache: Cache, query: str, now: float | None = None,
           limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """BM25 over the conversations, a phrase bonus, a little recency; best first."""
    terms = list(dict.fromkeys(stem(w) for w in words(query)))
    if not terms:
        return []
    now = now or time.time()
    phrase = " ".join(terms) if len(terms) > 1 else ""
    quoted = [q.lower() for q in re.findall(r'"([^"]+)"', query)]
    docs = []
    for item in items:
        turns = cache.turns(item.path, item.size, item.updated)
        item.turns = turns
        counts: dict[str, int] = {}
        length = 0
        joined = []
        for turn in turns:
            tokens = words(turn.text)
            length += len(tokens)
            for token in tokens:
                for term in terms:
                    if token.startswith(term):
                        counts[term] = counts.get(term, 0) + 1
            joined.append(" ".join(tokens))
        docs.append((item, counts, length, " ".join(joined)))
    if not docs:
        return []
    average = sum(d[2] for d in docs) / len(docs) or 1.0
    total = len(docs)
    df = {term: sum(1 for d in docs if d[1].get(term)) for term in terms}
    results = []
    for item, counts, length, flat in docs:
        if not counts:
            continue
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (total - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / average))
        for wanted in [phrase, *quoted]:
            if wanted:
                together = flat.count(" ".join(words(wanted)))
                if together:
                    score += 1.5 * math.log(1 + together)
        age_days = max(0.0, (now - item.updated) / 86400)
        score *= 1 + RECENCY_WEIGHT * 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
        scored_turns = []
        for turn in item.turns:
            hits = sum(1 for token in words(turn.text) for term in terms if token.startswith(term))
            if hits:
                scored_turns.append((hits, turn))
        best = sorted(scored_turns, key=lambda pair: pair[0], reverse=True)[:PASSAGES]
        passages = [p for p in (passage(turn, terms) for _hits, turn in sorted(best, key=lambda pair: pair[1].at)) if p]
        results.append({"id": item.id, "project": item.project, "title": item.title, "last": item.last,
                        "branch": item.branch, "updated": item.updated, "score": round(score, 4),
                        "matches": sum(counts.values()), "turns_matched": len(scored_turns),
                        "passages": passages})
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# ---- ranked by the model ------------------------------------------------------------------------

RANK_PROMPT = """You help a person find one of their past conversations with an AI coding assistant.
Their question: {question}

Below are candidate conversations, each with an id, its first prompt and passages from it. Choose the
ones that best answer the question, best first (at most 8), and say in one short sentence for each
why it matches. Answer with JSON only, no other text: [{{"id": "...", "why": "..."}}]

{candidates}"""


def rank_prompt(question: str, results: list[dict[str, Any]]) -> str:
    blocks = []
    for r in results[:RANK_CANDIDATES]:
        lines = [f"id: {r['id']}", f"first prompt: {r['title'][:200]}"]
        lines += [f"{p['role']}: {p['text']}" for p in r["passages"]]
        blocks.append("\n".join(lines))
    return RANK_PROMPT.format(question=question.strip()[:500], candidates="\n\n---\n\n".join(blocks))


def parse_ranking(text: str, known: set[str]) -> list[dict[str, str]]:
    """The model's [{"id", "why"}], keeping only ids that were offered. Raises ValueError otherwise."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("the answer held no list")
    data = json.loads(text[start:end + 1])
    out = [{"id": str(row.get("id")), "why": str(row.get("why") or "")[:300]}
           for row in data if isinstance(row, dict) and str(row.get("id")) in known]
    if not out:
        raise ValueError("the answer named none of the conversations")
    return out


def rank_with_model(claude: str, base_url: str, question: str, results: list[dict[str, Any]],
                    run: Callable[..., Any] = subprocess.run) -> list[dict[str, str]]:
    """One isolated model request (oneshot.ask), through this proxy. Raises RuntimeError on failure."""
    from .oneshot import ask
    text = ask(claude, base_url, rank_prompt(question, results), RANK_TIMEOUT_S, run=run)
    try:
        return parse_ranking(text, {r["id"] for r in results})
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"the AI ranking did not answer as asked: {exc}") from exc

