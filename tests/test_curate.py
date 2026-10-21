"""Checks `yerk curate` (curate.py), the nightly pass over the cards, the memory's fact files: a sweep blanks stale lines and a promotion adds new
ones from each day's sources. Its cases, layer 11: the pack filter's four bugs from the Oct 20 backfill (a mark on a heading covers what it holds,
an emoji before SUPERSEDED still supersedes, names in any case, relation words find a person's lines); the wall by class (a card in a person's
room's class gets nothing from the owner's private sources, and her room never reads a promoted private line); the sweep (repeats, superseded and
past-date lines blank, a cite moved only on one exact match, every other drift a finding, a backup first); the promotion's gates (a worker that
the knock-down, shape, head, secret, repeat, every cite a pack line, the bench); `--since`; a dry run; the morning line."""
import json
import shutil
from datetime import datetime, timezone

import pytest

from yerk_ai import curate, memindex, memory, morning, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger

HER = "discord:200"
NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()


def said(day, i, body, room="owner-room", rid="discord:100", role="owner", cls="private"):
    at = datetime(2026, 10, day, 9, 0, i, tzinfo=timezone.utc)
    return memory.format_line(at, room, rid, role, "", body, cls, f"m{day}-{i}")


@pytest.fixture
def layer(tmp_path):
    """The example layer with a day log for Oct 19 and Oct 20 (the owner's room and her room), a box notes folder as an
    `also` root, a texting log as a [curate] log, the owner's card on Ada (private) and her room's card (class room)."""
    priv, state, box = tmp_path / "private", tmp_path / "state", tmp_path / "box" / "memory"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    shutil.rmtree(priv / "memory" / "daily")
    (daily := priv / "memory" / "daily").mkdir()
    (daily / "2026-10-19.md").write_text("\n".join(["# 2026-10-19", "", said(19, 1, "my mom Ada moved to Exampleton"),
                                                   said(19, 2, "MOM likes green tea, she said \"I only drink green tea now\""),
                                                   said(19, 3, "hi from her room", "her-room", HER, "person", "room"), ""]))
    (daily / "2026-10-20.md").write_text("\n".join(["# 2026-10-20", "", said(20, 1, "Ada starts her new job at Example Co on Monday"),
                                                   said(20, 2, "my mom wants the blue car", "her-room", HER, "person", "room"),
                                                   said(20, 3, "Ada's password is hunter22"), said(20, 4, "Ada says Bea is home"), ""]))
    box.mkdir(parents=True)
    (box / "2026-10-20.md").write_text("\n".join(["# Oct 20", "## ⛔ not confirmed", "- Ada might move to Mars",
                                                 "### what Ada said", "- Ada is buying a boat", "## 10:00 the day",
                                                 "- 🔴 SUPERSEDED: Ada's birthday is in May", "- Ada's birthday is June 2",
                                                 "- Do not state: Ada's old address", ""]))
    (tmp_path / "texts.log").write_text("2026-10-19 08:00 Ada IN: \"home by six\"\n2026-10-20 18:00 Ada IN: \"Bea passed her exam\"\n")
    (priv / "yerkd.toml").write_text(f'[memory]\ntimezone = "UTC"\nalso = ["{box}"]\n\n[curate]\nlogs = ["{tmp_path / "texts.log"}"]\n'
                                     'index_wait = 2\n')
    (facts := priv / "memory" / "facts").mkdir()
    (facts / "ada.md").write_text("# Ada\n- Ada Example: the owner's mom, a teacher (src: memory/daily/2026-10-19.md:3)\n")
    (facts / "ada-for-her.md").write_text(f"---\nclass: room\nroom: {HER}\n---\n# Ada\n"
                                          "- Ada Example: the owner's mom (src: memory/daily/2026-10-19.md:5)\n")
    state.mkdir()
    return priv, state, memory.settings(priv, state, {"timezone": "UTC", "also": [str(box)]}), tmp_path / "texts.log"


def test_a_mark_on_a_heading_covers_what_it_holds_and_an_emoji_before_superseded_still_supersedes():
    rows = ["# Oct 20", "## ⛔ not confirmed", "- a", "### under it", "- b", "## Do NOT state: rumours", "- c", "## the day",
            "- d", "- ⛔ e", "  - under e", "- f 🔴 SUPERSEDED by :12", "- g ⟵ SUPERSEDED", "- h [superseded by x.md:3]",
            "- i (supersedes x.md:2)", "superseded: j"]
    assert [x for x, k in zip(rows, curate.kept(rows)) if k] == ["# Oct 20", "## the day", "- d", "- i (supersedes x.md:2)"]


@pytest.mark.parametrize("first, hits, misses", [
    ("- Ada Example: Owner's mom, lives in Exampleton", ["my mom said", "MOM called", "his Mother", "ada example", "Ada's car"],
     ["momentum", "Adam"]),
    ("- Bob Example: Owner's dad, retired", ["Dad", "my father", "his dad's truck"], ["daddy long legs"]),
    ("- Nan: Owner's grandma, in Example Bay", ["grandma", "his GRANDMOTHER"], ["grand"]),
    ("- Bea: Owner's sister, at school", ["my sister", "his sister's exam", "Bea"], ["the sister city"]),
    ("- Cal: Owner's oldest brother, in Example City", ["my oldest brother", "his oldest-brother"],
     ["my second-oldest brother", "my second oldest brother"]),
    ("- Dan: Owner's second-oldest brother", ["my second oldest brother", "his second-oldest brother", "Dan"], ["my oldest brother"]),
])
def test_relation_words_find_a_persons_lines_in_any_case(tmp_path, first, hits, misses):
    pat = curate.names(tmp_path / "x.md", f"# {first[2:].split(':')[0]}\n{first} (src: a.md:1)\n")
    assert all(pat.search(h) for h in hits) and not any(pat.search(m) for m in misses)


def test_a_card_in_her_rooms_class_gets_nothing_from_his_private_sources(layer):
    priv, state, s, log = layer
    pk, said_as = curate.packs(s, priv, [log.resolve()], [datetime(2026, 10, d).date() for d in (19, 20)])
    hers = [x for _, _, x in pk[s.root / "facts" / "ada-for-her.md"]]
    his = [x for _, _, x in pk[s.root / "facts" / "ada.md"]]
    assert hers == [said(20, 2, "my mom wants the blue car", "her-room", HER, "person", "room")]
    assert "- Ada's birthday is June 2" in his and any("Bea passed" in x for x in his) and any("new job" in x for x in his)
    assert not any(w in x for x in his for w in ("Mars", "boat", "May", "old address"))      # marked, under a mark, superseded
    assert not any("moved to Exampleton" in x for x in his)                                     # the card cites it already
    assert his.index("- Ada's birthday is June 2") < [i for i, x in enumerate(his) if "green tea" in x][0]    # newest day first
    (snap := priv.parent / "snapshot" / "memory").mkdir(parents=True)              # a copy of a root, its cites another file's
    (snap / "2026-10-20.md").write_text("# Oct 20\n- Ada's birthday is June 2\n- Ada said the snapshot only line\n")
    s2 = memory.settings(priv, state, {"timezone": "UTC", "also": [str(s.scope.roots[1]), str(snap)]})
    assert not any("snapshot only" in x for _, _, x in curate.packs(s2, priv, [], [datetime(2026, 10, 20).date()])[0][s.root / "facts" / "ada.md"])
    (s.root / "facts" / "pets.md").write_text("# Pets\n- Pets: the owner's animals (src: x.md:1)\n- Zed: a tabby (src: x.md:1)\n"
                                              "- Back: a common word (src: x.md:1)\n")
    box = s.scope.roots[1] / "2026-10-20.md"
    box.write_text(box.read_text() + "- the vet saw Zed today\n- back home by six\n")
    pk, said_as = curate.packs(s, priv, [], [datetime(2026, 10, 20).date()])
    assert [x for _, _, x in pk[s.root / "facts" / "pets.md"]] == ["- the vet saw Zed today"] and "back" not in said_as


def test_a_curate_log_never_feeds_her_card_whatever_its_class_and_her_recall_never_finds_a_promoted_line(layer, monkeypatch):
    priv, state, s, log = layer
    (log.parent / f"{log.name}.class").write_text("shared\n")                 # her recall never reads it: it is outside the scope
    pk, _ = curate.packs(s, priv, [log.resolve()], [datetime(2026, 10, d).date() for d in (19, 20)])
    assert not any("Ada IN" in x for _, _, x in pk[s.root / "facts" / "ada-for-her.md"])
    assert any("Ada IN" in x for _, _, x in pk[s.root / "facts" / "ada.md"])
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    mine = proposals(s)[0]
    assert curate.run(priv, state, now=NOW, model=fake([mine, f"KEEP {mine}"], []))["landed"] == [mine]
    (state / "memory").mkdir(exist_ok=True)
    memindex.Index(s.index_db, s.scope).sync()
    hits = memory.recall(s, "Example Co new job Monday", reader=memory.Reader(name="her-room", rid=HER)).get("hits", [])
    assert not any("facts/ada.md" in h.get("cite", "") for h in hits)


def fake(answers, calls):
    def model(prompt, tools):
        calls.append((prompt, tools))
        return answers[len(calls) - 1] if len(calls) <= len(answers) else ""
    return model


def proposals(s):
    day = "memory/daily/2026-10-20.md"
    return [f"ada.md: - Ada's new job: starts at Example Co on Monday (src: {day}:3)",
            f"ada.md: - Back: starts at Example Co (src: {day}:3)",
            f"ada.md: - Ada's login: her password is hunter22 (src: {day}:3)",
            f"ada.md: - Ada's tea: green tea only — she said so (src: memory/daily/2026-10-19.md:4)",
            "ada.md: - Ada's cousin: lives in Example Bay (src: memory/daily/2026-10-19.md:1)",
            "ada.md: - Bea: Ada's daughter passed her exam (src: texts.log:2)",
            f"ada.md: - Ada's tea: green tea only (src: memory/daily/2026-10-19.md:4)"]


def test_the_promotion_lands_only_what_the_knockdown_passed_and_the_checks_hold(layer, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    (state / "memory").mkdir()
    index = memindex.Index(s.index_db, s.scope)
    index.sync()
    monkeypatch.setattr(curate.time, "sleep", lambda n: index.sync())      # yerk-index's next pass takes the card in
    lines = [x.replace("texts.log:2", f"{log}:2") for x in proposals(s)]
    keep = "\n".join(f"KEEP {x}" for x in lines[:-1]) + f"\nDROP {lines[-1]} | rule 5: the same fact twice"
    calls: list = []
    r = curate.run(priv, state, now=NOW, model=fake(["\n".join(lines) + "\nada.md fix 2: - Ada Example: the owner's mom, retired (src: x.md:1)",
                                                   keep], calls))
    assert [t for _, t in calls] == ["", "Read,Grep,Glob", ""]              # the proposer reads the pack alone
    assert "=== pack ada.md" in calls[0][0] and "ada-for-her.md" not in calls[0][0]       # one reader to a call
    assert "=== pack ada-for-her.md" in calls[2][0] and "ada.md" not in calls[2][0].replace("ada-for-her.md", "")
    assert r["landed"] == [lines[0], lines[5]]
    why = {x.split(" | ")[0]: x.split(" | ")[1] for x in r["checked"]}
    assert "one common word" in why[lines[1]] and "secret" in why[lines[2]] and "300 characters" in why[lines[3]]
    assert "not a line of this card's pack" in why[lines[4]]
    assert r["dropped"] == [f"{lines[-1]} | rule 5: the same fact twice"] and r["corrections"][0].startswith("ada.md:2 ")
    card = (s.root / "facts" / "ada.md").read_text()
    assert card.endswith(lines[0].split(": ", 1)[1] + "\n" + lines[5].split(": ", 1)[1] + "\n") and "retired" not in card
    assert len(list((priv / "backups" / "curate-2026-10-21").iterdir())) == 1
    assert json.loads((state / "curate.json").read_text())["landed"] == r["landed"]


def test_a_night_lands_at_most_its_cap_a_card_and_a_pinned_account_binds(layer, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    (priv / "yerkd.toml").write_text((priv / "yerkd.toml").read_text() + "per_card = 1\n")
    lines = [proposals(s)[0], proposals(s)[5].replace("texts.log:2", f"{log}:2")]
    r = curate.run(priv, state, now=NOW, model=fake(["\n".join(lines), "\n".join(f"KEEP {x}" for x in lines)], []))
    assert r["landed"] == [lines[0]] and r["checked"] == [f"{lines[1]} | held: over the night's cap, 1 a card and 25 in all"]
    monkeypatch.setattr(curate, "busy", lambda state: {"two"})
    with pytest.raises(RuntimeError, match="account two has a worker running"):
        curate.ask(priv, state, {"account": "two"}, "p", "")              # never the next account behind the owner's back
    with pytest.raises(RuntimeError, match="not in accounts.toml"):
        curate.ask(priv, state, {"account": "five"}, "p", "")


def test_her_room_never_reads_a_promoted_private_line_and_her_card_takes_none(layer, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    mine ="ada.md: - Ada's new job: starts at Example Co on Monday (src: memory/daily/2026-10-20.md:3)"
    hers = "ada-for-her.md: - Ada's new job: starts at Example Co on Monday (src: memory/daily/2026-10-20.md:3)"
    calls: list = []
    r = curate.run(priv, state, now=NOW, model=fake([mine, f"KEEP {mine}", hers, f"KEEP {hers}"], calls))
    assert len(calls) == 4 and not any("=== pack ada.md" in p and "=== pack ada-for-her.md" in p for p, _ in calls)
    assert r["landed"] == [mine] and r["checked"] == [f"{hers} | its cite memory/daily/2026-10-20.md:3 is not a line of this card's pack"]
    her = memory.Reader(name="her-room", rid=HER)
    assert not memory.read(s, "facts/ada.md", reader=her)["ok"] and memory.read(s, "facts/ada-for-her.md", reader=her)["ok"]
    assert "new job" not in (s.root / "facts" / "ada-for-her.md").read_text()


def test_the_code_check_stops_a_code_an_id_and_a_bank_number_and_passes_a_phone_number(layer):
    priv, state, s, log = layer
    card, pack = s.root / "facts" / "ada.md", [((s.daily / "2026-10-20.md").resolve(), 3, "x")]
    for fact, secret in [("the gate code: 4471 at the side door", True), ("her SIN is 123 456 789", True), ("the garage PIN 0420", True),
                         ("transit 12345, account 1234567", True), ("the hotel, +34 911 222 333", False)]:
        why = curate.check(s, priv, [], card, f"- Ada's thing: {fact} (src: memory/daily/2026-10-20.md:3)", pack, {"ada's thing"})
        assert ("secret" in why) == secret and (secret or why == ""), (fact, why)


@pytest.mark.parametrize("after, index, why", [({"top_k": 10, "top_1": 4}, True, "the bench fell from 10, 5 to 10, 4"),
                                               ({"top_k": 11, "top_1": 5}, False, "the index did not take the lines in")])
def test_a_bench_that_falls_or_an_index_that_never_takes_the_lines_in_lands_nothing(layer, monkeypatch, after, index, why):
    priv, state, s, log = layer
    runs = iter([{"top_k": 10, "top_1": 5, "at": "a", "seconds": 1.0}, {**after, "at": "b", "seconds": 1.0}])
    monkeypatch.setattr(curate, "bench", lambda s: next(runs))
    monkeypatch.setattr(curate, "taken", lambda s, cards: index)
    monkeypatch.setattr(curate.time, "sleep", lambda n: None)
    line = proposals(s)[0]
    r = curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line}"], []))
    assert r["landed"] == [] and r["checked"] == [f"nothing landed: {why}"]
    rows = (s.root / "facts" / "ada.md").read_text().split("\n")
    assert rows[2] == "" and "new job" not in "".join(rows)                  # blanked, so a line filed since keeps its number



def test_the_proposer_runs_with_no_tools_and_the_knockdown_with_read_grep_and_glob_only(layer, monkeypatch):
    import subprocess
    priv, state, s, log = layer
    seen: list = []
    monkeypatch.setattr(curate.subprocess, "run", lambda argv, **k: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "ok", ""))
    assert curate.ask(priv, state, {}, "p", "") == "ok" and curate.ask(priv, state, {}, "p", "Read,Grep,Glob") == "ok"
    for argv, tools in zip(seen, ("", "Read,Grep,Glob")):
        assert argv[argv.index("--tools") + 1] == tools and argv[argv.index("--disallowedTools") + 1] == "Agent"
        assert argv[argv.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in argv and "--allowedTools" not in argv


def test_a_knockdown_that_rewrites_a_line_lands_neither_and_no_bench_lands_nothing(layer, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    card, line = s.root / "facts" / "ada.md", proposals(s)[0]
    before = card.read_text()
    r = curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line.replace('Monday', 'Tuesday')}"], []))
    assert r["landed"] == [] and r["dropped"] == [f"{line} | the knock-down did not pass it"] and card.read_text() == before
    monkeypatch.setattr(curate, "bench", lambda s: None)
    r = curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line}"], []))
    assert r["landed"] == [] and r["checked"] == ["nothing landed: no bench to hold the lines to"] and card.read_text() == before


def test_a_fall_blanks_only_the_landed_lines_and_a_line_filed_meanwhile_keeps_its_number(layer, monkeypatch):
    priv, state, s, log = layer
    runs = iter([{"top_k": 10, "top_1": 5, "at": "a", "seconds": 1.0}, {"top_k": 9, "top_1": 5, "at": "b", "seconds": 1.0}])
    monkeypatch.setattr(curate, "bench", lambda s: next(runs))
    card = s.root / "facts" / "ada.md"
    before = card.read_text().split("\n")[:-1]

    def meanwhile(s, cards):                         # memory.remember files a line while yerk-index takes the new ones in
        with memory.daily_lock(s.daily):
            card.write_text(card.read_text() + "- Ada's car: a blue one (owner-room, 2026-10-21 12:00:00+00:00) [m:9]\n")
        return True
    monkeypatch.setattr(curate, "taken", meanwhile)
    lines = [proposals(s)[0], proposals(s)[5].replace("texts.log:2", f"{log}:2")]
    r = curate.run(priv, state, now=NOW, model=fake(["\n".join(lines), "\n".join(f"KEEP {x}" for x in lines)], []))
    rows = card.read_text().split("\n")
    assert r["landed"] == [] and rows[:2] == before and rows[2:4] == ["", ""] and rows[4].startswith("- Ada's car: a blue one")


def test_every_card_write_is_a_backed_up_rewrite_under_the_writers_lock(layer, monkeypatch):
    import contextlib
    priv, state, s, log = layer
    held, writes, lock, rewrite = [], [], memory.daily_lock, curate.doctor.rewrite

    @contextlib.contextmanager
    def locked(daily):
        with lock(daily):
            held.append(1)
            try:
                yield
            finally:
                held.pop()
    monkeypatch.setattr(curate.memory, "daily_lock", locked)
    monkeypatch.setattr(curate.doctor, "rewrite", lambda p, b, f, d: writes.append((p.name, bool(held), b.name)) or rewrite(p, b, f, d))
    runs = iter([{"top_k": 10, "top_1": 5, "at": "a", "seconds": 1.0}, {"top_k": 10, "top_1": 4, "at": "b", "seconds": 1.0}])
    monkeypatch.setattr(curate, "bench", lambda s: next(runs))
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    line = proposals(s)[0]
    curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line}"], []))
    assert len(writes) == 6 and all(h for _, h, _ in writes) and {b for *_, b in writes} == {"curate-2026-10-21"}   # 2 cards x 2, land, blank


def test_the_also_roots_and_the_logs_are_never_written(layer, monkeypatch):
    priv, state, s, log = layer
    box = s.scope.roots[1]
    (box / "facts").mkdir()
    (box / "facts" / "ada.md").write_text("# Ada\n- Ada: twice (src: memory/2026-10-20.md:3)\n- Ada: twice (src: memory/2026-10-20.md:3)\n"
                                          "- Ada: 🔴 SUPERSEDED: old (src: memory/2026-10-20.md:5)\n- Ada: away until 2026-10-01 (src: x.md:1)\n")
    files = [p for p in box.rglob("*") if p.is_file()] + [log]
    snap = {p: p.read_bytes() for p in files}
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    line = proposals(s)[0]
    calls: list = []
    r = curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line}"], calls))
    assert r["landed"] == [line] and {p: p.read_bytes() for p in files} == snap and sorted(p for p in box.rglob("*") if p.is_file()) == sorted(files[:-1])
    assert not any("twice" in p for p, _ in calls)                          # a root's card is no card of this tree


def test_the_sweep_blanks_repeats_superseded_and_past_lines_and_moves_a_cite_only_on_one_exact_match(layer, capsys, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "ask", lambda *a: "")
    card = s.root / "facts" / "ada.md"
    day = "memory/daily/2026-10-19.md"
    card.write_text("\n".join(["# Ada", f"- Ada Example: the owner's mom (src: {day}:3)", f"- Ada Example: the owner's mom (src: {day}:3)",
                               "- Ada's old job: a teacher [superseded by facts/ada.md:7]", f"- Ada's trip: away [until 2026-10-01] (src: {day}:3)",
                               f"- Ada's tea: \"I only drink green tea now\" (src: {day}:3)", f"- Ada's town: Exampleton (src: {day}:9)",
                               f"- Ada's drink: \"only drinks coffee now\" (src: {day}:4)",
                               "- Ada's note: \"the owner's mom\" (src: memory/facts/ada.md:7)",     # a name two roots share
                               "- Ada's login: she said “Ada’s password is hunter22” (src: memory/daily/2026-10-20.md:5)",   # curly
                               "- Ada's plan: \"Ada might move to Mars\" (src: memory/2026-10-20.md:2)",         # on a marked line only
                               f"- Ada's two: \"my mom Ada moved to Exampleton\", \"I only drink green tea now\" (src: {day}:3)", ""])) # held
    before = card.read_text()
    assert curate.main(priv, state, dry=True) == 1 and card.read_text() == before
    out = capsys.readouterr().out
    assert "changed: ada.md: blanked lines 3, 4, 5" in out and "changed: ada.md: moved the cites of line 6" in out
    assert "found: ada.md:7 cites memory/daily/2026-10-19.md:9, a blank or a line past its end" in out
    assert "found: ada.md:8 quotes \"only drinks coffee now\", on 0 lines of the files it cites" in out
    assert "found: ada.md:9 cites memory/facts/ada.md:7, its own card" in out and "ada.md:10" not in out
    assert "found: ada.md:11 quotes \"Ada might move to Mars\", on 0 lines of the files it cites" in out
    assert "found: ada.md:12 quotes \"I only drink green tea now\", on 1 lines of the files it cites" in out   # its cite holds the other
    curate.sweep(s, priv, [], priv / "backups" / "b", False, datetime(2026, 10, 21).date())
    rows = card.read_text().split("\n")
    assert rows[2:5] == ["", "", ""] and rows[5].endswith(f"(src: {day}:4)") and len(rows) == len(before.split("\n"))
    assert (priv / "backups" / "b" / "facts.ada.md").read_text() == before


def test_the_sweep_never_blanks_a_standing_line_and_never_moves_a_cite_onto_a_card_or_a_superseded_section(layer, capsys, monkeypatch):
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "ask", lambda *a: "")
    (s.scope.roots[1] / "2026-10-18.md").write_text("# Oct 18\n## 10:00 the old plan, SUPERSEDED\n- Ada said \"we meet at the north gate at noon\"\n")
    (s.root / "facts" / "bea.md").write_text("# Bea\n- Bea: line one\n- Bea said \"the spare key is under the blue pot\"\n")
    day, card = "memory/daily/2026-10-19.md", s.root / "facts" / "ada.md"
    card.write_text("\n".join(["# Ada", f"- Video rule: 🔴 NOT SUPERSEDED, the Sep 6 order still holds (src: {day}:3)",
                               f"- Shift: 7:30 to 4 (the old 7:00 to 3:30 is SUPERSEDED) (src: {day}:3)",
                               f"- Superseded phone: the old number, kept for reference (src: {day}:3)",
                               f"- Lease: the landlord wrote \"yours until 2026-09-30, then month to month\" (src: {day}:3)",
                               f"- Old job: worked at Example Co from 2026-01-05 until 2026-09-11 (src: {day}:3)",
                               f"- Gate: 🔴 SUPERSEDED: the south gate (src: {day}:3)", f"- Trip: away [until 2026-10-01] (src: {day}:3)",
                               "- Spare key: \"the spare key is under the blue pot\" (src: memory/facts/bea.md:2)",
                               "- Meeting: Ada said \"we meet at the north gate at noon\" (src: memory/2026-10-18.md:2)",
                               f"- Car: a 2019 Coupe (src: {day}:3)", f"- Car: a 2019 Coupe. (src: {day}:3)", ""]))
    before = card.read_text().split("\n")
    curate.sweep(s, priv, [], priv / "backups" / "b", False, datetime(2026, 10, 21).date())
    after = card.read_text().split("\n")
    assert after[1:6] == before[1:6] and after[6:8] == ["", ""] and after[8:] == before[8:]    # only the two true marks blank
    assert curate.main(priv, state, dry=True) == 1
    out = capsys.readouterr().out
    assert "found: ada.md:5 says until a date now past" in out and "found: ada.md:6 says until a date now past" in out
    assert "found: ada.md:9 cites memory/facts/bea.md:2, another card of this tree" in out and "moved the cites" not in out
    assert "found: ada.md:10 quotes \"we meet at the north gate at noon\", on 0 lines of the files it cites" in out


def test_since_reads_each_day_to_yesterday_and_the_morning_carries_one_curate_line(layer):
    priv, state, s, log = layer
    accounts, cool, led = load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"), Ledger(state / "ledger.sqlite")
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": NOW - 86400, "units": []}, NOW)
    assert words.line(f"{words.NOT_DONE} curate: unknown, no run in 24h") in lines and "unknown curate" in stamp["findings"]
    calls: list = []
    r = curate.run(priv, state, since="2026-10-19", now=NOW, model=fake(["ada.md fix 2: - Ada Example: retired (src: x.md:1)"], calls))
    assert r["days"] == ["2026-10-19", "2026-10-20"] and "green tea" in calls[0][0] and "new job" in calls[0][0]
    assert r["found"] == [] and r["changed"] == []
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": NOW - 86400, "units": []}, NOW)
    assert words.line(f"{words.NOT_DONE} curate: 1 to read, ada.md:2 - Ada Example: retired (src: x.md:1)") in lines


def test_a_run_cut_off_before_its_bench_is_undone_by_the_next_and_said_in_the_morning_and_two_never_overlap(layer, monkeypatch):
    import os
    import subprocess
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "taken", lambda s, cards: True)
    runs = iter([{"top_k": 10, "top_1": 5, "at": "a", "seconds": 1.0}])
    monkeypatch.setattr(curate, "bench", lambda s: next(runs))           # the second bench never comes: the run is cut off
    card, line = s.root / "facts" / "ada.md", proposals(s)[0]
    before = card.read_text().split("\n")[:-1]
    with pytest.raises(StopIteration):
        curate.run(priv, state, now=NOW, model=fake([line, f"KEEP {line}"], []))
    assert card.read_text().split("\n")[2] == line.split(": ", 1)[1]               # on the card, on trial, no verdict
    dead = subprocess.Popen(["true"])
    dead.wait()
    run = json.loads((state / curate.RUN).read_text())
    (state / curate.RUN).write_text(json.dumps({**run, "pid": dead.pid}))
    assert curate.line(state, NOW + 60)[0] == "curate cut off" and curate.line(state, NOW + 60)[1].endswith("cut off at 08:00, 1 to undo")
    (state / curate.RUN).write_text(json.dumps({**run, "pid": os.getppid()}))   # a run still on: never undone, never overlapped
    assert "running since" in curate.line(state, NOW + 60)[1] and curate.main(priv, state) == 2
    (state / curate.RUN).write_text(json.dumps({**run, "pid": dead.pid}))
    r = curate.run(priv, state, now=NOW + 3600, model=fake([], []))
    rows = card.read_text().split("\n")
    assert rows[:2] == before and rows[2] == "" and r["checked"][0].startswith("nothing landed: the run of ")
    assert not (state / curate.RUN).exists() and curate.line(state, NOW + 3700)[0] != "curate cut off"


def test_a_blind_bench_holds_nothing(layer, monkeypatch):
    priv, state, s, log = layer
    s.bench.parent.mkdir(parents=True, exist_ok=True)
    s.bench.write_text("{}") if not s.bench.is_file() else None
    monkeypatch.setattr(curate.memory, "bench", lambda s: {"ok": True, "top_k": 10, "top_1": 5, "blind": ["control-1"]})
    assert curate.bench(s) is None
    monkeypatch.setattr(curate.memory, "bench", lambda s: {"ok": True, "top_k": 10, "top_1": 5, "blind": []})
    assert curate.bench(s)["top_k"] == 10
