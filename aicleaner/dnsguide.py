"""What a domain's DNS looks like today, for the setup wizard's remote-access step (feature 106; spec
docs/superpowers/specs/2026-10-09-onboarding-and-public-release-design.md, section 6).

Cloudflare's tunnel needs the domain's DNS on Cloudflare (on the Free and Pro plans the nameservers
move there). Before anyone moves anything, this reads what exists - nameservers, mail (MX, SPF,
DMARC), the apex and www, autodiscover - and says who hosts the DNS now, so the page can say what
moving would involve and what must not be lost. Read-only, standard library only: it runs Windows'
own nslookup and reads its answer; no record is changed anywhere.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Nameserver suffixes and who they belong to. cPanel hosts first: the commonest starting point for
# the people this is for. Anything else is reported by its nameserver names.
HOSTS = [
    ("cloudflare.com", "Cloudflare", "cloudflare"),
    ("domaincontrol.com", "GoDaddy", "registrar"),
    ("crazydomains.com", "Crazy Domains", "registrar"),
    ("ventraip.net.au", "VentraIP", "cpanel"),
    ("syd.secureserver.net", "GoDaddy", "registrar"),
    ("registrar-servers.com", "Namecheap", "registrar"),
    ("squarespacedns.com", "Squarespace", "registrar"),
    ("googledomains.com", "Google Domains (now Squarespace)", "registrar"),
    ("azure-dns.com", "Azure DNS", "cloud"), ("azure-dns.net", "Azure DNS", "cloud"),
    ("azure-dns.org", "Azure DNS", "cloud"), ("azure-dns.info", "Azure DNS", "cloud"),
    ("awsdns", "Amazon Route 53", "cloud"),
    ("hostgator.com", "HostGator", "cpanel"), ("bluehost.com", "Bluehost", "cpanel"),
    ("siteground.net", "SiteGround", "cpanel"), ("netregistry.net", "Netregistry", "cpanel"),
    ("melbourneit.net", "Melbourne IT", "registrar"), ("digitalocean.com", "DigitalOcean", "cloud"),
]


@dataclass
class DomainReport:
    domain: str
    nameservers: list[str] = field(default_factory=list)
    host: str = ""                 # who hosts the DNS now, when recognised
    kind: str = ""                 # cloudflare | cpanel | registrar | cloud | ""
    mail: list[str] = field(default_factory=list)          # MX
    spf: str = ""
    dmarc: str = ""
    apex: list[str] = field(default_factory=list)          # A records of the domain itself
    www: list[str] = field(default_factory=list)
    autodiscover: str = ""
    error: str = ""

    @property
    def on_cloudflare(self) -> bool:
        return self.kind == "cloudflare"

    def as_dict(self) -> dict:
        return {**self.__dict__, "on_cloudflare": self.on_cloudflare}


def _nslookup(name: str, kind: str, timeout: float = 8.0) -> str:
    try:
        done = subprocess.run(["nslookup", f"-type={kind}", name], capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout


def parse_ns(text: str) -> list[str]:
    return sorted({m.lower().rstrip(".") for m in re.findall(r"nameserver\s*=\s*(\S+)", text)})


def parse_mx(text: str) -> list[str]:
    found = re.findall(r"MX preference\s*=\s*(\d+),\s*mail exchanger\s*=\s*(\S+)", text)
    return [host.lower().rstrip(".") for _pref, host in sorted(found, key=lambda f: int(f[0]))]


def parse_txt(text: str) -> list[str]:
    """TXT records as whole strings: nslookup prints each on its own quoted line(s) under 'text ='."""
    records, current = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("text =") or stripped == "text =":
            if current is not None:
                records.append(current)
            current = ""
        elif current is not None and stripped.startswith('"'):
            current += stripped.strip('"')
        elif current is not None and not stripped and current:
            # nslookup prints a blank line between "text =" and the value; only one after it ends it
            records.append(current)
            current = None
    if current is not None:
        records.append(current)
    return records


def parse_addresses(text: str) -> list[str]:
    """The answer's addresses: everything after the 'Name:' line (the server's own comes before)."""
    _, _, answer = text.partition("Name:")
    return re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}\b", answer)


def parse_cname(text: str) -> str:
    m = re.search(r"canonical name\s*=\s*(\S+)", text)
    return m.group(1).lower().rstrip(".") if m else ""


def recognise(nameservers: list[str]) -> tuple[str, str]:
    for ns in nameservers:
        for suffix, name, kind in HOSTS:
            if suffix in ns:
                return name, kind
    return "", ""


def look_up(domain: str, lookup=None) -> DomainReport:
    lookup = lookup or _nslookup
    domain = (domain or "").strip().lower().rstrip(".")
    domain = re.sub(r"^https?://", "", domain).split("/")[0]
    report = DomainReport(domain=domain)
    if not DOMAIN_RE.match(domain):
        report.error = "that does not look like a domain name (for example: example.com)"
        return report
    report.nameservers = parse_ns(lookup(domain, "NS"))
    if not report.nameservers:
        report.error = f"no nameservers answered for {domain} - is it registered, and spelt as registered?"
        return report
    report.host, report.kind = recognise(report.nameservers)
    report.mail = parse_mx(lookup(domain, "MX"))
    report.spf = next((t for t in parse_txt(lookup(domain, "TXT")) if t.lower().startswith("v=spf1")), "")
    report.dmarc = next((t for t in parse_txt(lookup(f"_dmarc.{domain}", "TXT")) if t.lower().startswith("v=dmarc1")), "")
    report.apex = parse_addresses(lookup(domain, "A"))
    report.www = parse_addresses(lookup(f"www.{domain}", "A"))
    report.autodiscover = parse_cname(lookup(f"autodiscover.{domain}", "CNAME"))
    return report
