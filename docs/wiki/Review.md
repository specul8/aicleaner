# Review

[Home](Home.md) > Review

Review is the inbox for identifiers AI Cleaner saw in outgoing traffic that the mapping does not
know yet. Each one is a **suggestion**, and you decide what it is: a topic's, nobody's, or not
worth keeping.

## What lands here

With learning mode on (Settings > [Notifications](Settings.md#card-notifications)), every outgoing request is checked after
tokenising. Anything still identifier-shaped is, by definition, not in the mapping:

| Found | What happens to it in the request | In Review |
|---|---|---|
| A type set to **Tokenise & Review** (by default email, tenant, SharePoint, domain, DevOps organisation and project, and resource names carrying a topic's name) | Held back behind a temporary **token** (stable, of the same shape, on a reserved `.invalid` domain or a documentation IP range) and swapped back in replies | Listed as "held back as `<token>`" |
| A low-confidence find, such as a recurring capitalised phrase | Sent as written | Listed as "sent as-is" |
| A find that should have been held back but no token could be made | Sent as written | Flagged in red: "sent WITHOUT being replaced" |

Types set to **Auto-tokenise** or **Ignore** never reach Review, and nor does anything on the
shipped list of identifiers that are the same in every tenant. A value that proves its own owner
(an address at a topic's mapped domain) may be added to that topic straight away instead - see
Settings, [New names found](Settings.md#card-kinds).

You hear about new suggestions through a footer on the model's reply and a Windows notification
(both switchable in [Notifications](Settings.md#card-notifications)), and through the amber "<n> to
Review" status.

## Reading a suggestion

| Column | Shows |
|---|---|
| Identifier | The value, whether it was held back or sent, and why it looks like a topic's ("carries the name of Contoso", or "one letter from ... in the names of ...") |
| Kind | The type and its confidence |
| Seen | How many times, and from which source |
| Seen with | The topics whose terms were in the same requests, with counts, and the project or AI session |
| Map to | The topic to map it to, pre-selected when the owner is clear |

Columns sort by clicking their heading (natural order: vm2 before vm10). The **Filter** box matches
every word you type against the value, kind, topics, projects and sessions, and survives a reload.

## Mapping to a topic

Choose a topic (or Global) in **Map to** and press **Map**. The value is added to that topic as a
term with a token derived from its shape, and the next request carries that token wherever the
held-back token was.

If another topic already holds the value, the refusal suggests it may be the same in every tenant
and offers **Treat as public**: the value is ignored everywhere and removed from the topics that
held it.

## A new topic from the page

Choose **+ New topic...** in Map to (or in the bulk bar), fill in id, display name and index, and
**Create and map** creates the topic and maps the selected values to it. The **+ New topic**
button above the table opens the same dialog without mapping anything, and **Add mapping** beside
it adds one real term and token to any topic directly.

## Ignoring

**Ignore** offers two scopes:

- **For <topic>** - never suggested again for that topic; other topics still see it.
- **Everywhere** - added to Global's ignore list.

The bin icon is different: **Remove from Review** takes the suggestion off the list without ignoring
it, so it comes back if it is seen again.

## Bulk actions and undo

Tick suggestions (Select all ticks only the ones the filter shows) and use the bar:

| Group | Buttons |
|---|---|
| Who they should belong to | topic picker, **Make them theirs** |
| Stop watching for them | **In <topic>** (the topic chosen in the picker), **Everywhere** |
| Remove | **From Review** |

Every action, single or bulk, reports what was refused and offers **Undo** on its message. Undo puts
the suggestions back in Review exactly as they were listed - count, sources and held-back token -
after taking back what was done (un-mapping or un-ignoring them).

## Quiet kinds and "Not now"

When one type has been ignored 20 or more times and kept no more than 1 time in 10, Review offers:

- **Tokenise <type> automatically** - moves that type to Auto-tokenise: every find is still
  replaced, but without asking you. Undo is on the message, and [New names found](Settings.md#card-kinds) can turn it back.
- **Not now** - hides the offer until that type has been ignored 50 more times, without changing
  anything else.

## Public values

Identifiers that are the same in every tenant (built-in role and policy ids, Microsoft first-party
application ids, vendor hosts) ship with AI Cleaner and are never queued. The Settings card
[Shared identifiers](Settings.md#card-public-list) shows the list and what it has suppressed since the proxy
started. **Treat as public** in Review is how you add your own: it goes on Global's ignore list.

## Added by someone else

When a topic is shared (see [Sharing](Sharing.md)), every term records who added it - the
signed-in work account (UPN), or `DOMAIN\user`. Review shows a card listing terms a colleague
added to a topic you share that you have not yet accepted:

| Column | Shows |
|---|---|
| Topic, Real, Token | The term |
| Added by | The colleague's account |

- **Keep** - your answer that the term is right. Remembered on this machine only
  (`kept-additions.json`); the shared file is unchanged, so each teammate decides for themselves.
- **Remove** - deletes the term from the shared file, **for everyone**.

Terms with no recorded author (added before authors were recorded) and Global's terms are never
listed. The reason for the card: a shared file is only as trustworthy as its least careful editor,
and an ordinary word mapped into a topic rewrites everyone's traffic.

## When the list is empty

The page still shows the kind offers, the "Added by someone else" card, and the **+ New topic** and
**Add mapping** controls, with "Nothing to review."

---

[Home](Home.md) > Review
