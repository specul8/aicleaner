# Introduction

[Home](Home.md) > Introduction

## The problem

AI coding assistants send what you are working on - prompts, file contents, command output - to a
model hosted by someone else. On client work that means client names, domains, tenant ids, email
addresses and resource names leave your machine, often in a form you did not choose to share.

## What aicleaner does

aicleaner is a small proxy that runs on your own machine. The assistant (Claude Code, and Claude
Desktop) is pointed at it instead of at the AI service. On every request it:

1. replaces each client identifier it knows with a **token** - a stable stand-in such as
   `Contoso` becoming a token like `Client42`;
2. replaces identifiers it has not seen before with a **placeholder** while you decide what they
   are (see [Review](Review.md));
3. checks that no known real value survived (the **canary**) and refuses the request if one did;
4. forwards the cleaned request to the AI service.

On the way back it does the reverse: tokens in the reply become real values again, so what you
read, and what the assistant writes to disk or runs, uses the real names.

```
 your files, terminal, repos          aicleaner (your machine)             AI service
 real names everywhere   ---------->  tokenise, placeholder, canary  ----> tokens only
                         <----------  rehydrate (tokens -> real)     <---- tokens only
```

## Who it is for

Consultants and engineers who use an AI assistant on more than one client's work, on their own
Windows machine, and who need to be able to say that client names did not go to the AI provider.
It is built for one person per machine; a client's files can be shared with colleagues through
OneDrive (see [Sharing a client with a team](Sharing.md)).

## What it is not

It hides *who*, not *what*: architecture, technology choices and anything not in the mapping still
reach the model. It only sees traffic from tools pointed at it. See [Security model](Security-model.md).

---

[Home](Home.md) > Introduction
