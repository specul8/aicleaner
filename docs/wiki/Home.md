# AI Cleaner wiki

AI Cleaner lets you work with AI coding assistants on client work without sending your clients' names
to the AI service. It sits on your machine between the assistant and the service, replaces every
term of a topic with a token on the way out, and puts the real terms back on the way in.

A topic is anything whose names you keep out of AI services: a client, a product, a person.

This wiki describes what AI Cleaner does today (v0.9.165). The registers in the repository root
(`DEFECTS.md`, `FUTURE-FEATURES.md`, `DECISIONS.md`) hold the history and the reasoning.

## Pages

| Page | What it covers |
|---|---|
| [Introduction](Introduction.md) | What AI Cleaner is, who it is for, and the idea in one diagram |
| [Benefits](Benefits.md) | What you get from it, and what it costs you |
| [Getting started](Getting-started.md) | Install, switch filtering on, add a topic, start a session |
| [How it works](How-it-works.md) | The request path, tokens and held-back tokens, rehydration, the canary |
| [Topics](Client-Filters.md) | Topics, terms, ignore lists, Global, finding things |
| [Review](Review.md) | Identifiers the mapping does not know yet, and what to do with them |
| [Project Sessions](Project-Sessions.md) | Driving Claude Code from the browser: project sessions, AI sessions, files |
| [Settings](Settings.md) | What each setting does |
| [Cloudflare tunnel](Cloudflare-Tunnel.md) | Deciding about remote access: what you get, why it is safe, what it needs |
| [Moving DNS from cPanel](Repoint-DNS-cPanel.md) | Pointing a domain's DNS at Cloudflare without disturbing email or the website |
| [Remote access](Remote-access.md) | Using AI Cleaner from another device, and the encrypted tunnel |
| [Sharing a topic with a team](Sharing.md) | One topic folder per engagement, in OneDrive |
| [Security model](Security-model.md) | What is protected, what is not, and why |
| [Architecture](Architecture.md) | How AI Cleaner is built: the stack, its parts, one request's path, where data lives, releases |
| [Troubleshooting](Troubleshooting.md) | Common symptoms and what to do |
| [Future features](Future-features.md) | What is planned and not yet built |
| [Glossary](Glossary.md) | The words this wiki uses |
