"""The Add Client wizard's drafts, and what a draft becomes (feature 111).

What is typed is saved as it is typed, to a draft on THIS machine (never the shared clients folder:
a half-made client is nobody else's business), and becomes a client only at Save. A draft can be
resumed or deleted. Its parts:

    names    the client id, the display name, and the other names that make the company obvious
    domains  the domains it uses - scheme, path and port stripped as typed
    emails   "*@<domain>": every address at the domain is the client's (an email rule, feature 96)
    urls     other identifiers: a SharePoint host (its -my and -admin twins added), a DevOps
             organisation, a GitHub organisation, a tenant - anything unique to the client
    found    what a repository scan turned up and the user ticked

Save adds names, domains and urls as entries in one write, each email domain as a rule (its
domain added as an entry first, since a rule rests on a domain the client is known to own), and
the ticked finds as scan finds.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from . import fileio
from .mapping import classify

DRAFTS_FOLDER = "client-drafts"
PARTS = ("names", "domains", "emails", "urls", "found")
DRAFT_ID = re.compile(r"^[0-9a-f]{12}$")
MAX_ENTRIES = 500                      # a draft is something typed, not a scan dump


class DraftError(ValueError):
    """A draft that cannot be read, written or turned into a client."""


def strip_scheme(value: str) -> str:
    """acme.com from https://www.acme.com/about: the value aicleaner matches every variant of."""
    text = str(value or "").strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.I)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = re.sub(r":\d+$", "", text)
    return text.strip().strip(".").lower()


def email_domain(value: str) -> str:
    """acme.com from *@acme.com, @acme.com or acme.com."""
    return strip_scheme(str(value or "").strip().lstrip("*").lstrip("@"))


def sharepoint_twins(host: str) -> list[str]:
    """acme.sharepoint.com brings acme-my.sharepoint.com and acme-admin.sharepoint.com with it."""
    found = re.fullmatch(r"([a-z0-9-]+?)(?:-my|-admin)?\.sharepoint\.com", host.lower())
    if not found:
        return [host]
    tenant = found.group(1)
    return [f"{tenant}.sharepoint.com", f"{tenant}-my.sharepoint.com", f"{tenant}-admin.sharepoint.com"]


def normalised(draft: dict[str, Any]) -> dict[str, Any]:
    """The draft as stored: known parts only, each a list of distinct, trimmed strings."""
    out: dict[str, Any] = {
        "id": str(draft.get("id") or ""),
        "mode": "global" if draft.get("mode") == "global" else "client",
        "client_id": str(draft.get("client_id") or "").strip(),
        "label": str(draft.get("label") or "").strip(),
        "page": int(draft.get("page") or 0) if str(draft.get("page") or "0").isdigit() else 0,
    }
    for part in PARTS:
        seen: list[str] = []
        for value in draft.get(part) or []:
            text = str(value or "").strip()
            if part == "domains":
                text = strip_scheme(text)
            elif part == "emails":
                text = f"*@{email_domain(text)}" if email_domain(text) else ""
            if text and text not in seen:
                seen.append(text)
        if len(seen) > MAX_ENTRIES:
            raise DraftError(f"too many {part} in one draft (at most {MAX_ENTRIES})")
        out[part] = seen
    return out


class DraftStore:
    """client-drafts/<id>.json beside the config: this machine's, never synced."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _path(self, draft_id: str) -> Path:
        if not DRAFT_ID.match(draft_id or ""):
            raise DraftError("not a draft id")
        return self.folder / f"{draft_id}.json"

    def save(self, draft: dict[str, Any]) -> dict[str, Any]:
        clean = normalised(draft)
        if not clean["id"]:
            clean["id"] = uuid.uuid4().hex[:12]
        clean["updated"] = time.time()
        path = self._path(clean["id"])
        self.folder.mkdir(parents=True, exist_ok=True)
        # Its own .tmp per save, swapped in by fileio.replace (2026-10-18): two quick saves of one draft
        # shared a single .tmp, and a scanner or the other save holding it failed the rename (WinError 32).
        temp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}.tmp")
        temp.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            fileio.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return clean

    def load(self, draft_id: str) -> dict[str, Any]:
        path = self._path(draft_id)
        try:
            return normalised(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            raise DraftError("that draft is gone") from None
        except (OSError, ValueError) as exc:
            raise DraftError(f"that draft cannot be read: {exc}") from None

    def delete(self, draft_id: str) -> None:
        path = self._path(draft_id)
        path.unlink(missing_ok=True)

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                draft = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({"id": path.stem, "mode": draft.get("mode", "client"), "client_id": draft.get("client_id", ""),
                         "label": draft.get("label", ""), "updated": draft.get("updated", 0)})
        return sorted(rows, key=lambda r: -float(r["updated"] or 0))


def entries_of(draft: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """What Save adds: (entries, email-rule domains, scan finds). The id and display name lead the
    names; an email domain not among the domains is added as a domain, since a rule needs one."""
    names = list(draft.get("names") or [])
    for lead in (draft.get("label"), draft.get("client_id")):
        if lead and draft.get("mode") != "global" and lead not in names:
            names.insert(0, lead)
    domains = list(draft.get("domains") or [])
    rules = []
    for value in draft.get("emails") or []:
        domain = email_domain(value)
        if domain:
            rules.append(domain)
            if domain not in domains:
                domains.append(domain)
    urls: list[str] = []
    for value in draft.get("urls") or []:
        text = str(value).strip()
        host = strip_scheme(text) if "://" in text or text.lower().endswith(".sharepoint.com") else text
        for item in sharepoint_twins(host) if host.endswith(".sharepoint.com") else [host]:
            if item not in urls:
                urls.append(item)
    entries: list[str] = []
    for value in names + domains + urls:
        if value and value not in entries:
            entries.append(value)
    found = [v for v in draft.get("found") or [] if v not in entries]
    return entries, sorted(set(rules)), found


def summary(draft: dict[str, Any]) -> dict[str, Any]:
    """The Finish page's counts: reals per kind, the email rules, and the scan finds."""
    entries, rules, found = entries_of(draft)
    kinds = Counter(classify(value) for value in entries + found)
    return {"entries": len(entries) + len(found), "kinds": dict(sorted(kinds.items())), "email_rules": rules,
            "found": len(found)}
