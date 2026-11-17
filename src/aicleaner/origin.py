"""Where a prompt was typed (the user, 2026-10-18).

The person often works from a device other than the machine Claude runs on, and had to keep saying
so. AI Cleaner knows it for each prompt, so it says so: a short note sent with the prompt, in a text
block of its own, wrapped in a tag so the page, titles and search leave it out.

- Through remote access (the tunnel's address): nothing AI Cleaner installs is on that device.
- The page on this machine (127.0.0.1): the person is at this machine.
- VS Code (through the proxy): either VS Code connected to a VS Code Server on this machine (so remote)
  or VS Code on this machine - the proxy cannot tell which, so the note says both.
"""

from __future__ import annotations

import re

OPEN, CLOSE = "<aicleaner-origin>", "</aicleaner-origin>"
NOTE = re.compile(re.escape(OPEN) + r".*?" + re.escape(CLOSE), re.S)
# The note's own tag, opening or closing, in any case and with any whitespace inside it (`< / AICleaner-Origin >`):
# text from outside (a LAN message's source, summary or words, a sender's name) that holds it could forge a note or
# close the real one early, so it is refused where such text comes in (security review, 2026-11-14).
TAG = re.compile(r"<\s*/?\s*aicleaner\s*-\s*origin", re.I)


def forges_note(text: str) -> bool:
    """Whether this text holds the origin note's tag (opening or closing), so it could forge or end a note."""
    return isinstance(text, str) and bool(TAG.search(text))


def quoted(text: str) -> str:
    """Text from outside, quoted for a note: in double quotes, with quotes, backslashes and angle brackets escaped,
    so it cannot end the quote or open or close a tag."""
    return '"' + (str(text).replace("\\", "\\\\").replace('"', '\\"').replace("<", "\\u003c")
                  .replace(">", "\\u003e")) + '"'


def device(user_agent: str) -> str:
    """'a Windows browser', 'an iPhone browser' ... from a browser's User-Agent."""
    ua = user_agent or ""
    for needle, name in (("iPhone", "an iPhone"), ("iPad", "an iPad"), ("Android", "an Android device"),
                         ("Windows", "a Windows"), ("Macintosh", "a Mac"), ("CrOS", "a Chromebook"),
                         ("Linux", "a Linux")):
        if needle in ua:
            return f"{name} browser" if not name.endswith("device") else f"{name}'s browser"
    return "a browser"


def page_note(remote: bool, host: str, user_agent: str, device_name: str = "") -> str:
    """f168 (Brad, 2026-11-08): through remote access the note is short and names the device when the browser was told
    which it is, so the person's own rules can depend on it ("from the tablet, no commands on the desktop"). The name
    is the browser's word, not proof. `host` and `user_agent` are no longer said."""
    if remote:
        from .config import device_name as checked

        name = checked(device_name)
        return f"{OPEN}The user is accessing the AI Cleaner app remotely{f' from {name}' if name else ''}.{CLOSE}"
    return f"{OPEN}Sent from the AI Cleaner page on the machine Claude runs on (127.0.0.1).{CLOSE}"


VSCODE_NOTE = (f"{OPEN}Sent from Claude Code in VS Code or a terminal: either VS Code connected remotely to a VS "
               f"Code Server on the machine Claude runs on (the person is elsewhere, and their own device has "
               f"nothing AI Cleaner installs), or VS Code or a terminal on that machine itself - AI Cleaner cannot "
               f"tell which.{CLOSE}")

_SKIP = ("<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-caveat>",
         "<local-command-stderr>", "Caveat:", "<task-notification>")


def _typed(content: list) -> bool:
    """A person's prompt: no tool result in it, and some text that is not a command echo or a note."""
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    if any(t.lstrip().startswith("/") for t in texts):
        return False                     # a slash command: left exactly as typed
    return any(t.strip() and not t.lstrip().startswith(("<system-reminder>", *_SKIP)) and not is_note(t) for t in texts)


def add_to_typed(obj: dict, note: str) -> dict:
    """The note added to EVERY typed prompt in a Messages request, not just the newest: the same text in
    the same place each time, so the history is identical from one request to the next and the
    provider's prompt cache still matches. The client never sees it - its own history stays as typed."""
    for message in obj.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            # Kept a string (code further on reads a string prompt as one): the note follows the words.
            # Never after a slash command - it would no longer read as one.
            if (content.strip() and not content.lstrip().startswith((*_SKIP, "/")) and OPEN not in content):
                message["content"] = f"{content}\n\n{note}"
            continue
        if not isinstance(content, list) or not _typed(content):
            continue
        if any(isinstance(b, dict) and is_note(b.get("text", "")) for b in content):
            continue
        message["content"] = [*content, {"type": "text", "text": note}]
    return obj


def is_note(text: str) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(OPEN)


def strip(text: str) -> str:
    return NOTE.sub("", text or "").strip()
