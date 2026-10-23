"""Checks that a rule told once is known in every room. `remember` with a `job` files how a job is done from now on
into the install's one rules file, from an owner's room only; every running room hears what changed on its next
message, once per version, and a session that starts after the change has it in its start file and hears nothing
is a placeholder."""
import asyncio
import json
import shutil
from pathlib import Path

from yerk_ai import context, memory, words
from yerk_ai.config import EXAMPLE_PRIVATE, budgets
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


EX = " <example>" + "Asked the example question, it gave the example answer first and stopped there. " * 2 + "</example>"
SMALL = " <example>Asked the example question twice, it gave the one example answer first, then stopped.</example>"
PLAIN = "- example job {i}: done the example way, step by step. (example-room, 2026-10-21 09:00:00-04:00) [m:m-{i}]"


def fill(y: Yerkd, size: int, newest: tuple[str, ...] = ()) -> Path:
    """The rules file at exactly `size` characters as a session start counts them: plain example rules, one padded to land
    on it, then `newest`, the file's last lines."""
    path, head, plain = y.memory.root / memory.RULES, ["---", "class: shared", "---", "# Rules: one live line per job, in every room", ""], []
    path.parent.mkdir(parents=True, exist_ok=True)

    def measure(pad: str) -> int:
        path.write_text("\n".join([*head, *plain, pad, *newest]) + "\n")
        return context.chars(context.rules_text(y.room_confs[A], path)[0])
    while measure("- example pad x") < size - 300:
        plain.append(PLAIN.format(i=len(plain)))
    assert measure("- example pad x" + "x" * (size - measure("- example pad x"))) == size
    return path


def line_len(job: str, text: str) -> int:
    """What the new rule's line adds to the rules part: the line and its newline (the time's width never changes)."""
    return len(f"- {job}: {text} ({A}, 2026-01-01 00:00:00+00:00) [m:m-1]") + 1


def older(job: str, example: str = EX) -> str:
    return f"- {job}: done the {job} way.{example} ({A}, 2026-10-21 09:00:00-04:00) [m:m-{job.split()[-1]}]"


def fitted(y: Yerkd) -> list[str]:
    """The day-log lines Yerk AI wrote for a filing's fit, a strip's or a hold's, in order."""
    return [ln.body for f in sorted(y.daily.daily.glob("*.md")) for x in f.read_text().splitlines()
            if (ln := memory.parse_line(x)) and ln.role == "yerk"]


def size(y: Yerkd) -> int:
    return context.chars(context.rules_text(y.room_confs[A], y.memory.root / memory.RULES)[0])


def test_a_filing_that_fits_is_filed_whole_with_its_example(tmp_path):
    y = daemon(tmp_path)
    cap = y.budgets["session_start"]["appended"]["rules"]
    path = fill(y, cap - 1000, (older("example older a"),))
    r = rule(y, A, "Answer the example question first." + EX, "example new")
    assert "stripped" not in r and "held" not in r and size(y) <= cap
    assert path.read_text().splitlines()[-1].startswith(f"- example new: Answer the example question first.{EX} ({A}, ")
    assert path.read_text().count("<example>") == 2 and not (y.memory.root / memory.OVERFLOW).exists() and fitted(y) == []


def test_a_filing_past_the_cap_loses_its_own_example_first_and_older_rules_keep_theirs(tmp_path):
    """Seen live: one rule's 190-character example took the rules file past its cap and seven proven rules went out of
    every session start. The new rule's own example goes first; here that is enough, so the older rules keep theirs."""
    y = daemon(tmp_path)
    cap, text = y.budgets["session_start"]["appended"]["rules"], "Answer the example question first."
    path = fill(y, cap - memory.HEADROOM - line_len("example new", text), (older("example older a"), older("example older b")))
    r = rule(y, A, text + EX, "example new")
    assert r["stripped"] == ["example new"] and size(y) == cap - memory.HEADROOM
    lines = path.read_text().splitlines()
    assert lines[-1].startswith(f"- example new: {text} ({A}, ") and lines[-3:-1] == [older("example older a"), older("example older b")]
    assert fitted(y) == [words.RULE_STRIPPED.format(job="example new", cap=cap)]


def test_then_the_newest_older_rules_example_goes_and_never_an_older_ones_first(tmp_path):
    y = daemon(tmp_path)
    cap, text = y.budgets["session_start"]["appended"]["rules"], "Answer the example question first."
    path = fill(y, cap - memory.HEADROOM - line_len("example new", text) + 50, (older("example older a"), older("example older b")))
    r = rule(y, A, text + SMALL, "example new")                     # its own example is not enough: 50 still over
    assert r["stripped"] == ["example new", "example older b"] and size(y) <= cap - memory.HEADROOM
    lines = path.read_text().splitlines()
    assert lines[-3] == older("example older a") and lines[-2] == older("example older b", "") and "<example>" not in lines[-1]
    assert lines[-1].startswith(f"- example new: {text} ({A}, ")    # the rule's own words, whole
    assert fitted(y) == [words.RULE_STRIPPED.format(job=j, cap=cap) for j in ("example new", "example older b")]


def test_a_rule_with_no_room_even_with_no_examples_is_held_in_the_overflow_file_and_said_once(tmp_path, capsys):
    """A rule that would take the rules file past its cap with every example taken out is never filed there: the file stays
    as it was, the rule goes to rules-overflow.md (0600) whole, the machine room gets one notice and the room a clause."""
    y = daemon(tmp_path)
    y.room_confs["example-machine"] = {**y.room_confs[A], "role": "machine", "id": "example-machine-id"}
    y.add_room("example-machine", ConsoleChannel())
    cap = y.budgets["session_start"]["appended"]["rules"]
    before = (path := fill(y, cap - 50)).read_bytes()
    start(y, B, "sid-b")
    r = rule(y, A, "Example job last is done the example way." + EX, "example job last")
    assert r == {"ok": True, "job": "example job last", "cite": "tree/rules-overflow.md:3", "replaced": [], "held": True,
                 "next": words.FILED_NEXT.format(clause=words.HELD_CLAUSE)}
    held = y.memory.root / memory.OVERFLOW
    assert path.read_bytes() == before and held.stat().st_mode & 0o777 == 0o600
    assert held.read_text().splitlines()[-1].startswith(f"- example job last: Example job last is done the example way.{EX} ({A}, ")
    posts = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert [p["notice"] for p in posts if p["room"] == "example-machine"] == [
        f"{words.WORKING} " + words.RULE_HELD.format(file="rules-overflow.md", job="example job last", room=A, cap=cap)]
    assert [p for p in posts if p["room"] == A] == []                  # the room says it in its reply
    assert fitted(y) == [words.RULE_HELD_LOG.format(job="example job last", file="rules-overflow.md", cap=cap)]
    assert context.NEWS not in told(y, "sid-b")                         # no room has it
    assert rule(y, A, "Example job last is done the example way." + EX, "example job last") == r      # a retried call
    assert held.read_text().count("- example job last: ") == 1 and len(fitted(y)) == 1 and "notice" not in capsys.readouterr().out


def test_a_rule_file_past_its_size_at_session_start_loses_its_newest_lines_never_a_middle_one(tmp_path):
    """Hand edits can still take a rule file past its size: the rules part and the rulebook keep their first lines whole and
    drop the newest, the last ones; a persona keeps its head and tail as before."""
    shutil.copytree(EXAMPLE_PRIVATE, priv := tmp_path / "private")
    (rules := tmp_path / "rules.md").write_text("".join(f"- example job {i}: done the example way, step by step.\n" for i in range(300)))
    (priv / "example-rulebook.md").write_text("".join(f"- example habit {i}: kept the example way, step by step.\n" for i in range(400)))
    (priv / "example-persona.md").write_text("".join(f"The example voice, line {i}, said the example way.\n" for i in range(400)))
    parts = {p.name: p for p in context.session_start(priv, {"rulebook": "example-rulebook.md", "persona": "example-persona.md"},
                                                      budgets(), rules=rules)}
    for name, path in (("rules", rules), ("rulebook", priv / "example-rulebook.md")):
        p, lines = parts[name], path.read_text().splitlines()
        k = len(lines) - len(p.cut)
        assert p.over and p.size <= p.budget and 0 < k < len(lines) - 12, name
        assert list(p.cut) == lines[k:] and p.text.split("\n\n[...trimmed ")[0].split("\n")[1:] == lines[:k], name   # the tail only, at a line's end
        assert p.text.endswith(f"Its last lines, the newest, are omitted. The full file stays readable with the Read tool: {path.resolve()}]")
    assert parts["persona"].over and "The middle is omitted." in parts["persona"].text


def test_a_tail_cut_never_ends_inside_an_example_that_spans_lines():
    """A rulebook kept by hand can hold an <example> over several lines under its rule: the cut keeps the rule's line and
    drops the example whole, so no session start ends inside one."""
    block = "- example habit {i}: kept the example way, step by step.\n<example>\n" + "Asked the example question, it gave the example answer.\n" * 3 + "</example>\n"
    text = "".join(block.format(i=i) for i in range(40))
    plain = text.rfind("\n", 0, 900 + 1)                                  # a line cut at 90% of 1,000 lands inside an example
    assert text[:plain].count("<example>") == text[:plain].count("</example>") + 1
    sent, cut = truncate(text, "rulebook", 1000, mode="tail")
    body = sent.split("\n\n[...trimmed ")[0]
    assert text.startswith(body + "\n") and cut == len(text) - len(body) and body.endswith("- example habit 3: kept the example way, step by step.")
    assert body.count("<example>") == body.count("</example>") == 3


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
