# Troubleshooting

[Home](Home.md) > Troubleshooting

This page covers the common cases. The full list of symptoms, commands and file locations is in
TROUBLESHOOTING.md, kept with the project's development records.

## First checks

| Check | How |
|---|---|
| Is the proxy running? | Open http://127.0.0.1:8787/ or `Invoke-RestMethod http://127.0.0.1:8787/healthz` |
| What does the status bar say? | The stack at the top of every page; each entry links to where it can be fixed |
| What did the proxy log? | `logs\proxy-stderr.log` in the app folder (when started detached) |
| Just updated? | Hard refresh the page: **Ctrl+Shift+R** |

## Healthz

`http://127.0.0.1:8787/healthz` answers with counts and settings, never content, so it is safe to
send when reporting a problem. Useful fields:

| Field | Meaning |
|---|---|
| `ok` | The proxy is up |
| `clients`, `forms` | Client ids loaded, and how many forms are being replaced |
| `broken` | Client files that are invalid with no good copy - traffic is refused until fixed |
| `mapping_error` | The last mapping load problem |
| `candidates_pending` | Items waiting in [Review](Review.md) |
| `canary_fail_open`, `proxy_bypass`, `detect_enabled` | Whether any protection is switched off |
| `credential_swap` | Dialects whose key the proxy swaps in (Claude Desktop) |
| `clients_in_onedrive` | Whether the clients folder is in OneDrive, so it can sync |

## Status bar states

| Shown | What it means | What to do |
|---|---|---|
| Filtering on (green) | Nothing needs attention | - |
| `<client>: file invalid` (red) | The client file failed validation and there is no good copy; model requests are refused | Open the client from the link and fix the highlighted row, or restore the last good version |
| `<client>: edit rejected` (red) | The file failed validation; the last good copy is in use and filtering continues | As above |
| `<client>: client file missing` (red) | The file has gone (unsynced, access removed, left in the cloud). The last good copy keeps its names replaced | Bring the file back: check OneDrive sync and sharing for the client folder |
| `<client>: conflict copy` (red) | OneDrive saved a second copy after two people edited at once; the copy is ignored | Merge any missing entries by hand, then delete the copy |
| `<client>: a change is waiting for review` (red) | The file changed without the client's key; last good copy in use | Open the client and review the change |
| `<client>: update aicleaner to read it` (red) | Written by a newer aicleaner | Update |
| `a project routes around aicleaner` (red) | A repo's `.claude` settings send Claude Code elsewhere, unfiltered | Remove the override |
| `N to Review` (amber) | New identifiers waiting | See [Review](Review.md) |
| `N MCP servers get tokens until allowed` (amber) | A remote MCP server receives tokens, not names | Allow it on the Overview if it needs real names |
| `client files are not in OneDrive` (amber) | Nothing syncs to colleagues | See [Sharing](Sharing.md) |

Session notices ("Claude has finished", "is waiting on you") also appear at the top of the stack;
see [Project Sessions](Project-Sessions.md#notices).

## Common symptoms

**Claude Code cannot reach the API / connection refused.** The proxy is not running - fail-closed by
design. Run `.\installer\Start-AiCleaner.ps1 -Detach`; if it will not start, read `logs\proxy-stderr.log`. A
port clash on 8787 shows with `Get-NetTCPConnection -LocalPort 8787 -State Listen`.

**"Client-identity data filtering: Stopped" in replies.** A client file is broken with no good copy.
Follow the link in the message; `/healthz` lists it under `broken`.

**HTTP 403 `aicleaner_canary`.** A real identifier survived tokenising - usually a case or spelling
variant that is not yet a form. Add it on the client's page and retry.

**The AI mentions `pending-...` or a documentation-range IP address.** That is a placeholder for an
identifier not yet in the mapping; your replies show the real value. Map or ignore it in
[Review](Review.md).

**An image or PDF "was withheld".** By design: content that cannot be checked is replaced by a
notice. Paste the text instead, or change the Images setting.

**The UI answers 421, 403 or 415.** 421: use `http://127.0.0.1:8787/`, not another host name. 403: a
write from another web origin, or the proxy is not listening on loopback. 415: an API write that was
not JSON.

**The page looks out of date after an update.** Hard refresh with **Ctrl+Shift+R**. Through the
tunnel, also reload on the remote device.

**A Session page session shows Not connected.** The page cannot reach its events; what you see may
be stale. A proxy restart does not end the session (the broker holds it): wait for the proxy to come
back and the page reconnects.

## Getting back to work quickly

If the filter itself is what stops you, stop routing through it rather than uninstalling:

```powershell
.\installer\Disable-AiCleaner.ps1     # point Claude Code and VS Code back at the real API
.\installer\Enable-AiCleaner.ps1      # route through aicleaner again
```

`Disable-AiCleaner.ps1` changes only the two base-URL settings; the proxy, mapping, logs and settings
stay. A running Claude Code session follows the change without a restart. While disabled, client
names go to the AI service unfiltered - re-enable before client work.

## Start, stop, restart

| Want to | Run |
|---|---|
| Start (hidden) | `.\installer\Start-AiCleaner.ps1 -Detach` |
| Start with logs in the console | `.\installer\Start-AiCleaner.ps1` |
| Stop | `.\installer\Stop-AiCleaner.ps1` |
| Restart | `.\installer\Stop-AiCleaner.ps1; .\installer\Start-AiCleaner.ps1 -Detach` |

A restart is needed after editing `aicleaner-config.json` (listen, port, upstreams, credentials,
`remote`) or storing an API key. Mapping, client files and ignore lists reload on their own.

## Where things are

| What | Where |
|---|---|
| Proxy log | `logs\proxy-stderr.log` (detached) or the console |
| Evidence log (counts only, safe to share) | `logs\evidence-<host>-<date>.jsonl` |
| Substitution detail (real names, never share) | `logs\detail\` - read with `.\aicleaner detail` |
| Client files | `clients\` (or the shared client folders) |
| Last good copies | `%USERPROFILE%\.aicleaner\lastgood\` |
| Session state (brokers) | `%USERPROFILE%\.aicleaner\sessions\` |
| Installer transcripts | `Install-AiCleaner-*.log`, `Uninstall-AiCleaner.log`, `Start-AiCleaner.log` |

## Reporting a problem

Send the `/healthz` output and the relevant evidence lines - both hold counts and settings, never
content. The contact is shown in Settings, [Your details](Settings.md#card-contact).

---

[Home](Home.md) > Troubleshooting
