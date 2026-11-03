"""Attachments in a request (review finding 4): nothing leaves uninspected.

A client name inside a base64 PDF or image used to reach the provider untouched, because the
filter only sees text. Every attachment is now either turned into text that is filtered like the
rest of the request, or withheld and replaced by a short notice the AI can act on. Replacing
rather than refusing keeps the conversation usable: Claude Code resends earlier attachments with
every turn.

What can be read is decided by `convert.py`, and depends on what is installed:

    PDF with a text layer   pdftotext (Poppler)
    Scanned PDF             pdftoppm, then OCR of each page
    .docx, .pptx, .xlsx     the OOXML parts, with the standard library
    image                   OCR - Tesseract if present, otherwise the Windows engine

Anything that cannot be read is still a notice, so widening what can be read never widens what
goes out. Images can also be *sent* with one switch; then only the types the provider accepts go,
and they go unfiltered. That is a different question from whether their text can be read, and it
stays a separate switch.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from .convert import (OFFICE_TYPES, ConversionError, ImageToText, OfficeToText, PdfToText,
                      Repair, office_to_text, pdf_pages_to_images, pdftotext_converter, tidy)

log = logging.getLogger("aicleaner")

__all__ = ["ACCEPTED_IMAGE_TYPES", "ConversionError", "PdfToText", "pdftotext_converter", "scrub"]

ACCEPTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_URL_IMAGE_EXT = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
MIN_CHARS_PER_PAGE = 20  # fewer than this on average means a scanned PDF (no text layer)
MIN_OCR_CHARS = 8        # less than this back from an engine is noise, not a reading
_DATA_URL = re.compile(r"data:([^;,]+)(?:;[^,]*)?,(.*)", re.S)


def _notice(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _the_pdf(name: str) -> str:
    return f"the PDF {name}" if name else "a PDF"


def _the_file(name: str, what: str) -> str:
    return f"the {what} {name}" if name else f"a {what}"


def _url_image_type(url: str) -> str:
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    return _URL_IMAGE_EXT.get(last.rsplit(".", 1)[-1].lower(), "") if "." in last else ""


def _decode(data_b64: str) -> bytes | None:
    try:
        return base64.b64decode(data_b64, validate=False)
    except (binascii.Error, ValueError):
        return None


class _Scrubber:
    def __init__(self, allow_images: bool, pdf_to_text: PdfToText,
                 image_to_text: ImageToText | None = None,
                 office: OfficeToText | None = office_to_text,
                 repair: Repair | None = None,
                 ocr_max_pages: int = 10, ocr_timeout_s: int = 60,
                 ocr_max_bytes: int = 20_000_000, picture: Any = None):
        self.allow_images = allow_images
        # Feature 114: (image bytes) -> picture.Redacted, when pictures go with client names blacked out.
        self.picture = picture
        self.pdf_to_text = pdf_to_text
        self.image_to_text = image_to_text
        self.office = office
        self.repair = repair
        self.ocr_max_pages = max(1, int(ocr_max_pages))
        self.ocr_timeout_s = max(1, int(ocr_timeout_s))
        self.ocr_max_bytes = max(0, int(ocr_max_bytes))
        self.counts: Counter[str] = Counter()

    # ---- reading -------------------------------------------------------------------------

    def _read_image(self, raw: bytes | None, media: str = "") -> str | None:
        """The words in one image, or None when there is no engine, no bytes, or nothing found."""
        if not raw or self.image_to_text is None:
            return None
        if self.ocr_max_bytes and len(raw) > self.ocr_max_bytes:
            return None
        try:
            found = self.image_to_text(raw, media)
        except ConversionError:
            return None
        text = tidy(found or "")
        return text if len(re.sub(r"\s", "", text)) >= MIN_OCR_CHARS else None

    def _read_scan(self, raw: bytes) -> str | None:
        """A PDF with no text layer, one page at a time, as far as the page cap allows."""
        if self.image_to_text is None:
            return None
        try:
            pages = pdf_pages_to_images(raw, self.ocr_max_pages, self.ocr_timeout_s)
        except ConversionError:
            return None
        if not pages:
            return None
        read = [self._read_image(page, "image/png") or "" for page in pages]
        text = tidy("\n\n".join(part for part in read if part))
        return text if len(re.sub(r"\s", "", text)) >= MIN_OCR_CHARS else None

    def _read(self, text: str) -> str:
        """Text a converter produced, on its way into the body.

        This is the one place a misreading is expected, so it is the one place near-miss repair
        belongs: a letter going wrong is what an OCR engine does, and what a person typing does
        not. Applied here rather than to the whole request, where nearly every match would be a
        long ordinary word being replaced by a client's token.
        """
        return self.repair(text) if self.repair else text

    # ---- outcomes ------------------------------------------------------------------------

    def other(self, what: str) -> dict[str, str]:
        self.counts["other_withheld"] += 1
        return _notice(f"[aicleaner: an attachment ({what or 'unknown type'}) was withheld: it cannot be checked for client names.]")

    def image(self, block: dict[str, Any], media: str, raw: bytes | None = None) -> Any:
        if self.allow_images and media in ACCEPTED_IMAGE_TYPES:
            self.counts["image_allowed"] += 1
            return block
        # Anthropic's image block only: the copy goes back in the same shape (an OpenAI image_url stays text only).
        if self.picture is not None and raw and media in ACCEPTED_IMAGE_TYPES and block.get("type") == "image":
            sent = self._redacted(raw)
            if sent is not None:
                return sent
        text = self._read(self._read_image(raw, media) or "")
        if text:
            self.counts["image_read"] += 1
            return _notice("[aicleaner: an image was read on this machine and replaced by its text, which is "
                           f"approximate; the image itself was not sent.]\n\n{text}")
        self.counts["image_withheld"] += 1
        return _notice(f"[aicleaner: an image ({media or 'unknown type'}) was withheld: images are not sent to "
                       "the AI service. Describe it, or paste its text.]")

    def _redacted(self, raw: bytes) -> list[dict[str, Any]] | None:
        """Feature 114: the picture with client names blacked out, and its text (filtered with the rest of the
        request), or None to fall back to text only (the reader failed, or a name stayed legible)."""
        try:
            result = self.picture(raw)
        except (ConversionError, OSError, ValueError) as exc:
            # Said in the log (D218, 2026-10-29): a failure here fell back to text without a trace, which hid that
            # redaction had only ever worked for a picture with exactly one box to fill.
            log.warning("redacting a picture failed, so it went as its text: %s", exc)
            self.counts["image_redaction_failed"] += 1
            return None
        if result.image is None:
            self.counts["image_redaction_refused"] += 1
            return None
        self.counts["image_redacted" if result.hidden else "image_clean"] += 1
        said = (f"[aicleaner: this image was sent with {result.hidden} client name(s) or identifier(s) blacked out; "
                "its readable text follows.]" if result.hidden else
                "[aicleaner: this image was checked on this machine and had no client names; its readable text follows.]")
        media = "image/png"                    # always re-encoded (feature 114 security review)
        picture = {"type": "image", "source": {"type": "base64", "media_type": media,
                                               "data": base64.b64encode(result.image).decode("ascii")}}
        return [picture, _notice(f"{said}\n\n{self._read(result.text)}" if result.text.strip() else said)]

    def pdf(self, data_b64: str, name: str) -> dict[str, str]:
        raw = _decode(data_b64)
        if raw is None:
            return self.other("application/pdf, unreadable")
        try:
            text = self.pdf_to_text(raw)
        except ConversionError:
            self.counts["pdf_withheld"] += 1
            return _notice(f"[aicleaner: {_the_pdf(name)} was withheld: it could not be converted to text on this "
                           "machine. Paste or describe the parts you need.]")
        if text is None:
            self.counts["pdf_withheld"] += 1
            return _notice(f"[aicleaner: {_the_pdf(name)} was withheld: the local PDF text tool (pdftotext) is not installed.]")
        pages = max(1, text.count("\f"))
        if len(re.sub(r"\s", "", text)) < MIN_CHARS_PER_PAGE * pages:
            # No text layer: a scan. Rasterise it and read the pages instead.
            scanned = self._read(self._read_scan(raw) or "")
            if scanned:
                self.counts["pdf_read"] += 1
                return _notice(f"[aicleaner: {_the_pdf(name)} is a scan; up to {self.ocr_max_pages} pages were read "
                               f"on this machine and replaced by their text, which is approximate.]\n\n{scanned}")
            self.counts["pdf_withheld"] += 1
            return _notice(f"[aicleaner: {_the_pdf(name)} was withheld: it has no text layer (scanned) and could not "
                           "be read on this machine. Paste or describe the parts you need.]")
        self.counts["pdf_converted"] += 1
        body = self._read(text.replace("\f", "\n").strip())
        return _notice(f"[aicleaner: {_the_pdf(name)} was converted to text on this machine; the layout is approximate.]\n\n{body}")

    def document(self, data_b64: str, media: str, name: str) -> dict[str, str]:
        """A Word document, deck or workbook, read out of its OOXML parts."""
        what = OFFICE_TYPES.get(media, "document")
        raw = _decode(data_b64)
        if raw is None or self.office is None:
            return self.other(media)
        try:
            text = self.office(raw, media)
        except ConversionError as exc:
            self.counts["office_withheld"] += 1
            return _notice(f"[aicleaner: {_the_file(name, what + ' document')} was withheld: it could not be read on "
                           f"this machine ({exc}). Paste or describe the parts you need.]")
        if text is None:
            return self.other(media)
        if not text.strip():
            self.counts["office_withheld"] += 1
            return _notice(f"[aicleaner: {_the_file(name, what + ' document')} was withheld: no text could be read "
                           "from it. Paste or describe the parts you need.]")
        self.counts["office_converted"] += 1
        return _notice(f"[aicleaner: {_the_file(name, what + ' document')} was read on this machine and replaced by "
                       f"its text; the layout is approximate.]\n\n{self._read(text)}")

    # ---- one content block -----------------------------------------------------------------

    def _base64_document(self, block: dict[str, Any], media: str, data: str, name: str) -> dict[str, Any]:
        """One attachment carried as base64, whatever wrapper it arrived in.

        The original block is carried through because an image may be allowed rather than read,
        and `allow_images` returns the block itself - a stand-in would blank the attachment.
        """
        if media == "application/pdf":
            return self.pdf(data, name)
        if media in OFFICE_TYPES:
            return self.document(data, media, name)
        if media.startswith("image/"):
            return self.image(block, media, _decode(data))
        return self.other(media)

    def block(self, b: dict[str, Any]) -> dict[str, Any] | None:
        """A replacement for an attachment block, or None when b is not one (keep walking)."""
        kind = b.get("type")
        src = b.get("source") if isinstance(b.get("source"), dict) else None
        if kind == "image":
            stype = (src or {}).get("type")
            if stype == "base64":
                return self.image(b, str(src.get("media_type", "")), _decode(str(src.get("data", ""))))
            if stype == "url":
                # Nothing is fetched: reading it would mean this machine going to get it.
                return self.image(b, _url_image_type(str(src.get("url", ""))))
            return self.other("image file")
        if kind == "document" and src is not None:
            stype = src.get("type")
            if stype == "base64":
                return self._base64_document(b, str(src.get("media_type", "")), str(src.get("data", "")),
                                             str(b.get("title") or ""))
            if stype == "file":
                return self.other("stored file")
            return None  # url, text and content sources are plain references or strings
        if kind == "image_url":
            url = str((b.get("image_url") or {}).get("url", ""))
            m = _DATA_URL.match(url)
            if m:
                return self.image(b, m.group(1), _decode(m.group(2)))
            return self.image(b, _url_image_type(url))
        if kind == "file":
            f = b.get("file") or {}
            m = _DATA_URL.match(str(f.get("file_data", "")))
            if m:
                return self._base64_document(b, m.group(1), m.group(2), str(f.get("filename") or ""))
            return self.other("stored file")
        if kind in ("input_audio", "audio"):
            return self.other("audio")
        if src is not None and src.get("type") == "base64":
            return self._base64_document(b, str(src.get("media_type", "")), str(src.get("data", "")),
                                         str(b.get("title") or ""))
        return None

    def walk(self, x: Any) -> Any:
        if isinstance(x, list):
            out: list[Any] = []
            for item in x:
                done = self.walk(item)
                # A redacted picture becomes two blocks, the picture and its text, side by side (feature 114).
                if isinstance(done, list) and isinstance(item, dict) and item.get("type") == "image":
                    out.extend(done)
                else:
                    out.append(done)
            return out
        if isinstance(x, dict):
            if "type" in x:
                replacement = self.block(x)
                if replacement is not None:
                    return replacement
            return {k: self.walk(v) for k, v in x.items()}
        return x


def scrub(obj: Any, allow_images: bool, pdf_to_text: PdfToText, *,
          image_to_text: ImageToText | None = None, office: OfficeToText | None = office_to_text,
          repair: Repair | None = None, ocr_max_pages: int = 10, ocr_timeout_s: int = 60,
          ocr_max_bytes: int = 20_000_000, picture: Any = None) -> tuple[Any, Counter[str]]:
    """Rewrite every attachment in a parsed request; returns the new body and outcome counts.

    `repair` is handed the text a converter produced, and nothing else. The caller uses it to put
    right the letters an OCR engine got wrong, and to keep what was read.
    """
    s = _Scrubber(allow_images, pdf_to_text, image_to_text=image_to_text, office=office,
                  repair=repair, ocr_max_pages=ocr_max_pages, ocr_timeout_s=ocr_timeout_s,
                  ocr_max_bytes=ocr_max_bytes, picture=picture)
    return s.walk(obj), s.counts
