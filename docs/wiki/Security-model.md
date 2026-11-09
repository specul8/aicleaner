# Security model

[Home](Home.md) > Security model

AI Cleaner has one promise: **the real terms of your topics stay on this machine; the AI service
sees tokens.** This page says what that covers, what it does not, and what protects AI Cleaner itself.
The mechanics are in [How it works](How-it-works.md).

## What is protected

| Traffic | Protection |
|---|---|
| Prompts, file contents, command output and history sent by a tool pointed at the proxy | Every known identifier tokenised; the canary refuses a request if one survives |
| Unknown identifiers of the kinds set to "Tokenise & Review" | Held back behind a temporary token until you decide in [Review](Review.md) |
| GUIDs (tenant and subscription ids) | Always replaced by a token |
| Attachments (PDF, Office, images) | Converted to text and filtered, or withheld with a note saying so |
| Secrets in the text | Redacted by default (Secrets page) |
| Arguments to kept tools (remote MCP servers) | Stay tokenised |
| A body that cannot be inspected (not JSON) | Refused |
| A topic's real term in the request URL or a header | Refused |

## Which AI tools go through AI Cleaner

| Tool | Filtered? | How it is known |
|---|---|---|
| Claude Code in a terminal or VS Code | Yes | AI Cleaner points it at the proxy in your Claude Code user settings |
| Claude Code in JetBrains IDEs (IntelliJ, PyCharm, Rider ...) | Yes, expected | The JetBrains plugin runs the same Claude Code, which reads the same user settings; not yet checked on a JetBrains IDE |
| Claude Code, Copilot and Codex in [Project Sessions](Project-Sessions.md) | Yes | AI Cleaner starts them with the proxy set |
| Codex's VS Code extension | No | Its own copy of Codex, not pointed at AI Cleaner |
| The AI services' phone apps | No | See below; use Project Sessions on the phone instead |

## What is not protected

| Gap | Why |
|---|---|
| Tools not pointed at the proxy | AI Cleaner only sees traffic sent to it. Claude Code on the web, Copilot and ChatGPT outside AI Cleaner, and the same Claude account on another device all go direct. Use them for client work only under your own policy. |
| The AI services' phone apps | AI Cleaner does not protect conversations in the AI services' phone apps (Claude, ChatGPT, Copilot). It does protect them when you use [Project Sessions](Project-Sessions.md) on a phone instead of the app, through [Remote access](Remote-access.md). |
| Codex's VS Code extension | Not filtered: the extension runs its own copy of Codex, which talks to OpenAI directly. Codex **is** filtered when AI Cleaner runs it, in Project Sessions. |
| Identifiers the mapping does not know and no detector recognises | A topic's name written as plain words is suggested in Review but **travels as written while it waits**. The canary cannot help: it only knows declared values. |
| Kinds set to "Ignore" | Not queued and not held back, by your choice. (A kind set to "Auto-tokenise" is still replaced.) |
| A held-back token that could not be made safely | That one value goes out as it is; this is logged in the proxy log. |
| What, rather than who | Architecture, technology choices, topology and anything not mapped still reach the model. AI Cleaner hides **who**, not **what**. |
| Images when [Pictures](Settings.md#card-images) allows them | Sent as images, unfiltered. Off by default. |
| A path listed in `allow_non_json_paths` | Forwarded uninspected; every such request is logged `UNFILTERED`. Empty by default. |
| Telemetry the tools send themselves | Outside the model path. The installer reviews OpenTelemetry settings and turns off what it can. |
| What was sent before AI Cleaner was in the path | Cannot be undone. |

Claude Code refuses its own Remote Control feature when `ANTHROPIC_BASE_URL` points anywhere but
the AI service, so routing through the proxy also stops a session being relayed around the filter.

### Switches that weaken the filter

Each is off by default, and each is logged on every request it affects.

| Setting | Effect | Reachable remotely |
|---|---|---|
| `proxy.bypass` | Nothing is replaced, detected or checked | No |
| `detect.enabled` | Detection and Review off | No |
| `canary.fail_open` | A request the canary would refuse is sent anyway, and marked in the evidence | Yes - so a false positive can be cleared from away from the desk |
| `keep_tokenised_tools` | Decides which tools keep tokens | No |
| `attachments.allow_images` | Images are sent as images | No |

Since 1.1, turning `proxy.bypass`, `canary.fail_open` or `attachments.allow_images` on, setting `secrets.mode` to
`off`, or taking an AI tool off filtering (Claude Desktop's chat, its Code tab, Copilot, Claude Code) waits for a
**Yes on this machine's desktop**: AI Cleaner shows a Windows box, No is the default, and no answer in two minutes is
a No. A script or a program on the machine cannot answer it. A change made through remote access is already gated by
the remote sign-in, so it is not asked again on the desktop. Turning a protection back on is never asked.

## Tools that reach out

Tool calls are rehydrated before they run, which is right for files and local commands. Two cases
are handled differently:

- **Kept tools.** Hosted MCP servers receive what they are given straight over the internet, so
  their arguments stay tokenised. The default list covers remote Microsoft Learn and Context7;
  `AI Cleaner mcp-audit` lists the MCP servers configured on the machine, classes each as hosted,
  bridged, egress or local, and `--write` adds the missing patterns.
- **Tool calls that reach the network** (`WebFetch`, `curl`, `Invoke-WebRequest`) run on this
  machine with the real terms, like the same command typed by hand. Their traffic does not pass
  through AI Cleaner; Claude Code's own permission prompts are the control there.

## Local data holds the real terms, by design

Everything below stays on this machine and is never sent anywhere by AI Cleaner. The real terms are kept
locally on purpose: they are what lets you audit a request afterwards and diagnose a missed match.

| What | Where | Holds |
|---|---|---|
| Evidence log | `logs\evidence-<host>-<date>.jsonl` | Counts per topic, canary result, what was held back. Never content. |
| Substitution log | `logs\detail\substitutions-<host>-<date>.jsonl.gz` | Each real term and the token that replaced it, per request. Kept 90 days by default; can be switched off in [Logging](Settings.md#card-logging). |
| Proxy log | `logs\` | Diagnostics, including the real terms |
| Claude Code transcripts | `%USERPROFILE%\.claude\projects` | What Claude Code saw, in the real terms |
| Last-good copies, case variants, keys, secrets | `%USERPROFILE%\.aicleaner\` | Per user, never in the shared topics folder |
| Claude Desktop history backup | `%USERPROFILE%\.aicleaner\backups\claude-3p` by default | Never OneDrive: the history holds the real terms |

Treat these like the topic files themselves. They are not for sharing.

## Topic files: stamps and keys

Topic files often live in a shared OneDrive folder, which anyone with write access - or any tool,
or the OneDrive web editor - can change.

- **Stamp** (`aicleaner/stamp.py`). Every file AI Cleaner writes carries a `_stamp`: a SHA-256 of
  the content, the machine and the time. A file whose stamp no longer matches is flagged "edited
  outside AI Cleaner". A checksum can be recomputed by anyone, so on its own it only makes edits
  visible.
- **Topic key** (`aicleaner/keys.py`). The first person on an engagement generates a key on the
  topic's page and sends it to the others out of band; each pastes it in. With a key, the stamp is
  also signed (HMAC-SHA256). A change not signed with the key is **held**: the last-good copy stays
  in use and the topic page shows the difference to accept or reject.
- The key is stored DPAPI-encrypted under your Windows profile, readable only by you on this PC,
  and **never** in the shared folder - anyone who can write the folder could read a key kept there.
- Limit: a key cannot protect against malware running as a colleague on their own PC, which can
  use their AI Cleaner and their key.

See [Sharing a topic with a team](Sharing.md).

## The UI and local API

`aicleaner/guard.py` protects the UI against web pages in your own browser:

- answers only when the `Host` is `127.0.0.1`, `localhost` or `[::1]` (defeats DNS rebinding);
- refuses a write from another origin, and any API request a browser marks cross-site;
- API writes must be `application/json` (except `/api/tokenise` and `/api/rehydrate`);
- a strict content security policy (no inline script, no framing), and nothing is cached;
- with `listen` set to anything but a loopback address, the UI and API are switched off.

Since 1.1 it also protects the API against other programs on this machine. Every change through `/api/` made from this
machine must carry this install's key (header `X-AI-Cleaner-Key`), so a one-line `curl` changes nothing. The key is
made at the first start and kept DPAPI-protected in `%USERPROFILE%\.aicleaner\secrets\api-key.dpapi`. AI Cleaner's own
page is given it when opened on this machine (never through remote access), and AI Cleaner's scripts read it from the
file. Claude Code's hooks make three calls that change nothing (is this tool call to be stopped, were names put back,
is a message waiting), and these need no key.

The limit, said plainly: a program running as the same Windows user can read the file or load the page, so the key
stops a blind call, not a determined one. The control that holds against such a program is the desktop confirmation
for the switches above.

### Claude does not change AI Cleaner's settings

Since 1.1 AI Cleaner also installs a guard into Claude Code (a PreToolUse hook, `hooks/settings_guard.py`, always on,
kept in step at every start). It stops a Claude Code tool call that would change AI Cleaner's settings other than
through you: writing the installed `aicleaner-config.json` or anything under `.aicleaner\secrets`, a shell command
that reads the API key, sends it, or writes the config file, and taking the guard itself out of Claude Code's
settings. Reading the config, and any other work that merely names these files, goes on as before. If a setting
needs changing, Claude says so and leaves it to you. It is aimed at a helpful model "fixing" a setting on its own
initiative; a script Claude writes and then runs is not inspected, and the desktop confirmation still holds for every
switch that turns a protection off.

## Remote access

Off by default (`remote.enabled`). When set up (`Enable-RemoteControl.ps1`), a Cloudflare tunnel
publishes the UI under a hostname, with Cloudflare Access doing the sign-in. See
[Remote access](Remote-access.md).

- **Who gets in is decided twice.** First by Cloudflare: the Access application's policy only
  includes the addresses you set up (`Enable-RemoteControl.ps1 -Emails`, or yours by default), so a
  stranger who can sign in to Google - or whichever identity provider you chose - is turned away by
  Cloudflare before anything reaches this machine. Then by AI Cleaner, which does not trust that:
- **Access is verified, not trusted** (`aicleaner/access.py`). Anyone can send headers, so the
  Access token on each request is checked locally: RS256 signature against the team's published
  keys, issuer, audience, expiry, and the email address against `remote.allowed_emails`. That list
  is required, not optional: an address not on it is refused even if the Cloudflare policy were
  loosened, and an empty list lets nobody in.
- **The UI only.** Model routes are refused through the tunnel: it is for driving a session, never
  for proxying someone else's traffic through your mapping.
- **Less from outside.** `/api/secrets`, `/api/tokenise` and `/api/rehydrate` are refused through
  the tunnel, as are the settings that weaken the filter (table above).
- **Encrypted tunnel** (`aicleaner/tunnelcrypt.py`, `remote.encrypt`, off by default). Cloudflare
  terminates TLS at its edge, so without this Cloudflare - and any inspection box on the way - can
  read the page's traffic. With it, the page and the proxy run an ECDH P-256 exchange per
  connection, each proving it holds a 24-character code generated on the machine (PBKDF2-HMAC-SHA256);
  traffic is AES-256-GCM, numbered against replay. Paths and queries are sealed too. Recorded
  traffic stays sealed even if the code leaks later.

## Releases

`Deploy-AiCleaner.ps1` installs signed releases only: the release manifest is checked with OpenSSH
against the key the install trusts, and anything unsigned or altered is refused unless you pass
`-AllowUnsigned`.

---

[Home](Home.md) > Security model
