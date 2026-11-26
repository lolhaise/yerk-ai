"""Checks that two cards for one person, two fact files in the memory, become one. The cases: a card is live or emptied (memory.live), an
emptied card may say where it moved (`# moved to: <name>`, memory.moves) and then answers by its names as its target, `yerk cards merge A B`
moves one card's live lines onto another's end and leaves it moved, recall's walk goes on through what the merge moved, deep recall hands no
matter line and no card title, and `yerk cards near` lists the cards sharing a first word. Every name here is made up."""
from __future__ import annotations

from datetime import date

from yerk_ai import memory, words
from test_card_aka import NEXT, cards, fresh
from test_remember import daemon, remember

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
