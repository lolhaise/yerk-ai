"""Checks where the web plug-in (plugins/web, the add-on that ships here) meets the harness. Its spend rows reach the
morning diff (the daily 06:30 report), `yerk explain web` answers from the abilities file, the rooms template attaches it
by its command line, and the shipped mechanics send a picture on the web to its web_image. The spend rows follow
decision 32. The plug-in itself (plugins/web, JavaScript, outside the code count) has its own tests: `npm test` in its folder."""
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from test_observe import NOW, layer

from yerk_ai import abilities, cli, context, morning, retention, status, words
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


def test_the_morning_diff_says_each_web_plan_alert_once_the_day_after_it_was_written(tmp_path):
    """The plug-in's plan alerts (plugins/web: fetch/alerts.log a text line each, search/alerts.log a JSON line each) reach
    the owner: a line written yesterday opens the diff, a line written today waits for tomorrow's, and the delivered stamp
    keeps how far they were said, so none is said twice."""
    priv, state, accounts, cool = layer(tmp_path, machine=None)
    led, y0, today = Ledger(state / "ledger.sqlite"), status.midnight(NOW, 1), status.midnight(NOW)
    local = lambda t: datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")    # noqa: E731 (util.js isoLocal)
    heads_up = ". Nothing was refused; this is a budget heads-up only.\n"
    (state / "web" / "fetch").mkdir(parents=True)
    (state / "web" / "search").mkdir(parents=True)
    (state / "web" / "fetch" / "alerts.log").write_text(           # as util.js recordUsage writes them
        f"{local(y0 - 3600)} fetch-plus month=2026-10 fetchCalls passed 5000 (now 5001, of which proxied 3){heads_up}"
        f"{local(y0 + 3600)} fetch-plus month=2026-10 proxy traffic passed 1.00 GB (now 3.446 GB, Browserbase's own meter){heads_up}"
        f"{local(today + 60)} fetch-plus month=2026-10 browser time passed 150 h (now 150.20 h over 900 sessions){heads_up}")
    (state / "web" / "search" / "alerts.log").write_text(json.dumps(  # as search/util.js countMonthly writes it
        {"ts": datetime.fromtimestamp(y0 + 7200, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
         "engine": "exa", "count": 5000, "month": "2026-10"}) + "\n")
    live = {"started": y0 - 10, "units": []}
    lines, stamp = morning.build(priv, state, accounts, cool, led, live, NOW)
    said = [f"{words.WORKING} web proxy: 3.4 GB in Oct > 1 GB", f"{words.WORKING} Exa searches reached 5,000 in Oct"]
    assert [x for x in lines if " in Oct" in x] == said                  # not the day before's (its diff said it), not today's
    assert lines[0].startswith(f"{words.NOT_DONE} morning: ") and lines.index(said[0]) < lines.index(f"{words.PLAIN} {words.NO_SHADOW}")
    (state / "morning").mkdir(exist_ok=True)
    (state / "morning" / "last.json").write_text(json.dumps(stamp))      # delivered
    lines, _ = morning.build(priv, state, accounts, cool, led, live, NOW + 86400)
    assert [x for x in lines if " in Oct" in x] == [f"{words.WORKING} web browser: 150.2 h in Oct > 150 h"]    # once each


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


def test_yerk_explain_oebb_trains_answers_from_the_abilities_file_and_a_room_s_train_ask_reaches_it(tmp_path, capsys):
    """Need 136 (2026-11-22): a room asked for a dated journey's train tickets and times met a captcha on every rung of
    ÖBB's ticket shop and answered off a reseller's times of another month. web_fetch now reads a shop link's trains from
    Scotty and their fares from the shop's API, never up the ladder (plugins/web/test/oebb.test.mjs has the wall and the
    read), an ability of its own, free, which a room asked for train tickets and times is handed whole."""
    assert cli.main(["explain", "oebb-trains", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    ab = tab["oebb-trains"]
    assert lines[:2] == [f"{words.DONE} oebb-trains: {words.ability('on')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("stationOrigName=FROM", "never climbs", "next five trains", "Scotty", "lowest fare", "bot wall", "link's shape"))
    assert "key" not in ab and ab["tier"] == "free" and "no browser" in ab["needs"]
    found = abilities.asked("can you get me train tickets and times from Example Town to Placeholder Valley on two dates in January?", None, tab)
    assert [e["key"] for e in found] == ["oebb-trains"]
    assert [e["key"] for e in abilities.asked("what does the öbb railjet cost to Example Town?", None, tab)] == ["oebb-trains"]
    tools = (REPO / "plugins" / "web" / "lib" / "tools.mjs").read_text()
    assert "const trip = oebbJourney(url);" in tools and "An ÖBB ticket shop link (" in tools
    assert "(`yerk explain oebb-trains`)" in (REPO / "docs" / "GUIDE.md").read_text()

def test_yerk_explain_web_documents_answers_from_the_abilities_file_and_web_fetch_keeps_a_document_s_bytes(tmp_path, capsys):
    """Need 87 (2026-11-11): a room asked what a province's form asks for met web_fetch's binary wall on the form's own Word
    file and on two PDF copies of it, "these tools read no other binary file", and answered off a forms list page and
    search previews. web_fetch now keeps a PDF or a Word file's bytes, saves the file in the room's work folder and answers
    with its words (plugins/web/test/tools.test.mjs has the wall and the read of each kind), an ability of its own, free."""
    assert cli.main(["explain", "web-documents", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    ab = tab["web-documents"]
    assert lines[:2] == [f"{words.DONE} web-documents: {words.ability('on')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("PDF", "Word file", "web_fetch", "work folder", "Read", "render"))
    assert ab["tier"] == "free" and "key" not in ab and "pdftotext" in ab["needs"]
    found = abilities.asked("can you read the pdf at this link? it's the form's pdf", None, tab)
    assert "web-documents" in [e["key"] for e in found]
    lib = REPO / "plugins" / "web" / "lib"
    wall = (lib / "fetch" / "ladder.js").read_text()
    assert "these tools read no other binary file" in wall and "web_fetch reads a PDF or a Word file itself" in wall
    assert "document: { kind, buf }" in (lib / "fetch" / "local.js").read_text() and "document: { kind, buf: bytes }" in wall
    tools = (lib / "tools.mjs").read_text()
    assert "if (p.document) return documentRead(" in tools and "A PDF or a Word file (.docx, .doc, .odt) comes back as its words" in tools
    assert "export async function readDocument" in (lib / "fetch" / "document.js").read_text()


def test_yerk_explain_research_ids_answers_from_the_abilities_file_on_with_the_exa_key(tmp_path, capsys):
    """Need 49 (2026-11-04): a room's web_research came back after 60 s as a timeout with no run id, so web_research_result had
    nothing to collect: Claude Code cut the call at the room's own one-minute limit on the server while the plug-in waited
    up to 170 s. The call's wait now fits the room's limit and each start carries a reference of Yerk's own, by which a run
    whose create went unanswered is found (plugins/web/test/tools.test.mjs has the wall and the way), an ability of its own,
    on with the Exa key the research runs on."""
    assert cli.main(["explain", "research-ids", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    ab = tab["research-ids"]
    assert lines[:2] == [f"{words.WORKING} research-ids: {words.ability('needs', provider='Exa')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("reference", "whole wait", "web_research_result", "run id"))
    found = abilities.asked("the web research timed out after a minute with no run id", None, tab)
    assert "research-ids" in [e["key"] for e in found]
    priv = tmp_path / "private"
    (priv / "secrets").mkdir(parents=True)
    off = SimpleNamespace(private=priv, embeddings_key=None)
    assert abilities.state("research-ids", ab, tab, off) == words.ability("needs", provider="Exa")
    (priv / "secrets" / "exa.key").write_text("exa-placeholder-key\n")
    assert abilities.state("research-ids", ab, tab, off) == words.ability("on")
    code = (REPO / "plugins" / "web" / "lib" / "research.mjs").read_text()
    assert "body.metadata = { [META]: ref }" in code and "findRun(key.value, id" in code   # the reference sent, and taken back


def test_yerk_explain_research_kept_answers_from_the_abilities_file_on_with_the_exa_key(tmp_path, capsys):
    """research-answers-2026-11-26: a web_research answer lived only in the tool's result, so a compacted context lost it
    and asking again paid again, and no run said where its sources disagreed or what it did not find. A completed run's
    answer is now kept as a file in the room's work folder, web_research_result with no run id lists the last 7 days',
    and every run is asked for its disagreements and its gaps (plugins/web/test/tools.test.mjs has each), an ability of
    its own, on with the Exa key the runs need."""
    assert cli.main(["explain", "research-kept", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    ab = tab["research-kept"]
    assert lines[:2] == [f"{words.WORKING} research-kept: {words.ability('needs', provider='Exa')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("kept as a file in the room's work folder", "web_research_result with no run id", "disagree", "did not find"))
    found = abilities.asked("can you pull up the earlier research on the venue?", None, tab)
    assert "research-kept" in [e["key"] for e in found]
    priv = tmp_path / "private"
    (priv / "secrets").mkdir(parents=True)
    off = SimpleNamespace(private=priv, embeddings_key=None)
    assert abilities.state("research-kept", ab, tab, off) == words.ability("needs", provider="Exa")
    (priv / "secrets" / "exa.key").write_text("exa-placeholder-key\n")
    assert abilities.state("research-kept", ab, tab, off) == words.ability("on")
    code = (REPO / "plugins" / "web" / "lib" / "research.mjs").read_text()
    assert "const kept = keepRun(run, {" in code and "export const STANDING = " in code   # the answer kept, the words sent


def test_the_plug_ins_kept_research_answers_sit_in_a_folder_the_nightly_sweep_leaves(tmp_path):
    """research-answers-2026-11-26: web_research keeps a completed run's answer in research/ of the room's work folder beside
    a file named KEEP (plugins/web/lib/research.mjs keepRun), which retention.kept() spares, so the sweep that takes every
    other file of the work folder after two days leaves the answers. Fails the day the plug-in's name and retention.KEEP differ."""
    code = (REPO / "plugins" / "web" / "lib" / "research.mjs").read_text()
    names = dict(re.findall(r'^export const (KEEP|KEPT_DIR) = "([^"]+)";$', code, re.M))
    assert names == {"KEEP": retention.KEEP, "KEPT_DIR": "research"}
    room = tmp_path / "rooms" / "a-room"
    answers, web = room / "work" / names["KEPT_DIR"], room / "work" / "web"
    answers.mkdir(parents=True)
    web.mkdir()
    (answers / names["KEEP"]).write_text("web_research keeps this room's research answers in this folder.\n")
    (answers / "2026-11-01-a-question-abc123.md").write_text("an answer\n")
    (web / "0123456789ab.png").write_bytes(b"png")
    assert retention.kept(room) == [answers]
    swept = [p for p, _ in retention.scratch(room)]
    assert web / "0123456789ab.png" in swept and not [p for p in swept if answers in p.parents]


def test_yerk_explain_worker_web_answers_from_the_abilities_file_and_a_worker_s_plug_in_writes_only_its_own_folder(tmp_path, capsys):
    """Need 109 (2026-11-18): a worker a room started with the web plug-in failed every web_fetch on EROFS, the lock of the
    free rungs' shared rate window in <state>/web/fetch, which its unit mounts read-only, and read its pages by a plain GET
    from its shell. The worker's server is still the room's table plus its id and folder, and its unit still sees the state
    folder read-only (no write under <state>/web: the worker's shell shares the plug-in's mounts); the plug-in, told it is
    in a worker, writes its state in that folder and counts the rooms' shared window with its own
    (plugins/web/test/tools.test.mjs has the wall, with the shared folder unwritable, and the way)."""
    from yerk_ai import workers
    assert cli.main(["explain", "worker-web", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    tab = abilities.manifest()
    ab = tab["worker-web"]
    assert lines[:2] == [f"{words.DONE} worker-web: {words.ability('on')[1]}", ab["what"]]
    assert all(w in ab["what"] for w in ("worker", "web plug-in", "`addons`", "own folder", "read-only", "web_fetch"))
    assert ab["tier"] == "free" and "key" not in ab and "web-ladder" in ab["needs"]
    assert [e["key"] for e in abilities.asked("web_fetch fails in a worker on every call", None, tab)] == ["worker-web"]
    rows = [x.removeprefix("# ") for x in (REPO / "rooms.toml.example").read_text().splitlines()]
    at = rows.index("[defaults.mcp.web-ladder]")
    web = tomllib.loads("\n".join(rows[at:at + 6]))["defaults"]["mcp"]["web-ladder"]
    state, folder = tmp_path / "state", tmp_path / "state" / "workers" / "0a1b2c"
    conf = {"allow": ["mcp__web-ladder__web_fetch", "mcp__web-ladder__web_search"], "mcp": {"web-ladder": web}}
    servers, tools = workers.room_addons(conf, ["web-ladder"], "0a1b2c", folder)
    assert tools == conf["allow"] and servers["web-ladder"]["args"] == web["args"]
    assert {k: v for k, v in servers["web-ladder"]["env"].items() if k.startswith("YERK_WORK")} == {"YERK_WORKER": "0a1b2c", "YERK_WORK_DIR": str(folder)}
    p = workers.Systemd(state).sandbox(folder, {})[1::2]
    assert f"ReadOnlyPaths={state}" in p and [x for x in p if x.startswith("ReadWritePaths=")] == [f"ReadWritePaths={folder}"]
    lib = REPO / "plugins" / "web" / "lib"
    common = (lib / "common.mjs").read_text()
    assert "env.YERK_WORKER" in common and "env.YERK_WORK_DIR" in common and 'own: worker ? path.join(path.resolve(folder), "web") : web' in common
    assert "also.flatMap((f) => inWindow(f, now))" in common
    assert (lib / "tools.mjs").read_text().count('...dirsOf(at, "') == 3
    assert "sharedStateDir" in (lib / "fetch" / "local.js").read_text() and "sharedStateDir" in (lib / "search" / "util.js").read_text()


def test_each_room_s_stdio_server_is_told_its_own_limit_so_web_research_answers_inside_it(tmp_path):
    """Need 49's wall as the room met it: a person room's own web-ladder table carried `timeout = 60000` (no [defaults] for
    it), Claude Code cut the call there and dropped its answer, run id and all, while the plug-in waited up to 170 s. Each
    stdio server in a room's mcp.json now carries its own limit in YERK_TOOL_TIMEOUT beside its own env, and the plug-in
    fits its wait inside it; an HTTP server, one with no limit and the room's own table are left as they were."""
    from yerk_ai import mcp_server
    web = {"type": "stdio", "command": "/usr/bin/node", "args": ["server.mjs", "--room", "a-person-room"], "timeout": 60000, "alwaysLoad": True}
    servers = {"web-ladder": web, "other": {"command": "x", "env": {"KEEP": "as is"}, "timeout": 90000},
               "far": {"type": "http", "url": "http://127.0.0.1:1/mcp", "timeout": 60000}, "open": {"type": "stdio", "command": "y"}}
    before = json.dumps(servers)
    cfg = json.loads(mcp_server.room_config("http://127.0.0.1:1/mcp", "a-person-room", tmp_path / "m.json", "t", servers).read_text())
    got = cfg["mcpServers"]
    assert got["web-ladder"] == {**web, "env": {"YERK_TOOL_TIMEOUT": "60000"}}
    assert got["other"]["env"] == {"KEEP": "as is", "YERK_TOOL_TIMEOUT": "90000"}
    assert got["far"] == servers["far"] and got["open"] == servers["open"] and json.dumps(servers) == before
    assert list(got) == ["web-ladder", "other", "far", "open", "yerk"] and got["yerk"]["timeout"] == 600_000
    lib = REPO / "plugins" / "web" / "lib"
    assert "env.YERK_TOOL_TIMEOUT" in (lib / "common.mjs").read_text() and "wait: waitIn(at.limit)" in (lib / "tools.mjs").read_text()


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
                                   "never the x.com page, even if that fetch fails, since", "`web_image`"))
