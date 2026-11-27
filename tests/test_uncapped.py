"""Checks the `uncapped` switch in yerkd.toml's [limits]: off (absent or false), what a room is sent is unchanged, byte for
byte; on, every character budget in budgets.toml reads as unlimited for what is sent, the numbers kept as measurements, and
Claude Code's 10,000-character hook ceiling the one cut left."""
import asyncio
import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from fake_channel import ConsoleChannel

from yerk_ai import cli, context, files, hooks, memory, morning, shape, status, taste, transcribe, voice_note, words, workers
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, uncapped
from yerk_ai.daemon import Yerkd
from yerk_ai.ledger import Ledger

FIXTURE = Path(__file__).parent / "fixtures" / "uncapped-capped-assembly.json"
NOW = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)
ROOM = "example-room"
PAD = 220
CODE = Path("/srv/yerk-ai")      # the checkout that runs, as the mechanics name it: one path wherever the test runs
PROMPT = "how do we do the deploy card for the northwind launch tomorrow?"
TOPICS = ["# Topic rules: each loads on a message that names its topic, in the rooms its [rooms: ...] names, else in every room", "",
          "- deploy card: the card names the commit, the tests and the restart, one line each (example-room, 2026-10-20 10:00:00+00:00) [m:t1]",
          "- northwind launch: say the launch date as the owner filed it, never a guess (example-room, 2026-10-21 11:00:00+00:00) [m:t2]",
          "- quiet hours: no post between 23:00 and 07:00 [rooms: example-quiet] (example-quiet, 2026-10-22 12:00:00+00:00) [m:t3]"]


def layer(tmp: Path, yerkd: str | None = None) -> tuple[Path, Path]:
    """The example layer with a persona past its line, a rules file, three topic rules (two every room's, one another
    room's) and a taste file whose rows run long; `yerkd`, the private yerkd.toml's text, when there is one."""
    private, state = tmp / "private", tmp / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir(parents=True)
    (private / "persona.md").write_text((private / "persona.md").read_text() + "".join(
        f"\n## Part {i}\n" + "".join(f"- persona line {i}.{j}: the way this room answers, said at some length.\n" for j in range(12))
        for i in range(24)))
    (private / "memory" / memory.RULES).write_text("---\nclass: shared\n---\n# Rules: one live line per job, in every room\n\n" + "".join(
        f"- job {i}: how job {i} is done in every room (example-room, 2026-10-1{i % 10} 09:00:00+00:00) [m:r{i}]\n" for i in range(20)))
    (private / "memory" / memory.TOPIC_RULES).write_text("\n".join(TOPICS) + "\n")
    rows = [{"ts": f"2026-10-2{i}T10:00:00", "channel": "c", "verdict": "pos" if i % 3 else "neg", "reply_id": f"m{i}", "reply_at": "10:00",
             "signal": f"that was actually good, keep doing it exactly like that every single time {i} " * 2,
             "reply": f"reply {i}: " + "a long reply the room gave, with every clause the owner rated. " * 9, "room": ROOM} for i in range(8)]
    (private / "taste.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    if yerkd is not None:
        (private / "yerkd.toml").write_text(yerkd)
    return private, state


def recall(room: str, prompt: str) -> tuple[list, list, list, list]:
    """A recall part past its 2,000: two names, a past search's hits and the room's lines on the topic."""
    names = [{"key": "Northwind", "named": "Northwind", "text": f"Northwind: line {i} on file about Northwind, " + "said plainly. " * 12,
              "cite": f"memory/facts/northwind.md:{i}"} for i in range(1, 6)]
    past = [{"text": f"hit {i}: " + "what was decided about the launch. " * 8, "cite": f"memory/daily/2026-10-1{i}.md:4"} for i in range(4)]
    topics = [{"day": "2026-10-20", "time": f"1{i}:00", "role": "owner", "text": "the deploy card again, " * 20,
               "cite": f"memory/daily/2026-10-20.md:{i}"} for i in range(4)]
    return names, topics, past, ["the launch date"]


def assemble(tmp: Path, yerkd: str | None = None) -> dict:
    """A process start, a message after the rules file grew, and a compaction, as the daemon assembles each: the start file,
    the hook texts, every row assembled.jsonl got (its time out), the topic rules told and every log line; the layer's
    folder written TMP, at one length wherever the test runs (PAD): a part's size counts the path in its `[from` line, and
    the mechanics' the checkout's (CODE)."""
    tmp = tmp.resolve() / ("p" * (PAD - len(str(tmp.resolve())) - 1))
    private, state = layer(tmp, yerkd)
    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: seen.append(f"{r.name} {r.levelname} {r.getMessage()}")    # type: ignore[method-assign]
    logging.getLogger().addHandler(handler)
    repo, context.REPO = context.REPO, CODE
    try:
        y = Yerkd(private, state)
        y.spec_for(ROOM)
        y.sessions.bind(ROOM, "s1", "one")              # as RoomProcess.start binds it: the compaction below is the room's own
        rules = private / "memory" / memory.RULES
        rules.write_text(rules.read_text() + "".join(f"- new job {i}: how new job {i} is done from today, said in full.\n" for i in range(40)))
        msg = hooks.user_prompt_submit({"session_id": "s1", "prompt": PROMPT}, state, y.room_confs,
                                       lambda r, p: ("owner", NOW.timestamp() - 300), y.budgets, NOW, recall, ROOM,
                                       y.rules_news, y.taste_for, y.place_for, y.rules_topic)
        again = hooks.user_prompt_submit({"session_id": "s1", "prompt": PROMPT}, state, y.room_confs,
                                         lambda r, p: ("owner", NOW.timestamp() - 60), y.budgets, NOW, recall, ROOM,
                                         y.rules_news, y.taste_for, y.place_for, y.rules_topic)
        compact = asyncio.run(y._hook("SessionStart", ROOM, {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1"}))
    finally:
        logging.getLogger().removeHandler(handler)
        context.REPO = repo
    rdir = state / "rooms" / ROOM
    rows = [json.loads(x) for x in (rdir / "assembled.jsonl").read_text().splitlines()]
    for r in rows:
        r.pop("t")
        r.pop("recall_ms", None)
    told = rdir / context.TOLD
    out = {"start": (rdir / "context.md").read_text(), "message": msg, "again": again, "compact": compact, "rows": rows,
           "told": told.read_text() if told.is_file() else None, "seen": (rdir / "rules-seen.json").read_text(),
           "logs": [x for x in seen if x.startswith("yerk")]}
    return json.loads(json.dumps(out).replace(str(tmp), "TMP"))


ON = "[limits]\nuncapped = true\n"


def padded(tmp: Path) -> Path:
    return tmp.resolve() / ("p" * (PAD - len(str(tmp.resolve())) - 1))


def test_capped_is_mains_assembly_byte_for_byte_with_the_switch_absent_or_false(tmp_path):
    """The fixture is this scenario assembled on the base commit (1ec2592, PR 145's merge; retaken on note-to-self, on
    retention-keep, on need-2026-10-31-27, on need-2026-10-25-21, on need-2026-10-24-22, on fold-named-2026-11-04 and on
    rule-filing-check-2026-11-06, on zoom-2026-11-09, on memory-docs-shape-2026-11-09, on fable-5-2, on need-2026-11-18-110, on
    need-2026-11-19-123 and on need-2026-11-21-134, each
    one's mechanics line its only change, need-2026-10-25-21's with the checkout's path, CODE; and on need-2026-10-28-25, its only change the rules
    note past its size naming the room's work folder file, context.CHANGED; and on persona-voice-news-2026-11-09, its only
    change the room's look on file, rules-seen.json, holding the persona and voice texts; and on need-2026-11-19-124, its
    mechanics line and the recall part's 2,200, which holds the room's own line on the topic whole in both messages; and on
    start file, both messages,
    the compaction, every assembled.jsonl row, the topic rules told and every log line, the same with [limits] absent and
    false."""
    want = json.loads(FIXTURE.read_text())
    assert assemble(tmp_path / "absent") == want
    assert assemble(tmp_path / "false", "[limits]\nuncapped = false\n") == want


def test_uncapped_session_start_sends_every_part_whole_and_the_rooms_topic_rules_after_the_rules_file(tmp_path):
    got = assemble(tmp_path, ON)
    start, row = got["start"], got["rows"][0]
    assert "[...trimmed" not in start and "persona line 23.11" in start and row["parts"]["persona"] > 11800
    assert TOPICS[2] in start and TOPICS[3] in start and TOPICS[4] not in start       # every room's two; another room's, never
    assert start.index("- job 19:") < start.index(context.TOPICAL) < start.index(TOPICS[2]) < start.index("[from TMP/private/memory/open-loops.md]")
    assert got["logs"] == []                                                         # nothing cut, nothing said


def test_uncapped_message_sends_each_part_whole_and_cuts_only_at_claude_codes_hook_ceiling(tmp_path):
    got = assemble(tmp_path, ON)
    sent = got["message"]["hookSpecificOutput"]["additionalContext"]
    whole = (padded(tmp_path) / "state" / "rooms" / ROOM / "work" / hooks.WHOLE).read_text()
    assert sent.endswith(f"hook limit were cut; the whole is at TMP/state/rooms/{ROOM}/work/{hooks.WHOLE}]")
    assert sent.splitlines()[-1].startswith("[uncapped: ") and whole.startswith("\n".join(sent.splitlines()[:-1]))
    real = got["rows"][1]["chars"]                                                   # as sent, its path whole
    assert real <= 10000 - context.APPROVAL and got["rows"][1]["text"] == sent
    assert all(f"new job {i}:" in whole for i in range(40)) and "lines new or replaced" not in whole      # the news, every line
    assert "line 1 on file about Northwind" in whole and all(f"hit {i}:" in whole for i in range(4))   # beside a past search: a card's first line
    assert all(f"(memory/daily/2026-10-20.md:{i})" in whole for i in range(4))     # the room's lines on the topic, every one
    assert context.TOPICAL not in whole                                              # both its topic rules rode at the start
    rows = [x for x in whole.splitlines() if x.startswith(("👍", "👎"))]
    assert len(rows) == 5 and "…" not in "\n".join(rows) and all(x.count("every clause the owner rated") == 9 for x in rows)
    again = got["again"]["hookSpecificOutput"]["additionalContext"]                 # no news: under the ceiling, nothing cut
    assert "[uncapped: " not in again and context.chars(again) == got["rows"][2]["chars"]


def test_a_topic_rule_filed_after_the_start_rides_once_and_a_compaction_re_adds_it(tmp_path):
    private, state = layer(tmp_path, ON)
    y = Yerkd(private, state)
    y.spec_for(ROOM)
    y.sessions.bind(ROOM, "s1", "one")                  # as RoomProcess.start binds it: the compaction below is the room's own
    tier, late = private / "memory" / memory.TOPIC_RULES, ("- launch checklist: read the checklist aloud before the launch call "
                                                           "(example-room, 2026-10-26 18:00:00+00:00) [m:t4]")
    tier.write_text(tier.read_text() + late + "\n")

    def say(prompt: str) -> str:
        out = hooks.user_prompt_submit({"session_id": "s1", "prompt": prompt}, state, y.room_confs, lambda r, p: ("owner", None),
                                       y.budgets, NOW, None, ROOM, y.rules_news, None, None, y.rules_topic)
        return out["hookSpecificOutput"]["additionalContext"]
    first = say("is the launch checklist for the deploy card ready?")
    assert late in first and TOPICS[2] not in first and TOPICS[3] not in first     # the start's own never ride again
    assert late not in say("and the launch checklist after that?")                   # once a session
    back = asyncio.run(y._hook("SessionStart", ROOM, {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1"}))
    text = back["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("[from ") and f"\n\n{context.TOPICAL}\n{late}" in text and TOPICS[2] not in text
    assert late not in say("the launch checklist once more")                         # re-added at the compaction: told


def test_the_ceiling_cuts_at_a_line_names_the_file_and_leaves_room_for_the_approval_line(tmp_path):
    text = "\n".join(f"line {i}: " + "x" * 90 for i in range(300))                   # 30,290
    path = tmp_path / "work" / "message-whole.md"
    out = context.ceiling(text, 10000, path, context.APPROVAL)
    *kept, last = out.split("\n")
    assert context.chars(out) <= 10000 - context.APPROVAL and text.startswith("\n".join(kept) + "\n")
    assert last == f"[uncapped: {context.chars(text) - context.chars(chr(10).join(kept)):,} characters past Claude Code's 10,000 hook limit were cut; the whole is at {path}]"
    assert path.read_text() == text + "\n" and oct(path.stat().st_mode & 0o777) == "0o600"
    assert context.ceiling("short", 10000, tmp_path / "none.md", context.APPROVAL) == "short" and not (tmp_path / "none.md").exists()


def test_a_rule_filed_in_an_uncapped_install_keeps_its_examples_and_never_goes_to_the_topic_file(tmp_path):
    """memory.rule's cap of 0, the daemon's when uncapped: no example stripped, no filing to rules-topics.md for size; capped,
    the same filing past the line goes to the topic file."""
    for cap, topic in ((0, False), (16000, True)):
        private, state = layer(tmp_path / str(cap))
        s = memory.settings(private, state)
        rules = s.root / memory.RULES
        rules.write_text(rules.read_text() + "".join(f"- big {i}: done so <example>like this, at length</example>{'.' * 200}\n" for i in range(80)))
        r = memory.rule(s, ROOM, "m1", "new job", "how it is done, since it works <example>this way</example>", cap)
        assert r["ok"] and bool(r.get("topic")) is topic and "stripped" not in r
        assert ("- new job: how it is done, since it works <example>this way</example> (" in rules.read_text()) is not topic


def test_taste_rows_go_whole_and_the_counts_stay(tmp_path):
    private, _ = layer(tmp_path)
    rows = taste.load(private / taste.FILE)
    capped, whole = taste.render(rows, ROOM, 1500), taste.render(rows, ROOM, 1500, whole=True)
    assert context.chars(capped) <= 1500 and "…" in capped
    lines = whole.splitlines()[1:]
    assert len(lines) == 5 and sum(x.startswith("👍") for x in lines) == 3 and "…" not in whole and context.chars(whole) > 3000


def test_an_uncapped_relay_hands_the_room_the_whole_report_and_asks_no_length(tmp_path):
    lifted, kept = workers.settings(tmp_path, {}, budgets({"limits": {"uncapped": True}})), workers.settings(tmp_path, {}, budgets())
    assert (lifted.relay_message, lifted.relay_reply) == (0, 0) and (kept.relay_message, kept.relay_reply) == (1500, 1500)
    (report := tmp_path / "abc123" / "REPORT.md").parent.mkdir()
    report.write_text("# Report\n" + "".join(f"- finding {i}: what was found, said in full.\n" for i in range(200)))
    row = {"id": "abc123", "cap": 900.0, "started": time.time() - 185, "account": "one", "model": "m", "room": ROOM,
           "report": str(report), "tasks": str(tmp_path / "TASKS.md")}
    end = {"exit": "done", "finished": row["started"] + 181, "result": "done", "has_report": True, "result_code": "success",
           "detail": "success, exited 0"}
    whole, part = workers.relay_text(row, end, 0), workers.relay_text(row, end, 1500)
    assert "the report, all of it:" in whole and "- finding 199:" in whole and "under " not in whole and "own voice, ending with" in whole
    assert len(part) <= 1500 and "the report begins:" in part and "own voice, under 1,500 characters, ending with" in part


def test_an_uncapped_file_keeps_every_line_however_long_the_read(tmp_path, monkeypatch):
    lines = [f"row {i}: " + "c" * 990 for i in range(300)]                          # 300,000: past [files] chars
    monkeypatch.setattr(files, "workbook", lambda path: ("1 sheet", iter(lines)))
    (book := tmp_path / "book.xlsx").write_bytes(b"")
    whole = files.write(ROOM, book, timezone.utc, whole=True).read_text()
    cut = files.write(ROOM, book, timezone.utc).read_text()
    assert "row 299:" in whole and "[cut at " not in whole and "[stopped after" not in whole
    assert "row 299:" not in cut and "[cut at 200,000 characters: " in cut


def test_the_map_says_uncapped_where_it_would_say_over_and_passes(tmp_path, capsys):
    private, state = layer(tmp_path / "capped")
    assert cli.main(["map", ROOM, "--private", str(private), "--state", str(state)]) == 1
    out = capsys.readouterr().out
    assert re.search(r"^  persona +[\d,]+ +11,800  OVER, sent trimmed to ", out, re.M) and "OVER BUDGET: persona:" in out
    private, state = layer(tmp_path / "on", ON)
    assert cli.main(["map", ROOM, "--private", str(private), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "This install is uncapped ([limits] uncapped in yerkd.toml)" in out and "OVER" not in out
    assert re.search(r"^  persona +2\d,\d{3} +11,800  uncapped  \[", out, re.M) and "uncapped, past its line: persona: " in out
    assert "each rides whole in the session start's `rules` part" in out


def test_the_switch_leaves_the_compaction_window_and_idle_compaction_alone(tmp_path):
    private, state = layer(tmp_path, ON)
    y = Yerkd(private, state)
    assert uncapped(y.budgets) and y.budgets["session"] == budgets()["session"] and not uncapped(budgets())
    assert y.spec_for(ROOM).settings["autoCompactWindow"] == budgets()["session"]["compact_window"]


def test_an_uncapped_install_lifts_the_money_gates_and_transcribes_result_carries_its_cost(tmp_path, monkeypatch):
    """[limits] uncapped (the owner's call: track every spend, or say what each thing costs): no paid call
    waits for owner_yes, every call still books its spend row, and transcribe's result says what it cost, as voice_note's does."""
    asked: list[bool] = []

    def heard(audio, openai_key, groq_key, book, owner_yes=False, meta_key=None):
        asked.append(owner_yes)
        book("openai/whisper-1", 0.25, "chunk 1 of 2")
        book("openai/whisper-1", 0.125, "chunk 2 of 2")
        return {"ok": True, "file": "memo.transcript.txt", "text": "hi", "segments": 1, "uncertain": 0}

    def spoke(script, name, folder, private, key, book, owner_yes=False):
        asked.append(owner_yes)
        book("fish/s2.1-pro-free", 0.5, "section 1 of 1")
        return {"ok": True, "engine": "fish/s2.1-pro-free", "duration_s": 3.0, "usd": 0.5}
    monkeypatch.setattr(transcribe, "run", heard)
    monkeypatch.setattr(voice_note, "run", spoke)
    for name, yerkd in (("capped", None), ("on", ON)):
        private, state = layer(tmp_path / name, yerkd)
        y = Yerkd(private, state)
        y.add_room(ROOM, ConsoleChannel())
        (inbox := state / "rooms" / ROOM / "work" / "inbox" / "5").mkdir(parents=True)
        (inbox / "memo.ogg").write_bytes(b"OggS")
        r = asyncio.run(y.tool_transcribe(ROOM, str(inbox / "memo.ogg")))
        assert r == {"ok": True, "file": "memo.transcript.txt", "text": "hi", "segments": 1, "uncertain": 0, **({"usd": 0.375} if yerkd else {})}
        assert asyncio.run(y.tool_voice_note(ROOM, "a short script"))["usd"] == 0.5
        assert asked[-2:] == [bool(yerkd)] * 2                                       # capped: the owner's yes as the call gave it
        assert y.ledger.spent(0, time.time() + 1) == {"n": 3, "usd": 0.875}           # the spend rows, unchanged either way


def test_an_uncapped_start_has_the_room_say_a_paid_calls_cost_and_asks_no_relay_length(tmp_path):
    capped, lifted = context.mechanics(), context.mechanics(True)
    assert capped == context.MECHANICS.read_text().strip().replace(context.CODE, str(context.REPO)) and ", under 1,500 characters," in capped
    assert lifted == capped.replace(", under 1,500 characters,", ",") + "\n" + context.PAID
    assert "only when the owner asks for a voice note or to say it out loud" in lifted and shape.offenses(context.PAID) == []
    start = assemble(tmp_path, ON)["start"]
    assert context.PAID in start and "under 1,500 characters" not in start


def test_an_uncapped_status_and_morning_diff_carry_the_days_and_the_weeks_spend(tmp_path):
    now = NOW.timestamp()
    for name, yerkd in (("capped", None), ("on", ON)):
        private, state = layer(tmp_path / name, yerkd)
        led, accounts, cool = Ledger(state / "ledger.sqlite"), load_accounts(private / "accounts.toml"), Cooldowns(state / "cooldowns.json")
        for at, usd in ((now - 60, 0.25), (now - 120, 0.125), (status.midnight(now) - 60, 0.5), (status.midnight(now, 3), 1.0),
                        (status.midnight(now, 10), 5.0)):
            led.spend(t=at, room=ROOM, tool="transcribe", engine="openai/whisper-1", usd=usd, note="")
        s = status.collect(private, state, accounts, cool, led, now=now)
        card = status.card({**s, "alive": False, "commit": "x"}, now)
        lines, _ = morning.build(private, state, accounts, cool, led, None, now)
        if yerkd is None:
            assert "spend" not in s and not any("week US$" in x for x in card + lines)
            assert "- spent yesterday: US$0.50 on 1 calls" in lines
            continue
        assert s["spend"] == {"n": 2, "usd": 0.375, "week": 1.875}
        assert "- spend today US$0.38 · week US$1.88" in card and "- spend yesterday US$0.50 · week US$1.50" in lines
        assert not any(x.startswith("- spent yesterday") for x in lines) and all(words.width(x) <= words.PHONE for x in card + lines)
        assert words.SPEND.format(day=words.TODAY, n=1, s="", usd=0.0, week=0.0) == "spend today US$0.00 (1 call) · week US$0.00"
