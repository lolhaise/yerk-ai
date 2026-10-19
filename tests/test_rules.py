"""Checks that a rule told once is known in every room. `remember` with a `job` files how a job is done from now on
into the install's one rules file, from an owner's room only; every running room hears what changed on its next
message, once per version, and a session that starts after the change has it in its start file and hears nothing
is a placeholder."""
import asyncio
import shutil

from yerk_ai import context, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import ConsoleChannel, Yerkd
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
                 "line": "from now on every room does open web pages this way"}
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
