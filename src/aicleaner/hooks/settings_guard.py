"""Claude Code PreToolUse hook: the model does not change AI Cleaner's own settings (feature 70, AI Cleaner 1.1).

Brad, 2026-09-29: "setting controls blocked remotely cannot be changed by claude himself." AI Cleaner's settings are
changed by a person, on its Settings page. This stops a tool call that would change them some other way: writing the
installed config file, reading or sending the API key (H2), or taking this guard out of Claude Code's settings - and
(f185) changing which add-ons are loaded (`aicleaner-addons.json`), an add-on's code or the secrets and state it names,
or reading any DPAPI-protected secret file. D247 (1.3.1): a shell command naming the registry, an add-on's paths
or AI Cleaner's folder itself is refused unless it only reads (`_read_only`), however the name is spelled
(`_spelled`) - and the registry is sealed, so one rewritten by a route this guard misses is not loaded. D248 (1.3.2):
the same for the installed program and its venv (`_program_rules`), except running the installer's own scripts as
the deploy line does. It is aimed at a helpful model "fixing" a setting on its own initiative; a process running as
the user can always write the user's files.

Run as: python settings_guard.py <install folder> <state folder>. Standard library only: Claude Code runs it with AI
Cleaner's own Python. Anything it cannot read is let through - a broken guard must not stop all tool use.
Spec: specs/2026-11-08-f70-model-cannot-change-settings.md (tooling repository).
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
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
    if _within(path, Path(__file__).resolve().parents[2]) or _within(path, install):
        return "it would change AI Cleaner's installed program, this guard among it"
    # D248: and the Python environment it runs in (a package there is code AI Cleaner imports).
    if _within(path, state / "venv") or _within(os.path.realpath(os.path.expanduser(path)), state / "venv"):
        return "it would change AI Cleaner's installed program (the Python environment it runs in)"
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
               "frombase64string", "tostring", "parse",
               # D262: [Math]'s, which only compute (a [Math]::Min in a listing of AI Cleaner's logs was blocked)
               "min", "max", "abs", "round", "floor", "ceiling", "truncate", "pow", "sqrt", "sign"}
# Anywhere in a command, these make it more than a read: file methods, find's actions, git's output options.
NOT_READ = re.compile(r"\.(moveto|copyto|delete|replace|create\w*|appendtext|appendall\w*|open\w*|write\w*|encrypt|"
                      r"decrypt|setattributes|setaccesscontrol|invoke\w*|start|save|rename)\s*\(|"
                      r"\s-(?:exec|execdir|ok|okdir|delete|fprint\w*|fls)\b|--output|\s-O\b|"
                      r"--open-files-in-pager|-membername\b", re.I)
REDIRECT = re.compile(r"(?<![<>=-])>(?!=)")
# A .NET format item: index, width, and a format of the characters formats use (N2, yyyy-MM-dd HH:mm, #,##0.0%).
# Never $ ( ) or a backtick: "{0:$(Remove-Item x)}" runs the Remove-Item, and blanking it hid it (security review of
# 1.3.12, 2026-11-21).
FORMAT_ITEM = re.compile(r"\{\d+(?:,-?\d+)?(?::[\w.#,%+\-: /]*)?\}")


def _single_quoted_out(command: str) -> str:
    """Single-quoted text is literal in both shells: its | ; { } are not commands."""
    return re.sub(r"'[^']*'", "'_'", command)



def _segment_reads(segment: str, tool: str) -> bool:
    segment = segment.strip().lstrip("!").strip()
    if not segment or segment in ("@", ","):
        return True
    if tool == "PowerShell":
        assigned = re.match(r"^\$[\w:]+\s*=(?!=)\s*(.*)$", segment, re.S)
        if assigned:
            return _segment_reads(assigned.group(1), tool)
        # D252: `$x = if (...) { ... } else { ... }` - the keyword alone; its condition and blocks are their own
        # segments, each checked.
        keyword = segment.split(None, 1)
        if keyword[0].lower() in PS_KEYWORDS:
            return len(keyword) == 1 or _segment_reads(keyword[1], tool)
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
    if rest == ["--version"] and re.fullmatch(r"[\w.+-]+", head):
        return True                                    # D262: `claude --version` beside a read of AI Cleaner's logs
    if head in ("python", "python3", "py"):
        if rest[:2] == ["-c", PY_READ_ONLY]:
            return True                                # D252: a -c body `_python_reads_out` found read-only
        positional = [w for w in rest[2:] if not w.startswith("-")]
        return rest[:2] == ["-m", "json.tool"] and len(positional) <= 1
    if head == "git":
        return bool(rest) and rest[0] in GIT_READ
    if tool == "PowerShell":
        if head in PS_FOREACH:
            return not rest                            # only with a script block (checked as its own segments)
        return head in PS_READ or bool(PS_READ_VERB.match(head))
    if head == "curl":                                 # D252: a GET that writes no file
        return not any(CURL_WRITES.match(w) or CURL_SENDS.match(w) for w in rest)
    return head in BASH_READ


def _read_only(command: str, tool: str, segment_ok=None, mask=None, not_read=None) -> bool:
    """Whether every command in it only reads: no redirection into a file, no call operator or background job, no
    file-changing method, and each command one of the reading ones above. (D248 passes its own `segment_ok`, a
    little wider, a `mask` that also hides literal double-quoted text, and `not_read` without the -O/-o rule.)"""
    segment_ok = segment_ok or _segment_reads
    mask = mask or _single_quoted_out
    text = DISCARDED.sub(" ", command)
    text = re.sub(r"\$\{((?:env:)?\w+)\}", r"$\1", text, flags=re.I)     # ${HOME}, ${env:X}: a variable, not a block
    if REDIRECT.search(mask(text)) or (not_read or NOT_READ).search(_grep_flags_out(text)):
        return False
    if re.search(r"(?<![&>])&(?!&)", mask(text).replace("&&", "")):
        return False                                   # PowerShell's call operator; Bash's background job
    if tool == "PowerShell" and re.search(r"(?:^|[\s;|(])\.\s", text):
        return False                                   # dot-sourcing a script
    statics = [name.lower() for name in re.findall(r"::\s*(\w+)", text)]
    if any(name not in STATIC_READ for name in statics):
        return False
    if tool == "PowerShell":
        # D262: a -f format's placeholders ({0}, {1,5}, {0:N2}) are not script blocks; split at their braces, "{0}: {1}"
        # left ": " as a command no list knows.
        text = FORMAT_ITEM.sub(" ", text)
    return all(segment_ok(segment, tool) for segment in re.split(r"\$\(|&&|\|\||[;|\n(){}`]", mask(text)))


ADDONS_WHY = "it would change which add-ons AI Cleaner loads"
TOP_OF_STATE_WHY = (ADDONS_WHY + " (a file created, copied, moved or renamed in AI Cleaner's own folder could take the "
                    "place of its add-on registry)")


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
        return ADDONS_WHY
    if in_state or _top_of_state(text, state):
        return TOP_OF_STATE_WHY
    if any(_names(command, folder) for folder in _addon_paths(state)):
        return "it would change an AI Cleaner add-on's code, secrets or state"
    return ""


# ---- D248: the installed program and its Python environment, from a shell -------------------------------------------
#
# The Edit/Write rule kept tool calls out of the installed program (`<install>`, this guard among it) but a shell
# command could still copy a .py over it, pip-install into its venv (`<state>\venv`) or redirect into either: code run
# inside AI Cleaner's own process, or the guard switched off. Now a statement that touches them - names them however
# `_spelled` reads, or runs with the shell's folder inside AI Cleaner's folder - must only read, and so must every
# statement after it (a variable or a pipe could carry the path on to a writer). Running the installer's own scripts
# is execution, not writing: the deploy line and its Disable follow-up, Start, Stop, Set-AiCleanerService and
# Invoke-AiCleanerSession are let through (`_sanctioned`), as are the venv's pytest and read-only `-m aicleaner`
# commands. As with D247, this is aimed at a helpful model "fixing" the program by hand: a script file, or a name
# built at run time, is beyond any command reader, and a process running as the user can always write the user's
# files - the deploy's signed release and the add-on seal are the checks that do not depend on reading commands.

PROGRAM_ADVICE = ("AI Cleaner blocked this tool call: {what}. AI Cleaner's installed program is changed by its "
                  "deploy/update, not by Claude (D248): change the source repository and deploy a release. Reading "
                  "it is fine, and so is running the installer's scripts - Deploy-AiCleaner.ps1 -Action Update "
                  "-AssumeDefaults, Disable-, Enable- (no arguments), Start-, Stop-AiCleaner.ps1, Set-AiCleanerService.ps1 and "
                  "Invoke-AiCleanerSession.ps1 from <install>\\installer.")
PROGRAM_WHY = "it would change AI Cleaner's installed program"
INSTALLER_SCRIPTS = {"deploy-aicleaner.ps1", "disable-aicleaner.ps1", "enable-aicleaner.ps1", "start-aicleaner.ps1",
                     "stop-aicleaner.ps1",
                     "set-aicleanerservice.ps1", "invoke-aicleanersession.ps1"}
DEPLOY_ARGS = ["-action", "update", "-assumedefaults"]
AICLEANER_READS = {"check", "scan", "scan-tokens", "detail", "prune", "mcp-audit"}
AICLEANER_WRITES = {"--add", "--queue", "--apply", "--write"}
PATH_END = r"(?=$|[\s'\"|;&<>(),}])"
PROGRAM_GENERIC = re.compile(r"(?<![\w-])(?:\.aicleaner|aiclea~\d)/(?P<sub>app|venv)(?P<rest>/[^\s'\"|;&<>(),]*)?"
                             + PATH_END)
BASH_KEYWORDS = re.compile(r"^(?:(?:if|then|else|elif|do|while|until|time|!)\s+)+")
CONTROL_ONLY = {"fi", "done", "esac", "break", "continue", "then", "else", "do", "}", ")", ":"}
PS_KEYWORDS = {"if", "elseif", "else", "while", "for", "do", "try", "catch", "finally", "break", "continue"}
CURL_WRITES = re.compile(r"^-(?:-(?:output|remote-name|dump-header|cookie-jar|trace|trace-ascii|stderr|libcurl|"
                         r"etag-save|hsts|alt-svc|config|output-dir|remote-header-name|create-dirs|upload-file)\b|"
                         r"[a-zA-Z]*[oODcJKT])")


def _statements(command: str, tool: str) -> list[tuple[str, int]]:
    """The command's statements - split at ; && || newlines (and Bash's background &) outside quotes - each with the
    bracket depth it reached (a cd inside ( ) or { } may or may not last)."""
    out: list[tuple[str, int]] = []
    buf: list[str] = []
    quote, depth, deepest, i, n = "", 0, 0, 0, len(command)
    escape = "\\" if tool == "Bash" else "`"

    def flush() -> None:
        nonlocal deepest
        out.append(("".join(buf).strip(), max(deepest, depth)))
        buf.clear()
        deepest = depth

    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == escape and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "'\"":
            quote = c
        elif c == escape and i + 1 < n:
            buf.append(command[i:i + 2])
            i += 2
            continue
        elif command[i:i + 2] in ("&&", "||"):
            flush()
            i += 2
            continue
        elif c in ";\n" or (c == "&" and tool == "Bash" and command[i - 1:i] not in ("<", ">")
                             and command[i + 1:i + 2] != ">"):
            flush()
            i += 1
            continue
        elif c in "({":
            depth += 1
            deepest = max(deepest, depth)
        elif c in ")}":
            depth = max(depth - 1, 0)
        buf.append(c)
        i += 1
    flush()
    return [(text, level) for text, level in out if text]


HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_][\w-]*)\2")


def _heredocs(command: str, tool: str) -> tuple[str, list[str]]:
    """Bash here-documents taken out: the command with `<<HEREDOC<n>` in each one's place, and their bodies - text
    fed to the command, not commands (a commit message naming the program is not a write to it)."""
    if tool != "Bash" or "<<" not in command:
        return command, []
    lines, out, bodies, i = command.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        found = list(HEREDOC.finditer(line)) if "<<" in _double_quoted_out(line) else []   # not one inside quotes
        start, taken = i + 1, []
        for match in found:
            body = []
            while start < len(lines) and (lines[start].lstrip("\t") if match.group(1) else lines[start]) \
                    != match.group(3):
                body.append(lines[start])
                start += 1
            start += 1                                 # the closing word
            taken.append("\n".join(body))
        # An unquoted word lets the shell run $( ) and backticks in the body: then its lines stay commands.
        if not found or any(not m.group(2) and re.search(r"\$\(|`", body) for m, body in zip(found, taken)):
            out.append(line)
            i += 1
            continue
        numbers = iter(range(len(bodies), len(bodies) + len(found)))
        out.append(HEREDOC.sub(lambda _m: f"<<HEREDOC{next(numbers)}", line))
        bodies.extend(taken)
        i = start
    return "\n".join(out), bodies


def _words(text: str) -> list[str]:
    """Words, quote-aware, their outer quotes taken off."""
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"" else w
            for w in re.findall(r"\"[^\"]*\"|'[^']*'|[^\s'\"]+", text)]


def _pipeline(statement: str) -> list[str]:
    """A statement's commands, split at single | outside quotes."""
    parts, buf, quote = [], [], ""
    for i, c in enumerate(statement):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "|" and statement[i - 1:i] != "|" and statement[i + 1:i + 2] != "|":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    parts.append("".join(buf))
    return [p.strip() for p in parts]


def _logs_only(rest: str | None) -> bool:
    """`<install>\\logs` (and below): the program's log files, data rather than code."""
    rest = rest or ""
    return (rest == "/logs" or rest.startswith("/logs/")) and ".." not in rest


def _state_part(part: str) -> bool:
    if part == ".aicleaner" or re.fullmatch(r"aiclea~\d", part):
        return True
    return any(c in part for c in "*?[") and len(re.sub(r"[*?\[\]]", "", part)) >= 2 \
        and fnmatch.fnmatchcase(".aicleaner", part)


def _names_program(text: str, install: Path, state: Path) -> bool:
    """Whether spelled text names the installed program or its venv (or something in them): by path, by the
    `.aicleaner\\app|venv` form, 8.3 or with a wildcard or brace. `<install>\\logs` is not the program."""
    for index, base in enumerate((_slashed(str(install)).rstrip("/"), _slashed(str(state / "venv")).rstrip("/"))):
        for found in re.finditer(re.escape(base) + r"(?P<rest>/[^\s'\"|;&<>(),]*)?" + PATH_END, text):
            if not (index == 0 and _logs_only(found.group("rest"))):
                return True
    for found in PROGRAM_GENERIC.finditer(text):
        if not (found.group("sub") == "app" and _logs_only(found.group("rest"))):
            return True
    bases = [(_slashed(str(install)).rstrip("/").split("/"), True),
             (_slashed(str(state / "venv")).rstrip("/").split("/"), False)]
    for token in _tokens(text):
        parts = token.split("/")
        # A wildcard anywhere in the path (`C:/Users/*/.aicleaner/app`, `~/.aic*/ap?`), matched folder by folder.
        if any(c in token for c in "*?["):
            for base, is_install in bases:
                if len(parts) >= len(base) and all(fnmatch.fnmatchcase(b, p) for b, p in zip(base, parts)) \
                        and not (is_install and _logs_only("/" + "/".join(parts[len(base):]))):
                    return True
        for k, part in enumerate(parts[:-1]):
            if not _state_part(part):
                continue
            nxt = parts[k + 1]
            if any(c in nxt for c in "{}$%"):         # a brace list or a variable: it may well be app or venv
                return True
            if any(c in nxt for c in "*?[") and (fnmatch.fnmatchcase("app", nxt) or fnmatch.fnmatchcase("venv", nxt)):
                return True
            if nxt in ("app", "venv") and part != ".aicleaner" and not (nxt == "app" and _logs_only(
                    "/" + "/".join(parts[k + 2:]) if len(parts) > k + 2 else "")):
                return True
    return False


def _inside(path: str, folders: tuple[str, ...]) -> bool:
    return any(path == f or path.startswith(f + "/") for f in folders if f)


def _cwd_guarded(path: str | None, state: Path, install: Path) -> bool:
    """A working folder in the program or its venv: every relative name there is theirs. (AI Cleaner's folder
    itself is D247's; its other folders - sessions among them - are where relative names are resolved instead.)"""
    if not path:
        return False
    folders = tuple(_slashed(str(p)).rstrip("/") for p in (install, state / "venv"))
    logs = _slashed(str(install)).rstrip("/") + "/logs"
    return _inside(path, folders) and not _inside(path, (logs,))   # in logs, a relative name is a log (or resolved)


def _touches(spelled: str, paths: list[str], install: Path, state: Path) -> bool:
    """Names the program or its venv - directly, or as a relative name from any folder the shell has been in."""
    if _names_program(spelled, install, state):
        return True
    for token in _tokens(spelled):
        if token.startswith(("-", "$", "/")) or re.match(r"^[a-z]:", token) or "://" in token:
            continue
        if any(_names_program(posixpath.normpath(posixpath.join(base, token)), install, state) for base in paths):
            return True
    return False


def _cd_target(statement: str, tool: str) -> str | None:
    """The folder a plain change of folder goes to ('' for home, '-' for back); None when it is not one."""
    found = re.match(r"^(cd|chdir|pushd|popd|sl|set-location|push-location|pop-location)\b(.*)$", statement,
                     re.I | re.S)
    if not found:
        return None
    verb, rest = found.group(1).lower(), found.group(2).strip()
    if verb in ("popd", "pop-location"):
        return "-" if not rest else None
    if re.search(r"[|<>`]|\$\(", rest):
        return None
    words = [w for w in _words(rest) if w.lower() not in ("/d", "-path", "-literalpath")]
    if len(words) > 1:
        return None
    return words[0] if words else ""


def _next_cwd(where: tuple[str | None, bool], target: str, ever: bool, depth: int, state: Path,
              install: Path) -> tuple[str | None, bool]:
    """The shell's folder after a cd: (path or None when unknown, inside the program or its venv). Inside ( ) or
    { } going in counts and coming out may not last."""
    path, guarded = where
    if target == "-":
        return None, ever
    spelled = _spelled(target or "~").strip()
    if re.search(r"[$*?\[{]", spelled):
        new_path = None
        new_guarded = guarded or _names_program(spelled, install, state) or _names_state(spelled, state)
    else:
        if re.match(r"^[a-z]:/", spelled):
            new_path = posixpath.normpath(spelled)
        elif path:
            new_path = posixpath.normpath(posixpath.join(path, spelled))
        else:
            new_path = None
        new_guarded = _cwd_guarded(new_path, state, install) if new_path else \
            (guarded or _names_program(spelled, install, state) or _names_state(spelled, state))
    if depth:
        return new_path, guarded or new_guarded
    return new_path, new_guarded


def _double_quoted_out(text: str) -> str:
    """Literal quoted text (no $( or backtick in it) is not commands: a | or ; inside it splits nothing."""
    text = _single_quoted_out(text)
    return re.sub(r"\"[^\"`$]*(?:\$(?!\()[^\"`$]*)*\"", "\"_\"", text)


def _segment_harmless(segment: str, tool: str) -> bool:
    """D247's reading commands, and the few more a deploy line runs after the deploy: assignments of a literal (or of
    a read-only `$(...)`, checked as its own segments), loop and if keywords, sleep, seq, curl writing no file,
    `sed -n Np`, and `pwsh -Command` with a read-only command."""
    segment = segment.strip().lstrip("!").strip()
    if tool == "Bash":
        segment = BASH_KEYWORDS.sub("", segment).strip()
        if not segment or segment in CONTROL_ONLY:
            return True
        if re.fullmatch(r"\w+=(?:[^\s$`]|\$\?)*", segment):
            return True
        if re.fullmatch(r"for\s+\w+\s+in\b.*", segment, re.S):
            return _segment_harmless(re.sub(r"^for\s+\w+\s+in\b", "", segment).strip() or "true", tool) \
                if not re.search(r"\$", segment) else True
    else:
        words = segment.split()
        if words and words[0].lower() in PS_KEYWORDS:
            return _segment_harmless(" ".join(words[1:]), tool) if len(words) > 1 else True
    words = _words(segment)
    if not words:
        return True
    head = words[0].replace("\\", "/").split("/")[-1].lower()
    head = head[:-4] if head.endswith(".exe") else head
    if head in ("sleep", "start-sleep", "seq"):
        return True
    if head == "date":
        return not any(w.startswith(("-s", "--set")) for w in words[1:])
    if head == "schtasks":                             # starting or listing a task; not making or changing one
        flags = {w.lower().lstrip("/") for w in words[1:] if w.startswith("/")}
        return bool(flags & {"run", "query"}) and not flags & {"create", "change", "delete", "end"}
    if head == "tree" and any(re.match(r"^-\w*o", w) for w in words[1:]):
        return False                                   # tree -o writes its listing to a file
    if tool == "Bash" and (head in PS_READ or PS_READ_VERB.match(head)):
        return True                                    # a PowerShell reading command typed into Bash runs nothing
    if head == "curl":                                 # an output option only to nowhere (-o /dev/null)
        return not any(CURL_WRITES.match(w) and not (w in ("-o", "-so", "-sSo", "--output") and words[1:][k + 1:k + 2]
                                                     in (["/dev/null"], ["NUL"], ["nul"]))
                       for k, w in enumerate(words[1:]))
    if head == "sed":
        script = [w for w in words[1:] if w != "-n"]
        return "-n" in words and bool(script) and re.fullmatch(r"\d+(,\d+)?p", script[0]) is not None
    return _segment_reads(segment, tool)


def _nested_powershell(statement: str) -> str | None:
    """The command of `pwsh|powershell [-NoProfile] -Command "<command>"`, or None when it is not that."""
    words = _words(statement)
    if len(_pipeline(statement)) != 1 or len(words) < 3:
        return None
    head = words[0].replace("\\", "/").split("/")[-1].lower()
    if (head[:-4] if head.endswith(".exe") else head) not in ("pwsh", "powershell"):
        return None
    lowered = [w.lower() for w in words]
    at = next((i for i, w in enumerate(lowered) if w in ("-command", "-c")), None)
    if at is None or not all(w in ("-noprofile", "-nologo", "-noninteractive") for w in lowered[1:at]):
        return None
    return re.sub(r"\\([$\"`\\])", r"\1", " ".join(words[at + 1:]))     # Bash's escapes inside "..."


def _harmless(statement: str, tool: str) -> bool:
    inner = _nested_powershell(statement) if tool == "Bash" else None
    if inner is not None:
        return not REDIRECT.search(_double_quoted_out(statement)) and _harmless(inner, "PowerShell")
    return _read_only(statement, tool, segment_ok=_segment_harmless, mask=_double_quoted_out, not_read=NOT_READ_D248)


# D247's NOT_READ less its `-O` rule, which (case-blind) also stops `grep -o`: curl's and tree's output options are
# checked by name in `_segment_harmless`; wget and sort are not reading commands at all.
NOT_READ_D248 = re.compile(NOT_READ.pattern.replace(r"|--output|\s-O\b|", "|--output|"), re.I)


def _script_path(word: str, where: tuple[str | None, bool]) -> str | None:
    spelled = _spelled(word).strip()
    if re.match(r"^[a-z]:/", spelled):
        return posixpath.normpath(spelled)
    if where[0] and not re.search(r"[$*?\[{]", spelled):
        return posixpath.normpath(posixpath.join(where[0], spelled))
    return None


def _python_sanctioned(words: list[str], where: tuple[str | None, bool], install: Path, state: Path) -> bool:
    """The venv's python running pytest from outside AI Cleaner's folder, or a read-only `-m aicleaner` command."""
    exe = _script_path(words[0], where) or ""
    venv = _slashed(str(state / "venv")).rstrip("/")
    if exe not in (venv + "/scripts/python.exe", venv + "/scripts/python"):
        return False
    rest = list(words[1:])
    while rest and rest[0].lower() in ("-i", "-u", "-b", "-x", "utf8"):
        rest.pop(0)
    args = " ".join(rest[2:])
    if _names_program(_spelled(args), install, state) or _names_state(_spelled(args), state):
        return False
    if rest[:2] == ["-m", "pytest"]:
        return bool(where[0]) and not where[1]
    if rest[:2] == ["-m", "aicleaner"] and len(rest) > 2:
        return rest[2] in AICLEANER_READS and not any(a.split("=")[0] in AICLEANER_WRITES for a in rest[3:])
    return False


def _sanctioned(statement: str, tool: str, where: tuple[str | None, bool], install: Path, state: Path) -> bool:
    """One of the installer's own scripts run as the deploy line runs it (or the venv's pytest / read-only CLI):
    input from nothing, output to a file outside or through reading filters, and arguments naming nothing of AI
    Cleaner's. Deploy only as `-Action Update -AssumeDefaults` - its other actions install from elsewhere."""
    run = _bare_run(statement, tool, where, install, state)
    if run is None:
        return False
    words = _words(run)
    if tool == "PowerShell" and words[:1] == ["&"]:
        words = words[1:]
    if not words:
        return False
    head = words[0].replace("\\", "/").split("/")[-1].lower()
    head = head[:-4] if head.endswith(".exe") else head
    if head in ("pwsh", "powershell"):
        at = 1
        while at < len(words) and words[at].lower() != "-file":
            flag = words[at].lower()
            if flag in ("-noprofile", "-nologo", "-noninteractive"):
                at += 1
            elif flag == "-executionpolicy" and at + 1 < len(words) and words[at + 1].lower() in ("bypass",
                                                                                                    "remotesigned"):
                at += 2
            else:
                return False
        if at + 1 >= len(words):
            return False
        script, args = words[at + 1], words[at + 2:]
    elif head.endswith(".ps1"):
        script, args = words[0], words[1:]
    else:
        return _python_sanctioned(words, where, install, state)
    full = _script_path(script, where)
    if not full:
        return False
    folder, name = posixpath.split(full)
    if folder != _slashed(str(install)).rstrip("/") + "/installer" or name not in INSTALLER_SCRIPTS:
        return False
    lowered = [a.lower() for a in args]
    if name == "deploy-aicleaner.ps1":
        return sorted(lowered) == sorted(DEPLOY_ARGS) and lowered[lowered.index("-action") + 1] == "update"
    if name == "start-aicleaner.ps1" and any(a.startswith("-configpath") for a in lowered):
        return False                                   # started on another settings file
    if name == "enable-aicleaner.ps1" and lowered:
        return False                # Brad, 2026-11-17: Enable only as-is, the counterpart of Disable after a deploy
    spelled = _spelled(" ".join(args))
    return not (_names_program(spelled, install, state) or _names_state(spelled, state))


def _bare_run(statement: str, tool: str, where: tuple[str | None, bool], install: Path, state: Path) -> str | None:
    """The command a statement runs, its input from nothing, its output to nowhere, to a file outside AI Cleaner's
    folder or through reading filters taken off - or None when any of that is more than it."""
    text = re.sub(r"\s*\d?>&\d", " ", statement)
    text = re.sub(r"\s*<\s*(?:/dev/null|nul|\$null)(?=\s|$)", " ", text, flags=re.I)
    commands = _pipeline(text)
    run, filters = commands[0], commands[1:]
    for item in filters:
        spelled = _spelled(item)
        if not _harmless(item, tool) or _names_program(spelled, install, state) or _names_state(spelled, state):
            return None
    output = re.search(r"\s\d?>>?\s*(\"[^\"]*\"|'[^']*'|\S+)\s*$", run)
    if output:
        target = _spelled(output.group(1).strip("'\""))
        if target not in ("/dev/null", "$null", "nul"):
            if _names_program(target, install, state) or _names_state(target, state) \
                    or (where[1] and not re.match(r"^[a-z]:/", target)):
                return None
        run = run[:output.start()]
    if re.search(r"[<>]|\$\(|<\(|\$\{", run) or (tool == "Bash" and "`" in run):
        return None
    return run


def _program_rules(command: str, tool: str, install: Path, state: Path, cwd: str) -> str:
    """D248: from the first statement touching the installed program or its venv on, only reading (or the
    sanctioned runs above). Statements before it are not this rule's business."""
    start = _slashed(cwd).rstrip("/") if cwd else None
    where: tuple[str | None, bool] = (start, _cwd_guarded(start, state, install) or (bool(cwd) and _cwd_guarded(
        _slashed(os.path.realpath(cwd)).rstrip("/"), state, install)))
    ever, touched = where[1], False
    paths = [start] if start else []           # every folder the shell may be in: a ( cd ) may not have lasted
    command, bodies = _heredocs(command, tool)
    for statement, depth in _statements(command, tool):
        fed = " ".join(bodies[int(n)] for n in re.findall(r"<<HEREDOC(\d+)", statement) if int(n) < len(bodies))
        core = re.sub(r"^[({\s]+|[)}\s]+$", "", statement)
        core = BASH_KEYWORDS.sub("", core).strip() if tool == "Bash" else core
        if not core:
            continue
        target = _cd_target(core, tool)
        if target is not None:
            where = _next_cwd(where, target, ever, depth, state, install)
            ever = ever or where[1]
            if where[0] and not depth and target != "-":
                paths = [where[0]]                 # a cd at the top level: the folder is known again
            elif where[0] and where[0] not in paths:
                paths.append(where[0])
            continue
        if _sanctioned(core, tool, where, install, state):
            continue
        spelled = _spelled(core + (" " + fed if fed else ""))
        names = where[1] or _touches(spelled, paths, install, state)
        touched = touched or names
        # After the first touch, a statement using a variable could be handed the path: it must only read too.
        if (names or (touched and VARIABLE.search(spelled))) and not _harmless(core, tool):
            return _program_why(core, spelled, where, install, state)
    return ""


VARIABLE = re.compile(r"\$(?:\{|[a-z_])|%[a-z_]\w*%")     # in `_spelled` text: the known ones are written out


def _program_why(core: str, spelled: str, where: tuple[str | None, bool], install: Path, state: Path) -> str:
    venv = _slashed(str(state / "venv")).rstrip("/") + "/scripts/"
    if _words(spelled) and _words(spelled)[0].startswith(venv):
        return (PROGRAM_WHY + " (its Python environment runs only pytest and the read-only `-m aicleaner` commands "
                "for a tool call; run other scripts with the python on PATH or a repository's .venv)")
    if where[1] and not _names_program(spelled, install, state):
        return (PROGRAM_WHY + " (the shell's folder is in the installed program, where a relative name is one of "
                "its files; change folder first)")
    return PROGRAM_WHY + ", this guard among it (its code or its Python environment)"


# ---- D252: plain reads are not refused --------------------------------------------------------------------------------
#
# Brad authorised narrowing the rules above (2026-11-18) after they refused three reads: a repository Python's
# `-c` reading a JSON file in the program folder, a PowerShell `$x = if (...) {...} else {...}`, and curl on AI
# Cleaner's own API (`grep -o` read as wget's -O; curl not a reading command). Now a Python `-c` (or `python -`
# here-document) body is read as Python: one that imports only json, os, sys and pathlib, opens files only to read
# and calls only reading functions counts as a read (`_python_reads`); anything it does not recognise is not. A URL
# (scheme://...) is never a path to the registry or the program. The checks before these rules (an API key read, the
# settings file) still see the command as written.

PY_READ_ONLY = "__d252_read_only__"            # stands in for a -c body found read-only; as Python, a NameError
CURL_SENDS = re.compile(r"^-(?:-(?:data[\w-]*|form[\w-]*|json|request|upload-file|url-query|post[\w-]*)\b|"
                        r"[a-zA-Z]*[dFXT])")
PY_MODULES = {"json", "os", "os.path", "sys", "pathlib"}
PY_NAMES = {"json.load", "json.loads", "json.dumps", "json.JSONDecodeError", "os.path", "os.environ", "os.getenv",
            "os.getcwd", "os.listdir", "os.scandir", "os.walk", "os.stat", "os.lstat", "os.sep", "os.linesep",
            "os.pathsep", "os.name", "os.curdir", "os.fspath", "sys.argv", "sys.stdout", "sys.stderr", "sys.stdin",
            "sys.exit", "sys.version", "sys.version_info", "sys.platform", "sys.executable", "sys.maxsize",
            "sys.stdout.write", "sys.stderr.write", "sys.stdout.reconfigure", "sys.stdout.flush", "pathlib.Path",
            "pathlib.PurePath", "pathlib.PureWindowsPath", "pathlib.PurePosixPath", "pathlib.WindowsPath",
            "pathlib.PosixPath"}
PY_BUILTINS = {"print", "len", "open", "sorted", "list", "dict", "str", "int", "float", "bool", "tuple", "set",
               "frozenset", "enumerate", "zip", "range", "min", "max", "sum", "any", "all", "isinstance", "repr",
               "round", "abs", "map", "filter", "reversed", "format", "hasattr", "type", "iter", "next", "chr", "ord",
               "hex", "divmod", "exit", "quit", "bytes", "Exception", "ValueError", "KeyError", "OSError",
               "TypeError", "IndexError", "FileNotFoundError", "StopIteration"}
PY_METHODS = {"read", "readline", "readlines", "read_text", "read_bytes", "get", "items", "keys", "values", "exists",
              "is_file", "is_dir", "is_symlink", "iterdir", "glob", "rglob", "resolve", "absolute", "expanduser",
              "stat", "lstat", "format", "format_map", "join", "split", "rsplit", "splitlines", "strip", "lstrip",
              "rstrip", "lower", "upper", "casefold", "title", "capitalize", "startswith", "endswith", "find",
              "rfind", "index", "rindex", "count", "partition", "rpartition", "encode", "decode", "zfill", "ljust",
              "rjust", "center", "isdigit", "isalpha", "isalnum", "isspace", "isdecimal", "isnumeric", "islower",
              "isupper", "append", "extend", "insert", "pop", "sort", "reverse", "clear", "add", "discard",
              "setdefault", "update", "relative_to", "is_relative_to", "with_name", "with_suffix", "with_stem",
              "as_posix", "as_uri", "is_absolute", "match", "joinpath", "samefile", "walk", "readlink", "hex",
              "isoformat", "close", "seek", "tell", "readable", "open", "home", "cwd"}
# A method named like a writer, taken without calling it (to hand to map, say): not a read.
PY_WRITER = re.compile(r"write|unlink|remove|rename|replace|mkdir|rmdir|touch|chmod|chown|link|truncate|dump|system|"
                       r"popen|spawn|exec|kill|startfile|copy|move|delete|save|putenv|unsetenv|^open$", re.I)
PY_FUNCTION_ARGS = {"key", "default", "object_hook", "object_pairs_hook", "parse_float", "parse_int",
                    "parse_constant", "cls", "opener"}
PY_EXE = re.compile(r"^(?:python[\d.]*|py|pythonw)(?:\.exe)?$", re.I)


def _py_dotted(node, modules: dict) -> str | None:
    """`os.path.join` written out through the script's import names; None when not rooted at an imported module."""
    import ast
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in modules:
        return ".".join([modules[node.id], *reversed(parts)])
    return None


PY_OS_PATH = {"join", "exists", "lexists", "isfile", "isdir", "islink", "isabs", "expanduser", "expandvars",
              "abspath", "realpath", "normpath", "normcase", "basename", "dirname", "split", "splitext", "splitdrive",
              "getsize", "getmtime", "getctime", "getatime", "relpath", "commonpath", "commonprefix", "samefile",
              "sep"}


def _py_name_ok(dotted: str) -> bool:
    """A module name the script may use: listed, or one of os.path's (pure) functions, or os.environ's reads."""
    head, _dot, last = dotted.rpartition(".")
    return dotted in PY_NAMES or dotted in PY_MODULES or (head == "os.path" and last in PY_OS_PATH) \
        or (head == "os.environ" and last in ("get", "items", "keys", "values"))


def _py_open_ok(call, mode_at: int) -> bool:
    """open(...) / Path.open(...) only to read: a literal mode of r, b and t, and no opener."""
    import ast
    mode = call.args[mode_at] if len(call.args) > mode_at else None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
        elif keyword.arg not in ("encoding", "errors", "newline", "buffering"):
            return False
    if mode is None:
        return True
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) <= set("rbt")


def _py_imports(tree) -> dict | None:
    """The names the script's imports bind (name -> module or module.name), or None when one is not allowed."""
    import ast
    bound: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in PY_MODULES:
                    return None
                bound[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else \
                    alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in PY_MODULES:
                return None
            for alias in node.names:
                if alias.name == "*" or not _py_name_ok(f"{node.module}.{alias.name}"):
                    return None
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _python_reads(code: str) -> bool:
    """D252: whether a Python script only reads - fail closed: anything not recognised as reading is not."""
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return False
    bound = _py_imports(tree)
    if bound is None:
        return False
    modules = {name: target for name, target in bound.items() if target in PY_MODULES}
    defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    callable_names = PY_BUILTINS | defined | {name for name, target in bound.items() if _py_name_ok(target)}
    called = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal)):
            return False
        if isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id != "__name__":
                return False
            if node.id in ("open", "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals",
                           "vars", "breakpoint", "input", "help", "memoryview") and id(node) not in called:
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                return False
            dotted = _py_dotted(node, modules)
            if dotted is not None and not _py_name_ok(dotted):
                return False                           # os.remove, sys.modules, json.dump, os.path.os, ...
            if id(node) not in called and PY_WRITER.search(node.attr):
                return False
        elif isinstance(node, ast.Call) and not _py_call_ok(node, modules, callable_names):
            return False
    return True


def _py_call_ok(call, modules: dict, callable_names: set) -> bool:
    import ast
    func = call.func
    for keyword in call.keywords:                      # a function handed over to be called: a lambda or a builtin
        if keyword.arg in PY_FUNCTION_ARGS and keyword.arg != "opener" and not (
                isinstance(keyword.value, ast.Lambda)
                or (isinstance(keyword.value, ast.Name) and keyword.value.id in PY_BUILTINS - {"open"})):
            return False
    if isinstance(func, ast.Name):
        if func.id not in callable_names:
            return False
        if func.id == "open":
            return _py_open_ok(call, 1)
        if func.id in ("map", "filter", "iter") and call.args and not (
                isinstance(call.args[0], ast.Lambda)
                or (isinstance(call.args[0], ast.Name) and call.args[0].id in PY_BUILTINS - {"open"})):
            return False
        return True
    if isinstance(func, ast.Attribute):
        dotted = _py_dotted(func, modules)
        if dotted is not None:
            return _py_name_ok(dotted) and dotted not in PY_MODULES
        if func.attr not in PY_METHODS:
            return False
        return _py_open_ok(call, 0) if func.attr == "open" else True
    return False


def _python_body(quoted: str, tool: str) -> str | None:
    """A quoted -c argument as Python receives it; None when the shell would expand something in it."""
    inner = quoted[1:-1]
    if quoted[0] == "'":
        return inner.replace("''", "'") if tool == "PowerShell" else inner
    if "$" in inner or "`" in inner:
        return None
    return inner.replace('""', '"') if tool == "PowerShell" else re.sub(r"\\([\"\\$`])", r"\1", inner)


PY_C = re.compile(r"(?P<call>&\s*)?(?P<exe>\"[^\"]*\"|'[^']*'|[^\s'\"|;&<>()]+)"
                  r"(?P<flags>(?:\s+-[IuBEsS]+|\s+-X\s+\S+)*)\s+-c\s+")
PY_HEREDOC = re.compile(r"^\s*(?P<exe>\"[^\"]*\"|'[^']*'|[^\s'\"|;&<>()]+)(?:\s+-[IuBEsS]+|\s+-X\s+\S+)*\s+"
                        r"(?:-\s+)?<<(?P<dash>-?)\s*(?P<q>['\"]?)(?P<word>[A-Za-z_][\w-]*)(?P=q)\s*$")


def _foreign_python(exe: str, install: Path, state: Path) -> bool:
    """A Python outside AI Cleaner's folder (its venv's runs only what `_python_sanctioned` allows)."""
    name = exe.strip("'\"").replace("\\", "/").split("/")[-1]
    spelled = _spelled(exe)
    return bool(PY_EXE.match(name)) and not (_names_program(spelled, install, state) or _names_state(spelled, state))


def _python_reads_out(command: str, tool: str, install: Path, state: Path) -> str:
    """D252: each `python -c "<body>"` (and Bash's `python - <<EOF`) whose body only reads, as `python -c
    __d252_read_only__` - a read to the rules that follow. Any other body is left as written."""
    out, at = [], 0
    for found in PY_C.finditer(command):
        if found.start() < at:
            continue
        rest = command[found.end():]
        quoted = re.match(r"'(?:[^']|'')*'" if tool == "PowerShell" else r"'[^']*'", rest) if rest[:1] == "'" else \
            re.match(r"\"(?:[^\"]|\"\")*\"" if tool == "PowerShell" else r"\"(?:[^\"\\]|\\.)*\"", rest)
        if not quoted or not _foreign_python(found.group("exe"), install, state):
            continue
        body = _python_body(quoted.group(0), tool)
        if body is None or not _python_reads(body):
            continue
        call = (found.group("call") or "") if tool == "Bash" else ""      # Bash's & ends a statement: kept
        out.append(command[at:found.start()] + call + "python -c " + PY_READ_ONLY)
        at = found.end() + quoted.end()
    command = "".join(out) + command[at:]
    if tool != "Bash" or "<<" not in command:
        return command
    lines, kept, i = command.split("\n"), [], 0
    while i < len(lines):
        found = PY_HEREDOC.match(lines[i])
        if found and _foreign_python(found.group("exe"), install, state):
            end = i + 1
            while end < len(lines) and (lines[end].lstrip("\t") if found.group("dash") else lines[end]) \
                    != found.group("word"):
                end += 1
            body = "\n".join(lines[i + 1:end])
            if end < len(lines) and (found.group("q") or not re.search(r"[$`]", body)) and _python_reads(body):
                kept.append("python -c " + PY_READ_ONLY)
                i = end + 1
                continue
        kept.append(lines[i])
        i += 1
    return "\n".join(kept)


URL = re.compile(r"(?<![\w.+-])(?!file:)[a-z][a-z0-9+.-]+://[^\s'\"|;&<>()`]*", re.I)


def _urls_out(command: str) -> str:
    """D252: a URL (not file://) names a place on a server, never a file of AI Cleaner's."""
    return URL.sub("URL", command)


def _grep_flags_out(text: str) -> str:
    """D252: grep's and rg's short options (-o: only the matching part) are not wget's -O."""
    return re.sub(r"(?<![\w-])((?:e|f)?grep|rg)((?:\s+-[A-Za-z]+)+)",
                  lambda m: m.group(1) + re.sub(r"-[A-Za-z]+", "-_", m.group(2)), text)


def _shell(command: str, state: Path | None = None, tool: str = "Bash", cwd: str = "",
           install: Path | None = None) -> str:
    command = DISCARDED.sub(" ", command)
    if "api-key.dpapi" in command and READS_KEY.search(command):
        return "it would read AI Cleaner's API key"
    # f185: any DPAPI-protected secret - AI Cleaner's own and its add-ons' (a message authority's key, say).
    if DPAPI_FILE.search(command) and READS_KEY.search(command):
        return "it would read a secret AI Cleaner or one of its add-ons keeps (DPAPI)"
    if state is not None:
        # D252: read-only Python bodies and URLs taken out first; the checks above and below see the command whole.
        rules = _urls_out(_python_reads_out(command, tool, install or state / "app", state))
        program = _program_rules(rules, tool, install or state / "app", state, cwd)
        if program and "aicleaner-config.json" in command.lower():
            program = "it would change AI Cleaner's settings file"     # in the program's folder, but a setting
        why = _addon_rules(rules, tool, state, cwd)
        # `.aicleaner\app` is also a name directly in AI Cleaner's folder, and `aiclea~1` also the registry's short
        # name: D247 refuses those too, but the program's reason is the one that says why.
        if why in (ADDONS_WHY, TOP_OF_STATE_WHY) and program and REGISTRY_STEM not in _spelled(command):
            return program
        if why or program:
            return why or program
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
        return _shell(given["command"], state, str(tool), str(payload.get("cwd") or ""), install)
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
        advice = PROGRAM_ADVICE if why.startswith(PROGRAM_WHY) else ADVICE
        sys.stderr.write(advice.format(what=why) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
