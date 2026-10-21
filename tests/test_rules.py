"""Checks that a rule told once is known in every room. `remember` with a `job` files how a job is done from now on
into the install's one rules file, from an owner's room only; every running room hears what changed on its next
message, once per version, and a session that starts after the change has it in its start file and hears nothing
is a placeholder."""
import asyncio
import json
import re
import shutil

from yerk_ai import context, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import ConsoleChannel, Yerkd
from yerk_ai.lifted.trim import truncate
from yerk_ai.room import Inbound

A, B, FAMILY = "example-room", "example-quiet", "family-room"


def daemon(tmp_path, private=EXAMPLE_PRIVATE) -> Yerkd:
    (tmp_path / "state").mkdir()
    y = Yerkd(private, tmp_path / "state")
    y.room_confs[FAMILY] = {**y.room_confs[A], "privacy": "family", "id": f"{FAMILY}-id"}     # not an owner room
    for name in (A, B, FAMILY):
        y.add_room(name, ConsoleChannel()).inflight = [Inbound("m-1", "owner", "a placeholder message")]
    return y


def start(y: Yerkd, room: str, sid: str) -> None:
    """A process start for `room` (its start file and first look), its session bound as RoomProcess.start binds it."""
    y.spec_for(room)
    y.sessions.bind(room, sid, "one")


def told(y: Yerkd, sid: str) -> str:
    """What the next message of session `sid` is handed, through the daemon's own UserPromptSubmit hook."""
    out = asyncio.run(y.hook({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "open the example page"}))
    return out["hookSpecificOutput"]["additionalContext"]


def rule(y: Yerkd, room: str, text: str, job: str = "open web pages") -> dict:
    return asyncio.run(y.tool_rule(room, text, job))


def test_a_rule_filed_in_one_room_reaches_a_running_room_on_its_next_message_once_per_version(tmp_path):
    y = daemon(tmp_path)
    start(y, B, "sid-b")
    assert context.NEWS not in told(y, "sid-b")                         # nothing changed since its start
    r = rule(y, A, "Open web pages with the example browser, never the example fetcher.", "Open web pages:")
    assert r == {"ok": True, "job": "open web pages", "cite": "tree/rules.md:6", "replaced": [],
                 "next": words.FILED_NEXT.format(clause=words.RULE_CLAUSE.format(job="open web pages", over=""))}
    assert r["next"].endswith("the filing is one clause at its end, this one: every room now does open web pages this way.")
    block = told(y, "sid-b")
    assert block.count(f"{context.NEWS}\nnew in rules.md: open web pages: Open web pages with the example browser, never "
                       "the example fetcher. (example-room, ") == 1 and "[m:m-1]" in block
    assert context.NEWS not in told(y, "sid-b")                         # once per version, then never again for it
    assert rule(y, A, "Open web pages with the example browser, never the example fetcher.")["cite"] == r["cite"]
    assert context.NEWS not in told(y, "sid-b")                         # a retried call files nothing new
    assert memory.file_class(y.memory.root / memory.RULES) == ("shared", "")      # every room may read it


def test_a_replaced_rule_shows_as_replaced_and_stays_in_the_file_marked(tmp_path):
    y = daemon(tmp_path)
    old = rule(y, A, "Open web pages with the old fetcher.")
    start(y, B, "sid-b")
    new = rule(y, A, "Open web pages with the example browser.")
    assert new["cite"] == "tree/rules.md:7" and new["replaced"] == [old["cite"]]
    block = told(y, "sid-b")
    assert "new in rules.md: open web pages: Open web pages with the example browser. (" in block
    assert "replaced in rules.md: open web pages: Open web pages with the old fetcher. (" in block
    line = (y.memory.root / memory.RULES).read_text().splitlines()[5]
    assert line.startswith("- open web pages: Open web pages with the old fetcher. (example-room, ")
    assert line.endswith(" [superseded by tree/rules.md:7] [m:m-1]")          # marked, never deleted
    start(y, A, "sid-a")                                                # a new session starts with the live line only
    ctx = (tmp_path / "state" / "rooms" / A / "context.md").read_text()
    assert "Open web pages with the example browser." in ctx and "old fetcher" not in ctx


def test_a_room_that_is_not_an_owner_room_cannot_file_a_rule(tmp_path):
    y = daemon(tmp_path)
    assert rule(y, FAMILY, "Open web pages with the example browser.") == {"ok": False, "line": words.RULES_OWNER_ONLY}
    assert rule(y, "", "Open web pages with the example browser.") == {"ok": False, "line": words.RULES_OWNER_ONLY}
    assert not (y.memory.root / memory.RULES).exists()
    assert not rule(y, A, "  ")["ok"] and not rule(y, A, "Open web pages with it.", " : ")["ok"]


def test_the_block_keeps_to_its_budget_and_past_it_gives_the_count_and_where_to_read(tmp_path):
    y = daemon(tmp_path)
    start(y, FAMILY, "sid-f")
    budget = y.budgets["per_message"]["on_hit"]["rules"]
    rule(y, A, "Example job one is done the example way.", "example job one")
    assert context.chars(y.rules_news(FAMILY, "sid-f")) <= budget
    for i in range(12):
        rule(y, A, f"Example job {i} is done the example way, " + "with care " * 10, f"example job {i}")
    note = y.rules_news(FAMILY, "sid-f")
    assert note == (f"{context.NEWS} 12 lines new or replaced, past the {budget:,} characters this note holds; "
                    'read them: read_memory("rules.md").') and context.chars(note) <= budget
    assert memory.read(y.memory, "rules.md", 6, 1, y.reader(FAMILY))["ok"]      # the pointer reads from any room


def test_a_rule_past_the_rules_budget_is_filed_and_said_and_the_machine_room_hears_which_lines_every_start_cuts(tmp_path, capsys):
    """2026-10-21: a room's rule took the rules part to 12,398 against its 11,886 and nothing said so, so every session
    start sent it trimmed. Filed all the same (a correction is never lost); the result carries `over_budget` and the
    reply's closing clause; the machine room gets one line naming the file, its size, the cap and the lines the trim drops."""
    y = daemon(tmp_path)
    y.room_confs["example-machine"] = {**y.room_confs[A], "role": "machine", "id": "example-machine-id"}
    y.add_room("example-machine", ConsoleChannel())
    cap, path = y.budgets["session_start"]["appended"]["rules"], y.memory.root / memory.RULES
    assert "over_budget" not in rule(y, A, "Example job zero is done the example way.", "example job zero")
    path.write_text(path.read_text() + "".join(f"- example job {i}: done the example way, {'with care ' * 9}[m:m-{i}]\n"
                                               for i in range(110)))
    r = rule(y, A, "Example job last is done the example way.", "example job last")
    text = context.rules_text(y.room_confs[A], path)[0]
    assert path.read_text().splitlines()[-1].startswith("- example job last: Example job last is done the example way. (")
    assert r["over_budget"] == {"size": context.chars(text), "cap": cap} and context.chars(text) > cap
    assert list(r)[-1] == "next" and r["next"].endswith(
        "every room now does example job last this way, and the shared rules file is over its cap.")
    posts = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    said = [p["notice"] for p in posts if p["room"] == "example-machine"]
    assert len(said) == 1 and [p for p in posts if p["room"] == A] == []          # the room says it in its reply
    head, cut = said[0].split("\n")
    assert head == f"{words.WORKING} rules.md over cap: {context.chars(text):,} of {cap:,}"
    first, last = map(int, re.fullmatch(r"session starts cut lines (\d+) to (\d+)", cut).groups())
    lines, sent = path.read_text().splitlines(), truncate(text, "rules", cap)[0]
    assert first < last and all(lines[n - 1] not in sent for n in range(first, last + 1))   # each one gone or broken
    assert lines[first - 2] in sent and lines[last] in sent                                 # either side goes out whole


def test_a_session_started_after_the_change_has_the_rules_at_start_and_no_block(tmp_path):
    y = daemon(tmp_path)
    rule(y, A, "Open web pages with the example browser.")
    start(y, B, "sid-new")
    ctx = (tmp_path / "state" / "rooms" / B / "context.md").read_text()
    assert (f"[from {(y.memory.root / memory.RULES).resolve()}]\n# Rules: one live line per job, in every room\n\n"
            "- open web pages: Open web pages with the example browser. (example-room, ") in ctx
    assert "class: shared" not in ctx
    assert context.NEWS not in told(y, "sid-new")


def test_a_rulebook_change_reaches_a_running_room_the_same_way(tmp_path):
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    y = daemon(tmp_path, priv)
    start(y, B, "sid-b")
    (book := priv / "rulebook.md").write_text(book.read_text() + "\n- Example: answer in one line.\n")
    assert told(y, "sid-b").count(f"{context.NEWS}\nnew in rulebook.md: Example: answer in one line.") == 1
    assert context.NEWS not in told(y, "sid-b")


SEALED = "sealed-room"


def sealed(tmp_path) -> Yerkd:
    """The install with SEALED added: example-room's table again under its own channel id, with `sealed = true`, the way
    the room kept blind for a side-by-side test is set."""
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    block = (priv / "rooms.toml").read_text().split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    with (priv / "rooms.toml").open("a") as f:
        f.write(f"\n[rooms.{SEALED}]\n{block.replace('CHANNEL_ID', 'SEALED_ID')}sealed = true\n")
    y = daemon(tmp_path, priv)
    y.add_room(SEALED, ConsoleChannel()).inflight = [Inbound("m-2", "owner", "a placeholder message")]
    return y


def test_a_sealed_room_never_gets_another_rooms_rule_at_start_on_a_message_or_on_file(tmp_path):
    """A rule filed in an owner's room reaches an unsealed room's next message, and never a sealed one: no block on its
    next message, no rules text in a start file written after the rule, no look on file holding it in the room's folder,
    and read_memory refuses the rules file there."""
    y = sealed(tmp_path)
    start(y, SEALED, "sid-s")
    start(y, B, "sid-b")
    rule(y, A, "Open web pages with the example browser.")
    assert "example browser" in told(y, "sid-b")                        # the control: an unsealed room hears it
    got = told(y, "sid-s")
    assert context.NEWS not in got and "example browser" not in got
    start(y, SEALED, "sid-s2")                                        # a session started after the rule
    room = tmp_path / "state" / "rooms" / SEALED
    assert "example browser" not in (room / "context.md").read_text() and "example browser" not in told(y, "sid-s2")
    assert "Open web pages with the example browser." in (tmp_path / "state" / "rooms" / B / "rules-seen.json").read_text()
    assert not (room / "rules-seen.json").exists()
    assert not asyncio.run(y.tool_read_memory(SEALED, memory.RULES, 1, 50))["ok"]


def test_a_sealed_rooms_own_rule_stays_its_own_note_and_never_reaches_the_rules_file(tmp_path):
    """A sealed room's `remember` with a `job` files the line in its own day log, the job as its label, where the room
    reads it back; the shared rules file never gets it, so no other room hears it."""
    y = sealed(tmp_path)
    start(y, B, "sid-b")
    r = rule(y, SEALED, "Use the example browser, never the example fetcher.", "Open web pages:")
    assert r["ok"] and "/daily/" in r["cite"] and not (y.memory.root / memory.RULES).exists()
    assert context.NEWS not in told(y, "sid-b")
    day = asyncio.run(y.tool_read_memory(SEALED, r["cite"].rpartition(":")[0], 1, 50))
    assert day["ok"] and "Open web pages: Use the example browser, never the example fetcher." in day["text"]
