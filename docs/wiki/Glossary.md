# Glossary

[Home](Home.md) > Glossary

The words this wiki uses, in alphabetical order. Examples use the fictional client Contoso with
index 42.

**Approval** - On the [Project Sessions](Project-Sessions.md) page, a permission prompt from Claude
Code (may I edit this file, run this command) shown as a card with Allow and Deny. Anything that
cannot be asked is denied.

**Archive** - Taking a client out of use without deleting it: its file moves to the clients folder's
`_archive` subfolder and is no longer loaded, and its index stays reserved so its tokens are never
reused.

**Auto-add** - Adding an unknown value to a client without asking, only where the value proves its
owner: an email at the client's mapped domain, a resource name carrying the client's name, a name
carrying the client's token. Set per kind in [New names found](Settings.md#card-kinds).

**Auto-tokenise** - A per-kind choice: unknown values of that kind are replaced automatically and not
offered in Review. IP addresses default to this.

**Bypass** - The `proxy.bypass` setting. Forwards everything untouched. Off by default, local only,
and logged on every request.

**Canary** - The last check before a request leaves: it looks for any declared real value that
survived tokenising and refuses the request (403) if it finds one. It does not find unmapped
identifiers. See [How it works](How-it-works.md#the-canary).

**Case variant** - A spelling of an entry in different case (`CONTOSO` for `Contoso`). Matched
anyway, given its own token, and restored exactly as typed. Remembered per user, not in the shared
file.

**Client** - One engagement: a file of entries, an ignore list, and an index, kept in its own folder
in the clients folder. Every client's entries are replaced in every session, not only the sessions
labelled with that client.

**Client id** - The short key that names a client's file and is used on the command line, such as
`contoso`.

**Client key** - A secret per client, shared out of band with the people on the engagement, used to
sign the client file. A change not signed with it is held for review. Stored DPAPI-encrypted under
your profile, never in the shared folder. See [Security model](Security-model.md).

**Conflict copy** - A `<name>-<COMPUTER>.json` file OneDrive leaves when two copies clash. Never
loaded as a client; the client is flagged.

**Detail log** - See *substitution log*.

**Dialect** - How aicleaner reads one AI service's API, chosen by the route prefix: `/anthropic/`
(in use) or `/openai/` (built, not wired to any tool).

**Entry** - One real value of a client and its token, for example `contoso.com` and
`client42.invalid`. Called a *form* in the code and the client files.

**Evidence log** - `logs\evidence-<host>-<date>.jsonl`. One line per request: substitution counts
per client, the canary result and what was held back. Never content.

**Fail closed** - Refusing traffic rather than letting it through uninspected: a body that is not
JSON, a canary hit, a broken client file.

**Fail open** - The `canary.fail_open` emergency switch: a request the canary would refuse is sent
anyway and marked in the evidence log. Off by default.

**Footer** - A short line aicleaner appends to a reply when something new is waiting in Review.
Removed again from the history before it is sent back to the AI service.

**Form** - See *entry*.

**Global** - Your own filters (your employer, vendors you use), in `_global.json` beside the
config. Applied to every client, never shared. Its tokens use the prefix `Org` instead of `Client`.
When the same value is in Global and a client, Global wins on this machine.

**Held** - A client file changed without the client key's signature. The last-good copy stays in use
until you accept or reject the change.

**Ignore list** - Values that are not identifiers. A client's ignore list travels with its file; the
global ignore list (`ignore-list.json`) is yours. An ignored value is not queued or held back. An
ignored domain also covers the hosts under it.

**Index** - The number inside a client's tokens (index 42 gives `Client42`). Set explicitly, else
taken from trailing digits of the client id, else derived from a hash of the id - so every machine
that names a client the same derives the same index.

**Kept tools** - Tools whose arguments stay tokenised (`keep_tokenised_tools`), because they send
what they are given to a hosted service: remote MCP servers such as Microsoft Learn and Context7.

**Kind** - The shape of a detected identifier: email, domain, tenant, SharePoint, IP, GUID, DevOps
org or project, client resource, name. Each kind has a setting: Auto-tokenise, Tokenise & Review,
or Ignore.

**Last-good copy** - A per-machine copy of each client file as it last loaded cleanly, in
`%USERPROFILE%\.aicleaner\lastgood\`. Used when the file becomes invalid, missing, or held.

**Mapping** - Every client's entries together, compiled into one two-way table of real values and
tokens.

**Near miss** - A word that is almost a client's name, typically an OCR misreading of a screenshot or
scan. Corrected before tokenising, or held back for review.

**Placeholder** - A temporary stand-in for an unknown identifier, such as `pending-<hash>.invalid`,
used until you decide about it in Review. Stable for a given value, and rehydrated in replies.

**Preflight** - `aicleaner preflight <folder>`: scans a repository and its Claude Code history for
identifiers the mapping does not know, and ends `READY` or `NOT READY`.

**Profile** - A *project profile*: one engagement on the Project Sessions page - a name, a client, the
repo folders, a model and a standing brief. Stored in `profiles.json` on this machine, because folder
paths differ between machines.

**Rehydrate** - Turning tokens and placeholders in a reply back into real values, so what you read,
and what tools write and run, uses real names.

**Review** - The page listing identifiers seen on the wire that the mapping does not know. Map each to
a client or Global, or ignore it. See [Review](Review.md).

**Scan** - `aicleaner scan <folder>`: lists what a folder would send that the mapping does not know.
`--add` maps the finds; `--queue` sends them to Review.

**Seed** - `aicleaner seed <client-id>`: fills a client's entries from the tenant you are signed in to
(organisation name, domains, tenant and subscription ids; `--resources` adds resource names).

**Session** - A *Project Session*: Claude Code run by aicleaner on this machine and driven from the
browser, filtered exactly like a terminal session. One per profile at a time, plus worktree sessions.

**Share** - Two meanings. *Sharing a client*: putting its folder in OneDrive and sharing it with the
people on the engagement (see [Sharing a client with a team](Sharing.md)). A client's *share folder*:
a OneDrive folder per client, recorded in `client-shares.json` on this machine, used to move files
between this machine and the device driving a session.

**Stamp** - The `_stamp` in a client file: a checksum, the machine and the time, plus a signature
when the client has a key. A mismatch means the file was edited outside aicleaner.

**Stand-in** - Any value sent in place of a real one: a token, a placeholder, or the stateless
replacement every GUID gets.

**Substitution log** - `logs\detail\substitutions-<host>-<date>.jsonl.gz`: each real value and the
token that replaced it, per request. Holds real values, so it never leaves the machine. Kept 90 days
by default.

**Token** - A stable stand-in for one known real value, shaped like it: `Contoso` becomes `Client42`,
`contoso.com` becomes `client42.invalid`. Stored in the client file, so everyone sharing it uses the
same token.

**Tokenise** - Replacing every known real value in a request with its token.

**Tokenise & Review** - A per-kind choice: unknown values of that kind are replaced by placeholders
and queued for Review.

**Tunnel** - The Cloudflare tunnel that publishes the UI for [Remote access](Remote-access.md),
behind Cloudflare Access. The *encrypted tunnel* adds end-to-end encryption between the page and the
proxy, unlocked by a *tunnel code* generated on the machine.

**Worktree session** - A second chat session on the same project, in a git worktree of its own
(`<repo>.worktrees/<name>`, on a branch of the same name), so two sessions do not commit over each
other. Its work comes back by merging the branch.

---

[Home](Home.md) > Glossary
