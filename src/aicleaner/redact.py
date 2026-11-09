"""Credential redaction on the outbound path.

Unlike identifiers, secrets are never "learned": one leak is a compromise. Every
outbound body is scanned after tokenisation and each match is replaced with
[REDACTED-<family>] (mode "redact", default) or the request is refused (mode
"block"). Only family counts reach the evidence log; the values never do.

Families are deliberately broad: a false positive costs a placeholder in a value
the model did not need verbatim, a false negative costs the environment.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

# The name half of an env-secret: `export ` optional, then an upper-case name holding one of the secret words, then =.
# No length caps (second review of 1.0.15: a cap is a way past the check); the name is matched possessively and only
# starts after a separator, so a long run of capitals is read once, not again from every position.
_ENV_NAME = (r"(?m)(?:^|(?<=[\s\"'\[{,;]))(?:export[ \t]+)?"
             r"(?=[A-Z0-9_]*?(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|API_?KEY|APIKEY|PRIVATE_?KEY|PSK|PRESHARED))"
             r"[A-Z0-9_]++[ \t]*=[ \t]*")

# (family, pattern). Order matters where patterns overlap: specific first.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # Azure storage account keys and Service Bus / Event Hub keys: base64, 86 chars + "=="
    ("azure-account-key", re.compile(r"(?i)(?:AccountKey|SharedAccessKey|PrimaryKey|SecondaryKey|key1|key2|value)\\?\"?\s*[=:]\s*\\?\"?([A-Za-z0-9+/]{86}==)")),
    ("azure-account-key", re.compile(r"\b[A-Za-z0-9+/]{86}==")),
    # SAS tokens: sig= followed by URL-encoded base64
    ("azure-sas", re.compile(r"(?i)(?:[?&]|\b)sig=[A-Za-z0-9%+/=_-]{20,}")),
    # Entra app client secrets (~40 chars, mixed with ~ . _ -), when labelled
    ("entra-client-secret", re.compile(r"(?i)(?:client[_-]?secret|clientSecret|secretText|password)\\?\"?\s*[=:]\s*\\?\"?([A-Za-z0-9~._-]{34,44})(?=[\"'\s,;&}\\]|$)")),
    # Azure DevOps PATs: legacy 52-char base32-ish, new format prefixed
    ("azdo-pat", re.compile(r"\b[a-z0-9]{52}\b|\b(?:azdo|ado)[A-Za-z0-9]{40,}\b")),
    # Secrets in .env lines and shell exports (feature 165, home-network, 2026-11-05): a NAME holding PASSWORD, SECRET,
    # TOKEN, API_KEY, PRIVATE_KEY, PSK ... then = and the value, quoted or not. Upper-case names only - the .env
    # convention - so code such as `max_tokens = 100` is left alone; a value of digits alone is left alone too.
    # The name starts after a space, a quote, a bracket or a separator rather than only at a line's start: block mode
    # reads the body as one JSON line, its line breaks turned into spaces and its quotes escaped (\"), and has to see
    # the same secrets. Values have no length cap and are matched possessively; a quoted value runs to its closing
    # quote, escaped (\") or not, and may hold backslashes; an unquoted one runs to whitespace, a quote or a #.
    ("env-secret", re.compile(_ENV_NAME + r"\\?\"(?![0-9]+\\?\")(?=(?:[^\"\r\n\\]|\\(?!\")){6})((?:[^\"\r\n\\]|\\(?!\"))++)\\?\"")),
    ("env-secret", re.compile(_ENV_NAME + r"\\?'(?![0-9]+\\?')(?=(?:[^'\r\n\\]|\\(?!')){6})((?:[^'\r\n\\]|\\(?!'))++)\\?'")),
    ("env-secret", re.compile(_ENV_NAME + r"(?![0-9]+(?:\s|$))(?=[^\s\"'#]{6})([^\s\"'#]++)")),
    # API keys in request headers (UniFi's X-API-KEY and the like): the header's value.
    ("api-key-header", re.compile(r"(?i)\b(?:x-api-key|api-key|apikey)[\"']?[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_-]{24,64})")),
    # Synology DSM session ids and tokens: sid=..., "sid":"...", _sid=..., SynoToken=... (a Windows SID, S-1-..., is
    # not a secret and is left alone).
    ("synology-sid", re.compile(r"(?i)(?<![A-Za-z0-9])(?:_?sid|SynoToken)[\"']?[ \t]*[=:][ \t]*[\"']?(?!S-1-)([A-Za-z0-9._~-]{16,})")),
    # WireGuard private, public and preshared keys: 32 bytes as base64, 44 characters ending in = and standing alone
    # (not part of a longer base64 run). Broad by design - a 32-byte hash in base64 is masked too.
    ("wireguard-key", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=(?![A-Za-z0-9+/=])")),
    # Connection strings with an inline password
    ("connection-string-password", re.compile(r"(?i)(?:Password|Pwd)=([^;\"'\s]{6,})")),
    # Generic bearer tokens in text (not headers)
    ("bearer-token", re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._~+/-]{30,}=*)")),
]


def redact(text: str, counter: Counter[str] | None = None) -> str:
    """Replace every credential match with a family placeholder."""
    for family, pat in PATTERNS:
        def repl(m: re.Match[str], family: str = family) -> str:
            if counter is not None:
                counter[family] += 1
            if m.lastindex:  # keep the label, replace only the secret group
                start, end = m.span(1)
                return m.group(0)[: start - m.start()] + f"[REDACTED-{family}]" + m.group(0)[end - m.start():]
            return f"[REDACTED-{family}]"
        text = pat.sub(repl, text)
    return text


def find_families(text: str) -> list[str]:
    """Families present in text (for block mode); no values returned."""
    return sorted({family for family, pat in PATTERNS if pat.search(text)})


def families() -> Iterable[str]:
    return sorted({f for f, _ in PATTERNS})
