"""What can usefully be typed after a slash, gathered from the files that define it.

A session driven from this page runs `claude --print`, which is not a terminal. Claude Code
expands a skill or a custom command named in the prompt before the turn runs, so those work.
A built-in that exists only in the terminal interface - /resume, /plugin, /login - does not,
and neither does anything that opens a picker.

So this does not mirror the menu a terminal shows. It reads what is actually on this machine:
a command is a Markdown file under a commands folder, a skill is a SKILL.md, and the sentence
shown beside each one is the description in its own front matter. The handful of built-ins
listed here are the ones Anthropic documents as working with --print, and they are marked as
wanting a value where they need one, because without it they would open a picker that nobody
can see. Nothing here runs anything.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

MAX_DESCRIPTION = 160
MAX_ENTRIES = 400                # a plugin folder gone wrong should not fill the page

# Documented as working in --print mode. Each takes its value as an argument, because the
# terminal's picker or slider is not there to open.
BUILT_INS: tuple[tuple[str, str, bool], ...] = (
    ("/model", "Run the rest of the session on another model, for example /model sonnet", True),
    ("/effort", "How hard it thinks, for example /effort high", True),
    ("/fast", "Turn fast mode on or off", True),
    ("/output-style", "Switch output style; on its own it lists them", False),
    ("/config", "Change one setting, written as /config key=value", True),
    ("/rename", "Give this conversation a name", True),
    ("/mcp", "What each MCP server's status is", False),
    ("/color", "Change the colour scheme", True),
    # Tried headless 2026-10-04 (feature 84): each answers locally, at no cost, as a reply. Not
    # /usage-credits or /extra-usage: they open a browser on the machine running the session,
    # which from a phone is somebody else's screen.
    ("/clear", "Start the conversation afresh: what was said so far is no longer in context", False),
    # Run from this page by the user 2026-10-14: it summarised the session and carried on. Send it
    # while the session is idle; sent mid-turn it waits in the queue like any message.
    ("/compact", "Summarise the conversation so far to free context; add instructions to steer the summary", False),
    ("/context", "How full the context is, by category", False),
    ("/usage", "How much of the plan's session and weekly limits is used", False),
    ("/autocompact", "When the conversation is summarised to free context", False),
    ("/goal", "A condition to keep working towards, for example /goal all tests pass", True),
    ("/advisor", "A second model to consult: fable, opus, sonnet or off", True),
    ("/reload-skills", "Pick up skills added or changed since the session started", False),
    ("/reload-plugins", "Pick up plugins added or changed since the session started", False),
    # Named by a headless session's own init event (Claude Code 2.1.289, 2026-10-14): the commands
    # and bundled skills it accepts without a terminal. Descriptions from Anthropic's reference.
    ("/agents", "How to create or manage subagents", False),
    ("/auto-mode-setup", "Draft auto mode's environment entries from this project and recent sessions", False),
    ("/batch", "Plan a large change, split it into independent units and run them in parallel", True),
    ("/claude-api", "Load the Claude API and Managed Agents reference for this project's language", False),
    ("/code-review", "Review the current diff, or a PR, branch or path, for bugs; --fix applies the findings", False),
    ("/dataviz", "Design guidance for charts, graphs and dashboards", False),
    ("/debug", "Turn on debug logging for this session and troubleshoot from the log", False),
    ("/deep-research", "Search the web on a question, cross-check sources and write a cited report", True),
    ("/design", "Draft UI mockups or screen flows as a Claude Design artifact", True),
    ("/design-sync", "Upload this repo's design system to Claude Design", False),
    ("/doctor", "Check the installation, settings and CLAUDE.md, and propose fixes", False),
    ("/fewer-permission-prompts", "Add an allowlist of common read-only tool calls to the project settings", False),
    ("/init", "Write a CLAUDE.md guide for this project", False),
    ("/insights", "An HTML report on recent sessions on this machine", False),
    ("/list-agents", "The subagents, teammates and other sessions Claude can message", False),
    ("/loop", "Run a prompt repeatedly, at an interval or self-paced", False),
    ("/plugin-authoring", "Load the reference for writing a plugin mod", False),
    ("/recap", "A one-line summary of this session", False),
    ("/run", "Launch and drive the project's app to see a change working", False),
    ("/run-skill-generator", "Teach /run and /verify how to build and launch this project", False),
    ("/schedule", "Create, list or run routines that execute in the cloud", False),
    ("/security-review", "Review this branch's changes for security vulnerabilities", False),
    ("/simplify", "Review the changed code for cleanup and apply the fixes", False),
    ("/team-onboarding", "A teammate onboarding guide from the last 30 days of usage", False),
    ("/update-config", "Describe a settings change and have it made in settings.json", False),
    ("/verify", "Build and run the app to confirm a change does what it should", False),
    ("/workflow-authoring", "Load the reference for writing dynamic workflow scripts", False),
)

# Named by a headless session but not offered: /usage-credits and /extra-usage open a browser on the
# machine running the session (from a phone, somebody else's screen); /heapdump writes a memory dump
# to that machine's Desktop; /focus changes a terminal view nobody here sees; the rest are internal.
HIDDEN = frozenset({"usage-credits", "extra-usage", "heapdump", "focus", "design-consent", "design-revoke",
                    "workflow-launch-exec"})


@dataclass(frozen=True)
class Command:
    """One thing that can be typed after a slash."""
    name: str
    kind: str                    # command, skill or built-in
    description: str
    source: str                  # project, user, plugin name, or built-in
    takes_value: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def front_matter(text: str) -> dict[str, str]:
    """The simple `key: value` lines between the opening and closing --- of a file.

    Deliberately not a YAML parser: what is wanted is a description and a name, and a
    dependency for that would be a poor trade.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            found[key.strip().lower()] = value.strip().strip("'\"")
    return found


def _described(path: Path) -> str:
    """The file's own description, or its first line of prose."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    described = front_matter(text).get("description", "")
    if not described:
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        for line in body.splitlines():
            stripped = line.strip().lstrip("# ").strip()
            if stripped:
                described = stripped
                break
    return described[:MAX_DESCRIPTION]


def commands_in(folder: Path, source: str, prefix: str = "") -> list[Command]:
    """Every Markdown file under a commands folder, named as it would be typed.

    A file in a subfolder is namespaced the way Claude Code namespaces it, so
    commands/review/api.md is /review:api.
    """
    found: list[Command] = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.rglob("*.md")):
        parts = list(path.relative_to(folder).with_suffix("").parts)
        name = "/" + ":".join(([prefix] if prefix else []) + parts)
        found.append(Command(name=name, kind="command", description=_described(path), source=source))
    return found


def skills_in(folder: Path, source: str, prefix: str = "") -> list[Command]:
    """Every skill folder holding a SKILL.md, named as it would be invoked."""
    found: list[Command] = []
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*/SKILL.md")):
        named = front_matter(path.read_text(encoding="utf-8", errors="replace")).get("name", "")
        name = named or path.parent.name
        found.append(Command(name="/" + (f"{prefix}:{name}" if prefix else name),
                             kind="skill", description=_described(path), source=source))
    return found


def _plugin_roots(home: Path) -> list[tuple[str, Path]]:
    """The plugins that are installed, by the path their own record gives.

    The cache holds every version that was ever fetched, so the folder listing is not the
    answer: installed_plugins.json says which one is in use.
    """
    record = home / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    roots: list[tuple[str, Path]] = []
    for full, installs in (data.get("plugins") or {}).items():
        short = str(full).split("@", 1)[0]
        for install in installs if isinstance(installs, list) else []:
            path = str((install or {}).get("installPath") or "")
            if path:
                roots.append((short, Path(path)))
                break
    return roots


def _from_session(offered: Iterable[str]) -> list[Command]:
    """The names the running session accepts that the lists above do not have - a plugin's skill
    from a store this scan does not read, an MCP server's prompt, a built-in added by a newer
    Claude Code. A headless session names only what it accepts without a terminal (/login, /resume
    and /plugin are not among them; checked 2026-10-14), so a bare name is offered too, unless it is
    one of HIDDEN or internal (a leading underscore)."""
    return [Command(name=f"/{name}", kind="built-in" if ":" not in name and not name.startswith("mcp__")
                    else "command", description="", source="session")
            for name in offered if name not in HIDDEN and not name.startswith("_")]


def available(projects: Iterable[str | Path] = (), home: str | Path = "",
              offered: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Everything that can be typed after a slash in a session driven from this page.

    Listed alphabetically (the user, 2026-10-04). Precedence still decides which definition of a
    name is kept - a project's own first, then the person's, then the plugins', then the
    built-ins - because the first is the one Claude Code would use.

    `offered` is what the running session named in its `init` event (feature 84). When there is
    one, a built-in it does not name is not offered - a CLI update that drops one leaves no dead
    entry - and its namespaced names the disk scan missed are added last.
    """
    named = {name.lower() for name in offered} if offered else None
    base = Path(home) if home else Path.home() / ".claude"
    found: list[Command] = []
    for project in projects:
        root = Path(project) / ".claude"
        found += commands_in(root / "commands", "project")
        found += skills_in(root / "skills", "project")
    found += commands_in(base / "commands", "user")
    found += skills_in(base / "skills", "user")
    for name, root in _plugin_roots(base):
        found += commands_in(root / "commands", name, prefix=name)
        found += skills_in(root / "skills", name, prefix=name)
    found += [Command(name=name, kind="built-in", description=text, source="built-in",
                      takes_value=wants) for name, text, wants in BUILT_INS
              if named is None or name[1:].lower() in named]
    found += _from_session(offered or ())
    seen: set[str] = set()
    kept: list[dict[str, Any]] = []
    for item in found:
        if item.name.lower() in seen or len(kept) >= MAX_ENTRIES:
            continue
        seen.add(item.name.lower())
        kept.append(item.as_dict())
    return sorted(kept, key=lambda row: row["name"].lower())
