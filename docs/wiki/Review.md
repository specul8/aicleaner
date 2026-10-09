# Review

[Home](Home.md) > Review

Review is the inbox for identifiers aicleaner saw in outgoing traffic that the mapping does not
know yet. You decide what each one is: a client's, nobody's, or not worth keeping.

## What lands here

With learning mode on (Settings > [Notifications](Settings.md#card-notifications)), every outgoing request is checked after
tokenising. Anything still identifier-shaped is, by definition, not in the mapping:

| Found | What happens to it in the request | In Review |
|---|---|---|
| A type set to **Tokenise & Review** (by default email, tenant, SharePoint, domain, DevOps organisation and project, and resource names carrying a client's name) | Held back as a **placeholder** (a stable stand-in of the same shape, on a reserved `.invalid` domain or a documentation IP range) and swapped back in replies | Listed as "held back as `<placeholder>`" |
| A low-confidence find, such as a recurring capitalised phrase | Sent as written | Listed as "sent as-is" |
| A find that should have been held back but no placeholder could be made | Sent as written | Flagged in red: "sent WITHOUT being replaced" |

Types set to **Auto-tokenise** or **Ignore** never reach Review, and nor does anything on the
shipped list of identifiers that are the same in every tenant. A value that proves its own owner
(an address at a client's mapped domain) may be added to that client straight away instead - see
Settings, [What happens to something new](Settings.md#card-kinds).

You hear about new items through a footer on the model's reply and a Windows toast (both switchable
in [Notifications](Settings.md#card-notifications)), and through the amber "<n> to Review" status.

## Reading a row

| Column | Shows |
|---|---|
| Identifier | The value, whether it was held back or sent, and why it looks like a client's ("carries the name of Contoso", or "one letter from ... in the names of ...") |
| Kind | The type and its confidence |
| Seen | How many times, and from which source |
| Seen with | The clients whose names were in the same requests, with counts, and the project or session |
| Map to | The client to map it to, pre-selected when the owner is clear |

Columns sort by clicking their heading (natural order: vm2 before vm10). The **Filter** box matches
every word you type against the value, kind, clients, projects and sessions, and survives a reload.

## Mapping to a client

Choose a client (or Global) in **Map to** and press **Map**. The value is added to that client with
a token derived from its shape, and the next request carries the token wherever the placeholder was.

If another client already holds the value, the refusal suggests it may be the same in every tenant
and offers **Treat as public**: the value is ignored everywhere and removed from the clients that
held it.

## A new client from the page

Choose **+ New client...** in Map to (or in the bulk bar), fill in id, display name and index, and
**Create and map** creates the client and maps the selected values to it. The **+ New client**
button above the table opens the same dialog without mapping anything, and **Add mapping** beside
it adds one real value and token to any client directly.

## Ignoring

**Ignore** offers two scopes:

- **For <client>** - never proposed again for that client; other clients still see it.
- **Everywhere** - added to Global's ignore list.

The bin icon is different: **Remove from Review** takes the row off the list without ignoring it, so
it comes back if it is seen again.

## Bulk actions and undo

Tick rows (Select all ticks only the rows the filter shows) and use the bar:

| Group | Buttons |
|---|---|
| Who they should belong to | client picker, **Make them theirs** |
| Stop watching for them | **In <client>** (the client chosen in the picker), **Everywhere** |
| Remove | **From Review** |

Every action, single or bulk, reports what was refused and offers **Undo** on its message. Undo puts
the items back in Review exactly as they were listed - count, sources and placeholder - after taking
back what was done (un-mapping or un-ignoring them).

## Quiet kinds and "Not now"

When one type has been ignored 20 or more times and kept no more than 1 time in 10, Review offers:

- **Tokenise <type> automatically** - moves that type to Auto-tokenise: every find is still
  replaced, but without asking you. Undo is on the message, and [What happens to something new](Settings.md#card-kinds) can turn it back.
- **Not now** - hides the offer until that type has been ignored 50 more times, without changing
  anything else.

## Public values

Identifiers that are the same in every tenant (built-in role and policy ids, Microsoft first-party
application ids, vendor hosts) ship with aicleaner and are never queued. The Settings card
[Identifiers that belong to nobody](Settings.md#card-public-list) shows the list and what it has suppressed since the proxy
started. **Treat as public** in Review is how you add your own: it goes on Global's ignore list.

## Added by someone else

When a client is shared (see [Sharing](Sharing.md)), every entry records who added it - the
signed-in work account (UPN), or `DOMAIN\user`. Review shows a card listing entries a colleague
added to a client you share that you have not yet accepted:

| Column | Shows |
|---|---|
| Client, Value, Token | The entry |
| Added by | The colleague's account |

- **Keep** - your answer that the entry is right. Remembered on this machine only
  (`kept-additions.json`); the shared file is unchanged, so each teammate decides for themselves.
- **Remove** - deletes the entry from the shared file, **for everyone**.

Entries with no recorded author (added before authors were recorded) and Global's entries are never
listed. The reason for the card: a shared file is only as trustworthy as its least careful editor,
and an ordinary word mapped as a client rewrites everyone's traffic.

## When the list is empty

The page still shows the kind offers, the "Added by someone else" card, and the **+ New client** and
**Add mapping** controls, with "Nothing to review."

---

[Home](Home.md) > Review
