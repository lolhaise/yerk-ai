"""Checks the `email` tool (emails.py), which sends an email in your name from your own Gmail, with the room's files attached, once you have seen
it and said yes. Before it (need 59 of 2026-11-05), a room asked to send a written statement and a document to a person whose only contact was an
email address had a mail tool that reads only, a text tool that reaches phone numbers and Apple addresses, and a browser not signed into the
owner's Gmail, so it handed the owner a block to paste into an email himself. An email in the owner's name goes from the owner's own Gmail with
the room's files attached, held first with who it goes to, its subject, its files and its words shown in the room, then sent once on the owner's
word, and never from a person's room, from a worker, past a rule turned off or a hook that failed open. An email that did not go is told apart
from one that may have. A fake Gmail here stands in for the real one: nothing leaves the machine, and every address and name is a placeholder."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import smtplib
from pathlib import Path

import pytest

from test_gates import Rec, call
from yerk_ai import emails, gates, mcp_server, sources, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import YERK_TOOLS, Yerkd
from yerk_ai.engine import SessionTable
from yerk_ai.room import Inbound

at = "@".join                                      # an address, put together here: the repository holds none
OWNER, TO = at(["owner", "example.test"]), at(["desk", "example.org"])
PASSWORD = "abcd efgh ijkl mnop"                   # Google's four groups, one password


class Gmail:
    """smtplib.SMTP_SSL as the tool uses it; what it is handed lands in `log`, and `mode` says how it answers."""
    log: list[tuple] = []
    mode = "ok"

    def __init__(self, host: str, port: int, timeout: float = 0, context: object = None) -> None:
        if Gmail.mode == "away":
            raise OSError("Network is unreachable")
        Gmail.log.append(("connect", host, port))

    def login(self, user: str, pw: str) -> None:
        Gmail.log.append(("login", user, pw))
        if Gmail.mode == "login":
            raise smtplib.SMTPAuthenticationError(535, f"5.7.8 not accepted: {pw}".encode())

    def send_message(self, msg) -> dict:
        if Gmail.mode == "refuse":
            raise smtplib.SMTPRecipientsRefused({TO: (550, b"no such user")})
        if Gmail.mode == "drop":
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        Gmail.log.append(("send", msg))
        return {TO: (550, b"no such user")} if Gmail.mode == "some" else {}

    def quit(self) -> None:
        Gmail.log.append(("quit",))


@pytest.fixture(autouse=True)
def gmail(monkeypatch):
    monkeypatch.setattr(emails, "SMTP", Gmail)
    Gmail.log, Gmail.mode = [], "ok"
    return Gmail


def setup(tmp_path: Path, people: bool = False, gate: dict | None = None) -> tuple[Yerkd, object, Rec, dict]:
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    if gate is not None:
        y.gates = gates.settings(gate)
    if people:
        y.room_confs["example-room"] = {**y.room_confs["example-room"], "privacy": "family", "people": [31]}
    (pw := tmp_path / "gmail.password").write_text(f"User: {OWNER}\nApp password (no spaces): {PASSWORD}\n")
    y.conf["gmail"] = {"user": OWNER, "password_file": str(pw), "name": "Example Owner"}
    (inbox := tmp_path / "rooms" / "example-room" / "work" / "inbox" / "m1").mkdir(parents=True)
    (slip := inbox / "slip.pdf").write_bytes(b"%PDF-1.4 example slip\n" * 100)
    ch = Rec()
    room = y.add_room("example-room", ch)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-g", "one")
    return y, room, ch, {"to": [TO], "subject": "Statement, case 0001", "text": "Hello,\n\nMy statement is below.\n\nExample Owner",
                         "files": [str(slip)]}


def pre(y: Yerkd, ti: dict) -> dict:
    return call(y, {"hook_event_name": "PreToolUse", "tool_name": emails.TOOL, "tool_input": ti})


def held(out: dict) -> bool:
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def email(y: Yerkd, ti: dict, room: str = "example-room") -> dict:
    return asyncio.run(y.tool_email(room, ti))


def word(y: Yerkd, room) -> dict:
    room.busy, room.inflight = True, [Inbound(f"m{len(y.worded)}", "owner", "approve")]   # the owner's word, in the turn
    return call(y, {"hook_event_name": "UserPromptSubmit", "prompt": "approve"})


def sent() -> list:
    return [x[1] for x in Gmail.log if x[0] == "send"]


def test_every_room_is_handed_the_email_tool_where_the_room_of_the_need_had_none(tmp_path):
    """The wall: the room's mail tool read only and its text tool reached phone numbers, so an email address had no way."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", Rec())
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["email"].inputSchema["properties"]) == ["to", "subject", "text", "files"] and emails.TOOL in YERK_TOOLS
    assert "the owner's own Gmail" in str(tools["email"].description) and "never a name" in str(tools["email"].description)
    assert emails.TOOL in y.spec_for("example-room").settings["permissions"]["allow"]
    assert gates.external_hit(emails.TOOL, {"to": [TO]}, "example-room", y.gates)[:2] == ("email-out", "an email in your name")


def test_an_email_is_held_with_who_it_goes_to_its_subject_its_file_and_its_words_and_the_word_sends_it_once(tmp_path):
    y, room, ch, mail = setup(tmp_path)
    out = pre(y, mail)
    assert held(out) and out["hookSpecificOutput"]["permissionDecisionReason"].startswith("Held: an email in your name")
    assert ch.notes == [f"{words.WORKING} held: an email in your name\nsay approve here to let it run once\n```\nfrom: Example Owner <{OWNER}>\n"
                        f"to: {TO}\nsubject: Statement, case 0001\nattached: slip.pdf (2 KB)\n\nHello,\n\nMy statement is below.\n\n"
                        "Example Owner\n```"]                                     # as it reads and goes: no JSON, no path
    assert email(y, mail) == {"ok": False, "maybe_sent": False, "error": words.EMAIL_NOT_RELEASED} and Gmail.log == []
    assert "[approval] owner said approve" in word(y, room)["hookSpecificOutput"]["additionalContext"]
    assert pre(y, mail) == {}                                                     # the same call, released
    r = email(y, mail)
    assert r["ok"] and r["to"] == [TO] and r["files"] == ["slip.pdf"] and r["id"].endswith("@example.test>")
    assert r["from"] == f"Example Owner <{OWNER}>"                               # the From line it went under, name and all
    assert Gmail.log[:2] == [("connect", "smtp.gmail.com", 465), ("login", OWNER, "abcdefghijklmnop")]
    msg = sent()[0]
    assert (msg["From"], msg["To"], msg["Subject"]) == (f"Example Owner <{OWNER}>", TO, "Statement, case 0001")
    assert msg.get_body(("plain",)).get_content().startswith("Hello,\n\nMy statement is below.")
    [part] = list(msg.iter_attachments())
    assert (part.get_filename(), part.get_content_type(), part.get_content()) == ("slip.pdf", "application/pdf", Path(mail["files"][0]).read_bytes())
    assert email(y, mail)["error"] == words.EMAIL_NOT_RELEASED and len(sent()) == 1    # once: never again on that word
    assert held(pre(y, mail))                                                     # the next one is held again
    assert [(r["gate"], r["decision"]) for r in reversed(y.ledger.rows(10, "gates"))] == [
        ("approval", "hold"), ("approval", "approved"), ("approval", "release"), ("email", "sent"), ("approval", "hold")]
    assert f"to {TO}" in y.ledger.rows(10, "gates")[1]["detail"] and "1 file(s)" in y.ledger.rows(10, "gates")[1]["detail"]


def test_an_email_with_no_files_given_is_the_same_call_as_one_with_none_and_an_edit_is_a_new_preview(tmp_path):
    y, room, ch, mail = setup(tmp_path)
    bare = {k: v for k, v in mail.items() if k != "files"}
    edit = {**bare, "text": "Hello,\n\nMy statement, corrected."}
    assert held(pre(y, bare)) and "attached" not in ch.notes[0] and held(pre(y, edit))
    word(y, room)
    assert held(pre(y, bare)) and pre(y, {**edit, "files": []}) == {}             # the word was for the email shown last
    assert email(y, bare)["error"] == words.EMAIL_NOT_RELEASED and email(y, edit)["ok"]
    assert [m.get_body(("plain",)).get_content() for m in sent()] == ["Hello,\n\nMy statement, corrected.\n"]


def test_the_word_sends_the_held_email_once_whatever_folder_the_shell_is_in_when_it_is_called_again(tmp_path):
    """need 78 of 2026-11-09, the wall: held with the shell in the folder of the file it attaches, a /compact before the word,
    the session back in its work folder, and the same call was held again on a second card. Its files go by whole path and
    sha256, so the folder adds nothing to the call: the word sends it once, wherever the shell is."""
    y, room, ch, mail = setup(tmp_path)
    work = tmp_path / "rooms" / "example-room" / "work"

    def pre_in(cwd: Path) -> dict:
        return call(y, {"hook_event_name": "PreToolUse", "tool_name": emails.TOOL, "tool_input": mail, "cwd": str(cwd)})
    assert held(pre_in(work / "inbox" / "m1"))
    word(y, room)
    assert pre_in(work) == {} and len(ch.notes) == 1                  # released from the work folder: no second card
    assert email(y, mail)["ok"] and len(sent()) == 1
    assert held(pre_in(work / "inbox" / "m1")) and email(y, mail)["error"] == words.EMAIL_NOT_RELEASED   # once
    assert [r["decision"] for r in y.ledger.rows(10, "gates")].count("approved") == 1      # one word


def test_a_persons_room_never_emails_as_the_owner_and_nothing_of_it_is_shown_there(tmp_path):
    y, room, ch, mail = setup(tmp_path, people=True)
    out = pre(y, mail)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == words.EMAIL_NOT_SENT.format(why=words.EMAIL_OWNER_ROOMS)
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == []                 # no preview, nothing to approve
    word(y, room)
    assert email(y, mail)["error"] == words.EMAIL_NOT_SENT.format(why=words.EMAIL_OWNER_ROOMS) and Gmail.log == []
    assert email(y, mail, room="no-such-room")["error"] == words.EMAIL_NOT_SENT.format(why=words.EMAIL_OWNER_ROOMS)


def test_an_email_that_cannot_go_is_said_before_the_owner_is_asked_for_a_word(tmp_path, monkeypatch):
    y, room, ch, mail = setup(tmp_path)
    (outside := tmp_path / "elsewhere.pdf").write_bytes(b"x")
    cases = [({**mail, "to": ["Desk Person"]}, words.EMAIL_NO_ADDRESS.format(given="Desk Person")), ({**mail, "to": []}, words.EMAIL_NO_TO),
             ({**mail, "subject": " ", "text": "  "}, words.EMAIL_EMPTY), ({**mail, "subject": "one\ntwo"}, words.EMAIL_SUBJECT),
             ({**mail, "files": [str(outside)]}, words.EMAIL_FILE_OUTSIDE.format(name="elsewhere.pdf")),
             ({**mail, "files": ["slip.pdf"]}, words.EMAIL_FILE_OUTSIDE.format(name="slip.pdf")),       # a name alone is no path
             ({**mail, "files": [mail["files"][0] + ".gone"]}, words.EMAIL_FILE_NONE.format(name="slip.pdf.gone")),
             ({**mail, "files": [str(tmp_path / "secrets")]}, words.EMAIL_FILE_OUTSIDE.format(name="secrets"))]
    for ti, why in cases:
        out = pre(y, ti)["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"] == words.EMAIL_NOT_SENT.format(why=why), why
    monkeypatch.setattr(emails, "FILES", 1000)
    assert pre(y, mail)["hookSpecificOutput"]["permissionDecisionReason"] == words.EMAIL_NOT_SENT.format(
        why=words.EMAIL_FILES_BIG.format(size="2 KB", limit="1000 bytes"))
    y.conf.pop("gmail")
    assert pre(y, mail)["hookSpecificOutput"]["permissionDecisionReason"] == words.EMAIL_NOT_SENT.format(why=words.EMAIL_NO_SETUP)
    assert ch.notes == [] and y.ledger.rows(5, "approvals") == [] and Gmail.log == []
    assert [(r["gate"], r["decision"]) for r in y.ledger.rows(20, "gates")] == [("email", "deny")] * (len(cases) + 2)


def test_no_email_goes_without_its_preview_with_the_rule_off_or_a_hook_that_failed_open(tmp_path):
    y, room, ch, mail = setup(tmp_path, gate={"off": ["email-out"]})
    assert pre(y, mail) == {} and ch.notes == []                                  # nothing held, so nothing shown
    word(y, room)
    assert email(y, mail) == {"ok": False, "maybe_sent": False, "error": words.EMAIL_NOT_RELEASED} and Gmail.log == []


def test_an_email_that_did_not_go_is_told_apart_from_one_that_may_have(tmp_path):
    y, room, ch, mail = setup(tmp_path)
    (g, (c, parts, _)) = y.conf["gmail"], y.email_check("example-room", mail)
    for mode, why in (("away", words.EMAIL_AWAY), ("login", words.EMAIL_LOGIN), ("refuse", words.EMAIL_REFUSED)):
        Gmail.mode = mode
        assert emails.send(g, y.private, c, parts) == {"ok": False, "maybe_sent": False, "error": words.EMAIL_NOT_SENT.format(why=why)}
    assert "abcdefghijklmnop" not in words.EMAIL_LOGIN and sent() == []           # Gmail's refusal, which may echo it, is never said
    Gmail.mode = "drop"
    assert emails.send(g, y.private, c, parts) == {"ok": False, "maybe_sent": True, "error": words.EMAIL_MAYBE}
    assert "never send it again unasked" in words.EMAIL_MAYBE
    Gmail.mode = "some"
    assert emails.send(g, y.private, c, parts)["refused"] == [TO]
    assert emails.send({**g, "password_file": str(tmp_path / "none")}, y.private, c, parts)["error"] == words.EMAIL_NOT_SENT.format(
        why=words.EMAIL_NO_SETUP)


def test_an_email_that_may_have_gone_out_is_ledgered_so(tmp_path):
    y, room, ch, mail = setup(tmp_path)
    pre(y, mail)
    word(y, room)
    assert pre(y, mail) == {}
    Gmail.mode = "drop"
    assert email(y, mail) == {"ok": False, "maybe_sent": True, "error": words.EMAIL_MAYBE}
    assert y.ledger.rows(1, "gates")[0]["decision"] == "maybe sent"


def test_the_app_password_is_read_as_yerk_import_gmail_reads_it():
    assert sources.emails.app_password is emails.app_password                     # one reader for the import and the send
    assert [emails.app_password(t) for t in (f"User: {OWNER}\nApp password (no spaces): abcdefghijklmnop\n", " abcd efgh ijkl mnop \n",
                                             "App password: abcd efgh ijkl mnop", "", "two\nlines")] == [
        "abcdefghijklmnop", "abcdefghijklmnop", "abcdefghijklmnop", "", ""]
    assert emails.password({"user": OWNER}, Path("/nonexistent")) == "" == emails.password({}, Path("/nonexistent"))


def test_a_worker_never_emails_as_the_owner(tmp_path):
    out = gates.worker_check({"tool_name": emails.TOOL, "tool_input": {"to": [TO], "subject": "s", "text": "t"}}, tmp_path,
                             {"room": "example-room"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "a worker never takes one" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_file_rewritten_after_the_owners_word_is_held_again_and_never_goes_as_its_new_bytes(tmp_path):
    """The word is for the bytes the owner was shown: a file held by its path alone went out as whatever it held when the
    tool read it, so one rewritten after the word, or between the release and the read, went as bytes no one approved."""
    y, room, ch, mail = setup(tmp_path)
    slip = Path(mail["files"][0])
    shown, other = slip.read_bytes(), b"%PDF-1.4 another slip\n" * 100           # the same size: the preview cannot tell
    assert held(pre(y, mail)) and "approve" in word(y, room)["hookSpecificOutput"]["additionalContext"]
    slip.write_bytes(other)                                                       # rewritten after the word
    assert held(pre(y, mail)) and len(ch.notes) == 2 and "attached: slip.pdf (2 KB)" in ch.notes[1]   # shown again, held
    assert email(y, mail)["error"] == words.EMAIL_NOT_RELEASED and sent() == []
    assert [hashlib.sha256(b).hexdigest() in r["call"] for r, b in zip(y.ledger.rows(5, "approvals"), (other, shown))] == [True] * 2
    slip.write_bytes(shown)                                                       # back to the bytes the word was for
    assert pre(y, mail) == {}                                                     # released: the call the owner approved
    slip.write_bytes(other)                                                       # rewritten between the release and the read
    assert email(y, mail)["error"] == words.EMAIL_NOT_RELEASED and sent() == [] and Gmail.log == []
    word(y, room)                                                                 # the word for the bytes shown second
    assert pre(y, mail) == {} and email(y, mail)["ok"]
    [part] = list(sent()[0].iter_attachments())
    assert part.get_content() == other and len(sent()) == 1


def test_a_link_swapped_on_between_the_check_and_the_read_never_attaches_a_file_outside_the_rooms_folders(tmp_path, monkeypatch):
    """The check and the read are one open: a file swapped for a link to a file outside the room's folders, or a folder on its
    path for a link to one outside, once the check had passed but before the read, attached the outside file; a pipe swapped
    in held the read open for good. Each is refused now, at the tool and at the gate, and nothing of the outside file shows."""
    y, room, ch, mail = setup(tmp_path)
    slip, may = Path(mail["files"][0]), y.may_send
    shown, none = slip.read_bytes(), words.EMAIL_NOT_SENT.format(why=words.EMAIL_FILE_NONE.format(name="slip.pdf"))
    (outside := tmp_path / "outside").mkdir()
    (outside / "slip.pdf").write_bytes(secret := b"SECRET outside the room\n" * 300)
    flips = {"the file for a link": lambda: (slip.unlink(), slip.symlink_to(outside / "slip.pdf")),
             "a folder on its path for a link": lambda: (slip.parent.rename(slip.parent.with_name("m0")), slip.parent.symlink_to(outside)),
             "the file for a pipe": lambda: (slip.unlink(), os.mkfifo(slip))}
    for kind, flip in flips.items():
        assert held(pre(y, mail)) and word(y, room) and pre(y, mail) == {}       # shown and released while it was the room's
        monkeypatch.setattr(y, "may_send", lambda r, p, flip=flip: (may(r, p), flip())[0])    # swapped the moment the check passed
        assert email(y, mail) == {"ok": False, "maybe_sent": False, "error": none}, kind
        monkeypatch.setattr(y, "may_send", may)
        for x in (slip.parent, slip.parent.with_name("m0")):
            x.unlink() if x.is_symlink() else shutil.rmtree(x, ignore_errors=True)
        slip.parent.mkdir()
        slip.write_bytes(shown)
    monkeypatch.setattr(y, "may_send", lambda r, p: (may(r, p), flips["the file for a link"]())[0])
    assert pre(y, mail)["hookSpecificOutput"]["permissionDecisionReason"] == none                # the gate's own read, the same
    assert sent() == [] and len(ch.notes) == 3 and words.size(len(secret)) not in str(ch.notes) and secret not in str(y.ledger.rows(20, "gates")).encode()


def test_an_email_with_no_gmail_name_says_so_on_its_from_line_before_the_word_and_in_the_answer_after_it(tmp_path):
    """need 102 of 2026-11-16, the wall: every email a room sent in the owner's name went under the bare address, while the
    owner's own mail carried the owner's name beside it, since [gmail] name was not set; the preview showed the bare address
    either way, and yerkd.toml is out of a room's reach, so neither the owner nor the room saw it. The preview now shows the
    From line as it goes, marked when it has no name, and the send's answer names the From line it went under."""
    y, room, ch, mail = setup(tmp_path)
    del y.conf["gmail"]["name"]
    assert emails.nameless(y.conf["gmail"]) and held(pre(y, mail))
    assert ch.notes[0].split("\n")[3] == f"from: {OWNER} ({words.EMAIL_NAMELESS})"          # seen before the word
    word(y, room)
    assert pre(y, mail) == {} and (r := email(y, mail))["ok"] and r["from"] == OWNER        # and named in the answer after it
    assert sent()[0]["From"] == OWNER                                                       # no name is ever made up
    for blank in ("", "  ", "\n"):                                                          # a blank name is no name
        assert emails.sender({**y.conf["gmail"], "name": blank}) == OWNER and emails.nameless({**y.conf["gmail"], "name": blank})
    assert not emails.nameless({"user": OWNER}) and not emails.nameless({**y.conf["gmail"], "name": "Example Owner"})


def test_a_gmail_name_is_one_line_on_the_from_line_never_a_header_of_its_own_and_reads_as_written(tmp_path):
    """A line break in [gmail] name ended the From header: the email failed to build, or a line after it read as a header.
    A name past ASCII goes on the wire encoded, and the preview shows it as written, never that encoding."""
    y, room, ch, mail = setup(tmp_path)
    y.conf["gmail"]["name"] = f"Example\r\nBcc: {TO}\n  Owner"
    msg = emails.message(y.conf["gmail"], emails.normal(mail), [])
    assert msg["Bcc"] is None and msg["From"] == f'"Example Bcc: {TO} Owner" <{OWNER}>' and "\nBcc:" not in msg.as_string()
    y.conf["gmail"]["name"] = "Zoë Example"                                      # a name past ASCII reads as written
    assert held(pre(y, mail)) and ch.notes[-1].split("\n")[3] == f"from: Zoë Example <{OWNER}>"
    msg = emails.message(y.conf["gmail"], emails.normal(mail), [])
    assert msg["From"] == f"Zoë Example <{OWNER}>" and "From: =?utf-8?q?Zo=C3=AB?= Example" in msg.as_string()   # its wire form
