# Getting started

[Home](Home.md) > Getting started

This page takes you from nothing to a filtered session. One run of the deploy script installs
aicleaner, waits while you tell the filter about a client, then switches filtering on;
then you open a first Project Session. Allow an hour for the first client - most of
it is deciding what names that client.

## Before you start

aicleaner filters what an AI agent sends, so **the agent must already be installed**:

| Agent | Install it with |
|---|---|
| Claude Code (terminal or VS Code) | `winget install Anthropic.ClaudeCode` |
| Claude Desktop | `winget install Anthropic.Claude` |

Everything else (Python 3.11 or newer, git, Poppler and pandoc for reading documents) the installer
offers to fetch through winget, asking first. Windows 11 already has winget.

## 1. Install

The quickest way: download **`aicleaner-setup.zip`** from the
[latest release](https://github.com/specul8/aicleaner/releases/latest), extract it anywhere, and
double-click **`Install aicleaner.cmd`**. It runs the installer below for you, step by step, and -
unless you switch your AI tools over at its last question - opens the **setup wizard** in your
browser (`http://127.0.0.1:8787/#/setup`), which walks you through adding your first client, remote
access if you want it (including what moving your domain's DNS to Cloudflare would involve), and
routing your AI tools last. The setup page is in
Settings too ([Set up](Settings.md#card-setup)), on this machine only.

By hand: copy `Deploy-AiCleaner.ps1` and a `contact-<audience>.json` (its `repo_url` names the
repository) into one folder and run, with no parameters:

```powershell
.\Deploy-AiCleaner.ps1
```

The first question is **which AI agent** this is for. Only Claude is supported today; ChatGPT and
Copilot are listed as coming soon, and choosing one shows where to ask for it (a feature request in
the repository you installed from) and installs nothing.

The second is **how you use Claude**. A machine is often more than one, so give every number that
applies (for example `1 3`). The default - marked `*`, and what Enter chooses - is what was found on
this machine, shown in green (for example "VS Code Server IS here"):

| Answer | Meaning |
|---|---|
| 1 | Claude Code in VS Code, and you work at this PC |
| 2 | Claude Desktop on this PC (needs an elevated PowerShell for one step) |
| 3 | Claude Code here, reached from another PC over SSH (VS Code Remote) |
| 4 | Claude Code here, driven from a plain SSH terminal |
| 5 | None here: this device uses a remote AI agent on a different computer (screenshot tooling only) |
| 6 | None yet: just put the files in place |

If you take screenshots on the machine, the next question is which keys you press to take one; the
installer makes them send the screenshot's text instead. Before the installers run, the changes they
will make are listed and numbered, each one its default. Press Enter to accept them all, or type the
numbers of the ones you want to change (for example `2 5`): only those questions are asked, and
answering one differently may bring a follow-up. Claude Desktop still asks for its API key.

Every question says what Enter chooses. Yes/no questions show `[Y/n]`; a choice between two shows
the letter to type in colour, for example `(Y)es / (N)o`. Folders are typed in full, the suggestion
in brackets.

### What a run does, in order

An interactive run has five framed steps and ends either with **filtering on** - you set a client
up and said yes at the last question - or with the **setup wizard** open in your browser, where you
add a client, set up remote access if you want it, and switch your AI tools over last. Everything
before that is safe to re-run and cannot stop you working, because Claude Code is still talking to
the AI service directly until then.

1. **Which agent, and which roles this machine has** - the two questions above.
2. **Where things go.** The code always goes to `%USERPROFILE%\.aicleaner\app` (`-InstallPath` to
   override); you are asked where the client files go - `<OneDrive>\aicleaner\clients` is suggested,
   so clients can be shared with a team (see [Sharing a client with a team](Sharing.md)).
3. **Which release.** It confirms the repository, checks it can be read (asking for a token if it
   is private), finds the newest signed `vX.Y.Z` release (unsigned ones are refused without
   `-AllowUnsigned`), and shows the plan - release, folders, roles - for you to confirm.
4. **Fetches and verifies** the release against its signature, and copies only the files the roles
   need into the app folder.
5. **Configuration**: creates `aicleaner-config.json` from the example if there is none, points it
   at the clients folder, creates that folder, and pins it to this device under OneDrive so the
   proxy never meets a cloud-only file. Copies your `contact-*.json` beside it.
6. **Records** the install in `%USERPROFILE%\.aicleaner\deployment.json`, so a failed step can still
   be updated or uninstalled.
7. **Wires each role.** For Claude Code (answers 1, 3, 4) `Install-AiCleaner-VSCode.ps1`:
   - finds Python 3.11+ (offering to install it), builds the venv in `%USERPROFILE%\.aicleaner\venv`
     and installs the requirements;
   - validates the client mapping;
   - in `%USERPROFILE%\.claude\settings.json`: `skipWebFetchPreflight`, and Claude Code's telemetry
     and error reporting off (`DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`) - Claude Code still
     updates itself; VS Code's telemetry off too (local and VS Code Server settings);
   - writes the aicleaner rules block into your user `CLAUDE.md` (files converted locally, never sent);
   - removes opt-out variables and the weekly update task an earlier install left, reviews
     OpenTelemetry variables that would capture content around the proxy, and checks for settings
     that would route around it;
   - registers the `aicleaner-proxy` logon task and starts the proxy on `127.0.0.1:8787`;
   - Snipping Tool and the PowerToys remap that make the screenshot shortcut produce text, the
     document tools (pandoc, Poppler, the Python Office readers) and PSScriptAnalyzer - each only if
     missing;
   - checks `/healthz`.

   It does **not** point Claude Code at the proxy - that is step 9. Claude Desktop (answer 2) and
   the screenshot-only role (answer 5) have installers of their own; see Claude Desktop below.
8. **Final Steps**: says the release is deployed and prints what is left to do - only what applies
   here: open the app's Client Filters page and click **Add Client**. Reached from another PC
   (answers 3, 4), it also says what that means for screenshots and how to fix it on that PC.
9. **"Ready to activate AI Cleaner?"** The proxy is running but not in the path:
   - **(Y)es** (the default) - you have set up at least one client: it runs `Enable-AiCleaner.ps1`,
     which makes five checks (one refuses until a client has at least one entry) and, when all pass,
     points your AI tools at the proxy (section 3). The run ends with **"Filtering is on"** - a
     session already running follows the change without a restart.
   - **(N)o** - you still need to set a client up: the **setup wizard** opens in your browser
     (`http://127.0.0.1:8787/#/setup`). Add the client there and switch the tools over in its last
     step.
   - **A check fails**: nothing is switched over; it names what to fix, and the wizard opens.

**Remote access is set up in the app**, not by the installer: in the setup wizard or in Settings
(see [Remote access](Remote-access.md)). `-RemoteHostname` and `-Identity` are accepted, for an old
command line, and ignored.

An **unattended** run (`-AssumeDefaults`) stops at step 9 with "provisioned but not switched
over": nobody is there to set a client up, and answering the question with its default would route
traffic through an empty filter. Every other answer has a parameter for scripted runs (`-Target`,
`-ClientsPath`, `-Ref` and so on); `-WhatIf` shows the plan. Later:

```powershell
.\installer\Deploy-AiCleaner.ps1 -Action Update                  # newest release, data untouched
.\installer\Deploy-AiCleaner.ps1 -Action Rollback                # the previous build back
.\installer\Deploy-AiCleaner.ps1 -Action Uninstall               # wiring and code removed, data kept
.\installer\Deploy-AiCleaner.ps1 -Action Uninstall -RemoveData   # the data too, after typing REMOVE
.\installer\Deploy-AiCleaner.ps1 -Action Uninstall -RemoveAppFiles   # and the app itself, data kept
.\installer\Deploy-AiCleaner.ps1 -Action Uninstall -RemoveRemote     # and offer to take remote access down
```

`-RemoveAppFiles` takes the machine back to where an install deploys every file afresh: it also
removes the Python environment, rollback copies, backups and anything left in the app folder that is
not data. Your client files, Global, the configuration, contact, profiles, ignore list, review queue,
logs and sessions are kept.

`-RemoveRemote` offers to take remote access down: the tunnel on this machine, and - with the stored
Cloudflare API token - its tunnel, DNS record and Access application in your account; the
configuration forgets it. Without it, remote access is kept (the app's Remote Access step offers to
keep or replace it). To take remote access down without uninstalling:
`.\installer\Uninstall-AiCleaner.ps1 -RemoteOnly`.

## 2. Set the filter up (during the pause)

Routing traffic through a filter that knows no clients would forward everything untouched while
looking as if it works, and fill Review with everything it sees - which is why the install waits
for this. Open `http://127.0.0.1:8787/` in a browser on the same machine while the deploy script
waits at its pause. Over VS Code Remote, forward port 8787 in the Ports view and open the forwarded
address.

1. In the sidebar, choose **+ Add new client**.
2. Fill in the dialog:

   | Field | What to put |
   |---|---|
   | Client id | A short key such as `contoso`. It names the client's file and is what the command line uses. |
   | Display name | What you read in the UI, such as `Contoso Pharmaceuticals`. Optional. |
   | Index | Leave it as automatic. It is the number inside the tokens the AI sees (index 42 gives `Client42`). |

3. On the client's page, add the entries that name the client: the full name, the short name or
   acronym, house spellings, email domains, the tenant name, the people you deal with.
4. Scan a repository of theirs, on the same client page: the **Scan a folder for this client** card.
   Choose the folder with its picker, then one of:

   | Button | What it does |
   |---|---|
   | **Scan and map** | Reads the folder and maps every high-confidence find to this client. |
   | **Preflight and map** | The same, over the folder **and** its Claude Code history - so picking up existing work does not send a first request full of identifiers the filter has never seen. |
   | **Scan for review** | Reads the folder and puts the finds in Review, already marked as this client's, to decide by hand. Writes nothing to the mapping. |

   A progress bar shows the reading and then the writing. Scan again after a big change to the repo.

   The same is available from PowerShell in the app folder, plus one thing the UI does not do -
   reading names straight from the client's Azure tenant:

   ```powershell
   .\aicleaner seed contoso --resources                       # org name, domains, tenant, subscriptions
                                                              # and resource names from the tenant you
                                                              # are signed in to with az
   .\aicleaner preflight C:\Repos\contoso-app                 # what the repo and its Claude Code history would send
   .\aicleaner preflight C:\Repos\contoso-app --add contoso   # the same, and map what it found
   ```

   Useful switches: `--add <client>` maps what is found to that client (the UI's "... and map");
   `--queue <client>` puts the finds in Review marked as that client's instead (the UI's "Scan for
   review"); `--resources` on `seed` also adds every resource name in the tenant's subscriptions;
   `--top N` prints more finds. `preflight` says `READY` when nothing unmapped is left.
5. Open **Review** and decide about anything waiting there: map it to the client, put it in
   Global, or ignore it. See [Review](Review.md).

More on clients, entries and Global: [Client Filters](Client-Filters.md).

## 3. Switch filtering on

Answering **Yes** at the deploy script's pause does this for you. If you answered No, ran
unattended, or a check failed, run it yourself when the filter knows your client (from the app
folder):

```powershell
.\installer\Enable-AiCleaner.ps1
```

It runs five checks first, and any failure stops it:

1. the proxy answers `/healthz`;
2. the mapping loads and no client file is broken;
3. at least one client has an entry;
4. a real value round-trips and the canary is clean;
5. a test request survives the whole path to the AI service.

Only then does it point `ANTHROPIC_BASE_URL` at `http://127.0.0.1:8787/anthropic` in
`%USERPROFILE%\.claude\settings.json` and in VS Code's `claudeCode.environmentVariables`. A session
already running picks up the change without a restart.

Check it: in Claude Code, run `/status` - the Anthropic base URL line should show the proxy. The
Overview page in the UI shows what each AI client on the machine is pointed at.

To get back to work quickly if the filter itself is the problem, `.\installer\Disable-AiCleaner.ps1` points
both settings back at the AI service and leaves everything else installed.

## Claude Desktop

Answer 2 (or `-Target Desktop`) runs `Install-AiCleaner-Desktop.ps1`, which needs an **elevated**
PowerShell signed in as the same user. It:

- sets Claude Desktop's per-user policy (`HKCU\SOFTWARE\Policies\Claude`) to gateway mode, pointed
  at the proxy;
- gives the app a locally generated gateway token, and stores your Anthropic Console API key
  DPAPI-encrypted; the proxy swaps the token for the key, so the app never holds the key.

Know the trade-offs: gateway mode uses a separate app profile (`Claude-3p`), so claude.ai chats and
projects are not visible there, and usage is billed to the Console key, not a subscription. The same
account on a phone, another PC or claude.ai in a browser has no aicleaner in front of it.
`-Remove` on the Desktop installer takes the policy away again.

## 4. Start a first Project Session

The Project Sessions page runs Claude Code from the browser, through the filter. It is off by
default.

1. **Settings**, [Session page](Settings.md#card-session): tick **Drive Claude Code sessions from the Session page** and save.
2. **Project Sessions**: choose **Edit project profiles**, then **+** to add one.
3. Give it a name, pick the client, add the repo folders it works in, and optionally a model and a
   standing brief. **Save**.
4. Pick the profile and choose **Start**, then **Start chatting**.

Questions and permission prompts from Claude Code appear on the page with Allow and Deny. Names
restored in replies are marked in green. More in [Project Sessions](Project-Sessions.md).

## What to check on day one

- The **Overview** banner reads "Client-identity data filtering: On". Amber means something is
  waiting in Review; red means traffic is stopped - see [Troubleshooting](Troubleshooting.md).
- The first days on a new client produce the most Review items. Work through them as they arrive.
- To use the UI from another device, see [Remote access](Remote-access.md).

---

[Home](Home.md) > Getting started
