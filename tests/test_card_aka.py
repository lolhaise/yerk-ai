"""Checks that a card, the fact file the memory keeps on one person or thing, answers to more than one name. A fact file's
front matter may say `aka: name, name`, the other names it answers to, each one mentions.key_of takes (memory.card_names). The
mention index keys the card by each as by its own name, after a card whose own name it is; `remember` files under the one card
whose aka its `about` is; a new card started beside one that shares its first word says so (`near`); the night's promotion
made up."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path

from yerk_ai import curate, memindex, memory, mentions, words
from test_curate import NOW, layer, said  # noqa: F401 (layer: the promotion's example layer, a fixture)
from test_remember import daemon, remember
from test_room_scope import install

NEXT = words.FILED_NEXT.format(clause=words.FILED_AT_MOST)
CARD = "# Orla Vantree\n\n- Orla Vantree: a retired lighthouse keeper.\n- Orla lives by the old quay.\n- Boat: the Example Gull, at pier 3.\n"
PEOPLE = "# people\n\n- Orla Vantree: plays the fiddle on Fridays.\n- Boat: the ferry, twice a day.\n"
AKAS = ("orly", "lady vantree", "bree-anne", "bramble")


FRONT_CASES = {
    "facts/orla-vantree.md": "---\naka: the, my sister, Al, home, she is my sister, Orly\n---\n" + CARD,
    "facts/quay-house.md": "# Quay House\n\naka: Quayside\n- Quay House: the keeper's cottage.\n- Keeper: the post at the quay.\n",
    "facts/gull-rock.md": "# Gull Rock\n---\naka: Gullet\n---\n- Gull Rock: a reef off the point.\n",
    "MEMORY.md": "---\naka: Pinny\n---\n# Pinned\n\n## Example Pier\nby the quay\n",
    "facts/MEMORY.md": "---\naka: Factsy\n---\n# Notes\n\n## Example Dune\nbehind the quay\n",
    "facts/keeper.md": "---\naka: Keepy\n---\n# Keeper\n\n## Example Lamp\nlit at dusk\n",
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
        "MEMORY.md": [], "facts/MEMORY.md": ["MEMORY", "Factsy"], "facts/keeper.md": ["keeper", "Keepy"], "facts/brim-quell.md": ["brim quell", "Brimmy"],
        "facts/zed-quill.md": ["zed quill", "Zeddy"], "facts/ada-quill.md": ["ada quill", "ada", "addie"], "notes/orla.md": [], "facts/orla.txt": []}
    assert [memory.file_class(root / f) for f in ("facts/brim-quell.md", "facts/zed-quill.md", "facts/ada-quill.md")] == [
        ("shared", ""), ("room", "rid-1"), ("private", "")]
    assert memory.card_names(root / "facts" / "ada-quill.md", "# Ada Quill\n") == ["ada quill"]      # the text given is the one read


def build(tmp_path: Path, files: dict[str, str], name: str = "tree") -> mentions.Index:
    """The name index of a tree holding `files` (MEMORY.md and keeper.md, wherever they sit, curated files), as yerk-index builds it."""
    root, out = write(tmp_path / name, files), tmp_path / f"{name}.json"
    mentions.Builder(out, (root,), ("MEMORY.md", "keeper.md"), ()).refresh({str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*.md")})
    return mentions.Index(out).load()


def seen(idx: mentions.Index, text: str) -> list[tuple[str, str]]:
    return [(e["cite"], e["text"]) for e in idx.match(text)]


def test_saying_a_cards_aka_gets_what_its_own_name_gets_in_that_order_and_any_other_word_what_it_got(tmp_path):
    """Each aka, one word or two, in another case, with a hyphen or a plural, folds as a name does and gets the card's own
    name's entries in their order, `named` the words said; its start, one of its words or a longer word gets nothing; a word
    that is no aka gets what the tree with no aka line gives; the index is that tree's but for the aka keys."""
    idx = build(tmp_path, {"facts/orla-vantree.md": "---\naka: Orly, Lady Vantree, BREE-ANNE, Brambles\n---\n" + CARD, "facts/people.md": PEOPLE})
    plain = build(tmp_path, {"facts/orla-vantree.md": "---\nclass: private\n---\n" + CARD, "facts/people.md": PEOPLE}, "plain")
    own = seen(idx, "how is orla vantree?")
    assert [c for c, _ in own] == ["facts/orla-vantree.md:6", "facts/people.md:3"]
    for said_as, named in (("how is orly?", "orly"), ("is Lady Vantree home", "lady vantree"), ("was it BREE-ANNE's?", "bree-anne"),
                           ("bree-anne", "bree-anne"), ("the bramble", "bramble"), ("the brambles", "bramble")):
        assert seen(idx, said_as) == own and {e["named"] for e in idx.match(said_as)} == {named}, said_as
    for word in ("orl", "orlyx", "lady", "vantree", "bree anne", "bree", "bram"):     # its start, one of its words, the hyphen apart
        assert seen(idx, f"what about {word}?") == [], word
    for word in ("boat", "orla lives by the old quay", "people"):                # a word no aka is: what it got with no aka line
        assert seen(idx, word) == seen(plain, word), word
    assert seen(idx, "boat") and seen(plain, "orly") == []
    full, bare = (json.loads((tmp_path / f"{n}.json").read_text()) for n in ("tree", "plain"))
    assert full["entries"] == bare["entries"] and {k: v for k, v in full["keys"].items() if k not in AKAS} == bare["keys"]
    assert all(full["keys"][a] == full["keys"]["orla vantree"] for a in AKAS)


def test_a_card_whose_own_name_is_the_aka_comes_first_then_the_card_that_says_it_then_the_rest(tmp_path):
    files = {"facts/orla-vantree.md": "---\naka: Orly\n---\n" + CARD, "facts/orly.md": "# Orly\n\n- Orly: the ferry's ginger cat.\n",
             "facts/people.md": PEOPLE + "- Orly: naps on the ferry's bench.\n"}
    assert [c for c, _ in seen(build(tmp_path, files), "where is orly")] == [
        "facts/orly.md:3", "facts/orla-vantree.md:6", "facts/people.md:3", "facts/people.md:5"]
    files["facts/orla-vantree.md"] = "---\nclass: private\n---\n" + CARD
    assert [c for c, _ in seen(build(tmp_path, files, "plain"), "where is orly")] == ["facts/orly.md:3", "facts/people.md:5"]


def test_the_index_keys_no_aka_key_of_refuses_none_outside_the_front_matter_and_none_of_a_curated_file(tmp_path):
    idx = build(tmp_path, FRONT_CASES)
    keys = set(json.loads((tmp_path / "tree.json").read_text())["keys"])
    assert "orly" in keys and "keeper" in keys and not keys & {"the", "my sister", "al", "home", "she is my sister", "quayside", "gullet", "pinny",
                                                               "factsy", "keepy"}
    assert seen(idx, "quayside") == seen(idx, "gullet") == seen(idx, "pinny") == seen(idx, "factsy") == seen(idx, "keepy") == []
    assert {e["class"] for e in idx.match("brimmy")} == {"shared"} and {e["room"] for e in idx.match("zeddy")} == {"rid-1"}
    assert seen(idx, "addie") == seen(idx, "ada quill") != []


def test_a_tree_with_no_aka_builds_the_index_main_builds(tmp_path):
    """Main 8dc53f66's build of this tree, its entries and keys hashed (all but `built`): no aka, no change."""
    build(tmp_path, {"MEMORY.md": "# Pinned\n\n## Example Gull\nthe boat, at pier 3\n\n## Orla Vantree\nthe keeper\n",
                     "facts/orla-vantree.md": "---\nclass: private\n---\n" + CARD,
                     "facts/orla.md": "# Orla\n\n- Orla: the keeper's niece.\n- Orla Vantree: her aunt.\n- Orla: the keeper's niece, nine.\n",
                     "facts/people.md": PEOPLE + "- Orla: sings in the choir.\n- Zed lives in Example Bay with Orla.\n",
                     "facts/brim-quell.md": "---\nclass: shared\n---\n# Brim Quell\n\n- Brim Quell: runs the example bakery.\n- ⛔ Brim: old address\n",
                     "facts/quay-house.md": "# Quay House\n\nThe keeper's cottage.\n- Quay House: two rooms.\n## Orla's room\n- Orla Vantree: sleeps here.\n"})
    data = json.loads((tmp_path / "tree.json").read_text())
    assert hashlib.sha256(json.dumps([data["entries"], data["keys"]]).encode()).hexdigest()[:16] == "3b82457b67947f13"


def sync(y) -> None:
    s = y.memory
    memindex.Index(s.index_db, s.scope).sync()
    mentions.Builder(s.mentions, s.roots_for_mentions, s.curated, s.mentions_exclude).refresh(s.scope.files())


def test_a_room_that_may_not_read_a_card_gets_nothing_by_its_aka_and_an_owner_room_gets_it(tmp_path):
    y = install(tmp_path, family=True)
    (facts := y.memory.root / "facts").mkdir(exist_ok=True)
    (facts / "orla-vantree.md").write_text("---\naka: Orly\n---\n# Orla Vantree\n\n- Orla Vantree: keeps the example lighthouse.\n")
    (facts / "brim-quell.md").write_text("---\nclass: shared\naka: Brimmy\n---\n# Brim Quell\n\n- Brim Quell: runs the example bakery.\n")
    (facts / "ferry.md").write_text("---\nclass: shared\n---\n# Ferry\n\n- Orly: the ferry's cat, on board daily.\n")     # one key, both kinds
    sync(y)
    got = [e["text"] for e in y.recall_for("family-room", "is orly at the lighthouse?")[0]]
    assert "Orly: the ferry's cat, on board daily." in got and not [x for x in got if "lighthouse" in x]
    assert "Orla Vantree: keeps the example lighthouse." in [e["text"] for e in y.recall_for("example-room", "is orly at the lighthouse?")[0]]
    assert "Brim Quell: runs the example bakery." in [e["text"] for e in y.recall_for("family-room", "did brimmy bake?")[0]]


def fresh(tmp_path: Path, name: str):
    (tmp_path / name).mkdir()
    return daemon(tmp_path / name)


def cards(y, files: dict[str, str]) -> Path:
    (facts := y.memory.root / "facts").mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (facts / name).write_text(text)
    return facts


ORLA = "---\naka: Orly, Lady Vantree\n---\n# Orla Vantree\n\n- Orla Vantree: keeps the example lighthouse.\n"


def test_remember_about_an_aka_files_on_that_card_and_its_own_name_and_anything_else_file_as_today(tmp_path):
    y = daemon(tmp_path)
    facts = cards(y, {"orla-vantree.md": ORLA})
    r = remember(y, "example-room", "Orly painted the lamp room blue.", "Orly")
    assert r == {"ok": True, "cite": "tree/facts/orla-vantree.md:7", "superseded": [], "refused": [], "not_edited": [], "next": NEXT}
    assert remember(y, "example-room", "Lady Vantree retired in May.", "lady vantree")["cite"] == "tree/facts/orla-vantree.md:8"
    assert remember(y, "example-room", "Orla Vantree rows on Sundays.", "Orla Vantree")["cite"] == "tree/facts/orla-vantree.md:9"
    assert remember(y, "example-room", "Orly painted the lamp room blue.", "Orly")["cite"] == r["cite"]         # a retry, once
    text = (facts / "orla-vantree.md").read_text()
    assert text.startswith(ORLA) and len(text.splitlines()) == 9 and sorted(p.name for p in facts.iterdir()) == ["orla-vantree.md"]
    assert remember(y, "example-room", "Lady Gull sails on Monday.", "Lady")["cite"] == "tree/facts/lady.md:3"    # its start: a card of its own


def test_a_card_named_exactly_about_wins_and_two_cards_saying_one_aka_choose_neither(tmp_path):
    y = daemon(tmp_path)
    facts = cards(y, {"orla-vantree.md": ORLA, "orly.md": "# Orly\n\n- Orly: the ferry's ginger cat.\n"})
    assert remember(y, "example-room", "Orly caught a fish.", "Orly")["cite"] == "tree/facts/orly.md:4"
    assert (facts / "orla-vantree.md").read_text() == ORLA
    y = fresh(tmp_path, "two")
    facts = cards(y, {"orla-vantree.md": ORLA, "orla-quay.md": ORLA.replace("Lady Vantree", "Quay Orla").replace("Vantree:", "Quay:")})
    r = remember(y, "example-room", "Orly caught a fish.", "Orly")
    assert r["cite"] == "tree/facts/orly.md:3" and r["near"] == ["orla quay", "orla vantree"]
    assert (facts / "orla-vantree.md").read_text() == ORLA and "fish" not in (facts / "orla-quay.md").read_text()


def test_said_a_room_that_is_not_an_owner_room_and_an_aka_on_a_card_that_is_not_private_file_where_they_did(tmp_path):
    y = daemon(tmp_path, "example-room", "family-room")
    facts = cards(y, {"orla-vantree.md": ORLA, "brim-quell.md": "---\nclass: shared\naka: Brimmy\n---\n# Brim Quell\n\n- Brim Quell: bakes.\n"})
    r = asyncio.run(y.tool_remember("example-room", "I painted the lamp room", "Orly", [], said="Orla", source="m-1"))
    assert r["ok"] and r["cite"].startswith("tree/daily/") and "near" not in r
    r = remember(y, "family-room", "Orly painted the lamp room blue.", "Orly")
    assert r["ok"] and r["cite"].startswith("tree/daily/") and "near" not in r
    assert (facts / "orla-vantree.md").read_text() == ORLA
    r = remember(y, "example-room", "Brimmy baked rye.", "Brimmy")
    assert r["cite"] == "tree/facts/brimmy.md:3" and "rye" not in (facts / "brim-quell.md").read_text()


def test_a_new_card_beside_one_sharing_its_first_word_by_name_or_aka_says_so_and_files_where_it_would(tmp_path):
    for i, (have, about, near) in enumerate((
            ({"orla-vantree.md": "# Orla Vantree\n\n- Orla Vantree: keeps the lighthouse.\n", "quay-orla.md": "# Quay Orla\n"}, "Orla", ["orla vantree"]),
            ({"orla.md": "# Orla\n\n- Orla: the keeper's niece.\n", "vantree-hall.md": "# Vantree Hall\n"}, "Orla Vantree", ["orla"]),
            ({"zed-quill.md": "---\naka: Orla\n---\n# Zed Quill\n"}, "Orla Vantree", ["zed quill"]),
            ({"zed-quill.md": "---\naka: Orla Vantree\n---\n# Zed Quill\n"}, "Orla", ["zed quill"]))):
        y = fresh(tmp_path, str(i))
        facts = cards(y, have)
        r = remember(y, "example-room", "Orla rowed to the point.", about)
        stem = "-".join(mentions.tokens(about))
        assert r == {"ok": True, "cite": f"tree/facts/{stem}.md:3", "superseded": [], "refused": [], "not_edited": [], "near": near,
                     "near_next": words.FILED_NEAR, "next": NEXT}, about
        assert all((facts / n).read_text() == t for n, t in have.items())
        assert "near" not in remember(y, "example-room", "Orla rowed back.", about)          # a card that exists: none


def test_a_new_card_with_no_neighbour_answers_as_main_and_six_neighbours_give_five_sorted(tmp_path):
    y = daemon(tmp_path)
    cards(y, {"orla-vantree.md": ORLA, "quay-orla.md": "---\naka: Gull Orla\n---\n# Quay Orla\n"})
    assert remember(y, "example-room", "Example kettle: painted teal.", "example kettle") == {
        "ok": True, "cite": "tree/facts/example-kettle.md:3", "superseded": [], "refused": [], "not_edited": [], "next": NEXT}
    y = fresh(tmp_path, "six")
    cards(y, {f"orla-{w}.md": f"# Orla {w}\n" for w in ("heath", "fen", "bay", "glen", "dune", "cove")})
    assert remember(y, "example-room", "Orla rowed to the point.", "Orla")["near"] == ["orla bay", "orla cove", "orla dune", "orla fen", "orla glen"]


def test_the_promotion_reads_a_cards_pack_by_its_aka_and_a_card_with_no_aka_as_it_did(layer):  # noqa: F811
    priv, state, s, log = layer
    day = s.daily / "2026-10-20.md"
    day.write_text(day.read_text() + said(20, 5, "Orly fixed the lamp room on Sunday") + "\n" + said(20, 6, "other things were moved") + "\n")
    (orla := s.root / "facts" / "orla-vantree.md").write_text("---\naka: Orly, other things\n---\n# Orla Vantree\n"
                                                             "- Orla Vantree: keeps the lighthouse (src: memory/daily/2026-10-19.md:3)\n")
    pack = [x for _, _, x in curate.packs(s, priv, [log.resolve()], [date(2026, 10, 20)])[0].get(orla, [])]
    assert any("Orly fixed the lamp room" in x for x in pack) and not any("other things" in x for x in pack)
    assert "Orly" in curate.names(orla, orla.read_text()).pattern and "other" not in curate.names(orla, orla.read_text()).pattern
    ada = s.root / "facts" / "ada.md"
    assert curate.names(ada, ada.read_text()).pattern == r"\bAda\b|\bAda\ Example\b|\bAda\ Example\b|\bAda\b|\bmom\b|\bmother\b"


def test_a_proposed_line_headed_by_a_cards_aka_passes_the_head_check_and_another_common_word_does_not(layer, monkeypatch):  # noqa: F811
    priv, state, s, log = layer
    monkeypatch.setattr(curate, "bench", lambda s: {"top_k": 10, "top_1": 5, "at": "now", "seconds": 1.0})
    day = s.daily / "2026-10-20.md"
    day.write_text(day.read_text() + said(20, 5, "Orly fixed the lamp room on Sunday") + "\n")
    (s.root / "facts" / "orla-vantree.md").write_text("---\naka: Orly\n---\n# Orla Vantree\n"
                                                      "- Orla Vantree: keeps the lighthouse (src: memory/daily/2026-10-19.md:3)\n")
    (state / "memory").mkdir()
    index = memindex.Index(s.index_db, s.scope)
    index.sync()
    monkeypatch.setattr(curate.time, "sleep", lambda n: index.sync())
    cite = "memory/daily/2026-10-20.md:7"
    lines = [f"orla-vantree.md: - Orly: fixed the lamp room on Sunday (src: {cite})", f"orla-vantree.md: - Lamp: fixed on Sunday (src: {cite})"]

    def model(prompt: str, tools: str) -> str:
        return "\n".join(f"KEEP {x}" for x in lines) if tools else "\n".join(lines) if "pack orla-vantree.md" in prompt else ""
    r = curate.run(priv, state, now=NOW, model=model)
    assert r["landed"] == [lines[0]] and [x for x in r["checked"] if "one common word" in x] == [lines[1] + " | its head 'Lamp' is one common word, a key that would pull the card into any message"]
