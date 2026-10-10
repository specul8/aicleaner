# aicleaner wiki

aicleaner lets you work with AI coding assistants on client work without sending your clients' names
to the AI service. It sits on your machine between the assistant and the service, replaces every
client identifier with a stand-in on the way out, and puts the real values back on the way in.

This wiki describes what aicleaner does today (v0.9.165). The registers in the repository root
(`DEFECTS.md`, `FUTURE-FEATURES.md`, `DECISIONS.md`) hold the history and the reasoning.

## Pages

| Page | What it covers |
|---|---|
| [Introduction](Introduction.md) | What aicleaner is, who it is for, and the idea in one diagram |
| [Benefits](Benefits.md) | What you get from it, and what it costs you |
| [Getting started](Getting-started.md) | Install, switch filtering on, add a client, start a session |
| [How it works](How-it-works.md) | The request path, tokens and placeholders, rehydration, the canary |
| [Client Filters](Client-Filters.md) | Clients, entries, ignore lists, Global, finding things |
| [Review](Review.md) | Identifiers the mapping does not know yet, and what to do with them |
| [Project Sessions](Project-Sessions.md) | Driving Claude Code from the browser: profiles, sessions, files |
| [Settings](Settings.md) | What each setting does |
| [Cloudflare tunnel](Cloudflare-Tunnel.md) | Deciding about remote access: what you get, why it is safe, what it needs |
| [Moving DNS from cPanel](Repoint-DNS-cPanel.md) | Pointing a domain's DNS at Cloudflare without disturbing email or the website |
| [Remote access](Remote-access.md) | Using aicleaner from another device, and the encrypted tunnel |
| [Sharing a client with a team](Sharing.md) | One client folder per engagement, in OneDrive |
| [Security model](Security-model.md) | What is protected, what is not, and why |
| [Troubleshooting](Troubleshooting.md) | Common symptoms and what to do |
| [Future features](Future-features.md) | What is planned and not yet built |
| [Glossary](Glossary.md) | The words this wiki uses |
