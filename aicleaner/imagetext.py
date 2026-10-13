"""Reading the text in an image or PDF for the Session page's prompt (feature 113).

The page pastes, drops or uploads a file, or names one in the session's folders; this turns it into
text with the same readers the proxy uses for attachments (`convert.py`: Snipping Tool's engine
through Read-ImageText.ps1 when the user has it, a PDF's own text layer when it has one, its pages
read as images when it is a scan). The text goes back to the page to review in the prompt box;
nothing is sent to the model from here, and the file is not kept.
"""

from __future__ import annotations

from typing import Callable

from .convert import ConversionError, ImageToText, PdfToText, pdf_pages_to_images

MAX_BYTES = 20 * 1024 * 1024
MIN_CHARS_PER_PAGE = 20        # less than this on average: a scan, so its pages are read as images

# The first bytes of each kind accepted, so the name a file arrives with decides nothing.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"), (b"BM", "image/bmp"), (b"II*\x00", "image/tiff"), (b"MM\x00*", "image/tiff"),
    (b"%PDF-", "application/pdf"),
)


def _trimmed(text: str) -> str:
    """Line ends trimmed and blank runs shortened - and nothing else. Not convert.tidy, which shortens
    runs of spaces: here a person reads the text in the prompt box, so the columns stay aligned."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    return "\n".join(kept).strip("\n")


class ReadRefused(ValueError):
    """The file is not one this reads, or too big; the message says which, for the person."""


def kind_of(raw: bytes) -> str:
    """The media type the bytes are, by their signature, or "" for anything else."""
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    for signature, media in _SIGNATURES:
        if raw.startswith(signature):
            return media
    return ""


def read_text(raw: bytes, name: str, image_to_text: ImageToText | None, pdf_to_text: PdfToText | None,
              max_pages: int, timeout: int,
              pages_of: Callable[[bytes, int, int], "list[bytes] | None"] = pdf_pages_to_images) -> str:
    """The text of one image or PDF, layout kept. Raises ReadRefused for what is not read here,
    ConversionError when a reader fails."""
    if len(raw) > MAX_BYTES:
        raise ReadRefused(f"{name or 'the file'} is {len(raw) // (1024 * 1024)} MB; the limit is "
                          f"{MAX_BYTES // (1024 * 1024)} MB")
    media = kind_of(raw)
    if not media:
        raise ReadRefused(f"{name or 'the file'} is not an image or a PDF")
    if media == "application/pdf":
        return _read_pdf(raw, name, image_to_text, pdf_to_text, max_pages, timeout, pages_of)
    if image_to_text is None:
        raise ReadRefused("no engine on this machine reads images (Snipping Tool, or Windows' OCR language pack)")
    return _trimmed(image_to_text(raw, media) or "")


def _read_pdf(raw: bytes, name: str, image_to_text: ImageToText | None, pdf_to_text: PdfToText | None,
              max_pages: int, timeout: int, pages_of: Callable[[bytes, int, int], "list[bytes] | None"]) -> str:
    text = (pdf_to_text(raw) or "") if pdf_to_text is not None else ""
    pages = max(1, text.count("\f"))                 # pdftotext ends every page with a form feed
    if len(text.strip()) >= MIN_CHARS_PER_PAGE * pages:
        return _trimmed(text.replace("\f", "\n"))
    if image_to_text is None:
        raise ReadRefused(f"{name or 'the PDF'} is a scan, and no engine on this machine reads images")
    images = pages_of(raw, max_pages, timeout)
    if not images:
        raise ReadRefused(f"{name or 'the PDF'} has no text layer, and its pages could not be rendered "
                          "(pdftoppm, part of Poppler, is not installed)")
    read = [_trimmed(image_to_text(page, "image/png") or "") for page in images]
    if len(read) == 1:
        return read[0]
    return "\n\n".join(f"--- page {number} ---\n{page}" for number, page in enumerate(read, 1))


__all__ = ["MAX_BYTES", "ConversionError", "ReadRefused", "kind_of", "read_text"]
