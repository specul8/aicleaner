"""Can these bytes become text on this machine, and what is the text?

That is the only question here. What to do when the answer is no belongs to `attachments.py`,
which keeps the policy: an attachment that cannot be read is replaced by a notice, so nothing
leaves uninspected either way. Widening what can be read does not widen what goes out.

Every converter is a callable the proxy wires once at startup, the way `pdftotext` already was,
so a test passes a stub instead of shelling out to anything. A converter returns None when there
is no tool for the job on this machine - not an error, just an absence - and raises
ConversionError when a tool ran and could not do it.

Nothing here is a new dependency. PDFs use Poppler, which is already installed for the text
path; Office documents are read with zipfile and ElementTree; OCR uses Tesseract if somebody has
installed it and otherwise the engine that ships with Windows.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path
from typing import Callable

PdfToText = Callable[[bytes], "str | None"]
ImageToText = Callable[[bytes, str], "str | None"]
OfficeToText = Callable[[bytes, str], "str | None"]
# Handed text a converter produced, on its way into the body: the caller puts right what
# the engine misread, and keeps a copy of what was read.
Repair = Callable[[str], str]

OCR_DPI = 150                    # enough for 10pt text, cheap enough for a ten-page scan
# Runs of spaces carry meaning here: the reader marks a column break with two, and pdftotext
# -layout aligns columns with many. Collapsed to two rather than to one, so a table stays a table
# without carrying a screenful of padding - flattening them turns a timesheet back into a list.
SPACES = re.compile(r"[ \t]{2,}")
BLANK_LINES = re.compile(r"\n{3,}")

# The OOXML parts worth reading, and the elements that hold words in each. Headers, footers,
# comments, tracked changes and speaker notes are all in here: a document library skips them by
# design, and they are exactly where a client's name hides.
OFFICE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "word",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "powerpoint",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "word",
    "application/vnd.openxmlformats-officedocument.presentationml.slideshow": "powerpoint",
    "application/vnd.ms-excel.sheet.macroEnabled.12": "excel",
    "application/vnd.ms-word.document.macroEnabled.12": "word",
    "application/vnd.ms-powerpoint.presentation.macroEnabled.12": "powerpoint",
}
OFFICE_PARTS = ("word/", "ppt/", "xl/", "docProps/")
# <w:t> in Word, <a:t> in PowerPoint and in every drawing, <t> in Excel's shared strings.
TEXT_TAGS = {"t", "delText", "instrText"}
OFFICE_MAX_PARTS = 2000          # a zip with a hundred thousand members is not a document
OFFICE_MAX_PART_BYTES = 50_000_000     # one part, uncompressed: past this it is a zip bomb
OFFICE_MAX_TOTAL_BYTES = 200_000_000   # and a budget across the whole document
# A DOCTYPE in an OOXML part is never legitimate, and is how an entity-expansion bomb gets in.
# ElementTree does not fetch external entities, but it will happily expand internal ones until
# the machine gives up, so a part declaring any is refused before it is parsed rather than
# reaching for a third-party parser to do the same job.
DOCTYPE = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)


class ConversionError(RuntimeError):
    """A tool ran and could not convert the file (encrypted, damaged, out of time)."""


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout, creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConversionError(str(exc)) from exc


def tidy(text: str) -> str:
    """Collapse the whitespace a converter leaves behind, keeping the line breaks."""
    lines = [SPACES.sub("  ", line).rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


# ---- PDF ----------------------------------------------------------------------------------

def pdftotext_converter(path: str | None = None, timeout: int = 30) -> PdfToText:
    """Poppler's pdftotext (-layout keeps table columns side by side).

    Returns None from the call when the tool is not installed.
    """
    exe = path or shutil.which("pdftotext")

    def convert(raw: bytes) -> str | None:
        if not exe or not Path(exe).exists():
            return None
        with tempfile.TemporaryDirectory(prefix="aicleaner-pdf-") as tmp:
            src = Path(tmp) / "in.pdf"
            src.write_bytes(raw)
            proc = _run([exe, "-layout", "-enc", "UTF-8", str(src), "-"], timeout)
            if proc.returncode != 0:
                raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200])
            return proc.stdout.decode("utf-8", "replace")

    return convert


def pdf_pages_to_images(raw: bytes, max_pages: int, timeout: int,
                        path: str | None = None, dpi: int = OCR_DPI) -> list[bytes] | None:
    """A scanned PDF as one PNG per page, through Poppler's pdftoppm.

    pdftoppm ships beside pdftotext, so wherever the text path works this does too. Returns None
    when it is not installed.
    """
    exe = path or shutil.which("pdftoppm")
    if not exe or not Path(exe).exists():
        return None
    with tempfile.TemporaryDirectory(prefix="aicleaner-scan-") as tmp:
        src = Path(tmp) / "in.pdf"
        src.write_bytes(raw)
        proc = _run([exe, "-png", "-r", str(dpi), "-f", "1", "-l", str(max(1, max_pages)),
                     str(src), str(Path(tmp) / "page")], timeout)
        if proc.returncode != 0:
            raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200])
        return [page.read_bytes() for page in sorted(Path(tmp).glob("page*.png"))][:max_pages]


# ---- images ------------------------------------------------------------------------------

def find_tesseract() -> str | None:
    """Tesseract on PATH, or where its Windows installer puts it, or None.

    The build winget installs (UB-Mannheim.TesseractOCR) leaves PATH alone, so a PATH-only probe
    reported no Tesseract on a machine that had it (2026-10-13). Read-ImageText.ps1 looks in the
    same places.
    """
    found = shutil.which("tesseract")
    if found:
        return found
    if os.name != "nt":
        return None
    for variable, folder in (("ProgramFiles", "Tesseract-OCR"), ("ProgramFiles(x86)", "Tesseract-OCR"),
                             ("LOCALAPPDATA", r"Programs\Tesseract-OCR")):
        base = os.environ.get(variable)
        if base and (Path(base) / folder / "tesseract.exe").is_file():
            return str(Path(base) / folder / "tesseract.exe")
    return None


def tesseract_converter(path: str | None = None, timeout: int = 60) -> ImageToText | None:
    """Tesseract alone, for a machine without Windows' engine. None when it is not installed."""
    exe = path or find_tesseract()
    if not exe:
        return None

    def convert(raw: bytes, media: str = "") -> str | None:
        with tempfile.TemporaryDirectory(prefix="aicleaner-ocr-") as tmp:
            src = Path(tmp) / "in.img"
            src.write_bytes(raw)
            proc = _run([exe, str(src), "stdout"], timeout)
            if proc.returncode != 0:
                raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200])
            return proc.stdout.decode("utf-8", "replace")

    return convert




def windows_powershell() -> str | None:
    """Windows PowerShell 5.1 by its full path, or None off Windows.

    It has to be 5.1: the WinRT projections were taken out of PowerShell 7, so the OCR type
    cannot be loaded there at all. This is the same reason Capture-TextFromScreen.ps1 refuses
    PS7, and the full System32 path is the same idiom restart.py uses.
    """
    if os.name != "nt":
        return None
    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(exe) if exe.exists() else None


def reader_script() -> Path:
    """Read-ImageText.ps1, in the app folder's installer\\ (feature 102), or its root before the move."""
    root = Path(__file__).resolve().parent.parent
    moved = root / "installer" / "Read-ImageText.ps1"
    return moved if moved.exists() else root / "Read-ImageText.ps1"   # an install from before the move


def windows_ocr_converter(timeout: int = 60) -> ImageToText | None:
    """The OCR engine that ships with Windows, through the reader both callers share.

    None when this is not Windows, or when the reader is not beside the package - an install
    from before it existed still runs, and images stay a notice there rather than failing.

    The reader rebuilds rows and columns from the word boxes rather than returning the engine's
    lines. That is not decoration: a screenshot of a timesheet read as lines puts every entry in
    reading order with no day attached to any of them, which is worse than useless to reason
    about even though the words are all present.
    """
    shell = windows_powershell()
    if not shell:
        return None
    script = reader_script()
    if not script.exists():
        return None

    def convert(raw: bytes, media: str = "") -> str | None:
        with tempfile.TemporaryDirectory(prefix="aicleaner-ocr-") as tmp:
            src = Path(tmp) / "in.png"
            src.write_bytes(raw)
            proc = _run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-File", str(script), "-Path", str(src)], timeout)
            if proc.returncode != 0:
                raise ConversionError(proc.stderr.decode("utf-8", "replace").strip()[:200])
            return proc.stdout.decode("utf-8", "replace")

    return convert


def image_converter(timeout: int = 60) -> ImageToText | None:
    """The shared reader first, then Tesseract alone, then none.

    The reader (Read-ImageText.ps1) keeps rows, columns and indentation, and reads with Tesseract
    as well as Windows' engine when Tesseract is installed, keeping the better reading of each
    word (2026-10-13). Tesseract alone, as plain lines, only where Windows' engine cannot run.
    Neither is a dependency.
    """
    return windows_ocr_converter(timeout=timeout) or tesseract_converter(timeout=timeout)


def ocr_engine_name() -> str:
    """Which engines would read an image, for the health check and the installer to report."""
    windows = bool(windows_powershell()) and reader_script().exists()
    tesseract = bool(find_tesseract())
    if windows and tesseract:
        return "windows+tesseract"
    return "windows" if windows else ("tesseract" if tesseract else "")


# ---- Office documents ---------------------------------------------------------------------

def _part_text(data: bytes) -> list[str]:
    """Every run of words in one XML part, in document order.

    A part that declares a DTD or an entity is refused unparsed: the file came from outside, and
    that is the shape an expansion bomb arrives in.
    """
    found: list[str] = []
    if DOCTYPE.search(data[:4096]):
        raise ConversionError("the document declares XML entities")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return found
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in TEXT_TAGS and element.text:
            found.append(element.text)
        elif tag in ("br", "cr", "p") and found and found[-1] != "\n":
            found.append("\n")
    return found


def office_to_text(raw: bytes, media: str = "") -> str | None:
    """The words in a .docx, .pptx or .xlsx, read straight out of the OOXML parts.

    Deliberately not python-docx, python-pptx and openpyxl: those are installed for the agent, in
    the user's Python, and for filtering every word counts while fidelity does not. Walking the
    parts also reads headers, footers, comments, tracked changes and speaker notes, which the
    document libraries skip by design and where a client's name is most likely to be sitting.

    Returns None when the bytes are not an OOXML container - a legacy .doc or .xls is a binary
    format and stays a notice.
    """
    from io import BytesIO

    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except (zipfile.BadZipFile, OSError):
        return None
    words: list[str] = []
    try:
        names = [n for n in archive.namelist()[:OFFICE_MAX_PARTS]
                 if n.endswith(".xml") and n.startswith(OFFICE_PARTS)]
        if not names:
            return None
        budget = OFFICE_MAX_TOTAL_BYTES
        for name in sorted(names):
            try:
                # The declared size, before anything is decompressed: a zip bomb is small on
                # disk and enormous in memory, and this is the only place that is visible.
                size = archive.getinfo(name).file_size
                if size > OFFICE_MAX_PART_BYTES or size > budget:
                    raise ConversionError("the document is larger than it can usefully be")
                budget -= size
                words += _part_text(archive.read(name))
                words.append("\n")
            except (KeyError, OSError, zipfile.BadZipFile):
                continue
    finally:
        archive.close()
    text = tidy("".join(words))
    return text or ""
