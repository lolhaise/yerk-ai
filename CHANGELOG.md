# Changelog

One entry per merge to main, newest first: the date, the pull request, and what a person running Yerk AI notices.
A line that changes a file the install keeps (rooms.toml, yerkd.toml, budgets.toml, a unit) says so under
**Upgrading**, with what to do. The ledger migrates itself at the next start (its schema is versioned and a ledger
newer than the code is refused with one line, never changed); the index chunks every file again when its chunker's
version changes.

## 2026-11-17

### status-failed-read-keeps-reading (#?)

- One `/usage` call that fails no longer brings the stale `/status` card back. Since status-meters-kept-fresh yerkd keeps
  each account's live reading under 15 minutes old, but a card's live ask that failed (no answer in 15 s, output it could
  not read, no meters printed) was filed in `<state>/usage.json` over the account's good reading. The keeper then took
  the account as read a moment ago and left it for 15 minutes, and each card meanwhile showed the stream's reading, hours
  old with its age under it and without the model's own week. The live card, the one edited in after the ask, was handed
  no kept reading at all, so it showed the stream's even with a good reading minutes old in the file.
- A failed live ask now leaves a good kept reading in the file as it was: its numbers, its time, its source. The card that
  asked is still handed the failure, and the failure still counts as the try for a minute, so a second card inside that
  minute starts no second Claude Code for that account and gets the same failure back; that try is held in yerkd's
  memory, never in the file over a good reading. An account whose kept entry is itself an error, or that has none, takes
  the failure in the file as before.
- The keeper files a good background reading over an error whatever that error's time, so an error a card filed while
  the background read ran no longer drops the good reading. A good reading a card filed meanwhile still stays, and a
  failed background read still never replaces a good one.
- The live card, `yerk status` in a terminal included, shows an account whose ask failed with the newest reading it holds:
  the kept reading whole where it is newer than the stream's or the stream has none, else the stream's numbers with the
  kept model's own week while that week runs. The one age line under the account says how old that is, as it says for
  any reading not live on that card. In the payload the meter carries `live_error`, why the ask failed, beside it; the
  kept reading has `source` `kept` and its own `at`, as on the first answer. A card where every ask succeeds is the same
  byte for byte.
- Two lines of the keeper had no test: one now fails when the keeper is not among the tasks yerkd starts, and one when it
  does not wait between two steps.
- budgets.toml: `[code] lines` 32,803 to 32,816, with its dated note; docs/budgets.md is what `yerk docs own` writes.
  docs/GUIDE.md is unchanged: what it says of `/status`, `yerk status` and `--json` still holds.
- The tests: 11 new, in tests/test_observe.py (a failed ask leaves the good kept reading in the file and hands back the
  failure; a second card inside the minute asks nothing and gets that failure, and after it asks again; an account kept as
  an error or not kept takes the failure in the file; after a failed ask the keeper reads the account once its good
  reading is 15 minutes old; a good background read replaces an error a card filed meanwhile, never a good reading),
  tests/test_room_card.py (a failed ask on the live card: the kept reading 10 minutes old and one age line, not the
  stream's 8 hours; the kept reading older than the stream's: the stream's numbers with the kept model's week; every ask
  good: main's card byte for byte), tests/test_slash.py (`/status` whose ask fails: the edit, once, carries the kept
  numbers) and tests/test_reliability.py (the keeper is one of the tasks yerkd starts; it waits between two steps). The
  `/status` harness in tests/test_slash.py can have the /usage call fail, and its `run_discord` harness marks that the
  keeper started.
- **Upgrading**: nothing to change.

## 2026-11-16

### status-meters-kept-fresh (#91)

- `/status` typed in a room is now current from its first answer. Since status-card-at-once that answer is built from
  what is on hand, and an account no room's turn had run on since the morning showed its stream's reading, 8 h 13 min
  old, with `last reading 8 h 13 min ago` under it, its week 57% where the live meter said 59% (the owner's ask, that
  evening). A card that asks nothing can only be as fresh as what yerkd holds, so yerkd now keeps each account's live
  reading fresh, and the card uses the newest reading it holds.
- yerkd reads each account's meters again in the background. Every 30 seconds it takes the account whose kept `/usage`
  reading is oldest among those 15 minutes old or more and not tried in the last 15 minutes, and runs `claude -p /usage`
  on it once, through `nice -n 19` so it never competes with a room's turn; the result goes into `<state>/usage.json` as
  the card's live ask files one. One account a step, so never two background reads at once. One read, measured on the
  box that evening: 8.6 s and 10.5 s of wall time, about 7 to 9 CPU-seconds and about 350 MB at its peak, so each account
  once every 15 minutes is about 3.6% of one core for four accounts. After a start every reading is older than that, so
  the first steps read the accounts one after another, 30 seconds apart.
- A background read that fails leaves a good kept reading as it was and is tried again 15 minutes later (the try is kept
  in memory, not in the file); an account with no good reading kept takes the failure as the card's own ask would. A
  reading a card's live ask files while a background read runs stays: the background read's older result never
  overwrites it. A step that raises is one warning in yerkd's log for each distinct message, and the next step runs.
- A card built with no live ask (the room's first answer, `yerk status --json`, the `/status` HTTP answer without
  `live`) shows an account's last live reading whole (its 5-hour, its week and the model's own week, a window whose reset
  has passed left out) where it is newer than the stream's reading or the stream has none; else, as before, the stream's
  numbers with the model's own week from that reading. In the payload that meter has `source` `kept` and its own `at`, so
  a program tells it from a live reading (`live`) and from the stream's (`stream`); `/status` still reads it as not live,
  so the line under the tank saying the meters are being asked, and the one edit, still come while no reading is under a
  minute old.
- The account's age line counts the kept reading's own time as it counts the stream's, so it shows only when what the
  line shows is an hour old or more: with the background reads running, only when an account could not be read for an
  hour.
- The live card, `yerk status` in a terminal included, is the same byte for byte, and so is the first answer for an
  account whose stream reading is newer than its kept one. budgets.toml: `[code] lines` 32,751 to 32,803, with its dated
  note; docs/budgets.md is what `yerk docs own` writes. docs/GUIDE.md, where `yerk status --json` is described, says
  what the background reads do and what `kept` means.
- The tests: 10 new, in tests/test_room_card.py (stream 8 hours old and kept 10 minutes: the kept reading whole and no age
  line, and the payload marks it; stream 2 minutes and kept 10: main's card byte for byte; kept 2 hours and newer: its
  numbers and one age line, a passed window left out; every reading under a minute old on two accounts: main's card byte
  for byte), tests/test_slash.py (`/status` with the account as the owner found it: the kept week and model in the first
  answer, the line under the tank, the edit once), tests/test_observe.py (a step reads only the oldest past 15 minutes,
  then the next, then none; a failed read leaves a good reading and waits 15 minutes, an account with none takes the
  error; a card's reading filed meanwhile stays; the background argv starts with `nice -n 19` and the card's does not)
  and tests/test_reliability.py (the keeper says each failing step's message once, runs the next, ends with the daemon's
  tasks and never steps with no account). Two tests of status-card-model-meter change where the kept reading is the
  newer: the age line's test keeps only its case with the stream's reading the newer (its two others are the new tests'
  shapes), and an account no stream has reported shows its kept reading whole, "no reading yet" only with none kept. The
  two harnesses that run `run_discord` (tests/test_slash.py, tests/test_watch.py) hold the keeper as they hold the reaper.
- **Upgrading**: nothing to change. After the restart yerkd reads every account once, 30 seconds apart, then each about
  every 15 minutes.

### texting-9b-cards (#90)

- With `[texting]` on, a card for a text that waits for you no longer shows the writer's reasoning. A card with a draft
  says why in the code's own few words: `why: dating, your call` for a class that waits (two: `why: dating, money, your
  call`), `why: no source for: …` when the draft says something about you that nothing backs, else `why: your call`.
  Until now it printed the writer's question, the class words and its why joined into a paragraph that mostly repeated
  what the card already shows. The code's own check holds with the same words, and a card made from your edit reads
  `why: your words; dating, your call` or `why: your words; the code finds nothing`. A card the check's model call
  held, or one with release off, reads as before.
- A card with no draft (a plan: your answer is the text) gives the one question only you can answer, shows no empty
  `draft:` line and offers `edit <text>` and `skip` alone. Until now it offered `send` too, and your send on one queued
  a row with no text: the sender's hand-off raised on it and the sender stopped, for everyone, until yerkd was
  restarted. Your send on such a card, as a reply or bare, sends nothing and says so, and a row with no text that
  reaches the sender is dropped, never handed.
- The writer's whole reasoning is kept in the ledger, in a new column of the texting row written once at the decision
  and never again, and in the practice file's line in dry. The `texting` tool's `why` reads it back for the newest
  decision, also after the text went or was skipped, where until now that answer was only the state the card closed
  with; for a text the check held it gives the check's reason. The writer is handed the whole reasoning of what
  already waits on you, as before; `log` is unchanged.
- A statement's number is the code's, by its place. Two statements the writer numbered 1 and 1 are two the check must
  confirm, where until now a verdict confirming number 1 once let the other go confirmed by nobody.
- budgets.toml: `[code] lines` 32,743 to 32,751, with its dated note; docs/budgets.md is what `yerk docs own` writes.
  docs/GUIDE.md, Texts as you, says how a card gives its why, that a card with no draft takes no `send`, and that the
  `texting` tool's `why` reads the reasoning back.
- The tests: 10 new, in tests/test_texting_writer.py (the card's line for a drafted class hold; a card with a draft
  carries only the code's words and one with none the question; a reply and a none; a card the writer held, six lines,
  its whole reason the same after a newer burst, a send, a skip and an expiry; the held fence; a ledger made before the
  column), test_texting_people.py (the tool's `why` after a send and for a text the check held, `log` unchanged),
  test_texting_check.py (statements numbered by place, and a verdict that confirms only the first holds and names the
  second), test_texting_holds.py (a card with no draft only asks, and your send on it sends nothing) and
  test_texting_sender.py (a queued row with no text is dropped and the sender runs on). The practice file's test reads
  `why`; the card, edit and pre-check tests read the new words.
- **Upgrading**: nothing to change. The ledger gains the column by itself at the next start; a row made before it has
  no kept reason, and the tool's `why` and the writer read its `reason` as before.

### status-card-model-meter (#86)

- `/status` typed in a room now shows each account's model's own week from its first answer. Since status-card-at-once
  the first answer showed `wk 44%` alone, and the model's own week came only with the edit about 15 seconds later; the
  owner asked within the hour why the card did not show it (the owner's ask, 2026-11-16). No room's stream reads that
  meter, only `claude -p /usage` does, and the first answer asks nothing.
- A card built with no live ask (the room's first answer, `yerk status --json`, the `/status` HTTP answer without
  `live`) now takes the model's own week from the account's last `/usage` reading, which `<state>/usage.json` keeps
  whatever its age, when the account has no reading under a minute old: `wk 44% · Fable ⚠️83%`, the week and the
  5-hour numbers still the stream's, the newer ones. Nothing is asked for it and no Claude Code is started. A model's
  week whose reset has passed, or was never read, is not shown: its number is wrong. An account whose last call failed,
  or that no stream has reported yet, shows what it showed before.
- While that reading is under an hour old the card says nothing about its age: the live numbers are seconds away. At an
  hour or more the account's own age line says so (`①: last reading 2 h 0 min ago`). Where the stream's reading earns
  that line too it is still one line, saying the older of the two ages, so no number on the account's line is older
  than the line says.
- In the payload the carried entry has the time of the reading it came from beside it (`model.at`, never in a live
  reading's), and the account's meter stays the stream's, so `/status` still reads it as not live: the line under the
  tank saying the meters are being asked, and the one edit, still come.
- The live card, `yerk status` in a terminal included, is the same byte for byte, and so is a card whose readings are
  all under a minute old. STYLE.md's sentence about the room's `/status` card now names the hourglass that line opens
  with. budgets.toml: `[code] lines` 32,731 to 32,743, with its dated note; docs/budgets.md is what `yerk docs own`
  writes. No README or docs/GUIDE.md text describes `/status` in a room, so neither changes.
- The tests: 6 new, in tests/test_room_card.py (from the files alone, a live reading kept ten minutes puts the model's
  week beside the stream's newer week with no age line, and the payload marks it; kept two hours, the age line says so
  once, and with the stream's reading old too one line with the older age; a kept week whose reset has passed or was
  never read is not shown; no kept reading, a failed one or no stream reading leave the card as it was; a reading under
  a minute old and the live card print what main printed) and tests/test_slash.py (`/status` with the account as in the
  first: the first answer has the line under the tank and the model's week on the account's line, and the edit comes
  once).
- **Upgrading**: nothing to do.

### texting-9a (#87)

- With `[texting]` on, a text the owner answers himself while its draft is being written is left alone. Until now the
  writer's call coming back wrote its draft over his answer, so the draft was checked and timed as if he had said
  nothing, and when it needed his word a card was shown for a text he had answered seconds before. The read of the
  thread before a send still stopped it, so nothing wrong went. The decision is kept for the record (what it was, its
  seconds and tokens), and in dry its line in the practice file says `answered_first`. A draft still being decided
  whose texts he answered with nothing else having moved it is closed as answered by him: left as it was, the writer's
  next pass would decide it again, one call a pass (no path reaches that today).
- A tapback is seen. The Mac's bridge hands one over only when asked, and texting never asked, so none ever reached it.
  The link now asks. A tapback of the owner's own stops a waiting text and takes its card back, as his own text does,
  both when the watch hands it over and in the read of the thread before a send, where the bridge lists it on the row it
  sits on; one taken away is filed and is nothing else; one of theirs is filed and opens no decision, as before.
- A tapback texting sent is recognised as sent. Until now each one was marked "may not have gone, not sent again" three
  minutes after it was handed, `yerk status` showed texting not done for a day and the morning line counted it, though
  it went. A claimed tapback is kept by its row id, the one name the bridge's thread read gives it.
- The writer and the check see each row's tapbacks on its thread line: whose (the owner's, theirs, or texting's own as
  `auto`) and which. A thread with none is handed to both byte for byte as before.
- budgets.toml: `[code] lines` 32,706 to 32,731, with its dated note; docs/budgets.md is what `yerk docs own` writes.
  docs/GUIDE.md says a tapback of yours stops a text and takes its card back, as your own text does.
- The tests: 10 new, in tests/test_texting_writer.py (his own text filed while the writer's call runs, for a reply, a
  held and a none; the same while the check's call runs; one case for each read after the call, the row closed where only
  its texts were answered; each kind of tapback
  on the writer's and the check's thread line, and today's stdin for a thread with none), test_texting_link.py (the
  ask on every chat at a first start and a reconnect), test_texting_intake.py (their tapback and its removal from the
  watch) and test_texting_sender.py (his own tapback as his answer and its removal as nothing; theirs and texting's own
  claimed one on the thread do not stop a text, and neither does one of his older than the texts answered; texting's own
  tapback claimed as sent through the link). The stub
  bridges now answer as the Mac's does: a thread read lists a tapback on the row it sits on and never as a row, and a
  watch hands one over only when asked; three sender tests moved to that shape.
- **Upgrading**: nothing to change.

### need-2026-11-16-102 (#?)

- An email a room sends in your name now shows, before your word, the From line it goes out under, and says when your
  name is missing from it. A room asked why its emails landed in people's spam found, in your Sent Mail, each email it
  had sent under the bare address and each one you sent yourself under your name beside it. The `email` tool takes the
  name only from `[gmail] name` in yerkd.toml, which was not set; the held email's preview showed the bare address
  either way, and yerkd.toml is out of a room's reach, so nothing said so.
- The preview's from line is now the From line as it goes, `from: Example Owner <address>`; with no name set it reads
  `from: <address> (no name on it: yerkd.toml has no [gmail] name)`. The send's answer names the From line it went
  under, so the room can say so after it went too. No name is ever made up: with none set an email still goes under
  the bare address.
- `yerk doctor` says it each day while it lasts, one orange line with the line to add: `[gmail] name: none, so each
  email goes out under the bare address, ...; set name = "<your name>" under [gmail] in yerkd.toml`, then the restart.
  An install with a name set, or with no Gmail to send from, hears nothing of it.
- A room asked why its emails land in spam, or whether your name is on them, is handed the new ability `email-name`
  (`yerk explain email-name`) with a Here line saying whether the name is set on this install, read from yerkd.toml as
  the doctor reads it, never the name or the address, so it answers without the file it cannot read.
- A `[gmail] name` with a line break in it goes on the From line as one line; before, the email failed to build. A
  name past ASCII reads in the preview as written, never in its wire encoding.
- budgets.toml: `[code] lines` 32,659 to 32,706, with its dated note; docs/budgets.md and docs/settings.md (`[gmail]
  name`'s description) are what `yerk docs own` writes. The README says it in one clause, kept under its 12,000 bytes
  by five shorter phrasings with the same meaning.
- The tests: 4 new, in tests/test_emails.py (with no name, the preview's from line marked before the word, the From
  line in the answer after it, the bare address sent and a blank name no name; a name with line breaks one line, never
  a header of its own, and one past ASCII shown as written), tests/test_doctor.py (the orange line with the line to
  add, quiet with a name or no Gmail to send from) and tests/test_abilities.py (the ask that finds email-name, its Here
  line set and unset, never the name or the address, and `yerk explain email-name`); the held preview's test reads the
  From line as it goes.
- **Upgrading**: to put your name on each email, add `name = "<your name>"` under `[gmail]` in yerkd.toml, written as
  your own Gmail writes it, then restart yerkd, which reads yerkd.toml at its start. Restart yerkd for the new preview.

### status-card-at-once (#?)

- `/status` typed in a room now shows its card at once. It showed nothing for about 30 seconds (the owner's ask,
  2026-11-16): every line of the card was ready at once but the meters, and the card waited for them. Each account read
  more than a minute ago was asked with `claude -p /usage`, a whole Claude Code start, three at a time and up to 15 s
  each: 18 s for four accounts on a quiet box, about 30 s on a busy one, with Discord showing it thinking and not one
  line.
- The first answer is the room's card from what is on hand, with no Claude Code started: each account's reading under a
  minute old, else its stream's. While an account on it has no reading under a minute old, one more line under the tank,
  `⏳ asking the meters now`, says they are being asked; they are then asked as before, and the same message is edited
  once to exactly the card `/status` gave until now, without that line.
- With every account read under a minute ago, the first answer is already that card: one message, no line, no edit and
  nothing asked.
- An account whose read fails or runs out of time shows what the card showed for it before. An edit Discord refuses, the
  interaction's 15 minutes gone say, is one line in the daemon's log, and the first card stays.
- `yerk status`, the `/status` HTTP answer, the deploy's proof and the other slash commands answer as before, and every
  line the card had is the same. budgets.toml: `[code] lines` 32,636 to 32,659, with its dated note; docs/budgets.md is
  what `yerk docs own` writes. No README or docs/GUIDE.md text describes `/status` in a room, so neither changes.
- The tests: 4 new, in tests/test_slash.py (with the account's `/usage` call held on an event, the first answer is out
  before it ends, carrying the line under the tank; let go, it edits that message once, to exactly the card status_now
  with live gives, without the line; every reading fresh, one message, no edit and no call; an edit that raises
  `discord.HTTPException` leaves nothing out of the handler, the first answer standing and one log line with no
  traceback). Its `/status` card test now reads the two calls and the edit; the other slash commands' tests pass untouched.
- **Upgrading**: nothing to do.

### need-2026-11-16-101 (#?)

- A job fired in the same second the jobs file changes now starts. A one-shot's run ended and took itself out of the
  file in the second a script started another job. That job's runner found it and ledgered its run, but the daemon's
  fire answered "no job" and the run ended not run. The schedule tool's add and remove, `yerk schedule` and a one-shot
  leaving after its run each wrote the file in place, and the fire, a job's runner and a sync read it with no lock, so a
  read in that instant found the file empty or cut and every job gone.
- Each new text of the jobs file is now written beside it and swapped in with one rename, so a read at any instant finds
  the old file or the new one whole. It holds alike for a job its timer fires and one a script starts, so neither needs
  a retry for it. A sync in that instant could take every job's timer down until the next sync; it reads the file whole
  now too. A write that fails partway, on a full disk say, leaves the file as it was.
- The file is written 0600, as every private file yerkd writes, and a jobs file that is a symlink keeps its link: the
  file it points at is swapped. A hand edit is as whole as the editor writes it.
- The ability is `job-fires` (`yerk explain job-fires`), found by a room told "no job" by a fire or asking why a job
  fired but did not start. budgets.toml: `[code] lines` 32,629 to 32,636, with its dated note; docs/budgets.md is what
  `yerk docs own` writes. The README says it in one clause, kept under its 12,000 bytes by eight shorter phrasings with
  the same meaning.
- The tests: 3 new, in tests/test_jobs.py (a job fired at the instant a one-shot's run swaps its own removal into the
  file finds its job and starts its worker, and the one-shot still leaves the file; a reader that had the file open
  reads it whole through an add and a remove, a reader beside a hundred rewrites never misses a job, a rewrite that
  fails leaves the file as it was and no temp file, the file is 0600 and a symlink keeps its link) and
  tests/test_abilities.py (the ask that finds job-fires).
- **Upgrading**: nothing to do. A jobs file that was not 0600 is at its next add or remove.

### need-2026-11-16-100 (#?)

- A scheduled job's worker card now names its job and shows its one-line result. A room's six quiet job cards read
  alike, `🟢 worker finished clean, nothing to add`, the time and "report saved", though four were runs of one job whose
  reports each held a forecast's one line and two were another job's runs with nothing to say. On a phone, where
  `yerk worker report` is out of reach, nobody could tell them apart; the job's brief tells its worker to write that one
  line to REPORT.md because the card shows it, and the quiet card hid it.
- The card's small line now starts with the job's name, as in `-# job <name>, done in 4 min`, while the worker waits for
  an account, while it runs and once it settles. A long name is cut to phone width. A worker no job started reads as
  before.
- A worker whose last message is exactly NO_REPLY, and whose report's first line is a result, settles as `🟢 worker
  finished clean` over that line. `🟢 worker finished clean, nothing to add` stays for a run whose report says NO_REPLY
  too, or that left none. Either way no turn is asked of the room, as before, and the steps stay behind
  `yerk worker report <id>`.
- Every settled card's report line now takes up to two phone lines, broken at a word, instead of one: cut at 40 columns,
  a forecast's line lost its number. A longer line ends in "...".
- A job's brief now asks every run for its one-line result as REPORT.md's first line, a NO_REPLY run's too. It said to
  write the same text as the last message, so a quiet run that followed it wrote the token itself and its card had
  nothing to show. A job whose own message asks for something else there still gets what it asks for.
- The worker tool's `list` and `report` answers carry the job, and `yerk worker list` and `yerk worker report` print it,
  so a room asked which job a card was for reads it there, not in the ledger's table.
- The ability is `job-cards` (`yerk explain job-cards`), found by a room asked whether its cards are the same worker
  doing the same thing. budgets.toml: `[code] lines` 32,607 to 32,629, with its dated note; docs/budgets.md is what
  `yerk docs own` writes. The README says it in one clause, kept under its 12,000 bytes by four shorter phrasings in
  its first section with the same meaning; docs/GUIDE.md, What you see, has the long form.
- The tests: 3 new, in tests/test_workers.py (a job's card on fixed ends: the job in the footer waiting, running and
  settled, a quiet result whole over two lines, the token and a missing report still nothing to add, a longer line and a
  long name cut, no job on a plain worker's), tests/test_jobs.py (two jobs fired for real, both answering NO_REPLY:
  each card names its job, one shows its report's line and one has nothing to add, no turn asked, the tool's list and
  report and `yerk worker list|report` name the job, and the brief asks a quiet run for its line) and
  tests/test_abilities.py (the ask that finds job-cards).
  Two quiet-card tests in tests/test_workers.py now expect a real report line under "finished clean", and
  tests/test_style.py holds a job's cards to the style rules.
- **Upgrading**: nothing to change; a worker that ends after the deploy's restart settles this way. A card that already
  settled keeps its text.

## 2026-11-14

### need-2026-11-14-97 (#?)

- A room asked whether a store has an item in stock, met by a retailer whose product page a captcha or bot-check holds
  shut, now hears that plainly instead of reading an opaque empty page. The hands browser came back empty behind the
  challenge frame and left the room to improvise off a photo of the stock box; it now names the check and the ways that
  still read the count.
- The browser names a page behind a bot-check: a dedicated captcha host (such as DataDome's) in a frame on sight,
  another challenge host or a challenge phrase only when the page's own content never arrived, so a full page that
  merely embeds a widget on a form, a payment page or a sign-in is not one. The note says this browser cannot read or
  pass it, and that hands does not spoof a browser or solve a check; it says the page's own content never loaded only
  when it did not.
- The note names the ways that still read the page, each as what it is: Yerk's web reach (web_fetch), whose ladder is
  built for such pages, with its own stealth browser and captcha solvers (the machine's where installed, then the paid
  cloud browser's), and names the wall when the page stays shut; or the owner reads it on their own screen. The check is
  not called a wall left alone while the room is sent to a ladder that solves one.
- web_fetch's cloud-browser rungs cost what the web ability's price says: a read inside the Browserbase plan's included
  calls and hours, each paid call a spend row. Its rungs on the machine, and the owner's own screen, cost nothing. The
  web ability's one daily cap is web_research's, not web_fetch's.
- The ability is `store-stock` (`yerk explain store-stock`). The behaviour lives in the hands plug-in (its branch
  need-2026-11-14-97); its browser tool entries do not change, so no budget moves and no room needs new settings.

### texting-7d (#80)

- Nothing changes until an install has a `[texting]` table.
- A file sent in a thread is named to the writer, which until now was not told of it: the thread it read asked the
  bridge for each row's text and never for its files. A photo, a video, a voice message, a sticker or another file is
  named by its kind, in the check's thread too, and the mark a file leaves in a text is no longer shown as words.
- A photo in the texts being answered, the newest four, is looked at: converted to a small JPEG on the Mac over the
  hands' ssh (`[imessage] host`, or an owner room's mac.json), with its path on ssh's stdin, and shown to the writer's
  one call. It is never stored on the box. Its ledger row says seen, too big, refused or not fetched. A video, a voice
  message, a sticker or an older photo is named and not opened; with no host a photo stays named and unseen, and the
  call is what it was.
- The check stays text only and its thread names every photo as unseen. On a real call it held both replies that said
  what a photo showed and released one that only reacted to it: a reply about what a photo shows waits for the owner's
  word.
- A link preview is no longer counted as a file: the writer is not told of one, and the ledger and the day log no
  longer list it.
- A text in a watched chat is no longer counted as a dead turn in the morning line or in `yerk status`, nor as a turn a
  restart cut: it gets a decision and never a turn. The machine room's line read "dead turns: 4" two mornings running,
  every one of them a text.
- budgets.toml: `[code] lines` 32,508 to 32,607, with its dated note.
- **Upgrading**: nothing to change.

### texting-7b (#79)

- Nothing changes until an install has a `[texting]` table.
- `yerk status` and `yerk doctor` say when the sender stopped. The sender writes the time of its last pass, at most every
  15 seconds; when it is older than a minute in dry or live, the texting row reads "texting sender stopped" with the
  time, and doctor is red while yerkd runs, with `systemctl --user restart yerkd` as the way back. An error the sender's
  pass did not expect now stops the sender alone, never yerkd and its rooms, and the machine room gets one line naming
  the error's kind, never a text or a person.
- The machine room gets one line a day after 08:00, in dry and in live, counted from the ledger alone for the day to that
  morning's 08:00: texts in, decisions by kind, held, sent (dry: would have gone), stopped because you answered first,
  stopped because they wrote again, unseen, and the longest run of texting's own sends to one person with no text of
  yours between. A quiet day says so in a few words; a restart never says a day twice and never skips one.
- Your main room, the one `[texting] report` names, is told every turn while texting is dry or live what texting is,
  that a card there is posted by the texting code and your `send`, `skip` or `edit <text>` on one is taken before the
  turn starts, and that anything else you say about texting is the room's to answer from the `texting` tool. No other
  room is told, a person's room least of all.
- A text of texting's own that the Mac sent while the bridge was away, seen only after its job was settled unseen, is
  no longer filed as your own words: its job reads sent, seen late, and it cancels nothing.
- `yerk doctor` is red in live when the hands have no way to the Mac: no `[imessage] host` and no ssh in an owner room's
  add-on's mac.json, or a host that begins with a dash. In dry or live it is red when nothing names the Mac's bridge
  at all, which until now turned texting off with one log line and no other word.
- docs/GUIDE.md has "Texts as you", the README names it, and `yerk abilities` lists text-as-you as built.
- budgets.toml: `[code] lines` 32,385 to 32,508, with its dated note.
### texting-7c (#78)

- Nothing changes unless the owner runs `yerk texting replay --since DAY --sender`. It calls no model and sends
  nothing: for every burst of their texts since the day in his imported threads, it times a reply as the live sender
  would (an early, a middle and a late send time, the ends and the middle of the sender's own span) and reads the thread
  as it stood at each time as the sender does, then prints counts only, a row a person and the total for each time: the
  bursts, the texts it would stop because he answered first or because they wrote again, the ones it would send, and of
  those whether he answered too within the hour, the old agent did, or nobody did. One private file under the state
  folder's `texting/replay/` holds a line a burst; its first line names the limits (times to the minute, no tapbacks in
  an import, a thread's file ending where its import does, the reply taken as passed when the burst closed) and says
  whether an old agent's log was given. His answer in the same minute as their last text counts as answered first, by
  the file's order. `--of` a finished replay's file keeps only the bursts whose reply passed the check in that run. An
  answer the old agent sent (`--old-log`) never counts as his.
- **Upgrading**: nothing to change.

## 2026-11-13

### need-2026-11-13-91 (#?)

- A room opens a file of its own folders on your PC's screen when you ask to have it up: a slide deck it built as a web
  page, a PDF, a picture, text, a video or a sound. Before, a room asked during a timed exam to bring up the deck it had
  built had no way: the browser plug-in opens only web addresses in its own browser, a server started in the room's
  shell is not reachable from the PC, and a command to open the file through Windows was refused outside the room's
  sandbox.
- The new `show` tool, in every room: `path` the file, `screen` the monitor (left, middle, right, primary or its number
  from the left; else `[page] screen`, the live page's). yerkd opens it, since it runs outside the sandbox: on a WSL
  box, the file's Windows path, as `wslpath -w` names it, goes to Windows' own PowerShell in its environment, never on a
  command line, and opens in a new window of your default browser (Chrome, Edge, Brave, Vivaldi, Opera or Firefox),
  moved to that monitor, maximized and raised without taking the focus, the display woken with no click, as the live
  page opens. With no screen named, or on a box that is not a Windows PC, the box's own opener opens it. A reload there
  shows the file's newest save.
- Never a file the PC would run: only what a browser shows itself is opened, so a script, a program, a shortcut or an
  Office file is refused in words, a document with the way through `render` to a PDF. Only the room's own folders (its
  work folder and the files sent to it, never a secret), and only in your own rooms.
- Said in words, opened once at most: a screen that is not there, a browser yerkd opens no window of, a window that
  never showed, a locked screen.
- A file whose name, or a folder's above it, holds anything but a to z, digits, spaces and . _ - ( ) is refused in
  words, with the way (a copy under a plain name): the box's opener under WSL with wslu, wslview, puts the path inside
  PowerShell's double quotes, where a `$( )` in a name the room chose would run as a command on your PC outside the
  sandbox (the pull request's cold read).
- The ability is `show` (`yerk explain show`). The hands plug-in's browser, asked to open a file (a file: address or
  a path), still refuses, and its words now name `show` (its branch need-2026-11-13-91); its tool entries do not change.
- budgets.toml: `yerk_tools` 27,250 to 28,000 and the session start's total 130,773 to 131,523 for the tool's entry
  (about 761 characters as sent), and `[code] lines` 32,167 to 32,259, each with its dated note.

### texting-6 (#76)

- Nothing changes until an install has a `[texting]` table.
- In dry, each text that would go now waits its own time: a reply released by the owner's word goes 0 to 30 seconds after
  it; one that may go on its own waits as the person's sheet says the owner answers them, counted from their text, or
  2.5 to 4 minutes when the owner himself has just written in the thread; a text due in quiet hours to a person whose
  sheet holds at night waits for the morning, 10 to 90 minutes after they end. At that time the thread is read again,
  and the text is written as a job file under the state folder's `texting/dry/` only when every text it answers is still
  there as it was and neither the owner nor the person has written since. Its card says "would have gone" with the time,
  or "not sent: you answered first", "they wrote again" or "they took it back".
- In live, the same text is handed to the Mac's own runner over ssh, once, parts of a reply in order, and never after
  the owner's own text or a newer text of theirs. A text the Mac took and that never showed up in the thread is unseen,
  shown in status and on its card, and never sent again. Texting's own sends are told apart from the owner's: they
  answer nothing and withdraw no card.
- A decision made in live says so in its row and the dry file is written in dry only; the status line that said a live
  install still decides dry is gone. `yerk status` shows the last text out, how many are held and how many unseen, and
  `yerk doctor` checks the new keys.
- yerkd.toml `[texting]`: new keys `outbox`, `first_shot`, `first_shot_window`, `send_delay`, `quiet`, `morning`,
  `claim_seconds`, `stale_minutes`, each with its default in docs/settings.md.
- budgets.toml: `[code] lines` 31,718 to 32,167, with its dated note.
- **Upgrading**: nothing to change. `outbox` (the Mac runner's folder, a whole path) is needed before `mode = "live"`.

### idle-copy (#75)

- A room quiet for 25 minutes whose session holds 0.7 of its window or more is now compacted on a copy of the session in
  every room, as a room with fast mode on has been since yesterday. A message sent while the copy compacts is answered at
  once, in place of waiting under the card that says the conversation is being shortened, and what was said meanwhile is
  carried to the copy, which becomes the room's session as that turn ends. With no process alive (after a restart) the
  room's process is started first and a message sent during that start waits for it alone; a room with fast mode on still
  waits for its next turn, as before.
- What can still make a message wait under that card: the compaction when a turn ends right at Claude Code's own line (the
  last resort before Claude Code compacts inside the next turn), the one before a rerun when a turn overflowed, and
  `yerk room compact`, which someone asked for.
- budgets.toml: `[code] lines` 31,696 to 31,718, with its dated note.
- **Upgrading**: nothing to change.

### texting-6b (#74)

- Texting: nothing changes on an install with no `[texting]` table. With one, the check runs from a config folder of its
  own, `<state>/texting/check-config` (empty, 0700), in place of an account's, so it is handed the four inputs and no
  account's settings or address. It runs on an account's one-year token and passes over an account that has none; with
  none anywhere each draft is held with "the check needs an account token, and no account has one", and `yerk doctor`
  says the same while texting is on. A replay counts that hold as a call no model answered.
- `[texting] fallback` names a second model. When every account refuses the writer's or the check's model, the same call
  is made on this one in place of holding the text, and the row's account reads `<account> (<model>)`. With no key the
  text is held as before. A replay falls back the same way, and its `--model` flag still names the first model.
- budgets.toml: `[code] lines` 31,677 to 31,696, with its dated note.
- **Upgrading**: nothing to change; `yerk doctor` says so if no account has a token.

### texting-7a-asof (#71)

- `yerk texting replay`: the memory a replayed decision sees is held to the cut more tightly. A log named for its day is as
  of that day, as before. Any other memory file is as of its last change: a hit from one that changed at or after the cut
  is left out, and so is one whose name holds a later day anywhere in it. The first real run showed why: a people file kept
  up to date and the text of a screenshot filed minutes after a cut both held what the owner sent next, and two drafts
  matched his words because of it. The run's header says the writer now knows less than it did at the time.
- budgets.toml: `[code] lines` 31,673 to 31,677, with its dated note.
- **Upgrading**: nothing to change.

### texting-7a-fix (#70)

- A one-shot call (the texting writer and check, the judges) whose account answers "You've reached your <model> limit. Switch
  to another model" now rests that account for that model and goes on to the next one. One wording of it matched no rule, so
  the call stopped there with no answer; the other read as billing and rested the whole account. `failures.toml`: a new
  `model_limit` pattern ahead of `billing`.
- `yerk texting replay`: a cut whose call no model answered (refused, timed out, no account free) is written as failed and
  counted under `failed`, never as a held decision. `--model NAME` runs a replay's calls on that model in place of the report
  room's; the run's header and the first line of its file name the model.
- budgets.toml: `[code] lines` 31,657 to 31,673, with its dated note.
- **Upgrading**: nothing to change.

### texting-7a (#69)

- Nothing changes until an install has a `[texting]` table with a report room and someone runs the new command.
- `yerk texting replay --since YYYY-MM-DD` shows the plan and calls nothing: each person on the reply list who has a
  sheet, the one thread file of theirs under `imports/texts/` (by its name), how many cuts that is (each burst of their
  texts on or after that day, at most `--limit` a person, newest first, 10 unless asked) and how many model calls a run
  makes.
- With `--run` it puts each cut through the writer and the check as they run live, as of the cut's minute: the thread
  ends before the burst, the memory part is the report room's search with every line dated at or after the cut taken
  out, and the writer has no tool. A cut whose memory search does not answer is written as failed and never decided.
  It prints counts only, a row a person and the total (the decisions, each beside what
  was sent next, the check's verdicts, seconds and tokens), and keeps each decision beside what went in one private
  file under `<state>/texting/replay/`. Every cut is a real model call on the install's accounts, in the report room's
  order.
- It sends nothing and changes nothing the running install uses: not the ledger, the dry folder, a day log, a room or
  the account cooldowns.
- budgets.toml: `[code] lines` 31,256 to 31,657, with its dated note.
- **Upgrading**: nothing to change.

## 2026-11-12

### room-recall-pool (#68)

- A room that is not the owner's now finds its own lines about a name or a word that is common in the owner's memory. A
  person room, a sealed room and a room with its own `also` took the 80 best matches from the whole memory and only then
  dropped what they may not read, so where the owner's notes name someone in hundreds of places, none of the 80 was the
  room's and its assistant said nothing was on file about people in the room's own files. Such a room now reads on past
  what it may not read, at most 2,000 matches further, and deep recall on a name does the same. It still gets nothing it
  may not read.
- Such a room also asks the meaning search for 400 rows, the most it serves, where it asked for 80, and keeps those it
  may read.
- A search in such a room can take up to about a second longer when the word is common in the owner's memory
  (measured on a made-up memory with 20 rooms, and on a real one of 440,000 chunks with nine rooms: up to 1.2 s for a
  word the room has nothing on). An owner's room with no `also` searches as before: the same results in the same
  order, at the same cost. An owner's room that lists its own sources draws its pool from them too: on a real
  install's 177 recall questions the answer was in the top six as often as before (117), and at the top in 69 against
  70.
- budgets.toml: `[code] lines` 31,245 to 31,256, with its dated note.
- **Upgrading**: nothing to change.

### transcribe-voice-joins (#67)

- On a long recording a person keeps one letter through a silence. Where a voice said nothing through the minute two
  windows share and came back under a new letter, a short splice of each voice's speech from both sides of that break
  goes to Muse once more, in two orders, and in a third when those two leave a letter unsure. A new letter that two
  of them put with one earlier voice takes that voice's letter again. A splice that gives one letter to two voices a
  window heard apart is not used, and a letter never joins a voice a window heard apart from it; a window counts two
  letters as two voices when each spoke 5 seconds in it, since Muse now and then gives a voice a second letter for two
  or three seconds.
- The speakers line counts only the letters the bridges left unsure, and says `1 letter` for one. A letter that two
  splices heard with no earlier voice is a new voice and is not counted, nor are a recording's first voices. A turn
  Muse sends with no length is no turn (one had made a phantom first voice). The tool's result says how many letters
  were joined (`speakers_joined`).
- A few more seconds and a fraction of a cent a recording: the bridges are one more round of Muse requests after the
  windows (two when a third order is sent), each a spend row (`bridge 1 of 6, 41 s`), and are not in the US$1 gate's
  estimate, since they cannot be known before the windows are in. A bridge that fails or runs long joins nothing and
  never costs the words or the windows' letters.
- budgets.toml: `[code] lines` 31,083 to 31,245, with its dated note.
- **Upgrading**: nothing to change.

### transcribe-words-speakers (#66)

- A recording whose first pass heard only music now comes back with the second pass's words. Where whisper-1 wrote no
  words (a `♪` through minutes of a call) and Groq heard some, Groq's lines stand in with their own times, each marked
  `(second pass only)`, and the file's first line counts them: `checked by a second pass: 0 of 4 segments uncertain, and
  2 more heard by the second pass only, where the first heard no words`. A chunk the first pass heard no word in at all
  is the second pass's, whether it wrote marks or nothing. Where neither pass heard a word, the line stays as it was.
- Speakers come back on recordings past five minutes again. Muse gets 2-minute windows starting every minute, 16 at once,
  each given up after 100 seconds: Meta cuts a request at about 2 minutes, and on 2026-11-12 a 2-minute window took
  about a minute, where a 9-minute one had failed on every recording since 2026-11-10. Each second but the first and last
  minute goes to Muse twice, so an hour is about US$0.75 with all three engines (it was US$0.61), and the US$1 gate now
  asks from about 79 minutes (it was 98).
- With 2-minute windows a voice that says nothing through the minute two windows share gets a new letter when it speaks
  again, so on a long call one person can carry two or more letters. The speakers line counts the letters it could not
  match and says so. On the 28-minute call this was found on, six letters could not be matched.
- A slow Muse no longer holds the words for six minutes. A request that ran long (a timeout, or an error after 30
  seconds) is never sent again, on all three engines, while a quick busy answer still gets three tries; after one Muse
  window fails no further window starts, so the words come back once the running ones end, in about 100 seconds when
  Muse answers nothing. The speakers line names the code and the seconds (`HTTP 504 after 121 s`), never an error page.
- budgets.toml: `[code] lines` 31,044 to 31,083, with its dated note.
- **Upgrading**: nothing to change.

### texting-5 (#65)

- Nothing changes on an install until it has a `[texting]` table in yerkd.toml.
- Then a text that may not go on its own (one the writer or the check held, or one that passed for a person whose
  standing release is off, the default) is one card in the owner's texting room (`report`): who, their texts, the exact
  draft, why it waits, and the words. His reply `send` to the card sends exactly that text (queued for the sender),
  `edit <text>` makes a new card from his words with the code's pre-check shown, and `skip` drops it. A bare `send` or
  `skip` counts only while that card is the one hold open in the room. When he texts the person himself the card is
  edited to say he answered and nothing new is posted; their next text is decided afresh. A newer text of theirs edits
  the open card in place, and a card older than `hold_hours` says expired and cannot be sent.
- In dry mode the cards go to the `dry_report` room, marked DRY, and `send` only says it would; with no `dry_report`
  nothing is shown. `report` and `dry_report` must be owner rooms without people: any other is refused at the start
  and shown nothing (`yerk doctor` says so in red, and red too when live with `text-out` in `[gates] off`).
- A bare `approve` never approves a text's hold; every other hold works as before.
- New `[texting]` key: `hold_hours` (default 6). The ledger's `texting` table gains a `shown` column, added at the next start.
- budgets.toml: `[code] lines` 30,680 to 31,044, with its dated note.
- **Upgrading**: nothing to change.

### texting-4 (#64)

- Nothing changes on an install until it has a `[texting]` table in yerkd.toml.
- Then, in dry mode, each reply or tapback the writer decides gets a verdict from a pass that reads only the thread,
  the person's sheet, their texts and the draft: one `claude -p` with no tool, no MCP server, no settings and an empty
  folder, on the writer's model and accounts. The code holds first, with no call, a draft in which the writer names
  itself ("as an AI", "auto reply") or that holds a name listed in `never_say`, has markdown, more than 3 texts or one over 400 characters, or a word of an always-held class the sheet
  does not open. It releases only when the pass fails nothing, leaves nothing unlisted and gives every statement about
  the owner a basis the code confirms: a row of his side in the thread, a key of the sheet, or a topic row of his. An
  error, a timeout or a missing field is a hold.
- The verdict goes on the decision's `texting` row (state `release-ready`, or decision `held` with the reason) and one
  more line of the dry file. Nothing is sent or shown.
- New `[texting]` keys: `check_model` (default the writer's model), `check_seconds` (default 120), `never_say` (the
  names an assistant goes by on this install, default none; a topic such as AI or a bot is never held by its word alone).
- budgets.toml: `[code] lines` 30,462 to 30,680, with its dated note.
- **Upgrading**: nothing to change.

### texting-3 (#63)

- Nothing changes on an install until it has a `[texting]` table in yerkd.toml.
- Then, in dry mode, each burst of a listed person's texts gets one decision: reply (one to three texts), react, none
  or held, with its reasons and a source for every statement about the owner (a thread line of his, a memory cite or
  the person's sheet). It is made by one `claude -p` on the report room's model and account order that reads the owner's
  memory as the report room does, through that room's recall and read_memory alone, with no other tool. A statement
  with no source, a class the person's sheet keeps shut, no clean answer, a call past `decide_seconds` or no account
  free is held. A burst older than `catch_up_hours` when its turn comes is missed, never decided.
- Each decision is written to the ledger's `texting` row and one line of `<state>/texting/dry/YYYY-MM-DD.jsonl`, and
  nothing is sent or shown: no text, no room, no day log line. With `mode = "live"` decisions are still dry until
  texting can send, and `yerk status` says so.
- New `[texting]` keys: `model` (default the report room's), `effort` (default high), `parallel` (default 2),
  `decide_seconds` (default 240).
- budgets.toml: `[code] lines` 30,191 to 30,462, with its dated note.
- **Upgrading**: nothing to change.

### texting-2 (#62)

- Nothing changes on an install until it has a `[texting]` table in yerkd.toml.
- Then each person on the reply list may have a sheet beside it (`people/<key>.toml`, or the `sheet` the list names):
  the always-held classes open for them (health, medication, substances, faith, money, job, dating, location), facts
  they know, at night hold or answer, tapbacks, the share left unanswered, reply times and the owner's voice card for
  them. A missing sheet is no voice card and every class held; one that does not load keeps texting off.
- The report room (`[texting] report`) has a `texting` tool: it reads a person's sheet, a day of their texts and
  decisions, or the newest reason; and it opens or closes a class, adds or drops a fact, or turns a person's standing
  release on or off, only when the owner's own message in that turn holds the words it passes. Each change is one line
  of `sheets.log` beside the reply list. The tool's entry, which every room is sent, says only that it is the owner's
  texting settings and that `help` in that room says the rest; another owner room is told which room, and a person's
  room gets one fixed line that names no room, the same with texting on or off. `said` is three words or more.
- `yerk texting sheets --check` names a sheet that does not load, a sheet missing and a file no one on the list names,
  each with its file (and its line where the sheet gives one); it writes nothing.
- budgets.toml: `[code] lines` 29,919 to 30,191, and `yerk_tools` 26,700 to 27,250 with the session total 130,223 to
  130,773, each with its dated note.
- **Upgrading**: nothing to change.

### texting-1 (#61)

- Nothing changes on an install until it adds a `[texting]` table to yerkd.toml: with no table, or `mode = "off"`, Yerk
  opens no connection to the Mac, starts nothing and writes nothing for texting.
- With `mode = "dry"` Yerk reads the 1:1 threads of the people in the reply list (`people`, a file under the private
  folder only the owner can read) on the Mac's bridge, reached as the `text` tool reaches it, and writes each text down
  once, across reconnects and restarts: a ledger row by its id and a private line in the day's log, which no person room
  can read. Their texts close into one pending decision per burst (`burst_quiet`, `burst_cap`); a text older than
  `catch_up_hours` is listed once as missed, in dry mode only in the `dry_report` room and never in `report`; a text
  followed by the owner's own is his. Nothing is decided and nothing is sent: the connection refuses every method but the
  three reads before anything reaches the Mac.
- `yerk status` shows texting's mode, the bridge up or down since when and the last text in; `yerk doctor` names a
  `[texting]` table that keeps texting off (a number, the reply list, the report rooms).
- The machine room hears one line once the Mac's bridge has been away 5 minutes and one once it has been back 2.
- budgets.toml: `[code] lines` 29,359 to 29,919 with its dated note.
- **Upgrading**: nothing to change.

### fast-idle (#60)

- In a room with fast mode on, the compaction that runs while the room is quiet (`idle_compact_after` minutes with the
  session at `idle_compact_at` of its window) never runs in fast mode: a copy launched without it compacts off to the
  side and is taken, as a turn's copy is, and the next reply is fast as before. A message sent meanwhile is answered at
  once on the room's fast session and is carried to the copy. With no process alive to copy (after a restart, or once
  the room's process closed) it waits for the room's next turn rather than start a fast process for itself.
- Left in fast mode, since someone is waiting behind them or asked for them: the compaction as a turn ends near Claude
  Code's line, the one before a message that overflowed runs again, and `yerk room compact`.
- A room with fast mode off, or on a model other than Fable 5.2, compacts while quiet as before.
- budgets.toml: `[code] lines` 29,338 to 29,359 with its dated note.
- **Upgrading**: nothing to change.

### compact-size (#59)

- Right after a compaction, `/status` in a room shows the session's real size, not the few thousand tokens the
  compaction left of the conversation: the 📚 line, the compaction's ledger row and the number a daemon start reads
  back are what Claude Code counts for the session's next call (the summary, the system prompt with the room's context,
  the tools), asked of it with no model call. A copy compacted off to the side and taken gives the room its own count
  the same way. Before, the line read about 12k all night after a late compaction while the next call carried about
  328k, until the room's next reply.
- budgets.toml: `[code] lines` 29,299 to 29,338 with its dated note.
- **Upgrading**: nothing to change. It needs a Claude Code that answers `get_context_usage` (2.1.321 does); an older
  one shows the old number.
  A room whose newest row is a compaction written before this version shows the old number until its next reply.

### fast-stays (#58)

- A room with fast mode on is fast again from the first reply after a compaction, with nothing typed and nothing said:
  the copy the room takes was launched without fast mode, so it is closed as it is taken, and the next message resumes
  the compacted session on a process launched with it. The copy's compaction still never runs in fast mode. This
  replaces "Once a copy is taken, the room runs at standard speed until its next new process." in fast-mode (#51).
- A room with fast mode off, or on a model other than Fable 5.2, goes on on the copy it took, as before.
- budgets.toml: `[code] lines` 29,286 to 29,299 with its dated note.
- **Upgrading**: nothing to change.

## 2026-11-11

### oom-wording (#57)

- A worker the kernel killed for memory says so: its card reads "died: it ran out of memory", and so do its relay, the
  ledger's exit detail and a scheduled job's alert, where they read "died: it was killed". Under the memory ceiling
  (OOMPolicy=continue) systemd reports that death as a kill by signal, so a worker's exit record now also holds how many
  of its processes the kernel killed for memory (exit.json's `oom_kill`, read as the unit ends), and the words go by it.
- A worker that lost a process to memory and went on (a test run past the ceiling, then a clean finish) says so in one
  line, "one of its processes ran out of memory" (the number when more than one), on its card, in its relay, in the
  ledger and in a scheduled job's alert or relay. A worker that lost none reads as before.
- A death for memory is never taken for a stop nobody asked for: on 2026-11-11 a fix that ran out of memory (oom-kill,
  exited 143) was started again as if the box had shut down. A fix or a cold read that died for memory now holds its
  need with that reason, and a builder that did is reported failed with it and held, not built again by itself.
- budgets.toml: `[code] lines` 29,239 to 29,286 with its dated note.
- **Upgrading**: nothing to change.

### status-broom (#56)

- The room card's broom (the second line of `/status` typed in a room) is the room's compactions today, counted from
  the ledger when the card is collected: `📚 606k/900k (67%) · 🧹 2 today`. In status-card it was the live session's
  compactions since it began, kept in memory, so after every daemon restart (each deploy) it was not known and the card
  left it out until the room's next new session. Now a restart keeps it, a room with none today reads `🧹 0 today`,
  and the daemon's /status answer gives every room's `head` a number for `compactions`.
- All three kinds count, each once: a held or idle /compact and a compacted copy taken (rows the ledger already marks
  `compacted`), and Claude Code's own compaction inside a turn, now marked on that turn's row with the note
  `compacted in the turn` (the ledger's note column, so no migration). A dropped copy, another room's and yesterday's
  do not count.
- budgets.toml: `[code] lines` 29,244 to 29,239 with its dated note (29,218 to 29,213 before worker-memory was merged in).
- **Upgrading**: nothing to change. The card counts the day from yerkd's next start; a compaction inside a turn before
  that start was never marked, so that day's count leaves it out.
### worker-memory (#55)

- Every worker's unit has a memory ceiling, 4G unless `[workers] memory_max` says otherwise, with no swap past it (the
  owner's order of 2026-11-11: a worker reproducing an out-of-memory bug took 16G of RAM and 3.2G of swap on a 20G box,
  and the box froze for 76 seconds). Past the ceiling the kernel kills the process using the most memory inside that
  worker, never one of the box's, and systemd does not stop the worker for it (OOMPolicy=continue): a test run that
  blows up dies alone and the worker goes on. The time cap, the sandbox and how a worker's end is read are unchanged.
- `[workers] memory_max` in yerkd.toml: a whole number of bytes, or one with K, M, G or T after it (any case). Any other
  value (a word, a fraction, a percentage, infinity, zero) is never handed to systemd: the log says it is not a size and
  every worker gets 4G. docs/settings.md lists the key.
- budgets.toml: `[code] lines` 29,218 to 29,244 with its dated note.
- **Upgrading**: nothing to change; the ceiling holds for every worker started after yerkd's next start. An install
  that capped its workers with a systemd drop-in (a `yerk-worker-.service.d/` folder in ~/.config/systemd/user/ setting
  MemoryMax) can remove it and run `systemctl --user daemon-reload`, so yerkd.toml is the one place that states the
  number: while the drop-in is there, systemd applies its number over the one Yerk AI gives.

### need-2026-11-11-87 (#?)

- A room reads a PDF or a Word file at a web link now: a form, a notice or a report someone links. web_fetch named any
  such link a binary wall ("these tools read no other binary file"), so a room had the page that links to a document and
  never the document.
- web_fetch keeps the file's bytes where it named that wall: the plain read from this machine takes one up to 25 MB,
  known by its own first bytes (a .docx, whose type says "xml", was read as text before); a site that refuses that read
  is read from Browserbase Fetch's answer, at its price, as any page is. It saves the file in the room's work folder,
  next to the pictures web_image saves, and answers with its words as it answers with a page's: a PDF's page by page,
  a Word file's (.docx, .doc and .odt) a paragraph a line with each table row's cells side by side and a check box as
  ☐ or ☒. The answer names the saved file: Read opens a PDF's pages (a form's boxes and ticks), and `render` makes a PDF
  of a Word file.
- A PDF with no text on any page is said as a scan, to Read; a dynamic XFA form, whose pages show only Adobe's "Please
  wait..." placeholder, is read from its own template (each caption, label and fixed text, a list's choices). A file
  that will not read (a password, no pdftotext) is still saved and named, with why. Nothing new to install for Word
  files; a PDF's words need poppler-utils (pdftotext), already on most Linux boxes.
- web_fetch's description gains one sentence (152 characters), so budgets.toml's `deferred_tool_names` line goes from
  3,429 to 3,581 and the session total from 130,071 to 130,223, the dated reason beside them. `yerk explain
  web-documents` has the ability, found by asks such as "pdf at a link" and "the form's pdf"; the README's Add-ons line
  names it, the Install line's note on systemd units left to docs/units.md so it stays under 12,000 bytes.
- Why: on 2026-11-11 a room asked what a province's form is and what it asks for tried web_fetch on the form's own Word
  file and on two PDF copies of it, met the binary wall each time, and answered from the province's forms list page and
  the search previews of a copy, saying so. It filed the need (need 87 of 2026-11-11) rather than work around the wall.
- A crafted file costs only what its own bytes hold (the review's fix): the file-type check runs on every binary read,
  and the readers run in the web plug-in's one process, which serves every room. A 64 KB file at any link made the
  check ask for 16 GiB of memory (a looping chain in a Word 97-2003 header), a 4.6 KB one walked its table of contents
  2^n ways (8 seconds at 26 deep, with 64 allowed), and a .docx or an XFA PDF of about 1 KB, which unpacks to a
  megabyte of tags that never close, of one long name or of spaces before a letter, held the plug-in for about ten
  minutes. Now the check takes only 512- or 4,096-byte sectors, follows each chain through each sector once, and walks
  each entry once. It also reads at most 1 KB of an OpenDocument file's type. The XML readers stop a tag at the next
  "<" and find each closing tag in one pass, the trailing spaces are cut by hand, and an XFA form's streams together
  unpack to 256 MB at most. Every one of those files reads in milliseconds now: three tests in
  plugins/web/test/tools.test.mjs, which fail or hang on the code before the fix.

**Upgrading**: budgets.toml moves two lines (above); nothing in rooms.toml or yerkd.toml changes. The web plug-in is in
this repository, so the deploy carries it: a room reads documents from its next process start. Keep poppler-utils
installed for a PDF's words (`apt install poppler-utils`); without it a PDF is still saved for Read, and the answer says
why its words are missing.

### status-card (#54)

- `/status` typed in a room answers with that room's own card, in the shape of the box's own `/status` card, instead of
  the whole machine card (31 lines on the owner's install the day he asked, too long to read on a phone; his order of
  2026-11-11). Three lines about the room: the model its next reply launches with and the account its session is on
  (with no live process, the one its next turn would try: its own unless that one rests);
  its session's tokens over its compaction window and how many times it was compacted since it began; its thinking
  level, ending ` · Fast: on` while fast mode is on.
- Then the tank, two lines an account in number order: the account the room is on wears `➤`, the soonest weekly reset
  the green mark as before, a rest until other than the account's own weekly reset ends its first line with `💤` and
  the time, then `wk next:` (and `5h next:` when a reading has a 5-hour window). Then what runs, at most four lines (this
  room's turn first, then other rooms', then the workers, the fourth "and n more" past that), or "Quiet, nothing
  running"; today's first-line and turn medians; on an uncapped install today's and the week's dollars; and a red line
  only for trouble: stalls today, no backup or a stale one, a timer list that could not be read.
- A part that cannot be read truthfully is left out: a room whose window is left to the model shows its tokens alone; a
  session resumed at a daemon start (its earlier compactions never seen) shows no compaction count; a model or thinking
  level nothing names (Claude Code's own default, whose id Yerk never learns) is not shown. A room with no session yet,
  or a `/new` waiting, reads "new session". The card answers with the room's process closed too.
- `yerk status` on the command line is unchanged, byte for byte: the operator's machine card. The daemon's /status
  answer (and `yerk status --json` while yerkd runs) carries each room's `head`: model, effort, account, context, window
  and compactions.
- budgets.toml: `[code] lines` 29,066 to 29,218 with its dated note.
- **Upgrading**: nothing to change. The card shows from yerkd's next start, and each session's compaction count starts
  with the first new session after it.

### need-2026-11-11-86 (#?)

- A room can click inside a web app whose own top bar offers a paid plan now, such as your mail app with its blue Upgrade
  button: a search's rows, a mail's file chip, the search box. The hands plug-in's browser read a button whose whole words
  are Upgrade, Subscribe or Start free trial as a pay button wherever it showed, so the whole mailbox was a payment page
  and every click in it was refused, your word or not.
- Such a button makes a payment page now only inside a form, a dialog or a box (a frame of the page's own site), where it
  is a plan's last step and an Enter in the form can press it; the form or dialog is looked for up through each web
  component's host too. One in the app's own frame (a top bar, a side bar, a banner) makes none. A button whose words make
  a payment (Pay, Place order, Buy now, Checkout, Donate, Submit payment) still makes a payment page wherever it shows, as
  a card field, a payment frame, a saved card and a payment service's address do.
- The button itself is never clicked, by its words or by position, your word or not, and neither is any pay button, a
  hidden one a click by label still finds included: "is a pay button (its words offer a paid plan), and hands never clicks
  one, confirmed or not". A link that offers a plan asks for your word, as before.
- The plug-in's change is on its branch `need-2026-11-11-86` (1de35ea): 86 of 86 of its offline tests green, one running
  the page probe in Playwright's headless shell against pages it writes, whose mail app read as a payment page on its main
  1c6df9d. Its tool entries are unchanged (10,072 bytes as built, as on main), so no budgets.toml line moves. `yerk explain
  upsell-pages` has the ability, found by asks such as "click inside my gmail" and "read as a payment page"; the README's
  Add-ons line names it beside `free-bookings`, at 11,992 of its 12,000 bytes, the Install paragraph no longer repeating
  the docs list's line on docs/units.md.
- Why: on 2026-11-11 a room asked why it could not see a file one of your emails carried opened your mail app in the
  browser, found the mail with its file on screen, and was refused the click on the file as on a payment page. It filed the
  need (need 86 of 2026-11-11) and asked you to send the file into the room, rather than work around the wall. The file
  itself comes by the mail tool's `attachment` (need-2026-11-11-85), with no click at all.

**Upgrading**: merge this before, or with, the plug-in's deploy, which fast-forwards hands-mcp's main to
`need-2026-11-11-86` (1de35ea). A room clicks in such an app from its next process start after that. Nothing in
rooms.toml, yerkd.toml or budgets.toml changes.

### fast-mode (#51)

- `/fast` in a room's own channel turns Claude Code's fast mode on or off for that room, from the next reply (the
  owner's order of 2026-11-11). Fast mode bills usage credits, never the plan, so it reaches Claude Code only in a room
  where the owner typed `/fast on`, and only while that room runs on Fable 5.2 by its full id, `claude-fable-5-2`
  (`/model fable` stores it). On an Opus model the launch setting alone turns fast mode on, so no Opus process ever gets
  it; no worker, job, curate run, beat, health check, evidence judge or need gets it either.
- `/fast on` first asks Claude Code itself, on the account the room is on now, with the local command `/fast on` (no
  model request, no cost), and keeps the setting only when it answers on: "fast mode on from the next reply", "billed to
  usage credits, not the plan". Refused, nothing is kept and the line says why: "usage credits are off on account
  five", or Claude Code's own reason. In a room on another model: "fast mode is for Fable 5.2 rooms" and the room's
  model. `/fast off` says "fast mode off"; `/fast` alone says the room's setting and what Claude Code last reported. A
  switch off Fable 5.2 drops it, and the settings line ends "fast mode off".
- A room with it on launches with `fastMode` in its settings file and sends `/fast on` before the first message of every
  new process (fresh, resumed, or after a hop), never shown, ledgered or counted. A refusal there (a hop to an account
  with usage credits off, credits run out mid-session) is said once in the room, and the reply still comes at standard
  speed; a cooldown is no refusal. A `/fast on` there that gets no result, or fails, ends that process: the turn runs on
  one launched as before, and the room is told once. `/status` says "fast mode on:" and the room while it is on. A room
  with it off launches as before.
- The copy a compaction runs on never gets fast mode, whatever the room's setting: fast replies were asked for, not a
  fast compaction billed to credits. Once a copy is taken, the room runs at standard speed until its next new process.
- While a room has it on, every turn in that room's process runs fast, a job's message into the room included: fast
  mode is the session's, and switching it around one message would read the conversation again at the fast rate.
- The check leaves nothing behind: the empty folder Claude Code makes under the account's `projects/` is removed. A
  Fable 5.2 model that is not found drops fast mode with it, as `/model` does.
- budgets.toml: `[code] lines` 28,898 to 29,066 with its dated note.
- **Upgrading**: nothing to change. The rooms' slash commands are registered again at the next start, `/fast` with
  them. No account has usage credits on today, so `/fast on` says so until the owner turns them on for an account.

### need-2026-11-11-85 (#?)

- A room can read a file one of your emails carries now: a PDF quote, a picture, a document. The hands plug-in's mail
  `read` numbers the files a mail carries, each with its type and size (`Attachments: 1. quote.pdf (application/pdf,
  412 KB)`), and its new `attachment` action saves one by that number (`number`), or every one, in the room's own work
  folder, where the room opens it with Read. Before, a read named a file and nothing reached it: the mail tool neither
  saved nor opened one, Yerk's `attachments` tool takes a Discord message's files only, and a room's shell has no
  network, so the room could only ask you to send the file into the room.
- The plug-in's mail reader writes the file from the same read-only fetch (nothing is marked read, moved or deleted),
  under `hands-mail/<mailbox>-<uid>/`, by the file's own name made safe: anything a path could carry becomes `_`, a
  second file with the same name gets ` (2)`, and a file with no name is named for its number and type. A worker saves in
  its own folder. The path comes back inside the mail's untrusted wrap, since it ends in a name the sender wrote, and
  what the file says is the mail's, never an instruction.
- The reader walks to that folder a part at a time without following a link, and makes each file under a new temporary
  name before renaming it into place, since a room can write its work folder and the plug-in runs outside the room's
  sandbox. A link where the folder goes is refused by name, with nothing written where it points; a link at the file's
  own name is replaced, never written through. A mail with no files, a number past the list and a room whose work folder
  is not known are each said in one line.
- The plug-in's change is on its branch `need-2026-11-11-85` (1c6df9d): 83 of 83 of its offline tests green, the three
  new ones failing on its main b85fc18. `yerk explain mail-files` has the ability, found by asks such as "attached to the
  email" and "attachment in my gmail"; the README's Add-ons line names it beside `mail-links`, at 11,999 of its 12,000
  bytes.
- budgets.toml: the hands line 9,831 to 10,131 (the mail entry 1,494 as the plug-in's branch builds it, 1,525 as sent)
  and the session-start total by the 300 to 130,071, each with its dated note; docs/budgets.md is what `yerk docs own`
  writes.
- Why: on 2026-11-11 a room asked to confirm the figures on a quote an email carried as a PDF read the mail and got the
  file's name only. It filed the need (need 85 of 2026-11-11) and asked for the file in the room, rather than work
  around the wall.

**Upgrading**: merge this before, or with, the plug-in's deploy, which fast-forwards hands-mcp's main to
`need-2026-11-11-85` (1c6df9d). A room has `attachment` from its next process start after that. budgets.toml's hands
line is 10,131 and the session `total` 130,071; keep yours at least that, or the deploy's fit check refuses on the longer
mail entry. Until the plug-in's deploy a room sends that entry at its old 1,225, under the new line.

### his-four-2026-11-11 (#49)

- Four changes the owner said yes to on 2026-11-10, after the cleanup audit put them to him as questions. Each is in
  commits named `cleanup <finding id>: ...`.
- A need's number never repeats (F5-01). A new need is numbered past every number any need has held and past its day's
  page; the count used to restart each day, so two different needs could both be "need 81". The needs filed before
  this that share a number are named with their day in every line ("need 81 of 2026-11-10 held"); a number one need
  holds reads as it did. Nothing is renumbered, and `yerk needs build DAY N` and `yerk needs done DAY N PR` take what
  they took.
- The `reverse_image_search` tool is gone (F6-06). No room ever called it and its key was never on the box, so a call
  could only answer that the key was missing, while every room carried its description in every request. Yerk AI's
  own server hands a room 22 tools. The `delete` tool stays (F6-02).
- `web_search` takes its filters again (F7-13): `freshness` (day, week, month or year), `date_after`, `date_before`,
  `domain_filter` (up to 20 domains to include or, each with a leading -, to exclude) and `country`. A value the tool
  cannot use is refused in one line before any engine runs. The engines differ in what they enforce, so the answer's
  first line says it: ", filtered by freshness week" for what the answering engine enforced, "; not enforced by this
  engine: before 2026-11-08" for what it dropped or only worded, and ", localized to CA" for a country, which
  localizes the results and keeps no page out. With no filter the answer is what it was.
- One way to write a reset time (F2-11), the status card's: '5:10p' within a day, 'Fri 10a' within a week, 'Nov 20'
  past that. `yerk status --full`, `yerk accounts`, `yerk accounts cool`, /model's resting state, the card's note when
  every account is out of a room's model and the morning diff's account line wrote '17:10', 'Fri 10:00' or the weekday
  alone. The morning diff's account line stays one line where its resets fit and takes the card's two where they do
  not; the card's note, too wide, loses the end of the name of the model the turn runs on, never its time.
- budgets.toml: `[code] lines` 29,052 to 28,898 with its dated note.
- **Upgrading**: nothing to change. A room has the new `web_search` and is without `reverse_image_search` from its
  next session.

## 2026-11-10

### cleanup-2026-11-10 (#?)

- The cleanup audit of 2026-11-10: Yerk AI read in eleven regions, every finding judged, and each fix here in a commit
  of its own named `cleanup <finding id>: ...`, so a line below leads to its commit. Pull requests 20 (F6-04, Fish
  only), 24 (the card default) and 31 (F9-03, `fable` is claude-fable-5-2) are folded in; their own entries follow this
  one.
- Fixed, in turns and rooms (F1-03, F1-05, F1-06, F1-07, F1-08, F3-01, F3-03, F3-05): a failed turn is classed by its
  own process's stderr, never by a line an earlier process left there. A message that comes in while a room's idle close
  waits for the old process starts once that process has exited, where it used to crash on the session lock. A copy's
  compaction no longer clears the live session's record of the topic rules it was told. After a refusal fallback the
  room starts fresh only when failures.toml's refusal action is `fresh_next_turn`, the shipped default, so the file now
  decides. A machine-room line that fails to post is said on the worker's row, never recorded as sent. A cite past a
  vertical tab in a memory file opens and marks its own line, since read_memory and a correction's edit now number lines
  as the index does. An index rebuild reloads yerk-index's store with the install's own `embeddings_units`. The
  assembled log rolls to its next part and never writes over one; the nightly sweep cuts every part into its day in
  order, and curate reads the whole week's parts.
- Fixed, in the needs pipeline (F5-02, F5-03, F5-07, F5-08, F5-10, and the pipeline builder's own find): a stuck need
  with a pull request goes back to `pr` once its head's checks are all in and final, and only when no other need is in
  flight. A need's worker cut off by a stop nobody asked for, a shutdown say, is no verdict: the need is queued again
  with nothing counted and the next tick starts it; a stop a person asked for stays a stop. A deploy that gave up
  waiting for quiet sets its need back to merged, so the next tick deploys again. A builder that cannot start holds its
  need with the reason, said, and the next queued need starts. A need worker's clean worktrees are removed when it ends;
  its branches are kept, and a worktree with changes is named on its row. A need left building with no worker after the
  daemon died goes back to the queue at the next start, where it used to hold the queue for good.
- Fixed, in jobs, retention, the doctor and curate (F4-01, F4-03, F5-05, F5-06, F9-21, F10-03, F10-04, F10-14, FB-01): a
  jobs sync no longer deletes the unit file of a one-shot that is still running, and yerk status and the jobs list read
  a running one-shot the same way. An agent job's run waits for a daemon that is not listening yet, up to its unit's
  limit; a script or a note gives up at once as before. The start sweep closes a script run that was cut mid-run and
  sends its failed-run alert. Retention moves a job run's folder to the trash 14 days after it ended and was delivered,
  never one still undelivered, and a finished worker's pytest temp trees a day after it finished, the rest of its folder
  waiting its 30 days. The doctor's growth check and retention's lines count disk blocks, once per file, so sparse files
  and second hard links no longer read as growth. Curate's nightly run keeps the day's rule proposals it built, so
  `--apply` takes the numbers the card showed. A blind recall bench run exits 5 and fails the weekly unit, where it
  passed as a success.
- Fixed, in the gates (F5-09, F5-13, F5-14, F5-15, F5-18, F5-19): the restart rule and the hardline rules read a shell
  line as it would really run, so a write to a start-up file in a nested shell, by a relative name or after a heredoc is
  seen, and a wrapper or a nested shell is no way round them. `((` and `$((` read as arithmetic only when their last two
  parentheses close together, as bash reads them, and `coproc` is peeled so the command it runs is read. The git askpass
  helper answers only a prompt for github.com. An add-on whose install is not a table is refused with its check line,
  not a traceback.
- Fixed, in the web plug-in (F7-02, F7-04, F7-10, F7-14, F7-15) and `yerk room add` (F7-05): `web_fetch` refuses a
  browser rung that an ad or a script sent off-site, and ends its climb 10 s inside the room's own limit. A proxied
  session retried without proxies is booked as a spend row of its own. The rendered-page check takes every og:title as a
  title candidate. The plug-in's plan-limit alerts, which went to log files nothing read, reach the morning diff once,
  the day after they are written. `yerk room add` names the new room in the MCP flags it copies, never the room it was
  made like.
- Fixed, in the install (F10-01, F10-12, FB-03): yerkd's unit is sized for what its rooms hold today, MemoryHigh 4G and
  MemoryMax 8G (from 2G and 4G; a nine-room install sat at its 2G line), and yerk-index's MemoryHigh is 2G (from 1400M),
  its rule and a fifth for what a start costs; each unit file carries its measurements. The MCP library's per-request
  lines, two thirds of the daemon's journal, stay out of it.
- What a person reads differently (F1-01, F2-02, F2-03, F2-09, F2-13, F4-02, F4-07, F4-09, F5-04, F6-05, F6-08, F7-12,
  F7-16, F9-07, F9-12, F10-11, F11-08): a watched room's audit verdict keeps orange for something actually wrong that
  changes what the person would say or do; green is looks good, with a nit in one line under it or none, and red is
  wrong, then what is wrong and the fix (the owner's order of 2026-11-05; the `watch_audit` setting's words say the
  same). A stuck need's line counts the final checks and names the pending ones. `yerk status` names a watched channel
  by its name where it is known, its help names every part of the card, and `--full` says no workers in the shared
  words. The live page's close button says answers come back as bubbles under the line. The doctor's backup lines carry
  the red mark; curate `--rules` names all six kinds it proposes, and its dry run speaks from words.py. A command that
  falls back to the example layer says so once on stderr, with the flag to pass. The hold tool says what is true: a
  worker's result comes back by itself, and nothing watches another event. The login wall names a move a room can take,
  `web_research` names the configured daily cap, and prompt-diff says built, with the eight abilities that ship in part
  naming what is still missing. The accounts help names `mint`, `[gmail]` smtp_host and smtp_port are described, and the
  hooks page lists all eleven Stop rules.
- The guide and the other written pages say what the code does (F6-03, F8-03, F8-04, F9-05, F9-06, F9-08, F9-09, F9-10,
  F9-11, F9-13, F9-14, F9-19, F9-20, F9-22): the docs fetch is daily; the ability counts match abilities.toml (118: 56
  built, 62 described); the Ledgers section names all 22 tables and the Files section every kind of unit; every route of
  the daemon's HTTP server is named; the location-day job example is one the loader accepts; the doctor prints one green
  line when all is well; a script job keeps the unit's YERK_PRIVATE and YERK_STATE; the contract probes run by hand and
  take short and think; the hello add-on points at the guide's Add-ons section; the example ability cards that wait on
  the owner open on the orange mark; NOTICE no longer credits the watcher job that left. Docstrings and comments that
  said otherwise now match the code (F1-04, F2-05, F2-12, F5-16, F9-01, F9-02).
- Faster (F2-15, F3-04, F3-06, F4-04, F6-07, F8-07, F8-09): `words.line` cuts a long line in one pass, not a character
  at a time (one card had cost 11 s of CPU). The Stop check's figure test is 32 times faster, with the same answers. A
  message reads only today's day log; a past day's parsed lines are kept while the file's size and time hold. Each
  ability phrase's pattern is compiled once, not 540 times a message (8.3 ms to 0.3 ms), and the abilities manifest is
  parsed again only when abilities.toml or an add-on changes. A reply's parts are read by key range, never by a LIKE
  over every sent delivery of every room. The doctor's growth check adds sizes as it walks and holds no list.
- Gone (F1-02, F1-09, F2-01, F5-12, F7-03, F7-11, F8-01, F8-02): the `kind` arguments of the channel's open_card and of
  Card, which every caller left at its default; workers.ADDON_ENV, which nothing read; the recall cap's dead fallback of
  1500, as the prompt hook reads budgets.toml alone; eight web plug-in exports nothing calls, and its site-memory seed
  merge, an OpenClaw deploy step nothing in Yerk writes; a skipped test nothing could run and a test constant nothing
  read.
- The tests: 46 new test functions (1,434 to 1,480) and 11 in the web plug-in's suite. The style test also holds the
  status card's phrases and the live page's server to the one vocabulary (F2-04, F2-14); pytest keeps only a failed
  test's temp folder, where a whole run left 340 MB of passed tests' folders (F8-05); the screen test waits 0.3 s, not
  10, for a paint no tab gives (F8-08).
- budgets.toml: `[code] lines` 28,654 to 29,052 with its dated note.
- **Upgrading**: copy `units/yerkd.service` and `units/yerk-index.service` again (yerkd MemoryHigh 4G and MemoryMax 8G,
  yerk-index MemoryHigh 2G) and `systemctl --user daemon-reload`; a box that set those with `systemctl --user
  set-property` raises them there instead. The doctor's and the recall bench's units changed only in their comments.
  Nothing else to change for the cleanup itself; pull requests 20 and 24 say their own steps below.

### fish-only-2026-11-07 (#?)

- MiniMax is out and Fish Audio is the only voice engine, since the owner cancelled the MiniMax plan on 2026-11-07. Left
  as it was, every note would have spent a failing MiniMax call first, and the abilities text and the guide would have
  named a voice that no longer speaks. `[mood]` tags go to Fish as they did, `(break)` is its pause, and a `<#0.4#>`
  written in a script still becomes `(break)`.
- A Fish failure on any section means no note, and the error names fish. The words for it change from "neither voice
  made it" to "the voice failed".
- A note costs its 20-second check, about 0.2 US cents, since Fish's s2.1-pro-free is US$0. The US$1 gate stays, and no
  note reaches it at that price. Fish takes about a minute per 1,000 characters, section after section (measured on the
  box, 2026-10-22), so a long note takes minutes.
- `voice/pronounce.txt` in the private folder is no longer read: those rules were MiniMax's.
- The `[code]` ceiling goes to 26,959.

**Upgrading**: `secrets/minimax.key` is no longer read and the owner may remove it; no other step.

### fable-5-2-2026-11-09 (#?)

- `fable` in models.toml names claude-fable-5-2, released Nov 9 2026 and the default Fable model in Claude Code 2.1.321; a room put on `fable` from now on runs it. `fable51` names claude-fable-5-1, so a room pinned to 5.1 keeps running it: an id that leaves this list is ignored and the room runs its rooms.toml model.

### card-default (#24)

- Every room's progress card now shows the step rows, a room with people's too: the state and the model's newest line
  ("thinking" while it thinks), then one row per step, done or not done, as the owner's rooms always have. The thinking
  line under it stays where it was: owner-only rooms that turn `thinking_panel` on.
- Why: a room with people showed "step 1", "step 2" where an owner's room showed the state, the model's
  newest line and a row per step, and every room should show the same card.
  
  
- The short card a room with people has shown since 2026-10-21 (its newest holding line and "step N", never a step's
  words) is now the opt-in: `card = "count"` in the room's block. `card = "steps"` (2026-11-03) still loads and means
  the default, as any other value does. The code ceiling moves up by 1.

**Upgrading**: nothing to change for a room that should show the rows. A room with people that should keep the short
card adds `card = "count"` to its block in rooms.toml; `card = "steps"` lines may stay or go. rooms.toml is read at
yerkd's start, so the change shows from the next restart.

### need-2026-11-10-83 (#47)

- A worker can drive the browser. The `worker` tool takes `addons`: the room's own add-on tools the worker gets too, by
  the name the room has (`mcp__hands__browser`) or by server (`hands`). Before, a worker had only its built-in tools, so
  a room that handed a long form-filling job to a worker, to stay free to talk, saw it stop in under a minute: it could
  not open or fill a web form.
- Only what the room itself has: a tool missing from the room's allow list, or a server whose rooms.toml table passes a
  credential in its env or headers (a worker's shell could read it), refuses the start whole, saying why. A room that
  is not the owner's still starts no worker at all.
- The server runs from the room's own table, with `YERK_WORKER` (the worker's id) and `YERK_WORK_DIR` (its folder)
  added to its env. The browser plug-in (its branch need-2026-11-10-83) then acts on the room's tabs, so the room can
  pick up a form the worker left ready; holds a brake lease of its own, `hands-<room>-w<id>`, since two processes on one
  owner name share a lease and the first one's arm would end the other's mid-call; saves its pictures in the worker's
  folder; and puts the worker's id on each ledger line.
- The worker's gate refuses any other MCP tool, and any add-on call carrying the owner's word (`confirmed`): nobody is
  there to give it. The worker leaves that step ready (a quote form filled up to its last button, say) and its report
  says what waits, for the room to do once the owner says it.
- **Upgrading**: nothing to set. The browser plug-in's branch lands with this one; a room's rooms.toml table and allow
  list are read as they are. budgets.toml: `[code] lines` and `yerk_tools` rise with their dated reasons.

### copy-survives-2026-11-10 (#46)

- A message sent while a room compacts a copy of its session no longer throws the copy away. Before, a turn starting
  ended the copy, so in a fast chat no copy ever finished: on 2026-11-10, inside seven minutes, a busy room sent
  a message within a minute of each of nine answers, all nine copies were dropped (50 of 54 over two days), and the
  tenth turn's end compacted the live session for 121 s, a message waiting 14 s under the card that says the
  conversation is being shortened first.
- Now the message runs at once on the live session and the copy goes on compacting off to the side. Once it has, the
  room takes it in one step: at once when no turn runs, else as the running turn ends, before a message waiting runs.
  Taking it makes no model call, so no message waits on it.
- What was said on the live session after the copy was made reaches the copy once, as text, in the first message it
  gets: each message and the room's reply, word for word with their times, under one heading that says they came after
  the summary, that their tool output is not included and that what they wrote to memory or disk is there. Past
  `copy_tail` characters the newest stay whole and the oldest are named by their day-log lines. A restart before that
  message keeps it.
- While a copy compacts, the turn-end compaction waits for it as long as `copy_room` tokens are left under Claude Code's
  line, so it is the last resort. With no copy, or after one failed, it runs as before.
- A copy still ends when the room's model, effort or account changes, on /new or a fresh-next file, when a held
  compaction starts, when the daemon stops, when its own compaction fails, and when a turn's run started a fresh
  session or the live session compacted meanwhile. The log and the ledger say why; a take says how long the compaction
  took, how many turns overtook the copy and the tail's size (the turns row's new `note`).
- The tests: 9 in tests/test_idle_compact.py in place of the old drop's 1: the replay of the nine quick
  turns (one copy, taken at a turn's end, its tail given once, no wait, no held compaction); a copy compacted while a
  turn runs taken as it ends, before the message waiting; each change that still ends a copy, and a failed one tried
  again; the turn-end compaction waiting for a healthy copy and running past `copy_room`; a restart with a copy in flight.

**Upgrading:** budgets.toml adds `[session] copy_tail` (12,000) and `copy_room` (30,000) and raises `[code] lines` to
28,587, each with its dated note. The ledger adds its `note` column at the next start.

### need-2026-11-10-82 (#?)

- A room opens your live page on the PC monitor you name, maximized there, and hears that it painted before it looks.
  Before, the `page` tool's open handed the address to the default browser, which put it wherever its last window was:
  on 2026-11-10 a scheduled check told to open the page on the owner's middle monitor heard "opened on the owner's
  screen" while the page sat maximized on the left one, unpainted on a screen asleep. A desktop screenshot then showed
  the night before's frame, and only a click, which needs your word, would have woken it.
- Now the `page` tool takes `screen`, and yerkd.toml's `[page] screen` names a standing one: left, middle, right,
  primary or its number from the left (middle is the centre one; of two, the right one, so the same screen stays the
  middle one when Windows drops a third that sleeps). On a WSL box, Windows' own PowerShell opens the page's one-time
  address in a new window of the default browser (Chrome, Edge, Brave, Vivaldi, Opera or Firefox), moves that window to
  the screen, maximizes it and raises it above the rest without taking the focus. It wakes the display first with a
  display-required request and a still mouse event: no click, no key, the pointer unmoved. The address rides in
  PowerShell's environment, never on a command line.
- The page's tab says when it has drawn a frame while it shows; a browser draws none for a covered window or a screen
  that sleeps or is locked. The open waits 10 seconds for that and says "painted" with the screen's rectangle for a
  desktop screenshot, else "not painted" and why: locked, or still asleep or covered. A room no longer reads an old frame
  as the page.
- Said in words, opening the page once at most: a screen that is not there (with how many there are), a browser yerkd
  opens no window of, a window that never showed, a box that is not a Windows PC. Only when nothing opened yet does the
  box's opener open it where the browser puts it. An open with no screen named works as before.
- The ability is page-mac's (`yerk explain page-mac`), the entry for where the page opens, since a key of its own would
  push the abilities answer past its 2,000 recall cap; it answers a room asked for the page on the middle monitor, the
  wrong monitor or a blank page. The README's live page line names the PC monitor you name; to stay under 12,000 bytes
  its progress card clause and two Install words are shorter.
- The tests: 5 new, in tests/test_page.py: the named screen's open with the address in the environment and on no command
  line; the standing `[page] screen`; the paint counted only from the tab that open made, never another ticket's or
  another room's; a locked screen; each failure said, with no second tab; PowerShell's own failure in words, never the
  address; the tab's paint and the script's screen count read in place. conftest.py keeps every test's box off Windows,
  so no test opens a page on a real screen.

**Upgrading:** budgets.toml raises `yerk_tools` to 26,400, the session start's `total` to 129,471 and `[code] lines` to
28,431, each with its dated note. To keep the page on one monitor, set `[page] screen` in yerkd.toml (for example
`screen = "middle"`); else a room names it per open. A room picks up the tool's `screen` at its next session; a page tab
open from before the deploy says it painted only after a reload.

### walls-2026-11-10 (#43)

- A gate that cannot be asked refuses. A room's tool call is not run when the daemon gives its gate no answer within 8
  seconds or answers with an error, and the room's model is told so in one line; a worker's call is not run when its
  gate hook cannot start, fails or times out. Before, Claude Code carried the call out: on 2026-10-19 one room's
  170 gate calls failed and every action ran. Tried in real Claude Code 2.1.321 sessions before the merge: with the old
  settings and no daemon the command ran; with these, a gate that is down, slow past 8 s or answering 403 stops it, and
  a healthy one runs it.
- A held command is held in every shape a shell runs it: behind `timeout` or `nice`, in `bash -c`, after `then`, behind
  `xargs`, or by its full path. The same for a restart of yerkd, wherever systemctl's options sit. Before, only the plain
  form was held (worker 00c67e's `timeout 60 git push`, 2026-10-28, went past the gate and failed for lack of a login).
  Replayed: 15,187 real worker commands gain that one hold; 1,424 real room commands gain none.
- A worker cannot change its own gate: worker.json, settings.json and mcp.json in its folder are read-only inside its
  unit, and a write to one that shows in the command is refused in plain words. Reading and naming them stay free (a
  first cut refused 31 of 17,017 real worker commands, every one a read; this one refuses none of them).
- The owner's word approves only a hold that was there when the word reached Yerk, never one the turn made afterwards.
- A provider key or token pasted into a room is filed in the day log as its kind (`[groq-key withheld]`), never its
  value; imports withhold Groq keys too. A code or a password said in a room is filed as said. Over the 197,187 day-log
  lines on file the fourteen key shapes match one string.
- From the walls audit of 2026-11-09 (branch walls-audit-2026-11-09 on da8bfd8), brought onto main with need-2026-11-09-81.
- **Upgrading**: nothing to change. While yerkd is down or stalled past 8 seconds a room's tool calls are refused, not
  run; they go through again on the next try.

### need-2026-11-09-81 (#38)

- A room closes or opens its live page's chat when you ask, with its `page` tool. Before, the tool drew the program, the
  tests, marks, the hint, the bar, the timer and the stall wait but had no switch for the chat pane: only the `close`
  button in your browser closed it, and the page's own record of it is yerkd's, which no room writes. Asked on the page
  for no chat box, a room had no way to take it away.
- Now the `page` tool takes `chat`: "closed" closes the page's chat to nothing, as its `close` button does, and "open"
  brings it back. Every open tab shows it at once (an opened chat comes back at the width it had), yerkd keeps it across
  a restart, and a tab that opens later shows it too; with no tab open the answer says the page shows it when one opens,
  and `open` and `chat` go in one call. With the chat closed every answer of the room's to the page is a bubble under
  the line it was asked from (else under your last # line), never in the program, as when you close it yourself.
- The room is not told back what it did itself, a switch of yours that it undoes before it heard of it is never said,
  and a word that is neither is refused in words with nothing drawn. The tab shows the room's switch and keeps it in the
  browser's split, posting nothing back; your own press of `close` or `chat` after it tells yerkd as before.
- The ability is page-talk's (`yerk explain page-talk`): its line on the closed chat names the room's switch, and it
  answers a room asked for no chat box, about the chat on the right, to hide the chat or to bring it back. The README's
  live page line says "chat on ask" (its `delete` clause says paths for files or folders, to stay under 12,000 bytes).
- The tests: 4 new, in tests/test_page.py (the tool's switch on every open tab, in talk.json and after a restart, the
  room's answer then a bubble and never in the program, nothing told back, an owner's switch it undoes left unsaid, a
  bad word drawing nothing; the tool through the daemon with no tab open, beside `open`, and its refusal; the page's own
  script, run by node, closing and opening the chat as its buttons do and posting nothing back) and
  tests/test_abilities.py (the ask in those words finds page-talk whole under the recall cap), and the tool's `chat`
  field checked in tests/test_hooks_mcp.py.

**Upgrading:** budgets.toml raises `yerk_tools` to 26,100, the session start's `total` to 129,171 and `[code] lines` to
28,094, each with its dated note. Nothing else to change. A room picks up the tool's `chat` at its next session; a page
tab open from before the deploy shows the room's switch once it is reloaded.

### need-2026-11-09-74 (#27)

- A room can fill a web form it sees only in a shot now: the hands plug-in's browser gains `type`, which puts text into
  whatever field has focus in one of the room's tabs (one a click by x and y, or by label, just focused), typed by the
  browser itself over its developer protocol (chrome-cdp's `insert`, Input.insertText), so a field inside a web
  component's shadow root, open or closed, a framework's field that ignores a value set by script, and a field no list
  names all take it. `key` presses one key after the text, or alone: Tab, Escape, ArrowDown, ArrowUp or Backspace, and
  Enter on the owner's word, since an Enter can send the form. A select takes the option its words name (an exact one
  first, else the one option that holds them; none or several are said with the options), and the reply says what the
  field reads after, or where Tab moved the focus.
- The field is found before anything is typed, through each open shadow root and each frame of the page's own site:
  nothing focused, a field that takes no text and a file input (upload's) are each refused with the way past; a card
  field is never typed into, confirmed or not; a frame of another site holding the focus is judged as a click by x and
  y into one is. Nothing is typed on a payment page, and on a look-alike only on the owner's word, as with set and click.
- One `type` with need-2026-11-09-75's, which reached main first: the plug-in's main aa4d3fc merges its branch
  `need-2026-11-09-74` onto need 75's `type` (cc75696) as one action. With a `label` (or need 75's `value` for the text),
  the field by its label, found as `set` finds it, through each open shadow root too, then a box: a text box cleared with
  a real Backspace and typed a character at a time while short, a select given the option its words name. With no label,
  the field that has focus, as above, typed a character at a time too. A `key` after either, and what the field holds
  read back.
- A form drawn inside a web component's open shadow root is read too: its words follow the page's on `read` (after
  `--- inside web components (shadow roots) ---`), `fields` lists its controls after the page's, `set` and `click`
  reach them by label after chrome-cdp's own NOT FOUND, and a click by x and y is judged by the control under the
  point inside it, so a form's submit there waits for the owner's word. A card field, a pay button or a payment frame
  inside one makes a payment page, as one on the page does.
- The plug-in's branch `need-2026-11-09-74` (8c2aff9, then af44c92, a test's placeholder postal code); 58 of 58 of its offline
  tests green, six of them new, the first the wall itself: a click by x and y focuses a Year select in a shadow root,
  `type` picks the year, then types the postal code and presses Tab. Its page scripts were also run in Playwright's
  headless Chromium against a form in a shadow root, a closed root, nested roots and a card field in a root. Joined to
  need 75's on the plug-in's main (aa4d3fc): 66 of 66 green, both branches' cases and one for the joined paths, and its
  by-label scripts run in that headless Chromium against a field and a select inside a shadow root.
- The abilities entry `form-typing` (`yerk explain form-typing`), with its phrases ("type into the field", "shadow
  root", "fields are not listed", ...); the README's Add-ons line names it in as many bytes.
- budgets.toml: the per-message recall line 2,000 to 2,100 and the per-message cap 5,650 to 5,750, since the answer to
  "what can you do" (every ability's name by its state) rides whole under the recall line, sat 7 under it on main, and
  `form-typing`'s name took it 6 over. The hands line and the session-start total stay where they are (9,831 and
  129,071): need-2026-11-09-76 reached main first and its hands line already counts the joined browser entry. Each
  with its dated note; docs/budgets.md is what `yerk docs own` writes.
- Why: on 2026-11-09 a room asked to run an online quote saw the site's quote form in a shot, while the browser's read
  and its fields saw only the page's footer, since the form sits in a web component's shadow root; a click by x and y
  focused the form's first field, and no action typed into it, so the room filed the need (need 74 of 2026-11-09).

**Upgrading**: hands-mcp's main already holds the joined `type` (aa4d3fc): the hands line here counts the browser entry it
sends, and its rooms have it at their next session. An install that keeps its own budgets.toml takes the recall line's 2,100 and the per-message cap's 5,750.

### need-2026-11-09-83 (#?)

- The `texts` tool reads the words of the texts you sent. Before, a room asked to confirm the words of a text you had
  just sent got an empty text field for it, and for every other text you sent that evening, while the texts you got
  carried their words: Messages keeps the words of many sent texts only in the message's rich copy (its
  `attributedBody`), and the read took the plain text column alone.
- Now a row whose text column is blank brings its rich copy too, as hex, only for that row, and the daemon reads the
  words out of it the way `yerk import imessage:` already did. The copy on the box keeps them, so a read while the Mac
  sleeps has them too.
- A text with no words anywhere says why in a new `blank` field, never an empty text a room could take for what was
  said: a picture or a file alone, a rich copy that could not be read, or no words stored (as with an unsent text or a
  group change). A text whose only content was a picture used to come back as one invisible mark; now it is `blank`.
- The `texts` tool's description names `blank`, the `text` ability (`yerk explain text`) says how a blank text's words
  are read, found by a room asked what you texted, and the README's `texts` clause says it reads yours whole.
- The tests: 4 new in tests/test_inbox.py (the wall: a text you sent with a blank text column read whole from its rich
  copy, a long one too; the read's own query run on a database shaped like the Mac's, the rich copy brought only for a
  blank row; every reason a blank gives, and words beside a picture kept as words; the tool's answer and the copy) and
  1 in tests/test_abilities.py (asked what you texted, a room gets the text entry whole under the recall cap).

**Upgrading:** budgets.toml raises `yerk_tools` to 26,000, the session start's `total` to 129,071 and `[code] lines` to
28,086, each with its dated note. Nothing to set: the copy on the box is written again at the Mac's next answer, and a
room picks up the tool's new words at its next session.

### need-2026-11-09-82 (#?)

- A room's live page talks in the code now, not in it: the room's answers to what is said on the page show as bubbles
  between the editor's rows, under the line they were asked from, in the room's colour with its name and time, four lines
  shown and a click to open the rest, an x that puts one away on every tab. They are never written into `solution.py`,
  where the room's reply used to land as a dozen plain `#>` lines at the bottom of the owner's file. With the chat closed
  every answer to the page is a bubble (under the `#` line said last when its message named no line); with it open, the
  answer to a line asked about still is. yerkd keeps the newest 100 in `work/page/talk.json`, so a reload or a restart
  shows them, and a bubble moves with its line as the owner types; a program the room draws keeps it on the line it kept.
- A pulsing typing mark sits under that line from the send until the room's turn ends (its reply, a failed turn's notice
  or a silent NO_REPLY end), and at the foot of the chat for a line said in the say box.
- One key asks about a line: Ctrl+Enter on it, or a click on its number, sends "about line N of the program:" and its
  words to the room at once; a click on a red test tile sends "about test NAME, which fails at line N:" and its message,
  and the answer lands under the test's `def`.
- Both editors colour Python (keywords, strings, numbers, names, comments, decorators), the owner's own `#` lines in a
  colour of their own and the room's old `#>` lines in its colour, with the page's own code and no outside script. Enter
  keeps the indent (four more after a colon), Tab indents and Shift+Tab out, Ctrl+Z and Ctrl+Shift+Z undo and redo.
- A bar number that rises pops "+N" beside it.
- The room is told the new way when the chat closes or opens; the `page` tool's clause says its answers are bubbles.
- The tests: the `#>` tests (the tab's catch-up of replies it missed, the cap on replies in the program, marks moving
  under them) are gone with the lines; new ones cover the bubble under the line asked from and never in the program, kept
  across a restart and put away for good, the typing mark's life, the line and test asks, bubbles moving with edits and
  draws, need 80's rule (only answers to the page), and node runs of the page's own editor, lexer, `+N` and its moves
  checked against page.py's on 300 random edits.
- The fix: a bubble moved by lines typed above it is written to `work/page/talk.json` at once, so after a restart it
  comes back beside its line. Before, it came back on its old line, now beside some other line of the program, because
  only a new bubble, its x or a program the room drew wrote the file. An edit that moves no bubble writes nothing there.
  `[code]` lines go to 28,056, one fewer, since the draw's own write folds into the move.

### persona-voice-news-2026-11-09 (#?)

- A line added, changed or removed in a room's persona or voice file now reaches the room's running session. Before, the
  `Rules changed since you last looked:` block on a message was built from the mechanics, the room's rulebook and the
  rules file only, and a resumed or compacted session keeps the start file it began with, so a fix made by hand to the
  persona or a voice file reached no running room: one that kept its session for days went on with the old persona until
  a fresh session (`/new`, a model switch that cannot carry the session, a crash).
- Now each such line is told once, on the session's next message, inside the same block and in the rulebook's words
  (`new in persona.md: ...`, `replaced in persona.md: ...`, a voice file by its own name), as Claude Fable 5.2's page
  says a change partway through a conversation is made: "put them in the newest turn". Every room that loads the file
  hears it (one layer's rooms share a persona; a voice file is one room's). The block's 1,500 characters and
  rules-changed.md past them hold for the larger block as before; a sealed room still hears nothing, and a room with no
  persona or no voice key is as before.
- A fresh session's look on file (rules-seen.json, and the texts kept beside its start, starts/<id>.json) now holds the
  persona and voice texts it began with, so every later edit is told against them, and a compaction's copy keeps its
  parent's, as it does the three rule texts.
- A session begun before this change has no persona or voice text on file, so it reads them off the start file it was
  given (starts/<id>.md): a line of the file now that the start holds whole counts as seen and the others are told as new,
  and a line of the start's own persona or voice part (from its `[from <path>]` line to the next part, its trim marker left
  out) that the file no longer has is told as replaced. On a capped install where that part was cut to fit, the lines
  cut from its middle are told once as new, past the budget in rules-changed.md. With no kept start, or one that holds
  none of the file's lines, nothing is told, so no message carries a whole persona or voice file.
- The `rules-changed` ability (`yerk explain rules-changed`), the guide's paragraph on it, budgets.toml's note on `rules`
  and the UserPromptSubmit line of docs/hooks.md name the two files. The mechanics are unchanged: their line on the block
  says it brings changes, which stays true.
- The tests: 4 new in tests/test_rules.py (an edit told once to each room that loads the file, with two rooms sharing one
  persona, a room with neither key quiet and a fresh start's kept texts holding both; a session from before the change
  told only the lines its start does not hold, a removed line among them, and with no kept start nothing; on a start cut
  to fit, a line gone from the persona's head told and no line of another part taken for the persona's; a compaction's
  copy told the change since the start it kept, and a sealed room nothing), and tests/test_uncapped.py's capped fixture
  retaken, its only change rules-seen.json holding the persona and voice texts.

**Upgrading:** budgets.toml raises `[code] lines` to 27,990 with its dated note. Nothing else to change: a running
session's first message after the deploy reads its baseline off its kept start.

### hot-reads-2026-11-10 (#?)

- The daemon no longer freezes, and is no longer killed by systemd's watchdog, when one room posts a reply while another
  room's turn is busy. To post a reply it read the room's whole delivery history on the event loop. sqlite hands
  Python's interpreter back after every row, so with another room's turn working at that moment a row cost about 8 ms
  instead of microseconds: on the owner's ledger 236 rows took 2.0 s, and the busiest room has 4,667, about 40 s, past the
  watchdog's 30. That killed the daemon on 2026-11-09 with a turn running, which was cut and redelivered (the
  loop's stack was in that read); an earlier kill the same day fits it (a send in another room sat 29 s between
  planned and posted, the one before it 11 s).
- Now one reply's parts are read by key range, which sqlite answers from the key's index: the same rows in the same
  order, whatever the room's age. A turn's landed parts and the replies the end-of-turn check reads are read the same
  way (the engine audit of 2026-11-09, defect 1: that check took 1,017 ms under the ledger's one lock in a session 2,564
  turns old and 9.5 ms after, its text identical in 120 of 120 sessions read on the live ledger).
- `yerk deploy` no longer stops on its fast tests in the first three minutes after midnight. tests/test_style.py's
  worker card test used a row begun 185 seconds before; right after midnight that is the evening before, which the card
  says with its day ("Mon 23:57"), and the test's pattern took a bare time only. A deploy that tested from 23:57 to
  00:00 on 2026-11-10 stopped there with "assert None", and its need was held.
- The tests: 4 new. tests/test_ledger_hot_reads.py counts sqlite's own steps, never seconds: the end-of-turn read costs
  the session's replies, and a reply's parts and a turn's landed parts cost the same in a room of 100 replies and in
  one of 4,000. tests/test_style.py states the card a row begun before midnight gets, and its two clock tests no longer
  depend on the hour they run.
- budgets.toml: `[code] lines` 27,961 to 27,977 with its dated note.

## 2026-11-09

### need-2026-11-09-80 (#?)

- With a live page's chat closed, only the room's answers to what was said on the page go into the program. Before,
  every line the room said, from any thread, was written into the program as #> lines under your last # line: its
  answer to a Discord message, to a scheduled note or to a worker's end, and a job's nightly post, all landed in your
  code.
- Now a line goes in when a message it answers is one the page sent the room (its own page message, ledgered on the
  page's channel): the reply to a turn on it, a turn that folded it in with another message too; the notice of such a
  turn that failed; and the room's own `send` from that turn, which now hands the page the messages its turn answers.
  Every other line stays on the page's chat alone, kept for when you open it, and reaches the room's channel as before.
  The room is told so when you close the chat, and the `page` tool's description says it.
- The page-talk ability (`yerk explain page-talk`) carries it, found by a room asked why its Discord replies land in
  the code (a key of its own would push the "what can you do" answer past its 2,000-character cap). docs/GUIDE.md's
  live page section says how the talk in the program works, and the README's live page clause says only replies to it
  go in its code (three lines of its docs list say the same in fewer words, to stay under 12,000 bytes).
- The tests: 4 new, in tests/test_page.py (with the chat closed, a Discord message's, a scheduled note's and a worker's
  end's answer, a job's post, a notice that answers nothing and an answer to another room's page stay on the chat,
  while the reply, the notice, the room's own send and a fold's reply on the page's message go in under the owner's #
  line; a room's own turn on a Discord message leaves the program as it was and its turn on the page writes its reply
  in; the send tool from a turn on the page writes in, from a turn on Discord or no turn it does not) and
  tests/test_abilities.py (the page-talk entry answers the ask whole under the recall cap). The talk tests that said a
  line with no message now name the page message it answers.

**Upgrading:** budgets.toml raises `[code] lines` to 27,961 with its dated note; the `page` tool's 18 more characters
fit `yerk_tools` as it is. Nothing to set: yerkd's next start takes it, and a room reads the tool's clause at its next
session.

### need-2026-11-09-79 (#?)

- A scheduled agent job sets its own worker's thinking level and model. Before, every job's worker took its room's live
  level and model, and neither the `schedule` tool nor `yerk schedule add` had a field for either: a room kept on max
  thinking that started each research run of a bot as a standing agent job (deliver silent, so no relay turn) paid max
  for every run, one run at max costing about eight times the same run at high, and the accounts' weekly meters filled.
- Now the `schedule` tool takes `effort` (low, medium, high, xhigh, max) and `model` (an alias or a full id on the
  install's list: models.toml, or [models] in yerkd.toml) for an agent job, `yerk schedule add` takes `--effort` and
  `--model`, and jobs.toml takes the `effort` and `model` keys. The job's worker runs at them while its room stays on
  its own. An alias is kept by its name in the jobs file, so the job follows the list when the list moves, and the worker
  gets the alias's full id when the job fires. A job that sets neither runs as before: `[workers]` effort and model when
  yerkd.toml sets them, else the room's live ones.
- Refused, the jobs file left as it was: either on a note or a script job (a note runs nothing, a script calls no
  model), a level that is not one, and, from the tool or `yerk schedule add`, a model off the install's list, answered
  with the list. A model written into jobs.toml by hand goes to the worker as written (an alias on the list made its full
  id); one Claude Code does not know ends the run with the job's failure alert, as before.
- The add's answer, `yerk schedule add`'s line and every list of jobs (`yerk schedule list`, `yerk jobs list`, the
  tool's list) say what a job sets ("agent, model sonnet, effort high"). The schedule ability (`yerk explain schedule`)
  carries it, found by a room asked about a job's thinking level, effort or model, and the example jobs file's agent job
  shows `effort`. The README's jobs.toml line names an agent job's effort and model (its usage meters sentence a few
  words shorter, to stay under 12,000 bytes).
- The tests: 2 new and 5 new refusal cases, in tests/test_jobs.py (with the room on max, a job added through the tool
  with effort high and model sonnet runs its worker at `--effort high --model claude-sonnet-5-5` while one that sets
  neither runs at the room's max and model; a model off the list, a level that is not one and a level on a note refused
  with the file unchanged; the list's words; `yerk schedule add --model --effort` and its refusal; a note or a script
  with either key, and a made-up level, refused by the loader) and tests/test_abilities.py (the schedule entry answers
  the ask whole under the recall cap), and the tool's two fields and its words checked in tests/test_hooks_mcp.py.

**Upgrading:** budgets.toml raises `yerk_tools` to 25,900, the session start's `total` to 128,971 and `[code] lines` to
27,950, each with its dated note. Nothing to change in jobs.toml: a job without the new keys runs as it did. A room picks
up the tool's two fields at its next session.

### need-2026-11-09-76 (#?)

- A room saves a key a web page shows straight into your private folder's `secrets/`, and never reads it. Asked to keep
  a new API key from a signed-in settings page in a locked file, a room had no clean way: the page's Copy button,
  clicked through the browser, copied onto the clipboard of the computer that browser runs on, never the box's; a room
  cannot write the secrets folder; so the only way left was to read the page, which put the key in the room's session
  log. The hands plug-in's browser gains `keep`: the room names its tab, the new file's name and a label, and the
  plug-in reads the value in the page and writes it itself as `secrets/NAME.key` (the key on its first line, 0600, the
  folder 0700), so the value never reaches a reply, an error, the plug-in's ledger or the room. It finds the value three
  ways: a field by its label (the page, its web components, a frame of the page's own site); else the page's own Copy
  button, clicked with the copy caught inside the page (a button whose words say copy and nothing outward, the page's
  clipboard put back after); else the one-word value shown beside the words. The reply names the file and the
  value's length only.
- The guards: only on your word (`confirmed`), since it writes outside the room; never over a file already there, a
  link included, so a steered call cannot replace a key Yerk runs on; nothing kept from a payment page; and a value
  shown masked, holding spaces, under 8 or over 4,096 characters, several values with none picked, or nothing found is
  refused with its reason. A job or a script you run reads the file; no room does.
- The plug-in's change is on its branch `need-2026-11-09-76`; `yerk explain page-keys` has the ability. The "what can
  you do" answer fits its 2,000-character cap with the new name by cutting reverse-image-search's provider words to
  "Google Cloud Vision".
- The cold read found the room still read the key in the filed case: `keep` needed a tab, only `open` gave one, and
  `open` always returns the page's text, so the key was in the room's log before `keep` ran; a later read of the page
  was never scrubbed, and keep's own refusals said to read the tab. Now `keep` takes the page's `url`: the plug-in opens
  the page itself and returns the tab id with no page text (a tab still works for a key shown only after a click).
  Every `secrets/*.key` file's first line is scrubbed from every reply after, so a later read of that page shows
  `<key>`. No refusal or repeat hint sends the room to read the page: "nothing to keep" lists the page's labels and
  Copy button words, never a value. The words-beside way takes only a value shaped like a key (a letter and a digit)
  and skips buttons, links and other controls, since it saved a Regenerate button's word or a date as the key; a Copy
  button whose words also say regenerate, reset, rotate, roll or revoke is never clicked; a failed page script is said
  in fixed words, never its output, and so is a failed read of the page keep opened. The plug-in's fix is on its
  branch `need-2026-11-09-76`, commits 305c25c and d570241.

**Upgrading:** budgets.toml raises `mcp__hands__` to 9,831 and the session start's `total` to 128,671 (on main's 127,548), each with its
dated note: 424 for `keep`, 481 the plug-in's main already sends since need 74's `type` was merged onto need 75's
with no line raised, and 218 for keep by the page's url. The plug-in's branch merges onto its main, and a room picks up `keep` at its next session.

### need-2026-11-09-78 (#?)

- A text or an email you approved goes on that one word, whatever folder the room's shell is in when it calls again.
  Before, an approval matched the call and the folder the shell was in when it was held, so a session a compaction had
  put back in its work folder (a resumed session starts there) made the same call, was held a second time on a second
  card, and you said the word twice; the text still went once.
- The same holds for a send to another room, a delete through the `delete` tool and any other tool's call the gates
  hold: a tool's input is its whole call (an email's files by whole path and sha256, a delete's whole paths), so the
  folder adds nothing to it. A shell command (a `git push`, a `gh pr create`) still runs on its word only from the
  folder it was held in, since the same words push another repository from another folder.
- Nothing else changes: each call is still shown before the word, the word releases it once, the next one is held
  again, and the hold's ledger row still keeps the folder the shell was in.
- The tests: 3 new, in tests/test_texts.py and tests/test_emails.py (held with the shell in a folder under the work
  folder, the word, then the same call from the work folder: released and sent once, one card and one word, and the
  next one held again) and tests/test_gates.py (a send to another room released from another folder; a `git push` held
  again from another folder and released from its own).

**Upgrading:** budgets.toml raises `[code] lines` to 27,923 with its dated note. Nothing to set; a hold made before the
deploy's restart is released by the same call from any folder too.

### need-2026-11-09-77 (#?)

- A worker whose last message is exactly NO_REPLY now ends with no relay turn, however it was started: the `worker` tool,
  or `yerk worker start --brief FILE --room ROOM` in a timer's script. Before, only a scheduled agent job's NO_REPLY was
  silent; any other worker's quiet end settled its card as `🟢 worker finished clean, nothing to add`, then still asked its
  room for a turn to tell the outcome, which the room had to answer with a posted line. A timer script that starts a
  worker each time something might need a look, dozens of times on a busy day, put a turn in its room for every quiet run,
  and the way around it, a one-shot agent job a run, left a job in the jobs file each time and could not pick the
  thinking level.
- What stays: the card settles quiet as before; the worker's ledger row says relay skipped, "the worker answered
  NO_REPLY", and `yerk worker list` shows it. A run with something to say is told as before, and one that dies, times
  out or is stopped after saying NO_REPLY is still told. A worker that ended while yerkd was down ends the same way at
  its next start. A job's worker, a pictures backfill batch and the needs pipeline's workers keep their own endings, and
  `--effort`, `--model`, `--account`, `--cap` and `--task` work as they did.
- The `worker` tool's words say the room hears nothing when the last message is exactly NO_REPLY, `yerk worker start`'s
  `--brief` help says so, and the workers ability (`yerk explain workers`) carries the silent end, found by a room that
  asks for a worker from a script that ends silently.
- The tests: 2 new, in tests/test_workers.py (a worker started by `yerk worker start --brief FILE --room ROOM --effort
  low` that answers NO_REPLY asks no turn and runs at its level, live and at a re-attach, while one with something to say
  and one that dies after NO_REPLY are each told, in order) and tests/test_abilities.py (the workers entry answers the
  ask whole under the recall cap), and the tool's words checked in tests/test_hooks_mcp.py.
- Tests only: that check of the `worker` tool's words now reads them with their lines joined, as the attachments,
  seen and watch tests read theirs (box-ci red on py3.12 at this branch's head 385d9e1). Python 3.12 keeps a
  docstring's indent where 3.13 and 3.14 strip it, so on 3.12 the line break before NO_REPLY came with eight spaces
  and the test, which matched the bare line break, failed there alone. The tool's words are unchanged.

**Upgrading:** budgets.toml raises `yerk_tools` to 25,600, the session start's `total` to 127,548 and `[code] lines` to
27,915, each with its dated note. Of the 800 on `yerk_tools`, 100 is the `worker` tool's clause and 700 is `remember` as
main builds it (3,016 characters since memory-docs-shape and fable-5-2, no line moved for it there). A worker that ends
after the deploy's restart ends this way, and a room picks up the tool's words at its next session.

### memory-docs-shape-2026-11-09 (#?)

- A rule is filed as one lesson in the shape Anthropic's Claude Fable 5 prompting page gives a memory system: one lesson,
  a one-line summary first, its reason, a brief body, and a day-log cite in place of a quote the day log already holds.
  `remember` with `job` refuses a rule that enumerates cases, runs a summary past 140 characters, gives no reason, runs
  a body past 600 characters, or pastes more than 8 words a day log holds. Each refusal quotes the docs sentence it
  enforces, read off the fetched page, and says the fix.
- `yerk docs` fetches the Fable 5 prompting page too, and the doctor fetches the pages daily. Each check is pinned to its
  docs sentence and page; a sentence the page no longer holds turns the test suite red and is a doctor line.
- The doctor's daily run scores the whole rule store against the lesson shape and the rules file against the
  code-memory limits (200 lines or 25KB), writes `docs/shape/lessons.md`, and on a finding files a need and says one line
  in the machine room. A green day says nothing.
- `remember` with `retire` deletes a rule that turned out wrong: its line is emptied and its words go to the day log.
- A filing is never announced. A final message reporting one ("Filed", "Logged", "the rule now says") after a filing
  call bounces once with the docs' readability sentence, and `remember`'s result no longer asks for a filing clause.

- Claude Fable 5.2: a room's checks read its own model's prompting page beside the guide, the Fable family page and
  code-memory, and the shared rules store is scored against the pages of every model any room runs, each finding naming
  its page. `yerk docs` fetches the 5.2 page; a model a room runs with no page fetched is a doctor line and a need naming
  the page to add.
- From the 5.2 page, `remember` with `job` refuses an absolute rule (never, always, must not) that does not say where it
  applies or what lifts it, an approval rule that names no action ("anything outward" alone), a line telling the model
  to think less, and a line asking for its reasoning in the reply. A hard limit of lines, sentences or words is counted
  in `docs/shape/lessons.md`, never refused. The 40-word sentence cap is gone from the gate, since the page says a hard
  sentence limit makes prose choppy.
- The mechanics say the 5.2 page's finishing, opening-line-and-recap and communicating-clearly text in their own words,
  each absolute carries its scope and exception, and a line names the steps that need no approval.
- A resumed session, or a compaction's copy of one, is given the start file it began with (kept per session under
  `rooms/<room>/starts/`), and every change since rides its newest message, since Claude Fable 5.2 rejects a resumed
  request whose system prompt changed. A new session takes the current files.

**Upgrading:** budgets.toml `[shape]` gains the `lesson_*` lines, `[session_start.appended] mechanics` rises to 8,500 and
the total to 126,748, and `[code]` lines rises, each with its dated note; `[docs] refresh_days` defaults to 1. An install
whose `[docs] pages` in yerkd.toml replaces the default list adds `prompting-fable-5-2` (and each model its rooms run)
there, or the doctor files the need. Until the store is converted (the migration, a later change), the doctor files one
need for the rules out of the lesson shape and says one machine-room line a day.

### need-2026-11-09-75 (#?)

- A room fills a React or Angular form's text field so the form registers it. The hands plug-in's browser `set` writes
  a value from a script, and a framework form whose checks wait on the browser's own events showed the value but never
  had it, so its Next button stayed disabled and the form went no further. The browser's new `type` action finds the
  field by `set`'s own label rules (the page, then a frame of the page's own site), a text box only, focuses it, clears
  what it held with a real Backspace, and types the value through the browser's own text input (`Input.insertText`, a
  character at a time while the value is short, so a mask formats it as it goes). Then it reads the field back (a
  password by its length only) and takes the focus off it, so the change event and the checks on leaving the field run.
  A field that offers suggestions keeps the focus for its list. The guards are `set`'s: nothing on a payment page, and a
  checkout look-alike only on your word.
- The plug-in's change is on its branch `need-2026-11-09-75`; `yerk explain browser` carries the typing line.

**Upgrading:** budgets.toml raises `mcp__hands__` to 8,708 and the session start's `total` to 125,448, each with its
dated note. The plug-in's branch merges onto its main, and a room picks up `type` at its next session.

### zoom-2026-11-09 (#?)

- A room reads the small print on a big picture. Read shows a room a picture at most 2000 px on its long side, so a
  calendar's times, a form's figures or a receipt's lines on a screenshot or photo bigger than that came out too small
  to read, and ffmpeg in a room's shell waited on an approval no one gives mid-turn. The new `zoom` tool takes a picture
  in the room's own folders and a box as fractions of its width and height (0, 0, 1, 1 is the whole picture), and
  returns a PNG of that part 2000 px on its long side, enlarged 4 times at most, which the room Reads before it answers.
  The whole picture zoomed is also the way past a picture Read refuses to open.
- A picture sent to a room whose long side is over 2000 px says so on its `[attachment]` line, with its size and that
  `zoom` reads its small print; the size comes off the picture's header (PNG, JPEG, WebP, GIF), nothing decoded and no
  new dependency. The rooms' mechanics carry one line on it.
- The zooms are saved under the room's `work/zoom/<message id>/` as `NAME.zoom-N.png`, outside the inbox, so no backfill
  or doctor count takes them for pictures sent, and the scratch sweep clears them with the rest of the room's scratch.

**Upgrading:** budgets.toml raises `yerk_tools` to 24,800, `mechanics` to 7,200, the session start's `total` to 125,303
and `[code] lines` to 27,355, each with its dated note. A room picks up the tool and the line at its next session.

## 2026-11-08

### need-2026-11-08-74 (#?)

- Asking a room to text someone as you from your Mac works with nothing set in yerkd.toml, in every owner room. Before,
  the `text` tool's gate said no Mac was set up for texts unless yerkd.toml named `[imessage] host` and `socket`, even on
  a box that already reached the Mac by ssh for the hands plug-in's `mac` tool and read your texts from it, with the
  Mac's bridge up there; the `mac` tool cannot drive Messages over plain ssh, and a room cannot set yerkd.toml while a
  conversation runs.
- The way to the bridge: `[imessage] host` and `socket` (or `command`), as before, else the Mac and the bridge's socket
  an owner room's add-on names together in the `mac.json` beside its server's script
  (`{"ssh": "user@host", "imessage_socket": "/path/to/the/bridge.sock"}`), the same Mac `texts` and the page already
  reach. It is read only from a file of yours that no other user may write and none under Yerk's state folder, a socket
  only beside the host that same file names, and only as a whole path of plain characters, since it rides ssh's command
  line to the Mac's shell; a person's room's add-on is never read. The other `[imessage]` keys (`service`, `socat`,
  `send_timeout`) still apply.
- Nothing else changes: each text is still held in the room with its words and who it goes to, sent once on your word,
  and refused before you are asked when it could not go. The refusal now names both places a Mac is set up. The
  `[code]` ceiling goes to 27,183.

**Upgrading**: the hands plug-in names its Mac's bridge in its `mac.json`, beside the Mac's ssh host, and its `mac`
tool's entry says a text goes by the `text` tool (its branch `need-2026-11-08-74`); its rooms have it at their next
session, and Yerk's `text` tool reads it at once. A yerkd.toml `[imessage]` with `host` and `socket` wins as before.

### precompact-fork-2026-11-08 (#?)

- A room near its window no longer makes the next message wait on a compaction it could have done in a quiet moment.
  A turn that ends within `precompact_within` of the window under Claude Code's own line (0.1: 80,000 tokens at an
  800,000 window, so from 687,000 to the 727,000 where the end-of-turn compaction starts) compacts a copy of the
  session instead: `claude --resume <session> --fork-session --session-id <new>`, then `/compact` in the copy, on the
  live process's own spec and account, so it reads the prompt cache the room warmed and its SessionStart hook adds
  the recall pack again as a live compaction does. The room stays free the whole time.
- A message that comes in while the copy compacts runs at once on the live session, which is what the room did before
  at that size (no compaction ran there), and the copy is ended and dropped as stale, its transcript copy removed:
  Claude Code's own rule for a precomputed summary whose prefix changed. The turn's end copies the session again.
- A copy that no turn, compaction, settings change or /new overtook becomes the room's session in one step, under
  the room's lock: bound in sessions.json with the live process's token, ledgered `compacted` with its size after,
  the old process closed. The next message lands on a session of about 14,000 tokens and never waits.
- Nothing changes within `idle_compact_within` of the line: the live session still compacts at the turn's end and a
  message meanwhile waits, its card saying so. The window stays whatever rooms.toml says (800,000 in the room
  measured); what moves is when a quiet room compacts, at most `precompact_within` less `idle_compact_within` earlier.
- Why: on 2026-11-08 a busy room ended a turn 37,362 under its line and the owner's next message
  waited 126 s behind a 206 s compaction (the ledger's last 40 compactions: median 161 s, at most 263 s). Twenty
  minutes earlier a turn had ended 61,754 under the line with 305 s of quiet after it, time enough for a copy. Claude
  Code 2.1.316's own precomputed compaction does not reach a headless room: it sits behind the server flag
  `tengu_sepia_moth`, false on the account tested, whatever settings.json says, and its idle compaction fires at 90%
  of a one-hour cache (54 min idle), after yerkd has closed an idle room's process at 30.

**Upgrading**: budgets.toml `[session]` gains `precompact_within = 0.1`. A room's block in rooms.toml may set its own;
0 turns the copy off and leaves the room as it was.

## 2026-11-07

### card-race-2026-11-07 (#?)

- A turn that starts right after a restart keeps its card. The restart's sweep removes the cards the old process left,
  and it also removed a card this process had just posted when a message came in before the sweep reached that room; it
  now leaves every card this process holds. Why: a room's turn lost its card a second after it
  was posted, and for eight minutes the owner saw only the reaction while a voice note was made.
- A card deleted by anyone while its turn runs is forgotten at the first edit that finds it gone: one line in the log,
  no edit after it, and no card posted in its place. Why: before, every later edit of that turn failed again, 26 log
  lines in those eight minutes.
- `yerk deploy` waits for a room to have had no message either way for `[deploy] quiet_seconds`: its person's last
  message and its own last post (a reply, a notice, a file, a kept card; a card's progress is not one). Why: a
  restart went two seconds after a room's 6-minute voice note landed, since the window
  counted only from the owner's message 8.6 minutes before, and the owner's next message came six seconds later.
- A need's "built and live" line to a room that filed it waits until that room's turn ends, looking every 5 seconds,
  and goes after 20 minutes even if the turn still runs; the machine room's line and the next need start at once as
  before. Why: need 74's line came into an owner's room five seconds into a turn, so the owner's
  message looked answered by a deploy line.
- The `[code]` ceiling goes to 27,154 (main's 27,124 plus this branch's 30).

**Upgrading**: none.

### need-2026-11-07-74 (#?)

- A room can open a link in one of the owner's emails in the browser now, and press a button in one: the hands
  plug-in's mail `read` lists the mail's links after its text, numbered, each link's words beside its address
  (`3. Cancel: https://...`), and the browser's `open` takes `mail` (the mail's uid), `link` (that number) and
  `mailbox` (INBOX unless the mail was found elsewhere) in place of `url`. The plug-in reads the mail again and opens
  that link's own address in a background tab, under every guard an open has, so a long tracking address is never
  copied out by a room, and the reply names the link it opened.
- Opening a link is clicking it in the mail, so its words are judged first, as a click's label is: a Cancel, a
  Confirm, an Unsubscribe, an answer a link gives just by being opened (I'll be there, decline, verify, opt out, or
  Yes, No or Maybe as its whole words), and a link that shows no words wait for the owner's word. A View or a
  Modify opens at once. A number past the list, a mail with no HTML part (its addresses are in its own text, for
  `url`) and the reader's own failure are each said in one line.
- Need 33's list (2026-11-02) is in this too. Its plug-in branch was cut before need 39's and could no longer
  fast-forward the plug-in's main once that one landed, so the links never reached a room, though the abilities entry
  and the budget line here already counted them. The plug-in's branch `need-2026-11-07-74` merges it (e19a355), then
  adds the numbered open (aa73df9), so its main fast-forwards to both; 51 of 51 of the plug-in's offline tests green
  (six of them about links, all six failing on its main f384c2b).
- The abilities entry (`yerk explain mail-links`) says the link is opened by the mail and its number and held for the
  owner's word when its words say it acts, with five more phrases ("link from an email", "button in that email", ...);
  the README's Add-ons line says it in as many bytes.
- budgets.toml: the hands line 8,165 to 8,563 (the browser entry 3,446 as the plug-in's branch builds it, 3,477 as
  sent) and the session-start total by the 398 to 123,803, each with its dated note; docs/budgets.md is what
  `yerk docs own` writes.
- Why: on 2026-11-07 a room asked to make sure a guest booking was still on that night read the booking's
  confirmation and reminder mails and got their words with every link stripped. The booking site showed no sign-in
  for a guest and had no account page to look in, so only the mail's own link could show it, and the room filed the
  need (need 74 of 2026-11-07).

**Upgrading**: merge this before, or with, the plug-in's deploy, which is fast-forwarding hands-mcp's main to
`need-2026-11-07-74` (aa73df9). A room has the links and the numbered open from its next process start after that.
budgets.toml's hands line is 8,563 and the session `total` 123,803; keep yours at least that, or the deploy's fit check
refuses on the longer browser entry. Until the plug-in's deploy a room sends that entry at its old 3,079, under the new
line.

## 2026-11-06

### rule-filing-check-2026-11-06 (#?)

- A rule a room files with `remember` and a `job` is checked before anything is written. Its words are read by the
  counts `yerk doctor` scores the rule files with (a no with no reason, a shouted word, an example outside `<example>`
  tags, an alarm mark), its examples left out. A rule that misses one is not filed: the room hears each count, the words
  that tripped it and the prompting guide's fix in one line, rewrites the rule and files it again. Before, the doctor
  named such a line the day after it was filed, and every room had loaded it by then.
- Then the filing lists the live lines of other jobs that speak about the same thing, in the rules file, the topic rules
  and each owner room's rulebook, persona and voice: a topic of the doctor's said-twice count, near-duplicate words, a
  mark, code, a quoted phrase, a channel or a file both name, a thing one says no to that the other names, or one's job
  named in the other, and then any line naming the job of a line so found. A line that says no to the same thing as the
  new rule agrees with it, so that thing links neither of them. A word more than five lines say links none,
  so the list stays short. Nothing is filed until the room's next call settles each line, superseded or kept, with a
  one-clause reason. Before, a new rule could land while older lines of other jobs still asked for the opposite, and
  every room that loaded them kept the old way.
- A line superseded in the rules file or the topic rules is marked by the new rule, as its own job's older line is. A
  line in a rulebook, persona or voice file, which no room changes, stays as it is, and the machine room gets one notice
  naming the file, the line and the new rule, for whoever keeps that file. Each verdict and its reason is a line in the
  room's day log.
- `yerk curate --apply` writes no rule line a filing would turn back, and says which proposal and why.
- The mechanics' line on filing a job now asks for literal words, an example that is a real exchange from the day log,
  and the older line's examples carried forward, and says the filing checks the wording and lists older lines on the
  same thing; four other mechanics lines are reworded with no change in what they ask. The mechanics' line goes to
  6,800, `yerk_tools` to 23,700, the session total to 123,405 and the `[code]` ceiling to 26,968.

**Upgrading**: budgets.toml's `yerk_tools` is 23,700 and the session `total` 123,405; keep yours at least that, or the
deploy's fit check refuses on the longer `remember`. A script that files rules through `memory.rule` gets the same
wording check.

### retired-marks-2026-11-06 (#?)

- A reply shape you retired can now be held by code, not only asked for by a rule. Rule text is a request, so a room
  could still answer with checklist boxes, colour squares saying who holds an item and a colour key under the list after
  its rules said not to, and the status board's own rule turned that shape back only in a room with people.
- With `[gates] retired_marks` set in yerkd.toml, a final message in any room without people (an owner room, the machine
  room) holding one of those marks anywhere in a line (after a list number, after a dash, mid-line) is turned back once
  before it is posted, saying how many lines hold one and which marks. The room is asked for a plain numbered list
  instead: a bold lead of two or three words a line and who does what in words, and for a status one line per item with
  its state first. A mark inside a fenced block never counts, and the second attempt goes out as written. Each bounce is
  a `retired` shadow row, counted in the morning diff.
- With the list set, the session start's room pack quotes the room's earlier lines without those marks, and a line that
  was only a colour key is left out whole, so the pack no longer shows the retired shape back to the room as its own.
  The day logs keep every line as it was.
- A room with people keeps the board rule as it was, list or no list, and with no list nothing changes. The `[code]`
  ceiling goes to 26,780.

**Upgrading**: to turn it on, list the marks in yerkd.toml's `[gates]` table, like `retired_marks = ["☐", "🟦"]`, and
restart yerkd. A value that is not a list of marks stops yerkd at its start with one line saying so. budgets.toml:
`[code] lines` goes to 26,780.

### need-2026-11-06-69 (#?)

- A room can now post a file again that it posted days ago. Before, a room asked to send a file once more found its
  own copy gone (the nightly sweep moves the copies of what a room sent to the trash after two days, and the trash
  empties), a history listing that named the file but gave no address, and no network in its shell, so it pointed
  you at the old message to download the file yourself.
- The new `attachments` tool saves the files an earlier Discord message carries back into the room's inbox, where the
  sweep never takes them, and the room posts one with `send`, reads it or attaches it to an email. The message is named
  by its id (looked for in the room's own channel, then the one it watches; the id is on its line in the day log), by
  its link, or with a file's name alone, the newest message the room posted that file in. A message with several files
  gives them all, or with a name only that one.
- A room reads a message only where it reads already: its own channel and the one it watches, and one of your rooms
  also any channel of its own server, as `watch` reads a message's video. What cannot come is said in one line: a
  message gone, an id in neither channel, a channel the bot cannot read, a message with no files or none of that name
  (its files named), a name the room never posted, a file past 300 MB, or a room not on Discord.
- The "what can you do" answer names the new ability inside its 2,000 characters: github-state's provider words are
  shorter. The `[code]` ceiling goes to 26,729, and `yerk_tools` to 23,300 with the session total to 122,405.

**Upgrading**: budgets.toml's `yerk_tools` is 23,300 and the session `total` 122,405; keep yours at least that, or the
deploy's fit check refuses on the new tool. Restart yerkd for the new tool. Nothing to set: the bot reads the message
on the permissions it already has (View Channel and Read Message History).

## 2026-11-05

### compact-visible-2026-11-05 (#?)

- When Claude Code shortens a room's conversation by itself inside a turn, the card now says "shortening the
  conversation first" from the moment it starts until it ends, at the start of a turn or in its middle, and then goes
  back to what the model is doing: "thinking", "working on it" or its words. Before, that wait (2 minutes in
  one room on Nov 5) showed only "working on it".
- A model that is thinking and has said nothing yet shows as "thinking" on an owner room's card (and on a card = "steps"
  room's) in place of "working on it", at the start of a turn and after each step, until its words or its thinking text
  come. A room with people keeps its card as it was.
- A room whose turn ends close to the point where Claude Code would compact the conversation by itself (within
  `idle_compact_within` of the window, 0.05, so 40,000 tokens at an 800,000 window and 12,500 at 250,000) compacts it
  right then in the background, once, instead of inside your next message. A message you send while it runs waits for
  it and runs right after, as with the idle compaction, and its card says "shortening the conversation first" from the
  moment it arrives; that is new for the idle compaction too, where such a message showed no card until the compaction
  ended. A message already waiting when the turn ends runs at once, and the card says so if Claude Code compacts inside it.
- The `[code]` ceiling goes to 27,008: main's 26,968 plus this branch's 40.

**Upgrading**: budgets.toml `[session]` gains `idle_compact_within = 0.05`. A room's block in rooms.toml may set its own,
like `idle_compact_at` and `idle_compact_after`; 0 turns it off. Restart yerkd for it.

### need-2026-11-05-59 (#?)

- A room can now email someone for you from your own Gmail, with a file attached. Before, a room asked to send a
  statement and a document to a person whose only contact was an email address had no way to: the mail tool reads only,
  the `text` tool reaches phone numbers, and the browser is not signed into your Gmail, so it handed you the words to
  paste into an email yourself.
- The new `email` tool takes the addresses, a subject, the words and the files (whole paths in the room's own folders,
  a file you sent the room included). It is held in the room first, showing who it is from and goes to, the subject,
  each file by its name and size, and the words, never a path; your word sends that exact email once, and an edit is a
  new preview. A person's room never sends one, and neither does a worker.
- What cannot go is said before you are asked: no Gmail set up, a name in place of an address, nothing to send, a file
  outside the room's folders or missing, or files past 18 MB. A send that may have gone out (the line dropped mid-way)
  says so and is never sent again unasked. Each outcome is a gate row with who it went to and the id it went under.
- A file goes out as the bytes it held when you were shown it. The email was first held with its files by their paths
  only, so a file rewritten after your word went out as its new contents, and a file swapped for a link between the
  tool's check and its read attached a file from outside the room's folders. Now each file is read once, with no link on
  its way followed; your word is for those exact bytes (their sha256 is in the ledger's receipt), and they are what
  goes. A file changed after your word is held again with its new preview, and a link swapped in is refused.
- `yerk import gmail` and the tool read the app password the same way. The `[code]` ceiling goes to 26,462, and
  `yerk_tools` to 22,300 with the session total to 121,405.

**Upgrading**: to send, yerkd.toml needs `[gmail]` with `user` (your Gmail address) and `password_file` (a file in your
private folder holding a Gmail app password, 0600; Google makes one under 2-Step Verification), the same table
`yerk import gmail` reads; `name` sets the name your email goes out under. Restart yerkd for the new tool. With no
`[gmail]`, a room asked to email says what is missing and sends nothing.

### workflow-truth (#?)

- The GitHub Actions workflow runs the checks the project's own test machine runs, in its order, so a fork's CI is the
  real one: `uv lock --check` and the system tools the tests use (bubblewrap, ffmpeg) are new, and the line counts leave
  it. The web plug-in's job stays as it was. Nothing in the code changes.

### need-branch-drop (#?)

- A need's branch goes when the need goes live, on the pipeline's road and the hand road alike: deleted in the checkout,
  then on GitHub, and a plug-in branch it carried deleted in that plug-in's own repository, never on a remote. A shipped
  need no longer leaves a branch behind for a person to drop.
- A worktree a finished worker left on the branch is detached first, its files untouched, since git will not delete a
  branch a worktree is on. A branch the deployed commit does not hold stays, with its worktree and its branch on GitHub,
  and so does one git refuses; each is one log line and the need is live regardless. With no GitHub token the branch on
  GitHub stays. The `[code]` ceiling goes to 26,224.

### need-2026-11-04-53 (#?)

- A room can now say whether someone has texted you, or what a text said, even while your Mac is asleep. Before, a room
  read your Messages live over ssh, and a room asked one night whether a text had come in could not answer: your Mac had
  slept with its lid shut on battery, as it does every night by your own rule, and the read timed out.
- The `texts` tool reads your own recent texts (iMessage and SMS) from your Mac, newest first, in your own rooms only; it
  only reads and never sends one (that is `text`). Each message carries who it was with, when, which way it went and the
  words.
- Because the Mac sleeps, the newest texts are kept as a private copy under the state folder (0600, as private as the
  rest of it). A light daemon check reads them whenever the Mac answers, its own short wakes included, so the copy is no
  older than the Mac's last wake. A room asked while the Mac is asleep answers from the last copy and says that copy's
  time, so a stale answer is never passed off as a fresh one; with no copy made yet it says so rather than guess.
- Nothing here keeps the Mac awake or wakes it: ssh to a sleeping Mac fails fast and nothing is retried. The check reads
  the same Mac the `text` tool reaches (`[imessage] host`, else an owner room's `mac` add-on), by a read-only sqlite of
  the Messages database, so no Messages grant is needed, only an ssh login that can read that file. The `[code]` ceiling
  goes to 26,186.

**Upgrading**

- yerkd.toml: with a Mac set up for texts, the copy runs every 120 seconds by default. Set `[imessage] poll_seconds` to
  change the cadence, or `0` to turn the copy off (the tool then answers only while the Mac is awake). Reading needs the
  Mac's ssh login to have full-disk access to its Messages database; sending is unchanged.
- budgets.toml: `[session_start] yerk_tools` goes to 21,300 and `total` to 120,405 for the new tool, re-measured as sent
  on deploy.

## 2026-11-04

### need-2026-11-04-51 (#?)

- A live page can be one surface for code, talk, output and ink. Its chat now closes to nothing: drag the grip to the
  edge or press `close` on the chat, and `chat` on the bar brings it back; yerkd keeps the closed chat across restarts
  and every tab that opens shows it. While it is closed the talk lives in the program: a # line ended with Enter goes
  to the room at once (not after the 1.5 s pause), and the room's replies come back as #> lines the page writes under
  the owner's last # line, in the room's colour and marked as its own. The tab writes them in its own text, so the
  caret stays where it was and what was typed since is kept; the room is told in its next message when the chat closes
  or opens, and never reads its own #> lines as typed by the owner.
- The room's #> lines stop at the program's 100,000-character cap, in yerkd's copy and in every tab: what fits goes
  in, the rest is counted as on the chat ("... 20 more lines on the page's chat"), and the room hears once in its next
  message that the program is full. Before, replies grew the program past the cap with no tab open, and the next
  keystroke cut it on disk, the end of the owner's code with it. The room's marks under its new lines move down with
  them, so an arrow stays on the code it points at and no longer lands on a #> line. An Enter after a comment the
  room has already heard, such as a drill's header or a question the pause already sent, waits for the pause like any
  typing, so the line typed under it goes in the same message; before, it sent the room a message at once that held
  only a blank line.
- What a run prints shows large, above the tests' tiles, and its end rides in the room's next message. A program whose
  tests are blank runs alone, as a script, in the same sandbox, so a game or a script the room writes for a `#make`
  line runs on the page. A program the room draws whole keeps the caret where it was.
- **Upgrading**: budgets.toml raises `yerk_tools` to 22,500 and the session-start `total` to 121,605 (the `page` tool's
  two new clauses) and `[code] lines` to 26,634, each with its dated note; nothing to set.

### need-2026-11-04-50 (#?)

- Asking a room for the live page on your Mac works with nothing set in yerkd.toml, in every room. Before, the page
  tool's open with `where` mac said no Mac was set up for the page, and then would have said the Mac had no address,
  even on a box that already reached the Mac by ssh for the hands plug-in's `mac` tool and sat on your tailnet; and a
  room cannot set yerkd.toml while a conversation runs.
- The Mac: `[page] mac`, else `[imessage] host`, as before, else the Mac an owner room's add-on reaches: `ssh` in the
  `mac.json` beside its server's script (`{"ssh": "user@host"}`). It is read only from a file of yours that no other
  user may write and none under Yerk's state folder, where each room's folder is, and only as a host or user@host,
  never an ssh option, since the open hands the page's one-time address to that Mac.
- The address: with `[page] listen` unset, yerkd serves the page's routes on the box's own tailnet address too, as
  `tailscale ip -4` prints it at yerkd's start, and the Mac's open and `yerk room page` carry it. A LAN address is never
  taken unasked, since a LAN carries the page and its key in plain text. A box on no tailnet stays on loopback alone,
  and the Mac's open says the box has no address, naming `[page] listen` for a LAN address and `[page] address` for a
  WSL forward when Tailscale runs on Windows alone. The `[code]` ceiling goes to 25,992.

**Upgrading**

- yerkd.toml: on a box with Tailscale, the page's routes (and nothing else of yerkd's) are now on its tailnet address
  with nothing set, each still opened only with its room's key. To keep the page on loopback alone, set
  `[page] listen = "127.0.0.1"`. `[page] mac`, `[page] address` and `[page] listen` set to an address win as before.
- The hands plug-in keeps its Mac in `mac.json` beside server.mjs, where its `mac` tool and the browser's Mac read it
  (its branch `need-2026-11-04-50`); its rooms have it at their next session.

### recall-drop-2026-11-04 (#?)

- `yerk deploy` goes through again on the install it was written on. On Nov 4 it refused need-2026-11-04-49
  at the fast tests, merged and not deployed, and it would have refused any change after it: the recall bench read 103
  of 164 in the top six where the run of Nov 1 read 105, one under what the ratchet lets by. The index did that, not a
  change: two answers were crowded out of the top six by day-log lines written since, both still found whole, and the
  commit that passed this test at a deploy that afternoon read 103 that evening. A dated note under `[recall]` `drops` in
  budgets.toml names the drop, as the ratchet asks. Nothing changes for a room.

### need-2026-11-04-49 (#?)

- A `web_research` run always comes back with something to collect it by. Every start now carries a reference of
  Yerk's own (`yerk_`, the moment it went out, 16 random hex) in the run's metadata, which Exa keeps with the run, and
  the start may take the call's whole wait but its last 30 s (about 2 min 20 s), where every request to Exa used to stop
  at a minute. When Exa's answer to the start is late, lost, a server error (a gateway's 504 at a minute) or names no
  run, the plug-in reads Exa's newest runs for that reference every few seconds, up to 30 s, and the run it finds goes
  on as any other: polled to its end, its answer returned, or its run id for `web_research_result` when it is still
  going, with a line saying it was found by its reference.
- The call's wait now fits the room's own limit on the plug-in's server, so a run's id comes back before Claude Code
  cuts the call. Yerk writes each stdio server's `timeout` from rooms.toml into that server's environment in the room's
  mcp.json (`YERK_TOOL_TIMEOUT`, beside the table's own `env`; an HTTP server and one with no `timeout` are left as they
  are), and `web_research` and `web_research_result` wait 10 s less than it, 170 s at most (half of it under 20 s). In a
  room whose web-ladder table says `timeout = 60000`, a run still going after 50 s comes back as its run id, or its
  reference, where the call used to be cut at 60 s with nothing to collect, and `web_research_result` collects it in
  calls as long; a room at 180 s waits 170 s, as before. A lookup by reference begins no page of Exa's runs past the wait.
- A run Exa has not listed by the end of the wait comes back as its reference ("still starting", with the reason), and
  `web_research_result` takes the reference as it takes a run id: it finds the run among Exa's newest, page by page back
  to a minute before the reference was made, and returns it, or says none carries it yet; 15 minutes after the start
  with none, it says Exa never started that run and there is nothing to collect.
- The day's booking against the US$2 research cap stays on a start whose answer was lost, since Exa may have started
  and billed the run; a 4xx answer is still Exa's refusal and gives its booking back, as before. A run found by its
  reference writes its run row and its spend row once, when it is first seen finished.
- Here, `yerk explain research-ids` has the ability (tier key, on with the Exa key web_research runs on), with six
  phrases ("research timed out", "no run id", ...) that hand a room asked that way the entry whole. The "what can you
  do" answer drops its closing "`yerk explain NAME` has one." (the cite after it names the command) so the new
  "needs Exa" group fits under the 2,000-character recall cap, 1,998 as before; the `[code]` ceiling goes 1
  lower, to 25,939 with entrypoint-name's 4 lines in. The README says it in Add-ons (a few words shorter elsewhere on that line, to stay under 12,000 bytes) and the
  GUIDE's Web reach says how.
- No tool's description changes (the reference is "the run id web_research gave" as far as the room is concerned), so
  the web plug-in's tool entries and their budget line do not move. The plug-in's tests: 5 new in
  `plugins/web/test/tools.test.mjs` (the wall as the room met it: a call raced against a room's short limit, as Claude
  Code cuts it, gets its run id, its reference and its collected run back inside it (the same race is cut on the PR's
  first head, whose call waits 170 s);
  a create that never answers, found by its reference; a 504 at the create and a run Exa lists late; a run never
  listed, its reference collected later and refused when stale; the lookup's paging and where it stops), 3 changed (the
  create's body carries the reference; the run_id refusal names both forms; the command line's place has the limit);
  80 in all, 78 pass and the 2 live ones skip. Here, `tests/test_web.py` checks a person room's own 60 s table reaches
  its server as `YERK_TOOL_TIMEOUT` (it fails on the first head), and `tests/test_hooks_mcp.py` the 180 s one.
- Why: on 2026-11-04 a room's `web_research` at medium came back after 60 s as a timeout with no run id: Claude Code
  cut the call at that room's own limit on the server, a minute (a person room's own web-ladder table, which takes
  nothing from `[defaults]`; the box's carry `timeout = 60000`), while the plug-in waited up to 170 s, so
  `web_research_result` had nothing to collect and the room did the research by hand with `web_search` and `web_fetch`;
  the room filed the need (need 49 of 2026-11-04). This PR's first head blamed the plug-in's own minute on the create,
  which the reference still covers; its cold read found the room's limit.
- Tests only: the page test of the address printed and the Mac's open no longer fails now and then (box-ci red on
  py3.14 at this branch's first head). An earlier test, the owner's ask turns the page on, typed a line on the page
  and left that turn running on the real Claude Code; its "not answered" notice, printed about two seconds later,
  could land in the later test's captured output. That test now keeps what reaches the room, as the listen test
  does, and checks the line got there (the same change as need-2026-11-04-48's).

**Upgrading**: nothing to change by hand; every room that attaches the web plug-in starts its runs this way from its
next session after the deploy, its mcp.json written with each stdio server's limit then (a session already running
keeps the plug-in process it started with).

### entrypoint-name-2026-11-04 (#?)

- Every Claude Code process Yerk AI starts (each room's, each worker's, the nightly curation's and the account checks)
  goes out under Yerk AI's own name, `yerk`, where it used to carry Claude Code's headless default, `sdk-cli`, which
  a plan's meters were measured counting heavier for the same work. Artifacts stay off, since any name but `sdk-*`
  would turn them on and add a Scratchpad line to every request. Nothing else changes and nothing needs doing: every
  room has it from yerkd's restart at the deploy. The `[code]` ceiling goes to 25,940.

### fold-named-2026-11-04 (#?)

- A message you send while a room is still answering is answered by name, in every room. Yerk writes it into the
  running turn with one line ahead of it: sent so many seconds after the message above, while the room was answering,
  to be answered in that same reply, a correction replacing the question it corrects, and named whichever it answers
  (the number, the name, the thing), since you cannot see it was read. Before this, a quick fix ("41" a second after
  "who opened 40?") went into the turn with nothing saying so, and the one reply covering both answered the first and
  never said the second, so on a phone the second message read as unanswered.
- A reply that covers such a message and names none of its content bounces once at Stop, and the room writes the whole
  reply again naming it in its first words. Its content is its numbers (41 in "EX-41", never inside 410) and, in a
  message of three words or fewer, its words of three letters or more, filler such as "okay", "thanks" or "wait" aside.
  Never bounced: a message with no number and more than three words, a filler one, the turn's own first message, a
  reply of exactly NO_REPLY, Yerk's own messages, and a message Claude Code had not taken into that reply yet (it is
  answered as the next one). Each bounce is a `fold` row in the shadow ledger.
- A message from the channel a room watches goes in with no line ahead, since its own line says how it is answered,
  and so does a message that ran as its own turn after the turn it folded into failed. An "approve" typed while a turn
  runs still counts with the line ahead of it.
- The mechanics' line on a message arriving mid-turn says the same, so `mechanics` in budgets.toml goes to 6,200 and
  the session start's total to 119,405; the `[code]` ceiling goes to 25,936.

### need-2026-11-04-47 (#?)

- A room can compile LaTeX. The `render` tool takes a `.tex` in the room's work folder, compiles it with TeX Live's
  latexmk and returns the PDF, saved beside it as `NAME.pdf`, with its page count, so a room editing a resume in LaTeX
  can say whether it fits a page and send the PDF, with no trip through an online editor. Every room gets it, with a
  shell or without one. Before this, render took only LibreOffice's formats, and a room's shell had no TeX.
- The program is the one the file asks for: its `% !TEX program = xelatex` line, else xelatex or lualatex when the
  document (or a class file beside it) loads fontspec, unicode-math, polyglossia or Lua, else pdflatex.
- A compile that fails says TeX's first error and the line it stopped at (`resume.tex:12: Undefined control sequence.`),
  so the room can fix the source and run it again; an older `NAME.pdf` is kept until a run is clean.
- LaTeX runs in a sandbox of its own (bubblewrap): no network, nothing of your home, private or state folder, the
  document's folder read-only and shell escape off, since a `.tex` can name any file and yerkd can read your secrets.
  A `.tex` outside the room's work folder is refused in words, and with no bubblewrap that starts nothing runs.
- "What can you do" ends with "`yerk explain NAME` has one." (4 fewer) so the new entry's name fits under its cap.
  `yerk_tools` in budgets.toml goes to 20,300 and the session start's total to 119,205 (the render tool is 1,032 as
  built, 180 more), and the `[code]` ceiling to 25,869; the abilities entry is latex.
- Upgrading: install TeX Live on the box, latexmk and the programs (Debian or Ubuntu: `sudo apt install latexmk
  texlive-latex-extra texlive-fonts-extra texlive-xetex texlive-luatex`), and bubblewrap if it is not there already
  (a room's shell sandbox needs it too). Until then, render on a `.tex` says TeX Live is not on the machine.

### need-2026-11-03-45 (#?)

- The live page opens on your Mac and your phone, not only on the PC that hosts the box. Set `[page] listen` in
  yerkd.toml to the box's tailnet address (`tailscale ip -4` on the box prints it) or its LAN one, and yerkd serves the
  page there too, on its own port, beside loopback. Until you set it, nothing changes: the page stays on 127.0.0.1.
- Only the page is served there. `/page` and its calls answer on that address; `/mcp`, `/hook`, `/control` and
  `/status` are not found there whatever secret is sent, and the page's calls still open only with its room's key.
- The address carries that host. `yerk room page NAME` prints `http://<the box's address>:<port>/page#room=NAME&key=...`,
  the key still only in the fragment, which a browser never sends, so you can paste it on the Mac or the phone;
  `--here` prints the box's own 127.0.0.1 one. The page tool's open on the Mac now needs no `[page] address`: it carries
  the same host, with a one-time ticket, never the key. `[page] address`, when set, still wins for both, so a name
  the Mac knows the box by (`box.example.ts.net`) is the one printed and opened.
- yerkd binds the address even while the box does not have it yet, as at boot before tailscale is up, so the page is
  there the moment the tailnet comes up, with no restart; its log says so. A wildcard (`0.0.0.0`), a public address or
  a host name is refused, said in yerkd's log and in the room when the Mac's open needs it, and the page stays on
  loopback. An address off the tailnet is logged as carrying the page and its key unencrypted on that network.
- "What can you do" no longer lists `abilities` among the abilities, since the line before it names it; that makes room
  for the new entry under the per-message cap. The `[code]` ceiling goes to 25,739; the abilities entry is page-reach.
- Upgrading: to open the page from the Mac or the phone, set `listen = "<the box's tailnet address>"` under `[page]` in
  yerkd.toml and restart yerkd once. Set `[http] port` too, so the address stays the same across restarts. Tailscale on
  the box, the Mac and the phone is the encrypted way; a LAN address works without it, unencrypted. The forward that
  need-2026-11-03-49's notes asked for is no longer needed when yerkd runs tailscale itself.

### need-2026-11-03-49 (#?)

- The live page opens on the screen you are at: ask the room for the page on your Mac and the `page` tool's open, with
  its new `where` set to `mac`, opens it there, not in the box's own browser. `pc`, the box's own screen, stays the
  default, so an ask with no screen named opens where it did.
- yerkd reaches the Mac by ssh and runs `open` there on the page's one-time address, which it hands over on ssh's
  input, so neither the key nor the ticket is ever on a command line, the box's or the Mac's ssh. The address is yerkd as
  the Mac reaches it (`[page] address`), since 127.0.0.1 on the Mac is the Mac itself.
- A reload no longer strands the page. The page takes its key out of the address bar on load, so a reload or a retyped
  address used to come back as a bare `/page` that said it needs its address. Now the tab keeps its room and key in its
  own session storage: a reload or a retyped `/page` in that tab opens it again. The key is never in the address bar,
  the history or a bookmark, and it goes when the tab closes.
- A line typed on a page with no key no longer goes to yerkd only to come back as a refusal about a header: the tab says
  no room heard it, keeps the words in the box and says how to open the page (on a Mac, "open my page on my Mac").
- Said in words when it cannot open on the Mac: no Mac named, no address set, a Mac that does not answer over ssh, or a
  `where` that is neither `pc` nor `mac`. The page is on all the same.
- `yerk_tools` in budgets.toml goes to 20,100 and the session start's total to 119,005 (the tool is 1,901 as built, 110
  more), and the `[code]` ceiling to 25,626; the abilities entry is page-mac.
- Upgrading: to open the page on your Mac, set `[page] address` in yerkd.toml to yerkd as the Mac reaches it, the box's
  tailnet or LAN host and port (`"http://box.example.ts.net:8787"`; with no port, yerkd's own). Set `[page] mac` to the
  Mac's ssh host, unless `[imessage] host` is already that Mac. The box needs key-based ssh to the Mac (no password
  prompt) and a forward that reaches yerkd's port from the Mac. A tab opened before this change has no kept key: open
  the page once more and it keeps it from then on.

## 2026-11-03

### need-2026-11-03-48 (#?)

- A room's live page holds the whole conversation, so a night on it needs no Discord open: the chat is a pane of its
  own beside the program and its tests, with the owner's lines and the room's replies as a readable chat, each line
  time-stamped and the newest at the bottom; the column stays where you scrolled, with a button to the newest.
- Each reply is shown the way Discord shows it: bold, italic, underline, strike, spoilers, code and fenced blocks in a
  box of their own, headings, subtext, quotes, lists, a gap between thoughts and links that open in a tab of their own
  (http and https only). yerkd reads each line into parts and the page draws them, every word as text, never markup.
- The say box takes many lines: Enter sends, Shift+Enter starts a new line, it keeps the focus after a send, and words
  yerkd did not take go back in the box. A line said there shows on every open tab, not just the one that typed it.
- Drag the grip between the code and the chat, or press `wide chat` or `wide code`; a double click evens them, the
  arrow keys move the grip, and the browser keeps the split. On a narrow screen the chat goes under the code, and a
  narrow code side stacks the program over the tests.
- A whole-line `#` comment typed in the program is talk too: it shows as your line on the chat (one still being typed
  grows its own line) and goes to the room as said, beside the typing's diff.
- The newest 200 chat lines, yours and the room's, are kept in the room's `work/page/chat.json`, so a reload or a
  restart shows the night's talk again.
- Need 46's scoreboard and tiles and need 47's spaced bar cells are kept as they are; the bar spans the code, the grip
  and the chat.
- The `[code]` ceiling in budgets.toml goes to 25,560 (main's 25,451 plus 109); the abilities entry is page-chat, and
  the "what can you do" answer's own line drops ", built or known" (its census already says which) so it keeps every
  name under its 2,000.
- One long line on the chat no longer freezes yerkd: a mark that is never closed (`*`, `**`, `***`, `_`, `__`, `~~`, a
  link's `[`) is read only up to its own next mark, so a line costs time in step with its length (a 100,000-character row
  of `*a ` took 27 s on yerkd's only loop, every room's hooks and tools waiting; now well under a second), and each line
  is read once, kept beside its words and never in `chat.json`, not again for all 200 kept lines whenever a tab opens. A
  room's line is cut at the say box's 100,000 characters. Two marked words no longer read as one run (`**a** and **b**`
  was bold from the first `a` to the last `b`). The `[code]` ceiling goes to 25,568.
- A program of repeated lines no longer freezes yerkd: the `#` comments are found without a diff of the whole program
  (a line the program already had is never talk, so none is needed), and an unchanged program is not read at all; a
  line said beside 8,000 blank lines took 5 s on yerkd's only loop, now a few milliseconds. One message says the first
  60 comments typed, the most a message carries of the diff too, and counts the rest, so a paste of 25,000 comment
  lines no longer takes 10 s and sends the room a 1.3 MB message. The `[code]` ceiling goes to 25,572.
- Upgrading: nothing to set. An open page picks up the chat pane when its tab is reloaded.

### need-2026-11-03-47 (#?)

- A live page's top bar shows each label and value as a cell of its own, spaced from the next, in every room: the cells
  are a row with its own gap, wrapping on a narrow screen, each a box with its label over its value. A test now holds
  that layout, so the cells cannot run together again as they did when every one sat inside one span.
- A room never pads the bar's words to keep them apart: yerkd keeps each label and value as its words alone (a run of
  spaces of any kind, a no-break, thin or wide one, a tab or a blank filler, is one space, and none is kept at either
  end) before the 24 and 40 character caps, and drops a cell left with no words, a spacer, which no longer takes one of
  the 8. A padded bar reads and shows as the plain words: no blank inside a cell, and a tier word kept from its division
  by a no-break space is a rank badge in its tier's colour again.
- The `page` tool's answer says when it dropped padding, so the room stops putting it in.
- The `[code]` ceiling in budgets.toml goes to 25,451; the abilities entry is page-cells, and the "what can you do"
  answer's last line is cut to "`yerk explain NAME` has any one." (21 fewer) so it keeps every name under its 2,000.
- Upgrading: nothing to set. An open page shows the plain words from the room's next draw.

### need-2026-11-03-46 (#?)

- A room's live page looks like a game, not a form, the same for every room, and the room still sets only the words:
  the bar on top is a scoreboard, each test a tile, the hint a card under the program and the chat lines a readable
  column of their own beside the editors (under them on a narrow screen), all on a dark theme.
- The scoreboard is read from the bar's labels and values: a tier word, Iron to Challenger, with its division or none
  ("Gold II"), or any value under a rank label, is a rank badge in its tier's colour; `N/M`, `N of M`, `N%` or a number
  under LP or before LP is a bar filled that far (LP of 100) that slides when it moves; a number is big, a signed one
  green or red; anything else stays words. The countdown is a clock, amber in its last minute and red in its last 10 s.
- Each test is a green or red tile, a red one with the line it failed at and its message; a tile that is new or
  flipped pops, and the count pops when every test passes.
- Every word goes on the page as text, never markup, so no room's words restyle it. The `page` tool's own words say
  which words become a badge, a bar or a big number.
- `yerk_tools` in budgets.toml goes to 20,000 and the session start's total to 118,905 (the tool is 1,791 as built, 91
  more), and the `[code]` ceiling to 25,438; the abilities entry is page-look, and the "what can you do" answer's last
  line is cut by 7 so it keeps every name under its 2,000.
- Upgrading: nothing to set. An open page picks up the new look when its tab is reloaded.

### need-2026-11-03-44 (#?)

- The owner can ask a room for its live page ("open my page") and it comes up on the owner's screen: the `page` tool's
  new `open` turns the page on in the running yerkd with no restart, so the room's live turn and every other room go on
  untouched, and yerkd opens it with the box's own opener. Nobody edits rooms.toml, restarts yerkd or pastes an address.
- The switch is kept across restarts in the room's override, the daemon's own file in the state folder (the one the
  settings tool keeps the model in), over rooms.toml, which is never written. `off` turns the page off the same way:
  its open tabs' feeds end, and back on it has the same address.
- The address yerkd opens carries a one-time ticket in place of the key, since an opener's command line is readable by
  every local user: the page trades the ticket once, within a minute, for its key and takes it out of the address bar,
  and a used or older ticket gets nothing. No room ever holds the address or the key.
- The opener: `[page] open` in yerkd.toml (a list, the address added last), else `wslview` or Windows'
  `rundll32.exe url.dll,FileProtocolHandler` under WSL, `open` on a Mac, `xdg-open` on a Linux desktop with a display.
  Where nothing opens it, the page is still on and the room says `yerk room page NAME` on the box prints the address.
- Owner rooms only, as the page is: any other room's `open` is refused, with nothing written and nothing opened.
- `yerk_tools` in budgets.toml goes to 19,900 and the session start's total to 118,805 (the tool is 1,700 as built,
  253 more), and the `[code]` ceiling to 25,396; the abilities entry is page-switch, and the abilities line is cut by 14
  so the "what can you do" answer keeps every name under its 2,000.
- Upgrading: nothing to set. Under WSL the default opener needs Windows interop (on unless turned off in wsl.conf); a
  box with another way to its screen names it in `[page] open`.

### machine-room-send (#?)

- An owner room's send to the machine room (rooms.toml `role = "machine"`) no longer holds for the approval
  word: that line stays in the in-group, and the `need` tool already reaches the same room with no word. A
  family room's send there, and any send to any other room, hold as before.

### approve-first-word (#?)

- The owner's approval counts when `approve` is the first word of the message, with anything after it
  ("Approve, go ahead"), not only when it is the whole message. A sentence that merely contains the word
  ("I don't approve") still releases nothing.

### need-2026-11-03-43 (#?)

- A room can be a live page: `page = true` in its rooms.toml table gives it a browser tab, opened at the address
  `yerk room page NAME` prints, with a program in an editor, its unittest tests beside it, a bar on top and a draw
  layer. yerkd serves it on its own local port beside /mcp and /hook; the room and its key ride in the address's
  fragment, and the key opens that room's page calls and nothing else.
- What the owner does there reaches the room as the owner's messages, folded into a running turn as a Discord message
  is: each keystroke is a line in the room's `work/page/events.jsonl` at once, with the program and its tests beside it;
  a 1.5 s pause in typing sends one message with the change as a diff and the tests' newest result; a stroke (the lines
  it covers), a line said on the page, the countdown's end or a stall goes at once.
- The tests run with no model call, in a fresh folder under `python3 -I`, capped at 10 s and 1 GB, red to green on every
  pause in typing, each failure with its line. The `page` tool hands the runner code a model wrote, so every run goes in
  bubblewrap: no network, no home, state or private folder, nothing writable but its own folder, and a PID namespace that
  dies with it; with no bwrap that starts nothing runs and the run says so (the cold read read a dummy 0600 file, wrote
  outside and reached 1.1.1.1 through one tool call before it). `page = true` outside the owner's rooms is refused at
  the start, logged, and the tool refuses there too.
- The room's replies, notices and sends to itself render on every open tab beside Discord, and a new `page` tool draws:
  a new program or tests, marks on lines (a highlight, an arrow, each with a note), a hint, the bar's labels and values,
  a countdown and the stall wait. A tab that reconnects after a restart gets the whole state first.
- `yerk_tools` in budgets.toml goes to 19,650 and the session start's total to 118,555 (the tool is 1,447 as built), and
  the `[code]` ceiling to 25,245; the abilities entry is live-page.
- Upgrading: the box needs bubblewrap (`bwrap`, already there for Claude Code's sandbox) for the page's tests to run; a
  room that wants a page, an owner room, adds `page = true` to its table in rooms.toml and yerkd restarts; set `port` in
  yerkd.toml so the page's address stays the same across restarts. Rooms without the key see no change.

### changelog-numbers-2 (#?)

- Every entry since Oct 24 that read `(#?)` carries its pull request number, read from GitHub for the branch each
  entry names. Numbers up to #183 refer to the private archive repository, the repository as it was before the
  public cut of 2026-11-03 (its history rewritten for the cut, its pull requests kept there); later numbers refer
  to this repository. Nothing in the code changes.

### dialect (#183)

- The owner's own filler words, his slang, are out of the package: the pushback, lead, trailing and name-cut word lists
  in words.py and the one taste exception ship neutral words only (bro, man, dude, yo, lol and the like), and a new
  `[style] dialect` list in yerkd.toml adds the owner's own at daemon start (words.dialect), so his rooms read him as
  before and a stranger's install reads a stranger's words. Anything but a list of words there ends the start with one
  plain line. The taste test's fixture messages carry neutral words now.
- Upgrading: an install whose owner writes with his own fillers adds them to yerkd.toml, `[style] dialect = [...]`;
  without the key nothing else changes.

### first-run (#182)

- A first run of yerkd without yerkd.toml's `[discord]` table, or with a token file that is missing or empty, ends in one
  plain line naming what to write (the README's three-line table, the token in a 0600 file under secrets/) instead of a
  Python traceback. The README's install block runs yerkd before a new user has written that file, and every other
  config miss already failed this way.
- README, Install: says Linux with systemd (the jobs and workers are user units) and that `yerk abilities` names the
  tools some abilities add (ffmpeg, yt-dlp, LibreOffice, node); the yerkd line's comment says it refuses until
  yerkd.toml names the token. No setting changes.

### silent-worker-card (#181)

- A worker that ends with nothing to say (its last message exactly NO_REPLY, as a scheduled job's is when it has nothing
  for the room) now settles its card as one line, `🟢 worker finished clean, nothing to add`, over the time it took and
  "report saved" with `yerk worker report <id>`. Before, the card read `🟢 worker finished clean`, then the word NO_REPLY
  itself, then every step row: one scheduled job that runs three times a day had left eight such cards since Nov 1, and
  the owner replied to the latest asking what it did.
- What stays: the steps are in the worker's TASKS.md and its report, behind `yerk worker report <id>`; the footer shows
  the account when the install has more than one and the report's path under presentation.paths = owner, as before. The
  card is never deleted: a turn that ends silent keeps its one line, and a worker does the same. A job's silence is as
  it was, no relay turn and its run's ledger row saying no_reply, silent (NO_REPLY). A worker that dies, times out or is
  stopped after saying NO_REPLY still shows how it ended, and every other end settles exactly as before.
- The card settles quiet both when the daemon sees the worker end and when a restart finds one that ended while the
  daemon was away.
- The tests: 2 new in tests/test_workers.py (a NO_REPLY end settling quiet live and at a re-attach, read the way the job
  hook reads it, and a death after NO_REPLY still a death; every other end rendering as main rendered it, line for line,
  and the quiet card keeping the account and the path), the quiet card held to the style rules in tests/test_style.py
  and a silent job's card checked in tests/test_jobs.py; 1,458 in all. budgets.toml [code] lines 24,572 + 6 = 24,578,
  with its dated note; docs/budgets.md is what `yerk docs own` writes.

**Upgrading**: nothing to change; a worker that ends after the deploy's restart settles this way. A card that already
settled keeps its text.

### card-steps (#179)

- A room with people can show its steps on its progress card now: `card = "steps"` in its block in rooms.toml gives it
  an owner room's card, the model's newest line and one row per step, each marked done or not done, two or more failed
  steps in a row folded into one and the oldest rows counted on an "N earlier steps" line, as the owner's rooms show
  them. Before, every room with people showed its newest holding line and "step 1", "step 2" and so on, never a row; the
  owner preferred the rows once he saw them in a room with people.
- Every other room with people keeps the card it has had since 2026-10-21 (a person's card listed the steps of a file
  build): with the key unset, or set to any other value, it shows its holding line and a step count, unchanged. The
  thinking line stays an owner room's alone; a room that shows the rows shows the model's newest line, not its holding
  line. An owner room's card is unchanged, with or without the key.
- budgets.toml: `[code]` lines 24,569 + 3 = 24,572 on main after #180, with its dated note and a merge note;
  docs/settings.md (the key's line) and docs/budgets.md are what `yerk docs own` writes, the GUIDE's "How a room runs"
  and rooms.toml.example say it.

**Upgrading**: to show the rows in a person room, add `card = "steps"` to its block in rooms.toml; rooms.toml is read
at yerkd's start, so the rows show from the next restart. Nothing to change for any other room.

### trusted-wall (#180)

- A trusted room's shell now reads nothing of the owner's home but the room's own folders (its layer, its work folder
  and the files people send it) and the Python user site-packages, read only: every entry of the home that holds none
  of the room's own folders is denied to the shell whole, for reading and writing, and one that holds one is walked the
  same way, its other entries denied. Before, only the folders the daemon knew were denied (the private and state
  folders around the room's own, the memory roots, the other rooms' layers and `read` folders, `projects/` under each
  account's folder, and the home's folders that hold one of those), and Claude Code's sandbox reads the whole computer
  but what is denied: on 2026-11-03 the one trusted room's 88 denies left `~/.ssh`, every account's token file, the daily
  backup archives, the shell history and `~/.claude.json` readable to its shell. Around the home alone, that box's wall
  is 164 denies now.
- The explicit roots stay, for a private, state or memory folder outside the home; a layer set directly in the home
  stays open to its room; a home entry that is a link is denied by its target, as before. `yerk explain trusted` and
  docs/GUIDE.md ("How a room runs") say it.
- The tests: 3 new in tests/test_trusted_room.py (a fake home's key, token file, backup and shell history walled, the
  room's own folders open and a sibling room's layer walled; a layer directly in the home kept; each deny once and a
  parent's taking its children's), 1,454 in all. budgets.toml [code] lines 24,568 + 1 = 24,569, with its dated note.
- Why: a read of the one trusted room's live settings found what the deny list did not name open to its shell; a
  stopgap `denyRead` of the home's other entries went into the install's rooms.toml, and this is the fix that lets it go.

**Upgrading**: nothing to change in rooms.toml; every trusted room has the wall from its next process start after the
deploy. A `[rooms.NAME.sandbox.filesystem] denyRead` list that only repeated home folders can be removed.

## 2026-11-02

### need-2026-11-02-38 (#178)

- Every room now writes a text file in its own work folder without waiting on a prompt: its Write and Edit tools, and a
  shell redirect like `printf ... > <work folder>/sheet.md`, land there as called. Before, an owner room asked to write a
  sheet in its work folder got "Output redirection to '...' needs approval" from Claude Code three times, with nobody
  at the keyboard to say yes, and handed its work over without the sheet. Claude Code 2.1.316 auto-allows a command its
  sandbox runs, but asks in a room's default mode for every Write and Edit, and for any shell write its sandbox does not
  auto-allow, unless a rule covers the path; the daemon now writes that one rule, `Edit` on the room's own work folder,
  into every room's settings, as a trusted room already had. Proven on 2.1.316 by a scratch probe: without the rule the
  redirect and the Write tool asked inside the work folder; with it both landed, and both still asked outside it.
- A write anywhere else is asked as before; a trusted room keeps Write and Edit on its layer too, and its outbox stays
  read only. Here, `yerk explain work-writes` has the ability. Nothing to set: the next process start of each room
  carries the rule.

### merged-replay (#177)

- Several messages sent while a room is still answering are now each marked answered by the reply that answers them:
  the check mark lands on the message that reply answers, its card closes, and once the last is answered the room goes
  idle (idle close and idle compaction run again). Claude Code 2.1.316 takes the messages still waiting when a reply
  ends as one message and answers them once; the room counted that one as a single message. Before, when
  four one-character messages came in two seconds, the room stayed at "working on it", each reply left a fresh
  "working on it" card, every later reply was credited to the message two back, and only /stop cleared it, marking two
  messages that had been answered "not answered".
- A message written after the last of a turn's replays still waits for the next reply, as before.

- A room asked about the answer to a House of Commons written question now gets it: `web_fetch` of the question's page
  (`ourcommons.ca/written-questions/<parliament>-<session>/q-<n>`, or the French site's `noscommunes.ca/questions-ecrites/...`)
  returns the question, its details line (who asked, the dates, its status) and each response tabled for it, every
  section (a cover page, then one per federal institution) as markdown with its tables filled, in the page's order. The
  page loads its responses only after it opens, by a request carrying the token in the page's head and the cookie the
  page set, and the site sends any other request to its error page, so a plain read had the question alone and a
  browser at most one section. F1, the plain read from the machine, now makes that request as the page does: one more
  request a response, free, under a second and a half for the whole read live.
- A `section` in the address keeps that section (in any case), the rest listed by name and code; one the response
  lacks gives every section, said so. The full text view's address (`.../q-<n>/<response id>`) reads the same.
- A question still awaiting its response says so with the date it is due; an older one whose response is a sessional
  paper the Library of Parliament holds names the paper and its link; a refused or unreadable request is named (`HTTP
  400`), and the read says its text is the question alone, never passing it off as the whole page. The call log carries
  the reader's word (`answers`: `read`, `awaiting`, `library`, `http_400` and so on).
- Only F1 can ask: site memory never skips F1 on such a page, and a read of one at another rung (the machine's browser
  after F1 failed, or a paid rung) says it may hold the question alone.
- Here, `yerk explain commons-answers` has the ability, with six phrases ("written question", "sessional paper", ...)
  that hand a room asked that way the entry whole. The `abilities` entry's lead sentence is 32 characters shorter ("each
  ability it ships or knows how to build"): its "what can you do" answer, every ability's name whole under the
  2,000-character recall cap, stood at 2,000 to the character, so the new name did not fit; it is 1,986 now. The README
  says it in Add-ons (three of its phrases a few words shorter, to stay under 12,000 bytes), the GUIDE's Web reach says
  how, and its count is what the file holds, 103: 42 built.
- The plug-in's tests: 8 new in `plugins/web/test/commons.test.mjs`, on a placeholder question page and answer shaped
  like the live ones and the site's refusal mirrored; 75 in all, 73 pass and the 2 live ones skip. No tool's description
  grows and nothing in the harness's code changes, so no budget line moves.
- Why: on 2026-11-02 a room asked about a government answer tabled to a written question; `web_fetch` of the question's
  page, with and without a `section`, returned only the question's text, the institutions' responses never rendering,
  and the ladder stopped at F1, which took the page for read; the room filed the need (need 37 of 2026-11-02).

**Upgrading**: nothing to change by hand; every room that attaches the web plug-in reads written questions this way
from its next call after the deploy.

### need-2026-11-02-36 (#175)

- A need whose fix lives in a plug-in's own code is built there now. An add-on whose code is a local git repository of
  its own (a room's mcp server whose command or an argument is a file in a git checkout other than Yerk's, the home
  folder or the root) is named in the builder's brief, with its path and the branch that checkout is on, the code its
  rooms run, and the brief says a fix living there is built there, on a branch of the need's name, with the plug-in's
  own tests, never described in abilities.toml as out of reach. Nothing to set: the repositories come from rooms.toml's
  mcp tables.
- The pipeline carries that branch to live. The build's end finds it (the machine room's line says so, and `yerk needs
  list` names its head); a fix worker is pointed at it; the cold read gets its diff and its tests at its head as it
  stands when the read starts; and the merge checks it lands as a fast-forward of the checkout the rooms run (the
  branch not moved since the read, that checkout on its branch, nothing uncommitted) before it merges the pull request,
  then fast-forwards that checkout to the head the read saw. One that cannot land holds the need with why, nothing
  merged; a pull request a person merges lands it the same way, and `yerk needs done` lands it on the hand road. A room
  runs it from its next session.
- budgets.toml: `[code]` lines 24,432 to 24,550, with its dated note; docs/budgets.md is what `yerk docs own` writes.
  The abilities entry `plugin-builds` (`yerk explain plugin-builds`); the README says it in Add-ons (three of its
  sentences a few words shorter, to stay under 12,000 bytes), and the GUIDE's count is what the file holds, 102: 41
  built.
- Why: a room's wall was a gate in a plug-in's own code, the browser plug-in kept in a git repository outside this one;
  the builder only ever edited Yerk's repo, so a first build described the fix as out of reach, the wall came back
  twice, and the plug-in's changes were landed by hand (need 36 of 2026-11-02).

**Upgrading**: nothing to set. For the merge to land a fix in a plug-in, the checkout its rooms run stays on its branch
with its changes committed; one that is not holds the need, said in the machine room with why.

### need-2026-11-01-33 (#173)

- A room can use a link in one of the owner's emails now: the hands plug-in's mail `read` lists the mail's links after
  its text, each link's words beside its address (`- Cancel: https://...`), from the mail's HTML part, web addresses
  only, in page order, each whole or not at all, up to the read's `max_chars` of them, inside the mail's untrusted
  wrap. The room opens the one it was asked for with the browser's `open`, by its address, so the mailbox's web page is
  never driven for it: no sign-in, no search row to click, no window to show. The mail's text strips the HTML's tags,
  so a link's words ("Modify Cancel") came with no address, and a token only the link carries never reached a room.
- The reader opens none. The read's head counts the links and says that opening one is clicking it in the mail, which
  can act by itself (cancel, confirm, unsubscribe), so a room opens one only for what the owner asked; the page it
  lands on keeps every browser guard, a cancel or confirm click there still on the owner's word. A mail with no HTML
  part has its addresses in its own text: no list, and the head as before.
- The change itself is in the hands plug-in's own repository (`~/.local/share/hands-mcp`, a local git tree with no
  remote), on its branch `need-2026-11-01-33` (3c3c56c, cut from its main 6fc2b7a), so its main fast-forwards to it;
  the plug-in's offline tests there, 44 of 44 green (two new: its real reader against a fake IMAP server on a
  placeholder booking mail, and the size cap with a link-less control; both fail on its main). Here, its tools'
  budget line and the abilities entry (`yerk explain mail-links`), with eight phrases ("cancel link", "link in the
  email", ...) that hand a room asked that way the entry whole; the README says it in Add-ons (five of its clauses a
  few words shorter, to stay under 12,000 bytes), and the GUIDE's count is what the file holds, 101: 40 built.
- budgets.toml: the hands line 8,121 to 8,165 (the mail entry 1,194 as the plug-in's branch builds it, 1,225 as sent)
  and the session-start total by the 44 to 117,105, each with its dated note; docs/budgets.md is what `yerk docs own`
  writes.
- Why: on 2026-11-01 a room asked to cancel a booking by the Cancel link in its confirmation mail read the mail and had
  the link's words and no address; the mailbox's web page in the browser would not open the mail by its row, and the
  booking site's own cancel page needs the token only that link carries, so the room filed the need (need 33 of
  2026-11-01).

**Upgrading**: merge this before, or with, the plug-in's deploy, which is fast-forwarding hands-mcp's main to
`need-2026-11-01-33` (3c3c56c); a room has the links from its next process start after that. budgets.toml's hands line
and `total` move with this change, so a deploy after the plug-in's branch is in does not refuse on its size; until
then a room sends the mail entry at its old 1,181, under the new line.

### need-2026-10-30-25 (#171)

- A room can watch a video attached to a Discord message now: `watch` takes the message's link as its `url`
  (Discord's Copy Message Link, discord.com/channels/SERVER/CHANNEL/MESSAGE). The daemon's bot reads that one message,
  saves its video to the room's `work/inbox/<message id>/` as a file sent to the room is saved (its own name, a ledger
  row), and watches it there as `path` does: the same frames, the transcript beside the file, paid once. A message with
  more videos has the first watched and the rest saved, named with their paths for `path`; one past 300 MB is named
  and never fetched. yt-dlp never runs on a Discord link, where it said "Unsupported URL".
- Where: a room reads a message in its own channel or the one it watches; an owner's room also one in any channel of
  the server its own channel is in. Any other is refused in one line, and its message is never read; the channel's
  server is the one Discord gives, never the one the link names. A message gone, a channel the bot cannot read, a
  message with no video (its files named) and a room off Discord each come back in words.
- The adapter's docstring names this one read past a room's channels. abilities.toml has the built `discord-video`
  entry and a line under `watch`; the README says it beside a file sent to the room (three of its clauses a few words
  shorter, to stay under 12,000 bytes), and the GUIDE's count is what the file holds, 100: 40 built; docs/tools.md and
  docs/budgets.md are what `yerk docs own` writes.
- budgets.toml: `yerk_tools` 18,000 to 18,200 (the watch tool's sentence, 1,768 to 1,992 as built, all 18 about 18,138
  as sent) and the session-start total by the 200 to 117,061; [code] lines 24,368 + 64 = 24,432; each with its dated
  note.
- Why: on 2026-10-30 a room asked to watch videos another assistant had posted as mp4 files in a Discord channel read
  the channel through a plug-in's history, which gave each file's name and no address, and `watch` handed the
  message's link to yt-dlp, which refused it; the room read the documents the videos were made from instead and filed
  the need (need 25 of 2026-10-30).

**Upgrading**: nothing to change by hand; every room has it from yerkd's restart at the deploy. The bot reads a
message outside a room's channels only where it already has View Channel and Read Message History. budgets.toml's
`yerk_tools`, `total` and [code] `lines` move with this change.

### need-2026-10-28-28 (#170)

- A room can send a picture of what one of its tabs shows now: the hands plug-in's browser gains `shot`, which saves a
  PNG of a tab or a popup the room opened, the page as rendered and scrolled where it is, in the room's own work folder
  (`hands-shots/`), and gives back its path; the room opens it with Read and sends it with `send`, which takes any file
  in its work folder. The browser draws the tab itself over its developer protocol, so a tab with no window on the
  owner's screen is shot. The desktop's screenshot shoots the screen, where that browser shows nothing; a shot never
  takes the owner's screen, and the browser's window is never shown for it.
- A read: no owner's word and no payment check, under every browser call's guards (the room's own tabs, owner rooms
  only, the brake's lease). A file that did not arrive or is not a PNG is said so and never named, and a second shot in
  the same second takes a new name, never the first one's. The active tab of a minimized window never paints: after the
  browser's 25 s wait its words reach the room with the way past them, to open the page again in a background tab and
  shoot that one.
- The change itself is in the hands plug-in's own repository (`~/.local/share/hands-mcp`, a local git tree with no
  remote): `shot` on its branch `need-2026-10-28-28` (06baf79, cut from its 64af176), merged onto the plug-in's main
  2fc539f, which carries `upload`, on its branch `need-2026-10-28-28-merge` (ee2deda), so its main fast-forwards to
  both; the plug-in's offline tests there, 40 of 40 green. Here, its tools' budget line and the abilities entry
  (`yerk explain tab-shots`), with eight phrases ("screenshot of the page", "pic of the page", ...) that hand a room
  asked that way the entry whole, never the desktop's; the README says it in Add-ons (four of its clauses a few words
  shorter, to stay under 12,000 bytes), and the GUIDE's count is what the file holds, 99: 39 built.
- budgets.toml: the hands line 7,900 to 8,121 (the browser 3,048 as the merge builds it, about 3,079 as sent) and the
  session-start total by the 221 to 116,861, each with its dated note; docs/budgets.md is what `yerk docs own` writes.
- Why: on 2026-10-28 a room asked to send a picture of the web form it was filling in, in the browser on the owner's
  PC, found no screenshot among the browser's actions, and the desktop's screenshot shoots the owner's screen, where
  that browser shows no window; it described the form in words instead and filed the need (need 28 of 2026-10-28).

**Upgrading**: merge this before, or with, the plug-in's deploy, which is fast-forwarding hands-mcp's main to
`need-2026-10-28-28-merge` (ee2deda); a room has `shot` from its next process start after that. Moving the plug-in's
main to `need-2026-10-28-28` (06baf79) instead would drop `upload`. budgets.toml's hands line and `total` move with this
change, so a deploy after the plug-in's merge is in does not refuse on its size. need-2026-10-26-23's plug-in branch
(e94ce96) still goes live by a merge of its own, as need-2026-11-01-31's block says, now onto ee2deda.

### need-2026-10-28-25 (#169)

- A room told its rules changed reads every changed line now, whatever its privacy. When the lines new or replaced in
  the mechanics, the room's rulebook or the shared rules file since its session last looked are past the note's 1,500
  characters (budgets.toml `[per_message.on_hit] rules`), the note gives their count and names `rules-changed.md` in
  the room's own work folder, which holds each of those lines whole, written 0600; told once per version, as before.
  Every room's Read reaches its work folder, the folder its process runs in. The note used to name the mechanics' own
  file in Yerk's checkout, which no room's Read reached then and only an owner room's does now, and the rules file by
  `read_memory`, which gave the file whole rather than what changed.
- A note inside its size is as it was, the lines themselves, and leaves no file. A sealed room still hears none.
- abilities.toml has the built `rules-changed` entry; the README names the part among what each message gets (four of
  its clauses a few words shorter, to stay under 12,000 bytes), the GUIDE has a paragraph under Yerk knows its harness,
  and its count is what the file holds, 98: 39 built; docs/budgets.md is what `yerk docs own` writes.
- budgets.toml: [code] lines 24,363 + 5 = 24,368 on main after #167, with its dated note and a merge note. The capped
  assembly fixture is retaken, its one change this note's last words.
- Why: on 2026-10-28 a room handed a rules-changed note naming the mechanics file in Yerk's checkout had its Read of
  that file refused, kept working on the mechanics its session started with, and filed the need (need 25 of 2026-10-28).

**Upgrading**: nothing to change by hand; every room has it from yerkd's restart at the deploy. budgets.toml's [code]
`lines` moves with this change.

### need-2026-11-01-31 (#157)

- A room can put a picture on a web form now: the hands plug-in's browser gains `upload`, which puts a file from the
  room's own work folder (a picture sent to the room, a saved picture, a screenshot) on a page's file input, in the
  browser on the owner's PC or Mac. A file input takes no typed value, so `set` on one met an InvalidStateError and the
  form would not send; that error now says to use `upload`. The plug-in copies the file to the browser's own computer
  first (over ssh to a Mac), a folder per call, kept a day for a form that reads it only when it sends, then sets it
  over the browser's developer protocol, as picking it in the file dialog would. A file outside the room's work folder,
  a link out of it, or one past 20 MB is refused; every upload waits for the owner's word, since a page may send the
  file the moment it is picked, and nothing is put on a payment page.
- The change itself is in the hands plug-in's own repository (`~/.local/share/hands-mcp`, a local git tree with no
  remote), on its branch of the same name; its main was fast-forwarded to that branch (2fc539f) on 2026-11-02 after one
  real upload on a test page passed in a browser profile on the PC and in one on the Mac, so a room has `upload` from
  its next session. Here, its tools' budget line and the abilities entry (`yerk explain form-files`); the README says
  it in Add-ons, and the GUIDE's count is what the file holds, 97: 38 built.
- Why: on 2026-11-01 a room filled two listing forms in the browser on the owner's computer, and both came back "fix
  the errors on the page": the site wants at least one photo, and nothing could put one on the form.

**Upgrading**: the hands plug-in's main is already at 2fc539f (fast-forwarded on 2026-11-02, before this merge), so
nothing to change by hand. budgets.toml moves with it: the hands plug-in's line to 7,900 and the session-start total to
116,640, so a deploy after the plug-in's branch is in does not refuse on its size. need-2026-10-26-23's plug-in branch
(e94ce96, cut from the plug-in's 64af176) is no longer a fast-forward of that main: both change the payment-page guard
in tools.mjs (`set and click` there, `set, click and upload` here) and the plug-in README's test count, so
free-bookings goes live by merging that branch onto 2fc539f, its look-alike rule covering `upload` too, and running the
plug-in's tests again; moving the plug-in's main to e94ce96, as that block's Upgrading reads, would drop `upload`.

### reattach-race (#167)

- A worker is no longer settled as lost a second after its start while its unit runs. The minute's backstop pass
  (`Workers.reconcile`, which made the call that night) and a start's re-attach (`Workers.reattach`) each read the unit
  list first and the running workers after, and a worker started in between, in the ledger and watched before
  `systemd-run` had made its unit, was in no list and settled as lost on that one read: on 2026-11-02 the
  journal has `worker 99319d: lost (the unit is gone and left no exit record)` one second after its `started` line,
  so need 22 was held for a person ("the cold read gave no verdict") and account two, one worker at a time under
  `[workers] per_account` (1 by default), went to the next need's builder, while 99319d, the need's cold read, ran on
  to its `merge` verdict and exited clean seven minutes later. Now a worker one list missed gets a second look at its unit
  first, and one under two minutes old is kept for a later pass, its unit maybe not listed yet; a unit both looks
  miss, with no exit file, in a worker past its first two minutes is lost, as before, and a look that saves one logs
  `the unit list was stale, re-attached`.

**Upgrading**: nothing to change by hand; the daemon looks twice from its next start. budgets.toml moves with the code:
the code ceiling to 24,363.

### need-2026-10-24-22 (#160)

- A room sent a video file now sees it: `watch` takes `path`, the file's saved path from its `[attachment]` line, in place
  of a link, and gives what a link gives: frames spread evenly through it (6, or 1 to 12) that the room opens with Read,
  and a transcript of its sound. Nothing is pulled and yt-dlp never runs. The frames go to
  `videos/file-<its name>-<10 hex of its path>/` in the room's work folder, so two files of one name never share them, and
  the nightly sweep may take them, since a later call makes them again; the file stays in the inbox as it was sent. The
  transcript is saved beside it as `NAME.transcript.txt`, where `transcribe` writes one, on the same keys and US$1 gate,
  each paid call a spend row under `watch`, and one already there is read, so a video watched or transcribed before is
  never paid for twice. A video with no sound gives its frames, and a file with no picture gives its words; the note says why.
- Only the room's own files: a path outside its folders (a link out included, since the path is resolved first), a
  missing file, and a call with both a link and a path are refused in words. A link the room's shell left where the
  frames go is never written through.
- A video's `[attachment]` line gains a `[video]` line: the room sees a video only as the frames `watch` takes, so it
  calls `watch` on that path before it answers. A turn that ends without that call is bounced once at Stop, naming the
  file, as a picture never read and a voice note never transcribed are; a Read of a video opens nothing, and `transcribe`
  on one gives its sound alone, so neither counts. A `.webm` stays a recording, as before, since it can be either.
- The card says `watching the video clip`, the file's plain name, never its path. The mechanics' watch line names the
  file's way, the tool gains a sentence, and `yerk explain video-file` has the ability; `yerk explain watch` points to it.
  The GUIDE has it under Video files, and its count is what the file holds, 96: 38 built; the README, at its 12,000
  bytes on main, is left as it is.
- Why: on 2026-10-24 a room asked to say what was in a video file sent to it found Read could not open it, and its two
  tries at ffmpeg in its shell each waited on an approval no one could give mid-turn; it answered off a thumbnail in a
  screenshot sent with it. On 2026-10-28 another room asked to watch a 9-second clip, not to listen to it, got the sound
  alone from `transcribe`, and `watch` took only a link (need 22 of 2026-10-24, filed twice).

**Upgrading**: nothing to change by hand; ffmpeg and ffprobe already on yerkd's PATH serve it from the next start, and
each room has the mechanics line from its next process start. budgets.toml moves with the code, on main's numbers after
#161 to #166 and #168: Yerk AI's tools to 18,000, the session-start total to 116,202, the code ceiling to 24,347; the
mechanics (5,888) stay under their 6,000, which leaves a checkout's path of up to 118 characters.

### need-2026-10-27-23 (#168)

- An owner's room can see a branch or a pull request of Yerk's own repository as GitHub has it now: the new
  `github_state` tool on Yerk's own server takes a branch's name (a worker's) or a pull request's number (12, #12, PR 12
  or its address). The repository is private, so a room's web tools get a 404 there, and no room holds a GitHub login;
  the daemon asks GitHub's REST API on the token the needs pipeline already uses, `secrets/github.token`, sent in a
  header and never in a result, a log or a card.
- It returns the branch's head on GitHub (`github`: its commit and how long ago it was made; null when GitHub has no
  such branch, as after a merge that deleted it), the same branch in Yerk's checkout against it (`here`: its head,
  whether GitHub has that same commit, and how many commits each side lacks when both are in the checkout; read only,
  no fetch and no ref written), and the branch's pull requests, the newest five, each open, draft, merged or closed with
  when, and an open one's commit statuses (`checks`, and `red` naming each that failed). So "committed, not pushed" and
  "pushed, its pull request open and green" are read, not guessed.
- Yerk's own repository only: `[needs] repo` in yerkd.toml, else the checkout's origin; another repository's address is
  refused in one line. The owner's rooms only, since the repository is the owner's; a person's room hears that in one
  line. A name that is no branch (an option, a range, a space) is refused before anything is sent.
- With no token the tool says, in one line, the file that turns it on and asks GitHub nothing; a token that does not
  reach the repository (GitHub's 401, 403 or 404 on the repository itself), a pull request that is not there and GitHub
  not answering are each said in words. `yerk abilities` lists `github-state` as needing GitHub until the token file is
  there (abilities.toml's `github` key is that file). Free: no spend row.
- The card's row reads "checking need-example-1 on GitHub", never the call.
- abilities.toml has the built `github-state` entry; the README names it among Yerk's tools (its inbox sentence a clause
  shorter, to stay under 12,000 bytes), the GUIDE has a section, A branch on GitHub, and its count is what the file
  holds, 95: 37 built; docs/tools.md and docs/budgets.md are what `yerk docs own` writes.
- budgets.toml: `yerk_tools` 16,900 to 17,700 (the tool is 822 as built on Python 3.13, about 853 as sent) and the
  session total by the 800 to 115,902; [code] lines 24,148 + 129 = 24,277; each with its dated note.
- Why: on 2026-10-27 a room asked to tell a worker's outcome (its branch committed, not pushed), and whether the branch
  or its pull request had since reached GitHub, fetched the repository's pull requests from GitHub's API and got a 404:
  the repository is private and the room has no login. It said the push state was unknown from there and filed the need.

**Upgrading**: nothing to change where the needs pipeline already has its token: the tool reads
`secrets/github.token` in the private folder (one line, 0600), which needs read access to the repository's contents,
pull requests and commit statuses. A room has the tool from its next session start after the deploy. budgets.toml's
`yerk_tools`, `total` and [code] `lines` move with this change.

### need-2026-10-26-23 (#166)

- A room the owner asks to book a free table can fill and confirm its booking page now, though the page's address
  looks like a checkout. The browser plug-in kept outside this repository (hands, attached to every owner room under
  rooms.toml's `[defaults]`) judged a payment page by its address alone, so a booking's Confirm page on a `/checkout/`
  address (a name, a phone number, an email, a Confirm button; no card, no deposit) had every field and click refused,
  the owner's own order included.
- The page decides now, not its address, on the plug-in's branch `need-2026-10-26-23`: its read of a tab also takes
  the first money the page's whole text shows, an amount (a currency sign or code beside a number) or a saved card (its
  last four digits after a mask or after "ending in"). A checkout address whose page shows no card field, no payment
  frame, no amount and no saved card is a look-alike: each field and each click there goes only with `confirmed: true`,
  the owner's own order, and without it the refusal asks for that word. A read of the page says which it is.
- A payment page is still never touched, confirmed or not: a card field, a payment frame, a payment service's host, or
  a checkout address whose words show money; the refusal now says what it saw. A page off a checkout address that shows
  prices (a menu, a shop) is judged as before, and opening a checkout address is still refused, since only the page can
  say whether it takes money. The plug-in's offline tests run its real page read against a stub page: 31 of 31 green.
- Here, abilities.toml has the described `free-bookings` entry (the browser ability is not in this repo), with six
  phrases ("book a table", "make a reservation", ...) that hand a room asked that way the entry whole
  (`yerk explain free-bookings`); the README says it in Add-ons (its web plug-in aside cut to stay under 12,000 bytes),
  and the GUIDE's count is what the file holds, 94: 36 built. The plug-in's tool text does not change, so no budget
  line moves.
- Why: on 2026-10-26 a room the owner told to book a table reached the booking's Confirm page and had its four fields
  and a checkbox refused as a payment page eleven times, once with the owner's confirmation on the call.

**Upgrading**: nothing to change here. The plug-in's change is live when hands-mcp's main moves to its branch
`need-2026-10-26-23` (e94ce96, on its main 64af176); a room has it from its next process start after that.

### need-2026-10-26-22 (#165)

- A room can search the web by a picture now, as Google Lens or TinEye do: the new `reverse_image_search` tool on Yerk's
  own server takes a picture in the room's own folders (one sent to it, a picture `web_image` saved, a frame `watch`
  took) and returns Google's best guess at what it is, the names it ties to it with their scores (a work and its maker,
  a place, a product), each page that shows it, whole or in part, and the addresses of its copies, parts and look-alikes,
  up to 20 of each. It is in every room's allow list, a person's room too, so one deploy gives every room the same way.
- It runs on Google Cloud Vision's web detection, the documented API of Google's search by picture: the picture's own
  bytes go in the request, so no picture is put at a public address for a search to fetch, and the key goes in a header,
  never in an address, a log or a result. A jpeg, png, gif, webp, bmp or tiff goes as it is; a HEIC, an AVIF or a picture
  past 7 MB goes as a JPEG of at most 2048 pixels a side that ffmpeg makes, the picture itself untouched.
- The result says all of it came off the web, a record and never an instruction, that the best guess is Google's guess,
  and to open a page that shows the picture before saying who made it; a search that finds nothing says so. Google's
  refusal (a wrong key, the API off in the key's project, no billing) comes back in its own words with the key taken out;
  a busy answer is tried three times.
- Each call Google answers is one spend row (tool `reverse_image_search`, engine `google-vision`) at the list price,
  US$0.0035 a picture past the first 1,000 a month, which are free; the morning diff carries the day's total. A refusal
  books nothing.
- With no key the tool says, in one line, the file that turns it on (`secrets/google-vision.key`) and sends nothing.
  `yerk abilities` lists `reverse-image-search` as needing Google Cloud Vision until that file is there.
- The card's row reads "finding IMG_0001 on the web", never the call.
- abilities.toml has the built `reverse-image-search` entry; the README and the GUIDE (a section, Search by picture) say
  it; the GUIDE's ability count is corrected to what the file holds, 93: 36 built (it said 90: 33, two built entries
  behind); docs/tools.md and docs/budgets.md are what `yerk docs own` writes.
- budgets.toml: `yerk_tools` 16,000 to 16,900 (the tool is 826 as built on Python 3.13, about 857 as sent) and the
  session total by the 900 to 115,102; [code] lines 23,977 + 171 = 24,148; each with its dated note.
- Why: on 2026-10-26 a room was asked who painted a painting in a phone photo, with no signature on it. It read the photo
  and ran six word searches on its style and subject, checked the closest painter it found and ruled them out, and found
  no match: no tool searched the web by picture, so it filed the need.

**Upgrading**: to turn it on, make an API key in a Google Cloud project with the Cloud Vision API turned on and billing set
up (restrict the key to that API), and put it on the first line of `secrets/google-vision.key` in the private folder,
0600; the next call reads it, no restart. ffmpeg, which transcribe and watch already need, makes the JPEG for a HEIC, an
AVIF or a picture past 7 MB. A room has the tool from its next session start after the deploy. budgets.toml's
`yerk_tools`, `total` and [code] `lines` move with this change.

### need-2026-10-26-21 (#164)

- A room asked to open a page in the browser on the owner's PC opens it: the browser plug-in, kept outside this
  repository (hands, attached to every owner room under rooms.toml's `[defaults]`), starts that browser again before
  each call on the PC, inside the call's lease, since 2026-10-26. A browser that will not come back is said to be down in
  one line with its reason, and an error from the browser's driver names its exception's own line, never a stack trace's
  first line. The plug-in's own offline tests hold both (28 of 28 green on its main); its call log has no failed browser
  call since the change.
- Here, the manifest says so: the browser entry's `how` gains the relaunch before each call and the computer the owner
  names (the default one when none is named; another computer's browser is another profile with its own sign-ins, so a
  read moves there only when the owner names it), and its `why_not` no longer waits on the rooms' move: a browser reaches
  the rooms as a plug-in an install attaches, a room that holds such a tool opens the page with it, and when the tool says
  that browser is down the room tells the owner, never reading on another computer unasked. Three new phrases ("open it
  in my browser", "open a page in my browser", "browser on my pc") hand a room asked that way the entry whole
  (`yerk explain browser`).
- Nothing in the harness's code changes, so no budget line moves.
- Why: on 2026-10-26 a room asked to open a page in the browser on the owner's PC met "opening the tab failed"
  with only a stack trace's first line after it: the browser had quit with its last tab, and its port took each call and
  reset it. The same open in the Mac's browser, another profile, landed on a sign-in page.

**Upgrading**: nothing to change by hand; the plug-in's change has been live since 2026-10-26, and a room hears the new
entry from the deploy.

### need-2026-10-25-23 (#163)

- A room the owner orders to delete files or folders outside its work folder can do it now: the new `delete` tool on
  Yerk's own server takes their whole paths, and the daemon deletes them, since a room's shell deletes only inside its work
  folder and Claude Code's sandbox keeps the rest of the disk read-only to it. For good, not to the trash: what the owner
  orders deleted out there is to free the disk, and the trash frees nothing. A file or a link is unlinked (never the file a
  link points to), a folder is removed whole without following a link inside it, and the result says what went, what would
  not and why, and the space freed.
- Shown first: the new `delete-outside` gate rule holds every call, and the room is shown each path whole with what it is
  (a file and its size on disk, a folder, a link); the owner's approval word runs that exact call once, and an edit is a new
  call and a new hold. The tool deletes only what the gate released, so with `delete-outside` in `[gates] off`, or a hook
  that fails open, nothing goes.
- Never deleted, approval or not, and refused before the owner is asked: a call from a person's room, a path that is not
  whole, not there or not its own real path (a link or a `..` on its way, so the path shown is the path deleted), the
  root, a top folder, the home folder, a mounted disk or a folder holding one, and Yerk AI's own folders (its code, its
  state and private folders, the memory, an account's folder) or one holding them.
- A room's own `rm` outside its work folder is still refused, and in an owner room the refusal now names the `delete` tool;
  a worker and a person's room keep the old refusal. Each delete's outcome is a `delete` row in the ledger's gates table.
- Sizes of a gigabyte or more read in GB everywhere words.size says one (`yerk retention`, the doctor's growth line).
- `yerk explain delete-outside` has the ability. Every room is handed the tool's sentence, so `yerk_tools` goes to 16,000
  and the session-start total to 114,202; the code ceiling moves up by 166, to 23,977.
- Why: on 2026-10-25 a room the owner asked to delete old junk outside its work folder to free a full disk had its `rm`
  refused by the trash gate, found the rest of the disk read-only in its sandbox, and wrote the delete lines into a script
  for the owner to paste and run (need 23 of that day).

**Upgrading**: nothing to change by hand; every owner room has the tool from its next process start. budgets.toml moves
with the code: `yerk_tools` to 16,000, the session-start total to 114,202, the code ceiling to 23,977.

### need-2026-10-25-22 (#162)

- A room can choose the words a topic rule loads on: `remember` with a `job` takes `triggers`, words or short phrases a
  message on the job holds (the owner's, when named). A rule the shared rules file has no room for then loads on its
  job's words and those, in place of what its own text names (a name, a capitalised word mid-sentence), kept on its line
  in rules-topics.md as an `[on: ...]` mark before its trailer.
- A picked word rides alone at the weight of a word only that rule says, so an everyday word the extractor never takes
  ("money") or one every rule says still loads it; a phrase rides when a message says each of its words that names
  something, in any order. A trigger whose words all name nothing ("how do I") is refused, and nothing is filed.
- A filing of the job that names no `triggers` keeps the ones its live topic rule has, so a correction to the rule's
  words never drops the owner's choice; the same call again is a retried one and files nothing.
- `replaced` names every line of its job a filing marked, in the rules file or the topic rules. A topic filing marked its
  job's older topic line and said `replaced: []`, and so did a filing into the rules file that marked its job's topic rule.
- The reply's clause and the machine room's notice name the words picked. `yerk explain topic-triggers` has the ability.
  `remember` grows by about 279 characters as sent, so `yerk_tools` goes to 15,400 and the session-start total to 113,602;
  the code ceiling moves up by 25, to 23,811.
- Why: on 2026-10-25 an owner room asked to make a topic rule load on an everyday word and phrases holding it filed the
  rule again with those words in its text; its triggers came from its job's words and the names its text held, never the
  word asked for, and the filing said `replaced: []` though it had marked the older line, so the room could not tell
  whether two filings of the job sat side by side.

**Upgrading**: nothing to change by hand; every owner room files with `triggers` from its next process start after the
deploy, and a topic rule filed before it keeps the words its text gives it until it is filed again with `triggers`.
budgets.toml moves with the code: `yerk_tools` to 15,400, the session-start total to 113,602, the code ceiling to 23,811.

### need-2026-10-25-21 (#161)

- An owner room can check a worker's claim about Yerk against the code now: every owner room's settings carry a Read
  rule for Yerk's own checkout, the code that runs (the folder the package runs from), so the room's Read, Grep and
  Glob reach every file there. The daemon writes it at each process start: the checkout whole, or, when the checkout
  holds the private or the state folder (a clone on its example layer), each of its other entries by a rule of its own;
  the read denies on every secret still beat it.
- A rule, never an `--add-dir`: Claude Code's sandbox opens an added folder to the room's shell for writing, and no room
  writes the harness. A room whose privacy is not owner reads its own folders only, as only an owner room takes a
  `read` folder.
- The mechanics gain one line naming the checkout's path: check a worker's claim about Yerk, or how Yerk does a thing,
  there before saying it, since a report can be wrong. mechanics.md holds a `{code}` mark there, and the session start
  and the news of a changed line fill in the path, so every room is handed the path and never the mark.
- `yerk explain harness-code` has the ability. The mechanics go to 6,000 (5,818 in the file, the checkout's path on top
  as sent) and the session-start total to 113,302; the code ceiling moves up by 10, to 23,786.
- Why: on 2026-10-25 an owner room asked to tell a worker's code-review outcome Grepped the checkout for the worker's
  finding to confirm it first; Claude Code refused the read, since the room's settings opened its own folder only, and
  the room relayed the finding unchecked, marked as the worker's read.

**Upgrading**: nothing to change by hand; every owner room reads the checkout from its next process start after the
deploy. budgets.toml moves with the code: `mechanics` to 6,000, the session-start total to 113,302, the code ceiling to
23,786.

### need-2026-10-24-21 (#159)

- A room asked to read a Microsoft Bookings page (Bookings with me) now reads it as a guest, with no sign-in:
  `web_fetch` adds the `anonymous` flag every share link Microsoft makes carries (`.../bookwithme/user/<id>@<domain>?anonymous&ep=pcard`)
  when a link lacks it, first in the query as Microsoft writes it, on Outlook's hosts and `bookings.cloud.microsoft`, so
  no rung lands on Microsoft's sign-in page. The flagged page opens on "Sign in or continue as guest", and its meeting
  types and open times show only after a click on Continue as guest: the machine's own browser (F2) makes that click on
  a Bookings page only, waits for the guest view (the click's own requests done, the text steady) and takes its few
  hundred chars as the answer; the answer says it read the page as a guest. A plain read gets only the page's app shell,
  for either link. A link that has the flag, and every other page, is read as before. Read live on 2026-11-02 with no
  key: `F1:thin:js_shell,F2:ok:short`, the meeting type and the day's open times, in 4.7 s.
- A read that is still that choice screen is no answer at any rung (`guest_choice`, `retry=maybe`): L3's "a short but
  real page is still an answer" took it for one. Once F2 has clicked, or met the screen, the climb stops before
  Browserbase, whose rungs make no click, so no paid read is spent on it.
- A Bookings page whose guest view goes on to Microsoft's sign-in is named so (what was seen, not a guess at the owner's
  setting), with `retry=no` and the next move (ask the page's owner for a public meeting type's link or the times in
  words), in place of the saved-login advice the login wall gives every other site.
- `yerk explain bookings-guest` has the ability. Nothing in the harness's code changes, so no budget line moves.
- Why: on 2026-10-24 a room asked to read a person's Bookings page for their open call slots met `login_required` at
  every rung, because the link it had carried no `anonymous` flag and the page sent every reader to Microsoft's sign-in;
  it answered from a read two days old. The browser crash it met the same evening (the PC browser's open call failing
  with a traceback) was fixed in the hands plug-in on 2026-10-26, outside this repository.

**Upgrading**: nothing to change by hand; every room that attaches the web plug-in reads Bookings pages this way from
its next call after the deploy.

## 2026-11-01

### need-2026-10-24-1 (#158)

- A room the owner asks to text someone can send it now: the new `text` tool on Yerk's own server sends one text in the
  owner's name, an iMessage or else an SMS, from the owner's Mac to the person's phone number or email address (a name is
  refused, so the room recalls the number from the owner's texts or asks). The Mac's bridge, a small app there that holds
  Messages' grants and serves imsg's JSON-RPC on a socket, is reached by ssh to `[imessage] host` and socat to its
  `socket`, or by `[imessage] command`; each text is a `status`, then one `send`, on a connection of its own.
- Preview first: the new `text-out` gate rule holds every call, and the room is shown the text as it will read and who
  it goes to exactly as it is sent (a number by its digits, an address whole, never cut to a name), never the call's
  JSON; the owner's approval word sends that exact call once, and an edit is a new call with a new preview. The tool
  sends only what the gate released, so with `text-out` in `[gates] off`, or a hook that fails open, no text goes. A
  person's room, an install with no Mac set up, a name in place of a number and an empty text are refused at the gate,
  before the owner is asked for a word; a worker is refused, as for any outward action.
- A text the bridge never took is said as not sent; one it took and never confirmed (no answer to the send, or its own
  may-have-completed answer) is said as maybe sent, and the room tells the owner to check Messages and never sends it again
  unasked. Each outcome is a `text` row in the ledger's gates table, after the hold, the word and the release.
- `yerk explain text` has the ability, and `text-as-you` names it as its built part. Every room is handed the tool's
  sentence, so `yerk_tools` goes to 15,100 and the session-start total to 113,002; the code ceiling moves up by 167, to
  23,776.
- Why: on 2026-10-24 a room asked to text a person for the owner had no tool to send one, the Mac's Messages would not
  answer over plain ssh, and the room wrote the line for the owner to paste and send (need 1 of that day).

**Upgrading**: to text, add `[imessage]` to yerkd.toml with `host` (the Mac's ssh host, reached from the box with a key)
and `socket` (the bridge's socket on the Mac), and `socat` when it is not at /opt/homebrew/bin/socat; or `command`, a whole
command whose stdin and stdout are the bridge. Without it the tool says no Mac is set up, and nothing is held. budgets.toml
moves with the code: `yerk_tools` to 15,100, the session-start total to 113,002, the code ceiling to 23,776.

### needs-pipeline (#155)

- A need a room files now builds itself to live with no person in the loop ([needs] auto, on unless set false; false is
  the hand road exactly). The first need starts its builder at once; one filed while another is in flight is queued and
  starts when that one is live, held or stuck, since two open PRs that both move the code ceiling conflict. The daemon
  watches the PR the box's sync opens and the checks the box's CI posts: red checks start a fix worker that adds a new
  commit (at most [needs] fix_cap, default 2, then held for a person), checks that never all come within
  [needs] checks_minutes (default 40) make it stuck, and green checks start a fresh cold read worker that says `merge`,
  `fix first` or `unsure`. On `merge` the daemon merges the PR at the head the read saw (the sha guard), starts
  `yerk deploy origin/main` in a transient unit `yerk-deploy-<id>` after a fetch through the package's askpass (the token
  never reaches argv), and once a proven deploy holds the merge says live in the machine room and once in each room that
  filed it. Every step is one line in the machine room; `yerk needs list` words each state (queued, fixing, cold read,
  held, stuck, deploying, live). `yerk needs build DAY N` by hand still starts a held or stuck need. A need filed before
  the pipeline, or while it was off, that no build ever started is queued on the watch's next tick and starts in its
  turn; one whose build came back to filed stays for a person, as a held one does. The code ceiling moves up by 509, to
  23,609.
- Why: on 2026-10-31 need 27 took four hand steps from its PR to live, and a red CI leg had to be noticed by a person.

**Upgrading**: put a GitHub token for the repo at `secrets/github.token` in the private folder (one line, 0600) for the
merge and the deploy's fetch; without it the builds and PRs still happen and the merge and the deploy wait for a person.
`[needs] auto` is on unless set false in yerkd.toml. Set `[needs] pr_check` to the install's PR checker if it has one.
budgets.toml moves with the code: the code ceiling to 23,609.

### dst-night-tests (#156)

- Tests only, nothing in the code: the suite passes on a daylight-saving night. Two tests built the morning diff for
  "tomorrow" as `time.time() + 86400`, and from 00:00 to 01:00 on a fall-back night that is 23:xx of the same local
  day, so the diff's window (the local day before it) was the day before and missed the refusals the test had just
  written. box-ci ran main fdc0db9 from 00:03 EDT on 2026-11-01 and went red on all three Pythons (2 failed); the
  same sha passed by hand later that night. Both tests now build the diff at noon of the next local day,
  `status.midnight(time.time()) + 86400 + 43200`: tests/test_said_or_noted.py (the morning diff counts each refusal)
  and tests/test_sheet_hard.py (the morning diff counts the sheet refusals). The diff's local-day window is unchanged,
  and so are budgets.toml and the code ceiling (23,100).

### need-2026-10-31-27 (#154)

- A YouTube link's transcript is free and comes in seconds when the video has captions: `watch` reads the video's own
  captions, uploaded or auto-generated, with their timestamps, before it would pay to transcribe the sound. The paid
  engine and its US$1 gate run only for a video with no captions, and the result's note says which way the words came:
  the video's own captions, its auto-generated ones (no punctuation, and a name may be misheard), or no captions, so its
  sound was transcribed. The result gains `source` (captions, auto-captions, heard, or empty with no transcript) and the
  page's `title`.
- The daemon asks yt-dlp for the page's facts first (`-J`, about 2 s, nothing downloaded), picks one track (the
  video's own language, an uploaded track over an auto one, the untranslated auto track over its plain one, never live
  chat or a translation) and fetches exactly that one key, never a pattern or a list, since those reach YouTube's
  auto-translated tracks and it answers HTTP 429. The transcript file has the same shape as a paid one, a head line and
  `[HH:MM:SS -> HH:MM:SS]` lines, so a second call reads it and pays nothing; the caption file and the page's facts are
  deleted once read.
- A video too big to save (past the 300 MB cap) still gives its captions: the call comes back with the transcript, no
  video and no frames, and the note says why there is no video. A failed or live page, or a track that saves nothing,
  goes the paid way as before. The mechanics' watch line says it, the tool's description gains a sentence, and the code
  ceiling moves up by 207.
- Why: on 2026-10-31 a room was quoted US$1.43 to transcribe a 140-minute YouTube podcast whose
  auto-generated captions YouTube already had; the captions were already the transcript.

**Upgrading**: nothing to change by hand; yt-dlp already on yerkd's PATH reads the captions from the next start, and each
room has the mechanics line from its next process start. budgets.toml moves with the code: Yerk AI's tools to 14,400,
the mechanics to 5,700, the session-start total to 112,302, the code ceiling to 23,100.

### worker-no-secrets (#153)

- A worker can no longer read the install's API keys: the private layer's secrets/ (every provider key and every
  account's token) is not there for a worker's unit, as the install's secret and every room's folder already were not,
  so no worker can spend money outside the daemon's tools, their gates and the ledger. A worker whose account token
  lives there (`secrets/claude-<name>.token`, the default) gets that one file back, read-only, for its first step, and
  no other. The unit carries `TemporaryFileSystem=<private>/secrets:ro` and `BindReadOnlyPaths=-<its token>`, or
  `InaccessiblePaths=-<private>/secrets` when its token lives elsewhere or it has none; the "-" lets an install with no
  secrets/ start its workers as before. The code ceiling moves up by 13, to 22,893.
- Why: on 2026-10-31 a room briefed a worker to transcribe a long video after the daemon's transcribe tool had refused
  at its US$1 gate. The worker wrote a short script that imported the package's transcribe module, read three provider
  keys from secrets/ and made thirty paid calls itself, about US$1.20; every spend row it tried to write failed on the
  read-only state folder, so the money is in no ledger, and the run was stopped before it wrote anything. The gates see
  tool calls and shell commands, not a Python import; the sandbox is what stops it.

**Upgrading**: nothing to change by hand; the next worker start carries it. budgets.toml moves with the code: the code
ceiling to 22,893.

### retention-keep (#152)

- A room keeps a folder of its work from the nightly sweep with a file named KEEP: a top folder of work/ holding a
  regular file named `KEEP` is never scratch, so nothing under it moves, by age or by the 500 MB cap, and its size is
  not counted toward the cap. Only the top of work/ is read for it, as for inbox/; a KEEP file deeper down, or a KEEP
  that is a link, keeps nothing. The file's words are for a person (why the folder is kept); only its being there
  counts. `yerk retention` says the size beside the inbox's: `inbox and KEEP folders: 1.2 MB, kept`.
- The morning diff's sweep line names what left which room: after the kinds, each room the sweep took scratch from,
  the biggest first, with its top folders and their file counts, six folders at most and then "and N more", as in
  `- swept: scratch 11 MB; scratch: example-room offer-2026-10-28 (11 files), videos (3)`. A sweep kept before this
  change reads as it did.
- A room the sweep took from finds RETENTION.md at the top of its own work folder: when the sweep ran, each path it
  lost as the room sees it from work/ (a sent copy as ../outbox/...) and where that waits under the state folder's
  trash/scratch/, how long the trash keeps it (`scratch_trash`), and that a folder under work/ with a file named KEEP
  is never swept. It is rewritten whole at each sweep that takes something from that room, never on a dry run or when
  nothing left, and is never swept itself.
- Every room's mechanics gain one line saying both: a folder under work/ with a file named KEEP is never swept, and
  RETENTION.md at the top of work/ says what the sweep took. The code ceiling moves up by 42, to 22,880.
- Why: on 2026-10-31 the sweep took eleven drafts an owner room had written three days before and cited in its
  facts, past the shipped two days of scratch; the morning line said `scratch 11 MB` and nothing told the room, so
  later that day it told the owner the drafts were held. They were copied back from the trash by hand. The shipped days stay
  as they were (`scratch` 2, `scratch_trash` 1).

**Upgrading**: nothing to change by hand; the sweep reads KEEP and writes RETENTION.md from its next run, and each room
has the mechanics line from its next process start. budgets.toml moves with the code: the mechanics to 5,500, the
session-start total to 111,802, the code ceiling to 22,880. units/yerk-retention.service changes its comment only.

## 2026-10-30

### index-one-thread (#151)

- Meaning search answers in about 1 s instead of 4 to 5 s on a store of a million vectors, so recall stops falling back
  to keywords only several times a day (`meaning search skipped (index-timeout)` in the daemon's journal, 4 to 8 times a
  day since 10-24), and `yerk deploy` stops refusing on the recall bench's controls only the meaning half finds (two
  of them went blind on 10-25 and on 10-30).
- yerk-index sets OPENBLAS_NUM_THREADS, OMP_NUM_THREADS and MKL_NUM_THREADS to 1 as it starts, before numpy loads,
  unless the unit or an operator set them, and the shipped unit sets them too. Only yerk-index: yerkd imports the
  index's module and keeps its own environment, so the rooms it starts and the commands they run keep every core. The
  code ceiling moves up by 5.
- Why: a search embeds the query (0.3 to 0.5 s), then scans every stored vector, 62 blocks of 16,384 at 1,015,673
  vectors, a small matrix-vector product each, and numpy's OpenBLAS spread each one over all 12 cores; at the unit's
  `Nice=10` and under this box's 300% CPU quota the threads spun, and on 2026-10-30 every search took 3.8 to 4.9 s
  against recall's 5 s wait. The same scan at the store's shape takes 1.4 to 1.8 s on the default threads and 0.6 s on
  one; with the three set in a drop-in the live index answered in 0.85 to 1.15 s and the bench's controls hit 13 of 13.
- pypdf, the PDF reader behind the attachment sidecars, moves from 6.21.2 to 6.21.4 in uv.lock: `box-ci/audit` named
  seven advisories against 6.21.2 (CVE-2026-119203 to CVE-2026-119209), four fixed in 6.21.3 and three in 6.21.4.

**Upgrading**: yerk-index sets them itself from its next start. An existing install also adds the three lines
(`Environment=OPENBLAS_NUM_THREADS=1`, `Environment=OMP_NUM_THREADS=1`, `Environment=MKL_NUM_THREADS=1`) to its copy
of `yerk-index.service`, or puts them in a drop-in (`systemctl --user edit yerk-index`), then runs
`systemctl --user daemon-reload` and restarts yerk-index. budgets.toml moves with the code: the code ceiling to 22,838.

### need-2026-10-30-26 (#150)

- A file another assistant attaches where a room reads is saved in the room's `work/inbox/<message id>/` as a person's
  is, so the room opens it when asked, where before the day log had its name and size and the room had no copy. That is
  an assistant `[watch] assistants` lists in yerkd.toml, in the channel the room watches or in the room's own: the
  answer a held message waits for has its files saved before that turn, and the line under the answer names where each
  was saved (an owner room's PDF or workbook with its text beside it, as a person's has) or that it could not be
  fetched; any other message of theirs has its files saved as it is filed, in the room's own channel as it lands.
- Within one run of the daemon a message's files are fetched once, however often it is judged and filed, a ledger row a
  file, `staged` or `missing`; after a restart a file already on disk at its size is not fetched again, though its row is
  written once more. A file that will not
  come is never the "could not fetch, send it again" notice a person's gets, since no one in the room sent it, and none
  is a file the room must open before it may answer. A save that fails for any other reason is logged, and the turn
  still gets the file's name and why. Retention keeps them as it keeps a person's files. The code ceiling
  moves up by 35.
- Why: on 2026-10-30 a watching room asked to open a PDF the other assistant had attached in its answer minutes before
  found its name and size in the day log and no saved copy, and its shell cannot reach the chat service, so it could
  only ask for the file to be posted again.

**Upgrading**: nothing to change by hand; files the other assistant attaches from the next start on are saved. At the
next start, catch-up also saves the other assistant's files posted after each channel's last ledgered message, which
includes ones from before this deploy. budgets.toml moves with the code: the code
ceiling to 22,833.

### deliver-transport-error (#149)

- A reply whose post dies on the connection (Discord's answer never arrives, as when a TLS read fails) is sent once more
  inside the turn under the same nonce, so the room gets the answer, with no crash footer and no orange card left
  standing. Discord answers a nonce it already took with the message it made, so the retry never posts it twice.
- Only a reply Discord still did not take after that retry gets a footer, and the footer now says `Discord did not take
  the reply` where it said `the harness failed`. Nothing of the reply is posted under its `not answered` line; of a long
  reply, the parts already in the room stay and none after the failed one is posted. A reply Discord refuses with its
  own error is not retried and keeps the old footer. Either way the turn's working card is removed.
- A room's `send` whose post is lost twice answers the room with ok false and the reason, as a post lost once did.
- A part Discord did not take twice is `failed` in the ledger, where before it stayed `pending` and the next start's
  redelivery posted it labelled; a failed part is skipped by every later send under its key, a reply's or any other
  keyed post (a job's late line, curate's page), which then gets no message id for it.
- Why: on 2026-10-30 a reply in one of the owner's rooms died in the TLS read of Discord's answer; the turn
  crashed with its working card up, and the crash footer posted the reply under `not answered`.

**Upgrading**: nothing to change by hand. budgets.toml moves with the code: the code ceiling to 22,798.
### changelog-numbers (#148)

- This file only, nothing in the code: six entries that said `(#?)` now carry their pull request numbers (#139 and
  #143 to #147), and the uncapped (#136) entry names the fixture's base and the `[code] lines` count as they stood at
  its merge (1ec2592; 22,736, main's 22,613 plus its 123), the way the docs-from-code and latency-bench entries were
  brought up to date when they merged.

## 2026-10-29

### note-to-self (#147)

- A room whose final message opens with its own plan for the reply or a note to itself, before the answer, is turned
  back once at Stop and answers again with the answer alone. A note is a first paragraph of at most 700 characters that
  holds a rulebook tag (`Landed:` or `Bounced:`), or that has a paragraph after it and tells the room how to reply (a
  directive such as "Confirm" or "Keep" beside a shape word such as "in a line", "style" or "nuance") with no you or
  your, since advice to the owner on his own message says those. An empty final, a final of exactly NO_REPLY and a turn
  already turned back once stand.
- The mechanics gain one line saying where a note goes: every word of the final message is posted as the answer, so a
  read of the sender, a plan for the reply or a note to itself goes in thinking.
- The morning diff counts each such bounce as `note`, a shadow ledger row with the note's length and the signal that
  fired, never its text. The code ceiling moves up with the rule.
- Why: on 2026-10-29 an owner's room answered on a turn with no thinking, wrote its plan for the reply above
  the answer, and the room posted both, so the owner read the plan as the reply.

**Upgrading**: nothing to change by hand; each room has the mechanics line from its next process start. budgets.toml
moves with the code: the mechanics to 5,300, the session-start total to 111,602.

### hands-budget (#146)

- A deploy no longer refuses on the size of the hands plug-in's tools: their budget line moves to what a busy
  room sends since need 27 (7,462, from 7,119) and the session-start total moves with it. Nothing else changes.

### watch-audit (#145)

- A watching room with `watch_audit = true` grades the other assistant's answer to every message sent there: it posts
  a verdict in its own channel, first line `🟢 looks good`, `🟠 right but missing something` or `🔴 wrong`, then what is
  wrong or missing and the fix, written out in full in plain words, and one or two lines when the answer is right. It
  never goes quiet on one: no more `🟢 read it, nothing to add` there.
- The room reads each such message, held for the other assistant's answer as before, under a line asking for that
  verdict, checked against the files and the thread, not recall; the line names who sent the message, by the name the
  server shows, and who reads the room: the owner, and in a room with `people` each of them by name (fetched once from
  the server). If the other assistant had not answered within `watch_wait`, the room says so in one line.
- Should the room still answer NO_REPLY on one, nothing is posted and its card stays as `🔴 no verdict: the room answered
  nothing`, kept as history: a later quiet line never removes it, and it never removes the quiet line before it. A
  message in the room's own channel, a worker's relay and every room without the key are unchanged. The code ceiling
  moves up by 50.

**Upgrading**: the key is opt-in; `watch_audit = true` in a room's rooms.toml block turns it on (docs/settings.md names
it), and a value that is not true or false stops the start with one line naming the room. budgets.toml moves with the
code.
### hold-card (#144)

- A room that watches an OpenClaw bot's channel no longer answers off that bot's progress card. A message with one of
  OpenClaw's tool rows (a line starting "🛠️ Bash", "🧩 Mcp ...", or any other tool's emoji) or the shape of Yerk AI's
  own card is never the answer a watched message waits for, however long it sits still while a slow step runs, and it
  never goes in the room's day log. Before, a card that sat 6 seconds through a web fetch was read as the answer, the
  room said it had nothing to add, and the card was filed as the bot's line once it sat 30 seconds.
- While that card is being worked on, the room keeps waiting: `watch_wait` counts again from the card's last edit, up
  to three times `watch_wait` after the message (a stuck card cannot hold it forever), so the reply a long turn ends
  with comes in with the message as its answer. When the card is deleted and no reply follows (the bot chose not to
  answer), the message comes in within 30 seconds with the line that it had not answered. A card edited into a plain
  reply is the answer once that reply has gone 6 seconds unedited, and is filed once it has gone 30, as any reply.
- After a restart, a card in the channel's history is skipped the same way: never the answer, never filed, and the
  wait counts from its last edit. The code ceiling moves up by 57.

**Upgrading**: nothing to change; `watch_wait` keeps its key and default, and docs/settings.md says the new rule.
budgets.toml moves with the code.

## 2026-10-28

### need-2026-10-28-26 (#143)

- Send a room a reel, a TikTok, an X video or a YouTube link and it watches the video, where before it answered off the
  caption, the cover picture and the comments. The new `watch` tool on Yerk's own server pulls the video from the link
  into the room's work folder (`videos/<site>-<the post's id>/`), takes still frames spread evenly through it (6, or 1
  to 12 when asked) that the room opens and looks at, and transcribes its sound, so the answer is about what is on
  screen and what is said. The turn card reads `watching a video on <the site>`, never the link.
- The daemon runs yt-dlp for the room, since a room's shell has no network; the sandbox and the wall between rooms are
  unchanged. A call pulls the best picture at 720 lines or under with its sound, skips a video over 300 MB, runs one
  download at a time across rooms, and stops one past 300 seconds with everything it started, and the room says so. A
  page that needs a login, a private post or a page with no video is said in the site's own words; no cookies and no
  logins are used.
- The transcript goes through the transcribe engine on the same keys and the same US$1 gate, each paid call a spend row
  under `watch`; a video with no sound has frames only, and a transcript that fails still leaves the frames, with the
  reason. A post already pulled is never pulled or paid for again: a second call reads its saved transcript and only
  makes the frames again. The mechanics gain one line (a video in a post is watched with `watch`; an answer off the
  caption says so), `yerk explain video-read` and `yerk explain youtube-transcript` name `watch` as their built part,
  and the code ceiling moves up by 260.

**Upgrading**: install yt-dlp so `yt-dlp` is on yerkd's PATH, beside the ffmpeg and ffprobe transcribe already uses;
nothing changes in rooms.toml or yerkd.toml, and each room allows `mcp__yerk__watch` from its next process start.
budgets.toml moves with the code: Yerk AI's tools to 14,100, the mechanics to 5,000, the session-start total to 110,959.

### need-2026-10-28-24 (#141)

- A room can hand back a PDF of a document and say how many pages it is, so "does it fit on one page?" gets a measured
  answer. The new `render` tool on Yerk's own server takes a document in the room's own folders (a .docx, .doc, .odt,
  .rtf, .xlsx, .pptx or anything LibreOffice opens), saves its PDF beside it as `NAME.pdf` (an older one there is
  replaced) and returns the page count; the room `send`s that PDF when you want it. A .pdf is counted as it is, nothing
  rendered.
- The daemon runs LibreOffice for the room, since LibreOffice keeps its pipe in /tmp and a room's sandbox has no writable
  /tmp; the sandbox and the wall between rooms are unchanged. Each call runs `soffice --headless --convert-to pdf` under a
  fresh profile of its own in the state folder's `render/`, removed after; one conversion runs at a time across rooms, a
  second call waiting its turn; one past 120 seconds is stopped with everything it started, and the room says so. The
  fonts in your own `~/.local/share/fonts` are used, so a document set in Arial lays out in its same-width twin where one
  is installed, not in a wider stand-in that pushes it onto another page.
- The layout is LibreOffice's, not Word's: a document that fits by a line in one can spill in the other, and the room says
  a result near the edge as near the edge. Without LibreOffice on the machine the room says no PDF could be made. It is
  free: no spend row. The mechanics gain one line (a page count comes from `render`, never an estimate off the text), and
  the code ceiling moves up by 126.

**Upgrading**: for PDFs, install LibreOffice so `soffice` is on yerkd's PATH; nothing changes in rooms.toml or
yerkd.toml, and each room allows `mcp__yerk__render` from its next process start. budgets.toml moves with the code: Yerk
AI's tools to 12,800, the mechanics to 4,700, the session-start total to 109,359.

### quiet-card (#140)

- A room that watches another channel keeps one `🟢 read it, nothing to add` line, not one per message. When the room
  reads a message there and has nothing to add, its new quiet line stands and the quiet line before it is deleted, as
  long as the room posted nothing else in between: a reply, a notice, a file, a voice note, a worker's card or a card
  kept to say a turn failed leaves the older line where it is, as history. A conversation in the watched channel now
  leaves one green line in the room, the newest, where it left one per message before.
- The line before is found in the ledger, so the rule holds across a restart. One already deleted by hand changes
  nothing; one Discord will not delete stays and is logged. The code ceiling moves up by 25.

**Upgrading**: nothing to change. budgets.toml moves with the code.

### watch-answer (#139)

- A message the owner sends in a channel a room watches now waits for the other assistant's answer there before the
  room reads it, so the room no longer says it has nothing to add while the other assistant is still typing. The
  answer is the first message after his from an id in `[watch] assistants` that goes 6 seconds with no edit and is not
  deleted, so the other assistant's progress card, edited again and again and then deleted, is never it. The room then
  reads both in one turn: the line saying who his message was sent to, his message, and `[<assistant> answered at
  HH:MM in #<channel>: <text>]`, the text's whitespace folded and cut at 2,500 characters, the time in the room's zone,
  with a `[<assistant> attached: "<name>"]` line per file it sent (named, not fetched).
- The room waits up to `watch_wait` seconds after his last message (a new rooms.toml key, default 150); past it the
  turn comes with `[<assistant> had not answered in #<channel> within 150 s of his message]`. Messages he sends there
  while one waits come in the same turn, each as he last edited it; one he deletes while it waits never comes. The
  room's own channel never waits behind it, and nothing is posted or typed in the watched channel, as before.
- After a restart, a watched message whose answer is already in the channel's history comes in at once with it; one
  without waits what is left of its wait. The answer is still filed in the day log under its own label, as before.
  With no `[watch] assistants` listed there is nothing to wait for, so a watched message comes in at once, as before.
  The code ceiling moves up by 152.

**Upgrading**: nothing to change; `watch_wait` is a new optional key beside `watch_assistant` in rooms.toml
(docs/settings.md names it). budgets.toml moves with the code.

### need-2026-10-28-23 (#138)

- A recording of two or more people comes back saying who talks when. With a Meta key at `secrets/meta.key`, the
  transcribe tool also sends the recording to Meta's Muse Voice Transcribe in DIARIZATION mode, and each line of the
  transcript starts with a speaker letter: `[00:14:00 -> 00:14:03] B: then email the vendor`. The words, their times and
  the uncertain marks are whisper-1's and Groq's as before; Muse only says who. A line under the file's head gives each
  letter's speaking time and tells the room the letters are not names, so it gives who is who as a guess from what is
  said unless the owner says; the tool's result carries that line as `speakers_how` and the seconds as `speakers`.
- Muse takes 10 minutes a request and its letters hold for one request only, so a longer recording goes as 9-minute
  windows starting every 7.5 minutes, and each window's letters are matched to the ones before by the speaking time
  they share in the 90 seconds the two overlap. A letter that cannot be matched (its speaker silent through an overlap)
  gets a new letter, and the speakers line says how many could not be matched.
- One voice writes no letters, so a voice note's file reads as before. Without the key, or when a Muse window fails,
  the words come back as before under the line `speakers: none (Muse: why)`.
- Muse costs US$0.18 an hour of audio sent, about 1.2 times the recording since the windows overlap: a spend row per
  window in the morning's total, inside the same US$1 gate; a 70-minute meeting is about 72 US cents with all three
  engines. The code ceiling moves up by 131.

**Upgrading**: for speaker letters, put the Meta key on the first line of `secrets/meta.key` in the private folder,
0600; the next call reads it, no restart. Without it every transcript says `speakers: none (Muse: no Meta key at
secrets/meta.key)` under its head and nothing else changes. budgets.toml moves with the code.

## 2026-10-27

### watch-note (#137)

- A message the owner types in a channel a room watches reaches the room under one line saying who it was sent to: the
  assistant there, by the name `watch_assistant` gives it in rooms.toml (else "the other assistant"), in that channel by
  its name, not this room. The room reads along, files what matters, and answers only if his words ask it something or
  it has something he would want that the other assistant did not give; otherwise it answers exactly NO_REPLY. Before,
  it read the message as its own and answered it as if asked. The line comes on every way a message from there reaches
  the room: as it lands, edited, and run again after a restart; a message in the room's own channel never carries it.
- A reply that is exactly NO_REPLY posts nothing. The turn's card stays, saying the room read it and had nothing to
  add; the message is answered in the ledger with the note `silent`, its row kind `silent`. No Stop rule sends it back.
  An empty reply still goes back to the session once, as before, and a reply that only contains NO_REPLY is posted as
  it is. The code ceiling moves up by 32 for both.

**Upgrading**: nothing to change; `watch_assistant` is a new optional key beside `watch` in rooms.toml (docs/settings.md
names it).

## 2026-10-26

### uncapped (#136)

- One switch lifts every character budget, and flipping it back restores them. `[limits] uncapped = true` in the private
  yerkd.toml makes each budget in budgets.toml read as unlimited for what is sent, its number kept as a measurement: the
  session start sends every part whole, its rules part carrying the room's topic rules after the rules file; a rule
  filing never strips an example or files a rule away for size; a message's rules, recall and taste parts go whole
  (taste rows untrimmed, the counts the same) and carry only a topic rule filed after the session started, which a
  compaction re-adds with the whole recall pack; a worker's relay hands the room the whole report and asks no length;
  a workbook's or a PDF's text is kept whole, however long the read. The one cut left is Claude Code's: a hook text
  past its 10,000 characters is cut at a line, its last line naming the file in the room's work folder that holds it
  whole. `yerk map` says `uncapped` where it said OVER and exits 0; the fit tests and the deploy's fast gate report a
  part past its line, never fail on it; `yerk doctor` no longer calls such a part cut. False, or no table, is main's
  assembly byte for byte, held by a test against a fixture retaken from main at each merge (1ec2592 at this PR's).
  compact_window and idle_compact_at are not touched (the 800,000 window is rooms.toml's). docs/budgets.md gains an
  Uncapped section.
- The same switch lifts the money gates and puts a receipt in their place: `transcribe` and `voice_note` no longer
  wait for the owner's yes past US$1, every paid call still books its spend row, transcribe's result carries `usd` as
  voice_note's does, the session start's mechanics tell the room to say a paid call's cost in the same reply (and ask
  no length of a worker's relay), and `yerk status` and the morning diff carry `spend today US$x.xx · week US$y.yy`
  (the call count too where the phone's 40 columns hold it). The web plug-in's own gates (its US$2 daily research
  cap and xhigh's yes) are read from web.json inside plugins/web and stay. budgets.toml's [session] note no longer
  says the first token slows with size: a cold re-read after the cache's hour is where size costs.
- **Upgrading:** nothing to change; `[limits] uncapped` is a new optional key in yerkd.toml (docs/settings.md), and
  leaving it out keeps the install capped. budgets.toml: `[code] lines` 22,736, main's 22,613 plus this PR's 123 (a
  dated note).

### tools-line (#135)

- Deploys pass again. The `effort` field #129 put on the room's `worker` tool grew Yerk AI's tool entries to 11,871
  characters as sent, past the 11,734 line in budgets.toml, and the deploy of #134 refused on the fast tests. The line
  is 11,900 and the session-start total rose by the same 166; a person running Yerk notices nothing else.

**Upgrading**: budgets.toml moves with the code; nothing to do by hand.

### note-feedback (#134)

- A complaint about a voice note counts as one. A message that called a note too short and said it had not hyped its
  listener up went into taste.jsonl as a thumbs up, because the praise list reads a bare "hype" as praise and the
  complaint list knew length only as "too long" and "shorter". Two complaint shapes added, "not long enough" and
  "didn't hype me up", each run over the owner's 36,747 lines (one hit each, that message) and over both taste ledgers (no
  earlier row changes sign). The mechanics line on `voice_note` no longer says each note costs, which read as a reason
  to keep one short; the dollar gate on a dear note is unchanged.

## 2026-10-25

### worker-effort (#129)

- A worker runs at its room's thinking level. A room already passed its `effort` to its own claude, and a worker it
  started ran at Claude Code's default; a worker started from a room running at max now runs at max. The order: the
  call's `effort` (a new field on the room's `worker` tool, `yerk worker start --effort`), then `[workers] effort` in
  yerkd.toml, then the room's live level: a `/effort` typed in the room counts, the same way the room's own claude reads
  it, and so does a `/model`, which a worker used to miss too. A level outside low, medium, high, xhigh, max is refused
  in one line with the list, before any folder, ledger row or card exists. The code ceiling moves up by 4 for it, one
  of them the key's line in config.YERKD_WORDS (docs/settings.md names it).

**Upgrading**: nothing to change; `[workers] effort` is a new optional key in yerkd.toml.

## 2026-10-24

### docs-from-code (#122)

- `yerk docs own` writes Yerk AI's own docs into docs/ from the code: commands.md (every command and flag of `yerk`,
  `yerkd` and `yerk-index`, from their parsers), tools.md (the twelve tools a room is handed, each description as the
  model reads it, with its arguments), settings.md (every rooms.toml, yerkd.toml and jobs.toml key with its default and
  plain words), budgets.md (budgets.toml with its comments as prose), hooks.md (each Claude Code event and what it does)
  and units.md (each unit and when it runs). The same tree writes the same bytes on Python 3.12, 3.13 and 3.14, and
  nothing of the private layer or the state folder goes in. `--dry-run` names a stale page and writes nothing.
- A test fails the build when docs/ is stale against the code (test_owndocs), so a change to a flag, a tool's words, a
  key, a budget, a hook event or a unit runs `yerk docs own` in the same commit. The scrub test reads docs/ and the
  README as it reads the code, and scans the pages as they are written before they reach docs/.
- And the other way for yerkd.toml: a test reads the package for every section and key of yerkd.toml it reads by name
  (the file as load_toml returns it, followed through names, sections and the functions they are handed to; all 19
  sections and 58 keys today) and fails on one settings.md does not describe, with its file and line. It found three, all
  described now: `[discord] guild`, which `yerk setup-room` needs, and `[backup] keep` and `units` (#128).
- README.md is a ten-minute read: 10,235 bytes from 118,399. Its first section stays as it was; install in five commands
  and what a first room needs after them (the bot's intent, scopes and permissions, where a channel id and a user id come
  from, a three-line yerkd.toml; yerkd.toml.example copied whole had stopped yerkd's start on its `[watch]` placeholder),
  the private folder in one picture, a room's turn in one paragraph, add-ons and where the docs are are new. The Status
  section is docs/STATUS.md, whole, a record as of today; Use through Files are docs/GUIDE.md, whole (two links mended).
  The pointers to README sections in rooms.toml.example, yerkd.toml.example and units/yerkd.service name docs/GUIDE.md.
- The hooks' description is now hooks.EVENTS and says the seven Stop checks the daemon runs (the docstring named three).
- **Upgrading:** nothing to edit. budgets.toml: `[code] lines` 21,776, main's 21,418 plus this PR's 358 (a dated
  note), and three tables gain the comment line that says what they are; no number changed.

### latency-bench (#121)

- `yerk bench latency [--days N]` (default 7): per room and over all of them, the first visible line after a person's
  message, the queue wait and the turn (median, p90, p95), the prompt hook and the daemon's hook handling summed per turn,
  the recall inside the prompt hook, Yerk's own tool round trips, and the cache hit rate. The ledger is opened read only
  and the logs are only read; the table is printed and written to `<state>/bench/latency-<day>.json`, each median beside
  the newest earlier day's file, so a speed lever is kept or cut on a diff. Under the table, what the records cannot
  measure and the one record that would (three lines).
- `yerk status` carries today's median first line and turn on one line ("today: first line 5.6 s, turn 12 s"), and
  `--json` as `speed`; the morning diff carries yesterday's against the day before.
- The dead turns query (`yerk status --json`, the morning diff) is a lookup per message, no longer every turn times every
  sent part: a day of messages on a copy of the Oct 24 ledger took 128.8 s and takes 2.0 ms, and `yerk status --json` on
  that copy 139.7 s, now 0.20 s. The same rows, compared per message on all 1,427.
- **Upgrading:** nothing to edit. budgets.toml: `[code] lines` 21,418 (a dated note). The bench's files go in a new
  folder, `<state>/bench/`.

### quiet-doctor (#120)

- `yerk doctor` is quiet when all is well: each line it prints is a finding with its severity first (red or orange) and
  the fix it wants, mostly one command, and a run with none prints one line, `doctor: clean`, green. The morning diff's
  doctor line says `doctor: clean` too, and takes the colour of the worst finding otherwise.
- The rule-shape counts and the topics said twice are one weekly line, on the Sunday run, or on the first run after a
  count rose (the rise marked, as `(+1)`); the daily run and the morning diff no longer repeat them. A rules line a room
  filed in the last day that misses the shape is still said the next morning.
- `memory index: scope-changed` (a room added or dropped, yerk-index still on the old roots) is said once, then not again
  until the index has run on the new roots, which clears it. The fix it names: `systemctl --user restart yerk-index`.
- `yerk doctor --scrub [--dry-run]`: each secret-shaped line the check finds (the shapes it already had) has its value
  swapped for `[scrubbed]`, its label and every other line kept, so the file keeps its line count; each file is copied
  first into `<private>/backups/scrub-<day>/`, a line per file says which lines, never a value, and a second run finds
  nothing. The finding now says to run it, then rotate the key. The copies keep the values: they are 0600, and the key
  still wants rotating.
- `yerk deploy` installs each timer in units/ that systemd does not know yet, once the deploy is proven: the timer and
  its service copied as shipped (0600) into `~/.config/systemd/user/`, one daemon-reload, then `enable --now`, a card
  line each. A timer the owner installed and turned off stays off. `yerk deploy --timers` does only that, restarting
  nothing. The doctor names a timer in units/ that is not enabled, orange, with that command (not installed) or
  `systemctl --user enable --now NAME.timer` (installed, off).
- **Upgrading:** yerk-curate.timer is installed and turned on by the next `yerk deploy` (or `yerk deploy --timers`)
  on a box that never copied it, so the night's card curation runs from then on, at 05:00; the same deploy installs
  and turns on yerk-backup.timer (#128) the same way, which does #128's copy and enable step. If the doctor said
  `secret-shaped lines`, run `yerk doctor --scrub --dry-run`, then `yerk doctor --scrub`, then rotate each key it named.
  budgets.toml: `[code] lines` 21,238 (a dated note).

### addons-1 (#124)

- `yerk add SOURCE` installs an add-on, an ability that lives in a repo of its own: SOURCE is a folder, a git URL
  (`file://` included), or an ability whose abilities.toml entry names its `repo`. Each step is a line as it is done:
  fetched into the state folder's `addons/NAME`, checked (its entry, and its code against its own `size`: an add-on over
  its own ceiling is refused, nothing written; so is a name that is not one plain word of letters, digits, `-` and `_`,
  a words file, test or unit outside its folder, absolute, through `..` or through a link, and a budget value that is not
  a whole number, each with its reason), its package installed into this venv, its MCP server and its tools'
  allow lines written under rooms.toml's `[defaults]` and its tables into yerkd.toml (each a marked block, read back
  through the loaders that start yerkd before anything is written), its units linked into `~/.config/systemd/user`, its
  budget lines and its words file named, its own fast tests run. A failing test, or any step that fails after the first
  write, undoes every step. `--dry-run` says each step as "would".
- `yerk remove NAME` undoes each step and leaves the private folder byte for byte as it was before the add; the add-on's
  folder goes to the trash, and a package that will not uninstall is said on its own line.
- abilities.toml entries gain `repo`, `version`, `size` and `install`, and `yerk abilities` reads an add-on's state from
  what is installed (the package importable, its server under `[defaults]`, its units linked): installed, missing a part,
  or not installed when a repo is named. A key-tier ability is on when the keys its new `keys` field names are there,
  where three names were checked by hand.
- While an add-on is installed its budget lines count in the gate (`yerk map`, the daemon) and its rooms.toml keys are
  known keys to the doctor. `yerk deploy` runs each installed add-on's fast tests after the core's, and a failure names
  the add-on. `yerk status` has one `add-ons:` line.
- An add-on's `install` may name the attachment types it reads, each to one of its own allowed tools
  (`reads = { "video/*" = "mcp__NAME__frames" }`; a tool not on its allow list is refused): while it is installed, a
  room's `[attachment]` line of that type gets one line under it naming the tool, so a kind the core cannot read, a video,
  is read through the add-on.
- examples/addons/hello is the example: one MCP tool, one unit, one budget line, one words file, one test.
- **Upgrading:** nothing to edit; rooms.toml and yerkd.toml change only when you run `yerk add`. budgets.toml:
  `[code] lines` 21,129 (dated notes: main's 20,754 + 355, then 20 for the cold read's fixes and `reads`).

### bench-kinds (#126)

- `yerk recall-bench` scores three kinds: recall (the question file it always read), and episodic and self
  (`episodic-questions.json` and `self-questions.json` beside it, read when they are there, in the recall file's shape).
  The first line is the kinds together, as before; after `misses:` comes a line per kind. A run's row in
  recall-bench.jsonl carries `kinds`: each kind's questions, top six and top one, with their rates.
- The ratchet holds each kind on its own, so a change that lifts recall and sinks self exits 4 with `RATCHET: self: ...`
  even when the kinds together hold. A row from before this is the recall kind whole, so the series' history still holds
  recall. A dated note in budgets.toml `[recall] drops` names episodic or self before BEST ("2026-10-25: self 21 to 18,
  why"); a note with no kind names recall, as every note so far does.
- The nightly curate holds the lines it lands to the kinds together, and `yerk deploy`'s real-index test holds each kind to
  its ratchet, once the two files are in the private layer. The controls stay in the recall file; `--controls` reads only
  it. The example layer has two invented questions of each new kind.
- **Upgrading:** to score the new kinds, put `episodic-questions.json` and `self-questions.json` in `<private>/evals/`
  beside the recall file; without them the bench runs the recall kind alone, as before. budgets.toml: `[code] lines`
  20,754 (a dated note).

### decision-receipts (#119)

- A reply that makes a call for the owner shows its receipt in the next morning diff (claim 19). Code reads each reply that
  went out, sentence by sentence, for a call's shape (`words.CALL`: "I'd go with" or "I'd" with an imperative verb, "go with"
  or "pick" opening a sentence, "my call:", "you should", "the plan is", a number with an imperative after it); no model
  decides, and a question, a quoted span and a code block are never a call, nor "you should have" or "should've", nor "I'd
  put" or "I'd set" with a past-time word later in the sentence ("I'd" as "I had"). Replayed read only over one day of a work
  room (Oct 23, 96 turns): 7 calls in 6 turns, 6 of them real recommendations; the seventh was "I'd" meaning "I had", so the
  day now reads 6 calls in 5 turns.
- Each call is a `calls` row in the ledger: its room, its turn, its sentence, and the cites its turn read (the recall hits
  the prompt hook handed it, each file and line it opened, the file:line hits a recall or grep returned), or a flag that it
  read nothing. A call in a person's room is that person's, never the owner's, and a reply a Stop rule turned back makes none.
- The morning diff gives each of yesterday's calls its room and words, and under it its number and cites, or "no reads
  behind it": the first 20, then the count after. A person's room's calls are counted, never quoted; a quiet day says
  `calls yesterday: none`.
- `yerk calls fix ID PATH:LINE NOTE` files the correction beside the wrong line the way `remember` does: a note in the call's
  room's day log that names the line it supersedes and the call, that line marked superseded. It refuses a persona, a
  rulebook, a voice file or a rules file; the next morning diff shows the fix.
- **Upgrading:** nothing to edit; the ledger gains its `calls` table at the next start (no schema version change).
  budgets.toml: `[code] lines` 20,737 (a dated note).

### import-openclaw (#125)

- `yerk import openclaw:<workspace dir>` moves an OpenClaw box's notes in, one workspace a run: MEMORY.md, USER.md,
  memory/ and wiki/ (the kinds the index reads, and the text files of memory/transcripts/), and media/gym whole, copied
  as they are into <private>/openclaw/<its name>/<the same path>, files 0600, folders 0700, each with the box file's
  time. Never into the day logs or facts/, and outside imports/, so a note keeps its whole weight.
- Left by rule, one line a rule with its files and bytes: the box tools' own state (memory/health, jobstate, finance,
  sms, modelbench, forensics, tft, index, archive-offindex), a kind the index does not read, a link, a temp or backup
  copy and the folders the index skips; a dot or secrets folder is never entered.
- A line holding a shape of yerk doctor's secret scan (a key, a one-time code, a password or token line) is copied with
  its kind in the value's place, the file's lines kept; the run lists each by file and line. On an import the key is
  the tell, not the value: a line whose key is token, api_key, secret, password, passwd, pin, bearer, authorization,
  cookie, session or private_key (with `:` or `=`) is written `[token-line withheld]` from the key on, so a short
  token, a pin or a password with no digit never crosses; a line that only says the word (the token economy) is copied.
- By path and sha256 against <private>/openclaw/<name>.sums: a second run writes nothing, so the same command tops up; a
  changed file is written again through a temp file and a rename, one the box deleted is deleted from the copy, and a
  part the box no longer has is held. `--since YYYY-MM-DD` takes the files changed from that day on; `--dry-run` prints
  the inventory (files, bytes, each rule, the lines to scrub) and writes nothing. A workspace inside the private folder
  (or the other way round), a copy that names another box and, for a real run, a private folder in the repository are
  refused before the box is read.
- After a real run it prints the yerkd.toml and rooms.toml lines that would read the copies, and never writes them: each
  box path in an `also` line becomes its copy in its place, so every cite name stays; a `read` naming the box stays
  while the box runs (yerkd refuses one inside the private folder); a denyRead keeping a shell off the box gains the
  copies.
- **Upgrading:** nothing to edit; the printed lines are made by hand, then yerk-index and yerkd restarted. budgets.toml:
  `[code] lines` 20,565 (a dated note).

### fusion-levers (#118)

- A room's recall counts the meaning search's order a little above the keyword search's: in the reciprocal rank fusion of
  the two, the keyword rank's part is 0.4 and the meaning rank's 0.6, where both counted alike (`SHARE` in memory.py). On
  the install's 164 scored questions (13 controls), both halves recorded once from the live index and replayed for every
  setting, the top six goes from 103 to 107 and the top one from 59 to 62, and no control ranks lower. The keyword share
  was swept: 0.3 109/65 with a control falling from first to second, 0.5 103/59 (as before), 0.6 102/57 and 0.7 96/57,
  both with a control missed.
- Measured alone on the same questions and left out, as none raised both the top six and the top one with no control
  ranking lower: score fusion (each search's score over its best) in place of rank fusion, keyword share 0.3 to 0.7: 103/65,
  103/59, 103/54, 106/53, 106/53; a hit whose file name or first heading holds a name the question names ranked above a
  body-only hit, at 1.5 to 100 times: 100/56 down to 96/45; a facts file above a day log for a present-tense question: 104/61
  at 1.5, 105/63 at 4, with a control falling from first to third; the day logs newest first for "when did" and "last time"
  questions and oldest first for "first time": 103/59 at every setting (7 of the 177 questions say "when did", none the
  other two); the question's names followed by their card keys before the keyword search: 97/53, and 98/55 with a fuller
  form only, a control falling from first to second.
- Deep recall is unchanged: the lines a room is handed on each name and topic in a message come from the keyword half
  alone, where there is no meaning rank to share with; on 187 of the install's names they are byte-identical to main's.
- **Upgrading:** nothing to edit. budgets.toml: `[code] lines` 20,351: backup-restore's 20,348 plus this PR's 3 (a dated
  note).

### backup-restore (#128)

- `yerk backup [--to DIR] [--with-index]`: one dated archive, `yerk-YYYYmmdd-HHMMSS.tar`, 0600 in a 0700 folder: the
  private layer whole (persona, rules, rooms, memory, imports, the attachments folders, secrets as bytes with their
  modes), the ledger through SQLite's own backup (whole while yerkd writes), sessions.json and the rooms' small files,
  the index's metadata, and the `yerk*` units. The last member, MANIFEST.json, lists every other with its bytes, sha256
  and mode. The index's vector store goes in only with `--with-index`; `yerk-index` rebuilds it from the layer. One line
  a run; the last `[backup] keep` (7) stay. An archive is written under a hidden name and renamed only when whole.
- `yerk restore ARCHIVE [--to DIR] [--dry-run] [--force]`: the whole archive read against its manifest first; one cut
  short, missing a member or changed in a byte is refused before anything is written. Into an empty folder (the layer's
  own, on a new disk, too), or with `--force` over a layer that holds files after a backup of it; what it replaces, a
  unit file too, moved to `<state>/trash/<day>/restore-<time>/`; refused while yerkd runs on the target unless
  `--force`. A line a step; `--dry-run` writes nothing.
- `yerk status` says the newest backup's age and size on one line, red when there is none or it is past 36 h; `--json`'s
  `backup_age` reads the same folder (a missing `[backup] path` had read the current folder). `yerk doctor` says a
  backup older than 36 h, and none at all once yerkd.toml has a `[backup]` table.
- **Upgrading:** yerkd.toml gains an optional `[backup]` table: `path` (default `yerk-backups` beside the private
  folder), `keep` (7), `units` (`~/.config/systemd/user`). Copy `units/yerk-backup.service` and `units/yerk-backup.timer`
  to `~/.config/systemd/user/` and `systemctl --user enable --now yerk-backup.timer` (04:10, before retention's 04:20).
  budgets.toml: `[code] lines` 20,348 (a dated note).

### measured-budgets (#123)

- The budget gate reads what Claude Code sends. `yerk map`, the budget test and so `yerk deploy`'s fast gate take Claude
  Code's parts (its prompt, the built-in tools, Yerk AI's tools, the other tool entries) from the room's newest
  transcript, its newest request shape, where they counted them at their budgets.toml line or against numbers measured
  Oct 20. A room with no transcript yet is counted at its lines, and the map says which it used. A part past its line
  fails the gate, named with the room, the part and the transcript.
- Read that way, three rooms sent more than their lines that day and no check saw it: the hands plug-in's five tools
  (7,119) and web_image (913) inside the line for other tool entries (3,429), and a person's room's Edit and Write
  (1,731) past the built-in tools' line (10,401). budgets.toml gives each plug-in a line of its own, named by the prefix
  of its tools' names (`mcp__hands__` 7,119, `mcp__web-ladder__web_image` 913), the built-in tools 12,132 (the widest
  room's), and the session-start total 108,093: up by the 9,763 already sent, since no part could pay. That evening an
  owner room's new start sent Yerk AI's tools at 11,734 (schedule's and send's new descriptions), 34 past 11,700:
  `yerk_tools` 11,734, paid by Claude Code's prompt line (10,966; the largest prompt sent is 10,889).
- `yerk map --wire` splits the system prompt where that start's file began, so Claude Code's own prompt reads what it
  sent (it read -6,401 in a room whose layer had changed since its last start), and the first message's own blocks
  (Claude Code's environment and account reminder, the first hook) show outside the total, which no line names.
- The morning diff: each room's line is its session start as sent and what is left under the total, where it was Yerk
  AI's parts and the change since the last diff. What moved is the part lines below it, Claude Code's parts among them
  from the second diff on, and a Claude Code part past its line is a red line every morning it lasts.
- **Upgrading:** nothing to edit (budgets.toml ships in the repo). budgets.toml: `[code] lines` 20,048 (a dated note).
  The first morning diff after the deploy counts Claude Code's parts in each room's size for the first time.

### send-own-channel (#117)

- A room that names itself in `send` by its channel id (the id alone, or `discord:<id>` as rooms.toml writes it) posts in
  its own chat. The approval gate held that send as a send to another room and asked for the word, and the send would
  then have answered that no room has that name (seen live: a room asked to post a picture in its chat passed its
  own id). The gate and the send tool read one resolver: a room's name, its channel, or the id alone.
- Another room's id or channel is still a send to another room: held until the owner says the word, the held line naming
  the room rather than the id, then posted in that room. A value that is no room's name or channel is held as before, and
  the send refuses it with the rooms' names. A room with people still posts to itself only, its own id now included.
- The send tool's description says `room` is a room's name, and that the room's own name or its channel id both mean
  that room: 40 characters more, inside the line for Yerk AI's tools (11,671 against 11,700).
- **Upgrading:** nothing to edit. budgets.toml: `[code] lines` 19,976 (a dated note).

### web-budget (#116)

- The line for Claude Code's other tool entries (its deferred-loading placeholder and the web plug-in's tools) is 3,429,
  what the rooms sent when it was measured: web_research and web_research_result (1,716) came in on Oct 20 with no
  raise, and `yerk map` counts this part at its line until a transcript measures it, so no check saw them. `yerk map
  --wire` reads 3,421, under it, in the two rooms whose newest shape holds the plug-in's four tools and nothing more;
  the two whose newest shape came after the measurement (Oct 24, 13:2x) also send web_image (913) and read 4,334, one
  of them 11,453 with a second MCP server's five tools, both still over it.
- The budget test carries the research pair at its measured sizes, so it fails when the part as sent passes its line.
- **Upgrading:** nothing to edit. budgets.toml: `deferred_tool_names` 3,429 and the session `total` 98,330 (a dated note).

### attachments-root (#115)

- The files a chat brought when `yerk room add NAME` moved it in, saved in `<layer>/NAME-attachments`, can be recalled.
  `yerk pictures backfill ROOM --root FOLDER` and `yerk files backfill ROOM --root FOLDER` take that folder for its own
  room, NAME-on-yerk, when the room's `also` lists it: the one folder of the private layer they take. Another room's
  folder, and every other folder of the private layer or the state, is refused in the same line as before.
- `yerk room add` lists the folder in the new block's `also` when the block it copies has one (a block with none recalls
  every root and is left so; a person room never), prints the yerkd.toml `[memory] also` line to add by hand before the
  deploy (yerkd does not start while a room's `also` names a root `[memory] also` lacks), and ends on the two backfills,
  after the deploy line. `--dry-run` says "would list".
- `yerk doctor`, in its daily run, names an owner room whose moved-in folder holds pictures, workbooks or PDFs with no
  sidecar: one 🟠 line with the counts and the two commands, and to list the folder first while it is not.
- A recording's `NAME.transcript.txt`, the file the transcribe tool writes beside it, is indexed under a listed folder
  as a picture's `NAME.seen.txt` is, where before only a room's inbox took it, so a recording a moved room brought is
  recalled once it is transcribed. The recording itself is never indexed; nothing here transcribes one.
- **Upgrading:** budgets.toml: `[code] lines` (a dated note). A room moved in before this needs its folder, by hand, in
  yerkd.toml's `[memory] also` and in its block's `also` in rooms.toml, both as `"<layer>/NAME-attachments"` (in
  `[memory] also` first: rooms.toml alone stops yerkd's next start); then `yerk deploy`; then once each,
  `yerk files backfill NAME-on-yerk --root <layer>/NAME-attachments` and
  `yerk pictures backfill NAME-on-yerk --root <layer>/NAME-attachments` (workers on the room's account, 20 pictures a batch).

### gate-live-step (#114)

- `yerk deploy` holds the layer's recall controls (the question set's `"control": true` rows) after the fast tests and
  before the restart: each is searched on the exact half, one missed there once more as a room's recall searches, the
  meaning half and its 5 s wait included, and one missed then deploys nothing, the tree put back and nothing restarted.
  The card says `recall: N of N hit`, with how many only through the meaning half, or names the ones missed on a
  `missed:` line. It runs on the code under test, in the daemon's tree (`yerk recall-bench --controls`: one JSON line,
  nothing written to the bench series), about 3 s on the box's index. `--no-tests` skips it with the fast tests; with no
  index or no controls the card says `recall: not checked` and the deploy goes on.
- With the meaning half off, down or late, the recall tool's exact half alone (a room's recall, the bench, the deploy's
  controls) now weighs a stored message as the fused rank does: the keywords go by their cover times its share (RECORD,
  0.5), so a note comes before an old day log or an import that covers no more of the question, where the keyword list
  was used as it came (a hit's score there is still the keyword cover). On the install's 164 scored questions the exact
  half alone goes from 66 to 78 in the top six (top one 31 to 36), and 2 of the 3 controls only the meaning half found
  are found without it; the fused rank is unchanged (top six 103). No reindex.
- Deep recall is unchanged: the lines a room is handed on each name and topic in a message (decision 42) come from the
  exact half as the index ranks it, unweighed, as before (`ranked(weigh=False)`), so it picks the lines it picked; a test
  pins them to main's. Weighed, 90 of 186 sampled names on the install would have traded old day-log lines for notes.
- **Upgrading:** budgets.toml `[code] lines` goes to 19922: shrink-1's 19,865 plus this PR's 57 (a dated note).

### shrink-1 (#112)

- Deletion only: 725 counted lines out (20,590 to 19,865), toward ROADMAP-v3 claim 9. Nothing a room or a job used this
  week goes; the script job kind stays (two live jobs) and so does the voice note's waveform (discord.py reads it).
- The watcher job kind is gone (380): `kind = "watcher"` and its keys (`prefix`, `regex`, `batch`, `lines`, `wake`,
  `prime`) are refused, and the ledger writes no `watch_events` rows. A room's own `watch` channel in rooms.toml is unchanged.
- `schedule` is a systemd calendar line now (152): `Mon 10:00`, `Mon..Fri *-*-* 07:30`, `*:0/30`, read in `tz`, checked by
  `systemd-analyze calendar` and read back from systemd, never parsed by Yerk. The `every` key is gone: an interval is a
  calendar step (`0/2:00` is every two hours). A cron line is refused with one line naming the calendar line that says the
  same, in `yerk jobs sync`, `yerk jobs list`, the schedule tool and `yerk doctor`; the jobs file is never rewritten. A
  calendar run's planned time is when it fired, so a run caught up after the box was down shows its catch-up time.
- Console mode is gone (84): `yerkd --console`. The contract probes' `smoke` and `measure` go through the room's test webhook.
- The two one-off migrations whose job is done are gone (88): the day-log rewrite at the daemon's start with
  `yerk memory migrate-daylogs`, and the ledger step that turned comma text into rows (a fresh ledger runs steps 1 and 3).
- The shadow Stop check's `file` and `decide` rules are gone (21); the figure rule and the rest log as before.
- **Upgrading:** jobs.toml: each cron `schedule` becomes a calendar line at the deploy, before the next `yerk jobs sync`.
  `yerk doctor` prints each as `replace schedule = "0 10 * * 1" with schedule = "Mon *-*-* 10:0:00"`, the text its timer
  already carries, so the sync finds the unit unchanged. Until it is swapped, the job's runs are refused (ledgered "not
  run" with that line), and a sync removes its timer. budgets.toml: `[code] lines` (a dated note).

### rules-margin (#113)

- Test only. The rules-part test tripled a 5,300-character rules file and expected it over the 16,000 line, but the
  part counts its own `[from <path>]` line, so it came to 15,907 plus the length of the test's temporary path: 15,992
  at /tmp's 85, over the line only at 94 or more, as under a longer TMPDIR. No earlier test's state was in it. It
  refused a deploy on 2026-10-24 with nothing wrong in the layer. The file is now quadrupled (21,207 plus the
  path), so the test says what it means in any temp folder.

## 2026-10-23

### rules-on-demand (#102)

- A rule the shared rules file has no room for is no longer held in no room: it is filed in `memory/rules-topics.md` and
  loads on a message that names its topic. The machine-room notice says "filed as a topic rule: loads when <its words>
  come up", with its job, the room it came from and whether it is that room's own or every room's; the room ends its reply
  saying the same.
- A topic rule's words are its job's words and what its own words name (a #channel, a file, code, a capitalised name),
  weighted by how few rules say each, the way recall weighs a word. A message (with its attachments' names) that holds
  enough of them gets the rule in the per-message rules part, beside "Rules changed since you last looked", best match
  first and under that part's 1,500 characters, once per session and again after a compaction. It never loads at session
  start, so the rules file's size and every room's start stay as they were.
- A rule is kept to the rooms reading its room's rulebook (`[rooms: NAME,...]` before its trailer) when the heaviest of
  its words that any rulebook says is said by that rulebook alone, so a rule about work chat messages filed in a work room
  never loads in a family room; any other rule loads in every room. The mark can be edited by hand.
- At yerkd's first start with this change, `rules-overflow.md`'s rules move to `rules-topics.md` once (the old file kept
  beside it as `rules-overflow.md.moved`, one log line, one machine-room line), the newest of a job's lines live.
- `yerk curate --rules` proposes three more kinds, each with BEFORE, AFTER, WHY and EVIDENCE: `topic` (a rule of the rules
  file filed off a message naming its topic, on no every-reply topic, named by under a third of the week's messages, moved
  to the topic rules, freeing its characters), `promote` from the topic rules (a rule every room's that rode in most of
  the week's sessions, into the rules file when it fits) and `gate` (a rule a code check could hold on a reply: a banned
  phrase, a format, a figure; it names the check and never builds it, and `--apply` refuses it).
- `yerk map ROOM` shows the topic rules' count and size and which rode on the room's last 20 messages; `yerk doctor`'s fit
  check reads `rules-topics.md`: a topic rule too long for the per-message rules part or kept to no room on file, and an
  overflow not moved yet.
- A topic rule filed as a correction marks its job's older line in the rules file superseded, so a session start no longer
  sends the old rule beside the new one.
- A `[rooms: NAME,...]` mark typed on a line of `rules-overflow.md` before that first start is kept by the move, so the owner
  can keep a held rule whose words are every room's to one room.
- **Upgrading:** nothing to edit. budgets.toml: `[code] lines` (a dated note). The move runs by itself at the next start.

### curate-examples (#108)
- A rule condensation keeps the rule's worked examples. `yerk curate --rules` now cuts a long rule's prose first (a clause
  it says again, a hedge such as "basically" or "of course"), then its quoted asides, and takes its examples out only when
  its file is past its line in budgets.toml even with every other proposal in. Its WHY says what it kept and the file's
  size against its line. A long rule with nothing but examples to cut is no proposal at all: the three condensations of
  a live rulebook proposed on 2026-10-23, each taking its examples out with the file 3,451 characters under its line, are
  not proposed again.
- The morning diff lists the last day's rule proposals by kind with their numbers ("condense: 1-3", "promote: 4, 5")
  and the `yerk curate --apply DAY N` line.
- A script job's command now gets the unit's YERK_PRIVATE and YERK_STATE and this install's `yerk` first on PATH, so a
  `yerk` line in a job runs on the install's folders; before, it fell back to the fake example layer, or found no `yerk`.
  Still no account token.
- **Upgrading:** to run the rules pass nightly without units/yerk-curate.timer, copy `example-curate-rules` from
  examples/private/jobs.toml into the private jobs.toml (its room, its zone) and run `yerk jobs sync`.

### missing-ability (#110)

- A room that hits a wall (no tool, a refused tool, a page that will not open) now files what it needed with the new
  `need` tool, in place of building a way of its own: one line with what was asked, what it tried and what it needed,
  plus what it did anyway behind a `[shadow]` mark. The machine room gets one line, "need N from ROOM: ABILITY", with
  the command that builds it. The same ability filed again from any room is counted on the open need, never a second one.
- Each need sits on the day's rule proposals page (`<memory>/rules-proposals/<day>.md`) as `N. need: ABILITY`, in the
  same numbering as curate's proposals; curate's own proposals take the numbers the needs leave, and its nightly rewrite
  of the page keeps every need.
- `yerk needs build DAY N` starts a builder worker with a brief made from the need (every ask, the repository, and the
  shape of done: the ability built once in Yerk with tests, committed on its own branch, never merged, pushed or
  deployed by the worker), its card in the machine room. When it ends, the machine room hears "need N built, PR N" (or
  the branch, or that it ended with no branch and is filed again). `yerk needs list` shows every need and its state;
  `yerk needs done DAY N PR` marks one merged.
- A person's room may file a need about its own files only; one naming another room's folder is refused with one line.
- **Upgrading:** yerkd.toml, optional: `[needs] account` (the account builders run on; default `[curate] account`) and
  `[needs] cap_minutes` (default 60). budgets.toml: `yerk_tools` 11,700, `mechanics` 4,500 and `total` 96,614 (on main's
  location line) and `[code] lines`, each with a dated note. Each room has the `need` tool and its mechanics line from its next process start.

### sheets-and-pdfs (#104)

- A workbook (.xlsx, .xlsm) or a PDF sent to an owner room gets its text written beside it as it is staged, by code with
  no model call: `NAME.sheet.txt` (a `## <sheet name>` section a sheet, a row a line, its cells tab-separated, empty rows
  skipped, a formula as the value saved with it) or `NAME.pdf.txt` (a `## page N` section a page). Recall finds what is
  in it and shows its lines behind "[text of a file sent to the room, as extracted]", never as anyone's words, and the
  `[attachment]` line names the text file so the room can read it in the same turn. A message waits 30 s at most for
  its files' text however many it holds, and a message sent after it still reaches the room after it.
- A scanned PDF (no text on any page) gets one line naming it, its pages and "no text layer, not read"; there is no OCR.
  A file that does not open gets one line saying so. Past 200,000 characters the text is cut, its last line saying how
  many characters were not kept; a file still being read after 60 s stops there and says so. A person's room's files are never read this way.
- `yerk files backfill ROOM [--dry-run]` writes the text of the workbooks and PDFs already in the room's inbox, and of any
  whose text is older than the file, in that process (no yerkd, no model); `--root FOLDER` does it under a folder the room
  lists in rooms.toml (`read` or `also`).
- **Upgrading:** two new libraries, openpyxl and pypdf: `uv sync` in the daemon's checkout before `yerk deploy`, which
  installs nothing (until then no text is written and the files wait for the backfill). budgets.toml: the new `[files] chars` (200,000) and
  `secs` (60), and `[code] lines` (a dated note). Nothing to set; run `yerk files backfill ROOM` once per owner room for the files sent
  before this.

### location (#109)

- An owner room's messages carry where the owner's phone last was, one short line after the metadata line:
  `[place] near the office (Example City), 7 min ago, stationary`. It comes from yerkd.toml `[location]`: `file`, the
  fixes a location watcher writes (one JSON fix a line), and `places`, its table of named places, the fix named by the
  nearest place whose radius holds it; both only read, nothing sent anywhere. No line when the newest fix is older than
  `stale_after` (3h unset) or at no named place, so no coordinate is ever said; none in a person's room or on a person's
  message; no table, no line in any room.
- The mechanics say when to use it unasked: at a gym, log the session there; at work, mind the hours and the commute;
  moving, keep replies short.
- `yerk location day [DATE]` writes the day's places into that day's log as one line the owner's rooms recall and no
  person's room reads: each stay with its first and last fix, `08:40 to 12:00 the office (Example City); 17:40 to 23:40
  home (Example Town)`. A re-run replaces its own line where it stands; `--dry-run` prints it. README has the nightly
  script job for jobs.toml.
- **Upgrading:** yerkd.toml, optional: `[location]` with `file`, `places` and `stale_after`. budgets.toml:
  `[per_message.parts] place` (150) and `[per_message] cap` 5,650, `mechanics` 4,300 and the session total 95,714,
  `[code] lines` (dated notes). jobs.toml, optional: the `location-day` script job in README. Each room has the new
  mechanics line from its next process start.

### recall-pack-fit (#107)

- Every part of a room's session start now fits under its key in budgets.toml by construction. The recall pack counts
  its own head and the line saying how many older items it left out, in Claude Code's characters (an emoji outside the
  basic plane counts 2), and leaves its oldest items out whole. That morning one room's pack came out at
  6,003 against its 6,000 (that line is 64 characters once the count has three digits, where 60 were kept for it, and two
  emoji were counted once each), and the session start then cut 8 of the owner's items from the middle of the pack, two
  of them mid-line.
- A part still over its key (a file past it, or the open commitments alone past the pack's) loses whole lines, its
  marker counted inside the key. Nothing is cut mid-line any more.
- The machine room hears of a cut only when lines of a part's own file went, in one notice of two short lines: the part
  and how many lines, then its key and the room (`🟠 rules: 3 lines dropped`, `to fit its 16,000 in example-room`). The
  pack leaving its oldest items out is its normal shape and is never said; its own last line tells the room how many.
- After a compaction the re-added recall pack is fitted the same way. The per-message recall part keeps its "nothing on
  file" lines whole while they fit its key, and the taste part, still over with every reply at 80 characters, drops its
  last reply whole.
- **Upgrading:** budgets.toml: `[code] lines` (a dated note); no key changes. Each room sends the fitted parts from its
  next process start.

### taste-eval (#106)

- PR 93 (judge-2) cut to its eval half; PR 93 is closed in favour of this one. `yerk eval taste [--cases P --controls P
  --rubric P --ledger P --sample N --cap S]` replays the taste judge over the owner's complaints and the replies he
  accepted, each with what its turn read, joined from the ledger read only and cut at the reply's time (a row the
  ledger has no turn for is judged on the chat alone, never told that a claim no read carries was not checked). It
  prints precision, recall, the false alarms per verdict (missed-the-ask, unchecked-claim, not-his-voice,
  slow-or-no-answer) and the median judge seconds beside phase 1's, and the bar a gate would need (recall at least 5 of
  7, false alarms under 1 in 10), never enforced: exit 0 whatever the numbers, 2 when the case file is missing. A call
  that ends in an error (an account out of a model) is left out of the scores and counted, and a run where every call
  failed says so instead of printing a recall of 0. The rubric is the private layer's `evals/taste/rubric.md`, else the
  package's `taste-rubric.md`; the case file stays private and `tests/fixtures/taste-judge-*.jsonl` are example values.
- Left out of PR 93: the judge call after every reply to the owner (a `claude -p` call on the room's account, in a
  thread after the Stop hook), its `shadow` rows, the `taste_judge` room key and the morning diff's count of taste rows.
  No reply is judged as it is posted, so no reply costs a second model call.
- **Upgrading:** budgets.toml [code] lines goes to 19462. rooms.toml takes no new key.

### room-defaults (#111)

- One tool set for every owner room, declared once: a `[defaults]` table in rooms.toml holds `allow` and `mcp.<name>`
  server tables, and every room whose privacy is owner (or unset) carries them from its next process start. A room's own
  `allow`, or its own `mcp.<name>` table, replaces that one default whole; `allow_remove = [...]` in a room drops named
  tools from the allow it inherits; `{room}` in a server's `args` and `env` is the room's name. A room whose privacy is
  anything else inherits nothing. Each room keeps its own sandbox. Any other key in `[defaults]` stops the start with one
  line naming it.
- `yerk doctor` names an owner room that drifts from `[defaults]`, one line each: red for a default tool its own allow
  misses and its `allow_remove` does not name, orange for its own copy of a default (an allow equal as a set, a server
  equal once `{room}` is its name), since a copy is how the rooms come apart later.
- `yerk room add` copies the room it is made like as rooms.toml writes it, so a new owner room inherits `[defaults]` under
  its own name; before, it copied the other room's tables with the other room's name in them.
- **Upgrading:** rooms.toml, optional: move the tables every owner room repeats under `[defaults]` (rooms.toml.example
  shows the shape, commented) and take each room's copy out; `yerk doctor` names any copy left. An owner room with no
  tools of its own, a machine room too, gains the defaults. budgets.toml: `[code] lines` (a dated note).

### status-card (#105)

- `yerk status` and the room's `/status` show each account the way the box's own card does, two lines each, listed in
  accounts.toml order: `- ① 5h 7% · wk ⚠️93% · Fable ⚠️100%`, then `- ↻ 5h 5p · wk 12a · 💳 Nov 8`. Account one to
  twenty is ① to ⑳. A number at 80% or more wears ⚠️. The line starts red when the account rests (its second line
  then says until when) or a 5-hour or weekly window is full, orange when the model's week is full or any window is at
  90% or more.
- The meters are live: the card runs Claude Code's own `/usage` on each account's config folder (no model call; Claude
  Code reads its own login, never the one-year token, which cannot read the meters), at most three at a time, and keeps
  the answers a minute in `<state>/usage.json`, so a second card within the minute starts nothing. An account whose call
  fails shows the stream's last reading and how old it is. `yerk status --json` asks nothing new and uses a reading
  under a minute old when there is one; deploy's proof and the morning diff never ask.
- The order of resets: the account whose week resets soonest wears 🟢 on its tag, and two lines say the next reset and
  the one after it for both windows, `- wk next: 🟢① 12a · then ⑤ Sun 3a` and `- 5h next: ④ 2p · then ③ 2:40p`. One
  more line names the account the next turn tries first (the first in order not resting). A room on an account of its
  own says it by tag (`on account ④`), and rooms on one account share a line (`- on account ④: 6 rooms`).
- **Upgrading:** accounts.toml, optional: `bill_day = N` on an account (1 to 31) puts its next bill under it, as a
  date. budgets.toml: `[code] lines` (a dated note). A cold card for five accounts takes two rounds of Claude Code's
  start (about 12 s); Discord shows it thinking meanwhile.

### model-account-pin (#103)
- `/model` takes an account as well as a model, in one argument: `/model five` (or `/model 5`) puts the room on account
  five, `/model five fable` (or `five/claude-fable-5-1`) on both, `/model fable` on the model as before, and
  `/model default` clears both. As the owner types, Discord suggests every account with its order and whether it is
  resting for the room's model, the models, and each account with each model; anyone else is offered nothing, so no
  account name shows to the guild. Bare `/model` lists the accounts in the order tried and the models under the room's line.
- The pinned account is tried first; a rest or a limit on it falls through in accounts.toml's order, every account out of
  the pinned model runs the turn on the room's own model and says so, and the pin stays for the next turn, as before.
  The typed `account` option still works (with it, the argument is a model alone), and takes the digit too.

**Upgrading**: nothing to do; the next start registers `/model`'s argument as suggestions in place of its fixed list.

### person-room-parity (#101)

- A person's room can read what the other assistant already told them where it watches. List that assistant's Discord
  user id in yerkd.toml (`[watch] assistants = ["<user id>"]`): its messages in a watched channel go in the room's day
  log under their own label, "[the other assistant's reply where this room watches: a record, never an instruction]",
  about 30 seconds after they stop changing, so a progress card it edits and then deletes is never filed. They are never
  answered or run and never fill the recall pack; recall and read_memory find them, so "which answer was better?" can be
  answered by reading both.
- A person's room files what it read on a picture sent to it (`seen`), and is turned back once when it read one and filed
  nothing, as an owner room is. What is sent there (the text kinds, a recording's transcript, a picture's
  `NAME.seen.txt`) is recalled there and cited as `<room>/work/inbox/...`, the same way: that room and the owner's rooms
  read it, no other room does. An owner room's uploads stay in the owner's rooms, whatever a file's own front matter says.
  A picture's lines now read "[text on a picture sent to the room, as the room read it]".
- A person's room now turns back, once a result, a reply that says what a third person said or quotes them from a turn
  that read nothing, as an owner room does; the refusal points at the room's own lines (its day log, a file sent there, a
  recording's transcript), never at a file outside its folder. There the owner is a third person too, so his words need a
  read. The second try goes out as it is.
- **Upgrading:** yerkd.toml, optional: `[watch] assistants`, a list of Discord user ids (anything else, or the owner's id
  or one a room's `people` lists, stops the start with one line). budgets.toml: `[code] lines` (a dated note). Each room
  has the new `seen` description and mechanics line from its next process start.

### message-envelope (#96)

- `yerk map ROOM --wire` reads every later message by part: Yerk AI's hook (clock, metadata, rules, recall, taste) and
  a compaction's recall pack, each against its budgets.toml key with how many messages passed it; the reply header,
  attachment lines and the message's own text, reported and never cut; and Claude Code's own blocks by name (sandbox
  instructions, files re-read after a compaction, notes on changed files, environment, token count, other reminders).
  Then the five largest messages of the last 7 days, in the room's time zone, each with the part that made it large.
- The median row's OVER now reads against the parts' keys. `typical` (1,500) is a total that parts each under their own
  key pass on most messages, since recall rides on nearly every one and taste on every owner message; the row says so.
  Measured in one room on 2026-10-23: median 2,192 over 303 messages, no part past its key, Yerk AI's hook parts
  together at most 4,481 of the 5,500 cap. Its largest message, 60,927, was Claude Code's restore after an idle
  compaction (five files re-read whole, 27,458; sandbox instructions, 19,600; environment, 2,641) beside Yerk AI's
  recall pack (5,938) and hook parts (4,140), each under its key.
- Two counting fixes in the same measure: the hook text Claude Code merges into its own block at a process start or a
  compaction's restore was counted twice (34 of 326 messages in that room; the largest read 65,164 for 60,927), and
  a compaction's summary, Claude Code's image notes and the Stop hook's feedback were counted as messages (23 of 326).
- Nothing is cut: no Yerk AI part passed its key in any room's 640 assemblies from Oct 18 to Oct 23.
- **Upgrading:** budgets.toml: [code] lines to 18,712 with its dated note; no budget changes.

### pictures-seen (#97)

- A picture sent to an owner room is findable later, the way its uploads and recordings are: in the same turn the room
  files what it read on it beside the picture as `NAME.seen.txt` (the new `seen` tool; the room already reads every
  picture before it answers, so there is no second model call and nothing paid). Its first line is written by code (the
  room, the message id, when it was saved, its name), then every word on the picture in reading order (a chat
  screenshot keeps who said what), then a line or two of what it shows: a person in a photo the owner sends in his own
  room is the owner unless its own text says otherwise, anyone else named only as the picture names them. Recall and
  read_memory cite it as `<room>/work/inbox/<id>/NAME.seen.txt:line` and show its lines behind "[text on a
  picture he sent, as the room read it]", never as anyone's words. A room that read a picture and filed nothing is
  turned back once to file it. A person's room files no picture and none is indexed.
- A reply to a picture in an owner room takes a little longer: the words on it are written out once, as the `seen` call,
  before the answer.
- `yerk pictures backfill ROOM [--limit N] [--dry-run]` files the pictures sent before this through Yerk workers on the
  room's own account and model, 20 to a batch and one batch at a time, so the account's other workers wait for one batch
  at most. Each batch's card and its one line ("batch 1 of 8 finished clean, 20 of 20 filed; batch 2 started") are in the
  machine room; no relay turn runs in any room. `--dry-run` lists each sidecar it would write and starts nothing.
- `yerk pictures backfill ROOM --root FOLDER` does the same for the pictures under a folder the room lists in rooms.toml
  (`read` or `also`), each sidecar beside its picture, cited and indexed under that folder as its other files are; a
  folder the room does not list, or one of Yerk's own, is refused in one line.
- `yerk map --wire` measures a room's newest request shape, so a session resumed for days shows the tools it sends now.
- **Upgrading:** budgets.toml, each with a dated note: `[code] lines` to 18,602; `yerk_tools` 8,000 to 11,000 (Yerk AI's
  tools measured at 9,978 before `seen` and 10,823 with it); `mechanics` 4,000 to 4,100 (web-image's
  4,000 and this line); the session `total` 88,914 to 92,014. Each room has the `seen` tool from its next process start. Then, with yerkd running, once per owner room:
  `yerk pictures backfill ROOM --dry-run` to see what it would read, and the same without `--dry-run` to start it. For a
  folder of older pictures, list it in the room's `also` (and in yerkd.toml's `[memory] also`, as every `also` is) or
  `read`, restart yerkd, then the same with `--root FOLDER`.

### web-image

- A room can look at a picture on the web: `web_image(url, max_bytes?)`, the web plug-in's fifth tool, saves one jpeg,
  png, gif or webp of 15 MB at most into the room's work folder as `web/<the URL's sha1, 12 hex>.<ext>`, where the
  room's own Read shows it, and says its path, bytes, type, width, height and the rung that read it. It is read by a
  plain fetch from the machine first, asked for as a browser asks for a picture, then by Browserbase Fetch without and
  with proxies only when a site refuses that (each paid call a spend row, as web_fetch's are), never by a browser rung.
  Another picture type (avif, svg), a page or any other file is refused where it stands, a private host is refused as
  web_fetch refuses it, and the same URL within a day is the saved copy, nothing fetched.
- A private host written as an IPv4-mapped IPv6 address (`http://[::ffff:127.0.0.1]/`, which a URL writes
  `[::ffff:7f00:1]`) is refused by web_fetch and web_image alike; before, both reached a server listening on 127.0.0.1
  only (web_fetch since #76).
- web_fetch's wall for a picture says `It is an image: use web_image(url), then Read the file it saves.`, and its next
  move no longer says to download the file, which sent a room to curl in a shell with no network.
- The mechanics say it in one line: a picture in a post or a page is saved by `web_image`, then Read; never curl.
- And in one more: an x.com or twitter.com post is read through fxtwitter's API first
  (`https://api.fxtwitter.com/<user>/status/<id>`: the text, counts and picture URLs, free, under a second), never the
  x.com page, which serves a login page at a paid rung; its picture then through `web_image`.
- **Upgrading:** budgets.toml: `mechanics` goes to 4000 and `total` to 88914. Each room that attaches the plug-in needs
  `mcp__web-ladder__web_image` in its `allow` (rooms.toml.example has it); a yerk-web process already running keeps its
  four tools until its room's session starts it again.

### deploy-private

- `yerk deploy` with no `--private` and no `YERK_PRIVATE` no longer runs its fast tests on the fake example layer: it
  takes the private and state folders from the running yerkd's own command line (`--private` and `--state` in its
  unit's ExecStart), so the budget test and the live index test read the layer the daemon serves, and the card's
  second line says `private layer: the daemon's`. With no unit found, or no `--private` on its command line, it refuses
  in one line, `no private layer: pass --private`, before anything is touched. A `--private` given always wins.
- **Upgrading:** budgets.toml [code] lines goes to 18320. A unit installed from `units/yerkd.service` sets its layer in
  `Environment=`, not on its command line: pass `--private` to `yerk deploy` there.

### web (#76)

- Web reach is Yerk's own: `plugins/web` (`yerk-web`), one MCP server in the repo and outside the harness, serves the
  four tools the rooms already call (`web_fetch`, `web_search`, `web_research`, `web_research_result`) under the same
  names and arguments. It reads nothing outside the install: keys from the private folder's `secrets/`, settings from
  an optional `web.json` there, state under `<state>/web/`.
- `web_fetch` climbs a plain fetch from the machine, its own stealth browser and trawl (each skipped without a try when
  it is not installed), then Browserbase's paid rungs; it names the wall when a page stays shut and remembers per site
  and path class which step read it. The free rungs' caps are one window for every room's process.
- The ladder is the machine's as of 2026-10-22: the stealth browser stops waiting once the text is readable and settled
  (a page carrying Cloudflare's bot script is no longer taken for a challenge), a page behind a modal dialog reads as
  the page (a listing's "Translation on" notice gave 135 chars), and two free rungs on the same captcha skip the paid
  fetch rungs and cap the call at 45 s.
- `web_search` runs Exa, then Parallel's paid API, then You.com's and Parallel's keyless engines; `web_research` runs
  the Exa Agent at four fixed efforts under one daily cap of US$2 for every room.
- Every paid call is one row in the ledger's `spend` table (the one transcribe, #77, brought), and the morning diff says
  `spent yesterday: US$x on n calls`. `yerk explain web` answers from abilities.toml, where one built `web` entry
  replaces the described hard-pages, web-search and web-research.
- **Upgrading:** budgets.toml gains a dated note only, its [code] lines unchanged (the spend table, its read and the
  morning line came with transcribe, #77). Run `npm ci --omit=dev` once in `plugins/web`
  (Node 22.13 or newer). In rooms.toml each room's `[rooms.NAME.mcp.web-ladder]` table changes only its command line,
  `args = ["<repo>/plugins/web/server.mjs", "--private", "<private folder>"]`; its `allow` list stays. Keys, each
  optional, go in the private folder's `secrets/`: `exa.key`, `parallel.key`, `browserbase.key`,
  `browserbase-project.id`. An install moving from an older web setup keeps its saved logins and site memory by
  copying that setup's `contexts.json` and `local-cooldown.json` into `<state>/web/fetch/` once, before the first call.

### said-or-noted

- In an owner's room a reply or a `send` that tells what a third person said, decided, wants or asked, or quotes their
  words, from a turn that made no reading call is turned back once, naming them: read where their words are (the day
  log, the transcript, the mirror message, the file) and cite it, or write it as "from my notes, not their words". The
  second try of the result goes out as it is. Each refusal is one shadow row, `person-refused` or `quote-refused`, and
  the morning diff counts them beside sheet and paste. A person's room only logs, as before.
- A sentence about the owner ("you", or the name `[import] me` in yerkd.toml gives his words) or the room itself is
  never a claim about a third person, in the shadow rows too: eight of the first day's twelve `person` rows were the
  owner's own words read back to him. Nor is a bare "told" with only punctuation after it ("286 posted, Sam told,"):
  that is his own act in the passive, the first day's one false alarm. A sentence that says "from my notes", "not their
  words" or "proposed, not confirmed" passes.
- A note a room filed is shown in the recall pack, the topic hits, recall and read_memory behind "[the room's note, not
  their words]", in the day log and in a fact file alike. `remember` takes `said` (who) and `source` (the message id,
  file path or transcript line): the text is filed word for word as a `said` line in the day log, shown behind "[their
  words]"; `said` without `source` is refused.
- What was sent to an owner room is a recall root there: its `work/inbox` (text, Markdown, csv, json, vtt, srt and the
  transcribe tool's `NAME.transcript.txt`; never audio, images or other binaries), cited as `<room>/work/inbox/...`, of
  the private class. A person's room's uploads are never indexed, so they never reach an owner's room.

**Upgrading:** nothing to edit. yerk-index sees the inbox roots at its next start (the scope changes, so one pass
chunks every file again and writes only the changed chunks; the inbox files are embedded once). With no `[import] me`,
only "you" and "your" mark the owner.

## 2026-10-22

### recall-ratchet (#87)

- The recall bench is a ratchet. `yerk recall-bench` still adds its row to `<state>/memory/recall-bench.jsonl`, then holds
  it to the four runs before it on the same questions: a top six more than one question under their best exits 4 and
  says so in one `RATCHET:` line, unless budgets.toml `[recall] drops` holds a dated note naming that drop
  ("YYYY-MM-DD: BEST to NEW, why"). Where the install's index is, the budget test runs the bench on the code under test
  (writing nothing) and fails the same way, so `yerk deploy`'s fast tests refuse a deploy that loses recall; a box with
  no index, and CI, skip it. On the author's box the bench fell from 116 to 98 of 164 between Oct 19 and Oct 21 with
  nothing in the way: the index grew (an import of old chats into the day logs, five new roots and five more), and Oct
  19's code scores the grown index exactly as today's does, question for question.
- Recall ranks a note above a stored message on the same ranks: a line under `<private>/imports` (texts, mail) or a day
  of the install's own log older than the room window (14 days) counts half in the fusion (`RECORD`, 0.5). On the
  author's 164 questions against the grown index: top six 99 to 105, top one 56 to 60, no question lost (one answer
  moves from first to second).
- **Upgrading:** budgets.toml gains `[recall]` (`slack = 1` and `drops`, with the author's Oct 22 note) and [code]
  lines, up 37. An install whose bench fell before this change adds its own dated note, or its next run exits 4.

### curate-rules

- The rule files get better by proposal, never by a model rewriting them: the nightly `yerk curate`, after its
  promotion, and `yerk curate --rules [--dry-run]` alone, read the rules file, each room's rulebook and
  `rules-overflow.md` and propose merges (two rules on one job, in near-duplicate words, or from one message on jobs
  that overlap, never one message alone: one line with every clause of both, the fuller example and both cites), cuts (a superseded line a session start still sends,
  a cite to a day-log line that is no longer there, an older rule a newer one on the same job says otherwise to, both
  quoted), condensations (a line past 600 characters with its examples and quoted asides out, never a reason or an aside's own
  clause, its own words otherwise)
  and promotions (a rule held in `rules-overflow.md` that fits again, naming the proposals it needs first). Each has
  its evidence of the week: the shadow bounces, rated replies and `yerk` day-log lines that touch it, with cites.
- They go numbered to `memory/rules-proposals/<day>.md` (0600: BEFORE, AFTER, WHY, EVIDENCE, and each rule file's size
  now and with every proposal in), and one card to the machine room lists the ten that save the most, at most once a
  day and never when there are none.
- `yerk curate --apply DAY N[,N...]` is the owner's word: those proposals written, every file backed up first by the
  curate backup, refused whole when a proposed line changed since or a file would pass its line in `budgets.toml`, a
  line blanked rather than cut so every cite keeps its number, an apply cut off mid-write put back by the next curate.
  `yerk curate --apply DAY undo` puts the last apply back byte for byte, unless a rule was filed into the file since.
  No room can apply one: the CLI, run on the owner's word.

### rules-fit (#91)

- A new rule no longer pushes proven rules out of every session start: a room filed a rule with a 190-character
  example, the shared rules file went from about 12,230 to 12,825 against its 12,500, and the middle cut dropped seven
  established rules in every room. A rule filed with `remember` and `job` that would take the rules file past its cap is
  fitted before it is written: the `<example>` blocks come out of the rules, newest first and the new rule's own first,
  until the file is at least 150 characters under the cap. A rule's own words are never cut, only its example. Each
  strip is one line in the filing room's day log.
- A rule with no room even with every example out is not written to rules.md: the file stays as it was, the rule goes
  whole to `rules-overflow.md` beside it (0600, private, read by no session start), the machine room gets one notice
  (the rule's job, the room it came from, the file it is held in and the cap), the hold is a line in the day log, and
  the room ends its reply saying the rule is held. The filing-time "over cap" line and the `over_budget` result are
  gone, since a filing can no longer take the file past its cap.
- A rule file still past its size at session start (a hand edit can do that) keeps its first lines whole and drops the
  newest, the last ones, never a line from the middle: the rules part and each rulebook, by `truncate(..., mode="tail")`.
  An `<example>` a hand-kept rulebook spreads over several lines goes whole or not at all, never cut inside.
  The mechanics, persona, room voice, pinned memory and recall pack keep the head and tail trim. The once-a-day
  machine-room notice names a rule file's dropped lines by their job.
- `yerk doctor`'s fit check reads a non-empty `rules-overflow.md` as an orange finding with the count of held rules, until
  each is moved into rules.md by hand.

**Upgrading:** budgets.toml [code] lines 17,701; nothing to do. No cap moves.

### sheet-hard (#90)

- Rules were being cut from the middle of every session start, said to no one, and no longer are: the shared rules file
  sat at 12,298 against its 11,886 (seven rules gone in every room) and one room's rulebook at 12,696 against 10,850.
- An owner's room no longer ships a document or a paste in the owner's name without a claims sheet. A `send` of a
  document (.docx, .pdf, .xlsx, .pptx, .txt, .md or .csv) or of a text holding a pasted block (a fence with no language
  tag, or text, plain or none) with no claims sheet written that turn is turned back once, `{"ok": false, "error": ...}`,
  the error saying what the sheet is (a file whose name holds claims, one row per claim: the claim, its source line, the
  quote from that line) and where to write it (beside the document, else in the room's work folder); the next send of
  the turn goes out as it is. A final message holding a pasted block bounces once at Stop the same way, the Stop hook's
  sixth rule, one ask per turn between the two. Written this turn is the shadow rule's own test (a Write, Edit or
  NotebookEdit of such a file, or a shell command naming one), or such a file changed since the turn began in that
  folder. Each refusal is one `sheet` shadow row, so the morning diff counts refusals as it counts stubs and pastes. A
  person's room (people set) is never refused and still only ledgers, as before.
- A cut is said: a session start that sends a part trimmed posts one line in the machine room naming the part, the room
  and the lines it cut (their first six words, one a line, twelve at most, then how many more), once a day per part and
  its file, so a file every room shares is said once, by the first room to start. `yerk doctor` has a fit check: a part past its line is a finding with the overflow in characters and the rooms it is cut
  in, so the doctor and the morning diff read red the same day.

**Upgrading:** budgets.toml `rules` 11,886 to 12,500, `rulebook` 10,850 to 14,000 and the session-start total 84,750 to
88,514 (the same 614 + 3,150), [code] lines 17,640; nothing to do. Each room's next process start says in the machine
room any part it still trims (`cut-said.json` in the state folder keeps the day).

### paste-check (#89)

- A room no longer posts yesterday's block as today's: on 2026-10-22 a room sent again, through `send` and word for
  word, a fenced plan it had posted the evening before, its "tomorrow" and its times that day's. A fenced block
  equal to one the same room posted on an earlier day of the last 14 (each line stripped, blank lines dropped, runs of
  spaces and tabs made one, the fence's language tag left out: the day log's own shape) is turned back once per result,
  in two places with one set of words and one flag: the `send` tool refuses it before it posts, and the Stop hook bounces
  a final holding it (its fifth rule). The words name the day and time the block first went out and ask for a
  line-by-line re-read against today (relative dates, clock times, who meets whom and when) or one plain line on why
  nothing changed; the room's next send in that result goes out whatever it holds. A block posted again the same day
  never bounces, and a send to another room is compared with that room's posts, where it is filed.
- Each bounce is a `paste` row in the shadow ledger, counted in the morning diff as `stub` and `board` are; the row says
  how many lines the block has and when it first went out, never the block. The card's row for a refused send reads
  not done (a send's result that says `"ok": false` read done before, since Claude Code never marks an MCP answer an error).
- What it costs: a text with no three backticks costs one substring check. The first fenced text of the day in a room
  reads that room's day files of the 13 days before today, off the event loop, and the view is kept until the date
  changes; a restart builds it again from the logs. Replayed over the live day logs of the last 14 days: 62 fenced
  posts by the rooms, 1 turned back, that morning's, naming the post of the evening before.
- A block over about 1,950 characters is posted, and so filed, in parts, so it is compared part by part: one re-sent
  whole is not matched.

**Upgrading:** budgets.toml: mechanics 3,400 to 3,600 for the new line and the session-start total 84,550 to 84,750
(no part could pay, on #88's measurements of the day), [code] lines 17,530; nothing to do.

### voice-note (#88)

- A room answers with a voice note. When the owner asks for one or to say something out loud (a new mechanics line:
  only asked, since each one costs), the room writes the script, calls the new `voice_note` tool on Yerk's own MCP
  server (the daemon runs it, since a room's shell has no network) and `send`s the file with no text, and it lands as a
  Discord voice message: flag 8192, its duration and waveform, the file posted as audio/ogg through the bot's own HTTP
  client (discord.py posts a file as application/octet-stream, and Discord reads the voice fields only beside an audio
  type). Words for the room go in a message of their own after it. When Discord refuses the voice form, or takes it
  without the flag, the note goes as the plain audio file and the send's result says why, so the room says so; a note a
  crash cut short goes again as the plain file from its snapshot.
- The tool is the box's tools/voice-note recipe: `[[pause]]` alone on a line between sections (each its own call,
  joined by 0.5 s of silence; one over 4,000 characters split again at its paragraphs; one under 50 characters joined to
  the next with a `(break)`, since a voice glues a stray syllable to a short section), `(break)` a short pause, `[mood]`
  tags dropped for MiniMax and passed to Fish. MiniMax speech-2.8-hd first (English_FriendlyPerson, speed 1.18, pitch
  -1, language_boost English, the private folder's optional voice/pronounce.txt as its pronunciation rules); on any
  MiniMax failure the whole note again on Fish Audio s2.1-pro-free, so a note never changes voice mid-way. Then
  loudnorm I=-14:TP=-1.0:LRA=11 and opus 48 kHz mono in ogg under the room's work/voice/, beside a NAME.voice.json
  (duration, waveform, size): `send` takes the file as a voice note only while it is unchanged.
- A note is checked before it is handed back: its first 20 seconds are read back by whisper-1 (transcribe's pass-1
  call), and one whose words do not line up with the script's opening (fewer than 3 in 4), or with a word glued in
  between two matching runs, is deleted and the room told. English only, and its explain lines say so.
- Every paid call is a `spend` row at list price (MiniMax US$100 a million characters, by its own billed count; Fish
  s2.1-pro-free US$0; whisper-1 for the check; read 2026-10-22), so the morning diff counts it, and a note estimated
  past US$1 (about 10,000 characters) is refused until the owner says yes (decision 32).
- Measured once, live, on the real keys and outside any room: a two-sentence note of 165 characters on MiniMax, 6.9 s
  of opus, read back word for word, 1.72 US cents in two spend rows, 5.4 s end to end. Then once into a test room
  through `send`: Discord took the voice form, and the message read back from its API carries flag 8192, no content and
  one audio/ogg attachment with its duration and waveform.
- `yerk abilities` and `yerk explain voice_note`: the described voice-notes entry is the built voice_note, tier paid,
  on when a voice key and the OpenAI key are there.
- The evidence rules count a `voice_note` call as no read, as they count `send`: a claim about a person in a turn
  whose only calls made a note is still ledgered (cold read).

**Upgrading:** put MiniMax's key on the first line of `secrets/minimax.key` and Fish Audio's on the first line of
`secrets/fish.key` in the private folder (0600; either one makes notes, MiniMax first); the check reads the OpenAI key
the embeddings read. ffmpeg must be on the daemon's PATH. Optional: MiniMax pronunciation rules in `voice/pronounce.txt`
there, one `word/say-it` a line. budgets.toml: mechanics 3,200 to 3,400 for the new line and the session-start total
84,350 to 84,550 (no part could pay: today the largest persona measured 11,704, room voice 5,392, pinned memory
3,854), [code] lines 17,412; nothing to do. Each room's settings allow `mcp__yerk__voice_note` from its next process
start.

### transcribe (#77)

- A room hears a voice note. A Discord voice message or another audio file (ogg, m4a, mp3, wav, webm) arrives with a
  line under its `[attachment]` line: the room takes in the sound only through the new `transcribe` tool, so it calls
  the tool on the saved path before it answers, opens its reply with what the note says or that it could not be
  transcribed, and calls that step transcribing, never listening. A turn that ends without calling it bounces once, as
  an unopened image does (a Read of the file does not count), and the card's row reads "transcribing voice-message".
- `transcribe`, on Yerk's own MCP server in every room (the daemon runs it, since a room's shell has no network):
  ffmpeg cuts the audio into 14-minute mono mp3 chunks; pass 1 is OpenAI whisper-1 on each chunk in parallel, a segment
  both near silence and low in confidence dropped (no_speech_prob over 0.9 and avg_logprob under -1.0); with a Groq
  key, pass 2 is Groq whisper-large-v3-turbo on the same chunks, the two aligned word by word, and a segment where they
  differ on more than 3 words in 10 (fillers such as "um" aside) is marked `(uncertain)` with both readings. The text,
  a timestamp per segment, is returned and saved beside the audio as NAME.transcript.txt (0600), its first line saying
  whether a second pass checked it. Each room's entry for Yerk's server carries a 10-minute tool timeout: without one,
  Claude Code drops an HTTP tool call at 60 s, and a long recording's call runs past that.
- Every paid call is a row in the ledger's `spend` table at list price (US$0.006 a minute on OpenAI; US$0.04 an hour
  on Groq, 10 s billed at least; both read 2026-10-21), and the morning diff says "spent yesterday: US$X on N calls";
  the table, its reader and the morning line are new here (#76 carries the same ones). A recording estimated past US$1
  is refused until the owner says yes (decision 32).
- Measured on a 6:39 voice note: 16.1 s end to end (whisper-1 15.3 s, Groq 1.3 s, one chunk), 99 segments, 6 marked
  uncertain, US$0.0443 in two spend rows. A wait that long is why the room calls the tool instead of the daemon
  transcribing before the turn starts: the turn would start 16 s late with no card to show for it.
- `yerk abilities` and `yerk explain transcribe`: transcribe is built, on when the OpenAI key is there; a built
  ability's `how` lines are headed "How it works".

**Upgrading:** for the second pass, put Groq's key on the first line of `secrets/groq.key` in the private folder
(0600); the OpenAI key is the one the embeddings already read (`secrets/openai.key`, or where `[memory] embeddings_key`
points). ffmpeg must be on the daemon's PATH. The ledger gains its `spend` table at the next start, and each room's
settings allow `mcp__yerk__transcribe` from its next process start.

### test-hygiene (#86)

- The scrub test passes after `ruff check .`: it skips the folders a run leaves in a checkout (`.ruff_cache`,
  `.hypothesis`, `.tox`, `venv`, `node_modules`), where ruff's cache held absolute home paths. Lint and test in any order.
- With a private denylist set (`YERK_DENYLIST_FILE`), the tree passes it: the test fixtures, docstrings and comments that
  named the author's rooms, people and work tickets use example values now. A line that starts with `Copyright (c) YYYY`
  is the one line the denylist skips, since the license names its holder. Nothing changes for an install.

### recall-warm (#80)

- `yerk-index` keeps the pages the keyword rank reads (the FTS tables, the `chunks` rows it joins, `meta`) mapped into
  itself: at start, within 5 s of a writer pass that changed chunks, every 10 minutes and after a rebuild. A box that
  drops its page cache on a clock never drops a mapped page, so recall, which reads the index file itself as before,
  finds them in RAM. On the author's box, where a trimmer drops the cache every 2 minutes, recall's keyword read on a
  pinned copy of the live index took a median of 0.116 s (0.173 at most, 0.116 right after a drop) against 0.494 s
  (3.604 right after the drop) on the live file unpinned, at the same moments. The meaning search is unchanged.
- The pages are mapped, never locked: real memory pressure can still take one, and the next pass maps it again. A
  connection's own page cache cannot do this job: SQLite drops it at every commit of the writer.
- **Upgrading:** `units/yerk-index.service` MemoryHigh rises from 800M to 1400M: the rule adds the mapped pages
  (index.json's `pin`, 394M at 314,010 chunks), and `yerk doctor` counts them. A box that set MemoryHigh with
  `systemctl --user set-property` raises that instead, before the restart that brings this (the author's box: 1G to
  1536M); below it the kernel takes mapped pages back and part of recall reads cold again. budgets.toml changed
  ([code] lines, up 165).

### figure-noise (#85)

- The figure shadow rule counts a line on file as a figure's source only when it is about the same thing, not when it
  merely shares the digits: the label words beside the number in the reply (Z8, girth, worksheet) sit beside it on
  the line too (two of them, one holding a digit, or the reply's only one), the unit agrees (1.4 ft is not 1.4 s,
  0.11 ft is not 0.11%), the line is not the reply's own record (its day-log line, or another room quoting it), and a
  dollar figure is looked up with its dollar sign (the bare digits used to find only lines where the number was not
  dollars). A read of the same number with or without thousands separators carries it. A figure with no label word
  beside it, in the reply or on the line, is matched by its digits as before. Replayed over the owner's 68 complaint
  cases and 74 accepted replies: false alarms on accepted replies 13 to 6 of 74 (0.176 to 0.081), wrong-fact hits 4
  to 1 (none of the 4 was the wrong fact); every other rule's rows unchanged.
- **Upgrading:** budgets.toml changed ([code] lines, up 90 for the same-thing test). Nothing to do.

### people-card (#84)

- A room with people (`people` in rooms.toml) gets its own turn card: the room's newest holding line (the text it sent
  itself), else "working on it", and the step count, as "-# step 12"; never a step's words, the earlier-steps line or
  the model's narration. An owner room's card is byte for byte what it was (a test renders main's card of one long turn).
- The board rule, the Stop hook's fourth, in a room with people only: a final shaped as the owner rooms' status board
  (a line opening on ☑, ☐, ⛔, 🟦 or 🟨, the colour key, "unblocks:") bounces once for two or three plain sentences, a
  `board` row in the shadow ledger the morning diff counts. An owner room's board never bounces.
- A room that is not the owner's adds 3 notes per 5-minute window, not 1, so a reminder, its follow-up check and a watch
  go in one turn: on 2026-10-21 one per window refused a person's second note twice and the room put it off to
  the person's next message. 20 notes at most, as before.

**Upgrading**: yerkd.toml `[jobs] room_note_burst` is new and optional (default 3; 1 keeps one note per window).
budgets.toml `[code]` rises by 56. Nothing to do.

### midnight-test (#83)

- Two deploy tests that read the morning diff a minute after their rows now pin the clock to noon, so a suite that
  crosses midnight no longer turns the box CI red (PR 79, Oct 21); nothing a person running Yerk AI notices changes.

### person-claim-rule (#81)

- Two more shadow rules in the Stop check, beside figure, file, decide, the claims, stance and sheet. `person`: a
  sentence telling what someone said, wants, knows, decided or asked, or where they were (the owner, "you", counts),
  from a turn that made no call that reads (any call but a write, a send or the room's other non-reading tools; the
  prompt hook's recall block is not a call), ledgered with the name, the sentence and the tools the turn ran. `quote`:
  a quoted span of three or more words its lead-in gives as someone's or a document's words (said, wrote, the note
  says, her message, the email) that no read, prompt or earlier reply of the session carries, ledgered with the span
  and its lead-in. A question, a hedge or a proposal is no claim. The morning diff counts both ("shadow flag person:
  N", "shadow flag quote: N"); nothing is blocked. Replayed over the owner's complaint cases (68, four of them
  re-pointed or marked) and 74 accepted replies: together they fire on 10 of the 21 wrong-fact replies (5 on the
  wrong line itself) and on 5 of the 74 accepted replies (0.068); the older rules' rows are unchanged.
- **Upgrading:** budgets.toml changed ([code] lines, up 65 for the two rules and their call). Nothing to do.

## 2026-10-21

### taste-judge (#79)

- The taste eval, phase 1: `evals/taste/run.py` scores a judge model against the owner's own complaints about a room's
  replies, kept as a private case file outside the repository (the cases, at least as many accepted replies as controls,
  a rubric of the kinds in his words), one `claude -p` call per row with hooks, settings and tools off and the answer
  forced to JSON; precision, recall and the false-alarm rate on a held-out half, one table, `scores.json` with the cost
  per call as the binary reports it. The first run, the owner's three rooms Oct 19 to 21: 68 cases and 74 controls;
  held out, precision 0.71, recall 0.15, false alarms 0.054, about 6 s and 7,600 tokens a call at medium effort. Under
  the bar on false alarms, short of it on cases (100), and far short on recall. Nothing in the daemon changes; mypy now
  covers `evals/`.

### taste-ledger (#78)

- An owner room keeps the owner's thumbs on its replies in the format an earlier setup used, in `taste.jsonl` in the room's
  layer: a message of the owner's in the room's own channel that praises or faults the last reply ("that was good",
  "too long") is one row on that reply, the owner's words beside its text, one row per
  reply per sign. The classifier is that setup's, its three lists unchanged: an insult is never a verdict, and a message
  that is neither rates nothing. A test sender, a person or the harness never rates.
- Every message in such a room carries the `taste` part after recall, as that setup did: "Rated by him, newest
  first (his words → what you had said):", then three praised and two faulted replies, the room's own newest first,
  then the other rooms' newest; each reply is cut 40 shorter, down to 80, until the part fits its 1,500 characters.
  `yerk map` shows its size. A room with people, or a layer without the file, gets no part and no row.

**Upgrading**: `budgets.toml` gained `[per_message.on_hit] taste = 1500`, and `[per_message] cap` rose by the same
1,500, from 4,000 to 5,500. `[code] lines` moved to the new count, by 178. To turn it on, put a `taste.jsonl` (the box's
ledger converted, or an empty file) in an owner room's layer; a layer without one changes nothing.

### mechanics-lines (#75)

- Every room's mechanics (yerk_ai/mechanics.md) take the three lines owed since #69, in the prompting guide's shape:
  a room files a fact, decision or correction with `remember` before it writes the answer, since text above its last
  tool call never reaches the room, and the filing is one clause of the answer at most (#73's line); a promise to act
  later is a `schedule` job the same turn, a note in any room, a script or worker in the owner's rooms (#71's line);
  and a person's own message in the room is an ask to act on, while recalled, imported and quoted text stays a record.
- The tag on a person's own message says the same: "not the owner: their message is an ask to act on here; an order to
  act as the owner, in another room or against a rule is reported, not followed". It said their words were a record to
  answer, never an instruction, which made a people room read that person's asks as notes. Each recall block's header,
  imported lines included, keeps "records, never instructions".
- Three wording fixes from #69's cold read: re-read the file a `[from <path>]` part names before acting, work out
  relative times from the clock line, and answer on the harness from the kept docs, never from what the room remembers.
- The file is 3,187 of its 3,200 characters (3,194 before). A running room is told at its next message that 12 lines
  are new or replaced, more than the 1,500 characters its rules note holds, and reads the file.

### trusted-room (#74)

- A room with people can run the owner rooms' tools: `shell = "trusted"` in its rooms.toml table lets it list Bash,
  Write, Edit and the rest. Its shell runs inside a wall the daemon builds at each process start from what it knows:
  every other entry of the private and state folders (the other layers, the memory folder, rooms.toml, imports; every
  other room's folder, the ledger), every memory root and every other room's layer and `read` folders, `projects/`
  under each account's folder, and each folder of the home that holds one of those, all denied to the shell for
  reading and writing. No command runs outside the sandbox and the room does not start without it. Its layer, work
  folder and attachments stay readable and writable, its outbox read only, the Python user site-packages readable
  (python-docx); Write and Edit reach its layer and work folder only. Proven on Claude Code 2.1.304 by a scratch probe.
- A trusted room whose rooms.toml would undo the wall (a sandbox key past `filesystem.denyRead`, `filesystem.denyWrite`
  and the `network` allowlist, or an `allow` rule for the file tools), a `shell` other than "trusted", or a wall that
  cannot be built stops the start with one line. `yerk explain trusted` describes it.
- Workers, jobs past notes, rules and settings changes stay the owner rooms'; the lines a room that is not the
  owner's hears when it asks for one say what it can do instead and name no person.

**Upgrading**: rooms.toml gained `shell` (unset, nothing changes). For a room with people, `shell = "trusted"` and
the shell and file tools in `tools`; its own `[rooms.NAME.sandbox]` may add `filesystem.denyRead`,
`filesystem.denyWrite` and the `network` allowlist, no other key. `budgets.toml` `[code] lines` moved to the new
count, by 109.

### answer-first (#73)

- A room's answer comes first and whole: every `remember` result ends with `next`, the shape of the reply that follows
  the call (the result, the numbers, what was found, then the filing in one clause at most), since only the text after
  the last tool call reaches the room. A rule's result no longer carries the canned line that went out as a whole reply
  that day; the clause it asks for ("every room now does X this way") rides inside `next`.
- A final message that only reports a filing bounces once (`stub`): the result's last call is `remember`, `schedule`,
  `hold` or `settings`, and the answer sits above that call (a text block over 200 characters and at least twice the
  final's length) or was never written (a final under 200 characters opening "Filed", "Saved" or "From now on every
  room"). The room is told to write the whole answer as its final message; each bounce is a `stub` row in the shadow
  ledger, counted in the morning diff. Replayed over the 593 results in the live rooms' stream logs of the last 7 days:
  3 bounces, all three answers that never reached the room (all on 2026-10-21). "Noted" is not an
  opener word: it bounced a whole answer that day.
- A rule that takes the shared rules file past its budget is filed all the same and said: its result carries
  `over_budget` (size and cap), the room ends its reply with a clause saying the file is over its cap, and the machine
  room (else `[jobs] ops_room`) gets one line naming the file, its size, the cap and the lines every session start now
  cuts.
- **Upgrading:** budgets.toml `[code] lines` moved to the new count. Nothing to do.

### idle-compact (#72)

- A room compacts between turns, never inside one: idle 25 minutes with its session at 0.7 of its compaction window
  or more, the daemon compacts it in the background as `yerk room compact` does, so the next message finds it small
  (on Oct 21 a one-line question waited 5 min 20 s for Claude Code's own compaction). `budgets.toml` `[session]`
  `idle_compact_at` and `idle_compact_after` set the line, a room's own keys in rooms.toml move it, and
  `idle_compact_at = 0` turns it off.
- A message sent during `yerk room compact` runs right after it; it waited for the next message before.
- Each turn's ledger row holds the session's size in tokens (`context`).
- A compaction's own row (this one or `yerk room compact`) has the outcome `compacted`, so `yerk status --json` never
  lists one among the day's failed turns (its empty answer read as a failed turn before).

**Upgrading**: `budgets.toml` gained `[session] idle_compact_at` and `idle_compact_after`; the ledger adds its column
at the next start. Nothing to do.

### note-jobs (#71)

- Every room can set reminders: a fourth job kind, `note` (`room`, `message`, one of `at`, `every` or `schedule`
  with `tz`, `deliver`). When it is due the room's own session is told `[reminder NAME due]` and the message and
  says it in its own voice; no worker, shell, account or cap. A one-shot note leaves the jobs file after it fires.
- A note that fires while yerkd is down, or whose turn a restart cuts, is told at the next start, and never twice
  when its reply was already in the room.
- A room that is not the owner's adds, lists and removes its own notes with the `schedule` tool and sees no other
  job; agent, script and watcher jobs, and any job for another room, stay the owner's rooms' own, and the refusal
  says what the room can do instead. `yerk schedule --kind note` adds one from the command line.
- Such a room keeps 20 notes at most and adds one per 5 minutes, and its `schedule` call with no `kind` sets a
  note (the owner's rooms keep "agent"); a note's message is at most 600 characters in every room and from
  `yerk schedule`, refused, never cut. Each refusal says the cap and when the next fits.
- A held card in a room with `people` shows the call's content in plain words (for a send: the room in its first
  line, the text below), never the tool call's JSON; the exact call stays in the ledger. The owner's rooms see the
  exact call as before.
- **Upgrading:** budgets.toml changed ([code] lines, up 122 for the note kind, its gate, the plain card, the note's
  restart replay and its caps). yerkd.toml takes three optional `[jobs]` keys, listed by `yerk explain jobs`:
  `room_notes` (20), `room_note_minutes` (5) and `note_chars` (600). Nothing to do.

### sheet-rule (#70)

- A sixth shadow rule, `sheet`: a turn that sends a document through the send tool (.docx, .pdf, .xlsx, .pptx, .txt,
  .md or .csv) or pastes a block (a fenced block with no language tag, or tagged text, plain or none) in its reply or
  in a send's text, with no claims sheet written or named in the turn (a Write, Edit or NotebookEdit of a file whose
  name holds claims and ends in .md, .txt or .csv, or a shell command naming one), is ledgered once as `sheet` and the
  morning diff counts it ("shadow flag sheet: N"). Nothing is blocked: it measures how often a room ships something
  in the owner's name without checking its claims against source first. `yerk explain sheet` describes it.
- **Upgrading:** budgets.toml changed ([code] lines, up 50 for the rule and its call). Nothing to do.

### harness-expert (#69)

- `yerk docs refresh | list | show NAME | shape [ROOM] [--rules]`: the Claude docs Yerk runs on (the prompting guide,
  each model's prompting page, the Claude Code pages on settings, hooks, memory, sandboxing, MCP, the CLI and the
  system prompt) are fetched as Markdown into `<state>/docs/claude/` and kept as a memory root of class shared, so a
  room's own recall and read_memory reach them; they never ride in what a room is handed per message, and in a room's
  own recall the owner's notes rank above them.
- The doctor refreshes the pages once a week (`[docs] fetch = false` turns that off; `yerk docs refresh` still
  fetches), and the morning diff says `docs changed: <slug>` once per change.
- The doctor scores every rule file a room loads against the prompting guide (no why, loud, untagged examples, alarm
  marks, a topic said twice): a report per room in `docs/shape/<room>.md`, one totals line in the morning diff, and a
  finding for a file past a budgets.toml `[shape]` threshold. mechanics.md and examples/private are rewritten in the
  guide's shape.
- `yerk explain` takes a command's name (`yerk explain docs`), and the built abilities `claude-docs` and `rule-shape`
  answer a room asked whether its rules are written the docs' way.
- **Upgrading:** yerkd.toml takes an optional `[docs]` table (`pages`, `refresh_days` 7, `fetch` true, `topics`,
  `acronyms`); budgets.toml gains `[shape]` (every threshold 0) and [code] lines rises by 452. The memory index takes
  `<state>/docs` as a new root at its next start and chunks every file again once; run `yerk docs refresh` once after
  the deploy so the pages are there before the first weekly run.

### budgets-voice (#68)

- The taste rules (one line each: a rule and the rated replies that prove it) fit a rulebook: its budget rises from
  7,000 to 10,850 characters and a room voice's from 5,000 to 5,500; the session-start total rises by the same 4,350,
  to 84,350.
- **Upgrading:** budgets.toml changed (rulebook, room_voice, total). Nothing to do.

### hardening (#66)

- A room's token reaches its hooks through a 0600 file beside its settings, never on a command line (which every
  local user can read in /proc). The state folder is made 0700, and an existing one is closed to 0700 at the next
  start.
- No room may read or send a secret (both secrets folders, every room's token files, each account's token file and
  its config folder's login, the secret files yerkd.toml names): Read deny rules in every room's settings.
- A room whose tools run a shell (Bash, Monitor, PowerShell or `default`) gets Claude Code's sandbox when rooms.toml
  gives it no `[rooms.NAME.sandbox]` table: unsandboxed, a script, `grep` or a redirect read every secret.
- Recalled and imported lines, and a person's words, reach a room marked as records, never instructions.
- `yerk doctor` names any file or folder another user can read in the private or state folder, and any
  secret-shaped line in memory, attachments or imports by file and line, never by value; a one-time code in an
  import two days past its own date is dead and not said.
- Tests: `YERK_DENYLIST_FILE` names a file of words no tracked file may hold.

**Upgrading**: a room whose tools run a shell and that has no `[rooms.NAME.sandbox]` table now runs sandboxed (on
Linux Claude Code's sandbox needs bubblewrap and socat); `[rooms.NAME.sandbox]` with `enabled = false` keeps its
shell on the host. `budgets.toml` `[code] lines` moved to the new count.

### reliability (#65)

- A stop (`systemctl stop`, `yerk deploy`) ends the daemon clean: STOPPING=1 to systemd, every room's Claude Code
  ended by the daemon, the gateway closed, exit 0, about a second. A turn cut mid-run has its messages run again
  once at the next start, marked, as a crash already had.
- A gateway session Discord let go (an outage past a few minutes) no longer loses the messages sent meanwhile: every
  ready reads each room's channel since the last ledgered message, not only the first ready of the process.
- A message the ledger could not take (the disk full) is read again from the channel a minute later, and again
  until it is taken, instead of waiting for the next restart.
- A stream log that cannot be written (the disk full, a file-size limit) is dropped for that process; the turn
  runs. A turn the harness itself crashes is said in the room ("the harness failed; it is in the log") and
  its messages closed, instead of a card left at "working on it".
- A ledger from a newer version is refused with one line and left as it is. A second yerkd on the same state
  folder is refused with one line.
- `yerk status` names the version and the commit that runs; `yerk status --json` prints one document for a
  monitoring tool.
- CI: `ruff check .` (pyflakes and the syntax-level pycodestyle rules, the config in pyproject.toml), pip-audit
  over uv.lock (fails on a known vulnerability; certifi moved to 2026.10.19, the first run's one finding), and
  property tests with hypothesis for the day-log line grammar.

**Upgrading**: copy `units/yerkd.service` and `units/yerk-index.service` again (KillMode, TimeoutStopSec,
NoNewPrivileges, PrivateTmp) and `systemctl --user daemon-reload`; `budgets.toml` `[code] lines` moved to the new
count; the dev group gained hypothesis, ruff and pip-audit (`pip install -e . pytest mypy hypothesis ruff pip-audit`).

### curate (#63)

- `yerk curate`, nightly at 05:00 after the doctor: a sweep in code blanks a card line said twice, a superseded line
  and a line past its `[until YYYY-MM-DD]`, and moves a cite only when its quoted words sit on exactly one line; any
  other drift is a finding in the morning diff, each card backed up first. Then a worker proposes lines from the
  day's logs for each card, a second knocks them down, code checks each one, and they land only when the recall
  bench does not fall. A correction is only proposed, never applied. `--dry-run` shows the night, `--since DATE`
  runs a range as a backfill.

**Upgrading**: copy `units/yerk-curate.service` and `units/yerk-curate.timer` and enable the timer; `[curate]
account` in yerkd.toml pins the account it runs on, and `[curate] logs` adds logs of the owner's own.

### compact-window (#64)

- A room compacts where Yerk AI says, never at the model's window edge: `budgets.toml` `[session] compact_window`
  (250,000 tokens) is written as every room's `autoCompactWindow`; a room's own `compact_window` in rooms.toml
  overrides it (kept between 100,000 and 1,000,000), and 0 leaves it to the model.

**Upgrading**: `budgets.toml` gained `[session] compact_window`; each room takes it at its next start.

### import-sources (#62)

- `yerk import imessage:<chat.db> [--contacts <db>]` and `yerk import gmail`: a person's texts and mail come in as
  files, `imports/texts/<person or group>.md` and `imports/mail/<YYYY-MM>.md`, one line a message, both sources
  only read. Anything shaped like a key, a token, a password or a one-time code is written as its kind only.

**Upgrading**: for mail, `[gmail] user` and `password_file` (an app password) in yerkd.toml; `[import] me` names
your own texts.

### rail-2 (#59)

- Recall per message reads up to 2,000 characters when it finds something (`[per_message.on_hit]`, the cold read's
  measurement with the index healthy); the recall hook stays inside its 10 s; a keyword search that did not finish no
  longer says nothing was found, and when the best 8 chunks hold nothing, 20 are read once before it says so.

### keys (#60), index-health (#61)

- keys: the one-time codes and the private layer's key files (see the pull request).
- index-health: yerk-index's memory ceiling moved to 800M with a rule (256M plus 8.3M per block of 16,384 vectors;
  `yerk doctor` says when the vector count passes it), the doctor reads the index.

**Upgrading**: copy `units/yerk-index.service` again (MemoryHigh=800M) and `systemctl --user daemon-reload`.

### import-discord (#58), doctor (#55)

- `yerk import discord:<channel or thread id>`: a chat's whole history into the day logs, no room made; a chat
  imported before its room never reaches `yerk room add`.
- `yerk doctor`, daily at 04:40: the private folder's shape, rooms.toml keys against the loaders, memory classes
  and day-log lines, token expiry, the index, each running room's hooks, disk growth, and the crash repair of NUL
  lines in day logs, assembled logs and bound transcripts (each file backed up first). A finding exits 1 and the
  morning diff says it; yerkd's start runs the repair before anything reads a log.

**Upgrading**: copy `units/yerk-doctor.service` and `units/yerk-doctor.timer` and enable the timer.

## 2026-10-20

- name-keys (#54), shrink-imessage (#57), stance (#52), web-research (#53), owner-cmds (#51), nothing-dropped (#50),
  land3 (#49), gates (#47), silence-cap (#48), people-recall (#46), limit-rest (#45), room-account (#44),
  room-people (#43): see each pull request; this file starts with the merges of 2026-10-21.
