# Client Filters

[Home](Home.md) > Client Filters

The Client Filters page is where the mapping lives: every client, the real values that belong to
it, the token each one becomes, and the strings it ignores. It is about tokenising only - where a
client's sessions run is set on the [Project Sessions](Project-Sessions.md) page.

## Clients, Global and where they are stored

| What | Where it is kept | Shared? |
|---|---|---|
| A client | `<clients location>\<id>\<id>.json` - one folder per client | Yes, with that engagement's people (see [Sharing](Sharing.md)) |
| Global | `_global.json` beside `aicleaner-config.json` | Never |
| Index register | `<clients location>\_registry.json` | With the clients location |
| Last-good copies, keys, backups, evidence of where an entry came from | `%USERPROFILE%\.aicleaner\` | Never |

- **A client** is one engagement. Its names are replaced in every session, not only in sessions
  labelled as that client's.
- **Global** is yours alone: your employer and vendors you use. It applies to every client, uses
  index 0 for its tokens, and is never shared. When the same value is in Global and in a client,
  Global wins on your machine.
- Files written flat by earlier versions (`<clients>\<id>.json`) are moved into their own folders
  on upgrade, with a backup taken first.

The page lists Global first, then every client, each as a collapsed row showing its display name,
any state chips and its entry count. Click a row to open it; rows open independently. A client
picked in the sidebar (`#/clients/<id>`) is shown on its own, already open, with a pencil to change
its display name.

## Adding a client

**Add client** (on this page, from the menu, from the setup wizard, or **+ New client** on
[Review](Review.md)) opens the **Add Client wizard**, which walks through:

1. **Client name** - the Client ID (the key: names the file, keys the mapping; 1-64 letters, digits,
   `-` and `_`, starting with a letter or digit - keep it meaningless, it appears in the evidence
   log) and the Display name (what you read everywhere; the id when blank). The number inside the
   tokens the AI sees (`Client42`) is worked out from the id.
2. **Alternate names** - other names that make the company obvious: legal names, acronyms and what
   they stand for, brands. A name is replaced on its own and at a capital-letter join (`newAcme`),
   not inside a lower-case run (`acmeish`).
3. **Domains** - `https://` and paths are taken off as you type. Each is looked up at Microsoft (only
   the domain is sent, to `login.microsoftonline.com` and `autodiscover-s.outlook.com`) for its
   tenant, SharePoint and other domains.
4. **Email domains** - `*@` each domain, prefilled: every address there counts as the client's (an
   email rule).
5. **Other identifiers** - what the Microsoft lookup found, to tick, and anything else unique to the
   client: a SharePoint host (its `-my` and `-admin` twins are added), a DevOps or GitHub
   organisation, cloud account names.
6. **Scan a repository** (optional) - a local copy of their work, read for more identifiers to tick.
7. **Finish** - the client, its index and how many entries of each kind; **Save** makes it.

What you type is kept as a draft on this machine as you go: close the wizard and **Add client**
offers to resume or delete it. **Add Global Info** runs the same pages into Global. Tick **Skip the
wizard next time** on the last page for the short form instead (Client ID and Display name only);
its "Use the step-by-step wizard instead" brings the wizard back.

## Entries (forms) and tokens

Each entry is one real value and its token. Open a client to see a table of **Real**, **Token** and
**Kind**, with a chip saying how each entry got there (Manual entry, Review, Repo scan, Auto-add,
Derived); click the chip for the local detail - when, how often seen, in which projects.

- **Add entry**: type the real value, and a token or leave it blank for one derived from the
  value's shape (`Contoso Pty Ltd` becomes something like `Client42 Pty`, `contoso.com` becomes
  `client42.invalid`). Focus returns to the first box so a list can be typed in quickly.
- **Edit in place**: click the value or the pencil. You can change the real value, the token, the
  shape the token is built from, and a **matching rule**:
  - *Matches anywhere* (the default);
  - *Only this capitalisation*;
  - *Only beside a word* - before or after one of a list of words such as `Pty, Ltd, Group`.
  A rule narrows only what is replaced; detection and the canary still use the full value. Rows
  with a rule show a chip (`cased only`, `before a word`, `after a word`).
  The row saves when you leave it; Enter saves, Escape cancels.
- **Derive again** (in edit mode): rebuilds that one token under today's rules. Stored tokens
  otherwise never change, which is what keeps them stable for everyone sharing the file.
- **Row actions**: Copy to Global, Move to another client (changes it for everyone who shares the
  client), Ignore instead of tokenising (for this client or everywhere), Delete (with a
  confirmation). On Global's rows, **Give to one client** is the reverse of Copy to Global.
- **Locked rows** (padlock): an entry that is also in Global cannot be edited, moved or deleted
  here. Remove it from Global to unlock it.

### Filtering and bulk actions

- **Filter entries** matches real values, tokens, kinds and the ignore list below.
- **Kind chips** above the table hide one kind at a time (for example every GUID), which a text
  filter cannot do.
- **Tick rows** (or Select all, which only ticks rows in view) for the bulk bar:

| Group | Buttons |
|---|---|
| Who they should belong to | **Everyone** (copy to Global), **Make them theirs** (move to the chosen client) |
| Stop watching for them | **In <client>**, **Everywhere** |
| Remove | **Delete them** (asks first) |

Each bulk action reports what was refused and why, and offers **Undo** on its message, which puts
the entries back with their original tokens.

### Email rules

A client with many addresses at its own domains can have one rule per domain instead of a row per
address. The **Email rules** card lists each of the client's domain and tenant entries as
`*@<domain>`; **Make a rule** says every address there is the client's. From then on:

- each address is replaced the first time it is seen, whatever the email setting in
  [What happens to something new](Settings.md#card-kinds);
- its token comes from the address itself (eight hex digits of its hash), so every machine sharing
  the client derives the same one, in whatever order it meets the addresses - two copies of the
  client file merge without disagreeing;
- the addresses are kept in the client file but shown as a count on the rule, not one by one.

Addresses already there keep their tokens, so transcripts written with them still restore.
**Remove rule** lists them again; nothing is deleted.

## Ignore lists

Under the entries is the client's ignore list ("Ignored for <client>"), or on Global, "Ignored
everywhere". An ignored string is never proposed in Review again for that scope.

- Each chip can be **tokenised instead** (moves it onto the same scope's token list), **moved** to
  another client's list or to Global, or removed with **x**.
- When four or more ignored hosts share a parent domain, or four or more strings share a prefix or
  suffix of at least four characters, the page **offers a pattern** (`contoso.com`, `rg-dev-*`,
  `*-validation`). Accepting adds the pattern and removes the entries it covers. A value containing
  a client's name is never covered by a pattern, so it still reaches Review.

## Find across every client

The **Find** box at the top of the list searches every client's name and id, real values, tokens
and ignored entries. Clients with no match are hidden; a client whose entries match opens with its
own filter set to the same text. Clearing the box closes the clients it opened.

## The client page, card by card

| Card | What it does |
|---|---|
| Problem callouts | Shown only when something is wrong (see States below). |
| **Names** | Client id, display name and index side by side, the matching `aicleaner scan <folder> --add <id>` command, **Change the index**, and **Open in OneDrive**. |
| Entries | The table, filter, bulk bar, **Export mapping and ignore policies** and **Archive**. |
| Ignored for <client> | The ignore list and pattern offers. |
| Scan a folder for this client | Runs a repo scan (below). Not on Global. |
| Client key | Generate, enter or rotate the client's key (see [Sharing](Sharing.md)). Not on Global. |

**Change the index** gives a client a different number for new tokens - used before sharing, when a
colleague's client has the same index. Tokens already in the file stay as they are. An index another
client here already has is refused, and the message names the next free one. Local only.

**Open in OneDrive** opens the client's folder on the OneDrive website so you can press Share there.
A work account opens the folder itself; a personal account opens OneDrive and shows the folder's
path to find. If the folder is not in OneDrive the card says so.

## Repo scan

The scan card reads every text file in a folder on the machine the proxy runs on (pick it with the
folder picker; only folders under [Folders the pages may reach](Settings.md#card-folders) can be chosen):

| Button | What it does |
|---|---|
| Scan and map | Maps every high-confidence find to this client. Writes to the mapping. |
| Preflight and map | The same, over the folder and the Claude Code history for it - use before picking up existing work. |
| Scan for review | Puts the finds in Review, already marked as this client's. Writes nothing. |

Progress shows counting, reading (files and MB) and writing as separate steps. Leaving the page
does not stop a scan; coming back shows it.

## States

| Chip | Meaning | What happens to traffic |
|---|---|---|
| (none) | The file is valid and in use. | Filtered normally. |
| edited outside aicleaner | Valid, but changed by something other than aicleaner. | Filtered normally. |
| edit rejected (amber) | The file is invalid; the last good copy is in use. The bad row is highlighted, or the file is shown around a syntax error with **Restore last good version** and **Keep the valid entries**. | Filtered from the last good copy. |
| file invalid (red) | Invalid and no good copy on this machine. **Keep the valid entries** rebuilds what is readable; the broken file is kept in `.aicleaner\broken`. | **All AI traffic stops** until it is fixed. |
| file missing (red) | A client this PC has used whose file has gone (folder unshared, unsynced or not downloaded). | Its names are still replaced, from the last good copy. |
| change waiting for review | A shared file changed without the client key. Accept or Reject. | Last good copy in use until you decide. |
| conflict copy (red) | An OneDrive conflict copy that could not be merged on its own. Compare it, then delete it. | Not loaded. |
| written by a newer aicleaner | Update aicleaner to use it. | Last good copy, if there is one. |

Red and amber states also appear in the status line at the top of every page. See
[Troubleshooting](Troubleshooting.md).

## Archive, export and import

- **Archive** moves the client's file to `<clients>\_archive\<id>.deleted-<time>.json`. It is no
  longer loaded, and its index stays reserved so its tokens are never reused.
- **Export mapping and ignore policies** downloads the whole client - every entry, token and the
  ignore list - not the current selection.
- **Import**, and exporting every client at once, are on Settings, in [Import and export](Settings.md#card-importexport).

An exported file contains real client identifiers: handle it as client work.

---

[Home](Home.md) > Client Filters
