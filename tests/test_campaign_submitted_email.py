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
