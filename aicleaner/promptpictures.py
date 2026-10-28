"""D216 (spec 2026-10-28-d216-session-pictures.md): pictures attached to a Session-page prompt.

The page sends them as `images: [{name, media_type, data}]` beside the text. Checked here once, on the way in, so
the queue, `_send` and the proxy only ever see what the AI service takes: PNG, JPEG, GIF or WebP, each within the
API's 5 MB, at most MAX_COUNT to a prompt. The type is read from the bytes, not taken from the page: the API refuses
a picture whose bytes are not the type it is labelled, and a refusal there would cost the whole turn.

What leaves the machine is still the proxy's business (feature 114): redacted, as it is, or its text, per Pictures.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any

MAX_BYTES = 5 * 1024 * 1024         # the API's limit for one picture, counted on the base64 it is sent as
MAX_COUNT = 20                      # to one prompt (the API takes more; the page and the broker need not)
MAX_TOTAL = 20 * 1024 * 1024        # base64, all of one prompt's pictures: the broker's line and the request's room
NAME_MAX = 120

# The four types the API takes, by their first bytes.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class PictureError(ValueError):
    """A picture the prompt cannot take, said so the page can show it."""


def sniff(raw: bytes) -> str:
    """The picture's type from its bytes, or "" for anything the API does not take."""
    for head, media in SIGNATURES:
        if raw.startswith(head):
            return media
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _name(value: Any, index: int) -> str:
    name = " ".join(str(value or "").split())[:NAME_MAX]
    return name or f"picture {index + 1}"


def checked(images: Any) -> list[dict[str, str]]:
    """The page's pictures as [{name, media_type, data}], each checked; PictureError for the first that is not."""
    if images in (None, ""):
        return []
    if not isinstance(images, list):
        raise PictureError("images must be a list")
    if len(images) > MAX_COUNT:
        raise PictureError(f"at most {MAX_COUNT} pictures go with one prompt, not {len(images)}")
    out: list[dict[str, str]] = []
    total = 0
    for index, item in enumerate(images):
        if not isinstance(item, dict):
            raise PictureError("each picture is {name, media_type, data}")
        name = _name(item.get("name"), index)
        data = str(item.get("data") or "").strip()
        if not data:
            raise PictureError(f"{name} has no picture in it")
        if len(data) > MAX_BYTES:
            raise PictureError(f"{name} is over the 5 MB the AI service takes for one picture")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise PictureError(f"{name} is not base64") from None
        media = sniff(raw)
        if not media:
            raise PictureError(f"{name} is not a PNG, JPEG, GIF or WebP picture")
        total += len(data)
        if total > MAX_TOTAL:
            raise PictureError(f"the pictures come to over {MAX_TOTAL // (1024 * 1024)} MB; send some with the next prompt")
        out.append({"name": name, "media_type": media, "data": data})
    return out


def blocks(images: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The API's image blocks, in the order attached."""
    return [{"type": "image", "source": {"type": "base64", "media_type": p["media_type"], "data": p["data"]}}
            for p in images]


def names(images: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """What the page is told about pictures: their names, never their bytes."""
    return [{"name": str(p.get("name") or "picture")} for p in images or [] if isinstance(p, dict)]


def image_blocks_in(content: Any) -> int:
    """How many image blocks a user message's content holds (replay: the broker's input, a transcript record)."""
    if not isinstance(content, list):
        return 0
    return sum(1 for block in content if isinstance(block, dict) and block.get("type") == "image")
