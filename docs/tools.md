# Tools

Written by `yerk docs own` from yerk_ai/mcp_server.py (build) and workers.WORKER_TOOLS; change those and run it again, never this page.

The 12 tools Yerk AI's own MCP server hands every room's Claude Code, preloaded, as mcp__yerk__NAME. Each one's description is the text the model reads, indentation dropped. A worker gets none of them unless [workers] yerk_tools in yerkd.toml names some, and then only recall, read_memory, on its own server.

## send

```text
Post to a room now, before your reply: text, or a file (absolute path) with the text as its
caption; a voice_note file goes as a voice message. `room` is a room's name; this room's own name or its
channel id both mean this room. Returns the posted message ids.
```

- `room`: string, required
- `text`: string, default ""
- `file`: string, default ""

## hold

```text
End your turn while you wait on a named event or worker. After this call write one short
line saying what you are waiting for, and make no more tool calls; the room hears when it happens.
```

- `wait_for`: string, required
- `reason`: string, required

## recall

```text
Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords
and, where the install has a provider key, by meaning too, so a paraphrase finds the line.
Each hit is the lines that carry the match and a `cite` (file:line). Cite every fact you use
by its `cite`; read_memory(cite) reads on from it. A "timeout" status means busy, not down.
```

- `query`: string, required
- `k`: integer, default 6

## read_memory

```text
Exact lines of a memory file, `lines` of them (default 12; ask more, up to 400, or a later
`from_line` to read on). `path`: a recall `cite` (its line is the start) or a memory path.
```

- `path`: string, required
- `from_line`: integer, default 1
- `lines`: integer, default 12

## remember

```text
File in memory now, before you write the answer, what you were just told: a new fact, a decision or a
correction, one line in plain words. `about`: for a fact, the person, place or thing it is about,
by the name the owner uses (it names the fact file). `replaces`: for a correction, each old line as
`cite | its words` (a hit's cite, then the old line's words); the one line from that cite through the
next 12 holding those words is marked superseded. Returns the new line's cite. `job`: when the owner
says how a job is done from now on, or corrects how it was done, the job's short name ("open web pages",
the rules' own name if it has one): `text` becomes its one rule in every room, the older one marked
replaced. `said`: when `text` is what a person said or wrote, word for word, their name, with `source`
(the message id, file path or transcript line holding those words): it is filed as their words.
Anything else about a person (what they decided or want, in your words) is your note, and is shown
as your note, never as their words.
```

- `text`: string, required
- `about`: string, default ""
- `replaces`: list of string or null, default null
- `job`: string, default ""
- `said`: string, default ""
- `source`: string, default ""

## worker

```text
Heavy work leaves the room: a worker is a headless run with its own card in the room and a
report. action "start": `brief` is the whole job (what done looks like, where it ends, what to
leave alone); optional `tasks` (its checklist rows), `cap_minutes` (default 65), `account`, `model`,
`effort` (low, medium, high, xhigh, max; default the room's live level), `room` (default this room). Returns its
id, folder and report path; when it ends, the card settles and the room hears the result. "list": workers and their state. "stop" / "report":
by `id` (report gives the report's text, for you to tell in your own words).
```

- `action`: string, required
- `brief`: string, default ""
- `id`: string, default ""
- `room`: string, default ""
- `tasks`: list of string or null, default null
- `cap_minutes`: number, default 0
- `account`: string, default ""
- `model`: string, default ""
- `effort`: string, default ""

## schedule

```text
Timed work that lands in a room, kept on systemd timers that outlive the daemon. action
"add": `name` (lowercase letters, digits, hyphens); `when` is a date and time ("2026-03-09 09:00":
a one-shot, removed after it fires) or a systemd calendar line, recurring ("Mon..Fri 09:00",
"*:0/30" every half hour; never cron); `tz` an IANA zone (default: this room's); `what`, by `kind` (default "agent" in the
owner's rooms, "note" in any other): "note", a reminder
(when it is due this room is told the text and says it in its own voice; no worker, shell or
account); "agent", the brief a worker gets (a headless run with a card here, its last message told
by this room, exactly NO_REPLY for silence); "script", the shell line to run (no model call, its
output posted as is, exit 0 with no output posts nothing, a failure alerts the ops room). Any room
adds, lists and removes its own notes; an agent or script job runs as the owner, so only the owner's
rooms set one. `deliver` "announce" (default) or "silent" (run and ledger, never post). Returns the
next fire time read back from systemd. "remove": by `name`. "list": the jobs this room may see.
<example>"remind me Saturday at 10 to call the clinic": action "add", kind "note", name
"call-clinic", when "2026-10-24 10:00", what "Remind them to call the clinic now."</example>
```

- `action`: string, required
- `name`: string, default ""
- `when`: string, default ""
- `what`: string, default ""
- `kind`: string, default ""
- `tz`: string, default ""
- `deliver`: string, default "announce"
- `room`: string, default ""
- `cap_minutes`: number, default 0
- `account`: string, default ""

## settings

```text
The owner asks to change this room's model or thinking level, or the account it runs on ("put this room on
Opus at max thinking on account three"): `model` an alias or full id from the install's list, `effort` low,
medium, high, xhigh or max, `account` an account's name (tried first, the others behind it); "default" for any
goes back to the room's own. It applies from the next reply on, in the same conversation unless the line says
a new session starts; with none it says what the room runs now. Say its `line`.
```

- `model`: string, default ""
- `effort`: string, default ""
- `account`: string, default ""

## transcribe

```text
A voice note or recording as text, since you take in sound only this way: `path` is the file's saved path from
its [attachment] line, or another audio file in this room's folders. The daemon transcribes it (your shell has no
network) and returns the words with a timestamp per segment, saved beside the audio as NAME.transcript.txt. A
segment marked (uncertain) has two readings that disagree, so give it as unsure or ask. A recording past US$1
comes back refused until the owner says yes; then call again with `owner_yes` true.
```

- `path`: string, required
- `owner_yes`: boolean, default false

## seen

```text
After you Read a picture sent to you and before your answer: file what you read on it, so a later recall finds
it. `path` its saved path from the [attachment] line; `text` every word on it, verbatim, in reading order (a chat
screenshot keeps who said what as shown), "" if none; `shows` one or two lines of what it shows, a person in it "the
owner" (in a person's room "the sender") unless its own text says otherwise, anyone else named only by that text, else "a man" or
"a woman". Saved beside it as NAME.seen.txt.
```

- `path`: string, required
- `text`: string, required
- `shows`: string, required

## need

```text
You cannot do what you were asked (no tool, a refused tool, a wall): file what you needed in place of building a
way of your own, so one build gives every room the same way. `asked` what you were asked, `tried` what you tried and
how it failed, `needed` the ability in a few plain words ("see a picture in a post"), `shadow` what you did anyway, if
anything. The same ability filed again is counted on the open one.
```

- `asked`: string, required
- `tried`: string, required
- `needed`: string, required
- `shadow`: string, default ""

## voice_note

```text
Speaks `script` as a voice note in English, when the owner asks for one or to say something out loud. Write it as
it should sound: `[[pause]]` alone on a line between sections, `(break)` for a short pause. The daemon makes it (your
shell has no network), checks that its first 20 s read back as your opening, and returns its file, length, engine,
cost and `probe` words: `send` that file with no text and it goes as a voice message. A note past US$1 comes back
refused until the owner says yes; then call again with `owner_yes` true.
```

- `script`: string, required
- `name`: string, default ""
- `owner_yes`: boolean, default false
