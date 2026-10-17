"""The scrub test: no personal data or secrets anywhere in the repository."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "build", "dist"}
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


def repo_files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts):
            yield p


def scan(path: Path, rel: str) -> list[str]:
    text = path.read_text(errors="ignore")
    hits = [f"{rel}: {name}: {m.group()[:40]}" for name, rx in PATTERNS.items() for m in rx.finditer(text)]
    if not rel.startswith(HOME_OK):
        hits += [f"{rel}: home path: {m.group()}" for m in HOME.finditer(text)]
    return hits


def test_repository_is_clean():
    hits = []
    for p in repo_files():
        rel = p.relative_to(ROOT).as_posix()
        if rel != "tests/test_scrub.py":
            hits += scan(p, rel)
    assert not hits, "\n".join(hits)


def test_the_scrub_catches_what_it_should(tmp_path):
    bad = tmp_path / "leak.md"
    bad.write_text("mail someone@example.com, id 123456789012345678, call 416-555-0199, "
                   "key sk-ant-" + "a" * 24 + ", path /home/someone/x")
    kinds = {h.split(": ")[1] for h in scan(bad, "docs/leak.md")}
    assert kinds == {"email", "discord-id-shaped number", "phone number", "secret", "home path"}
