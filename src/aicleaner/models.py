"""The model names this machine could actually use (feature 45).

Model was a free-text box. `check_model` tests it against a character class - letters, digits,
`. _ : - [ ]`, not starting with `-` - and then `_command` appends `--model=<whatever you typed>`
and hands it to the CLI. The validation is there to stop a string becoming a second command-line
argument, not to check the name means anything, so a name that does not exist is accepted at save
time and fails when the session starts. That is the worst moment to find out.

Decided 2026-09-27: ask the API. The question had been "does Settings have to hold a credential
just to populate a dropdown", and the answer is that it already does - the installers write an
Anthropic key to `~/.aicleaner/secrets/anthropic.key.dpapi`, DPAPI-encrypted under the current
user, the same way `Enable-RemoteControl.ps1` keeps the Cloudflare token. No new secret, no new
prompt, nothing extra to protect.

Two rules this follows:

- **The field stays free text.** A list that cannot be fetched must not become a box you cannot
  type in - that would make a missing key worse than no feature at all. The names are offered,
  never enforced.
- **Nothing is fetched on the request path.** The list changes when Anthropic releases a model,
  not between two prompts, so it is cached for the day and a failure is remembered briefly rather
  than retried on every page load.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .secrets import read_secret

log = logging.getLogger("aicleaner.models")

# Where the installers put it. `AICLEANER_ANTHROPIC_API_KEY` is the documented override and is
# what `Deploy-AiCleaner.ps1` looks for when the file is absent.
KEY_FILE = "~/.aicleaner/secrets/anthropic.key.dpapi"
KEY_ENV = "AICLEANER_ANTHROPIC_API_KEY"
ENDPOINT = "https://api.anthropic.com/v1/models?limit=100"
VERSION = "2023-06-01"

# Claude Code's own names for its models (`--model opus`), offered whether or not the list can be
# fetched (D95). A subscription login has no API key, so on most machines the fetched list is empty
# and these are the whole choice. They follow the CLI's current models, so they never go stale.
ALIASES = (
    ("opus", "Opus - the most capable (Claude Code's default on most plans)"),
    ("sonnet", "Sonnet - fast and capable, uses less of your plan"),
    ("haiku", "Haiku - fastest, for simple work"),
    ("opusplan", "Opus to plan, Sonnet to carry it out"),
)

GOOD_FOR = 12 * 3600    # a model release is not a thing that happens twice in a day
BAD_FOR = 300           # but a failure should not be retried on every page load either


def key_path() -> Path:
    return Path(os.path.expandvars(KEY_FILE)).expanduser()


def have_key() -> bool:
    return bool(read_secret(key_path(), KEY_ENV))


class ModelList:
    """The names, cached. One instance per proxy."""

    def __init__(self) -> None:
        self._names: list[str] = []
        self._at = 0.0
        self._why = ""

    def get(self, refresh: bool = False) -> dict:
        """{"models": [...], "why": ""} - `why` says what went wrong, for the page to show.

        Never raises: a model list that cannot be fetched is a missing convenience, not a fault,
        and the field it decorates still works without it.
        """
        age = time.time() - self._at
        fresh = age < (GOOD_FOR if self._names else BAD_FOR)
        if not refresh and self._at and fresh:
            return {"models": list(self._names), "why": self._why, "cached": True, "aliases": _aliases()}
        key = read_secret(key_path(), KEY_ENV)
        self._at = time.time()
        if not key:
            self._names, self._why = [], (
                "no Anthropic key on this machine, so the list cannot be fetched. Type the name "
                "instead - the field has always taken one.")
            return {"models": [], "why": self._why, "cached": False, "aliases": _aliases()}
        try:
            self._names = _fetch(key)
            self._why = "" if self._names else "the API returned no models"
        except Exception as exc:  # noqa: BLE001 - every failure here degrades to free text
            log.info("model list unavailable: %s", exc)
            self._names = []
            self._why = f"could not reach the models API ({exc.__class__.__name__}). Type the name instead."
        return {"models": list(self._names), "why": self._why, "cached": False, "aliases": _aliases()}


def _aliases() -> list[dict[str, str]]:
    return [{"name": name, "label": label} for name, label in ALIASES]


def _fetch(key: str) -> list[str]:
    request = urllib.request.Request(ENDPOINT, headers={
        "x-api-key": key, "anthropic-version": VERSION, "accept": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    # `id` is what --model takes; display_name is for people and is not interchangeable.
    return [str(row["id"]) for row in rows
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip()]
