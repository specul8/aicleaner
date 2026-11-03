"""Tell the user there is something to review, on every surface.

Footer: the proxy appends one text block to the assistant reply. For JSON
replies that is an extra content block; for streams it is a synthetic
content_block_start / delta / stop emitted just before message_delta, so
Claude Code and Claude Desktop render it as part of the answer.

Toast: a Windows notification with an "Open review" button that launches the
Review page URL. Runs powershell.exe with the WinRT toast API; silently skipped on
other platforms or if it fails.

Both are rate-limited on time (D55). The footer is also removed again from the history the
client sends back (strip_footers_from_history, D63): a footer left in the model's own prior
turns is a pattern the model learns to continue.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .sse import SSEEvent, make_event

log = logging.getLogger("aicleaner.notify")

# How long the review nag stays quiet after speaking (D55). The footer says "there is work
# waiting", which is worth hearing occasionally and worthless continuously - and every repeat is
# charged against the same response budget as the work, so an unbounded one can truncate a reply.
FOOTER_QUIET_S = 900.0     # 15 minutes
TOAST_QUIET_S = 1800.0     # 30 minutes; a desktop notification is more intrusive than a line
# A tool call carrying part of a token is about to act on the wrong name (D87), so it is told much
# sooner than a review reminder - but still not once per call when a model repeats the pattern.
FRAGMENT_QUIET_S = 120.0

# The footer's own grammar, as footer_text() writes it. Recognising only these phrases is what
# keeps a person's own "---" rule, or a sentence that mentions aicleaner, out of reach.
_PART = (r"(?:\d+ unmapped identifier candidates? to review: [^\s;]+"
         r"|\d+ client names? restored in this reply)")
# One or more footers in a row, with the blank lines that lead into each (D63).
_FOOTERS = re.compile(rf"(?:\s*---[ \t]*\r?\n[ \t]*aicleaner: {_PART}(?:; {_PART})*[ \t]*)+")
# A flood the output cap cut off mid-word leaves a fragment at the very end.
_CUT_FOOTER = re.compile(r"\s*---[ \t]*\r?\n[ \t]*aicleaner: [^\n]*\Z")
# What an assistant turn says when every word of it was footer. The API refuses an empty text
# block, and dropping the turn would break the user/assistant alternation.
_EMPTIED = "(no reply text)"

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$xml = @"
<toast activationType="protocol" launch="__URL__">
  <visual><binding template="ToastGeneric">
    <text>__TITLE__</text>
    <text>__BODY__</text>
  </binding></visual>
  <actions><action content="Open review" activationType="protocol" arguments="__URL__"/></actions>
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
"""


class Notifier:
    def __init__(self, ui_url: str, footer: bool = True, toast: bool = True):
        self.ui_url = ui_url
        self.footer_enabled = footer
        self.toast_enabled = toast
        # Far enough in the past that the first call always speaks, without special-casing it.
        self._last_footer_at = -FOOTER_QUIET_S * 2.0
        self._last_toast_at = -TOAST_QUIET_S * 2.0
        self._last_fragment_at = -FRAGMENT_QUIET_S * 2.0
        self._lock = threading.Lock()

    # ---- decision ----------------------------------------------------------------

    def footer_text(self, pending: int, version: int,
                    restored: int = 0, marked: bool = True) -> str | None:
        """Return the footer to append, or None when there is nothing to say.

        Two independent parts, and only the first is rate-limited.

        The review sentence is a nag, so it speaks at most once per FOOTER_QUIET_S. It used to
        be guarded on the store's `version` instead, and the docstring here claimed it went on
        the count - neither was right (D55). Version bumps on every change, so a session that
        is itself finding candidates saw a fresh version on every reply and carried a footer on
        every reply: several hundred in a row, enough to exhaust a response budget and truncate
        a reply. The count is no better, because a number moving by one every few seconds is
        not news. Time is the only axis that matches what the sentence is for - somebody should
        be told there is work waiting, occasionally.

        `version` is kept in the signature because callers pass it and `maybe_toast` still
        needs it, but it no longer decides anything here.

        The restored count is a fact about this one reply, so it is never rate-limited: the
        reply either restored names or it did not, and skipping it would make the footer lie.
        It is only worth saying to an audience that cannot see the colour marking, which is
        what `marked` reports.
        """
        if not self.footer_enabled:
            return None
        parts: list[str] = []
        if pending > 0:
            with self._lock:
                since = time.monotonic() - self._last_footer_at
                fresh = since >= FOOTER_QUIET_S
                if fresh:
                    self._last_footer_at = time.monotonic()
            if fresh:
                noun = "candidate" if pending == 1 else "candidates"
                parts.append(f"{pending} unmapped identifier {noun} to review: {self.ui_url}")
        if restored > 0 and not marked:
            noun = "name" if restored == 1 else "names"
            parts.append(f"{restored} client {noun} restored in this reply")
        if not parts:
            return None
        return "\n\n---\naicleaner: " + "; ".join(parts)

    def maybe_toast(self, pending: int, version: int) -> None:
        # Rate-limited on time for the same reason as the footer (D55), and it matters more
        # here: a footer that repeats is noise in a transcript, a toast that repeats is a
        # desktop notification every few seconds.
        if not self.toast_enabled or pending <= 0 or sys.platform != "win32":
            return
        with self._lock:
            if time.monotonic() - self._last_toast_at < TOAST_QUIET_S:
                return
            self._last_toast_at = time.monotonic()
        body = f"{pending} unmapped identifier{'s' if pending != 1 else ''} seen on the wire. Review before they recur."
        self._show("aicleaner: identifiers to review", body)

    def fragment_toast(self, fragments: list[str]) -> bool:
        """Say that a tool call went out with part of a token in it (D87). True if shown."""
        if not self.toast_enabled or not fragments or sys.platform != "win32":
            return False
        with self._lock:
            if time.monotonic() - self._last_fragment_at < FRAGMENT_QUIET_S:
                return False
            self._last_fragment_at = time.monotonic()
        shown = ", ".join(sorted(set(fragments))[:3])
        self._show("aicleaner: a tool call kept part of a token",
                   f"It carried {shown}, which no swap-back restored, so it may act on the wrong name. See the log.")
        return True

    def _show(self, title: str, body: str) -> None:
        # Both go inside an XML document inside a PowerShell here-string: escaped for the first,
        # and `$` and the backtick neutralised for the second.
        def safe(text: str) -> str:
            return xml_escape(text).replace("`", "``").replace("$", "`$")

        script = (_TOAST_PS.replace("__URL__", self.ui_url)
                  .replace("__TITLE__", safe(title)).replace("__BODY__", safe(body)))
        threading.Thread(target=self._run_toast, args=(script,), daemon=True).start()

    @staticmethod
    def _run_toast(script: str) -> None:
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # toast is best effort
            log.debug("toast failed: %s", exc)

    # ---- injection ---------------------------------------------------------------

    @staticmethod
    def append_to_json(obj: Any, text: str) -> Any:
        """The footer at the end of a Messages API response: inside its last text block when the reply
        ends with text, so that block - what a ralph loop's stop hook reads for its completion promise -
        still holds what the model wrote (D97); a block of its own after a tool call."""
        if isinstance(obj, dict) and isinstance(obj.get("content"), list):
            content = obj["content"]
            last = content[-1] if content else None
            if isinstance(last, dict) and last.get("type") == "text" and isinstance(last.get("text"), str):
                last["text"] += text
            else:
                content.append({"type": "text", "text": text})
        return obj

    @staticmethod
    def stream_delta(text: str, index: int) -> SSEEvent:
        """The footer as the last piece of an open text block's stream (D97)."""
        return make_event("content_block_delta", json.dumps({"type": "content_block_delta", "index": index,
                                                             "delta": {"type": "text_delta", "text": text}}))

    @staticmethod
    def stream_events(text: str, index: int) -> list[SSEEvent]:
        """Synthetic content block carrying the footer, for insertion before message_delta."""
        return [
            make_event("content_block_start", json.dumps({"type": "content_block_start", "index": index,
                                                          "content_block": {"type": "text", "text": ""}})),
            make_event("content_block_delta", json.dumps({"type": "content_block_delta", "index": index,
                                                          "delta": {"type": "text_delta", "text": text}})),
            make_event("content_block_stop", json.dumps({"type": "content_block_stop", "index": index})),
        ]


# ---- removal (D63) -------------------------------------------------------------------------------
#
# The footer is added to a reply, so the client keeps it as something the model said and sends it
# back on every later request. Left there, the model learns the pattern and writes it itself - in
# session 6dd04873, 871 to 1,554 at a time, until the 64,000-token output cap. It is removed from
# the history the same way the green marks are: ours on the way in, gone on the way out.

def strip_footer(text: str) -> str:
    """The text with every footer removed, including runs of them and a fragment the cap cut."""
    return _CUT_FOOTER.sub("", _FOOTERS.sub("", text))


def _strip_content(content: Any) -> Any:
    if isinstance(content, str):
        return strip_footer(content) or _EMPTIED
    if not isinstance(content, list):
        return content
    kept = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            text = strip_footer(block["text"])
            if not text.strip():
                continue              # the block the proxy appended, or one the model filled with footers
            block = {**block, "text": text}
        kept.append(block)
    return kept or [{"type": "text", "text": _EMPTIED}]


def strip_footers_from_history(obj: Any) -> Any:
    """A Messages API request with the footer removed from every assistant turn.

    Assistant turns only: a person may quote a footer, and what they typed is theirs. Thinking
    blocks are never touched - they carry a signature the provider checks, and the footer is
    never injected into one.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("messages"), list):
        return obj
    messages = []
    for message in obj["messages"]:
        if isinstance(message, dict) and message.get("role") == "assistant" and "content" in message:
            message = {**message, "content": _strip_content(message["content"])}
        messages.append(message)
    return {**obj, "messages": messages}
