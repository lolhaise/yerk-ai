# texting

Texting as you: Yerk AI answers the one-to-one iMessage and SMS threads of the people on your reply list in your name, through your Mac. `[texting] mode` in yerkd.toml is `off`, `dry` (everything but the send) or `live`.

- `__init__.py`: marks the folder as a Python package.

## Reading your texts
- `bridge.py`: a read-only connection to the bridge app on your Mac, through which texting reads your Messages threads; it can never send.
- `intake.py`: writes each text down once, and gathers a person's texts into one burst to decide on.
- `link.py`: watches the one-to-one chats of the people on your reply list, and hands each new message to the intake.
- `people.py`: the reply list and each person's sheet (what may be said to them, and how you write to them); a sheet changes only on your word.

## Deciding what to answer
- `check.md`: the check's prompt, the rules a model follows to judge a draft.
- `check.py`: a second model call, which sees only the thread, the person's sheet, their new texts and the draft, checks every reply and tapback; what it cannot confirm is held.
- `writer.md`: the writer's prompt, the rules a model follows to write a text as you.
- `writer.py`: for each burst, one model call decides: a reply of one to three texts, a tapback, nothing, or a hold for you.

## Asking you, and sending
- `hands.py`: hands a text that may go to the Mac: in dry mode a job file on this machine, in live mode a job in the Mac's outbox, over ssh, for the runner there to send.
- `holds.py`: a text that waits for your word is one card in the room texting reports to; you answer it with send, edit or skip.
- `sender.py`: sends a text that may go at its time, after reading the thread again, and stops it if you or they wrote in between.

## Trying it on old threads
- `replay.py`: runs your past threads through the writer and the check to show what it would have decided; it sends nothing.
