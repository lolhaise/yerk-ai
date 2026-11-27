"""Checks that every part a room is handed at its session start, such as the recall pack (the notes filed for that room), the open loops
and what is due, fits under its size in budgets.toml by construction, cut by whole lines or items and never mid-line. The case behind it
(seen live): one room's recall pack came out at 6,003 against its 6,000, since the pack kept 60 characters for the line counting
what it leaves out, which is 64 with its newline once that count has three digits, and measured its items in code points where the key
counts UTF-16 units; the session start then cut 600 characters from the pack's middle, mid-line, and said 8 lines went. The pack drops
whole items, the oldest first, its head and that line measured with it; the open loops keep their room; every part's fit keeps whole
lines with its marker counted; and the machine room hears of a real drop only, in one line. Every value here is a placeholder."""
from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from test_recall import NOW, day, tree
from test_room_scope import install
from test_sheet_hard import machine_said

from yerk_ai import context, due, hooks, memory, taste, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.room import Inbound

ROOM = "example-room"
MORE = "({} older items not shown; recall or read_memory reaches them)"


def items(s: memory.Settings, n: int) -> None:
    """`n` filed items of the owner's in today's day log, a thinking face and a skull (two UTF-16 units each) in every tenth."""
    day(s, 0, [f"- 00:{i // 60:02d}:{i % 60:02d} {ROOM} owner: example note {i}{' 🤔💀' if i % 10 == 0 else ''} [m:p{i}]" for i in range(n)])


def pack_part(tmp_path, s: memory.Settings, key: int, loops: str | None = None) -> context.Part:
    """The recall_pack part a session start sends with its key at `key`: the pack from `s`, then `loops` as the open loops."""
    if not (priv := tmp_path / "layer").exists():
        shutil.copytree(EXAMPLE_PRIVATE, priv)
    if loops:
        (priv / "memory" / "open-loops.md").write_text(loops)
    b = budgets()
    b["session_start"]["appended"]["recall_pack"] = key
    conf = {**rooms(priv)[ROOM], "open_loops": "memory/open-loops.md" if loops else None}
    return next(p for p in context.session_start(priv, conf, b, lambda left: (memory.room_pack(s, ROOM, left, NOW)[0], str(s.daily)))
                if p.name == "recall_pack")


def test_a_pack_exactly_at_its_key_goes_whole_and_one_character_over_drops_its_oldest_items_whole(tmp_path):
    s = tree(tmp_path)
    items(s, 30)
    whole, shown = memory.room_pack(s, ROOM, 10 ** 6, NOW)
    key = context.chars(whole)
    assert shown == 30 and key > len(whole) and memory.room_pack(s, ROOM, key, NOW) == (whole, 30)    # emoji count 2 each
    part = pack_part(tmp_path, s, key)
    assert part.text == whole and part.size == key and not part.over and part.cut == ()
    text, shown = memory.room_pack(s, ROOM, key - 1, NOW)                                # one character over
    lines = text.split("\n")
    assert context.chars(text) <= key - 1 and 0 < shown < 30
    assert lines[:-1] == whole.split("\n")[:shown + 1] and lines[-1] == MORE.format(30 - shown)   # the newest whole, the oldest out
    part = pack_part(tmp_path, s, key - 1)
    assert part.text == text and not part.over and part.cut == ()                        # fitted by the pack: nothing dropped


def test_the_pack_never_passes_its_key_with_three_digits_left_out_and_emoji_in_what_it_shows(tmp_path):
    """The live shape: hundreds left out, so the line counting them is 64 with its newline, and emoji in the items shown, on
    every third budget from 1,500 to 2,600 and on the 5,998 the pack was given that morning."""
    s = tree(tmp_path)
    items(s, 160)
    every = memory.room_pack(s, ROOM, 10 ** 6, NOW)[0].split("\n")
    for budget in [*range(1500, 2600, 3), 5998]:
        text, shown = memory.room_pack(s, ROOM, budget, NOW)
        lines = text.split("\n")
        assert context.chars(text) <= budget and lines[-1] == MORE.format(160 - shown), budget
        assert lines[:-1] == every[:shown + 1], budget                                   # whole items, never part of one
    assert 160 - memory.room_pack(s, ROOM, 2599, NOW)[1] >= 100


def test_the_head_alone_over_the_key_sends_no_pack_and_drops_nothing(tmp_path):
    s = tree(tmp_path)
    items(s, 5)
    head = memory.room_pack(s, ROOM, 10 ** 6, NOW)[0].split("\n")[0]
    with_more = context.chars(head) + 1 + context.chars(MORE.format(5))
    for key in (0, 10, context.chars(head) - 1, context.chars(head), with_more - 1):    # the head, never the line with it
        assert memory.room_pack(s, ROOM, key, NOW) == ("", 0), key
        part = pack_part(tmp_path, s, key)
        assert part.text == "" and part.cut == () and not part.over, key
    assert memory.room_pack(s, ROOM, with_more, NOW) == (f"{head}\n{MORE.format(5)}", 0)


def test_open_loops_alone_over_the_key_keep_their_room_and_lose_whole_lines(tmp_path):
    s = tree(tmp_path)
    items(s, 30)
    loops = "# Open commitments (example, invented)\n\n" + "".join(
        f"- Example promise {i}: kept the example way. surface: on the {i % 28 + 1}th.\n" for i in range(80))
    part = pack_part(tmp_path, s, 2000, loops)
    sent = [x for x in part.text.split("\n") if x and not x.startswith(("[from ", "[...trimmed "))]
    assert part.over and part.size <= 2000 and "[recall pack]" not in part.text           # the loops first: no room left for it
    assert sent and all(x in loops.split("\n") for x in sent)                            # whole lines of the owner's, never part of one
    assert part.cut and all(x.startswith("- Example promise ") and x not in sent for x in part.cut)
    small = pack_part(tmp_path, s, 2000, "- Example promise: one.\n")
    assert small.size <= 2000 and small.cut == () and small.text.startswith("[recall pack]")
    assert small.text.endswith("\n\n[from " + str((tmp_path / "layer" / "memory" / "open-loops.md").resolve()) + "]\n- Example promise: one.")


def test_every_appended_part_fits_its_key_by_whole_lines_on_a_layer_past_every_key(tmp_path):
    s = tree(tmp_path)
    items(s, 60)
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "layer")
    (rules := tmp_path / "rules.md").write_text("".join(f"- example job {i}: done the example way, step by step.\n" for i in range(40)))
    conf = rooms(priv)[ROOM]

    def pack(left: int) -> tuple[str, str]:
        return memory.room_pack(s, ROOM, left, NOW)[0], str(s.daily)
    b = budgets()
    b["session_start"]["appended"] = {k: 10 ** 6 for k in context.APPENDED}
    full = {p.name: p.text for p in context.session_start(priv, conf, b, pack, rules)}
    assert all(full.values()) and set(full) == set(context.APPENDED)                     # every part has lines to lose
    for share in (0.9, 0.5, 0.2, 0.05):
        b["session_start"]["appended"] = {k: int(context.chars(full[k]) * share) for k in context.APPENDED}
        for p in context.session_start(priv, conf, b, pack, rules):
            src, sent = full[p.name].split("\n"), p.text.split("\n")
            assert p.size <= p.budget and (p.over or p.name == "recall_pack"), (p.name, share)   # the marker counted too
            assert p.name != "recall_pack" or p.over or (p.cut == () and MORE.format("")[1:] in p.text), share      # its oldest out
            assert all(x in src for x in sent if x and not x.startswith(("[...trimmed ", "("))), (p.name, share)
            assert all(x in src and x not in sent for x in p.cut), (p.name, share)


def test_the_machine_room_hears_of_a_real_drop_only_with_its_count_and_key_in_one_line(tmp_path, capsys):
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    (state := tmp_path / "state").mkdir()
    y = Yerkd(priv, state)
    y.room_confs["example-machine"] = {**y.room_confs[ROOM], "role": "machine", "id": "example-machine-id"}
    for name in (ROOM, "example-machine"):
        y.add_room(name, ConsoleChannel())
    y.memory.daily.mkdir(parents=True, exist_ok=True)
    today = datetime.now(y.memory.tz).date().isoformat()
    (y.memory.daily / f"{today}.md").write_text(f"# {today}\n\n" + "".join(
        f"- 00:{i // 60:02d}:{i % 60:02d} {ROOM} owner: example note {i} 🤔 [m:q{i}]\n" for i in range(400)))
    y.spec_for(ROOM)
    cap = y.budgets["session_start"]["appended"]["recall_pack"]
    sent = (state / "rooms" / ROOM / "context.md").read_text()
    assert "older items not shown" in sent and "[...trimmed" not in sent                 # its oldest out by construction
    assert machine_said(capsys) == []                                                    # the part fits: nothing said
    (priv / "memory" / "open-loops.md").write_text("# Open commitments (example, invented)\n\n" + "".join(
        f"- Example promise {i}: kept the example way. surface: on the {i % 28 + 1}th.\n" for i in range(200)))
    y.spec_for(ROOM)
    part = next(p for p in context.session_start(y.private, y.room_confs[ROOM], y.budgets, lambda left: y.room_pack(ROOM, left),
                                                y.memory.root / memory.RULES) if p.name == "recall_pack")
    assert part.cut and all(x.startswith("- Example promise ") for x in part.cut)       # the owner's loops, whole
    assert machine_said(capsys) == [f"{words.WORKING} recall_pack: {len(part.cut)} lines dropped\nto fit its {cap:,} in {ROOM}"]
    assert context.chars(y.compact_pack(ROOM)) <= cap                                    # the re-add after a compaction fits alike


def test_a_part_is_cut_in_claude_codes_characters_its_marker_too_and_a_cut_never_says_a_from_line_or_keeps_part_of_one():
    """A part under its key in code points but over it as Claude Code counts (an emoji outside the basic plane counts 2) is
    cut, whole lines, the marker's numbers in the same characters; a middle that holds another file's `[from` line loses it
    with that file's lines, and only those lines are said; a rule file whose first line alone is past its room keeps none of
    that line rather than part of it."""
    text = "[from /example/pinned.md]\n" + "\n".join(f"- example fact {i} 🤔💀" for i in range(200))
    key = len(text) + 10                                                                 # 400 emoji: over the key by 390
    sent, budget, raw, _, cut = context.fit("pinned_memory", text, key, "/example/pinned.md")
    head, _, rest = sent.partition("\n\n[...trimmed pinned_memory: ")
    tail = rest.partition("]\n\n")[2]
    assert context.chars(sent) <= key < raw == context.chars(text) and cut and budget == key
    assert f"kept {context.chars(head)}+{context.chars(tail)} of {raw} chars" in rest
    assert all(x in text.split("\n") for x in (head + "\n" + tail).split("\n"))
    notes = "\n\n".join(context.cited(Path(f"/example/daily/2026-01-0{d}.md"), "\n".join(f"- example note {d}.{i}" for i in range(40)))
                        for d in (3, 2, 1))
    sent, _, _, _, cut = context.fit("recall_pack", notes, 600)
    assert "[from /example/daily/2026-01-02.md]" not in sent and "- example note 2.0" in cut
    assert all(x.startswith("- example note ") and x not in sent.split("\n") for x in cut)   # the file's lines, never its `[from`
    rules = "- example rule " + "x" * 400 + "\n" + "\n".join(f"- example rule {i}" for i in range(10))
    sent, _, _, _, cut = context.fit("rules", rules, 300)
    assert context.chars(sent) <= 300 and "x" * 10 not in sent and len(cut) == 11


def test_the_per_message_parts_fit_their_keys_by_whole_lines_too():
    """The topic hit's absence lines, code's word, go whole while they fit; the taste part, still over with each reply at 80,
    drops its last reply whole where it cut characters off the end."""
    cap = budgets()["per_message"]["on_hit"]["recall"]
    absent = [f"example thing {i}" for i in range(80)]
    lines = hooks.render_recall([], [], cap, [], absent).split("\n")
    assert context.chars("\n".join(lines)) <= cap and 0 < len(lines) < 80
    assert lines == [words.ABSENT.format(what=a) for a in absent[:len(lines)]]
    rows = [{"room": ROOM, "reply_id": i, "verdict": "pos" if i % 2 else "neg", "signal": "😤" * 200, "reply": "😤" * 900} for i in range(6)]
    part = taste.render(rows, ROOM, 700)
    assert context.chars(part) <= 700 and part.count("\n") >= 1
    assert all(x == taste.HEAD or (x.startswith(("👍", "👎")) and x.endswith('…"')) for x in part.split("\n"))


# the due part (due.part, 2026-11-25): what is overdue or due in a week, first in the pack, inside its limit
FILED_IN = "(example-room#rid, 2026-01-02 09:00:00+00:00)"


def on(n: int) -> str:
    """The day `n` days from NOW."""
    return (NOW + timedelta(days=n)).date().isoformat()


def owed(s: memory.Settings, *lines: str) -> list[dict]:
    """A fact file of filed lines (each `words | mark`), and the list yerk-index writes from it."""
    rows = [f"- {w} {FILED_IN} [due {m}] [m:m-{i}]" for i, (w, _, m) in enumerate(x.partition(" | ") for x in lines)]
    (s.root / "facts" / "example-owed.md").write_text("# Owed\n\n" + "\n".join(rows) + "\n")
    due.Builder(s).refresh(s.scope.files(), NOW)
    return due.Index(s.due).load()


def test_the_due_part_is_overdue_oldest_first_then_the_next_seven_days_soonest_first_a_line_each_with_its_cite(tmp_path):
    s = tree(tmp_path)                                                                   # NOW: 2026-01-20 15:00 in its zone
    items_ = owed(s, f"Seven days out | {on(7)}", f"Eight days out | {on(8)}", f"Tomorrow | {on(1)} owes Example Roofers", f"Yesterday | {on(-1)}",
                  f"Today, over by one hour | {on(0)}T14:00", f"Today, in one hour | {on(0)}T16:00", f"Today | {on(0)}", f"Fourteen days ago | {on(-14)}")
    text = due.part(s, items_, None, 10 ** 6, NOW)
    head, *rows, last = text.split("\n")
    cite = "(memory/facts/example-owed.md:{})"
    assert rows == [f"- {on(-14)}: Fourteen days ago {cite.format(10)}", f"- {on(-1)}: Yesterday {cite.format(6)}",
                    f"- {on(0)} 14:00: Today, over by one hour {cite.format(7)}", f"- {on(0)}: Today {cite.format(9)}",
                    f"- {on(0)} 16:00: Today, in one hour {cite.format(8)}", f"- {on(1)}, waiting on Example Roofers: Tomorrow {cite.format(5)}",
                    f"- {on(7)}: Seven days out {cite.format(3)}"]
    assert head == due.HEAD.format(at="2026-01-20 15:00", days=7) and last == due.MORE.format(1)      # the one eight days out
    assert [x["text"] for x in due.listed(s, items_, None, NOW)][-1] == "Eight days out" and "Eight days out" not in text
    assert [x["overdue"] for x in due.listed(s, items_, None, NOW)] == [True] * 3 + [False] * 5
    assert due.part(s, items_[1:2], None, 10 ** 6, NOW) == due.FAR.format(at="2026-01-20 15:00", days=7, n=1)    # nothing near: one line


def test_the_day_a_dated_line_is_due_is_the_installs_own_day_and_a_time_its_own_minute(tmp_path):
    s = replace(tree(tmp_path), tz=ZoneInfo("America/Vancouver"))                       # 15:00 UTC is 07:00 there
    items_ = owed(s, "This morning | 2026-01-20T07:00", "Earlier | 2026-01-20T06:59", "Yesterday | 2026-01-19", "Today | 2026-01-20")
    assert [(x["text"], x["overdue"]) for x in due.listed(s, items_, None, NOW)] == [
        ("Yesterday", True), ("Earlier", True), ("Today", False), ("This morning", False)]
    at = datetime(2026, 1, 21, 2, 0, tzinfo=timezone.utc)                                # still the 20th there
    assert [x["text"] for x in due.listed(s, items_, None, at) if not x["overdue"]] == ["Today"]
    assert "as of 2026-01-20 18:00" in due.part(s, items_, None, 10 ** 6, at)


def test_thirty_open_items_give_twelve_lines_the_last_with_the_count_and_the_key_takes_whole_lines(tmp_path):
    s = tree(tmp_path)
    items_ = owed(s, *(f"Example thing {i:02d} | {on(i % 7)}" for i in range(30)))
    text = due.part(s, items_, None, 10 ** 6, NOW)
    lines = text.split("\n")
    assert len(lines) == due.LINES == 12 and lines[-1] == "(20 more open, not shown; `recall` with `since` and `until` lists what is due in a range)"
    assert [x["cite"] for x in due.listed(s, items_, None, NOW)][:10] == [x.rpartition("(")[2][:-1] for x in lines[1:-1]]
    whole = [due.part(s, items_[:n], None, 10 ** 6, NOW).split("\n") for n in (1, 11, 12)]
    assert [len(x) for x in whole] == [2, 12, 12] and not whole[1][-1].startswith("(") and whole[2][-1] == due.MORE.format(2)
    for budget in range(0, context.chars(text) + 1, 7):                                  # never past its characters, never part of a line
        got = due.part(s, items_, None, budget, NOW)
        assert context.chars(got) <= budget, budget
        rows = got.split("\n") if got else []
        assert not rows or rows[:-1] == lines[:len(rows) - 1] and rows[-1] == due.MORE.format(30 - (len(rows) - 2)) and len(rows) >= 3, budget
    key = budgets()["session_start"]["recall_pack"]["due"]
    assert context.chars(text) < key < budgets()["session_start"]["appended"]["recall_pack"]
    long = owed(s, *(f"{'Example thing 🤔 ' * 40}{i:02d} | {on(0)}T16:00 owes {'Example Roofers ' * 9}" for i in range(30)))
    got = due.part(s, long, None, key, NOW)
    assert context.chars(got) <= key and "…" in got and 3 <= len(got.split("\n")) <= 12 and got.endswith(due.MORE.format(30 - (len(got.split("\n")) - 2)))


def with_due(tmp_path, s: memory.Settings, key: int, items_: list[dict], loops: str | None = None, most: int = 2400) -> tuple[context.Part, context.Part]:
    """The recall_pack part with no due callable, and with one."""
    b = budgets()
    b["session_start"]["appended"]["recall_pack"] = key
    plain = pack_part(tmp_path, s, key, loops)
    conf = {**rooms(tmp_path / "layer")[ROOM], "open_loops": "memory/open-loops.md" if loops else None}
    parts = context.session_start(tmp_path / "layer", conf, b, lambda left: (memory.room_pack(s, ROOM, left, NOW)[0], str(s.daily)),
                                  due=lambda left: due.part(s, items_, None, min(left, most), NOW))
    return plain, next(p for p in parts if p.name == "recall_pack")


def test_the_due_part_and_the_open_loops_file_both_fit_inside_the_limit_and_the_room_still_gets_the_file(tmp_path):
    s = tree(tmp_path)
    items(s, 60)
    items_ = owed(s, *(f"Example thing {i:02d} of the example list | {on(i % 7)}" for i in range(30)))
    loops = "# Open commitments (example, invented)\n\n" + "".join(f"- Example promise {i}: kept the example way.\n" for i in range(20))
    for key in (2000, 2500, 3000, 4000, 6000):
        _, part = with_due(tmp_path, s, key, items_, loops)
        assert part.size <= key and part.cut == () and not part.over, key
        at = (tmp_path / "layer" / "memory" / "open-loops.md").resolve()
        assert part.text.endswith(f"\n\n[from {at}]\n{loops.strip()}"), key               # the file, whole
        first, *_ = part.text.split("\n\n")
        assert first.startswith("[due] ") and len(first.split("\n")) <= 12 and first.split("\n")[-1].startswith("("), key
    _, part = with_due(tmp_path, s, 6000, items_, loops)
    assert part.text.index("[due] ") == 0 < part.text.index("[recall pack] ") < part.text.index("[from ")
    big = loops + "".join(f"- Example promise {i}: kept the example way, whatever it takes.\n" for i in range(20, 90))
    plain, part = with_due(tmp_path, s, 5000, items_, big)                               # the file alone leaves no room: it keeps its own
    assert part == plain and "[due]" not in part.text
    none, part = with_due(tmp_path, s, 6000, items_)                                     # and with no such file
    assert part.size <= 6000 and part.text.startswith("[due] ") and "\n\n[recall pack] " in part.text and "[due]" not in none.text


def test_a_room_with_nothing_open_gets_the_pack_it_gets_today_byte_for_byte_and_one_with_all_of_it_further_out_one_line_first(tmp_path):
    s = tree(tmp_path)
    items(s, 60)
    loops = "# Open commitments (example, invented)\n\n- Example promise: one.\n"
    for key in (0, 300, 2000, 6000):
        for file in (None, loops):
            plain, part = with_due(tmp_path, s, key, [], file)
            assert part == plain, (key, file)
    closed = owed(s, f"Fifteen days ago | {on(-15)}")                                    # past its fourteen days: nothing open
    plain, part = with_due(tmp_path, s, 6000, closed, loops)
    assert part == plain and part.text.startswith("[recall pack] ")
    far = owed(s, f"Eight days out | {on(8)}", f"Fifteen days ago | {on(-15)}")
    plain, part = with_due(tmp_path, s, 6000, far, loops)
    one = due.FAR.format(at="2026-01-20 15:00", days=7, n=1)
    assert part.text.startswith(one + "\n\n[recall pack] ") and part.size <= 6000 and not part.over and plain.text.startswith("[recall pack] ")


def test_a_room_starts_and_compacts_with_what_is_due_and_an_uncapped_one_lifts_its_characters_never_its_lines(tmp_path):
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    (state := tmp_path / "state").mkdir()
    y = Yerkd(priv, state)
    y.add_room(ROOM, ConsoleChannel()).inflight = [Inbound("m-1", "owner", "a placeholder message")]
    start = state / "rooms" / ROOM / "context.md"
    y.spec_for(ROOM)
    assert "[due]" not in start.read_text() and "[due]" not in y.compact_pack(ROOM)
    today = datetime.now(y.memory.tz).date()
    for i in range(20):
        r = asyncio.run(y.tool_remember(ROOM, f"Example thing {i:02d} {'of the example list ' * 8}", "", [], due=(today + timedelta(days=i % 5)).isoformat()))
        assert r["ok"]
    r = asyncio.run(y.tool_remember(ROOM, "I will bring the key", "", [], said="Dana", source="message m-9", due=(today - timedelta(days=3)).isoformat()))
    due.Builder(y.memory).refresh(y.memory.scope.files())                                # yerk-index's pass
    y.spec_for(ROOM)
    part = next(p for p in start.read_text().split("\n\n") if p.startswith("[due] "))
    rows = part.split("\n")
    assert context.chars(part) <= 2400 and 8 < len(rows) <= 12 and rows[-1].startswith("(1") and rows[-1].endswith(
        " more open, not shown; `recall` with `since` and `until` lists what is due in a range)")
    assert rows[1] == f'- {today - timedelta(days=3)}: [their words] Dana: "I will bring the key" (from message m-9) ({r["cite"]})'
    assert y.compact_pack(ROOM).split("\n\n")[0].split("\n")[1:] == rows[1:]            # after a compaction too; its head says when
    asyncio.run(y.tool_remember(ROOM, "Dana brought the key.", "", [r["cite"]]))          # its outcome filed clears it
    due.Builder(y.memory).refresh(y.memory.scope.files())
    assert "I will bring the key" not in y.compact_pack(ROOM).split("\n\n")[0]
    y.budgets["session_start"]["recall_pack"]["due"] = 700
    small = y.due_pack(ROOM)
    assert 0 < context.chars(small) <= 700 and len(small.split("\n")) < 8
    y.budgets["limits"] = {"uncapped": True}
    assert len(y.due_pack(ROOM, context.NONE).split("\n")) == 12 and y.due_pack(ROOM, 700) == small     # what is left still bounds it
    assert y.compact_pack(ROOM).startswith("[due] ")


def test_a_peoples_rooms_part_holds_its_own_items_never_the_owners_and_a_sealed_room_its_own(tmp_path):
    """What a room may read is what `recall` in it may return (memory.recalled): the owner's fact, note and said line reach no person's
    room and no sealed one; a person's own item reaches that room's part, and an owner room only when it asks (due.unasked: no part
    shows another room's own line); a shared file every room but a sealed one."""
    y = install(tmp_path, sealed=True, family=True)
    owner, sealed, hers = "example-room", "frozen-room", "family-room"
    when = (datetime.now(y.memory.tz).date() + timedelta(days=2)).isoformat()

    def file(room: str, text: str, about: str = "", **kw: str) -> None:
        y.rooms[room].inflight = [Inbound(f"m-{room}", "owner", "a placeholder message")]
        assert asyncio.run(y.tool_remember(room, text, about, [], due=when, **kw))["ok"]
    for room in (owner, sealed, hers):
        y.add_room(room, ConsoleChannel())
    file(owner, "Owner fact to do.", "example boiler")
    file(owner, "Owner note to do.")
    file(owner, "owner words to do", said="Dana", source="m-9")
    file(sealed, "Sealed note to do.")
    file(hers, "Her note to do.", owes="Riley")
    (y.memory.root / "facts" / "example-club.md").write_text(f"---\nclass: shared\n---\n- Shared fee to do. {FILED_IN} [due {when}] [m:m-1]\n")
    due.Builder(y.memory).refresh(y.memory.scope.files())

    def got(room: str) -> set[str]:
        return {w for w in ("Owner fact", "Owner note", "owner words", "Sealed note", "Her note", "Shared fee") if w in y.due_pack(room)}
    assert got(owner) == {"Owner fact", "Owner note", "owner words", "Sealed note", "Shared fee"}    # a person's room's own line: not unasked
    asked = {x["text"] for x in due.listed(y.memory, y.owed.load(), y.reader(owner))}         # asked, an owner room reads them all
    assert "Her note to do." in asked and "more open" not in y.due_pack(owner)               # and it is not counted as left out
    assert got(hers) == {"Her note", "Shared fee"} and "waiting on Riley" in y.due_pack(hers)
    assert got(sealed) == {"Sealed note"}
    assert y.due_pack("no-such-room") and got("no-such-room") == {"Shared fee"}              # no room: shared files only, as recall
    club = y.memory.root / "facts" / "example-club.md"                                       # its class as it is now, no rebuild
    club.write_text(club.read_text().replace("class: shared", "class: room"))
    assert got(hers) == {"Her note"}


def test_a_line_marked_do_not_surface_is_in_no_part_and_in_the_asked_list_and_the_two_word_form_is_only_words(tmp_path):
    """The part is shown unasked, so a dated line carrying mention recall's do-not-surface mark stays out of it (due.unasked), as it
    stays out of mention recall; `yerk due` and `recall`, which are asked, still hold it. "Ask only" as two words is what a thing's
    own words can say, and hides nothing."""
    y = install(tmp_path, sealed=True, family=True)
    owner = "example-room"
    y.add_room(owner, ConsoleChannel())
    when = (datetime.now(y.memory.tz).date() + timedelta(days=2)).isoformat()
    for n, text in enumerate(("\u26d4 Example renewal, never raised.", "Example deposit, ask-only.", "Ask only Dana about the example fee.", "Example form.")):
        y.rooms[owner].inflight = [Inbound(f"m-{n}", "owner", "a placeholder message")]
        assert asyncio.run(y.tool_remember(owner, text, "", [], due=when))["ok"]
    due.Builder(y.memory).refresh(y.memory.scope.files())
    part = y.due_pack(owner)
    assert "Example form." in part and "Ask only Dana" in part
    assert "Example renewal" not in part and "Example deposit" not in part and "more open" not in part
    asked = {x["text"] for x in due.listed(y.memory, y.owed.load(), y.reader(owner))}
    assert {"\u26d4 Example renewal, never raised.", "Example deposit, ask-only."} <= asked
