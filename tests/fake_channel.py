"""A fake chat channel, in place of Discord, for the tests that run rooms under yerkd, the program that keeps the rooms running:
replies go to stdout as JSON lines and the progress card to stderr. It is the console channel yerkd had until shrink-1, kept here."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any


class ConsoleChannel:
    """A room on the terminal: replies on stdout as JSON lines, the card on stderr."""

    def __init__(self) -> None:
        self.n = 0

    def _out(self, obj: dict[str, Any]) -> list[str]:
        self.n += 1
        print(json.dumps({"t": round(time.time(), 3), **obj}, ensure_ascii=False), flush=True)   # the room's own words
        return [f"console-{self.n}"]

    def open_card(self, room: str, text: str, kind: str = "turn") -> None:
        self.progress(room, text)

    def progress(self, room: str, text: str) -> None:
        print(f"[card] {text}", file=sys.stderr, flush=True)

    def reply(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        if card:
            self.progress(room, card)
        return self._out({"room": room, "reply": text, "inbound": inbound, **({"card": card} if card else {})})

    def notice(self, room: str, text: str, inbound: list[str], card: str | None = None, key: str | None = None) -> list[str]:
        if card:
            self.progress(room, card)
        return self._out({"room": room, "notice": text, "inbound": inbound})

    def silent(self, room: str, inbound: list[str], card: str, key: str | None = None) -> list[str]:
        self.progress(room, card)                 # nothing on stdout: a final of exactly NO_REPLY posts nothing
        return []

    async def send_text(self, room: str, text: str, key: str | None = None, inbound: str = "") -> list[str]:
        return self._out({"room": room, "send": text})

    async def notify(self, room: str, text: str) -> list[str]:
        return self._out({"room": room, "notice": text, "inbound": []})

    async def send_file(self, room: str, path: Path, caption: str = "", key: str | None = None, inbound: str = "") -> list[str]:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return self._out({"room": room, "file": str(path), "caption": caption})

    async def send_voice(self, room: str, path: Path, meta: dict[str, Any], key: str | None = None, inbound: str = "") -> tuple[list[str], str]:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return self._out({"room": room, "voice": str(path), "duration": meta["duration"]}), ""

    async def worker_card(self, room: str, key: str, text: str, message_id: str | None = None) -> str | None:
        print(f"[{key}] {text}", file=sys.stderr, flush=True)
        return message_id or f"console-{key}"

    async def worker_settle(self, room: str, key: str, text: str, how: str, message_id: str | None = None) -> str | None:
        return await self.worker_card(room, key, f"{text}\n({how})", message_id)
