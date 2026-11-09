# Glossary

[Home](Home.md) > Glossary

The words this wiki uses, in alphabetical order. Examples use the fictional topic Contoso with
index 42.

**AI session** - Claude Code running now for a project session on the
[Project Sessions](Project-Sessions.md) page: it can be busy, stopped or restarted.

**Approval** - On the [Project Sessions](Project-Sessions.md) page, a permission prompt from Claude
Code (may I edit this file, run this command) shown as a card with Allow and Deny. Anything that
cannot be asked is denied.

**Archive** - Taking a topic out of use without deleting it: its file moves to the topics folder's
`_archive` subfolder and is no longer loaded, and its index stays reserved so its tokens are never
reused.

**Auto-add** - Adding an unknown value to a topic without asking, only where the value proves its
owner: an email at the topic's mapped domain, a resource name carrying the topic's name, a name
carrying the topic's token. Set per kind in [New names found](Settings.md#card-kinds).

**Auto-tokenise** - A per-kind choice: unknown values of that kind are replaced automatically and not
suggested in Review. IP addresses default to this.

**Canary** - The last check before a request leaves: it looks for any declared real term that
survived tokenising and refuses the request (400) if it finds one. It does not find unmapped
identifiers. See [How it works](How-it-works.md#the-canary).

**Case variant** - A spelling of a term in different case (`CONTOSO` for `Contoso`). Matched
anyway, given its own token, and restored exactly as typed. Remembered per user, not in the shared
file.

**Conflict copy** - A `<name>-<COMPUTER>.json` file OneDrive leaves when two copies clash. Never
loaded as a topic; the topic is flagged.

**Detail log** - See *substitution log*.

**Dialect** - How AI Cleaner reads one AI service's API, chosen by the route prefix: `/anthropic/`
(in use) or `/openai/` (built, not wired to any tool).

**Evidence log** - `logs\evidence-<host>-<date>.jsonl`. One line per request: substitution counts
per topic, the canary result and what was held back. Never content.

**Fail closed** - Refusing traffic rather than letting it through uninspected: a body that is not
JSON, a canary hit, a broken topic file.

**Footer** - A short line AI Cleaner appends to a reply when something new is waiting in Review.
Removed again from the history before it is sent back to the AI service.

**Global** - Your own filters (your employer, vendors you use), in `_global.json` beside the
config. Applied to every topic, never shared. Its tokens use the prefix `Org` instead of `Client`.
When the same term is in Global and a topic, Global wins on this machine.

**Held** - A topic file changed without the topic key's signature. The last-good copy stays in use
until you accept or reject the change.

**Held back** - What happens to an unknown identifier of a kind set to Tokenise & Review: it is
sent as a temporary token, such as `pending-<hash>.invalid`, until you decide about it in Review.
Stable for a given value, and rehydrated in replies.

**Ignore list** - Values that are not identifiers. A topic's ignore list travels with its file; the
global ignore list (`ignore-list.json`) is yours. An ignored value is not queued or held back. An
ignored domain also covers the hosts under it.

**Index** - The number inside a topic's tokens (index 42 gives `Client42`). Set explicitly, else
taken from trailing digits of the topic id, else derived from a hash of the id - so every machine
that names a topic the same derives the same index.

**Kept tools** - Tools whose arguments stay tokenised (`keep_tokenised_tools`), because they send
what they are given to a hosted service: remote MCP servers such as Microsoft Learn and Context7.

**Kind** - The shape of a detected identifier: email, domain, tenant, SharePoint, IP, GUID, DevOps
org or project, resource name, name. Each kind has a setting: Auto-tokenise, Tokenise & Review,
or Ignore.

**Last-good copy** - A per-machine copy of each topic file as it last loaded cleanly, in
`%USERPROFILE%\.aicleaner\lastgood\`. Used when the file becomes invalid, missing, or held.

**Mapping** - Every topic's terms together, compiled into one two-way table of real terms and
tokens.

**Preflight** - `AI Cleaner preflight <folder>`: scans a repository and its Claude Code history for
identifiers the mapping does not know, and ends `READY` or `NOT READY`.

**Project assets folder** - A OneDrive folder per topic, recorded in `client-shares.json` on this
machine, used to move files between this machine and the device driving a session.

**Project session** - One engagement on the [Project Sessions](Project-Sessions.md) page - a name, a
topic, the repo folders, a model and a standing brief - and the Claude Code it runs, from the
browser, filtered exactly like a terminal session. Stored in `profiles.json` on this machine, because
folder paths differ between machines. One AI session per project session at a time, plus worktree
sessions.

**Real** - A term's own text, as it is on your machine (the **Real** column on Topics), as opposed
to its token.

**Rehydrate** - Turning tokens and held-back tokens in a reply back into the real terms, so what you
read, and what tools write and run, uses the real terms.

**Review** - The page listing identifiers seen on the wire that the mapping does not know. Map each to
a topic or Global, or ignore it. See [Review](Review.md).

**Scan** - `AI Cleaner scan <folder>`: lists what a folder would send that the mapping does not know.
`--add` maps the finds; `--queue` sends them to Review.

**Seed** - `AI Cleaner seed <client-id>`: fills a topic's terms from the tenant you are signed in to
(organisation name, domains, tenant and subscription ids; `--resources` adds resource names).

**Send everything unfiltered** - The `proxy.bypass` setting. Forwards everything untouched. Off by
default, local only, and logged on every request.

**Send what the canary refuses** - The `canary.fail_open` emergency switch: a request the canary
would refuse is sent anyway and marked in the evidence log. Off by default.

**Sharing** - *Sharing a topic*: putting its folder in OneDrive and sharing it with the people on
the engagement (see [Sharing a topic with a team](Sharing.md)). Not the same as the topic's
*project assets folder*.

**Stamp** - The `_stamp` in a topic file: a checksum, the machine and the time, plus a signature
when the topic has a key. A mismatch means the file was edited outside AI Cleaner.

**Substitution log** - `logs\detail\substitutions-<host>-<date>.jsonl.gz`: each real term and the
token that replaced it, per request. Holds the real terms, so it never leaves the machine. Kept 90
days by default.

**Suggestion** - Something for you to decide on in Review: an identifier the mapping does not know,
or a word that is almost a topic's name (typically an OCR misreading of a screenshot or scan, which
is corrected before tokenising or held back for review).

**Term** - One real value of a topic and its token, for example `contoso.com` and
`client42.invalid`. Called a *form* in the code and in the topic files.

**Token** - A stable replacement for one known real term, shaped like it: `Contoso` becomes
`Client42`, `contoso.com` becomes `client42.invalid`. Stored in the topic file, so everyone sharing
it uses the same token. Any value sent in place of a real one is a token: a lasting one, a held-back
one, or the stateless replacement every GUID gets.

**Tokenise** - Replacing every known real term in a request with its token.

**Tokenise & Review** - A per-kind choice: unknown values of that kind are held back behind
temporary tokens and suggested in Review.

**Topic** - Anything whose names you keep out of AI services: a client, a product, a person. One
engagement: a file of terms, an ignore list, and an index, kept in its own folder in the topics
folder. Every topic's terms are replaced in every session, not only the sessions labelled with that
topic.

**Topic id** - The short key that names a topic's file and is used on the command line, such as
`contoso`.

**Topic key** - A secret per topic, shared out of band with the people on the engagement, used to
sign the topic file. A change not signed with it is held for review. Stored DPAPI-encrypted under
your Windows profile, never in the shared folder. See [Security model](Security-model.md).

**Tunnel** - The Cloudflare tunnel that publishes the UI for [Remote access](Remote-access.md),
behind Cloudflare Access. The *encrypted tunnel* adds end-to-end encryption between the page and the
proxy, unlocked by a *tunnel code* generated on the machine.

**Worktree session** - A second AI session on the same project, in a git worktree of its own
(`<repo>.worktrees/<name>`, on a branch of the same name), so two sessions do not commit over each
other. Its work comes back by merging the branch.

---

[Home](Home.md) > Glossary
