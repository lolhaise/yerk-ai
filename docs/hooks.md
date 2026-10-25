# Hooks

Written by `yerk docs own` from yerk_ai/hooks.py (EVENTS, settings_for); change those and run it again, never this page.

The hooks block of a room's --settings file (written 0600): every event a command hook, curl posting the event's JSON to the daemon with the process's token as its secret header, read from `header` (the 0600 file the daemon writes beside it), never on curl's command line, which /proc shows every local user while a hook runs. Never `type: http`: Claude Code 2.1.301 sends those through a sandboxed room's network proxy, which refuses the daemon's loopback port with 403 (2026-10-19, 389 silent refusals in two rooms). Resume is out of SessionStart's matcher: no reset.

## UserPromptSubmit

Timeout 10 s. It adds the per-message parts: the clock line, the metadata line, in an owner room where the owner's phone last was (location.py, capped at `place`; never on a person's message), the rules changed since the session last looked (context.news) and the topic rules the message names (context.topics), together capped at `rules`, and the recall part (render_recall: what is not on file, the owner's people and things the message names with their best lines, the best hits of a question about the owner's past, then this room's lines of the last 14 days on its topic, capped at `recall`), then in an owner room whose layer keeps taste.jsonl the owner's rated replies (taste.py, capped at `taste`); every assembly goes to the room's assembled.jsonl, and a message that is the approval word from an approver approves the room's held action.

## Stop

Timeout 10 s. It runs the rule checks, no judge, each bouncing the turn once: an image never read, a voice note never transcribed or a picture never filed; a turn ending with nothing visible; a final text that only reports a filing; a status board in a room with people; a fenced block posted on an earlier day; the sheet rule; the person and quote rules. The evidence rules after a reply only log (shadow rows).

## PreToolUse

Timeout 10 s. It runs the gates in order (gates.py): hardline, trash (a delete rewritten into the trash), restart, approval.

## PostToolUse

Timeout 10 s. It times the call into the tool_times ledger.

## PostToolUseFailure

Timeout 10 s. It times the call, and a rewritten delete whose trash command failed is ledgered with one line for the room.

## ConfigChange

Timeout 10 s. It writes the tamper row (who, what, when, the hashes before and after), never blocks.

## SessionStart

Timeout 15 s, on startup, clear, compact. It re-adds the recall pack after a `compact` and lets the topic rules ride again; after a `startup` or `clear` that replaced a session which had answered something, it posts the reset line.
