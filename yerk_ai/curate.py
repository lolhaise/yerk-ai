"""`yerk curate`: each night it tidies the fact cards, memory files of standing facts about one subject each, and adds new facts
to them from the day's notes; `--since` runs the same over a range of days. units/yerk-curate.timer runs it after the doctor's
(Layer 11, claims 6 and 15, decision 42). Two parts; no model rewrites a card alone. The sweep is code: on each card in the
tree's facts/, a line said twice, a superseded line and a line `until` a date now past become blank (cites into a card hold), and
a cite whose quoted words left its line moves only when they sit on exactly one line of its file; every other drift is a finding,
never a guess. The promotion: a pack per card from each day's sources as the card's reader may read them; a worker that sees the
pack alone proposes lines, a second knocks them down (curate.md), code checks shape, head, secrets and that each cite is a line
of the pack, and the lines land, appended with a backup, only when the bench does not fall. A correction to a line on file is
the morning diff. Each run is kept in <state>/curate.json. Then the rule files (`--rules`): merges, cuts, condensations
and promotions proposed, numbered, into <memory>/rules-proposals/<day>.md; a rule file changes only by the owner's
`--apply DAY N[,N...]`, backed up first and fitted to its line in budgets.toml, its lines blanked rather than cut."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from contextlib import nullcontext, suppress
from datetime import date, datetime, timedelta
from itertools import combinations, groupby
from pathlib import Path
from typing import Any, Callable

from . import context, doctor, memindex, memory, mentions, needs, shape, taste, words
from .accounts import account_env, load_accounts
from .config import budgets, load_toml, rooms, write_private
from .engine import claude_bin

LAST, RUN, PROMPTS = "curate.json", "curate-run.json", Path(__file__).with_name("curate.md")   # RUN: this run, its lines on trial
PACK_CHARS, CALL_CHARS = 20_000, 150_000            # a card's pack; one worker call's cards and packs together
LINE = re.compile(r"- ([^:\n]{2,60}): (.+) \(src: ([^()]+)\)")                  # a card line: `- Key: fact (src: file:line, :line)`
CITE = re.compile(r"(?:^|,)\s*([^,:]*):(\d+)(?:-(\d+))?\s*(?=,|$)")             # file:line or file:from-to; no file: the one before
MARK = re.compile(r"⛔|do not state", re.I)                                     # accuracy marks: a heading's covers what it holds
GONE = re.compile(r"(?i:\[superseded)|SUPERSEDED|(?i:^\W*superseded\b)")         # Yerk's own mark, the word in capitals after anything
STRUCK = re.compile(r"(?i:\[superseded)|^- [^\w\n]*SUPERSEDED\b|^- [^:\n]{2,60}: [^\w\n]*(?i:superseded)\b")   # a card line the
# sweep blanks: Yerk's mark, or the word opening its key in capitals or its fact; never "NOT SUPERSEDED" or a line naming an old value
QUOTE = re.compile(r"[\"“]([^\"”]{20,})[\"”]")                                   # long enough to name one line
FLAT = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


def flat(text: str) -> str:
    """Curly quotes and apostrophes as straight ones, on both sides of a quote's match."""
    return text.translate(FLAT)
UNTIL = re.compile(r"\[moved (\to{4}-\with{2}-\queue{2})\]")                            # the sample item moved to the next step and the
SAYS_UNTIL = re.compile(r"\buntil (\d{4}-\d{2}-\d{2})\b")                        # in its words: an expiry or a history, so a finding
SECRET = re.compile(r"passw(?:or)?d|passcode|one-time code|\b(?:sk|rk)-[\w-]{16,}|\bgh[pousr]_\w{20,}|\bxox[abp]-|\bAKIA[0-9A-Z]{16}|"
                    r"X-Amz-|[?&](?:sig|token|key)=|\b\d(?:[ -]?\d){11,18}\b|\b[A-Za-z0-9+/_-]{40,}\b", re.I)
KIN = {"mom": "mother", "dad": "father", "grandma": "grandmother", "grandpa": "grandfather"}   # a relation also said as a name
FIX = re.compile(r"^(\S+?\.md) fix (\d+): (- .+)$", re.M)                       # a proposed correction: card, its line, the new line
CODES = re.compile(r"\b(?:pin|(?<!area )code|key)\W{0,3}\d{3,}|(?<!\d[ -])\b\d{3}[ -]\d{3}[ -]\d{3}\b(?![ -]?\d)|\b\d{4}[ -]\d{3}[ -]\d{3}\b|"
                   r"\b(?:account|acct|transit|routing)\b\W{0,12}\d{5,}|\bwi-?fi\b[^.;]*\bkey\b", re.I)   # a code, a SIN or health card, a bank number


def kept(rows: list[str]) -> list[bool]:
    """Which lines a pack may hold: none marked, none under a marked heading or bullet (to the next of its level or
    higher), none superseded or under a superseded heading."""
    out, up = [], [(0, False)]                           # the headings and bullets open above, by depth, and their mark
    for x in rows:
        d = len(h[1]) if (h := re.match(r"(#{1,6}) ", x)) else 9 + len(x) - len(x.lstrip()) if x.strip() else 99
        up = [u for u in up if u[0] < d]
        up.append((d, bool(MARK.search(x)) or bool(h and GONE.search(x)) or up[-1][1]))
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
    """The file a cite names, as cards write it (a root's name, the private layer's parent, a root's folder, a root's own
    cite name as memory.read takes it), kept to the memory scope and the [curate] logs."""
    p = Path(path.strip())
    for c in (s.root / p, private.parent / p, *(r.parent / p for r in s.scope.roots),
              *(r / p.relative_to(n) for r, n in s.cites if "/" in n and p.is_relative_to(n))):
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
    changed, found, text, facts = [], [], dict[Path, list[str]](), (s.root / "facts").resolve()
    for card in sorted((s.root / "facts").glob("*.md")):
        seen: set[str] = set()

        def gone(x: bytes) -> bytes | None:
            t = x.decode(errors="replace").strip()
            if not t.startswith("- "):
                return None
            old = t in seen or bool(STRUCK.search(t)) or any(d < today.isoformat() for d in UNTIL.findall(t))
            seen.add(t)
            return b"\n" if old else None
        moves: dict[str, str] = {}
        for i, x in enumerate(card.read_text(errors="replace").splitlines(), 1):
            if not UNTIL.search(x) and any(d < today.isoformat() for d in SAYS_UNTIL.findall(x)):
                found.append(f"{card.name}:{i} says until a date now past, in its words: an expiry or a history")
            if not (m := LINE.match(x)) or STRUCK.search(x):
                continue
            cs = []                                      # (cite, its file's lines); one list per file, so `is` tells files apart
            for path, lo, hi, c in cites(m[3]):
                if (f := resolve(s, private, logs, path)) and f not in text:
                    text[f] = [flat(y) for y in f.read_text(errors="replace").splitlines()]
                card_of_tree = f is not None and f.parent == facts       # never a source: a name another root's folder shares
                cs.append((path, lo, hi, c, text[f] if f and not card_of_tree else []))
                if not (rows := cs[-1][4]) or hi > len(rows) or not all(rows[j - 1].strip() for j in (lo, hi)):
                    found.append(f"{card.name}:{i} cites {path}:{lo}, " + ("its own card" if f == card.resolve() else "another card "
                                 "of this tree" if card_of_tree else "not in the memory scope" if f is None else "a blank or a line "
                                 "past its end"))
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
            changed += [f"{card.name}: {what} line{'s' if len(n) > 1 else ''} {', '.join(map(str, n))}"] if n else []
    return changed, found


def sources(s: memory.Settings, private: Path, logs: list[Path], day: date) -> list[tuple[Path, list[tuple[int, str]]]]:
    """A day's kept lines with their numbers: each date-named file in the memory scope whole, the install's day log
    first and the `also` roots in their order, but a copy (a snapshot of a root: its path under its root also sits under
    an earlier root of the same folder name, read from the roots since each root's cite name is its own), then the lines
    of each [curate] log that start with the day."""
    name, out, rs = f"{day.isoformat()}.md", [], s.scope.roots
    at = {p: next((i for i, r in enumerate(rs) if p.is_relative_to(r)), len(rs)) for f in s.scope.files()
          if (p := Path(f).resolve()).name == name}
    files = [p for p, i in at.items() if i < len(rs) and not any(
        r.name == rs[i].name and (r / p.relative_to(rs[i])).is_file() for r in rs[:i])]
    for p in sorted(files, key=lambda p: (at[p], str(p))) + logs:
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
            seen = memory.allowed_text(who, p, p.read_text(errors="replace"), whole=True, daily=s.daily) if who and p not in logs else None
            if who and seen is None:                         # a [curate] log is the owner's own stream, whatever its class says
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
    if SECRET.search(m[2]) or CODES.search(f"{m[1]}: {m[2]}"):
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
    """One headless Claude Code turn with `tools` (none: the prompt is all it reads) on `[curate] account` alone when it
    names one (the owner knows what else runs there; the ledger sees only Yerk's workers), else the first account in
    order that no worker holds; its last message."""
    rows, held = load_accounts(private / "accounts.toml"), busy(state)
    if (pin := conf.get("account")) and pin not in {r.name for r in rows}:
        raise RuntimeError(f"[curate] account {pin} is not in accounts.toml")
    a = next((x for x in [r for r in rows if r.name == pin] or rows if x.name not in held), None)
    if a is None:
        raise RuntimeError(f"account {pin} has a worker running" if pin else "every account has a worker running")
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
    """The recall bench's two numbers, stamped with when it ran and how long it took; None with no bench to run, or a
    blind one (a control missed, so its numbers hold nothing)."""
    t = time.time()
    r = memory.bench(s) if s.bench.is_file() else {}
    return {"top_k": r["top_k"], "top_1": r["top_1"], "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
            "seconds": round(time.time() - t, 1)} if r.get("ok") and not r.get("blind") else None


def undo(s: memory.Settings, state: Path, backup: Path) -> list[str]:
    """The lines on trial of a run that never reached its bench's verdict (killed, or failed mid-landing), blanked, never
    cut, so a line memory.remember filed since keeps its number; refused while that run is still alive."""
    try:
        d = json.loads((state / RUN).read_text())
    except (OSError, ValueError):
        return []
    pid = int(d.get("pid") or 0)
    with suppress(OSError):                              # another process's run, still alive: never touch its lines
        if 0 < pid != os.getpid():
            os.kill(pid, 0)
            raise RuntimeError(f"curate is already running, since {words.clock(float(d.get('at', 0)))}")
    rows = [(s.root / "facts" / Path(str(c)).name, str(x)) for c, x in d.get("lines", [])]
    for c in dict.fromkeys(c for c, _ in rows):
        ours = {x for k, x in rows if k == c}
        with memory.daily_lock(s.daily):
            doctor.rewrite(c, backup, lambda x: b"\n" if x.decode(errors="replace").rstrip("\n") in ours else None, False)
    put = restore(d.get("files", []))                    # an apply cut off mid-write: each file it wrote, back from its copy
    (state / RUN).unlink()
    return ([f"nothing landed: the run of {words.clock(float(d.get('at', 0)))} never reached its bench, {len(rows)} lines blanked"] if rows
            else []) + [f"nothing applied: the apply of {words.clock(float(d.get('at', 0)))} was cut off; {x}" for x in put]


def promote(s: memory.Settings, private: Path, state: Path, conf: dict[str, Any], days: list[date], backup: Path, dry: bool,
            model: Callable[[str, str], str] | None = None, at: float = 0.0) -> dict[str, Any]:
    """The proposals, what the knock-down and the checks left, and what landed, each `card: line`, a drop with its why."""
    logs = [(private / Path(str(x)).expanduser()).resolve() for x in conf.get("logs", [])]
    model = model or (lambda p, tools: ask(private, state, conf, p, tools))
    out: dict[str, Any] = {k: [] for k in ("proposed", "dropped", "checked", "landed", "corrections")} | {"bench": None}
    (pk, said_as), land = packs(s, private, logs, days), list[tuple[Path, str]]()
    heads = said_as | {n.lower() for c in (s.root / "facts").glob("*.md") for n in (c.stem.replace("-", " "), title(c, c.read_text(errors="replace")))}
    per, most = (int(conf.get(k, n)) * max(1, len(days)) for k, n in (("per_card", 3), ("per_night", 25)))   # a day's cap: no card doubles unread
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
            elif len(land) >= most or sum(k == by[c] for k, _ in land) >= per:
                out["checked"].append(f"{c}: {x} | held: over the night's cap, {per} a card and {most} in all")
            else:
                land.append((by[c], x))
    if dry or not land:
        return out
    out["bench"] = [bench(s)]
    if out["bench"][0] is None:
        out["checked"].append("nothing landed: no bench to hold the lines to")
        return out
    write_private(state / RUN, json.dumps({"pid": os.getpid(), "at": at, "lines": [[c.name, x] for c, x in land]}))   # on trial
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
        undo(s, state, backup)                           # blanked, never cut: a line memory.remember added since keeps its number
        out["checked"].append("nothing landed: " + ("the index did not take the lines in" if after is None else
                              f"the bench fell from {before['top_k']}, {before['top_1']} to {after['top_k']}, {after['top_1']}"))
    else:
        (state / RUN).unlink()
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
    undone = [] if dry else undo(s, state, backup)       # a run cut off mid-landing: its lines blanked before anything else
    if not dry:
        write_private(state / RUN, json.dumps({"pid": os.getpid(), "at": now, "lines": []}))
    changed, found = sweep(s, private, logs, backup, dry, today)
    out = {"at": now, "days": [d.isoformat() for d in days], "changed": changed, "found": found,
           **promote(s, private, state, c, days, backup, dry, model, now)}
    out["checked"] = undone + out["checked"]
    try:                                                 # then the rule files: proposed, never applied (PR curate-rules)
        out["rules"] = rules(private, state, s, dry, now)["card"]
    except (Exception, SystemExit) as e:                 # rooms.toml or budgets.toml unreadable: the morning says it
        out["rules"], out["checked"] = [], out["checked"] + [f"rules: not proposed, {type(e).__name__}: {words.no_paths(str(e))[:80]}"]
    if not dry:
        write_private(state / LAST, json.dumps(out))
        (state / RUN).unlink(missing_ok=True)
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
    print("".join(f"rules: {x}\n" for x in r.get("rules", [])), end="")
    return 1 if r["found"] or r["corrections"] or r["checked"] else 0


def line(state: Path, now: float) -> tuple[str, str]:
    """The morning diff's one curation line: a run still on or cut off, what landed and was swept, else the count to
    read and the first."""
    with suppress(OSError, ValueError, KeyError, TypeError):
        r = json.loads((state / RUN).read_text())
        at = words.clock(float(r["at"]), now)
        with suppress(OSError):
            os.kill(int(r["pid"]), 0)
            return "", words.line(f"{words.WORKING} " + words.CURATE_RUNNING.format(at=at))
        return "curate cut off", words.line(f"{words.NOT_DONE} " + words.CURATE_CUT.format(at=at, n=len(r.get("lines", []))))
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


# The rule files (PR curate-rules; decisions 30 and 42): the line-per-rule parts every room loads, read whole, and merges,
# cuts, condensations and promotions PROPOSED; only the owner's `--apply` writes one, and a line is blanked, never cut.
PROPOSED, PROPOSALS, NEAR, LONG, CARD_MOST, WEEK = "curate-rules.json", "rules-proposals", 0.75, 600, 10, 7 * 86400
TRAILER = re.compile(r" \(([^()]*\d{4}-\d{2}-\d{2} \d{2}:\d{2}[^()]*)\)( \[m:[^\]\s]+\])?$")   # a filing's room, time, message
ASIDE = re.compile(r" ?\((?:[^()\"]{0,60}: )?\"[^\"]{8,}\"[^();]*\)")        # a quote in parentheses, no clause after it: an example
REASON = re.compile(r"\b(?:because|since|so that)\b", re.I)                    # a reason, which a condensation never takes out
DAYCITE, DATED = re.compile(r"\bdaily/(\d{4}-\d{2}-\d{2})\.md:(\d+)"), re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
NO = re.compile(r"\b(?:never|not|no longer|instead of|rather than|stop|don'?t)\b", re.I)    # a newer line that says otherwise
CLAUSE = re.compile(r"(?<=[.;])\s+")
HEDGE = re.compile(r"(?:,\s*|\s+|^)(?:basically|needless to say|so to speak|as it were)\b,?|,\s*of course\b,?|(?:^|(?<=[.;]))\s*of course,", re.I)  # set off
SPAN, HELD, EXM, QM = re.compile(r"\"[^\"]*\"|“[^”]*”"), re.compile("[\x00\x01]\\d+[\x00\x01]"), "\x00", "\x01"   # a quote; held aside


def rule_files(private: Path, s: memory.Settings) -> dict[Path, tuple[str, int]]:
    """Each line-per-rule file a room loads, once (the rules file, each rulebook), its kind and its line in budgets.toml;
    then OVERFLOW, in no room, with no line."""
    caps, out = budgets()["session_start"]["appended"], dict[Path, tuple[str, int]]()
    for room in rooms(private).values():
        for k, p in shape.files(private, room, s.root / memory.RULES):
            if k in context.TAIL:
                out.setdefault(p.resolve(), (k, int(caps[k])))
    if (o := s.root / memory.OVERFLOW).is_file():
        out[o.resolve()] = ("overflow", 0)
    return out


def part(kind: str, path: Path, text: str) -> int:
    """A rule file as a session start counts it (context.session_start): the rules file's live lines, any other whole."""
    return context.chars(memory.rules_part(path, text) if kind == "rules" else context.cited(path, text.strip()))


def split(x: str) -> tuple[str, str, str]:
    """A rule line as its `job: ` ("" on a hand-kept bullet, a colon in an example none), its words, and its filing's trailer, kept whole."""
    t, m = memory.topic(memory.EXAMPLE.sub("", x)), TRAILER.search(x)
    said = (x[:m.start()] if m else x)[2:]
    head = f"{t}: " if said.startswith(f"{t}: ") else ""
    return head, said[len(head):], m[0] if m else ""


def short(x: str) -> str:
    """A rule's name on the card and in its evidence: its job, or a hand-kept bullet's first six words."""
    return t if len(t := memory.topic(x)) <= 60 else " ".join(t.split()[:6])


def bare(said: str) -> str:
    return " ".join(memory.EXAMPLE.sub("", said).split())


def near(a: str, b: str) -> bool:
    """Near-duplicate words: the same in nearly the same order, curly quotes read straight."""
    m = difflib.SequenceMatcher(None, flat(a).lower(), flat(b).lower(), autojunk=False)
    return m.real_quick_ratio() >= NEAR and m.quick_ratio() >= NEAR and m.ratio() >= NEAR


def ids(x: str) -> set[str]:
    """The messages a line was filed from (a seeded line's `seed` is none)."""
    return {i for m in re.findall(r"\[m:([^\]\s]+)\]", x) for i in m.split(",") if i.isdigit()}


def overlap(a: str, b: str) -> bool:
    """Two rules' jobs share at least half the words of the shorter one, stop words out (one message often files two jobs)."""
    x, y = set(content(memory.topic(a))), set(content(memory.topic(b)))
    return bool(x & y) and 2 * len(x & y) >= min(len(x), len(y))


def trailer(*xs: str) -> str:
    """The trailers of `xs` as one: every room and time, every message, each once."""
    ms = [m for x in xs if (m := TRAILER.search(x))]
    said = dict.fromkeys(p.strip() for m in ms for p in m[1].split(";"))
    msgs = dict.fromkeys(i for m in ms if m[2] for i in m[2].strip()[3:-1].split(","))
    return (f" ({'; '.join(said)})" if said else "") + (f" [m:{','.join(msgs)}]" if msgs else "")


def merged(a: str, b: str) -> str:
    """One line for two: a's words, then each clause of b's that a does not say yet, the fuller example of the two, and both
    trailers; the rules' own words, never a paraphrase."""
    (ha, sa, _), (hb, sb, _) = split(a), split(b)
    have = [c for c in CLAUSE.split(bare(sa)) if c]
    body = " ".join(have + [c for c in CLAUSE.split(bare(sb)) if c and not any(near(c, h) for h in have)])
    ex = max((e.strip() for e in memory.EXAMPLE.findall(sa) + memory.EXAMPLE.findall(sb)), key=len, default="")
    head = ha if hb.lower() in ("", ha.lower()) else f"{ha[:-2]} and {hb}" if ha else hb
    return f"- {head}{body}{' ' + ex if ex else ''}{trailer(a, b)}"


def hold(pattern: re.Pattern[str], text: str, mark: str) -> tuple[str, list[str]]:
    """`text` with each match of `pattern` held aside as `mark`N`mark`, and the matches in order."""
    held: list[str] = []

    def put(m: re.Match[str]) -> str:
        held.append(m[0])
        return f"{mark}{len(held) - 1}{mark}"
    return pattern.sub(put, text), held


def back(text: str, held: list[str], mark: str, keep: bool = True) -> str:
    """What `hold` held aside, put back in its place, or left out."""
    return re.sub(f"{mark}(\\d+){mark}", lambda m: held[int(m[1])] if keep else "", text)


def said_in(c: str, h: str) -> bool:
    """Three or more words, clause `c` word for word one or more whole comma-parts of clause `h` in a row: `c` says nothing `h`
    does not (a reason, a no, a name) and drops no word that narrows it ("never call before nine" is not "... on weekends")."""
    want, *parts = (re.findall(r"[\w']+", flat(HELD.sub("", t)).lower()) for t in (c, *h.split(",")))
    return len(want) > 2 and any(want == sum(parts[i:j], []) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1))


def prose(said: str) -> str:
    """A rule's words with its prose cut, its quotes and what is held aside kept as they are: a clause said again (whole comma-parts
    of an earlier clause, word for word; one holding a quote never), its examples left where it stood; then a hedge set off."""
    text, quotes = hold(SPAN, said, QM)
    have: list[str] = []
    for c in CLAUSE.split(text):
        if QM not in c and any(said_in(c, h) for h in have):
            c = "".join(re.findall(f"{EXM}\\d+{EXM}", c))
        have.append(c)
    return back(HEDGE.sub("", " ".join(have)), quotes, QM)


def stages(x: str) -> tuple[str, str, str]:
    """A long line cut in order, only its own words and never a reason, its trailer whole: its prose, then its quotes in
    parentheses, then its examples."""
    head, said, trail = split(x)
    text, ex = hold(memory.EXAMPLE, said, EXM)
    a = prose(text)
    b = ASIDE.sub(lambda m: m[0] if REASON.search(m[0]) else "", a)

    def whole(t: str, keep: bool) -> str:
        return f"- {head}{' '.join(back(t, ex, EXM, keep).split())}{trail}"
    return whole(a, True), whole(b, True), whole(b, False)


def condensed(x: str, examples: bool = False) -> str:
    """A long line with its prose and its quotes in parentheses out, and its examples too only with `examples` (its file past
    its line even without the rest): the worked examples are a rule's strongest words (2026-08-28)."""
    return stages(x)[2 if examples else 1]


def why_condensed(x: str, y: str, size: int, cap: int, label: str) -> str:
    """A condensation's why: what it takes out, then what it keeps, and the file's part with every other cut against its line."""
    a, b, _ = stages(x)
    n = len(memory.EXAMPLE.findall(split(x)[1]))
    ex = f"its {n} example{'s' * (n != 1)}"
    took = [w for w, cut in (("its prose said twice or hedged", a.split() != x.split()), ("its quoted asides", b != a), (ex, y != b)) if cut]
    return (f"{len(x):,} characters, past {LONG}: {' and '.join(took) or 'its spacing'} out, {len(y):,} left" + (
        f", since {label} is past its line even without the rest ({size:,} of {cap:,})" if y != b else
        f"; kept: {ex}, since {label} has " + (f"room ({size:,} of {cap:,} with every other cut)" if cap else "no line") if n else "")
        + ", every other word its own")


def lost(s: memory.Settings, x: str) -> str:
    """The first day-log line `x` cites that is no longer there (past its file's end, blank, or its file gone), else ""."""
    for d, n in DAYCITE.findall(x):
        f = s.daily / f"{d}.md"
        rows = f.read_text(errors="replace").splitlines() if f.is_file() else []
        if int(n) > len(rows) or not rows[int(n) - 1].strip():
            return f"daily/{d}.md:{n}"
    return ""


def when(x: str) -> str:
    """The newest time in a line's trailer, "" when it has none."""
    return max(DATED.findall(m[1]), default="") if (m := TRAILER.search(x)) else ""


def edited(rows: dict[str, list[str]], edits: list[list[Any]], s: memory.Settings) -> None:
    """`edits` ([file, line, its text now, the new text]; line 0 appends, a new text of None marks the line superseded by
    the line appended before it) made on `rows`, each file read on first use; a line that no longer holds its text refuses."""
    last = ""
    for f, i, was, new in edits:
        r = rows.setdefault(f, Path(f).read_text(errors="replace").split("\n"))
        if not i:
            r.insert(len(r) - (r[-1] == ""), new)
            last = f"{memory.cite(s, Path(f))}:{len(r) - (r[-1] == '')}"
        elif i > len(r) or r[i - 1] != was:
            raise RuntimeError(f"{Path(f).name}:{i} changed since it was proposed")
        else:
            r[i - 1] = memory.mark(was, last) if new is None else new


def week(private: Path, state: Path, s: memory.Settings, now: float) -> tuple[list[dict[str, Any]], list[tuple[str, int, dict[str, Any]]],
                                                                             list[tuple[str, str]]]:
    """The week's evidence: the ledger's shadow rows and each owner room's rated replies (taste.jsonl), newest first, and the
    day logs' `yerk` lines (a rule's example taken out, a rule held), each with its cite."""
    since, tastes, said = now - WEEK, list[tuple[str, int, dict[str, Any]]](), list[tuple[str, str]]()
    try:
        with sqlite3.connect(f"file:{state / 'ledger.sqlite'}?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            shadows = [dict(r) for r in c.execute("SELECT id, t, room, turn, rule, detail FROM shadow WHERE t >= ? ORDER BY t DESC", (since,))]
    except sqlite3.Error:
        shadows = []
    cut, kept_ = datetime.fromtimestamp(since, s.tz).strftime("%Y-%m-%dT%H:%M:%S"), [taste.kept(private, c) for c in rooms(private).values()]
    for p in dict.fromkeys(p for p in kept_ if p is not None):
        for i, x in reversed(list(enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1))):
            with suppress(ValueError):
                if isinstance(t := json.loads(x), dict) and str(t.get("ts", "")) >= cut:
                    tastes.append((shape.label(private, s, "taste", p), i, t))
    for d in sorted({datetime.fromtimestamp(since + k * 86400, s.tz).date() for k in range(8)}, reverse=True):
        if (f := s.daily / f"{d.isoformat()}.md").is_file():
            said += [(f"{memory.cite(s, f)}:{i}" + (f" [m:{ln.mid}]" if ln.mid else ""), ln.body) for i, x in
                     enumerate(f.read_text(errors="replace").splitlines(), 1) if (ln := memory.parse_line(x)) and ln.role == "yerk"]
    return shadows, tastes, said


def evidence(x: str, shadows: list[dict[str, Any]], tastes: list[tuple[str, int, dict[str, Any]]], said: list[tuple[str, str]]) -> tuple[int, str]:
    """(rows, one line): the week's shadow bounces of a check this rule names, its rated replies that hold two of its job's
    words, its `yerk` lines that quote its job, and the first three of them with their cites."""
    low, job = flat(x).lower(), short(x)
    keys = set(content(job))
    hits = [f"shadow #{r['id']} {r['rule']}, {r['room']} turn {r['turn']}: {words.line(str(r['detail'] or ''), 50)}"
            for r in shadows if re.search(rf"\b{re.escape(str(r['rule']))}s?\b", low)]
    tasted = [f"{f}:{i} {t.get('verdict')} \"{words.line(str(t.get('signal') or ''), 40)}\" on {t.get('reply_id')}" for f, i, t in tastes
              if keys and len(keys & set(mentions.tokens(f"{t.get('signal') or ''} {str(t.get('reply') or '')[:600]}"))) >= min(2, len(keys))]
    yerk = [c for c, body in said if f'"{job.lower()}"' in body.lower()]
    n = len(hits) + len(tasted) + len(yerk)
    return n, f"{n} in 7 days (shadow {len(hits)}, taste {len(tasted)}, yerk {len(yerk)})" + "".join(f"; {h}" for h in (hits + tasted + yerk)[:3])


def propose(private: Path, state: Path, s: memory.Settings, now: float) -> tuple[list[dict[str, Any]], list[list[Any]], dict[str, str]]:
    """The proposals, numbered, the most characters saved first and then the most evidence (a promotion last, with what it
    needs); per rule file its name, kind, line, its part now and with every proposal applied; and each file's name."""
    files, ev = rule_files(private, s), week(private, state, s, now)
    rows = {str(p): p.read_text(errors="replace").split("\n") for p in files}
    name = {str(p): shape.label(private, s, k, p) for p, (k, _) in files.items()}
    out: list[dict[str, Any]] = []

    def size(r: dict[str, list[str]], f: str) -> int:
        return part(files[Path(f)][0], Path(f), "\n".join(r[f]))

    def add(kind: str, label: str, edits: list[list[Any]], why: str) -> None:
        after = {f: list(r) for f, r in rows.items()}
        edited(after, edits, s)
        n, line = evidence(" ".join(str(e[2] or e[3]) for e in edits), *ev)
        out.append({"kind": kind, "label": label, "edits": edits, "why": why, "rows": n, "evidence": line, "saved": sum(
            size(rows, f) - size(after, f) for f in dict.fromkeys(e[0] for e in edits) if files[Path(f)][1])})   # OVERFLOW: in no room
    for p, (kind, _) in files.items():
        if kind == "overflow":
            continue
        f, used = str(p), set[int]()
        live = [(i, x) for i, x in enumerate(rows[f], 1) if x.startswith("- ") and mentions.OLD not in x]
        for i, x in enumerate(rows[f], 1):           # cuts: a superseded line the session start still sends, a lost cite
            if x.startswith("- ") and GONE.search(x) and part(kind, p, "\n".join(rows[f][:i - 1] + [""] + rows[f][i:])) < size(rows, f):
                add("cut", short(x), [[f, i, x, ""]], "it is marked superseded, and every session start still sends it")
                used.add(i)
            elif x.startswith("- ") and (gone := lost(s, x)):
                add("cut", short(x), [[f, i, x, ""]], f"its cite {gone} is a day-log line that is no longer there")
                used.add(i)
        for (i, a), (j, b) in combinations(live, 2):  # merges: one job, the same words, or one message on overlapping jobs; a newer
            if {i, j} & used:                         # line that says no. One message alone is never a merge: it files two jobs often
                continue
            same, one, dup = memory.topic(a).lower() == memory.topic(b).lower(), ids(a) & ids(b), near(bare(split(a)[1]), bare(split(b)[1]))
            if not (same or dup or one and overlap(a, b)):
                continue
            used |= {i, j}
            (k, old), (m, new) = ((i, a), (j, b)) if when(a) <= when(b) else ((j, b), (i, a))
            if same and not dup and when(old) and when(new) > when(old) and NO.search(split(new)[1]):
                add("cut", short(old), [[f, k, old, ""]], f"a newer rule on the same job says otherwise: {name[f]}:{m} \"{new}\" "
                    f"against {name[f]}:{k} \"{old}\"")
            else:
                add("merge", f"{short(a)} + {short(b)}", [[f, i, a, merged(a, b)], [f, j, b, ""]], "the same job" if same
                    else "near-duplicate words" if dup else f"filed from the same message ({', '.join(sorted(one))}) on overlapping jobs")
        long = [(i, x) for i, x in live if i not in used and len(x) > LONG]    # condensations: a line past LONG, its prose out
        t = {k: list(v) for k, v in rows.items()}                             # first, then its quoted asides; its examples last,
        edited(t, [e for q in out if q["edits"][0][0] == f for e in q["edits"]] + [[f, i, x, condensed(x)] for i, x in long], s)
        over = (n := size(t, f)) > (cap := files[p][1]) > 0                    # and only with the file past its line without them
        for i, x in long:
            if len(y := condensed(x, over)) < len(x):
                add("condense", short(x), [[f, i, x, y]], why_condensed(x, y, n, cap, name[f]))
    out.sort(key=lambda q: (-q["saved"], -q["rows"]))
    for n, q in enumerate(out, 1):
        q["n"] = n
    held = next((p for p, (k, _) in files.items() if k == "overflow"), None)
    if (r := next((p for p, (k, _) in files.items() if k == "rules"), None)) and held:   # promotions: held, and now it fits
        f, cap, mine = str(r), files[r][1], [q for q in out if all(e[0] == str(r) for e in q["edits"])]
        for j, h in enumerate(rows[str(held)], 1):
            if not h.startswith("- ") or mentions.OLD in h:
                continue
            marks = [[f, k, x, None] for k, x in enumerate(rows[f], 1) if x.startswith(f"- {memory.topic(h)}: ") and mentions.OLD not in x]
            for y, need in [(y, mine[:k]) for y in dict.fromkeys((h, memory.EXAMPLE.sub("", h))) for k in range(len(mine) + 1)]:
                t = {k: list(v) for k, v in rows.items()}       # as held, with the fewest of the rest in; then examples out
                edited(t, [e for q in need for e in q["edits"]] + [[f, 0, "", y], *marks], s)
                if size(t, f) <= cap - memory.HEADROOM:
                    add("promote", short(h), [[f, 0, "", y], *marks, [str(held), j, h, ""]], f"held in {memory.OVERFLOW}; it fits "
                        f"{memory.RULES} under its line" + (" with its examples out" if y != h else "") +
                        (f" once {', '.join(str(q['n']) for q in need)} are applied" if need else ""))
                    out[-1] |= {"n": len(out), "needs": [q["n"] for q in need]}
                    break
    every = {f: list(v) for f, v in rows.items()}
    for q in out:
        with suppress(RuntimeError):
            edited(every, q["edits"], s)
    return out, [[name[str(p)], k, cap, size(rows, str(p)), size(every, str(p))] for p, (k, cap) in files.items()], name


def page(day: str, props: list[dict[str, Any]], sizes: list[list[Any]], name: dict[str, str]) -> str:
    """The proposals file: each rule file's part now and with every proposal applied, then a numbered block a proposal."""
    def at(f: str, i: int) -> str:
        return f"{name.get(f, Path(f).name)}:{i}" if i else f"{name.get(f, Path(f).name)}, appended"
    head = (f"---\nclass: private\n---\n# Rule proposals, {day}\n\nProposed by `yerk curate --rules`, never applied: `yerk curate --apply "
            f"{day} N[,N...]` writes the numbers named (backed up first, refused past a file's line), `yerk curate --apply {day} undo` "
            "puts the last apply back.\n\n" + "".join(f"- {n}: {a:,} characters now, {b:,} with every proposal applied"
                                                      + (f", its line {c:,}" if c else ", in no room") + "\n" for n, _, c, a, b in sizes))
    return head + "".join(
        f"\n## {q['n']}. {q['kind']}: {q['label']} ({-q['saved']:+,} chars)\n\nBEFORE\n"
        + "".join(f"{at(f, i)}: {was}\n" for f, i, was, _ in q["edits"] if i)
        + "\nAFTER\n" + "".join(f"{at(f, i)}: " + ("(blank)" if new == "" else "(marked superseded by the line appended)" if new is None else new)
                                + "\n" for f, i, _, new in q["edits"])
        + f"\nWHY: {q['why']}\n\nEVIDENCE: {q['evidence']}\n" for q in props)


def card(day: str, props: list[dict[str, Any]]) -> list[str]:
    """The machine room's card: a head, a line for each of the CARD_MOST that save the most (then with the most evidence),
    the rest only in the file, and how to apply; nothing at all with no proposal."""
    if not props:
        return []
    out = [words.line(f"{words.WORKING} " + words.RULES_CARD.format(n=len(props), chars=f"{sum(max(0, q['saved']) for q in props):,}"))]
    for q in sorted(props, key=lambda q: (-q["saved"], -q["rows"]))[:CARD_MOST]:
        head, tail = f"{q['n']}. {q['kind']}: ", f" ({-q['saved']:+,} chars)"
        out.append(head + words.line(q["label"], max(8, words.PHONE - len(head) - len(tail))) + tail)
    return out + [words.line(words.RULES_MORE.format(n=len(props) - CARD_MOST))] * (len(props) > CARD_MOST) + [
        words.line(words.RULES_APPLY.format(day=day))]


def spans(ns: list[int]) -> str:
    """Numbers as their runs: 1-3, 5, 8, 9."""
    runs = [[n for _, n in g] for _, g in groupby(enumerate(sorted(ns)), lambda x: x[1] - x[0])]
    return ", ".join(f"{r[0]}-{r[-1]}" if len(r) > 2 else ", ".join(map(str, r)) for r in runs)


def rules_lines(state: Path, now: float) -> list[tuple[str, str]]:
    """The morning diff's rule proposals of the last day (the nightly `yerk curate --rules` job, or curate's own phase): a head,
    each kind's numbers and how to apply them, so the owner names them to `--apply`; one line at none, nothing past a day."""
    try:
        d = dict(json.loads((state / PROPOSED).read_text()))
    except (OSError, ValueError, TypeError):
        return []
    if now - float(d.get("at", 0)) > doctor.DAY:
        return []
    props, day, by = list(d.get("proposals", [])), str(d.get("date", "")), dict[str, list[int]]()
    if not props:
        return [("", words.line(f"{words.PLAIN} " + words.RULES_NONE.format(day=day)))]
    for q in sorted(props, key=lambda q: int(q["n"])):
        by.setdefault(str(q["kind"]), []).append(int(q["n"]))
    head = words.RULES_CARD.format(n=len(props), chars=f"{sum(max(0, int(q['saved'])) for q in props):,}")
    return [("", words.line(f"{words.WORKING} {head}")), *[("", words.line(words.RULES_KIND.format(kind=k, numbers=spans(ns))))
                                                          for k, ns in by.items()], ("", words.line(words.RULES_APPLY.format(day=day)))]


def say(private: Path, state: Path, day: str, lines: list[str], post: Callable[[dict[str, Any]], dict[str, Any]] | None) -> bool:
    """The card to the machine room through the running yerkd, as deploy's and the morning's go; False when it did not go."""
    from . import jobs
    if not lines or not (machine := [n for n, c in rooms(private).items() if c.get("role") == "machine"]):
        return False
    try:
        out = (post or (lambda p: jobs.post_control(state, p, 60)))(
            {"machine": {"room": machine[0], "text": "\n".join(lines), "key": f"curate-rules-{day}"}})
    except (OSError, ValueError):
        return False
    return bool(out.get("ok"))


def rules(private: Path, state: Path, s: memory.Settings, dry: bool, now: float,
          post: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """The rules phase: the proposals, kept in <memory>/rules-proposals/<day>.md (0600) and <state>/curate-rules.json unless
    `dry` (the applies on file kept for undo), and the card said in the machine room at most once a day, never with none."""
    day = datetime.fromtimestamp(now, s.tz).date().isoformat()
    props, sizes, name = propose(private, state, s, now)
    with nullcontext() if dry else memory.daily_lock(s.daily):    # the needs' lock: a need filed now keeps its number and block
        needs.around(props, needs.numbers(state, day))           # the day's needs keep theirs; the proposals take the rest
        out: dict[str, Any] = {"day": day, "proposals": props, "sizes": sizes, "card": card(day, props), "file": ""}
        if props and not dry:
            (folder := s.root / PROPOSALS).mkdir(mode=0o700, parents=True, exist_ok=True)
            out["file"] = str(write_private(folder / f"{day}.md", page(day, props, sizes, name) + needs.part(state, day)))
    if dry:
        return out
    try:
        old = dict(json.loads((state / PROPOSED).read_text()))
    except (OSError, ValueError, TypeError):
        old = {}
    said = str(old.get("said", ""))
    if out["card"] and said != day and say(private, state, day, out["card"], post):
        said = day
    write_private(state / PROPOSED, json.dumps({"date": day, "at": now, "proposals": props, "said": said, "applied": old.get("applied", [])}))
    return out


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore(files: list[list[str]]) -> list[str]:
    """Each [file, its sha256 before an apply, after it, the backup folder] put back byte-identical from its copy there when
    it still holds what the apply wrote; one that changed since (a rule filed meanwhile) is said and kept as it is."""
    out = []
    for f, was, wrote, folder in files:
        p = Path(f)
        if (cur := sha(p.read_bytes()) if p.is_file() else "") == was:
            continue                                     # never written, or put back already
        copy = next((c for c in sorted(Path(folder).glob(f"{p.parent.name}.{p.name}*")) if sha(c.read_bytes()) == was), None)
        if cur != wrote or copy is None:
            out.append(f"{p.name}: " + ("changed since the apply, kept as it is" if cur != wrote else "no backup copy, kept as it is"))
            continue
        write_private(p, copy.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        out.append(f"{p.name}: put back as it was")
    return out


def put(path: Path, backup: Path, new: bytes) -> None:
    """`path` rewritten to `new` by doctor.rewrite (its copy into `backup` first, its mode kept); `new` only ever adds lines at
    the end, so each line keeps its number."""
    old = len(path.read_bytes().split(b"\n"))
    parts = [x + b"\n" for x in new.split(b"\n")]
    parts[-1] = parts[-1][:-1]
    at = iter(range(old))
    doctor.rewrite(path, backup, lambda x: (lambda i: parts[i] if i < old - 1 else b"".join(parts[i:]))(next(at)), False)


def apply(private: Path, state: Path, day: str, which: str, now: float | None = None) -> list[str]:
    """The owner's `--apply DAY N[,N...]`: those proposals of DAY written, each file they touch fitted to its line first (refused
    whole when one would pass it, or a line changed since), backed up by doctor.rewrite and on trial in RUN while it writes,
    so the next curate puts a cut-off one back; `undo`, the last apply put back byte-identical. RuntimeError says why not."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    s = memory.settings(private, state, dict(conf.get("memory", {})))
    backup = private / "backups" / f"curate-{datetime.fromtimestamp(now, s.tz).date().isoformat()}"
    try:
        rec = dict(json.loads((state / PROPOSED).read_text()))
    except (OSError, ValueError, TypeError):
        rec = {}
    if rec.get("date") != day:
        raise RuntimeError(f"no proposals of {day} on file" + (f"; the newest are of {rec['date']}" if rec.get("date") else ""))
    done = undo(s, state, backup)                        # a run or an apply cut off: put back first; refused while one runs
    if which == "undo":
        if not rec.get("applied"):
            raise RuntimeError("no apply to undo")
        done += restore(rec["applied"].pop())
        write_private(state / PROPOSED, json.dumps(rec))
        return done
    props, want = {int(q["n"]): q for q in rec.get("proposals", [])}, [int(n) for n in which.split(",") if n.strip()]
    if not want or (bad := [n for n in want if n not in props]):
        raise RuntimeError(f"no proposal {bad[0] if want and bad else which} of {day}")
    files, rows = rule_files(private, s), dict[str, list[str]]()
    with memory.daily_lock(s.daily):                     # the writers' lock: memory.rule files under it too
        edited(rows, [e for n in want for e in props[n]["edits"]], s)
        for f, r in rows.items():
            if (kind_cap := files.get(Path(f))) is None:
                raise RuntimeError(f"{Path(f).name} is no rule file a room loads now: nothing applied")
            if kind_cap[1] and (n := part(kind_cap[0], Path(f), "\n".join(r))) > kind_cap[1]:
                raise RuntimeError(f"{shape.label(private, s, kind_cap[0], Path(f))} would be {n:,} characters, past its line of "
                                   f"{kind_cap[1]:,}: nothing applied")
        olds, news = {f: Path(f).read_bytes() for f in rows}, {f: "\n".join(r).encode() for f, r in rows.items()}
        trial = [[f, sha(olds[f]), sha(news[f]), str(backup)] for f in rows]
        write_private(state / RUN, json.dumps({"pid": os.getpid(), "at": now, "lines": [], "files": trial}))    # on trial
        try:
            for f in rows:
                put(Path(f), backup, news[f])
                if not any(c.read_bytes() == olds[f] for c in backup.glob(f"{Path(f).parent.name}.{Path(f).name}*")):
                    raise RuntimeError(f"{Path(f).name} was written without its backup copy, so it is put back: nothing applied")
        except Exception:
            for f in rows:
                write_private(Path(f), olds[f], stat.S_IMODE(Path(f).stat().st_mode))
            (state / RUN).unlink(missing_ok=True)
            raise
        write_private(state / PROPOSED, json.dumps({**rec, "applied": [*rec.get("applied", []), trial]}))
        (state / RUN).unlink()
    return done + [f"{n}. {props[n]['kind']}: {props[n]['label']}, applied" for n in want] + [
        f"{shape.label(private, s, files[Path(f)][0], Path(f))}: {part(files[Path(f)][0], Path(f), news[f].decode()):,} characters"
        + (f" of {files[Path(f)][1]:,}" if files[Path(f)][1] else "") for f in rows]


def rules_main(private: Path, state: Path, dry: bool = False) -> int:
    """`yerk curate --rules`: the rules phase alone, a line a proposal; exits 1 with any to read, 2 when it could not run."""
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        r = rules(private, state, memory.settings(private, state, dict(conf.get("memory", {}))), dry, time.time())
    except (Exception, SystemExit) as e:
        print(f"curate failed: {type(e).__name__}: {words.no_paths(str(e))[:160]}")
        return 2
    print("".join(f"{q['n']}. {q['kind']}: {q['label']} ({-q['saved']:+,} chars, evidence {q['rows']}): {q['why']}\n" for q in r["proposals"]), end="")
    print("".join(f"{n}: {a:,} now, {b:,} with every proposal" + (f", line {c:,}" if c else "") + "\n" for n, _, c, a, b in r["sizes"]), end="")
    print("dry run: nothing written" if dry else f"proposals: {r['file']}" if r["file"] else "no proposals")
    return 1 if r["proposals"] else 0


def apply_main(private: Path, state: Path, day: str, which: str) -> int:
    """`yerk curate --apply DAY N[,N...]` or `--apply DAY undo`: what it wrote, a line each; exits 2 refused."""
    try:
        print("\n".join(apply(private, state, day, which)))
    except (OSError, ValueError, RuntimeError) as e:
        print(f"curate --apply refused: {words.no_paths(str(e))[:200]}")
        return 2
    return 0
