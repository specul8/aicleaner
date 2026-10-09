"""aicleaner seed CLIENTID [--index N] [--resources]: fill a client's forms from Azure and Graph.

Requires an `az login` against the client's tenant. Reads:
  az account show / list            tenant id, subscription ids and names (this tenant only)
  Graph /v1.0/organization          display name, verified domains (incl. onmicrosoft.com)
  --resources:                      every resource, resource group and management group name in
                                    those subscriptions, which is the quickest way to fill in the
                                    names that would otherwise be met one request at a time
Adds forms the mapping does not have yet, with shape-aware tokens (a name that carries the client's
own name keeps its shape: kvaueprdclient05management). Identifiers that are the same in every
tenant are skipped. Nothing is removed and existing forms are left alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .detect import is_public
from .mapping import SOURCE_SCAN, MappingError, classify
from .store import MappingStore

GENERIC_SUB_NAMES = {"pay-as-you-go", "visual studio enterprise", "azure subscription 1", "free trial", "production", "development", "test"}
# Resource names every tenant has: they identify nobody and tokenising them costs the model context.
GENERIC_RESOURCE_NAMES = {
    "default", "master", "gatewaysubnet", "azurebastionsubnet", "azurefirewallsubnet",
    "azurefirewallmanagementsubnet", "routeserversubnet", "nat", "web", "api", "db", "data",
    "network", "storage", "backup", "monitoring", "logs", "shared", "common", "main", "primary",
    "secondary", "public", "private", "internal", "external", "test", "dev", "prod", "uat",
}


def _az(args: list[str]) -> Any:
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        raise RuntimeError("az CLI not found on PATH")
    proc = subprocess.run([exe, *args, "-o", "json"], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"az {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout or "null")


def collect_resource_names() -> list[tuple[str, str]]:
    """Resource, resource group and management group names in the signed-in tenant's subscriptions.

    These are the names that otherwise turn up one at a time in a session; generic names that every
    tenant has are left out, as are names too short to be worth matching.
    """
    out: list[tuple[str, str]] = []
    for query in (["resource", "list", "--query", "[].name"],
                  ["group", "list", "--query", "[].name"],
                  ["account", "management-group", "list", "--query", "[].{n:displayName}"]):
        try:
            values = _az(query) or []
        except RuntimeError as exc:
            print(f"  skipped {' '.join(query[:2])}: {exc}")
            continue
        for value in values:
            name = str(value.get("n") if isinstance(value, dict) else value).strip()
            if len(name) < 5 or name.lower() in GENERIC_RESOURCE_NAMES or is_public(name):
                continue
            out.append((name, classify(name)))
    return out


def collect_identifiers() -> list[tuple[str, str]]:
    """Return (real, kind) pairs for the signed-in tenant."""
    out: list[tuple[str, str]] = []
    account = _az(["account", "show"])
    tenant_id = account.get("tenantId")
    # The tenant and subscription ids are not collected: a GUID is stood in for on every request
    # instead (DECISIONS 38). They are still read, to tell this tenant's subscriptions from others.
    for sub in _az(["account", "list"]) or []:
        if sub.get("tenantId") != tenant_id:
            continue
        name = (sub.get("name") or "").strip()
        if name and name.lower() not in GENERIC_SUB_NAMES and classify(name) == "name":
            out.append((name, "name"))
    org = _az(["rest", "--method", "get", "--url", "https://graph.microsoft.com/v1.0/organization"])
    for o in (org or {}).get("value", []):
        if o.get("displayName"):
            out.append((o["displayName"].strip(), "name"))
        for d in o.get("verifiedDomains", []) or []:
            name = (d.get("name") or "").strip().lower()
            if name:
                out.append((name, classify(name)))
    # de-duplicate, keep first occurrence order
    seen: set[str] = set()
    uniq = []
    for real, kind in out:
        if real.lower() not in seen:
            seen.add(real.lower())
            uniq.append((real, kind))
    return uniq


def seed(mapping_path: Path, client_id: str, index: int | None, clients_dir: Path | None = None,
         resources: bool = False, state_dir: Path | None = None) -> list[tuple[str, str]]:
    """Add missing forms for client_id; returns [(real, token)] that were added."""
    mf = MappingStore(mapping_path, clients_dir, state_dir)
    loaded = mf.load()
    if client_id not in loaded.sources:
        mf.add_client(client_id, index)
        loaded = mf.load()
    existing = {f["real"].lower() for c in loaded.data["clients"] if c.get("id") == client_id for f in c.get("forms", [])}
    wanted = collect_identifiers() + (collect_resource_names() if resources else [])
    fresh: list[str] = []
    for real, _kind in wanted:
        if real.lower() in existing or is_public(real):
            continue
        existing.add(real.lower())
        fresh.append(real)
    if not fresh:
        return []
    # One validate-and-write for the lot. This was a loop calling add_form, and each of those
    # revalidated and rebuilt the whole mapping (D36) - so seeding a client with a few hundred
    # identifiers cost a few hundred rebuilds. add_forms keeps the ones that hold and reports the
    # rest, which is the same tolerance the loop had, done once.
    added = mf.add_forms(client_id, fresh, source=SOURCE_SCAN)
    for real in fresh:
        if real not in {r for r, _t in added}:
            print(f"  skipped {real!r}: refused by the mapping")
    return added
