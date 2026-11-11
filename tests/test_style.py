"""Checks that every card and notice follows STYLE.md, the rules for how they read. Every surface a room reads is
rendered here and held to the rules (state first, no path, no exit code, no raw timestamp, no em dash, phone width),
words.py is the only place a state mark or a state phrase is spelled, the manifest says built or described on every
among the described, and a room that asks "can you do voice" gets the manifest's answer."""
import ast
import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import discord
from starlette.testclient import TestClient

from yerk_ai import abilities, addons, cli, mcp_server, words, workers
from yerk_ai.card import Card
from yerk_ai.channels import discord as dmod
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import NO_KEY_LINE, Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.engine import SessionTable
from yerk_ai.room import fit

DOTS = (words.DONE, words.WORKING, words.NOT_DONE, words.NOTHING)

PKG = Path(__file__).resolve().parents[1] / "yerk_ai"
HOME = "/home/someone/.local/state/yerk/workers/abc123"          # a path that must never reach a room
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
DEV = ("/home/", "exited", "Result=", "SERVICE_RESULT", "systemd", "Traceback", "Errno", "<t:", "\u2014")


def room_safe(text: str) -> list[str]:
    """What breaks a room rule in `text`: a path, an exit or systemd word, a raw timestamp, an em dash."""
    bad = [w for w in DEV if w in text]
    return bad + (["an ISO time"] if ISO.search(text) else [])


def state_first(text: str) -> bool:
    return text.split("\n")[0][:1] in "".join(DOTS)


def se(event):
    return {"type": "stream_event", "event": event}


def tool(i, tid, name, args):
    return [se({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": tid, "name": name,
                                                                             "input": {}}}),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": args}]}}]


def done(tid, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": error}]}}


def a_turn_with_three_tools() -> Card:
    c = Card(thinking=True)
    c.feed(se({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
    c.feed(se({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta",
                                                                    "text": "Checking the three places the owner named, "
                                                                            "starting with the notes from last week"}}))
    for ev in (tool(1, "a", "Read", {"file_path": f"{HOME}/memory/pinned.md"})
               + tool(2, "b", "Bash", {"command": f"grep -rn 'boiler' {HOME}/memory/daily | head -50 && echo ok"})
               + tool(3, "c", "WebFetch", {"url": "https://docs.example.org/a/very/long/path?b=1"})):
        c.feed(ev)
    c.feed(se({"type": "content_block_start", "index": 4, "content_block": {"type": "thinking", "thinking": ""}}))
    c.feed(se({"type": "content_block_delta", "index": 4, "delta": {"type": "thinking_delta",
                                                                    "thinking": "The owner wants the date first. " * 9}}))
    c.feed(se({"type": "content_block_stop", "index": 4}))
    c.feed(done("a"))
    c.feed(done("b", error=True))
    return c


NOON = datetime(2026, 1, 7, 12, 0).timestamp()      # a Wednesday noon on the box's own clock


def worker_row(cap=900.0, started=None):
    return {"id": "abc123", "cap": cap, "started": started or time.time() - 185, "account": "one", "model": "a-model",
            "report": f"{HOME}/REPORT.md", "tasks": f"{HOME}/TASKS.md", "room": "r"}


ENDS = {"done": "success", "died": "exit-code", "timed out": "timeout", "stopped": "success", "lost": "",
        "did not start": ""}


def settled(state, row, has_report=True):
    return {"exit": state, "finished": row["started"] + 181, "result": "Three ticks done, one a minute",
            "has_report": has_report, "result_code": ENDS[state], "detail": "exit-code, exited 3"}


# 1. every surface, rendered, against the room rules
def test_the_turn_card_is_state_first_phone_width_and_names_no_path():
    c = a_turn_with_three_tools()
    for text in (c.render(), c.owed(words.why("auth", "error"), f"read {HOME}/notes.md and tell me the date"),
                 f"{words.WORKING} {words.BACKUP[0]}\n{words.SUB}{words.BACKUP[1]}"):
        assert state_first(text) and not room_safe(text), (text, room_safe(text))
        assert all(words.width(x) <= words.PHONE for x in text.splitlines()), text
    lines = c.render().splitlines()
    assert lines[0].startswith(f"{words.WORKING} Checking the three places") and lines[0].endswith("...")
    assert lines[1] == "-# thinking: The owner wants the date" and lines[2].startswith("-# first. The owner")
    assert [x.split(" ", 2)[:2] for x in lines[-3:]] == [[words.DONE, "reading"], [words.NOT_DONE, "running"],
                                                          [words.WORKING, "reading"]]
    assert lines[-1] == f"{words.WORKING} reading docs.example.org" and "grep" not in c.render()   # never the command


def test_every_worker_card_running_and_settled_is_state_first_with_no_path_and_no_exit():
    row = worker_row()
    items = [(True, "first tick"), (False, "second tick"), (False, "third tick")]
    cards = [workers.card_text(row, items), workers.card_text(row, [])]
    cards += [workers.card_text(row, items, settled(st, row)) for st in ENDS]
    cards += [workers.card_text(row, [(True, x) for _, x in items], settled("done", row)),
              workers.card_text(row, items, settled("done", row, has_report=False))]
    quiet = workers.card_text(row, items, {**settled("done", row), "result": words.NO_REPLY, "quiet": True})
    for text in [*cards, quiet]:
        assert state_first(text) and not room_safe(text), (text, room_safe(text))
        assert all(words.width(x) <= words.PHONE for x in text.splitlines()), text
        assert "account" not in text and "a-model" not in text            # one account here: no account line
    heads = [t.splitlines()[0] for t in cards]
    assert heads == [f"{words.WORKING} worker running, 1 of 3 done", f"{words.WORKING} worker running",
                     f"{words.WORKING} worker finished, 1 of 3 ticked", f"{words.NOT_DONE} worker died: it ended with an error",
                     f"{words.NOT_DONE} worker stopped: timed out at 15 min", f"{words.NOT_DONE} worker stopped by the owner",
                     f"{words.NOT_DONE} worker lost: it ended with no record", f"{words.NOT_DONE} worker did not start",
                     f"{words.DONE} worker finished clean", f"{words.WORKING} worker finished, no report"]
    assert cards[0].splitlines()[-1] == "-# worker abc123, 3 min of 15"
    assert cards[-2].splitlines()[-2:] == ["-# done in 3 min", "-# report saved: `yerk worker report abc123`"]
    assert quiet.splitlines() == [f"{words.DONE} worker {words.QUIET_WORKER}", *cards[-2].splitlines()[-2:]]   # no token, no rows
    assert "-# no report: `yerk worker report abc123`" in cards[-1]
    assert cards[7].splitlines()[-1] == "-# no report: `yerk worker report abc123`" or "ran" not in cards[7]
    assert " ran " not in cards[7] and "of 15" not in cards[7] and "-# \n" not in cards[7] + "\n"   # never ran


def test_the_relay_line_and_reply_carry_the_command_never_the_path(tmp_path):
    (tmp_path / "abc123").mkdir()
    report = tmp_path / "abc123" / "REPORT.md"                        # a worker's folder is its id
    report.write_text("Three ticks done\n\nall of it went fine\n")
    row = {**worker_row(), "report": str(report), "tasks": str(tmp_path / "TASKS.md")}
    text = workers.relay_text(row, settled("done", row), 1500)
    assert str(report) not in text and "exited" not in text and "`yerk worker report abc123`" in text
    assert text.startswith("[worker abc123 finished] finished clean, 3 min of 15")
    reply = fit("Done: three ticks, one a minute.", 1500, [workers.relay_line(row)])
    assert not room_safe(reply) and reply.endswith("-# report saved: `yerk worker report abc123`")


def test_every_notice_is_state_first_plain_and_phone_width():
    ch = type("Ch", (), {"guild": None})()
    d = dmod.DiscordChannel.__new__(dmod.DiscordChannel)
    big = Path(__file__)
    reasons = [d.file_problem(ch, Path("/nonexistent/x.pdf")), words.file_why("type", ext=".exe"),
               words.file_why("size", size="25 MB", limit="20 MB"), words.file_why("outside"),
               dmod.refusal(discord.HTTPException(type("R", (), {"status": 413, "reason": "Payload Too Large"})(),
                                                  {"code": 40005, "message": "Request entity too large"}),
                            dmod.upload_limit(ch)),
               dmod.refusal(discord.HTTPException(type("R", (), {"status": 500, "reason": "Internal"})(), "boom"), 1)]
    assert d.file_problem(ch, big) is None and reasons[-2] == "Discord said too large (limit 20 MB)"
    notices = [f"{words.NOT_DONE} {words.NOT_SENT.format(name='report.pdf', why=r)}" for r in reasons if r]
    notices += [f"{words.NOT_DONE} {words.NOT_FETCHED.format(name='photo.png')}", f"{words.NOT_DONE} {words.NOT_RUN_AGAIN}",
                f"{words.NOT_DONE} {words.NOT_SENT_AGAIN.format(name='report.pdf', why=words.file_why('gone'))}",
                f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{words.why('quota', 'error')}", NO_KEY_LINE,
                f"{words.NOT_DONE} {words.NOT_ANSWERED}\n{words.BACK_ON.format(model='Sonnet 5.5', usual='Fable 5.1')}",
                f"{dmod.REDELIVERED}\nthe reply", f"{words.NOT_DONE} {words.HOOKS_DEAD}",
                f"{words.NOT_DONE} {words.NOT_AN_ACCOUNT.format(account='five', accounts='one, two, default')}",
                f"{words.DONE} {words.CARRIED}", f"{words.DONE} {words.RECAPPED}",
                f"{words.DONE} " + words.RULE_TOPIC.format(triggers=words.line("enterprise, example, widget or another", words.PHONE - 8),
                                                           job=words.line("send the example person to an example store for the example thing",
                                                                          words.PHONE - 2), room="example-room", cap=11886,   # the longest topic line
                                                           where=words.line(words.TOPIC_WHERE[1].format(rooms="example-room, example-quiet, family-room"))),
                f"{words.DONE} " + words.TOPICS_MOVED.format(n=3, s="s"),
                f"{words.WORKING} " + words.PART_CUT.format(part="pinned_memory", room="example-room-long", cap=14000, n=1400, s="s")]
    for text in notices:
        assert (state_first(text) or text.startswith(words.SUB)) and not room_safe(text), text
        assert all(words.width(x) <= words.PHONE for x in text.splitlines()), text
        assert not re.search(r"\b(4\d\d|5\d\d|40005)\b", text), text              # no status code
    assert NO_KEY_LINE.splitlines() == [f"{words.WORKING} meaning search needs an OpenAI key",
                                        "to add it: `yerk explain embeddings`", "meanwhile recall matches words only"]


def test_a_failed_turn_says_why_in_plain_words_for_every_class_and_outcome():
    for cls in ("auth", "quota", "transient", "overflow", "fatal", "refusal"):
        assert words.why(cls, "error") == words.WHY[cls]
    for outcome in ("timeout", "exited", "empty", "error"):
        assert words.why(None, outcome) == words.WHY[outcome]
    assert all(words.width(f"{words.NOT_DONE} {x}") <= words.PHONE for x in words.WHY.values())


def test_status_abilities_and_explain_print_state_first_at_phone_width(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("YERK_CLAUDE", str(Path(__file__).with_name("fake_claude.py")))   # the card's /usage call, never the real one
    assert cli.main(["status", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    assert cli.main(["abilities", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    shown = [x for x in lines if not x.startswith("yerk explain")]
    dashed = lambda x: state_first(x) or x.startswith(f"{words.PLAIN} ")      # noqa: E731  the card: a dash, no colour
    assert shown and all(dashed(x) and not room_safe(x) and words.width(x) <= words.PHONE for x in shown), shown
    assert f"{words.NOTHING} voice: known, not built" in lines and lines[-2].endswith(": known, not built")  # described last
    assert f"{words.WORKING} embeddings: needs OpenAI" in lines and f"{words.NOTHING} recall: runs on the free path" in lines


# 2. one vocabulary
RENDERERS = ["card.py", "workers.py", "room.py", "channels/discord.py", "daemon.py", "cli.py", "meters.py",
             "abilities.py", "status.py", "morning.py", "addons.py"]
MARKS = ("\U0001F7E2", "\U0001F7E0", "\U0001F534", "\u26AA", "LARGE GREEN CIRCLE", "LARGE ORANGE CIRCLE",
         "LARGE RED CIRCLE", "MEDIUM WHITE CIRCLE", "HOURGLASS WITH FLOWING SAND", "CROSS MARK", "WHITE SMALL SQUARE",
         "STOPWATCH", "BLACK SQUARE FOR STOP", "\u23F3", "\u274C", "\u25AB", "\u23F1", "\u23F9")


def phrases() -> list[str]:
    """Every state phrase words.py owns: its fixed part (up to any blank to fill), more than one word,
    and not the bare name of an internal state (a ledger's "did not start" is not room text)."""
    out = [t for _, t in words.OUTCOME.values()] + [t for _, t in words.ABILITY.values()] + list(words.WHY.values())
    out += [words.REPORT_SAVED, words.NO_REPORT, words.NOT_SENT, words.NOT_SENT_AGAIN, words.NOT_FETCHED,
            words.NOT_RUN_AGAIN, words.SENT_AGAIN, words.NOT_ANSWERED, words.GONE, words.STRAY, *words.BACKUP,
            words.MOVED, words.MOVED_ON, words.OUT_OF, words.COMPACTING, *words.FILE_WHY.values(), words.NEXT_REPLY, words.OWNER_ONLY,
            words.NOT_LISTED, words.NOT_A_LEVEL, words.BACK_ON, words.ON, words.NOT_AN_ACCOUNT, words.CARRIED, words.RECAPPED,
            words.NONE_RUNNING, words.NOT_RUNNING, words.NOT_ANSWERING, words.NO_JOB_DUE, words.NO_TIMERS, words.NO_BACKUP]
    out += [v for k, v in words.STEP.items() if k != "hold"]            # a worker's card says "waiting for an account" itself
    out += [*words.WORKER_STEP.values(), *words.JOB_STEP.values(), words.MESSAGE_TO, words.NOTES, *words.FILES.values(),
            words.ANOTHER_WAY, words.ROOM_NOW]
    fixed = (re.split(r"[{\n]", x)[0].strip(" :,;") for x in out)
    return sorted({p for p in fixed if " " in p and len(p) >= 8 and p not in words.OUTCOME})


def literals(source: str) -> list[str]:
    """The string literals in code that can reach a reader: no docstring, nothing passed to a logger."""
    tree, skip = ast.parse(source), set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            skip.add(id(body[0].value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id in ("log", "logging"):
            skip |= {id(n) for a in node.args for n in ast.walk(a)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in skip]


def spelled(source: str) -> list[str]:
    found = literals(source)
    return [f"mark {m!r}" for m in MARKS if m in source] + [f"phrase {p!r}" for p in phrases() if any(p in x for x in found)]


def test_the_vocabulary_is_the_only_place_a_state_mark_or_phrase_is_spelled():
    hits = [f"{rel}: {h}" for rel in RENDERERS for h in spelled((PKG / rel).read_text())]
    assert not hits, "\n".join(hits)
    assert len(phrases()) > 30 and DOTS == ("\U0001F7E2", "\U0001F7E0", "\U0001F534", "\u26AA")


def test_the_vocabulary_grep_catches_a_renderer_that_spells_its_own():
    assert spelled('head = f"{x} worker finished clean"\n') == ["phrase 'finished clean'"]
    assert spelled('mark = "\\N{LARGE GREEN CIRCLE}"\n') == ["mark 'LARGE GREEN CIRCLE'"]
    assert spelled('def f():\n    """finished clean, in a docstring"""\n    log.info("finished clean")\n') == []


def test_an_add_ons_words_file_is_held_to_the_same_rules():
    """addons-1: an add-on's own words file (the example's, and each one installed under $YERK_STATE, which `yerk deploy`
    sets) spells no state mark or phrase of words.py's, and none of its lines breaks a room rule."""
    state = Path(os.environ["YERK_STATE"]) if os.environ.get("YERK_STATE") else None
    files = [PKG.parent / "examples" / "addons" / "hello" / "yerk_hello" / "words.py"]
    files += [state / addons.DIR / n / ab["install"]["words"] for n, ab in addons.installed(state).items()] if state else []
    for f in files:
        src = f.read_text()
        assert not spelled(src) and not [x for x in literals(src) if room_safe(x)], f


# 3. the manifest: built or described on every entry, voice among the described (tests/test_abilities.py has the rest)
def test_every_ability_says_built_or_described_and_voice_is_described():
    tab = abilities.manifest()
    assert all(ab.get("state") in ("built", "described") for ab in tab.values()), {k: v.get("state") for k, v in tab.items()}
    assert "voice" in [k for k, ab in tab.items() if ab["state"] == "described"]
    v = tab["voice"]
    assert v["tier"] == "paid" and len(v["how"]) >= 5 and v["why_not"].startswith("The owner's call")
    assert v["needs"] and v["price"] and v["free_path"].startswith("none") and "voice" in v["asked_as"]
    joined = " ".join(v["how"]).lower()
    assert all(x in joined for x in ("barge-in", "realtime speech vendor as a tool", "claude code supplies the words"))
    assert not room_safe(json.dumps(v, ensure_ascii=False))


def test_yerk_explain_voice_answers_from_the_manifest(tmp_path, capsys):
    assert cli.main(["explain", "voice", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == f"{words.NOTHING} voice: known, not built" and lines[1].startswith("Voice rooms:")
    at = lines.index("How it would be built:")
    assert all(x.startswith("- ") for x in lines[at + 1:at + 8]) and lines[at + 8] == ""
    for label in ("Needs: ", "Cost: ", "Free path: none", "Why it is not built: The owner's call"):
        assert any(x.startswith(label) for x in lines), label
    assert not room_safe(out)


def test_a_room_asking_can_you_do_voice_gets_the_manifest_answer_before_the_first_word(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-v", "one")
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    with TestClient(app) as c:
        asked = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-v",
                                      "prompt": "can you do voice?"}).json()
        other = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-v",
                                      "prompt": "what is on the list today?"}).json()
    ctx = asked["hookSpecificOutput"]["additionalContext"]
    assert "- voice, a Yerk AI ability (known, not built): Voice rooms:" in ctx and "(yerk explain voice)" in ctx
    assert "Why it is not built: The owner's call" in ctx and "Barge-in from the user's transcript" in ctx
    [entry] = abilities.asked("can you do voice?", y.memory)
    assert len(entry["text"]) + 40 < y.budgets["per_message"]["on_hit"]["recall"]    # whole, under the recall cap
    assert "Yerk AI ability" not in other["hookSpecificOutput"]["additionalContext"]


# 4. the presentation settings (decision 27): named, described from the code, honored
def test_yerk_config_get_presentation_prints_each_setting_with_its_plain_words(tmp_path, capsys):
    assert cli.main(["config", "get", "presentation", "--private", str(EXAMPLE_PRIVATE)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [x.split(" = ")[0] for x in lines if " = " in x] == ["presentation.footer", "presentation.paths",
                                                               "presentation.times"]
    assert lines[0] == "presentation.footer = minimal (the default)" and lines[2].startswith("  choices: minimal, full;")
    assert cli.main(["config", "get", "presentation.times", "--json", "--private", str(EXAMPLE_PRIVATE)]) == 0
    [r] = json.loads(capsys.readouterr().out)
    assert (r["value"], r["source"], r["choices"]) == ("relative", "default", ["relative", "clock"]) and r["what"]
    assert cli.main(["config", "get", "nope", "--private", str(EXAMPLE_PRIVATE)]) == 2
    priv = tmp_path / "priv"
    priv.mkdir()
    (priv / "yerkd.toml").write_text('[presentation]\nfooter = "full"\ntimes = "sometimes"\n')
    assert cli.main(["config", "get", "--json", "--private", str(priv)]) == 0
    got = {r["key"]: (r["value"], r["source"]) for r in json.loads(capsys.readouterr().out)}
    assert {k: v for k, v in got.items() if k.startswith("presentation.")} == {
        "presentation.footer": ("full", "yerkd.toml"), "presentation.paths": ("never", "default"),
        "presentation.times": ("relative", "yerkd.toml")}                     # an unknown value: the default
    from yerk_ai import memory
    assert {k for k in got if k.startswith("memory.")} == {f"memory.{k}" for k in (*memory.EMBEDDING_SETTINGS, *memory.RECALL_SETTINGS)}
    assert cli.main(["config", "get", "memory", "--private", str(priv)]) == 0
    assert [x.split(" = ")[0] for x in capsys.readouterr().out.splitlines() if " = " in x] == [
        f"memory.{k}" for k in (*memory.EMBEDDING_SETTINGS, *memory.RECALL_SETTINGS)]  # a section by its name


def test_the_worker_card_honors_footer_paths_and_times(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOON)        # one fixed noon: a row begun 185 s ago is today's at any hour the suite runs
    row = worker_row()
    items = [(True, "first tick")]
    full = workers.card_text(row, items, look=words.Look(footer="full"))
    assert full.splitlines()[-2:] == ["-# worker abc123, 3 min of 15, account one", "-# model a-model"]
    end = settled("done", row)
    owner = workers.card_text(row, items, end, look=words.Look(paths="owner"), privacy="owner")
    shared = workers.card_text(row, items, end, look=words.Look(paths="owner"), privacy="family")
    assert owner.splitlines()[-1] == f"-# {HOME}/REPORT.md" and HOME not in shared
    clock = workers.card_text(row, items, look=words.Look(times="clock"))
    assert re.search(r"-# worker abc123, started \d\d:\d\d, cap 15 min$", clock)
    assert workers.card_text(row, items, accounts=2).splitlines()[-1] == "-# worker abc123, 3 min of 15, account one"
    fresh = workers.card_text(worker_row(cap=300, started=time.time() - 10), items, accounts=4)   # as it ran live
    assert fresh.splitlines()[-2:] == ["-# worker abc123, under 1 min of 5", "-# account one"]
    assert all(words.width(x) <= words.PHONE for x in fresh.splitlines())
    assert words.configure({"footer": "full"}) == words.Look(footer="full") == words.LOOK
    words.configure(None)
    assert words.LOOK == words.Look()


def test_a_card_begun_before_midnight_says_its_day(monkeypatch):
    """A deploy's fast tests ran through midnight on 2026-11-10 and the test above failed for the first 185 seconds of the
    day, so the deploy stopped: its row, begun 185 s before, was the evening before's, which words.clock says with its day."""
    monkeypatch.setattr(time, "time", lambda: datetime(2026, 1, 8, 0, 0, 20).timestamp())
    card = workers.card_text(worker_row(), [(True, "first tick")], look=words.Look(times="clock"))
    assert re.search(r"started Wed 23:57, cap 15 min$", card), card
    assert all(words.width(x) <= words.PHONE for x in card.splitlines()), card


def test_times_are_said_the_way_a_person_says_them():
    assert [words.span(x) for x in (18, 181, 3725 * 2)] == ["18 s", "3 min", "2 h 4 min"]
    assert words.of_cap(30, 900) == "under 1 min of 15" and words.of_cap(185, 900) == "3 min of 15"
    assert words.ago(20) == "just now" and words.ago(125) == "2 min ago"
    now = time.time()                                       # one reading for both, so midnight between two readings changes nothing
    assert re.fullmatch(r"\d\d:\d\d", words.clock(now, now)) and re.fullmatch(r"\w{3} \d\d:\d\d", words.clock(now - 3 * 86400, now))
    assert words.sizes(20_971_521, 20_971_520) == ("20,971,521 bytes", "20,971,520 bytes")
    assert words.sizes(26_214_400, 20_971_520) == ("25 MB", "20 MB")


def test_the_console_channel_shows_the_rooms_own_words(capsys):
    asyncio.run(ConsoleChannel().notify("r", NO_KEY_LINE))
    assert NO_KEY_LINE.splitlines()[0] in json.loads(capsys.readouterr().out)["notice"]


def test_job_lines_and_the_waiting_card_are_plain_and_state_first():
    """Run 2's two alert lines (a job's failure and a job worker's death) go through words.outcome and
    words.died like every other room line: no exit code, no systemd word, state first, phone width."""
    lines = [words.job_line("bad", "failed"), words.job_line("slow", "timed out", 600),
             words.job_line("crash", "died", 0, "exit-code"), words.job_line("gone", "lost"),
             words.job_line("never", "did not start"), words.job_line("held", "stopped", 60),
             words.job_line("sig", "failed", result="signal")]
    assert lines[:2] == [f"{words.NOT_DONE} job bad failed: it ended with an error",
                         f"{words.NOT_DONE} job slow stopped: timed out at 10 min"]
    assert lines[6].endswith("job sig failed: it was killed")
    card = workers.card_text({"id": "abc123", "account": "one", "started": 0.0, "cap": 300.0, "waiting": 1, "line": 2},
                             [(False, "write it up")])
    status = workers.status_lines.__doc__ or ""
    for x in [*lines, *card.splitlines()]:                # a notice may wrap on a long job name; a card row never
        assert not room_safe(x) and "exit" not in x and words.width(x) <= (words.PHONE if x in card else 60), x
    assert all(state_first(x) for x in lines) and state_first(card)
    assert card.splitlines()[:2] == [f"{words.WORKING} worker waiting for an account",
                                     f"{words.WAITING} 2nd in line for account one"]
    assert status
