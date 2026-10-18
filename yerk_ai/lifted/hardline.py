# Adapted from Hermes Agent, tools/approval_detection.py (HARDLINE_PATTERNS, _hardline_rm_path,
# _HARDLINE_SYSTEM_DIRS), https://github.com/NousResearch/hermes-agent
# Copyright (c) 2025 Nous Research. MIT License. See NOTICE.
# Changes: matched against one simple command at a time (yerk_ai/lifted/shellguard.py finds the
# command word), so the _CMDPOS prefix and the shell scanner are not needed; Windows rules left out.
"""Commands that never run, whatever anyone says: no approval releases them."""
from __future__ import annotations

import re

TAIL = r"(?:\s|$|[)`;|&])"
SYSTEM_DIRS = (r"/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|"
               r"/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*")
RM = r"^(?:rm|trash|trash-put|gio\s+trash)\s+(?:-[^\s]*\s+)*(?:--\s+)?"


def rm_path(alt: str) -> str:
    return rf"(?:[\"'](?:{alt})[\"']|(?:{alt}){TAIL})"


PATTERNS = [
    (RM + rm_path(r"/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*"), "a delete of the root folder"),
    (RM + rm_path(SYSTEM_DIRS), "a delete of a system folder"),
    (RM + rm_path(r"(?:~|\$\{?HOME\}?)(?:/?|/\*)?"), "a delete of the home folder"),
    (r"^mkfs(\.[a-z0-9]+)?\b", "formatting a filesystem"),
    (r"^dd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", "dd onto a raw disk"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "a redirect onto a raw disk"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "a fork bomb"),
    (r"^kill\s+(-[^\s]+\s+)*-1\b", "killing every process"),
    (r"^(shutdown|reboot|halt|poweroff)\b", "shutting the machine down"),
    (r"^(?:tel)?init\s+[06]\b", "shutting the machine down"),
    (r"^systemctl\s+(poweroff|reboot|halt|kexec)\b", "shutting the machine down"),
]
COMPILED = [(re.compile(p, re.I | re.S), what) for p, what in PATTERNS]


def hardline(segment: str, anchored: bool = True) -> str | None:
    """What a simple command would do that never runs, else None. `anchored`: the command-word rules,
    read off the parsed words; else the positionless ones (a redirect, a fork bomb), read off the
    segment with its quoted text blanked, so a quote of them is prose."""
    for rx, what in COMPILED:
        if rx.pattern.startswith("^") == anchored and rx.search(segment.strip()):
            return what
    return None
