"""The add-on registry's seal (D247, AI Cleaner 1.3.1): AI Cleaner loads add-ons only from a registry their installer
wrote.

`<state>\\aicleaner-addons.json` says which code AI Cleaner runs in its own process (addons.py). The settings guard
stops Claude writing it, but a guard that reads shell commands can be talked past (a verb it does not know, a script
file it never sees). So the registry is sealed by whoever is allowed to write it - an add-on's installer - and AI
Cleaner checks the seal before it loads anything:

- **Key**: 32 random bytes, DPAPI-protected (this Windows user, this machine) at `<state>\\secrets\\addons-seal.dpapi`.
  Made the first time something seals. The settings guard refuses a shell command that reads a `*.dpapi` file and any
  Edit/Write under `secrets\\`.
- **Seal**: `<state>\\aicleaner-addons.json.seal`, one line `hmac-sha256 <hex>`: HMAC-SHA256 of the registry's exact
  bytes under the key.
- **Writing one** (an add-on's installer, after it writes the registry): run this file with AI Cleaner's own Python -
  `"<state>\\venv\\Scripts\\python.exe" -I "<state>\\app\\aicleaner\\addonseal.py" seal --state "<state>"` - exit 0 and
  `sealed <registry>` on success; exit 1 and the reason on stderr otherwise. `check` says whether the seal holds.
- **Checking** (AI Cleaner at start): `status(state)` - "sealed", "unsealed" (no seal file), "no-key", "broken" (a
  seal or key that cannot be read) or "changed" (the registry is not the one sealed). Anything but "sealed" loads
  nothing (addons.py; a registry written before 1.3.1 is sealed once there, see `AddonHost.load`).

What this does and does not stop. Claude runs as the same Windows user as AI Cleaner, so a process that sets out to
can unprotect the key and write a matching seal; nothing at user level can prevent that. The seal is aimed at a
helpful model: a registry it rewrites by any route the guard misses ("fixing" an entry, a rename it did not think of,
a script file) loads nothing and the Add-ons card says why, instead of loading code it wrote. Forging a seal needs a
deliberate DPAPI unprotect of a secret, which the guard refuses in a shell command and which no helpful model does by
accident. A stronger control (a key only an administrator can write) would need an elevated installer every time.

Standard library only and importing nothing from AI Cleaner, so an installer can run this one file.
Spec: specs/2026-11-17-f185-messages-addon.md, "The registry's seal" (tooling repository).
"""
from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path:  # run as a file: never import AI Cleaner's own modules (secrets.py) as stdlib
    import os as _os
    if _os.path.normcase(_os.path.abspath(sys.path[0] or ".")) == _os.path.normcase(_os.path.dirname(_os.path.abspath(__file__))):
        del sys.path[0]

import argparse  # noqa: E402
import ctypes  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

REGISTRY = "aicleaner-addons.json"
SEAL = REGISTRY + ".seal"
KEY = Path("secrets") / "addons-seal.dpapi"
PREFIX = "hmac-sha256 "

SEALED, UNSEALED, NO_KEY, BROKEN, CHANGED = "sealed", "unsealed", "no-key", "broken", "changed"


class SealError(RuntimeError):
    """The seal could not be written or read, with the reason the person is shown."""


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData, current user (the same as aicleaner.secrets, kept here so this file runs
    on its own)."""
    if sys.platform != "win32":
        raise SealError("the registry's seal needs Windows (DPAPI)")
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise SealError(f"DPAPI call failed ({ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def registry_path(state: Path) -> Path:
    return Path(state) / REGISTRY


def seal_path(state: Path) -> Path:
    return Path(state) / SEAL


def key_path(state: Path) -> Path:
    return Path(state) / KEY


def has_key(state: Path) -> bool:
    return key_path(state).exists()


def _read_key(state: Path) -> bytes:
    try:
        key = _dpapi(key_path(state).read_bytes(), protect=False)
    except OSError as exc:
        raise SealError(f"the seal key {key_path(state)} could not be read ({exc})") from exc
    if len(key) < 32:
        raise SealError(f"the seal key {key_path(state)} is not a key")
    return key


def _key(state: Path) -> bytes:
    """The key, made the first time (exclusive create: two installers at once end up with one key)."""
    path = key_path(state)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "xb") as handle:
                handle.write(_dpapi(os.urandom(32), protect=True))
        except FileExistsError:
            pass
        except OSError as exc:
            raise SealError(f"the seal key {path} could not be written ({exc})") from exc
    return _read_key(state)


def digest(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def seal(state: Path, data: bytes | None = None) -> Path:
    """Seal the registry as it is now, or the bytes given (those just read and checked); the key made if there is
    none. SealError when there is no registry."""
    state = Path(state)
    if data is None:
        try:
            data = registry_path(state).read_bytes()
        except OSError as exc:
            raise SealError(f"{registry_path(state)} could not be read ({exc}): nothing was sealed") from exc
    line = PREFIX + digest(_key(state), data) + "\n"
    target = seal_path(state)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(line, encoding="ascii")
        os.replace(tmp, target)
    except OSError as exc:
        raise SealError(f"{target} could not be written ({exc})") from exc
    return target


def status(state: Path, data: bytes | None = None) -> tuple[str, str]:
    """(state, reason): whether the registry - as it is now, or the bytes given (those AI Cleaner parsed) - is the one
    an installer sealed. The reason is for the person."""
    state = Path(state)
    registry, sealed = registry_path(state), seal_path(state)
    if not sealed.exists():
        return UNSEALED, f"{registry.name} has no seal ({sealed.name}): it was not written by an add-on's installer"
    if not has_key(state):
        return NO_KEY, f"{sealed.name} is there but the seal key ({key_path(state)}) is not"
    try:
        text = sealed.read_text(encoding="ascii").strip()
        key = _read_key(state)
        if data is None:
            data = registry.read_bytes()
    except (OSError, ValueError, SealError) as exc:
        return BROKEN, f"the registry's seal could not be checked ({exc})"
    if not text.startswith(PREFIX) or not hmac.compare_digest(text[len(PREFIX):].strip(), digest(key, data)):
        return CHANGED, (f"{registry.name} was changed after an add-on's installer wrote and sealed it "
                         "(not by an installer)")
    return SEALED, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="addonseal.py", description="Seal or check AI Cleaner's add-on registry.")
    parser.add_argument("action", choices=("seal", "check"))
    parser.add_argument("--state", default=str(Path.home() / ".aicleaner"),
                        help="AI Cleaner's folder (default %%USERPROFILE%%\\.aicleaner)")
    given = parser.parse_args(argv)
    state = Path(given.state)
    if given.action == "seal":
        try:
            print(f"sealed {registry_path(state)} ({seal(state).name})")
        except SealError as exc:
            print(f"not sealed: {exc}", file=sys.stderr)
            return 1
        return 0
    found, why = status(state)
    print(found if found == SEALED else f"{found}: {why}")
    return 0 if found == SEALED else 1


if __name__ == "__main__":
    sys.exit(main())
