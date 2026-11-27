# examples

Example files to copy or read: a fake private folder, room templates, an example add-on and example write-ups of abilities. Every person, message and setting in them is invented.

- `abilities/`: example write-ups of three abilities and of the question new users ask most, in the shape `yerk explain` answers:
  - `browser.md`: a browser on its own profile that never takes the mouse (described, not built).
  
  - `morning-brief.md`: the morning brief (half built).
  - `text-as-you.md`: texting as you, with a preview and a veto (built).
  - `what-can-you-do.md`: how a room answers "what can you do?" from abilities.toml.
- `addons/`: `hello`, a small add-on with one tool, one unit and one test, to copy when you write your own.
- `private/`: a fake private folder; the tests use it, and you can copy it to start your own:
  - `accounts.toml`: two example Claude Code accounts.
  - `evals/`: invented questions for the recall bench, the weekly test of memory search.
  - `jobs.toml`: scheduled jobs, one of each kind, which `yerk jobs sync` turns into timers.
  - `memory/`: invented memory: three day logs in `daily/`, open items in `open-loops.md`, pinned facts in `pinned.md`.
  - `persona.md`: an invented persona, who the assistant is.
  - `rooms/`: the voice of each example room.
  - `rooms.toml`: the two example rooms, `example-room` and `example-quiet`.
  - `rulebook.md`: an invented rulebook, one-line rules with their why.
- `rooms/`: the templates `yerk room add` uses to move a chat in: `owner.toml` for one of your own chats, `person.toml` for someone else's, walled off from yours.
