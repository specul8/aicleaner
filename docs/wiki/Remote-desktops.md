# Remote desktops

A Windows desktop in a browser tab, from any device, behind the same Cloudflare sign-in as AI Cleaner and sealed end
to end with AI Cleaner's tunnel code. This is **aicleaner-rdp**, a separate bolt-on: it is installed on each computer
whose desktop you want to reach, and AI Cleaner's menu lists those computers. For reaching AI Cleaner itself from
another device, see [Remote access](Remote-access.md) - set that up first; Remote desktops builds on it.

## How it fits together

- Each computer running aicleaner-rdp has its own small web page and relay on `127.0.0.1:8790`, and its own
  Cloudflare tunnel with its own address (`rmt-<name>.<your domain>`).
- That address is added to the same Cloudflare Access application as AI Cleaner, so one Cloudflare sign-in covers AI
  Cleaner and every desktop.
- The desktop stream is encrypted end to end with AI Cleaner's tunnel code, like AI Cleaner's own
  [encrypted tunnel](Remote-access.md#the-encrypted-tunnel): Cloudflare carries it but cannot read it.
- The relay only connects to Windows Remote Desktop on the same computer (127.0.0.1), so Remote Desktop's own port does
  not need to be open on the network.

## Adding a computer

Do this on the computer you want to reach, signed in as the Windows user whose desktop you want.

If you have the Remote desktops add-on, its deployment scripts come with it:

| Script | What it is for |
|---|---|
| `Install-AiCleanerRdpRelease.ps1` | Installs, repairs, updates or removes the add-on on this computer. It starts by asking what you are trying to do - reachable from anywhere (Cloudflare tunnel and sign-in), local use only, update, remove, make it start with the computer, or show the status - asks for anything it still needs, and ends by saying whether the computer can be reached from anywhere. |
| `Enable-RemoteDesktop.ps1` | Turns Windows Remote Desktop on (with Network Level Authentication) for the add-on, or off, or shows how it is set. The one step that needs an elevated PowerShell. |
| `Show-AiCleanerAccess.ps1` | On the computer that runs AI Cleaner: shows the Cloudflare values and the full install command for another computer, and (when asked) AI Cleaner's tunnel code. |

Where they are:

- in the add-on's release folder: `Install-AiCleanerRdpRelease.ps1` beside `aicleaner-rdp-<version>.zip` and its
  `.sha256`, and all three inside the zip's `installer` folder;
- once the add-on is installed on a computer: `%USERPROFILE%\.aicleaner-rdp\installer\`.

Each script asks what you want to do when run without parameters, and asks for any value it needs that you did not
give, so the steps below can also be done by simply running `.\Install-AiCleanerRdpRelease.ps1` and answering.

1. **Have ready:**
   - **AI Cleaner's tunnel code** - the one a new device types for AI Cleaner's encrypted tunnel. On the AI Cleaner
     computer, `.\Show-AiCleanerAccess.ps1 -ShowCode` shows it.
   - **A Cloudflare API token** (only for a computer that does not run AI Cleaner) - Cloudflare dashboard > My Profile >
     API Tokens > Create Token > Custom token, with: Account - Cloudflare Tunnel - Edit; Account - Access: Apps and
     Policies - Edit; Zone - DNS - Edit (your zone).
   - **Your Cloudflare values** (only for a computer that does not run AI Cleaner): the zone, the Access team domain,
     the Access application's audience tag and id, and the email addresses allowed in. They are on the AI Cleaner
     computer: `.\Show-AiCleanerAccess.ps1` lists them, with the full install command for another computer.
2. **Copy the release folder** onto that computer.
3. **Run the installer** in an ordinary PowerShell (not as administrator) in that folder:
   - on the computer that runs AI Cleaner (everything is read from AI Cleaner's settings):

     ```powershell
     .\Install-AiCleanerRdpRelease.ps1 -Cloudflare
     ```

   - on any other computer:

     ```powershell
     .\Install-AiCleanerRdpRelease.ps1 -Cloudflare -Zone example.com -TeamDomain team.cloudflareaccess.com `
         -Audience <audience tag> -AccessAppId <application id> -AllowedEmails 'you@example.com'
     ```

   Answer its questions: the tunnel code, the Cloudflare token, and **Y** to install cloudflared (and Python 3.13) if
   it asks. It ends with "The sidecar answers on 127.0.0.1:8790" and the computer's address. It then asks whether to
   **start with the computer** (Enter is yes) - see [Starting with the computer](#starting-with-the-computer).
4. **If it says Remote Desktop is off** (or Network Level Authentication is off), run in an **elevated** PowerShell:

   ```powershell
   .\Enable-RemoteDesktop.ps1 -LocalOnly
   ```

   It turns Remote Desktop on with Network Level Authentication, and `-LocalOnly` turns the Remote Desktop firewall
   rules off - only aicleaner-rdp on the computer itself connects to it. Then run step 3 again. This is the one step
   that needs administrator rights.
5. **Add it to AI Cleaner's menu.** On the computer that runs AI Cleaner the installer does this itself. For any other
   computer: in AI Cleaner, on its own computer, Settings > [Remote desktops](Settings.md#card-remote-desktop), add a line
   `NAME = https://rmt-<name>.<your domain>/` and **Save**. (Through remote access the list is shown, not changed.)

The address is `rmt-` and the part of the computer name after its last hyphen (DESKTOP-ABC123 gives `rmt-abc123`);
choose another with `-HostLabel <name>`. Running the installer again repairs rather than repeats.

### What the install does

- **Checks the computer** and says what would stop a connection (Remote Desktop off, NLA off, clipboard blocked by
  policy). It changes none of these itself (step 4 does).
- **Python 3.13** - used where its installer put it; if missing, installed with winget (asked first).
- **The program** - a venv and aicleaner-rdp in `%USERPROFILE%\.aicleaner-rdp`, with its settings.
- **The code** - on the AI Cleaner computer, AI Cleaner's own; elsewhere the code you type is stored encrypted to your
  Windows account (DPAPI).
- **Two sign-in tasks** - `aicleaner-rdp-sidecar` (the page and relay) and `aicleaner-rdp-tunnel` (the computer's own
  Cloudflare tunnel), started now and at every sign-in. No administrator rights. Each runs a supervisor that starts
  its program again whenever it ends, so a tunnel that fails because the network was not ready comes back by itself.
- **Optionally, two start-up tasks** - see the next section.
- **At Cloudflare** - the tunnel `aicleaner-rdp-<computer>`, a DNS record for its address, and that address added to
  AI Cleaner's Access application (nothing else about the application changes).

### Starting with the computer

Without it, a computer's desktop can be reached only while its Windows user is signed in. Started with the computer,
it is reachable after a restart before anyone signs in - Windows' own sign-in screen is the first thing you see.

- Turn it on: answer **Y** (or Enter) when the installer asks after choice 1, or run
  `.\Install-AiCleanerRdpRelease.ps1` and choose **5** (scripted: `-StartWithComputer`).
- Windows lets only an administrator make a task start with the computer, so it asks for **administrator approval**
  (in a window you can see; another administrator can approve - the tasks are still yours), then for **your Windows
  password**. Windows keeps the password for the tasks, encrypted; the script never stores or logs it. An account set
  to Windows Hello only ("passwordless") has no password to give: turn that off first.
- It registers `aicleaner-rdp-sidecar-startup` and `aicleaner-rdp-tunnel-startup`: at start-up, as you, at your
  normal (not elevated) rights, each running the same supervisor as the sign-in tasks. While they run, the sign-in
  tasks stand down; if Windows cannot sign them in (your password changed), the sign-in tasks still start everything
  when you sign in.
- **After your Windows password changes**, choose **5** again and enter the new one.
- The network must be up before sign-in for this to help: on Wi-Fi that authenticates the user (802.1X user sign-in),
  the network - and so the tunnel - only comes up when someone signs in; the supervisor keeps trying and connects then.
- **Status** (choice **6**, `-Status`) shows whether it starts with the computer, whether each task runs, whether
  Windows could sign a start-up task in, whether the sidecar answers, and the tunnel's last "connector registered".
- Turn it off: `& "$env:USERPROFILE\.aicleaner-rdp\installer\Set-RdpStartup.ps1" -Mode Off` (administrator approval).

## Connecting

1. In AI Cleaner's menu, choose the computer. It opens a new tab, "AI Cleaner RDP - <name>". The entry for the device
   you are on is greyed out (see [Which device you are on](Remote-access.md#which-device-you-are-on)).
2. Enter the tunnel code (once per device, per computer).
3. Your Windows user name and password; tick **Remember** if you want. Under **Use with the remote computer**, tick
   what the session may use - **Sound**, **Microphone**, **Camera** (all ticked at first; the device keeps your
   choice). **Connect.**
   - A Windows account signed in with a Microsoft account: the email and the Microsoft account's password (the page
     finds the local account it belongs to); typing the local name works too.
   - A local account: its name. A domain or Entra account: `DOMAIN\name` or `AzureAD\name@domain`.
4. The bar shows **End-to-end encrypted**, then the desktop at its real size; resize the window and the remote screen
   follows. The remote computer uses this device's display scale (a tablet's 200%, a laptop's 150%), so text is the
   usual size and sharp. Connecting takes over the session on that computer's own screen (its programs keep running).
5. **Sound, microphone and camera**, inside the encrypted stream: the remote computer's sound plays in the tab. The
   microphone and camera are used only while a program there uses them (a call, a recording); the browser asks this
   device's permission the first time, and the bar shows **Microphone on** or **Camera on** while they are. There
   they appear as *Remote Audio* and *Browser camera*.
6. Keys the browser keeps for itself (Ctrl+N, Ctrl+T, Ctrl+W, the Windows key, Alt+Tab): **Send keys** sends one of
   them; **Capture all keys** (Edge and Chrome) goes full screen and sends every key - hold **Esc** to leave.
7. Files, either way: drag files or folders onto the remote screen then **Ctrl+V** in a folder there; files copied in
   the remote File Explorer appear in **Transfer Files** with **Download**; **Transfer Files** also browses your
   profile there to send or download files and folders (up to 200 MB each), all inside the encrypted stream.

## The menu list

Settings > [Remote desktops](Settings.md#card-remote-desktop) lists the computers, one per line as
`Name = https://address`. Each becomes a menu entry that opens that computer's page in a new tab - nothing is embedded
in AI Cleaner's page. An address must be `https://` and may not hold a user name or password; at most 20. The list is
changed on the AI Cleaner computer.

## Updating and removing

- **Update:** put the new zip and its `.sha256` beside `Install-AiCleanerRdpRelease.ps1` and run it with no
  parameters. It keeps the tunnel, address and sign-in as they are. Update the computer you connect to.
- **Remove:** `.\Install-AiCleanerRdpRelease.ps1 -Uninstall` removes the start-up tasks if there are any (an
  administrator approves; not approved, nothing is removed), stops and removes both sign-in tasks, takes the address out of
  the Access application, deletes the DNS record and the tunnel, takes the menu entry out (on the AI Cleaner
  computer), and deletes `%USERPROFILE%\.aicleaner-rdp`. Files you transferred (`Desktop\aicleaner-rdp`) and Python
  stay. `.\Enable-RemoteDesktop.ps1 -Undo` (elevated) turns Remote Desktop off again.

## If something goes wrong

- The page says why; the browser console (F12) has the detail.
- On the computer: `%USERPROFILE%\.aicleaner-rdp\logs\` - `sidecar-<date>.log` (the relay), `rdp-<date>.jsonl` (who
  connected and when; never a password), `cloudflared-*.log` (the tunnel), `Install-AiCleanerRdp-*.log`.
- **Cloudflare error 1033** means the computer's tunnel has no connection to Cloudflare. On the computer,
  `%USERPROFILE%\logs\Run-RdpTunnel\` has a log per run of the tunnel's supervisor: each attempt, whether DNS answered,
  cloudflared's exit code and last error, and a "connector registered" line for each connection made. No recent log
  at all means nothing is running the tunnel: run `.\Install-AiCleanerRdpRelease.ps1 -Status`.

---

[Home](Home.md) > Remote desktops
