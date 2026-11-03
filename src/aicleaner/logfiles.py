"""Stamped log files (D219, 2026-10-29): one file per run, so a run never overwrites the last one's.

A fixed name (update.log, restart.log, remote-setup.log) was emptied by the next run, and with it the evidence of what
the last one did. Each run now writes <name>-<yyyyMMdd-HHmmss>.log, and the newest KEEP of a name are kept.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

KEEP = 30


def _same(name: str) -> re.Pattern[str]:
    return re.compile(re.escape(name) + r"-\d{8}-\d{6}(-\d+)?\.log")


def stamped(log_dir: Path, name: str, keep: int = KEEP) -> Path:
    """A new, empty <name>-<stamp>.log in log_dir; all but the newest `keep` of the name are removed."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = log_dir / f"{name}-{stamp}.log"
    count = 1
    while path.exists():                       # two runs in one second: the second gets its own file too
        count += 1
        path = log_dir / f"{name}-{stamp}-{count}.log"
    path.write_text("", encoding="utf-8")
    for old in list(reversed(_logs(log_dir, name)))[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def newest(log_dir: Path, name: str) -> Path | None:
    """The newest <name>-<stamp>.log in log_dir, or None."""
    found = _logs(log_dir, name)
    return found[-1] if found else None


def _logs(log_dir: Path, name: str) -> list[Path]:
    """The name's stamped logs, oldest first ("-2" after the plain stamp, which sorting by name gets wrong)."""
    pattern = _same(name)
    try:
        found = [p for p in log_dir.glob(f"{name}-*.log") if pattern.fullmatch(p.name)]
    except OSError:
        return []

    def order(path: Path) -> tuple[str, int]:
        rest = path.name[len(name) + 1:-len(".log")]          # 20261029-182554, or 20261029-182554-2
        return rest[:15], int(rest[16:] or 1)
    return sorted(found, key=order)
