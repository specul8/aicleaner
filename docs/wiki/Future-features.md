# Future features

[Home](Home.md) > Future features

What is planned, parked or partly built, and not yet delivered. The full register, with the
reasoning and what was already decided for each, is kept with the project's development
records (FUTURE-FEATURES.md); the numbers below are its entry numbers. Delivered,
retired and withdrawn entries are not listed here.

Status words:

- **Open** - not built yet (some are waiting on a decision, recorded in `DECISIONS.md`).
- **Part built** - some of it shipped; the section says what remains.
- **Parked** - deferred on purpose, not dropped.
- **v1.1** - scheduled for the next minor release.

## Other assistants and services

| # | Area | What | Status |
|---|---|---|---|
| 1 | security | Self-hosted remote control for agents other than Claude Code (Copilot CLI, Codex) - done for Claude Code via [Project Sessions](Project-Sessions.md) | Open |
| 5 | proxy | Copilot Chat through the proxy (bring-your-own-key); the OpenAI Chat dialect is built and dormant | v1.1 |
| 6 | proxy | The OpenAI Responses API dialect, for Codex CLI in API-key mode | Open |
| 8 | proxy | Claude hosted in Microsoft Foundry (Azure) as an upstream | Open |

## Detection and scanning

| # | Area | What | Status |
|---|---|---|---|
| 2 | scan | A blocking mode that refuses a request carrying an unmapped high-confidence identifier (mostly superseded by placeholders) | Open |
| 3 | proxy | A Claude Code hook layer: deny reading PDFs and images, tokenise tool output, check prompts before they are sent | Open |
| 7 | scan | A local Presidio pre-pass for person names, phone numbers and other personal data in documents | Open |
| 26 | proxy | Parse multipart requests instead of exempting them - the refusal is now loud; the parser waits for a need | Part built |

## Security and sign-in

| # | Area | What | Status |
|---|---|---|---|
| 20 | security | Per-user sign-in to the local gateway (OIDC) for Claude Desktop | Parked (with Claude Desktop) |
| 28 | security | Remote sign-in policy from an identity rule (an Entra group), and listing a tenant's own providers - the providers themselves are delivered | Open (mostly delivered) |
| 70 | security | Stop Claude itself changing a setting that is blocked remotely | Parked to v1.1 |

## Install and deployment

| # | Area | What | Status |
|---|---|---|---|
| 11 | install | Organisation-wide deployment: a shared, versioned mapping store, central evidence logs, an Intune package | Open |
| 14 | install | A versioned data-migration template, built when the client file's structure next changes | Parked |
| 19 | install | Claude Desktop set up without an elevated PowerShell | Parked (with Claude Desktop) |

## Platforms

| # | Area | What | Status |
|---|---|---|---|
| 12 | platform | macOS (and Safari): Keychain, launchd, shell equivalents of the scripts | v1.1 |
| 18 | platform | aicleaner on an Android phone - today the answer is driving a filtered machine through [Remote access](Remote-access.md) | Open |

## Performance

| # | Area | What | Status |
|---|---|---|---|
| 25 | perf | Pipeline optimisation, points 2 and 3 (point 1 delivered); to be measured end to end first | Open |
| 59 | perf | A local index in front of the client files - the files stay the source of truth | Open |

## Interface

| # | Area | What | Status |
|---|---|---|---|

---

[Home](Home.md) > Future features
