# Example: texting as you, with a preview and a veto

Built. `yerk explain text-as-you` has the full entry and what it needs. This file shows
the shape a reader should expect, with placeholder people; nothing here is a config Yerk reads.

## The rule that makes it safe

Nothing goes out in the owner's name without the owner seeing it first. A reply waits in the
owner's room for one of three words: send, skip or edit. Plans, money and feelings are never
answered by the agent at all: they go to the owner as a question.

## A thread, start to finish

A text arrives on the owner's phone, from a placeholder contact, Sam:

> Sam: are we still on for Saturday?

The texting agent knows Saturday is on the owner's calendar, so it drafts, and the draft waits in
`example-room`:

```
🟠 text to Sam, waiting for you
"Yeah, still on. Noon works?"
send · edit · no
```

The owner answers `send`, and only then does it go. Another text:

> Sam: can you lend me some money till Friday?

Money is the owner's call, so nothing is drafted. The room asks instead:

```
🟠 Sam asks to borrow money till Friday
what do you want to say?
```

## What it would need

- The phone's messaging account signed in on the owner's own desktop computer.
- A small signed app there that carries texts both ways, and an ssh path from the box.
- Texts queue while that computer sleeps; none is lost and none is sent late without a fresh look.
