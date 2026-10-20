"""AI services other than Claude Code in Project sessions (plan item K; spec
2026-10-20-agents-in-project-sessions.md).

A session's process speaks Claude Code's stream-json to the Session object whatever drives it: for
GitHub Copilot CLI and Codex the process is aicleaner's bridge (bridge.py), which speaks stream-json on
one side and the agent's own protocol on the other - the Agent Client Protocol for Copilot CLI
(`copilot --acp`), the app-server's JSON-RPC for Codex. Everything around a session - the broker, the
queue, interrupts, permission cards, titles, the page - works unchanged.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Agent:
    id: str            # stored in a profile, sent by the page
    label: str         # what the page's drop-down says
    cli: str           # the command on PATH
    package: str       # what `npm install` installs when it is missing
    reader: str        # the forward proxy's reader that filters its traffic


CLAUDE = Agent("claude", "Claude Code", "claude", "@anthropic-ai/claude-code", "anthropic")
COPILOT = Agent("copilot", "GitHub Copilot", "copilot", "@github/copilot", "copilot")
CODEX = Agent("codex", "ChatGPT (Codex)", "codex", "@openai/codex", "openai")
AGENTS = {a.id: a for a in (CLAUDE, COPILOT, CODEX)}


def agent(name: str) -> Agent:
    return AGENTS.get((name or "claude").strip().lower(), CLAUDE)


def transcripts_root(state_dir: str | Path, which: Agent) -> Path | None:
    """Where the bridge files this agent's conversations (Claude Code's own: None - its own folder)."""
    if which.id == "claude":
        return None
    return Path(state_dir) / "agents" / which.id / "projects"


def install_dir() -> Path:
    """aicleaner's own folder for the agents it installs (`npm install --prefix`): no administrator, no
    PATH change. AICLEANER_AGENTS_DIR for tests."""
    from ..codetab_settings import home      # AICLEANER_CLIENT_HOME in tests: never the real profile

    override = os.environ.get("AICLEANER_AGENTS_DIR", "").strip()
    return Path(override) if override else home() / ".aicleaner" / "agents"


def command_for(which: Agent) -> list[str]:
    """How to run the agent's CLI: on PATH as it is, else aicleaner's own copy; [] when neither.
    AICLEANER_AGENT_<ID> (a JSON list) stands in for it in tests."""
    stand_in = os.environ.get(f"AICLEANER_AGENT_{which.id.upper()}", "").strip()
    if stand_in:
        return list(json.loads(stand_in))
    found = shutil.which(which.cli)
    if not found:
        mine = install_dir() / "node_modules" / ".bin" / (f"{which.cli}.cmd" if os.name == "nt" else which.cli)
        found = str(mine) if mine.exists() else ""
    if not found:
        return []
    return ["cmd", "/c", found] if found.lower().endswith((".cmd", ".bat")) else [found]


def protocol_args(which: Agent) -> list[str]:
    """The flags that make the CLI speak its protocol on stdio. Codex is held to OpenAI's own provider
    (security review of v0.9.290): a custom provider or base URL in its config would send the
    conversation somewhere aicleaner does not read."""
    return {"copilot": ["--acp"],
            "codex": ["app-server", "-c", 'model_provider="openai"']}.get(which.id, [])


# Environment that would point an agent somewhere aicleaner does not read: taken out of its process.
UNREAD_ENV = {"codex": ("OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_BASE_URL")}
