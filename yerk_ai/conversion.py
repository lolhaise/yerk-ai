"""`yerk curate --lessons`: rewrites the rules files as lesson files, each a one-line summary, then its reason and examples.
(lessons.py), as prompting-fable-5-2 says of rules that built up: "rewrite absolute rules so that they state their scope and
reason. ... keep a copy of the originals." The dry run writes one proposals file, <memory>/rules-proposals/lessons-<day>.md,
proposals file, <memory>/rules-proposals/lessons-<day>.md, and changes nothing else: for each original rule by its cite, its words,
filing's own, memory.unshaped, on the pages of every model a room runs), and each thing done to it as a listed item; the JSON
block at its end is what an apply reads. Meaning never changes: a rule already in the shape is split as it stands (its first
sentence the summary); one out of it goes to curate's model pass ("Lessons", then "Knock down lessons" in curate.md); code alone
cites a quote the day log holds, writes the owner's go-ahead into an absolute and checks that every sentence of the original
lands in a lesson or is listed. The frozen trigger test refuses a split that loses an owner message of the last 14 days that
loads its rule today. Apply takes that exact file and refuses unless every lesson passes the gate, every original is accounted
for, the trigger test passes and the store is byte for byte the one the dry run read; it copies the originals to
<memory>/rules-backup-<day>/ first, and undo puts them back."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import curate, lessons, memory, shape
from .config import budgets, load_toml, models, rooms, write_private
from .mentions import OLD

FILE = "lessons-{day}.md"          # in <memory>/rules-proposals/
APPLIED = "applied.json"           # in the backup folder: what an apply wrote, each file's sha256, for its undo
DAYS = 14                          # the trigger test's window of owner messages
OWNER = ("owner", "test-webhook")  # whose messages the trigger test reads (the webhook stands in for the owner)
PEOPLE = re.compile(r"\b(?:anyone|anybody|someone|somebody|people|a person|other persons?|others|strangers?|whoever)\b", re.I)
GO_AHEAD = "Only the owner's go-ahead lifts it."
HIS_WORD = "Only the owner's own go-ahead lifts it, not another person's."


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def flat(text: str) -> str:
    return " ".join(shape.flat(text).lower().split())


def originals(s: memory.Settings) -> list[dict[str, Any]]:
    """Each live rule of RULES and TOPIC_RULES, by its cite: its line, job, words (trailer and marks out), rooms, triggers, filing
    and class (the file's: rules.md shared, the topic rules private unless their front matter says)."""
    out = []
    for name in (memory.RULES, memory.TOPIC_RULES):
        p = s.root / name
        rows = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []
        for n, job, said in shape.lessons_of(p):
            raw, tm = rows[n - 1], memory.TRAILER.search(rows[n - 1])
            out.append({"cite": f"{memory.cite(s, p)}:{n}", "file": name, "line": n, "text": raw, "job": job.strip().lower(),
                        "said": said.strip(), "rooms": sorted(memory.scope(raw) or ()), "on": memory.picked(raw),
                        "filed": f"{tm[1]}{tm[2] or ''}" if tm else "", "cls": memory.file_class(p)[0]})
    return out


def ends(s: memory.Settings) -> dict[str, str]:
    """Each line of the store, live or replaced, by cite -> the cite of the live rule its replacements end at (by its mark, else
    LATER), lessons.HOPS at most and a cite met twice ending it; a line that ends nowhere live (emptied, a note) is none."""
    rows = {f"{memory.cite(s, s.root / n)}:{i}": x for n in (memory.RULES, memory.TOPIC_RULES) if (s.root / n).is_file()
            for i, x in enumerate((s.root / n).read_text(encoding="utf-8", errors="replace").splitlines(), 1) if x.startswith("- ")}
    kept, out = memory.later(s), dict[str, str]()
    for c in rows:
        at, been = c, set[str]()
        while at in rows and at not in been and len(been) < lessons.HOPS:
            been.add(at)
            if OLD not in rows[at]:
                out[c] = at
                break
            at = (memory.BY.findall(rows[at]) or [kept.get(at, "")])[0]
    return out


def owner_messages(s: memory.Settings, now: float, days: int = DAYS) -> list[tuple[str, str]]:
    """(cite, words) of each owner message of the last `days` day logs, the trigger test's frozen set."""
    first = datetime.fromtimestamp(now - days * 86400, s.tz).date().isoformat()
    out = []
    for p in sorted(s.daily.glob("*.md")) if s.daily.is_dir() else []:
        if memory.DAYLOG.search(p.name) and p.stem >= first:
            for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if (ln := memory.parse_line(raw)) and ln.role in OWNER:
                    out.append((f"daily/{p.name}:{i}", ln.body))
    return out


def quotes(text: str, daily: Path, seen: dict[Path, str], most: int) -> tuple[str, list[dict[str, str]]]:
    """A lesson's words with each quote of more than `most` words that a day-log line holds, found by exact text search in
    daily/, replaced by that line's cite; each replaced or kept quote a listed item."""
    done = []
    for m in list(shape.QUOTED.finditer(text)):
        if len(m[1].split()) > most:
            at = shape.on_record(m[1], daily, seen)
            done.append({"kind": "quote cited", "said": m[1], "at": at} if at else {"kind": "quote kept", "said": m[1],
                                                                                     "why": "no day-log line holds it"})
            text = text.replace(m[0], f"the words at {at}", 1) if at else text
    return text, done


def absolute(job: str, text: str, read: Any) -> tuple[str, list[dict[str, str]]]:
    """An absolute with no scope or no exception written in: its scope from its own job and the owner's go-ahead as its exception
    (true of every rule he gave; one that guards him against other people says only his word lifts it), each a listed item."""
    miss = shape.lesson(job, text, None, {}, read=read).get("absolute")
    if not miss:
        return text, []
    done = []
    if "no scope" in miss[0]:
        said = f'It holds for the job "{job}".'
        text, done = f"{text} {said}", [{"kind": "scope added", "said": said, "from": "its job"}]
    if "no exception" in miss[0]:
        said = HIS_WORD if PEOPLE.search(text) else GO_AHEAD
        text, done = f"{text} {said}", [*done, {"kind": "exception added", "said": said, "from": "the owner's own go-ahead: every rule here is his"
                                                + ("; it guards him against other people" if said == HIS_WORD else "")}]
    return text, done


def accounted(rule: dict[str, Any], made: list[dict[str, Any]], listed: list[dict[str, str]]) -> list[str]:
    """The original's sentences that land in no lesson (word for word, a cited quote aside) and are not listed as left out or
    rewritten: none, when every sentence is accounted for."""
    have = flat(" ".join(f"{x['summary']} {x['body']}" for x in made))
    said = {flat(x["said"]) for x in listed if x["kind"] in ("left out", "rewritten")}
    out = []
    for sent in shape.SENTENCES.split(rule["said"]):
        bare = flat(shape.QUOTED.sub(" ", sent))
        if sent.strip() and flat(sent) not in have and flat(sent) not in said and not set(bare.split()) <= set(have.split()):
            out.append(sent.strip())
    return out


def build(s: memory.Settings, rule: dict[str, Any], prop: dict[str, Any] | None, read: Any, seen: dict[Path, str],
          taken: set[str], acronyms: frozenset[str] | None) -> dict[str, Any]:
    """The lessons made of one rule, each with its gate, and what was done to it."""
    most = shape.threshold(dict(budgets().get("shape") or {}), "lesson_quote_words", 8)
    parts = [(x.get("summary", ""), x.get("body", "")) for x in (prop or {}).get("lessons", [])] or [memory.split_summary(rule["said"])]
    done = [{"kind": k, **x} for k in ("left out", "rewritten") for x in (prop or {}).get(k.replace(" ", "_"), [])]
    if (why := (prop or {}).get("reason")) and why.get("said"):
        src = why.get("from", "")
        line = (s.daily / src.removeprefix("daily/").rpartition(":")[0]) if src.startswith("daily/") else None
        n = int(src.rpartition(":")[2]) if line and src.rpartition(":")[2].isdigit() else 0
        held = flat(rule["said"]) if not line else flat(line.read_text(errors="replace").splitlines()[n - 1]) if line.is_file() and n else ""
        done.append({"kind": "reason added", "said": why["said"], "from": src or "the rule's own words"} if flat(why["said"]) in held
                    else {"kind": "reason refused", "said": why["said"], "why": "neither the rule's own words nor the line it names hold it"})
    if (c := (prop or {}).get("caution")):
        done.append({"kind": "caution to review", "said": c, "why": "left in place: deleting one is the owner's side's call"})
    made = []
    for summary, body in parts:
        body, cited = quotes(body, s.daily, seen, most)
        summary, cited2 = quotes(summary, s.daily, seen, most)
        body2, added = absolute(rule["job"], f"{summary} {body}".strip(), read)
        body = body2[len(summary):].strip()
        slug = lessons.slug_of(rule["job"], taken)
        taken.add(slug)
        refused = memory.unshaped(rule["job"], f"{summary} {body}".strip(), acronyms, s, read)
        made.append({"slug": slug, "job": rule["job"], "summary": summary, "body": body, "cls": rule["cls"], "rooms": rule["rooms"],
                     "on": rule["on"], "filed": rule["filed"], "gate": (refused or {}).get("fix", [])})
        done += cited + cited2 + added
    if not any(x["kind"] == "reason added" for x in done) and "no reason" in shape.lesson(rule["job"], rule["said"], None, {}, read=read):
        done.append({"kind": "no reason on file", "why": "listed for Ben: a reason is never invented"})
    return {**{k: rule[k] for k in ("cite", "file", "line", "text", "job")}, "lessons": made, "done": done,
            "missing": accounted(rule, made, done)}


def lesson_of(x: dict[str, Any]) -> lessons.Lesson:
    return lessons.Lesson(x["slug"], x["job"], x["summary"], x["body"], x["cls"], tuple(x["rooms"]), tuple(x["on"]), x["filed"])


def trigger_test(rules: list[dict[str, Any]], made: list[dict[str, Any]], messages: list[tuple[str, str]]) -> dict[str, list[str]]:
    """The frozen trigger test: cite -> each owner message that loads the rule today (memory.on_topic among every live rule)
    and loads no lesson made of it (on_topic among every lesson made); none lost, no key."""
    old = [r["text"] for r in rules]
    new = {r["cite"]: [lesson_of(x).line for x in m["lessons"]] for r, m in zip(rules, made)}
    corpus = [x for v in new.values() for x in v]
    w_old, w_new, lost = memory.rarities(old), memory.rarities(corpus), dict[str, list[str]]()
    for at, said in messages:
        if not (hit := {x for _, x in memory.on_topic(said, old, old, w_old)}):
            continue
        now = {x for _, x in memory.on_topic(said, corpus, corpus, w_new)}
        for r in rules:
            if r["text"] in hit and not now & set(new[r["cite"]]):
                lost.setdefault(r["cite"], []).append(at)
    return lost


def model_pass(rules: list[dict[str, Any]], model: Callable[[str, str], str]) -> dict[str, dict[str, Any]]:
    """curate's propose and knock-down machinery on the rules out of the shape: curate.md's "Lessons" proposes each rule's lessons
    as one JSON line, "Knock down lessons" passes or knocks down each; cite -> a proposal that passed (a knocked one is none, and the
    rule is split as it stands)."""
    if not rules:
        return {}
    body = "\n".join(f"=== {r['cite']} (job: {r['job']})\n{r['said']}" for r in rules)
    props = {}
    for raw in model(curate.prompt("Lessons", {}, body), "").splitlines():
        try:
            if isinstance(x := json.loads(raw), dict) and x.get("cite") in {r["cite"] for r in rules}:
                props[x["cite"]] = x
        except ValueError:
            continue
    said = model(curate.prompt("Knock down lessons", {}, body + "\n\n=== proposed\n" + "\n".join(json.dumps(x) for x in props.values())),
                 "Read,Grep,Glob") if props else ""
    keep = {m[1] for m in re.finditer(r"^KEEP (\S+)$", said, re.M)}
    return {c: x for c, x in props.items() if c in keep}


def render(rec: dict[str, Any]) -> str:
    """The proposals file: each original by its cite, its lessons with their gates, and each thing done as a listed item, then
    the JSON block an apply reads; apply renders it again and refuses a file that differs."""
    out = [f"# Lessons proposed from the rule store, {rec['day']}", "",
           f"Store read: {', '.join(f'{k} {v[:12]}' for k, v in sorted(rec['store'].items()))}. Pages read: {', '.join(rec['read'])}.",
           f"Apply: `yerk curate --lessons-apply {rec['path']}`. Undo after it: `yerk curate --lessons-undo {rec['day']}`.",
           f"Ready to apply: {'yes' if rec['ready'] else 'no: ' + '; '.join(rec['why'])}.", ""]
    for r in rec["rules"]:
        out += [f"## {r['cite']}", "", f"Original: {r['text']}", ""]
        for x in r["lessons"]:
            out.append(f"- lesson {lessons.DIR}/{x['slug']}.md: {x['summary']}" + (f" | {x['body']}" if x["body"] else ""))
            out.append("  - gate: passes" if not x["gate"] else "  - gate: refuses: " + " / ".join(x["gate"]))
        for d in r["done"]:
            out.append(f"- {d['kind']}" + (f": \"{d['said']}\"" if d.get("said") else "") + (f" -> {d['at']}" if d.get("at") else "")
                       + (f" as \"{d['as']}\"" if d.get("as") else "") + (f", from {d['from']}" if d.get("from") else "")
                       + (f" ({d['why']})" if d.get("why") else ""))
        out += [f"- not accounted for: \"{x}\"" for x in r["missing"]]
        lost = rec["lost"].get(r["cite"], [])
        out += [f"- triggers: {r['loads']} owner message{'s' * (r['loads'] != 1)} of the last {DAYS} days load it; "
                + (f"lost by the split: {', '.join(lost)}" if lost else "each still loads a lesson made of it"), ""]
    return "\n".join(out) + "\n```json\n" + json.dumps(rec, sort_keys=True) + "\n```\n"


def dry_run(private: Path, state: Path, s: memory.Settings, now: float | None = None,
            model: Callable[[str, str], str] | None = None, read: Any = None) -> Path:
    """The dry run: the proposals file written, nothing else. Its path. `read`: the pages the gate reads, else those of every
    model a room runs (shape.pages), since every room loads the store."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    read = shape.pages(shape.run_by(rooms(private), state, models(conf))) if read is None else read
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    rules, seen, taken = originals(s), dict[Path, str](), set(lessons.retired(s.root))
    off = [r for r in rules if memory.unshaped(r["job"], r["said"], acronyms, s, read)]
    props = model_pass(off, model or (lambda p, tools: curate.ask(private, state, dict(conf.get("curate", {})), p, tools)))
    made = [build(s, r, props.get(r["cite"]), read, seen, taken, acronyms) for r in rules]
    msgs = owner_messages(s, now)
    lost = trigger_test(rules, made, msgs)
    w = memory.rarities([r["text"] for r in rules])
    for r, m in zip(rules, made):
        m["loads"] = sum(1 for _, said in msgs if memory.on_topic(said, [r["text"]], [x["text"] for x in rules], w))
    day = datetime.fromtimestamp(now, s.tz).date().isoformat()
    path = s.root / curate.PROPOSALS / FILE.format(day=day)
    why = [f"{n} lesson{'s' * (n != 1)} the gate refuses" for n in [sum(bool(x["gate"]) for m in made for x in m["lessons"])] if n]
    why += [f"{n} original{'s' * (n != 1)} not accounted for" for n in [sum(bool(m["missing"]) for m in made)] if n]
    why += [f"{n} original{'s' * (n != 1)} losing a trigger" for n in [len(lost)] if n]
    rec = {"day": day, "path": str(path), "store": {n: sha(s.root / n) for n in (memory.RULES, memory.TOPIC_RULES)}, "read": list(read),
           "ends": ends(s), "rules": made, "lost": lost, "ready": not why, "why": why}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, render(json.loads(json.dumps(rec, sort_keys=True))))      # as an apply will read it back
    return path


def loaded(path: Path) -> dict[str, Any]:
    """The proposals file's record, refused unless the file is exactly what the dry run wrote."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"\n```json\n(.+)\n```\n\Z", text, re.S)
    rec = json.loads(m[1]) if m else {}
    if not rec or render(rec) != text:
        raise RuntimeError(f"{path.name} is not the file a dry run wrote, word for word: run the dry run again")
    return dict(rec)


def apply(private: Path, state: Path, s: memory.Settings, path: Path) -> list[str]:
    """The conversion, from the exact proposals file: refused unless it is ready, every lesson still passes the gate and the
    store is byte for byte the one it read; the originals copied first, then each lesson, the index, the old cites' map and the
    snapshot news reads written, and rules-topics.md moved into the copy."""
    rec = loaded(path)
    if not rec["ready"]:
        raise RuntimeError(f"not ready: {'; '.join(rec['why'])}: nothing applied")
    if moved := [n for n, h in rec["store"].items() if sha(s.root / n) != h]:
        raise RuntimeError(f"{', '.join(moved)} changed since the dry run read it: run the dry run again; nothing applied")
    if lessons.converted(s.root):
        raise RuntimeError(f"the store is converted already ({lessons.DIR}/ is there): nothing applied")
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    if off := [x["slug"] for m in rec["rules"] for x in m["lessons"]
               if memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, rec["read"])]:
        raise RuntimeError(f"{off[0]} no longer passes the gate: run the dry run again; nothing applied")
    backup = s.root / f"{lessons.BACKUP}{rec['day']}"
    with memory.daily_lock(s.daily):
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        for n in (memory.RULES, memory.TOPIC_RULES, memory.LATER):
            if (s.root / n).is_file():
                shutil.copy2(s.root / n, backup / n)
        made = [(lesson_of(x), m) for m in rec["rules"] for x in m["lessons"]]
        first = {m["cite"]: lesson_of(m["lessons"][0]).slug for m in rec["rules"] if m["lessons"]}
        (s.root / lessons.DIR).mkdir(mode=0o700, exist_ok=True)
        for x, _ in made:
            write_private(s.root / lessons.DIR / f"{x.slug}.md", x.text())
        write_private(s.root / lessons.CITES, "".join(f"{a}\t{first[b]}\n" for a, b in rec["ends"].items() if b in first))
        lessons.keep(s.root, rec["day"], [(x, [m["text"]]) for x, m in made])
        write_private(s.root / memory.RULES, lessons.index_text([x for x, _ in made]))
        (s.root / memory.TOPIC_RULES).unlink(missing_ok=True)
        write_private(backup / APPLIED, json.dumps({"files": {f"{lessons.DIR}/{x.slug}.md": hashlib.sha256(x.text().encode()).hexdigest()
                                                              for x, _ in made}, "index": sha(s.root / memory.RULES)}))
    return [f"{len(made)} lessons from {len(rec['rules'])} rules written to {lessons.DIR}/, the index to {memory.RULES}",
            f"originals kept in {backup.name}/; undo: yerk curate --lessons-undo {rec['day']}"]


def undo(s: memory.Settings, day: str) -> list[str]:
    """The originals put back from rules-backup-<day>/ and the lessons, the old cites' map and the tombstones gone, refused when
    a lesson was filed or changed since the apply (re-file it after the undo, or retire it first)."""
    backup = s.root / f"{lessons.BACKUP}{day}"
    try:
        rec = json.loads((backup / APPLIED).read_text())
    except (OSError, ValueError):
        raise RuntimeError(f"no apply of {day} to undo: {backup.name}/{APPLIED} is not there") from None
    have = {f"{lessons.DIR}/{p.name}": sha(p) for p in (s.root / lessons.DIR).glob("*.md")}
    if changed := sorted(f for f, h in have.items() if rec["files"].get(f) != h):
        raise RuntimeError(f"{changed[0]} was filed or changed since the apply: retire it or keep its words, then undo; nothing put back")
    with memory.daily_lock(s.daily):
        for n in (memory.RULES, memory.TOPIC_RULES, memory.LATER):
            if (backup / n).is_file():
                write_private(s.root / n, (backup / n).read_bytes())
        for f in have:
            (s.root / f).unlink()
        (s.root / lessons.DIR).rmdir()
        for n in (lessons.CITES, lessons.RETIRED):
            (s.root / n).unlink(missing_ok=True)
        (backup / APPLIED).rename(backup / f"{APPLIED}.undone")
    return [f"{len(have)} lessons removed; {memory.RULES} and {memory.TOPIC_RULES} put back from {backup.name}/"]


def main(private: Path, state: Path, apply_to: str = "", undo_day: str = "") -> int:
    """`yerk curate --lessons` (the dry run), `--lessons-apply FILE`, `--lessons-undo DAY`; exits 1 on a refusal, 2 when it could not run."""
    try:
        conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
        s = memory.settings(private, state, dict(conf.get("memory", {})))
        out = undo(s, undo_day) if undo_day else apply(private, state, s, Path(apply_to)) if apply_to else [
            f"proposals: {dry_run(private, state, s)}; nothing else written"]
    except RuntimeError as e:
        print(f"refused: {e}")
        return 1
    except (Exception, SystemExit) as e:
        print(f"curate --lessons failed: {type(e).__name__}: {str(e)[:160]}")
        return 2
    print("\n".join(out))
    return 0
