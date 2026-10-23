"""Checks the rules phase of `yerk curate` (curate.py): the rule files every room loads are read whole, and merges, cuts,
condensations and promotions are proposed into a numbered file, never applied. The owner's --apply writes them, backed up and fitted
file's line, refuses one that would pass it or that runs without its backup, and undo puts the files back byte-identical;
the machine room's card holds ten at most, goes once a day and says nothing at none. Example values only (decision 22)."""
import json
import shutil
import stat
from datetime import datetime, timezone

import pytest

from yerk_ai import curate, memory, morning, words
from yerk_ai.accounts import Cooldowns, load_accounts
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.ledger import Ledger

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-21"
EX = "<example>Landed: \"" + "Hello Ada, good to see you at the Example Cafe again, the table by the window is ready. " * 5 + "\"</example>"
RULES = ["---", "class: shared", "---", "# Rules: one live line per job, in every room", "",
         "- greet a guest: Say hello by name, since a cold open reads rude. (example-room, 2026-10-20 09:00:00+00:00) [m:1001]",
         "- send a photo: Send it as a file. (example-room, 2026-10-20 10:00:00+00:00) [m:1003]",
         "- greet a guest: Say hello by name, since a cold open reads rude; wave once. (example-room, 2026-10-21 09:00:00+00:00) [m:1002]",
         "- send a photo: Never as a file, inline only, since a file goes unopened. (example-room, 2026-10-21 10:00:00+00:00) [m:1004]",
         f"- tell the menu: Name the soup first, then the bread, then the price on its own line {EX}, then stop. Name the soup first, "
         "then the bread. (example-room, 2026-10-21 11:00:00+00:00) [m:1005]", ""]
SAID = " Name the soup first, then the bread."               # RULES[9]'s clause said twice: the prose a condensation cuts first
HELD = "- close the shop: Lights off, door locked, keys in the box. (example-room, 2026-10-21 11:30:00+00:00) [m:1006]"
BOOK = ["# Rulebook", "", "<working>", "- Read the file before you answer, since a guess reads as a fact.",
        "- 🔴 SUPERSEDED: Answer in French.", "- Quote a day only from its line, memory/daily/2026-10-19.md:99 for the menu.", "</working>", ""]


def caps(monkeypatch, rules):
    monkeypatch.setattr(curate, "budgets", lambda: {"session_start": {"appended": {"rules": rules, "rulebook": 14000}}})


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """The example layer as one owner room and a machine room, a rules file with two rules on one job, two that disagree
    and one past 600 characters, a rule held in OVERFLOW, a rulebook with a superseded line and a lost day-log cite, and
    the week's evidence: a shadow bounce, a rated reply and a `yerk` line."""
    priv, state = tmp_path / "private", tmp_path / "state"
    shutil.copytree(EXAMPLE_PRIVATE, priv)
    shutil.rmtree(priv / "memory" / "daily")
    (daily := priv / "memory" / "daily").mkdir()
    at = datetime(2026, 10, 21, 8, tzinfo=timezone.utc)
    (daily / f"{DAY}.md").write_text(f"# {DAY}\n\n" + memory.format_line(at, "example-room", "discord:100", "yerk", "", words.RULE_STRIPPED.format(
        job="greet a guest", cap=12500), "shared", "m21-1") + "\n")
    (priv / "rooms.toml").write_text('[rooms.example-room]\nchannel = "discord:100"\nrulebook = "rulebook.md"\n\n'
                                     '[rooms.example-ops]\nchannel = "discord:101"\nrole = "machine"\n')
    (priv / "yerkd.toml").write_text('[memory]\ntimezone = "UTC"\n')
    (priv / "rulebook.md").write_text("\n".join(BOOK))
    (priv / "memory" / "rules.md").write_text("\n".join(RULES))
    (priv / "memory" / "rules-overflow.md").write_text(f"# Rules held: past the rules file's cap, in no room\n\n{HELD}\n")
    (priv / "taste.jsonl").write_text(json.dumps({"ts": "2026-10-21T08:30:00", "verdict": "pos", "signal": "greet a guest by name, that was good",
                                                  "reply_id": "r1", "reply": "Hello Ada", "room": "example-room"}) + "\n")
    state.mkdir()
    Ledger(state / "ledger.sqlite").shadow(t=NOW - 3600, room="example-room", session_id="s1", turn="t1", rule="photo", detail="sent as a file", reply_chars=80)
    caps(monkeypatch, 12500)
    return priv, state, memory.settings(priv, state, {"timezone": "UTC"})


def squeeze(monkeypatch, priv, state, s):
    """The rules file's line set so the held rule fits only once the file's other proposals are in."""
    props, _, _ = curate.propose(priv, state, s, NOW)
    f = (s.root / "rules.md").resolve()
    saved = sum(q["saved"] for q in props if q["kind"] != "promote" and all(e[0] == str(f) for e in q["edits"]))
    caps(monkeypatch, cap := curate.part("rules", f, f.read_text()) - saved + len(HELD) + 1 + memory.HEADROOM)
    return cap


def kinds(props):
    return {q["kind"]: q for q in reversed(props)}


def test_the_proposals_are_a_numbered_file_of_before_after_why_and_evidence_and_no_rule_file_changes(tree, monkeypatch):
    priv, state, s = tree
    files = [s.root / "rules.md", s.root / "rules-overflow.md", priv / "rulebook.md"]
    before = {p: p.read_bytes() for p in files}
    assert len(RULES[9]) > 600
    squeeze(monkeypatch, priv, state, s)
    r = curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})
    props, by = r["proposals"], kinds(r["proposals"])
    assert [q["n"] for q in props] == list(range(1, len(props) + 1)) and set(by) == {"merge", "cut", "condense", "promote"}
    merge = by["merge"]
    assert merge["label"] == "greet a guest + greet a guest" and merge["edits"][1][3] == "" and merge["saved"] > 0
    assert merge["edits"][0][3] == ("- greet a guest: Say hello by name, since a cold open reads rude. wave once. (example-room, 2026-10-20 "
                                    "09:00:00+00:00; example-room, 2026-10-21 09:00:00+00:00) [m:1001,1002]")
    assert "taste 1, yerk 1" in merge["evidence"] and "taste.jsonl:1 pos" in merge["evidence"] and "memory/daily/2026-10-21.md:3" in merge["evidence"]
    cuts = {q["edits"][0][2]: q for q in props if q["kind"] == "cut"}
    assert set(cuts) == {RULES[6], BOOK[4], BOOK[5]}
    assert "says otherwise" in cuts[RULES[6]]["why"] and RULES[8] in cuts[RULES[6]]["why"] and "shadow 1" in cuts[RULES[6]]["evidence"]
    assert "daily/2026-10-19.md:99" in cuts[BOOK[5]]["why"] and "superseded" in cuts[BOOK[4]]["why"]
    short = by["condense"]["edits"][0][3]
    assert short == RULES[9].replace(SAID, "") and EX in short and "kept: its 1 example, since memory/rules.md has room" in by["condense"]["why"]
    assert by["promote"]["needs"] and by["promote"]["edits"][0][1:] == [0, "", HELD] and by["promote"]["saved"] < 0
    page = (s.root / "rules-proposals" / f"{DAY}.md")
    text = page.read_text()
    assert stat.S_IMODE(page.stat().st_mode) == 0o600 and text.startswith("---\nclass: private\n---\n# Rule proposals, 2026-10-21")
    assert all(f"## {q['n']}. {q['kind']}: " in text for q in props)
    assert text.count("\nBEFORE\n") == text.count("\nAFTER\n") == text.count("\nWHY: ") == text.count("\nEVIDENCE: ") == len(props)
    assert f"memory/rules.md, appended: {HELD}" in text and "memory/rules-overflow.md:3: (blank)" in text
    assert "memory/rules.md: " in text and "characters now" in text and "with every proposal applied" in text
    assert {p: p.read_bytes() for p in files} == before


def test_two_proposals_apply_under_the_line_backed_up_and_undo_puts_the_files_back_byte_identical(tree, monkeypatch):
    priv, state, s = tree
    cap, f = squeeze(monkeypatch, priv, state, s), s.root / "rules.md"
    by, was = kinds(curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})["proposals"]), f.read_bytes()
    out = curate.apply(priv, state, DAY, f"{by['merge']['n']},{by['condense']['n']}", now=NOW)
    rows = f.read_text().split("\n")
    assert len(rows) == len(RULES) and rows[7] == "" and rows[5].endswith("[m:1001,1002]") and rows[9] == RULES[9].replace(SAID, "")
    assert curate.part("rules", f.resolve(), f.read_text()) <= cap and any(x.endswith(f" of {cap:,}") for x in out)
    assert (priv / "backups" / f"curate-{DAY}" / "memory.rules.md").read_bytes() == was and not (state / curate.RUN).exists()
    assert curate.apply(priv, state, DAY, "undo", now=NOW) == ["rules.md: put back as it was"] and f.read_bytes() == was


def test_an_apply_that_would_take_a_file_past_its_line_is_refused_whole(tree, monkeypatch):
    priv, state, s = tree
    squeeze(monkeypatch, priv, state, s)
    by = kinds(curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})["proposals"])
    files = [s.root / "rules.md", s.root / "rules-overflow.md"]
    before = {p: p.read_bytes() for p in files}
    with pytest.raises(RuntimeError, match="past its line"):
        curate.apply(priv, state, DAY, str(by["promote"]["n"]), now=NOW)                    # its needs not applied
    assert {p: p.read_bytes() for p in files} == before and not (priv / "backups").exists()
    curate.apply(priv, state, DAY, ",".join(str(n) for n in [*by["promote"]["needs"], by["promote"]["n"]]), now=NOW)
    assert HELD in (s.root / "rules.md").read_text() and HELD not in (s.root / "rules-overflow.md").read_text()
    with pytest.raises(RuntimeError, match="changed since it was proposed"):
        curate.apply(priv, state, DAY, str(by["merge"]["n"]), now=NOW)                      # applied already


def test_an_apply_that_writes_without_its_backup_is_refused_and_put_back(tree, monkeypatch):
    priv, state, s = tree
    by = kinds(curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})["proposals"])
    was = (f := s.root / "rules.md").read_bytes()

    def mutant(path, backup, fix, dry):                  # the mutation: the rewrite with its backup copy left out
        parts = [x + b"\n" for x in path.read_bytes().split(b"\n")]
        parts[-1] = parts[-1][:-1]
        path.write_bytes(b"".join(y if (y := fix(x)) is not None else x for x in parts))
        return [1]
    monkeypatch.setattr(curate.doctor, "rewrite", mutant)
    with pytest.raises(RuntimeError, match="without its backup copy"):
        curate.apply(priv, state, DAY, str(by["merge"]["n"]), now=NOW)
    assert f.read_bytes() == was and not (state / curate.RUN).exists()


def test_the_card_names_ten_at_most_each_on_a_phone_line_and_is_silent_at_none():
    assert curate.card(DAY, []) == []
    props = [{"n": n, "kind": "merge", "label": f"rule number {n} with a long name + rule {n + 1}", "saved": 100 - n, "rows": n} for n in range(1, 13)]
    lines = curate.card(DAY, props)
    assert len(lines) == 13 and lines[0].startswith(words.WORKING) and lines[1].startswith("1. merge: rule") and lines[1].endswith("(-99 chars)")
    assert lines[-2] == "and 2 more in the proposals file" and lines[-1] == f"apply: yerk curate --apply {DAY} N"
    assert all(words.width(x) <= words.PHONE for x in lines) and not any(x.startswith(("11.", "12.")) for x in lines)


def test_the_card_goes_to_the_machine_room_once_a_day_never_dry_and_never_with_no_proposal(tree):
    priv, state, s = tree
    sent: list = []

    def post(p):
        sent.append(p)
        return {"ok": True}
    assert curate.rules(priv, state, s, True, NOW, post)["card"] and sent == [] and not (s.root / "rules-proposals").exists()
    curate.rules(priv, state, s, False, NOW, post)
    curate.rules(priv, state, s, False, NOW + 60, post)
    assert len(sent) == 1 and sent[0]["machine"]["room"] == "example-ops" and sent[0]["machine"]["key"] == f"curate-rules-{DAY}"
    (s.root / "rules.md").write_text("\n".join(RULES[:6]) + "\n")
    (priv / "rulebook.md").write_text("\n".join(BOOK[:4]) + "\n")
    (s.root / "rules-overflow.md").unlink()
    r = curate.rules(priv, state, s, False, NOW + 86400, post)
    assert r["proposals"] == [] and r["card"] == [] and len(sent) == 1
    with pytest.raises(RuntimeError, match="no proposals of 2026-10-21 on file; the newest are of 2026-10-22"):
        curate.apply(priv, state, DAY, "1", now=NOW + 86400)


def test_the_nightly_run_proposes_after_the_promotion_and_its_dry_run_writes_nothing(tree, capsys):
    priv, state, s = tree
    files = [s.root / "rules.md", s.root / "rules-overflow.md", priv / "rulebook.md"]
    before = {p: p.read_bytes() for p in files}
    r = curate.run(priv, state, dry=True, now=NOW, model=lambda p, t: "")
    assert r["rules"][0].startswith(words.WORKING) and not (s.root / "rules-proposals").exists() and not (state / curate.PROPOSED).exists()
    r = curate.run(priv, state, now=NOW, model=lambda p, t: "")
    assert r["rules"] == json.loads((state / curate.LAST).read_text())["rules"] and (s.root / "rules-proposals" / f"{DAY}.md").is_file()
    assert {p: p.read_bytes() for p in files} == before


def test_one_message_that_filed_two_jobs_is_never_a_merge_and_one_on_overlapping_jobs_is(tree):
    """PR 92's cold read: the live dry run merged two rules of different jobs filed from one message. One message alone is
    no merge; with overlapping jobs it is."""
    priv, state, s = tree
    a = ("- state what a person decided or how they count: Read their own words this turn and quote them beside the claim, because a note "
         "about them, his included, drops the hedge; test a rule drawn from their words on one worked example first, since one answer covers "
         "one case; with no words on file, write \"proposed, not confirmed\". (example-room, 2026-10-21 12:46:47+00:00) [m:1007]")
    b = ("- send a document or a paste in his name: Write the claims sheet first, in the work folder beside the file (claim, source line, "
         "quote, one row each), then send, because \"every line traced\" with no sheet is a self-report. (example-room, 2026-10-21 12:46:47+00:00) [m:1007]")
    c = "- send a photo: Send it as a file. (example-room, 2026-10-21 13:00:00+00:00) [m:1008]"
    d = "- send a photo album: Name the album in the message before it. (example-room, 2026-10-21 13:00:00+00:00) [m:1008]"
    (s.root / "rules.md").write_text("\n".join([*RULES[:5], a, b, c, d, ""]))
    props, _, _ = curate.propose(priv, state, s, NOW)
    assert not any(e[2] in (a, b) for q in props for e in q["edits"])
    merges = [q for q in props if q["kind"] == "merge"]
    assert [[e[2] for e in q["edits"]] for q in merges] == [[c, d]] and merges[0]["why"] == "filed from the same message (1008) on overlapping jobs"


def test_a_condensation_takes_out_quotes_and_examples_never_a_reason_or_a_clause_of_the_rules_own():
    """A quote in parentheses may go; one that holds a because, since or so-that clause stays, and so does one with a clause
    of the rule's own after the quote (the live rules.md:10 shape: a condensation that dropped it was proposed)."""
    keep = [' (Ada, Oct 20: "see how the kitchen pins it on the board"; the card\'s Copy button gives plain text)',
            ' (Ada, Oct 20: "plain cards only, so that the printer reads them")', ' (Ada, Oct 20: "pin it on the left", because the right side is the oven)']
    quote, trail = ' (Ada, Oct 20: "a picture of the dish goes after the steps")', " (example-room, 2026-10-21 11:00:00+00:00) [m:1009]"
    x = (f"- hand over a recipe: One recipe per message, alone, the steps numbered{keep[0]}, one card per dish{keep[1]}, the photo last{quote}, "
         f"the oven note first{keep[2]}, then stop {EX}.{trail}")
    assert len(x) > curate.LONG and curate.condensed(x) == x.replace(quote, "") and curate.condensed(x, True) == x.replace(quote, "").replace(f" {EX}", "")
    y = "- hand over text to paste: " + "One paste per message, alone, under the line or it splits, " * 10 + keep[0][1:] + "." + trail
    assert len(y) > curate.LONG and curate.condensed(y) == y


def test_a_bullet_whose_only_colon_is_in_an_example_loses_its_clause_said_twice_first_and_its_examples_only_when_asked():
    """The live mom rulebook's line 72 shape: a hand-kept bullet's first colon is in its first example, and that example stayed."""
    x = ("- What a guest asks here is an order to fill now, " + "the kitchen reads it as it comes and the bill waits for the end of the meal; " * 6
         + '<example>Bounced, Oct 20 14:53: "I need the owner\'s word to change the menu."</example> '
         + '<example>Bounced, Oct 20 14:54: "orders count as notes rather than requests".</example>')
    y = curate.condensed(x)
    assert len(x) > curate.LONG and y.count("the bill waits") == 1 and memory.EXAMPLE.findall(y) == memory.EXAMPLE.findall(x)
    assert curate.condensed(x, True) == memory.EXAMPLE.sub("", y) and "<example>" not in curate.condensed(x, True)


MOM = ("- A menu she will show guests opens with the soup and keeps to the price, the detail under it for whoever asks, because "
       "guests can ask the waiter themselves. " + " ".join(f'<example>Bounced, Oct {d}: "{w} Way too long for a menu, the guests at the long table '
       'stopped reading after the soup and never found the price at all."</example>' for d, w in ((3, "Too much."), (5, "Oh my."), (7, "Again."))))


def test_a_condensation_keeps_its_examples_while_the_file_has_room_and_says_what_it_kept(tree):
    """The 2026-10-23 three, declined: each took a live rulebook line's examples out with the file 3,451 characters under its line.
    A line with room keeps them; a line whose only cut is its examples is no proposal at all."""
    priv, state, s = tree
    (book := priv / "rulebook.md").write_text(book.read_text() + MOM + "\n")
    assert len(MOM) > curate.LONG
    props, _, _ = curate.propose(priv, state, s, NOW)
    by = kinds(props)
    assert by["condense"]["edits"][0][3] == RULES[9].replace(SAID, "") and EX in by["condense"]["edits"][0][3]
    assert by["condense"]["why"].startswith(f"{len(RULES[9]):,} characters, past 600: its prose said twice or hedged out, ")
    assert "; kept: its 1 example, since memory/rules.md has room (" in by["condense"]["why"]
    assert not any(e[2] == MOM for q in props for e in q["edits"])


def test_its_examples_go_only_once_the_file_is_past_its_line_even_without_the_rest(tree, monkeypatch):
    priv, state, s = tree
    caps(monkeypatch, 600)
    props, _, _ = curate.propose(priv, state, s, NOW)
    q = kinds(props)["condense"]
    assert q["edits"][0][3] == RULES[9].replace(SAID, "").replace(f" {EX}", "") and "<example>" not in q["edits"][0][3]
    assert "its prose said twice or hedged and its 1 example out" in q["why"] and "since memory/rules.md is past its line even without the rest (" in q["why"]
    assert q["why"].endswith("of 600), every other word its own")


def test_its_examples_stay_when_the_files_other_proposals_bring_it_under_its_line(tree, monkeypatch):
    """Past its line now, under it with the merge, the cut and the prose cut in: the file has room, so the example stays."""
    priv, state, s = tree
    f = (s.root / "rules.md").resolve()
    caps(monkeypatch, curate.part("rules", f, f.read_text()) - 1)
    q = kinds(curate.propose(priv, state, s, NOW)[0])["condense"]
    assert q["edits"][0][3] == RULES[9].replace(SAID, "") and "; kept: its 1 example, since memory/rules.md has room (" in q["why"]


def test_the_prose_cut_takes_a_hedge_set_off_and_a_clause_said_again_whole_never_a_quote_a_fragment_or_a_narrower_rule():
    """Only prose: a hedge set off by a comma, and a clause that is whole comma-parts of an earlier one, word for word. A clause
    of two words, one holding a quote, and one that drops an earlier clause's "on weekends" stay; "a change of course" is no hedge."""
    trail = " (example-room, 2026-10-21 11:00:00+00:00) [m:1010]"
    said = ['Name the soup first, then the bread, of course, then stop.', 'Then stop.', 'Greet the guest, then wait.',
            'Greet the guest "by name", then wait.', 'Never pour before nine on weekends.', 'Never pour before nine.',
            'Announce a change of course to the kitchen, basically.', 'Name the soup first, then the bread.']
    x = "- serve a table: " + " ".join(said) + trail
    want = ("- serve a table: Name the soup first, then the bread then stop. " + " ".join(said[1:6])
            + " Announce a change of course to the kitchen." + trail)
    assert curate.condensed(x) == curate.condensed(x, True) == want
    assert curate.why_condensed(x, want, 1, 2, "rules.md").startswith(f"{len(x):,} characters, past 600: its prose said twice or hedged out, ")
    z = "- serve a table: Greet the guest,  then wait." + trail
    assert curate.why_condensed(z, curate.condensed(z), 1, 2, "rules.md").startswith(f"{len(z):,} characters, past 600: its spacing out, ")


def test_the_morning_lists_the_nights_proposals_by_kind_and_number_for_apply(tree):
    priv, state, s = tree
    assert curate.rules_lines(state, NOW) == []
    props = curate.rules(priv, state, s, False, NOW, post=lambda p: {"ok": True})["proposals"]
    lines = [x for _, x in curate.rules_lines(state, NOW + 3600)]
    nums = {k: [q["n"] for q in props if q["kind"] == k] for k in dict.fromkeys(q["kind"] for q in sorted(props, key=lambda q: q["n"]))}
    assert lines[0] == f"{words.WORKING} rules: {len(props)} proposed, -{sum(max(0, q['saved']) for q in props):,} chars"
    assert lines[1:-1] == [f"{k}: {curate.spans(ns)}" for k, ns in nums.items()] and lines[-1] == f"apply: yerk curate --apply {DAY} N"
    assert f"condense: {kinds(props)['condense']['n']}" in "\n".join(lines) and all(words.width(x) <= words.PHONE for x in lines)
    diff, _ = morning.build(priv, state, load_accounts(priv / "accounts.toml"), Cooldowns(state / "cooldowns.json"),
                            Ledger(state / "ledger.sqlite"), {"started": NOW - 86400, "units": []}, NOW + 3600)
    assert all(x in diff for x in lines) and curate.rules_lines(state, NOW + 2 * 86400) == []
    assert curate.spans([1, 2, 3, 5, 8, 9]) == "1-3, 5, 8, 9"
