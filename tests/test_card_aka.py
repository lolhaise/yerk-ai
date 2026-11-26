"""Checks that a card, the fact file the memory keeps on one person or thing, answers to more than one name. A fact file's
front matter may say `aka: name, name`, the other names it answers to, each one mentions.key_of takes (memory.card_names). The
mention index keys the card by each as by its own name, after a card whose own name it is; `remember` files under the one card
whose aka its `about` is; a new card started beside one that shares its first word says so (`near`); the night's promotion
made up."""
from __future__ import annotations

from pathlib import Path

from yerk_ai import memory

CARD = "# Orla Vantree\n\n- Orla Vantree: a retired lighthouse keeper.\n- Orla lives by the old quay.\n- Boat: the Example Gull, at pier 3.\n"


FRONT_CASES = {
    "facts/orla-vantree.md": "---\naka: the, my sister, Al, home, she is my sister, Orly\n---\n" + CARD,
    "facts/quay-house.md": "# Quay House\n\naka: Quayside\n- Quay House: the keeper's cottage.\n",
    "facts/gull-rock.md": "# Gull Rock\n---\naka: Gullet\n---\n- Gull Rock: a reef off the point.\n",
    "MEMORY.md": "---\naka: Pinny\n---\n# Pinned\n\n## Example Pier\nby the quay\n",
    "facts/MEMORY.md": "---\naka: Factsy\n---\n# Notes\n\n## Example Dune\nbehind the quay\n",
    "facts/brim-quell.md": "---\nclass: shared\naka: Brimmy\n---\n# Brim Quell\n\n- Brim Quell: runs the example bakery.\n",
    "facts/zed-quill.md": "---\nclass: room\nroom: rid-1\naka: Zeddy\n---\n# Zed Quill\n\n- Zed Quill: mends nets.\n",
    "facts/ada-quill.md": "---\naka: ada, addie\nclass: private\n---\n# Ada Quill\n\n- Ada Quill: rows the ferry.\n",
    "notes/orla.md": "---\naka: Orly\n---\n# Orla\n",
    "facts/orla.txt": "---\naka: Orly\n---\n# Orla\n"}


def write(root: Path, files: dict[str, str]) -> Path:
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    return root


def test_a_cards_names_are_its_file_name_then_each_aka_key_of_takes_from_front_matter_that_opens_a_fact_file(tmp_path):
    """A name key_of refuses is ignored; an aka line below the front matter, in front matter that does not open the file, or in
    a file that is no fact file is words; `class:` and `room:` beside an aka still give the file's class."""
    root = write(tmp_path / "tree", FRONT_CASES)
    assert {f: memory.card_names(root / f) for f in FRONT_CASES} == {
        "facts/orla-vantree.md": ["orla vantree", "Orly"], "facts/quay-house.md": ["quay house"], "facts/gull-rock.md": ["gull rock"],
        "MEMORY.md": [], "facts/MEMORY.md": ["MEMORY", "Factsy"], "facts/brim-quell.md": ["brim quell", "Brimmy"],
        "facts/zed-quill.md": ["zed quill", "Zeddy"], "facts/ada-quill.md": ["ada quill", "ada", "addie"], "notes/orla.md": [], "facts/orla.txt": []}
    assert [memory.file_class(root / f) for f in ("facts/brim-quell.md", "facts/zed-quill.md", "facts/ada-quill.md")] == [
        ("shared", ""), ("room", "rid-1"), ("private", "")]
    assert memory.card_names(root / "facts" / "ada-quill.md", "# Ada Quill\n") == ["ada quill"]      # the text given is the one read
