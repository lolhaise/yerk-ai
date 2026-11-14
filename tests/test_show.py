"""Checks the `show` tool, which opens a file from a room's own folders on a screen of your Windows PC. Filed as need-2026-11-13-91."""
from __future__ import annotations

import asyncio
import base64

from yerk_ai import card, mcp_server, words
from yerk_ai.channels import page as pagemod
from test_page import opener_script, plain_daemon

UNC = r"\\wsl.localhost\Distro\rooms\example-room\work\deck\deck.html"
PLACED = '{"ok": true, "n": 3, "of": 3, "x": 4480, "y": 0, "w": 1920, "h": 1080, "locked": false, "opened": true}'


def pc(tmp_path, monkeypatch, said=PLACED, unc=UNC):
    """A stand-in for the owner's Windows PC under WSL: its PowerShell writes its command line to `ps-args` and the address,
    screen, room, file flag and WSLENV it was handed in its environment to `ps-env`, then says `said`; wslpath names a
    file as `unc`."""
    (ps := tmp_path / "powershell.exe").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{tmp_path}/ps-args"\n'
        f'printf \'%s\\n\' "$YERK_PAGE_URL" "$YERK_PAGE_SCREEN" "$YERK_PAGE_ROOM" "$YERK_PAGE_FILE" "$WSLENV" > "{tmp_path}/ps-env"\n'
        f"cat <<'SAID'\n{said}\nSAID\n")
    ps.chmod(0o700)
    monkeypatch.setattr(pagemod, "POWERSHELL", str(ps))
    monkeypatch.setattr(pagemod, "wsl", lambda: True)
    monkeypatch.setattr(pagemod, "windows_path", lambda p, seconds=0: unc)
    return tmp_path / "ps-args", tmp_path / "ps-env"


def deck(state, name="deck.html", body="<title>Deck</title><h1>slide one</h1>"):
    """A built slide deck in the room's own work folder."""
    (d := state / "rooms" / "example-room" / "work" / "deck").mkdir(parents=True, exist_ok=True)
    (f := d / name).write_text(body)
    return f


def test_the_room_puts_a_deck_from_its_work_folder_on_the_owner_s_named_screen_through_windows_not_its_shell(tmp_path, monkeypatch):
    """need-2026-11-13-91, the wall a room hit: asked during a timed exam to bring up the slide deck it built (an HTML file
    in its work folder) on the PC, the browser tool opened only http(s) addresses, a server in its shell was not
    reachable from the PC, and a command to open it through Windows was refused outside the sandbox. yerkd opens it: the
    file's Windows path in a window of its own of the owner's browser on the monitor named, maximized, the screen woken,
    the path in the script's environment and never on a command line, and the browser's new window found by its process."""
    y, _, state = plain_daemon(tmp_path)
    args, handed = pc(tmp_path, monkeypatch)
    f = deck(state)
    got = asyncio.run(y.tool_show("example-room", str(f), "Right"))
    assert got == {"ok": True, "line": f"{words.SHOW_OPENED_SCREEN.format(name='deck.html', screen='right', n=3, of=3)}; "
                                       f"{words.SHOW_AT.format(x=4480, y=0, w=1920, h=1080)}"}
    url, screen, room, file, wslenv = handed.read_text().splitlines()
    assert (url, screen, room, file) == (UNC, "right", "example-room", "1")
    assert {"YERK_PAGE_URL/w", "YERK_PAGE_SCREEN/w", "YERK_PAGE_ROOM/w", "YERK_PAGE_FILE/w"} <= set(wslenv.split(":"))
    argv = args.read_text().splitlines()
    assert "deck" not in args.read_text() and argv[:-1] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand"]
    assert base64.b64decode(argv[-1]).decode("utf-16-le") == pagemod.PLACE_PS1
    ps = pagemod.PLACE_PS1                                                     # a file's window: the browser's new one, by process
    assert "if ($env:YERK_PAGE_FILE) { $now[$k] -and (Get-Process -Id ([YerkScreen]::Pid(" in ps and ".ProcessName -eq $name }" in ps
    assert "GetWindowThreadProcessId(IntPtr h, out uint pid)" in ps and "StartsWith('Yerk page')" in ps    # the page's own match kept
    y.conf["page"] = {"screen": "middle"}                                      # the owner's standing monitor, set once for the page
    handed.unlink()
    got = asyncio.run(y.tool_show("example-room", str(f)))
    assert got["ok"] and handed.read_text().splitlines()[1] == "middle"
    assert card.step("mcp__yerk__show", {"path": str(f), "screen": "right"}) == "putting deck on your screen"


def test_the_page_s_own_open_hands_no_file_flag_so_its_window_is_still_found_by_its_title(tmp_path, monkeypatch):
    _, handed = pc(tmp_path, monkeypatch)
    assert pagemod.place("http://127.0.0.1:1/page?room=r&open=t", "left", "r")["ok"]
    assert handed.read_text().splitlines()[3] == ""
    pc(tmp_path, monkeypatch, said='{"ok": false, "error": "window", "opened": true}')
    assert pagemod.place(UNC, "left", "r", file=True)["why"] == words.SHOW_NO_WINDOW
    assert pagemod.place("http://127.0.0.1:1/page?room=r&open=t", "left", "r")["why"] == words.PAGE_NO_WINDOW


def test_nothing_the_pc_would_run_and_nothing_outside_the_room_s_own_folders_is_ever_opened(tmp_path, monkeypatch):
    """A file the PC would run (a script, a program, a shortcut, an Office file with its macros) is never handed to it, nor
    a file of another room, a secret or a path that is no file; a person's room puts nothing on the owner's screen."""
    y, private, state = plain_daemon(tmp_path, family=True)
    args, handed = pc(tmp_path, monkeypatch)
    for name in ("run.bat", "setup.exe", "go.ps1", "link.lnk", "page.hta", "notes.js", "deck.pptm", "deck.pptx", "noext"):
        f = deck(state, name)
        assert asyncio.run(y.tool_show("example-room", str(f))) == {"ok": False, "error": words.SHOW_TYPE.format(name=name)}
    (other := state / "rooms" / "example-quiet" / "work").mkdir(parents=True)
    (theirs := other / "deck.html").write_text("<h1>theirs</h1>")
    (secret := private / "secrets").mkdir(exist_ok=True)
    (key := secret / "discord.token").write_text("not-a-real-token\n")
    for p in (theirs, key, tmp_path / "elsewhere.html"):
        assert asyncio.run(y.tool_show("example-room", str(p))) == {"ok": False, "error": words.SHOW_NOT_HERE.format(name=p.name)}
    gone = state / "rooms" / "example-room" / "work" / "gone.html"
    assert asyncio.run(y.tool_show("example-room", str(gone))) == {"ok": False, "error": words.SHOW_NO_FILE.format(name="gone.html")}
    assert asyncio.run(y.tool_show("example-quiet", str(theirs))) == {"ok": False, "error": words.SHOW_OWNER_ONLY}
    assert asyncio.run(y.tool_show("example-room", str(deck(state)), "top")) == {"ok": False, "error": words.PAGE_SCREEN.format(screen="top")}
    assert not args.exists() and not handed.exists()                           # the PC was never asked


def test_a_screen_that_is_not_there_or_a_box_with_no_windows_is_said_in_words_and_opens_once_at_most(tmp_path, monkeypatch):
    y, _, state = plain_daemon(tmp_path)
    f = deck(state)
    y.conf["page"] = {"open": [str(opener_script(tmp_path))]}
    got = asyncio.run(y.tool_show("example-room", str(f)))                    # no screen named, no Windows: the box's opener
    assert got == {"ok": True, "line": words.SHOW_OPENED.format(name="deck.html")}
    assert (tmp_path / "opened").read_text().strip() == str(f)
    (tmp_path / "opened").unlink()
    got = asyncio.run(y.tool_show("example-room", str(f), "middle"))
    assert got["line"] == (f"{words.PAGE_NOT_PLACED.format(screen='middle', why=words.SHOW_SCREEN_NOT_HERE)}; "
                           f"{words.SHOW_OPENED.format(name='deck.html')}")
    (tmp_path / "opened").unlink()
    pc(tmp_path, monkeypatch, said='{"ok": false, "error": "screen", "of": 2, "opened": false}')
    got = asyncio.run(y.tool_show("example-room", str(f), "5"))
    why = words.PAGE_NO_SUCH_SCREEN.format(screen="5", n=2)
    assert got == {"ok": False, "error": words.PAGE_NOT_PLACED.format(screen="5", why=why)} and not (tmp_path / "opened").exists()
    pc(tmp_path, monkeypatch, said='{"ok": false, "error": "window", "opened": true}')
    got = asyncio.run(y.tool_show("example-room", str(f), "left"))           # the browser was started: never a second window
    assert got == {"ok": True, "line": words.PAGE_NOT_PLACED.format(screen="left", why=words.SHOW_NO_WINDOW)}
    assert not (tmp_path / "opened").exists()
    pc(tmp_path, monkeypatch, said='{"ok": false, "error": "browser", "opened": false}')
    got = asyncio.run(y.tool_show("example-room", str(f), "left"))           # a browser it opens no window of: the opener, with the
    assert got["ok"] and (tmp_path / "opened").read_text().strip() == UNC     # path Windows reads
    pc(tmp_path, monkeypatch, said=PLACED.replace('"locked": false', '"locked": true'))
    assert asyncio.run(y.tool_show("example-room", str(f), "right"))["line"].endswith(words.SHOW_LOCKED)
    pc(tmp_path, monkeypatch, unc="")
    assert asyncio.run(y.tool_show("example-room", str(f), "right")) == {"ok": False, "error": words.SHOW_NO_PATH.format(name="deck.html")}
    y.conf["page"] = {"open": [str(opener_script(tmp_path, body="echo 'no app for it' >&2; exit 3"))]}
    monkeypatch.setattr(pagemod, "wsl", lambda: False)
    got = asyncio.run(y.tool_show("example-room", str(f)))
    assert got == {"ok": False, "error": words.SHOW_NOT_OPENED.format(name="deck.html", why=words.PAGE_OPEN_FAILED.format(why="no app for it"))}


def test_the_windows_path_is_wslpath_s_or_none_and_the_tool_is_every_room_s(tmp_path, monkeypatch):
    (bin_ := tmp_path / "bin").mkdir()
    (w := bin_ / "wslpath").write_text('#!/bin/sh\n[ "$1" = "-w" ] && printf "%s\\n" "\\\\\\\\wsl.localhost\\\\Distro$2"\n')
    w.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    assert pagemod.windows_path(tmp_path / "deck.html") == "\\\\wsl.localhost\\Distro" + str(tmp_path / "deck.html")
    w.write_text("#!/bin/sh\necho 'not a path'\n")
    assert pagemod.windows_path(tmp_path / "deck.html") == ""
    w.unlink()
    monkeypatch.setenv("PATH", str(bin_))                                     # no wslpath at all: a box that is not WSL
    assert pagemod.windows_path(tmp_path / "deck.html") == ""
    monkeypatch.setenv("PATH", f"{bin_}:/usr/bin:/bin")
    y, _, _ = plain_daemon(tmp_path)
    assert "mcp__yerk__show" in y.spec_for("example-room").settings["permissions"]["allow"]
    tools = {t.name: t for t in asyncio.run(mcp_server.build(y, y.port, y.hook).list_tools())}
    assert list(tools["show"].inputSchema["properties"]) == ["path", "screen"]


def test_a_name_windows_could_read_as_a_command_never_reaches_an_opener_or_the_pc(tmp_path, monkeypatch):
    """PR 77's cold read: with no screen named, show handed the file to the box's opener, under WSL with wslu wslview, whose
    PowerShell `Start "<path>"` runs a $( ) inside the quotes; wslpath -w passes $, a backtick, PowerShell's own curly
    quote, %, & and ; unchanged, so a file the room names x$(calc).html would run a command on the PC outside the
    sandbox. A path holding anything but letters, digits, spaces and . _ - ( ) is refused in words before any opener or
    PowerShell is asked, its folders' names too; a plain name with spaces and brackets still opens."""
    y, _, state = plain_daemon(tmp_path)
    (wslview := tmp_path / "wslview").write_text(f"#!/bin/sh\nprintf 'Start \"%s\"\\n' \"$1\" >> \"{tmp_path}/ran\"\n")
    wslview.chmod(0o700)                                                       # wslu's: the path inside PowerShell's double quotes
    y.conf["page"] = {"open": [str(wslview)]}
    for name in ("x$(calc).html", "a`b.html", "e\u201df.html", "g'h.html", "i%PATH%.html", "k&calc.html", "m;calc.html",
                 "n|o.html", "p^q.pdf", "r!s!.png", "t\u02bau.html"):
        f = deck(state, name)
        for screen in ("", "right"):
            args, handed = pc(tmp_path, monkeypatch, unc=UNC.replace("deck.html", name))
            got = asyncio.run(y.tool_show("example-room", str(f), screen))
            assert got == {"ok": False, "error": words.SHOW_NAME.format(name=name)}, (name, screen)
            monkeypatch.setattr(pagemod, "wsl", lambda: False)                 # a box that is not WSL: its own path, the same rule
            assert asyncio.run(y.tool_show("example-room", str(f), screen)) == got
    (bad := state / "rooms" / "example-room" / "work" / "$(calc)").mkdir()
    (f := bad / "deck.html").write_text("<h1>deck</h1>")
    assert asyncio.run(y.tool_show("example-room", str(f))) == {"ok": False, "error": words.SHOW_NAME.format(name="deck.html")}
    assert not (tmp_path / "ran").exists() and not args.exists() and not handed.exists()   # no opener, no PowerShell
    plain = deck(state, "Deck (v2) final_1.html")
    pc(tmp_path, monkeypatch, unc=UNC.replace("deck.html", plain.name))
    assert asyncio.run(y.tool_show("example-room", str(plain))) == {"ok": True, "line": words.SHOW_OPENED.format(name=plain.name)}
    assert (tmp_path / "ran").read_text() == f'Start "{UNC.replace("deck.html", plain.name)}"\n'
    assert asyncio.run(y.tool_show("example-room", str(plain), "left"))["ok"] and args.exists()
