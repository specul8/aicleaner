"""Incremental filtering: a string already filtered is not filtered again (spec
2026-11-10-incremental-filtering.md, decided by Brad 2026-11-10).

A conversation's request is the previous request plus a few new messages, and the filter read all of it every time:
8.5 - 10 s per request at 3.6 MB. Each pass over the body now works on units - one string for tokenising, secrets
and the canary; one piece of the detection text (a JSON object at the leaves' level with its keys, so a key stays
beside its value) for detection - and what a unit gave is remembered, keyed by the SHA-256 of the unit and the
filter generation. The generation is everything that can change a result: the mapping (each Mapper has its own
serial number), the case variants it has learned, the ignore list, the acronym list detection keeps, and the
settings that change results. Any change starts a new generation, and nothing from an older one is used again.

What is remembered is client data, so it is held in memory only (an LRU of about 200 MB), never written anywhere,
and lost on restart.

The passes are the same functions as before, run per unit, and put back together in document order:
- tokenising: `walk_strings` itself with a remembering function, so the walk - keys after values, key collisions,
  signed and schema fields left alone - is the walk it always was;
- detection: `detect.unit_findings` per piece and `detect.merged_findings` over them, which decides the two
  detectors that read beyond a unit (a domain's context, an IPv4 address's version test) against the whole text;
- secrets: redaction per string as before; block mode's families per piece, with `private-key` (BEGIN and END can
  sit in different strings) searched over the whole text;
- the canary: see FilterRun.canary - it reads the whole body as sent, one string at a time, and skips only a string
  it has itself cleared under the same generation.

`filter.incremental` off runs the whole-body functions as before. `filter.shadow` runs both, sends the old result and
logs any difference as sizes and kinds, never text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter, OrderedDict
from typing import Any, Callable, Iterable

from . import detect
from .detect import kept_candidates, merged_findings, unit_findings
from .mapping import NEIGHBOUR_CHARS, Mapper, _declined
from .redact import PATTERNS, find_families, redact
from .transform import (_ESCAPE, _SCHEMA_KEYWORDS, _attachment_data, _is_opaque, _is_schema_node, canary,
                        detection_text, normalised, readable_strings, rule_declined, tokenise_json, walk_strings)

log = logging.getLogger("aicleaner.incremental")

# The decided limits (Brad 2026-11-10): about 200 MB of remembered results, 200,000 units.
CACHE_BYTES = 200 * 1024 * 1024
CACHE_ENTRIES = 200_000
# Below this much new text a request is filtered in one worker thread: handing work to a process costs more than it
# saves for a few messages.
POOL_MIN_BYTES = 200 * 1024
# The secret family whose match can run from one string into another: a key's BEGIN and END lines.
SPANNING_FAMILY = "private-key"
# How often (s) a thread holding the GIL is asked to hand it over. Python's default is 5 ms, but on Windows a waiting
# event loop got it back only every ~50 ms while a filter thread ran (GIL hand-off plus the 15.6 ms timer; the spec's
# limit is 50 ms); at 0.5 ms the worst gap measured 16 - 25 ms (wip/gil_probe.py, 2026-11-10).
LOOP_SWITCH_INTERVAL = 0.0005


def share_the_gil() -> None:
    """Shorten the interpreter's switch interval so filtering on a worker thread does not stall the event loop.
    Process-wide, and only ever made shorter."""
    import sys

    if sys.getswitchinterval() > LOOP_SWITCH_INTERVAL:
        sys.setswitchinterval(LOOP_SWITCH_INTERVAL)


class _Counts(Counter):
    """A counter with the (client, real, token) pairs beside it, as evidence.SubCounter (kept here so a worker
    process does not import the evidence log)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.pairs: Counter[tuple[str, str, str]] = Counter()


def digest(text: str) -> bytes:
    """The SHA-256 of a unit. surrogatepass: a JSON body may carry a lone surrogate (\\ud800), which is a string
    Python holds but UTF-8 cannot encode."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


# ---- the units --------------------------------------------------------------------------------------------------

def unit_tokenise(mapper: Mapper, mode: str, text: str) -> tuple[str | None, tuple, tuple]:
    """One string tokenised: (the new text, or None when unchanged; counts; pairs). `mode` "j" is JSON text inside a
    string (tool-call arguments), as tokenise_json treats it."""
    counts = _Counts()
    out = mapper.tokenise(text, counts) if mode == "t" else mapper.tokenise_json_text(text, counts)
    return (None if out == text else out), tuple(counts.items()), tuple(counts.pairs.items())


def unit_redact(text: str) -> tuple[str | None, tuple]:
    """One string through the built-in secret patterns: (the new text or None, family counts)."""
    counts: Counter[str] = Counter()
    out = redact(text, counts)
    return (None if out == text else out), tuple(counts.items())


def unit_families(text: str) -> frozenset[str]:
    """Block mode's secret families in one piece of the detection text, less the one that can span pieces."""
    return frozenset(family for family, pattern in PATTERNS if family != SPANNING_FAMILY and pattern.search(text))


def _near_an_edge(rule: Any, start: int, end: int, length: int) -> bool:
    """Whether a neighbour rule's window (NEIGHBOUR_CHARS either side) reaches past this string's edge. The canary has
    always read the body's strings joined by line breaks, so a corroborating word in the next string counted; a
    string alone cannot say what that word would have been."""
    return bool((rule.followed_by and end + NEIGHBOUR_CHARS > length)
                or (rule.preceded_by and start < NEIGHBOUR_CHARS))


def unit_canary(mapper: Mapper, text: str) -> tuple[tuple[str, ...], tuple[tuple[str, str, str], ...], bool]:
    """The canary over one string, as `transform.canary` and `transform.rule_declined` read it: (topics found as
    written or once its disguises are off; (topic, text, declared real) for each match a rule declined; whether a
    neighbour rule's answer here depends on the strings either side)."""
    found: set[str] = set()
    declined: list[tuple[str, str, str]] = []
    edge = False
    ruled = any(f.match is not None for f in mapper.forms)
    for m in mapper._canary_matches(text):
        form = mapper._canary_form(m.group(0))
        if form is None:
            continue
        if _declined(form, m):
            if ruled:
                declined.append((form.client, m.group(0), form.real))
        else:
            found.add(form.client)
        if form.match is not None and _near_an_edge(form.match, m.start(), m.end(), len(text)):
            edge = True
    plain = normalised(text)
    if plain != text:
        for m in mapper._canary_matches(plain):
            form = mapper._canary_form(m.group(0))
            if form is None:
                continue
            if not _declined(form, m):
                found.add(form.client)
            if form.match is not None and _near_an_edge(form.match, m.start(), m.end(), len(plain)):
                edge = True
    return tuple(sorted(found)), tuple(declined), edge


def run_unit(kind: str, mapper: Mapper | None, item: Any) -> Any:
    """One unit of `kind`, in this process or a worker's: t (tokenise; item is (mode, text)), d (detection), r
    (secrets), f (block mode's families), c (the canary)."""
    if kind == "t":
        return unit_tokenise(mapper, item[0], item[1])
    if kind == "d":
        return unit_findings(item, mapper)
    if kind == "r":
        return unit_redact(item)
    if kind == "f":
        return unit_families(item)
    if kind == "c":
        return unit_canary(mapper, item)
    raise ValueError(f"unknown unit kind {kind!r}")


# ---- the detection text, cut into units -------------------------------------------------------------------------

class _Unsupported(Exception):
    """A document json.dumps would write differently from the pieces below (a key that is not a string)."""


_encode_string = json.encoder.encode_basestring        # what json.dumps uses with ensure_ascii=False


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return _encode_string(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return json.dumps(value)
    raise _Unsupported(type(value).__name__)


def detection_pieces(obj: Any) -> list[str] | None:
    """`transform.detection_text(obj)` cut into units, or None when it cannot be (the caller then reads the text whole).

    A cut is made only after a ", " that separates two items of a list or two keys of an object that holds another
    object or list - never inside an object whose values are all plain (a content block with its keys), and never
    inside a string. So every key/value pair the detectors read together (`"tenantId": "<guid>"`) is in one piece, and
    no detector's match can cross a cut: none of them matches a comma, and a piece starts after a space, which every
    detector reads as the start of the text. The pieces joined are exactly the text; the caller checks that."""
    pieces: list[str] = []
    buf: list[str] = []

    def cut() -> None:
        pieces.append(_ESCAPE.sub(_escape_as_detected, "".join(buf)))
        buf.clear()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            schema_node = _is_schema_node(node)
            leaf = not any(isinstance(v, (dict, list)) for v in node.values())
            buf.append("{")
            first = True
            for key, value in node.items():
                if not isinstance(key, str):
                    raise _Unsupported("key")
                if not first:
                    buf.append(", ")
                    if not leaf:
                        cut()
                first = False
                buf.append(_encode_string(key))
                buf.append(": ")
                # Blanked exactly as transform._without_opaque blanks them.
                if isinstance(value, str) and (_is_opaque(node, key) or _attachment_data(node, key, value)
                                               or (schema_node and key in _SCHEMA_KEYWORDS)):
                    buf.append('""')
                else:
                    walk(value)
            buf.append("}")
        elif isinstance(node, list):
            buf.append("[")
            for i, item in enumerate(node):
                if i:
                    buf.append(", ")
                    cut()
                walk(item)
            buf.append("]")
        else:
            buf.append(_scalar(node))

    try:
        walk(obj)
    except (_Unsupported, RecursionError):
        return None
    cut()
    return pieces


def _escape_as_detected(m: Any) -> str:
    """transform.detection_text's escape rule: an escaped backslash kept, a line break or tab made a space."""
    return m.group(0) if m.group(0) == "\\\\" else " "


# ---- the remembered results -------------------------------------------------------------------------------------

class UnitCache:
    """Results by (kind, generation, SHA-256 of the unit), least recently used dropped first. In memory only: the
    units are client data (Brad 2026-11-10: never on disk)."""

    def __init__(self, max_bytes: int = CACHE_BYTES, max_entries: int = CACHE_ENTRIES) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._items: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Any:
        with self._lock:
            found = self._items.get(key)
            if found is None:
                return None
            self._items.move_to_end(key)
            return found[0]

    def put(self, key: tuple, value: Any, size: int) -> None:
        size += 120                                   # the key, the tuple and the dictionary's own share
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            self._items[key] = (value, size)
            self._bytes += size
            while self._items and (self._bytes > self.max_bytes or len(self._items) > self.max_entries):
                _key, (_value, gone) = self._items.popitem(last=False)
                self._bytes -= gone

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def bytes(self) -> int:
        return self._bytes


def _size(value: Any) -> int:
    """Roughly the bytes a remembered result holds."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value) * 2
    if isinstance(value, (tuple, list, frozenset, set)):
        return 16 + sum(_size(v) + 8 for v in value)
    if isinstance(value, detect.UnitFindings):
        return value.size()
    return 16


# ---- the generation ---------------------------------------------------------------------------------------------

def _ignore_print(mapper: Mapper) -> str:
    """The ignore list's fingerprint, worked out once per list."""
    ignore = getattr(mapper, "ignore", None) or set()
    memo = getattr(mapper, "_ignore_print", None)
    if memo is not None and memo[0] is ignore and memo[1] == len(ignore):
        return memo[2]
    printed = hashlib.sha256("\n".join(sorted(ignore)).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    mapper._ignore_print = (ignore, len(ignore), printed)
    return printed


def generation(mapper: Mapper | None, settings: str) -> str:
    """The filter generation for this mapper and these settings (see the module's docstring). Detection's acronym
    list is settled for this mapper first: it is a list the detectors share, and it grows as mappings are built."""
    if mapper is None:
        return "nomapper:" + settings
    detect._needles(mapper)
    parts = (mapper.serial, len(mapper._learned), len(detect.ACRONYMS), _ignore_print(mapper),
             tuple(sorted((getattr(mapper, "email_rules", None) or {}).items())), settings)
    return hashlib.sha256(repr(parts).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def secrets_print(user_secrets: Any) -> str:
    """Your secrets list's fingerprint: its pattern and labels as they are now."""
    pattern = getattr(user_secrets, "_pattern", None) if user_secrets is not None else None
    labels = sorted((getattr(user_secrets, "_labels", None) or {}).items()) if user_secrets is not None else []
    return hashlib.sha256(repr((pattern.pattern if pattern is not None else "", labels))
                          .encode("utf-8", "surrogatepass")).hexdigest()[:16]


def settings_print(cfg: Any, user_secrets: Any = None) -> str:
    """The settings that change a result (spec part 2): secrets mode and your secrets list, the held-back kinds,
    near-miss repair, what each kind does, auto-add, and the tools kept tokenised. Your secrets' redaction is keyed
    on the list as it is when that pass runs (FilterRun._redact_user), not on this print: the list can be reloaded
    later in the same request."""
    parts = (getattr(cfg, "secrets_mode", ""), secrets_print(user_secrets), tuple(getattr(cfg, "placeholder_kinds", ()) or ()),
             bool(getattr(cfg, "near_miss", False)), tuple(getattr(cfg, "quiet_kinds", ()) or ()),
             tuple(getattr(cfg, "owner_add_kinds", ()) or ()), tuple(getattr(cfg, "ignore_add_kinds", ()) or ()),
             bool(getattr(cfg, "auto_map_client_resources", False)), bool(getattr(cfg, "detect_enabled", False)),
             tuple(getattr(cfg, "keep_tokenised_tools", ()) or ()))
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:16]


def mapper_payload(mapper: Mapper) -> dict[str, Any]:
    """What a worker process needs to filter as this mapper does: built once per generation and sent once to each
    worker, not per request. Forms rather than the Mapper: its escaping styles are functions, which do not travel."""
    return {"forms": list(mapper.forms), "learned": list(mapper._learned.items()),
            "ignore": set(getattr(mapper, "ignore", None) or set()),
            "email_rules": dict(getattr(mapper, "email_rules", None) or {}),
            "acronyms": sorted(detect.ACRONYMS)}


def mapper_from_payload(payload: dict[str, Any]) -> Mapper:
    """The mapper a worker filters with: the same forms in the same order, the variants learned so far in the order
    they were learned, and `learning` off (a new variant raises VariantNeeded and the string is done again in the
    proxy, in document order)."""
    mapper = Mapper(payload["forms"])
    mapper.learning = False
    mapper.ignore = payload["ignore"]
    mapper.email_rules = payload["email_rules"]
    for text, form in payload["learned"]:
        mapper._learned[text] = form
        mapper._rev.setdefault(form.token, form)
        mapper._rev_fold.setdefault(form.token.lower(), form)
        mapper._rev_dirty = True
    detect.ACRONYMS.update(payload["acronyms"])
    return mapper


# ---- one request ------------------------------------------------------------------------------------------------

class FilterEngine:
    """The remembered results and the worker pool, one per proxy; `begin` starts a request's FilterRun."""

    def __init__(self, cache: UnitCache | None = None) -> None:
        self.cache = cache or UnitCache()
        self.pool: Any = None
        self._pool_lock = threading.Lock()
        self._closed = False

    def start_pool(self, mapper: Mapper | None = None, settings: str = "") -> None:
        """The worker processes (spec part 4), started with AI Cleaner and kept; warmed with the current mapping so
        the first request after a start does not wait for the workers to build it."""
        from .filterpool import FilterPool

        with self._pool_lock:
            if self._closed:
                return
            if self.pool is None:
                self.pool = FilterPool()
            pool = self.pool
        pool.start()
        with self._pool_lock:
            closed = self._closed
        if closed:                       # the proxy stopped while the workers were starting
            pool.close()
            return
        if mapper is not None and pool.usable:
            key = generation(mapper, settings)
            pool.warm(key, lambda: mapper_payload(mapper))

    def close(self) -> None:
        with self._pool_lock:
            self._closed = True
            pool, self.pool = self.pool, None
        if pool is not None:
            pool.close()
        self.cache.clear()

    def begin(self, incremental: bool, shadow: bool, settings: str,
              cancel: threading.Event | None = None) -> "FilterRun":
        return FilterRun(self, incremental, shadow, settings, cancel)


class FilterAbandoned(Exception):
    """The client went away while its request was being filtered (coordinator, 2026-11-10): the work stops between
    units and passes rather than running on for minutes for nobody. What was worked out per unit stays remembered."""


class FilterRun:
    """The passes for one request, and what they did, for the upstream monitor (spec part 5).

    Each pass returns exactly what the whole-body function it replaces returns. With `incremental` off they are those
    functions; with `shadow` on both run, the old result is used, and a difference is logged as sizes and kinds.
    `cancel`, when set, stops the run at the next check (FilterAbandoned)."""

    def __init__(self, engine: FilterEngine, incremental: bool, shadow: bool, settings: str,
                 cancel: threading.Event | None = None) -> None:
        self.engine = engine
        self.cancel = cancel
        self.cache = engine.cache
        self.incremental = incremental
        self.shadow = shadow
        self.settings = settings
        self.units = {"total": 0, "cached": 0, "filtered": 0}
        self.ms: dict[str, float] = {}
        self.pool_used = False
        self.generation = ""
        self.differences: list[str] = []
        self.whole: list[str] = []        # passes that read the text whole this time, and why
        self.abandoned = False

    # -- bookkeeping --

    def summary(self) -> dict[str, Any]:
        """The upstream line's `filter` field: no text, only counts, times and the generation."""
        units = {"total": self.units["total"], "cached": max(0, self.units["total"] - self.units["filtered"]),
                 "filtered": self.units["filtered"]}
        out: dict[str, Any] = {"mode": "shadow" if self.shadow else "incremental" if self.incremental else "whole",
                               "units": units, "ms": {k: round(v, 1) for k, v in self.ms.items()},
                               "pool": self.pool_used, "generation": self.generation}
        if self.differences:
            out["shadow_differences"] = list(self.differences)
        if self.whole:
            out["read_whole"] = list(self.whole)
        if self.abandoned:
            out["abandoned"] = True        # the client went away; the passes stopped where `ms` ends
        return out

    def _timed(self, name: str, started: float) -> None:
        self.ms[name] = self.ms.get(name, 0.0) + (time.perf_counter() - started) * 1000

    def check(self) -> None:
        """Stop here if the client has gone (FilterAbandoned); called between passes and between units."""
        if self.cancel is not None and self.cancel.is_set():
            self.abandoned = True
            raise FilterAbandoned()

    def _gen(self, mapper: Mapper | None) -> str:
        key = generation(mapper, self.settings)
        if mapper is not None and not self.generation:
            self.generation = key
        return key

    def _differs(self, name: str, detail: str) -> None:
        """A shadow difference: logged with sizes and kinds only, never text (spec, Proving it)."""
        self.differences.append(name)
        log.warning("filter shadow: the incremental %s differs from the whole-body one (%s)", name, detail)

    # -- the work, from the cache, the pool or this thread --

    def _resolve(self, kind: str, gen: str, mapper: Mapper | None, items: list[tuple[bytes, Any, int]]
                 ) -> dict[bytes, Any]:
        """Results for units not remembered: (digest, work item, size) each. Handed to the worker processes when
        there is enough new text to be worth it and the pool is free; a unit they could not do (a dead worker, a case
        variant still to learn) is left out, and the caller does it here, at its place in the document."""
        out: dict[bytes, Any] = {}
        pool = self.engine.pool
        if pool is None or not pool.usable:
            return out
        missing: list[tuple[bytes, Any, int]] = []
        seen: set[bytes] = set()
        for key, item, size in items:
            if key in seen:
                continue
            seen.add(key)
            if self.cache.get((kind, gen, key)) is None:
                missing.append((key, item, size))
        if sum(size for _k, _i, size in missing) < POOL_MIN_BYTES:
            return out
        results = pool.run(kind, gen if mapper is not None else None,
                           (lambda: mapper_payload(mapper)) if mapper is not None else None,
                           [item for _k, item, _s in missing], cancel=self.cancel)
        self.check()
        if results is None:
            return out
        self.pool_used = True
        for (key, _item, _bytes), result in zip(missing, results):
            if result is not None and not _is_redo(result):
                out[key] = result
                self.units["filtered"] += 1
                if _rememberable(kind, result):
                    self.cache.put((kind, gen, key), result, _size(result))
        return out

    def _compute(self, kind: str, gen: str, mapper: Mapper | None, key: bytes, item: Any,
                 local: dict[bytes, Any]) -> Any:
        """A unit's result: from this request's results, the cache, or worked out now (and remembered). Counted for
        the monitor: every unit once in `total`, and in `filtered` when it was worked out this request."""
        self.units["total"] += 1
        found = local.get(key)
        if found is not None:
            return found
        found = self.cache.get((kind, gen, key))
        if found is not None:
            local[key] = found
            return found
        self.check()
        result = run_unit(kind, mapper, item)
        self.units["filtered"] += 1
        local[key] = result
        if _rememberable(kind, result):
            self.cache.put((kind, gen, key), result, _size(result))
        return result

    # -- the passes --

    def tokenise_json(self, mapper: Mapper, obj: Any, counter: Counter[str]) -> Any:
        """transform.tokenise_json(mapper, obj, counter)."""
        started = time.perf_counter()
        try:
            if not self.incremental and not self.shadow:
                return tokenise_json(mapper, obj, counter)
            if self.shadow:
                old_counts = _Counts()
                old = tokenise_json(mapper, obj, old_counts)
                new_counts = _Counts()
                new = self._tokenise(mapper, obj, new_counts)
                if new != old or new_counts != old_counts or new_counts.pairs != old_counts.pairs:
                    self._differs("tokenising", f"old {len(json.dumps(old))} bytes, {sum(old_counts.values())} "
                                                f"replaced; new {len(json.dumps(new))} bytes, "
                                                f"{sum(new_counts.values())} replaced")
                _add(counter, tuple(old_counts.items()), tuple(old_counts.pairs.items()))
                return old
            return self._tokenise(mapper, obj, counter)
        finally:
            self._timed("tokenise", started)

    def _tokenise(self, mapper: Mapper, obj: Any, counter: Counter[str]) -> Any:
        gen = self._gen(mapper)
        local: dict[bytes, Any] = {}
        keys: dict[tuple[str, str], bytes] = {}

        def key_of(mode: str, text: str) -> bytes:
            found = keys.get((mode, text))
            if found is None:
                found = keys[(mode, text)] = digest(mode + text)
            return found

        if self.engine.pool is not None and self.engine.pool.usable:
            # The strings in walk order, for the pool to take what is new in one go; the walk below then puts each
            # result in its place, and does here (in order) any a worker left - a case variant to learn.
            wanted: list[tuple[bytes, Any, int]] = []

            def note(mode: str) -> Callable[[str], str]:
                def seen(text: str) -> str:
                    wanted.append((key_of(mode, text), (mode, text), len(text)))
                    return text
                return seen

            walk_strings(obj, note("t"), note("j"), keys=True)
            local.update(self._resolve("t", gen, mapper, wanted))

        def through(mode: str) -> Callable[[str], str]:
            def fn(text: str) -> str:
                out, counts, pairs = self._compute("t", gen, mapper, key_of(mode, text), (mode, text), local)
                _add(counter, counts, pairs)
                return text if out is None else out
            return fn

        return walk_strings(obj, through("t"), through("j"), keys=True)

    def detect(self, mapper: Mapper, obj: Any) -> tuple[list[tuple[str, str, str, str]], str]:
        """(list(find_candidates(text, mapper)), text) with text = detection_text(obj)."""
        started = time.perf_counter()
        try:
            text = detection_text(obj)
            if not self.incremental and not self.shadow:
                return list(detect.find_candidates(text, mapper)), text
            if self.shadow:
                old = list(detect.find_candidates(text, mapper))
                new = self._detect(mapper, obj, text)
                if new != old:
                    self._differs("detection", f"old {len(old)} candidates of {_kinds(old)}; "
                                               f"new {len(new)} of {_kinds(new)}")
                return old, text
            return self._detect(mapper, obj, text), text
        finally:
            self._timed("detect", started)

    def _units_of(self, obj: Any, text: str, why: str) -> list[tuple[int, str]] | None:
        """(offset, piece) for the detection text, or None to read it whole (said in the monitor line)."""
        pieces = detection_pieces(obj)
        if pieces is None or "".join(pieces) != text:
            self.whole.append(why)
            log.info("incremental filtering read the %s text whole: it could not be cut into units", why)
            return None
        out: list[tuple[int, str]] = []
        offset = 0
        for piece in pieces:
            out.append((offset, piece))
            offset += len(piece)
        return out

    def _detect(self, mapper: Mapper, obj: Any, text: str) -> list[tuple[str, str, str, str]]:
        units = self._units_of(obj, text, "detection")
        if units is None:
            return list(detect.find_candidates(text, mapper))
        gen = self._gen(mapper)
        wanted = [(digest(piece), piece, len(piece)) for _offset, piece in units]
        local = self._resolve("d", gen, mapper, wanted)
        found = [(offset, self._compute("d", gen, mapper, key, piece, local))
                 for (offset, piece), (key, _p, _s) in zip(units, wanted)]
        return list(kept_candidates(merged_findings(found, text), mapper))

    def families(self, obj: Any) -> list[str]:
        """find_families(detection_text(obj)), for block mode."""
        started = time.perf_counter()
        try:
            text = detection_text(obj)
            if not self.incremental and not self.shadow:
                return find_families(text)
            if self.shadow:
                old = find_families(text)
                new = self._families(obj, text)
                if new != old:
                    self._differs("secret detection", f"old {len(old)} families, new {len(new)}")
                return old
            return self._families(obj, text)
        finally:
            self._timed("secrets", started)

    def _families(self, obj: Any, text: str) -> list[str]:
        units = self._units_of(obj, text, "secrets")
        if units is None:
            return find_families(text)
        gen = self._gen(None)
        wanted = [(digest(piece), piece, len(piece)) for _offset, piece in units]
        local = self._resolve("f", gen, None, wanted)
        found: set[str] = set()
        for (_offset, piece), (key, _p, _s) in zip(units, wanted):
            found |= self._compute("f", gen, None, key, piece, local)
        # BEGIN and END of a private key can sit in two strings: that one family is searched over the whole text.
        found.update(family for family, pattern in PATTERNS if family == SPANNING_FAMILY and pattern.search(text))
        return sorted(found)

    def redact(self, obj: Any, counter: Counter[str]) -> Any:
        """walk_strings(obj, lambda t: redact(t, counter))."""
        started = time.perf_counter()
        try:
            if not self.incremental and not self.shadow:
                return walk_strings(obj, lambda t: redact(t, counter))
            if self.shadow:
                old_counts: Counter[str] = Counter()
                old = walk_strings(obj, lambda t: redact(t, old_counts))
                new_counts: Counter[str] = Counter()
                new = self._redact(obj, new_counts)
                if new != old or new_counts != old_counts:
                    self._differs("secret redaction", f"old {sum(old_counts.values())} redacted, "
                                                      f"new {sum(new_counts.values())}")
                counter.update(old_counts)
                return old
            return self._redact(obj, counter)
        finally:
            self._timed("secrets", started)

    def _redact(self, obj: Any, counter: Counter[str]) -> Any:
        gen = self._gen(None)
        keys: dict[str, bytes] = {}
        local: dict[bytes, Any] = {}
        if self.engine.pool is not None and self.engine.pool.usable:
            wanted: list[tuple[bytes, Any, int]] = []

            def note(text: str) -> str:
                key = keys.get(text)
                if key is None:
                    key = keys[text] = digest(text)
                wanted.append((key, text, len(text)))
                return text

            walk_strings(obj, note)
            local.update(self._resolve("r", gen, None, wanted))

        def fn(text: str) -> str:
            key = keys.get(text)
            if key is None:
                key = keys[text] = digest(text)
            out, counts = self._compute("r", gen, None, key, text, local)
            for family, n in counts:
                counter[family] += n
            return text if out is None else out

        return walk_strings(obj, fn)

    def redact_user(self, user_secrets: Any, obj: Any, counter: Counter[str]) -> Any:
        """walk_strings(obj, lambda t: user_secrets.redact(t, counter)). Your own secrets never go to a worker
        process: their pattern is one alternation of literals, cheap here, and remembered per string."""
        started = time.perf_counter()
        try:
            if not self.incremental and not self.shadow:
                return walk_strings(obj, lambda t: user_secrets.redact(t, counter))
            if self.shadow:
                old_counts: Counter[str] = Counter()
                old = walk_strings(obj, lambda t: user_secrets.redact(t, old_counts))
                new_counts: Counter[str] = Counter()
                new = self._redact_user(user_secrets, obj, new_counts)
                if new != old or new_counts != old_counts:
                    self._differs("your secrets", f"old {sum(old_counts.values())} redacted, "
                                                  f"new {sum(new_counts.values())}")
                counter.update(old_counts)
                return old
            return self._redact_user(user_secrets, obj, counter)
        finally:
            self._timed("secrets", started)

    def _redact_user(self, user_secrets: Any, obj: Any, counter: Counter[str]) -> Any:
        # Keyed on the list as it is NOW: the proxy reloads it (maybe_reload) after this run's settings print was
        # taken, and a result remembered under the old list must not be used for the new one.
        gen = self._gen(None) + ":" + secrets_print(user_secrets)
        local: dict[bytes, Any] = {}

        def fn(text: str) -> str:
            key = digest(text)
            found = local.get(key) or self.cache.get(("u", gen, key))
            self.units["total"] += 1
            if found is None:
                counts: Counter[str] = Counter()
                out = user_secrets.redact(text, counts)
                found = ((None if out == text else out), tuple(counts.items()))
                self.units["filtered"] += 1
                self.cache.put(("u", gen, key), found, _size(found))
            local[key] = found
            for label, n in found[1]:
                counter[label] += n
            return text if found[0] is None else found[0]

        return walk_strings(obj, fn)

    def canary(self, mapper: Mapper, body: bytes) -> tuple[list[str], list[tuple[str, str, str]]]:
        """(canary(mapper, body), rule_declined(mapper, body)).

        Incremental and still the last check (spec part 3): it reads the body as sent - these bytes, parsed again -
        one string at a time, and the only strings it skips are ones the canary itself cleared, under the same
        generation, in an earlier request. The filter's own verdict on a string is never used, so a string the
        filter got wrong is a new string here and is scanned. A refusal is never remembered. Where a neighbour
        rule's answer depends on the strings either side, the body is read whole, as before."""
        started = time.perf_counter()
        try:
            if not self.incremental and not self.shadow:
                return canary(mapper, body), rule_declined(mapper, body)
            if self.shadow:
                old = (canary(mapper, body), rule_declined(mapper, body))
                new = self._canary(mapper, body)
                if new != old:
                    self._differs("canary", f"old {len(old[0])} topics, {len(old[1])} declined; "
                                            f"new {len(new[0])} topics, {len(new[1])} declined")
                return old
            return self._canary(mapper, body)
        finally:
            self._timed("canary", started)

    def _canary(self, mapper: Mapper, body: bytes) -> tuple[list[str], list[tuple[str, str, str]]]:
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.whole.append("canary")
            return canary(mapper, body), rule_declined(mapper, body)
        strings = list(readable_strings(obj))
        gen = self._gen(mapper)
        keys: dict[str, bytes] = {}
        wanted: list[tuple[bytes, Any, int]] = []
        for text in strings:
            key = keys.get(text)
            if key is None:
                key = keys[text] = digest(text)
            wanted.append((key, text, len(text)))
        local = self._resolve("c", gen, mapper, wanted)
        verdicts = [self._compute("c", gen, mapper, key, text, local) for key, text, _size in wanted]
        if any(edge for _found, _declined, edge in verdicts):
            self.whole.append("canary (a neighbour rule at a string's edge)")
            return canary(mapper, body), rule_declined(mapper, body)
        leaked = sorted({topic for found, _declined, _edge in verdicts for topic in found})
        declined = [entry for _found, declined, _edge in verdicts for entry in declined]
        return leaked, declined


def _is_redo(result: Any) -> bool:
    return isinstance(result, str) and result == REDO


def _rememberable(kind: str, result: Any) -> bool:
    """Everything is remembered except a canary verdict that found something, or that depends on the strings
    either side: a refusal is scanned again on the next request (spec part 3)."""
    return kind != "c" or (not result[0] and not result[2])


# What a worker returns for a string that needs a case variant learned (mapping.VariantNeeded).
REDO = "\x00redo"


def _add(counter: Counter[str], counts: Iterable[tuple[Any, int]], pairs: Iterable[tuple[Any, int]]) -> None:
    """A unit's counts added to the request's, the pairs too where the counter keeps them (evidence.SubCounter)."""
    for key, n in counts:
        counter[key] += n
    kept = getattr(counter, "pairs", None)
    if kept is not None:
        for key, n in pairs:
            kept[key] += n


def _kinds(found: list[tuple[str, str, str, str]]) -> str:
    """The kinds in a candidate list, counted: what a shadow difference says instead of the values."""
    counted = Counter(kind for _v, kind, _c, _o in found)
    return ", ".join(f"{kind} {n}" for kind, n in sorted(counted.items())) or "none"
