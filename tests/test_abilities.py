"""Checks abilities.toml, the manifest of every ability Yerk AI has and every one it knows how to build but does not ship. The cases: every
described one carries what, how, tier, needs, price, free path and why it is not built, none holds the
shape of a piece of anyone's life (patterns, never names), `yerk abilities` stays one phone-width line per ability, `yerk explain` answers
every described one from its entry, and a room that names one, or asks "what can you do", gets the manifest's answer whole within the
recall cap, the size limit on the notes a message arrives with. The described entries are decision 26, the patterns rule decision 22."""
import json
import re
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

from yerk_ai import abilities, addons, cli, hooks, mcp_server, words
from yerk_ai.config import EXAMPLE_PRIVATE, table
from yerk_ai.daemon import Yerkd
from fake_channel import ConsoleChannel
from yerk_ai.engine import SessionTable

DOTS = (words.DONE, words.WORKING, words.NOT_DONE, words.NOTHING)

TAB = abilities.manifest()
DESCRIBED = {k: v for k, v in TAB.items() if v.get("state") == "described"}
DEV = ("/home/", "exited", "Result=", "SERVICE_RESULT", "systemd", "Traceback", "Errno", "<t:", "—")
ROLE = (r"CEO|CIO|CTO|CFO|VP|boss|manager|director|recruiter|lawyer|doctor|mom|dad|mother|father"
        r"|brother|sister|wife|husband|girlfriend|boyfriend|cousin|aunt|uncle|grandma|grandpa|coworker|colleague|friend")
LIFE = {
    "a name and a role": re.compile(rf"\b[A-Z][a-z]+ [A-Z][a-z]+,? (?:(?:the|our|my|his|her|their) )?(?:{ROLE})\b"),
    "a #room": re.compile(r"(?<![\w&])#[A-Za-z][\w-]+"),
    "an @handle": re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,}"),
    "a dollar amount": re.compile(r"\$\s?\d"),
    "a street address": re.compile(r"\b\d{1,5} [A-Z][a-z]+(?: [A-Z][a-z]+)? (?:St|Street|Ave|Avenue|Rd|Road|Blvd"
                                   r"|Boulevard|Dr|Drive|Ln|Lane|Way|Cres|Crescent|Ct|Court|Pl|Place)\b"),
    "a date": re.compile(r"\b(?:19|20)\d\d-\d\d-\d\d\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? "
                         r"\d{1,2}\b"),
}
# everyday messages that ask about no ability: none of them may pull an ability's answer into the room
ORDINARY = ["what is on the list today?", "can you back up a second", "are you down to play later",
            "I lost my glasses again", "it's on my mac somewhere", "I have to fill out a form for work",
            "I watched a youtube video about it", "thanks, that works", "what time is it", "ok send it"]


def life(text: str) -> list[str]:
    return [f"{kind}: {m.group()}" for kind, rx in LIFE.items() for m in rx.finditer(text)]


def text_of(ab: dict) -> str:
    return json.dumps(ab, ensure_ascii=False)


def test_the_manifest_is_parsed_once_and_read_again_when_a_file_changes_or_an_add_on_comes_or_goes(tmp_path, monkeypatch):
    """Every message looks its abilities up (asked, through the daemon's recall_for); the parsed manifest is kept while
    abilities.toml and each add-on's entry keep their size and modification time and the add-ons installed stay the same,
    and it is always what a fresh read gives: after a file changes, an add-on comes and one goes (cleanup F8-09)."""
    core, state = tmp_path / "abilities.toml", tmp_path / "state"
    shutil.copy(table("abilities.toml"), core)
    monkeypatch.setattr(abilities, "table", lambda name: core)
    reads: list[str] = []
    for mod in (abilities, addons):
        monkeypatch.setattr(mod, "load_toml", lambda p, real=mod.load_toml: reads.append(Path(p).name) or real(p))

    def addon(name: str, what: str) -> None:
        (folder := state / addons.DIR / name).mkdir(parents=True, exist_ok=True)
        (folder / addons.MANIFEST).write_text(f'[abilities.{name}]\nstate = "described"\nwhat = "{what}"\n')

    def fresh() -> dict:                                       # read from disk, nothing kept
        return dict(tomllib.loads(core.read_text())["abilities"]) | {
            f.parent.name: next(iter(tomllib.loads(f.read_text())["abilities"].values()))
            for f in sorted((state / addons.DIR).glob(f"*/{addons.MANIFEST}"))}
    addon("hello", "says hello")
    first = abilities.manifest(state)
    assert first == fresh() and first["hello"]["what"] == "says hello" and reads == ["abilities.toml", "addon.toml"]
    reads.clear()
    assert abilities.manifest(state) == first and abilities.asked("zzqx", SimpleNamespace(state=state)) == [] and reads == []
    core.write_text(core.read_text() + '\n[abilities.extra]\nstate = "described"\nwhat = "one more"\n')
    addon("hello", "says hello twice")
    assert abilities.manifest(state) == fresh() and abilities.manifest(state)["extra"]["what"] == "one more"
    addon("bye", "says bye")
    assert abilities.manifest(state) == fresh() and abilities.manifest(state)["bye"]["what"] == "says bye"
    shutil.rmtree(state / addons.DIR / "hello")
    assert abilities.manifest(state) == fresh() and "hello" not in abilities.manifest(state)


def test_every_entry_says_built_or_described():
    assert TAB and all(ab.get("state") in ("built", "described") for ab in TAB.values()), \
        {k: v.get("state") for k, v in TAB.items()}
    assert len(DESCRIBED) > 40 and "voice" in DESCRIBED


def test_every_described_entry_has_what_how_tier_needs_cost_free_path_and_why():
    for name, ab in DESCRIBED.items():
        for field in ("what", "needs", "price", "free_path", "why_not"):
            assert isinstance(ab.get(field), str) and ab[field].strip(), (name, field)
        assert ab["how"] and all(isinstance(x, str) and x.strip() for x in ab["how"]), name
        assert ab["tier"] in ("free", "key", "paid"), name
        assert ab["tier"] == "free" or ab.get("provider"), (name, "a key or paid ability names its provider kind")
        assert ab["asked_as"] and all(p == p.lower() for p in ab["asked_as"]), name
        assert ab["what"].endswith(".") and not re.search(r"\.\s+[A-Z]", ab["what"]), (name, "one sentence")
        if "upgrade" in ab:
            assert TAB.get(ab["upgrade"], {}).get("state") == "built", (name, "upgrade names a built ability")
        if "layer" in ab:
            assert isinstance(ab["layer"], int) and 1 <= ab["layer"] <= 10, name


def test_no_described_entry_holds_a_piece_of_anyones_life_or_a_word_a_room_must_not_see():
    hits = {name: life(text_of(ab)) + [w for w in DEV if w in text_of(ab)] for name, ab in DESCRIBED.items()}
    assert not {k: v for k, v in hits.items() if v}, hits


def test_the_life_patterns_catch_what_they_are_for():
    planted = {"a name and a role": "ask Jane Doe, the manager", "a #room": "post it in #general",
               "an @handle": "ping @someone", "a dollar amount": "it costs $20 a month",
               "a street address": "drive to 12 Main Street", "a date": "due 2030-01-02, or on March 3"}
    for kind, text in planted.items():
        assert any(h.startswith(kind) for h in life(text)), (kind, text)
    assert not life("Reads a spreadsheet and keeps a net worth number a day, paid to the provider.")


def test_yerk_abilities_is_one_phone_width_line_per_ability_built_first(tmp_path, capsys):
    assert cli.main(["abilities", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    shown, tail = lines[:-1], lines[-1]
    assert tail.startswith("yerk explain") and len(shown) == len(TAB)
    assert all(x[:1] in "".join(DOTS) and words.width(x) <= words.PHONE for x in shown), shown
    names = [x.split(" ", 1)[1].split(":", 1)[0] for x in shown]
    assert sorted(names) == sorted(TAB)
    states = [TAB[n]["state"] for n in names]
    assert states == sorted(states, key=lambda st: st == "described")          # built ones, then described
    assert sum(x.endswith(": " + words.ability("described")[1]) for x in shown) == len(DESCRIBED)


def test_yerk_explain_answers_every_described_ability_from_its_entry(tmp_path, capsys):
    for name, ab in DESCRIBED.items():
        assert cli.main(["explain", name, "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0] == f"{words.NOTHING} {name}: {words.ability('described')[1]}" and lines[1] == ab["what"]
        at = lines.index("How it would be built:")
        assert lines[at + 1:at + 1 + len(ab["how"])] == [f"- {x}" for x in ab["how"]], name
        for want in (f"Needs: {ab['needs']}", f"Cost: {ab['price']}", f"Free path: {ab['free_path']}",
                     f"Why it is not built: {ab['why_not']}"):
            assert want in lines, (name, want)
        if "upgrade" in ab:
            assert f"Built part: {ab['upgrade']} (yerk explain {ab['upgrade']})" in lines, name
        assert not [w for w in DEV if w in "\n".join(lines)], name


def test_each_ability_is_found_by_its_own_phrases_and_its_answer_fits_whole(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    cap = int(y.budgets["per_message"]["on_hit"]["recall"])
    seen: dict[str, str] = {}
    for name, ab in TAB.items():
        for phrase in ab.get("asked_as", []):
            assert phrase not in seen, (phrase, seen.get(phrase), name)
            seen[phrase] = name
            found = abilities.asked(f"hey, {phrase}?", y.memory, TAB)
            assert name in [e["key"] for e in found], (name, phrase)
            mine = [e for e in found if e["key"] == name]
            assert hooks.render_recall(mine, [], cap), (name, "its answer must fit the recall cap whole")


def test_a_room_asked_to_open_a_page_in_the_browser_on_the_pc_hears_the_plug_in_way(tmp_path):
    """need-2026-10-26-21: a room asked to open a tracker in the browser on the PC met that browser down (it had
    quit with its last tab, and its port reset every call) behind an error that was a stack trace's first line, and read
    the page in the Mac's browser instead, another profile, which met a sign-in. The plug-in that drives the browser lives
    outside this repo and starts it again before each call; asked that way, a room gets the browser entry whole: a room
    holding such a tool opens the page with it on the computer named, a relaunch before each call, a browser still down
    told to the owner, and no read moved to another computer unasked."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("open the team's tracker in my browser on my pc and see whether the hours can be logged there",
                            y.memory, TAB)
    assert [e["key"] for e in found] == ["browser"]
    text = found[0]["text"]
    assert text.startswith(f"browser, a Yerk AI ability ({words.ability('described')[1]}): Drives a real browser")
    for want in ("A relaunch before each call", "The computer the owner names", "Why it is not built: Not in this repo",
                 "a room that holds such a tool opens the page with it, on the computer the owner names",
                 "when the tool says that browser is down the room tells the owner, never reading on another computer unasked"):
        assert want in text, want
    assert text in hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    told = abilities.explain("browser", TAB["browser"], TAB, y.memory)
    assert next(x for x in told if x.startswith("- A relaunch before each call:")).endswith(
        "is said to be down in one line with its reason, never a stack trace's first line.")
    assert not abilities.asked("the tracker is open on my pc already, I logged the hours myself", y.memory, TAB)


def test_a_room_asked_for_the_page_on_the_owner_s_middle_monitor_hears_the_named_screen_way(tmp_path):
    """need-2026-11-10-82: a room told to open the live page on the owner's middle monitor had it put on the left one by the
    browser, unpainted on a screen asleep, and was told it opened. Asked that way, it gets page-mac whole under the recall
    cap: the tool's `screen` opens it maximized there with the screen woken and no click, and says when it painted."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("open the coding page on my middle monitor, the wrong monitor got it last time", y.memory, TAB)
    assert "page-mac" in [e["key"] for e in found]
    text = next(e["text"] for e in found if e["key"] == "page-mac")
    for want in ("`screen` (else [page] screen) names the owner's monitor", "the screen woken with no click",
                 "the room is told it painted only once the tab says so"):
        assert want in text, want
    assert text in hooks.render_recall([e for e in found if e["key"] == "page-mac"], [], int(y.budgets["per_message"]["on_hit"]["recall"]))


def test_a_room_asked_to_book_a_free_table_hears_the_page_decides_way(tmp_path):
    """need-2026-10-26-23: a room asked to book a free table filled none of the Confirm page's fields (a name, a phone
    number, an email, no card, no deposit): the page sat on a checkout address, and the browser refused every field and
    click on it as a payment page, the owner's own order included. Asked that way, a room gets free-bookings whole: the
    page decides, not its address; on a look-alike each set and click goes on the owner's word; a page that shows a card
    field, a payment frame or money is still never touched."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("ok book a table for two at the place on the corner, thursday at seven, in my name", y.memory, TAB)
    assert [e["key"] for e in found] == ["free-bookings"]
    text = found[0]["text"]
    assert text.startswith(f"free-bookings, a Yerk AI ability ({words.ability('described')[1]}): Fills and confirms a free "
                           "booking form")
    for want in ("The page decides, not its address", "Money read from the page's whole text",
                 "each field set and each click only on the owner's word", "On a payment page, nothing set or clicked",
                 "Why it is not built: It rides on the browser ability",
                 "tells a checkout look-alike from a payment page has it there"):
        assert want in text, want
    assert text in hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("the table is booked, they texted me the confirmation", y.memory, TAB)


def test_a_room_asked_to_put_a_photo_on_a_web_form_hears_the_upload_way(tmp_path):
    """need-2026-11-01-31: a room filled a listing form whose site wants a photo, and the browser's `set` on the file input
    met an InvalidStateError (a file input takes no typed value), so the form never sent. Asked that way, a room gets
    form-files whole: the browser plug-in's upload, the file copied to the browser's computer first, the owner's word."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("the site says to fix the errors: can you add photos to the listing from the ones I sent?", y.memory, TAB)
    assert [e["key"] for e in found] == ["form-files"]
    text = found[0]["text"]
    assert text.startswith(f"form-files, a Yerk AI ability ({words.ability('described')[1]}): Puts a file from the room's work folder")
    for want in ("One browser action, upload", "Copied first to the computer the browser runs on", "Set over the browser's "
                 "developer protocol", "The owner's word before each upload", "whose browser plug-in carries the upload action"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("the photos are on the listing already, I added them myself", y.memory, TAB)


def test_a_room_asked_to_keep_a_key_a_page_shows_hears_the_keep_way_never_the_read(tmp_path):
    """need-2026-11-09-76: a room asked to keep a bot's API key, shown on a signed-in settings page, in a locked file found
    the page's Copy button copied onto the browser computer's clipboard, a room cannot write the secrets folder, and
    reading the page put the key in its log. Asked that way, a room gets page-keys whole: the browser plug-in's keep, the
    value read in the page and written by the plug-in as a new file in the secrets folder, never shown, never over one."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("make the example bot and keep the api key it shows in a locked file", y.memory, TAB)
    assert [e["key"] for e in found] == ["page-keys"]
    text = found[0]["text"]
    assert text.startswith(f"page-keys, a Yerk AI ability ({words.ability('described')[1]}): Saves a key or token a web page shows")
    for want in ("One browser action, keep", "the page's own Copy button, clicked with the copy caught inside the page",
                 "Written by the plug-in itself", "Never over a file already there", "only on the owner's word",
                 "whose browser plug-in carries the keep action", "never by reading the page",
                 # the cold read (2026-11-09): by the page's url, the page never opened or read first, every key scrubbed after
                 "with the page's url", "the room never opening or reading that page first", "scrubs every NAME.key's value",
                 "a later read of that page shows <key>", "given the page's url, never by reading the page"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("the key works now, thanks", y.memory, TAB)


def test_a_room_whose_form_never_takes_a_set_value_hears_the_typing_way(tmp_path):
    """need-2026-11-09-75: a room drove a quote form to its last text field, and the browser's `set` put the value there
    (the field showed it) while the page's React form never had it, so its Next stayed disabled. Asked that way, a room
    gets the browser entry whole, with its typing line: the plug-in's type, the browser's own text input, the field left so
    its checks run (a key of its own would push the abilities answer, 1,998 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("the form shows the number but the next button stays disabled, can you get past it?", y.memory, TAB)
    assert [e["key"] for e in found] == ["browser"]
    text = found[0]["text"]
    assert text.startswith(f"browser, a Yerk AI ability ({words.ability('described')[1]}): Drives a real browser")
    for want in ("A field typed as keys type it, by the browser action type in place of set, for a React or Angular form "
                 "that never sees a value a script sets and keeps its Next button disabled.", "Drives a real browser"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("the form went through and the quote came back", y.memory, TAB)


def test_a_room_asked_for_a_picture_of_its_tab_hears_the_shot_way_never_the_desktops(tmp_path):
    """need-2026-10-28-28: a room asked for a picture of the page it was filling in found no screenshot among the browser's
    actions, and the desktop's screenshot shoots the owner's screen, where that browser shows no window. Asked that way, a
    room gets tab-shots whole, never pc-control's screenshot: the browser plug-in's shot, drawn by the browser itself,
    saved in the room's work folder to Read and send; asked for the screen, it still gets the desktop's entry."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("ok, can you send me a pic of the page you're filling in?", y.memory, TAB)
    assert [e["key"] for e in found] == ["tab-shots"]
    text = found[0]["text"]
    assert text.startswith(f"tab-shots, a Yerk AI ability ({words.ability('described')[1]}): Saves a picture of what one of the room's tabs")
    for want in ("One browser action, shot", "Drawn by the browser itself, so a tab with no window on the owner's screen is shot",
                 "Saved in the room's own work folder, to open with Read and send with send", "the owner's screen is never shot",
                 "A tab that never paints is named, with the way past it", "whose browser plug-in carries the shot action"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("take a screenshot of my whole screen", y.memory, TAB)] == ["pc-control"]
    assert not abilities.asked("the page looks right now, no need for a screenshot", y.memory, TAB)


def test_a_room_asked_to_use_a_link_in_an_email_hears_the_links_way_never_the_mailbox_page(tmp_path):
    """need-2026-11-01-33: a room asked to cancel a booking by the Cancel link in its confirmation mail read the mail and
    had "Modify Cancel" as words, no address (the mail tool's text strips the tags), and the mail would not open by its
    row on the mailbox's web page. Asked that way, a room gets mail-links whole: the mail read lists each link's words
    beside its address, the room opens the one it was asked for in the browser by that address, and opening one is
    clicking it in the mail, so only for what the owner asked; a mail ask with no link in it still gets the email entry."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you cancel the table for me with the cancel link in the confirmation email?", y.memory, TAB)
    assert [e["key"] for e in found] == ["mail-links"]
    text = found[0]["text"]
    assert text.startswith(f"mail-links, a Yerk AI ability ({words.ability('described')[1]}): Gives a room each link in one of the "
                           "owner's emails, numbered, its words beside its address")
    for want in ("One mail read, with its links", "Each address whole or not at all",
                 "Opened in the browser by its address, never found on the mailbox's web page", "Only for what the owner asked",
                 "a room asked to use a link in an email reads the mail, then opens that link's address with the browser"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    told = abilities.explain("mail-links", TAB["mail-links"], TAB, y.memory)
    assert next(x for x in told if x.startswith("- Only for what the owner asked:")).endswith("(cancel, confirm, unsubscribe).")
    assert [e["key"] for e in abilities.asked("search my email for the receipt", y.memory, TAB)] == ["email"]
    assert not abilities.asked("the table is cancelled, they sent me a cancellation email", y.memory, TAB)


def test_a_room_asked_for_a_link_or_a_button_in_an_email_hears_the_numbered_way_and_the_owners_word(tmp_path):
    """need-2026-11-07-74: a room asked whether a guest booking was still live read the booking's mails and got their words
    with every link stripped (the plug-in's main had never taken need 33's list), and the booking site kept no account page
    for a guest, so it filed a link from an email opened in the browser, or a button in it clicked. Asked that way, a room
    gets mail-links whole: each link numbered and opened by the mail and that number, so a long tracking address is never
    copied out, and one whose words say it acts (a Cancel, an answer such as I'll be there, or no words at all) held for the
    owner's word; a mail ask with neither a link nor a button in it still gets the email entry."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for ask in ("can you open the link from an email the restaurant sent and check the booking is still on tonight?",
                "just click the button in that email, the one that says view reservation"):
        found = abilities.asked(ask, y.memory, TAB)
        assert [e["key"] for e in found] == ["mail-links"], ask
    text = found[0]["text"]
    assert text.startswith(f"mail-links, a Yerk AI ability ({words.ability('described')[1]}): Gives a room each link in one of the "
                           "owner's emails, numbered, its words beside its address")
    for want in ("Opened by the mail and the link's number, never copied out", "Held for the owner's word when its words say it acts",
                 "by the mail and the link's number where its browser takes them"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    told = abilities.explain("mail-links", TAB["mail-links"], TAB, y.memory)
    assert [x.split(":")[0] for x in told if x.startswith(("- Opened", "- Only", "- Held"))] == [
        "- Opened in the browser by its address, never found on the mailbox's web page", "- Opened by the mail and the link's number, never copied out",
        "- Only for what the owner asked", "- Held for the owner's word when its words say it acts, as a click's label is"]
    assert next(x for x in told if x.startswith("- Opened by")).endswith("so a long tracking address reaches the page whole, and the reply names the link it opened.")
    assert next(x for x in told if x.startswith("- Held")).endswith("an answer such as I'll be there, Yes or No, and a link that shows no words.")
    assert [e["key"] for e in abilities.asked("search my email for the receipt", y.memory, TAB)] == ["email"]
    assert not abilities.asked("the button on the booking form is greyed out", y.memory, TAB)


def test_a_room_asked_to_read_a_file_an_email_carries_hears_the_attachment_way_never_to_ask_for_the_file(tmp_path):
    """need-2026-11-11-85: a room asked to read a quote an email carried as a PDF, to confirm what it said, had the file's
    name from the mail tool's read and no way to the file (the mail tool neither saved nor opened one, the attachments tool
    takes a Discord message's files only, and its shell has no network), so it asked the owner to send the file into the
    room. Asked that way, a room gets mail-files whole: the read numbers the mail's files, the plug-in's attachment action
    saves one by that number in the room's work folder, and the room opens the path with Read, never asking for the file;
    a mail ask with no file in it still gets the email entry, and a file posted again from a Discord message the
    attachments entry."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for ask in ("can you read the quote the insurer sent? it's a pdf attached to the email, I need the deductible",
                "check the attachment in my gmail from the bank and tell me the total"):
        found = abilities.asked(ask, y.memory, TAB)
        assert [e["key"] for e in found] == ["mail-files"], ask
    text = found[0]["text"]
    assert text.startswith(f"mail-files, a Yerk AI ability ({words.ability('described')[1]}): Gives a room a file one of the "
                           "owner's emails carries")
    for want in ("One mail read, with its files; Saved by its number, or every one; Under the file's own name made safe; "
                 "Never out of the room's folder; Read as the mail is read; Refused in one line with its reason",
                 "saves the file by its number with that action, then opens the path it names with Read, never asking the owner "
                 "to send the file into the room"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    told = abilities.explain("mail-files", TAB["mail-files"], TAB, y.memory)
    assert next(x for x in told if x.startswith("- Saved by its number")).endswith("and returns its path to open with Read.")
    assert next(x for x in told if x.startswith("- Never out of")).startswith(
        "- Never out of the room's folder: the reader walks to that folder a part at a time without following a link")
    assert [e["key"] for e in abilities.asked("search my email for the receipt", y.memory, TAB)] == ["email"]
    assert [e["key"] for e in abilities.asked("send the file again", y.memory, TAB)] == ["attachments"]
    assert not abilities.asked("I attached the file to my reply", y.memory, TAB)


def test_a_room_asked_for_a_worker_a_script_starts_that_ends_silently_hears_the_no_reply_way(tmp_path):
    """need-2026-11-09-77: a timer script started a worker with `yerk worker start` each time something might need a look,
    and each run that answered NO_REPLY still asked its room for a relay turn. Asked that way, a room gets the workers entry
    whole, with its silent end: none when the last message is exactly NO_REPLY, the tool's or a script's alike (a key of its
    own would push the abilities answer, 1,998 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can I start a worker from a script that ends silently on NO_REPLY?", y.memory, TAB)
    assert [e["key"] for e in found] == ["workers"]
    text = found[0]["text"]
    assert text.startswith(f"workers, a Yerk AI ability ({words.ability('on')[1]}): Heavy work leaves the room")
    assert ("none when its last message is exactly NO_REPLY, however it was started (the tool, or `yerk worker start` in a "
            "timer's script, at any --effort): its card settles quiet and the ledger says why.") in text
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("the worker finished and the report is saved", y.memory, TAB)


def test_a_room_asked_for_a_scheduled_jobs_own_thinking_level_hears_the_schedule_way(tmp_path):
    """need-2026-11-09-79: a room on max started a bot's research runs as standing agent jobs, and each job's worker took the
    room's level, since neither the schedule tool nor `yerk schedule add` had a thinking level or a model. Asked that way, a
    room gets the schedule entry whole, with the job's own level and model (a key of its own would push the abilities
    answer, 1,993 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can each job's effort be high on a cheaper model while this room stays on max?", y.memory, TAB)
    assert [e["key"] for e in found] == ["schedule"]
    text = found[0]["text"]
    assert text.startswith(f"schedule, a Yerk AI ability ({words.ability('on')[1]}): The schedule tool in every room")
    assert ("An agent job sets its own worker's thinking level and model (the tool's `effort` and `model`, `yerk schedule add "
            "--effort --model`, or the keys in jobs.toml; a model an alias or a full id on the install's list)") in text
    assert "one that sets neither runs on the room's live level and model" in text
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("what is the job's model now?", y.memory, TAB)] == ["schedule"]
    assert not abilities.asked("the job ran and its card settled quiet", y.memory, TAB)


def test_a_room_asked_for_no_chat_box_on_its_page_hears_the_page_tool_s_switch(tmp_path):
    """need-2026-11-09-81: asked on the live page a second time for no chat box, a room found no switch for the chat in its
    page tool, only the close button in the owner's browser. Asked that way, or about the chat on the right, or to bring
    it back, a room gets page-talk whole, with the page tool's switch (a key of its own would push the abilities answer,
    1,993 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for ask in ("I do not want a chat box on the page", "why is the chat on the right still there?", "bring the chat back"):
        found = abilities.asked(ask, y.memory, TAB)
        assert [e["key"] for e in found] == ["page-talk"], ask
        assert "the chat closes to nothing (its button, or the room's `page` tool on the owner's ask, which opens it again too)" in found[0]["text"]
        assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    how = TAB["page-talk"]["how"][0]
    assert "`page` tool's `chat` closes it (closed) or opens it (open) on every open tab at once" in how


def test_a_room_asked_why_its_discord_replies_land_in_the_code_hears_the_page_talk_way(tmp_path):
    """need-2026-11-09-80: with a live page's chat closed, every line a room said from any thread went into the program, so a
    Discord answer and a job's nightly post landed in the owner's code. Asked that way, a room gets the page-talk entry
    whole, only its answers to the page in the program and every other line on the chat (a key of its own would push the
    abilities answer, 1,993 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("why are your discord replies in my code on the page?", y.memory, TAB)
    assert [e["key"] for e in found] == ["page-talk"]
    text = found[0]["text"]
    assert text.startswith(f"page-talk, a Yerk AI ability ({words.ability('on')[1]}): A room's live page holds code, talk")
    assert "what it says anywhere else (a Discord answer, a job's post) stays on the chat" in text
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("the patch notes in my code again", y.memory, TAB)] == ["page-talk"]
    assert not abilities.asked("the patch notes went out on discord", y.memory, TAB)


def test_a_room_asked_for_the_words_of_a_text_the_owner_sent_hears_the_blank_text_way(tmp_path):
    """need-2026-11-09-83: a room asked to confirm the words of a text the owner had just sent got an empty text field, as
    on every text they sent that evening. Asked that way, a room gets the text entry whole, with the words read from the rich
    copy and a blank said with its reason (a key of its own would push the abilities answer past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("what did i text her just now? confirm the exact words", y.memory, TAB)
    assert [e["key"] for e in found] == ["text"]
    text = found[0]["text"]
    assert "the owner's own sent ones too, whose words Messages often keeps only in its rich copy" in text
    assert "A blank text's words" in text
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert not abilities.asked("she sent the photo again", y.memory, TAB)


def test_a_room_asked_to_type_into_a_field_it_focused_hears_the_type_way_and_the_web_component_form(tmp_path):
    """need-2026-11-09-74: a room asked to run a quote on a form drawn inside a web component saw the form only in a shot:
    the browser's read and its list of fields stopped at the page's footer, and a click by position focused the form's
    first field with no action to type into it. Asked that way, a room gets form-typing whole: the browser plug-in's type,
    into the field that has focus, typed by the browser itself; a select takes the option its words name; the web
    component's form is read, listed, filled and clicked by label; an Enter waits for the owner's word, and nothing is typed
    into a card field; a message about typing up a form gets no ability."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    for ask in ("the quote page draws its form but nothing lists it, can you type into the field you clicked?",
                "its fields are not listed, the form sits in a shadow root"):
        found = abilities.asked(ask, y.memory, TAB)
        assert [e["key"] for e in found] == ["form-typing"], ask
    text = found[0]["text"]
    assert text.startswith(f"form-typing, a Yerk AI ability ({words.ability('described')[1]}): Types into whatever field has focus "
                           "in one of the room's tabs")
    for want in ("One browser action, type; The field found first, as the browser will type into it; A select takes the option its "
                 "words name; A web component's form read too; The owner's word before an Enter",
                 "whose browser plug-in carries the type action", "clicks its field by position and types into it"):
        assert want in text, want
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    told = abilities.explain("form-typing", TAB["form-typing"], TAB, y.memory)
    assert next(x for x in told if x.startswith("- One browser action, type:")).startswith(
        "- One browser action, type: text into the field that has focus, typed by the browser itself over its developer protocol")
    assert next(x for x in told if x.startswith("- A web component's form read too:")).endswith(
        "one whose inside is closed to the page is reached by type alone.")
    held = next(x for x in told if x.startswith("- The owner's word before an Enter"))
    assert "nothing typed into a card field, the owner's word or not" in held and held.endswith("as one on the page does.")
    assert not abilities.asked("I have to type up the form for work tonight", y.memory, TAB)


def test_ordinary_messages_pull_no_ability_into_the_room(tmp_path):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    assert {m: [e["key"] for e in abilities.asked(m, y.memory, TAB)] for m in ORDINARY} == {m: [] for m in ORDINARY}


def test_a_room_asking_what_can_you_do_gets_every_ability_by_state(tmp_path, capsys):
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    y.add_room("example-room", ConsoleChannel())
    SessionTable(tmp_path / "sessions.json").bind("example-room", "sid-w", "one")
    app = mcp_server.build(y, y.port, y.hook).streamable_http_app()
    with TestClient(app) as c:
        out = c.post("/hook", json={"hook_event_name": "UserPromptSubmit", "session_id": "sid-w",
                                    "prompt": "so what can you do?"}).json()
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "- abilities, a Yerk AI ability (on): Says what Yerk AI can do" in ctx and "(yerk explain abilities)" in ctx
    assert f"{words.ability('described')[1]} ({len(DESCRIBED)}):" in ctx
    assert all(re.search(rf"[ (:]{re.escape(n)}[,.]", ctx) for n in TAB), [n for n in TAB if n not in ctx]
    assert cli.main(["explain", "abilities", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    said = capsys.readouterr().out
    assert said.startswith(f"{words.DONE} abilities: on") and all(n in said for n in TAB)


def test_a_room_asked_to_talk_in_the_code_with_bubbles_hears_the_page_talk_way(tmp_path):
    """need-2026-11-09-82: asked to talk to the owner inside the code, a room had its replies written into the program as plain
    #> lines in a one-colour editor, nothing on screen while it thought. Asked that way, a room gets the page-talk entry
    whole: bubbles under the line asked from, never in solution.py, the typing mark, Ctrl+Enter on a line and Python colours
    (a key of its own would push the abilities answer, 1,993 at main, past the 2,000 recall cap)."""
    y = Yerkd(EXAMPLE_PRIVATE, tmp_path)
    found = abilities.asked("can you talk to me in the code with bubbles, not a chat box?", y.memory, TAB)
    assert [e["key"] for e in found] == ["page-talk"]
    text = found[0]["text"]
    assert ("its answers coming back as bubbles between the rows under the line asked from, never written into solution.py, "
            "with a typing mark there while it thinks; Ctrl+Enter or a line's number asks about that line") in text
    assert "the editors colour Python, the owner's own # lines in a colour of their own" in text
    assert hooks.render_recall(found, [], int(y.budgets["per_message"]["on_hit"]["recall"]))
    assert [e["key"] for e in abilities.asked("please upgrade the page", y.memory, TAB)] == ["page-talk"]
    assert not abilities.asked("the bubbles in the bath", y.memory, TAB)


SHIPPED_IN_PART = ("taste", "status-card", "safe-restarts", "dead-turns", "watchdog", "shell-guards", "import", "read-documents")


def test_an_ability_that_ships_says_built_and_one_that_ships_in_part_names_what_is_missing(tmp_path, capsys):
    """F9-07: a room asked whether it can do what the install ships never hears that it cannot. prompt-diff ships with the
    morning diff, measured (decision 26) by tests/test_observe.py's planted payload change that shows the next morning in the
    machine room only and tests/test_sheet_hard.py's part over its line said there once a day, so it says built; the eight
    that ship in part each say what is still missing, never only a layer that has shipped."""
    assert TAB["prompt-diff"]["state"] == "built" and "why_not" not in TAB["prompt-diff"], TAB["prompt-diff"].get("why_not")
    stale = {n: TAB[n]["why_not"] for n in SHIPPED_IN_PART if TAB[n]["state"] != "described" or "still missing: " not in TAB[n]["why_not"]}
    assert not stale, stale
    assert cli.main(["explain", "prompt-diff", "--private", str(EXAMPLE_PRIVATE), "--state", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{words.DONE} prompt-diff: {words.ability('on')[1]}" and abilities.HOW["built"] in lines
    assert not [x for x in lines if x.startswith("Why it is not built")], lines
