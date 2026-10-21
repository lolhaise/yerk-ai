"""`yerk curate`: each night it tidies the fact cards, memory files of standing facts about one subject each, and adds new facts
to them from the day's notes; `--since` runs the same over a range of days. units/yerk-curate.timer runs it after the doctor's
(Layer 11, claims 6 and 15, decision 42). Two parts; no model rewrites a card alone. The sweep is code: on each card in the
tree's facts/, a line said twice, a superseded line and a line `until` a date now past become blank (cites into a card hold), and
a cite whose quoted words left its line moves only when they sit on exactly one line of its file; every other drift is a finding,
never a guess. The promotion: a pack per card from each day's sources as the card's reader may read them; a worker that sees the
pack alone proposes lines, a second knocks them down (curate.md), code checks shape, head, secrets and that each cite is a line
of the pack, and the lines land, appended with a backup, only when the bench does not fall. A correction to a line on file is
the morning diff. Each run is kept in <state>/curate.json."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import doctor, memindex, memory, mentions, words
from .accounts import account_env, load_accounts
from .config import load_toml, write_private
from .engine import claude_bin

LAST, PROMPTS = "curate.json", Path(__file__).with_name("curate.md")
PACK_CHARS, CALL_CHARS = 20_000, 150_000            # a card's pack; one worker call's cards and packs together
LINE = re.compile(r"- ([^:\n]{2,60}): (.+) \(src: ([^()]+)\)")                  # a card line: `- Key: fact (src: file:line, :line)`
CITE = re.compile(r"(?:^|,)\s*([^,:]*):(\d+)(?:-(\d+))?\s*(?=,|$)")             # file:line or file:from-to; no file: the one before
MARK = re.compile(r"⛔|do not state", re.I)                                     # accuracy marks: a heading's covers what it holds
GONE = re.compile(r"(?i:\[superseded)|SUPERSEDED|(?i:^\W*superseded\b)")         # Yerk's own mark, the word in capitals after anything
QUOTE = re.compile(r"[\"“]([^\"”]{20,})[\"”]")                                   # long enough to name one line
FLAT = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def flat(text: str) -> str:
    """Curly quotes and apostrophes as straight ones, on both sides of a quote's match."""
    return text.translate(FLAT)
UNTIL = re.compile(r"\step (\and{4}-\for{2}-\and{2})\note")
SECRET = re.compile(r"passw(?:or)?d|passcode|one-time code|\b(?:sk|rk)-[\w-]{16,}|\bgh[pousr]_\w{20,}|\bxox[abp]-|\bAKIA[0-9A-Z]{16}|"
                    r"X-Amz-|[?&](?:sig|token|key)=|\b\d(?:[ -]?\d){11,18}\b|\b[A-Za-z0-9+/_-]{40,}\b", re.I)
KIN = {"mom": "mother", "dad": "father", "grandma": "grandmother", "grandpa": "grandfather"}   # a relation also said as a name
FIX = re.compile(r"^(\S+?\.md) fix (\d+): (- .+)$", re.M)                       # a proposed correction: card, its line, the new line


def kept(rows: list[str]) -> list[bool]:
    """Which lines a pack may hold: none marked, none under a marked heading or bullet (to the next of its level or
    higher), none superseded."""
    out, up = [], [(0, False)]                           # the headings and bullets open above, by depth, and their mark
    for x in rows:
        d = len(h[1]) if (h := re.match(r"(#{1,6}) ", x)) else 9 + len(x) - len(x.lstrip()) if x.strip() else 99
        up = [u for u in up if u[0] < d]
        up.append((d, bool(MARK.search(x)) or up[-1][1]))
        out.append(not up[-1][1] and not GONE.search(x))
    return out


def content(head: str) -> list[str]:
    """A head's words that are not stop words: fewer than two and it is a common word unless it is a name."""
    return [w for w in mentions.tokens(head) if w not in mentions.KEY_STOP | mentions.SENTENCEY | memindex.STOP]


def title(card: Path, text: str) -> str:
    return next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), card.stem.replace("-", " "))


def proper(src: list[tuple[date, Path, list[tuple[int, str]]]]) -> set[str]:
    """The words the night's lines only ever say capitalised, and at least once mid-sentence: names ("Ada", "ACME"),
    never a common word ("Back") that only opens a line."""
    cap, mid, low = set[str](), set[str](), set[str]()
    for _, _, rows in src:
        for _, x in rows:
            for m in re.finditer(r"(?<![\w'])[A-Za-z][\w'-]*", x):
                (cap if m[0][0].isupper() else low).add(w := m[0].lower())
                mid |= {w} if m[0][0].isupper() and re.search(r"[\w,]\s*$", x[:m.start()]) else set()
    return (cap & mid) - low


def names(card: Path, text: str, said_as: set[str] = set()) -> re.Pattern[str]:
    """What a card answers to, any case: its title, its first key (and a name's first word), each key of two or more
    words and each one-word key the night says as a name, none of them stop words alone, and the relation its first
    line gives (`<someone>'s mom`): one said as a name (mom, dad, grandma) bare and in its long form, any other
    (`sister`, `second-oldest brother`) after my, his, her, our or a name's `'s`."""
    keys = [m for x in text.splitlines() if (m := re.match(r"- ([^:\n]{2,60}): (.*)", x))]
    said = [title(card, text), *(k[1].strip() for k in keys[:1]), *(k[1].strip() for k in keys if mentions.key_of(k[1]) and (
        len(k[1].split()) > 1 or k[1].strip().lower() in said_as))]
    if keys and len(w := keys[0][1].split()) > 1 and all(x[:1].isupper() for x in w):
        said.append(w[0])
    pats = [rf"\b{re.escape(n)}\b" for n in said if content(n)]
    if keys and (rel := re.search(r"\b\w+'s ([a-z]+(?:[ -][a-z]+){0,2})$", re.split(r"[,;.(]| and ", keys[0][2])[0].strip())):
        part = re.split(r"[ -]", rel[1])
        pats += [rf"\b{part[0]}\b", rf"\b{KIN[part[0]]}\b"] if part[0] in KIN and len(part) == 1 else [
            rf"\b(?:my|his|her|our|\w+'s) {'[ -]'.join(map(re.escape, part))}\b"]
    return re.compile("|".join(pats), re.I)


def resolve(s: memory.Settings, private: Path, logs: list[Path], path: str) -> Path | None:
    """The file a cite names, as cards write it (a root's name, the private layer's parent, a root's folder), kept to the
    memory scope and the [curate] logs."""
    p = Path(path.strip())
    for c in (s.root / p, private.parent / p, *(r.parent / p for r in s.scope.roots)):
        if c.is_file() and (s.scope.contains(f := c.resolve()) or f in logs):
            return f
    return None


def cites(src: str) -> list[tuple[str, int, int, re.Match[str]]]:
    """(file, from, to, match) per cite in a line's `(src: ...)`; `:12` is the file before it."""
    out, last = [], ""
    for m in CITE.finditer(src):
        last = m[1].strip() or last
        out.append((last, int(m[2]), int(m[3] or m[2]), m))
    return out


def sweep(s: memory.Settings, private: Path, logs: list[Path], backup: Path, dry: bool, today: date) -> tuple[list[str], list[str]]:
    """(what changed, what it found) on every card, each card rewritten under the writers' lock."""
    changed, found, text = [], [], dict[Path, list[str]]()
    for card in sorted((s.root / "facts").glob("*.md")):
        seen: set[str] = set()

        def gone(x: bytes) -> bytes | None:
            t = x.decode(errors="replace").strip()
            if not t.startswith("- "):
                return None
            old = t in seen or bool(GONE.search(t)) or any(d < today.isoformat() for d in UNTIL.findall(t))
            seen.add(t)
            return b"\n" if old else None
        moves: dict[str, str] = {}
        for i, x in enumerate(card.read_text(errors="replace").splitlines(), 1):
            if not (m := LINE.match(x)) or GONE.search(x):
                continue
            cs = []                                      # (cite, its file's lines); one list per file, so `is` tells files apart
            for path, lo, hi, c in cites(m[3]):
                if (f := resolve(s, private, logs, path)) and f not in text:
                    text[f] = [flat(y) for y in f.read_text(errors="replace").splitlines()]
                cs.append((path, lo, hi, c, text[f] if f and f != card.resolve() else []))     # never its own card
                if not (rows := cs[-1][4]) or hi > len(rows) or not all(rows[j - 1].strip() for j in (lo, hi)):
                    found.append(f"{card.name}:{i} cites {path}:{lo}, " + ("its own card" if f and not rows else "not in the memory "
                                 "scope" if not rows else "a blank or a line past its end"))
            mv = {}
            quotes = [flat(q) for q in QUOTE.findall(m[2])]
            held = {k for k, (_, lo, hi, _, rows) in enumerate(cs) if any(q in r for q in quotes for r in rows[lo - 1:hi])}
            for q in quotes:                             # a quote on none of its cited lines: on one unmarked line of one cited
                if any(q in r for _, lo, hi, _, rows in cs for r in rows[lo - 1:hi]):      # file, that file cited once and its
                    continue                                                               # cite holding no other quote, moves it
                at = [(k, j) for k, y in enumerate(cs) for j, (r, ok) in enumerate(zip(y[4], kept(y[4])), 1) if ok and q in r]
                if len(at) == 1 and at[0][0] not in held and cs[at[0][0]][1] == cs[at[0][0]][2] and sum(
                        y[4] is cs[at[0][0]][4] for y in cs) == 1:
                    mv[at[0][0]] = at[0][1]
                else:
                    found.append(f"{card.name}:{i} quotes \"{q[:40]}\", on {len(at)} lines of the files it cites")
            new = x
            for k in sorted(mv, reverse=True):          # from the right, so each cite's place in the line still holds
                a = m.start(3) + cs[k][3].start(2)
                new = new[:a] + str(mv[k]) + new[a + len(cs[k][3][2]):]
            moves.update({x: new} if new != x else {})
        with memory.daily_lock(s.daily):                 # the writers' lock: memory.remember writes a card under it too
            blank = doctor.rewrite(card, backup, gone, dry)
            moved = doctor.rewrite(card, backup, lambda x: (moves[t] + "\n").encode() if (t := x.decode(errors="replace").rstrip("\n")) in moves else None, dry)
        for what, n in (("blanked", blank), ("moved the cites of", moved)):
            changed += [f"{card.name}: {what} line{'s' if len(n) > 1 else ''} {', '.join(map(str, n[:5]))}"] if n else []
    return changed, found


def sources(s: memory.Settings, private: Path, logs: list[Path], day: date) -> list[tuple[Path, list[tuple[int, str]]]]:
    """A day's kept lines with their numbers: each date-named file in the memory scope whole, the install's day log
    first and the `also` roots in their order, but a copy whose cite names another file first (a snapshot of a root),
    then the lines of each [curate] log that start with the day."""
    name, out = f"{day.isoformat()}.md", []
    files = {p for f in s.scope.files() if (p := Path(f).resolve()).name == name and resolve(s, private, logs, memory.cite(s, p)) == p}
    for p in sorted(files, key=lambda p: (next((i for i, r in enumerate(s.scope.roots) if p.is_relative_to(r)), 99), str(p))) + logs:
        rows = p.read_text(errors="replace").splitlines() if p.is_file() else []
        out.append((p, [(i, x) for i, (x, k) in enumerate(zip(rows, kept(rows)), 1) if k and x.strip()
                        and (p not in logs or x.startswith(day.isoformat()))]))
    return out


def reader(card: Path) -> memory.Reader | None:
    """Who a card is read by: the owner (None) for a private card, else the room its class names."""
    cls, rid = memory.file_class(card)
    return None if cls == "private" else memory.Reader(rid=rid, owner=False)


def packs(s: memory.Settings, private: Path, logs: list[Path], days: list[date]) -> tuple[dict[Path, list[tuple[Path, int, str]]], set[str]]:
    """Per card, every kept source line naming it that the card does not cite yet, newest day first, each once, as the
    card's reader may read it, to PACK_CHARS a day; and the words the night says as names."""
    out, src = {}, [(d, p, rows) for d in reversed(days) for p, rows in sources(s, private, logs, d)]
    said_as = proper(src)
    for card in sorted((s.root / "facts").glob("*.md")):
        text, who = card.read_text(errors="replace"), reader(card)
        pat, pack, said, size = names(card, text, said_as), list[tuple[Path, int, str]](), set(), dict[date, int]()
        had = {(f, j) for m in map(LINE.match, text.splitlines()) if m for path, lo, hi, _ in cites(m[3])
               if (f := resolve(s, private, logs, path)) for j in range(lo, hi + 1)}
        for d, p, rows in src:
            seen = memory.allowed_text(who, p, p.read_text(errors="replace"), whole=True, daily=s.daily) if who else None
            if who and seen is None:
                continue
            vis = seen.split("\n") if seen is not None else []
            for i, x in rows:
                if pat.search(x) and (p, i) not in had and x not in said and (seen is None or vis[i - 1] == x) and (
                        size.get(d, 0) + len(x[:600]) + 40 <= PACK_CHARS):
                    pack.append((p, i, x[:600]))
                    said.add(x)
                    size[d] = size.get(d, 0) + len(x[:600]) + 40
        out.update({card: pack} if pack else {})
    return out, said_as


def shown(s: memory.Settings, pack: list[tuple[Path, int, str]]) -> str:
    return "\n".join(f"[{memory.cite(s, p) if s.scope.contains(p) else p}:{i}] {x}" for p, i, x in pack)


def check(s: memory.Settings, private: Path, logs: list[Path], card: Path, line: str, pack: list[tuple[Path, int, str]],
          heads: set[str]) -> str:
    """Why a proposed line may not land, or "": its shape, a head of one common word, a secret, a repeat, and any cite
    that is not a line of the card's pack (so read as its reader may read it, unmarked, from the night's sources)."""
    m = LINE.fullmatch(line)
    if not m or len(line) > mentions.ENTRY_CHARS or "\u2014" in line:
        return "not a card line of at most 300 characters, no em dash, ending in its (src: ...)"
    if len(content(m[1])) < 2 and m[1].strip().lower() not in heads:      # a card's name, a key it has, a name the night says
        return f"its head {m[1]!r} is one common word, a key that would pull the card into any message"
    if SECRET.search(m[2]):
        return "it holds what looks like a secret"
    if any(f": {m[2]} (src:" in x for x in card.read_text(errors="replace").splitlines()):
        return "the card has it"
    lines = {(p, i) for p, i, _ in pack}
    for path, lo, hi, _ in cites(m[3]):
        if not (f := resolve(s, private, logs, path)) or any((f, j) not in lines for j in range(lo, hi + 1)):
            return f"its cite {path}:{lo} is not a line of this card's pack"
    return ""


def busy(state: Path) -> set[str]:
    """Accounts with a Yerk worker running (the account rule: one heavy worker on an account at a time)."""
    try:
        with sqlite3.connect(f"file:{state / 'ledger.sqlite'}?mode=ro", uri=True) as c:
            return {r[0] for r in c.execute("SELECT account FROM workers WHERE finished IS NULL AND started IS NOT NULL")}
    except sqlite3.Error:
        return set()


def ask(private: Path, state: Path, conf: dict[str, Any], prompt: str, tools: str) -> str:
    """One headless Claude Code turn with `tools` (none: the prompt is all it reads) on `[curate] account`, else the
    first account in order, when no worker holds it, else the next free one; its last message."""
    rows, held = load_accounts(private / "accounts.toml"), busy(state)
    a = next((x for x in [r for r in rows if r.name == conf.get("account")] + rows if x.name not in held), None)
    if a is None:
        raise RuntimeError("every account has a worker running")
    argv = [claude_bin(), "-p", "--output-format", "text", "--model", str(conf.get("model") or "opus"), "--setting-sources", "",
            "--strict-mcp-config", "--tools", tools, "--disallowedTools", "Agent", "--permission-mode", "bypassPermissions"]
    r = subprocess.run(argv, input=prompt, capture_output=True, text=True, env=account_env(a), cwd=state,
                       timeout=float(conf.get("cap_minutes", 45)) * 60)
    if r.returncode:
        raise RuntimeError(f"the worker on account {a.name} ended {r.returncode}: {words.no_paths(r.stderr)[-200:]}")
    return r.stdout


def prompt(part: str, cards: dict[Path, str], body: str) -> str:
    """curate.md's `part` section, the cards as they stand, then `body`."""
    text = PROMPTS.read_text().split(f"# {part}\n", 1)[1].split("\n# ", 1)[0]
    return text + "".join(f"\n\n=== {c.name} (as it stands)\n{t}" for c, t in cards.items()) + "\n\n" + body


def batches(s: memory.Settings, pk: dict[Path, list[tuple[Path, int, str]]]) -> list[list[Path]]:
    """The cards in worker calls of at most CALL_CHARS, one reader to a call, so no worker sees another room's pack."""
    out, size = list[list[Path]](), 0
    for c in sorted(pk, key=lambda c: (memory.file_class(c), c.name)):
        n = len(c.read_text(errors="replace")) + len(shown(s, pk[c]))
        if not out or memory.file_class(out[-1][0]) != memory.file_class(c) or size + n > CALL_CHARS:
            out, size = [*out, []], 0
        out[-1].append(c)
        size += n
    return out


def taken(s: memory.Settings, cards: list[Path]) -> bool:
    """yerk-index holds each card as it is on disk now (a busy day log elsewhere in the scope does not matter)."""
    try:
        conn = memindex.connect(s.index_db, readonly=True)
        try:
            return all(conn.execute("SELECT size, mtime_ns FROM files WHERE path = ?", (str(c),)).fetchone() == (
                (st := c.stat()).st_size, st.st_mtime_ns) for c in cards)
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return False


def bench(s: memory.Settings) -> dict[str, Any] | None:
    """The recall bench's two numbers, stamped with when it ran and how long it took; None with no bench to run."""
    t = time.time()
    r = memory.bench(s) if s.bench.is_file() else {}
    return {"top_k": r["top_k"], "top_1": r["top_1"], "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
            "seconds": round(time.time() - t, 1)} if r.get("ok") else None


def promote(s: memory.Settings, private: Path, state: Path, conf: dict[str, Any], days: list[date], backup: Path, dry: bool,
            model: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """The proposals, what the knock-down and the checks left, and what landed, each `card: line`, a drop with its why."""
    logs = [(private / Path(str(x)).expanduser()).resolve() for x in conf.get("logs", [])]
    model = model or (lambda p, tools: ask(private, state, conf, p, tools))
    out: dict[str, Any] = {k: [] for k in ("proposed", "dropped", "checked", "landed", "corrections")} | {"bench": None}
    (pk, said_as), land = packs(s, private, logs, days), list[tuple[Path, str]]()
    heads = said_as | {n.lower() for c in (s.root / "facts").glob("*.md") for n in (c.stem.replace("-", " "), title(c, c.read_text(errors="replace")))}
    for batch in batches(s, pk):
        cards = {c: c.read_text(errors="replace") for c in batch}
        body = "".join(f"\n=== pack {c.name}\n{shown(s, pk[c])}\n" for c in batch)
        said = model(prompt("Propose", cards, body), "")
        by = {c.name: c for c in batch}
        lines = list(dict.fromkeys((m[1], m[2]) for m in re.finditer(r"^(\S+?\.md): (- .+)$", said, re.M) if m[1] in by))
        out["corrections"] += [f"{m[1]}:{m[2]} {m[3]}" for m in FIX.finditer(said) if m[1] in by]
        roots = "".join(f"\n{r.name}: {r}" for r in s.scope.roots) + "".join(f"\n{p.name}: {p}" for p in logs)
        passed = model(prompt("Knock down", cards, body + f"\n=== roots{roots}\n\n=== proposed\n" + "\n".join(
            f"{c}: {x}" for c, x in lines)), "Read,Grep,Glob") if lines else ""
        keep = {(m[1], m[2]) for m in re.finditer(r"^KEEP (\S+?\.md): (- .+)$", passed, re.M)}
        why = {(m[1], m[2]): m[3] for m in re.finditer(r"^DROP (\S+?\.md): (- .+) \| (.+)$", passed, re.M)}
        for c, x in lines:
            out["proposed"].append(f"{c}: {x}")
            if (c, x) not in keep:
                out["dropped"].append(f"{c}: {x} | {why.get((c, x), 'the knock-down did not pass it')}")
            elif no := check(s, private, logs, by[c], x, pk[by[c]], heads | {k[1].strip().lower() for k in (
                    re.match(r"- ([^:\n]{2,60}):", y) for y in cards[by[c]].splitlines()) if k}):
                out["checked"].append(f"{c}: {x} | {no}")
            else:
                land.append((by[c], x))
    if dry or not land:
        return out
    out["bench"] = [bench(s)]
    if out["bench"][0] is None:
        out["checked"].append("nothing landed: no bench to hold the lines to")
        return out
    ours = {x for _, x in land}
    for c in dict.fromkeys(c for c, _ in land):
        add = "".join(f"{x}\n" for k, x in land if k == c).encode()
        with memory.daily_lock(s.daily):
            doctor.rewrite(c, backup, lambda x: None if x.endswith(b"\n") else x + (b"\n" if x else b"") + add, False)
    for _ in range(int(conf.get("index_wait", 600))):    # yerk-index takes the new lines in before the bench reads them
        if taken(s, list(dict.fromkeys(c for c, _ in land))):
            out["bench"].append(bench(s))
            break
        time.sleep(1)
    before, after = out["bench"][0], out["bench"][-1] if len(out["bench"]) > 1 else None
    if after is None or after["top_k"] < before["top_k"] or after["top_1"] < before["top_1"]:
        for c in dict.fromkeys(c for c, _ in land):      # blanked, never cut: a line memory.remember added since keeps its number
            with memory.daily_lock(s.daily):
                doctor.rewrite(c, backup, lambda x: b"\n" if x.decode(errors="replace").rstrip("\n") in ours else None, False)
        out["checked"].append("nothing landed: " + ("the index did not take the lines in" if after is None else
                              f"the bench fell from {before['top_k']}, {before['top_1']} to {after['top_k']}, {after['top_1']}"))
    else:
        out["landed"] = [f"{c.name}: {x}" for c, x in land]
    return out


def run(private: Path, state: Path, since: str = "", dry: bool = False, now: float | None = None,
        model: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """The sweep, then the promotion of yesterday, or of each day from `since` to yesterday; kept unless `dry`."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    s, c = memory.settings(private, state, dict(conf.get("memory", {}))), dict(conf.get("curate", {}))
    today = datetime.fromtimestamp(now, s.tz).date()
    first = date.fromisoformat(since) if since else today - timedelta(days=1)
    days = [first + timedelta(days=i) for i in range((today - first).days)]
    backup = private / "backups" / f"curate-{today.isoformat()}"
    logs = [(private / Path(str(x)).expanduser()).resolve() for x in c.get("logs", [])]
    changed, found = sweep(s, private, logs, backup, dry, today)
    out = {"at": now, "days": [d.isoformat() for d in days], "changed": changed, "found": found,
           **promote(s, private, state, c, days, backup, dry, model)}
    if not dry:
        write_private(state / LAST, json.dumps(out))
    return out


def main(private: Path, state: Path, since: str = "", dry: bool = False) -> int:
    """Every change, finding, landed line, correction and drop, a line each, and the bench with when it ran; exits 1
    when there is anything to read, 2 when it could not run."""
    try:
        r = run(private, state, since, dry)
    except Exception as e:                               # yerkd.toml unreadable, a worker that failed: the morning says it
        print(f"curate failed: {type(e).__name__}: {words.no_paths(str(e))[:160]}")
        return 2
    for k in ("changed", "found", "landed", "corrections", "checked", "dropped") + (("proposed",) if dry else ()):
        print("".join(f"{k}: {x}\n" for x in r[k]), end="")
    print("".join(f"bench: top {b['top_k']}, first {b['top_1']}, at {b['at']} in {b['seconds']} s\n" for b in r["bench"] or [] if b), end="")
    return 1 if r["found"] or r["corrections"] or r["checked"] else 0


def line(state: Path, now: float) -> tuple[str, str]:
    """The morning diff's one curation line: what landed and was swept, else the count to read and the first."""
    try:
        d = json.loads((state / LAST).read_text())
    except (OSError, ValueError):
        d = {}
    if now - float(d.get("at", 0)) > doctor.DAY:
        return "unknown curate", words.line(f"{words.NOT_DONE} " + words.UNKNOWN.format(what="curate", why=words.NO_RUN))
    read = [*d.get("found", []), *d.get("corrections", []), *d.get("checked", [])]
    if not read:
        return "", words.line(f"{words.PLAIN} " + words.CURATE_OK.format(n=len(d.get("landed", [])), m=len(d.get("changed", []))))
    return f"curate {read[0]}", words.line(f"{words.NOT_DONE} " + words.CURATE.format(n=len(read), first=read[0]))
