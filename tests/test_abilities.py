"""Checks abilities.toml, the manifest of every ability Yerk AI has and every one it knows how to build but does not ship. The cases: every
described one carries what, how, tier, needs, price, free path and why it is not built, none holds the
shape of a piece of anyone's life (patterns, never names), `yerk abilities` stays one phone-width line per ability, `yerk explain` answers
every described one from its entry, and a room that names one, or asks "what can you do", gets the manifest's answer whole within the
recall cap, the size limit on the notes a message arrives with. The described entries are decision 26, the patterns rule decision 22."""
import json
import re

from starlette.testclient import TestClient

from yerk_ai import abilities, cli, hooks, mcp_server, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.engine import SessionTable

DOTS = (words.DONE, words.WORKING, words.NOT_DONE, words.NOTHING)

TAB = abilities.manifest()
DESCRIBED = {k: v for k, v in TAB.items() if v.get("state") == "described"}
DEV = ("/home/", "exited", "Result=", "SERVICE_RESULT", "systemd", "Traceback", "Errno", "<t:", "—")
ROLE = (r"CEO|CIO|CTO|CFO|VP|boss|manager|director|recruiter|lawyer|doctor|mom|dad|mother|father"
        r"|brother|sister|wife|husband|girlfriend|boyfriend|cousin|aunt|uncle|grandma|grandpa|coworker|colleague|friend")
LIFE = {
    "a name and a role": re.compile(rf"\b[A-Z][a-z]+ [A-Z][a-z]+,? (?:(?:the|our|my|his|her|their) )?(?:{ROLE})\b"),
    "a #room": re.compile(r"(?<![\w&])#[A-Za-z][\w-]+"),
    "an @handle": re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"),
    "a dollar amount": re.compile(r"\$\s?\d"),
    "a street address": re.compile(r"\b\d{1,5} [A-Z][a-z]+(?: [A-Z][a-z]+)? (?:St|Street|Ave|Avenue|Rd|Road|Blvd"
                                   r"|Boulevard|Dr|Drive|Ln|Lane|Way|Cres|Crescent|Ct|Court|Pl|Place)\b"),
    "a date": re.compile(r"\b(?:19|20)\d\d-\d\d-\d\d\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? "
                         r"\d{1,2}\b"),
}
# everyday messages that ask about no ability: none of them may pull an ability's answer into the room
ORDINARY = ["what is on the list today?", "can you back up a second", "are you down to play later",
            "I lost my glasses again", "it's on my mac somewhere", "I have to fill out a form for work",
            "I watched a youtube video about it", "thanks, that works", "what time is it", "ok send it"]


def life(text: str) -> list[str]:
    return [f"{kind}: {m.group()}" for kind, rx in LIFE.items() for m in rx.finditer(text)]


def text_of(ab: dict) -> str:
    return json.dumps(ab, ensure_ascii=False)


def test_every_entry_says_built_or_described():
    assert TAB and all(ab.get("state") in ("built", "described") for ab in TAB.values()), \
        {k: v.get("state") for k, v in TAB.items()}
    assert len(DESCRIBED) > 40 and "voice" in DESCRIBED


def test_every_described_entry_has_what_how_tier_needs_cost_free_path_and_why():
    for name, ab in DESCRIBED.items():
        for field in ("what", "needs", "price", "free_path", "why_not"):
            assert isinstance(ab.get(field), str) and ab[field].strip(), (name, field)
        assert ab["how"] and all(isinstance(x, str) and x.strip() for x in ab["how"]), name
        assert ab["tier"] in ("free", "key", "paid"), name
        assert ab["tier"] == "free" or ab.get("provider"), (name, "a key or paid ability names its provider kind")
        assert ab["asked_as"] and all(p == p.lower() for p in ab["asked_as"]), name
        assert ab["what"].endswith(".") and not re.search(r"\.\s+[A-Z]", ab["what"]), (name, "one sentence")
        if "upgrade" in ab:
            assert TAB.get(ab["upgrade"], {}).get("state") == "built", (name, "upgrade names a built ability")
        if "layer" in ab:
            assert isinstance(ab["layer"], int) and 1 <= ab["layer"] <= 10, name


def test_no_described_entry_holds_a_piece_of_anyones_life_or_a_word_a_room_must_not_see():
    hits = {name: life(text_of(ab)) + [w for w in DEV if w in text_of(ab)] for name, ab in DESCRIBED.items()}
    assert not {k: v for k, v in hits.items() if v}, hits


def test_the_life_patterns_catch_what_they_are_for():
    planted = {"a name and a role": "ask Jane Doe, the manager", "a #room": "post it in #general",
               "an @handle": "ping @someone", "a dollar amount": "it costs $20 a month",
               "a street address": "drive to 12 Main Street", "a date": "due 2030-01-02, or on March 3"}
    for kind, text in planted.items():
        assert any(h.startswith(kind) for h in life(text)), (kind, text)
    assert not life("Reads a spreadsheet and keeps a net worth number a day, paid to the provider.")


def test_yerk_abilities_is_one_phone_width_line_per_ability_built_first(tmp_path, capsys):
    assert cli.main(["abilities", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    shown, tail = lines[:-1], lines[-1]
    assert tail.startswith("yerk explain") and len(shown) == len(TAB)
    assert all(x[:1] in "".join(DOTS) and words.width(x) <= words.PHONE for x in shown), shown
    names = [x.split(" ", 1)[1].split(":", 1)[0] for x in shown]
    assert sorted(names) == sorted(TAB)
    states = [TAB[n]["state"] for n in names]
    assert states == sorted(states, key=lambda st: st == "described")          # built ones, then described
    assert sum(x.endswith(": " + words.ability("described")[1]) for x in shown) == len(DESCRIBED)


def test_yerk_explain_answers_every_described_ability_from_its_entry(tmp_path, capsys):
    for name, ab in DESCRIBED.items():
        assert cli.main(["explain", name, "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"{words.NOTHING} {name}: {words.ability('described')[1]}" and lines[1] == ab["what"]
        at = lines.index("How it would be built:")
        assert lines[at + 1:at + 1 + len(ab["how"])] == [f"- {x}" for x in ab["how"]], name
        for want in (f"Needs: {ab['needs']}", f"Cost: {ab['price']}", f"Free path: {ab['free_path']}",
                     f"Why it is not built: {ab['why_not']}"):
            assert want in lines, (name, want)
        if "upgrade" in ab:
            assert f"Built part: {ab['upgrade']} (yerk explain {ab['upgrade']})" in lines, name
        assert not [w for w in DEV if w in "\n".join(lines)], name


def test_each_ability_is_found_by_its_own_phrases_and_its_answer_fits_whole(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    cap = int(y.budgets["per_message"]["on_hit"]["recall"])
    seen: dict[str, str] = {}
    for name, ab in TAB.items():
        for phrase in ab.get("asked_as", []):
            assert phrase not in seen, (phrase, seen.get(phrase), name)
            seen[phrase] = name
            found = abilities.asked(f"hey, {phrase}?", y.memory, TAB)
            assert name in [e["key"] for e in found], (name, phrase)
            mine = [e for e in found if e["key"] == name]
            assert hooks.render_recall(mine, [], cap), (name, "its answer must fit the recall cap whole")


def test_ordinary_messages_pull_no_ability_into_the_room(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    assert {m: [e["key"] for e in abilities.asked(m, y.memory, TAB)] for m in ORDINARY} == {m: [] for m in ORDINARY}


def test_a_room_asking_what_can_you_do_gets_every_ability_by_state(tmp_path, capsys):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-w", "one")
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    with TestClient(app) as c:
        out = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-w",
                                    "prompt": "so what can you do?"}).json()
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "- abilities, a Yerk AI ability (on): Says what Yerk AI can do" in ctx and "(yerk explain abilities)" in ctx
    assert f"{words.ability('described')[1]} ({len(DESCRIBED)}):" in ctx
    assert all(re.search(rf"[ (:]{re.escape(n)}[,.]", ctx) for n in TAB), [n for n in TAB if n not in ctx]
    assert cli.main(["explain", "abilities", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    said = capsys.readouterr().out
    assert said.startswith(f"{words.DONE} abilities: on") and all(n in said for n in TAB)
