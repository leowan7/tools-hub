"""The visible line that says an unranked "#" column is not a ranking.

``templates/components/candidate_table.html`` renders a one-sentence note
above the table when the caller passes ``ranked=false``. It exists because the
"#" heading carries no title of its own while Score and Pctile do, so before
this line the only accurate statement of what an unranked "#" means lived in a
Jinja comment the browser never sees.

The wording SPLITS on whether any score column survived, and this file pins
both halves plus the silence on a normal run. The split is not cosmetic: rfd4
passes ``ranked=false`` unconditionally and ships no score columns, so its rows
really are arrival order, while every other partial passes
``ranked=(not output.get('backfilled'))`` and seven of them sort on a recovered
score before calling the macro, so on those pages an unranked table is in SCORE
order and the arrival-order sentence would be false.
"""
import os
import re
from types import SimpleNamespace

import pytest

# The RENDERED paragraph, never the bare class name: ``cand-unranked-note``
# also matches the ``.cand-unranked-note {`` CSS rule in the same template,
# which every render carries whether the line is shown or not.
NOTE = re.compile(r'<p class="cand-unranked-note">')
ARRIVAL = "the order the designs came back in"
POSITION = "only each row's place in the list"


def _flat(html: str) -> str:
    """One space for every run of whitespace.

    The two sentences wrap across source lines in the template, so a literal
    substring search against the raw render misses them both and the two
    halves of the split read as interchangeable.
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
    """What ``job_recovery._candidate_from_partial`` builds: nested scores."""
    return {
        "candidates": [
            {"rank": 1, "pdb_key": "designs/d0.pdb",
             "scores": {"ipTM": 0.71, "mean_pLDDT": 80.0}},
            {"rank": 2, "pdb_key": "designs/d1.pdb",
             "scores": {"ipTM": 0.95, "mean_pLDDT": 91.0}},
        ],
        "candidate_count": 2,
        "backfilled": True,
    }


def test_a_backfilled_scoring_run_gets_the_position_wording(flask_app):
    """af2 sorts on mean pLDDT unconditionally (af2_results.html:76), with no
    backfilled branch, so its unranked rows are in SCORE order. The line must
    not tell that reader they are in arrival order."""
    html = _render(flask_app, "af2", _backfilled_with_scores())
    assert NOTE.search(html), "no unranked note on a backfilled af2 run"
    assert POSITION in _flat(html)
    assert ARRIVAL not in _flat(html)


def test_a_normal_ranked_run_gets_no_note(flask_app):
    """``ranked=(not output.get('backfilled'))`` is true here, so the table is
    ranked and the line would be a false statement."""
    html = _render(flask_app, "af2", {"designs": [
        {"rank": 1, "pdb_key": "designs/d0.pdb", "name": "d0",
         "mean_plddt": 80.0, "iptm": 0.71},
        {"rank": 2, "pdb_key": "designs/d1.pdb", "name": "d1",
         "mean_plddt": 91.0, "iptm": 0.95}]})
    assert not NOTE.search(html), "a ranked table got the unranked note"


def test_rfd4_gets_the_arrival_wording(flask_app):
    """rfd4 passes ranked=false unconditionally and resolves no score columns
    (templates/tools/rfd4_results.html:90, :133), so the stronger sentence is
    true there and this is the page it was written for."""
    html = _render(flask_app, "rfd4", {"designs": [
        {"rank": 1, "name": "d0", "pdb_key": "designs/d0.cif"},
        {"rank": 2, "name": "d1", "pdb_key": "designs/d1.cif"}]})
    assert NOTE.search(html), "no unranked note on an rfd4 run"
    assert ARRIVAL in _flat(html)
    assert POSITION not in _flat(html)
