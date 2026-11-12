# Architecture

[Home](Home.md) > Architecture

This page is for anyone who wants to know how AI Cleaner is built: what it is made of, which part
does what, the path one request takes, where data is kept, what protects it, and how a release
reaches a machine. It links to the pages that explain each feature in use rather than repeating
them. Module names are the files under `aicleaner\` in the install folder (`src\aicleaner\` in the
repository).

## Purpose

AI Cleaner keeps the real terms of your topics on your machine while you use AI coding assistants.
A topic is anything whose uniquely identifiable information (terms) you want to keep out of AI services: a client, a product or a person could all be topics you might want to protect. AI
Cleaner is a local proxy: AI tools are pointed at it instead of at the AI service, and every request
passes through it. On the way out it replaces each known name, domain, address and ID with a stable
token, holds back identifiers it does not know yet, redacts secrets and refuses to send anything
that still holds a real term. On the way back it puts the real terms into the reply, so you, your
files and your local tools see the real terms while the AI service only ever sees tokens. Around
that core it has a browser UI for managing topics and driving Claude Code sessions, and optional
remote access through a Cloudflare tunnel. The one-line summary: **real terms on the machine,
tokens on the wire.**

## Technology stack

| Layer | What is used | Notes |
|---|---|---|
| Platform | Windows 10 and 11 | Several parts use Windows APIs directly (DPAPI, job objects, the certificate store, scheduled tasks, Windows OCR). Some modules fall back to plain files or no-ops elsewhere, but Windows is the supported platform. |
| Language | Python 3.11 or later (3.13 preferred) | The installer builds its own virtual environment in `%USERPROFILE%\.aicleaner\venv`; the locked wheels cover 3.11 to 3.14. |
| Web server and HTTP client | aiohttp 3.x | One asyncio process serves the proxy routes, the UI and its JSON API. Calls to the AI service use aiohttp's client. |
| Cryptography | The `cryptography` package | AES-256-GCM, ECDH and HKDF for the encrypted tunnel, and the local certificate authorities. Cloudflare Access token checks (RS256) use only the standard library. |
| Other Python | Standard library | Office documents are read from their XML parts with `zipfile` and `ElementTree`; hooks are standard-library only. |
| Dependency pinning | `requirements.lock` | Installed with `pip --require-hashes --only-binary :all:`: exactly the hashed wheels, nothing built on the machine. |
| Front end | Plain HTML, CSS and JavaScript (ES modules) | No framework and no build step for the app's own code: `web\index.html` loads `web\js\app.js`, which imports one module per page. |
| Code editor | Monaco (the editor VS Code is built on) | A bundled copy in `web\vendor\monaco`, used by the file editor on the Project Sessions page. |
| Text from pictures and PDFs | Windows' own OCR engine (the one Snipping Tool uses), Poppler (`pdftotext`, `pdftoppm`) | Through `installer\Read-ImageText.ps1`; Tesseract is used when installed. |
| Secrets at rest | Windows DPAPI (current user) | API keys, topic keys, tunnel code, certificate authority keys, the Cloudflare token. |
| Remote access | Cloudflare Tunnel (`cloudflared`) and Cloudflare Access | Optional. The tunnel dials out; Access does the sign-in; AI Cleaner checks Access's token itself. |
| Installer and service scripts | PowerShell (Windows PowerShell 5.1 or PowerShell 7) | `Deploy-AiCleaner.ps1` and the scripts beside it; scheduled tasks start the proxy. |
| Release signing | OpenSSH (`ssh-keygen -Y verify`) | Built into Windows 10 and 11. |
| Remote desktop | **aicleaner-rdp**, a separate add-on | Not part of AI Cleaner itself; the menu only links to it (see [Remote access](Remote-access.md#remote-desktops)). |

## The parts at a glance

```
  Claude Code (terminal, VS Code)          Claude Desktop            VS Code + GitHub Copilot
        |  ANTHROPIC_BASE_URL                 | gateway / HTTPS_PROXY       | http.proxy
        v                                     v                             v
  +----------------------------------------------------------------------------------+
  |  AI Cleaner, one Python process on this machine                                  |
  |                                                                                  |
  |  127.0.0.1:8787   proxy routes  /anthropic/...  /openai/...  /session/<p>/...    |
  |                   UI  /   and   JSON API  /api/...   (loopback, or the tunnel)    |
  |  127.0.0.1:8788   forward proxy (CONNECT) + readers for the Code tab, Copilot,   |
  |                   claude.ai and Codex                                            |
  |                                                                                  |
  |  filtering engine: mapping -> tokenise -> detect -> redact -> hold back          |
  |                    -> canary  ...  reply -> rehydrate                             |
  +----------------------------------------------------------------------------------+
        |  tokens only, over TLS                  ^
        v                                         |  session brokers (one process each)
  api.anthropic.com / api.openai.com /            |  -> Claude Code, Copilot CLI, Codex
  Copilot / claude.ai                             |
                                     browser on this machine, or another device
                                     through Cloudflare Access + tunnel
```

## Component map

Modules are grouped by role. Smaller helpers are left out; the module's own opening comment says
what it does.

### Proxy and dialects

| Module | Role |
|---|---|
| `proxy.py` | The core. Registers `/{dialect}/{tail}` and `/session/{profile}/{dialect}/{tail}`, runs the outbound pipeline, forwards to the AI service, rewrites the reply, writes the evidence line. Also `/healthz`, `/api/tokenise` and `/api/rehydrate`. |
| `config.py` | Reads `aicleaner-config.json` with defaults: `listen` `127.0.0.1`, `port` 8787, `upstreams` `anthropic` -> `https://api.anthropic.com` and `openai` -> `https://api.openai.com`. |
| `dialects\anthropic.py` | Streaming rewriter for the Messages API: text, thinking and tool-call argument deltas, one tail buffer per content block. |
| `dialects\openai_chat.py`, `openai_responses.py`, `openai_completions.py` | Streaming rewriters for OpenAI Chat Completions, the Responses API (Copilot chat and agent) and legacy Completions (Copilot inline suggestions). |
| `dialects\__init__.py` | Picks a rewriter for a dialect; a generic one rewrites every string in every event, without tail buffering, for anything not modelled. |
| `sse.py` | Server-sent-events parsing and the tail-buffered rewriter that keeps a token from being split across two events. |
| `attachments.py`, `convert.py`, `picture.py` | PDFs, Office files and images in a request become text that is filtered like the rest, or are withheld with a note saying so; with pictures allowed, the real terms can be blacked out of an image. |
| `acceptguard.py`, `shutdown.py` | Keep the listeners alive when a client drops a connection mid-accept, and leave promptly when Windows logs off or shuts down. |

### Filtering engine

| Module | Role |
|---|---|
| `mapping.py` | Loads and validates topic files and compiles them into one two-way substitution: longest term first, whole word, case-insensitive with the typed case restored. Derives tokens from each value's shape. |
| `transform.py` | Applies a function to every string - values and keys - of a JSON document: tokenise, rehydrate, and the **canary**, which re-reads the finished body for any declared real term. |
| `detect.py` | Learning mode: sweeps the already-tokenised text for anything still shaped like an identifier (emails, domains, tenants, SharePoint hosts, IPs, DevOps names, resource names carrying a topic's name) and holds them back behind temporary tokens. |
| `redact.py`, `usersecrets.py` | Secret redaction by family (or refusal), plus literal strings you entered yourself, stored DPAPI-encrypted. |
| `evidence.py` | One line per request with counts only; beside it the substitution log with the actual values, kept locally. |
| `notify.py` | The review footer added to a reply and the Windows notification. |

### Topic files and scanning

| Module | Role |
|---|---|
| `store.py` | Where the mapping lives on disk and every way it changes. Every write is validated by building a full mapping from the merged result before the file is replaced. |
| `stamp.py`, `keys.py` | The integrity stamp on each topic file, and the optional per-topic key that signs it (HMAC-SHA256). See [Sharing a topic with a team](Sharing.md). |
| `fileio.py` | Atomic replace that waits out OneDrive or a scanner holding the file. |
| `clientdrafts.py`, `tenantlookup.py`, `seed.py` | The Add Topic wizard's drafts; the public Microsoft endpoints it asks about a domain; filling a topic from the Azure tenant you are signed in to. |
| `scan.py`, `scanjob.py` | Folder scans with the proxy's own detectors: what a folder would leak, and adding it to a topic. Run from the topic's page or the command line. |
| `provenance.py`, `who.py` | Where each term came from, and who was driving when something changed - kept on this machine. |
| `shares.py` | Each topic's project assets folder in OneDrive, for passing files to and from another device. |

### Sessions

| Module | Role |
|---|---|
| `session.py` | Runs Claude Code headless (`claude --print` with stream-json in and out), one AI session per project session, with `ANTHROPIC_BASE_URL` pointing back at the proxy so a session is filtered like any other. |
| `broker.py` | One small process per session that owns the agent's input and output, numbers every event and serves it on loopback behind a per-broker token. A proxy restart reconnects to it instead of losing or duplicating the session. |
| `winjob.py`, `proctree.py`, `lifecycle.py` | Windows job objects so a session's whole process tree ends together; process snapshots; a lifecycle log of starts, ends and leftovers. |
| `mcp_approve.py` | The approval host: a small MCP server Claude Code asks for permission, which shows the question as a card on the page. |
| `history.py`, `titles.py`, `convsearch.py`, `consolidate.py`, `convbin.py`, `convmove.py` | Earlier conversations read from Claude Code's transcripts: listing, titles, search, summaries, recoverable delete, moving between sessions. |
| `worktree.py`, `workspace.py`, `setupcheck.py`, `claudetrust.py` | A second session on a project in its own git worktree; which repository is primary; whether a project is set up and trusted. |
| `profiles.py`, `presets.py`, `casual.py` | Project sessions, Start Work and Start Personal sets, and the Casual session. |
| `agents\bridge.py` | Lets GitHub Copilot CLI (Agent Client Protocol) and Codex (its app-server JSON-RPC) run as a session: speaks Claude Code's stream-json to the session on one side and the agent's own protocol on the other. |
| `oneshot.py` | One isolated model request (titles, summaries, ranking), through the proxy, with no settings, tools or plugins. |
| `alerts.py` | Persistent alerts raised by scripts or sessions, shown on every device until dismissed. |

See [Project Sessions](Project-Sessions.md) for how these look in use.

### Web UI and API

| Part | Role |
|---|---|
| `ui.py` | Serves the single-page app from `web\` and the JSON API behind it (status, clients, review, settings, sessions, files, wiki, setup). |
| `guard.py` | The request guard in front of every UI and API route (see the security model below). |
| `web\index.html`, `web\js\app.js` | The app shell: a hash router (`#/overview`, `#/session`, `#/settings/...`), the status stack, navigation and a status poll every 10 seconds. |
| `web\js\pages\*.js` | One module per page: overview, review, clients, secrets, settings, session, log, wiki, setup, alerts. |
| `web\js\*.js` | Shared pieces: the API client (`api.js`), DOM helper (`dom.js`), Markdown renderer, file tabs and editor, the sealed-tunnel client (`sealed.js`). |
| `web\css\app.css`, `editor.css` | Styles. No inline script or style is allowed by the page's content security policy. |

Session output reaches the page as server-sent events (`/api/session/events`). Through the
encrypted tunnel the page polls instead, because an event stream cannot be sealed.

### Remote access

| Module | Role |
|---|---|
| `access.py` | Verifies the Cloudflare Access token on each tunnelled request: RS256 signature against the team's published keys, issuer, audience, expiry, and the email address against `remote.allowed_emails`. |
| `tunnelcrypt.py`, `tunnelgate.py`, `web\js\sealed.js` | The optional end-to-end sealing: a per-connection ECDH P-256 key, proven with a code generated on the machine (PBKDF2-HMAC-SHA256), traffic in AES-256-GCM, numbered against replay. With it on, every API call crosses as `POST /api/sealed`. |
| `folders.py` | Which folders the pages may reach, from here or remotely. |
| `dnsguide.py` | Reads a domain's current DNS for the setup wizard's remote-access step. |
| `installer\Enable-RemoteControl.ps1`, `Start-Tunnel.ps1` | Create the tunnel, DNS record and Access application, and run `cloudflared`. |

See [Remote access](Remote-access.md) and [Cloudflare tunnel](Cloudflare-Tunnel.md).

### AI tool routing

Each AI tool reaches the proxy a different way, because each honours a different setting.

| Tool | How it is routed | Modules |
|---|---|---|
| Claude Code (terminal and VS Code) | `ANTHROPIC_BASE_URL` in Claude Code's user settings, set by `Enable-AiCleaner.ps1`. Claude Code's own sign-in is passed through. | `routing.py` reads where each tool points, so the Overview tells the truth |
| Claude Desktop (third-party gateway mode) | Policy settings give Desktop the proxy's address and a local gateway token, which the proxy swaps for the real API key stored with DPAPI. | `secrets.py` |
| Claude Desktop's Code tab | It ignores `ANTHROPIC_BASE_URL` but honours `HTTPS_PROXY`, so it is pointed at the forward proxy, which reads its calls with a certificate only Claude Code trusts. [Claude Desktop: Code tab](Settings.md#card-codetab) | `codetab.py`, `codetab_ca.py`, `codetab_settings.py` |
| Claude Desktop's chat (signed in to claude.ai) | The Windows proxy auto-config points `claude.ai` at the forward proxy, with no direct fallback; a separate PAC service and a watchdog task keep that safe when AI Cleaner is down. The chat's binary protobuf is read without a schema. [Claude Desktop: chat](Settings.md#card-claudeai) | `claudeai.py`, `claudeai_settings.py`, `claudeai_tasks.py`, `pacserve.py`, `protowire.py` |
| GitHub Copilot in VS Code | VS Code's `http.proxy` points at the forward proxy; Copilot's hosts are read with a certificate from a Copilot-only authority. [GitHub Copilot](Settings.md#card-copilot) | `copilot.py`, `copilot_settings.py` |
| Copilot CLI and Codex in Project Sessions | Started through the agent bridge with the forward proxy and their own certificate in their environment. | `agents\` |
| The file editor's suggestions | A warm Claude Code process (Haiku, no tools, no settings) fed prompt after prompt through the proxy; Copilot or Codex through the bridge as alternatives. [File editor](Settings.md#card-editor) | `completion.py`, `pscommands.py` |

Each local certificate authority is limited (a critical name constraint) to the hosts it reads, its
private key is DPAPI-protected, and the server certificate's key is made afresh at each start. The
Code tab and Codex authorities are given only to those processes, never the Windows store; the
Copilot and claude.ai authorities are added to your user's certificate store, which Windows asks you
to confirm. [Where your AI apps point](Settings.md#card-routing) shows the current routing of every
tool.

### Hooks

Hooks are small standard-library scripts in `aicleaner\hooks\` that Claude Code runs, installed into
Claude Code's user settings while their setting is on.

| Hook | When | What |
|---|---|---|
| `bash_guard.py` | Before a tool call | Stops a Bash call containing a doubled backslash, which Claude Code's Bash tool on Windows would silently change ([Claude Code: PowerShell](Settings.md#card-bash-guard)). |
| `check_collapse.py` | Before a tool call | Stops a call in which two different spellings would be written as the same real term (for example a "rename" that would overwrite the only copy). |
| `names_restored.py` | After a tool call | Tells the model that a file it wrote holds real terms it sees as tokens, so it does not "correct" a correct file name. |
| `deliver_queued.py` | After a tool call | Hands a message typed on the Session page to Claude between tool calls instead of at the end of the turn. |
| `clear_proxy.py` | Session start | With the Code tab routed, keeps the commands Claude Code runs connecting directly, as before. |

### Installer, service and updates

| Part | Role |
|---|---|
| `installer\Deploy-AiCleaner.ps1` | Install, Update, Rollback, Uninstall and GetSource. Self-contained, so it can be copied to a machine and run on its own. |
| `Install-AiCleaner-VSCode.ps1`, `Install-AiCleaner-Desktop.ps1`, `Install-AiCleaner-Workstation.ps1` | Wire each role: the Python environment, Claude Code settings, Claude Desktop policy, the screenshot-to-text set-up on a client PC. |
| `Enable-AiCleaner.ps1`, `Disable-AiCleaner.ps1` | Run readiness checks, then point the AI tools at the proxy; or point them back. |
| `Start-AiCleaner.ps1`, `Stop-AiCleaner.ps1` | Start the proxy from the venv (`python -m aicleaner run`), detached or in a console; stop it. |
| `Set-AiCleanerService.ps1`, `Run-AiCleanerService.ps1`, `Run-TunnelService.ps1`, `service.py`, `lowertoken.py` | Optionally start AI Cleaner with the computer, as you, at a standard-user token ([Start with the computer](Settings.md#card-service)). |
| `Check-AiCleanerHealth.ps1`, `Watch-AiCleaner.ps1` | The wiring checked on a timer; the claude.ai watchdog and PAC service. |
| `updates.py`, `restart.py` | The [Version and updates](Settings.md#card-updates) card: what is installed, what is available, and handing the update or a restart to a PowerShell that outlives the proxy. |
| `perms.py` | Warns when other users on the PC could write to the install, topics or logs folders. |
| `aicleaner.cmd`, `__main__.py` | The command line: `run`, `check`, `scan`, `scan-tokens`, `preflight`, `seed`, `detail`, `mcp-audit` and others. |
| `mcp_audit.py`, `mcp_watch.py` | Find every MCP server on the machine, class it as hosted, bridged, egress or local, and keep hosted ones' tool arguments tokenised. |

## One request, step by step

A Claude Code prompt such as "list Contoso's resource groups", with Contoso mapped to index 42:

```
  Claude Code                      AI Cleaner (127.0.0.1:8787)                        AI service
  -----------                      ---------------------------                        ----------
  POST /anthropic/v1/messages  -->  1  route: dialect "anthropic", AI service chosen
  "list Contoso's RGs"              2  stopped? broken topic file -> refuse
                                    3  URL and headers: a real term there -> refuse
                                    4  body must be JSON (else refuse, unless allow-listed)
                                    5  strip AI Cleaner's own marks and footers from history
                                    6  attachments -> text (or a note saying so)
                                    7  tokenise every string and key
                                         "list Client42's RGs"
                                    8  detect what is still identifier-shaped;
                                         auto-add what proves its owner, tokenise again
                                    9  secrets redacted (or refused)
                                   10  unknowns of chosen kinds -> held back, suggested in Review
                                   11  canary: any declared real term left? -> 400, not sent
                                   12  credentials: pass-through or gateway swap  ---------->  request
                                                                                   <--------  streamed reply
                                   13  rehydrate per content block, tail-buffered
                                         tokens and held-back tokens -> real terms
                                         thinking untouched; kept tools stay tokenised
                                   14  footer if Review has something new
  "az group list ... contoso"  <--  15  one evidence line: counts, canary, what was held
  runs here, with real terms
```

In words:

1. **Route.** The path's first segment names the dialect, which picks the AI service. A session
   started from the Session page uses `/session/<profile>/<dialect>/...`, which only labels the
   evidence; every topic's terms are replaced in every session. One snapshot of the mapping is
   taken per request, so a reload mid-request cannot mix two mappings.
2. **Stopped?** A topic file that is broken with no last-good copy stops every model request,
   because that topic's terms would otherwise travel untouched.
3. **URL and headers** are sent as written, so they are checked rather than rewritten: a real term
   there refuses the request. Cookies, encodings and forwarding headers are never passed on.
4. **JSON only.** A body that cannot be parsed cannot be inspected, and is refused unless its path
   is in `allow_non_json_paths` ([Unchecked endpoints](Settings.md#card-uninspected), empty by default).
5. **History clean-up.** Marks and footers AI Cleaner added to earlier replies are removed.
6. **Attachments** become text (or are withheld) off the event loop.
7. **Tokenise** every string value and key (`transform.tokenise_json`).
8. **Detect** in one sweep of the tokenised text. A value that proves its owner (an address at a
   topic's mapped domain, say) is added to that topic and the request is tokenised again from the
   original, so this request is already covered.
9. **Secrets** are redacted by family, or the request is blocked, per the Secrets page.
10. **Hold back.** Temporary tokens replace unknowns of the kinds set to "Tokenise & Review"; the
    reply side uses a combined mapping so they come back as the real values.
11. **The canary** reads every decoded string and key of the final body, including disguised
    spellings. Any declared real term refuses the request with 400; nothing is sent.
12. **Forward.** Claude Code's own sign-in is forwarded untouched; Claude Desktop's local gateway
    token is swapped for the stored API key. An unreachable AI service is a 502, still with an
    evidence line.
13. **Reply.** `text/event-stream` goes through the dialect's rewriter: each content block holds back
    its last few characters until the next fragment or the end of the block, so a token split across
    two events is still replaced whole. JSON replies are rehydrated string by string. Thinking blocks
    pass through untouched (they are signed by the service). Tool-call arguments are rehydrated, so
    commands and file writes run here with real terms - except tools in `keep_tokenised_tools`
    (hosted MCP servers), which stay tokenised ([Names kept in tool calls](Settings.md#card-tools)).
14. **Footer.** A short review note is added when something new is waiting in [Review](Review.md).
15. **Evidence.** One line per request, counts only.

The forward-proxy routes (Code tab, Copilot, claude.ai, Codex) end in the same filtering: their
readers hand each request to the proxy's own pipeline with the right dialect. More detail on tokens,
held-back tokens and the canary is in [How it works](How-it-works.md).

## Data and storage

AI Cleaner keeps data in three places: the topics location (shared), the install folder (per
user, holds the code and your own settings), and the state folder (per user, per machine, never
shared).

| What | Where | Shared? | Encrypted? |
|---|---|---|---|
| Topic files | `<clients location>\<id>\<id>.json`, plus `_registry.json` (index allocations) | With that engagement's people, through OneDrive | No; stamped, and signed with the topic key when one is set |
| Configuration | `aicleaner-config.json` in the install folder | No | No (holds no secrets) |
| Global, ignore list, project sessions, project assets folders | `_global.json`, `ignore-list.json`, `profiles.json`, `client-shares.json` beside the config | No | No |
| Review suggestions | `candidates-<host>.jsonl` beside the config | No | No |
| Contact details | `contact-*.json` beside the config | Shipped with the install | No |
| Logs | `logs\` beside the config: proxy log, `evidence-<host>-<date>.jsonl`, `detail\substitutions-...jsonl.gz`, `lifecycle-...`, stamped run logs | No | No - the substitution log and proxy log hold the real terms on purpose |
| Last-good copies, case variants | `%USERPROFILE%\.aicleaner\lastgood\`, `case-variants.json` | No | No |
| Sessions | `%USERPROFILE%\.aicleaner\sessions\<session>\` (`launch.json`, `broker.json`, `events.jsonl`) | No | No; the broker's token guards its loopback port |
| AI service API key, gateway token | `%USERPROFILE%\.aicleaner\secrets\` | No | **DPAPI** (current user) |
| Your own redaction list | `%USERPROFILE%\.aicleaner\secrets\redact-list.json.dpapi` | No | **DPAPI** |
| Topic keys | Under `%USERPROFILE%\.aicleaner\` | Sent out of band by people, never in the shared folder | **DPAPI** |
| Tunnel code, Cloudflare token | `tunnel-code.dpapi`, `cloudflare-*.dpapi` in `%USERPROFILE%\.aicleaner\` | No | **DPAPI** |
| Local certificate authorities | `*-ca.pem` (public) and `*-ca.key.dpapi` (private) | No | Private keys **DPAPI** |
| Deployment record, rollback copies | `deployment.json`, `last-update.json`, `rollback\` | No | No |
| Conversation titles, search index, recycle bin, alerts | `conversation-labels.json`, `search\`, `conversation-bin\`, `alerts.json` | No | No |
| Claude Code's transcripts | `%USERPROFILE%\.claude\projects\` (Claude Code's own) | No | No - they are in the real terms |

DPAPI ties a secret to your Windows account on this PC: copying the file elsewhere yields something
that cannot be decrypted. On other platforms the secrets modules fall back to owner-only plain files.

The in-memory mapping is rebuilt only when a topic file's signature changes, not on every request.
Each topic file is validated on its own; a bad one falls back to its last-good copy, so one bad
edit degrades one topic, not the proxy (see [How it works](How-it-works.md#last-good-copies-and-fail-closed-states)).

## Security model

The full account is on [Security model](Security-model.md). Architecturally it rests on these:

- **Loopback only.** The proxy, the forward proxy, the brokers and the PAC service listen on
  `127.0.0.1`. The forward proxy also refuses a caller that is not on this machine and anything but
  `CONNECT`. With `listen` set to anything but a loopback address, the UI and API switch off.
- **The request guard** (`guard.py`) in front of the UI and API: the `Host` must be a loopback
  name (defeats DNS rebinding); a write from another origin, or any API request a browser marks
  cross-site, is refused; API writes must be `application/json`; a strict content security policy
  (scripts and styles from the app only, no framing) and no caching.
- **Remote requests get less.** A request through the tunnel needs a valid, locally verified
  Access token for an allowed address, and is then marked remote. It can never reach the model
  routes. `/api/secrets` (except listing labels and adding one), `/api/tokenise`, `/api/rehydrate`,
  the setup wizard's actions and raising alerts are refused, and settings that weaken the filter or
  act on the machine are local-only - shown greyed out remotely rather than hidden. With encryption
  on, only the sealed endpoint and the key exchange are reachable.
- **Fail closed.** Not JSON, a real term in the URL, a broken topic file, a canary hit, an unread
  attachment: each is refused or withheld rather than sent. For claude.ai, the proxy auto-config has
  no direct fallback, so the conversation fails rather than going out unfiltered.
- **Narrow certificate authorities**, each name-constrained to the hosts it reads (above).
- **Signed releases** (below), and dependencies installed from hashed wheels only.
- **Least privilege.** The proxy runs as you, at a standard-user token, even when started by a
  scheduled task before sign-in.

What it does **not** protect: tools that are not pointed at it, identifiers nobody has mapped and
no detector recognises, *what* you are working on as opposed to *who* for, and anything sent before
AI Cleaner was in the path. The [Security model](Security-model.md#what-is-not-protected) page lists
each gap and the switches that weaken the filter.

## Deployment and updates

### Install layout

| Folder | Holds |
|---|---|
| `%USERPROFILE%\.aicleaner\app` | The code (`aicleaner\`, `installer\`, `docs\`, `release\`, templates), the configuration and your own data files beside it. Outside any synced folder. |
| `%USERPROFILE%\.aicleaner\venv` | The Python environment, always here whatever the install path. |
| `%USERPROFILE%\.aicleaner\` | State: secrets, last-good copies, sessions, rollback copies, the deployment record. |
| `<clients location>` | Topic files, chosen at install; OneDrive is suggested and pinned to the device. |

A sign-in task (`aicleaner-proxy`) starts the proxy. The optional start-with-the-computer task
(`aicleaner-service`) starts it at boot instead, with the sign-in task kept as the fallback. [Getting started](Getting-started.md#1-install) walks through an install step by step.

### Releases

| Piece | What it is |
|---|---|
| Release tag | `release-X.Y.Z` from 1.0.0 (`vX.Y.Z` before). Install and Update take the highest tag. |
| `release\RELEASE.json` | The signpost: `version`; `minimum`, the lowest installed version that may update straight to this one (an older install steps through an intermediate release); `deploy`, the folder holding the scripts; `layout`, which repository paths go where in the install; `migrations`, one-off steps. |
| `release\RELEASE-MANIFEST` | The SHA-256 of every file in the release, `RELEASE.json` included. |
| `release\RELEASE-MANIFEST.sig` | An OpenSSH signature over the manifest. |
| `release\release-signing.pub` | The public key the release names. |

When `Deploy-AiCleaner.ps1` installs or updates:

1. It first checks the release for a newer copy of itself and runs that instead.
2. It lists the release tags with `git ls-remote` and picks the next one to install.
3. It fetches that tag and verifies it before copying anything: the manifest's signature with
   `ssh-keygen -Y verify` against the **pinned** key, then every file's hash against the manifest,
   and no file outside it. The key is pinned on first install (trust on first use); a release
   signed with a different key afterwards is refused. Unsigned code needs `-AllowUnsigned`.
4. It keeps the build it is about to replace in `%USERPROFILE%\.aicleaner\rollback\`, stops the
   proxy, copies the new files (removing files the release dropped), runs any migrations, and
   starts the proxy again. Data files are never touched.
5. It records what was installed in `deployment.json`.

**Rollback** (`-Action Rollback`) puts the previous build back from the rollback folder without
needing the proxy to run - the point, since a broken proxy takes the AI tooling with it. Data is
never rolled back.

**Updates from the page.** The [Version and updates](Settings.md#card-updates) card asks the same
`git ls-remote` question at most hourly and runs the installed `Deploy-AiCleaner.ps1 -Action Update`
in a detached PowerShell. Installing updates on their own (`update.auto`) is off by default.

## Extension points

These are where new work plugs in. They describe the code's shape today, not a promised API.

| To add | Where it plugs in |
|---|---|
| **A new API family (dialect)** | A streaming rewriter in `dialects\<name>.py` that knows which event fields carry text fragments and when a fragment sequence ends; register it in `get_stream_rewriter` (`dialects\__init__.py`); give it an AI service address in `DEFAULT_UPSTREAMS` (`config.py`) or the config's `upstreams`. Non-streaming JSON needs no dialect - every string is rewritten. |
| **A new AI service reached through the forward proxy** | A certificate authority kind in `codetab_ca.py` (its own name constraint), a reader registered on the forward proxy in `codetab.py` (as `set_copilot`, `set_claudeai` and `set_openai` do), and a handler that sends the paths carrying the person's text through `Proxy.handle` with the right dialect, as `copilot.py` and `claudeai.py` do. A switch module writes the tool's own settings and puts them back (`copilot_settings.py`, `claudeai_settings.py`). |
| **A new agent in Project Sessions** | An entry in `agents\__init__.py` (id, label, command, package, certificate kind) and its protocol in `agents\bridge.py`, which translates to Claude Code's stream-json. Everything else - broker, queue, permission cards, titles - works unchanged. |
| **A new editor suggestion service** | `completion.py`: add it to `SERVICES` (and `AUTO_ORDER` if `auto` may choose it) and give the worker a command; bridged services reuse `agents\bridge.py`. |
| **A new identifier kind** | A detector in `detect.py` and, for a stable token, a shape in `mapping.py`'s token derivation. |
| **A new hook** | A standard-library script in `hooks\`, installed into Claude Code's settings by a module like `deliver.py` or `bashguard.py`, which adds and removes only its own entry. |
| **A new page** | A module in `web\js\pages\` exporting `render(main, ctx)`, added to `PAGES` in `app.js`; its API routes in `ui.py`, behind the guard, with a decision about what is allowed remotely. |

---

[Home](Home.md) > Architecture
