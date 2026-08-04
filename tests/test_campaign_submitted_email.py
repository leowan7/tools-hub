"""The lab-handoff emails must report the real shortlist size.

A 'web' lab_campaigns row keeps its shortlist in ``candidate_indices``, but a
'campaign' row (migration 0037) leaves that empty and stores the shortlist in
``candidate_refs`` instead. Both the customer confirmation and the
leo@ranomics.com staff notify read ``candidate_indices`` alone, so every
campaign-sourced handoff was announced as "0 candidates" to both parties.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from shared import email as em


def _flat(text: str) -> str:
    """Collapse whitespace: the HTML template wraps mid-sentence, so the
    rendered count and its noun sit on different lines."""
    return re.sub(r"\s+", " ", text or "")


def _campaign(*, indices=None, refs=None):
    return SimpleNamespace(
        id="11111111-2222-3333-4444-555555555555",
        target_name="HER2",
        assay_type="yeast_display",
        budget_band="pilot",
        candidate_indices=list(indices or []),
        candidate_refs=list(refs) if refs is not None else None,
    )


@pytest.fixture
def sent(monkeypatch):
    """Capture Resend payloads instead of posting them."""
    captured: list[dict] = []

    class _Resp:
        status_code = 200

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.append(json or {})
        return _Resp()

    monkeypatch.setattr(em.requests, "post", fake_post)
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    return captured


def test_campaign_shortlist_count_comes_from_candidate_refs(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[
            {"job_id": "j1", "index": 0},
            {"job_id": "j1", "index": 4},
            {"job_id": "j2", "index": 2},
        ]),
        user_email="scientist@example.com",
    )

    assert len(sent) == 2, "one confirmation + one staff notify"
    user_mail, staff_mail = sent
    # The regression printed "0 candidates" in both.
    assert "3 candidates)" in _flat(user_mail["html"])
    assert "0 candidate" not in _flat(user_mail["html"])
    assert "<td>3</td>" in _flat(staff_mail["html"])
    assert "Candidates: 3" in staff_mail["text"]


def test_web_shortlist_still_counts_candidate_indices(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(indices=[0, 1]),
        user_email="scientist@example.com",
    )
    user_mail, staff_mail = sent
    assert "2 candidates)" in _flat(user_mail["html"])
    assert "Candidates: 2" in staff_mail["text"]


def test_singular_wording_for_one_candidate(sent):
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert "1 candidate)" in _flat(sent[0]["html"])


def test_no_key_sends_nothing(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    calls: list = []
    monkeypatch.setattr(
        em.requests, "post",
        lambda *a, **k: calls.append(1),
    )
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert calls == []


# ---------------------------------------------------------------------------
# Target-sourced handoffs (Phase 5.3, migration 0040)
# ---------------------------------------------------------------------------

_TID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _target_campaign(refs):
    """A 'target' lab_campaigns row: shortlist in candidate_refs, parent is a
    design target rather than a compute campaign or a single job."""
    c = _campaign(refs=refs)
    c.submission_source = "target"
    c.source_target_id = _TID
    c.source_campaign_id = None
    c.source_job_id = None
    return c


def test_a_target_shortlist_counts_candidate_refs_too(sent):
    """The count fix generalises: a 'target' row leaves candidate_indices empty
    for the same reason a 'campaign' row does."""
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([
            {"job_id": "j1", "index": 0},
            {"job_id": "j2", "index": 0},
        ]),
        user_email="scientist@example.com",
        source_tools={"bindcraft": 1, "pxdesign": 1},
    )
    user_mail, staff_mail = sent
    assert "2 candidates)" in _flat(user_mail["html"])
    assert "Candidates: 2" in staff_mail["text"]


def test_the_staff_notify_names_the_tools_the_designs_came_from(sent):
    """The cross-tool spread at a glance. Ordered by count descending then
    slug, so the same shortlist always renders the same string."""
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([{"job_id": "j1", "index": i} for i in range(7)]),
        user_email="scientist@example.com",
        source_tools={"pxdesign": 3, "rfdiffusion": 4},
    )
    staff_mail = sent[1]
    assert "Designs from: rfdiffusion (4), pxdesign (3)" in staff_mail["text"]
    assert "rfdiffusion (4), pxdesign (3)" in _flat(staff_mail["html"])


def test_the_staff_notify_links_back_to_the_target(sent, monkeypatch):
    """Target-aware detail URL. Before this the staff email carried only the
    admin link, so the one page showing WHICH designs were picked, and what
    else the target holds, was not reachable from the notification at all.

    PUBLIC_BASE_URL is pinned rather than assumed: ``app.py`` calls
    ``load_dotenv()`` at import, so whichever test imported the app first
    leaks the repo-root .env's value into this process and the base differs
    between a solo run and a full-suite run. Same guard test_email_real.py
    already uses.
    """
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    em.send_campaign_submitted_emails(
        campaign=_target_campaign([{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
        source_tools={"bindcraft": 1},
    )
    staff_mail = sent[1]
    assert f"Target: https://tools.ranomics.com/targets/{_TID}" in staff_mail["text"]
    assert f"/targets/{_TID}" in staff_mail["html"]


def test_a_campaign_row_links_back_to_its_run_not_a_target(sent, monkeypatch):
    """The sibling arm. _handoff_source_link branches on submission_source, so
    each shape has to be checked; a single 'whichever id is set' rule would
    have printed a target link for a campaign row once 0040 landed."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://tools.ranomics.com")
    c = _campaign(refs=[{"job_id": "j1", "index": 0}])
    c.submission_source = "campaign"
    c.source_campaign_id = "cccccccc-1111-2222-3333-444444444444"
    c.source_job_id = None
    em.send_campaign_submitted_emails(campaign=c, user_email="s@example.com")
    staff_mail = sent[1]
    assert "Run: https://tools.ranomics.com/campaigns/cccccccc" in staff_mail["text"]
    assert "/targets/" not in staff_mail["text"]


def test_without_source_tools_no_designs_from_line_is_printed(sent):
    """Omitted, not printed empty. The campaign and single-job branches do not
    pass it, because they have exactly one tool by construction."""
    em.send_campaign_submitted_emails(
        campaign=_campaign(refs=[{"job_id": "j1", "index": 0}]),
        user_email="scientist@example.com",
    )
    assert "Designs from" not in sent[1]["text"]
    assert "Designs from" not in sent[1]["html"]
