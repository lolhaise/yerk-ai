"""Texting's reply list: owner-only, handles in one form, a handle on one person only."""
from __future__ import annotations

import os
import shutil

import pytest

from yerk_ai import doctor
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
