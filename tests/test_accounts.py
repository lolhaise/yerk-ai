from pathlib import Path

from yerk_ai.accounts import Account, Cooldowns, account_env, load_accounts, pick
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.lifted.cooldowns import merge_cooldowns
from yerk_ai.lifted.expiry import DAY_MS, login_state

A = [Account("one", Path("/x/one")), Account("two", Path("/x/two")), Account("three", Path("/x/three"))]


def test_env_pins_the_config_dir_and_drops_inherited_logins():
    env = account_env(A[1], {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/x/one", "CLAUDECODE": "1", "ANTHROPIC_X": "y"})
    assert env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/x/two", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def test_pick_skips_resting_accounts_and_always_tries_the_last(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert pick(A, c, now=0) == A[0]
    c.fail("one", lambda r: 30, now=0)
    assert pick(A, c, now=1) == A[1]
    c.fail("two", lambda r: 30, now=0)
    c.fail("three", lambda r: 30, now=0)
    assert pick(A, c, now=1) == A[2]
    assert pick(A, c, now=31) == A[0]


def test_cooldowns_double_persist_and_clear(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert c.fail("one", lambda r: 30 * 2 ** r, now=0) == 30
    assert c.fail("one", lambda r: 30 * 2 ** r, now=0) == 60
    assert Cooldowns(tmp_path / "cool.json").until("one", now=1) == 60
    c.clear("one")
    assert Cooldowns(tmp_path / "cool.json").until("one", now=1) is None


def test_merge_keeps_the_latest_reset():
    assert merge_cooldowns({"one": 5, "two": 9}, {"one": 7}, None) == {"one": 7.0, "two": 9.0}


def test_login_state_window():
    now = 10 * DAY_MS
    assert login_state(None, True, now)[0] == "dead"
    assert login_state(now + DAY_MS, False, now)[0] == "dead"
    assert login_state(now + 5 * DAY_MS, True, now) == ("due", 5.0)
    assert login_state(now + 40 * DAY_MS, True, now)[0] == "ok"


def test_example_accounts_parse():
    names = [a.name for a in load_accounts(EXAMPLE_PRIVATE / "accounts.toml")]
    assert names == ["one", "two"]
