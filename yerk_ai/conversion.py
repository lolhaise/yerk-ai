"""`yerk curate --lessons`: rewrites the rules files as lesson files, each a one-line summary, then its reason and examples.
It turns an existing rules.md and rules-topics.md into lesson files and their index (lessons.py), as prompting-fable-5-2 says of
rules that built up: "rewrite absolute rules with their scope and reason", with the originals copied first. The dry run writes one
proposals file, <memory>/rules-proposals/lessons-<day>.md, and changes nothing else: for each original rule by its cite, its words,
the lessons made of it with each one's gate (the filing's own, memory.unshaped, on READ), and each thing done to it as a listed item;
block at its end is what an apply reads. Meaning never changes: a rule already in the shape is split as it stands (its first
sentence the summary); one out of it goes to curate's model pass ("Lessons", then "Knock down lessons" in curate.md); code alone
cites a quote the day log holds, writes the owner's go-ahead into an absolute and checks that every sentence of the original
lands in a lesson or is listed. The frozen trigger test refuses a split that loses an owner message of the last 14 days that
loads its rule today. Apply takes that exact file and refuses unless every lesson passes the gate, every original is accounted
for, the trigger test passes and the store is byte for byte the one the dry run read; it copies the originals to
<memory>/rules-backup-<day>/ first, and undo puts them back."""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import curate, lessons, memory, shape
from .config import budgets, load_toml, write_private
from .mentions import OLD

FILE = "lessons-{day}.md"          # in <memory>/rules-proposals/
APPLIED = "applied.json"           # in the backup folder: what an apply wrote, each file's sha256, for its undo
DAYS = 14                          # the trigger test's window of owner messages
OWNER = ("owner", "test-webhook")  # whose messages the trigger test reads (the webhook stands in for the owner)
PEOPLE = re.compile(r"\b(?:anyone|anybody|someone|somebody|people|a person|other persons?|others|strangers?|whoever)\b", re.I)
WORD = re.compile(r"[\w'’-]+")      # a sentence is accounted for by its words, the punctuation a cited quote left aside
GO_AHEAD = "Only the owner's go-ahead lifts it."
HIS_WORD = "Only the owner's own go-ahead lifts it, not anyone else's."     # no word a rule loads on: "person" outweighed one
ROUNDS = 2                         # the times a rule still failing goes back to the model with its findings, after its first
CHUNK = 8                          # rules a call of the first proposal, a knock-down or a round back reads; the calls run side by
                                   # side (16 to a call ran 9 minutes on a store of the owner's size; a round's rules in one call, 11)
REASONS = "reasons.tsv"            # in <memory>/rules-proposals/: cite, the daily/<day>.md:<line> holding it, the reason, a row
# The conversion's pages: the ones every room reads and the Claude Fable 5.2 page, whatever model the rooms run that minute,
# since the owner's order of 2026-11-09 names that page; the doctor's daily run follows the rooms (shape.run_by).
READ = ("claude-fable-5-2",)
WANTING = ("no reason given", "a negative with no why", "a hard style limit with no reason")   # what a reason on file would meet
WRAP = re.compile(r"\s*</?examples(?:\s[^>]*)?>", re.I)    # the wrapper left round a rule's examples once code takes them out


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

    def uncited(text: str) -> str:      # a cited quote is accounted for by its cite, a quote across two sentences too
        for x in listed:
            text = text.replace(x["said"], " ") if x["kind"] == "quote cited" else text
        return text
    said = [y for y in {flat(uncited(x["said"])) for x in listed if x["kind"] in ("left out", "rewritten")} if y.strip(" .;:,")]
    out, text = [], uncited(WRAP.sub(" ", shape.EXAMPLE_TAGS.sub(" ", rule["said"])))   # an example is code's, each listed
    held = [m[0] for m in shape.QUOTED.finditer(text)]       # a quote is never split at a sentence end inside it
    text = shape.QUOTED.sub(lambda m: f"\u201c\x00{held.index(m[0])}\x00\u201d", text)
    for sent in shape.SENTENCES.split(text):
        sent = re.sub("\u201c\x00(\\d+)\x00\u201d", lambda m: held[int(m[1])], sent)
        rest = flat(sent)
        cut = {i for y in said for m in re.finditer(re.escape(y), rest) for i in range(m.start(), m.end())}
        rest = "".join(" " if i in cut else c for i, c in enumerate(rest))    # each part the model listed, found in the
        bare = flat(shape.MARKED.sub(" ", shape.QUOTED.sub(" ", rest)))      # sentence as it stands (the parts may overlap); a
        # mark (landed, bounced) is the store's, never a lesson's: the prompt asks it written nowhere
        if rest.strip(" .;:,") and flat(sent) not in have and not set(WORD.findall(bare)) <= set(WORD.findall(have)):
            out.append(sent.strip())
    return out


def examples(rule: dict[str, Any], daily: Path, seen: dict[Path, str]) -> list[dict[str, str]]:
    """Each <example> of the original in the cited form the gate takes: the day-log line that holds its words, by exact text
    search, with its mark (Landed or Bounced) as it had one; one no line holds kept as it stands. Code's, never the model's."""
    out, most = [], shape.threshold(dict(budgets().get("shape") or {}), *shape.GLOSS)
    for ex in shape.INSIDE.findall(rule["said"]):
        mark = (m[0].capitalize() + ", ") if (m := shape.MARKED.search(ex)) else ""
        if (m := shape.SHOWN.match(ex.strip())) and len(m[2].split()) > most:      # cited, its gloss past the gate's room
            out.append({"kind": "example cited", "said": ex.strip(), "as": f"<example>{mark}{m[1]}</example>",
                        "why": f"its gloss of {len(m[2].split())} words left out, since the gate takes {most} at most beside a cite"})
        elif shape.SHOWN.match(ex.strip()) or not (quote := shape.MARKED.sub(" ", ex).strip(shape.QUOTES_ALL + ",:;.")):
            out.append({"kind": "example cited", "said": ex.strip(), "as": f"<example>{ex.strip()}</example>"})
        elif at := next((x for q in [quote, *(m[1] for m in shape.QUOTED.finditer(ex))] if (x := shape.on_record(q, daily, seen))), ""):
            out.append({"kind": "example cited", "said": ex.strip(), "at": at, "as": f"<example>{mark}{at}</example>"})
        else:
            out.append({"kind": "example kept", "said": ex.strip(), "as": f"<example>{ex.strip()}</example>",
                        "why": "no day-log line holds it"})
    return out


def reason_of(s: memory.Settings, rule: dict[str, Any], why: dict[str, Any], supplied: dict[str, list[tuple[str, str]]]) -> dict[str, str]:
    """A proposed reason as a listed item: added when the rule's own words or the day-log line it names hold it word for word,
    supplied when it is the owner's side's for this cite (REASONS), else refused; a reason is never invented."""
    said, src = str(why.get("said") or ""), str(why.get("from") or "")
    if mine := next((x for x in supplied.get(rule["cite"], []) if flat(said) and flat(said) in flat(x[1])), None):
        return {"kind": "reason supplied", "said": said, "from": f"{mine[0]}, named in {curate.PROPOSALS}/{REASONS}"}
    line = (s.daily / src.removeprefix("daily/").rpartition(":")[0]) if src.startswith("daily/") else None
    n = int(src.rpartition(":")[2]) if line and src.rpartition(":")[2].isdigit() else 0
    rows = line.read_text(errors="replace").splitlines() if line and line.is_file() else []
    held = flat(rule["said"]) if not line else flat(rows[n - 1]) if 0 < n <= len(rows) else ""
    return ({"kind": "reason added", "said": said, "from": src or "the rule's own words"} if flat(said) and flat(said) in held
            else {"kind": "reason refused", "said": said, "why": "neither the rule's own words nor the line it names hold it"})


def supplied_reasons(s: memory.Settings) -> dict[str, list[tuple[str, str]]]:
    """cite -> each (its day-log line, the reason) row of <memory>/rules-proposals/REASONS that the line it names holds word for
    word (`cite<TAB>daily/<day>.md:<line><TAB>reason`, # a comment), the owner's side's reason for a rule with none on file, a
    row a reason, so a rule split in parts may have one for each part that has none."""
    p, out = s.root / curate.PROPOSALS / REASONS, dict[str, list[tuple[str, str]]]()
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []:
        cite, at, why = ([*raw.split("\t"), "", ""])[:3]
        f, _, n = at.strip().removeprefix("daily/").rpartition(":")
        rows = (s.daily / f).read_text(errors="replace").splitlines() if f and (s.daily / f).is_file() else []
        if not raw.startswith("#") and why.strip() and n.isdigit() and 0 < int(n) <= len(rows) and flat(why) in flat(rows[int(n) - 1]):
            out.setdefault(cite.strip(), []).append((at.strip(), why.strip()))
    return out


def keeps(line: str, weights: tuple[dict[str, float], float]) -> list[str]:
    """The words a rule loads on today as the `on` words each lesson made of it keeps (on_topic weighs an `on` phrase held as a
    word only one rule says, so it always loads): each smallest set of its trigger words whose weight among the live rules
    reaches memory.RIDE, a set one phrase, three words at most. A rule a set of more words could load keeps each trigger word
    alone, which loads more often but never less, so a message that loads it today always loads a lesson made of it."""
    weight, one = weights
    tr, picked, ride = memory.triggers(line), {memory.wants(t) for t in memory.picked(line)}, memory.RIDE * weights[1]
    w = {k: one if k in picked else weight.get(k, one) for k in tr}
    light = sorted((k for k in w if w[k] < ride), key=lambda k: w[k])
    if next((n for n in range(1, len(light) + 1) if sum(w[k] for k in light[:n]) >= ride), 0) > 3:
        return list(dict.fromkeys(tr.values()))
    out: list[tuple[str, ...]] = [(k,) for k in w if w[k] >= ride]
    for c in (c for n in (2, 3) for c in itertools.combinations(sorted(light, key=lambda k: -w[k]), n)):
        out += [c] if sum(w[k] for k in c) >= ride and not any(set(o) <= set(c) for o in out) else []
    return [" ".join(tr[k] for k in c) for c in out]


def build(s: memory.Settings, rule: dict[str, Any], prop: dict[str, Any] | None, read: Any, seen: dict[Path, str],
          taken: set[str], acronyms: frozenset[str] | None, supplied: dict[str, list[tuple[str, str]]] | None = None) -> dict[str, Any]:
    """The lessons made of one rule, each with its gate, and what was done to it. Each lesson keeps the words its rule loads on
    (keeps; else memory.triggers' every word: its job's, its picked ones, what its words name) as its `on`, so no split loses a
    trigger; each
    example of the rule lands in the first lesson whose body holds it, in the cited form, else is listed as left out."""
    most, supplied = shape.threshold(dict(budgets().get("shape") or {}), "lesson_quote_words", 8), supplied or {}
    parts = [(x.get("summary", ""), x.get("body", ""), x.get("reason")) for x in (prop or {}).get("lessons", []) if isinstance(x, dict)] \
        or [(*memory.split_summary(WRAP.sub("", shape.EXAMPLE_TAGS.sub("", rule["said"])).strip()), None)]
    done = [{"kind": k, **x} for k in ("left out", "rewritten") for x in (prop or {}).get(k.replace(" ", "_"), []) if isinstance(x, dict)]
    whys = [w for *_, w in parts if isinstance(w, dict) and w.get("said")] + [w for w in [(prop or {}).get("reason")] if isinstance(w, dict) and w.get("said")]
    done += list({json.dumps(x, sort_keys=True): x for x in (reason_of(s, rule, w, supplied) for w in whys)}.values())
    if (c := (prop or {}).get("caution")):
        done.append({"kind": "caution to review", "said": c, "why": "left in place: deleting one is the owner's side's call"})
    every = list(dict.fromkeys(memory.triggers(rule["text"]).values()))
    on = rule.get("keeps") or every
    shown_ = examples(rule, s.daily, seen)
    made = []
    for summary, body, _ in parts:
        mine = {m[0]: examples({"said": m[0]}, s.daily, seen)[0] for m in shape.INSIDE.finditer(body)}    # an <example> the
        body = shape.INSIDE.sub(lambda m: mine[m[0]]["as"], body)                                         # model wrote, cited
        body, cited = quotes(body, s.daily, seen, most)
        done += list(mine.values())
        summary, cited2 = quotes(summary, s.daily, seen, most)
        body2, added = absolute(rule["job"], f"{summary} {body}".strip(), read)
        body = body2[len(summary):].strip()
        slug = lessons.slug_of(rule["job"], taken)
        taken.add(slug)
        made.append({"slug": slug, "job": rule["job"], "summary": summary, "body": body, "cls": rule["cls"], "rooms": rule["rooms"],
                     "on": on, "filed": rule["filed"]})
        if len(lesson_of(made[-1]).front()) > lessons.FRONT_MAX:      # the sets past the front matter's room: each word alone
            made[-1]["on"] = on = every
        while made[-1]["on"] and len(lesson_of(made[-1]).front()) > lessons.FRONT_MAX:
            made[-1]["on"] = made[-1]["on"][:-1]
        done += cited + cited2 + added
    room = shape.threshold(dict(budgets().get("shape") or {}), "lesson_body_chars", 600)
    for x in shown_:                    # the examples, each in the first lesson whose body still holds it
        if at := next((y for y in made if len(shape.CITED.sub("", f"{y['body']} {x['as']}")) <= room), None):
            at["body"] = f"{at['body']} {x['as']}".strip()
        else:
            x = {**x, "kind": "example left out", "why": "the lesson's body would pass its length with it"}
        done.append(x)
    for x in made:
        x["gate"] = (memory.unshaped(rule["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, read, seen) or {}).get("fix", [])
        if len(x["on"]) < len(on):
            done.append({"kind": "triggers trimmed", "said": ", ".join(on[len(x["on"]):]), "why": f"the front matter holds {lessons.FRONT_MAX} characters"})
    row = f"{curate.PROPOSALS}/{REASONS} names one for it" if rule["cite"] in supplied else f"a row in {curate.PROPOSALS}/{REASONS} supplies one"
    if not any(x["kind"] in ("reason added", "reason supplied") for x in done) and "no reason" in shape.lesson(rule["job"], rule["said"], None, {}, read=read):
        done.append({"kind": "no reason on file", "why": "listed for the owner's side: a reason is never invented; " + row})
    elif prop:                          # a split rule's lesson refused only for a reason, whose part the rule gives none
        done += [{"kind": "no reason on file", "said": x["summary"], "why": "listed for the owner's side: the rule gives this lesson's part no "
                  "reason and a reason is never invented; " + row} for x, (*_, w) in zip(made, parts) if x["gate"]
                 and all(g.startswith(WANTING) for g in x["gate"]) and not (isinstance(w, dict) and w.get("said"))]
    return {**{k: rule[k] for k in ("cite", "file", "line", "text", "job")}, "lessons": made, "done": done,
            "missing": accounted(rule, made, done)}


def findings(m: dict[str, Any]) -> list[str]:
    """What still fails in one rule's lessons, word for word: each lesson's gate refusal, each sentence of the rule not accounted
    for and each reason refused. A refusal for no reason goes back too, since a reason a reader sees ("which slows the page")
    is none to the gate; the prompt says to give none where none is on file."""
    return [f'lesson {i} ("{x["summary"]}"): {g}' for i, x in enumerate(m["lessons"], 1) for g in x["gate"]] + \
        [f'not accounted for: "{x}"' for x in m["missing"]] + \
        [f'reason refused: "{d["said"]}" ({d["why"]})' for d in m["done"] if d["kind"] == "reason refused"]


def lesson_of(x: dict[str, Any]) -> lessons.Lesson:
    return lessons.Lesson(x["slug"], x["job"], x["summary"], x["body"], x["cls"], tuple(x["rooms"]), tuple(x["on"]), x["filed"])


def trigger_test(rules: list[dict[str, Any]], made: list[dict[str, Any]],
                 messages: list[tuple[str, str]]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """The frozen trigger test: cite -> each owner message that loads the rule today (memory.on_topic among every live rule)
    and loads no lesson made of it (on_topic among every lesson made), none lost no key; and cite -> how many load it."""
    old = [r["text"] for r in rules]
    new = {r["cite"]: [lesson_of(x).line for x in m["lessons"]] for r, m in zip(rules, made)}
    corpus = [x for v in new.values() for x in v]
    w_old, w_new, lost, loads = memory.rarities(old), memory.rarities(corpus), dict[str, list[str]](), dict[str, int]()
    for at, said in messages:
        if not (hit := {x for _, x in memory.on_topic(said, old, old, w_old)}):
            continue
        now = {x for _, x in memory.on_topic(said, corpus, corpus, w_new)}
        for r in rules:
            if r["text"] in hit:
                loads[r["cite"]] = loads.get(r["cite"], 0) + 1
                if not now & set(new[r["cite"]]):
                    lost.setdefault(r["cite"], []).append(at)
    return lost, loads


def proposed(said: str, cites: set[str]) -> dict[str, dict[str, Any]]:
    """cite -> the JSON object a model's answer gives for it, one a line."""
    out = {}
    for raw in said.splitlines():
        try:
            if isinstance(x := json.loads(raw.strip()), dict) and x.get("cite") in cites:
                out[x["cite"]] = x
        except ValueError:
            continue
    return out


def model_pass(rules: list[dict[str, Any]], model: Callable[[str, str], str], check: Callable[[dict[str, Any], dict[str, Any]], list[str]],
               supplied: dict[str, list[tuple[str, str]]] | None = None) -> dict[str, dict[str, Any]]:
    """curate's propose and knock-down machinery on the rules out of the shape: curate.md's "Lessons" proposes each rule's lessons
    as one JSON line (CHUNK rules a call, side by side), "Knock down lessons" keeps or drops each; a rule whose lessons `check`
    still finds failing (the gate's refusals, its words not all accounted for) or that was dropped goes back with its proposal and
    the exact findings ("Lessons again", the round's failing rules CHUNK a call, side by side), ROUNDS times at most. cite -> {"prop": its last
    proposal (none: no answer came for it), "findings": what still fails after the last round, "rounds": the times it went back}."""
    if not rules:
        return {}
    have = supplied or {}
    shown_ = {r["cite"]: f"=== {r['cite']} (job: {r['job']})\n{WRAP.sub('', shape.EXAMPLE_TAGS.sub('', r['said'])).strip()}" + "".join(
        f"\nReason on file, from {at}: {why}" for at, why in have.get(r["cite"], [])) for r in rules}

    def side(part: str, group: list[dict[str, Any]], extra: Callable[[str], str], tools: str) -> list[str]:
        chunks = [group[i:i + CHUNK] for i in range(0, len(group), CHUNK)]
        with ThreadPoolExecutor(max(1, len(chunks))) as ex:
            return list(ex.map(lambda c: model(curate.prompt(part, {}, "\n\n".join(shown_[r["cite"]] + extra(r["cite"]) for r in c)), tools), chunks))

    props: dict[str, dict[str, Any]] = {}
    for said in side("Lessons", rules, lambda c: "", ""):
        props |= proposed(said, {r["cite"] for r in rules})
    todo, out = list(rules), dict[str, dict[str, Any]]()
    for n in range(ROUNDS + 1):
        asked = [r for r in todo if r["cite"] in props]
        said = "\n".join(side("Knock down lessons", asked, lambda c: "\n=== proposed\n" + json.dumps(props[c]), "Read,Grep,Glob")) if asked else ""
        kept = {m[1] for m in re.finditer(r"^KEEP (\S+)\s*$", said, re.M)}
        dropped = {m[1]: m[2].strip() for m in re.finditer(r"^DROP (\S+)\s*(?:\|\s*(.*))?$", said, re.M)}
        for r in todo:
            c = r["cite"]
            found = ["no proposal came back for it"] if c not in props else [*check(r, props[c]), *(
                [] if c in kept else [f"the reviewer dropped it: {dropped.get(c) or 'no verdict'}"])]
            out[c] = {"prop": props.get(c), "findings": found, "rounds": n}
        todo = [r for r in todo if out[r["cite"]]["findings"]]
        if not todo or n == ROUNDS:
            break
        for said in side("Lessons again", todo, lambda c: "\n=== proposed\n" + json.dumps(props.get(c) or {}) + "\n=== findings\n"
                         + "\n".join(f"- {f}" for f in out[c]["findings"]), ""):
            props |= proposed(said, {r["cite"] for r in todo})
    return out


def render(rec: dict[str, Any]) -> str:
    """The proposals file: each original by its cite, its lessons with their gates, and each thing done as a listed item, then
    the JSON block an apply reads; apply renders it again and refuses a file that differs."""
    out = [f"# Lessons proposed from the rule store, {rec['day']}", "",
           f"Store read: {', '.join(f'{k} {v[:12]}' for k, v in sorted(rec['store'].items()))}. Pages read: {', '.join(rec['read'])}.",
           f"Apply: `yerk curate --lessons-apply {rec['path']}`. Undo after it: `yerk curate --lessons-undo {rec['day']}`.",
           f"Ready to apply: {'yes' if rec['ready'] else 'no: ' + '; '.join(rec['why'])}.",
           *([f"The index it would write: {rec['index'][0]:,} lines and {rec['index'][1]:,} bytes, against code-memory's "
              f"{rec['index'][2]:,} and {rec['index'][3]:,}."] if rec.get("index") else []), ""]
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
    """The dry run: the proposals file written, nothing else. Its path. `read`: the pages the gate reads, else READ's."""
    now = time.time() if now is None else now
    conf = load_toml(private / "yerkd.toml") if (private / "yerkd.toml").is_file() else {}
    read = shape.pages(READ) if read is None else read
    acronyms = shape.install(dict(conf.get("docs") or {}))[1]
    rules, seen, taken = originals(s), dict[Path, str](), set(lessons.retired(s.root))
    supplied, weights = supplied_reasons(s), memory.rarities([r["text"] for r in rules])
    for r in rules:
        r["keeps"] = keeps(r["text"], weights)
    off = [r for r in rules if memory.unshaped(r["job"], r["said"], acronyms, s, read, seen)]
    props = model_pass(off, model or (lambda p, tools: curate.ask(private, state, dict(conf.get("curate", {})), p, tools)),
                       lambda r, x: findings(build(s, r, x, read, seen, set(taken), acronyms, supplied)), supplied)
    made = []
    for r in rules:
        made.append(build(s, r, (props.get(r["cite"]) or {}).get("prop"), read, seen, taken, acronyms, supplied))
        if (p := props.get(r["cite"])) and p["findings"]:
            made[-1]["done"].append({"kind": "still failing", "why": f"after {p['rounds']} round{'s' * (p['rounds'] != 1)} back to the model: "
                                     + " / ".join(p["findings"]) + ("" if p["prop"] else "; split as it stands")})
    lost, loads = trigger_test(rules, made, owner_messages(s, now))
    for r, m in zip(rules, made):
        m["loads"] = loads.get(r["cite"], 0)
    day = datetime.fromtimestamp(now, s.tz).date().isoformat()
    path = s.root / curate.PROPOSALS / FILE.format(day=day)
    bare = [x for m in made for x in m["lessons"] if x["gate"] and all(g.startswith(WANTING) for g in x["gate"]) and any(
            d["kind"] == "no reason on file" and d.get("said", x["summary"]) == x["summary"] for d in m["done"])]
    why = [f"{n} lesson{'s' * (n != 1)} the gate refuses" for n in [sum(bool(x["gate"]) for m in made for x in m["lessons"]) - len(bare)] if n]
    why += [f"{n} lesson{'s' * (n != 1)} refused only for a reason no file holds, each listed (a row in {curate.PROPOSALS}/{REASONS} "
            "supplies one)" for n in [len(bare)] if n]
    why += [f"{n} original{'s' * (n != 1)} not accounted for" for n in [sum(bool(m["missing"]) for m in made)] if n]
    why += [f"{n} original{'s' * (n != 1)} losing a trigger" for n in [len(lost)] if n]
    index, (most, size) = lessons.index_text([lesson_of(x) for m in made for x in m["lessons"]]), shape.index_limits(s.docs)
    lines, have = len(index.splitlines()), len(index.encode())
    over = [f"{n:,} {unit}, {n - cap:,} past {cap:,}" for n, cap, unit in ((lines, most, "lines"), (have, size, "bytes")) if n > cap]
    why += [f"the index it would write is past code-memory's limits: {' and '.join(over)}"] if over else []
    rec = {"day": day, "path": str(path), "store": {n: sha(s.root / n) for n in (memory.RULES, memory.TOPIC_RULES)}, "read": list(read),
           "ends": ends(s), "rules": made, "lost": lost, "ready": not why, "why": why, "index": [lines, have, most, size]}
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
    seen = dict[Path, str]()
    if off := [x["slug"] for m in rec["rules"] for x in m["lessons"]
               if memory.unshaped(x["job"], f"{x['summary']} {x['body']}".strip(), acronyms, s, rec["read"], seen)]:
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
