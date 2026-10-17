"""An account is a name and a Claude Code config folder: one code path for every account."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_toml
from .lifted.cooldowns import cooldown_until

DROP_PREFIXES = ("CLAUDE", "ANTHROPIC")


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path


def load_accounts(path: Path) -> list[Account]:
    return [Account(a["name"], Path(a["config_dir"]).expanduser()) for a in load_toml(path).get("account", [])]


def from_dir(config_dir: str | Path, name: str | None = None) -> Account:
    p = Path(config_dir).expanduser()
    return Account(name or p.name.lstrip(".") or "default", p)


def account_env(account: Account, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a Claude Code process on this account. Only the account's own login
    is used: inherited CLAUDE* and ANTHROPIC* variables are dropped and CLAUDE_CONFIG_DIR is set
    explicitly. Claude Code's auto memory stays off, because memory is Yerk AI's job."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith(DROP_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


class Cooldowns:
    """Resting accounts with doubling backoff, kept in one JSON file. A success clears."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        if path and path.is_file():
            self.data = json.loads(path.read_text())

    def until(self, name: str, now: float | None = None) -> float | None:
        return cooldown_until({k: v["until"] for k, v in self.data.items()}, name, now)

    def fail(self, name: str, seconds: Callable[[int], float], now: float | None = None) -> float:
        """Rest `name` for seconds(repeats so far); returns when the rest ends."""
        now = time.time() if now is None else now
        repeats = int(self.data.get(name, {}).get("repeats", 0))
        until = now + seconds(repeats)
        self.data[name] = {"until": until, "repeats": repeats + 1}
        self._save()
        return until

    def clear(self, name: str) -> None:
        if self.data.pop(name, None) is not None:
            self._save()

    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)


def pick(accounts: list[Account], cooldowns: Cooldowns, now: float | None = None) -> Account:
    """The first account in order that is not resting; when every one rests, the last anyway."""
    for a in accounts:
        if cooldowns.until(a.name, now) is None:
            return a
    return accounts[-1]
