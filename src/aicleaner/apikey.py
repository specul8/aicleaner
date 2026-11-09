"""The local API's key (H2, 1.1; spec `specs/2026-11-08-h2-local-api-secret.md` in the tooling repo).

Anything on this machine could change AI Cleaner's settings with one line - a prompt-injected model's shell included:
`curl -X POST 127.0.0.1:8787/api/config ...`. Other machines and web pages were already shut out (guard.py). Now every
changing call to `/api/` from this machine carries a per-install key: the page is given it when it loads, the hooks and
the scripts read it from this file. A program running as the same Windows user can still read the file or load the
page - the key stops a blind call, not a determined one; the desktop confirmation for turning a protection off, and
f70's hooks, are the controls for those.

The key lives in `<state>\\secrets\\api-key.dpapi`, DPAPI-protected to this Windows account (a plain 0600 file
elsewhere), made at the first start.
"""
from __future__ import annotations

import hmac
import logging
import secrets as random
from pathlib import Path

from .secrets import read_secret, write_secret

log = logging.getLogger("aicleaner.apikey")

HEADER = "X-AI-Cleaner-Key"
FILE = Path("secrets") / "api-key.dpapi"


def path(state_dir: Path) -> Path:
    return Path(state_dir) / FILE


def ensure(state_dir: Path) -> str:
    """This install's key, made the first time."""
    where = path(state_dir)
    try:
        found = read_secret(where)
    except OSError as exc:
        log.warning("the API key could not be read (%s): making a new one", exc)
        found = None
    if found:
        return found
    key = random.token_urlsafe(32)
    write_secret(where, key)
    log.info("made this install's API key (%s)", where)
    return key


def matches(given: str, key: str) -> bool:
    return bool(key) and bool(given) and hmac.compare_digest(given.encode("utf-8"), key.encode("utf-8"))
