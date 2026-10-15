"""The agent's own instructions, kept in step with the setting that governs them (feature 36).

Before this, the Images setting and the CLAUDE.md rules block said opposite things and neither
knew about the other. Turn binaries on - they may be sent to the provider - and CLAUDE.md still
carried *"Never use the Read tool on ... any image file"*, so the agent went on refusing to look
at a screenshot the proxy would happily have passed. The setting governs the pipe; the rule
governs the agent; only the rule is in front of the model, so the rule won and the setting
appeared to do nothing.

One control now. Binaries allowed -> the file-handling rule is **not** in CLAUDE.md. Binaries
withheld -> it **is**. Flipping the setting rewrites the block.

**Writing a person's own agent instructions from a settings page is a surprising thing for a
proxy to do**, and it is done deliberately (decided 2026-09-27): this application is deployed by
an individual on their own device, so a decision made in its interface carries the same weight as
any other change to that environment. It is done with the installer's care all the same - back up
first, touch only between the sentinels, and refuse outright on a file whose sentinels are
missing or crossed, because a half-matched region is how a merge eats somebody's notes.

All binaries or none, not a permission per file type. The block therefore has one always-on part
(the preamble and the Identifiers rule, which must survive whatever the toggle says) and one
conditional part, marked in the template.

Scope is the **user-level** file. Per-repository rules are the v1.1 shape and are blocked on
defect D41 - in a workspace of many repositories, nothing here yet knows which one is primary.
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("aicleaner.rules")

START = "<!-- aicleaner:start -->"
END = "<!-- aicleaner:end -->"
BIN_START = "<!-- aicleaner:binaries:start -->"
BIN_END = "<!-- aicleaner:binaries:end -->"
READ_START = "<!-- aicleaner:reading:start -->"
READ_END = "<!-- aicleaner:reading:end -->"

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "CLAUDE.md"


class RulesError(RuntimeError):
    """The block could not be written, with a reason worth showing."""


def user_claude_md(home: Path | None = None) -> Path:
    """The user-level CLAUDE.md. Honours AICLEANER_CLIENT_HOME, as `routing` does, so a test
    server never rewrites the instructions of whoever happens to be running it."""
    override = os.environ.get("AICLEANER_CLIENT_HOME", "").strip()
    base = home or (Path(override) if override else Path.home())
    return base / ".claude" / "CLAUDE.md"


def block_text(allow_binaries: bool, template: Path | None = None, reading: bool = False) -> str:
    """The block as it should read for this setting, sentinels included.

    Three states (the user, 2026-10-14): binaries sent -> no file rule; withheld but read on this
    machine (`reading`: an OCR engine is present) -> Read images and PDFs directly, the proxy turns
    them into text; withheld and unread -> convert everything first. A session told never to read
    an image refused screenshots the proxy would have turned into text."""
    source = Path(template or TEMPLATE)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RulesError(f"the rules template could not be read: {exc}") from exc
    start, end = text.find(BIN_START), text.find(BIN_END)
    if allow_binaries and (start < 0 or end < start):
        # The template is ours, so this is a packaging fault rather than a user one. Keeping the
        # whole block is the safe direction: the agent goes on converting locally, which works
        # whatever the setting says.
        log.warning("the rules template has no binaries region; leaving the rule in place")
        return _strip_markers(_cut(text, READ_START, READ_END))
    if allow_binaries:
        text = _cut(_cut(text, BIN_START, BIN_END), READ_START, READ_END)
    elif reading and text.find(READ_START) >= 0:
        text = _cut(text, BIN_START, BIN_END)
    else:
        text = _cut(text, READ_START, READ_END)
    return _strip_markers(text).replace("\n\n\n", "\n\n")


def _cut(text: str, start_tag: str, end_tag: str) -> str:
    """The text without one marked region, markers and all."""
    start, end = text.find(start_tag), text.find(end_tag)
    if start < 0 or end < start:
        return text
    return text[:start] + text[end + len(end_tag):]


def _strip_markers(text: str) -> str:
    for tag in (BIN_START, BIN_END, READ_START, READ_END):
        text = text.replace(tag + "\n", "").replace(tag, "")
    return text.strip() + "\n"


def apply(allow_binaries: bool, path: Path | None = None, template: Path | None = None,
          reading: bool = False, only_if_present: bool = False) -> str:
    """Rewrite the block in `path` for this setting. Returns what happened, for the UI to say.

    Creates the file when it is absent - a machine with no user-level CLAUDE.md still wants the
    rule when binaries are withheld - unless `only_if_present`: the check at every start keeps a
    block the installer wrote in step, and never adds one somebody declined. Never touches a byte
    outside the sentinels.
    """
    target = Path(path or user_claude_md())
    wanted = block_text(allow_binaries, template, reading=reading)
    try:
        existing = target.read_text(encoding="utf-8-sig") if target.exists() else ""
    except OSError as exc:
        raise RulesError(f"{target} could not be read: {exc}") from exc
    if only_if_present and START not in existing:
        return ""

    start, end = existing.find(START), existing.find(END)
    if (start < 0) != (end < 0) or (start >= 0 and end < start):
        raise RulesError(
            f"{target.name} has a damaged aicleaner region, so it was left alone. "
            "Repair the <!-- aicleaner:start --> and <!-- aicleaner:end --> markers by hand.")

    if start >= 0:
        if existing[start:end + len(END)].strip() == wanted.strip():
            return "no change: the rules already match the setting"
        updated = existing[:start] + wanted.strip() + existing[end + len(END):]
    else:
        joiner = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        updated = existing + joiner + wanted

    _back_up(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"{target} could not be written: {exc}") from exc
    if allow_binaries:
        return f"binaries may now be sent, so the file-handling rule was removed from {target.name}"
    if reading:
        return (f"images and PDFs are read on this machine, so {target.name} now lets sessions Read "
                "them directly (only their text is sent)")
    return f"binaries are withheld, so the file-handling rule was written into {target.name}"


def _back_up(target: Path) -> None:
    """A copy beside the original before it is touched, as the installer does."""
    if not target.exists():
        return
    stamped = target.with_name(f"{target.name}.{datetime.now():%Y%m%dT%H%M%S}.bak")
    try:
        shutil.copy2(target, stamped)
    except OSError as exc:     # a backup that cannot be taken is worth saying, not worth stopping for
        log.warning("could not back up %s: %s", target, exc)
