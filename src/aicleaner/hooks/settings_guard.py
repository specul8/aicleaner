"""Claude Code PreToolUse hook: the model does not change AI Cleaner's own settings (feature 70, AI Cleaner 1.1).

Brad, 2026-09-29: "setting controls blocked remotely cannot be changed by claude himself." AI Cleaner's settings are
changed by a person, on its Settings page. This stops a tool call that would change them some other way: writing the
installed config file, reading or sending the API key (H2), or taking this guard out of Claude Code's settings - and
(f185) changing which add-ons are loaded (`aicleaner-addons.json`), an add-on's code or the secrets and state it names,
or reading any DPAPI-protected secret file. D247 (1.3.1): a shell command naming the registry, an add-on's paths
or AI Cleaner's folder itself is refused unless it only reads (`_read_only`), however the name is spelled
(`_spelled`) - and the registry is sealed, so one rewritten by a route this guard misses is not loaded. It is
aimed at a helpful model "fixing" a setting on its own initiative; a process running as the user can always write
the user's files.

Run as: python settings_guard.py <install folder> <state folder>. Standard library only: Claude Code runs it with AI
Cleaner's own Python. Anything it cannot read is let through - a broken guard must not stop all tool use.
Spec: specs/2026-11-08-f70-model-cannot-change-settings.md (tooling repository).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
from pathlib import Path

MARKER = "settings_guard.py"
EDITS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELLS = ("Bash", "PowerShell")

READS_KEY = re.compile(r"Unprotect|ProtectedData|read_secret|Get-Content|\bgc\b|\bcat\b|\btype\b|Copy-Item|\bcp\b|"
                       r"\bcopy\b|\[IO\.File\]|open\(|read_bytes|read_text|Format-Hex|\bxxd\b|\bbase64\b|"
                       r"\bcertutil\b|\bhexdump\b|\bod\s|ReadAll", re.I)
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


REGISTRY = "aicleaner-addons.json"
REGISTRY_STEM = "aicleaner-addons"          # the registry, its seal (.seal) and their temporaries (.tmp)


def _addon_paths(state: Path) -> list[Path]:
    """f185: every registered add-on's code folder (`root`) and the secrets and state it names (`protect`). A registry
    that cannot be read adds nothing - the guard lets calls through rather than stopping all tool use; AI Cleaner
    itself loads no add-on from such a file."""
    try:
        data = json.loads((state / REGISTRY).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return []
    found: list[Path] = []
    for entry in (data.get("addons") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        for value in [entry.get("root"), *(entry.get("protect") or [])]:
            if isinstance(value, str) and value.strip() and Path(value).is_absolute():
                found.append(Path(value))
    return found


def _registry_file(path: str, state: Path) -> bool:
    """The registry, its seal or a temporary of either - by name, in AI Cleaner's folder - whatever the case, slashes
    or short (8.3) form of the path (D247)."""
    for form in {_norm(path), _norm(os.path.realpath(os.path.expanduser(path)))}:
        folder, name = os.path.split(form)
        if name.startswith(REGISTRY_STEM) and folder in (_norm(str(state)), _norm(os.path.realpath(str(state)))):
            return True
    return False


def _edit(tool: str, given: dict, install: Path, state: Path) -> str:
    path = str(given.get("file_path") or given.get("notebook_path") or "")
    if not path:
        return ""
    config = install / "aicleaner-config.json"
    if _norm(path) in (_norm(str(config)), _norm(str(config) + ".tmp")):
        return "it would change AI Cleaner's settings file directly"
    if _within(path, state / "secrets") or _within(os.path.realpath(os.path.expanduser(path)), state / "secrets"):
        return "it would change AI Cleaner's secrets (its API key among them)"
    # f185: which add-ons AI Cleaner loads, and each one's code, secrets and state.
    if _registry_file(path, state):
        return "it would change which add-ons AI Cleaner loads"
    if any(_within(path, folder) for folder in _addon_paths(state)):
        return "it would change an AI Cleaner add-on's code, secrets or state"
    # The installed app - this guard and the code that reads the settings - is Deploy's to change, not a tool call's.
    if _within(path, Path(__file__).resolve().parents[2]):
        return "it would change AI Cleaner's installed program, this guard among it"
    if _claude_settings(path) and tool in ("Write", "Edit", "MultiEdit"):
        return _settings_change(tool, given, path)
    return ""


CLAUDE_SETTINGS = re.compile(r"\.claude[\\/]+settings[\w.-]*\.json", re.I)

# Output thrown away or joined (2>/dev/null, 2>&1, > $null) is not a write.
DISCARDED = re.compile(r"\d?>&\d|\d?>\s*(?:/dev/null|\$null|NUL\b)", re.I)


DPAPI_FILE = re.compile(r"\.dpapi\b", re.I)


def _names(command: str, folder: Path) -> bool:
    """Whether a command names this folder (or something in it), with either kind of slash, in any case - the command
    as `_spelled` (environment-variable and ~ forms written out)."""
    text = _spelled(command)
    want = _slashed(str(folder)).rstrip("/")
    return bool(want) and want in text


# ---- D247: what a shell command names, however it is spelled ----------------------------------------------------------
#
# The add-on rules used to refuse a command naming the registry *with a known write verb*: a verb the list did not
# know (Rename-Item, [IO.File]::Move, os.replace, ...) went through. Now a command naming the registry (or an add-on's
# paths, or AI Cleaner's folder itself) is refused unless it is clearly read-only (`_read_only`: every command in it a
# reading one). How it is named is read generously (`_spelled`): case, slashes, $env:/%VAR%/~ forms, strings joined
# with +, PowerShell's backtick and cmd's caret escapes, -EncodedCommand, 8.3 short names and wildcards. A name built
# at run time (a variable, a script file) is beyond any command reader; the registry's seal (addonseal.py) is what
# stops such a registry being loaded.

ENV_NAMES = ("USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME", "TEMP", "TMP", "PROGRAMDATA")
ENCODED = re.compile(r"\s-e(?:nc(?:odedcommand)?|c)?\s+([A-Za-z0-9+/=]{16,})", re.I)
SHORT_NAME = re.compile(r"aiclea~\d")                    # the 8.3 form of .aicleaner and of aicleaner-addons.json
# Running the seal helper, or calling it from Python: a command that only names its file (grep, git add) is not this.
SEAL_HELPER = re.compile(r"addonseal\S*\s+seal\b|import\s+addonseal|from\s+\S*addonseal|\bseal\(|sealed_or_why\(",
                         re.I)


def _slashed(text: str) -> str:
    text = text.lower().replace("\\", "/")
    text = re.sub(r"(?<![\w.:])/mnt/([a-z])/", r"\1:/", text)
    text = re.sub(r"(?<![\w.:/])/([a-z])/", r"\1:/", text)        # Git Bash's /c/ is c:/
    return re.sub(r"/{2,}", "/", text)


def _env_value(name: str) -> str:
    if name.upper() == "HOME":
        return os.environ.get("HOME") if os.environ.get("HOME", "").find(":") > 0 else str(Path.home())
    return os.environ.get(name.upper(), "")


def _spelled(command: str) -> str:
    """The command as the shell would see its names: decoded, unescaped, joined, variables written out, lower case,
    forward slashes. Only for finding names - never run."""
    text = command
    for encoded in ENCODED.findall(command):
        try:
            import base64
            text += " " + base64.b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-16-le", "ignore")
        except ValueError:
            pass
    text = text.replace("`", "").replace("^", "")
    text = re.sub(r"""['"]\s*\+\s*['"]""", "", text)                # 'aicleaner-' + 'addons.json'
    text = re.sub(r"""(?<=\w)['"]{2}(?=\w)""", "", text)             # aicleaner-''addons.json
    for name in ENV_NAMES:
        value = _env_value(name)
        if not value:
            continue
        for form in (rf"\$\{{env:{name}\}}", rf"\$env:{name}\b", rf"%{name}%", rf"\$\{{{name}\}}", rf"\${name}\b"):
            text = re.sub(form, lambda _m, v=value: v, text, flags=re.I)
    text = re.sub(r"\[environment\]::getfolderpath\(\s*['\"]?userprofile['\"]?\s*\)",
                  lambda _m: str(Path.home()), text, flags=re.I)
    text = re.sub(r"(?<![\w~.-])~(?=[\\/]|$|[\s'\"])", lambda _m: str(Path.home()), text)
    return _slashed(text)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\s'\"(),;|&<>=]+", text)


def _names_state(text: str, state: Path) -> bool:
    """AI Cleaner's folder (or anything in it), spelled any way `_spelled` reads, or as a wildcard."""
    own = _slashed(str(state)).rstrip("/")
    if (own and own in text) or re.search(r"(?<![\w-])\.aicleaner(?![\w.-])", text) or SHORT_NAME.search(text):
        return True
    # A wildcard folder name that matches it and spells at least two of its letters (`.aic*`, `.ai?leaner`; not `*`).
    return any(fnmatch.fnmatchcase(".aicleaner", part) for token in _tokens(text) if any(c in token for c in "*?[")
               for part in token.split("/")
               if any(c in part for c in "*?[") and len(re.sub(r"[*?\[\]]", "", part)) >= 2)


def _top_of_state(text: str, state: Path) -> bool:
    """The folder itself, or a file directly in it - where a file could take the registry's place."""
    own = re.escape(_slashed(str(state)).rstrip("/"))
    end = r"(?:/[^/\s'\"]*)?/?(?=$|[\s'\";|&)])"
    return bool(re.search(rf"(?:{own}|(?<![\w-])\.aicleaner|aiclea~\d)" + end, text))


def _names_registry(text: str, state: Path, in_state: bool) -> bool:
    """The registry or its seal: by name, by short (8.3) name, or by a wildcard that matches it - in AI Cleaner's
    folder, or with "addon" in it."""
    if REGISTRY_STEM in text or SHORT_NAME.search(text):
        return True
    targets = (REGISTRY, REGISTRY + ".seal")
    for token in _tokens(text):
        if not any(c in token for c in "*?["):
            continue
        last = token.rstrip("/").split("/")[-1]
        if not any(fnmatch.fnmatchcase(t, last) for t in targets):
            continue
        if "addon" in re.sub(r"[*?\[\]]", "", last) or in_state or _names_state(token, state) \
                or _names_state(text, state):
            return True
    return False


# Commands that only read. A command naming the registry or an add-on's paths passes only when every command in it is
# one of these (and nothing is redirected into a file).
PS_READ = {"gc", "gci", "gi", "gp", "gcm", "ls", "dir", "cat", "type", "sls", "select", "where", "?", "fl", "ft",
           "fw", "measure", "sort", "group", "echo", "write", "pwd", "cd", "sl", "pushd", "popd", "set-location",
           "push-location", "pop-location", "where-object", "sort-object", "group-object", "compare-object",
           "out-string", "out-host", "write-output", "write-host", "resolve-path", "join-path", "split-path",
           "convert-path", "findstr", "jq", "rg", "grep", "exit"}
PS_READ_VERB = re.compile(r"^(get|test|select|format|measure|convertfrom|convertto)-\w+$")
PS_FOREACH = {"foreach-object", "foreach", "%"}
BASH_READ = {"cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "wc", "ls", "dir", "stat",
             "file", "jq", "echo", "printf", "test", "[", "[[", "pwd", "cd", "pushd", "popd", "sha256sum", "sha1sum",
             "md5sum", "diff", "cmp", "realpath", "readlink", "basename", "dirname", "true", "false", "exit", "find",
             "tree", "du", "df", "column", "cut", "tr", "type", "findstr"}
GIT_READ = {"log", "show", "diff", "status", "grep", "blame", "rev-parse", "ls-files", "commit", "add"}
STATIC_READ = {"readalltext", "readallbytes", "readalllines", "readlines", "exists", "getfolderpath",
               "getenvironmentvariable", "combine", "getfilename", "getfilenamewithoutextension", "getdirectoryname",
               "getextension", "getfullpath", "gettemppath", "now", "utcnow", "utf8", "ascii", "unicode", "default",
               "newline", "isnullorempty", "isnullorwhitespace", "join", "format", "tobase64string",
               "frombase64string", "tostring", "parse"}
# Anywhere in a command, these make it more than a read: file methods, find's actions, git's output options.
NOT_READ = re.compile(r"\.(moveto|copyto|delete|replace|create\w*|appendtext|appendall\w*|open\w*|write\w*|encrypt|"
                      r"decrypt|setattributes|setaccesscontrol|invoke\w*|start|save|rename)\s*\(|"
                      r"\s-(?:exec|execdir|ok|okdir|delete|fprint\w*|fls)\b|--output|\s-O\b|"
                      r"--open-files-in-pager|-membername\b", re.I)
REDIRECT = re.compile(r"(?<![<>=-])>(?!=)")


def _single_quoted_out(command: str) -> str:
    """Single-quoted text is literal in both shells: its | ; { } are not commands."""
    return re.sub(r"'[^']*'", "'_'", command)


def _segments(command: str) -> list[str]:
    return re.split(r"\$\(|&&|\|\||[;|\n(){}`]", _single_quoted_out(command))


def _segment_reads(segment: str, tool: str) -> bool:
    segment = segment.strip().lstrip("!").strip()
    if not segment or segment in ("@", ","):
        return True
    if tool == "PowerShell":
        assigned = re.match(r"^\$[\w:]+\s*=(?!=)\s*(.*)$", segment, re.S)
        if assigned:
            return _segment_reads(assigned.group(1), tool)
        if re.match(r"^\$[^\s=]*\.[^\s=]*\s*=(?!=)", segment):
            return False                               # a property set on an object: not a read
        if segment[0] in "$'\"-+0123456789@," or re.match(r"^\.\w", segment):
            return True                                # an expression, not a command
        if segment[0] == "[":
            return True                                # a type or a static call: its method names checked whole
    elif segment[0] == "$" or re.match(r"^\w+=", segment):
        return False                                   # Bash: a command held in a variable, or one with VAR=x first
    words = segment.split()
    head = words[0].strip("'\"").replace("\\", "/").split("/")[-1].lower()
    head = head[:-4] if head.endswith(".exe") else head
    rest = words[1:]
    if head in ("python", "python3", "py"):
        positional = [w for w in rest[2:] if not w.startswith("-")]
        return rest[:2] == ["-m", "json.tool"] and len(positional) <= 1
    if head == "git":
        return bool(rest) and rest[0] in GIT_READ
    if tool == "PowerShell":
        if head in PS_FOREACH:
            return not rest                            # only with a script block (checked as its own segments)
        return head in PS_READ or bool(PS_READ_VERB.match(head))
    return head in BASH_READ


def _read_only(command: str, tool: str) -> bool:
    """Whether every command in it only reads: no redirection into a file, no call operator or background job, no
    file-changing method, and each command one of the reading ones above."""
    text = DISCARDED.sub(" ", command)
    text = re.sub(r"\$\{((?:env:)?\w+)\}", r"$\1", text, flags=re.I)     # ${HOME}, ${env:X}: a variable, not a block
    if REDIRECT.search(_single_quoted_out(text)) or NOT_READ.search(text):
        return False
    if re.search(r"(?<![&>])&(?!&)", _single_quoted_out(text).replace("&&", "")):
        return False                                   # PowerShell's call operator; Bash's background job
    if tool == "PowerShell" and re.search(r"(?:^|[\s;|(])\.\s", text):
        return False                                   # dot-sourcing a script
    statics = [name.lower() for name in re.findall(r"::\s*(\w+)", text)]
    if any(name not in STATIC_READ for name in statics):
        return False
    return all(_segment_reads(segment, tool) for segment in _segments(text))


def _addon_rules(command: str, tool: str, state: Path, cwd: str) -> str:
    """D247: the registry, its seal, its key and AI Cleaner's folder - and each add-on's paths - fail closed."""
    text = _spelled(command)
    in_state = bool(cwd) and _norm(cwd) in (_norm(str(state)), _norm(os.path.realpath(str(state))))
    reads = _read_only(command, tool)
    if reads:
        return ""
    if SEAL_HELPER.search(text):
        return "it would seal AI Cleaner's add-on registry, which only an add-on's installer does"
    if DPAPI_FILE.search(text):
        return "it would change or remove a secret AI Cleaner or one of its add-ons keeps (DPAPI)"
    if _names_registry(text, state, in_state):
        return "it would change which add-ons AI Cleaner loads"
    if in_state or _top_of_state(text, state):
        return ("it would change which add-ons AI Cleaner loads (a file created, copied, moved or renamed in AI "
                "Cleaner's own folder could take the place of its add-on registry)")
    if any(_names(command, folder) for folder in _addon_paths(state)):
        return "it would change an AI Cleaner add-on's code, secrets or state"
    return ""


def _shell(command: str, state: Path | None = None, tool: str = "Bash", cwd: str = "") -> str:
    command = DISCARDED.sub(" ", command)
    if "api-key.dpapi" in command and READS_KEY.search(command):
        return "it would read AI Cleaner's API key"
    # f185: any DPAPI-protected secret - AI Cleaner's own and its add-ons' (a message authority's key, say).
    if DPAPI_FILE.search(command) and READS_KEY.search(command):
        return "it would read a secret AI Cleaner or one of its add-ons keeps (DPAPI)"
    if state is not None:
        why = _addon_rules(command, tool, state, cwd)
        if why:
            return why
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
        return _shell(given["command"], state, str(tool), str(payload.get("cwd") or ""))
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
