"""Client mapping: load client-map.json, validate it, compile two-way substitution.

The mapping is a deterministic bijection between real client identifiers and
tokens. Forms are matched longest-first so "ACME Inc" wins over "ACME".
Matching ignores case: an undeclared variant of a form is matched too, and is given its own token
so the case that was typed is the case that comes back (see variant_token). "expand_case" still
declares the four common shapes up front, which is what teammates sharing the file get.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

# Tokens must survive shells, HCL, URLs and JSON without quoting surprises.
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]*$")


GLOBAL_ID = "global"  # the user's own filters (e.g. their employer Contoso, vendors they use); tokens Org00 ...; never shared

# A placeholder's form is tagged with a client id that is not a client: "pending:<kind>", so the
# evidence log can count what was held back per kind without inventing a second mechanism. It
# keeps that simple and leaves a trap - anything iterating clients has to know to skip it, and
# the next thing to iterate them might not (D11). So the prefix has one definition and one test,
# rather than being a string literal spelled out wherever somebody remembered.
PENDING_PREFIX = "pending:"

# How many undeclared case variants one mapper will learn. Each costs a Form, two reverse-table
# entries and a place in the reverse pattern, and nothing evicts them - see Mapper.forward for
# why eviction is the wrong tool and what happens past the ceiling instead. High enough that no
# ordinary session reaches it; low enough that a runaway one cannot grow without limit.
MAX_LEARNED_VARIANTS = 5000


def is_real_client(cid: str) -> bool:
    """True for an engagement. False for Global, and for the pending-placeholder pseudo-client."""
    low = (cid or "").lower()
    return bool(low) and low != GLOBAL_ID and not low.startswith(PENDING_PREFIX)


class MappingError(ValueError):
    """Raised when client-map.json is malformed or unsafe.

    A refusal a person actually meets in the UI needs more than the sentence that names the
    rule it broke. `why` says what the rule is protecting, and `offer` names an action the UI
    can put behind a button - a string cannot carry an action, so the structure has to live
    here rather than in the message.

    `offer` is declarative on purpose: `{"action": ..., "label": ..., plus whatever values the
    action needs}`. The store does not know what a route is, so it names the thing to do, and
    the page maps it to the call it already makes. Both fields are optional and default to
    empty, so every bare MappingError("...") in the codebase behaves exactly as before.
    """

    def __init__(self, message: str, why: str = "", offer: dict[str, str] | None = None):
        super().__init__(message)
        self.why = why
        self.offer = offer or {}


GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", re.IGNORECASE)


def client_index(client_id: str, explicit: int | None = None) -> int:
    """Numeric index for a client.

    explicit "index" wins; else trailing digits of the id (CLIENT04 -> 4, legacy); else a
    4-digit hash of the lower-cased id, so every machine that names a client "fabrikam"
    derives the same index and therefore the same tokens with no coordination.
    """
    if explicit is not None:
        return int(explicit)
    m = re.search(r"(\d+)$", client_id)
    if m:
        return int(m.group(1))
    if not client_id.strip():
        raise MappingError("client id is empty")
    return 1000 + int(hashlib.sha1(client_id.strip().lower().encode()).hexdigest()[:4], 16) % 9000


# The shapes token_for knows how to build. A form may declare one to override what classify()
# guesses from the text, because the guess being wrong makes the token meaningless rather than
# merely untidy: a profile URL read as a name becomes "Org00".
TOKEN_SHAPES = ("guid", "ip", "email", "tenant", "sharepoint", "domain", "slug", "name")

IPV4_RE = re.compile(r"^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$")


def is_ipv6(value: str) -> bool:
    """A literal IPv6 address (no zone, no brackets, no prefix length)."""
    if ":" not in value or not re.fullmatch(r"[0-9A-Fa-f:.]+", value):
        return False
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


# IPv6 tokens and stand-ins live in the documentation prefix (RFC 3849): never routed, and the
# detector's identifies_somebody() already says no to it, so our own output is never re-detected
# (DECISIONS 41). Tokens take a client's index; stand-ins take the ffff block no index can reach.
IPV6_DOC_PREFIX = "2001:db8"
IPV6_STAND_IN_BLOCK = 0xFFFF


def ipv6_token(index: int, hx: str) -> str:
    """2001:db8:<index>:<hash>::<hash> - address-shaped, stable, canonical (compressed) text.

    A client index lands in 0..fffe; fffe is the highest, so fffe and ffff never meet and the
    stand-in block (IPV6_STAND_IN_BLOCK = ffff) is reached only when asked for by name."""
    block = index if index == IPV6_STAND_IN_BLOCK else index % IPV6_STAND_IN_BLOCK
    groups = [0x2001, 0xDB8, block, int(hx[0:4], 16), 0, 0, 0, int(hx[4:8], 16) or 1]
    return ipaddress.IPv6Address(":".join(f"{g:x}" for g in groups)).compressed


def classify(real: str) -> str:
    """Shape of an identifier: guid | ip | email | tenant | sharepoint | domain | slug | name."""
    r = real.strip()
    if GUID_RE.match(r):
        return "guid"
    if IPV4_RE.match(r) or is_ipv6(r):
        return "ip"
    if EMAIL_RE.match(r):
        return "email"
    low = r.lower()
    if low.endswith(".onmicrosoft.com"):
        return "tenant"
    if low.endswith(".sharepoint.com"):
        return "sharepoint"
    if DOMAIN_RE.match(r) and "." in r:
        return "domain"
    if " " in r or any(ch.isupper() for ch in r[1:]) and r[0].isupper():
        return "name"
    if re.fullmatch(r"[a-z0-9]+([_-][a-z0-9]+)*", r):
        return "slug"
    return "name"


def token_for(real: str, index: int, kind: str | None = None, prefix: str = "Client") -> str:
    """Derive a token from the identifier's shape so different shapes never collide.

    name      ACME Inc              -> Client05_Pty      (Client05 alone for a single word)
    slug      acme-inc / acmeinc    -> client05-pty / client05pty (separator style preserved)
    domain    acme.com.au           -> client05.invalid
    tenant    acmeinc.onmicrosoft.com -> client05.onmicrosoft.invalid
    sharepoint acmeinc.sharepoint.com -> client05.sharepoint.invalid
    email     jane@acme.com.au      -> user05a1b2@client05.invalid
    guid      1f3e...               -> 00000000-0000-4000-8000-0000NN + 6 hash hex (NN = index)
    """
    kind = kind or classify(real)
    tag = f"{prefix.lower()}{index:02d}"
    # GUIDs and emails recur within a client (tenant, several subscriptions, several people),
    # so a short hash keeps each one distinct while the client index stays visible.
    h = hashlib.sha1(real.lower().encode()).hexdigest()
    if kind == "guid":
        return f"00000000-0000-4000-8000-{index:06d}{h[:6]}"
    if kind == "ip":
        if is_ipv6(real):
            return ipv6_token(index, h)
        # 198.18.0.0/15 is reserved for benchmarking (RFC 2544): never routable, never a real host.
        return f"198.{18 + (index % 2)}.{int(h[:2], 16)}.{int(h[2:4], 16)}"
    if kind == "tenant":
        return f"{tag}.onmicrosoft.invalid"
    if kind == "sharepoint":
        return f"{tag}.sharepoint.invalid"
    if kind == "domain":
        return f"{tag}.invalid"
    if kind == "email":
        return f"user{index:02d}{h[:4]}@{tag}.invalid"
    if kind == "slug":
        sep = "-" if "-" in real else ("_" if "_" in real else "")
        return f"{tag}{sep}pty" if len(real) > 5 else tag
    # name: keep the word count so multi-word names stay multi-word - joined into one identifier (Brad,
    # 2026-10-23), as hash-suffixed name tokens have been since 2026-10-15: "Client05 Pty" was read as a name and a
    # qualifier, and a token written in two pieces is one no reverse table knows.
    words = real.split()
    if len(words) == 1:
        return f"{prefix}{index:02d}"
    return "_".join([f"{prefix}{index:02d}", *["Pty", "Ltd", "Group", "Co"][: len(words) - 1]])


# How long a shape-preserving token may get before the shape is abandoned (D7, decided
# 2026-09-27). 24 is the tightest common Azure limit - a storage account and a key vault both
# stop there - and the name itself never says which resource it is, so this is a stopgap until a
# form can declare what it is (feature 23). It applies only where the real was itself within the
# ceiling: a name already past 24 is plainly not one of the resources that enforces it.
SHAPED_TOKEN_CEILING = 24


def embedded_token(run: str, real: str, index: int, prefix: str = "Client") -> str:
    """Token for a resource name that carries a client form: the form is replaced by the client's
    tag and the rest of the name is kept, so the type, region and environment stay readable.

        kvaueprdtailwindmgmt + form "tailwind" -> kvaueprdclient05management
        TailwindOpsAudit           + form "Tailwind" -> Client05OpsAudit

    Identity goes, the shape the model needs to work with the estate stays. Case follows the run.
    """
    at = run.lower().find(real.lower())
    if at < 0:
        raise MappingError(f"{real!r} is not inside {run!r}")
    matched = run[at:at + len(real)]
    tag = f"{prefix.lower()}{index:02d}"
    if matched.isupper():
        tag = tag.upper()
    elif matched[:1].isupper():
        tag = tag.capitalize()
    return run[:at] + tag + run[at + len(real):]


def _suffixed(real: str, index: int, prefix: str, kind: str, n: int) -> str:
    """The plain token made distinct with n hex characters from the real's own hash."""
    hx = hashlib.sha1(real.lower().encode()).hexdigest()
    h = hx[:n]
    tag = f"{prefix.lower()}{index:02d}"
    plain = token_for(real, index, kind, prefix)
    if kind in ("guid", "email", "ip"):
        # Already hash-bearing; the n=4 form equals the plain token, n=8 draws on later digits.
        if n <= 4:
            return plain
        if kind == "guid":
            return f"00000000-0000-4000-8000-{index:06d}{hx[6:12]}"
        if kind == "email":
            return f"user{index:02d}{h}@{tag}.invalid"
        if is_ipv6(real):
            return ipv6_token(index, hx[n:n + 8])
        return f"198.{18 + (index % 2)}.{int(hx[4:6], 16)}.{int(hx[6:8], 16)}"
    if kind == "domain":
        return f"{tag}-{h}.invalid"
    if kind == "tenant":
        return f"{tag}-{h}.onmicrosoft.invalid"
    if kind == "sharepoint":
        return f"{tag}-{h}.sharepoint.invalid"
    if kind == "slug":
        sep = "-" if "-" in real else ("_" if "_" in real else "")
        return f"{plain}{sep}{h}"
    # Joined with underscores into one identifier (the user, 2026-10-15). "Client05 Pty 1a2b" read
    # as a name and a qualifier, so the model wrote the hex part on its own ("the 1a2b docs") - a
    # piece no reverse table knows, which went out and into files as a literal. Still this real's own
    # hash, so the token is the same on every machine whatever order the names were met in.
    return joined_suffix(plain, h)


def joined_suffix(token: str, suffix: str) -> str:
    """A name token with a hash suffix, as one identifier: words and suffix joined by underscores
    (Client05_Pty_1a2b), so the suffix is never read as a word of its own."""
    return "_".join(token.split() + [suffix])


# A name token whose suffix was minted after a space, before 2026-10-15: "Client05 1a2b",
# "Client05 Pty 9015 cdd6" (a case variant's second suffix). Rewritten with underscores by
# store.migrate_spaced_suffixes; the spaced spelling still comes back as the real, for text written
# before. The last word is always the hash, which no plain multi-word token ends in.
SPACED_SUFFIX_RE = re.compile(r"^(?:client|org)\d+(?: [a-z0-9]+)* [0-9a-f]{4}(?:[0-9a-f]{4})?$", re.IGNORECASE)
# The same token as it is now; its spaced and URL-encoded spellings are kept as aliases of it.
JOINED_SUFFIX_RE = re.compile(r"^(?:client|org)\d+(?:_[a-z0-9]+)*_[0-9a-f]{4}(?:[0-9a-f]{4})?$", re.IGNORECASE)
# A multi-word name token as token_for minted it before 2026-10-23 ("Client05 Pty Ltd"), and as it is now
# ("Client05_Pty_Ltd"). Rewritten by store.migrate_spaced_suffixes; the spaced spelling still comes back as the real.
SPACED_NAME_RE = re.compile(r"^(?:client|org)\d+(?: (?:pty|ltd|group|co))+$", re.IGNORECASE)
JOINED_NAME_RE = re.compile(r"^(?:client|org)\d+(?:_(?:pty|ltd|group|co))+$", re.IGNORECASE)


def email_rule_for(real: str, rules: Iterable[str]) -> str:
    """The email rule (a client's domain) covering an address - its domain or a parent of it - or ""
    (feature 96)."""
    m = EMAIL_RE.match(str(real).strip())
    if not m:
        return ""
    host = m.group(1).lower()
    for rule in rules:
        domain = str(rule).lower()
        if host == domain or host.endswith("." + domain):
            return domain
    return ""


def email_rules_of(clients: Iterable[dict]) -> dict[str, str]:
    """domain -> client id for every client document's email rules."""
    out: dict[str, str] = {}
    for client in clients:
        if isinstance(client, dict):
            for domain in client.get("email_rules") or []:
                out.setdefault(str(domain).lower(), str(client.get("id", "")))
    return out


def rule_email_token(real: str, index: int, prefix: str = "Client", taken: Iterable[str] = ()) -> str:
    """The token for an address an email rule adds (feature 96): eight hex digits of the address's
    own hash, so every machine meeting it - in whatever order - mints the same token and two copies
    of the client file merge without disagreeing. Four digits (token_for's) collide often enough
    across a few hundred addresses that the order met would decide which got the longer one."""
    digest = hashlib.sha1(real.strip().lower().encode()).hexdigest()
    tag = f"{prefix.lower()}{index:02d}"
    used = {t.lower() for t in taken}
    for n in (8, 12, 16):
        candidate = f"user{index:02d}{digest[:n]}@{tag}.invalid"
        if candidate.lower() not in used:
            return candidate
    raise MappingError(f"no free token for {real!r}")


def derive_token(real: str, index: int, prefix: str = "Client", taken: set[str] | None = None,
                 kind: str | None = None) -> str:
    """token_for's readable token, or, when a client already uses it, the same token with a short
    suffix from this real's own hash (review finding 3: a client's second domain, name or resource
    identifier used to collide and was refused, so it went out untokenised)."""
    kind = kind or classify(real)
    used = {t.lower() for t in (taken or ())}
    plain = token_for(real, index, kind, prefix)
    if plain.lower() not in used:
        return plain
    for n in (4, 8):
        candidate = _suffixed(real, index, prefix, kind, n)
        if candidate.lower() not in used:
            return candidate
    raise MappingError(f"no free token for {real!r}; give it an explicit token")


def client_tokens(client: dict) -> dict[str, str]:
    """real -> token for one client document: stored tokens as they are, the rest derived in file
    order so each avoids those before it. A case variant of an earlier real shares its token.

    A form may declare its own "kind", which overrides what classify() would guess. The guess is
    right nearly always and useless when it is wrong: "linkedin.com/in/janedoe" reads as a name,
    so it becomes "Org00", and a request to read a bio at Org00 means nothing to anybody. The shape
    of a token is what carries meaning past the filter, so being able to correct the shape is what
    fixes the meaning.
    """
    cid = str(client.get("id", ""))
    prefix = "Org" if cid == GLOBAL_ID else "Client"
    forms = [f for f in client.get("forms", []) or [] if isinstance(f, dict) and "real" in f]
    out: dict[str, str] = {}
    by_lower: dict[str, str] = {}
    for f in forms:
        if f.get("token"):
            real = str(f["real"])
            out[real] = str(f["token"])
            by_lower.setdefault(real.lower(), out[real])
    taken = {t.lower() for t in out.values()}
    index: int | None = None
    for f in forms:
        real = str(f["real"])
        if real in out:
            continue
        if real.lower() in by_lower:
            out[real] = by_lower[real.lower()]
            continue
        if index is None:
            index = client_index(cid, client.get("index"))
        tok = derive_token(real, index, prefix, taken, str(f.get("kind") or "") or None)
        out[real] = tok
        by_lower[real.lower()] = tok
        taken.add(tok.lower())
    return out


# A letter or digit in any script; underscores, hyphens, dots, spaces and brackets are breaks.
_WORD_CHAR = r"[^\W_]"
# Marks "a key ends at this node" in the prefix tree. Not a character, so it can never collide
# with one: every other node key is a single character from a form.
_END = None
# Runs long enough to be worth checking against the mapping for a misreading.
_WORD_RUN = re.compile(r"[A-Za-z0-9]{8,}")


def _json_escape(text: str) -> str:
    """How text appears inside a JSON string literal (quotes, backslashes, control characters)."""
    return json.dumps(text, ensure_ascii=False)[1:-1]


# The ways a name is written escaped, each as the function that writes it (bug report 2026-10-14).
# A domain in a regex (`fabrikam\.com`, or re.escape's `fabrikam\-prod\.com`), in PowerShell with a
# backtick before the dot, URL-encoded (`%2E`), or defanged (`[.]`) went out with the real name in
# it: the tokeniser matched only the plain spelling, and the canary decoded only %XX. Each spelling
# is now a key of its own and is replaced by the token written the same way, so a regex still works
# after the swap and comes back as the real name, escaped as it was sent.
DISGUISES: tuple[Callable[[str], str], ...] = (
    lambda s: s.replace(".", "\\."),
    re.escape,
    lambda s: s.replace(".", "`."),
    lambda s: s.replace(".", "%2E"),
    lambda s: s.replace(".", "%2e"),
    lambda s: s.replace(".", "[.]"),
)


def _as_written(text: str) -> str:
    return text


def _url_spaces(text: str) -> str:
    return text.replace(" ", "%20")


def _disguises(text: str) -> list[tuple[str, Callable[[str], str]]]:
    """Each escaped spelling of `text` that differs from it, with the function that wrote it."""
    out: list[tuple[str, Callable[[str], str]]] = []
    seen = {text}
    for style in DISGUISES:
        spelling = style(text)
        if spelling not in seen:
            seen.add(spelling)
            out.append((spelling, style))
    return out


def _whole(key: str) -> str:
    body = re.escape(key)
    if key[:1].isalnum():
        body = f"(?<!{_WORD_CHAR})" + body
    if key[-1:].isalnum():
        body = body + f"(?!{_WORD_CHAR})"
    return body


# Where a match may begin and end (D77, DECISIONS 36). Three rules, one per direction of travel:
#   "word"  - whole words: the old rule, kept for anything that is neither a real nor a token.
#   "real"  - a real name going out: a word boundary, or a CASE boundary - lower-to-Upper
#             (Get-acme|Approved... newAcme) or the end of an acronym before a capitalised word
#             (ACME|Approved). Identifiers glue words together; a client name inside one was sent in
#             the clear. An ordinary-word name inside an unrelated lowercase word ("tailwindcss") is
#             still left alone, which is what whole-word matching was protecting.
#   "token" - a token coming back: anywhere, because aicleaner minted it to be unique - except that
#             a digit edge may not touch another digit, so Client07 is never found inside Client0712.
#             Whole-word restore left Get-<token>ApprovedSoftware.ps1 on disk with the token in it.
# The case tests use (?-i:...) because the patterns are IGNORECASE.
_CASE_START = r"(?:(?<![^\W_])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z]))|(?<=(?-i:[A-Z]))(?=(?-i:[A-Z][a-z])))"
_CASE_END = r"(?:(?![^\W_])|(?<=(?-i:[a-z0-9]))(?=(?-i:[A-Z]))|(?=(?-i:[A-Z][a-z])))"

# A token the model wrote as a case-insensitive pattern: its first letter as a class, `[Cc]lient04`
# (D87). The reverse table carries one such key per token, `[cc]lient04`; the pattern is
# IGNORECASE, so it matches `[Cc]`, `[cC]` and also `[CC]` - which is not a class of two cases,
# and is left alone where it is restored.
BRACKET_LEN = 3   # what `[Xx]` adds to a token's length
# A token that can be abbreviated (D117): a word of three letters or more, then a number of four
# digits or more, and nothing else - `Client4211`, not `Client4211 Pty`.
ABBREVIABLE = re.compile(r"([A-Za-z]{3,})(\d{4,})")
# The mailbox part of an address token as token_for and rule_email_token mint it: user, the client
# index, then four to sixteen hex digits of the address's hash (D170).
EMAIL_LOCAL_TOKEN = re.compile(r"user\d{2,}[0-9a-f]{4,16}")


def _bracket_key(token: str) -> str | None:
    first = token[:1]
    return f"[{first}{first}]{token[1:]}" if first.lower() != first.upper() else None


def _bracket_parts(matched: str) -> tuple[str, str] | None:
    """(the token with its class collapsed, the class letters) for a bracketed match, else None."""
    if len(matched) > 4 and matched[0] == "[" and matched[3] == "]":
        return matched[1] + matched[4:], matched[1:3]
    return None


def _bracket_real(real: str) -> str:
    """The real name in the form the model wrote the token: `[Aa]CME`. A real that does not start
    with a letter has no case to class, so it goes in as it is."""
    first = real[:1]
    return f"[{first.upper()}{first.lower()}]{real[1:]}" if first.lower() != first.upper() else real


def _head(first: str, mode: str) -> str:
    """The assertion before a key, which depends only on its first character."""
    if not first.isalnum():
        return ""
    if mode == "token":
        return "(?<![0-9])" if first.isdigit() else ""
    return _CASE_START if mode == "real" else f"(?<!{_WORD_CHAR})"


def _tail(key: str, mode: str = "word") -> str:
    """The assertion that has to hold after this key, which depends only on its last character."""
    last = key[-1:]
    if not last.isalnum():
        return ""
    if mode == "token":
        return "(?![0-9])" if last.isdigit() else ""
    return _CASE_END if mode == "real" else f"(?!{_WORD_CHAR})"


@lru_cache(maxsize=16384)
def _spelling(key: str) -> str:
    """The spelling to put in the tree: lower case where that still matches the original.

    The pattern is IGNORECASE, so "Acme" and "acme" match the same text - but as tree branches they
    are 'A' and 'a', and a longer key under one is unreachable once the other matches. "acme-prod-01"
    came back as "acme-prod" for exactly that reason. Folding makes the shared prefix genuinely
    shared. A few letters do not survive lower() as a case-insensitive match for themselves (the
    Turkish dotted I lowers to two characters), so those keep their own spelling: being in a branch
    of their own costs a rare missed longest-match, where folding them would cost matching at all.

    Memoised because it is a pure function of the key and was the single most expensive thing in
    a rebuild: one `re.fullmatch` per key per tree, with a distinct pattern each time, which
    overflows `re`'s own small pattern cache and recompiles. At twenty builds per edit that was
    tens of thousands of throwaway compilations.
    """
    low = key.lower()
    return low if re.fullmatch(re.escape(low), key, re.IGNORECASE) else key


def _trie(keys: Iterable[str], mode: str = "word") -> dict:
    """Keys as a prefix tree. A terminal carries the assertion for the key that ends there."""
    root: dict = {}
    for key in keys:
        node = root
        for char in _spelling(key):
            node = node.setdefault(char, {})
        node[_END] = _tail(key, mode)
    return root


def _trie_regex(node: dict) -> str:
    """A prefix tree as a pattern, preferring the longest match.

    Alternatives at a node are the continuations FIRST and the "this key ends here" assertion last,
    so a longer key still beats a shorter one that is a prefix of it - which is the guarantee the
    old flat alternation got from sorting longest-first, and the thing a naive trie would lose.

    Walked with an explicit stack rather than by recursion. The depth of this tree is the LENGTH of
    the longest form, so a single long one took the whole build down with RecursionError - and a
    mapper that cannot compile makes its client `broken`, which stops model traffic for every
    client. A 3,205-character run out of a minified file did exactly that. Detection no longer
    offers anything that long (detect.MAX_RESOURCE_RUN), but a form can also arrive by hand or in a
    file written by an older version, and the cost of meeting one should be that form failing
    validation, not the filter failing to start.
    """
    done: dict[int, str] = {}
    stack: list[tuple[dict, bool]] = [(node, False)]
    while stack:
        current, built = stack.pop()
        if not built:
            stack.append((current, True))
            for char, child in current.items():
                if char is not _END:
                    stack.append((child, False))
            continue
        children = sorted((char, child) for char, child in current.items() if char is not _END)
        if not children:
            done[id(current)] = current.get(_END, "")   # a key ends here and goes no further
            continue
        parts = [re.escape(char) + done[id(child)] for char, child in children]
        if _END in current:
            parts.append(current[_END])   # may be empty: the last resort, matching nothing further
        done[id(current)] = parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"
    return done[id(node)]


# Where a form came from. It decides one thing only: whether the form's words are trusted as
# NAMES OF THE CLIENT, and so watched for inside longer words (detect._needles).
#
# A scan that collected "TerraformValidation" made "validation" a word to hunt for, and the next
# pass took everything containing it - 233 generic identifiers reached one client that way. The
# scan's own output became the vocabulary it searched with (D1). What names a client is its id,
# its label, and what a person put there deliberately; what a scan or an auto-add found is
# evidence, not vocabulary.
#
# "" means unknown, which is every form written before this existed, and is treated as trusted -
# those were curated by hand or have already been reviewed, and silently dropping their words
# would quietly weaken matching on files nobody has touched.
ORIGIN_SCAN = "scan"      # a repository scan collected it
ORIGIN_AUTO = "auto"      # the proxy auto-added it mid-request
FOUND_ORIGINS = frozenset({ORIGIN_SCAN, ORIGIN_AUTO})

# How an entry came to be here, in the words a person reads (feature 30). This is NOT `origin`
# above: that one answers "does this name the client", which decides whether its words become
# needles (D1), and overloading it would change detection. This one answers "how did this get
# here", for somebody deciding whether the entry belongs.
#
# Deliberately coarse, because it is the part that TRAVELS. A client file is published and shared,
# and it should say how an entry arrived without saying which repository path it was found in.
# The detail lives in local state and never leaves the machine - see `provenance.py`.
SOURCE_SCAN = "repo-scan"
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto-add"
SOURCE_DERIVED = "derived"
SOURCE_REVIEW = "review"
SOURCES = {
    SOURCE_SCAN: "Repo scan",
    SOURCE_MANUAL: "Manual entry",
    SOURCE_AUTO: "Auto-add",
    SOURCE_DERIVED: "Derived",
    SOURCE_REVIEW: "Review",
}


@dataclass(frozen=True)
class Form:
    real: str
    token: str
    client: str
    origin: str = ""
    # What this form is allowed to match, for a client whose name is an ordinary word (feature 23,
    # defect D2). Empty means "wherever the text appears", which is every form today.
    #
    # `match` narrows what is REPLACED, and the canary honours it too (DECISIONS 39). Detection and
    # the near-miss path keep using the unrestricted form, so a rule never reduces what is found.
    # The canary used to ignore the rule as well - decided 2026-09-27 "so a rule can never turn
    # ordinary vocabulary into a 403" - and did exactly that: `cased` left "the beacon light" alone
    # and the canary refused the request for it. Now what a rule declines is not a leak, and every
    # such occurrence is reported (Mapper.rule_declined) - logged, counted and shown, never hidden.
    match: "MatchRule | None" = None


@dataclass(frozen=True)
class MatchRule:
    """When a form's text counts as the client rather than as the ordinary word it looks like.

    `cased` matches only the capitalisation as declared, so *Harbour* is the client and *harbour* is a
    noun. Brittle - it trusts whoever typed the prompt to capitalise - but it is the cheapest
    useful rule.

    `followed_by` / `preceded_by` match only next to a corroborating word, which is stronger and
    more work to maintain. Both are case-insensitive and are satisfied by any one of their words.
    """

    cased: bool = False
    followed_by: tuple[str, ...] = ()
    preceded_by: tuple[str, ...] = ()

    def as_json(self) -> Any:
        if self.cased and not self.followed_by and not self.preceded_by:
            return "cased"
        out: dict[str, Any] = {}
        if self.cased:
            out["cased"] = True
        if self.followed_by:
            out["followed_by"] = list(self.followed_by)
        if self.preceded_by:
            out["preceded_by"] = list(self.preceded_by)
        return out


# How far either side of a match to look for a corroborating word. A neighbour is the next word,
# not a word somewhere in the paragraph - "Harbour" three sentences before "Pty" is not corroboration.
NEIGHBOUR_CHARS = 40
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def parse_match(value: Any) -> "MatchRule | None":
    """A stored `match` value as a rule, or None for no rule. Raises on a shape it cannot honour.

    Refusing an unknown shape matters more here than it usually would: silently ignoring a rule
    somebody wrote would replace text they had declared should be left alone, which is the
    opposite of what they asked for - but silently *applying* a rule nobody wrote would stop
    replacing text that should be replaced. Neither failure is acceptable, so it raises.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if value != "cased":
            raise MappingError(
                f"{value!r} is not a matching rule (the only one with a short name is 'cased')",
                why="A rule that is not understood would be ignored, and the form would go on "
                    "replacing the ordinary word you were trying to protect.")
        return MatchRule(cased=True)
    if not isinstance(value, dict):
        raise MappingError(f"a matching rule is 'cased' or an object, not {type(value).__name__}")
    unknown = set(value) - {"cased", "followed_by", "preceded_by"}
    if unknown:
        raise MappingError(f"unknown matching rule {sorted(unknown)[0]!r}",
                           why="Allowed: cased, followed_by, preceded_by.")

    def words(key: str) -> tuple[str, ...]:
        raw = value.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(w, str) for w in raw):
            raise MappingError(f"{key} is a list of words")
        clean = tuple(w.strip() for w in raw if w and w.strip())
        if any(not _WORD_RE.fullmatch(w) for w in clean):
            raise MappingError(f"{key} takes single words (letters and digits), not phrases")
        return clean

    rule = MatchRule(cased=bool(value.get("cased")),
                     followed_by=words("followed_by"), preceded_by=words("preceded_by"))
    if not rule.cased and not rule.followed_by and not rule.preceded_by:
        return None
    return rule


def _rule_probe(form: Form) -> tuple[str, str]:
    """(text, expected) proving this form still replaces in the circumstances it declared.

    For an unruled form and for `cased`, that is the bare declared text. For a neighbour rule it
    has to be the text beside a word the rule accepts, because not replacing the bare text is the
    entire point of the rule. Used by the round-trip check, so a rule that could not satisfy even
    its own example is refused at build time rather than silently never firing.
    """
    rule = form.match
    if rule is None or (not rule.followed_by and not rule.preceded_by):
        return form.real, form.token
    before = f"{rule.preceded_by[0]} " if rule.preceded_by else ""
    after = f" {rule.followed_by[0]}" if rule.followed_by else ""
    return f"{before}{form.real}{after}", f"{before}{form.token}{after}"


def rule_allows(rule: "MatchRule | None", text: str, start: int, end: int, real: str) -> bool:
    """Whether a match at [start:end) in `text` counts as the client under `rule`.

    The floor: a rule can never stop a form matching the text as declared *in the circumstances it
    declared*. `cased` is satisfied by the declared spelling by definition. A neighbour rule is
    checked only against the surrounding words and never against the match itself, so it cannot
    reject the declared spelling either - though it will, by design, decline the bare word with no
    neighbour, which is what it was written to do. `_rule_probe` is where that floor is enforced.
    """
    if rule is None:
        return True
    found = text[start:end]
    if rule.cased and found != real:
        return False
    if rule.followed_by:
        after = _WORD_RE.search(text[end:end + NEIGHBOUR_CHARS])
        if after is None or after.group(0).lower() not in {w.lower() for w in rule.followed_by}:
            return False
    if rule.preceded_by:
        before = _WORD_RE.findall(text[max(0, start - NEIGHBOUR_CHARS):start])
        if not before or before[-1].lower() not in {w.lower() for w in rule.preceded_by}:
            return False
    return True


def _declined(form: "Form", m: "re.Match[str]") -> bool:
    """True when a form's match rule says this occurrence is not the client (feature 23)."""
    return form.match is not None and not rule_allows(form.match, m.string, m.start(), m.end(), form.real)


# Case variants are matched even when they were never declared (a user types VMAUEPRD01 for a form
# declared vmaueprd01). Each variant gets its own token so the case that went in is the case that
# comes back, which matters for names the user will paste into a console.
CASE_INSENSITIVE_KINDS = frozenset({"guid", "ip", "email", "domain", "tenant", "sharepoint"})

# What a restored name is wrapped in when the reader should see it: ANSI green at normal weight.
# The combined form (32;22 rather than plain 32) is deliberately unusual, so stripping it from the
# history on the way back out cannot touch colour codes that were in a pasted terminal log.
MARK_OPEN = "\x1b[32;22m"
MARK_CLOSE = "\x1b[39;22m"


def _expand_case(forms: Iterable[Form]) -> list[Form]:
    """Add lower/upper/title/capitalised variants of each form when the client asks for it.

    Variants may share the original's token (many reals -> one token); the
    first-declared real is what the token rehydrates to. Losing case fidelity
    on the way back is the price of never letting a case variant leak.
    """
    out: list[Form] = list(forms)
    seen_real = {f.real for f in out}
    for f in list(out):
        if classify(f.real) in CASE_INSENSITIVE_KINDS:
            continue  # hosts, GUIDs and addresses: case carries no meaning, one token is enough
        if f.match is not None and f.match.cased:
            continue  # the form says only its own capitalisation is the client; expanding is the
                      # exact opposite of that, and the client-wide switch must not override it
        variants = (
            (f.real.lower(), f.token.lower()),
            (f.real.upper(), f.token.upper()),
            (f.real.title(), f.token.title()),
            (f.real.capitalize(), f.token.capitalize()),
        )
        for variant, tok in variants:
            if variant == f.real or variant in seen_real:
                continue
            # A case variant is as trusted as its form, and carries its rule: a `followed_by`
            # form that gained an upper-case variant still wants the corroborating word.
            out.append(Form(variant, tok, f.client, f.origin, f.match))
            seen_real.add(variant)
    return out


def variant_token(variant: str, real: str, token: str, taken: set[str] | None = None) -> str:
    """The token for a case variant of `real`.

    Kinds where case carries no meaning (hosts, GUIDs, addresses) keep the primary token. Otherwise
    the variant takes the same transform of the token (VMAUEPRD01 -> CLIENT05PRD01), or, for mixed
    case, the token with a short hash of the variant, so it rehydrates to exactly what was typed.
    """
    if classify(real) in CASE_INSENSITIVE_KINDS:
        return token
    used = set(taken or ())
    for transform in (str.lower, str.upper, str.title, str.capitalize):
        if variant == transform(real):
            candidate = transform(token)
            if candidate != token and candidate not in used:
                return candidate
    digest = hashlib.sha1(variant.encode()).hexdigest()
    for size in (4, 8):
        short = digest[:size]
        if "." in token:
            head, dot, rest = token.partition(".")
            candidate = f"{head}-{short}{dot}{rest}"
        elif " " in token:
            candidate = joined_suffix(token, short)      # one identifier, as _suffixed (2026-10-15)
        else:
            candidate = f"{token}-{short}"
        if candidate not in used:
            return candidate
    return token


# How reading text off a screen goes wrong: letters merge (rn becomes m, ri becomes n) and
# lookalikes swap (1/l/i, 0/o, 5/s). Two words are compared on their "shape", with those confusions
# folded away, so Fabrikam and a misread Fabnkam meet.
_CONFUSIONS = (("rn", "m"), ("ri", "n"), ("cl", "d"), ("vv", "w"), ("ii", "n"), ("nn", "m"))
_LOOKALIKE = str.maketrans({"1": "i", "l": "i", "|": "i", "!": "i", "0": "o", "5": "s", "8": "b",
                            "2": "z", "9": "g", "q": "g"})


def ocr_shape(text: str) -> str:
    """A word with the usual misreadings folded away, for comparing one word against another."""
    shaped = text.lower()
    for pair, single in _CONFUSIONS:
        shaped = shaped.replace(pair, single)
    return shaped.translate(_LOOKALIKE)


def one_edit_apart(word: str, other: str) -> bool:
    """True when the two differ by a single insertion, deletion or substitution."""
    if abs(len(word) - len(other)) > 1:
        return False
    if len(word) == len(other):
        return sum(1 for a, b in zip(word, other) if a != b) == 1
    longer, shorter = (word, other) if len(word) > len(other) else (other, word)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def near_miss_match(word: str, other: str) -> bool:
    """True when `word` is almost certainly a misreading of `other`.

    Digits must match exactly: wingtip02 is not a misreading of wingtip01, it is another machine.
    What is allowed is the letters going wrong, once, after the usual confusions are folded away.
    """
    if word.lower() == other.lower():
        return False
    if [c for c in word if c.isdigit()] != [c for c in other if c.isdigit()]:
        return False
    # Two readings: lookalike letters only (fabrlkam), and pairs of letters merged as well
    # (fabnkam). Folding the pairs changes a correct word too, so both readings are compared.
    plain = (word.lower().translate(_LOOKALIKE), other.lower().translate(_LOOKALIKE))
    shaped = (ocr_shape(word), ocr_shape(other))
    return any(a == b or one_edit_apart(a, b) for a, b in (plain, shaped))


class Mapper:
    """Compiled two-way substitution over a validated set of forms."""

    def __init__(self, forms: Iterable[Form], build: bool = True):
        self.forms: list[Form] = list(forms)
        self._validate()
        if not build:
            # Validation only (see from_dict): nothing below can refuse a mapping, it only builds
            # the tables a substitution needs - which a check before a write throws away.
            return
        self._fwd ={f.real: f for f in self.forms}
        self._rev: dict[str, Form] = {}
        for f in self.forms:
            self._rev.setdefault(f.token, f)
        self._abbrev: dict[str, str] = {}      # abbreviated token -> its token (D117)
        # Folded tables: what an undeclared case variant resolves to, in both directions.
        self._fold: dict[str, Form] = {}
        self._rev_fold: dict[str, Form] = {}
        for f in self.forms:
            self._fold.setdefault(f.real.lower(), f)
            self._rev_fold.setdefault(f.token.lower(), f)
        self._learned: dict[str, Form] = {}   # variants met at runtime, keyed by exact spelling
        # What backward's last-resort scan concluded, per spelling - including "nothing", which is
        # the answer a model repeats most (D10). Bounded by the same ceiling as the variants.
        self._rev_scanned: dict[str, Form | None] = {}
        self.variant_sink = None              # set by MapperHandle so variants are remembered
        self._rev_dirty = False
        # Escaped spellings of each real (DISGUISES): matched like a real, replaced by the token
        # written the same way. The first form to claim a spelling keeps it, as with the reals.
        self._disguised: dict[str, tuple[Form, Callable[[str], str]]] = {}
        for f in self.forms:
            for spelling, style in _disguises(f.real):
                self._disguised.setdefault(spelling, (f, style))
        self._disguised_fold: dict[str, tuple[Form, Callable[[str], str]]] = {}
        for spelling, found in self._disguised.items():
            self._disguised_fold.setdefault(spelling.lower(), found)
        keys = dict(self._fwd)
        for spelling, (f, _style) in self._disguised.items():
            keys.setdefault(spelling, f)
        self._fwd_re = self._compile(keys, "real")
        self._rev_disguised: dict[str, tuple[str, Callable[[str], str], bool]] = {}
        self._rev_disguised_fold: dict[str, tuple[str, Callable[[str], str], bool]] = {}
        self._rev_re = self._compile_reverse()
        # Quotes and backslashes in a real are escaped wherever JSON sits inside a string (tool-call
        # arguments, streamed partial_json), and escaped again when that request is serialised; the
        # JSON-text and leak-check tables carry those spellings too.
        self._fwd_json: dict[str, Form] = {}
        self._canary: dict[str, Form] = {}
        for f in self.forms:
            once = _json_escape(f.real)
            for key in (f.real, once):
                self._fwd_json.setdefault(key, f)
            for key in (f.real, once, _json_escape(once)):
                self._canary.setdefault(key, f)
        # The escaped spellings too: inside tool-call JSON a regex's backslash is itself escaped.
        self._disguised_json: dict[str, tuple[Form, Callable[[str], str]]] = {}
        for spelling, (f, style) in self._disguised.items():
            once = _json_escape(spelling)
            self._fwd_json.setdefault(spelling, f)
            if once != spelling:
                self._fwd_json.setdefault(once, f)
                self._disguised_json.setdefault(once, (f, style))
            for key in (spelling, once, _json_escape(once)):
                self._canary.setdefault(key, f)
        # Compiled on first use, not here. Both are only ever read on a request path
        # (tokenise_json_text and the canary scan), and a Mapper is built far more often for
        # validation than for serving: _combine_error builds one purely to find out whether it
        # can be built, and load_tolerant does that about twice per client file. Compiling these
        # eagerly meant every one of those paid for two large trie regexes it would never use.
        self._escapes = any(f.real != _json_escape(f.real) for f in self.forms) or bool(self._disguised)
        self._fwd_json_re_cache: re.Pattern[str] | None = None
        self._canary_re_cache: re.Pattern[str] | None = None
        self._tails_re: re.Pattern[str] | None = None
        self.max_token_len = max((len(f.token) for f in self.forms), default=1)
        self.max_real_len = max((len(f.real) for f in self.forms), default=1)
        self.clients = sorted({f.client for f in self.forms})
        self.ignore: set[str] = set()  # lowercased strings never proposed as candidates (set by MappingStore)
        # Email rules (feature 96): a client domain -> its client; every address there is the client's.
        self.email_rules: dict[str, str] = {}
        self.shadowed: set[tuple[str, str]] = set()  # (client, real) left out because Global has it
        self._check_roundtrip()

    # ---- construction -----------------------------------------------------

    @staticmethod
    def _compile(table: dict[str, Form], mode: str = "word") -> re.Pattern[str]:
        """Whole words only, via a memo on the exact set of keys.

        One edit to a client rebuilds the mapping several times over - validation builds a Mapper
        to see whether it can be built, the write validates again, and the reload after it builds
        the real one - and most of those builds are over identical key sets. Sorting and hashing
        a couple of thousand short strings is a fraction of a millisecond; compiling the tree is
        tens of them, so the memo turns every repeat into a lookup. Patterns are immutable, so
        sharing one between mappers is safe.
        """
        return Mapper._compile_keys(tuple(sorted(table, key=lambda k: (-len(k), k))), mode)

    @staticmethod
    @lru_cache(maxsize=16)
    def _compile_keys(keys: tuple[str, ...], mode: str = "word") -> re.Pattern[str]:  # noqa: D401 (docstring below)
        """Whole words only: an edge that is a letter or digit must not touch another letter or
        digit, so "acme" never changes inside "pacmen" while "acme-prod" and "acme.com" still match.
        Longest first, then alphabetical, so the result never depends on declaration order."""
        if not keys:
            return re.compile(r"(?!x)x")  # never matches: an empty mapping substitutes nothing
        # A prefix tree rather than a flat alternation. `re` tries each alternative at every
        # position, so "a|b|c|..." costs O(text x forms): at three forms that is nothing, and at
        # the couple of thousand a repository scan produces it took a request from 210ms to over
        # two minutes. A tree tests each shared prefix once.
        #
        # Split on whether the first character is alphanumeric, because that decides the assertion
        # in front and it has to wrap the whole tree. The two groups cannot both match in the same
        # place - a character either is alphanumeric or is not - so ordering between them cannot
        # cost a longer match. The assertion behind depends on each key's last character, so it
        # sits at that key's terminal inside the tree.
        # Grouped by the assertion in front, which has to wrap its whole tree (see _head): letters
        # and digits apart, because a token's digit edge is guarded and a letter edge is not.
        groups: dict[str, list[str]] = {}
        for k in keys:
            groups.setdefault(_head(k[:1], mode), []).append(k)
        parts = [head + _trie_regex(_trie(group, mode)) for head, group in groups.items()]
        # IGNORECASE so a case variant is matched; which form it is remains an exact lookup first.
        return re.compile("|".join(parts), re.IGNORECASE)

    def _validate(self) -> None:
        # An empty mapping is allowed (fresh install, or a client file with no forms yet):
        # the proxy then substitutes nothing, and `check` warns about it.
        reals: set[str] = set()
        tokens: dict[str, str] = {}  # token -> client id
        first_real: dict[str, str] = {}  # token -> first real declared for it
        for f in self.forms:
            if not f.real or not f.token or not f.client:
                raise MappingError(f"empty real/token/client in form {f!r}")
            if f.real == f.token:
                raise MappingError(f"real and token identical: {f.token!r}")
            if any(ord(c) < 32 for c in f.real + f.token):
                raise MappingError(f"form for client {f.client} contains a control character")
            # A colon is not shell/HCL/URL safe in general, but an IPv6 token is an address and has
            # to look like one (DECISIONS 41) - so only a token that parses as IPv6 may carry one.
            if not SAFE_TOKEN_RE.match(f.token) and not is_ipv6(f.token):
                raise MappingError(
                    f"token {f.token!r} is not shell/HCL/URL safe "
                    "(allowed: letters, digits, space . _ @ -)"
                )
            if f.real in reals:
                raise MappingError(f"duplicate real form for client {f.client}")
            if tokens.get(f.token, f.client) != f.client:
                raise MappingError(f"token {f.token!r} is used by more than one client")
            # A token shared inside one client is only safe for case variants of one string;
            # anything else would rehydrate two different identifiers to the first one.
            if f.token in first_real and first_real[f.token].lower() != f.real.lower():
                raise MappingError(
                    f"token {f.token!r} is shared by {first_real[f.token]!r} and {f.real!r}; "
                    "give each identifier its own token (see token_for)"
                )
            reals.add(f.real)
            tokens.setdefault(f.token, f.client)
            first_real.setdefault(f.token, f.real)
        # A token that contains a real form (or vice versa) makes the two
        # directions fight each other; refuse rather than guess.
        #
        # Tested against one joined string per side rather than every pair. The pair loop was
        # O(tokens x reals) - about 1.9 million Python-level checks for a 1,400-form mapping, and
        # a Mapper is built around twenty times for a single edit, so it was the largest single
        # cost in the whole write path. Joining is exact here because a control character cannot
        # appear in either side (rejected above), so NUL cannot occur inside a real or a token and
        # no match can span a join. The slow scan runs only to name the culprit in the error.
        joined_tokens = "\x00".join(tokens)
        joined_reals = "\x00".join(reals)
        # Compared ignoring case, as the matching does: "box" was let past a token "Client05_Box" (2026-10-23).
        folded_tokens = joined_tokens.lower()
        for r in reals:
            if r.lower() in folded_tokens:
                # Only where the real would actually be FOUND inside the token by the outbound matching rule (a
                # word or case edge each side): "AB" sits inside a token like "abwidgetbox" but runs on into
                # "widget", so it is never matched there, and refusing it kept a two-letter name out (2026-10-23).
                found = re.compile(_head(r[:1], "real") + re.escape(r) + _tail(r, "real"), re.IGNORECASE)
                bad = next((t for t in tokens if r.lower() in t.lower() and found.search(t)), None)
                if bad is not None:
                    raise MappingError(f"token {bad!r} contains real form; unsafe")
        for t in tokens:
            if t in joined_reals:
                raise MappingError(f"real form contains token {t!r}; unsafe")

    def _check_roundtrip(self) -> None:
        for f in self.forms:
            # A form with a neighbour rule does not replace its own bare text - that is what the
            # rule is for - so the probe puts it next to a word the rule accepts. The check is
            # then the one that matters: a declared form, in the circumstances it declared, is
            # still replaced. A rule that could not satisfy even its own example would be one
            # that never fires, and this is where that would be caught.
            probe, want = _rule_probe(f)
            if self.tokenise(probe) != want:
                raise MappingError(f"tokenise({probe!r}) != {want!r}")
            primary = self._rev[f.token].real
            if self.rehydrate(f.token) != primary:
                raise MappingError(f"rehydrate({f.token!r}) != {primary!r}")

    # ---- operations -------------------------------------------------------

    def forward(self, text: str) -> Form:
        """The form for a matched string: the declared one, or a case variant of it (learned once,
        so the same spelling always gets the same token and rehydrates to what was typed)."""
        f = self._fwd.get(text) or self._learned.get(text)
        if f is not None:
            return f
        primary = self._primary(text)
        if primary is None:   # cannot happen for a regex match; never let it break a request
            return Form(text, text, "")
        # A `cased` form is declaring that only its own capitalisation is the client, so a
        # different one must not become a learned variant. Without this the noise the rule exists
        # to stop would still be minted a token, written to the variants store and added to the
        # reverse tables - all for a spelling the rule is about to decline to replace.
        if primary.match is not None and primary.match.cased:
            return primary
        # Past the ceiling, stop minting new ones and use the declared form's own token.
        #
        # Nothing evicted this, so a long-lived process that met thousands of case variants kept
        # them all, and each one also lands in the reverse tables and the reverse pattern (D9).
        # Eviction is not the answer: a variant's token has already gone to the provider, so the
        # reverse side must keep it or the reply cannot be rehydrated - and dropping only the
        # forward entry would mint a DIFFERENT token for the same spelling next time, breaking
        # the guarantee this cache exists for.
        #
        # So the degradation is in fidelity, not in correctness: beyond the ceiling the case that
        # went in is not the case that comes back, which is the same thing that happens for any
        # form the mapping does not declare a variant of. Nothing leaks either way.
        if len(self._learned) >= MAX_LEARNED_VARIANTS:
            return primary
        token = variant_token(text, primary.real, primary.token,
                              set(self._rev) | {f.token for f in self._learned.values()})
        # The variant inherits the rule. Without it, one occurrence next to a corroborating word
        # would learn an unruled variant of that spelling, and every later occurrence of it -
        # corroborated or not - would replace. The rule would hold until the first time it was
        # satisfied and then quietly stop.
        variant = Form(text, token, primary.client, match=primary.match)
        self._learned[text] = variant
        self._rev.setdefault(token, variant)
        self._rev_fold.setdefault(token.lower(), variant)
        self._rev_dirty = True
        if self.variant_sink is not None:
            try:
                self.variant_sink(primary.client, text, token)
            except Exception:  # remembering a variant must never break a request
                pass
        return variant

    def backward(self, text: str) -> Form:
        """The form for a matched token, including one whose case the model changed."""
        f = self._rev.get(text) or self._rev_fold.get(text.lower()) or self._rev_fold.get(text.casefold())
        if f is None:
            # The last resort: a token that three lookups missed, because `re`'s IGNORECASE
            # matches a few letters that neither lower() nor casefold() maps the same way - the
            # long s, the dotted capital I, the Kelvin sign. Getting rid of the scan entirely
            # would mean reproducing `re`'s own folding tables, which is not worth being subtly
            # wrong about; making it happen once per spelling is (D10).
            #
            # Remembered whether or not it found anything, because a token that matches nothing
            # is exactly the one a model will repeat in the same reply, and scanning every form
            # again for each occurrence is the cost this is here to avoid.
            if text in self._rev_scanned:
                f = self._rev_scanned[text]
            else:
                f = next((g for g in self._rev.values()
                          if re.fullmatch(re.escape(g.token), text, re.IGNORECASE)), None)
                if len(self._rev_scanned) < MAX_LEARNED_VARIANTS:
                    self._rev_scanned[text] = f
        return f if f is not None else Form(text, text, "")   # left as it came, never a crash

    def _disguise(self, text: str) -> tuple[Form, Callable[[str], str]] | None:
        """The form and the escaping of a matched escaped spelling (DISGUISES), in any case."""
        return self._disguised.get(text) or self._disguised_fold.get(text.lower())

    def _canary_form(self, key: str) -> Form | None:
        """The form a canary match belongs to: a real or a JSON spelling of one, a case variant, or
        an escaped spelling (also as JSON writes it)."""
        f = self._canary.get(key) or self._primary(key)
        if f is None:
            hidden = self._disguise(key) or self._disguised_json.get(key)
            f = hidden[0] if hidden else None
        return f

    def _primary(self, text: str) -> Form | None:
        """The declared form a case-insensitive match belongs to. The regex's IGNORECASE matches
        some letters .lower() does not map (the long s, the dotless and dotted i, the Kelvin sign),
        so a plain lookup raised KeyError: a 500 on every turn carrying the text (H14)."""
        f = self._fold.get(text.lower()) or self._fold.get(text.casefold())
        if f is None:
            f = next((g for g in self.forms if re.fullmatch(re.escape(g.real), text, re.IGNORECASE)), None)
        return f

    def _reverse_pattern(self) -> re.Pattern[str]:
        if self._rev_dirty:
            self._rev_re = self._compile_reverse()
            self._rev_dirty = False
        return self._rev_re

    def _compile_reverse(self) -> re.Pattern[str]:
        """Every token, every token with its first letter written as a class (D87), and every
        abbreviation of a word-and-number token (D117)."""
        keys = dict.fromkeys(self._rev)
        for token in self._rev:
            bracketed = _bracket_key(token)
            if bracketed is not None:
                keys.setdefault(bracketed)
        self._abbrev = self._abbreviations()
        keys.update(dict.fromkeys(self._abbrev))
        # A token written escaped (DISGUISES) - `client99-1a2b.invalid` in a regex the model wrote -
        # went back unrestored, so a search or a rule written with it found nothing. Each escaped
        # spelling, and that spelling as JSON writes it, comes back as the real escaped the same way.
        # Keyed exactly, and folded for a case variant: %2E and %2e fold together, and each must
        # come back as it was written.
        self._rev_disguised = {}
        self._rev_disguised_fold = {}
        for token in self._rev:
            spellings = _disguises(token)
            if JOINED_SUFFIX_RE.match(token) or JOINED_NAME_RE.match(token):
                # Written before the suffix was joined (2026-10-15) or the name token was (2026-10-23), or
                # URL-encoded: "Client05 1a2b", "Client05 Pty" and "Client05%201a2b" come back as the real, the
                # last encoded the same way.
                spellings += [(token.replace("_", " "), _as_written),
                              (token.replace("_", "%20"), _url_spaces)]
            for spelling, style in spellings:
                for written, as_json in ((spelling, False), (_json_escape(spelling), True)):
                    if written not in self._rev_disguised and written not in self._rev:
                        self._rev_disguised[written] = (token, style, as_json)
                        self._rev_disguised_fold.setdefault(written.lower(), (token, style, as_json))
                        keys.setdefault(written)
        return self._compile(keys, "token")

    def _abbreviations(self) -> dict[str, str]:
        """Shortened forms of each word-and-number token, and the token each stands for (D117).

        A model shown `Client4211` made up `C4211-BuildTeam-Reader` as a name for something it
        created, and nothing knew `C4211`: it went back unrestored and real objects were made in a
        client tenant with the token's number in their names. The number is aicleaner's, minted to
        be unique, so its first letter or two with that number - `C4211`, `CL4642`, `C-4642` - is
        the token shortened, and comes back as the token's own real. That real is a known form,
        so the name goes out tokenised again next time and nothing leaks. Three digits or more,
        four digits or more, as tokens are minted, so a short number never makes a key; never a key that is
        already a token. Unlike a token, an abbreviation is not found glued to a letter or digit in front
        (`_abbreviation_fits`): `XC4211` is somebody else's part number, not a token shortened."""
        out: dict[str, str] = {}
        for token in self._rev:
            parts = ABBREVIABLE.fullmatch(token)
            if parts is None:
                continue
            label, digits = parts.groups()
            for head in {label[:1], label[:2]}:
                for sep in ("", "-", "_"):
                    key = f"{head}{sep}{digits}"
                    if key in self._rev or key.lower() in self._rev_fold:
                        continue
                    # The capitalised spelling is the one to restore by; a lower-case abbreviation
                    # picks the lower-case token in _restored when the client declares one.
                    if key.lower() not in out or (token[:1].isupper() and not token.isupper()):
                        out[key.lower()] = token
        # An address token's mailbox part alone - `user05ab12cd` for user05ab12cd@client05.invalid -
        # which a model writes when it means the person (D170: "user2341ae4f is one of them" reached
        # a question shown to the user). The hash makes it one address's, so it comes back as that
        # whole address: a known form, tokenised again next time - the mailbox name alone would not be.
        for token, form in self._rev.items():
            local, at, _domain = token.partition("@")
            if at and EMAIL_LOCAL_TOKEN.fullmatch(local) and classify(form.real) == "email":
                key = local.lower()
                if key not in self._rev_fold and key not in out:
                    out[key] = token
        return out

    def token_fragments(self, text: str) -> list[str]:
        """Pieces of tokens left in already-restored text: a token whose first letter was written
        some way the reverse pattern does not know (`(C|c)lient04`, `?lient04`) arrives intact
        apart from that letter. Only tails of six characters or more count, so a short common
        word is never reported. The safety net under D87: found, it is logged and toasted."""
        if getattr(self, "_tails_re", None) is None:
            tails = {f.token[1:] for f in self.forms if len(f.token) > 6}
            self._tails_re = self._compile(dict.fromkeys(tails), "token")
        found = [m.group(0) for m in self._tails_re.finditer(text)]
        # A name token's hash suffix written on its own (2026-10-15): "Client99_1a2b" was read as a
        # name and a qualifier, and "1a2b" alone went to files and commits. Whole words only, with a
        # letter in them (not "2001"), and never an ordinary word ("face").
        if getattr(self, "_suffixes_re", None) is None:
            from .detect import dictionary_words      # here, not at the top: detect imports mapping
            ordinary = dictionary_words()
            suffixes = {f.token.rsplit("_", 1)[1] for f in self.forms if JOINED_SUFFIX_RE.match(f.token)}
            suffixes = {s for s in suffixes if re.search(r"[a-f]", s, re.IGNORECASE) and s.lower() not in ordinary}
            self._suffixes_re = (re.compile(r"(?<![A-Za-z0-9_%])(?:" + "|".join(sorted(suffixes)) + r")(?![A-Za-z0-9_])",
                                            re.IGNORECASE) if suffixes else None)
        if self._suffixes_re is not None:
            found += [m.group(0) for m in self._suffixes_re.finditer(text)]
        return found

    @property
    def stream_re(self) -> re.Pattern[str]:
        """What a stream must never cut in half: the reverse pattern, bracketed forms included."""
        return self._reverse_pattern()

    @property
    def stream_hold(self) -> int:
        """The longest thing stream_re matches."""
        return self.max_token_len + BRACKET_LEN

    def _restored(self, matched: str, counter: Counter[str] | None, escape: bool = False) -> str:
        """The real for a matched token, in the form the token was written; counted as restored.
        `[CC]lient04` is not a case-insensitive class, so it comes back exactly as it was."""
        hidden = (self._rev_disguised.get(matched) or self._rev_disguised_fold.get(matched.lower())
                  if matched not in self._rev else None)
        if hidden is not None:
            # An escaped token: its real, escaped as the token was (and JSON-escaped when the token
            # was written inside JSON text).
            token, style, as_json = hidden
            f = self.backward(token)
            if counter is not None:
                counter[f.client] += 1
                pairs = getattr(counter, "pairs", None)
                if pairs is not None:
                    pairs[(f.client, f.real, matched)] += 1
            out = style(f.real)
            return _json_escape(out) if (as_json or escape) else out
        base = self._abbrev.get(matched.lower())
        if base is not None and matched not in self._rev:
            # An abbreviation (D117): the token's real, counted under the abbreviation so the
            # substitution log says what was written.
            lower = base.lower()
            token = lower if matched[:1].islower() and lower in self._rev else base
            f = self.backward(token)
            if counter is not None:
                counter[f.client] += 1
                pairs = getattr(counter, "pairs", None)
                if pairs is not None:
                    pairs[(f.client, f.real, matched)] += 1
            return _json_escape(f.real) if escape else f.real
        parts = _bracket_parts(matched)
        if parts is not None and parts[1][0] == parts[1][1]:
            return matched
        token = matched
        if parts is not None:
            # Either letter of the class makes the token, and a client can declare both spellings
            # ("Client04" -> ACME, "client04" -> acme). The class means "either case", so the
            # capital is tried first whichever way round it was written: "[cC]" is "[Aa]CME" too.
            rest = parts[0][1:]
            first = parts[1][0]
            token = next((c + rest for c in (first.upper(), first.lower()) if c + rest in self._rev),
                         parts[0])
        f = self.backward(token)
        if counter is not None:
            counter[f.client] += 1
            pairs = getattr(counter, "pairs", None)
            if pairs is not None:
                pairs[(f.client, f.real, f.token)] += 1
        real = _json_escape(f.real) if escape else f.real
        return _bracket_real(real) if parts else real

    # Both fall back to the forward pattern when no real needs escaping, which is the common case:
    # the escaped spelling of every real is the real itself, so the tables are identical.
    @property
    def _fwd_json_re(self) -> re.Pattern[str]:
        if not self._escapes:
            return self._fwd_re
        if self._fwd_json_re_cache is None:
            self._fwd_json_re_cache = self._compile(self._fwd_json, "real")
        return self._fwd_json_re_cache

    @property
    def _canary_re(self) -> re.Pattern[str]:
        if not self._escapes:
            return self._fwd_re
        if self._canary_re_cache is None:
            self._canary_re_cache = self._compile(self._canary, "real")
        return self._canary_re_cache

    def tokenise(self, text: str, counter: Counter[str] | None = None) -> str:
        pairs = getattr(counter, "pairs", None)

        def repl(m: re.Match[str]) -> str:
            disguised = self._disguise(m.group(0))
            f = disguised[0] if disguised else self.forward(m.group(0))
            # A form with a matching rule is still MATCHED here - the pattern is the unrestricted
            # one, and has to stay that way so detection, the near-miss path and the canary all
            # see it. The rule decides only whether this particular occurrence is replaced
            # (feature 23). Left alone, the text goes back out exactly as it came in.
            if f.match is not None and not rule_allows(f.match, m.string, m.start(), m.end(), f.real):
                return m.group(0)
            if counter is not None:
                counter[f.client] += 1
                if pairs is not None:
                    pairs[(f.client, f.real, f.token)] += 1
            # An escaped spelling gets the token escaped the same way (DISGUISES).
            return disguised[1](f.token) if disguised else f.token

        return self._fwd_re.sub(repl, text)

    def rehydrate(self, text: str, counter: Counter[str] | None = None, mark: bool = False) -> str:
        """Tokens back to real names. With `mark`, each restored name is wrapped so the reader can
        see what the filter did - only ever for text a person reads, never for tool arguments."""
        def repl(m: re.Match[str]) -> str:
            if not self._abbreviation_fits(m):
                return m.group(0)
            out = self._restored(m.group(0), counter)
            return f"{MARK_OPEN}{out}{MARK_CLOSE}" if mark and out != m.group(0) else out

        return self._reverse_pattern().sub(repl, text)

    def collapses(self, text: str) -> list[tuple[list[str], str]]:
        """Different spellings in `text` that come back as the same real name: (spellings, real).

        The bug report of 2026-10-14: a "rename" in one command held the misspelt name and the
        intended one - two different strings to the model - and both were written to disk as the
        client's one real name, so writing the new file overwrote the old and deleting the old
        deleted the only copy. Read on the arguments as the model wrote them (tokens), before they
        are restored; nothing is counted."""
        restored: dict[str, set[str]] = {}
        for m in self._reverse_pattern().finditer(text):
            if not self._abbreviation_fits(m):
                continue
            real = self._restored(m.group(0), None)
            if real != m.group(0):
                restored.setdefault(real, set()).add(m.group(0))
        return [(sorted(spellings), real) for real, spellings in restored.items() if len(spellings) > 1]

    def _abbreviation_fits(self, m: re.Match[str]) -> bool:
        """A token matches glued inside a longer word (DECISIONS 36); an abbreviation does not - it is
        short, and with a letter or digit in front it is far more likely to be some other code than
        a token shortened (D117, security review). Anything that is not an abbreviation fits."""
        matched = m.group(0)
        if matched in self._rev or matched.lower() not in self._abbrev:
            return True
        before = m.string[m.start() - 1] if m.start() else ""
        if EMAIL_LOCAL_TOKEN.fullmatch(matched.lower()):
            # A mailbox part stands alone (D170): not the start of a longer code, nor of an address
            # the model wrote with some other domain.
            after = m.string[m.end()] if m.end() < len(m.string) else ""
            return not before.isalnum() and not (after.isalnum() or after == "@")
        return not before.isalnum()

    def tokenise_json_text(self, text: str, counter: Counter[str] | None = None) -> str:
        """Tokenise text that is itself JSON (tool-call arguments): plain and escaped reals."""
        pairs = getattr(counter, "pairs", None)

        def repl(m: re.Match[str]) -> str:
            key = m.group(0)
            # An escaped spelling as written, or as JSON writes it (a regex's backslash doubled).
            disguised = self._disguise(key)
            in_json = None if disguised else self._disguised_json.get(key)
            hidden = disguised or in_json
            f = hidden[0] if hidden else (self._fwd_json.get(key) or self._fwd_json.get(_json_escape(key))
                                          or self.forward(key))
            if f.match is not None and not rule_allows(f.match, m.string, m.start(), m.end(), f.real):
                return key
            if counter is not None:
                counter[f.client] += 1
                if pairs is not None:
                    pairs[(f.client, f.real, f.token)] += 1
            if disguised:
                return disguised[1](f.token)
            if in_json:
                return _json_escape(in_json[1](f.token))
            return f.token

        return self._fwd_json_re.sub(repl, text)

    def rehydrate_json_text(self, text: str, counter: Counter[str] | None = None) -> str:
        """Rehydrate text that is itself JSON: each real goes in escaped, so the JSON stays valid."""
        return self._reverse_pattern().sub(
            lambda m: self._restored(m.group(0), counter, escape=True) if self._abbreviation_fits(m) else m.group(0),
            text)

    def _near_index(self) -> dict[int, list[tuple[str, Form]]]:
        """Single-word forms by the length of their shape, so a near miss is a few lookups."""
        index = getattr(self, "_near_cache", None)
        if index is None:
            index = {}
            for form in self.forms:
                if form.real.isalnum() and len(form.real) >= 7:
                    index.setdefault(len(ocr_shape(form.real)), []).append((form.real, form))
            self._near_cache = index
        return index

    def near_miss(self, word: str) -> "Form | None":
        """The form this word is almost certainly a misreading of, or None. An exact match is not a
        near miss: the mapper has already replaced those."""
        if len(word) < 8 or not word.isalnum():
            return None
        low = word.lower()
        if low in self._fold or low in self._rev_fold:
            return None
        shaped = len(ocr_shape(low))
        index = self._near_index()
        for length in (shaped - 1, shaped, shaped + 1):
            for real, form in index.get(length, ()):
                if near_miss_match(low, real):
                    return form
        return None

    def fix_near_misses(self, text: str, counter: Counter[str] | None = None) -> str:
        """Replace words that are one letter away from a mapped identifier with its token, so a
        misreading does not walk past the filter (review: text read off the screen)."""
        return self._near_misses(text, counter, to_real=False)

    def repair_near_misses(self, text: str, counter: Counter[str] | None = None) -> str:
        """The same matching, correcting the misreading to the form it is a misreading OF.

        This is for text a converter has just produced, before anything has been tokenised:
        putting the real form back means ordinary tokenisation then replaces it, so one code path
        does the substituting and the evidence line reads like any other. Replacing straight to
        the token would leave the rest of the body a step behind it.
        """
        return self._near_misses(text, counter, to_real=True)

    def _near_misses(self, text: str, counter: Counter[str] | None, to_real: bool) -> str:
        pairs = getattr(counter, "pairs", None)

        def repl(m: re.Match[str]) -> str:
            form = self.near_miss(m.group(0))
            if form is None:
                return m.group(0)
            if counter is not None:
                counter[form.client] += 1
                if pairs is not None:
                    pairs[(form.client, m.group(0), form.token)] += 1
            return form.real if to_real else form.token

        return _WORD_RUN.sub(repl, text)

    def real_forms(self, text: str) -> list[Form]:
        """The canary's matches as forms, in order."""
        return [f for f, _start, _end in self.real_spans(text)]

    def real_spans(self, text: str) -> list[tuple[Form, int, int]]:
        """The canary's matches with where each sits in `text`, for showing a refusal in context."""
        out = []
        for m in self._canary_re.finditer(text):
            key = m.group(0)
            f = self._canary_form(key)
            if f is not None and not _declined(f, m):
                out.append((f, m.start(), m.end()))
        return out

    def find_real(self, text: str) -> list[str]:
        """Canary: which client ids still have a real form present in text, plain or JSON-escaped
        (once for a string in the body, twice for JSON inside a string such as tool arguments)."""
        found = set()
        for m in self._canary_re.finditer(text):
            key = m.group(0)
            f = self._canary_form(key)
            if f is not None and not _declined(f, m):
                found.add(f.client)
        return sorted(found)

    def rule_declined(self, text: str) -> list[tuple[Form, str]]:
        """(form, text as found) for each occurrence a match rule declined to replace.

        The canary does not count these (DECISIONS 39): the rule said this occurrence is not the
        client. They are reported instead, so what a rule lets through is seen, never hidden.
        """
        if not any(f.match is not None for f in self.forms):
            return []
        out = []
        for m in self._canary_re.finditer(text):
            key = m.group(0)
            f = self._canary_form(key)
            if f is not None and _declined(f, m):
                out.append((f, key))
        return out

    # ---- loading ----------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict, build: bool = True) -> "Mapper":
        """`build=False` validates and stops before the substitution tables are compiled: what a
        check before a write needs, at a fraction of the cost (feature 57, DECISIONS 58)."""
        if data.get("version") != 1:
            raise MappingError("mapping version must be 1")
        forms: list[Form] = []
        clients = data.get("clients", [])
        # Global wins on this machine: a client entry that Global also has (any case) is left out
        # here but kept in the client's file for teammates. Two clients never share a string.
        global_reals = {str(f.get("real", "")).lower() for c in clients if c.get("id") == GLOBAL_ID
                        for f in c.get("forms", []) if isinstance(f, dict)}
        owner: dict[str, str] = {}
        shadowed: set[tuple[str, str]] = set()
        for client in clients:
            cid = client.get("id", "")
            if cid == GLOBAL_ID:
                continue
            for f in client.get("forms", []):
                if not isinstance(f, dict):
                    continue
                real = str(f.get("real", ""))
                low = real.lower()
                if low in global_reals:
                    shadowed.add((cid, real))
                elif owner.setdefault(low, cid) != cid:
                    raise MappingError(f"{real!r} is in both {owner[low]} and {cid}")
        for client in clients:
            cid = client.get("id", "")
            tokens = client_tokens(client)
            client_forms = [Form(str(f["real"]), tokens[str(f["real"])], cid,
                                 str(f.get("origin") or ""), parse_match(f.get("match")))
                            for f in client.get("forms", [])
                            if (cid, str(f["real"])) not in shadowed]
            if client.get("expand_case"):
                client_forms = _expand_case(client_forms)
            forms.extend(client_forms)
        mapper = cls(forms, build=build)
        mapper.shadowed = shadowed
        return mapper

    @classmethod
    def load(cls, path: str | Path) -> "Mapper":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise MappingError(f"mapping file not found: {p}") from exc
        except json.JSONDecodeError as exc:
            raise MappingError(f"mapping file is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


class MapperHandle:
    """Holds the current Mapper and reloads it when any mapping input changes.

    Inputs are client-map.json, clients/*.json and ignore-list.json (see store.py). Loading is
    tolerant: a client file that fails validation falls back to its last-good copy (on disk under
    state_dir, else in memory), so one bad edit degrades only that client. A client with no good
    copy is `broken`; the proxy then refuses model traffic and the UI shows where to fix it.
    """

    def __init__(self, path: str | Path, clients_dir: str | Path | None = None,
                 state_dir: str | Path | None = None):
        from .store import MappingStore  # local import: store imports mapping

        self.path = Path(path)
        self.store = MappingStore(self.path, Path(clients_dir) if clients_dir else None,
                                  Path(state_dir) if state_dir else None)
        self.reloads = 0
        # Reals this install settled into Global because two clients claimed them, kept so
        # the page can say what was done rather than leaving it to be noticed.
        self.promoted: list[tuple[str, list[str]]] = []
        self.reports: dict = {}
        self.broken: list[str] = []
        self.last_error: str | None = None
        self._sig = self.store.signature()
        self._reload()

    def _reload(self) -> None:
        # A client file is a shared thing: somebody else's environment cannot know what our other
        # clients hold, so their file can arrive with a real we already have elsewhere. Settling it
        # in Global first means the load below shadows both copies and locks both rows, and exactly
        # one token is ingested - rather than one client silently losing to the other while its
        # page still shows a token the proxy never uses.
        try:
            # Kept, not replaced: promoting writes to Global, which changes the signature and makes
            # the very next reload find nothing left to promote. Replacing the list would erase the
            # record of what was just done before anything could report it.
            for entry in self.store.promote_duplicates():
                if entry not in self.promoted:
                    self.promoted.append(entry)
        except Exception:                      # never let housekeeping stop a reload
            pass
        loaded, reports = self.store.load_tolerant()
        mapper = Mapper.from_dict(loaded.data)
        mapper.ignore = loaded.ignore
        mapper.email_rules = email_rules_of(loaded.data.get("clients", []))
        mapper.variant_sink = self.store.record_variant
        self.mapper = mapper
        self.reports = reports
        self.broken = sorted(cid for cid, r in reports.items() if "broken" in r.state)
        errors = [f"{cid}: {r.error['message']}" for cid, r in reports.items() if r.error]
        self.last_error = "; ".join(errors) or None

    def current(self) -> Mapper:
        # No cache or TTL in front of signature(): a client file changing on disk must be seen at
        # once, including one arriving through OneDrive from a colleague, and that is what the
        # hot-reload tests pin. A 2s TTL was tried on 2026-09-26 and backed out - signature()
        # measures 0.4ms, the status poll is ten seconds apart so a short TTL never collapses it,
        # and the cost this entry was chasing turned out to be _reload() rather than the walk.
        sig = self.store.signature()
        if sig != self._sig:
            self._sig = sig
            self._reload()
            self.reloads += 1
        return self.mapper
