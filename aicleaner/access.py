"""Cloudflare Access tokens, verified here rather than trusted.

The Session page is reached from a phone through a Cloudflare tunnel, with Access in front doing
the sign-in (Entra ID for a work tenant, a Microsoft account for a personal one). Access puts a
signed token on every request it lets through, in Cf-Access-Jwt-Assertion. Anyone who can reach the
tunnel's hostname can send whatever headers they like, so the token is verified here: signature,
issuer, audience, expiry, and the address it was issued to.

RS256 is verified with the integer arithmetic Python already has (pow with three arguments), so
this needs no cryptography library: an RSA signature check is a public-key operation, and comparing
the result against the expected PKCS#1 v1.5 encoding is what verification is.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.request
from typing import Any

log = logging.getLogger("aicleaner.access")

CERTS_PATH = "/cdn-cgi/access/certs"
JWKS_TTL_S = 3600        # Cloudflare rotates keys; an hour is what their own libraries use
FORCED_REFRESH_S = 60    # an unknown key id forces a fetch at most this often (H15)
CLOCK_SKEW_S = 60
# The DigestInfo that PKCS#1 v1.5 puts in front of a SHA-256 hash (RFC 8017, 9.2).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


class AccessError(ValueError):
    """The token is missing, malformed, expired, or not for us."""


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _int(data: str) -> int:
    return int.from_bytes(_b64(data), "big")


class AccessVerifier:
    """Checks Cloudflare Access tokens against a team's published keys."""

    def __init__(self, team_domain: str, audience: str, allowed_emails: list[str] | None = None,
                 fetch: Any = None) -> None:
        self.team_domain = (team_domain or "").strip().rstrip("/")
        if self.team_domain and not self.team_domain.startswith("http"):
            self.team_domain = f"https://{self.team_domain}"
        self.audience = (audience or "").strip()
        self.allowed = {e.strip().lower() for e in (allowed_emails or []) if e.strip()}
        self._fetch = fetch or self._fetch_jwks
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0

    @property
    def issuer(self) -> str:
        return self.team_domain

    def _fetch_jwks(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.team_domain + CERTS_PATH, timeout=10) as response:  # noqa: S310
            return json.loads(response.read())

    def keys(self, force: bool = False) -> dict[str, dict[str, Any]]:
        # A forced fetch - a token naming a key id not on the list - happens at most once a minute
        # (H15). Anyone can send a token with a made-up key id, and each one used to fetch.
        if force and self._keys and (time.time() - self._fetched_at) < FORCED_REFRESH_S:
            force = False
        if force or not self._keys or (time.time() - self._fetched_at) > JWKS_TTL_S:
            document = self._fetch() or {}
            self._keys = {str(k.get("kid")): k for k in document.get("keys", []) if k.get("kid")}
            self._fetched_at = time.time()
        return self._keys

    # ---- the check ---------------------------------------------------------------------

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, or AccessError. Nothing is trusted until this returns."""
        if not self.team_domain or not self.audience:
            raise AccessError("remote access is not configured (team_domain and audience)")
        parts = (token or "").split(".")
        if len(parts) != 3:
            raise AccessError("no Access token on the request")
        header_part, payload_part, signature_part = parts
        try:
            header = json.loads(_b64(header_part))
            claims = json.loads(_b64(payload_part))
            signature = _b64(signature_part)
        except (ValueError, json.JSONDecodeError) as exc:
            raise AccessError(f"the Access token is malformed ({exc})") from exc
        if header.get("alg") != "RS256":
            raise AccessError(f"unexpected signing algorithm {header.get('alg')!r}")
        key = self.keys().get(str(header.get("kid"))) or self.keys(force=True).get(str(header.get("kid")))
        if key is None:
            raise AccessError("the token was signed with a key this team does not publish")
        signed = f"{header_part}.{payload_part}".encode("ascii")
        if not self._signature_ok(signed, signature, key):
            raise AccessError("the Access token's signature does not check out")
        self._claims_ok(claims)
        return claims

    @staticmethod
    def _signature_ok(signed: bytes, signature: bytes, key: dict[str, Any]) -> bool:
        """RSASSA-PKCS1-v1_5 with SHA-256, done with integers."""
        try:
            modulus, exponent = _int(key["n"]), _int(key["e"])
        except (KeyError, ValueError):
            return False
        size = (modulus.bit_length() + 7) // 8
        if len(signature) != size:
            return False
        recovered = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(size, "big")
        digest = hashlib.sha256(signed).digest()
        expected = b"\x00\x01" + b"\xff" * (size - len(SHA256_PREFIX) - len(digest) - 3) + b"\x00" + SHA256_PREFIX + digest
        # Constant time is not the point here (the comparison is against a public value), but it
        # costs nothing to use the safe one.
        return hmac.compare_digest(recovered, expected)

    def _claims_ok(self, claims: dict[str, Any]) -> None:
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if self.audience not in [str(a) for a in audiences if a]:
            raise AccessError("the token is for a different Access application")
        if str(claims.get("iss", "")).rstrip("/") != self.issuer.rstrip("/"):
            raise AccessError(f"the token is from {claims.get('iss')!r}, not this team")
        now = time.time()
        if float(claims.get("exp", 0)) < now - CLOCK_SKEW_S:
            raise AccessError("the Access token has expired; sign in again")
        if float(claims.get("nbf", 0)) > now + CLOCK_SKEW_S:
            raise AccessError("the Access token is not valid yet")
        email = str(claims.get("email", "")).lower()
        # An empty list used to mean "whoever Access lets in" - for a permissive Access policy,
        # anyone (H32). It now means no one: the list is the one place this deployment names its user.
        if not self.allowed:
            raise AccessError("no one is allowed through the tunnel yet: add your address to remote.allowed_emails")
        if email not in self.allowed:
            raise AccessError(f"{email or 'that account'} is not on the list for this deployment")


def email_of(claims: dict[str, Any]) -> str:
    return str(claims.get("email") or claims.get("common_name") or "").lower()
