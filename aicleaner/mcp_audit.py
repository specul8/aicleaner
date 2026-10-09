"""Which MCP servers this machine has, and which of them send what they are given somewhere else.

`keep_tokenised_tools` decides whose tool-call arguments stay tokenised. Maintained by hand it is
always one server out of date, and the cost of missing one is the thing this tool exists to
prevent: the real name goes out in a tool call rather than in the prompt.

Servers are found where they actually live, which is four places, not one:

  * ``~/.claude.json``: ``mcpServers``, and the per-project ``projects.<path>.mcpServers``
  * ``.mcp.json`` in a project folder
  * installed plugins: ``~/.claude/plugins/installed_plugins.json`` names each install folder, whose
    ``plugin.json`` points at the ``.mcp.json`` beside it
  * Claude Desktop's ``claude_desktop_config.json`` (including the packaged build's container)

claude.ai connectors are configured in the account rather than on disk, so they cannot be
enumerated here; they are hosted by definition and are covered by a pattern.

Each server is then placed in one of four classes:

  ``hosted``      the config names a URL: whatever is sent goes straight out
  ``bridged``     a local command that is a front for a remote server (mcp-remote and friends)
  ``egress``      a local process that talks to a service anyway (the GitHub server runs here and
                  still posts to github.com) - the class people forget
  ``local``       a local process with nothing known about where it sends things

The first three are reported as "keep tokenised". The last is reported as "decide": running
locally is not the same as keeping data local, and this tool will not pretend otherwise.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# Commands whose whole job is to relay to a remote server.
BRIDGES = ("mcp-remote", "supergateway", "mcp-proxy", "mcp-gateway")

# Servers that run as a local process and send what they are given to a service. The name is the
# giveaway: these are the vendors' own servers for their own APIs. Substring match on the package.
CLOUD_PACKAGES = (
    "github", "gitlab", "bitbucket", "atlassian", "jira", "confluence", "linear", "asana",
    "notion", "slack", "todoist", "monday", "airtable", "trello", "zendesk", "freshdesk",
    "sentry", "datadog", "newrelic", "pagerduty", "cloudflare", "vercel", "netlify", "heroku",
    "azure", "aws", "gcp", "google", "firebase", "supabase", "planetscale", "neon",
    "stripe", "hubspot", "salesforce", "twilio", "sendgrid", "shopify", "spotify",
    "brave-search", "exa", "tavily", "perplexity", "firecrawl", "context7", "microsoft-docs",
    "microsoft_docs", "learn", "prisma", "openai", "anthropic", "huggingface",
)

CONNECTOR_PATTERN = "mcp__claude_ai_*"


@dataclass(frozen=True)
class Server:
    """One MCP server, wherever it was declared."""

    name: str
    source: str            # where the declaration was found, for the report
    pattern: str           # the tool-name pattern Claude Code uses for it
    verdict: str           # hosted | bridged | egress | local
    reason: str

    @property
    def keep_tokenised(self) -> bool:
        return self.verdict in ("hosted", "bridged", "egress")


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


# Flags whose value names where the package is fetched FROM, which says nothing about where the
# server sends anything. `uvx --from git+https://github.com/oraios/serena` is a local code tool,
# not a bridge to GitHub, and reading it as one would tokenise the arguments of a tool that needs
# them real.
SOURCE_FLAGS = ("--from", "--index-url", "--extra-index-url", "--find-links", "-f", "--registry")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")


def egress_words(config: dict[str, Any]) -> str:
    """The command line with package sources taken out, lowercased, for judging egress."""
    command = _text(config.get("command"))
    args = config.get("args") if isinstance(config.get("args"), list) else []
    words: list[str] = [command]
    skip_next = False
    for raw in args:
        word = str(raw)
        if skip_next:
            skip_next = False
            continue
        if word.lower() in SOURCE_FLAGS:
            skip_next = True                      # the value after it is a source, not a target
            continue
        if word.lower().startswith(("git+", "file:", "./", "../")):
            continue
        words.append(word)
    return " ".join(words).lower()


def classify(name: str, config: dict[str, Any]) -> tuple[str, str]:
    """(verdict, reason) for one server's configuration."""
    url = _text(config.get("url"))
    if url or str(config.get("type", "")).lower() in ("sse", "http", "streamable-http"):
        if any(host in url for host in LOCAL_HOSTS):
            return "local", f"a URL on this machine ({url[:40]})"
        return "hosted", f"config names a URL ({url[:60] or config.get('type')})"
    command = _text(config.get("command"))
    whole = egress_words(config)
    for bridge in BRIDGES:
        if bridge in whole:
            return "bridged", f"{bridge} relays to a remote server"
    if ("http://" in whole or "https://" in whole) and not any(host in whole for host in LOCAL_HOSTS):
        return "bridged", "a URL is passed on the command line"
    haystack = f"{name} {whole}"
    for vendor in CLOUD_PACKAGES:
        if vendor in haystack:
            return "egress", f"runs here, but talks to {vendor}"
    if not command:
        return "local", "no command and no URL: nothing to judge"
    return "local", "a local command with no known service behind it"


def _servers_from(mapping: Any, source: str, pattern: str = "mcp__{name}__*") -> list[Server]:
    found: list[Server] = []
    if not isinstance(mapping, dict):
        return found
    for name, config in mapping.items():
        if not isinstance(config, dict):
            continue
        verdict, reason = classify(str(name), config)
        found.append(Server(name=str(name), source=source, pattern=pattern.format(name=name),
                            verdict=verdict, reason=reason))
    return found


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _server_map(document: dict[str, Any]) -> dict[str, Any]:
    """The servers in a file that may or may not wrap them.

    Both shapes ship today: ``{"mcpServers": {...}}`` and the bare ``{"<name>": {...}}``. Reading
    only the first shape is how the GitHub plugin's server went unnoticed.
    """
    if isinstance(document.get("mcpServers"), dict):
        return document["mcpServers"]
    bare = {name: config for name, config in document.items()
            if isinstance(config, dict) and ({"command", "url", "type"} & set(config))}
    return bare


def _plugin_servers(home: Path) -> list[Server]:
    """Servers a plugin brings with it: plugin.json points at the .mcp.json beside it."""
    installed = _read(home / ".claude" / "plugins" / "installed_plugins.json")
    found: list[Server] = []
    for full_name, entries in (installed.get("plugins") or {}).items():
        plugin = str(full_name).split("@", 1)[0]
        for entry in entries if isinstance(entries, list) else []:
            folder = Path(str(entry.get("installPath", "")))
            if not folder.is_dir():
                continue
            declared: Any = None
            for manifest in (folder / ".claude-plugin" / "plugin.json", folder / "plugin.json"):
                if manifest.is_file():
                    declared = _read(manifest).get("mcpServers")
                    if declared is not None:
                        break
            if isinstance(declared, str):          # a pointer to a file beside the manifest
                declared = _server_map(_read((folder / declared).resolve()))
            if not declared and (folder / ".mcp.json").is_file():
                declared = _server_map(_read(folder / ".mcp.json"))
            found += _servers_from(declared, f"plugin {plugin}",
                                   "mcp__plugin_" + plugin + "_{name}__*")
    return found


def discover(home: Path | None = None, project: Path | None = None) -> list[Server]:
    """Every MCP server this machine declares, from every place they are declared."""
    home = home or Path(os.path.expanduser("~"))
    found: list[Server] = []

    claude_json = _read(home / ".claude.json")
    found += _servers_from(claude_json.get("mcpServers"), "~/.claude.json")
    for folder, settings in (claude_json.get("projects") or {}).items():
        if isinstance(settings, dict):
            found += _servers_from(settings.get("mcpServers"), f"project {folder}")

    if project:
        found += _servers_from(_server_map(_read(project / ".mcp.json")), f"{project}/.mcp.json")

    found += _plugin_servers(home)

    desktop = [home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"]
    packages = home / "AppData" / "Local" / "Packages"
    if packages.is_dir():
        desktop += sorted(packages.glob("Claude_*/LocalCache/Roaming/Claude/claude_desktop_config.json"))
    for path in desktop:
        if path.is_file():
            found += _servers_from(_read(path).get("mcpServers"), "Claude Desktop")

    seen: dict[str, Server] = {}
    for server in found:
        seen.setdefault(server.pattern, server)
    return sorted(seen.values(), key=lambda s: (s.verdict, s.pattern))


def patterns_to_keep(servers: Iterable[Server]) -> list[str]:
    """The tool patterns whose arguments must not be rehydrated, connectors included."""
    keep = {s.pattern for s in servers if s.keep_tokenised}
    keep.add(CONNECTOR_PATTERN)     # hosted in the account: never enumerable, always remote
    return sorted(keep)


def report(servers: list[Server], configured: list[str]) -> list[str]:
    """Lines for the console: what was found, what it means, and what is not covered yet."""
    lines: list[str] = []
    if not servers:
        lines.append("No MCP servers are declared on this machine.")
    else:
        width = max(len(s.pattern) for s in servers)
        for verdict, heading in (("hosted", "Hosted elsewhere"), ("bridged", "A front for a remote server"),
                                 ("egress", "Runs here, sends elsewhere"), ("local", "Local, unknown egress")):
            group = [s for s in servers if s.verdict == verdict]
            if not group:
                continue
            lines.append("")
            lines.append(f"{heading}:")
            for server in group:
                covered = "kept" if any(_matches(server.pattern, p) for p in configured) else "NOT KEPT"
                lines.append(f"  {server.pattern:<{width}}  {covered:<8}  {server.source} - {server.reason}")
    lines.append("")
    lines.append("claude.ai connectors are configured in your account, not on this machine, so they")
    lines.append(f"cannot be listed here. {CONNECTOR_PATTERN} covers them.")
    return lines


def _matches(pattern: str, configured: str) -> bool:
    """True when what is already configured covers this pattern (equal, or a wider wildcard)."""
    import fnmatch

    return configured == pattern or fnmatch.fnmatch(pattern, configured)
