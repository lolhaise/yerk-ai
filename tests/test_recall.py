"""Checks recall, what a room is handed from memory before it writes its first word. Covered: mention recall (notes
on whatever a message names), the room-topic hit, the recall pack and its re-add after compaction, the evidence
atomically. Every value here is a placeholder."""
import asyncio
import json
import shutil
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from yerk_ai import due, evidence, hooks, mcp_server, memindex, memory, mentions, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets, rooms
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from test_remember import daemon as daemon_of
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
    recall = lambda room, prompt: (idx.match(prompt), memory.topic_hits(s, room, prompt, set(), NOW), [], [])  # noqa: E731
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


def named(tmp_path: Path, files: dict[str, str]) -> mentions.Index:
    """The name index of only these fact files (stem: text), built."""
    (facts := tmp_path / "private" / "memory" / "facts").mkdir(parents=True)
    for stem, text in files.items():
        (facts / f"{stem}.md").write_text(text)
    return build_names(memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC"}))


def cites(entries: list[dict]) -> list[str]:
    return [e["cite"] for e in entries]


def test_a_nested_bullet_is_an_entry_keyed_as_a_top_level_one(tmp_path):
    idx = named(tmp_path, {"people": "- Example Sibling: the middle one.\n  - Example Sibling's job: an example firm.\n"
                                     "    - Example Bike: blue, 2019.\n"})
    assert cites(idx.match("what is example sibling's job? and the example bike")) == ["facts/people.md:2", "facts/people.md:3"]
    assert {"Example Sibling's job", "Example Bike"} <= {e["key"] for e in idx.entries}


def test_a_known_name_at_the_head_of_a_line_keys_the_line(tmp_path):
    idx = named(tmp_path, {"examplia": "- Examplia: the eldest.\n",
                           "people": "- Examplia lives in a townhouse her parents gave her at about 25, on the example street.\n"
                                     "- CONFIRMED 2026-01-19 17:38 off Examplia's own example profile: an example firm.\n"
                                     "- CONFIRMED 2026-01-19 17:38 by the example office: nobody here is named.\n"})
    assert cites(idx.match("where does examplia live?")) == ["facts/examplia.md:1", "facts/people.md:1", "facts/people.md:2"]
    assert idx.match("confirmed by the example office") == []                    # a non-name head is no name
    assert not any("nobody here" in e["text"] for e in idx.match("examplia"))      # no name in its first six tokens


def test_a_two_word_name_answers_to_both_words_and_a_first_name_to_its_own_line(tmp_path):
    idx = named(tmp_path, {"people": "- Ada Example: the example office's chief.\n- Black Example: an example show.\n"})
    assert cites(idx.match("and ada example?")) == ["facts/people.md:1"]
    assert idx.match("what does ada do?") == idx.match("the black cat") == []           # a first word alone is no key
    alias = named(tmp_path / "b", {"people": "- Ada Example: the chief.\n- Ada: Ada Example, by her first name.\n"})
    assert set(cites(alias.match("what does ada do?"))) == {"facts/people.md:1", "facts/people.md:2"}


def test_a_keys_own_fact_file_comes_first_and_a_text_is_listed_once(tmp_path):
    idx = named(tmp_path, {"aaa": "- Examplia: noted in a file that sorts first.\n",
                           "examplia": "- Examplia: the eldest, in her own file.\n- Examplia: likes the example ladder.\n",
                           "people": "".join(f"- Examplia: example line {i}.\n" for i in range(8))})
    assert cites(idx.match("examplia")) == ["facts/examplia.md:1", "facts/examplia.md:2", "facts/aaa.md:1",
                                            "facts/people.md:1", "facts/people.md:2", "facts/people.md:3"]
    assert mentions.MATCH_MAX == 6
    twice = named(tmp_path / "b", {"examplia": "- Examplia: likes tea.\n- Examplia: likes tea. And the example ladder.\n"})
    assert cites(twice.match("examplia")) == ["facts/examplia.md:1", "facts/examplia.md:2"]    # the name's entry, then line 2
    assert cites(named(tmp_path / "c", {"x": "- Examplia: likes tea.\n- Examplia: likes tea. And more.\n"}).match("examplia")) == ["facts/x.md:1"]


def test_a_line_is_listed_under_a_name_it_writes_as_one_and_a_keys_own_lines_come_first(tmp_path):
    idx = named(tmp_path, {"car": "- An example hatchback, grey.\n", "aaa": "- ALL EXAMPLE BROTHERS: an example game rank.\n",
                           "people": "- Examplia's car: an example coupe.\n- Car notes: the example hatchback's tyres.\n"
                                     "- Brother: two, both older, example names.\n"})
    assert cites(idx.match("my car")) == ["facts/car.md:1", "facts/people.md:2"]       # another's car is no line about the owner's
    assert cites(idx.match("what does my brother do?")) == ["facts/people.md:3", "facts/aaa.md:1"]     # the key's own line first


def test_a_plural_meets_its_singular_on_keys_and_messages_alike(tmp_path):
    idx = named(tmp_path, {"brothers": "- Both example brothers are older.\n", "glass": "- The example glass is blue.\n"})
    assert cites(idx.match("what does my brother do?")) == cites(idx.match("my brothers")) == ["facts/brothers.md:1"]
    assert [mentions.tokens(w, True) for w in ("brothers", "class", "this", "kettle's")] == [["brother"], ["class"], ["this"], ["kettle"]]
    assert mentions.tokens("dates") == ["dates"]                  # a fact file's name (memory.remember) keeps its s


def test_one_word_that_names_nothing_is_never_a_key_whatever_file_heading_or_line_it_heads(tmp_path):
    idx = named(tmp_path, {"dates": "- An example date line.\n",
                           "people": "- Who: Examplia runs it.\n- Back: an example ache, mid left.\n- August: Examplia's birthday, August 1.\n"
                                     "- Watch: an example watch.\n- Back hurt: an example ache.\n- Next car: an example coupe.\n"
                                     "- Phone: an example phone.\n- Examplia: the eldest.\n## Next\n- Examplia's example step.\n"})
    keys = {" ".join(mentions.tokens(e["key"], True)) for e in idx.entries}
    assert not keys & {"who", "back", "august", "watch", "next", "date"}            # a stop word, the calendar, an everyday word
    assert idx.match("did they get back to me? who knows, next august, watch this, up to date") == []
    assert cites(idx.match("where does my back hurt")) == ["facts/people.md:5"]      # a longer key keeps working
    assert cites(idx.match("the next car")) == ["facts/people.md:6"] and cites(idx.match("my phone")) == ["facts/people.md:7"]
    assert cites(idx.match("examplia", limit=20)) == ["facts/people.md:8", "facts/people.md:1", "facts/people.md:3",
                                                      "facts/people.md:9", "facts/people.md:10"]    # under the name it writes
    assert [mentions.key_of(w) for w in ("Tomorrow", "Lives", "Runs", "Greta")] == [None, None, None, "Greta"]


def test_the_own_file_order_is_the_order_that_survives_the_recall_trim(tmp_path):
    idx = named(tmp_path, {"aaa": "".join(f"- Examplia: other line {i}, {'y' * 260}\n" for i in range(3)),
                           "examplia": "".join(f"- Examplia: own line {i}, {'x' * 262}\n" for i in range(3))})
    text = hooks.render_recall(idx.match("examplia"), [], 1500)        # six lines sized so a 1,500 part keeps four
    assert [ln.rsplit(" (", 1)[1] for ln in text.splitlines()[1:]] == ["facts/examplia.md:1)", "facts/examplia.md:2)",
                                                                      "facts/examplia.md:3)", "facts/aaa.md:1)"]


def test_a_marked_line_still_drops_nested_or_named(tmp_path):
    idx = named(tmp_path, {"examplia": "- Examplia: the eldest.\n  - ⛔ Examplia's example secret: never raised.\n"
                                       "- Examplia (ask-only): an example line.\n  - Examplia's example note ⛔\n"})
    assert cites(idx.match("examplia")) == ["facts/examplia.md:1"] and len(idx.entries) == 1


def test_a_line_under_a_marked_bullet_or_heading_never_enters(tmp_path):
    idx = named(tmp_path, {"examplia": "- Examplia: the eldest. ⛔ never said as fact: an example claim.\n"
                                       "  - Examplia's example job: an example firm.\n    - Examplia lives on the example street.\n\n"
                                       "  - Examplia's example bike: blue.\n- Examplia's example car: grey.\n## Ask-only\n"
                                       "- Examplia's example habit.\n### Examplia's example sub\n- Examplia's example note.\n"
                                       "## Examplia\n- Examplia's example hobby.\n"})
    assert {"facts/examplia.md:6", "facts/examplia.md:12"} <= set(cites(idx.match("examplia", limit=20)))
    assert not {f"facts/examplia.md:{n}" for n in (1, 2, 3, 5, 7, 8, 9, 10)} & set(cites(idx.entries))     # children, a blank line, a section
    assert not any(w in e["text"] for e in idx.entries for w in ("example job", "example street", "example bike"))


def test_a_scoped_room_keeps_its_own_copy_when_another_root_holds_the_same_words(tmp_path):
    private, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir()
    for side in ("live", "snap"):
        (tmp_path / side / "memory" / "facts").mkdir(parents=True)
        (tmp_path / side / "memory" / "facts" / "examplia.md").write_text("- Examplia: the same words in both copies.\n")
    roots = {side: str(tmp_path / side / "memory") for side in ("live", "snap")}
    (private / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\nalso = {json.dumps(list(roots.values()))}\n')
    base = (private / "rooms.toml").read_text()
    block = base.split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    (private / "rooms.toml").write_text(base + f"\n[rooms.frozen-room]\n{block}also = {json.dumps([roots['snap']])}\n")
    y = Yerkd(private, state)
    mentions.Builder(y.memory.mentions, y.memory.roots_for_mentions, y.memory.curated, y.memory.mentions_exclude).refresh(y.memory.scope.files())
    got = {room: [c for c in cites(y.recall_for(room, "what about examplia?")[0]) if str(tmp_path) in c] for room in ("frozen-room", "example-room")}
    assert got["frozen-room"] == [f"{roots['snap']}/facts/examplia.md:1"]          # the other root's copy never stands in for it
    assert sorted(got["example-room"]) == sorted(f"{r}/facts/examplia.md:1" for r in roots.values())


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



def test_the_pack_quotes_an_item_with_the_retired_marks_dropped_and_a_colour_key_line_dropped_whole(tmp_path):
    """With [gates] retired_marks set, the room's own earlier lines come back in the pack without them, so the pack never
    shows the retired shape to the room as its own style; with none set, every item is quoted as filed."""
    s = tree(tmp_path)
    day(s, 1, ["- 09:00:00 example-room yerk: we decided the move plan / 1. 🟦 **Van booked.** It comes Saturday / "
               "2. 🟨 **Keys back.** The old keys go back Friday / -# 🟦 me · 🟨 you [m:c1]",
               "- 09:05:00 example-room owner: ☑\ufe0f van paid, ☐ boxes still to buy [m:c2]"])
    marks = ("☐", "☑", "🟦", "🟨")
    text = memory.room_pack(s, "example-room", 6000, NOW, retired=marks)[0]
    assert ("yerk: we decided the move plan / 1. **Van booked.** It comes Saturday / 2. **Keys back.** The old keys go "
            "back Friday (daily/2026-01-19.md:3)") in text
    assert "owner: van paid, boxes still to buy (daily/2026-01-19.md:4)" in text
    assert not any(m in text for m in marks) and "me · you" not in text
    assert "-# 🟦 me · 🟨 you" in memory.room_pack(s, "example-room", 6000, NOW)[0]            # no list: as filed
    for line in ("🟦 = me, 🟨 = you", "☐ open · ☑ done"):
        assert memory.unmarked(line, marks) == "", line
    assert memory.unmarked("☑ shipped", marks) == "shipped"                     # one mark and a word is a status, not a key

# 4. the evidence ledger and the shadow Stop check
def stream(ev: evidence.Evidence, tool: str, ti: dict, result: str, n: int = 1) -> None:
    ev.observe({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{n}", "name": tool,
                                                              "input": ti}]}})
    ev.observe({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                                                         "content": [{"type": "text", "text": result}]}]}})


def test_read_then_quote_logs_nothing_and_quote_without_read_logs_the_figure(tmp_path):
    calls = []
    ev = evidence.Evidence(on_call=calls.append)
    filed = lambda fig: "facts/boiler.md:3" if fig == "1.35" else None  # noqa: E731
    stream(ev, "mcp__yerk__read_memory", {"path": "facts/boiler.md"}, "- The example boiler holds 1.35 bar cold.")
    assert calls[0]["tool"] == "mcp__yerk__read_memory" and calls[0]["target"] == "path=facts/boiler.md"
    assert evidence.check("1.35 bar cold, per facts/boiler.md:3", ev, filed) == []
    ev.reset()
    ev.give("what was that number again?")
    found = evidence.check("1.35 bar", ev, filed)
    assert len(found) == 1 and found[0][0] == "figure" and "1.35" in found[0][1] and "missing read" in found[0][1]


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


# 5. rail run 2: code decides when the room's memory is searched, and the on-hit budget bends per hit (decision 42)
def test_a_question_about_the_past_or_a_name_asked_about_fires_and_ordinary_chat_stays_quiet():
    fire = ["who did i go to lunch with at the example office", "what did i do last friday?", "when did example neighbour move",
            "remember when we went to the example diner", "did we cancel the example subscription?", "so where was that example store",
            "last friday what did we eat", "anything on aug 14?", "how many days did we say the example park needs"]
    quiet = ["what was that", "how was it?", "what did you change", "so why did it fail", "i went to the gym last night",
             "what happened: the room did the job", "continue with what was going on", "see you friday", "who is coming tonight",
             "remember to call the example shop", "can we meet on oct 30"]
    assert [q for q in fire if not words.PAST.search(q)] == [] and [q for q in quiet if words.PAST.search(q)] == []
    assert words.asked("who is Examplia?") == ["Examplia"] and words.asked("what do you know about the example ladder") == ["example ladder"]
    assert words.asked("who is example neighbour bro") == ["example neighbour"] and words.asked("tell me about my brother and his car") == ["brother"]
    assert [words.asked(q) for q in ("who is coming", "who's more senior Ada or Bo?", "tell me about your day", "who is the guy")] == [[]] * 4


def test_the_on_hit_budget_bends_one_name_takes_the_part_several_share_it_and_a_past_search_takes_what_they_leave():
    def card(name: str, n: int, size: int = 240) -> list[dict]:
        return [{"key": name, "text": f"{name} line {i} " + "x" * size, "cite": f"facts/{name}.md:{i}", "named": name} for i in range(n)]
    past = [{"cite": f"daily/2026-01-0{i}.md:3", "text": f"the past hit {i} " + "y" * 194} for i in range(1, 7)]   # six fill 1,500 under
    # the head that says they are records, never instructions
    one = hooks.render_recall(card("ada", 8), [], 1500)
    assert len(one) <= 1500 and one.count("ada line") == 5                        # one name: the whole part
    two = hooks.render_recall(card("ada", 8) + card("bo", 8), [], 1500)
    assert len(two) <= 1500 and two.count("ada line") >= 2 and two.count("bo line") >= 2   # two: a share each, what one leaves passed on
    both = hooks.render_recall(card("ada", 8), [], 1500, past)
    assert len(both) <= 1500 and both.count("ada line") == 1 and both.count("the past hit") == 4 and hooks.HEADS[1] in both   # beside a
    # past-tense search a name brings its card's first line, the search the rest
    assert hooks.render_recall([], [], 1500, past).count("the past hit") == 6       # no name: the past hits take the part
    many = hooks.render_recall([e for k in ("k1", "k2", "k3", "k4", "k5", "k6", "k7", "k8", "k9") for e in card(k, 1, 290)], [], 1500)
    assert [ln.split(" line ")[0][2:] for ln in many.splitlines()[1:]] == ["k1", "k2", "k3", "k4"]     # in the order named, whole
    gone = hooks.render_recall(card("ada", 8), [], 1500, None, ["Zorblaxia"])
    assert gone.splitlines()[0] == "[named, nothing on file: Zorblaxia]" and len(gone) <= 1500     # the absence line first, never cut


# what is due: the list yerk-index writes beside the mention index, with no model (due.py)
FILED_IN = "(example-room#rid, 2026-01-02 09:00:00+00:00)"


def fact(s: memory.Settings, name: str, *lines: str) -> Path:
    (f := s.root / "facts" / f"{name}.md").write_text(f"# {name}\n\n" + "\n".join(lines) + "\n")
    return f


def note(i: int, body: str, cls: str = "private", rid: str = "rid", role: str = "note") -> str:
    return f"- 09:00:{i:02d}+00:00 example-room#{rid} {role}: {body} [c:{cls}] [m:m-{i}.d{i:08x}]"


def listing(s: memory.Settings, b: due.Builder | None = None, now: datetime = NOW) -> list[dict]:
    (b or due.Builder(s)).refresh(s.scope.files(), now)
    return json.loads(s.due.read_text())["items"]


def test_the_due_list_holds_each_open_dated_line_with_its_date_words_cite_class_room_and_when_it_was_filed(tmp_path):
    s = tree(tmp_path)
    fact(s, "boiler", f"- Example boiler: the yearly check. (supersedes memory/facts/boiler.md:9) {FILED_IN} [due 2026-01-25] [m:m-1]",
         f"- Example boiler: in the cellar. {FILED_IN} [m:m-2]")
    (s.root / "facts" / "club.md").write_text(f"---\nclass: room\nroom: her-id\n---\n- Example club: pay the fee {FILED_IN} [due 2026-01-30T18:00 owes Dana] [m:m-3]\n")
    day(s, 3, [note(1, "Send the example form back [due 2026-01-19]"), "- 09:00:02+00:00 example-room#rid owner: hello [c:private] [m:1001]",
               note(3, 'Dana: "I will bring the key" (from m-9) [due 2026-01-22 owes Dana]', "room", "her-id", "said")])
    got = {x["cite"]: x for x in listing(s)}
    assert oct(s.due.stat().st_mode & 0o777) == "0o600" and s.due.parent == s.mentions.parent
    assert list(got) == ["memory/daily/2026-01-17.md:3", "memory/daily/2026-01-17.md:5", "memory/facts/boiler.md:3", "memory/facts/club.md:5"]
    keys = ("date", "time", "owes", "text", "role", "class", "room", "filed")
    assert [tuple(x[k] for k in keys) for x in got.values()] == [
        ("2026-01-19", "", "", "Send the example form back", "note", "private", "rid", "2026-01-17T09:00:01+00:00"),
        ("2026-01-22", "", "Dana", 'Dana: "I will bring the key" (from m-9)', "said", "room", "her-id", "2026-01-17T09:00:03+00:00"),
        ("2026-01-25", "", "", "Example boiler: the yearly check.", "note", "private", "", "2026-01-02T09:00:00+00:00"),
        ("2026-01-30", "18:00", "Dana", "Example club: pay the fee", "note", "room", "her-id", "2026-01-02T09:00:00+00:00")]
    for x in got.values():                                          # each cite opens on its own line
        assert memory.read(s, x["cite"], lines=1)["text"].replace("[the room's note, not their words] ", "").replace("[their words] ", "") == x["raw"]


def test_a_correction_takes_a_dated_line_off_the_list_by_its_mark_or_by_the_later_map(tmp_path):
    y = daemon_of(tmp_path, "example-room", "family-room")
    y.daily.rooms["example-room"] = ("discord:CHANNEL_ID", "shared")                  # its lines every room's, its own to mark
    s, b = y.memory, due.Builder(y.memory)

    def open_() -> list[str]:
        return [x["cite"] for x in listing(s, b, datetime.now(UTC))]
    when = (datetime.now(UTC).astimezone(s.tz) + timedelta(days=2)).date().isoformat()
    fact_, mine, shared = (asyncio.run(y.tool_remember("example-room", t, about, [], due=when)) for t, about in (
        ("Example boiler: the yearly check.", "example boiler"), ("Send the example form back.", ""), ("Bring the example ladder back.", "")))
    hers = asyncio.run(y.tool_remember("family-room", "Pay the example fee.", "", [], due=when))
    assert sorted(open_()) == sorted([fact_["cite"], mine["cite"], shared["cite"], hers["cite"]])
    asyncio.run(y.tool_remember("example-room", "Example boiler: checked.", "example boiler", [fact_["cite"], mine["cite"]]))
    assert sorted(open_()) == sorted([shared["cite"], hers["cite"]])                  # marked superseded where they stand
    asyncio.run(y.tool_remember("example-room", "The fee is paid, from the surprise fund.", "surprise fund", [hers["cite"]]))
    assert open_() == [shared["cite"]]                                                # a bare [superseded], its replacement in LATER
    named = asyncio.run(y.tool_remember("family-room", "The example ladder is back.", "", [shared["cite"]]))
    assert named["superseded"] == [] and memory.later(s)[shared["cite"]] == named["cite"]
    assert memory.mentions.OLD not in memory.read(s, shared["cite"], lines=1)["text"]           # named, never marked: not her line
    assert open_() == []


def test_a_dated_line_stays_fourteen_days_past_its_date_and_leaves_the_day_after_in_the_list_and_as_it_is_read(tmp_path):
    s = tree(tmp_path)
    at = {n: (NOW - timedelta(days=n)).date().isoformat() for n in (13, 14, 15)}
    fact(s, "boiler", *(f"- Example check {n}. {FILED_IN} [due {d}] [m:m-{n}]" for n, d in at.items()))
    assert [x["date"] for x in listing(s)] == [at[13], at[14]]
    items = due.Index(s.due).load()
    assert [x["date"] for x in due.listed(s, items, now=NOW)] == [at[14], at[13]]               # overdue, the oldest on top
    assert [x["date"] for x in due.listed(s, items, now=NOW + timedelta(days=1))] == [at[13]]   # with no rebuild between
    assert due.listed(s, items, now=NOW + timedelta(days=2)) == []


def test_the_characters_of_a_mark_in_a_lines_own_words_put_nothing_on_the_list(tmp_path):
    s = tree(tmp_path)
    mark = "[due 2026-01-25]"
    fact(s, "boiler", f"- Example boiler: {mark} is its mark. {FILED_IN} [m:m-1]", f"- Example boiler, ends in {mark} {FILED_IN} [m:m-2]",
         f"- Example boiler, by hand {mark}", f"- Example boiler, copied {FILED_IN} {mark} [m:m-3] {FILED_IN} [m:m-4]")
    day(s, 1, [f"- 09:00:01+00:00 example-room#rid owner: do it {mark} [c:private] [m:1001]", note(2, f"a note with {mark} in it"),
               f"- 09:00:03+00:00 example-room#rid note: ends in {mark} [c:private] [m:m-3.n0000000a]",
               f"- 09:00:04+00:00 example-room#rid yerk: ends in {mark} [c:private] [m:m-4.d0000000a]", f"a hand edit {mark}"])
    assert listing(s) == []


def test_the_list_is_yerks_own_fact_files_and_day_logs_never_another_folder_or_root(tmp_path):
    (other := tmp_path / "other" / "memory").mkdir(parents=True)
    (other / "facts").mkdir()
    line = f"- Example boiler: the yearly check. {FILED_IN} [due 2026-01-25] [m:m-1]"
    (other / "facts" / "boiler.md").write_text(line + "\n")
    (other / "2026-01-17.md").write_text(note(1, "Send it [due 2026-01-25]") + "\n")
    s0 = tree(tmp_path)
    s = memory.settings(tmp_path / "private", tmp_path / "state", {"timezone": "UTC", "also": [str(other)]})
    for sub in ("facts/old", "topics", "daily/old"):
        (s0.root / sub).mkdir(parents=True)
        (s0.root / sub / "2026-01-17.md").write_text(line + "\n" + note(1, "Send it [due 2026-01-25]") + "\n")
    (s0.daily / "notes.md").write_text(note(1, "Send it [due 2026-01-25]") + "\n")
    fact(s, "boiler", line)
    assert [x["cite"] for x in listing(s)] == ["memory/facts/boiler.md:3"]               # its copy in a read-only root lists nothing


def test_a_rebuild_after_one_file_changed_reads_that_file_alone_and_writes_only_a_list_that_changed(tmp_path, monkeypatch):
    s = tree(tmp_path)
    for n in range(1, 8):
        day(s, n, [note(n, f"Example thing {n} [due 2026-01-2{n}]")])
    fact(s, "boiler", f"- Example boiler: the yearly check. {FILED_IN} [due 2026-01-25] [m:m-1]")
    reads: list[str] = []
    real = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (reads.append(self.name) if self.suffix == ".md" else None, real(self, *a, **k))[1])
    b = due.Builder(s)
    assert b.refresh(s.scope.files(), NOW) == 8 and len(reads) == b.read == 11            # the first: every day log and fact file
    reads.clear()
    assert b.refresh(s.scope.files(), NOW) is None and reads == []                        # nothing moved
    written = s.due.stat().st_mtime_ns
    today = s.daily / "2026-01-19.md"
    today.write_text(real(today) + "- 09:01:00+00:00 example-room#rid owner: hello [c:private] [m:1001]\n")
    assert b.refresh(s.scope.files(), NOW) == 8 and reads == ["2026-01-19.md"] and b.read == 1
    assert s.due.stat().st_mtime_ns == written                                            # the same list: not written again
    reads.clear()
    today.write_text(real(today) + note(9, "Example thing 9 [due 2026-01-29]") + "\n")
    assert b.refresh(s.scope.files(), NOW) == 9 and reads == ["2026-01-19.md"]
    assert len(json.loads(real(s.due))["items"]) == 9
    reads.clear()
    (s.daily / "2026-01-18.md").unlink()                                                  # a file gone takes its lines with it
    assert b.refresh(s.scope.files(), NOW) == 8 and reads == []
    (s.root / memory.LATER).write_text("memory/facts/boiler.md:3\tmemory/facts/boiler.md:4\n")    # LATER alone moved: no file read
    assert b.refresh(s.scope.files(), NOW) == 7 and reads == []


def test_a_file_or_a_later_map_that_cannot_be_read_is_unknown_never_empty_and_a_broken_list_stops_nothing(tmp_path, monkeypatch, caplog):
    s = tree(tmp_path)
    f = fact(s, "boiler", f"- Example boiler: the yearly check. {FILED_IN} [due 2026-01-25] [m:m-1]")
    day(s, 1, [note(1, "Send the example form back [due 2026-01-25]")])
    b, real, down = due.Builder(s), due.items_of, {f}

    def items_of(path: Path, name: str, day: str = "") -> list[dict[str, str]]:
        if path in down:
            raise OSError("busy")
        return real(path, name, day)
    monkeypatch.setattr(due, "items_of", items_of)
    assert b.refresh(s.scope.files(), NOW) == 1
    down.clear()
    assert b.refresh(s.scope.files(), NOW) == 2                                           # tried again though no file moved
    (s.root / memory.LATER).write_bytes(b"memory/facts/boiler.md:3\t\xff\xfe\n")
    assert b.refresh(s.scope.files(), NOW) is None and len(json.loads(s.due.read_text())["items"]) == 2 and memory.LATER in caplog.text
    assert b.refresh({}, NOW) is None and len(json.loads(s.due.read_text())["items"]) == 2          # a pass that saw no disk
    idx = due.Index(s.due)
    assert len(idx.load()) == 2
    for bad in ("{", "[]", '{"items": 3}', '{"x": []}'):
        s.due.write_text(bad)
        assert idx.load() == [], bad
    s.due.unlink()
    assert idx.load() == [] == due.Index(tmp_path / "none" / "due.json").load()


def test_yerk_index_writes_the_list_at_a_pass_and_wakes_on_the_later_map(tmp_path, monkeypatch):
    s = tree(tmp_path)
    (s.root.parent / "yerkd.toml").write_text('[memory]\nroot = "memory"\ntimezone = "UTC"\n')
    when = datetime.now(UTC).date().isoformat()
    fact(s, "boiler", f"- Example boiler: the yearly check. {FILED_IN} [due {when}] [m:m-1]")
    woken: list[tuple[str, ...]] = []
    real = memindex.Wake
    monkeypatch.setattr(memindex, "Wake", lambda names=(): (woken.append(names), real(names))[1])
    monkeypatch.setattr(signal, "signal", lambda *a: None)              # this process's own, left as they are
    assert memindex.main(["--once", "--private", str(s.root.parent), "--state", str(tmp_path / "state")]) == 0
    assert [x["cite"] for x in json.loads(s.due.read_text())["items"]] == ["memory/facts/boiler.md:3"] and memory.LATER in woken[0]
