"""Whether a file of a type AI Cleaner does not know is text (feature 172, Brad 2026-11-09).

Brad: "check the start of the file to see if they only contain text, and offer the option to 'view it' ...
make sure the 'is this a file containing text' is careful not to execute code from the file itself."

So this looks at bytes and nothing else: the first SNIFF_BYTES of the file, read as bytes. It never
opens the file with another program, never follows a file association, never parses a format, and
never guesses a language. "Text" is:
- valid UTF-8 (a byte-order mark allowed), or UTF-16 that starts with its byte-order mark;
- no NUL at all (UTF-8 has none in text; UTF-16 text decodes to none);
- under 2% control characters other than tab, CR and LF (C0, DEL and C1 all count).
A sample cut off in the middle of a character is judged on the whole characters before the cut.
"""
from __future__ import annotations

import codecs
from pathlib import Path

SNIFF_BYTES = 8 * 1024
MAX_CONTROL_SHARE = 0.02
_ALLOWED_CONTROLS = {"\t", "\r", "\n"}
_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_BOMS = {b"\xff\xfe": "utf-16-le", b"\xfe\xff": "utf-16-be"}


def _control(char: str) -> bool:
    code = ord(char)
    return (code < 0x20 and char not in _ALLOWED_CONTROLS) or 0x7F <= code <= 0x9F


def _decode(sample: bytes, encoding: str, whole: bool) -> str | None:
    """The sample as text, or None when it is not valid in `encoding`. Not `whole`: the end may be a
    character cut in two, which the incremental decoder holds back rather than calls invalid."""
    try:
        return codecs.getincrementaldecoder(encoding)("strict").decode(sample, final=whole)
    except UnicodeDecodeError:
        return None


def text_encoding(sample: bytes, whole: bool = True) -> str | None:
    """The encoding to read `sample` with when it is text ("utf-8", "utf-8-sig", "utf-16"), else None.
    `whole`: the sample is the whole file, so a cut-off last character is a fault, not the cut."""
    encoding = None
    body = sample
    for bom, name in _UTF16_BOMS.items():
        if sample.startswith(bom):
            encoding, body = name, sample[len(bom):]
            break
    if encoding is None:
        if b"\x00" in sample:
            return None
        if sample.startswith(_UTF8_BOM):
            encoding, body = "utf-8", sample[len(_UTF8_BOM):]
        else:
            encoding = "utf-8"
    text = _decode(body, encoding, whole)
    if text is None or "\x00" in text:
        return None
    controls = sum(1 for char in text if _control(char))
    if text and controls / len(text) >= MAX_CONTROL_SHARE:
        return None
    if encoding.startswith("utf-16"):
        return "utf-16"
    return "utf-8-sig" if sample.startswith(_UTF8_BOM) else "utf-8"


def sniff_file(path: Path) -> str | None:
    """text_encoding for the first SNIFF_BYTES of a file, read as bytes and nothing more. The caller
    has already put the path through the tree's allow-list (network and device paths refused)."""
    with Path(path).open("rb") as fh:
        sample = fh.read(SNIFF_BYTES + 1)
    whole = len(sample) <= SNIFF_BYTES
    return text_encoding(sample[:SNIFF_BYTES], whole=whole)
