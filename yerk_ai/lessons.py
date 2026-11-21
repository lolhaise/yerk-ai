"""Keeps the standing rules a room is given as lessons, one file per lesson in memory/rules/ that opens with a one-line summary, and their index,
rules.md, which a room loads when its session starts. It follows the Claude docs' lesson shape (2026-11-10): "Store one lesson per file with a
(code-memory). Each lesson is `<memory>/rules/<slug>.md`: front matter first, `class:` its first key and the block under
files" (code-memory). Each lesson is `<memory>/rules/<slug>.md`: front matter first, `class:` its first key and the block under FRONT_MAX
characters, since memory.file_class reads that much of a file and a file with no class is private, hidden from every room that is not the owner's;
then the summary, one imperative line a room can follow with the body unread; then the body, its reason and its cited examples. rules.md is the
index: one line per job, the job and its lessons' files, in the order filed, no summary, so a store whose rules split into several lessons each
stays under code-memory's limits (a capped start loads it). A lesson is replaced in place ("update an existing note rather than creating a
duplicate") and its slug never changes; a retired one's file is deleted and a row goes to RETIRED (slug, day, replaced by), so a cite to it still
leads to the lesson that replaced it, and CITES, written by the conversion (curate.lessons), maps every rules.md:N and rules-topics.md:N cite from
lesson its rule became. An install with no rules/ folder has not converted and keeps the one-line-per-rule files (memory.rule)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import write_private

DIR = "rules"                      # <memory>/rules/<slug>.md, one lesson each; the folder is the mark of a converted store
INDEX = "rules.md"                 # the index (memory.RULES), what a session start loads
RETIRED = "retired.tsv"            # slug, day, the slug that replaced it ("" for none): a retired lesson's tombstone
NO_REASON = "rules-no-reason.tsv"  # slug, day, the old cite: a lesson converted with no reason on file, as the owner's side says
CITES = "rules-cites.tsv"          # an old store cite (rules.md:N, rules-topics.md:N), the slug its rule became
SNAPSHOT = "converted.json"        # in the backup folder: the lessons as converted, news's look for a session from before
BACKUP = "rules-backup-"           # <memory>/rules-backup-<day>/: the originals, copied before a conversion writes
HEAD = ["---", "class: shared", "---", "# Rules: one line per job, its lessons' files under rules/", ""]
FRONT_MAX = 400                    # memory.file_class's read
SUMMARY_MAX = 140                  # shape.LESSON's summary check, a lesson's first line
NEAR = 0.9                         # an index at this share of a code-memory limit is near it: a capped install's filing says to
                                   # shorten it, an uncapped one's daily run says its size (a measurement)
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CITE = re.compile(r"(?<![\w./-])(?:[\w.-]+/)*rules/([a-z0-9]+(?:-[a-z0-9]+)*)\.md(?::\d+)?")    # a lesson's cite
OLD_CITE = re.compile(r"(?<![\w./-])((?:[\w.-]+/)*rules(?:-topics)?\.md:\d+)")       # a cite into the store from before
FILE = r"rules/[a-z0-9]+(?:-[a-z0-9]+)*\.md"
ROW = re.compile(rf"^- (.*) \(({FILE}(?:, {FILE})*)\)$")          # an index line: a job, its lessons' files
LESSON_MARK = "# lessons"          # news's text of a converted store opens with it, a heading the diff never tells


@dataclass(frozen=True)
class Lesson:
    slug: str
    job: str
    summary: str
    body: str = ""
    cls: str = "shared"
    rooms: tuple[str, ...] = ()    # the rooms it is kept to ([rooms: ...] before the conversion); none: every room
    on: tuple[str, ...] = ()       # the words it was filed to load on ([on: ...]); none: its own words pick them
    filed: str = ""                # "<room>, <time>" and " [m:<id>]": who filed it, when, from which message

    @property
    def cite(self) -> str:
        return f"{DIR}/{self.slug}.md"

    @property
    def index_line(self) -> str:
        """The lesson's line in a capped start: its summary and its file, so a lesson of one sentence reaches the room whole."""
        return f"- {self.summary} ({self.cite})"

    @property
    def whole(self) -> str:
        """The lesson as one line, summary then body: what an uncapped start and a message naming its topic send."""
        return f"- {self.summary}{' ' + self.body if self.body else ''} ({self.cite})"

    @property
    def line(self) -> str:
        """The lesson in a rule line's grammar (`- job: words [on: ...] [rooms: ...] (room, time) [m:id]`), which memory's
        triggers, scope and on_topic read, so a lesson loads on the words its rule loaded on."""
        marks = (f" [on: {', '.join(self.on)}]" if self.on else "") + (f" [rooms: {','.join(self.rooms)}]" if self.rooms else "")
        when, _, mid = self.filed.partition(" [m:")
        tail = (f" ({when})" + (f" [m:{mid}" if mid else "")) if when else ""
        return f"- {self.job}: {self.summary}{' ' + self.body if self.body else ''}{marks}{tail}"

    def front(self) -> str:
        keys = [("class", self.cls), ("job", self.job), ("rooms", ",".join(self.rooms)), ("on", ", ".join(self.on)), ("filed", self.filed)]
        return "\n".join(["---", *(f"{k}: {v}" for k, v in keys if v), "---"]) + "\n"

    def text(self) -> str:
        return self.front() + "\n".join([self.summary, *([self.body] if self.body else [])]) + "\n"


def folder(root: Path) -> Path:
    return root / DIR


def converted(root: Path) -> bool:
    """A store in the lesson layout: its rules/ folder is there."""
    return (root / DIR).is_dir()


def slug_of(job: str, taken: set[str]) -> str:
    """A new lesson's file name from its job's words, one no live or retired lesson has, so a cite never names two."""
    base = "-".join(re.findall(r"[a-z0-9]+", job.lower()))[:60].strip("-") or "lesson"
    out, n = base, 2
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out


def parse(path: Path) -> Lesson | None:
    """A lesson file's lesson; None for a file with no front matter or no summary."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not (m := FRONT.match(text)):
        return None
    keys = {k.strip(): v.strip() for k, _, v in (x.partition(":") for x in m.group(1).splitlines())}
    rest = [x.strip() for x in text[m.end():].splitlines() if x.strip()]
    if not rest:
        return None
    return Lesson(path.stem, keys.get("job", ""), rest[0], " ".join(rest[1:]), keys.get("class") or "private",
                  tuple(r.strip() for r in keys.get("rooms", "").split(",") if r.strip()),
                  tuple(t.strip() for t in keys.get("on", "").split(",") if t.strip()), keys.get("filed", ""))


def order(root: Path) -> list[str]:
    """The live slugs in the index's order, then a lesson file the index does not list (one written by hand), by name."""
    rows = (root / INDEX).read_text(encoding="utf-8", errors="replace").splitlines() if (root / INDEX).is_file() else []
    listed = list(dict.fromkeys(slug for x in rows if (m := ROW.match(x)) for slug in CITE.findall(m[2])))
    there = {p.stem for p in (root / DIR).glob("*.md")} if converted(root) else set()
    return [x for x in listed if x in there] + sorted(there - set(listed))


def store(root: Path) -> list[Lesson]:
    """Every live lesson, in the index's order."""
    return [x for s in order(root) if (x := parse(root / DIR / f"{s}.md")) is not None]


def index_rows(lessons: list[Lesson]) -> list[str]:
    """The index's lines: one a job, at its first lesson's place, the job and its lessons' files in their order, no summary."""
    jobs: dict[str, list[str]] = {}
    for x in lessons:
        jobs.setdefault(x.job or x.slug, []).append(x.cite)
    return [f"- {job} ({', '.join(files)})" for job, files in jobs.items()]


def index_text(lessons: list[Lesson]) -> str:
    return "\n".join([*HEAD, *index_rows(lessons)]) + "\n"


def retired(root: Path) -> dict[str, tuple[str, str]]:
    """slug -> (day, the slug that replaced it) for each retired lesson, the newest row last."""
    f = root / RETIRED
    rows = [x.split("\t") for x in f.read_text().splitlines()] if f.is_file() else []
    return {r[0]: (r[1] if len(r) > 1 else "", r[2] if len(r) > 2 else "") for r in rows if r and r[0]}


def cites(root: Path) -> dict[str, str]:
    """An old store cite (its file's name and line, `rules.md:12`) -> the slug its rule became, from the conversion."""
    f = root / CITES
    rows = [x.partition("\t") for x in f.read_text().splitlines()] if f.is_file() else []
    return {a.rpartition("/")[2]: b for a, _, b in rows if b}


HOPS = 64                          # a walk along replacements: the real store's longest chain ran 10 (2026-11-10)


def named(root: Path, cite: str) -> str:
    """The slug a cite names: a lesson's own (rules/<slug>.md, a bare slug), or an old store cite's by CITES; "" for none."""
    cite = cite.strip()
    if m := CITE.search(cite):
        return m[1]
    if m := OLD_CITE.search(cite):
        return cites(root).get(m[1].rpartition("/")[2], "")
    return cite if SLUG.fullmatch(cite) else ""


def live_slug(root: Path, cite: str) -> str:
    """The live lesson a cite leads to (named), a retired one on along RETIRED to the lesson that replaced it (HOPS at most, a
    slug met twice ends it); "" when it leads to none."""
    slug, gone, been = named(root, cite), retired(root), set[str]()
    while slug and slug not in been and len(been) < HOPS:
        if (root / DIR / f"{slug}.md").is_file():
            return slug
        been.add(slug)
        slug = gone.get(slug, ("", ""))[1]
    return ""


def no_reason(root: Path) -> dict[str, tuple[str, str]]:
    """slug -> (day, the old cite) of each lesson NO_REASON lists: carried with no reason, since the owner's side says none is on file."""
    f = root / NO_REASON
    rows = [x.split("\t") for x in f.read_text(encoding="utf-8", errors="replace").splitlines()] if f.is_file() else []
    return {r[0]: (r[1], r[2]) for r in rows if len(r) >= 3}


def unlist(root: Path, slug: str) -> None:
    """`slug` off NO_REASON: a filing that replaced it passed the gate with its reason, or a retire took it away."""
    if slug in (have := no_reason(root)):
        write_private(root / NO_REASON, "".join(f"{k}\t{d}\t{c}\n" for k, (d, c) in have.items() if k != slug))


def put(root: Path, lesson: Lesson) -> Lesson | None:
    """`lesson` written: its file replaced in place, or a new file, listed on its job's index line, or a new job's line after the
    last; off NO_REASON, since it passed the gate. The lesson it replaced, None for a new one. The caller holds the writers' lock."""
    (root / DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    path, have = root / DIR / f"{lesson.slug}.md", store(root)
    was = next((x for x in have if x.slug == lesson.slug), None)
    write_private(path, lesson.text())
    write_private(root / INDEX, index_text([lesson if x.slug == lesson.slug else x for x in have] + ([] if was else [lesson])))
    unlist(root, lesson.slug)
    return was


def drop(root: Path, slug: str, day: str, by: str = "") -> Lesson | None:
    """A lesson retired: its file deleted and off its job's index line and NO_REASON, a row in RETIRED (by: the slug that replaced
    it). The lesson."""
    was = parse(path := root / DIR / f"{slug}.md")
    if was is None:
        return None
    with (root / RETIRED).open("a", encoding="utf-8") as f:
        f.write(f"{slug}\t{day}\t{by}\n")
    path.unlink()
    write_private(root / INDEX, index_text(store(root)))
    unlist(root, slug)
    return was


def near(root: Path, most: int, size: int) -> list[str]:
    """The index against code-memory's limits (lines, bytes): each one it is near (NEAR) or past, said; none under both."""
    p = root / INDEX
    rows, have = (p.read_text(encoding="utf-8", errors="replace").splitlines(), p.stat().st_size) if p.is_file() else ([], 0)
    return [f"{len(rows)} lines of {most}"] * (len(rows) >= NEAR * most) + [f"{have:,} bytes of {size:,}"] * (have >= NEAR * size)


def snapshot(root: Path) -> list[list[Any]]:
    """The newest conversion's lessons as converted, [slug, rooms, whole line], news's look for a session that began before it."""
    for f in sorted(root.glob(f"{BACKUP}*/{SNAPSHOT}"), reverse=True):
        try:
            return [list(x) for x in json.loads(f.read_text())]
        except (OSError, ValueError, TypeError):
            continue
    return []


def keep(root: Path, day: str, made: list[tuple[Lesson, list[str]]]) -> Path:
    """The lessons as converted, each with the hashes of the old lines it came from, in the day's backup folder (snapshot)."""
    f = root / f"{BACKUP}{day}" / SNAPSHOT
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(f, json.dumps([[x.slug, list(x.rooms), x.whole, [hashlib.sha1(o.encode()).hexdigest()[:12] for o in olds]]
                                 for x, olds in made]))
    return f


def shown(lessons: list[Lesson], room: str | None) -> list[Lesson]:
    """The lessons room `room` may have: none kept to other rooms; `room` None: every one."""
    return [x for x in lessons if room is None or not x.rooms or room in x.rooms]


def news_text(lessons: list[Lesson]) -> str:
    """A converted store as news compares it: LESSON_MARK, then each lesson whole, one line, so a changed summary or body is told."""
    return "\n".join([LESSON_MARK, *(x.whole for x in lessons)])

