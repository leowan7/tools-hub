"""The starred row must stage the design the user actually starred.

Every designs-shape results partial reshapes ``result["designs"]`` into the
candidate_table contract and then re-sorts by its headline metric, so the row
at screen position 0 is usually NOT ``designs[0]``. The shortlist posts an
index and ``blueprints/lab_projects.py`` uses it to subscript
``candidate_records(job.result)``, which is in raw pipeline order.

Those two orders only agree if the template stamps ``_source_index`` before it
sorts. Without the stamp, ``candidate_table.html`` falls back to
``loop.index0`` (the post-sort screen position) and the lab receives a
different structure from the one on screen, with a success email to match.

This was a live regression: before ``candidate_records`` was threaded through
the handoff, the designs shape staged ZERO files, so the failure mode was
silent-empty. Fixing that turned it into silent-wrong for every tool below.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from flask import render_template

from shared.jobs import candidate_records


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


# (tool, template, metric key on the raw design, ascending-in-pipeline values)
# The values are chosen so the pipeline order is the REVERSE of the rendered
# order: designs[0] is the worst design and must never land at screen row 0.
TOOLS = [
    ("boltz2", "iptm", [0.42, 0.67, 0.91]),
    ("af2", "mean_plddt", [55.0, 72.0, 93.0]),
    ("colabfold", "mean_plddt", [55.0, 72.0, 93.0]),
    ("esmfold", "mean_plddt", [55.0, 72.0, 93.0]),
    ("iggm", "n_epitope_contacts", [1, 4, 9]),
]


def _designs(metric_key, values):
    out = []
    for i, v in enumerate(values):
        d = {
            "rank": i,
            "name": f"design_{i}",
            "pdb_key": f"d{i}_complex.pdb",
            metric_key: v,
            "filter_status": "PASS",
        }
        if metric_key == "mean_plddt":
            # af2/colabfold/esmfold render pLDDT straight through.
            d["ptm"] = 0.5
            d["total_length"] = 100
        if metric_key == "iptm":
            d["complex_plddt"] = 0.8
            d["ptm"] = 0.5
        out.append(d)
    return out


def _rendered_ref_indices(html):
    """data-ref-idx values in rendered row order."""
    return [int(m) for m in re.findall(r'data-ref-idx="(\d+)"', html)]


@pytest.mark.parametrize("tool,metric_key,values", TOOLS)
def test_starred_row_maps_to_its_raw_pipeline_index(app, tool, metric_key, values):
    designs = _designs(metric_key, values)
    result = {"designs": designs, "designs_total": len(designs),
              "designs_completed": len(designs), "n_failures": 0}
    job = SimpleNamespace(
        id="job-abcdef12", tool=tool, status="succeeded", result=result,
        created_at=None, cost_usd=None,
    )

    with app.test_request_context(f"/jobs/{job.id}"):
        html = render_template(f"tools/{tool}_results.html", job=job, result=result)

    refs = _rendered_ref_indices(html)
    assert len(refs) == len(designs), f"{tool}: expected one row per design"

    # The table re-sorts best-first, so the rendered order must be the REVERSE
    # of the pipeline order for these fixtures. If this assertion fails the
    # fixture no longer exercises a re-sort and the test proves nothing.
    assert refs != list(range(len(designs))), (
        f"{tool}: rendered order matches pipeline order, so this fixture does "
        "not exercise the sort; pick metric values that reorder the rows"
    )
    assert refs == sorted(range(len(designs)), reverse=True), refs

    # The real invariant: every posted index resolves, through the SAME list
    # the handoff subscripts, to the design shown in that row.
    staged = candidate_records(result)
    for row, raw_idx in enumerate(refs):
        expected_pdb_key = designs[len(designs) - 1 - row]["pdb_key"]
        assert staged[raw_idx]["pdb_key"] == expected_pdb_key, (
            f"{tool}: row {row} would stage {staged[raw_idx]['pdb_key']}, "
            f"but the screen shows {expected_pdb_key}"
        )


def _esmfold2_designs(values):
    return [
        {"rank": i, "name": f"design_{i}", "pdb_key": f"d{i}.pdb",
         "iptm": v, "distogram_iptm_proxy": v, "cdr_distogram_iptm_proxy": v,
         "final_loss": 1.0, "isoelectric_point": 7.0, "filter_status": "PASS",
         "designed_sequence": "MKTAY", "sequence": "MKTAY"}
        for i, v in enumerate(values)
    ]


def _render_esmfold2(app, result):
    job = SimpleNamespace(id="job-abcdef12", tool="esmfold2_design",
                          status="succeeded", result=result,
                          created_at=None, cost_usd=None)
    with app.test_request_context(f"/jobs/{job.id}"):
        return render_template("tools/esmfold2_design_results.html",
                               job=job, result=result)


def test_esmfold2_design_canonical_branch_is_identity(app):
    """esmfold2_design persists BOTH ``designs`` and ``candidates``. When
    ``candidates`` is present the template renders it verbatim, with no sort,
    so screen position already equals the index ``candidate_records`` uses.
    Pin that: adding a sort to this branch without a stamp would mis-stage."""
    designs = _esmfold2_designs([0.30, 0.60, 0.90])
    candidates = [
        {"rank": d["rank"], "name": d["name"], "pdb_key": d["pdb_key"],
         "designed_sequence": d["designed_sequence"], "sequence": d["sequence"],
         "scores": {"ipTM": d["iptm"], "iPTM_proxy": d["distogram_iptm_proxy"],
                    "final_loss": d["final_loss"], "pI": d["isoelectric_point"],
                    "filter_status": d["filter_status"]}}
        for d in designs
    ]
    result = {"designs": designs, "candidates": candidates, "is_antibody": False,
              "designs_total": 3, "designs_completed": 3, "n_failures": 0}

    refs = _rendered_ref_indices(_render_esmfold2(app, result))
    staged = candidate_records(result)
    assert staged == candidates          # candidate_records prefers "candidates"
    assert refs == list(range(len(designs)))
    for row, raw_idx in enumerate(refs):
        assert staged[raw_idx]["pdb_key"] == candidates[row]["pdb_key"]


def test_esmfold2_design_fallback_branch_sorts_and_stamps(app):
    """Legacy rows carry only ``designs``. That branch DOES re-sort, so it
    needs the stamp, and ``candidate_records`` then returns ``designs``."""
    designs = _esmfold2_designs([0.30, 0.60, 0.90])
    result = {"designs": designs, "is_antibody": False,
              "designs_total": 3, "designs_completed": 3, "n_failures": 0}

    refs = _rendered_ref_indices(_render_esmfold2(app, result))
    staged = candidate_records(result)
    assert staged == designs
    assert refs == [2, 1, 0], refs      # sorted by ipTM desc, so reversed
    for row, raw_idx in enumerate(refs):
        assert staged[raw_idx]["pdb_key"] == designs[len(designs) - 1 - row]["pdb_key"]
