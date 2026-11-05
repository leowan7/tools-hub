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
from types import SimpleNamespace

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


def _render_with_inputs(flask_app, adapter, *, result: dict, inputs: dict) -> str:
    """``_render_partial`` with the job's ``inputs`` filled in.

    The shared helper stubs ``inputs`` as ``{}`` (tests/test_worked_examples.py:133)
    and takes no override, and the two assertions below are precisely about the
    inputs fallbacks, so the stub is rebuilt here rather than widening a helper
    eleven other tools share. An object, not a dict, because
    shared/jobs.py::recovered_total reads ``job.inputs`` as an ATTRIBUTE and
    swallows the AttributeError a dict raises — the stub's shape decides
    whether this file tests the fallback or a silent zero.
    """
    job = SimpleNamespace(id=JOB_ID, status="succeeded", tool="rfd4",
                          inputs=inputs, result=result)
    with flask_app.test_request_context("/tools/rfd4"):
        return flask_app.jinja_env.get_template(adapter.results_partial).render(
            job=job, example=False, send_target_tools=[])


def _recovered_result(rows: int) -> dict:
    """What a recovered / wallet-stopped / cancel-kept rfd4 run stores.

    Every one of those writers replaces the result wholesale and keeps no root
    key of this tool's own: shared/job_recovery.py:480-484,
    shared/jobs.py:2093-2100 and shared/jobs.py:2299-2303.
    """
    designs = _example()["designs"][:rows]
    return {
        "candidates": [
            {"rank": d["rank"], "pdb_key": d["pdb_key"], "scores": {}} for d in designs
        ],
        "candidate_count": rows,
        "partial": True,
    }


def test_a_recovered_run_still_says_which_chain_is_the_binder(
        tools_app, rfd4_registered):
    """The chain line is the page's ONLY statement of which chain to keep, and
    test_the_designed_chain_is_not_the_longest_chain proves length cannot
    substitute for it. A recovered result carries no ``target_chain``, so it
    comes from the run's inputs instead.

    Mutation-proven 2026-11-04: with the partial's expression reverted to
    ``output.get('target_chain')`` alone, this test FAILS; restored, it passes.
    """
    flask_app, _ = tools_app
    html = _render_with_inputs(
        flask_app, rfd4_registered,
        result=_recovered_result(2),
        inputs={"target_chain": "A", "nsamples": 8,
                "_progress": {"designs_completed": 2, "designs_total": 8}})
    text = _page_text(html)
    assert "Target chain A" in text
    assert "the chain that is not A" in text


def _inputs(**extra) -> dict:
    """A job's inputs in the shape the adapter stores them.

    ``nsamples`` is tools/rfd4/__init__.py:200 and ``parameters.n_designs_total``
    is :209 — both the accepted design count, the latter the key the wallet
    scales the charge on. There is no ``num_designs`` key in inputs: :176 is
    the FORM field, read once and not kept under that name.
    """
    return {"target_chain": "A", "nsamples": 8,
            "parameters": {"n_designs_total": 8}} | extra


def test_a_lossy_run_says_how_many_designs_are_missing(tools_app, rfd4_registered):
    """M is the submitted count, so a shortfall shows however the designs were
    lost — including a generation shortfall, where the result root's
    ``designs_total`` is itself only ``len(design_cifs)``
    (tools/rfd4/run_pipeline.py:1290) and would render "6 / 6".

    ``requested_designs`` cannot supply it either: no _DESIGN_PARAM_KEY entry
    for rfd4, so it returns None and results_shell.html:49 drops the line.

    Mutation-proven 2026-11-04, this test failing on each revert and passing
    when restored: ``requested=requested_designs(job)`` fails at the first
    assertion (None drops the line outright), and dropping the ``submitted``
    term from the total fails at the second, rendering "6 / 6".
    """
    flask_app, _ = tools_app

    # Lost between CIF and delivery: eight generated, five uploaded.
    full = _example() | {"designs": _example()["designs"][:5]}
    html = _render_with_inputs(
        flask_app, rfd4_registered, result=full, inputs=_inputs())
    assert "5 / 8" in _page_text(html)

    # Generation shortfall: six CIFs written, all six delivered, billed for 8.
    short = _example() | {"designs": _example()["designs"][:6],
                          "designs_total": 6, "designs_completed": 6}
    html = _render_with_inputs(
        flask_app, rfd4_registered, result=short, inputs=_inputs())
    text = _page_text(html)
    assert "6 / 8" in text and "6 / 6" not in text, text[:400]

    # No root keys left at all.
    html = _render_with_inputs(
        flask_app, rfd4_registered,
        result=_recovered_result(2),
        inputs=_inputs(_progress={"designs_completed": 2, "designs_total": 8}))
    assert "2 / 8" in _page_text(html)


def _page_text(html: str) -> str:
    """``html`` with tags and runs of whitespace collapsed, so an assertion on
    "5 / 8" is not defeated by the markup inside the <strong>."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


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
