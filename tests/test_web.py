"""Checks where the web plug-in (plugins/web, the add-on that ships here) meets the harness. Its spend rows reach the
morning diff (the daily 06:30 report), `yerk explain web` answers from the abilities file, the rooms template attaches it
by its command line, and the shipped mechanics send a picture on the web to its web_image. The spend rows follow
tests: `npm test` in its folder."""
import tomllib
from pathlib import Path

from test_observe import NOW, layer

from yerk_ai import abilities, cli, context, morning, status, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
from yerk_ai.ledger import Ledger

REPO = Path(__file__).resolve().parents[1]


def test_the_morning_diff_carries_yesterdays_spend_in_one_plain_line(tmp_path):
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led = Ledger(state / "ledger.sqlite")
    y0 = status.midnight(NOW, 1)
    lines, _ = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    assert not [x for x in lines if " spent " in x]                       # no paid call, no line
    for t, usd in ((y0 + 60, 0.007), (y0 + 120, 0.1), (y0 - 60, 5.0)):   # as the plug-in writes them; the last is the day before
        led.db.execute("INSERT INTO spend (t, room, tool, engine, usd, note) VALUES (?, ?, ?, ?, ?, ?)",
                       (t, "example-room", "web_search", "exa", usd, "billed"))
    lines, stamp = morning.build(priv, state, accounts, cool, led, {"started": y0 - 10, "units": []}, NOW)
    assert f"{words.PLAIN} spent yesterday: US$0.11 on 2 calls" in lines
    assert not [k for k in stamp["findings"] if "spent" in k]             # spend is said, never an alert


def test_yerk_explain_web_answers_from_the_abilities_file(tmp_path, capsys):
    assert cli.main(["explain", "web", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [f"{words.DONE} web: {words.ability('on')[1]}", abilities.manifest()["web"]["what"]]
    assert "web_image saves one picture" in lines[1]
    assert any(x.startswith("How to add the key: ") and "secrets/" in x and "exa.key" in x and "browserbase.key" in x for x in lines)


def test_yerk_explain_bookings_guest_answers_from_the_abilities_file_with_no_key(tmp_path, capsys):
    """Need 21 (2026-10-24): a Bookings page met login_required at every rung, its link short of the anonymous flag; the
    plug-in now reads it as a guest (plugins/web/test/fetch.test.mjs has the wall and the read), an ability of its own."""
    assert cli.main(["explain", "bookings-guest", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    ab = abilities.manifest()["bookings-guest"]
    assert lines[:2] == [f"{words.DONE} bookings-guest: {words.ability('on')[1]}", ab["what"]]
    assert "anonymous flag" in ab["what"] and "no sign-in" in ab["what"] and "key" not in ab


def test_yerk_explain_commons_answers_answers_from_the_abilities_file_with_no_key(tmp_path, capsys):
    """Need 37 (2026-11-02): web_fetch of a House of Commons written question's page returned the question alone, since
    each response loads after the page opens, by a request carrying the page's token and cookie; the plug-in's F1 now makes
    it (plugins/web/test/commons.test.mjs has the wall and the read), an ability of its own, which a room asked about a
    written question is handed whole."""
    assert cli.main(["explain", "commons-answers", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    ab = abilities.manifest()["commons-answers"]
    assert lines[:2] == [f"{words.DONE} commons-answers: {words.ability('on')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("loads only after it opens", "token and the cookie", "awaiting", "refuses"))
    assert "key" not in ab and ab["tier"] == "free"
    found = abilities.asked("what did the government answer to that written question?", None, abilities.manifest())
    assert [e["key"] for e in found] == ["commons-answers"]


def test_the_rooms_template_attaches_the_plug_in_by_its_command_line_under_the_names_the_rooms_allow():
    lines = [x.removeprefix("# ") for x in (REPO / "rooms.toml.example").read_text().splitlines()]
    at = lines.index("[defaults.mcp.web-ladder]")                 # every owner room's, from [defaults] (2026-10-23)
    server = tomllib.loads("\n".join(lines[at:at + 6]))["defaults"]["mcp"]["web-ladder"]
    assert server["args"][0].endswith("plugins/web/server.mjs") and server["args"][1] == "--private" and server["type"] == "stdio"
    assert server["args"][-2:] == ["--room", "{room}"] and (REPO / "plugins" / "web" / "server.mjs").is_file()
    tools = ("web_fetch", "web_search", "web_research", "web_research_result", "web_image")
    allow = tomllib.loads(next(x for x in lines if x.startswith('allow = ["mcp__web-ladder__')))["allow"]
    assert [x for x in allow if x.startswith("mcp__web-ladder__")] == [f"mcp__web-ladder__{t}" for t in tools]
    assert all(f'name: "{t}"' in (REPO / "plugins" / "web" / "lib" / "tools.mjs").read_text() for t in tools)


def test_the_mechanics_send_a_picture_on_the_web_to_web_image_and_read_never_to_the_shell():
    """Seen live: a room had an X post's picture URL and spent three minutes on curl in its shell (no
    network), web_fetch (pages, never bytes) and a mirror, then asked the owner for a screenshot."""
    text = context.MECHANICS.read_text().strip()
    line = next(x for x in text.splitlines() if "`web_image`" in x)
    assert "picture" in line and "Read" in line and "never curl" in line and "no network" in line
    assert context.chars(text) < budgets()["session_start"]["appended"]["mechanics"]


def test_the_mechanics_read_an_x_post_through_fxtwitter_first_never_the_x_com_page():
    """Seen live: web_fetch of the post's x.com link climbed to the paid L3n rung for 33.6 s and got its login page;
    fxtwitter's API answered at F1 in 0.3 s with the text and the picture's URL."""
    line = next(x for x in context.MECHANICS.read_text().splitlines() if "api.fxtwitter.com" in x)
    assert all(w in line for w in ("x.com or twitter.com", "`web_fetch` `https://api.fxtwitter.com/<user>/status/<id>` first",
                                   "never the x.com page, since", "`web_image`"))
