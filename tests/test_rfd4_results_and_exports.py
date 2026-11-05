"""rfd4's results page and its exports (RFD4-Proteina step 5).

HOW THE PARITY GATE IS EXERCISED FOR AN UNREGISTERED TOOL. rfd4 ships
unregistered on purpose — ``tools/rfd4/__init__.py`` builds its adapter and
never calls ``register``, so ``tools.base.get("rfd4")`` is None and
``blueprints/tools.py::_require_tool`` 404s every rfd4 URL. The shared parity
driver in ``tests/test_export_page_parity.py`` resolves a tool's partial through
``tools.base.get`` (``tests/test_worked_examples.py:190``), so rfd4 cannot be
added to its ``TABLE_TOOLS`` tuple without registering the tool. This file
instead injects the adapter into ``tools.base._REGISTRY`` for the duration of
one test and reuses that driver's own helpers, so the page-vs-CSV assertion is
the same assertion the other eleven tools get, run against shipped code that
still has rfd4 unregistered.

The structure assertions read the committed rung-4 GPU run,
``docs/scoping/rung4-probe/gpu_result_20261103T114437.json`` (rc=0), so they are
pinned to real upstream output rather than a hand-written CIF.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from pathlib import Path

import pytest

from tests.test_export_page_parity import JOB_ID, _num, _page_rows, _stored
from tests.test_worked_examples import _render_partial, tools_app  # noqa: F401

pytestmark = pytest.mark.usefixtures("isolate_supabase")

REPO = Path(__file__).resolve().parent.parent
PROBE = REPO / "docs/scoping/rung4-probe/gpu_result_20261103T114437.json"


@pytest.fixture
def rfd4_registered(monkeypatch):
    """rfd4 in the registry for this test only; shipped code keeps it out."""
    from tools import base as tool_base
    import tools.rfd4 as rfd4

    assert tool_base.get("rfd4") is None, (
        "rfd4 is registered in shipped code — registration is step 7A, and this "
        "fixture's whole point is that it is not there yet"
    )
    monkeypatch.setitem(tool_base._REGISTRY, "rfd4", rfd4.adapter)
    return rfd4.adapter


def _probe_cifs() -> dict[str, str]:
    """Every design CIF the committed rung-4 run wrote, keyed by file stem."""
    files = json.loads(PROBE.read_text())["files"]
    return {
        Path(path).stem: text
        for path, text in files.items()
        if path.endswith("_generated.cif")
    }


def _example() -> dict:
    return json.loads((REPO / "tools/rfd4/example/result.json").read_text())


def test_the_probe_still_holds_the_eight_design_cifs():
    """Otherwise every structure assertion below passes over an empty set."""
    cifs = _probe_cifs()
    assert len(cifs) == 8, sorted(cifs)


def test_csv_rows_are_the_page_rows_in_page_order(tools_app, rfd4_registered):
    """The step-5 export parity gate, run the way the eleven registered tools
    get it in tests/test_export_page_parity.py."""
    from shared.exports import candidates_to_csv
    from shared.jobs import page_ordered_records

    flask_app, _ = tools_app
    result = _example()
    page = _page_rows(_render_partial(
        flask_app, "rfd4", job_id=JOB_ID, example=False, result=result))
    assert page, "rfd4: the page rendered no candidate rows"

    stored = _stored(result)
    exported = page_ordered_records("rfd4", result)
    # Identity, not equality: two designs can share every value.
    export_refs = [next(i for i, s in enumerate(stored) if s is r) for r in exported]
    assert export_refs == [p["ref"] for p in page]

    csv_rows = list(csv.DictReader(io.StringIO(candidates_to_csv(exported))))
    assert len(csv_rows) == len(page)
    for n, (p, c) in enumerate(zip(page, csv_rows), start=1):
        assert c["rank"] == str(n)
        csv_nums = [x for x in map(_num, c.values()) if x is not None]
        for col, val in p["cells"].items():
            want = _num(val)
            if want is None:
                continue
            assert any(math.isclose(want, x, rel_tol=1e-3, abs_tol=1e-3)
                       for x in csv_nums), f"row {n}: {col}={val} not in CSV {c}"


def test_the_page_offers_the_cif_extension_not_pdb(tools_app, rfd4_registered):
    """rfd4 delivers mmCIF. The download cell derives its label from the key's
    own extension (templates/components/candidate_table.html), so a page that
    advertised ``.pdb`` would be mislabelling every file."""
    flask_app, _ = tools_app
    html = _render_partial(
        flask_app, "rfd4", job_id=JOB_ID, example=False, result=_example())
    labels = set(re.findall(
        r'class="btn-secondary cand-pdb-download"[^>]*>\.(\w+)<', html))
    assert labels == {"cif"}, labels


def test_the_cif_export_path_reads_both_chains(tools_app):
    """``structure_chain_sequences`` sniffs ``data_`` and parses mmCIF, so the
    sequence columns carry a chain for the target AND the binder on real
    upstream output."""
    from shared.exports import structure_chain_sequences

    for stem, text in sorted(_probe_cifs().items()):
        chains = structure_chain_sequences(text.encode("utf-8"))
        assert chains is not None, f"{stem}: did not parse"
        assert {cid for cid, _ in chains} == {"A", "B"}, f"{stem}: {chains}"
        assert all(len(seq) > 20 for _, seq in chains), f"{stem}: {chains}"


def test_the_designed_chain_is_not_the_longest_chain(tools_app):
    """A length rule would pick the wrong chain on real output, so nothing may
    reintroduce one: in this run design 4's binder is LONGER than the target.

    The target is chain A in all eight (115 aa, the uploaded PDL1 chain); the
    binder is chain B, which upstream's own ``per_example_metrics.csv`` names as
    ``chain_of_interest = B_1``. Selection belongs to ``target_chain`` at the
    result root — see templates/tools/rfd4_results.html.
    """
    from shared.exports import structure_chain_sequences

    lengths = {
        stem: {cid: len(seq) for cid, seq in
               structure_chain_sequences(text.encode("utf-8")) or []}
        for stem, text in _probe_cifs().items()
    }
    assert {d["A"] for d in lengths.values()} == {115}, lengths
    longest_is_target = [s for s, d in lengths.items() if d["A"] > d["B"]]
    assert len(longest_is_target) == 7, lengths
    assert lengths["pdl1_binder_4_generated"]["B"] > 115, lengths
