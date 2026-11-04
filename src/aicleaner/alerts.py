"""Persistent alerts (feature 161): raised by a script or a session on this machine, shown on every page and device
until someone dismisses them.

The finished / waiting notices of feature 44 are worked out by the page from counters and go when the session is
looked at. An alert is different: it says something a job found - "the daily close needs you", with a summary - and
it stays until it is dismissed, on whichever device, because a glance at the session is not dealing with it.

Raised from this machine only (`/api/alerts/raise` is refused through remote access, guard.py): the page that can be
reached from a phone may read and dismiss, never put words in front of Brad. Kept in `alerts.json` in the state
folder, so a restart keeps them. Plain text throughout: the page draws the summary as text, never as HTML.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("aicleaner.alerts")

FILE = "alerts.json"
MAX_OPEN = 50
MAX_TITLE = 120
MAX_SUMMARY = 4000
MAX_SESSION = 200
LEVELS = ("amber", "red", "green")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")


class AlertError(ValueError):
    """Not an alert: says which field and why."""


def _text(value: Any, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AlertError(f"{field} must be text")
    # Line ends as the page draws them: a summary written on Windows arrives with CR LF.
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = value.strip() if field != "summary" else value.rstrip()
    if required and not value:
        raise AlertError(f"{field} is required")
    if len(value) > limit:
        raise AlertError(f"{field} is longer than {limit} characters")
    return value


class AlertStore:
    """The open alerts for one state folder."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / FILE
        self._lock = threading.Lock()
        self._alerts: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("alerts file %s could not be read (%s): starting with none", self.path, exc)
            return []
        alerts = data.get("alerts") if isinstance(data, dict) else None
        return [a for a in alerts if isinstance(a, dict) and a.get("id")] if isinstance(alerts, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"alerts": self._alerts}, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(self.path)

    def raise_alert(self, title: Any, summary: Any = "", session: Any = "", level: Any = "amber",
                    key: Any = "") -> dict[str, Any]:
        """A new open alert; one with the same key as an open alert replaces it."""
        title = _text(title, "title", MAX_TITLE, required=True)
        summary = _text(summary, "summary", MAX_SUMMARY)
        session = _text(session, "session", MAX_SESSION)
        level = level or "amber"
        if level not in LEVELS:
            raise AlertError(f"level is one of {', '.join(LEVELS)}")
        key = _text(key, "key", 120)
        if key and not KEY_PATTERN.match(key):
            raise AlertError("key is letters, digits and . _ : - only")
        alert = {"id": uuid.uuid4().hex[:12], "title": title, "summary": summary, "session": session,
                 "level": level, "key": key, "at": time.time()}
        with self._lock:
            if key:
                self._alerts = [a for a in self._alerts if a.get("key") != key]
            self._alerts.append(alert)
            while len(self._alerts) > MAX_OPEN:
                greens = [a for a in self._alerts if a.get("level") == "green"]
                self._alerts.remove(greens[0] if greens else self._alerts[0])
            self._save()
        log.info("alert raised: %s (%s, session %s, key %s)", title, level, session or "-", key or "-")
        return dict(alert)

    def dismiss(self, alert_id: str = "", key: str = "") -> bool:
        """True when an open alert went; by its id, or by the key it was raised with."""
        if not alert_id and not key:
            return False
        with self._lock:
            kept = [a for a in self._alerts
                    if not ((alert_id and a.get("id") == alert_id) or (key and a.get("key") == key))]
            if len(kept) == len(self._alerts):
                return False
            self._alerts = kept
            self._save()
        log.info("alert dismissed: %s", alert_id or key)
        return True

    def open_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._alerts]
