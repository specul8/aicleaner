# Project Sessions

[Home](Home.md) > Project Sessions

The **Project Sessions** page drives Claude Code from the browser. Each AI session runs the same
Claude Code CLI on this machine, in headless streaming mode, with `ANTHROPIC_BASE_URL` pointing back
at the proxy - so a prompt typed here is tokenised on the way out and the reply rehydrated on the way
back, exactly as in a terminal (see [How it works](How-it-works.md)). It works the same from your
desk or from another device through the tunnel (see [Remote access](Remote-access.md)).

The page is off until asked for: Settings, [Session page](Settings.md#card-session) (`session.enabled`). Switching it off
stops every AI session running from it. If the CLI cannot be found, the page says so.

## Project sessions

Work is organised by engagement, not by folder. A **project session** is one engagement, and every
AI session is named after its project session. Project sessions are edited on the page itself and
stored in `profiles.json` beside `_global.json` in the app folder - never in the shared topic folder,
because a project session names folders on *this* machine.

**Edit project sessions**, in the list on the left, opens **Project sessions** right under the conversation panel, in
the same column and as wide: a tab per project session (and **+**), the open tab's form, **Add a project session**, and
**Delete** / **Save** / **Close**. It opens on the tab you last chose, else the project session on screen. The link then
reads **Close project sessions**; it, or **Close**, closes the panel again, asking first when a tab has unsaved changes.
On a narrow screen the panel sits under the conversation panel.

The editor's controls are in blocks that sit side by side on a wide screen and one under another, in this order, on a
narrow one: **Basics** (Name, Topic, Model); **Folders appearing in the Files panel** (the widest); **Workspace file**
(with the terminal working directory note); **Standing brief**, folded. Any notices, and
**Add a project session** / **Delete** / **Save** / **Close**, run across the bottom. The (i) beside **Folders appearing
in the Files panel** and **Workspace file** says what each is for.

| Field | What it does |
|---|---|
| Name | How the session is known everywhere: the picker, the menu, notifications, the page address. Up to 40 characters; no slash, `?`, `#` or `%`. Locked while its AI session runs |
| Topic | The topic the work is for, or **None (Global only)**. Shown as the topic's label, never its id |
| Model | A drop-down of known models, **Default** (Claude Code's own choice), or **Other...** to type a name |
| Folders appearing in the Files panel | Every folder the Files panel shows, in the order you set - drag one (the handle at its left) or use **Move up** / **Move down** (for the keyboard and touch); **Remove** (x) at its right; **Add a folder** adds one at the end. In front of each, its three role buttons (below). The folder sessions start in is marked **cwd**. With a workspace file, each is also marked **workspace** (in the file) or **added** (not yet: Save writes it in). On a project session with no folders yet, the first folder added that holds a `.code-workspace` file connects that file instead, and the file's folders become the list |
| Workspace file | One `.code-workspace` file per project session: **Connect to a workspace file** chooses it; it shows as a single entry with **Disconnect**. Its folders join the list above. A project session saved with several keeps the first and names the others it dropped (the files themselves are never touched) |
| Standing brief | Text sent with the first prompt of each session, for example "Landing zone work for Contoso. Terraform only." Folded to one line showing its first line; open it to edit |

### Folder roles

Each folder has three toggle buttons in front of it. Hover (or focus) one for what it means; press it to turn it on or
off for that folder. A folder can have any of them, all three, or none.

| Role | What it means |
|---|---|
| **Assets** (Client assets) | The files that cross between you and the client - documents, designs, exports. Usually your OneDrive folder. **Exactly one** per project session: Save refuses until one folder has it. It is the **topic's** folder: every project session of the topic shows it among its folders with Assets on (wherever you put it in the order), and choosing another one here changes it for the topic and all its other project sessions - the editor names them before you save. The topic page shows and changes the same folder. A project session with **Topic: None** keeps its own |
| **Tooling (CWD)** (AI tooling, working folder) | Where the AI agent runs and its commands start; for non-client-facing tooling, operating rules and scripts that deliver the client outcomes. **At most one**. Sessions start here; with none, in the client assets folder |
| **Client code** | Folders that hold the client-facing solution - code and configuration delivered to the client. A label only (it tells Claude what the folder is); any number, including none |
| none | Any other folder - a template repository from another engagement, say: the session can still reach it, and Claude is told it has no role |

Roles never move a folder: the order is yours, in the editor, the Files panel and the workspace file alike. When the
first prompt of a session goes out, Claude is told every folder and its roles, beside the standing brief.

On updating to 1.3.8 each project session kept starting where it did: its home folder became its Tooling (CWD) folder,
and its topic's project assets folder joined its list first, with Assets on. Client code starts off everywhere.

Folders must sit inside the allowed folder locations (`folders.roots`, set in
[Folders the pages can open](Settings.md#card-folders)). A project session naming a topic that is not in the mapping is refused when
you save it. Edits to a project session apply from the AI session's next start.

**The workspace file follows the list.** With a workspace file, saving makes the file list exactly the folders in
the list, in that order - the client assets folder included - so a folder you added by hand, or had before you
connected the file, is written into it too. Its `terminal.integrated.cwd` setting points at the Tooling (CWD) folder
(else the client assets folder), wherever it sits: replaced in the form it already uses (a `${workspaceFolder:<name>}`
reference or a path), or added when that folder is not the file's first. Nothing else in the file changes: comments, other settings,
names and relative paths stay as they were. A copy of the file as it was is kept first in
`%USERPROFILE%\.aicleaner\workspace-backups\` (the newest 10 per file - never beside the file, where a repository
would pick it up). The file is left alone, with a note saying why, when it is read-only, outside the allowed folder
locations, or changed on disk since the page read it (reload the page and save again); sessions still start in the
Tooling (CWD) folder you chose. Without a workspace file, the project session keeps the folders and their order itself.
This works the same through remote access as on the machine itself.

**Naming a session does not narrow the filter.** Every topic's terms are replaced in every
session; a Contoso session that mentions Fabrikam still has Fabrikam replaced.

## Starting, continuing and stopping

Pick a project in **Pick a Project to chat about...**, then use the session card. Its first row has the project
session's name, its topic, the home folder (hover it for the whole path), whether it is running, and **Start** /
**Stop**; the second the conversation to continue, **All** and **Conversation actions**, with **Create a worktree**
under it; the third **Asks**.

- **Start** launches Claude Code in the Tooling (CWD) folder. Every other folder of the project session is passed
  with `--add-dir`, so Claude can reach them (prompts still apply). A project session with **no Tooling folder** starts
  in its client assets folder instead - for design and documentation work, where there is no repository. The editor
  says where sessions start under the folder list, and the session card shows it. With no folder at all, it cannot
  start.
- **Continue** (the drop-down) lists earlier conversations held in the project session's folders, newest first, each
  shown by its title or first prompt. Choosing one starts with `--resume`. The **All** tick (Show all conversations)
  lists every one, not just the newest 25. When there are older ones, the list ends with a line that cannot be chosen:
  "Latest 25 conversations shown - to see older ones, tick All beside this list and reopen it" ("Latest 25 per folder"
  when the list is grouped by folder); it is not there with All ticked, or when there are 25 or fewer. A transcript
  written to in the last few minutes is flagged as possibly open elsewhere.
- **Conversation actions** opens a panel from the right edge of the window, over the page (nothing under it moves).
  Each action has a line saying what it does: **Search conversations** (also Ctrl+K) and **Suggest titles**;
  **Rename**, **Retitle**, **Move to another session** and **New conversation from existing ones**; then **Recently
  deleted** and **Delete**. Choosing one closes the panel. Escape, the close button or a click outside it close it
  too; the arrow keys move through it.
- Start also scrolls down to the conversation; so does opening a running session from the menu or a notice's
  **Open**. (The separate **Start chatting** button went in 1.2.4.)
- **Stop** ends the AI session: its process, context and permission grants go.
- **Escape** (or **Interrupt**, shown beside a slow request) stops the current turn and keeps the
  session.

Up to `session.max_running` AI sessions (default 4) run at once, each with its own process,
transcript and permission queue. Running AI sessions are listed under Project Sessions in the menu,
each linking straight to its conversation.

In the prompt box, Enter sends, Shift+Enter or Ctrl+Enter adds a line, Up and Down walk your
earlier prompts, and `/` opens the list of commands that work in headless mode (the project's,
yours, plugins', and built-ins such as `/model`).

### It survives a proxy restart

The proxy does not own Claude Code's process. A small **broker** (`aicleaner.broker`) is started
detached from the proxy; it holds the CLI's input and output, numbers every line, and keeps them in
`%USERPROFILE%\.aicleaner\sessions\`. When the proxy restarts - every update does - it finds the
broker, reconnects and catches up, so the page keeps the session. Pressing Start on a conversation
that is already running reconnects to it rather than starting a second copy.

## Git and OneDrive

A Git repository does not belong in OneDrive. It is thousands of small files that change together; OneDrive syncs them
one by one, keeps both copies when two machines change the same one, can sync half of a change (a corrupted repository:
"bad object", "missing blob"), and locks files Git is trying to write. Your remote (GitHub, Azure DevOps) is a
repository's backup; OneDrive is for documents.

So **Start** checks every folder of the project session that is in OneDrive - the client assets folder, and any
other folder kept there - for Git: a `.git` folder or file anywhere inside it, at any depth (links and junctions are not followed). A `.git`
folder counts only when it holds a `HEAD` file (a `.git` file only when it starts with `gitdir:`). An empty `.git`
folder tree - what OneDrive often leaves behind after a repository is moved out - is not Git: it never stops Start, and
the dialog and the editor list it as "Empty Git folders left behind - safe to delete". The check takes at most about 2 seconds; when
it cannot finish in that time it says so, and does not treat the folder as clean. A folder that can be checked in
full in half a second is checked in full every time. For a larger one, AI Cleaner keeps a list of its folders (in its
own state folder, not in OneDrive), and Start looks again only at the folders created or changed on this computer
since, so it does not wait. Something OneDrive brings in from another device does not show as a change, so each of
those quick checks is followed by a full check in the background; Git that arrived that way is caught as soon as it
finishes, at the latest by the next Start. When it finds Git, the session does not start and a dialog asks what to do,
naming each folder that holds Git:

- **Yes & Migrate Repo** - choose a folder outside OneDrive (one of the allowed folder locations; a OneDrive folder is
  refused). AI Cleaner first shows the plan: each repository goes to the same place under the chosen folder, with its
  `.git` folder, every file Git tracks (uncommitted changes included) and every file `.gitignore` covers; untracked
  files - never added to the repository - stay in OneDrive. If anything is already where the move would write (a file
  of the same name, a folder that already holds files, another repository), it lists it and offers **Choose another
  folder** or **Move into a new folder** - it never overwrites or merges. **Move the repositories** copies everything,
  checks the copies (the same commit and changes, and `git fsck` clean), and only then removes the originals from
  OneDrive; if anything fails along the way, everything is put back as it was. In each repository's old folder it
  leaves **where did my files go.md**, saying what moved where, why, and the exact command to move it back. The new
  folder takes the old one's place in the project session (with its roles) when the repository was one of its folders,
  or joins it with no role, and its workspace file follows. With Git in more than one folder, the repositories of the
  first are moved; Start then checks again and asks about the next. A log of every
  step is kept in `%USERPROFILE%\.aicleaner\logs\git-migrate\`. Then **Start the session**.
  When every allowed folder location is in OneDrive, there is nowhere to move to: the dialog says so and offers
  **Ignore this time** or **Cancel**, with **Don't remind me again for this project**.
- **No, but remind me next time** - starts this time; the next Start asks again.
- **No, Don't remind me again (this project)** - starts, and this project session is not checked again. The editor's
  folder list then says "Git check: off for this project session", with **Turn it back on**.
- **Cancel** - nothing starts.

The editor's folder list also warns, without stopping anything, when one of its OneDrive folders holds Git. A script
that starts a session through the API (a scheduled run) can send `"git_onedrive": "once"` to skip the check for that
start. AI Cleaner never offers `git init` in the project assets folder.

## Permission modes and approvals

The **Asks** control sets how the session asks before acting. It can be changed while a session is
running, and the choice is remembered as the default for the next one (`session.permission_mode`). The **(i)** beside
it says what the chosen mode means - hover it, tab to it, or tap it.

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
menu and as a notification.

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

The file tree (toggle with **Files**) shows the project session's folders in the order you set, each once; the client
assets folder is marked "(client assets)" and the folder sessions start in "(this session)".

**Reordering the folders.** Drag a project folder's name up or down among the others: a line shows the gap it will
land in (it never drops *into* a folder, and it is not the drag that copies files or saves them to your device).
From the keyboard, its right-click menu has **Move up** and **Move down**. The new order is written to the workspace
file (or kept in the project session, with none), as when you save the list above. Moving a folder changes only the
order: where sessions start is the Tooling (CWD) role's, wherever that folder sits.

The mouse works as in File Explorer:

- **Click** chooses a file or folder (a folder also opens or closes).
- **Click a chosen name again**, slowly (not a double-click), to rename it in place: **Enter** or clicking
  anywhere else renames it, **Escape** cancels, and a name Windows would refuse says why and stays open to
  correct. The project folders themselves cannot be renamed.
- **Double-click a file** to open it: a text file (or an empty one) opens in a tab, ready to edit; anything
  else - a Word document, a zip, a picture - downloads, so the device you are using opens it with its own
  program. A Markdown file opens in **Preview**; **Source** is a click away. Double-click a folder to open
  or close it.
- **Right-click** (on a tablet, press and hold; on a keyboard, the menu key or Shift+F10) for the menu, one
  action per line: **Prompt** (the path into the prompt), **Read text** / **Attach**, **View** (open in a
  tab; text and code are editable with autosave, and a save is refused if the file changed on disk since it
  was opened, offering Reload, Overwrite, or copy yours then reload), **Download**, and then **New file**
  (a folder's menu), **Rename** and **Delete** (to the Recycle Bin, asking first). Arrow keys move through
  it, Enter runs an item, Escape closes it.

A file tab's heading has two lines: the file's path (with **Source** / **Preview** for Markdown) and, at the right
end, whether your edits are saved; beneath it, small, its size and when it was created and last modified. There is
no Reload or Download there: a file changed on disk is read again by itself (unless you have unsaved edits, when the
bar above the editor offers Reload, Overwrite or a copy), and Download is in the file's right-click menu.

After AI Cleaner is updated, a page that was already open keeps running the version it loaded until it is reloaded;
it says so ("AI Cleaner has been updated to ...") with a **Reload** button.

**Files Claude asks you to read.** When a reply asks you to read or review a Markdown file Claude has just
written ("please review spec.md", "spec.md is ready for your review", "have a look at spec.md"), the file opens
in Preview in a tab behind the one in front, and the menu bar and a notification say "<session>: Claude has opened
<file> for you to review". Nothing comes to the front by itself; clicking it (or the notification's **Open**)
brings that file's tab forward. A file Claude only writes or mentions raises nothing. In [Settings](Settings.md#card-session),
**Open files for review** turns this off.

Right-click never opens or closes a folder. A folder's menu also has
**View files (N)** - each of its own files that a tab can show - and **Download (zip)**, the folder
and everything in it.

**Several at once:** Shift+click a second item in the same folder to select both and everything
between; Ctrl+click adds or removes one item, in any folder. The menu then acts on them all:
**Prompt** puts every path in, one per line; **View** opens each that a tab can show (asking first
above 20); **Download** fetches up to five files one by one, and more - or any folder - as one zip
in which each file keeps its folders (`docs/wiki/Settings.md`; with items from two project folders,
each path starts with its project folder's name). When Download would fetch them one by one, **Download
selected (.zip)** under it makes them one zip instead. Escape clears the selection.

**Dragging out.** In Edge or Chrome, drag a file from the tree onto your desktop or a folder of the device you are
using to save it there; a folder is saved as its zip, and dragging any of several selected items saves them all as one
zip (`files-<date>-<time>.zip`). Onto the conversation the same drag opens them in tabs, and onto the prompt it puts
their paths in. Through the encrypted tunnel a drag cannot save; use the menu's **Download**. Dragging a project
folder's own name moves it among the others (above), whatever is selected.

Files are shown as they are on disk, real terms included: they go to you, not to the AI service.

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
searches every folder the tree shows, the project assets folder included, for files and folders whose names contain
what you typed (any case), while "Searching..." pulses. Matches are listed with their full paths and
the same Prompt / View / Download menu. Folders that cannot be read, and folders of installed
dependencies or tools - `node_modules`, Python virtual environments, `.terraform`, `.git` and the
like - are skipped, and the status line says how many. A search that stops early says why, in bold:
**Result Limit Reached:** (500 matches - type more of the name to narrow it) or **Search Timeout
Reached:** (30 seconds, not every folder searched yet), with **Keep searching** to carry on with the
folders not yet searched. **Refresh** beside the two buttons runs the search again from the start.
Clicking **Files** closes the file and folder search and returns the tree view to how it was before
the search; the query is kept, and runs again when you come back to **Search**.

## Notifications

Work that finishes while you are not looking at it is announced: a notification on whatever page is
open, and an entry in the status stack that stays until you visit it.

| Notification | When |
|---|---|
| `<session>: Claude has finished` | A turn ended in a session you are not viewing |
| `<session> is waiting on you` | A permission card or question arrived |

**Open** on a notification goes straight to that session's conversation. Visiting the session
clears both the entry and its notification.

## Alerts and scheduled runs

An **alert** is raised by a script or a session on the AI Cleaner machine - for example "Daily close needs
you", with a summary of what a job found. It shows in the status stack on every page and every device
(remote access included), with a notification when it arrives, and it stays until someone presses **Dismiss** on
the **Alerts** page. Dismissing it on one device clears it everywhere. Alerts are kept across restarts.
An alert names the session to talk it over in: **Open** goes straight to that session's conversation.

Alerts are raised only from the AI Cleaner machine; through remote access they can be read and
dismissed, never raised.

`installer\Invoke-AiCleanerSession.ps1` (in the AI Cleaner install folder) does three things:

| Use | Command |
|---|---|
| Send a prompt to a session | `.\Invoke-AiCleanerSession.ps1 -Session "Contoso" -Prompt "run the due jobs" -Title "Scheduled - {date}" -NewConversation` |
| Raise an alert | `.\Invoke-AiCleanerSession.ps1 -Alert -AlertTitle "Daily close needs you" -SummaryFile .\summary.txt -Session "Contoso" -Key daily-close-2026-11-04` |
| Dismiss an alert | `.\Invoke-AiCleanerSession.ps1 -Dismiss -Key daily-close-2026-11-04` |
| Run a prompt every day | `.\Invoke-AiCleanerSession.ps1 -RegisterTask -Session "Contoso" -Prompt "run the due jobs" -Title "Scheduled - {date}" -NewConversation -At 18:00` |

When it sends a prompt:

- **A stopped session** is started on a new conversation (titled `-Title`; `{date}` becomes the date) and
  sent the prompt. It runs with the project session's own topic, filtering and permission mode, as if you had
  pressed Start.
- **A session in use** - a turn running, a question waiting, prompts queued, or anything in the last 30
  minutes (`-IdleMinutes`) - gets the prompt queued in the conversation that is open. A conversation you
  are in is never switched away.
- **An idle session** gets a new conversation first with `-NewConversation`; without it, the prompt goes
  into the open conversation.
- **A run that cannot start the session** (the cap on sessions running at once, memory nearly full)
  raises an amber alert saying why.

A session started this way can still ask you things: its questions wait on the Session page, with the
"waiting on you" notification, until you answer - there is no time limit. While a question waits, the rest of
that turn waits too.

`-RegisterTask` creates one Windows Task Scheduler task for your user (no administrator rights), daily at
`-At`, run when you are signed in; a run missed while the machine was off or asleep happens when it is
next up. Running it again with the same settings changes nothing; `-UnregisterTask` removes it.

## Worktree sessions

Two Claude Codes in one working tree commit over each other, so a project has one session at a
time. For a second, use a git **worktree**: a second checkout of the same repo on its own branch.

1. On a project's session card, choose **Create a worktree** (under **Conversation actions**; hover it for what a
   worktree is for).
2. Pick one of the project session's repos (**Branch from**) - or a plain folder outside OneDrive, which is
   made a git repo with a first commit - and keep or change the name (default `wt-1`, `wt-2`...).
3. **Create** runs `git worktree add` into `<repo>.worktrees\<name>`, beside the repo, on a branch
   of the same name, and switches to the new session, named `<project session> @ <name>` (for example
   `Contoso @ wt-1`).

A worktree session uses the project session's topic, model and brief, but its only folder is the
worktree: it cannot write to the original repo. It is saved with its project session and appears in the picker like
any other session.

When the worktree session is stopped, its card offers, under its rows:

- **Merge** - merges the branch into the repo's current branch. Refused if either side has
  uncommitted changes; a conflict is reported and the merge undone, nothing merged.
- **Remove** - removes the worktree, and its branch if merged. A branch with unmerged commits is
  kept. The session is removed and the page returns to the parent project session.

## See also

- [Remote access](Remote-access.md) - driving sessions from another device
- [Settings](Settings.md) - `session.*`, allowed folders, restored-term colouring
- [Troubleshooting](Troubleshooting.md)

---

[Home](Home.md) > Project Sessions
