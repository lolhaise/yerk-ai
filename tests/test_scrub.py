"""The scrub test: no personal data or secrets anywhere in the repository."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# What a run leaves in a checkout, never part of the repository: git's own, the venvs (.venv, venv), the caches of python,
# mypy, pytest, ruff (absolute paths inside, so `ruff check .` before pytest failed this test), hypothesis and tox, node's
# packages, and the build outputs.
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis", ".tox",
             "node_modules", "build", "dist"}
PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "discord-id-shaped number": re.compile(r"(?<![\w-])\d{17,20}(?![\w-])"),
    "phone number": re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])"),
    "secret": re.compile(
        r"sk-ant-[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"|[MN][A-Za-z0-9]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
}
HOME = re.compile(r"/home/[a-z_][a-z0-9_-]*|/Users/[A-Za-z][\w.-]*")
HOME_OK = ("examples/", "tests/")
COPYRIGHT = re.compile(r"^\W*Copyright \(c\) \d{4} ")    # the MIT notice names its holder; the denylist skips that line only


def repo_files(root: Path = ROOT):
    for p in root.rglob("*"):
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts):
            yield p


def denylist() -> list[re.Pattern[str]]:
    """Decision 22: the owner's own names, people, rooms, companies and handles, one a line (`#` a comment), from a file in
    the private layer that is never committed (secrets/denylist.txt), named by YERK_DENYLIST_FILE, which only the owner's
    CI sets. Unset, as in a stranger's clone, it is empty and the generic shapes above are the whole test."""
    f = os.environ.get("YERK_DENYLIST_FILE")
    words = [w.strip() for w in Path(f).expanduser().read_text().splitlines()] if f else []
    return [re.compile(rf"(?<![\w]){re.escape(w).replace(chr(92) + ' ', chr(92) + 's+')}(?![\w])", re.I)
            for w in words if w and not w.startswith("#")]


def scan(path: Path, rel: str, deny: tuple[re.Pattern[str], ...] | list[re.Pattern[str]] = ()) -> list[str]:
    text = path.read_text(errors="ignore")
    hits = [f"{rel}: {name}: {m.group()[:40]}" for name, rx in PATTERNS.items() for m in rx.finditer(text)]
    if not rel.startswith(HOME_OK):
        hits += [f"{rel}: home path: {m.group()}" for m in HOME.finditer(text)]
    lines = text.splitlines()          # a denied word is named by its entry's number, never spelled out in a CI log
    return hits + [f"{rel}:{n}: denylist entry {i}" for i, rx in enumerate(deny, 1) for n, x in enumerate(lines, 1)
                   if rx.search(x) and not COPYRIGHT.match(x)]


def test_repository_is_clean():
    hits, deny = [], denylist()
    for p in repo_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel != "tests/test_scrub.py":
            hits += scan(p, rel, deny)
    assert not hits, "\n".join(hits)


def test_the_denylist_fails_a_planted_name_and_is_empty_without_its_file(tmp_path, monkeypatch):
    monkeypatch.delenv("YERK_DENYLIST_FILE", raising=False)
    assert denylist() == []
    (words := tmp_path / "denylist.txt").write_text("# the owner's names, one a line\nExamplia Quux\nquuxcorp\n")
    monkeypatch.setenv("YERK_DENYLIST_FILE", str(words))
    (bad := tmp_path / "leak.md").write_text("a line\nmet examplia  quux at QuuxCorp today\nquuxcorporate is fine\n")
    assert scan(bad, "docs/leak.md", denylist()) == ["docs/leak.md:2: denylist entry 1", "docs/leak.md:2: denylist entry 2"]
    bad.write_text("met Examplia Quux and QUUXCORP\n")
    assert scan(bad, "docs/leak.md", denylist()) == ["docs/leak.md:1: denylist entry 1", "docs/leak.md:1: denylist entry 2"]
    bad.write_text("# Copyright (c) 2026 Examplia Quux. MIT License.\nwritten by Examplia Quux\n")
    assert scan(bad, "docs/leak.md", denylist()) == ["docs/leak.md:2: denylist entry 1"]


def test_the_docs_and_the_readme_are_scanned_and_the_written_pages_carry_nothing_of_the_owners(tmp_path):
    """docs-from-code: README.md and every page in docs/ are among the files the scan reads (a docs/ added to SKIP_DIRS fails
    here), and the pages as `yerk docs own` writes them now, before they reach docs/, pass the same scan and denylist."""
    from yerk_ai import owndocs
    pages, rels = owndocs.pages(), {p.relative_to(ROOT).as_posix() for p in repo_files()}
    assert {"README.md", "docs/STATUS.md", "docs/GUIDE.md", *(f"docs/{n}" for n in pages)} <= rels
    hits, deny = [], denylist()
    for name, text in pages.items():
        (page := tmp_path / name).write_text(text)
        hits += scan(page, f"docs/{name}", deny)
    assert not hits, "\n".join(hits)


def test_what_a_run_leaves_in_the_checkout_is_not_scanned(tmp_path):
    for d in (".ruff_cache/0.17.1", ".hypothesis/examples", ".tox/py312", "venv/lib", "node_modules/x", "docs"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "blob").write_text("/home/someone/yerk-ai/tests/test_x.py someone@example.com\n")
    assert [p.relative_to(tmp_path).as_posix() for p in repo_files(tmp_path)] == ["docs/blob"]
    assert len(scan(tmp_path / "docs" / "blob", "docs/blob")) == 2


def test_the_scrub_catches_what_it_should(tmp_path):
    bad = tmp_path / "leak.md"
    bad.write_text("mail someone@example.com, id 123456789012345678, call 416-555-0199, "
                   "key sk-ant-" + "a" * 24 + ", path /home/someone/x")
    kinds = {h.split(": ")[1] for h in scan(bad, "docs/leak.md")}
    assert kinds == {"email", "discord-id-shaped number", "phone number", "secret", "home path"}


# The README's promise: Yerk AI never reads the credential files in a Claude Code config folder.
# Claude Code reads its own login; the package never names a credential file or its fields.
CREDENTIAL_READS = re.compile("|".join(re.escape(x) for x in (
    ".credentials" + ".json", "claudeAi" + "Oauth", "oauth" + "Account", "access" + "Token", "refresh" + "Token",
    "/api/" + "oauth/", ".claude" + ".json")))


def credential_reads(root: Path) -> list[str]:
    return [f"{p.relative_to(root.parent).as_posix()}:{n}: {m.group()}"
            for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts
            for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1)
            for m in CREDENTIAL_READS.finditer(line)]


def test_the_package_never_reads_a_config_folders_credentials():
    hits = credential_reads(ROOT / "yerk_ai")
    assert not hits, "\n".join(hits)


def test_the_credential_grep_catches_a_planted_read(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text('tok = json.loads((d / ".credentials' + '.json").read_text())["claudeAi' + 'Oauth"]\n')
    assert len(credential_reads(pkg)) == 2
