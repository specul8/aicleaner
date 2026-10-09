"""PowerShell commands for the file editor's completion (feature 80), compiled from this machine.

The editor's suggestions should reflect the environment Claude runs in (the user, 2026-10-07), so
the command list comes from `Get-Command` here, not from a list shipped with aicleaner. Modules
are looked for at user AND machine scope, for PowerShell 7 and Windows PowerShell 5.1 alike: the
user's Documents (which may be redirected into OneDrive), Program Files, and the system folder.

When it is compiled (spec 2026-10-07-f80, section 8):
- at proxy start, in the background - a restart picks up a module Claude has just installed;
- while the editor is in use: a request for the list more than CHECK_EVERY_S after the last check
  compares a fingerprint of the module folders (names and modified times - a listing, never a
  load), and recompiles in the background when it changed. Nothing is checked while nobody edits.

Parameters are fetched one command at a time, when the page first needs them, and kept for the run:
Az and Graph hold thousands of commands, and every parameter set up front would take minutes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("aicleaner.pscommands")

CHECK_EVERY_S = 300
COMPILE_TIMEOUT_S = 300
PARAMETERS_TIMEOUT_S = 60
# A command name as PowerShell spells one: Verb-Noun, or a plain word (functions, aliases). Checked
# before a name goes anywhere near a PowerShell command line.
COMMAND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)?")

# Every module folder, user and machine scope, both editions - worked out by PowerShell itself so a
# Documents folder redirected into OneDrive is found where it really is.
_MODULE_PATHS = r"""
$docs = [Environment]::GetFolderPath('MyDocuments')
$paths = @(
    (Join-Path $docs 'PowerShell\Modules'),
    (Join-Path $docs 'WindowsPowerShell\Modules'),
    (Join-Path $env:ProgramFiles 'PowerShell\Modules'),
    (Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules'),
    (Join-Path $PSHOME 'Modules'),
    (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules')
) + ($env:PSModulePath -split [IO.Path]::PathSeparator)
$paths = $paths | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
"""

_COMPILE = _MODULE_PATHS + r"""
$env:PSModulePath = $paths -join [IO.Path]::PathSeparator
$found = Get-Command -CommandType Cmdlet, Function, Alias -ErrorAction SilentlyContinue |
    Select-Object @{ n = 'name'; e = { $_.Name } }, @{ n = 'kind'; e = { $_.CommandType.ToString() } },
                  @{ n = 'module'; e = { $_.Source } }
[pscustomobject]@{ paths = @($paths); commands = @($found) } | ConvertTo-Json -Depth 3 -Compress
"""

_PATHS_ONLY = _MODULE_PATHS + "\n@($paths) | ConvertTo-Json -Compress\n"


def powershell() -> str:
    """PowerShell 7 when it is installed, else Windows PowerShell; "" on a machine with neither."""
    return shutil.which("pwsh") or shutil.which("powershell") or ""


def _run(script: str, timeout: float) -> str:
    exe = powershell()
    if not exe:
        raise OSError("PowerShell is not installed")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=flags)
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout).strip()[:500] or f"PowerShell exited {done.returncode}")
    return done.stdout


def fingerprint(paths: list[str]) -> str:
    """Which modules are installed where, cheaply: each module folder's name and modified time."""
    digest = hashlib.sha256()
    for root in sorted(paths, key=str.lower):
        try:
            with os.scandir(root) as found:
                for entry in sorted(found, key=lambda e: e.name.lower()):
                    if entry.is_dir(follow_symlinks=False):
                        digest.update(f"{root}|{entry.name}|{entry.stat().st_mtime_ns}\n".encode())
        except OSError:
            digest.update(f"{root}|unreadable\n".encode())
    return digest.hexdigest()


class PowerShellCommands:
    """The compiled list, its cache on disk, and the change check. Thread-safe; never blocks a caller
    on PowerShell - a compile runs in the background and the caller gets what is there now."""

    def __init__(self, state_dir: Path | None, run: Callable[[str, float], str] = _run,
                 clock: Callable[[], float] = time.monotonic, enabled: bool = True):
        # `editor.compile_powershell` (default on): off, nothing is run and the page uses its own
        # built-in list - the browser tests run that way, so each test server does not start pwsh.
        self.enabled = enabled
        self.path = (Path(state_dir) / "powershell-commands.json") if state_dir else None
        self._run = run
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"commands": [], "paths": [], "fingerprint": "", "compiled_at": 0}
        self._compiling = False
        self._checked_at: float | None = None
        self._parameters: dict[str, list[str]] = {}
        self.error = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path else None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("commands"), list):
            self._data = data

    # ---- compiling ----------------------------------------------------------------------------

    def compile(self) -> None:
        """Ask PowerShell for every command at user and machine scope, and keep the answer."""
        try:
            raw = json.loads(self._run(_COMPILE, COMPILE_TIMEOUT_S) or "{}")
            # Once each, whatever the case: PowerShell's -Unique is case-sensitive, and Windows lists
            # the same folder as C:\Program Files and c:\program files.
            paths = list({str(p).rstrip("\\/").lower(): str(p) for p in (raw.get("paths") or [])
                          if isinstance(p, str)}.values())
            rows = raw.get("commands") or []
            commands = sorted({(str(r.get("name") or ""), str(r.get("kind") or ""), str(r.get("module") or ""))
                               for r in (rows if isinstance(rows, list) else [rows]) if isinstance(r, dict)
                               and COMMAND_NAME.fullmatch(str(r.get("name") or ""))}, key=lambda t: t[0].lower())
            data = {"commands": [{"name": n, "kind": k, "module": m} for n, k, m in commands], "paths": paths,
                    "fingerprint": fingerprint(paths), "compiled_at": time.time()}
            with self._lock:
                self._data, self.error = data, ""
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(self.path)
            log.info("PowerShell commands compiled: %d from %d module folder(s)", len(commands), len(paths))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            with self._lock:
                self.error = str(exc)
            log.warning("PowerShell commands could not be compiled: %s", exc)
        finally:
            with self._lock:
                self._compiling = False

    def _compile_in_background(self) -> bool:
        with self._lock:
            if self._compiling:
                return False
            self._compiling = True
        threading.Thread(target=self.compile, name="pscommands", daemon=True).start()
        return True

    def start(self) -> None:
        """At proxy start: compile in the background, unless the cached list still matches."""
        if not self.enabled:
            return
        self._checked_at = self._clock()
        paths = self._data.get("paths") or []
        if paths and self._data.get("fingerprint") == fingerprint(paths):
            return
        self._compile_in_background()

    def _maybe_check(self) -> None:
        now = self._clock()
        if self._checked_at is not None and now - self._checked_at < CHECK_EVERY_S:
            return
        self._checked_at = now
        paths = self._data.get("paths") or []
        if not paths:
            try:
                paths = json.loads(self._run(_PATHS_ONLY, PARAMETERS_TIMEOUT_S) or "[]")
                paths = [paths] if isinstance(paths, str) else list(paths)
            except (OSError, ValueError, subprocess.SubprocessError):
                paths = []
        if not self._data.get("commands") or fingerprint(paths) != self._data.get("fingerprint"):
            self._compile_in_background()

    # ---- what the page asks for ---------------------------------------------------------------

    def commands(self) -> dict[str, Any]:
        """The list as it is now; checks for module changes at most every CHECK_EVERY_S."""
        if not self.enabled:
            return {"source": "off", "commands": [], "compiled_at": 0, "compiling": False, "error": ""}
        self._maybe_check()
        with self._lock:
            source = "machine" if self._data.get("commands") else ("compiling" if self._compiling else "unavailable")
            return {"source": source, "commands": list(self._data.get("commands") or []),
                    "compiled_at": self._data.get("compiled_at") or 0, "compiling": self._compiling,
                    "error": self.error}

    def compact(self) -> dict[str, Any]:
        """The list for the page, small: about 40,000 commands on a machine with Az and Graph, so
        each is [name, kind letter, module index] against one list of module names."""
        answer = self.commands()
        modules: dict[str, int] = {}
        rows = []
        for c in answer.pop("commands"):
            index = modules.setdefault(c["module"], len(modules))
            rows.append([c["name"], (c["kind"] or "?")[0], index])
        return {**answer, "modules": list(modules), "commands": rows}

    def version(self) -> str:
        """An ETag for the list: changes whenever a compile lands."""
        return f'"{self._data.get("fingerprint", "")[:16]}-{int(self._data.get("compiled_at") or 0)}"'

    def parameters(self, name: str) -> list[str]:
        """A command's own parameters (the common ones left out), fetched once per run."""
        if not COMMAND_NAME.fullmatch(name or ""):
            raise ValueError(f"{name!r} is not a PowerShell command name")
        if not self.enabled:
            return []
        key = name.lower()
        with self._lock:
            if key in self._parameters:
                return list(self._parameters[key])
        script = _MODULE_PATHS + (
            "\n$env:PSModulePath = $paths -join [IO.Path]::PathSeparator\n"
            f"$command = Get-Command -Name '{name}' -ErrorAction Stop\n"
            "$common = [System.Management.Automation.Cmdlet]::CommonParameters + "
            "[System.Management.Automation.Cmdlet]::OptionalCommonParameters\n"
            "@($command.Parameters.Keys | Where-Object { $_ -notin $common } | Sort-Object) | ConvertTo-Json -Compress\n")
        try:
            raw = json.loads(self._run(script, PARAMETERS_TIMEOUT_S) or "[]")
        except (OSError, ValueError, subprocess.SubprocessError):
            raw = []
        found = [raw] if isinstance(raw, str) else [str(p) for p in (raw or []) if isinstance(p, str)]
        with self._lock:
            self._parameters[key] = found
        return list(found)
