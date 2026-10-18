#!/usr/bin/env python3
"""A stand-in for `claude -p` in stream-json mode, for offline engine and room tests.

Each user line gets: a replay (with --replay-user-messages), one text delta, an assistant
message and a success result echoing the text. Variations:
- a line containing FAIL_RATE, or any line when CLAUDE_CONFIG_DIR is in FAKE_CLAUDE_FAIL_DIRS
  (comma separated): an assistant message with error rate_limit and an error result
- a line containing EMPTY_ONCE: an empty result the first time (marker file FAKE_CLAUDE_MARK)
- a line containing SLOW: waits up to 1.5 s for another line; if one comes, it is replayed and
  answered in the same result (a fold)
Records argv and the config folder to the file named by FAKE_CLAUDE_RECORD.
"""
import json
import os
import select
import sys
import uuid

argv = sys.argv[1:]
rec = os.environ.get("FAKE_CLAUDE_RECORD")
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
if rec:
    with open(rec, "a") as f:
        f.write(json.dumps({"argv": argv, "config_dir": cfg, "env_keys": sorted(os.environ)}) + "\n")
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]
failing = cfg in [x for x in os.environ.get("FAKE_CLAUDE_FAIL_DIRS", "").split(",") if x]


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def replay(msg):
    if "--replay-user-messages" in argv:
        out({**msg, "isReplay": True, "session_id": sid, "uuid": str(uuid.uuid4())})


def result(text, error=False):
    out({"type": "result", "subtype": "success", "is_error": error, "result": text, "session_id": sid,
         "modelUsage": {} if error else {"fake-model": {}}})


out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": []})
while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    msg = json.loads(line)
    text = msg["message"]["content"]
    replay(msg)
    if "FAIL_RATE" in text or failing:
        out({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "limit"}]}})
        result("You've hit your session limit", error=True)
        continue
    mark = os.environ.get("FAKE_CLAUDE_MARK", "")
    if "EMPTY_ONCE" in text and mark and not os.path.exists(mark):
        open(mark, "w").close()
        result("")
        continue
    if "SLOW" in text and select.select([sys.stdin], [], [], 1.5)[0]:
        extra = json.loads(sys.stdin.readline())
        replay(extra)
        text = text + " + " + extra["message"]["content"]
    reply = "echo: " + text
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
    result(reply)
