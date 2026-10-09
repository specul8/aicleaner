"""Apply a string function to every string leaf of a JSON document.

Tokenising and rehydrating rewrite keys as well as values. Keys were once left
alone as "schema, not content", but a request carries content in keys too - a
tool input or a result keyed by file path or tenant name - and the canary reads
keys, so a real name in one refused the whole request with nothing to fix it.
Rewriting both ways keeps a round trip exact: a tool call the model makes with
a tokenised key comes back with the real one. Other walks (marks, secrets)
still leave keys alone. Base64 payloads are skipped because a client name cannot appear in them and they can be large.
The same goes for the fields that carry a provider's own signed material: a
thinking block's signature, and a redacted_thinking block's data. Those are
opaque, they have to come back byte for byte on the next turn, and a real
that happened to sit inside one would be substituted and the block broken.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter
from typing import Any, Callable, Iterable

from .mapping import MARK_CLOSE, MARK_OPEN, Mapper

StrFn = Callable[[str], str]


def _is_opaque(obj: dict[str, Any], key: str) -> bool:
    """Fields that are the provider's to read, not ours: signed and byte-exact.

    A thinking block carries `signature`, and the streamed form arrives as a `signature_delta`
    whose `signature` is the same material. `redacted_thinking` carries `data`. Touching any of
    them can only invalidate what the provider signed.
    """
    # By position, not by name (H5): a `signature` anywhere else - a tool input, a text block - is
    # ordinary text, and skipping it let a real name past the tokeniser and the canary.
    if key == "signature":
        return obj.get("type") in ("thinking", "signature_delta")
    return key == "data" and obj.get("type") == "redacted_thinking"


_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def _attachment_data(obj: dict[str, Any], key: str, value: Any) -> bool:
    """An attachment's payload: skipped because a name cannot be in it and it can be megabytes.

    Only a real attachment source - `"type": "base64"` with a `media_type` beside it - and only when
    the payload is base64 (H5). `{"type": "base64", "data": "<real name>"}` used to be skipped by
    every walk; it is text now, so it is tokenised and the canary reads it. A regex, not
    b64decode: it runs over megabytes on every turn and needs no decoded copy."""
    if key != "data" or not isinstance(value, str) or obj.get("type") != "base64" or "media_type" not in obj:
        return False
    return _BASE64.fullmatch(value.replace("\n", "").replace("\r", "")) is not None


def _is_verbatim(obj: dict[str, Any], key: str) -> bool:
    """A thinking block's text: readable, scanned, and never rehydrated (D8).

    The provider signed the block over the text it saw, which is the TOKENISED text. Rehydrating
    it on the way in means the client holds real names, and the resend has to reproduce the
    signed bytes exactly by tokenising them again - which it cannot reliably do: a placeholder
    may have become a mapped token since, a case variant may have been learned, a near miss may
    have been repaired. The block is then rejected, and a rejected thinking block can make the
    model itself malfunction.

    Decided 2026-09-27: a resendable thinking block beats a readable one. It also makes the
    transcript show what was *actually sent* rather than a reconstruction of it, which is
    traceability end to end.

    Two things this is deliberately NOT:

    - **Not `_is_opaque`.** That means "neither rewrite nor read", and it is right for a
      signature, which is bytes. A thinking block is prose, so the canary must keep reading it:
      leaving it unrewritten is a decision about fidelity, not a reason to stop looking for a
      leak inside it. `readable_strings` therefore ignores this predicate.
    - **Not symmetric.** It applies on the way IN only. Outbound, a thinking block is still
      tokenised - normally a no-op, because it already holds tokens and nothing matches. But a
      conversation that began before this change has real names in its history, and those must
      still be replaced on their way out. Byte-identical in the ordinary case, fail-safe in the
      legacy one.
    """
    return key == "thinking" and obj.get("type") in ("thinking", "thinking_delta")


# JSON Schema's own keywords. Their values are structural - a meta-schema URI, a reference, an
# identifier, a regex - and the provider parses them rather than reading them.
_SCHEMA_KEYWORDS = frozenset({"$schema", "$ref", "$id", "$anchor", "$dynamicRef", "$dynamicAnchor",
                              "$vocabulary", "format", "pattern"})
# The values "type" takes in a schema. None of Anthropic's block types (text, image, tool_use,
# tool_result, thinking, document) is one, so a content block is never mistaken for a schema.
_SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def _is_schema_node(obj: dict[str, Any]) -> bool:
    """Whether this dict is a JSON Schema (or a subschema inside one)."""
    if "$schema" in obj or "properties" in obj:
        return True
    kind = obj.get("type")
    return isinstance(kind, str) and kind in _SCHEMA_TYPES


def _is_structural(obj: dict[str, Any], key: str) -> bool:
    """Schema keywords, which are rewritten into nonsense and refused.

    A tool's input_schema carries "$schema": "https://json-schema.org/draft/2020-12/schema". The
    domain in it is detected as an identifier like any other, and while it sits unreviewed the
    fail-closed pass substitutes a placeholder - so the provider was handed
    "https://pending-a1b2/draft/2020-12/schema", stopped recognising the meta-schema, and answered
    "tools.0.custom.input_schema: JSON schema is invalid. It must match JSON Schema draft 2020-12"
    to EVERY request. A $ref or $id breaks the same way, and a rewritten pattern is a regex that
    quietly stops matching.

    Unlike signed material these are not skipped by the canary: it still reads them, so a client's
    real name sitting in one refuses the request instead of going out. Rewriting them was never the
    protection - being read was.
    """
    return key in _SCHEMA_KEYWORDS and _is_schema_node(obj)


def _is_json_text(obj: dict[str, Any], key: str) -> bool:
    """Strings that carry JSON: streamed tool input (partial_json) and function-call arguments
    (an "arguments" string beside a "name"). Reals inside them are escaped."""
    return key == "partial_json" or (key == "arguments" and "name" in obj)


def walk_strings(obj: Any, fn: StrFn, json_fn: StrFn | None = None, keys: bool = False,
                 verbatim: bool = False) -> Any:
    """`verbatim` additionally leaves a thinking block's text alone - set on the rehydrate
    direction only, so the block goes back to the provider as the bytes it signed (D8)."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [walk_strings(item, fn, json_fn, keys, verbatim) for item in obj]
    if isinstance(obj, dict):
        schema_node = _is_schema_node(obj)
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if _attachment_data(obj, key, value):
                new = value
            elif isinstance(value, str) and (_is_opaque(obj, key) or (schema_node and key in _SCHEMA_KEYWORDS)
                                             or (verbatim and _is_verbatim(obj, key))):
                new = value
            elif json_fn is not None and isinstance(value, str) and _is_json_text(obj, key):
                new = json_fn(value)
            else:
                new = walk_strings(value, fn, json_fn, keys, verbatim)
            out[_new_key(key, fn, obj, out) if keys else key] = new
        return out
    return obj


def _new_key(key: str, fn: StrFn, original: dict[str, Any], out: dict[str, Any]) -> str:
    """The rewritten key, unless that would land on another key and lose a value. Then the key is
    kept as it was: nothing is dropped, and the canary refuses the request rather than sending it."""
    new = fn(key)
    if new != key and (new in original or new in out):
        return key
    return new


def tokenise_json(mapper: Mapper, obj: Any, counter: Counter[str]) -> Any:
    return walk_strings(obj, lambda s: mapper.tokenise(s, counter), lambda s: mapper.tokenise_json_text(s, counter),
                        keys=True)


def rehydrate_json(mapper: Mapper, obj: Any, counter: Counter[str]) -> Any:
    return walk_strings(obj, lambda s: mapper.rehydrate(s, counter), lambda s: mapper.rehydrate_json_text(s, counter),
                        keys=True, verbatim=True)


def strip_marks(text: str) -> str:
    """Remove the green marks we added to restored names.

    A client sends its history back on every turn, and that history carries the marks. They are
    ours: the provider must never see them, and the model must never learn to write them. Only the
    two exact sequences we use are removed, so colour in a pasted terminal log survives.

    Their visible halves go too (D151): a model that saw the marks in its history copies them
    without the escape it cannot type, and "[32;22m" in its next reply is then in the history
    teaching the same thing. The 32;22 pair is ours alone, so nothing a person wrote is caught.
    """
    if ";22m" not in text:
        return text
    for mark in (MARK_OPEN, MARK_CLOSE):
        text = text.replace(mark, "").replace(mark[1:], "")
    return text


def tool_is_kept(name: str | None, keep_patterns: Iterable[str]) -> bool:
    """True when a tool's arguments must stay tokenised (remote MCP servers)."""
    return bool(name) and any(fnmatch.fnmatchcase(name, pat) for pat in keep_patterns)


def rehydrate_message(
    mapper: Mapper, obj: Any, counter: Counter[str], keep_patterns: Iterable[str], mark: bool = False
) -> tuple[Any, int]:
    """Rehydrate a Messages API response, leaving kept tool_use inputs tokenised.

    With `mark`, restored names in `text` blocks are wrapped for the reader. Nothing else is: a
    tool_use input becomes a file or a command, where an escape sequence is a bug.

    Returns the rewritten object and how many tool_use blocks were kept.
    """
    if isinstance(obj, dict) and isinstance(obj.get("choices"), list):
        return _rehydrate_chat_completion(mapper, obj, counter, keep_patterns)
    if not isinstance(obj, dict) or not isinstance(obj.get("content"), list):
        return rehydrate_json(mapper, obj, counter), 0
    kept = 0
    out = {k: (v if k == "content" else rehydrate_json(mapper, v, counter)) for k, v in obj.items()}
    blocks = []
    for block in obj["content"]:
        if isinstance(block, dict) and block.get("type") == "tool_use" and tool_is_kept(block.get("name"), keep_patterns):
            blocks.append(block)
            kept += 1
        elif mark and isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            marked = {k: (v if k == "text" else rehydrate_json(mapper, v, counter)) for k, v in block.items()}
            marked["text"] = mapper.rehydrate(block["text"], counter, mark=True)
            blocks.append(marked)
        else:
            blocks.append(rehydrate_json(mapper, block, counter))
    out["content"] = blocks
    return out, kept


def _rehydrate_chat_completion(mapper: Mapper, obj: dict, counter: Counter[str],
                               keep_patterns: Iterable[str]) -> tuple[Any, int]:
    """An OpenAI Chat Completions reply, not streamed (H9): a kept tool's call keeps its tokenised
    arguments, as the Anthropic reply and the OpenAI stream already did - a hosted tool must not be
    handed real names. Everything else is restored as before."""
    kept = 0
    out = {k: (v if k == "choices" else rehydrate_json(mapper, v, counter)) for k, v in obj.items()}
    choices = []
    for choice in obj["choices"]:
        message = choice.get("message") if isinstance(choice, dict) else None
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list):
            choices.append(rehydrate_json(mapper, choice, counter))
            continue
        new_choice = {k: (v if k == "message" else rehydrate_json(mapper, v, counter)) for k, v in choice.items()}
        new_message = {k: (v if k == "tool_calls" else rehydrate_json(mapper, v, counter)) for k, v in message.items()}
        new_calls = []
        for call in calls:
            name = ((call or {}).get("function") or {}).get("name") if isinstance(call, dict) else None
            if tool_is_kept(name, keep_patterns):
                new_calls.append(call)
                kept += 1
            else:
                new_calls.append(rehydrate_json(mapper, call, counter))
        new_message["tool_calls"] = new_calls
        new_choice["message"] = new_message
        choices.append(new_choice)
    out["choices"] = choices
    return out, kept


def dumps(obj: Any) -> bytes:
    # ensure_ascii=False keeps the body byte-for-byte closer to what the
    # client sent; separators keep it compact.
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def readable_strings(obj: Any, keys: bool = True) -> Iterable[str]:
    """Every string in a JSON document that carries text: keys and leaves, decoded.

    Leaves out exactly what walk_strings leaves alone - a provider's signed material and base64
    payloads - because those are opaque bytes, never rewritten, and cannot carry a client's name
    as text. A match inside one is coincidence: fragments of a thinking signature were learned as
    "identifiers", mapped into Global, and then refused every request that carried the signature.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from readable_strings(item, keys)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if keys:
                yield key
            if isinstance(value, str) and (_is_opaque(obj, key) or _attachment_data(obj, key, value)):
                continue
            yield from readable_strings(value, keys)


# A JSON string escape: an escaped backslash (kept, so the pairs stay aligned) or one that stands
# for a control character (turned into a space).
_ESCAPE = re.compile(r"\\\\|\\(?:[nrtbf]|u[0-9a-fA-F]{4})")


def _without_opaque(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_without_opaque(item) for item in obj]
    if isinstance(obj, dict):
        schema_node = _is_schema_node(obj)
        # Schema keywords are blanked here as well: nothing rewrites them, so queueing the domain
        # out of a meta-schema URI for review asks a question whose answer changes nothing.
        return {key: "" if isinstance(value, str) and (_is_opaque(obj, key) or _attachment_data(obj, key, value)
                                                       or (schema_node and key in _SCHEMA_KEYWORDS))
                else _without_opaque(value) for key, value in obj.items()}
    return obj


def detection_text(obj: Any) -> str:
    """The text every detector reads: the body serialised as JSON, so a key stays beside its value
    ("tenantId": "<guid>" is how a tenant id is recognised), with each escape for a line break or
    tab turned back into a space, and signed or base64 payloads blanked.

    Serialised as is, a line starting with a declared domain reads as "\\n" + the domain, and the
    word the detectors saw was "n" + the domain: one letter from a mapped value, so it was queued as
    a near miss (and could be auto-added) on every request carrying it.
    """
    dumped = json.dumps(_without_opaque(obj), ensure_ascii=False)
    return _ESCAPE.sub(lambda m: m.group(0) if m.group(0) == "\\\\" else " ", dumped)


# Characters shown either side of a canary match: enough to see what the text was about.
CANARY_CONTEXT = 60


def canary_matches(mapper: Mapper, body: bytes, limit: int = 20) -> list[dict[str, Any]]:
    """Each canary match with its surroundings: the JSON path, key or string, the client, the form's
    position in the mapping, the text as found, and up to CANARY_CONTEXT characters either side.

    The text is included (2026-09-30, DECISIONS 40). The earlier rule - never the text, so the log
    explaining a refusal cannot itself carry the name - left a refusal undebuggable: without the
    surroundings there is no telling a real leak from a false positive. These logs stay on this
    machine, and real data in them is intended.
    """
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return []
    index = {id(f): i for i, f in enumerate(mapper.forms)}
    found: list[dict[str, Any]] = []

    def flat(text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def check(text: str, where: str, path: str) -> None:
        for form, start, end in mapper.real_spans(text):
            if len(found) < limit:
                found.append({"path": path, "where": where, "client": form.client,
                              "form": index.get(id(form), -1), "found": text[start:end],
                              "before": flat(text[max(0, start - CANARY_CONTEXT):start]),
                              "after": flat(text[end:end + CANARY_CONTEXT])})

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            check(node, "string", path)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")
        elif isinstance(node, dict):
            for key, value in node.items():
                check(key, "key", f"{path}/{{key}}")
                if isinstance(value, str) and (_is_opaque(node, key) or _attachment_data(node, key, value)):
                    continue
                walk(value, f"{path}/{key}")

    walk(obj, "")
    return found


def canary_sites(mapper: Mapper, body: bytes, limit: int = 20) -> list[str]:
    """Where the canary matched, as lines for the local log - with the match in brackets in its
    surroundings, so a refusal can be told apart from a false positive (DECISIONS 40)."""
    try:
        json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return ["body is not JSON"]
    return [f"{m['path']} ({m['where']}): client {m['client']}, form #{m['form']}: "
            f"...{m['before']}[{m['found']}]{m['after']}..." for m in canary_matches(mapper, body, limit)]


def rule_declined(mapper: Mapper, body: bytes) -> list[tuple[str, str, str]]:
    """(client, text as found, declared real) for each occurrence in an outbound body that a match
    rule declined to replace - what the canary lets through, reported so it is seen (DECISIONS 39).
    Reads the same text the canary reads."""
    if not any(f.match is not None for f in mapper.forms):
        return []
    text = body.decode("utf-8", errors="replace")
    try:
        text = "\n".join(readable_strings(json.loads(text)))
    except json.JSONDecodeError:
        pass
    return [(f.client, found, f.real) for f, found in mapper.rule_declined(text)]


# Characters that show as nothing (zero-width space / joiner / non-joiner, word joiner, BOM, soft
# hyphen) and ones that show as a space (no-break, narrow no-break, figure space).
_INVISIBLE = re.compile("[​‌‍⁠﻿­]")
_SPACELIKE = re.compile("[   ]")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_ENTITY = re.compile(r"&(?:nbsp|#32|#x20|#160|#xa0);", re.IGNORECASE)


def percent_decoded(text: str) -> str:
    """%XX turned back into the printable ASCII character it stands for; anything else - "50%",
    an escaped byte of a UTF-8 sequence - left as it is."""
    return _PERCENT.sub(lambda m: chr(int(m.group(1), 16)) if 0x20 <= int(m.group(1), 16) < 0x7f
                        else m.group(0), text)


def normalised(text: str) -> str:
    """The text with the usual disguises taken off (H7, H34): %XX decoded where it stands for a
    printable ASCII character, NFKC (full-width letters), invisible characters dropped, no-break
    spaces and their HTML entities made plain spaces. Read by the canary, never sent."""
    import unicodedata

    text = _ENTITY.sub(" ", percent_decoded(text))
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    return _SPACELIKE.sub(" ", text)


def canary(mapper: Mapper, body: bytes) -> list[str]:
    """Return client ids whose real forms still appear in an outbound body.

    Reads the body's decoded strings and its keys - the same text the tokeniser rewrote, plus the
    keys it leaves alone - rather than the serialised bytes. On the bytes, a newline is the two
    characters \\ and n, so a form beginning with n, t, r, b, f or u was "found" straddling an
    escape, and a real session was refused on every request with nothing real in it. Keys are
    read too; tokenise_json rewrites them, so a key only survives when rewriting it would have
    collided with another key. An empty list means clean.
    """
    text = body.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return _with_disguised(mapper, text)
    # A newline between strings is a word boundary, so no match can span two of them.
    return _with_disguised(mapper, "\n".join(readable_strings(obj)))


def _with_disguised(mapper: Mapper, text: str) -> list[str]:
    """What the text names as written, and what it names once its disguises are off (H7). The
    second read only adds: a request is refused for a name hidden by %41 or a zero-width space."""
    found = set(mapper.find_real(text))
    plain = normalised(text)
    if plain != text:
        found.update(mapper.find_real(plain))
    return sorted(found)
