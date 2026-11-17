# Add-ons

An add-on is a feature installed separately from AI Cleaner - its own release, its own installer - that plugs into AI
Cleaner's own process: it can add a card to Settings, answer on its own address under AI Cleaner's API, keep its own
settings, and start, prompt and stop project sessions. With no add-on installed, nothing of this shows anywhere.

## Which add-ons load

AI Cleaner loads exactly the add-ons listed in `aicleaner-addons.json` in its folder (`%USERPROFILE%\.aicleaner`). It
never looks in a folder for them, and never writes the list itself: each add-on's installer adds itself to it, turns
itself off (`"enabled": false`) or takes itself out.

Each entry names the add-on, its Python package, the folder its release was unpacked to (`root`), its own home folder,
the folders and files that hold its secrets and state (`protect`), and the SHA-256 of its release manifest.

## How an add-on is checked

- **Its release manifest.** `ADDON-MANIFEST` in the add-on's release folder lists every file of the release with its
  SHA-256. It must be the manifest the registry pins; every file it lists must match; and the add-on's package folder
  may hold no file the manifest does not list. Anything else and the add-on is not loaded: AI Cleaner starts without
  it, and the Add-ons card says why.
- **The bytes that run are the bytes checked.** AI Cleaner imports the add-on through its own loader, which reads each
  module, checks it against the manifest and compiles exactly what it checked. No compiled `.pyc` file is read or
  written, so a file swapped after the check, or a planted bytecode file, never runs.
- **Claude cannot change them.** The settings guard (which already keeps Claude from changing AI Cleaner's settings
  and reading its API key) also refuses a tool call that would change `aicleaner-addons.json`, an add-on's release
  folder, or the secrets and state its entry names, and any command that reads a DPAPI-protected secret file.
- **Its own Python packages only.** An add-on uses the standard library, the packages AI Cleaner already uses, and
  its own; it runs on AI Cleaner's Python.

## What an add-on can do

- **Routes** under `/api/addons/<name>/`, behind the same checks as the rest of AI Cleaner's API: the API key for a
  change made from this machine, and the remote-access rules - through remote access an add-on's pages can be read
  but not changed, unless the add-on marks a route as safe to change remotely (or as local only, read included).
- **Settings cards**, drawn by the add-on's own script in the group it names; through remote access a card whose
  settings act on this machine is shown greyed out, as built-in cards are.
- **Its own settings**, kept in AI Cleaner's config file under `addons.<name>` - never changeable through AI
  Cleaner's own Settings save, only through the add-on.
- **Project sessions**: start, prompt and stop them, and list sessions of its own. A prompt an add-on sends on
  someone's behalf always carries a note saying where it came from, and the Session page draws it as "Message from
  ..." - never as something you typed. An add-on's own sessions are grouped under its name in the session picker and
  are not counted against "sessions running at once": the add-on keeps its own limit.
- **An administrator step** opened on your desktop, the way Start with the computer does it.

## Seeing what is installed

The [Add-ons](Settings.md#card-addons) card (in the first group of Settings) appears once an add-on is listed: each one's version and whether it loaded, or why
not. The same list is at `GET /api/addons`.

## Turning one off or removing it

Use the add-on's own installer: it turns the entry off or removes it, and restarts AI Cleaner so the change takes
effect. An add-on is loaded when AI Cleaner starts, so a change to the list takes effect at the next start.
