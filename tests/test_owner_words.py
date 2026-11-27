"""[curate] owner_words in yerkd.toml: the pronouns the owner's rules use for them, which the conversion's picker reads with
"the owner", the install's names for them (owner_names) and "in reply to" to give a quote as the owner's. Unset, the picker
is the one before the setting, byte for byte; set, a rule that says she gives its quotes as the owner's by she."""
import ast
import re
from pathlib import Path

from test_lesson_store import FABLE2, MARKED, NAME, NOW, REASON, ledger_with, one_rule_model, reason_store

from yerk_ai import config, conversion

BEFORE = ("he", "him", "his", "the owner")      # the picker's words before the setting, in their order


def before(names: tuple[str, ...] = ()) -> re.Pattern[str]:
    """The picker's pattern as it was built before the setting, kept here word for word."""
    said = "|".join(re.escape(" ".join(w.split())) for w in (*BEFORE, *names) if w.strip())
    return re.compile(rf"(?:\b(?:{said})(?:['’]s)?\b(?:\W+\w+){{0,3}}|\bin reply to)\W*$", re.I)


def test_unset_the_words_are_he_him_and_his_and_the_pattern_is_the_one_before_it():
    assert conversion.owner_words({}) == conversion.owner_words({"curate": {}}) == conversion.HIS == ("he", "him", "his")
    assert conversion.owner_words({"curate": {"owner_words": ["she", " her ", ""]}}) == ("she", "her")
    assert conversion.owner_words({"curate": {"owner_words": "she"}}) == ("she",) and conversion.owner_words({"curate": {"owner_words": []}}) == ()
    for names in ((), (NAME,), (NAME, "Q. Example")):
        got, want = conversion.his_words(names), before(names)
        assert (got.pattern, got.flags) == (want.pattern, want.flags)
    assert "owner_words" in config.YERKD_WORDS["curate"] and "owner_names" in config.YERKD_WORDS["curate"]


def test_on_every_quote_in_the_rule_fixtures_the_picker_unset_answers_as_before():
    """Each string the converter's tests hold, cut before each of its quote marks: given_his with no setting answers what the
    pattern before it answers, with no name and with the install's name."""
    texts = [n.value for f in ("test_lesson_store.py", "test_curate_rules.py") for n in ast.walk(ast.parse((Path(__file__).parent / f).read_text()))
             if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    cuts = [t[:m.start()] for t in texts for m in re.finditer(r"[\"“]", t)]
    for cut in cuts:
        for names in ((), (NAME,)):
            assert conversion.given_his(cut, names) == bool(before(names).search(re.split(r"[.!?]\s", cut)[-1][-48:])), cut
    assert len(cuts) > 200 and any(conversion.given_his(c) for c in cuts) and not all(conversion.given_his(c) for c in cuts)


def dry(tmp_path: Path, name: str, toml: str, pronoun: str) -> tuple[dict, str]:
    """The dry run of one ledger rule whose reason on file is given as the owner's by `pronoun`, on a store of its own."""
    (root := tmp_path / name).mkdir()
    m, p = reason_store(root)
    marked = MARKED.replace(" he ", f" {pronoun} ")
    ledger_with(m, f"Never post the example ledger. {marked}")
    p = {**p, "lessons": [{**p["lessons"][0], "body": f'Never post it, since {pronoun} said "{REASON}". {marked}'}]}
    (priv := root / "private").mkdir()
    (priv / "yerkd.toml").write_text(toml)
    path = conversion.dry_run(priv, root / "state", m, NOW, model=one_rule_model(p), read=FABLE2)
    return conversion.loaded(path), path.read_text().replace(str(root), "ROOT")


def test_the_dry_run_is_the_same_bytes_unset_or_set_to_the_default_and_she_counts_only_once_set(tmp_path):
    unset, text = dry(tmp_path, "unset", "", "he")
    default, same = dry(tmp_path, "default", '[curate]\nowner_words = ["he", "him", "his"]\n', "he")
    assert unset["ready"] and unset["own"] == {"example-ledger": "tree/rules.md:9"} and same == text
    she, _ = dry(tmp_path, "she", '[curate]\nowner_words = ["she", "her"]\n', "she")
    assert she["ready"] and she["own"] == unset["own"]                          # given as the owner's by she, once set
    plain, _ = dry(tmp_path, "she-unset", "", "she")
    assert plain["own"] != unset["own"]                                         # unset, she is someone else


def test_what_the_program_says_about_the_owner_holds_no_he_his_or_him():
    """The words a model reads about the owner (the texting block and its two prompts, the watch lines, the taste head and its
    rubric) say the owner or they. A pronoun put back turns this red; the two outcome names are stored values, not words."""
    from yerk_ai import taste, words
    said = [words.TEXTING_TOLD, *words.TEXTING_MODE_SAID.values(), words.TEXTING_HELP, words.TEXTING_NOT_SAID, words.TEXTING_CHECK_CLAIMED,
            *words.REPLAY_SENDER_LIMITS, words.SENT_THERE, words.WATCH_UNANSWERED, taste.HEAD]
    pages = [(Path(words.__file__).parent / p).read_text() for p in ("texting/writer.md", "texting/check.md", "taste-rubric.md")]
    for text in (*said, *pages):
        assert not re.search(r"\b(?:he|him|his|himself)\b", text.replace("he-first", "").replace("he-too", ""), re.I), text[:80]
    for page in pages[:2]:                               # in the texting prompts "they" is the person texted, never the owner
        assert not re.search(r"the owner[^.;:]{0,60}\b(?:their side|they opened|their phone|as they would)\b", page), page[:80]
