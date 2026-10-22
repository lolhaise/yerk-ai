"""Checks a trusted room, one that answers other people and has shell = "trusted": it runs the tools your own rooms get, its shell inside
a wall the daemon builds itself from what it knows (Yerkd.walled, Yerkd.wall), and a rooms.toml that would undo the wall stops the start
with one line. A people room without the key is as it was, and the refusal lines a people room can hit name no person (2026-10-21)."""
import json
import re
import shutil
from pathlib import Path

import pytest

from yerk_ai import words
from yerk_ai.config import EXAMPLE_PRIVATE
from yerk_ai.daemon import Yerkd

MOM = 31        # the one person of the family room; a stand-in, since a real id (17 to 20 digits) never goes in this repository
TOOLS = '["Read", "Grep", "Glob", "Bash", "Write", "Edit"]'


def layout(tmp_path, monkeypatch, mom: str = f'shell = "trusted"\ntools = {TOOLS}\n', accounts: tuple[str, ...] = ("~/.claude-acct2", "~/.claude-acct4")):
    """A scratch home shaped like the box's: the private folder with an owner layer (`main`) and hers (`mom`), the state folder
    under ~/.local/state, the box's workspace (a memory root) and its staging (an owner room's `read` folder), two account
    folders with transcripts, and the owner's Python user site-packages. `mom` is the end of her room's table."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    private, state = home / "yerk-private", home / ".local" / "state" / "yerk"
    shutil.copytree(EXAMPLE_PRIVATE, private)
    state.mkdir(parents=True)
    box, staging = home / ".openclaw" / "workspaces" / "helper" / "memory", home / "staging" / "work"
    for d in (box, staging, private / "main", private / "mom", private / "imports" / "texts", home / ".local" / "lib" / "python3.14" / "site-packages"):
        d.mkdir(parents=True)
    (box / "2026-10-21.md").write_text("# 2026-10-21\n\n- an owner line\n")
    (private / "accounts.toml").write_text("[accounts]\n" + "".join(f'a{i} = {{ config_dir = "{c}", order = {i} }}\n' for i, c in enumerate(accounts)))
    for c in accounts:
        (Path(c).expanduser() / "projects" / "-some-other-room").mkdir(parents=True, exist_ok=True)
    (private / "yerkd.toml").write_text(f'[memory]\nroot = "memory"\nalso = {json.dumps([str(box)])}\n')
    base = (private / "rooms.toml").read_text()
    block = base.split("[rooms.example-room]\n", 1)[1].split("\n[rooms.", 1)[0]
    owner = block.replace("CHANNEL_ID", "WORK_ID").replace('tools = ["Read", "Grep", "Glob"]', 'tools = ["Read", "Grep", "Glob", "Bash"]')
    hers = "".join(x + "\n" for x in block.replace("CHANNEL_ID", "MOM_ID").replace('privacy = "owner"', 'privacy = "family"').splitlines()
                   if not x.startswith(("tools", "thinking_panel")))
    (private / "rooms.toml").write_text(base + f'\n[rooms.work-room]\n{owner}layer = "main"\nread = {json.dumps([str(staging)])}\n'
                                        f'\n[rooms.family-room]\n{hers}people = [{MOM}]\nlayer = "mom"\n{mom}')
    return home, private, state


def covered(path: Path, entries: list[str]) -> bool:
    return any(path == Path(e) or path.is_relative_to(Path(e)) for e in entries)


def test_a_trusted_people_room_starts_with_the_owner_tools_and_its_shell_inside_the_daemons_wall(tmp_path, monkeypatch):
    home, private, state = layout(tmp_path, monkeypatch, mom=f'shell = "trusted"\ntools = {TOOLS}\n'
                                  '[rooms.family-room.sandbox.filesystem]\ndenyRead = ["/srv/elsewhere"]\n'
                                  '[rooms.family-room.sandbox.network]\nstrictAllowlist = true\nallowedDomains = []\n')
    y = Yerkd(private, state)
    spec, mine = y.spec_for("family-room"), state / "rooms" / "family-room"
    box, fs = spec.settings["sandbox"], spec.settings["sandbox"]["filesystem"]
    assert spec.tools == json.loads(TOOLS)
    assert box["enabled"] is True and box["autoAllowBashIfSandboxed"] is True
    assert box["allowUnsandboxedCommands"] is False and box["failIfUnavailable"] is True          # no retry outside it, no start without it
    assert box["network"] == {"strictAllowlist": True, "allowedDomains": []}                       # its own web allowlist
    walled = [private / "main", private / "memory", private / "imports", private / "rooms.toml", private / "accounts.toml",
              private / "yerkd.toml", state / "ledger.sqlite", state / "secrets", state / "rooms" / "work-room",
              state / "rooms" / "example-room", home / ".openclaw", home / "staging", home / ".claude-acct2" / "projects",
              home / ".claude-acct4" / "projects"]
    for p in walled:
        assert covered(p.resolve(), fs["denyRead"]) and covered(p.resolve(), fs["denyWrite"]), p
    assert "/srv/elsewhere" in fs["denyRead"]                                                      # its own deny, kept
    for p in (private / "mom", mine / "work", mine / "work" / "inbox", mine / "outbox"):           # its layer, work and attachments
        assert not covered(p.resolve(), fs["denyRead"]), p
    for p in (private / "mom", mine / "work"):
        assert not covered(p.resolve(), fs["denyWrite"]), p
    assert covered((mine / "outbox").resolve(), fs["denyWrite"])                                   # what went out stays as it went
    assert fs["allowRead"] == [str((home / ".local" / "lib" / "python3.14" / "site-packages").resolve())]
    allow = spec.settings["permissions"]["allow"]
    assert [r for r in allow if r.startswith(("Edit", "Write"))] == [f"Edit(/{(private / 'mom').resolve()}/**)", f"Edit(/{(mine / 'work').resolve()}/**)"]
    assert spec.add_dirs == [(private / "mom").resolve(), (mine / "outbox").resolve()]
    assert spec.settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert "sandbox" in y.spec_for("work-room").settings and "allowRead" not in y.spec_for("work-room").settings["sandbox"].get("filesystem", {})


@pytest.mark.parametrize("mom,line", [
    (f'shell = "trusted"\ntools = {TOOLS}\n[rooms.family-room.sandbox]\nenabled = false\n', "its sandbox sets enabled"),
    (f'shell = "trusted"\ntools = {TOOLS}\n[rooms.family-room.sandbox]\nallowUnsandboxedCommands = true\n', "its sandbox sets allowUnsandboxedCommands"),
    (f'shell = "trusted"\ntools = {TOOLS}\n[rooms.family-room.sandbox]\nexcludedCommands = ["python3 *"]\n', "its sandbox sets excludedCommands"),
    (f'shell = "trusted"\ntools = {TOOLS}\n[rooms.family-room.sandbox.filesystem]\nallowRead = ["~/.openclaw"]\n', "its sandbox sets filesystem.allowRead"),
    (f'shell = "trusted"\ntools = {TOOLS}\n[rooms.family-room.sandbox.filesystem]\nallowWrite = ["~/.config"]\n', "its sandbox sets filesystem.allowWrite"),
    (f'shell = "trusted"\ntools = {TOOLS}\nallow = ["Edit"]\n', "allow names Edit"),
    (f'shell = "trusted"\ntools = {TOOLS}\nallow = ["Write(//home/**)"]\n', "allow names Write"),
])
def test_a_trusted_room_whose_rooms_toml_would_undo_the_wall_is_refused_with_one_line(tmp_path, monkeypatch, mom, line):
    _, private, state = layout(tmp_path, monkeypatch, mom=mom)
    with pytest.raises(SystemExit, match=re.escape('family-room: shell = "trusted" needs the daemon\'s wall whole: ' + line)):
        Yerkd(private, state)


def test_a_wall_the_daemon_cannot_build_stops_the_start(tmp_path, monkeypatch):
    """An account folder inside her layer would put every room's transcripts where her shell reads and writes."""
    _, private, state = layout(tmp_path, monkeypatch, accounts=("~/.claude-acct2", "~/yerk-private/mom/.claude"))
    with pytest.raises(SystemExit, match=r"room family-room: shell = \"trusted\" cannot wall .*projects: it is inside the room's own folders"):
        Yerkd(private, state)


def test_a_people_room_without_the_key_is_as_it_was(tmp_path, monkeypatch):
    _, private, state = layout(tmp_path, monkeypatch, mom='tools = ["Read", "Grep", "Glob"]\n')
    y = Yerkd(private, state)
    spec = y.spec_for("family-room")
    assert "sandbox" not in spec.settings and spec.add_dirs == [(private / "mom").resolve()]
    assert not [r for r in spec.settings["permissions"]["allow"] if r.startswith(("Edit", "Write"))]
    for i, (mom, line) in enumerate(((f"tools = {TOOLS}\n", 'people needs tools without "Bash" and no `read`'),
                                     (f'shell = "yes"\ntools = {TOOLS}\n', 'shell must be "trusted" or unset'),
                                     (f'shell = "trusted"\ntools = {TOOLS}\nread = ["~/notes"]\n', 'people needs tools without "Bash" and no `read`'))):
        _, private, state = layout(tmp_path / f"case{i}", monkeypatch, mom=mom)
        with pytest.raises(SystemExit, match=re.escape(line)):
            Yerkd(private, state)


def test_the_lines_a_people_room_can_hit_say_what_it_can_do_and_name_no_person():
    for line in (words.WORKERS_OWNER_ONLY, words.JOBS_OWNER_ONLY, words.RULES_OWNER_ONLY, words.OWNER_ONLY):
        assert "this room" in line, line
        assert not re.search(r"\b(owner|owner's|admin|main|ask|turn (?:it|that) on|switch)\b", line, re.I), line
