# Adapted from the author's tools/reason_codes.py (2026-08-26): the retry taxonomy
# (transient, compact then retry, never) and the first-match classifier in which an unmatched
# failure never retries. The codes are cut to what a Claude Code turn can fail with, and the
# patterns moved into failures.toml so the table lives in one place.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""Why did a turn fail: one table, first match wins, an unmatched failure is fatal."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CLASSES = ("transient", "overflow", "auth", "quota", "refusal", "fatal")
ACTIONS = ("rerun", "compact_then_rerun", "hop_account", "fresh_next_turn", "fresh_with_recap", "stop")


@dataclass(frozen=True)
class Failure:
    cls: str            # one of CLASSES
    action: str         # one of ACTIONS
    kind: str | None    # cooldown kind (rate_limit, billing, auth) or None
    source: str         # "type:<value>", "event:<name>", "pattern:<name>" or "none"


class FailureTable:
    def __init__(self, data: dict[str, Any]):
        self.actions: dict[str, str] = data["actions"]
        self.types: dict[str, dict[str, Any]] = data["types"]
        self.events: dict[str, dict[str, Any]] = data.get("events", {})
        self.cooldown: dict[str, int] = data.get("cooldown", {})
        self.rerun: dict[str, Any] = data.get("rerun", {"max": 0, "wait": [0]})
        self.patterns = [(p["name"], p["class"], p.get("kind"), p.get("action"), re.compile(p["regex"], re.I))
                         for p in data.get("patterns", [])]

    @classmethod
    def load(cls, path: Path) -> "FailureTable":
        return cls(tomllib.loads(path.read_text()))

    def _entry(self, e: dict[str, Any], source: str) -> Failure:
        return Failure(e["class"], e.get("action") or self.actions[e["class"]], e.get("kind"), source)

    def event(self, name: str) -> Failure | None:
        e = self.events.get(name)
        return self._entry(e, f"event:{name}") if e else None

    def classify(self, error_type: str | None = None, text: str = "", events: list[str] | None = None) -> Failure:
        """Claude Code's error type, then the stream's events, then the text, then fatal."""
        if (t := self.types.get(error_type or "")) and not t.get("text_first"):
            return self._entry(t, f"type:{error_type}")
        for name in events or []:
            if f := self.event(name):
                return f
        for name, c, kind, action, rx in self.patterns:
            if text and rx.search(text):
                return Failure(c, action or self.actions[c], kind, f"pattern:{name}")
        return self._entry(t, f"type:{error_type}") if t else Failure("fatal", self.actions["fatal"], None, "none")

    def cooldown_seconds(self, kind: str, repeats: int) -> float:
        """Base seconds for the kind, doubled per earlier repeat, capped at `max`."""
        base = float(self.cooldown.get(kind, 0))
        return min(base * (2 ** max(repeats, 0)), float(self.cooldown.get("max", 86400)))

    def rerun_wait(self, n: int) -> float | None:
        """Seconds before transient rerun number n (0-based), or None when the reruns are spent."""
        if n >= int(self.rerun.get("max", 0)):
            return None
        waits = list(self.rerun.get("wait") or [0])
        return float(waits[min(n, len(waits) - 1)])
