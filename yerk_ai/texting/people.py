"""The reply list, the people texting may answer in the owner's name: `<private>/<[texting] people>` (default texting/people.toml), owner-only,
name`, then one `[people.<key>]` a person: `first`, `handles` (numbers or Apple ID addresses), `sheet`, `release`. A
handle is held in texts.handle's form, an address lowercased, so a chat's identifier matches it however Messages wrote it."""
from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import texts
from ..config import load_toml

PEOPLE = "texting/people.toml"


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
class People:
    owner: str
    people: dict[str, Person]

    def by_handle(self, h: Any) -> Person | None:
        n = norm(h)
        return next((p for p in self.people.values() if n and n in p.handles), None)


def path(private: Path, conf: dict[str, Any]) -> Path:
    return private / str(conf.get("people") or PEOPLE)


def load(file: Path) -> People:
    """The reply list, else ValueError (OSError when it is missing) naming a person by key, never a handle."""
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
        people[key] = Person(key, str(p.get("first") or key), tuple(norm(h) for h in hs), str(p.get("sheet") or ""),
                             p.get("release") is True)
    return People(str(dict(raw.get("owner") or {}).get("name") or ""), people)
