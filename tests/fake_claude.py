#!/usr/bin/env python3
"""A stand-in for `claude -p` in stream-json mode, for offline engine tests.

Each user line gets: a replay (with --replay-user-messages), one text delta, an assistant
message and a success result echoing the text. A line containing FAIL_RATE gets an assistant
message with error rate_limit and an error result. Records argv and the config folder to the
file named by FAKE_CLAUDE_RECORD.
"""
import json
import os
import sys
import uuid

argv = sys.argv[1:]
rec = os.environ.get("FAKE_CLAUDE_RECORD")
if rec:
    with open(rec, "a") as f:
        f.write(json.dumps({"argv": argv, "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                            "env_keys": sorted(os.environ)}) + "\n")
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else argv[argv.index("--resume") + 1]


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


out({"type": "system", "subtype": "init", "session_id": sid, "model": "fake", "tools": []})
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    text = msg["message"]["content"]
    if "--replay-user-messages" in argv:
        out({**msg, "isReplay": True, "session_id": sid, "uuid": str(uuid.uuid4())})
    if "FAIL_RATE" in text:
        out({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "limit"}]}})
        out({"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your session limit",
             "session_id": sid, "modelUsage": {}})
        continue
    reply = "echo: " + text
    out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}}})
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply, "session_id": sid,
         "modelUsage": {"fake-model": {}}})
