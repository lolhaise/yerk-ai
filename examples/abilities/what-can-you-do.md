# Example: "what can you do?"

The most common question a new user asks. Yerk answers it from `abilities.toml`, the same file
`yerk abilities` and `yerk explain` read, so the room, the terminal and the docs never disagree.

## In a room

The owner types, in `example-room`:

> what can you do?

Before the model writes a word, the message's recall part carries the manifest's answer (one line,
every ability by its state on this install, shortened here):

```
- abilities, a Yerk AI ability (on): Says what Yerk AI can do ... on (12): abilities, workers,
  capture, ... runs on the free path (1): recall. needs OpenAI (1): embeddings. known, not built
  (61): voice, job-search, job-applications, ... `yerk explain NAME` says what any one does,
  needs and costs. (yerk explain abilities)
```

The room answers in its own voice, state first, from that line and nothing else:

> Here's what runs today: heavy work as workers with their own card, scheduled jobs, reminders,
> and memory (every message filed, recall by keyword).
>
> Known but not built yet, so ask and I'll say how it would work and what it would cost: texting
> as you with a preview, reading bank balances, a browser on its own profile, a morning
> brief, and about 55 more.

A described ability is never "I can't": asked about one ("can you text people as me?"), the room
gets that entry's what, how, needs, cost, free path and why it is not built.

## In a terminal

```
$ yerk abilities
🟢 workers: on
⚪ recall: runs on the free path
🟠 embeddings: needs OpenAI
⚪ voice: known, not built
⚪ text-as-you: known, not built
...
yerk explain <ability>: what it does, needs and costs

$ yerk explain morning-brief
⚪ morning-brief: known, not built
A morning brief posted each day: ...
How it would be built:
- An agent job from the jobs file at the owner's hour, as a worker with a fixed brief.
...
Built part: jobs (yerk explain jobs)
```
