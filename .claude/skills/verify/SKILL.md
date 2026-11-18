---
name: verify
description: Prove a change works in the running daemon, not only in its tests. Runs yerkd offline on the repo's fakes (tests/verify_daemon.py), drives the changed feature from outside, and reports the run's own lines as evidence. Use before the last commit of any change to yerk_ai/.
---

# Verify a change on the running daemon, offline

The unit tests swap the daemon's long-lived tasks for ones that return at once, so they pass on a tree where a task never runs. This recipe runs yerkd's own `main()` instead: `Yerkd.run_discord` on a scratch copy of the example layer, every task it starts running, and only the outside world replaced (Claude Code by the fakes, Discord by an in-process gateway, systemd by the worker tests' `Units`, the Mac by an ssh stand-in, no network). About 10 seconds. It never touches the live install: it refuses its folders and picks a free port.

## 1. Run the fixed checks

From the repo root, with the repo's virtual environment:

```
python tests/verify_daemon.py
```

It prints one line per check, state first, and exits 0 only when every check passed. A red line names the step and what it saw: the HTTP answer, the file's content, the last lines of the daemon's journal. The `·` lines name every period the run shortened. Add `--keep` to leave the scratch folder (its path is printed) so you can read the state files after the run.

A task you add to `run_discord` fails the run until it has a check in `CHECKS`, or a reason in `NOT_CHECKED`. A period you shorten goes in `SHORTER`, so the output names it.

## 2. Drive the one feature you changed

Write a drive file in your own scratch folder, never in the tree. Its `drive(run)` runs against the same scratch daemon after the fixed checks and before the stop:

```python
def drive(run):
    ch, before = run.message("example-room", "words that reach your change", 5000000101)    # the owner, in a room
    got = run.wait(lambda: [m for m in ch.sent[before:] if "echo:" in ch.text(m.id)], 10)
    run.say(bool(got), "my change: the room's reply", ch.text(got[0].id) if got else "no reply")
    it = run.slash("example-room", "status")                    # a slash command: it.said has each answer and edit
    w = run.worker("example-room", 'FAKE ticks=1 report="done"\nOne step.\n', ["one step"])   # through /control
    rows = run.ledger("SELECT * FROM turns ORDER BY id DESC LIMIT 3")                        # the ledger, read only
    kept = (run.state / "usage.json").read_text()                                            # any state file
```

Then run `python tests/verify_daemon.py --drive <your drive file>`. A fake's own variations (`tests/fake_claude.py`'s docstring: `SLOW`, `HANG`, `FIXTURE:<name>` and the rest) go in the message's words. `run.say(ok, what, saw)` prints your line in the same shape as the fixed ones.

## 3. What goes in the report

Paste the run's own lines as they printed: the fixed checks, your drive's lines, and the last line (`🟢 all N checks passed in S s`), with the commit they ran on. A red line goes in as printed, with what you did about it. Never a summary in your own words in place of the lines.
