# Security model

[Home](Home.md) > Security model

aicleaner has one promise: **real client identifiers stay on this machine; the AI service sees
tokens.** This page says what that covers, what it does not, and what protects aicleaner itself.
The mechanics are in [How it works](How-it-works.md).

## What is protected

| Traffic | Protection |
|---|---|
| Prompts, file contents, command output and history sent by a tool pointed at the proxy | Every known identifier tokenised; the canary refuses a request if one survives |
| Unknown identifiers of the kinds set to "Tokenise & Review" | Replaced by a placeholder until you decide in [Review](Review.md) |
| GUIDs (tenant and subscription ids) | Always replaced by a stand-in |
| Attachments (PDF, Office, images) | Converted to text and filtered, or withheld with a notice |
| Credentials in the text | Redacted by default (Secrets page) |
| Arguments to kept tools (remote MCP servers) | Stay tokenised |
| A body that cannot be inspected (not JSON) | Refused |
| A client's name in the request URL or a header | Refused |

## What is not protected

| Gap | Why |
|---|---|
| Tools not pointed at the proxy | aicleaner only sees traffic sent to it. claude.ai in a browser, the mobile apps, Claude Code on the web, Copilot, ChatGPT and the same Claude account on another device all go direct. Use them for client work only under your own policy. |
| Identifiers the mapping does not know and no detector recognises | A client's name written as plain words is queued for Review but **travels as written while it waits**. The canary cannot help: it only knows declared values. |
| Kinds set to "Ignore" | Not queued and not held back, by your choice. (A kind set to "Auto-tokenise" is still replaced.) |
| A placeholder that could not be made safely | That one value goes out as it is; this is logged in the proxy log. |
| What, rather than who | Architecture, technology choices, topology and anything not mapped still reach the model. aicleaner hides **who**, not **what**. |
| Images when [Pictures](Settings.md#card-images) allows them | Sent as images, unfiltered. Off by default. |
| A path listed in `allow_non_json_paths` | Forwarded uninspected; every such request is logged `UNFILTERED`. Empty by default. |
| Telemetry the tools send themselves | Outside the model path. The installer reviews OpenTelemetry settings and turns off what it can. |
| What was sent before aicleaner was in the path | Cannot be undone. |

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

## Tools that reach out

Tool calls are rehydrated before they run, which is right for files and local commands. Two cases
are handled differently:

- **Kept tools.** Hosted MCP servers receive what they are given straight over the internet, so
  their arguments stay tokenised. The default list covers remote Microsoft Learn and Context7;
  `aicleaner mcp-audit` lists the MCP servers configured on the machine, classes each as hosted,
  bridged, egress or local, and `--write` adds the missing patterns.
- **Tool calls that reach the network** (`WebFetch`, `curl`, `Invoke-WebRequest`) run on this
  machine with the real names, like the same command typed by hand. Their traffic does not pass
  through aicleaner; Claude Code's own permission prompts are the control there.

## Local data holds real values, by design

Everything below stays on this machine and is never sent anywhere by aicleaner. Real values are kept
locally on purpose: they are what lets you audit a request afterwards and diagnose a missed match.

| What | Where | Holds |
|---|---|---|
| Evidence log | `logs\evidence-<host>-<date>.jsonl` | Counts per client, canary result, what was held back. Never content. |
| Substitution log | `logs\detail\substitutions-<host>-<date>.jsonl.gz` | Each real value and the token that replaced it, per request. Kept 90 days by default; can be switched off in [Logging](Settings.md#card-logging). |
| Proxy log | `logs\` | Diagnostics, including real values |
| Claude Code transcripts | `%USERPROFILE%\.claude\projects` | What Claude Code saw, in real names |
| Last-good copies, case variants, keys, secrets | `%USERPROFILE%\.aicleaner\` | Per user, never in the shared clients folder |
| Claude Desktop history backup | `%USERPROFILE%\.aicleaner\backups\claude-3p` by default | Never OneDrive: the history holds real names |

Treat these like the client files themselves. They are not for sharing.

## Client files: stamps and keys

Client files often live in a shared OneDrive folder, which anyone with write access - or any tool,
or the OneDrive web editor - can change.

- **Stamp** (`aicleaner/stamp.py`). Every file aicleaner writes carries a `_stamp`: a SHA-256 of
  the content, the machine and the time. A file whose stamp no longer matches is flagged "edited
  outside aicleaner". A checksum can be recomputed by anyone, so on its own it only makes edits
  visible.
- **Client key** (`aicleaner/keys.py`). The first person on an engagement generates a key on the
  client's page and sends it to the others out of band; each pastes it in. With a key, the stamp is
  also signed (HMAC-SHA256). A change not signed with the key is **held**: the last-good copy stays
  in use and the client page shows the difference to accept or reject.
- The key is stored DPAPI-encrypted under your Windows profile, readable only by you on this PC,
  and **never** in the shared folder - anyone who can write the folder could read a key kept there.
- Limit: a key cannot protect against malware running as a colleague on their own PC, which can
  use their aicleaner and their key.

See [Sharing a client with a team](Sharing.md).

## The UI and local API

`aicleaner/guard.py` protects the UI against web pages in your own browser:

- answers only when the `Host` is `127.0.0.1`, `localhost` or `[::1]` (defeats DNS rebinding);
- refuses a write from another origin, and any API request a browser marks cross-site;
- API writes must be `application/json` (except `/api/tokenise` and `/api/rehydrate`);
- a strict content security policy (no inline script, no framing), and nothing is cached;
- with `listen` set to anything but a loopback address, the UI and API are switched off.

## Remote access

Off by default (`remote.enabled`). When set up (`Enable-RemoteControl.ps1`), a Cloudflare tunnel
publishes the UI under a hostname, with Cloudflare Access doing the sign-in. See
[Remote access](Remote-access.md).

- **Who gets in is decided twice.** First by Cloudflare: the Access application's policy only
  includes the addresses you set up (`Enable-RemoteControl.ps1 -Emails`, or yours by default), so a
  stranger who can sign in to Google - or whichever identity provider you chose - is turned away by
  Cloudflare before anything reaches this machine. Then by aicleaner, which does not trust that:
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
