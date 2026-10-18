#!/usr/bin/env python3
"""A stand-in for a worker's `claude -p` (the prompt on stdin, stream-json out), for the worker tests.

The brief's FAKE line says what to do, e.g. `FAKE ticks=3 gap=0.2 sleep=0 report="All three done" exit=0`:
wait `sleep` seconds, tick the TASKS.md rows one by one `gap` seconds apart, write REPORT.md (its
first line `report`, then `pad` filler characters), print a result event and exit with `exit`.
It records its argv and its environment's names (never values) to fake-record.json in its folder.
Started in stream-json input mode (a room's process, for the relay turn) it is fake_claude.py.
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

if "--input-format" in sys.argv:
    here = Path(__file__).with_name("fake_claude.py")
    os.execv(sys.executable, [sys.executable, str(here), *sys.argv[1:]])

prompt = sys.stdin.read()
Path("fake-record.json").write_text(json.dumps({"argv": sys.argv[1:], "env_keys": sorted(os.environ),
                                                "cwd": os.getcwd(), "prompt": prompt}))
m = re.search(r"^FAKE (.*)$", prompt, re.M)
opts = dict(x.split("=", 1) for x in shlex.split(m.group(1))) if m else {}
tasks = Path(re.search(r"Keep (\S+) as your checklist", prompt).group(1))
report = Path(re.search(r"Write your report to (\S+?)\. Its first line", prompt).group(1))
time.sleep(float(opts.get("sleep", 0)))
for _ in range(int(opts.get("ticks", 0))):
    time.sleep(float(opts.get("gap", 0)))
    lines = tasks.read_text().splitlines()
    i = next((n for n, x in enumerate(lines) if "- [ ]" in x), None)
    if i is None:
        break
    lines[i] = lines[i].replace("- [ ]", "- [x]", 1)
    tasks.write_text("\n".join(lines) + "\n")
if "report" in opts:
    report.write_text(f"# {opts['report']}\n\n" + "filler line of the report body\n" * (int(opts.get("pad", 0)) // 31)
                      + "LAST LINE OF THE REPORT\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": opts.get("result", "finished")}), flush=True)      # `result`: the last message
sys.exit(int(opts.get("exit", 0)))
