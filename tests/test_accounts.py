import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

from yerk_ai.accounts import (TOKEN_ENV, Account, Cooldowns, account_env, load_accounts, pick, read_token,
                              store_token, token_info)
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.lifted.expiry import DAY_MS, login_state

A = [Account("one", Path("/x/one")), Account("two", Path("/x/two")), Account("three", Path("/x/three"))]
FOUR = """[accounts]
one = { config_dir = "/x/one", order = 1 }
four = { config_dir = "/x/four", order = 2 }
two = { config_dir = "/x/two", order = 3 }
three = { config_dir = "/x/three", order = 4 }
"""


def test_env_pins_the_config_dir_and_drops_inherited_logins():
    env = account_env(A[1], {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/x/one", "CLAUDECODE": "1", "ANTHROPIC_X": "y",
                             TOKEN_ENV: "inherited"})
    assert env == {"PATH": "/bin", "CLAUDE_CONFIG_DIR": "/x/two", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                   "CLAUDE_CODE_ENTRYPOINT": "yerk", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}


def test_an_inherited_entrypoint_or_artifact_setting_is_replaced_not_kept(monkeypatch):
    inherited = {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "CLAUDE_CODE_DISABLE_ARTIFACT": "0"}
    for k, v in inherited.items():
        monkeypatch.setenv(k, v)                    # the daemon's own environment, which a room's process starts from
    for env in (account_env(A[1], {"PATH": "/bin", **inherited}), account_env(A[1])):
        assert (env["CLAUDE_CODE_ENTRYPOINT"], env["CLAUDE_CODE_DISABLE_ARTIFACT"]) == ("yerk", "1")


def test_a_stored_token_goes_to_the_child_and_its_file_is_private(tmp_path):
    acct = Account("one", tmp_path / "cfg", 1, tmp_path / "secrets" / "claude-one.token")
    path = store_token(acct, "fake-token-for-tests\n")
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert account_env(acct, {"PATH": "/bin"})[TOKEN_ENV] == "fake-token-for-tests"
    assert read_token(acct)[0] == "fake-token-for-tests"


def test_a_bare_pasted_token_reads_with_the_file_time(tmp_path):
    f = tmp_path / "claude-two.token"
    f.write_text("fake-token-pasted\n")
    tok, issued = read_token(Account("two", tmp_path, 2, f))
    assert tok == "fake-token-pasted" and abs(issued - f.stat().st_mtime) < 1


def test_token_age_days_left_and_the_30_day_warning(tmp_path):
    acct = Account("one", tmp_path, 1, tmp_path / "t.token")
    assert token_info(acct) == {"token": "none"}
    store_token(acct, "fake", issued=date.today() - timedelta(days=10))
    t = token_info(acct, noon := time.mktime(date.today().timetuple()) + 43200)   # noon: one clock-change hour moves no x.5 day
    assert (t["token"], t["age_days"], t["days_left"]) == ("ok", 10, 354)
    store_token(acct, "fake", issued=date.today() - timedelta(days=340))
    assert token_info(acct, noon)["token"] == "due" and token_info(acct, noon)["days_left"] == 24
    store_token(acct, "fake", issued=date.today() - timedelta(days=366))
    assert token_info(acct)["token"] == "dead"


def test_one_line_adds_a_fifth_account_and_one_line_removes_it(tmp_path):
    path = tmp_path / "accounts.toml"
    path.write_text(FOUR)
    four = load_accounts(path)
    assert [a.name for a in four] == ["one", "four", "two", "three"]
    assert four[0].token_file == tmp_path / "secrets" / "claude-one.token"
    line = 'five = { config_dir = "/x/five", order = 0 }\n'
    path.write_text(FOUR + line)                       # the one added line, nothing else
    five = load_accounts(path)
    assert [a.name for a in five] == ["five", "one", "four", "two", "three"]
    c = Cooldowns(tmp_path / "cool.json")
    assert pick(five, c) == five[0]                    # the new account is in the one code path at once
    c.fail("five", "auth", lambda r: 600)
    assert pick(five, c).name == "one"
    path.write_text(path.read_text().replace(line, ""))  # the one removed line
    assert [a.name for a in load_accounts(path)] == ["one", "four", "two", "three"]


def test_pick_skips_resting_accounts_and_always_tries_the_last(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert pick(A, c, now=0) == A[0]
    c.fail("one", "rate_limit", lambda r: 30, now=0)
    assert pick(A, c, now=1) == A[1]
    c.fail("two", "auth", lambda r: 30, now=0)
    c.fail("three", "billing", lambda r: 30, now=0)
    assert pick(A, c, now=1) == A[2]
    assert pick(A, c, now=31) == A[0]


def test_the_account_a_room_is_put_on_goes_first_and_the_order_stays_behind_it(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert [pick(A, c, now=0, first=n).name for n in ("three", "two", "", "gone")] == ["three", "two", "one", "one"]
    c.fail("three", "rate_limit", lambda r: 30, now=0)                  # the pinned one rests: the order's first starts
    assert pick(A, c, now=1, first="three") == A[0]
    c.fail("one", "auth", lambda r: 30, now=0)                          # and past it, the next in the order
    assert pick(A, c, now=1, first="three") == A[1]
    c.fail("two", "billing", lambda r: 30, now=0)
    assert pick(A, c, now=1, first="three") == A[1]                     # every one rests: the last of that order
    assert pick(A, c, now=31, first="three") == A[2] and [a.name for a in A] == ["one", "two", "three"]   # back on it


def test_cooldowns_double_per_account_and_kind_persist_and_clear(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert c.fail("one", "rate_limit", lambda r: 30 * 2 ** r, now=0) == 30
    assert c.fail("one", "rate_limit", lambda r: 30 * 2 ** r, now=0) == 60
    assert c.fail("one", "billing", lambda r: 600 * 2 ** r, now=0) == 600      # its own count
    again = Cooldowns(tmp_path / "cool.json")                                     # a restart
    assert again.until("one", now=1) == 600
    assert [(r["kind"], r["repeats"]) for r in again.show("one", now=1)] == [("billing", 1), ("rate_limit", 2)]
    c.clear("one")
    assert Cooldowns(tmp_path / "cool.json").until("one", now=1) is None


def test_a_rest_set_by_hand_is_seen_without_a_restart(tmp_path):
    daemon, by_hand = Cooldowns(tmp_path / "cool.json"), Cooldowns(tmp_path / "cool.json")
    assert pick(A, daemon) == A[0]
    time.sleep(0.01)
    by_hand.set("one", "manual", time.time() + 600)
    assert pick(A, daemon) == A[1]


def test_a_layer_1_cooldown_file_still_reads(tmp_path):
    (tmp_path / "cool.json").write_text(json.dumps({"one": {"until": time.time() + 60, "repeats": 1}}))
    assert pick(A, Cooldowns(tmp_path / "cool.json")) == A[1]


def test_a_rest_keyed_to_a_model_blocks_that_model_alone_and_ends_at_the_reset_it_was_given(tmp_path):
    c = Cooldowns(tmp_path / "cool.json")
    assert c.fail("one", "rate_limit", lambda r: 30, now=0, model="fable", until=5000) == 5000   # the reset the stream named
    assert c.fail("one", "rate_limit", lambda r: 30, now=0, until=0) == 30                        # a reset gone by: the backoff
    assert (c.until("one", now=1, model="fable"), c.until("one", now=1, model="opus"), c.until("one", now=1)) == (5000, 30, 5000)
    assert (pick(A, c, now=1, model="opus"), pick(A, c, now=31, model="opus"), pick(A, c, now=31, model="fable")) == (A[1], A[0], A[1])
    assert [(r["kind"], r["repeats"]) for r in c.show("one", now=1)] == [("rate_limit:fable", 1), ("rate_limit", 1)]
    c.clear("one", model="opus")                               # a reply on opus: its own and the model-less rests go, fable's stays
    assert (c.until("one", now=1, model="opus"), c.until("one", now=1, model="fable")) == (None, 5000)
    c.clear("one", model="fable")
    assert Cooldowns(tmp_path / "cool.json").until("one", now=1) is None and pick(A, c, now=1, model="fable") == A[0]


def test_a_rest_lifted_before_its_end_keeps_that_end_and_a_failure_before_it_stays_to_its_own(tmp_path):
    """usage.back lifts a rate limit's rest its usage shows over: the rest ends then, its end kept as `was`, its repeats
    kept, every other rest standing. The same rest failing again before that end proves the reading wrong: the new rest
    stays to its end, lift passes it by, and a reply on the account (clear) trusts the readings again."""
    c = Cooldowns(tmp_path / "cool.json")
    c.fail("one", "rate_limit", lambda r: 30, now=0, model="fable", until=5000)
    c.fail("one", "billing", lambda r: 600, now=0)
    rate = lambda kind, rest: kind.startswith("rate_limit")                # noqa: E731
    assert c.lift("one", rate, now=100) == ["rate_limit:fable"] and pick(A, c, now=101, model="fable") == A[1]   # billing stands
    assert [r["kind"] for r in c.show("one", now=101)] == ["billing"] and c.lift("one", rate, now=101) == []
    assert Cooldowns(tmp_path / "cool.json").data["one"]["rate_limit:fable"] == {"until": 100, "repeats": 1, "at": 0, "was": 5000}
    assert c.fail("one", "rate_limit", lambda r: 30, now=200, model="fable", until=5000) == 5000    # before 5000: wrong
    assert c.lift("one", rate, now=300) == [] and c.until("one", now=700, model="fable") == 5000
    assert c.show("one", now=300)[0] == {"kind": "rate_limit:fable", "until": 5000, "repeats": 2, "stays": True}
    c.clear("one", model="fable")
    c.fail("one", "rate_limit", lambda r: 30, now=400, model="fable", until=5000)
    assert c.lift("one", rate, now=500) == ["rate_limit:fable"] and c.until("one", now=501) is None


def test_login_state_window():
    now = 10 * DAY_MS
    assert login_state(None, True, now)[0] == "dead"
    assert login_state(now + DAY_MS, False, now)[0] == "dead"
    assert login_state(now + 5 * DAY_MS, True, now) == ("due", 5.0)
    assert login_state(now + 40 * DAY_MS, True, now)[0] == "ok"


def test_example_accounts_parse():
    names = [a.name for a in load_accounts(EXAMPLE_PRIVATE / "accounts.toml")]
    assert names == ["one", "two"]
    assert not os.environ.get("YERK_TEST_NEVER_SET")
