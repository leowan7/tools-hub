"""The two surfaces the guards did not reach: the live status payload and CSV.

WHY THIS FILE EXISTS. A reviewer mutated six things and no test in the repo
noticed any of them. Five were in ``blueprints/jobs.job_status`` and
``shared/exports`` -- and ``grep -rn "status.json" tests/`` returned nothing at
all, so the endpoint where a production defect had just been found and fixed
had no coverage of its own. The pure helpers behind it were pinned; the
payload they feed was not.

Every test here goes through the real route or the real serializer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_JID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def client():
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _ctx():
    return SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )


def _job(tool, partials):
    return SimpleNamespace(
        id=_JID,
        tool=tool,
        preset="pilot",
        status="running",
        created_at="2026-01-01T00:00:00+00:00",
        inputs={"_partial_candidates": partials, "_progress": {}},
        result=None,
        error=None,
        gpu_seconds_used=None,
        started_at=None,
        modal_function_call_id=None,
    )


def _status(client, tool, partials):
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    job = _job(tool, partials)
    # modal_function_call_id is None on the fixture, so the route skips its one
    # Modal poll and answers from the job alone.
    with patch("blueprints.jobs.load_user_context", return_value=_ctx()), \
            patch("blueprints.jobs.get_job", return_value=job):
        resp = client.get(f"/jobs/{_JID}/status.json")
    assert resp.status_code == 200, resp.status_code
    return json.loads(resp.get_data(as_text=True))


# ---------------------------------------------------------------------------
# The live counter
# ---------------------------------------------------------------------------

def test_a_bar_that_cannot_be_answered_reports_null_not_zero(client):
    """THE DEFECT THIS ENDPOINT SHIPPED ONCE, pinned at the payload.

    boltzgen's heartbeat carries ipTM, pLDDT and i_pae; its bar needs a
    refolding RMSD the refold only produces at the end. Reporting 0 asserts
    that nothing met the bar, which is a claim the run has not made -- and the
    first fix reported exactly that, because one partial short on a leg that
    DOES stream flipped its predicate and pinned the count at a 0 it could
    never leave.
    """
    body = _status(client, "boltzgen", [
        {"rank": 1, "iptm": 0.55, "plddt": 92.0},
        {"rank": 2, "iptm": 0.40, "plddt": 60.0},   # short on a streaming leg
    ])
    assert body["passed_count"] is None
    assert body["has_bar"] is True


def test_a_fully_streamed_bar_is_counted(client):
    """rfdiffusion streams all three of its legs, so its counter works."""
    body = _status(client, "rfdiffusion", [
        {"rank": 1, "iptm": 0.90, "plddt": 90.0, "i_pae": 5.0},
        {"rank": 2, "iptm": 0.10, "plddt": 90.0, "i_pae": 5.0},
    ])
    assert body["passed_count"] == 1
    assert body["has_bar"] is True


def test_a_tool_with_no_bar_counts_delivered_and_says_so(client):
    """bindcraft has no bar. The number is a delivered count, and the payload
    has to say which it is -- the live line words itself off ``has_bar``, and
    describing a no-bar tool's designs as "meeting the quality bar" is a claim
    about designs nobody judged."""
    body = _status(client, "bindcraft", [
        {"rank": 1, "iptm": 0.8}, {"rank": 2, "iptm": 0.7},
    ])
    assert body["passed_count"] == 2
    assert body["has_bar"] is False


def test_the_live_rows_carry_the_derived_text_not_a_stored_word(client):
    body = _status(client, "rfdiffusion", [
        {"rank": 1, "iptm": 0.10, "plddt": 90.0, "i_pae": 5.0,
         "filter_status": "pass"},
    ])
    row = body["partial_candidates"][0]
    assert row["bar_verdict"] == "below"
    assert row["bar_text"] == "ipTM 0.100, below 0.65"


def test_a_fabricated_row_says_so_on_the_live_page_too(client):
    """THE REGRESSION THIS ENDPOINT SHIPPED TWICE, now pinned.

    The results table and this endpoint used to branch on the verdict
    separately. When ``unusable`` arrived -- added so a fabricated row could
    not read as measured -- the table learned it and the endpoint did not, so
    the live page showed a smoke stub's invented numbers beside an empty bar
    column while the finished table on the same page said they were
    fabricated. Both render through score_legends.verdict_text now, and that
    is what makes a third state impossible to half-apply.
    """
    body = _status(client, "boltzgen", [
        {"rank": 1, "iptm": 0.46, "plddt": 71.0,
         "filter_status": "stub (smoke)"},
    ])
    row = body["partial_candidates"][0]
    assert row["bar_verdict"] == "unjudged"
    assert row["bar_text"] == "Not usable: smoke-test stub, scores fabricated"


def test_a_placeholder_row_says_so_on_the_live_page(client):
    """The same hole swallowed every parse-failure row: 0.0 / 0.0 / 99.0 is
    what rfdiffusion's AF2 reader writes when the score JSON has no such
    keys, and it rendered an empty bar column."""
    body = _status(client, "rfdiffusion", [
        {"rank": 1, "iptm": 0.0, "plddt": 0.0, "i_pae": 99.0},
    ])
    row = body["partial_candidates"][0]
    assert row["bar_verdict"] == "unjudged"
    assert row["bar_text"].startswith("Not usable: ")
    assert row["bar_text"]


def test_every_verdict_state_renders_some_text_when_a_bar_applies(client):
    """The shape of the bug, generalised: a gating tool may never produce an
    empty bar cell, whatever its measurements look like."""
    body = _status(client, "boltzgen", [
        {"rank": 1, "iptm": 0.9, "plddt": 92.0, "refolding_rmsd": 1.0},
        {"rank": 2, "iptm": 0.9, "plddt": 40.0, "refolding_rmsd": 1.0},
        {"rank": 3, "iptm": 0.9, "plddt": 92.0},
        {"rank": 4, "iptm": 0.9, "plddt": 0.0, "refolding_rmsd": 99.0},
        {"rank": 5, "filter_status": "stub (smoke)"},
    ])
    for row in body["partial_candidates"]:
        assert row["bar_text"], row


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

def _csv(cands):
    from shared.exports import candidates_to_csv

    return candidates_to_csv(cands)


def test_the_stale_verdict_never_reaches_a_csv():
    out = _csv([
        {"pdb_key": "a.pdb",
         "scores": {"ipTM": 0.9, "filter_status": "below threshold"}},
    ])
    assert "below threshold" not in out
    assert "filter_status" not in out


def test_one_stub_row_does_not_reopen_the_column_for_the_rest():
    """The first fix kept the whole ``filter_status`` column whenever ANY row
    was a stub, so one fabricated design shipped every real row's stale verdict
    beside it. Reachable in a cross-job export containing a smoke sub-job."""
    out = _csv([
        {"pdb_key": "a.pdb",
         "scores": {"ipTM": 0.9, "filter_status": "below threshold"}},
        {"pdb_key": "b.pdb",
         "scores": {"ipTM": 0.46, "filter_status": "stub (smoke)"}},
    ])
    assert "below threshold" not in out
    assert "stub (smoke)" in out
    header, real, stub = out.strip().splitlines()
    assert "provenance" in header
    # The marker is on the stub row and nowhere else.
    assert "stub (smoke)" in stub and "stub (smoke)" not in real


def test_fabrication_survives_the_export_at_all():
    """Stripping the stored field took the only marker saying these numbers
    were invented out of every CSV, while the page beside it said so."""
    assert "stub (smoke)" in _csv([
        {"pdb_key": "b.pdb",
         "scores": {"ipTM": 0.46, "filter_status": "stub (smoke)"}},
    ])


def test_an_ordinary_export_gains_no_provenance_column():
    assert "provenance" not in _csv([
        {"pdb_key": "a.pdb", "scores": {"ipTM": 0.9}},
    ])
