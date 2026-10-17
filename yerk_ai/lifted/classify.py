# Adapted from the author's tools/reason_codes.py (2026-08-26): the retry taxonomy
# (transient, compact then retry, never) and the first-match classifier in which `unknown`
# never retries. The codes are cut to what a Claude Code turn can fail with, and the
# patterns moved into failures.toml so the table lives in one place.
# Copyright (c) 2026 Eric McLaren. MIT License. See NOTICE.
"""Why did a turn fail: one table, first match wins, unknown never retries."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Failure:
    cls: str            # transient, overflow, auth, quota, config, unknown
    action: str         # rerun, compact_then_rerun, hop_account, stop
    kind: str | None    # cooldown kind (rate_limit, billing, auth) or None
    source: str         # "type:<value>", "pattern:<index>" or "none"


class FailureTable:
    def __init__(self, data: dict[str, Any]):
        self.actions: dict[str, str] = data["actions"]
        self.types: dict[str, str] = data["types"]
        self.type_kinds: dict[str, str] = data.get("type_kinds", {})
        self.cooldown: dict[str, int] = data.get("cooldown", {})
        self.patterns = [(p["class"], p.get("kind"), re.compile(p["regex"], re.I))
                         for p in data.get("patterns", [])]

    @classmethod
    def load(cls, path: Path) -> "FailureTable":
        return cls(tomllib.loads(path.read_text()))

    def classify(self, error_type: str | None = None, text: str = "") -> Failure:
        """Claude Code's own error type wins; `unknown` and unlisted types fall through to
        the text patterns, then to unknown."""
        if error_type and error_type != "unknown" and error_type in self.types:
            c = self.types[error_type]
            return Failure(c, self.actions[c], self.type_kinds.get(error_type), f"type:{error_type}")
        for i, (c, kind, rx) in enumerate(self.patterns):
            if text and rx.search(text):
                return Failure(c, self.actions[c], kind, f"pattern:{i}")
        return Failure("unknown", self.actions["unknown"], None, "none")

    def cooldown_seconds(self, kind: str, repeats: int) -> float:
        """Base seconds for the kind, doubled per earlier repeat, capped at `max`."""
        base = float(self.cooldown.get(kind, 0))
        return min(base * (2 ** max(repeats, 0)), float(self.cooldown.get("max", 86400)))
