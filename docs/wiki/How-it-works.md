# How it works

[Home](Home.md) > How it works

aicleaner is one local process (`aicleaner/proxy.py`) listening on `127.0.0.1:8787`. AI tools are
pointed at it instead of at the AI service. Everything under a route prefix is forwarded to that
prefix's upstream, after the request has been cleaned:

| Prefix | Forwarded to | Replies handled by |
|---|---|---|
| `/anthropic/` | `https://api.anthropic.com` | `aicleaner/dialects/anthropic.py` (Messages API events) |
| `/openai/` | `https://api.openai.com` | `aicleaner/dialects/openai_chat.py` (built, not wired to any tool) |

The same process serves the UI at `/`, which only answers on a loopback address.

## The request path

For each request, in this order:

1. **Stopped?** If any client file is broken with no good copy (see below), the request is refused
   with "Client-identity data filtering: Stopped" and a link to the client's page.
2. **URL and headers.** A client's real name in the path, query or a header is refused: those parts
   are sent as written, so they are checked rather than rewritten.
3. **Body must be JSON.** A body that is not JSON is refused (403), because it cannot be inspected.
   The only exception is a path listed in `allow_non_json_paths`; such a request is forwarded
   uninspected and logged as `UNFILTERED`. The list is empty by default.
4. **Clean the history.** Green marks and review footers that aicleaner added to earlier replies are
   removed, so the AI service never sees them.
5. **Attachments.** PDFs, Office documents and images become text (Poppler, the standard library,
   OCR) which is then filtered like everything else, or are withheld and replaced with a notice.
   Images are only sent as images if [Images](Settings.md#card-images) allows it, and then they go unfiltered.
6. **Tokenise.** Every string in the JSON - and every key - has each known real value replaced with
   its token.
7. **Detect.** The tokenised text is swept for anything still shaped like an identifier (email,
   domain, tenant, SharePoint, IP, DevOps org or project, a resource name carrying a client's name).
   Where the value proves its owner - an address at a client's mapped domain, a name carrying one
   client's token - it is added to that client at once and the request is tokenised again.
8. **Credentials.** Secrets in the text are redacted (the default), or the request is blocked, per
   the Secrets page.
9. **Hold back the unknown.** Unknown values of the kinds set to "Tokenise & Review" are replaced
   with placeholders and queued for [Review](Review.md). GUIDs are always replaced by a stand-in.
10. **The canary.** The finished body is read once more for any known real value. If one survived,
    the request is refused (403) and never sent.
11. **Send.** Claude Code's own subscription login is forwarded untouched; for Claude Desktop the
    local gateway token is swapped for the stored API key.

On the way back the reply is **rehydrated** - tokens and placeholders become real values again - and
a short review footer is added when something new is waiting in Review.

Every request writes one line to the evidence log (`logs\evidence-<host>-<date>.jsonl`): counts per
client, the canary result, what was held back - never content.

## Tokens

A token is a stable stand-in for one real value, shaped like it so the model can still reason about
it. For a client Contoso with index 42:

| Shape | Real | Token |
|---|---|---|
| name, one word | `Contoso` | `Client42` |
| name, several words | `Contoso Pharma` | `Client42 Pty` |
| slug | `contoso-pharma` | `client42-pty` |
| domain | `contoso.com` | `client42.invalid` |
| tenant | `contoso.onmicrosoft.com` | `client42.onmicrosoft.invalid` |
| SharePoint | `contoso.sharepoint.com` | `client42.sharepoint.invalid` |
| email | `jane@contoso.com` | `user42` + 4 hex + `@client42.invalid` |
| GUID | a tenant id | `00000000-0000-4000-8000-000042` + 6 hex |
| IPv4 / IPv6 | an address | inside `198.18.0.0/15` / inside `2001:db8::/32` |
| resource name carrying a form | `kvprdcontosomgmt` | `kvprdclient42mgmt` |

The ranges are chosen so a token can never reach anything real: `.invalid` is a reserved top-level
domain, `198.18.0.0/15` is never routed, and `2001:db8::/32` is the documentation prefix.

**How a token is derived** (`aicleaner/mapping.py`: `client_index`, `token_for`, `client_tokens`):

- The **client index** is the explicit `index` in the client file; else the trailing digits of the
  client id; else `1000 + (hash of the lower-cased id mod 9000)`. Everyone who names a client
  `contoso` derives the same index with no coordination.
- The **shape** is guessed from the text (`classify`), or set on the entry when the guess is wrong.
- A token already **stored** in the file is used exactly as written; only the rest are derived, in
  file order. New entries are stored with their token, so teammates sharing the file get the same
  one, and an improved derivation never moves a token already in use.
- When a second value would get the same token (a second domain, say), a short hash of the real
  value is appended to keep them apart.
- **Global** (your own filters) uses the same rules with the prefix `Org` instead of `Client`.

**Matching** is whole-word, longest form first, and ignores case. A case variant nobody declared is
matched, given its own token, and comes back exactly as it was typed; such variants are remembered
per user in `%USERPROFILE%\.aicleaner\case-variants.json`. Going out, a client's name glued into an
identifier (at a case boundary, as in `newContoso`) is matched too.

## Placeholders

An identifier the mapping does not know cannot have a token yet - nobody has said whose it is. For
the kinds set to "Tokenise & Review" ([What happens to something new](Settings.md#card-kinds)), a high-confidence find is replaced by a
**placeholder** and queued for Review (`make_placeholder` in `aicleaner/detect.py`):

| Kind | Placeholder |
|---|---|
| domain | `pending-<hash>.invalid` |
| email | `pending-<hash>@pending.invalid` |
| tenant / SharePoint | `pending-<hash>.onmicrosoft.invalid` / `pending-<hash>.sharepoint.invalid` |
| IPv4 | an address in a documentation range (`192.0.2.x`, `198.51.100.x`, `203.0.113.x`) |
| DevOps org or project, resource name | `pending-<hash>` |

The hash comes from the value, so the same unknown gets the same placeholder on every request.
Replies are rehydrated with placeholders as well, so you and your local tools still see the real
value. When you decide in Review, the value becomes a real entry with a real token (or is ignored).

Plain capitalised names are never held back: they are queued for Review and travel as written while
they wait. A kind set to "Ignore" is neither queued nor held back.

## Rehydration

**JSON replies** are rehydrated string by string.

**Streamed replies** arrive as many small events, and a token can be split across two of them. Each
content block keeps back its last few characters (the longest token's length, less one) until more
text or the end of the block arrives, so a token is never half-replaced.

- **Text** is rehydrated as it streams. In Project Sessions, restored names are marked in green.
- **Thinking blocks** pass through untouched. The service signed the tokenised text, and Claude
  Code sends the block back; changing it would make the resend invalid.
- **Tool calls** are rehydrated as they stream, so a command, a file path or a file body runs on
  this machine with the real names.
- **Kept tools** (`keep_tokenised_tools`, by default remote Microsoft Learn and Context7 MCP tools)
  keep their tokens, so a hosted server never receives real names.

Everything else - local tools, file writes, `az`, `git` - gets real values, which is what keeps
files, commits and your terminal in real names.

## The canary

The canary is the last check before a request leaves. It re-reads every decoded string and key in
the outgoing body and looks for any real value the mapping declares, including JSON-escaped
spellings and disguised ones (percent-encoding, zero-width characters). A hit means something
survived tokenising: the request is refused with a 403, nothing is sent, and the local log records
where it matched.

The canary does **not** find unmapped identifiers - it only knows declared ones. Finding unknown
identifiers is the detectors' job (step 7). A per-entry match rule that declines a match is honoured
and logged rather than refused. Settings has an emergency switch, [Emergency: canary fail open](Settings.md#card-fail-open) (`canary.fail_open`), that sends a
refused request anyway, logged loudly; it is off by default.

## Last-good copies and fail-closed states

Each client file is checked on its own. Every time one loads cleanly, a copy is kept in
`%USERPROFILE%\.aicleaner\lastgood\<client>.json` (per machine, never shared). So one bad edit
degrades one client, not the proxy:

| State | What happened | What aicleaner does |
|---|---|---|
| ok | The file loaded | Uses it, refreshes the last-good copy |
| hand-edited | The file is valid but its stamp does not match | Uses it, flags "edited outside aicleaner" |
| edit rejected (fallback) | The file is invalid (a stray quote, a bad merge) | Uses the last-good copy; the client page offers **Restore last good version** |
| held | The client has a key and the change is not signed with it | Uses the last-good copy until you accept or reject the change |
| missing | The file is gone (unshared, unsynced, not downloaded) but a last-good copy exists | Keeps protecting with the last-good copy; red on the status bar |
| conflict copy | OneDrive left a `<name>-<COMPUTER>.json` beside the file | Never loaded as a client; flagged |
| **broken** | Invalid, and no usable last-good copy | **Every model request is refused** until it is fixed |

A broken client stops traffic because its names would otherwise go out untouched. The proxy still
starts and the UI still works, so the fix is one click away. See
[Troubleshooting](Troubleshooting.md) and [Security model](Security-model.md).

---

[Home](Home.md) > How it works
