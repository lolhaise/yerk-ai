# Hooks

Written by `yerk docs own` from yerk_ai/hooks.py (EVENTS, settings_for); change those and run it again, never this page.

The hooks block of a room's --settings file (written 0600): every event a command hook, curl posting the event's JSON to the daemon with the process's token as its secret header, read from `header` (the 0600 file the daemon writes beside it), never on curl's command line, which /proc shows every local user while a hook runs. Never `type: http`: Claude Code 2.1.301 sends those through a sandboxed room's network proxy, which refuses the daemon's loopback port with 403 (2026-10-19, 389 silent refusals in two rooms). Resume is out of SessionStart's matcher: no reset. PreToolUse is the gate, and a gate that cannot be asked refuses: Claude Code carries an action out when its hook fails unless the hook blocks, and on that day the 170 gate calls among those refusals each let its action through. So the gate's curl gives up at 8 s, inside the hook's 10, and any failure of it exits 2 with GATE_DOWN, which blocks the call and tells the model why; `onFailure: block` (Claude Code 2.1.295 on) blocks a hook that Claude Code itself had to end. No other event blocks on a failure: a lost message or a turn that cannot end is no gate.

## UserPromptSubmit

Timeout 10 s. It adds the per-message parts: the clock line, the metadata line, in an owner room where the owner's phone last was (location.py, capped at `place`; never on a person's message), in the [texting] report room while texting is dry or live its own block (J29), the rules, persona and voice lines changed since the session last looked (context.news) and the topic rules the message names (context.topics), together capped at `rules`, and the recall part (render_recall: what is not on file, the owner's people and things the message names with their best lines, the best hits of a question about the owner's past, then this room's lines of the last 14 days on its topic, capped at `recall`), then in an owner room whose layer keeps taste.jsonl the owner's rated replies (taste.py, capped at `taste`); every assembly goes to the room's assembled.jsonl, and a message that is the approval word from an approver approves the room's held action. An uncapped install ([limits] uncapped) holds no part to its line: past Claude Code's 10,000 the text is cut at a line and its last line names the file holding it whole.

## Stop

Timeout 10 s. It runs the rule checks, no judge, each bouncing the turn once: an image never read, a voice note never transcribed, a video never watched or a picture never filed; a turn ending with nothing visible; a final text that only reports a filing; a final text that tells back a filing it made (Filed, Logged, the rule restated); a final text opening with a note to itself before the answer; a status board in a room with people; in any other room, a mark the install retired ([gates] retired_marks; none by default); a fenced block posted on an earlier day; the sheet rule; the person and quote rules; a person's message folded into the turn that the final never names. The evidence rules after a reply only log (shadow rows).

## PreToolUse

Timeout 10 s. It runs the gates in order (gates.py): hardline, trash (a delete rewritten into the trash), restart, approval; a call the daemon gave no answer for (down, slow past 8 s, an error) is refused, never run, with one line for the model.

## PostToolUse

Timeout 10 s. It times the call into the tool_times ledger.

## PostToolUseFailure

Timeout 10 s. It times the call, and a rewritten delete whose trash command failed is ledgered with one line for the room.

## ConfigChange

Timeout 10 s. It writes the tamper row (who, what, when, the hashes before and after), never blocks.

## SessionStart

Timeout 15 s, on startup, clear, compact. It re-adds the recall pack after a `compact` and lets the topic rules ride again (uncapped: the pack whole and the topic rules the session start did not carry, each told once); after a `startup` or `clear` that replaced a session which had answered something, it posts the reset line.
