"""Property tests (hypothesis): the day-log line grammar, which every captured message goes through, and the
renderers an import feeds it. Rendering a line and parsing it back gives the same fields; parsing any text never
raises; a rendered line never holds a line break."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import configuration, given, settings
from hypothesis import strategies as st

from yerk_ai.memory import format_line, one_line, parse_line

configuration.set_hypothesis_home_dir(str(Path(tempfile.gettempdir()) / "yerk-hypothesis"))   # never a .hypothesis/ in the tree
settings.register_profile("yerk", database=None, derandomize=True, deadline=None, print_blob=True)   # (the scrub test walks it)
# the same examples on every run and box: CI=true gives that through hypothesis's own ci profile, which this one
# would otherwise replace on a box runner or a laptop with no CI variable (random examples, a 200 ms deadline)
settings.load_profile("yerk")

TOKEN = st.text(alphabet=st.characters(categories=("L", "N"), include_characters="-_."), min_size=1, max_size=16)
ROLE = st.from_regex(r"[A-Za-z0-9_-]{1,12}", fullmatch=True)
BODY = st.text(min_size=1, max_size=200).map(one_line).filter(lambda s: s and s == s.strip())
MID = st.from_regex(r"[A-Za-z0-9_.-]{1,24}", fullmatch=True)
AT = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2040, 1, 1)).map(
    lambda d: d.replace(tzinfo=timezone(timedelta(hours=-4))))


@settings(max_examples=300)
@given(at=AT, label=TOKEN, rid=TOKEN, role=ROLE, who=TOKEN, body=BODY, cls=st.sampled_from(["private", "room", "shared"]), mid=MID)
def test_render_then_parse_is_identity(at, label, rid, role, who, body, cls, mid):
    line = format_line(at, label, rid, role, who, body, cls, mid)
    assert "\n" not in line and "\r" not in line
    p = parse_line(line)
    assert p is not None, line
    assert (p.time, p.offset, p.label, p.rid, p.role, p.who, p.body, p.cls, p.mid) == \
        (at.strftime("%H:%M:%S"), "-04:00", label, rid, role, who, body, cls, mid)


@settings(max_examples=500)
@given(st.text(max_size=400))
def test_any_text_parses_or_is_no_line_and_never_raises(text):
    p = parse_line(text)
    assert p is None or p.body is not None


@settings(max_examples=300)
@given(st.text(max_size=400))
def test_one_line_never_emits_a_line_break_and_is_idempotent(text):
    out = one_line(text)
    assert "\n" not in out and "\r" not in out and one_line(out) == out
