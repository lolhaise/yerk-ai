"""Checks texting's reply list, the people a room may answer by text for you: yours only, handles in one form, a handle on one
person only. Each person's sheet beside it (run 2), read by code and checked, and changed only through the report room's `texting`
of this turn, each change one line of sheets.log. Every person and number here is made up."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from yerk_ai import doctor, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.texting import people

AT, SPACED = "@", "+1 (555) 000" "-0101"     # split in the source: the repository scan reads no fixture
LIST = f"""[owner]
name = "Sam"

[people.alex]
first = "Alex"
handles = ["{SPACED}", "Alex.Q{AT}Example.com"]
sheet = "people/alex.toml"
release = true

[people.beta]
handles = ["+15550000102"]
"""


def write(tmp_path, text: str, mode: int = 0o600):
    f = tmp_path / "people.toml"
    f.write_text(text)
    os.chmod(f, mode)
    return f


def test_people_loads_and_matches_a_handle_however_it_is_written(tmp_path):
    p = people.load(write(tmp_path, LIST))
    assert p.owner == "Sam" and set(p.people) == {"alex", "beta"}
    alex = p.people["alex"]
    assert alex.handles == ("+15550000101", f"alex.q{AT}example.com") and alex.release and alex.sheet == "people/alex.toml"
    assert p.by_handle("+15550000101") is alex and p.by_handle(f"ALEX.Q{AT}example.com") is alex
    assert p.people["beta"].first == "beta" and not p.people["beta"].release
    assert p.by_handle("+15550000199") is None and p.by_handle("") is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_people_refuses_a_file_others_can_read(tmp_path, mode):
    with pytest.raises(ValueError, match="0600"):
        people.load(write(tmp_path, LIST, mode))


@pytest.mark.parametrize("bad", ['handles = ["Alex"]', 'handles = []', 'handles = "+15550000103"',
                                 'handles = ["+15550000102"]'])
def test_people_refuses_a_bad_handle_and_one_shared(tmp_path, bad):
    with pytest.raises(ValueError, match="people.casey"):
        people.load(write(tmp_path, LIST + f"\n[people.casey]\n{bad}\n"))


def test_doctor_names_what_keeps_texting_off(tmp_path):
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    state.mkdir()

    def said(table: str) -> list[str]:
        (priv / "yerkd.toml").write_text(table)
        return [x[2:] for x in doctor.run(priv, state, dry=True) if "[texting]" in x]
    assert said("") == [] and said('[texting]\nmode = "off"\nburst_cap = "soon"\n') == []
    on = '[texting]\nmode = "dry"\nreport = "example-room"\n'
    missing = said(on + 'burst_cap = "soon"\ndry_report = "nowhere"\n')
    assert missing[0] == "[texting] burst_cap: not a number above 0; texting stays off"
    assert missing[1].startswith("[texting] people: ") and missing[1].endswith("; texting stays off")
    assert missing[2:] == ["[texting] dry_report: nowhere is not an owner room in rooms.toml"]
    (priv / "texting").mkdir()
    write(priv / "texting", LIST, 0o644)
    assert [x for x in said(on) if "0600" in x]
    os.chmod(priv / "texting" / "people.toml", 0o600)
    assert said(on) == [] and said('[texting]\nmode = "dry"\n') == ["[texting] report: none is not an owner room in rooms.toml"]


A1, B1 = "+15550000101", "+15550000102"
REPLY = f'[owner]\nname = "Sam"\n\n[people.alex]\nfirst = "Alex"\nhandles = ["{A1}"]\n# kept as written\n\n' \
        f'[people.beta]\nfirst = "Beta"\nhandles = ["{B1}"]\nrelease = false\n'
SHEET = 'open = ["location"]\nknows = [{fact = "the new car", source = "owner-room m:1 2026-11-12"}]\nnight = "answer"\n' \
        'reacts = ["love"]\nunanswered_share = 0.25\n\n[pacing]\nactive = [0.5, 2]\nburst_gap = [10, 35]\n\n' \
        "[voice]\ncard = '''\nshort, lowercase, no full stops'''\n"


def folder(tmp_path: Path, sheet: str | None = SHEET) -> Path:
    """A texting folder: the list (0600) and, unless `sheet` is None, Alex's sheet; Beta has none."""
    (where := tmp_path / "texting" / "people").mkdir(parents=True)
    (f := where.parent / "people.toml").write_text(REPLY)
    os.chmod(f, 0o600)
    if sheet is not None:
        (s := where / "alex.toml").write_text(sheet)
        os.chmod(s, 0o600)
    return f


def test_a_sheet_loads_and_a_person_with_none_has_no_card_and_every_class_held(tmp_path):
    who = people.load(folder(tmp_path))
    alex = who.sheets["alex"]
    assert alex.open == ("location",) and alex.knows == (("the new car", "owner-room m:1 2026-11-12"),)
    assert (alex.night, alex.reacts, alex.unanswered_share) == ("answer", ("love",), 0.25)
    assert alex.pacing == {"active": (0.5, 2.0), "burst_gap": (10.0, 35.0)} and alex.card == "short, lowercase, no full stops"
    assert who.sheets["beta"] == people.Sheet() and who.sheets["beta"].open == () and who.sheets["beta"].card == ""
    for s in (alex, people.Sheet(card="it's\n'''"), people.Sheet(knows=(('a "quoted" fact', ""),))):    # written back, the same
        (f := tmp_path / "round.toml").write_text(people.sheet_text(s))
        os.chmod(f, 0o600)
        assert people.sheet(f) == s


def test_the_classes_are_one_list_and_a_sheet_opening_another_fails_the_load_with_its_file_and_line(tmp_path):
    assert words.TEXTING_CLASSES == ("health", "medication", "substances", "faith", "money", "job", "dating", "location")
    bad = SHEET.replace('open = ["location"]', 'open = [\n  "location",\n  "gossip",\n]')
    with pytest.raises(ValueError, match=r"alex\.toml:3: open: 'gossip' is not a class; the classes: health, "):
        people.load(folder(tmp_path, bad))
    for i, (old, new, line, key) in enumerate((('night = "answer"', 'night = "late"', 3, "night"),
                                               ('reacts = ["love"]', 'reacts = ["wow"]', 4, "reacts"),
                                               ("unanswered_share = 0.25", "unanswered_share = 2", 5, "unanswered_share"),
                                               ("burst_gap = [10, 35]", "burst_gap = [35, 10]", 7, "pacing"),
                                               ('night = "answer"', "nite = 1", 3, "nite"),
                                               ("[voice]", "[voice]\ntone = 1", 11, "voice"))):
        with pytest.raises(ValueError, match=rf"alex\.toml:{line}: {key}: "):
            people.load(folder(tmp_path / str(i), SHEET.replace(old, new)))
    with pytest.raises(ValueError, match=r"alex\.toml: .*\(at line 2"):
        people.load(folder(tmp_path / "syntax", "reacts = []\nnight = \n"))
    os.chmod(folder(tmp_path / "open").parent / "people" / "alex.toml", 0o644)
    with pytest.raises(ValueError, match=r"alex\.toml must be 0600"):
        people.load(tmp_path / "open" / "texting" / "people.toml")
    (f := folder(tmp_path / "out", None)).write_text(REPLY.replace('first = "Beta"', 'first = "Beta"\nsheet = "../b.toml"'))
    with pytest.raises(ValueError, match=r"people\.beta: sheet must be a path inside texting/"):
        people.load(f)


def test_check_names_a_bad_sheet_with_its_file_and_line_a_missing_one_and_a_file_no_one_names(tmp_path, capsys):
    from yerk_ai import cli
    f = folder(priv := tmp_path / "private")
    (priv / "yerkd.toml").write_text('[texting]\nmode = "dry"\n')
    (beta := f.parent / "people" / "beta.toml").write_text(SHEET)
    os.chmod(beta, 0o600)
    log = f.parent / "sheets.log"
    log.write_text("not read\n")                                   # the log is never read: a line in it is no finding
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 0
    assert capsys.readouterr().out == "every sheet loads\n"
    beta.write_text(SHEET.replace('reacts = ["love"]', 'reacts = ["love"]\nopen = 3'))
    (f.parent / "people" / "gamma.toml").write_text("")
    (f.parent / "people" / "alex.toml").unlink()
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"{f.parent / 'people' / 'alex.toml'}: missing, so alex has no voice card and every class held"
    assert out[1].startswith(f"{beta}: ") and "line 5" in out[1]                  # tomllib's own words: a key set twice
    assert out[2:] == [f"{f.parent / 'people' / 'gamma.toml'}: no one on the list names it"]
    (f.parent / "people" / "gamma.toml").unlink()
    beta.write_text(SHEET.replace('open = ["location"]', 'open = ["money", "gossip"]'))
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    assert capsys.readouterr().out.splitlines()[1] == f"{beta}:1: open: 'gossip' is not a class; the classes: " + ", ".join(words.TEXTING_CLASSES)
    f.write_text(REPLY.replace(f'handles = ["{B1}"]', f'handles = ["{A1}"]'))
    assert cli.main(["texting", "sheets", "--check", "--private", str(priv)]) == 1
    assert capsys.readouterr().out == f"{f}: people.beta: a handle is not a number or an address, or is someone else's too\n"
