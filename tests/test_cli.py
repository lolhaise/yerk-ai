import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from yerk_ai import memory
from yerk_ai.config import budgets
from conftest import state_read_only

ROOT = Path(__file__).resolve().parents[1]


def yerk(*args):
    env = {k: v for k, v in os.environ.items() if k != "YERK_PRIVATE"}
    return subprocess.run([sys.executable, "-m", "yerk_ai.cli", *args], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=60)


def test_help_lists_the_commands():
    r = yerk("--help")
    assert r.returncode == 0 and "map" in r.stdout and "retention" in r.stdout and "probe" not in r.stdout   # tests/contract/


@state_read_only
def test_map_example_room_is_within_budget():
    r = yerk("map", "example-room")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "total" in r.stdout and "OVER" not in r.stdout


@state_read_only
def test_map_fails_on_the_planted_file():
    r = yerk("map", "planted-room", "--private", "tests/fixtures/over-budget")
    assert r.returncode == 1 and "OVER BUDGET: persona" in r.stdout


def test_map_shows_the_rules_file_as_a_part_of_its_own(tmp_path):
    (tmp_path / "memory" / "tree").mkdir(parents=True)          # the example layer's own tree sits in the state folder
    (tmp_path / "memory" / "tree" / "rules.md").write_text("# Rules\n\n- a job: done this way.\n")
    r = yerk("map", "example-room", "--state", str(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(rf"^  rules +\d+ +{budgets()['session_start']['appended']['rules']:,}  ok  \[\S+/rules\.md\]$", r.stdout, re.M)
    assert r.stdout.index("  pinned_memory ") < r.stdout.index("  rules ") < r.stdout.index("  recall_pack ")
    assert "rules.md" not in next(x for x in r.stdout.splitlines() if x.startswith("  rulebook "))


def test_map_unknown_room():
    assert yerk("map", "no-such-room").returncode == 2


def test_a_command_on_the_example_layer_says_so_once_on_stderr(tmp_path):
    """F9-12: with no --private and no $YERK_PRIVATE a command answers about the example layer in the repo against the real
    state folder; one line on stderr says so, state first, with the flag that names the real folder, once however often the
    command reads the folder (`yerk explain retention` reads it twice), and the answer itself is unchanged."""
    from yerk_ai import words
    r = yerk("explain", "retention", "--state", str(tmp_path))
    said = r.stderr.splitlines()
    assert r.returncode == 0 and len(said) == 1 and said[0].startswith(words.WORKING), r.stderr
    assert "example layer" in said[0] and "--private" in said[0] and words.width(said[0]) <= words.PHONE
    q = yerk("explain", "retention", "--private", "examples/private", "--state", str(tmp_path))
    assert q.returncode == 0 and q.stdout == r.stdout and q.stderr == ""


def test_add_and_deploy_never_say_the_example_layer(tmp_path, monkeypatch, capsys):
    """F9-12: the two commands that never answer on the example layer say nothing of it: `yerk add` refuses there with its
    own line, and `yerk deploy` takes the running daemon's own layer, else refuses."""
    from yerk_ai import cli, config, deploy
    r = yerk("add", "anything", "--state", str(tmp_path))
    assert r.returncode == 2 and "example layer" not in r.stderr and "--private" in r.stderr, r.stderr
    monkeypatch.delenv("YERK_PRIVATE", raising=False)
    monkeypatch.setattr(config, "_EXAMPLE_SAID", False, raising=False)
    monkeypatch.setattr(deploy, "run", lambda private, *a, **k: 0 if private == config.EXAMPLE_PRIVATE else 1)
    assert cli.main(["deploy", "--state", str(tmp_path)]) == 0
    assert "example layer" not in capsys.readouterr().err


def due_layer(tmp_path):
    """The room scope layer (an owner room, a sealed one and a person's), a line dated in each, and the list yerk-index writes."""
    from fake_channel import ConsoleChannel
    from test_room_scope import install
    from yerk_ai import due
    from yerk_ai.room import Inbound
    y = install(tmp_path, sealed=True, family=True)
    today = datetime.now(y.memory.tz).date()
    for room, text, days, owes in (("example-room", "Owner item ten days out.", 10, ""), ("example-room", "Owner item overdue.", -3, ""),
                                   ("example-room", "Owner item soon.", 2, "Dana"), ("frozen-room", "Sealed item soon.", 3, ""),
                                   ("family-room", "Her item soon.", 4, "Riley"), ("family-room", "Her item fifteen days late.", -15, "")):
        if room not in y.rooms:
            y.add_room(room, ConsoleChannel()).inflight = [Inbound(f"m-{room}", "owner", "a placeholder message")]
        assert asyncio.run(y.tool_remember(room, text, "", [], due=(today + timedelta(days=days)).isoformat(), owes=owes))["ok"]
    due.Builder(y.memory).refresh(y.memory.scope.files())
    return y, today, ["--private", str(y.private), "--state", str(y.state)]


def due_said(capsys, *args):
    from yerk_ai import cli
    code = cli.main(["due", *args])
    got = capsys.readouterr()
    return code, got.out, got.err


def test_due_lists_an_owner_rooms_view_overdue_first_then_within_the_days_and_every_open_one_with_all(tmp_path, capsys):
    y, today, where = due_layer(tmp_path)
    day = {n: (today + timedelta(days=n)).isoformat() for n in (-3, 2, 3, 4, 10)}
    code, out, err = due_said(capsys, *where)
    rows = [re.sub(r"\(memory/daily/\d{4}-\d\d-\d\d\.md:\d+\)$", "(cite)", x) for x in out.splitlines()]
    assert code == 0 and rows == ["overdue:", f"- {day[-3]}: Owner item overdue. (cite)", "due in the next 7 days:",
                                  f"- {day[2]}, waiting on Dana: Owner item soon. (cite)", f"- {day[3]}: Sealed item soon. (cite)",
                                  f"- {day[4]}, waiting on Riley: Her item soon. (cite)", "(1 more open: yerk due --all)"]
    assert due_said(capsys, "--room", "example-room", *where)[1] == out                      # an owner room's view
    whole = due_said(capsys, "--all", *where)[1].splitlines()
    assert whole[:6] == out.splitlines()[:6] and whole[6] == "later:" and "Owner item ten days out." in whole[7] and len(whole) == 8
    assert "fifteen" not in "\n".join(whole) and "due in the next 10 days:" in due_said(capsys, "--days", "10", *where)[1]
    few = due_said(capsys, "--days", "2", *where)[1]
    assert "Owner item soon." in few and "Sealed item soon." not in few and "(3 more open: yerk due --all)" in few
    got = json.loads(due_said(capsys, "--json", *where)[1])
    assert got["open"] == 5 and [x["text"] for x in got["items"]] == ["Owner item overdue.", "Owner item soon.", "Sealed item soon.", "Her item soon."]
    assert set(got["items"][0]) == {"date", "time", "owes", "text", "role", "filed", "cite", "class", "room", "overdue"}
    assert [x["overdue"] for x in got["items"]] == [True, False, False, False] and got["items"][1]["owes"] == "Dana"
    assert memory.read(y.memory, got["items"][0]["cite"], lines=1)["text"].endswith(f"Owner item overdue. [due {day[-3]}] [c:private] [m:m-example-room.d"
                                                                                + memory.read(y.memory, got["items"][0]["cite"], lines=1)["text"][-9:])


def test_due_in_a_persons_room_shows_its_own_items_never_the_owners_and_a_sealed_room_its_own(tmp_path, capsys):
    y, today, where = due_layer(tmp_path)
    for flags in ((), ("--all",), ("--json", "--all")):
        hers, sealed = (due_said(capsys, "--room", r, *flags, *where)[1] for r in ("family-room", "frozen-room"))
        assert "Her item soon." in hers and not any(w in hers for w in ("Owner", "Sealed", "fifteen")), flags
        assert "Sealed item soon." in sealed and not any(w in sealed for w in ("Owner", "Her item")), flags
    assert "more open" not in due_said(capsys, "--room", "family-room", *where)[1]            # nor does the count say how many are not hers


def test_due_says_an_unknown_room_a_list_that_is_not_there_and_nothing_open_and_writes_nothing(tmp_path, capsys):
    y, today, where = due_layer(tmp_path)

    def files() -> dict[Path, int]:              # all but the search index's own, which the layer's writer closes when it is collected
        return {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file() and not p.name.startswith("index.sqlite")}
    before = files()
    code, out, err = due_said(capsys, "--room", "no-such-room", *where)
    assert (code, out) == (2, "") and "no room 'no-such-room'" in err
    due_said(capsys, "--all", *where)
    assert files() == before                                                                   # read-only
    y.memory.due.write_text('{"items": []}')
    assert due_said(capsys, *where)[:2] == (0, "nothing is open\n") == due_said(capsys, "--all", *where)[:2]
    far = {"date": (today + timedelta(days=30)).isoformat(), "time": "", "owes": "", "text": "x", "role": "note", "filed": "", "cite": "a.md:1",
           "class": "private", "room": "", "path": str(y.memory.root / "facts" / "a.md"), "raw": "- x"}
    y.memory.due.write_text(json.dumps({"items": [far]}))
    assert due_said(capsys, *where)[:2] == (0, "nothing is overdue or due in the next 7 days\n(1 more open: yerk due --all)\n")
    for broken in ("{", None):
        y.memory.due.unlink() if broken is None else y.memory.due.write_text(broken)
        code, out, err = due_said(capsys, *where)
        assert (code, out) == (2, "") and "no list of what is due reads at" in err
