"""Checks once a day that each Claude account really works, with one tiny model call, and that its login is still the same account.

Health is proved by a real model call, never by the login status command alone (a login can
read as signed in while every model call is refused). Identity is the login's email as Claude
Code reports it (`claude auth status`), kept only as a short fingerprint: pinned the first time
it is seen, a change or two accounts on one login fail the check. An account on a one-year token
is also checked for which account the token belongs to, without any email and without reading
any credential file: the token's call and a second call on the config folder's own login each
report their 5-hour and weekly reset times in the stream, and they must match (each account's
windows reset at its own times). Each call is a ledger row
(table health); a refused login rests the account the way a room's failure would, and a pass
clears it. The call runs without session persistence, so it leaves no transcript."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .accounts import TOKEN_ENV, Account, Cooldowns, account_env
from .engine import Turn, claude_bin
from .ledger import Ledger
from .lifted.classify import FailureTable
from .meters import Meters

PROMPT = "Reply with the single word OK."


def fingerprint(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def login_identity(env: dict[str, str], timeout: float = 30) -> tuple[str, str | None]:
    """(login method, identity fingerprint or None) from `claude auth status`; nothing printed."""
    try:
        p = subprocess.run([claude_bin(), "auth", "status"], env=env, capture_output=True, text=True, timeout=timeout)
        d = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "unreadable", None
    email = d.get("email") or (d.get("account") or {}).get("email") or d.get("emailAddress")
    method = str(d.get("authMethod") or ("none" if p.returncode else "unknown"))
    return method, (fingerprint(str(email)) if email else None)


def same_account(a: dict[str, Any] | None, b: dict[str, Any] | None, slack: float = 120) -> bool | None:
    """Do two streams' usage windows have the same reset times? None: nothing to compare."""
    pairs = [(float(a[w]["resetsAt"]), float(b[w]["resetsAt"])) for w in ("five_hour", "seven_day")
             if a and b and (a.get(w) or {}).get("resetsAt") and (b.get(w) or {}).get("resetsAt")]
    return None if not pairs else all(abs(x - y) <= slack for x, y in pairs)


def model_call(account: Account, model: str, failures: FailureTable, meters: Meters | None,
               timeout: float, env: dict[str, str] | None = None) -> tuple[Turn, str]:
    """One -p turn in stream-json, read with the room's own Turn; returns (turn, stderr tail)."""
    argv = [claude_bin(), "-p", PROMPT, "--model", model, "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config", "--tools", ""]
    turn = Turn(started=time.time())
    with tempfile.TemporaryDirectory(prefix="yerk-health-") as cwd:
        try:
            p = subprocess.run(argv, env=env or account_env(account), cwd=cwd, capture_output=True, text=True,
                               timeout=timeout)
            out, err = p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            out, err, turn.outcome = str(e.stdout or ""), "health call timed out", "timeout"
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        turn.observe(time.time(), ev)
        if meters and ev.get("type") == "rate_limit_event" and turn.windows:
            meters.seen(account.name, turn.windows)
    turn.ended = turn.ended or time.time()
    if turn.outcome == "running":
        turn.outcome = "exited"
    if turn.outcome == "ok" and not turn.text.strip():
        turn.outcome = "empty"
    if turn.outcome != "ok":
        turn.failure = failures.classify(turn.error_type, " ".join([turn.text, *turn.errors, err[-2000:]]), turn.flags)
    return turn, err[-300:]


def check(accounts: list[Account], failures: FailureTable, ledger: Ledger, cooldowns: Cooldowns, state: Path,
          model: str = "haiku", timeout: float = 120, meters: Meters | None = None) -> list[dict[str, Any]]:
    """Run the health call on each account; returns the ledger rows written."""
    pins_path = state / "identities.json"
    try:
        pins: dict[str, str] = json.loads(pins_path.read_text())
    except (OSError, ValueError):
        pins = {}
    rows = []
    for a in accounts:
        env = account_env(a)
        tokened = TOKEN_ENV in env
        method, ident = login_identity({k: v for k, v in env.items() if k != TOKEN_ENV})   # the folder's login
        login = login_identity(env)[0] if tokened else method
        turn, _err = model_call(a, model, failures, meters, timeout)
        ok = turn.outcome == "ok"
        note, ident_ok = [], None
        if ident:
            pinned = pins.setdefault(a.name, ident)
            twins = [n for n, v in pins.items() if v == ident and n != a.name]
            ident_ok = int(pinned == ident and not twins)
            if pinned != ident:
                note.append("identity changed since first seen")
            if twins:
                note.append("same login as " + ",".join(twins))
        else:
            note.append(f"no identity in the login status ({method})")
        if tokened and ok:
            folder, _ = model_call(a, model, failures, None, timeout,
                                   env={k: v for k, v in env.items() if k != TOKEN_ENV})   # the folder's own login
            same = same_account(turn.windows, folder.windows)
            if same is False:
                ident_ok = 0
                note.append("the token answers as another account (its reset times differ from the folder login's)")
            elif same is None:
                ident_ok = None if ident_ok else ident_ok
                note.append("token identity unverified (no reset times to compare)")
        f = turn.failure
        if ok:
            cooldowns.clear(a.name)
        elif f and f.action == "hop_account":
            kind = f.kind or "auth"
            cooldowns.fail(a.name, kind, lambda r: failures.cooldown_seconds(kind, r))
        row = {"account": a.name, "config_dir": str(a.config_dir), "ok": int(ok), "failure": f.cls if f else None,
               "source": f.source if f else None, "login": login, "identity": ident, "identity_ok": ident_ok,
               "model": ",".join(turn.models) or model, "ms": turn.ms(turn.ended), "note": "; ".join(note) or None}
        ledger.health(**row)
        rows.append(row)
    tmp = pins_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(pins, fh)
    os.replace(tmp, pins_path)
    return rows
