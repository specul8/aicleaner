# Remote access

[Home](Home.md) > Remote access

AI Cleaner's pages - including [Project Sessions](Project-Sessions.md) - can be used from another
device, such as a phone or a laptop away from your desk. The device is only a screen: Claude Code
still runs on the AI Cleaner machine, and every request it makes is still filtered there.

Claude Code's own Remote Control relays a session through Anthropic's servers, which is what
AI Cleaner exists to avoid (and it does not work behind a gateway). This is the replacement.

## How it fits together

```
 phone / laptop  --HTTPS-->  Cloudflare Access (sign-in)  -->  Cloudflare tunnel
                                                                    |
                                          (dials out; nothing opened on the machine)
                                                                    v
                                              AI Cleaner on 127.0.0.1:8787 (checks the token)
```

- **The tunnel** (`cloudflared`, run as a service) dials out from the AI Cleaner machine to
  Cloudflare. No port is opened, and the proxy keeps listening on loopback only.
- **Cloudflare Access** sits in front of the hostname and does the sign-in.
- **AI Cleaner verifies** the token Access adds to every request, rather than trusting it.

## Setting it up

The easy way is the setup wizard's **Remote access** step ([Set up](Settings.md#card-setup) in Settings): it checks your
Cloudflare account, looks at your domain, asks how you want to sign in, and runs the script below
for you. Deciding whether to? [Cloudflare tunnel](Cloudflare-Tunnel.md) is the short version.


```powershell
.\installer\Enable-RemoteControl.ps1 -Hostname claude.example.com
```

The script installs `cloudflared`, creates a named tunnel (default `aicleaner-<computer name>`),
points the hostname at the proxy's port and runs the tunnel as a service. It asks:

- whether you already have a Cloudflare account with Zero Trust turned on - if not, it prints the
  one-time account steps and waits while you do them;
- how to do the Cloudflare side:
  - `api` (the default - press Enter) - paste a scoped API token and it creates the tunnel, DNS record, Access application and
    allow policy, and writes the `remote` block into `aicleaner-config.json`. The token is never
    written to disk or logged; delete it when the run finishes.
  - `manual` - it prints the dashboard click path and you fill in the `remote` block yourself.

| Parameter | Use |
|---|---|
| `-Hostname` | The public name for the UI |
| `-Email` | The address allowed to sign in (and where a one-time PIN goes) |
| `-Identity OneTimePin \| Cloudflare \| Google \| Microsoft \| GitHub` | The sign-in provider (default one-time PIN here; the setup wizard defaults to Cloudflare, your Cloudflare account) |
| `-Setup api \| manual`, `-ApiToken` | Answer the questions up front |
| `-Rename -Hostname <new>` | Give this machine's address a new name, keeping the tunnel and Access application |
| `-Remove` | Stop and remove the service, delete the tunnel, turn remote access off |

One hostname and one tunnel per machine: two AI Cleaner machines need two names.

After a manual setup, the `remote` block looks like this; restart the proxy after editing it:

```json
"remote": {
  "enabled": true,
  "hostname": "claude.example.com",
  "team_domain": "yourteam.cloudflareaccess.com",
  "audience": "<the Application Audience tag>",
  "allowed_emails": ["you@example.com"]
}
```

## What AI Cleaner checks

Who gets in is decided twice. **Cloudflare first**: the Access application's policy includes only the
addresses given to `Enable-RemoteControl.ps1` (`-Emails`; yours by default), so someone who can sign
in to the identity provider - any Google account, say - but is not on that policy is turned away by
Cloudflare and never reaches this machine. **Then aicleaner**, which does not take Cloudflare's
word for it:

Anyone who can reach the hostname can send any headers, so every request arriving at it must carry
a token Cloudflare Access signed (`Cf-Access-Jwt-Assertion`). AI Cleaner checks the signature against
your team's published keys, plus the issuer, the audience, the expiry and the email address. An
empty `allowed_emails` list lets nobody in. Each check that fails is refused with a 403 saying why
("the Access token has expired; sign in again", "... is not on the list for this deployment").

The verified address and country are recorded as "who" in the substitution log for changes made
remotely.

## Which device you are on

A web page cannot read a computer's name, so the first page you open through remote access asks
once: **Which device is this?** Pick one of your Remote desktops entries or type the device's name
("Don't ask me again" stops the question). The answer is kept in that browser only. Each prompt you
send then carries the note "The user is accessing the AI Cleaner app remotely from <device name>",
so rules in your CLAUDE.md can depend on the device - for example, "from the tablet, do not suggest
running commands on the desktop". The name is what the browser was told, not proof of the device.
That device's own entry in the Remote desktops menu is greyed out.

## What is refused remotely

| Through the tunnel | Why |
|---|---|
| Model routes (`/anthropic`, `/openai`) | The tunnel is for driving a session, not for proxying other traffic through your mapping |
| Secrets page and `/api/secrets` | An API key is not something to read from a train. The menu entry stays, greyed out |
| `/api/tokenise`, `/api/rehydrate` | Turning tokens into names on demand has no use from a browser |
| Most settings | Only a short list can be changed remotely (see below); the rest say "local only" |
| Topic keys (generate, enter, rotate) | A key is what makes a change to a shared file trusted |
| Approving a remote MCP server for the real terms | Who receives the real terms is decided on the machine |
| Adding a shared topic, renumbering a topic | They change what this machine loads |
| Generating the tunnel code, and where a device keeps its key | Set on the machine itself (the code is shown once, so it is never sent through the tunnel) |

Settings that **can** be changed remotely include the session switch and permission mode (every mode,
including `bypassPermissions`), review footers and notifications, detail logging, detection choices
(held-back kinds, misreadings, auto-mapping), allowed folder locations, and the canary's emergency
`fail_open` switch. Everything else - Review, Topics, export, driving sessions - works as it
does at the desk.

These refusals are made by the server, not just hidden by the page.

## The encrypted tunnel

Cloudflare terminates TLS at its edge, so without more, Cloudflare (and any HTTPS-inspecting box on
the way) could read the rehydrated session. The encrypted tunnel closes that gap.

Through the tunnel, a badge under the AI Cleaner title always says which state you are in: a green
closed lock, **End-to-end Encrypted**, or an amber open lock, **Traffic can be Observed** (Cloudflare
ends the encrypted connection at its edge - SSL termination - and a proxy that decrypts HTTPS to
inspect it can read it too). Click it to open Settings at the [Encrypted tunnel](Settings.md#card-tunnel) card.

Set it up in Settings, [Encrypted tunnel](Settings.md#card-tunnel), on the AI Cleaner machine (afterwards the
on/off switch also works through the tunnel - switching it off from there only goes through the
encrypted connection, so only someone holding the code can):

1. **Generate a code.** A 24-character code (no easily confused characters such as 0/O or 1/I/L) is
   shown once. Keep it somewhere you can reach from the other device.
2. Tick **Encrypt everything that crosses the tunnel**.
3. Choose where a device keeps the derived key (never the code): **for the browser tab only**
   (typed again after closing it) or **on the device** (until you press **Forget the key on this device** on that device, or a new code is generated;
   locking the device or putting it to sleep does not touch it).

Through the tunnel, the page then asks for the code. From it both ends derive a key that only
proves each side to the other; every connection agrees a new key of its own (ECDH P-256, then
AES-256-GCM), and every API call goes as one sealed, numbered request that cannot be replayed. A
recording stays sealed even if the code leaks later.

In practice: the code is typed once per device; what stays the same is the key derived from it. Each
**connection** - opening the page, or the page reconnecting after AI Cleaner restarts or the
connection sits idle - runs a fresh key exchange, and the key it produces works only for that
connection and lives only in that page's memory. Signing in to Cloudflare Access is a separate,
earlier step: it decides who may reach the page at all; the encryption then decides what Cloudflare
can read once you are there - nothing.

What it does not hide: that you are using it, when, and how much - and someone able to change the
page itself could take the code as you type it.

While it is on:

- the Session page polls instead of streaming;
- images, PDFs and file downloads open on the AI Cleaner machine only;
- generating a new code drops every connection, and every device must enter the new one.

On the AI Cleaner machine itself nothing changes.

## Remote desktops

Settings > [Remote desktops](Settings.md#card-remote-desktop) lists the machines whose Windows desktop you can open from AI
Cleaner's menu, one per line as `Name = https://address`. Each becomes a menu entry that opens that machine's page in
a new tab: its own address, behind the same Cloudflare sign-in, sealed end to end like the encrypted tunnel. Nothing is
embedded in AI Cleaner's page.

The desktop page itself is **aicleaner-rdp**, a separate bolt-on installed on each machine whose desktop you want to
reach; its installer adds its own machine to this list when AI Cleaner runs beside it. An address must be `https://`
and may not hold a user name or password; at most 20. The list is changed on the AI Cleaner machine - through remote
access it is shown, not changed.

## If it is not acceptable for an engagement

Turn `remote.enabled` off (or run `Enable-RemoteControl.ps1 -Remove`) and drive sessions from the
machine itself. See [Security model](Security-model.md) for the wider picture.

---

[Home](Home.md) > Remote access
