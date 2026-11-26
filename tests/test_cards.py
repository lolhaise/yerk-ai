"""Checks that two cards for one person, two fact files in the memory, become one. The cases: a card is live or emptied (memory.live), an
emptied card may say where it moved (`# moved to: <name>`, memory.moves) and then answers by its names as its target, `yerk cards merge A B`
moves one card's live lines onto another's end and leaves it moved, recall's walk goes on through what the merge moved, deep recall hands no
matter line and no card title, and `yerk cards near` lists the cards sharing a first word. Every name here is made up."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

from yerk_ai import cards, cli, curate, due, memindex, memory, mentions, words
from test_card_aka import NEXT, build, fresh, seen, sync, write
from test_card_aka import cards as put
from test_remember import daemon, remember
from test_room_scope import install

TODAY = date(2026, 11, 25)
LIVE = "- Orla Vantree: keeps the example lighthouse. (example-room, 2026-11-20 10:00:00-05:00) [m:m-1]"


def test_a_card_is_live_by_one_line_that_is_no_blank_heading_superseded_or_ended_line_and_emptied_else():
    for text in ("# Orla\n\n\n", "# Orla\n\n- Orla: rows. [superseded by tree/facts/orla-vantree.md:4] [m:m-1]\n",
                 "# Orla\n\n- Orla: rows the ferry [until 2026-11-24]\n", "# Orla\n\n## Boats\n### Old\n#\n", "---\naka: Orly\n---\n# Orla\n", ""):
        assert not memory.live(text, TODAY), text
    for text in ("# Orla\n\n" + LIVE + "\n", "# Orla\n\nmoved to orla vantree\n", "# Orla\n\n- Orla: rows the ferry [until 2026-11-25]\n",
                 "---\naka: Orly\n---\nOrla rows.\n", "#Orla rows\n"):
        assert memory.live(text, TODAY), text


def test_remember_on_an_emptied_card_with_no_alias_starts_again_on_its_path_with_near_and_on_a_live_card_files_as_on_main(tmp_path):
    y = daemon(tmp_path)
    empty = "# Orla\n\n- Orla: rowed. [superseded by tree/facts/orla-vantree.md:3] [m:m-0]\n"
    facts = put(y, {"orla.md": empty, "orla-vantree.md": "# Orla Vantree\n\n" + LIVE + "\n"})
    r = remember(y, "example-room", "Orla rowed to the point.", "Orla")
    assert r == {"ok": True, "cite": "tree/facts/orla.md:4", "superseded": [], "refused": [], "not_edited": [], "near": ["orla vantree"],
                 "near_next": words.FILED_NEAR, "next": NEXT}
    assert (facts / "orla.md").read_text().startswith(empty)
    assert remember(y, "example-room", "Orla rowed back.", "Orla") == {"ok": True, "cite": "tree/facts/orla.md:5", "superseded": [],
                                                                      "refused": [], "not_edited": [], "next": NEXT}     # live now: as on main


def test_remember_about_an_aka_whose_own_name_card_is_emptied_files_on_the_card_that_says_it(tmp_path):
    y = fresh(tmp_path, "one")
    facts = put(y, {"addie.md": "# Addie\n\n\n", "ada-example.md": "---\naka: addie\n---\n# Ada Example\n\n" + LIVE + "\n"})
    assert remember(y, "example-room", "Addie paints boats.", "Addie")["cite"] == "tree/facts/ada-example.md:7"
    assert (facts / "addie.md").read_text() == "# Addie\n\n\n"
    y = fresh(tmp_path, "two")                 # the aka card emptied too: neither is a card, the name starts again on its own path
    put(y, {"addie.md": "# Addie\n\n\n", "ada-example.md": "---\naka: addie\n---\n# Ada Example\n\n"})
    assert remember(y, "example-room", "Addie paints boats.", "Addie")["cite"] == "tree/facts/addie.md:4"


ADA = "# Ada Example\n\n- Ada Example: keeps the grey ladder.\n- Ada Example: paints boats on the quay.\n"
ADDIE = "---\naka: Addy Bee\n---\n# Addie\n\n# moved to: Ada Example\n"


def test_a_moved_card_answers_by_its_own_name_and_aka_with_its_targets_list_then_what_else_they_key_its_pointer_no_entry(tmp_path):
    idx = build(tmp_path, {"facts/ada-example.md": ADA, "facts/addie.md": ADDIE, "facts/people.md": "# people\n\n- Addie: on the old ferry list.\n"})
    own = seen(idx, "how is ada example?")
    assert own and {c.rpartition(":")[0] for c, _ in own} == {"facts/ada-example.md"}
    for said_as, named in (("how is addie?", "addie"), ("is Addy Bee home", "addy bee")):
        assert seen(idx, said_as)[:len(own)] == own and {e["named"] for e in idx.match(said_as)} == {named}, said_as
    assert seen(idx, "addie")[len(own):] == [("facts/people.md:3", "Addie: on the old ferry list.")]
    data = json.loads((tmp_path / "tree.json").read_text())
    assert not [e for e in data["entries"] if "moved to" in e["text"] or e["cite"].startswith("facts/addie.md")]


def test_a_pointer_on_a_live_card_to_a_missing_emptied_or_moved_card_by_its_start_longer_or_one_of_two_is_words(tmp_path):
    for i, files in enumerate((
            {"addie.md": "# Addie\n\n- Addie: rows.\n# moved to: ada example\n"}, {"addie.md": "# Addie\n\n# moved to: ada sample\n"},
            {"addie.md": "# Addie\n\n# moved to: bea quay\n", "bea-quay.md": "# Bea Quay\n\n\n"},
            {"addie.md": "# Addie\n\n# moved to: bea quay\n", "bea-quay.md": "# Bea Quay\n\n# moved to: ada example\n"},
            {"addie.md": "# Addie\n\n# moved to: ada\n"}, {"addie.md": "# Addie\n\n# moved to: ada example and more\n"},
            {"addie.md": "# Addie\n\n# moved to: ada example\n# moved to: ada example\n"}, {"addie.md": "# Addie\n\n## moved to: ada example\n"},
            {"addie.md": "# Addie\n\n# moved to: ada example\n", "ada-examples.md": "# Ada Examples\n\n- Ada Examples: two of them.\n"})):
        root = write(tmp_path / str(i), {"facts/ada-example.md": ADA, **{f"facts/{k}": v for k, v in files.items()}})
        assert root / "facts" / "addie.md" not in memory.moves(root, TODAY), files
        idx = build(tmp_path, {"facts/ada-example.md": ADA, **{f"facts/{k}": v for k, v in files.items()}}, f"t{i}")
        assert not [c for c, _ in seen(idx, "addie") if c.startswith("facts/ada-example.md")], files
    root = write(tmp_path / "ok", {"facts/ada-example.md": ADA, "facts/addie.md": "# Addie\n\n# moved to: ADA Examples\n"})
    assert memory.moves(root, TODAY) == {root / "facts" / "addie.md": root / "facts" / "ada-example.md"}      # folded as a name is


def test_remember_about_a_moved_cards_name_or_aka_files_on_its_target_and_near_names_live_cards_a_moved_one_as_its_target_once(tmp_path):
    y = fresh(tmp_path, "one")
    facts = put(y, {"ada-example.md": ADA, "addie.md": ADDIE})
    assert remember(y, "example-room", "Addie rowed to the point.", "Addie")["cite"] == "tree/facts/ada-example.md:5"
    assert remember(y, "example-room", "Addy Bee painted the lamp room.", "addy bee")["cite"] == "tree/facts/ada-example.md:6"
    assert (facts / "addie.md").read_text() == ADDIE
    y = fresh(tmp_path, "two")
    have = {"ada-example.md": ADA, "addie.md": "---\naka: Ada E\n---\n# Addie\n\n# moved to: ada example\n", "ada-fen.md": "# Ada Fen\n\n\n",
            "ada-quay.md": "# Ada Quay\n\n- Ada Quay: the harbour office.\n"}
    put(y, have)
    r = remember(y, "example-room", "Ada Lane is the new pier road.", "Ada Lane")
    assert (r["cite"], r["near"]) == ("tree/facts/ada-lane.md:3", ["ada example", "ada quay"])
    y = fresh(tmp_path, "three")                 # the target emptied: the pointer is words, the name starts on its own path
    put(y, {"ada-example.md": "# Ada Example\n\n\n", "addie.md": ADDIE})
    assert remember(y, "example-room", "Addie rowed to the point.", "Addie")["cite"] == "tree/facts/addie.md:7"


def test_the_promotion_reads_a_target_by_the_names_of_the_cards_moved_to_it_under_the_stop_word_rule(tmp_path):
    root = write(tmp_path / "tree", {"facts/ada-example.md": ADA, "facts/addie.md": ADDIE, "facts/home.md": "# Home\n\n# moved to: ada example\n"})
    card = root / "facts" / "ada-example.md"
    pat, bare = curate.names(card, ADA), curate.names(card, ADA, moved={})
    assert pat.search("saw Addie at the quay") and pat.search("addy bee called") and not pat.search("went home early")
    assert not bare.search("saw Addie at the quay") and not bare.search("addy bee called")
    assert curate.names(root / "facts" / "addie.md", ADDIE).search("Addie") and not curate.names(root / "facts" / "addie.md", ADDIE).search("Ada Example")


def test_a_room_that_may_not_read_the_target_gets_nothing_by_the_moved_cards_name_and_an_owner_room_gets_it(tmp_path):
    y = install(tmp_path, family=True)
    (facts := y.memory.root / "facts").mkdir(exist_ok=True)
    (facts / "orla-vantree.md").write_text("# Orla Vantree\n\n- Orla Vantree: keeps the example lighthouse.\n")
    (facts / "orly.md").write_text("# Orly\n\n# moved to: orla vantree\n")
    (facts / "brim-quell.md").write_text("---\nclass: shared\n---\n# Brim Quell\n\n- Brim Quell: runs the example bakery.\n")
    (facts / "brimmy.md").write_text("---\nclass: shared\n---\n# Brimmy\n\n# moved to: brim quell\n")
    sync(y)
    assert not [e for e in y.recall_for("family-room", "is orly at the lighthouse?")[0] if "lighthouse" in e["text"]]
    assert "Orla Vantree: keeps the example lighthouse." in [e["text"] for e in y.recall_for("example-room", "is orly at the lighthouse?")[0]]
    assert "Brim Quell: runs the example bakery." in [e["text"] for e in y.recall_for("family-room", "did brimmy bake?")[0]]


def test_a_tree_with_no_pointer_followed_builds_the_index_main_builds(tmp_path):
    """Main 63ab7b64's build of this tree, its entries and keys hashed (all but `built`): pointer-shaped words and emptied cards, no move."""
    build(tmp_path, {"MEMORY.md": "# Pinned\n\n## Example Gull\nthe boat, at pier 3\n",
                     "facts/ada-example.md": "# Ada Example\n\n- Ada Example: keeps the grey ladder.\n# moved to: addie\n- Addie: her old name.\n",
                     "facts/addie.md": "# Addie\n\n\n", "facts/bea-quay.md": "# Bea Quay\n\n# moved to: ada sample\n",
                     "facts/orla.md": "---\naka: Orly\n---\n# Orla\n\n- Orla: rows. [superseded by tree/facts/ada-example.md:3] [m:m-1]\n",
                     "facts/people.md": "# people\n\n- Addie: on the ferry list.\n- Orly: the cat.\n## moved to: bea quay\n"})
    data = json.loads((tmp_path / "tree.json").read_text())
    assert hashlib.sha256(json.dumps([data["entries"], data["keys"]]).encode()).hexdigest()[:16] == "3de9505d37303079"


def tree(tmp_path: Path, files: dict[str, str]) -> tuple[memory.Settings, Path]:
    """A made-up private layer whose tree holds `files`, and its settings."""
    private = tmp_path / "private"
    for k, v in {"daily/.keep": "", **files}.items():
        (private / "memory" / k).parent.mkdir(parents=True, exist_ok=True)
        (private / "memory" / k).write_text(v)
    s = memory.settings(private, tmp_path / "state", {"timezone": "America/Toronto"})
    with memory.daily_lock(s.daily):            # its lock file, there on any install a writer ran on
        return s, private


def snap(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def line(n: int, said: str, until: str = "") -> str:
    return f"- {said}{until} (example-room, 2026-11-2{n % 10} 10:00:00-05:00) [m:m-{n}]"


A_LINES = ["# Addie", "", line(1, "Addie: rows the ferry on Sundays."), line(2, "Addie: an old line [superseded by memory/facts/addie.md:5]"),
           line(3, "Addie: likes tea."), line(4, "Addie: sails till spring", " [until 2020-01-01]"), line(5, "Ada Example: keeps the grey ladder."),
           "Addie lives by the quay."]
B_LINES = ["# Ada Example", "", line(5, "Ada Example: keeps the grey ladder."), line(6, "Ada Example: paints boats.")]
MERGE = {"facts/addie.md": "\n".join(A_LINES) + "\n", "facts/ada-example.md": "\n".join(B_LINES) + "\n",
         "facts/addie-b.md": "# Addie B\n\n# moved to: addie\n", "superseded.tsv": "memory/daily/2026-11-01.md:4\tmemory/facts/addie.md:3\n"}


def test_the_merge_plan_writes_nothing_and_go_moves_each_live_line_once_in_order_onto_bs_end_and_leaves_a_moved(tmp_path, capsys):
    s, private = tree(tmp_path, MERGE)
    before = snap(tmp_path)
    assert cards.merge(s, private, "addie", "Ada Example") == 0 and snap(tmp_path) == before
    plan = capsys.readouterr().out
    assert "4 lines move" in plan and "1 of these already" in plan and "lines 5 to 7" in plan and "memory/facts/addie.md:7\tmemory/facts/ada-example.md:3" in plan
    assert cards.merge(s, private, "addie.md", "ada-example", go=True) == 0
    facts, out = s.root / "facts", capsys.readouterr().out
    a, b = (facts / "addie.md").read_text().split("\n")[:-1], (facts / "ada-example.md").read_text().split("\n")[:-1]
    assert b == B_LINES + [A_LINES[2], A_LINES[4], A_LINES[7]]                # appended in order, the held line not again, B's own unmoved
    assert a == ["# Addie", "# moved to: ada example", "", A_LINES[3], "", A_LINES[5], "", ""]      # its count, superseded and ended kept
    assert (s.root / "superseded.tsv").read_text() == MERGE["superseded.tsv"] + "".join(
        f"memory/facts/addie.md:{i}\tmemory/facts/ada-example.md:{m}\n" for i, m in ((3, 5), (5, 6), (7, 3), (8, 7)))
    assert (facts / "addie-b.md").read_text() == "# Addie B\n\n# moved to: ada example\n"
    assert memory.moves(s.root, TODAY) == {facts / "addie.md": facts / "ada-example.md", facts / "addie-b.md": facts / "ada-example.md"}
    backup = private / "backups" / f"cards-merge-{memory.local_day(s).isoformat()}"
    assert str(backup) in out and "lines 5 to 7" in out
    assert {p.name: p.read_text() for p in backup.iterdir()} == {"facts.addie.md": MERGE["facts/addie.md"], "facts.ada-example.md": MERGE[
        "facts/ada-example.md"], "facts.addie-b.md": MERGE["facts/addie-b.md"], "memory.superseded.tsv": MERGE["superseded.tsv"]}
    after = snap(s.root)
    assert cards.merge(s, private, "addie", "ada example", go=True) == 0 and snap(s.root) == after       # run twice: nothing to move
    assert "nothing to move" in capsys.readouterr().out


def test_each_refusal_of_a_merge_writes_nothing_and_says_why_in_one_line(tmp_path, capsys, monkeypatch):
    s, private = tree(tmp_path, {**MERGE, "facts/bea-quay.md": "# Bea Quay\n\n\n", "facts/cal.md": "# Cal\n\n# moved to: ada example\n",
                                 "facts/brim.md": "---\nclass: shared\n---\n# Brim\n\n- Brim: bakes.\n", "facts/MEMORY.md": "# Notes\n\n- a note\n",
                                 "facts/dot.md": "# Dot\n\n- Dot: rows.\n# moved to: ada example\n", "notes.md": "# Notes\n\n- Ada: a note.\n"})
    (s.root / "facts" / "out.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("# Out\n\n- Out: far.\n")
    before = snap(tmp_path)
    for a, b, why in (("addie", "addie.md", "are one card"), ("addie", "brim", "a merge keeps to one class and room"),
                      ("addie", "bea quay", "bea-quay.md is emptied"), ("addie", "cal", "cal.md is moved to ada-example"),
                      ("addie", "nobody", "no fact card of the tree"), ("addie", "MEMORY", "a curated file"), ("addie", "out", "a link out of the tree"),
                      ("addie", "notes.md", "no fact card of the tree"), ("dot", "ada example", "a second would be words")):
        for go in (False, True):
            assert cards.merge(s, private, a, b, go) == 2, (a, b)
            err = capsys.readouterr().err
            assert why in err and len(err.splitlines()) == 1 and snap(tmp_path) == before, (a, b, err)
    monkeypatch.setattr(cards, "LOCK_WAIT", 0.2)
    with memory.daily_lock(s.daily):
        pid = os.fork()
        if pid == 0:                     # another process holds no lock: the child asks for the one the parent holds
            os._exit(cards.merge(s, private, "addie", "ada example", go=True))
        assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 2
    assert snap(tmp_path) == before


def test_a_merge_stopped_between_two_steps_loses_no_line_and_run_again_ends_in_the_bytes_of_one_clean_run(tmp_path, monkeypatch, capsys):
    s, private = tree(tmp_path / "clean", MERGE)
    assert cards.merge(s, private, "addie", "ada example", go=True) == 0
    clean, real = snap(s.root), cards.write_private
    for stop in ("addie.md", "addie-b.md"):                 # after B's lines and the rows, before A; after the rows, before the re-point
        s, private = tree(tmp_path / stop, MERGE)

        def cut(f, data, mode=0o600, stop=stop):
            if Path(f).name == stop:
                raise KeyboardInterrupt
            return real(f, data, mode)
        monkeypatch.setattr(cards, "write_private", cut)
        try:
            cards.merge(s, private, "addie", "ada example", go=True)
        except KeyboardInterrupt:
            pass
        half = (s.root / "facts" / "ada-example.md").read_text()
        assert all(x in half for x in (A_LINES[2], A_LINES[4], A_LINES[7])) and (s.root / "facts" / "addie.md").read_text() == MERGE["facts/addie.md"]
        monkeypatch.setattr(cards, "write_private", real)
        assert cards.merge(s, private, "addie", "ada example", go=True) == 0 and snap(s.root) == clean, stop


def test_recall_shows_bs_line_above_a_day_log_line_a_merged_cards_line_replaced_and_a_loop_of_rows_ends(tmp_path):
    day = "# 2026-11-24\n\n" + memory.mark("- 09:00:00-05:00 example-room note: Addie rows the ferry. [c:private] [m:m-9]", "memory/facts/addie.md:3") + "\n"
    s, private = tree(tmp_path, {**MERGE, "daily/2026-11-24.md": day})
    hit = {"ok": True, "hits": [{"cite": "memory/daily/2026-11-24.md:3", "text": day.splitlines()[2]}]}
    assert [h["cite"] for h in memory.live_first(s, hit)["hits"]] == ["memory/facts/addie.md:3", "memory/daily/2026-11-24.md:3"]
    assert cards.merge(s, private, "addie", "ada example", go=True) == 0
    got = memory.live_first(s, hit)["hits"]
    assert [h["cite"] for h in got] == ["memory/facts/ada-example.md:5", "memory/daily/2026-11-24.md:3"] and A_LINES[2][2:] in got[0]["text"]
    (s.root / "facts" / "addie.md").write_text("# Addie\n" + "\n" * 7)             # the pointer line blank too: on through its row
    (s.root / "superseded.tsv").write_text("memory/facts/addie.md:3\tmemory/facts/addie.md:2\nmemory/facts/addie.md:2\tmemory/facts/addie.md:3\n")
    assert [h["cite"] for h in memory.live_first(s, hit)["hits"]] == ["memory/daily/2026-11-24.md:3"]       # a loop of rows ends


def test_deep_recall_hands_no_front_matter_line_and_no_card_title_and_every_other_line_as_before(tmp_path):
    day = "# 2026-11-24\n\n- 09:00:00-05:00 example-room owner@ann: Addie rang about the ladder. [c:private] [m:m-9]\n"
    s, _ = tree(tmp_path, {"facts/ada-example.md": "---\naka: addie\n---\n# Ada Example\n\n- Ada Example: keeps the grey ladder.\n"
                                                   "- Ada Example: says aka Addie on the ferry list.\n", "daily/2026-11-24.md": day,
                           "notes.md": "---\ntitle: addie notes\n---\n# Addie notes\n\nAddie keeps a ladder.\n"})
    memindex.Index(s.index_db, s.scope).sync()
    got = [x["cite"] for x in memory.lines_on(s, "addie")]          # a note's heading is no card title: handed as before
    assert sorted(got[:-1]) == ["memory/facts/ada-example.md:7", "memory/notes.md:4", "memory/notes.md:6"] and got[-1] == "memory/daily/2026-11-24.md:3"
    assert sorted(x["cite"] for x in memory.lines_on(s, "ada example")) == ["memory/facts/ada-example.md:6", "memory/facts/ada-example.md:7"]


def test_cards_near_groups_live_cards_by_a_shared_first_word_a_moved_card_under_its_target_and_no_group_of_one(tmp_path, capsys):
    s, private = tree(tmp_path, {"facts/ada-example.md": "---\naka: Addy Bee\n---\n" + ADA + line(7, "Ada Example: x [superseded]") + "\n",
                                 "facts/ada-quay.md": "# Ada Quay\n\n- Ada Quay: the harbour office.\n",
                                 "facts/addie.md": "---\naka: Ada E\n---\n# Addie\n\n# moved to: ada example\n",
                                 "facts/quay-ada.md": "# Quay Ada\n\n- Quay Ada: a boat.\n", "facts/bea-fen.md": "# Bea Fen\n\n\n",
                                 "facts/bea-quay.md": "# Bea Quay\n\n- Bea Quay: a pier.\n"})
    got = cards.near(s, TODAY)
    assert got == {"groups": [{"word": "ada", "cards": [{"name": "ada example", "live": 2, "aka": ["Addy Bee"], "moved": ["addie"]},
                                                        {"name": "ada quay", "live": 1, "aka": [], "moved": []}]}],
                   "cards": 6, "emptied": 2, "moved": 1, "pointers": 1}
    args = ["cards", "near", "--private", str(private), "--state", str(tmp_path / "state")]
    assert cli.main([*args, "--json"]) == 0 and json.loads(capsys.readouterr().out) == got
    assert cli.main(args) == 0 and capsys.readouterr().out == (
        "ada: ada example (2 live lines, aka Addy Bee, moved addie); ada quay (1 live line)\n"
        "1 groups, 2 cards in them, of 6; 2 emptied, 1 holding a pointer, 1 moved\n")
    assert cli.main(["cards", "merge", "addie", *args[2:]]) == 2 and "takes two cards" in capsys.readouterr().err


def test_after_a_merge_each_reader_reaches_b_by_as_name_and_a_moved_dated_line_is_due_once(tmp_path):
    dated = "- Addie: the library at ten (example-room, 2026-11-20 10:00:00-05:00) [due 2030-01-01] [m:m-8]"
    s, private = tree(tmp_path, {**MERGE, "facts/addie.md": MERGE["facts/addie.md"] + dated + "\n"})
    assert cards.merge(s, private, "addie", "ada example", go=True) == 0
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude, s.tz).refresh(s.scope.files())
    said = [e["cite"] for e in mentions.Index(s.mentions).load().match("how is addie?")]
    assert said and {c.rpartition(":")[0] for c in said} == {"facts/ada-example.md"}
    memindex.Index(s.index_db, s.scope).sync()
    deep = [x["cite"] for x in memory.lines_on(s, "addie")]
    assert sorted(deep) == [f"memory/facts/ada-example.md:{n}" for n in (5, 6, 7, 8)]            # each moved line once, from B
    due.Builder(s).refresh(s.scope.files())
    assert [x["cite"] for x in due.Index(s.due).load()] == ["memory/facts/ada-example.md:8"]
    r = memory.remember(s, memory.DailyLog(s.daily, s.tz), "example-room", "m-9", "Addie rowed back.", about="Addie")
    assert r["cite"] == "memory/facts/ada-example.md:9"
