"""Checks watch audit: a room with `watch_audit = true`, watching a channel where another assistant answers, reads each message there
together with that assistant's answer, under a line asking for a verdict on the answer, naming who sent the message and who reads the
room; a final of NO_REPLY on that turn is never kept quiet. The reason for the key: the room should always answer on such a turn and
check the other assistant's work, saying whether it is good, partly right or wrong with the green, orange and red marks.
along as before. The adapter runs on test_watch's fakes, the room on the fake Claude (test_room)."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import discord
import pytest

import yerk_ai.channels.discord as dmod
from yerk_ai import config, owndocs, words
from yerk_ai.channels.discord import DiscordChannel
from yerk_ai.config import REPO, rooms
from yerk_ai.ledger import Ledger
from yerk_ai.room import Inbound

from test_discord import HOOK, PERSON, ROOM_CH, Msg, card_hows, dc, on_a_loop  # noqa: F401  (dc: the adapter fixture)
from test_room import Chan, env, wait_idle  # noqa: F401  (env: the room fixture)
from test_watch import HELPER, LINE, OWN, OWNER, WATCHED, Bot, Past, Spy, Watching

VERDICT, QUIET = f"{words.NOT_DONE} {words.NO_VERDICT}", f"{words.DONE} {words.QUIET}"


def audit_line(sender, readers):
    """The line as it reaches the room, spelled out: who=Helper, channel=lounge."""
    return ("[sent to Helper in #lounge, not to this room: " + sender + " is talking to Helper there. Helper's answer follows the "
            "message. AUDIT it and post your verdict here: check it against the files and the thread, not recall; first line 🟢 "
            "looks good (a nit rides under it in one line, or is dropped), 🟠 only when something is actually wrong and it changes "
            "what " + sender + " would say or do, or 🔴 wrong, then what is wrong and the fix, written out in full "
            "in plain words, because " + readers + " read this room; one or two lines when it is right. If Helper had not answered "
            "within the wait, say that in one line. Never NO_REPLY on this turn; never redo work Helper got right.]")


class Guild:
    """The watched channel's server: a member's name as it shows them, one call each (the adapter keeps no member cache)."""
    def __init__(self, names):
        self.id, self.filesize_limit, self.names, self.asked = 1, 0, names, []

    async def fetch_member(self, uid):
        self.asked.append(uid)
        if uid not in self.names:
            raise discord.NotFound(NS(status=404, reason="Not Found"), {"code": 10007, "message": "Unknown Member"})
        return NS(id=uid, display_name=self.names[uid], name=f"handle-{uid}")


def auditing(tmp_path, conf=None, people=frozenset({PERSON}), names=None):
    """A room in OWN watching #lounge, where `[watch] assistants` lists HELPER, its rooms.toml `conf` (default watch_audit = true)."""
    tmp_path.mkdir(exist_ok=True)
    own, watched = Spy(OWN), Past(WATCHED)
    watched.name, watched.guild = "lounge", Guild({OWNER: "Examplia", PERSON: "Quuxa"} if names is None else names)
    room = Watching(tmp_path, {"watch_audit": True} if conf is None else conf)
    link = DiscordChannel("token", OWNER, [HOOK], Ledger(tmp_path / "l.sqlite"), client=Bot(own, watched), state=tmp_path,
                        assistants=frozenset({HELPER}))
    link.add_room(OWN, room, watch=WATCHED, people=people)
    return link, room, watched


def said(watched, mid, text, author, name):
    m = Msg(watched, mid, text, author=author, name=f"handle-{author}")
    m.author.display_name = name                          # as the server shows the sender
    return m


def turns(link, *msgs, answer=None):
    """Each message in the watched channel, the other assistant's `answer` after the last; the time it was sent."""
    async def run():
        link.loop = asyncio.get_running_loop()
        for m in msgs:
            await link.on_message(m)
        at = datetime.now(timezone.utc)
        if answer is not None:
            reply = Msg(msgs[0].channel, msgs[-1].id + 1, answer, author=HELPER, name="helper-bot")
            reply.created_at = at
            await link.on_message(reply)
        await asyncio.sleep(0.6)
        return at
    return asyncio.run(run())


def test_a_watch_audit_rooms_held_message_comes_in_to_be_graded_naming_its_sender_and_its_readers(tmp_path, monkeypatch):
    """A message in the watched channel, held for the other assistant's settled answer, reaches the room in one turn under the
    audit line, the answer under it as today: the sender by the name the server shows them by, the readers the owner and the
    room's people by theirs, each fetched once and kept; one the server cannot name is said in words and asked again next turn.
    A room with no people is read by the owner, and nobody's name is fetched. Nothing is written in the watched channel."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.2)
    link, room, watched = auditing(tmp_path)
    at = turns(link, said(watched, 1501, "is the bank open on Monday", PERSON, "Quuxa"), answer="Yes,  it opens at 9.")
    assert [(g.id, g.sender, g.text) for g in room.got] == [("1501", "person", f"{audit_line('Quuxa', 'Examplia and Quuxa')}\n"
                                                             f"is the bank open on Monday\n[Helper answered at {at:%H:%M} in #lounge: Yes, it opens at 9.]")]
    at = turns(link, said(watched, 1511, "and on Tuesday", OWNER, "Examplia"), answer="Tuesday too.")
    assert room.got[-1].text == f"{audit_line('Examplia', 'Examplia and Quuxa')}\nand on Tuesday\n[Helper answered at {at:%H:%M} in #lounge: Tuesday too.]"
    assert watched.guild.asked == [OWNER, PERSON] and link.held == {} and watched.writes == [] and watched.sent == []

    link, room, watched = auditing(tmp_path / "unnamed", {"watch_audit": True, "watch_wait": 0.3}, names={PERSON: "Quuxa"})
    for mid in (1601, 1611):
        turns(link, said(watched, mid, "did he answer", PERSON, "Quuxa"))
    late = "[Helper had not answered in #lounge within 0.3 s of the owner's message]"
    assert [g.text for g in room.got] == [f"{audit_line('Quuxa', 'the owner and Quuxa')}\ndid he answer\n{late}"] * 2
    assert watched.guild.asked == [OWNER, PERSON, OWNER]          # the owner's name, asked again; Quuxa's, kept

    link, room, watched = auditing(tmp_path / "alone", people=frozenset())
    at = turns(link, said(watched, 1701, "how far was the run", OWNER, "Examplia"), answer="Eight miles.")
    assert room.got[-1].text == f"{audit_line('Examplia', 'the owner')}\nhow far was the run\n[Helper answered at {at:%H:%M} in #lounge: Eight miles.]"
    assert watched.guild.asked == []


@pytest.mark.parametrize("conf", [{}, {"watch_audit": False}])
def test_a_room_without_watch_audit_reads_along_as_before_and_asks_the_server_nothing(tmp_path, monkeypatch, conf):
    """Unset or false: the read-along line (SENT_THERE), word for word as before, whoever sent the message."""
    monkeypatch.setattr(dmod, "ANSWER_SETTLE", 0.2)
    link, room, watched = auditing(tmp_path, conf)
    at = turns(link, said(watched, 1801, "is the bank open on Monday", PERSON, "Quuxa"), answer="Yes.")
    assert [g.text for g in room.got] == [f"{LINE}\nis the bank open on Monday\n[Helper answered at {at:%H:%M} in #lounge: Yes.]"]
    assert watched.guild.asked == [] and LINE.startswith("[sent to Helper in #lounge, not to this room: the owner is talking")


def silent_heads(made, conf, rows):
    """The fake Claude answers NO_REPLY to each message (READ_ALONG), one turn each, its inbound row in channel `where`: the
    first line of each turn's kept card, and the rows' notes."""
    make, ledger, _ = made
    chan = Chan()
    room = make(chan, conf={"watch": f"discord:{WATCHED}", **conf})
    heads = []
    for mid, where, sender in rows:
        ledger.inbound_add(mid, "r", where, sender, 20)
        room.submit(Inbound(mid, sender, f"READ_ALONG message {mid}"))
        wait_idle(room)
        heads.append(chan.cards[-1].split("\n")[0])
    room.rp.close()
    assert chan.replies == [] and chan.notices == [] and chan.silent_ones == [[mid] for mid, *_ in rows]   # nothing posted
    return heads, [(ledger.inbound_get(mid)["state"], ledger.inbound_get(mid)["note"]) for mid, *_ in rows]


def test_a_no_reply_on_a_watched_turn_of_a_watch_audit_room_keeps_its_card_as_no_verdict(env):  # noqa: F811
    """A verdict was owed: a final of exactly NO_REPLY still posts nothing and is ledgered silent, but its card is kept saying
    no verdict came (NOT_DONE), never the quiet line. The room knows the turn by the message's inbound row, in the channel it
    watches; one in its own channel, or a relay (channel "worker"), owes no verdict and stays quiet."""
    rows = [("1", str(WATCHED), "person"), ("2", str(OWN), "person"), ("3", "worker", "yerk")]
    heads, notes = silent_heads(env, {"watch_audit": True}, rows)
    assert heads == [VERDICT, QUIET, QUIET] and notes == [("answered", "silent")] * 3
    assert VERDICT == "🔴 no verdict: the room answered nothing" and words.width(VERDICT) <= words.PHONE


@pytest.mark.parametrize("conf", [{}, {"watch_audit": False}])
def test_a_no_reply_in_a_room_without_watch_audit_keeps_the_quiet_card(env, conf):  # noqa: F811
    heads, notes = silent_heads(env, conf, [("1", str(WATCHED), "person")])
    assert heads == [QUIET] and notes == [("answered", "silent")]


def test_a_no_verdict_card_stays_as_history_and_never_takes_a_quiet_card_with_it(dc):  # noqa: F811
    """Through the adapter (`silent`): the card kept with the NO_VERDICT line, nothing posted, its part ledgered silent. Only a
    quiet card supersedes the quiet card before it (quiet-card); a NO_VERDICT card neither goes when a quiet one follows (its
    first line is not the quiet one) nor removes the quiet card before it, like a card kept for an unanswered turn."""
    d, client, _ = dc
    ch, card_up = client.chans[ROOM_CH], on_a_loop(d)
    for n, line in ((5, QUIET), (6, VERDICT), (7, QUIET)):
        card_up()
        assert d.silent("test-room", [str(n)], line, key=f"t{n}") == [str(ch.sent[-1].id)]
    quiet, verdict, last = ch.sent
    assert ch.deleted == [] and [m.content for m in ch.sent] == ["working on it"] * 3 and verdict.edits == [VERDICT]
    assert card_hows(d) == {str(quiet.id): f"kept: {QUIET}", str(verdict.id): f"kept: {VERDICT}", str(last.id): f"kept: {QUIET}"}
    assert [(r["kind"], r["state"]) for r in d.ledger.deliveries("test-room")] == [("silent", "sent")] * 3
    d.loop.call_soon_threadsafe(d.loop.stop)


def test_watch_audit_is_true_or_false_or_the_start_stops(tmp_path):
    """rooms.toml's `watch_audit`: a TOML true or false; anything else (a quoted "true" too) is refused when rooms.toml is
    read, with one plain line naming the room, as a `people` of the wrong shape is."""
    def load(value):
        (tmp_path / "rooms.toml").write_text(f'[rooms.a]\nchannel = "discord:1"\nwatch = "discord:3"\nwatch_audit = {value}\n')
        return rooms(tmp_path)["a"]["watch_audit"]
    assert load("true") is True and load("false") is False
    for value, shown in (('"true"', "'true'"), ("1", "1"), ('"yes"', "'yes'"), ("[true]", "[True]")):
        with pytest.raises(SystemExit) as e:
            load(value)
        assert str(e.value) == f"rooms.toml: a: watch_audit must be true or false, like watch_audit = true, not {shown}"


def test_watch_audit_is_a_known_room_key_and_settings_md_says_what_it_does():
    """The key is read (`yerk doctor` names no typo) and docs/settings.md, written by `yerk docs own`, carries its line."""
    page = (REPO / "docs" / "settings.md").read_text()
    assert "watch_audit" in config.ROOM_KEYS and "settings.md" not in owndocs.stale()
    assert f"- `watch_audit`: {config.ROOM_WORDS['watch_audit']}\n" in page and "never silent (default false" in page
