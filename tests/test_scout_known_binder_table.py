"""The known-binders table must not lie about how much of the list it shows.

Why this file exists
--------------------

Raising the RCSB page size off 40 (see ``scout/epitope_db.py``'s
``_RCSB_PROBE_LIMIT``) fixed known-binder RECALL and, in doing so, changed what
this table receives. SARS-CoV-2 spike goes from ~16 structures to 1340,
measured 2026-09-04. Nothing downstream bounded the rendering: no
``max-height`` rule exists for it in ``static/scout.css``, there is no
pagination, and the block appended one ``<tr>`` per entry.

So the table now renders a bounded slice. That is safe only while it SAYS it is
a slice — a table that silently shows 100 of 1340 is the same species of bug as
the one the page-size fix closed: a truncation nobody can see. The count label
is therefore the property under test here, not the row cap.

The rule lives in JavaScript, where no Python assertion can reach it, and this
repo's convention (``tests/test_scout_refusal_cta.py``,
``tests/test_hotspot_picker_runtime.py``) is to lift the real block out of the
shipped template and run it under node against a stubbed DOM rather than
regex the template source — a substring match cannot tell a live rule from a
comment describing one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "templates" / "scout" / "index.html"
HARNESS = REPO_ROOT / "tests" / "js" / "scout_known_binders_harness.cjs"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

# Both anchors are asserted below, so renaming or moving either fails loudly
# here instead of silently testing an empty string.
_START = "    function renderKnownBinders(binders, epitopes) {"
_END = "    function showSpinner("


def _render_block() -> str:
    src = TEMPLATE.read_text(encoding="utf-8")
    start = src.find(_START)
    assert start != -1, f"{_START!r} not found — did renderKnownBinders move?"
    end = src.find(_END, start + 1)
    assert end != -1, f"{_END!r} not found — did the block move?"
    block = src[start:end]
    assert "{{" not in block and "{%" not in block, "Jinja leaked into the block"

    # The DOM stub auto-creates unknown ids, which keeps the harness robust to
    # upstream additions but would also let a renamed element stay green here
    # while throwing at page load in a real browser.
    ids = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", block))
    missing = sorted(i for i in ids if f'id="{i}"' not in src)
    assert not missing, f"block reads elements the page does not define: {missing}"
    return block


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict:
    block = _render_block()
    js = tmp_path_factory.mktemp("scoutjs") / "known_binders.js"
    js.write_text(block, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(HARNESS), str(js)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# The property that matters
# ---------------------------------------------------------------------------

def _contact_cell(row_html: str) -> str:
    """The Contact residues cell's text, tags stripped.

    Asserted exactly rather than by substring, so that an embellishment of
    the label fails here instead of shipping.
    """
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    assert len(cells) == 7, f"expected 7 cells, got {len(cells)}: {row_html}"
    cell = cells[5]
    # Attributes are searched, not stripped. Tag-stripping alone let the same
    # retracted claim survive as a title= tooltip -- user-visible on hover,
    # and moving a claim into a tooltip is a live pattern in this repo.
    for banned in ("none found", "cutoff", "measured", "looked"):
        assert banned not in cell.lower(), (
            f"the contact cell claims a measurement via {banned!r}: {cell}"
        )
    return re.sub(r"<[^>]+>", "", cell).strip()


@needs_node
def test_a_truncated_table_says_how_many_it_is_hiding(results):
    """THE property. A silent slice would re-create the bug upstream just fixed.

    The label must carry the TRUE total, not the number of rows drawn, so a
    reader can tell "this target has 1340 known binders, you are seeing 100"
    from "this target has 100 known binders".
    """
    huge = results["huge"]
    assert huge["rendered"] < huge["requested"], "expected the table to truncate"
    assert str(huge["requested"]) in huge["countText"], (
        f"count text {huge['countText']!r} does not mention the real total "
        f"{huge['requested']} — the table is hiding rows silently"
    )
    assert str(huge["rendered"]) in huge["countText"]


@needs_node
def test_an_untruncated_table_does_not_claim_to_be_truncated(results):
    """The complement. A count that always says "showing N of M" would pass the
    test above while being wrong for every ordinary target."""
    small = results["small"]
    assert small["rendered"] == small["requested"] == 12
    assert "showing" not in small["countText"].lower()
    assert small["countText"] == "12 structures"


@needs_node
def test_the_row_count_is_bounded(results):
    """Whatever the cap is, a 1340-entry answer must not become 1340 rows."""
    assert results["huge"]["rendered"] <= 100
    assert results["exactly_cap"]["rendered"] == 100
    assert results["one_over_cap"]["rendered"] == 100


@needs_node
def test_the_boundary_does_not_truncate_one_row_early(results):
    """An off-by-one here would hide a row and, worse, label the table as
    truncated when it is not."""
    assert "showing" not in results["exactly_cap"]["countText"].lower()
    assert "showing" in results["one_over_cap"]["countText"].lower()
    assert "101" in results["one_over_cap"]["countText"]


@needs_node
def test_truncation_keeps_the_useful_end_of_the_list(results):
    """``query_sabdab`` sorts by resolution and only the first few structures
    carry contact residues, so the slice must keep the HEAD. Keeping the tail
    would drop every row that has anything to show — which is the shape of the
    original bug, where the 40 kept were the wrong 40."""
    ids = results["huge"]["renderedIds"]
    assert ids[0] == "X000", f"first rendered row is {ids[0]!r}, not the best-ranked"
    assert ids[:3] == ["X000", "X001", "X002"]


@needs_node
def test_rows_without_contacts_are_not_labelled_pending(results):
    """Nothing is on its way. Contacts are computed server-side before the
    reply is built, and only for the best ``_MAX_CONTACT_STRUCTURES``, so
    "pending" promised work that never arrives -- and at 1340 rows with 5
    computed it would have been the dominant claim on screen.
    """
    huge = results["huge"]
    assert "pending" not in huge["lastRowHtml"], "a finished row still says 'pending'"
    assert "not computed" in huge["lastRowHtml"]
    # And the rows that DO have contacts still render them.
    assert "not computed" not in huge["firstRowHtml"]
    assert "10, 11, 12" in huge["firstRowHtml"]


@needs_node
def test_a_settled_empty_interface_is_not_called_not_computed(results):
    """THE distinction, and the one this table used to get backwards.

    The template read ``b.contact_residues || []``, which collapses an ABSENT
    ``contact_residues`` with a PRESENT empty one, and then told the user
    about the interface on the strength of it.

    Scope it honestly: only the first ``_MAX_CONTACT_STRUCTURES`` entries are
    ever given the key, so at most a handful of rows per render carry a
    present []. They are the top-ranked ones, which is what a reader looks
    at first.

    What each shape MEANS is written down once, in ``scout.epitope_db``'s
    module docstring, and is deliberately not paraphrased here -- paraphrases
    of it in this file and in the template are what went stale and shipped
    two successive false labels. The property under test is only that the
    two shapes render differently and that neither label claims a
    measurement the server did not make.

    The cell text is asserted EXACTLY. An earlier version forbade only the
    literal "none found", and QC showed that relabelling the cell "no
    contacts recorded within the 4.5 A cutoff" -- re-introducing the very
    claim that wording was removed for -- kept the file green.
    """
    assert _contact_cell(results["computed_empty"]["firstRowHtml"]) == (
        "no contacts recorded"
    )


@needs_node
def test_an_unestablished_interface_is_still_labelled_not_computed(results):
    """The complement: the two shapes must not collapse the other way either.

    Relabelling every row "no contacts recorded" would satisfy the test above
    while telling the reader the server had settled rows it has not touched.
    For a well-studied target that is nearly every row, since only the first
    few entries are ever given the key at all.
    """
    assert _contact_cell(results["never_computed"]["firstRowHtml"]) == (
        "not computed"
    )


@needs_node
def test_the_section_is_revealed(results):
    """Cheap regression guard: the block's last act is unhiding the panel."""
    assert results["single"]["sectionShown"] is True
    assert results["single"]["countText"] == "1 structure"


@needs_node
def test_a_null_resolution_row_still_renders(results):
    """``query_sabdab`` normalises NMR and unparseable resolutions to None,
    and the template guards that before calling ``.toFixed``.

    The guard was untested: the harness hard-coded 2.0, so deleting it left
    this file green while a real page threw on the first NMR structure and
    rendered no known-binder table at all. QC found it by deleting it.
    """
    sparse = results["sparse"]
    assert sparse["rendered"] == 2, "a null resolution stopped the render"
    assert "N/A" in sparse["lastRowHtml"], sparse["lastRowHtml"]
    # And the complement: a real resolution must still be shown. Without this,
    # hard-coding the cell to "N/A" passes -- every structure in the table
    # loses the number the list is SORTED by, and nothing notices.
    assert "2.0" in results["single"]["firstRowHtml"], (
        results["single"]["firstRowHtml"]
    )
    assert "N/A" not in results["single"]["firstRowHtml"]
