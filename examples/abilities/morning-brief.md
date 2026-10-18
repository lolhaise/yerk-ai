# Example: the morning brief

Half built. The jobs ability runs it today as an agent job (`yerk explain jobs`); the brief's
fresh sources (the calendar, mail that matters, the weather, the commute) are described, not
built (`yerk explain morning-brief`).

## The job (works today, in a private folder's jobs.toml)

```toml
[jobs.morning-brief]
kind = "agent"                      # a worker with the message below as its whole brief
schedule = "0 7 * * 1-5"            # 07:00 on weekdays
tz = "Europe/Berlin"
room = "example-room"
cap = 15
deliver = "announce"
message = """Write the morning brief for the owner from the open loops and yesterday's daily log.
State first, three short lines at most: what is due today, what is waiting on someone, one thing
worth doing first. Nothing that is not on file. Nothing worth saying: answer exactly NO_REPLY."""
```

`yerk jobs sync` makes its timer and reads the next fire time back; `yerk jobs list` shows it.

## What the room gets at 07:00

```
🟢 morning brief
due today: the example invoice, and a reply to the example client
waiting on: the example quote, asked Tuesday
first: the invoice, it is 10 minutes
```

A morning with nothing on file posts nothing, because the worker answers `NO_REPLY`.

## What the described half adds

The calendar, mail that matters, the weather and the commute, read fresh each morning, each
labelled with where it came from. Until those are built, the brief says only what is on file.
