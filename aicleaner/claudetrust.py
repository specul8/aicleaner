"""Are a profile's folders trusted by Claude Code? (2026-10-16, item C; spec 2026-10-16-c-trust-check-design.md)

Three lists, all of them (decided 2026-10-16):
- auto-mode trusted repos - `settings.json` autoMode.environment, the "**Trusted repo**:" entries;
  an entry saying "and everything under it" covers the folders below its path;
- additional directories - `settings.json` permissions.additionalDirectories (covers children);
- workspace trust - `.claude.json` projects["<path with forward slashes>"].hasTrustDialogAccepted,
  per folder (case variants sit side by side there; any true one counts).

What must be covered: the folders added by hand and the OneDrive share (with their children) in the
first two, and the home folder in the third; the workspace file's folder (only that folder) in all
three. Writing backs each file up first, writes a temporary file, parses it back, then swaps it in:
a malformed settings.json or .claude.json stops Claude Code from starting.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

AUTO, DIRS, TRUST = "auto_mode", "additional_directories", "workspace_trust"
LIST_NAMES = {AUTO: "auto-mode trusted repos", DIRS: "additional directories", TRUST: "workspace trust"}
TRUSTED_ENTRY = "**Trusted repo**:"
PATH_IN_TEXT = re.compile(r"[A-Za-z]:[\\/][^\s`'\",;)]*")


class TrustError(RuntimeError):
    pass


def config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".claude"


def settings_file() -> Path:
    return config_dir() / "settings.json"


def global_file() -> Path:
    """Claude Code's own state file: beside the config folder, or inside CLAUDE_CONFIG_DIR when set."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured) / ".claude.json" if configured else Path.home() / ".claude.json"


def norm(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").lower()


def _covers(root: str, folder: str, children: bool) -> bool:
    r, f = norm(root), norm(folder)
    return f == r or (children and f.startswith(r + "/"))


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except ValueError as exc:
        raise TrustError(f"{path} is not valid JSON ({exc}); fix it by hand - it is not touched") from exc
    if not isinstance(data, dict):
        raise TrustError(f"{path} does not hold a JSON object; it is not touched")
    return data


def auto_mode_roots(settings: dict) -> list[tuple[str, bool]]:
    entries = ((settings.get("autoMode") or {}).get("environment")) or []
    out = []
    for entry in entries:
        text = str(entry)
        if TRUSTED_ENTRY not in text:
            continue
        children = "everything under" in text.lower()
        out += [(p.rstrip("\\/"), children) for p in PATH_IN_TEXT.findall(text)]
    return out


def extra_dirs(settings: dict) -> list[str]:
    return [str(d) for d in ((settings.get("permissions") or {}).get("additionalDirectories") or [])]


def trusted_folders(state: dict) -> list[str]:
    return [k for k, v in (state.get("projects") or {}).items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted")]


def check(folders: list[str], home: str, workspace_dirs: list[str]) -> list[dict[str, Any]]:
    """[{folder, lists: [...missing...]}] for what the profile needs and does not have."""
    settings, state = _read(settings_file()), _read(global_file())
    auto, dirs, trusted = auto_mode_roots(settings), extra_dirs(settings), trusted_folders(state)
    need: dict[str, set[str]] = {}

    def want(folder: str, lists: set[str]) -> None:
        if folder:
            key = next((k for k in need if norm(k) == norm(folder)), folder)
            need.setdefault(key, set()).update(lists)

    for folder in folders:
        want(folder, {AUTO, DIRS})
    want(home, {TRUST})
    for folder in workspace_dirs:
        want(folder, {AUTO, DIRS, TRUST})
    missing = []
    for folder, lists in need.items():
        gaps = []
        if AUTO in lists and not any(_covers(r, folder, c) for r, c in auto):
            gaps.append(AUTO)
        if DIRS in lists and not any(_covers(d, folder, True) for d in dirs):
            gaps.append(DIRS)
        if TRUST in lists and not any(norm(t) == norm(folder) for t in trusted):
            gaps.append(TRUST)
        if gaps:
            missing.append({"folder": folder, "lists": gaps})
    return missing


def _swap_in(path: Path, data: dict) -> str:
    """Backup, temporary file, parse back, replace. Answers the backup's path ('' when new)."""
    backup = ""
    if path.exists():
        backup = f"{path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    json.loads(temp.read_text(encoding="utf-8"))
    os.replace(temp, path)
    return backup


def add(missing: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Write every missing entry; answers what was written and the backups."""
    settings_path, state_path = settings_file(), global_file()
    settings, state = _read(settings_path), _read(state_path)
    wrote: list[str] = []
    backups: list[str] = []
    changed_settings = changed_state = False
    for item in missing:
        folder, lists = item["folder"], item["lists"]
        if AUTO in lists:
            env = settings.setdefault("autoMode", {}).setdefault("environment", [])
            env.append(f"{TRUSTED_ENTRY} {folder} and everything under it - {profile} (added by aicleaner)")
            changed_settings = True
            wrote.append(f"{folder}: {LIST_NAMES[AUTO]}")
        if DIRS in lists:
            dirs = settings.setdefault("permissions", {}).setdefault("additionalDirectories", [])
            if not any(norm(d) == norm(folder) for d in dirs):
                dirs.append(folder)
                changed_settings = True
            wrote.append(f"{folder}: {LIST_NAMES[DIRS]}")
        if TRUST in lists:
            projects = state.setdefault("projects", {})
            key = folder.replace("\\", "/")
            entry = projects.get(key) if isinstance(projects.get(key), dict) else {}
            entry["hasTrustDialogAccepted"] = True
            projects[key] = entry
            changed_state = True
            wrote.append(f"{folder}: {LIST_NAMES[TRUST]}")
    if changed_settings:
        backups.append(_swap_in(settings_path, settings))
    if changed_state:
        backups.append(_swap_in(state_path, state))
    return {"wrote": wrote, "backups": [b for b in backups if b]}
