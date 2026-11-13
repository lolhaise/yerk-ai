"""Checks the texting replay, which scores today's writer and check against past text threads. Past threads are cut at each burst of theirs,
put through the live writer and the live check as of each cut, scored beside what went, writing only under <state>/texting/replay/ (run 7a).
records the argv and the stdin it was handed; no account is touched, nothing is sent."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from yerk_ai import config, words
from yerk_ai.accounts import Account
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.texting import replay, writer
from yerk_ai.texting.people import load
from test_texting_link import A1
from test_texting_writer import FAKE, LIST, SHEET, answer, calls, setup

TZ = ZoneInfo("America/Toronto")
THREAD = f"""# Texts with Alex ({A1})
6 messages, 2026-10-01 to 2026-10-03

- 2026-10-01 09:00 Sam: morning
- 2026-10-01 09:05 Alex: you up
- 2026-10-01 09:06 Alex: coffee later?
- 2026-10-01 09:20 Sam: sure, at ten
- 2026-10-02 18:00 Alex: did you see the game
- 2026-10-03 08:00 Alex: [attachment: a.jpg]
"""


def install(tmp_path: Path) -> tuple[Path, Path]:
    """A made-up install: the reply list and Alex's sheet, Alex's thread file and a group's that names the same handle."""
    priv, state = tmp_path / "private", tmp_path / "state"
    (priv / "texting" / "people").mkdir(parents=True)
    (priv / "texting" / "people.toml").write_text(LIST)
    (priv / "texting" / "people" / "alex.toml").write_text(SHEET)
    for f in (priv / "texting" / "people.toml", priv / "texting" / "people" / "alex.toml"):
        os.chmod(f, 0o600)
    texts = priv / "imports" / "texts"
    texts.mkdir(parents=True)
    (texts / "alex.md").write_text(THREAD)
    (texts / "group-1.md").write_text(f"# Texts in group Pals (members: {A1})\n1 messages\n\n- 2026-10-01 09:00 Alex: hi all\n")
    state.mkdir()
    (tmp_path / "a0.token").write_text("token-a0")                               # the check runs on a token alone (J28)
    return priv, state


def thread_cuts(priv: Path, since: date = date(2026, 10, 1), limit: int = 10) -> list[replay.Cut]:
    return replay.cuts("alex", replay.read_thread(priv / "imports" / "texts" / "alex.md", "Sam", TZ), since, limit, 6.0)


def test_a_thread_file_reads_into_lines_bursts_and_cuts_newest_first_and_only_its_one_to_one_file_is_found(tmp_path):
    priv, _ = install(tmp_path)
    who = load(priv / "texting" / "people.toml")
    [f] = replay.find(priv / "imports" / "texts", who.people["alex"])           # the group's file names the handle too: never
    lines = replay.read_thread(f, who.owner, TZ)
    assert [(x.n, x.side) for x in lines] == [(4, "owner"), (5, "them"), (6, "them"), (7, "owner"), (8, "them"), (9, "them")]
    assert lines[1].text == "you up" and lines[0].at == datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    assert replay.bursts(lines) == [(1, 3), (4, 5), (5, 6)]                  # a day apart: two bursts, the owner's line splits
    cs = thread_cuts(priv)
    assert [[x.n for x in c.burst] for c in cs] == [[9], [8], [5, 6]]
    assert [x.n for x in cs[2].before] == [4] and [x.n for x in cs[2].went] == [7] and cs[2].at.minute == 6
    assert [c.open for c in cs] == [True, False, False] and cs[1].went == []  # their next line ends what went
    assert [[x.n for x in c.burst] for c in thread_cuts(priv, date(2026, 10, 2), 1)] == [[9]]
    (priv / "imports" / "texts" / "alex-2.md").write_text(THREAD)
    assert len(replay.find(priv / "imports" / "texts", who.people["alex"])) == 2    # two files: a skip, never a guess


def test_a_burst_they_followed_with_another_of_their_own_is_not_open_and_scores_by_what_went(tmp_path):
    priv, _ = install(tmp_path)
    with open(priv / "imports" / "texts" / "alex.md", "a") as f:
        f.write("- 2026-10-03 08:30 Alex: hello?\n")                       # half an hour on: the file ends in two bursts of theirs
    last, first = thread_cuts(priv)[:2]
    assert [x.n for x in first.burst] == [9] and first.went == [] and not first.open and last.open
    assert [replay.score(first, d, p) for d, p in (("none", []), ("reply", ["a"]))] == ["agree", "more"]


def test_then_is_the_minutes_to_their_next_line_when_nothing_of_the_owners_came_between(tmp_path):
    priv, _ = install(tmp_path)
    cs = thread_cuts(priv)
    assert [c.then for c in cs] == [None, 840, None]      # the file's last burst; a night to their next; the owner's answer between
    assert replay.entry(cs[1], {})["then"] == 840


def test_what_went_is_marked_the_old_agents_by_its_log_and_each_cut_gets_one_score(tmp_path):
    priv, _ = install(tmp_path)
    log = tmp_path / "old.log"
    log.write_text('2026-10-01 09:12 Alex IN: "coffee later?" -> reply (pace) OUT: "Sure,  at TEN" + "ok"\n'
                   '2026-10-01 09:04 Alex IN: "x" -> skip OUT: -\n')
    cs = thread_cuts(priv)
    assert [replay.score(cs[2], d, p) for d, p in (("reply", ["a"]), ("held", ["a"]), ("none", []), ("held", []), ("react", []))] == \
        ["agree", "agree", "less", "asks", "tapback"]
    assert [replay.score(cs[1], d, ["a"]) for d in ("reply", "none")] == ["more", "agree"]
    assert replay.score(cs[0], "none", []) == "open" and replay.score(cs[0], "reply", ["a"]) == "open"
    replay.mark(cs[2].went, replay.old_log(log, TZ), "Alex")
    assert [x.side for x in cs[2].went] == ["auto"] and replay.score(cs[2], "reply", ["a"]) == "auto"
    late = thread_cuts(priv)
    log.write_text('2026-10-01 09:04 Alex IN: "you up" -> reply OUT: "sure, at ten"\n')        # sixteen minutes before: the owner's
    replay.mark(late[2].went, replay.old_log(log, TZ), "Alex")
    assert [x.side for x in late[2].went] == ["owner"]


def test_the_live_writers_argv_is_what_it_was_and_the_replayed_writer_has_no_tool_and_no_server(tmp_path, monkeypatch):
    w, _ = setup(tmp_path, monkeypatch)
    assert w.argv == [str(FAKE), "-p", "--output-format", "json", "--json-schema", json.dumps(writer.SCHEMA), "--system-prompt-file",
                      str(writer.PROMPT), "--tools", "", "--mcp-config", str(tmp_path / "state" / "texting" / "writer-mcp.json"),
                      "--strict-mcp-config", "--allowedTools", "mcp__yerk__recall,mcp__yerk__read_memory", "--setting-sources", "",
                      "--no-session-persistence", "--model", "model-x", "--effort", "high"]           # as on main, byte for byte
    assert list(json.loads(Path(w.argv[w.argv.index("--mcp-config") + 1]).read_text())["mcpServers"]) == ["yerk"]
    bare = writer.Writer({"mode": "dry"}, w.file, tmp_path / "s2", w.ledger, "owner-room", w.thread, w.recall, w.place, w.accounts,
                         w.cooldowns, w.failures, claude=str(FAKE), tools=[])
    assert "--mcp-config" not in bare.argv and "--allowedTools" not in bare.argv and not [a for a in bare.argv if "mcp__" in a]
    assert sorted(a for a in bare.argv if a.startswith("-")) == sorted(a for a in replay.check.Check({}, bare).argv if a.startswith("-"))
    assert not (tmp_path / "s2" / "texting" / "writer-mcp.json").exists()        # the check's flags, as the canary ran them


def run(tmp_path: Path, monkeypatch, priv: Path, state: Path, cs: list[replay.Cut], hits: list[dict], search=None) -> list[dict]:
    acct = Account("a0", tmp_path / "cfg0", token_file=tmp_path / "a0.token")
    acct.config_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.txt"))
    return replay.go(state, "t1", {"since": "2026-10-01"}, {"mode": "dry", "model": "model-x"}, priv / "texting" / "people.toml",
                     "owner-room", [(c, A1) for c in cs], search or (lambda text: hits), lambda name: priv / "memory" / name, priv / "imports" / "texts",
                     [acct], FailureTable.load(config.table("failures.toml")), claude=str(FAKE))


def test_a_cut_is_decided_and_checked_by_the_live_code_as_of_its_minute_with_no_tool(tmp_path, monkeypatch):
    priv, state = install(tmp_path)
    (priv / "memory" / "daily").mkdir(parents=True)
    (priv / "memory" / "daily" / "2026-10-02.md").write_text("the day after\n")
    (priv / "memory" / "daily" / "2026-09-24.md").write_text("a week before\n")
    alex = priv / "imports" / "texts" / "alex.md"
    hits = [{"cite": f"{alex}:7", "text": "- 2026-10-01 09:20 Sam: sure, at ten"},
            {"cite": "daily/2026-10-02.md:1", "text": "DAY-AFTER-LINE"}, {"cite": "daily/2026-09-24.md:1", "text": "WEEK-BEFORE-LINE"},
            {"cite": f"{alex}:4", "text": "- 2026-10-01 09:00 Sam: morning"}, {"cite": f"{alex}:2", "text": "6 messages, 2026-10-01 to 2026-10-03"},
            {"cite": "daily/2026-09-01.md:1", "text": "NO-SUCH-FILE-LINE"},                # a cite read back to no file on disk
            {"cite": "notes.md:1", "text": "- 2026-10-01 09:20 Sam: COPIED-LATER-LINE"},    # a later line kept in an undated file
            {"cite": "people.md:1", "text": "OLD-FACT-LINE"},                              # a living file last changed before the cut
            {"cite": "living.md:1", "text": "LIVING-FILE-LINE"},                           # one changed after it: it can hold the answer
            {"cite": "Screenshot_2026-10-02_at_9.seen.txt:1", "text": "SCREENSHOT-LINE"}]  # a later day inside a file's name
    for name, text in (("notes.md", "- 2026-10-01 09:20 Sam: COPIED-LATER-LINE\n"), ("people.md", "OLD-FACT-LINE\n"), ("living.md", "LIVING-FILE-LINE\n"),
                       ("Screenshot_2026-10-02_at_9.seen.txt", "SCREENSHOT-LINE\n")):
        (priv / "memory" / name).write_text(text)
    before = datetime(2026, 9, 1, tzinfo=TZ).timestamp()
    for name in ("people.md", "Screenshot_2026-10-02_at_9.seen.txt"):                      # both last changed a month before the cut
        os.utime(priv / "memory" / name, (before, before))
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="reply", parts=["yes!"]))
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv)[2:], hits)
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "WEEK-BEFORE-LINE" in stdin and "DAY-AFTER-LINE" not in stdin and "sure, at ten" not in stdin
    assert "alex.md:4 - 2026-10-01 09:00 Sam: morning" in stdin and "6 messages" not in stdin   # the same thread's older line, by line
    assert "NO-SUCH-FILE-LINE" not in stdin and "COPIED-LATER-LINE" not in stdin
    assert "OLD-FACT-LINE" in stdin and "LIVING-FILE-LINE" not in stdin and "SCREENSHOT-LINE" not in stdin
    pack = stdin.split("=== end their texts")[0]
    assert "alex:4 " in pack and "alex:5 " in pack and "alex:6 " in pack and "alex:7 " not in pack and "alex:8 " not in pack
    assert "=== held" in stdin and stdin.split("=== held")[1].split("\n")[1] == "none"
    assert stdin.split("=== place")[1].split("\n")[1] == "none"
    [wr, ch] = [c["argv"] for c in calls(tmp_path)]                        # the writer's call, then the check's
    assert not [a for a in wr if "mcp__" in a]
    assert "--json-schema" in ch and str(writer.PROMPT) not in ch
    head, cut = [json.loads(x) for x in f.read_text().splitlines()]
    assert head == {"since": "2026-10-01"} and cut["id"] == "alex:5" and cut["at"] == "2026-10-01 09:06"
    assert cut["decision"] in ("reply", "held") and cut["verdict"] and cut["dropped"] == 7 and cut["kept"] == 3 and cut["score"] == "agree"
    assert cut["went"] == [{"at": "2026-10-01 09:20", "side": "owner", "text": "sure, at ten"}] and cut["account"] == "a0"


def test_a_replay_writes_only_under_its_own_folder_and_leaves_the_install_as_it_was(tmp_path, monkeypatch):
    priv, state = install(tmp_path)
    for f, text in ((state / "ledger.sqlite", "live ledger"), (state / "texting" / "dry" / "2026-10-01.jsonl", "{}\n"),
                    (priv / "memory" / "daily" / "2026-10-01.md", "a day\n"), (state / "cooldowns.json", "{}")):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv), [])
    base = state / "texting" / "replay"
    after = {p for p in tmp_path.rglob("*") if p.is_file() and not p.is_relative_to(base)} - {tmp_path / "record.jsonl", tmp_path / "stdin.txt"}
    assert after == set(before) and all((p.read_bytes(), p.stat().st_mtime_ns) == before[p] for p in before)
    assert sorted(x.name for x in base.iterdir()) == ["t1.jsonl"]          # the scratch folder is gone after a clean run
    assert stat.S_IMODE(base.stat().st_mode) == 0o700 and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert {(c := json.loads(x))["id"]: c["score"] for x in f.read_text().splitlines()[1:]} == \
        {"alex:9": "open", "alex:8": "agree", "alex:5": "less"}                # each cut's line lands as it ends


def test_a_cut_that_raises_is_written_with_its_error_the_run_goes_on_and_its_scratch_folder_stays(tmp_path, monkeypatch):
    priv, state = install(tmp_path)
    live = writer.Writer._decide

    seen: list[str] = []

    async def decide(self, row):
        if row["inbound"] == "alex:8":
            raise RuntimeError("boom\nsecond line")
        if row["inbound"].startswith("alex:5"):       # it starts once the cut that raised has ended: that line is on disk by now
            seen.extend((state / "texting" / "replay" / "t1.jsonl").read_text().splitlines())
        await live(self, row)
    monkeypatch.setattr(writer.Writer, "_decide", decide)
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv), [])
    head, *got = [json.loads(x) for x in f.read_text().splitlines()]
    assert head == {"since": "2026-10-01"} and sorted(x["id"] for x in got) == ["alex:5", "alex:8", "alex:9"]
    assert {x["id"]: x.get("error") for x in got} == {"alex:9": None, "alex:8": "RuntimeError: boom", "alex:5": None}
    assert json.loads(seen[0]) == {"since": "2026-10-01"} and [json.loads(x)["id"] for x in seen[1:]][:1] == ["alex:8"]
    assert (state / "texting" / "replay" / "t1" / "ledger.sqlite").exists()
    shutil.rmtree(state / "texting" / "replay" / "t1")


def test_a_search_that_does_not_answer_fails_its_cut_before_any_call(tmp_path, monkeypatch):
    priv, state = install(tmp_path)

    def search(text: str) -> list[dict]:
        raise LookupError(words.REPLAY_NO_SEARCH.format(why="timeout"))
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv), [], search)
    got = [json.loads(x) for x in f.read_text().splitlines()[1:]]
    assert len(got) == 3 and {x["error"] for x in got} == {"LookupError: the memory search did not answer: timeout"}
    assert {x["decision"] for x in got} == {""} and not (tmp_path / "record.jsonl").exists()     # never decided blind: no call made
    shutil.rmtree(state / "texting" / "replay" / "t1")


def test_a_call_no_model_answered_is_a_failed_cut_never_a_decision(tmp_path, monkeypatch):
    """2026-11-13, the first real run: every account refused the model, the writer held with "no clean answer" in 0.4 s and no
    tokens, and the run counted two held decisions that no model had made."""
    priv, state = install(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", str(tmp_path / "cfg0"))               # the one account refuses: no account free
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv), [])
    got = [json.loads(x) for x in f.read_text().splitlines()[1:]]
    assert len(got) == 3 and all(x["error"].startswith("LookupError: no model answered: no account free until ") for x in got)
    assert [x.split() for x in replay.table(got)][1][:1] == ["alex"] and replay.table(got)[1].split()[1:6] == ["3", "0", "0", "0", "0"]
    assert replay.table(got)[1].split()[list(words.REPLAY_COLUMNS).index("failed") + 1] == "3"
    assert not replay.unanswered({"reason": words.TEXTING_NO_CLEAN, "tokens": 900})     # a model answered and it would not parse: a real hold
    assert not replay.unanswered({"reason": "held by money", "tokens": 0})              # the code's own hold names its rule
    assert replay.unanswered({"reason": words.TEXTING_NO_TOKEN})                        # the check had no token to run on
    shutil.rmtree(state / "texting" / "replay" / "t1")


def test_a_check_no_model_answered_is_a_failed_cut_too(tmp_path, monkeypatch):
    priv, state = install(tmp_path)
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="reply", parts=["yes!"]))
    live = writer.Writer.hop

    def hop(self, argv, stdin, cwd, cap, model, read, hold, config=None):
        if str(writer.PROMPT) in argv:
            return live(self, argv, stdin, cwd, cap, model, read, hold)
        return {**hold(words.TEXTING_NO_CLEAN), "account": "a0", "seconds": cap}          # the check's call ran out of time
    monkeypatch.setattr(writer.Writer, "hop", hop)
    f = run(tmp_path, monkeypatch, priv, state, thread_cuts(priv)[2:], [])
    [cut] = [json.loads(x) for x in f.read_text().splitlines()[1:]]
    assert cut["error"] == "LookupError: no model answered: no clean answer"
    shutil.rmtree(state / "texting" / "replay" / "t1")


def test_the_command_plans_with_no_call_then_runs_printing_counts_only(tmp_path, monkeypatch, capsys):
    from yerk_ai import cli
    priv, state = install(tmp_path)
    (priv / "yerkd.toml").write_text('[texting]\nmode = "dry"\nreport = "owner-room"\nmodel = "model-z"\ncheck_model = "model-z"\n')
    (priv / "rooms.toml").write_text('[rooms.owner-room]\nchannel = "discord:1"\n')
    (priv / "accounts.toml").write_text(f'[accounts.a0]\nconfig_dir = "{tmp_path / "cfg0"}"\n')
    (tmp_path / "cfg0").mkdir()
    (priv / "secrets").mkdir()
    (priv / "secrets" / "claude-a0.token").write_text("token-a0")
    for k, v in (("YERK_CLAUDE", str(FAKE)), ("FAKE_CLAUDE_RECORD", str(tmp_path / "record.jsonl")),
                 ("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.txt")), ("FAKE_CLAUDE_JSON", answer(decision="reply", parts=["DRAFT-TEXT"]))):
        monkeypatch.setenv(k, v)
    yerk = ["texting", "replay", "--since", "2026-10-01", "--private", str(priv), "--state", str(state)]
    assert cli.main(yerk) == 0 and not (tmp_path / "record.jsonl").exists()               # the plan: nothing called
    assert capsys.readouterr().out.splitlines() == ["alex: alex.md, 3 cuts", words.REPLAY_PLAN.format(cuts=3, since="2026-10-01", limit=10),
                                                    words.REPLAY_DRY]
    (priv / "imports" / "texts" / "alex-2.md").write_text(THREAD)                # two files hold their handle: skipped, never a guess
    assert cli.main(yerk) == 0 and capsys.readouterr().out.splitlines()[:2] == [
        words.REPLAY_FILES.format(key="alex", n=2, files="alex-2.md, alex.md"), words.REPLAY_PLAN.format(cuts=0, since="2026-10-01", limit=10)]
    (priv / "imports" / "texts" / "alex-2.md").unlink()
    assert cli.main([*yerk, "--run"]) == 1 and not (state / "docs").exists() and not (tmp_path / "record.jsonl").exists()
    assert capsys.readouterr().out.splitlines()[-1] == words.REPLAY_NO_DOCS.format(docs=state / "docs")
    (state / "docs").mkdir()
    assert cli.main([*yerk, "--run", "--limit", "2"]) == 1 and not (tmp_path / "record.jsonl").exists()     # no index: no search, no call
    [f] = (state / "texting" / "replay").glob("*.jsonl")
    assert {json.loads(x)["error"].split(":")[0] for x in f.read_text().splitlines()[1:]} == {"LookupError"}
    assert capsys.readouterr().out.splitlines()[-1] == words.REPLAY_KEPT.format(n=2, folder=f.with_suffix(""))
    shutil.rmtree(state / "texting" / "replay")
    from yerk_ai.daemon import Yerkd
    monkeypatch.setattr(Yerkd, "search", lambda self, room, query, **kw: {"ok": True, "hits": []})
    assert cli.main([*yerk, "--run", "--limit", "2", "--model", "model-y"]) == 0 and len(calls(tmp_path)) == 4   # two cuts: a writer and a check call each
    assert all(c["argv"][c["argv"].index("--model") + 1] == "model-y" for c in calls(tmp_path))       # the flag's model, the writer's and the check's
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[2:8] == [words.REPLAY_MODEL.format(model="model-y"), words.REPLAY_AS_OF, words.REPLAY_LEFT_OUT, *words.REPLAY_LIMITS]
    assert lines[8].split() == list(words.REPLAY_COLUMNS) and [x.split()[:2] for x in lines[9:11]] == [["alex", "2"], ["total", "2"]]
    for said in ("morning", "you up", "coffee", "sure, at ten", "the game", "attachment", "DRAFT-TEXT"):
        assert said not in out
    [f] = (state / "texting" / "replay").glob("*.jsonl")
    assert lines[11] == words.REPLAY_FILE.format(file=f) and json.loads(f.read_text().splitlines()[0])["people"] == ["alex"]
    assert json.loads(f.read_text().splitlines()[0])["model"] == "model-y"
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 0 and capsys.readouterr().out == "every sheet loads\n"
    assert replay.located("imports/texts/alex.md", ((priv / "memory", "memory"), (priv / "imports", "imports")), priv / "memory") == \
        priv / "imports" / "texts" / "alex.md"                                    # a cite read back as memory.cite names it


def test_the_replay_never_imports_the_hands():
    src = Path(replay.__file__).read_text()
    assert "hands" not in src and "ssh" not in src and not re.search(r"^(from|import) .*\b(link|bridge|intake|holds)\b", src, re.M)
