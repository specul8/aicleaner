# Project Sessions

[Home](Home.md) > Project Sessions

The **Project Sessions** page drives Claude Code from the browser. Each session runs the same
Claude Code CLI on this machine, in headless streaming mode, with `ANTHROPIC_BASE_URL` pointing back
at the proxy - so a prompt typed here is tokenised on the way out and the reply rehydrated on the way
back, exactly as in a terminal (see [How it works](How-it-works.md)). It works the same from your
desk or from another device through the tunnel (see [Remote access](Remote-access.md)).

The page is off until asked for: Settings, [Session page](Settings.md#card-session) (`session.enabled`). Switching it off
stops every session running from it. If the CLI cannot be found, the page says so.

## Project profiles

Work is organised by engagement, not by folder. A **project profile** is one engagement, and every
session is named after its profile. Profiles are edited on the page itself (**Edit project
profiles**, a tab per profile) and stored in `profiles.json` beside `_global.json` in the app
folder - never in the shared client folder, because a profile names folders on *this* machine.

| Field | What it does |
|---|---|
| Name | How the session is known everywhere: the picker, the menu, notices, the page address. Up to 40 characters; no slash, `?`, `#` or `%`. Locked while its session runs |
| Client | The client the work is for, or **None (Global only)**. Shown as the client's label, never its id |
| Workspace files | `.code-workspace` files, one per line. Their folders make up the file tree; the first sets the home folder |
| Folders added by hand | Extra folders, one per line, chosen with the folder picker |
| Home folder | Where sessions start. Derived from the first workspace file (or the first folder); you can choose another, and Reset goes back to the derived one |
| OneDrive share | The client's share folder, saved per client on this machine (`client-shares.json`), not with the profile |
| Model | A drop-down of known models, **Default** (Claude Code's own choice), or **Other...** to type a name |
| Standing brief | Text sent with the first prompt of each session, for example "Landing zone work for Contoso. Terraform only." |

Folders must sit inside the allowed folder locations (`folders.roots`, set in
[Folders the pages can open](Settings.md#card-folders)). A profile naming a client that is not in the mapping is refused when you
save it. Edits to a profile apply from the session's next start.

**Naming a session does not narrow the filter.** Every client's names are replaced in every
session; a Contoso session that mentions Fabrikam still has Fabrikam replaced.

## Starting, continuing and stopping

Pick a project in **Pick a Project to chat about...**, then use the session card:

- **Start** launches Claude Code in the home folder. Every other folder of the profile, and the
  client's share, is passed with `--add-dir`, so Claude can reach them (prompts still apply).
- **Continue** lists earlier conversations held in the profile's folders, newest first, each shown
  by its first prompt. Choosing one starts with `--resume`. A tick-box also lists conversations
  started by programs (automated reviews). A transcript written to in the last few minutes is
  flagged as possibly open elsewhere.
- **Start chatting** scrolls down to the prompt box of a running session.
- **Stop** ends the session: its process, context and permission grants go.
- **Escape** (or **Interrupt**, shown beside a slow request) stops the current turn and keeps the
  session.

Up to `session.max_running` sessions (default 4) run at once, each with its own process,
transcript and permission queue. Running sessions are listed under Project Sessions in the menu,
each linking straight to its chat.

In the prompt box, Enter sends, Shift+Enter or Ctrl+Enter adds a line, Up and Down walk your
earlier prompts, and `/` opens the list of commands that work in headless mode (the project's,
yours, plugins', and built-ins such as `/model`).

### It survives a proxy restart

The proxy does not own Claude Code's process. A small **broker** (`aicleaner.broker`) is started
detached from the proxy; it holds the CLI's input and output, numbers every line, and keeps them in
`%USERPROFILE%\.aicleaner\sessions\`. When the proxy restarts - every update does - it finds the
broker, reconnects and catches up, so the page keeps the session. Pressing Start on a conversation
that is already running reconnects to it rather than starting a second copy.

## Permission modes and approvals

The **Asks** control sets how the session asks before acting. It can be changed while a session is
running, and the choice is remembered as the default for the next one (`session.permission_mode`).

| Mode | Shown as | Meaning |
|---|---|---|
| `manual` | Ask every time | Every command waits for you (default) |
| `auto` | Judge each action (sent to Anthropic) | Each action and its context go to Anthropic to be judged |
| `acceptEdits` | Accept file edits, ask for the rest | Edits go through; commands still ask |
| `dontAsk` | Do not ask | Nothing waits for you |
| `bypassPermissions` | Skip all permission checks | No checks at all. Least safe |
| `plan` | Plan only, change nothing | It can read and plan, but not act |

Prompts reach the page through a small local MCP tool (`mcp_approve.py`) with a token new for each
session. Each request appears as an **approval card** - "<session>: allow <tool>?", the tool's
details, **Allow** and **Deny**. A card waits as long as it takes unless a timeout is configured. If
no approval host is available, anything that would prompt is denied.

### Questions

When Claude asks a question (its AskUserQuestion tool) it appears as a **Claude is asking** card:
each option as a radio button or tick-box, plus **Other** with a box for your own words. **Answer**
sends the choices; **Decline** refuses. If a question closed before your answer arrived, the answer
is put back in the prompt box, ready to send.

Cards are shown only on their own session's page; another session waiting on you shows in the
menu and as a notice.

## The conversation

Replies are drawn as Markdown, with every name the proxy restored shown in **green** - the AI saw a
token there. Each message has a date and time; Claude's thinking shows as a collapsed line above the
reply; long tool output can be expanded; tools running in the background are listed. A light beside
**Send** says whether the page is connected, and a line under it says what the AI service request
is doing (sent, replying, or waiting for N minutes).

**Show earlier** loads up to 500 earlier items of the conversation from its transcript. Beyond that,
ask Claude to look through the transcript itself. **View latest** returns to the bottom and counts
new items while you are scrolled up.

### Autocorrect

Autocorrect, switched on in Settings > [Prompt autocorrect](Settings.md#card-autocorrect), corrects
spelling in the prompt as you type, from the moment it is switched on - never what is already typed.
It is off until you switch it on. At the next space or punctuation a misspelt word is replaced by the
closest match (a word starting with the same letter first, then the commonest) and shown in green;
click or tap it for a bubble with **Revert**, other suggestions, and **Add to my words** - which keeps
what you typed and never corrects it again. While it is on, right-click any word to add it; with text
selected or Shift held, right-click gives the browser's own menu.
Paths, code-like names (`myVar`) and anything in backticks are left alone; your own words keep their
spelling (powershell becomes PowerShell), and so do days, months and common product names
(monday becomes Monday, devops becomes DevOps). Ctrl+Z undoes a correction. Options are in
Settings > [Prompt autocorrect](Settings.md#card-autocorrect).

## Files

The file tree (toggle with **Files**) shows the profile's folders, with the client's OneDrive share
pinned at the top. Clicking a file offers:

- **Open in a tab** - beside the conversation tab. Text and code are editable with autosave; a save
  is refused if the file changed on disk since it was opened, offering Reload, Overwrite, or copy
  yours then reload. Markdown has a preview.
- **Download** - for Word, PowerPoint and anything a tab cannot show.
- **Put the path in the prompt**.

Right-click opens the same menu without opening or closing a folder. A folder's menu also has
**View files (N)** - each of its own files that a tab can show - and **Download (zip)**, the folder
and everything in it.

**Several at once:** Shift+click a second item in the same folder to select both and everything
between; Ctrl+click adds or removes one item, in any folder. The menu then acts on them all:
**Prompt** puts every path in, one per line; **View** opens each that a tab can show (asking first
above 20); **Download** fetches up to five files one by one, and more - or any folder - as one zip
in which each file keeps its folders (`docs/wiki/Settings.md`; with items from two project folders,
each path starts with its project folder's name). Escape clears the selection.

Files are shown as they are on disk, real names included: they go to you, not to the AI service.

### Editing files

Open tabs are remembered per session, so a refresh - or another device - brings them back.

In a Markdown file's **Preview**, double-click a word to edit it: the tab switches to **Source** with
that same word selected (the occurrence you clicked, not just the first). The first time, it asks
whether you want that or would rather keep double-click for selecting text, with "Don't ask me
again"; change it later in Settings > [File editor](Settings.md#card-editor).

PowerShell, Terraform/HCL, Python, JSON, YAML and Markdown files are coloured as you read and edit
them, and the editor offers suggestions as you type (Ctrl+Space to ask): the language's snippets,
keywords and built-ins, and every name already in the file. Tab or Enter accepts, Esc closes, and
Ctrl+Z takes an accepted suggestion back like typing. A snippet's placeholders are reached with Tab.
PowerShell suggestions are the commands installed on this machine - every module at user and machine
scope, Az and Graph included - with a command's parameters offered after it; a module installed
later is picked up within five minutes of editing, or at once on a restart. Tab and Shift+Tab indent
and outdent, Enter keeps the indentation, and brackets and quotes close themselves. Other files are
plain text, as before.

### Searching by name

Above the file tree are two buttons, **Files** and **Search**; the highlighted one is the view you
are looking at. **Search** opens a search box. After two characters it
searches every folder the tree shows, the share included, for files and folders whose names contain
what you typed (any case), while "Searching..." pulses. Matches are listed with their full paths and
the same Prompt / View / Download menu. Folders that cannot be read, and folders of installed
dependencies or tools - `node_modules`, Python virtual environments, `.terraform`, `.git` and the
like - are skipped, and the status line says how many. A search that stops early says why, in bold:
**Result Limit Reached:** (500 matches - type more of the name to narrow it) or **Search Timeout
Reached:** (30 seconds, not every folder searched yet), with **Keep searching** to carry on with the
folders not yet searched. **Refresh** beside the two buttons runs the search again from the start.
Clicking **Files** closes the file and folder search and returns the tree view to how it was before
the search; the query is kept, and runs again when you come back to **Search**.

## Notices

Work that finishes while you are not looking at it is announced: a toast on whatever page is open,
and an entry in the status stack that stays until you visit it.

| Notice | When |
|---|---|
| `<session>: Claude has finished` | A turn ended in a session you are not viewing |
| `<session> is waiting on you` | A permission card or question arrived |

**Open** on a notice goes straight to that session's chat. Visiting the session clears both the
entry and its toast.

## Worktree sessions

Two Claude Codes in one working tree commit over each other, so a project has one session at a
time. For a second, use a git **worktree**: a second checkout of the same repo on its own branch.

1. On a project's session card, choose **Create a worktree**.
2. Pick one of the profile's repos (**Branch from**) - or a plain folder outside OneDrive, which is
   made a git repo with a first commit - and keep or change the name (default `wt-1`, `wt-2`...).
3. **Create** runs `git worktree add` into `<repo>.worktrees\<name>`, beside the repo, on a branch
   of the same name, and switches to the new session, named `<profile> @ <name>` (for example
   `Contoso @ wt-1`).

A worktree session uses the profile's client, model and brief, but its only folder is the worktree:
it cannot write to the original repo. It is saved with its profile and appears in the picker like
any other session.

When the worktree session is stopped, its card offers:

- **Merge** - merges the branch into the repo's current branch. Refused if either side has
  uncommitted changes; a conflict is reported and the merge undone, nothing merged.
- **Remove** - removes the worktree, and its branch if merged. A branch with unmerged commits is
  kept. The session is removed and the page returns to the parent profile.

## See also

- [Remote access](Remote-access.md) - driving sessions from another device
- [Settings](Settings.md) - `session.*`, allowed folders, restored-name colouring
- [Troubleshooting](Troubleshooting.md)

---

[Home](Home.md) > Project Sessions
