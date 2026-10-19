#!/usr/bin/env python3
"""A stand-in for Claude Code's `claude -p` in stream-json mode, for offline engine, room and health tests.

Each user line gets: a replay (with --replay-user-messages), one text delta, an assistant
message and a success result echoing the text. Variations:
- a line containing FAIL_RATE, or any line when CLAUDE_CONFIG_DIR is in FAKE_CLAUDE_FAIL_DIRS
  (comma separated): an assistant message with error rate_limit and an error result
- any line when CLAUDE_CONFIG_DIR is in FAKE_CLAUDE_AUTH_DIRS: error authentication_failed,
  "Not logged in" (a config folder with no login)
- a line containing EMPTY_ONCE: an empty result the first time (marker file FAKE_CLAUDE_MARK)
- a line containing FIXTURE:<name>: the events of fixtures/<name>.jsonl the first time (marker
  FAKE_CLAUDE_MARK.<name>), then the plain echo; {sid} in a fixture is the session id
- a line containing TOO_LONG_ONCE: "Prompt is too long" (invalid_request) until a /compact
- the line /compact: a compaction result
- a line containing SLOW: waits up to 1.5 s for another line; if one comes, it is replayed and
  answered in the same result (a fold)
- a line containing HANG: no answer for 60 s (a turn only /stop ends)
- --resume with FAKE_CLAUDE_MISSING=1: "No conversation found" on stderr, exit 1
- --model equal to FAKE_CLAUDE_BAD_MODEL: every line fails as model_not_found, "There's an issue
  with the selected model" (a model on the list that no account can run)
- FAKE_CLAUDE_WINDOWS="0.07,0.03": a rate_limit_event with those 5-hour and weekly utilizations;
  FAKE_CLAUDE_TOKEN_SHIFT=N moves the 5-hour reset N seconds when CLAUDE_CODE_OAUTH_TOKEN is set
  (a token that belongs to another account)
- `claude auth status`: JSON with authMethod (oauth_token when CLAUDE_CODE_OAUTH_TOKEN is set)
  and an email made from the config folder's name; exit 1 for an FAKE_CLAUDE_AUTH_DIRS folder
- `claude -p PROMPT` without --input-format: one answer, then exit (the health call)
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
if "--resume" in argv and os.environ.get("FAKE_CLAUDE_MISSING") == "1":
    sys.stderr.write(f"No conversation found with session ID: {argv[argv.index('--resume') + 1]}\n")
    sys.exit(1)
one_shot = "--input-format" not in argv
sid = (argv[argv.index("--session-id") + 1] if "--session-id" in argv else
       argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4()))
failing = listed("FAKE_CLAUDE_FAIL_DIRS")
mark = os.environ.get("FAKE_CLAUDE_MARK", "")
model = argv[argv.index("--model") + 1] if "--model" in argv else ""
bad_model = bool(model) and model == os.environ.get("FAKE_CLAUDE_BAD_MODEL")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def replay(msg):
    if "--replay-user-messages" in argv:
        out({**msg, "isReplay": True, "session_id": sid, "uuid": str(uuid.uuid4())})


def result(text, error=False, model="fake-model"):
    out({"type": "result", "subtype": "success", "is_error": error, "result": text, "session_id": sid,
         "modelUsage": {} if error else {model: {}}})


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
        out({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "limit"}]}})
        return result("You've hit your session limit", error=True)
    if no_login:
        out({"type": "assistant", "error": "authentication_failed",
             "message": {"content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
        return result("Not logged in · Please run /login", error=True)
    if text.strip() == "/compact":
        if mark:
            open(f"{mark}.compacted", "w").close()
        return result("Compacted.")
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
    if windows := os.environ.get("FAKE_CLAUDE_WINDOWS"):
        five, week = (float(x) for x in windows.split(","))
        shift = int(os.environ.get("FAKE_CLAUDE_TOKEN_SHIFT", "0")) if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else 0
        out({"type": "rate_limit_event", "session_id": sid, "uuid": str(uuid.uuid4()), "rate_limit_info": {
            "status": "allowed", "rateLimitType": "five_hour", "unifiedWindows": {
                "five_hour": {"utilization": five, "resetsAt": 2000000000 + shift},
                "seven_day": {"utilization": week, "resetsAt": 2000300000}}}})
    reply = "echo: " + text
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
    return result(reply)


out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": []})
if one_shot:
    answer(argv[argv.index("-p") + 1])
    sys.exit(0)
while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    msg = json.loads(line)
    text = msg["message"]["content"]
    replay(msg)
    if "SLOW" in text and select.select([sys.stdin], [], [], 1.5)[0]:
        extra = json.loads(sys.stdin.readline())
        replay(extra)
        text = text + " + " + extra["message"]["content"]
    if "HANG" in text:
        time.sleep(60)
    answer(text)
