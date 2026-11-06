"""The visible line that says an unranked "#" column is not a ranking.

``templates/components/candidate_table.html`` renders a one-sentence note
above the table when the caller passes ``ranked=false``. It exists because the
"#" heading carries no title of its own while Score and Pctile do, so before
this line the only accurate statement of what an unranked "#" means lived in a
Jinja comment the browser never sees.

ONE wording, on every page that reaches it, and this file pins why that is
true rather than only that it renders. rfd4 passes ``ranked=false``
unconditionally and ships no score columns. Every other partial that passes
``ranked`` at all -- thirteen of them -- passes
``ranked=(not output.get('backfilled'))``, so it reaches the note only on a
backfilled result; ``mpnn_results.html`` and ``_default_results.html`` pass
no ``ranked=`` and take the macro default ``ranked=true``, so they never
reach it at all. On a backfilled result each of the thirteen DISCARDS its
own score sort,
because the sort is followed immediately by
``candidates = recovered or candidates``. ``test_the_score_sort_is_discarded``
below measures that on af2 instead of taking the template's word for it: an
earlier draft of this file claimed the opposite and justified a second wording
with it.
"""
import os
import re
from types import SimpleNamespace

import pytest

# The RENDERED paragraph, never the bare class name: ``cand-unranked-note``
# also matches the ``.cand-unranked-note {`` CSS rule in the same template,
# which every render carries whether the line is shown or not.
NOTE = re.compile(r'<p class="cand-unranked-note">')
SENTENCE = ("The # column is the order the designs came back in, not a "
            "ranking of how well anything binds.")


def _flat(html: str) -> str:
    """One space for every run of whitespace.

    The sentence wraps across source lines in the template, so a literal
    substring search against the raw render misses it and the test passes on
    a render that does not contain it.
    """
    return re.sub(r"\s+", " ", html)


@pytest.fixture(scope="module")
def flask_app(isolate_supabase_module):
    os.environ.setdefault("SESSION_SECRET_KEY", "test-secret")
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    return app


def _render(flask_app, tool, result):
    from flask import render_template

    job = SimpleNamespace(id="job-1", tool=tool, status="succeeded",
                          inputs={}, result=result)
    with flask_app.test_request_context("/jobs/job-1"):
        return render_template("tools/%s_results.html" % tool, job=job,
                               example=False, send_target_tools=None)


def _backfilled_with_scores():
    """What ``job_recovery._candidate_from_partial`` builds: nested scores.

    Deliberately in WORST-FIRST order -- ipTM 0.71 before 0.95 -- so a page
    that really sorted on a recovered score would come back reversed.
    """
    return {
        "candidates": [
            {"rank": 1, "name": "worst_first", "pdb_key": "designs/d0.pdb",
             "scores": {"ipTM": 0.71, "mean_pLDDT": 80.0}},
            {"rank": 2, "name": "best_second", "pdb_key": "designs/d1.pdb",
             "scores": {"ipTM": 0.95, "mean_pLDDT": 91.0}},
        ],
        "candidate_count": 2,
        "backfilled": True,
    }


def test_a_backfilled_scoring_run_gets_the_note(flask_app):
    """A backfilled af2 run is unranked, so it gets the same one sentence."""
    html = _render(flask_app, "af2", _backfilled_with_scores())
    assert NOTE.search(html), "no unranked note on a backfilled af2 run"
    assert SENTENCE in _flat(html)


def test_the_score_sort_is_discarded_so_the_sentence_is_true(flask_app):
    """The sentence says arrival order, and this is what makes it true.

    ``af2_results.html`` sorts on ``scores.mean_pLDDT`` descending and then
    runs ``candidates = recovered or candidates`` on the very next line, which
    replaces the sorted list with the rebuilt one. ``reconstruct`` walks the
    streamed ``_partial_candidates`` in stored order, so the rendered rows are
    the fixture's order. If that line were removed, the better-scoring design
    would render first and this test reds.

    Asserted on the ipTM cells rather than the design names, because this
    table renders no name column for a recovered af2 run.
    """
    html = _render(flask_app, "af2", _backfilled_with_scores())
    assert 'data-val="0.71"' in html and 'data-val="0.95"' in html
    assert html.index('data-val="0.71"') < html.index('data-val="0.95"')


def test_a_normal_ranked_run_gets_no_note(flask_app):
    """``ranked=(not output.get('backfilled'))`` is true here, so the table is
    ranked and the line would be a false statement."""
    html = _render(flask_app, "af2", {"designs": [
        {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0",
         "mean_plddt": 80.0, "iptm": 0.71},
        {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1",
         "mean_plddt": 91.0, "iptm": 0.95}]})
    assert not NOTE.search(html), "a ranked table got the unranked note"


def test_rfd4_gets_the_note_too(flask_app):
    """rfd4 passes ranked=false unconditionally and resolves no score columns
    (templates/tools/rfd4_results.html:90, :133). This is the page the
    sentence was written for."""
    html = _render(flask_app, "rfd4", {"designs": [
        {"rank": 1, "name": "d0", "pdb_key": "designs/d0.cif"},
        {"rank": 2, "name": "d1", "pdb_key": "designs/d1.cif"}]})
    assert NOTE.search(html), "no unranked note on an rfd4 run"
    assert SENTENCE in _flat(html)
