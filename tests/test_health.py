"""Checks the daily health call, a tiny model turn per account, and `yerk accounts` / `yerk status`, on the fake claude."""
import io
import json
from pathlib import Path

import pytest

from yerk_ai import cli, health
from yerk_ai.accounts import Account, Cooldowns, load_accounts, store_token
from yerk_ai.config import table
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.meters import Meters

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "argv.jsonl"))
    priv, state = tmp_path / "private", tmp_path / "state"
    priv.mkdir()
    state.mkdir()
    lines = [f'{n} = {{ config_dir = "{tmp_path / ("cfg-" + n)}", order = {i} }}'
             for i, n in enumerate(["one", "two", "three", "four"], 1)]
    (priv / "accounts.toml").write_text("[accounts]\n" + "\n".join(lines) + "\n")
    return priv, state, tmp_path


def run_check(priv, state, accounts=None):
    accts = accounts or load_accounts(priv / "accounts.toml")
    return health.check(accts, FailureTable.load(table("failures.toml")), Ledger(state / "ledger.sqlite"),
                        Cooldowns(state / "cooldowns.json"), state, meters=Meters(state / "meters.json"))


def test_each_account_gets_one_tiny_model_turn_and_an_identity(box, monkeypatch):
    priv, state, tmp = box
    monkeypatch.setenv("FAKE_CLAUDE_WINDOWS", "0.2,0.1")
    rows = run_check(priv, state)
    assert [(r["account"], r["ok"], r["identity_ok"], r["login"]) for r in rows] == [
        (n, 1, 1, "claude.ai") for n in ["one", "two", "three", "four"]]
    assert len({r["identity"] for r in rows}) == 4 and all(len(r["identity"]) == 12 for r in rows)
    calls = [json.loads(x) for x in (tmp / "argv.jsonl").read_text().splitlines()]
    turns = [c for c in calls if c["argv"][:1] == ["-p"]]
    assert len(turns) == 4 and all("--no-session-persistence" in c["argv"] and "haiku" in c["argv"] for c in turns)
    assert Meters(state / "meters.json").load()["three"]["five_hour"]["pct"] == 20.0
    assert Ledger(state / "ledger.sqlite").health_last("two")["ok"] == 1


def test_a_refused_login_fails_and_rests_the_account(box, monkeypatch):
    priv, state, tmp = box
    monkeypatch.setenv("FAKE_CLAUDE_AUTH_DIRS", str(tmp / "cfg-two"))
    rows = {r["account"]: r for r in run_check(priv, state)}
    assert (rows["two"]["ok"], rows["two"]["failure"], rows["two"]["login"]) == (0, "auth", "none")
    assert rows["two"]["identity_ok"] is None and rows["one"]["ok"] == 1
    assert Cooldowns(state / "cooldowns.json").until("two") is not None


def test_two_accounts_on_one_login_and_a_changed_login_fail_the_identity_check(box):
    priv, state, tmp = box
    run_check(priv, state)
    same = [Account("one", tmp / "cfg-one"), Account("two", tmp / "x" / "cfg-one")]   # the same login
    rows = run_check(priv, state, same)
    assert rows[1]["identity_ok"] == 0 and "same login as one" in rows[1]["note"]
    assert "identity changed since first seen" in rows[1]["note"]


def test_a_token_account_reports_its_login_method(box):
    priv, state, tmp = box
    acct = load_accounts(priv / "accounts.toml")[0]
    store_token(acct, "fake-token-for-tests")
    row = run_check(priv, state, [acct])[0]
    calls = [json.loads(x) for x in (tmp / "argv.jsonl").read_text().splitlines()]
    has = [("CLAUDE_CODE_OAUTH_TOKEN" in c["env_keys"], c["argv"][0]) for c in calls]
    assert row["login"] == "oauth_token" and has == [(False, "auth"), (True, "auth"), (True, "-p")]  # folder, token, turn


def test_yerk_accounts_lists_tokens_rests_and_health(box, capsys, monkeypatch):
    priv, state, _ = box
    base = ["--private", str(priv), "--state", str(state)]
    assert cli.main(["accounts", "cool", "two", "20", *base]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("fake-token-for-tests\n"))
    assert cli.main(["accounts", "token", "one", "--issued", "2025-11-10", *base]) == 0
    assert oct((priv / "secrets" / "claude-one.token").stat().st_mode & 0o777) == "0o600"
    capsys.readouterr()
    rc = cli.main(["accounts", *base])
    out = capsys.readouterr().out
    assert "fake-token" not in out
    lines = dict(line.split(" ", 1) for line in out.strip().splitlines())
    assert "RENEW" in lines["one"] and "days left" in lines["one"] and rc == 1      # inside 30 days
    assert "rests (manual, x0) until" in lines["two"] and "config folder's own login" in lines["two"]
    assert cli.main(["accounts", "clear", "two", *base]) == 0
    assert cli.main(["accounts", "health", "--names", "three", *base]) == 0
    assert "three: pass in" in capsys.readouterr().out


def test_yerk_status_prints_a_meter_per_account(box, capsys):
    priv, state, _ = box
    Meters(state / "meters.json").seen("one", {"five_hour": {"utilization": 0.07, "resetsAt": 2000000000},
                                                "seven_day": {"utilization": 0.03, "resetsAt": 2000300000}})
    assert cli.main(["status", "--no-live", "--private", str(priv), "--state", str(state)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "yerkd: not running" and out[1].startswith("one: 5h 7% (") and "wk 3% (" in out[1]
    assert out[2] == "two: no meter (not read)"


def test_a_token_from_another_account_fails_the_identity_check(box, monkeypatch):
    priv, state, tmp = box
    monkeypatch.setenv("FAKE_CLAUDE_WINDOWS", "0.2,0.1")          # the token's call: resets 2000000000 / 2000300000
    acct = load_accounts(priv / "accounts.toml")[0]
    store_token(acct, "fake-token-for-tests")
    mine = {"five_hour": {"pct": 20.0, "resets": 2000000000}, "seven_day": {"pct": 10.0, "resets": 2000300000}}
    monkeypatch.setattr(health, "read_usage", lambda d, t=None: mine)
    assert run_check(priv, state, [acct])[0]["identity_ok"] == 1
    other = {"five_hour": {"pct": 20.0, "resets": 2000007200}, "seven_day": {"pct": 10.0, "resets": 2000300000}}
    monkeypatch.setattr(health, "read_usage", lambda d, t=None: other)
    row = run_check(priv, state, [acct])[0]
    assert row["identity_ok"] == 0 and "another account" in row["note"]
    monkeypatch.setattr(health, "read_usage", lambda d, t=None: {"error": "http 401"})
    assert run_check(priv, state, [acct])[0]["identity_ok"] is None
