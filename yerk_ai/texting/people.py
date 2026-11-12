"""The reply list, the people texting may answer in the owner's name: `<private>/<[texting] people>` (default texting/people.toml), owner-only,
name`, then one `[people.<key>]` a person: `first`, `handles` (numbers or Apple ID addresses), `sheet`, `release`. A
handle is held in texts.handle's form, an address lowercased, so a chat's identifier matches it however Messages wrote it.
Beside it each person's sheet (`sheet`, default people/<key>.toml): what may be said to them unasked and how the owner
writes to them; none on file is no voice card and every class held. A sheet or the list changes only by `change`, on the
owner's words, each change one line of sheets.log."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import texts, words
from ..config import load_toml, toml_value, write_private

PEOPLE = "texting/people.toml"
KEYS, PACING = ("open", "knows", "night", "reacts", "unanswered_share", "pacing", "voice"), (
    "weekday_day", "weekday_evening", "weekend_day", "weekend_evening", "active", "burst_gap")
NIGHT, REACTS, OPS, LOG = ("hold", "answer"), ("love", "like"), ("open", "close", "knows", "forget", "release"), "sheets.log"
LOCK = threading.Lock()                   # one change at a time: a turn may call the tool twice at once


def norm(h: Any) -> str:
    return texts.handle(str(h or "")).lower()


@dataclass(frozen=True)
class Person:
    key: str
    first: str
    handles: tuple[str, ...]
    sheet: str = ""
    release: bool = False


@dataclass(frozen=True)
class Sheet:
    """A person sheet: the always-held classes open for them, facts they know (fact, source), at night hold or answer,
    the tapbacks the owner uses with them, the share of closers the owner leaves unanswered, reply times (p25 and p75 per
    bucket), and the owner's voice card for them."""
    open: tuple[str, ...] = ()
    knows: tuple[tuple[str, str], ...] = ()
    night: str = "hold"
    reacts: tuple[str, ...] = ()
    unanswered_share: float = 0.0
    pacing: dict[str, tuple[float, float]] = field(default_factory=dict)
    card: str = ""


@dataclass(frozen=True)
class People:
    owner: str
    people: dict[str, Person]
    sheets: dict[str, Sheet] = field(default_factory=dict)

    def by_handle(self, h: Any) -> Person | None:
        n = norm(h)
        return next((p for p in self.people.values() if n and n in p.handles), None)


def path(private: Path, conf: dict[str, Any]) -> Path:
    return private / str(conf.get("people") or PEOPLE)


def load(file: Path, sheets: bool = True) -> People:
    """The reply list and, with `sheets`, each sheet on file, else ValueError (OSError when the list is missing) naming a
    person by key, never a handle, or a sheet's file and line."""
    if stat.S_IMODE(file.stat().st_mode) & 0o077:
        raise ValueError(f"{file} must be 0600: others can read it")
    raw, seen, people = load_toml(file), dict[str, str](), dict[str, Person]()
    for key, p in dict(raw.get("people") or {}).items():
        hs = p.get("handles") if isinstance(p, dict) else None
        if not isinstance(p, dict) or not isinstance(hs, list) or not hs:
            raise ValueError(f"people.{key}: handles must be a list of numbers or addresses")
        for n in (norm(h) for h in hs):
            if not n or seen.setdefault(n, key) != key:
                raise ValueError(f"people.{key}: a handle is not a number or an address, or is someone else's too")
        if Path(at := str(p.get("sheet") or "")).is_absolute() or ".." in Path(at).parts:
            raise ValueError(f"people.{key}: sheet must be a path inside {file.parent.name}/")
        people[key] = Person(key, str(p.get("first") or key), tuple(norm(h) for h in hs), at, p.get("release") is True)
    return People(str(dict(raw.get("owner") or {}).get("name") or ""), people,
                  {k: sheet(sheet_file(file, p)) for k, p in people.items()} if sheets else {})


def sheet_file(file: Path, p: Person) -> Path:
    return file.parent / (p.sheet or f"people/{p.key}.toml")


def line_of(text: str, key: str, value: str = "") -> int:
    """The line `key` is set on (or opens its table), or the first after it holding `value` quoted: where an error is."""
    lines = text.splitlines()
    at = next((i for i, x in enumerate(lines) if re.match(rf"\s*{key}\s*=|\s*\[{key}\]", x)), 0)
    return 1 + next((i for i in range(at, len(lines)) if value and f'"{value}"' in lines[i]), at)


def sheet(file: Path) -> Sheet:
    """A person sheet, else ValueError naming its file and line; none on file is an empty sheet."""
    try:
        if stat.S_IMODE(file.stat().st_mode) & 0o077:
            raise ValueError(f"{file} must be 0600: others can read it")
        raw = tomllib.loads(text := file.read_text())
    except FileNotFoundError:
        return Sheet()
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{file}: {e}") from None              # its words name the line

    def bad(key: str, why: str, value: str = "") -> ValueError:
        return ValueError(f"{file}:{line_of(text, key, value)}: {key}: {why}")
    if extra := [k for k in raw if k not in KEYS]:
        raise bad(extra[0], f"not a sheet's; a sheet has {', '.join(KEYS)}")
    op, kn, voice, pace = raw.get("open", []), raw.get("knows", []), raw.get("voice", {}), raw.get("pacing", {})
    if not isinstance(op, list) or (x := next((c for c in op if c not in words.TEXTING_CLASSES), None)) is not None:
        raise bad("open", f"{x!r} is not a class; the classes: {', '.join(words.TEXTING_CLASSES)}", str(x))
    if not isinstance(kn, list) or not all(isinstance(k, dict) and set(k) <= {"fact", "source"} and isinstance(k.get("fact"), str)
                                           and k["fact"].strip() and isinstance(k.get("source", ""), str) for k in kn):
        raise bad("knows", 'each is {fact = "...", source = "..."}')
    if raw.get("night", "hold") not in NIGHT:
        raise bad("night", "hold or answer")
    if not isinstance(rs := raw.get("reacts", []), list) or not set(rs) <= set(REACTS):
        raise bad("reacts", "a list of love and like")
    if not (isinstance(share := raw.get("unanswered_share", 0.0), (int, float)) and not isinstance(share, bool) and 0 <= share <= 1):
        raise bad("unanswered_share", "a number from 0 to 1")
    if not isinstance(pace, dict) or not all(k in PACING and isinstance(v, list) and len(v) == 2 and all(
            isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 for n in v) and v[0] <= v[1] for k, v in pace.items()):
        raise bad("pacing", f"each of {', '.join(PACING)} is [p25, p75], two numbers from 0 up")
    if not isinstance(voice, dict) or set(voice) - {"card"} or not isinstance(voice.get("card", ""), str):
        raise bad("voice", "card, the voice card's words")
    return Sheet(tuple(dict.fromkeys(op)), tuple((k["fact"], k.get("source", "")) for k in kn), raw.get("night", "hold"),
                 tuple(dict.fromkeys(rs)), float(share), {k: (float(v[0]), float(v[1])) for k, v in pace.items()},
                 voice.get("card", ""))


def sheet_text(s: Sheet) -> str:
    """A sheet as `change` writes it, every key in one order; the card a literal block when it can be one, word for word."""
    block = "'''" not in s.card and not s.card.endswith("'") and not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", s.card)
    knows = "".join(f"\n  {{fact = {toml_value(f)}, source = {toml_value(src)}}}," for f, src in s.knows)
    return "\n".join([f"open = {toml_value(list(s.open))}", f"knows = [{knows}{chr(10) if knows else ''}]", f"night = {toml_value(s.night)}",
                      f"reacts = {toml_value(list(s.reacts))}", f"unanswered_share = {s.unanswered_share!r}", "", "[pacing]",
                      *(f"{k} = [{a!r}, {b!r}]" for k, (a, b) in s.pacing.items()), "", "[voice]",
                      f"card = '''\n{s.card}'''" if block else f"card = {toml_value(s.card)}"]) + "\n"


def check(file: Path) -> list[str]:
    """`yerk texting sheets --check`: the list and every sheet it names, read and never sheets.log: one line naming the file
    (and the line) for the list or a sheet that does not load, a sheet missing, and a file in people/ no one names."""
    try:
        who = load(file, sheets=False)
    except (OSError, ValueError) as e:
        return [f"{file}: {getattr(e, 'strerror', None) or e}"]
    bad, named = [], {sheet_file(file, p) for p in who.people.values()}
    for p in who.people.values():
        try:
            if not sheet_file(file, p).is_file():
                bad.append(f"{sheet_file(file, p)}: missing, so {p.key} has no voice card and every class held")
            sheet(sheet_file(file, p))
        except ValueError as e:
            bad.append(str(e))
    return bad + [f"{x}: no one on the list names it" for x in sorted((file.parent / "people").glob("*")) if x not in named]


def release_text(text: str, key: str, on: bool) -> str:
    """The list's words with `key`'s standing release set, every other line as it was."""
    lines, v = text.splitlines(keepends=True), "true" if on else "false"
    if (head := next((i for i, x in enumerate(lines) if x.strip() == f"[people.{key}]"), None)) is None:
        return text                               # not written as a table of its own: as it was, which the read-back refuses
    end = next((i for i in range(head + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
    if (at := next((i for i in range(head + 1, end) if re.match(r"\s*release\s*=", lines[i])), None)) is None:
        return "".join([*lines[:head + 1], f"release = {v}\n", *lines[head + 1:]])
    return "".join([*lines[:at], re.sub(r"^(\s*release\s*=\s*)\w+", rf"\g<1>{v}", lines[at]), *lines[at + 1:]])


def change(file: Path, key: str, op: str, value: str, source: str) -> tuple[bool, str]:
    """One change the owner ordered (`source`: where they said it): `open` or `close` a class on `key`'s sheet, `knows` or
    `forget` a fact, `release` on or off on the list. Written whole through write_private in the file's own mode, read
    back, and one line of sheets.log; (False, the one line why) changes nothing."""
    with LOCK:
        who = load(file)
        if (p := who.people.get(key)) is None:
            return False, words.TEXTING_NO_ONE.format(key=key, keys=", ".join(who.people))
        to, s, v = file if op == "release" else sheet_file(file, p), who.sheets[key], " ".join(value.split())
        before, want = to.read_bytes() if to.is_file() else b"", s
        if op == "release":
            if v not in ("on", "off"):
                return False, words.TEXTING_ON_OFF
            new, same = release_text(before.decode(), key, v == "on"), (v == "on") == p.release
        else:
            if op in ("open", "close") and v not in words.TEXTING_CLASSES:
                return False, words.TEXTING_NOT_CLASS.format(value=v, classes=", ".join(words.TEXTING_CLASSES))
            if op in ("open", "close"):
                want = replace(s, open=tuple(c for c in s.open if c != v) + ((v,) if op == "open" else ()))
            elif op == "knows" and v and v.casefold() not in (f.casefold() for f, _ in s.knows):
                want = replace(s, knows=(*s.knows, (v, source)))
            elif op == "forget":
                want = replace(s, knows=tuple(k for k in s.knows if k[0].casefold() != v.casefold()))
            new, same = sheet_text(want), set(want.open) == set(s.open) and want.knows == s.knows
        if same:
            return False, words.TEXTING_SAME.format(first=p.first, op=op, value=v)
        to.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(to, new, stat.S_IMODE(to.stat().st_mode) if before else 0o600)
        try:                                      # read back: what does not load, or not as asked, is put back
            back = load(file)
            if (back.people[key].release != (v == "on")) if op == "release" else (back.sheets[key] != want):
                raise ValueError(to.name)
        except ValueError:
            write_private(to, before, stat.S_IMODE(to.stat().st_mode)) if before else to.unlink()
            return False, words.TEXTING_UNREAD.format(file=to.name)
        row = {"at": datetime.now().astimezone().isoformat(timespec="seconds"), "person": key, "op": op, "value": v, "source": source,
               "file": str(to.relative_to(file.parent)), "before": hashlib.sha256(before).hexdigest() if before else "",
               "after": hashlib.sha256(to.read_bytes()).hexdigest()}
        with os.fdopen(os.open(file.parent / LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True, words.TEXTING_CHANGED.format(first=p.first, op=op, value=v)
