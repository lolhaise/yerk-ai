# What a user sees

Every card, notice, reply line and status line Yerk AI puts in front of a user follows these rules. It is for you if you change anything Yerk AI shows a user: each of the nine numbered rules below says what to do and gives a good and a bad example, and the last section lists the few choices a user can change. The words come from one file, `yerk_ai/words.py`: the state marks, the state words, the verbs on a card's rows and the time phrases. Every renderer takes them from there. `tests/test_style.py` renders every surface below and fails the build when one breaks a rule here, or when a renderer spells a state mark or a state phrase itself.

The surfaces: the turn card (`yerk_ai/card.py`), the worker card and the relay line (`yerk_ai/workers.py`, `room.fit`), every notice in the adapter (`yerk_ai/channels/discord.py`) and the daemon, `yerk status`, `yerk abilities`, `yerk explain`.

## 1. State first

The outcome is in the first five words, the colour before the words: 🟢 done, 🟠 running or partly, 🔴 not done, ⚪ nothing to do (or known, not built). Colour only where it means something: a step not reached yet gets a plain `·`.

- Good: `🔴 worker died: it ended with an error`
- Bad: `Worker 4f2a91 has finished its run, and unfortunately the result was a failure`

The room's `/status` card (`status.room_card`) takes the box card's shape: each of its lines opens with its own icon (🧠, 📚, ⚙️, ⛽, 🔨, ⏱, 💵) instead of a state mark or a dash, and the red mark appears only on trouble.

## 2. One look, no second read

One short line per item. A list for more than three facts, prose for one idea.

- Good: three lines, `🟢 first tick`, `🟠 second tick`, `· third tick`
- Bad: `Progress: first tick (done), second tick (in progress), third tick (waiting), 1 of 3 complete`

## 3. Phone width

A card line is about 40 characters, where a phone wraps it (`words.PHONE`). A notice is a short state line, then its reason on the next line. A reply is never a wall: a blank line between distinct thoughts.

- Good: `🟠 worker running, 1 of 3 done`
- Bad: `⏳ worker 4f2a91 running, started at 12:54, cap 10 min, 1 of 3 TASKS.md rows ticked, account one`

## 4. No raw paths and no exit codes in a room

A report path becomes "report saved" with the command that opens it. A worker's end is said in plain words: finished clean, stopped: timed out at 15 min, stopped by the owner, died: it ran out of memory, lost: it ended with no record. A path the owner typed is quoted back as its file name. A systemd or Python word never reaches a room, and neither does a status code from the chat service. A step on the turn card says what it was for, the way a person would say it: a command's own description, "searching your notes for EX-901", "reading the Oct 6 notes". Never the command, the pattern or the path. Two or more failed steps in a row read as one: "that did not work, trying another way".

- Good: `-# report saved: yerk worker report 4f2a91` (the command in code marks)
- Bad: `-# report: ~/.local/state/yerk/workers/4f2a91/REPORT.md` and `-# unit: success, exited 0`

## 5. Relative times

"3 min of 15", "started 2 min ago", "done in 3 min". A clock time only where a person would say one ("at 9:00 tomorrow", a meter that resets at 11:40p). Never a raw timestamp or an ISO string.

- Good: `-# worker 4f2a91, 3 min of 15`
- Bad: `started 2026-10-18T16:54:26Z`, or a chat service's timestamp code that only one client renders

## 6. The account line only when there is more than one account

- Good, on an install with one account: `-# worker 4f2a91, 3 min of 15`
- Bad, on the same install: `-# worker 4f2a91, 3 min of 15, account one`

## 7. The same words in every room

One vocabulary in code for the states, the verbs and the time phrases, so a card in one room reads like a card in any other, and the console shows the same characters a chat room does.

- Good: `🟢 reading the pinned notes`, `🟠 searching your notes for Nora`, `🟢 reading docs.example.org` in every room
- Bad: `✅ read pinned.md` in one room and `done: Read(pinned.md)` in another

## 8. The detail lives behind one command

`yerk worker report <id>` has the report, its path, the folder, the unit and how it exited; `yerk status --full` has the pid, the unit names and how old each meter reading is. None of it goes on a card.

- Good: a card that ends `-# report saved: yerk worker report 4f2a91`
- Bad: a card footer listing the folder, the unit name and the exit status

## 9. No em dashes

Plain punctuation: commas, colons, semicolons, full stops.

- Good: `🔴 not sent: big.zip`, then `it is 25 MB and this room takes 20 MB`
- Bad: the same two facts joined by an em dash (U+2014)

## The choices a user can change

The presentation choices are named settings under `[presentation]` in the private `yerkd.toml`, each described in plain words in `yerk_ai/words.py`, so `yerk config get presentation` prints them from the code and they cannot drift:

- `footer`: `minimal` (how long it ran and the command that opens the report) or `full` (also the account and the model)
- `paths`: `never` or `owner` (a worker card in a room whose privacy is owner also shows the report's path; the relay never does)
- `times`: `relative` or `clock` (the time of day)

Asking for one in plain words in a room, and the one-line before and after, comes with the config gate (Layer 6).
