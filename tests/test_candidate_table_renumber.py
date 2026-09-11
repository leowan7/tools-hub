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


def test_sort_table_actually_calls_renumber_rows():
    """The call site, which everything else here leaves unguarded.

    The tests below lift ``renumberRows`` out and drive it in isolation, and
    the hook contract in tests/test_candidate_table_js_contract.py matches
    ``'.cand-rank-n'`` as a token -- which appears inside renumberRows' own
    body. So deleting the single line that CALLS it leaves the original
    defect back on screen under a green suite: with that line removed and
    this test not yet written, the 69 tests then covering this behaviour --
    both this file and test_candidate_table_js_contract.py, plus
    test_worked_examples.py::TestTheRankColumnIsAPosition -- all passed.

    Compared line-wise against a stripped form rather than by substring, so
    a comment mentioning renumberRows cannot satisfy it -- this file's own
    neighbours are full of such mentions.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    sort_at, renumber_at = src.find(_SORT), src.find(_START)
    assert sort_at != -1, f"{_SORT!r} not found — did sortTable move?"
    assert renumber_at != -1, f"{_START!r} not found — did renumberRows move?"
    assert sort_at < renumber_at, (
        "renumberRows now precedes sortTable; this slice assumes the order "
        "and would read the wrong region"
    )
    body = src[sort_at:renumber_at]
    lines = [
        ln.strip() for ln in body.splitlines()
        if not ln.strip().startswith("//")
    ]
    assert "renumberRows(tbody);" in lines, (
        "sortTable reorders the rows and never renumbers them, so a column "
        "sort leaves the # column holding its pre-sort numbers"
    )
    # AFTER the rows move, not before -- renumbering the old order is a
    # no-op that would leave the same stale column behind.
    assert lines.index("renumberRows(tbody);") > lines.index("pairs.forEach(function (p) {"), (
        "renumberRows runs before the rows are re-appended, so it numbers "
        "the pre-sort order"
    )


def _renumber_source() -> str:
    """``renumberRows`` verbatim from the shipped script.

    Both anchors are asserted, so renaming or moving the function fails here
    loudly instead of silently running an empty string -- an empty slice would
    make every assertion below pass against a function that does nothing.
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
