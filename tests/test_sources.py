"""Checks `yerk import imessage:` and `yerk import gmail`, which copy your texts and your mail into memory files. They run against a
Messages and a Contacts database built here and a fake Gmail that answers only EXAMINE and PEEK: a file a person or group (a deleted chat
placed by ck_chat_id, the reasons rows are skipped, attachments, app messages, the attributedBody text), a file a month (a newsletter, a
draft, a security mail, sent mail longer, the HTML stripper), the secret rules, a second run adding nothing and a new message put in its
--since and --dry-run, and a person room that never reads an imported line. Every value here is a placeholder."""
import sqlite3
import stat
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import yerk_ai.sources as sources
from yerk_ai import cli, memindex, memory, mentions

TZ = ZoneInfo("America/Toronto")
NS = lambda *t: int((datetime(*t, tzinfo=TZ).timestamp() - sources.APPLE) * 1e9)   # noqa: E731  a Messages date
UUID = "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwbGFjZWhvbGRlciJ9.c2lnbmF0dXJlLXBsYWNlaG9sZGVy"
at = "@".join                                      # an address, put together here: the repository holds none
FRIEND, OWNER = at(["friend", "example.test"]), at(["owner", "example.test"])


def typedstream(text):
    raw = text.encode()
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString\x01\x94\x84\x01+" + bytes([len(raw)]) + raw + b"\x86\x84"


def chat_db(path, rows):
    c = sqlite3.connect(path)
    c.executescript("""
        create table handle (ROWID integer primary key, id text);
        create table chat (ROWID integer primary key, guid text, style int, chat_identifier text, display_name text, group_id text,
                           original_group_id text);
        create table chat_handle_join (chat_id int, handle_id int);
        create table chat_message_join (chat_id int, message_id int);
        create table message (ROWID integer primary key, guid text, text text, attributedBody blob, handle_id int, is_from_me int,
                              date int, item_type int, associated_message_type int, balloon_bundle_id text, ck_chat_id text,
                              message_summary_info blob, group_title text);
        create table attachment (ROWID integer primary key, transfer_name text, filename text, hide_attachment int);
        create table message_attachment_join (message_id int, attachment_id int);
        insert into chat values (1, 'c1', 45, '+15550001111', '', null, null), (2, 'c2', 43, 'chat222', 'Lunch Crew', 'G2', null),
                                (4, 'c4', 45, '32665', '', null, null);
        insert into chat_handle_join values (2, 1), (2, 3);
        insert into attachment values (1, 'teapot.jpg', '~/a/teapot.jpg', 0), (2, 'preview.png', '', 1);
        insert into message_attachment_join values (3, 1), (3, 2);
    """)
    c.executemany("insert into handle values (?, ?)", [(1, "+15550001111"), (2, "+15550002222"), (3, FRIEND), (4, "32665")])
    for r in rows:
        r = {"text": None, "ab": None, "h": 0, "me": 0, "item": 0, "assoc": 0, "app": None, "ck": None, "title": None, "chat": None} | r
        c.execute("insert into message values (?,?,?,?,?,?,?,?,?,?,?,?,?)", (r["id"], f"guid-{r['id']}", r["text"], r["ab"], r["h"], r["me"],
                  r["at"], r["item"], r["assoc"], r["app"], r["ck"], None, r["title"]))
        if r["chat"]:
            c.execute("insert into chat_message_join values (?, ?)", (r["chat"], r["id"]))
    c.commit()
    c.close()


ROWS = [dict(id=1, chat=1, h=1, text="morning, the kettle\nis on", at=NS(2026, 3, 1, 9, 30)),
        dict(id=2, chat=1, me=1, text="on my way", at=NS(2026, 3, 1, 9, 31)),
        dict(id=3, chat=1, h=1, text="look ￼", at=NS(2026, 3, 1, 9, 32)),
        dict(id=4, chat=1, h=1, ab=typedstream("only in the archive"), at=NS(2026, 3, 1, 9, 33)),
        dict(id=5, chat=1, h=1, text="Loved it", assoc=2000, at=NS(2026, 3, 1, 9, 34)),
        dict(id=6, chat=2, h=3, text="table for three?", at=NS(2026, 3, 2, 12, 0)),
        dict(id=7, chat=2, me=1, app="com.apple.messages.MSMessageExtensionBalloonPlugin:0:com.example.game", at=NS(2026, 3, 2, 12, 1)),
        dict(id=8, chat=2, item=1, at=NS(2026, 3, 2, 12, 2)),
        dict(id=9, ck="SMS;-;+15550002222", h=2, text="from a deleted chat", at=NS(2026, 3, 3, 8, 0)),
        dict(id=10, ck=UUID, item=2, title="Tea Club", at=NS(2026, 3, 3, 8, 1)),
        dict(id=11, ck=UUID, h=2, text="a deleted group's line", at=NS(2026, 3, 3, 8, 2)),
        dict(id=12, chat=4, h=4, text="Your sign-in code is 482913", at=NS(2026, 3, 4, 7, 0)),
        dict(id=13, chat=4, h=4, text="Sale on kettles today", at=NS(2026, 3, 4, 7, 1)),
        dict(id=14, chat=1, me=1, text=f"482913 is your verification code, and {JWT}", at=NS(2026, 3, 4, 7, 2)),
        dict(id=15, me=1, text="sent to nobody stored", at=NS(2026, 3, 4, 7, 3))]


def contacts_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        create table Z_PRIMARYKEY (Z_ENT int, Z_NAME text);
        create table ZABCDRECORD (Z_PK int, Z_ENT int, ZFIRSTNAME text, ZLASTNAME text, ZORGANIZATION text, ZNICKNAME text);
        create table ZABCDPHONENUMBER (ZOWNER int, ZFULLNUMBER text);
        create table ZABCDEMAILADDRESS (ZOWNER int, ZADDRESS text, ZADDRESSNORMALIZED text);
        insert into Z_PRIMARYKEY values (22, 'ABCDContact');
        insert into ZABCDRECORD values (1, 22, 'Kettle', 'Person', null, null), (2, 22, null, null, 'Example Co', null);
        insert into ZABCDPHONENUMBER values (1, '+1 555 0001111');
    """)
    c.execute("insert into ZABCDEMAILADDRESS values (2, ?, ?)", (FRIEND.title(), FRIEND))
    c.commit()
    c.close()


@pytest.fixture
def layer(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    (priv / "secrets").mkdir(parents=True)
    (priv / "secrets" / "gmail.txt").write_text("# a placeholder app password\n- App password (no spaces): fakepassword\n")
    (priv / "yerkd.toml").write_text('[memory]\nroot = "memory"\ntimezone = "America/Toronto"\nalso = ["imports/texts", "imports/mail"]\n\n'
                                     f'[import]\nme = "Owner"\n\n[gmail]\nuser = "{OWNER}"\npassword_file = "secrets/gmail.txt"\n')
    chat_db(tmp_path / "chat.db", ROWS)
    contacts_db(tmp_path / "contacts.db")
    return priv, state, tmp_path


def run(priv, state, *args):
    return cli.main(["import", *args, "--private", str(priv), "--state", str(state)])


def texts(priv):
    return {p.name: p.read_text().splitlines() for p in sorted((priv / "imports" / "texts").glob("*.md"))}


def test_each_person_and_group_gets_a_file_of_its_lines_and_a_second_run_adds_nothing(layer, capsys):
    priv, state, tmp = layer
    assert run(priv, state, f"imessage:{tmp / 'chat.db'}", "--contacts", str(tmp / "contacts.db")) == 0
    got = texts(priv)
    assert sorted(got) == ["15550002222.md", "32665.md", "group-lunch-crew.md", "group-tea-club.md", "kettle-person.md"]
    assert got["kettle-person.md"] == [
        "# Texts with Kettle Person (+15550001111)", "5 messages, 2026-03-01 to 2026-03-04", "",
        "- 2026-03-01 09:30 Kettle Person: morning, the kettle is on",
        "- 2026-03-01 09:31 Owner: on my way",
        "- 2026-03-01 09:32 Kettle Person: look [attachment: teapot.jpg]",
        "- 2026-03-01 09:33 Kettle Person: only in the archive",
        "- 2026-03-04 07:02 Owner: [one-time-code withheld] is your verification code, and [jwt withheld]"]
    assert got["group-lunch-crew.md"][0] == f"# Texts in group Lunch Crew (members: Example Co {FRIEND}, Kettle Person +15550001111)"
    assert got["group-lunch-crew.md"][3:] == ["- 2026-03-02 12:00 Example Co: table for three?", "- 2026-03-02 12:01 Owner: [app: com.example.game]"]
    assert got["15550002222.md"][3:] == ["- 2026-03-03 08:00 +15550002222: from a deleted chat"]
    assert got["group-tea-club.md"][:1] + got["group-tea-club.md"][3:] == [
        "# Texts in group Tea Club (members: +15550002222)", "- 2026-03-03 08:02 +15550002222: a deleted group's line"]
    assert got["32665.md"][3:] == ["- 2026-03-04 07:00 32665: [security text: body withheld]", "- 2026-03-04 07:01 32665: Sale on kettles today"]
    out = capsys.readouterr().out
    assert ("imessage: 15 rows, 4 skipped by rule (tapback / emoji reaction 1, system event: member added/removed 1, system event: group "
            "renamed 1, no chat, no ck_chat_id and no handle: conversation unknown 1); 5 files, 11 added, 0 there already") in out
    imports = priv / "imports"
    assert {oct(stat.S_IMODE(p.stat().st_mode)) for p in [imports, imports / "texts"]} == {"0o700"}
    assert {oct(stat.S_IMODE(p.stat().st_mode)) for p in imports.rglob("*") if p.is_file()} == {"0o600"}
    before = {p: p.stat().st_mtime_ns for p in imports.rglob("*")}
    assert run(priv, state, f"imessage:{tmp / 'chat.db'}", "--contacts", str(tmp / "contacts.db")) == 0
    assert "5 files, 0 added, 11 there already" in capsys.readouterr().out
    assert {p: p.stat().st_mtime_ns for p in imports.rglob("*")} == before


def test_a_new_message_goes_in_after_its_minute_and_since_and_dry_run_hold(layer, capsys):
    priv, state, tmp = layer
    db = f"imessage:{tmp / 'chat.db'}"
    assert run(priv, state, db, "--since", "2026-03-02") == 0                     # no contacts: each keeps its number
    assert sorted(texts(priv)) == ["15550001111.md", "15550002222.md", "32665.md", "group-lunch-crew.md", "group-tea-club.md"]
    assert texts(priv)["group-lunch-crew.md"][0] == f"# Texts in group Lunch Crew (members: +15550001111, {FRIEND})"
    assert texts(priv)["15550001111.md"][1:] == ["1 messages, 2026-03-04 to 2026-03-04", "",
                                                 "- 2026-03-04 07:02 Owner: [one-time-code withheld] is your verification code, and [jwt withheld]"]
    c = sqlite3.connect(tmp / "chat.db")
    c.execute("insert into message (ROWID, guid, text, handle_id, is_from_me, date) values (16, 'guid-16', 'the late one', 1, 0, ?)",
              (NS(2026, 3, 4, 7, 2),))
    c.execute("insert into chat_message_join values (1, 16)")
    c.commit()
    c.close()
    capsys.readouterr()
    assert run(priv, state, db, "--dry-run") == 0
    assert "15550001111.md: 5 to add, 1 there already" in capsys.readouterr().out and len(texts(priv)["15550001111.md"]) == 4
    assert run(priv, state, db) == 0
    lines = texts(priv)["15550001111.md"]
    assert lines[1] == "6 messages, 2026-03-01 to 2026-03-04" and lines[-2:] == [
        "- 2026-03-04 07:02 Owner: [one-time-code withheld] is your verification code, and [jwt withheld]",
        "- 2026-03-04 07:02 +15550001111: the late one"]
    assert lines[3] == "- 2026-03-01 09:30 +15550001111: morning, the kettle is on"


def test_a_tree_no_import_wrote_and_a_missing_database_stop_before_anything_is_read(layer, capsys):
    priv, state, tmp = layer
    (priv / "imports" / "texts").mkdir(parents=True)
    (priv / "imports" / "texts" / "mom.md").write_text("# Texts with Mom\n")
    assert run(priv, state, f"imessage:{tmp / 'chat.db'}") == 2
    assert "imports/texts: files no import wrote (no texts.ids beside it)" in capsys.readouterr().err
    assert run(priv, state, f"imessage:{tmp / 'nothing.db'}") == 2 and "nothing.db is not a file" in capsys.readouterr().err
    assert sorted(p.name for p in (priv / "imports" / "texts").iterdir()) == ["mom.md"]
    (priv / "imports" / "texts.ids").write_text("")                          # a record, and a file of its edited out of shape
    (priv / "imports" / "texts" / "15550001111.md").write_text("edited by hand\n")
    assert run(priv, state, f"imessage:{tmp / 'chat.db'}") == 0
    assert "15550001111.md: not as an import writes it (no blank line under its title), left as it is" in capsys.readouterr().out
    assert (priv / "imports" / "texts" / "15550001111.md").read_text() == "edited by hand\n" and len(texts(priv)) == 6


def test_a_database_that_does_not_read_is_said_and_nothing_is_written(layer, capsys):
    priv, state, tmp = layer
    (tmp / "junk.db").write_text("not a database\n" * 100)
    chat_db(tmp / "old.db", ROWS[:1])
    c = sqlite3.connect(tmp / "old.db")
    c.execute("alter table message drop column ck_chat_id")                  # an old Messages schema
    c.commit()
    c.close()
    assert run(priv, state, f"imessage:{tmp / 'junk.db'}") == 2 and "junk.db would not read: file is not a database" in capsys.readouterr().err
    assert run(priv, state, f"imessage:{tmp / 'old.db'}") == 2 and "no such column: ck_chat_id" in capsys.readouterr().err
    assert not (priv / "imports").exists()


def test_a_second_chat_of_one_name_keeps_its_file_when_the_first_one_s_old_texts_go(layer):
    priv, state, tmp = layer
    c = sqlite3.connect(tmp / "contacts.db")
    c.execute("insert into ZABCDRECORD values (3, 22, 'Kettle', 'Person', null, null)")       # a second card of the same name
    c.execute("insert into ZABCDPHONENUMBER values (3, '+1 555 0002222')")
    c.commit()
    c.close()
    db, ab = f"imessage:{tmp / 'chat.db'}", ["--contacts", str(tmp / "contacts.db")]
    assert run(priv, state, db, *ab) == 0
    first = texts(priv)
    assert first["kettle-person-2.md"][0] == "# Texts with Kettle Person (+15550002222)"
    c = sqlite3.connect(tmp / "chat.db")
    c.execute("delete from message where ROWID in (1, 2, 3, 4, 14)")                       # the first card's texts, gone from the phone
    c.execute("insert into message (ROWID, guid, text, handle_id, is_from_me, date, ck_chat_id) values "
              "(16, 'guid-16', 'the second card again', 2, 0, ?, 'SMS;-;+15550002222')", (NS(2026, 3, 5, 9, 0),))
    c.commit()
    c.close()
    assert run(priv, state, db, *ab) == 0
    assert texts(priv)["kettle-person.md"] == first["kettle-person.md"]
    assert texts(priv)["kettle-person-2.md"][0] == first["kettle-person-2.md"][0]
    assert texts(priv)["kettle-person-2.md"][-1] == "- 2026-03-05 09:00 Kettle Person: the second card again"


# Gmail
def mime(frm, to, subject, date, body, lu=False, ct="text/plain; charset=utf-8"):
    head = f"Date: {date}\r\nFrom: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nContent-Type: {ct}\r\n"
    return (head + ("List-Unsubscribe: <https://example.test/u>\r\n" if lu else "") + "\r\n").encode(), body.encode()


LONG = " ".join(f"word{i}" for i in range(400))
MAIL = {1: (mime(f"A Person <{at(['a', 'example.test'])}>", OWNER, "Lunch?", "Mon, 05 Oct 2026 14:00:00 +0000", "Lunch on Friday? " + LONG), ""),
        2: (mime(f"News <{at(['n', 'example.test'])}>", OWNER, "This week", "Mon, 05 Oct 2026 15:00:00 +0000", "never fetched", lu=True), ""),
        3: (mime(f"Shop <{at(['s', 'example.test'])}>", OWNER, "Your code 482913", "Tue, 06 Oct 2026 09:00:00 +0000", "Enter it to sign in"), ""),
        4: (mime(f"Owner <{OWNER}>", f"B Person <{at(['b', 'example.test'])}>", "Notes", "Tue, 06 Oct 2026 10:00:00 +0000", LONG), "\\Sent"),
        5: (mime(f"Owner <{OWNER}>", f"{at(['b', 'example.test'])}", "Unsent", "Tue, 06 Oct 2026 11:00:00 +0000", "half a thought"), "\\Draft"),
        6: (mime(f"Bank <{at(['bank', 'example.test'])}>", OWNER, "Statement", "Wed, 07 Oct 2026 12:00:00 +0000",
                 "<html><head><style>p{}</style><body><p>Your statement is ready.</p>" + "<i></i>" * 900, ct="text/html"), ""),
        7: (mime(f"Dev <{at(['d', 'example.test'])}>", OWNER, "the key", "Wed, 07 Oct 2026 13:00:00 +0000",
                 "use sk-proj-" + "a1B2" * 10 + " for now"), ""),
        8: (mime(f"A Person <{at(['a', 'example.test'])}>", OWNER, "Old", "Tue, 29 Sep 2026 14:00:00 +0000", "before since"), "")}


class FakeGmail:
    """IMAP4_SSL as Gmail answers it: only what a read-only import may ask (EXAMINE, SEARCH, FETCH with PEEK, LOGOUT)."""
    calls: list = []
    validity = b"7"

    def __init__(self, host, port, timeout=None):
        assert (host, port) == ("imap.gmail.com", 993)

    def login(self, user, pw):
        assert (user, pw) == (OWNER, "fakepassword")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, box, readonly=False):
        assert readonly and box == '"[Gmail]/All Mail"'
        return "OK", [b"8"]

    def response(self, code):
        return code, [self.validity]

    def uid(self, command, *args):
        FakeGmail.calls.append((command, args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(u).encode() for u in MAIL)]
        assert command == "FETCH" and "PEEK" in args[1]
        data = []
        for u in map(int, args[0].split(",")):
            (head, body), labels = MAIL[u]
            part = head if "HEADER" in args[1] else body[:int(args[1].rsplit(".", 1)[1].rstrip(">)"))]
            data += [(f'{u} (UID {u} INTERNALDATE "05-Oct-2026 14:00:00 +0000" X-GM-LABELS ({labels}) BODY[] {{{len(part)}}}'.encode(), part), b")"]
        return "OK", data

    def logout(self):
        pass


def test_mail_is_a_file_a_month_read_only_and_a_second_run_fetches_nothing_it_has(layer, monkeypatch, capsys):
    priv, state, _ = layer
    FakeGmail.calls = []
    monkeypatch.setattr(sources.imaplib, "IMAP4_SSL", FakeGmail)
    assert run(priv, state, "gmail", "--since", "2026-10-01") == 0
    assert sorted(p.name for p in (priv / "imports" / "mail").iterdir()) == ["2026-10.md"]
    got = (priv / "imports" / "mail" / "2026-10.md").read_text().split("\n")
    assert got[:3] == ["# Mail 2026-10", "7 messages", ""]
    assert got[3:5] == [f"- 2026-10-05 10:00 From: A Person <{at(['a', 'example.test'])}> | To: {OWNER} | Subject: Lunch?",
                        "  " + ("Lunch on Friday? " + LONG)[:400]]
    assert got[5:7] == [f"- 2026-10-05 11:00 From: News <{at(['n', 'example.test'])}> | To: {OWNER} | Subject: This week", "  [newsletter]"]
    assert got[7:9] == [f"- 2026-10-06 05:00 From: Shop <{at(['s', 'example.test'])}> | To: {OWNER} | Subject: Your code [code withheld]",
                        "  [security mail: body withheld]"]
    assert got[10] == "  " + LONG[:1200] and got[11].endswith("| Subject: Unsent | [draft]")
    assert got[12] == "  half a thought" and got[14:] == [
        "  Your statement is ready.", f"- 2026-10-07 09:00 From: Dev <{at(['d', 'example.test'])}> | To: {OWNER} | Subject: the key",
        "  [security mail: body withheld]", ""]
    assert ("SEARCH", ("SINCE", "29-Sep-2026")) in FakeGmail.calls
    assert not any("2" in a[0].split(",") for c, a in FakeGmail.calls if c == "FETCH" and "TEXT" in a[1])     # a newsletter's body
    assert (priv / "imports" / "mail.ids").read_text().split() == [f"7:{u}" for u in (1, 2, 3, 4, 5, 6, 7)]
    assert "gmail: 8 mails found, 8 not on record read; 1 files, 7 added, 0 there already" in capsys.readouterr().out
    FakeGmail.calls = []
    assert run(priv, state, "gmail", "--since", "2026-10-01") == 0
    assert [a[0] for c, a in FakeGmail.calls if c == "FETCH"] == ["8"]          # only the one dated before --since
    assert "8 mails found, 1 not on record read; 0 files, 0 added, 7 there already" in capsys.readouterr().out
    FakeGmail.validity = b"9"
    with pytest.raises(SystemExit, match="UIDVALIDITY is now 9"):
        run(priv, state, "gmail")
    FakeGmail.validity = b"7"


def test_the_app_password_is_its_line_or_the_file_alone_and_a_file_without_one_sends_nothing(layer, monkeypatch, capsys):
    priv, state, _ = layer
    monkeypatch.setattr(sources.imaplib, "IMAP4_SSL", FakeGmail)
    pw = priv / "secrets" / "gmail.txt"
    for good in ("App password: fake pass word\n", "fake pass word\n"):     # four groups, as Google shows one, are one password
        pw.write_text(good)
        assert run(priv, state, "gmail", "--dry-run") == 0
    pw.write_text("# notes\nfakepassword\n")
    with pytest.raises(SystemExit, match="no app password in"):
        run(priv, state, "gmail", "--dry-run")
    pw.unlink()
    assert run(priv, state, "gmail") == 2 and "gmail.txt is not a file" in capsys.readouterr().err


def test_a_batch_that_fails_is_said_the_rest_written_and_one_that_stays_down_ends_1(layer, monkeypatch, capsys):
    priv, state, _ = layer
    monkeypatch.setattr(sources, "WINDOWS", ((4000, 2), (32768, 2)))     # bodies two a batch: 8 7, 6 5, 4 3, 1

    class Flaky(FakeGmail):
        up, made, fail = 2, 0, True

        def __init__(self, host, port, timeout=None):
            Flaky.made += 1
            if Flaky.made > Flaky.up:
                raise OSError("Network is unreachable")

        def uid(self, command, *args):
            if Flaky.fail and command == "FETCH" and "TEXT" in args[1] and "3" in args[0].split(","):
                Flaky.fail = False
                raise sources.imaplib.IMAP4.abort("unexpected response: b'Your code is 482913'")
            return super().uid(command, *args)

    monkeypatch.setattr(sources.imaplib, "IMAP4_SSL", Flaky)
    ids = priv / "imports" / "mail.ids"
    assert run(priv, state, "gmail") == 1                                 # it comes back: 4 and 3 wait for the next run
    out = capsys.readouterr().out
    assert "2 mails not read, left for the next run: abort: unexpected response:" in out and "482913" not in out
    assert sorted(ids.read_text().split()) == [f"7:{u}" for u in (1, 2, 5, 6, 7, 8)]
    Flaky.up, Flaky.made = 9, 0
    assert run(priv, state, "gmail") == 0 and sorted(ids.read_text().split()) == [f"7:{u}" for u in range(1, 9)]
    ids.unlink()
    for p in (priv / "imports" / "mail").iterdir():
        p.unlink()
    Flaky.up, Flaky.made, Flaky.fail = 1, 0, True                         # it stays down: 1 waits too, the rest is written
    assert run(priv, state, "gmail") == 1 and "Gmail did not answer again (OSError)" in capsys.readouterr().out
    assert sorted(ids.read_text().split()) == [f"7:{u}" for u in (2, 5, 6, 7, 8)]


def test_the_secret_rules_write_a_kind_and_never_the_secret():
    assert sources.scrub("key sk-ant-" + "x" * 30 + " and AKIA" + "A" * 16) == "key [anthropic-key withheld] and [aws-key-id withheld]"
    assert sources.scrub("G-123456 is your Example code") == "G-[one-time-code withheld] is your Example code"
    assert sources.scrub("call me at 4165550123 or 2026-03-01 09:30") == "call me at 4165550123 or 2026-03-01 09:30"
    at = datetime(2026, 3, 1, 9, 30, tzinfo=TZ)
    assert sources.text_line(at, "77777", "your PIN is 1234", True) == "- 2026-03-01 09:30 77777: [security text: body withheld]"
    assert sources.text_line(at, "Mom", "the wifi password is on the fridge", False) == "- 2026-03-01 09:30 Mom: the wifi password is on the fridge"
    assert sources.scrub("Your Example code is 482913. Never share it") == "Your Example [code withheld]. Never share it"
    assert sources.scrub("the wifi pwd: kettle-4821 or the old one") == "the wifi [password withheld] or the old one"
    assert sources.scrub("join https://example.test/j/1?pwd=" + "Ab1" * 8 + " at noon") == "join https://example.test/j/1[link-token withheld] at noon"
    h = {"ct": "text/plain", "cte": "", "sent": False, "subject": "hello"}
    assert sources.body_line(h, b"a gateway token for you") == sources.HELD_MAIL
    assert sources.body_line(h, b"pin the note to the fridge") == "pin the note to the fridge"
    assert sources.body_line(h, b"") is None


def test_a_person_room_never_reads_an_imported_text_or_mail_line(layer, monkeypatch):
    priv, state, tmp = layer
    monkeypatch.setattr(sources.imaplib, "IMAP4_SSL", FakeGmail)
    assert run(priv, state, f"imessage:{tmp / 'chat.db'}", "--contacts", str(tmp / "contacts.db")) == 0
    assert run(priv, state, "gmail") == 0
    s = memory.settings(priv, state)
    assert [r.name for r in s.scope.roots[1:]] == ["texts", "mail", "docs"]          # docs: the Claude docs (docs.py)
    memindex.Index(s.index_db, s.scope).sync()
    her, owner = memory.Reader("cousin-room", "discord:20", False), memory.Reader("home", "discord:10", True)
    for words in ("kettle morning", "Lunch Friday", "statement ready"):
        assert memory.recall(s, words, 20, reader=owner)["hits"]
        assert memory.recall(s, words, 20, reader=her)["hits"] == []
    for cite in ("texts/kettle-person.md", "mail/2026-10.md"):
        assert memory.read(s, cite, reader=owner)["ok"] and not memory.read(s, cite, reader=her)["ok"]
    files = [p for p in (priv / "imports").rglob("*.md")]
    assert files and all(memory.file_class(p) == ("private", "") for p in files)
    names = mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude)
    assert not any(names.is_source(p) for p in files)                        # no name in them reaches a room's mention recall
