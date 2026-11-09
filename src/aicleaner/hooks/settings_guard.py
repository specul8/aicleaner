"""Claude Code PreToolUse hook: the model does not change AI Cleaner's own settings (feature 70, AI Cleaner 1.1).

Brad, 2026-09-29: "setting controls blocked remotely cannot be changed by claude himself." AI Cleaner's settings are
changed by a person, on its Settings page. This stops a tool call that would change them some other way: writing the
installed config file, reading or sending the API key (H2), or taking this guard out of Claude Code's settings. It is
aimed at a helpful model "fixing" a setting on its own initiative; a process running as the user can always write
the user's files.

Run as: python settings_guard.py <install folder> <state folder>. Standard library only: Claude Code runs it with AI
Cleaner's own Python. Anything it cannot read is let through - a broken guard must not stop all tool use.
Spec: specs/2026-11-08-f70-model-cannot-change-settings.md (tooling repository).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MARKER = "settings_guard.py"
EDITS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELLS = ("Bash", "PowerShell")

READS_KEY = re.compile(r"Unprotect|ProtectedData|read_secret|Get-Content|\bgc\b|\bcat\b|\btype\b|Copy-Item|\bcp\b|"
                       r"\bcopy\b|\[IO\.File\]|open\(|read_bytes|read_text", re.I)
HTTP_CLIENT = re.compile(r"\bcurl\b|\bwget\b|Invoke-RestMethod|Invoke-WebRequest|\birm\b|\biwr\b|urllib|requests\.|"
                         r"httpx|http\.client|aiohttp|fetch\(", re.I)
WRITES = re.compile(r"Set-Content|Out-File|Add-Content|(?<![<>=-])>(?!=)|sed\s+-i|Copy-Item|Move-Item|Remove-Item|"
                    r"\bcp\s|\bmv\s|\brm\s|\btee\b|WriteAllText|write_text|write_bytes|json\.dump|\bdel\s", re.I)

ADVICE = ("AI Cleaner blocked this tool call: {what}. AI Cleaner's settings are changed by the person, on its own "
          "Settings page - not by Claude (feature 70). If a setting needs changing, say which and why, and leave it "
          "to them.")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def _within(path: str, folder: Path) -> bool:
    target, base = _norm(path), _norm(str(folder))
    return target == base or target.startswith(base.rstrip("\\/") + os.sep)


def _claude_settings(path: str) -> bool:
    """Any of Claude Code's settings files: the user's, a project's, or a local one (settings*.json under .claude)."""
    parts = Path(_norm(path)).parts
    return len(parts) >= 2 and parts[-2] == ".claude" and parts[-1].startswith("settings") and parts[-1].endswith(".json")


# Claude Code's switch that turns every hook off, this guard with them (security review, 2026-11-08).
ALL_HOOKS_OFF = "disableallhooks"


def _after(tool: str, given: dict, path: str) -> str | None:
    """The settings file as it would be after this call: the Write's content, or the file with the Edit applied the
    way Claude Code applies it. None when it cannot be worked out (the file unreadable, an old_string not found)."""
    if tool == "Write":
        return str(given.get("content") or "")
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8-sig")
    except OSError:
        text = ""
    for edit in (given.get("edits") if tool == "MultiEdit" else [given]) or []:
        if not isinstance(edit, dict):
            return None
        old, new = str(edit.get("old_string") or ""), str(edit.get("new_string") or "")
        if old not in text:
            return None
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def _settings_change(tool: str, given: dict, path: str) -> str:
    """Read as JSON, not as text (security review, 2026-11-08): a key spelled with escapes, or a false turned true,
    is caught the same way. A result that is not a settings object is left to Claude Code to refuse."""
    off = "it would turn off every Claude Code hook, AI Cleaner's settings guard among them"
    removed = "it would take AI Cleaner's settings guard out of Claude Code's settings"
    guarded = _guard_on_disk(path)
    # The edit's own text, whatever the simulation makes of it (third security review): a new string that names the
    # switch, or one that drops the guard an old string named.
    for edit in ([given] if tool == "Write" else (given.get("edits") if tool == "MultiEdit" else [given])) or []:
        if not isinstance(edit, dict):
            continue
        new = str(edit.get("content") if tool == "Write" else edit.get("new_string") or "")
        old = str(edit.get("old_string") or "")
        if ALL_HOOKS_OFF in new.lower() and ALL_HOOKS_OFF not in old.lower():
            try:                                   # a Write naming it set false is fine; any other is not
                if tool != "Write" or json.loads(new).get("disableAllHooks"):
                    return off
            except (ValueError, AttributeError, RecursionError):
                return off
        if MARKER in old and MARKER not in new:
            return removed
    after = _after(tool, given, path)
    try:
        doc = json.loads(after) if after is not None and after.strip() else ({} if after is not None else None)
    except (ValueError, RecursionError):           # RecursionError: nested past the parser's depth (fourth review)
        doc = None
    if not isinstance(doc, dict):
        # Cannot tell what the file would be: refused where the guard is in it (fail closed), else left to Claude Code.
        return removed if guarded else ""
    if doc.get("disableAllHooks"):
        return off
    # The guard's own entries, compared whole (fourth review): kept by name but changed - another matcher, another
    # command - is as good as removed.
    if guarded and _guard_entries(doc) != _guard_entries(_on_disk(path)):
        return removed
    return ""


def _on_disk(path: str) -> dict:
    try:
        doc = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _guard_entries(doc: dict) -> list:
    """Every PreToolUse group that runs this guard, as written."""
    groups = ((doc.get("hooks") or {}).get("PreToolUse") or []) if isinstance(doc.get("hooks"), dict) else []
    return [group for group in groups if isinstance(group, dict) and MARKER in json.dumps(group)]


def _guard_on_disk(path: str) -> bool:
    try:
        return MARKER in Path(path).expanduser().read_text(encoding="utf-8-sig")
    except OSError:
        return False


def _edit(tool: str, given: dict, install: Path, state: Path) -> str:
    path = str(given.get("file_path") or given.get("notebook_path") or "")
    if not path:
        return ""
    config = install / "aicleaner-config.json"
    if _norm(path) in (_norm(str(config)), _norm(str(config) + ".tmp")):
        return "it would change AI Cleaner's settings file directly"
    if _within(path, state / "secrets"):
        return "it would change AI Cleaner's secrets (its API key among them)"
    # The installed app - this guard and the code that reads the settings - is Deploy's to change, not a tool call's.
    if _within(path, Path(__file__).resolve().parents[2]):
        return "it would change AI Cleaner's installed program, this guard among it"
    if _claude_settings(path) and tool in ("Write", "Edit", "MultiEdit"):
        return _settings_change(tool, given, path)
    return ""


CLAUDE_SETTINGS = re.compile(r"\.claude[\\/]+settings[\w.-]*\.json", re.I)

# Output thrown away or joined (2>/dev/null, 2>&1, > $null) is not a write.
DISCARDED = re.compile(r"\d?>&\d|\d?>\s*(?:/dev/null|\$null|NUL\b)", re.I)


def _shell(command: str) -> str:
    command = DISCARDED.sub(" ", command)
    if "api-key.dpapi" in command and READS_KEY.search(command):
        return "it would read AI Cleaner's API key"
    if re.search(r"X-AI-Cleaner-Key|AICLEANER_API_KEY", command, re.I) and HTTP_CLIENT.search(command):
        return "it would call AI Cleaner's API with its key"
    if "aicleaner-config.json" in command and WRITES.search(command):
        return "it would change AI Cleaner's settings file"
    # Only a command naming one of Claude Code's settings files: work on this guard's own code (test_settings_guard.py,
    # a .claude folder in a repository) names the same words.
    names_settings = CLAUDE_SETTINGS.search(command) is not None
    if names_settings and ALL_HOOKS_OFF in command.lower() and WRITES.search(command):
        return "it would turn off every Claude Code hook, AI Cleaner's settings guard among them"
    if names_settings and re.search(r"(?<![\w-])settings_guard\.py", command) and WRITES.search(command):
        return "it would take AI Cleaner's settings guard out of Claude Code's settings"
    return ""


def verdict(payload: object, install: Path, state: Path) -> str:
    """Why the call is refused, or '' to let it through."""
    if not isinstance(payload, dict):
        return ""
    tool = payload.get("tool_name")
    given = payload.get("tool_input")
    if not isinstance(given, dict):
        return ""
    if tool in EDITS:
        return _edit(str(tool), given, install, state)
    if tool in SHELLS and isinstance(given.get("command"), str):
        return _shell(given["command"])
    return ""


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except ValueError:
        return 0
    try:
        why = verdict(payload, Path(argv[1]), Path(argv[2]))
    except Exception:  # noqa: BLE001 - a broken guard lets the call through rather than stopping all tool use
        return 0
    if why:
        sys.stderr.write(ADVICE.format(what=why) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
