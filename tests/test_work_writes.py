"""Checks that every room writes a text file in its own work folder without Claude Code asking for approval, and is still asked outside it. The case
behind it (need 38 of 2026-11-02): an owner room asked to write a sheet in its work folder before a hand-over ran `printf ... > <work folder>/sheet.md`
three times, and each came back "Output redirection to '...' needs approval" from Claude Code's own check, with nobody at the keyboard; its Write tool
asked the same way. Claude Code 2.1.316 auto-allows a sandboxed command, but a command its sandbox does not auto-allow, and every Write and
Edit, is asked in a room's default mode unless an Edit rule covers the path (a scratch probe on 2.1.316, 2026-11-02: the
redirect and the Write tool asked inside the working folder without the rule, landed with it, and asked outside it either
way). The daemon writes that one rule, on the room's own work folder, into every room's settings."""
import json
from pathlib import Path

from test_trusted_room import TOOLS, layout

from yerk_ai.daemon import Yerkd


def writes(path: Path, rules: list[str]) -> bool:
    """An Edit rule as Claude Code reads one for Write, Edit and a shell redirect: `Edit(//<folder>/**)` every file under it."""
    for r in (r for r in rules if r.startswith("Edit(/") and r.endswith("/**)")):
        if path.is_relative_to(Path(r[len("Edit(/"):-len("/**)")])):
            return True
    return False


def test_every_room_writes_in_its_own_work_folder_unasked_and_nowhere_else(tmp_path, monkeypatch):
    """An owner room with a shell, one without, and a room with people that has a Write tool and no shell: one Edit rule each,
    on its own work folder, so a Write or a redirect there runs; never another room's folder, its layer, its own settings
    beside the work folder, the private folder or the state folder."""
    home, private, state = layout(tmp_path, monkeypatch, mom='tools = ["Read", "Grep", "Glob", "Write"]\n')
    y = Yerkd(private, state)
    rooms = ("example-room", "example-quiet", "work-room", "family-room")
    for name in rooms:
        spec, mine = y.spec_for(name), (state / "rooms" / name).resolve()
        allow = spec.settings["permissions"]["allow"]
        assert [r for r in allow if r.startswith(("Edit", "Write"))] == [f"Edit(/{mine / 'work'}/**)"], name
        assert spec.cwd.resolve() == mine / "work" and writes(mine / "work" / "sheet.md", allow), name
        assert writes(mine / "work" / "drafts" / "sheet.md", allow), name                       # a folder it makes there too
        for other in (m for m in rooms if m != name):
            assert not writes((state / "rooms" / other / "work" / "sheet.md").resolve(), allow), (name, other)
        for p in (mine / "settings.json", mine / "context.md", private.resolve() / "rooms.toml", private.resolve() / "desk" / "x.md",
                  private.resolve() / "mom" / "x.md", state.resolve() / "ledger.sqlite", home.resolve() / "notes.md"):
            assert not writes(p, allow), (name, p)


def test_the_redirect_the_room_was_refused_now_lands_and_one_outside_its_work_folder_is_still_asked(tmp_path, monkeypatch):
    """The wall as the room hit it: the settings its process starts with (written whole as settings.json) gave no rule on its
    work folder, so Claude Code asked for `printf ... > <work folder>/sheet.md`. The same settings now cover that target and
    the Write tool's, and leave the same line aimed past the work folder, at the room's own folder or its layer, to be asked."""
    _, private, state = layout(tmp_path, monkeypatch)
    y = Yerkd(private, state)
    spec = y.spec_for("work-room")
    sent = json.loads(json.dumps(spec.settings))                       # what start() writes for --settings, as JSON
    work = spec.cwd.resolve()
    assert "Bash" in spec.tools and sent["sandbox"]["autoAllowBashIfSandboxed"] is True    # the room as it was: sandbox on
    assert writes(work / "sheet.md", sent["permissions"]["allow"])
    for outside in (work.parent / "sheet.md", (private / "desk" / "sheet.md").resolve(), work.parent.parent / "example-room" / "work" / "sheet.md"):
        assert not writes(outside, sent["permissions"]["allow"]), outside


def test_a_trusted_room_keeps_its_layer_and_gains_nothing_else(tmp_path, monkeypatch):
    """A trusted room had Write and Edit on its layer and work folder before; it has exactly those two still, its outbox read
    only as before (the wall's denyWrite)."""
    _, private, state = layout(tmp_path, monkeypatch, mom=f'shell = "trusted"\ntools = {TOOLS}\n')
    y = Yerkd(private, state)
    spec, mine = y.spec_for("family-room"), (state / "rooms" / "family-room").resolve()
    allow = spec.settings["permissions"]["allow"]
    assert [r for r in allow if r.startswith(("Edit", "Write"))] == [f"Edit(/{(private / 'mom').resolve()}/**)", f"Edit(/{mine / 'work'}/**)"]
    assert not writes(mine / "outbox" / "sent.md", allow)
    assert str(mine / "outbox") in spec.settings["sandbox"]["filesystem"]["denyWrite"]
