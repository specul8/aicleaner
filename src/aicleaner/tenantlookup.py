"""What Microsoft's public endpoints say about a domain (feature 111, the Add Client wizard).

Entered on the wizard's Domains page, a domain is looked up automatically (the user, 2026-10-10:
"Yes, automatically"): the page says what is sent where. Three public, unauthenticated calls:

    login.microsoftonline.com/<domain>/.well-known/openid-configuration   is it a Microsoft tenant
    login.microsoftonline.com/getuserrealm.srf?login=...@<domain>          its sign-in brand name
    autodiscover-s.outlook.com (GetFederationInformation)                  the tenant's other domains,
                                                                           its <tenant>.onmicrosoft.com

What comes back are suggestions to tick, never entries: the tenant (<tenant>.onmicrosoft.com), its
SharePoint hosts (with -my and -admin), the tenant's other domains, and the brand name. The tenant's
GUID is not offered - GUIDs are stood in for on every request, never collected (DECISIONS 38).
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Callable

from .clientdrafts import sharepoint_twins

Fetch = Callable[[str, bytes | None, dict], bytes]
TIMEOUT_S = 8

FEDERATION_ENVELOPE = """<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:exm="http://schemas.microsoft.com/exchange/services/2006/messages"
  xmlns:ext="http://schemas.microsoft.com/exchange/services/2006/types"
  xmlns:a="http://www.w3.org/2005/08/addressing" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
<soap:Header>
  <a:Action soap:mustUnderstand="1">http://schemas.microsoft.com/exchange/2010/Autodiscover/Autodiscover/GetFederationInformation</a:Action>
  <a:To soap:mustUnderstand="1">https://autodiscover-s.outlook.com/autodiscover/autodiscover.svc</a:To>
</soap:Header>
<soap:Body>
  <GetFederationInformationRequestMessage xmlns="http://schemas.microsoft.com/exchange/2010/Autodiscover">
    <Request><Domain>{domain}</Domain></Request>
  </GetFederationInformationRequestMessage>
</soap:Body>
</soap:Envelope>"""
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")
SENT = ("login.microsoftonline.com (is it a Microsoft tenant, and its sign-in name) and "
        "autodiscover-s.outlook.com (the tenant's other domains)")


def _fetch(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:   # noqa: S310 - fixed https hosts
        return response.read()


def look_up(domain: str, fetch: Fetch | None = None) -> dict:
    """Suggestions for one domain, and what was asked. Never raises for a domain that is not a
    tenant, or a network that is down: the answer says so instead."""
    fetch = fetch or _fetch
    domain = str(domain or "").strip().lower()
    out: dict = {"domain": domain, "tenant": False, "suggestions": [], "sent_to": SENT, "error": ""}
    if not DOMAIN_RE.match(domain):
        out["error"] = "not a domain"
        return out
    try:
        config = json.loads(fetch(f"https://login.microsoftonline.com/{urllib.parse.quote(domain)}"
                                  "/.well-known/openid-configuration", None, {}))
    except Exception:  # noqa: BLE001 - "not a tenant" and "no network" both end here, and say so
        out["error"] = f"{domain} is not a Microsoft tenant, or Microsoft could not be reached"
        return out
    if not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", str(config.get("issuer", ""))):
        out["error"] = f"{domain} is not a Microsoft tenant"
        return out
    out["tenant"] = True
    suggestions: list[dict] = []

    def offer(value: str, kind: str, why: str) -> None:
        if value and value.lower() != domain and not any(s["value"].lower() == value.lower() for s in suggestions):
            suggestions.append({"value": value, "kind": kind, "why": why})

    try:
        realm = json.loads(fetch("https://login.microsoftonline.com/getuserrealm.srf?json=1&login="
                                 + urllib.parse.quote(f"nobody@{domain}"), None, {}))
        brand = str(realm.get("FederationBrandName") or "").strip()
        if brand:
            offer(brand, "name", "the name Microsoft sign-in shows for this tenant")
    except Exception:  # noqa: BLE001 - the brand is a nice-to-have
        pass
    try:
        xml = fetch("https://autodiscover-s.outlook.com/autodiscover/autodiscover.svc",
                    FEDERATION_ENVELOPE.format(domain=domain).encode("utf-8"),
                    {"Content-Type": "text/xml; charset=utf-8", "User-Agent": "Microsoft Office/16.0",
                     "SOAPAction": '"http://schemas.microsoft.com/exchange/2010/Autodiscover/Autodiscover/GetFederationInformation"'}
                    ).decode("utf-8", errors="replace")
        domains = sorted({d.lower() for d in re.findall(r"<Domain>([^<]+)</Domain>", xml)})
    except Exception:  # noqa: BLE001
        domains = []
    tenants = [d for d in domains if d.endswith(".onmicrosoft.com") and not d.endswith(".mail.onmicrosoft.com")]
    for tenant in tenants[:1]:
        offer(tenant, "tenant", "the tenant's own Microsoft domain")
        for host in sharepoint_twins(f"{tenant.split('.')[0]}.sharepoint.com"):
            offer(host, "sharepoint", "the tenant's SharePoint")
    for other in domains:
        if not other.endswith(".onmicrosoft.com"):
            offer(other, "domain", "another domain of the same tenant")
    out["suggestions"] = suggestions
    return out
