# Introduction

[Home](Home.md) > Introduction

## The problem

AI coding assistants send what you are working on - prompts, file contents, command output - to a
model hosted by someone else. On client work that means client names, domains, tenant ids, email
addresses and resource names leave your machine, often in a shape you did not choose to share.

## What AI Cleaner does

AI Cleaner is a small proxy that runs on your own machine. The assistant (Claude Code, and Claude
Desktop) is pointed at it instead of at the AI service. On every request it:

1. replaces each term it knows, in each of your **topics**, with a **token** - a stable replacement
   such as `Contoso` becoming a token like `Client42`;
2. holds back identifiers it has not seen before behind a temporary token while you decide what
   they are (see [Review](Review.md));
3. checks that no known real term survived (the **canary**) and refuses the request if one did;
4. forwards the cleaned request to the AI service.

On the way back it does the reverse: tokens in the reply become the real terms again, so what you
read, and what the assistant writes to disk or runs, uses the real terms.

A topic is anything whose names you keep out of AI services: a client, a product, a person.

```
 your files, terminal, repos          AI Cleaner (your machine)             AI service
 real terms everywhere   ---------->  tokenise, hold back, canary    ----> tokens only
                         <----------  rehydrate (tokens -> real)     <---- tokens only
```

## Who it is for

Consultants and engineers who use an AI assistant on more than one client's work, on their own
Windows machine, and who need to be able to say that client names did not go to the AI service.
It is built for one person per machine; a topic's files can be shared with colleagues through
OneDrive (see [Sharing a topic with a team](Sharing.md)).

## What it is not

It hides *who*, not *what*: architecture, technology choices and anything not in the mapping still
reach the model. It only sees traffic from tools pointed at it. See [Security model](Security-model.md).

---

[Home](Home.md) > Introduction
