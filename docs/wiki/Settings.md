# Settings

[Home](Home.md) > Settings

The Settings page is a set of cards, each saved on its own with its own **Save** button. Settings
are written to `aicleaner-config.json` (the key is shown in brackets below) and take effect without a
restart unless a card says otherwise. Leaving a card with unsaved changes asks first.

The cards are in groups, listed down the left of the page like Windows Settings; choosing a group
shows its cards. On a phone the groups are a drop-down at the top instead.

| Group | Cards |
|---|---|
| aicleaner | Set up, Version and updates, Start with the computer, Restart, Your details, Feedback, Menu layout, Import and export |
| Sessions | Session page, File editor, Prompt autocorrect, Real names in green, Notifications |
| Filtering | New names found, Near-misses, Shared identifiers, Names kept in tool calls, Pictures, Text in pictures and scans, Generated-output folders |
| AI apps | Sign-ins, Where your AI apps point, Claude Code: PowerShell, Claude Desktop: Code tab, Claude Desktop: chat, GitHub Copilot |
| Remote access | Encrypted tunnel, Folders the pages can open |
| Logs | Logging, Diagnostics, Advanced |
| Danger zone | Bypass filtering, Send what the canary refuses, Unchecked endpoints |

- **Find a setting**, above the groups, searches every group at once - card titles, descriptions
  and the words beside each control - and labels each card found with its group. Clearing it goes
  back to the group you had open.
- The Danger zone is last. While anything in it is on, its entry in the list is marked red, so it
  shows without opening the group (as is Filtering while Pictures sends pictures unfiltered).
- The group open is remembered in this browser. A link such as `#/settings/filtering` opens a
  group; `#/settings/folders` opens the group holding that card, at the card.
- Secrets mode (Redact, Block, Off) is on the **Secrets** page, not here.
- Through the tunnel (see [Remote access](Remote-access.md)), a card whose settings can only be
  changed on the machine itself is greyed out and says "Can only be changed where aicleaner is
  deployed".

## Everyday cards

| Card | Setting (key) | What it does | Default |
|---|---|---|---|
| [Set up](Settings.md#card-setup) | button | Opens the setup wizard: route your AI tools, add a client, remote access with a look at your domain's DNS. On this machine only. | |
| [Logging](Settings.md#card-logging) | Write the substitution log (`logs.detail`) | Records what was replaced and by what, whose it is, and who was driving - real names, kept on this machine, read on the Log page. The evidence log (counts only) is always written. | On |
| | Log every value on every request (`logs.detail_every_request`) | Off: a value is logged the first time a session sends it and when what happens to it changes. On: every value, every request, for diagnosis. | Off |
| | Keep substitution log files for (`logs.detail_retention_days`) | Days; older files are deleted at proxy start. 0 keeps them for ever. | 90 |
| [Notifications](Settings.md#card-notifications) | Learning mode (`detect.enabled`) | Find identifiers the mapping does not know and queue them for [Review](Review.md). Local only. | On |
| | Footer on the model's reply (`detect.footer`) | A line on the reply saying how many items wait in Review. | On |
| | Windows toast (`detect.toast`) | A toast with an Open review button. | On |
| [New names found](Settings.md#card-kinds) | see below | Per type of identifier. | |
| [Near-misses](Settings.md#card-near-miss) | Correct misreadings (`detect.near_miss`) | In text read from images, scans and documents only, a word one letter from a mapped identifier is corrected back to it. Typed text is never touched. | On |
| [Session page](Settings.md#card-session) | Drive Claude Code sessions from the Session page (`session.enabled`) | Turns the [Project Sessions](Project-Sessions.md) page on. Off stops any session running from it. | Off |
| [Encrypted tunnel](Settings.md#card-tunnel) | Encrypt everything that crosses the tunnel (`remote.encrypt`) | Through the tunnel, the page asks for the code once per device, then everything it sends and receives is encrypted between your browser and this machine - Cloudflare sees only sealed envelopes. **Every connection gets its own key**: each time the page connects (opening it, or reconnecting after a restart or a long idle), the browser and this machine agree a brand-new key for that connection, and the code only proves each end to the other. That key lives in the page's memory, works only for that connection, and is useless once it ends - so a recording stays sealed even if the code leaks later. Generate a code first; turning it on without one is refused. Can be switched on and off through the tunnel too (switching it off from there only works through the encrypted connection, so only someone holding the code can). | Off |
| | Where a device keeps the key (`remote.key_storage`) | What a device remembers is a key derived from the code - never the code itself: for the browser tab only (`tab`, typed again after closing it), or on the device (`device`, until you press **Forget the key on this device** there or a new code is generated - locking the device or letting it sleep does not touch it). Local only. **Forget the key on this device** (shown only through the encrypted tunnel, and usable from there) removes this device's copy; the page then asks for the code again. Nothing changes on the aicleaner machine or other devices. | Tab |
| | Generate a code | Makes a new code, shown once, on this machine only. A new code means typing it again on every device. | |
| [Real names in green](Settings.md#card-highlight) | Also colour them in Claude Code (`highlight.terminal`) | Restored names are always green on the Session page; this adds a terminal colour for Claude Code. If VS Code shows raw codes such as `[32;22m`, turn it off. | |
| [Names kept in tool calls](Settings.md#card-tools) | Patterns (`keep_tokenised_tools`) | One glob per line, for example `mcp__*learn*__*`: remote MCP tools whose arguments keep tokens instead of real names. Local tools (az, git) get real names. Local only. | Empty |
| [Where your AI apps point](Settings.md#card-routing) | read-only | Each AI client's settings file and whether it goes through aicleaner or direct; project settings that route around aicleaner are flagged in red; requests seen since start. | |
| [Restart](Settings.md#card-restart) | button | Runs Stop-AiCleaner.ps1 then Start-AiCleaner.ps1 -Detach. Sessions stop and requests in flight are cut off. Shows when the proxy started. | |
| [Folders the pages can open](Settings.md#card-folders) | One full path per line (`folders.roots`) | The only folders a folder picker can show - for a scan, a session's folders, a client's share, joining a shared client. Empty means none. System folders (Program Files, ProgramData, Windows) are refused. Folders already used by a profile or share are listed as also reachable. | Empty |
| [Generated-output folders](Settings.md#card-scan-markers) | One folder name or path fragment per line (`scan.generated_markers`) | Repositories collect output nobody wrote by hand - logs, diagnostics captures, portal exports, backups - full of version numbers, correlation ids and allow-listed domains that look like identifiers but name nobody. Scanned like hand-written files, they fill a client with junk tokens (one real scan: 962 of 1,442 finds were of that sort). In folders matching the names in this list, a scan only creates real-to-token mappings that carry a name the filter already knows. An empty list uses the built-in list: `scripts/output`, `scripts/diagnostics`, `intune-portal-export`, `state-backups`, `app-packages`, `diaglogs-`, `oobe-capture`, `/temp/`, `/out/`. | Built-in |
| [Unchecked endpoints](Settings.md#card-uninspected) | read-only (`allow_non_json_paths` in the file) | Endpoints exempt from all filtering. Red while any is listed. Change it in the config file and restart. | Empty |
| [Import and export](Settings.md#card-importexport) | see below | | |
| [Shared identifiers](Settings.md#card-public-list) | read-only | The shipped public-identifier list: counts, sources, a download, and what it has suppressed since the proxy started. | |
| [Your details](Settings.md#card-contact) | read-only | From the `contact-*.json` beside the install. | |
| [File editor](Settings.md#card-editor) | Double-click in a Markdown preview (`editor.preview_dblclick`) | **Ask the first time**, **Jump to the source with that word selected**, or **Nothing** (double-click selects text, as usual). Saved on the server, so every device follows it; can be changed through the tunnel. | Ask |
| [Prompt autocorrect](Settings.md#card-autocorrect) | Correct spelling in the prompt (`prompt.autocorrect`); also leave alone (`prompt.autocorrect_skip`: client names, words already used, capitalised words); your own words (`prompt.autocorrect_words`) | Misspelt words are corrected at the next space or punctuation - the closest candidate first - and shown in green; click or tap one for Revert, other suggestions and Add to my words. Code and paths are always left alone. Own words keep their casing (powershell becomes PowerShell). Its only switch (it left the Session page's prompt row, 2026-10-18); an open Session page follows it on Save. Saved on the server; can be changed through the tunnel. | Off |
| [Claude Code: PowerShell](Settings.md#card-bash-guard) | Use PowerShell, and stop Bash commands that contain a doubled backslash (`claude.bash_guard`) | Windows only. Claude Code's Bash tool on Windows halves every doubled backslash before the shell sees it, so text written through it can silently change. On, a hook in Claude Code's user settings stops such a Bash call and tells Claude to use the PowerShell tool (5.1 preferred, or 7) or Write/Edit; the PowerShell tool is switched on if it is not. Applies to skills and plugins too. Installed at every start while on; restart Claude Code sessions after changing it. Only on this machine. | On |
| [Diagnostics](Settings.md#card-diagnostics) | Record timings (`debug.timings`) | Times every request and each step of the pages (switching session, loading and drawing a conversation, decrypting through the tunnel) into `logs\timings-<date>.jsonl` on this machine - durations, sizes and paths, never content. Switches itself off after 24 hours; the card says until when. | Off |
| [Advanced](Settings.md#card-advanced) | read-only | Config file path, listening address and port, model routes. Edit the config file and restart to change. | |

### What happens to something new

One row per type (email, tenant, SharePoint, domain, external IP address, DevOps organisation,
DevOps project, and resource names that carry a client's name). Click the question-mark button beside a type to read what its
current choices add up to.

| Choice | Stored in | Effect |
|---|---|---|
| Auto-tokenise | `detect.quiet_kinds` | Replaced by the same stand-in every time; replies show the real value. Never stored, never in Review. Default for external IP addresses. |
| Tokenise & Review | `detect.placeholder_kinds` | Held back as a placeholder while it waits in Review. Default for the other types. |
| Ignore | in neither | Sent as written and not offered in Review. For email, the domain of an address at a client's mapped domain is still replaced, but the part before the @ is not. |
| Auto-add to client | `detect.owner_add_kinds`, and `detect.auto_map_client_resources` for resource names | With Tokenise & Review, a value that proves its owner (an address at a client's mapped domain, a SharePoint host of a client's tenant, a resource name carrying a client's name) is added to that client straight away. On by default for every type that offers it. |

GUIDs are always replaced and never proposed - the row is fixed. Names are never held back. Whatever
is chosen, a name that carries a client's token is added to that client, and an ignore wildcard
never covers a value containing a client's name.

### Import and export

- **Export this client** (pick one, including Global) or **Export all clients** downloads JSON.
- **Import** takes a file or pasted JSON - one client document or a bundle - with a mode:

| Mode | When the client is already here |
|---|---|
| Merge (default) | Adds what the file has and yours lacks; keeps everything of yours. A different token for the same value is reported, never taken. A backup is taken first. |
| New clients only | Skips a client held here with the same entries; refuses one that differs. |
| Replace | The file wins entirely. Your copy is backed up first. |

An import whose index belongs to a different client here is refused. Exports contain real
identifiers - share them as you would client work. For day-to-day team sharing, use a shared folder
instead (see [Sharing](Sharing.md)).

## Emergency cards

Bypass filtering and Send what the canary refuses are in the Danger zone group; Pictures is in
Filtering. Each is amber at rest and turns red, with a banner, while on.

| Card | Setting (key) | What it does |
|---|---|---|
| [Bypass filtering](Settings.md#card-bypass) | `proxy.bypass` | Forwards every request untouched: nothing replaced, held back, redacted or checked, replies not restored. Logged as PROXY BYPASS. Temporary, for debugging the filter. Local only. |
| [Pictures](Settings.md#card-images) | `attachments.allow_images` | Off (recommended): images are never sent; their text is read locally and filtered. On: JPEG, PNG, GIF and WebP are sent unfiltered. Also rewrites the aicleaner block in your user-level CLAUDE.md to match (with a backup). Local only. |
| [Send what the canary refuses](Settings.md#card-fail-open) | `canary.fail_open` | Sends a request the canary would refuse. Only for getting past a false "a real client identifier survived tokenisation". Each one is logged as FAIL-OPEN with its context. Can be turned on through the tunnel. Turn it off as soon as you are past the problem. |

## Settings changed elsewhere

| Setting | Where |
|---|---|
| `secrets.mode` | Secrets page |
| `session.permission_mode`, session profiles | [Project Sessions](Project-Sessions.md) |
| Remote access (`remote.enabled`, hostname, Access settings) | `aicleaner-config.json`, see [Remote access](Remote-access.md) |
| OCR limits (`attachments.ocr`, `ocr_max_pages`, `ocr_timeout_s`), `listen`, `port`, `clients_dir`, `state_dir`, upstreams | `aicleaner-config.json`, then restart |

A true/false setting written by hand must be a JSON `true` or `false`; anything else (such as the
string `"false"`) is ignored and the default is used, with a warning in the log.

---

[Home](Home.md) > Settings
