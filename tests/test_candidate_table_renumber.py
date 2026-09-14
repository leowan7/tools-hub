"""The "#" column follows a column sort, because it is a position.

``templates/components/candidate_table.html`` numbers candidate rows by their
position in the table rather than by ``cand.rank``, which is a payload field
the tools do not agree on. That makes the column a claim about WHERE a row is,
and ``static/js/candidate_table.js``'s ``sortTable`` reorders rows in the
browser without the server seeing it.

Before ``renumberRows`` existed, the cells kept their server-rendered text
through that reorder: one click on a metric header left the first row of a
column headed "#" reading "10". Nothing in Python could see it -- the defect
is entirely in the browser, after a click -- so this lifts the real function
out of the shipped script and runs it under node against a stubbed DOM, the
same way ``tests/test_scout_refusal_cta.py`` runs the real refusal block.

``tests/test_candidate_table_js_contract.py`` pins the other half: that the
macro still emits the ``.cand-rank-n`` span this writes to, and the
``.cand-group-row`` it refuses to renumber across. A rename on either side
makes the column silently keep its pre-sort numbers, which is exactly the
failure this file exists for.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "static" / "js" / "candidate_table.js"
HARNESS = REPO_ROOT / "tests" / "js" / "candidate_table_renumber_harness.cjs"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_SORT = "  function sortTable(table, col, dir) {"
_START = "  function renumberRows(tbody) {"
_END = "\n  }"
# The re-append loop that renumberRows has to run AFTER, and the call itself.
_APPEND = "pairs.forEach(function (p) {"
_CALL = "renumberRows(tbody);"
# sortTable's closing brace as a WHOLE LINE; see the slice in the test below.
_END_LINE = "  }"


def test_sort_table_actually_calls_renumber_rows():
    """The call site. Every other test here drives ``renumberRows`` in
    isolation, so deleting the one line that CALLS it left the suite green
    with the stale column back on screen.

    Anchors are COUNTED, not merely required to be present: ``.index()`` takes
    the first match, so a decoy ``pairs.forEach`` ahead of the call satisfied
    the ordering check while the real re-append ran after it. The body is
    comment-stripped first, so an ordinary commented-out call does not count
    (see the truncation trap at the slice below for when that stops holding).

    THIS READS TEXT, so it cannot see whether the call runs. Shapes that pass
    here and leave the stale column on screen include a never-true flag around
    the call, an uncalled nested helper holding it, unreachable code after an
    early ``return``, and a local shadow of ``renumberRows``. Others pass and
    break something else instead -- ``setTimeout`` renumbers a tick late with
    the column correct, and deleting the re-append's ``appendChild`` lines
    leaves the numbers right while no row moves. Deliberately not a closed
    list, but not unbounded either: this pins the line's PRESENCE, its
    POSITION after the re-append, and both anchor COUNTS at one, so a call
    moved above the re-append still fails on order, and a wrapper that repeats
    the anchor LINE ITSELF still fails on count. Both anchors are exact
    stripped-line matches, so a wrapper whose own loop is spelled any other
    way -- ``[].forEach``, ``pending.forEach`` -- leaves the count at one and
    gets through. Separating those needs the harness to drive the real
    ``sortTable``, whose DOM stub has no ``appendChild``, ``dataset`` or
    ``[data-col=]``.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    sort_at, renumber_at = src.find(_SORT), src.find(_START)
    assert sort_at != -1, f"{_SORT!r} not found — did sortTable move?"
    assert renumber_at != -1, f"{_START!r} not found — did renumberRows move?"
    assert sort_at < renumber_at, (
        "renumberRows now precedes sortTable; this slice assumes the order "
        "and would read the wrong region"
    )
    # A LINE equal to the closing brace, not a substring search for it: the
    # substring is a prefix of "  });" and "  }," too, so it can end the slice
    # on an inner closer. sortTable has exactly one line equal to _END_LINE.
    #
    # RESIDUAL TRAP. A bare inner "}" at two spaces is indistinguishable from
    # the real closer, so the slice ends early and drops whatever follows.
    # The LINE LIST only shrinks -- but the counts below are taken after the
    # /* */ strip, and that step is not monotone: truncating away a "*/"
    # orphans its "/*", the strip stops firing, and text it would have deleted
    # survives as live lines. So the brace hides a surplus AND can manufacture
    # one. Measured: a hidden real re-append passes (APPEND 2 -> 1), and a
    # block-commented call whose "*/" sits behind the brace comes back CALL=1
    # and passes with ZERO real calls, both on a page whose rows move while
    # the numbers do not. A "//"-commented call, a deleted call and a renamed
    # anchor all still fail.
    #
    # Only the SURPLUS half is specific to counting: the manufactured call
    # fools a membership test just as well, since the orphaned "/*" resurrects
    # a line that `in` also finds. Nor is the strip to blame -- run it WITHOUT
    # the closer bound and it catches this shape (CALL=0). Measured as a
    # factorial over the two steps: no bound + no strip PASS -- which is the
    # guard exactly as #257 shipped it, and this file did not exist before
    # that -- bound only PASS, strip only FAIL, both PASS. The bound is what
    # re-opens it, by truncating away the "*/" the strip needs.
    tail = src[sort_at:renumber_at].splitlines()
    closer = next(
        (i for i, ln in enumerate(tail) if ln.rstrip() == _END_LINE), None
    )
    assert closer is not None, (
        f"sortTable has no line equal to {_END_LINE!r} before renumberRows, "
        "so this slice cannot tell where its body ends"
    )
    body = "\n".join(tail[:closer])
    # Block comments as well as line-leading "//": a three-line /* */ has a
    # middle line that strips to exactly the target. Not string-aware, so a
    # "/*" AND a "*/" in string literals bracketing the call would delete it
    # and fail here on correct code -- a lone "/*" is inert, the regex needs
    # the pair. What keeps that theoretical is the file: the only /* */ pair
    # in candidate_table.js is its header.
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    lines = [
        ln.strip() for ln in body.splitlines()
        if not ln.strip().startswith("//")
    ]
    assert lines.count(_CALL) == 1, (
        f"sortTable contains {lines.count(_CALL)} lines equal to {_CALL!r}; "
        "expected exactly one. At zero it reorders the rows and never "
        "renumbers them, so a column sort leaves the # column holding its "
        "pre-sort numbers; above one, the ordering check below reads "
        "whichever comes first and stops meaning anything"
    )
    # AFTER the rows move, not before -- renumbering the old order is a no-op
    # that would leave the same stale column behind. Counted, not just
    # present: .index() on a missing anchor raises ValueError, which reports a
    # behaviour-preserving rename (the callback param, or an arrow function)
    # as a crash rather than as the mismatch it is.
    assert lines.count(_APPEND) == 1, (
        f"sortTable contains {lines.count(_APPEND)} lines equal to "
        f"{_APPEND!r}; expected exactly one. At zero the ordering check has "
        "no anchor; above one it anchors on the first, which a decoy loop "
        "ahead of the call satisfies while the real re-append runs after it"
    )
    assert lines.index(_CALL) > lines.index(_APPEND), (
        "renumberRows runs before the rows are re-appended, so it numbers "
        "the pre-sort order"
    )


def _renumber_source() -> str:
    """``renumberRows`` verbatim from the shipped script.

    Both anchors are asserted, so renaming or moving the function fails here
    by name instead of further down for a reason that reads like a behaviour
    change. Measured, if it did slip through: an EMPTY slice makes the harness
    die with ``ReferenceError: renumberRows is not defined``, so the fixture's
    returncode assert errors all four tests at once; a slice that parsed but
    did nothing would still be caught, though only by two of the four.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.find(_START)
    assert start != -1, f"{_START!r} not found — did renumberRows move?"
    end = src.find(_END, start + len(_START))
    assert end != -1, "renumberRows has no closing brace at function indent"
    block = src[start:end + len(_END)]
    assert "cand-rank-n" in block, "the slice lost the span selector"
    assert "cand-group-row" in block, "the slice lost the grouped guard"
    return block


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")
    block = tmp_path_factory.mktemp("renumber") / "renumber.js"
    block.write_text(_renumber_source(), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(block)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@needs_node
def test_a_sorted_table_is_renumbered_top_to_bottom(result):
    """The defect itself. Rows carrying 10,9,8 after a sort read 1,2,3."""
    assert result["descending_is_renumbered"] == ["1", "2", "3"]


@needs_node
def test_viewer_rows_neither_take_a_number_nor_consume_one(result):
    """An open 3D panel sits between two data rows. If it consumed a
    position, opening one would shift every number below it."""
    assert result["viewer_rows_skipped"] == ["1", "2", "3"]


@needs_node
def test_a_grouped_table_is_left_alone(result):
    """Grouped mode numbers with grp.n, which restarts at 1 in each tool
    block. Flattening that to 1..n would assert the single cross-tool
    ordering the table refuses to make (register item A82)."""
    assert result["grouped_untouched"] == ["1", "2", "1"]


@needs_node
def test_a_row_without_the_span_does_not_derail_the_count(result):
    """It must not throw, and it must still take its position -- the row
    after it is the third row and has to say so."""
    assert result["missing_span_still_counts"] == ["1", "3"]
