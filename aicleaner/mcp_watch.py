"""The MCP audit, run when the proxy and each session start rather than once at install (H11).

`mcp_audit` finds and classifies every server; this keeps the latest answer and turns it into the
tool patterns whose arguments stay tokenised. The rule is the user's (DECISIONS 52):

  * reached over http(s) - `hosted`, or a local `bridged` command that relays to a remote server -
    gets tokens, until approved on the Overview;
  * running on this machine as a stdio command - `local`, and `egress` too - gets real names, as
    today. An egress server (it runs here and posts to its service) is reported, so it can be added
    to keep_tokenised_tools by hand or with `aicleaner mcp-audit --write`.

Approvals are kept in the state folder, never in the config file, and survive restarts.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from . import fileio
from .mcp_audit import CONNECTOR_PATTERN, Server, discover

log = logging.getLogger("aicleaner")

APPROVED_FILE = "mcp-approved.json"
REACHED_OVER_HTTP = ("hosted", "bridged")


class McpWatch:
    def __init__(self, state_dir: Path | None, home: Path | None = None):
        self.path = Path(state_dir) / APPROVED_FILE if state_dir else None
        self.home = home
        self.servers: list[Server] = []
        self._lock = threading.Lock()
        self._approved = self._load()

    def _load(self) -> set[str]:
        if self.path is None:
            return set()
        try:
            return {str(p) for p in json.loads(self.path.read_text(encoding="utf-8")).get("approved", [])}
        except (OSError, ValueError, AttributeError):
            return set()

    def audit(self, projects: list[str]) -> list[Server]:
        """Discover every server: the machine's own, and each project folder's .mcp.json. A config
        file that cannot be read contributes nothing rather than stopping the audit."""
        found: dict[str, Server] = {}
        for project in [None, *dict.fromkeys(p for p in projects if p)]:
            try:
                servers = discover(home=self.home, project=Path(project) if project else None)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                log.warning("MCP audit could not read %s: %s", project or "this machine's config", exc)
                continue
            for server in servers:
                found.setdefault(server.pattern, server)
        with self._lock:
            new = sorted(set(found) - {s.pattern for s in self.servers})
            self.servers = sorted(found.values(), key=lambda s: (s.verdict, s.pattern))
        for pattern in new:
            server = found[pattern]
            names = "tokens until approved" if self._tokenised(server) else "real names"
            log.info("MCP server %s (%s, %s): %s", pattern, server.verdict, server.source, names)
        return self.servers

    def _tokenised(self, server: Server) -> bool:
        return server.verdict in REACHED_OVER_HTTP and server.pattern not in self._approved

    def keep_patterns(self, configured: list[str]) -> list[str]:
        """What stays tokenised: the configured patterns, unapproved remote servers, connectors."""
        with self._lock:
            auto = {s.pattern for s in self.servers if self._tokenised(s)}
        return sorted(set(configured) | auto | {CONNECTOR_PATTERN})

    def approve(self, pattern: str) -> None:
        """Real names for a remote server, decided by a person on the Overview."""
        with self._lock:
            self._approved.add(pattern)
            approved = sorted(self._approved)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"approved": approved}, indent=2), encoding="utf-8")
            fileio.replace(tmp, self.path)

    def summary(self, configured: list[str] | None = None) -> list[dict[str, Any]]:
        """One row per server. A server keep_tokenised_tools already covers is "kept by config":
        that is a decision someone made, not something waiting for approval."""
        import fnmatch

        with self._lock:
            servers = list(self.servers)
        rows = []
        for s in servers:
            by_config = any(p == s.pattern or fnmatch.fnmatch(s.pattern, p) for p in configured or [])
            waiting = self._tokenised(s) and not by_config
            rows.append({"pattern": s.pattern, "verdict": s.verdict, "source": s.source, "reason": s.reason,
                         "names": "tokens" if (by_config or waiting) else "real", "approvable": waiting,
                         "kept_by": "config" if by_config else ("audit" if waiting else "")})
        return rows
