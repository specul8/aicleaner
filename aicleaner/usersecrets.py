"""Pre-entered secrets: literal strings the user wants redacted regardless of shape.

Stored DPAPI-encrypted (Windows, current user) or 0600 plain elsewhere, at the
path secrets.user_file names. Only labels, lengths and dates ever leave this
module; values are used for matching and nothing else. Not synced, not shared.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .secrets import read_secret, write_secret


class UserSecrets:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: list[dict[str, Any]] = []
        self._pattern: re.Pattern[str] | None = None
        self._labels: dict[str, str] = {}
        self._mtime: float | None = None
        self.reload()

    # ---- persistence ------------------------------------------------------------

    def _current_mtime(self) -> float | None:
        for p in (self.path, self.path.with_suffix("")):
            if p.exists():
                return p.stat().st_mtime
        return None

    def reload(self) -> None:
        raw = read_secret(self.path)
        self._entries = json.loads(raw) if raw else []
        self._mtime = self._current_mtime()
        self._compile()

    def _save(self) -> None:
        write_secret(self.path, json.dumps(self._entries))
        self._mtime = self._current_mtime()
        self._compile()

    def _compile(self) -> None:
        values = sorted({e["value"] for e in self._entries if e.get("value")}, key=len, reverse=True)
        self._labels = {e["value"]: e["label"] for e in self._entries if e.get("value")}
        self._pattern = re.compile("|".join(re.escape(v) for v in values)) if values else None

    def maybe_reload(self) -> None:
        if self._current_mtime() != self._mtime:
            self.reload()

    # ---- editing (labels only come back out) ------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        return [{"label": e["label"], "length": len(e["value"]), "added": e.get("added", "")} for e in self._entries]

    def add(self, label: str, value: str) -> None:
        label = label.strip(); value = value.strip()
        if not label or not value:
            raise ValueError("label and value are both required")
        if len(value) < 8:
            raise ValueError("value is too short to be a credential (8 characters minimum)")
        if any(e["label"] == label for e in self._entries):
            raise ValueError(f"label {label!r} already exists; remove it first to replace the value")
        self._entries.append({"label": label, "value": value,
                              "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        self._save()

    def remove(self, label: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["label"] != label]
        if len(self._entries) != before:
            self._save()
            return True
        return False

    # ---- redaction ------------------------------------------------------------------

    def redact(self, text: str, counter: Counter[str] | None = None) -> str:
        if self._pattern is None:
            return text

        def repl(m: re.Match[str]) -> str:
            label = self._labels.get(m.group(0), "secret")
            if counter is not None:
                counter[f"user:{label}"] += 1
            return f"[REDACTED-user:{label}]"

        return self._pattern.sub(repl, text)
