# Adapted from the author's hook dispatcher in tools/hooks/ (strip_heredocs, quoted_spans,
# search_outside_quotes, rm_rewrite and rm_rewrite_modes).
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
# Changes: no kill-switch files or workspace paths; a recursive flag and `unlink` are rewritten too
# (the trash takes a folder whole); the trash command is passed in; segments are split here.
"""Shell text read the way the shell reads it, for the checks on each command a room runs: heredoc bodies are data, quoted text is prose, and
only a word in command position is a command. A plain delete becomes the trash command."""
from __future__ import annotations

import os
import re
import shlex
import stat
import threading

UNSAFE = re.compile(r"[;&|<>()$`*?\[\]{}\\\n\r~#!]")   # anything past one plain invocation
FLAGS = re.compile(r"^-[fvrRdI]+$|^--(?:force|verbose|recursive|dir)$")
WRAPPERS = frozenset({"sudo", "env", "command", "builtin", "exec", "nohup", "setsid", "time", "nice"})
TAKES = frozenset({"-u", "-g", "-n", "-C", "-D", "-p", "-r", "-t", "-U", "-h", "--user", "--group"})  # an option and its value
STAT_S = 2.0               # the lstat bound: a hook past its own timeout renders no decision at all


def strip_heredocs(c: str) -> str:
    """Heredoc bodies removed, every header line kept: a redirect target lives in the header,
    the body is data."""
    keep, term = [], None
    for line in str(c).split("\n"):
        if term is not None:
            if line.strip() == term:
                term = None
            continue
        keep.append(line)
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if m and not re.search(r"<<<", line):
            term = m.group(1)
    return "\n".join(keep)


def quoted_spans(c: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in re.finditer(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", str(c or ""))]


def search_outside_quotes(rx: re.Pattern[str], text: str) -> re.Match[str] | None:
    """The first match whose START is outside a quoted literal: an operator written inside quotes
    is prose, a quoted TARGET after a real operator is still a write."""
    spans = quoted_spans(text)
    for m in rx.finditer(text):
        if not any(a < m.start() < b for a, b in spans):
            return m
    return None


def segments(command: str) -> list[str]:
    """The simple commands of a command line, split on ; && || | & and newlines outside quotes,
    heredoc bodies dropped first."""
    text = strip_heredocs(command)
    spans = quoted_spans(text)
    out, start, i = [], 0, 0
    while i < len(text):
        if any(a <= i < b for a, b in spans):
            i += 1
            continue
        two = text[i:i + 2]
        step = 2 if two in ("&&", "||") else 1 if text[i] in ";|&\n" else 0
        if step:
            out.append(text[start:i])
            i += step
            start = i
            continue
        i += 1
    out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def argv_of(segment: str) -> list[str]:
    """The words of one simple command, wrappers and leading assignments skipped ([] if unparsable)."""
    try:
        words = shlex.split(segment.strip().lstrip("({").strip())
    except ValueError:
        return []
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS):
        if words.pop(0) in WRAPPERS:
            while len(words) > 1 and words[0].startswith("-"):     # sudo -u me, nice -n 5, env -i
                if words.pop(0) in TAKES and len(words) > 1:
                    words.pop(0)
    return words


def command_words(command: str) -> list[str]:
    """The command word of every simple command, by basename."""
    return [os.path.basename(a[0]) for a in map(argv_of, segments(command)) if a]


def modes(base: str, paths: list[str], bound: float = STAT_S) -> list[int | None] | None:
    """lstat each path under `base` on a thread, bounded: one st_mode per path (None: missing), or
    None when the filesystem did not answer in time."""
    box: dict[str, list[int | None]] = {}

    def work() -> None:
        if not os.path.isdir(base):
            return
        res: list[int | None] = []
        for p in paths:
            try:
                res.append(os.lstat(os.path.join(base, p)).st_mode)
            except (FileNotFoundError, NotADirectoryError):
                res.append(None)
        box["m"] = res

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(bound)
    return box.get("m")


def delete_rewrite(command: str, cwd: str, trash: list[str]) -> dict[str, object] | None:
    """{"command", "paths", "dropped"} when the WHOLE command is one plain `rm` or `unlink` of named
    paths that can go to the trash, else None. Folders go only with a recursive flag; a missing
    path only under -f, and is dropped (nothing left: `true`)."""
    if UNSAFE.search(command) or not trash:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or argv[0] not in ("rm", "unlink") or not os.path.isabs(cwd or ""):
        return None
    force = recursive = done = False
    paths: list[str] = []
    for tok in argv[1:]:
        if not done and tok == "--":
            done = True
            continue
        if not done and tok.startswith("-") and argv[0] == "rm":
            if not FLAGS.match(tok):
                return None
            force = force or "f" in tok or tok == "--force"
            recursive = recursive or bool(re.search(r"[rR]", tok)) or tok == "--recursive"
            continue
        if not tok:
            return None
        paths.append(tok)
    if not paths or (argv[0] == "unlink" and len(paths) != 1):
        return None
    found = modes(cwd, paths)
    if found is None:
        return None
    keep, dropped = [], []
    for p, mode in zip(paths, found):
        if mode is None:
            if not force:
                return None
            dropped.append(p)
        elif stat.S_ISDIR(mode) and not recursive:
            return None
        elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode) or stat.S_ISDIR(mode)):
            return None
        else:
            keep.append(p)
    new = " ".join([*map(shlex.quote, trash), "--", *map(shlex.quote, keep)]) if keep else "true"
    return {"command": new, "paths": keep, "dropped": dropped}
