"""Three streamed scores a kept design loses on the way to the customer.

THE DEFECT. A design that streams during a run and is then kept by a cancel or
a recovery reaches the page and the exports through three sequential gates:

1. ``webhooks/modal.py::_sanitize_candidate`` -- the allowlist that projects a
   heartbeat candidate into the partial stored in ``inputs._partial_candidates``.
2. ``shared/job_recovery.py::_candidate_from_partial`` -- the rebuild that turns
   a stored partial back into a ``result.candidates`` row.
3. ``shared/jobs.py::recovered_columns`` -- the column list the six
   designs-native partials switch to when they render a rebuilt list.

``n_hotspots`` (boltz2, tools/boltz2/run_pipeline.py:888), ``mean_plddt`` (af2
:1357, colabfold :1396, esmfold :1094) and ``ranking_score`` (opendde :535) are
streamed by those pipelines and were dropped: the first two at gate 1, all
three at gate 2, and ``mean_pLDDT``/``ranking_score`` again at gate 3 -- where
``mean_pLDDT`` is each fold tool's first column AND its sort key, and
``ranking_score`` is opendde's first column.

Every test here drives the real gate functions, in order, over the real
``design_entry`` dict its pipeline streams. The two
``still_drops``/``still_omits`` tests are the other direction: a gate that
passed everything would satisfy the rest of this file.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_CELL = re.compile(r'data-col="([^"]+)" data-val="([^"]*)"')

# The streamed dicts, copied from the pipelines named in the module docstring.
# Trimmed to the keys a gate can carry (the pipelines also stream
# runtime_seconds / total_aa / model_preset, which no gate has ever kept).
BOLTZ2 = {"rank": 1, "name": "d1", "pdb_key": "designs/d1.pdb", "iptm": 0.8,
          "ptm": 0.7, "complex_plddt": 0.85, "complex_iplddt": 0.8,
          "n_hotspot_contacts": 3, "n_hotspots": 5}
AF2 = {"rank": 1, "name": "d1", "pdb_key": "designs/d1.pdb",
       "mean_plddt": 91.5, "iptm": 0.8, "ptm": 0.7}
OPENDDE = {"rank": 1, "name": "d1", "pdb_key": "designs/d1.pdb",
           "ranking_score": 0.87, "ptm": 0.7, "iptm": 0.8, "plddt": 0.9}


@pytest.fixture(scope="module")
def flask_app(isolate_supabase_module):
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _render(flask_app, tool, result, inputs=None):
    from flask import render_template

    job = SimpleNamespace(id="job-1", tool=tool, status="succeeded",
                          inputs=inputs or {}, result=result)
    with flask_app.test_request_context("/jobs/job-1"):
        return render_template("tools/%s_results.html" % tool, job=job,
                               send_target_tools=None)


def _through_gates(streamed):
    """Gate 1 then gate 2, the way a kept design really travels."""
    from shared.job_recovery import _candidate_from_partial
    from webhooks.modal import _sanitize_candidate

    part = _sanitize_candidate(streamed)
    assert part is not None
    return part, _candidate_from_partial(part)


# ---------------------------------------------------------------------------
# Gate 1: the webhook allowlist
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("streamed,key,value", [
    (BOLTZ2, "n_hotspots", 5),
    (AF2, "mean_plddt", 91.5),
    (OPENDDE, "ranking_score", 0.87),
])
def test_the_sanitizer_keeps_the_score_its_pipeline_streams(streamed, key, value):
    from webhooks.modal import _sanitize_candidate

    part = _sanitize_candidate(streamed)
    assert part.get(key) == value, (
        "%s left the webhook allowlist as %r; the pipeline streamed %r"
        % (key, part.get(key), value)
    )


def test_the_sanitizer_still_drops_an_unknown_key():
    """The other direction. _sanitize_candidate bounds an unauthenticated
    payload, so a key no pipeline streams must not reach the inputs jsonb."""
    from webhooks.modal import _sanitize_candidate

    part = _sanitize_candidate(dict(BOLTZ2, smuggled="<script>", plddt=[1, 2]))
    assert "smuggled" not in part
    assert part["plddt"] is None


@pytest.mark.parametrize("raw,expected", [
    (-4, 0), ("7", 7), ("many", None), (None, None), (2.9, 2)])
def test_the_sanitizer_bounds_n_hotspots(raw, expected):
    """A count, not a measurement: non-negative int or nothing, like its
    sibling n_hotspot_contacts."""
    from webhooks.modal import _sanitize_candidate

    assert _sanitize_candidate(dict(BOLTZ2, n_hotspots=raw))["n_hotspots"] == expected


# ---------------------------------------------------------------------------
# Gate 2: the rebuild
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("streamed,key,value", [
    (AF2, "mean_pLDDT", 91.5),
    (OPENDDE, "ranking_score", 0.87),
])
def test_the_rebuild_carries_the_score_into_the_candidate(streamed, key, value):
    _, cand = _through_gates(streamed)
    assert cand["scores"].get(key) == value, (
        "the rebuilt candidate's scores are %r; %s is missing"
        % (cand["scores"], key)
    )


def test_the_rebuild_puts_n_hotspots_where_the_page_reads_it():
    """boltz2's against-bar denominator is ``c.n_hotspots`` at the record root
    (templates/tools/boltz2_results.html:191), not a scores entry."""
    _, cand = _through_gates(BOLTZ2)
    assert cand.get("n_hotspots") == 5
    assert "n_hotspots" not in cand["scores"]


# ---------------------------------------------------------------------------
# Gate 3: the recovered column list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("streamed,key", [
    (AF2, "mean_pLDDT"), (OPENDDE, "ranking_score")])
def test_the_recovered_column_list_names_the_score(streamed, key):
    from shared.jobs import recovered_columns

    _, cand = _through_gates(streamed)
    cols = recovered_columns([cand])
    assert key in cols, "recovered_columns returned %r; %s is missing" % (cols, key)


def test_the_recovered_column_list_still_omits_what_a_row_lacks():
    """The other direction: the list is the keys PRESENT, so a row without
    these scores must not gain a column of dashes."""
    from shared.jobs import recovered_columns

    assert recovered_columns([{"scores": {"ipTM": 0.9}}]) == ["ipTM"]


# ---------------------------------------------------------------------------
# What the customer sees
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool,streamed,col,val", [
    ("af2", AF2, "mean_pLDDT", "91.5"),
    ("colabfold", AF2, "mean_pLDDT", "91.5"),
    ("opendde", OPENDDE, "ranking_score", "0.87"),
])
def test_a_kept_design_shows_the_score_on_its_results_page(
        flask_app, tool, streamed, col, val):
    _, cand = _through_gates(streamed)
    result = {"candidates": [cand], "candidate_count": 1, "backfilled": True}
    cells = set(_CELL.findall(_render(flask_app, tool, result)))
    assert any(c == col and v.startswith(val) for c, v in cells), (
        "%s: the kept design's %s is not on the page; cells: %r"
        % (tool, col, sorted(cells))
    )


def test_a_kept_boltz2_design_counts_its_contacts_against_the_real_total(flask_app):
    """The against-bar read "3 / <the hotspots the operator asked for>" on a
    kept design, because the rebuilt row carried no n_hotspots to divide by."""
    _, cand = _through_gates(BOLTZ2)
    html = _render(flask_app, "boltz2", {
        "candidates": [cand], "candidate_count": 1, "backfilled": True,
        "hotspots_requested": ["A:50", "A:51"]})
    assert "3 / 5" in html, "the against-bar denominator is not the run's n_hotspots"
