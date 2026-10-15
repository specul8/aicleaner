<!-- aicleaner:start -->
# Client-data handling (aicleaner)

All model traffic from this machine goes through a local proxy that swaps client identifiers
for tokens. The rules below close the paths the proxy cannot see.

<!-- aicleaner:binaries:start -->
## Files: always convert locally, never send binaries

Never use the Read tool on `.pdf`, `.docx`, `.xlsx`, `.pptx`, `.msg`, `.eml` or any image file.
Convert to text with a local command first, then Read the text output. Never convert a file to
PDF or to an image as an intermediate step, and never render pages to images for "vision".

| Type    | Convert with (run locally via Bash/PowerShell)                                  |
|---------|----------------------------------------------------------------------------------|
| .docx   | `pandoc -t gfm --wrap=none "<file>.docx" -o "<file>.md"`                         |
| .xlsx   | `python -c "import openpyxl,sys;wb=openpyxl.load_workbook(sys.argv[1],data_only=True);[print(f'## {ws.title}') or [print('\t'.join('' if c is None else str(c) for c in r)) for r in ws.iter_rows(values_only=True)] for ws in wb]" "<file>.xlsx" > "<file>.txt"` |
| .pptx   | `python -c "from pptx import Presentation;import sys;p=Presentation(sys.argv[1]);[print(f'## Slide {i+1}') or [print(sh.text_frame.text) for sh in s.shapes if sh.has_text_frame] for i,s in enumerate(p.slides)]" "<file>.pptx" > "<file>.txt"` |
| .pdf    | `pdftotext -layout "<file>.pdf" "<file>.txt"` (poppler); if unavailable, `python -c "import fitz,sys;print('\n'.join(p.get_text() for p in fitz.open(sys.argv[1])))" "<file>.pdf" > "<file>.txt"` |
| images  | Do not read. Ask the user to describe the content or supply it as text.          |

Write conversions next to the source with a `.md` or `.txt` extension. Read only those.
<!-- aicleaner:binaries:end -->
<!-- aicleaner:reading:start -->
## Files: images and PDFs are read on this machine

Use the Read tool on images (screenshots included) and PDFs directly. The proxy reads them on this
machine and sends only their text, filtered like the rest of the request; the picture or document
itself never leaves. What arrives is text only - for an image, approximate, and without its layout,
colours or diagrams - so ask the user when those matter. If a notice says a file was withheld,
convert it locally as below.

Never use the Read tool on `.docx`, `.xlsx`, `.pptx`, `.msg` or `.eml`: convert them to text with a
local command first, then Read the text output.

| Type    | Convert with (run locally via Bash/PowerShell)                                  |
|---------|----------------------------------------------------------------------------------|
| .docx   | `pandoc -t gfm --wrap=none "<file>.docx" -o "<file>.md"`                         |
| .xlsx   | `python -c "import openpyxl,sys;wb=openpyxl.load_workbook(sys.argv[1],data_only=True);[print(f'## {ws.title}') or [print('\t'.join('' if c is None else str(c) for c in r)) for r in ws.iter_rows(values_only=True)] for ws in wb]" "<file>.xlsx" > "<file>.txt"` |
| .pptx   | `python -c "from pptx import Presentation;import sys;p=Presentation(sys.argv[1]);[print(f'## Slide {i+1}') or [print(sh.text_frame.text) for sh in s.shapes if sh.has_text_frame] for i,s in enumerate(p.slides)]" "<file>.pptx" > "<file>.txt"` |
| .pdf    | `pdftotext -layout "<file>.pdf" "<file>.txt"` (only when a PDF was withheld)    |

Write conversions next to the source with a `.md` or `.txt` extension. Read only those.
<!-- aicleaner:reading:end -->

## Identifiers

Use client identifiers exactly as they appear in the context. Do not reformat, abbreviate,
hyphenate, re-case or expand them, and do not invent variants. This matters for round-tripping.

This includes names you make up for things you create - app registrations, certificates, groups,
folders, resource names, tags. Build them from the whole identifier as written (for example
`<identifier>-BuildTeam-Reader`), never from a shortened form made of its first letter and its
number. If a shorter prefix is needed, ask for the real one.
<!-- aicleaner:end -->
