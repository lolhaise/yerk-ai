"""Checks that every part a room is handed at its session start, such as the recall pack (the notes filed for that room), the open loops
and what is due, fits under its size in budgets.toml by construction, cut by whole lines or items and never mid-line. The case behind it
(seen live): one room's recall pack came out at 6,003 against its 6,000, since the pack kept 60 characters for the line counting
what it leaves out, which is 64 with its newline once that count has three digits, and measured its items in code points where the key
the oldest first, its head and that line measured with it; the open loops keep their room; every part's fit keeps whole
lines with its marker counted; and the machine room hears of a real drop only, in one line. Every value here is a
placeholder."""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from test_recall import NOW, day, tree
from test_sheet_hard import machine_said

from yerk_ai import context, hooks, memory, taste, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel

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
