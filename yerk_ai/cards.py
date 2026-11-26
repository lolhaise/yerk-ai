"""Merges two fact cards by hand; a fact card is a memory file of standing facts about one subject. `yerk cards merge A B` moves card
A's live lines onto card B's end and leaves A moved to B (memory.moves), one pair at a time, the owner's choice after reading the pair;
`yerk cards near` lists the live cards whose names share a first word, as remember's `near` reads them. No model chooses. Nothing adds
gains lines at its end and A keeps its line count, so every cite of either still names the line it named."""
from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from typing import Any

from . import doctor, memory, mentions
from .config import write_private

LOCK_WAIT = 10.0         # seconds a merge waits for the writers' lock before it refuses


def fold(x: str) -> str:
    return " ".join(mentions.tokens(x, True))


def own(card: Path) -> str:
    return card.stem.replace("-", " ").replace("_", " ")


def find(s: memory.Settings, name: str) -> tuple[Path | None, str]:
    """The fact card of the tree `name` names (its file stem, its own name, or its path), else None and the one line why not."""
    folder, name = s.root / "facts", name.strip()
    asked = {(p if p.is_absolute() else r / p).resolve() for p in [Path(name)] for r in (s.root, s.root.parent)} if "/" in name else set()
    every = sorted(folder.glob("*.md"))
    hit = [c for c in every if c.resolve() in asked] if asked else [c for c in every if c.stem == name.removesuffix(".md")] or [
        c for c in every if fold(own(c)) == fold(name)]
    if len(hit) != 1:
        return None, f"{name}: " + (f"{len(hit)} cards answer to it ({', '.join(c.name for c in hit)}); name its file" if hit else
                                    f"no fact card of the tree ({folder}/*.md) is named so")
    c = hit[0]
    why = ("a link out of the tree" if c.is_symlink() or c.resolve().parent != folder.resolve() else "a curated file" if c.name in s.curated else
           "in a read-only root" if any(c.resolve().is_relative_to(r) and not s.root.is_relative_to(r) for r in s.scope.roots[1:]) else "")
    return (None, f"{c.name}: {why}, no fact card of the tree") if why else (c, "")


def text(p: Path) -> str:
    return p.read_bytes().decode("utf-8", "surrogateescape")      # written back byte for byte


def plan(s: memory.Settings, a: Path, b: Path, today: date) -> dict[str, Any]:
    """What `merge` writes, or `refused` (exit 2) or `done` (exit 0), the one line why nothing is written."""
    ta, tb, moved = text(a), text(b), memory.moves(s.root, today)
    if a.resolve() == b.resolve():
        return {"refused": f"{a.name} and {b.name} are one card"}
    if (ka := memory.file_class(a)) != (kb := memory.file_class(b)):
        return {"refused": f"{a.name} is {' '.join(filter(None, ka))} and {b.name} is {' '.join(filter(None, kb))}: a merge keeps to one class and room"}
    if len(twins := [c.name for c in sorted((s.root / "facts").glob("*.md")) if not c.is_symlink() and fold(own(c)) == fold(own(b))]) > 1:
        return {"refused": f"{' and '.join(twins)} answer to one name, so a pointer to it would name neither (memory.moves): rename one first"}
    if not memory.live(tb, today):
        return {"refused": f"{b.name} is " + (f"moved to {moved[b].stem}: merge into that card" if b in moved else "emptied: no card to merge into")}
    if not memory.live(ta, today):
        return {"done": f"{a.name} is emptied already" + (f", moved to {moved[a].stem}" if a in moved else "") + ": nothing to move"}
    rows, body = ta.splitlines(keepends=True), memory.body_of(ta)
    if any(memory.MOVED.fullmatch(x) for x in body):
        return {"refused": f"{a.name} holds a line `# moved to: ...` already, and a second would be words: take it out by hand first"}
    lines, have = [x.splitlines()[0] for x in rows], tb.splitlines()
    ends, move = [x[len(y):] for x, y in zip(rows, lines)], [i for i, x in enumerate(body) if memory.alive(x, today)]
    held, add, pairs = dict[str, int](), list[str](), list[tuple[int, int]]()
    for j, x in enumerate(memory.body_of(tb), 1):
        held.setdefault(x, j)
    for i in move:                                   # in file order, a line B holds word for word not again: its cite maps there
        if lines[i] not in held:
            add.append(lines[i])
            held[lines[i]] = len(have) + len(add)
        pairs.append((i + 1, held[lines[i]]))
    had = set(text(f).splitlines()) if (f := s.root / memory.LATER).is_file() else set()
    tsv = [r for i, m in pairs if (r := f"{memory.cite(s, a)}:{i}\t{memory.cite(s, b)}:{m}") not in had]
    to, out, front = f"# moved to: {own(b)}", list(lines), len(m[0].splitlines()) if (m := memory.FRONT.match(ta)) else 0
    for i in move:
        out[i], ends[i] = "", ends[i] or "\n"
    at = 1 if len(lines) > 1 and not front and not lines[1].strip() else move[0]
    out[at] = to
    points: dict[Path, str] = {}                     # each card moved to A, its one pointer naming B after
    for c in (c for c, t in moved.items() if t == a):
        rs = text(c).splitlines(keepends=True)
        k = next(i for i, y in enumerate(memory.body_of("".join(rs))) if memory.MOVED.fullmatch(y))
        rs[k] = to + rs[k][len(rs[k].splitlines()[0]):]
        points[c] = "".join(rs)
    sep = "\n" if tb.splitlines(keepends=True)[-1] == have[-1] else ""
    return {"move": len(move), "held": len(move) - len(add), "have": len(have), "add": add, "tsv": tsv, "at": at + 1, "to": to,
            "lines": len(lines), "new_a": "".join(x + e for x, e in zip(out, ends)), "new_b": tb + sep + "".join(x + "\n" for x in add), "points": points}


def merge(s: memory.Settings, private: Path, a_name: str, b_name: str, go: bool = False) -> int:
    """`yerk cards merge A B`: without `go` the plan, nothing written; with it, under one hold of the writers' lock, the copies
    of A, B, memory.LATER and each card moved to A into <private>/backups/cards-merge-<day>/, then B's new lines, LATER's rows,
    each card moved to A pointed at B, then A. A stop between two steps loses no line, and the same command run again finishes."""
    (a, why), (b, why_b) = find(s, a_name), find(s, b_name)
    if a is None or b is None:
        print(f"nothing merged: {why or why_b}", file=sys.stderr)
        return 2
    today, backup = memory.local_day(s), private / "backups" / f"cards-merge-{memory.local_day(s).isoformat()}"

    def put(f: Path, new: str) -> None:
        write_private(f, new.encode("utf-8", "surrogateescape"), stat.S_IMODE(f.stat().st_mode))
    try:
        with memory.daily_lock(s.daily, LOCK_WAIT) if go else nullcontext():
            p = plan(s, a, b, today)
            if "refused" in p or "done" in p:
                print(f"nothing merged: {p.get('refused') or p['done']}", file=sys.stderr if "refused" in p else sys.stdout)
                return 2 if "refused" in p else 0
            if go:
                for f in (a, b, s.root / memory.LATER, *p["points"]):
                    if f.is_file():
                        doctor.keep(f, backup)
                if p["add"]:
                    put(b, p["new_b"])
                if p["tsv"]:
                    with os.fdopen(os.open(s.root / memory.LATER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as t:
                        t.write("".join(f"{r}\n" for r in p["tsv"]))
                for c, new in p["points"].items():
                    put(c, new)
                put(a, p["new_a"])
    except TimeoutError as e:
        print(f"nothing merged: {e}, a write is under way; run it again", file=sys.stderr)
        return 2
    span = f"lines {p['have'] + 1} to {p['have'] + len(p['add'])}" if p["add"] else "no new line"
    print("\n".join([f"{'merged' if go else 'the plan, nothing written (--go writes it):'} {a.name} into {b.name}",
                     f"- {p['move']} lines move: {b.name} holds {p['have']} lines and {p['held']} of these already; {len(p['add'])} go on its end, {span}",
                     f"- {a.name} keeps its {p['lines']} lines, the {p['move']} blanked, line {p['at']} reading \"{p['to']}\"",
                     f"- {memory.LATER} gains {len(p['tsv'])} rows" + "".join(f"\n    {r}" for r in p["tsv"]),
                     *([f"- pointed at {b.name} after: {', '.join(c.name for c in p['points'])}"] if p["points"] else []),
                     *([f"- the copies as they were: {backup}"] if go else [])]))
    return 0


def near(s: memory.Settings, today: date) -> dict[str, Any]:
    """`yerk cards near`: each group of two or more live cards whose names (own, aka, and those of the cards moved to each) share
    a first word, as remember's `near` compares them; per card its own name, its live lines, its other names and what moved to it."""
    texts = {c: c.read_text(errors="replace") for c in sorted((s.root / "facts").glob("*.md"))}
    moved, groups = memory.moves(s.root, today, texts), dict[str, set[Path]]()
    names = {c: memory.card_names(c, t) for c, t in texts.items()}
    for c, n in names.items():
        for x in n if c in moved or memory.live(texts[c], today) else []:
            if fold(x):
                groups.setdefault(fold(x).split()[0], set()).add(moved.get(c, c))
    row = {c: {"name": names[c][0], "live": sum(memory.alive(x, today) for x in memory.body_of(texts[c])), "aka": names[c][1:],
               "moved": [names[m][0] for m, t in moved.items() if t == c]} for g in groups.values() for c in g}
    return {"groups": [{"word": w, "cards": [row[c] for c in sorted(g, key=lambda c: names[c][0])]} for w, g in sorted(groups.items()) if len(g) > 1],
            "cards": len(texts), "emptied": sum(not memory.live(t, today) for t in texts.values()), "moved": len(moved),
            "pointers": sum(any(memory.MOVED.fullmatch(x) for x in memory.body_of(t)) for t in texts.values())}


def shown(got: dict[str, Any], as_json: bool) -> str:
    if as_json:
        return json.dumps(got, indent=1)
    out = [f"{g['word']}: " + "; ".join(f"{c['name']} ({c['live']} live line{'s' * (c['live'] != 1)}" + "".join(f", {k} {', '.join(c[k])}" for k in ("aka", "moved") if c[k])
                                        + ")" for c in g["cards"]) for g in got["groups"]]
    return "\n".join([*out, f"{len(got['groups'])} groups, {len({c['name'] for g in got['groups'] for c in g['cards']})} cards in them, of "
                      f"{got['cards']}; {got['emptied']} emptied, {got['pointers']} holding a pointer, {got['moved']} moved"])
