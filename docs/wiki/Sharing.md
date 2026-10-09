# Sharing a client with a team

[Home](Home.md) > Sharing

A client's file is the unit of sharing: a colleague with the same file gets the same tokens, so a
conversation, document or commit that carries `Client42` rehydrates to the same real name on both
machines. aicleaner shares through OneDrive, one folder per client, with exactly that engagement's
people (DECISIONS 51 and 67).

## The layout

```
<clients location>\          chosen at install; should be in OneDrive
  Contoso\Contoso.json       one folder per client - this folder is what you share
  Fabrikam\Fabrikam.json
  _registry.json             which index each client has
  _archive\                  archived clients
```

- **The clients location should be in OneDrive.** Install and the health check verify it, and warn
  (but allow it) when it is not - a solo user or a test box does not need it.
- Install pins the clients folder in OneDrive as **Always keep on this device**, so the files are
  really there rather than online-only placeholders.
- **Global is never shared.** It lives beside `aicleaner-config.json`, not in the clients location.
- Keys, last-good copies, case variants, review queues and logs stay in `%USERPROFILE%\.aicleaner`
  and the install folder - never in the shared folder.

## Sharing a client (the first person)

1. Open the client's page. If a colleague's client could have the same index, use **Change the
   index** on the Names card first (see Index clashes below).
2. In the **Client key** card, press **Generate key (first on this engagement)**. The key is shown
   once: copy it and send it to the others by Teams chat or a password manager - never put it in the
   shared folder.
3. Press **Open in OneDrive** on the Names card and use OneDrive's **Share** to give the engagement's
   people access to that client's folder. A work account opens the folder itself; a personal
   account opens OneDrive and shows the folder's path to find.

## Joining a shared client (everyone else)

1. In OneDrive on the web, open the shared folder and choose **Add shortcut to My files**.
2. On Client Filters, press **Joined a shared client?** and pick that folder (it must be under one of
   the folders the pages may reach - see [Folders the pages may reach](Settings.md#card-folders)). This works on the machine itself
   only, not through the tunnel.
3. Paste the key you were sent into the **Client key** card and press **Enter key**.

Joining links the folder into your clients location (a directory junction on Windows), so it is
read, written, signed and watched like any client created locally. It is refused when:

- the folder does not hold exactly one client file;
- it holds someone's Global;
- a client with the same id already exists here;
- its tokens would clash with a client you already have.

## The key and the stamp

Every client file aicleaner writes carries a `_stamp`: a SHA-256 checksum of the content, the
machine that wrote it, and when. With the client's key the stamp is also **signed** (an HMAC over the
same content), plus a short key id that names the key without revealing it.

| The file on disk is... | Result |
|---|---|
| Signed with this client's key | Used straight away |
| Unsigned, altered after signing, or signed with a different key | **Held for review**: the client shows "change waiting for review", listing the names added and taken out. The last good copy is used until you **Accept** (signs it with your key) or **Reject** (puts the last good copy back). |
| No key on this machine | Used as it arrives; a checksum mismatch shows "edited outside aicleaner" |

- The key is kept DPAPI-encrypted under `%USERPROFILE%\.aicleaner\keys`, readable only by your
  Windows account on this PC.
- **First run with a key adopts matches**: a file identical to this PC's last good copy is signed
  automatically; one that differs is held.
- **Rotate (someone left)** makes a new key at once. The old one stops verifying immediately, so a
  colleague still on it has every change held until they enter the new key.

Why it matters: anything that can write the shared folder - a text editor, another tool, the
OneDrive web editor - can change the checksum. Only someone holding the key can sign.

## OneDrive conflict copies

When two people save at once, OneDrive keeps both as `<id>.json` and `<id>-<COMPUTER>.json`.

- If both the main file and the copy are signed with the client's key, the copy is **merged in
  automatically**: new entries and ignores are added, and where both have a token for the same
  value, this PC's token is kept. The copy is moved to `%USERPROFILE%\.aicleaner\conflicts`, never
  deleted.
- Otherwise the copy is never loaded and the client shows a red **conflict copy** chip. Compare it
  with the file and delete it.

## Index clashes and renumbering

Tokens are derived from the client's index, then stored in the file. Two people who create
different clients independently can land on the same index, and then could produce the same token.

- aicleaner only refuses a real clash - two clients with the same token. The message names both
  clients and the index they share, and suggests giving one a different index.
- **Change the index** on the Names card gives a client a new index. Only tokens added from then on
  use it; tokens already in the file never change. Do it **before** sharing. It is refused when
  another client here already has that index, and works on the machine itself only.
- Joining a shared client is refused, without touching the colleague's file, if its tokens would
  clash with one of yours.

## Who added an entry

Each entry added to a shared client records who added it (`by`): your work account (UPN) where the
machine is joined, otherwise `DOMAIN\user`. Global's entries carry no author.

[Review](Review.md) shows **Added by someone else** - entries a colleague added that you have not yet
kept. **Keep** is remembered on your machine only; **Remove** takes the entry out of the shared file
for everyone.

## A missing folder is a red state

aicleaner never deletes a client, so a client this PC has used whose file is no longer there means
the folder was unshared, is not syncing, or is online-only. The client shows **file missing** in red
on the client page and in the status line. Its names are **still replaced**, from the last good copy
on this machine, until the file is back. Check the folder is still shared with you, synced, and kept
on this device.

## Good practice

- Share each client folder with that engagement's people only, with the same access rules as the
  client work itself - these files are a list of the client's identifiers.
- Send keys out of band, and rotate when someone leaves the engagement.
- Look at **Added by someone else** in Review now and then.

See also [Security model](Security-model.md) and [Troubleshooting](Troubleshooting.md).

---

[Home](Home.md) > Sharing
