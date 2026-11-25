# Tools

Written by `yerk docs own` from yerk_ai/mcp_server.py (build) and workers.WORKER_TOOLS; change those and run it again, never this page.

The 25 tools Yerk AI's own MCP server hands every room's Claude Code, preloaded, as mcp__yerk__NAME. Each one's description is the text the model reads, indentation dropped. A worker gets none of them unless [workers] yerk_tools in yerkd.toml names some, and then only recall, read_memory, on its own server.

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
End your turn while you wait on a worker or a named event. After this call write one short
line saying what you are waiting for, and make no more tool calls. A worker's result comes back to
the room by itself. Nothing watches any other event: the room hears of one only from the next
message, or from a job you schedule to check it.
```

- `wait_for`: string, required
- `reason`: string, required

## recall

```text
Search the owner's memory (daily logs, fact files, the curated file, open loops) by keywords
and, where the install has a provider key, by meaning too, so a paraphrase finds the line.
Each hit is the lines that carry the match and a `cite` (file:line). Cite every fact you use
by its `cite`; read_memory(cite) reads on from it. A "timeout" status means busy, not down.
`since` and `until` (each YYYY-MM-DD, either alone) keep the search to files named for a day in that range, both ends in it.
Work them out from the clock line when the question names a day, a week, a month or a phrase like "last Sunday", and leave
them out when it names no time.
```

- `query`: string, required
- `k`: integer, default 6
- `since`: string, default ""
- `until`: string, default ""

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
replaced. A rule is one lesson in the Claude docs' shape, Store one lesson per file with a one-line summary at the top. Record corrections and confirmed approaches alike, including why they mattered. Don't save what the repo or chat history already records; update an existing note rather than creating a duplicate; delete notes that turn out to be wrong. So a rule opens with a one-line summary, gives its reason, keeps its body brief and cites an example by its day-log line (<example>Landed, daily/YYYY-MM-DD.md:N</example>) in place of pasting it. Use plain words, and write in clear, understandable full sentences.; one that is not comes back with each
fix and the docs sentence it misses, and so do live lines of other jobs on the same thing, settled in `older`:
`cite | superseded: why` or `cite | kept: why`. `retire`: a rule's cite that turned out wrong, `text` why;
its line is deleted and its words kept in the day log.
`triggers`, with `job`: words or short phrases a message on the job holds (the owner's, when
named); a topic rule loads on them and its job's words. `said`: when `text` is what a person said or
wrote, word for word, their name, with `source` (the message id, file path or transcript line holding
those words): it is filed as their words. Anything else about a person (what they decided or want, in
your words) is your note, and is shown as your note, never as their words. `due`: the day `text` is due
(YYYY-MM-DD, or YYYY-MM-DDTHH:MM in the install's zone), in front of you at a session start from a week before; `owes`,
with it: who owes it to the owner, empty when the owner does. Give a date only when the words give one or the clock
line lets you work it out, never as a guess. Clear a thing by filing its outcome with `replaces` naming its line.
```

- `text`: string, required
- `about`: string, default ""
- `replaces`: list of string or null, default null
- `job`: string, default ""
- `triggers`: list of string or null, default null
- `said`: string, default ""
- `source`: string, default ""
- `older`: list of string or null, default null
- `retire`: string, default ""
- `due`: string, default ""
- `owes`: string, default ""

## worker

```text
Heavy work leaves the room: a worker is a headless run with its own card in the room and a
report. action "start": `brief` is the whole job (what done looks like, where it ends, what to
leave alone); optional `tasks` (its checklist rows), `cap_minutes` (default 65), `account`, `model`,
`effort` (low, medium, high, xhigh, max; default the room's live level), `room` (default this room), `addons` (your own
add-on tools it gets too, as named here or by server, such as a browser; unnamed it has a shell, files, the web and the
add-ons this room gives every worker, and reads this room's memory with recall and read_memory where the install gives
workers those; a step needing `confirmed` it leaves ready for you). Returns its id, folder
and report path; when it ends, the card settles and the room hears the result, or nothing if its last message is exactly
NO_REPLY. "list": workers and their state. "stop" / "report": by `id` (report gives the report's text, for you to tell in your own words).
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
- `addons`: list of string or null, default null

## schedule

```text
Timed work that lands in a room, kept on systemd timers that outlive the daemon. action
"add": `name` (lowercase letters, digits, hyphens); `when` is a date and time ("2026-03-09 09:00":
a one-shot, removed after it fires) or a systemd calendar line, recurring ("Mon..Fri 09:00",
"*:0/30" every half hour; never cron); `tz` an IANA zone (default: this room's); `what`, by `kind` (default "agent" in the
owner's rooms, "note" in any other): "note", a reminder
(when it is due this room is told the text and says it in its own voice; no worker, shell or
account); "agent", the brief a worker gets (a headless run with a card here, its last message told
by this room, exactly NO_REPLY for silence; `effort` its thinking level, low, medium, high, xhigh or max, and
`model` one from the install's list, default this room's live ones; it has a shell, files and the web, of your tools
only the add-ons this room gives every worker, those `addons` names and recall and read_memory where the install gives
workers those, never send, text or email, so a light timed check needing your tools is a note); "script", the shell
line to run (no model call, its output posted as is, exit 0 with no output posts nothing, a failure alerts the ops room). Any room
adds, lists and removes its own notes; an agent or script job runs as the owner, so only the owner's
rooms set one. `deliver` "announce" (default), "silent" (run and ledger, never post) or, for a script, "tell" (a watch:
this room is told its output when there is any, at most `wakes` turns an hour, default 6). Returns the
next fire time read back from systemd. "remove": by `name`. "stop": by `name`, its run going now; the timer stays.
"list": the jobs this room may see.
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
- `model`: string, default ""
- `effort`: string, default ""
- `addons`: list of string or null, default null
- `wakes`: integer, default 0

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

## people

```text
Who this room answers in its channel besides the owner, and the owner's change to it ("answer them here too"): op
`add` with `person` one of their messages in this channel, by the id in the [m:ID] that ends its day-log line or by
its link, answers them from their next message on; `drop` with their handle or id stops one so added; no op says who
is answered. `said` is the owner's own words in this turn asking it, three or more. Say its `line`.
```

- `op`: string, default ""
- `person`: string, default ""
- `said`: string, default ""

## transcribe

```text
A voice note or recording as text, since you take in sound only this way: `path` is the file's saved path from
its [attachment] line, or another audio file in this room's folders. The daemon transcribes it and returns the
words with a timestamp and, when two or more talk, a speaker letter per segment, saved beside the audio as
NAME.transcript.txt. A segment marked (uncertain) has two readings that disagree, so give it as unsure or ask. A
recording past US$1 comes back refused until the owner says yes; then call again with `owner_yes` true.
```

- `path`: string, required
- `owner_yes`: boolean, default false

## render

```text
A document as a PDF and its page count, since LibreOffice does not run in your shell: `path` is a document in this
room's folders (a .docx, .doc, .odt, .rtf, .xlsx, .pptx or anything LibreOffice opens). The daemon renders it to a PDF
saved beside it as NAME.pdf (an older NAME.pdf there is replaced) and returns its page count; a .pdf path returns its
page count alone, nothing rendered. A .tex in your work folder is compiled by LaTeX (latexmk; pdflatex unless it
loads fontspec or has a `% !TEX program` line); a failed compile says TeX's first error and its line. Use it to check a document fits a page count before you send it, and `send` the PDF
when the owner wants the PDF. The layout is LibreOffice's, not Word's, so a document that fits by a line in one can
spill in the other: say a result near the edge as near the edge.
```

- `path`: string, required

## watch

```text
A video in a post, its frames and its transcript, since your shell has no network to pull it: `url` is the link to
a post that holds a video (an Instagram reel or post, a TikTok, an X post, a YouTube link, or any page yt-dlp knows).
The daemon pulls the video into this room's work folder under videos/, takes `frames` still pictures spread evenly
through it (6 unless you ask for 1 to 12) and transcribes its sound, and returns the file paths and the transcript;
open the frames with Read to see what is on screen. The transcript is the video's own captions when the page has
them (uploaded or auto-generated; YouTube's, and any site's yt-dlp lists in json3), free and with their timestamps,
and only a video with no captions goes to the paid transcription and its US$1 gate. Use it whenever the owner sends
a video link and expects you to have watched it, since the caption, the cover picture and the comments are not the
video. A page that needs a login, a private post or a page with no video comes back as that, in the site's own
words. A transcript past US$1 comes back refused until the owner says yes; then call again with `owner_yes` true. A
video already pulled is not pulled or transcribed again. A video file sent to you, which Read cannot open, is watched
the same way from `path`, its saved path from its [attachment] line, in place of `url`; nothing is pulled. A video
attached to a Discord message is watched from the message's link as `url` (discord.com/channels/SERVER/CHANNEL/MESSAGE),
saved to this room's inbox first; a message in a channel this room does not read is refused.
```

- `url`: string, default ""
- `path`: string, default ""
- `frames`: integer, default 6
- `owner_yes`: boolean, default false

## seen

```text
After you Read a picture sent to you and before your answer: file what you read on it, so a later recall finds
it. `path` its saved path from the [attachment] line; `text` every word on it, verbatim, in reading order (a chat
screenshot keeps who said what as shown), except a password, a passphrase, a key or a token it shows, which is written [left out] in its place, never its value; "" if none; `shows` one or two lines of what it shows, a
person in it "the owner" (in a person's room "the sender") unless its own text says otherwise, anyone else named only
by that text, else "a man" or "a woman". Saved beside it as NAME.seen.txt.
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

## text

```text
Texts one person as the owner when the owner asks you to: an iMessage, else an SMS, from the owner's Mac. `to` is
their phone number or email address, never a name (recall it from the owner's texts, or ask); `text` the message
exactly as it should arrive. It is held for the owner to read first: once they say the word, call it again the same
and it goes once. A result with `maybe_sent` may have gone out: say so, and never send it again unasked.
```

- `to`: string, required
- `text`: string, required

## email

```text
Emails as the owner from the owner's own Gmail when the owner asks you to. `to` their email addresses, never a name
(ask, or find it in the owner's mail); `subject` one line; `text` the email exactly as it should arrive; `files` whole
paths of files in this room's folders to attach (a file sent to the room is in its inbox). It is held for the owner to
read first: once they say the word, call it again the same and it goes once. A result with `maybe_sent` may have gone
out: say so, and never send it again unasked.
```

- `to`: list of string, required
- `subject`: string, required
- `text`: string, required
- `files`: list of string or null, default null

## texts

```text
The owner's own recent texts (iMessage and SMS), newest first, read from the owner's Mac: use it to answer
whether someone has texted, or what a text said. This only reads; it never sends one (that is `text`). `limit` how
many to return (12 unless you ask, up to 40). Each message has `who` (the other person's number or address, empty
for one the owner sent), `from_me`, `at` (epoch seconds) and `text`, its words, or `blank` saying why it has none:
say that, never that it was empty. The owner's Mac sleeps when its lid is shut on battery, so the texts are kept as
a copy on the box, refreshed whenever the Mac is awake: `as_of` says where this answer comes from. With `awake` true
it was just read from the Mac; with it false the Mac is asleep or away and the answer is the last copy, whose age
`as_of` gives, so say that time and do not pass a stale answer off as fresh.
```

- `limit`: integer, default 0

## texting

```text
The owner's texting settings, in one owner room only. There, op `help` says what it reads and changes.
```

- `op`: string, required
- `person`: string, default ""
- `value`: string, default ""
- `said`: string, default ""

## delete

```text
Deletes files or folders outside this room's work folder when the owner orders it, since your shell deletes only
inside it: for good, not to the trash, so the disk space comes back. `paths`: whole, real paths from /. Held for the
owner to read first: once they say the word, call it again the same and it runs once. Returns what went, what failed
and why, and the space freed.
```

- `paths`: list of string, required

## zoom

```text
A part of a picture, enlarged, since Read shows you a picture at most 2000 px on its long side and the small print
of a bigger one is lost: use it before you answer from a time, a figure, a name or any print you would squint at (a
calendar, a form, a receipt, a chat's timestamps). `path` a picture in this room's folders (its [attachment] path);
x0, y0, x1, y1 the part as fractions of its width and height from its top left (0, 0.5, 0.5, 1: its bottom left
quarter), so look at the whole first and zoom where the answer is. Returns a PNG 2000 px on its long side (enlarged 4
times at most): Read it, then answer. When Read refuses a picture, zoom 0, 0, 1, 1 makes the whole of it one Read opens.
```

- `path`: string, required
- `x0`: number, required
- `y0`: number, required
- `x1`: number, required
- `y1`: number, required

## github_state

```text
A branch or a pull request of Yerk's own repository as GitHub has it now, since that repository is private and no
room holds a GitHub login (a web fetch there gets a 404): `branch_or_pr` is a branch's name (a worker's) or a pull
request's number. Returns the branch's head on GitHub (`github`), the same branch in Yerk's checkout against it
(`here`: whether GitHub has its commit, how many commits each side lacks) and its pull requests: open, draft, merged
or closed, and an open one's checks. Use it when asked whether a branch was pushed or its pull request is open,
green or merged.
```

- `branch_or_pr`: string, required

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

## page

```text
Draws on this room's live page, where the owner types, draws and runs tests in a browser; each part given replaces
what it shows. `code` a new program (solution.py); `tests` its unittest file (it imports solution; blank: the program
runs alone, its prints shown large), which the page runs on every pause in typing; `marks` line marks, each {"line": N, "kind": "highlight" or "arrow", "text": a note},
[] clears; `hint` a card under the program; `bar` labels and values, a scoreboard on top ("Gold II" a rank badge,
"12/40" or a number under LP a bar, a number big); `timer` seconds on its clock, 0 stops;
`stall` seconds of no typing before the page tells you, 0 off. Your replies show there too, and those to a line the owner asked about (all to the page,
its chat closed by the owner or by `chat` "closed" on their ask, "open" back) as bubbles under it, never in solution.py. What the owner types,
draws and says there comes to you as messages, mid-turn too; each keystroke is in page/events.jsonl. `open` true
(the owner asked for the page) turns it on, with no restart, and opens it on the owner's screen, `where` "mac" on
their Mac (default "pc"); `screen` the PC monitor the owner names (left, middle, right, primary or its number from
the left; default [page] screen): maximized there, woken with no click, said painted only once it is, so look (a
desktop screenshot of the rectangle given) only then; `off` true: off.
```

- `code`: string, default ""
- `tests`: string, default ""
- `marks`: list of object or null, default null
- `hint`: string or null, default null
- `bar`: object or null, default null
- `timer`: integer, default -1
- `stall`: integer, default -1
- `chat`: string, default ""
- `open`: boolean, default false
- `where`: string, default ""
- `screen`: string, default ""
- `off`: boolean, default false

## show

```text
Opens a file of this room's folders on the owner's PC screen when they ask to have it up, since your shell reaches
no screen: `path` its whole path (a built HTML deck, a PDF, a picture, a video). It opens in their own browser, never
run; `screen` the monitor they name (left, middle, right, primary or its number from the left; default [page] screen)
puts it there maximized, woken with no click. A reload there shows its newest save; a document: render it to a PDF first.
```

- `path`: string, required
- `screen`: string, default ""

## attachments

```text
The files an earlier Discord message carries, saved again to this room's inbox, since your shell has no network: a
file this room posted whose copy is gone (the sweep took it), or one sent here. `message` its id (the [m:] of its
day-log line; in this room's channel or the one it watches) or its link (discord.com/channels/SERVER/CHANNEL/MESSAGE;
an owner's room, any channel of its server); `name` a file's name: with `message`, that file only; alone, the newest
file this room posted under it. Returns each saved path: `send` it again, Read it or attach it.
```

- `message`: string, default ""
- `name`: string, default ""
