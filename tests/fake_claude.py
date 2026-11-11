#!/usr/bin/env python3
"""A stand-in for Claude Code's `claude -p` in stream-json mode, for offline engine, room and health tests.

Each user line gets: a replay (with --replay-user-messages), one text delta, an assistant
message and a success result echoing the text. Variations:
- a line containing FAIL_RATE, or any line when CLAUDE_CONFIG_DIR is in FAKE_CLAUDE_FAIL_DIRS
  (comma separated; on --model FAKE_CLAUDE_FAIL_MODEL alone when that is set): an assistant message
  with error rate_limit and an error result, after a rejected rate_limit_event naming its reset
  (FAKE_CLAUDE_RESETS, epoch seconds) when that is set, as a weekly meter's rejection streams
- any line when CLAUDE_CONFIG_DIR is in FAKE_CLAUDE_AUTH_DIRS: error authentication_failed,
  "Not logged in" (a config folder with no login)
- a line containing EMPTY_ONCE: an empty result the first time (marker file FAKE_CLAUDE_MARK); EMPTY_TWICE: the
  first two times
- --resume with FAKE_CLAUDE_ORPHANS=1, the first time: before the first line's reply, the turn Claude Code 2.1.301
  ran of its own on the resume after a crash (seen live): two task_notification events for background
  commands the dead process orphaned, an init, and that turn's empty result, origin kind task-notification
- a line containing BACKGROUND: after its answer, while nothing is asked, a background command's notice turn
  (task_notification, init, a result with origin kind task-notification and text of its own)
- a line containing STALL: no word, every later line taken and never replayed, until stdin closes (a turn that goes
  silent with a message folded into it)
- a line containing FIXTURE:<name>: the events of fixtures/<name>.jsonl the first time (marker
  FAKE_CLAUDE_MARK.<name>), then the plain echo; {sid} in a fixture is the session id
- a line containing TOO_LONG_ONCE: "Prompt is too long" (invalid_request) until a /compact
- the line /compact: a compact_boundary as 2.1.304 streams it (post_tokens FAKE_CLAUDE_POST, else 20000), then a
  compaction result; each one appends a line to FAKE_CLAUDE_MARK.compacted. FAKE_CLAUDE_COMPACT_SLOW=S waits S seconds
  first; FAKE_CLAUDE_COMPACT_FAIL=1: Claude Code's failed-compaction status and an error result, no boundary
- a line containing CONTEXT:N (the last one in it): its assistant message's usage says the session holds N tokens; SUBAGENT:M adds, after it,
  the test reads (it_back_as_plain text) with one more Line
- a line containing AUTO_COMPACT: before its answer, Claude Code's own compaction inside the turn as 2.1.316 streams it:
  status compacting, FAKE_CLAUDE_COMPACT_SLOW seconds, status null (compact_result success), a compact_boundary (trigger
  auto, post_tokens FAKE_CLAUDE_POST, else 20000)
- a line containing SLOW: waits up to 1.5 s for another line; if one comes, it is replayed and
  answered in the same result (a fold)
- a line containing HANG: no answer for 60 s (a turn only /stop ends)
- a line containing MERGE: replayed, then up to 0.5 s for more lines (0.2 s more once one comes, unread), then answered
  as usual; every line then waiting on stdin is replayed as ONE user message, their texts joined with newlines (one
  replay event, one uuid), and answered once with "echo: " plus that text: the stream Claude Code 2.1.316 gave for the
  lines waiting when a turn ended (seen live once: three one-character messages in one replay)
- a line containing READ_ALONG: the answer is NO_REPLY on a line of its own (a room reading along, nothing to add)
- a line containing CHATTY: a stream event every 0.3 s for about 2 s, then the answer
- --resume with FAKE_CLAUDE_MISSING=1: "No conversation found" on stderr, exit 1
- --model equal to FAKE_CLAUDE_BAD_MODEL: every line fails as model_not_found, "There's an issue
  with the selected model" (a model on the list that no account can run)
- FAKE_CLAUDE_WINDOWS="0.07,0.03": a rate_limit_event with those 5-hour and weekly utilizations;
  FAKE_CLAUDE_TOKEN_SHIFT=N moves the 5-hour reset N seconds when CLAUDE_CODE_OAUTH_TOKEN is set
  (a token that belongs to another account)
- `claude auth status`: JSON with authMethod (oauth_token when CLAUDE_CODE_OAUTH_TOKEN is set)
  and an email made from the config folder's name; exit 1 for an FAKE_CLAUDE_AUTH_DIRS folder
- `claude -p PROMPT` without --input-format: one answer, then exit (the health call)
- `claude -p /usage`: a result whose text is FAKE_CLAUDE_USAGE_DIR/<config folder's name>.txt, the meters as Claude
  Code prints them on the folder's own login; under CLAUDE_CODE_OAUTH_TOKEN, or with no such file, the session's cost
  instead (no meters), as the real one prints under a one-year token. FAKE_CLAUDE_USAGE_SLOW=S waits S seconds first
- /fast on, /fast off (a line, or `-p "/fast on"`): what 2.1.321 answers in -p (PROBES 2026-11-11; the accepted text from
  its binary): an init, a synthetic assistant message and a result with num_turns 0 and fast_mode_state; launched without
  "fastMode" in its --settings, "not available in the Agent SDK"; for a FAKE_CLAUDE_NO_CREDITS_DIRS folder, "requires usage
  credits". Every result carries the session's fast_mode_state. FAKE_CLAUDE_STDIN: each line's text is appended there.
- a line containing OUT_OF_CREDITS: the notice 2.1.321 streams once a turn while fast mode is out of usage credits
Turn and, the last part closes the case before (the next) run Starts_The_Example.
"""
import json
import os
import select
import sys
import time
import uuid
from pathlib import Path

argv = sys.argv[1:]
rec = os.environ.get("FAKE_CLAUDE_RECORD")
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
if rec:
    with open(rec, "a") as f:
        f.write(json.dumps({"argv": argv, "config_dir": cfg, "env_keys": sorted(os.environ)}) + "\n")


def listed(var):
    return cfg in [x for x in os.environ.get(var, "").split(",") if x]


no_login = listed("FAKE_CLAUDE_AUTH_DIRS")
if argv[:1] == ["setup-token"]:
    tok = "sk-" + "ant-oat01-" + "Fake" * 25                    # token-shaped (a real one is ~108 characters),
    cols = (os.get_terminal_size(sys.stdout.fileno()).columns if sys.stdout.isatty() else 0) or 80   # built so the
    line = " " + tok                                            # scrub never sees one; one space of indent
    wrapped = "\r\n".join(line[i:i + cols] for i in range(0, len(line), cols))   # as the real screen wraps
    sys.stdout.write("Open this link to sign in: https://example.test/oauth?x=1\r\n")
    sys.stdout.write("Your one-year token:\r\n" + wrapped[:20])
    sys.stdout.flush()
    sys.stdout.write(wrapped[20:] + "\r\nStore it safely.\r\n")
    sys.exit(0)
if argv[:2] == ["auth", "status"]:
    method = "oauth_token" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else ("none" if no_login else "claude.ai")
    user = Path(cfg).name.strip(".") or "default"
    print(json.dumps({"loggedIn": not no_login, "authMethod": method,
                      **({} if no_login else {"email": "@".join([user, "example.test"])})}))
    sys.exit(1 if no_login else 0)
if argv[argv.index("-p") + 1:argv.index("-p") + 2] == ["/usage"] if "-p" in argv else False:
    time.sleep(float(os.environ.get("FAKE_CLAUDE_USAGE_SLOW") or 0))
    usage = Path(os.environ.get("FAKE_CLAUDE_USAGE_DIR") or "/nonexistent") / f"{Path(cfg).name}.txt"
    text = usage.read_text() if usage.is_file() and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else \
        "Total cost:            $0.0000\nTotal duration (API):  0s\n"
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "total_cost_usd": 0,
                      "result": text}))
    sys.exit(0)
if "--resume" in argv and os.environ.get("FAKE_CLAUDE_MISSING") == "1":
    sys.stderr.write(f"No conversation found with session ID: {argv[argv.index('--resume') + 1]}\n")
    sys.exit(1)
one_shot = "--input-format" not in argv
sid = (argv[argv.index("--session-id") + 1] if "--session-id" in argv else
       argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4()))
mark = os.environ.get("FAKE_CLAUDE_MARK", "")
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
failing = listed("FAKE_CLAUDE_FAIL_DIRS") and model == os.environ.get("FAKE_CLAUDE_FAIL_MODEL", model)   # one model, when named
bad_model = bool(model) and model == os.environ.get("FAKE_CLAUDE_BAD_MODEL")


def launch_settings():
    v = argv[argv.index("--settings") + 1] if "--settings" in argv else ""
    try:
        return json.loads(v if v.lstrip().startswith("{") else Path(v).read_text()) if v else {}
    except (OSError, ValueError):
        return {}


fast = {"launch": launch_settings().get("fastMode") is True, "state": "off"}


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def replay(msg):
    if "--replay-user-messages" in argv:
        out({**msg, "isReplay": True, "session_id": sid, "uuid": str(uuid.uuid4())})


def result(text, error=False, model="fake-model"):
    out({"type": "result", "subtype": "success", "is_error": error, "result": text, "session_id": sid,
         "modelUsage": {} if error else {model: {}}, "fast_mode_state": fast["state"]})


def fast_command(text):
    """/fast as 2.1.321 answers it under -p: the state it reaches, said by a synthetic message, no model call."""
    arg = text.strip()[len("/fast"):].strip()
    if not fast["launch"]:
        said = "Fast mode unavailable: Fast mode is not available in the Agent SDK"
    elif arg == "off":
        fast["state"], said = "off", "Fast mode OFF (this session only)"
    elif listed("FAKE_CLAUDE_NO_CREDITS_DIRS"):
        said = "Fast mode unavailable: Fast mode requires usage credits · /usage-credits to turn them on"
    else:
        fast["state"], said = "on", "↯ Fast mode ON · $16/$80 per Mtok (this session only)"
    out({"type": "system", "subtype": "init", "session_id": sid, "model": model, "tools": [], "fast_mode_state": fast["state"]})
    out({"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": said}]}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": said, "num_turns": 0, "total_cost_usd": 0,
         "session_id": sid, "fast_mode_state": fast["state"]})


def first(name):
    """True the first time `name` is asked for in this test (a marker file next to FAKE_CLAUDE_MARK)."""
    m = f"{mark}.{name}" if mark else ""
    if m and not os.path.exists(m):
        open(m, "w").close()
        return True
    return False


def answer(text):
    if bad_model:
        said = f"There's an issue with the selected model ({model}). It may not exist or you may not have access to it."
        out({"type": "assistant", "error": "model_not_found", "message": {"content": [{"type": "text", "text": said}]}})
        return result(said, error=True)
    if failing or "FAIL_RATE" in text:
        if resets := os.environ.get("FAKE_CLAUDE_RESETS"):      # the stream's shape for a weekly meter's rejection (seen live)
            out({"type": "rate_limit_event", "session_id": sid, "uuid": str(uuid.uuid4()), "rate_limit_info": {
                "status": "rejected", "rateLimitType": "seven_day_overage_included", "resetsAt": int(resets), "unifiedWindows": {
                    "seven_day": {"utilization": 0.7, "resetsAt": int(resets)},
                    "seven_day_overage_included": {"utilization": 1, "resetsAt": int(resets)}}}})
        out({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "limit"}]}})
        return result("You've hit your session limit", error=True)
    if no_login:
        out({"type": "assistant", "error": "authentication_failed",
             "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
        return result("Not logged in · Please run /login", error=True)
    if text.strip() == "/compact":
        if mark:
            with open(f"{mark}.compacted", "a") as f:
                f.write("compact\n")
        time.sleep(float(os.environ.get("FAKE_CLAUDE_COMPACT_SLOW") or 0))
        if os.environ.get("FAKE_CLAUDE_COMPACT_FAIL"):
            out({"type": "system", "subtype": "status", "status": None, "compact_result": "failed", "compact_error": "fake", "session_id": sid})
            return result("Error during compaction: fake", error=True)
        out({"type": "system", "subtype": "compact_boundary", "session_id": sid, "uuid": str(uuid.uuid4()),
             "compact_metadata": {"trigger": "manual", "post_tokens": int(os.environ.get("FAKE_CLAUDE_POST") or 20000)}})
        return result("")                 # 2.1.304 answers /compact with no text (the live ledger's rows 513, 514)
    if "TOO_LONG_ONCE" in text and mark and not os.path.exists(f"{mark}.compacted"):
        out({"type": "assistant", "error": "invalid_request",
             "message": {"content": [{"type": "text", "text": "Prompt is too long"}]}})
        return result("Prompt is too long", error=True)
    if "FIXTURE:" in text:
        name = text.split("FIXTURE:")[1].split()[0]
        if first(name):
            path = Path(__file__).with_name("fixtures") / f"{name}.jsonl"
            for line in path.read_text().splitlines():
                if line.strip():
                    out(json.loads(line.replace("{sid}", sid)))
            return None
    if "EMPTY_ONCE" in text and mark and not os.path.exists(mark):
        open(mark, "w").close()
        return result("")
    if "EMPTY_TWICE" in text and (first("empty-1") or first("empty-2")):
        return result("")
    if windows := os.environ.get("FAKE_CLAUDE_WINDOWS"):
        five, week = (float(x) for x in windows.split(","))
        shift = int(os.environ.get("FAKE_CLAUDE_TOKEN_SHIFT", "0")) if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else 0
        out({"type": "rate_limit_event", "session_id": sid, "uuid": str(uuid.uuid4()), "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {
                "five_hour": {"utilization": five, "resetsAt": 2000000000 + shift},
                "seven_day": {"utilization": week, "resetsAt": 2000300000}}}})
    if "OUT_OF_CREDITS" in text:               # fast mode on, the account out of usage credits: once a turn (fast mode page)
        out({"type": "system", "subtype": "notification", "session_id": sid, "uuid": str(uuid.uuid4()),
             "text": "Fast mode disabled \u00b7 Out of usage credits"})
    if "HOOK_ERROR" in text:                   # what Claude Code 2.1.301 streams when a hook fails, non-blocking
        out({"type": "system", "subtype": "notification", "session_id": sid, "uuid": str(uuid.uuid4()),
             "key": "stop-hook-error", "text": "Stop hook error occurred \u00b7 ctrl+o to see"})
    if "AUTO_COMPACT" in text:                 # Claude Code's own, past its line: the events 2.1.316 streamed for one, 2026-11-05
        out({"type": "system", "subtype": "status", "status": "compacting", "session_id": sid, "uuid": str(uuid.uuid4())})
        time.sleep(float(os.environ.get("FAKE_CLAUDE_COMPACT_SLOW") or 0))
        out({"type": "system", "subtype": "status", "status": None, "compact_result": "success", "session_id": sid})
        out({"type": "system", "subtype": "compact_boundary", "session_id": sid, "uuid": str(uuid.uuid4()),
             "compact_metadata": {"trigger": "auto", "post_tokens": int(os.environ.get("FAKE_CLAUDE_POST") or 20000)}})
    reply = "\nNO_REPLY\n" if "READ_ALONG" in text else "echo: " + text
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}}})
    n = int(text.split("CONTEXT:")[-1].split()[0]) if "CONTEXT:" in text else 0     # the last: a copy's tail ahead names earlier ones
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}],
                                          **({"usage": {"input_tokens": 2, "cache_read_input_tokens": n - 2}} if n else {})}})
    if "SUBAGENT:" in text:
        out({"type": "assistant", "parent_tool_use_id": "toolu_sub", "message": {
            "content": [], "usage": {"input_tokens": int(text.split("SUBAGENT:")[1].split()[0])}}})
    return result(reply)


def notice_turn(status, text, tasks=("bg1",)):
    """A turn Claude Code runs of its own for background commands' notices, as 2.1.301 streams it."""
    for task in tasks:
        out({"type": "system", "subtype": "task_notification", "task_id": task, "tool_use_id": "toolu_" + task,
             "status": status, "output_file": "", "summary": "Orphaned by a previous Claude Code process exit and reported "
             "in an aggregate summary." if status == "stopped" else "Background command finished.", "session_id": sid})
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": []})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 0, "session_id": sid,
         "origin": {"kind": "task-notification"}, "modelUsage": {"fake-model": {}}})


out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": []})
if one_shot:
    asked = argv[argv.index("-p") + 1]
    fast_command(asked) if asked.startswith("/fast") else answer(asked)
    sys.exit(0)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    msg = json.loads(line)
    text = msg["message"]["content"]
    if os.environ.get("FAKE_CLAUDE_STDIN"):
        with open(os.environ["FAKE_CLAUDE_STDIN"], "a") as f:
            f.write(json.dumps(text) + "\n")
    if "--resume" in argv and os.environ.get("FAKE_CLAUDE_ORPHANS") == "1" and first("orphans"):
        notice_turn("stopped", "", tasks=("bg1", "bg2"))
    if "STALL" in text:
        for _ in sys.stdin:                    # a message folded in is taken and never replayed
            pass
        break
    replay(msg)
    if text.strip().startswith("/fast"):
        fast_command(text)
        continue
    if "SLOW" in text and select.select([sys.stdin], [], [], 1.5)[0]:
        extra = json.loads(sys.stdin.readline())
        replay(extra)
        text = text + " + " + extra["message"]["content"]
    if "HANG" in text:
        time.sleep(60)
    if "MERGE" in text and select.select([sys.stdin], [], [], 0.5)[0]:
        time.sleep(0.2)                        # a line came: the rest of the burst lands too, still unread
    if "CHATTY" in text:     # keeps talking for about 2 s in 0.3 s steps: a silence window must not cut it
        for _ in range(7):
            out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "."}}})
            time.sleep(0.3)
    answer(text)
    if "BACKGROUND" in text:                   # the notice turn lands while the room asks nothing
        notice_turn("completed", "The render finished.")
    if "MERGE" in text:                        # the lines waiting on stdin when the turn ended, read from the pipe itself
        waiting = b""                          # (sys.stdin's buffer would hide the second line from select)
        while select.select([sys.stdin], [], [], 0)[0] and (chunk := os.read(sys.stdin.fileno(), 1 << 16)):
            waiting += chunk
        if lines := [json.loads(x)["message"]["content"] for x in waiting.decode().splitlines() if x.strip()]:
            replay({"type": "user", "message": {"role": "user", "content": "\n".join(lines)}})   # ONE replay, one uuid
            answer("\n".join(lines))
