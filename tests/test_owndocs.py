"""Checks that the pages `yerk docs own` writes are the code's own words, the same bytes every time, and that docs/ is
never stale against the code. The check, decision 16, has the same shape as the budget test's [code] ceiling."""
import ast
import tomllib
from pathlib import Path

from yerk_ai import cli, hooks, owndocs
from yerk_ai.config import REPO, ROOM_KEYS, ROOM_WORDS, YERKD_WORDS


def test_the_docs_are_what_the_code_writes():
    """The staleness test: a change to a parser, a tool, a key table, budgets.toml, a hook event or a unit changes a page,
    so the change that makes it runs `yerk docs own` in the same commit."""
    old = owndocs.stale()
    assert old == [], f"docs/{', docs/'.join(old)} stale against the code: run `yerk docs own` in this change"


def test_the_pages_are_the_same_bytes_every_time_and_carry_nothing_of_the_install(tmp_path, monkeypatch):
    """Twice the same bytes, and the same again under another install's private layer, state folder, working folder and
    terminal width: no value of the owner's rides into a page (decision 22)."""
    first = owndocs.pages()
    private = tmp_path / "private"
    (private / "memory").mkdir(parents=True)
    (private / "rooms.toml").write_text('[rooms.quuxroom]\nchannel = "discord:1"\nmodel = "quuxmodel"\n')
    (private / "yerkd.toml").write_text('[memory]\ntimezone = "Asia/Tokyo"\n[presentation]\nfooter = "full"\n')
    monkeypatch.setenv("YERK_PRIVATE", str(private))
    monkeypatch.setenv("YERK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "40")
    monkeypatch.chdir(tmp_path)
    again = owndocs.pages()
    assert again == first and list(first) == ["commands.md", "tools.md", "settings.md", "budgets.md", "hooks.md", "units.md"]
    assert not [n for n, t in again.items() if "quux" in t or "Asia/Tokyo" in t]


def test_the_dry_run_names_a_stale_page_and_writes_nothing_and_the_run_writes_it(tmp_path, capsys):
    assert owndocs.main(True, tmp_path) == 1 and list(tmp_path.iterdir()) == []
    assert "docs/commands.md: stale against the code" in capsys.readouterr().out
    assert owndocs.main(False, tmp_path) == 0 and sorted(p.name for p in tmp_path.iterdir()) == sorted(owndocs.pages())
    (tmp_path / "hooks.md").write_text("written by hand\n")
    assert owndocs.stale(tmp_path) == ["hooks.md"] and owndocs.main(True, tmp_path) == 1
    assert cli.main(["docs", "own", "--dry-run"]) == 0 and "every page is what the code writes" in capsys.readouterr().out


def test_every_event_a_room_registers_is_described_and_every_described_one_registered():
    assert set(hooks.settings_for("http://127.0.0.1:1/hook", "header")) == set(hooks.EVENTS)


def test_every_key_described_is_one_the_package_names_outside_the_tables():
    """A described key the code no longer names would be a stale line in settings.md: each rooms.toml key and each yerkd.toml
    section and key (an upper-case placeholder aside) is a string the package holds outside the two word tables."""
    named: set[str] = set()
    for f in sorted((REPO / "yerk_ai").rglob("*.py")):
        tree = ast.parse(f.read_text())
        tables = [a for a in ast.walk(tree) if isinstance(a, ast.Assign) and any(getattr(t, "id", "") in ("ROOM_WORDS", "YERKD_WORDS") for t in a.targets)]
        skip = {id(n) for a in tables for n in ast.walk(a)}
        named |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip}
    want = set(ROOM_WORDS) | set(YERKD_WORDS) | {k for ks in YERKD_WORDS.values() for k in ks if not k.isupper()}
    assert sorted(want - named) == [] and ROOM_KEYS == frozenset(ROOM_WORDS)


YERKD = "yerkd.toml"
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def described_yerkd() -> set[tuple[str, str]]:
    """settings.md's yerkd.toml part: (section, key) a key line, (section, "") a section."""
    part = owndocs.settings().split("\n## yerkd.toml\n", 1)[1].split("\n## jobs.toml\n", 1)[0]
    out, sec = set(), ""
    for line in part.splitlines():
        if line.startswith("### ["):
            sec = line[5:line.index("]")]
            out.add((sec, ""))
        elif sec and line.startswith("- `"):
            out.add((sec, line[3:line.index("`", 3)]))
    return out


def bare(n: ast.AST) -> ast.AST:
    """The dict an expression holds through dict(), cast(), `or {}` and `if ... else {}`."""
    while True:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("dict", "cast") and n.args:
            n = n.args[-1]
        elif isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            n = n.values[0]
        elif isinstance(n, ast.IfExp):
            n = n.body
        else:
            return n


def read_of(n: ast.AST) -> tuple[ast.AST | None, str]:
    """`r.get("k", ...)` or `r["k"]` as (r, "k")."""
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args \
            and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
        return n.func.value, n.args[0].value
    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
        return n.value, n.slice.value
    return None, ""


def own(scope: ast.AST) -> list[ast.AST]:
    """A scope's own nodes: a def or a class inside it is listed, not entered."""
    out, todo = [], [c for c in ast.iter_child_nodes(scope) if not isinstance(c, ast.arguments)]
    while todo:
        out.append(n := todo.pop())
        if not isinstance(n, (*DEFS, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(n))
    return out


def held(t: ast.AST) -> str | None:
    """What an expression names that an assignment can bind: `x`, or `self.x`."""
    if isinstance(t, ast.Name):
        return t.id
    return f"self.{t.attr}" if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self" else None


def rebound(t: ast.AST) -> list[str]:
    """The names an assignment target binds (a subscript or another object's attribute binds none)."""
    if isinstance(t, (ast.Tuple, ast.List)):
        return [h for e in t.elts for h in rebound(e)]
    if isinstance(t, ast.Starred):
        return rebound(t.value)
    return [h] if (h := held(t)) else []


def loads_yerkd(n: ast.AST, paths: set[str]) -> bool:
    """`load_toml(PATH)` with PATH spelling out yerkd.toml, or a name bound to one that does."""
    n = bare(n)
    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "load_toml" and n.args):
        return False
    return (held(n.args[0]) or "") in paths or any(isinstance(c, ast.Constant) and c.value == YERKD for c in ast.walk(n.args[0]))


def yerkd_reads() -> dict[tuple[str, str], set[str]]:
    """Every yerkd.toml (section, key) the package reads by name, with where: `r.get("k")` or `r["k"]` on what load_toml
    returns for yerkd.toml, or on a section of it (through dict(), cast(), `or {}`), held in a name, a `self.` attribute
    or a parameter a call hands it to; a key read on the file itself is a section, (key, ""). A function is a scope of
    its own (a def inside one sees its names), and a name bound to one of these and to anything else in a scope is left
    out, never guessed. A key built at run time is not seen."""
    files = {f: ast.parse(f.read_text()) for f in sorted((REPO / "yerk_ai").rglob("*.py"))}
    defs: dict[tuple[str, str], tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for f, t in files.items():
        for d in ast.walk(t):
            if isinstance(d, DEFS):
                defs.setdefault((f.stem, d.name), (f, d))
            elif isinstance(d, ast.ClassDef):                 # a class called is its __init__ called
                defs.update({(f.stem, d.name): (f, b) for b in d.body if isinstance(b, ast.FunctionDef) and b.name == "__init__"})
    imported = {(f.stem, a.asname or a.name): (str(i.module).rsplit(".", 1)[-1], a.name)
                for f, t in files.items() for i in ast.walk(t) if isinstance(i, ast.ImportFrom) and i.level for a in i.names}
    mods = {f.stem for f in files}
    reads: dict[tuple[str, str], set[str]] = {}
    passed: dict[int, dict[str, str]] = {}                   # id(def): the parameters a call hands the file ("") or a section
    calls: list[tuple[tuple[str, str], str, str]] = []

    def scan(f: Path, scope: ast.AST, outer: dict[str, str]) -> None:
        a = getattr(scope, "args", None)
        params = {p.arg for p in [*a.posonlyargs, *a.args, *a.kwonlyargs]} if a else set()
        nodes = own(scope)
        paths = {h for s in nodes if isinstance(s, ast.Assign) for t in s.targets
                 if (h := held(t)) and any(isinstance(c, ast.Constant) and c.value == YERKD for c in ast.walk(s.value))}
        base = {k: v for k, v in outer.items() if k not in params} | passed.get(id(scope), {})
        env = dict(base)

        def kind(n: ast.AST) -> str | None:                    # "" the file, a section's name, or None
            if loads_yerkd(n, paths):
                return ""
            n = bare(n)
            if (h := held(n)) is not None:
                return env.get(h)
            r, k = read_of(n)
            return k if r is not None and kind(r) == "" else None

        for _ in range(3):                                      # a name bound from another name of the same scope
            bound: dict[str, set[str | None]] = {}
            for s in nodes:
                if isinstance(s, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and s.value is not None:
                    pairs = [(t, s.value) for t in (s.targets if isinstance(s, ast.Assign) else [s.target])]
                elif isinstance(s, (ast.For, ast.AsyncFor, ast.comprehension)):
                    pairs = [(s.target, None)]
                elif isinstance(s, ast.withitem) and s.optional_vars is not None:
                    pairs = [(s.optional_vars, None)]
                else:
                    continue
                for t, v in pairs:
                    for h in rebound(t):
                        bound.setdefault(h, set()).add(kind(v) if v is not None and held(t) == h else None)
            env.clear()
            env.update({k: v for k, v in base.items() if k not in bound})
            env.update({h: next(iter(ks)) for h, ks in bound.items() if len(ks) == 1 and None not in ks})
        for n in nodes:
            r, k = read_of(n)
            if r is not None and (s := kind(r)) is not None:
                reads.setdefault((s, k) if s else (k, ""), set()).add(f"{f.relative_to(REPO)}:{n.lineno}")
            if isinstance(n, ast.Call):
                fn = n.func
                callee = ((fn.value.id, fn.attr) if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in mods
                          else imported.get((f.stem, fn.id), (f.stem, fn.id)) if isinstance(fn, ast.Name) else None)
                if callee in defs:
                    d = defs[callee][1]
                    ps = [p.arg for p in [*d.args.posonlyargs, *d.args.args]]
                    for p, v in [*zip(ps[1:] if ps[:1] == ["self"] else ps, n.args), *((w.arg, w.value) for w in n.keywords if w.arg)]:
                        if (s := kind(v)) is not None:
                            calls.append((callee, p, s))
        for d in nodes:
            if isinstance(d, DEFS):
                scan(f, d, env)
            elif isinstance(d, ast.ClassDef):                   # self.X across the methods: the file, when every binding loads it
                meths = [b for b in d.body if isinstance(b, DEFS)]
                sets = [(h, s.value) for b in meths for s in own(b) if isinstance(s, ast.Assign) for t in s.targets if (h := held(t))]
                attrs = {h: "" for h, _ in sets if h.startswith("self.") and all(loads_yerkd(v, set()) for g, v in sets if g == h)}
                for b in meths:
                    scan(f, b, attrs)

    for f, t in files.items():
        scan(f, t, {})
    done = set()
    while calls:
        if (c := calls.pop()) not in done:
            done.add(c)
            (f, d), p, s = defs[c[0]], c[1], c[2]
            passed[id(d)] = passed.get(id(d), {}) | {p: s}
            scan(f, d, {})
    return reads


def test_every_yerkd_key_the_package_reads_is_described():
    """The other direction (2026-10-24, the cold read of #122): `[discord] guild`, which `yerk setup-room` reads, was on no
    page, so settings.md's claim to every key was false while the test above, which asks only that a described key is
    named somewhere, passed. Each yerkd.toml section and key the package reads by name is on settings.md, its file and line
    named when one is not; and the scan still sees the reads it was built on, so a scan gone blind cannot pass."""
    reads, described = yerkd_reads(), described_yerkd()
    blind = {("discord", "guild"), ("watch", "assistants"), ("backup", "keep"), ("memory", "root")} - set(reads)
    assert not blind, f"the scan no longer sees {sorted(blind)}: mend yerkd_reads if the code still reads them, else drop them here"
    missing = {f"[{s}] {k}".rstrip(): sorted(w) for (s, k), w in reads.items() if (s, k) not in described}
    assert missing == {}, f"read in the code, not in docs/settings.md (a line in config.YERKD_WORDS, then `yerk docs own`): {missing}"


def test_every_key_of_the_example_files_is_described():
    doc = tomllib.loads((REPO / "yerkd.toml.example").read_text())
    assert {(s, k) for s, t in doc.items() for k in t} - {(s, k) for s, ks in YERKD_WORDS.items() for k in ks} == set()
    rooms = tomllib.loads((REPO / "rooms.toml.example").read_text())["rooms"]
    assert {k for c in rooms.values() for k in c} - set(ROOM_WORDS) == set()


def test_the_readme_stays_a_ten_minute_read():
    """2026-10-24: 118,399 bytes, 57 percent of them a Status section, cut to the first section, five install lines, the private
    folder, a turn, the add-ons and the docs; the rest is docs/GUIDE.md and docs/STATUS.md, whole."""
    size = len((REPO / "README.md").read_bytes())
    assert size < 12_000, f"README.md is {size:,} bytes: the long form goes in docs/GUIDE.md, the facts the code owns in its pages"
