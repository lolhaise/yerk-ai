# Adapted from Hermes Agent v0.22.2, cron/scheduler_script.py (_terminate_process_group),
# https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
"""Stop a command and every process it started: TERM its process group, give it a moment, then
KILL the group if any member survived (a survivor holds the pipe's write end open, so a reader
waiting for the end of its output would wait forever). The command must have been started with
`start_new_session=True`, so its group is its own and never the caller's."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess


def stop_group(proc: subprocess.Popen[str] | subprocess.Popen[bytes], grace: float = 1.0) -> None:
    if proc.poll() is not None:
        return
    try:
        group = os.getpgid(proc.pid)
        os.killpg(group, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    try:
        os.killpg(group, 0)
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(group, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
