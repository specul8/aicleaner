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
