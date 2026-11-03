"""Checks the dialect list under [style] in yerkd.toml: your own filler words join the built-in filler lists when yerkd starts,
and the package ships none of them. NON-GOALS: the owner's dialect out of the package. A stranger's install reads "who is Ada zorp" as
a two-word name and "zorp nah" as no pushback; the owner's, with zorp in their dialect, reads the name alone and the pushback."""
import shutil

import pytest

from yerk_ai import words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd


def test_the_neutral_lists_hold_no_dialect_and_the_owners_words_join_them():
    try:
        assert words.asked("who is Ada zorp?") == ["Ada zorp"] and not words.PUSHBACK.search("zorp nah")
        assert words.PUSHBACK.search("bro nah") and words.PAST.search("bro what did I say") and not words.PAST.search("zorp what did I say")
        words.dialect(["Zorp", " zorp ", ""])
        assert words._DIALECT == ("zorp",) and words.dialect_alt() == "|zorp"
        assert words.asked("who is Ada zorp?") == ["Ada"] and words.PUSHBACK.search("zorp nah") and words.PAST.search("zorp what did I say")
    finally:
        words.dialect()
    assert words.asked("who is Ada zorp?") == ["Ada zorp"] and words.dialect_alt() == ""


def test_the_daemon_reads_the_dialect_from_yerkd_toml_and_refuses_anything_but_a_list(tmp_path):
    priv = tmp_path / "private"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    (priv / "yerkd.toml").write_text('[style]\ndialect = ["zorp"]\n')
    (tmp_path / "s1").mkdir()
    (tmp_path / "s2").mkdir()
    try:
        Yerkd(priv, tmp_path / "s1")
        assert words._DIALECT == ("zorp",)
    finally:
        words.dialect()
    (priv / "yerkd.toml").write_text('[style]\ndialect = "zorp"\n')
    with pytest.raises(SystemExit) as e:
        Yerkd(priv, tmp_path / "s2")
    assert str(e.value).startswith("yerkd.toml: [style] dialect must be a list of words") and "\n" not in str(e.value)
