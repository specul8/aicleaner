"""Contact details from contact-*.json, for the UI (via /api/status) and the Desktop policy text.

The first contact-*.json in the app root (ordinal order of the lower-cased name) wins. The
maintainer keeps one file per publishing audience side by side, so the order only matters on
that machine; a deployment has exactly one. Files are re-read when they change, so edits show
without a restart.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

from .config import APP_ROOT

log = logging.getLogger(__name__)

FALLBACK = "the aicleaner maintainer"
PATTERN = "contact-*.json"
URL_FIELDS = ("repo_url", "issues_url", "profile_url")
EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")


@dataclass(frozen=True)
class Contact:
    source: str = ""  # file the values came from; "" when none was found
    name: str = ""
    email: str = ""
    organisation: str = ""
    repo_url: str = ""
    issues_url: str = ""
    profile_url: str = ""

    @classmethod
    def from_dict(cls, data: dict, source: str) -> "Contact":
        """Keep non-empty strings only; drop non-http(s) URLs and malformed emails so nothing
        unsafe can reach an href."""
        values: dict[str, str] = {}
        for f in fields(cls):
            if f.name == "source":
                continue
            raw = data.get(f.name)
            value = raw.strip() if isinstance(raw, str) else ""
            if value and f.name in URL_FIELDS and urlsplit(value).scheme.lower() not in ("http", "https"):
                value = ""
            if value and f.name == "email" and not EMAIL_RE.match(value):
                value = ""
            values[f.name] = value
        return cls(source=source, **values)

    def as_dict(self) -> dict[str, str]:
        """Every field (validated), for the UI to render with textContent."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self) if f.name != "source")

    def short(self) -> str:
        """One plain-text pointer for places without links (the Desktop policy text)."""
        return self.issues_url or self.email or self.name or FALLBACK


class ContactSource:
    """Chooses the first contact-*.json in a folder and re-reads it when it changes."""

    def __init__(self, folder: Path = APP_ROOT) -> None:
        self.folder = Path(folder)
        self._key: tuple[str, int, int] | None = None
        self._contact = Contact()

    def _choose(self) -> Path | None:
        # The owner's own contact file in the app folder wins; the repository's (examples\
        # contact-git.json since feature 102) is the fallback.
        for folder in (self.folder, self.folder / "examples"):
            found = sorted(folder.glob(PATTERN), key=lambda p: p.name.lower())
            if found:
                return found[0]
        return None

    def get(self) -> Contact:
        path = self._choose()
        if path is None:
            self._key, self._contact = None, Contact()
            return self._contact
        try:
            st = path.stat()
        except OSError:
            return Contact()
        key = (path.name, st.st_mtime_ns, st.st_size)
        if key != self._key:
            self._key, self._contact = key, self._load(path)
        return self._contact

    @staticmethod
    def _load(path: Path) -> Contact:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as exc:
            log.warning("contact file %s ignored: %s", path.name, exc)
            return Contact(source=path.name)
        return Contact.from_dict(data, path.name)
