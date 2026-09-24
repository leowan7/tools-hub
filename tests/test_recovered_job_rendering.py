"""A recovered run must render the designs it actually produced -- and must not
claim they are ranked.

THE DEFECT. ``shared/job_recovery.recover_stuck_job_result`` finalizes a job
whose terminal webhook was lost. Whatever tool it recovers, it returns the same
dict: ``{"candidates": [...], "candidate_count": N, "backfilled": True}``. The
partials in ``DESIGNS_NATIVE`` below read ``output.get('designs', [])`` and
nothing else, so a recovered boltz2 run rendered as "Designs folded 0 / 0 ...
Candidates (0)  Boltz-2 returned no designs" over designs that are in Storage
and that ``/export.csv`` emits in full. ``shared/jobs.candidate_records`` has
read both shapes all along -- the CSV export and the lab handoff already route
through it (blueprints/jobs.py) -- and the partials were the holdout.

THE SECOND HALF, AND WHY THE FIX IS NOT JUST "ALSO READ candidates". A rebuilt
list is in ARRIVAL order: ``job_recovery.reconstruct`` replays streamed
partials in the order they were streamed and sorts nothing. So
``candidate_table.html``'s ``loop.first`` "Top-ranked design in this run"
badge, which reads the list's head as its winner, is now gated on a
``ranked=`` flag the partials pass. The badge half was ALREADY live for the six
candidates-native tools (bindcraft/boltzgen/rfdiffusion/pxdesign/rfantibody/
proteina), which have rendered recovered lists all along; they now pass the
same flag. The completion email's copy of the same claim is refused by
``shared/jobs.py::supports_headline_claim`` on the ``backfilled`` flag, held by
``test_a_recovered_run_gets_no_email_callout`` in
tests/test_job_complete_email_headline.py, and is not re-tested here.

Every test here asserts BOTH directions. A recovered-only assertion passes
against a partial that dropped the badge unconditionally.
"""
from __future__ import annotations

import os
import re
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

# Tools whose pipelines store per-design rows under "designs" AND whose
# partial read only that key. Verified against the partials' own reads (each
# sets `raw_designs = output.get('designs', ...)`) and by _native below, which
# is rendered through them. esmfold2_design also stores under "designs" but is
# NOT here: its partial has read `output.get('candidates')` first since before
# this change (esmfold2_design_results.html, `output_candidates`), which is why
# it was the one tool that already rendered a recovered run.
DESIGNS_NATIVE = ("boltz2", "af2", "colabfold", "esmfold", "iggm", "opendde")

# Tools whose pipelines store under "candidates" already. These render a
# recovered list today; only the ranking claim over it was wrong.
CANDIDATES_NATIVE = ("bindcraft", "boltzgen", "rfdiffusion", "pxdesign",
                     "rfantibody", "proteina")

# The RENDERED badge, never the bare class name: `cand-rank-top-badge` also
# matches the `.cand-rank-top-badge {` CSS rule in candidate_table.html, which
# every page carries.
# Counting that made "no badge" and "one badge" read as 1 and 2.
TOP_BADGE = re.compile(r'<span class="cand-rank-top-badge"')
CAND_ROW = re.compile(r'class="cand-row')

# Every partial's empty-state body is "<Tool> returned no <something>", and the
# something differs: "designs" (boltz2, iggm), "completed designs" (af2,
# colabfold, esmfold), "predicted structures" (opendde). Matching the shared
# stem rather than enumerating the three -- an enumeration would silently stop
# covering a tool that rewords its own sentence. Proven to actually fire by
# test_the_empty_state_matcher_fires_on_a_genuinely_empty_run.
EMPTY_STATE = re.compile(r"returned no\b", re.I)


@pytest.fixture(scope="module")
def flask_app(isolate_supabase_module):
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _recovered():
    """The EXACT dict shared/job_recovery.recover_stuck_job_result returns.

    Two rows, arrival order, the weaker one first -- the ordering that makes an
    unranked list distinguishable from a ranked one. Scores are nested under
    ``scores`` because ``job_recovery._candidate_from_partial`` builds them
    that way.
    """
    return {
        "candidates": [
            {"rank": 1, "pdb_key": "designs/d0.pdb",
             "scores": {"ipTM": 0.71, "pLDDT": 80.0}},
            {"rank": 2, "pdb_key": "designs/d1.pdb",
             "scores": {"ipTM": 0.95, "pLDDT": 91.0}},
        ],
        "candidate_count": 2,
        "backfilled": True,
    }


def _native(tool):
    """Two rows in the tool's own flat designs[] shape."""
    if tool == "boltz2":
        return {"designs": [
            {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0",
             "iptm": 0.71, "complex_plddt": 0.80},
            {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1",
             "iptm": 0.95, "complex_plddt": 0.91}]}
    if tool == "iggm":
        return {"designs": [
            {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0",
             "epitope_contacts": 7},
            {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1",
             "epitope_contacts": 3}]}
    if tool == "opendde":
        return {"designs": [
            {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0", "score": 0.9},
            {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1", "score": 0.5}]}
    return {"designs": [
        {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0",
         "mean_plddt": 80.0, "iptm": 0.71},
        {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1",
         "mean_plddt": 91.0, "iptm": 0.95}]}


def _render(flask_app, tool, result, inputs=None):
    from flask import render_template

    job = SimpleNamespace(id="job-1", tool=tool, status="succeeded",
                          inputs=inputs or {}, result=result)
    with flask_app.test_request_context("/jobs/job-1"):
        return render_template("tools/%s_results.html" % tool, job=job,
                               send_target_tools=None)


# ---------------------------------------------------------------------------
# The reported gap
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", DESIGNS_NATIVE)
def test_a_recovered_run_renders_the_designs_it_produced(tool, flask_app):
    html = _render(flask_app, tool, _recovered())
    rows = len(CAND_ROW.findall(html))
    assert rows == 2, (
        "%s: a recovered run carrying 2 designs rendered %d candidate rows. "
        "The rows are under result['candidates']; the partial read only "
        "result['designs']." % (tool, rows)
    )


@pytest.mark.parametrize("tool", DESIGNS_NATIVE)
def test_a_recovered_run_is_not_reported_as_empty(tool, flask_app):
    """The empty-state copy is the half a customer actually reads."""
    html = _render(flask_app, tool, _recovered())
    assert EMPTY_STATE.search(html) is None, (
        "%s: a recovered run with 2 designs still renders the "
        "returned-nothing copy" % tool
    )


# The three tools whose empty state is reachable from a result alone. af2,
# colabfold and esmfold put their empty copy INSIDE the batch branch, so an
# empty designs[] sends them down the single-fold path instead and the copy
# never renders; there is no result that shows it without a design.
EMPTY_STATE_REACHABLE = ("boltz2", "iggm", "opendde")


@pytest.mark.parametrize("tool", EMPTY_STATE_REACHABLE)
def test_the_empty_state_matcher_fires_on_a_genuinely_empty_run(tool, flask_app):
    """The other direction, and the only thing that makes the test above mean
    anything: a matcher that never matches would pass it against any page."""
    html = _render(flask_app, tool, {"designs": []})
    assert EMPTY_STATE.search(html) is not None, (
        "%s: EMPTY_STATE did not match a run that really produced nothing, so "
        "its absence proves nothing about a recovered one" % tool
    )


@pytest.mark.parametrize("tool", ("boltz2", "af2", "colabfold", "esmfold"))
def test_the_run_counter_counts_the_rendered_list(tool, flask_app):
    """"Designs folded 0 / 0" over a populated table is its own falsehood.

    A recovered result carries no ``designs_total``, so the counter falls back
    to a list length -- it was falling back to ``raw_designs``, the list the
    page is NOT rendering. Only the four partials that print an N / N counter
    are covered; iggm and opendde print none.
    """
    html = _render(flask_app, tool, _recovered())
    assert "0 / 0" not in html, (
        "%s: counter reads 0 / 0 over a 2-row recovered table" % tool
    )
    assert "2 / 2" in html, (
        "%s: counter does not report the 2 designs it is rendering" % tool
    )


@pytest.mark.parametrize("tool", ("boltz2", "af2", "colabfold", "esmfold"))
def test_a_lossy_recovery_shows_the_run_total(tool, flask_app):
    """Recovery skips a design whose structure never reached Storage.

    The rebuilt list is then shorter than the run, and "2 / 2" would present
    a run that lost designs as a complete one. The total comes from the
    heartbeat snapshot recovery was authorised on.
    """
    html = _render(flask_app, tool, _recovered(),
                   inputs={"_progress": {"designs_completed": 5, "designs_total": 5}})
    assert "2 / 5" in html, (
        "%s: a recovery that kept 2 of 5 designs does not say so" % tool
    )


# ---------------------------------------------------------------------------
# The ranking claim, both directions
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", DESIGNS_NATIVE + CANDIDATES_NATIVE)
def test_a_rebuilt_list_carries_no_top_badge(tool, flask_app):
    html = _render(flask_app, tool, _recovered())
    # Rows FIRST. "No badge" is satisfied by a page that renders no rows at
    # all, which is exactly what the six designs-native partials did before
    # this change -- so without this line the badge assertion below passes
    # against the very defect it is here to pin.
    assert len(CAND_ROW.findall(html)) == 2, (
        "%s: rendered no rows, so the badge assertion below is vacuous" % tool
    )
    assert TOP_BADGE.search(html) is None, (
        "%s: a rebuilt list is in arrival order (job_recovery.reconstruct "
        "sorts nothing), so the Top badge crowns whichever design finished "
        "first and its tooltip calls it 'Top-ranked design in this run'" % tool
    )


@pytest.mark.parametrize("tool", DESIGNS_NATIVE)
def test_a_normal_run_still_carries_its_top_badge(tool, flask_app):
    """The other direction. Without this, dropping the badge unconditionally
    passes every assertion above."""
    html = _render(flask_app, tool, _native(tool))
    assert len(TOP_BADGE.findall(html)) == 1, (
        "%s: a normal run lost the Top badge" % tool
    )
    assert len(CAND_ROW.findall(html)) == 2, (
        "%s: a normal run stopped rendering its designs" % tool
    )


@pytest.mark.parametrize("tool", CANDIDATES_NATIVE)
def test_a_normal_candidates_run_still_carries_its_top_badge(tool, flask_app):
    result = {"candidates": [
        {"rank": 1, "pdb_key": "designs/d0.pdb", "design_name": "d0",
         "sequence": "AAAA", "scores": {"ipTM": 0.95, "pLDDT": 91.0, "i_pae": 6.0}},
        {"rank": 2, "pdb_key": "designs/d1.pdb", "design_name": "d1",
         "sequence": "CCCC", "scores": {"ipTM": 0.71, "pLDDT": 80.0, "i_pae": 12.0}}]}
    html = _render(flask_app, tool, result)
    assert len(TOP_BADGE.findall(html)) == 1, (
        "%s: ranked= defaults to true; a pipeline-ranked list keeps its badge"
        % tool
    )


# ---------------------------------------------------------------------------
# Mixed nulls -- the shape that 500s a sort
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tool", DESIGNS_NATIVE)
def test_a_rebuilt_list_with_partial_scores_renders(tool, flask_app):
    """``_candidate_from_partial`` writes a score key only when the streamed
    partial carried that measurement, and the Storage-listing fallback writes
    ``scores: {}``. So a rebuilt list is routinely MIXED null/non-null -- the
    one shape Jinja's ``sort(attribute=)`` raises on (an all-null column
    short-circuits on ``==`` and passes). Recovered rows are handed over
    unsorted, which is also the true statement about their order.
    """
    result = _recovered()
    result["candidates"][1] = {"rank": 2, "pdb_key": "designs/d1.pdb",
                               "scores": {}}
    html = _render(flask_app, tool, result)
    assert len(CAND_ROW.findall(html)) == 2, (
        "%s: a rebuilt list with one scoreless row lost a row" % tool
    )


_CELL = re.compile(r'data-col="([^"]+)" data-val="([^"]*)"')


@pytest.mark.parametrize("tool", DESIGNS_NATIVE)
def test_a_recovered_run_shows_the_scores_it_kept(tool, flask_app):
    """A recovered row keeps ipTM and pLDDT; the page must show both.

    Five of these partials name columns a rebuilt row never carries
    (``mean_pLDDT``, ``epitope_contacts``, ``ranking_score``), so the rows
    rendered with every number as a dash. The other direction: a normal run
    keeps the tool's own columns.
    """
    cells = set(_CELL.findall(_render(flask_app, tool, _recovered())))
    for col, val in (("ipTM", "0.95"), ("pLDDT", "91")):
        assert any(c == col and v.startswith(val) for c, v in cells), (
            "%s: the recovered %s %s is not on the page; cells: %r"
            % (tool, col, val, sorted(cells))
        )
    empty = {c for c, _ in cells} - {c for c, v in cells if v}
    assert not empty, "%s: recovered rows render always-blank columns %r" % (tool, empty)
    native_cols = {c for c, _ in _CELL.findall(_render(flask_app, tool, _native(tool)))}
    assert not native_cols & {"i_pae"}, (
        "%s: a normal run picked up the recovered column set: %r" % (tool, native_cols)
    )


def test_a_recovered_opendde_run_still_offers_its_downloads(flask_app):
    """The sentence pointing at the per-structure downloads read ``raw_designs``."""
    assert "Download each structure" in _render(flask_app, "opendde", _recovered())


def test_the_recovery_shape_this_file_asserts_on_is_the_one_recovery_builds():
    """Pin the fixture to its producer.

    Every assertion above is only as good as ``_recovered()`` matching what
    ``shared/job_recovery`` actually returns. Built here from the module's own
    ``_candidate_from_partial`` rather than retyped, so a change to the
    recovery shape fails here instead of silently making this file test a
    payload nothing produces.
    """
    from shared.job_recovery import _candidate_from_partial

    built = _candidate_from_partial(
        {"rank": 1, "pdb_key": "some/path/d0.pdb", "iptm": 0.71, "plddt": 80.0})
    assert built == _recovered()["candidates"][0], (
        "job_recovery._candidate_from_partial now builds %r; the fixture in "
        "this file says %r" % (built, _recovered()["candidates"][0])
    )
