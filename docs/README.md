# docs

The long guide and the reference pages. Six of the pages are written by the program from the code (`yerk docs own`), and a test fails one that is out of date, so they are never edited by hand; the other three are written by hand.

## Written by hand

- `HOW-IT-WORKS.md`: who is who, what happens to one message from send to reply, and the project's words in plain ones. Read it after the front page.
- `GUIDE.md`: the long guide: using Yerk AI, running it as a service, and how each part works inside.
- `STATUS.md`: a record of how Yerk AI was built, stage by stage; it is not checked against today's code.

## Written from the code

- `budgets.md`: every size limit Yerk AI enforces and why, from budgets.toml.
- `commands.md`: every command and flag of `yerk`, `yerkd` and `yerk-index`.
- `hooks.md`: what Yerk AI does on each Claude Code event.
- `settings.md`: every key of rooms.toml, yerkd.toml and jobs.toml, with its default.
- `tools.md`: the tools a room's model is given, in the words the model reads.
- `units.md`: each systemd unit Yerk AI ships and when it runs.
