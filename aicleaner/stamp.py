"""Integrity stamp on client files written by aicleaner.

A checksum of the canonical content plus who wrote it and when. A file whose stamp no longer
matches was edited outside aicleaner (a text editor, a sync tool); the UI flags it, so hand edits
of shared client files are visible. A file with no stamp predates stamping (legacy) and is not
flagged; the next save through aicleaner stamps it.

With the client's key (DECISIONS 51) the stamp is also signed: an HMAC over the same canonical body
that only someone holding the key can make. The plain checksum can be recomputed by anything that
edits the file; the signature cannot, so it is what tells a colleague's aicleaner from a hand edit,
another tool, or an account used from the OneDrive web editor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
from datetime import datetime, timezone
from typing import Any

KEY = "_stamp"


def machine() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname() or "unknown"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(doc: dict[str, Any]) -> str:
    """Key order and whitespace independent, so re-serialising a file never changes its digest."""
    body = {k: v for k, v in doc.items() if k != KEY}
    return json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False)


def digest(doc: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(doc).encode("utf-8")).hexdigest()


def signature(doc: dict[str, Any], key: str) -> str:
    from .keys import decode
    return hmac.new(decode(key), canonical(doc).encode("utf-8"), hashlib.sha256).hexdigest()


def apply(doc: dict[str, Any], by: str | None = None, at: str | None = None,
          key: str | None = None) -> dict[str, Any]:
    st: dict[str, Any] = {"sha256": digest(doc), "by": by or machine(), "at": at or now()}
    if key:
        from .keys import key_id
        st["hmac"] = signature(doc, key)
        st["key_id"] = key_id(key)
    doc[KEY] = st
    return doc


def verify(doc: dict[str, Any], key: str | None = None) -> str:
    """Without a key: "ok", "missing" (legacy, never stamped) or "mismatch" (edited outside
    aicleaner). With this client's key: "signed", "unsigned" (no signature), "other-key" (signed
    with a key this PC does not hold - a rotation, or another engagement's key) or "altered"
    (signed with ours, changed since)."""
    st = doc.get(KEY)
    if key is None:
        if not isinstance(st, dict) or "sha256" not in st:
            return "missing"
        return "ok" if st["sha256"] == digest(doc) else "mismatch"
    if not isinstance(st, dict) or not isinstance(st.get("hmac"), str):
        return "unsigned"
    from .keys import key_id
    if st.get("key_id") != key_id(key):
        return "other-key"
    return "signed" if hmac.compare_digest(st["hmac"], signature(doc, key)) else "altered"
