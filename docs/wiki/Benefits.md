# Benefits

[Home](Home.md) > Benefits

## What you get

- **Client names stay on your machine.** The AI service sees tokens. Files on disk, git history and
  your terminal keep the real terms, because the assistant's file writes and commands are
  rehydrated locally.
- **You work normally.** No special syntax and no copying text through a scrubber: you prompt,
  read and run as usual. Replies show the real terms - marked in green in the Project Sessions page,
  so you can see what was swapped.
- **Evidence.** One log line per request records how many substitutions were made and whether the
  canary passed - never the content. A separate local detail log records which terms were
  replaced, for auditing a request afterwards.
- **It fails closed.** A request that cannot be inspected, a known real term that survived, or a
  topic file that is broken with no good copy stops traffic instead of letting it through.
- **It learns.** Identifiers it has not seen are held back behind tokens and offered in
  [Review](Review.md) as suggestions, where one click adds them to a topic or ignores them. Values
  that prove their own owner (an address at a topic's domain) can be added automatically.
- **A browser front end for Claude Code.** The [Project Sessions](Project-Sessions.md) page runs
  Claude Code sessions per project, with the conversation, files, questions and approvals in
  one place - from your desk, or from another device through the encrypted tunnel.
- **Team sharing.** Each topic is a folder you can share in OneDrive with exactly the people on
  that engagement; a key per topic stops changes made outside AI Cleaner from being trusted.

## What it costs

- Every request passes through one more local hop (normally milliseconds).
- You decide what new identifiers are, in Review. The first days on a new topic produce the most.
- It covers what it can see: tools not pointed at it, and identifiers not in the mapping, are not
  protected.

---

[Home](Home.md) > Benefits
