"""One isolated model request through this proxy (2026-10-17).

AI ranking, conversation titles and consolidation summaries each ask the model one thing. They used to
run `claude --print` in the proxy's own folder with the person's settings - so the person's plugins,
hooks and memory came along: a live consolidation on 2026-10-17 was handed another project's memory
notes by a plugin hook and summarised those instead. Now every such request runs:

- in a new empty folder (no project CLAUDE.md, no .claude, no git),
- with `--restricted` (user, project and local settings files ignored: no plugins, no hooks),
- with no tools at all (`--tools ""`), no MCP servers (`--strict-mcp-config`), no skills, one turn,
- not saved as a session (`--no-session-persistence`),
- through this proxy (ANTHROPIC_BASE_URL), so it is filtered like every request.

Checked 2026-10-17: so run, the model reports no CLAUDE.md, memory or project notes in its context.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable

ISOLATION = ["--max-turns", "1", "--tools", "", "--restricted", "--strict-mcp-config",
             "--no-session-persistence", "--disable-slash-commands"]


def command(claude: str, model: str = "") -> list[str]:
    cmd = [claude, "--print", "--output-format", "json", *ISOLATION]
    if model:
        cmd += ["--model", model]
    return cmd


def _reason(done: Any) -> str:
    """Why a request failed, readably: the JSON answer's own text when there is one (a refusal says
    what happened in its first line), else the end of what was printed."""
    try:
        answer = json.loads(done.stdout)
        said = " ".join(str(answer.get("result", "")).split()) if isinstance(answer, dict) else ""
    except ValueError:
        said = ""
    if said:
        return said[:300]
    return (done.stderr or done.stdout or "the request failed").strip()[-300:]


def ask(claude: str, base_url: str, prompt: str, timeout: int, model: str = "",
        run: Callable[..., Any] = subprocess.run) -> str:
    """The model's answer text. Raises RuntimeError with the reason when there is none."""
    env = dict(os.environ)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    folder = tempfile.mkdtemp(prefix="aicleaner-ask-")
    try:
        done = run(command(claude, model), input=prompt, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=timeout, env=env, cwd=folder)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"no answer within {timeout} seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"could not start {claude}: {exc}") from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if done.returncode != 0:
        raise RuntimeError(_reason(done))
    try:
        answer = json.loads(done.stdout)
        text = answer.get("result", "") if isinstance(answer, dict) else str(answer)
    except ValueError:
        text = done.stdout
    if not str(text).strip():
        raise RuntimeError("the answer came back empty")
    return str(text)
