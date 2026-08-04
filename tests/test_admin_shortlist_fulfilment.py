"""The admin fulfilment page must show WHICH designs a shortlist named.

Found while wiring the target branch, and it is not a target-only problem:
``templates/admin/campaign_detail.html`` rendered
``campaign.candidate_indices | join(', ')`` for every non-API row. A 'campaign'
row -- live since migration 0037 -- has that column empty by its own CHECK
constraint, so every campaign-sourced handoff has been shown to ops as a
scoping request with no candidates and a "Source job" of "—". Filed as A84.

Rendered through the real route so the assertions read the page an operator
actually gets, not the helper in isolation.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

_STAFF = "leo@ranomics.com"
_TID = str(uuid.uuid4())
_CID = str(uuid.uuid4())


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = "admin-1"
        sess["user_email"] = _STAFF
    return c


def _campaign(**over):
    from shared.campaigns import Campaign
    row = {
        "id": str(uuid.uuid4()),
        "user_id": "u-1",
        "target_name": "HER2",
        "assay_type": "yeast_display",
        "budget_band": "pilot",
        "status": "submitted",
        "submission_source": "web",
        "candidate_indices": [],
    }
    row.update(over)
    return Campaign.from_row(row)


def _job(jid, tool):
    return SimpleNamespace(id=jid, tool=tool)


def _render(client, campaign, jobs=None):
    jobs = jobs or {}
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job",
                  side_effect=lambda jid, **kw: jobs.get(jid)):
        resp = client.get(f"/admin/lab-projects/{campaign.id}")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_a_target_row_shows_its_design_count_and_tool_breakdown(client):
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[
            {"job_id": "j-bc", "index": 0},
            {"job_id": "j-bc", "index": 3},
            {"job_id": "j-px", "index": 1},
        ],
    )
    html = _render(client, campaign,
                   {"j-bc": _job("j-bc", "bindcraft"),
                    "j-px": _job("j-px", "pxdesign")})
    assert "3 designs from 2 jobs" in " ".join(html.split())
    assert "bindcraft (2)" in html
    assert "pxdesign (1)" in html
    assert f"/targets/{_TID}" in html


def test_a_campaign_row_gets_the_same_treatment(client):
    """The pre-existing half of A84. This shape shipped with 0037 and has been
    rendering an empty candidate list ever since; the target branch renders
    identically because both read candidate_refs."""
    campaign = _campaign(
        submission_source="campaign",
        source_campaign_id=_CID,
        candidate_refs=[{"job_id": "j-bc", "index": i} for i in range(4)],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    assert "4 designs from 1 job" in " ".join(html.split())
    assert "bindcraft (4)" in html
    assert f"/campaigns/{_CID}" in html


def test_a_legacy_single_job_row_still_shows_its_indices(client):
    """The arm that must NOT change. A 'web' row keeps its shortlist in
    candidate_indices, and that rendering is correct for it."""
    campaign = _campaign(
        submission_source="web",
        source_job_id="j-solo",
        candidate_indices=[0, 2, 5],
    )
    html = _render(client, campaign)
    assert "0, 2, 5" in html
    assert "(indices, 0-based)" in html
    assert "/jobs/j-solo" in html


def test_a_design_whose_source_job_cannot_be_read_is_disclosed_not_dropped(client):
    """A tool breakdown that quietly omits rows is worse than one that says so:
    the counts would silently disagree with the design count beside them."""
    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[{"job_id": "j-bc", "index": 0},
                        {"job_id": "j-gone", "index": 0}],
    )
    html = _render(client, campaign, {"j-bc": _job("j-bc", "bindcraft")})
    flat = " ".join(html.split())
    assert "2 designs from 2 jobs" in flat
    assert "bindcraft (1)" in html
    assert "1 design could not be attributed to a tool" in flat


def test_job_lookups_are_deduped_by_job_id(client):
    """40 refs across 2 jobs must be 2 reads, not 40. The fulfilment page is a
    staff GET, but a shortlist can legitimately carry hundreds of refs."""
    seen: list[str] = []

    def fake_get_job(jid, **kw):
        seen.append(jid)
        return _job(jid, "bindcraft" if jid == "j-a" else "pxdesign")

    campaign = _campaign(
        submission_source="target",
        source_target_id=_TID,
        candidate_refs=[
            {"job_id": "j-a" if i % 2 else "j-b", "index": i} for i in range(40)
        ],
    )
    with patch("shared.campaigns.get_campaign", return_value=campaign), \
            patch("shared.jobs.get_job", side_effect=fake_get_job):
        resp = client.get(f"/admin/lab-projects/{campaign.id}")
    assert resp.status_code == 200
    assert sorted(seen) == ["j-a", "j-b"], seen
