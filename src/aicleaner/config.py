"""aicleaner-config.json loading with sane defaults."""

from __future__ import annotations

import json
import re
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fileio

log = logging.getLogger("aicleaner")


# Double-click in a Markdown preview (feature 92).
INLINE_SERVICES = ("auto", "off", "claude", "copilot", "copilot_filtered", "codex")   # completion.SERVICES
PREVIEW_DBLCLICK = ("ask", "source", "off")
# What prompt autocorrect may also leave alone (feature 94); code and paths always are.
AUTOCORRECT_SKIPS = ("clients", "used", "capitalised")
# What the Session page does with a picture pasted, dropped, uploaded or read from the file tree (D216): read its
# text into the prompt, attach it, or both. The proxy still decides what leaves (attachments.*, feature 114).
PICTURE_MODES = ("text", "image", "image_text")
REPLACEMENT_RE = re.compile(r"[A-Za-z][A-Za-z'-]{0,39} = [^\r\n=]{1,60}")
# Your own words for a Settings card (feature 153): ui.setting_keywords = {"<card key>": ["telemetry"]}.
# A card key is checked by its shape, not against a list: the cards are drawn by the page, so a list
# here would be a second copy that drifts, and a key no card answers to (a card later renamed) is
# harmless - it never matches - and is kept so it is not lost while a page is out of date.
SETTING_CARD_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")
SETTING_WORD_MAX = 40
SETTING_WORDS_PER_CARD = 20
SETTING_KEYWORD_CARDS = 100     # a bound on the whole map, so a stray POST cannot grow the config file


def setting_keywords(value: Any, strict: bool = True) -> dict[str, list[str]]:
    """Clean ui.setting_keywords: words trimmed (inner spaces collapsed), 1-40 characters on one line,
    duplicates (any case) dropped, up to 20 per card; a card with none left is dropped. Strict (from
    the page) refuses what does not fit; not strict (a config file edited by hand) drops it instead."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("ui.setting_keywords is a map of card key to a list of words")
        return {}
    cleaned: dict[str, list[str]] = {}
    for key, words in value.items():
        key = str(key).strip()
        if not SETTING_CARD_RE.fullmatch(key):
            if strict:
                raise ValueError(f"{key!r} is not a settings card key (lower-case letters, digits and -)")
            continue
        if isinstance(words, str):
            words = [words]
        if not isinstance(words, list):
            if strict:
                raise ValueError(f"the words for {key} must be a list")
            continue
        kept: list[str] = []
        for word in words:
            word = " ".join(str(word).split())
            if not word or word.lower() in (w.lower() for w in kept):
                continue
            if len(word) > SETTING_WORD_MAX or not word.isprintable():
                if strict:
                    raise ValueError(f"a word for a setting is up to {SETTING_WORD_MAX} characters on one line: {word[:50]}")
                continue
            kept.append(word)
        if len(kept) > SETTING_WORDS_PER_CARD:
            if strict:
                raise ValueError(f"up to {SETTING_WORDS_PER_CARD} words for one setting ({key} has {len(kept)})")
            kept = kept[:SETTING_WORDS_PER_CARD]
        if kept:
            cleaned[key] = kept
    if len(cleaned) > SETTING_KEYWORD_CARDS:
        if strict:
            raise ValueError(f"words for up to {SETTING_KEYWORD_CARDS} settings")
        cleaned = dict(list(cleaned.items())[:SETTING_KEYWORD_CARDS])
    return cleaned


def _flag(section: dict[str, Any], key: str, default: bool, name: str) -> bool:
    """A true/false setting, from a JSON boolean only (H23). `bool("false")` is True, so a string
    written by hand used to switch settings ON - fail-open and the tunnel among them. Anything that
    is not a boolean keeps the default, and the key is named in the log."""
    value = section.get(key, default) if isinstance(section, dict) else default
    if isinstance(value, bool):
        return value
    log.warning("%s in aicleaner-config.json is %r, not true or false; using %s", name, value,
                str(default).lower())
    return default

# Unknown identifiers of these kinds are held back as placeholders (high-confidence detector kinds);
# "name" is low confidence and stays queue-only unless chosen.
DEFAULT_PLACEHOLDER_KINDS = ("email", "tenant", "sharepoint", "domain", "ip", "devops-org",
                             "devops-project", "client-resource")

# Kinds that are never held back and never auto-added, whatever a saved setting says (feature 63).
# The checkbox is gone from the page (SELECTABLE_KINDS), and a setting outlives its control:
# without this, a config that already listed "name" would keep the behaviour with no way left to
# see or change it. Applied to placeholder_kinds on load and on save.
NEVER_HELD_BACK = frozenset({"name"})
# And kinds that are always replaced by a stand-in, so there is nothing to choose (DECISIONS 38;
# detect.STATELESS_KINDS). Dropped from a saved placeholder_kinds for the same reason.
ALWAYS_STOOD_IN = frozenset({"guid"})


def _default_presets() -> list[dict]:
    from .presets import default_presets
    return default_presets()


def _held_back(kinds: list[str]) -> list[str]:
    return [k for k in kinds if k not in NEVER_HELD_BACK and k not in ALWAYS_STOOD_IN]

# There is no "auto-add these kinds" setting any more (DECISIONS 38): it filed a value under
# whichever client the conversation was about, and client files are shared. A saved
# detect.auto_add_kinds is read and ignored. What is still added by itself proves its owner in the
# value: a client resource, or a name carrying a client's token.

# What happens to something new, per kind (walkthrough topic 9, DECISIONS 46): a kind in
# quiet_kinds is Auto-tokenise, one in placeholder_kinds is Tokenise & Review, one in neither is
# Ignore. External IP addresses are tokenised automatically by default: never owned, rarely worth a
# decision. The owner-add defaults are the kinds whose owner the value can prove.
DEFAULT_QUIET_KINDS = ("ip",)
DEFAULT_OWNER_ADD_KINDS = ("email", "tenant", "sharepoint", "devops-org", "devops-project")

DEFAULT_UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
}

# The folder holding the aicleaner package, its config and the contact-*.json files.
APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "aicleaner-config.json"
# The pre-2026-09 name; refused with a rename hint instead of read, so there is one source of truth.
LEGACY_CONFIG_NAME = "aicleaner.json"


def session_profiles(session: dict, legacy_projects: list[str]) -> list[dict]:
    """The session profiles, or one built from a plain projects list.

    An older config has `projects` and no `profiles`. Rather than ask anyone to migrate, that
    reads as a single profile named Projects holding those folders, so upgrading changes nothing
    until somebody wants more than one.
    """
    declared = session.get("profiles")
    if not isinstance(declared, list) or not declared:
        if not legacy_projects:
            return []
        return [{"name": "Projects", "client": "", "projects": list(legacy_projects), "model": "", "brief": ""}]
    profiles: list[dict] = []
    seen: set[str] = set()
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ValueError("every session profile needs a name")
        if name.lower() in seen:
            raise ValueError(f"two session profiles are called {name}")
        seen.add(name.lower())
        profiles.append({
            "name": name,
            "client": str(entry.get("client", "")).strip(),
            "projects": [os.path.expandvars(str(p)) for p in (entry.get("projects") or [])],
            "model": str(entry.get("model", "")),
            "brief": str(entry.get("brief", "")),
        })
    return profiles


@dataclass
class Config:
    listen: str = "127.0.0.1"
    port: int = 8787
    mapping: Path = Path("client-map.json")
    clients_dir: Path = Path("clients")
    log_dir: Path = Path("logs")
    contact_dir: Path = APP_ROOT  # where contact-*.json live; tests point it at a temp folder
    # Per-machine state (last-good copies of client files); never shared. Tests use a temp folder.
    state_dir: Path = Path.home() / ".aicleaner"
    upstreams: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_UPSTREAMS))
    allow_non_json_paths: list[str] = field(default_factory=list)
    keep_tokenised_tools: list[str] = field(default_factory=list)
    # Absolute folders the pages may reach: a scan, a session's project folders, a client's share.
    # Empty means none, which is the right way round - the previous behaviour was everything this
    # account can read, and nobody chose it. Set on this machine only (see ui.REMOTE_SETTINGS):
    # reaching an allowed folder from the office is the feature, deciding which are allowed is not.
    folder_roots: list[str] = field(default_factory=list)
    # Path fragments marking a folder as machine-written output, where a scan only keeps a find
    # that visibly belongs to a client. Empty means the built-in list (scan.GENERATED_MARKERS):
    # a heuristic that will drift, so it is something somebody can correct rather than a constant.
    scan_generated_markers: list[str] = field(default_factory=list)
    # dialect -> {"gateway_token_file": ..., "upstream_key_file": ..., "upstream_key_env": ...}
    credentials: dict[str, dict[str, str]] = field(default_factory=dict)
    # learning-mode detection of unmapped identifiers + how to tell the user
    detect_enabled: bool = True
    notify_footer: bool = True
    notify_toast: bool = True
    ui_url: str = ""  # default: http://<listen>:<port>/#/review
    # Kinds never proposed to Review, and so never held back (feature 31): set by accepting the
    # offer Review makes for a kind ignored far more often than kept, undone on Settings.
    quiet_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_QUIET_KINDS))
    # Tokenise & Review kinds that are added straight to a client when the value proves the owner
    # (DECISIONS 46). Client resources keep their own switch, auto_map_client_resources.
    owner_add_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_OWNER_ADD_KINDS))
    auto_map_client_resources: bool = True  # a resource name carrying a client's form is mapped to that client
    # Ignore kinds that are still added to a client when the value proves the owner (E3, Brad
    # 2026-10-19: "Ignore, but auto-add"). Its own list, empty until chosen, so a kind already on Ignore
    # never starts auto-adding because owner_add_kinds' defaults happened to name it.
    ignore_add_kinds: list[str] = field(default_factory=list)
    # A word one letter from a mapped identifier is corrected back to it, in text a converter
    # produced and nowhere else: an engine misreads, a person typing does not, and over a whole
    # request nearly every match would be an ordinary long word replaced by a client's token.
    near_miss: bool = True
    # Driving a Claude Code session from the UI (see session.py). On for an install by default since
    # item J (2026-10-20: "Project Sessions on by default") - the loader below; a program building a
    # Config in code (the tests) still starts with it off.
    session_enabled: bool = False
    # The casual-chats folder (item J): chat assets that belong to no project; Casual chats lives there.
    session_casual_folder: str = ""
    # The file editor's PowerShell commands, compiled from this machine (feature 80).
    editor_compile_powershell: bool = True
    # Double-click in a Markdown preview (feature 92): ask the first time, jump to the source with
    # the word selected, or leave double-click to select text.
    editor_preview_dblclick: str = "ask"
    # Feature 143: code suggested as you pause in the file editor, by a warm Claude Code worker through aicleaner.
    editor_inline_suggest: bool = True
    editor_inline_model: str = "haiku"
    editor_minimap: bool = False
    # Which service suggests code as you pause (spec 2026-10-23-editor-suggestion-services.md): auto, off, claude,
    # copilot (unfiltered), copilot_filtered (unsupported), codex.
    editor_inline_service: str = "auto"         # the file editor's minimap (Brad, 2026-10-23: off by default)
    # Autocorrect in the Session page's prompt (feature 94): off by default; code and paths are
    # always left alone, `prompt_autocorrect_skip` adds "clients", "used", "capitalised"; the user's
    # own words carry their casing (powershell -> PowerShell).
    prompt_autocorrect: bool = False    # off until a person turns it on (the user, 2026-10-18; was on - D152)
    prompt_autocorrect_skip: list[str] = field(default_factory=lambda: list(AUTOCORRECT_SKIPS))
    prompt_autocorrect_words: list[str] = field(default_factory=lambda: ["PowerShell", "SharePoint", "aicleaner"])
    # The user's own replacements, "typed = replacement" (keyvault = Key Vault): a habit, not a typo,
    # so the dictionary would never offer it (the user, 2026-10-08).
    prompt_autocorrect_replace: list[str] = field(default_factory=list)
    # Diagnostics (feature 91): record request timings, off again 24 hours after it was turned on.
    debug_timings: bool = False
    debug_timings_since: int = 0
    # Your own words for Settings cards (feature 153), found by "Find a setting"; see setting_keywords().
    ui_setting_keywords: dict[str, list[str]] = field(default_factory=dict)
    session_claude: str = "claude"
    session_projects: list[str] = field(default_factory=list)
    session_permission_mode: str = "manual"
    # One entry per way of working: {name, client, projects[], model, brief}. A bare
    # session.projects list still reads as a single profile, so nobody has to migrate to upgrade.
    session_profiles: list[dict] = field(default_factory=list)
    session_max_running: int = 4
    # Start Work and Start Personal (feature 152): [{id, name, profiles}], at most max_running - 1 profiles each.
    session_presets: list[dict] = field(default_factory=lambda: _default_presets())
    session_auto_titles: bool = True     # a title for a conversation when its session stops (item E)
    # A new session's job ends its whole process tree when its broker goes (spec 2026-10-24-session-lifecycle-
    # instrumentation.md). Off: the job only counts and measures.
    session_job_cleanup: bool = False
    # The Session page draws Claude's thinking expanded (on) or folded to its "thinking" line (off) (2026-10-27).
    session_show_thinking: bool = True
    # A message sent while Claude works waits for the task's end-of-turn summary, unless it asks for something now.
    session_turn_summary: bool = False
    # Caps on each new session as a whole (D208): processes alive at once, and memory committed (0 = no cap).
    session_max_processes: int = 400
    session_max_memory_gb: int = 16
    # The Session page offers to stop a session idle this many hours (0 = never). An offer, never a stop.
    session_idle_offer_hours: int = 8
    session_thinking_display: str = "summarized"   # "" leaves thinking to the CLI (empty blocks)
    # D216: a picture on the Session page is read to text (text), attached (image), or both (image_text).
    session_picture_mode: str = "text"
    # Restored names shown in green (see transform.strip_marks). Always on the session page, which
    # draws them; for Claude Code in a terminal or VS Code, only once someone has seen it render.
    highlight_session: bool = True
    # On by default now it has been seen to render. It was off because a terminal that does not
    # understand the codes shows "[32;22m" instead of colour, which would look like the filter had
    # corrupted the reply - but seeing which names were put back is how you tell the filter worked,
    # and that is worth having on from the first session rather than after somebody finds the switch.
    highlight_terminal: bool = True
    origin_note: bool = True                # say where each Claude Code prompt was typed (origin.py)
    # Windows: Claude Code's Bash tool halves doubled backslashes, so a hook stops Bash calls that
    # hold them and points to PowerShell or Write/Edit (feature 98, see bashguard.py).
    claude_bash_guard: bool = True
    # A PostToolUse hook hands the Session page's waiting messages to Claude between tool calls
    # instead of at the end of the turn (feature 119, see deliver.py).
    claude_deliver_between_tools: bool = True
    # A PreToolUse hook stops a tool call in which two different spellings would be written as the
    # same client name - the rename collapse that deleted a file (bug report 2026-10-14; deliver.py).
    claude_collapse_guard: bool = True
    # The Claude Desktop app's Code tab through aicleaner (feature 117, codetab*.py): a forward proxy
    # on this port, and Claude Code's settings pointed at it. Off until chosen (installer or Settings).
    codetab_enabled: bool = False
    update_auto: bool = False            # install a newer release on its own (2026-10-16), off by default
    feedback_relay: str = "https://aicleaner-feedback.biosconsulting.com.au"   # where feedback goes (2026-10-16)
    codetab_port: int = 8788
    # GitHub Copilot in VS Code through the same forward proxy (2026-10-18, copilot.py): off until chosen.
    copilot_enabled: bool = False
    # Route B (spec 2026-10-21-desktop-chat-route-b.md, claudeai*.py): Claude Desktop's chat, signed in to
    # claude.ai, through the same forward proxy by the Windows proxy setting. Off until chosen.
    claudeai_enabled: bool = False
    # The paths and kinds of claude.ai's protobuf messages written to the log (never their content), to grow
    # the field map from a synthetic session. Off.
    claudeai_structure_log: bool = False
    # The value of a reply's sender field that means Claude wrote it (claudeai.py REPLY_SENDER): the filtered mark is
    # added to those replies only. 1, read from a synthetic chat 2026-10-23 (every finished reply carried it); 0 = no mark.
    claudeai_mark_role: int = 1
    # EMERGENCY ONLY. When the canary finds a real name after tokenising, send the request anyway
    # (logged, and flagged in the evidence log) instead of refusing it. It exists so a false
    # positive cannot lock you out of the assistant you would use to fix it; off is the only safe
    # setting, and it is never turned on by anything but a person on the Settings page.
    canary_fail_open: bool = False
    # TEMPORARY, for debugging the filter itself. On, model traffic is forwarded untouched: nothing
    # is replaced, detected, held back, redacted or checked, and replies come back as the provider
    # sent them. It exists so the filter can be switched off without an uninstall while its own bugs
    # are worked through, and is to be removed before the tool is relied on.
    proxy_bypass: bool = False
    # Reaching the UI through a Cloudflare tunnel, with Access in front (see access.py).
    remote_enabled: bool = False
    remote_hostname: str = ""
    remote_team_domain: str = ""
    remote_audience: str = ""
    remote_allowed_emails: list[str] = field(default_factory=list)
    # The encrypted tunnel (feature 21, DECISIONS 57): through the tunnel, every API call sealed with
    # a key Cloudflare never sees. Where a device keeps the key: "tab" (sessionStorage) or "device"
    # (localStorage). Both settable remotely (REMOTE_SETTINGS) - turning encryption on needs a code made
    # on this machine; the key's storage reaches since H2 (Brad, 2026-10-19).
    remote_encrypt: bool = False
    remote_key_storage: str = "tab"
    placeholder_kinds: list[str] = field(default_factory=lambda: list(DEFAULT_PLACEHOLDER_KINDS))
    allow_images: bool = False  # images are withheld unless this is on (then sent unfiltered)
    # Feature 114: pictures sent with client names blacked out (and their text), instead of text only. Local only: it
    # lets pictures leave the machine. allow_images (as is) wins when both are on.
    redact_images: bool = False
    pdftotext: str = ""         # path to Poppler's pdftotext; default: found on PATH
    # Reading an image is a different question from sending one: OCR decides whether its text can
    # be filtered, allow_images decides whether the picture itself goes. OCR runs in the request
    # path, so it is bounded.
    ocr: bool = True
    ocr_max_pages: int = 10          # of a scanned PDF; a 200-page scan is not worth the wait
    ocr_timeout_s: int = 60          # one stuck engine must not hold a request open
    ocr_max_bytes: int = 20_000_000  # a photo is not a filesystem
    secrets_mode: str = "redact"  # redact | block | off
    secrets_user_file: str = ""   # DPAPI list of user-entered secrets; default under ~/.aicleaner/secrets
    detail_log: bool = True            # logs/detail/*.jsonl.gz: the substitution log (DECISIONS 38)
    # False: a value is logged the first time a session sends it and when what happens to it changes.
    # True: every value on every request, for diagnosis.
    detail_every_request: bool = False
    detail_retention_days: int = 90    # detail files older than this are deleted at proxy start
    path: Path | None = None  # the aicleaner-config.json this was loaded from, for runtime edits
    client_max_mb: int = 64
    upstream_timeout_s: int = 600

    EDITABLE = {
        "detect.footer": ("notify_footer", bool), "detect.toast": ("notify_toast", bool),
        "detect.enabled": ("detect_enabled", bool),
        "detect.quiet_kinds": ("quiet_kinds", list),
        "detect.owner_add_kinds": ("owner_add_kinds", list),
        "detect.ignore_add_kinds": ("ignore_add_kinds", list),
        "detect.auto_map_client_resources": ("auto_map_client_resources", bool),
        "detect.near_miss": ("near_miss", bool),
        "session.enabled": ("session_enabled", bool),
        "session.casual_folder": ("session_casual_folder", str),
        "remote.enabled": ("remote_enabled", bool),
        "remote.encrypt": ("remote_encrypt", bool),
        "remote.key_storage": ("remote_key_storage", str),
        "session.projects": ("session_projects", list),
        "session.permission_mode": ("session_permission_mode", str),
        "session.max_running": ("session_max_running", int),
        "session.presets": ("session_presets", "presets"),
        "session.auto_titles": ("session_auto_titles", bool),
        "session.job_cleanup": ("session_job_cleanup", bool),
        "session.show_thinking": ("session_show_thinking", bool),
        "session.turn_summary": ("session_turn_summary", bool),
        "session.max_processes": ("session_max_processes", int),
        "session.max_memory_gb": ("session_max_memory_gb", int),
        "session.idle_offer_hours": ("session_idle_offer_hours", int),
        "session.picture_mode": ("session_picture_mode", str),
        "detect.placeholder_kinds": ("placeholder_kinds", list),
        "attachments.allow_images": ("allow_images", bool),
        "attachments.redact_images": ("redact_images", bool),
        "attachments.ocr": ("ocr", bool),
        "attachments.ocr_max_pages": ("ocr_max_pages", int),
        "attachments.ocr_timeout_s": ("ocr_timeout_s", int),
        "logs.detail": ("detail_log", bool), "logs.detail_retention_days": ("detail_retention_days", int),
        "logs.detail_every_request": ("detail_every_request", bool),
        "secrets.mode": ("secrets_mode", str),
        "keep_tokenised_tools": ("keep_tokenised_tools", list),
        "folders.roots": ("folder_roots", list),
        "scan.generated_markers": ("scan_generated_markers", list),
        "highlight.terminal": ("highlight_terminal", bool),
        "claude.bash_guard": ("claude_bash_guard", bool),
        "claude.deliver_between_tools": ("claude_deliver_between_tools", bool),
        "claude.collapse_guard": ("claude_collapse_guard", bool),
        "codetab.enabled": ("codetab_enabled", bool),
        "update.auto": ("update_auto", bool),
        "codetab.port": ("codetab_port", int),
        "copilot.enabled": ("copilot_enabled", bool),
        "claudeai.enabled": ("claudeai_enabled", bool),
        "claudeai.structure_log": ("claudeai_structure_log", bool),
        "claudeai.mark_role": ("claudeai_mark_role", int),
        "canary.fail_open": ("canary_fail_open", bool),
        "proxy.bypass": ("proxy_bypass", bool),
        "debug.timings": ("debug_timings", bool),
        "editor.preview_dblclick": ("editor_preview_dblclick", str),
        "editor.inline_suggest": ("editor_inline_suggest", bool),
        "editor.minimap": ("editor_minimap", bool),
        "editor.inline_service": ("editor_inline_service", str),
        "prompt.autocorrect": ("prompt_autocorrect", bool),
        "prompt.autocorrect_skip": ("prompt_autocorrect_skip", list),
        "prompt.autocorrect_words": ("prompt_autocorrect_words", list),
        "prompt.autocorrect_replace": ("prompt_autocorrect_replace", list),
        "debug.timings_since": ("debug_timings_since", int),
        "ui.setting_keywords": ("ui_setting_keywords", dict),
    }

    def settings(self) -> dict[str, Any]:
        return {k: getattr(self, attr) for k, (attr, _t) in self.EDITABLE.items()}

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Apply a whitelist of settings in memory and persist them to aicleaner-config.json."""
        data = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path and self.path.exists() else {}
        for key, value in changes.items():
            if key not in self.EDITABLE:
                raise ValueError(f"{key} is not an editable setting")
            attr, typ = self.EDITABLE[key]
            if typ is bool:
                # bool("false") is True: a string would have switched fail-open ON (H23).
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be true or false")
            elif typ is int:
                value = int(value)
            elif typ is list:
                value = [str(v).strip() for v in (value or []) if str(v).strip()]
                if key == "detect.placeholder_kinds":
                    value = _held_back(value)
            elif typ is dict:
                value = setting_keywords(value)
            elif typ == "presets":
                # Against the cap as it will be after this save, when the same save changes it.
                from .presets import normalise_presets
                cap = int(changes.get("session.max_running", self.session_max_running) or 0)
                value = normalise_presets(value, cap)
            else:
                value = str(value)
            if key == "secrets.mode" and value not in ("redact", "block", "off"):
                raise ValueError("secrets.mode must be redact, block or off")
            if key == "remote.key_storage" and value not in ("tab", "device"):
                raise ValueError("remote.key_storage must be tab or device")
            if key == "prompt.autocorrect_skip":
                unknown = [v for v in value if v not in AUTOCORRECT_SKIPS]
                if unknown:
                    raise ValueError(f"prompt.autocorrect_skip takes {', '.join(AUTOCORRECT_SKIPS)}, not {', '.join(unknown)}")
            if key == "prompt.autocorrect_words":
                odd = [v for v in value if not re.fullmatch(r"[A-Za-z][A-Za-z'-]{0,39}", v)]
                if odd:
                    raise ValueError(f"an autocorrect word is letters only (up to 40): {', '.join(odd[:3])}")
            if key == "prompt.autocorrect_replace":
                value = [re.sub(r"\s*=\s*", " = ", v, count=1) for v in value]
                odd = [v for v in value if not REPLACEMENT_RE.fullmatch(v)]
                if odd:
                    raise ValueError("a replacement is 'typed = replacement': one word typed, then up to 60 "
                                     f"characters on one line: {', '.join(odd[:3])}")
            if key == "editor.inline_service" and value not in INLINE_SERVICES:
                raise ValueError(f"editor.inline_service must be one of: {', '.join(INLINE_SERVICES)}")
            if key == "session.picture_mode" and value not in PICTURE_MODES:
                raise ValueError(f"session.picture_mode must be one of: {', '.join(PICTURE_MODES)}")
            if key == "editor.preview_dblclick" and value not in PREVIEW_DBLCLICK:
                raise ValueError(f"editor.preview_dblclick must be one of: {', '.join(PREVIEW_DBLCLICK)}")
            if key == "session.permission_mode":
                # Checked here as well as on the API route, because a config file edited by hand
                # never goes through the route - and an unknown mode is not rejected until the
                # session tries to launch with it, which reads as "sessions are broken".
                from .session import PERMISSION_MODES
                if value not in PERMISSION_MODES:
                    raise ValueError(
                        f"session.permission_mode must be one of: {', '.join(PERMISSION_MODES)}")
            if key == "codetab.port" and not 1024 <= value <= 65535:
                raise ValueError("codetab.port must be between 1024 and 65535")
            if key == "logs.detail_retention_days" and value < 0:
                raise ValueError("retention must be 0 (keep forever) or more")
            if key == "session.max_running" and not 0 <= value <= 64:
                raise ValueError("sessions running at once must be 0 (no cap) to 64")
            if key == "folders.roots":
                # Absolute only: a relative path resolves against whatever the proxy's working
                # directory happens to be, so "repos" would mean different folders depending on how
                # it was started - and this list decides what may be reached.
                relative = [v for v in value if not Path(v).expanduser().is_absolute()]
                if relative:
                    raise ValueError("a folder must be a full path (for example C:\\Users\\you\\OneDrive), "
                                     f"not {', '.join(relative)}")
                # System folders are refused outright (decided 2026-09-27). This list can be set
                # remotely now - the case being a client site, realising a folder is missing -
                # and that is exactly the situation where a hurried entry or an autocomplete
                # could put Program Files behind the picker. Client work is never in there.
                from .folders import blocked_reason
                for entry in value:
                    why = blocked_reason(entry)
                    if why:
                        raise ValueError(why)
            setattr(self, attr, value)
            node = data
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        if self.path:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            fileio.replace(tmp, self.path)
        return self.settings()

    def set_secrets_mode(self, mode: str) -> None:
        """Change secrets.mode in memory and in aicleaner-config.json (if loaded from a file)."""
        mode = mode.strip().lower()
        if mode not in ("redact", "block", "off"):
            raise ValueError("mode must be redact, block or off")
        self.secrets_mode = mode
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            data.setdefault("secrets", {})["mode"] = mode
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            fileio.replace(tmp, self.path)

    def _load_picture_mode(self, ses: dict[str, Any]) -> None:
        """session.picture_mode as saved; absent (D216's migration), what the Pictures setting implies - a setup that
        already sends pictures (redacted or as they are) starts on attaching them - written back at once, so a later
        change to Pictures does not flip it. A value written by hand that is not one of the modes is read as text."""
        if "picture_mode" in ses:
            value = str(ses.get("picture_mode") or "")
            if value not in PICTURE_MODES:
                log.warning("session.picture_mode in aicleaner-config.json is %r, not one of %s; using text",
                            value, ", ".join(PICTURE_MODES))
                value = "text"
            self.session_picture_mode = value
            return
        self.session_picture_mode = "image" if (self.redact_images or self.allow_images) else "text"
        if self.path is None:
            return
        try:
            self.update_settings({"session.picture_mode": self.session_picture_mode})
        except (OSError, ValueError) as exc:
            log.warning("session.picture_mode not written to %s: %s", self.path, exc)

    @property
    def user_secrets_path(self) -> Path:
        if self.secrets_user_file:
            return Path(self.secrets_user_file).expanduser()
        return Path.home() / ".aicleaner" / "secrets" / "redact-list.json.dpapi"

    @property
    def review_url(self) -> str:
        return self.ui_url or f"http://{self.listen}:{self.port}/#/review"

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None:
            return cls()
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        base = p.parent
        cfg = cls()
        cfg.path = p
        cfg.listen = str(data.get("listen", cfg.listen))
        cfg.port = int(data.get("port", cfg.port))
        cfg.mapping = base / data.get("mapping", "client-map.json")
        cfg.clients_dir = base / data.get("clients_dir", "clients")
        cfg.log_dir = base / data.get("log_dir", "logs")
        cfg.upstreams = dict(DEFAULT_UPSTREAMS)
        cfg.upstreams.update({k: str(v).rstrip("/") for k, v in data.get("upstreams", {}).items()})
        cfg.allow_non_json_paths = [str(x) for x in data.get("allow_non_json_paths", [])]
        cfg.keep_tokenised_tools = [str(x) for x in data.get("keep_tokenised_tools", [])]
        cfg.folder_roots = [str(x) for x in (data.get("folders", {}) or {}).get("roots", [])]
        cfg.scan_generated_markers = [str(x) for x in (data.get("scan", {}) or {}).get("generated_markers", [])]
        cfg.credentials = {
            str(k): {str(kk): os.path.expandvars(str(vv)) for kk, vv in (v or {}).items()}
            for k, v in data.get("credentials", {}).items()
        }
        det = data.get("detect", {}) or {}
        cfg.detect_enabled = _flag(det, "enabled", True, "detect.enabled")
        cfg.notify_footer = _flag(det, "footer", True, "detect.footer")
        cfg.notify_toast = _flag(det, "toast", True, "detect.toast")
        cfg.ui_url = str(det.get("ui_url", ""))
        cfg.quiet_kinds = [str(k) for k in (det["quiet_kinds"] or [] if "quiet_kinds" in det else DEFAULT_QUIET_KINDS)]
        cfg.owner_add_kinds = [str(k) for k in (det["owner_add_kinds"] or [] if "owner_add_kinds" in det
                                                 else DEFAULT_OWNER_ADD_KINDS)]
        cfg.auto_map_client_resources = _flag(det, "auto_map_client_resources", True, "detect.auto_map_client_resources")
        cfg.ignore_add_kinds = [str(k) for k in (det.get("ignore_add_kinds") or [])]
        cfg.near_miss = _flag(det, "near_miss", True, "detect.near_miss")
        cfg.editor_compile_powershell = _flag(data.get("editor") or {}, "compile_powershell", True,
                                              "editor.compile_powershell")
        dbl = str((data.get("editor") or {}).get("preview_dblclick", "ask"))
        cfg.editor_preview_dblclick = dbl if dbl in PREVIEW_DBLCLICK else "ask"
        cfg.editor_inline_suggest = _flag(data.get("editor") or {}, "inline_suggest", True, "editor.inline_suggest")
        cfg.editor_inline_model = str((data.get("editor") or {}).get("inline_model") or "haiku")
        cfg.editor_minimap = _flag(data.get("editor") or {}, "minimap", False, "editor.minimap")
        service = str((data.get("editor") or {}).get("inline_service") or "auto")
        cfg.editor_inline_service = service if service in INLINE_SERVICES else "auto"
        prompt_cfg = data.get("prompt") or {}
        cfg.prompt_autocorrect = _flag(prompt_cfg, "autocorrect", False, "prompt.autocorrect")
        # Every skip on until a person changes them (the user, 2026-10-16: out of the box it was too
        # noisy); a saved list - even an empty one - is kept as chosen.
        if "autocorrect_skip" in prompt_cfg:
            cfg.prompt_autocorrect_skip = [str(v) for v in (prompt_cfg.get("autocorrect_skip") or [])
                                           if str(v) in AUTOCORRECT_SKIPS]
        if "autocorrect_words" in prompt_cfg:
            cfg.prompt_autocorrect_words = [str(v).strip() for v in (prompt_cfg.get("autocorrect_words") or [])
                                            if str(v).strip()]
        cfg.prompt_autocorrect_replace = [str(v).strip() for v in (prompt_cfg.get("autocorrect_replace") or [])
                                          if REPLACEMENT_RE.fullmatch(str(v).strip())]
        debug = data.get("debug") or {}
        cfg.debug_timings = _flag(debug, "timings", False, "debug.timings")
        try:
            cfg.debug_timings_since = int(debug.get("timings_since", 0) or 0)
        except (TypeError, ValueError):
            cfg.debug_timings_since = 0
        ui_cfg = data.get("ui") if isinstance(data.get("ui"), dict) else {}
        cfg.ui_setting_keywords = setting_keywords(ui_cfg.get("setting_keywords"), strict=False)
        ses = data.get("session") or {}
        cfg.session_enabled = _flag(ses, "enabled", True, "session.enabled")
        cfg.session_casual_folder = os.path.expandvars(str(ses.get("casual_folder", "") or "")).strip()
        cfg.session_claude = os.path.expandvars(str(ses.get("claude", "claude")))
        cfg.session_projects = [os.path.expandvars(str(p)) for p in (ses.get("projects") or [])]
        cfg.session_permission_mode = str(ses.get("permission_mode", "manual"))
        # 0 = no cap (the user, 2026-10-16); missing or not a number = the default of 4.
        try:
            cfg.session_max_running = max(0, int(ses.get("max_running", 4)))
        except (TypeError, ValueError):
            cfg.session_max_running = 4
        cfg.session_thinking_display = str(ses.get("thinking_display", "summarized") or "")
        cfg.session_auto_titles = _flag(ses, "auto_titles", True, "session.auto_titles")
        cfg.session_job_cleanup = _flag(ses, "job_cleanup", False, "session.job_cleanup")
        cfg.session_show_thinking = _flag(ses, "show_thinking", True, "session.show_thinking")
        cfg.session_turn_summary = _flag(ses, "turn_summary", False, "session.turn_summary")
        # D208's caps: missing or not a number = the default, never "no cap" by accident.
        for attr, key, default in (("session_max_processes", "max_processes", 400),
                                   ("session_max_memory_gb", "max_memory_gb", 16),
                                   ("session_idle_offer_hours", "idle_offer_hours", 8)):
            try:
                setattr(cfg, attr, max(0, int(ses.get(key, default))))
            except (TypeError, ValueError):
                setattr(cfg, attr, default)
        cfg.session_profiles = session_profiles(ses, cfg.session_projects)
        # Loaded without the length limit: a cap lowered since must not lose a panel's projects (the run fails the
        # ones that find no slot instead). A malformed entry falls back to the two empty panels.
        from .presets import PresetError, normalise_presets
        try:
            cfg.session_presets = normalise_presets(ses.get("presets"))
        except PresetError as exc:
            log.warning("session.presets ignored: %s", exc)
            cfg.session_presets = _default_presets()
        hl = data.get("highlight") or {}
        cfg.highlight_session = _flag(hl, "session", True, "highlight.session")
        cfg.highlight_terminal = _flag(hl, "terminal", False, "highlight.terminal")
        cfg.claude_bash_guard = _flag(data.get("claude") or {}, "bash_guard", True, "claude.bash_guard")
        cfg.origin_note = _flag(data.get("claude") or {}, "origin_note", True, "claude.origin_note")
        cfg.claude_deliver_between_tools = _flag(data.get("claude") or {}, "deliver_between_tools", True,
                                                 "claude.deliver_between_tools")
        cfg.claude_collapse_guard = _flag(data.get("claude") or {}, "collapse_guard", True, "claude.collapse_guard")
        codetab = data.get("codetab") or {}
        cfg.codetab_enabled = _flag(codetab, "enabled", False, "codetab.enabled")
        cfg.codetab_port = int(codetab.get("port", 8788))
        cfg.copilot_enabled = _flag(data.get("copilot") or {}, "enabled", False, "copilot.enabled")
        claudeai = data.get("claudeai") or {}
        cfg.claudeai_enabled = _flag(claudeai, "enabled", False, "claudeai.enabled")
        cfg.claudeai_structure_log = _flag(claudeai, "structure_log", False, "claudeai.structure_log")
        cfg.claudeai_mark_role = int(claudeai.get("mark_role", 1) or 0)
        cfg.update_auto = _flag(data.get("update") or {}, "auto", False, "update.auto")
        cfg.feedback_relay = str((data.get("feedback") or {}).get("relay") or cfg.feedback_relay).rstrip("/")
        cfg.canary_fail_open = _flag(data.get("canary") or {}, "fail_open", False, "canary.fail_open")
        cfg.proxy_bypass = _flag(data.get("proxy") or {}, "bypass", False, "proxy.bypass")
        rem = data.get("remote") or {}
        cfg.remote_enabled = _flag(rem, "enabled", False, "remote.enabled")
        cfg.remote_hostname = str(rem.get("hostname", "")).strip().lower()
        cfg.remote_team_domain = str(rem.get("team_domain", "")).strip()
        cfg.remote_audience = str(rem.get("audience", "")).strip()
        cfg.remote_allowed_emails = [str(e).strip().lower() for e in (rem.get("allowed_emails") or [])]
        cfg.remote_encrypt = _flag(rem, "encrypt", False, "remote.encrypt")
        storage = str(rem.get("key_storage", "tab")).strip().lower()
        cfg.remote_key_storage = storage if storage in ("tab", "device") else "tab"
        att = data.get("attachments") or {}
        cfg.allow_images = _flag(att, "allow_images", False, "attachments.allow_images")
        cfg.redact_images = _flag(att, "redact_images", False, "attachments.redact_images")
        cfg.pdftotext = os.path.expandvars(str(att.get("pdftotext", "")))
        cfg.ocr = _flag(att, "ocr", True, "attachments.ocr")
        cfg.ocr_max_pages = int(att.get("ocr_max_pages", 10))
        cfg.ocr_timeout_s = int(att.get("ocr_timeout_s", 60))
        cfg.ocr_max_bytes = int(att.get("ocr_max_bytes", 20_000_000))
        cfg._load_picture_mode(ses)
        if "placeholder_kinds" in det:
            cfg.placeholder_kinds = _held_back([str(k) for k in det.get("placeholder_kinds") or []])
        cfg.secrets_mode = str((data.get("secrets") or {}).get("mode", "redact")).lower()
        if cfg.secrets_mode not in ("redact", "block", "off"):
            raise ValueError("secrets.mode must be redact, block or off")
        logs = data.get("logs") or {}
        cfg.detail_log = _flag(logs, "detail", True, "logs.detail")
        cfg.detail_retention_days = int(logs.get("detail_retention_days", 90))
        cfg.detail_every_request = _flag(logs, "detail_every_request", False, "logs.detail_every_request")
        if data.get("state_dir"):
            cfg.state_dir = Path(os.path.expandvars(str(data["state_dir"])))
        cfg.secrets_user_file = os.path.expandvars(str((data.get("secrets") or {}).get("user_file", "")))
        cfg.client_max_mb = int(data.get("client_max_mb", cfg.client_max_mb))
        cfg.upstream_timeout_s = int(data.get("upstream_timeout_s", cfg.upstream_timeout_s))
        return cfg
