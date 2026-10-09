"""A key per client, for signing that client's shared file (DECISIONS 51, step 1c).

The first person on an engagement generates the key on the client's page and sends it to the
others out of band; each pastes it in. It is kept DPAPI-encrypted under the user's profile, so it
is readable only by this Windows user on this PC, and never travels with the shared folder: anyone
who can write the folder could read a key kept there, which would defeat it.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets as random_source
from pathlib import Path

from .secrets import _dpapi

KEY_BYTES = 32
_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


class KeyError_(ValueError):
    """A key that cannot be used or stored, with a message meant for the person pasting it."""


def generate() -> str:
    return base64.urlsafe_b64encode(random_source.token_bytes(KEY_BYTES)).decode("ascii").rstrip("=")


def decode(key: str) -> bytes:
    """The key's bytes; KeyError_ for anything that is not one. Spaces and line breaks a paste
    picks up are ignored."""
    text = re.sub(r"\s+", "", str(key or ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise KeyError_("that is not a client key: it should be 43 letters, digits, - and _")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError as exc:
        raise KeyError_("that is not a client key") from exc
    if len(raw) != KEY_BYTES:
        raise KeyError_("that is not a whole client key - was part of it missed when copying?")
    return raw


def normalise(key: str) -> str:
    return base64.urlsafe_b64encode(decode(key)).decode("ascii").rstrip("=")


def key_id(key: str) -> str:
    """Eight hex characters that name a key without revealing it: written into each stamp, so a
    file signed with a key this PC does not hold can be told from one that was altered."""
    return hashlib.sha256(decode(key)).hexdigest()[:8]


class KeyStore:
    """The client keys this Windows user holds, one DPAPI-encrypted file per client."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def _path(self, cid: str) -> Path:
        if not _ID.match(str(cid)):
            raise KeyError_(f"{cid!r} is not a client id")
        return self.folder / f"{cid}.key"

    def get(self, cid: str) -> str | None:
        """The client's key, or None - including for a file this user cannot decrypt (another
        user's, or damaged): no key is the honest answer, and it never crashes a load."""
        try:
            raw = _dpapi(self._path(cid).read_bytes(), protect=False)
            return normalise(raw.decode("ascii"))
        except (OSError, RuntimeError, UnicodeDecodeError, KeyError_):
            return None

    def set(self, cid: str, key: str) -> str:
        path = self._path(cid)
        clean = normalise(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_dpapi(clean.encode("ascii"), protect=True))
        return clean

    def rotate(self, cid: str) -> str:
        """A new key in place of the old, which stops verifying at once (DECISIONS 51)."""
        return self.set(cid, generate())

    def forget(self, cid: str) -> None:
        try:
            self._path(cid).unlink()
        except FileNotFoundError:
            pass
