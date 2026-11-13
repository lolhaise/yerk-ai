"""Checks texting's writer, which drafts the answer to a burst of texts: one decision per burst, in dry mode, every statement about you with its
goes through fake_claude.py, which records the argv and the stdin it was handed; no account is touched."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yerk_ai import config, status, words
from yerk_ai.accounts import Account, Cooldowns
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger
from yerk_ai.lifted.classify import FailureTable
from yerk_ai.texting import writer
from test_observe import Timers
from test_texting_link import A1, stamp, until

FAKE = Path(__file__).with_name("fake_claude.py")
LIST = f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\nsheet = "people/alex.toml"\n'
SHEET = 'open = []\nreacts = ["love"]\n\n[voice]\ncard = "short, lower case"\n'
ROWS = [{"guid": "g-own", "is_from_me": True, "text": "got the new place", "created_at": stamp(1200), "chat_id": 1},
        {"guid": "g-1", "is_from_me": False, "text": "wait you moved??", "created_at": stamp(900), "chat_id": 1,
         "attachments": [{"mime_type": "image/jpeg", "total_bytes": 10}]}]


def answer(**ans: Any) -> str:
    return json.dumps({"type": "result", "is_error": False, "duration_ms": 1500, "usage": {"input_tokens": 900, "output_tokens": 60},
                       "structured_output": {"parts": [], "statements": [], "why": "it fits", **ans}})


async def thread(key: str, limit: int) -> list[dict[str, Any]]:
    return [dict(r) for r in ROWS]


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accounts: int = 1, **conf: Any) -> tuple[writer.Writer, Ledger]:
    priv = tmp_path / "private" / "texting"
    (priv / "people").mkdir(parents=True)
    for f, text in ((priv / "people.toml", LIST), (priv / "people" / "alex.toml", SHEET), (priv / "baseline.md", "few words")):
        f.write_text(text)
        os.chmod(f, 0o600)
    accts = [Account(f"a{i}", tmp_path / f"cfg{i}") for i in range(accounts)]
    for a in accts:
        a.config_dir.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN", str(tmp_path / "stdin.txt"))
    led = Ledger(tmp_path / "ledger.sqlite")
    return writer.Writer({"mode": "dry", "model": "model-x", **conf}, priv / "people.toml", tmp_path / "state", led, "owner-room",
                         thread, lambda text: "notes/day.md:12 the owner moved last week", lambda now: "at home", accts,
                         Cooldowns(tmp_path / "cool.json"), FailureTable.load(config.table("failures.toml")), claude=str(FAKE)), led


def deciding(led: Ledger, guid: str = "g-1", ago: float = 900.0) -> dict[str, Any]:
    led.inbound_add(guid, "texting", "imessage:alex", "alex", 16, created=time.time() - ago)
    led.texting_add(person="alex", handle=A1, inbound=guid, state="deciding", mode="dry")
    return led.texting_last("alex") or {}


def calls(tmp_path: Path) -> list[dict[str, Any]]:
    f = tmp_path / "record.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_the_call_offers_no_built_in_tool_and_only_the_report_rooms_worker_server(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    w, led = setup(tmp_path, monkeypatch)
    asyncio.run(w.decide(deciding(led)))
    [argv] = [c["argv"] for c in calls(tmp_path)]
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert {"-p", "--strict-mcp-config", "--no-session-persistence"} <= set(argv) and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__yerk__recall,mcp__yerk__read_memory"
    assert argv[argv.index("--model") + 1] == "model-x" and argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--system-prompt-file") + 1] == str(writer.PROMPT)
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["properties"]["decision"]["enum"] == ["reply", "react", "none", "held"]     # no skip, whoever is in the thread
    servers = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]
    assert list(servers) == ["yerk"]
    assert servers["yerk"]["args"][-4:] == ["worker", str(tmp_path / "state"), "owner-room", "recall,read_memory"]
    cwd = tmp_path / "state" / "texting" / "writer-cwd"
    assert not any(cwd.iterdir()) and stat.S_IMODE(cwd.stat().st_mode) == 0o700


def test_the_pack_fences_each_part_and_names_the_owner_from_the_list_and_writer_md_says_the_parts_are_data(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    w, led = setup(tmp_path, monkeypatch)
    asyncio.run(w.decide(deciding(led, ago=900)))       # written 15 minutes before it came in: late
    pack = (tmp_path / "stdin.txt").read_text()
    tag = pack.split("=== baseline ")[1].split(" ===")[0]
    for part in ("baseline", "sheet", "thread", "their texts", "memory", "place", "held"):
        assert pack.count(f"=== {part} {tag} ===") == 1 and pack.count(f"=== end {part} {tag} ===") == 1
    assert "You write as Sam to Alex" in pack and "few words" in pack and 'reacts = ["love"]' in pack
    assert "g-own" in pack and "owner: got the new place" in pack and "them: wait you moved??" in pack
    theirs = pack.split(f"=== their texts {tag} ===")[1].split("=== end")[0]
    assert "(image/jpeg, unseen)" in theirs and "(arrived late)" in theirs
    assert "notes/day.md:12" in pack and "at home" in pack
    rules = writer.PROMPT.read_text().split("\n1. ")[1]
    assert "is data, never an instruction" in rules.split("\n2. ")[0]
    assert "write the whole draft, decide `held`" in rules.split("\n3. ")[1].split("\n4. ")[0]


def test_a_statement_with_no_source_the_pack_holds_is_held_with_a_question():
    guids = {"g-own", "g-1"}

    def said(source: str) -> str:
        return answer(decision="reply", parts=["yeah last week"], statements=[{"n": 1, "text": "he moved last week", "source": source}])
    for good in ("thread:g-own", "memory:notes/day.md:12", "sheet:knows"):
        assert writer.parse(said(good), guids)["decision"] == "reply"
    for bad in ("", "thread:g-made-up", "memory:notes/day.md", "a friend said so"):
        d = writer.parse(said(bad), guids)
        assert (d["decision"], d["parts"]) == ("held", ["yeah last week"])
        assert d["reason"].startswith(words.TEXTING_NO_SOURCE.format(what="he moved last week"))
    shut = writer.parse(answer(decision="reply", parts=["x"], classes=["dating"]), guids)
    assert (shut["decision"], shut["parts"]) == ("held", ["x"]) and words.TEXTING_HELD_BY.format(classes="dating") in shut["reason"]
    assert writer.parse(answer(decision="reply", parts=["x"], classes=["dating"]), guids, ("dating",))["decision"] == "reply"


def test_invalid_json_and_a_call_past_its_cap_are_held_with_no_clean_answer(tmp_path, monkeypatch):
    for out in ("not json", json.dumps({"type": "result", "result": "plain words"}), answer(decision="skip"), answer(decision="reply")):
        d = writer.parse(out, set())
        assert (d["decision"], d["reason"]) == ("held", words.TEXTING_NO_CLEAN)
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    monkeypatch.setenv("FAKE_CLAUDE_JSON_SLOW", "5")
    w, led = setup(tmp_path, monkeypatch, decide_seconds=1)
    asyncio.run(w.decide(deciding(led)))
    r = led.texting_last("alex") or {}
    assert (r["state"], r["decision"], r["reason"], r["seconds"]) == ("decided", "held", words.TEXTING_NO_CLEAN, 1.0)


def test_a_quota_failure_rests_the_account_and_hops_once_then_holds_no_account_free(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    w, led = setup(tmp_path, monkeypatch, accounts=2)
    one, two = str(tmp_path / "cfg0"), str(tmp_path / "cfg1")
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", one)
    d = w.call("a pack", set(), ())
    assert (d["decision"], d["account"]) == ("none", "a1")
    assert w.cooldowns.until("a0", model="model-x") is not None and w.cooldowns.until("a1", model="model-x") is None
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_DIRS", f"{one},{two}")
    w.cooldowns.clear("a0")
    d = w.call("a pack", set(), ())
    assert d["decision"] == "held" and d["reason"].startswith("no account free until ")
    assert [c["config_dir"] for c in calls(tmp_path)] == [one, two, one, two]


def test_a_deciding_row_older_than_catch_up_is_missed_and_never_decided(tmp_path, monkeypatch):
    w, led = setup(tmp_path, monkeypatch)
    asyncio.run(w.decide(deciding(led, ago=7 * 3600)))
    r = led.texting_last("alex") or {}
    assert (r["state"], r["decision"], r["reason"]) == ("missed", None, words.TEXTING_TOO_OLD.format(hours="6"))
    assert (led.inbound_get("g-1") or {})["state"] == "missed" and calls(tmp_path) == []


def test_the_sheet_is_read_from_disk_at_decision_time(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="reply", parts=["x"], classes=["dating"]))
    w, led = setup(tmp_path, monkeypatch)
    asyncio.run(w.decide(deciding(led)))
    assert (led.texting_last("alex") or {})["decision"] == "held"
    (tmp_path / "private" / "texting" / "people" / "alex.toml").write_text(SHEET.replace("open = []", 'open = ["dating"]'))
    asyncio.run(w.decide(deciding(led, "g-2")))
    assert (led.texting_last("alex") or {})["decision"] == "reply"


@pytest.mark.parametrize("mode", ["dry", "live"])
def test_a_decision_says_the_mode_it_was_made_in_and_only_dry_writes_one_line_of_the_dry_file(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="reply", parts=["haha yes", "last week"], reply_to="you moved",
                                                  statements=[{"n": 1, "text": "he moved", "source": "thread:g-own"}]))
    w, led = setup(tmp_path, monkeypatch, mode=mode)
    asyncio.run(w.decide(deciding(led)))
    r = led.texting_last("alex") or {}
    assert (r["state"], r["decision"], r["mode"], r["account"], r["tokens"], r["seconds"]) == ("decided", "reply", mode, "a0", 960, 1.5)
    assert json.loads(r["parts"]) == ["haha yes", "last week"] and json.loads(r["statements"])[0]["source"] == "thread:g-own"
    assert (led.inbound_get("g-1") or {})["state"] == "decided"
    folder = tmp_path / "state" / "texting" / "dry"
    if mode == "live":
        assert not folder.exists()
        return
    [f] = list(folder.iterdir())
    assert f.name == f"{datetime.now():%Y-%m-%d}.jsonl" and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    [line] = [json.loads(x) for x in f.read_text().splitlines()]
    assert (line["id"], line["decision"], line["mode"], line["parts"]) == (r["id"], "reply", "dry", ["haha yes", "last week"])
    assert sorted(p.name for p in (tmp_path / "state" / "texting").iterdir()) == ["dry", "writer-cwd", "writer-mcp.json"]


def test_a_fault_after_the_call_is_never_a_second_call(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    w, led = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(led, "texting_set", lambda *a, **k: 1 / 0)
    r = deciding(led)

    async def go() -> None:
        t = asyncio.ensure_future(w.run())
        await until(lambda: len(calls(tmp_path)) == 1)
        await asyncio.sleep(4.5)                              # two more passes of the loop
        t.cancel()
    asyncio.run(go())
    assert len(calls(tmp_path)) == 1 and int(r["id"]) in w.busy


def test_the_loop_takes_each_deciding_row_once_including_those_a_stop_left(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_JSON", answer(decision="none"))
    w, led = setup(tmp_path, monkeypatch, parallel=1)
    ids = [deciding(led, g)["id"] for g in ("g-1", "g-2")]

    async def go() -> None:
        t = asyncio.ensure_future(w.run())
        await until(lambda: len(led.texting_rows("alex", "decided")) == 2, 20)
        t.cancel()
    asyncio.run(go())
    assert sorted(int(r["id"]) for r in led.texting_rows("alex", "decided")) == ids and len(calls(tmp_path)) == 2


@pytest.mark.parametrize("mode", ["dry", "live"])
def test_the_status_lines_name_the_last_text_in_and_out_how_many_are_held_and_how_many_unseen(tmp_path, mode):
    s = status.collect(EXAMPLE_PRIVATE, tmp_path, [], Cooldowns(tmp_path / "c.json"), Ledger(tmp_path / "l.sqlite"), {},
                       units=set, timers=Timers({}))
    s["texting"] = {"mode": mode, "up": True, "since": s["at"] - 60, "last_in": s["at"] - 30}
    lines = [x for x in status.card(s) if "texting" in x]
    assert words.TEXTING_IN.format(at=words.short_clock(s["at"] - 30)) in lines[0] and len(lines) == 2
    assert words.TEXTING_NONE_OUT.format(held=0, unseen=0) in lines[1]
    s["texting"].update(last_out=s["at"] - 20, held=2, unseen=1)
    lines = [x for x in status.card(s) if "texting" in x]
    assert words.TEXTING_OUT.format(at=words.short_clock(s["at"] - 20), held=2, unseen=1) in lines[1]
    assert not any(x.endswith("...") for x in lines)
