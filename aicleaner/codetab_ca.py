"""Feature 117: the certificate authority that lets aicleaner read the Code tab's calls to Anthropic.

The Claude Desktop app's Code tab ignores ANTHROPIC_BASE_URL but honours HTTPS_PROXY, so its calls to
api.anthropic.com reach aicleaner's forward proxy encrypted. To filter them, aicleaner answers for
api.anthropic.com itself, with a certificate from this authority. Three things keep that narrow:

- Only Claude Code trusts it, through NODE_EXTRA_CA_CERTS in its settings - never the Windows store,
  so no browser or other program on the machine does.
- The authority is name-constrained (critical) to api.anthropic.com: a certificate it issued for any
  other name fails verification, even in a program that does trust it.
- Its private key is DPAPI-protected (current user) on disk; the server certificate's key is made
  afresh at every start and never written down.

The authority lives for ten years; the server certificate for 90 days, renewed well before that.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import socket
import ssl
import tempfile
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOST = "api.anthropic.com"
CA_CERT = "codetab-ca.pem"            # public: the file NODE_EXTRA_CA_CERTS names
CA_KEY = "codetab-ca.key.dpapi"       # private: DPAPI-protected
CA_YEARS = 10
LEAF_DAYS = 90
RENEW_DAYS = 30                       # a server certificate this close to its end is replaced

Protect = Callable[[bytes, bool], bytes]

# One authority per thing read, each constrained to its own names (2026-10-18): the Code tab's can only
# ever vouch for api.anthropic.com, Copilot's only for Copilot's hosts. A leaked key is limited to them.
KINDS = {
    "codetab": {"cert": CA_CERT, "key": CA_KEY, "label": "Code tab",
                "permitted": [HOST], "names": [HOST]},
    "copilot": {"cert": "copilot-ca.pem", "key": "copilot-ca.key.dpapi", "label": "Copilot",
                "permitted": ["githubcopilot.com", "copilot-telemetry.githubusercontent.com"],
                "names": ["*.githubcopilot.com", "*.individual.githubcopilot.com", "*.business.githubcopilot.com",
                          "*.enterprise.githubcopilot.com", "copilot-telemetry.githubusercontent.com"]},
}


def _dpapi(data: bytes, protect: bool) -> bytes:
    from .secrets import _dpapi as call
    return call(data, protect)


class AuthorityError(RuntimeError):
    """The authority could not be made or read, with a reason worth showing."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Authority:
    """The authority in `folder` (aicleaner's state folder). `protect(data, encrypt)` wraps the key;
    DPAPI unless a test gives another."""

    def __init__(self, folder: Path, protect: Protect = _dpapi, kind: str = "codetab") -> None:
        self.folder = Path(folder)
        self._protect = protect
        self.kind = KINDS[kind]
        self._ca: tuple[x509.Certificate, ec.EllipticCurvePrivateKey] | None = None
        self.leaf: x509.Certificate | None = None

    @property
    def ca_path(self) -> Path:
        return self.folder / self.kind["cert"]

    @property
    def key_path(self) -> Path:
        return self.folder / self.kind["key"]

    # ---- the authority ----------------------------------------------------------------------

    def ensure(self) -> x509.Certificate:
        """The authority, made when missing or near its end; loaded otherwise."""
        if self._ca is not None and self._ca[0].not_valid_after_utc - _now() > dt.timedelta(days=RENEW_DAYS):
            return self._ca[0]
        if self.ca_path.exists() and self.key_path.exists():
            try:
                cert = x509.load_pem_x509_certificate(self.ca_path.read_bytes())
                key = serialization.load_der_private_key(self._protect(self.key_path.read_bytes(), False), None)
            except (OSError, ValueError) as exc:
                raise AuthorityError(f"the {self.kind['label']} certificate authority in {self.folder} could not be read ({exc}); "
                                     "turn the switch off and on to make a new one") from exc
            if isinstance(key, ec.EllipticCurvePrivateKey) and cert.not_valid_after_utc - _now() > dt.timedelta(days=RENEW_DAYS):
                self._ca = (cert, key)
                return cert
        return self._make()

    def _make(self) -> x509.Certificate:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"aicleaner {self.kind['label']} ({socket.gethostname()})"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "aicleaner (this machine only)")])
        start = _now() - dt.timedelta(hours=1)
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(start + dt.timedelta(days=365 * CA_YEARS))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                             crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                # The narrowing that matters: whatever trusts this authority trusts it for its names only.
                .add_extension(x509.NameConstraints(permitted_subtrees=[x509.DNSName(n) for n in self.kind["permitted"]],
                                                    excluded_subtrees=None),
                               critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self._write(self.key_path, self._protect(der, True))
            self._write(self.ca_path, cert.public_bytes(serialization.Encoding.PEM))
        except OSError as exc:
            raise AuthorityError(f"the {self.kind['label']} certificate authority could not be saved in {self.folder} ({exc})") from exc
        self._ca = (cert, key)
        return cert

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        temp = path.with_name(path.name + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, path)

    def remove(self) -> None:
        """The switch turned off: the authority goes, so a later switch-on makes a new one."""
        for path in (self.ca_path, self.key_path):
            path.unlink(missing_ok=True)
        self._ca = None
        self.leaf = None

    # ---- the server certificate -------------------------------------------------------------

    def issue(self) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
        """A fresh server certificate for api.anthropic.com, signed by the authority."""
        ca_cert = self.ensure()
        assert self._ca is not None
        ca_key = self._ca[1]
        key = ec.generate_private_key(ec.SECP256R1())
        start = _now() - dt.timedelta(hours=1)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.kind["names"][0])]))
                .issuer_name(ca_cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(start + dt.timedelta(days=LEAF_DAYS))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in self.kind["names"]]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        self.leaf = cert
        return cert, key

    def needs_renewal(self) -> bool:
        return self.leaf is None or self.leaf.not_valid_after_utc - _now() < dt.timedelta(days=RENEW_DAYS)

    def load_into(self, context: ssl.SSLContext) -> None:
        """A fresh server certificate loaded into `context` (new handshakes use it). The key reaches
        the file ssl needs only encrypted with a one-off password held in memory, and the file is
        deleted straight after."""
        cert, key = self.issue()
        one_off = secrets.token_bytes(32)
        chain = cert.public_bytes(serialization.Encoding.PEM)
        wrapped = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.BestAvailableEncryption(one_off))
        handle, name = tempfile.mkstemp(prefix="aicleaner-codetab-", suffix=".pem")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(chain + wrapped)
            context.load_cert_chain(name, None, lambda: one_off)
        finally:
            Path(name).unlink(missing_ok=True)

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])   # aiohttp speaks HTTP/1.1 only
        self.load_into(context)
        return context

    def summary(self) -> dict[str, str]:
        ca = self._ca[0] if self._ca else None
        return {"ca": str(self.ca_path) if self.ca_path.exists() else "",
                "ca_expires": ca.not_valid_after_utc.date().isoformat() if ca else "",
                "server_expires": self.leaf.not_valid_after_utc.date().isoformat() if self.leaf else ""}
