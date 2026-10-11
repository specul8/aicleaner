"""Learning-mode detection of unmapped identifiers in outbound traffic.

Runs on the already-tokenised outbound body, so anything that still looks like an
identifier is by definition not in the mapping. Structural detectors (emails,
tenant/SharePoint hosts, other domains, GUIDs next to tenant/subscription keys,
DevOps org/project slugs, resource names that carry a mapped client name) are high
confidence; heuristics (recurring capitalised phrases, a client name inside an ordinary
word) are low. Identifiers that are the same in every tenant (built-in role and policy
ids, Microsoft first-party app ids, vendor domains) are never candidates: see
data/public-identifiers.json. Nothing is blocked: candidates are queued for review in /ui,
and the user is told there is something to review.

Storage, next to the mapping file:
  candidates.jsonl   one line per candidate: value, kind, confidence, count,
                     first/last seen, where it was seen (dialect, path)
  ignore-list.json   {"ignore": ["value", ...]}  values the user said are not identifiers
Neither file contains surrounding content, only the candidate strings themselves.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import fileio
from .mapping import (FOUND_ORIGINS, GLOBAL_ID, IPV6_STAND_IN_BLOCK, Mapper, classify, ipv6_token, is_ipv6,
                      is_real_client)

log = logging.getLogger("aicleaner.detect")

# Each of these starts only where an identifier can start, and matches a label atomically, so a
# long run of dots cannot make it retry from every character: the domain pattern took seconds on a
# fetched page and grew with the square of its size (H12).
RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@(?:[A-Za-z0-9-]++\.)+[A-Za-z]{2,}\b")
# Tried once per run of name characters, from its start, and never backed into (H12). `\b` let a
# match start at every hyphen inside a long run, each attempt rescanning it to the end: 3 s for
# 16 KB of "a-a-a-". A `-my` personal site is part of the run, so it is still matched whole.
RE_TENANT = re.compile(r"(?<![a-z0-9-])[a-z0-9-]++\.onmicrosoft\.com\b", re.IGNORECASE)
RE_SHAREPOINT = re.compile(r"(?<![a-z0-9-])[a-z0-9-]++\.sharepoint\.com\b", re.IGNORECASE)
# Real top-level domains, including ones that are also file extensions (.tf, .md, .py): those need
# a scheme, www., @ or three or more labels before they count as a host (see _domain_has_context).
_TLDS = ("com|net|org|edu|gov|mil|int|info|biz|io|ai|app|dev|cloud|co|tv|online|site|tech|digital|"
         "group|systems|solutions|services|consulting|partners|agency|works|team|zone|email|host|"
         "au|nz|uk|us|ca|de|fr|es|it|nl|se|no|dk|fi|ie|sg|hk|jp|cn|in|my|ph|th|vn|za|ae|sa|pl|cz|"
         "at|ch|be|pt|gr|tr|il|br|mx|ar|cl|pe|id|me|tf|md|py|sh|ps|rs|cc|so|zip|mov|ml|"
         # RFC 2606 reserves .example for documentation and tests. It is a real top level,
         # and without it an address at one reads as not-an-address at all.
         "example")
RE_DOMAIN = re.compile(r"(?<![\w.-])(?:[a-z0-9-]++\.)+(?:" + _TLDS + r")\b", re.IGNORECASE)
RE_GUID_KEYED = re.compile(
    r"(?:tenantId|tenant_id|homeTenantId|subscriptionId|subscription_id|\\?\"id\\?\"|tenant|subscription)\\?[\"']?\s*[:=]\s*\\?[\"']?"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
    re.IGNORECASE,
)
# Four numbers and exactly four: \b does not stop at a dot, because a dot is not a word character,
# so the plain pattern walked into the middle of any longer dotted run. The certificate EKU
# 1.3.6.1.5.5.7.3.3 came out as the two "addresses" 1.3.6.1 and 5.5.7.3, neither of which is
# anything. The lookarounds refuse a run that continues with another number on either side, while
# still allowing a full stop that ends a sentence.
RE_IPV4 = re.compile(r"(?<![.\d])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\.?\d)")
# IPv6, which nothing looked for until now. That is the inverse of the private-IPv4 problem and the
# more serious half: a global IPv6 address is routable and traceable to an organisation, and it was
# going out untouched. Only forms with a run of at least two groups, so a bare "::" or a time like
# 10:30 is not an address.
RE_IPV6 = re.compile(
    r"(?<![:.\w])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"           # full
    r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,6})?"   # compressed
    r")(?![:.\w])")
# Words that turn 1.2.3.4 from an address into a version. The four numbers cannot tell you which it
# is - both shapes are identical - so the only evidence is what sits beside them. A client mapping
# built by scanning a repository was ~40% assembly and package versions replaced by fake addresses,
# which does not protect anybody and makes a conversation about upgrading a module read as one
# about a host.
_VERSION_WORDS = (r"versions?|ver\.?|assembly|module|runtime|sdk|package|release|build|schema|"
                  r"requires?|installed|upgrade|downgrade|semver|tag")
# Immediately in front of the number: "version 1.2.3.4", '"version": "1.2.3.4"', ">= 1.2.3.4", "v1.2.3.4".
RE_VERSION_CONTEXT = re.compile(
    rf"(?:{_VERSION_WORDS})\b[\s:=\"']*$"
    r"|[><=~^]=?\s*$"
    r"|\bv$",
    re.IGNORECASE)
# Anywhere in sight, for the second number of "from A to B" - unanchored, so the word may sit
# behind the first number rather than immediately before this one.
RE_VERSION_NEARBY = re.compile(rf"\b(?:{_VERSION_WORDS})\b", re.IGNORECASE)


def reads_as_version(text: str, start: int, end: int) -> bool:
    """Whether the four numbers at this position are a version rather than an address.

    Decided by what precedes them, and by an immediately following version-ish suffix: "1.2.3.4-rc1"
    is never a host. Nothing here looks at the numbers themselves, because the numbers cannot say.
    """
    before = text[max(0, start - 40):start]
    if RE_VERSION_CONTEXT.search(before):
        return True
    # "Chrome/148.0.0.0", "Safari/537.36": a name, a slash, then the number. A URL would have a
    # scheme or a dot in the host before the slash, so requiring a bare word keeps http://1.2.3.4/
    # and host/1.2.3.4 as addresses.
    if re.search(r"(?:^|[\s(\[,;])[A-Za-z][A-Za-z0-9_+-]*/$", before):
        return True
    # "netfxca.dll 4.0.5.0", "quartz.dll\t3.13.1.0": the thing being versioned is right there, and
    # a file name is never followed by an address.
    if re.search(r"\.(?:dll|exe|sys|msi|msu|cab|ocx|drv|nupkg|whl|jar|so)['\"`]?[\s\t]+$", before, re.IGNORECASE):
        return True
    # A column of its own on a row that also carries a file size: a loaded-module listing, which is
    # "name <tab> version <tab> 945.13 KB (967,808 bytes) <tab> ... <tab> path". The version word is
    # in a header thousands of lines away, so the row's own shape has to be the signal. An address
    # is never a tab-delimited field sitting next to a byte count.
    if before.endswith("\t") and text[end:end + 1] == "\t":
        if re.match(r"\t[^\t\n]{0,40}\b(?:bytes|[KMG]B)\b", text[end:end + 60], re.IGNORECASE):
            return True
    # "from 1.0.0.3 to 1.101.109.0": the second number is a version too, but the word that says so
    # is behind the first one. Only a joining word counts, and only with a version word still in
    # sight, so an address that merely follows the word "version" in a sentence is not swallowed.
    if re.search(r"\b(?:to|through|until|and)\s+$|[-.]{2,}\s*$|->\s*$", before, re.IGNORECASE):
        if RE_VERSION_NEARBY.search(text[max(0, start - 90):start]):
            return True
    return bool(re.match(r"[-+][A-Za-z0-9.]+", text[end:end + 12]))


def identifies_somebody(value: str) -> bool:
    """Whether an address could point at anybody in particular.

    A private address identifies nobody by definition: it is not routable, and a very large number
    of networks use the same one. The same goes for loopback, link-local, carrier NAT, documentation
    ranges and IPv6 unique-local. Holding those back costs a review decision and buys nothing.
    """
    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified
                or address in ipaddress.ip_network("100.64.0.0/10")
                or any(address in ipaddress.ip_network(doc) for doc in
                       ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")))
NOISE_IPS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "1.1.1.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "168.63.129.16", "169.254.169.254"}
RE_DEVOPS = re.compile(r"dev\.azure\.com/([A-Za-z0-9._-]+)(?:/([A-Za-z0-9._%-]+))?")

# Resource names: Azure's usual shape is <type>-<region|environment>-<purpose>, separated or run
# together. The parts below are what makes a slug a resource name rather than a code identifier;
# measured against a real client repo, the shape alone is a good signal and everything else
# (Write-Host, azurerm_*, MSFT_* DSC types) is not worth queueing.
RESOURCE_PREFIXES = {
    "rg", "vnet", "snet", "nsg", "pip", "nic", "vm", "vmss", "st", "sa", "kv", "law", "log", "appi",
    "agw", "afw", "fw", "lb", "bas", "udr", "rt", "pe", "pep", "asp", "app", "func", "sql", "sqldb",
    "cosmos", "aks", "acr", "mi", "id", "uami", "evh", "sb", "apim", "des", "disk", "osdisk", "rsv",
    "bvault", "avd", "hp", "dnspr", "pdns", "dns", "vgw", "vpng", "ergw", "lgw", "conn", "mg", "sg",
    "sgd", "sga", "psc", "dcr", "dce", "amw", "wks", "ws", "aa", "rb", "srv", "host",
}
RESOURCE_REGIONS = {
    "aue", "ause", "auc", "aus", "ase", "eus", "eus2", "wus", "wus2", "cus", "neu", "weu", "uks",
    "ukw", "sea", "eas", "jpe", "ind", "australiaeast", "australiasoutheast", "eastus", "westus",
    "northeurope", "westeurope",
}
RESOURCE_ENVS = {
    "prd", "prod", "dev", "devtest", "tst", "test", "uat", "stg", "stage", "staging", "np",
    "nonprod", "shd", "shared", "mgmt", "hub", "sbx", "poc", "dr", "pilot", "live",
}
# Parts long enough to look for inside a run-together name without matching ordinary words.
_NAMING_PARTS = tuple(sorted(RESOURCE_PREFIXES | RESOURCE_REGIONS | RESOURCE_ENVS, key=len, reverse=True))
RE_SEGMENTED = re.compile(r"(?<![\w.-])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![\w-])")
RE_RUN = re.compile(r"(?<![\w.-])[A-Za-z0-9]{6,}(?![\w-])")
# Bounded on purpose. A run of letters and digits carrying a client's name is a resource name, and
# no resource name is 3,205 characters - but a minified line, a base64 blob or a long hash is, and
# one containing a client's name was collected whole. It then carried a digit, so it read as a resource,
# was auto-mapped, and the trie could not be compiled from it at all: RecursionError, the client
# broken, and a broken client refuses model traffic for EVERY client. The longest thing Azure lets
# you name is 90 characters; 128 leaves room and still excludes anything that is not a name.
MAX_RESOURCE_RUN = 128
RE_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_RUN_TOGETHER = re.compile(
    "^(?:{p})(?:(?:{r})(?:{e})|(?:{e})(?:{r}))[a-z0-9]*$".format(
        p="|".join(sorted(RESOURCE_PREFIXES, key=len, reverse=True)),
        r="|".join(sorted(RESOURCE_REGIONS, key=len, reverse=True)),
        e="|".join(sorted(RESOURCE_ENVS, key=len, reverse=True))),
    re.IGNORECASE)
# Vocabulary: provider types, Intune/DSC setting names, PowerShell cmdlets and Windows event
# channels are public words, not resource names.
RE_VOCABULARY = re.compile(
    r"^(?:azurerm|azuread|azapi|azuredevops|random|null|time|tls|local|var|data|each|self|module|MSFT)_"
    r"|_vendor_msft_"
    r"|^(?:Microsoft|Windows|System|MSFT)-"
    r"|^(?:Add|Approve|Assert|Backup|Build|Clear|Close|Compare|Complete|Confirm|Connect|Convert|ConvertFrom|ConvertTo|Copy|Debug|Deny|Disable|Disconnect|Dismount|Edit|Enable|Enter|Exit|Expand|Export|Find|Format|Get|Grant|Group|Hide|Import|Initialize|Install|Invoke|Join|Limit|Lock|Measure|Merge|Mount|Move|New|Open|Optimize|Out|Ping|Pop|Protect|Publish|Push|Read|Receive|Redo|Register|Remove|Rename|Repair|Request|Reset|Resize|Resolve|Restart|Restore|Resume|Revoke|Save|Search|Select|Send|Set|Show|Skip|Split|Start|Step|Stop|Submit|Suspend|Switch|Sync|Test|Trace|Unblock|Undo|Uninstall|Unlock|Unprotect|Unpublish|Unregister|Update|Use|Wait|Watch|Write)-",
    re.IGNORECASE)

PUBLIC_PATH = Path(__file__).resolve().parent / "data" / "public-identifiers.json"
COMMON_WORDS_PATH = Path(__file__).resolve().parent / "data" / "common-words.txt"


@functools.lru_cache(maxsize=1)
def common_words() -> frozenset[str]:
    """Ordinary English words (SCOWL's most common; rebuilt by tools/build_common_words.py). They
    are never watched as a client's name and never queued for review on their own."""
    try:
        lines = COMMON_WORDS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(w.strip() for w in lines if w.strip() and not w.startswith("#"))


def is_common_word(value: str) -> bool:
    return value.lower() in common_words()


AUTOCORRECT_WORDS_PATH = Path(__file__).resolve().parent / "data" / "autocorrect-words.txt"
# Vendor and product words a phrase may carry without saying anything about who a client is.
PRODUCT_WORDS = frozenset({"entra", "intune", "azure", "office", "teams", "sharepoint", "exchange", "defender",
                           "sentinel", "keyvault", "terraform", "powershell", "windows", "microsoft", "licencing",
                           "licensing", "github", "devops", "onedrive", "outlook", "copilot", "fortigate",
                           "fortinet", "applocker", "bitlocker", "autopilot", "purview", "graph", "wiki"})


@functools.lru_cache(maxsize=1)
def dictionary_words() -> frozenset[str]:
    """Every word in the larger SCOWL list (feature 94's autocorrect list): ordinary English words of
    any commonness, for telling an ordinary phrase from one holding something distinctive."""
    try:
        lines = AUTOCORRECT_WORDS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(line.split("\t", 1)[0] for line in lines if line and not line.startswith("#"))


def phrase_adds_nothing(phrase: str, mapper: Mapper) -> bool:
    """True when every word of a phrase is a client's token, an ordinary English word or a product
    word (D149): "Set Client42", "Client42 Key Vault". Learning such a phrase as a form added nothing -
    the client's own word is replaced anyway - and filled clients with dozens of sentence openings.
    A phrase with anything else in it (a typo, an unusual name) still goes to Review."""
    words = dictionary_words() | common_words()
    tokens = {f.token.lower() for f in mapper.forms}
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", phrase):
        low = word.lower()
        if low in tokens or low in words or low in PRODUCT_WORDS or low in GENERIC_WORDS:
            continue
        if any(low.startswith(t) and low[len(t):] in ("", "s", "'s") for t in tokens):
            continue
        return False
    return True


@functools.lru_cache(maxsize=1)
def public_identifiers() -> tuple[frozenset[str], frozenset[str]]:
    """(guids, domains) that are the same in every tenant, so they identify nobody: built-in role
    and policy ids, Microsoft first-party app ids, Graph permission ids, vendor domains.
    Rebuilt by tools/build_public_identifiers.py."""
    try:
        doc = json.loads(PUBLIC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(), frozenset()
    return (frozenset(str(g).lower() for g in doc.get("guids", [])),
            frozenset(str(d).lower() for d in doc.get("domains", [])))


def is_public(value: str) -> bool:
    """True for an identifier that is public: the same GUID or vendor host in every tenant."""
    guids, domains = public_identifiers()
    low = value.strip().lower()
    if low in guids:
        return True
    # An Azure Private DNS zone (privatelink.blob.core.windows.net) is the same in every tenant. Only
    # the zone itself: a record inside it (stfabrikam.privatelink...) names a client's resource.
    if low.startswith("privatelink.") and low.count(".") >= 3:
        return True
    # A HOST at a vendor is public; a MAILBOX at one is a person. The test ran on the whole string,
    # so "bob@microsoft.com" was not public (no dot before the domain) while
    # "harbour.helpdesk@x.case.salesforce.com" was - the same question answered both ways depending on
    # whether the vendor had a subdomain, and the wrong way round for the one that names somebody.
    if "@" in low:
        return False
    # Walk this value's own suffixes rather than the whole list. The test is the same - the value
    # is a listed domain, or something under one - but asked the other way round: a few set
    # lookups instead of a concatenation and an endswith for each of a couple of hundred entries.
    # It was 72% of a repository scan, because a scan asks this once per find and a find recurs in
    # every file that mentions it.
    if low in domains:
        return True
    cut = low.find(".")
    while cut != -1:
        if low[cut + 1:] in domains:
            return True
        cut = low.find(".", cut + 1)
    return False


# Hosts that are infrastructure, not identity (the rest of the list is in public-identifiers.json).
NOISE_DOMAINS = {"learn.microsoft.com", "docs.microsoft.com", "registry.terraform.io"}


@dataclass
class Candidate:
    value: str
    kind: str
    confidence: str  # "high" | "low"
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    sources: list[str] = field(default_factory=list)
    placeholder: str = ""  # what goes on the wire instead of value until it is mapped or ignored
    # True when this one was meant to be held back and no placeholder could be minted, so the
    # value went out as it is. Every other kind of "not held back" is a decision; this is a
    # failure, and the only trace of it used to be a line in proxy-stderr.log (D12). On a tool
    # whose whole purpose is noticing what escapes, the count of what was held back was visible
    # and the count of what FAILED to be held back was not.
    unprotected: bool = False
    client: str = ""       # suggested owner: the client whose form is inside this value
    near: str = ""         # for a near miss: the word of the client's names it is one letter from
    # What it was seen with, so Review can say which engagement it came from: per client, the
    # number of requests it appeared in with that client's names (Global never counts); the
    # Claude Code project folders and Session-page sessions those requests came from.
    seen_with: dict[str, int] = field(default_factory=dict)
    projects: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)


@dataclass
class SeenContext:
    """Where a request came from, for the candidates it produces. Never the text around them."""
    clients: dict[str, int] = field(default_factory=dict)  # replacements per client in the request
    project: str = ""   # the Claude Code working folder's name
    session: str = ""   # the Session page's profile name

    def note(self, c: Candidate) -> None:
        for cid, n in self.clients.items():
            if n > 0 and is_real_client(cid):
                c.seen_with[cid] = c.seen_with.get(cid, 0) + 1
        if self.project and self.project not in c.projects and len(c.projects) < 5:
            c.projects.append(self.project)
        if self.session and self.session not in c.sessions and len(c.sessions) < 5:
            c.sessions.append(self.session)


# Claude Code states its working folder in the system prompt; the folder is usually the engagement.
_WORKING_DIR = re.compile(r"(?:Primary working directory|Working directory|cwd)\s*:\s*([^\r\n]+)", re.IGNORECASE)


def request_project(obj: Any) -> str:
    """The name of the working folder a Claude Code request states, or "" (read before tokenising,
    so the name is the real one: Review is local, and the real name is what a person recognises)."""
    system = obj.get("system") if isinstance(obj, dict) else None
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    if not isinstance(system, str):
        return ""
    m = _WORKING_DIR.search(system)
    if not m:
        return ""
    path = m.group(1).strip().strip("`'\"").rstrip("\\/")
    return re.split(r"[\\/]", path)[-1][:80] if path else ""


# Documentation-only ranges (RFC 5737): never a real host, so a placeholder IP cannot be mistaken
# for, or rehydrate over, a real address.
_TEST_NETS = ("192.0.2", "198.51.100", "203.0.113")


def make_placeholder(value: str, kind: str, taken: set[str]) -> str:
    """A stable stand-in for an unknown identifier, shaped like it, unique among `taken`
    (lower-cased placeholders and tokens). "" when none is free (the value then goes out as-is)."""
    hx = hashlib.sha1(value.lower().encode()).hexdigest()
    used = {t.lower() for t in taken}
    if kind == "ip" and is_ipv6(value):
        # An IPv6 address waits as an IPv6 address (DECISIONS 41): the documentation prefix, in the
        # ffff block that no client index reaches, so a stand-in can never equal a token.
        for n in range(0, 32, 4):
            candidate = ipv6_token(IPV6_STAND_IN_BLOCK, (hx[n:] + hx)[:8])
            if candidate.lower() not in used:
                return candidate
        return ""
    if kind == "ip":
        slots = len(_TEST_NETS) * 254
        start = int(hx[:6], 16) % slots
        for i in range(slots):
            slot = (start + i) % slots
            candidate = f"{_TEST_NETS[slot // 254]}.{slot % 254 + 1}"
            if candidate not in used:
                return candidate
        return ""
    for n in (6, 8, 10, 12, 16):
        h = hx[:n]
        if kind == "email":
            candidate = f"pending-{h}@pending.invalid"
        elif kind == "tenant":
            candidate = f"pending-{h}.onmicrosoft.invalid"
        elif kind == "sharepoint":
            candidate = f"pending-{h}.sharepoint.invalid"
        elif kind == "domain":
            candidate = f"pending-{h}.invalid"
        elif kind == "guid":
            candidate = f"00000000-0000-4000-9000-{hx[n - 6:n + 6]}"
        elif kind == "name":
            candidate = f"Pending {h.upper()}"
        else:  # devops-org, devops-project and anything else identifier-shaped
            candidate = f"pending-{h}"
        if candidate.lower() not in used:
            return candidate
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _domain_is_noise(host: str) -> bool:
    h = host.lower()
    return is_public(h) or any(h == d or h.endswith("." + d) for d in NOISE_DOMAINS)


def _camel_hump(run: str, start: int, end: int) -> bool:
    """True when run[start:end] is its own camel-case word (ClientOpsAudit -> Client, Ops, Audit)."""
    if not run[start:end][:1].isupper():
        return False
    before_ok = start == 0 or run[start - 1].islower() or run[start - 1].isdigit()
    after_ok = end == len(run) or run[end].isupper() or run[end].isdigit()
    return before_ok and after_ok


def _all_naming_parts(text: str) -> bool:
    """True when text is made only of Azure naming parts (type, region, environment) and digits:
    the leftovers of a resource name once the client form is taken out ("dev", "stprd", "01")."""
    rest = text
    while rest:
        for part in _NAMING_PARTS:
            if rest.startswith(part):
                rest = rest[len(part):]
                break
        else:
            if rest[0].isdigit():
                rest = rest[1:]
                continue
            return False
    return True


def _looks_like_a_resource(run: str, start: int, end: int) -> bool:
    """A run that contains a mapped form is a resource name, rather than an ordinary word that
    happens to contain it ("tailwinds" ends in a plural, and also contains the region code
    "ind"), when the form is its own camel-case word, the run carries a digit, the run has Azure
    naming shape, or what is left either side of the form is made only of naming parts."""
    if _camel_hump(run, start, end):
        return True
    if any(ch.isdigit() for ch in run):
        return True
    if is_resource_name(run):
        return True
    low = run.lower()
    before, after = low[:start], low[end:]
    return bool(before and _all_naming_parts(before)) or bool(after and _all_naming_parts(after))


def contained_forms(text: str, mapper: Mapper) -> Iterable[tuple[str, str, bool]]:
    """(run, client, looks_like_a_resource) for every run of letters and digits that contains a
    mapped form but is not that form: whole-word matching never replaces these, so they reach the
    provider in clear (e.g. a storage account named after the client)."""
    pattern, owners = _needles(mapper)
    if pattern is _NEVER:
        return
    for hit in pattern.finditer(text):
        run_match = RE_ALNUM_RUN.search(text, *_run_bounds(text, hit.start()))
        if run_match is None:
            continue
        run = run_match.group(0)
        if len(run) > MAX_RESOURCE_RUN:
            continue  # a blob, a hash or a minified line, not something anybody named
        if run.lower() == hit.group(0).lower():
            continue  # the form itself: the mapper already replaced it
        found = hit.group(0)
        if found.lower() in ACRONYMS and not (found.isupper() or found.islower()):
            continue  # an acronym's letters in mixed case: another word (NetBios), D149
        before = text[run_match.start() - 1:run_match.start()]
        after = text[run_match.end():run_match.end() + 1]
        if before == "." or after == "." or before == "@":
            continue  # a label inside a host or email: the domain detectors own those shapes
        start = hit.start() - run_match.start()
        yield run, owners.get(hit.group(0).lower(), ""), _looks_like_a_resource(run, start, start + len(hit.group(0)))


# Words that say nothing about who a client is, so they are never looked for inside longer words.
GENERIC_WORDS = {
    "inc", "ltd", "pty", "corp", "corporation", "company", "limited", "group", "holdings",
    "services", "service", "technologies", "technology", "solutions", "systems", "consulting",
    "partners", "australia", "global", "international", "national", "council", "commission",
    "agency", "department", "works", "team", "cloud", "azure", "microsoft", "onmicrosoft",
    "sharepoint", "com", "net", "org", "gov", "edu", "local", "prod", "test", "dev",
    # A vendor's service labels, carried by a client's host forms (D149): ".vault.azure.net" made
    # "vault" a client word, caught inside KeyVault and VaultName; SharePoint's "-admin" site made
    # "admin" one, caught inside LocalAdmin and GraphAsAdmin.
    "vault", "vaultcore", "privatelink", "admin", "blob", "dfs", "database", "servicebus",
    "azurewebsites", "scm", "cloudapp", "windows", "core", "azurecr", "azconfig",
}


def _needles(mapper: Mapper) -> tuple[re.Pattern[str], dict[str, str]]:
    """(pattern, needle -> client) for the words worth looking for inside longer words.

    A client's bare name is often not a form of its own: the mapping holds "Fabrikam Inc" and
    "fabrikam.com", and the resource name in the wild is "stfabrikamprdtfstate". So each form
    also contributes its distinctive words, with generic ones (Inc, Services, com) left out.
    """
    cached = getattr(mapper, "_contained_cache", None)
    if cached is not None:
        return cached
    owners: dict[str, str] = {}
    # The client's own id and label name it by definition, whether or not a form happens to spell
    # them. Added first so they are never crowded out.
    for form in mapper.forms:
        low = form.client.lower()
        if len(low) >= 4 and low.isalnum() and low not in GENERIC_WORDS and low not in common_words():
            owners.setdefault(low, form.client)
    for form in mapper.forms:
        # What a scan or an auto-add FOUND is evidence that something belongs to a client; it is
        # not the client's name, and must not become a word to hunt for inside other words. A pass
        # that collected "TerraformValidation" made "validation" one of this client's words, and
        # the next pass took everything containing it - 233 generic identifiers reached one client
        # before anyone noticed, and the scan's own output had become its vocabulary (D1).
        #
        # An empty origin is every form written before this was recorded: trusted, because those
        # were put there by hand or have already been through Review, and dropping their words
        # would quietly weaken matching on files nobody has touched.
        if form.origin in FOUND_ORIGINS:
            continue
        # An email's mailbox names a person or a service account ("svc_project_backup"), not the
        # client: its words are ordinary ones, and watching them held back "projects" and
        # "packages" as the client's. The domain after the @ is the client's.
        # An opaque identifier has no distinctive words in it, only chunks. Splitting a GUID on its
        # hyphens offered "4ef8" as a name to hunt for, and it duly matched inside "cd54ef8d",
        # which became "cd5client4079d". An address splits the same way and is no better.
        if classify(form.real) in ("guid", "ip"):
            continue
        real = form.real.rsplit("@", 1)[1] if "@" in form.real else form.real
        parts = [real] if real.isalnum() else re.split(r"[^A-Za-z0-9]+", real)
        for part in parts:
            low = part.lower()
            # An ordinary word is not a name to watch for, unless it is what the client is called.
            if low in common_words() and low != form.client.lower():
                continue
            if len(part) >= 4 and part.isalnum() and low not in GENERIC_WORDS and not low.isdigit():
                owners.setdefault(low, form.client)
                if part.isupper() and len(part) <= 6:
                    ACRONYMS.add(low)
    pattern = _NEVER
    if owners:
        pattern = re.compile("|".join(re.escape(n) for n in sorted(owners, key=len, reverse=True)),
                             re.IGNORECASE)
    cached = (pattern, owners)
    mapper._contained_cache = cached  # per mapper; rebuilt whenever the mapping reloads
    return cached


def _run_bounds(text: str, index: int) -> tuple[int, int]:
    """The span of the run of letters and digits around index."""
    start = index
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    end = index
    while end < len(text) and text[end].isalnum():
        end += 1
    return start, end


def is_resource_name(value: str) -> bool:
    """Azure naming shape: <type>-<region|env>-... separated, or run together."""
    if RE_VOCABULARY.search(value) or GUID_LIKE.match(value):
        return False
    parts = re.split(r"[-_]", value)
    if len(parts) > 1:
        return parts[0].lower() in RESOURCE_PREFIXES and any(
            p.lower() in RESOURCE_REGIONS or p.lower() in RESOURCE_ENVS for p in parts[1:])
    return bool(_RUN_TOGETHER.match(value))


_NEVER = re.compile(r"(?!x)x")
# Client words written all in capitals (NWND): inside a longer word they count only in the same
# capitals or in lowercase (stnwndprd), never mixed case (NetNwndName), D149.
ACRONYMS: set[str] = set()
GUID_LIKE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _token_pattern(mapper: Mapper) -> re.Pattern[str] | None:
    """Every token the mapper hands out, as one pattern (cached on the mapper)."""
    cached = getattr(mapper, "_token_pattern_cache", False)
    if cached is not False:
        return cached
    tokens = sorted({f.token for f in mapper.forms if len(f.token) >= 5}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(t) for t in tokens), re.IGNORECASE) if tokens else None
    mapper._token_pattern_cache = pattern
    return pattern


_HOST_SHAPE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
# The shortest stem a pattern entry may have. DECISIONS 25 set four characters as the floor for an
# offered prefix or suffix; one typed by hand is held to the same, or "a*" would silence most of
# the queue.
PATTERN_MIN = 4


def _client_real_pattern(mapper: Mapper) -> re.Pattern[str] | None:
    """Every client's real (Global's excluded), as one pattern (cached on the mapper; feature 76).

    Inside other text, not on word boundaries: the detectors read tokenised text, where a name on
    its own boundaries has already become its token, so what is left carrying a real is a name run
    together with something else - `adminacmevm`. A real shorter than PATTERN_MIN must stand on its
    own, or a two-letter client would protect every word with those letters in it.
    """
    cached = getattr(mapper, "_client_real_cache", False)
    if cached is not False:
        return cached
    reals = sorted({f.real for f in mapper.forms if f.client.lower() != GLOBAL_ID and f.real.strip()},
                   key=len, reverse=True)
    parts = [re.escape(r) if len(r) >= PATTERN_MIN else rf"(?<![a-z0-9]){re.escape(r)}(?![a-z0-9])"
             for r in reals]
    pattern = re.compile("|".join(parts), re.IGNORECASE) if parts else None
    mapper._client_real_cache = pattern
    return pattern


def carries_client_real(value: str, mapper: Mapper | None) -> bool:
    """True for a value with a client's name in it - which no wildcard or parent ignore may cover."""
    if mapper is None:
        return False
    pattern = _client_real_pattern(mapper)
    return bool(pattern and pattern.search(value))


def ignored_by(value: str, ignore: Iterable[str], protected: Callable[[str], bool] | None = None) -> bool:
    """True when an ignore list covers this value.

    `protected` (feature 76): a value it says carries a client's name is covered only by an entry
    that is exactly that value. `admin*` accepted for 48 harmless names must not silently swallow
    `admin-<client>-vm`, a new resource named after a client, nor may a parent domain swallow a
    host or mailbox named after one. Asked after the exact check, so the common case costs nothing.

    Three ways an entry covers something:
    - **itself**, as it always did;
    - **a host under it**, when the entry is a domain (D67). Feature 31 offered "ignore the parent
      instead" and removed the host entries, but matching was exact, so the parent covered none of
      them and every one came back to Review. On label boundaries, and hosts only: an address at
      such a host is still its own finding;
    - **a pattern**: `stem*` for a prefix, `*stem` for a suffix (feature 31's other two), with a
      stem of at least PATTERN_MIN characters and something left over after it.
    """
    entries = ignore if isinstance(ignore, (set, frozenset, dict)) else set(ignore)
    v = value.strip().lower()
    if not v or not entries:
        return False
    if v in entries:
        return True
    if protected is not None and protected(value):
        return False
    if "@" in v:
        # An address is covered by its domain (DECISIONS 46): ignoring salesforce.com means a case
        # mailbox there is not a client's either. Until then an address stayed its own finding.
        host = v.rsplit("@", 1)[1]
        labels = host.split(".")
        if any(".".join(labels[i:]) in entries for i in range(len(labels) - 1)):
            return True
    if _HOST_SHAPE.fullmatch(v):
        labels = v.split(".")
        if any(".".join(labels[i:]) in entries for i in range(1, len(labels) - 1)):
            return True
    for entry in entries:
        if entry.endswith("*") and not entry.startswith("*"):
            stem = entry[:-1]
            if len(stem) >= PATTERN_MIN and len(v) > len(stem) and v.startswith(stem):
                return True
        elif entry.startswith("*") and not entry.endswith("*"):
            stem = entry[1:]
            if len(stem) >= PATTERN_MIN and len(v) > len(stem) and v.endswith(stem):
                return True
    return False


def worth_queueing(value: str) -> bool:
    """False for a value that could not be anybody's identifier: under three characters, or with
    no letter or digit in it (D47). Pending rows for `'`, `,` and `.` reached Review with no
    detector shown to be the source - and none needed to be, because nothing on the way into the
    queue checked. So the check is at the queue's entry, where a detector written later cannot
    forget it."""
    stripped = value.strip()
    return len(stripped) >= 3 and any(ch.isalnum() for ch in stripped)


# `name` narrowed to a capitalised phrase that carries one of our tokens (feature 63). The words a
# phrase is made of are the old any-capitalised-phrase detector's, plus a token as a word in its
# own right: tokens carry digits (Client07), so the plain pattern stopped at the token and
# reported only the rest.
_PHRASE_WORD = r"(?:[A-Z][a-z]{2,}|Pty|Ltd|Inc|Corp|Group|Council|Commission|Agency|Department)"
# Words that say what kind of body something is, not which one. A token followed only by these
# ("Client07 Pty Ltd") adds nothing the token does not already hide.
_LEGAL_WORDS = {"Pty", "Ltd", "Inc", "Corp", "Group"}


def _phrase_pattern(mapper: Mapper) -> re.Pattern[str] | None:
    """Capitalised phrases in which a token may stand as a word (cached on the mapper)."""
    cached = getattr(mapper, "_phrase_pattern_cache", False)
    if cached is not False:
        return cached
    tokens = _token_pattern(mapper)
    pattern = None
    if tokens is not None:
        word = rf"(?:(?i:{tokens.pattern})|{_PHRASE_WORD})"
        pattern = re.compile(rf"\b{word}(?:\s{word})+\b")
    mapper._phrase_pattern_cache = pattern
    return pattern


def _phrase_parts(phrase: str, tokens: re.Pattern[str]) -> list[str]:
    """A phrase split into words, with a token of several words ("Client05 Pty") kept as one part.

    Split on spaces alone, a multi-word token was two words neither of which is a token, so every
    client whose name carries a legal suffix ("Fabrikam Inc") was never seen in a phrase at all.
    """
    parts: list[str] = []
    pos = 0
    while pos < len(phrase):
        hit = tokens.match(phrase, pos)
        end = hit.end() if hit else -1
        if hit and (end == len(phrase) or phrase[end] == " "):
            parts.append(hit.group(0))
        else:
            end = phrase.find(" ", pos)
            end = len(phrase) if end < 0 else end
            parts.append(phrase[pos:end])
        pos = end + 1
    return parts


def token_phrases(text: str, mapper: Mapper) -> Iterable[tuple[str, str]]:
    """(phrase, client) for each capitalised phrase that carries one of our tokens (feature 63).

    The phrase-level sibling of `contained_forms`, and the inverse of `carries_token`: a single
    word built round a token is our own output, but in a phrase the token marks an entity already
    known and the rest is the part nobody has mapped - "Client07 Corporation Holdings" is Tailwind's
    longer name. It is reported with each token put back as its real form, because that is what
    can be added to a client; the token form cannot. `client` is the owner when every token in it
    is the same client's, and "" when a phrase joins two.
    """
    pattern = _phrase_pattern(mapper)
    tokens = _token_pattern(mapper)
    if pattern is None or tokens is None:
        return
    by_token = {f.token.lower(): f for f in mapper.forms}
    for m in pattern.finditer(text):
        words = _phrase_parts(m.group(0), tokens)
        forms = [by_token.get(w.lower()) if tokens.fullmatch(w) else None for w in words]
        rest = [w for w, f in zip(words, forms) if f is None]
        if not any(forms) or not any(w not in _LEGAL_WORDS for w in rest):
            continue
        owners = {f.client for f in forms if f is not None}
        # Put back through the mapper, not f.real: the lookup is case-blind, and a case variant's
        # form ("TAILWIND") would otherwise stand in for the word as written.
        yield (" ".join(mapper.rehydrate(w) if f is not None else w for w, f in zip(words, forms)),
               owners.pop() if len(owners) == 1 else "")


def carries_token(value: str, mapper: Mapper | None) -> bool:
    """True for a value that contains one of our own tokens ("ClaudeClient2341"). The text the
    detectors read is tokenised, and a conversation about the filter is full of tokens; a word
    built round one is our output, never a new identifier. Queued anyway, each was auto-mapped,
    rejected as unsafe, and cost a mapping rebuild - minutes of them for one request."""
    if mapper is None:
        return False
    pattern = _token_pattern(mapper)
    return bool(pattern and pattern.search(value))


# The shapes the placeholders above take (pending-1a2b3c, pending-1a2b3c.invalid, ...@pending.invalid,
# Pending 1A2B3C). Reserved: a placeholder whose review entry has gone - ignored, added, removed -
# still sits in the conversation history Claude Code resends (thinking is never rehydrated, D8), and
# the detectors queued it as a new identifier with a placeholder of its own (D180).
PLACEHOLDER_SHAPE = re.compile(
    r"(?i)^(?:pending-[0-9a-f]{6,16}(?:@pending\.invalid|\.onmicrosoft\.invalid|\.sharepoint\.invalid|\.invalid)?"
    r"|pending [0-9a-f]{6,16})$")


def is_placeholder(value: str) -> bool:
    """True for a string shaped like one of our own placeholders."""
    return bool(PLACEHOLDER_SHAPE.match(str(value or "").strip()))


def find_candidates(text: str, mapper: Mapper | None = None, resource_names: bool = False,
                    suppressed: list[tuple[str, str]] | None = None) -> Iterable[tuple[str, str, str, str]]:
    """Every candidate the detectors find, less the ones built round one of our own tokens, and our
    own placeholders (D180).

    `suppressed`, when given, collects (value, kind) for each one the PUBLIC LIST took out - the
    same shape `scan_candidates` uses for what Policy C held back. The list has three times nearly
    eaten real client data, and a suppressed value is otherwise invisible: not queued, not held
    back, not tokenised, not written anywhere a person looks (D13).
    """
    for found in _raw_candidates(text, mapper, resource_names, suppressed):
        if not carries_token(found[0], mapper) and not is_placeholder(found[0]):
            yield found


def _raw_candidates(text: str, mapper: Mapper | None = None, resource_names: bool = False,
                    suppressed: list[tuple[str, str]] | None = None) -> Iterable[tuple[str, str, str, str]]:
    """Yield (value, kind, confidence, client) for identifier-shaped strings in text.

    `client` is the suggested owner, set only when the value carries a mapped form of that client
    (kind "client-resource"); it is "" for everything else. With no mapper, the two resource kinds
    that need the mapping are skipped."""
    for m in RE_EMAIL.finditer(text):
        v = m.group(0)
        host = v.split("@", 1)[1]
        # The host has to end in a real top level. Without that, anything with an @ and a dot after
        # it reads as an address: a client mapping collected four OData annotations that way -
        # "authenticationStrength@odata.context" is a Graph response field, not a person.
        if not RE_DOMAIN.fullmatch(host):
            continue
        if not _domain_is_noise(host):
            yield v, "email", "high", ""
        elif suppressed is not None and is_public(host):
            suppressed.append((v, "email"))
    for m in RE_TENANT.finditer(text):
        yield m.group(0).lower(), "tenant", "high", ""
    for m in RE_SHAREPOINT.finditer(text):
        yield m.group(0).lower(), "sharepoint", "high", ""
    for m in RE_DOMAIN.finditer(text):
        v = m.group(0).lower()
        if v.endswith(".onmicrosoft.com") or v.endswith(".sharepoint.com") or _domain_is_noise(v):
            if suppressed is not None and is_public(v):
                suppressed.append((v, "domain"))
            continue
        if not _domain_has_context(text, m.start(), m.end(), v):
            continue
        yield v, "domain", "high", ""
    for m in RE_GUID_KEYED.finditer(text):
        v = m.group(1).lower()
        if not is_public(v):
            yield v, "guid", "high", ""
        elif suppressed is not None:
            suppressed.append((v, "guid"))
    for m in RE_IPV4.finditer(text):
        v = m.group(0)
        if v in NOISE_IPS or v.startswith(("198.18.", "198.19.", "0.", "127.", "224.", "239.")):
            continue  # noise, our own token range, multicast
        if not identifies_somebody(v):
            continue  # private, reserved, or a version number wearing the same shape
        if reads_as_version(text, m.start(), m.end()):
            continue
        yield v, "ip", "high", ""
    for m in RE_IPV6.finditer(text):
        v = m.group(0)
        if identifies_somebody(v):
            yield v.lower(), "ip", "high", ""
    for m in RE_DEVOPS.finditer(text):
        # A mapped organisation is its token here; a project under it is that client's (DECISIONS 46).
        org_owner = mapper.backward(m.group(1)).client if mapper is not None and _is_token(m.group(1), mapper) else ""
        if not org_owner:
            yield m.group(1), "devops-org", "high", ""
        if m.group(2):
            yield m.group(2), "devops-project", "high", org_owner
    # `name` only where a phrase carries one of our tokens (feature 63). Any capitalised phrase was
    # 40% of everything found and 1 in 140 was kept; a conversation about this tool filled Review
    # with "The Global". The cost, chosen: a client named in prose and not yet mapped at all is no
    # longer caught by this kind. domain, email and tenant are what catch a new client.
    seen_resources: set[str] = set()
    if mapper is not None:
        for phrase, owner in token_phrases(text, mapper):
            # A phrase whose other words are all ordinary - "Set <client>", "<client> Key Vault" -
            # adds nothing: the client's own word is already replaced (D149, the user 2026-10-08).
            if phrase_adds_nothing(phrase, mapper):
                continue
            yield phrase, "name", "low", owner
        # A mapped client name inside a longer word: whole-word matching leaves these in clear.
        for run, client, resource_shaped in contained_forms(text, mapper):
            if run.lower() in seen_resources:
                continue
            seen_resources.add(run.lower())
            yield run, "client-resource", "high" if resource_shaped else "low", client
    # A naming-shaped slug with no client in it: rg-aue-prd-management, vm-aue-prd-papercut-01.
    # Off unless asked for. Measured on a real client mapping these were a quarter of everything
    # collected and none of them named anybody - the shape is our own convention, repeated for
    # every engagement, and an application name in one (TRIM, Papercut) says nothing without the
    # client beside it. A name that DOES carry a client form is reported above, with its owner.
    if resource_names:
        for pattern in (RE_SEGMENTED, RE_RUN):
            for m in pattern.finditer(text):
                v = m.group(0)
                if v.lower() in seen_resources or not is_resource_name(v):
                    continue
                seen_resources.add(v.lower())
                yield v, "resource", "high", ""


# Endings that are real top-level domains but are also how code names things: a file extension
# (config.tf, main.py) or a property (member.id, group.app). These need more than the ending.
_AMBIGUOUS_TLDS = {"tf", "md", "py", "sh", "ps", "pl", "rs", "cc", "so", "zip", "mov", "ml",
                   "id", "app", "dev", "me", "it", "is", "as", "at", "in", "tv",
                   # Added for D57. Common method and attribute names that are also real
                   # gTLDs. The call rule in _domain_has_context covers these anyway; they
                   # are listed so a bare reference outside a call is not queued either.
                   "group", "name", "link", "page", "site", "email", "how", "new", "one",
                   # D124: an attribute read in code (an object, a dot, this word) was queued
                   # as a domain and written back to a file as its placeholder.
                   "host"}


def _domain_has_context(text: str, start: int, end: int, value: str) -> bool:
    """True when the match really is a host rather than a property path or a file name.

    Transcripts and Terraform are full of `$member.id`, `each.value.group.id` and `main.tf`, and
    `.id`, `.app` and `.tf` are all real top-level domains. A scheme, `www.` or an `@` settles it;
    otherwise the match must not sit inside a longer dotted expression, and its ending must be one
    that is not also an everyday word in code.
    """
    before = text[max(0, start - 8):start].lower()
    after = text[end:end + 2]
    if value.startswith("www.") or before.endswith(("http://", "https://", "//", "www.", "@")):
        return True
    # Reserved by RFC 2606 and the namespace our own placeholders and tokens live in, so it can
    # never be a client's real domain - and a substituted value re-entering the queue as a fresh
    # candidate is D1's feedback loop arriving by another route (D57).
    if value.endswith(".invalid"):
        return False
    if before[-1:] in (".", "$", "-", "_"):
        return False  # the tail of a longer dotted expression or part of an identifier
    if after[:1] == "." and after[1:2].isalnum():
        return False  # the head of one: something.app.displayName
    # A host is never called. `m.group(1)` is a two-label match ending in a real gTLD, and writing
    # it to a file used to rewrite the code (D57). The list below cannot be the answer on its own:
    # it is a guess at which words programmers use, and every new gTLD adds another. This is the
    # rule underneath, and it holds for endings nobody has thought of yet.
    if after[:1] == "(":
        return False
    return value.rsplit(".", 1)[-1] not in _AMBIGUOUS_TLDS


# Feature 31's last part, at DECISIONS 25's threshold: a kind is offered to be stopped once it has
# been ignored this many times and kept this often or less. Twenty, so a new install is not told
# after a morning's work; 1 in 10, where the detector is costing more than it finds.
QUIET_MIN_IGNORES = 20
QUIET_KEEP_RATE = 0.1
# "Not now" on a kind offer: it comes back after this many more ignores (DECISIONS 42).
SNOOZE_IGNORES = 50


class CandidateStore:
    """Thread-safe queue of candidates persisted as JSONL next to the mapping."""

    def __init__(self, path: Path):
        self.path = path
        self.ignore_path = path.parent / "ignore-list.json"
        # How often each kind was ignored and how often kept (mapped), kept beside the queue it
        # counts. What feature 31's kind offer reads; a dismiss is neither.
        self.tally_path = path.with_name(path.stem + "-kinds.json")
        self._tally: dict[str, dict[str, int]] = {}
        # Kinds never proposed, set from detect.quiet_kinds by whoever owns this store. Never
        # proposed means never held back either: a placeholder is given to a pending candidate.
        self.quiet_kinds: frozenset[str] = frozenset()
        # Kinds set to Ignore (DECISIONS 46): never proposed, never held back, sent as written.
        self.ignore_kinds: frozenset[str] = frozenset()
        self._lock = threading.Lock()
        self._items: dict[str, Candidate] = {}
        # Values this store has deliberately removed. `_save` merges with what is on disk so a
        # second process's finds are not overwritten, and a deletion has to survive that.
        self._gone: set[str] = set()
        # What the public list has taken out, and how often. The list has nearly eaten real client
        # data three times - tailwind, wingtip, and a helpdesk mailbox at a Salesforce case host -
        # and each time nothing on any page would have shown it: a suppressed value simply never
        # appears. This is the counterweight (D13). In memory only, because the question it
        # answers is "what is being suppressed now", and bounded because it is fed by traffic.
        self._suppressed: dict[str, dict] = {}
        self._ignore: set[str] = set()
        self._ignore_seen: tuple[int, int] | None = None   # the ignore file as last read (D128)
        # "Carries a client's name" for the paths that have no mapper to hand (add_candidate,
        # offer); the proxy points it at the current mapper (feature 76). None: nothing protected.
        self.protected: Callable[[str], bool] | None = None
        self.version = 0  # bumps on every change; notifier compares against it
        self.placeholder_version = 0  # bumps when the set of placeholders changes (proxy rebuilds its mapper)
        self._load()

    # ---- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    d = json.loads(line)
                    known = {f for f in Candidate.__dataclass_fields__}
                    self._items[d["value"]] = Candidate(**{k: v for k, v in d.items() if k in known})
        self._ignore_seen = None
        self._read_ignore()
        try:
            self._tally = {str(k): {"ignored": int(v.get("ignored", 0)), "kept": int(v.get("kept", 0)),
                                    **({"snoozed_at": int(v["snoozed_at"])} if "snoozed_at" in v else {})}
                           for k, v in json.loads(self.tally_path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            pass    # not there yet, or unreadable: counting starts again, which only delays an offer

    # Enough to notice a pattern, few enough that traffic cannot grow it without bound.
    MAX_SUPPRESSED = 500

    def _note_suppressed(self, value: str, kind: str, client: str) -> None:
        """Remember that the public list took this one out, so over-suppression is visible."""
        row = self._suppressed.get(value)
        if row is None:
            if len(self._suppressed) >= self.MAX_SUPPRESSED:
                return
            row = {"value": value, "kind": kind, "count": 0, "clients": []}
            self._suppressed[value] = row
        row["count"] += 1
        if client and client not in row["clients"] and len(row["clients"]) < 5:
            row["clients"].append(client)

    def suppressed(self) -> list[dict]:
        """What the public list has taken out this run, most-seen first."""
        with self._lock:
            return sorted(self._suppressed.values(), key=lambda r: (-r["count"], r["value"]))

    def reload(self) -> None:
        """Read the file again, picking up what another process has queued.

        A scan started from a page runs as a separate process and queues through its own store
        (D14), so after one finishes this store is a scan behind. Anything held here and not yet
        on disk survives, for the same reason `_save` merges: what we hold is the newer copy.
        """
        with self._lock:
            mine = dict(self._items)
            gone = set(self._gone)
            self._items = {}
            self._load()
            for key in gone:
                self._items.pop(key, None)
            self._items.update(mine)

    def _save(self) -> None:
        """Write our items, keeping any the file has gained that we do not know about.

        This used to write the whole file from `self._items` alone, which is safe only while one
        process has it. A scan started from a page now runs as a separate process (D14) and
        queues its finds through a store of its own: it loads the file at the start and saves at
        the end, so everything the proxy queued from live traffic in between was overwritten.

        So the write merges. A value only on disk is kept as it is; a value we hold is written as
        we hold it, because ours is the copy that has just been updated. It does not make two
        writers safe in general - nothing here locks - but it turns "the last writer wins the
        whole file" into "the last writer wins the rows it touched", which is the difference
        between losing a scan's work and losing nothing.
        """
        merged: dict[str, Candidate] = {}
        known = {f.name for f in fields(Candidate)}
        on_disk = ""
        try:
            on_disk = self.path.read_text(encoding="utf-8")
            for line in on_disk.splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                merged[d["value"]] = Candidate(**{k: v for k, v in d.items() if k in known})
        except (OSError, ValueError, KeyError, TypeError):
            merged = {}          # unreadable or not there yet: ours is all there is
        # Without this the merge cannot tell "I never knew about this row" from "I deleted it",
        # and reads a resolved candidate straight back off disk - so resolving one never stuck.
        for key in self._gone:
            merged.pop(key, None)
        merged.update(self._items)
        text = "".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in merged.values())
        if text == on_disk:
            return   # nothing changed: a busy session rewrote it every few seconds regardless (H13)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        fileio.replace(tmp, self.path)

    # Global's ignore list has one source of truth, the file (D128). The mapping store writes it too -
    # moving an ignore between scopes, undoing a move - so a copy read once at start went stale, and
    # writing that whole copy back undid those changes. Read again whenever the file has changed;
    # write only this store's own change, applied to the file as it is now.
    def _read_ignore(self) -> set[str]:
        """The ignore file as it is now; the copy already read when the file has not changed."""
        try:
            stat = self.ignore_path.stat()
        except OSError:
            self._ignore, self._ignore_seen = set(), None
            return self._ignore
        seen = (stat.st_mtime_ns, stat.st_size)
        if seen != self._ignore_seen:
            try:
                self._ignore = set(json.loads(self.ignore_path.read_text(encoding="utf-8")).get("ignore", []))
                self._ignore_seen = seen
            except (OSError, ValueError):
                pass    # caught mid-write by the other store: what was read last stands, once
        return self._ignore

    def ignore_set(self) -> set[str]:
        """Global's ignore list as it is now (D129) - what the proxy's owner checks consult."""
        with self._lock:
            return set(self._read_ignore())

    def _change_ignore(self, add: Iterable[str] = (), remove: Iterable[str] = ()) -> None:
        """Apply one change to the file as it is now, keeping whatever else is in it."""
        current = (set(self._read_ignore()) | set(add)) - set(remove)
        tmp = self.ignore_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ignore": sorted(current)}, indent=2), encoding="utf-8")
        fileio.replace(tmp, self.ignore_path)
        self._ignore_seen = None
        self._read_ignore()

    def _count(self, kind: str, outcome: str, delta: int = 1) -> None:
        """Add to a kind's ignored or kept count (under the lock), never below nought."""
        row = self._tally.setdefault(kind, {"ignored": 0, "kept": 0})
        row[outcome] = max(0, row[outcome] + delta)
        self._save_tally()

    def _save_tally(self) -> None:
        tmp = self.tally_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tally, indent=2, sort_keys=True), encoding="utf-8")
        fileio.replace(tmp, self.tally_path)

    def kind_tally(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: dict(v) for k, v in self._tally.items()}

    def kind_offers(self) -> list[dict[str, Any]]:
        """Kinds ignored far more often than kept, not already quiet: feature 31's last offer."""
        with self._lock:
            offers = [{"kind": kind, **row} for kind, row in self._tally.items()
                      if kind not in self.quiet_kinds and kind not in self.ignore_kinds and self._offerable(row)
                      and row["ignored"] >= row.get("snoozed_at", -SNOOZE_IGNORES) + SNOOZE_IGNORES]
        return sorted(offers, key=lambda o: -o["ignored"])

    @staticmethod
    def _offerable(row: dict[str, int]) -> bool:
        return (row["ignored"] >= QUIET_MIN_IGNORES
                and row["kept"] <= QUIET_KEEP_RATE * (row["ignored"] + row["kept"]))

    def snooze_kind(self, kind: str, undo: bool = False) -> int | bool:
        """"Not now" on a kind offer (DECISIONS 42): hidden until SNOOZE_IGNORES more ignores.

        Returns the ignore count at which it comes back, True for an undo, or False when the kind
        is not being offered (nothing to snooze) or was not snoozed (nothing to undo).
        """
        with self._lock:
            row = self._tally.get(kind)
            if row is None:
                return False
            if undo:
                if row.pop("snoozed_at", None) is None:
                    return False
                self._save_tally()
                return True
            if kind in self.quiet_kinds or kind in self.ignore_kinds or not self._offerable(row):
                return False
            row["snoozed_at"] = row["ignored"]
            self._save_tally()
            return row["ignored"] + SNOOZE_IGNORES

    # ---- recording --------------------------------------------------------------

    def add_candidate(self, value: str, kind: str, confidence: str, client: str, source: str,
                      placeholder_kinds: Iterable[str] = (), context: SeenContext | None = None,
                      near: str = "") -> bool:
        """Queue one value the detectors did not produce (a near miss of a client name), so the
        placeholder pass holds it back in this same request. True when it is new."""
        key = value.lower() if kind != "name" else value
        if is_common_word(value) or not worth_queueing(value) or kind in self.quiet_kinds or kind in self.ignore_kinds:
            return False
        with self._lock:
            if ignored_by(value, self._read_ignore(), self.protected) or key in self._items:
                return False
            self._items[key] = Candidate(value=value, kind=kind, confidence=confidence,
                                         first_seen=_now(), last_seen=_now(), count=1,
                                         sources=[source], client=client, near=near)
            if context is not None:
                context.note(self._items[key])
            if self._assign_placeholders(set(placeholder_kinds), mapper=None):
                self.placeholder_version += 1
            self._save()
            self.version += 1
            return True

    def record(self, text: str, mapper: Mapper, source: str, placeholder_kinds: Iterable[str] = (),
               context: SeenContext | None = None,
               candidates: list[tuple[str, str, str, str]] | None = None) -> int:
        """Detect in text; returns the number of NEW candidates added. Candidates of the
        placeholder kinds get a placeholder, so the proxy can hold them back."""
        kinds = set(placeholder_kinds)
        known = {f.real.lower() for f in mapper.forms} | {f.token.lower() for f in mapper.forms}
        added = 0
        with self._lock:
            # Our own output: tokens and placeholders. The text read here has been tokenised, so a
            # piece of one (the user00b798 of user00b798@org00.invalid) is not a new identifier.
            tokens = "\n".join(sorted({f.token.lower() for f in mapper.forms}))
            placeholders = {c.placeholder.lower() for c in self._items.values() if c.placeholder}
            ours = tokens + "\n" + "\n".join(sorted(placeholders))
            # An address whose domain is one of our placeholders is a real mailbox at a domain still
            # waiting in Review (D122): queued as it is, it joined a real name to our stand-in. It
            # is found again, whole, once that domain is decided.
            at_pending = lambda v, k: k == "email" and v.rsplit("@", 1)[-1].lower() in placeholders  # noqa: E731
            # and clear the ones queued before this check existed
            shared_ignore = getattr(mapper, "ignore", None) or set()
            protect = lambda v: carries_client_real(v, mapper)   # noqa: E731 - feature 76
            stale = [k for k, c in self._items.items() if c.value.lower() in tokens or c.value.lower() in known
                     or is_common_word(c.value) or not worth_queueing(c.value)
                     or ignored_by(c.value, shared_ignore, protect) or c.kind in self.quiet_kinds or c.kind in self.ignore_kinds
                     or c.kind in STATELESS_KINDS or not _still_a_client_resource(c, mapper)
                     or at_pending(c.value, c.kind)]
            for k in stale:
                self._gone.add(k)
                del self._items[k]
            noted: set[str] = set()
            # What the public list takes out, so over-suppression is visible rather than silent.
            taken: list[tuple[str, str]] = []
            found = candidates if candidates is not None else find_candidates(text, mapper, suppressed=taken)
            for value, kind, confidence, client in found:
                key = value.lower() if kind != "name" else value
                if kind in self.quiet_kinds or kind in self.ignore_kinds or kind in STATELESS_KINDS:
                    continue
                if (ignored_by(value, self._read_ignore(), protect) or value.lower() in known
                        or ignored_by(value, shared_ignore, protect)):
                    continue
                if value.lower() in ours or is_common_word(value) or not worth_queueing(value):
                    continue
                if at_pending(value, kind):
                    continue
                if is_public(value):
                    continue  # the same in every tenant: not this client's identity
                c = self._items.get(key)
                if c is None:
                    c = Candidate(value=value, kind=kind, confidence=confidence, first_seen=_now(), client=client)
                    self._items[key] = c
                    added += 1
                c.count += 1
                c.last_seen = _now()
                if source not in c.sources and len(c.sources) < 5:
                    c.sources.append(source)
                if context is not None and key not in noted:
                    noted.add(key)   # once per request, however often it appears in it
                    context.note(c)
            for value, kind in taken:
                self._note_suppressed(value, kind, "")
            assigned = self._assign_placeholders(kinds, mapper)
            if assigned or stale:
                self.placeholder_version += 1
            if added or self._items or stale:
                self._save()
            if added or stale:
                self.version += 1
        return added

    def offer(self, value: str, kind: str, confidence: str = "high", client: str = "",
              count: int = 1, source: str = "") -> bool:
        """Queue one find that is already known, rather than detecting it in text.

        A scan has decided what it found; it does not need the detector to find it again. Used
        where a scan routes a kind to Review instead of into the mapping (defect D3), so the
        judgement about which kind to route lives in the scan and the queueing lives here.

        The same filters as `record` still apply - an ignored value, one already in the mapping,
        or one the public list covers is not queued - because the reason each of those is not a
        candidate has nothing to do with how it arrived.
        """
        key = value.lower() if kind != "name" else value
        with self._lock:
            if (ignored_by(value, self._read_ignore(), self.protected) or is_public(value) or is_common_word(value)
                    or not worth_queueing(value) or kind in self.quiet_kinds or kind in self.ignore_kinds
                    or kind in STATELESS_KINDS):
                return False
            existing = self._items.get(key)
            if existing is None:
                existing = Candidate(value=value, kind=kind, confidence=confidence,
                                     first_seen=_now(), client=client)
                self._items[key] = existing
                new = True
            else:
                new = False
            existing.count += max(1, count)
            existing.last_seen = _now()
            if source and source not in existing.sources and len(existing.sources) < 5:
                existing.sources.append(source)
            self._save()
            self.version += 1
            return new

    def _assign_placeholders(self, kinds: set[str], mapper: Mapper | None) -> int:
        """Give every pending HIGH-confidence candidate of the chosen kinds a placeholder.

        Low confidence is queued and sent, never held back - which is already the stated rule for
        "name" (a capitalised phrase), and was not being applied to the one other kind that can be
        low. A client form inside a longer word is graded high only when the word around it is
        resource-shaped, because otherwise the containment is a coincidence: with a short client name
        declared, an ordinary longer word containing it matched, was given a placeholder, and the model
        was handed
        "the pending-fa670d were fine". Holding a word back costs the answer and protects nobody.
        """
        if not kinds:
            return 0
        taken = {c.placeholder.lower() for c in self._items.values() if c.placeholder}
        taken |= {f.token.lower() for f in (mapper.forms if mapper is not None else ())}
        assigned = 0
        for c in self._items.values():
            if c.placeholder or c.kind not in kinds or c.confidence != "high":
                continue
            ph = make_placeholder(c.value, c.kind, taken)
            if ph:
                c.placeholder = ph
                c.unprotected = False
                taken.add(ph.lower())
                assigned += 1
            else:
                # Nothing left to mint. The value goes out as it is, which is the one case here
                # that is a failure rather than a decision - so it is recorded on the candidate
                # and counted on the status, instead of only in a log nobody reads.
                c.unprotected = True
                log.warning("no placeholder left for %r (%s): it will be sent as it is", c.value, c.kind)
        return assigned

    def placeholder_forms(self) -> list[tuple[str, str, str]]:
        """(value, placeholder, kind) for every pending item that is being held back."""
        with self._lock:
            return [(c.value, c.placeholder, c.kind) for c in self._items.values() if c.placeholder]

    # ---- review ------------------------------------------------------------------

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._items.values(), key=lambda c: (c.confidence != "high", -c.count))
            return [asdict(c) for c in items]

    def find(self, value: str) -> "Candidate | None":
        """The queued candidate for a value, without removing it.

        Accepting a candidate is the moment its evidence is worth keeping - where it was seen,
        how often, in which projects - and `resolve` discards all of it. This is the look before
        that (feature 30).
        """
        with self._lock:
            return self._items.get(value) or self._items.get(value.lower())

    def resolve(self, value: str, action: str) -> bool:
        """action: 'ignore' adds to the allowlist; 'mapped' and 'dismiss' remove it from the queue
        (a dismissed value comes back if it is seen again)."""
        key = value.lower() if value not in self._items else value
        with self._lock:
            c = self._items.pop(key, None) or self._items.pop(value, None)
            self._gone.update((key, value))
            if c is not None and c.placeholder:
                self.placeholder_version += 1
            if action == "ignore":
                self._change_ignore(add={key})
            # A dismiss is "not now", neither a keep nor an ignore, so it is not counted.
            if c is not None and action in ("ignore", "mapped"):
                self._count(c.kind, "ignored" if action == "ignore" else "kept")
            self._save()
            self.version += 1
            return c is not None

    def restore(self, record: dict[str, Any], undoing: str = "") -> None:
        """Put a candidate back as it was before Review acted on it - the undo (D45).

        The whole record comes back, placeholder included: a conversation already under way was
        sent that placeholder, and minting a new one would leave the model naming something that
        no longer restores. If it has been seen again since, the sightings are added together and
        the newer copy's evidence is kept alongside, because undo takes back an action, not time.

        `undoing` names what is being taken back - "ignored" or "kept" - so the kind's count goes
        back with it; otherwise an undone mis-click would still push a kind towards being quieted.
        """
        known = {f.name for f in fields(Candidate)}
        c = Candidate(**{k: v for k, v in record.items() if k in known})
        key = c.value.lower() if c.kind != "name" else c.value
        with self._lock:
            self._gone.discard(key)
            self._gone.discard(c.value)
            if c.value.lower() in self._read_ignore():
                self._change_ignore(remove={c.value.lower()})
            now = self._items.get(key)
            if now is not None:
                c.count += now.count
                c.last_seen = max(c.last_seen, now.last_seen)
                for cid, n in now.seen_with.items():
                    c.seen_with[cid] = c.seen_with.get(cid, 0) + n
                c.projects += [p for p in now.projects if p not in c.projects]
                c.sessions += [s for s in now.sessions if s not in c.sessions]
                c.sources += [s for s in now.sources if s not in c.sources][:max(0, 5 - len(c.sources))]
                c.placeholder = c.placeholder or now.placeholder
            self._items[key] = c
            if c.placeholder:
                self.placeholder_version += 1
            if undoing in ("ignored", "kept"):
                self._count(c.kind, undoing, -1)
            self._save()
            self.version += 1

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    # ---- ignore list ------------------------------------------------------------

    def ignore_entries(self) -> list[str]:
        with self._lock:
            return sorted(self._read_ignore())

    def add_ignore(self, value: str) -> None:
        """Pre-emptively allowlist a string; also drops it from the queue if present."""
        key = value.strip().lower()
        if not key:
            raise ValueError("empty value")
        with self._lock:
            self._change_ignore(add={key})
            self._gone.update((key, value.strip()))
            # What it covers leaves the queue with it: a domain its hosts, a pattern its matches (D67).
            covered = [k for k, c in self._items.items() if ignored_by(c.value, {key})]
            self._gone.update(covered)
            for gone in [self._items.pop(k, None) for k in {key, value.strip(), *covered}]:
                if gone is not None and gone.placeholder:
                    self.placeholder_version += 1
                if gone is not None:
                    self._count(gone.kind, "ignored")   # Review's Ignore arrives this way, not via resolve
            self._save()
            self.version += 1

    def remove_ignore(self, value: str) -> bool:
        with self._lock:
            key = value.strip().lower()
            existed = key in self._read_ignore()
            if existed:
                self._change_ignore(remove={key})
            return existed


ALL_KINDS = ["email", "tenant", "sharepoint", "domain", "guid", "ip", "devops-org", "devops-project",
             "client-resource", "resource", "name"]

# The kinds a person can tick on the Settings page. "resource" - a bare resource name with no
# client name in it - is not one of them: it is only ever produced by `aicleaner scan --resources`
# from a terminal, and is reported by nothing the proxy or the UI does. Ticking it changed nothing,
# which is worse than having no tick at all, because a control that does nothing reads as
# protection you have. `client-resource`, the one that carries a client's name, is unaffected.
#
# Nor is "name" (feature 63): accepted about 1 time in 140, holding it back would put rubbish into
# every request, and auto-adding it would put rubbish into client files - where, by D1, it becomes
# the words the scanner watches for. The mirror of the "resource" case: that control did nothing,
# this one would do harm. NEVER_HELD_BACK in config.py drops it from a saved setting too.
SELECTABLE_KINDS = [k for k in ALL_KINDS if k not in ("resource", "name", "guid")]

# Kinds replaced by a stateless stand-in on every request and never collected (DECISIONS 38): never
# queued, never auto-added, not scanned, not seeded. A GUID is protected because a tenant or
# subscription id does lead back to a client, but nobody needs to decide about one - and filing it
# under a client meant guessing which. The stand-in is a hash, so it is the same on every request
# and every machine without being stored. Not a placeholder setting: it is always on.
STATELESS_KINDS = frozenset({"guid"})


def _still_a_client_resource(c: Candidate, mapper: Mapper) -> bool:
    """False for a queued "carries a client's name" item that no longer does: it matched a word
    the client's names are no longer watched for (the words of an email's mailbox)."""
    if c.kind != "client-resource" or not c.client:
        return True
    pattern, owners = _needles(mapper)
    if pattern.search(c.value):
        return True
    return bool(near_miss_words(c.value, mapper))


def near_miss_words(text: str, mapper: Mapper) -> list[tuple[str, str, str]]:
    """(word, client, near) for words one letter away from a word `near` inside a mapped form.

    `Mapper.fix_near_misses` handles forms that are one word ("kvaueprdmgmt"): it swaps in the
    token. A client mapped as "Fabrikam Inc" has no single-word form, so a misread "Fabnkam" is
    caught here instead and held back as a placeholder, which leaks nothing and keeps the reply
    readable.
    """
    from .mapping import near_miss_match, ocr_shape

    _pattern, owners = _needles(mapper)
    if not owners:
        return []
    by_length: dict[int, list[str]] = {}
    for needle in owners:
        by_length.setdefault(len(ocr_shape(needle)), []).append(needle)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in RE_ALNUM_RUN.finditer(text):
        word = match.group(0)
        low = word.lower()
        if len(word) < 8 or low in owners or low in seen or carries_token(word, mapper):
            continue
        shaped = len(ocr_shape(low))
        for length in (shaped - 1, shaped, shaped + 1):
            hit = next((n for n in by_length.get(length, []) if near_miss_match(low, n)), None)
            if hit:
                seen.add(low)
                out.append((word, owners[hit], hit))
                break
    return out


def _is_token(value: str, mapper: Mapper) -> bool:
    """True when the whole value is one of the mapper's tokens."""
    return value.lower() in {f.token.lower() for f in mapper.forms}


_M365_SUFFIXES = ("-my", "-admin")


def _m365_name(value: str) -> str:
    """The tenant name in a tenant or SharePoint host: fabrikam365 for fabrikam365.onmicrosoft.com,
    fabrikam365.sharepoint.com, fabrikam365-my.sharepoint.com and fabrikam365-admin.sharepoint.com."""
    name = value.lower().split(".", 1)[0]
    for suffix in _M365_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def owned_values(text: str, mapper: Mapper, candidates: list[tuple[str, str, str, str]],
                 kinds: set[str], ignore: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str, str]]:
    """(value, kind, client) for finds whose owner the value itself proves, for auto-adding
    (DECISIONS 46; the same line DECISIONS 38 drew - proof from the value, never the conversation).

    - tenant / SharePoint: a mapped tenant or SharePoint host with the same tenant name (Microsoft
      365 gives every tenant <name>.sharepoint.com, -my and -admin);
    - DevOps project: its organisation is mapped (find_candidates carries the owner);
    - tenant, SharePoint, domain, DevOps organisation: it contains one client's form, and only one.
    Email has its own rule (owned_emails); a resource name, client_resources.
    """
    siblings: dict[str, str] = {}
    for f in mapper.forms:
        if classify(f.real) in ("tenant", "sharepoint"):
            siblings.setdefault(_m365_name(f.real), f.client)
    known = {f.real.lower() for f in mapper.forms} | {f.token.lower() for f in mapper.forms}
    skip = set(ignore) | set(getattr(mapper, "ignore", ()) or ())
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for value, kind, confidence, client in candidates:
        key = value.lower()
        if kind not in kinds or confidence != "high" or key in seen or key in known:
            continue
        if ignored_by(value, skip, lambda v: carries_client_real(v, mapper)) or is_public(value):
            continue
        owner = client if kind == "devops-project" else ""
        if not owner and kind in ("tenant", "sharepoint"):
            owner = siblings.get(_m365_name(value), "")
        if not owner and kind in ("tenant", "sharepoint", "domain", "devops-org"):
            # The first label alone: contained_forms leaves labels inside a host to the host
            # detectors, which is this. `fabrikamgroup` of fabrikamgroup.onmicrosoft.com.
            label = _m365_name(value) if kind in ("tenant", "sharepoint") else value.split(".", 1)[0]
            owners = {c for _run, c, _shaped in contained_forms(label, mapper) if c}
            owner = owners.pop() if len(owners) == 1 else ""
        if owner:
            seen.add(key)
            out.append((value, kind, owner))
    return out


def owned_emails(text: str, mapper: Mapper,
                 ignore: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """(address, client) for emails at a client's mapped domain, for auto-adding (DECISIONS 45).

    Reads the tokenised text, where such an address has become `jane.doe@client05.invalid`: the
    domain replaced, the mailbox name in clear. The domain proves the owner, so the whole address is
    returned with the domain put back, to be added to that client. A subdomain counts
    (`ops@mail.client05.invalid`). An address already mapped is its own token here and is skipped.
    """
    domains = {f.token.lower(): f for f in mapper.forms if classify(f.real) == "domain"}
    if not domains:
        return []
    known = {f.real.lower() for f in mapper.forms} | {f.token.lower() for f in mapper.forms}
    skip = {v.lower() for v in ignore} | set(getattr(mapper, "ignore", ()) or ())
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in RE_EMAIL.finditer(text):
        address = m.group(0)
        if address.lower() in known:
            continue
        host = address.rsplit("@", 1)[1].lower()
        labels = host.split(".")
        owner = next((domains[".".join(labels[i:])] for i in range(len(labels) - 1)
                      if ".".join(labels[i:]) in domains), None)
        if owner is None:
            continue
        real = mapper.rehydrate(address)
        key = real.lower()
        if (key in seen or key in known or ignored_by(real, skip, lambda v: carries_client_real(v, mapper))
                or carries_token(real, mapper)):
            continue
        seen.add(key)
        out.append((real, owner.client))
    return out


def token_names(text: str, mapper: Mapper, candidates: list[tuple[str, str, str, str]] | None = None,
                ignore: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, str]]:
    """(value, client) for names carrying one client's token, for auto-adding (DECISIONS 37).

    The owner is proven by the token, so these are added rather than left in Review with their
    unmapped words travelling in clear. A phrase joining two clients' tokens has no owner: skipped.
    """
    known = {f.real.lower() for f in mapper.forms}
    skip = set(ignore) | set(getattr(mapper, "ignore", ()))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, kind, _confidence, client in (find_candidates(text, mapper) if candidates is None else candidates):
        key = value.lower()
        if kind != "name" or not client or key in seen or key in known or key in skip:
            continue
        # A phrase is never auto-added (D149): the client's own word in it is replaced already, and an
        # unusual phrase is for Review to decide. A joined identifier (ClientApprovedSoftware) still is.
        if any(ch.isspace() for ch in value):
            continue
        seen.add(key)
        out.append((value, client))
    return out


def client_resources(text: str, mapper: Mapper,
                     candidates: list[tuple[str, str, str, str]] | None = None) -> list[tuple[str, str]]:
    """(value, client) for resource names that carry a mapped client form, for auto-mapping."""
    known = {f.real.lower() for f in mapper.forms} | {f.token.lower() for f in mapper.forms}
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, kind, confidence, client in (find_candidates(text, mapper) if candidates is None else candidates):
        key = value.lower()
        if kind != "client-resource" or confidence != "high" or not client or key in seen or key in known:
            continue
        if key in getattr(mapper, "ignore", ()) or is_public(value):
            continue
        seen.add(key)
        out.append((value, client))
    return out
