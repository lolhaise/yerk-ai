"""Checks the /login slash command: a login you hand over is saved by the daemon as NAME.login in the private folder's
secrets, from a slash command that reaches no room's session, and the browser plug-in types it by name. This is need 127. The
wall the ask hit: a room's shell cannot write the secrets folder, nor read it, and a password a room types through a tool call
transcript; here the room is handed nothing but the login's name. Every name, site and value is a placeholder."""
import asyncio
import json
import logging
import stat

import pytest
from test_slash import FAKE, OWNER, PERSON, ROOM_CH, STRANGER, It, wire

from yerk_ai import logins, words
from yerk_ai.accounts import Account
from yerk_ai.channels.discord import commands
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd

PASSWORD = "pw-Fake-7-placeholder"


def slash(dc, _command, who=OWNER, channel=ROOM_CH, **options):
    """/login as Discord hands it over: its four parts as the command's options (name and user are its own here)."""
    it = It(_command, user=who, channel=channel)
    it.data["options"] = [{"name": k, "type": 3, "value": v} for k, v in options.items()]
    asyncio.run(dc.on_interaction(it))
    return it.said


@pytest.fixture
def y(tmp_path, monkeypatch):
    """test_slash's daemon, its private folder's secrets in the test's own folder, never the example layer's."""
    monkeypatch.setenv("YERK_CLAUDE", str(FAKE))
    d = Yerkd(EXAMPLE_PRIVATE, tmp_path, only_account=Account("one", tmp_path / "cfg1"))
    d.private = tmp_path / "private"
    return d


def secret(tmp_path, name="example"):
    return tmp_path / "private" / "secrets" / f"{name}.login"


def test_login_is_saved_by_the_owner_alone_as_a_0600_secret_and_no_answer_or_log_line_holds_the_password(y, tmp_path, caplog):
    dc, room, chan = wire(y)
    caplog.set_level(logging.DEBUG)
    said = slash(dc, "login", name="example", site="https://www.example.com/signin", user="example-user", password=PASSWORD)
    assert said == [("message", f"{words.DONE} " + words.LOGIN_SAVED.format(name="example", site="example.com", user="example-user"), True)]
    f = secret(tmp_path)
    kept = json.loads(f.read_text())
    assert (kept["site"], kept["user"], kept["password"]) == ("example.com", "example-user", PASSWORD)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert not room.busy and chan.replies == [] and chan.notified == []        # nothing reached the room or its channel
    assert PASSWORD not in json.dumps(said) and PASSWORD not in caplog.text and "Fake-7" not in caplog.text
    assert [p.name for p in f.parent.iterdir()] == ["example.login"]            # no temporary file left beside it
    again = slash(dc, "login", name="example", site="example.com", user="example-user", password="pw-Fake-8-placeholder")
    assert again[0][1] == f"{words.DONE} " + words.LOGIN_REPLACED.format(name="example", site="example.com", user="example-user")
    assert json.loads(f.read_text())["password"] == "pw-Fake-8-placeholder"    # the owner's own word replaces their own login


def test_t4_a_password_that_starts_or_ends_with_a_space_is_saved_exactly_as_given(y, tmp_path):
    """T4 (need 127's fix): the password is kept as the owner gave it, a space at either end too; the plug-in types it so."""
    dc, _, _ = wire(y)
    for i, pw in enumerate((f" {PASSWORD}", f"{PASSWORD} ", f"  {PASSWORD}  ")):
        said = slash(dc, "login", name="example", site="example.com", user="example-user", password=pw)
        line = (words.LOGIN_REPLACED if i else words.LOGIN_SAVED).format(name="example", site="example.com", user="example-user")
        assert said[0][1] == f"{words.DONE} {line}", said
        assert json.loads(secret(tmp_path).read_text())["password"] == pw, repr(pw)


def test_login_from_anyone_but_the_owner_or_outside_a_room_saves_nothing(y, tmp_path):
    dc, _, _ = wire(y)
    for kw in ({"who": STRANGER}, {"who": PERSON}, {"channel": 22}):
        said = slash(dc, "login", name="example", site="example.com", user="x", password=PASSWORD, **kw)
        assert said[0][1].startswith(words.NOT_DONE) and said[0][2] is True and PASSWORD not in said[0][1]
    assert not secret(tmp_path).exists()


def test_login_refuses_a_bad_name_a_site_that_is_no_host_an_empty_part_and_a_link_in_its_place(y, tmp_path):
    dc, _, _ = wire(y)
    bad = [({"name": "Bad Name"}, words.LOGIN_NAME), ({"name": "../x"}, words.LOGIN_NAME), ({"site": "localhost"}, words.LOGIN_SITE),
           ({"site": "not a site"}, words.LOGIN_SITE), ({"password": ""}, words.LOGIN_EMPTY), ({"user": " "}, words.LOGIN_EMPTY),
           ({"password": "a\nb"}, words.LOGIN_EMPTY), ({"password": "x" * 513}, words.LOGIN_EMPTY)]
    for over, line in bad:
        said = slash(dc, "login", **{"name": "example", "site": "example.com", "user": "someone", "password": PASSWORD, **over})
        assert said[0][1] == f"{words.NOT_DONE} {line}", over
    assert not secret(tmp_path).exists()
    secret(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    secret(tmp_path).symlink_to(tmp_path / "elsewhere")
    said = slash(dc, "login", name="example", site="example.com", user="someone", password=PASSWORD)
    assert said[0][1] == f"{words.NOT_DONE} " + words.LOGIN_LINK.format(name="example") and not (tmp_path / "elsewhere").exists()


def test_a_site_is_its_host_without_scheme_path_port_or_www():
    assert [logins.site(s) for s in ("example.com", "https://www.Example.com/a?b", "login.example.org:443/x", "example.net.",
                                      "localhost", "127.0.0.1", "", "http://")] == ["example.com", "example.com", "login.example.org",
                                                                                     "example.net", "", "", "", ""]


def test_the_command_takes_four_required_parts_and_no_room_reads_the_file(y, tmp_path):
    login = next(c for c in commands() if c["name"] == "login")
    assert [(o["name"], o["required"]) for o in login["options"]] == [("name", True), ("site", True), ("user", True), ("password", True)]
    assert all(len(o["description"]) <= 100 for o in login["options"]) and len(login["description"]) <= 100
    folders, _ = y.secrets_at()                                                  # the wall stands: every room's Read denies it
    assert (y.private / "secrets").resolve() in folders


TWO = ("pw-Fake-7-Mn4Vb8Rt", "pw-Fake-8-Hj3Kd9Pq")      # two saved logins' passwords (logins-notes-doctor), placeholders
ELSEWHERE = "pw-Fake-6-Zt5Gw1Lc"                        # the one a link's target holds


def y7_plant(private):
    """Two logins saved, then beside them in the secrets folder: a link named x.login to a login of the same shape, a file
    that is not JSON, one with no password, and a password of each kind that does not count (7 with a digit, 15 letters)."""
    for name, pw in zip(("example", "example-two"), TWO):
        assert logins.save(private, name, "example.com", "example-user", pw)["ok"]
    folder, shape = private / "secrets", {"site": "example.com", "user": "example-user", "saved": "2026-11-20"}
    (elsewhere := private.parent / "elsewhere.json").write_text(json.dumps({**shape, "password": ELSEWHERE}))
    (folder / "x.login").symlink_to(elsewhere)
    (folder / "broken.login").write_text("{not json")
    (folder / "nopass.login").write_text(json.dumps(shape))
    for name, pw in (("short", "Fake7pw"), ("plain", "Placeholderword")):
        (folder / f"{name}.login").write_text(json.dumps({**shape, "password": pw}))


def test_y7_the_reader_skips_a_link_a_file_not_json_one_with_no_password_and_one_that_does_not_count_and_two_give_two(tmp_path):
    """Y7: logins.passwords, the one reader the `seen` tool and the doctor look for a saved password with, reads a plain
    NAME.login of the shape save writes, never through a link, and keeps a password only when it can be looked for by
    value: 8 characters or more with a digit or a symbol, or 16 or more of any kind. Its end spaces are off (T4 keeps them
    in the file), as text holds what shows."""
    private = tmp_path / "private"
    assert logins.passwords(private) == ()                                      # no secrets folder: none
    y7_plant(private)
    assert logins.passwords(private) == TWO
    assert logins.save(private, "example-two", "example.com", "example-user", f"  {TWO[1]} ")["ok"]
    assert logins.passwords(private) == TWO                                     # the same value, its end spaces off
    for name, pw in (("eight", "Fake7pwd"), ("sixteen", "Placeholderwords")):  # the lines: each counts
        assert logins.save(private, name, "example.com", "example-user", pw)["ok"]
    assert logins.passwords(private) == (*TWO, "Placeholderwords", "Fake7pwd")
