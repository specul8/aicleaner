"""The encrypted tunnel: what crosses Cloudflare is ciphertext (feature 21, DECISIONS 57 and 60).

Cloudflare terminates TLS at its edge, so the one hop aicleaner added for remote use is the one hop
that can read what crosses it - and so can anything else able to open HTTPS on the way, such as an
enterprise inspection box. With this on, the page and the proxy share keys none of them sees.

**The code.** Generated here, ~119 bits, shown once on the machine and typed on the device; kept
DPAPI-protected beside a random salt (v0.9.147: chosen passphrases are gone - DECISIONS 60). From it
both ends derive `K` with PBKDF2-HMAC-SHA256. `K` never encrypts anything: it only proves each side.

**A key per connection.** The page and the proxy each make a throwaway ECDH P-256 key pair; the
shared secret, through HKDF, is that connection's session key. Each side proves it holds `K` with an
HMAC over the exchange, so someone in the middle without the code cannot complete it. Traffic is
AES-256-GCM under the session key, each request numbered so it cannot be replayed. Recorded traffic
stays sealed even if the code leaks later: the session keys died with their connections.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ITERATIONS = 600_000
SECRET_FILE = "tunnel-code.dpapi"
SALT_FILE = "tunnel-code-salt"
# What v0.9.143-v0.9.145 stored. It may have been a chosen passphrase, and cannot be told from a
# code, so it is never used: Settings asks for a code to be generated (DECISIONS 60).
LEGACY_FILES = ("tunnel-secret.dpapi", "tunnel-salt")
# No 0/O, 1/I/L: a code is read off one screen and typed on another.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 24
INFO = b"aicleaner tunnel 2"
SESSION_IDLE_S = 30 * 60
MAX_SESSIONS = 64
REPLAY_WINDOW = 1024


class SealError(ValueError):
    """An envelope that will not open: wrong key, altered, replayed, or not an envelope at all."""


class UnknownSession(SealError):
    """A session this proxy does not hold - it restarted, or the session went idle. The page runs
    the exchange again."""


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: Any) -> bytes:
    return base64.b64decode(str(text or ""), validate=True)


def normalise_code(code: str) -> str:
    """A code as typed - any case, with or without its hyphens and spaces - as the code itself."""
    return "".join(c for c in str(code).upper() if c in CODE_ALPHABET)


def derive(secret: str, salt: bytes, iterations: int = ITERATIONS, length: int = 32) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations, dklen=length)


def generate_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def seal(key: bytes, data: bytes, aad: bytes | None = None) -> dict[str, Any]:
    iv = os.urandom(12)
    return {"iv": b64(iv), "ct": b64(AESGCM(key).encrypt(iv, data, aad))}


def open_sealed(key: bytes, envelope: Any, aad: bytes | None = None) -> bytes:
    if not isinstance(envelope, dict):
        raise SealError("not a sealed envelope")
    try:
        return AESGCM(key).decrypt(unb64(envelope.get("iv")), unb64(envelope.get("ct")), aad)
    except (ValueError, InvalidTag) as exc:
        raise SealError("the envelope did not open with this key") from exc


class TunnelSecret:
    """The code, its salt, and `K` derived from them, for one state folder."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self._key: bytes | None = None

    def is_set(self) -> bool:
        return (self.state_dir / SECRET_FILE).is_file() and (self.state_dir / SALT_FILE).is_file()

    def legacy(self) -> bool:
        """A secret from before codes only, which is not used (DECISIONS 60)."""
        return not self.is_set() and (self.state_dir / LEGACY_FILES[0]).is_file()

    def salt(self) -> bytes:
        return (self.state_dir / SALT_FILE).read_bytes()

    def generate(self) -> str:
        """A new code, stored and returned - shown once, never again. Every device must enter it."""
        from .secrets import _dpapi

        code = generate_code()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        salt = os.urandom(16)
        (self.state_dir / SECRET_FILE).write_bytes(_dpapi(normalise_code(code).encode("ascii"), protect=True))
        (self.state_dir / SALT_FILE).write_bytes(salt)
        self._key = derive(normalise_code(code), salt)
        return code

    def key(self) -> bytes | None:
        """`K`, derived once per process; None when no code is set or it cannot be read."""
        if self._key is None and self.is_set():
            from .secrets import _dpapi

            try:
                code = _dpapi((self.state_dir / SECRET_FILE).read_bytes(), protect=False).decode("ascii")
            except (OSError, RuntimeError, UnicodeDecodeError):
                return None
            self._key = derive(code, self.salt())
        return self._key

    def params(self) -> dict[str, Any]:
        """What a device needs to derive `K` - nothing that opens or checks anything."""
        return {"salt": b64(self.salt()) if self.is_set() else "", "iterations": ITERATIONS}


# ---- the exchange, and the sessions it makes -------------------------------------------------

def _public_raw(private: ec.EllipticCurvePrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint)


def transcript(client_pub: bytes, client_nonce: bytes, server_pub: bytes, server_nonce: bytes) -> bytes:
    return client_pub + client_nonce + server_pub + server_nonce


def session_key(shared: bytes, client_pub: bytes, client_nonce: bytes, server_pub: bytes, server_nonce: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=client_nonce + server_nonce,
                info=INFO + client_pub + server_pub).derive(shared)


def make_proof(key: bytes, who: bytes, exchange: bytes) -> bytes:
    return hmac.new(key, who + exchange, hashlib.sha256).digest()


def request_aad(sid: str, seq: int) -> bytes:
    return f"{sid}|{seq}".encode("ascii")


def reply_aad(sid: str, seq: int) -> bytes:
    return f"{sid}|{seq}|reply".encode("ascii")


@dataclass
class Session:
    key: bytes
    exchange: bytes
    confirmed: bool = False
    highest: int = -1
    seen: set[int] = field(default_factory=set)
    used_at: float = field(default_factory=time.monotonic)


class Sessions:
    """The connections the proxy holds, in memory only: a restart forgets them all, and the page
    simply runs the exchange again."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def hello(self, long_term: bytes, client_pub: str, client_nonce: str) -> dict[str, Any]:
        """Both arguments as they arrive on the wire, in base64."""
        try:
            client_pub, client_nonce = unb64(client_pub), unb64(client_nonce)
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_pub)
        except ValueError as exc:
            raise SealError("not a key exchange") from exc
        if len(client_nonce) != 16:
            raise SealError("not a key exchange")
        mine = ec.generate_private_key(ec.SECP256R1())
        server_pub, server_nonce = _public_raw(mine), os.urandom(16)
        shared = mine.exchange(ec.ECDH(), peer)
        exchange = transcript(client_pub, client_nonce, server_pub, server_nonce)
        sid = secrets.token_urlsafe(16)
        with self._lock:
            self._expire()
            self._sessions[sid] = Session(session_key(shared, client_pub, client_nonce, server_pub, server_nonce), exchange)
        return {"sid": sid, "server_pub": b64(server_pub), "server_nonce": b64(server_nonce),
                "proof": b64(make_proof(long_term, b"server", exchange))}

    def confirm(self, long_term: bytes, sid: str, proof: str) -> None:
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                raise UnknownSession("unknown session")
            try:
                given = unb64(proof)
            except ValueError as exc:
                raise SealError("not a proof") from exc
            if not hmac.compare_digest(given, make_proof(long_term, b"client", session.exchange)):
                # Not the code: forget the session rather than leave it for another try.
                del self._sessions[sid]
                raise SealError("the device did not prove it holds the code")
            session.confirmed = True

    def open(self, envelope: Any) -> tuple[str, int, bytes]:
        """(sid, seq, plaintext) for a request, refusing an unknown or unconfirmed session and a
        replayed or stale number."""
        if not isinstance(envelope, dict) or envelope.get("v") != 2:
            raise SealError("not a sealed envelope")
        sid, seq = str(envelope.get("sid") or ""), envelope.get("seq")
        if not isinstance(seq, int) or seq < 0:
            raise SealError("not a sealed envelope")
        with self._lock:
            session = self._sessions.get(sid)
            if session is None or time.monotonic() - session.used_at > SESSION_IDLE_S:
                self._sessions.pop(sid, None)
                raise UnknownSession("unknown session")
            if not session.confirmed:
                raise SealError("the session was never confirmed")
            if seq in session.seen or seq <= session.highest - REPLAY_WINDOW:
                raise SealError("a request already seen")
            plain = open_sealed(session.key, envelope, request_aad(sid, seq))
            session.seen.add(seq)
            if seq > session.highest:
                session.highest = seq
                session.seen = {s for s in session.seen if s > seq - REPLAY_WINDOW}
            session.used_at = time.monotonic()
            return sid, seq, plain

    def seal_reply(self, sid: str, seq: int, data: bytes) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                raise UnknownSession("unknown session")
            return {"v": 2, "sid": sid, "seq": seq, **seal(session.key, data, reply_aad(sid, seq))}

    def forget_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _expire(self) -> None:
        now = time.monotonic()
        for sid in [s for s, v in self._sessions.items() if now - v.used_at > SESSION_IDLE_S]:
            del self._sessions[sid]
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[min(self._sessions, key=lambda s: self._sessions[s].used_at)]
