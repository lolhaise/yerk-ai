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

from .config import load_toml, write_private
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
    bill_day: int | None = None       # the day of the month the plan bills (`yerk status` shows the next one)


def load_accounts(path: Path) -> list[Account]:
    """[accounts] name = { config_dir, order, token, bill_day }; tried in ascending order, then file order.
    `token` is relative to the file's folder; it defaults to secrets/claude-<name>.token. `bill_day`, optional,
    is a day of the month, 1 to 31; any other value is left out."""
    rows = load_toml(path).get("accounts", {})
    out = [Account(name, Path(r["config_dir"]).expanduser(), int(r.get("order", i)),
                   path.parent / str(r.get("token", f"secrets/claude-{name}.token")),
                   d if type(d := r.get("bill_day")) is int and 1 <= d <= 31 else None)
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
    write_private(account.token_file, json.dumps({"token": token.strip(), "issued": (issued or date.today()).isoformat()}))
    return account.token_file


TOKEN_SHAPE = re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")
MINT_COLUMNS = 400   # wider than any token line: the setup screen wraps at the terminal width


def spawn_wide(argv: list[str], read: Callable[[int], bytes], cols: int = MINT_COLUMNS) -> int:
    """pty.spawn with the child's terminal declared `cols` wide before it starts, so a line it prints
    is never wrapped mid-string. Claude Code's setup screen wraps the token at the terminal width,
    80 by default, and the wrapped tail escaped the swap below (account one, 2026-10-17: 79 of the
    token's characters stored, the rest shown). Returns the child's wait status, as pty.spawn does."""
    import fcntl
    import pty
    import struct
    import termios
    import tty
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, cols, 0, 0))
    try:
        mode = termios.tcgetattr(pty.STDIN_FILENO)
        tty.setraw(pty.STDIN_FILENO)
        restore = True
    except termios.error:
        restore = False
    try:
        pty._copy(fd, read, pty._read)   # type: ignore[attr-defined]  # pty.spawn's own loop; the size is the one difference
    finally:
        if restore:
            termios.tcsetattr(pty.STDIN_FILENO, termios.TCSAFLUSH, mode)
    os.close(fd)
    return os.waitpid(pid, 0)[1]


def mint(account: Account, claude: str = "claude") -> tuple[int, bool]:
    """`claude setup-token` for this account in a pseudo-terminal: the browser flow's text is
    shown as it comes, the token itself is swapped for "[token stored]" and written to the
    account's token file, so it never reaches a screen, a log or a transcript. Returns (exit
    status, stored)."""
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
        status = spawn_wide([claude, "setup-token"], read)
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
    Yerk AI's job. Its requests go out under Yerk AI's own name, `yerk`, not the `claude -p`
    default `sdk-cli`, which a plan's meters count heavier for the same work. Artifacts stay off:
    any name but `sdk-*` would turn them on, adding a Scratchpad line to every request."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith(DROP_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(account.config_dir)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env["CLAUDE_CODE_ENTRYPOINT"] = "yerk"
    env["CLAUDE_CODE_DISABLE_ARTIFACT"] = "1"
    tok, _ = read_token(account)
    if tok:
        env[TOKEN_ENV] = tok
    return env


class Cooldowns:
    """Resting accounts, per account and failure kind, with doubling backoff, in one JSON file
    in the state folder: {account: {kind or kind:model: {until, repeats}}}. A restart keeps them, a hand edit
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

    def until(self, name: str, now: float | None = None, model: str = "") -> float | None:
        """When the account's rest ends, None when it is free; for `model`, a rest keyed to another model does not count."""
        self._refresh()
        rests = {k: v["until"] for k, v in self.data.get(name, {}).items() if not model or k.partition(":")[2] in ("", model)}
        return cooldown_until(rests, None, now)

    def fail(self, name: str, kind: str, seconds: Callable[[int], float], now: float | None = None, model: str = "",
             until: float | None = None) -> float:
        """Rest `name` for seconds(repeats of this kind so far), or until `until` when the failure named when its limit
        resets; a `model` keys the rest to that model alone (kind:model, a weekly meter being per model). Returns the end."""
        self._refresh()
        now = time.time() if now is None else now
        kinds, key = self.data.setdefault(name, {}), f"{kind}:{model}" if model else kind
        repeats = int(kinds.get(key, {}).get("repeats", 0))
        until = until if until and until > now else now + seconds(repeats)
        kinds[key] = {"until": until, "repeats": repeats + 1, "at": now}
        self._save()
        return until

    def set(self, name: str, kind: str, until: float) -> None:
        """A rest set by hand; the repeat count is kept."""
        self._refresh()
        kinds = self.data.setdefault(name, {})
        kinds[kind] = {**kinds.get(kind, {"repeats": 0}), "until": until, "at": time.time()}
        self._save()

    def clear(self, name: str, model: str = "") -> None:
        """Every rest of the account goes; with a `model`, those keyed to it or to no model (a reply on one model says
        nothing about another model's meter)."""
        self._refresh()
        kept = {k: v for k, v in self.data.get(name, {}).items() if model and k.partition(":")[2] not in ("", model)}
        if name in self.data and self.data[name] != kept:
            self.data[name] = kept
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


def pick(accounts: list[Account], cooldowns: Cooldowns, now: float | None = None, first: str = "", model: str = "") -> Account:
    """The first account in order not resting (for `model`, when one is named), `first` (a room's pinned one) ahead of the rest;
    when every one rests, the last anyway."""
    accounts = sorted(accounts, key=lambda a: a.name != first)
    for a in accounts:
        if cooldowns.until(a.name, now, model) is None:
            return a
    return accounts[-1]
