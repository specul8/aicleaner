"""Route B (spec 2026-10-21-desktop-chat-route-b.md, section 3): claude.ai's chat rewritten without its schema.

Claude Desktop's chat, signed in to claude.ai, is Connect-RPC with binary protobuf (probed 2026-10-21): the send
(`ConversationService/PerformAction`, `application/proto`) carries the typed text as a plain string field, and the
reply (`StreamTimeline`, `application/connect+proto`) carries the reply text the same way, inside Connect envelope
frames. There is no published schema, so this reads the wire format alone:

- every field is (number, wire type, value); a length-delimited value is a string, bytes or a nested message, and
  only a schema says which. A **field map** (`{"3": "text", "1.2": "message"}`, paths of field numbers from the top)
  says so where it is known; elsewhere a value is **text** when it is valid UTF-8 with no control characters but
  tab, newline and carriage return, a **message** when it parses cleanly to its end, and **bytes** otherwise; a
  value that is both is a message when it is mostly nested payload (see `_mostly_nested`);
- `rewrite` passes every text field through a function and re-encodes the length of every enclosing message, so a
  name that grows or shrinks the text leaves a valid message. A part with nothing changed is copied byte for byte.

The guess can be wrong (a short string can parse as a message; a message can look like text). That is why the
outbound route runs the canary over the final raw bytes (UTF-8 and UTF-16) after rewriting: a real name left in a
field read wrongly refuses the request instead of leaving.
"""
from __future__ import annotations

from typing import Callable

VARINT, I64, LEN, SGROUP, EGROUP, I32 = 0, 1, 2, 3, 4, 5
MAX_DEPTH = 48
MAX_FIELD = (1 << 29) - 1

# Connect envelope flags (connectrpc.com/docs/protocol): bit 0 compressed, bit 1 the end-of-stream trailer (JSON).
COMPRESSED, END_STREAM = 0x01, 0x02

TextFn = Callable[[str, str], str]          # (field path, text) -> text


class ProtoError(ValueError):
    """Not a protobuf message this reader can take apart."""


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("varint runs past the end")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint longer than 10 bytes")


def write_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _fields(buf: bytes) -> list[tuple[int, int, int, int, int]]:
    """(number, wire, start of field, start of value, end) for every field of `buf`; ProtoError when it is not a
    message: a group, a wire type that does not exist, field number 0 or past the limit, or a value running off
    the end."""
    out = []
    pos = 0
    while pos < len(buf):
        start = pos
        key, pos = read_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if not 0 < number <= MAX_FIELD:
            raise ProtoError(f"field number {number}")
        if wire == VARINT:
            value_at = pos
            _, pos = read_varint(buf, pos)
        elif wire == I64:
            value_at, pos = pos, pos + 8
        elif wire == I32:
            value_at, pos = pos, pos + 4
        elif wire == LEN:
            length, value_at = read_varint(buf, pos)
            pos = value_at + length
        else:
            raise ProtoError(f"wire type {wire}")   # groups are long deprecated; not seen from claude.ai
        if pos > len(buf):
            raise ProtoError("field runs past the end")
        out.append((number, wire, start, value_at, pos))
    return out


def is_text(value: bytes) -> bool:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ord(c) < 32 and c not in "\t\n\r" or c == "\x7f" for c in text)


def is_message(value: bytes) -> bool:
    if not value:
        return False
    try:
        return bool(_fields(value))
    except ProtoError:
        return False


def kind_of(value: bytes, path: str, field_map: dict[str, str] | None) -> str:
    """'text', 'message' or 'bytes' for a length-delimited value at `path`."""
    known = (field_map or {}).get(path)
    if known == "message" and is_message(value):
        return "message"
    if known == "bytes":
        return known
    if known == "text":
        try:
            value.decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            return "bytes"
    text, message = is_text(value), is_message(value)
    if text and message:
        return "message" if _mostly_nested(value) else "text"
    if text:
        return "text"
    return "message" if message else "bytes"


def _mostly_nested(value: bytes) -> bool:
    """For a value that is both valid text and a clean message: a nested message whose tag and length bytes happen
    to be printable ("Jx..." is field 9 holding 120 bytes) is mostly length-delimited payload; text that parses by
    chance ("hi" is field 13 = 105, "en-AU" a fixed 32-bit field) is varints and fixed fields."""
    payload = sum(end - value_at for _n, wire, _s, value_at, end in _fields(value) if wire == LEN)
    return payload * 2 >= len(value)


def _rewrite(buf: bytes, prefix: str, fn: TextFn | None, field_map: dict[str, str] | None,
             depth: int, seen: list[tuple[str, str]] | None) -> tuple[bytes, bool]:
    if depth > MAX_DEPTH:
        raise ProtoError("nested too deep")
    out = bytearray()
    changed = False
    for number, wire, start, value_at, end in _fields(buf):
        if wire != LEN:
            out += buf[start:end]
            continue
        path = f"{prefix}.{number}" if prefix else str(number)
        value = buf[value_at:end]
        kind = kind_of(value, path, field_map)
        new = value
        if kind == "text":
            text = value.decode("utf-8")
            if seen is not None:
                seen.append((path, text))
            if fn is not None:
                new = fn(path, text).encode("utf-8")
        elif kind == "message":
            new, inner = _rewrite(value, path, fn, field_map, depth + 1, seen)
            if not inner:
                new = value
        if new == value:
            out += buf[start:end]
            continue
        changed = True
        # The field's own tag as it was written, then the new length and value.
        out += buf[start:read_varint(buf, start)[1]] + write_varint(len(new)) + new
    return (bytes(out) if changed else buf), changed


def rewrite(body: bytes, fn: TextFn, field_map: dict[str, str] | None = None) -> bytes:
    """`body` with every text field passed through `fn(path, text)`; every enclosing length re-encoded."""
    return _rewrite(body, "", fn, field_map, 0, None)[0]


def texts(body: bytes, field_map: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Every text field of `body`, in order: (path, text)."""
    seen: list[tuple[str, str]] = []
    _rewrite(body, "", None, field_map, 0, seen)
    return seen


def structure(body: bytes, field_map: dict[str, str] | None = None, prefix: str = "", depth: int = 0) -> list[str]:
    """The shape of `body` with no content - `path:kind(length)` per field - for the structure log, which grows
    the field map from synthetic sessions without ever writing what was said."""
    if depth > MAX_DEPTH:
        return []
    out = []
    for number, wire, _start, value_at, end in _fields(body):
        path = f"{prefix}.{number}" if prefix else str(number)
        if wire == VARINT:
            # Small numbers are kinds and roles, never what was said: shown, so the field map can name them (a
            # reply's sender, 2026-10-23). Large ones - times, sizes, ids - stay a bare type.
            value = read_varint(body, value_at)[0]
            out.append(f"{path}:varint={value}" if value < 1000 else f"{path}:varint")
            continue
        if wire != LEN:
            out.append(f"{path}:{('varint', 'i64', '', '', '', 'i32')[wire]}")
            continue
        value = body[value_at:end]
        kind = kind_of(value, path, field_map)
        out.append(f"{path}:{kind}({len(value)})")
        if kind == "message":
            out += structure(value, field_map, path, depth + 1)
    return out


def varints(body: bytes, path: str, prefix: str = "", depth: int = 0) -> list[int]:
    """The values of the varint fields at `path` (field numbers joined by dots), in order - a sibling of a text field
    says what the text is (who wrote it). Messages along the way are followed whatever the guess; ProtoError when
    `body` is not a message."""
    if depth > MAX_DEPTH:
        return []
    out: list[int] = []
    for number, wire, _start, value_at, end in _fields(body):
        here = f"{prefix}.{number}" if prefix else str(number)
        if here == path and wire == VARINT:
            out.append(read_varint(body, value_at)[0])
        elif wire == LEN and path.startswith(here + ".") and is_message(body[value_at:end]):
            out += varints(body[value_at:end], path, here, depth + 1)
    return out


# ---- Connect envelopes ---------------------------------------------------------------------------------------

def envelope(flag: int, message: bytes) -> bytes:
    return bytes([flag]) + len(message).to_bytes(4, "big") + message


class FrameReader:
    """Connect envelope frames out of a byte stream that arrives in pieces: a frame split across chunks is held
    until it is whole."""

    def __init__(self, limit: int = 64 * 1024 * 1024) -> None:
        self._buf = b""
        self._limit = limit

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out = []
        while len(self._buf) >= 5:
            length = int.from_bytes(self._buf[1:5], "big")
            if length > self._limit:
                raise ProtoError(f"frame of {length} bytes")
            if len(self._buf) < 5 + length:
                break
            out.append((self._buf[0], self._buf[5:5 + length]))
            self._buf = self._buf[5 + length:]
        return out

    @property
    def pending(self) -> bytes:
        """What is left that is not a whole frame (passed on as it is at the end)."""
        return self._buf


# ---- the start-up self-test (spec, open question 5) ------------------------------------------------------------

def _sample() -> bytes:
    def field(number: int, value: bytes) -> bytes:
        return write_varint((number << 3) | LEN) + write_varint(len(value)) + value

    inner = field(9, b"reply text") + write_varint(16) + write_varint(300)
    return (field(3, b"typed message") + field(5, "Australia/Sydney".encode()) + field(7, inner)
            + write_varint(8 << 3) + write_varint(1) + bytes([(10 << 3) | I32]) + b"\x00\x00\x80\x3f")


def self_test() -> str:
    """'' when a known message shape parses and re-encodes as expected; otherwise what went wrong. Run at start:
    a reader that cannot take the shape apart refuses claude.ai's chat rather than guessing."""
    try:
        sample = _sample()
        if rewrite(sample, lambda _p, t: t) != sample:
            return "an unchanged message did not re-encode byte for byte"
        grown = rewrite(sample, lambda p, t: t + " and more" if p == "7.9" else t)
        if dict(texts(grown)).get("7.9") != "reply text and more" or dict(texts(grown)).get("3") != "typed message":
            return "a changed nested string was not re-encoded"
        frames = FrameReader().feed(envelope(0, sample)[:7] + envelope(0, sample)[7:] + envelope(END_STREAM, b"{}"))
        if [f for f, _ in frames] != [0, END_STREAM] or frames[0][1] != sample:
            return "Connect frames did not read back"
    except ProtoError as exc:
        return f"the sample did not parse ({exc})"
    return ""
