"""A candidate row that is not a dict must not 500 the results page.

HARDENING, NOT AN OBSERVED OUTAGE. An AST sweep of every list append feeding a
``candidates``/``designs`` key across ``tools/``, ``shared/`` and ``webhooks/``
found only dict literals, ``dict(...)`` calls, ``_candidate_from_partial``
and three locals that each resolve to a dict literal in the same function,
so nothing in this repo writes such a row. Five tools have no in-repo
``run_pipeline.py``, and ``shared/jobs.py::_slim_result_for_persist`` guards
its own read with ``isinstance(cand, dict)`` rather than rejecting the row, so
a container-produced non-dict would be STORED rather than refused. This pins
the render layer against that shape; it does not claim it has happened.

THE REAL ROUTE, NOT THE BARE MACRO. Each tool's partial reads ``job.result``
directly instead of going through ``candidate_records``, so the three entry
points fail in three different places and a macro-only test sees one of them:

* pass-through into ``results_panel`` (bindcraft, boltzgen, pxdesign,
  rfantibody, rfdiffusion);
* a per-template loop that runs BEFORE the macro (proteina's numbering count,
  and the reshape loops in boltz2/af2/colabfold/esmfold/iggm/opendde/
  esmfold2-design);
* the ``candidate_table`` macro called directly, which is how
  ``templates/targets/detail.html`` reaches it.

All three are covered here, the first two by driving ``GET /jobs/<id>``.

NO RENUMBERING. ``test_a_later_good_row_keeps_its_own_index`` is the one that
pins the design decision: the fix COERCES a malformed row to ``{}`` and never
drops it, because ``candidate_records`` is indexed BY POSITION downstream --
``shared/storage.py::stage_campaign_candidates`` does ``candidates[idx]`` with
the index the starred row posted. A fix that filtered the bad row out would
still make every assertion about a 500 pass while shipping the wet lab a
different design from the one the user starred.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_JOB_ID = "fc9d14d7-ada8-40f3-995c-56a67e4087a2"

# mpnn is the fourteenth adapter and reads result["sequences"], not
# candidates/designs, so a malformed CANDIDATE row cannot reach its template
# and a "the good rows still render" assertion would have nothing to find.
# Its own key carries the identical defect and is covered separately by
# ``test_a_malformed_sequence_row_cannot_500_the_mpnn_page``.
# Asserted against the live registry below rather than trusted, so a
# fifteenth tool fails here instead of silently escaping the sweep.
_NOT_CANDIDATE_BEARING = {"mpnn"}


def _row(index: int, *, iptm=0.82, plddt=91.0):
    """One candidate carrying BOTH result shapes.

    The partials disagree on where a metric lives -- the canonical shape nests
    everything under ``scores`` while the cofold shape puts it at the row root
    -- so one row that satisfies both lets a single payload drive all thirteen
    tools without a per-tool fixture table to keep in sync.
    """
    return {
        "rank": index,
        "name": "good_design_%d" % index,
        "pdb_key": "designs/d%d.pdb" % index,
        "_source_index": index,
        "scores": {
            "ipTM": iptm,
            "pTM": 0.71,
            "pLDDT": plddt,
            "mean_pLDDT": plddt,
            "epitope_contacts": 3,
            "ranking_score": 0.9,
            "total_reward": 1.2,
            "af2_iptm": iptm,
            "af2_plddt": plddt,
            "rf3_score": 0.5,
            "binder_scrmsd": 1.1,
            "RMSD": 1.1,
            "shape_complementarity": 0.7,
            "surface_hydrophobicity": 0.3,
        },
        "iptm": iptm,
        "ptm": 0.71,
        "complex_plddt": plddt,
        "mean_plddt": plddt,
        "n_epitope_contacts": 3,
        "ranking_score": 0.9,
        "total_aa": 120,
        "total_length": 120,
        "num_chains": 2,
        "seed": 1,
        "sample": 0,
        "target_numbering": "input",
    }


def _result(rows):
    """Both keys, same rows: ``candidate_records`` prefers ``candidates`` and
    the cofold partials read ``designs``, so one payload serves either."""
    return {
        "status": "COMPLETED",
        "tier": "standard",
        "candidates": list(rows),
        "designs": list(rows),
        "designs_total": len(rows),
        "designs_completed": len(rows),
        "n_failures": 0,
        "runtime_seconds": 10,
    }


def _job(tool, result):
    return SimpleNamespace(
        id=_JOB_ID,
        tool=tool,
        preset="default",
        status="succeeded",
        created_at="2026-09-11T00:00:00+00:00",
        inputs={},
        result=result,
        error=None,
        gpu_seconds_used=27,
        campaign_id=None,
        user_id="u-1",
    )


def _get(flask_app, tool, result):
    """Render the real ``/jobs/<id>`` page for ``tool``.

    ``get_job`` is patched with a SimpleNamespace rather than a real ToolJob
    because ToolJob is a frozen dataclass; the existing suites patch it the
    same way.
    """
    client = flask_app.test_client()
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"
    )
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), patch(
        "blueprints.jobs.get_job", return_value=_job(tool, result)
    ), patch(
        "shared.jobs.resolve_user_email_and_meta",
        return_value=("u@example.com", {}),
    ):
        return client.get("/jobs/%s" % _JOB_ID)


def _candidate_bearing(slugs):
    return sorted(set(slugs) - _NOT_CANDIDATE_BEARING)


# A str is the shape the report named; the rest are every other JSON scalar a
# container could emit in a row's place. {} is here because it is the one the
# report called safe and is not: it survives `.get` and then reaches the
# nullable sort, which is a separate defect fixed in the same change.
BAD_ROWS = [
    pytest.param("design_1 failed", id="str"),
    pytest.param(None, id="none"),
    pytest.param(7, id="int"),
    pytest.param([], id="list"),
    pytest.param(True, id="bool"),
    pytest.param({}, id="emptydict"),
]


def test_the_sweep_covers_every_candidate_bearing_adapter(all_tools_app):
    """The tool list is derived, so a new adapter cannot escape the sweep."""
    _flask_app, slugs = all_tools_app
    assert _NOT_CANDIDATE_BEARING <= set(slugs)
    assert len(_candidate_bearing(slugs)) == 13


@pytest.mark.parametrize("bad", BAD_ROWS)
def test_a_malformed_row_cannot_500_any_tools_results_page(all_tools_app, bad):
    flask_app, slugs = all_tools_app
    result = _result([_row(0), bad, _row(2)])
    for tool in _candidate_bearing(slugs):
        resp = _get(flask_app, tool, result)
        assert resp.status_code == 200, "%s returned %s" % (tool, resp.status_code)
        html = resp.get_data(as_text=True)
        # The GOOD rows still render -- a page that survived by dropping the
        # whole table would pass a status-code-only assertion.
        # The GOOD rows, individually: `d0.pdb` and `d2.pdb` are each
        # rendered only by their own row. A coerced row takes the
        # `design_N.pdb` fallback instead, so neither can stand in for
        # the other.
        assert "d0.pdb" in html, tool
        assert "d2.pdb" in html, tool


@pytest.mark.parametrize("bad", BAD_ROWS)
def test_a_malformed_sequence_row_cannot_500_the_mpnn_page(all_tools_app, bad):
    """The same defect one key over, on the one tool neither sink reaches.

    mpnn renders its own sequence table and skips results_shell +
    candidate_table entirely, so the two shared coercions never see its
    rows. Measured on this tree before the template was changed: five of
    these six row types raised on the ``seq.get('score')`` in this file's
    score cell; ``{}`` survived, because there is no sort here.

    Counted, not dropped, for the same reason as the candidate tables:
    ``shared/email.py`` reports this list as ``len(seqs)`` and never reads
    a row, so dropping one here would make the page disagree with the
    email that is already out.
    """
    flask_app, _slugs = all_tools_app
    good = {"seq": "MKVGOOD", "score": 1.2, "recovery": 0.41, "sample": 0}
    other = {"seq": "MKWLAST", "score": 1.1, "recovery": 0.52, "sample": 2}
    result = {
        "status": "COMPLETED",
        "tier": "standard",
        "sequences": [good, bad, other],
        "runtime_seconds": 10,
    }
    resp = _get(flask_app, "mpnn", result)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "MKVGOOD" in html
    assert "MKWLAST" in html
    assert "Sequences (3)" in html


@pytest.mark.parametrize("bad", BAD_ROWS)
def test_a_later_good_row_keeps_its_own_index(all_tools_app, bad):
    """The malformed row is COERCED, never dropped.

    ``data-ref-idx`` is what the shortlist posts and what
    ``shared/storage.py::stage_campaign_candidates`` indexes the raw stored
    list with. Row 2 must still say 2. A fix that filtered the bad row would
    renumber it to 1 and stage the wrong design.
    """
    flask_app, slugs = all_tools_app
    result = _result([_row(0), bad, _row(2)])
    for tool in _candidate_bearing(slugs):
        html = _get(flask_app, tool, result).get_data(as_text=True)
        refs = sorted(int(m) for m in re.findall(r'data-ref-idx="(\d+)"', html))
        assert refs == [0, 1, 2], "%s renumbered to %s" % (tool, refs)


@pytest.mark.parametrize("bad", BAD_ROWS)
def test_the_malformed_row_is_still_counted(all_tools_app, bad):
    """Counts are unchanged on purpose.

    The row is rendered, so it is counted -- which keeps the panel header, the
    completion email's ``candidate_count`` and the ``_source_index`` bounds all
    describing the same list.
    """
    flask_app, slugs = all_tools_app
    result = _result([_row(0), bad, _row(2)])
    for tool in _candidate_bearing(slugs):
        html = _get(flask_app, tool, result).get_data(as_text=True)
        assert "Candidates (3)" in html, tool


def test_the_bare_candidate_table_macro_survives_a_malformed_row(all_tools_app):
    """Family three: ``targets/detail.html`` calls the macro directly.

    ``sort_mode='tool'`` selects the grouped branch, whose
    ``{% set raw_tool = cand.get('_source_tool') %}`` is the earliest
    per-row read in the macro -- earlier than the ``_source_index`` and
    ``pdb_key`` reads the ungrouped path starts with -- so it is the
    branch that fails first on a row that is not a Mapping.
    """
    flask_app, _slugs = all_tools_app
    tmpl = flask_app.jinja_env.from_string(
        "{% from 'components/candidate_table.html' import candidate_table %}"
        "{{ candidate_table(candidates, ['ipTM'], 'job-1', 'bindcraft',"
        " multi_tool=true, sort_mode='tool') }}"
    )
    with flask_app.test_request_context("/"):
        html = tmpl.render(candidates=[_row(0), "design_1 failed", _row(2)])
    assert "d0.pdb" in html
    assert "d2.pdb" in html
    # The renumbering guard belongs here too, not only on the route: this
    # macro is reached directly by targets/detail.html, so a fix that
    # FILTERED here would drop row 1 and leave refs [0, 2] while every
    # aggregator upstream still counted three.
    refs = sorted(int(m) for m in re.findall(r'data-ref-idx="(\d+)"', html))
    assert refs == [0, 1, 2]


def test_a_null_metric_beside_a_number_cannot_500_the_sort(all_tools_app):
    """The second defect behind the same symptom, and not a malformed row.

    Six partials sorted on a nullable key, where Jinja's ``sort`` compares the
    raw values: one design measured and one not raises ``TypeError: '<' not
    supported between instances of 'float' and 'NoneType'``.
    PR #290 replaced all seven -- the six built-in sorts and the
    hand-rolled ``_sort_iptm`` mirror in ``esmfold2_design_results.html``
    -- with the ``sort_by_number`` filter. This asserts that same
    guarantee over the real route rather than a bare Environment,
    because the coercion above DEPENDS on it: the ``{}`` it substitutes
    for a malformed row is itself an unmeasured row.

    An ALL-null column sorts FINE -- Jinja keys each row as a
    one-element list and ``[None] == [None]`` settles the comparison
    before any ``<`` is tried -- so a fixture that nulls the metric on
    every row passes against an unfixed template. This one mixes a
    measured row with an unmeasured one for that reason.
    """
    flask_app, slugs = all_tools_app
    measured = _row(0)
    unmeasured = _row(2, iptm=None, plddt=None)
    unmeasured["rank"] = None
    unmeasured["scores"]["epitope_contacts"] = None
    result = _result([unmeasured, measured])
    for tool in _candidate_bearing(slugs):
        resp = _get(flask_app, tool, result)
        assert resp.status_code == 200, "%s returned %s" % (tool, resp.status_code)
        html = resp.get_data(as_text=True)
        # The GOOD rows, individually: `d0.pdb` and `d2.pdb` are each
        # rendered only by their own row. A coerced row takes the
        # `design_N.pdb` fallback instead, so neither can stand in for
        # the other.
        assert "d0.pdb" in html, tool
        assert "d2.pdb" in html, tool


def test_display_rows_preserves_length_and_order():
    from shared.jobs import display_rows

    rows = [{"a": 1}, "bad", None, {"b": 2}]
    out = display_rows(rows)
    assert out == [{"a": 1}, {}, {}, {"b": 2}]
    assert out[0] is rows[0], "an untouched row must keep its identity"
    assert display_rows(None) == []
    assert display_rows("candidates") == []


def test_a_tuple_of_good_rows_is_not_blanked(all_tools_app):
    """Rejecting a row sequence that is not a ``list`` is a SILENT error.

    ``display_rows`` returning ``[]`` renders the zero-candidate empty state,
    which tells the user the job produced nothing. Over rows that are all
    perfectly good that is worse than the crash this module exists to remove,
    because the page still returns 200 and nothing reports it. Measured on
    these three tools while the guard still read ``list``: a tuple of two
    valid rows rendered 200 with no rows in the table at all.

    One tool from each of the three crash families: bindcraft reaches the
    shared results_shell, boltz2 and proteina render their own partials.
    """
    from shared.jobs import display_rows

    good = [_row(0), _row(1)]
    assert display_rows(tuple(good)) == good

    flask_app, _slugs = all_tools_app
    result = _result(good)
    result["candidates"] = tuple(good)
    result["designs"] = tuple(good)
    for tool in ("bindcraft", "boltz2", "proteina"):
        html = _get(flask_app, tool, result).get_data(as_text=True)
        refs = sorted(int(m) for m in re.findall(r'data-ref-idx="(\d+)"', html))
        assert refs == [0, 1], "%s blanked a tuple: %s" % (tool, refs)


def test_a_tuple_container_is_counted_the_same_everywhere(all_tools_app):
    """The three readers of a candidate array must agree on its length.

    ``display_rows`` accepts a tuple, so ``candidate_records`` and
    ``candidate_count`` have to as well. Measured while only the render
    layer was widened: a tuple candidate array rendered two rows while
    ``candidate_count`` returned ``None`` -- the email's "not stated" --
    and ``candidate_records`` returned ``[]``, so the CSV, FASTA and ZIP
    held nothing. That is the page-says-N, download-says-fewer divergence
    this module exists to keep out. It predates this change -- before it the
    partials iterated the raw value, so a tuple rendered then too, against
    the same two list-only readers -- and routing every partial through one
    reader is what made it fixable in one place.
    """
    from shared.jobs import candidate_count, candidate_records, display_rows

    good = [_row(0), _row(1)]
    result = _result(good)
    # Both keys. ``_result`` writes the rows twice, so a reader that
    # skips a tuple falls through to the other key and returns the right
    # answer for the wrong reason: with only ``candidates`` made a tuple,
    # reverting either sibling guard left this test green (mutation sweep
    # M24, M25).
    result["candidates"] = tuple(good)
    result["designs"] = tuple(good)

    records = candidate_records(result)
    assert records == good
    assert isinstance(records, list), "candidate_records must return a list"
    assert candidate_count(result) == 2

    flask_app, _slugs = all_tools_app
    html = _get(flask_app, "bindcraft", result).get_data(as_text=True)
    rendered = len(re.findall(r'data-ref-idx="(\d+)"', html))
    counted = candidate_count(result)
    assert rendered == counted == len(display_rows(result["candidates"])) == 2

    # An EMPTY tuple short-circuits the key search exactly as an empty list
    # does: "read like a list" holds at the boundary too. Before this change
    # an empty tuple fell through to ``designs`` and an empty list did not,
    # so restoring that fall-through would make a tuple behave unlike a list.
    # An empty candidates array is how a job reports it really delivered zero,
    # not a shape this module cannot read -- see :func:`candidate_count`.
    empty_tuple = dict(result, candidates=(), designs=[_row(0)])
    empty_list = dict(empty_tuple, candidates=[])
    assert candidate_records(empty_tuple) == candidate_records(empty_list) == []
    assert candidate_count(empty_tuple) == candidate_count(empty_list) == 0


def test_candidate_records_stays_raw_and_positional():
    """The fix must NOT be applied here. ``shared/storage.py`` indexes this
    list with the index a starred row posted."""
    from shared.jobs import candidate_records

    assert candidate_records({"candidates": [{"a": 1}, "bad"]}) == [{"a": 1}, "bad"]


def test_staging_a_malformed_row_does_not_raise(monkeypatch):
    """``cand = candidates[idx] or {}`` did not neutralise a TRUTHY non-dict.

    A bare string is truthy, so the ``.get`` on the next line raised
    ``AttributeError`` and took the whole lab submit down. The Storage client
    is faked rather than left unavailable: the real one raises StorageError
    before the loop is ever entered, which would pass this test against the
    unfixed code.
    """
    from shared import storage

    fake = SimpleNamespace(storage=SimpleNamespace(from_=lambda _b: object()))
    monkeypatch.setattr(storage, "get_service_client", lambda: fake)
    # The coerced {} resolves via neither pdb_content_b64 nor pdb_key, which
    # the docstring already calls a silent skip.
    assert (
        storage.stage_campaign_candidates(
            campaign_id="c-1",
            user_id="u-1",
            job_id="j-1",
            candidates=[{"pdb_key": "a.pdb"}, "design_1 failed"],
            indices=[1],
        )
        == []
    )


def test_the_esmfold2_designs_arm_survives_a_malformed_row(all_tools_app):
    """esmfold2-design reshapes designs[] only when result['candidates'] is
    empty, so the shared payload above never reaches that arm."""
    flask_app, _slugs = all_tools_app
    result = _result([_row(0), "design_1 failed", _row(2)])
    result.pop("candidates")
    resp = _get(flask_app, "esmfold2-design", result)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "d0.pdb" in html
    assert "d2.pdb" in html
def test_the_compare_page_survives_a_malformed_row(all_tools_app):
    """/jobs/compare is the second render entry point, and needed no fix.

    Unlike the per-tool partials it reaches every field by DOTTED access,
    which Jinja resolves to Undefined on a non-Mapping instead of raising,
    and its two helpers (judge_design, raw_metric) guard their own reads.
    Rendering this page with and without a display copy produced identical
    HTML apart from the CSRF token, so the coercion that was written here
    first was removed as dead code. This pins that, and it fails if the
    template is ever rewritten to call `.get` on a row.
    """
    flask_app, _slugs = all_tools_app
    result = _result([_row(0), "design_1 failed", _row(2)])
    jobs = []
    for n, tool in enumerate(("bindcraft", "boltz2")):
        j = _job(tool, result)
        j.id = "job-%d" % n
        jobs.append(j)
    client = flask_app.test_client()
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com"
    )
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), patch(
        "shared.jobs.list_jobs_by_ids", return_value=jobs
    ):
        resp = client.get("/jobs/compare?ids=job-0,job-1")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Both columns report the full stored length, malformed row included.
    assert html.count("(3 total)") == 2
    # And the headline is a real design, not the coerced blank.
    assert "good_design_0" in html
