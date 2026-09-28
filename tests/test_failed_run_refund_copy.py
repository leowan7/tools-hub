"""A run that did not succeed says WHY in plain words and whether it was paid for.

Two surfaces, one decision: ``shared/jobs.py::failure_notice`` is called by
the failure panel in ``templates/job_detail.html`` (through the jinja global
registered in ``app.py``) and by the ``failed`` tone of
``shared/email.py::_result_summary``.

The defect this file pins: both surfaces were gated on ``status == "failed"``.
Every non-succeeded status takes the failed tone
(``shared/email.py::_result_tone``) and a timed-out run is refunded in full
(``no_progress_timeout`` is in ``_REFUNDED_FAILURE_CLASSES``), so a user whose
run timed out was told neither the cause in plain words nor that their wallet
was not charged. Job c70da107 (af2, 2026-08-27) is a real instance: a $4.50
hold released in full, and no surface said so.

The page assertions read the RENDERED text of the real route, not the
template source: a substring of ``job_detail.html`` cannot say which Jinja
branch matched, and the branch is the subject here.
"""

from __future__ import annotations

import re
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.usefixtures("isolate_supabase")

from shared import email as email_mod
from shared.jobs import (
    _BILLED_FAILURE_CLASSES,
    _REFUNDED_FAILURE_CLASSES,
    ToolJob,
    failure_notice,
)

_JID = str(uuid.uuid4())
_HOLD = {"_wallet": {"hold_tx_id": "tx-hold-stub"}}


def _job(**over) -> ToolJob:
    base = {
        "id": _JID,
        "user_id": str(uuid.uuid4()),
        "tool": "af2",
        "preset": "standalone",
        "status": "timeout",
        "failure_class": "no_progress_timeout",
        "inputs": dict(_HOLD),
        "result": None,
        "error": None,
        "modal_function_call_id": "fc-stub-x",
        "job_token": "t" * 64,
        "gpu_seconds_used": 131,
        "created_at": "2026-08-27T12:00:00Z",
        "started_at": None,
        "completed_at": "2026-08-27T18:15:00Z",
    }
    base.update(over)
    return ToolJob.from_row(base)


# ---------------------------------------------------------------------------
# The shared decision
# ---------------------------------------------------------------------------

class TestFailureNotice:
    def test_every_class_a_failed_row_can_carry_has_plain_words(self):
        """Set equality against the classes ``classify_terminal_state`` writes.

        ``succeeded`` and ``completed_no_yield`` are excluded because they are
        ``status="succeeded"`` rows, which ``failure_notice`` answers with
        None. A class added to the enum without a sentence here would render
        the generic fallback rather than its own cause, which is the kind of
        silent degradation this assertion exists to catch.
        """
        from shared.jobs import _FAILURE_CLASS_PLAIN_WORDS

        expected = (_BILLED_FAILURE_CLASSES | _REFUNDED_FAILURE_CLASSES) - {
            "succeeded", "completed_no_yield",
        }
        assert set(_FAILURE_CLASS_PLAIN_WORDS) == expected, (
            sorted(set(_FAILURE_CLASS_PLAIN_WORDS) ^ expected))

    def test_succeeded_job_gets_no_notice(self):
        assert failure_notice(_job(status="succeeded", failure_class="succeeded")) is None

    def test_running_job_gets_no_notice(self):
        assert failure_notice(_job(status="running", failure_class=None)) is None

    def test_timeout_with_hold_is_refunded(self):
        notice = failure_notice(_job())
        assert notice["refunded"] is True
        assert "time limit" in notice["cause"]

    def test_timeout_without_hold_states_the_cause_but_no_money(self):
        """Free-tier runs never carried a hold: cause yes, refund claim no."""
        notice = failure_notice(_job(inputs={}))
        assert notice["refunded"] is False
        assert "time limit" in notice["cause"]

    def test_cancelled_after_gpu_time_is_not_claimed_refunded(self):
        """``user_cancelled`` is a BILLED class: consumed seconds are charged
        by ``_settle_wallet_hold_for_completed_job``, so the page must not
        tell the user they paid nothing."""
        notice = failure_notice(
            _job(status="cancelled", failure_class="user_cancelled",
                 gpu_seconds_used=76))
        assert notice["refunded"] is False

    def test_cancelled_before_any_gpu_time_is_refunded(self):
        notice = failure_notice(
            _job(status="cancelled", failure_class="user_cancelled",
                 gpu_seconds_used=0))
        assert notice["refunded"] is True

    def test_null_class_with_consumed_seconds_is_not_claimed_refunded(self):
        """A NULL ``failure_class`` with GPU time takes the legacy SETTLE arm
        of ``_settle_wallet_hold_for_completed_job`` (refund only when
        ``gpu_seconds <= 0``), so "not charged" would be false here."""
        notice = failure_notice(_job(failure_class=None, gpu_seconds_used=131))
        assert notice["refunded"] is False

    def test_unknown_class_falls_back_without_leaking_the_enum(self):
        notice = failure_notice(_job(failure_class="teleported_away"))
        assert "teleported_away" not in notice["cause"]
        assert notice["cause"]


# ---------------------------------------------------------------------------
# Surface 1 — the completion email
# ---------------------------------------------------------------------------

class TestEmail:
    def test_refunded_timeout_says_wallet_not_charged(self):
        """The regression. Before the fix the ``status == "failed"`` gate in
        ``_result_summary`` excluded this job and the mail said only "The run
        did not complete"."""
        summary = email_mod._result_summary(_job(), tone="failed")
        assert "wallet was not charged" in summary

    def test_cancelled_after_gpu_time_makes_no_refund_claim(self):
        summary = email_mod._result_summary(
            _job(status="cancelled", failure_class="user_cancelled",
                 gpu_seconds_used=76),
            tone="failed",
        )
        assert "wallet was not charged" not in summary


# ---------------------------------------------------------------------------
# Surface 2 — the job page
# ---------------------------------------------------------------------------

class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-secret")
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app.test_client()


def _page(client, job) -> str:
    ctx = SimpleNamespace(
        user_id="u-1", tier="free", balance=100, email="u@example.com",
    )
    with client.session_transaction() as sess:
        sess["user_id"] = "u-1"
        sess["user_email"] = "u@example.com"
    with patch("blueprints.jobs.load_user_context", return_value=ctx), \
            patch("blueprints.jobs.get_job", return_value=job):
        resp = client.get(f"/jobs/{_JID}")
    assert resp.status_code == 200, resp.status_code
    parser = _Text()
    parser.feed(resp.get_data(as_text=True))
    return parser.text


class TestJobPage:
    def test_timeout_page_states_cause_and_refund(self, client):
        """A timed-out row carries no ``error`` dict, so the old
        ``status == 'failed' and job.error`` gate rendered nothing at all."""
        text = _page(client, _job())
        assert "hit its time limit" in text
        assert "Your wallet was not charged for this run." in text

    def test_failed_page_keeps_the_raw_detail_under_the_plain_words(self, client):
        text = _page(client, _job(
            status="failed",
            failure_class="tool_error",
            error={"bucket": "pipeline", "detail": "no *scores*.json in output"},
        ))
        assert "hit an error while running" in text
        assert "Your wallet was not charged for this run." in text
        assert "no *scores*.json in output" in text

    def test_page_makes_no_refund_claim_without_a_wallet_hold(self, client):
        text = _page(client, _job(inputs={}))
        assert "hit its time limit" in text
        assert "wallet was not charged" not in text
