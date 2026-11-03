"""A sub-agent's report, for the Session page (2026-10-16; spec 2026-10-16-a-subagent-reports-design.md).

Claude Code keeps each sub-agent's own transcript beside the conversation that started it -
`<transcript folder>\\<conversation id>\\subagents\\agent-<agentId>.jsonl` - and a `.meta.json` beside it
naming the Agent tool call that started it (`toolUseId`). The report is the agent's last word: a
`SubagentHandback` call's message, or the plain text it ended with.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .history import folders_for

TOOL_ID = re.compile(r"\Atoolu_[A-Za-z0-9]{6,64}\Z")
MAX_REPORT = 200_000
TAIL_BYTES = 2_000_000
_CACHE: dict[str, tuple[tuple[int, float], dict[str, Any]]] = {}


def find_meta(projects: Iterable[str], tool_id: str, root: Path, prefer: str = "") -> Path | None:
    """The .meta.json naming this tool call, among the profile's conversation folders."""
    if not TOOL_ID.match(tool_id or ""):
        raise ValueError("not a tool call id")
    folders = [f for project in projects for f in folders_for(project, root)]
    patterns = ([f"{prefer}/subagents/*.meta.json"] if prefer else []) + ["*/subagents/*.meta.json"]
    for pattern in patterns:
        for folder in folders:
            for meta in folder.glob(pattern):
                try:
                    if json.loads(meta.read_text(encoding="utf-8")).get("toolUseId") == tool_id:
                        return meta
                except (OSError, ValueError):
                    continue
    return None


def _last_word(path: Path) -> tuple[str, bool]:
    """(the report, finished) from the end of the agent's transcript."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        blocks = (record.get("message") or {}).get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SubagentHandback":
                return str((block.get("input") or {}).get("message") or ""), True
        uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        # An assistant turn that still calls tools is the agent at work, not its last word.
        return (text, True) if text and not uses else ("", False)
    return "", False


def report(projects: Iterable[str], tool_id: str, root: Path, prefer: str = "") -> dict[str, Any]:
    meta = find_meta(projects, tool_id, root, prefer)
    if meta is None:
        return {"found": False, "reason": "no report for this agent was found in this profile's folders"}
    try:
        about = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        about = {}
    transcript = meta.with_name(meta.name[: -len(".meta.json")] + ".jsonl")
    base = {"description": str(about.get("description") or ""), "agent_type": str(about.get("agentType") or "")}
    try:
        stat = transcript.stat()
    except OSError:
        return {**base, "found": False, "reason": "the agent's transcript is not there"}
    stamp = (stat.st_size, stat.st_mtime)
    held = _CACHE.get(str(transcript))
    if held and held[0] == stamp:
        return held[1]
    text, finished = _last_word(transcript)
    if not finished:
        answer = {**base, "found": False, "status": "running", "reason": "not finished yet"}
    else:
        cut = len(text) > MAX_REPORT
        answer = {**base, "found": True, "status": "done", "words": len(text.split()),
                  "report": text[:MAX_REPORT] + ("\n\n[cut at 200 000 characters]" if cut else "")}
        _CACHE[str(transcript)] = (stamp, answer)
    return answer
