"""Checks that two cards for one person, two fact files in the memory, become one. The cases: a card is live or emptied (memory.live), an
emptied card may say where it moved (`# moved to: <name>`, memory.moves) and then answers by its names as its target, `yerk cards merge A B`
moves one card's live lines onto another's end and leaves it moved, recall's walk goes on through what the merge moved, deep recall hands no
matter line and no card title, and `yerk cards near` lists the cards sharing a first word. Every name here is made up."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from yerk_ai import curate, memory, words
from test_card_aka import NEXT, build, cards, fresh, seen, sync, write
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
    facts = cards(y, {"orla.md": empty, "orla-vantree.md": "# Orla Vantree\n\n" + LIVE + "\n"})
    r = remember(y, "example-room", "Orla rowed to the point.", "Orla")
    assert r == {"ok": True, "cite": "tree/facts/orla.md:4", "superseded": [], "refused": [], "not_edited": [], "near": ["orla vantree"],
                 "near_next": words.FILED_NEAR, "next": NEXT}
    assert (facts / "orla.md").read_text().startswith(empty)
    assert remember(y, "example-room", "Orla rowed back.", "Orla") == {"ok": True, "cite": "tree/facts/orla.md:5", "superseded": [],
                                                                      "refused": [], "not_edited": [], "next": NEXT}     # live now: as on main


def test_remember_about_an_aka_whose_own_name_card_is_emptied_files_on_the_card_that_says_it(tmp_path):
    y = fresh(tmp_path, "one")
    facts = cards(y, {"addie.md": "# Addie\n\n\n", "ada-example.md": "---\naka: addie\n---\n# Ada Example\n\n" + LIVE + "\n"})
    assert remember(y, "example-room", "Addie paints boats.", "Addie")["cite"] == "tree/facts/ada-example.md:7"
    assert (facts / "addie.md").read_text() == "# Addie\n\n\n"
    y = fresh(tmp_path, "two")                 # the aka card emptied too: neither is a card, the name starts again on its own path
    cards(y, {"addie.md": "# Addie\n\n\n", "ada-example.md": "---\naka: addie\n---\n# Ada Example\n\n"})
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
    facts = cards(y, {"ada-example.md": ADA, "addie.md": ADDIE})
    assert remember(y, "example-room", "Addie rowed to the point.", "Addie")["cite"] == "tree/facts/ada-example.md:5"
    assert remember(y, "example-room", "Addy Bee painted the lamp room.", "addy bee")["cite"] == "tree/facts/ada-example.md:6"
    assert (facts / "addie.md").read_text() == ADDIE
    y = fresh(tmp_path, "two")
    have = {"ada-example.md": ADA, "addie.md": "---\naka: Ada E\n---\n# Addie\n\n# moved to: ada example\n", "ada-fen.md": "# Ada Fen\n\n\n",
            "ada-quay.md": "# Ada Quay\n\n- Ada Quay: the harbour office.\n"}
    cards(y, have)
    r = remember(y, "example-room", "Ada Lane is the new pier road.", "Ada Lane")
    assert (r["cite"], r["near"]) == ("tree/facts/ada-lane.md:3", ["ada example", "ada quay"])
    y = fresh(tmp_path, "three")                 # the target emptied: the pointer is words, the name starts on its own path
    cards(y, {"ada-example.md": "# Ada Example\n\n\n", "addie.md": ADDIE})
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
