"""Where a prompt was typed (the user, 2026-10-18).

The person often works from a device other than the machine Claude runs on, and had to keep saying
so. aicleaner knows it for each prompt, so it says so: a short note sent with the prompt, in a text
block of its own, wrapped in a tag so the page, titles and search leave it out.

- Through remote access (the tunnel's address): nothing aicleaner installs is on that device.
- The page on this machine (127.0.0.1): the person is at this machine.
- VS Code (through the proxy): either VS Code connected to a VS Code Server on this machine (so remote)
  or VS Code on this machine - the proxy cannot tell which, so the note says both.
"""

from __future__ import annotations

import re

OPEN, CLOSE = "<aicleaner-origin>", "</aicleaner-origin>"
NOTE = re.compile(re.escape(OPEN) + r".*?" + re.escape(CLOSE), re.S)


def device(user_agent: str) -> str:
    """'a Windows browser', 'an iPhone browser' ... from a browser's User-Agent."""
    ua = user_agent or ""
    for needle, name in (("iPhone", "an iPhone"), ("iPad", "an iPad"), ("Android", "an Android device"),
                         ("Windows", "a Windows"), ("Macintosh", "a Mac"), ("CrOS", "a Chromebook"),
                         ("Linux", "a Linux")):
        if needle in ua:
            return f"{name} browser" if not name.endswith("device") else f"{name}'s browser"
    return "a browser"


def page_note(remote: bool, host: str, user_agent: str) -> str:
    if remote:
        where = f" at {host}" if host else ""
        return (f"{OPEN}Sent through aicleaner's remote access{where} from {device(user_agent)}. The person is "
                f"not at the machine Claude runs on: assume nothing aicleaner installs is on their device - "
                f"anything they must run, open or install has to be delivered for that device.{CLOSE}")
    return f"{OPEN}Sent from the aicleaner page on the machine Claude runs on (127.0.0.1).{CLOSE}"


VSCODE_NOTE = (f"{OPEN}Sent from Claude Code in VS Code or a terminal: either VS Code connected remotely to a VS "
               f"Code Server on the machine Claude runs on (the person is elsewhere, and their own device has "
               f"nothing aicleaner installs), or VS Code or a terminal on that machine itself - aicleaner cannot "
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
