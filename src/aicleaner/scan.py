"""Two folder scans that help maintain the mapping.

scan          candidates that look like identifiers but are not in the mapping,
              found with the proxy's own detectors so the report matches what a
              session would hold back: emails, domains, GUIDs, DevOps slugs,
              resource names that carry a client's name, Azure naming shapes and
              recurring capitalised phrases. Frequency-ranked.
              Also reports project Claude Code settings that would route requests
              around the proxy (routing_overrides).
preflight     the same scan over a repo AND the Claude Code transcripts for it,
              so existing work can be resumed through the proxy without the
              first turn carrying identifiers the mapping has never seen.
scan-tokens   token residue: places where a token (or a mangled variant of it)
              ended up in a real file because rehydration missed it.
"""

from __future__ import annotations

import codecs
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .detect import ALL_KINDS, STATELESS_KINDS, find_candidates, is_public
from .mapping import Mapper

TEXT_SUFFIXES = {
    # code and configuration
    ".md", ".txt", ".json", ".yaml", ".yml", ".tf", ".tfvars", ".hcl", ".ps1", ".psm1", ".psd1",
    ".ps1xml", ".psrc", ".py", ".cs", ".js", ".ts", ".bicep", ".bicepparam", ".xml", ".csv", ".tsv",
    ".ini", ".cfg", ".conf", ".config", ".properties", ".toml", ".sh", ".bat", ".cmd", ".vbs",
    ".env", ".sql", ".kql", ".kusto", ".html", ".htm", ".rst", ".log", ".ipynb", ".nuspec",
    ".csproj", ".props", ".targets", ".sln", ".service", ".http", ".rest",
    # where an estate describes itself: state files hold every real name and id, registry exports
    # and compiled DSC hold machine and account names, captures hold hosts, certificates hold CNs
    ".tfstate", ".backup", ".reg", ".mof", ".har", ".rdp", ".pem", ".crt", ".cer",
    ".jsonl",  # Claude Code transcripts: what a resumed session would send
}
# Files worth reading that have no extension at all.
TEXT_NAMES = {"dockerfile", "makefile", "jenkinsfile", "vagrantfile", "procfile", "hosts",
              ".env", ".gitignore", ".gitattributes", ".npmrc", ".dockerignore", "readme"}
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".terraform", "bin", "obj"}
MAX_BYTES = 8 * 1024 * 1024

def iter_text_files(root: Path, skipped: Counter[str] | None = None,
                    on_file=None) -> Iterator[Path]:
    """Every file worth reading under root. `skipped` is filled with what was left out and why, so
    a scan can say where its blind spots are instead of quietly having them.

    `on_file(path, size)` is called for each file that survives the filters, which is what makes a
    progress bar honest: the denominator is the bytes that will actually be read, not the size of
    the folder. A repository is mostly .git, node_modules and binaries, and counting those would
    show a bar that stops at nine percent and stays there.
    """
    for p in root.rglob("*"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in TEXT_SUFFIXES and p.name.lower() not in TEXT_NAMES:
            if skipped is not None:
                skipped[f"not a text file: {p.suffix.lower() or '(no extension)'}"] += 1
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > MAX_BYTES:
            if skipped is not None:
                skipped[f"over {MAX_BYTES // (1024 * 1024)} MB: {p.suffix.lower()}"] += 1
            continue
        if on_file is not None:
            on_file(p, size)
        yield p


# Files a machine wrote rather than a person: captures, tool output, exports, state dumps, and
# vendor payloads that happen to sit in a client's folder.
GENERATED_SUFFIXES = {".log", ".har", ".etl", ".evtx", ".reg", ".backup", ".tfstate", ".mof"}
GENERATED_MARKERS = ("scripts/output", "scripts/diagnostics", "intune-portal-export",
                     "state-backups", "app-packages", "diaglogs-", "oobe-capture",
                     "/temp/", "/out/")


def machine_written(path: Path, markers: Iterable[str] | None = None, root: Path | None = None) -> bool:
    """Whether this file is output rather than something somebody wrote.

    Tracing a real scan's finds back into the repository gave a consistent answer: the addresses
    were assembly versions in loaded-module listings, the GUIDs were correlation ids in portal
    exports, and the domains were entries in a Defender URL allowlist. None of them identified the
    client, and 962 of 1,442 collected forms were of that sort.

    Deliberately NOT keyed on the extension alone: only two of five sampled addresses came from a
    .log, the rest from .har, .json, .csv and .txt exports. What they share is the folder.

    Matched against the path BELOW the scan root, never the absolute path. The markers describe
    folders inside a repository, and an absolute match makes them mean whatever happens to be above
    it: "temp" matched every path under C:\\Users\\...\\AppData\\Local\\Temp, so scanning a folder
    there marked every file in it as machine-written.
    """
    if path.suffix.lower() in GENERATED_SUFFIXES:
        return True
    below = path
    if root is not None:
        try:
            below = path.relative_to(root)
        except ValueError:
            pass  # not under this root after all; fall back to the whole path
    low = "/" + below.as_posix().lower().lstrip("/")
    return any(m in low for m in (markers if markers is not None else GENERATED_MARKERS))


def vouched_by_mapping(value: str, mapper: Mapper) -> bool:
    """Whether this value visibly belongs to somebody the mapping already knows.

    A diagnostic dump is a description of somebody else's infrastructure that happens to be in this
    client's folder, so a bare GUID or dotted quad in one is a shape with no owner. The same file's
    mention of a client mailbox IS the client.

    Two ways to carry a name, and both count, because both are how a client's name really appears:

      tania.carr@acme.example                 a declared form, whole, inside a longer string
      kvaueprdACMEmanagement.vault.azure.net  a declared form embedded in a resource name

    The first is what the mapping's own pattern matches. The second is not a whole-word match at
    all - the name is welded into a longer run of letters - so it takes the same containment test
    that decides an embedded token, against the same minimum length and the same generic-word list.
    """
    if not value:
        return False
    if mapper.tokenise(value) != value:
        return True
    from .detect import GENERIC_WORDS
    from .mapping import FOUND_ORIGINS

    low = value.lower()
    for form in mapper.forms:
        # What a scan or the proxy found is evidence about one value, not a word to judge others by
        # (D1, H35): a scanned "descript.com" made every "descriptor-..." look vouched for.
        if form.origin in FOUND_ORIGINS:
            continue
        text = form.real
        parts = [text] if text.isalnum() else re.split(r"[^A-Za-z0-9]+", text)
        for part in parts:
            part = part.lower()
            if len(part) >= 4 and part.isalnum() and part not in GENERIC_WORDS and part in low:
                return True
    return False


def _read(p: Path) -> str:
    """The file as text, whatever Windows wrote it as.

    Registry exports, and plenty of PowerShell output, are UTF-16: read as UTF-8 they come back as
    letters separated by NULs, and no identifier in them would ever match.
    """
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF32_LE, "utf-32-le"),
                          (codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                          (codecs.BOM_UTF16_BE, "utf-16-be")):
        if raw.startswith(bom):
            return raw[len(bom):].decode(encoding, errors="replace")
    head = raw[:4096]
    if head.count(b"\x00") > len(head) // 4:  # no BOM, but every other byte is a NUL
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


# A unicode escape sitting immediately before an identifier, in a file that happens to hold JSON.
#
# The bytes on disk are backslash-u-0027 - six characters - so the scanner read
# "jane.doe's@contoso.example" and collected that. The request path never sees this, because a body
# is parsed before the detectors read it and json.loads has already turned the escape into an
# apostrophe. So the escaped spelling is dead weight in a shared file, and worse: where the scan
# met only the escaped form, the real address was never collected and is not protected (D4).
#
# Deliberately narrower than transform.detection_text, which turns every \n \r \t \b \f and \uXXXX
# into a space. That is right for a JSON body, where a genuine backslash arrives doubled - and
# wrong for a file, where "C:\temp" and "C:\new" hold single ones and would become "C: emp" and
# "C: ew", breaking every path the scan should be reading. \uXXXX is the one form that is not
# ambiguous in ordinary file text, so it is the only one undone here.
_FILE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")


def for_detection(text: str) -> str:
    """File text as the detectors should read it: a unicode escape cannot weld itself to a name, and
    %XX is read as what it stands for, so `%2Ffabrikam...` is `/fabrikam...` (H34)."""
    from .transform import percent_decoded
    return percent_decoded(_FILE_ESCAPE.sub(" ", text))


@dataclass
class Candidate:
    """One thing the scan found: what it is, how sure, and which client it belongs to (if known)."""
    value: str
    kind: str
    confidence: str
    client: str
    count: int


def scan_candidates(root: Path, mapper: Mapper, min_count: int = 2,
                    skipped: Counter[str] | None = None, on_file=None,
                    resource_names: bool = False, markers: Iterable[str] | None = None,
                    unvouched: list[str] | None = None) -> list[Candidate]:
    """Identifier-shaped strings under root that the mapping does not cover yet.

    The detectors are the proxy's own (detect.find_candidates), so what this reports is what a
    session would hold back or queue: one set of rules, no second opinion.

    In a file a MACHINE wrote, a find is only kept if it visibly belongs to somebody already in the
    mapping - see `machine_written`. `unvouched`, when given, collects what that held back so the
    scan can say so instead of being quietly selective.
    """
    known = {f.real.lower() for f in mapper.forms} | {f.token.lower() for f in mapper.forms}
    ignore = {str(v).lower() for v in getattr(mapper, "ignore", ())}
    counts: Counter[tuple[str, str, str, str]] = Counter()
    for path in iter_text_files(root, skipped, on_file):
        text = for_detection(_read(path))
        folded = text.lower()
        by_machine = machine_written(path, markers, root)
        for value, kind, confidence, client in find_candidates(text, mapper, resource_names):
            low = value.lower()
            # A GUID is stood in for on every request, never collected (DECISIONS 38).
            if kind in STATELESS_KINDS or low in known or low in ignore or is_public(value):
                continue
            if by_machine and not vouched_by_mapping(value, mapper):
                if unvouched is not None:
                    unvouched.append(value)
                continue
            # A detector reports each value once per file; the count is what the report ranks on.
            counts[(value, kind, confidence, client)] += max(1, folded.count(low))
    rows = [Candidate(value, kind, confidence, client, count)
            for (value, kind, confidence, client), count in counts.items() if count >= min_count]
    rows.sort(key=lambda c: (c.confidence != "high",
                             ALL_KINDS.index(c.kind) if c.kind in ALL_KINDS else 99,
                             -c.count, c.value.lower()))
    return rows


def _loose_pattern(token: str) -> re.Pattern[str]:
    # "Client04 Pty" also matches "client04-pty", "Client 04 Pty", "CLIENT_04PTY"
    parts = [re.escape(ch) for ch in token if not ch.isspace()]
    joined = r"[\s_\-\.]*".join(parts)
    return re.compile(joined, re.IGNORECASE)


def scan_tokens(root: Path, mapper: Mapper, on_file=None) -> list[tuple[Path, int, str, str]]:
    """Return (file, line_no, token, line_text) for every token residue found."""
    patterns = [(t, _loose_pattern(t)) for t in sorted({f.token for f in mapper.forms}, key=len, reverse=True)]
    hits: list[tuple[Path, int, str, str]] = []
    for path in iter_text_files(root, None, on_file):
        for no, line in enumerate(_read(path).splitlines(), 1):
            for token, pat in patterns:
                if pat.search(line):
                    hits.append((path, no, token, line.strip()[:160]))
                    break
    return hits


def format_candidates(rows: Iterable[Candidate], top: int = 40) -> Iterable[str]:
    """Report lines grouped by kind, high confidence first."""
    shown = 0
    last_kind = ""
    for row in rows:
        if shown >= top:
            yield f"  ... more (raise --top)"
            return
        if row.kind != last_kind:
            last_kind = row.kind
            yield f"[{row.kind}]"
        owner = f"  -> {row.client}" if row.client else ""
        flag = "" if row.confidence == "high" else "  (review only)"
        yield f"  {row.count:5d}  {row.value}{owner}{flag}"
        shown += 1


def project_folder_name(path: Path) -> str:
    """How Claude Code names the folder it keeps a project's transcripts in: the full path with
    ':' and the separators replaced by '-' (C:\\Repos\\x becomes C--Repos-x)."""
    return str(Path(path).resolve()).replace(":", "-").replace("\\", "-").replace("/", "-")


def transcript_folders(repo: Path, projects_root: Path | None = None) -> list[Path]:
    """Every Claude Code transcript folder for a repo and the folders below it.

    A repo of repos (each subfolder opened as its own project) has one folder per subfolder, so
    resuming any of them is covered by one preflight.
    """
    root = Path(projects_root) if projects_root else Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    prefix = project_folder_name(repo)
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p.name == prefix or p.name.startswith(prefix + "-")))


# Project settings Claude Code reads from a repo; any of these keys sends requests somewhere other
# than the proxy configured in the user's settings (review finding 7: bypass detection).
PROJECT_SETTINGS = ("settings.json", "settings.local.json")
PROVIDER_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_LOOPBACK_URL = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", re.I)


def routing_overrides(root: Path) -> list[tuple[Path, str, str]]:
    """(file, key, value) for every project setting that routes around a loopback proxy."""
    found: list[tuple[Path, str, str]] = []
    for folder in [root / ".claude", *(p for p in root.rglob(".claude") if p.is_dir())]:
        if any(part in SKIP_DIRS for part in folder.relative_to(root).parts):
            continue
        for name in PROJECT_SETTINGS:
            path = folder / name
            if not path.is_file() or any(f[0] == path for f in found):
                continue
            try:
                env = (json.loads(path.read_text(encoding="utf-8-sig")) or {}).get("env") or {}
            except (OSError, ValueError, AttributeError):
                found.append((path, "(unreadable)", "not valid JSON; check it by hand"))
                continue
            base = str(env.get("ANTHROPIC_BASE_URL", "") or "")
            if base and not _LOOPBACK_URL.match(base):
                found.append((path, "ANTHROPIC_BASE_URL", base))
            for key in PROVIDER_SWITCHES:
                if str(env.get(key, "") or "").strip().lower() not in ("", "0", "false"):
                    found.append((path, key, str(env[key])))
    return found
