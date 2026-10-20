"""Checks recall, what a room is handed from memory before it writes its first word. Covered: mention recall (notes
on whatever a message names), the room-topic hit, the recall pack and its re-add after compaction, the evidence
atomically. Every value here is a placeholder."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from yerk_ai import evidence, hooks, mcp_server, memindex, memory, mentions
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.engine import SessionTable

UTC = timezone.utc
NOW = datetime(2026, 1, 20, 15, 0, tzinfo=UTC)


def tree(tmp_path: Path) -> memory.Settings:
    private = tmp_path / "private"
    (private / "memory" / "facts").mkdir(parents=True)
    (private / "memory" / "daily").mkdir(parents=True)
    (private / "memory" / "facts" / "people.md").write_text(
        "# People\n\n- Example Neighbour: lives on Example Street; borrowed the example ladder.\n"
        "- Example Cousin (ask-only): ⛔ never raised unasked.\n"
        "- The example ladder is back in the shed, which is a sentence and never a key.\n")
    (private / "memory" / "facts" / "car.md").write_text("# Car\n\nAn example hatchback, grey, 2011.\n")
    (private / "memory" / "facts" / "private.md").write_text("- Example Secret: kept out by the config.\n")
    (private / "memory" / "MEMORY.md").write_text("# Curated\n\n## Example Project\n\nThe example project ships "
                                                 "on a Friday.\n")
    return memory.settings(private, tmp_path / "state", {"timezone": "UTC", "mentions_exclude": ["private.md"]})


def day(s: memory.Settings, n: int, lines: list[str]) -> None:
    d = (NOW - timedelta(days=n)).date().isoformat()
    (s.daily / f"{d}.md").write_text(f"# {d}\n\n" + "\n".join(lines) + "\n")


def build_names(s: memory.Settings) -> mentions.Index:
    b = mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude)
    assert b.refresh(s.scope.files()) is not None
    assert b.refresh(s.scope.files()) is None                 # nothing moved: not built again
    return mentions.Index(s.mentions).load()


# 1. mention recall
def test_mention_index_keys_names_files_and_headings_and_drops_marked_and_excluded(tmp_path):
    s = tree(tmp_path)
    idx = build_names(s)
    assert oct(s.mentions.stat().st_mode & 0o777) == "0o600"
    keys = [e["key"] for e in idx.entries]
    assert "Example Neighbour" in keys and "car" in keys and "Example Project" in keys
    assert "Example Cousin" not in keys                       # the do-not-surface mark drops it
    assert not any("Secret" in k for k in keys)               # mentions_exclude keeps the file out
    assert not any(k.startswith("The example ladder") for k in keys)
    hit = idx.match("did example neighbour's ladder come back? and the car")
    assert [e["key"] for e in hit] == ["Example Neighbour", "car"]
    assert hit[0]["cite"] == "facts/people.md:3" and (hit[0]["class"], hit[0]["room"]) == ("private", "")   # no front matter: private
    assert idx.match("nothing named here") == []


def test_the_hook_puts_named_entries_in_before_the_model_and_records_the_cost(tmp_path):
    s = tree(tmp_path)
    idx = build_names(s)
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-1", "one")
    meta = lambda room, prompt: ("owner", None)  # noqa: E731
    recall = lambda room, prompt: (idx.match(prompt), memory.topic_hits(s, room, prompt, set(), NOW))  # noqa: E731
    out = hooks.user_prompt_submit({"session_id": "sid-1", "prompt": "is Example Neighbour around?"}, tmp_path,
                                   rooms(EXAMPLE_PRIVATE), meta, budgets(), now=NOW, recall=recall)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "- Example Neighbour: lives on Example Street; borrowed the example ladder. (facts/people.md:3)" in ctx
    rec = json.loads((tmp_path / "rooms" / "example-room" / "assembled.jsonl").read_text().splitlines()[-1])
    assert rec["event"] == "message" and rec["names"] == ["Example Neighbour"]
    assert rec["parts"]["recall"] > 0 and rec["chars"] == len(ctx) and rec["text"] == ctx
    many = [{"key": f"k{i}", "text": "x" * 290, "cite": f"facts/f.md:{i}"} for i in range(9)]
    capped = hooks.render_recall(many, [], 1500)
    assert len(capped) <= 1500 and capped.count("\n- ") == 4          # whole entries, under the cap


# 2. the room-topic hit
def test_a_line_from_three_days_back_in_this_room_comes_in_on_its_topic(tmp_path):
    s = tree(tmp_path)
    day(s, 3, ["- 14:05:00 example-room owner: the example greenhouse heater stays at 17.5 degrees [m:a1]",
               "- 14:06:00 other-room owner: the example greenhouse heater there is at 9 degrees [m:a2]"])
    day(s, 15, ["- 10:00:00 example-room owner: the example greenhouse heater was at 12 degrees [m:old]"])
    day(s, 0, ["- 14:59:00 example-room owner: what does the greenhouse heater stay at? [m:now1]"])
    hits = memory.topic_hits(s, "example-room", "what does the greenhouse heater stay at?", {"now1"}, NOW)
    assert [h["cite"] for h in hits] == [f"daily/{(NOW - timedelta(days=3)).date()}.md:3"]
    assert memory.topic_hits(s, "example-room", "lunch plans", set(), NOW) == []


# 3. the recall pack
def test_the_pack_is_the_rooms_filed_items_newest_first_and_comes_back_after_compaction(tmp_path):
    s = tree(tmp_path)
    day(s, 2, ["- 09:00:00 example-room owner: here is the example design reel [m:b1]",
               "  - attachment: reel.mp4, video/mp4, 1,000 bytes",
               "- 09:01:00 example-room yerk: noted [m:b2]",
               "- 09:02:00 example-room yerk: we decided the example layout goes left [m:b3]",
               "- 09:03:00 other-room owner: not this room [m:b4]"])
    day(s, 0, ["- 14:00:00 example-room owner: the newest thing [m:b5]"])
    text, shown = memory.room_pack(s, "example-room", 6000, NOW)
    lines = text.splitlines()
    assert shown == 3 and "the newest thing" in lines[1] and "decided the example layout" in lines[2]
    assert "[attachment: reel.mp4, video/mp4, 1,000 bytes]" in lines[3] and "not this room" not in text
    assert "noted" not in text                                        # a plain reply is not a filed item
    small, n = memory.room_pack(s, "example-room", 400, NOW)
    assert len(small) <= 400 and n < 3 and "older items not shown" in small
    assert memory.room_pack(s, "example-room", 6000, NOW, exclude={"b5"})[1] == 2
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-9", "one")
    out = hooks.session_start({"session_id": "sid-9", "source": "compact"}, tmp_path, lambda room: text)
    assert out["hookSpecificOutput"] == {"hookEventName": "SessionStart", "additionalContext": text}
    rec = json.loads((tmp_path / "rooms" / "example-room" / "assembled.jsonl").read_text().splitlines()[-1])
    assert rec["event"] == "compact" and rec["parts"] == {"recall_pack": len(text)}
    assert hooks.session_start({"session_id": "sid-9", "source": "startup"}, tmp_path, lambda room: text) == {}
    ss = hooks.settings_for("http://127.0.0.1:9/hook")["SessionStart"][0]
    assert ss["matcher"] == "startup|clear|compact" and ss["hooks"][0]["type"] == "command"
    assert ss["hooks"][0]["command"].endswith("--data-binary @- http://127.0.0.1:9/hook")


# 4. the evidence ledger and the shadow Stop check
def stream(ev: evidence.Evidence, tool: str, ti: dict, result: str, n: int = 1) -> None:
    ev.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": tool,
                                                              "input": ti}]}})
    ev.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                                                         "content": [{"type": "text", "text": result}]}]}})


def test_read_then_quote_logs_nothing_and_quote_without_read_logs_the_figure(tmp_path):
    calls = []
    ev = evidence.Evidence(on_call=calls.append)
    roots = (tmp_path / "memory",)
    filed = lambda fig: "facts/boiler.md:3" if fig == "1.35" else None  # noqa: E731
    known = lambda name: None  # noqa: E731
    stream(ev, "mcp__yerk__read_memory", {"path": "facts/boiler.md"}, "- The example boiler holds 1.35 bar cold.")
    assert calls[0]["tool"] == "mcp__yerk__read_memory" and calls[0]["target"] == "path=facts/boiler.md"
    assert evidence.check("1.35 bar cold, per facts/boiler.md:3", ["how much?"], ev, roots, filed, known) == []
    ev.reset()
    ev.give("what was that number again?")
    found = evidence.check("1.35 bar", ["what was that number again?"], ev, roots, filed, known)
    assert len(found) == 1 and found[0][0] == "figure" and "1.35" in found[0][1] and "missing read" in found[0][1]
    ev.reset()
    assert evidence.check("we went with the left layout", ["what did we decide about it?"], ev, roots, filed,
                          known)[0][0] == "decide"
    stream(ev, "mcp__yerk__recall", {"query": "layout"}, "...")
    assert evidence.check("we went with the left layout", ["what did we decide?"], ev, roots, filed, known) == []


def test_the_store_drops_the_oldest_text_never_a_later_hit():
    ev = evidence.Evidence()
    for n in range(12):
        stream(ev, "Read", {"file_path": f"/x/{n}.md"}, f"figure {n}.5 " + "y" * 50_000, n)
    seen = ev.read_text()
    assert "figure 11.5 " in seen and "figure 0.5 " not in seen and ev.kept <= evidence.KEEP_CHARS
    assert "file_path=/x/0.md" in seen                                # what a call read stays


def test_the_shadow_check_runs_at_stop_through_the_daemon_and_never_blocks(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    room = y.add_room("example-room", ConsoleChannel())
    (y.memory.root / "facts").mkdir(parents=True)
    (y.memory.root / "facts" / "boiler.md").write_text("# Boiler\n\n- The example boiler holds 1.35 bar cold.\n")
    memindex.Index(y.memory.index_db, y.memory.scope).sync()
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-3", "one")
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    with TestClient(app) as c:
        stream(room.evidence, "mcp__yerk__read_memory", {"path": "facts/boiler.md"}, "holds 1.35 bar cold")
        a = c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-3",
                                  "last_assistant_message": "1.35 bar cold"})
        room.evidence.reset()
        b = c.post("/hook", json={"hook_event_name": "Stop", "session_id": "sid-3",
                                  "last_assistant_message": "1.35 bar"})
    assert a.json() == {} and b.json() == {}                       # the reply is never touched
    rows = y.ledger.rows(table="shadow")
    assert len(rows) == 1 and rows[0]["rule"] == "figure" and "facts/boiler.md:3" in rows[0]["detail"]
    assert y.ledger.rows(table="evidence")[0]["target"] == "path=facts/boiler.md"


# 5. the rebuild swaps atomically
def test_a_recall_during_a_rebuild_answers_from_the_old_or_new_index_never_an_error(tmp_path):
    s = tree(tmp_path)
    for n in range(300):
        (s.root / "facts" / f"f{n}.md").write_text(f"# F{n}\n\n- example kettle number {n}\n" * 20)
    idx = memindex.Index(s.index_db, s.scope)
    idx.sync()
    results: list[dict] = []
    stop = threading.Event()

    def ask() -> None:
        while not stop.is_set():
            results.append(memory.recall(s, "example kettle", 3))

    t = threading.Thread(target=ask)
    t.start()
    gen = memindex.rebuild(s.index_db, s.scope)
    time.sleep(0.2)
    stop.set()
    t.join()
    assert results and all(r["ok"] for r in results), [r for r in results if not r["ok"]][:1]
    assert s.index_db.is_symlink() and memindex.live(s.index_db).name == gen.name
    idx.sync()                                                       # the writer moves to the new file
    assert idx.real.name == gen.name
    idx.close()
    assert memindex.sweep(s.index_db) == []                          # within the grace: kept
    memindex.sweep(s.index_db, now=time.time() + memindex.SWEEP_GRACE + 1)
    names = {p.name for p in s.index_db.parent.iterdir()}
    assert gen.name in names and "index.sqlite-wal" not in names and "index.sqlite-shm" not in names
    assert memory.recall(s, "example kettle", 3)["ok"]
