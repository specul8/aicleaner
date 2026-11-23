"""Feature 114 (spec 2026-10-22-f114-vision-opt-in.md): a picture sent with client names blacked out.

The reader that turns an image into text (Snipping Tool's engine, through Read-ImageText.ps1) also says where each line
of text sits. So a picture can be filtered as a picture:

1. the image is read for its lines and their boxes (`-Boxes`);
2. in each line, every client name (the mapping's real forms, with their disguised spellings), every unmapped
   identifier the detectors find and every secret is located, and its part of the line's box is worked out from its
   place in the line (Brad, 2026-10-23: "fill the word's box", not the whole line), a little wider for safety;
3. those boxes are filled solid in a copy (`-Fill`);
4. the copy is read again: a client name still legible means the picture is withheld (fail closed). A region the
   reader could not read is not a reason to withhold (Brad, 2026-10-23: "send anyway") - the residual risk is the
   person's choice in turning this on.

A picture with nothing to hide goes as it is.
"""
from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .convert import ConversionError, _run, reader_script, windows_powershell
from .redact import PATTERNS

PAD = 0.15                 # of the line's height, added round each box
_WORD = re.compile(r"[A-Za-z0-9]+")   # a word as Near-misses sees one (Mapper.near_miss wants letters and digits)
TIMEOUT_S = 60
# Brad, 2026-11-10: "not ballooning the file size, but still maintaining the fidelity of the original image". The copy
# that leaves is at most this many times the original's size in bytes; a picture that cannot be made to fit goes as
# its text. (Every copy was PNG before: 20 receipt photos of 8.6 MB went upstream as 64 MB.)
SIZE_CAP = 1.25

# (image bytes) -> [{"text", "x", "y", "w", "h"}]; and (image bytes, boxes[, max_bytes]) -> the redacted copy's bytes.
ReadBoxes = Callable[[bytes], list]
FillBoxes = Callable[..., bytes]


class PictureTooLarge(ConversionError):
    """No encoding of the redacted copy fitted the size cap."""


@dataclass
class Redacted:
    image: bytes | None          # the copy to send (None: withheld, or nothing to send)
    text: str                    # the picture's text, as read (filtered later with the rest of the request)
    hidden: int                  # boxes filled
    withheld: str = ""           # why it was not sent, when it was not
    media: str = "image/png"     # what `image` is, read from its bytes
    too_large: bool = False      # withheld because no encoding fitted SIZE_CAP


def media_of(data: bytes) -> str:
    """The media type of encoded image bytes, from their signature."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _fill(fill_boxes: FillBoxes, raw: bytes, boxes: list, cap: int) -> bytes:
    """The filler asked for a copy under `cap` bytes; a filler that takes no cap (a test's) is asked plainly."""
    import inspect

    try:
        params = inspect.signature(fill_boxes).parameters
        takes_cap = "max_bytes" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        takes_cap = False
    return fill_boxes(raw, boxes, max_bytes=cap) if takes_cap else fill_boxes(raw, boxes)


def spans(text: str, mapper: Any, candidates: Callable[[str], list[str]] | None = None) -> list[tuple[int, int]]:
    """Where in one line of text something must be hidden: client names, unmapped identifiers, secrets."""
    found: list[tuple[int, int]] = []
    pattern = mapper._canary_re
    for match in pattern.finditer(text):
        found.append(match.span())
    for _family, secret in PATTERNS:
        for match in secret.finditer(text):
            found.append(match.span(1) if match.lastindex else match.span())
    for value in (candidates(text) if candidates else []):
        for match in re.finditer(re.escape(value), text, re.IGNORECASE):
            found.append(match.span())
    # D232 (Brad, 2026-11-09): a term the local OCR misread ("Fabnkam" for "Fabrikam") is still the term in the
    # picture's pixels, and the AI service reads pictures better than the local engine does. The Near-misses
    # matching the text path uses decides here too, whatever the Near-misses setting says: what is hidden in a
    # picture is not a correction that could go wrong, only more black.
    near = getattr(mapper, "near_miss", None)
    if near is not None:
        for match in _WORD.finditer(text):
            if near(match.group(0)) is not None:
                found.append(match.span())
    return sorted(set(found))


def boxes_for(line: dict[str, Any], hide: list[tuple[int, int]]) -> list[dict[str, float]]:
    """Each span's part of the line's box: its share of the line's width by character position, padded."""
    text = str(line.get("text") or "")
    if not text or not hide:
        return []
    x, y, w, h = (float(line.get(k) or 0) for k in ("x", "y", "w", "h"))
    per = w / max(len(text), 1)
    pad = h * PAD
    out = []
    for start, end in hide:
        left = x + per * start - pad
        width = per * (end - start) + 2 * pad
        out.append({"x": max(left, 0.0), "y": max(y - pad, 0.0), "w": width, "h": h + 2 * pad})
    return out


def redact_picture(raw: bytes, mapper: Any, read_boxes: ReadBoxes, fill_boxes: FillBoxes,
                   candidates: Callable[[str], list[str]] | None = None) -> Redacted:
    """The picture with what must not leave blacked out, checked by reading it again."""
    lines = read_boxes(raw)
    text = "\n".join(str(line.get("text") or "") for line in lines)
    boxes = [box for line in lines for box in boxes_for(line, spans(str(line.get("text") or ""), mapper, candidates))]
    # Always a fresh single-frame encode of the frame that was read, never the original bytes (security review,
    # 2026-10-23): a multi-frame GIF or TIFF had only its first frame checked, and a picture "with nothing to hide" went
    # with its metadata (EXIF, GPS, comments) and any trailing bytes. Drawing the decoded pixels again drops all of it.
    # In the original's format at its pixel size and DPI, at most SIZE_CAP times its size (Brad, 2026-11-10).
    cap = max(1, int(len(raw) * SIZE_CAP))
    try:
        copy = _fill(fill_boxes, raw, boxes, cap)
    except PictureTooLarge as exc:
        return Redacted(None, text, len(boxes), f"the redacted copy could not be made small enough ({exc})",
                        too_large=True)
    if len(copy) > cap:                      # never send an over-cap picture, whatever the filler did
        return Redacted(None, text, len(boxes), f"the redacted copy was {len(copy)} bytes, over the {cap} cap",
                        too_large=True)
    # The copy is read again for everything the first pass hides - client names, unmapped identifiers and secrets -
    # not client names alone (security review, 2026-10-23). Anything still legible keeps the picture back. Read from
    # the final encoded bytes, the ones that will be sent: JPEG compression changes the pixels.
    again = [str(line.get("text") or "") for line in read_boxes(copy)]
    left = mapper.find_real("\n".join(again))
    still = [t for t in again if spans(t, mapper, candidates)]
    if left or still:
        what = f"a real name ({', '.join(left)})" if left else "an identifier or secret"
        return Redacted(None, text, len(boxes), f"{what} was still legible after blacking out")
    return Redacted(copy, text, len(boxes), media=media_of(copy))


# ---- pictures decided, remembered (D261) -------------------------------------------------------------------------

class Kept:
    """A bounded, thread-safe memory, least recently used out first, by count and by bytes (D261).

    The picture outcomes were kept first in, first out, 32 of them, and a hit did not refresh its entry: a conversation
    with more pictures than that, sent again in the same order with every request, found each one gone just before it
    was asked for - no hit at all, and three PowerShell runs per picture per request (38 pictures, 230 s)."""

    def __init__(self, kept: int, max_bytes: int) -> None:
        import threading
        from collections import OrderedDict

        self._items: "OrderedDict[Any, tuple[Any, int]]" = OrderedDict()
        self._bytes = 0
        self._limit = (max(1, int(kept)), max(1, int(max_bytes)))
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            return found[0]

    def put(self, key: Any, value: Any, size: int) -> None:
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, size)
            self._bytes += size
            while len(self._items) > 1 and (len(self._items) > self._limit[0] or self._bytes > self._limit[1]):
                _key, (_value, gone) = self._items.popitem(last=False)
                self._bytes -= gone

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _lines_size(lines: list) -> int:
    return sum(64 + len(str(line.get("text") or "")) if isinstance(line, dict) else 64 for line in lines)


class PictureMemory:
    """The box reader and the box filler, remembering what they said (D261; see picturestore.py for why this is safe).

    In memory first (bounded, least recently used out), then on disk when a store is given (only for the real reader,
    with `reader` its fingerprint), then the reader itself. `redact_picture` is handed these in place of the reader and
    so makes every decision as before, against the mapping it is given: only the reader's runs are saved. A failure
    (timeout, error) is never kept; a copy that could not fit the size cap is (same bytes, boxes and cap, same answer).
    """

    def __init__(self, read_boxes: ReadBoxes, fill_boxes: FillBoxes, store: Any = None, reader: str = "",
                 kept: int = 512, max_bytes: int = 128_000_000) -> None:
        self.readers = (read_boxes, fill_boxes)
        self.store = store
        self.reader = reader
        self._readings = Kept(kept, max_bytes)
        self._copies = Kept(kept, max_bytes)

    def read(self, raw: bytes) -> tuple[list, bool]:
        """(the lines and boxes read from `raw`, whether they were remembered rather than read)."""
        from .picturestore import key_of

        key = key_of(raw, self.reader)
        found = self._readings.get(key)
        if found is None and self.store is not None:
            stored = self.store.get("read", key)
            if isinstance(stored, dict) and isinstance(stored.get("lines"), list):
                found = stored["lines"]
                self._readings.put(key, found, _lines_size(found))
        if found is not None:
            return found, True
        lines = self.readers[0](raw)                  # a ConversionError goes to the caller, and nothing is kept
        lines = lines if isinstance(lines, list) else [lines]
        self._readings.put(key, lines, _lines_size(lines))
        if self.store is not None:
            self.store.put("read", key, {"lines": lines})
        return lines, False

    def fill(self, raw: bytes, boxes: list, cap: int) -> tuple[bytes, bool]:
        """(the copy with exactly `boxes` filled, at most `cap` bytes; whether it was remembered). PictureTooLarge as
        the filler said it."""
        from .picturestore import key_of

        key = key_of(raw, boxes, int(cap), self.reader)
        found = self._copies.get(key)
        if found is None and self.store is not None:
            stored = self.store.get("fill", key)
            if isinstance(stored, dict) and "too_large" in stored:
                found = PictureTooLarge(str(stored["too_large"]))
            elif isinstance(stored, dict) and isinstance(stored.get("copy"), str):
                import base64
                found = base64.b64decode(stored["copy"])
            if found is not None:
                self._copies.put(key, found, len(found) if isinstance(found, bytes) else 256)
        if found is not None:
            if isinstance(found, PictureTooLarge):
                again = PictureTooLarge(str(found))
                again.remembered = True              # type: ignore[attr-defined] - not a reader run (the tally)
                raise again
            return found, True
        try:
            copy = _fill(self.readers[1], raw, boxes, cap)
        except PictureTooLarge as exc:
            self._copies.put(key, PictureTooLarge(str(exc)), 256)
            if self.store is not None:
                self.store.put("fill", key, {"too_large": str(exc)})
            raise
        self._copies.put(key, copy, len(copy))
        if self.store is not None:
            import base64
            self.store.put("fill", key, {"copy": base64.b64encode(copy).decode("ascii")})
        return copy, False


class PictureTally:
    """One request's pictures, for the upstream line's `filter.pictures` (D261): counts and times only, never text.

    `seen` pictures (image blocks); `cached` - answered from memory with no reader run; `processed` - the reader ran
    for them (a PowerShell process, OCR) - the time a picture-heavy request goes to. `ms` per stage."""

    def __init__(self) -> None:
        self.seen = 0
        self.cached = 0
        self.processed = 0
        self.work = 0                     # reader runs so far in this request (box reads, fills, text reads)
        self.runs = {"read_boxes": 0, "fill": 0, "read_text": 0}
        self.ms: dict[str, float] = {}

    def timed(self, stage: str, started: float) -> None:
        import time

        self.ms[stage] = self.ms.get(stage, 0.0) + (time.perf_counter() - started) * 1000

    def ran(self, stage: str) -> None:
        self.runs[stage] += 1
        self.work += 1

    def block(self, work_before: int) -> None:
        """One picture done: processed when a reader ran for it, else cached."""
        self.seen += 1
        if self.work > work_before:
            self.processed += 1
        else:
            self.cached += 1

    def summary(self) -> dict[str, Any]:
        return {"seen": self.seen, "cached": self.cached, "processed": self.processed, "runs": dict(self.runs),
                "ms": {k: round(v, 1) for k, v in self.ms.items()}}


# ---- the reader, through Read-ImageText.ps1 ----------------------------------------------------------------------

def windows_readers(timeout: int = TIMEOUT_S) -> tuple[ReadBoxes, FillBoxes] | None:
    """The box reader and the box filler, or None where the reader cannot run (not Windows, no script)."""
    shell = windows_powershell()
    script = reader_script()
    if not shell or not script.exists():
        return None
    base = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]

    def read_boxes(raw: bytes) -> list:
        with tempfile.TemporaryDirectory(prefix="aicleaner-pic-") as tmp:
            src = Path(tmp) / "in.png"
            src.write_bytes(raw)
            proc = _run([*base, "-Path", str(src), "-Boxes"], timeout)
            if proc.returncode != 0:
                raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200])
            out = proc.stdout.decode("utf-8", "replace").strip()
            found = json.loads(out) if out else []
            return found if isinstance(found, list) else [found]

    def fill_boxes(raw: bytes, boxes: list, max_bytes: int = 0) -> bytes:
        with tempfile.TemporaryDirectory(prefix="aicleaner-pic-") as tmp:
            # The names' extensions do not matter: both sides read the format from the bytes.
            src, rects, dst = Path(tmp) / "in.png", Path(tmp) / "boxes.json", Path(tmp) / "out.png"
            src.write_bytes(raw)
            rects.write_text(json.dumps(boxes), encoding="utf-8")
            proc = _run([*base, "-Path", str(src), "-Fill", str(rects), "-OutPath", str(dst),
                         "-MaxBytes", str(int(max_bytes))], timeout)
            said = proc.stdout.decode("utf-8", "replace").strip()
            try:
                outcome = json.loads(said.splitlines()[-1]) if said else {}
            except ValueError:
                outcome = {}
            if proc.returncode == 0 and isinstance(outcome, dict) and outcome.get("tooLarge"):
                raise PictureTooLarge(f"smallest {outcome.get('smallest')} bytes, cap {outcome.get('cap')}")
            if proc.returncode != 0 or not dst.exists():
                raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200] or "the copy was not written")
            return dst.read_bytes()

    return read_boxes, fill_boxes
