"""Checks where the web plug-in (plugins/web, the add-on that ships here) meets the harness. Its spend rows reach the
answers from the abilities file, and the rooms template attaches it by its command line. The plug-in itself
(plugins/web, JavaScript, outside the code count) has its own tests: `npm test` in its folder."""
import tomllib
from pathlib import Path

from test_observe import NOW, layer

from yerk_ai import abilities, cli, morning, status, words
from yerk_ai.config import EXAMPLE_PRIVATE
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
    assert any(x.startswith("How to add the key: ") and "secrets/" in x and "exa.key" in x and "browserbase.key" in x for x in lines)


def test_the_rooms_template_attaches_the_plug_in_by_its_command_line_under_the_names_the_rooms_allow():
    lines = [x.removeprefix("# ") for x in (REPO / "rooms.toml.example").read_text().splitlines()]
    at = lines.index("[rooms.example-room.mcp.web-ladder]")
    server = tomllib.loads("\n".join(lines[at:at + 6]))["rooms"]["example-room"]["mcp"]["web-ladder"]
    assert server["args"][0].endswith("plugins/web/server.mjs") and server["args"][1] == "--private" and server["type"] == "stdio"
    assert (REPO / "plugins" / "web" / "server.mjs").is_file()
    tools = ("web_fetch", "web_search", "web_research", "web_research_result")
    assert tomllib.loads(next(x for x in lines if x.startswith('allow = ["mcp__web-ladder__')))["allow"] == [f"mcp__web-ladder__{t}" for t in tools]
    assert all(f'name: "{t}"' in (REPO / "plugins" / "web" / "lib" / "tools.mjs").read_text() for t in tools)
