"""Checks that the pages `yerk docs own` writes are the code's own words, the same bytes every time, and that docs/ is
never stale against the code. The check, decision 16, has the same shape as the budget test's [code] ceiling."""
import ast
import tomllib

from yerk_ai import cli, hooks, owndocs
from yerk_ai.config import REPO, ROOM_KEYS, ROOM_WORDS, YERKD_WORDS


def test_the_docs_are_what_the_code_writes():
    """The staleness test: a change to a parser, a tool, a key table, budgets.toml, a hook event or a unit changes a page,
    so the change that makes it runs `yerk docs own` in the same commit."""
    old = owndocs.stale()
    assert old == [], f"docs/{', docs/'.join(old)} stale against the code: run `yerk docs own` in this change"


def test_the_pages_are_the_same_bytes_every_time_and_carry_nothing_of_the_install(tmp_path, monkeypatch):
    """Twice the same bytes, and the same again under another install's private layer, state folder, working folder and
    terminal width: no value of the owner's rides into a page (decision 22)."""
    first = owndocs.pages()
    private = tmp_path / "private"
    (private / "memory").mkdir(parents=True)
    (private / "rooms.toml").write_text('[rooms.quuxroom]\nchannel = "discord:1"\nmodel = "quuxmodel"\n')
    (private / "yerkd.toml").write_text('[memory]\ntimezone = "Asia/Tokyo"\n[presentation]\nfooter = "full"\n')
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.chdir(tmp_path)
    again = owndocs.pages()
    assert again == first and list(first) == ["commands.md", "tools.md", "settings.md", "budgets.md", "hooks.md", "units.md"]
    assert not [n for n, t in again.items() if "quux" in t or "Asia/Tokyo" in t]


def test_the_dry_run_names_a_stale_page_and_writes_nothing_and_the_run_writes_it(tmp_path, capsys):
    assert owndocs.main(True, tmp_path) == 1 and list(tmp_path.iterdir()) == []
    assert "docs/commands.md: stale against the code" in capsys.readouterr().out
    assert owndocs.main(False, tmp_path) == 0 and sorted(p.name for p in tmp_path.iterdir()) == sorted(owndocs.pages())
    (tmp_path / "hooks.md").write_text("written by hand\n")
    assert owndocs.stale(tmp_path) == ["hooks.md"] and owndocs.main(True, tmp_path) == 1
    assert cli.main(["docs", "own", "--dry-run"]) == 0 and "every page is what the code writes" in capsys.readouterr().out


def test_every_event_a_room_registers_is_described_and_every_described_one_registered():
    assert set(hooks.settings_for("http://127.0.0.1:1/hook", "header")) == set(hooks.EVENTS)


def test_every_key_described_is_one_the_package_names_outside_the_tables():
    """A described key the code no longer names would be a stale line in settings.md: each rooms.toml key and each yerkd.toml
    section and key (an upper-case placeholder aside) is a string the package holds outside the two word tables."""
    named: set[str] = set()
    for f in sorted((REPO / "yerk_ai").rglob("*.py")):
        tree = ast.parse(f.read_text())
        tables = [a for a in ast.walk(tree) if isinstance(a, ast.Assign) and any(getattr(t, "id", "") in ("ROOM_WORDS", "YERKD_WORDS") for t in a.targets)]
        skip = {id(n) for a in tables for n in ast.walk(a)}
        named |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip}
    want = set(ROOM_WORDS) | set(YERKD_WORDS) | {k for ks in YERKD_WORDS.values() for k in ks if not k.isupper()}
    assert sorted(want - named) == [] and ROOM_KEYS == frozenset(ROOM_WORDS)


def test_every_key_of_the_example_files_is_described():
    doc = tomllib.loads((REPO / "yerkd.toml.example").read_text())
    assert {(s, k) for s, t in doc.items() for k in t} - {(s, k) for s, ks in YERKD_WORDS.items() for k in ks} == set()
    rooms = tomllib.loads((REPO / "rooms.toml.example").read_text())["rooms"]
    assert {k for c in rooms.values() for k in c} - set(ROOM_WORDS) == set()


def test_the_readme_stays_a_ten_minute_read():
    """2026-10-24: 118,399 bytes, 57 percent of them a Status section, cut to the first section, five install lines, the private
    folder, a turn, the add-ons and the docs; the rest is docs/GUIDE.md and docs/STATUS.md, whole."""
    size = len((REPO / "README.md").read_bytes())
    assert size < 12_000, f"README.md is {size:,} bytes: the long form goes in docs/GUIDE.md, the facts the code owns in its pages"
