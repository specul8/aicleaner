"""Local secret storage for upstream API keys.

On Windows a secret is stored DPAPI-encrypted (CryptProtectData, current user
scope), so the file is only readable by this user on this machine; syncing it
elsewhere yields an undecryptable blob. On other platforms it is a 0600 plain
file. An environment variable can stand in for the file.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

DPAPI_SUFFIX = ".dpapi"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def write_secret(path: Path, secret: str) -> Path:
    """Store a secret; returns the path actually written (adds .dpapi on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secret.strip().encode("utf-8")
    if sys.platform == "win32":
        target = path if path.suffix == DPAPI_SUFFIX else path.with_suffix(path.suffix + DPAPI_SUFFIX)
        target.write_bytes(_dpapi(data, protect=True))
        return target
    target = path.with_suffix("") if path.suffix == DPAPI_SUFFIX else path
    target.write_bytes(data)
    os.chmod(target, 0o600)
    return target


def read_secret(path: str | Path | None, env_var: str | None = None) -> str | None:
    """Return the secret from the file (DPAPI or plain) or the env var, else None."""
    if path:
        p = Path(os.path.expandvars(str(path))).expanduser()
        if p.exists():
            raw = p.read_bytes()
            if p.suffix == DPAPI_SUFFIX:
                raw = _dpapi(raw, protect=False)
            return raw.decode("utf-8").strip() or None
        # Allow the plain twin of a .dpapi path (non-Windows write_secret target).
        if p.suffix == DPAPI_SUFFIX and p.with_suffix("").exists():
            return p.with_suffix("").read_text(encoding="utf-8").strip() or None
    if env_var:
        value = os.environ.get(env_var, "").strip()
        if value:
            return value
    return None
