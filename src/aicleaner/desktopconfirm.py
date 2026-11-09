"""A person at this machine confirms turning a protection off (H2 part B, 1.1).

The API key (apikey.py) stops a blind `curl`; it cannot stop a program running as the same Windows user, which can read
the key file or load the page. For the few switches that let client names leave unfiltered, the change waits for a
Yes on this machine's desktop - a Windows message box, not something an API call or a script can answer. Through
remote access these switches are refused already (local only), so the box is only ever raised for a change made here.

Raised only when the running proxy asked for it (`Config.desktop_confirm`, set at start); tests that build the app in
process never see a box. AICLEANER_CONFIRM_ANSWER=yes|no, read by the proxy at start, answers it for the test suites.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

log = logging.getLogger("aicleaner.desktopconfirm")

TIMEOUT_S = 120
ANSWER_ENV = "AICLEANER_CONFIRM_ANSWER"
TITLE = "AI Cleaner - turn a protection off?"

# What each switch does when turned the weakening way, in the box's words.
_BOOL_ON = {
    "proxy.bypass": "Send everything unfiltered",
    "canary.fail_open": "Send what the canary refuses",
    "attachments.allow_images": "Send pictures and PDFs as they are, unfiltered",
}
_BOOL_OFF = {
    "claudeai.enabled": "Stop filtering Claude Desktop's chat",
    "copilot.enabled": "Stop filtering GitHub Copilot in VS Code",
    "codetab.enabled": "Stop filtering the Claude Desktop app's Code tab",
}
_TOOLS = {"ClaudeCode": "Stop filtering Claude Code", "CodeTab": "Stop filtering the Claude Desktop app's Code tab",
          "Copilot": "Stop filtering GitHub Copilot in VS Code"}


def weakening(changes: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """The protections a settings change turns off, described; empty when it turns none off. `current` is
    Config.settings() before the change: a switch already off is not asked about again."""
    said = []
    for key, text in _BOOL_ON.items():
        if key in changes and changes[key] is True and current.get(key) is not True:
            said.append(text)
    for key, text in _BOOL_OFF.items():
        if key in changes and changes[key] is False and current.get(key) is True:
            said.append(text)
    if "secrets.mode" in changes and str(changes["secrets.mode"]).strip().lower() == "off" \
            and current.get("secrets.mode") != "off":
        said.append("Stop finding secrets (keys, passwords) in what is sent")
    return said


def tool_off(only: str) -> list[str]:
    """Taking one tool off routing (setup_disable)."""
    return [_TOOLS.get(only, f"Stop filtering {only}")]


def _message(changes: list[str]) -> str:
    lines = "\n".join(f"  - {c}" for c in changes)
    return (f"A change to AI Cleaner's settings would turn a protection off:\n\n{lines}\n\n"
            "Real names could then reach the AI service unfiltered. Did you make this change?\n\n"
            f"Choose No if you did not - a program on this machine may be trying to. (No is chosen after "
            f"{TIMEOUT_S // 60} minutes.)")


def _ask_windows(text: str) -> bool:
    import ctypes

    MB_YESNO, MB_ICONWARNING, MB_DEFBUTTON2 = 0x4, 0x30, 0x100
    MB_SYSTEMMODAL, MB_SETFOREGROUND, MB_TOPMOST = 0x1000, 0x10000, 0x40000
    IDYES = 6
    flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
    user32 = ctypes.windll.user32
    try:
        # MessageBoxTimeoutW: in user32 since Windows XP, not in the SDK headers; answers 32000 when it times out.
        box = user32.MessageBoxTimeoutW
        box.restype = ctypes.c_int
        box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_ushort,
                        ctypes.c_uint]
        answer = box(None, text, TITLE, flags, 0, TIMEOUT_S * 1000)
    except AttributeError:
        answer = user32.MessageBoxW(None, text, TITLE, flags)
    return answer == IDYES


# A change already confirmed, briefly: taking a tool off routing runs Disable-AiCleaner.ps1, which then saves the same
# switch through /api/config - one Yes covers both. A remote request (Access-verified) grants it the same way, so the
# script it starts is not left waiting on a box nobody at the desktop will answer.
GRANT_S = 120
_granted: dict[str, float] = {}


def grant(changes: list[str]) -> None:
    now = time.monotonic()
    for change in changes:
        _granted[change] = now + GRANT_S


def _still_granted(change: str) -> bool:
    return _granted.get(change, 0.0) > time.monotonic()


async def confirm(changes: list[str]) -> tuple[bool, str]:
    """(confirmed, why not). Waits for the person, at most TIMEOUT_S."""
    changes = [c for c in changes if not _still_granted(c)]
    if not changes:
        return True, ""
    preset = os.environ.get(ANSWER_ENV, "").strip().lower()
    if preset in ("yes", "no"):
        log.info("protection-off change answered %s by %s: %s", preset, ANSWER_ENV, "; ".join(changes))
        if preset == "yes":
            grant(changes)
        return preset == "yes", "" if preset == "yes" else "declined"
    if sys.platform != "win32":
        return False, "there is no desktop here to confirm it on"
    log.warning("asking on the desktop before turning a protection off: %s", "; ".join(changes))
    try:
        yes = await asyncio.to_thread(_ask_windows, _message(changes))
    except OSError as exc:
        log.warning("the confirmation could not be shown: %s", exc)
        return False, f"the confirmation could not be shown ({exc})"
    log.warning("protection-off change %s on the desktop", "confirmed" if yes else "declined or timed out")
    if yes:
        grant(changes)
    return yes, "" if yes else "declined or not answered on this machine's desktop"


def refusal(why: str) -> str:
    return (f"Not changed: turning a protection off needs a Yes on this machine's desktop ({why}). "
            "Make the change at this machine and answer the box AI Cleaner shows.")
