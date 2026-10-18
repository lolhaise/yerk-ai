"""Loads the Claude Code accounts a room can run on, rests one that fails and picks the next. Each account is one line in
optionally a one-year token file. One code path for every account; nothing else names one."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import load_toml
from .lifted.cooldowns import cooldown_until
from .lifted.expiry import DAY_MS, login_state

DROP_PREFIXES = ("CLAUDE", "ANTHROPIC")
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"     # what `claude setup-token` prints is read from here
TOKEN_DAYS = 365
WARN_DAYS = 30


@dataclass(frozen=True)
class Account:
    name: str
    config_dir: Path
    order: int = 0
    token_file: Path | None = None


def load_accounts(path: Path) -> list[Account]:
    """[accounts] name = { config_dir, order, token }; tried in ascending order, then file order.
    `token` is relative to the file's folder; it defaults to secrets/claude-<name>.token."""
    rows = load_toml(path).get("accounts", {})
    out = [Account(name, Path(r["config_dir"]).expanduser(), int(r.get("order", i)),
                   path.parent / str(r.get("token", f"secrets/claude-{name}.token")))
           for i, (name, r) in enumerate(rows.items())]
    return sorted(out, key=lambda a: a.order)


def from_dir(config_dir: str | Path, name: str | None = None) -> Account:
    p = Path(config_dir).expanduser()
    return Account(name or p.name.lstrip(".") or "default", p)


def read_token(account: Account) -> tuple[str | None, float | None]:
    """(token, issued epoch seconds) from the account's token file, or (None, None). The file is
    what `yerk accounts token NAME` wrote ({"token", "issued"}); a bare pasted token also reads,
    issued then being the file's modification time. The token is never logged or printed."""
    f = account.token_file
    if not f or not f.is_file():
        return None, None
    raw = f.read_text().strip()
    try:
        d = json.loads(raw)
        tok, issued = str(d["token"]).strip(), datetime.fromisoformat(str(d["issued"])).timestamp()
    except (ValueError, KeyError, TypeError):
        tok, issued = raw.splitlines()[0].strip() if raw else "", f.stat().st_mtime
    return (tok or None), issued


def store_token(account: Account, token: str, issued: date | None = None) -> Path:
    """Write the token file 0600 (its folder 0700), with today's date as the issue date."""
    assert account.token_file
    account.token_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = account.token_file.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"token": token.strip(), "issued": (issued or date.today()).isoformat()}, fh)
    os.replace(tmp, account.token_file)
    return account.token_file


TOKEN_SHAPE = re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")


def mint(account: Account, claude: str = "claude") -> tuple[int, bool]:
    """`claude setup-token` for this account in a pseudo-terminal: the browser flow's text is
    shown as it comes, the token itself is swapped for "[token stored]" and written to the
    account's token file, so it never reaches a screen, a log or a transcript. Returns (exit
    status, stored)."""
    import pty
    found: list[bytes] = []
    held, eof = [b""], [False]

    def swap(m: re.Match[bytes]) -> bytes:
        found.append(m.group())
        return b"[token stored]"

    def read(fd: int) -> bytes:
        if eof[0]:
            return b""
        try:
            data = os.read(fd, 1024)
        except OSError:
            data = b""
        if not data:
            eof[0], rest, held[0] = True, held[0], b""
            return TOKEN_SHAPE.sub(swap, rest)
        buf, hold = held[0] + data, b""
        i = buf.rfind(b"sk-ant-")
        if i != -1 and re.fullmatch(rb"[A-Za-z0-9_-]*", buf[i + 7:]):   # the token may go on
            buf, hold = buf[:i], buf[i:]
        held[0] = hold
        return TOKEN_SHAPE.sub(swap, buf)

    env = {k: v for k, v in account_env(account).items() if k != TOKEN_ENV}
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        status = pty.spawn([claude, "setup-token"], read)
    finally:
        os.environ.clear()
        os.environ.update(old)
    if found:
        store_token(account, found[-1].decode())
    return status, bool(found)


def token_info(account: Account, now: float | None = None) -> dict[str, Any]:
    """Age, days to expiry and state (ok, due inside WARN_DAYS, dead, none) of the token."""
    tok, issued = read_token(account)
    if not tok or issued is None:
        return {"token": "none"}
    now = time.time() if now is None else now
    expires_ms = int((issued + TOKEN_DAYS * 86400) * 1000)
    state, left = login_state(expires_ms, True, int(now * 1000), WARN_DAYS)
    return {"token": state, "issued": date.fromtimestamp(issued).isoformat(),
            "age_days": int((now - issued) * 1000 // DAY_MS), "days_left": int(left)}


def account_env(account: Account, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a Claude Code process on this account. Inherited CLAUDE* and
    ANTHROPIC* variables are dropped, CLAUDE_CONFIG_DIR is set explicitly, and the account's
    one-year token, when its file exists, goes in CLAUDE_CODE_OAUTH_TOKEN (it outranks the config
    folder's own login); else the folder's login is used. Auto memory stays off: memory is
    Yerk AI's job."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith(DROP_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    tok, _ = read_token(account)
    if tok:
        env[TOKEN_ENV] = tok
    return env


class Cooldowns:
    """Resting accounts, per account and failure kind, with doubling backoff, in one JSON file
    in the state folder: {account: {kind: {until, repeats}}}. A restart keeps them, a hand edit
    (`yerk accounts cool`) is seen at the next pick, and a delivered reply clears the account."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.mtime: int | None = None
        self._refresh()

    def _refresh(self) -> None:
        if not self.path:
            return
        try:
            m = self.path.stat().st_mtime_ns
        except OSError:
            return
        if m != self.mtime:
            raw = json.loads(self.path.read_text() or "{}")
            # a Layer 1 file kept {account: {until, repeats}}
            self.data = {k: ({"legacy": v} if "until" in v else v) for k, v in raw.items()}
            self.mtime = m

    def until(self, name: str, now: float | None = None) -> float | None:
        self._refresh()
        return cooldown_until({k: v["until"] for k, v in self.data.get(name, {}).items()}, None, now)

    def fail(self, name: str, kind: str, seconds: Callable[[int], float], now: float | None = None) -> float:
        """Rest `name` for seconds(repeats of this kind so far); returns when the rest ends."""
        self._refresh()
        now = time.time() if now is None else now
        kinds = self.data.setdefault(name, {})
        repeats = int(kinds.get(kind, {}).get("repeats", 0))
        until = now + seconds(repeats)
        kinds[kind] = {"until": until, "repeats": repeats + 1, "at": now}
        self._save()
        return until

    def set(self, name: str, kind: str, until: float) -> None:
        """A rest set by hand; the repeat count is kept."""
        self._refresh()
        kinds = self.data.setdefault(name, {})
        kinds[kind] = {**kinds.get(kind, {"repeats": 0}), "until": until, "at": time.time()}
        self._save()

    def clear(self, name: str) -> None:
        self._refresh()
        if self.data.pop(name, None) is not None:
            self._save()

    def show(self, name: str, now: float | None = None) -> list[dict[str, Any]]:
        """The account's active rests, longest first."""
        self._refresh()
        now = time.time() if now is None else now
        rows = [{"kind": k, "until": v["until"], "repeats": v.get("repeats", 0)}
                for k, v in self.data.get(name, {}).items() if v.get("until", 0) > now]
        return sorted(rows, key=lambda r: -r["until"])

    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
            self.mtime = self.path.stat().st_mtime_ns


def pick(accounts: list[Account], cooldowns: Cooldowns, now: float | None = None) -> Account:
    """The first account in order that is not resting; when every one rests, the last anyway."""
    for a in accounts:
        if cooldowns.until(a.name, now) is None:
            return a
    return accounts[-1]
