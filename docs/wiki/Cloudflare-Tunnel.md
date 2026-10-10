# Remote access through a Cloudflare tunnel

[Home](Home.md) > Cloudflare tunnel

A short page for deciding whether to switch remote access on. The full reference - every option,
the encrypted tunnel, troubleshooting - is [Remote access](Remote-access.md).

## What you get

aicleaner's pages, including your AI sessions, at a private address on a domain you own - for
example `https://cld-bsdq8.yourdomain.com` - from any browser, your phone included. The device you
use is only a screen: the AI agent still runs on this machine, and everything it sends is still
filtered here.

## Why it is safe

- **Nothing is opened on this machine.** The tunnel dials out to Cloudflare; no port is opened and
  aicleaner keeps listening on this machine only. There is nothing on your network to find or attack.
- **Nobody reaches the address without signing in.** Cloudflare Access stands in front of it. You
  choose the sign-in: your Cloudflare account, a one-time PIN emailed to you, or your Google,
  Microsoft or GitHub login - each with its own MFA. Only the email addresses you name get past it;
  everyone else is turned away at Cloudflare, before anything reaches this machine.
- **aicleaner checks again.** It verifies the sign-in token Cloudflare adds to every request rather
  than trusting it.
- **With the encrypted tunnel on, nobody can read what crosses it - Cloudflare included.** Cloudflare
  handles the connection's encryption at its edge, so on its own it could see the pages in clear.
  The encrypted tunnel (Settings) seals every page and answer with a code only you hold, in addition.

## What it needs

- A **domain you own**. No domain? Cloudflare sells them at cost (about US$10 a year), and one bought
  there is ready at once.
- That domain's **DNS run by Cloudflare** (free). If it is run somewhere else now - by your host, for
  example - moving it is reversible and does not disrupt email or your website, provided every
  record moves with it: [moving DNS from cPanel](Repoint-DNS-cPanel.md).
- A free **Cloudflare account** with **Zero Trust** switched on (a team name and the Free plan).

## How to set it up

The setup wizard's Remote access step ([Set up](Settings.md#card-setup) in Settings) walks through it: it checks the account,
looks at your domain's DNS (read-only), asks how you want to sign in, and sets up the tunnel, its
DNS record and the sign-in rule for you with a Cloudflare API token that can only touch what this
needs. Or do it by hand from the [steps in Remote access](Remote-access.md).

## Taking it down

`installer\Uninstall-AiCleaner.ps1 -RemoteOnly` removes the tunnel, the DNS record and the sign-in
rule, and aicleaner forgets them. Your domain and its other records are not touched.
